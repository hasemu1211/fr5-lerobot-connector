"""Pure setup bindings for the collection desk.

This module discovers no devices and commands no hardware.  Callers provide
snapshots/documents and receive non-authoritative, session-local projections.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.data_factory.campaign_authoring import validate_collection_campaign_manifest
from tools.data_factory.cell_state import CellStateStore
from tools.data_factory.experiment_manifest import validate_fr5_hypothesis
from tools.data_factory.motion.pose_snapshot import JOINTS, _validate_snapshot, calibrate_place
from tools.data_factory.scene_state import SceneStateStore
from tools.data_factory.seed_campaign import (
    BUDGET_DIGEST_FIELDS as SEED_BUDGET_DIGEST_FIELDS,
    validate_seed_episode_intent,
)
from tools.fr5_data_factory import (
    ContractError,
    DIGEST,
    RFC3339,
    SAFE_ID,
    canonical_digest,
    load_json_strict,
    validate_yaw0_sheet,
)


ROOT_FIELDS = frozenset({
    "session_id", "run_id", "data_disposition", "run_root", "cell_root",
    "dataset_root", "production_writers_enabled", "binding_digest",
})
START_FIELDS = frozenset({
    "scope", "data_disposition", "manifest_digest", "slot_digest",
    "robot_start_pose_id", "robot_start_pose_qualification_digest",
    "motion_qualification_id", "motion_qualification_digest",
    "home_candidate_digest", "joint_order", "target_rad", "current_rad",
    "tolerance_rad", "max_snapshot_age_s", "snapshot_digest", "status", "authority",
    "binding_digest",
})
PLANE_FIELDS = frozenset({
    "source_artifact_id", "source_calibration_id", "robot_system_id", "place_id",
    "a4_family_digest", "tcp_digest", "table_normal_base",
    "source_artifact_digest", "status", "reference_digest",
})
CAMERA_FIELDS = frozenset({
    "binding_id", "device_kind", "stable_device_id", "intended_role",
    "collection_profile_id", "collection_profile_digest", "connection_state",
    "placement_status", "role_qualification", "production_qualified",
    "binding_digest",
})
NO_AUTHORITY = {
    "execution": "NONE", "human_approval": "NONE", "semantic_pass": "NONE",
    "training_approval": "NONE", "persistent_start_qualification": "NONE",
}
TEST_STATE_FIELDS = frozenset({
    "schema_version", "session_id", "run_id", "root_binding_digest",
    "robot_system_id", "data_disposition", "object_instance_id",
    "object_profile_id", "pose", "declared_by", "declaration_source",
    "scene_state_digest", "scene_revision", "cell_ready",
    "production_writers_enabled", "authority", "initialization_digest",
})
EPISODE_BINDING_FIELDS = frozenset({
    "schema_version", "session_id", "run_id", "intent_digest",
    "manifest_digest", "slot_digest", "resolved_job_digest",
    "root_binding_digest", "start_binding_digest",
    "state_initialization_digest", "scene_state_digest", "place_alias",
    "place_id", "yaw_deg", "x_mm", "y_mm", "robot_start_pose_id",
    "split_group", "repeat_index", "budget_digests", "expires_at",
    "data_disposition", "authority", "binding_digest",
})
PLANNED_START_EVIDENCE_SCHEMA = "data_factory.test_only_planned_start_evidence.v1"


def _exact(value: object, fields: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(code)
    return value


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ContractError(code)
    return value


def _finite(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(code)
    return float(value)


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ContractError(code)
    return value


def _timestamp(value: object, code: str) -> datetime:
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        raise ContractError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    return parsed.astimezone(timezone.utc)


def _no_symlink_components(root: Path, target: Path) -> None:
    current = root
    for part in target.relative_to(root).parts:
        current /= part
        if current.exists() and current.is_symlink():
            raise ContractError("TEST_ONLY_ROOT_SYMLINK")


def build_test_only_root_binding(
    repository_root: str | Path, *, session_id: str, run_id: str,
    run_root: str | Path | None = None, cell_root: str | Path | None = None,
    dataset_root: str | Path | None = None,
) -> dict[str, Any]:
    """Seal the exact ignored roots for one TEST_ONLY run without creating them."""
    session_id = _identifier(session_id, "TEST_ONLY_SESSION_ID")
    run_id = _identifier(run_id, "TEST_ONLY_RUN_ID")
    repository = Path(repository_root).resolve(strict=True)
    expected_run = repository / "outputs/data_factory/test_only_physical" / session_id / "runs"
    expected_cell = repository / "outputs/data_factory/test_only_physical" / session_id / "cells"
    expected_dataset = repository / "datasets/test_only_physical" / session_id / run_id
    supplied = (
        expected_run if run_root is None else Path(run_root),
        expected_cell if cell_root is None else Path(cell_root),
        expected_dataset if dataset_root is None else Path(dataset_root),
    )
    expected = (expected_run, expected_cell, expected_dataset)
    normalized = []
    for value, wanted in zip(supplied, expected):
        resolved = value.resolve(strict=False)
        if resolved != wanted or not resolved.is_absolute():
            raise ContractError("TEST_ONLY_ROOT_MISMATCH")
        try:
            resolved.relative_to(repository)
        except ValueError as exc:
            raise ContractError("TEST_ONLY_ROOT_OUTSIDE_REPOSITORY") from exc
        _no_symlink_components(repository, resolved)
        normalized.append(str(resolved))
    if any(path.exists() for path in expected):
        raise ContractError("TEST_ONLY_ROOT_COLLISION")
    value = {
        "session_id": session_id,
        "run_id": run_id,
        "data_disposition": "TEST_ONLY",
        "run_root": normalized[0],
        "cell_root": normalized[1],
        "dataset_root": normalized[2],
        "production_writers_enabled": False,
    }
    value["binding_digest"] = canonical_digest(value)
    return validate_test_only_root_binding(value, repository_root=repository)


def validate_test_only_root_binding(
    value: object, *, repository_root: str | Path,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, ROOT_FIELDS, "TEST_ONLY_ROOT_FIELDS")))
    if result["data_disposition"] != "TEST_ONLY" or result["production_writers_enabled"] is not False:
        raise ContractError("TEST_ONLY_ROOT_AUTHORITY")
    _identifier(result["session_id"], "TEST_ONLY_SESSION_ID")
    _identifier(result["run_id"], "TEST_ONLY_RUN_ID")
    repository = Path(repository_root).resolve(strict=True)
    wanted = {
        "run_root": repository / "outputs/data_factory/test_only_physical" / result["session_id"] / "runs",
        "cell_root": repository / "outputs/data_factory/test_only_physical" / result["session_id"] / "cells",
        "dataset_root": repository / "datasets/test_only_physical" / result["session_id"] / result["run_id"],
    }
    for field, target in wanted.items():
        path = Path(result[field])
        if not path.is_absolute() or path.resolve(strict=False) != target:
            raise ContractError("TEST_ONLY_ROOT_MISMATCH")
        _no_symlink_components(repository, target)
    if result["binding_digest"] != canonical_digest({key: result[key] for key in result if key != "binding_digest"}):
        raise ContractError("TEST_ONLY_ROOT_DIGEST_MISMATCH")
    return result


def initialize_test_only_state_from_user_declaration(
    roots: Mapping[str, Any], *, repository_root: str | Path,
    robot_system_id: str, object_instance_id: str, object_profile_id: str,
    place_id: str, yaw_deg: int | float, x_mm: int | float, y_mm: int | float,
    declared_by: str,
) -> dict[str, Any]:
    """Seed only isolated TEST_ONLY scene/cell state from an out-of-band user declaration."""
    roots = validate_test_only_root_binding(roots, repository_root=repository_root)
    for value, code in (
        (robot_system_id, "TEST_ONLY_STATE_ROBOT"),
        (object_instance_id, "TEST_ONLY_STATE_OBJECT"),
        (object_profile_id, "TEST_ONLY_STATE_OBJECT"),
        (place_id, "TEST_ONLY_STATE_POSE"),
        (declared_by, "TEST_ONLY_STATE_DECLARER"),
    ):
        _identifier(value, code)
    pose = {
        "place_id": place_id,
        "yaw_deg": _finite(yaw_deg, "TEST_ONLY_STATE_POSE"),
        "x_mm": _finite(x_mm, "TEST_ONLY_STATE_POSE"),
        "y_mm": _finite(y_mm, "TEST_ONLY_STATE_POSE"),
    }
    cell_store = CellStateStore(roots["cell_root"], robot_system_id)
    scene_store = SceneStateStore(roots["cell_root"], robot_system_id)
    if cell_store.read()["reason_code"] != "STATE_MISSING" or scene_store.read()["revision"] != 0:
        raise ContractError("TEST_ONLY_STATE_COLLISION")
    scene = scene_store.update_object(
        instance_id=object_instance_id, object_profile_id=object_profile_id,
        state="ON_SURFACE", pose=pose, source="HUMAN", updated_by=declared_by,
        expected_revision=0,
    )
    cell = cell_store.acknowledge_ready(declared_by)
    value = {
        "schema_version": "data_factory.test_only_state_initialization.v1",
        "session_id": roots["session_id"],
        "run_id": roots["run_id"],
        "root_binding_digest": roots["binding_digest"],
        "robot_system_id": robot_system_id,
        "data_disposition": "TEST_ONLY",
        "object_instance_id": object_instance_id,
        "object_profile_id": object_profile_id,
        "pose": pose,
        "declared_by": declared_by,
        "declaration_source": "USER_PROVIDED_OUT_OF_BAND",
        "scene_state_digest": scene["scene_state_digest"],
        "scene_revision": scene["scene_state"]["revision"],
        "cell_ready": cell["cell_ready"],
        "production_writers_enabled": False,
        "authority": copy.deepcopy(NO_AUTHORITY),
    }
    value["initialization_digest"] = canonical_digest(value)
    return validate_test_only_state_initialization(value, roots=roots)


def validate_test_only_state_initialization(
    value: object, *, roots: Mapping[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, TEST_STATE_FIELDS, "TEST_ONLY_STATE_FIELDS")))
    if (
        result["schema_version"] != "data_factory.test_only_state_initialization.v1"
        or result["session_id"] != roots.get("session_id")
        or result["run_id"] != roots.get("run_id")
        or result["root_binding_digest"] != roots.get("binding_digest")
        or result["data_disposition"] != "TEST_ONLY"
        or result["declaration_source"] != "USER_PROVIDED_OUT_OF_BAND"
        or result["cell_ready"] is not True
        or result["production_writers_enabled"] is not False
        or result["authority"] != NO_AUTHORITY
    ):
        raise ContractError("TEST_ONLY_STATE_BINDING")
    for field in ("robot_system_id", "object_instance_id", "object_profile_id", "declared_by"):
        _identifier(result[field], "TEST_ONLY_STATE_BINDING")
    _digest(result["scene_state_digest"], "TEST_ONLY_STATE_BINDING")
    if type(result["scene_revision"]) is not int or result["scene_revision"] != 1:
        raise ContractError("TEST_ONLY_STATE_BINDING")
    if not isinstance(result["pose"], Mapping) or set(result["pose"]) != {"place_id", "yaw_deg", "x_mm", "y_mm"}:
        raise ContractError("TEST_ONLY_STATE_BINDING")
    _identifier(result["pose"]["place_id"], "TEST_ONLY_STATE_BINDING")
    for field in ("yaw_deg", "x_mm", "y_mm"):
        _finite(result["pose"][field], "TEST_ONLY_STATE_BINDING")
    if result["initialization_digest"] != canonical_digest({key: result[key] for key in result if key != "initialization_digest"}):
        raise ContractError("TEST_ONLY_STATE_DIGEST_MISMATCH")
    return result


def build_test_only_episode_binding(
    *, roots: Mapping[str, Any], repository_root: str | Path,
    manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
    intent: Mapping[str, Any], start_binding: Mapping[str, Any],
    state_initialization: Mapping[str, Any], resolved_job: Mapping[str, Any],
    place_alias: str,
) -> dict[str, Any]:
    """Join one exact campaign intent to its TEST_ONLY run context."""
    roots = validate_test_only_root_binding(roots, repository_root=repository_root)
    manifest = validate_collection_campaign_manifest(manifest, hypothesis=hypothesis)
    intent = validate_seed_episode_intent(intent, manifest=manifest, hypothesis=hypothesis)
    start = validate_test_only_start_binding(
        start_binding, manifest=manifest, hypothesis=hypothesis,
    )
    initialized = validate_test_only_state_initialization(state_initialization, roots=roots)
    place_alias = _identifier(place_alias, "TEST_ONLY_EPISODE_ALIAS")
    if not isinstance(resolved_job, Mapping):
        raise ContractError("TEST_ONLY_EPISODE_JOB")
    receipts = [
        item for item in hypothesis.get("resolver_receipts", [])
        if item.get("resolver_result_digest") == intent["base_condition"]["resolver_result_digest"]
    ]
    if len(receipts) != 1:
        raise ContractError("TEST_ONLY_EPISODE_JOB")
    receipt = receipts[0]
    job = receipt["normalized_job"]
    if (
        resolved_job.get("normalized_job") != job
        or resolved_job.get("resolved_job_digest") != receipt["resolved_job_digest"]
        or resolved_job.get("input_digests") != receipt["input_digests"]
        or intent["run_id"] != roots["run_id"]
        or intent["manifest_digest"] != manifest["manifest_digest"]
        or start["slot_digest"] != intent["slot_digest"]
        or start["robot_start_pose_id"] != intent["robot_start_pose"]["robot_start_pose_id"]
        or initialized["scene_state_digest"] != intent["required_scene_digest"]
        or initialized["robot_system_id"] != job["robot_system_id"]
        or initialized["object_profile_id"] != job["object_profile_id"]
        or initialized["pose"] != {
            key: job[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
        }
    ):
        raise ContractError("TEST_ONLY_EPISODE_BINDING")
    slot = intent["slot"]
    value = {
        "schema_version": "data_factory.test_only_episode_binding.v1",
        "session_id": roots["session_id"],
        "run_id": roots["run_id"],
        "intent_digest": intent["intent_digest"],
        "manifest_digest": intent["manifest_digest"],
        "slot_digest": intent["slot_digest"],
        "resolved_job_digest": receipt["resolved_job_digest"],
        "root_binding_digest": roots["binding_digest"],
        "start_binding_digest": start["binding_digest"],
        "state_initialization_digest": initialized["initialization_digest"],
        "scene_state_digest": initialized["scene_state_digest"],
        "place_alias": place_alias,
        "place_id": job["place_id"],
        "yaw_deg": job["yaw_deg"],
        "x_mm": job["x_mm"],
        "y_mm": job["y_mm"],
        "robot_start_pose_id": slot["robot_start_pose_id"],
        "split_group": slot["split_group"],
        "repeat_index": slot["repeat_index"],
        "budget_digests": copy.deepcopy(intent["budget_digests"]),
        "expires_at": intent["expires_at"],
        "data_disposition": "TEST_ONLY",
        "authority": copy.deepcopy(NO_AUTHORITY),
    }
    value["binding_digest"] = canonical_digest(value)
    return validate_test_only_episode_binding(value, roots=roots, normalized_job=resolved_job)


def validate_test_only_episode_binding(
    value: object, *, roots: Mapping[str, Any], normalized_job: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the compact context consumed immediately before plan approval."""
    result = copy.deepcopy(dict(_exact(
        value, EPISODE_BINDING_FIELDS, "TEST_ONLY_EPISODE_FIELDS",
    )))
    if (
        result["schema_version"] != "data_factory.test_only_episode_binding.v1"
        or result["session_id"] != roots.get("session_id")
        or result["run_id"] != roots.get("run_id")
        or result["root_binding_digest"] != roots.get("binding_digest")
        or result["data_disposition"] != "TEST_ONLY"
        or result["authority"] != NO_AUTHORITY
        or result["resolved_job_digest"] != normalized_job.get("resolved_job_digest")
    ):
        raise ContractError("TEST_ONLY_EPISODE_BINDING")
    job = normalized_job.get("normalized_job")
    if not isinstance(job, Mapping) or any(
        result[field] != job[field] for field in ("place_id", "yaw_deg", "x_mm", "y_mm")
    ):
        raise ContractError("TEST_ONLY_EPISODE_JOB")
    for field in ("session_id", "run_id", "place_alias", "place_id", "robot_start_pose_id"):
        _identifier(result[field], "TEST_ONLY_EPISODE_BINDING")
    for field in (
        "intent_digest", "manifest_digest", "slot_digest", "resolved_job_digest",
        "root_binding_digest", "start_binding_digest", "state_initialization_digest",
        "scene_state_digest",
    ):
        _digest(result[field], "TEST_ONLY_EPISODE_BINDING")
    for field in ("yaw_deg", "x_mm", "y_mm"):
        _finite(result[field], "TEST_ONLY_EPISODE_BINDING")
    if (
        result["split_group"] not in {"TRAIN", "ID", "OOD"}
        or type(result["repeat_index"]) is not int
        or result["repeat_index"] < 0
        or not isinstance(result["budget_digests"], Mapping)
        or set(result["budget_digests"]) != SEED_BUDGET_DIGEST_FIELDS
    ):
        raise ContractError("TEST_ONLY_EPISODE_BINDING")
    for digest in result["budget_digests"].values():
        _digest(digest, "TEST_ONLY_EPISODE_BINDING")
    _timestamp(result["expires_at"], "TEST_ONLY_EPISODE_EXPIRY")
    if result["binding_digest"] != canonical_digest({
        key: result[key] for key in result if key != "binding_digest"
    }):
        raise ContractError("TEST_ONLY_EPISODE_DIGEST_MISMATCH")
    return result


