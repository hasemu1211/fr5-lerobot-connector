import json
import sys
import threading
import time
import unittest
from unittest.mock import patch

from tools.data_factory.one_job import JsonlProcess, OneJob, run_one_job
from tools.fr5_data_factory import ContractError, canonical_digest


PROGRAM = {"schema_version": "fr5.motion_program.v2"}
PHASES = ("PREGRASP_PTP","APPROACH_STOP_LIN","FINAL_APPROACH_LIN","GRIPPER_CLOSE","LIFT_LIN","LOWER_LIN","GRIPPER_OPEN","RETREAT_LIN","SAFE_POSE_PTP")
BINDINGS = {key: "sha256:" + "c" * 64 for key in ("selected_sheet", "yaw0_sheet", "cell_calibration", "robot_system", "collection_profile", "object_profile", "grasp_profile")}
RESOLVED = "sha256:" + "b" * 64
SETUP_APPROVAL = {"source":"HUMAN", "approval_id":"setup-1", "approved_by":"operator", "approval_expiry":"2099-01-01T00:00:00Z", "resolved_job_digest":RESOLVED}
PLAN = {"run_id":"run", "motion_program":PROGRAM, "setup_approval":SETUP_APPROVAL}


class OneJobTest(unittest.TestCase):
    def make(self, recorder_states, executor_states):
        calls = []
        normalized = {**PROGRAM, "robot_system_id":"fr5-lab-a", "resolved_job_digest":RESOLVED, "binding_digests":BINDINGS, "execution_timeouts_s":{"heartbeat_lease":1}, "steps":[{"phase":phase} for phase in PHASES]}
        planned = {"schema_version":"fr5.pickup_plan.v2", "run_id":"run", "motion_program_digest":canonical_digest(normalized), "resolved_job_digest":RESOLVED, "binding_digests":BINDINGS, "steps":[{"phase":phase, "start_joint_state":[0]*6, "final_joint_state":[0]*6} for phase in PHASES]}
        plan_digest = canonical_digest(planned)
        def recorder(request):
            calls.append(("recorder", request["op"])); item = recorder_states.pop(0)
            if isinstance(item, dict): return item
            rejected = item == "QUALITY_REJECTED"
            return {"schema_version":"data_factory.recorder_response.v1", "op_id":request["op_id"], "op":request["op"], "ok":not rejected, "state":"FROZEN" if rejected else item, "reason_code":item, "run_id":"run", "transaction_id":"tx", "episode_index":0, "metrics":{}, "artifacts":{}, "detail":"", "writer_alive":True, "writer_error":None}
        recorder.preserve = lambda: calls.append(("recorder", "preserve"))
        def executor(request):
            calls.append(("executor", request["op"])); state = executor_states.pop(0)
            if isinstance(state, dict): return state
            if state == "COMPLETED": calls.append(("executor_completed",))
            return {"schema_version":"fr5.pickup_executor.response.v3", "mode":"PRE_LIVE", "op_id":request["op_id"], "op":request["op"], "ok":state != "BLOCKED", "code":state, "run_id":"run", "plan_digest":plan_digest, "state":state, "data":planned if state == "PLANNED" else {"durable_blocked":True} if state == "BLOCKED" else None}
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
        job.approve({"source":"HUMAN", "approval_id":"a", "approved_by":"operator", "approval_expiry":"2099-01-01T00:00:00Z"})
        job.start()

    def test_happy_pass_orders_begin_before_execute_and_commits(self):
        job, calls = self.make(["RECORDING", "RECORDING", "RECORDING", "RECORDING", "RECORDING", "FROZEN", "FROZEN", "COMMITTED"], ["PLANNED", "APPROVED", "EXECUTING", "PRECONTACT_HUMAN", "PRECONTACT_HUMAN", "EXECUTING", "GRASP_VERDICT", "EXECUTING", "SEMANTIC_VERDICT", "EXECUTING", "COMPLETED"])
        self.prepare_and_start(job); job.poll(); self.assertEqual(job.poll()["state"], "PRECONTACT_HUMAN"); job.confirm("operator"); job.poll(); job.grasp_verdict("PASS", "operator"); job.poll(); job.semantic_verdict("PASS", "operator")
        self.assertEqual(job.poll()["state"], "AWAITING_CELL_READY")
        self.assertEqual(job.finish()["state"], "COMPLETE")
        self.assertLess(calls.index(("recorder", "begin")), calls.index(("executor", "execute")))
        self.assertLess(calls.index(("recorder", "freeze")), calls.index(("executor", "semantic_verdict")))

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
        drift = {"schema_version":"data_factory.recorder_response.v1", "op_id":"05-status", "op":"status", "ok":True, "state":"RECORDING", "reason_code":"OK", "run_id":"other", "transaction_id":"other", "episode_index":0, "metrics":{}, "artifacts":{}, "detail":"", "writer_alive":True, "writer_error":None}
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
        result = run_one_job(job, PLAN, {"source":"HUMAN", "approval_id":"a", "approved_by":"operator", "approval_expiry":"2099-01-01T00:00:00Z"}, decide, operator_id="operator", poll_interval_s=.001, sleep=keep_alive)
        self.assertEqual(result["state"], "COMPLETE")
        self.assertGreaterEqual(calls.count(("executor", "heartbeat")), 4)
        job, _ = self.make([], ["PLANNED"])
        with self.assertRaisesRegex(ContractError, "ONE_JOB_POLL_INTERVAL"):
            run_one_job(job, PLAN, {}, lambda *_: None, operator_id="operator", poll_interval_s=.5)
