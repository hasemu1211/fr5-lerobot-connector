"""Offline episode training admission; no dataset or training mutation authority."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.data_factory.candidate_admission import validate_candidate_admission
from tools.data_factory.quality.coverage_report import TECHNICAL_FIELDS
from tools.fr5_data_factory import ContractError, DIGEST, RFC3339, SAFE_ID, canonical_digest, load_json_strict


APPROVAL_SCHEMA = "data_factory.training_approval.v2"
BATCH_APPROVAL_SCHEMA = "data_factory.training_approval.v3"
DELEGATED_APPROVAL_SCHEMA = "data_factory.training_authorization.v1"
DELEGATION_SCHEMA = "data_factory.local_training_delegation.v1"
EPISODE_PROVENANCE_SCHEMA = "data_factory.episode_training_provenance.v1"
LEDGER_PROVENANCE_SCHEMA = "data_factory.episode_training_provenance.v2"
DERIVED_PROVENANCE_SCHEMA = "data_factory.episode_training_provenance.v3"
INVENTORY_SCHEMA = "data_factory.training_approved_inventory.v2"
PRODUCTION_SCOPE = "PRODUCTION"
SYNTHETIC_SCOPE = "SYNTHETIC_TEST_ONLY"
PROVENANCE = "HUMAN_TRAINING_APPROVED"
DELEGATED_PROVENANCE = "STANDING_LOCAL_TRAINING_DELEGATION"

DATASET_KEYS = frozenset({"dataset_id", "repo_id", "dataset_root", "dataset_digest"})
APPROVAL_KEYS = frozenset({
    "schema_version", "scope", "dataset_identity", "episode_id", "episode_index",
    "episode_content_digest", "technical_validator_digest",
    "human_semantic_evidence_digest", "episode_provenance_digest",
    "approved_by", "approved_at", "provenance",
})
DELEGATION_KEYS = frozenset({
    "schema_version", "delegation_id", "scope", "delegated_by",
    "authorized_actor", "authorization_source_ref", "dataset",
    "output_root", "profiles", "limits", "authority",
})
DELEGATION_DATASET_KEYS = frozenset({"repo_id", "dataset_root"})
DELEGATION_LIMIT_KEYS = frozenset({"max_steps", "max_batch_size", "max_checkpoints"})
DELEGATION_AUTHORITY = {
    "training": "LOCAL_OFFLINE_ONLY",
    "physical_execution": False,
    "remote_effects": False,
}
DELEGATED_APPROVAL_KEYS = frozenset({
    "schema_version", "scope", "dataset_identity", "episode_id", "episode_index",
    "episode_content_digest", "technical_validator_digest",
    "human_semantic_evidence_digest", "episode_provenance_digest",
    "authorized_actor", "authorized_at", "provenance", "delegation", "batch_digest",
})
EPISODE_PROVENANCE_KEYS = frozenset({
    "schema_version", "scope", "dataset_identity_digest", "episode_id",
    "episode_index", "episode_content_digest", "technical_validator_digest",
    "resolved_job_digest", "seed_manifest_id", "seed_manifest_digest",
    "manifest_slot_id", "split_group", "repeat_index",
    "base_condition_digest", "robot_start_pose_id",
})
LEDGER_PROVENANCE_KEYS = frozenset({
    "schema_version", "scope", "dataset_identity_digest", "episode_id", "episode_index",
    "episode_content_digest", "technical_validator_digest", "resolved_job_digest", "episode_ledger",
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
    try:
        value = validate_candidate_admission(value)
    except ContractError as exc:
        raise ContractError("TRAINING_SEMANTIC_PASS") from exc
    reviewer = value.get("reviewed_by")
    expected_context = canonical_digest({
        "run_id": episode_id,
        "resolved_job_digest": technical["resolved_job_digest"],
        "plan_digest": technical["plan_digest"],
        "technical_validator_digest": canonical_digest(technical),
    })
    if (
        value["run_id"] != episode_id
        or value["operational_gate"] != "PASS"
        or value["review_context_digest"] != expected_context
        or value["semantic_status"] != "PASS"
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
    if value.get("schema_version") == DERIVED_PROVENANCE_SCHEMA:
        return _validate_derived_provenance(value, expected_scope=expected_scope)
    if value.get("schema_version") == LEDGER_PROVENANCE_SCHEMA:
        _exact(value, LEDGER_PROVENANCE_KEYS, "TRAINING_EPISODE_PROVENANCE_FIELDS")
        _scope(value["scope"], expected_scope)
        _digest(value["dataset_identity_digest"], "TRAINING_EPISODE_PROVENANCE_DATASET")
        _episode_identity(value["episode_id"], value["episode_index"], value["episode_content_digest"])
        ref = _exact(value["episode_ledger"], EPISODE_PROVENANCE_REF_KEYS, "TRAINING_LEDGER_REFERENCE")
        from tools.data_factory.episode_ledger import validate_episode_ledger
        ledger = validate_episode_ledger(_artifact(ref["artifact_path"], ref["artifact_digest"], "TRAINING_LEDGER_ARTIFACT"))
        runtime_ref = ledger["artifacts"]["runtime_binding"]
        runtime = _artifact(runtime_ref["artifact_path"], runtime_ref["artifact_digest"], "TRAINING_LEDGER_RUNTIME")
        if runtime["data_disposition"] != "PRODUCTION":
            raise ContractError("TRAINING_APPROVAL_SCOPE")
        if (ledger["episode"]["run_id"] != value["episode_id"]
                or ledger["episode"]["episode_index"] != value["episode_index"]
                or ledger["artifacts"]["technical"]["artifact_digest"] != value["technical_validator_digest"]
                or ledger["bindings"]["resolved_job_digest"] != value["resolved_job_digest"]
                or ledger["admission"]["technical_status"] != "PASS"):
            raise ContractError("TRAINING_LEDGER_BINDING")
        return value
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


def compile_ledger_training_provenance(*, dataset_identity: Mapping[str, Any], episode_ledger_path: str | Path) -> dict:
    """Bind an existing Collection ledger to a frozen revision; grant no consent.

    The ledger's dataset digest is a Collection identity, not a byte snapshot.
    Its original root/repo/index and source references must match this revision.
    """
    from tools.data_factory.episode_ledger import validate_episode_ledger
    from tools.data_factory.training_receipts import file_digest

    dataset = _dataset(dataset_identity)
    path = Path(episode_ledger_path).resolve()
    ledger = validate_episode_ledger(load_json_strict(path))
    if any(ledger["dataset"][key] != dataset[key] for key in ("dataset_root", "repo_id")):
        raise ContractError("TRAINING_LEDGER_DATASET_BINDING")
    episode = ledger["episode"]
    source = Path(dataset["dataset_root"]) / f"meta/source_provenance/episode-{episode['episode_index']:06d}.jsonl"
    if file_digest(source) != file_digest(ledger["artifacts"]["source_provenance"]["artifact_path"]):
        raise ContractError("TRAINING_LEDGER_SOURCE_BINDING")
    value = {
        "schema_version": LEDGER_PROVENANCE_SCHEMA, "scope": PRODUCTION_SCOPE,
        "dataset_identity_digest": canonical_digest(dataset),
        "episode_id": episode["run_id"], "episode_index": episode["episode_index"],
        "episode_content_digest": current_episode_digest(dataset, episode["episode_index"]),
        "technical_validator_digest": ledger["artifacts"]["technical"]["artifact_digest"],
        "resolved_job_digest": ledger["bindings"]["resolved_job_digest"],
        "episode_ledger": {"artifact_path": str(path), "artifact_digest": canonical_digest(ledger)},
    }
    return validate_episode_training_provenance(value)


def _bind_ledger_revision(provenance: Mapping[str, Any], dataset: Mapping[str, Any]) -> None:
    if provenance["schema_version"] == LEDGER_PROVENANCE_SCHEMA:
        expected = compile_ledger_training_provenance(
            dataset_identity=dataset, episode_ledger_path=provenance["episode_ledger"]["artifact_path"],
        )
        if provenance != expected:
            raise ContractError("TRAINING_LEDGER_REVISION_BINDING")


def _derived_publication(reference: dict, dataset: Mapping[str, Any]) -> dict:
    from tools.data_factory.curator.workflow.derivation import published_training_evidence
    from tools.data_factory.curator.core.errors import CuratorError
    try:
        evidence = published_training_evidence(reference)
    except (CuratorError, OSError) as exc:
        raise ContractError("TRAINING_DERIVATION_EVIDENCE", str(exc)) from exc
    if any(dataset[key] != evidence["output"][source_key] for key, source_key in
           (("dataset_root", "root"), ("repo_id", "repo_id"), ("dataset_digest", "dataset_digest"))):
        raise ContractError("TRAINING_DERIVATION_DATASET")
    return evidence


def compile_derived_training_provenance(*, dataset: dict, derivation: dict, parent_draft: dict) -> dict:
    """Parent facts remain ancestry; only exact new authority can admit the child."""
    evidence = _derived_publication(derivation, dataset)
    args = parent_draft["approval_arguments"]
    provenance = parent_draft["provenance"]
    if provenance.get("schema_version") != LEDGER_PROVENANCE_SCHEMA:
        raise ContractError("TRAINING_DERIVATION_PARENT_LEDGER_REQUIRED")
    # Freshness belongs to new request/batch preparation, not retrospective
    # revocation of an issued approval that binds frozen evidence bytes.
    from tools.data_factory.episode_ledger import validate_episode_state
    ledger_path = Path(provenance["episode_ledger"]["artifact_path"])
    state = validate_episode_state(load_json_strict(ledger_path.parent / "episode_ledger_state.json"),
                                   ledger=load_json_strict(ledger_path))
    if (state["review"]["semantic_status"] != "PASS"
            or state["candidate"]["artifact_path"] != args["human_semantic_evidence_path"]
            or state["candidate"]["artifact_digest"] != args["human_semantic_evidence_digest"]):
        raise ContractError("TRAINING_DERIVATION_PARENT_REVIEW")
    parent = {
        "dataset_identity": args["dataset_identity"], "provenance": parent_draft["provenance"],
        "technical_validator": {"artifact_path": args["technical_validator_path"],
                                "artifact_digest": args["technical_validator_digest"]},
        "human_semantic_evidence": {"artifact_path": args["human_semantic_evidence_path"],
                                    "artifact_digest": args["human_semantic_evidence_digest"]},
    }
    value = {
        "schema_version": DERIVED_PROVENANCE_SCHEMA, "scope": PRODUCTION_SCOPE,
        "dataset_identity_digest": canonical_digest(dataset),
        "episode_id": args["episode_id"], "episode_index": args["episode_index"],
        "episode_content_digest": current_episode_digest(dataset, args["episode_index"]),
        "technical_validator_digest": evidence["technical"]["artifact_digest"],
        "resolved_job_digest": parent["provenance"]["resolved_job_digest"],
        "derivation": copy.deepcopy(derivation), "parent": parent,
        "curator_review": evidence["review"],
    }
    return validate_episode_training_provenance(value)


def _validate_derived_provenance(value: dict, *, expected_scope: str) -> dict:
    _exact(value, (LEDGER_PROVENANCE_KEYS - {"episode_ledger"}) |
           {"derivation", "parent", "curator_review"}, "TRAINING_EPISODE_PROVENANCE_FIELDS")
    _scope(value["scope"], expected_scope)
    parent = _exact(value["parent"], frozenset({"dataset_identity", "provenance", "technical_validator", "human_semantic_evidence"}), "TRAINING_DERIVATION_PARENT")
    _exact(value["derivation"], frozenset({"run_directory", "receipt_digest", "parent_dataset_identity"}), "TRAINING_DERIVATION_REFERENCE")
    parent_dataset = _dataset(parent["dataset_identity"])
    if value["derivation"].get("parent_dataset_identity") != parent_dataset:
        raise ContractError("TRAINING_DERIVATION_PARENT")
    # This bounded branch reuses actual Collection ledger/state authority only.
    if not isinstance(parent["provenance"], dict) or parent["provenance"].get("schema_version") != LEDGER_PROVENANCE_SCHEMA:
        raise ContractError("TRAINING_DERIVATION_PARENT_LEDGER_REQUIRED")
    provenance = validate_episode_training_provenance(parent["provenance"], expected_scope=expected_scope)
    _bind_ledger_revision(provenance, parent_dataset)
    technical_ref = _exact(parent["technical_validator"], EPISODE_PROVENANCE_REF_KEYS, "TRAINING_DERIVATION_PARENT_TECHNICAL")
    semantic_ref = _exact(parent["human_semantic_evidence"], EPISODE_PROVENANCE_REF_KEYS, "TRAINING_DERIVATION_PARENT_SEMANTIC")
    technical = _technical(technical_ref["artifact_path"], technical_ref["artifact_digest"],
                           episode_id=provenance["episode_id"], dataset_root=parent_dataset["dataset_root"])
    _semantic(semantic_ref["artifact_path"], semantic_ref["artifact_digest"],
              episode_id=provenance["episode_id"], technical=technical)
    if technical_ref["artifact_digest"] != provenance["technical_validator_digest"]:
        raise ContractError("TRAINING_DERIVATION_PARENT_REVIEW")
    from tools.data_factory.curator.workflow.derivation import published_training_evidence
    from tools.data_factory.curator.core.errors import CuratorError
    try:
        evidence = published_training_evidence(value["derivation"])
    except (CuratorError, OSError) as exc:
        raise ContractError("TRAINING_DERIVATION_EVIDENCE", str(exc)) from exc
    # The caller binds the complete child identity, including its local revision id.
    if (value["curator_review"] != evidence["review"]
            or value["technical_validator_digest"] != evidence["technical"]["artifact_digest"]
            or any(value[key] != provenance[key] for key in ("episode_id", "episode_index", "resolved_job_digest"))):
        raise ContractError("TRAINING_DERIVATION_BINDING")
    _digest(value["dataset_identity_digest"], "TRAINING_DERIVATION_DATASET")
    _episode_identity(value["episode_id"], value["episode_index"], value["episode_content_digest"])
    return value


def _training_evidence(provenance: dict, dataset: dict, *, episode_id: str,
                       technical_path: str, technical_digest: str,
                       semantic_path: str, semantic_digest: str) -> tuple[dict, dict]:
    """Validate raw facts or explicitly parent-only semantics with child pixels."""
    semantic_dataset = dataset
    if provenance["schema_version"] == DERIVED_PROVENANCE_SCHEMA:
        evidence = _derived_publication(provenance["derivation"], dataset)
        parent = provenance["parent"]
        if (evidence["technical"] != {"artifact_path": technical_path, "artifact_digest": technical_digest}
                or parent["human_semantic_evidence"] != {"artifact_path": semantic_path, "artifact_digest": semantic_digest}):
            raise ContractError("TRAINING_DERIVATION_BINDING")
        semantic_dataset = parent["dataset_identity"]
        technical_path = parent["technical_validator"]["artifact_path"]
        technical_digest = parent["technical_validator"]["artifact_digest"]
    technical = _technical(technical_path, technical_digest, episode_id=episode_id, dataset_root=semantic_dataset["dataset_root"])
    semantic = _semantic(semantic_path, semantic_digest, episode_id=episode_id, technical=technical)
    return technical, semantic


def validate_local_training_delegation(
    source: Mapping[str, Any] | str | Path, *, authorized_actor: str | None = None,
    dataset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate recorded user-authorized local trust, not human authentication."""
    value = _document(source)
    _exact(value, DELEGATION_KEYS, "TRAINING_DELEGATION_FIELDS")
    if value["schema_version"] != DELEGATION_SCHEMA:
        raise ContractError("TRAINING_DELEGATION_SCHEMA")
    _scope(value["scope"], PRODUCTION_SCOPE)
    _id(value["delegation_id"], "TRAINING_DELEGATION_ID")
    _id(value["delegated_by"], "TRAINING_DELEGATOR_ID")
    actor = _id(value["authorized_actor"], "TRAINING_DELEGATION_ACTOR")
    _text(value["authorization_source_ref"], "TRAINING_DELEGATION_SOURCE")
    declared_dataset = dict(_exact(
        value["dataset"], DELEGATION_DATASET_KEYS, "TRAINING_DELEGATION_DATASET",
    ))
    declared_dataset["repo_id"] = _text(
        declared_dataset["repo_id"], "TRAINING_DELEGATION_DATASET",
    )
    declared_dataset["dataset_root"] = _text(
        declared_dataset["dataset_root"], "TRAINING_DELEGATION_DATASET",
    )
    if not Path(declared_dataset["dataset_root"]).is_absolute():
        raise ContractError("TRAINING_DELEGATION_DATASET")
    output_root = _text(value["output_root"], "TRAINING_DELEGATION_OUTPUT")
    if not Path(output_root).is_absolute():
        raise ContractError("TRAINING_DELEGATION_OUTPUT")
    from tools.fr5_training_profile import PROFILE_NAMES
    profiles = value["profiles"]
    if (
        not isinstance(profiles, list) or not profiles
        or any(not isinstance(profile, str) for profile in profiles)
        or profiles != sorted(set(profiles))
        or any(profile not in PROFILE_NAMES for profile in profiles)
    ):
        raise ContractError("TRAINING_DELEGATION_PROFILES")
    limits = _exact(value["limits"], DELEGATION_LIMIT_KEYS, "TRAINING_DELEGATION_LIMITS")
    for limit in DELEGATION_LIMIT_KEYS:
        _count(limits[limit], "TRAINING_DELEGATION_LIMITS", positive=True)
    if value["authority"] != DELEGATION_AUTHORITY:
        raise ContractError("TRAINING_DELEGATION_AUTHORITY")
    if authorized_actor is not None and actor != authorized_actor:
        raise ContractError("TRAINING_DELEGATION_WRONG_ACTOR")
    if dataset is not None:
        checked_dataset = _dataset(dataset)
        if any(checked_dataset[key] != declared_dataset[key] for key in DELEGATION_DATASET_KEYS):
            raise ContractError("TRAINING_DELEGATION_DATASET")
    value["dataset"] = declared_dataset
    canonical_digest(value)
    return value


