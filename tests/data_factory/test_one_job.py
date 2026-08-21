import json
import sys
import threading
import time
import unittest
from unittest.mock import patch

from tools.data_factory.one_job import JsonlProcess, OneJob, run_one_job
from tools.data_factory.scene_state import release_slot
from tools.fr5_data_factory import ContractError, canonical_digest


PROGRAM = {"schema_version": "fr5.motion_program.v2"}
PHASES = ("PREGRASP_PTP","APPROACH_STOP_LIN","FINAL_APPROACH_LIN","GRIPPER_CLOSE","LIFT_LIN","RECYCLE_APPROACH_PTP","LOWER_LIN","GRIPPER_OPEN","RETREAT_LIN","SAFE_POSE_PTP")
BINDINGS = {key: "sha256:" + "c" * 64 for key in ("selected_sheet", "yaw0_sheet", "cell_calibration", "robot_system", "collection_profile", "object_profile", "grasp_profile", "planning_scene_digest")}
RESOLVED = "sha256:" + "b" * 64
SETUP_APPROVAL = {"source":"HUMAN", "approval_id":"setup-1", "approved_by":"operator", "approval_expiry":"2099-01-01T00:00:00Z", "resolved_job_digest":RESOLVED}
SCENE = {"scene_state_digest":"sha256:" + "8" * 64, "revision":1, "object_instance_id":"cube-1"}
RELEASE_SCENE = {**SCENE, "release_slot": release_slot(
    robot_system_id="fr5-lab-a",
    pose={"place_id":"place-a", "yaw_deg":0, "x_mm":60, "y_mm":0},
    object_profile_id="wood-cube-25mm-r001",
    exclusion_geometry_digest="sha256:" + "e" * 64,
)}
PLAN = {"run_id":"run", "motion_program":PROGRAM, "scene_binding":SCENE, "setup_approval":SETUP_APPROVAL}
RELEASE_PLAN = {**PLAN, "scene_binding":RELEASE_SCENE}
MOTION_APPROVAL = {"source":"HUMAN", "approval_id":"a", "approved_by":"operator", "approval_expiry":"2099-01-01T00:00:00Z", "approval_scope":"HUMAN_GATED"}


