"""Thin foreground UI adapter for one injected PHYSICAL TEST_ONLY campaign.

The injected episode callable remains the adapter to ``run_live``.  This module
owns no robot, recorder, dataset, scheduler, or lifecycle state machine.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import stat
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.data_factory.campaign_authoring import DRAFT_SCHEMA, validate_campaign_draft
from tools.data_factory.campaign_operator import (
    CampaignOperator,
    SIDE_EFFECT_COUNTERS,
)
from tools.data_factory.experiment_manifest import (
    FR5_TEST_ONLY_FEATURE_CONTRACT,
    compile_fr5_hypothesis,
)
from tools.data_factory.one_job import OneJob, TEST_ONLY_READINESS_CONTRACT
from tools.data_factory.operator_bridge import (
    ButtonDecisionPort,
    CandidateReviewPort,
    INTENT_SCHEMA,
    LoopbackBridge,
    OperatorCheckpointPort,
    OperatorIntentCore,
)
from tools.data_factory.operator_setup import (
    build_camera_binding_from_discovery,
    build_test_only_episode_binding,
    build_test_only_root_binding,
    build_test_only_start_binding,
    initialize_test_only_state_from_user_declaration,
    gripper_setup_projection,
    load_camera_binding_receipt,
    reuse_camera_binding_receipt,
    write_camera_binding_receipt,
)
from tools.data_factory.quality.coverage_report import build_coverage_report
from tools.data_factory.scene_state import SceneStateStore
from tools.data_factory import run_job
from tools.fr5_data_factory import (
    ContractError,
    DIGEST,
    SAFE_ID,
    canonical_digest,
    load_json_strict,
)


PLAN_REQUEST_FIELDS = frozenset({
    "schema_version", "run_id", "plan_digest", "approval_scope",
    "decision_binding", "timeout_s",
})
BASE_PROJECTION_FIELDS = frozenset({
    "setup", "fixed_lane", "draft", "capabilities", "workspace_wizard",
    "effect_counts",
})
SETUP_FIELDS = frozenset({"host_status", "operator_label", "subsystems"})
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JOB = Path(
    "config/data_factory/test_only_physical/goal2-place1/"
    "center-live-p45-20260821-r001.job.json"
)
DEFAULT_YAW0 = Path(
    "config/data_factory/test_only_physical/goal2-place1/yaw0_sheet.json"
)
DEFAULT_MOTION = Path(
    "config/data_factory/motion_qualifications/fr5-place-a-wood-cube-r001.json"
)
DEFAULT_HOME = Path(
    "config/data_factory/home_candidates/fr5-lab-a-home-r001.json"
)
DEFAULT_PROFILE = Path(
    "config/data_factory/collection_profiles/fr5-up-rgb-30hz-v1.json"
)
DEFAULT_URDF = Path("src/fairino_description/urdf/fairino5_v6.urdf")
DEFAULT_TCP_MANIFEST = Path(
    "config/data_factory/test_only_physical/goal2-place1/tcp_candidate_manifest.json"
)


def _measurement_for_code(code: str) -> str:
    if code == "PHYSICAL_SECOND_MOTION_OWNER" or code.endswith("_MISMATCH"):
        return "FAIL"
    if code.startswith(("PHYSICAL_", "GRIPPER_SETUP_")) or code.endswith("NOT_AVAILABLE"):
        return "NOT_AVAILABLE"
    return "FAIL"


def _redigest(value: dict[str, Any], field: str) -> dict[str, Any]:
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


def build_physical_test_contract(
    *, resolved_job: Mapping[str, Any], motion_qualification: Mapping[str, Any],
    home_candidate: Mapping[str, Any], scene_digest: str,
    draft_id: str, manifest_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile one in-memory, one-camera TEST_ONLY condition and one-slot draft."""
    if not isinstance(resolved_job, Mapping):
        raise ContractError("PHYSICAL_CONSOLE_RESOLVED_JOB")
    job = resolved_job.get("normalized_job")
    inputs = resolved_job.get("input_digests")
    profile = resolved_job.get("collection_profile")
    if (
        not isinstance(job, Mapping)
        or not isinstance(inputs, Mapping)
        or not isinstance(profile, Mapping)
        or profile.get("collection_profile_id")
        != FR5_TEST_ONLY_FEATURE_CONTRACT["collection_profile_id"]
        or job.get("collection_profile_id") != profile.get("collection_profile_id")
        or inputs.get("collection_profile") != canonical_digest(profile)
        or not isinstance(scene_digest, str)
        or not DIGEST.fullmatch(scene_digest)
        or motion_qualification.get("schema_version")
        != "data_factory.motion_qualification.v1"
        or motion_qualification.get("qualification_status") != "QUALIFIED"
        or home_candidate.get("schema_version") != "data_factory.home_candidate.v1"
        or motion_qualification.get("home_candidate_digest")
        != canonical_digest(home_candidate)
        or any(
            motion_qualification.get(field) != job.get(field)
            for field in (
                "robot_system_id", "cell_calibration_id", "object_profile_id",
                "grasp_profile_id",
            )
        )
    ):
        raise ContractError("PHYSICAL_CONSOLE_FIXED_BINDING")

    motion_digest = canonical_digest(motion_qualification)
    fixed = {
        "schema_version": "data_factory.fr5_fixed_contract.v1",
        "robot_system_id": job["robot_system_id"],
        "task": job["task"],
        "instruction": job["instruction"],
        "collection_profile_digest": inputs["collection_profile"],
        "feature_contract": copy.deepcopy(FR5_TEST_ONLY_FEATURE_CONTRACT),
        "object_profile_id": job["object_profile_id"],
        "grasp_profile_id": job["grasp_profile_id"],
        "scene_digest": scene_digest,
        "cell_calibration_id": job["cell_calibration_id"],
        "cell_calibration_digest": inputs["cell_calibration"],
        "motion_recipe": "DIRECT",
        "motion_recipe_digest": motion_digest,
        "pregrasp_digest": canonical_digest({
            "motion_qualification": motion_digest, "phase": "PREGRASP_PTP",
        }),
        "waypoint_digest": canonical_digest({
            "motion_qualification": motion_digest,
            "phases": ["APPROACH_STOP_LIN", "FINAL_APPROACH_LIN", "LIFT_LIN"],
        }),
        "trajectory_digest": canonical_digest({
            "motion_qualification": motion_digest, "recipe": "DIRECT",
        }),
    }
    condition = {
        "task_schema_version": job["schema_version"],
        "task": job["task"],
        "robot_system_id": job["robot_system_id"],
        "place_id": job["place_id"],
        "cell_calibration_id": job["cell_calibration_id"],
        "cell_calibration_digest": inputs["cell_calibration"],
        "yaw_deg": job["yaw_deg"],
        "x_mm": job["x_mm"],
        "y_mm": job["y_mm"],
        "object_profile_id": job["object_profile_id"],
        "grasp_profile_id": job["grasp_profile_id"],
        "motion_recipe_digest": motion_digest,
        "collection_profile_digest": inputs["collection_profile"],
    }
    report = build_coverage_report(
        collection_profile_id=profile["collection_profile_id"],
        domain=[condition], episodes=[],
    )
    base = _redigest({
        "schema_version": "data_factory.fr5_base_condition_qualification.v1",
        "source": "SYNTHETIC_TEST_ONLY",
        "qualification_status": "QUALIFIED",
        "coverage_report_digest": canonical_digest(report),
        "coverage_domain_digest": report["domain_digest"],
        "coverage_condition_digest": canonical_digest(condition),
        "resolver_result_digest": canonical_digest(resolved_job),
        "resolved_job_digest": resolved_job["resolved_job_digest"],
        "yaw_action_binding_digest": canonical_digest({
            "scope": "TEST_ONLY", "yaw_deg": job["yaw_deg"],
            "motion_qualification_digest": motion_digest,
        }),
        "dual_view_observability_digest": canonical_digest({
            "single_view": "CONNECTED_UNPLACED",
            "dual_view": "NOT_AVAILABLE",
            "semantic_authority": "NONE",
        }),
    }, "qualification_digest")
    joint_order = list(motion_qualification["qualified_safe_joint_positions_rad"])
    if len(joint_order) != 6:
        raise ContractError("PHYSICAL_CONSOLE_START_POSE")
    joints = ("j1", "j2", "j3", "j4", "j5", "j6")
    tolerance = motion_qualification.get("goal_tolerances", {}).get("joint_rad")
    pose = _redigest({
        "schema_version": "data_factory.robot_start_pose_qualification.v1",
        "source": "SYNTHETIC_TEST_ONLY",
        "robot_system_id": job["robot_system_id"],
        "robot_start_pose_id": "test-only-home-r001",
        "joint_order": list(joints),
        "target_rad": dict(zip(joints, joint_order)),
        "tolerance_rad": {joint: tolerance for joint in joints},
        "home_candidate_digest": canonical_digest(home_candidate),
        "qualification_status": "QUALIFIED",
        "safety_status": "SAFE_FOR_MOTION",
    }, "qualification_digest")
    catalog = _redigest({
        "schema_version": "data_factory.fr5_qualification_catalog.v1",
        "source": "SYNTHETIC_TEST_ONLY",
        "qualification_status": "QUALIFIED",
        "fixed_contract_digest": canonical_digest(fixed),
        "coverage_report_digest": canonical_digest(report),
        "coverage_domain_digest": report["domain_digest"],
        "resolver_result_digests": [canonical_digest(resolved_job)],
        "base_condition_qualifications": [base],
        "robot_start_pose_qualifications": [pose],
        "allowed_pairs": [{
            "base_condition_qualification_digest": base["qualification_digest"],
            "robot_start_pose_qualification_digest": pose["qualification_digest"],
            "split_groups": ["TRAIN"],
        }],
    }, "catalog_digest")
    hypothesis = compile_fr5_hypothesis(
        fixed_contract=fixed, coverage_report=report,
        resolver_results=[resolved_job], qualification_catalog=catalog,
    )
    manifest_budget = {
        "max_physical_episodes": 1, "max_rollout_trials": 1,
        "max_hil_prompts": 1, "max_reviews": 1,
        "max_pending_reviews": 1, "max_storage_bytes": 2_147_483_648,
    }
    program_budget = {
        "max_rounds": 1, "used_rounds": 0,
        "max_total_physical_episodes": 1, "used_total_physical_episodes": 0,
        "max_total_rollout_trials": 1, "used_total_rollout_trials": 0,
        "max_total_hil_prompts": 1, "used_total_hil_prompts": 0,
        "max_total_reviews": 1, "used_total_reviews": 0,
        "max_pending_reviews": 1, "used_pending_reviews": 0,
        "max_total_storage_bytes": 2_147_483_648,
        "used_total_storage_bytes": 0,
    }
    draft = validate_campaign_draft({
        "schema_version": DRAFT_SCHEMA,
        "draft_id": draft_id, "revision": 0,
        "source": {
            "hypothesis_digest": hypothesis["hypothesis_digest"],
            "catalog_digest": hypothesis["qualification_catalog"]["catalog_digest"],
            "coverage_digest": canonical_digest(hypothesis["coverage_report"]),
        },
        "branch": "INITIAL_SEED", "selector": "BALANCED_INITIAL",
        "requested_count": 1, "normalized_seed": 0,
        "pinned": [], "excluded": [], "direct_slots": [],
        "manifest_id": manifest_id,
        "manifest_budget": manifest_budget, "program_budget": program_budget,
    }, hypothesis=hypothesis)
    return hypothesis, draft