def validate_training_authorization(
    source: Mapping[str, Any] | str | Path, *, expected_scope: str = PRODUCTION_SCOPE,
) -> dict[str, Any]:
    """Validate either the original human approval or delegated local authority."""
    value = _document(source)
    if value.get("schema_version") != DELEGATED_APPROVAL_SCHEMA:
        return validate_training_approval(value, expected_scope=expected_scope)
    _exact(value, DELEGATED_APPROVAL_KEYS, "TRAINING_AUTHORIZATION_FIELDS")
    if value["provenance"] != DELEGATED_PROVENANCE:
        raise ContractError("TRAINING_AUTHORIZATION_SCHEMA")
    _scope(value["scope"], expected_scope)
    dataset = _dataset(value["dataset_identity"])
    _episode_identity(value["episode_id"], value["episode_index"], value["episode_content_digest"])
    for field in (
        "technical_validator_digest", "human_semantic_evidence_digest",
        "episode_provenance_digest", "batch_digest",
    ):
        _digest(value[field], "TRAINING_AUTHORIZATION_BINDING")
    actor = _id(value["authorized_actor"], "TRAINING_DELEGATION_ACTOR")
    _timestamp(value["authorized_at"], "TRAINING_AUTHORIZATION_TIME")
    reference = _exact(
        value["delegation"], EPISODE_PROVENANCE_REF_KEYS,
        "TRAINING_DELEGATION_REFERENCE",
    )
    delegation = _artifact(
        reference["artifact_path"], reference["artifact_digest"],
        "TRAINING_DELEGATION_ARTIFACT",
    )
    validate_local_training_delegation(
        delegation, authorized_actor=actor, dataset=dataset,
    )
    canonical_digest(value)
    return value


