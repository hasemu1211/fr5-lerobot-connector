"""Thin foreground UI adapter for one injected TEST_ONLY campaign.

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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from tools.data_factory.campaign_authoring import (
    DRAFT_SCHEMA,
    campaign_cell_id,
    validate_campaign_draft,
)
from tools.data_factory.campaign_authorization import (
    EPISODE_SCOPE_FIELDS,
    build_campaign_authorization,
    build_campaign_envelope,
    validate_authorized_episode_scope,
)
from tools.data_factory.campaign_operator import (
    CampaignOperator,
    SIDE_EFFECT_COUNTERS,
)
from tools.data_factory.experiment_manifest import (
    FR5_TEST_ONLY_FEATURE_CONTRACT,
    build_test_only_feature_contract,
    compile_fr5_hypothesis,
)
from tools.data_factory.one_job import OneJob, TEST_ONLY_READINESS_CONTRACT
from tools.data_factory.operator_application import CollectionOperatorApplication
from tools.data_factory.operator_bridge import (
    ButtonDecisionPort,
    CandidateReviewPort,
    INTENT_SCHEMA,
    LoopbackBridge,
    OperatorCheckpointPort,
    OperatorIntentCore,
)
from tools.data_factory.operator_setup import (
    build_camera_role_bindings,
    build_camera_binding_from_discovery,
    build_test_only_runtime_episode_binding,
    build_test_only_root_binding,
    build_test_only_start_binding,
    initialize_test_only_state_from_user_declaration,
    gripper_setup_projection,
    load_camera_binding_receipt,
    load_camera_role_bindings,
    normalize_camera_devices,
    qualified_table_plane_reference,
    reuse_camera_binding_receipt,
    reuse_camera_role_bindings,
    select_yaw0_print_profile,
    validate_camera_role_bindings,
    validate_print_measurements,
    write_camera_binding_receipt,
    write_camera_role_bindings,
)
from tools.data_factory.operator_catalog import (
    CATALOG_SCHEMA,
    SELECTION_SCHEMA,
    SELECTION_SCHEMA_V2,
    UNBOUND_CAMERA_DEVICE_ID,
    camera_binding_digest,
    load_operator_catalog,
    project_assisted_poses,
    project_balanced_start_pose_ids,
    validate_operator_pose,
    validate_operator_selection,
)
from tools.data_factory.quality.coverage_report import build_coverage_report
from tools.data_factory.scene_state import SceneStateStore
from tools.data_factory.start_pose_registry import (
    compile_start_pose_profile,
    list_start_pose_profiles,
    project_robot_start_pose_qualification,
    save_start_pose_profile,
)
from tools.data_factory.task_recipe import get_task_recipe
from tools.data_factory.workspace_manager import WorkspaceManager
from tools.data_factory import run_job
from tools.data_factory_recovery import write_json_atomic
from tools.fr5_data_factory import (
    ContractError,
    DIGEST,
    SAFE_ID,
    canonical_digest,
    load_json_strict,
    normalize_yaw_deg,
    validate_motion_program,
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
LIVE_RUNTIME_MILESTONES = {
    "PLANNING": ("경로 계획 및 충돌 검사", 10, "현재 시작 상태에서 각 동작 구간을 연결해 검사합니다."),
    "CAMERA_WARMUP": ("카메라 전송 확인", 25, "프레임 속도와 timestamp 전송 상태를 확인합니다."),
    "AWAITING_HUMAN_APPROVAL": ("승인 범위 확인", 35, "캠페인 승인 범위와 이번 계획을 대조합니다."),
    "RECORDER_STARTING": ("기록기 준비", 40, "30 Hz readiness와 writer 상태를 확인합니다."),
    "EXECUTING": ("수집 동작 실행", 50, "로봇 상태·명령·RGB를 동기화해 기록합니다."),
    "RECYCLING": ("수집 구간 완료 및 복구", 70, "녹화를 멈춘 뒤 물체를 다음 시작 상태로 복구합니다."),
    "FINALIZING": ("데이터 저장", 80, "동결된 episode를 commit하고 영상 파일을 마무리합니다."),
    "VALIDATING": ("데이터 품질 검사", 90, "timestamp·drop·provenance·프레임 일치를 검사합니다."),
}
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
DEFAULT_START_POSES = Path("config/data_factory/start_poses")
DEFAULT_PROFILE = Path(
    "config/data_factory/collection_profiles/fr5-up-rgb-30hz-v1.json"
)
DEFAULT_URDF = Path("src/fairino_description/urdf/fairino5_v6.urdf")
DEFAULT_TCP_MANIFEST = Path(
    "config/data_factory/test_only_physical/goal2-place1/tcp_candidate_manifest.json"
)
DEFAULT_GRIPPER_RETUNE = Path(
    "config/data_factory/test_only_physical/goal2-place1/"
    "gripper-retune-wood-cube-25mm-top-center-r002.json"
)
GRIPPER_RETUNE_FIELDS = frozenset({
    "schema_version", "retune_id", "status", "source",
    "object_profile_id", "grasp_profile_id",
    "base_grasp_profile_digest", "base_motion_qualification_digest",
    "command_position_m", "acceptable_feedback_m", "data_disposition",
    "production_authority", "training_authority", "retune_digest",
})


def _measurement_for_code(code: str) -> str:
    if code == "PHYSICAL_SECOND_MOTION_OWNER" or code.endswith("_MISMATCH"):
        return "FAIL"
    if code.startswith(("PHYSICAL_", "GRIPPER_SETUP_")) or code.endswith("NOT_AVAILABLE"):
        return "NOT_AVAILABLE"
    return "FAIL"


def _redigest(value: dict[str, Any], field: str) -> dict[str, Any]:
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


def _campaign_camera_warmup(
    *, cache: dict[str, Any], transport: Mapping[str, Any],
    payload: Mapping[str, Any], profile: Mapping[str, Any],
    cancel: threading.Event, measure_call: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    """Measure once per exact binding; later runs get a distinct reuse receipt."""
    if not isinstance(transport, Mapping) or not isinstance(
        transport.get("binding_digest"), str,
    ):
        raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
    cache_key = canonical_digest({
        "camera_transport_binding_digest": transport["binding_digest"],
        "collection_profile_digest": canonical_digest(profile),
    })
    cached = cache.get("camera_warmup_cache")
    if not isinstance(cached, Mapping) or cached.get("cache_key") != cache_key:
        measured = measure_call(payload, profile, cancel)
        attempts = measured.get("attempts") if isinstance(measured, Mapping) else None
        exact_measurement = (
            isinstance(measured, Mapping)
            and set(measured) == {
                "schema_version", "run_id", "camera_profile", "attempts",
            }
            and measured.get("schema_version") == "data_factory.camera_warmup.v1"
            and measured.get("run_id") == payload.get("run_id")
            and measured.get("camera_profile") == payload.get("camera_profile")
        )
        if (
            not cancel.is_set() and exact_measurement
            and isinstance(attempts, list) and attempts
            and isinstance(attempts[-1], Mapping)
            and attempts[-1].get("status") == "PASS"
        ):
            cache["camera_warmup_cache"] = {
                "cache_key": cache_key,
                "source_run_id": payload["run_id"],
                "source_evidence": copy.deepcopy(dict(measured)),
                "source_evidence_digest": canonical_digest(measured),
            }
        return copy.deepcopy(dict(measured))
    evidence = {
        "schema_version": "data_factory.camera_warmup_reuse.v1",
        "run_id": payload["run_id"],
        "status": "REUSED_PASS",
        "source_run_id": cached["source_run_id"],
        "source_evidence_digest": cached["source_evidence_digest"],
        "camera_transport_binding_digest": transport["binding_digest"],
        "collection_profile_digest": canonical_digest(profile),
    }
    write_json_atomic(
        Path(payload["run_root"]) / payload["run_id"]
        / "camera_warmup_reuse.json",
        evidence,
    )
    return evidence


def _derive_test_only_gripper_program(
    validated: Mapping[str, Any], motion: Mapping[str, Any],
    program: Mapping[str, Any], retune: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply an object-scoped TEST_ONLY override after qualified resolution."""
    if (
        not isinstance(retune, Mapping) or set(retune) != GRIPPER_RETUNE_FIELDS
        or retune.get("schema_version")
        != "data_factory.test_only_gripper_retune.v1"
        or retune.get("status") != "CANDIDATE_PENDING_HIL"
        or retune.get("source") != "OPERATOR_REPORTED_RETUNE"
        or retune.get("data_disposition") != "TEST_ONLY"
        or retune.get("production_authority") is not False
        or retune.get("training_authority") is not False
        or not isinstance(retune.get("retune_id"), str)
        or SAFE_ID.fullmatch(retune["retune_id"]) is None
        or not isinstance(retune.get("retune_digest"), str)
        or DIGEST.fullmatch(retune["retune_digest"]) is None
        or retune["retune_digest"] != canonical_digest({
            key: item for key, item in retune.items() if key != "retune_digest"
        })
        or not isinstance(validated, Mapping) or not isinstance(motion, Mapping)
        or not isinstance(program, Mapping)
    ):
        raise ContractError("TEST_ONLY_GRIPPER_RETUNE")
    job = validated.get("normalized_job")
    inputs = validated.get("input_digests")
    grasp = validated.get("grasp_profile")
    close = grasp.get("gripper_close") if isinstance(grasp, Mapping) else None
    feedback = retune.get("acceptable_feedback_m")
    base_feedback = close.get("acceptable_feedback_m") if isinstance(close, Mapping) else None
    program_bindings = program.get("binding_digests")
    program_requirements = program.get("gripper_requirements")
    numbers = (
        retune.get("command_position_m"),
        feedback.get("min") if isinstance(feedback, Mapping) else None,
        feedback.get("max") if isinstance(feedback, Mapping) else None,
    )
    if (
        not isinstance(job, Mapping) or not isinstance(inputs, Mapping)
        or not isinstance(grasp, Mapping) or not isinstance(close, Mapping)
        or not isinstance(base_feedback, Mapping)
        or not isinstance(feedback, Mapping) or set(feedback) != {"min", "max"}
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in numbers
        )
        or retune.get("object_profile_id") != job.get("object_profile_id")
        or retune.get("grasp_profile_id") != job.get("grasp_profile_id")
        or retune.get("object_profile_id") != grasp.get("object_profile_id")
        or retune.get("grasp_profile_id") != grasp.get("grasp_profile_id")
        or retune.get("base_grasp_profile_digest") != canonical_digest(grasp)
        or inputs.get("grasp_profile") != canonical_digest(grasp)
        or retune.get("base_motion_qualification_digest")
        != canonical_digest(motion)
        or not isinstance(program_bindings, Mapping)
        or program_bindings.get("motion_qualification") != canonical_digest(motion)
        or program_bindings.get("grasp_profile") != canonical_digest(grasp)
        or program_requirements != close
    ):
        raise ContractError("TEST_ONLY_GRIPPER_RETUNE_BINDING")
    command, minimum, maximum = (float(value) for value in numbers)
    if not (
        float(close["command_position_m"]) <= command <= minimum < maximum
        and float(base_feedback["min"]) <= minimum
        and maximum <= float(base_feedback["max"])
    ):
        raise ContractError("TEST_ONLY_GRIPPER_RETUNE_ENVELOPE")

    tuned_requirements = copy.deepcopy(dict(close))
    tuned_requirements.update(
        command_position_m=command,
        acceptable_feedback_m={"min": minimum, "max": maximum},
        evidence_digest=retune["retune_digest"],
    )
    derived = copy.deepcopy(dict(program))
    derived["gripper_requirements"] = tuned_requirements
    close_steps = [
        step for step in derived.get("steps", [])
        if isinstance(step, Mapping) and step.get("phase") == "GRIPPER_CLOSE"
    ]
    if len(close_steps) != 1:
        raise ContractError("TEST_ONLY_GRIPPER_RETUNE_BINDING")
    close_steps[0]["gripper_position_m"] = command
    close_steps[0]["limits"]["completion_tolerance_m"] = maximum - command
    return copy.deepcopy(validate_motion_program(derived))


def _test_only_home_start_pose(
    motion_qualification: Mapping[str, Any], home_candidate: Mapping[str, Any],
    robot_system_id: str,
) -> dict[str, Any]:
    target = motion_qualification.get("qualified_safe_joint_positions_rad")
    tolerance = motion_qualification.get("goal_tolerances", {}).get("joint_rad")
    source_id = home_candidate.get("home_candidate_id")
    if (
        not isinstance(target, list) or len(target) != 6
        or isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not math.isfinite(tolerance) or tolerance <= 0
    ):
        raise ContractError("PHYSICAL_CONSOLE_START_POSE")
    if not isinstance(source_id, str) or not SAFE_ID.fullmatch(source_id):
        source_id = "home-" + canonical_digest(home_candidate).removeprefix("sha256:")[:20]
    joints = ("j1", "j2", "j3", "j4", "j5", "j6")
    return _redigest({
        "schema_version": "data_factory.robot_start_pose_qualification.v1",
        "source": "SYNTHETIC_TEST_ONLY",
        "robot_system_id": robot_system_id,
        "robot_start_pose_id": source_id,
        "joint_order": list(joints),
        "target_rad": dict(zip(joints, target)),
        "tolerance_rad": {joint: float(tolerance) for joint in joints},
        "home_candidate_digest": canonical_digest(home_candidate),
        "qualification_status": "QUALIFIED",
        "safety_status": "SAFE_FOR_MOTION",
    }, "qualification_digest")


