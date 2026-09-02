"""Immutable, metadata-only receipt for one committed dataset episode.

The ledger grants no motion, deletion, or training authority.  Artifact paths
are references to existing evidence; artifact payloads are validated but never
copied into the receipt.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Mapping

from tools.data_factory.quality.coverage_report import CANDIDATE_FIELDS, TECHNICAL_FIELDS
from tools.data_factory.task_recipe import validate_episode_instruction_binding
from tools.fr5_data_factory import (
    ContractError, DIGEST, RFC3339, SAFE_ID, TASK_REVIEW_CHECKLIST_IDS,
    canonical_digest, load_json_strict,
)


SCHEMA_VERSION = "data_factory.episode_ledger.v1"
STATE_SCHEMA_VERSION = "data_factory.episode_ledger_state.v1"
EPISODE_REF_SCHEMA = "data_factory.episode_ref.v1"
EPISODE_LOCATOR_SCHEMA = "data_factory.lerobot_v3_episode_locator.v1"
ARTIFACT_NAMES = frozenset({
    "episode", "run", "staging_manifest", "manifest", "intent", "plan", "technical",
    "source_provenance", "recording_quality", "execution", "runtime_binding",
})
ARTIFACT_REF_FIELDS = frozenset({"artifact_path", "artifact_digest"})
LEDGER_FIELDS = frozenset({
    "schema_version", "dataset", "episode", "bindings", "artifacts",
    "admission", "ledger_digest",
})
STATE_FIELDS = frozenset({
    "schema_version", "ledger_digest", "episode_ref_digest", "candidate",
    "review", "retention", "state_digest",
})
DATASET_FIELDS = frozenset({
    "dataset_id", "repo_id", "dataset_root", "dataset_digest",
})
EPISODE_FIELDS = frozenset({
    "run_id", "episode_index", "transaction_id", "episode_ref",
    "episode_ref_digest", "lerobot_v3_locator",
})
BINDING_FIELDS = frozenset({
    "resolved_job_digest", "manifest_digest", "intent_digest", "slot_digest",
    "base_condition_digest", "robot_start_pose_id", "scene_state_digest",
    "root_binding_digest", "start_binding_digest", "collection_profile_digest",
    "plan_digest",
})
ADMISSION_FIELDS = frozenset({
    "technical_status", "review_context_digest", "training_status",
})
REVIEW_FIELDS = frozenset({
    "semantic_status", "reviewed_by", "reviewed_at", "reason", "training_status",
})
RETENTION_FIELDS = frozenset({
    "retention_state", "reclaim_state", "physical_deletion", "storage_layout",
})
EPISODE_REF_FIELDS = frozenset({
    "schema_version", "repo_id", "episode_index", "transaction_id",
    "resolved_job_digest", "staging_manifest_digest",
})
RUN_FIELDS = frozenset({
    "schema_version", "run_id", "transaction_id", "episode_index", "state",
    "reason_code", "rows", "detail",
})
STAGING_MANIFEST_FIELDS = frozenset({
    "schema_version", "run_id", "dataset_root", "episode_index", "staging_mode",
    "binding_digests", "camera_staging_dirs", "begin_snapshot",
})
STAGING_BINDING_FIELDS = frozenset({
    "resolved_job_digest", "selected_sheet_digest", "yaw0_sheet_digest",
    "cell_calibration_digest", "robot_system_digest", "collection_profile_digest",
    "object_profile_digest", "grasp_profile_digest",
})
EPISODE_STORAGE_FIELDS = frozenset({
    "schema_version", "run_id", "episode_ref", "dataset_filesystem",
    "encoder_temp_filesystem", "dataset_bytes_before", "dataset_bytes_after",
    "dataset_delta_bytes", "temporary_peak_bytes_by_filesystem", "free_bytes_before",
    "free_bytes_after", "reference_scan_status", "dataset_prunable",
})
EPISODE_LOCATOR_FIELDS = frozenset({
    "schema_version", "repo_id", "episode_index", "data", "videos",
    "locator_digest",
})
EPISODE_DATA_LOCATOR_FIELDS = frozenset({
    "chunk_index", "file_index", "relative_path", "file_row_start",
    "file_row_end_exclusive",
})
EPISODE_VIDEO_LOCATOR_FIELDS = frozenset({
    "camera_key", "chunk_index", "file_index", "relative_path", "file_frame_start",
    "file_frame_end_exclusive", "timestamp_start_s", "timestamp_end_s",
})
PREAPPROVAL_FIELDS = frozenset({
    "schema_version", "run_id", "resolved_job_digest", "plan_digest",
    "plan_envelope", "plan_envelope_digest",
})
PREAPPROVAL_V2_FIELDS = PREAPPROVAL_FIELDS | frozenset({
    "episode_instruction_binding", "episode_instruction_binding_digest",
})
PRECOMMIT_SAFETY_FIELDS = frozenset({
    "schema_version", "run_id", "approved_plan_digest", "scene_binding_digest",
    "expected_planning_scene_digest", "planning_scene_readback_digest",
    "collision_report_digest", "plan_only_no_motion_digest",
    "post_reset_safe_snapshot_digest", "status",
})
COMMON_SAFETY_FIELDS = (
    "approved_plan_digest", "scene_binding_digest", "expected_planning_scene_digest",
    "planning_scene_readback_digest", "collision_report_digest",
    "plan_only_no_motion_digest",
)
EXECUTION_FIELDS = frozenset({
    "schema_version", "mode", "op_id", "op", "ok", "code", "run_id",
    "plan_digest", "state", "data",
})
RUNTIME_BINDING_FIELDS = frozenset({
    "schema_version", "session_id", "run_id", "intent_digest", "manifest_digest",
    "slot_digest", "resolved_job_digest", "root_binding_digest", "start_binding_digest",
    "state_initialization_digest", "scene_observation_digest", "scene_state_digest",
    "place_alias", "place_id", "yaw_deg", "x_mm", "y_mm", "robot_start_pose_id",
    "split_group", "repeat_index", "budget_digests", "expires_at",
    "data_disposition", "authority", "binding_digest",
})
RUNTIME_BUDGET_FIELDS = frozenset({
    "manifest_budget_digest", "program_budget_digest", "planned_usage_digest",
    "slot_budget_digest",
})
RUNTIME_AUTHORITY = {
    "execution": "NONE", "human_approval": "NONE", "semantic_pass": "NONE",
    "training_approval": "NONE", "persistent_start_qualification": "NONE",
}
RUNTIME_SCHEMAS = {
    "TEST_ONLY": "data_factory.test_only_episode_binding.v1",
    "PRODUCTION": "data_factory.production_episode_binding.v1",
}


def _exact(value: object, fields: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(code)
    return copy.deepcopy(dict(value))


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ContractError(code)
    return value


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ContractError(code)
    return value


def _count(value: object, code: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ContractError(code)
    return value


def _locator_path(
    value: object, *, kind: str, chunk_index: int, file_index: int,
    camera_key: str | None = None, dataset_root: str | None = None,
) -> str:
    code = "EPISODE_LEDGER_LOCATOR_PATH"
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ContractError(code)
    relative = PurePosixPath(value)
    expected = (
        PurePosixPath("data") / f"chunk-{chunk_index:03d}" / f"file-{file_index:03d}.parquet"
        if kind == "data"
        else PurePosixPath("videos") / str(camera_key) / f"chunk-{chunk_index:03d}" / f"file-{file_index:03d}.mp4"
    )
    if (
        relative.is_absolute() or str(relative) != value or ".." in relative.parts
        or len(relative.parts) < 3 or relative.parts[-2:] != expected.parts[-2:]
        or kind == "video" and camera_key not in relative.parts[:-2]
    ):
        raise ContractError(code)
    if dataset_root is not None:
        path = Path(dataset_root).joinpath(*relative.parts)
        try:
            resolved = path.resolve(strict=True)
            if path != resolved or path.is_symlink() or not resolved.is_file():
                raise ContractError(code)
        except OSError as exc:
            raise ContractError(code) from exc
    return value


def _episode_locator(
    value: object, *, dataset_root: str | None = None,
    repo_id: str | None = None, episode_index: int | None = None,
    rows: int | None = None,
) -> dict[str, Any]:
    locator = _exact(value, EPISODE_LOCATOR_FIELDS, "EPISODE_LEDGER_LOCATOR_FIELDS")
    if (
        locator["schema_version"] != EPISODE_LOCATOR_SCHEMA
        or not isinstance(locator["repo_id"], str)
        or not locator["repo_id"].strip()
        or "\x00" in locator["repo_id"]
        or repo_id is not None and locator["repo_id"] != repo_id
        or episode_index is not None and locator["episode_index"] != episode_index
    ):
        raise ContractError("EPISODE_LEDGER_LOCATOR_BINDING")
    _count(locator["episode_index"], "EPISODE_LEDGER_LOCATOR_EPISODE")
    expected_digest = _digest(locator["locator_digest"], "EPISODE_LEDGER_LOCATOR_DIGEST")

    data = _exact(
        locator["data"], EPISODE_DATA_LOCATOR_FIELDS,
        "EPISODE_LEDGER_DATA_LOCATOR_FIELDS",
    )
    chunk = _count(data["chunk_index"], "EPISODE_LEDGER_DATA_LOCATOR_RANGE")
    file_index = _count(data["file_index"], "EPISODE_LEDGER_DATA_LOCATOR_RANGE")
    row_start = _count(data["file_row_start"], "EPISODE_LEDGER_DATA_LOCATOR_RANGE")
    row_end = _count(
        data["file_row_end_exclusive"], "EPISODE_LEDGER_DATA_LOCATOR_RANGE", positive=True,
    )
    if row_end <= row_start or rows is not None and row_end - row_start != rows:
        raise ContractError("EPISODE_LEDGER_DATA_LOCATOR_RANGE")
    data["relative_path"] = _locator_path(
        data["relative_path"], kind="data", chunk_index=chunk,
        file_index=file_index, dataset_root=dataset_root,
    )

    videos = locator["videos"]
    if not isinstance(videos, list) or not videos:
        raise ContractError("EPISODE_LEDGER_VIDEO_LOCATORS")
    normalized_videos = []
    for value in videos:
        video = _exact(
            value, EPISODE_VIDEO_LOCATOR_FIELDS,
            "EPISODE_LEDGER_VIDEO_LOCATOR_FIELDS",
        )
        camera_key = _identifier(
            video["camera_key"], "EPISODE_LEDGER_VIDEO_CAMERA_KEY",
        )
        chunk = _count(video["chunk_index"], "EPISODE_LEDGER_VIDEO_LOCATOR_RANGE")
        file_index = _count(video["file_index"], "EPISODE_LEDGER_VIDEO_LOCATOR_RANGE")
        frame_start = _count(
            video["file_frame_start"], "EPISODE_LEDGER_VIDEO_LOCATOR_RANGE",
        )
        frame_end = _count(
            video["file_frame_end_exclusive"], "EPISODE_LEDGER_VIDEO_LOCATOR_RANGE",
            positive=True,
        )
        start_s, end_s = video["timestamp_start_s"], video["timestamp_end_s"]
        if (
            frame_end <= frame_start
            or frame_end - frame_start != row_end - row_start
            or any(
                isinstance(item, bool) or not isinstance(item, (int, float))
                or not math.isfinite(item)
                for item in (start_s, end_s)
            )
            or start_s < 0 or end_s <= start_s
        ):
            raise ContractError("EPISODE_LEDGER_VIDEO_LOCATOR_RANGE")
        video["timestamp_start_s"] = float(start_s)
        video["timestamp_end_s"] = float(end_s)
        video["relative_path"] = _locator_path(
            video["relative_path"], kind="video", chunk_index=chunk,
            file_index=file_index, camera_key=camera_key, dataset_root=dataset_root,
        )
        normalized_videos.append(video)
    if (
        [video["camera_key"] for video in normalized_videos]
        != sorted(video["camera_key"] for video in normalized_videos)
        or len({video["camera_key"] for video in normalized_videos}) != len(normalized_videos)
    ):
        raise ContractError("EPISODE_LEDGER_VIDEO_LOCATORS")
    locator["data"], locator["videos"] = data, normalized_videos
    if canonical_digest({
        key: item for key, item in locator.items() if key != "locator_digest"
    }) != expected_digest:
        raise ContractError("EPISODE_LEDGER_LOCATOR_DIGEST")
    return locator


def build_lerobot_v3_episode_locator(
    *, repo_id: str, episode_index: int, data: Mapping[str, Any],
    videos: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Canonicalize file-local LeRobot v3 row/frame metadata for the ledger."""
    if not isinstance(data, Mapping) or not isinstance(videos, list) or any(
        not isinstance(video, Mapping) for video in videos
    ):
        raise ContractError("EPISODE_LEDGER_LOCATOR_FIELDS")
    normalized_videos = [copy.deepcopy(dict(video)) for video in videos]
    if any(not isinstance(video.get("camera_key"), str) for video in normalized_videos):
        raise ContractError("EPISODE_LEDGER_VIDEO_CAMERA_KEY")
    normalized_videos.sort(key=lambda video: video["camera_key"])
    locator = {
        "schema_version": EPISODE_LOCATOR_SCHEMA,
        "repo_id": repo_id,
        "episode_index": episode_index,
        "data": copy.deepcopy(dict(data)),
        "videos": normalized_videos,
    }
    locator["locator_digest"] = canonical_digest(locator)
    return _episode_locator(locator)


