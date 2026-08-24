"""Offline episode training admission; no dataset or training mutation authority."""
from __future__ import annotations

import copy
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.data_factory.quality.coverage_report import CANDIDATE_FIELDS, TECHNICAL_FIELDS
from tools.fr5_data_factory import ContractError, DIGEST, RFC3339, SAFE_ID, canonical_digest, load_json_strict


APPROVAL_SCHEMA = "data_factory.training_approval.v2"
EPISODE_PROVENANCE_SCHEMA = "data_factory.episode_training_provenance.v1"
INVENTORY_SCHEMA = "data_factory.training_approved_inventory.v2"
PRODUCTION_SCOPE = "PRODUCTION"
SYNTHETIC_SCOPE = "SYNTHETIC_TEST_ONLY"
PROVENANCE = "HUMAN_TRAINING_APPROVED"

DATASET_KEYS = frozenset({"dataset_id", "repo_id", "dataset_root", "dataset_digest"})
APPROVAL_KEYS = frozenset({
    "schema_version", "scope", "dataset_identity", "episode_id", "episode_index",
    "episode_content_digest", "technical_validator_digest",
    "human_semantic_evidence_digest", "episode_provenance_digest",
    "approved_by", "approved_at", "provenance",
})
EPISODE_PROVENANCE_KEYS = frozenset({
    "schema_version", "scope", "dataset_identity_digest", "episode_id",
    "episode_index", "episode_content_digest", "technical_validator_digest",
    "resolved_job_digest", "seed_manifest_id", "seed_manifest_digest",
    "manifest_slot_id", "split_group", "repeat_index",
    "base_condition_digest", "robot_start_pose_id",
})
TECHNICAL_REF_KEYS = frozenset({"artifact_path", "artifact_digest", "status"})
SEMANTIC_REF_KEYS = frozenset({"artifact_path", "artifact_digest", "status", "reviewer_id"})
EPISODE_PROVENANCE_REF_KEYS = frozenset({"artifact_path", "artifact_digest"})
APPROVAL_REF_KEYS = frozenset({"artifact_path", "artifact_digest", "provenance"})
EPISODE_KEYS = frozenset({
    "dataset_identity_digest", "episode_id", "episode_index", "episode_content_digest",
    "technical_validator", "human_semantic_evidence", "episode_provenance",
    "training_approval",
})
INVENTORY_KEYS = frozenset({"schema_version", "scope", "dataset_identity", "episodes", "inventory_digest"})
SEED_MANIFEST_KEYS = frozenset({
    "schema_version", "manifest_id", "kind", "hypothesis_digest",
    "fixed_contract_digest", "randomization_seed", "slots", "manifest_budget",
    "program_budget", "planned_usage", "authority", "manifest_digest",
})
SEED_SLOT_KEYS = frozenset({
    "slot_id", "base_condition_digest", "robot_start_pose_id", "split_group",
    "repeat_index", "hil_prompts", "reviews", "pending_reviews", "storage_bytes",
    "order_index",
})


