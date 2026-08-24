"""Strict, offline validation for FR5 training split contracts.

Schema v1 is legacy and validator-only.  New callers can only compile the
digest-bound schema v2 contract returned by :func:`compile_training_split`.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.fr5_data_factory import ContractError, DIGEST, SAFE_ID, canonical_digest, load_json_strict


LEGACY_FIELDS = frozenset({
    "schema_version", "repo_id", "total_episodes", "total_frames",
    "eval_split", "eval_episodes",
})
V2_FIELDS = frozenset({
    "schema_version", "dataset", "bindings", "feature_contract",
    "episode_groups", "evaluation_contract", "split_digest",
})
DATASET_FIELDS = frozenset({
    "dataset_root_identity_digest", "repo_id", "dataset_info_features_digest",
    "total_episodes", "total_frames",
})
BINDING_FIELDS = frozenset({
    "collection_profile_digest", "normalized_command_digest", "runtime_digest",
    "approved_episode_inventory_digest", "episode_manifest_digest",
})
EPISODE_FIELDS = frozenset({
    "episode_index", "episode_ref_digest", "training_approval_digest",
    "base_condition_digest", "robot_start_pose_id",
})
GROUPS = ("TRAIN", "ID", "OOD")
PROGRAM_BUDGET_FIELDS = frozenset({
    "max_rounds", "used_rounds",
    "max_total_physical_episodes", "used_total_physical_episodes",
    "max_total_rollout_trials", "used_total_rollout_trials",
    "max_total_hil_prompts", "used_total_hil_prompts",
    "max_total_reviews", "used_total_reviews",
    "max_pending_reviews", "used_pending_reviews",
    "max_total_storage_bytes", "used_total_storage_bytes",
})

FR5_FEATURE_CONTRACT = {
    "schema_version": "data_factory.fr5_feature_contract.v1",
    "collection_profile_id": "fr5-dual-rgb-30hz-v1",
    "camera_profile": "up-side",
    "camera_mapping": {"up": "camera1", "side": "camera2"},
    "state_dimension": 7,
    "action_dimension": 7,
}
EVALUATION_FIELDS = frozenset({
    "schema_version", "task", "required_result_fields", "outcomes",
    "program_budget",
})
EVALUATION_FIXED = {
    "schema_version": "data_factory.evaluation_contract.v1",
    "task": "pickup_e2e",
    "required_result_fields": ["task_success", "phase_success", "safety_stop", "outcome"],
    "outcomes": ["TERMINAL", "PARTIAL", "FAILURE"],
}


def _document(source: Mapping[str, Any] | str | Path, code: str) -> dict[str, Any]:
    if isinstance(source, Mapping):
        try:
            return load_json_strict(json.dumps(dict(source), allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ContractError("JSON_NONFINITE", str(exc)) from exc
    if isinstance(source, (str, Path)):
        return load_json_strict(source)
    raise ContractError(code)


def _exact(value: object, fields: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ContractError(code)
    return value


def _id(value: object, code: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ContractError(code)
    return value


def _repo_id(value: object) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > 256
        or value.strip() != value or "\x00" in value or any(char.isspace() for char in value)
    ):
        raise ContractError("SPLIT_REPO_ID")
    return value


def _count(value: object, code: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ContractError(code)
    return value


def validate_program_budget(value: object) -> dict[str, int]:
    """Validate finite cumulative limits; an already exhausted limit is closed."""
    value = _exact(value, PROGRAM_BUDGET_FIELDS, "PROGRAM_BUDGET_FIELDS")
    result = {key: _count(item, "PROGRAM_BUDGET_VALUE") for key, item in value.items()}
    pairs = (
        ("max_rounds", "used_rounds"),
        ("max_total_physical_episodes", "used_total_physical_episodes"),
        ("max_total_rollout_trials", "used_total_rollout_trials"),
        ("max_total_hil_prompts", "used_total_hil_prompts"),
        ("max_total_reviews", "used_total_reviews"),
        ("max_pending_reviews", "used_pending_reviews"),
        ("max_total_storage_bytes", "used_total_storage_bytes"),
    )
    if any(result[used] >= result[maximum] for maximum, used in pairs):
        raise ContractError("PROGRAM_BUDGET_EXHAUSTED")
    return result


def _validate_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    _exact(value, LEGACY_FIELDS, "SPLIT_V1_FIELDS")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ContractError("SPLIT_V1_SCHEMA")
    _repo_id(value["repo_id"])
    total = _count(value["total_episodes"], "SPLIT_V1_COUNT", positive=True)
    _count(value["total_frames"], "SPLIT_V1_COUNT", positive=True)
    fraction = value["eval_split"]
    if (
        isinstance(fraction, bool) or not isinstance(fraction, (int, float))
        or not math.isfinite(fraction) or not 0 < fraction < 1
    ):
        raise ContractError("SPLIT_V1_FRACTION")
    episodes = value["eval_episodes"]
    if (
        not isinstance(episodes, list) or not episodes
        or any(type(index) is not int or not 0 <= index < total for index in episodes)
        or episodes != sorted(set(episodes))
    ):
        raise ContractError("SPLIT_V1_EPISODES")
    return copy.deepcopy(dict(value))


def _episode(value: object, total_episodes: int) -> dict[str, Any]:
    value = _exact(value, EPISODE_FIELDS, "SPLIT_EPISODE_FIELDS")
    result = copy.deepcopy(dict(value))
    index = _count(result["episode_index"], "SPLIT_EPISODE_INDEX")
    if index >= total_episodes:
        raise ContractError("SPLIT_EPISODE_INDEX")
    for field in (
        "episode_ref_digest", "training_approval_digest", "base_condition_digest",
    ):
        _digest(result[field], "SPLIT_EPISODE_DIGEST")
    _id(result["robot_start_pose_id"], "SPLIT_START_POSE_ID")
    return result


def _evaluation(value: object) -> dict[str, Any]:
    value = _exact(value, EVALUATION_FIELDS, "EVALUATION_FIELDS")
    for key, expected in EVALUATION_FIXED.items():
        if value[key] != expected:
            raise ContractError("EVALUATION_CONTRACT")
    result = copy.deepcopy(dict(value))
    result["program_budget"] = validate_program_budget(value["program_budget"])
    return result


def _validate_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    _exact(value, V2_FIELDS, "SPLIT_V2_FIELDS")
    if type(value["schema_version"]) is not int or value["schema_version"] != 2:
        raise ContractError("SPLIT_V2_SCHEMA")

    dataset = _exact(value["dataset"], DATASET_FIELDS, "SPLIT_DATASET_FIELDS")
    for field in ("dataset_root_identity_digest", "dataset_info_features_digest"):
        _digest(dataset[field], "SPLIT_DATASET_DIGEST")
    _repo_id(dataset["repo_id"])
    total_episodes = _count(dataset["total_episodes"], "SPLIT_DATASET_COUNT", positive=True)
    _count(dataset["total_frames"], "SPLIT_DATASET_COUNT", positive=True)

    bindings = _exact(value["bindings"], BINDING_FIELDS, "SPLIT_BINDING_FIELDS")
    for item in bindings.values():
        _digest(item, "SPLIT_BINDING_DIGEST")
    if value["feature_contract"] != FR5_FEATURE_CONTRACT:
        raise ContractError("SPLIT_FEATURE_CONTRACT")

    groups = _exact(value["episode_groups"], frozenset(GROUPS), "SPLIT_GROUP_FIELDS")
    normalized: dict[str, list[dict[str, Any]]] = {}
    seen_indices: set[int] = set()
    seen_refs: set[str] = set()
    for group in GROUPS:
        episodes = groups[group]
        if not isinstance(episodes, list) or not episodes:
            raise ContractError("SPLIT_GROUP_EMPTY")
        normalized[group] = []
        for source in episodes:
            episode = _episode(source, total_episodes)
            if episode["episode_index"] in seen_indices or episode["episode_ref_digest"] in seen_refs:
                raise ContractError("SPLIT_EPISODE_DUPLICATE")
            seen_indices.add(episode["episode_index"])
            seen_refs.add(episode["episode_ref_digest"])
            normalized[group].append(episode)
        if [item["episode_index"] for item in normalized[group]] != sorted(item["episode_index"] for item in normalized[group]):
            raise ContractError("SPLIT_GROUP_ORDER")

    train_cells = {(item["base_condition_digest"], item["robot_start_pose_id"]) for item in normalized["TRAIN"]}
    if any((item["base_condition_digest"], item["robot_start_pose_id"]) not in train_cells for item in normalized["ID"]):
        raise ContractError("SPLIT_ID_NOT_TRAIN_CELL")
    train_conditions = {item["base_condition_digest"] for item in normalized["TRAIN"]}
    train_poses = {item["robot_start_pose_id"] for item in normalized["TRAIN"]}
    if any(
        item["base_condition_digest"] in train_conditions and item["robot_start_pose_id"] in train_poses
        for item in normalized["OOD"]
    ):
        raise ContractError("SPLIT_OOD_NOT_FACTOR_HOLDOUT")

    result = copy.deepcopy(dict(value))
    result["episode_groups"] = normalized
    result["evaluation_contract"] = _evaluation(value["evaluation_contract"])
    expected = canonical_digest({key: value[key] for key in value if key != "split_digest"})
    if _digest(value["split_digest"], "SPLIT_DIGEST") != expected:
        raise ContractError("SPLIT_DIGEST_MISMATCH")
    return result


def validate_training_split(source: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Validate v1 without upgrading it, or validate an immutable v2 artifact."""
    value = _document(source, "SPLIT_SOURCE")
    version = value.get("schema_version")
    if type(version) is not int:
        raise ContractError("SPLIT_SCHEMA")
    if version == 1:
        return _validate_v1(value)
    if version == 2:
        return _validate_v2(value)
    raise ContractError("SPLIT_SCHEMA")


