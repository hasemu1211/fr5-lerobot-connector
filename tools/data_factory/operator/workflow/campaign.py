"""Thin foreground UI adapter for one injected TEST_ONLY campaign.

The injected episode callable remains the adapter to ``run_live``.  This module
owns no robot, recorder, dataset, scheduler, or lifecycle state machine.
"""
from __future__ import annotations

import copy
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from tools.data_factory.campaign_authoring import (
    DRAFT_SCHEMA,
    DRAFT_SCHEMA_V2,
    campaign_cell_id,
    validate_campaign_draft,
)
from tools.data_factory.campaign_authorization import (
    EPISODE_SCOPE_FIELDS,
    build_campaign_authorization,
    build_campaign_envelope,
    validate_authorized_episode_scope,
)
from tools.data_factory.campaign_operator import CampaignOperator
from tools.data_factory.experiment_manifest import (
    FIXED_CONTRACT_ENDPOINT_SCHEMAS,
    FR5_TEST_ONLY_FEATURE_CONTRACT,
    build_test_only_feature_contract,
    compile_fr5_hypothesis,
)
from tools.data_factory.operator.workflow.intents import (
    ButtonDecisionPort,
    CandidateReviewPort,
    INTENT_SCHEMA,
    OperatorCheckpointPort,
    OperatorIntentCore,
)
from tools.data_factory.motion.trajectory_variants import (
    VARIANT_IDS,
    validate_trajectory_variant_binding,
)
from tools.data_factory.motion.object_reposition import (
    validate_object_reposition_binding,
)
from tools.data_factory.state_space import (
    validate_state_space_design_profile,
    validate_yaw_sample_binding,
)
from tools.data_factory.quality.coverage_report import build_coverage_report
from tools.data_factory import run_job
from tools.data_factory.task_recipe import (
    validate_episode_instruction_binding,
    validate_task_binding,
)
from tools.data_factory_recovery import write_json_atomic
from tools.fr5_data_factory import (
    ContractError,
    DIGEST,
    MOTION_QUALIFICATION_SCHEMAS,
    SAFE_ID,
    canonical_digest,
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
    "CAMERA_WARMUP": (
        "카메라 준비 증거 확인", 25,
        "동일 결속의 통과 증거를 재사용하거나 두 카메라를 병렬 측정합니다.",
    ),
    "AWAITING_HUMAN_APPROVAL": ("승인 범위 확인", 35, "캠페인 승인 범위와 이번 계획을 대조합니다."),
    "RECORDER_STARTING": ("기록기 준비", 40, "30 Hz readiness와 writer 상태를 확인합니다."),
    "EXECUTING": ("수집 동작 실행", 50, "로봇 상태·명령·RGB를 동기화해 기록합니다."),
    "RECYCLING": ("수집 구간 완료 및 복구", 70, "녹화를 멈춘 뒤 물체를 다음 시작 상태로 복구합니다."),
    "OBJECT_REPOSITION_PLANNED": (
        "다음 물체 자세 계획 확인", 92,
        "녹화 밖 재배치 계획을 기존 캠페인 결속과 대조했습니다.",
    ),
    "FINALIZING": ("데이터 저장", 80, "동결된 episode를 commit하고 영상 파일을 마무리합니다."),
    "VALIDATING": ("데이터 품질 검사", 90, "timestamp·drop·provenance·프레임 일치를 검사합니다."),
}
RECORDER_RUNTIME_MILESTONES = {
    "RECORDER_STARTING": {"status": "CONNECTING", "label": "기록 준비 중"},
    "EXECUTING": {"status": "RECORDING", "label": "기록 중"},
    "RECYCLING": {"status": "FROZEN", "label": "녹화 완료"},
    "FINALIZING": {"status": "FROZEN", "label": "녹화 완료"},
    "VALIDATING": {"status": "COMMITTED", "label": "저장 완료"},
}
ROOT = Path(__file__).resolve().parents[4]
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
    "gripper-retune-wood-cube-25mm-top-center-r008.json"
)
GRIPPER_RETUNE_FIELDS = frozenset({
    "schema_version", "retune_id", "status", "source",
    "object_profile_id", "grasp_profile_id",
    "base_grasp_profile_digest", "base_motion_qualification_digest",
    "command_position_m", "acceptable_feedback_m", "data_disposition",
    "production_authority", "training_authority", "retune_digest",
})
GRIPPER_RETUNE_V2_FIELDS = GRIPPER_RETUNE_FIELDS | {"force_percent"}
GRIPPER_RETUNE_V3_FIELDS = GRIPPER_RETUNE_V2_FIELDS | {
    "open_velocity_percent",
}
GRIPPER_RETUNE_V4_FIELDS = GRIPPER_RETUNE_V3_FIELDS | {
    "velocity_percent", "open_force_percent",
}


def _measurement_for_code(code: str) -> str:
    if code == "PHYSICAL_SECOND_MOTION_OWNER" or code.endswith("_MISMATCH"):
        return "FAIL"
    if code.startswith(("PHYSICAL_", "GRIPPER_SETUP_")) or code.endswith("NOT_AVAILABLE"):
        return "NOT_AVAILABLE"
    return "FAIL"


def _redigest(value: dict[str, Any], field: str) -> dict[str, Any]:
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