def build_test_only_start_binding(
    *, manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
    motion_qualification: Mapping[str, Any], home_candidate: Mapping[str, Any],
    current_snapshot: Mapping[str, Any], max_snapshot_age_s: float = 0.1,
) -> dict[str, Any]:
    """Bind one fresh HOME-range snapshot to one exact TEST_ONLY slot."""
    hypothesis = validate_fr5_hypothesis(hypothesis)
    manifest = validate_collection_campaign_manifest(manifest, hypothesis=hypothesis)
    if len(manifest["slots"]) != 1:
        raise ContractError("TEST_ONLY_START_EXACT_ONE_SLOT")
    if not isinstance(motion_qualification, Mapping) or motion_qualification.get("schema_version") != "data_factory.motion_qualification.v1" or motion_qualification.get("qualification_status") != "QUALIFIED":
        raise ContractError("TEST_ONLY_START_MOTION_QUALIFICATION")
    if not isinstance(home_candidate, Mapping) or home_candidate.get("schema_version") != "data_factory.home_candidate.v1":
        raise ContractError("TEST_ONLY_START_HOME_CANDIDATE")
    home_digest = canonical_digest(home_candidate)
    if motion_qualification.get("home_candidate_digest") != home_digest:
        raise ContractError("TEST_ONLY_START_HOME_DIGEST")
    fixed = hypothesis["fixed_contract"]
    slot = manifest["slots"][0]
    poses = [
        item for item in hypothesis["robot_start_poses"]
        if item["robot_start_pose_id"] == slot["robot_start_pose_id"]
    ]
    if len(poses) != 1:
        raise ContractError("TEST_ONLY_START_QUALIFICATION")
    pose = poses[0]
    if (
        home_candidate.get("robot_system_id") != fixed["robot_system_id"]
        or home_candidate.get("joint_order") != list(JOINTS)
        or motion_qualification.get("robot_system_id") != fixed["robot_system_id"]
        or motion_qualification.get("cell_calibration_id") != fixed["cell_calibration_id"]
        or motion_qualification.get("object_profile_id") != fixed["object_profile_id"]
        or motion_qualification.get("grasp_profile_id") != fixed["grasp_profile_id"]
        or pose["home_candidate_digest"] != home_digest
    ):
        raise ContractError("TEST_ONLY_START_QUALIFICATION")
    snapshot = _validate_snapshot(copy.deepcopy(dict(current_snapshot)))
    max_age = _finite(max_snapshot_age_s, "TEST_ONLY_START_AGE")
    qualified_age = _finite(motion_qualification.get("max_joint_state_age_s"), "TEST_ONLY_START_AGE")
    if max_age <= 0 or max_age > 0.1 or qualified_age > 0.1:
        raise ContractError("TEST_ONLY_START_AGE")
    if max(snapshot["joint_state_age_s"], snapshot["ros_sample_age_s"]) > min(max_age, qualified_age):
        raise ContractError("TEST_ONLY_START_STALE")
    target = motion_qualification.get("qualified_safe_joint_positions_rad")
    tolerance = motion_qualification.get("goal_tolerances", {}).get("joint_rad")
    if (
        not isinstance(target, list) or len(target) != len(JOINTS)
        or any(not math.isfinite(_finite(item, "TEST_ONLY_START_TARGET")) for item in target)
        or _finite(tolerance, "TEST_ONLY_START_TOLERANCE") <= 0
        or float(tolerance) > 0.01
    ):
        raise ContractError("TEST_ONLY_START_TARGET")
    expected_target = [pose["target_rad"][joint] for joint in JOINTS]
    if (
        any(abs(actual - expected) > 1e-9 for actual, expected in zip(target, expected_target))
        or any(float(pose["tolerance_rad"][joint]) < float(tolerance) for joint in JOINTS)
    ):
        raise ContractError("TEST_ONLY_START_QUALIFICATION")
    current = [snapshot["joint_positions_rad"][joint] for joint in JOINTS]
    if any(abs(actual - expected) > float(tolerance) for actual, expected in zip(current, target)):
        raise ContractError("TEST_ONLY_START_OUTSIDE_HOME")
    value = {
        "scope": "MOTION_Q_SAFE_START",
        "data_disposition": "TEST_ONLY",
        "manifest_digest": manifest["manifest_digest"],
        "slot_digest": canonical_digest(slot),
        "robot_start_pose_id": slot["robot_start_pose_id"],
        "robot_start_pose_qualification_digest": pose["qualification_digest"],
        "motion_qualification_id": _identifier(
            motion_qualification.get("motion_qualification_id"),
            "TEST_ONLY_START_MOTION_QUALIFICATION",
        ),
        "motion_qualification_digest": canonical_digest(motion_qualification),
        "home_candidate_digest": home_digest,
        "joint_order": list(JOINTS),
        "target_rad": [float(item) for item in target],
        "current_rad": current,
        "tolerance_rad": float(tolerance),
        "max_snapshot_age_s": min(max_age, qualified_age),
        "snapshot_digest": canonical_digest(snapshot),
        "status": "BOUND_TEST_ONLY",
        "authority": copy.deepcopy(NO_AUTHORITY),
    }
    value["binding_digest"] = canonical_digest(value)
    return validate_test_only_start_binding(value, manifest=manifest, hypothesis=hypothesis)


