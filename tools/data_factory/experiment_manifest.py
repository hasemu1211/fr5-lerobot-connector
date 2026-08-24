"""Finite, offline FR5 seed and rollout manifest contracts."""
from __future__ import annotations

import copy
import math
import random
from collections import Counter
from typing import Any, Mapping, Sequence

from tools.data_factory.quality.coverage_report import CONDITION_FIELDS
from tools.data_factory.training_split import FR5_FEATURE_CONTRACT, GROUPS, validate_program_budget
from tools.fr5_data_factory import ContractError, DIGEST, SAFE_ID, canonical_digest


JOINTS = ("j1", "j2", "j3", "j4", "j5", "j6")
FIXED_FIELDS = frozenset({
    "schema_version", "robot_system_id", "task", "instruction",
    "collection_profile_digest", "feature_contract", "object_profile_id",
    "grasp_profile_id", "scene_digest", "cell_calibration_id",
    "cell_calibration_digest", "motion_recipe", "motion_recipe_digest",
    "pregrasp_digest", "waypoint_digest", "trajectory_digest",
})
BASE_FIELDS = frozenset({
    "coverage_condition", "coverage_condition_digest", "yaw_action_binding_digest",
    "dual_view_observability_digest", "base_condition_digest",
})
POSE_FIELDS = frozenset({
    "robot_start_pose_id", "joint_order", "target_rad", "tolerance_rad",
    "home_candidate_digest", "qualification_digest", "qualification_status",
    "safety_status", "start_pose_digest",
})
PAIR_FIELDS = frozenset({"base_condition_digest", "robot_start_pose_id", "split_groups"})
HYPOTHESIS_FIELDS = frozenset({
    "schema_version", "fixed_contract", "base_conditions", "robot_start_poses",
    "allowed_pairs", "hypothesis_digest",
})
SLOT_INPUT_FIELDS = frozenset({
    "slot_id", "base_condition_digest", "robot_start_pose_id", "split_group",
    "repeat_index", "hil_prompts", "reviews", "pending_reviews", "storage_bytes",
})
SLOT_FIELDS = SLOT_INPUT_FIELDS | {"order_index"}
MANIFEST_BUDGET_FIELDS = frozenset({
    "max_physical_episodes", "max_rollout_trials", "max_hil_prompts",
    "max_reviews", "max_pending_reviews", "max_storage_bytes",
})
USAGE_FIELDS = frozenset({
    "physical_episodes", "rollout_trials", "hil_prompts", "reviews",
    "pending_reviews", "storage_bytes",
})
MANIFEST_FIELDS = frozenset({
    "schema_version", "manifest_id", "kind", "hypothesis_digest",
    "fixed_contract_digest", "randomization_seed", "slots", "manifest_budget",
    "program_budget", "planned_usage", "authority", "manifest_digest",
})
SCHEMAS = {
    "seed": "data_factory.seed_manifest.v1",
    "rollout": "data_factory.rollout_manifest.v1",
}


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


