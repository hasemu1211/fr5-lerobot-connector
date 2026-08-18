#!/usr/bin/env python3
"""Compile qualification-bound FR5 pickup plans without sending robot goals."""
from __future__ import annotations

import argparse
import base64
import copy
import json
import math
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.fr5_data_factory import (
    RFC3339,
    SAFE_ID,
    ContractError,
    canonical_digest,
    load_json_strict,
    validate_motion_program,
)


MODE = "PRE_LIVE"
PHASES = (
    "PREGRASP_PTP",
    "APPROACH_STOP_LIN",
    "FINAL_APPROACH_LIN",
    "GRIPPER_CLOSE",
    "LIFT_LIN",
    "LOWER_LIN",
    "GRIPPER_OPEN",
    "RETREAT_LIN",
    "SAFE_POSE_PTP",
)
ARM_PHASES = frozenset(PHASES) - {"GRIPPER_CLOSE", "GRIPPER_OPEN"}
JOINT_ORDER = ["j1", "j2", "j3", "j4", "j5", "j6"]
COMMAND_FIELDS = {"schema_version", "op_id", "op", "payload"}
COMMAND_OPS = {"preflight", "plan", "approve", "execute", "heartbeat", "confirm", "semantic_verdict", "cancel", "status"}
EXPECTED_GRAPH = {
    "move_action": ("/move_action", "moveit_msgs/action/MoveGroup"),
    "execute_trajectory": ("/execute_trajectory", "moveit_msgs/action/ExecuteTrajectory"),
    "gripper": (
        "/gripper_controller/follow_joint_trajectory",
        "control_msgs/action/FollowJointTrajectory",
    ),
    "joint_states": ("/joint_states", "sensor_msgs/msg/JointState"),
}


def _exact(value, fields, code):
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError(code)
    return value


def _joint_positions(value):
    if (
        not isinstance(value, list)
        or len(value) != len(JOINT_ORDER)
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            for item in value
        )
    ):
        raise ContractError("JOINTS")
    return [float(item) for item in value]


def _future_timestamp(value, now):
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        raise ContractError("APPROVAL_EXPIRY")
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("APPROVAL_EXPIRY") from exc
    if now.tzinfo is None:
        raise ContractError("NOW_TIMEZONE")
    if expiry <= now:
        raise ContractError("APPROVAL_EXPIRED")
    return expiry


def _response(
    *,
    op_id=None,
    op=None,
    code="OK",
    ok=False,
    run_id=None,
    plan_digest=None,
    state="IDLE",
    data=None,
):
    return {
        "schema_version": "fr5.pickup_executor.response.v3",
        "mode": MODE,
        "op_id": op_id,
        "op": op,
        "ok": ok,
        "code": code,
        "run_id": run_id,
        "plan_digest": plan_digest,
        "state": state,
        "data": data,
    }


class UnavailableTransport:
    def preflight(self):
        raise ContractError("OFFLINE_TRANSPORT_UNAVAILABLE")

    def plan_arm(self, *args):
        raise ContractError("OFFLINE_TRANSPORT_UNAVAILABLE")

    def build_gripper_goal(self, *args):
        raise ContractError("OFFLINE_TRANSPORT_UNAVAILABLE")