def validate_test_only_start_binding(
    value: object, *, manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
) -> dict[str, Any]:
    hypothesis = validate_fr5_hypothesis(hypothesis)
    manifest = validate_collection_campaign_manifest(manifest, hypothesis=hypothesis)
    result = _validate_test_only_start_shape(value)
    poses = [
        item for item in hypothesis["robot_start_poses"]
        if item["robot_start_pose_id"] == result.get("robot_start_pose_id")
    ]
    if (
        len(manifest.get("slots", [])) != 1
        or result["manifest_digest"] != manifest.get("manifest_digest")
        or result["slot_digest"] != canonical_digest(manifest["slots"][0])
        or result["robot_start_pose_id"] != manifest["slots"][0]["robot_start_pose_id"]
        or len(poses) != 1
        or result["robot_start_pose_qualification_digest"] != poses[0]["qualification_digest"]
    ):
        raise ContractError("TEST_ONLY_START_BINDING")
    return result


def _validate_test_only_start_shape(value: object) -> dict[str, Any]:
    """Validate the sealed target binding without claiming its old snapshot is current."""
    result = copy.deepcopy(dict(_exact(value, START_FIELDS, "TEST_ONLY_START_FIELDS")))
    if (
        result["scope"] != "MOTION_Q_SAFE_START"
        or result["data_disposition"] != "TEST_ONLY"
        or result["status"] != "BOUND_TEST_ONLY"
        or result["authority"] != NO_AUTHORITY
    ):
        raise ContractError("TEST_ONLY_START_BINDING")
    for field in ("robot_start_pose_id", "motion_qualification_id"):
        _identifier(result[field], "TEST_ONLY_START_BINDING")
    for field in (
        "manifest_digest", "slot_digest", "robot_start_pose_qualification_digest",
        "motion_qualification_digest", "home_candidate_digest", "snapshot_digest",
    ):
        _digest(result[field], "TEST_ONLY_START_BINDING")
    tolerance = _finite(result["tolerance_rad"], "TEST_ONLY_START_BINDING")
    max_age = _finite(result["max_snapshot_age_s"], "TEST_ONLY_START_BINDING")
    if (
        result["joint_order"] != list(JOINTS)
        or not isinstance(result["target_rad"], list)
        or not isinstance(result["current_rad"], list)
        or len(result["target_rad"]) != len(JOINTS)
        or len(result["current_rad"]) != len(JOINTS)
        or not 0 < tolerance <= 0.01
        or not 0 < max_age <= 0.1
    ):
        raise ContractError("TEST_ONLY_START_BINDING")
    target = [_finite(item, "TEST_ONLY_START_BINDING") for item in result["target_rad"]]
    current = [_finite(item, "TEST_ONLY_START_BINDING") for item in result["current_rad"]]
    if any(abs(actual - expected) > tolerance for actual, expected in zip(current, target)):
        raise ContractError("TEST_ONLY_START_BINDING")
    if result["binding_digest"] != canonical_digest({key: result[key] for key in result if key != "binding_digest"}):
        raise ContractError("TEST_ONLY_START_DIGEST_MISMATCH")
    return result


