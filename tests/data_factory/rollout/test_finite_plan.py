"""Synthetic native integration; no ROS node, model download, GPU or dataset writes."""
import base64
import copy
import hashlib
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from tools.fr5_data_factory import ContractError, canonical_digest, validate_motion_program
from tools.data_factory.learned_action_adapter import fake_rgb
from tools.data_factory.rollout.finite_plan import (
    FinitePolicyInference, JOINTS, compile_program, validate_proposal,
)
from tools.data_factory.motion.pickup_executor import PickupExecutor
from tools.data_factory.one_job import OneJob
from tools.data_factory.run_job import learned_run_diagnostic, _operator_summary
from tests.data_factory.test_motion import T, snapshot, SCENE
from tests.data_factory.operator.fixtures import motion

XML = '<robot name="synthetic">' + ''.join(
    f'<joint name="{name}" type="{"prismatic" if i == 6 else "revolute"}"><limit lower="{0 if i == 6 else -3}" upper="{.02 if i == 6 else 3}" velocity="{.1 if i == 6 else 10}"/></joint>'
    for i, name in enumerate(JOINTS)) + '</robot>'
CHECKPOINT = {"tree_digest": canonical_digest("synthetic-weights"),
              "training_receipt_digest": canonical_digest("synthetic-receipt"), "runtime": "SYNTHETIC_TEST_ONLY"}
INITIAL = [0.] * 6 + [.01]
ACTION = [.001] * 6 + [.0101]
OPTIONS = dict(instruction="synthetic probe", robot_description=XML, period_s=.1)
APPROVAL = {"source": "HUMAN", "approval_id": "synthetic-approval", "approved_by": "operator",
            "approval_expiry": "2099-01-01T00:00:00Z", "approval_scope": "HUMAN_GATED"}


def observation():
    return {"source_clock": "SYSTEM_TIME", "source_timestamps_s": {key: 10. for key in ("state", "camera1", "camera2")},
            "observation.state": INITIAL[:], "observation.images.camera1": fake_rgb(), "observation.images.camera2": fake_rgb()}


def source():
    value = motion()
    value["binding_digests"]["robot_description_digest"] = 'sha256:' + hashlib.sha256(XML.encode()).hexdigest()
    for step in value["steps"]:
        step["limits"]["execution_timeout_s"] = 4.
    return value


def proposal():
    return FinitePolicyInference(lambda _: [ACTION[:]], CHECKPOINT, source_clock=lambda: 10.).propose(observation(), **OPTIONS)


def redigest(p):
    p["proposal_digest"] = canonical_digest({k: v for k, v in p.items() if k != "proposal_digest"})
    return p


class Transport(T):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.active = False
        self.cancel_count = 0
        self.current = INITIAL[:]
        self.failure = None
        self.on_start = None

    def snapshot(self, *_):
        value = snapshot(self.current[:6], gripper_position=self.current[-1])
        value["gripper_controller"]["reference_position_m"] = self.current[-1]
        return value

    def build_learned_trajectory(self, p):
        self.calls.append("compile-learned")
        return repr(p["actions"]).encode()

    def start_phase(self, step, **kwargs):
        if self.active:
            raise ContractError("ROS_EXEC_ACTIVE")
        self.active = True
        self.sent.append(copy.deepcopy(step))
        if self.on_start:
            self.on_start()
        if kwargs["cancel_event"].is_set():
            raise ContractError("ROS_EXEC_CANCELLED")

    def poll_active(self):
        if not self.active:
            return None
        if self.failure:
            raise ContractError(self.failure)
        self.active = False
        self.current = ACTION[:]
        return object()

    def cancel_active(self, *_):
        self.cancel_count += 1
        self.active = False


class Cell:
    def __init__(self):
        self.ready = True
    def read(self):
        return {"robot_system_id": "fr5-lab-a", "cell_ready": self.ready}
    def mark_blocked(self, *_):
        self.ready = False


class Scene:
    def __init__(self):
        self.updates = []
    @contextmanager
    def locked_snapshot(self, digest):
        yield {"scene_state_digest": digest, "scene_state": {"revision": 1, "objects": {"cube-1": {"state": "ON_SURFACE", "object_profile_id": "cube"}}}}
    def update_object(self, **kwargs):
        self.updates.append(kwargs)


class Recorder:
    def __init__(self, calls):
        self.calls = calls
        self.state = "IDLE"
    def __call__(self, request):
        op = request["op"]
        self.calls.append(("recorder", op))
        self.state = {"begin": "RECORDING", "freeze": "FROZEN", "abort": "ABORTED", "commit": "COMMITTED"}.get(op, self.state)
        return {"schema_version": "data_factory.recorder_response.v1", "op_id": request["op_id"], "op": op,
                "ok": True, "state": self.state, "reason_code": self.state, "run_id": "run", "transaction_id": "tx",
                "episode_index": 0, "metrics": {"rows": 1, "writer_queue": 0, "writer_queue_drops": 0,
                "alignment_failures": 0, "observed_monotonic_ns": time.monotonic_ns()}, "artifacts": {}, "detail": "",
                "writer_alive": True, "writer_error": None, "sampler_alive": True}