def _canonical_directory(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ContractError(code)
    source = Path(value)
    try:
        resolved = source.resolve(strict=True)
        if source.is_symlink() or not resolved.is_dir():
            raise ContractError(code)
    except OSError as exc:
        raise ContractError(code) from exc
    canonical = str(resolved)
    if value != canonical:
        raise ContractError(code)
    return canonical


def _canonical_file(value: object, code: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ContractError(code)
    source = Path(value)
    try:
        resolved = source.resolve(strict=True)
        if source.is_symlink() or not resolved.is_file():
            raise ContractError(code)
    except OSError as exc:
        raise ContractError(code) from exc
    if value != str(resolved):
        raise ContractError(code)
    return resolved


def _dataset(value: object) -> dict[str, Any]:
    dataset = _exact(value, DATASET_FIELDS, "EPISODE_LEDGER_DATASET_FIELDS")
    _identifier(dataset["dataset_id"], "EPISODE_LEDGER_DATASET_ID")
    if (
        not isinstance(dataset["repo_id"], str) or not dataset["repo_id"].strip()
        or "\x00" in dataset["repo_id"]
    ):
        raise ContractError("EPISODE_LEDGER_DATASET_REPO")
    dataset["dataset_root"] = _canonical_directory(
        dataset["dataset_root"], "EPISODE_LEDGER_DATASET_ROOT",
    )
    _digest(dataset["dataset_digest"], "EPISODE_LEDGER_DATASET_DIGEST")
    return dataset


def _jsonl(path: Path, code: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ContractError(code) from exc
    if not lines or any(not line.strip() for line in lines):
        raise ContractError(code)
    try:
        return [load_json_strict(line) for line in lines]
    except ContractError as exc:
        raise ContractError(code) from exc


def _artifact(
    value: object, *, name: str, episode_index: int,
) -> tuple[dict[str, str], object]:
    ref = _exact(value, ARTIFACT_REF_FIELDS, f"EPISODE_LEDGER_{name.upper()}_REF")
    path = _canonical_file(ref["artifact_path"], f"EPISODE_LEDGER_{name.upper()}_PATH")
    expected = _digest(ref["artifact_digest"], f"EPISODE_LEDGER_{name.upper()}_DIGEST")
    if name in {"source_provenance", "recording_quality"}:
        rows = _jsonl(path, f"EPISODE_LEDGER_{name.upper()}_ARTIFACT")
        if name == "source_provenance":
            payload: object = rows
        else:
            matches = [row for row in rows if row.get("episode_index") == episode_index]
            if len(matches) != 1:
                raise ContractError("EPISODE_LEDGER_RECORDING_QUALITY_EPISODE")
            payload = matches[0]
    else:
        try:
            payload = load_json_strict(path)
        except ContractError as exc:
            raise ContractError(f"EPISODE_LEDGER_{name.upper()}_ARTIFACT") from exc
    if canonical_digest(payload) != expected:
        raise ContractError(f"EPISODE_LEDGER_{name.upper()}_DIGEST")
    return {"artifact_path": str(path), "artifact_digest": expected}, payload


def _episode_ref(value: object) -> dict[str, Any]:
    ref = _exact(value, EPISODE_REF_FIELDS, "EPISODE_LEDGER_EPISODE_REF_FIELDS")
    if (
        ref["schema_version"] != EPISODE_REF_SCHEMA
        or not isinstance(ref["repo_id"], str) or not ref["repo_id"].strip()
        or "\x00" in ref["repo_id"]
    ):
        raise ContractError("EPISODE_LEDGER_EPISODE_REF")
    _count(ref["episode_index"], "EPISODE_LEDGER_EPISODE_INDEX")
    if not isinstance(ref["transaction_id"], str) or not ref["transaction_id"]:
        raise ContractError("EPISODE_LEDGER_TRANSACTION_ID")
    _digest(ref["resolved_job_digest"], "EPISODE_LEDGER_RESOLVED_JOB_DIGEST")
    _digest(ref["staging_manifest_digest"], "EPISODE_LEDGER_STAGING_MANIFEST_DIGEST")
    return ref


def _self_digest(value: Mapping[str, Any], field: str, code: str) -> str:
    expected = _digest(value.get(field), code)
    body = {key: copy.deepcopy(item) for key, item in value.items() if key != field}
    if canonical_digest(body) != expected:
        raise ContractError(code)
    return expected


def _runtime_binding(
    value: object, *, run_id: str, resolved_job_digest: str,
    manifest_digest: str, intent_digest: str, slot: Mapping[str, Any],
    required_scene_digest: str,
) -> dict[str, Any]:
    runtime = _exact(
        value, RUNTIME_BINDING_FIELDS, "EPISODE_LEDGER_RUNTIME_BINDING_FIELDS",
    )
    disposition = runtime["data_disposition"]
    if (
        disposition not in RUNTIME_SCHEMAS
        or runtime["schema_version"] != RUNTIME_SCHEMAS[disposition]
        or runtime["run_id"] != run_id
        or runtime["resolved_job_digest"] != resolved_job_digest
        or runtime["manifest_digest"] != manifest_digest
        or runtime["intent_digest"] != intent_digest
        or runtime["slot_digest"] != canonical_digest(slot)
        or runtime["robot_start_pose_id"] != slot.get("robot_start_pose_id")
        or runtime["scene_state_digest"] != required_scene_digest
        or runtime["split_group"] != slot.get("split_group")
        or runtime["repeat_index"] != slot.get("repeat_index")
        or runtime["authority"] != RUNTIME_AUTHORITY
        or runtime["binding_digest"] != canonical_digest({
            key: item for key, item in runtime.items() if key != "binding_digest"
        })
    ):
        raise ContractError("EPISODE_LEDGER_RUNTIME_BINDING")
    for field in ("session_id", "place_alias", "place_id", "robot_start_pose_id"):
        _identifier(runtime[field], "EPISODE_LEDGER_RUNTIME_BINDING")
    for field in (
        "intent_digest", "manifest_digest", "slot_digest", "resolved_job_digest",
        "root_binding_digest", "start_binding_digest", "scene_state_digest",
        "binding_digest",
    ):
        _digest(runtime[field], "EPISODE_LEDGER_RUNTIME_BINDING")
    sources = (runtime["state_initialization_digest"], runtime["scene_observation_digest"])
    if (
        (disposition == "TEST_ONLY" and sum(item is not None for item in sources) != 1)
        or (
            disposition == "PRODUCTION"
            and (runtime["state_initialization_digest"] is not None
                 or runtime["scene_observation_digest"] is None)
        )
    ):
        raise ContractError("EPISODE_LEDGER_RUNTIME_SCENE_SOURCE")
    _digest(next(item for item in sources if item is not None), "EPISODE_LEDGER_RUNTIME_BINDING")
    if (
        any(
            isinstance(runtime[field], bool)
            or not isinstance(runtime[field], (int, float))
            or not math.isfinite(runtime[field])
            for field in ("yaw_deg", "x_mm", "y_mm")
        )
        or type(runtime["repeat_index"]) is not int
        or runtime["repeat_index"] < 0
        or runtime["split_group"] not in {"TRAIN", "ID", "OOD"}
        or not isinstance(runtime["expires_at"], str)
        or not RFC3339.fullmatch(runtime["expires_at"])
    ):
        raise ContractError("EPISODE_LEDGER_RUNTIME_BINDING")
    budgets = _exact(
        runtime["budget_digests"], RUNTIME_BUDGET_FIELDS,
        "EPISODE_LEDGER_RUNTIME_BUDGET_FIELDS",
    )
    for item in budgets.values():
        _digest(item, "EPISODE_LEDGER_RUNTIME_BINDING")
    runtime["budget_digests"] = budgets
    return runtime


def _plan_binding(
    value: object, run_id: str, resolved_job_digest: str,
) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise ContractError("EPISODE_LEDGER_PLAN_FIELDS")
    schema = value.get("schema_version")
    fields = (
        PREAPPROVAL_FIELDS
        if schema == "data_factory.preapproval_evidence.v1"
        else PREAPPROVAL_V2_FIELDS
        if schema == "data_factory.preapproval_evidence.v2"
        else frozenset()
    )
    plan_artifact = _exact(value, fields, "EPISODE_LEDGER_PLAN_FIELDS")
    if (
        plan_artifact["run_id"] != run_id
        or plan_artifact["resolved_job_digest"] != resolved_job_digest
    ):
        raise ContractError("EPISODE_LEDGER_PLAN_RUN")
    if schema == "data_factory.preapproval_evidence.v2":
        try:
            instruction = validate_episode_instruction_binding(
                plan_artifact["episode_instruction_binding"],
            )
            if (
                plan_artifact["episode_instruction_binding_digest"]
                != instruction["binding_digest"]
            ):
                raise ContractError("EPISODE_LEDGER_PLAN_INSTRUCTION")
        except ContractError as exc:
            raise ContractError("EPISODE_LEDGER_PLAN_INSTRUCTION") from exc
    envelope = plan_artifact["plan_envelope"]
    if (
        not isinstance(envelope, Mapping)
        or set(envelope) != {"plan", "precommit_safety", "precommit_evidence", "operator_summary"}
        or any(not isinstance(envelope.get(key), Mapping) for key in envelope)
    ):
        raise ContractError("EPISODE_LEDGER_PLAN")
    plan = envelope["plan"]
    safety = _exact(
        envelope["precommit_safety"], PRECOMMIT_SAFETY_FIELDS,
        "EPISODE_LEDGER_PLAN_SAFETY_FIELDS",
    )
    precommit = envelope["precommit_evidence"]
    plan_digest = _digest(plan_artifact["plan_digest"], "EPISODE_LEDGER_PLAN_DIGEST")
    if (
        canonical_digest(plan) != plan_digest
        or plan_artifact["plan_envelope_digest"] != canonical_digest(envelope)
        or plan.get("schema_version") != "fr5.pickup_plan.v3"
        or plan.get("run_id") != run_id
        or plan.get("resolved_job_digest") != resolved_job_digest
        or safety.get("schema_version") != "data_factory.precommit_safety.v1"
        or safety.get("run_id") != run_id
        or safety.get("approved_plan_digest") != plan_digest
        or safety.get("status") != "PENDING"
        or safety.get("post_reset_safe_snapshot_digest") is not None
        or precommit.get("schema_version") != "data_factory.precommit_evidence.v1"
        or precommit.get("run_id") != run_id
        or precommit.get("approved_plan_digest") != plan_digest
    ):
        raise ContractError("EPISODE_LEDGER_PLAN_DIGEST")
    for field in COMMON_SAFETY_FIELDS[1:]:
        _digest(safety[field], "EPISODE_LEDGER_PLAN_SAFETY_DIGEST")
    return plan_digest, safety


def _source_bindings(
    *, dataset_root: str, artifacts: object,
    expected_episode_ref: Mapping[str, Any] | None = None,
) -> tuple[
    dict[str, dict[str, str]], dict[str, str], dict[str, str], str,
    dict[str, Any], int,
]:
    refs = _exact(artifacts, ARTIFACT_NAMES, "EPISODE_LEDGER_ARTIFACTS")
    loaded: dict[str, object] = {}
    normalized: dict[str, dict[str, str]] = {}
    normalized["episode"], loaded["episode"] = _artifact(
        refs["episode"], name="episode", episode_index=0,
    )
    episode_document = loaded["episode"]
    if not isinstance(episode_document, Mapping):
        raise ContractError("EPISODE_LEDGER_EPISODE_ARTIFACT_FIELDS")
    episode_ref = _episode_ref(episode_document.get("episode_ref"))
    if expected_episode_ref is not None and episode_ref != expected_episode_ref:
        raise ContractError("EPISODE_LEDGER_EPISODE_ARTIFACT_BINDING")
    for name in sorted(ARTIFACT_NAMES - {"episode"}):
        normalized[name], loaded[name] = _artifact(
            refs[name], name=name, episode_index=episode_ref["episode_index"],
        )

    run = _exact(loaded["run"], RUN_FIELDS, "EPISODE_LEDGER_RUN_FIELDS")
    run_id = _identifier(run["run_id"], "EPISODE_LEDGER_RUN_ID")
    rows = _count(run["rows"], "EPISODE_LEDGER_RUN_ROWS", positive=True)
    expected_transaction = f"{run_id}:episode-{episode_ref['episode_index']:06d}"
    if (
        run["schema_version"] != "data_factory.recorder_result.v1"
        or run["state"] != "COMMITTED" or run["reason_code"] != "COMMITTED"
        or run["episode_index"] != episode_ref["episode_index"]
        or run["transaction_id"] != episode_ref["transaction_id"]
        or run["transaction_id"] != expected_transaction
        or not isinstance(run["detail"], str)
    ):
        raise ContractError("EPISODE_LEDGER_RUN_STATE")
    storage = _exact(
        loaded["episode"], EPISODE_STORAGE_FIELDS, "EPISODE_LEDGER_EPISODE_ARTIFACT_FIELDS",
    )
    before = storage["dataset_bytes_before"]
    after = storage["dataset_bytes_after"]
    dataset_filesystem = storage["dataset_filesystem"]
    if (
        storage["schema_version"] != "data_factory.storage_usage.v1"
        or storage["run_id"] != run_id
        or storage["episode_ref"] != episode_ref
        or type(before) is not int or before < 0
        or type(after) is not int or after < before
        or storage["dataset_delta_bytes"] != after - before
        or storage["reference_scan_status"] != "NOT_AVAILABLE"
        or storage["dataset_prunable"] != []
        or not isinstance(dataset_filesystem, Mapping)
        or dataset_filesystem.get("path") != dataset_root
        or any(not isinstance(storage[field], Mapping) for field in (
            "dataset_filesystem", "encoder_temp_filesystem",
            "temporary_peak_bytes_by_filesystem", "free_bytes_before", "free_bytes_after",
        ))
    ):
        raise ContractError("EPISODE_LEDGER_EPISODE_ARTIFACT_BINDING")
    staging = _exact(
        loaded["staging_manifest"], STAGING_MANIFEST_FIELDS,
        "EPISODE_LEDGER_STAGING_MANIFEST_FIELDS",
    )
    staging_bindings = _exact(
        staging["binding_digests"], STAGING_BINDING_FIELDS,
        "EPISODE_LEDGER_STAGING_BINDING_FIELDS",
    )
    collection_profile_digest = _digest(
        staging_bindings["collection_profile_digest"],
        "EPISODE_LEDGER_COLLECTION_PROFILE_DIGEST",
    )
    if (
        staging["schema_version"] != "data_factory.staging_manifest.v1"
        or staging["run_id"] != run_id
        or staging["dataset_root"] != dataset_root
        or staging["episode_index"] != episode_ref["episode_index"]
        or staging["staging_mode"] != "batch"
        or staging_bindings["resolved_job_digest"] != episode_ref["resolved_job_digest"]
        or not isinstance(staging["camera_staging_dirs"], Mapping)
        or not isinstance(staging["begin_snapshot"], Mapping)
        or canonical_digest(staging) != episode_ref["staging_manifest_digest"]
    ):
        raise ContractError("EPISODE_LEDGER_STAGING_MANIFEST_BINDING")

    manifest = loaded["manifest"]
    intent = loaded["intent"]
    if not isinstance(manifest, Mapping) or not isinstance(intent, Mapping):
        raise ContractError("EPISODE_LEDGER_CAMPAIGN_BINDING")
    manifest_digest = _self_digest(manifest, "manifest_digest", "EPISODE_LEDGER_MANIFEST_DIGEST")
    intent_digest = _self_digest(intent, "intent_digest", "EPISODE_LEDGER_INTENT_DIGEST")
    if (
        intent.get("run_id") != run_id
        or intent.get("manifest_id") != manifest.get("manifest_id")
        or intent.get("manifest_digest") != manifest_digest
    ):
        raise ContractError("EPISODE_LEDGER_INTENT_BINDING")
    slots = manifest.get("slots")
    slot = intent.get("slot")
    order_index = intent.get("order_index")
    if (
        not isinstance(slots, list) or type(order_index) is not int
        or order_index < 0 or order_index >= len(slots)
        or not isinstance(slot, Mapping) or dict(slot) != slots[order_index]
        or intent.get("slot_digest") != canonical_digest(slot)
    ):
        raise ContractError("EPISODE_LEDGER_SLOT_BINDING")
    base_condition = intent.get("base_condition")
    if (
        not isinstance(base_condition, Mapping)
        or base_condition.get("base_condition_digest") != slot.get("base_condition_digest")
        or base_condition.get("base_condition_digest") != canonical_digest({
            key: item for key, item in base_condition.items() if key != "base_condition_digest"
        })
    ):
        raise ContractError("EPISODE_LEDGER_RESOLVED_JOB_BINDING")
    pose = intent.get("robot_start_pose")
    fixed_contract = intent.get("fixed_contract")
    required_scene_digest = _digest(
        intent.get("required_scene_digest"), "EPISODE_LEDGER_REQUIRED_SCENE_DIGEST",
    )
    if (
        not isinstance(pose, Mapping)
        or pose.get("robot_start_pose_id") != slot.get("robot_start_pose_id")
    ):
        raise ContractError("EPISODE_LEDGER_START_BINDING")
    if (
        not isinstance(fixed_contract, Mapping)
        or fixed_contract.get("collection_profile_digest") != collection_profile_digest
    ):
        raise ContractError("EPISODE_LEDGER_COLLECTION_PROFILE_BINDING")
    runtime = _runtime_binding(
        loaded["runtime_binding"], run_id=run_id,
        resolved_job_digest=episode_ref["resolved_job_digest"],
        manifest_digest=manifest_digest, intent_digest=intent_digest,
        slot=slot, required_scene_digest=required_scene_digest,
    )

    plan_digest, planned_safety = _plan_binding(
        loaded["plan"], run_id, episode_ref["resolved_job_digest"],
    )
    technical = _exact(loaded["technical"], TECHNICAL_FIELDS, "EPISODE_LEDGER_TECHNICAL_FIELDS")
    expected_fps = technical["expected_fps"]
    if (
        technical["schema_version"] != "data_factory.technical_validator_result.v1"
        or technical["run_id"] != run_id
        or technical["resolved_job_digest"] != episode_ref["resolved_job_digest"]
        or technical["plan_digest"] != plan_digest
        or technical["dataset_root"] != dataset_root
        or technical["status"] not in {"PASS", "FAIL"}
        or isinstance(expected_fps, bool) or not isinstance(expected_fps, (int, float))
        or not math.isfinite(expected_fps) or expected_fps <= 0
    ):
        raise ContractError("EPISODE_LEDGER_TECHNICAL_BINDING")
    _digest(technical["result_digest"], "EPISODE_LEDGER_TECHNICAL_DIGEST")

    expected_context = canonical_digest({
        "run_id": run_id,
        "resolved_job_digest": episode_ref["resolved_job_digest"],
        "plan_digest": plan_digest,
        "technical_validator_digest": canonical_digest(technical),
    })
    provenance = loaded["source_provenance"]
    quality = loaded["recording_quality"]
    if (
        not isinstance(provenance, list) or len(provenance) != rows
        or any(row.get("frame_index") != index for index, row in enumerate(provenance))
        or Path(normalized["source_provenance"]["artifact_path"]).name
        != f"episode-{episode_ref['episode_index']:06d}.jsonl"
    ):
        raise ContractError("EPISODE_LEDGER_SOURCE_PROVENANCE_BINDING")
    if (
        not isinstance(quality, Mapping)
        or quality.get("episode_index") != episode_ref["episode_index"]
        or quality.get("frames") != rows
    ):
        raise ContractError("EPISODE_LEDGER_RECORDING_QUALITY_BINDING")

    execution = _exact(loaded["execution"], EXECUTION_FIELDS, "EPISODE_LEDGER_EXECUTION_FIELDS")
    execution_data = execution.get("data")
    terminal_safety = (
        execution_data.get("precommit_safety")
        if isinstance(execution_data, Mapping)
        else None
    )
    if (
        execution["schema_version"] != "fr5.pickup_executor.response.v3"
        or not isinstance(execution["mode"], str) or not execution["mode"]
        or not isinstance(execution["op_id"], str) or not execution["op_id"]
        or not isinstance(execution["op"], str) or not execution["op"]
        or execution["ok"] is not True or execution["code"] != "COMPLETE"
        or execution["run_id"] != run_id
        or execution["plan_digest"] != plan_digest
        or execution["state"] != "COMPLETED"
    ):
        raise ContractError("EPISODE_LEDGER_EXECUTION_BINDING")
    terminal_safety = _exact(
        terminal_safety, PRECOMMIT_SAFETY_FIELDS,
        "EPISODE_LEDGER_EXECUTION_SAFETY_FIELDS",
    )
    if (
        terminal_safety["schema_version"] != "data_factory.precommit_safety.v1"
        or terminal_safety["run_id"] != run_id
        or terminal_safety["status"] != "PASS"
        or any(
            terminal_safety[field] != planned_safety[field]
            for field in COMMON_SAFETY_FIELDS
        )
    ):
        raise ContractError("EPISODE_LEDGER_EXECUTION_SAFETY_BINDING")
    _digest(
        terminal_safety["post_reset_safe_snapshot_digest"],
        "EPISODE_LEDGER_EXECUTION_POST_RESET_DIGEST",
    )

    bindings = {
        "resolved_job_digest": episode_ref["resolved_job_digest"],
        "manifest_digest": manifest_digest,
        "intent_digest": intent_digest,
        "slot_digest": canonical_digest(slot),
        "base_condition_digest": base_condition["base_condition_digest"],
        "robot_start_pose_id": runtime["robot_start_pose_id"],
        "scene_state_digest": runtime["scene_state_digest"],
        "root_binding_digest": runtime["root_binding_digest"],
        "start_binding_digest": runtime["start_binding_digest"],
        "collection_profile_digest": collection_profile_digest,
        "plan_digest": plan_digest,
    }
    admission = {
        "technical_status": technical["status"],
        "review_context_digest": expected_context,
        "training_status": "NOT_AUTHORIZED",
    }
    return normalized, bindings, admission, run_id, episode_ref, rows


def _retention(value: object) -> dict[str, Any]:
    retention = _exact(value, RETENTION_FIELDS, "EPISODE_LEDGER_RETENTION_FIELDS")
    if (
        retention["retention_state"] != "PRESERVE"
        or retention["physical_deletion"] != "NOT_AUTHORIZED"
        or retention["storage_layout"] != "SHARED_CHUNK"
        or retention["reclaim_state"] not in {"NOT_EVALUATED", "REPACK_REQUIRED"}
    ):
        raise ContractError("EPISODE_LEDGER_RETENTION_STATE")
    return retention


def _candidate_review(
    value: object, *, run_id: str, review_context_digest: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    normalized, raw = _artifact(value, name="candidate", episode_index=0)
    candidate = _exact(raw, CANDIDATE_FIELDS, "EPISODE_LEDGER_CANDIDATE_FIELDS")
    semantic = candidate["semantic_status"]
    if (
        candidate["schema_version"] != "data_factory.candidate_admission.v1"
        or candidate["run_id"] != run_id
        or candidate["operational_gate"] != "PASS"
        or candidate["operational_source"] not in {"HIL_PROXY", "HUMAN_GATED"}
        or candidate["checklist_id"] not in TASK_REVIEW_CHECKLIST_IDS
        or candidate["review_context_digest"] != review_context_digest
        or semantic not in {"PENDING", "PASS", "FAIL", "UNCERTAIN"}
    ):
        raise ContractError("EPISODE_LEDGER_CANDIDATE_BINDING")
    if semantic == "PENDING":
        if any(candidate[key] is not None for key in ("reviewed_by", "reviewed_at", "reason")):
            raise ContractError("EPISODE_LEDGER_CANDIDATE_STATE")
    elif (
        not isinstance(candidate["reviewed_by"], str)
        or not SAFE_ID.fullmatch(candidate["reviewed_by"])
        or candidate["reviewed_by"] == "HUMAN"
        or not isinstance(candidate["reviewed_at"], str)
        or not RFC3339.fullmatch(candidate["reviewed_at"])
        or (semantic == "PASS" and candidate["reason"] is not None)
        or (semantic != "PASS" and not isinstance(candidate["reason"], str))
        or (semantic != "PASS" and not candidate["reason"])
    ):
        raise ContractError("EPISODE_LEDGER_CANDIDATE_STATE")
    return normalized, {
        "semantic_status": semantic,
        "reviewed_by": candidate["reviewed_by"],
        "reviewed_at": candidate["reviewed_at"],
        "reason": candidate["reason"],
        "training_status": "NOT_AUTHORIZED",
    }


def compile_episode_ledger(
    *, dataset: Mapping[str, Any], artifacts: Mapping[str, Any],
    episode_locator: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile a receipt from existing evidence without mutating any source."""
    dataset = _dataset(dataset)
    root = dataset["dataset_root"]
    refs, bindings, admission, run_id, ref, rows = _source_bindings(
        dataset_root=root, artifacts=artifacts,
    )
    if ref["repo_id"] != dataset["repo_id"]:
        raise ContractError("EPISODE_LEDGER_DATASET_BINDING")
    ref_digest = canonical_digest(ref)
    locator = _episode_locator(
        episode_locator, dataset_root=root, repo_id=dataset["repo_id"],
        episode_index=ref["episode_index"], rows=rows,
    )
    ledger = {
        "schema_version": SCHEMA_VERSION,
        "dataset": dataset,
        "episode": {
            "run_id": run_id,
            "episode_index": ref["episode_index"],
            "transaction_id": ref["transaction_id"],
            "episode_ref": ref,
            "episode_ref_digest": ref_digest,
            "lerobot_v3_locator": locator,
        },
        "bindings": bindings,
        "artifacts": refs,
        "admission": admission,
    }
    ledger["ledger_digest"] = canonical_digest(ledger)
    return validate_episode_ledger(ledger)


def validate_episode_ledger(value: Mapping[str, Any]) -> dict[str, Any]:
    """Re-open every referenced artifact and validate the immutable join."""
    ledger = _exact(value, LEDGER_FIELDS, "EPISODE_LEDGER_FIELDS")
    if ledger["schema_version"] != SCHEMA_VERSION:
        raise ContractError("EPISODE_LEDGER_SCHEMA")
    expected_digest = _digest(ledger["ledger_digest"], "EPISODE_LEDGER_DIGEST")
    body = {key: copy.deepcopy(item) for key, item in ledger.items() if key != "ledger_digest"}
    if canonical_digest(body) != expected_digest:
        raise ContractError("EPISODE_LEDGER_DIGEST")

    dataset = _dataset(ledger["dataset"])
    root = dataset["dataset_root"]

    episode = _exact(ledger["episode"], EPISODE_FIELDS, "EPISODE_LEDGER_EPISODE_FIELDS")
    ref = _episode_ref(episode["episode_ref"])
    ref_digest = canonical_digest(ref)
    locator = _episode_locator(
        episode["lerobot_v3_locator"], dataset_root=root,
        repo_id=dataset["repo_id"], episode_index=ref["episode_index"],
    )
    if (
        ref["repo_id"] != dataset["repo_id"]
        or episode["episode_index"] != ref["episode_index"]
        or episode["transaction_id"] != ref["transaction_id"]
        or episode["episode_ref_digest"] != ref_digest
        or locator != episode["lerobot_v3_locator"]
    ):
        raise ContractError("EPISODE_LEDGER_EPISODE_BINDING")

    ledger_bindings = _exact(
        ledger["bindings"], BINDING_FIELDS, "EPISODE_LEDGER_BINDING_FIELDS",
    )
    ledger_admission = _exact(
        ledger["admission"], ADMISSION_FIELDS, "EPISODE_LEDGER_ADMISSION_FIELDS",
    )
    refs, bindings, admission, run_id, source_ref, rows = _source_bindings(
        dataset_root=root, artifacts=ledger["artifacts"], expected_episode_ref=ref,
    )
    _episode_locator(
        locator, dataset_root=root, repo_id=dataset["repo_id"],
        episode_index=ref["episode_index"], rows=rows,
    )
    if source_ref != ref:
        raise ContractError("EPISODE_LEDGER_EPISODE_ARTIFACT_BINDING")
    if refs != ledger["artifacts"] or bindings != ledger_bindings or admission != ledger_admission:
        raise ContractError("EPISODE_LEDGER_SOURCE_BINDING")
    if episode["run_id"] != run_id:
        raise ContractError("EPISODE_LEDGER_RUN_BINDING")
    return ledger


def project_episode_state(
    *, ledger: Mapping[str, Any], candidate: Mapping[str, Any] | None = None,
    reclaim_state: str = "NOT_EVALUATED",
) -> dict[str, Any]:
    """Project rewritable review and retention state without changing the base ledger."""
    base = validate_episode_ledger(ledger)
    technical_status = base["admission"]["technical_status"]
    if technical_status == "PASS":
        if candidate is None:
            candidate_ref = None
            review = {
                "semantic_status": "NOT_MEASURED", "reviewed_by": None,
                "reviewed_at": None, "reason": None,
                "training_status": "NOT_AUTHORIZED",
            }
        else:
            candidate_ref, review = _candidate_review(
                candidate, run_id=base["episode"]["run_id"],
                review_context_digest=base["admission"]["review_context_digest"],
            )
    else:
        if candidate is not None:
            raise ContractError("EPISODE_LEDGER_TECHNICAL_FAIL_CANDIDATE")
        candidate_ref = None
        review = {
            "semantic_status": "NOT_AVAILABLE", "reviewed_by": None,
            "reviewed_at": None, "reason": "TECHNICAL_FAIL",
            "training_status": "NOT_AUTHORIZED",
        }
    retention = _retention({
        "retention_state": "PRESERVE",
        "reclaim_state": reclaim_state,
        "physical_deletion": "NOT_AUTHORIZED",
        "storage_layout": "SHARED_CHUNK",
    })
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "ledger_digest": base["ledger_digest"],
        "episode_ref_digest": base["episode"]["episode_ref_digest"],
        "candidate": candidate_ref,
        "review": review,
        "retention": retention,
    }
    state["state_digest"] = canonical_digest(state)
    return validate_episode_state(state, ledger=base)


def reproject_episode_state(
    *, ledger: Mapping[str, Any], current_state: Mapping[str, Any],
    candidate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Reproject review from current evidence without changing retention policy."""
    base = validate_episode_ledger(ledger)
    current = _exact(
        current_state, STATE_FIELDS, "EPISODE_LEDGER_STATE_FIELDS",
    )
    if (
        current["schema_version"] != STATE_SCHEMA_VERSION
        or current["ledger_digest"] != base["ledger_digest"]
        or current["episode_ref_digest"] != base["episode"]["episode_ref_digest"]
        or canonical_digest({
            key: item for key, item in current.items() if key != "state_digest"
        }) != _digest(current["state_digest"], "EPISODE_LEDGER_STATE_DIGEST")
    ):
        raise ContractError("EPISODE_LEDGER_STATE_LEDGER_BINDING")
    retention = _retention(current["retention"])
    updated = project_episode_state(ledger=base, candidate=candidate)
    updated["retention"] = retention
    updated["state_digest"] = canonical_digest({
        key: item for key, item in updated.items() if key != "state_digest"
    })
    return validate_episode_state(updated, ledger=base)


def validate_episode_state(
    value: Mapping[str, Any], *, ledger: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one current-state projection against an immutable ledger."""
    base = validate_episode_ledger(ledger)
    state = _exact(value, STATE_FIELDS, "EPISODE_LEDGER_STATE_FIELDS")
    if state["schema_version"] != STATE_SCHEMA_VERSION:
        raise ContractError("EPISODE_LEDGER_STATE_SCHEMA")
    expected_digest = _digest(state["state_digest"], "EPISODE_LEDGER_STATE_DIGEST")
    if canonical_digest({
        key: copy.deepcopy(item) for key, item in state.items() if key != "state_digest"
    }) != expected_digest:
        raise ContractError("EPISODE_LEDGER_STATE_DIGEST")
    if (
        state["ledger_digest"] != base["ledger_digest"]
        or state["episode_ref_digest"] != base["episode"]["episode_ref_digest"]
    ):
        raise ContractError("EPISODE_LEDGER_STATE_LEDGER_BINDING")
    technical_status = base["admission"]["technical_status"]
    if technical_status == "PASS":
        if state["candidate"] is None:
            candidate_ref = None
            review = {
                "semantic_status": "NOT_MEASURED", "reviewed_by": None,
                "reviewed_at": None, "reason": None,
                "training_status": "NOT_AUTHORIZED",
            }
        else:
            candidate_ref, review = _candidate_review(
                state["candidate"], run_id=base["episode"]["run_id"],
                review_context_digest=base["admission"]["review_context_digest"],
            )
    else:
        if state["candidate"] is not None:
            raise ContractError("EPISODE_LEDGER_TECHNICAL_FAIL_CANDIDATE")
        candidate_ref = None
        review = {
            "semantic_status": "NOT_AVAILABLE", "reviewed_by": None,
            "reviewed_at": None, "reason": "TECHNICAL_FAIL",
            "training_status": "NOT_AUTHORIZED",
        }
    checked_review = _exact(state["review"], REVIEW_FIELDS, "EPISODE_LEDGER_REVIEW_FIELDS")
    if candidate_ref != state["candidate"] or review != checked_review:
        raise ContractError("EPISODE_LEDGER_REVIEW_BINDING")
    retention = _retention(state["retention"])
    if retention != state["retention"]:
        raise ContractError("EPISODE_LEDGER_RETENTION_BINDING")
    return state


__all__ = [
    "EPISODE_LOCATOR_SCHEMA", "SCHEMA_VERSION", "STATE_SCHEMA_VERSION",
    "build_lerobot_v3_episode_locator", "compile_episode_ledger",
    "project_episode_state", "reproject_episode_state", "validate_episode_ledger",
    "validate_episode_state",
]