def validate_test_only_planned_start(
    *, start_binding: Mapping[str, Any], episode_binding: Mapping[str, Any],
    motion_program: Mapping[str, Any], plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove the executor's fresh plan snapshot still matches the qualified HOME target."""
    start = _validate_test_only_start_shape(start_binding)
    if (
        not isinstance(episode_binding, Mapping)
        or episode_binding.get("start_binding_digest") != start["binding_digest"]
        or not isinstance(motion_program, Mapping)
        or not isinstance(motion_program.get("binding_digests"), Mapping)
        or not isinstance(motion_program.get("planning"), Mapping)
        or not isinstance(plan, Mapping)
        or plan.get("binding_digests") != motion_program["binding_digests"]
        or not isinstance(plan.get("initial_joint_state"), list)
        or len(plan["initial_joint_state"]) != len(JOINTS)
    ):
        raise ContractError("TEST_ONLY_PLANNED_START")
    bindings = motion_program["binding_digests"]
    max_age = _finite(
        motion_program["planning"].get("max_joint_state_age_s"),
        "TEST_ONLY_PLANNED_START",
    )
    if (
        bindings.get("motion_qualification") != start["motion_qualification_digest"]
        or bindings.get("home_candidate") != start["home_candidate_digest"]
        or not 0 < max_age <= start["max_snapshot_age_s"]
    ):
        raise ContractError("TEST_ONLY_PLANNED_START")
    initial = [_finite(item, "TEST_ONLY_PLANNED_START") for item in plan["initial_joint_state"]]
    maximum = max(abs(actual - target) for actual, target in zip(initial, start["target_rad"]))
    if maximum > start["tolerance_rad"]:
        raise ContractError("TEST_ONLY_PLANNED_START_MISMATCH")
    evidence = {
        "schema_version": PLANNED_START_EVIDENCE_SCHEMA,
        "start_binding_digest": start["binding_digest"],
        "motion_qualification_digest": start["motion_qualification_digest"],
        "home_candidate_digest": start["home_candidate_digest"],
        "plan_digest": canonical_digest(plan),
        "initial_joint_state": initial,
        "target_rad": copy.deepcopy(start["target_rad"]),
        "max_joint_delta_rad": maximum,
        "tolerance_rad": start["tolerance_rad"],
        "max_joint_state_age_s": max_age,
        "status": "PASS",
        "authority": copy.deepcopy(NO_AUTHORITY),
    }
    evidence["evidence_digest"] = canonical_digest(evidence)
    return evidence


def qualified_table_plane_reference(cell_calibration: Mapping[str, Any]) -> dict[str, Any]:
    """Project an explicit qualified plane reference; never accept a bare normal."""
    if (
        not isinstance(cell_calibration, Mapping)
        or cell_calibration.get("schema_version") != "data_factory.cell_calibration.v1"
        or cell_calibration.get("qualification_status") != "QUALIFIED"
    ):
        raise ContractError("WORKSPACE_PLANE_UNQUALIFIED")
    normal = cell_calibration.get("table_normal_base")
    if not isinstance(normal, list) or len(normal) != 3 or any(not math.isfinite(_finite(item, "WORKSPACE_PLANE")) for item in normal):
        raise ContractError("WORKSPACE_PLANE")
    value = {
        "source_artifact_id": _identifier(cell_calibration.get("calibration_id"), "WORKSPACE_PLANE"),
        "source_calibration_id": cell_calibration["calibration_id"],
        "robot_system_id": _identifier(cell_calibration.get("robot_system_id"), "WORKSPACE_PLANE"),
        "place_id": _identifier(cell_calibration.get("place_id"), "WORKSPACE_PLANE"),
        "a4_family_digest": _digest(cell_calibration.get("a4_family_digest"), "WORKSPACE_PLANE"),
        "tcp_digest": _digest(cell_calibration.get("tcp_digest"), "WORKSPACE_PLANE"),
        "table_normal_base": [float(item) for item in normal],
        "source_artifact_digest": canonical_digest(cell_calibration),
        "status": "QUALIFIED_REFERENCE",
    }
    value["reference_digest"] = canonical_digest(value)
    return validate_table_plane_reference(value)


def validate_table_plane_reference(value: object) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, PLANE_FIELDS, "WORKSPACE_PLANE_FIELDS")))
    if result["status"] != "QUALIFIED_REFERENCE" or result["source_artifact_id"] != result["source_calibration_id"]:
        raise ContractError("WORKSPACE_PLANE")
    for field in ("source_artifact_id", "robot_system_id", "place_id"):
        _identifier(result[field], "WORKSPACE_PLANE")
    for field in ("source_artifact_digest", "a4_family_digest", "tcp_digest"):
        _digest(result[field], "WORKSPACE_PLANE")
    if not isinstance(result["table_normal_base"], list) or len(result["table_normal_base"]) != 3:
        raise ContractError("WORKSPACE_PLANE")
    normal = [_finite(item, "WORKSPACE_PLANE") for item in result["table_normal_base"]]
    length = math.sqrt(sum(item * item for item in normal))
    if abs(length - 1.0) > 1e-6:
        raise ContractError("WORKSPACE_PLANE")
    if result["reference_digest"] != canonical_digest({key: result[key] for key in result if key != "reference_digest"}):
        raise ContractError("WORKSPACE_PLANE_DIGEST_MISMATCH")
    return result


