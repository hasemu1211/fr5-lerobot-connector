"""Pure setup bindings for the collection desk.

This module discovers no devices and commands no hardware.  Callers provide
snapshots/documents and receive non-authoritative, session-local projections.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.data_factory.campaign_authoring import validate_collection_campaign_manifest
from tools.data_factory.cell_state import CellStateStore
from tools.data_factory.experiment_manifest import validate_fr5_hypothesis
from tools.data_factory.motion.pose_snapshot import JOINTS, _validate_snapshot, calibrate_place
from tools.data_factory.scene_state import SceneStateStore, validate_scene_binding
from tools.data_factory.seed_campaign import (
    BUDGET_DIGEST_FIELDS as SEED_BUDGET_DIGEST_FIELDS,
    validate_seed_episode_intent,
)
from tools.data_factory_recovery import write_json_atomic
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
    "binding_id", "device_kind", "stable_device_id", "capture_endpoint",
    "intended_role",
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
SCENE_OBSERVATION_FIELDS = frozenset({
    "schema_version", "session_id", "run_id", "root_binding_digest",
    "manifest_digest", "slot_digest", "resolver_result_digest",
    "resolved_job_digest", "robot_system_id", "object_instance_id",
    "object_profile_id", "pose", "scene_binding", "scene_binding_digest",
    "scene_evidence", "scene_state_digest", "scene_revision", "observed_by",
    "data_disposition", "production_writers_enabled", "authority",
    "binding_digest",
})
EPISODE_BINDING_FIELDS = frozenset({
    "schema_version", "session_id", "run_id", "intent_digest",
    "manifest_digest", "slot_digest", "resolved_job_digest",
    "root_binding_digest", "start_binding_digest",
    "state_initialization_digest", "scene_observation_digest",
    "scene_state_digest", "place_alias",
    "place_id", "yaw_deg", "x_mm", "y_mm", "robot_start_pose_id",
    "split_group", "repeat_index", "budget_digests", "expires_at",
    "data_disposition", "authority", "binding_digest",
})
PLANNED_START_EVIDENCE_SCHEMA = "data_factory.test_only_planned_start_evidence.v1"
CAMERA_BINDING_RECEIPT = Path("outputs/data_factory/operator_setup/camera_binding.json")
CAMERA_ROLE_BINDINGS_RECEIPT = Path(
    "outputs/data_factory/operator_setup/camera_role_bindings.json"
)
CAMERA_ROLE_BINDINGS_SCHEMA = "data_factory.camera_role_bindings.v1"
CAMERA_ROLE_BINDINGS_FIELDS = frozenset({
    "schema_version", "collection_profile_id", "collection_profile_digest",
    "devices", "assignments", "bindings", "binding_digest",
})
CAMERA_SETUP_ROLES = frozenset({"UP", "SIDE", "WRIST", "UNUSED"})


def _exact(value: object, fields: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(code)
    return value


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ContractError(code)
    return value


def _stable_device_id(device_kind: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
        or (device_kind == "UVC" and not value.startswith("usb-"))
    ):
        raise ContractError("CAMERA_BINDING_DEVICE_ID")
    return value


def normalize_camera_devices(
    values: Sequence[object], *, default_kind: str = "UVC",
) -> list[dict[str, str]]:
    """Validate passive logical camera identities without opening a device."""
    if (
        default_kind not in {"UVC", "REALSENSE"}
        or isinstance(values, (str, bytes))
        or not isinstance(values, Sequence)
    ):
        raise ContractError("CAMERA_BINDING_DISCOVERY")
    devices = []
    for value in values:
        if isinstance(value, str):
            device = {
                "logical_id": value, "label": value, "status": "CONNECTED",
                "kind": default_kind,
                "capture_endpoint": (
                    str(Path("/dev/v4l/by-id") / value)
                    if default_kind == "UVC" else value
                ),
            }
        elif isinstance(value, Mapping):
            device = copy.deepcopy(dict(value))
            if set(device) == {"logical_id", "label", "status"}:
                device.update(
                    kind=default_kind,
                    capture_endpoint=(
                        str(Path("/dev/v4l/by-id") / device["logical_id"])
                        if default_kind == "UVC" else device["logical_id"]
                    ),
                )
        else:
            raise ContractError("CAMERA_BINDING_DISCOVERY")
        if (
            set(device) != {
                "logical_id", "label", "status", "kind", "capture_endpoint",
            }
            or device["kind"] not in {"UVC", "REALSENSE"}
            or device["status"] != "CONNECTED"
            or not isinstance(device["label"], str)
            or not device["label"]
        ):
            raise ContractError("CAMERA_BINDING_DISCOVERY")
        stable_id = _stable_device_id(device["kind"], device["logical_id"])
        endpoint = device["capture_endpoint"]
        expected = (
            str(Path("/dev/v4l/by-id") / stable_id)
            if device["kind"] == "UVC" else stable_id
        )
        if not isinstance(endpoint, str) or endpoint != expected:
            raise ContractError("CAMERA_BINDING_DISCOVERY")
        devices.append(device)
    if len({device["logical_id"] for device in devices}) != len(devices):
        raise ContractError("CAMERA_BINDING_DISCOVERY")
    return sorted(devices, key=lambda item: (item["kind"], item["logical_id"]))


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


def _root_targets(repository: Path, session_id: str, run_id: str) -> dict[str, Path]:
    run_root = repository / "outputs/data_factory/test_only_physical" / session_id / "runs"
    return {
        "run_root": run_root,
        "cell_root": repository / "outputs/data_factory/test_only_physical" / session_id / "cells",
        "dataset_root": repository / "datasets/test_only_physical" / session_id / run_id,
        "run_dir": run_root / run_id,
    }


def _manifest_slot(manifest: Mapping[str, Any], slot: Mapping[str, Any] | None) -> dict[str, Any]:
    slots = manifest["slots"]
    if slot is None:
        if len(slots) != 1:
            raise ContractError("TEST_ONLY_START_SLOT_REQUIRED")
        return copy.deepcopy(slots[0])
    matches = [item for item in slots if item == slot]
    if len(matches) != 1:
        raise ContractError("TEST_ONLY_START_SLOT")
    return copy.deepcopy(matches[0])


def _resolved_job_for_slot(
    *, manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
    slot: Mapping[str, Any], resolved_job: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    bases = [
        item for item in hypothesis["base_conditions"]
        if item["base_condition_digest"] == slot["base_condition_digest"]
    ]
    if len(bases) != 1:
        raise ContractError("TEST_ONLY_EPISODE_JOB")
    receipts = [
        item for item in hypothesis["resolver_receipts"]
        if item["resolver_result_digest"] == bases[0]["resolver_result_digest"]
    ]
    if len(receipts) != 1 or not isinstance(resolved_job, Mapping):
        raise ContractError("TEST_ONLY_EPISODE_JOB")
    receipt = receipts[0]
    if any(
        resolved_job.get(field) != receipt[field]
        for field in (
            "normalized_job", "resolved_job_digest", "resolver_result_digest",
            "input_digests",
        )
    ):
        raise ContractError("TEST_ONLY_EPISODE_JOB")
    return receipt, receipt["normalized_job"]


def _fresh_scene_evidence(
    value: object, *, expected_scene_digest: str,
    max_evidence_age_s: float, clock,
) -> dict[str, Any]:
    evidence = copy.deepcopy(dict(_exact(
        value,
        frozenset({"schema_version", "scene_digest", "observed_at", "evidence_digest"}),
        "TEST_ONLY_SCENE_OBSERVATION_EVIDENCE",
    )))
    if evidence["schema_version"] != "data_factory.scene_freshness_evidence.v1":
        raise ContractError("TEST_ONLY_SCENE_OBSERVATION_EVIDENCE")
    for field in ("scene_digest", "evidence_digest"):
        _digest(evidence[field], "TEST_ONLY_SCENE_OBSERVATION_EVIDENCE")
    if evidence["evidence_digest"] != canonical_digest({
        key: item for key, item in evidence.items() if key != "evidence_digest"
    }):
        raise ContractError("TEST_ONLY_SCENE_OBSERVATION_EVIDENCE")
    if evidence["scene_digest"] != expected_scene_digest:
        raise ContractError("TEST_ONLY_SCENE_OBSERVATION_SCENE")
    observed = _timestamp(evidence["observed_at"], "TEST_ONLY_SCENE_OBSERVATION_EVIDENCE")
    max_age = _finite(max_evidence_age_s, "TEST_ONLY_SCENE_OBSERVATION_AGE")
    try:
        now = datetime.now(timezone.utc) if clock is None else clock()
    except Exception as exc:
        raise ContractError("TEST_ONLY_SCENE_OBSERVATION_CLOCK") from exc
    if (
        max_age <= 0
        or not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise ContractError("TEST_ONLY_SCENE_OBSERVATION_CLOCK")
    now = now.astimezone(timezone.utc)
    if observed > now or now - observed > timedelta(seconds=max_age):
        raise ContractError("TEST_ONLY_SCENE_OBSERVATION_STALE")
    return evidence


def build_test_only_root_binding(
    repository_root: str | Path, *, session_id: str, run_id: str,
    run_root: str | Path | None = None, cell_root: str | Path | None = None,
    dataset_root: str | Path | None = None,
) -> dict[str, Any]:
    """Seal the exact ignored roots for one TEST_ONLY run without creating them."""
    session_id = _identifier(session_id, "TEST_ONLY_SESSION_ID")
    run_id = _identifier(run_id, "TEST_ONLY_RUN_ID")
    repository = Path(repository_root).resolve(strict=True)
    targets = _root_targets(repository, session_id, run_id)
    expected_run = targets["run_root"]
    expected_cell = targets["cell_root"]
    expected_dataset = targets["dataset_root"]
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
    targets = _root_targets(repository, result["session_id"], result["run_id"])
    wanted = {field: targets[field] for field in ("run_root", "cell_root", "dataset_root")}
    for field, target in wanted.items():
        path = Path(result[field])
        if not path.is_absolute() or path.resolve(strict=False) != target:
            raise ContractError("TEST_ONLY_ROOT_MISMATCH")
        _no_symlink_components(repository, target)
        if field != "dataset_root" and target.exists() and not target.is_dir():
            raise ContractError("TEST_ONLY_ROOT_COLLISION")
    _no_symlink_components(repository, targets["run_dir"])
    if targets["dataset_root"].exists() or targets["run_dir"].exists():
        raise ContractError("TEST_ONLY_ROOT_COLLISION")
    if result["binding_digest"] != canonical_digest({key: result[key] for key in result if key != "binding_digest"}):
        raise ContractError("TEST_ONLY_ROOT_DIGEST_MISMATCH")
    return result


def build_production_root_binding(
    repository_root: str | Path, *, session_id: str, run_id: str,
    dataset_root: str | Path,
) -> dict[str, Any]:
    """Seal the standard production run/cell roots and one configured dataset."""
    repository = Path(repository_root).resolve(strict=True)
    value = {
        "session_id": _identifier(session_id, "PRODUCTION_SESSION_ID"),
        "run_id": _identifier(run_id, "PRODUCTION_RUN_ID"),
        "data_disposition": "PRODUCTION",
        "run_root": str(repository / "outputs/data_factory/runs"),
        "cell_root": str(repository / "outputs/data_factory/cells"),
        "dataset_root": str(Path(dataset_root).resolve(strict=False)),
        "production_writers_enabled": True,
    }
    value["binding_digest"] = canonical_digest(value)
    return validate_production_root_binding(value, repository_root=repository)


def validate_production_root_binding(
    value: object, *, repository_root: str | Path,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, ROOT_FIELDS, "PRODUCTION_ROOT_FIELDS")))
    if (
        result["data_disposition"] != "PRODUCTION"
        or result["production_writers_enabled"] is not True
    ):
        raise ContractError("PRODUCTION_ROOT_AUTHORITY")
    _identifier(result["session_id"], "PRODUCTION_SESSION_ID")
    _identifier(result["run_id"], "PRODUCTION_RUN_ID")
    repository = Path(repository_root).resolve(strict=True)
    expected = {
        "run_root": repository / "outputs/data_factory/runs",
        "cell_root": repository / "outputs/data_factory/cells",
    }
    for field, target in expected.items():
        path = Path(result[field])
        if not path.is_absolute() or path.resolve(strict=False) != target:
            raise ContractError("PRODUCTION_ROOT_MISMATCH")
        _no_symlink_components(repository, target)
        if target.exists() and not target.is_dir():
            raise ContractError("PRODUCTION_ROOT_COLLISION")
    dataset = Path(result["dataset_root"])
    dataset_parent = repository / "datasets/fr5_episodes"
    if (
        not dataset.is_absolute()
        or dataset.resolve(strict=False).parent != dataset_parent
        or not SAFE_ID.fullmatch(dataset.name)
    ):
        raise ContractError("PRODUCTION_ROOT_MISMATCH")
    _no_symlink_components(repository, dataset)
    if dataset.exists() and not dataset.is_dir():
        raise ContractError("PRODUCTION_ROOT_COLLISION")
    run_dir = expected["run_root"] / result["run_id"]
    _no_symlink_components(repository, run_dir)
    if run_dir.exists():
        raise ContractError("PRODUCTION_ROOT_COLLISION")
    if result["binding_digest"] != canonical_digest({
        key: result[key] for key in result if key != "binding_digest"
    }):
        raise ContractError("PRODUCTION_ROOT_DIGEST_MISMATCH")
    return result


def build_runtime_root_binding(
    repository_root: str | Path, *, session_id: str, run_id: str,
    data_disposition: str, dataset_root: str | Path | None = None,
) -> dict[str, Any]:
    if data_disposition == "TEST_ONLY":
        if dataset_root is not None:
            raise ContractError("RUNTIME_ROOT_DISPOSITION")
        return build_test_only_root_binding(
            repository_root, session_id=session_id, run_id=run_id,
        )
    if data_disposition == "PRODUCTION" and dataset_root is not None:
        return build_production_root_binding(
            repository_root, session_id=session_id, run_id=run_id,
            dataset_root=dataset_root,
        )
    raise ContractError("RUNTIME_ROOT_DISPOSITION")


def validate_runtime_root_binding(
    value: object, *, repository_root: str | Path,
) -> dict[str, Any]:
    disposition = value.get("data_disposition") if isinstance(value, Mapping) else None
    if disposition == "TEST_ONLY":
        return validate_test_only_root_binding(value, repository_root=repository_root)
    if disposition == "PRODUCTION":
        return validate_production_root_binding(value, repository_root=repository_root)
    raise ContractError("RUNTIME_ROOT_DISPOSITION")


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


def _build_scene_observation_binding(
    *, roots: Mapping[str, Any], repository_root: str | Path,
    manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
    slot: Mapping[str, Any], resolved_job: Mapping[str, Any],
    scene_binding: Mapping[str, Any], scene_evidence: Mapping[str, Any],
    observed_by: str, data_disposition: str,
    max_evidence_age_s: float = 5.0, clock=None,
) -> dict[str, Any]:
    """Seal a fresh scene observation without changing scene state."""
    roots = validate_runtime_root_binding(roots, repository_root=repository_root)
    if roots["data_disposition"] != data_disposition:
        raise ContractError("RUNTIME_SCENE_OBSERVATION_DISPOSITION")
    hypothesis = validate_fr5_hypothesis(hypothesis)
    manifest = validate_collection_campaign_manifest(manifest, hypothesis=hypothesis)
    slot = _manifest_slot(manifest, slot)
    receipt, job = _resolved_job_for_slot(
        manifest=manifest, hypothesis=hypothesis, slot=slot,
        resolved_job=resolved_job,
    )
    binding = validate_scene_binding(copy.deepcopy(scene_binding))
    evidence = _fresh_scene_evidence(
        scene_evidence, expected_scene_digest=binding["scene_state_digest"],
        max_evidence_age_s=max_evidence_age_s, clock=clock,
    )
    prefix = data_disposition.lower()
    value = {
        "schema_version": f"data_factory.{prefix}_scene_observation.v1",
        "session_id": roots["session_id"],
        "run_id": roots["run_id"],
        "root_binding_digest": roots["binding_digest"],
        "manifest_digest": manifest["manifest_digest"],
        "slot_digest": canonical_digest(slot),
        "resolver_result_digest": receipt["resolver_result_digest"],
        "resolved_job_digest": receipt["resolved_job_digest"],
        "robot_system_id": job["robot_system_id"],
        "object_instance_id": binding["object_instance_id"],
        "object_profile_id": job["object_profile_id"],
        "pose": {key: job[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")},
        "scene_binding": binding,
        "scene_binding_digest": canonical_digest(binding),
        "scene_evidence": evidence,
        "scene_state_digest": binding["scene_state_digest"],
        "scene_revision": binding["revision"],
        "observed_by": _identifier(
            observed_by, f"{data_disposition}_SCENE_OBSERVATION_OBSERVER",
        ),
        "data_disposition": data_disposition,
        "production_writers_enabled": data_disposition == "PRODUCTION",
        "authority": copy.deepcopy(NO_AUTHORITY),
    }
    value["binding_digest"] = canonical_digest(value)
    return _validate_scene_observation_binding(
        value, roots=roots, manifest=manifest, hypothesis=hypothesis, slot=slot,
        normalized_job=resolved_job, max_evidence_age_s=max_evidence_age_s,
        clock=clock, data_disposition=data_disposition,
    )


def _validate_scene_observation_binding(
    value: object, *, roots: Mapping[str, Any], manifest: Mapping[str, Any],
    hypothesis: Mapping[str, Any], slot: Mapping[str, Any],
    normalized_job: Mapping[str, Any], max_evidence_age_s: float = 5.0,
    clock=None, data_disposition: str,
) -> dict[str, Any]:
    if data_disposition not in {"TEST_ONLY", "PRODUCTION"}:
        raise ContractError("RUNTIME_SCENE_OBSERVATION_DISPOSITION")
    code = f"{data_disposition}_SCENE_OBSERVATION_BINDING"
    result = copy.deepcopy(dict(_exact(
        value, SCENE_OBSERVATION_FIELDS, f"{data_disposition}_SCENE_OBSERVATION_FIELDS",
    )))
    hypothesis = validate_fr5_hypothesis(hypothesis)
    manifest = validate_collection_campaign_manifest(manifest, hypothesis=hypothesis)
    slot = _manifest_slot(manifest, slot)
    receipt, job = _resolved_job_for_slot(
        manifest=manifest, hypothesis=hypothesis, slot=slot,
        resolved_job=normalized_job,
    )
    binding = validate_scene_binding(result["scene_binding"])
    evidence = _fresh_scene_evidence(
        result["scene_evidence"], expected_scene_digest=binding["scene_state_digest"],
        max_evidence_age_s=max_evidence_age_s, clock=clock,
    )
    pose = {key: job[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")}
    if (
        result["schema_version"] != f"data_factory.{data_disposition.lower()}_scene_observation.v1"
        or result["session_id"] != roots.get("session_id")
        or result["run_id"] != roots.get("run_id")
        or result["root_binding_digest"] != roots.get("binding_digest")
        or result["manifest_digest"] != manifest["manifest_digest"]
        or result["slot_digest"] != canonical_digest(slot)
        or result["resolver_result_digest"] != receipt["resolver_result_digest"]
        or result["resolved_job_digest"] != receipt["resolved_job_digest"]
        or result["robot_system_id"] != job["robot_system_id"]
        or result["object_instance_id"] != binding["object_instance_id"]
        or result["object_profile_id"] != job["object_profile_id"]
        or result["pose"] != pose
        or result["scene_binding_digest"] != canonical_digest(binding)
        or result["scene_evidence"] != evidence
        or result["scene_state_digest"] != binding["scene_state_digest"]
        or result["scene_revision"] != binding["revision"]
        or roots.get("data_disposition") != data_disposition
        or result["data_disposition"] != data_disposition
        or result["production_writers_enabled"] is not (data_disposition == "PRODUCTION")
        or result["authority"] != NO_AUTHORITY
    ):
        raise ContractError(code)
    for field in (
        "session_id", "run_id", "robot_system_id", "object_instance_id",
        "object_profile_id", "observed_by",
    ):
        _identifier(result[field], code)
    for field in (
        "root_binding_digest", "manifest_digest", "slot_digest",
        "resolver_result_digest", "resolved_job_digest", "scene_binding_digest",
        "scene_state_digest",
    ):
        _digest(result[field], code)
    if type(result["scene_revision"]) is not int or result["scene_revision"] < 1:
        raise ContractError(code)
    if result["binding_digest"] != canonical_digest({
        key: item for key, item in result.items() if key != "binding_digest"
    }):
        raise ContractError(f"{data_disposition}_SCENE_OBSERVATION_DIGEST_MISMATCH")
    return result


def build_test_only_scene_observation_binding(**kwargs) -> dict[str, Any]:
    return _build_scene_observation_binding(
        **kwargs, data_disposition="TEST_ONLY",
    )


def build_production_scene_observation_binding(**kwargs) -> dict[str, Any]:
    return _build_scene_observation_binding(
        **kwargs, data_disposition="PRODUCTION",
    )


def build_runtime_scene_observation_binding(**kwargs) -> dict[str, Any]:
    roots = kwargs.get("roots")
    disposition = roots.get("data_disposition") if isinstance(roots, Mapping) else None
    if disposition not in {"TEST_ONLY", "PRODUCTION"}:
        raise ContractError("RUNTIME_SCENE_OBSERVATION_DISPOSITION")
    return _build_scene_observation_binding(
        **kwargs, data_disposition=disposition,
    )


def validate_test_only_scene_observation_binding(value: object, **kwargs) -> dict[str, Any]:
    return _validate_scene_observation_binding(
        value, **kwargs, data_disposition="TEST_ONLY",
    )


def validate_production_scene_observation_binding(value: object, **kwargs) -> dict[str, Any]:
    return _validate_scene_observation_binding(
        value, **kwargs, data_disposition="PRODUCTION",
    )


def validate_runtime_scene_observation_binding(value: object, **kwargs) -> dict[str, Any]:
    disposition = value.get("data_disposition") if isinstance(value, Mapping) else None
    if disposition not in {"TEST_ONLY", "PRODUCTION"}:
        raise ContractError("RUNTIME_SCENE_OBSERVATION_DISPOSITION")
    return _validate_scene_observation_binding(
        value, **kwargs, data_disposition=disposition,
    )


def _build_episode_binding(
    *, roots: Mapping[str, Any], repository_root: str | Path,
    manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
    intent: Mapping[str, Any], start_binding: Mapping[str, Any],
    resolved_job: Mapping[str, Any], place_alias: str,
    state_initialization: Mapping[str, Any] | None = None,
    scene_observation: Mapping[str, Any] | None = None,
    data_disposition: str, max_scene_evidence_age_s: float = 5.0, clock=None,
) -> dict[str, Any]:
    """Join one exact campaign intent to its disposition-bound run context."""
    roots = validate_runtime_root_binding(roots, repository_root=repository_root)
    if roots["data_disposition"] != data_disposition:
        raise ContractError("RUNTIME_EPISODE_DISPOSITION")
    manifest = validate_collection_campaign_manifest(manifest, hypothesis=hypothesis)
    intent = validate_seed_episode_intent(intent, manifest=manifest, hypothesis=hypothesis)
    start = validate_runtime_start_binding(
        start_binding, manifest=manifest, hypothesis=hypothesis, slot=intent["slot"],
    )
    if start["data_disposition"] != data_disposition:
        raise ContractError("RUNTIME_EPISODE_DISPOSITION")
    source_code = f"{data_disposition}_EPISODE_SCENE_SOURCE"
    if data_disposition == "TEST_ONLY" and intent["order_index"] == 0:
        if state_initialization is None or scene_observation is not None:
            raise ContractError(source_code)
        source = validate_test_only_state_initialization(state_initialization, roots=roots)
        initialization_digest = source["initialization_digest"]
        observation_digest = None
    else:
        if state_initialization is not None or scene_observation is None:
            raise ContractError(source_code)
        source = validate_runtime_scene_observation_binding(
            scene_observation, roots=roots, manifest=manifest,
            hypothesis=hypothesis, slot=intent["slot"], normalized_job=resolved_job,
            max_evidence_age_s=max_scene_evidence_age_s, clock=clock,
        )
        if source["data_disposition"] != data_disposition:
            raise ContractError("RUNTIME_EPISODE_DISPOSITION")
        initialization_digest = None
        observation_digest = source["binding_digest"]
    place_alias = _identifier(place_alias, f"{data_disposition}_EPISODE_ALIAS")
    receipt, job = _resolved_job_for_slot(
        manifest=manifest, hypothesis=hypothesis, slot=intent["slot"],
        resolved_job=resolved_job,
    )
    if (
        intent["base_condition"]["resolver_result_digest"] != receipt["resolver_result_digest"]
        or intent["run_id"] != roots["run_id"]
        or intent["manifest_digest"] != manifest["manifest_digest"]
        or start["slot_digest"] != intent["slot_digest"]
        or start["robot_start_pose_id"] != intent["robot_start_pose"]["robot_start_pose_id"]
        or source["scene_state_digest"] != intent["required_scene_digest"]
        or source["robot_system_id"] != job["robot_system_id"]
        or source["object_profile_id"] != job["object_profile_id"]
        or source["pose"] != {
            key: job[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
        }
    ):
        raise ContractError(f"{data_disposition}_EPISODE_BINDING")
    slot = intent["slot"]
    value = {
        "schema_version": f"data_factory.{data_disposition.lower()}_episode_binding.v1",
        "session_id": roots["session_id"],
        "run_id": roots["run_id"],
        "intent_digest": intent["intent_digest"],
        "manifest_digest": intent["manifest_digest"],
        "slot_digest": intent["slot_digest"],
        "resolved_job_digest": receipt["resolved_job_digest"],
        "root_binding_digest": roots["binding_digest"],
        "start_binding_digest": start["binding_digest"],
        "state_initialization_digest": initialization_digest,
        "scene_observation_digest": observation_digest,
        "scene_state_digest": source["scene_state_digest"],
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
        "data_disposition": data_disposition,
        "authority": copy.deepcopy(NO_AUTHORITY),
    }
    value["binding_digest"] = canonical_digest(value)
    return _validate_episode_binding(
        value, roots=roots, normalized_job=resolved_job,
        data_disposition=data_disposition,
    )


def _validate_episode_binding(
    value: object, *, roots: Mapping[str, Any], normalized_job: Mapping[str, Any],
    data_disposition: str,
) -> dict[str, Any]:
    """Validate the compact context consumed immediately before plan approval."""
    if data_disposition not in {"TEST_ONLY", "PRODUCTION"}:
        raise ContractError("RUNTIME_EPISODE_DISPOSITION")
    code = f"{data_disposition}_EPISODE_BINDING"
    result = copy.deepcopy(dict(_exact(
        value, EPISODE_BINDING_FIELDS, f"{data_disposition}_EPISODE_FIELDS",
    )))
    if (
        result["schema_version"] != f"data_factory.{data_disposition.lower()}_episode_binding.v1"
        or result["session_id"] != roots.get("session_id")
        or result["run_id"] != roots.get("run_id")
        or result["root_binding_digest"] != roots.get("binding_digest")
        or roots.get("data_disposition") != data_disposition
        or result["data_disposition"] != data_disposition
        or result["authority"] != NO_AUTHORITY
        or result["resolved_job_digest"] != normalized_job.get("resolved_job_digest")
    ):
        raise ContractError(code)
    job = normalized_job.get("normalized_job")
    if not isinstance(job, Mapping) or any(
        result[field] != job[field] for field in ("place_id", "yaw_deg", "x_mm", "y_mm")
    ):
        raise ContractError(f"{data_disposition}_EPISODE_JOB")
    for field in ("session_id", "run_id", "place_alias", "place_id", "robot_start_pose_id"):
        _identifier(result[field], code)
    for field in (
        "intent_digest", "manifest_digest", "slot_digest", "resolved_job_digest",
        "root_binding_digest", "start_binding_digest", "scene_state_digest",
    ):
        _digest(result[field], code)
    sources = (
        result["state_initialization_digest"], result["scene_observation_digest"],
    )
    if (
        data_disposition == "TEST_ONLY"
        and sum(item is not None for item in sources) != 1
    ) or (
        data_disposition == "PRODUCTION"
        and (sources[0] is not None or sources[1] is None)
    ):
        raise ContractError(f"{data_disposition}_EPISODE_SCENE_SOURCE")
    _digest(next(item for item in sources if item is not None), code)
    for field in ("yaw_deg", "x_mm", "y_mm"):
        _finite(result[field], code)
    if (
        result["split_group"] not in {"TRAIN", "ID", "OOD"}
        or type(result["repeat_index"]) is not int
        or result["repeat_index"] < 0
        or not isinstance(result["budget_digests"], Mapping)
        or set(result["budget_digests"]) != SEED_BUDGET_DIGEST_FIELDS
    ):
        raise ContractError(code)
    for digest in result["budget_digests"].values():
        _digest(digest, code)
    _timestamp(result["expires_at"], f"{data_disposition}_EPISODE_EXPIRY")
    if result["binding_digest"] != canonical_digest({
        key: result[key] for key in result if key != "binding_digest"
    }):
        raise ContractError(f"{data_disposition}_EPISODE_DIGEST_MISMATCH")
    return result


def build_test_only_episode_binding(**kwargs) -> dict[str, Any]:
    return _build_episode_binding(**kwargs, data_disposition="TEST_ONLY")


def build_production_episode_binding(**kwargs) -> dict[str, Any]:
    return _build_episode_binding(**kwargs, data_disposition="PRODUCTION")


def validate_test_only_episode_binding(value: object, **kwargs) -> dict[str, Any]:
    return _validate_episode_binding(value, **kwargs, data_disposition="TEST_ONLY")


def validate_production_episode_binding(value: object, **kwargs) -> dict[str, Any]:
    return _validate_episode_binding(value, **kwargs, data_disposition="PRODUCTION")


def validate_runtime_episode_binding(value: object, **kwargs) -> dict[str, Any]:
    disposition = value.get("data_disposition") if isinstance(value, Mapping) else None
    if disposition not in {"TEST_ONLY", "PRODUCTION"}:
        raise ContractError("RUNTIME_EPISODE_DISPOSITION")
    return _validate_episode_binding(
        value, **kwargs, data_disposition=disposition,
    )


def build_runtime_episode_binding(
    *, roots: Mapping[str, Any], repository_root: str | Path,
    manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
    intent: Mapping[str, Any], start_binding: Mapping[str, Any],
    resolved_job: Mapping[str, Any], place_alias: str,
    state_initialization: Mapping[str, Any] | None = None,
    scene_binding: Mapping[str, Any] | None = None,
    scene_evidence: Mapping[str, Any] | None = None,
    observed_by: str | None = None,
    max_scene_evidence_age_s: float = 5.0, clock=None,
) -> dict[str, Any]:
    """Build one fresh episode edge under the root binding disposition."""
    disposition = roots.get("data_disposition") if isinstance(roots, Mapping) else None
    if disposition not in {"TEST_ONLY", "PRODUCTION"}:
        raise ContractError("RUNTIME_EPISODE_DISPOSITION")
    order_index = intent.get("order_index") if isinstance(intent, Mapping) else None
    if disposition == "TEST_ONLY" and order_index == 0:
        if (
            state_initialization is None
            or scene_binding is not None
            or scene_evidence is not None
            or observed_by is not None
        ):
            raise ContractError("TEST_ONLY_EPISODE_SCENE_SOURCE")
        source = {"state_initialization": state_initialization}
    elif type(order_index) is int and order_index >= 0:
        if (
            state_initialization is not None
            or scene_binding is None
            or scene_evidence is None
            or observed_by is None
        ):
            raise ContractError(f"{disposition}_EPISODE_SCENE_SOURCE")
        source = {
            "scene_observation": build_runtime_scene_observation_binding(
                roots=roots, repository_root=repository_root,
                manifest=manifest, hypothesis=hypothesis, slot=intent["slot"],
                resolved_job=resolved_job, scene_binding=scene_binding,
                scene_evidence=scene_evidence, observed_by=observed_by,
                max_evidence_age_s=max_scene_evidence_age_s, clock=clock,
            ),
        }
    else:
        raise ContractError(f"{disposition}_EPISODE_SCENE_SOURCE")
    return _build_episode_binding(
        roots=roots, repository_root=repository_root,
        manifest=manifest, hypothesis=hypothesis, intent=intent,
        start_binding=start_binding, resolved_job=resolved_job,
        place_alias=place_alias,
        max_scene_evidence_age_s=max_scene_evidence_age_s, clock=clock,
        data_disposition=disposition,
        **source,
    )


def build_test_only_runtime_episode_binding(**kwargs) -> dict[str, Any]:
    roots = kwargs.get("roots")
    if not isinstance(roots, Mapping) or roots.get("data_disposition") != "TEST_ONLY":
        raise ContractError("TEST_ONLY_EPISODE_SCENE_SOURCE")
    return build_runtime_episode_binding(**kwargs)


def build_production_runtime_episode_binding(**kwargs) -> dict[str, Any]:
    roots = kwargs.get("roots")
    if not isinstance(roots, Mapping) or roots.get("data_disposition") != "PRODUCTION":
        raise ContractError("PRODUCTION_EPISODE_SCENE_SOURCE")
    return build_runtime_episode_binding(**kwargs)


def build_test_only_start_binding(
    *, manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
    motion_qualification: Mapping[str, Any], home_candidate: Mapping[str, Any],
    current_snapshot: Mapping[str, Any], slot: Mapping[str, Any] | None = None,
    max_snapshot_age_s: float = 0.1,
) -> dict[str, Any]:
    """Bind one fresh qualified-start snapshot to one exact TEST_ONLY slot."""
    hypothesis = validate_fr5_hypothesis(hypothesis)
    manifest = validate_collection_campaign_manifest(manifest, hypothesis=hypothesis)
    slot = _manifest_slot(manifest, slot)
    if not isinstance(motion_qualification, Mapping) or motion_qualification.get("schema_version") != "data_factory.motion_qualification.v1" or motion_qualification.get("qualification_status") != "QUALIFIED":
        raise ContractError("TEST_ONLY_START_MOTION_QUALIFICATION")
    if not isinstance(home_candidate, Mapping) or home_candidate.get("schema_version") != "data_factory.home_candidate.v1":
        raise ContractError("TEST_ONLY_START_HOME_CANDIDATE")
    home_digest = canonical_digest(home_candidate)
    if motion_qualification.get("home_candidate_digest") != home_digest:
        raise ContractError("TEST_ONLY_START_HOME_DIGEST")
    fixed = hypothesis["fixed_contract"]
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
    safe_target = motion_qualification.get("qualified_safe_joint_positions_rad")
    motion_tolerance = motion_qualification.get("goal_tolerances", {}).get("joint_rad")
    if (
        not isinstance(safe_target, list) or len(safe_target) != len(JOINTS)
        or any(not math.isfinite(_finite(item, "TEST_ONLY_START_TARGET")) for item in safe_target)
        or _finite(motion_tolerance, "TEST_ONLY_START_TOLERANCE") <= 0
        or float(motion_tolerance) > 0.01
    ):
        raise ContractError("TEST_ONLY_START_TARGET")
    target = [_finite(pose["target_rad"][joint], "TEST_ONLY_START_QUALIFICATION") for joint in JOINTS]
    pose_tolerances = [
        _finite(pose["tolerance_rad"][joint], "TEST_ONLY_START_QUALIFICATION")
        for joint in JOINTS
    ]
    if any(item <= 0 for item in pose_tolerances):
        raise ContractError("TEST_ONLY_START_QUALIFICATION")
    tolerance = min(float(motion_tolerance), *pose_tolerances)
    current = [snapshot["joint_positions_rad"][joint] for joint in JOINTS]
    if any(abs(actual - expected) > tolerance for actual, expected in zip(current, target)):
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
        "target_rad": target,
        "current_rad": current,
        "tolerance_rad": tolerance,
        "max_snapshot_age_s": min(max_age, qualified_age),
        "snapshot_digest": canonical_digest(snapshot),
        "status": "BOUND_TEST_ONLY",
        "authority": copy.deepcopy(NO_AUTHORITY),
    }
    value["binding_digest"] = canonical_digest(value)
    return validate_test_only_start_binding(
        value, manifest=manifest, hypothesis=hypothesis, slot=slot,
    )


def validate_test_only_start_binding(
    value: object, *, manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
    slot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    hypothesis = validate_fr5_hypothesis(hypothesis)
    manifest = validate_collection_campaign_manifest(manifest, hypothesis=hypothesis)
    slot = _manifest_slot(manifest, slot)
    result = _validate_test_only_start_shape(value)
    poses = [
        item for item in hypothesis["robot_start_poses"]
        if item["robot_start_pose_id"] == result.get("robot_start_pose_id")
    ]
    pose = poses[0] if len(poses) == 1 else None
    if (
        result["manifest_digest"] != manifest.get("manifest_digest")
        or result["slot_digest"] != canonical_digest(slot)
        or result["robot_start_pose_id"] != slot["robot_start_pose_id"]
        or pose is None
        or result["robot_start_pose_qualification_digest"] != pose["qualification_digest"]
        or any(
            abs(actual - pose["target_rad"][joint]) > 1e-9
            for actual, joint in zip(result["target_rad"], JOINTS)
        )
        or any(
            result["tolerance_rad"] > pose["tolerance_rad"][joint]
            for joint in JOINTS
        )
    ):
        raise ContractError("TEST_ONLY_START_BINDING")
    return result


def build_production_start_binding(
    *, manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
    motion_qualification: Mapping[str, Any], home_candidate: Mapping[str, Any],
    current_snapshot: Mapping[str, Any], slot: Mapping[str, Any] | None = None,
    max_snapshot_age_s: float = 0.1,
) -> dict[str, Any]:
    """Bind a fresh exact qualified start without creating execution authority."""
    result = build_test_only_start_binding(
        manifest=manifest, hypothesis=hypothesis,
        motion_qualification=motion_qualification,
        home_candidate=home_candidate, current_snapshot=current_snapshot,
        slot=slot, max_snapshot_age_s=max_snapshot_age_s,
    )
    result.update(data_disposition="PRODUCTION", status="BOUND_PRODUCTION")
    result["binding_digest"] = canonical_digest({
        key: value for key, value in result.items() if key != "binding_digest"
    })
    return validate_production_start_binding(
        result, manifest=manifest, hypothesis=hypothesis, slot=slot,
    )


def validate_production_start_binding(
    value: object, *, manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
    slot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    hypothesis = validate_fr5_hypothesis(hypothesis)
    manifest = validate_collection_campaign_manifest(manifest, hypothesis=hypothesis)
    slot = _manifest_slot(manifest, slot)
    result = _validate_start_shape(value, "PRODUCTION")
    poses = [
        item for item in hypothesis["robot_start_poses"]
        if item["robot_start_pose_id"] == result.get("robot_start_pose_id")
    ]
    pose = poses[0] if len(poses) == 1 else None
    source_poses = [
        item for item in hypothesis["qualification_catalog"][
            "robot_start_pose_qualifications"
        ]
        if item["qualification_digest"]
        == result.get("robot_start_pose_qualification_digest")
    ]
    if (
        result["manifest_digest"] != manifest.get("manifest_digest")
        or result["slot_digest"] != canonical_digest(slot)
        or result["robot_start_pose_id"] != slot["robot_start_pose_id"]
        or pose is None
        or len(source_poses) != 1
        or source_poses[0].get("source") != "QUALIFICATION_ARTIFACT"
        or result["robot_start_pose_qualification_digest"] != pose["qualification_digest"]
        or any(
            abs(actual - pose["target_rad"][joint]) > 1e-9
            for actual, joint in zip(result["target_rad"], JOINTS)
        )
        or any(
            result["tolerance_rad"] > pose["tolerance_rad"][joint]
            for joint in JOINTS
        )
    ):
        raise ContractError("PRODUCTION_START_BINDING")
    return result


def build_runtime_start_binding(
    *, data_disposition: str, manifest: Mapping[str, Any],
    hypothesis: Mapping[str, Any], motion_qualification: Mapping[str, Any],
    home_candidate: Mapping[str, Any], current_snapshot: Mapping[str, Any],
    slot: Mapping[str, Any] | None = None, max_snapshot_age_s: float = 0.1,
) -> dict[str, Any]:
    builder = {
        "TEST_ONLY": build_test_only_start_binding,
        "PRODUCTION": build_production_start_binding,
    }.get(data_disposition)
    if builder is None:
        raise ContractError("RUNTIME_START_DISPOSITION")
    return builder(
        manifest=manifest, hypothesis=hypothesis,
        motion_qualification=motion_qualification,
        home_candidate=home_candidate, current_snapshot=current_snapshot,
        slot=slot, max_snapshot_age_s=max_snapshot_age_s,
    )


def validate_runtime_start_binding(
    value: object, *, manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
    slot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    disposition = value.get("data_disposition") if isinstance(value, Mapping) else None
    if disposition == "TEST_ONLY":
        return validate_test_only_start_binding(
            value, manifest=manifest, hypothesis=hypothesis, slot=slot,
        )
    if disposition == "PRODUCTION":
        return validate_production_start_binding(
            value, manifest=manifest, hypothesis=hypothesis, slot=slot,
        )
    raise ContractError("RUNTIME_START_DISPOSITION")


def _validate_start_shape(value: object, data_disposition: str) -> dict[str, Any]:
    """Validate a sealed exact target without claiming its old snapshot is current."""
    if data_disposition not in {"TEST_ONLY", "PRODUCTION"}:
        raise ContractError("RUNTIME_START_DISPOSITION")
    code = f"{data_disposition}_START_BINDING"
    result = copy.deepcopy(dict(_exact(value, START_FIELDS, f"{data_disposition}_START_FIELDS")))
    if (
        result["scope"] != "MOTION_Q_SAFE_START"
        or result["data_disposition"] != data_disposition
        or result["status"] != f"BOUND_{data_disposition}"
        or result["authority"] != NO_AUTHORITY
    ):
        raise ContractError(code)
    for field in ("robot_start_pose_id", "motion_qualification_id"):
        _identifier(result[field], code)
    for field in (
        "manifest_digest", "slot_digest", "robot_start_pose_qualification_digest",
        "motion_qualification_digest", "home_candidate_digest", "snapshot_digest",
    ):
        _digest(result[field], code)
    tolerance = _finite(result["tolerance_rad"], code)
    max_age = _finite(result["max_snapshot_age_s"], code)
    if (
        result["joint_order"] != list(JOINTS)
        or not isinstance(result["target_rad"], list)
        or not isinstance(result["current_rad"], list)
        or len(result["target_rad"]) != len(JOINTS)
        or len(result["current_rad"]) != len(JOINTS)
        or not 0 < tolerance <= 0.01
        or not 0 < max_age <= 0.1
    ):
        raise ContractError(code)
    target = [_finite(item, code) for item in result["target_rad"]]
    current = [_finite(item, code) for item in result["current_rad"]]
    if any(abs(actual - expected) > tolerance for actual, expected in zip(current, target)):
        raise ContractError(code)
    if result["binding_digest"] != canonical_digest({key: result[key] for key in result if key != "binding_digest"}):
        raise ContractError(f"{data_disposition}_START_DIGEST_MISMATCH")
    return result


def _validate_test_only_start_shape(value: object) -> dict[str, Any]:
    return _validate_start_shape(value, "TEST_ONLY")


def validate_runtime_planned_start(
    *, start_binding: Mapping[str, Any], episode_binding: Mapping[str, Any],
    motion_program: Mapping[str, Any], plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove the executor's fresh plan snapshot still matches its qualified target."""
    disposition = (
        start_binding.get("data_disposition")
        if isinstance(start_binding, Mapping) else None
    )
    if disposition not in {"TEST_ONLY", "PRODUCTION"}:
        raise ContractError("RUNTIME_PLANNED_START_DISPOSITION")
    code = f"{disposition}_PLANNED_START"
    start = _validate_start_shape(start_binding, disposition)
    if (
        not isinstance(episode_binding, Mapping)
        or episode_binding.get("data_disposition") != disposition
        or episode_binding.get("start_binding_digest") != start["binding_digest"]
        or not isinstance(motion_program, Mapping)
        or not isinstance(motion_program.get("binding_digests"), Mapping)
        or not isinstance(motion_program.get("planning"), Mapping)
        or not isinstance(plan, Mapping)
        or plan.get("binding_digests") != motion_program["binding_digests"]
        or not isinstance(plan.get("initial_joint_state"), list)
        or len(plan["initial_joint_state"]) != len(JOINTS)
    ):
        raise ContractError(code)
    bindings = motion_program["binding_digests"]
    max_age = _finite(
        motion_program["planning"].get("max_joint_state_age_s"),
        code,
    )
    if (
        bindings.get("motion_qualification") != start["motion_qualification_digest"]
        or bindings.get("home_candidate") != start["home_candidate_digest"]
        or not 0 < max_age <= start["max_snapshot_age_s"]
    ):
        raise ContractError(code)
    initial = [_finite(item, code) for item in plan["initial_joint_state"]]
    maximum = max(abs(actual - target) for actual, target in zip(initial, start["target_rad"]))
    if maximum > start["tolerance_rad"]:
        raise ContractError(f"{disposition}_PLANNED_START_MISMATCH")
    evidence = {
        "schema_version": (
            PLANNED_START_EVIDENCE_SCHEMA
            if disposition == "TEST_ONLY"
            else "data_factory.production_planned_start_evidence.v1"
        ),
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


def validate_test_only_planned_start(**kwargs) -> dict[str, Any]:
    start = kwargs.get("start_binding")
    if not isinstance(start, Mapping) or start.get("data_disposition") != "TEST_ONLY":
        raise ContractError("TEST_ONLY_PLANNED_START")
    episode = kwargs.get("episode_binding")
    if isinstance(episode, Mapping) and "data_disposition" not in episode:
        kwargs = {**kwargs, "episode_binding": {**episode, "data_disposition": "TEST_ONLY"}}
    return validate_runtime_planned_start(**kwargs)


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


def select_yaw0_print_profile(
    repository_root: str | Path, *, place_id: str, source_scale_bar_mm: float,
) -> Path:
    """Resolve one checked-in yaw-zero sheet for the measured print source."""
    _identifier(place_id, "WORKSPACE_PRINT_PROFILE")
    source = _finite(source_scale_bar_mm, "WORKSPACE_PRINT_PROFILE")
    if source <= 0:
        raise ContractError("WORKSPACE_PRINT_PROFILE")
    root = Path(repository_root)
    matches: dict[str, Path] = {}
    for path in sorted((root / "config/data_factory").rglob("*.json")):
        try:
            sheet = validate_yaw0_sheet(load_json_strict(path))
        except (ContractError, OSError):
            continue
        if (
            sheet["place_id"] == place_id
            and abs(float(sheet["print_calibration"]["measured_scale_bar_mm"]) - source) <= 1e-9
        ):
            matches.setdefault(canonical_digest(sheet), path)
    if len(matches) != 1:
        raise ContractError(
            "WORKSPACE_PRINT_PROFILE_UNAVAILABLE" if not matches
            else "WORKSPACE_PRINT_PROFILE_AMBIGUOUS"
        )
    return next(iter(matches.values()))


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
    # The qualified plane supplies robot/TCP/table provenance.  A compensated
    # print profile has its own family digest, so bind it by place and the exact
    # measured print-source value instead of pretending it is the old sheet.
    if (
        plane["place_id"] != place_id
        or plane["robot_system_id"] != robot_system_id
        or sheet["place_id"] != place_id
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
    capture_endpoint: str | None = None,
) -> dict[str, Any]:
    if device_kind not in {"UVC", "REALSENSE"}:
        raise ContractError("CAMERA_BINDING_DEVICE_KIND")
    _identifier(binding_id, "CAMERA_BINDING_ID")
    _stable_device_id(device_kind, stable_device_id)
    expected_endpoint = (
        str(Path("/dev/v4l/by-id") / stable_device_id)
        if device_kind == "UVC" else stable_device_id
    )
    if capture_endpoint is None:
        capture_endpoint = expected_endpoint
    if capture_endpoint != expected_endpoint:
        raise ContractError("CAMERA_BINDING_DEVICE_ID")
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
        "capture_endpoint": capture_endpoint,
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
    for field in ("binding_id", "intended_role", "collection_profile_id"):
        _identifier(result[field], "CAMERA_BINDING_ID")
    _stable_device_id(result["device_kind"], result["stable_device_id"])
    expected_endpoint = (
        str(Path("/dev/v4l/by-id") / result["stable_device_id"])
        if result["device_kind"] == "UVC" else result["stable_device_id"]
    )
    if result["capture_endpoint"] != expected_endpoint:
        raise ContractError("CAMERA_BINDING_DEVICE_ID")
    _digest(result["collection_profile_digest"], "CAMERA_BINDING_DIGEST")
    if result["binding_digest"] != canonical_digest({key: result[key] for key in result if key != "binding_digest"}):
        raise ContractError("CAMERA_BINDING_DIGEST_MISMATCH")
    return result


def build_camera_binding_from_discovery(
    *, binding_id: str, device_kind: str, discovered_device_ids: Sequence[object],
    intended_role: str, collection_profile: Mapping[str, Any],
    selected_device_id: str | None = None,
) -> dict[str, Any]:
    """Bind one passive-discovery token; multiple devices require an explicit choice."""
    if device_kind not in {"UVC", "REALSENSE"}:
        raise ContractError("CAMERA_BINDING_DEVICE_KIND")
    if (
        isinstance(discovered_device_ids, (str, bytes))
        or not isinstance(discovered_device_ids, Sequence)
    ):
        raise ContractError("CAMERA_BINDING_DISCOVERY")
    devices = [
        device
        for value in discovered_device_ids
        for device in normalize_camera_devices([value], default_kind=device_kind)
    ]
    discovered = [
        item["logical_id"] for item in devices if item["kind"] == device_kind
    ]
    if selected_device_id is None:
        if not discovered:
            raise ContractError("CAMERA_BINDING_DISCOVERY_ZERO")
        if len(discovered) != 1:
            raise ContractError("CAMERA_BINDING_DISCOVERY_AMBIGUOUS")
        selected_device_id = discovered[0]
    else:
        selected_device_id = _stable_device_id(device_kind, selected_device_id)
        if discovered.count(selected_device_id) == 0:
            raise ContractError("CAMERA_BINDING_DISCOVERY_ZERO")
        if discovered.count(selected_device_id) != 1:
            raise ContractError("CAMERA_BINDING_DISCOVERY_AMBIGUOUS")
    return build_camera_binding_candidate(
        binding_id=binding_id,
        device_kind=device_kind,
        stable_device_id=selected_device_id,
        intended_role=intended_role,
        collection_profile=collection_profile,
        connected=True,
        capture_endpoint=next(
            item["capture_endpoint"] for item in devices
            if item["kind"] == device_kind
            and item["logical_id"] == selected_device_id
        ),
    )


def reuse_camera_binding_receipt(
    value: object, *, discovered_device_ids: Sequence[str],
    collection_profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Reuse a stored stable token only when passive discovery finds it exactly once."""
    binding = validate_camera_binding_candidate(value)
    if (
        not isinstance(collection_profile, Mapping)
        or binding["collection_profile_id"] != collection_profile.get("collection_profile_id")
        or binding["collection_profile_digest"] != canonical_digest(collection_profile)
    ):
        raise ContractError("CAMERA_BINDING_PROFILE")
    return build_camera_binding_from_discovery(
        binding_id=binding["binding_id"],
        device_kind=binding["device_kind"],
        discovered_device_ids=discovered_device_ids,
        selected_device_id=binding["stable_device_id"],
        intended_role=binding["intended_role"],
        collection_profile=collection_profile,
    )


def build_camera_role_bindings(
    *, collection_profile: Mapping[str, Any],
    discovered_device_ids: Sequence[object], assignments: Mapping[str, str],
) -> dict[str, Any]:
    """Bind an exact connected-device role map to one matching v2 profile."""
    if (
        not isinstance(collection_profile, Mapping)
        or collection_profile.get("schema_version")
        != "data_factory.collection_profile.v2"
        or isinstance(discovered_device_ids, (str, bytes))
        or not isinstance(discovered_device_ids, Sequence)
        or not isinstance(assignments, Mapping)
    ):
        raise ContractError("CAMERA_ROLE_BINDINGS_INPUT")
    devices = normalize_camera_devices(discovered_device_ids)
    discovered = {item["logical_id"]: item for item in devices}
    if set(assignments) != set(discovered):
        raise ContractError("CAMERA_ROLE_BINDINGS_DISCOVERY")
    checked_assignments = {}
    for device, role in assignments.items():
        if device not in discovered:
            raise ContractError("CAMERA_ROLE_BINDINGS_DISCOVERY")
        if role not in CAMERA_SETUP_ROLES:
            raise ContractError("CAMERA_ROLE_BINDINGS_ROLE")
        checked_assignments[device] = role
    used_roles = [role.lower() for role in checked_assignments.values() if role != "UNUSED"]
    profile_roles = collection_profile.get("camera_roles")
    if (
        not used_roles
        or len(used_roles) != len(set(used_roles))
        or not isinstance(profile_roles, list)
        or sorted(used_roles) != sorted(profile_roles)
    ):
        raise ContractError("CAMERA_ROLE_BINDINGS_PROFILE")
    serials = collection_profile.get("camera_serials")
    if not isinstance(serials, Mapping) or set(serials) != set(profile_roles):
        raise ContractError("CAMERA_ROLE_BINDINGS_PROFILE")
    bindings = {}
    for device, ui_role in sorted(checked_assignments.items()):
        if ui_role == "UNUSED":
            continue
        role = ui_role.lower()
        serial = serials.get(role)
        if (
            not isinstance(serial, str) or not serial
            or serial != "RUNTIME_BINDING_REQUIRED" and serial not in device
        ):
            raise ContractError("CAMERA_ROLE_BINDINGS_DEVICE_PROFILE")
        binding_id = (
            "camera-"
            + canonical_digest({
                "profile": collection_profile["collection_profile_id"],
                "role": role, "device": device,
            }).removeprefix("sha256:")[:20]
        )
        descriptor = discovered[device]
        bindings[role] = build_camera_binding_candidate(
            binding_id=binding_id, device_kind=descriptor["kind"],
            stable_device_id=device,
            capture_endpoint=descriptor["capture_endpoint"],
            intended_role=role, collection_profile=collection_profile,
            connected=True,
        )
    result = {
        "schema_version": CAMERA_ROLE_BINDINGS_SCHEMA,
        "collection_profile_id": collection_profile["collection_profile_id"],
        "collection_profile_digest": canonical_digest(collection_profile),
        "devices": {
            device: {
                "kind": descriptor["kind"],
                "capture_endpoint": descriptor["capture_endpoint"],
            }
            for device, descriptor in sorted(discovered.items())
        },
        "assignments": dict(sorted(checked_assignments.items())),
        "bindings": bindings,
    }
    result["binding_digest"] = canonical_digest(result)
    return validate_camera_role_bindings(result)


def validate_camera_role_bindings(value: object) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(
        value, CAMERA_ROLE_BINDINGS_FIELDS, "CAMERA_ROLE_BINDINGS_FIELDS",
    )))
    if result.get("schema_version") != CAMERA_ROLE_BINDINGS_SCHEMA:
        raise ContractError("CAMERA_ROLE_BINDINGS_SCHEMA")
    _identifier(result.get("collection_profile_id"), "CAMERA_ROLE_BINDINGS_PROFILE")
    _digest(result.get("collection_profile_digest"), "CAMERA_ROLE_BINDINGS_PROFILE")
    devices = result.get("devices")
    assignments, bindings = result.get("assignments"), result.get("bindings")
    if (
        not isinstance(devices, Mapping)
        or not isinstance(assignments, Mapping)
        or not isinstance(bindings, Mapping)
        or set(devices) != set(assignments)
    ):
        raise ContractError("CAMERA_ROLE_BINDINGS_FIELDS")
    for stable_id, descriptor in devices.items():
        if (
            not isinstance(descriptor, Mapping)
            or set(descriptor) != {"kind", "capture_endpoint"}
            or descriptor["kind"] not in {"UVC", "REALSENSE"}
        ):
            raise ContractError("CAMERA_ROLE_BINDINGS_DISCOVERY")
        _stable_device_id(descriptor["kind"], stable_id)
        expected_endpoint = (
            str(Path("/dev/v4l/by-id") / stable_id)
            if descriptor["kind"] == "UVC" else stable_id
        )
        if descriptor["capture_endpoint"] != expected_endpoint:
            raise ContractError("CAMERA_ROLE_BINDINGS_DISCOVERY")
    used = {}
    for device, role in assignments.items():
        if device not in devices:
            raise ContractError("CAMERA_ROLE_BINDINGS_DISCOVERY")
        if role not in CAMERA_SETUP_ROLES:
            raise ContractError("CAMERA_ROLE_BINDINGS_ROLE")
        if role != "UNUSED":
            if role in used:
                raise ContractError("CAMERA_ROLE_BINDINGS_ROLE")
            used[role] = device
    if set(bindings) != {role.lower() for role in used} or not bindings:
        raise ContractError("CAMERA_ROLE_BINDINGS_FIELDS")
    for role, candidate in bindings.items():
        checked = validate_camera_binding_candidate(candidate)
        ui_role = role.upper()
        if (
            checked["intended_role"] != role
            or checked["stable_device_id"] != used.get(ui_role)
            or checked["device_kind"] != devices[checked["stable_device_id"]]["kind"]
            or checked["capture_endpoint"]
            != devices[checked["stable_device_id"]]["capture_endpoint"]
            or checked["collection_profile_id"] != result["collection_profile_id"]
            or checked["collection_profile_digest"] != result["collection_profile_digest"]
        ):
            raise ContractError("CAMERA_ROLE_BINDINGS_BINDING")
        bindings[role] = checked
    if result["binding_digest"] != canonical_digest({
        key: result[key] for key in result if key != "binding_digest"
    }):
        raise ContractError("CAMERA_ROLE_BINDINGS_DIGEST_MISMATCH")
    result["assignments"] = dict(sorted(assignments.items()))
    result["devices"] = dict(sorted(devices.items()))
    result["bindings"] = dict(sorted(bindings.items()))
    return result


def reuse_camera_role_bindings(
    value: object, *, discovered_device_ids: Sequence[object],
    collection_profile: Mapping[str, Any],
) -> dict[str, Any]:
    stored = validate_camera_role_bindings(value)
    discovered = normalize_camera_devices(discovered_device_ids)
    exact_devices = {
        item["logical_id"]: {
            "kind": item["kind"], "capture_endpoint": item["capture_endpoint"],
        }
        for item in discovered
    }
    if (
        exact_devices != stored["devices"]
        or stored["collection_profile_id"]
        != collection_profile.get("collection_profile_id")
        or stored["collection_profile_digest"] != canonical_digest(collection_profile)
    ):
        raise ContractError("CAMERA_ROLE_BINDINGS_STALE")
    return build_camera_role_bindings(
        collection_profile=collection_profile,
        discovered_device_ids=discovered,
        assignments=stored["assignments"],
    )


def write_camera_role_bindings(
    value: object, *, repository_root: str | Path,
) -> Path:
    bindings = validate_camera_role_bindings(value)
    repository = Path(repository_root).resolve(strict=True)
    receipt = repository / CAMERA_ROLE_BINDINGS_RECEIPT
    _no_symlink_components(repository, receipt)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    _no_symlink_components(repository, receipt)
    write_json_atomic(receipt, bindings)
    return receipt


def load_camera_role_bindings(
    *, repository_root: str | Path,
) -> dict[str, Any]:
    repository = Path(repository_root).resolve(strict=True)
    receipt = repository / CAMERA_ROLE_BINDINGS_RECEIPT
    _no_symlink_components(repository, receipt)
    if receipt.is_symlink() or not receipt.is_file():
        raise ContractError("CAMERA_ROLE_BINDINGS_RECEIPT")
    return validate_camera_role_bindings(load_json_strict(receipt))


def write_camera_binding_receipt(
    value: object, *, repository_root: str | Path,
) -> Path:
    """Atomically store the validated candidate under the already-ignored outputs root."""
    binding = validate_camera_binding_candidate(value)
    repository = Path(repository_root).resolve(strict=True)
    receipt = repository / CAMERA_BINDING_RECEIPT
    _no_symlink_components(repository, receipt)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    _no_symlink_components(repository, receipt)
    write_json_atomic(receipt, binding)
    return receipt


def load_camera_binding_receipt(*, repository_root: str | Path) -> dict[str, Any]:
    repository = Path(repository_root).resolve(strict=True)
    receipt = repository / CAMERA_BINDING_RECEIPT
    _no_symlink_components(repository, receipt)
    if receipt.is_symlink() or not receipt.is_file():
        raise ContractError("CAMERA_BINDING_RECEIPT")
    return validate_camera_binding_candidate(load_json_strict(receipt))


def gripper_setup_projection(
    readback: Mapping[str, Any] | None, *,
    open_target_m: float = 0.021, tolerance_m: float = 0.000105,
) -> dict[str, Any]:
    """Classify one fresh controller readback without invoking activation."""
    if readback is None:
        return {"state": "NOT_AVAILABLE", "supported_action": "NONE", "maintenance_call_count": 0}
    fields = {
        "active", "position_valid", "gripper_index", "reference_position_m",
        "feedback_position_m", "sample_age_s", "max_age_s", "source",
    }
    if not isinstance(readback, Mapping) or set(readback) != fields:
        raise ContractError("GRIPPER_SETUP_READBACK")
    if (
        type(readback["active"]) is not bool
        or type(readback["position_valid"]) is not bool
        or type(readback["gripper_index"]) is not int
        or readback["source"] not in {"CONTROLLER_STATE", "COMMAND_SERVER_MAINTENANCE"}
        or isinstance(open_target_m, bool) or not isinstance(open_target_m, (int, float))
        or isinstance(tolerance_m, bool) or not isinstance(tolerance_m, (int, float))
        or not math.isfinite(open_target_m) or not math.isfinite(tolerance_m)
        or open_target_m <= 0 or tolerance_m <= 0
    ):
        raise ContractError("GRIPPER_SETUP_READBACK")
    age, max_age = readback["sample_age_s"], readback["max_age_s"]
    if (
        isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age)
        or isinstance(max_age, bool) or not isinstance(max_age, (int, float)) or not math.isfinite(max_age)
        or age < 0 or max_age <= 0 or max_age > 0.1 or age > max_age
    ):
        return {"state": "NOT_AVAILABLE", "supported_action": "NONE", "maintenance_call_count": 0}
    reference, feedback = readback["reference_position_m"], readback["feedback_position_m"]
    positions_valid = all(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0 <= value <= open_target_m
        for value in (reference, feedback)
    )
    if readback["position_valid"] is not positions_valid:
        raise ContractError("GRIPPER_SETUP_READBACK")
    readback_digest = canonical_digest(dict(readback))
    if readback["gripper_index"] != 1:
        return {
            "state": "BLOCKED_BINDING", "supported_action": "NONE",
            "maintenance_call_count": 0, "readback_digest": readback_digest,
        }
    if (
        readback["active"] and positions_valid
        and abs(float(reference) - float(open_target_m)) <= float(tolerance_m)
        and abs(float(feedback) - float(open_target_m)) <= float(tolerance_m)
    ):
        return {
            "state": "ATTACHED", "supported_action": "VERIFY",
            "maintenance_call_count": 0, "readback_digest": readback_digest,
        }
    return {
        "state": "MAINTENANCE_APPROVAL_REQUIRED",
        "supported_action": "REQUEST_OPEN_NORMALIZATION",
        "maintenance_call_count": 0,
        "readback_digest": readback_digest,
    }
