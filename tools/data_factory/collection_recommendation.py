"""Pure validation and operator-intent projection for collection advice."""
from __future__ import annotations

import copy
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from tools.data_factory.experiment_manifest import (
    _check_budgets,
    _manifest_budget,
    _usage,
)
from tools.data_factory.state_space import validate_state_space_design_profile
from tools.data_factory.training_split import validate_program_budget
from tools.fr5_data_factory import (
    ContractError,
    DIGEST,
    RFC3339,
    SAFE_ID,
    canonical_digest,
)


SCHEMA_VERSION = "data_factory.collection_recommendation.v1"
SNAPSHOT_SCHEMA = "data_factory.collection_recommendation_input_snapshot.v1"
MANIFEST_SCHEMA = "data_factory.collection_campaign_manifest.v2"
RUN_SCHEMA = "data_factory.recorder_result.v1"
LEDGER_SCHEMA = "data_factory.episode_ledger.v1"
STATE_SCHEMA = "data_factory.episode_ledger_state.v1"
EPISODE_REF_SCHEMA = "data_factory.episode_ref.v1"
LOCATOR_SCHEMA = "data_factory.lerobot_v3_episode_locator.v1"
CANDIDATE_SCHEMA = "data_factory.candidate_admission.v1"
DATA_QUALITY_SCHEMA = "data_factory.coverage_report.v1"
SYNTHETIC_ROLLOUT_SCHEMA = "data_factory.synthetic_rollout_failure_evidence.v1"
VIEW_SCHEMA = "data_factory.operator_session_view.v2"
INTENT_SCHEMA = "data_factory.operator_intent.v1"