def validate_print_measurements(
    *, source_scale_bar_mm: float, final_scale_bar_mm: float, nominal_mm: float = 100.0,
    max_final_error_mm: float = 4.0,
) -> dict[str, Any]:
    source = _finite(source_scale_bar_mm, "WORKSPACE_PRINT_MEASUREMENT")
    final = _finite(final_scale_bar_mm, "WORKSPACE_PRINT_MEASUREMENT")
    nominal = _finite(nominal_mm, "WORKSPACE_PRINT_MEASUREMENT")
    limit = _finite(max_final_error_mm, "WORKSPACE_PRINT_MEASUREMENT")
    if min(source, final, nominal, limit) <= 0:
        raise ContractError("WORKSPACE_PRINT_MEASUREMENT")
    if abs(final - nominal) > limit:
        raise ContractError("WORKSPACE_FINAL_PRINT_OUT_OF_TOLERANCE")
    value = {
        "source_scale_bar_measured_mm": source,
        "compensation_scale": nominal / source,
        "final_scale_bar_measured_mm": final,
        "nominal_scale_bar_mm": nominal,
        "max_final_error_mm": limit,
        "status": "FINAL_PRINT_MEASUREMENT_BOUND",
    }
    value["measurement_digest"] = canonical_digest(value)
    return value


