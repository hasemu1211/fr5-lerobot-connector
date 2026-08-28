"""Immutable named collection start poses captured from read-only joint state."""
from __future__ import annotations

import copy
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.fr5_data_factory import (
    ContractError,
    DIGEST,
    SAFE_ID,
    canonical_digest,
    load_json_strict,
)


JOINTS = ("j1", "j2", "j3", "j4", "j5", "j6")
SNAPSHOT_FIELDS = frozenset({
    "schema_version", "source", "robot_system_id", "joint_order",
    "joint_positions_rad", "captured_at",
})
CAPTURE_FIELDS = frozenset({
    "schema_version", "source", "captured_at", "max_snapshot_age_s",
    "snapshot_digest",
})
PROFILE_FIELDS = frozenset({
    "schema_version", "start_pose_id", "display_name", "robot_system_id",
    "joint_order", "target_rad", "tolerance_rad", "capture_provenance",
    "recovery_home_digest", "qualification_status", "safety_status",
    "authority", "profile_digest",
})
QUALIFICATION_STATUSES = frozenset({"CANDIDATE", "QUALIFIED"})
SAFETY_STATUSES = frozenset({
    "UNASSESSED", "NOT_SAFE_FOR_MOTION", "SAFE_FOR_MOTION",
})


def _exact(value: object, fields: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(code)
    return value


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ContractError(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ContractError(code)
    return value


def _finite(value: object, code: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(code)
    result = float(value)
    if positive and result <= 0:
        raise ContractError(code)
    return result


def _joint_map(value: object, code: str, *, positive: bool = False) -> dict[str, float]:
    joints = _exact(value, frozenset(JOINTS), code)
    return {joint: _finite(joints[joint], code, positive=positive) for joint in JOINTS}


def _timestamp(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise ContractError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fresh(captured_at: str, max_age_s: float, now: datetime | None) -> None:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ContractError("START_POSE_NOW")
    captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    age_s = (current.astimezone(timezone.utc) - captured).total_seconds()
    if age_s < 0 or age_s > max_age_s:
        raise ContractError("START_POSE_SNAPSHOT_STALE")


def _snapshot(
    value: object, *, robot_system_id: str, max_snapshot_age_s: float,
    now: datetime | None,
) -> dict[str, Any]:
    source = copy.deepcopy(dict(_exact(value, SNAPSHOT_FIELDS, "START_POSE_SNAPSHOT_FIELDS")))
    if (
        source["schema_version"] != "data_factory.start_pose_joint_snapshot.v1"
        or source["source"] != "READ_ONLY_JOINT_STATE"
        or source["robot_system_id"] != robot_system_id
        or source["joint_order"] != list(JOINTS)
    ):
        raise ContractError("START_POSE_SNAPSHOT")
    source["joint_positions_rad"] = _joint_map(
        source["joint_positions_rad"], "START_POSE_TARGET",
    )
    source["captured_at"] = _timestamp(source["captured_at"], "START_POSE_CAPTURE_TIME")
    _fresh(source["captured_at"], max_snapshot_age_s, now)
    return source


def compile_start_pose_profile(
    *, start_pose_id: str, display_name: str, robot_system_id: str,
    snapshot: Mapping[str, Any], tolerance_rad: Mapping[str, Any],
    recovery_home_digest: str, qualification_status: str,
    safety_status: str, max_snapshot_age_s: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compile one profile from a fresh read-only joint snapshot; grant no motion."""
    start_pose_id = _identifier(start_pose_id, "START_POSE_ID")
    robot_system_id = _identifier(robot_system_id, "START_POSE_ROBOT")
    if (
        not isinstance(display_name, str) or not display_name
        or display_name.strip() != display_name or not display_name.isprintable()
        or len(display_name) > 100
    ):
        raise ContractError("START_POSE_DISPLAY_NAME")
    if not isinstance(qualification_status, str) or qualification_status not in QUALIFICATION_STATUSES:
        raise ContractError("START_POSE_QUALIFICATION_STATUS")
    if not isinstance(safety_status, str) or safety_status not in SAFETY_STATUSES:
        raise ContractError("START_POSE_SAFETY_STATUS")
    maximum_age = _finite(max_snapshot_age_s, "START_POSE_MAX_AGE", positive=True)
    captured = _snapshot(
        snapshot, robot_system_id=robot_system_id,
        max_snapshot_age_s=maximum_age, now=now,
    )
    draft = {
        "schema_version": "data_factory.start_pose_profile.v1",
        "start_pose_id": start_pose_id,
        "display_name": display_name,
        "robot_system_id": robot_system_id,
        "joint_order": list(JOINTS),
        "target_rad": copy.deepcopy(captured["joint_positions_rad"]),
        "tolerance_rad": _joint_map(
            tolerance_rad, "START_POSE_TOLERANCE", positive=True,
        ),
        "capture_provenance": {
            "schema_version": "data_factory.start_pose_capture_provenance.v1",
            "source": "READ_ONLY_JOINT_STATE",
            "captured_at": captured["captured_at"],
            "max_snapshot_age_s": maximum_age,
            "snapshot_digest": canonical_digest(captured),
        },
        "recovery_home_digest": _digest(
            recovery_home_digest, "START_POSE_RECOVERY_HOME_DIGEST",
        ),
        "qualification_status": qualification_status,
        "safety_status": safety_status,
        "authority": "NO_EXECUTION_AUTHORITY",
    }
    draft["profile_digest"] = canonical_digest(draft)
    return validate_start_pose_profile(draft)


def validate_start_pose_profile(value: object) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, PROFILE_FIELDS, "START_POSE_PROFILE_FIELDS")))
    if result["schema_version"] != "data_factory.start_pose_profile.v1":
        raise ContractError("START_POSE_PROFILE_SCHEMA")
    _identifier(result["start_pose_id"], "START_POSE_ID")
    _identifier(result["robot_system_id"], "START_POSE_ROBOT")
    if (
        not isinstance(result["display_name"], str) or not result["display_name"]
        or result["display_name"].strip() != result["display_name"]
        or not result["display_name"].isprintable()
        or len(result["display_name"]) > 100
    ):
        raise ContractError("START_POSE_DISPLAY_NAME")
    if result["joint_order"] != list(JOINTS):
        raise ContractError("START_POSE_JOINT_ORDER")
    result["target_rad"] = _joint_map(result["target_rad"], "START_POSE_TARGET")
    result["tolerance_rad"] = _joint_map(
        result["tolerance_rad"], "START_POSE_TOLERANCE", positive=True,
    )
    capture = copy.deepcopy(dict(_exact(
        result["capture_provenance"], CAPTURE_FIELDS,
        "START_POSE_CAPTURE_PROVENANCE_FIELDS",
    )))
    if (
        capture["schema_version"] != "data_factory.start_pose_capture_provenance.v1"
        or capture["source"] != "READ_ONLY_JOINT_STATE"
    ):
        raise ContractError("START_POSE_CAPTURE_PROVENANCE")
    capture["captured_at"] = _timestamp(capture["captured_at"], "START_POSE_CAPTURE_TIME")
    capture["max_snapshot_age_s"] = _finite(
        capture["max_snapshot_age_s"], "START_POSE_MAX_AGE", positive=True,
    )
    expected_snapshot = {
        "schema_version": "data_factory.start_pose_joint_snapshot.v1",
        "source": capture["source"],
        "robot_system_id": result["robot_system_id"],
        "joint_order": list(JOINTS),
        "joint_positions_rad": copy.deepcopy(result["target_rad"]),
        "captured_at": capture["captured_at"],
    }
    if (
        _digest(capture["snapshot_digest"], "START_POSE_SNAPSHOT_DIGEST")
        != canonical_digest(expected_snapshot)
    ):
        raise ContractError("START_POSE_CAPTURE_DIGEST_MISMATCH")
    result["capture_provenance"] = capture
    _digest(result["recovery_home_digest"], "START_POSE_RECOVERY_HOME_DIGEST")
    if (
        not isinstance(result["qualification_status"], str)
        or result["qualification_status"] not in QUALIFICATION_STATUSES
    ):
        raise ContractError("START_POSE_QUALIFICATION_STATUS")
    if (
        not isinstance(result["safety_status"], str)
        or result["safety_status"] not in SAFETY_STATUSES
    ):
        raise ContractError("START_POSE_SAFETY_STATUS")
    if result["authority"] != "NO_EXECUTION_AUTHORITY":
        raise ContractError("START_POSE_AUTHORITY")
    if (
        _digest(result["profile_digest"], "START_POSE_PROFILE_DIGEST")
        != canonical_digest({key: item for key, item in result.items() if key != "profile_digest"})
    ):
        raise ContractError("START_POSE_PROFILE_DIGEST_MISMATCH")
    return result


def project_robot_start_pose_qualification(value: object) -> dict[str, Any]:
    """Project only separately qualified, safe profiles into the frozen P5.8a schema."""
    profile = validate_start_pose_profile(value)
    if (
        profile["qualification_status"] != "QUALIFIED"
        or profile["safety_status"] != "SAFE_FOR_MOTION"
    ):
        raise ContractError("START_POSE_NOT_QUALIFIED")
    result = {
        "schema_version": "data_factory.robot_start_pose_qualification.v1",
        "source": "QUALIFICATION_ARTIFACT",
        "robot_system_id": profile["robot_system_id"],
        "robot_start_pose_id": profile["start_pose_id"],
        "joint_order": copy.deepcopy(profile["joint_order"]),
        "target_rad": copy.deepcopy(profile["target_rad"]),
        "tolerance_rad": copy.deepcopy(profile["tolerance_rad"]),
        "home_candidate_digest": profile["recovery_home_digest"],
        "qualification_status": "QUALIFIED",
        "safety_status": "SAFE_FOR_MOTION",
    }
    result["qualification_digest"] = canonical_digest(result)
    return result


def _load(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ContractError("START_POSE_REGISTRY")
        return validate_start_pose_profile(load_json_strict(path))
    except OSError as exc:
        raise ContractError("START_POSE_REGISTRY") from exc


def _publish(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ) + "\n").encode()
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    published = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        published = True
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        if published:
            path.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def save_start_pose_profile(
    start_poses: str | Path, value: object, *, now: datetime | None = None,
) -> dict[str, Any]:
    """Exclusively persist one profile; identical retries return the existing value."""
    profile = validate_start_pose_profile(value)
    if not isinstance(start_poses, (str, Path)):
        raise ContractError("START_POSE_REGISTRY")
    root = Path(start_poses)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ContractError("START_POSE_REGISTRY")
    existing_profiles = list_start_pose_profiles(root) if root.exists() else []
    if any(
        item["recovery_home_digest"] != profile["recovery_home_digest"]
        for item in existing_profiles
    ):
        raise ContractError("START_POSE_RECOVERY_HOME_CONFLICT")
    target = root / f"{profile['start_pose_id']}.json"
    if target.exists() or target.is_symlink():
        existing = _load(target)
        if existing != profile:
            raise ContractError("START_POSE_ID_CONFLICT")
        return existing
    _fresh(
        profile["capture_provenance"]["captured_at"],
        profile["capture_provenance"]["max_snapshot_age_s"], now,
    )
    root.mkdir(parents=True, exist_ok=True)
    try:
        _publish(target, profile)
    except FileExistsError:
        existing = _load(target)
        if existing != profile:
            raise ContractError("START_POSE_ID_CONFLICT")
        return existing
    return profile


def list_start_pose_profiles(start_poses: str | Path) -> list[dict[str, Any]]:
    if not isinstance(start_poses, (str, Path)):
        raise ContractError("START_POSE_REGISTRY")
    root = Path(start_poses)
    if root.is_symlink():
        raise ContractError("START_POSE_REGISTRY")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ContractError("START_POSE_REGISTRY")
    paths = list(root.iterdir())
    if any(path.is_symlink() or not path.is_file() or path.suffix != ".json" for path in paths):
        raise ContractError("START_POSE_REGISTRY")
    profiles = [_load(path) for path in paths]
    if any(path.stem != value["start_pose_id"] for path, value in zip(paths, profiles)):
        raise ContractError("START_POSE_REGISTRY")
    if len({value["recovery_home_digest"] for value in profiles}) > 1:
        raise ContractError("START_POSE_RECOVERY_HOME_CONFLICT")
    return sorted(profiles, key=lambda value: value["start_pose_id"])
