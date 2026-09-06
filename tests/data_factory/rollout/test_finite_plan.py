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