@contextmanager
def local_hf_offline(enabled: bool):
    """Keep delegated Hugging Face and Transformers loads cache-only."""
    if not enabled:
        yield
        return
    names = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")
    previous = {name: os.environ.get(name) for name in names}
    constants = sys.modules.get("huggingface_hub.constants")
    previous_constant = getattr(constants, "HF_HUB_OFFLINE", any(
        value is not None and value.upper() in {"1", "ON", "YES", "TRUE"}
        for value in previous.values()
    ))
    try:
        os.environ.update({name: "1" for name in names})
        if constants is not None:
            constants.HF_HUB_OFFLINE = True
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        constants = sys.modules.get("huggingface_hub.constants")
        if constants is not None:
            constants.HF_HUB_OFFLINE = bool(previous_constant)


def inventory_local_training_delegation(inventory: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the current delegation behind a validated inventory, if any."""
    references = []
    modes = {
        episode["training_approval"]["provenance"]
        for episode in inventory["episodes"]
    }
    if len(modes) != 1:
        raise ContractError("TRAINING_AUTHORIZATION_MIXED")
    if modes == {PROVENANCE}:
        return None
    if modes != {DELEGATED_PROVENANCE}:
        raise ContractError("TRAINING_APPROVAL_PROVENANCE")
    for episode in inventory["episodes"]:
        reference = episode["training_approval"]
        authorization = validate_training_authorization(
            _artifact(
                reference["artifact_path"], reference["artifact_digest"],
                "TRAINING_APPROVAL_ARTIFACT",
            ),
        )
        references.append(authorization["delegation"])
    if not references or any(reference != references[0] for reference in references[1:]):
        raise ContractError("TRAINING_AUTHORIZATION_MIXED")
    reference = references[0]
    return validate_local_training_delegation(_artifact(
        reference["artifact_path"], reference["artifact_digest"],
        "TRAINING_DELEGATION_ARTIFACT",
    ))


def validate_training_approval(
    source: Mapping[str, Any] | str | Path, *, expected_scope: str = PRODUCTION_SCOPE,
) -> dict[str, Any]:
    """Validate one immutable human training-admission artifact."""
    value = _document(source)
    batch = value.get("schema_version") == BATCH_APPROVAL_SCHEMA
    _exact(value, APPROVAL_KEYS | {"batch_digest"} if batch else APPROVAL_KEYS, "TRAINING_APPROVAL_FIELDS")
    if batch:
        _digest(value["batch_digest"], "TRAINING_BATCH_BINDING")
    if value["schema_version"] not in {APPROVAL_SCHEMA, BATCH_APPROVAL_SCHEMA} or value["provenance"] != PROVENANCE:
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


def _confirm_human_training_approval(confirmation: str, *, summary: str = "") -> None:
    """Read the decision only from the controlling terminal, never stdin/JSONL."""
    try:
        with open("/dev/tty", "r", encoding="utf-8", buffering=1) as tty_in, open(
            "/dev/tty", "w", encoding="utf-8", buffering=1
        ) as tty_out:
            if not tty_in.isatty() or not tty_out.isatty():
                raise ContractError("HUMAN_TTY_REQUIRED")
            if summary:
                tty_out.write(summary + "\n")
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
    # Publish complete bytes atomically, including under interruption; link never
    # replaces a competing producer's artifact. A killed writer can leave only
    # an unreferenced temporary file, never a half-written approval/inventory.
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".training-", suffix=".tmp") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
            os.link(file.name, target)
    except FileExistsError as exc:
        raise ContractError(exists_code) from exc
    except OSError as exc:
        raise ContractError("TRAINING_OUTPUT_IO", str(exc)) from exc


def _prepare_training_approval(
    *, scope: str, dataset_identity: Mapping[str, Any],
    episode_id: str, episode_index: int, episode_content_digest: str,
    technical_validator_path: str | Path, technical_validator_digest: str,
    human_semantic_evidence_path: str | Path, human_semantic_evidence_digest: str,
    episode_provenance_path: Mapping[str, Any] | str | Path, episode_provenance_digest: str,
    approved_by: str, clock=lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Validate exact evidence and prepare a document; this grants no consent."""
    if scope != PRODUCTION_SCOPE:
        raise ContractError("TRAINING_APPROVAL_SCOPE")
    dataset = _dataset(dataset_identity)
    episode_id, episode_index, episode_content_digest = _episode_identity(episode_id, episode_index, episode_content_digest)
    approved_by = _id(approved_by, "TRAINING_APPROVER_ID")
    technical_path = str(technical_validator_path)
    semantic_path = str(human_semantic_evidence_path)
    provenance_raw = (
        _document(episode_provenance_path) if isinstance(episode_provenance_path, Mapping)
        else _artifact(str(episode_provenance_path), episode_provenance_digest, "TRAINING_EPISODE_PROVENANCE_ARTIFACT")
    )
    if canonical_digest(provenance_raw) != episode_provenance_digest:
        raise ContractError("TRAINING_EPISODE_PROVENANCE_ARTIFACT")
    episode_provenance = validate_episode_training_provenance(provenance_raw)
    technical, _ = _training_evidence(
        episode_provenance, dataset, episode_id=episode_id,
        technical_path=technical_path, technical_digest=technical_validator_digest,
        semantic_path=semantic_path, semantic_digest=human_semantic_evidence_digest,
    )
    _bind_ledger_revision(episode_provenance, dataset)
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
    return approval


def issue_training_approval(
    output_path: str | Path, *, scope: str, dataset_identity: Mapping[str, Any],
    episode_id: str, episode_index: int, episode_content_digest: str,
    technical_validator_path: str | Path, technical_validator_digest: str,
    human_semantic_evidence_path: str | Path, human_semantic_evidence_digest: str,
    episode_provenance_path: str | Path, episode_provenance_digest: str,
    approved_by: str, clock=lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Issue one production approval with the original exact /dev/tty phrase."""
    if scope != PRODUCTION_SCOPE:
        raise ContractError("TRAINING_APPROVAL_SCOPE")
    target = _target(output_path, "TRAINING_APPROVAL_EXISTS")
    arguments = dict(scope=scope, dataset_identity=copy.deepcopy(dataset_identity),
        episode_id=episode_id, episode_index=episode_index, episode_content_digest=episode_content_digest,
        technical_validator_path=technical_validator_path, technical_validator_digest=technical_validator_digest,
        human_semantic_evidence_path=human_semantic_evidence_path, human_semantic_evidence_digest=human_semantic_evidence_digest,
        episode_provenance_path=str(episode_provenance_path), episode_provenance_digest=episode_provenance_digest,
        approved_by=approved_by)
    approval = _prepare_training_approval(**arguments, clock=clock)
    confirmation = (
        f"{PROVENANCE} {approval['dataset_identity']['dataset_id']} {episode_id} {episode_index} "
        f"{canonical_digest(approval)}"
    )
    _confirm_human_training_approval(confirmation)
    # Reload consumed evidence after the human wait, retaining the reviewed time.
    reviewed_at = datetime.fromisoformat(approval["approved_at"].replace("Z", "+00:00"))
    if _prepare_training_approval(**arguments, clock=lambda: reviewed_at) != approval:
        raise ContractError("TRAINING_INPUT_CHANGED")
    _write_exclusive(target, approval, "TRAINING_APPROVAL_EXISTS")
    return approval


def _batch_digest(approvals: Sequence[Mapping[str, Any]]) -> str:
    # v3 binds the complete sorted set of independent v2 evidence documents.
    return canonical_digest([
        {**{key: item[key] for key in APPROVAL_KEYS}, "schema_version": APPROVAL_SCHEMA}
        for item in sorted(approvals, key=lambda item: (item["episode_index"], item["episode_id"]))
    ])


def delegated_batch_digest(authorizations: Sequence[Mapping[str, Any]]) -> str:
    return canonical_digest([
        {key: item[key] for key in DELEGATED_APPROVAL_KEYS if key != "batch_digest"}
        for item in sorted(
            authorizations, key=lambda item: (item["episode_index"], item["episode_id"]),
        )
    ])


def _validate_batch_inventory(episodes: Sequence[Mapping[str, Any]]) -> None:
    approvals = [
        _artifact(item["training_approval"]["artifact_path"], item["training_approval"]["artifact_digest"], "TRAINING_APPROVAL_ARTIFACT")
        for item in episodes
    ]
    modes = {item.get("provenance") for item in approvals}
    if len(modes) != 1 or not modes <= {PROVENANCE, DELEGATED_PROVENANCE}:
        raise ContractError("TRAINING_AUTHORIZATION_MIXED")
    if modes == {DELEGATED_PROVENANCE}:
        if any(item.get("schema_version") != DELEGATED_APPROVAL_SCHEMA for item in approvals):
            raise ContractError("TRAINING_AUTHORIZATION_SCHEMA")
        if (
            len({json.dumps(item["delegation"], sort_keys=True) for item in approvals}) != 1
            or len({item["authorized_actor"] for item in approvals}) != 1
        ):
            raise ContractError("TRAINING_AUTHORIZATION_MIXED")
        digest = delegated_batch_digest(approvals)
        if any(item.get("batch_digest") != digest for item in approvals):
            raise ContractError("TRAINING_BATCH_BINDING")
    elif any(item["schema_version"] == BATCH_APPROVAL_SCHEMA for item in approvals):
        digest = _batch_digest(approvals)
        if any(item.get("batch_digest") != digest for item in approvals):
            raise ContractError("TRAINING_BATCH_BINDING")


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
    semantic_ref = _exact(result["human_semantic_evidence"], SEMANTIC_REF_KEYS, "TRAINING_SEMANTIC_REFERENCE")

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
    _bind_ledger_revision(episode_provenance, dataset)
    technical, semantic = _training_evidence(
        episode_provenance, dict(dataset), episode_id=episode_id,
        technical_path=technical_ref["artifact_path"], technical_digest=technical_ref["artifact_digest"],
        semantic_path=semantic_ref["artifact_path"], semantic_digest=semantic_ref["artifact_digest"],
    )
    expected_semantic = "PARENT_PASS" if episode_provenance["schema_version"] == DERIVED_PROVENANCE_SCHEMA else "PASS"
    if semantic_ref["status"] != expected_semantic:
        raise ContractError("TRAINING_SEMANTIC_PASS")
    if semantic_ref["reviewer_id"] != semantic["reviewed_by"]:
        raise ContractError("TRAINING_SEMANTIC_REVIEWER")
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
    if approval_ref.get("provenance") not in {PROVENANCE, DELEGATED_PROVENANCE}:
        raise ContractError("TRAINING_APPROVAL_PROVENANCE")
    approval_raw = _artifact(
        approval_ref.get("artifact_path"), approval_ref.get("artifact_digest"), "TRAINING_APPROVAL_ARTIFACT",
    )
    approval = validate_training_authorization(approval_raw, expected_scope=scope)
    if approval_ref["provenance"] != approval["provenance"]:
        raise ContractError("TRAINING_APPROVAL_PROVENANCE")
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
    seed_provenances = [value for value in provenances if value["schema_version"] == EPISODE_PROVENANCE_SCHEMA]
    slot_ids = [
        (value["seed_manifest_id"], value["seed_manifest_digest"], value["manifest_slot_id"])
        for value in seed_provenances
    ]
    slot_keys = [
        (
            value["seed_manifest_id"], value["seed_manifest_digest"],
            value["split_group"], value["base_condition_digest"],
            value["robot_start_pose_id"], value["repeat_index"],
        )
        for value in seed_provenances
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
    _validate_batch_inventory(parsed)
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
    _validate_batch_inventory(parsed)
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


def current_dataset_identity(root: str | Path, *, repo_id: str, dataset_id: str) -> dict:
    """Bind approval to frozen dataset bytes, including all metadata/provenance.

    Approval artifacts must live outside this tree: publishing consent must not
    change the bytes being approved. Collection's path identity is not a content
    snapshot and must never be substituted here.
    """
    from tools.data_factory.curator.core.identity import stable_tree_identity

    root = Path(root).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise ContractError("TRAINING_DATASET_ROOT")
    root = root.resolve()
    quarantine = root / "meta/quarantine.json"
    if quarantine.exists() or quarantine.is_symlink():
        raise ContractError("TRAINING_DATASET_QUARANTINED")
    _, digest = stable_tree_identity(root, code="TRAINING_DATASET_CHANGED")
    return _dataset({
        "dataset_id": dataset_id, "repo_id": repo_id,
        "dataset_root": str(root), "dataset_digest": digest,
    })


def current_episode_digest(dataset: Mapping[str, Any], episode_index: int) -> str:
    """Identify an episode within the exact frozen revision, not just its count."""
    from tools.data_factory.training_receipts import file_digest

    if type(episode_index) is not int or episode_index < 0:
        raise ContractError("TRAINING_EPISODE_INDEX")
    source = Path(dataset["dataset_root"]) / f"meta/source_provenance/episode-{episode_index:06d}.jsonl"
    return canonical_digest({
        "dataset_digest": dataset["dataset_digest"],
        "episode_index": episode_index,
        "source_provenance_digest": file_digest(source),
    })


def validate_current_training_inventory(
    source: str | Path, *, dataset_root: str | Path, repo_id: str,
    selected_episodes: list[int] | None = None,
) -> dict:
    """Public admission gate: strict production approval plus current byte identity."""
    root = Path(dataset_root).expanduser().resolve()
    path = Path(source).expanduser()
    if path.is_symlink() or not path.is_file() or path.resolve().is_relative_to(root):
        raise ContractError("TRAINING_INVENTORY_EXTERNAL_FILE_REQUIRED")
    inventory = validate_training_approved_inventory(path)
    dataset = inventory["dataset_identity"]
    current = current_dataset_identity(root, repo_id=repo_id, dataset_id=dataset["dataset_id"])
    if current != dataset:
        raise ContractError("TRAINING_DATASET_CHANGED")
    indices = [episode["episode_index"] for episode in inventory["episodes"]]
    if selected_episodes is not None and (
        any(type(index) is not int for index in selected_episodes)
        or selected_episodes != sorted(set(selected_episodes))
        or selected_episodes != indices
    ):
        raise ContractError("TRAINING_SELECTED_EPISODE_SET")
    for episode in inventory["episodes"]:
        if episode["episode_content_digest"] != current_episode_digest(current, episode["episode_index"]):
            raise ContractError("TRAINING_EPISODE_CONTENT_CHANGED")
    return inventory