def compile_workspace_registration_candidate(
    *, center_snapshot: Mapping[str, Any], x_ref_snapshot: Mapping[str, Any],
    y_check_snapshot: Mapping[str, Any], plane_reference: Mapping[str, Any],
    print_measurements: Mapping[str, Any], calibration_id: str, place_id: str,
    operator_or_agent_id: str, yaw0_sheet: str | Path,
    tcp_candidate_manifest: str | Path, output_root: str | Path,
    tolerance_mm: float, robot_system_id: str = "fr5-lab-a",
    max_snapshot_age_s: float = 0.5,
) -> dict[str, Any]:
    """Validate the three-point wizard inputs, then reuse the preview-only calibrator."""
    plane = validate_table_plane_reference(plane_reference)
    try:
        measured = validate_print_measurements(
            source_scale_bar_mm=print_measurements["source_scale_bar_measured_mm"],
            final_scale_bar_mm=print_measurements["final_scale_bar_measured_mm"],
            nominal_mm=print_measurements["nominal_scale_bar_mm"],
            max_final_error_mm=print_measurements["max_final_error_mm"],
        )
    except (KeyError, TypeError) as exc:
        raise ContractError("WORKSPACE_PRINT_MEASUREMENT") from exc
    if dict(print_measurements) != measured:
        raise ContractError("WORKSPACE_PRINT_MEASUREMENT_DIGEST_MISMATCH")
    max_age = _finite(max_snapshot_age_s, "WORKSPACE_SNAPSHOT_AGE")
    if max_age <= 0:
        raise ContractError("WORKSPACE_SNAPSHOT_AGE")
    snapshots = []
    for snapshot in (center_snapshot, x_ref_snapshot, y_check_snapshot):
        checked = _validate_snapshot(copy.deepcopy(dict(snapshot)))
        if max(checked["joint_state_age_s"], checked["ros_sample_age_s"]) > max_age:
            raise ContractError("WORKSPACE_SNAPSHOT_STALE")
        snapshots.append(checked)
    sheet = validate_yaw0_sheet(load_json_strict(yaw0_sheet))
    if (
        plane["place_id"] != place_id
        or plane["robot_system_id"] != robot_system_id
        or sheet["place_id"] != place_id
        or sheet["a4_family_digest"] != plane["a4_family_digest"]
        or float(sheet["print_calibration"]["measured_scale_bar_mm"])
        != measured["source_scale_bar_measured_mm"]
        or any(snapshot["base_tcp"]["candidate_source_sha256"] != plane["tcp_digest"] for snapshot in snapshots)
    ):
        raise ContractError("WORKSPACE_REGISTRATION_BINDING")
    result = calibrate_place(
        snapshots[0], snapshots[1], ycheck_snapshot=snapshots[2],
        calibration_id=calibration_id, place_id=place_id,
        operator_or_agent_id=operator_or_agent_id, yaw0_sheet=yaw0_sheet,
        tcp_candidate_manifest=tcp_candidate_manifest, output_root=output_root,
        tolerance_mm=tolerance_mm,
        scale_bar_mm=measured["final_scale_bar_measured_mm"],
        table_normal=plane["table_normal_base"], robot_system_id=robot_system_id,
    )
    if result.get("execution_authorized") is not False or result.get("training_approved") is not False:
        raise ContractError("WORKSPACE_REGISTRATION_AUTHORITY")
    return result


