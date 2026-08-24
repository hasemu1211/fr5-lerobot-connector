"""Cross-artifact readiness validation for the offline software contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from tools.data_factory.experiment_manifest import (
    validate_experiment_manifest,
    validate_fr5_hypothesis,
)
from tools.data_factory.training_approval import (
    PRODUCTION_SCOPE,
    validate_episode_training_provenance,
    validate_training_approved_inventory,
)
from tools.data_factory.training_receipts import (
    ReceiptError,
    canonical_digest as receipt_digest,
    validate_reload_receipt,
    validate_training_receipt,
)
from tools.data_factory.training_split import validate_training_split
from tools.fr5_data_factory import ContractError, canonical_digest


CONTRACT_READY = "CONTRACT_READY"
READINESS_SCHEMA = "data_factory.software_contract_readiness.v1"


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ContractError(code)


def _validate_receipts(training: object, reload: object) -> tuple[dict, dict]:
    try:
        train = validate_training_receipt(training)
        reloaded = validate_reload_receipt(reload, train)
    except ReceiptError as exc:
        raise ContractError("SOFTWARE_CONTRACT_RECEIPT", str(exc)) from exc
    return train, reloaded


def _validate_feature_binding(split: Mapping[str, Any], train: Mapping[str, Any]) -> None:
    feature = split["feature_contract"]
    binding = train["feature_binding"]
    features = binding["features"]
    mapping = feature["camera_mapping"]
    _require(
        binding["collection_profile_id"] == feature["collection_profile_id"]
        and binding["camera_profile"] == feature["camera_profile"]
        and features["observation.state"] == {
            "dtype": "float32", "shape": [feature["state_dimension"]],
        }
        and features["action"] == {
            "dtype": "float32", "shape": [feature["action_dimension"]],
        }
        and features["observation.images.up"]["policy_key"]
        == f"observation.images.{mapping['up']}"
        and features["observation.images.side"]["policy_key"]
        == f"observation.images.{mapping['side']}",
        "SOFTWARE_CONTRACT_FEATURE",
    )


def _episode_provenances(
    inventory: Mapping[str, Any], *, expected_scope: str,
) -> dict[int, dict[str, Any]]:
    provenances = {}
    for episode in inventory["episodes"]:
        reference = episode["episode_provenance"]
        provenance = validate_episode_training_provenance(
            reference["artifact_path"], expected_scope=expected_scope,
        )
        _require(
            canonical_digest(provenance) == reference["artifact_digest"],
            "SOFTWARE_CONTRACT_EPISODE_PROVENANCE_DIGEST",
        )
        provenances[episode["episode_index"]] = provenance
    return provenances


def validate_software_contract(
    *,
    approved_inventory: Mapping[str, Any] | str | Path,
    split: Mapping[str, Any] | str | Path,
    hypothesis: Mapping[str, Any],
    seed_manifest: Mapping[str, Any],
    training_receipt: Mapping[str, Any],
    reload_receipt: Mapping[str, Any],
    expected_scope: str = PRODUCTION_SCOPE,
) -> dict[str, Any]:
    """Validate one exact offline bundle; never issue, rewrite, train, reload, or execute."""
    inventory = validate_training_approved_inventory(
        approved_inventory, expected_scope=expected_scope,
    )
    split_v2 = validate_training_split(split)
    _require(split_v2["schema_version"] == 2, "SOFTWARE_CONTRACT_SPLIT_V2_REQUIRED")
    hypothesis_value = validate_fr5_hypothesis(hypothesis)
    manifest = validate_experiment_manifest(seed_manifest, hypothesis=hypothesis_value)
    _require(manifest["kind"] == "seed", "SOFTWARE_CONTRACT_SEED_REQUIRED")
    train, reloaded = _validate_receipts(training_receipt, reload_receipt)

    dataset = inventory["dataset_identity"]
    split_dataset = split_v2["dataset"]
    _require(
        split_dataset["repo_id"] == dataset["repo_id"]
        and split_dataset["dataset_root_identity_digest"] == dataset["dataset_digest"]
        and train["dataset_id"] == dataset["dataset_id"]
        and train["dataset_digest"] == dataset["dataset_digest"],
        "SOFTWARE_CONTRACT_DATASET",
    )

    bindings = split_v2["bindings"]
    _require(
        bindings["approved_episode_inventory_digest"] == inventory["inventory_digest"]
        and bindings["episode_manifest_digest"] == manifest["manifest_digest"]
        and bindings["collection_profile_digest"]
        == hypothesis_value["fixed_contract"]["collection_profile_digest"],
        "SOFTWARE_CONTRACT_SPLIT_BINDING",
    )
    _require(
        split_v2["evaluation_contract"]["program_budget"] == manifest["program_budget"],
        "SOFTWARE_CONTRACT_PROGRAM_BUDGET",
    )
    expected_receipt = {
        "dataset_id": dataset["dataset_id"],
        "dataset_digest": dataset["dataset_digest"],
        "collection_profile_digest": bindings["collection_profile_digest"],
        "argv_digest": bindings["normalized_command_digest"],
        "runtime_digest": bindings["runtime_digest"],
        "approved_episode_inventory_digest": inventory["inventory_digest"],
        "episode_manifest_digest": manifest["manifest_digest"],
        "split_digest": split_v2["split_digest"],
    }
    _require(
        all(train[key] == value for key, value in expected_receipt.items()),
        "SOFTWARE_CONTRACT_TRAIN_BINDING",
    )

    approved = {item["episode_index"]: item for item in inventory["episodes"]}
    provenances = _episode_provenances(inventory, expected_scope=expected_scope)
    manifest_slots = {item["slot_id"]: item for item in manifest["slots"]}
    base_conditions = {
        item["base_condition_digest"]: item for item in hypothesis_value["base_conditions"]
    }
    _require(
        len(provenances) == len(manifest_slots)
        and {item["manifest_slot_id"] for item in provenances.values()} == set(manifest_slots),
        "SOFTWARE_CONTRACT_MANIFEST_SLOT_SET",
    )
    for provenance in provenances.values():
        slot = manifest_slots[provenance["manifest_slot_id"]]
        base = base_conditions.get(provenance["base_condition_digest"])
        _require(
            provenance["seed_manifest_id"] == manifest["manifest_id"]
            and provenance["seed_manifest_digest"] == manifest["manifest_digest"]
            and provenance["split_group"] == slot["split_group"]
            and provenance["repeat_index"] == slot["repeat_index"]
            and provenance["base_condition_digest"] == slot["base_condition_digest"]
            and provenance["robot_start_pose_id"] == slot["robot_start_pose_id"]
            and base is not None
            and provenance["resolved_job_digest"] == base["resolved_job_digest"],
            "SOFTWARE_CONTRACT_MANIFEST_SLOT_BINDING",
        )

    grouped = [
        (group, item)
        for group, episodes in split_v2["episode_groups"].items()
        for item in episodes
    ]
    _require(
        {item["episode_index"] for _, item in grouped} == set(approved),
        "SOFTWARE_CONTRACT_EPISODE_SET",
    )
    for group, item in grouped:
        source = approved[item["episode_index"]]
        provenance = provenances[item["episode_index"]]
        _require(
            item["episode_ref_digest"] == source["episode_content_digest"]
            and item["training_approval_digest"]
            == source["training_approval"]["artifact_digest"],
            "SOFTWARE_CONTRACT_EPISODE_BINDING",
        )
        _require(
            group == provenance["split_group"]
            and item["base_condition_digest"] == provenance["base_condition_digest"]
            and item["robot_start_pose_id"] == provenance["robot_start_pose_id"],
            "SOFTWARE_CONTRACT_CELL_BINDING",
        )
    _validate_feature_binding(split_v2, train)

    body = {
        "schema_version": READINESS_SCHEMA,
        "status": CONTRACT_READY,
        "scope": inventory["scope"],
        "dataset_identity_digest": canonical_digest(dataset),
        "approved_episode_inventory_digest": inventory["inventory_digest"],
        "hypothesis_digest": hypothesis_value["hypothesis_digest"],
        "seed_manifest_digest": manifest["manifest_digest"],
        "split_digest": split_v2["split_digest"],
        "training_receipt_digest": receipt_digest(train),
        "reload_receipt_digest": receipt_digest(reloaded),
        "checkpoint_tree_digest": train["checkpoint_tree_digest"],
        "feature_digest": train["feature_digest"],
    }
    return {**body, "readiness_digest": canonical_digest(body)}