def _validate_successful_object_reposition_result(
    value: object, binding: Mapping[str, Any], *,
    post_scene_digest: object, code: str,
) -> dict[str, Any]:
    """Validate the compact successful continuation result at an outer boundary."""
    try:
        expected = validate_object_reposition_binding(binding)
    except ContractError as exc:
        raise ContractError(code) from exc
    if not isinstance(value, Mapping) or set(value) != run_job.OBJECT_REPOSITION_RESULT_FIELDS:
        raise ContractError(code)
    result = copy.deepcopy(dict(value))
    response = result.get("execution_response")
    response_data = response.get("data") if isinstance(response, Mapping) else None
    transition = (
        response_data.get("scene_transition")
        if isinstance(response_data, Mapping) else None
    )
    digest_fields = (
        "plan_digest", "resolved_job_digest", "scene_state_digest",
        "preapproval_scope_digest", "plan_artifact_digest", "result_digest",
    )
    if (
        expected["start_state"] != "ON_SURFACE"
        or result.get("schema_version")
        != "data_factory.object_reposition_result.v2"
        or result.get("status") != "PASS"
        or result.get("code") != "PASS"
        or result.get("parent_run_id") != expected["parent_run_id"]
        or result.get("continuation_run_id") != expected["continuation_run_id"]
        or result.get("next_run_id") != expected["next_run_id"]
        or result.get("object_reposition_binding_digest")
        != expected["binding_digest"]
        or any(
            not isinstance(result.get(field), str)
            or DIGEST.fullmatch(result[field]) is None
            for field in digest_fields
        )
        or result.get("result_digest") != canonical_digest({
            key: item for key, item in result.items() if key != "result_digest"
        })
        or result.get("scene_state_digest") != post_scene_digest
        or not isinstance(response, Mapping)
        or response.get("ok") is not True
        or response.get("code") != "COMPLETE"
        or response.get("state") != "COMPLETED"
        or response.get("run_id") != expected["continuation_run_id"]
        or response.get("plan_digest") != result.get("plan_digest")
        or not isinstance(transition, Mapping)
        or transition.get("scene_state_digest") != result.get("scene_state_digest")
    ):
        raise ContractError(code)
    return result


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


def _validate_test_only_gripper_retune(
    retune: Mapping[str, Any], *, grasp: Mapping[str, Any],
    motion: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    """Validate the passive TEST_ONLY gripper settings before ROS bring-up."""
    schema = (
        retune.get("schema_version")
        if isinstance(retune, Mapping) else None
    )
    expected_fields = {
        "data_factory.test_only_gripper_retune.v1": GRIPPER_RETUNE_FIELDS,
        "data_factory.test_only_gripper_retune.v2": GRIPPER_RETUNE_V2_FIELDS,
        "data_factory.test_only_gripper_retune.v3": GRIPPER_RETUNE_V3_FIELDS,
        "data_factory.test_only_gripper_retune.v4": GRIPPER_RETUNE_V4_FIELDS,
    }.get(schema)
    if (
        not isinstance(retune, Mapping) or set(retune) != expected_fields
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
        or not isinstance(grasp, Mapping) or not isinstance(motion, Mapping)
    ):
        raise ContractError("TEST_ONLY_GRIPPER_RETUNE")
    close = grasp.get("gripper_close") if isinstance(grasp, Mapping) else None
    feedback = retune.get("acceptable_feedback_m")
    base_feedback = close.get("acceptable_feedback_m") if isinstance(close, Mapping) else None
    numbers = (
        retune.get("command_position_m"),
        feedback.get("min") if isinstance(feedback, Mapping) else None,
        feedback.get("max") if isinstance(feedback, Mapping) else None,
    )
    if (
        not isinstance(close, Mapping)
        or not isinstance(base_feedback, Mapping)
        or not isinstance(feedback, Mapping) or set(feedback) != {"min", "max"}
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in numbers
        )
        or retune.get("object_profile_id") != grasp.get("object_profile_id")
        or retune.get("grasp_profile_id") != grasp.get("grasp_profile_id")
        or retune.get("base_grasp_profile_digest") != canonical_digest(grasp)
        or retune.get("base_motion_qualification_digest")
        != canonical_digest(motion)
    ):
        raise ContractError("TEST_ONLY_GRIPPER_RETUNE_BINDING")
    command, minimum, maximum = (float(value) for value in numbers)
    settings = {
        "velocity_percent": retune.get(
            "velocity_percent", close.get("velocity_percent"),
        ),
        "force_percent": retune.get(
            "force_percent", close.get("force_percent"),
        ),
        "open_velocity_percent": retune.get(
            "open_velocity_percent", close.get("velocity_percent"),
        ),
        "open_force_percent": retune.get(
            "open_force_percent", close.get("force_percent"),
        ),
    }
    if not (
        float(close["command_position_m"]) <= command <= minimum < maximum
        and float(base_feedback["min"]) <= minimum
        and maximum <= float(base_feedback["max"])
        and all(type(value) is int for value in settings.values())
        and 1 <= settings["velocity_percent"] <= close["velocity_percent"]
        and 1 <= settings["force_percent"] <= close["force_percent"]
        and 1 <= settings["open_velocity_percent"] <= close["velocity_percent"]
        and 1 <= settings["open_force_percent"] <= close["force_percent"]
    ):
        raise ContractError("TEST_ONLY_GRIPPER_RETUNE_ENVELOPE")

    return copy.deepcopy(dict(retune)), settings


