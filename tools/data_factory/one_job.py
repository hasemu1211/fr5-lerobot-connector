"""Small, non-live coordinator for exactly one recorder/executor run."""
from __future__ import annotations

import copy
import json
import queue
import selectors
import subprocess
import threading
import time
from datetime import datetime, timezone

from tools.fr5_data_factory import ContractError, DIGEST, RFC3339, SAFE_ID, canonical_digest, load_json_strict, validate_motion_program
from tools.data_factory.scene_state import validate_scene_binding


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
            self.process.stdin.close()  # EOF is the recorder/executor fail-safe signal.
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

    def __init__(self, recorder_call, executor_call, cell_state_call=None, clock=None, monotonic_clock=None):
        if not callable(recorder_call) or not callable(executor_call):
            raise ContractError("ONE_JOB_CALLABLE")
        if cell_state_call is not None and not callable(cell_state_call):
            raise ContractError("ONE_JOB_CALLABLE")
        self.recorder_call, self.executor_call = recorder_call, executor_call
        self.cell_state_call = cell_state_call
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic_clock = monotonic_clock or time.monotonic
        self.run_id = self.plan_digest = self.lease_id = None
        self.state, self.grasp, self.semantic = "IDLE", None, None
        self.recorder_state = self.executor_state = None
        self.transaction_id = self.episode_index = None
        self.cancel_error = None
        self.dry_run_digest = None
        self.scene_binding = None
        self.approval_scope = None
        self.execution_evidence = None
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
                "execution_evidence": copy.deepcopy(self.execution_evidence),
                **extra}

    def _approval(self, value, *, resolved_job_digest, include_digest):
        fields = {"source", "approval_id", "approved_by", "approval_expiry"}
        if include_digest:
            fields.add("resolved_job_digest")
        else:
            fields.add("approval_scope")
        if not isinstance(value, dict) or set(value) != fields or value.get("source") != "HUMAN":
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
                if callable(preserve):
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
        if self.recorder_state == "STATUS_UNCERTAIN":
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
            dry_run = response.get("data")
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
        except ContractError as exc:
            self.state = "BLOCKED"
            return self._result(False, exc.code)
        self.run_id, self.plan_digest, self.dry_run_digest, self._program, self.scene_binding, self.state = run_id, digest, digest, program, scene_binding, "PLANNED"
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

    def start(self, lease_id="one-job-lease"):
        if self.state != "APPROVED" or not isinstance(lease_id, str) or not SAFE_ID.fullmatch(lease_id):
            return self._result(False, "START_STATE")
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
            self.lease_id = lease_id
            response = self._request("executor", "execute", {"run_id": self.run_id, "plan_digest": self.plan_digest, "lease_id": lease_id})
            if response["state"] != "EXECUTING":
                raise ContractError("EXECUTOR_STATE")
        except ContractError as exc:
            return self._abort(exc.code)
        self.state = "EXECUTING"
        return self._result(True, "EXECUTING")

    def poll(self):
        if self.state not in {"EXECUTING", "PRECONTACT_HUMAN", "GRASP_VERDICT", "SEMANTIC_VERDICT"}:
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
            response = self._request("executor", "heartbeat", {"run_id": self.run_id, "plan_digest": self.plan_digest, "lease_id": self.lease_id, "recorder_health": health})
            state = response.get("state")
            if state == "PRECONTACT_HUMAN":
                self.state = state
                return self._result(True, state)
            if state == "GRASP_VERDICT":
                self.state = state
                return self._result(True, state)
            if state == "SEMANTIC_VERDICT":
                if self.grasp != "PASS":
                    raise ContractError("GRASP_GUARD")
                if self.recorder_state == "RECORDING":
                    self._freeze_recorder_with_heartbeats(health)
                if self.recorder_state != "FROZEN":
                    raise ContractError("RECORDER_FREEZE")
                self.state = state
                return self._result(True, state)
            if state == "COMPLETED":
                return self._finalize()
            if state != "EXECUTING":
                raise ContractError("EXECUTOR_STATE")
        except ContractError as exc:
            return self._abort(exc.code)
        return self._result(True, "EXECUTING")

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

    def _finalize(self):
        if self.semantic == "FAIL":
            return self._abort("SEMANTIC_FAIL")
        if self.grasp != "PASS" or self.semantic != "PASS" or self.recorder_state != "FROZEN":
            return self._abort("COMMIT_GUARD")
        try:
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
        if (
            not isinstance(value, dict)
            or value.get("robot_system_id") != self._program["robot_system_id"]
            or value.get("cell_ready") is not True
            or value.get("reason_code") != "HUMAN_ACKNOWLEDGED"
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

    def hil_numeric_verdict(state, result):
        evidence = result.get("execution_evidence") or {}
        required = job._program["gripper_requirements"]
        try:
            feedback = float(evidence["gripper_feedback_m" if state == "GRASP_VERDICT" else "post_lift_gripper_feedback_m"])
            if state == "GRASP_VERDICT" and abs(float(evidence["gripper_reference_m"]) - required["command_position_m"]) > 1e-9:
                return "FAIL"
        except (KeyError, TypeError, ValueError):
            return "FAIL"
        return "PASS" if required["acceptable_feedback_m"]["min"] <= feedback <= required["acceptable_feedback_m"]["max"] else "FAIL"

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
        if result["state"] not in {"PRECONTACT_HUMAN", "GRASP_VERDICT", "SEMANTIC_VERDICT"}:
            sleep(poll_interval_s)
            continue
        if result["state"] in {"GRASP_VERDICT", "SEMANTIC_VERDICT"} and job.approval_scope == "HIL_NUMERIC_PROXY":
            decision = hil_numeric_verdict(result["state"], result)
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
        else:
            return job.cancel()
        if not result["ok"]:
            return result