def _document(source: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(source, Mapping):
        try:
            return load_json_strict(json.dumps(dict(source), allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ContractError("JSON_NONFINITE", str(exc)) from exc
    if isinstance(source, (str, Path)):
        return load_json_strict(source)
    raise ContractError("TRAINING_DOCUMENT")


def _exact(value: object, keys: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ContractError(code)
    return value


def _id(value: object, code: str) -> str:
    if not isinstance(value, str) or value in {".", "..", "HUMAN"} or not SAFE_ID.fullmatch(value):
        raise ContractError(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ContractError(code)
    return value


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or not value.isprintable():
        raise ContractError(code)
    return value


def _timestamp(value: object, code: str) -> str:
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        raise ContractError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    return value


def _scope(value: object, expected: str | None = None) -> str:
    if value not in {PRODUCTION_SCOPE, SYNTHETIC_SCOPE} or expected is not None and value != expected:
        raise ContractError("TRAINING_APPROVAL_SCOPE")
    return value


def _dataset(value: object) -> dict[str, Any]:
    value = _exact(value, DATASET_KEYS, "TRAINING_DATASET_FIELDS")
    result = dict(value)
    result["dataset_id"] = _id(result["dataset_id"], "TRAINING_DATASET_ID")
    result["repo_id"] = _text(result["repo_id"], "TRAINING_DATASET_ID")
    result["dataset_root"] = _text(result["dataset_root"], "TRAINING_DATASET_ID")
    _digest(result["dataset_digest"], "TRAINING_DATASET_DIGEST")
    canonical_digest(result)
    return result


def _episode_identity(episode_id: object, episode_index: object, content_digest: object) -> tuple[str, int, str]:
    ident = _id(episode_id, "TRAINING_EPISODE_ID")
    if type(episode_index) is not int or episode_index < 0:
        raise ContractError("TRAINING_EPISODE_INDEX")
    return ident, episode_index, _digest(content_digest, "TRAINING_EPISODE_DIGEST")


def _count(value: object, code: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ContractError(code)
    return value


def _artifact(path: object, digest: object, code: str) -> dict[str, Any]:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ContractError(code)
    expected = _digest(digest, code)
    artifact = Path(path)
    try:
        if artifact.is_symlink() or not artifact.is_file():
            raise ContractError(code)
    except OSError as exc:
        raise ContractError(code) from exc
    value = load_json_strict(artifact)
    if canonical_digest(value) != expected:
        raise ContractError(code)
    return value


def _technical(path: object, digest: object, *, episode_id: str, dataset_root: str) -> dict[str, Any]:
    value = _artifact(path, digest, "TRAINING_TECHNICAL_ARTIFACT")
    if (
        set(value) != TECHNICAL_FIELDS
        or value.get("schema_version") != "data_factory.technical_validator_result.v1"
        or value.get("run_id") != episode_id
        or value.get("dataset_root") != dataset_root
        or value.get("status") != "PASS"
        or any(not isinstance(value.get(key), str) or not DIGEST.fullmatch(value[key]) for key in ("resolved_job_digest", "plan_digest", "result_digest"))
        or isinstance(value.get("expected_fps"), bool)
        or not isinstance(value.get("expected_fps"), (int, float))
        or not math.isfinite(value["expected_fps"])
        or value["expected_fps"] <= 0
    ):
        raise ContractError("TRAINING_TECHNICAL_PASS")
    return value


def _semantic(path: object, digest: object, *, episode_id: str, technical: Mapping[str, Any]) -> dict[str, Any]:
    value = _artifact(path, digest, "TRAINING_SEMANTIC_ARTIFACT")
    reviewer = value.get("reviewed_by")
    expected_context = canonical_digest({
        "run_id": episode_id,
        "resolved_job_digest": technical["resolved_job_digest"],
        "plan_digest": technical["plan_digest"],
        "technical_validator_digest": canonical_digest(technical),
    })
    if (
        set(value) != CANDIDATE_FIELDS
        or value.get("schema_version") != "data_factory.candidate_admission.v1"
        or value.get("run_id") != episode_id
        or value.get("operational_gate") != "PASS"
        or value.get("operational_source") not in {"HIL_PROXY", "HUMAN_GATED"}
        or value.get("checklist_id") != "pickup-v2"
        or value.get("review_context_digest") != expected_context
        or value.get("semantic_status") != "PASS"
        or value.get("reason") is not None
    ):
        raise ContractError("TRAINING_SEMANTIC_PASS")
    _id(reviewer, "TRAINING_SEMANTIC_REVIEWER")
    _timestamp(value.get("reviewed_at"), "TRAINING_SEMANTIC_REVIEW_TIME")
    return value


def _seed_manifest_slot(
    source: Mapping[str, Any] | str | Path, *, manifest_slot_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the exact seed source and derive its named slot without caller annotations."""
    manifest = _document(source)
    _exact(manifest, SEED_MANIFEST_KEYS, "TRAINING_SEED_MANIFEST_FIELDS")
    if (
        manifest["schema_version"] != "data_factory.seed_manifest.v1"
        or manifest["kind"] != "seed"
        or manifest["authority"] != "NO_EXECUTION_AUTHORITY"
    ):
        raise ContractError("TRAINING_SEED_MANIFEST_SCHEMA")
    _id(manifest["manifest_id"], "TRAINING_SEED_MANIFEST_ID")
    _digest(manifest["hypothesis_digest"], "TRAINING_SEED_MANIFEST_DIGEST")
    _digest(manifest["fixed_contract_digest"], "TRAINING_SEED_MANIFEST_DIGEST")
    _count(manifest["randomization_seed"], "TRAINING_SEED_MANIFEST_SEED")
    expected_digest = canonical_digest({key: manifest[key] for key in manifest if key != "manifest_digest"})
    if _digest(manifest["manifest_digest"], "TRAINING_SEED_MANIFEST_DIGEST") != expected_digest:
        raise ContractError("TRAINING_SEED_MANIFEST_DIGEST")
    if not isinstance(manifest["slots"], list) or not manifest["slots"]:
        raise ContractError("TRAINING_SEED_MANIFEST_SLOTS")

    slots = []
    for order_index, source_slot in enumerate(manifest["slots"]):
        slot = dict(_exact(source_slot, SEED_SLOT_KEYS, "TRAINING_SEED_SLOT_FIELDS"))
        _id(slot["slot_id"], "TRAINING_SEED_SLOT_ID")
        _digest(slot["base_condition_digest"], "TRAINING_SEED_SLOT_DIGEST")
        _id(slot["robot_start_pose_id"], "TRAINING_SEED_SLOT_POSE")
        if slot["split_group"] not in {"TRAIN", "ID", "OOD"}:
            raise ContractError("TRAINING_SEED_SLOT_GROUP")
        for field in ("repeat_index", "hil_prompts", "reviews", "pending_reviews"):
            _count(slot[field], "TRAINING_SEED_SLOT_COUNT")
        _count(slot["storage_bytes"], "TRAINING_SEED_SLOT_COUNT", positive=True)
        if _count(slot["order_index"], "TRAINING_SEED_SLOT_ORDER") != order_index:
            raise ContractError("TRAINING_SEED_SLOT_ORDER")
        slots.append(slot)
    slot_ids = [slot["slot_id"] for slot in slots]
    slot_keys = [
        (slot["split_group"], slot["base_condition_digest"], slot["robot_start_pose_id"], slot["repeat_index"])
        for slot in slots
    ]
    if len(slot_ids) != len(set(slot_ids)) or len(slot_keys) != len(set(slot_keys)):
        raise ContractError("TRAINING_SEED_SLOT_DUPLICATE")
    manifest_slot_id = _id(manifest_slot_id, "TRAINING_SEED_SLOT_ID")
    selected = [slot for slot in slots if slot["slot_id"] == manifest_slot_id]
    if len(selected) != 1:
        raise ContractError("TRAINING_SEED_SLOT_MISSING")
    return manifest, selected[0]


def validate_episode_training_provenance(
    source: Mapping[str, Any] | str | Path, *, expected_scope: str = PRODUCTION_SCOPE,
) -> dict[str, Any]:
    """Validate one immutable episode-to-seed-slot provenance artifact."""
    value = _document(source)
    _exact(value, EPISODE_PROVENANCE_KEYS, "TRAINING_EPISODE_PROVENANCE_FIELDS")
    if value["schema_version"] != EPISODE_PROVENANCE_SCHEMA:
        raise ContractError("TRAINING_EPISODE_PROVENANCE_SCHEMA")
    _scope(value["scope"], expected_scope)
    _digest(value["dataset_identity_digest"], "TRAINING_EPISODE_PROVENANCE_DATASET")
    _episode_identity(value["episode_id"], value["episode_index"], value["episode_content_digest"])
    _digest(value["technical_validator_digest"], "TRAINING_EPISODE_PROVENANCE_TECHNICAL")
    _digest(value["resolved_job_digest"], "TRAINING_EPISODE_PROVENANCE_TECHNICAL")
    _id(value["seed_manifest_id"], "TRAINING_EPISODE_PROVENANCE_MANIFEST")
    _digest(value["seed_manifest_digest"], "TRAINING_EPISODE_PROVENANCE_MANIFEST")
    _id(value["manifest_slot_id"], "TRAINING_EPISODE_PROVENANCE_SLOT")
    if value["split_group"] not in {"TRAIN", "ID", "OOD"}:
        raise ContractError("TRAINING_EPISODE_PROVENANCE_GROUP")
    _count(value["repeat_index"], "TRAINING_EPISODE_PROVENANCE_REPEAT")
    _digest(value["base_condition_digest"], "TRAINING_EPISODE_PROVENANCE_CONDITION")
    _id(value["robot_start_pose_id"], "TRAINING_EPISODE_PROVENANCE_POSE")
    canonical_digest(value)
    return value


def compile_episode_training_provenance(
    *, scope: str, dataset_identity: Mapping[str, Any], episode_id: str,
    episode_index: int, episode_content_digest: str,
    technical_validator_path: str | Path, technical_validator_digest: str,
    seed_manifest: Mapping[str, Any] | str | Path, manifest_slot_id: str,
) -> dict[str, Any]:
    """Purely derive an episode binding from exact technical and seed sources."""
    scope = _scope(scope)
    dataset = _dataset(dataset_identity)
    episode_id, episode_index, episode_content_digest = _episode_identity(
        episode_id, episode_index, episode_content_digest,
    )
    technical = _technical(
        str(technical_validator_path), technical_validator_digest,
        episode_id=episode_id, dataset_root=dataset["dataset_root"],
    )
    manifest, slot = _seed_manifest_slot(seed_manifest, manifest_slot_id=manifest_slot_id)
    provenance = {
        "schema_version": EPISODE_PROVENANCE_SCHEMA,
        "scope": scope,
        "dataset_identity_digest": canonical_digest(dataset),
        "episode_id": episode_id,
        "episode_index": episode_index,
        "episode_content_digest": episode_content_digest,
        "technical_validator_digest": technical_validator_digest,
        "resolved_job_digest": technical["resolved_job_digest"],
        "seed_manifest_id": manifest["manifest_id"],
        "seed_manifest_digest": manifest["manifest_digest"],
        "manifest_slot_id": slot["slot_id"],
        "split_group": slot["split_group"],
        "repeat_index": slot["repeat_index"],
        "base_condition_digest": slot["base_condition_digest"],
        "robot_start_pose_id": slot["robot_start_pose_id"],
    }
    return validate_episode_training_provenance(provenance, expected_scope=scope)


def validate_training_approval(
    source: Mapping[str, Any] | str | Path, *, expected_scope: str = PRODUCTION_SCOPE,
) -> dict[str, Any]:
    """Validate one immutable human training-admission artifact."""
    value = _document(source)
    _exact(value, APPROVAL_KEYS, "TRAINING_APPROVAL_FIELDS")
    if value["schema_version"] != APPROVAL_SCHEMA or value["provenance"] != PROVENANCE:
        raise ContractError("TRAINING_APPROVAL_SCHEMA")
    _scope(value["scope"], expected_scope)
    value["dataset_identity"] = _dataset(value["dataset_identity"])
    _episode_identity(value["episode_id"], value["episode_index"], value["episode_content_digest"])
    _digest(value["technical_validator_digest"], "TRAINING_APPROVAL_BINDING")
    _digest(value["human_semantic_evidence_digest"], "TRAINING_APPROVAL_BINDING")
    _digest(value["episode_provenance_digest"], "TRAINING_APPROVAL_BINDING")
    _id(value["approved_by"], "TRAINING_APPROVER_ID")
    _timestamp(value["approved_at"], "TRAINING_APPROVAL_TIME")
    canonical_digest(value)
    return value


def _confirm_human_training_approval(confirmation: str) -> None:
    """Read the decision only from the controlling terminal, never stdin/JSONL."""
    try:
        with open("/dev/tty", "r", encoding="utf-8", buffering=1) as tty_in, open(
            "/dev/tty", "w", encoding="utf-8", buffering=1
        ) as tty_out:
            if not tty_in.isatty() or not tty_out.isatty():
                raise ContractError("HUMAN_TTY_REQUIRED")
            tty_out.write(f"Type exactly '{confirmation}' to issue training approval:\n")
            tty_out.flush()
            if tty_in.readline().rstrip("\r\n") != confirmation:
                raise ContractError("HUMAN_CONFIRMATION_FAILED")
    except OSError as exc:
        raise ContractError("HUMAN_TTY_REQUIRED") from exc


def _target(path: str | Path, exists_code: str) -> Path:
    if not isinstance(path, (str, Path)):
        raise ContractError("TRAINING_OUTPUT_PATH")
    target = Path(path)
    try:
        if target.exists() or target.is_symlink():
            raise ContractError(exists_code)
        if not target.parent.is_dir() or target.parent.is_symlink():
            raise ContractError("TRAINING_OUTPUT_PATH")
    except OSError as exc:
        raise ContractError("TRAINING_OUTPUT_PATH") from exc
    return target


def _write_exclusive(target: Path, value: Mapping[str, Any], exists_code: str) -> None:
    data = (json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise ContractError(exists_code) from exc
    except OSError as exc:
        raise ContractError("TRAINING_OUTPUT_IO", str(exc)) from exc
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    except (OSError, TypeError, ValueError) as exc:
        try:
            target.unlink()
        except OSError:
            pass
        raise ContractError("TRAINING_OUTPUT_IO", str(exc)) from exc


def issue_training_approval(
    output_path: str | Path, *, scope: str, dataset_identity: Mapping[str, Any],
    episode_id: str, episode_index: int, episode_content_digest: str,
    technical_validator_path: str | Path, technical_validator_digest: str,
    human_semantic_evidence_path: str | Path, human_semantic_evidence_digest: str,
    episode_provenance_path: str | Path, episode_provenance_digest: str,
    approved_by: str, clock=lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Issue one production approval after all bindings pass and a human confirms on /dev/tty."""
    if scope != PRODUCTION_SCOPE:
        raise ContractError("TRAINING_APPROVAL_SCOPE")
    dataset = _dataset(dataset_identity)
    episode_id, episode_index, episode_content_digest = _episode_identity(episode_id, episode_index, episode_content_digest)
    approved_by = _id(approved_by, "TRAINING_APPROVER_ID")
    target = _target(output_path, "TRAINING_APPROVAL_EXISTS")
    technical_path = str(technical_validator_path)
    semantic_path = str(human_semantic_evidence_path)
    technical = _technical(technical_path, technical_validator_digest, episode_id=episode_id, dataset_root=dataset["dataset_root"])
    _semantic(semantic_path, human_semantic_evidence_digest, episode_id=episode_id, technical=technical)
    provenance_raw = _artifact(
        str(episode_provenance_path), episode_provenance_digest,
        "TRAINING_EPISODE_PROVENANCE_ARTIFACT",
    )
    episode_provenance = validate_episode_training_provenance(provenance_raw)
    if (
        episode_provenance["dataset_identity_digest"] != canonical_digest(dataset)
        or episode_provenance["episode_id"] != episode_id
        or episode_provenance["episode_index"] != episode_index
        or episode_provenance["episode_content_digest"] != episode_content_digest
        or episode_provenance["technical_validator_digest"] != technical_validator_digest
        or episode_provenance["resolved_job_digest"] != technical["resolved_job_digest"]
    ):
        raise ContractError("TRAINING_EPISODE_PROVENANCE_BINDING")
    approved_at = clock()
    if not isinstance(approved_at, datetime) or approved_at.tzinfo is None or approved_at.utcoffset() is None:
        raise ContractError("TRAINING_APPROVAL_TIME")
    approval = {
        "schema_version": APPROVAL_SCHEMA,
        "scope": scope,
        "dataset_identity": dataset,
        "episode_id": episode_id,
        "episode_index": episode_index,
        "episode_content_digest": episode_content_digest,
        "technical_validator_digest": technical_validator_digest,
        "human_semantic_evidence_digest": human_semantic_evidence_digest,
        "episode_provenance_digest": episode_provenance_digest,
        "approved_by": approved_by,
        "approved_at": approved_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "provenance": PROVENANCE,
    }
    validate_training_approval(approval)
    confirmation = (
        f"{PROVENANCE} {dataset['dataset_id']} {episode_id} {episode_index} "
        f"{canonical_digest(approval)}"
    )
    _confirm_human_training_approval(confirmation)
    _write_exclusive(target, approval, "TRAINING_APPROVAL_EXISTS")
    return approval


def _episode(
    value: object, *, dataset: Mapping[str, Any], scope: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    value = _exact(value, EPISODE_KEYS, "TRAINING_INVENTORY_EPISODE_FIELDS")
    result = copy.deepcopy(dict(value))
    episode_id, episode_index, content_digest = _episode_identity(
        result["episode_id"], result["episode_index"], result["episode_content_digest"],
    )
    if result["dataset_identity_digest"] != canonical_digest(dataset):
        raise ContractError("TRAINING_INVENTORY_DATASET_BINDING")

    technical_ref = _exact(result["technical_validator"], TECHNICAL_REF_KEYS, "TRAINING_TECHNICAL_REFERENCE")
    if technical_ref.get("status") != "PASS":
        raise ContractError("TRAINING_TECHNICAL_PASS")
    technical = _technical(
        technical_ref.get("artifact_path"), technical_ref.get("artifact_digest"),
        episode_id=episode_id, dataset_root=dataset["dataset_root"],
    )

    semantic_ref = _exact(result["human_semantic_evidence"], SEMANTIC_REF_KEYS, "TRAINING_SEMANTIC_REFERENCE")
    if semantic_ref.get("status") != "PASS":
        raise ContractError("TRAINING_SEMANTIC_PASS")
    semantic = _semantic(
        semantic_ref.get("artifact_path"), semantic_ref.get("artifact_digest"),
        episode_id=episode_id, technical=technical,
    )
    if semantic_ref.get("reviewer_id") != semantic["reviewed_by"]:
        raise ContractError("TRAINING_SEMANTIC_REVIEWER")

    provenance_ref = _exact(
        result["episode_provenance"], EPISODE_PROVENANCE_REF_KEYS,
        "TRAINING_EPISODE_PROVENANCE_REFERENCE",
    )
    provenance_raw = _artifact(
        provenance_ref.get("artifact_path"), provenance_ref.get("artifact_digest"),
        "TRAINING_EPISODE_PROVENANCE_ARTIFACT",
    )
    episode_provenance = validate_episode_training_provenance(
        provenance_raw, expected_scope=scope,
    )
    if (
        episode_provenance["dataset_identity_digest"] != canonical_digest(dataset)
        or episode_provenance["episode_id"] != episode_id
        or episode_provenance["episode_index"] != episode_index
        or episode_provenance["episode_content_digest"] != content_digest
        or episode_provenance["technical_validator_digest"] != technical_ref["artifact_digest"]
        or episode_provenance["resolved_job_digest"] != technical["resolved_job_digest"]
    ):
        raise ContractError("TRAINING_EPISODE_PROVENANCE_BINDING")

    approval_ref = _exact(result["training_approval"], APPROVAL_REF_KEYS, "TRAINING_APPROVAL_REFERENCE")
    if approval_ref.get("provenance") != PROVENANCE:
        raise ContractError("TRAINING_APPROVAL_PROVENANCE")
    approval_raw = _artifact(
        approval_ref.get("artifact_path"), approval_ref.get("artifact_digest"), "TRAINING_APPROVAL_ARTIFACT",
    )
    approval = validate_training_approval(approval_raw, expected_scope=scope)
    if (
        approval["dataset_identity"] != dataset
        or approval["episode_id"] != episode_id
        or approval["episode_index"] != episode_index
        or approval["episode_content_digest"] != content_digest
        or approval["technical_validator_digest"] != technical_ref["artifact_digest"]
        or approval["human_semantic_evidence_digest"] != semantic_ref["artifact_digest"]
        or approval["episode_provenance_digest"] != provenance_ref["artifact_digest"]
    ):
        raise ContractError("TRAINING_APPROVAL_BINDING")
    canonical_digest(result)
    return result, episode_provenance


def _unique_episodes(
    episodes: Sequence[Mapping[str, Any]], provenances: Sequence[Mapping[str, Any]],
) -> None:
    ids = [value["episode_id"] for value in episodes]
    indices = [value["episode_index"] for value in episodes]
    slot_ids = [
        (value["seed_manifest_id"], value["seed_manifest_digest"], value["manifest_slot_id"])
        for value in provenances
    ]
    slot_keys = [
        (
            value["seed_manifest_id"], value["seed_manifest_digest"],
            value["split_group"], value["base_condition_digest"],
            value["robot_start_pose_id"], value["repeat_index"],
        )
        for value in provenances
    ]
    if (
        len(ids) != len(set(ids))
        or len(indices) != len(set(indices))
        or len(slot_ids) != len(set(slot_ids))
        or len(slot_keys) != len(set(slot_keys))
    ):
        raise ContractError("TRAINING_INVENTORY_DUPLICATE")


def build_training_approved_inventory(
    *, scope: str, dataset_identity: Mapping[str, Any], episodes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a canonical inventory from already-issued exact episode approvals."""
    scope = _scope(scope)
    dataset = _dataset(dataset_identity)
    if not isinstance(episodes, Sequence) or isinstance(episodes, (str, bytes)) or not episodes:
        raise ContractError("TRAINING_INVENTORY_EPISODES")
    checked = [_episode(value, dataset=dataset, scope=scope) for value in episodes]
    parsed = [value for value, _ in checked]
    _unique_episodes(parsed, [provenance for _, provenance in checked])
    parsed.sort(key=lambda value: (value["episode_index"], value["episode_id"]))
    body = {
        "schema_version": INVENTORY_SCHEMA,
        "scope": scope,
        "dataset_identity": dataset,
        "episodes": parsed,
    }
    return {**body, "inventory_digest": canonical_digest(body)}


def validate_training_approved_inventory(
    source: Mapping[str, Any] | str | Path, *, expected_scope: str = PRODUCTION_SCOPE,
) -> dict[str, Any]:
    """Strictly reload every referenced artifact and validate the immutable inventory."""
    value = _document(source)
    _exact(value, INVENTORY_KEYS, "TRAINING_INVENTORY_FIELDS")
    if value["schema_version"] != INVENTORY_SCHEMA:
        raise ContractError("TRAINING_INVENTORY_SCHEMA")
    _scope(value["scope"], expected_scope)
    dataset = _dataset(value["dataset_identity"])
    episodes = value["episodes"]
    if not isinstance(episodes, list) or not episodes:
        raise ContractError("TRAINING_INVENTORY_EPISODES")
    checked = [_episode(item, dataset=dataset, scope=value["scope"]) for item in episodes]
    parsed = [item for item, _ in checked]
    _unique_episodes(parsed, [provenance for _, provenance in checked])
    if parsed != sorted(parsed, key=lambda item: (item["episode_index"], item["episode_id"])):
        raise ContractError("TRAINING_INVENTORY_ORDER")
    body = {key: value[key] for key in ("schema_version", "scope", "dataset_identity", "episodes")}
    if value["inventory_digest"] != canonical_digest(body):
        raise ContractError("TRAINING_INVENTORY_DIGEST")
    return value


def write_training_approved_inventory(
    output_path: str | Path, inventory: Mapping[str, Any], *, expected_scope: str = PRODUCTION_SCOPE,
) -> Path:
    """Exclusively publish one fully validated inventory; never create or mutate its dataset."""
    value = validate_training_approved_inventory(inventory, expected_scope=expected_scope)
    target = _target(output_path, "TRAINING_INVENTORY_EXISTS")
    _write_exclusive(target, value, "TRAINING_INVENTORY_EXISTS")
    return target
