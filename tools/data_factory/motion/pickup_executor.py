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
from tools.data_factory.scene_state import validate_scene_binding
from tools.data_factory.quality.phase_events import PhaseEventWriter


MODE = "PRE_LIVE"
PHASES = (
    "PREGRASP_PTP",
    "APPROACH_STOP_LIN",
    "FINAL_APPROACH_LIN",
    "GRIPPER_CLOSE",
    "LIFT_LIN",
    "RECYCLE_APPROACH_PTP",
    "LOWER_LIN",
    "GRIPPER_OPEN",
    "RETREAT_LIN",
    "SAFE_POSE_PTP",
)
ARM_PHASES = frozenset(PHASES) - {"GRIPPER_CLOSE", "GRIPPER_OPEN"}
JOINT_ORDER = ["j1", "j2", "j3", "j4", "j5", "j6"]
COMMAND_FIELDS = {"schema_version", "op_id", "op", "payload"}
COMMAND_OPS = {"preflight", "plan", "approve", "execute", "heartbeat", "confirm", "grasp_verdict", "semantic_verdict", "release_verdict", "cancel", "status"}
ACTIVE_STATES = {"EXECUTING", "PRECONTACT_HUMAN", "GRASP_VERDICT", "SEMANTIC_VERDICT", "RELEASE_VERDICT"}
RECYCLE_PHASES = ("RECYCLE_APPROACH_PTP", "LOWER_LIN", "GRIPPER_OPEN", "RETREAT_LIN", "SAFE_POSE_PTP")
EXECUTION_RESULT_MARGIN_S = 2.0
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


_PRECOMMIT_SAFETY_FIELDS = {
    "schema_version", "run_id", "approved_plan_digest", "scene_binding_digest",
    "expected_planning_scene_digest", "planning_scene_readback_digest",
    "collision_report_digest", "plan_only_no_motion_digest",
    "post_reset_safe_snapshot_digest", "status",
}
_PRECOMMIT_EVIDENCE_FIELDS = {
    "schema_version", "run_id", "approved_plan_digest", "scene_binding_digest",
    "expected_planning_scene_digest", "planning_scene_readback", "collision_report",
    "plan_only_no_motion",
}


def _precommit_evidence(value, safety, *, run_id, plan_digest, scene_binding, planning_scene_digest):
    value = _exact(value, _PRECOMMIT_EVIDENCE_FIELDS, "PRECOMMIT_EVIDENCE_SCHEMA")
    if (
        value["schema_version"] != "data_factory.precommit_evidence.v1"
        or value["run_id"] != run_id
        or value["approved_plan_digest"] != plan_digest
        or value["scene_binding_digest"] != canonical_digest(scene_binding)
        or value["expected_planning_scene_digest"] != planning_scene_digest
    ):
        raise ContractError("PRECOMMIT_EVIDENCE_BINDING")
    readback, collision, no_motion = (
        value["planning_scene_readback"], value["collision_report"], value["plan_only_no_motion"],
    )
    if (
        not isinstance(readback, dict)
        or set(readback) != {"schema_version", "run_id", "plan_digest", "expected_planning_scene_digest", "objects"}
        or readback["schema_version"] != "data_factory.planning_scene_readback.v1"
        or readback["run_id"] != run_id
        or readback["plan_digest"] != plan_digest
        or readback["expected_planning_scene_digest"] != planning_scene_digest
        or not isinstance(readback["objects"], list)
        or not isinstance(collision, dict)
        or set(collision) != {"schema_version", "plan_digest", "sample_count", "samples", "failure_count", "all_valid"}
        or collision["schema_version"] != "data_factory.collision_report.v1"
        or collision["plan_digest"] != plan_digest
        or not isinstance(no_motion, dict)
        or set(no_motion) != {"schema_version", "run_id", "plan_digest", "before_snapshot", "after_snapshot", "max_joint_delta_rad", "gripper_delta_m", "execute_goal_count", "gripper_goal_count"}
        or no_motion["schema_version"] != "data_factory.plan_only_no_motion.v1"
        or no_motion["run_id"] != run_id
        or no_motion["plan_digest"] != plan_digest
        or canonical_digest(readback) != safety["planning_scene_readback_digest"]
        or canonical_digest(collision) != safety["collision_report_digest"]
        or canonical_digest(no_motion) != safety["plan_only_no_motion_digest"]
    ):
        raise ContractError("PRECOMMIT_EVIDENCE_BINDING")
    return copy.deepcopy(value)