def compile_training_split(
    *, dataset: Mapping[str, Any], bindings: Mapping[str, Any],
    episode_groups: Mapping[str, Sequence[Mapping[str, Any]]],
    program_budget: Mapping[str, int],
) -> dict[str, Any]:
    """Compile only schema v2; no legacy writer or filesystem side effect exists."""
    dataset = _exact(dataset, DATASET_FIELDS, "SPLIT_DATASET_FIELDS")
    bindings = _exact(bindings, BINDING_FIELDS, "SPLIT_BINDING_FIELDS")
    episode_groups = _exact(episode_groups, frozenset(GROUPS), "SPLIT_GROUP_FIELDS")
    normalized_groups = {}
    for group in GROUPS:
        if not isinstance(episode_groups[group], list):
            raise ContractError("SPLIT_GROUP_EPISODES")
        normalized_groups[group] = copy.deepcopy(episode_groups[group])
    program_budget = _exact(program_budget, PROGRAM_BUDGET_FIELDS, "PROGRAM_BUDGET_FIELDS")
    draft: dict[str, Any] = {
        "schema_version": 2,
        "dataset": copy.deepcopy(dict(dataset)),
        "bindings": copy.deepcopy(dict(bindings)),
        "feature_contract": copy.deepcopy(FR5_FEATURE_CONTRACT),
        "episode_groups": normalized_groups,
        "evaluation_contract": {
            **copy.deepcopy(EVALUATION_FIXED),
            "program_budget": copy.deepcopy(dict(program_budget)),
        },
    }
    draft["split_digest"] = canonical_digest(draft)
    return validate_training_split(draft)
