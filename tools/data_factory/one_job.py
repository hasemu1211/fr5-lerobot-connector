"""Small, non-live coordinator for exactly one recorder/executor run."""
from __future__ import annotations

import copy
import json
import math
import queue
import selectors
import subprocess
import threading
import time
from datetime import datetime, timezone

from tools.fr5_data_factory import ContractError, DIGEST, RFC3339, SAFE_ID, canonical_digest, load_json_strict, validate_motion_program
from tools.data_factory.scene_state import validate_scene_binding


TEST_ONLY_READINESS_CONTRACT = {
    "schema_version": "data_factory.recorder_readiness_contract.v1",
    "deadline_s": 5.0,
    "min_durable_rows": 60,
    "target_fps": 30,
    "row_fps_min": 27.0,
    "row_fps_max": 33.0,
    "min_camera_source_fps": 28.5,
    "status_max_age_heartbeat_fraction": 0.5,
    "require_writer_alive": True,
    "max_writer_queue_drops": 0,
    "max_alignment_failures": 0,
    "require_quality_accepted": True,
}


def hil_numeric_gripper_verdict(state, execution_evidence, gripper_requirements):
    """Return the existing TEST_ONLY mechanical proxy verdict without semantic authority."""
    if state not in {"GRASP_VERDICT", "SEMANTIC_VERDICT"}:
        return "FAIL"
    try:
        feedback = float(execution_evidence[
            "gripper_feedback_m" if state == "GRASP_VERDICT" else "post_lift_gripper_feedback_m"
        ])
        if state == "GRASP_VERDICT" and abs(
            float(execution_evidence["gripper_reference_m"]) - gripper_requirements["command_position_m"]
        ) > 1e-9:
            return "FAIL"
        acceptable = gripper_requirements["acceptable_feedback_m"]
        return "PASS" if acceptable["min"] <= feedback <= acceptable["max"] else "FAIL"
    except (KeyError, TypeError, ValueError):
        return "FAIL"


