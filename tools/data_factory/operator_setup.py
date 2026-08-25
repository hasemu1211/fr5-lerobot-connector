"""Pure setup bindings for the collection desk.

This module discovers no devices and commands no hardware.  Callers provide
snapshots/documents and receive non-authoritative, session-local projections.
"""
from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Mapping

from tools.data_factory.campaign_authoring import validate_collection_campaign_manifest
from tools.data_factory.motion.pose_snapshot import JOINTS, _validate_snapshot
from tools.fr5_data_factory import ContractError, DIGEST, SAFE_ID, canonical_digest


ROOT_FIELDS = frozenset({
    "session_id", "run_id", "data_disposition", "run_root", "cell_root",
    "dataset_root", "production_writers_enabled", "binding_digest",
})
START_FIELDS = frozenset({
    "scope", "data_disposition", "manifest_digest", "slot_digest",
    "robot_start_pose_id", "motion_qualification_id", "motion_qualification_digest",
    "home_candidate_digest", "joint_order", "target_rad", "current_rad",
    "tolerance_rad", "max_snapshot_age_s", "snapshot_digest", "status", "authority",
    "binding_digest",
})
PLANE_FIELDS = frozenset({
    "source_artifact_id", "source_calibration_id", "table_normal_base",
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


def build_test_only_start_binding(
    *, manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
    motion_qualification: Mapping[str, Any], home_candidate: Mapping[str, Any],
    current_snapshot: Mapping[str, Any], max_snapshot_age_s: float = 0.1,
) -> dict[str, Any]:
    """Bind one fresh HOME-range snapshot to one exact TEST_ONLY slot."""
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
    current = [snapshot["joint_positions_rad"][joint] for joint in JOINTS]
    if any(abs(actual - expected) > float(tolerance) for actual, expected in zip(current, target)):
        raise ContractError("TEST_ONLY_START_OUTSIDE_HOME")
    slot = manifest["slots"][0]
    value = {
        "scope": "MOTION_Q_SAFE_START",
        "data_disposition": "TEST_ONLY",
        "manifest_digest": manifest["manifest_digest"],
        "slot_digest": canonical_digest(slot),
        "robot_start_pose_id": slot["robot_start_pose_id"],
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
    return validate_test_only_start_binding(value, manifest=manifest)


def validate_test_only_start_binding(
    value: object, *, manifest: Mapping[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, START_FIELDS, "TEST_ONLY_START_FIELDS")))
    if (
        result["scope"] != "MOTION_Q_SAFE_START"
        or result["data_disposition"] != "TEST_ONLY"
        or result["status"] != "BOUND_TEST_ONLY"
        or result["authority"] != NO_AUTHORITY
        or len(manifest.get("slots", [])) != 1
        or result["manifest_digest"] != manifest.get("manifest_digest")
        or result["slot_digest"] != canonical_digest(manifest["slots"][0])
        or result["robot_start_pose_id"] != manifest["slots"][0]["robot_start_pose_id"]
        or result["joint_order"] != list(JOINTS)
    ):
        raise ContractError("TEST_ONLY_START_BINDING")
    for field in ("manifest_digest", "slot_digest", "motion_qualification_digest", "home_candidate_digest", "snapshot_digest"):
        _digest(result[field], "TEST_ONLY_START_BINDING")
    tolerance_value = _finite(result["tolerance_rad"], "TEST_ONLY_START_BINDING")
    age_value = _finite(result["max_snapshot_age_s"], "TEST_ONLY_START_BINDING")
    if (
        not isinstance(result["target_rad"], list) or not isinstance(result["current_rad"], list)
        or len(result["target_rad"]) != len(JOINTS) or len(result["current_rad"]) != len(JOINTS)
        or not 0 < tolerance_value <= 0.01
        or not 0 < age_value <= 0.1
        or any(abs(_finite(actual, "TEST_ONLY_START_BINDING") - _finite(expected, "TEST_ONLY_START_BINDING")) > result["tolerance_rad"] for actual, expected in zip(result["current_rad"], result["target_rad"]))
    ):
        raise ContractError("TEST_ONLY_START_BINDING")
    if result["binding_digest"] != canonical_digest({key: result[key] for key in result if key != "binding_digest"}):
        raise ContractError("TEST_ONLY_START_DIGEST_MISMATCH")
    return result


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
    _identifier(result["source_artifact_id"], "WORKSPACE_PLANE")
    _digest(result["source_artifact_digest"], "WORKSPACE_PLANE")
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