def _build_physical_campaign_contract(
    *, resolver_results: Sequence[Mapping[str, Any]],
    motion_qualification: Mapping[str, Any], home_candidate: Mapping[str, Any],
    scene_digest: str, draft_id: str, manifest_id: str,
    requested_count: int, normalized_seed: int = 0,
    anchor_resolved_job_digest: str | None = None,
    direct_resolved_job_digests: Sequence[str] | None = None,
    direct_start_pose_ids: Sequence[str] | None = None,
    start_pose_qualifications: Sequence[Mapping[str, Any]] | None = None,
    test_only_gripper_retune_digest: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile a finite TEST_ONLY condition domain without execution authority."""
    if (
        type(requested_count) is not int or not 1 <= requested_count <= 100
        or type(normalized_seed) is not int or normalized_seed < 0
        or test_only_gripper_retune_digest is not None
        and (
            not isinstance(test_only_gripper_retune_digest, str)
            or DIGEST.fullmatch(test_only_gripper_retune_digest) is None
        )
    ):
        raise ContractError("PHYSICAL_CONSOLE_REQUESTED_COUNT")
    if (
        not isinstance(resolver_results, (list, tuple))
        or not resolver_results
        or any(not isinstance(item, Mapping) for item in resolver_results)
    ):
        raise ContractError("PHYSICAL_CONSOLE_RESOLVED_JOB")
    resolved_jobs = [copy.deepcopy(dict(item)) for item in resolver_results]
    resolved_by_job_digest = {
        item.get("resolved_job_digest"): item for item in resolved_jobs
    }
    if (
        len(resolved_by_job_digest) != len(resolved_jobs)
        or any(not isinstance(key, str) or not DIGEST.fullmatch(key) for key in resolved_by_job_digest)
    ):
        raise ContractError("PHYSICAL_CONSOLE_RESOLVED_JOB")
    first = resolved_jobs[0]
    job = first.get("normalized_job")
    inputs = first.get("input_digests")
    profile = first.get("collection_profile")
    try:
        feature_contract = build_test_only_feature_contract(profile)
    except ContractError as exc:
        if (
            isinstance(profile, Mapping)
            and profile.get("collection_profile_id")
            == FR5_TEST_ONLY_FEATURE_CONTRACT["collection_profile_id"]
        ):
            feature_contract = copy.deepcopy(FR5_TEST_ONLY_FEATURE_CONTRACT)
        else:
            raise ContractError("PHYSICAL_CONSOLE_FIXED_BINDING") from exc
    if (
        not isinstance(job, Mapping)
        or not isinstance(inputs, Mapping)
        or not isinstance(profile, Mapping)
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
    fixed_job_fields = (
        "task", "instruction", "robot_system_id", "collection_profile_id",
        "cell_calibration_id", "object_profile_id", "grasp_profile_id",
    )
    fixed_input_fields = (
        "cell_calibration", "robot_system", "collection_profile",
        "object_profile", "grasp_profile",
    )
    for resolved in resolved_jobs:
        other_job = resolved.get("normalized_job")
        other_inputs = resolved.get("input_digests")
        if (
            not isinstance(other_job, Mapping)
            or not isinstance(other_inputs, Mapping)
            or resolved.get("collection_profile") != profile
            or any(other_job.get(field) != job[field] for field in fixed_job_fields)
            or any(other_inputs.get(field) != inputs[field] for field in fixed_input_fields)
            or resolved.get("resolved_job_digest")
            != canonical_digest({"job": other_job, "input_digests": other_inputs})
        ):
            raise ContractError("PHYSICAL_CONSOLE_FIXED_BINDING")
    fixed = {
        "schema_version": "data_factory.fr5_fixed_contract.v1",
        "robot_system_id": job["robot_system_id"],
        "task": job["task"],
        "instruction": job["instruction"],
        "collection_profile_digest": inputs["collection_profile"],
        "feature_contract": feature_contract,
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
            **(
                {"test_only_gripper_retune": test_only_gripper_retune_digest}
                if test_only_gripper_retune_digest is not None else {}
            ),
        }),
    }
    conditions = []
    resolved_by_condition = {}
    for resolved in resolved_jobs:
        current_job = resolved["normalized_job"]
        current_inputs = resolved["input_digests"]
        condition = {
            "task_schema_version": current_job["schema_version"],
            "task": current_job["task"],
            "robot_system_id": current_job["robot_system_id"],
            "place_id": current_job["place_id"],
            "cell_calibration_id": current_job["cell_calibration_id"],
            "cell_calibration_digest": current_inputs["cell_calibration"],
            "yaw_deg": current_job["yaw_deg"],
            "x_mm": current_job["x_mm"],
            "y_mm": current_job["y_mm"],
            "object_profile_id": current_job["object_profile_id"],
            "grasp_profile_id": current_job["grasp_profile_id"],
            "motion_recipe_digest": motion_digest,
            "collection_profile_digest": current_inputs["collection_profile"],
        }
        condition_digest = canonical_digest(condition)
        if condition_digest in resolved_by_condition:
            raise ContractError("PHYSICAL_CONSOLE_RESOLVED_JOB")
        conditions.append(condition)
        resolved_by_condition[condition_digest] = resolved
    report = build_coverage_report(
        collection_profile_id=profile["collection_profile_id"],
        domain=conditions, episodes=[],
    )
    bases = []
    for cell in report["cells"]:
        condition = cell["condition"]
        condition_digest = canonical_digest(condition)
        resolved = resolved_by_condition[condition_digest]
        bases.append(_redigest({
            "schema_version": "data_factory.fr5_base_condition_qualification.v1",
            "source": "SYNTHETIC_TEST_ONLY",
            "qualification_status": "QUALIFIED",
            "coverage_report_digest": canonical_digest(report),
            "coverage_domain_digest": report["domain_digest"],
            "coverage_condition_digest": condition_digest,
            "resolver_result_digest": canonical_digest(resolved),
            "resolved_job_digest": resolved["resolved_job_digest"],
            "yaw_action_binding_digest": canonical_digest({
                "scope": "TEST_ONLY", "yaw_deg": condition["yaw_deg"],
                "motion_qualification_digest": motion_digest,
            }),
            "dual_view_observability_digest": canonical_digest({
                "single_view": "CONNECTED_UNPLACED",
                "dual_view": "NOT_AVAILABLE",
                "semantic_authority": "NONE",
                "yaw_deg": condition["yaw_deg"],
            }),
        }, "qualification_digest"))
    home_pose = _test_only_home_start_pose(
        motion_qualification, home_candidate, job["robot_system_id"],
    )
    poses = [home_pose] if start_pose_qualifications is None else [
        copy.deepcopy(dict(item)) for item in start_pose_qualifications
    ]
    pose_ids = [item.get("robot_start_pose_id") for item in poses]
    if (
        not poses or pose_ids != sorted(pose_ids)
        or len(pose_ids) != len(set(pose_ids))
        or any(
            item.get("schema_version")
            != "data_factory.robot_start_pose_qualification.v1"
            or item.get("source") != "SYNTHETIC_TEST_ONLY"
            or item.get("robot_system_id") != job["robot_system_id"]
            or item.get("home_candidate_digest") != canonical_digest(home_candidate)
            or item.get("qualification_status") != "QUALIFIED"
            or item.get("safety_status") != "SAFE_FOR_MOTION"
            or item.get("qualification_digest")
            != canonical_digest({
                key: value for key, value in item.items()
                if key != "qualification_digest"
            })
            for item in poses
        )
    ):
        raise ContractError("PHYSICAL_CONSOLE_START_POSE")
    catalog = _redigest({
        "schema_version": "data_factory.fr5_qualification_catalog.v1",
        "source": "SYNTHETIC_TEST_ONLY",
        "qualification_status": "QUALIFIED",
        "fixed_contract_digest": canonical_digest(fixed),
        "coverage_report_digest": canonical_digest(report),
        "coverage_domain_digest": report["domain_digest"],
        "resolver_result_digests": sorted(
            canonical_digest(resolved) for resolved in resolved_jobs
        ),
        "base_condition_qualifications": bases,
        "robot_start_pose_qualifications": poses,
        "allowed_pairs": sorted(({
            "base_condition_qualification_digest": base["qualification_digest"],
            "robot_start_pose_qualification_digest": pose["qualification_digest"],
            "split_groups": ["TRAIN"],
        } for base in bases for pose in poses), key=lambda item: (
            item["base_condition_qualification_digest"],
            item["robot_start_pose_qualification_digest"],
        )),
    }, "catalog_digest")
    hypothesis = compile_fr5_hypothesis(
        fixed_contract=fixed, coverage_report=report,
        resolver_results=resolved_jobs, qualification_catalog=catalog,
    )
    manifest_budget = {
        "max_physical_episodes": requested_count, "max_rollout_trials": requested_count,
        "max_hil_prompts": requested_count, "max_reviews": requested_count,
        "max_pending_reviews": requested_count,
        "max_storage_bytes": 2_147_483_648 * requested_count,
    }
    program_budget = {
        "max_rounds": 1, "used_rounds": 0,
        "max_total_physical_episodes": requested_count, "used_total_physical_episodes": 0,
        "max_total_rollout_trials": requested_count, "used_total_rollout_trials": 0,
        "max_total_hil_prompts": requested_count, "used_total_hil_prompts": 0,
        "max_total_reviews": requested_count, "used_total_reviews": 0,
        "max_pending_reviews": requested_count, "used_pending_reviews": 0,
        "max_total_storage_bytes": 2_147_483_648 * requested_count,
        "used_total_storage_bytes": 0,
    }
    base_by_job_digest = {
        item["resolved_job_digest"]: item for item in hypothesis["base_conditions"]
    }
    if set(base_by_job_digest) != set(resolved_by_job_digest):
        raise ContractError("PHYSICAL_CONSOLE_RESOLVED_JOB")
    start_pose_id = hypothesis["robot_start_poses"][0]["robot_start_pose_id"]
    pinned = []
    direct_slots = []
    selector = "BALANCED_INITIAL"
    if direct_resolved_job_digests is not None:
        if direct_start_pose_ids is None:
            direct_start_pose_ids = [start_pose_id] * requested_count
        if (
            not isinstance(direct_resolved_job_digests, (list, tuple))
            or len(direct_resolved_job_digests) != requested_count
            or any(item not in base_by_job_digest for item in direct_resolved_job_digests)
            or not isinstance(direct_start_pose_ids, (list, tuple))
            or len(direct_start_pose_ids) != requested_count
            or any(item not in pose_ids for item in direct_start_pose_ids)
        ):
            raise ContractError("PHYSICAL_CONSOLE_DIRECT_SEQUENCE")
        selector = "DIRECT_LIST"
        repeats: dict[tuple[str, str], int] = {}
        for resolved_digest, selected_start_pose_id in zip(
            direct_resolved_job_digests, direct_start_pose_ids,
        ):
            base_digest = base_by_job_digest[resolved_digest]["base_condition_digest"]
            repeat_key = (base_digest, selected_start_pose_id)
            repeat_index = repeats.get(repeat_key, 0)
            repeats[repeat_key] = repeat_index + 1
            direct_slots.append({
                "slot_id": campaign_cell_id(
                    base_digest, selected_start_pose_id, "TRAIN", repeat_index,
                ),
                "base_condition_digest": base_digest,
                "robot_start_pose_id": selected_start_pose_id,
                "split_group": "TRAIN", "repeat_index": repeat_index,
                "hil_prompts": 1, "reviews": 1, "pending_reviews": 0,
                "storage_bytes": max(
                    1, manifest_budget["max_storage_bytes"] // requested_count,
                ),
            })
    elif anchor_resolved_job_digest is not None:
        if anchor_resolved_job_digest not in base_by_job_digest:
            raise ContractError("PHYSICAL_CONSOLE_SEQUENCE_ANCHOR")
        base_digest = base_by_job_digest[
            anchor_resolved_job_digest
        ]["base_condition_digest"]
        pinned = [campaign_cell_id(base_digest, start_pose_id, "TRAIN", 0)]
    draft = validate_campaign_draft({
        "schema_version": DRAFT_SCHEMA,
        "draft_id": draft_id, "revision": 0,
        "source": {
            "hypothesis_digest": hypothesis["hypothesis_digest"],
            "catalog_digest": hypothesis["qualification_catalog"]["catalog_digest"],
            "coverage_digest": canonical_digest(hypothesis["coverage_report"]),
        },
        "branch": "INITIAL_SEED", "selector": selector,
        "requested_count": requested_count, "normalized_seed": normalized_seed,
        "pinned": pinned, "excluded": [], "direct_slots": direct_slots,
        "manifest_id": manifest_id,
        "manifest_budget": manifest_budget, "program_budget": program_budget,
    }, hypothesis=hypothesis)
    return hypothesis, draft


def build_physical_test_contract(
    *, resolved_job: Mapping[str, Any], motion_qualification: Mapping[str, Any],
    home_candidate: Mapping[str, Any], scene_digest: str,
    draft_id: str, manifest_id: str, requested_count: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Backward-compatible single-condition TEST_ONLY contract."""
    return _build_physical_campaign_contract(
        resolver_results=[resolved_job],
        motion_qualification=motion_qualification,
        home_candidate=home_candidate, scene_digest=scene_digest,
        draft_id=draft_id, manifest_id=manifest_id,
        requested_count=requested_count,
    )


class OperatorConsole:
    """Expose one finite campaign through one outer intent core and worker."""

    def __init__(
        self, *, session_id: str, operator_label: str,
        campaign_operator_factory: Callable[[Callable[..., Mapping[str, Any]]], CampaignOperator],
        episode_call: Callable[..., Mapping[str, Any]],
        projection_call: Callable[[], Mapping[str, Any]],
        test_only_paths: str, run_id: str | None = None,
        candidate_review_port: CandidateReviewPort | None = None,
        candidate_state_bind_call: Callable[
            [Mapping[str, Any], str | Path], Mapping[str, Any]
        ] | None = None,
        terminal_response_call: Callable[[], Mapping[str, Any] | None] | None = None,
        gripper_setup_request: Mapping[str, Any] | None = None,
        gripper_setup_resolution_call: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        initial_block_code: str | None = None,
        campaign_approval_once: bool = False,
        run_id_factory: Callable[[int], str] | None = None,
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
        if type(campaign_approval_once) is not bool or run_id_factory is not None and not callable(run_id_factory):
            raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_MODE")
        if not all(callable(call) for call in (
            campaign_operator_factory, episode_call, projection_call,
        )):
            raise ContractError("OPERATOR_CONSOLE_CALLABLE")
        if not isinstance(test_only_paths, str) or not test_only_paths or "\x00" in test_only_paths:
            raise ContractError("OPERATOR_CONSOLE_TEST_ONLY_PATHS")
        if candidate_review_port is not None and not isinstance(candidate_review_port, CandidateReviewPort):
            raise ContractError("OPERATOR_CONSOLE_CANDIDATE_PORT")
        if candidate_state_bind_call is not None and (
            candidate_review_port is None or not callable(candidate_state_bind_call)
        ):
            raise ContractError("OPERATOR_CONSOLE_CANDIDATE_BIND_CALL")
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
        self._base_run_id = run_id
        self._run_index = 0
        self._run_id_factory = run_id_factory or (
            lambda index: run_id if index == 0 else f"{run_id}-e{index + 1}"
        )
        self.campaign_approval_once = campaign_approval_once
        self.episode_call, self.projection_call = episode_call, projection_call
        self.test_only_paths = test_only_paths
        self.prepare_timeout_s, self.close_timeout_s = float(prepare_timeout_s), float(close_timeout_s)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.terminal_response_call = terminal_response_call
        self.gripper_setup_resolution_call = gripper_setup_resolution_call
        self.button_port = ButtonDecisionPort(
            session_id=f"{session_id}-plan-0", operator_label=operator_label, clock=self.clock,
        )
        self.checkpoint_port = OperatorCheckpointPort(operator_label=operator_label)
        self.candidate_review_port = candidate_review_port
        self.candidate_state_bind_call = (
            candidate_state_bind_call or run_job.bind_candidate_episode_state
        )
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
        self._runtime_milestone = None
        self._terminal_object_pose = None
        self._episode_history: list[dict[str, Any]] = []
        self._candidate_review_queue: list[dict[str, Any]] = []
        self._active_candidate_review: dict[str, Any] | None = None
        self._campaign_envelope = self._campaign_authorization = None
        self._active_episode_scope = None
        self._active_intent_projection = None
        self._cancel_requested = False
        self._plan_choice = None
        if gripper_setup_request is not None:
            self.checkpoint_port.offer(gripper_setup_request)
            self._last_error = "MAINTENANCE_APPROVAL_REQUIRED"

        self.campaign_operator = campaign_operator_factory(self._run_episode)
        if (
            not isinstance(self.campaign_operator, CampaignOperator)
            or self.campaign_operator.effect_scope not in {"FAKE", "PHYSICAL"}
            or self.campaign_operator.lifecycle_action != "LIVE_COLLECT"
            or self.campaign_operator.data_disposition not in {"TEST_ONLY", "PRODUCTION"}
            or self.campaign_operator.effect_scope == "FAKE"
            and self.campaign_operator.data_disposition != "TEST_ONLY"
            or self.campaign_operator.operator_label != operator_label
        ):
            raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_OPERATOR")
        self._base_projection()
        handlers = {
            "compile_draft": self.compile_draft,
            "authorize_campaign": self.authorize_campaign,
            "approve_exact_plan": self.approve_exact_plan,
            "reject_plan": self.reject_plan,
            "resolve_checkpoint": self.resolve_checkpoint,
            "cancel_session": self.cancel_session,
        }
        if candidate_review_port is not None:
            handlers["review_candidate"] = self.review_candidate
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

    @property
    def campaign_authorization(self) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._campaign_authorization)

    @property
    def campaign_envelope(self) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._campaign_envelope)

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

    @staticmethod
    def _browser_result(value: object) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        result = copy.deepcopy(dict(value))
        ledger = result.get("episode_ledger")
        if isinstance(ledger, Mapping):
            result["episode_ledger"] = {
                key: copy.deepcopy(item)
                for key, item in ledger.items()
                if key not in {"path", "state_path"}
            }
        return result

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

    def _reset_episode_ports(self) -> None:
        self.button_port = ButtonDecisionPort(
            session_id=f"{self.session_id}-plan-{self._run_index}",
            operator_label=self.operator_label,
            clock=self.clock,
        )
        self.checkpoint_port = OperatorCheckpointPort(operator_label=self.operator_label)
        self._prepared = threading.Event()
        self._plan_choice = None
        self._episode_plan = None
        self._episode_result = None
        self._runtime_milestone = None
        self._active_episode_scope = None
        self._active_intent_projection = None

    def _campaign_coverage(self) -> list[dict[str, Any]]:
        manifest = self.campaign_operator.manifest
        if not isinstance(manifest, Mapping):
            return []
        bases = {
            item["base_condition_digest"]: item
            for item in self.campaign_operator.hypothesis["base_conditions"]
        }
        result = []
        for slot in manifest["slots"]:
            base = bases.get(slot["base_condition_digest"])
            condition = base.get("coverage_condition") if isinstance(base, Mapping) else None
            condition_digest = (
                base.get("coverage_condition_digest")
                if isinstance(base, Mapping) else None
            )
            if (
                not isinstance(condition, Mapping)
                or canonical_digest(condition) != condition_digest
            ):
                raise ContractError("OPERATOR_CONSOLE_COVERAGE_BINDING")
            result.append({
                "order_index": slot["order_index"],
                "slot_id": slot["slot_id"],
                "slot_digest": canonical_digest(slot),
                "base_condition_digest": slot["base_condition_digest"],
                "coverage_condition": copy.deepcopy(dict(condition)),
                "coverage_condition_digest": condition_digest,
            })
        return result

    def _advance_run(self) -> None:
        self._run_index += 1
        run_id = self._run_id_factory(self._run_index)
        if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id) or run_id == self.run_id:
            raise ContractError("OPERATOR_CONSOLE_RUN_ID")
        self.run_id = run_id
        self._reset_episode_ports()

    def _queue_candidate_review(self, value: object, sealed: Mapping[str, Any]) -> None:
        fields = {
            "candidate_path", "run_id", "expected_file_digest",
            "expected_review_context_digest", "ledger_reference",
        }
        binding = sealed.get("intent_binding")
        if (
            self.candidate_review_port is None
            or not isinstance(value, Mapping) or set(value) != fields
            or not isinstance(binding, Mapping)
            or value.get("run_id") != binding.get("run_id")
            or not isinstance(value.get("ledger_reference"), Mapping)
        ):
            raise ContractError("OPERATOR_CONSOLE_CANDIDATE_OFFER")
        item = copy.deepcopy(dict(value))
        item.update({
            "episode_number": len(self._episode_history) + 1,
            "coverage_condition": copy.deepcopy(binding.get("coverage_condition")),
        })
        self._candidate_review_queue.append(item)

    def _offer_next_candidate_review(self) -> None:
        if (
            self.candidate_review_port is None
            or self._active_candidate_review is not None
            or not self._candidate_review_queue
        ):
            return
        item = self._candidate_review_queue.pop(0)
        self.candidate_review_port.offer(**{
            key: item[key] for key in (
                "candidate_path", "run_id", "expected_file_digest",
                "expected_review_context_digest",
            )
        })
        self._active_candidate_review = item

    def _available_ops(self, checkpoint, candidate) -> list[str]:
        if self._workflow == "AUTHORING":
            return ["resolve_checkpoint"] if checkpoint is not None else ["compile_draft"]
        if self._workflow == "REVIEW_CAMPAIGN":
            return ["authorize_campaign"]
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
            if candidate is not None and self._active_candidate_review is not None:
                candidate.update({
                    "episode_number": self._active_candidate_review["episode_number"],
                    "queue_remaining": 1 + len(self._candidate_review_queue),
                    "coverage_condition": copy.deepcopy(
                        self._active_candidate_review["coverage_condition"]
                    ),
                })
            campaign_session = None if self.session is None else self.session.status()
            active = (
                campaign_session.get("active_run_id")
                if isinstance(campaign_session, Mapping)
                and campaign_session.get("active_child") is True
                else self.run_id if self._workflow in {
                    "AWAITING_APPROVAL", "RUNNING", "CANCELLING",
                } else None
            )
            runtime = {
                "workflow_state": self._workflow,
                "measurement_outcome": self._measurement_outcome,
                "reason_codes": [] if self._last_error is None else [self._last_error],
                "active_child_id": active,
            }
            if self._workflow in {"RUNNING", "CANCELLING"}:
                runtime.update(copy.deepcopy(
                    self._runtime_milestone
                    or {
                        "phase": "STARTING" if self._workflow == "RUNNING" else "CANCEL",
                        "phase_label": (
                            "에피소드 준비" if self._workflow == "RUNNING"
                            else "안전한 중단 확인"
                        ),
                        "progress": 0 if self._workflow == "RUNNING" else 90,
                        "detail": (
                            "실행 입력과 현재 상태를 연결합니다."
                            if self._workflow == "RUNNING"
                            else "다음 에피소드는 시작하지 않습니다."
                        ),
                    }
                ))
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
                "connection_state": "READY",
                "effect_scope": self.campaign_operator.effect_scope,
                "lifecycle_action": self.campaign_operator.lifecycle_action,
                "data_disposition": self.campaign_operator.data_disposition,
                "available_ops": self._available_ops(checkpoint, candidate),
                "operator_checkpoint": checkpoint,
                "candidate_review": candidate,
                "candidate_review_status": "NOT_APPLICABLE" if candidate is None else candidate["status"],
                "runtime": runtime, "approval": approval,
                "episode_plan": copy.deepcopy(self._episode_plan),
                "episode_result": self._browser_result(self._episode_result),
                "episode_history": [
                    self._browser_result(item) for item in self._episode_history
                ],
                "campaign_envelope": copy.deepcopy(self._campaign_envelope),
                "campaign_authorization": copy.deepcopy(self._campaign_authorization),
                # The lifecycle owner retains the full manifest and receipt.  The
                # browser only needs the compact campaign status; copying the full
                # owner projection here made large campaign views superlinear.
                "campaign_operator": (
                    None if campaign_session is None else {
                        "campaign": copy.deepcopy(campaign_session["campaign"]),
                    }
                ),
                "campaign_session": campaign_session,
                "campaign_coverage": self._campaign_coverage(),
                "terminal_object_pose": copy.deepcopy(self._terminal_object_pose),
                "operator_identity": self.operator_label,
                "human_semantic": "NOT_MEASURED",
            })
            return base

    def publish_runtime(self, event: Mapping[str, Any]) -> None:
        """Project bounded run_job milestones without taking lifecycle ownership."""
        if not isinstance(event, Mapping):
            raise ContractError("OPERATOR_CONSOLE_RUNTIME_EVENT")
        code = event.get("code")
        milestone = LIVE_RUNTIME_MILESTONES.get(code)
        if milestone is None:
            return
        if event.get("run_id") != self.run_id:
            raise ContractError("OPERATOR_CONSOLE_RUNTIME_EVENT")
        label, progress, detail = milestone

        def change():
            if self._workflow != "RUNNING":
                return
            self._runtime_milestone = {
                "phase": code, "phase_label": label,
                "progress": progress, "detail": detail,
            }

        self._owner_transition(change)

    def _owner_transition(self, change: Callable[[], None]) -> None:
        with self._lock:
            initial = self._initial_handler_active
        if initial:
            with self._lock:
                change()
                self._prepared.set()
        else:
            self.core.transition(change)

    def _authorization_payload(self) -> dict[str, str]:
        manifest = self.campaign_operator.manifest
        if not isinstance(manifest, Mapping) or not isinstance(self._campaign_envelope, Mapping):
            raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_NOT_COMPILED")
        return {
            "draft_id": self.campaign_operator.draft["draft_id"],
            "manifest_digest": manifest["manifest_digest"],
            "envelope_digest": self._campaign_envelope["envelope_digest"],
            "data_disposition": self.campaign_operator.data_disposition,
        }

    def _build_campaign_authorization(self) -> dict[str, Any]:
        if self._campaign_envelope is None:
            raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_NOT_COMPILED")
        now = self.clock().astimezone(timezone.utc)
        return build_campaign_authorization(
            authorization_id=f"{self.session_id}-campaign-authorization",
            operator_label=self.operator_label,
            envelope=self._campaign_envelope,
            approved_at=now.isoformat().replace("+00:00", "Z"),
            expires_at=self.campaign_operator.expires_at,
        )

    def _active_campaign_episode_scope(self) -> dict[str, str]:
        session = self.session.status() if self.session is not None else None
        intent = self._active_intent_projection
        if (
            not isinstance(self._campaign_envelope, Mapping)
            or not isinstance(session, Mapping)
            or session.get("active_child") is not True
            or not isinstance(intent, Mapping)
        ):
            raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH")
        scope = {
            "manifest_digest": self._campaign_envelope["manifest_digest"],
            "intent_digest": intent.get("intent_digest"),
            "run_id": intent.get("run_id"),
            "slot_digest": intent.get("slot_digest"),
            "root_binding_digest": session.get("root_binding_digest"),
            "start_binding_digest": session.get("start_binding_digest"),
        }
        if (
            scope["run_id"] != session.get("active_run_id")
            or scope["intent_digest"] != session.get("active_intent_digest")
        ):
            raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH")
        return scope

    def _authorized_plan_decision(self, request: Mapping[str, Any]) -> dict[str, Any]:
        authorization = self._campaign_authorization
        binding = request.get("decision_binding") if isinstance(request, Mapping) else None
        episode = binding.get("episode_binding") if isinstance(binding, Mapping) else None
        summary = binding.get("operator_summary") if isinstance(binding, Mapping) else None
        session = self.session.status() if self.session is not None else None
        scope = (
            {field: copy.deepcopy(episode[field]) for field in EPISODE_SCOPE_FIELDS}
            if isinstance(episode, Mapping) and EPISODE_SCOPE_FIELDS <= set(episode)
            else None
        )
        if (
            not isinstance(authorization, Mapping)
            or not isinstance(self._campaign_envelope, Mapping)
            or not isinstance(binding, Mapping)
            or not isinstance(scope, Mapping)
            or not isinstance(summary, Mapping)
            or binding.get("data_disposition")
            != self._campaign_envelope["data_disposition"]
            or request.get("approval_scope") != "HIL_NUMERIC_PROXY"
            or not isinstance(summary.get("path"), list)
            or not summary["path"]
            or not all(isinstance(phase, str) and SAFE_ID.fullmatch(phase) for phase in summary["path"])
            or not all(isinstance(summary.get(field), Mapping) for field in ("speed", "clearance"))
            or not isinstance(summary.get("flow"), Mapping)
            or self._active_episode_scope is not None
            and self._active_episode_scope != scope
        ):
            raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH")
        validate_authorized_episode_scope(
            authorization,
            run_id=request.get("run_id"),
            plan_digest=request.get("plan_digest"),
            active_run_id=session.get("active_run_id") if isinstance(session, Mapping) else None,
            active_intent_digest=(
                session.get("active_intent_digest") if isinstance(session, Mapping) else None
            ),
            data_disposition=binding.get("data_disposition"),
            episode_binding=scope,
            expected_envelope=self._campaign_envelope,
            now=self.clock(),
        )
        pending = {
            "plan_digest": request["plan_digest"],
            "approval_scope": request["approval_scope"],
            "decision_binding_digest": canonical_digest({
                "run_id": request["run_id"],
                "plan_digest": request["plan_digest"],
                "approval_scope": request["approval_scope"],
                "decision_binding": binding,
            }),
            "operator_summary": copy.deepcopy(summary),
            "campaign_authorization_digest": authorization["authorization_digest"],
        }
        self._episode_plan = pending
        self._active_episode_scope = copy.deepcopy(scope)
        return {
            "choice": "APPROVE",
            "run_id": request["run_id"],
            "plan_digest": request["plan_digest"],
            "approval_scope": request["approval_scope"],
            "decision_binding_digest": pending["decision_binding_digest"],
            "decision_source": "CAMPAIGN_AUTHORIZATION",
            "operator_label": self.operator_label,
        }

    def _authorized_checkpoint(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        if self._campaign_authorization is None:
            return None
        choices = {
            "PHYSICAL_SCENE_CONFIRMATION": (
                "READY", "CAMPAIGN_AUTHORIZATION",
            ),
            "RELEASE_VERDICT": (
                "LANDED", "CAMPAIGN_CONTROL_PROXY",
            ),
        }
        decision = choices.get(request.get("kind"))
        evidence = request.get("evidence")
        if decision is None or not isinstance(evidence, Mapping):
            return None
        choice, source = decision
        session = self.session.status() if self.session is not None else None
        if request["kind"] == "PHYSICAL_SCENE_CONFIRMATION":
            if self._episode_plan is not None:
                raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH")
            scope = self._active_campaign_episode_scope()
            if (
                not isinstance(evidence.get("checklist"), Mapping)
                or not isinstance(evidence.get("operator_summary"), Mapping)
                or not isinstance(evidence.get("planned_start_evidence_digest"), str)
                or DIGEST.fullmatch(evidence["planned_start_evidence_digest"]) is None
                or evidence.get("data_disposition")
                != self.campaign_operator.data_disposition
            ):
                raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH")
            expected_plan_digest = None
        else:
            if (
                not isinstance(self._active_episode_scope, Mapping)
                or not isinstance(self._episode_plan, Mapping)
            ):
                raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH")
            scope = self._active_episode_scope
            expected_plan_digest = self._episode_plan["plan_digest"]
        validate_authorized_episode_scope(
            self._campaign_authorization,
            run_id=request.get("run_id"),
            plan_digest=request.get("plan_digest"),
            active_run_id=session.get("active_run_id") if isinstance(session, Mapping) else None,
            active_intent_digest=(
                session.get("active_intent_digest") if isinstance(session, Mapping) else None
            ),
            data_disposition=self._campaign_envelope["data_disposition"],
            episode_binding=scope,
            expected_plan_digest=expected_plan_digest,
            expected_envelope=self._campaign_envelope,
            now=self.clock(),
        )
        if request["kind"] == "RELEASE_VERDICT" and (
            not isinstance(evidence.get("release_target"), Mapping)
            or not isinstance(evidence.get("safe_staging_joint_positions_rad"), list)
            or not isinstance(evidence.get("execution_evidence_digest"), str)
            or not DIGEST.fullmatch(evidence["execution_evidence_digest"])
        ):
            raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH")
        bound = {
            key: copy.deepcopy(request[key])
            for key in ("kind", "run_id", "plan_digest", "prompt", "choices", "evidence")
        }
        if choice not in request.get("choices", []):
            raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH")
        if request["kind"] == "PHYSICAL_SCENE_CONFIRMATION":
            self._active_episode_scope = copy.deepcopy(scope)
        return {
            "kind": request["kind"], "choice": choice,
            "run_id": request["run_id"], "plan_digest": request["plan_digest"],
            "checkpoint_binding_digest": canonical_digest(bound),
            "decision_source": source, "operator_label": self.operator_label,
        }

    def _decision_provider(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        if (
            not isinstance(request, Mapping) or set(request) != PLAN_REQUEST_FIELDS
            or request.get("schema_version") != "data_factory.plan_decision_request.v1"
        ):
            raise ContractError("OPERATOR_CONSOLE_PLAN_REQUEST")

        if self.campaign_approval_once:
            with self._lock:
                return self._authorized_plan_decision(request)

        def change():
            offered = self.button_port.offer(
                run_id=request["run_id"], plan_digest=request["plan_digest"],
                decision_binding=request["decision_binding"],
                approval_scope=request["approval_scope"],
            )
            pending = offered["projection"]["pending_plan"]
            plan = copy.deepcopy(dict(pending["decision_binding"]))
            plan["decision_binding_digest"] = pending["decision_binding_digest"]
            self._episode_plan = plan
            self._workflow, self._last_error = "AWAITING_APPROVAL", None

        self._owner_transition(change)
        return self.button_port.wait(request["timeout_s"])

    def _checkpoint_provider(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        automatic = self._authorized_checkpoint(request)
        if automatic is not None:
            return automatic

        def change():
            self.checkpoint_port.offer(request)
            self._workflow, self._last_error = "RUNNING", None

        self._owner_transition(change)
        return self.checkpoint_port.wait(request["timeout_s"])

    def _run_episode(self, intent, lifecycle, cancel_event, episode_context):
        slot = intent.get("slot") if isinstance(intent, Mapping) else None
        base = intent.get("base_condition") if isinstance(intent, Mapping) else None
        condition = base.get("coverage_condition") if isinstance(base, Mapping) else None
        condition_digest = (
            base.get("coverage_condition_digest") if isinstance(base, Mapping) else None
        )
        if (
            not isinstance(slot, Mapping)
            or not isinstance(condition, Mapping)
            or canonical_digest(condition) != condition_digest
        ):
            raise ContractError("OPERATOR_CONSOLE_COVERAGE_BINDING")
        binding = {
            "run_id": intent["run_id"],
            "intent_digest": intent["intent_digest"],
            "order_index": intent["order_index"],
            "slot_id": slot["slot_id"],
            "slot_digest": intent["slot_digest"],
            "base_condition_digest": slot["base_condition_digest"],
            "coverage_condition": copy.deepcopy(dict(condition)),
            "coverage_condition_digest": condition_digest,
        }
        binding["binding_digest"] = canonical_digest(binding)
        with self._lock:
            self._active_intent_projection = binding
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

    def _publish_outcome(self, outcome: Mapping[str, Any]) -> bool:
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
        continue_campaign = bool(
            outcome.get("ok") is True
            and not self._cancel_requested
            and self.campaign_approval_once
            and campaign
            and campaign.get("state") == "READY"
        )
        if self._cancel_requested or campaign and campaign.get("state") == "CANCELLED":
            name, code, workflow = "CANCEL", "PLAN_CANCELLED", "TERMINAL"
            self._measurement_outcome = "NOT_MEASURED"
            self._last_error = code
        elif outcome.get("ok") is True:
            name, code = "PASS", "TECHNICAL_PASS"
            workflow = "RUNNING" if continue_campaign else "TERMINAL"
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
            "episode_ledger": copy.deepcopy(
                result.get("episode_ledger", terminal_data.get("episode_ledger"))
            ),
            "intent_binding": copy.deepcopy(
                result.get("intent_binding", self._active_intent_projection)
            ),
        }
        for field in (
            "one_job", "synthetic_review", "synthetic_coverage_update",
        ):
            value = result.get(field, terminal_data.get(field))
            if value is not None:
                sealed[field] = copy.deepcopy(value)
        review_offer = result.get("candidate_review_offer")
        if review_offer is not None:
            if name != "PASS":
                raise ContractError("OPERATOR_CONSOLE_CANDIDATE_OFFER")
            self._queue_candidate_review(review_offer, sealed)
        terminal_pose = result.get("terminal_object_pose")
        if terminal_pose is not None:
            if (
                not isinstance(terminal_pose, Mapping)
                or set(terminal_pose) != {"place_id", "yaw_deg", "x_mm", "y_mm"}
                or not isinstance(terminal_pose.get("place_id"), str)
                or not SAFE_ID.fullmatch(terminal_pose["place_id"])
                or any(
                    isinstance(terminal_pose.get(field), bool)
                    or not isinstance(terminal_pose.get(field), (int, float))
                    or not math.isfinite(terminal_pose[field])
                    for field in ("yaw_deg", "x_mm", "y_mm")
                )
            ):
                raise ContractError("OPERATOR_CONSOLE_TERMINAL_OBJECT_POSE")
            self._terminal_object_pose = copy.deepcopy(dict(terminal_pose))
            sealed["terminal_object_pose"] = copy.deepcopy(self._terminal_object_pose)
        sealed["result_digest"] = canonical_digest(sealed)
        self._episode_result, self._workflow = sealed, workflow
        self._episode_history.append(copy.deepcopy(sealed))
        if workflow in {"BLOCKED", "TERMINAL"}:
            self._offer_next_candidate_review()
        self._prepared.set()
        return continue_campaign

    def _worker_target(self) -> None:
        while True:
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
                    continuation = self._publish_outcome(outcome)
            else:
                continuation_holder = []

                def publish():
                    continuation_holder.append(self._publish_outcome(outcome))

                self.core.transition(publish)
                continuation = continuation_holder[0]
            if not continuation:
                return
            if self._cancel_requested:
                return
            if initial:
                with self._lock:
                    self._advance_run()
            else:
                self.core.transition(self._advance_run)

    def compile_draft(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        expected = {
            "draft_id": self.campaign_operator.draft["draft_id"],
            "data_disposition": self.campaign_operator.data_disposition,
        }
        with self._lock:
            if (
                payload != expected or self._workflow != "AUTHORING"
                or self._thread is not None or self._checkpoint_projection() is not None
            ):
                raise ContractError("OPERATOR_CONSOLE_COMPILE_FIELDS")
            if self.campaign_operator.manifest is None:
                self.campaign_operator.compile_draft({}, {})
            if self.campaign_approval_once:
                self._campaign_envelope = build_campaign_envelope(
                    source_draft=self.campaign_operator.draft,
                    manifest=self.campaign_operator.manifest,
                    compilation_receipt=self.campaign_operator.compilation_receipt,
                    hypothesis=self.campaign_operator.hypothesis,
                    effect_scope=self.campaign_operator.effect_scope,
                    lifecycle_action=self.campaign_operator.lifecycle_action,
                    data_disposition=self.campaign_operator.data_disposition,
                )
                self._workflow, self._last_error = "REVIEW_CAMPAIGN", None
                return {
                    "outcome": "REVIEW_CAMPAIGN",
                    "manifest_digest": self.campaign_operator.manifest["manifest_digest"],
                    "envelope_digest": self._campaign_envelope["envelope_digest"],
                    "episode_count": len(self.campaign_operator.manifest["slots"]),
                }
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

    def authorize_campaign(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if (
                not self.campaign_approval_once
                or self._workflow != "REVIEW_CAMPAIGN"
                or payload != self._authorization_payload()
                or self._thread is not None
                or self._campaign_authorization is not None
            ):
                raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_AUTHORIZATION")
            self._campaign_authorization = self._build_campaign_authorization()
            self._workflow, self._last_error = "RUNNING", None
            self._thread = threading.Thread(
                target=self._worker_target,
                name=f"operator-console-{self.session_id}",
                daemon=False,
            )
            self._thread.start()
            return {
                "outcome": "RUNNING",
                "active_child_id": self.run_id,
                "campaign_authorization_digest": self._campaign_authorization["authorization_digest"],
            }

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
            "data_disposition": self.campaign_operator.data_disposition,
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

    def review_candidate(self, payload: dict[str, Any], _view=None) -> dict[str, Any]:
        """Resolve one terminal candidate and reproject its existing ledger state."""
        with self._lock:
            if (
                self._workflow not in {"BLOCKED", "TERMINAL"}
                or self.candidate_review_port is None
                or self._active_candidate_review is None
            ):
                raise ContractError("OPERATOR_CONSOLE_CANDIDATE_STATE")
            item = self._active_candidate_review
            resolved = self.candidate_review_port.resolve_deferred(payload)
            try:
                ledger_reference = self.candidate_state_bind_call(
                    item["ledger_reference"], item["candidate_path"],
                )
            except Exception as exc:
                # The candidate CAS may already be durable.  Keep the exact
                # review pending so the same intent can retry only the ledger
                # projection instead of losing or changing the decision.
                raise ContractError("OPERATOR_CONSOLE_CANDIDATE_STATE") from exc
            if (
                ledger_reference.get("review_status") != resolved["status"]
                or ledger_reference.get("training_status") != "NOT_AUTHORIZED"
                or ledger_reference.get("retention_state") != "PRESERVE"
            ):
                raise ContractError("OPERATOR_CONSOLE_CANDIDATE_STATE")
            matches = [
                history for history in self._episode_history
                if history.get("intent_binding", {}).get("run_id") == resolved["run_id"]
            ]
            if len(matches) != 1:
                raise ContractError("OPERATOR_CONSOLE_CANDIDATE_STATE")
            history = matches[0]
            history["episode_ledger"] = copy.deepcopy(ledger_reference)
            history["human_semantic"] = resolved["status"]
            history["result_digest"] = canonical_digest({
                key: value for key, value in history.items()
                if key != "result_digest"
            })
            self.candidate_review_port.acknowledge(
                resolved["review_binding_digest"],
            )
            self._active_candidate_review = None
            self._offer_next_candidate_review()
            return {
                **resolved,
                "remaining_reviews": (
                    len(self._candidate_review_queue)
                    + (1 if self._active_candidate_review is not None else 0)
                ),
            }

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


def discover_uvc_devices(device_root: str | Path = "/dev/v4l/by-id") -> list[dict[str, str]]:
    """Return one passive card per physical UVC identity, never per video node."""
    root = Path(device_root)
    if root.is_symlink() or not root.is_dir():
        return []
    grouped: dict[str, list[tuple[int, Path]]] = {}
    for path in sorted(root.glob("*-video-index*"), key=lambda item: item.name):
        match = re.fullmatch(r"(.+)-video-index(\d+)", path.name)
        if match is None or "realsense" in path.name.lower():
            continue
        try:
            target = path.resolve(strict=True)
            mode = target.stat().st_mode
        except OSError:
            continue
        if path.is_symlink() and stat.S_ISCHR(mode):
            grouped.setdefault(match.group(1), []).append((int(match.group(2)), path))
    result = []
    for stem, nodes in sorted(grouped.items()):
        _index, canonical = min(nodes, key=lambda item: (item[0] != 0, item[0]))
        label = stem.removeprefix("usb-").replace("_", " ").strip() or "USB camera"
        result.append({
            "logical_id": canonical.name,
            "label": label,
            "status": "CONNECTED",
            "kind": "UVC",
            "capture_endpoint": str(Path("/dev/v4l/by-id") / canonical.name),
        })
    return result


def discover_uvc_device_ids(device_root: str | Path = "/dev/v4l/by-id") -> list[str]:
    """Compatibility projection of stable physical UVC identities."""
    return [item["logical_id"] for item in discover_uvc_devices(device_root)]


def query_realsense_serials(command_call=None) -> list[str]:
    """Passively enumerate librealsense serials; absence is not a fabricated device."""
    command_call = command_call or subprocess.run
    try:
        completed = command_call(
            ["rs-enumerate-devices", "-s", "--no-dds"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0 or not isinstance(completed.stdout, str):
        return []
    output = completed.stdout
    serials = re.findall(
        r"(?m)^\s*Serial Number\s*:\s*([A-Za-z0-9_.-]+)\s*$", output,
    )
    if not serials:
        lines = output.splitlines()
        header = next((line for line in lines if "Serial Number" in line), None)
        if header is not None and "Firmware Version" in header:
            start, end = header.index("Serial Number"), header.index("Firmware Version")
            serials = [
                line[start:end].strip() for line in lines[lines.index(header) + 1:]
                if line[start:end].strip()
            ]
    return sorted({serial for serial in serials if SAFE_ID.fullmatch(serial)})


def discover_camera_devices(
    device_root: str | Path = "/dev/v4l/by-id", *, realsense_query=None,
) -> list[dict[str, str]]:
    """Return one passive logical descriptor per UVC or RealSense camera."""
    devices = discover_uvc_devices(device_root)
    realsense_query = realsense_query or query_realsense_serials
    try:
        serials = realsense_query()
    except (OSError, ContractError):
        serials = []
    if isinstance(serials, (str, bytes)) or not isinstance(serials, Sequence):
        serials = []
    for serial in sorted(set(serials)):
        if not isinstance(serial, str) or SAFE_ID.fullmatch(serial) is None:
            continue
        devices.append({
            "logical_id": serial, "label": "RealSense camera",
            "status": "CONNECTED", "kind": "REALSENSE",
            "capture_endpoint": serial,
        })
    devices = normalize_camera_devices(devices)
    counts: dict[str, int] = {}
    for device in devices:
        counts[device["kind"]] = counts.get(device["kind"], 0) + 1
        prefix = "RealSense" if device["kind"] == "REALSENSE" else "USB"
        device["label"] = f"{prefix} camera {counts[device['kind']]}"
    return devices


def _v2_camera_profiles(repository: Path) -> dict[str, dict[str, Any]]:
    profiles = {}
    directory = repository / "config/data_factory/collection_profiles"
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        value = load_json_strict(path)
        roles = value.get("camera_roles")
        if (
            value.get("schema_version") != "data_factory.collection_profile.v2"
            or not isinstance(roles, list) or not roles
            or len(roles) != len(set(roles))
            or any(role not in {"up", "side", "wrist"} for role in roles)
            or not isinstance(value.get("camera_serials"), Mapping)
            or set(value["camera_serials"]) != set(roles)
            or not isinstance(value.get("camera_topics"), Mapping)
            or set(value["camera_topics"]) != set(roles)
        ):
            continue
        profiles[value["collection_profile_id"]] = value
    return profiles


def resolve_camera_setup(
    *, repository_root: str | Path, devices: Sequence[object],
    preferred_profile_id: str,
    requested_bindings: Mapping[str, str] | None = None,
    persist: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Resolve camera cards and roles to a profile without opening any device."""
    repository = Path(repository_root).resolve(strict=True)
    logical_devices = normalize_camera_devices(devices)
    cards = [
        {key: item[key] for key in ("logical_id", "label", "status")}
        for item in logical_devices
    ]
    device_ids = [card["logical_id"] for card in cards]
    profiles = _v2_camera_profiles(repository)
    preferred = profiles.get(preferred_profile_id)
    preferred_roles = (
        [role.upper() for role in preferred["camera_roles"]]
        if preferred is not None else []
    )
    available_roles = sorted({
        role.upper() for profile in profiles.values()
        if len(profile["camera_roles"]) <= len(cards)
        for role in profile["camera_roles"]
    }) + ["UNUSED"]

    def profile_label(profile: Mapping[str, Any] | None) -> str:
        if profile is None:
            return "카메라 역할 선택"
        labels = {"up": "상단", "side": "측면", "wrist": "손목"}
        return " + ".join(labels[role] for role in profile["camera_roles"]) + " 카메라"

    def view(
        status: str, assignments: Mapping[str, str], reason: str | None,
        required_roles: Sequence[str] = preferred_roles,
        selected_profile: Mapping[str, Any] | None = preferred,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "profile_label": profile_label(selected_profile),
            "devices": copy.deepcopy(cards),
            "bindings": {
                device: assignments.get(device, "UNUSED") for device in device_ids
            },
            "required_roles": list(required_roles),
            "available_roles": available_roles,
        }

    if not cards:
        return view("NO_CAMERA_CONNECTED", {}, "DEVICE_NOT_CONNECTED"), None

    def resolve(assignments: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if set(assignments) != set(device_ids) or any(
            role not in {"UP", "SIDE", "WRIST", "UNUSED"}
            for role in assignments.values()
        ):
            raise ContractError("CAMERA_SETUP_BINDINGS")
        used_roles = sorted(role.lower() for role in assignments.values() if role != "UNUSED")
        matches = []
        for profile in profiles.values():
            if sorted(profile["camera_roles"]) != used_roles:
                continue
            role_devices = {
                role.lower(): device
                for device, role in assignments.items() if role != "UNUSED"
            }
            if all(
                isinstance(profile["camera_serials"].get(role), str)
                and (
                    profile["camera_serials"][role] == "RUNTIME_BINDING_REQUIRED"
                    or profile["camera_serials"][role] in role_devices[role]
                )
                for role in used_roles
            ):
                matches.append(profile)
        if preferred in matches:
            matches = [preferred]
        if len(matches) != 1:
            reason = (
                "CAMERA_PROFILE_NOT_AVAILABLE" if not matches
                else "CAMERA_PROFILE_AMBIGUOUS"
            )
            return view("BINDING_REQUIRED", assignments, reason, [], None), None
        profile = matches[0]
        receipt = build_camera_role_bindings(
            collection_profile=profile, discovered_device_ids=logical_devices,
            assignments=assignments,
        )
        if persist:
            write_camera_role_bindings(receipt, repository_root=repository)
        return view(
            "READY", assignments, None,
            [role.upper() for role in profile["camera_roles"]],
            profile,
        ), {"collection_profile": profile, "role_bindings": receipt}

    if requested_bindings is not None:
        return resolve(dict(requested_bindings))

    stored = None
    try:
        stored = load_camera_role_bindings(repository_root=repository)
    except ContractError:
        pass
    if stored is not None:
        profile = profiles.get(stored["collection_profile_id"])
        if profile is not None:
            try:
                restored = reuse_camera_role_bindings(
                    stored, discovered_device_ids=logical_devices,
                    collection_profile=profile,
                )
            except ContractError:
                restored = None
            if restored is not None:
                return view(
                    "READY", restored["assignments"], None,
                    [role.upper() for role in profile["camera_roles"]],
                    profile,
                ), {"collection_profile": profile, "role_bindings": restored}
        return view(
            "BINDING_REQUIRED", {device: "UNUSED" for device in device_ids},
            "SAVED_CAMERA_BINDING_NOT_AVAILABLE",
        ), None

    # Preserve the established single-camera receipt without rewriting it.
    try:
        legacy = load_camera_binding_receipt(repository_root=repository)
        profile = profiles.get(legacy["collection_profile_id"])
        if profile is not None and len(profile["camera_roles"]) == 1:
            restored = reuse_camera_binding_receipt(
                legacy, discovered_device_ids=logical_devices,
                collection_profile=profile,
            )
            assignments = {device: "UNUSED" for device in device_ids}
            assignments[restored["stable_device_id"]] = restored["intended_role"].upper()
            return resolve(assignments)
    except ContractError:
        pass

    if preferred is not None and len(device_ids) == 1 and len(preferred["camera_roles"]) == 1:
        role = preferred["camera_roles"][0].upper()
        return resolve({device_ids[0]: role})
    return view(
        "BINDING_REQUIRED", {device: "UNUSED" for device in device_ids},
        "ROLE_ASSIGNMENT_REQUIRED",
    ), None


def _camera_binding(
    repository: Path, profile: Mapping[str, Any], *, selected_device_id: str | None,
    discovery_call: Callable[[], list[str]],
) -> dict[str, Any]:
    roles = profile.get("camera_roles")
    if not isinstance(roles, list) or len(roles) != 1 or not isinstance(roles[0], str):
        raise ContractError("PHYSICAL_CAMERA_ROLE_BINDING_REQUIRED")
    intended_role = roles[0]
    discovered = discovery_call()
    receipt_path = repository / "outputs/data_factory/operator_setup/camera_binding.json"
    if receipt_path.is_file() and selected_device_id is None:
        return reuse_camera_binding_receipt(
            load_camera_binding_receipt(repository_root=repository),
            discovered_device_ids=discovered, collection_profile=profile,
        )
    binding = build_camera_binding_from_discovery(
        binding_id=(
            "camera-"
            + canonical_digest({
                "profile": profile.get("collection_profile_id"),
                "role": intended_role,
                "device": selected_device_id,
            }).removeprefix("sha256:")[:20]
        ),
        device_kind="UVC",
        discovered_device_ids=discovered, selected_device_id=selected_device_id,
        intended_role=intended_role, collection_profile=profile,
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
            ["ros2", "node", "list", "--no-daemon"], "GRIPPER_SETUP_NODE_GRAPH",
        ).splitlines()
        if line.strip()
    )
    command_server = "/fr_command_server" in nodes
    controller_listing = (
        ""
        if command_server and "/controller_manager" not in nodes
        else _readonly_command(
            ["ros2", "control", "list_controllers"],
            "GRIPPER_SETUP_CONTROLLER_GRAPH",
        )
    )
    controllers = _controller_names(controller_listing)
    normal = {
        "fairino5_controller", "gripper_controller", "joint_state_broadcaster",
    } <= controllers
    if normal and command_server:
        raise ContractError("PHYSICAL_SECOND_MOTION_OWNER")
    if normal:
        output = _readonly_command([
            "ros2", "topic", "echo", "/gripper_controller/controller_state",
            "control_msgs/msg/JointTrajectoryControllerState", "--once",
            "--timeout", "2", "--flow-style", "--no-daemon",
        ], "GRIPPER_SETUP_READBACK")
        try:
            import yaml
        except ImportError as exc:
            raise ContractError("GRIPPER_SETUP_READBACK") from exc
        message_start = re.search(r"(?m)^joint_names:\s*", output)
        if message_start is None:
            raise ContractError("GRIPPER_SETUP_READBACK")
        try:
            message = next(yaml.safe_load_all(output[message_start.start():]))
            names = message["joint_names"]
            reference = message["reference"]["positions"]
            feedback = message["feedback"]["positions"]
            if names != ["finger_right_joint"] or len(reference) != 1 or len(feedback) != 1:
                raise ValueError
            reference_m, feedback_m = float(reference[0]), float(feedback[0])
        except (KeyError, StopIteration, TypeError, ValueError, yaml.YAMLError) as exc:
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
    if command_server and not controller_listing.strip():
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


def normalize_gripper_after_operator_ready(
    readback: Mapping[str, Any], *, settle_call: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
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
        activation = _remote_gripper_command(
            "GetGripperActivateStatus()", expected_fields=3,
        )
        if activation[0] != 0:
            raise ContractError("GRIPPER_MAINTENANCE_ACTION")
        if activation[1] != 0:
            if _remote_gripper_command(
                "ResetAllError()", expected_fields=1,
            ) != [0]:
                raise ContractError("GRIPPER_MAINTENANCE_ACTION")
            activation = _remote_gripper_command(
                "GetGripperActivateStatus()", expected_fields=3,
            )
            if activation[0] != 0 or activation[1] != 0:
                raise ContractError("GRIPPER_MAINTENANCE_ACTION")
        if activation[2] & 1 != 1:
            if _remote_gripper_command(
                "ActGripper(1,0)", expected_fields=1,
            ) != [0]:
                raise ContractError("GRIPPER_MAINTENANCE_ACTION")
            settle_call(1.0)
            if _remote_gripper_command(
                "ActGripper(1,1)", expected_fields=1,
            ) != [0]:
                raise ContractError("GRIPPER_MAINTENANCE_ACTION")
            settle_call(2.0)
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
    device_kind: str = "UVC", capture_endpoint: str | None = None,
    device_root: str | Path = "/dev/v4l/by-id",
    discovery_call: Callable[[], Sequence[object]] = discover_uvc_device_ids,
) -> dict[str, Any]:
    """Attach only to an already-running graph; perform no lifecycle mutation."""
    def read_graph(args: list[str], code: str) -> str:
        for attempt in range(3):
            try:
                return _readonly_command(args, code)
            except ContractError:
                if attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))
        raise AssertionError("unreachable")

    discovered = normalize_camera_devices(
        discovery_call(), default_kind=device_kind,
    )
    matches = [
        item for item in discovered
        if item["logical_id"] == discovered_device_id
        and item["kind"] == device_kind
    ]
    if not matches:
        raise ContractError("PHYSICAL_CAMERA_BINDING")
    if len(matches) != 1:
        raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
    endpoint = capture_endpoint or matches[0]["capture_endpoint"]
    if endpoint != matches[0]["capture_endpoint"]:
        raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
    stable_target = None
    if device_kind == "UVC":
        stable_path = Path(device_root) / discovered_device_id
        try:
            stable_target = stable_path.resolve(strict=True)
        except OSError as exc:
            raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH") from exc
        if not stable_path.is_symlink() or not stat.S_ISCHR(stable_target.stat().st_mode):
            raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
    controllers = read_graph(
        ["ros2", "control", "list_controllers"], "PHYSICAL_CONTROLLER_GRAPH",
    )
    for name in ("fairino5_controller", "gripper_controller", "joint_state_broadcaster"):
        if not any(line.split()[:1] == [name] and "active" in line.split() for line in controllers.splitlines()):
            raise ContractError("PHYSICAL_CONTROLLER_STATE_MISMATCH")
    nodes = read_graph(
        ["ros2", "node", "list", "--no-daemon"], "PHYSICAL_NODE_GRAPH",
    )
    if "/fr_command_server" in nodes.splitlines():
        raise ContractError("PHYSICAL_SECOND_MOTION_OWNER")
    if read_graph(
        ["ros2", "topic", "type", "/joint_states", "--no-daemon"],
        "PHYSICAL_JOINT_TOPIC",
    ).strip() != "sensor_msgs/msg/JointState":
        raise ContractError("PHYSICAL_JOINT_TOPIC_MISMATCH")
    if read_graph(
        ["ros2", "topic", "type", camera_topic, "--no-daemon"],
        "PHYSICAL_CAMERA_TOPIC",
    ).strip() != "sensor_msgs/msg/Image":
        raise ContractError("PHYSICAL_CAMERA_TOPIC_MISMATCH")
    if device_kind == "UVC":
        reported = read_graph(
            [
                "ros2", "param", "get", camera_node, "video_device",
                "--hide-type", "--no-daemon",
            ],
            "PHYSICAL_CAMERA_DEVICE_PARAMETER",
        ).strip()
        try:
            configured_target = Path(reported).resolve(strict=True)
        except OSError as exc:
            raise ContractError("PHYSICAL_CAMERA_DEVICE_MISMATCH") from exc
        if configured_target != stable_target:
            raise ContractError("PHYSICAL_CAMERA_DEVICE_MISMATCH")
        resolved_device = str(stable_target)
    elif device_kind == "REALSENSE":
        parameters = {
            name: read_graph(
                [
                    "ros2", "param", "get", camera_node, name,
                    "--hide-type", "--no-daemon",
                ],
                "PHYSICAL_CAMERA_DEVICE_PARAMETER",
            ).strip().strip('"')
            for name in ("serial_no", "enable_color", "enable_depth")
        }
        if (
            parameters["serial_no"].lstrip("_") != discovered_device_id
            or parameters["enable_color"].lower() != "true"
            or parameters["enable_depth"].lower() != "false"
        ):
            raise ContractError("PHYSICAL_CAMERA_DEVICE_MISMATCH")
        reported = parameters["serial_no"]
        resolved_device = endpoint
    else:
        raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
    evidence = {
        "schema_version": "data_factory.test_only_camera_transport_binding.v1",
        "device_kind": device_kind,
        "stable_device_id": discovered_device_id,
        "resolved_device": resolved_device,
        "camera_node": camera_node,
        "camera_topic": camera_topic,
        "reported_video_device": reported,
        "topic_type": "sensor_msgs/msg/Image",
        "authority": "TEST_ONLY_TRANSPORT",
    }
    evidence["binding_digest"] = canonical_digest(evidence)
    return evidence


def capture_home_snapshot(
    *, tcp_candidate_manifest: Path, max_age_s: float = 0.1,
) -> dict[str, Any]:
    """Capture a fresh ROS snapshot with one bounded transient retry."""
    command = [
        sys.executable, "-m", "tools.data_factory.motion.pose_snapshot", "capture",
        "--timeout-s", "2", "--max-age-s", str(max_age_s),
        "--tcp-candidate-manifest", str(tcp_candidate_manifest),
    ]
    for attempt in range(2):
        try:
            output = _readonly_command(command, "PHYSICAL_HOME_SNAPSHOT")
        except ContractError:
            if attempt:
                raise
        else:
            return load_json_strict(output.strip())
    raise AssertionError("unreachable")


def _repository_path(repository: Path, value: str | Path) -> Path:
    path = Path(value)
    path = (repository / path).resolve(strict=True) if not path.is_absolute() else path.resolve(strict=True)
    try:
        path.relative_to(repository)
    except ValueError as exc:
        raise ContractError("PHYSICAL_CONSOLE_PATH") from exc
    return path


def _resolve_physical_pose_domain(
    *, template_job: Mapping[str, Any], poses: Sequence[Mapping[str, Any]],
    operator_label: str, payload_template: Mapping[str, Any],
    sheet_manifest: Path, resolver=None,
) -> list[dict[str, Any]]:
    """Resolve each exact pose with the ordinary JobSpec/motion input path."""
    if not isinstance(poses, (list, tuple)) or not poses:
        raise ContractError("PHYSICAL_CONSOLE_POSE_DOMAIN")
    sheet_digest = canonical_digest(load_json_strict(sheet_manifest))
    result = []
    seen = set()
    resolver = run_job.resolve_inputs if resolver is None else resolver
    if not callable(resolver):
        raise ContractError("PHYSICAL_CONSOLE_RESOLVER")
    for pose in poses:
        if not isinstance(pose, Mapping) or set(pose) != {
            "place_id", "yaw_deg", "x_mm", "y_mm",
        }:
            raise ContractError("PHYSICAL_CONSOLE_POSE_DOMAIN")
        token = canonical_digest(dict(pose)).removeprefix("sha256:")[:20]
        job = copy.deepcopy(dict(template_job))
        job.update(
            job_id=f"physical-pose-{token}",
            operator_or_agent_id=operator_label,
            sheet_manifest_digest=sheet_digest,
            **copy.deepcopy(dict(pose)),
        )
        candidate_payload = copy.deepcopy(dict(payload_template))
        candidate_payload["job"] = job
        resolved, program, _binding = resolver(
            candidate_payload, scene_binding_call=lambda *_args: {},
        )
        key = resolved["resolved_job_digest"]
        if key in seen:
            continue
        if (
            program.get("schema_version") != "fr5.motion_program.v2"
            or len(program.get("steps", [])) != 10
        ):
            raise ContractError("PHYSICAL_CONSOLE_EXACT_SCOPE")
        seen.add(key)
        result.append(resolved)
    return result


def build_physical_operator_console(
    *, repository_root: str | Path, session_id: str, run_id: str,
    operator_label: str, job_path: str | Path = DEFAULT_JOB,
    yaw0_sheet: str | Path = DEFAULT_YAW0,
    motion_qualification_path: str | Path = DEFAULT_MOTION,
    home_candidate_path: str | Path = DEFAULT_HOME,
    collection_profile_path: str | Path = DEFAULT_PROFILE,
    urdf_path: str | Path = DEFAULT_URDF,
    tcp_candidate_manifest: str | Path = DEFAULT_TCP_MANIFEST,
    gripper_retune_path: str | Path = DEFAULT_GRIPPER_RETUNE,
    selected_camera_device_id: str | None = None,
    selected_camera_bindings: Mapping[str, str] | None = None,
    selected_camera_binding_digest: str | None = None,
    selected_camera_binding_set: Mapping[str, Any] | None = None,
    discovery_call: Callable[[], Sequence[object]] = discover_uvc_device_ids,
    activation_call: Callable[[], bool | Mapping[str, Any]] | None = None,
    snapshot_call: Callable[[], Mapping[str, Any]] | None = None,
    gripper_readback_call: Callable[[], Mapping[str, Any]] | None = None,
    gripper_maintenance_call: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    run_live_call: Callable[..., Mapping[str, Any]] = run_job.run_live,
    requested_count: int = 1, normalized_seed: int = 0,
    candidate_poses: Sequence[Mapping[str, Any]] | None = None,
    direct_pose_sequence: Sequence[Mapping[str, Any]] | None = None,
    direct_start_pose_ids: Sequence[str] | None = None,
    selected_start_pose_qualifications: Sequence[Mapping[str, Any]] | None = None,
    start_transition_call: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None,
    initial_object_pose: Mapping[str, Any] | None = None,
    environment_prepared: bool = False,
    clock=None,
) -> tuple[OperatorConsole, dict[str, Any]]:
    """Compose a finite registered-workspace TEST_ONLY campaign without activation."""
    repository = Path(repository_root).resolve(strict=True)
    clock = clock or (lambda: datetime.now(timezone.utc))
    paths = {
        "job": _repository_path(repository, job_path),
        "yaw0_sheet": _repository_path(repository, yaw0_sheet),
        "motion": _repository_path(repository, motion_qualification_path),
        "home": _repository_path(repository, home_candidate_path),
        "profile": _repository_path(repository, collection_profile_path),
        "urdf": _repository_path(repository, urdf_path),
        "tcp": _repository_path(repository, tcp_candidate_manifest),
        "gripper_retune": _repository_path(repository, gripper_retune_path),
    }
    template_job = load_json_strict(paths["job"])
    template_job["operator_or_agent_id"] = operator_label
    configured_profile = load_json_strict(paths["profile"])
    if (
        configured_profile.get("schema_version")
        != "data_factory.collection_profile.v2"
        or not isinstance(configured_profile.get("camera_profile"), str)
        or not configured_profile["camera_profile"]
    ):
        raise ContractError("PHYSICAL_CONSOLE_COLLECTION_PROFILE")
    # TEST_ONLY derives the runtime JobSpec from the selected profile in memory;
    # the source job and its production digest remain unchanged on disk.
    template_job["collection_profile_id"] = configured_profile[
        "collection_profile_id"
    ]
    default_pose = {
        key: template_job[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
    }
    initial_pose = copy.deepcopy(dict(initial_object_pose or default_pose))
    if set(initial_pose) != set(default_pose):
        raise ContractError("PHYSICAL_CONSOLE_SEQUENCE_ANCHOR")
    if direct_pose_sequence is not None:
        if (
            not isinstance(direct_pose_sequence, (list, tuple))
            or len(direct_pose_sequence) != requested_count
            or not direct_pose_sequence
            or dict(direct_pose_sequence[0]) != initial_pose
            or direct_start_pose_ids is not None
            and (
                not isinstance(direct_start_pose_ids, (list, tuple))
                or len(direct_start_pose_ids) != requested_count
            )
        ):
            raise ContractError("PHYSICAL_CONSOLE_SEQUENCE_ANCHOR")
        pose_domain = [copy.deepcopy(dict(item)) for item in direct_pose_sequence]
    else:
        if direct_start_pose_ids is not None:
            raise ContractError("PHYSICAL_CONSOLE_DIRECT_SEQUENCE")
        pose_domain = [
            copy.deepcopy(dict(item))
            for item in (candidate_poses or [initial_pose])
        ]
        if initial_pose not in pose_domain:
            pose_domain.insert(0, initial_pose)
    payload = {
        "mode": "live", "run_id": run_id, "job": template_job,
        "selected_sheet": str(paths["yaw0_sheet"]),
        "yaw0_sheet": str(paths["yaw0_sheet"]),
        "config_root": str(repository / "config/data_factory"),
        "motion_qualification": str(paths["motion"]),
        "home_candidate": str(paths["home"]), "urdf": str(paths["urdf"]),
        "expected_robot_system_id": template_job["robot_system_id"],
        "camera_profile": configured_profile["camera_profile"],
    }
    retune = load_json_strict(paths["gripper_retune"])
    motion_qualification = load_json_strict(paths["motion"])

    def test_only_resolver(value, *, scene_binding_call):
        resolved, program, binding = run_job.resolve_inputs(
            value, scene_binding_call=scene_binding_call,
        )
        return resolved, _derive_test_only_gripper_program(
            resolved, motion_qualification, program, retune,
        ), binding

    resolved_jobs = _resolve_physical_pose_domain(
        template_job=template_job, poses=pose_domain,
        operator_label=operator_label, payload_template=payload,
        sheet_manifest=paths["yaw0_sheet"],
        resolver=test_only_resolver,
    )
    resolved_by_pose = {
        tuple(item["normalized_job"][key] for key in (
            "place_id", "yaw_deg", "x_mm", "y_mm",
        )): item for item in resolved_jobs
    }
    initial_key = tuple(initial_pose[key] for key in (
        "place_id", "yaw_deg", "x_mm", "y_mm",
    ))
    resolved = resolved_by_pose.get(initial_key)
    if resolved is None:
        raise ContractError("PHYSICAL_CONSOLE_SEQUENCE_ANCHOR")
    direct_digests = None
    if direct_pose_sequence is not None:
        direct_digests = []
        for pose in direct_pose_sequence:
            normalized_key = (
                pose["place_id"], normalize_yaw_deg(pose["yaw_deg"]),
                float(pose["x_mm"]), float(pose["y_mm"]),
            )
            matched = next((
                item for key, item in resolved_by_pose.items()
                if key[0] == normalized_key[0]
                and all(float(key[index]) == normalized_key[index] for index in range(1, 4))
            ), None)
            if matched is None:
                raise ContractError("PHYSICAL_CONSOLE_DIRECT_SEQUENCE")
            direct_digests.append(matched["resolved_job_digest"])
    profile = resolved["collection_profile"]
    if canonical_digest(profile) != canonical_digest(configured_profile):
        raise ContractError("PHYSICAL_CONSOLE_COLLECTION_PROFILE")
    discovered_cameras = normalize_camera_devices(discovery_call())
    discovered_devices = [item["logical_id"] for item in discovered_cameras]
    if selected_camera_bindings is None:
        legacy_binding = _camera_binding(
            repository, profile, selected_device_id=selected_camera_device_id,
            discovery_call=lambda: discovered_devices,
        )
        role_device_ids = {
            legacy_binding["intended_role"]: legacy_binding["stable_device_id"],
        }
    else:
        role_device_ids = copy.deepcopy(dict(selected_camera_bindings))
        if (
            set(role_device_ids) != set(profile["camera_roles"])
            or any(discovered_devices.count(device) != 1 for device in role_device_ids.values())
            or len(set(role_device_ids.values())) != len(role_device_ids)
        ):
            raise ContractError("PHYSICAL_CAMERA_ROLE_BINDING_REQUIRED")
    role_map_digest = camera_binding_digest(profile, role_device_ids)
    if (
        selected_camera_binding_digest is not None
        and selected_camera_binding_digest != role_map_digest
    ):
        raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
    assignments = {device: "UNUSED" for device in discovered_devices}
    assignments.update({device: role.upper() for role, device in role_device_ids.items()})
    if selected_camera_binding_set is not None:
        camera_binding_set = reuse_camera_role_bindings(
            selected_camera_binding_set,
            discovered_device_ids=discovered_cameras,
            collection_profile=profile,
        )
        if {
            role: binding["stable_device_id"]
            for role, binding in camera_binding_set["bindings"].items()
        } != role_device_ids:
            raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
    elif selected_camera_bindings is None:
        camera_binding_set = {
            "schema_version": "data_factory.camera_role_bindings.v1",
            "collection_profile_id": profile["collection_profile_id"],
            "collection_profile_digest": canonical_digest(profile),
            "devices": {
                device: {
                    "kind": descriptor["kind"],
                    "capture_endpoint": descriptor["capture_endpoint"],
                }
                for device, descriptor in (
                    (item["logical_id"], item) for item in discovered_cameras
                )
            },
            "assignments": dict(sorted(assignments.items())),
            "bindings": {legacy_binding["intended_role"]: legacy_binding},
        }
        camera_binding_set["binding_digest"] = canonical_digest(camera_binding_set)
        camera_binding_set = validate_camera_role_bindings(camera_binding_set)
    else:
        camera_binding_set = build_camera_role_bindings(
            collection_profile=profile, discovered_device_ids=discovered_cameras,
            assignments=assignments,
        )
    camera_bindings = camera_binding_set["bindings"]
    read_gripper = gripper_readback_call or capture_gripper_setup_readback
    maintain_gripper = gripper_maintenance_call or normalize_gripper_after_operator_ready
    if type(environment_prepared) is not bool:
        raise ContractError("PHYSICAL_CONSOLE_ENVIRONMENT")
    first_roots = build_test_only_root_binding(
        repository, session_id=session_id, run_id=run_id,
    )
    state_initialization = initialize_test_only_state_from_user_declaration(
        first_roots, repository_root=repository,
        robot_system_id=resolved["normalized_job"]["robot_system_id"],
        object_instance_id=(
            "test-object-"
            + canonical_digest({
                "session_id": session_id,
                "object_profile_id": resolved["normalized_job"]["object_profile_id"],
            }).removeprefix("sha256:")[:20]
        ),
        object_profile_id=resolved["normalized_job"]["object_profile_id"],
        place_id=resolved["normalized_job"]["place_id"],
        yaw_deg=resolved["normalized_job"]["yaw_deg"],
        x_mm=resolved["normalized_job"]["x_mm"],
        y_mm=resolved["normalized_job"]["y_mm"],
        declared_by=operator_label,
    )
    home_candidate = load_json_strict(paths["home"])
    live_start_qualifications: dict[str, dict[str, Any]] = {}
    campaign_start_qualifications = None
    if selected_start_pose_qualifications is not None:
        campaign_start_qualifications = []
        for source_value in selected_start_pose_qualifications:
            if not isinstance(source_value, Mapping):
                raise ContractError("PHYSICAL_CONSOLE_START_POSE")
            qualification = copy.deepcopy(dict(source_value))
            identifier = qualification.get("robot_start_pose_id")
            if (
                not isinstance(identifier, str) or not SAFE_ID.fullmatch(identifier)
                or identifier in live_start_qualifications
                or qualification.get("home_candidate_digest")
                != canonical_digest(home_candidate)
            ):
                raise ContractError("PHYSICAL_CONSOLE_START_POSE")
            if qualification.get("source") == "QUALIFICATION_ARTIFACT":
                live_start_qualifications[identifier] = copy.deepcopy(qualification)
            qualification["source"] = "SYNTHETIC_TEST_ONLY"
            qualification["qualification_digest"] = canonical_digest({
                key: value for key, value in qualification.items()
                if key != "qualification_digest"
            })
            campaign_start_qualifications.append(qualification)
        campaign_start_qualifications.sort(
            key=lambda value: value["robot_start_pose_id"],
        )
    hypothesis, draft = _build_physical_campaign_contract(
        resolver_results=resolved_jobs, motion_qualification=motion_qualification,
        home_candidate=home_candidate,
        scene_digest=state_initialization["scene_state_digest"],
        draft_id=f"{session_id}-draft", manifest_id=f"{session_id}-manifest",
        requested_count=requested_count,
        normalized_seed=normalized_seed,
        anchor_resolved_job_digest=(
            None if direct_digests is not None else resolved["resolved_job_digest"]
        ),
        direct_resolved_job_digests=direct_digests,
        direct_start_pose_ids=direct_start_pose_ids,
        start_pose_qualifications=campaign_start_qualifications,
        test_only_gripper_retune_digest=retune["retune_digest"],
    )
    resolved_by_digest = {
        item["resolved_job_digest"]: item
        for item in hypothesis["resolver_receipts"]
    }
    campaign_run_ids = [
        run_id if index == 0 else f"{run_id}-e{index + 1}"
        for index in range(requested_count)
    ]
    if (
        len(set(campaign_run_ids)) != requested_count
        or any(len(value) > 128 or SAFE_ID.fullmatch(value) is None for value in campaign_run_ids)
    ):
        raise ContractError("PHYSICAL_CONSOLE_RUN_ID")

    def run_id_for(index: int) -> str:
        if type(index) is not int or not 0 <= index < len(campaign_run_ids):
            raise ContractError("PHYSICAL_CONSOLE_RUN_ID")
        return campaign_run_ids[index]

    def roots_for(active_run_id: str) -> dict[str, Any]:
        if active_run_id not in campaign_run_ids:
            raise ContractError("PHYSICAL_CONSOLE_RUN_ID")
        return build_test_only_root_binding(
            repository, session_id=session_id, run_id=active_run_id,
        )

    counters = {name: 0 for name in SIDE_EFFECT_COUNTERS}
    holder: dict[str, Any] = {}

    def campaign_camera_warmup(
        active_payload: Mapping[str, Any], active_profile: Mapping[str, Any],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        """Measure once per exact camera binding; keep episode readiness fresh."""
        return _campaign_camera_warmup(
            cache=holder, transport=holder.get("camera_transport_evidence"),
            payload=active_payload, profile=active_profile,
            cancel=cancel_event, measure_call=run_job._camera_warmup,
        )

    def physical_gate_evidence() -> dict[str, Any]:
        try:
            value = activation_call() if activation_call is not None else None
            if activation_call is None:
                transports = {}
                for role in profile["camera_roles"]:
                    binding = camera_bindings[role]
                    transports[role] = passive_physical_gate(
                        camera_topic=profile["camera_topics"][role],
                        camera_node=(
                            f"/camera/{role}/color/uvc_{role}_camera"
                            if binding["device_kind"] == "UVC"
                            else f"/camera/{role}"
                        ),
                        device_kind=binding["device_kind"],
                        capture_endpoint=binding["capture_endpoint"],
                        discovered_device_id=binding["stable_device_id"],
                        discovery_call=lambda: discovered_cameras,
                    )
                value = {
                    "schema_version": "data_factory.test_only_camera_transport_set.v1",
                    "camera_binding_digest": role_map_digest,
                    "roles": transports,
                    "status": "PASSIVE_GRAPH_VERIFIED",
                }
                value["binding_digest"] = canonical_digest(value)
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError("CAMPAIGN_OPERATOR_PHYSICAL_ACTIVATION_FAILED") from exc
        if value is True:
            evidence = {
                "schema_version": "data_factory.test_only_camera_transport_set.v1",
                "camera_binding_digest": role_map_digest,
                "roles": {
                    role: {
                        "stable_device_id": binding["stable_device_id"],
                        "status": "INJECTED_TEST_GATE",
                    }
                    for role, binding in camera_bindings.items()
                },
                "status": "INJECTED_TEST_GATE",
            }
            evidence["binding_digest"] = canonical_digest(evidence)
            return evidence
        if not isinstance(value, Mapping):
            raise ContractError("CAMPAIGN_OPERATOR_PHYSICAL_ACTIVATION_FAILED")
        evidence = copy.deepcopy(dict(value))
        if (
            evidence.get("camera_binding_digest") != role_map_digest
            or set(evidence.get("roles", {})) != set(camera_bindings)
            or evidence.get("binding_digest")
            != canonical_digest({
                key: item for key, item in evidence.items()
                if key != "binding_digest"
            })
        ):
            raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
        return evidence

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

    initial_gripper = (
        {
            "state": "ATTACHED", "supported_action": "VERIFY_AT_RUN",
            "maintenance_call_count": 0,
        }
        if environment_prepared else refresh_gripper()
    )
    holder["gripper_projection"] = copy.deepcopy(initial_gripper)
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
    if (
        not environment_prepared
        and setup_request is None
        and initial_block_code is None
    ):
        try:
            holder["camera_transport_evidence"] = physical_gate_evidence()
        except ContractError as exc:
            initial_block_code = exc.code

    def scene_evidence(_run_id: str) -> dict[str, Any]:
        snapshot = SceneStateStore(
            first_roots["cell_root"], resolved["normalized_job"]["robot_system_id"],
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
        holder["camera_transport_evidence"] = physical_gate_evidence()
        return True

    def start_binding(_run_id: str, slot: Mapping[str, Any]) -> dict[str, Any]:
        start_pose_id = slot.get("robot_start_pose_id")
        qualification = live_start_qualifications.get(start_pose_id)
        if qualification is not None:
            if start_transition_call is None:
                from tools.data_factory.motion.home_recovery import (
                    transition_to_start_live,
                )
                transition = transition_to_start_live(
                    motion_qualification=motion_qualification,
                    robot_start_pose_qualification=qualification,
                )
            else:
                transition = start_transition_call(
                    motion_qualification, qualification,
                )
            if (
                not isinstance(transition, Mapping)
                or transition.get("status") not in {
                    "AT_START", "ALREADY_AT_START",
                }
                or transition.get("robot_start_pose_id") != start_pose_id
            ):
                raise ContractError("PHYSICAL_CONSOLE_START_TRANSITION")
        snapshot = (
            snapshot_call() if snapshot_call is not None
            else capture_home_snapshot(tcp_candidate_manifest=paths["tcp"])
        )
        return build_test_only_start_binding(
            manifest=holder["operator"].manifest, hypothesis=hypothesis,
            motion_qualification=motion_qualification,
            home_candidate=home_candidate, current_snapshot=snapshot, slot=slot,
        )

    def episode(
        intent, lifecycle, cancel_event, episode_context,
        decision_provider, checkpoint_provider,
    ):
        active_roots = episode_context["root_binding"]
        active_payload = copy.deepcopy(payload)
        active_resolved = resolved_by_digest.get(
            intent["base_condition"]["resolved_job_digest"],
        )
        if active_resolved is None:
            raise ContractError("PHYSICAL_CONSOLE_RESOLVED_JOB")
        active_job = active_resolved["normalized_job"]
        active_payload["job"] = copy.deepcopy(active_job)
        active_payload.update(
            run_id=intent["run_id"], run_root=active_roots["run_root"],
            dataset_root=active_roots["dataset_root"],
        )
        order_index = intent["order_index"]
        next_slot = (
            holder["operator"].manifest["slots"][order_index + 1]
            if order_index + 1 < len(holder["operator"].manifest["slots"])
            else None
        )
        next_run_id = (
            run_id_for(order_index + 1)
            if next_slot is not None
            else None
        )
        release_resolved = active_resolved
        if next_slot is not None:
            next_base = next(
                item for item in hypothesis["base_conditions"]
                if item["base_condition_digest"]
                == next_slot["base_condition_digest"]
            )
            release_resolved = resolved_by_digest.get(
                next_base["resolved_job_digest"],
            )
            if release_resolved is None:
                raise ContractError("PHYSICAL_CONSOLE_RESOLVED_JOB")
        release_job = release_resolved["normalized_job"]
        active_payload.update(
            recycle_yaw_deg=release_job["yaw_deg"],
            recycle_x_mm=release_job["x_mm"],
            recycle_y_mm=release_job["y_mm"],
        )

        def episode_resolver(value):
            return run_job.resolve_campaign_episode_inputs(
                value,
                release_role=(
                    "DESTINATION_THEN_NEXT_SOURCE"
                    if next_run_id is not None else "RELEASE_DESTINATION"
                ),
                next_run_id=next_run_id,
                cell_root=active_roots["cell_root"],
                resolver=test_only_resolver,
            )

        scene_source: dict[str, Any]
        if order_index == 0:
            scene_source = {"state_initialization": state_initialization}
        else:
            live_resolved, _program, active_scene_binding = episode_resolver(active_payload)
            if canonical_digest(live_resolved) != active_resolved["resolver_result_digest"]:
                raise ContractError("PHYSICAL_CONSOLE_RESOLVED_JOB")
            scene_source = {
                "scene_binding": active_scene_binding,
                "scene_evidence": scene_evidence(intent["run_id"]),
                "observed_by": operator_label,
            }
        place_alias = "place1" if active_job["place_id"] == "PLACE_A" else active_job["place_id"]
        episode_binding = build_test_only_runtime_episode_binding(
            roots=active_roots, repository_root=repository,
            manifest=holder["operator"].manifest, hypothesis=hypothesis,
            intent=intent, start_binding=episode_context["start_binding"],
            resolved_job=active_resolved, place_alias=place_alias, clock=clock,
            **scene_source,
        )
        transport = holder.get("camera_transport_evidence")
        if not isinstance(transport, Mapping):
            raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
        task_recipe = get_task_recipe(active_job["task"])
        checklist = {
            "schema_version": "data_factory.site_checklist.v1",
            "place_alias": place_alias, "place_id": active_job["place_id"],
            "cell_calibration_id": active_job["cell_calibration_id"],
            "yaw_deg": active_job["yaw_deg"],
            "x_mm": active_job["x_mm"], "y_mm": active_job["y_mm"],
            "object_profile_id": active_job["object_profile_id"],
            "grasp_profile_id": active_job["grasp_profile_id"],
            "task": active_job["task"],
            "motion_recipe": intent["fixed_contract"]["motion_recipe"],
            "full_return_step_count": (
                len(task_recipe["recorded_phases"])
                + len(task_recipe["post_recording_phases"])
            ),
            "robot_start_pose_id": intent["slot"]["robot_start_pose_id"],
            "gripper_empty": "OPERATOR_CONFIRM_REQUIRED",
            "cell_clear": "OPERATOR_CONFIRM_REQUIRED",
            "estop_monitoring": "OPERATOR_CONFIRM_REQUIRED",
            "camera_state": "CONNECTED_UNPLACED",
            "camera_profile_id": profile["collection_profile_id"],
            "camera_transport_binding_digest": transport["binding_digest"],
            "episode_number": order_index + 1,
            "episode_limit": requested_count, "data_disposition": "TEST_ONLY",
        }
        holder["last_live_response"] = None
        try:
            live = run_live_call(
                active_payload, cancel_event, holder["console"].publish_runtime,
                resolver=episode_resolver,
                one_job=lifecycle, decision_provider=decision_provider,
                checkpoint_provider=checkpoint_provider,
                approval_scope="HIL_NUMERIC_PROXY",
                test_only_root_binding=episode_context["root_binding"],
                test_only_episode_binding=episode_binding,
                test_only_start_binding=episode_context["start_binding"],
                episode_ledger_context={
                    "manifest": holder["operator"].manifest,
                    "intent": intent,
                },
                preapproval_checklist=checklist,
                campaign_authorization=holder["console"].campaign_authorization,
                camera_warmup_call=campaign_camera_warmup,
                candidate_writer_enabled=False, repository_root=repository,
            )
        except Exception:
            holder.pop("camera_warmup_cache", None)
            raise
        if not isinstance(live, Mapping) or live.get("ok") is not True:
            holder.pop("camera_warmup_cache", None)
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
        admission = run_job.write_candidate_admission(
            active_payload, active_resolved, validator,
            operational_source="HIL_PROXY",
        )
        candidate_path = (
            Path(active_payload["run_root"]) / intent["run_id"]
            / "candidate_admission.json"
        ).resolve(strict=True)
        ledger_reference = run_job.bind_candidate_episode_state(
            data.get("episode_ledger"), candidate_path,
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
                "terminal_object_pose": {
                    key: release_job[key]
                    for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
                },
                "episode_ledger": copy.deepcopy(ledger_reference),
                "candidate_review_offer": {
                    "candidate_path": str(candidate_path),
                    "run_id": intent["run_id"],
                    "expected_file_digest": canonical_digest(admission),
                    "expected_review_context_digest": admission["review_context_digest"],
                    "ledger_reference": copy.deepcopy(ledger_reference),
                },
            },
            "technical_evidence": technical,
        }

    workspace_alias = (
        "place1"
        if resolved["normalized_job"]["place_id"] == "PLACE_A"
        else resolved["normalized_job"]["place_id"]
    )

    def operator_factory(episode_call) -> CampaignOperator:
        operator = CampaignOperator(
            session_id=session_id, lifecycle_owner=operator_label,
            operator_label=operator_label,
            workspace={
                "workspace_id": f"{workspace_alias}-test-only",
                "identity": (
                    f"{resolved['normalized_job']['place_id']}@"
                    f"{resolved['normalized_job']['cell_calibration_id']}"
                ),
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
            physical_root_binding_call=roots_for,
            physical_start_binding_call=start_binding,
            repository_root=repository, clock=clock,
        )
        holder["operator"] = operator
        return operator

    fixed_lane = {
        "workspace": {
            "display_name": f"{workspace_alias} · {resolved['normalized_job']['place_id']} · TEST_ONLY",
            "place_id": resolved["normalized_job"]["place_id"],
            "revision": resolved["normalized_job"]["cell_calibration_id"],
            "bounds": (
                f"{len(resolved_jobs)} validated candidate poses · "
                f"{requested_count} episodes"
            ),
        },
        "object_id": resolved["normalized_job"]["object_profile_id"],
        "grasp_id": resolved["normalized_job"]["grasp_profile_id"],
        "task": {
            "id": resolved["normalized_job"]["task"],
            "capability": "PHYSICAL_EXECUTABLE",
        },
        "motion": {
            "id": hypothesis["fixed_contract"]["motion_recipe"],
            "capability": "PHYSICAL_EXECUTABLE",
        },
        "start_pose_id": hypothesis["robot_start_poses"][0]["robot_start_pose_id"],
        "camera_role": (
            f"{'+'.join(profile['camera_roles'])} · CONNECTED_UNPLACED · TEST_ONLY"
        ),
        "profile_id": profile["collection_profile_id"],
    }
    full_open_m = float(motion_qualification["gripper_positions_m"]["open"])
    retune_feedback = retune["acceptable_feedback_m"]
    gripper_tuning = {
        "retune_id": retune["retune_id"],
        "retune_digest": retune["retune_digest"],
        "status": retune["status"],
        "object_profile_id": retune["object_profile_id"],
        "grasp_profile_id": retune["grasp_profile_id"],
        "command_position_m": retune["command_position_m"],
        "command_percent": round(
            100 * float(retune["command_position_m"]) / full_open_m, 2,
        ),
        "acceptable_feedback_m": copy.deepcopy(retune_feedback),
        "acceptable_feedback_percent": {
            key: round(100 * float(value) / full_open_m, 2)
            for key, value in retune_feedback.items()
        },
        "data_disposition": "TEST_ONLY",
        "production_authority": False,
        "training_authority": False,
    }

    def projection() -> dict[str, Any]:
        gripper = holder["gripper_projection"]
        conditions = [
            item["coverage_condition"] for item in hypothesis["base_conditions"]
        ]
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
                    {
                        "label": "camera", "status": "CONNECTED_UNPLACED",
                        "detail": ", ".join(
                            f"{role}: {device}"
                            for role, device in role_device_ids.items()
                        ),
                    },
                    {"label": "data", "status": "TEST_ONLY", "detail": "production writers disabled"},
                ],
            },
            "fixed_lane": copy.deepcopy(fixed_lane),
            "gripper_tuning": copy.deepcopy(gripper_tuning),
            "draft": {
                "draft_id": draft["draft_id"], "revision": draft["revision"],
                "authoring_mode": "DIRECT_EDIT", "selector": draft["selector"],
                "selector_version": "campaign-selector-v1", "budget": requested_count,
                "selected_count": requested_count, "blocked_count": 0,
                "estimated_minutes": requested_count * 2,
                "split_summary": f"TRAIN {requested_count}",
                "repeat_summary": f"x{requested_count}",
                "coverage_summary": f"{requested_count}/{requested_count} selected",
                "cells": [{
                    "cell_id": f"pose-{canonical_digest(condition)[7:27]}",
                    "x_mm": condition["x_mm"], "y_mm": condition["y_mm"],
                    "yaw_deg": condition["yaw_deg"], "split": "TRAIN",
                    "repeat": 1, "coverage_count": 0,
                    "selection_state": "SELECTED",
                    "eligibility_status": "ELIGIBLE",
                    "reason_codes": [
                        "CURRENT_SCENE_ANCHOR"
                        if condition == hypothesis["base_conditions"][0]["coverage_condition"]
                        else "REGISTERED_WORKSPACE_CANDIDATE"
                    ],
                } for condition in conditions],
            },
            "capabilities": [{
                "label": (
                    f"{resolved['normalized_job']['task']} · "
                    f"{hypothesis['fixed_contract']['motion_recipe']} · "
                    f"{len(profile['camera_roles'])}-camera TEST_ONLY"
                ),
                "status": "PHYSICAL_EXECUTABLE",
                "reason_codes": ["REGISTERED_WORKSPACE_FINITE_CAMPAIGN"],
            }],
            "workspace_wizard": {
                "capability": "NOT_AVAILABLE",
                "plane_reference": {
                    "id": resolved["normalized_job"]["cell_calibration_id"],
                    "digest": resolved["input_digests"]["cell_calibration"],
                    "table_normal_base": resolved["calibration"]["z"],
                },
                "source_measurement_mm": None, "final_measurement_mm": None,
                "captures": {"CENTER": False, "X_REF": False, "Y_CHECK": False},
            },
            "effect_counts": copy.deepcopy(counters),
        }

    paths_text = " · ".join(
        f"{name}={first_roots[name]}"
        for name in ("run_root", "dataset_root", "cell_root")
    )
    console = OperatorConsole(
        session_id=session_id, run_id=run_id, operator_label=operator_label,
        campaign_operator_factory=operator_factory, episode_call=episode,
        projection_call=projection, test_only_paths=paths_text,
        terminal_response_call=lambda: holder.get("last_live_response"),
        candidate_review_port=CandidateReviewPort(
            operator_label=operator_label,
            review_call=lambda path, **kwargs: run_job.review_candidate_admission(
                path, clock=clock, **kwargs,
            ),
        ),
        gripper_setup_request=setup_request,
        gripper_setup_resolution_call=(
            resolve_gripper_setup if setup_request is not None else None
        ),
        initial_block_code=initial_block_code,
        campaign_approval_once=True,
        run_id_factory=run_id_for,
        prepare_timeout_s=8.0, close_timeout_s=5.0, clock=clock,
    )
    holder["console"] = console
    context = {
        "session_id": session_id, "run_id": run_id,
        "effect_scope": "PHYSICAL", "data_disposition": "TEST_ONLY",
        "camera_binding_set": camera_binding_set,
        "camera_binding_digest": role_map_digest,
        "camera_bindings": copy.deepcopy(role_device_ids),
        "roots": first_roots,
        "requested_count": requested_count,
        "resolved_job_digest": resolved["resolved_job_digest"],
        "hypothesis_digest": hypothesis["hypothesis_digest"],
        "feature_contract": copy.deepcopy(
            hypothesis["fixed_contract"]["feature_contract"]
        ),
        "motion_qualification_digest": canonical_digest(motion_qualification),
        "base_motion_qualification_digest": (
            retune["base_motion_qualification_digest"]
        ),
        "gripper_tuning": copy.deepcopy(gripper_tuning),
        "gripper_setup": copy.deepcopy(holder["gripper_projection"]),
        "production_writers_enabled": False,
    }
    return console, context


def build_physical_operator_application(
    *, repository_root: str | Path, session_id: str, operator_label: str,
    environment_call: Callable[[], Mapping[str, Any]],
    prepare_environment_call: Callable[[], Mapping[str, Any]],
    selected_camera_device_id: str | None = None,
    discovery_call: Callable[[], Sequence[object]] = discover_uvc_device_ids,
    activation_call: Callable[[], bool | Mapping[str, Any]] | None = None,
    snapshot_call: Callable[[], Mapping[str, Any]] | None = None,
    gripper_readback_call: Callable[[], Mapping[str, Any]] | None = None,
    gripper_maintenance_call: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    home_recovery_call: Callable[[], Mapping[str, Any]] | None = None,
    start_transition_call: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None,
    run_live_call: Callable[..., Mapping[str, Any]] = run_job.run_live,
    production_campaign_factory: Callable[
        [str, dict[str, Any], dict[str, Any]], OperatorConsole
    ] | None = None,
    initial_environment: Mapping[str, Any] | None = None,
    initial_catalog: Mapping[str, Any] | None = None,
    initial_camera_devices: Sequence[object] | None = None,
    job_path: str | Path = DEFAULT_JOB,
    gripper_retune_path: str | Path = DEFAULT_GRIPPER_RETUNE,
    camera_environment_call: Callable[
        [Mapping[str, Any] | None, Mapping[str, Mapping[str, str]]], Mapping[str, Any]
    ] | None = None,
    clock=None,
) -> tuple[CollectionOperatorApplication, dict[str, Any]]:
    """Compose the reusable app without creating campaign roots or run state."""
    if production_campaign_factory is not None and not callable(
        production_campaign_factory
    ):
        raise ContractError("OPERATOR_APPLICATION_PRODUCTION_FACTORY")
    repository = Path(repository_root).resolve(strict=True)
    if initial_catalog is None:
        camera_devices = normalize_camera_devices(discovery_call())
        devices = [item["logical_id"] for item in camera_devices]
        catalog = load_operator_catalog(repository, device_ids=devices)
    else:
        catalog = copy.deepcopy(dict(initial_catalog))
        machine = catalog.get("machine")
        devices = machine.get("camera_device_ids") if isinstance(machine, Mapping) else None
        if (
            catalog.get("schema_version") != CATALOG_SCHEMA
            or catalog.get("catalog_digest") != canonical_digest({
                key: value for key, value in catalog.items()
                if key != "catalog_digest"
            })
            or not isinstance(devices, list)
            or any(not isinstance(item, str) or not item for item in devices)
            or devices != sorted(set(devices))
        ):
            raise ContractError("OPERATOR_CATALOG_SCHEMA")
        camera_devices = normalize_camera_devices(
            devices if initial_camera_devices is None else initial_camera_devices,
        )
        if {item["logical_id"] for item in camera_devices} != set(devices):
            raise ContractError("OPERATOR_CATALOG_SCHEMA")
    initial_job_path = _repository_path(repository, job_path)
    initial_job = load_json_strict(initial_job_path)
    initial_job_source = str(initial_job_path.relative_to(repository))
    requested = None
    if selected_camera_device_id is not None:
        if devices.count(selected_camera_device_id) != 1:
            raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
        requested = {device: "UNUSED" for device in devices}
        preferred_profile = _v2_camera_profiles(repository).get(
            initial_job.get("collection_profile_id"),
        )
        if preferred_profile is None or len(preferred_profile["camera_roles"]) != 1:
            raise ContractError("OPERATOR_APPLICATION_COMPATIBLE_COMBINATION")
        requested[selected_camera_device_id] = preferred_profile["camera_roles"][0].upper()
    camera_setup, camera_resolution = resolve_camera_setup(
        repository_root=repository, devices=camera_devices,
        preferred_profile_id=initial_job.get("collection_profile_id", ""),
        requested_bindings=requested,
    )
    selected_camera_bindings: dict[str, str] = {}
    selected_camera_binding_digest = None
    selected_profile = None
    if camera_resolution is not None:
        selected_profile = camera_resolution["collection_profile"]
        selected_camera_bindings = {
            role: binding["stable_device_id"]
            for role, binding in camera_resolution["role_bindings"]["bindings"].items()
        }
        selected_camera_binding_digest = camera_binding_digest(
            selected_profile, selected_camera_bindings,
        )
        selected_camera_device_id = (
            next(iter(selected_camera_bindings.values()))
            if len(selected_camera_bindings) == 1 else None
        )
    available_binding_digests = sorted({
        candidate["camera_binding_digest"] for candidate in catalog["combinations"]
        if candidate["camera_bindings"]
    })
    mapping_ready = (
        camera_setup["status"] == "READY"
        and selected_camera_binding_digest in available_binding_digests
    )
    preferred_binding_digests = sorted({
        candidate["camera_binding_digest"] for candidate in catalog["combinations"]
        if candidate["camera_bindings"]
        and candidate["camera_profile_id"] == initial_job.get("collection_profile_id")
    })
    internal_binding_digest = (
        selected_camera_binding_digest if mapping_ready
        else preferred_binding_digests[0] if preferred_binding_digests
        else available_binding_digests[0] if available_binding_digests
        else None
    )
    camera_state = {
        "bindings": selected_camera_bindings,
        "binding_digest": selected_camera_binding_digest,
        "profile": selected_profile,
        "device_id": selected_camera_device_id,
        "ready": mapping_ready,
        "binding_set": (
            None if camera_resolution is None
            else copy.deepcopy(camera_resolution["role_bindings"])
        ),
    }

    def bind_selected_camera(
        value: Mapping[str, Any], *, binding_digest: str | None,
    ) -> dict[str, Any]:
        bound = copy.deepcopy(dict(value))
        for candidate in bound["combinations"]:
            if candidate["camera_binding_digest"] != binding_digest:
                reason = (
                    "CAMERA_REBIND_REQUIRED" if binding_digest is not None
                    else camera_setup["reason"] or "CAMERA_ROLE_BINDING_REQUIRED"
                )
                for mode in ("TEST_COLLECTION", "GENERAL_COLLECTION"):
                    candidate["execution"][mode] = {
                        "executable": False, "reason": reason,
                    }
            if production_campaign_factory is None:
                candidate["execution"]["GENERAL_COLLECTION"] = {
                    "executable": False,
                    "reason": "GENERAL_CALLER_NOT_CONFIGURED",
                }
            candidate["combination_digest"] = canonical_digest({
                key: item for key, item in candidate.items()
                if key != "combination_digest"
            })
        bound["combinations"].sort(key=lambda item: item["combination_digest"])
        bound["catalog_digest"] = canonical_digest({
            key: item for key, item in bound.items() if key != "catalog_digest"
        })
        return bound

    catalog = bind_selected_camera(
        catalog, binding_digest=(
            camera_state["binding_digest"] if camera_state["ready"] else None
        ),
    )
    compatible = [
        combination for combination in catalog["combinations"]
        if (
            internal_binding_digest is None
            or combination["camera_binding_digest"] == internal_binding_digest
        )
    ]
    initial_cells = {
        option["id"]
        for option in catalog["axes"]["cell"]
        if all(
            option.get("metadata", {}).get(field) == initial_job.get(field)
            for field in ("place_id", "yaw_deg", "x_mm", "y_mm")
        )
    }
    initial = [
        item for item in compatible
        if item["cell_id"] in initial_cells
        and item["sources"]["job"] == initial_job_source
        and (
            internal_binding_digest is not None
            or item["camera_profile_id"] == initial_job.get("collection_profile_id")
        )
        and (
            not mapping_ready
            or item["execution"]["TEST_COLLECTION"]["executable"] is True
        )
    ]
    if len(initial) != 1:
        raise ContractError("OPERATOR_APPLICATION_COMPATIBLE_COMBINATION")
    combination = initial[0]
    selection = {
        "schema_version": SELECTION_SCHEMA_V2,
        "combination_digest": combination["combination_digest"],
        "data_mode": "TEST_COLLECTION",
        **{
            field: combination[field]
            for field in (
                "workspace_id", "frame_id", "task_id", "object_id", "grasp_id",
                "cell_id", "start_pose_id", "motion_id", "variant_id",
                "camera_profile_id", "camera_device_id",
            )
        },
        "camera_bindings": copy.deepcopy(combination["camera_bindings"]),
        "camera_binding_digest": combination["camera_binding_digest"],
        "policy_id": "DETERMINISTIC_SPREAD",
    }
    application_holder: dict[str, CollectionOperatorApplication] = {}

    def active_catalog() -> Mapping[str, Any]:
        application = application_holder.get("application")
        return catalog if application is None else application.catalog

    def active_selection() -> Mapping[str, Any]:
        application = application_holder.get("application")
        return selection if application is None else application.selection

    def active_combination() -> Mapping[str, Any]:
        selected = active_selection()
        matches = [
            item for item in active_catalog()["combinations"]
            if item["combination_digest"] == selected["combination_digest"]
        ]
        if len(matches) != 1:
            raise ContractError("OPERATOR_APPLICATION_COMPATIBLE_COMBINATION")
        return matches[0]

    def start_pose_domain(
        selected_ids: Sequence[str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        source = active_combination()["sources"]
        motion = load_json_strict(_repository_path(repository, source["motion"]))
        home = load_json_strict(_repository_path(repository, source["start_pose"]))
        home_digest = canonical_digest(home)
        home_pose = _test_only_home_start_pose(
            motion, home, motion["robot_system_id"],
        )
        qualifications = {home_pose["robot_start_pose_id"]: home_pose}
        profiles = [{
            "start_pose_id": home_pose["robot_start_pose_id"],
            "display_name": "HOME",
            "status": "AVAILABLE",
        }]
        registry = repository / DEFAULT_START_POSES
        for profile in list_start_pose_profiles(registry):
            identifier = profile["start_pose_id"]
            if identifier in qualifications:
                raise ContractError("PHYSICAL_CONSOLE_START_POSE")
            compatible = (
                profile["robot_system_id"] == motion["robot_system_id"]
                and profile["recovery_home_digest"] == home_digest
            )
            available = (
                compatible
                and profile["qualification_status"] == "QUALIFIED"
                and profile["safety_status"] == "SAFE_FOR_MOTION"
            )
            status = (
                "AVAILABLE" if available else "CANDIDATE"
                if compatible and profile["qualification_status"] == "CANDIDATE"
                else "QUALIFICATION_REQUIRED"
            )
            projected = {
                "start_pose_id": identifier,
                "display_name": profile["display_name"],
                "status": status,
            }
            if not available:
                projected["reason"] = (
                    "START_POSE_REQUIRES_QUALIFICATION"
                    if compatible else "START_POSE_BINDING_MISMATCH"
                )
            else:
                qualifications[identifier] = project_robot_start_pose_qualification(
                    profile,
                )
            profiles.append(projected)
        profiles.sort(key=lambda value: (
            value["start_pose_id"] != home_pose["robot_start_pose_id"],
            value["start_pose_id"],
        ))
        chosen = (
            [home_pose["robot_start_pose_id"]]
            if selected_ids is None else list(selected_ids)
        )
        if (
            not chosen or len(chosen) != len(set(chosen))
            or any(identifier not in qualifications for identifier in chosen)
        ):
            raise ContractError("PHYSICAL_CONSOLE_START_POSE")
        return {
            "profiles": profiles,
            "selected_start_pose_ids": chosen,
        }, qualifications

    start_pose_setup, _initial_start_qualifications = start_pose_domain()

    def _camera_bindings_update(
        assignments: Mapping[str, str] | None,
    ) -> Mapping[str, Any]:
        nonlocal camera_setup, devices, camera_devices
        camera_devices = normalize_camera_devices(discovery_call())
        devices = [item["logical_id"] for item in camera_devices]
        next_setup, resolution = resolve_camera_setup(
            repository_root=repository, devices=camera_devices,
            preferred_profile_id=initial_job.get("collection_profile_id", ""),
            requested_bindings=assignments,
        )
        current_catalog = active_catalog()
        current_selection = copy.deepcopy(dict(active_selection()))
        if resolution is None:
            camera_setup = next_setup
            camera_state.update(
                bindings={}, binding_digest=None, profile=None,
                device_id=None, ready=False, binding_set=None,
            )
            environment = (
                camera_environment_call(None, {})
                if camera_environment_call is not None else environment_call()
            )
            return {
                "camera_setup": next_setup, "catalog": current_catalog,
                "selection": current_selection, "environment": environment,
            }
        profile = resolution["collection_profile"]
        role_map = {
            role: binding["stable_device_id"]
            for role, binding in resolution["role_bindings"]["bindings"].items()
        }
        binding_digest = camera_binding_digest(profile, role_map)
        raw_catalog = load_operator_catalog(repository, device_ids=devices)
        bound_catalog = bind_selected_camera(
            raw_catalog, binding_digest=binding_digest,
        )
        current_source = active_combination()["sources"]["job"]
        candidates = [
            item for item in bound_catalog["combinations"]
            if item["camera_profile_id"] == profile["collection_profile_id"]
            and item["camera_binding_digest"] == binding_digest
            and item["sources"]["job"] == current_source
            and all(
                item[field] == current_selection[field]
                for field in (
                    "workspace_id", "frame_id", "task_id", "object_id",
                    "grasp_id", "cell_id", "start_pose_id", "motion_id",
                    "variant_id",
                )
            )
        ]
        if len(candidates) != 1:
            next_setup = {
                **next_setup, "status": "READY",
                "reason": "COLLECTION_CAMERA_CALLER_NOT_AVAILABLE",
            }
            camera_setup = next_setup
            camera_state.update(
                bindings=role_map, binding_digest=binding_digest, profile=profile,
                device_id=(next(iter(role_map.values())) if len(role_map) == 1 else None),
                ready=True,
                binding_set=copy.deepcopy(resolution["role_bindings"]),
            )
            write_camera_role_bindings(
                resolution["role_bindings"], repository_root=repository,
            )
            camera_devices = {
                role: {
                    "kind": binding["device_kind"],
                    "stable_id": binding["stable_device_id"],
                    "capture_endpoint": binding["capture_endpoint"],
                }
                for role, binding in resolution["role_bindings"]["bindings"].items()
            }
            environment = (
                camera_environment_call(profile, camera_devices)
                if camera_environment_call is not None else environment_call()
            )
            return {
                "camera_setup": next_setup, "catalog": current_catalog,
                "selection": current_selection, "environment": environment,
            }
        candidate = candidates[0]
        next_selection = {
            **current_selection,
            "combination_digest": candidate["combination_digest"],
            "camera_profile_id": candidate["camera_profile_id"],
            "camera_device_id": candidate["camera_device_id"],
            "camera_bindings": copy.deepcopy(candidate["camera_bindings"]),
            "camera_binding_digest": candidate["camera_binding_digest"],
        }
        validate_operator_selection(bound_catalog, next_selection)
        write_camera_role_bindings(
            resolution["role_bindings"], repository_root=repository,
        )
        camera_setup = next_setup
        camera_state.update(
            bindings=role_map, binding_digest=binding_digest, profile=profile,
            device_id=(next(iter(role_map.values())) if len(role_map) == 1 else None),
            ready=True, binding_set=copy.deepcopy(resolution["role_bindings"]),
        )
        camera_devices = {
            role: {
                "kind": binding["device_kind"],
                "stable_id": binding["stable_device_id"],
                "capture_endpoint": binding["capture_endpoint"],
            }
            for role, binding in resolution["role_bindings"]["bindings"].items()
        }
        environment = (
            camera_environment_call(profile, camera_devices)
            if camera_environment_call is not None else environment_call()
        )
        return {
            "camera_setup": next_setup, "catalog": bound_catalog,
            "selection": next_selection, "environment": environment,
        }

    def camera_bindings_call(
        assignments: Mapping[str, str],
    ) -> Mapping[str, Any]:
        return _camera_bindings_update(assignments)

    def camera_refresh_call() -> Mapping[str, Any]:
        return _camera_bindings_update(None)

    def workspace_manager_factory(display_name: str) -> WorkspaceManager:
        return WorkspaceManager(
            session_id=f"{session_id}-workspace",
            candidate_root=repository / "outputs/data_factory/workspace_registration",
            config_root=repository / "config/data_factory",
            display_name=display_name,
        )

    def workspace_snapshot_call() -> Mapping[str, Any]:
        if snapshot_call is not None:
            return snapshot_call()
        return capture_home_snapshot(
            tcp_candidate_manifest=_repository_path(repository, DEFAULT_TCP_MANIFEST),
        )

    def start_pose_capture_call(display_name: str) -> Mapping[str, Any]:
        source = active_combination()["sources"]
        motion = load_json_strict(_repository_path(repository, source["motion"]))
        home = load_json_strict(_repository_path(repository, source["start_pose"]))
        captured = workspace_snapshot_call()
        joints = captured.get("joint_positions_rad") if isinstance(captured, Mapping) else None
        ages = (
            captured.get("joint_state_age_s"), captured.get("ros_sample_age_s"),
        ) if isinstance(captured, Mapping) else (None, None)
        if (
            not isinstance(captured, Mapping)
            or captured.get("schema_version") != "data_factory.pose_snapshot.v1"
            or not isinstance(joints, Mapping)
            or set(joints) != {"j1", "j2", "j3", "j4", "j5", "j6"}
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in joints.values()
            )
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= float(motion["max_joint_state_age_s"])
                for value in ages
            )
        ):
            raise ContractError("START_POSE_SNAPSHOT")
        now = clock().astimezone(timezone.utc)
        normalized_snapshot = {
            "schema_version": "data_factory.start_pose_joint_snapshot.v1",
            "source": "READ_ONLY_JOINT_STATE",
            "robot_system_id": motion["robot_system_id"],
            "joint_order": ["j1", "j2", "j3", "j4", "j5", "j6"],
            "joint_positions_rad": {
                joint: float(joints[joint])
                for joint in ("j1", "j2", "j3", "j4", "j5", "j6")
            },
            "captured_at": now.isoformat().replace("+00:00", "Z"),
        }
        slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")[:32]
        token = canonical_digest({
            "display_name": display_name,
            "snapshot": normalized_snapshot,
        }).removeprefix("sha256:")[:12]
        profile = compile_start_pose_profile(
            start_pose_id=f"start-{slug or 'pose'}-{token}",
            display_name=display_name,
            robot_system_id=motion["robot_system_id"],
            snapshot=normalized_snapshot,
            tolerance_rad={
                joint: float(motion["goal_tolerances"]["joint_rad"])
                for joint in ("j1", "j2", "j3", "j4", "j5", "j6")
            },
            recovery_home_digest=canonical_digest(home),
            qualification_status="CANDIDATE", safety_status="UNASSESSED",
            max_snapshot_age_s=0.5, now=now,
        )
        save_start_pose_profile(repository / DEFAULT_START_POSES, profile, now=now)
        application = application_holder.get("application")
        selected = (
            application.draft["selected_start_pose_ids"]
            if application is not None else start_pose_setup["selected_start_pose_ids"]
        )
        setup, _qualifications = start_pose_domain(selected)
        return setup

    def workspace_preview_call(
        manager: WorkspaceManager, measurements: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        sources = active_combination()["sources"]
        cell = load_json_strict(_repository_path(repository, sources["cell"]))
        measured = validate_print_measurements(
            source_scale_bar_mm=measurements["source_scale_bar_mm"],
            final_scale_bar_mm=measurements["final_scale_bar_mm"],
        )
        yaw0_sheet = select_yaw0_print_profile(
            repository,
            place_id=cell["place_id"],
            source_scale_bar_mm=measured["source_scale_bar_measured_mm"],
        )
        return manager.preview_captured(
            plane_reference=qualified_table_plane_reference(cell),
            print_measurements=measured,
            operator_or_agent_id=operator_label,
            yaw0_sheet=yaw0_sheet,
            tcp_candidate_manifest=_repository_path(repository, DEFAULT_TCP_MANIFEST),
            tolerance_mm=1.0,
        )

    def catalog_reload_call() -> Mapping[str, Any]:
        return bind_selected_camera(
            load_operator_catalog(repository, device_ids=devices),
            binding_digest=(
                camera_state["binding_digest"] if camera_state["ready"] else None
            ),
        )

    def selected_home_recovery_call() -> Mapping[str, Any]:
        if home_recovery_call is not None:
            return home_recovery_call()
        from tools.data_factory.motion.home_recovery import recover_home_live
        source = active_combination()["sources"]
        return recover_home_live(
            motion_qualification=load_json_strict(
                _repository_path(repository, source["motion"]),
            ),
        )

    def physical_pose_plan(
        selected: Mapping[str, Any], draft: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        current_catalog = active_catalog()
        anchor = validate_operator_pose(
            current_catalog, selected, draft.get("current_object_pose"),
        )
        count = draft["requested_count"]
        if draft.get("authoring_mode") == "ASSISTED":
            poses = project_assisted_poses(
                current_catalog, selected, anchor, count,
                repeat=draft["repeat"], normalized_seed=draft["normalized_seed"],
            )
            start_ids = project_balanced_start_pose_ids(
                draft["selected_start_pose_ids"], count,
                normalized_seed=draft["normalized_seed"],
            )
        else:
            pairs = copy.deepcopy(draft.get("direct_pairs") or [])
            if len(pairs) != count:
                raise ContractError("PHYSICAL_CONSOLE_DIRECT_SEQUENCE")
            poses = [
                validate_operator_pose(current_catalog, selected, {
                    key: pair[key]
                    for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
                })
                for pair in pairs
            ]
            start_ids = [pair["start_pose_id"] for pair in pairs]
        _setup, qualifications = start_pose_domain(
            draft["selected_start_pose_ids"],
        )
        return {
            "direct_pose_sequence": poses,
            "direct_start_pose_ids": start_ids,
            "selected_start_pose_qualifications": [
                qualifications[identifier]
                for identifier in sorted(set(start_ids))
            ],
        }, poses[0]

    def verified_camera_environment() -> dict[str, Any]:
        environment = environment_call()
        component = environment.get("components", {}).get("camera")
        binding_set = camera_state.get("binding_set")
        if (
            not isinstance(component, Mapping)
            or component.get("state") != "READY"
            or not isinstance(binding_set, Mapping)
        ):
            raise ContractError("PHYSICAL_CAMERA_TOPIC")
        checked = validate_camera_role_bindings(binding_set)
        evidence = {
            "schema_version": "data_factory.test_only_camera_transport_set.v1",
            "camera_binding_digest": camera_state["binding_digest"],
            "roles": {
                role: {
                    "device_kind": binding["device_kind"],
                    "stable_device_id": binding["stable_device_id"],
                    "capture_endpoint": binding["capture_endpoint"],
                    "status": "ENVIRONMENT_GRAPH_VERIFIED",
                }
                for role, binding in checked["bindings"].items()
            },
            "status": "ENVIRONMENT_GRAPH_VERIFIED",
        }
        evidence["binding_digest"] = canonical_digest(evidence)
        return evidence

    def campaign_factory(
        campaign_id: str, selected: dict[str, Any], draft: dict[str, Any],
    ) -> OperatorConsole:
        chosen = next((
            item for item in active_catalog()["combinations"]
            if item["combination_digest"] == selected.get("combination_digest")
        ), None)
        mode = selected.get("data_mode")
        execution = (
            chosen.get("execution", {}).get(mode)
            if isinstance(chosen, Mapping) else None
        )
        if (
            not isinstance(chosen, Mapping)
            or not isinstance(execution, Mapping)
            or execution.get("executable") is not True
            or mode not in {"TEST_COLLECTION", "GENERAL_COLLECTION"}
            or draft.get("draft_id") != f"{campaign_id}-draft"
            or type(draft.get("requested_count")) is not int
        ):
            raise ContractError("OPERATOR_APPLICATION_CAMPAIGN_FACTORY")
        if mode == "GENERAL_COLLECTION":
            if production_campaign_factory is None:
                raise ContractError("OPERATOR_APPLICATION_PRODUCTION_FACTORY")
            console = production_campaign_factory(
                campaign_id, copy.deepcopy(selected), copy.deepcopy(draft),
            )
            if (
                not isinstance(console, OperatorConsole)
                or console.campaign_operator.effect_scope != "PHYSICAL"
                or console.campaign_operator.data_disposition != "PRODUCTION"
            ):
                close = getattr(console, "close", None)
                if callable(close):
                    close()
                raise ContractError("OPERATOR_APPLICATION_PRODUCTION_FACTORY")
            return console
        source = chosen["sources"]
        pose_plan, campaign_initial_pose = physical_pose_plan(selected, draft)
        console, _context = build_physical_operator_console(
            repository_root=repository,
            session_id=campaign_id,
            run_id=f"{campaign_id}-run-1",
            operator_label=operator_label,
            job_path=source["job"],
            yaw0_sheet=source["yaw0_sheet"],
            motion_qualification_path=source["motion"],
            home_candidate_path=source["start_pose"],
            collection_profile_path=source["camera_profile"],
            selected_camera_device_id=selected["camera_device_id"],
            selected_camera_bindings=selected["camera_bindings"],
            selected_camera_binding_digest=selected["camera_binding_digest"],
            selected_camera_binding_set=camera_state["binding_set"],
            discovery_call=discovery_call,
            activation_call=(
                activation_call
                if activation_call is not None or camera_environment_call is None
                else verified_camera_environment
            ),
            snapshot_call=snapshot_call,
            gripper_readback_call=gripper_readback_call,
            gripper_maintenance_call=gripper_maintenance_call,
            gripper_retune_path=gripper_retune_path,
            run_live_call=run_live_call,
            start_transition_call=start_transition_call,
            requested_count=draft["requested_count"],
            normalized_seed=draft["normalized_seed"],
            initial_object_pose=campaign_initial_pose,
            **pose_plan,
            environment_prepared=True,
            clock=clock,
        )
        return console

    application = CollectionOperatorApplication(
        session_id=session_id,
        operator_label=operator_label,
        catalog=catalog,
        initial_selection=selection,
        environment_call=environment_call,
        prepare_environment_call=prepare_environment_call,
        campaign_factory=campaign_factory,
        workspace_manager_factory=workspace_manager_factory,
        workspace_snapshot_call=workspace_snapshot_call,
        workspace_preview_call=workspace_preview_call,
        catalog_reload_call=catalog_reload_call,
        home_recovery_call=selected_home_recovery_call,
        camera_setup=(camera_setup if camera_environment_call is not None else None),
        camera_bindings_call=(
            camera_bindings_call if camera_environment_call is not None else None
        ),
        camera_refresh_call=(
            camera_refresh_call if camera_environment_call is not None else None
        ),
        start_pose_setup=start_pose_setup,
        start_pose_capture_call=start_pose_capture_call,
        initial_environment=initial_environment,
        effect_scope="PHYSICAL",
    )
    application_holder["application"] = application
    return application, {
        "session_id": session_id,
        "effect_scope": "PHYSICAL",
        "data_disposition": "TEST_ONLY",
        "catalog_digest": catalog["catalog_digest"],
        "combination_digest": combination["combination_digest"],
        "camera_device_id": (
            camera_state["device_id"] if camera_state["ready"] else None
        ),
        "camera_bindings": copy.deepcopy(
            camera_state["bindings"] if camera_state["ready"] else {}
        ),
        "camera_binding_digest": (
            camera_state["binding_digest"] if camera_state["ready"] else None
        ),
        "production_writers_enabled": False,
    }


QA_WORKFLOW = (
    "환경 준비 결과에서 robot·gripper·camera의 측정 상태를 확인한다",
    "수집 계획에서 사용 가능한 상태공간과 횟수를 선택한다",
    "사람이 읽는 캠페인 요약을 한 번 확인하고 시작한다",
    "실행 중 E-stop과 cell을 감시하고 문제 있을 때 즉시 중단한다",
    "완료 결과와 남은 횟수를 확인한 뒤 같은 설정 또는 변경한 설정으로 계속한다",
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve the reusable foreground FR5 collection operator",
    )
    parser.add_argument("--effect-scope", choices=("FAKE", "PHYSICAL"), default="FAKE")
    parser.add_argument("--port", type=int, default=4174)
    parser.add_argument("--repository-root", default=str(ROOT))
    parser.add_argument("--session-id")
    parser.add_argument("--operator-label", default="local-operator")
    parser.add_argument("--camera-device-id")
    parser.add_argument(
        "--job", default=str(DEFAULT_JOB),
        help="Repository-relative qualified job used only for the initial selection",
    )
    parser.add_argument(
        "--gripper-retune", default=str(DEFAULT_GRIPPER_RETUNE),
        help="Repository-relative object/grasp TEST_ONLY gripper retune",
    )
    parser.add_argument(
        "--no-auto-prepare", action="store_true",
        help="Show discovery facts without starting missing foreground children",
    )
    args = parser.parse_args(argv)
    if args.effect_scope == "FAKE":
        from tools.data_factory.fake_operator_console import main as fake_main
        return fake_main(["--port", str(args.port)])
    now = datetime.now(timezone.utc)
    suffix = now.strftime("%Y%m%dT%H%M%SZ")
    session_id = args.session_id or f"collection-test-only-{suffix}"
    application = bridge = None
    environment_holder: dict[str, Any] = {"active": None, "pending": None}
    try:
        repository = Path(args.repository_root).resolve(strict=True)
        camera_descriptors = discover_camera_devices()
        devices = [item["logical_id"] for item in camera_descriptors]
        catalog = load_operator_catalog(repository, device_ids=devices)
        initial_job = load_json_strict(_repository_path(repository, args.job))
        requested_bindings = None
        if args.camera_device_id is not None:
            if args.camera_device_id not in devices:
                raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
            profile = _v2_camera_profiles(repository).get(
                initial_job.get("collection_profile_id"),
            )
            if profile is None or len(profile["camera_roles"]) != 1:
                raise ContractError("OPERATOR_APPLICATION_COMPATIBLE_COMBINATION")
            requested_bindings = {device: "UNUSED" for device in devices}
            requested_bindings[args.camera_device_id] = profile[
                "camera_roles"
            ][0].upper()
        initial_camera_setup, initial_resolution = resolve_camera_setup(
            repository_root=repository, devices=camera_descriptors,
            preferred_profile_id=initial_job.get("collection_profile_id", ""),
            requested_bindings=requested_bindings,
        )
        selected_device = None
        if initial_resolution is not None:
            profile = initial_resolution["collection_profile"]
            if len(profile["camera_roles"]) == 1:
                selected_device = initial_resolution["role_bindings"]["bindings"][
                    profile["camera_roles"][0]
                ]["stable_device_id"]
        def blocked_environment(reason: str) -> dict[str, Any]:
            return {
                "schema_version": "data_factory.operator_environment.v1",
                "state": "BLOCKED",
                "observed_at": now.isoformat().replace("+00:00", "Z"),
                "components": {
                    name: {
                        "state": "MISSING" if name == "camera" else "BLOCKED",
                        "owner": None,
                        "reason": reason,
                    }
                    for name in ("robot", "controller", "gripper", "camera")
                },
            }

        blocked = blocked_environment(
            initial_camera_setup["reason"] or "CAMERA_ROLE_BINDING_REQUIRED",
        )

        def select_camera_environment(
            profile: Mapping[str, Any] | None,
            camera_devices: Mapping[str, Mapping[str, str]],
        ) -> Mapping[str, Any]:
            current = environment_holder["active"] or environment_holder["pending"]
            if profile is None:
                if current is not None:
                    current.stop_cameras()
                    environment_holder["pending"] = current
                    return current.projection()
                return copy.deepcopy(blocked_environment(
                    "CAMERA_ROLE_BINDING_REQUIRED",
                ))
            if current is not None:
                projection = current.rebind_cameras(profile, camera_devices)
                environment_holder["pending"] = current
                return projection
            from tools.data_factory.operator_physical_environment import (
                build_physical_operator_environment,
            )
            pending = build_physical_operator_environment(
                repository_root=repository,
                collection_profile=profile,
                camera_devices=camera_devices,
                gripper_readback_call=capture_gripper_setup_readback,
                gripper_maintenance_call=normalize_gripper_after_operator_ready,
            )
            environment_holder["pending"] = pending
            return pending.projection()

        def environment_call() -> Mapping[str, Any]:
            current = environment_holder["pending"] or environment_holder["active"]
            return copy.deepcopy(blocked) if current is None else current.projection()

        def prepare_environment_call() -> Mapping[str, Any]:
            pending = environment_holder["pending"]
            if pending is None:
                return copy.deepcopy(blocked)
            environment_holder["active"] = pending
            return pending.prepare_environment()

        if initial_resolution is None:
            prepared = copy.deepcopy(blocked)
        else:
            camera_devices = {
                role: {
                    "kind": binding["device_kind"],
                    "stable_id": binding["stable_device_id"],
                    "capture_endpoint": binding["capture_endpoint"],
                }
                for role, binding in initial_resolution["role_bindings"]["bindings"].items()
            }
            select_camera_environment(
                initial_resolution["collection_profile"], camera_devices,
            )
            prepared = (
                environment_call()
                if args.no_auto_prepare else prepare_environment_call()
            )
        application, context = build_physical_operator_application(
            repository_root=repository,
            session_id=session_id,
            operator_label=args.operator_label,
            selected_camera_device_id=selected_device,
            discovery_call=discover_camera_devices,
            environment_call=environment_call,
            prepare_environment_call=prepare_environment_call,
            initial_environment=prepared,
            initial_catalog=catalog,
            initial_camera_devices=camera_descriptors,
            job_path=args.job,
            gripper_retune_path=args.gripper_retune,
            camera_environment_call=select_camera_environment,
        )
        bridge = LoopbackBridge(
            core=application.bridge_core,
            ui_root=repository / "operator-ui",
            host="127.0.0.1", port=args.port,
        )
        print(json.dumps({
            "status": "LISTENING", "url": bridge.origin,
            "environment_state": prepared["state"],
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
        if application is not None:
            application.close()
        stopped = set()
        for environment in (
            environment_holder.get("pending"), environment_holder.get("active"),
        ):
            if environment is not None and id(environment) not in stopped:
                stopped.add(id(environment))
                environment.stop()
        if bridge is not None:
            bridge.server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
