#!/usr/bin/env python3
"""Foreground, loopback-only FAKE console for one collection campaign."""
from __future__ import annotations

import argparse
import copy
import json
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.data_factory.campaign_authoring import (
    DRAFT_SCHEMA,
    campaign_cell_id,
    compile_collection_campaign,
    direct_draft_from_manifest,
    validate_campaign_draft,
)
from tools.data_factory.campaign_operator import (
    FORBIDDEN_FAKE_COUNTERS as OPERATOR_ZERO_SENTINELS,
    CampaignOperator,
)
from tools.data_factory.experiment_manifest import (
    SLOT_INPUT_FIELDS,
    compile_fr5_hypothesis,
    validate_fr5_hypothesis,
)
from tools.data_factory.one_job import (
    TEST_ONLY_READINESS_CONTRACT,
    OneJob,
    hil_numeric_gripper_verdict,
)
from tools.data_factory.operator_bridge import (
    INTENT_SCHEMA,
    ButtonDecisionPort,
    LoopbackBridge,
    OperatorIntentCore,
)
from tools.data_factory.operator_setup import validate_print_measurements
from tools.data_factory.quality.coverage_report import build_coverage_report
from tools.data_factory.scene_state import release_slot
from tools.data_factory.training_split import FR5_FEATURE_CONTRACT
from tools.fr5_data_factory import ContractError, SAFE_ID, canonical_digest, load_json_strict


TEST_OPERATOR = "TEST_OPERATOR"
FAKE_RECORDER_COUNTERS = (
    "fake_recorder_begin",
    "fake_recorder_readiness_status",
    "fake_recorder_freeze",
    "fake_recorder_commit",
)
ZERO_SENTINELS = (
    "physical_factory",
    "robot",
    "gripper",
    "camera",
    "production_recorder",
    "dataset",
    "production_run_state",
    "HUMAN",
    "candidate",
    "inventory",
    "production_coverage",
    "training",
)
EFFECT_COUNTERS = FAKE_RECORDER_COUNTERS + ZERO_SENTINELS
QA_WORKFLOW = (
    "Start this foreground command and open the printed loopback URL.",
    "Confirm FAKE / LIVE_COLLECT / TEST_ONLY and every forbidden effect count is 0.",
    "Press compile once; confirm AWAITING_APPROVAL and the displayed plan digest.",
    "Press the exact-plan approval button once; confirm technical PASS and human semantic NOT_MEASURED.",
    "Refresh and confirm the same terminal result; a repeated old button intent must fail stale/replay.",
    "Restart, compile, then cancel before approval; confirm no execute, commit, candidate, inventory, or training effect.",
)
PREPARE_TIMEOUT_S = 2.0
DECISION_TIMEOUT_S = 600.0
RUNNING_OBSERVATION_WINDOW_S = 0.2
PHASES = (
    "PREGRASP_PTP", "APPROACH_STOP_LIN", "FINAL_APPROACH_LIN",
    "GRIPPER_CLOSE", "LIFT_LIN", "RECYCLE_APPROACH_PTP", "LOWER_LIN",
    "GRIPPER_OPEN", "RETREAT_LIN", "SAFE_POSE_PTP",
)


def new_effect_counters() -> dict[str, int]:
    return {name: 0 for name in EFFECT_COUNTERS}