class JsonlProcess:
    """One request/one response transport for the existing factory CLIs."""

    def __init__(self, command, timeout_s=10.0):
        if not isinstance(command, (list, tuple)) or not command or any(not isinstance(part, str) or not part for part in command):
            raise ContractError("JSONL_COMMAND")
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s <= 0:
            raise ContractError("JSONL_TIMEOUT")
        self.timeout_s = float(timeout_s)
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self._preserved = False
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def request(self, request, cancel_event=None, timeout_s=None):
        if self.process.poll() is not None:
            raise ContractError("JSONL_PROCESS_EXIT", str(self.process.returncode))
        try:
            self.process.stdin.write(json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, TypeError, ValueError) as exc:
            raise ContractError("JSONL_WRITE", str(exc)) from exc
        wait_s = self.timeout_s if timeout_s is None else timeout_s
        if not isinstance(wait_s, (int, float)) or isinstance(wait_s, bool) or wait_s <= 0:
            raise ContractError("JSONL_TIMEOUT")
        deadline = time.monotonic() + min(self.timeout_s, float(wait_s))
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ContractError("JSONL_REQUEST_CANCELLED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ContractError("JSONL_RESPONSE_TIMEOUT")
            if self.selector.select(min(remaining, 0.05) if cancel_event is not None else remaining):
                break
        line = self.process.stdout.readline()
        if not line:
            raise ContractError("JSONL_PROCESS_EXIT", str(self.process.poll()))
        return load_json_strict(line)

    def __call__(self, request):
        return self.request(request)

    def close(self, timeout_s=None):
        if self._preserved:
            return None
        wait_s = self.timeout_s if timeout_s is None else timeout_s
        if not isinstance(wait_s, (int, float)) or isinstance(wait_s, bool) or wait_s <= 0:
            raise ContractError("JSONL_TIMEOUT")
        wait_s = min(self.timeout_s, float(wait_s))
        if self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.close()  # EOF is the recorder/executor fail-safe signal.
            except (BrokenPipeError, OSError):
                pass
        timed_out = False
        try:
            return_code = self.process.wait(wait_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.process.terminate()
            try:
                return_code = self.process.wait(min(1.0, wait_s))
            except subprocess.TimeoutExpired:
                self.process.kill()
                return_code = self.process.wait()
        finally:
            self.selector.close()
            self.process.stdout.close()
        if timed_out:
            raise ContractError("JSONL_EXIT_TIMEOUT", str(return_code))
        return return_code

    def preserve(self):
        self._preserved = True

    def release(self):
        self._preserved = False
        return self.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class OneJob:
    """Coordinate injected command callables; this module never starts live services."""

    RECORDER_FIRST_ROW_TIMEOUT_S = 5.0

    def __init__(self, recorder_call, executor_call, cell_state_call=None, clock=None, monotonic_clock=None,
                 readiness_contract=None):
        if not callable(recorder_call) or not callable(executor_call):
            raise ContractError("ONE_JOB_CALLABLE")
        if cell_state_call is not None and not callable(cell_state_call):
            raise ContractError("ONE_JOB_CALLABLE")
        self.recorder_call, self.executor_call = recorder_call, executor_call
        self.cell_state_call = cell_state_call
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic_clock = monotonic_clock or time.monotonic
        if readiness_contract is not None and (
            not isinstance(readiness_contract, dict) or readiness_contract != TEST_ONLY_READINESS_CONTRACT
        ):
            raise ContractError("RECORDER_READINESS_CONTRACT")
        self.readiness_contract = copy.deepcopy(readiness_contract)
        self.run_id = self.plan_digest = self.lease_id = None
        self.state, self.grasp, self.semantic = "IDLE", None, None
        self.recorder_state = self.executor_state = None
        self.transaction_id = self.episode_index = None
        self.cancel_error = None
        self.dry_run_digest = None
        self.scene_binding = None
        self.approval_scope = None
        self.execution_evidence = None
        self.recorder_evidence = None
        self.readiness_evidence = None
        self.plan_envelope = None
        self.frozen_rows = self.rows_after_recycle = None
        self._sequence = 0
        self._sequence_lock = threading.Lock()

    def _op_id(self, op):
        with self._sequence_lock:
            self._sequence += 1
            return "%02d-%s" % (self._sequence, op)

    def _result(self, ok=True, code="OK", **extra):
        return {"ok": ok, "code": code, "state": self.state, "run_id": self.run_id,
                "plan_digest": self.plan_digest, "recorder_state": self.recorder_state,
                "executor_state": self.executor_state, "grasp_verdict": self.grasp,
                "semantic_verdict": self.semantic, "cancel_error": self.cancel_error,
                "dry_run_digest": self.dry_run_digest,
                "scene_binding": copy.deepcopy(self.scene_binding),
                "approval_scope": self.approval_scope,
                "plan_envelope": copy.deepcopy(self.plan_envelope),
                "execution_evidence": copy.deepcopy(self.execution_evidence),
                "recorder_evidence": copy.deepcopy(self.recorder_evidence),
                "readiness_evidence": copy.deepcopy(self.readiness_evidence),
                "frozen_rows": self.frozen_rows,
                "rows_after_recycle": self.rows_after_recycle,
                **extra}

    def _precommit_safety(self, value, *, terminal):
        fields = {
            "schema_version", "run_id", "approved_plan_digest", "scene_binding_digest",
            "expected_planning_scene_digest", "planning_scene_readback_digest",
            "collision_report_digest", "plan_only_no_motion_digest",
            "post_reset_safe_snapshot_digest", "status",
        }
        if not isinstance(value, dict) or set(value) != fields or value.get("schema_version") != "data_factory.precommit_safety.v1":
            raise ContractError("PRECOMMIT_SAFETY_SCHEMA")
        if value["run_id"] != self.run_id or value["approved_plan_digest"] != self.plan_digest:
            raise ContractError("PRECOMMIT_SAFETY_BINDING")
        if value["scene_binding_digest"] != canonical_digest(self.scene_binding):
            raise ContractError("PRECOMMIT_SAFETY_BINDING")
        if value["expected_planning_scene_digest"] != self._program["binding_digests"]["planning_scene_digest"]:
            raise ContractError("PRECOMMIT_SAFETY_BINDING")
        for key in (
            "planning_scene_readback_digest", "collision_report_digest", "plan_only_no_motion_digest",
        ):
            if not isinstance(value[key], str) or not DIGEST.fullmatch(value[key]):
                raise ContractError("PRECOMMIT_SAFETY_SCHEMA")
        if not isinstance(value["status"], str):
            raise ContractError("PRECOMMIT_SAFETY_SCHEMA")
        post_reset = value["post_reset_safe_snapshot_digest"]
        if terminal and (not isinstance(post_reset, str) or not DIGEST.fullmatch(post_reset) or value["status"] != "PASS"):
            raise ContractError("PRECOMMIT_SAFETY")
        if not terminal and (value["status"] != "PENDING" or post_reset is not None):
            raise ContractError("PRECOMMIT_SAFETY")
        return copy.deepcopy(value)

    def _precommit_evidence(self, value, safety):
        fields = {
            "schema_version", "run_id", "approved_plan_digest", "scene_binding_digest",
            "expected_planning_scene_digest", "planning_scene_readback", "collision_report",
            "plan_only_no_motion",
        }
        if not isinstance(value, dict) or set(value) != fields or value.get("schema_version") != "data_factory.precommit_evidence.v1":
            raise ContractError("PRECOMMIT_EVIDENCE_SCHEMA")
        if (
            value["run_id"] != self.run_id
            or value["approved_plan_digest"] != self.plan_digest
            or value["scene_binding_digest"] != canonical_digest(self.scene_binding)
            or value["expected_planning_scene_digest"] != self._program["binding_digests"]["planning_scene_digest"]
        ):
            raise ContractError("PRECOMMIT_EVIDENCE_BINDING")
        readback, collision, no_motion = value["planning_scene_readback"], value["collision_report"], value["plan_only_no_motion"]
        if (
            not isinstance(readback, dict)
            or set(readback) != {"schema_version", "run_id", "plan_digest", "expected_planning_scene_digest", "objects"}
            or readback.get("schema_version") != "data_factory.planning_scene_readback.v1"
            or readback.get("run_id") != self.run_id
            or readback.get("plan_digest") != self.plan_digest
            or readback.get("expected_planning_scene_digest") != self._program["binding_digests"]["planning_scene_digest"]
            or not isinstance(readback.get("objects"), list)
            or not isinstance(collision, dict)
            or set(collision) != {"schema_version", "plan_digest", "sample_count", "samples", "failure_count", "all_valid"}
            or collision.get("schema_version") != "data_factory.collision_report.v1"
            or collision.get("plan_digest") != self.plan_digest
            or not isinstance(no_motion, dict)
            or set(no_motion) != {"schema_version", "run_id", "plan_digest", "before_snapshot", "after_snapshot", "max_joint_delta_rad", "gripper_delta_m", "execute_goal_count", "gripper_goal_count"}
            or no_motion.get("schema_version") != "data_factory.plan_only_no_motion.v1"
            or no_motion.get("run_id") != self.run_id
            or no_motion.get("plan_digest") != self.plan_digest
            or canonical_digest(readback) != safety["planning_scene_readback_digest"]
            or canonical_digest(collision) != safety["collision_report_digest"]
            or canonical_digest(no_motion) != safety["plan_only_no_motion_digest"]
        ):
            raise ContractError("PRECOMMIT_EVIDENCE_BINDING")
        if (
            collision["all_valid"] is not True
            or type(collision["failure_count"]) is not int
            or collision["failure_count"] != 0
            or type(no_motion["execute_goal_count"]) is not int
            or no_motion["execute_goal_count"] != 0
            or type(no_motion["gripper_goal_count"]) is not int
            or no_motion["gripper_goal_count"] != 0
        ):
            raise ContractError("PRECOMMIT_EVIDENCE_UNSAFE")
        return copy.deepcopy(value)

    def _approval(self, value, *, resolved_job_digest, include_digest):
        fields = {"source", "approval_id", "approved_by", "approval_expiry"}
        if include_digest:
            fields.add("resolved_job_digest")
        else:
            fields.add("approval_scope")
        if not isinstance(value, dict) or set(value) != fields:
            raise ContractError("APPROVAL_SCHEMA")
        source = value.get("source")
        local_button = (
            source == "LOCAL_UI_BUTTON"
            and not include_digest
            and self.readiness_contract == TEST_ONLY_READINESS_CONTRACT
        )
        if source != "HUMAN" and not local_button:
            raise ContractError("APPROVAL_SCHEMA")
        if any(not isinstance(value[key], str) or not SAFE_ID.fullmatch(value[key]) for key in ("approval_id", "approved_by")):
            raise ContractError("APPROVAL_SCHEMA")
        if include_digest and value["resolved_job_digest"] != resolved_job_digest:
            raise ContractError("APPROVAL_BINDING")
        if not include_digest and value["approval_scope"] not in {"HUMAN_GATED", "HIL_NUMERIC_PROXY"}:
            raise ContractError("APPROVAL_SCOPE")
        expiry = value["approval_expiry"]
        if not isinstance(expiry, str) or not RFC3339.fullmatch(expiry):
            raise ContractError("APPROVAL_EXPIRY")
        try:
            parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError("APPROVAL_EXPIRY") from exc
        if parsed <= self.clock():
            raise ContractError("APPROVAL_EXPIRED")
        return value

    def _request(self, target, op, payload=None, transaction=None, allowed_failure=False, update_state=True):
        request = {"schema_version": "data_factory.recorder_command.v1", "op_id": self._op_id(op), "op": op}
        if target == "executor":
            request = {"schema_version": "fr5.pickup_executor.command.v4", "op_id": request["op_id"], "op": op, "payload": payload}
        elif transaction is not None:
            request["transaction"] = transaction
        caller = self.executor_call if target == "executor" else self.recorder_call
        transported_status = target == "recorder" and op == "status" and callable(getattr(caller, "request", None))
        try:
            if transported_status:
                response = caller.request(request, timeout_s=self._program["execution_timeouts_s"]["heartbeat_lease"] / 2)
            else:
                response = caller(request)
        except ContractError as exc:
            if target == "recorder" and op == "status" and exc.code == "JSONL_RESPONSE_TIMEOUT":
                self.recorder_state = "STATUS_UNCERTAIN"
                preserve = getattr(self.recorder_call, "preserve", None)
                if self.lease_id is not None and callable(preserve):
                    preserve()
                raise ContractError("RECORDER_STATUS_TIMEOUT") from exc
            raise ContractError("%s_CALL_FAILED" % target.upper(), str(exc)) from exc
        except Exception as exc:
            raise ContractError("%s_CALL_FAILED" % target.upper(), str(exc)) from exc
        fields = ({"schema_version", "op_id", "op", "ok", "state", "reason_code", "run_id", "transaction_id", "episode_index", "metrics", "artifacts", "detail"}
                  if target == "recorder" else
                  {"schema_version", "mode", "op_id", "op", "ok", "code", "run_id", "plan_digest", "state", "data"})
        schema = "data_factory.recorder_response.v1" if target == "recorder" else "fr5.pickup_executor.response.v3"
        allowed = fields | ({"writer_alive", "writer_error", "quality", "abort_reason_code"} if target == "recorder" else set())
        if (not isinstance(response, dict) or not fields <= set(response) <= allowed or response.get("schema_version") != schema
                or response.get("op_id") != request["op_id"] or response.get("op") != op
                or type(response.get("ok")) is not bool or not isinstance(response.get("state"), str)
                or not isinstance(response.get("reason_code" if target == "recorder" else "code"), str)):
            raise ContractError("%s_RESPONSE" % target.upper())
        if target == "recorder":
            self.recorder_evidence = copy.deepcopy(response)
            if (
                response["run_id"] is not None and not isinstance(response["run_id"], str)
                or response["transaction_id"] is not None and not isinstance(response["transaction_id"], str)
                or type(response["episode_index"]) is not int
                or not isinstance(response["metrics"], dict)
                or not isinstance(response["artifacts"], dict)
                or not isinstance(response["detail"], str)
            ):
                raise ContractError("RECORDER_RESPONSE")
            if op == "begin" and response["ok"]:
                if response["run_id"] != self.run_id or not response["transaction_id"]:
                    raise ContractError("RECORDER_BINDING")
                self.transaction_id, self.episode_index = response["transaction_id"], response["episode_index"]
            elif self.transaction_id is not None and (
                response["run_id"] != self.run_id
                or response["transaction_id"] != self.transaction_id
                or response["episode_index"] != self.episode_index
            ):
                raise ContractError("RECORDER_BINDING")
            elif self.transaction_id is None and response["run_id"] not in {None, self.run_id}:
                raise ContractError("RECORDER_BINDING")
            if update_state:
                self.recorder_state = response["state"]
            if transported_status:
                metrics = response["metrics"]
                required = {"rows", "writer_queue", "writer_queue_drops", "alignment_failures", "observed_monotonic_ns"}
                if not required <= set(metrics) or any(type(metrics[key]) is not int or metrics[key] < 0 for key in required):
                    raise ContractError("RECORDER_HEALTH_SCHEMA")
                age_ns = time.monotonic_ns() - metrics["observed_monotonic_ns"]
                if age_ns < 0 or age_ns >= self._program["execution_timeouts_s"]["heartbeat_lease"] * 500_000_000:
                    raise ContractError("RECORDER_HEALTH_STALE")
        else:
            if not isinstance(response["mode"], str) or not isinstance(response["code"], str):
                raise ContractError("EXECUTOR_RESPONSE")
            expected_run = payload.get("run_id") if isinstance(payload, dict) else None
            if expected_run is not None and response["run_id"] not in ({expected_run} if response["ok"] else {None, expected_run}):
                raise ContractError("EXECUTOR_BINDING")
            if self.plan_digest is not None and response["plan_digest"] not in ({self.plan_digest} if response["ok"] else {None, self.plan_digest}):
                raise ContractError("EXECUTOR_BINDING")
            self.executor_state = response["state"]
            if op != "plan" and isinstance(response.get("data"), dict):
                self.execution_evidence = copy.deepcopy(response["data"])
        if not response["ok"] and not allowed_failure:
            raise ContractError(response.get("reason_code" if target == "recorder" else "code") or "%s_RESPONSE" % target.upper())
        return response

    def _abort(self, code):
        if self.recorder_state == "QUARANTINED_COMMIT":
            self.state = "QUARANTINED_COMMIT"
            return self._result(False, code)
        if self.lease_id and self.executor_state != "COMPLETED":
            try:
                cancel = self._request("executor", "cancel", {"run_id": self.run_id, "plan_digest": self.plan_digest, "lease_id": self.lease_id}, allowed_failure=True)
                data = cancel.get("data") if isinstance(cancel.get("data"), dict) else {}
                if cancel["state"] not in {"BLOCKED", "COMPLETED"} or cancel["state"] == "BLOCKED" and data.get("durable_blocked") is not True:
                    self.cancel_error = data.get("cancel_error") or "CANCEL_UNCONFIRMED"
                elif data.get("cancel_error"):
                    self.cancel_error = data["cancel_error"]
            except ContractError as exc:
                self.cancel_error = exc.code
        if self.cancel_error:
            if self.recorder_state == "RECORDING":
                try:
                    self._request("recorder", "freeze", allowed_failure=True)
                except ContractError:
                    pass
            preserve = getattr(self.recorder_call, "preserve", None)
            if callable(preserve):
                preserve()
            self.state = "BLOCKED"
            return self._result(False, code)
        if self.recorder_state == "STATUS_UNCERTAIN" and self.lease_id is not None:
            preserve = getattr(self.recorder_call, "preserve", None)
            if callable(preserve):
                preserve()
            self.state = "BLOCKED"
            return self._result(False, code)
        try:
            response = self._request("recorder", "abort", allowed_failure=True)
        except ContractError:
            response = None
        if self.recorder_state == "QUARANTINED_COMMIT": self.state = "QUARANTINED_COMMIT"
        elif self.cancel_error or self.executor_state == "BLOCKED": self.state = "BLOCKED"
        elif response and response["ok"] and response["state"] == "ABORTED": self.state = "ABORTED"
        else: self.state = "BLOCKED"
        return self._result(False, code)

    def _prepare_plan(self, run_id, program, scene_binding, setup_approval=None):
        if self.state != "IDLE":
            return self._result(False, "ONE_JOB_ONLY")
        try:
            if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
                raise ContractError("PLAN_SCHEMA")
            program = validate_motion_program(copy.deepcopy(program))
            scene_binding = validate_scene_binding(scene_binding)
            if setup_approval is not None:
                self._approval(setup_approval, resolved_job_digest=program["resolved_job_digest"], include_digest=True)
            response = self._request("executor", "plan", {"run_id": run_id, "motion_program": program, "scene_binding": scene_binding})
            digest = response.get("plan_digest")
            envelope = response.get("data")
            if not isinstance(envelope, dict) or set(envelope) != {"plan", "precommit_safety", "precommit_evidence", "operator_summary"} or not isinstance(envelope["operator_summary"], dict):
                raise ContractError("EXECUTOR_RESPONSE")
            dry_run = envelope["plan"]
            if (
                response["state"] != "PLANNED"
                or not isinstance(digest, str)
                or not DIGEST.fullmatch(digest)
                or not isinstance(dry_run, dict)
                or canonical_digest(dry_run) != digest
                or dry_run.get("schema_version") != "fr5.pickup_plan.v3"
                or dry_run.get("run_id") != run_id
                or dry_run.get("scene_binding") != scene_binding
                or dry_run.get("motion_program_digest") != canonical_digest(program)
                or dry_run.get("resolved_job_digest") != program["resolved_job_digest"]
                or dry_run.get("binding_digests") != program["binding_digests"]
                or [step.get("phase") for step in dry_run.get("steps", [])] != [step["phase"] for step in program["steps"]]
                or dry_run["steps"][-1]["phase"] != "SAFE_POSE_PTP"
            ):
                raise ContractError("EXECUTOR_RESPONSE")
            if not isinstance(envelope["precommit_safety"], dict) or envelope["precommit_safety"].get("run_id") != run_id or envelope["precommit_safety"].get("approved_plan_digest") != digest:
                raise ContractError("PRECOMMIT_SAFETY_BINDING")
            previous_run, previous_plan, previous_program, previous_scene = self.run_id, self.plan_digest, getattr(self, "_program", None), self.scene_binding
            self.run_id, self.plan_digest, self._program, self.scene_binding = run_id, digest, program, scene_binding
            try:
                safety = self._precommit_safety(envelope["precommit_safety"], terminal=False)
                evidence = self._precommit_evidence(envelope["precommit_evidence"], safety)
            finally:
                self.run_id, self.plan_digest, self._program, self.scene_binding = previous_run, previous_plan, previous_program, previous_scene
        except ContractError as exc:
            self.state = "BLOCKED"
            return self._result(False, exc.code)
        self.run_id, self.plan_digest, self.dry_run_digest, self._program, self.scene_binding, self.plan_envelope, self.state = run_id, digest, digest, program, scene_binding, {**copy.deepcopy(envelope), "precommit_safety": safety, "precommit_evidence": evidence}, "PLANNED"
        return self._result(True, "PLANNED")

    def plan_only(self, run_id, motion_program, scene_binding):
        """Compile a non-moving plan without manufacturing a human approval."""
        return self._prepare_plan(run_id, motion_program, scene_binding)

    def prepare(self, plan):
        if not isinstance(plan, dict) or set(plan) != {"run_id", "motion_program", "scene_binding", "setup_approval"}:
            return self._result(False, "PLAN_SCHEMA")
        return self._prepare_plan(plan["run_id"], plan["motion_program"], plan["scene_binding"], plan["setup_approval"])

    def approve(self, approval):
        if self.state != "PLANNED" or not isinstance(approval, dict):
            return self._result(False, "APPROVAL_STATE")
        try:
            self._approval(approval, resolved_job_digest=self._program["resolved_job_digest"], include_digest=False)
            payload = {key: approval[key] for key in ("approval_id", "approved_by", "approval_expiry")}
            payload.update(run_id=self.run_id, plan_digest=self.plan_digest,
                           resolved_job_digest=self._program["resolved_job_digest"], approval_scope=approval["approval_scope"])
            response = self._request("executor", "approve", payload)
            if response["state"] != "APPROVED":
                raise ContractError("APPROVAL_STATE")
        except (ContractError, KeyError) as exc:
            return self._result(False, exc.code if isinstance(exc, ContractError) else "APPROVAL_SCHEMA")
        self.approval_scope, self.state = approval["approval_scope"], "APPROVED"
        return self._result(True, "APPROVED")

    @staticmethod
    def _cancel_requested(cancel):
        if cancel is None:
            return False
        check = getattr(cancel, "is_set", cancel)
        if not callable(check):
            raise ContractError("START_CANCEL")
        try:
            requested = check()
        except Exception as exc:
            raise ContractError("START_CANCEL") from exc
        if type(requested) is not bool:
            raise ContractError("START_CANCEL")
        return requested

    def _wait_for_first_recorder_row(self, cancel=None):
        """Do not arm robot motion until the recorder has durably written a hold row."""
        deadline = self.monotonic_clock() + (
            self.readiness_contract["deadline_s"]
            if self.readiness_contract is not None else self.RECORDER_FIRST_ROW_TIMEOUT_S
        )
        while True:
            if self._cancel_requested(cancel):
                raise ContractError("START_CANCELLED")
            status_started_ns = time.monotonic_ns()
            status = self._request("recorder", "status")
            status_received_ns = time.monotonic_ns()
            if self._cancel_requested(cancel):
                raise ContractError("START_CANCELLED")
            if status["state"] != "RECORDING":
                raise ContractError("RECORDER_STATE")
            if status.get("writer_alive") is not True or status.get("writer_error") is not None:
                raise ContractError("RECORDER_WRITER_FAULT")
            rows = status["metrics"].get("rows")
            if type(rows) is not int or rows < 0:
                raise ContractError("RECORDER_HEALTH_SCHEMA")
            if self.readiness_contract is None and rows >= 1:
                return status
            if self.readiness_contract is not None:
                evidence = self._test_only_readiness(status, status_started_ns, status_received_ns)
                if evidence is not None:
                    self.readiness_evidence = evidence
                    return status
            if self.monotonic_clock() >= deadline:
                raise ContractError(
                    "RECORDER_READINESS_TIMEOUT" if self.readiness_contract is not None
                    else "RECORDER_FIRST_ROW_TIMEOUT"
                )
            time.sleep(0.01)

    def _test_only_readiness(self, status, started_ns, received_ns):
        contract = self.readiness_contract
        metrics = status["metrics"]
        quality = metrics.get("quality_snapshot")
        observed_ns = metrics.get("observed_monotonic_ns")
        required_quality = {
            "accepted", "reasons", "frames", "target_fps", "effective_fps", "cameras",
            "writer_queue_drops", "alignment_failures", "image_quality_warnings",
        }
        if (
            not isinstance(quality, dict)
            or not required_quality <= set(quality)
            or type(observed_ns) is not int
            or not isinstance(quality["reasons"], list)
            or any(not isinstance(reason, str) for reason in quality["reasons"])
            or not isinstance(quality["image_quality_warnings"], list)
            or any(not isinstance(warning, str) for warning in quality["image_quality_warnings"])
            or type(quality["accepted"]) is not bool
        ):
            raise ContractError("RECORDER_READINESS_SCHEMA")
        max_age_ns = int(
            self._program["execution_timeouts_s"]["heartbeat_lease"]
            * contract["status_max_age_heartbeat_fraction"] * 1_000_000_000
        )
        age_ns = received_ns - observed_ns
        if age_ns < 0 or age_ns >= max_age_ns or received_ns - started_ns >= max_age_ns:
            raise ContractError("RECORDER_READINESS_STALE")
        drops, failures = metrics.get("writer_queue_drops"), metrics.get("alignment_failures")
        if type(drops) is not int or type(failures) is not int:
            raise ContractError("RECORDER_READINESS_SCHEMA")
        if drops != contract["max_writer_queue_drops"]:
            raise ContractError("RECORDER_READINESS_DROPS")
        if failures != contract["max_alignment_failures"]:
            raise ContractError("RECORDER_READINESS_ALIGNMENT")
        rows = metrics["rows"]
        if rows < contract["min_durable_rows"]:
            return None
        if (
            type(quality["frames"]) is not int
            or quality["frames"] != rows
            or type(quality["writer_queue_drops"]) is not int
            or type(quality["alignment_failures"]) is not int
            or quality["writer_queue_drops"] != drops
            or quality["alignment_failures"] != failures
            or quality["target_fps"] != contract["target_fps"]
        ):
            raise ContractError("RECORDER_READINESS_MISMATCH")
        row_fps = quality["effective_fps"]
        if (
            not isinstance(row_fps, (int, float)) or isinstance(row_fps, bool)
            or not math.isfinite(row_fps)
            or not contract["row_fps_min"] <= row_fps <= contract["row_fps_max"]
        ):
            raise ContractError("RECORDER_READINESS_ROW_FPS")
        cameras = quality["cameras"]
        if not isinstance(cameras, dict) or not cameras:
            raise ContractError("RECORDER_READINESS_SCHEMA")
        camera_fps = {}
        for name, camera in cameras.items():
            source_fps = camera.get("source_fps") if isinstance(camera, dict) else None
            if (
                not isinstance(name, str) or not name
                or not isinstance(source_fps, (int, float)) or isinstance(source_fps, bool)
                or not math.isfinite(source_fps)
            ):
                raise ContractError("RECORDER_READINESS_SCHEMA")
            if source_fps < contract["min_camera_source_fps"]:
                raise ContractError("RECORDER_READINESS_CAMERA_FPS")
            camera_fps[name] = float(source_fps)
        if contract["require_quality_accepted"] and (quality["accepted"] is not True or quality["reasons"]):
            raise ContractError("RECORDER_READINESS_QUALITY")
        return {
            "schema_version": "data_factory.recorder_readiness_evidence.v1",
            "run_id": self.run_id,
            "transaction_id": self.transaction_id,
            "episode_index": self.episode_index,
            "collection_profile_digest": self._program["binding_digests"]["collection_profile"],
            "quality_contract_digest": canonical_digest(contract),
            "observed_monotonic_ns": observed_ns,
            "metrics": {
                "durable_rows": rows,
                "effective_fps": float(row_fps),
                "camera_source_fps": camera_fps,
                "writer_alive": True,
                "writer_error": None,
                "writer_queue_drops": drops,
                "alignment_failures": failures,
                "quality_accepted": True,
                "quality_reasons": [],
                "image_quality_warnings": list(quality["image_quality_warnings"]),
            },
        }

    def start(self, lease_id="one-job-lease", cancel_event=None):
        if self.state != "APPROVED" or not isinstance(lease_id, str) or not SAFE_ID.fullmatch(lease_id):
            return self._result(False, "START_STATE")
        try:
            if self._cancel_requested(cancel_event):
                return self._result(False, "START_CANCELLED")
        except ContractError as exc:
            return self._result(False, exc.code)
        bindings = self._program["binding_digests"]
        transaction = {"run_id": self.run_id, "binding_digests": {
            "resolved_job_digest": self._program["resolved_job_digest"],
            "selected_sheet_digest": bindings["selected_sheet"], "yaw0_sheet_digest": bindings["yaw0_sheet"],
            "cell_calibration_digest": bindings["cell_calibration"], "robot_system_digest": bindings["robot_system"],
            "collection_profile_digest": bindings["collection_profile"], "object_profile_digest": bindings["object_profile"],
            "grasp_profile_digest": bindings["grasp_profile"],
        }}
        try:
            response = self._request("recorder", "begin", transaction=transaction)  # Recording precedes motion.
            if response["state"] != "RECORDING":
                raise ContractError("RECORDER_BEGIN")
            self._wait_for_first_recorder_row(cancel_event)
            if self._cancel_requested(cancel_event):
                raise ContractError("START_CANCELLED")
            self.lease_id = lease_id  # Arm only when the execute request is about to leave this process.
            response = self._request("executor", "execute", {"run_id": self.run_id, "plan_digest": self.plan_digest, "lease_id": lease_id})
            if response["state"] != "EXECUTING":
                raise ContractError("EXECUTOR_STATE")
        except ContractError as exc:
            return self._abort(exc.code)
        self.state = "EXECUTING"
        return self._result(True, "EXECUTING")

    def poll(self):
        if self.state not in {"EXECUTING", "PRECONTACT_HUMAN", "GRASP_VERDICT", "SEMANTIC_VERDICT", "RELEASE_VERDICT"}:
            return self._result(False, "POLL_STATE")
        try:
            status_started = self.monotonic_clock()
            status = self._request("recorder", "status")
            if self.monotonic_clock() - status_started >= self._program["execution_timeouts_s"]["heartbeat_lease"] / 2:
                raise ContractError("RECORDER_HEALTH_STALE")
            if status["state"] != ("FROZEN" if self.state == "SEMANTIC_VERDICT" or self.semantic is not None else "RECORDING"):
                raise ContractError("RECORDER_STATE")
            health = {key: status.get(key) for key in ("writer_alive", "writer_error")}
            if type(health["writer_alive"]) is not bool or health["writer_error"] is not None and not isinstance(health["writer_error"], str):
                raise ContractError("RECORDER_HEALTH_SCHEMA")
            if not health["writer_alive"] or health["writer_error"] is not None:
                raise ContractError("RECORDER_WRITER_FAULT")
            if status["state"] == "FROZEN":
                rows = status["metrics"].get("rows")
                if type(rows) is not int or rows < 0:
                    raise ContractError("RECORDER_HEALTH_SCHEMA")
                if self.frozen_rows is None:
                    self.frozen_rows = rows
                elif rows != self.frozen_rows:
                    raise ContractError("RECORDER_ROWS_AFTER_FREEZE")
                self.rows_after_recycle = rows
            response = self._request("executor", "heartbeat", {"run_id": self.run_id, "plan_digest": self.plan_digest, "lease_id": self.lease_id, "recorder_health": health})
            state = response.get("state")
            if state == "PRECONTACT_HUMAN":
                self.state = state
                return self._result(True, state)
            if state == "GRASP_VERDICT":
                self.state = state
                return self._result(True, state)
            if state == "SEMANTIC_VERDICT":
                if self._grasp_verdict_required() and self.grasp != "PASS":
                    raise ContractError("GRASP_GUARD")
                if self.recorder_state == "RECORDING":
                    self._freeze_recorder_with_heartbeats(health)
                if self.recorder_state != "FROZEN":
                    raise ContractError("RECORDER_FREEZE")
                self.state = state
                return self._result(True, state)
            if state == "RELEASE_VERDICT":
                self.state = state
                return self._result(True, state)
            if state == "COMPLETED":
                return self._finalize()
            if state != "EXECUTING":
                raise ContractError("EXECUTOR_STATE")
        except ContractError as exc:
            return self._abort(exc.code)
        return self._result(True, "EXECUTING")

    def _grasp_verdict_required(self):
        return any(step.get("pause_after") == "GRASP_VERDICT" for step in self._program["steps"])

    def _freeze_recorder_with_heartbeats(self, health):
        outcome = queue.Queue(maxsize=1)

        def freeze():
            try:
                outcome.put(self._request("recorder", "freeze", update_state=False))
            except Exception as exc:
                outcome.put(exc)

        worker = threading.Thread(target=freeze, daemon=True)
        worker.start()
        interval = min(0.1, self._program["execution_timeouts_s"]["heartbeat_lease"] / 3)
        deadline = time.monotonic() + self._program["execution_timeouts_s"]["semantic_verdict"]
        heartbeat_error = None
        while worker.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.recorder_state = "FREEZE_UNCERTAIN"
                self.cancel_error = "RECORDER_FREEZE_TIMEOUT"
                preserve = getattr(self.recorder_call, "preserve", None)
                if callable(preserve):
                    preserve()
                raise ContractError("RECORDER_FREEZE_TIMEOUT")
            worker.join(min(interval, remaining))
            if worker.is_alive() and heartbeat_error is None:
                try:
                    self._request("executor", "heartbeat", {
                        "run_id": self.run_id,
                        "plan_digest": self.plan_digest,
                        "lease_id": self.lease_id,
                        "recorder_health": health,
                    })
                except ContractError as exc:
                    heartbeat_error = exc
        result = outcome.get()
        if heartbeat_error is not None:
            raise heartbeat_error
        if isinstance(result, Exception):
            raise result
        self.recorder_state = result["state"]
        rows = result.get("metrics", {}).get("rows")
        if rows is not None and (type(rows) is not int or rows < 0):
            raise ContractError("RECORDER_HEALTH_SCHEMA")
        if rows is not None:
            self.frozen_rows = self.rows_after_recycle = rows
        return result

    def confirm(self, confirmed_by, source="HUMAN"):
        if self.state != "PRECONTACT_HUMAN" or not isinstance(confirmed_by, str) or not SAFE_ID.fullmatch(confirmed_by):
            return self._result(False, "CONFIRM_STATE")
        if source != "HUMAN":
            return self._result(False, "CONFIRM_SOURCE")
        try:
            response = self._request("executor", "confirm", {"run_id": self.run_id, "plan_digest": self.plan_digest, "confirmed_by": confirmed_by, "source": source})
            if response["state"] != "EXECUTING":
                raise ContractError("EXECUTOR_STATE")
        except ContractError as exc:
            return self._abort(exc.code)
        self.state = "EXECUTING"
        return self._result(True, "CONFIRMED")

    def grasp_verdict(self, verdict, decided_by, source="HUMAN"):
        if self.state != "GRASP_VERDICT" or verdict not in {"PASS", "FAIL"} or not isinstance(decided_by, str) or not SAFE_ID.fullmatch(decided_by):
            return self._result(False, "GRASP_VERDICT_STATE")
        if source not in {"HUMAN", "HIL_PROXY"} or source == "HIL_PROXY" and self.approval_scope != "HIL_NUMERIC_PROXY":
            return self._result(False, "GRASP_VERDICT_SOURCE")
        self.grasp = verdict
        try:
            response = self._request("executor", "grasp_verdict", {"run_id": self.run_id, "plan_digest": self.plan_digest, "verdict": verdict, "decided_by": decided_by, "source": source})
            if response["state"] != "EXECUTING":
                raise ContractError("EXECUTOR_STATE")
        except ContractError as exc:
            return self._abort(exc.code)
        self.state = "EXECUTING"
        return self._result(True, "GRASP_VERDICT_ACCEPTED")

    def semantic_verdict(self, verdict, decided_by, source="HUMAN"):
        if self.state != "SEMANTIC_VERDICT" or verdict not in {"PASS", "FAIL"} or not isinstance(decided_by, str) or not SAFE_ID.fullmatch(decided_by):
            return self._result(False, "VERDICT_STATE")
        if source not in {"HUMAN", "HIL_PROXY"} or source == "HIL_PROXY" and self.approval_scope != "HIL_NUMERIC_PROXY":
            return self._result(False, "VERDICT_SOURCE")
        try:
            response = self._request("executor", "semantic_verdict", {"run_id": self.run_id, "plan_digest": self.plan_digest, "verdict": verdict, "decided_by": decided_by, "source": source})
            if response["state"] != "EXECUTING":
                raise ContractError("EXECUTOR_STATE")
        except ContractError as exc:
            return self._abort(exc.code)
        self.semantic = verdict
        self.state = "EXECUTING"
        return self._result(True, "VERDICT_ACCEPTED")

    def release_verdict(self, verdict, decided_by, source="HUMAN"):
        if self.state != "RELEASE_VERDICT" or verdict not in {"LANDED", "OFF_SLOT", "UNCERTAIN"} or not isinstance(decided_by, str) or not SAFE_ID.fullmatch(decided_by):
            return self._result(False, "RELEASE_VERDICT_STATE")
        if source != "HUMAN" and not (
            source == "TEST_OPERATOR" and self.readiness_contract == TEST_ONLY_READINESS_CONTRACT
        ):
            return self._result(False, "RELEASE_VERDICT_SOURCE")
        try:
            response = self._request("executor", "release_verdict", {
                "run_id": self.run_id, "plan_digest": self.plan_digest, "verdict": verdict,
                "decided_by": decided_by, "source": source,
            }, allowed_failure=verdict != "LANDED")
        except ContractError as exc:
            return self._abort(exc.code)
        if verdict == "LANDED" and response["state"] != "COMPLETED":
            return self._abort("EXECUTOR_STATE")
        self.state = "EXECUTING" if response["state"] == "COMPLETED" else response["state"]
        return self._result(response["ok"], response["code"])

    def _finalize(self):
        if self.semantic == "FAIL":
            return self._abort("SEMANTIC_FAIL")
        if (self._grasp_verdict_required() and self.grasp != "PASS") or self.semantic != "PASS" or self.recorder_state != "FROZEN":
            return self._abort("COMMIT_GUARD")
        try:
            evidence = self.execution_evidence
            if not isinstance(evidence, dict):
                raise ContractError("PRECOMMIT_SAFETY")
            safety = self._precommit_safety(evidence.get("precommit_safety"), terminal=True)
            planned_safety = self.plan_envelope["precommit_safety"] if isinstance(self.plan_envelope, dict) else None
            if not isinstance(planned_safety, dict) or any(
                safety[key] != planned_safety[key] for key in (
                    "approved_plan_digest", "scene_binding_digest", "expected_planning_scene_digest",
                    "planning_scene_readback_digest", "collision_report_digest", "plan_only_no_motion_digest",
                )
            ):
                raise ContractError("PRECOMMIT_SAFETY_BINDING")
            response = self._request("recorder", "commit", allowed_failure=True)
            if response.get("state") == "QUARANTINED_COMMIT":
                self.recorder_state = "QUARANTINED_COMMIT"
                self.state = "QUARANTINED_COMMIT"
                return self._result(False, "QUARANTINED_COMMIT")
            if not response["ok"]:
                return self._abort(response["reason_code"] or "RECORDER_COMMIT")
            if response["state"] != "COMMITTED":
                return self._abort("RECORDER_COMMIT")
        except ContractError as exc:
            if self.recorder_state == "FROZEN":
                return self._abort(exc.code)  # QUALITY_REJECTED remains frozen until explicitly aborted.
            self.state = "QUARANTINED_COMMIT"
            return self._result(False, exc.code)
        self.state = "AWAITING_CELL_READY"
        return self._result(True, "COMMITTED")

    def finish(self):
        if self.state != "AWAITING_CELL_READY":
            return self._result(False, "CELL_READY_STATE")
        if self.cell_state_call is None:
            return self._result(False, "CELL_READY_REQUIRED")
        try:
            value = self.cell_state_call()
        except Exception as exc:
            return self._result(False, "CELL_STATE_CALL_FAILED", detail=str(exc))
        reason_code = value.get("reason_code") if isinstance(value, dict) else None
        reason_allowed = reason_code == "HUMAN_ACKNOWLEDGED" or (
            reason_code == "TEST_OPERATOR_ACKNOWLEDGED"
            and self.readiness_contract == TEST_ONLY_READINESS_CONTRACT
        )
        if (
            not isinstance(value, dict)
            or value.get("robot_system_id") != self._program["robot_system_id"]
            or value.get("cell_ready") is not True
            or not reason_allowed
            or value.get("run_id") != self.run_id
            or value.get("plan_digest") != self.plan_digest
            or not isinstance(value.get("acknowledged_by"), str)
            or not SAFE_ID.fullmatch(value["acknowledged_by"])
        ):
            return self._result(False, "CELL_READY_REQUIRED")
        self.state = "COMPLETE"
        return self._result(True, "COMPLETE")

    def cancel(self):
        if self.state in {"AWAITING_CELL_READY", "COMPLETE", "ABORTED", "QUARANTINED_COMMIT", "IDLE"}:
            return self._result(False, "CANCEL_STATE")
        return self._abort("CANCELLED_BY_OPERATOR")


def run_one_job(job, plan, motion_approval, decision_call, *, operator_id, lease_id="one-job-lease", poll_interval_s=0.1, sleep=time.sleep):
    """Drive one episode while continuing heartbeats during human/agent holds."""
    if not callable(decision_call) or not isinstance(operator_id, str) or not SAFE_ID.fullmatch(operator_id):
        raise ContractError("ONE_JOB_DECISION")
    if not isinstance(poll_interval_s, (int, float)) or isinstance(poll_interval_s, bool) or poll_interval_s <= 0:
        raise ContractError("ONE_JOB_POLL_INTERVAL")
    result = job.prepare(plan)
    if not result["ok"]:
        return result
    lease = job._program["execution_timeouts_s"]["heartbeat_lease"]
    if poll_interval_s * 3 > lease:
        raise ContractError("ONE_JOB_POLL_INTERVAL")
    for action in (lambda: job.approve(motion_approval), lambda: job.start(lease_id)):
        result = action()
        if not result["ok"]:
            return result
    decisions = queue.Queue(maxsize=1)
    pending_state = None

    def await_decision(state, result):
        try:
            decisions.put((state, decision_call(state, result)))
        except Exception as exc:
            decisions.put((state, exc))

    while True:
        if job.state == "AWAITING_CELL_READY":
            if pending_state != job.state:
                pending_state = job.state
                threading.Thread(target=await_decision, args=(job.state, job._result()), daemon=True).start()
            try:
                state, decision = decisions.get_nowait()
            except queue.Empty:
                sleep(poll_interval_s)
                continue
            if isinstance(decision, Exception):
                return job._result(False, "DECISION_FAILED", detail=str(decision))
            pending_state = None
            if state == job.state and decision == "READY":
                result = job.finish()
                if result["ok"]:
                    return result
            sleep(poll_interval_s)
            continue
        result = job.poll()
        if not result["ok"] or result["state"] in {"COMPLETE", "ABORTED", "BLOCKED", "QUARANTINED_COMMIT"}:
            return result
        if result["state"] == "AWAITING_CELL_READY":
            continue
        if result["state"] not in {"PRECONTACT_HUMAN", "GRASP_VERDICT", "SEMANTIC_VERDICT", "RELEASE_VERDICT"}:
            sleep(poll_interval_s)
            continue
        if result["state"] in {"GRASP_VERDICT", "SEMANTIC_VERDICT"} and job.approval_scope == "HIL_NUMERIC_PROXY":
            decision = hil_numeric_gripper_verdict(
                result["state"], result.get("execution_evidence") or {}, job._program["gripper_requirements"]
            )
            result = (job.grasp_verdict if result["state"] == "GRASP_VERDICT" else job.semantic_verdict)(decision, operator_id, source="HIL_PROXY")
            if not result["ok"]:
                return result
            sleep(poll_interval_s)
            continue
        if pending_state != result["state"]:
            pending_state = result["state"]
            threading.Thread(target=await_decision, args=(result["state"], result), daemon=True).start()
        try:
            state, decision = decisions.get_nowait()
        except queue.Empty:
            sleep(poll_interval_s)
            continue
        if isinstance(decision, Exception):
            return job.cancel()
        pending_state = None
        if state != result["state"]:
            return job.cancel()
        if result["state"] == "PRECONTACT_HUMAN" and decision == "CONFIRM":
            result = job.confirm(operator_id)
        elif result["state"] == "GRASP_VERDICT" and decision in {"PASS", "FAIL"}:
            result = job.grasp_verdict(decision, operator_id)
        elif result["state"] == "SEMANTIC_VERDICT" and decision in {"PASS", "FAIL"}:
            result = job.semantic_verdict(decision, operator_id)
        elif result["state"] == "RELEASE_VERDICT" and decision in {"LANDED", "OFF_SLOT", "UNCERTAIN"}:
            result = job.release_verdict(decision, operator_id)
        else:
            return job.cancel()
        if not result["ok"]:
            return result
