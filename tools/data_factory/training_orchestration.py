"""Pure dependency-injected orchestration for one offline training baseline."""

from __future__ import annotations

import copy
import json
from typing import Callable, Mapping

from tools.data_factory import training_receipts as receipts
from tools.data_factory.training_approval import (
    PRODUCTION_SCOPE,
    validate_episode_training_provenance,
    validate_training_approved_inventory,
)
from tools.data_factory.training_split import GROUPS, validate_training_split
from tools.fr5_data_factory import ContractError, DIGEST, canonical_digest


REQUEST_SCHEMA = "data_factory.offline_training_request.v1"
_REQUEST_KEYS = frozenset({
    "approved_inventory",
    "split",
    "normalized_argv",
    "config",
    "runtime_versions",
    "training_seed",
    "repository_commit",
    "source_digest",
    "profile_id",
    "profile_digest",
})
_CHECKPOINT_KEYS = frozenset({
    "status", "checkpoint_id", "checkpoint_tree_digest", "dataset_digest", "split_digest",
})
_EVALUATION_KEYS = frozenset({
    "status", "metric", "samples", "request_digest", "dataset_digest", "split_digest",
    "checkpoint_tree_digest", "reload_receipt_digest",
})


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ContractError(code)


def _mapping(value: object, code: str) -> dict:
    if not isinstance(value, Mapping):
        raise ContractError(code)
    try:
        return json.loads(json.dumps(dict(value), sort_keys=True, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ContractError(code, str(exc)) from exc


def _bind_inventory_split(inventory: Mapping, split: Mapping) -> None:
    dataset = inventory["dataset_identity"]
    split_dataset = split["dataset"]
    _require(
        split_dataset["repo_id"] == dataset["repo_id"]
        and split_dataset["dataset_root_identity_digest"] == dataset["dataset_digest"]
        and split["bindings"]["approved_episode_inventory_digest"]
        == inventory["inventory_digest"],
        "TRAINING_ORCHESTRATION_DATASET",
    )

    approved = {episode["episode_index"]: episode for episode in inventory["episodes"]}
    grouped = [
        (group, episode)
        for group in GROUPS
        for episode in split["episode_groups"][group]
    ]
    _require(
        len(grouped) == len(approved)
        and {episode["episode_index"] for _, episode in grouped} == set(approved),
        "TRAINING_ORCHESTRATION_EPISODE_SET",
    )
    for group, episode in grouped:
        source = approved[episode["episode_index"]]
        provenance_ref = source["episode_provenance"]
        provenance = validate_episode_training_provenance(
            provenance_ref["artifact_path"], expected_scope=inventory["scope"],
        )
        _require("seed_manifest_digest" in provenance, "TRAINING_FACTOR_SPLIT_REQUIRES_SEED_PROVENANCE")
        _require(
            canonical_digest(provenance) == provenance_ref["artifact_digest"]
            and provenance["seed_manifest_digest"]
            == split["bindings"]["episode_manifest_digest"]
            and episode["episode_ref_digest"] == source["episode_content_digest"]
            and episode["training_approval_digest"]
            == source["training_approval"]["artifact_digest"],
            "TRAINING_ORCHESTRATION_EPISODE_BINDING",
        )
        _require(
            provenance["split_group"] == group
            and provenance["base_condition_digest"] == episode["base_condition_digest"]
            and provenance["robot_start_pose_id"] == episode["robot_start_pose_id"],
            "TRAINING_ORCHESTRATION_CELL_BINDING",
        )


def normalize_training_request(
    value: Mapping[str, object], *, expected_scope: str = PRODUCTION_SCOPE,
) -> dict:
    """Validate all static inputs before an injected trainer can be called."""
    _require(isinstance(value, Mapping) and set(value) == _REQUEST_KEYS, "TRAINING_REQUEST_FIELDS")
    inventory = validate_training_approved_inventory(
        value["approved_inventory"], expected_scope=expected_scope,
    )
    split = validate_training_split(value["split"])
    _require(split["schema_version"] == 2, "TRAINING_REQUEST_SPLIT_V2_REQUIRED")
    _bind_inventory_split(inventory, split)

    try:
        argv = receipts.normalize_argv(value["normalized_argv"])
        runtime = receipts._runtime(_mapping(value["runtime_versions"], "TRAINING_REQUEST_RUNTIME"))
    except receipts.ReceiptError as exc:
        raise ContractError("TRAINING_REQUEST_PROVENANCE", str(exc)) from exc
    config = _mapping(value["config"], "TRAINING_REQUEST_CONFIG")
    seed = value["training_seed"]
    _require(type(seed) is int and seed >= 0, "TRAINING_REQUEST_SEED")
    _require(
        receipts.canonical_digest(argv) == split["bindings"]["normalized_command_digest"]
        and receipts.canonical_digest(runtime) == split["bindings"]["runtime_digest"],
        "TRAINING_REQUEST_SPLIT_PROVENANCE",
    )

    commit = value["repository_commit"]
    _require(
        isinstance(commit, str)
        and len(commit) in {40, 64}
        and all(character in "0123456789abcdef" for character in commit),
        "TRAINING_REQUEST_REPOSITORY_COMMIT",
    )
    for field in ("source_digest", "profile_digest"):
        _require(isinstance(value[field], str) and DIGEST.fullmatch(value[field]), "TRAINING_REQUEST_DIGEST")
    profile_id = value["profile_id"]
    _require(
        isinstance(profile_id, str)
        and bool(profile_id)
        and profile_id == profile_id.strip()
        and "\x00" not in profile_id,
        "TRAINING_REQUEST_PROFILE",
    )

    dataset = inventory["dataset_identity"]
    return {
        "schema_version": REQUEST_SCHEMA,
        "scope": inventory["scope"],
        "dataset": {
            **copy.deepcopy(dataset),
            "dataset_info_features_digest": split["dataset"]["dataset_info_features_digest"],
            "total_episodes": split["dataset"]["total_episodes"],
            "total_frames": split["dataset"]["total_frames"],
        },
        "approved_episode_inventory_digest": inventory["inventory_digest"],
        "episode_manifest_digest": split["bindings"]["episode_manifest_digest"],
        "split": copy.deepcopy(split),
        "split_digest": split["split_digest"],
        "repository_commit": commit,
        "source_digest": value["source_digest"],
        "profile_id": profile_id,
        "profile_digest": value["profile_digest"],
        "collection_profile_digest": split["bindings"]["collection_profile_digest"],
        "normalized_argv": argv,
        "argv_digest": receipts.canonical_digest(argv),
        "config": config,
        "config_digest": receipts.canonical_digest(config),
        "runtime_versions": copy.deepcopy(runtime),
        "runtime_digest": receipts.canonical_digest(runtime),
        "training_seed": seed,
    }


def training_request_bytes(value: Mapping[str, object]) -> bytes:
    """Return the byte-stable canonical representation sent to a trainer adapter."""
    try:
        return json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("TRAINING_REQUEST_SERIALIZATION", str(exc)) from exc


def _cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ContractError("TRAINING_ORCHESTRATION_CANCELLED")


def _invoke(stage: str, callback: Callable[[dict], object], context: Mapping) -> object:
    try:
        return callback(copy.deepcopy(dict(context)))
    except Exception as exc:
        code = f"TRAINING_ORCHESTRATION_{stage}_FAILED"
        raise ContractError(code, f"{code}: {exc}") from exc


def _checkpoint(value: object, train: Mapping[str, object], request: Mapping[str, object]) -> dict:
    result = _mapping(value, "TRAINING_ORCHESTRATION_CHECKPOINT_RESULT")
    _require(_CHECKPOINT_KEYS <= set(result), "TRAINING_ORCHESTRATION_CHECKPOINT_RESULT")
    _require(result["status"] == "PASS", "TRAINING_ORCHESTRATION_CHECKPOINT_STATUS")
    expected = {
        "checkpoint_id": train["checkpoint_id"],
        "checkpoint_tree_digest": train["checkpoint_tree_digest"],
        "dataset_digest": request["dataset"]["dataset_digest"],
        "split_digest": request["split_digest"],
    }
    _require(
        all(result[key] == expected_value for key, expected_value in expected.items()),
        "TRAINING_ORCHESTRATION_CHECKPOINT_BINDING",
    )
    return result


def _evaluation(value: object, expected: Mapping[str, object]) -> dict:
    result = _mapping(value, "TRAINING_ORCHESTRATION_EVALUATION_RESULT")
    samples = result.get("samples")
    _require(
        set(result) == _EVALUATION_KEYS
        and result.get("status") == "PASS"
        and isinstance(result.get("metric"), str)
        and bool(result["metric"])
        and type(samples) is int
        and samples > 0,
        "TRAINING_ORCHESTRATION_EVALUATION_RESULT",
    )
    _require(
        all(result[key] == value for key, value in expected.items()),
        "TRAINING_ORCHESTRATION_EVALUATION_BINDING",
    )
    return result


def orchestrate_training(
    value: Mapping[str, object], *,
    trainer: Callable[[dict], object],
    checkpoint_validator: Callable[[dict], object],
    reloader: Callable[[dict], object],
    evaluator: Callable[[dict], object],
    cancelled: Callable[[], bool] | None = None,
    expected_scope: str = PRODUCTION_SCOPE,
) -> dict:
    """Run only injected offline stages; this module owns no real-effect adapter."""
    request = normalize_training_request(value, expected_scope=expected_scope)
    request_digest = receipts.canonical_digest(request)
    _cancelled(cancelled)

    raw_train = _invoke("TRAINER", trainer, request)
    _cancelled(cancelled)
    expected = {
        "dataset_id": request["dataset"]["dataset_id"],
        "dataset_digest": request["dataset"]["dataset_digest"],
        "repository_commit": request["repository_commit"],
        "source_digest": request["source_digest"],
        "profile_id": request["profile_id"],
        "profile_digest": request["profile_digest"],
        "collection_profile_digest": request["collection_profile_digest"],
        "normalized_argv": request["normalized_argv"],
        "argv_digest": request["argv_digest"],
        "config_digest": request["config_digest"],
        "runtime_versions": request["runtime_versions"],
        "runtime_digest": request["runtime_digest"],
        "approved_episode_inventory_digest": request["approved_episode_inventory_digest"],
        "episode_manifest_digest": request["episode_manifest_digest"],
        "split_digest": request["split_digest"],
        "training_seed": request["training_seed"],
    }
    try:
        train = receipts.validate_training_receipt(raw_train, expected=expected)
    except receipts.ReceiptError as exc:
        code = "TRAINING_ORCHESTRATION_TRAINING_RECEIPT"
        raise ContractError(code, f"{code}: {exc}") from exc

    checkpoint_context = {"request": request, "training_receipt": train}
    _cancelled(cancelled)
    checkpoint = _checkpoint(
        _invoke("CHECKPOINT_VALIDATOR", checkpoint_validator, checkpoint_context), train, request,
    )
    _cancelled(cancelled)

    reload_context = {**checkpoint_context, "checkpoint_validation": checkpoint}
    raw_reload = _invoke("RELOADER", reloader, reload_context)
    _cancelled(cancelled)
    try:
        reload_receipt = receipts.validate_reload_receipt(raw_reload, train)
    except receipts.ReceiptError as exc:
        code = "TRAINING_ORCHESTRATION_RELOAD_RECEIPT"
        raise ContractError(code, f"{code}: {exc}") from exc

    evaluation_context = {**reload_context, "reload_receipt": reload_receipt}
    _cancelled(cancelled)
    evaluation = _evaluation(
        _invoke("EVALUATOR", evaluator, evaluation_context),
        {
            "request_digest": request_digest,
            "dataset_digest": request["dataset"]["dataset_digest"],
            "split_digest": request["split_digest"],
            "checkpoint_tree_digest": checkpoint["checkpoint_tree_digest"],
            "reload_receipt_digest": receipts.canonical_digest(reload_receipt),
        },
    )
    _cancelled(cancelled)
    return {
        "status": "PASS",
        "request": copy.deepcopy(request),
        "request_digest": request_digest,
        "training_receipt": train,
        "checkpoint_validation": checkpoint,
        "reload_receipt": reload_receipt,
        "evaluation": evaluation,
        "evaluation_digest": receipts.canonical_digest(evaluation),
        "production_artifact_issued": False,
        "human_authority": False,
        "training_authority": False,
    }