def _sealed_result(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {"schema_version": "data_factory.fake_episode_result.v1", **copy.deepcopy(dict(value))}
    result["result_digest"] = canonical_digest(result)
    return result


def _recorder_response(request: Mapping[str, Any], state: str, run_id: str | None,
                       transaction_id: str | None, metrics: Mapping[str, Any], *,
                       ok: bool = True, writer_error: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "data_factory.recorder_response.v1",
        "op_id": request["op_id"], "op": request["op"], "ok": ok,
        "state": state, "reason_code": "OK" if ok else state,
        "run_id": run_id, "transaction_id": transaction_id,
        "episode_index": 0, "metrics": copy.deepcopy(dict(metrics)),
        "artifacts": {}, "detail": "SYNTHETIC_FIXTURE",
        "writer_alive": writer_error is None, "writer_error": writer_error,
    }


def _plan_envelope(run_id: str, program: Mapping[str, Any],
                   scene_binding: Mapping[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    planned = {
        "schema_version": "fr5.pickup_plan.v3", "run_id": run_id,
        "scene_binding": copy.deepcopy(dict(scene_binding)),
        "motion_program_digest": canonical_digest(program),
        "resolved_job_digest": program["resolved_job_digest"],
        "binding_digests": copy.deepcopy(program["binding_digests"]),
        "steps": [
            {"phase": phase, "start_joint_state": [0.0] * 6,
             "final_joint_state": [0.0] * 6}
            for phase in PHASES
        ],
    }
    plan_digest = canonical_digest(planned)
    readback = {
        "schema_version": "data_factory.planning_scene_readback.v1",
        "run_id": run_id, "plan_digest": plan_digest,
        "expected_planning_scene_digest": program["binding_digests"]["planning_scene_digest"],
        "objects": [],
    }
    collision = {
        "schema_version": "data_factory.collision_report.v1",
        "plan_digest": plan_digest, "sample_count": 0, "samples": [],
        "failure_count": 0, "all_valid": True,
    }
    no_motion = {
        "schema_version": "data_factory.plan_only_no_motion.v1",
        "run_id": run_id, "plan_digest": plan_digest,
        "before_snapshot": {}, "after_snapshot": {},
        "max_joint_delta_rad": 0.0, "gripper_delta_m": 0.0,
        "execute_goal_count": 0, "gripper_goal_count": 0,
    }
    safety = {
        "schema_version": "data_factory.precommit_safety.v1",
        "run_id": run_id, "approved_plan_digest": plan_digest,
        "scene_binding_digest": canonical_digest(scene_binding),
        "expected_planning_scene_digest": program["binding_digests"]["planning_scene_digest"],
        "planning_scene_readback_digest": canonical_digest(readback),
        "collision_report_digest": canonical_digest(collision),
        "plan_only_no_motion_digest": canonical_digest(no_motion),
        "post_reset_safe_snapshot_digest": None, "status": "PENDING",
    }
    return plan_digest, {
        "plan": planned, "precommit_safety": safety,
        "precommit_evidence": {
            "schema_version": "data_factory.precommit_evidence.v1",
            "run_id": run_id, "approved_plan_digest": plan_digest,
            "scene_binding_digest": canonical_digest(scene_binding),
            "expected_planning_scene_digest": program["binding_digests"]["planning_scene_digest"],
            "planning_scene_readback": readback, "collision_report": collision,
            "plan_only_no_motion": no_motion,
        },
        "operator_summary": {"effect_scope": "FAKE", "operator": TEST_OPERATOR},
    }, safety


def make_fake_one_job(*, trace: list[str], counters: dict[str, int],
                      fault: str | None = None, clock=None) -> OneJob:
    """Return an actual OneJob backed only by process-local pure callables."""
    if fault not in {None, "readiness_rate", "readiness_drop", "readiness_fault", "executor_plan"}:
        raise ContractError("FAKE_CONSOLE_FAULT")
    if set(counters) != set(EFFECT_COUNTERS):
        raise ContractError("FAKE_CONSOLE_COUNTERS")
    state: dict[str, Any] = {
        "run_id": None, "transaction_id": None, "frozen": False,
        "semantic": False, "release_requested": False,
        "safety": None, "plan_digest": None,
        "readiness_seen": False,
    }

    def metrics() -> dict[str, Any]:
        drops = int(fault == "readiness_drop")
        row_fps = 26.0 if fault == "readiness_rate" else 30.0
        return {
            "rows": 60, "writer_queue": 0, "writer_queue_drops": drops,
            "alignment_failures": 0, "observed_monotonic_ns": time.monotonic_ns(),
            "quality_snapshot": {
                "accepted": True, "reasons": [], "frames": 60,
                "target_fps": 30, "effective_fps": row_fps,
                "cameras": {"synthetic-up": {"source_fps": 30.0}},
                "writer_queue_drops": drops, "alignment_failures": 0,
                "image_quality_warnings": [],
            },
        }

    def recorder(request: Mapping[str, Any]) -> dict[str, Any]:
        op = request["op"]
        trace.append("recorder:readiness_status" if op == "status" and not state["frozen"] and not state["readiness_seen"] else f"recorder:{op}")
        if op == "begin":
            state["run_id"] = request["transaction"]["run_id"]
            state["transaction_id"] = f"{state['run_id']}-transaction"
            counters["fake_recorder_begin"] += 1
            return _recorder_response(request, "RECORDING", state["run_id"], state["transaction_id"], metrics())
        if op == "status":
            if not state["frozen"] and not state["readiness_seen"]:
                counters["fake_recorder_readiness_status"] += 1
                state["readiness_seen"] = True
            return _recorder_response(
                request, "FROZEN" if state["frozen"] else "RECORDING",
                state["run_id"], state["transaction_id"], metrics(),
                writer_error="synthetic writer fault" if fault == "readiness_fault" and not state["frozen"] else None,
            )
        if op == "freeze":
            state["frozen"] = True
            counters["fake_recorder_freeze"] += 1
            return _recorder_response(request, "FROZEN", state["run_id"], state["transaction_id"], metrics())
        if op == "commit":
            counters["fake_recorder_commit"] += 1
            return _recorder_response(request, "COMMITTED", state["run_id"], state["transaction_id"], metrics())
        if op == "abort":
            return _recorder_response(request, "ABORTED", state["run_id"], state["transaction_id"], metrics())
        raise ContractError("SYNTHETIC_RECORDER_OP")

    def executor(request: Mapping[str, Any]) -> dict[str, Any]:
        op, payload = request["op"], request["payload"]
        trace.append(f"executor:{op}")
        run_id = payload.get("run_id") if isinstance(payload, dict) else state["run_id"]
        if op == "plan" and fault == "executor_plan":
            return {
                "schema_version": "fr5.pickup_executor.response.v3", "mode": "FAKE",
                "op_id": request["op_id"], "op": op, "ok": False,
                "code": "SYNTHETIC_EXECUTOR_PLAN_FAIL", "run_id": run_id,
                "plan_digest": None, "state": "BLOCKED", "data": None,
            }
        if op == "plan":
            digest, data, safety = _plan_envelope(
                run_id, payload["motion_program"], payload["scene_binding"],
            )
            state.update(run_id=run_id, plan_digest=digest, safety=safety)
            response_state, data = "PLANNED", data
        elif op == "approve":
            response_state, data = "APPROVED", None
        elif op == "execute":
            response_state, data = "EXECUTING", None
        elif op == "semantic_verdict":
            state["semantic"] = True
            response_state, data = "EXECUTING", None
        elif op == "heartbeat" and not state["semantic"]:
            response_state, data = "SEMANTIC_VERDICT", {
                "gripper_feedback_m": 0.011, "gripper_reference_m": 0.01,
                "post_lift_gripper_feedback_m": 0.011,
            }
        elif op == "heartbeat" and not state["release_requested"]:
            response_state, data = "RELEASE_VERDICT", None
        elif op == "release_verdict":
            state["release_requested"] = True
            response_state, data = "COMPLETED", None
        elif op == "heartbeat":
            response_state, data = "COMPLETED", {
                "precommit_safety": {
                    **state["safety"],
                    "post_reset_safe_snapshot_digest": canonical_digest("synthetic-safe-reset"),
                    "status": "PASS",
                },
            }
        elif op == "cancel":
            response_state, data = "BLOCKED", {"durable_blocked": True}
        else:
            raise ContractError("SYNTHETIC_EXECUTOR_OP")
        return {
            "schema_version": "fr5.pickup_executor.response.v3", "mode": "FAKE",
            "op_id": request["op_id"], "op": op, "ok": op != "cancel",
            "code": response_state, "run_id": run_id,
            "plan_digest": state["plan_digest"], "state": response_state, "data": data,
        }

    holder: dict[str, OneJob] = {}

    def cell_state() -> dict[str, Any]:
        trace.append("cell:TEST_OPERATOR_ACKNOWLEDGED")
        job = holder["job"]
        return {
            "robot_system_id": job._program["robot_system_id"], "cell_ready": True,
            "reason_code": "TEST_OPERATOR_ACKNOWLEDGED", "run_id": job.run_id,
            "plan_digest": job.plan_digest, "acknowledged_by": TEST_OPERATOR,
        }

    job = OneJob(
        recorder, executor, cell_state_call=cell_state, clock=clock,
        readiness_contract=copy.deepcopy(TEST_ONLY_READINESS_CONTRACT),
        allow_synthetic_test_operator=True,
    )
    holder["job"] = job
    return job


def _motion_program(intent: Mapping[str, Any], hypothesis: Mapping[str, Any]) -> dict[str, Any]:
    resolver = next(
        item for item in hypothesis["resolver_receipts"]
        if item["resolver_result_digest"] == intent["base_condition"]["resolver_result_digest"]
    )
    planning_scene = {
        "frame_id": "base_link",
        "floor": {"id": "synthetic-floor", "dimensions_m": [1.0, 1.0, 0.1],
                  "surface_z_m": 0.0, "source": "SYNTHETIC_FIXTURE"},
        "wall": {"id": "synthetic-wall", "dimensions_m": [1.0, 0.1, 1.0],
                 "near_face_y_m": 1.0, "wall_side": "opposite_home_arm_protrusion",
                 "home_arm_protrusion_base_xy": [1.0, 0.0], "j1_home_deg": 0.0},
    }
    bindings = {
        **copy.deepcopy(resolver["input_digests"]),
        "robot_description_digest": canonical_digest("synthetic-robot-description"),
        "moveit_config_digest": canonical_digest("synthetic-moveit-config"),
        "planning_scene_digest": canonical_digest(planning_scene),
        "motion_qualification": canonical_digest("synthetic-motion-qualification"),
        "home_candidate": intent["robot_start_pose"]["home_candidate_digest"],
    }
    target = {
        "translation_m": [0.0, 0.0, 0.1],
        "rotation_columns": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    }
    motion_limits = {
        "velocity_scaling": 0.1, "acceleration_scaling": 0.1,
        "planning_timeout_s": 1.0, "execution_timeout_s": 2.0,
    }
    gripper_limits = {
        "command_duration_s": 0.1, "execution_timeout_s": 1.0,
        "completion_tolerance_m": 0.002,
    }
    steps = []
    for phase in PHASES:
        step: dict[str, Any] = {
            "phase": phase,
            "limits": copy.deepcopy(gripper_limits if phase.startswith("GRIPPER") else motion_limits),
        }
        if phase == "SAFE_POSE_PTP":
            pose = intent["robot_start_pose"]
            step["joint_positions_rad"] = [pose["target_rad"][joint] for joint in pose["joint_order"]]
        elif phase.startswith("GRIPPER"):
            step["gripper_position_m"] = 0.01 if phase == "GRIPPER_CLOSE" else 0.02
        else:
            step["target"] = {"base_tcp": copy.deepcopy(target), "base_tool": copy.deepcopy(target)}
        if phase == "LIFT_LIN":
            step["pause_after"] = "SEMANTIC_VERDICT"
        steps.append(step)
    return {
        "schema_version": "fr5.motion_program.v2",
        "robot_system_id": intent["fixed_contract"]["robot_system_id"],
        "resolved_job_digest": intent["base_condition"]["resolved_job_digest"],
        "binding_digests": bindings,
        "frames": {"planning_frame": "base_link", "planning_group": "fairino5_v6_group",
                   "tool_link": "wrist3_link"},
        "planning_scene": planning_scene,
        "planning": {
            "pipeline_id": "pilz_industrial_motion_planner", "ptp_planner_id": "PTP",
            "lin_planner_id": "LIN",
            "goal_tolerances": {"position_m": 0.001, "orientation_rad": 0.01, "joint_rad": 0.01},
            "max_joint_state_age_s": 0.1,
        },
        "gripper_requirements": {
            "command_position_m": 0.01, "acceptable_feedback_m": {"min": 0.01, "max": 0.012},
            "velocity_percent": 50, "force_percent": 50,
            "evidence_digest": canonical_digest("synthetic-gripper-requirements"),
        },
        "execution_timeouts_s": {
            "heartbeat_lease": 2.0, "cancel": 2.0, "precontact_confirmation": 2.0,
            "grasp_verdict": 2.0, "semantic_verdict": 2.0,
        },
        "steps": steps,
    }


class FakeOperatorConsole:
    """UI adapter around CampaignOperator, ButtonDecisionPort, and one worker."""

    def __init__(
        self, *, session_id: str, hypothesis: Mapping[str, Any], draft: Mapping[str, Any],
        fixture_root: str | Path, one_job_factory: Callable[[], OneJob],
        counters: dict[str, int], trace: list[str], expires_at: str,
        technical_status: str = "PASS", current_usage: Mapping[str, int] | None = None,
        clock=None,
    ):
        if not isinstance(session_id, str) or not SAFE_ID.fullmatch(session_id):
            raise ContractError("FAKE_CONSOLE_SESSION_ID")
        root = Path(fixture_root)
        if root.is_symlink() or not root.is_dir():
            raise ContractError("FAKE_CONSOLE_FIXTURE_ROOT")
        if not callable(one_job_factory) or technical_status not in {"PASS", "FAIL"}:
            raise ContractError("FAKE_CONSOLE_PORT")
        if set(counters) != set(EFFECT_COUNTERS) or any(type(value) is not int or value < 0 for value in counters.values()):
            raise ContractError("FAKE_CONSOLE_COUNTERS")
        self.session_id = session_id
        self.hypothesis = validate_fr5_hypothesis(hypothesis)
        if self.hypothesis["qualification_catalog"]["source"] != "SYNTHETIC_TEST_ONLY":
            raise ContractError("FAKE_CONSOLE_SYNTHETIC_FIXTURE_REQUIRED")
        checked_draft = validate_campaign_draft(draft, hypothesis=self.hypothesis)
        self.fixture_root = root.resolve(strict=True)
        self._one_job_factory = one_job_factory
        self.counters, self.trace = counters, trace
        self.expires_at, self.technical_status = expires_at, technical_status
        self.current_usage = copy.deepcopy(current_usage)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.intent = self.plan_result = self.episode_plan = self.episode_result = None
        self.synthetic_review = self.synthetic_coverage_update = None
        self._scene_digest = self.hypothesis["fixed_contract"]["scene_digest"]
        self._workflow, self._last_error, self._run_index = "AUTHORING", None, 0
        self._factory_calls, self._lifecycle_ids = 0, set()
        self.button_port = self._thread = self._prepare_event = None
        self._decision_choice = self._pending_one_job = self._pending_technical = None
        self._initial_handler_active = False
        self._workspace_revision = 0
        self._wizard = {
            "capability": "OFFLINE_ONLY",
            "plane_reference": {
                "id": "synthetic-table-plane",
                "digest": canonical_digest("synthetic-table-plane"),
                "table_normal_base": [0.0, 0.0, 1.0],
            },
            "source_measurement_mm": None, "final_measurement_mm": None,
            "captures": {"CENTER": False, "X_REF": False, "Y_CHECK": False},
            "print_measurement_digest": None,
            "saved_revision_digest": None,
        }
        self.campaign_operator = CampaignOperator(
            session_id=f"{session_id}-campaign", lifecycle_owner=TEST_OPERATOR,
            workspace={
                "workspace_id": "synthetic-workspace", "identity": "SYNTHETIC",
                "fixture_root": str(self.fixture_root),
            },
            hypothesis=self.hypothesis, draft=checked_draft,
            effect_scope="FAKE", lifecycle_action="LIVE_COLLECT",
            data_disposition="SYNTHETIC_FIXTURE",
            subsystems={
                "workspace": {"readiness": "READY", "capability": "AUTHOR", "reason": "SYNTHETIC"},
                "planner": {"readiness": "READY", "capability": "PLAN", "reason": "SYNTHETIC"},
                "recorder": {"readiness": "READY", "capability": "FAKE_ONLY", "reason": "SYNTHETIC"},
            },
            expires_at=expires_at, initial_scene_digest=self._scene_digest,
            scene_evidence_call=self._scene_evidence,
            side_effect_counter_call=self._operator_counter_snapshot,
            fake_lifecycle_factory=self._fresh_one_job,
            fake_live_call=self._live_episode,
            current_usage=self.current_usage,
            clock=self.clock,
        )
        self.draft = copy.deepcopy(self.campaign_operator.draft)
        self.manifest = self.receipt = None
        self.core = OperatorIntentCore(
            session_id=session_id, projection_call=self.projection,
            handlers={
                "update_draft": self.update_draft,
                "capture_workspace_point": self.capture_workspace_point,
                "save_workspace_revision": self.save_workspace_revision,
                "compile_draft": self.compile_draft,
                "approve_exact_plan": self.approve_exact_plan,
                "reject_plan": self.reject_plan,
                "cancel_session": self.cancel_session,
            },
            clock=self.clock,
        )

    @property
    def bridge_core(self) -> OperatorIntentCore:
        """The exact atomic seam accepted by the existing LoopbackBridge."""
        return self.core

    @property
    def factory_calls(self) -> int:
        return self._factory_calls

    @property
    def session(self):
        """Expose the CampaignOperator-owned session for integration assertions."""
        return self.campaign_operator._session

    def _operator_counter_snapshot(self) -> dict[str, int]:
        aliases = {"run_state": "production_run_state", "human": "HUMAN"}
        result = {name: self.counters[name] for name in FAKE_RECORDER_COUNTERS}
        result.update({
            name: self.counters[aliases.get(name, name)]
            for name in OPERATOR_ZERO_SENTINELS
        })
        return result

    def _fresh_one_job(self) -> OneJob:
        job = self._one_job_factory()
        self._factory_calls += 1
        if (
            type(job) is not OneJob or job.state != "IDLE"
            or job.readiness_contract != TEST_ONLY_READINESS_CONTRACT
            or job.allow_synthetic_test_operator is not True
        ):
            raise ContractError("FAKE_CONSOLE_ONE_JOB_REQUIRED")
        if id(job) in self._lifecycle_ids:
            raise ContractError("FAKE_CONSOLE_ONE_JOB_REUSED")
        self._lifecycle_ids.add(id(job))
        self.trace.append(f"factory:OneJob:{self._factory_calls}")
        return job

    def _scene_evidence(self, _run_id: str) -> dict[str, Any]:
        observed_at = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        value = {
            "schema_version": "data_factory.scene_freshness_evidence.v1",
            "scene_digest": self._scene_digest, "observed_at": observed_at,
        }
        value["evidence_digest"] = canonical_digest(value)
        return value

    def _technical(
        self, intent: Mapping[str, Any], lifecycle_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        status = self.technical_status
        post_scene = canonical_digest([
            "SYNTHETIC_POST_SCENE", intent["run_id"], intent["intent_digest"],
            lifecycle_result.get("plan_digest"), status,
        ])
        value = {
            "schema_version": "data_factory.seed_technical_result.v1",
            "intent_digest": intent["intent_digest"], "run_id": intent["run_id"],
            "manifest_digest": intent["manifest_digest"],
            "slot_id": intent["slot"]["slot_id"], "status": status,
            "technical_result_digest": canonical_digest({
                "one_job_state": lifecycle_result.get("state"),
                "plan_digest": lifecycle_result.get("plan_digest"),
                "readiness_evidence": lifecycle_result.get("readiness_evidence"),
                "frozen_rows": lifecycle_result.get("frozen_rows"),
                "rows_after_recycle": lifecycle_result.get("rows_after_recycle"),
                "status": status,
            }),
            "post_scene_digest": post_scene,
            "observed_at": self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        value["evidence_digest"] = canonical_digest(value)
        return value

    def _intent_projection(
        self, intent: Mapping[str, Any], plan_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        resolver = next(
            item for item in self.hypothesis["resolver_receipts"]
            if item["resolver_result_digest"] == intent["base_condition"]["resolver_result_digest"]
        )
        return {
            "schema_version": "data_factory.fake_episode_plan.v1",
            "run_id": intent["run_id"], "intent_digest": intent["intent_digest"],
            "manifest_digest": intent["manifest_digest"],
            "compilation_receipt_digest": self.receipt["receipt_digest"],
            "hypothesis_digest": intent["hypothesis_digest"],
            "normalized_seed": self.manifest["normalized_seed"],
            "base_condition": copy.deepcopy(intent["base_condition"]),
            "resolver_result": copy.deepcopy(resolver),
            "robot_start_pose": copy.deepcopy(intent["robot_start_pose"]),
            "slot": copy.deepcopy(intent["slot"]),
            "budget_digests": copy.deepcopy(intent["budget_digests"]),
            "plan_digest": plan_result["plan_digest"],
            "scene_binding_digest": canonical_digest(plan_result["scene_binding"]),
            "approval_scope": "HIL_NUMERIC_PROXY", "data_disposition": "TEST_ONLY",
            "fixture_identity": "SYNTHETIC_TEST_ONLY",
        }

    def _fail_episode(self, lifecycle: OneJob, result: Mapping[str, Any]) -> None:
        self._pending_one_job = copy.deepcopy(dict(result))
        raise ContractError(result.get("code") or "FAKE_CONSOLE_EPISODE_FAILED")

    def _episode_context(
        self, intent: Mapping[str, Any], value: Mapping[str, Any],
    ) -> dict[str, Any]:
        fields = {
            "schema_version", "session_id", "run_id", "intent_digest",
            "effect_scope", "lifecycle_action", "data_disposition",
            "root_binding", "start_binding", "context_digest",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ContractError("FAKE_CONSOLE_EPISODE_CONTEXT")
        result = copy.deepcopy(dict(value))
        if (
            result["schema_version"] != "data_factory.campaign_episode_context.v1"
            or result["session_id"] != self.campaign_operator.session_id
            or result["run_id"] != intent["run_id"]
            or result["intent_digest"] != intent["intent_digest"]
            or result["effect_scope"] != "FAKE"
            or result["lifecycle_action"] != "LIVE_COLLECT"
            or result["data_disposition"] != "SYNTHETIC_FIXTURE"
            or result["root_binding"] is not None
            or result["start_binding"] is not None
            or result["context_digest"] != canonical_digest({
                key: item for key, item in result.items() if key != "context_digest"
            })
        ):
            raise ContractError("FAKE_CONSOLE_EPISODE_CONTEXT")
        return result

    def _live_episode(
        self, intent: dict[str, Any], lifecycle: OneJob,
        cancel_event: threading.Event, episode_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Drive the CampaignOperator-owned child after one exact button choice."""
        episode_context = self._episode_context(intent, episode_context)
        self.trace.append(f"context:{episode_context['context_digest']}")
        program = _motion_program(intent, self.hypothesis)
        condition = intent["base_condition"]["coverage_condition"]
        scene_binding = {
            "scene_state_digest": intent["required_scene_digest"],
            "revision": self._run_index, "object_instance_id": "synthetic-object",
            "release_slot": release_slot(
                robot_system_id=intent["fixed_contract"]["robot_system_id"],
                pose={key: condition[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")},
                object_profile_id=intent["fixed_contract"]["object_profile_id"],
                exclusion_geometry_digest=canonical_digest("synthetic-release-exclusion"),
            ),
        }
        plan_result = lifecycle.plan_only(intent["run_id"], program, scene_binding)
        if not plan_result["ok"]:
            self._pending_one_job = copy.deepcopy(plan_result)
            raise ContractError(plan_result["code"])
        plan = self._intent_projection(intent, plan_result)
        plan["episode_context"] = copy.deepcopy(episode_context)
        plan["episode_context_digest"] = episode_context["context_digest"]
        port = ButtonDecisionPort(
            session_id=f"{self.session_id}-button-{self._run_index}",
            operator_label=TEST_OPERATOR, clock=self.clock,
        )
        offered = port.offer(
            run_id=intent["run_id"], plan_digest=plan_result["plan_digest"],
            decision_binding=plan, approval_scope="HIL_NUMERIC_PROXY",
        )
        plan["decision_binding_digest"] = offered["projection"]["pending_plan"]["decision_binding_digest"]
        with self._lock:
            self.intent, self.plan_result = copy.deepcopy(intent), copy.deepcopy(plan_result)
            self.episode_plan, self.button_port = copy.deepcopy(plan), port
            self._workflow, self._last_error = "AWAITING_APPROVAL", None
            self.trace.append("console:AWAITING_APPROVAL")
            self._prepare_event.set()
        decision = port.wait(DECISION_TIMEOUT_S)
        if decision is None:
            self._pending_one_job = lifecycle.cancel()
            raise ContractError("FAKE_CONSOLE_DECISION_TIMEOUT")
        if decision["choice"] != "APPROVE":
            self._decision_choice = decision["choice"]
            if lifecycle.state not in {"ABORTED", "BLOCKED"}:
                self._pending_one_job = lifecycle.cancel()
            raise ContractError(f"FAKE_CONSOLE_PLAN_{decision['choice']}ED")
        if decision["run_id"] != intent["run_id"] or decision["plan_digest"] != lifecycle.plan_digest:
            self._fail_episode(lifecycle, lifecycle.cancel())
        self.trace.append("one_job:approve")
        result = lifecycle.approve({
            "source": decision["decision_source"],
            "approval_id": f"{intent['run_id']}-button",
            "approved_by": decision["operator_label"],
            "approval_expiry": self.expires_at,
            "approval_scope": decision["approval_scope"],
        })
        if not result["ok"]:
            self._fail_episode(lifecycle, result)
        if cancel_event.wait(RUNNING_OBSERVATION_WINDOW_S):
            raise ContractError("FAKE_CONSOLE_PLAN_CANCELLED")
        self.trace.append("one_job:start")
        result = lifecycle.start(cancel_event=cancel_event)
        if not result["ok"]:
            self._fail_episode(lifecycle, result)
        for _ in range(10):
            if cancel_event.is_set():
                raise ContractError("FAKE_CONSOLE_PLAN_CANCELLED")
            result = lifecycle.poll()
            if not result["ok"]:
                self._fail_episode(lifecycle, result)
            if result["state"] == "SEMANTIC_VERDICT":
                verdict = hil_numeric_gripper_verdict(
                    result["state"], result["execution_evidence"],
                    lifecycle._program["gripper_requirements"],
                )
                self.trace.append(f"semantic:{TEST_OPERATOR}:HIL_PROXY:{verdict}")
                result = lifecycle.semantic_verdict(verdict, TEST_OPERATOR, source="HIL_PROXY")
                if not result["ok"]:
                    self._fail_episode(lifecycle, result)
            elif result["state"] == "RELEASE_VERDICT":
                self.trace.append(f"release:{TEST_OPERATOR}:LANDED")
                result = lifecycle.release_verdict("LANDED", TEST_OPERATOR, source="TEST_OPERATOR")
                if not result["ok"]:
                    self._fail_episode(lifecycle, result)
            elif result["state"] == "AWAITING_CELL_READY":
                self.trace.append("one_job:finish")
                result = lifecycle.finish()
                break
        else:
            self._fail_episode(lifecycle, {**result, "code": "FAKE_CONSOLE_POLL_BOUND"})
        if not result["ok"] or result["state"] != "COMPLETE":
            self._fail_episode(lifecycle, result)
        technical = self._technical(intent, result)
        self._pending_one_job = copy.deepcopy(result)
        self._pending_technical = copy.deepcopy(technical)
        return {
            "result": {
                "one_job": copy.deepcopy(result),
                "technical_evidence": copy.deepcopy(technical),
                "human_semantic": "NOT_MEASURED",
            },
            "technical_evidence": technical,
        }

    @staticmethod
    def _campaign_status(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        nested = value.get("campaign")
        return copy.deepcopy(dict(nested if isinstance(nested, Mapping) else value))

    def _review_projection(
        self, technical: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        review = {
            "schema_version": "data_factory.fake_synthetic_review.v1",
            "identity": "SYNTHETIC", "reviewed_by": TEST_OPERATOR,
            "review_source": "LOCAL_TEST_OPERATOR", "technical_status": "PASS",
            "technical_result_digest": technical["technical_result_digest"],
            "human_semantic": "NOT_MEASURED", "persistence": "PROCESS_LOCAL_ONLY",
        }
        review["review_digest"] = canonical_digest(review)
        coverage = {
            "schema_version": "data_factory.fake_synthetic_coverage_update.v1",
            "source": "SYNTHETIC_TEST_ONLY", "synthetic_review_delta": 1,
            "human_semantic_pass_delta": 0, "production_coverage_delta": 0,
            "review_digest": review["review_digest"], "persistence": "PROCESS_LOCAL_ONLY",
        }
        coverage["coverage_update_digest"] = canonical_digest(coverage)
        return review, coverage

    def _result_binding(self) -> dict[str, Any] | None:
        if self.episode_plan is None:
            return None
        plan = self.episode_plan
        return {
            "episode_plan_digest": canonical_digest(plan),
            "run_id": plan["run_id"], "intent_digest": plan["intent_digest"],
            "manifest_digest": plan["manifest_digest"],
            "compilation_receipt_digest": plan["compilation_receipt_digest"],
            "hypothesis_digest": plan["hypothesis_digest"],
            "normalized_seed": plan["normalized_seed"],
            "base_condition_digest": plan["base_condition"]["base_condition_digest"],
            "resolver_result_digest": plan["resolver_result"]["resolver_result_digest"],
            "robot_start_pose_id": plan["robot_start_pose"]["robot_start_pose_id"],
            "slot": copy.deepcopy(plan["slot"]),
            "budget_digests": copy.deepcopy(plan["budget_digests"]),
            "plan_digest": plan["plan_digest"],
            "decision_binding_digest": plan["decision_binding_digest"],
            "episode_context_digest": plan["episode_context_digest"],
        }

    def _publish_outcome(self, outcome: Mapping[str, Any]) -> None:
        campaign = self._campaign_status(outcome.get("campaign"))
        code = outcome.get("code")
        if outcome.get("ok") is True and self._pending_technical is not None:
            technical = self._pending_technical
            if technical["status"] == "PASS":
                review, coverage = self._review_projection(technical)
                self.synthetic_review, self.synthetic_coverage_update = review, coverage
                self._scene_digest = technical["post_scene_digest"]
                self._run_index += 1
                self._workflow = "TERMINAL" if campaign["state"] == "COMPLETE" else "AUTHORING"
                self._last_error = None
                self.episode_result = _sealed_result({
                    "outcome": "PASS", "code": "TECHNICAL_PASS",
                    "technical_evidence": copy.deepcopy(technical),
                    "one_job": copy.deepcopy(self._pending_one_job),
                    "campaign": campaign, "synthetic_review": review,
                    "synthetic_coverage_update": coverage,
                    "intent_binding": self._result_binding(),
                    "human_semantic": "NOT_MEASURED",
                })
                self.trace.extend((
                    "campaign:technical_PASS", "review:TEST_OPERATOR:SYNTHETIC",
                    "coverage:SYNTHETIC_TEST_ONLY:+1",
                ))
                return
            code = "SEED_CAMPAIGN_TECHNICAL_NOT_PASS"
        choice = self._decision_choice
        if choice in {"REJECT", "CANCEL"}:
            outcome_name, code = choice, f"PLAN_{choice}ED"
        else:
            outcome_name, code = "FAIL", code or "FAKE_CONSOLE_EPISODE_FAILED"
        self._workflow = "TERMINAL" if choice == "CANCEL" else "BLOCKED"
        self._last_error = code
        self.episode_result = _sealed_result({
            "outcome": outcome_name, "code": code,
            "technical_evidence": copy.deepcopy(self._pending_technical),
            "one_job": copy.deepcopy(self._pending_one_job),
            "campaign": campaign, "intent_binding": self._result_binding(),
            "human_semantic": "NOT_MEASURED",
        })
        self.trace.append(f"campaign:technical_FAIL:{code}")

    def _worker_target(self, run_id: str) -> None:
        try:
            outcome = self.campaign_operator.run_next({"run_id": run_id}, {})
        except ContractError as exc:
            outcome = {
                "ok": False, "code": exc.code,
                "campaign": None if self.session is None else self.session.status(),
            }
        with self._lock:
            initial = self._initial_handler_active
        if initial:
            with self._lock:
                self._publish_outcome(outcome)
                self._prepare_event.set()
        else:
            self.core.transition(lambda: self._publish_outcome(outcome))

    def compile_draft(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        if payload != {"draft_id": self.draft["draft_id"], "data_disposition": "TEST_ONLY"}:
            raise ContractError("FAKE_CONSOLE_COMPILE_FIELDS")
        with self._lock:
            if self._workflow != "AUTHORING" or self._thread is not None and self._thread.is_alive():
                raise ContractError("FAKE_CONSOLE_NOT_AUTHORING")
            try:
                if self.campaign_operator.manifest is None:
                    self.campaign_operator.compile_draft({}, {})
                    self.manifest = copy.deepcopy(self.campaign_operator.manifest)
                    self.receipt = copy.deepcopy(self.campaign_operator.compilation_receipt)
            except ContractError as exc:
                self._workflow, self._last_error = "BLOCKED", exc.code
                self.episode_result = _sealed_result({
                    "outcome": "FAIL", "code": exc.code, "human_semantic": "NOT_MEASURED",
                })
                return copy.deepcopy(self.episode_result)
            self._decision_choice = self._pending_one_job = self._pending_technical = None
            self.intent = self.plan_result = self.episode_plan = self.episode_result = None
            self.synthetic_review = self.synthetic_coverage_update = None
            self.button_port = None
            self._workflow, self._last_error = "RUNNING", None
            self._prepare_event = threading.Event()
            self._initial_handler_active = True
            run_id = f"synthetic-run-{self._run_index}"
            self._thread = threading.Thread(
                target=self._worker_target, args=(run_id,),
                name=f"fake-console-episode-{self._run_index}", daemon=False,
            )
            self._thread.start()
        ready = self._prepare_event.wait(PREPARE_TIMEOUT_S)
        with self._lock:
            self._initial_handler_active = False
            if not ready:
                raise ContractError("FAKE_CONSOLE_PREPARE_TIMEOUT")
            if self._workflow == "AWAITING_APPROVAL":
                return {
                    "outcome": "AWAITING_APPROVAL",
                    "episode_plan": copy.deepcopy(self.episode_plan),
                }
            return copy.deepcopy(self.episode_result)

    def _decision_payload(self) -> dict[str, Any]:
        return {
            "plan_digest": self.episode_plan["plan_digest"],
            "approval_scope": "HIL_NUMERIC_PROXY", "data_disposition": "TEST_ONLY",
        }

    @staticmethod
    def _port_intent(
        snapshot: dict[str, Any], op: str, digest: str, name: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": INTENT_SCHEMA, "intent_id": name,
            "session_id": snapshot["session_id"], "view_revision": snapshot["revision"],
            "view_digest": snapshot["view_digest"], "op": op,
            "payload": {"decision_binding_digest": digest},
        }

    def _consume_button(self, choice: str) -> dict[str, Any]:
        snapshot = self.button_port.core.snapshot()
        pending = snapshot["projection"]["pending_plan"]
        if pending is None or pending["decision_binding_digest"] != self.episode_plan["decision_binding_digest"]:
            raise ContractError("FAKE_CONSOLE_BUTTON_STATE")
        op = {
            "APPROVE": "approve_exact_plan", "REJECT": "reject_plan", "CANCEL": "cancel_plan",
        }[choice]
        return self.button_port.core.consume(self._port_intent(
            snapshot, op, pending["decision_binding_digest"],
            f"button-{choice.lower()}-{self._run_index}",
        ))["result"]

    def approve_exact_plan(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._workflow != "AWAITING_APPROVAL" or payload != self._decision_payload():
                raise ContractError("FAKE_CONSOLE_PLAN_DIGEST_MISMATCH")
            self.trace.append("button:APPROVE")
            self._workflow = "RUNNING"
            decision = self._consume_button("APPROVE")
            return {
                "outcome": "RUNNING", "active_child_id": self.intent["run_id"],
                "decision": decision,
            }

    def reject_plan(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._workflow != "AWAITING_APPROVAL" or payload != self._decision_payload():
                raise ContractError("FAKE_CONSOLE_PLAN_DIGEST_MISMATCH")
            self.trace.append("button:REJECT")
            self._workflow = "CANCELLING"
            decision = self._consume_button("REJECT")
            return {"outcome": "REJECT", "decision": decision}

    def cancel_session(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if (
                self._workflow not in {"AWAITING_APPROVAL", "RUNNING"}
                or self.intent is None
                or payload != {"active_child_id": self.intent["run_id"]}
            ):
                raise ContractError("FAKE_CONSOLE_CANCEL_BINDING")
            awaiting = self._workflow == "AWAITING_APPROVAL"
            self.trace.append("button:CANCEL")
            cancelled = self.campaign_operator.cancel_campaign({}, {})
            self._decision_choice = "CANCEL"
            if isinstance(cancelled.get("child"), Mapping):
                self._pending_one_job = copy.deepcopy(cancelled["child"])
            self._workflow = "CANCELLING"
            decision = self._consume_button("CANCEL") if awaiting else None
            return {"outcome": "CANCELLING", "decision": decision, **cancelled}

    def _candidate_slots(self, count: int | None = None) -> dict[str, dict[str, Any]]:
        count = self.draft["requested_count"] if count is None else count
        budget = self.draft["manifest_budget"]
        template = {
            "hil_prompts": min(1, budget["max_hil_prompts"] // count),
            "reviews": min(1, budget["max_reviews"] // count),
            "pending_reviews": 0,
            "storage_bytes": max(1, budget["max_storage_bytes"] // count),
        }
        result = {}
        for repeat_index in range(count):
            for pair in self.hypothesis["allowed_pairs"]:
                for split in pair["split_groups"]:
                    cell_id = campaign_cell_id(
                        pair["base_condition_digest"], pair["robot_start_pose_id"],
                        split, repeat_index,
                    )
                    result[cell_id] = {
                        "slot_id": cell_id,
                        "base_condition_digest": pair["base_condition_digest"],
                        "robot_start_pose_id": pair["robot_start_pose_id"],
                        "split_group": split, "repeat_index": repeat_index, **template,
                    }
        return result

    def _full_update(self, authoring_mode: str, **changes: Any) -> dict[str, Any]:
        payload = {
            "authoring_mode": authoring_mode,
            "requested_count": self.draft["requested_count"],
            "normalized_seed": self.draft["normalized_seed"],
            "pinned": copy.deepcopy(self.draft["pinned"]),
            "excluded": copy.deepcopy(self.draft["excluded"]),
            "direct_slots": copy.deepcopy(self.draft["direct_slots"]),
        }
        payload.update(changes)
        result = self.campaign_operator.update_draft(payload, {})
        self.draft = copy.deepcopy(self.campaign_operator.draft)
        return result

    def update_draft(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._workflow != "AUTHORING" or payload.get("draft_id") != self.draft["draft_id"]:
                raise ContractError("FAKE_CONSOLE_UPDATE_BINDING")
            change = set(payload) - {"draft_id"}
            if len(change) != 1 or not change <= {"authoring_mode", "budget", "toggle_cell_id"}:
                raise ContractError("FAKE_CONSOLE_UPDATE_FIELDS")
            mode = "ASSISTED" if self.draft["selector"] == "BALANCED_INITIAL" else "DIRECT_EDIT"
            if "authoring_mode" in change:
                requested = payload["authoring_mode"]
                if requested not in {"ASSISTED", "DIRECT_EDIT"}:
                    raise ContractError("FAKE_CONSOLE_AUTHORING_MODE")
                if requested == mode:
                    return {"draft_revision": self.draft["revision"], "authoring_mode": mode}
                if requested == "DIRECT_EDIT":
                    manifest, _ = compile_collection_campaign(self.draft, hypothesis=self.hypothesis)
                    direct = direct_draft_from_manifest(self.draft, manifest, hypothesis=self.hypothesis)
                    result = self._full_update(
                        "DIRECT_EDIT", requested_count=direct["requested_count"],
                        pinned=[], excluded=[], direct_slots=direct["direct_slots"],
                    )
                else:
                    result = self._full_update("ASSISTED", pinned=[], excluded=[], direct_slots=[])
            elif "budget" in change:
                requested_count = payload["budget"]
                if type(requested_count) is not int or requested_count <= 0:
                    raise ContractError("FAKE_CONSOLE_BUDGET")
                if mode == "ASSISTED":
                    result = self._full_update(mode, requested_count=requested_count)
                else:
                    assisted = copy.deepcopy(self.draft)
                    assisted.update(
                        selector="BALANCED_INITIAL", requested_count=requested_count,
                        direct_slots=[],
                    )
                    assisted = validate_campaign_draft(assisted, hypothesis=self.hypothesis)
                    manifest, _ = compile_collection_campaign(assisted, hypothesis=self.hypothesis)
                    slots = [{key: item[key] for key in SLOT_INPUT_FIELDS} for item in manifest["slots"]]
                    result = self._full_update(
                        mode, requested_count=requested_count, direct_slots=slots,
                    )
            else:
                cell_id = payload["toggle_cell_id"]
                candidates = self._candidate_slots()
                if not isinstance(cell_id, str) or cell_id not in candidates:
                    raise ContractError("FAKE_CONSOLE_CELL")
                preview, _ = compile_collection_campaign(self.draft, hypothesis=self.hypothesis)
                selected = {item["slot_id"] for item in preview["slots"]}
                if mode == "ASSISTED":
                    pinned, excluded = set(self.draft["pinned"]), set(self.draft["excluded"])
                    if cell_id in selected:
                        pinned.discard(cell_id)
                        excluded.add(cell_id)
                    else:
                        excluded.discard(cell_id)
                        pinned.add(cell_id)
                    result = self._full_update(
                        mode, pinned=sorted(pinned), excluded=sorted(excluded),
                    )
                else:
                    slots = {item["slot_id"]: item for item in self.draft["direct_slots"]}
                    if cell_id in slots:
                        if len(slots) == 1:
                            raise ContractError("FAKE_CONSOLE_EMPTY_DIRECT")
                        slots.pop(cell_id)
                    else:
                        slots[cell_id] = candidates[cell_id]
                    count = len(slots)
                    budget = self.draft["manifest_budget"]
                    costs = {
                        "hil_prompts": min(1, budget["max_hil_prompts"] // count),
                        "reviews": min(1, budget["max_reviews"] // count),
                        "pending_reviews": 0,
                        "storage_bytes": max(1, budget["max_storage_bytes"] // count),
                    }
                    normalized = {
                        key: {**{field: item[field] for field in (
                            "slot_id", "base_condition_digest", "robot_start_pose_id",
                            "split_group", "repeat_index",
                        )}, **costs}
                        for key, item in slots.items()
                    }
                    result = self._full_update(
                        mode, requested_count=len(slots),
                        direct_slots=[normalized[key] for key in sorted(slots)],
                    )
            return {
                **result,
                "authoring_mode": "ASSISTED" if self.draft["selector"] == "BALANCED_INITIAL" else "DIRECT_EDIT",
            }

    def capture_workspace_point(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "draft_id", "mode", "point", "source_measurement_mm",
            "final_measurement_mm", "plane_reference_digest",
        }
        with self._lock:
            if (
                self._workflow != "AUTHORING" or set(payload) != fields
                or payload["draft_id"] != self.draft["draft_id"] or payload["mode"] != "FAKE"
                or payload["point"] not in self._wizard["captures"]
                or payload["plane_reference_digest"] != self._wizard["plane_reference"]["digest"]
            ):
                raise ContractError("FAKE_CONSOLE_WORKSPACE_CAPTURE")
            measured = validate_print_measurements(
                source_scale_bar_mm=payload["source_measurement_mm"],
                final_scale_bar_mm=payload["final_measurement_mm"],
            )
            self._wizard["source_measurement_mm"] = measured["source_scale_bar_measured_mm"]
            self._wizard["final_measurement_mm"] = measured["final_scale_bar_measured_mm"]
            self._wizard["print_measurement_digest"] = measured["measurement_digest"]
            self._wizard["captures"][payload["point"]] = True
            return {
                "point": payload["point"], "captured": True, "mode": "FAKE",
                "measurement_digest": measured["measurement_digest"],
            }

    def save_workspace_revision(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if (
                self._workflow != "AUTHORING"
                or set(payload) != {"draft_id", "mode", "source_measurement_mm", "final_measurement_mm"}
                or payload["draft_id"] != self.draft["draft_id"] or payload["mode"] != "FAKE"
                or not all(self._wizard["captures"].values())
            ):
                raise ContractError("FAKE_CONSOLE_WORKSPACE_SAVE")
            measured = validate_print_measurements(
                source_scale_bar_mm=payload["source_measurement_mm"],
                final_scale_bar_mm=payload["final_measurement_mm"],
            )
            if (
                measured["source_scale_bar_measured_mm"] != self._wizard["source_measurement_mm"]
                or measured["final_scale_bar_measured_mm"] != self._wizard["final_measurement_mm"]
                or measured["measurement_digest"] != self._wizard["print_measurement_digest"]
            ):
                raise ContractError("FAKE_CONSOLE_WORKSPACE_SAVE")
            self._workspace_revision += 1
            value = {
                "workspace_revision": f"synthetic-session-r{self._workspace_revision:03d}",
                "mode": "FAKE", "identity": "SYNTHETIC",
                "captures": copy.deepcopy(self._wizard["captures"]),
            }
            value["revision_digest"] = canonical_digest(value)
            self._wizard["saved_revision_digest"] = value["revision_digest"]
            return value

    def _cells(self) -> tuple[list[dict[str, Any]], int]:
        preview, receipt = compile_collection_campaign(self.draft, hypothesis=self.hypothesis)
        selected = {item["slot_id"] for item in preview["slots"]}
        decisions = {item["cell_id"]: item for item in receipt["decisions"]}
        bases = {item["base_condition_digest"]: item for item in self.hypothesis["base_conditions"]}
        result = []
        for cell_id, slot in self._candidate_slots().items():
            condition = bases[slot["base_condition_digest"]]["coverage_condition"]
            decision = decisions.get(cell_id, {
                "status": "ELIGIBLE_NOT_SELECTED",
                "reason_codes": ["DIRECT_EDIT_NOT_SELECTED"],
            })
            result.append({
                "cell_id": cell_id, "x_mm": condition["x_mm"], "y_mm": condition["y_mm"],
                "yaw_deg": condition["yaw_deg"], "split": slot["split_group"],
                "repeat": slot["repeat_index"] + 1, "coverage_count": 0,
                "selection_state": "SELECTED" if cell_id in selected else "AVAILABLE",
                "eligibility_status": "BLOCKED" if decision["status"] == "INELIGIBLE" else "ELIGIBLE",
                "reason_codes": copy.deepcopy(decision["reason_codes"]),
            })
        return result, len(selected)

    def projection(self) -> dict[str, Any]:
        with self._lock:
            fixed = self.hypothesis["fixed_contract"]
            base = self.hypothesis["base_conditions"][0]["coverage_condition"]
            pose = self.hypothesis["robot_start_poses"][0]
            resolver = self.hypothesis["resolver_receipts"][0]["normalized_job"]
            cells, selected_count = self._cells()
            available = {
                "AUTHORING": [
                    "update_draft", "capture_workspace_point", "save_workspace_revision",
                    "compile_draft",
                ],
                "AWAITING_APPROVAL": ["approve_exact_plan", "reject_plan", "cancel_session"],
                "RUNNING": ["cancel_session"],
            }.get(self._workflow, [])
            campaign = None if self.session is None else self.session.status()
            runtime = {
                "workflow_state": self._workflow, "measurement_outcome": "NOT_MEASURED",
                "reason_codes": [] if self._last_error is None else [self._last_error],
                "active_child_id": None if self.intent is None or self._workflow not in {
                    "AWAITING_APPROVAL", "RUNNING", "CANCELLING",
                } else self.intent["run_id"],
            }
            if self._workflow in {"RUNNING", "CANCELLING"}:
                runtime.update({
                    "phase": "ONE_JOB" if self._workflow == "RUNNING" else "CANCEL",
                    "progress": 50 if self._workflow == "RUNNING" else 90,
                    "detail": "Bounded synthetic episode thread; no hardware authority.",
                })
            return {
            "connection_state": "READY", "effect_scope": "FAKE",
            "lifecycle_action": "LIVE_COLLECT", "data_disposition": "TEST_ONLY",
            "available_ops": available,
            "fixed_lane": {
                "workspace": {"display_name": "Synthetic fixture", "place_id": base["place_id"],
                              "revision": f"synthetic-session-r{self._workspace_revision:03d}",
                              "bounds": "fixture-only X / Y / yaw"},
                "object_id": fixed["object_profile_id"], "grasp_id": fixed["grasp_profile_id"],
                "task": {"id": fixed["task"], "capability": "OFFLINE_ONLY"},
                "motion": {"id": fixed["motion_recipe"], "capability": "OFFLINE_ONLY"},
                "start_pose_id": pose["robot_start_pose_id"], "camera_role": "synthetic-up · FAKE",
                "profile_id": resolver["collection_profile_id"],
            },
            "draft": {
                "draft_id": self.draft["draft_id"], "revision": self.draft["revision"],
                "authoring_mode": "ASSISTED" if self.draft["selector"] == "BALANCED_INITIAL" else "DIRECT_EDIT",
                "selector": self.draft["selector"], "selector_version": "v1 · canonical row tie-break",
                "budget": self.draft["requested_count"], "selected_count": selected_count,
                "blocked_count": sum(item["eligibility_status"] == "BLOCKED" for item in cells),
                "estimated_minutes": self.draft["requested_count"],
                "split_summary": " · ".join(f"{name} {count}" for name, count in sorted(Counter(item["split"] for item in cells).items())),
                "repeat_summary": f"×1–{self.draft['requested_count']}",
                "coverage_summary": f"{selected_count}/{len(cells)} selected",
                "cells": cells,
            },
            "capabilities": [
                {"label": "Task · pickup_e2e", "status": "OFFLINE_ONLY", "reason_codes": ["SYNTHETIC_FIXTURE"]},
                {"label": "Task · pick_place", "status": "NOT_AVAILABLE", "reason_codes": ["FUTURE_TASK_RECIPE"]},
                {"label": "Motion · DIRECT", "status": "OFFLINE_ONLY", "reason_codes": ["SYNTHETIC_FIXTURE"]},
            ],
            "runtime": runtime,
            "approval": None if self._workflow != "AWAITING_APPROVAL" else {
                "plan_digest": self.episode_plan["plan_digest"],
                "approval_scope": "HIL_NUMERIC_PROXY", "test_only_paths": str(self.fixture_root),
                "decision_binding_digest": self.episode_plan["decision_binding_digest"],
            },
            "workspace_wizard": copy.deepcopy(self._wizard),
            "effect_counts": copy.deepcopy(self.counters),
            "episode_plan": copy.deepcopy(self.episode_plan),
            "episode_result": copy.deepcopy(self.episode_result),
            "campaign_operator": self.campaign_operator.projection(),
            "campaign_session": campaign,
            "synthetic_review": copy.deepcopy(self.synthetic_review),
            "synthetic_coverage_update": copy.deepcopy(self.synthetic_coverage_update),
            "operator_identity": TEST_OPERATOR,
            "human_semantic": "NOT_MEASURED",
            "qa_workflow": list(QA_WORKFLOW),
        }

    def wait_for_episode(self, timeout_s: float = PREPARE_TIMEOUT_S) -> dict[str, Any] | None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
        with self._lock:
            return copy.deepcopy(self.episode_result)

    def close(self) -> None:
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        with self._lock:
            awaiting = self._workflow == "AWAITING_APPROVAL" and self.button_port is not None
        try:
            self.campaign_operator.cancel_campaign({}, {})
        except ContractError:
            pass
        if awaiting:
            try:
                self._consume_button("CANCEL")
            except ContractError:
                pass
        thread.join(PREPARE_TIMEOUT_S)
        if thread.is_alive():
            raise ContractError("FAKE_CONSOLE_THREAD_LEAK")


def build_fake_operator_console(
    *, hypothesis: Mapping[str, Any], draft: Mapping[str, Any], fixture_root: str | Path,
    session_id: str = "fake-console-session", expires_at: str | None = None,
    one_job_factory: Callable[[], OneJob] | None = None,
    fault: str | None = None, technical_status: str = "PASS",
    current_usage: Mapping[str, int] | None = None, clock=None,
) -> FakeOperatorConsole:
    """Build the small public seam used by LoopbackBridge and browser integration."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    counters, trace = new_effect_counters(), []
    factory = one_job_factory or (lambda: make_fake_one_job(
        trace=trace, counters=counters, fault=fault, clock=clock,
    ))
    expiry = expires_at or (clock() + timedelta(hours=1)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return FakeOperatorConsole(
        session_id=session_id, hypothesis=hypothesis, draft=draft,
        fixture_root=fixture_root, one_job_factory=factory,
        counters=counters, trace=trace, expires_at=expiry,
        technical_status=technical_status, current_usage=current_usage, clock=clock,
    )


def synthetic_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the deterministic no-argument QA fixture without touching production files."""
    digest = canonical_digest

    def redigest(value: dict[str, Any], field: str) -> dict[str, Any]:
        value[field] = digest({key: item for key, item in value.items() if key != field})
        return value

    documents = {
        "robot_system": {
            "schema_version": "data_factory.robot_system.v1",
            "robot_system_id": "fr5-r1", "qualification_status": "QUALIFIED",
            "base_frame": "base_link", "tcp_digest": digest("synthetic-tcp"),
        },
        "collection_profile": {
            "schema_version": "data_factory.collection_profile.v1",
            "collection_profile_id": "fr5-dual-rgb-30hz-v1",
            "qualification_status": "QUALIFIED",
        },
        "object_profile": {
            "schema_version": "data_factory.object_profile.v2",
            "object_profile_id": "object-r1", "qualification_status": "QUALIFIED",
            "description": "synthetic object", "dimensions_mm": [40, 30, 20],
            "datum": "center",
        },
        "grasp_profile": {
            "schema_version": "data_factory.grasp_profile.v2",
            "grasp_profile_id": "grasp-r1", "qualification_status": "QUALIFIED",
            "object_profile_id": "object-r1", "grasp_kind": "top_center",
        },
        "cell_calibration": {
            "schema_version": "data_factory.cell_calibration.v1",
            "calibration_id": "calibration-r1", "qualification_status": "QUALIFIED",
            "robot_system_id": "fr5-r1", "place_id": "place-r1",
        },
    }
    fixed = {
        "schema_version": "data_factory.fr5_fixed_contract.v1",
        "robot_system_id": "fr5-r1", "task": "pickup_e2e",
        "instruction": "pick up the synthetic object",
        "collection_profile_digest": digest(documents["collection_profile"]),
        "feature_contract": copy.deepcopy(FR5_FEATURE_CONTRACT),
        "object_profile_id": "object-r1", "grasp_profile_id": "grasp-r1",
        "scene_digest": digest("synthetic-scene"),
        "cell_calibration_id": "calibration-r1",
        "cell_calibration_digest": digest(documents["cell_calibration"]),
        "motion_recipe": "DIRECT", "motion_recipe_digest": digest("synthetic-direct"),
        "pregrasp_digest": digest("synthetic-pregrasp"),
        "waypoint_digest": digest("synthetic-waypoint"),
        "trajectory_digest": digest("synthetic-trajectory"),
    }
    conditions = [
        {
            "task_schema_version": "data_factory.job.v1", "task": "pickup_e2e",
            "robot_system_id": "fr5-r1", "place_id": "place-r1",
            "cell_calibration_id": "calibration-r1",
            "cell_calibration_digest": fixed["cell_calibration_digest"],
            "yaw_deg": yaw, "x_mm": x_mm, "y_mm": 0,
            "object_profile_id": "object-r1", "grasp_profile_id": "grasp-r1",
            "motion_recipe_digest": fixed["motion_recipe_digest"],
            "collection_profile_digest": fixed["collection_profile_digest"],
        }
        for yaw, x_mm in ((0, 10), (90, 20))
    ]
    report = build_coverage_report(
        collection_profile_id="fr5-dual-rgb-30hz-v1",
        domain=conditions, episodes=[],
    )
    resolvers = []
    for condition, name in zip(conditions, ("a", "b")):
        sheet_digest = digest(["synthetic-sheet", name])
        job = {
            "schema_version": "data_factory.job.v1", "job_id": f"job-{name}",
            "task": condition["task"], "robot_system_id": condition["robot_system_id"],
            "collection_profile_id": "fr5-dual-rgb-30hz-v1",
            "place_id": condition["place_id"],
            "cell_calibration_id": condition["cell_calibration_id"],
            "sheet_manifest_digest": sheet_digest, "yaw_deg": condition["yaw_deg"],
            "x_mm": condition["x_mm"], "y_mm": condition["y_mm"],
            "object_profile_id": condition["object_profile_id"],
            "grasp_profile_id": condition["grasp_profile_id"],
            "instruction": "pick up the synthetic object", "episode_intent": "nominal pickup",
            "operator_or_agent_id": "synthetic-test",
            "approval_expiry": "2099-01-01T00:00:00Z", "dry_run_required": True,
        }
        inputs = {
            "selected_sheet": sheet_digest, "yaw0_sheet": digest("synthetic-yaw0"),
            **{key: digest(value) for key, value in documents.items()},
        }
        resolvers.append({
            "normalized_job": job, "input_digests": inputs,
            "resolved_job_digest": digest({"job": job, "input_digests": inputs}),
            "robot": documents["robot_system"],
            "collection_profile": documents["collection_profile"],
            "calibration": {
                "center": [0.4, 0.0, 0.1], "x": [1.0, 0.0, 0.0],
                "y": [0.0, 1.0, 0.0], "z": [0.0, 0.0, 1.0],
                "document": documents["cell_calibration"],
            },
            "object_profile": documents["object_profile"],
            "grasp_profile": documents["grasp_profile"],
        })
    base_qualifications = [
        redigest({
            "schema_version": "data_factory.fr5_base_condition_qualification.v1",
            "source": "SYNTHETIC_TEST_ONLY", "qualification_status": "QUALIFIED",
            "coverage_report_digest": digest(report),
            "coverage_domain_digest": report["domain_digest"],
            "coverage_condition_digest": digest(condition),
            "resolver_result_digest": digest(resolved),
            "resolved_job_digest": resolved["resolved_job_digest"],
            "yaw_action_binding_digest": digest(["synthetic-yaw-action", name]),
            "dual_view_observability_digest": digest(["synthetic-view", name]),
        }, "qualification_digest")
        for resolved, condition, name in zip(resolvers, conditions, ("a", "b"))
    ]
    joints = ("j1", "j2", "j3", "j4", "j5", "j6")
    pose_qualifications = [
        redigest({
            "schema_version": "data_factory.robot_start_pose_qualification.v1",
            "source": "SYNTHETIC_TEST_ONLY", "robot_system_id": "fr5-r1",
            "robot_start_pose_id": name, "joint_order": list(joints),
            "target_rad": {joint: offset + index / 10 for index, joint in enumerate(joints)},
            "tolerance_rad": {joint: 0.01 for joint in joints},
            "home_candidate_digest": digest(["synthetic-home", name]),
            "qualification_status": "QUALIFIED", "safety_status": "SAFE_FOR_MOTION",
        }, "qualification_digest")
        for name, offset in (("start-1", 0.0), ("start-2", 0.1), ("start-3", 0.2))
    ]
    allowed = [
        {
            "base_condition_qualification_digest": base_qualifications[0]["qualification_digest"],
            "robot_start_pose_qualification_digest": pose_qualifications[index]["qualification_digest"],
            "split_groups": ["TRAIN", "ID"],
        }
        for index in (0, 1)
    ] + [{
        "base_condition_qualification_digest": base_qualifications[1]["qualification_digest"],
        "robot_start_pose_qualification_digest": pose_qualifications[2]["qualification_digest"],
        "split_groups": ["OOD"],
    }]
    allowed.sort(key=lambda item: (
        item["base_condition_qualification_digest"],
        item["robot_start_pose_qualification_digest"],
    ))
    catalog = redigest({
        "schema_version": "data_factory.fr5_qualification_catalog.v1",
        "source": "SYNTHETIC_TEST_ONLY", "qualification_status": "QUALIFIED",
        "fixed_contract_digest": digest(fixed), "coverage_report_digest": digest(report),
        "coverage_domain_digest": report["domain_digest"],
        "resolver_result_digests": sorted(digest(item) for item in resolvers),
        "base_condition_qualifications": base_qualifications,
        "robot_start_pose_qualifications": pose_qualifications,
        "allowed_pairs": allowed,
    }, "catalog_digest")
    hypothesis = compile_fr5_hypothesis(
        fixed_contract=fixed, coverage_report=report,
        resolver_results=resolvers, qualification_catalog=catalog,
    )
    budget = {
        "max_physical_episodes": 10, "max_rollout_trials": 10,
        "max_hil_prompts": 10, "max_reviews": 10,
        "max_pending_reviews": 10, "max_storage_bytes": 10_000,
    }
    program_budget = {
        "max_rounds": 5, "used_rounds": 0,
        "max_total_physical_episodes": 10, "used_total_physical_episodes": 0,
        "max_total_rollout_trials": 10, "used_total_rollout_trials": 0,
        "max_total_hil_prompts": 10, "used_total_hil_prompts": 0,
        "max_total_reviews": 10, "used_total_reviews": 0,
        "max_pending_reviews": 10, "used_pending_reviews": 0,
        "max_total_storage_bytes": 10_000, "used_total_storage_bytes": 0,
    }
    draft = {
        "schema_version": DRAFT_SCHEMA, "draft_id": "campaign-draft-r001",
        "revision": 0,
        "source": {
            "hypothesis_digest": hypothesis["hypothesis_digest"],
            "catalog_digest": hypothesis["qualification_catalog"]["catalog_digest"],
            "coverage_digest": digest(hypothesis["coverage_report"]),
        },
        "branch": "INITIAL_SEED", "selector": "BALANCED_INITIAL",
        "requested_count": 1, "normalized_seed": 17,
        "pinned": [], "excluded": [], "direct_slots": [],
        "manifest_id": "collection-campaign-r001",
        "manifest_budget": budget, "program_budget": program_budget,
    }
    return hypothesis, draft


def _load_fixture(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if root.is_symlink() or not root.is_dir():
        raise ContractError("FAKE_CONSOLE_FIXTURE_ROOT")
    return load_json_strict(root / "hypothesis.json"), load_json_strict(root / "draft.json")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve the existing operator UI over a foreground FAKE LoopbackBridge",
        epilog="Under-10-minute QA: " + " ".join(f"{index + 1}) {step}" for index, step in enumerate(QA_WORKFLOW)),
    )
    parser.add_argument("--port", type=int, default=4174)
    parser.add_argument(
        "--fixture-root",
        help="Synthetic directory containing hypothesis.json and draft.json; omitted uses the built-in fixture in a cleaned temporary root",
    )
    args = parser.parse_args(argv)
    temporary = tempfile.TemporaryDirectory(prefix="fake-operator-console-") if args.fixture_root is None else None
    root = Path(temporary.name if temporary is not None else args.fixture_root)
    bridge = console = None
    try:
        hypothesis, draft = synthetic_fixture() if temporary is not None else _load_fixture(root)
        console = build_fake_operator_console(hypothesis=hypothesis, draft=draft, fixture_root=root)
        bridge = LoopbackBridge(
            core=console.bridge_core,
            ui_root=Path(__file__).resolve().parents[2] / "operator-ui",
            host="127.0.0.1", port=args.port,
        )
        print(json.dumps({
            "status": "LISTENING", "url": bridge.origin, "effect_scope": "FAKE",
            "operator_identity": TEST_OPERATOR, "fixture_root": str(root),
            "qa_workflow": QA_WORKFLOW,
        }, sort_keys=True), flush=True)
        bridge.serve_forever()
    except KeyboardInterrupt:
        return 130
    except (ContractError, OSError) as exc:
        code = exc.code if isinstance(exc, ContractError) else "FAKE_CONSOLE_FAILED"
        print(json.dumps({"error": {"code": code, "message": str(exc)}}, sort_keys=True), flush=True)
        return 2
    finally:
        if console is not None:
            console.close()
        if bridge is not None:
            bridge.server.server_close()
        if temporary is not None:
            temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