class PickupExecutor:
    """Compile and approve plans; real execution stays opt-in for tests only."""

    def __init__(self, transport=None, clock=None, monotonic_clock=None, cell_state_store=None, execution_enabled=False):
        self.transport = transport or UnavailableTransport()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic_clock = monotonic_clock or time.monotonic
        self.cell_state_store = cell_state_store
        self.execution_enabled = execution_enabled
        self.cache = {}
        self.runs = {}

    def process(self, request):
        try:
            request = _exact(request, COMMAND_FIELDS, "COMMAND_SCHEMA")
            op_id, op = request["op_id"], request["op"]
            if (
                request["schema_version"] != "fr5.pickup_executor.command.v3"
                or not isinstance(op_id, str)
                or not SAFE_ID.fullmatch(op_id)
                or op not in COMMAND_OPS
            ):
                raise ContractError("COMMAND_SCHEMA")
            request_digest = canonical_digest(request)
        except ContractError as exc:
            return _response(code=exc.code)

        previous = self.cache.get(op_id)
        if previous is not None:
            if previous[0] == request_digest:
                return copy.deepcopy(previous[1])
            return _response(op_id=op_id, op=op, code="OP_ID_CONFLICT")

        self.tick()
        try:
            result = getattr(self, f"_{op}")(request["payload"])
        except ContractError as exc:
            result = _response(code=exc.code)
        result["op_id"], result["op"] = op_id, op
        snapshot = copy.deepcopy(result)
        self.cache[op_id] = (request_digest, snapshot)
        return copy.deepcopy(snapshot)

    def _validated_preflight(self, motion_program):
        validate_motion_program(motion_program)
        facts = _exact(
            self.transport.preflight(),
            {*EXPECTED_GRAPH, "joint_order"},
            "PREFLIGHT_FACTS",
        )
        for key, (endpoint, type_name) in EXPECTED_GRAPH.items():
            expected = {"endpoint": endpoint, "type": type_name, "ready": True}
            if _exact(facts[key], set(expected), "PREFLIGHT_FACTS") != expected:
                raise ContractError("PREFLIGHT_ACTION_SURFACE_MISMATCH")
        if facts["joint_order"] != JOINT_ORDER:
            raise ContractError("PREFLIGHT_ACTION_SURFACE_MISMATCH")
        return {"action_graph_digest": canonical_digest(facts), **facts}

    def _preflight(self, payload):
        _exact(payload, {"motion_program"}, "PREFLIGHT_SCHEMA")
        facts = self._validated_preflight(payload["motion_program"])
        return _response(code="PREFLIGHT_OK", ok=True, state="PREFLIGHT", data=facts)

    def _plan(self, payload):
        _exact(payload, {"run_id", "motion_program", "initial_joint_state"}, "PLAN_SCHEMA")
        run_id = payload["run_id"]
        if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
            raise ContractError("PLAN_SCHEMA")
        if run_id in self.runs:
            raise ContractError("RUN_ID_REUSED")
        if self.runs:
            raise ContractError("ONE_JOB_ONLY")

        motion_program = validate_motion_program(copy.deepcopy(payload["motion_program"]))
        action_graph = self._validated_preflight(motion_program)
        state = _joint_positions(payload["initial_joint_state"])
        planned_steps = []
        for step in motion_program["steps"]:
            phase = step["phase"]
            if phase in ARM_PHASES:
                result = _exact(
                    self.transport.plan_arm(
                        phase,
                        step.get("target"),
                        step.get("joint_positions_rad"),
                        step["limits"],
                        motion_program["frames"],
                        motion_program["planning"],
                        state,
                    ),
                    {
                        "terminal_status",
                        "moveit_success",
                        "serialized_trajectory",
                        "final_joint_state",
                    },
                    "PLAN_RESULT",
                )
                if (
                    result["terminal_status"] != "SUCCEEDED"
                    or result["moveit_success"] is not True
                    or not isinstance(result["serialized_trajectory"], bytes)
                    or not result["serialized_trajectory"]
                ):
                    raise ContractError("PLAN_NOT_COMPLETE")
                final_state = _joint_positions(result["final_joint_state"])
                serialized = result["serialized_trajectory"]
                step_type = "ARM"
            else:
                serialized = self.transport.build_gripper_goal(
                    phase, step["gripper_position_m"], step["limits"]
                )
                if not isinstance(serialized, bytes) or not serialized:
                    raise ContractError("GRIPPER_GOAL")
                final_state = state
                step_type = "GRIPPER"

            compiled = {
                "phase": phase,
                "type": step_type,
                "trajectory_b64": base64.b64encode(serialized).decode("ascii"),
                "limits": step["limits"],
                "start_joint_state": state,
                "final_joint_state": final_state,
            }
            for key in (
                "target",
                "joint_positions_rad",
                "requires_confirmation",
                "pause_after",
            ):
                if key in step:
                    compiled[key] = step[key]
            planned_steps.append(compiled)
            state = final_state

        plan = {
            "schema_version": "fr5.pickup_plan.v1",
            "run_id": run_id,
            "motion_program_digest": canonical_digest(motion_program),
            "action_graph": action_graph,
            "resolved_job_digest": motion_program["resolved_job_digest"],
            "binding_digests": motion_program["binding_digests"],
            "robot_system_id": motion_program["robot_system_id"],
            "frames": motion_program["frames"],
            "planning": motion_program["planning"],
            "execution_timeouts_s": motion_program["execution_timeouts_s"],
            "initial_joint_state": _joint_positions(payload["initial_joint_state"]),
            "steps": planned_steps,
        }
        plan_digest = canonical_digest(plan)
        self.runs[run_id] = {
            "plan": copy.deepcopy(plan),
            "digest": plan_digest,
            "state": "PLANNED",
        }
        return _response(
            code="PLANNED",
            ok=True,
            run_id=run_id,
            plan_digest=plan_digest,
            state="PLANNED",
            data=copy.deepcopy(plan),
        )

    def _approve(self, payload):
        _exact(
            payload,
            {
                "approval_id",
                "approved_by",
                "run_id",
                "resolved_job_digest",
                "plan_digest",
                "approval_expiry",
            },
            "APPROVAL_SCHEMA",
        )
        if any(
            not isinstance(payload[key], str) or not SAFE_ID.fullmatch(payload[key])
            for key in ("approval_id", "approved_by")
        ):
            raise ContractError("APPROVAL_SCHEMA")
        run = self._bound(payload)
        if run["state"] != "PLANNED":
            raise ContractError("APPROVAL_STATE")
        if payload["resolved_job_digest"] != run["plan"]["resolved_job_digest"]:
            raise ContractError("APPROVAL_BINDING")
        _future_timestamp(payload["approval_expiry"], self.clock())
        run["state"] = "APPROVED"
        run["approval"] = dict(payload)
        return _response(
            code="APPROVED",
            ok=True,
            run_id=payload["run_id"],
            plan_digest=payload["plan_digest"],
            state="APPROVED",
        )

    def _execution_payload(self, payload, fields, code):
        _exact(payload, fields, code)
        if not isinstance(payload["run_id"], str) or not SAFE_ID.fullmatch(payload["run_id"]):
            raise ContractError(code)
        return self._bound(payload)

    def _execute(self, payload):
        run = self._execution_payload(payload, {"run_id", "plan_digest", "lease_id"}, "EXECUTE_SCHEMA")
        if not isinstance(payload["lease_id"], str) or not SAFE_ID.fullmatch(payload["lease_id"]):
            raise ContractError("EXECUTE_SCHEMA")
        if run["state"] != "APPROVED":
            raise ContractError("NOT_APPROVED")
        _future_timestamp(run["approval"]["approval_expiry"], self.clock())
        if not self.execution_enabled:
            return _response(code="LIVE_EXECUTION_BLOCKED", run_id=payload["run_id"], plan_digest=payload["plan_digest"], state="APPROVED")
        if self.cell_state_store is None:
            raise ContractError("CELL_NOT_READY")
        cell = self.cell_state_store.read()
        if cell["robot_system_id"] != run["plan"]["robot_system_id"] or not cell["cell_ready"]:
            raise ContractError("CELL_NOT_READY")
        try:
            self.cell_state_store.mark_blocked("EXECUTION_IN_PROGRESS", run["plan"]["run_id"], run["digest"])
        except Exception:
            return _response(code="CELL_STATE_ARMING_FAILED", run_id=payload["run_id"], plan_digest=payload["plan_digest"], state="APPROVED")
        run["execution"] = {"lease_id": payload["lease_id"], "lease_deadline": self.monotonic_clock() + run["plan"]["execution_timeouts_s"]["heartbeat_lease"], "step_index": 0, "semantic_verdict": None, "snapshot": None, "active": False}
        run["state"] = "EXECUTING"
        self._start_current_step(run)
        return self._execution_response(run, payload["run_id"], payload["plan_digest"], "EXECUTING")

    def _execution_response(self, run, run_id, plan_digest, success_code):
        if run["state"] == "BLOCKED":
            return _response(code=run["failure_code"], run_id=run_id, plan_digest=plan_digest, state="BLOCKED", data=self._execution_data(run))
        if run["state"] == "COMPLETED":
            return _response(code="COMPLETE", ok=True, run_id=run_id, plan_digest=plan_digest, state="COMPLETED", data=self._execution_data(run))
        return _response(code=success_code, ok=True, run_id=run_id, plan_digest=plan_digest, state=run["state"], data=self._execution_data(run))

    @staticmethod
    def _execution_data(run):
        data = {key: copy.deepcopy(run["execution"].get(key)) for key in ("step_index", "semantic_verdict", "snapshot", "snapshot_error", "cancel_error", "durable_blocked", "cell_state_error") if key in run["execution"]}
        if "failure_code" in run:
            data["failure_code"] = run["failure_code"]
        return data

    def _start_current_step(self, run):
        execution, steps = run["execution"], run["plan"]["steps"]
        if execution["step_index"] >= len(steps):
            run["state"] = "COMPLETED"
            return "COMPLETED"
        step = steps[execution["step_index"]]
        if step.get("requires_confirmation") == "PRECONTACT_HUMAN" and execution.get("confirmed_step") != execution["step_index"]:
            run["state"] = "PRECONTACT_HUMAN"
            execution["wait_deadline"] = self.monotonic_clock() + run["plan"]["execution_timeouts_s"]["precontact_confirmation"]
            return "PRECONTACT_HUMAN"
        try:
            observed = self.transport.snapshot(run["plan"]["planning"]["max_joint_state_age_s"])
            if not observed["arm_controller"]["ready"] or not observed["gripper_controller"]["ready"]:
                raise ContractError("CONTROLLER_NOT_READY")
            expected = step["start_joint_state"]
            actual = _joint_positions(observed["joint_positions"])
            tolerance = run["plan"]["planning"]["goal_tolerances"]["joint_rad"]
            if any(abs(a - b) > tolerance for a, b in zip(actual, expected)):
                raise ContractError("START_STATE_MISMATCH")
            execution["active"] = True
            self.transport.start_phase(step)
        except (ContractError, KeyError, TypeError) as exc:
            self._fault(run, exc.code if isinstance(exc, ContractError) else "SNAPSHOT_SCHEMA")
        return run["state"]

    def _fault(self, run, code):
        if run["state"] == "BLOCKED":
            return run["failure_code"]
        run["failure_code"] = code
        execution = run["execution"]
        if execution.get("active"):
            try:
                self.transport.cancel_active(run["plan"]["execution_timeouts_s"]["cancel"])
            except Exception as exc:
                execution["cancel_error"] = exc.code if isinstance(exc, ContractError) else "CANCEL_FAILED"
            execution["active"] = False
        try:
            execution["snapshot"] = self.transport.snapshot(run["plan"]["planning"]["max_joint_state_age_s"])
        except Exception as exc:
            execution["snapshot"] = None
            execution["snapshot_error"] = exc.code if isinstance(exc, ContractError) else "SNAPSHOT_FAILED"
        execution["durable_blocked"] = False
        try:
            if self.cell_state_store is not None:
                self.cell_state_store.mark_blocked(code, run["plan"]["run_id"], run["digest"])
                execution["durable_blocked"] = True
            else:
                execution["cell_state_error"] = "CELL_STATE_STORE_MISSING"
        except Exception as exc:
            execution["cell_state_error"] = exc.code if isinstance(exc, ContractError) else "CELL_STATE_WRITE_FAILED"
        run["state"] = "BLOCKED"
        return code

    def tick(self):
        for run in self.runs.values():
            if run["state"] not in {"EXECUTING", "PRECONTACT_HUMAN", "SEMANTIC_VERDICT"}:
                continue
            execution, now = run["execution"], self.monotonic_clock()
            if now > execution["lease_deadline"]:
                self._fault(run, "HEARTBEAT_TIMEOUT")
            elif run["state"] in {"PRECONTACT_HUMAN", "SEMANTIC_VERDICT"}:
                if now > execution["wait_deadline"]:
                    self._fault(run, "PRECONTACT_TIMEOUT" if run["state"] == "PRECONTACT_HUMAN" else "SEMANTIC_TIMEOUT")
            else:
                try:
                    active = self.transport.poll_active()
                except Exception as exc:
                    self._fault(run, exc.code if isinstance(exc, ContractError) else "ROS_EXEC_POLL_FAILED")
                    continue
                if active is not None:
                    execution["active"] = False
                    execution["step_index"] += 1
                    if run["plan"]["steps"][execution["step_index"] - 1].get("pause_after") == "SEMANTIC_VERDICT":
                        run["state"] = "SEMANTIC_VERDICT"
                        execution["wait_deadline"] = now + run["plan"]["execution_timeouts_s"]["semantic_verdict"]
                    else:
                        self._start_current_step(run)

    def _heartbeat(self, payload):
        run = self._execution_payload(payload, {"run_id", "plan_digest", "lease_id", "recorder_health"}, "HEARTBEAT_SCHEMA")
        health = _exact(payload["recorder_health"], {"writer_alive", "writer_error"}, "HEARTBEAT_SCHEMA")
        if not isinstance(payload["lease_id"], str) or not SAFE_ID.fullmatch(payload["lease_id"]) or payload["lease_id"] != run.get("execution", {}).get("lease_id"):
            raise ContractError("LEASE_BINDING")
        if type(health["writer_alive"]) is not bool or health["writer_error"] is not None and not isinstance(health["writer_error"], str):
            raise ContractError("HEARTBEAT_SCHEMA")
        if run["state"] in {"BLOCKED", "COMPLETED"}:
            return self._execution_response(run, payload["run_id"], payload["plan_digest"], "HEARTBEAT_OK")
        if run["state"] not in {"EXECUTING", "PRECONTACT_HUMAN", "SEMANTIC_VERDICT"}:
            raise ContractError("EXECUTION_STATE")
        if not health["writer_alive"] or health["writer_error"] is not None:
            self._fault(run, "RECORDER_WRITER_FAULT")
            return self._execution_response(run, payload["run_id"], payload["plan_digest"], "HEARTBEAT_OK")
        run["execution"]["lease_deadline"] = self.monotonic_clock() + run["plan"]["execution_timeouts_s"]["heartbeat_lease"]
        return _response(code="HEARTBEAT_OK", ok=True, run_id=payload["run_id"], plan_digest=payload["plan_digest"], state=run["state"])

    def _confirm(self, payload):
        run = self._execution_payload(payload, {"run_id", "plan_digest", "confirmed_by", "source"}, "CONFIRM_SCHEMA")
        if payload["source"] != "HUMAN" or not isinstance(payload["confirmed_by"], str) or not SAFE_ID.fullmatch(payload["confirmed_by"]):
            raise ContractError("CONFIRM_SCHEMA")
        if run["state"] != "PRECONTACT_HUMAN":
            raise ContractError("CONFIRM_STATE")
        run["execution"]["confirmed_step"] = run["execution"]["step_index"]
        run["state"] = "EXECUTING"
        self._start_current_step(run)
        return self._execution_response(run, payload["run_id"], payload["plan_digest"], "CONFIRMED")

    def _semantic_verdict(self, payload):
        run = self._execution_payload(payload, {"run_id", "plan_digest", "verdict", "decided_by", "source"}, "VERDICT_SCHEMA")
        if payload["source"] != "HUMAN" or payload["verdict"] not in {"PASS", "FAIL"} or not isinstance(payload["decided_by"], str) or not SAFE_ID.fullmatch(payload["decided_by"]):
            raise ContractError("VERDICT_SCHEMA")
        if run["state"] != "SEMANTIC_VERDICT":
            raise ContractError("VERDICT_STATE")
        run["execution"]["semantic_verdict"] = payload["verdict"]
        run["state"] = "EXECUTING"
        self._start_current_step(run)
        return self._execution_response(run, payload["run_id"], payload["plan_digest"], "VERDICT_ACCEPTED")

    def _cancel(self, payload):
        run = self._execution_payload(payload, {"run_id", "plan_digest", "lease_id"}, "CANCEL_SCHEMA")
        if not isinstance(payload["lease_id"], str) or not SAFE_ID.fullmatch(payload["lease_id"]) or payload["lease_id"] != run.get("execution", {}).get("lease_id"):
            raise ContractError("LEASE_BINDING")
        if run["state"] in {"BLOCKED", "COMPLETED"}:
            return self._execution_response(run, payload["run_id"], payload["plan_digest"], "CANCELLED_BY_OPERATOR")
        self._fault(run, "CANCELLED_BY_OPERATOR")
        return self._execution_response(run, payload["run_id"], payload["plan_digest"], "CANCELLED_BY_OPERATOR")

    def _status(self, payload):
        _exact(payload, {"run_id", "plan_digest"}, "STATUS_SCHEMA")
        run = self._bound(payload)
        data = self._execution_data(run) if "execution" in run else None
        return _response(
            code="STATUS",
            ok=True,
            run_id=payload["run_id"],
            plan_digest=payload["plan_digest"],
            state=run["state"], data=data,
        )

    def _bound(self, payload):
        try:
            run = self.runs[payload["run_id"]]
        except (KeyError, TypeError) as exc:
            raise ContractError("RUN_NOT_FOUND") from exc
        if payload["plan_digest"] != run["digest"]:
            raise ContractError("PLAN_DIGEST_MISMATCH")
        return run