def build_camera_binding_candidate(
    *, binding_id: str, device_kind: str, stable_device_id: str,
    intended_role: str, collection_profile: Mapping[str, Any], connected: bool,
) -> dict[str, Any]:
    if device_kind not in {"UVC", "REALSENSE"}:
        raise ContractError("CAMERA_BINDING_DEVICE_KIND")
    _identifier(binding_id, "CAMERA_BINDING_ID")
    _identifier(stable_device_id, "CAMERA_BINDING_DEVICE_ID")
    _identifier(intended_role, "CAMERA_BINDING_ROLE")
    if (
        not isinstance(collection_profile, Mapping)
        or collection_profile.get("schema_version") != "data_factory.collection_profile.v2"
        or intended_role not in collection_profile.get("camera_roles", [])
    ):
        raise ContractError("CAMERA_BINDING_PROFILE")
    if type(connected) is not bool:
        raise ContractError("CAMERA_BINDING_STATE")
    value = {
        "binding_id": binding_id,
        "device_kind": device_kind,
        "stable_device_id": stable_device_id,
        "intended_role": intended_role,
        "collection_profile_id": collection_profile["collection_profile_id"],
        "collection_profile_digest": canonical_digest(collection_profile),
        "connection_state": "CONNECTED" if connected else "NOT_AVAILABLE",
        "placement_status": "UNPLACED",
        "role_qualification": "NOT_QUALIFIED",
        "production_qualified": False,
    }
    value["binding_digest"] = canonical_digest(value)
    return validate_camera_binding_candidate(value)