class OperatorConsole:
    """Expose one CampaignOperator through one outer intent core and worker."""

    def __init__(
        self, *, session_id: str, operator_label: str,
        campaign_operator_factory: Callable[[Callable[..., Mapping[str, Any]]], CampaignOperator],
        episode_call: Callable[..., Mapping[str, Any]],
        projection_call: Callable[[], Mapping[str, Any]],
        test_only_paths: str, run_id: str | None = None,
        candidate_review_port: CandidateReviewPort | None = None,
        terminal_response_call: Callable[[], Mapping[str, Any] | None] | None = None,
        gripper_setup_request: Mapping[str, Any] | None = None,
        gripper_setup_resolution_call: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        initial_block_code: str | None = None,
        prepare_timeout_s: float = 5.0, close_timeout_s: float = 5.0,
        clock=None,
    ):
        if (
            not isinstance(session_id, str) or not SAFE_ID.fullmatch(session_id)
            or not isinstance(operator_label, str) or not SAFE_ID.fullmatch(operator_label)
        ):
            raise ContractError("OPERATOR_CONSOLE_ID")
        run_id = run_id or f"{session_id}-run-0"
        if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
            raise ContractError("OPERATOR_CONSOLE_RUN_ID")
        if not all(callable(call) for call in (
            campaign_operator_factory, episode_call, projection_call,
        )):
            raise ContractError("OPERATOR_CONSOLE_CALLABLE")
        if not isinstance(test_only_paths, str) or not test_only_paths or "\x00" in test_only_paths:
            raise ContractError("OPERATOR_CONSOLE_TEST_ONLY_PATHS")
        if candidate_review_port is not None and not isinstance(candidate_review_port, CandidateReviewPort):
            raise ContractError("OPERATOR_CONSOLE_CANDIDATE_PORT")
        if terminal_response_call is not None and not callable(terminal_response_call):
            raise ContractError("OPERATOR_CONSOLE_CALLABLE")
        if (
            (gripper_setup_request is None) != (gripper_setup_resolution_call is None)
            or gripper_setup_request is not None and not isinstance(gripper_setup_request, Mapping)
            or gripper_setup_resolution_call is not None and not callable(gripper_setup_resolution_call)
            or initial_block_code is not None and (
                not isinstance(initial_block_code, str)
                or not SAFE_ID.fullmatch(initial_block_code)
            )
            or gripper_setup_request is not None and initial_block_code is not None
        ):
            raise ContractError("OPERATOR_CONSOLE_SETUP")
        for value in (prepare_timeout_s, close_timeout_s):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ContractError("OPERATOR_CONSOLE_TIMEOUT")

        self.session_id, self.operator_label, self.run_id = session_id, operator_label, run_id
        self.episode_call, self.projection_call = episode_call, projection_call
        self.test_only_paths = test_only_paths
        self.prepare_timeout_s, self.close_timeout_s = float(prepare_timeout_s), float(close_timeout_s)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.terminal_response_call = terminal_response_call
        self.gripper_setup_resolution_call = gripper_setup_resolution_call
        self.button_port = ButtonDecisionPort(
            session_id=f"{session_id}-plan", operator_label=operator_label, clock=self.clock,
        )
        self.checkpoint_port = OperatorCheckpointPort(operator_label=operator_label)
        self.candidate_review_port = candidate_review_port
        self._lock = threading.RLock()
        self._prepared = threading.Event()
        self._thread = None
        self._initial_handler_active = False
        self._workflow = "BLOCKED" if initial_block_code is not None else "AUTHORING"
        self._last_error = initial_block_code
        self._measurement_outcome = (
            _measurement_for_code(initial_block_code)
            if initial_block_code is not None else "NOT_MEASURED"
        )
        self._episode_plan = self._episode_result = None
        self._cancel_requested = False
        self._plan_choice = None
        if gripper_setup_request is not None:
            self.checkpoint_port.offer(gripper_setup_request)
            self._last_error = "MAINTENANCE_APPROVAL_REQUIRED"

        self.campaign_operator = campaign_operator_factory(self._run_episode)
        if (
            not isinstance(self.campaign_operator, CampaignOperator)
            or self.campaign_operator.effect_scope != "PHYSICAL"
            or self.campaign_operator.lifecycle_action != "LIVE_COLLECT"
            or self.campaign_operator.data_disposition != "TEST_ONLY"
            or self.campaign_operator.operator_label != operator_label
        ):
            raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_OPERATOR")
        self._base_projection()
        handlers = {
            "compile_draft": self.compile_draft,
            "approve_exact_plan": self.approve_exact_plan,
            "reject_plan": self.reject_plan,
            "resolve_checkpoint": self.resolve_checkpoint,
            "cancel_session": self.cancel_session,
        }
        if candidate_review_port is not None:
            handlers["review_candidate"] = candidate_review_port.resolve
        self.core = OperatorIntentCore(
            session_id=session_id, projection_call=self.projection,
            handlers=handlers, clock=self.clock,
        )

    @property
    def bridge_core(self) -> OperatorIntentCore:
        """The sole outer core accepted by the existing LoopbackBridge."""
        return self.core

    @property
    def episode_worker(self) -> threading.Thread | None:
        return self._thread

    @property
    def session(self):
        return self.campaign_operator._session

    def _base_projection(self) -> dict[str, Any]:
        value = self.projection_call()
        if not isinstance(value, Mapping) or not BASE_PROJECTION_FIELDS <= set(value):
            raise ContractError("OPERATOR_CONSOLE_PROJECTION")
        result = copy.deepcopy(dict(value))
        setup = result["setup"]
        draft = result["draft"]
        if (
            not isinstance(setup, Mapping) or set(setup) != SETUP_FIELDS
            or setup.get("operator_label") != self.operator_label
            or not isinstance(setup.get("subsystems"), list) or not setup["subsystems"]
            or not isinstance(result["fixed_lane"], Mapping)
            or not isinstance(draft, Mapping)
            or draft.get("draft_id") != self.campaign_operator.draft["draft_id"]
            or not isinstance(draft.get("cells"), list)
            or not isinstance(result["capabilities"], list)
            or not isinstance(result["workspace_wizard"], Mapping)
            or not isinstance(result["effect_counts"], Mapping)
            or any(type(count) is not int or count < 0 for count in result["effect_counts"].values())
        ):
            raise ContractError("OPERATOR_CONSOLE_PROJECTION")
        return result

    @staticmethod
    def _campaign(value: object) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        nested = value.get("campaign")
        return copy.deepcopy(dict(nested if isinstance(nested, Mapping) else value))

    def _pending_plan(self) -> dict[str, Any] | None:
        pending = self.button_port.core.snapshot()["projection"]["pending_plan"]
        return copy.deepcopy(pending)

    def _checkpoint_projection(self) -> dict[str, Any] | None:
        pending = self.checkpoint_port.projection()
        if pending is None:
            return None
        return {
            key: copy.deepcopy(pending[key])
            for key in ("kind", "prompt", "binding_digest", "choices", "evidence")
        }

    def _available_ops(self, checkpoint, candidate) -> list[str]:
        if self._workflow == "AUTHORING":
            return ["resolve_checkpoint"] if checkpoint is not None else ["compile_draft"]
        if self._workflow == "AWAITING_APPROVAL":
            return ["approve_exact_plan", "reject_plan", "cancel_session"]
        if self._workflow == "RUNNING":
            result = ["cancel_session"]
            if checkpoint is not None:
                result.insert(0, "resolve_checkpoint")
            return result
        if candidate is not None and candidate.get("status") == "PENDING":
            return ["review_candidate"]
        return []

    def projection(self) -> dict[str, Any]:
        with self._lock:
            base = self._base_projection()
            pending = self._pending_plan()
            checkpoint = self._checkpoint_projection()
            candidate = None if self.candidate_review_port is None else self.candidate_review_port.projection()
            active = self.run_id if self._workflow in {
                "AWAITING_APPROVAL", "RUNNING", "CANCELLING",
            } else None
            runtime = {
                "workflow_state": self._workflow,
                "measurement_outcome": self._measurement_outcome,
                "reason_codes": [] if self._last_error is None else [self._last_error],
                "active_child_id": active,
            }
            if self._workflow in {"RUNNING", "CANCELLING"}:
                runtime.update({
                    "phase": "ONE_JOB" if self._workflow == "RUNNING" else "CANCEL",
                    "progress": 50 if self._workflow == "RUNNING" else 90,
                    "detail": "One injected CampaignOperator child; foreground TEST_ONLY session.",
                })
            approval = None
            if pending is not None:
                binding = pending.get("decision_binding")
                binding = binding if isinstance(binding, Mapping) else {}
                approval = {
                    "plan_digest": pending["plan_digest"],
                    "approval_scope": pending["approval_scope"],
                    "test_only_paths": self.test_only_paths,
                    "decision_binding_digest": pending["decision_binding_digest"],
                    "operator_summary": copy.deepcopy(binding.get("operator_summary")),
                    "preapproval_checklist": copy.deepcopy(
                        binding.get("preapproval_checklist")
                    ),
                    "site_confirmation_digest": binding.get("site_confirmation_digest"),
                }
            base.update({
                "connection_state": "READY", "effect_scope": "PHYSICAL",
                "lifecycle_action": "LIVE_COLLECT", "data_disposition": "TEST_ONLY",
                "available_ops": self._available_ops(checkpoint, candidate),
                "operator_checkpoint": checkpoint,
                "candidate_review": candidate,
                "candidate_review_status": "NOT_APPLICABLE" if candidate is None else candidate["status"],
                "runtime": runtime, "approval": approval,
                "episode_plan": copy.deepcopy(self._episode_plan),
                "episode_result": copy.deepcopy(self._episode_result),
                "campaign_operator": self.campaign_operator.projection(),
                "campaign_session": None if self.session is None else self.session.status(),
                "operator_identity": self.operator_label,
                "human_semantic": "NOT_MEASURED",
            })
            return base

    def _owner_transition(self, change: Callable[[], None]) -> None:
        with self._lock:
            initial = self._initial_handler_active
        if initial:
            with self._lock:
                change()
                self._prepared.set()
        else:
            self.core.transition(change)

    def _publish_plan(self, pending: Mapping[str, Any]) -> None:
        def change():
            plan = copy.deepcopy(dict(pending["decision_binding"]))
            plan["decision_binding_digest"] = pending["decision_binding_digest"]
            self._episode_plan = plan
            self._workflow, self._last_error = "AWAITING_APPROVAL", None

        self._owner_transition(change)

    def _decision_provider(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        if (
            not isinstance(request, Mapping) or set(request) != PLAN_REQUEST_FIELDS
            or request.get("schema_version") != "data_factory.plan_decision_request.v1"
        ):
            raise ContractError("OPERATOR_CONSOLE_PLAN_REQUEST")
        offered = self.button_port.offer(
            run_id=request["run_id"], plan_digest=request["plan_digest"],
            decision_binding=request["decision_binding"],
            approval_scope=request["approval_scope"],
        )
        pending = offered["projection"]["pending_plan"]
        self._publish_plan(pending)
        return self.button_port.wait(request["timeout_s"])

    def _checkpoint_provider(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        def change():
            self.checkpoint_port.offer(request)
            self._workflow, self._last_error = "RUNNING", None

        self._owner_transition(change)
        return self.checkpoint_port.wait(request["timeout_s"])

    def _run_episode(self, intent, lifecycle, cancel_event, episode_context):
        return self.episode_call(
            intent, lifecycle, cancel_event, episode_context,
            self._decision_provider, self._checkpoint_provider,
        )

    def _clear_pending_plan(self) -> None:
        pending = self._pending_plan()
        if pending is not None:
            try:
                self._consume_button("CANCEL")
            except ContractError:
                pass

    def _publish_outcome(self, outcome: Mapping[str, Any]) -> None:
        self._clear_pending_plan()
        self.checkpoint_port.close()
        campaign = self._campaign(outcome.get("campaign"))
        result = outcome.get("result") if isinstance(outcome.get("result"), Mapping) else {}
        technical = result.get("technical_evidence") if isinstance(result, Mapping) else None
        terminal = (
            self.terminal_response_call()
            if self.terminal_response_call is not None else None
        )
        terminal = copy.deepcopy(dict(terminal)) if isinstance(terminal, Mapping) else None
        terminal_data = (
            terminal.get("data")
            if isinstance(terminal, Mapping) and isinstance(terminal.get("data"), Mapping)
            else {}
        )
        if outcome.get("ok") is True:
            name, code, workflow = "PASS", "TECHNICAL_PASS", "TERMINAL"
            self._measurement_outcome = "PASS"
            self._last_error = None
        elif terminal is not None and terminal.get("ok") is False:
            code = terminal.get("code") if isinstance(terminal.get("code"), str) else "OPERATOR_CONSOLE_EPISODE"
            measured = terminal_data.get("measurement_outcome")
            if measured not in {"PASS", "FAIL", "NOT_AVAILABLE", "NOT_MEASURED"}:
                measured = "NOT_MEASURED" if terminal.get("state") == "CANCELLED" else "FAIL"
            self._measurement_outcome = measured
            if code == "PAUSED_AWAITING_OPERATOR":
                name, workflow = "PAUSED", "PAUSED_AWAITING_OPERATOR"
            elif terminal.get("state") == "CANCELLED":
                name, workflow = "CANCEL", "TERMINAL"
            elif measured == "NOT_AVAILABLE":
                name, workflow = "NOT_AVAILABLE", "BLOCKED"
            else:
                name, workflow = "FAIL", "BLOCKED"
            self._last_error = code
        elif self._plan_choice == "REJECT":
            name, code, workflow = "REJECT", "PLAN_REJECTED", "BLOCKED"
            self._measurement_outcome = "NOT_MEASURED"
            self._last_error = code
        elif self._cancel_requested or campaign and campaign.get("state") == "CANCELLED":
            name, code, workflow = "CANCEL", "PLAN_CANCELLED", "TERMINAL"
            self._measurement_outcome = "NOT_MEASURED"
            self._last_error = code
        else:
            name = "FAIL"
            code = outcome.get("code") if isinstance(outcome.get("code"), str) else "OPERATOR_CONSOLE_EPISODE"
            self._measurement_outcome = _measurement_for_code(code)
            workflow, self._last_error = "BLOCKED", code
        sealed = {
            "outcome": name, "code": code,
            "technical_evidence": copy.deepcopy(
                technical if technical is not None else terminal_data.get("technical_validator")
            ),
            "campaign": campaign,
            "human_semantic": result.get(
                "human_semantic", terminal_data.get("human_semantic_outcome", "NOT_MEASURED"),
            ),
        }
        sealed["result_digest"] = canonical_digest(sealed)
        self._episode_result, self._workflow = sealed, workflow
        self._prepared.set()

    def _worker_target(self) -> None:
        try:
            outcome = self.campaign_operator.run_next({"run_id": self.run_id}, {})
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
        else:
            self.core.transition(lambda: self._publish_outcome(outcome))

    def compile_draft(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        expected = {
            "draft_id": self.campaign_operator.draft["draft_id"],
            "data_disposition": "TEST_ONLY",
        }
        with self._lock:
            if (
                payload != expected or self._workflow != "AUTHORING"
                or self._thread is not None or self._checkpoint_projection() is not None
            ):
                raise ContractError("OPERATOR_CONSOLE_COMPILE_FIELDS")
            if self.campaign_operator.manifest is None:
                self.campaign_operator.compile_draft({}, {})
            self._workflow, self._last_error = "RUNNING", None
            self._initial_handler_active = True
            self._thread = threading.Thread(
                target=self._worker_target, name=f"operator-console-{self.run_id}",
                daemon=False,
            )
            self._thread.start()
        ready = self._prepared.wait(self.prepare_timeout_s)
        with self._lock:
            ready = ready or self._prepared.is_set()
            self._initial_handler_active = False
            if ready and self._workflow == "AWAITING_APPROVAL":
                return {
                    "outcome": "AWAITING_APPROVAL",
                    "episode_plan": copy.deepcopy(self._episode_plan),
                }
            if ready:
                checkpoint = self._checkpoint_projection()
                if checkpoint is not None:
                    return {
                        "outcome": "AWAITING_CHECKPOINT",
                        "operator_checkpoint": checkpoint,
                    }
                return copy.deepcopy(self._episode_result)
            return {"outcome": "RUNNING", "active_child_id": self.run_id}

    def _button_intent(self, snapshot, op, digest, choice) -> dict[str, Any]:
        return {
            "schema_version": INTENT_SCHEMA,
            "intent_id": f"{self.session_id}-button-{choice.lower()}",
            "session_id": snapshot["session_id"],
            "view_revision": snapshot["revision"],
            "view_digest": snapshot["view_digest"], "op": op,
            "payload": {"decision_binding_digest": digest},
        }

    def _consume_button(self, choice: str) -> dict[str, Any]:
        snapshot = self.button_port.core.snapshot()
        pending = snapshot["projection"]["pending_plan"]
        if pending is None:
            raise ContractError("OPERATOR_CONSOLE_PLAN_STATE")
        op = {
            "APPROVE": "approve_exact_plan", "REJECT": "reject_plan", "CANCEL": "cancel_plan",
        }[choice]
        return self.button_port.core.consume(self._button_intent(
            snapshot, op, pending["decision_binding_digest"], choice,
        ))["result"]

    def _plan_payload(self) -> dict[str, Any]:
        pending = self._pending_plan()
        if pending is None:
            raise ContractError("OPERATOR_CONSOLE_PLAN_STATE")
        return {
            "plan_digest": pending["plan_digest"],
            "approval_scope": pending["approval_scope"],
            "data_disposition": "TEST_ONLY",
        }

    def approve_exact_plan(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._workflow != "AWAITING_APPROVAL" or payload != self._plan_payload():
                raise ContractError("OPERATOR_CONSOLE_PLAN_DIGEST_MISMATCH")
            decision = self._consume_button("APPROVE")
            self._plan_choice = "APPROVE"
            self._workflow = "RUNNING"
            return {"outcome": "RUNNING", "active_child_id": self.run_id, "decision": decision}

    def reject_plan(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._workflow != "AWAITING_APPROVAL" or payload != self._plan_payload():
                raise ContractError("OPERATOR_CONSOLE_PLAN_DIGEST_MISMATCH")
            decision = self._consume_button("REJECT")
            self._plan_choice = "REJECT"
            self._workflow = "CANCELLING"
            return {"outcome": "REJECT", "decision": decision}

    def resolve_checkpoint(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            pending = self._checkpoint_projection()
            if (
                self._workflow == "AUTHORING"
                and pending is not None
                and pending.get("kind") == "GRIPPER_MAINTENANCE"
                and self.gripper_setup_resolution_call is not None
            ):
                decision = self.checkpoint_port.resolve(payload)
                consumed = self.checkpoint_port.wait(0)
                if consumed != decision:
                    raise ContractError("OPERATOR_CONSOLE_SETUP")
                if decision["choice"] == "CANCEL":
                    self._workflow = "PAUSED_AWAITING_OPERATOR"
                    self._measurement_outcome = "NOT_MEASURED"
                    self._last_error = "GRIPPER_MAINTENANCE_CANCELLED"
                    return {"outcome": "PAUSED", "measurement_outcome": "NOT_MEASURED"}
                try:
                    result = self.gripper_setup_resolution_call(copy.deepcopy(decision))
                except ContractError as exc:
                    self._last_error = exc.code
                    if exc.code == "GRIPPER_NORMAL_GRAPH_REQUIRED":
                        self._workflow = "PAUSED_AWAITING_OPERATOR"
                        self._measurement_outcome = "NOT_MEASURED"
                        return {
                            "outcome": "PAUSED",
                            "measurement_outcome": "NOT_MEASURED",
                            "code": exc.code,
                        }
                    self._workflow = "BLOCKED"
                    self._measurement_outcome = (
                        "NOT_AVAILABLE" if exc.code.endswith("NOT_AVAILABLE") else "FAIL"
                    )
                    return {
                        "outcome": "BLOCKED",
                        "measurement_outcome": self._measurement_outcome,
                        "code": exc.code,
                    }
                if not isinstance(result, Mapping) or result.get("state") != "ATTACHED":
                    self._workflow = "BLOCKED"
                    self._measurement_outcome = "FAIL"
                    self._last_error = "GRIPPER_MAINTENANCE_RECHECK"
                    return {
                        "outcome": "BLOCKED", "measurement_outcome": "FAIL",
                        "code": self._last_error,
                    }
                self._last_error = None
                return {"outcome": "READY", "gripper_setup": copy.deepcopy(dict(result))}
            if self._workflow != "RUNNING":
                raise ContractError("OPERATOR_CONSOLE_CHECKPOINT_STATE")
            return self.checkpoint_port.resolve(payload)

    def _cancel_owner(self) -> dict[str, Any] | None:
        self._cancel_requested = True
        self._plan_choice = "CANCEL"
        self._clear_pending_plan()
        self.checkpoint_port.close()
        try:
            return self.campaign_operator.cancel_campaign({}, {})
        except ContractError:
            return None

    def cancel_session(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if (
                self._workflow not in {"AWAITING_APPROVAL", "RUNNING"}
                or payload != {"active_child_id": self.run_id}
            ):
                raise ContractError("OPERATOR_CONSOLE_CANCEL_BINDING")
            cancelled = self._cancel_owner()
            self._workflow = "CANCELLING"
            return {"outcome": "CANCELLING", "campaign": copy.deepcopy(cancelled)}

    def offer_candidate_review(self, **kwargs) -> dict[str, Any]:
        """Backend-only offer; browser review payload never includes a path."""
        if self.candidate_review_port is None:
            raise ContractError("OPERATOR_CONSOLE_CANDIDATE_NOT_APPLICABLE")
        offered = None

        def change():
            nonlocal offered
            offered = self.candidate_review_port.offer(**kwargs)

        self.core.transition(change)
        return copy.deepcopy(offered)

    def wait_for_episode(self, timeout_s: float | None = None) -> dict[str, Any] | None:
        thread = self._thread
        if thread is not None:
            thread.join(self.close_timeout_s if timeout_s is None else timeout_s)
        with self._lock:
            return copy.deepcopy(self._episode_result)

    def close(self) -> None:
        thread = self._thread
        if thread is None or not thread.is_alive():
            self.checkpoint_port.close()
            return
        with self._lock:
            self._cancel_owner()
        thread.join(self.close_timeout_s)
        if thread.is_alive():
            raise ContractError("OPERATOR_CONSOLE_THREAD_LEAK")


def discover_uvc_device_ids(device_root: str | Path = "/dev/v4l/by-id") -> list[str]:
    """Return stable index-0 UVC basenames without opening a camera."""
    root = Path(device_root)
    if root.is_symlink() or not root.is_dir():
        return []
    result = []
    for path in sorted(root.glob("*-video-index0"), key=lambda item: item.name):
        try:
            target = path.resolve(strict=True)
            mode = target.stat().st_mode
        except OSError:
            continue
        if path.is_symlink() and stat.S_ISCHR(mode) and "realsense" not in path.name.lower():
            result.append(path.name)
    return result


def _camera_binding(
    repository: Path, profile: Mapping[str, Any], *, selected_device_id: str | None,
    discovery_call: Callable[[], list[str]],
) -> dict[str, Any]:
    discovered = discovery_call()
    receipt_path = repository / "outputs/data_factory/operator_setup/camera_binding.json"
    if receipt_path.is_file() and selected_device_id is None:
        return reuse_camera_binding_receipt(
            load_camera_binding_receipt(repository_root=repository),
            discovered_device_ids=discovered, collection_profile=profile,
        )
    binding = build_camera_binding_from_discovery(
        binding_id="goal2-up-camera-r001", device_kind="UVC",
        discovered_device_ids=discovered, selected_device_id=selected_device_id,
        intended_role="up", collection_profile=profile,
    )
    write_camera_binding_receipt(binding, repository_root=repository)
    return binding


def _bounded_command(command: list[str], code: str, *, timeout_s: float = 5) -> str:
    try:
        completed = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout_s, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(code) from exc
    if completed.returncode != 0:
        raise ContractError(code)
    return completed.stdout


def _readonly_command(command: list[str], code: str) -> str:
    return _bounded_command(command, code)


def _controller_names(value: str) -> set[str]:
    return {
        fields[0]
        for line in value.splitlines()
        if (fields := line.split()) and "active" in fields
    }


def _remote_gripper_command(command: str, *, expected_fields: int) -> list[int]:
    output = _bounded_command([
        "ros2", "service", "call", "/fairino_remote_command_service",
        "fairino_msgs/srv/RemoteCmdInterface",
        json.dumps({"cmd_str": command}, separators=(",", ":")),
    ], "GRIPPER_MAINTENANCE_SERVICE", timeout_s=35)
    match = re.search(r"cmd_res(?:=|:)\s*['\"]?(-?\d+(?:,-?\d+)*)", output)
    if match is None:
        raise ContractError("GRIPPER_MAINTENANCE_RESPONSE")
    result = [int(value) for value in match.group(1).split(",")]
    if len(result) != expected_fields:
        raise ContractError("GRIPPER_MAINTENANCE_RESPONSE")
    return result


def capture_gripper_setup_readback() -> dict[str, Any]:
    """Read one fresh gripper source without opening a second SDK owner."""
    nodes = set(
        line.strip()
        for line in _readonly_command(
            ["ros2", "node", "list"], "GRIPPER_SETUP_NODE_GRAPH",
        ).splitlines()
        if line.strip()
    )
    controllers = _controller_names(_readonly_command(
        ["ros2", "control", "list_controllers"],
        "GRIPPER_SETUP_CONTROLLER_GRAPH",
    ))
    normal = {
        "fairino5_controller", "gripper_controller", "joint_state_broadcaster",
    } <= controllers
    command_server = "/fr_command_server" in nodes
    if normal and command_server:
        raise ContractError("PHYSICAL_SECOND_MOTION_OWNER")
    if normal:
        output = _readonly_command([
            "ros2", "topic", "echo", "/gripper_controller/controller_state",
            "control_msgs/msg/JointTrajectoryControllerState", "--once",
            "--timeout", "2", "--flow-style",
        ], "GRIPPER_SETUP_READBACK")
        try:
            import yaml
            message = next(yaml.safe_load_all(output))
            names = message["joint_names"]
            reference = message["reference"]["positions"]
            feedback = message["feedback"]["positions"]
            if names != ["finger_right_joint"] or len(reference) != 1 or len(feedback) != 1:
                raise ValueError
            reference_m, feedback_m = float(reference[0]), float(feedback[0])
        except (ImportError, KeyError, StopIteration, TypeError, ValueError) as exc:
            raise ContractError("GRIPPER_SETUP_READBACK") from exc
        if not all(math.isfinite(value) for value in (reference_m, feedback_m)):
            raise ContractError("GRIPPER_SETUP_READBACK")
        return {
            "active": True, "position_valid": True, "gripper_index": 1,
            "reference_position_m": reference_m,
            "feedback_position_m": feedback_m,
            "sample_age_s": 0.0, "max_age_s": 0.1,
            "source": "CONTROLLER_STATE",
        }
    if command_server and not controllers:
        activation = _remote_gripper_command(
            "GetGripperActivateStatus()", expected_fields=3,
        )
        position = _remote_gripper_command(
            "GetGripperCurPosition()", expected_fields=3,
        )
        active = activation[0] == 0 and activation[1] == 0 and activation[2] & 1 == 1
        valid = position[0] == 0 and position[1] == 0 and 0 <= position[2] <= 100
        position_m = 0.021 * position[2] / 100 if valid else None
        return {
            "active": active, "position_valid": valid, "gripper_index": 1,
            "reference_position_m": position_m,
            "feedback_position_m": position_m,
            "sample_age_s": 0.0, "max_age_s": 0.1,
            "source": "COMMAND_SERVER_MAINTENANCE",
        }
    raise ContractError("GRIPPER_SETUP_NOT_AVAILABLE")


def normalize_gripper_after_operator_ready(readback: Mapping[str, Any]) -> dict[str, Any]:
    """Perform the one approved open-normalization branch; never manage processes."""
    source = readback.get("source") if isinstance(readback, Mapping) else None
    if source == "CONTROLLER_STATE":
        goal = {
            "trajectory": {
                "joint_names": ["finger_right_joint"],
                "points": [{
                    "positions": [0.021],
                    "time_from_start": {"sec": 2, "nanosec": 0},
                }],
            },
            "goal_tolerance": [{"name": "finger_right_joint", "position": 0.000105}],
            "goal_time_tolerance": {"sec": 5, "nanosec": 0},
        }
        output = _bounded_command([
            "ros2", "action", "send_goal",
            "/gripper_controller/follow_joint_trajectory",
            "control_msgs/action/FollowJointTrajectory",
            json.dumps(goal, separators=(",", ":")),
        ], "GRIPPER_MAINTENANCE_ACTION", timeout_s=15)
        if (
            "Goal finished with status: SUCCEEDED" not in output
            or re.search(r"error_code(?:=|:)\s*0\b", output) is None
        ):
            raise ContractError("GRIPPER_MAINTENANCE_ACTION")
        return {"status": "NORMALIZED", "requires_graph_switch": False}
    if source == "COMMAND_SERVER_MAINTENANCE":
        if readback.get("active") is not True:
            for command in ("ActGripper(1,0)", "ActGripper(1,1)"):
                if _remote_gripper_command(command, expected_fields=1) != [0]:
                    raise ContractError("GRIPPER_MAINTENANCE_ACTION")
        if _remote_gripper_command("MoveGripper(1,100)", expected_fields=1) != [0]:
            raise ContractError("GRIPPER_MAINTENANCE_ACTION")
        done = _remote_gripper_command("GetGripperMotionDone()", expected_fields=3)
        position = _remote_gripper_command("GetGripperCurPosition()", expected_fields=3)
        if done != [0, 0, 1] or position != [0, 0, 100]:
            raise ContractError("GRIPPER_MAINTENANCE_ACTION")
        return {"status": "NORMALIZED", "requires_graph_switch": True}
    raise ContractError("GRIPPER_MAINTENANCE_NOT_AVAILABLE")


def passive_physical_gate(
    *, camera_topic: str, discovered_device_id: str,
    camera_node: str = "/camera/up/color/uvc_up_camera",
    device_root: str | Path = "/dev/v4l/by-id",
    discovery_call: Callable[[], list[str]] = discover_uvc_device_ids,
) -> dict[str, Any]:
    """Attach only to an already-running graph; perform no lifecycle mutation."""
    if discovery_call().count(discovered_device_id) != 1:
        raise ContractError("PHYSICAL_CAMERA_BINDING")
    stable_path = Path(device_root) / discovered_device_id
    try:
        stable_target = stable_path.resolve(strict=True)
    except OSError as exc:
        raise ContractError("PHYSICAL_CAMERA_BINDING") from exc
    if not stable_path.is_symlink() or not stat.S_ISCHR(stable_target.stat().st_mode):
        raise ContractError("PHYSICAL_CAMERA_BINDING")
    controllers = _readonly_command(
        ["ros2", "control", "list_controllers"], "PHYSICAL_CONTROLLER_GRAPH",
    )
    for name in ("fairino5_controller", "gripper_controller", "joint_state_broadcaster"):
        if not any(line.split()[:1] == [name] and "active" in line.split() for line in controllers.splitlines()):
            raise ContractError("PHYSICAL_CONTROLLER_STATE_MISMATCH")
    nodes = _readonly_command(["ros2", "node", "list"], "PHYSICAL_NODE_GRAPH")
    if "/fr_command_server" in nodes.splitlines():
        raise ContractError("PHYSICAL_SECOND_MOTION_OWNER")
    if _readonly_command(
        ["ros2", "topic", "type", "/joint_states"], "PHYSICAL_JOINT_TOPIC",
    ).strip() != "sensor_msgs/msg/JointState":
        raise ContractError("PHYSICAL_JOINT_TOPIC_MISMATCH")
    if _readonly_command(
        ["ros2", "topic", "type", camera_topic], "PHYSICAL_CAMERA_TOPIC",
    ).strip() != "sensor_msgs/msg/Image":
        raise ContractError("PHYSICAL_CAMERA_TOPIC_MISMATCH")
    configured_device = _readonly_command(
        ["ros2", "param", "get", camera_node, "video_device", "--hide-type"],
        "PHYSICAL_CAMERA_DEVICE_PARAMETER",
    ).strip()
    try:
        configured_target = Path(configured_device).resolve(strict=True)
    except OSError as exc:
        raise ContractError("PHYSICAL_CAMERA_DEVICE_MISMATCH") from exc
    if configured_target != stable_target:
        raise ContractError("PHYSICAL_CAMERA_DEVICE_MISMATCH")
    evidence = {
        "schema_version": "data_factory.test_only_camera_transport_binding.v1",
        "stable_device_id": discovered_device_id,
        "resolved_device": str(stable_target),
        "camera_node": camera_node,
        "camera_topic": camera_topic,
        "reported_video_device": configured_device,
        "topic_type": "sensor_msgs/msg/Image",
        "authority": "TEST_ONLY_TRANSPORT",
    }
    evidence["binding_digest"] = canonical_digest(evidence)
    return evidence


def capture_home_snapshot(
    *, tcp_candidate_manifest: Path, max_age_s: float = 0.1,
) -> dict[str, Any]:
    """Invoke the existing read-only ROS snapshot command once."""
    output = _readonly_command([
        sys.executable, "-m", "tools.data_factory.motion.pose_snapshot", "capture",
        "--timeout-s", "2", "--max-age-s", str(max_age_s),
        "--tcp-candidate-manifest", str(tcp_candidate_manifest),
    ], "PHYSICAL_HOME_SNAPSHOT")
    return load_json_strict(output.strip())


def _repository_path(repository: Path, value: str | Path) -> Path:
    path = Path(value)
    path = (repository / path).resolve(strict=True) if not path.is_absolute() else path.resolve(strict=True)
    try:
        path.relative_to(repository)
    except ValueError as exc:
        raise ContractError("PHYSICAL_CONSOLE_PATH") from exc
    return path


def build_physical_operator_console(
    *, repository_root: str | Path, session_id: str, run_id: str,
    operator_label: str, job_path: str | Path = DEFAULT_JOB,
    selected_sheet: str | Path = DEFAULT_YAW0,
    yaw0_sheet: str | Path = DEFAULT_YAW0,
    motion_qualification_path: str | Path = DEFAULT_MOTION,
    home_candidate_path: str | Path = DEFAULT_HOME,
    urdf_path: str | Path = DEFAULT_URDF,
    tcp_candidate_manifest: str | Path = DEFAULT_TCP_MANIFEST,
    selected_camera_device_id: str | None = None,
    discovery_call: Callable[[], list[str]] = discover_uvc_device_ids,
    activation_call: Callable[[], bool] | None = None,
    snapshot_call: Callable[[], Mapping[str, Any]] | None = None,
    gripper_readback_call: Callable[[], Mapping[str, Any]] | None = None,
    gripper_maintenance_call: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    run_live_call: Callable[..., Mapping[str, Any]] = run_job.run_live,
    clock=None,
) -> tuple[OperatorConsole, dict[str, Any]]:
    """Compose the exact HOME↔place1 TEST_ONLY caller without activating hardware."""
    repository = Path(repository_root).resolve(strict=True)
    clock = clock or (lambda: datetime.now(timezone.utc))
    paths = {
        "job": _repository_path(repository, job_path),
        "selected_sheet": _repository_path(repository, selected_sheet),
        "yaw0_sheet": _repository_path(repository, yaw0_sheet),
        "motion": _repository_path(repository, motion_qualification_path),
        "home": _repository_path(repository, home_candidate_path),
        "urdf": _repository_path(repository, urdf_path),
        "tcp": _repository_path(repository, tcp_candidate_manifest),
    }
    job = load_json_strict(paths["job"])
    job["operator_or_agent_id"] = operator_label
    payload = {
        "mode": "live", "run_id": run_id, "job": job,
        "selected_sheet": str(paths["selected_sheet"]),
        "yaw0_sheet": str(paths["yaw0_sheet"]),
        "config_root": str(repository / "config/data_factory"),
        "motion_qualification": str(paths["motion"]),
        "home_candidate": str(paths["home"]), "urdf": str(paths["urdf"]),
        "expected_robot_system_id": "fr5-lab-a", "camera_profile": "up",
    }
    resolved, program, _ = run_job.resolve_inputs(
        payload, scene_binding_call=lambda *_args: {},
    )
    if (
        resolved["normalized_job"].get("place_id") != "PLACE_A"
        or resolved["normalized_job"].get("yaw_deg") != 0
        or resolved["normalized_job"].get("x_mm") != 0
        or resolved["normalized_job"].get("y_mm") != 0
        or program.get("schema_version") != "fr5.motion_program.v2"
        or len(program.get("steps", [])) != 10
    ):
        raise ContractError("PHYSICAL_CONSOLE_EXACT_SCOPE")
    profile = resolved["collection_profile"]
    camera_binding = _camera_binding(
        repository, profile, selected_device_id=selected_camera_device_id,
        discovery_call=discovery_call,
    )
    roots = build_test_only_root_binding(
        repository, session_id=session_id, run_id=run_id,
    )
    payload.update(run_root=roots["run_root"], dataset_root=roots["dataset_root"])
    state_initialization = initialize_test_only_state_from_user_declaration(
        roots, repository_root=repository,
        robot_system_id=resolved["normalized_job"]["robot_system_id"],
        object_instance_id="goal2-wood-cube-r001",
        object_profile_id=resolved["normalized_job"]["object_profile_id"],
        place_id="PLACE_A", yaw_deg=0, x_mm=0, y_mm=0,
        declared_by=operator_label,
    )
    motion_qualification = load_json_strict(paths["motion"])
    home_candidate = load_json_strict(paths["home"])
    hypothesis, draft = build_physical_test_contract(
        resolved_job=resolved, motion_qualification=motion_qualification,
        home_candidate=home_candidate,
        scene_digest=state_initialization["scene_state_digest"],
        draft_id=f"{session_id}-draft", manifest_id=f"{session_id}-manifest",
    )
    counters = {name: 0 for name in SIDE_EFFECT_COUNTERS}
    holder: dict[str, Any] = {}
    read_gripper = gripper_readback_call or capture_gripper_setup_readback
    maintain_gripper = gripper_maintenance_call or normalize_gripper_after_operator_ready

    def refresh_gripper() -> dict[str, Any]:
        try:
            readback = read_gripper()
            projection = gripper_setup_projection(readback)
        except ContractError as exc:
            holder["gripper_setup_error"] = exc.code
            holder["gripper_readback"] = None
            projection = gripper_setup_projection(None)
        projection["maintenance_call_count"] = (
            holder.get("gripper_projection", {}).get("maintenance_call_count", 0)
        )
        holder["gripper_projection"] = copy.deepcopy(projection)
        if projection["state"] != "NOT_AVAILABLE":
            holder["gripper_setup_error"] = None
            holder["gripper_readback"] = copy.deepcopy(dict(readback))
        return copy.deepcopy(projection)

    initial_gripper = refresh_gripper()
    setup_request = None
    initial_block_code = None
    if initial_gripper["state"] == "MAINTENANCE_APPROVAL_REQUIRED":
        setup_binding_digest = canonical_digest({
            "schema_version": "data_factory.gripper_setup_binding.v1",
            "run_id": run_id,
            "readback_digest": initial_gripper["readback_digest"],
            "operation": initial_gripper["supported_action"],
            "gripper_index": 1, "authority": "SETUP_ONLY",
        })
        setup_request = {
            "schema_version": "data_factory.operator_checkpoint_request.v1",
            "kind": "GRIPPER_MAINTENANCE", "run_id": run_id,
            "plan_digest": setup_binding_digest,
            "prompt": (
                "Confirm the gripper is empty and physically clear before one "
                "TEST_ONLY open-normalization action."
            ),
            "choices": ["READY", "CANCEL"],
            "evidence": {
                "setup_only": True, "plan_exists": False,
                "gripper_index": 1,
                "operation": initial_gripper["supported_action"],
                "readback_digest": initial_gripper["readback_digest"],
                "empty_gripper": "OPERATOR_CONFIRM_REQUIRED",
                "finger_and_cell_clear": "OPERATOR_CONFIRM_REQUIRED",
                "authority": "SETUP_ONLY",
            },
            "timeout_s": None,
        }
    elif initial_gripper["state"] != "ATTACHED":
        initial_block_code = (
            "GRIPPER_BINDING_MISMATCH"
            if initial_gripper["state"] == "BLOCKED_BINDING"
            else holder.get("gripper_setup_error") or "GRIPPER_SETUP_NOT_AVAILABLE"
        )

    def scene_evidence(_run_id: str) -> dict[str, Any]:
        snapshot = SceneStateStore(
            roots["cell_root"], resolved["normalized_job"]["robot_system_id"],
        ).snapshot()
        value = {
            "schema_version": "data_factory.scene_freshness_evidence.v1",
            "scene_digest": snapshot["scene_state_digest"],
            "observed_at": clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        value["evidence_digest"] = canonical_digest(value)
        return value

    def unused_port(_request):
        raise ContractError("PHYSICAL_CONSOLE_PORT_NOT_ATTACHED")

    def fresh_one_job() -> OneJob:
        counters["physical_factory"] += 1
        return OneJob(
            unused_port, unused_port,
            readiness_contract=TEST_ONLY_READINESS_CONTRACT,
            allow_synthetic_test_operator=True,
        )

    def resolve_gripper_setup(_decision: Mapping[str, Any]) -> dict[str, Any]:
        current = refresh_gripper()
        if current["state"] == "ATTACHED":
            holder["operator"].subsystems["gripper"] = {
                "readiness": "READY", "capability": "ATTACH",
                "reason": "FRESH_CONTROLLER_READBACK",
            }
            return current
        if (
            current["state"] != "MAINTENANCE_APPROVAL_REQUIRED"
            or current.get("readback_digest") != initial_gripper.get("readback_digest")
        ):
            raise ContractError("GRIPPER_MAINTENANCE_STALE")
        counters["gripper"] += 1
        result = maintain_gripper(copy.deepcopy(holder["gripper_readback"]))
        if (
            not isinstance(result, Mapping)
            or result.get("status") != "NORMALIZED"
            or type(result.get("requires_graph_switch")) is not bool
        ):
            raise ContractError("GRIPPER_MAINTENANCE_ACTION")
        if result["requires_graph_switch"]:
            holder["gripper_projection"] = {
                **current, "state": "NORMAL_GRAPH_REQUIRED",
                "supported_action": "RESTART_FOREGROUND_NORMAL_GRAPH",
                "maintenance_call_count": 1,
            }
            raise ContractError("GRIPPER_NORMAL_GRAPH_REQUIRED")
        refreshed = refresh_gripper()
        if refreshed["state"] != "ATTACHED":
            raise ContractError("GRIPPER_MAINTENANCE_RECHECK")
        refreshed["maintenance_call_count"] = 1
        holder["gripper_projection"] = copy.deepcopy(refreshed)
        holder["operator"].subsystems["gripper"] = {
            "readiness": "READY", "capability": "ATTACH",
            "reason": "APPROVED_OPEN_NORMALIZATION",
        }
        return refreshed

    def activate() -> bool:
        gripper = refresh_gripper()
        if gripper["state"] != "ATTACHED":
            raise ContractError("GRIPPER_SETUP_NOT_AVAILABLE")
        value = (
            activation_call() if activation_call is not None
            else passive_physical_gate(
                camera_topic=profile["camera_topics"]["up"],
                discovered_device_id=camera_binding["stable_device_id"],
                discovery_call=discovery_call,
            )
        )
        if value is True:
            evidence = {
                "schema_version": "data_factory.test_only_camera_transport_binding.v1",
                "stable_device_id": camera_binding["stable_device_id"],
                "status": "INJECTED_TEST_GATE",
            }
            evidence["binding_digest"] = canonical_digest(evidence)
        elif isinstance(value, Mapping):
            evidence = copy.deepcopy(dict(value))
            if (
                evidence.get("stable_device_id") != camera_binding["stable_device_id"]
                or evidence.get("binding_digest")
                != canonical_digest({
                    key: item for key, item in evidence.items()
                    if key != "binding_digest"
                })
            ):
                raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
        else:
            raise ContractError("CAMPAIGN_OPERATOR_PHYSICAL_ACTIVATION_FAILED")
        holder["camera_transport_evidence"] = evidence
        return True

    def start_binding(_run_id: str) -> dict[str, Any]:
        snapshot = (
            snapshot_call() if snapshot_call is not None
            else capture_home_snapshot(tcp_candidate_manifest=paths["tcp"])
        )
        return build_test_only_start_binding(
            manifest=holder["operator"].manifest, hypothesis=hypothesis,
            motion_qualification=motion_qualification,
            home_candidate=home_candidate, current_snapshot=snapshot,
        )

    def episode(
        intent, lifecycle, cancel_event, episode_context,
        decision_provider, checkpoint_provider,
    ):
        episode_binding = build_test_only_episode_binding(
            roots=episode_context["root_binding"], repository_root=repository,
            manifest=holder["operator"].manifest, hypothesis=hypothesis,
            intent=intent, start_binding=episode_context["start_binding"],
            state_initialization=state_initialization, resolved_job=resolved,
            place_alias="place1",
        )
        transport = holder.get("camera_transport_evidence")
        if not isinstance(transport, Mapping):
            raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
        checklist = {
            "schema_version": "data_factory.goal2_site_checklist.v1",
            "place_alias": "place1", "place_id": "PLACE_A",
            "cell_calibration_id": resolved["normalized_job"]["cell_calibration_id"],
            "yaw_deg": 0, "x_mm": 0, "y_mm": 0,
            "object_profile_id": resolved["normalized_job"]["object_profile_id"],
            "grasp_profile_id": resolved["normalized_job"]["grasp_profile_id"],
            "task": "pickup_e2e", "motion_recipe": "DIRECT",
            "full_return_step_count": 10,
            "robot_start_pose_id": "test-only-home-r001",
            "gripper_empty": "OPERATOR_CONFIRM_REQUIRED",
            "cell_clear": "OPERATOR_CONFIRM_REQUIRED",
            "estop_monitoring": "OPERATOR_CONFIRM_REQUIRED",
            "camera_state": "CONNECTED_UNPLACED",
            "camera_profile_id": profile["collection_profile_id"],
            "camera_transport_binding_digest": transport["binding_digest"],
            "episode_limit": 1, "data_disposition": "TEST_ONLY",
        }
        holder["last_live_response"] = None
        live = run_live_call(
            copy.deepcopy(payload), cancel_event, lambda _event: None,
            one_job=lifecycle, decision_provider=decision_provider,
            checkpoint_provider=checkpoint_provider,
            approval_scope="HIL_NUMERIC_PROXY",
            test_only_root_binding=episode_context["root_binding"],
            test_only_episode_binding=episode_binding,
            test_only_start_binding=episode_context["start_binding"],
            preapproval_checklist=checklist,
            candidate_writer_enabled=False, repository_root=repository,
        )
        if not isinstance(live, Mapping) or live.get("ok") is not True:
            holder["last_live_response"] = (
                copy.deepcopy(dict(live)) if isinstance(live, Mapping) else None
            )
            code = live.get("code") if isinstance(live, Mapping) else "PHYSICAL_CONSOLE_LIVE"
            raise ContractError(code if isinstance(code, str) else "PHYSICAL_CONSOLE_LIVE")
        data = live.get("data")
        if not isinstance(data, Mapping):
            raise ContractError("PHYSICAL_CONSOLE_LIVE_RESULT")
        validator = data.get("technical_validator")
        technical_digest = (
            validator.get("result_digest")
            if isinstance(validator, Mapping)
            and isinstance(validator.get("result_digest"), str)
            and DIGEST.fullmatch(validator["result_digest"])
            else canonical_digest(validator)
        )
        technical = {
            "schema_version": "data_factory.seed_technical_result.v1",
            "intent_digest": intent["intent_digest"], "run_id": intent["run_id"],
            "manifest_digest": intent["manifest_digest"],
            "slot_id": intent["slot"]["slot_id"], "status": "PASS",
            "technical_result_digest": technical_digest,
            "post_scene_digest": data["postcommit_scene_state_digest"],
            "observed_at": clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        technical["evidence_digest"] = canonical_digest(technical)
        return {
            "result": {
                "technical_evidence": technical,
                "human_semantic": data.get("human_semantic_outcome", "NOT_MEASURED"),
            },
            "technical_evidence": technical,
        }

    def operator_factory(episode_call) -> CampaignOperator:
        operator = CampaignOperator(
            session_id=session_id, lifecycle_owner=operator_label,
            operator_label=operator_label,
            workspace={
                "workspace_id": "place1-test-only",
                "identity": "PLACE_A@place-a-yaw0-r002",
            },
            hypothesis=hypothesis, draft=draft,
            effect_scope="PHYSICAL", lifecycle_action="LIVE_COLLECT",
            data_disposition="TEST_ONLY",
            subsystems={
                "robot": {"readiness": "READY", "capability": "ATTACH", "reason": "PASSIVE_GATE_AT_RUN"},
                "gripper": {
                    "readiness": (
                        "READY" if initial_gripper["state"] == "ATTACHED"
                        else "NOT_AVAILABLE"
                    ),
                    "capability": "ATTACH",
                    "reason": initial_gripper["state"],
                },
                "camera": {"readiness": "READY", "capability": "CONNECTED_UNPLACED", "reason": "STABLE_LOCAL_BINDING"},
            },
            expires_at=(clock() + timedelta(hours=1)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            initial_scene_digest=state_initialization["scene_state_digest"],
            scene_evidence_call=scene_evidence,
            side_effect_counter_call=lambda: copy.deepcopy(counters),
            fake_lifecycle_factory=lambda: (_ for _ in ()).throw(
                ContractError("PHYSICAL_CONSOLE_FAKE_FACTORY"),
            ),
            physical_activation_gate=activate,
            physical_lifecycle_factory=fresh_one_job,
            physical_live_call=episode_call,
            physical_root_binding_call=lambda _run_id: copy.deepcopy(roots),
            physical_start_binding_call=start_binding,
            repository_root=repository, clock=clock,
        )
        holder["operator"] = operator
        return operator

    fixed_lane = {
        "workspace": {
            "display_name": "place1 · TEST_ONLY",
            "place_id": "PLACE_A", "revision": "place-a-yaw0-r002",
            "bounds": "yaw 0° · x=0 mm · y=0 mm · one episode",
        },
        "object_id": resolved["normalized_job"]["object_profile_id"],
        "grasp_id": resolved["normalized_job"]["grasp_profile_id"],
        "task": {"id": "pickup_e2e", "capability": "PHYSICAL_EXECUTABLE"},
        "motion": {"id": "DIRECT", "capability": "PHYSICAL_EXECUTABLE"},
        "start_pose_id": "test-only-home-r001",
        "camera_role": "up · CONNECTED_UNPLACED · TEST_ONLY",
        "profile_id": profile["collection_profile_id"],
    }

    def projection() -> dict[str, Any]:
        gripper = holder["gripper_projection"]
        return {
            "setup": {
                "host_status": (
                    "READY" if gripper["state"] == "ATTACHED"
                    else "READY_WITH_EXCEPTION"
                    if gripper["state"] == "MAINTENANCE_APPROVAL_REQUIRED"
                    else "BLOCKED"
                ),
                "operator_label": operator_label,
                "subsystems": [
                    {"label": "robot", "status": "ATTACH_ON_RUN", "detail": "existing foreground ROS graph only"},
                    {
                        "label": "gripper", "status": gripper["state"],
                        "detail": (
                            f"{gripper['supported_action']} · maintenance calls "
                            f"{gripper['maintenance_call_count']}"
                        ),
                    },
                    {"label": "camera", "status": "CONNECTED_UNPLACED", "detail": camera_binding["stable_device_id"]},
                    {"label": "data", "status": "TEST_ONLY", "detail": "production writers disabled"},
                ],
            },
            "fixed_lane": copy.deepcopy(fixed_lane),
            "draft": {
                "draft_id": draft["draft_id"], "revision": draft["revision"],
                "authoring_mode": "DIRECT_EDIT", "selector": draft["selector"],
                "selector_version": "campaign-selector-v1", "budget": 1,
                "selected_count": 1, "blocked_count": 0, "estimated_minutes": 2,
                "split_summary": "TRAIN 1", "repeat_summary": "x1",
                "coverage_summary": "1/1 selected",
                "cells": [{
                    "cell_id": "place1-yaw0-origin", "x_mm": 0, "y_mm": 0,
                    "yaw_deg": 0, "split": "TRAIN", "repeat": 1,
                    "coverage_count": 0, "selection_state": "SELECTED",
                    "eligibility_status": "ELIGIBLE",
                    "reason_codes": ["EXACT_TEST_ONLY_SCOPE"],
                }],
            },
            "capabilities": [{
                "label": "pickup_e2e · DIRECT · one-camera TEST_ONLY",
                "status": "PHYSICAL_EXECUTABLE",
                "reason_codes": ["EXACT_HOME_PLACE1_ONE_EPISODE"],
            }],
            "workspace_wizard": {
                "capability": "NOT_AVAILABLE",
                "plane_reference": {
                    "id": "place-a-yaw0-r002",
                    "digest": resolved["input_digests"]["cell_calibration"],
                    "table_normal_base": resolved["calibration"]["z"],
                },
                "source_measurement_mm": None, "final_measurement_mm": None,
                "captures": {"CENTER": False, "X_REF": False, "Y_CHECK": False},
            },
            "effect_counts": copy.deepcopy(counters),
        }

    paths_text = " · ".join(
        f"{name}={roots[name]}" for name in ("run_root", "dataset_root", "cell_root")
    )
    console = OperatorConsole(
        session_id=session_id, run_id=run_id, operator_label=operator_label,
        campaign_operator_factory=operator_factory, episode_call=episode,
        projection_call=projection, test_only_paths=paths_text,
        terminal_response_call=lambda: holder.get("last_live_response"),
        gripper_setup_request=setup_request,
        gripper_setup_resolution_call=(
            resolve_gripper_setup if setup_request is not None else None
        ),
        initial_block_code=initial_block_code,
        prepare_timeout_s=8.0, close_timeout_s=5.0, clock=clock,
    )
    context = {
        "session_id": session_id, "run_id": run_id,
        "effect_scope": "PHYSICAL", "data_disposition": "TEST_ONLY",
        "camera_binding": camera_binding, "roots": roots,
        "resolved_job_digest": resolved["resolved_job_digest"],
        "hypothesis_digest": hypothesis["hypothesis_digest"],
        "motion_qualification_digest": canonical_digest(motion_qualification),
        "gripper_setup": copy.deepcopy(holder["gripper_projection"]),
        "production_writers_enabled": False,
    }
    return console, context


QA_WORKFLOW = (
    "single-camera/TEST_ONLY/fixed place1 lane을 확인한다",
    "계획 만들기를 누르고 exact plan digest·DIRECT 10-phase·격리 경로를 확인한다",
    "범위가 같을 때만 계획 승인 또는 취소를 누른다",
    "실물 실행 중 E-stop/cell을 감시한다",
    "release checkpoint에서만 LANDED/OFF_SLOT/UNCERTAIN를 한 번 선택한다",
    "terminal에서 candidate review NOT_APPLICABLE과 다음 intent 0을 확인한다",
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve one foreground FR5 collection operator console",
    )
    parser.add_argument("--effect-scope", choices=("FAKE", "PHYSICAL"), default="FAKE")
    parser.add_argument("--port", type=int, default=4174)
    parser.add_argument("--repository-root", default=str(ROOT))
    parser.add_argument("--session-id")
    parser.add_argument("--run-id")
    parser.add_argument("--operator-label", default="local-operator")
    parser.add_argument("--camera-device-id")
    args = parser.parse_args(argv)
    if args.effect_scope == "FAKE":
        from tools.data_factory.fake_operator_console import main as fake_main
        return fake_main(["--port", str(args.port)])
    now = datetime.now(timezone.utc)
    suffix = now.strftime("%Y%m%dT%H%M%SZ")
    session_id = args.session_id or f"goal2-test-only-{suffix}"
    run_id = args.run_id or f"goal2-place1-{suffix}"
    console = bridge = None
    try:
        console, context = build_physical_operator_console(
            repository_root=args.repository_root,
            session_id=session_id, run_id=run_id,
            operator_label=args.operator_label,
            selected_camera_device_id=args.camera_device_id,
        )
        bridge = LoopbackBridge(
            core=console.bridge_core,
            ui_root=Path(args.repository_root).resolve() / "operator-ui",
            host="127.0.0.1", port=args.port,
        )
        print(json.dumps({
            "status": "LISTENING", "url": bridge.origin,
            "qa_workflow": QA_WORKFLOW, **context,
        }, sort_keys=True), flush=True)
        bridge.serve_forever()
    except KeyboardInterrupt:
        return 130
    except (ContractError, OSError) as exc:
        code = exc.code if isinstance(exc, ContractError) else "OPERATOR_CONSOLE_FAILED"
        print(json.dumps({"error": {"code": code, "message": str(exc)}}), flush=True)
        return 2
    finally:
        if console is not None:
            console.close()
        if bridge is not None:
            bridge.server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