class OneJobTest(unittest.TestCase):
    def make(self, recorder_states, executor_states, *, first_row_rows=1, continuous=False, release=False):
        calls = []
        scene = RELEASE_SCENE if release else SCENE
        steps = [{"phase":phase} for phase in PHASES]
        if not continuous:
            steps[2]["requires_confirmation"] = "PRECONTACT_HUMAN"
            steps[3]["pause_after"] = "GRASP_VERDICT"
        steps[4]["pause_after"] = "SEMANTIC_VERDICT"
        normalized = {**PROGRAM, "robot_system_id":"fr5-lab-a", "resolved_job_digest":RESOLVED, "binding_digests":BINDINGS, "execution_timeouts_s":{"heartbeat_lease":1,"semantic_verdict":30}, "gripper_requirements":{"command_position_m":.01,"acceptable_feedback_m":{"min":.01,"max":.012}}, "steps":steps}
        planned = {"schema_version":"fr5.pickup_plan.v3", "run_id":"run", "scene_binding":scene, "motion_program_digest":canonical_digest(normalized), "resolved_job_digest":RESOLVED, "binding_digests":BINDINGS, "steps":[{"phase":phase, "start_joint_state":[0]*6, "final_joint_state":[0]*6} for phase in PHASES]}
        plan_digest = canonical_digest(planned)
        readback = {"schema_version":"data_factory.planning_scene_readback.v1", "run_id":"run", "plan_digest":plan_digest, "expected_planning_scene_digest":BINDINGS["planning_scene_digest"], "objects":[]}
        collision = {"schema_version":"data_factory.collision_report.v1", "plan_digest":plan_digest, "sample_count":0, "samples":[], "failure_count":0, "all_valid":True}
        no_motion = {"schema_version":"data_factory.plan_only_no_motion.v1", "run_id":"run", "plan_digest":plan_digest, "before_snapshot":{}, "after_snapshot":{}, "max_joint_delta_rad":0., "gripper_delta_m":0., "execute_goal_count":0, "gripper_goal_count":0}
        safety = {"schema_version":"data_factory.precommit_safety.v1", "run_id":"run", "approved_plan_digest":plan_digest, "scene_binding_digest":canonical_digest(scene), "expected_planning_scene_digest":BINDINGS["planning_scene_digest"], "planning_scene_readback_digest":canonical_digest(readback), "collision_report_digest":canonical_digest(collision), "plan_only_no_motion_digest":canonical_digest(no_motion), "post_reset_safe_snapshot_digest":None, "status":"PENDING"}
        evidence = {"schema_version":"data_factory.precommit_evidence.v1", "run_id":"run", "approved_plan_digest":plan_digest, "scene_binding_digest":canonical_digest(scene), "expected_planning_scene_digest":BINDINGS["planning_scene_digest"], "planning_scene_readback":readback, "collision_report":collision, "plan_only_no_motion":no_motion}
        envelope = {"plan":planned, "precommit_safety":safety, "precommit_evidence":evidence, "operator_summary":{}}
        first_status_after_begin = False
        def recorder(request):
            nonlocal first_status_after_begin
            calls.append(("recorder", request["op"]))
            if request["op"] == "begin":
                first_status_after_begin = True
            if request["op"] == "status" and first_status_after_begin:
                first_status_after_begin = False
                item = "RECORDING"
                metrics = {"rows":first_row_rows}
            else:
                item = recorder_states.pop(0)
                metrics = {"rows":1}
            if isinstance(item, dict): return item
            rejected = item == "QUALITY_REJECTED"
            return {"schema_version":"data_factory.recorder_response.v1", "op_id":request["op_id"], "op":request["op"], "ok":not rejected, "state":"FROZEN" if rejected else item, "reason_code":item, "run_id":"run", "transaction_id":"tx", "episode_index":0, "metrics":metrics, "artifacts":{}, "detail":"", "writer_alive":True, "writer_error":None}
        recorder.preserve = lambda: calls.append(("recorder", "preserve"))
        def executor(request):
            calls.append(("executor", request["op"])); state = executor_states.pop(0)
            if isinstance(state, dict): return state
            if state == "COMPLETED": calls.append(("executor_completed",))
            data = envelope if state == "PLANNED" else {"durable_blocked":True} if state == "BLOCKED" else {"gripper_feedback_m":.011,"gripper_reference_m":.01} if state == "GRASP_VERDICT" else {"gripper_feedback_m":.011,"gripper_reference_m":.01,"post_lift_gripper_feedback_m":.011} if state == "SEMANTIC_VERDICT" else {"precommit_safety":{**safety, "post_reset_safe_snapshot_digest":"sha256:" + "4" * 64, "status":"PASS"}} if state == "COMPLETED" else None
            return {"schema_version":"fr5.pickup_executor.response.v3", "mode":"PRE_LIVE", "op_id":request["op_id"], "op":request["op"], "ok":state != "BLOCKED", "code":state, "run_id":"run", "plan_digest":plan_digest, "state":state, "data":data}
        patcher = patch("tools.data_factory.one_job.validate_motion_program", lambda _: normalized)
        patcher.start(); self.addCleanup(patcher.stop)
        job = None
        def cell():
            return {"robot_system_id":"fr5-lab-a", "cell_ready":True, "reason_code":"HUMAN_ACKNOWLEDGED", "run_id":job.run_id, "plan_digest":job.plan_digest, "acknowledged_by":"operator"}
        job = OneJob(recorder, executor, cell)
        return job, calls

    @staticmethod
    def prepare_and_start(job):
        job.prepare(PLAN)
        job.approve(MOTION_APPROVAL)
        job.start()

    def test_happy_pass_orders_begin_before_execute_and_commits(self):
        job, calls = self.make(["RECORDING", "RECORDING", "RECORDING", "RECORDING", "RECORDING", "FROZEN", "FROZEN", "COMMITTED"], ["PLANNED", "APPROVED", "EXECUTING", "PRECONTACT_HUMAN", "PRECONTACT_HUMAN", "EXECUTING", "GRASP_VERDICT", "EXECUTING", "SEMANTIC_VERDICT", "EXECUTING", "COMPLETED"])
        self.prepare_and_start(job); job.poll(); self.assertEqual(job.poll()["state"], "PRECONTACT_HUMAN"); job.confirm("operator"); job.poll(); job.grasp_verdict("PASS", "operator"); job.poll(); job.semantic_verdict("PASS", "operator")
        self.assertEqual(job.poll()["state"], "AWAITING_CELL_READY")
        self.assertEqual(job.finish()["state"], "COMPLETE")
        self.assertLess(calls.index(("recorder", "begin")), calls.index(("executor", "execute")))
        self.assertLess(calls.index(("recorder", "freeze")), calls.index(("executor", "semantic_verdict")))

    def test_continuous_plan_needs_only_post_lift_semantic_verdict(self):
        job, calls = self.make(
            ["RECORDING", "RECORDING", "FROZEN", "FROZEN", "COMMITTED"],
            ["PLANNED", "APPROVED", "EXECUTING", "SEMANTIC_VERDICT", "EXECUTING", "COMPLETED"],
            continuous=True,
        )
        self.prepare_and_start(job)
        result = job.poll()
        self.assertEqual((result["state"], result["grasp_verdict"]), ("SEMANTIC_VERDICT", None))
        job.semantic_verdict("PASS", "operator")
        self.assertEqual(job.poll()["state"], "AWAITING_CELL_READY")
        self.assertNotIn(("executor", "confirm"), calls)
        self.assertNotIn(("executor", "grasp_verdict"), calls)
        self.assertLess(calls.index(("recorder", "freeze")), calls.index(("executor", "semantic_verdict")))

    def test_recycle_stays_frozen_until_release_transition_then_commits(self):
        job, calls = self.make(
            ["RECORDING", "RECORDING", "FROZEN", "FROZEN", "FROZEN", "COMMITTED"],
            ["PLANNED", "APPROVED", "EXECUTING", "SEMANTIC_VERDICT", "EXECUTING", "RELEASE_VERDICT", "COMPLETED", "COMPLETED"],
            continuous=True,
            release=True,
        )
        job.prepare(RELEASE_PLAN); job.approve(MOTION_APPROVAL); job.start()
        self.assertEqual(job.poll()["state"], "SEMANTIC_VERDICT")
        job.semantic_verdict("PASS", "operator")
        self.assertEqual(job.poll()["state"], "RELEASE_VERDICT")
        self.assertEqual((job.frozen_rows, job.rows_after_recycle), (1, 1))
        self.assertTrue(job.release_verdict("LANDED", "operator")["ok"])
        self.assertEqual(job.poll()["state"], "AWAITING_CELL_READY")
        self.assertEqual(job.finish()["state"], "COMPLETE")
        self.assertLess(calls.index(("executor", "release_verdict")), calls.index(("recorder", "commit")))

    def test_first_row_timeout_never_executes(self):
        job, calls = self.make(["RECORDING", "ABORTED"], ["PLANNED", "APPROVED"], first_row_rows=0)
        ticks = iter((0.0, 6.0))
        job.monotonic_clock = lambda: next(ticks)
        job.prepare(PLAN); job.approve(MOTION_APPROVAL)
        result = job.start()
        self.assertEqual((result["state"], result["code"]), ("ABORTED", "RECORDER_FIRST_ROW_TIMEOUT"))
        self.assertNotIn(("executor", "execute"), calls)
        self.assertNotIn(("executor", "cancel"), calls)
        self.assertEqual(calls.count(("recorder", "abort")), 1)
        self.assertNotIn(("recorder", "preserve"), calls)

    def test_cancel_while_waiting_for_first_row_never_executes(self):
        job, calls = self.make(["RECORDING", "ABORTED"], ["PLANNED", "APPROVED"])
        cancelled = [False]
        recorder = job.recorder_call

        def cancel_after_status(request):
            response = recorder(request)
            if request["op"] == "status":
                cancelled[0] = True
            return response

        job.recorder_call = cancel_after_status
        job.prepare(PLAN); job.approve(MOTION_APPROVAL)
        result = job.start(cancel_event=lambda: cancelled[0])
        self.assertEqual((result["state"], result["code"]), ("ABORTED", "START_CANCELLED"))
        self.assertNotIn(("executor", "execute"), calls)
        self.assertNotIn(("executor", "cancel"), calls)
        self.assertEqual(calls.count(("recorder", "abort")), 1)
        self.assertNotIn(("recorder", "preserve"), calls)

    def test_semantic_fail_waits_for_executor_and_rejections_abort_once(self):
        job, calls = self.make(["RECORDING", "RECORDING", "RECORDING", "RECORDING", "FROZEN", "FROZEN", "ABORTED"], ["PLANNED", "APPROVED", "EXECUTING", "PRECONTACT_HUMAN", "EXECUTING", "GRASP_VERDICT", "EXECUTING", "SEMANTIC_VERDICT", "EXECUTING", "COMPLETED"])
        self.prepare_and_start(job); job.poll(); job.confirm("operator"); job.poll(); job.grasp_verdict("PASS", "operator"); job.poll(); job.semantic_verdict("FAIL", "operator")
        self.assertEqual(job.poll()["state"], "ABORTED")
        self.assertLess(calls.index(("executor_completed",)), calls.index(("recorder", "abort")))
        job, calls = self.make(["RECORDING", "RECORDING", "RECORDING", "RECORDING", "FROZEN", "FROZEN", "QUALITY_REJECTED", "ABORTED"], ["PLANNED", "APPROVED", "EXECUTING", "PRECONTACT_HUMAN", "EXECUTING", "GRASP_VERDICT", "EXECUTING", "SEMANTIC_VERDICT", "EXECUTING", "COMPLETED"])
        self.prepare_and_start(job); job.poll(); job.confirm("operator"); job.poll(); job.grasp_verdict("PASS", "operator"); job.poll(); job.semantic_verdict("PASS", "operator"); self.assertEqual(job.poll()["state"], "ABORTED")
        self.assertEqual(calls.count(("recorder", "commit")), 1); self.assertEqual(calls.count(("recorder", "abort")), 1)

    def test_quarantine_and_malformed_response_fail_closed_without_commit(self):
        job, calls = self.make(["RECORDING", "RECORDING", "RECORDING", "RECORDING", "FROZEN", "FROZEN", "QUARANTINED_COMMIT"], ["PLANNED", "APPROVED", "EXECUTING", "PRECONTACT_HUMAN", "EXECUTING", "GRASP_VERDICT", "EXECUTING", "SEMANTIC_VERDICT", "EXECUTING", "COMPLETED"])
        self.prepare_and_start(job); job.poll(); job.confirm("operator"); job.poll(); job.grasp_verdict("PASS", "operator"); job.poll(); job.semantic_verdict("PASS", "operator"); self.assertEqual(job.poll()["state"], "QUARANTINED_COMMIT")
        self.assertNotIn(("recorder", "abort"), calls)
        job, calls = self.make([], [{"schema_version":"wrong"}])
        self.assertFalse(job.prepare(PLAN)["ok"])
        self.assertNotIn(("recorder", "commit"), calls)
        rejected = {"schema_version":"fr5.pickup_executor.response.v3", "mode":"PRE_LIVE", "op_id":"01-plan", "op":"plan", "ok":False, "code":"PLAN_NOT_COMPLETE", "run_id":None, "plan_digest":None, "state":"IDLE", "data":None}
        job, calls = self.make([], [rejected])
        self.assertEqual(job.prepare(PLAN)["code"], "PLAN_NOT_COMPLETE")
        drift = {"schema_version":"data_factory.recorder_response.v1", "op_id":"06-status", "op":"status", "ok":True, "state":"RECORDING", "reason_code":"OK", "run_id":"other", "transaction_id":"other", "episode_index":0, "metrics":{}, "artifacts":{}, "detail":"", "writer_alive":True, "writer_error":None}
        job, calls = self.make(["RECORDING", drift, "ABORTED"], ["PLANNED", "APPROVED", "EXECUTING", "BLOCKED"])
        self.prepare_and_start(job); result = job.poll(); self.assertEqual((result["state"], result["code"]), ("BLOCKED", "RECORDER_BINDING"))
        self.assertNotIn(("recorder", "commit"), calls)

        job, calls = self.make(["RECORDING", "RECORDING", "RECORDING", "ABORTED"], ["PLANNED", "APPROVED", "EXECUTING", "PRECONTACT_HUMAN", "EXECUTING", "GRASP_VERDICT", "BLOCKED", "BLOCKED"])
        self.prepare_and_start(job); job.poll(); job.confirm("operator"); job.poll()
        result = job.grasp_verdict("FAIL", "operator")
        self.assertEqual((result["state"], result["grasp_verdict"]), ("BLOCKED", "FAIL"))
        self.assertNotIn(("recorder", "commit"), calls)

        failed_cancel = {"schema_version":"fr5.pickup_executor.response.v3", "mode":"LIVE", "op_id":"05-cancel", "op":"cancel", "ok":False, "code":"CANCEL_UNCONFIRMED", "run_id":"run", "plan_digest":None, "state":"EXECUTING", "data":{"cancel_error":"ROS_EXEC_CANCEL_ACK_TIMEOUT"}}
        job, calls = self.make(["RECORDING", "FROZEN"], ["PLANNED", "APPROVED", "EXECUTING", failed_cancel])
        self.prepare_and_start(job); result = job.cancel()
        self.assertEqual((result["state"], result["recorder_state"]), ("BLOCKED", "FROZEN"))
        self.assertNotIn(("recorder", "abort"), calls)
        self.assertIn(("recorder", "preserve"), calls)

    def test_precommit_safety_must_stay_exact_and_pass_before_commit(self):
        job, calls = self.make([], ["PLANNED"])
        executor = job.executor_call
        def invalid_preflight(request):
            response = executor(request)
            if request["op"] == "plan":
                response["data"]["precommit_safety"]["collision_report_digest"] = "not-a-digest"
            return response
        job.executor_call = invalid_preflight
        self.assertEqual(job.prepare(PLAN)["code"], "PRECOMMIT_SAFETY_SCHEMA")
        self.assertNotIn(("recorder", "commit"), calls)
        job, calls = self.make([], ["PLANNED"])
        executor = job.executor_call
        def tampered_evidence(request):
            response = executor(request)
            if request["op"] == "plan":
                response["data"]["precommit_evidence"]["collision_report"]["all_valid"] = False
            return response
        job.executor_call = tampered_evidence
        self.assertEqual(job.prepare(PLAN)["code"], "PRECOMMIT_EVIDENCE_BINDING")
        self.assertNotIn(("recorder", "commit"), calls)
        for mutate, digest_key in (
            (lambda evidence: evidence["collision_report"].update(all_valid=False), "collision_report_digest"),
            (lambda evidence: evidence["collision_report"].update(failure_count=1), "collision_report_digest"),
            (lambda evidence: evidence["plan_only_no_motion"].update(execute_goal_count=1), "plan_only_no_motion_digest"),
        ):
            job, _ = self.make([], ["PLANNED"])
            executor = job.executor_call
            def unsafe_evidence(request):
                response = executor(request)
                if request["op"] == "plan":
                    evidence = response["data"]["precommit_evidence"]
                    mutate(evidence)
                    response["data"]["precommit_safety"][digest_key] = canonical_digest(
                        evidence["collision_report"] if digest_key == "collision_report_digest" else evidence["plan_only_no_motion"]
                    )
                return response
            job.executor_call = unsafe_evidence
            self.assertEqual(job.prepare(PLAN)["code"], "PRECOMMIT_EVIDENCE_UNSAFE")
        job, _ = self.make([], ["PLANNED"])
        prepared = job.prepare(PLAN)
        self.assertEqual(prepared["plan_envelope"]["precommit_evidence"]["collision_report"]["all_valid"], True)
        for mutation in (lambda safety: safety.update(status="FAIL"), lambda safety: safety.pop("collision_report_digest"), lambda safety: safety.update(collision_report_digest="sha256:" + "f" * 64)):
            job, calls = self.make(["RECORDING", "RECORDING", "RECORDING", "RECORDING", "FROZEN", "FROZEN", "ABORTED"], ["PLANNED", "APPROVED", "EXECUTING", "PRECONTACT_HUMAN", "EXECUTING", "GRASP_VERDICT", "EXECUTING", "SEMANTIC_VERDICT", "EXECUTING", "COMPLETED"])
            self.assertEqual(job.prepare(PLAN)["plan_envelope"]["operator_summary"], {})
            executor = job.executor_call
            def failed_precommit(request):
                response = executor(request)
                if request["op"] == "heartbeat" and response["state"] == "COMPLETED":
                    mutation(response["data"]["precommit_safety"])
                return response
            job.executor_call = failed_precommit
            job.approve(MOTION_APPROVAL); job.start(); job.poll(); job.confirm("operator"); job.poll(); job.grasp_verdict("PASS", "operator"); job.poll(); job.semantic_verdict("PASS", "operator")
            self.assertEqual(job.poll()["state"], "ABORTED")
            self.assertNotIn(("recorder", "commit"), calls)

    def test_jsonl_process_and_driver_keep_holds_alive(self):
        child = "import json,sys; [print(json.dumps(json.loads(line)),flush=True) for line in sys.stdin]"
        with JsonlProcess([sys.executable, "-u", "-c", child]) as process:
            self.assertEqual(process({"hello":"world"}), {"hello":"world"})
        process = JsonlProcess([sys.executable, "-u", "-c", child])
        with process:
            process.preserve()
        self.assertIsNone(process.process.poll())
        self.assertEqual(process.release(), 0)
        job, calls = self.make(["RECORDING", "RECORDING", "RECORDING", "RECORDING", "RECORDING", "FROZEN", "FROZEN", "COMMITTED"], ["PLANNED", "APPROVED", "EXECUTING", "PRECONTACT_HUMAN", "PRECONTACT_HUMAN", "EXECUTING", "GRASP_VERDICT", "EXECUTING", "SEMANTIC_VERDICT", "EXECUTING", "COMPLETED"])
        decision_gate = threading.Event()
        def decide(state, _):
            if state == "PRECONTACT_HUMAN":
                decision_gate.wait(1)
                return "CONFIRM"
            return {"GRASP_VERDICT":"PASS", "SEMANTIC_VERDICT":"PASS", "AWAITING_CELL_READY":"READY"}.get(state)
        def keep_alive(_):
            decision_gate.set()
            time.sleep(.001)
        result = run_one_job(job, PLAN, MOTION_APPROVAL, decide, operator_id="operator", poll_interval_s=.001, sleep=keep_alive)
        self.assertEqual(result["state"], "COMPLETE")
        self.assertGreaterEqual(calls.count(("executor", "heartbeat")), 4)
        job, calls = self.make(["RECORDING", "RECORDING", "RECORDING", "RECORDING", "FROZEN", "FROZEN", "COMMITTED"], ["PLANNED", "APPROVED", "EXECUTING", "PRECONTACT_HUMAN", "EXECUTING", "GRASP_VERDICT", "EXECUTING", "SEMANTIC_VERDICT", "EXECUTING", "COMPLETED"])
        result = run_one_job(job, PLAN, {**MOTION_APPROVAL, "approval_scope":"HIL_NUMERIC_PROXY"}, lambda state, _: {"PRECONTACT_HUMAN":"CONFIRM", "AWAITING_CELL_READY":"READY"}.get(state), operator_id="operator", poll_interval_s=.001, sleep=lambda _: None)
        self.assertEqual(result["state"], "COMPLETE")
        job, _ = self.make([], ["PLANNED"])
        with self.assertRaisesRegex(ContractError, "ONE_JOB_POLL_INTERVAL"):
            run_one_job(job, PLAN, {}, lambda *_: None, operator_id="operator", poll_interval_s=.5)

        heartbeats = []
        def slow_recorder(request):
            time.sleep(.12)
            return {"schema_version":"data_factory.recorder_response.v1", "op_id":request["op_id"], "op":"freeze", "ok":True, "state":"FROZEN", "reason_code":"FROZEN", "run_id":"run", "transaction_id":"tx", "episode_index":0, "metrics":{}, "artifacts":{}, "detail":""}
        def live_executor(request):
            heartbeats.append(request["op"])
            return {"schema_version":"fr5.pickup_executor.response.v3", "mode":"LIVE", "op_id":request["op_id"], "op":"heartbeat", "ok":True, "code":"SEMANTIC_VERDICT", "run_id":"run", "plan_digest":"sha256:" + "d"*64, "state":"SEMANTIC_VERDICT", "data":{}}
        job = OneJob(slow_recorder, live_executor)
        job.run_id, job.plan_digest, job.lease_id = "run", "sha256:" + "d"*64, "lease"
        job.transaction_id, job.episode_index, job.recorder_state = "tx", 0, "RECORDING"
        job._program = {"execution_timeouts_s":{"heartbeat_lease":.1,"semantic_verdict":1}}
        job._freeze_recorder_with_heartbeats({"writer_alive":True, "writer_error":None})
        self.assertEqual(job.recorder_state, "FROZEN")
        self.assertGreaterEqual(heartbeats.count("heartbeat"), 2)

        calls, preserved = [], []
        def stuck_recorder(request):
            calls.append(("recorder", request["op"]))
            if request["op"] == "freeze":
                time.sleep(.2)
                state = "FROZEN"
            else:
                state = "RECORDING"
            return {"schema_version":"data_factory.recorder_response.v1", "op_id":request["op_id"], "op":request["op"], "ok":True, "state":state, "reason_code":state, "run_id":"run", "transaction_id":"tx", "episode_index":0, "metrics":{}, "artifacts":{}, "detail":"", "writer_alive":True, "writer_error":None}
        stuck_recorder.preserve = lambda: preserved.append(True)
        def blocking_executor(request):
            calls.append(("executor", request["op"]))
            state = "BLOCKED" if request["op"] == "cancel" else "SEMANTIC_VERDICT"
            return {"schema_version":"fr5.pickup_executor.response.v3", "mode":"LIVE", "op_id":request["op_id"], "op":request["op"], "ok":state != "BLOCKED", "code":state, "run_id":"run", "plan_digest":"sha256:" + "d"*64, "state":state, "data":{"durable_blocked":True} if state == "BLOCKED" else {}}
        job = OneJob(stuck_recorder, blocking_executor)
        job.run_id, job.plan_digest, job.lease_id = "run", "sha256:" + "d"*64, "lease"
        job.transaction_id, job.episode_index = "tx", 0
        job.state, job.grasp, job.recorder_state, job.executor_state = "EXECUTING", "PASS", "RECORDING", "EXECUTING"
        job._program = {"execution_timeouts_s":{"heartbeat_lease":.03,"semantic_verdict":.05}, "steps":[{"pause_after":"GRASP_VERDICT"}]}
        started = time.monotonic()
        result = job.poll()
        self.assertLess(time.monotonic() - started, .15)
        self.assertEqual((result["state"], result["code"], result["cancel_error"]), ("BLOCKED", "RECORDER_FREEZE_TIMEOUT", "RECORDER_FREEZE_TIMEOUT"))
        self.assertTrue(preserved)
        self.assertNotIn(("recorder", "abort"), calls)
        time.sleep(.2)
        self.assertEqual(job.recorder_state, "FREEZE_UNCERTAIN")