def validate_camera_binding_candidate(value: object) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, CAMERA_FIELDS, "CAMERA_BINDING_FIELDS")))
    if (
        result["device_kind"] not in {"UVC", "REALSENSE"}
        or result["connection_state"] not in {"CONNECTED", "NOT_AVAILABLE"}
        or result["placement_status"] != "UNPLACED"
        or result["role_qualification"] != "NOT_QUALIFIED"
        or result["production_qualified"] is not False
    ):
        raise ContractError("CAMERA_BINDING_AUTHORITY")
    for field in ("binding_id", "stable_device_id", "intended_role", "collection_profile_id"):
        _identifier(result[field], "CAMERA_BINDING_ID")
    _digest(result["collection_profile_digest"], "CAMERA_BINDING_DIGEST")
    if result["binding_digest"] != canonical_digest({key: result[key] for key in result if key != "binding_digest"}):
        raise ContractError("CAMERA_BINDING_DIGEST_MISMATCH")
    return result


def gripper_setup_projection(readback: Mapping[str, Any] | None) -> dict[str, Any]:
    """Describe attach vs maintenance exception without invoking activation."""
    if readback is None:
        return {"state": "NOT_AVAILABLE", "supported_action": "NONE", "maintenance_call_count": 0}
    if not isinstance(readback, Mapping) or set(readback) != {"active", "position_valid", "gripper_index"}:
        raise ContractError("GRIPPER_SETUP_READBACK")
    if type(readback["active"]) is not bool or type(readback["position_valid"]) is not bool or type(readback["gripper_index"]) is not int:
        raise ContractError("GRIPPER_SETUP_READBACK")
    if readback["gripper_index"] != 1:
        return {"state": "BLOCKED_BINDING", "supported_action": "NONE", "maintenance_call_count": 0}
    if readback["active"] and readback["position_valid"]:
        return {"state": "ATTACHED", "supported_action": "VERIFY", "maintenance_call_count": 0}
    return {
        "state": "MAINTENANCE_APPROVAL_REQUIRED",
        "supported_action": "REQUEST_ACTIVATE_AND_NORMALIZE",
        "maintenance_call_count": 0,
    }