def _gripper_settings(value):
    required = {
        "hardware_plugin", "velocity_percent", "force_percent",
        "settle_time_ms",
    }
    optional = {"open_velocity_percent", "open_force_percent"}
    if (
        not isinstance(value, dict)
        or not required <= set(value) <= required | optional
    ):
        raise ContractError("GRIPPER_SETTINGS_UNVERIFIED")
    value = dict(value)
    value.setdefault("open_velocity_percent", value["velocity_percent"])
    value.setdefault("open_force_percent", value["force_percent"])
    if (
        value["hardware_plugin"] not in {"fairino_hardware/FairinoHardwareInterface", "mock_components/GenericSystem"}
        or any(type(value[key]) is not int or not 1 <= value[key] <= 100 for key in ("velocity_percent", "open_velocity_percent", "force_percent", "open_force_percent"))
        or type(value["settle_time_ms"]) is not int
        or not 50 <= value["settle_time_ms"] <= 10000
    ):
        raise ContractError("GRIPPER_SETTINGS_UNVERIFIED")
    return dict(value)


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
    mode=MODE,
):
    return {
        "schema_version": "fr5.pickup_executor.response.v3",
        "mode": mode,
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

    def snapshot(self, *args):
        raise ContractError("OFFLINE_TRANSPORT_UNAVAILABLE")

    def build_gripper_goal(self, *args):
        raise ContractError("OFFLINE_TRANSPORT_UNAVAILABLE")

    def precommit_safety(self, *args):
        raise ContractError("OFFLINE_TRANSPORT_UNAVAILABLE")


class PickupExecutor:
    """Compile and approve plans; real execution stays opt-in for tests only."""

    def __init__(self, transport=None, clock=None, monotonic_clock=None, cell_state_store=None, scene_state_store=None, execution_enabled=False, phase_events_root=None, event_clock=None):
        self.transport = transport or UnavailableTransport()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic_clock = monotonic_clock or time.monotonic
        self.cell_state_store = cell_state_store
        self.scene_state_store = scene_state_store
        self.execution_enabled = execution_enabled
        self.phase_events_root = Path(phase_events_root) if phase_events_root is not None else None
        self.event_clock = event_clock or (lambda: (time.time_ns(), "SYSTEM_TIME"))
        self.mode = "LIVE" if execution_enabled else MODE
        self.cache = {}
        self.runs = {}
        self._phase_event_writer = None

    def close(self):
        if self._phase_event_writer is None:
            return True
        ok = self._phase_event_writer.close()
        if not ok:
            for run in self.runs.values():
                if "execution" in run:
                    run["execution"]["behavior_report_status"] = "BEHAVIOR_REPORT_UNAVAILABLE"
        return ok

    def _emit_phase_event(self, run, event, step, action_status, evidence):
        writer = self._phase_event_writer
        if writer is None:
            return
        execution = run["execution"]
        sequence = execution["phase_event_sequence"]
        execution["phase_event_sequence"] += 1
        try:
            event_ros_time_ns, ros_clock_type = self.event_clock()
            record = {
                "schema_version": "data_factory.phase_event.v1",
                "run_id": run["plan"]["run_id"],
                "plan_digest": run["digest"],
                "sequence": sequence,
                "phase": step["phase"],
                "segment_index": None if event in {"HOLD_ENTERED", "DECISION_RECEIVED"} else 0,
                "segment_count": None if event in {"HOLD_ENTERED", "DECISION_RECEIVED"} else 1,
                "event": event,
                "event_ros_time_ns": event_ros_time_ns,
                "monotonic_time_ns": int(round(self.monotonic_clock() * 1_000_000_000)),
                "ros_clock_type": ros_clock_type,
                "event_source": "pickup_executor",
                "action_status": action_status,
                "evidence_digest": canonical_digest(evidence),
            }
            if not writer.emit(record):
                execution["behavior_report_status"] = "BEHAVIOR_REPORT_UNAVAILABLE"
        except (ContractError, KeyError, TypeError, ValueError, OverflowError):
            execution["behavior_report_status"] = "BEHAVIOR_REPORT_UNAVAILABLE"

    def process(self, request):
        try:
            request = _exact(request, COMMAND_FIELDS, "COMMAND_SCHEMA")
            op_id, op = request["op_id"], request["op"]
            if (
                request["schema_version"] != "fr5.pickup_executor.command.v4"
                or not isinstance(op_id, str)
                or not SAFE_ID.fullmatch(op_id)
                or op not in COMMAND_OPS
            ):
                raise ContractError("COMMAND_SCHEMA")
            request_digest = canonical_digest(request)
        except ContractError as exc:
            return _response(code=exc.code, mode=self.mode)

        previous = self.cache.get(op_id)
        if previous is not None:
            if previous[0] == request_digest:
                return copy.deepcopy(previous[1])
            return _response(op_id=op_id, op=op, code="OP_ID_CONFLICT", mode=self.mode)

        self.tick()
        try:
            result = getattr(self, f"_{op}")(request["payload"])
        except ContractError as exc:
            result = _response(code=exc.code)
        result["mode"] = self.mode
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
        _exact(payload, {"run_id", "motion_program", "scene_binding"}, "PLAN_SCHEMA")
        run_id = payload["run_id"]
        if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
            raise ContractError("PLAN_SCHEMA")
        if run_id in self.runs:
            raise ContractError("RUN_ID_REUSED")
        if self.runs:
            raise ContractError("ONE_JOB_ONLY")

        motion_program = validate_motion_program(copy.deepcopy(payload["motion_program"]))
        scene_binding = validate_scene_binding(payload["scene_binding"])
        action_graph = self._validated_preflight(motion_program)
        observed = self.transport.snapshot(motion_program["planning"]["max_joint_state_age_s"])
        observed = _exact(
            observed,
            {"joint_positions", "joint_state_age_s", "gripper_settings", "arm_controller", "gripper_controller"},
            "SNAPSHOT_SCHEMA",
        )
        settings = _gripper_settings(observed["gripper_settings"])
        required = motion_program["gripper_requirements"]
        if (
            settings["velocity_percent"] != required["velocity_percent"]
            or settings["open_velocity_percent"]
            != required.get("open_velocity_percent", required["velocity_percent"])
            or settings["force_percent"] != required["force_percent"]
        ):
            raise ContractError("GRIPPER_SETTINGS_MISMATCH")
        for controller in ("arm_controller", "gripper_controller"):
            fields = {"endpoint", "type", "publisher_count", "ready", "age_s", "speed_scaling"}
            if controller == "gripper_controller":
                fields |= {"reference_position_m", "feedback_position_m"}
            controller_state = _exact(
                observed[controller],
                fields,
                "SNAPSHOT_SCHEMA",
            )
            if controller_state["ready"] is not True:
                raise ContractError("CONTROLLER_NOT_READY")
        initial_state = _joint_positions(observed.get("joint_positions"))
        state = initial_state
        planned_steps = []
        for step in motion_program["steps"]:
            phase = step["phase"]
            planned_duration_s = None
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
                duration_reader = getattr(self.transport, "arm_trajectory_duration_s", None)
                if callable(duration_reader):
                    planned_duration_s = duration_reader(serialized)
                    if (
                        not isinstance(planned_duration_s, (int, float))
                        or isinstance(planned_duration_s, bool)
                        or not math.isfinite(planned_duration_s)
                        or planned_duration_s <= 0
                    ):
                        raise ContractError("PLAN_TRAJECTORY_DURATION")
                    if planned_duration_s + EXECUTION_RESULT_MARGIN_S > step["limits"]["execution_timeout_s"]:
                        raise ContractError("EXECUTION_TIMEOUT_INSUFFICIENT")
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
            if planned_duration_s is not None:
                compiled["planned_duration_s"] = float(planned_duration_s)
            for key in (
                "target",
                "joint_positions_rad",
                "gripper_position_m",
                "requires_confirmation",
                "pause_after",
            ):
                if key in step:
                    compiled[key] = step[key]
            planned_steps.append(compiled)
            state = final_state

        plan = {
            "schema_version": "fr5.pickup_plan.v3",
            "run_id": run_id,
            "scene_binding": scene_binding,
            "motion_program_digest": canonical_digest(motion_program),
            "action_graph": action_graph,
            "resolved_job_digest": motion_program["resolved_job_digest"],
            "binding_digests": motion_program["binding_digests"],
            "robot_system_id": motion_program["robot_system_id"],
            "frames": motion_program["frames"],
            "planning": motion_program["planning"],
            "execution_timeouts_s": motion_program["execution_timeouts_s"],
            "gripper_requirements": motion_program["gripper_requirements"],
            "active_gripper_settings": settings,
            "initial_joint_state": initial_state,
            "steps": planned_steps,
        }
        plan_digest = canonical_digest(plan)
        precommit_response = self.transport.precommit_safety(
            copy.deepcopy(plan), motion_program["planning_scene"], copy.deepcopy(observed)
        )
        precommit_response = _exact(
            precommit_response, {"precommit_safety", "precommit_evidence"}, "PRECOMMIT_EVIDENCE_SCHEMA"
        )
        precommit = precommit_response["precommit_safety"]
        precommit = _exact(
            precommit,
            _PRECOMMIT_SAFETY_FIELDS,
            "PRECOMMIT_SAFETY_SCHEMA",
        )
        if (
            precommit["schema_version"] != "data_factory.precommit_safety.v1"
            or precommit["run_id"] != run_id
            or precommit["approved_plan_digest"] != plan_digest
            or precommit["scene_binding_digest"] != canonical_digest(scene_binding)
            or precommit["expected_planning_scene_digest"]
                != motion_program["binding_digests"]["planning_scene_digest"]
            or precommit["post_reset_safe_snapshot_digest"] is not None
            or precommit["status"] != "PENDING"
            or any(
                not isinstance(precommit[key], str) or not precommit[key].startswith("sha256:")
                for key in (
                    "planning_scene_readback_digest", "collision_report_digest",
                    "plan_only_no_motion_digest",
                )
            )
        ):
            raise ContractError("PRECOMMIT_SAFETY_BINDING")
        evidence = _precommit_evidence(
            precommit_response["precommit_evidence"], precommit,
            run_id=run_id, plan_digest=plan_digest, scene_binding=scene_binding,
            planning_scene_digest=motion_program["binding_digests"]["planning_scene_digest"],
        )
        semantic_steps = [
            step for step in planned_steps
            if step.get("pause_after") == "SEMANTIC_VERDICT"
        ]
        if len(semantic_steps) != 1:
            raise ContractError("MOTION_PROGRAM_MARKER")
        recording_boundary = semantic_steps[0]["phase"]
        has_precontact_hold = any(
            step.get("requires_confirmation") == "PRECONTACT_HUMAN"
            for step in planned_steps
        )
        operator_summary = {
            "path": [step["phase"] for step in planned_steps],
            "flow": {
                "continuous_through": (
                    "APPROACH_STOP_LIN"
                    if has_precontact_hold else recording_boundary
                ),
                "next_human_hold": (
                    "PRECONTACT_HUMAN"
                    if has_precontact_hold
                    else (
                        "POST_LIFT_SEMANTIC"
                        if recording_boundary == "LIFT_LIN"
                        else "POST_RETREAT_SEMANTIC"
                    )
                ),
            },
            "speed": {
                "max_velocity_scaling": max(
                step["limits"]["velocity_scaling"]
                for step in planned_steps if step["type"] == "ARM"
            ),
                "max_acceleration_scaling": max(
                step["limits"]["acceleration_scaling"]
                for step in planned_steps if step["type"] == "ARM"
            ),
            },
            "clearance": {
                "status": "COLLISION_CHECKED_NO_DISTANCE",
                "collision_report_digest": precommit["collision_report_digest"],
            },
        }
        recycle_plan_digest = None
        if "release_slot" in scene_binding:
            recycle_steps = [step for step in planned_steps if step["phase"] in RECYCLE_PHASES]
            recycle_plan_digest = canonical_digest({
                "schema_version": "fr5.recycle_plan.v1",
                "scene_binding": scene_binding,
                "recording_boundary_after": recording_boundary,
                "steps": recycle_steps,
            })
            operator_summary["recycle"] = {
                "recording_boundary_after": recording_boundary,
                "path": list(RECYCLE_PHASES),
                "release_slot_id": scene_binding["release_slot"]["slot_id"],
                "release_target": copy.deepcopy(scene_binding["release_slot"]["pose"]),
                "safe_staging_joint_positions_rad": copy.deepcopy(planned_steps[-1]["final_joint_state"]),
                "plan_digest": recycle_plan_digest,
            }
        self.runs[run_id] = {
            "plan": copy.deepcopy(plan),
            "digest": plan_digest,
            "precommit_safety": copy.deepcopy(precommit),
            "precommit_evidence": copy.deepcopy(evidence),
            "recycle_plan_digest": recycle_plan_digest,
            "state": "PLANNED",
        }
        return _response(
            code="PLANNED",
            ok=True,
            run_id=run_id,
            plan_digest=plan_digest,
            state="PLANNED",
            data={
                "plan": copy.deepcopy(plan),
                "precommit_safety": copy.deepcopy(precommit),
                "precommit_evidence": copy.deepcopy(evidence),
                "operator_summary": operator_summary,
            },
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
                "approval_scope",
            },
            "APPROVAL_SCHEMA",
        )
        if any(
            not isinstance(payload[key], str) or not SAFE_ID.fullmatch(payload[key])
            for key in ("approval_id", "approved_by")
        ):
            raise ContractError("APPROVAL_SCHEMA")
        if payload["approval_scope"] not in {"HUMAN_GATED", "HIL_NUMERIC_PROXY"}:
            raise ContractError("APPROVAL_SCOPE")
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
        if run.get("precommit_safety", {}).get("status") != "PENDING":
            raise ContractError("PRECOMMIT_SAFETY_REQUIRED")
        _future_timestamp(run["approval"]["approval_expiry"], self.clock())
        if not self.execution_enabled:
            return _response(code="LIVE_EXECUTION_BLOCKED", run_id=payload["run_id"], plan_digest=payload["plan_digest"], state="APPROVED")
        try:
            observed = self.transport.snapshot(run["plan"]["planning"]["max_joint_state_age_s"])
            settings = _gripper_settings(observed["gripper_settings"])
        except ContractError as exc:
            return _response(code=exc.code, run_id=payload["run_id"], plan_digest=payload["plan_digest"], state="APPROVED")
        except (KeyError, TypeError):
            return _response(code="GRIPPER_SETTINGS_UNVERIFIED", run_id=payload["run_id"], plan_digest=payload["plan_digest"], state="APPROVED")
        if settings != run["plan"]["active_gripper_settings"] or settings["hardware_plugin"] != "fairino_hardware/FairinoHardwareInterface":
            return _response(code="GRIPPER_SETTINGS_MISMATCH", run_id=payload["run_id"], plan_digest=payload["plan_digest"], state="APPROVED")
        if self.cell_state_store is None:
            raise ContractError("CELL_NOT_READY")
        if self.scene_state_store is None:
            raise ContractError("SCENE_STATE_REQUIRED")
        try:
            cell = self.cell_state_store.read()
            if cell["robot_system_id"] != run["plan"]["robot_system_id"] or not cell["cell_ready"]:
                raise ContractError("CELL_NOT_READY")
            binding = run["plan"]["scene_binding"]
            execution_scene_digest = binding["scene_state_digest"]
            execution_scene_revision = binding["revision"]
            source_slot = binding.get("source_slot")
            if source_slot is not None:
                if source_slot["allowed_run_id"] != run["plan"]["run_id"]:
                    raise ContractError("SCENE_SLOT_NEXT_RUN")
                consumed = self.scene_state_store.consume_next_source(
                    slot_id=source_slot["slot_id"], run_id=run["plan"]["run_id"],
                    expected_scene_digest=binding["scene_state_digest"], expected_slot_digest=source_slot["slot_digest"],
                )
                execution_scene_digest = consumed["scene_state_digest"]
                execution_scene_revision = consumed["scene_state"]["revision"]
            with self.scene_state_store.locked_snapshot(execution_scene_digest) as snapshot:
                scene = snapshot["scene_state"]
                item = scene["objects"].get(binding["object_instance_id"])
                if snapshot["scene_state_digest"] != execution_scene_digest or scene["revision"] != execution_scene_revision:
                    raise ContractError("SCENE_STATE_CHANGED")
                if not isinstance(item, dict) or item.get("state") != "ON_SURFACE":
                    raise ContractError("SCENE_OBJECT_NOT_READY")
                try:
                    self.cell_state_store.mark_blocked("EXECUTION_IN_PROGRESS", run["plan"]["run_id"], run["digest"])
                except Exception as exc:
                    raise ContractError("CELL_STATE_ARMING_FAILED") from exc
                run["execution"] = {"lease_id": payload["lease_id"], "lease_deadline": self.monotonic_clock() + run["plan"]["execution_timeouts_s"]["heartbeat_lease"], "step_index": 0, "grasp_verdict": None, "semantic_verdict": None, "release_verdict": None, "snapshot": None, "active": False, "scene_object": copy.deepcopy(item), "scene_state_digest": execution_scene_digest, "scene_revision": execution_scene_revision, "terminal_phases": [], "phase_event_sequence": 0}
                if self.phase_events_root is not None:
                    path = self.phase_events_root / run["plan"]["run_id"] / "phase_events.jsonl"
                    try:
                        self._phase_event_writer = PhaseEventWriter(path)
                        run["execution"]["phase_events_path"] = str(path)
                        run["execution"]["behavior_report_status"] = "PENDING"
                    except Exception:
                        self._phase_event_writer = None
                        run["execution"]["behavior_report_status"] = "BEHAVIOR_REPORT_UNAVAILABLE"
                run["state"] = "EXECUTING"
                self._start_current_step(run)
        except ContractError as exc:
            return _response(code=exc.code, run_id=payload["run_id"], plan_digest=payload["plan_digest"], state="APPROVED")
        except Exception:
            return _response(code="CELL_STATE_ARMING_FAILED", run_id=payload["run_id"], plan_digest=payload["plan_digest"], state="APPROVED")
        return self._execution_response(run, payload["run_id"], payload["plan_digest"], "EXECUTING")

    def _execution_response(self, run, run_id, plan_digest, success_code):
        if run["state"] == "BLOCKED":
            response = _response(code=run["failure_code"], run_id=run_id, plan_digest=plan_digest, state="BLOCKED", data=self._execution_data(run))
        elif run["state"] == "COMPLETED":
            response = _response(code="COMPLETE", ok=True, run_id=run_id, plan_digest=plan_digest, state="COMPLETED", data=self._execution_data(run))
        else:
            response = _response(code=success_code, ok=True, run_id=run_id, plan_digest=plan_digest, state=run["state"], data=self._execution_data(run))
        response["mode"] = self.mode
        return response

    def _execution_data(self, run):
        execution = run["execution"]
        if self._phase_event_writer is not None:
            if self._phase_event_writer.error_code:
                execution["behavior_report_status"] = "BEHAVIOR_REPORT_UNAVAILABLE"
            elif self._phase_event_writer.ready:
                execution["behavior_report_status"] = "AVAILABLE"
        data = {key: copy.deepcopy(execution.get(key)) for key in ("step_index", "grasp_verdict", "semantic_verdict", "release_verdict", "precontact_confirmation", "grasp_decision", "semantic_decision", "release_decision", "gripper_feedback_m", "gripper_reference_m", "post_lift_gripper_feedback_m", "release_evidence", "scene_transition", "snapshot", "snapshot_error", "cancel_error", "durable_blocked", "cell_state_error", "scene_state_error", "phase_events_path", "behavior_report_status") if key in execution}
        if run.get("recycle_plan_digest") is not None:
            data["recycle_plan_digest"] = run["recycle_plan_digest"]
        data["precommit_safety"] = copy.deepcopy(run.get("precommit_safety"))
        if "failure_code" in run:
            data["failure_code"] = run["failure_code"]
        return data

    def _start_current_step(self, run):
        execution, steps = run["execution"], run["plan"]["steps"]
        if execution["step_index"] >= len(steps):
            try:
                observed = self.transport.snapshot(run["plan"]["planning"]["max_joint_state_age_s"])
                joints = _joint_positions(observed["joint_positions"])
                tolerance = run["plan"]["planning"]["goal_tolerances"]["joint_rad"]
                safe = steps[-1]["final_joint_state"]
                gripper = observed["gripper_controller"]
                open_step = next(step for step in steps if step["phase"] == "GRIPPER_OPEN")
                open_target = open_step.get("gripper_position_m")
                open_tolerance = open_step["limits"]["completion_tolerance_m"]
                settings = _gripper_settings(observed["gripper_settings"])
                if (
                    not observed["arm_controller"]["ready"]
                    or not gripper["ready"]
                    or settings != run["plan"]["active_gripper_settings"]
                    or any(abs(actual - target) > tolerance for actual, target in zip(joints, safe))
                    or not isinstance(open_target, (int, float))
                    or abs(float(gripper["feedback_position_m"]) - open_target) > open_tolerance
                    or abs(float(gripper["reference_position_m"]) - open_target) > open_tolerance
                ):
                    raise ContractError("POST_RESET_SAFE_SNAPSHOT")
                safety = run["precommit_safety"]
                safety["post_reset_safe_snapshot_digest"] = canonical_digest({
                    "joint_positions": joints,
                    "gripper_feedback_position_m": float(gripper["feedback_position_m"]),
                    "gripper_reference_position_m": float(gripper["reference_position_m"]),
                })
                safety["status"] = "PASS"
                slot = run["plan"]["scene_binding"].get("release_slot")
                if slot is None:
                    run["state"] = "COMPLETED"
                else:
                    terminals = [phase for phase in execution["terminal_phases"] if phase in RECYCLE_PHASES]
                    if terminals != list(RECYCLE_PHASES):
                        raise ContractError("RECYCLE_TERMINAL_EVIDENCE")
                    execution["release_evidence"] = {
                        "schema_version": "data_factory.recycle_release_evidence.v2",
                        "run_id": run["plan"]["run_id"],
                        "plan_digest": run["digest"],
                        "release_slot_id": slot["slot_id"],
                        "expected_scene_state_digest": execution["scene_state_digest"],
                        "expected_scene_revision": execution["scene_revision"],
                        "gripper_reference_m": float(gripper["reference_position_m"]),
                        "gripper_feedback_m": float(gripper["feedback_position_m"]),
                        "terminal_phases": terminals,
                        "post_retreat_snapshot_digest": safety["post_reset_safe_snapshot_digest"],
                        "next_start_tolerance_rad": tolerance,
                        "release_outcome": None,
                        "outcome_source": None,
                        "decided_by": None,
                        "decided_at": None,
                    }
                    run["state"] = "RELEASE_VERDICT"
                    execution["wait_deadline"] = self.monotonic_clock() + run["plan"]["execution_timeouts_s"]["semantic_verdict"]
            except (ContractError, KeyError, TypeError, ValueError):
                self._fault(run, "POST_RESET_SAFE_SNAPSHOT")
            return run["state"]
        step = steps[execution["step_index"]]
        if step.get("requires_confirmation") == "PRECONTACT_HUMAN" and execution.get("confirmed_step") != execution["step_index"]:
            run["state"] = "PRECONTACT_HUMAN"
            execution["wait_deadline"] = self.monotonic_clock() + run["plan"]["execution_timeouts_s"]["precontact_confirmation"]
            self._emit_phase_event(run, "HOLD_ENTERED", step, None, {"hold": "PRECONTACT_HUMAN", "step": step})
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
            self._emit_phase_event(run, "DISPATCH_REQUESTED", step, "REQUESTED", {"step": step})
            self.transport.start_phase(step)
            self._emit_phase_event(run, "GOAL_ACCEPTED", step, "ACCEPTED", {"accepted": True, "step": step})
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
                step = run["plan"]["steps"][execution["step_index"]]
                self._emit_phase_event(run, "ACTION_TERMINAL", step, "CANCELLED", {"failure_code": code, "step": step, "terminal_status": "CANCELLED"})
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
        try:
            item = execution.get("scene_object")
            binding = run["plan"]["scene_binding"]
            if self.scene_state_store is not None and isinstance(item, dict):
                slot = binding.get("release_slot")
                if slot is None:
                    self.scene_state_store.update_object(
                        instance_id=binding["object_instance_id"],
                        object_profile_id=item["object_profile_id"],
                        state="UNKNOWN",
                        source="ROBOT_ACTION",
                        updated_by="pickup-executor",
                        expected_revision=binding["revision"],
                    )
                else:
                    snapshot = execution.get("snapshot")
                    gripper = snapshot.get("gripper_controller") if isinstance(snapshot, dict) else None
                    evidence = {
                        "schema_version": "data_factory.recycle_release_evidence.v2",
                        "run_id": run["plan"]["run_id"],
                        "plan_digest": run["digest"],
                        "release_slot_id": slot["slot_id"],
                        "expected_scene_state_digest": execution.get("scene_state_digest", binding["scene_state_digest"]),
                        "expected_scene_revision": execution.get("scene_revision", binding["revision"]),
                        "gripper_reference_m": gripper.get("reference_position_m") if isinstance(gripper, dict) else None,
                        "gripper_feedback_m": gripper.get("feedback_position_m") if isinstance(gripper, dict) else None,
                        "terminal_phases": [phase for phase in execution["terminal_phases"] if phase in RECYCLE_PHASES],
                        "post_retreat_snapshot_digest": canonical_digest(snapshot if isinstance(snapshot, dict) else {"status": "UNAVAILABLE", "failure_code": code}),
                        "next_start_tolerance_rad": run["plan"]["planning"]["goal_tolerances"]["joint_rad"],
                        "release_outcome": "UNCERTAIN",
                        "outcome_source": "EXECUTOR_FAILURE",
                        "decided_by": "pickup-executor",
                        "decided_at": self.clock().isoformat().replace("+00:00", "Z"),
                    }
                    execution["release_evidence"] = evidence
                    execution["scene_transition"] = self.scene_state_store.transition_release(
                        instance_id=binding["object_instance_id"],
                        release_slot=slot,
                        evidence=evidence,
                        updated_by="pickup-executor",
                        expected_digest=execution.get("scene_state_digest", binding["scene_state_digest"]),
                        expected_revision=execution.get("scene_revision", binding["revision"]),
                        allowed_next_run_id=binding.get("allowed_next_run_id"),
                    )
        except Exception as exc:
            execution["scene_state_error"] = exc.code if isinstance(exc, ContractError) else "SCENE_STATE_WRITE_FAILED"
        run["state"] = "BLOCKED"
        return code

    def _verified_gripper_feedback(self, run):
        observed = self.transport.snapshot(run["plan"]["planning"]["max_joint_state_age_s"])
        controller = observed["gripper_controller"]
        feedback = float(controller["feedback_position_m"])
        reference = float(controller["reference_position_m"])
        required = run["plan"]["gripper_requirements"]
        if (
            not controller["ready"]
            or not math.isfinite(feedback)
            or not math.isfinite(reference)
            or abs(reference - required["command_position_m"]) > 1e-9
            or not required["acceptable_feedback_m"]["min"] <= feedback <= required["acceptable_feedback_m"]["max"]
        ):
            raise ContractError("GRIPPER_FEEDBACK_OUT_OF_RANGE")
        return feedback, reference

    def tick(self):
        for run in self.runs.values():
            if run["state"] not in ACTIVE_STATES:
                continue
            execution, now = run["execution"], self.monotonic_clock()
            if now >= execution["lease_deadline"]:
                self._fault(run, "HEARTBEAT_TIMEOUT")
            elif run["state"] in {"PRECONTACT_HUMAN", "GRASP_VERDICT", "SEMANTIC_VERDICT", "RELEASE_VERDICT"}:
                if now > execution["wait_deadline"]:
                    self._fault(run, {"PRECONTACT_HUMAN": "PRECONTACT_TIMEOUT", "GRASP_VERDICT": "GRASP_VERDICT_TIMEOUT", "SEMANTIC_VERDICT": "SEMANTIC_TIMEOUT", "RELEASE_VERDICT": "RELEASE_VERDICT_TIMEOUT"}[run["state"]])
            else:
                try:
                    active = self.transport.poll_active()
                except Exception as exc:
                    code = exc.code if isinstance(exc, ContractError) else "ROS_EXEC_POLL_FAILED"
                    if code == "ROS_EXEC_RESULT_TIMEOUT":
                        step = run["plan"]["steps"][execution["step_index"]]
                        if step["phase"] == "GRIPPER_OPEN":
                            code = "GRIPPER_OPEN_TIMEOUT"
                    self._fault(run, code)
                    continue
                if active is not None:
                    execution["active"] = False
                    completed_step = run["plan"]["steps"][execution["step_index"]]
                    execution["terminal_phases"].append(completed_step["phase"])
                    self._emit_phase_event(run, "ACTION_TERMINAL", completed_step, "SUCCEEDED", {"step": completed_step, "terminal_status": "SUCCEEDED"})
                    if completed_step["phase"] in {"GRIPPER_CLOSE", "LIFT_LIN"}:
                        try:
                            feedback, reference = self._verified_gripper_feedback(run)
                            if completed_step["phase"] == "GRIPPER_CLOSE":
                                execution["gripper_feedback_m"] = feedback
                                execution["gripper_reference_m"] = reference
                            else:
                                execution["post_lift_gripper_feedback_m"] = feedback
                        except (ContractError, KeyError, TypeError, ValueError):
                            self._fault(run, "GRIPPER_FEEDBACK_OUT_OF_RANGE")
                            continue
                    execution["step_index"] += 1
                    pause_after = run["plan"]["steps"][execution["step_index"] - 1].get("pause_after")
                    if pause_after in {"GRASP_VERDICT", "SEMANTIC_VERDICT"}:
                        run["state"] = pause_after
                        execution["wait_deadline"] = now + run["plan"]["execution_timeouts_s"][pause_after.lower()]
                        self._emit_phase_event(run, "HOLD_ENTERED", completed_step, None, {"hold": pause_after, "step": completed_step})
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
        if run["state"] not in ACTIVE_STATES:
            raise ContractError("EXECUTION_STATE")
        if not health["writer_alive"] or health["writer_error"] is not None:
            self._fault(run, "RECORDER_WRITER_FAULT")
            return self._execution_response(run, payload["run_id"], payload["plan_digest"], "HEARTBEAT_OK")
        run["execution"]["lease_deadline"] = self.monotonic_clock() + run["plan"]["execution_timeouts_s"]["heartbeat_lease"]
        return self._execution_response(run, payload["run_id"], payload["plan_digest"], "HEARTBEAT_OK")

    def _confirm(self, payload):
        run = self._execution_payload(payload, {"run_id", "plan_digest", "confirmed_by", "source"}, "CONFIRM_SCHEMA")
        if (
            payload["source"] not in {"HUMAN", "CAMPAIGN_AUTHORIZATION"}
            or payload["source"] == "CAMPAIGN_AUTHORIZATION"
            and run["approval"]["approval_scope"] != "HIL_NUMERIC_PROXY"
            or not isinstance(payload["confirmed_by"], str)
            or not SAFE_ID.fullmatch(payload["confirmed_by"])
        ):
            raise ContractError("CONFIRM_SCHEMA")
        if run["state"] != "PRECONTACT_HUMAN":
            raise ContractError("CONFIRM_STATE")
        run["execution"]["precontact_confirmation"] = {"source": payload["source"], "decided_by": payload["confirmed_by"], "decided_at": self.clock().isoformat().replace("+00:00", "Z")}
        self._emit_phase_event(run, "DECISION_RECEIVED", run["plan"]["steps"][run["execution"]["step_index"]], None, {"decision": "PRECONTACT_HUMAN", **run["execution"]["precontact_confirmation"]})
        run["execution"]["confirmed_step"] = run["execution"]["step_index"]
        run["state"] = "EXECUTING"
        self._start_current_step(run)
        return self._execution_response(run, payload["run_id"], payload["plan_digest"], "CONFIRMED")

    def _semantic_verdict(self, payload):
        run = self._execution_payload(payload, {"run_id", "plan_digest", "verdict", "decided_by", "source"}, "VERDICT_SCHEMA")
        if payload["source"] not in {"HUMAN", "HIL_PROXY"} or payload["verdict"] not in {"PASS", "FAIL"} or not isinstance(payload["decided_by"], str) or not SAFE_ID.fullmatch(payload["decided_by"]):
            raise ContractError("VERDICT_SCHEMA")
        if payload["source"] == "HIL_PROXY" and run["approval"]["approval_scope"] != "HIL_NUMERIC_PROXY":
            raise ContractError("VERDICT_SOURCE")
        if run["state"] != "SEMANTIC_VERDICT":
            raise ContractError("VERDICT_STATE")
        run["execution"]["semantic_verdict"] = payload["verdict"]
        run["execution"]["semantic_decision"] = {"source": payload["source"], "decided_by": payload["decided_by"], "decided_at": self.clock().isoformat().replace("+00:00", "Z")}
        self._emit_phase_event(run, "DECISION_RECEIVED", run["plan"]["steps"][run["execution"]["step_index"] - 1], None, {"decision": "SEMANTIC_VERDICT", "verdict": payload["verdict"], **run["execution"]["semantic_decision"]})
        run["state"] = "EXECUTING"
        self._start_current_step(run)
        return self._execution_response(run, payload["run_id"], payload["plan_digest"], "VERDICT_ACCEPTED")

    def _grasp_verdict(self, payload):
        run = self._execution_payload(payload, {"run_id", "plan_digest", "verdict", "decided_by", "source"}, "GRASP_VERDICT_SCHEMA")
        if payload["source"] not in {"HUMAN", "HIL_PROXY"} or payload["verdict"] not in {"PASS", "FAIL"} or not isinstance(payload["decided_by"], str) or not SAFE_ID.fullmatch(payload["decided_by"]):
            raise ContractError("GRASP_VERDICT_SCHEMA")
        if payload["source"] == "HIL_PROXY" and run["approval"]["approval_scope"] != "HIL_NUMERIC_PROXY":
            raise ContractError("GRASP_VERDICT_SOURCE")
        if run["state"] != "GRASP_VERDICT":
            raise ContractError("GRASP_VERDICT_STATE")
        run["execution"]["grasp_verdict"] = payload["verdict"]
        run["execution"]["grasp_decision"] = {"source": payload["source"], "decided_by": payload["decided_by"], "decided_at": self.clock().isoformat().replace("+00:00", "Z")}
        self._emit_phase_event(run, "DECISION_RECEIVED", run["plan"]["steps"][run["execution"]["step_index"] - 1], None, {"decision": "GRASP_VERDICT", "verdict": payload["verdict"], **run["execution"]["grasp_decision"]})
        if payload["verdict"] == "FAIL":
            self._fault(run, "GRASP_REJECTED")
        else:
            run["state"] = "EXECUTING"
            self._start_current_step(run)
        return self._execution_response(run, payload["run_id"], payload["plan_digest"], "GRASP_VERDICT_ACCEPTED")

    def _release_verdict(self, payload):
        run = self._execution_payload(payload, {"run_id", "plan_digest", "verdict", "decided_by", "source"}, "RELEASE_VERDICT_SCHEMA")
        if (
            payload["source"] not in {
                "HUMAN", "LOCAL_UI_BUTTON", "CAMPAIGN_CONTROL_PROXY",
            }
            or payload["verdict"] not in {"LANDED", "OFF_SLOT", "UNCERTAIN"}
            or payload["source"] == "CAMPAIGN_CONTROL_PROXY"
            and (
                payload["verdict"] != "LANDED"
                or run["approval"]["approval_scope"] != "HIL_NUMERIC_PROXY"
            )
            or not isinstance(payload["decided_by"], str)
            or not SAFE_ID.fullmatch(payload["decided_by"])
            or run["state"] != "RELEASE_VERDICT"
        ):
            raise ContractError("RELEASE_VERDICT_SCHEMA")
        execution = run["execution"]
        evidence = copy.deepcopy(execution.get("release_evidence"))
        if (
            not isinstance(evidence, dict)
            or evidence.get("schema_version")
            != "data_factory.recycle_release_evidence.v2"
        ):
            raise ContractError("RELEASE_EVIDENCE")
        evidence["release_outcome"] = (
            "EXPECTED_LANDED"
            if payload["source"] == "CAMPAIGN_CONTROL_PROXY"
            else payload["verdict"]
        )
        evidence["outcome_source"] = {
            "HUMAN": "HUMAN_TTY",
            "LOCAL_UI_BUTTON": "LOCAL_UI_BUTTON",
            "CAMPAIGN_CONTROL_PROXY": "CAMPAIGN_CONTROL_PROXY",
        }[payload["source"]]
        evidence["decided_by"] = payload["decided_by"]
        evidence["decided_at"] = self.clock().isoformat().replace("+00:00", "Z")
        execution["release_verdict"] = payload["verdict"]
        execution["release_decision"] = {
            "source": payload["source"],
            "decided_by": payload["decided_by"],
            "decided_at": self.clock().isoformat().replace("+00:00", "Z"),
        }
        try:
            transition = self.scene_state_store.transition_release(
                instance_id=run["plan"]["scene_binding"]["object_instance_id"],
                release_slot=run["plan"]["scene_binding"]["release_slot"],
                evidence=evidence,
                updated_by="pickup-executor",
                expected_digest=run["execution"].get("scene_state_digest", run["plan"]["scene_binding"]["scene_state_digest"]),
                expected_revision=run["execution"].get("scene_revision", run["plan"]["scene_binding"]["revision"]),
                allowed_next_run_id=run["plan"]["scene_binding"].get("allowed_next_run_id"),
            )
            execution["release_evidence"] = evidence
            execution["scene_transition"] = transition
        except ContractError as exc:
            return self._block_release(run, exc.code)
        except Exception:
            return self._block_release(run, "SCENE_TRANSITION_WRITE_FAILED")
        if payload["verdict"] == "LANDED":
            run["state"] = "COMPLETED"
            return self._execution_response(run, payload["run_id"], payload["plan_digest"], "RELEASE_CONFIRMED")
        return self._block_release(run, "RELEASE_OFF_SLOT" if payload["verdict"] == "OFF_SLOT" else "RELEASE_UNCONFIRMED")

    def _block_release(self, run, code):
        run["failure_code"] = code
        execution = run["execution"]
        execution["durable_blocked"] = False
        try:
            if self.cell_state_store is None:
                raise ContractError("CELL_STATE_STORE_MISSING")
            self.cell_state_store.mark_blocked(code, run["plan"]["run_id"], run["digest"])
            execution["durable_blocked"] = True
        except Exception as exc:
            execution["cell_state_error"] = exc.code if isinstance(exc, ContractError) else "CELL_STATE_WRITE_FAILED"
        run["state"] = "BLOCKED"
        return self._execution_response(run, run["plan"]["run_id"], run["digest"], code)

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
    terminal_ok = None
    def read_lines():
        try:
            for line in input_stream:
                events.put(("line", line))
            events.put(("eof", None))
        except Exception:
            events.put(("error", None))
    threading.Thread(target=read_lines, daemon=True).start()
    def terminal(run):
        executor.close()
        result = executor._execution_response(run, run["plan"]["run_id"], run["digest"], "TERMINAL")
        output_stream.write(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        output_stream.flush()
        return result["state"] == "COMPLETED"
    while True:
        try:
            kind, value = events.get(timeout=0.05)
        except queue.Empty:
            executor.tick()
            continue
        if kind == "line":
            try:
                result = executor.process(load_json_strict(value))
            except ContractError as exc:
                result = _response(code=exc.code, mode=executor.mode)
            if result["state"] in {"BLOCKED", "COMPLETED"} and result.get("run_id") in executor.runs:
                executor.close()
                run = executor.runs[result["run_id"]]
                result["data"] = executor._execution_data(run)
            output_stream.write(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            output_stream.flush()
            if result["state"] in {"BLOCKED", "COMPLETED"}:
                terminal_ok = result["state"] == "COMPLETED"
                continue
            executor.tick()
            for run in executor.runs.values():
                if run["state"] == "BLOCKED":
                    return terminal(run)
            continue
        for run in executor.runs.values():
            if run["state"] in ACTIVE_STATES:
                executor._fault(run, "INPUT_READER_ERROR" if kind == "error" else "INPUT_EOF")
                return terminal(run)
        return terminal_ok if terminal_ok is not None else not any(run["state"] == "BLOCKED" for run in executor.runs.values())


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory-jsonl", action="store_true")
    ros_mode = parser.add_mutually_exclusive_group()
    ros_mode.add_argument("--ros-plan-only", action="store_true")
    ros_mode.add_argument("--ros-live", action="store_true")
    parser.add_argument("--robot-system-id")
    parser.add_argument("--cell-state-root")
    parser.add_argument("--phase-events-root")
    args = parser.parse_args(argv)
    if not args.factory_jsonl:
        parser.error("--factory-jsonl required")
    if args.ros_live:
        if not args.robot_system_id or not args.cell_state_root or not args.phase_events_root:
            parser.error("--ros-live requires --robot-system-id, --cell-state-root, and --phase-events-root")
        try:
            if __package__ in (None, ""):
                from tools.data_factory.cell_state import CellStateStore
                from tools.data_factory.scene_state import SceneStateStore
            else:
                from ..cell_state import CellStateStore
                from ..scene_state import SceneStateStore
            cell_state_store = CellStateStore(args.cell_state_root, args.robot_system_id)
            scene_state_store = SceneStateStore(args.cell_state_root, args.robot_system_id)
        except ContractError as exc:
            parser.error(exc.code)
    elif args.robot_system_id or args.cell_state_root or args.phase_events_root:
        parser.error("--robot-system-id, --cell-state-root, and --phase-events-root require --ros-live")
    else:
        cell_state_store = None
        scene_state_store = None
    transport = None
    node = None
    rclpy = None
    if args.ros_plan_only or args.ros_live:
        os.environ["RCUTILS_LOGGING_USE_STDOUT"] = "0"
        try:
            import rclpy
            if __package__ in (None, ""):
                from tools.data_factory.motion.moveit_transport import RosMoveItTransport
            else:
                from .moveit_transport import RosMoveItTransport
            rclpy.init()
            node = rclpy.create_node("fr5_pickup_live" if args.ros_live else "fr5_pickup_plan_only")
            transport = RosMoveItTransport(node)
        except (ContractError, ImportError, RuntimeError) as exc:
            print(
                json.dumps(
                    {"error": {"code": "ROS_LIVE_UNAVAILABLE" if args.ros_live else "ROS_PLAN_ONLY_UNAVAILABLE", "message": str(exc)}},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                file=sys.stderr,
            )
            try:
                if node is not None:
                    node.destroy_node()
            finally:
                if rclpy is not None and rclpy.ok():
                    rclpy.shutdown()
            return 2
    executor = PickupExecutor(
        transport,
        cell_state_store=cell_state_store,
        scene_state_store=scene_state_store,
        execution_enabled=args.ros_live,
        phase_events_root=args.phase_events_root,
        event_clock=(lambda: (node.get_clock().now().nanoseconds, "ROS_TIME")) if node is not None else None,
    )
    try:
        return 0 if run_jsonl(sys.stdin, sys.stdout, executor) else 2
    finally:
        try:
            executor.close()
        finally:
            try:
                if node is not None:
                    node.destroy_node()
            finally:
                if rclpy is not None and rclpy.ok():
                    rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