RECOMMENDATION_FIELDS = frozenset({
    "schema_version", "recommendation_id", "input_snapshot", "claims",
    "suggested_draft_patches", "authority", "recommendation_digest",
})
SNAPSHOT_FIELDS = frozenset({
    "schema_version", "source_commit", "campaign", "episodes",
    "data_quality_analysis_ref", "rollout_evidence_analysis_ref",
    "snapshot_digest",
})
CAMPAIGN_FIELDS = frozenset({
    "schema_version", "manifest_id", "manifest_digest",
})
EPISODE_SNAPSHOT_FIELDS = frozenset({
    "manifest_order_index", "run_id", "episode_index", "dataset_id",
    "dataset_digest", "episode_ref", "locator", "ledger", "state",
    "candidate", "source_provenance_digest", "recording_quality_digest",
})
SCHEMA_DIGEST_FIELDS = frozenset({"schema_version", "digest"})
ANALYSIS_REF_FIELDS = frozenset({
    "availability", "schema_version", "analysis_id", "analysis_digest",
    "reason_codes",
})
CLAIM_FIELDS = frozenset({
    "claim_id", "class", "subject", "value", "evidence_refs",
    "basis_claim_ids", "reason_codes",
})
PATCH_FIELDS = frozenset({"change_id", "field", "value", "basis_claim_ids"})
AUTHORITY_FIELDS = frozenset({
    "recommendation", "dataset_mutation", "candidate_mutation",
    "ledger_mutation", "training_authorization", "motion_authority",
    "gate_bypass", "plan_compile", "campaign_authorization",
})
AUTHORITY = {
    "recommendation": "ADVISORY_ONLY",
    "dataset_mutation": False,
    "candidate_mutation": False,
    "ledger_mutation": False,
    "training_authorization": False,
    "motion_authority": False,
    "gate_bypass": False,
    "plan_compile": False,
    "campaign_authorization": False,
}
MANIFEST_FIELDS = frozenset({
    "schema_version", "manifest_id", "kind", "hypothesis_digest",
    "fixed_contract_digest", "catalog_digest", "coverage_digest", "selector",
    "selector_version", "normalized_seed", "slots", "manifest_budget",
    "program_budget", "planned_usage", "authority", "manifest_digest",
    "state_space_design_profile",
})
SLOT_FIELDS = frozenset({
    "slot_id", "base_condition_digest", "robot_start_pose_id", "split_group",
    "repeat_index", "hil_prompts", "reviews", "pending_reviews",
    "storage_bytes", "order_index",
})
EVIDENCE_FIELDS = frozenset({
    "manifest_order_index", "run", "ledger", "state", "candidate",
    "source_provenance", "recording_quality",
})
RUN_FIELDS = frozenset({
    "schema_version", "run_id", "transaction_id", "episode_index", "state",
    "reason_code", "rows", "detail",
})
LEDGER_FIELDS = frozenset({
    "schema_version", "dataset", "episode", "bindings", "artifacts",
    "admission", "ledger_digest",
})
DATASET_FIELDS = frozenset({
    "dataset_id", "repo_id", "dataset_root", "dataset_digest",
})
EPISODE_FIELDS = frozenset({
    "run_id", "episode_index", "transaction_id", "episode_ref",
    "episode_ref_digest", "lerobot_v3_locator",
})
EPISODE_REF_FIELDS = frozenset({
    "schema_version", "repo_id", "episode_index", "transaction_id",
    "resolved_job_digest", "staging_manifest_digest",
})
LOCATOR_FIELDS = frozenset({
    "schema_version", "repo_id", "episode_index", "data", "videos",
    "locator_digest",
})
DATA_LOCATOR_FIELDS = frozenset({
    "chunk_index", "file_index", "relative_path", "file_row_start",
    "file_row_end_exclusive",
})
VIDEO_LOCATOR_FIELDS = frozenset({
    "camera_key", "chunk_index", "file_index", "relative_path",
    "file_frame_start", "file_frame_end_exclusive", "timestamp_start_s",
    "timestamp_end_s",
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
ARTIFACT_NAMES = frozenset({
    "episode", "run", "staging_manifest", "manifest", "intent", "plan",
    "technical", "source_provenance", "recording_quality", "execution",
    "runtime_binding",
})
ARTIFACT_REF_FIELDS = frozenset({"artifact_path", "artifact_digest"})
STATE_FIELDS = frozenset({
    "schema_version", "ledger_digest", "episode_ref_digest", "candidate",
    "review", "retention", "state_digest",
})
REVIEW_FIELDS = frozenset({
    "semantic_status", "reviewed_by", "reviewed_at", "reason",
    "training_status",
})
RETENTION_FIELDS = frozenset({
    "retention_state", "reclaim_state", "physical_deletion", "storage_layout",
})
CANDIDATE_FIELDS = frozenset({
    "schema_version", "run_id", "operational_gate", "operational_source",
    "checklist_id", "semantic_status", "reviewed_by", "reviewed_at", "reason",
    "review_context_digest",
})
VIEW_FIELDS = frozenset({
    "schema_version", "session_id", "revision", "projection", "generated_at",
    "view_digest", "authority",
})
VIEW_AUTHORITY = {
    "browser": "INTENT_ONLY",
    "lifecycle_owner": "BACKEND",
    "human_identity": "NOT_AUTHENTICATED",
    "training_approval": "SEPARATE",
}
CLAIM_CLASSES = frozenset({"OBSERVED", "SUGGESTED", "UNKNOWN"})
CLAIM_SUBJECTS = frozenset({
    "person", "background", "robot", "coverage", "quality", "rollout",
})
PATCH_FIELDS_ALLOWLIST = frozenset({
    "requested_count", "repeat", "split", "selection",
    "state_space_design_factors",
})
FORBIDDEN_ACTIONS = frozenset({
    "compile_draft", "authorize_campaign", "approve_exact_plan", "motion",
    "record", "delete", "promote", "train",
})
FORBIDDEN_ASSERTION_KEYS = frozenset({
    "causal", "cause", "causes", "benefit", "outcome", "success",
    "scene_truth", "semantic_pass", "training_approved", "authority",
    "dataset_mutation", "candidate_mutation", "ledger_mutation",
    "training_authorization", "motion_authority", "recording_authority",
    "gate_bypass", "plan_compile", "campaign_authorization",
    "compile_draft", "authorize_campaign", "approve_exact_plan", "motion",
    "record", "delete", "promote", "train",
})
_MISSING = object()


def _exact(value: object, fields: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(code)
    return copy.deepcopy(dict(value))


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _count(value: object, code: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ContractError(code)
    return value


def _strings(
    value: object, code: str, *, nonempty: bool = False, identifiers: bool = True,
) -> list[str]:
    if (
        not isinstance(value, list)
        or nonempty and not value
        or any(
            not isinstance(item, str)
            or not item
            or identifiers and SAFE_ID.fullmatch(item) is None
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise ContractError(code)
    return sorted(value)


def _self_digest(value: Mapping[str, Any], field: str, code: str) -> str:
    expected = _digest(value.get(field), code)
    if canonical_digest({key: item for key, item in value.items() if key != field}) != expected:
        raise ContractError(code)
    return expected


def _forbidden(value: object) -> bool:
    if isinstance(value, Mapping):
        if any(str(key).lower() in FORBIDDEN_ASSERTION_KEYS for key in value):
            return True
        return any(_forbidden(item) for item in value.values())
    if isinstance(value, list):
        return any(_forbidden(item) for item in value)
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    if lowered in FORBIDDEN_ACTIONS:
        return True
    words = {
        word for word in lowered.replace("-", "_").replace(" ", "_").split("_")
        if word
    }
    return bool(words & {"causal", "cause", "causes", "benefit", "outcome", "success", "improve", "improves"})


def _manifest(value: object) -> dict[str, Any]:
    manifest = _exact(value, MANIFEST_FIELDS, "COLLECTION_RECOMMENDATION_MANIFEST_FIELDS")
    if (
        manifest["schema_version"] != MANIFEST_SCHEMA
        or manifest["kind"] != "collection"
        or manifest["authority"] != "NO_EXECUTION_AUTHORITY"
        or manifest["selector"] not in {"BALANCED_INITIAL", "DIRECT_LIST"}
        or manifest["selector_version"] != "campaign-selector-v1"
        or type(manifest["normalized_seed"]) is not int
        or manifest["normalized_seed"] < 0
        or not isinstance(manifest["slots"], list)
        or not manifest["slots"]
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_MANIFEST")
    _identifier(manifest["manifest_id"], "COLLECTION_RECOMMENDATION_MANIFEST_ID")
    for field in (
        "hypothesis_digest", "fixed_contract_digest", "catalog_digest",
        "coverage_digest",
    ):
        _digest(manifest[field], "COLLECTION_RECOMMENDATION_MANIFEST_DIGEST")
    slots = []
    identities = set()
    for index, raw in enumerate(manifest["slots"]):
        slot = _exact(raw, SLOT_FIELDS, "COLLECTION_RECOMMENDATION_SLOT_FIELDS")
        if slot["order_index"] != index or slot["split_group"] not in {"TRAIN", "ID", "OOD"}:
            raise ContractError("COLLECTION_RECOMMENDATION_SLOT_ORDER")
        _identifier(slot["slot_id"], "COLLECTION_RECOMMENDATION_SLOT_ID")
        _identifier(slot["robot_start_pose_id"], "COLLECTION_RECOMMENDATION_SLOT_ID")
        _digest(slot["base_condition_digest"], "COLLECTION_RECOMMENDATION_SLOT_DIGEST")
        for field in ("repeat_index", "hil_prompts", "reviews", "pending_reviews"):
            _count(slot[field], "COLLECTION_RECOMMENDATION_SLOT_COUNT")
        _count(slot["storage_bytes"], "COLLECTION_RECOMMENDATION_SLOT_COUNT", positive=True)
        identity = slot["slot_id"]
        if identity in identities:
            raise ContractError("COLLECTION_RECOMMENDATION_SLOT_DUPLICATE")
        identities.add(identity)
        slots.append(slot)
    manifest["slots"] = slots
    manifest_budget = _manifest_budget(manifest["manifest_budget"])
    program_budget = validate_program_budget(manifest["program_budget"])
    planned_usage = _usage("seed", slots)
    if manifest["planned_usage"] != planned_usage:
        raise ContractError("COLLECTION_RECOMMENDATION_MANIFEST_USAGE")
    _check_budgets(manifest_budget, program_budget, planned_usage)
    manifest["manifest_budget"] = manifest_budget
    manifest["program_budget"] = program_budget
    manifest["planned_usage"] = planned_usage
    manifest["state_space_design_profile"] = validate_state_space_design_profile(
        manifest["state_space_design_profile"],
    )
    _self_digest(manifest, "manifest_digest", "COLLECTION_RECOMMENDATION_MANIFEST_DIGEST")
    return manifest


def _artifact_refs(value: object) -> dict[str, dict[str, str]]:
    refs = _exact(value, ARTIFACT_NAMES, "COLLECTION_RECOMMENDATION_ARTIFACTS")
    for name, raw in refs.items():
        ref = _exact(raw, ARTIFACT_REF_FIELDS, "COLLECTION_RECOMMENDATION_ARTIFACT_REF")
        if not isinstance(ref["artifact_path"], str) or not ref["artifact_path"] or "\x00" in ref["artifact_path"]:
            raise ContractError("COLLECTION_RECOMMENDATION_ARTIFACT_REF")
        _digest(ref["artifact_digest"], "COLLECTION_RECOMMENDATION_ARTIFACT_REF")
        refs[name] = ref
    return refs


def _locator_path(
    value: object, *, kind: str, chunk_index: int, file_index: int,
    camera_key: str | None = None,
) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ContractError("COLLECTION_RECOMMENDATION_LOCATOR_PATH")
    path = PurePosixPath(value)
    expected = (
        PurePosixPath("data") / f"chunk-{chunk_index:03d}" / f"file-{file_index:03d}.parquet"
        if kind == "data"
        else PurePosixPath("videos") / str(camera_key) / f"chunk-{chunk_index:03d}" / f"file-{file_index:03d}.mp4"
    )
    if (
        path.is_absolute() or str(path) != value or ".." in path.parts
        or len(path.parts) < 3 or path.parts[-2:] != expected.parts[-2:]
        or kind == "video" and camera_key not in path.parts[:-2]
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_LOCATOR_PATH")
    return value


def _locator(value: object, *, repo_id: str, episode_index: int, rows: int) -> dict[str, Any]:
    locator = _exact(value, LOCATOR_FIELDS, "COLLECTION_RECOMMENDATION_LOCATOR_FIELDS")
    if (
        locator["schema_version"] != LOCATOR_SCHEMA
        or locator["repo_id"] != repo_id
        or locator["episode_index"] != episode_index
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_LOCATOR_BINDING")
    data = _exact(locator["data"], DATA_LOCATOR_FIELDS, "COLLECTION_RECOMMENDATION_DATA_LOCATOR")
    for field in ("chunk_index", "file_index", "file_row_start", "file_row_end_exclusive"):
        _count(data[field], "COLLECTION_RECOMMENDATION_DATA_LOCATOR")
    if data["file_row_end_exclusive"] - data["file_row_start"] != rows:
        raise ContractError("COLLECTION_RECOMMENDATION_DATA_LOCATOR")
    data["relative_path"] = _locator_path(
        data["relative_path"], kind="data", chunk_index=data["chunk_index"],
        file_index=data["file_index"],
    )
    videos = locator["videos"]
    if not isinstance(videos, list) or not videos:
        raise ContractError("COLLECTION_RECOMMENDATION_VIDEO_LOCATOR")
    normalized_videos = []
    for raw in videos:
        video = _exact(raw, VIDEO_LOCATOR_FIELDS, "COLLECTION_RECOMMENDATION_VIDEO_LOCATOR")
        _identifier(video["camera_key"], "COLLECTION_RECOMMENDATION_VIDEO_LOCATOR")
        for field in (
            "chunk_index", "file_index", "file_frame_start",
            "file_frame_end_exclusive",
        ):
            _count(video[field], "COLLECTION_RECOMMENDATION_VIDEO_LOCATOR")
        if video["file_frame_end_exclusive"] - video["file_frame_start"] != rows:
            raise ContractError("COLLECTION_RECOMMENDATION_VIDEO_LOCATOR")
        video["relative_path"] = _locator_path(
            video["relative_path"], kind="video",
            chunk_index=video["chunk_index"], file_index=video["file_index"],
            camera_key=video["camera_key"],
        )
        for field in ("timestamp_start_s", "timestamp_end_s"):
            if isinstance(video[field], bool) or not isinstance(video[field], (int, float)):
                raise ContractError("COLLECTION_RECOMMENDATION_VIDEO_LOCATOR")
        if video["timestamp_start_s"] < 0 or video["timestamp_end_s"] <= video["timestamp_start_s"]:
            raise ContractError("COLLECTION_RECOMMENDATION_VIDEO_LOCATOR")
        normalized_videos.append(video)
    cameras = [video["camera_key"] for video in normalized_videos]
    if cameras != sorted(cameras) or len(cameras) != len(set(cameras)):
        raise ContractError("COLLECTION_RECOMMENDATION_VIDEO_LOCATOR")
    locator["data"], locator["videos"] = data, normalized_videos
    _self_digest(locator, "locator_digest", "COLLECTION_RECOMMENDATION_LOCATOR_DIGEST")
    return locator


def _episode_snapshot(value: object, manifest: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _exact(value, EVIDENCE_FIELDS, "COLLECTION_RECOMMENDATION_EVIDENCE_FIELDS")
    order_index = _count(evidence["manifest_order_index"], "COLLECTION_RECOMMENDATION_EVIDENCE_ORDER")
    if order_index >= len(manifest["slots"]):
        raise ContractError("COLLECTION_RECOMMENDATION_EVIDENCE_ORDER")
    slot = manifest["slots"][order_index]

    run = _exact(evidence["run"], RUN_FIELDS, "COLLECTION_RECOMMENDATION_RUN_FIELDS")
    run_id = _identifier(run["run_id"], "COLLECTION_RECOMMENDATION_RUN_ID")
    episode_index = _count(run["episode_index"], "COLLECTION_RECOMMENDATION_EPISODE_INDEX")
    rows = _count(run["rows"], "COLLECTION_RECOMMENDATION_RUN_ROWS", positive=True)
    if (
        run["schema_version"] != RUN_SCHEMA
        or run["state"] != "COMMITTED"
        or run["reason_code"] != "COMMITTED"
        or run["transaction_id"] != f"{run_id}:episode-{episode_index:06d}"
        or not isinstance(run["detail"], str)
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_RUN_BINDING")

    ledger = _exact(evidence["ledger"], LEDGER_FIELDS, "COLLECTION_RECOMMENDATION_LEDGER_FIELDS")
    if ledger["schema_version"] != LEDGER_SCHEMA:
        raise ContractError("COLLECTION_RECOMMENDATION_LEDGER_SCHEMA")
    _self_digest(ledger, "ledger_digest", "COLLECTION_RECOMMENDATION_LEDGER_DIGEST")
    dataset = _exact(ledger["dataset"], DATASET_FIELDS, "COLLECTION_RECOMMENDATION_DATASET_FIELDS")
    _identifier(dataset["dataset_id"], "COLLECTION_RECOMMENDATION_DATASET_ID")
    if any(not isinstance(dataset[field], str) or not dataset[field] for field in ("repo_id", "dataset_root")):
        raise ContractError("COLLECTION_RECOMMENDATION_DATASET")

    episode = _exact(ledger["episode"], EPISODE_FIELDS, "COLLECTION_RECOMMENDATION_EPISODE_FIELDS")
    ref = _exact(episode["episode_ref"], EPISODE_REF_FIELDS, "COLLECTION_RECOMMENDATION_EPISODE_REF_FIELDS")
    if (
        ref["schema_version"] != EPISODE_REF_SCHEMA
        or ref["repo_id"] != dataset["repo_id"]
        or ref["episode_index"] != episode_index
        or ref["transaction_id"] != run["transaction_id"]
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_REF_BINDING")
    for field in ("resolved_job_digest", "staging_manifest_digest"):
        _digest(ref[field], "COLLECTION_RECOMMENDATION_EPISODE_REF_DIGEST")
    ref_digest = canonical_digest(ref)
    if (
        episode["run_id"] != run_id
        or episode["episode_index"] != episode_index
        or episode["transaction_id"] != run["transaction_id"]
        or episode["episode_ref_digest"] != ref_digest
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_BINDING")
    expected_dataset_digest = canonical_digest({
        "repo_id": dataset["repo_id"],
        "dataset_root": dataset["dataset_root"],
        "episode_ref": ref,
    })
    if (
        dataset["dataset_digest"] != expected_dataset_digest
        or dataset["dataset_id"] != f"dataset-{expected_dataset_digest[7:23]}"
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_DATASET_DIGEST")
    locator = _locator(
        episode["lerobot_v3_locator"], repo_id=dataset["repo_id"],
        episode_index=episode_index, rows=rows,
    )

    bindings = _exact(ledger["bindings"], BINDING_FIELDS, "COLLECTION_RECOMMENDATION_BINDING_FIELDS")
    for field, item in bindings.items():
        if field == "robot_start_pose_id":
            _identifier(item, "COLLECTION_RECOMMENDATION_BINDING")
        else:
            _digest(item, "COLLECTION_RECOMMENDATION_BINDING")
    if (
        bindings["resolved_job_digest"] != ref["resolved_job_digest"]
        or bindings["manifest_digest"] != manifest["manifest_digest"]
        or bindings["slot_digest"] != canonical_digest(slot)
        or bindings["base_condition_digest"] != slot["base_condition_digest"]
        or bindings["robot_start_pose_id"] != slot["robot_start_pose_id"]
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_SLOT_BINDING")
    admission = _exact(ledger["admission"], ADMISSION_FIELDS, "COLLECTION_RECOMMENDATION_ADMISSION_FIELDS")
    if admission["technical_status"] != "PASS" or admission["training_status"] != "NOT_AUTHORIZED":
        raise ContractError("COLLECTION_RECOMMENDATION_ADMISSION")
    _digest(admission["review_context_digest"], "COLLECTION_RECOMMENDATION_ADMISSION")
    artifacts = _artifact_refs(ledger["artifacts"])
    if (
        artifacts["manifest"]["artifact_digest"] != canonical_digest(manifest)
        or artifacts["run"]["artifact_digest"] != canonical_digest(run)
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_ARTIFACT_BINDING")

    candidate = _exact(evidence["candidate"], CANDIDATE_FIELDS, "COLLECTION_RECOMMENDATION_CANDIDATE_FIELDS")
    if (
        candidate["schema_version"] != CANDIDATE_SCHEMA
        or candidate["run_id"] != run_id
        or candidate["operational_gate"] != "PASS"
        or candidate["operational_source"] not in {"HIL_PROXY", "HUMAN_GATED"}
        or candidate["review_context_digest"] != admission["review_context_digest"]
        or candidate["semantic_status"] not in {"PENDING", "PASS", "FAIL", "UNCERTAIN"}
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_CANDIDATE_BINDING")
    _identifier(candidate["checklist_id"], "COLLECTION_RECOMMENDATION_CANDIDATE")
    status = candidate["semantic_status"]
    if (
        status == "PENDING" and any(candidate[field] is not None for field in ("reviewed_by", "reviewed_at", "reason"))
        or status != "PENDING" and (
            not isinstance(candidate["reviewed_by"], str)
            or SAFE_ID.fullmatch(candidate["reviewed_by"]) is None
            or candidate["reviewed_by"] == "HUMAN"
            or not isinstance(candidate["reviewed_at"], str)
            or RFC3339.fullmatch(candidate["reviewed_at"]) is None
            or status == "PASS" and candidate["reason"] is not None
            or status != "PASS" and (not isinstance(candidate["reason"], str) or not candidate["reason"])
        )
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_CANDIDATE_STATE")
    candidate_digest = canonical_digest(candidate)

    state = _exact(evidence["state"], STATE_FIELDS, "COLLECTION_RECOMMENDATION_STATE_FIELDS")
    if state["schema_version"] != STATE_SCHEMA:
        raise ContractError("COLLECTION_RECOMMENDATION_STATE_SCHEMA")
    _self_digest(state, "state_digest", "COLLECTION_RECOMMENDATION_STATE_DIGEST")
    candidate_ref = _exact(state["candidate"], ARTIFACT_REF_FIELDS, "COLLECTION_RECOMMENDATION_CANDIDATE_REF")
    if (
        not isinstance(candidate_ref["artifact_path"], str)
        or not candidate_ref["artifact_path"]
        or "\x00" in candidate_ref["artifact_path"]
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_CANDIDATE_REF")
    _digest(
        candidate_ref["artifact_digest"],
        "COLLECTION_RECOMMENDATION_CANDIDATE_REF",
    )
    review = _exact(state["review"], REVIEW_FIELDS, "COLLECTION_RECOMMENDATION_REVIEW_FIELDS")
    retention = _exact(state["retention"], RETENTION_FIELDS, "COLLECTION_RECOMMENDATION_RETENTION_FIELDS")
    if (
        state["ledger_digest"] != ledger["ledger_digest"]
        or state["episode_ref_digest"] != ref_digest
        or candidate_ref["artifact_digest"] != candidate_digest
        or review != {
            "semantic_status": candidate["semantic_status"],
            "reviewed_by": candidate["reviewed_by"],
            "reviewed_at": candidate["reviewed_at"],
            "reason": candidate["reason"],
            "training_status": "NOT_AUTHORIZED",
        }
        or retention["retention_state"] != "PRESERVE"
        or retention["reclaim_state"] not in {"NOT_EVALUATED", "REPACK_REQUIRED"}
        or retention["physical_deletion"] != "NOT_AUTHORIZED"
        or retention["storage_layout"] != "SHARED_CHUNK"
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_STATE_BINDING")

    provenance = evidence["source_provenance"]
    quality = evidence["recording_quality"]
    if (
        not isinstance(provenance, list)
        or len(provenance) != rows
        or any(not isinstance(row, Mapping) or row.get("frame_index") != index for index, row in enumerate(provenance))
        or not isinstance(quality, Mapping)
        or quality.get("episode_index") != episode_index
        or quality.get("frames") != rows
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_RECORDING_BINDING")
    provenance_digest = canonical_digest(provenance)
    quality_digest = canonical_digest(quality)
    if (
        artifacts["source_provenance"]["artifact_digest"] != provenance_digest
        or artifacts["recording_quality"]["artifact_digest"] != quality_digest
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_RECORDING_DIGEST")

    return {
        "manifest_order_index": order_index,
        "run_id": run_id,
        "episode_index": episode_index,
        "dataset_id": dataset["dataset_id"],
        "dataset_digest": dataset["dataset_digest"],
        "episode_ref": {"schema_version": EPISODE_REF_SCHEMA, "digest": ref_digest},
        "locator": {"schema_version": LOCATOR_SCHEMA, "digest": locator["locator_digest"]},
        "ledger": {"schema_version": LEDGER_SCHEMA, "digest": ledger["ledger_digest"]},
        "state": {"schema_version": STATE_SCHEMA, "digest": state["state_digest"]},
        "candidate": {"schema_version": CANDIDATE_SCHEMA, "digest": candidate_digest},
        "source_provenance_digest": provenance_digest,
        "recording_quality_digest": quality_digest,
    }


def _episode_summaries(values: object, manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
        raise ContractError("COLLECTION_RECOMMENDATION_EPISODES")
    episodes = sorted(
        (_episode_snapshot(item, manifest) for item in values),
        key=lambda item: item["manifest_order_index"],
    )
    if [item["manifest_order_index"] for item in episodes] != list(range(len(episodes))):
        raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_ORDER")
    unique_fields = (
        "run_id", "dataset_digest", "source_provenance_digest",
        "recording_quality_digest",
    )
    if any(len({item[field] for item in episodes}) != len(episodes) for field in unique_fields):
        raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_DUPLICATE")
    for nested in ("episode_ref", "locator", "ledger", "state", "candidate"):
        if len({item[nested]["digest"] for item in episodes}) != len(episodes):
            raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_DUPLICATE")
    return episodes


def _analysis_digest(artifact: Mapping[str, Any]) -> str:
    if "analysis_digest" not in artifact:
        return canonical_digest(artifact)
    expected = _digest(artifact["analysis_digest"], "COLLECTION_RECOMMENDATION_ANALYSIS_DIGEST")
    if canonical_digest({key: item for key, item in artifact.items() if key != "analysis_digest"}) != expected:
        raise ContractError("COLLECTION_RECOMMENDATION_ANALYSIS_DIGEST")
    return expected


def _contains_synthetic(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_synthetic(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_synthetic(item) for item in value)
    return value == "SYNTHETIC_TEST_ONLY" or value == SYNTHETIC_ROLLOUT_SCHEMA


def _analysis_ref(
    value: object, *, owner: str, artifact: object = _MISSING,
) -> dict[str, Any]:
    ref = _exact(value, ANALYSIS_REF_FIELDS, "COLLECTION_RECOMMENDATION_ANALYSIS_REF_FIELDS")
    reasons = _strings(ref["reason_codes"], "COLLECTION_RECOMMENDATION_ANALYSIS_REASONS")
    if ref["availability"] == "UNAVAILABLE":
        if (
            any(ref[field] is not None for field in ("schema_version", "analysis_id", "analysis_digest"))
            or not reasons
            or owner == "rollout"
            and "NO_CANONICAL_PHYSICAL_ROLLOUT_ANALYSIS" not in reasons
            or artifact is not _MISSING and artifact is not None
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_ANALYSIS_UNAVAILABLE")
        ref["reason_codes"] = reasons
        return ref
    if ref["availability"] != "AVAILABLE" or reasons:
        raise ContractError("COLLECTION_RECOMMENDATION_ANALYSIS_AVAILABILITY")
    schema = _identifier(ref["schema_version"], "COLLECTION_RECOMMENDATION_ANALYSIS_SCHEMA")
    analysis_id = _identifier(ref["analysis_id"], "COLLECTION_RECOMMENDATION_ANALYSIS_ID")
    expected_digest = _digest(ref["analysis_digest"], "COLLECTION_RECOMMENDATION_ANALYSIS_DIGEST")
    if owner == "data_quality" and schema != DATA_QUALITY_SCHEMA:
        raise ContractError("COLLECTION_RECOMMENDATION_DATA_QUALITY_OWNER")
    if owner == "rollout" and schema in {DATA_QUALITY_SCHEMA, SYNTHETIC_ROLLOUT_SCHEMA}:
        raise ContractError("COLLECTION_RECOMMENDATION_ROLLOUT_OWNER")
    if artifact is not _MISSING:
        if not isinstance(artifact, Mapping) or artifact.get("schema_version") != schema:
            raise ContractError("COLLECTION_RECOMMENDATION_ANALYSIS_ARTIFACT")
        if owner == "data_quality":
            if artifact.get("collection_profile_id") != analysis_id or artifact.get("authority") != "REPORT_ONLY":
                raise ContractError("COLLECTION_RECOMMENDATION_DATA_QUALITY_OWNER")
        elif (
            artifact.get("analysis_id") != analysis_id
            or artifact.get("evidence_scope") != "PHYSICAL"
            or _contains_synthetic(artifact)
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_ROLLOUT_PHYSICAL")
        if _analysis_digest(artifact) != expected_digest:
            raise ContractError("COLLECTION_RECOMMENDATION_ANALYSIS_DIGEST")
    ref["reason_codes"] = []
    return ref


def _analysis_refs(
    data_quality_ref: object, rollout_ref: object, *,
    data_quality_analysis: object = _MISSING,
    rollout_evidence_analysis: object = _MISSING,
) -> tuple[dict[str, Any], dict[str, Any]]:
    data_quality = _analysis_ref(
        data_quality_ref, owner="data_quality", artifact=data_quality_analysis,
    )
    rollout = _analysis_ref(
        rollout_ref, owner="rollout", artifact=rollout_evidence_analysis,
    )
    if data_quality["availability"] == rollout["availability"] == "AVAILABLE" and (
        data_quality["analysis_id"] == rollout["analysis_id"]
        or data_quality["analysis_digest"] == rollout["analysis_digest"]
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_ANALYSIS_ALIAS")
    return data_quality, rollout


def _known_evidence(snapshot: Mapping[str, Any]) -> set[str]:
    result = {snapshot["campaign"]["manifest_digest"]}
    for episode in snapshot["episodes"]:
        result.update({
            episode["dataset_digest"], episode["source_provenance_digest"],
            episode["recording_quality_digest"],
            *(episode[name]["digest"] for name in ("episode_ref", "locator", "ledger", "state", "candidate")),
        })
    for name in ("data_quality_analysis_ref", "rollout_evidence_analysis_ref"):
        if snapshot[name]["availability"] == "AVAILABLE":
            result.add(snapshot[name]["analysis_digest"])
    return result


def _claims(values: object, snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
        raise ContractError("COLLECTION_RECOMMENDATION_CLAIMS")
    known_evidence = _known_evidence(snapshot)
    result = []
    for raw in values:
        claim = _exact(raw, CLAIM_FIELDS, "COLLECTION_RECOMMENDATION_CLAIM_FIELDS")
        _identifier(claim["claim_id"], "COLLECTION_RECOMMENDATION_CLAIM_ID")
        if claim["class"] not in CLAIM_CLASSES or claim["subject"] not in CLAIM_SUBJECTS:
            raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_TYPE")
        evidence = _strings(
            claim["evidence_refs"], "COLLECTION_RECOMMENDATION_CLAIM_EVIDENCE",
            identifiers=False,
        )
        if any(_digest(item, "COLLECTION_RECOMMENDATION_CLAIM_EVIDENCE") not in known_evidence for item in evidence):
            raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_EVIDENCE")
        basis = _strings(claim["basis_claim_ids"], "COLLECTION_RECOMMENDATION_CLAIM_BASIS")
        reasons = _strings(claim["reason_codes"], "COLLECTION_RECOMMENDATION_CLAIM_REASONS")
        if (
            claim["class"] == "OBSERVED" and (claim["value"] is None or not evidence or basis)
            or claim["class"] == "OBSERVED"
            and claim["subject"] != "rollout" and _forbidden(claim["value"])
            or claim["class"] == "SUGGESTED"
            and (claim["value"] is None or not basis or _forbidden(claim["value"]))
            or claim["class"] == "UNKNOWN" and (claim["value"] is not None or basis or not reasons)
            or claim["subject"] in {"person", "background"} and claim["class"] != "UNKNOWN"
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_EPISTEMIC")
        canonical_digest(claim["value"])
        claim.update(evidence_refs=evidence, basis_claim_ids=basis, reason_codes=reasons)
        result.append(claim)
    result.sort(key=lambda item: item["claim_id"])
    ids = [claim["claim_id"] for claim in result]
    if len(ids) != len(set(ids)):
        raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_DUPLICATE")
    by_id = {claim["claim_id"]: claim for claim in result}
    for claim in result:
        if any(
            basis_id == claim["claim_id"]
            or basis_id not in by_id
            or by_id[basis_id]["class"] == "SUGGESTED"
            for basis_id in claim["basis_claim_ids"]
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_BASIS")
    for subject in ("person", "background", "robot"):
        matches = [claim for claim in result if claim["subject"] == subject]
        if len(matches) != 1:
            raise ContractError("COLLECTION_RECOMMENDATION_NUISANCE_CLAIM")
    rollout_ref = snapshot["rollout_evidence_analysis_ref"]
    if any(
        claim["subject"] == "rollout" and claim["class"] == "OBSERVED"
        and (
            rollout_ref["availability"] != "AVAILABLE"
            or rollout_ref["analysis_digest"] not in claim["evidence_refs"]
        )
        for claim in result
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_ROLLOUT_EVIDENCE")
    if rollout_ref["availability"] == "UNAVAILABLE":
        rollout_unknowns = [
            claim for claim in result
            if claim["subject"] == "rollout" and claim["class"] == "UNKNOWN"
            and "NO_CANONICAL_PHYSICAL_ROLLOUT_ANALYSIS" in claim["reason_codes"]
        ]
        if not rollout_unknowns or any(
            claim["subject"] == "rollout" and claim["class"] != "UNKNOWN"
            for claim in result
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_ROLLOUT_UNKNOWN")
    robot = next(claim for claim in result if claim["subject"] == "robot")
    if robot["class"] == "SUGGESTED":
        raise ContractError("COLLECTION_RECOMMENDATION_NUISANCE_CLAIM")
    if robot["class"] == "OBSERVED" and not any(
        claim["class"] == "UNKNOWN"
        and "ROBOT_VARIATION_UNMEASURED" in claim["reason_codes"]
        for claim in result
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_NUISANCE_CLAIM")
    return result


def _patch_value(field: str, value: object) -> Any:
    if field in {"requested_count", "repeat"}:
        if type(value) is not int or not 1 <= value <= 100:
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_VALUE")
    elif field == "split":
        if value not in {"TRAIN", "ID", "OOD"}:
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_VALUE")
    elif field == "selection":
        if not isinstance(value, Mapping) or len(value) != 1:
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_VALUE")
        axis, selected = next(iter(value.items()))
        _identifier(axis, "COLLECTION_RECOMMENDATION_PATCH_VALUE")
        _identifier(selected, "COLLECTION_RECOMMENDATION_PATCH_VALUE")
    else:
        factors = _exact(
            value, frozenset({"columns", "rows", "yaw_cdf_strata"}),
            "COLLECTION_RECOMMENDATION_PATCH_VALUE",
        )
        columns, rows, yaw = factors["columns"], factors["rows"], factors["yaw_cdf_strata"]
        if (
            type(columns) is not int or type(rows) is not int or type(yaw) is not int
            or not 1 <= columns <= 100 or not 1 <= rows <= 100
            or columns * rows > 100 or not 1 <= yaw <= columns * rows
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_VALUE")
        value = factors
    if _forbidden(value):
        raise ContractError("COLLECTION_RECOMMENDATION_PATCH_AUTHORITY")
    canonical_digest(value)
    return copy.deepcopy(value)


def _patches(values: object, claims: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ContractError("COLLECTION_RECOMMENDATION_PATCHES")
    by_id = {claim["claim_id"]: claim for claim in claims}
    result = []
    for raw in values:
        patch = _exact(raw, PATCH_FIELDS, "COLLECTION_RECOMMENDATION_PATCH_FIELDS")
        _identifier(patch["change_id"], "COLLECTION_RECOMMENDATION_PATCH_ID")
        if patch["field"] not in PATCH_FIELDS_ALLOWLIST:
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_FIELD")
        basis = _strings(
            patch["basis_claim_ids"], "COLLECTION_RECOMMENDATION_PATCH_BASIS",
            nonempty=True,
        )
        if any(item not in by_id for item in basis):
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_BASIS")
        if any(
            by_id[item]["subject"] == "rollout"
            and by_id[item]["class"] == "UNKNOWN"
            for item in basis
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_CAUSAL")
        patch["value"] = _patch_value(patch["field"], patch["value"])
        patch["basis_claim_ids"] = basis
        result.append(patch)
    result.sort(key=lambda item: item["change_id"])
    ids = [patch["change_id"] for patch in result]
    if len(ids) != len(set(ids)):
        raise ContractError("COLLECTION_RECOMMENDATION_PATCH_DUPLICATE")
    return result


def _authority(value: object) -> dict[str, Any]:
    authority = _exact(value, AUTHORITY_FIELDS, "COLLECTION_RECOMMENDATION_AUTHORITY_FIELDS")
    if authority["recommendation"] != "ADVISORY_ONLY" or any(
        type(authority[field]) is not bool or authority[field] is not False
        for field in AUTHORITY_FIELDS - {"recommendation"}
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_AUTHORITY")
    return authority


def build_collection_recommendation(
    *, recommendation_id: str, source_commit: str,
    campaign_manifest: Mapping[str, Any], episode_evidence: Sequence[Mapping[str, Any]],
    data_quality_analysis_ref: Mapping[str, Any],
    rollout_evidence_analysis_ref: Mapping[str, Any], claims: Sequence[Mapping[str, Any]],
    suggested_draft_patches: Sequence[Mapping[str, Any]],
    data_quality_analysis: Mapping[str, Any] | None = None,
    rollout_evidence_analysis: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one canonical recommendation from already-loaded evidence."""
    _identifier(recommendation_id, "COLLECTION_RECOMMENDATION_ID")
    if (
        not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_SOURCE_COMMIT")
    manifest = _manifest(campaign_manifest)
    episodes = _episode_summaries(episode_evidence, manifest)
    data_quality_ref, rollout_ref = _analysis_refs(
        data_quality_analysis_ref, rollout_evidence_analysis_ref,
        data_quality_analysis=data_quality_analysis,
        rollout_evidence_analysis=rollout_evidence_analysis,
    )
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA,
        "source_commit": source_commit,
        "campaign": {
            "schema_version": MANIFEST_SCHEMA,
            "manifest_id": manifest["manifest_id"],
            "manifest_digest": manifest["manifest_digest"],
        },
        "episodes": episodes,
        "data_quality_analysis_ref": data_quality_ref,
        "rollout_evidence_analysis_ref": rollout_ref,
    }
    snapshot["snapshot_digest"] = canonical_digest(snapshot)
    checked_claims = _claims(claims, snapshot)
    value = {
        "schema_version": SCHEMA_VERSION,
        "recommendation_id": recommendation_id,
        "input_snapshot": snapshot,
        "claims": checked_claims,
        "suggested_draft_patches": _patches(suggested_draft_patches, checked_claims),
        "authority": copy.deepcopy(AUTHORITY),
    }
    value["recommendation_digest"] = canonical_digest(value)
    return validate_collection_recommendation(value)


def _snapshot(value: object) -> dict[str, Any]:
    snapshot = _exact(value, SNAPSHOT_FIELDS, "COLLECTION_RECOMMENDATION_SNAPSHOT_FIELDS")
    if snapshot["schema_version"] != SNAPSHOT_SCHEMA:
        raise ContractError("COLLECTION_RECOMMENDATION_SNAPSHOT_SCHEMA")
    source_commit = snapshot["source_commit"]
    if (
        not isinstance(source_commit, str) or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_SOURCE_COMMIT")
    campaign = _exact(snapshot["campaign"], CAMPAIGN_FIELDS, "COLLECTION_RECOMMENDATION_CAMPAIGN_FIELDS")
    if campaign["schema_version"] != MANIFEST_SCHEMA:
        raise ContractError("COLLECTION_RECOMMENDATION_CAMPAIGN_SCHEMA")
    _identifier(campaign["manifest_id"], "COLLECTION_RECOMMENDATION_MANIFEST_ID")
    _digest(campaign["manifest_digest"], "COLLECTION_RECOMMENDATION_MANIFEST_DIGEST")
    episodes = snapshot["episodes"]
    if not isinstance(episodes, list) or not episodes:
        raise ContractError("COLLECTION_RECOMMENDATION_EPISODES")
    normalized_episodes = []
    for index, raw in enumerate(episodes):
        episode = _exact(raw, EPISODE_SNAPSHOT_FIELDS, "COLLECTION_RECOMMENDATION_EPISODE_SNAPSHOT_FIELDS")
        if episode["manifest_order_index"] != index:
            raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_ORDER")
        _identifier(episode["run_id"], "COLLECTION_RECOMMENDATION_RUN_ID")
        _count(episode["episode_index"], "COLLECTION_RECOMMENDATION_EPISODE_INDEX")
        _identifier(episode["dataset_id"], "COLLECTION_RECOMMENDATION_DATASET_ID")
        for field in ("dataset_digest", "source_provenance_digest", "recording_quality_digest"):
            _digest(episode[field], "COLLECTION_RECOMMENDATION_EPISODE_DIGEST")
        expected_schemas = {
            "episode_ref": EPISODE_REF_SCHEMA,
            "locator": LOCATOR_SCHEMA,
            "ledger": LEDGER_SCHEMA,
            "state": STATE_SCHEMA,
            "candidate": CANDIDATE_SCHEMA,
        }
        for name, schema in expected_schemas.items():
            nested = _exact(episode[name], SCHEMA_DIGEST_FIELDS, "COLLECTION_RECOMMENDATION_EPISODE_REF")
            if nested["schema_version"] != schema:
                raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_REF")
            _digest(nested["digest"], "COLLECTION_RECOMMENDATION_EPISODE_DIGEST")
            episode[name] = nested
        normalized_episodes.append(episode)
    unique_fields = (
        "run_id", "dataset_digest", "source_provenance_digest",
        "recording_quality_digest",
    )
    if any(
        len({item[field] for item in normalized_episodes}) != len(normalized_episodes)
        for field in unique_fields
    ) or any(
        len({item[name]["digest"] for item in normalized_episodes})
        != len(normalized_episodes)
        for name in ("episode_ref", "locator", "ledger", "state", "candidate")
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_DUPLICATE")
    snapshot["campaign"], snapshot["episodes"] = campaign, normalized_episodes
    data_quality_ref, rollout_ref = _analysis_refs(
        snapshot["data_quality_analysis_ref"], snapshot["rollout_evidence_analysis_ref"],
    )
    snapshot["data_quality_analysis_ref"] = data_quality_ref
    snapshot["rollout_evidence_analysis_ref"] = rollout_ref
    _self_digest(snapshot, "snapshot_digest", "COLLECTION_RECOMMENDATION_SNAPSHOT_DIGEST")
    return snapshot


def validate_collection_recommendation(
    value: object, *, campaign_manifest: Mapping[str, Any] | None = None,
    episode_evidence: Sequence[Mapping[str, Any]] | None = None,
    data_quality_analysis: Mapping[str, Any] | None = None,
    rollout_evidence_analysis: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a self-digested value, optionally rejoining supplied evidence."""
    recommendation = _exact(value, RECOMMENDATION_FIELDS, "COLLECTION_RECOMMENDATION_FIELDS")
    if recommendation["schema_version"] != SCHEMA_VERSION:
        raise ContractError("COLLECTION_RECOMMENDATION_SCHEMA")
    _identifier(recommendation["recommendation_id"], "COLLECTION_RECOMMENDATION_ID")
    snapshot = _snapshot(recommendation["input_snapshot"])
    claims = _claims(recommendation["claims"], snapshot)
    patches = _patches(recommendation["suggested_draft_patches"], claims)
    authority = _authority(recommendation["authority"])
    recommendation.update(
        input_snapshot=snapshot, claims=claims,
        suggested_draft_patches=patches, authority=authority,
    )
    _self_digest(
        recommendation, "recommendation_digest",
        "COLLECTION_RECOMMENDATION_DIGEST",
    )
    if (campaign_manifest is None) != (episode_evidence is None):
        raise ContractError("COLLECTION_RECOMMENDATION_EVIDENCE_REQUIRED")
    if campaign_manifest is not None and episode_evidence is not None:
        manifest = _manifest(campaign_manifest)
        episodes = _episode_summaries(episode_evidence, manifest)
        if snapshot["campaign"] != {
            "schema_version": MANIFEST_SCHEMA,
            "manifest_id": manifest["manifest_id"],
            "manifest_digest": manifest["manifest_digest"],
        } or snapshot["episodes"] != episodes:
            raise ContractError("COLLECTION_RECOMMENDATION_SNAPSHOT_BINDING")
        checked_refs = _analysis_refs(
            snapshot["data_quality_analysis_ref"],
            snapshot["rollout_evidence_analysis_ref"],
            data_quality_analysis=data_quality_analysis,
            rollout_evidence_analysis=rollout_evidence_analysis,
        )
        if checked_refs != (
            snapshot["data_quality_analysis_ref"],
            snapshot["rollout_evidence_analysis_ref"],
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_ANALYSIS_BINDING")
    elif data_quality_analysis is not None or rollout_evidence_analysis is not None:
        raise ContractError("COLLECTION_RECOMMENDATION_EVIDENCE_REQUIRED")
    return recommendation


def project_update_draft_intent(
    recommendation: object, *, selected_change_id: str,
    operator_view: Mapping[str, Any], intent_id: str | None = None,
) -> dict[str, Any]:
    """Return one update_draft intent; never consume or apply it."""
    checked = validate_collection_recommendation(recommendation)
    _identifier(selected_change_id, "COLLECTION_RECOMMENDATION_PATCH_SELECTION")
    selected = [
        patch for patch in checked["suggested_draft_patches"]
        if patch["change_id"] == selected_change_id
    ]
    if len(selected) != 1:
        raise ContractError("COLLECTION_RECOMMENDATION_PATCH_SELECTION")
    patch = selected[0]
    view = _exact(operator_view, VIEW_FIELDS, "COLLECTION_RECOMMENDATION_VIEW_FIELDS")
    projection = view["projection"]
    if (
        view["schema_version"] != VIEW_SCHEMA
        or not isinstance(projection, Mapping)
        or projection.get("workflow_state") != "AUTHORING"
        or not isinstance(projection.get("available_ops"), list)
        or projection["available_ops"].count("update_draft") != 1
        or view["authority"] != VIEW_AUTHORITY
        or type(view["revision"]) is not int
        or view["revision"] < 0
        or not isinstance(view["generated_at"], str)
        or RFC3339.fullmatch(view["generated_at"]) is None
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_VIEW_STATE")
    session_id = _identifier(view["session_id"], "COLLECTION_RECOMMENDATION_VIEW_SESSION")
    expected_view_digest = canonical_digest({
        "session_id": session_id,
        "revision": view["revision"],
        "projection": projection,
    })
    if view["view_digest"] != expected_view_digest:
        raise ContractError("COLLECTION_RECOMMENDATION_VIEW_STALE")
    draft = projection.get("draft")
    if not isinstance(draft, Mapping):
        raise ContractError("COLLECTION_RECOMMENDATION_VIEW_DRAFT")
    draft_id = _identifier(draft.get("draft_id"), "COLLECTION_RECOMMENDATION_VIEW_DRAFT")
    _count(draft.get("revision"), "COLLECTION_RECOMMENDATION_VIEW_DRAFT")
    if patch["field"] == "selection":
        axis, selected_value = next(iter(patch["value"].items()))
        catalog = projection.get("catalog")
        axes = catalog.get("axes") if isinstance(catalog, Mapping) else None
        options = axes.get(axis) if isinstance(axes, Mapping) else None
        if not isinstance(options, list) or not any(
            isinstance(option, Mapping)
            and option.get("id") == selected_value
            and option.get("available") is True
            for option in options
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_VALUE")
    if intent_id is None:
        intent_id = "recommendation-" + canonical_digest({
            "recommendation_digest": checked["recommendation_digest"],
            "change_id": selected_change_id,
            "view_digest": view["view_digest"],
        })[7:31]
    _identifier(intent_id, "COLLECTION_RECOMMENDATION_INTENT_ID")
    return {
        "schema_version": INTENT_SCHEMA,
        "intent_id": intent_id,
        "session_id": session_id,
        "view_revision": view["revision"],
        "view_digest": view["view_digest"],
        "op": "update_draft",
        "payload": {
            "draft_id": draft_id,
            patch["field"]: copy.deepcopy(patch["value"]),
        },
    }


__all__ = [
    "AUTHORITY", "SCHEMA_VERSION", "SNAPSHOT_SCHEMA",
    "build_collection_recommendation", "project_update_draft_intent",
    "validate_collection_recommendation",
]
