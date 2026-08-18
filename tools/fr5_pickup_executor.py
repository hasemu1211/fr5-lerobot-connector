#!/usr/bin/env python3
"""Compile qualification-bound FR5 pickup plans without sending robot goals."""
from __future__ import annotations

import argparse
import base64
import copy
import json
import math
import sys
from datetime import datetime, timezone

try:
    from fr5_data_factory import (
        RFC3339,
        SAFE_ID,
        ContractError,
        canonical_digest,
        load_json_strict,
        validate_motion_program,
    )
except ImportError:
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
COMMAND_OPS = {"preflight", "plan", "approve", "execute", "status"}
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
    """Compile and approve one full plan; live execution is deliberately blocked."""

    def __init__(self, transport=None, clock=None):
        self.transport = transport or UnavailableTransport()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
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
            "frames": motion_program["frames"],
            "planning": motion_program["planning"],
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

    def _execute(self, payload):
        _exact(payload, {"run_id", "plan_digest"}, "EXECUTE_SCHEMA")
        run = self._bound(payload)
        if run["state"] != "APPROVED":
            raise ContractError("NOT_APPROVED")
        _future_timestamp(run["approval"]["approval_expiry"], self.clock())
        return _response(
            code="LIVE_EXECUTION_BLOCKED",
            run_id=payload["run_id"],
            plan_digest=payload["plan_digest"],
            state="APPROVED",
        )

    def _status(self, payload):
        _exact(payload, {"run_id", "plan_digest"}, "STATUS_SCHEMA")
        run = self._bound(payload)
        return _response(
            code="STATUS",
            ok=True,
            run_id=payload["run_id"],
            plan_digest=payload["plan_digest"],
            state=run["state"],
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
    for line in input_stream:
        try:
            result = executor.process(load_json_strict(line))
        except ContractError as exc:
            result = _response(code=exc.code)
        output_stream.write(
            json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        )
        output_stream.flush()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory-jsonl", action="store_true")
    args = parser.parse_args(argv)
    if not args.factory_jsonl:
        parser.error("--factory-jsonl required; PRE_LIVE only")
    run_jsonl(sys.stdin, sys.stdout, PickupExecutor())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