class FinitePlanTest(unittest.TestCase):
    def make_held_job(self, initial_feedback=.021):
        # Reuse this file's lifecycle fixtures with the actual ROS serializers,
        # action dispatch, polling and cancellation; no ROS node is constructed.
        from builtin_interfaces.msg import Duration
        from control_msgs.action import FollowJointTrajectory
        from control_msgs.msg import JointTolerance
        from moveit_msgs.action import ExecuteTrajectory
        from moveit_msgs.msg import RobotTrajectory
        from trajectory_msgs.msg import JointTrajectoryPoint
        from rclpy.serialization import serialize_message, deserialize_message
        from tools.data_factory.motion.moveit_transport import RosMoveItTransport

        now = [10.]
        state = {"joints": [0.] * 6, "feedback": initial_feedback, "reference": .021, "age": 0., "complete": False}
        class SyntheticTransport(RosMoveItTransport):
            """Synthetic clients only; retain production rejection of synthetic ROS runs."""
        t = object.__new__(SyntheticTransport)
        t._RobotTrajectory, t._JointTrajectoryPoint, t._Duration = RobotTrajectory, JointTrajectoryPoint, Duration
        t._ExecuteTrajectory, t._FollowJointTrajectory, t._JointTolerance = ExecuteTrajectory, FollowJointTrajectory, JointTolerance
        t._serialize_message, t._deserialize_message = serialize_message, deserialize_message
        t._goal_succeeded, t._goal_canceled, t._goal_aborted = 4, 5, 6
        t._moveit_success, t._gripper_success = 1, 0
        t._active, t._execution_locked = None, False
        t._execute_goal_count = t._gripper_goal_count = 0
        t._clock, t.graph_timeout_s, t.node = lambda: now[0], .1, object()
        t._rclpy = SimpleNamespace(spin_until_future_complete=lambda *a, **kw: None, spin_once=lambda *a, **kw: None)
        t.preflight, t.precommit_safety = T().preflight, T().precommit_safety
        def observe(*_):
            value = snapshot(state["joints"][:], gripper_position=state["feedback"])
            value["gripper_controller"]["reference_position_m"] = state["reference"]
            value["joint_state_age_s"] = state["age"]
            return value
        t.snapshot = observe
        sent, handles = [], []
        def send(goal):
            sent.append(goal)
            state["complete"] = False
            result = (ExecuteTrajectory.Result() if isinstance(goal, ExecuteTrajectory.Goal) else FollowJointTrajectory.Result())
            if isinstance(goal, ExecuteTrajectory.Goal):
                result.error_code.val = 1
            packet = SimpleNamespace(status=4, result=result)
            future = mock.Mock()
            future.done.side_effect = lambda: state["complete"]
            future.result.return_value = packet
            handle = mock.Mock(accepted=True)
            handle.get_result_async.return_value = future
            def cancel():
                state["complete"], packet.status = True, 5
                return mock.Mock(done=lambda: True, result=lambda: SimpleNamespace(goals_canceling=[object()]))
            handle.cancel_goal_async.side_effect = cancel
            handles.append(handle)
            return mock.Mock(done=lambda: True, result=lambda: handle)
        t.execute_trajectory, t.gripper = mock.Mock(), mock.Mock()
        t.execute_trajectory.send_goal_async.side_effect = send
        t.gripper.send_goal_async.side_effect = send
        src = source()
        xml = XML.replace('upper="0.02"', 'upper="0.021"')
        src["binding_digests"]["robot_description_digest"] = 'sha256:' + hashlib.sha256(xml.encode()).hexdigest()
        src["gripper_requirements"].update(command_position_m=.01176, acceptable_feedback_m={"min": .01176, "max": .01218})
        for step in src["steps"]:
            if step["phase"] == "GRIPPER_OPEN":
                step["gripper_position_m"] = .021
            if step["phase"] == "GRIPPER_CLOSE":
                step["gripper_position_m"] = .01176
                step["limits"]["completion_tolerance_m"] = .01218 - .01176
        obs = observation()
        obs["observation.state"] = [0.] * 6 + [initial_feedback]
        actions = [[0.] * 6 + [.021] for _ in range(4)] + [[.001] * 6 + [.01176] for _ in range(8)]
        calls, cell, scene = [], Cell(), Scene()
        executor = PickupExecutor(t, execution_enabled=True, cell_state_store=cell, scene_state_store=scene,
                                  source_clock=lambda: now[0], monotonic_clock=lambda: now[0])
        job = OneJob(Recorder(calls), executor.process)
        inference = FinitePolicyInference(lambda _: actions, CHECKPOINT, source_clock=lambda: now[0])
        planned = job.plan_learned("run", src, SCENE, inference, obs, **{**OPTIONS, "robot_description": xml, "period_s": 1 / 30,
                                  "held_gripper_targets": True, "max_observation_age_s": 5.})
        self.assertTrue(planned["ok"], planned)
        return job, executor, t, state, now, sent, handles, calls

    def test_held_reference_replay_uses_one_gripper_goal_then_fresh_arm_start(self):
        job, executor, transport, state, now, sent, _, calls = self.make_held_job(initial_feedback=.02079)
        frozen = copy.deepcopy(executor.runs["run"]["plan"])
        from tools.data_factory.quality.phase_events import validate_phase_event, validate_phase_event_sequence
        events = []
        def emit(record):
            events.append(validate_phase_event(record, plan=frozen))
            return True
        executor._phase_event_writer = SimpleNamespace(emit=emit, ready=True, error_code=None)
        executor.event_clock = lambda: (int(now[0] * 1e9), "SYSTEM_TIME")
        self.assertEqual(sent, [])

        self.assertEqual(calls, [])
        self.assertTrue(job.approve(APPROVAL)["ok"])
        self.assertTrue(job.start()["ok"])
        self.assertEqual(job.poll()["state"], "PRECONTACT_HUMAN")
        with mock.patch('tools.data_factory.motion.moveit_transport.time.time', side_effect=lambda: now[0]):
            confirmed = job.confirm("operator")
            self.assertTrue(confirmed["ok"], confirmed["code"])
            self.assertEqual(len(sent), 1)
            state["complete"] = True  # first arm segment completed at open reference
            job.poll()
            self.assertEqual(len(sent), 2)
            self.assertEqual([list(p.positions) for p in sent[1].trajectory.points], [[.01176], [.01176]])
            for _ in range(11):
                job.poll()
            self.assertEqual(len(sent), 2)  # no repeated close / no early arm send
            now[0] += 11 / 30
            state.update(reference=.01176, feedback=.01218)
            job.poll()
            self.assertEqual(len(sent), 2)  # matching feedback is not terminal evidence
            now[0] += 1.01 - 11 / 30
            state["complete"] = True
            job.poll()
            self.assertEqual(len(sent), 3)
            self.assertEqual(transport._gripper_goal_count, 1)
            self.assertEqual(list(sent[2].trajectory.joint_trajectory.joint_names), JOINTS[:6])
            self.assertEqual(list(sent[2].trajectory.joint_trajectory.points[0].positions), [0.] * 6)
            start = executor.runs["run"]["execution"]["learned_start_observation"]
            self.assertEqual(start["snapshot"]["gripper_controller"]["feedback_position_m"], .01218)
            self.assertEqual(start["captured_at_s"], now[0])
            state.update(complete=True, joints=[.001] * 6)
            self.assertEqual(job.poll()["state"], "SEMANTIC_VERDICT")
        self.assertEqual(executor.runs["run"]["plan"], frozen)
        self.assertTrue(job.semantic_verdict("PASS", "operator")["ok"])
        diagnostic = learned_run_diagnostic(job.poll())
        self.assertEqual(diagnostic["execution_trace"]["status"], "COMPLETED")
        self.assertEqual(len(diagnostic["execution_trace"]["segments"]), 3)
        self.assertEqual(diagnostic["task_effectiveness"], "UNKNOWN")
        self.assertNotIn(("recorder", "commit"), calls)
        self.assertEqual(validate_phase_event_sequence(events, plan=frozen), events)
        for event_type in ("GOAL_ACCEPTED", "ACTION_TERMINAL"):
            self.assertEqual([(e["segment_index"], e["segment_count"]) for e in events if e["event"] == event_type],
                             [(0, 3), (1, 3), (2, 3)])
        from tools.data_factory.rollout.finite_plan import validate_execution_trace
        trace = copy.deepcopy(diagnostic["execution_trace"])
        trace["terminal_state"][0] += .001
        trace["trace_digest"] = canonical_digest({k: v for k, v in trace.items() if k != "trace_digest"})
        with self.assertRaisesRegex(ContractError, "LEARNED_TRACE_TERMINAL"):
            validate_execution_trace(frozen, trace)

    def test_held_float32_profile_reference_is_preserved_without_snapping(self):
        import struct
        job, executor, _, _, _, _, _, _ = self.make_held_job()
        p = copy.deepcopy(executor.runs["run"]["plan"]["learned_proposal"])
        for row in p["actions"]:
            row[-1] = struct.unpack('f', struct.pack('f', row[-1]))[0]
        program = compile_program(job._program["source_program"], redigest(p))
        segments = program["steps"][0]["held_target_segments"]
        target = p["actions"][4][-1]
        self.assertNotEqual(target, .01176)
        self.assertEqual(next(s for s in segments if s["action_range"] == [4, 4])["gripper_position_m"], target)

    def test_held_reference_replay_failure_staleness_and_cancel_never_send_next_arm(self):
        failures = {"stale": "LEARNED_STALE_STATE", "future": "LEARNED_STALE_STATE",
                    "feedback": "GRIPPER_FEEDBACK_OUT_OF_RANGE", "reference": "GRIPPER_FEEDBACK_OUT_OF_RANGE",
                    "arm_drift": "LEARNED_TERMINAL_STATE", "aborted": "ROS_EXEC_FAILED",
                    "cancel": "CANCELLED_BY_OPERATOR", "unresolved": "CANCELLED_BY_OPERATOR"}
        for failure, expected in failures.items():
            with self.subTest(failure=failure):
                job, executor, t, state, now, sent, handles, _ = self.make_held_job()
                job.approve(APPROVAL)
                job.start()
                job.poll()
                with mock.patch('tools.data_factory.motion.moveit_transport.time.time', side_effect=lambda: now[0]):
                    confirmed = job.confirm("operator")
                    self.assertTrue(confirmed["ok"], confirmed["code"])
                    state["complete"] = True
                    job.poll()
                    if failure == "cancel":
                        job.cancel()
                    elif failure == "unresolved":
                        handles[-1].cancel_goal_async.return_value = None
                        handles[-1].cancel_goal_async.side_effect = lambda: mock.Mock(done=lambda: False)
                        job.cancel()
                    else:
                        state.update(complete=True, reference=.01176, feedback=.01218)
                        if failure == "stale": state["age"] = 2.
                        if failure == "future": state["age"] = -1.
                        if failure == "feedback": state["feedback"] = .014
                        if failure == "reference": state["reference"] = .021
                        if failure == "arm_drift": state["joints"] = [.1] * 6
                        if failure == "aborted": handles[-1].get_result_async.return_value.result.return_value.status = 6
                        job.poll()
                    job.poll()
                    self.assertEqual(len(sent), 2)
                    self.assertEqual(executor.runs["run"]["state"], "BLOCKED")
                    self.assertEqual(executor.runs["run"]["failure_code"], expected)
                    if failure in {"cancel", "unresolved"}:
                        handles[-1].cancel_goal_async.assert_called_once()
                    if failure == "unresolved":
                        self.assertTrue(t.owns_active_goal)

    def test_held_target_contract_rejects_unbound_targets_limits_and_unbounded_holds(self):
        job, executor, transport, _, _, sent, _, _ = self.make_held_job()
        p = executor.runs["run"]["plan"]["learned_proposal"]
        src = job._program["source_program"]
        cases = [
            ("LEARNED_UNBOUND_GRIPPER_TARGET", lambda value: value["actions"][4].__setitem__(6, .012)),
            ("LEARNED_JOINT_LIMIT", lambda value: value["actions"][4].__setitem__(6, .022)),
            ("LEARNED_VELOCITY_LIMIT", lambda value: value["actions"][4].__setitem__(0, 2.)),
            ("LEARNED_ACTION_7D", lambda value: value["actions"].__setitem__(4, [0.] * 6)),
            ("LEARNED_HELD_HORIZON", lambda value: value.update(actions=[[0.] * 6 + [(.01176 if i % 2 == 0 else .021)] for i in range(6)])),
            ("LEARNED_VELOCITY_LIMIT", lambda value: value.update(schema_version="data_factory.finite_learned_proposal.v1")),
        ]
        for code, mutate in cases:
            with self.subTest(code=code):
                value = copy.deepcopy(p)
                mutate(value)
                with self.assertRaisesRegex(ContractError, code):
                    compile_program(src, redigest(value))
        with self.assertRaisesRegex(ContractError, "LEARNED_HELD_SEGMENTS_REQUIRED"):
            transport.build_learned_trajectory(p)
        self.assertEqual(sent, [])

    def test_continuous_references_and_staged_source_reject_without_rewriting_inputs(self):
        # These are in-limit continuous outputs, not an inferred close/open class.
        # A production staged source must not be stripped to make them executable.
        job, executor, _, _, now, sent, _, calls = self.make_held_job()
        xml = executor.runs["run"]["plan"]["learned_proposal"]["robot_description"]
        for staged, expected in ((False, "LEARNED_UNBOUND_GRIPPER_TARGET"),
                                 (True, "LEARNED_HELD_PROFILE_UNSUPPORTED")):
            with self.subTest(staged=staged):
                src = copy.deepcopy(job._program["source_program"])
                if staged:
                    opened = next(s for s in src["steps"] if s["phase"] == "GRIPPER_OPEN")
                    opened.update(release_position_m=.0126, release_hold_s=.5)
                validate_motion_program(src)
                actions = [[0.] * 6 + [.016342543065547943 + i * .00001] for i in range(50)]
                original_source, original_actions = copy.deepcopy(src), copy.deepcopy(actions)
                policy = mock.Mock(return_value=actions)
                inference = FinitePolicyInference(policy, CHECKPOINT, source_clock=lambda: now[0])
                consumer = OneJob(Recorder(calls), executor.process)
                obs = observation()
                obs["observation.state"] = [0.] * 6 + [.021]
                result = consumer.plan_learned(
                    "continuous", src, SCENE, inference, obs,
                    **{**OPTIONS, "robot_description": xml, "period_s": 1 / 30,
                       "held_gripper_targets": True, "max_observation_age_s": 5.},
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], expected)
                policy.assert_called_once()
                self.assertEqual(src, original_source)
                self.assertEqual(actions, original_actions)
                self.assertNotIn("continuous", executor.runs)
                self.assertEqual(sent, [])
                self.assertEqual(calls, [])

    def test_held_start_snapshot_is_rechecked_after_deserialization_before_send(self):
        job, executor, t, _, now, sent, _, _ = self.make_held_job()
        job.approve(APPROVAL)
        job.start()
        job.poll()
        original = t._compiled_execution_goal
        def slow_decode(step):
            result = original(step)
            now[0] += 1.1  # proposal is still valid; the arm-start observation is stale
            return result
        with mock.patch.object(t, '_compiled_execution_goal', side_effect=slow_decode), mock.patch(
                'tools.data_factory.motion.moveit_transport.time.time', side_effect=lambda: now[0]):
            result = job.confirm("operator")
        self.assertEqual(result["code"], "LEARNED_STALE_STATE")
        self.assertEqual(sent, [])
        self.assertEqual(executor.runs["run"]["state"], "BLOCKED")

    def test_native_transport_rejects_changed_held_arm_message(self):
        _, executor, t, _, _, sent, _, _ = self.make_held_job()
        step = copy.deepcopy(executor.runs["run"]["plan"]["steps"][0]["held_target_segments"][0])
        message = t._deserialize_message(base64.b64decode(step["trajectory_b64"]), t._RobotTrajectory)
        message.joint_trajectory.points[-1].positions[0] += .001
        step["trajectory_b64"] = base64.b64encode(t._serialize_message(message)).decode()
        with self.assertRaisesRegex(ContractError, "LEARNED_SERIALIZED_ACTION_MISMATCH"):
            t.start_phase(step)
        self.assertEqual(sent, [])

    def test_cancel_during_held_completion_snapshot_fences_late_next_segment(self):
        job, executor, t, state, now, sent, _, _ = self.make_held_job()
        job.approve(APPROVAL)
        job.start()
        job.poll()
        with mock.patch('tools.data_factory.motion.moveit_transport.time.time', side_effect=lambda: now[0]):
            job.confirm("operator")
            state["complete"] = True
            job.poll()
            state.update(complete=True, reference=.01176, feedback=.01218)
            observe = t.snapshot
            def late_observation(*args):
                value = observe(*args)
                executor._fault(executor.runs["run"], "SYNTHETIC_CANCEL_DURING_SNAPSHOT")
                return value
            with mock.patch.object(t, 'snapshot', side_effect=late_observation):
                result = job.poll()
        self.assertEqual(result["code"], "SYNTHETIC_CANCEL_DURING_SNAPSHOT")
        self.assertEqual(len(sent), 2)

    def test_held_collision_sampling_covers_gripper_travel_and_feedback_bounds(self):
        from moveit_msgs.msg import RobotState
        from moveit_msgs.srv import GetStateValidity
        from sensor_msgs.msg import JointState
        _, executor, t, _, _, sent, _, _ = self.make_held_job()
        t._GetStateValidity, t._RobotState, t._JointState = GetStateValidity, RobotState, JointState
        requests = []
        def service(_kind, _endpoint, request, _code):
            requests.append(request)
            return SimpleNamespace(valid=True)
        t._service = service
        plan = executor.runs["run"]["plan"]
        report = t._check_plan_collision(plan, .021)
        self.assertTrue(report["all_valid"])
        self.assertTrue(all(request.group_name == '' for request in requests))
        values = [request.robot_state.joint_state.position[-1] for request in requests]
        self.assertIn(.01218, values)
        self.assertTrue(any(.01218 < value < .020 for value in values))
        self.assertEqual(sent, [])
        t._service = lambda *args: SimpleNamespace(valid=args[2].robot_state.joint_state.position[-1] != .01218)
        with self.assertRaisesRegex(ContractError, "COLLISION_DETECTED"):
            t._check_plan_collision(plan, .021)

    def held_phase_events(self):
        from tools.data_factory.quality.phase_events import validate_phase_event
        _, executor, _, _, _, _, _, _ = self.make_held_job()
        run = executor.runs["run"]
        run["execution"] = {"phase_event_sequence": 0, "step_index": 0}
        events, rows, clock = [], [], [10.]
        def emit(record):
            events.append(validate_phase_event(record, plan=run["plan"]))
            return True
        executor._phase_event_writer = SimpleNamespace(emit=emit)
        executor.event_clock = lambda: (int(clock[0] * 1e9), "SYSTEM_TIME")
        for index, step in enumerate(run["plan"]["steps"][0]["held_target_segments"]):
            run["execution"]["learned_segment_index"] = index
            clock[0] = 10. + index * 2
            executor._emit_phase_event(run, "GOAL_ACCEPTED", step, "ACCEPTED", {"step": step, "accepted": True})
            rows.extend({"target_ros_s": clock[0] + (j + 1) / (index + 2)} for j in range(index + 1))
            clock[0] += 1.
            executor._emit_phase_event(run, "ACTION_TERMINAL", step, "SUCCEEDED", {"step": step, "terminal_status": "SUCCEEDED"})
        self.assertEqual(len(events), 6)
        return run["plan"], events, rows

    def test_held_phase_event_identity_requires_exact_plan_and_unique_segments(self):
        from tools.data_factory.quality.phase_events import validate_phase_event_sequence, validate_phase_event
        plan, events, _ = self.held_phase_events()
        self.assertEqual(validate_phase_event_sequence(events, plan=plan), events)
        self.assertEqual([(event["segment_index"], event["segment_count"]) for event in events[::2]], [(0, 3), (1, 3), (2, 3)])
        with self.assertRaisesRegex(ContractError, "PHASE_EVENT_PLAN_REQUIRED"):
            validate_phase_event_sequence(events)
        for changes, code in (({"segment_index": True}, "PHASE_EVENT_SEGMENT"),
                              ({"segment_count": 2.5}, "PHASE_EVENT_SEGMENT"),
                              ({"segment_count": 2}, "PHASE_EVENT_SEGMENT"),
                              ({"segment_index": 3}, "PHASE_EVENT_SEGMENT"),
                              ({"segment_index": 1}, "PHASE_EVENT_SEGMENT_BINDING"),
                              ({"plan_digest": canonical_digest("other")}, "PHASE_EVENT_PLAN_BINDING")):
            with self.subTest(changes=changes), self.assertRaisesRegex(ContractError, code):
                validate_phase_event({**events[0], **changes}, plan=plan)
        with self.assertRaisesRegex(ContractError, "PHASE_EVENT_SEGMENT_DUPLICATE"):
            validate_phase_event_sequence([*events, {**events[-2], "sequence": 6}], plan=plan)
        with self.assertRaisesRegex(ContractError, "PHASE_EVENT_SEGMENT_ORDER"):
            validate_phase_event_sequence([*events[2:4], *events[:2]], plan=plan)

    def test_held_phase_rows_reach_existing_report_without_count_or_arm_aliasing(self):
        import tempfile
        from pathlib import Path
        from tools.data_factory.quality.episode_report import build_episode_report
        from tools.data_factory.quality.phase_events import PhaseEventWriter, read_phase_events
        from tools.data_factory.quality.phase_metrics import phase_timing_attribute
        plan, events, rows = self.held_phase_events()
        for row in rows:
            index = int((row["target_ros_s"] - 10.) // 2)
            step = plan["steps"][0]["held_target_segments"][index]
            row["action"] = [*step["final_joint_state"], step["gripper_position_m"]]
            row["observation.state"] = [*step["final_joint_state"], .021 if index == 0 else .01218]
        common = {"run_id": "run", "resolved_job_digest": plan["resolved_job_digest"],
                  "plan_digest": canonical_digest(plan), "plan": plan,
                  "recorder_rows": rows, "recorder_rows_digest": canonical_digest(rows),
                  "recorder_ros_clock_type": "SYSTEM_TIME"}
        with tempfile.TemporaryDirectory() as directory:
            sidecar = Path(directory) / "phase_events.jsonl"
            writer = PhaseEventWriter(sidecar, plan=plan)
            for event in events:
                self.assertTrue(writer.emit(event))
            self.assertTrue(writer.close())
            self.assertEqual(read_phase_events(sidecar, plan=plan), events)
            report = build_episode_report(Path(directory) / "episode_quality.json", **common,
                phase_events_path=sidecar, execution_evidence={}, stall_epsilon_rad=1e-4,
                technical_validator={"schema_version": "data_factory.technical_validator_ref.v1",
                                     "status": "PASS", "result_digest": canonical_digest("synthetic-only")})
        attributes = {item["attribute"]: item for item in report["attributes"]}
        timing = attributes["phase_timing_integrity"]
        self.assertEqual(timing["status"], "AVAILABLE")
        self.assertEqual([item["row_count"] for item in timing["metrics"]["phase_intervals"]], [1, 2, 3])
        self.assertEqual(timing["metrics"]["joined_row_count"], 6)
        joints = attributes["joint_execution_quality"]["metrics"]["phase_metrics"]
        self.assertEqual([item["segment_index"] for item in joints], [0, 2])
        self.assertEqual([item["row_count"] for item in joints], [1, 3])
        self.assertEqual([item["endpoint_joint_error_max_rad"] for item in joints], [0., 0.])
        interaction = attributes["interaction_quality"]
        self.assertEqual(interaction["status"], "NOT_AVAILABLE")
        self.assertEqual(interaction["flags"], ["LEARNED_INTERACTION_UNQUALIFIED"])
        self.assertIsNone(interaction["metrics"]["gripper_close"])
        self.assertIsNone(interaction["metrics"]["lift_continuity"])
        with self.assertRaisesRegex(ContractError, "PHASE_EVENT_SEGMENT_DUPLICATE"):
            phase_timing_attribute(**common, events=[*events, {**events[0], "sequence": 6}])
        with self.assertRaisesRegex(ContractError, "PHASE_EVENT_PLAN_REQUIRED"):
            phase_timing_attribute(**{k: v for k, v in common.items() if k != "plan"}, events=events)

    def test_post_inference_source_clock_freshness_and_reentrant_inference(self):
        now = [10.]
        def slow(_):
            now[0] += .4
            return [ACTION]
        inference = FinitePolicyInference(slow, CHECKPOINT, source_clock=lambda: now[0])
        with self.assertRaisesRegex(ContractError, "LEARNED_STALE_OBSERVATION"):
            inference.propose(observation(), **OPTIONS)
        inference = None
        def recursive(_):
            with self.assertRaisesRegex(ContractError, "LEARNED_REENTRANT_INFERENCE"):
                inference.propose(observation(), **OPTIONS)
            return [ACTION]
        inference = FinitePolicyInference(recursive, CHECKPOINT, source_clock=lambda: 10.)
        with self.assertRaisesRegex(ContractError, "LEARNED_CANCELLED"):
            inference.propose(observation(), **OPTIONS)

    def test_cancel_fences_late_result_from_another_thread(self):
        entered, release = threading.Event(), threading.Event()
        def policy(_):
            entered.set()
            self.assertTrue(release.wait(2))
            return [ACTION]
        inference = FinitePolicyInference(policy, CHECKPOINT, source_clock=lambda: 10.)
        failures = []
        def work():
            try:
                inference.propose(observation(), **OPTIONS)
            except ContractError as error:
                failures.append(error.code)
        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(entered.wait(2))
        inference.cancel()
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, ["LEARNED_CANCELLED"])

    def test_exact_units_limits_source_clocks_and_horizon(self):
        mutations = [
            ("LEARNED_ACTION_CONTRACT", lambda p: p.update(units=["deg"] * 6 + ["m"])),
            ("LEARNED_ACTION_CONTRACT", lambda p: p.update(action_semantics="DELTA")),
            ("LEARNED_ACTION_7D", lambda p: p.update(actions=[[0.] * 6])),
            ("LEARNED_JOINT_LIMIT", lambda p: p.update(actions=[[0.] * 6 + [21.]])),
            ("LEARNED_VELOCITY_LIMIT", lambda p: p.update(actions=[[2.] * 6 + [.01]])),
            ("LEARNED_HORIZON", lambda p: p.update(actions=[ACTION] * 51)),
            ("LEARNED_HORIZON", lambda p: p.update(period_s=.001)),
            ("LEARNED_HORIZON", lambda p: p.update(period_s=6.)),
            ("LEARNED_STALE_OBSERVATION", lambda p: p["source_timestamps_s"].update(camera2=9.)),
            ("LEARNED_STALE_OBSERVATION", lambda p: p["source_timestamps_s"].update(state=11.)),
        ]
        for code, mutate in mutations:
            with self.subTest(code=code):
                p = proposal()
                mutate(p)
                with self.assertRaisesRegex(ContractError, code):
                    validate_proposal(redigest(p))

    def make_job(self):
        calls = []
        transport, cell, scene = Transport(), Cell(), Scene()
        now = [10.]
        executor = PickupExecutor(transport, execution_enabled=True, cell_state_store=cell,
                                  scene_state_store=scene, source_clock=lambda: now[0], monotonic_clock=lambda: 10.)
        def execute(request):
            calls.append(("executor", request["op"]))
            return executor.process(request)
        job = OneJob(Recorder(calls), execute)
        inference = FinitePolicyInference(lambda _: [ACTION[:]], CHECKPOINT, source_clock=lambda: now[0])
        planned = job.plan_learned("run", source(), SCENE, inference, observation(), **OPTIONS)
        self.assertTrue(planned["ok"], planned)
        return job, executor, transport, cell, scene, now, calls

    def start_job(self):
        values = self.make_job()
        job = values[0]
        self.assertTrue(job.approve(APPROVAL)["ok"])
        self.assertTrue(job.start()["ok"])
        self.assertEqual(job.poll()["state"], "PRECONTACT_HUMAN")
        return values

    def test_native_program_canonical_validation_and_plan_only_zero_effects(self):
        src = source()
        original = copy.deepcopy(src)
        p = proposal()
        program = compile_program(src, p)
        self.assertEqual(validate_motion_program(program), program)
        self.assertEqual(src, original)
        self.assertEqual([s["phase"] for s in program["steps"]], ["LEARNED_CHUNK"])
        program["steps"].append(src["steps"][-1])
        with self.assertRaisesRegex(ContractError, "LEARNED_PROGRAM_BINDING"):
            validate_motion_program(program)
        job, _, transport, cell, scene, _, calls = self.make_job()
        self.assertEqual(transport.sent, [])
        self.assertFalse(any(target == "recorder" for target, _ in calls))
        self.assertTrue(cell.ready)
        self.assertEqual(scene.updates, [])
        self.assertEqual(_operator_summary(job._result())["path"], ["LEARNED_CHUNK"])

    def test_completed_probe_flows_to_diagnostic_and_cannot_commit_pending_safety(self):
        job, executor, transport, cell, scene, _, calls = self.start_job()
        self.assertTrue(job.confirm("operator")["ok"])
        self.assertEqual(job.poll()["state"], "SEMANTIC_VERDICT")
        self.assertEqual(job.recorder_state, "FROZEN")
        self.assertTrue(job.semantic_verdict("PASS", "operator")["ok"])
        result = job.poll()
        self.assertEqual((result["code"], result["recorder_state"]), ("PRECOMMIT_SAFETY", "ABORTED"))
        self.assertEqual(executor.runs["run"]["state"], "COMPLETED")
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(transport.sent[0]["learned_proposal"]["actions"], [ACTION])
        self.assertFalse(cell.ready)
        self.assertEqual([entry["state"] for entry in scene.updates], ["UNKNOWN"])
        self.assertNotIn(("recorder", "commit"), calls)
        self.assertLess(calls.index(("recorder", "begin")), calls.index(("executor", "execute")))
        diagnostic = learned_run_diagnostic(result)
        self.assertEqual(diagnostic["execution_trace"]["status"], "COMPLETED")
        self.assertEqual(diagnostic["task_effectiveness"], "UNKNOWN")
        self.assertIsNone(diagnostic["episode_ledger"])
        self.assertFalse(diagnostic["training_authorized"])
        tampered = copy.deepcopy(result)
        tampered["plan_envelope"]["plan"]["learned_proposal"]["actions"][0][0] += .001
        with self.assertRaises(ContractError):
            learned_run_diagnostic(tampered)

    def test_source_clock_ages_between_approval_and_send_rejects_without_goal(self):
        job, _, transport, _, _, now, _ = self.start_job()
        now[0] += .4
        result = job.confirm("operator")
        self.assertFalse(result["ok"])
        self.assertEqual(transport.sent, [])
        self.assertEqual(result["recorder_state"], "ABORTED")

    def test_controller_fault_uses_existing_cancel_owner_and_failure_diagnostic(self):
        job, _, transport, cell, scene, _, calls = self.start_job()
        self.assertTrue(job.confirm("operator")["ok"])
        transport.failure = "ROS_EXEC_RESULT_TIMEOUT"
        result = job.poll()
        self.assertEqual(result["code"], "ROS_EXEC_RESULT_TIMEOUT")
        self.assertEqual((len(transport.sent), transport.cancel_count), (1, 1))
        self.assertEqual(learned_run_diagnostic(result)["execution_trace"]["status"], "FAILED")
        job.cancel()
        self.assertEqual(transport.cancel_count, 1)
        self.assertFalse(cell.ready)
        self.assertNotIn(("recorder", "commit"), calls)

    def test_recursive_executor_command_during_send_cannot_dispatch_again(self):
        job, executor, transport, _, _, _, _ = self.start_job()
        responses = []
        transport.on_start = lambda: responses.append(executor.process({"schema_version": "fr5.pickup_executor.command.v4",
            "op_id": "recursive", "op": "status", "payload": {"run_id": "run", "plan_digest": job.plan_digest}}))
        result = job.confirm("operator")
        self.assertFalse(result["ok"])
        self.assertEqual(responses[0]["code"], "REENTRANT_COMMAND")
        self.assertEqual((len(transport.sent), transport.cancel_count), (1, 1))


if __name__ == '__main__':
    unittest.main()