def _derive_test_only_gripper_program(
    validated: Mapping[str, Any], motion: Mapping[str, Any],
    program: Mapping[str, Any], retune: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply an object-scoped TEST_ONLY override after qualified resolution."""
    if not isinstance(validated, Mapping) or not isinstance(program, Mapping):
        raise ContractError("TEST_ONLY_GRIPPER_RETUNE")
    grasp = validated.get("grasp_profile")
    checked_retune, settings = (
        _validate_test_only_gripper_retune(
            retune, grasp=grasp, motion=motion,
        )
    )
    job = validated.get("normalized_job")
    inputs = validated.get("input_digests")
    close = grasp.get("gripper_close") if isinstance(grasp, Mapping) else None
    opened = grasp.get("gripper_open") if isinstance(grasp, Mapping) else None
    program_bindings = program.get("binding_digests")
    base_requirements = copy.deepcopy(dict(close)) if isinstance(close, Mapping) else None
    if isinstance(base_requirements, dict) and isinstance(opened, Mapping):
        base_requirements.update(
            open_velocity_percent=opened.get("velocity_percent"),
            open_force_percent=opened.get("force_percent"),
        )
    if (
        not isinstance(job, Mapping) or not isinstance(inputs, Mapping)
        or checked_retune.get("object_profile_id") != job.get("object_profile_id")
        or checked_retune.get("grasp_profile_id") != job.get("grasp_profile_id")
        or inputs.get("grasp_profile") != canonical_digest(grasp)
        or not isinstance(program_bindings, Mapping)
        or program_bindings.get("motion_qualification") != canonical_digest(motion)
        or program_bindings.get("grasp_profile") != canonical_digest(grasp)
        or program.get("gripper_requirements") != base_requirements
    ):
        raise ContractError("TEST_ONLY_GRIPPER_RETUNE_BINDING")

    command = float(checked_retune["command_position_m"])
    feedback = checked_retune["acceptable_feedback_m"]
    minimum, maximum = float(feedback["min"]), float(feedback["max"])

    tuned_requirements = copy.deepcopy(base_requirements)
    tuned_requirements.update(
        command_position_m=command,
        acceptable_feedback_m={"min": minimum, "max": maximum},
        evidence_digest=checked_retune["retune_digest"],
    )
    if checked_retune["schema_version"] in {
        "data_factory.test_only_gripper_retune.v2",
        "data_factory.test_only_gripper_retune.v3",
        "data_factory.test_only_gripper_retune.v4",
    }:
        tuned_requirements["force_percent"] = settings["force_percent"]
        tuned_requirements["open_force_percent"] = settings["open_force_percent"]
    if checked_retune["schema_version"] in {
        "data_factory.test_only_gripper_retune.v3",
        "data_factory.test_only_gripper_retune.v4",
    }:
        tuned_requirements["open_velocity_percent"] = settings[
            "open_velocity_percent"
        ]
    if checked_retune["schema_version"] == "data_factory.test_only_gripper_retune.v4":
        tuned_requirements["velocity_percent"] = settings["velocity_percent"]
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


def _home_start_pose(
    motion_qualification: Mapping[str, Any], home_candidate: Mapping[str, Any],
    robot_system_id: str, *, source: str = "SYNTHETIC_TEST_ONLY",
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
        "source": source,
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
    motion_qualifications: Sequence[Mapping[str, Any]] | None = None,
    scene_digest: str, draft_id: str, manifest_id: str,
    requested_count: int, normalized_seed: int = 0,
    anchor_resolved_job_digest: str | None = None,
    direct_resolved_job_digests: Sequence[str] | None = None,
    direct_start_pose_ids: Sequence[str] | None = None,
    start_pose_qualifications: Sequence[Mapping[str, Any]] | None = None,
    test_only_gripper_retune_digest: str | None = None,
    qualification_source: str = "SYNTHETIC_TEST_ONLY",
    motion_recipe: str = "DIRECT",
    state_space_design_profile: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile a finite physical condition domain without execution authority."""
    if (
        type(requested_count) is not int or not 1 <= requested_count <= 100
        or type(normalized_seed) is not int or normalized_seed < 0
        or qualification_source not in {
            "SYNTHETIC_TEST_ONLY", "QUALIFICATION_ARTIFACT",
        }
        or motion_recipe not in VARIANT_IDS
        or test_only_gripper_retune_digest is not None
        and (
            qualification_source != "SYNTHETIC_TEST_ONLY"
            or not isinstance(test_only_gripper_retune_digest, str)
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
        not in MOTION_QUALIFICATION_SCHEMAS
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

    qualifications = (
        [copy.deepcopy(dict(motion_qualification))]
        if motion_qualifications is None else [
            copy.deepcopy(dict(item)) for item in motion_qualifications
            if isinstance(item, Mapping)
        ]
    )
    qualification_by_cell = {
        item.get("cell_calibration_id"): item for item in qualifications
    }
    resolved_cells = {
        item.get("normalized_job", {}).get("cell_calibration_id")
        for item in resolved_jobs
    }
    if (
        len(qualifications) != len(qualification_by_cell)
        or set(qualification_by_cell) != resolved_cells
        or any(
            item.get("schema_version") not in MOTION_QUALIFICATION_SCHEMAS
            or item.get("qualification_status") != "QUALIFIED"
            or item.get("home_candidate_digest")
            != canonical_digest(home_candidate)
            for item in qualifications
        )
    ):
        raise ContractError("PHYSICAL_CONSOLE_FIXED_BINDING")
    motion_digests = {
        cell_id: canonical_digest(item)
        for cell_id, item in qualification_by_cell.items()
    }
    fixed_job_fields = (
        "task", "instruction", "robot_system_id", "collection_profile_id",
        "object_profile_id", "grasp_profile_id",
    )
    fixed_input_fields = (
        "robot_system", "collection_profile", "object_profile",
        "grasp_profile",
    )
    for resolved in resolved_jobs:
        other_job = resolved.get("normalized_job")
        other_inputs = resolved.get("input_digests")
        endpoint_motion = (
            qualification_by_cell.get(other_job.get("cell_calibration_id"))
            if isinstance(other_job, Mapping) else None
        )
        if (
            not isinstance(other_job, Mapping)
            or not isinstance(other_inputs, Mapping)
            or resolved.get("collection_profile") != profile
            or any(other_job.get(field) != job[field] for field in fixed_job_fields)
            or any(other_inputs.get(field) != inputs[field] for field in fixed_input_fields)
            or not isinstance(endpoint_motion, Mapping)
            or any(
                endpoint_motion.get(field) != other_job.get(field)
                for field in (
                    "robot_system_id", "cell_calibration_id",
                    "object_profile_id", "grasp_profile_id",
                )
            )
            or resolved.get("resolved_job_digest")
            != canonical_digest({"job": other_job, "input_digests": other_inputs})
        ):
            raise ContractError("PHYSICAL_CONSOLE_FIXED_BINDING")
    endpoint_by_cell: dict[str, dict[str, Any]] = {}
    for resolved in resolved_jobs:
        current_job = resolved["normalized_job"]
        current_inputs = resolved["input_digests"]
        cell_id = current_job["cell_calibration_id"]
        endpoint = {
            "workspace_id": current_job["place_id"],
            "cell_calibration_id": cell_id,
            "cell_calibration_digest": current_inputs["cell_calibration"],
            "motion_recipe_digest": motion_digests[cell_id],
        }
        previous = endpoint_by_cell.get(cell_id)
        if previous is not None and previous != endpoint:
            raise ContractError("PHYSICAL_CONSOLE_FIXED_BINDING")
        endpoint_by_cell[cell_id] = endpoint
    endpoint_bindings = sorted(
        endpoint_by_cell.values(),
        key=lambda item: (item["workspace_id"], item["cell_calibration_id"]),
    )
    multi_endpoint = len(endpoint_bindings) > 1
    if multi_endpoint and (
        job["task"] != "pick_place" or len(endpoint_bindings) != 2
    ):
        raise ContractError("PHYSICAL_CONSOLE_FIXED_BINDING")
    endpoint_digest = canonical_digest(endpoint_bindings)
    motion_digest = (
        endpoint_digest if multi_endpoint
        else endpoint_bindings[0]["motion_recipe_digest"]
    )
    fixed = {
        "schema_version": (
            "data_factory.fr5_fixed_contract.v4"
            if multi_endpoint and motion_recipe != "DIRECT"
            else "data_factory.fr5_fixed_contract.v2" if multi_endpoint
            else "data_factory.fr5_fixed_contract.v3"
            if motion_recipe != "DIRECT"
            else "data_factory.fr5_fixed_contract.v1"
        ),
        "robot_system_id": job["robot_system_id"],
        "task": job["task"],
        "instruction": job["instruction"],
        "collection_profile_digest": inputs["collection_profile"],
        "feature_contract": feature_contract,
        "object_profile_id": job["object_profile_id"],
        "grasp_profile_id": job["grasp_profile_id"],
        "scene_digest": scene_digest,
        "cell_calibration_id": endpoint_bindings[0]["cell_calibration_id"],
        "cell_calibration_digest": endpoint_bindings[0][
            "cell_calibration_digest"
        ],
        "motion_recipe": motion_recipe,
        "motion_recipe_digest": motion_digest,
        "pregrasp_digest": canonical_digest({
            "motion_qualification": motion_digest, "phase": "PREGRASP_PTP",
            "recipe": motion_recipe,
        }),
        "waypoint_digest": canonical_digest({
            "motion_qualification": motion_digest,
            "phases": ["APPROACH_STOP_LIN", "FINAL_APPROACH_LIN", "LIFT_LIN"],
            "recipe": motion_recipe,
        }),
        "trajectory_digest": canonical_digest({
            "motion_qualification": motion_digest, "recipe": motion_recipe,
            **(
                {"test_only_gripper_retune": test_only_gripper_retune_digest}
                if test_only_gripper_retune_digest is not None else {}
            ),
        }),
    }
    if multi_endpoint:
        fixed.update(
            endpoint_bindings=copy.deepcopy(endpoint_bindings),
            endpoint_bindings_digest=endpoint_digest,
        )
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
            "motion_recipe_digest": motion_digests[
                current_job["cell_calibration_id"]
            ],
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
            "source": qualification_source,
            "qualification_status": "QUALIFIED",
            "coverage_report_digest": canonical_digest(report),
            "coverage_domain_digest": report["domain_digest"],
            "coverage_condition_digest": condition_digest,
            "resolver_result_digest": canonical_digest(resolved),
            "resolved_job_digest": resolved["resolved_job_digest"],
            "yaw_action_binding_digest": canonical_digest({
                "scope": (
                    "TEST_ONLY" if qualification_source == "SYNTHETIC_TEST_ONLY"
                    else "QUALIFIED_MOTION"
                ),
                "yaw_deg": condition["yaw_deg"],
                "motion_qualification_digest": condition[
                    "motion_recipe_digest"
                ],
            }),
            "dual_view_observability_digest": canonical_digest({
                "single_view": "CONNECTED_UNPLACED",
                "dual_view": "NOT_AVAILABLE",
                "semantic_authority": "NONE",
                "yaw_deg": condition["yaw_deg"],
                **(
                    {
                        "workspace_id": condition["place_id"],
                        "cell_calibration_id": condition[
                            "cell_calibration_id"
                        ],
                    }
                    if multi_endpoint else {}
                ),
            }),
        }, "qualification_digest"))
    home_pose = _home_start_pose(
        motion_qualification, home_candidate, job["robot_system_id"],
        source=qualification_source,
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
            or item.get("source") != qualification_source
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
        "source": qualification_source,
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
    checked_state_space_design = (
        None if state_space_design_profile is None
        else validate_state_space_design_profile(
            state_space_design_profile,
        )
    )
    draft = validate_campaign_draft({
        "schema_version": (
            DRAFT_SCHEMA_V2
            if checked_state_space_design is not None else DRAFT_SCHEMA
        ),
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
        **(
            {"state_space_design_profile": checked_state_space_design}
            if checked_state_space_design is not None else {}
        ),
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
        task_bindings: Sequence[Mapping[str, Any]] | None = None,
        episode_instruction_bindings: Sequence[Mapping[str, Any]] | None = None,
        yaw_sample_bindings: Sequence[Mapping[str, Any] | None] | None = None,
        object_reposition_bindings: Sequence[Mapping[str, Any] | None] | None = None,
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
        if task_bindings is None:
            self._task_bindings = None
            if episode_instruction_bindings is not None:
                raise ContractError("OPERATOR_CONSOLE_TASK_BINDING")
        else:
            self._task_bindings = [
                validate_task_binding(item) for item in task_bindings
            ]
            if (
                len(self._task_bindings)
                != self.campaign_operator.draft["requested_count"]
                or any(
                    item["task_id"]
                    != self.campaign_operator.hypothesis["fixed_contract"]["task"]
                    for item in self._task_bindings
                )
            ):
                raise ContractError("OPERATOR_CONSOLE_TASK_BINDING")
        if episode_instruction_bindings is None:
            self._episode_instruction_bindings = None
        else:
            self._episode_instruction_bindings = [
                validate_episode_instruction_binding(item)
                for item in episode_instruction_bindings
            ]
            if (
                self._task_bindings is None
                or len(self._episode_instruction_bindings)
                != len(self._task_bindings)
                or any(
                    instruction["task_binding"] != task_binding
                    for instruction, task_binding in zip(
                        self._episode_instruction_bindings,
                        self._task_bindings,
                    )
                )
            ):
                raise ContractError("OPERATOR_CONSOLE_TASK_BINDING")
        if object_reposition_bindings is None:
            self._object_reposition_bindings = None
        else:
            if (
                not isinstance(object_reposition_bindings, (list, tuple))
                or len(object_reposition_bindings)
                != self.campaign_operator.draft["requested_count"]
            ):
                raise ContractError("OPERATOR_CONSOLE_REPOSITION_BINDING")
            self._object_reposition_bindings = [
                None if item is None
                else validate_object_reposition_binding(item)
                for item in object_reposition_bindings
            ]
        if yaw_sample_bindings is None:
            self._yaw_sample_bindings = None
        else:
            if (
                not isinstance(yaw_sample_bindings, (list, tuple))
                or len(yaw_sample_bindings)
                != self.campaign_operator.draft["requested_count"]
            ):
                raise ContractError("OPERATOR_CONSOLE_YAW_BINDING")
            self._yaw_sample_bindings = [
                None if item is None else validate_yaw_sample_binding(item)
                for item in yaw_sample_bindings
            ]
            if self._task_bindings is not None and any(
                binding is not None
                and not math.isclose(
                    float(binding["source_object_yaw_deg"]),
                    float(next(
                        spatial["pose"]["yaw_deg"]
                        for spatial in task["spatial_bindings"]
                        if spatial["role"] == "SOURCE"
                    )),
                    rel_tol=0.0, abs_tol=1e-9,
                )
                for binding, task in zip(
                    self._yaw_sample_bindings, self._task_bindings,
                )
            ):
                raise ContractError("OPERATOR_CONSOLE_YAW_BINDING")
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
        for index, slot in enumerate(manifest["slots"]):
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
            projected = {
                "order_index": slot["order_index"],
                "slot_id": slot["slot_id"],
                "slot_digest": canonical_digest(slot),
                "base_condition_digest": slot["base_condition_digest"],
                "robot_start_pose_id": slot["robot_start_pose_id"],
                "coverage_condition": copy.deepcopy(dict(condition)),
                "coverage_condition_digest": condition_digest,
            }
            if self._task_bindings is not None:
                binding = self._task_bindings[index]
                source = binding["spatial_bindings"][0]
                if (
                    source["role"] != "SOURCE"
                    or any(
                        source["pose"][field] != condition[field]
                        for field in ("place_id", "yaw_deg", "x_mm", "y_mm")
                    )
                ):
                    raise ContractError("OPERATOR_CONSOLE_TASK_BINDING")
                projected["task_binding"] = copy.deepcopy(binding)
                if self._episode_instruction_bindings is None:
                    raise ContractError("OPERATOR_CONSOLE_TASK_BINDING")
                projected["episode_instruction_binding"] = copy.deepcopy(
                    self._episode_instruction_bindings[index]
                )
                destination = next((
                    item for item in binding["spatial_bindings"]
                    if item["role"] == "DESTINATION"
                ), None)
                if destination is not None:
                    projected["destination_pose"] = copy.deepcopy(
                        destination["pose"],
                    )
            if self._object_reposition_bindings is not None:
                reposition = self._object_reposition_bindings[index]
                if reposition is not None:
                    projected["object_reposition"] = copy.deepcopy(reposition)
            if self._yaw_sample_bindings is not None:
                yaw_sample = self._yaw_sample_bindings[index]
                if yaw_sample is not None:
                    projected["yaw_sample_binding"] = copy.deepcopy(yaw_sample)
            result.append(projected)
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
            "expected_review_context_digest", "checklist_id",
            "ledger_reference",
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
                "expected_review_context_digest", "checklist_id",
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
            if candidate is not None and candidate.get("status") == "PENDING":
                result.insert(0, "review_candidate")
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
        event_data = event.get("data")
        reposition_evidence = None
        if code == "OBJECT_REPOSITION_PLANNED":
            digest_fields = (
                "object_reposition_binding_digest",
                "object_reposition_plan_digest",
                "object_reposition_plan_artifact_digest",
                "object_reposition_collision_report_digest",
                "object_reposition_plan_only_no_motion_digest",
            )
            try:
                expected_reposition = self._expected_object_reposition_binding()
            except ContractError as exc:
                raise ContractError("OPERATOR_CONSOLE_RUNTIME_EVENT") from exc
            if (
                not isinstance(event_data, Mapping)
                or not isinstance(expected_reposition, Mapping)
                or expected_reposition.get("start_state") != "ON_SURFACE"
                or event_data.get("object_reposition_binding_digest")
                != expected_reposition.get("binding_digest")
                or event_data.get("object_reposition_run_id")
                != expected_reposition.get("continuation_run_id")
                or not isinstance(event_data.get("object_reposition_run_id"), str)
                or SAFE_ID.fullmatch(event_data["object_reposition_run_id"]) is None
                or any(
                    not isinstance(event_data.get(field), str)
                    or DIGEST.fullmatch(event_data[field]) is None
                    for field in digest_fields
                )
            ):
                raise ContractError("OPERATOR_CONSOLE_RUNTIME_EVENT")
            evidence_fields = (
                "object_reposition_binding_digest",
                "object_reposition_run_id",
                "object_reposition_plan_digest",
                "object_reposition_plan_artifact_digest",
                "object_reposition_collision_report_digest",
                "object_reposition_plan_only_no_motion_digest",
            )
            reposition_evidence = {
                field: event_data[field] for field in evidence_fields
            }

        def change():
            if self._workflow != "RUNNING":
                return
            self._runtime_milestone = {
                "phase": code, "phase_label": label,
                "progress": progress, "detail": detail,
            }
            if reposition_evidence is not None:
                self._runtime_milestone["evidence"] = copy.deepcopy(
                    reposition_evidence,
                )
            recorder = RECORDER_RUNTIME_MILESTONES.get(code)
            if recorder is not None:
                self._runtime_milestone["recorder"] = dict(recorder)

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

    def _expected_object_reposition_binding(self) -> dict[str, Any] | None:
        if self._object_reposition_bindings is None:
            return None
        intent = self._active_intent_projection
        manifest = self.campaign_operator.manifest
        slots = manifest.get("slots") if isinstance(manifest, Mapping) else None
        order_index = intent.get("order_index") if isinstance(intent, Mapping) else None
        if (
            not isinstance(slots, list)
            or type(order_index) is not int
            or order_index != self._run_index
            or not 0 <= order_index < len(slots)
            or slots[order_index].get("order_index") != order_index
            or intent.get("run_id") != self.run_id
            or intent.get("slot_id") != slots[order_index].get("slot_id")
            or intent.get("slot_digest") != canonical_digest(slots[order_index])
        ):
            raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH")
        return self._object_reposition_bindings[order_index]

    def _validated_object_reposition_preapproval(
        self, value: object, *, request: Mapping[str, Any],
        decision_binding: Mapping[str, Any], episode_binding: Mapping[str, Any],
        authorization: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        expected = self._expected_object_reposition_binding()
        if expected is None or expected["start_state"] == "HELD_OBJECT":
            if value is not None:
                raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH")
            return None
        try:
            scope = run_job._validate_object_reposition_preapproval(value)
        except ContractError as exc:
            raise ContractError(
                "OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH",
            ) from exc
        manifest = self.campaign_operator.manifest
        envelope = (
            self._campaign_envelope
            if isinstance(self._campaign_envelope, Mapping) else {}
        )
        slots = manifest.get("slots") if isinstance(manifest, Mapping) else None
        intent = self._active_intent_projection
        order_index = intent.get("order_index") if isinstance(intent, Mapping) else None
        task = (
            self._task_bindings[order_index]
            if self._task_bindings is not None
            and type(order_index) is int
            and 0 <= order_index < len(self._task_bindings)
            else None
        )
        destinations = (
            [
                item for item in task.get("spatial_bindings", [])
                if isinstance(item, Mapping) and item.get("role") == "DESTINATION"
            ]
            if isinstance(task, Mapping) else []
        )
        destination = destinations[0] if len(destinations) == 1 else None
        fixed = self.campaign_operator.hypothesis.get("fixed_contract")
        fixed_endpoints = (
            fixed.get("endpoint_bindings")
            if isinstance(fixed, Mapping)
            and fixed.get("schema_version") in FIXED_CONTRACT_ENDPOINT_SCHEMAS
            else None
        )
        if isinstance(fixed_endpoints, list) and isinstance(destination, Mapping):
            endpoint_matches = [
                item for item in fixed_endpoints
                if item.get("workspace_id") == destination.get("workspace_id")
                and item.get("cell_calibration_id") == destination.get("frame_id")
            ]
        elif isinstance(fixed, Mapping) and isinstance(destination, Mapping):
            endpoint_matches = [{
                "workspace_id": destination.get("workspace_id"),
                "cell_calibration_id": fixed.get("cell_calibration_id"),
                "cell_calibration_digest": fixed.get("cell_calibration_digest"),
                "motion_recipe_digest": fixed.get("motion_recipe_digest"),
            }]
        else:
            endpoint_matches = []
        fixed_endpoint = endpoint_matches[0] if len(endpoint_matches) == 1 else None
        next_slot = (
            slots[order_index + 1]
            if isinstance(slots, list)
            and type(order_index) is int
            and order_index + 1 < len(slots)
            else None
        )
        expected_endpoint = (
            {
                "run_id": expected["next_run_id"],
                "workspace_id": destination["workspace_id"],
                "frame_id": destination["frame_id"],
                "source_pose": copy.deepcopy(expected["source_pose"]),
                "target_pose": copy.deepcopy(expected["target_pose"]),
                "sheet_digest": destination["sheet_digest"],
                "family_digest": destination["family_digest"],
                "region_binding": copy.deepcopy(destination["region_binding"]),
                "cell_calibration_digest": fixed_endpoint[
                    "cell_calibration_digest"
                ],
                "motion_qualification_digest": fixed_endpoint[
                    "motion_recipe_digest"
                ],
            }
            if isinstance(destination, Mapping)
            and isinstance(fixed_endpoint, Mapping)
            else None
        )
        episode_digest = episode_binding.get("binding_digest")
        if (
            expected["start_state"] != "ON_SURFACE"
            or not isinstance(next_slot, Mapping)
            or not isinstance(expected_endpoint, Mapping)
            or destination.get("pose") != expected.get("source_pose")
            or expected.get("parent_run_id") != request.get("run_id")
            or expected.get("parent_run_id") != self.run_id
            or expected.get("next_run_id")
            != self._run_id_factory(order_index + 1)
            or not isinstance(episode_digest, str)
            or DIGEST.fullmatch(episode_digest) is None
            or episode_digest != canonical_digest({
                key: item for key, item in episode_binding.items()
                if key != "binding_digest"
            })
            or scope["parent_run_id"] != request.get("run_id")
            or scope["parent_plan_digest"] != request.get("plan_digest")
            or scope["parent_preapproval_evidence_digest"]
            != decision_binding.get("preapproval_evidence_digest")
            or scope["campaign_authorization_digest"]
            != authorization.get("authorization_digest")
            or scope["campaign_envelope_digest"]
            != envelope.get("envelope_digest")
            or scope["manifest_digest"] != (
                manifest.get("manifest_digest")
                if isinstance(manifest, Mapping) else None
            )
            or scope["intent_digest"] != intent.get("intent_digest")
            or scope["runtime_episode_binding_digest"] != episode_digest
            or scope["current_slot_digest"] != intent.get("slot_digest")
            or scope["next_slot"] != next_slot
            or scope["next_slot_digest"] != canonical_digest(next_slot)
            or scope["next_slot_endpoint"] != expected_endpoint
            or scope["next_slot_endpoint_digest"]
            != canonical_digest(expected_endpoint)
            or scope["continuation_run_id"]
            != expected.get("continuation_run_id")
            or scope["next_run_id"] != expected.get("next_run_id")
            or scope["object_reposition_binding_digest"]
            != expected.get("binding_digest")
        ):
            raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH")
        return scope

    def _authorized_plan_decision(self, request: Mapping[str, Any]) -> dict[str, Any]:
        authorization = self._campaign_authorization
        binding = request.get("decision_binding") if isinstance(request, Mapping) else None
        episode = binding.get("episode_binding") if isinstance(binding, Mapping) else None
        summary = binding.get("operator_summary") if isinstance(binding, Mapping) else None
        trajectory = (
            binding.get("trajectory_variant_binding")
            if isinstance(binding, Mapping) else None
        )
        yaw_sample = (
            binding.get("yaw_sample_binding")
            if isinstance(binding, Mapping) else None
        )
        precommit_safety = (
            binding.get("precommit_safety")
            if isinstance(binding, Mapping) else None
        )
        exact_plan_fields = {
            "trajectory_variant_binding",
            "trajectory_variant_binding_digest", "yaw_sample_binding",
            "yaw_sample_binding_digest", "precommit_safety",
            "plan_envelope_digest", "preapproval_evidence_digest",
        }
        exact_plan_present = (
            isinstance(binding, Mapping)
            and bool(exact_plan_fields & set(binding))
        )
        if exact_plan_present:
            try:
                trajectory = validate_trajectory_variant_binding(trajectory)
            except ContractError as exc:
                raise ContractError(
                    "OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH",
                ) from exc
        session = self.session.status() if self.session is not None else None
        scope = (
            {field: copy.deepcopy(episode[field]) for field in EPISODE_SCOPE_FIELDS}
            if isinstance(episode, Mapping) and EPISODE_SCOPE_FIELDS <= set(episode)
            else None
        )
        reposition_preapproval = self._validated_object_reposition_preapproval(
            (
                binding.get("object_reposition_preapproval")
                if isinstance(binding, Mapping) else None
            ),
            request=request,
            decision_binding=binding if isinstance(binding, Mapping) else {},
            episode_binding=episode if isinstance(episode, Mapping) else {},
            authorization=(
                authorization if isinstance(authorization, Mapping) else {}
            ),
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
            or exact_plan_present and (
                not exact_plan_fields <= set(binding)
                or trajectory.get("binding_digest")
                != binding.get("trajectory_variant_binding_digest")
                or trajectory.get("trajectory_variant_id")
                != self._campaign_envelope.get("motion_recipe")
                or not isinstance(precommit_safety, Mapping)
                or precommit_safety.get("approved_plan_digest")
                != request.get("plan_digest")
                or not isinstance(binding.get("plan_envelope_digest"), str)
                or DIGEST.fullmatch(binding["plan_envelope_digest"]) is None
                or not isinstance(
                    binding.get("preapproval_evidence_digest"), str,
                )
                or DIGEST.fullmatch(
                    binding["preapproval_evidence_digest"]
                ) is None
                or yaw_sample is None
                and binding.get("yaw_sample_binding_digest") is not None
                or yaw_sample is not None
                and (
                    not isinstance(yaw_sample, Mapping)
                    or binding.get("yaw_sample_binding_digest")
                    != yaw_sample.get("binding_digest")
                )
            )
            or self._active_episode_scope is not None
            and self._active_episode_scope != scope
        ):
            raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH")
        if exact_plan_present and yaw_sample is not None:
            try:
                yaw_sample = validate_yaw_sample_binding(yaw_sample)
            except ContractError as exc:
                raise ContractError(
                    "OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH"
                ) from exc
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
            "object_reposition_preapproval": copy.deepcopy(
                reposition_preapproval,
            ),
            **({
                "trajectory_variant_binding": copy.deepcopy(trajectory),
                "trajectory_variant_binding_digest": trajectory[
                    "binding_digest"
                ],
                "yaw_sample_binding": copy.deepcopy(yaw_sample),
                "yaw_sample_binding_digest": (
                    None if yaw_sample is None
                    else yaw_sample["binding_digest"]
                ),
                "precommit_safety": copy.deepcopy(precommit_safety),
                "plan_envelope_digest": binding["plan_envelope_digest"],
                "preapproval_evidence_digest": binding[
                    "preapproval_evidence_digest"
                ],
            } if exact_plan_present else {}),
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
            plan.update(
                plan_digest=pending["plan_digest"],
                approval_scope=pending["approval_scope"],
                decision_binding_digest=pending["decision_binding_digest"],
            )
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
        expected_reposition = self._expected_object_reposition_binding()
        reposition_result = result.get("object_reposition")
        if (
            name == "PASS"
            and isinstance(expected_reposition, Mapping)
            and expected_reposition.get("start_state") == "ON_SURFACE"
        ):
            reposition_result = _validate_successful_object_reposition_result(
                reposition_result, expected_reposition,
                post_scene_digest=(
                    technical.get("post_scene_digest")
                    if isinstance(technical, Mapping) else None
                ),
                code="OPERATOR_CONSOLE_REPOSITION_RESULT",
            )
            sealed["object_reposition"] = reposition_result
        elif reposition_result is not None:
            raise ContractError("OPERATOR_CONSOLE_REPOSITION_RESULT")
        for field in (
            "one_job", "synthetic_review", "synthetic_coverage_update",
        ):
            value = result.get(field, terminal_data.get(field))
            if value is not None:
                sealed[field] = copy.deepcopy(value)
        terminal_pose = result.get("terminal_object_pose")
        if reposition_result is not None:
            if terminal_pose != expected_reposition["target_pose"]:
                raise ContractError("OPERATOR_CONSOLE_TERMINAL_OBJECT_POSE")
            terminal_pose = copy.deepcopy(expected_reposition["target_pose"])
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
        review_offer = result.get("candidate_review_offer")
        if review_offer is not None:
            if name != "PASS":
                raise ContractError("OPERATOR_CONSOLE_CANDIDATE_OFFER")
            self._queue_candidate_review(review_offer, sealed)
        sealed["result_digest"] = canonical_digest(sealed)
        self._episode_result, self._workflow = sealed, workflow
        self._episode_history.append(copy.deepcopy(sealed))
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
                self._workflow not in {"RUNNING", "BLOCKED", "TERMINAL"}
                or self.candidate_review_port is None
                or self._active_candidate_review is None
            ):
                raise ContractError("OPERATOR_CONSOLE_CANDIDATE_STATE")
            item = self._active_candidate_review
            matches = [
                history for history in self._episode_history
                if isinstance(history.get("intent_binding"), Mapping)
                and history["intent_binding"].get("run_id") == item["run_id"]
            ]
            if len(matches) != 1:
                raise ContractError("OPERATOR_CONSOLE_CANDIDATE_STATE")
            history = matches[0]
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
                or resolved.get("run_id") != item["run_id"]
                or ledger_reference.get("training_status") != "NOT_AUTHORIZED"
                or ledger_reference.get("retention_state") != "PRESERVE"
            ):
                raise ContractError("OPERATOR_CONSOLE_CANDIDATE_STATE")
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