def _count(value: object, code: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ContractError(code)
    return value


def _number(value: object, code: str, *, positive: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(code)
    if positive and value <= 0:
        raise ContractError(code)
    return value


def _fixed_contract(value: object) -> dict[str, Any]:
    value = _exact(value, FIXED_FIELDS, "HYPOTHESIS_FIXED_FIELDS")
    result = copy.deepcopy(dict(value))
    if (
        result["schema_version"] != "data_factory.fr5_fixed_contract.v1"
        or result["task"] != "pickup_e2e"
        or result["motion_recipe"] != "DIRECT"
        or result["feature_contract"] != FR5_FEATURE_CONTRACT
    ):
        raise ContractError("HYPOTHESIS_FIXED_CONTRACT")
    for field in (
        "robot_system_id", "object_profile_id", "grasp_profile_id",
        "cell_calibration_id",
    ):
        _id(result[field], "HYPOTHESIS_FIXED_ID")
    if (
        not isinstance(result["instruction"], str) or not result["instruction"]
        or result["instruction"].strip() != result["instruction"]
        or not result["instruction"].isprintable()
    ):
        raise ContractError("HYPOTHESIS_INSTRUCTION")
    for field in (
        "collection_profile_digest", "scene_digest", "cell_calibration_digest",
        "motion_recipe_digest", "pregrasp_digest", "waypoint_digest",
        "trajectory_digest",
    ):
        _digest(result[field], "HYPOTHESIS_FIXED_DIGEST")
    return result


def compile_base_condition(
    coverage_condition: Mapping[str, Any], *, yaw_action_binding_digest: str,
    dual_view_observability_digest: str,
) -> dict[str, Any]:
    """Bind one explicit P5 coverage condition without changing its v1 key."""
    condition = _coverage_condition(coverage_condition)
    _digest(yaw_action_binding_digest, "HYPOTHESIS_BASE_DIGEST")
    _digest(dual_view_observability_digest, "HYPOTHESIS_BASE_DIGEST")
    draft = {
        "coverage_condition": condition,
        "coverage_condition_digest": canonical_digest(condition),
        "yaw_action_binding_digest": yaw_action_binding_digest,
        "dual_view_observability_digest": dual_view_observability_digest,
    }
    draft["base_condition_digest"] = canonical_digest(draft)
    return draft


def _coverage_condition(value: object) -> dict[str, Any]:
    condition = copy.deepcopy(dict(_exact(value, frozenset(CONDITION_FIELDS), "HYPOTHESIS_COVERAGE_FIELDS")))
    for field in CONDITION_FIELDS:
        item = condition[field]
        if field.endswith("_digest"):
            _digest(item, "HYPOTHESIS_COVERAGE_DIGEST")
        elif field in {"yaw_deg", "x_mm", "y_mm"}:
            _number(item, "HYPOTHESIS_COVERAGE_NUMBER")
        elif not isinstance(item, str) or not item:
            raise ContractError("HYPOTHESIS_COVERAGE_VALUE")
    return condition


def _base_condition(value: object, fixed: Mapping[str, Any]) -> dict[str, Any]:
    value = _exact(value, BASE_FIELDS, "HYPOTHESIS_BASE_FIELDS")
    result = copy.deepcopy(dict(value))
    condition = _coverage_condition(result["coverage_condition"])
    expected = {
        "task": fixed["task"],
        "robot_system_id": fixed["robot_system_id"],
        "cell_calibration_id": fixed["cell_calibration_id"],
        "cell_calibration_digest": fixed["cell_calibration_digest"],
        "object_profile_id": fixed["object_profile_id"],
        "grasp_profile_id": fixed["grasp_profile_id"],
        "motion_recipe_digest": fixed["motion_recipe_digest"],
        "collection_profile_digest": fixed["collection_profile_digest"],
    }
    if any(condition[field] != item for field, item in expected.items()):
        raise ContractError("HYPOTHESIS_MIXED_FIXED_AXIS")
    for field in (
        "coverage_condition_digest", "yaw_action_binding_digest",
        "dual_view_observability_digest", "base_condition_digest",
    ):
        _digest(result[field], "HYPOTHESIS_BASE_DIGEST")
    if result["coverage_condition_digest"] != canonical_digest(condition):
        raise ContractError("HYPOTHESIS_COVERAGE_DIGEST_MISMATCH")
    if result["base_condition_digest"] != canonical_digest({key: result[key] for key in result if key != "base_condition_digest"}):
        raise ContractError("HYPOTHESIS_BASE_DIGEST_MISMATCH")
    return result


def compile_robot_start_pose(
    *, robot_start_pose_id: str, target_rad: Mapping[str, int | float],
    tolerance_rad: Mapping[str, int | float], home_candidate_digest: str,
    qualification_digest: str, qualification_status: str = "QUALIFIED",
    safety_status: str = "SAFE_FOR_MOTION",
) -> dict[str, Any]:
    target_rad = _exact(target_rad, frozenset(JOINTS), "HYPOTHESIS_JOINT_FIELDS")
    tolerance_rad = _exact(tolerance_rad, frozenset(JOINTS), "HYPOTHESIS_JOINT_FIELDS")
    draft = {
        "robot_start_pose_id": robot_start_pose_id,
        "joint_order": list(JOINTS),
        "target_rad": copy.deepcopy(dict(target_rad)),
        "tolerance_rad": copy.deepcopy(dict(tolerance_rad)),
        "home_candidate_digest": home_candidate_digest,
        "qualification_digest": qualification_digest,
        "qualification_status": qualification_status,
        "safety_status": safety_status,
    }
    draft["start_pose_digest"] = canonical_digest(draft)
    return _start_pose(draft)


def _start_pose(value: object) -> dict[str, Any]:
    value = _exact(value, POSE_FIELDS, "HYPOTHESIS_START_POSE_FIELDS")
    result = copy.deepcopy(dict(value))
    _id(result["robot_start_pose_id"], "HYPOTHESIS_START_POSE_ID")
    if result["joint_order"] != list(JOINTS):
        raise ContractError("HYPOTHESIS_JOINT_ORDER")
    for field in ("target_rad", "tolerance_rad"):
        joints = _exact(result[field], frozenset(JOINTS), "HYPOTHESIS_JOINT_FIELDS")
        for item in joints.values():
            _number(item, "HYPOTHESIS_JOINT_VALUE", positive=field == "tolerance_rad")
    for field in ("home_candidate_digest", "qualification_digest", "start_pose_digest"):
        _digest(result[field], "HYPOTHESIS_START_POSE_DIGEST")
    if result["qualification_status"] != "QUALIFIED" or result["safety_status"] != "SAFE_FOR_MOTION":
        raise ContractError("HYPOTHESIS_START_POSE_UNQUALIFIED")
    if result["start_pose_digest"] != canonical_digest({key: result[key] for key in result if key != "start_pose_digest"}):
        raise ContractError("HYPOTHESIS_START_POSE_DIGEST_MISMATCH")
    return result


def validate_fr5_hypothesis(value: object) -> dict[str, Any]:
    value = _exact(value, HYPOTHESIS_FIELDS, "HYPOTHESIS_FIELDS")
    if value["schema_version"] != "data_factory.fr5_hypothesis.v1":
        raise ContractError("HYPOTHESIS_SCHEMA")
    fixed = _fixed_contract(value["fixed_contract"])
    if not isinstance(value["base_conditions"], list) or not value["base_conditions"]:
        raise ContractError("HYPOTHESIS_BASE_CONDITIONS")
    conditions = [_base_condition(item, fixed) for item in value["base_conditions"]]
    condition_ids = [item["base_condition_digest"] for item in conditions]
    if len(condition_ids) != len(set(condition_ids)):
        raise ContractError("HYPOTHESIS_BASE_DUPLICATE")

    yaw_bindings: dict[int | float, tuple[str, str]] = {}
    observations: dict[str, tuple[str, str, str, str, str]] = {}
    policy = (
        fixed["grasp_profile_id"], fixed["pregrasp_digest"],
        fixed["waypoint_digest"], fixed["trajectory_digest"],
    )
    for item in conditions:
        yaw = item["coverage_condition"]["yaw_deg"]
        binding = (item["yaw_action_binding_digest"], item["dual_view_observability_digest"])
        if yaw in yaw_bindings and yaw_bindings[yaw] != binding:
            raise ContractError("HYPOTHESIS_YAW_BINDING_MIXED")
        yaw_bindings[yaw] = binding
        observed_policy = (*policy, item["yaw_action_binding_digest"])
        prior = observations.setdefault(item["dual_view_observability_digest"], observed_policy)
        if prior != observed_policy:
            raise ContractError("HYPOTHESIS_UNOBSERVABLE_POLICY_VARIATION")

    if not isinstance(value["robot_start_poses"], list) or not value["robot_start_poses"]:
        raise ContractError("HYPOTHESIS_START_POSES")
    poses = [_start_pose(item) for item in value["robot_start_poses"]]
    pose_ids = [item["robot_start_pose_id"] for item in poses]
    if len(pose_ids) != len(set(pose_ids)):
        raise ContractError("HYPOTHESIS_START_POSE_DUPLICATE")

    if not isinstance(value["allowed_pairs"], list) or not value["allowed_pairs"]:
        raise ContractError("HYPOTHESIS_ALLOWED_PAIRS")
    pairs: list[dict[str, Any]] = []
    pair_keys: set[tuple[str, str]] = set()
    eligibility = {group: set() for group in GROUPS}
    for source in value["allowed_pairs"]:
        pair = copy.deepcopy(dict(_exact(source, PAIR_FIELDS, "HYPOTHESIS_PAIR_FIELDS")))
        _digest(pair["base_condition_digest"], "HYPOTHESIS_PAIR_DIGEST")
        _id(pair["robot_start_pose_id"], "HYPOTHESIS_PAIR_POSE")
        groups = pair["split_groups"]
        if (
            not isinstance(groups, list) or not groups
            or any(group not in GROUPS for group in groups)
            or groups != [group for group in GROUPS if group in groups]
        ):
            raise ContractError("HYPOTHESIS_PAIR_GROUPS")
        key = (pair["base_condition_digest"], pair["robot_start_pose_id"])
        if key in pair_keys:
            raise ContractError("HYPOTHESIS_PAIR_DUPLICATE")
        if key[0] not in condition_ids or key[1] not in pose_ids:
            raise ContractError("HYPOTHESIS_PAIR_OUTSIDE_DOMAIN")
        pair_keys.add(key)
        for group in groups:
            eligibility[group].add(key)
        pairs.append(pair)
    if any(not eligibility[group] for group in GROUPS):
        raise ContractError("HYPOTHESIS_SPLIT_GROUP_EMPTY")
    if not eligibility["ID"].issubset(eligibility["TRAIN"]):
        raise ContractError("HYPOTHESIS_ID_NOT_TRAIN_CELL")
    train_conditions = {condition for condition, _ in eligibility["TRAIN"]}
    train_poses = {pose for _, pose in eligibility["TRAIN"]}
    if any(condition in train_conditions and pose in train_poses for condition, pose in eligibility["OOD"]):
        raise ContractError("HYPOTHESIS_OOD_NOT_FACTOR_HOLDOUT")

    result = copy.deepcopy(dict(value))
    result.update({
        "fixed_contract": fixed,
        "base_conditions": conditions,
        "robot_start_poses": poses,
        "allowed_pairs": pairs,
    })
    _digest(value["hypothesis_digest"], "HYPOTHESIS_DIGEST")
    if value["hypothesis_digest"] != canonical_digest({key: value[key] for key in value if key != "hypothesis_digest"}):
        raise ContractError("HYPOTHESIS_DIGEST_MISMATCH")
    return result


def compile_fr5_hypothesis(
    *, fixed_contract: Mapping[str, Any], base_conditions: Sequence[Mapping[str, Any]],
    robot_start_poses: Sequence[Mapping[str, Any]], allowed_pairs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    fixed_contract = _exact(fixed_contract, FIXED_FIELDS, "HYPOTHESIS_FIXED_FIELDS")
    if not isinstance(base_conditions, (list, tuple)) or not isinstance(robot_start_poses, (list, tuple)) or not isinstance(allowed_pairs, (list, tuple)):
        raise ContractError("HYPOTHESIS_SEQUENCE")
    draft = {
        "schema_version": "data_factory.fr5_hypothesis.v1",
        "fixed_contract": copy.deepcopy(dict(fixed_contract)),
        "base_conditions": copy.deepcopy(list(base_conditions)),
        "robot_start_poses": copy.deepcopy(list(robot_start_poses)),
        "allowed_pairs": copy.deepcopy(list(allowed_pairs)),
    }
    draft["hypothesis_digest"] = canonical_digest(draft)
    return validate_fr5_hypothesis(draft)


def _manifest_budget(value: object) -> dict[str, int]:
    value = _exact(value, MANIFEST_BUDGET_FIELDS, "MANIFEST_BUDGET_FIELDS")
    return {key: _count(item, "MANIFEST_BUDGET_VALUE", positive=True) for key, item in value.items()}


def _slot(value: object, *, ordered: bool) -> dict[str, Any]:
    fields = SLOT_FIELDS if ordered else SLOT_INPUT_FIELDS
    result = copy.deepcopy(dict(_exact(value, fields, "MANIFEST_SLOT_FIELDS")))
    _id(result["slot_id"], "MANIFEST_SLOT_ID")
    _digest(result["base_condition_digest"], "MANIFEST_SLOT_DIGEST")
    _id(result["robot_start_pose_id"], "MANIFEST_SLOT_POSE")
    if result["split_group"] not in GROUPS:
        raise ContractError("MANIFEST_SLOT_GROUP")
    for field in ("repeat_index", "hil_prompts", "reviews", "pending_reviews"):
        _count(result[field], "MANIFEST_SLOT_COUNT")
    _count(result["storage_bytes"], "MANIFEST_SLOT_STORAGE", positive=True)
    if ordered:
        _count(result["order_index"], "MANIFEST_SLOT_ORDER")
    return result


def _usage(kind: str, slots: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "physical_episodes": len(slots),
        "rollout_trials": len(slots) if kind == "rollout" else 0,
        "hil_prompts": sum(item["hil_prompts"] for item in slots),
        "reviews": sum(item["reviews"] for item in slots),
        "pending_reviews": sum(item["pending_reviews"] for item in slots),
        "storage_bytes": sum(item["storage_bytes"] for item in slots),
    }


def _check_budgets(
    manifest_budget: Mapping[str, int], program_budget: Mapping[str, int],
    planned: Mapping[str, int],
) -> None:
    for resource in USAGE_FIELDS:
        if planned[resource] > manifest_budget[f"max_{resource}"]:
            raise ContractError("MANIFEST_BUDGET_OVERSUBSCRIBED")
    program_names = {
        "physical_episodes": ("max_total_physical_episodes", "used_total_physical_episodes"),
        "rollout_trials": ("max_total_rollout_trials", "used_total_rollout_trials"),
        "hil_prompts": ("max_total_hil_prompts", "used_total_hil_prompts"),
        "reviews": ("max_total_reviews", "used_total_reviews"),
        "pending_reviews": ("max_pending_reviews", "used_pending_reviews"),
        "storage_bytes": ("max_total_storage_bytes", "used_total_storage_bytes"),
    }
    if program_budget["used_rounds"] + 1 > program_budget["max_rounds"]:
        raise ContractError("PROGRAM_BUDGET_OVERSUBSCRIBED")
    if any(program_budget[used] + planned[name] > program_budget[maximum] for name, (maximum, used) in program_names.items()):
        raise ContractError("PROGRAM_BUDGET_OVERSUBSCRIBED")


def validate_experiment_manifest(value: object, *, hypothesis: Mapping[str, Any]) -> dict[str, Any]:
    value = _exact(value, MANIFEST_FIELDS, "MANIFEST_FIELDS")
    result = copy.deepcopy(dict(value))
    kind = result["kind"]
    if not isinstance(kind, str) or kind not in SCHEMAS or result["schema_version"] != SCHEMAS[kind]:
        raise ContractError("MANIFEST_SCHEMA")
    _id(result["manifest_id"], "MANIFEST_ID")
    hypothesis = validate_fr5_hypothesis(hypothesis)
    if result["hypothesis_digest"] != hypothesis["hypothesis_digest"]:
        raise ContractError("MANIFEST_HYPOTHESIS_DIGEST")
    if result["fixed_contract_digest"] != canonical_digest(hypothesis["fixed_contract"]):
        raise ContractError("MANIFEST_FIXED_DIGEST")
    seed = _count(result["randomization_seed"], "MANIFEST_RANDOM_SEED")
    if not isinstance(result["slots"], list) or not result["slots"]:
        raise ContractError("MANIFEST_SLOTS")
    slots = [_slot(item, ordered=True) for item in result["slots"]]
    if [item["order_index"] for item in slots] != list(range(len(slots))):
        raise ContractError("MANIFEST_SLOT_ORDER")
    slot_ids = [item["slot_id"] for item in slots]
    slot_keys = [(item["split_group"], item["base_condition_digest"], item["robot_start_pose_id"], item["repeat_index"]) for item in slots]
    if len(slot_ids) != len(set(slot_ids)) or len(slot_keys) != len(set(slot_keys)):
        raise ContractError("MANIFEST_SLOT_DUPLICATE")
    allowed = {
        (pair["base_condition_digest"], pair["robot_start_pose_id"]): set(pair["split_groups"])
        for pair in hypothesis["allowed_pairs"]
    }
    if any(
        (item["base_condition_digest"], item["robot_start_pose_id"]) not in allowed
        or item["split_group"] not in allowed[(item["base_condition_digest"], item["robot_start_pose_id"])]
        for item in slots
    ):
        raise ContractError("MANIFEST_DISALLOWED_PAIR")
    present = {item["split_group"] for item in slots}
    required = set(GROUPS) if kind == "seed" else {"ID", "OOD"}
    if not required.issubset(present) or (kind == "rollout" and "TRAIN" in present):
        raise ContractError("MANIFEST_SPLIT_GROUPS")
    if kind == "seed":
        train_counts = Counter(
            (item["base_condition_digest"], item["robot_start_pose_id"])
            for item in slots if item["split_group"] == "TRAIN"
        )
        expected_train = {key for key, groups in allowed.items() if "TRAIN" in groups}
        if set(train_counts) != expected_train or len(set(train_counts.values())) != 1:
            raise ContractError("MANIFEST_UNBALANCED_TRAIN")

    source_slots = [{key: item[key] for key in SLOT_INPUT_FIELDS} for item in slots]
    expected_order = sorted(source_slots, key=lambda item: item["slot_id"])
    random.Random(seed).shuffle(expected_order)
    if source_slots != expected_order:
        raise ContractError("MANIFEST_RANDOM_ORDER")
    manifest_budget = _manifest_budget(result["manifest_budget"])
    program_budget = validate_program_budget(result["program_budget"])
    planned = _usage(kind, slots)
    planned_input = _exact(result["planned_usage"], USAGE_FIELDS, "MANIFEST_USAGE")
    if planned_input != planned:
        raise ContractError("MANIFEST_USAGE")
    _check_budgets(manifest_budget, program_budget, planned)
    if result["authority"] != "NO_EXECUTION_AUTHORITY":
        raise ContractError("MANIFEST_AUTHORITY")
    expected_digest = canonical_digest({key: result[key] for key in result if key != "manifest_digest"})
    if _digest(result["manifest_digest"], "MANIFEST_DIGEST") != expected_digest:
        raise ContractError("MANIFEST_DIGEST_MISMATCH")
    return result


def compile_experiment_manifest(
    *, kind: str, manifest_id: str, hypothesis: Mapping[str, Any],
    slots: Sequence[Mapping[str, Any]], randomization_seed: int,
    manifest_budget: Mapping[str, int], program_budget: Mapping[str, int],
) -> dict[str, Any]:
    if not isinstance(kind, str) or kind not in SCHEMAS:
        raise ContractError("MANIFEST_KIND")
    _id(manifest_id, "MANIFEST_ID")
    validated_hypothesis = validate_fr5_hypothesis(hypothesis)
    if not isinstance(slots, (list, tuple)) or not slots:
        raise ContractError("MANIFEST_SLOTS")
    normalized_slots = [_slot(item, ordered=False) for item in slots]
    normalized_slots.sort(key=lambda item: item["slot_id"])
    random.Random(_count(randomization_seed, "MANIFEST_RANDOM_SEED")).shuffle(normalized_slots)
    ordered = [{**item, "order_index": index} for index, item in enumerate(normalized_slots)]
    manifest_budget = _manifest_budget(manifest_budget)
    program_budget = validate_program_budget(program_budget)
    draft = {
        "schema_version": SCHEMAS[kind],
        "manifest_id": manifest_id,
        "kind": kind,
        "hypothesis_digest": validated_hypothesis["hypothesis_digest"],
        "fixed_contract_digest": canonical_digest(validated_hypothesis["fixed_contract"]),
        "randomization_seed": randomization_seed,
        "slots": ordered,
        "manifest_budget": copy.deepcopy(dict(manifest_budget)),
        "program_budget": copy.deepcopy(dict(program_budget)),
        "planned_usage": _usage(kind, ordered),
        "authority": "NO_EXECUTION_AUTHORITY",
    }
    draft["manifest_digest"] = canonical_digest(draft)
    return validate_experiment_manifest(draft, hypothesis=validated_hypothesis)


def compile_seed_manifest(**kwargs: Any) -> dict[str, Any]:
    return compile_experiment_manifest(kind="seed", **kwargs)


def compile_rollout_manifest(**kwargs: Any) -> dict[str, Any]:
    return compile_experiment_manifest(kind="rollout", **kwargs)


validate_hypothesis = validate_fr5_hypothesis