def run_jsonl(input_stream, output_stream, executor):
    """Keep ticking while stdin is quiet so a lease cannot be bypassed."""
    events = queue.Queue()
    def read_lines():
        try:
            for line in input_stream:
                events.put(("line", line))
            events.put(("eof", None))
        except Exception:
            events.put(("error", None))
    threading.Thread(target=read_lines, daemon=True).start()
    def terminal(run):
        result = executor._execution_response(run, run["plan"]["run_id"], run["digest"], "TERMINAL")
        output_stream.write(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        output_stream.flush()
        return result["state"] == "COMPLETED"
    while True:
        try:
            kind, value = events.get(timeout=0.05)
        except queue.Empty:
            executor.tick()
            for run in executor.runs.values():
                if run["state"] in {"BLOCKED", "COMPLETED"}:
                    return terminal(run)
            continue
        if kind == "line":
            try:
                result = executor.process(load_json_strict(value))
            except ContractError as exc:
                result = _response(code=exc.code)
            output_stream.write(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            output_stream.flush()
            if result["state"] in {"BLOCKED", "COMPLETED"}:
                return result["state"] == "COMPLETED"
            executor.tick()
            for run in executor.runs.values():
                if run["state"] in {"BLOCKED", "COMPLETED"}:
                    return terminal(run)
            continue
        for run in executor.runs.values():
            if run["state"] in {"EXECUTING", "PRECONTACT_HUMAN", "SEMANTIC_VERDICT"}:
                executor._fault(run, "INPUT_READER_ERROR" if kind == "error" else "INPUT_EOF")
                return terminal(run)
        return not any(run["state"] == "BLOCKED" for run in executor.runs.values())


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory-jsonl", action="store_true")
    parser.add_argument("--ros-plan-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.factory_jsonl:
        parser.error("--factory-jsonl required; PRE_LIVE only")
    transport = None
    node = None
    rclpy = None
    if args.ros_plan_only:
        os.environ.setdefault("RCUTILS_LOGGING_USE_STDOUT", "0")
        try:
            import rclpy
            if __package__ in (None, ""):
                from tools.data_factory.motion.moveit_transport import RosMoveItTransport
            else:
                from .moveit_transport import RosMoveItTransport
            rclpy.init()
            node = rclpy.create_node("fr5_pickup_plan_only")
            transport = RosMoveItTransport(node)
        except (ContractError, ImportError, RuntimeError) as exc:
            print(
                json.dumps(
                    {"error": {"code": "ROS_PLAN_ONLY_UNAVAILABLE", "message": str(exc)}},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                file=sys.stderr,
            )
            if node is not None:
                node.destroy_node()
            if rclpy is not None and rclpy.ok():
                rclpy.shutdown()
            return 2
    try:
        return 0 if run_jsonl(sys.stdin, sys.stdout, PickupExecutor(transport)) else 2
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy is not None and rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
