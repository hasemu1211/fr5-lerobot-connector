"""Finite, offline FR5 seed and rollout manifest contracts."""
from __future__ import annotations

import copy
import math
import random
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from tools.data_factory.quality.coverage_report import (
    RESOLVED_INPUT_DIGEST_FIELDS,
    _key as coverage_key,
    validate_coverage_report,
)
from tools.data_factory.training_split import FR5_FEATURE_CONTRACT, GROUPS, validate_program_budget
from tools.fr5_data_factory import (
    TASK_CONTRACTS,
    ContractError,
    DIGEST,
    SAFE_ID,
    canonical_digest,
    normalize_job_spec,
)


JOINTS = ("j1", "j2", "j3", "j4", "j5", "j6")
FEATURE_CONTRACT_FIELDS = frozenset({
    "schema_version", "collection_profile_id", "camera_profile",
    "camera_mapping", "state_dimension", "action_dimension",
})
TEST_ONLY_CAMERA_ROLES = {
    "up": ("up",),
    "up-side": ("up", "side"),
    "up-wrist": ("up", "wrist"),
}
FR5_TEST_ONLY_FEATURE_CONTRACT = {
    "schema_version": "data_factory.fr5_feature_contract.v1",
    "collection_profile_id": "fr5-up-rgb-30hz-v1",
    "camera_profile": "up",
    "camera_mapping": {"up": "camera1"},
    "state_dimension": 7,
    "action_dimension": 7,
}
FIXED_FIELDS = frozenset({
    "schema_version", "robot_system_id", "task", "instruction",
    "collection_profile_digest", "feature_contract", "object_profile_id",
    "grasp_profile_id", "scene_digest", "cell_calibration_id",
    "cell_calibration_digest", "motion_recipe", "motion_recipe_digest",
    "pregrasp_digest", "waypoint_digest", "trajectory_digest",
})
FIXED_V2_FIELDS = FIXED_FIELDS | frozenset({
    "endpoint_bindings", "endpoint_bindings_digest",
})
ENDPOINT_BINDING_FIELDS = frozenset({
    "workspace_id", "cell_calibration_id", "cell_calibration_digest",
    "motion_recipe_digest",
})
BASE_FIELDS = frozenset({
    "coverage_condition", "coverage_condition_digest", "yaw_action_binding_digest",
    "dual_view_observability_digest", "resolver_result_digest", "resolved_job_digest",
    "qualification_digest", "base_condition_digest",
})
POSE_FIELDS = frozenset({
    "robot_start_pose_id", "joint_order", "target_rad", "tolerance_rad",
    "home_candidate_digest", "qualification_digest", "qualification_status",
    "safety_status", "start_pose_digest",
})
HYPOTHESIS_FIELDS = frozenset({
    "schema_version", "fixed_contract", "coverage_report", "resolver_receipts",
    "base_conditions", "robot_start_poses", "qualification_catalog",
    "allowed_pairs", "hypothesis_digest",
})
RESOLVER_RESULT_FIELDS = frozenset({
    "normalized_job", "input_digests", "resolved_job_digest", "robot",
    "collection_profile", "calibration", "object_profile", "grasp_profile",
})
RESOLVER_RECEIPT_FIELDS = frozenset({
    "normalized_job", "input_digests", "resolved_job_digest", "resolver_result_digest",
})
BASE_QUALIFICATION_FIELDS = frozenset({
    "schema_version", "source", "qualification_status", "coverage_report_digest",
    "coverage_domain_digest", "coverage_condition_digest", "resolver_result_digest",
    "resolved_job_digest", "yaw_action_binding_digest",
    "dual_view_observability_digest", "qualification_digest",
})
POSE_QUALIFICATION_FIELDS = frozenset({
    "schema_version", "source", "robot_system_id", "robot_start_pose_id",
    "joint_order", "target_rad", "tolerance_rad", "home_candidate_digest",
    "qualification_status", "safety_status", "qualification_digest",
})
CATALOG_PAIR_FIELDS = frozenset({
    "base_condition_qualification_digest",
    "robot_start_pose_qualification_digest", "split_groups",
})
CATALOG_FIELDS = frozenset({
    "schema_version", "source", "qualification_status", "fixed_contract_digest",
    "coverage_report_digest", "coverage_domain_digest", "resolver_result_digests",
    "base_condition_qualifications", "robot_start_pose_qualifications",
    "allowed_pairs", "catalog_digest",
})
QUALIFICATION_SOURCES = frozenset({"QUALIFICATION_ARTIFACT", "SYNTHETIC_TEST_ONLY"})
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


def build_test_only_feature_contract(collection_profile: Mapping[str, Any]) -> dict[str, Any]:
    """Derive camera slots from a qualified v2 profile, never from device identity."""
    if not isinstance(collection_profile, Mapping):
        raise ContractError("HYPOTHESIS_TEST_ONLY_PROFILE")
    profile_id = collection_profile.get("collection_profile_id")
    camera_profile = collection_profile.get("camera_profile")
    roles = TEST_ONLY_CAMERA_ROLES.get(camera_profile) if isinstance(camera_profile, str) else None
    serials, topics = (
        collection_profile.get("camera_serials"),
        collection_profile.get("camera_topics"),
    )
    if (
        collection_profile.get("schema_version") != "data_factory.collection_profile.v2"
        or collection_profile.get("qualification_status") != "QUALIFIED"
        or not isinstance(profile_id, str) or not SAFE_ID.fullmatch(profile_id)
        or roles is None or collection_profile.get("camera_roles") != list(roles)
        or not isinstance(serials, Mapping) or set(serials) != set(roles)
        or not isinstance(topics, Mapping) or set(topics) != set(roles)
        or collection_profile.get("fps") != 30
        or collection_profile.get("width") != 640
        or collection_profile.get("height") != 480
    ):
        raise ContractError("HYPOTHESIS_TEST_ONLY_PROFILE")
    return {
        "schema_version": "data_factory.fr5_feature_contract.v1",
        "collection_profile_id": profile_id,
        "camera_profile": camera_profile,
        "camera_mapping": {
            role: f"camera{index}" for index, role in enumerate(roles, start=1)
        },
        "state_dimension": 7,
        "action_dimension": 7,
    }


def _is_test_only_feature_contract(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != FEATURE_CONTRACT_FIELDS:
        return False
    if value == FR5_FEATURE_CONTRACT:
        return False
    camera_profile = value.get("camera_profile")
    roles = TEST_ONLY_CAMERA_ROLES.get(camera_profile) if isinstance(camera_profile, str) else None
    profile_id = value.get("collection_profile_id")
    return bool(
        roles is not None
        and isinstance(profile_id, str) and SAFE_ID.fullmatch(profile_id)
        and value == {
            "schema_version": "data_factory.fr5_feature_contract.v1",
            "collection_profile_id": profile_id,
            "camera_profile": value["camera_profile"],
            "camera_mapping": {
                role: f"camera{index}" for index, role in enumerate(roles, start=1)
            },
            "state_dimension": 7,
            "action_dimension": 7,
        }
    )


def _fixed_contract(value: object) -> dict[str, Any]:
    fields = (
        FIXED_V2_FIELDS
        if isinstance(value, Mapping)
        and value.get("schema_version") == "data_factory.fr5_fixed_contract.v2"
        else FIXED_FIELDS
    )
    value = _exact(value, fields, "HYPOTHESIS_FIXED_FIELDS")
    result = copy.deepcopy(dict(value))
    if (
        result["schema_version"] not in {
            "data_factory.fr5_fixed_contract.v1",
            "data_factory.fr5_fixed_contract.v2",
        }
        or result["task"] not in TASK_CONTRACTS
        or result["motion_recipe"] != "DIRECT"
        or not (
            result["feature_contract"] == FR5_FEATURE_CONTRACT
            or _is_test_only_feature_contract(result["feature_contract"])
        )
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
    if result["schema_version"] == "data_factory.fr5_fixed_contract.v2":
        endpoints = result["endpoint_bindings"]
        if (
            result["task"] != "pick_place"
            or not isinstance(endpoints, list)
            or len(endpoints) != 2
        ):
            raise ContractError("HYPOTHESIS_ENDPOINT_BINDINGS")
        checked = []
        for endpoint in endpoints:
            endpoint = _exact(
                endpoint, ENDPOINT_BINDING_FIELDS,
                "HYPOTHESIS_ENDPOINT_BINDINGS",
            )
            checked.append(copy.deepcopy(dict(endpoint)))
            for field in ("workspace_id", "cell_calibration_id"):
                _id(endpoint[field], "HYPOTHESIS_ENDPOINT_BINDINGS")
            for field in ("cell_calibration_digest", "motion_recipe_digest"):
                _digest(endpoint[field], "HYPOTHESIS_ENDPOINT_BINDINGS")
        if (
            checked != sorted(
                checked,
                key=lambda item: (
                    item["workspace_id"], item["cell_calibration_id"],
                ),
            )
            or len({item["workspace_id"] for item in checked}) != 2
            or len({item["cell_calibration_id"] for item in checked}) != 2
            or result["cell_calibration_id"]
            != checked[0]["cell_calibration_id"]
            or result["cell_calibration_digest"]
            != checked[0]["cell_calibration_digest"]
            or result["endpoint_bindings_digest"] != canonical_digest(checked)
            or result["motion_recipe_digest"]
            != result["endpoint_bindings_digest"]
        ):
            raise ContractError("HYPOTHESIS_ENDPOINT_BINDINGS")
    return result


def _qualification_source(value: object, code: str) -> str:
    if not isinstance(value, str) or value not in QUALIFICATION_SOURCES:
        raise ContractError(code)
    return value


def _resolver_receipt(value: object) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, RESOLVER_RECEIPT_FIELDS, "HYPOTHESIS_RESOLVER_RECEIPT_FIELDS")))
    if not isinstance(result["normalized_job"], Mapping):
        raise ContractError("HYPOTHESIS_RESOLVER_JOB")
    job = normalize_job_spec(
        copy.deepcopy(dict(result["normalized_job"])),
        now=datetime.min.replace(tzinfo=timezone.utc),
    )
    inputs = _exact(result["input_digests"], frozenset(RESOLVED_INPUT_DIGEST_FIELDS), "HYPOTHESIS_RESOLVER_DIGESTS")
    for item in inputs.values():
        _digest(item, "HYPOTHESIS_RESOLVER_DIGESTS")
    expected = canonical_digest({"job": job, "input_digests": dict(inputs)})
    if result["normalized_job"] != job or result["resolved_job_digest"] != expected:
        raise ContractError("HYPOTHESIS_RESOLVER_DIGEST_MISMATCH")
    _digest(result["resolver_result_digest"], "HYPOTHESIS_RESOLVER_DIGEST")
    return result


def _resolver_result(
    value: object, *,
    feature_contract: Mapping[str, Any] = FR5_FEATURE_CONTRACT,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, RESOLVER_RESULT_FIELDS, "HYPOTHESIS_RESOLVER_FIELDS")))
    receipt = _resolver_receipt({
        "normalized_job": result["normalized_job"],
        "input_digests": result["input_digests"],
        "resolved_job_digest": result["resolved_job_digest"],
        "resolver_result_digest": canonical_digest(result),
    })
    job, inputs = receipt["normalized_job"], receipt["input_digests"]
    profiles = (
        ("robot", "robot_system", "robot_system_id"),
        ("collection_profile", "collection_profile", "collection_profile_id"),
        ("object_profile", "object_profile", "object_profile_id"),
        ("grasp_profile", "grasp_profile", "grasp_profile_id"),
    )
    for field, digest_field, id_field in profiles:
        document = result[field]
        if (
            not isinstance(document, Mapping)
            or document.get("qualification_status") != "QUALIFIED"
            or document.get(id_field) != job[id_field]
            or inputs[digest_field] != canonical_digest(document)
        ):
            raise ContractError("HYPOTHESIS_RESOLVER_SOURCE_BINDING")
    calibration = result["calibration"]
    if not isinstance(calibration, Mapping) or set(calibration) != {"center", "x", "y", "z", "document"}:
        raise ContractError("HYPOTHESIS_RESOLVER_SOURCE_BINDING")
    document = calibration["document"]
    if (
        not isinstance(document, Mapping)
        or document.get("qualification_status") != "QUALIFIED"
        or document.get("calibration_id") != job["cell_calibration_id"]
        or document.get("robot_system_id") != job["robot_system_id"]
        or document.get("place_id") != job["place_id"]
        or inputs["cell_calibration"] != canonical_digest(document)
        or inputs["selected_sheet"] != job["sheet_manifest_digest"]
        or job["collection_profile_id"] != feature_contract["collection_profile_id"]
        or result["grasp_profile"].get("object_profile_id") != job["object_profile_id"]
    ):
        raise ContractError("HYPOTHESIS_RESOLVER_SOURCE_BINDING")
    if (
        feature_contract not in (FR5_FEATURE_CONTRACT, FR5_TEST_ONLY_FEATURE_CONTRACT)
        and build_test_only_feature_contract(result["collection_profile"])
        != feature_contract
    ):
        raise ContractError("HYPOTHESIS_RESOLVER_SOURCE_BINDING")
    return receipt


def _profile_contract(
    fixed: Mapping[str, Any], report: Mapping[str, Any],
    receipts: Sequence[Mapping[str, Any]], source: str,
) -> None:
    feature = fixed["feature_contract"]
    profile_id = feature["collection_profile_id"]
    if (
        report["collection_profile_id"] != profile_id
        or any(
            receipt["normalized_job"]["collection_profile_id"] != profile_id
            or receipt["input_digests"]["collection_profile"] != fixed["collection_profile_digest"]
            for receipt in receipts
        )
    ):
        raise ContractError("HYPOTHESIS_COLLECTION_PROFILE_BINDING")


def _base_qualification(value: object) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, BASE_QUALIFICATION_FIELDS, "HYPOTHESIS_BASE_QUALIFICATION_FIELDS")))
    if result["schema_version"] != "data_factory.fr5_base_condition_qualification.v1":
        raise ContractError("HYPOTHESIS_BASE_QUALIFICATION_SCHEMA")
    _qualification_source(result["source"], "HYPOTHESIS_BASE_QUALIFICATION_SOURCE")
    if result["qualification_status"] != "QUALIFIED":
        raise ContractError("HYPOTHESIS_BASE_UNQUALIFIED")
    for field in BASE_QUALIFICATION_FIELDS - {"schema_version", "source", "qualification_status"}:
        _digest(result[field], "HYPOTHESIS_BASE_QUALIFICATION_DIGEST")
    if result["qualification_digest"] != canonical_digest({key: result[key] for key in result if key != "qualification_digest"}):
        raise ContractError("HYPOTHESIS_BASE_QUALIFICATION_DIGEST_MISMATCH")
    return result


def _resolver_condition(receipt: Mapping[str, Any], condition: Mapping[str, Any]) -> None:
    job, inputs = receipt["normalized_job"], receipt["input_digests"]
    expected = {
        "task_schema_version": job["schema_version"], "task": job["task"],
        "robot_system_id": job["robot_system_id"], "place_id": job["place_id"],
        "cell_calibration_id": job["cell_calibration_id"], "yaw_deg": job["yaw_deg"],
        "x_mm": job["x_mm"], "y_mm": job["y_mm"],
        "object_profile_id": job["object_profile_id"], "grasp_profile_id": job["grasp_profile_id"],
        "cell_calibration_digest": inputs["cell_calibration"],
        "collection_profile_digest": inputs["collection_profile"],
    }
    if any(condition[field] != item for field, item in expected.items()):
        raise ContractError("HYPOTHESIS_RESOLVER_CONDITION_MISMATCH")


def _base_from(
    report: Mapping[str, Any], receipt: Mapping[str, Any], qualification: object,
) -> dict[str, Any]:
    evidence = _base_qualification(qualification)
    matches = [
        cell["condition"] for cell in report["cells"]
        if canonical_digest(cell["condition"]) == evidence["coverage_condition_digest"]
    ]
    if len(matches) != 1:
        raise ContractError("HYPOTHESIS_COVERAGE_CONDITION_MISSING")
    condition = matches[0]
    _resolver_condition(receipt, condition)
    expected = {
        "coverage_report_digest": canonical_digest(report),
        "coverage_domain_digest": report["domain_digest"],
        "resolver_result_digest": receipt["resolver_result_digest"],
        "resolved_job_digest": receipt["resolved_job_digest"],
    }
    if any(evidence[field] != item for field, item in expected.items()):
        raise ContractError("HYPOTHESIS_BASE_QUALIFICATION_BINDING")
    draft = {
        "coverage_condition": copy.deepcopy(condition),
        "coverage_condition_digest": evidence["coverage_condition_digest"],
        "yaw_action_binding_digest": evidence["yaw_action_binding_digest"],
        "dual_view_observability_digest": evidence["dual_view_observability_digest"],
        "resolver_result_digest": evidence["resolver_result_digest"],
        "resolved_job_digest": evidence["resolved_job_digest"],
        "qualification_digest": evidence["qualification_digest"],
    }
    draft["base_condition_digest"] = canonical_digest(draft)
    return draft


def compile_base_condition(
    *, coverage_report: Mapping[str, Any], resolver_result: Mapping[str, Any],
    qualification: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile a compact condition binding from exact resolver and coverage evidence."""
    return _base_from(
        validate_coverage_report(coverage_report), _resolver_result(resolver_result),
        qualification,
    )


def _pose_qualification(value: object) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, POSE_QUALIFICATION_FIELDS, "HYPOTHESIS_POSE_QUALIFICATION_FIELDS")))
    if result["schema_version"] != "data_factory.robot_start_pose_qualification.v1":
        raise ContractError("HYPOTHESIS_POSE_QUALIFICATION_SCHEMA")
    _qualification_source(result["source"], "HYPOTHESIS_POSE_QUALIFICATION_SOURCE")
    _id(result["robot_system_id"], "HYPOTHESIS_START_POSE_ROBOT")
    _id(result["robot_start_pose_id"], "HYPOTHESIS_START_POSE_ID")
    if result["joint_order"] != list(JOINTS):
        raise ContractError("HYPOTHESIS_JOINT_ORDER")
    for field in ("target_rad", "tolerance_rad"):
        joints = _exact(result[field], frozenset(JOINTS), "HYPOTHESIS_JOINT_FIELDS")
        for item in joints.values():
            _number(item, "HYPOTHESIS_JOINT_VALUE", positive=field == "tolerance_rad")
    _digest(result["home_candidate_digest"], "HYPOTHESIS_START_POSE_DIGEST")
    _digest(result["qualification_digest"], "HYPOTHESIS_START_POSE_DIGEST")
    if result["qualification_status"] != "QUALIFIED" or result["safety_status"] != "SAFE_FOR_MOTION":
        raise ContractError("HYPOTHESIS_START_POSE_UNQUALIFIED")
    if result["qualification_digest"] != canonical_digest({key: result[key] for key in result if key != "qualification_digest"}):
        raise ContractError("HYPOTHESIS_POSE_QUALIFICATION_DIGEST_MISMATCH")
    return result


def compile_robot_start_pose(*, qualification: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _pose_qualification(qualification)
    draft = {
        key: copy.deepcopy(evidence[key])
        for key in POSE_FIELDS - {"start_pose_digest"}
    }
    draft["start_pose_digest"] = canonical_digest(draft)
    return draft


def _pair_groups(value: object, code: str) -> list[str]:
    if (
        not isinstance(value, list) or not value
        or any(group not in GROUPS for group in value)
        or value != [group for group in GROUPS if group in value]
    ):
        raise ContractError(code)
    return value


def _fixed_base(fixed: Mapping[str, Any], base: Mapping[str, Any], receipt: Mapping[str, Any]) -> None:
    condition = base["coverage_condition"]
    expected = {
        "task": fixed["task"], "robot_system_id": fixed["robot_system_id"],
        "object_profile_id": fixed["object_profile_id"],
        "grasp_profile_id": fixed["grasp_profile_id"],
        "collection_profile_digest": fixed["collection_profile_digest"],
    }
    if fixed["schema_version"] == "data_factory.fr5_fixed_contract.v1":
        expected.update(
            cell_calibration_id=fixed["cell_calibration_id"],
            cell_calibration_digest=fixed["cell_calibration_digest"],
            motion_recipe_digest=fixed["motion_recipe_digest"],
        )
        endpoint_matches = True
    else:
        endpoint_matches = any(
            endpoint == {
                "workspace_id": condition["place_id"],
                "cell_calibration_id": condition["cell_calibration_id"],
                "cell_calibration_digest": condition[
                    "cell_calibration_digest"
                ],
                "motion_recipe_digest": condition["motion_recipe_digest"],
            }
            for endpoint in fixed["endpoint_bindings"]
        )
    if (
        any(condition[field] != item for field, item in expected.items())
        or not endpoint_matches
        or receipt["normalized_job"]["instruction"] != fixed["instruction"]
    ):
        raise ContractError("HYPOTHESIS_MIXED_FIXED_AXIS")


def _qualification_catalog(
    value: object, fixed: Mapping[str, Any], report: Mapping[str, Any],
    receipts: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    result = copy.deepcopy(dict(_exact(value, CATALOG_FIELDS, "HYPOTHESIS_CATALOG_FIELDS")))
    if result["schema_version"] != "data_factory.fr5_qualification_catalog.v1":
        raise ContractError("HYPOTHESIS_CATALOG_SCHEMA")
    source = _qualification_source(result["source"], "HYPOTHESIS_CATALOG_SOURCE")
    if result["qualification_status"] != "QUALIFIED":
        raise ContractError("HYPOTHESIS_CATALOG_UNQUALIFIED")
    if (
        result["fixed_contract_digest"] != canonical_digest(fixed)
        or result["coverage_report_digest"] != canonical_digest(report)
        or result["coverage_domain_digest"] != report["domain_digest"]
    ):
        raise ContractError("HYPOTHESIS_CATALOG_BINDING")
    _profile_contract(fixed, report, receipts, source)
    receipt_lookup = {item["resolver_result_digest"]: item for item in receipts}
    if (
        len(receipt_lookup) != len(receipts)
        or result["resolver_result_digests"] != [item["resolver_result_digest"] for item in receipts]
    ):
        raise ContractError("HYPOTHESIS_CATALOG_RESOLVER_DOMAIN")

    qualifications = result["base_condition_qualifications"]
    if not isinstance(qualifications, list) or not qualifications:
        raise ContractError("HYPOTHESIS_CATALOG_BASES")
    bases, base_keys, base_lookup = [], [], {}
    for source_value in qualifications:
        evidence = _base_qualification(source_value)
        if evidence["source"] != source or evidence["resolver_result_digest"] not in receipt_lookup:
            raise ContractError("HYPOTHESIS_CATALOG_SOURCE_MISMATCH")
        receipt = receipt_lookup[evidence["resolver_result_digest"]]
        base = _base_from(report, receipt, evidence)
        _fixed_base(fixed, base, receipt)
        key = coverage_key(base["coverage_condition"])
        base_keys.append(key)
        bases.append(base)
        base_lookup[evidence["qualification_digest"]] = base["base_condition_digest"]
    if base_keys != sorted(base_keys) or len(base_lookup) != len(bases):
        raise ContractError("HYPOTHESIS_CATALOG_BASE_DOMAIN")

    qualifications = result["robot_start_pose_qualifications"]
    if not isinstance(qualifications, list) or not qualifications:
        raise ContractError("HYPOTHESIS_CATALOG_POSES")
    poses, pose_lookup = [], {}
    for source_value in qualifications:
        evidence = _pose_qualification(source_value)
        if evidence["source"] != source or evidence["robot_system_id"] != fixed["robot_system_id"]:
            raise ContractError("HYPOTHESIS_CATALOG_SOURCE_MISMATCH")
        pose = compile_robot_start_pose(qualification=evidence)
        poses.append(pose)
        pose_lookup[evidence["qualification_digest"]] = pose["robot_start_pose_id"]
    pose_ids = [item["robot_start_pose_id"] for item in poses]
    if pose_ids != sorted(pose_ids) or len(pose_lookup) != len(poses):
        raise ContractError("HYPOTHESIS_CATALOG_POSE_DOMAIN")

    sources = result["allowed_pairs"]
    if not isinstance(sources, list) or not sources:
        raise ContractError("HYPOTHESIS_CATALOG_PAIRS")
    pairs, pair_keys = [], []
    for source_value in sources:
        item = _exact(source_value, CATALOG_PAIR_FIELDS, "HYPOTHESIS_CATALOG_PAIR_FIELDS")
        base_digest = _digest(item["base_condition_qualification_digest"], "HYPOTHESIS_CATALOG_PAIR_DIGEST")
        pose_digest = _digest(item["robot_start_pose_qualification_digest"], "HYPOTHESIS_CATALOG_PAIR_DIGEST")
        groups = _pair_groups(item["split_groups"], "HYPOTHESIS_CATALOG_PAIR_GROUPS")
        if base_digest not in base_lookup or pose_digest not in pose_lookup:
            raise ContractError("HYPOTHESIS_CATALOG_PAIR_OUTSIDE_DOMAIN")
        pair_keys.append((base_digest, pose_digest))
        pairs.append({
            "base_condition_digest": base_lookup[base_digest],
            "robot_start_pose_id": pose_lookup[pose_digest],
            "split_groups": copy.deepcopy(groups),
        })
    if pair_keys != sorted(pair_keys) or len(pair_keys) != len(set(pair_keys)):
        raise ContractError("HYPOTHESIS_CATALOG_PAIR_NONCANONICAL")
    _digest(result["catalog_digest"], "HYPOTHESIS_CATALOG_DIGEST")
    if result["catalog_digest"] != canonical_digest({key: result[key] for key in result if key != "catalog_digest"}):
        raise ContractError("HYPOTHESIS_CATALOG_DIGEST_MISMATCH")
    return result, bases, poses, pairs


def _validate_design(
    fixed: Mapping[str, Any], bases: Sequence[Mapping[str, Any]],
    poses: Sequence[Mapping[str, Any]], pairs: Sequence[Mapping[str, Any]],
) -> None:
    yaw_bindings: dict[object, tuple[str, str]] = {}
    action_yaws: dict[str, int | float] = {}
    observations: dict[str, tuple[str, str, str, str, str]] = {}
    policy = (
        fixed["grasp_profile_id"], fixed["pregrasp_digest"],
        fixed["waypoint_digest"], fixed["trajectory_digest"],
    )
    for item in bases:
        condition = item["coverage_condition"]
        yaw = condition["yaw_deg"]
        yaw_domain = (
            (condition["place_id"], condition["cell_calibration_id"], yaw)
            if fixed["schema_version"] == "data_factory.fr5_fixed_contract.v2"
            else yaw
        )
        binding = (item["yaw_action_binding_digest"], item["dual_view_observability_digest"])
        if yaw_domain in yaw_bindings and yaw_bindings[yaw_domain] != binding:
            raise ContractError("HYPOTHESIS_YAW_BINDING_MIXED")
        yaw_bindings[yaw_domain] = binding
        prior_yaw = action_yaws.setdefault(item["yaw_action_binding_digest"], yaw)
        if prior_yaw != yaw:
            raise ContractError("HYPOTHESIS_YAW_ACTION_BINDING_ALIASED")
        observed_policy = (*policy, item["yaw_action_binding_digest"])
        prior = observations.setdefault(item["dual_view_observability_digest"], observed_policy)
        if prior != observed_policy:
            raise ContractError("HYPOTHESIS_UNOBSERVABLE_POLICY_VARIATION")

    condition_ids = {item["base_condition_digest"] for item in bases}
    pose_ids = {item["robot_start_pose_id"] for item in poses}
    eligibility = {group: set() for group in GROUPS}
    pair_keys = set()
    for item in pairs:
        key = (item["base_condition_digest"], item["robot_start_pose_id"])
        if key in pair_keys or key[0] not in condition_ids or key[1] not in pose_ids:
            raise ContractError("HYPOTHESIS_PAIR_OUTSIDE_DOMAIN")
        pair_keys.add(key)
        for group in item["split_groups"]:
            eligibility[group].add(key)
    if _is_test_only_feature_contract(fixed["feature_contract"]):
        if (
            not poses or not pairs
            or any(item["split_groups"] != ["TRAIN"] for item in pairs)
        ):
            raise ContractError("HYPOTHESIS_TEST_ONLY_PROFILE_DESIGN")
        return
    if any(not eligibility[group] for group in GROUPS):
        raise ContractError("HYPOTHESIS_SPLIT_GROUP_EMPTY")
    if not eligibility["ID"].issubset(eligibility["TRAIN"]):
        raise ContractError("HYPOTHESIS_ID_NOT_TRAIN_CELL")
    train_conditions = {condition for condition, _ in eligibility["TRAIN"]}
    train_poses = {pose for _, pose in eligibility["TRAIN"]}
    if any(condition in train_conditions and pose in train_poses for condition, pose in eligibility["OOD"]):
        raise ContractError("HYPOTHESIS_OOD_NOT_FACTOR_HOLDOUT")


def validate_fr5_hypothesis(value: object) -> dict[str, Any]:
    value = _exact(value, HYPOTHESIS_FIELDS, "HYPOTHESIS_FIELDS")
    if value["schema_version"] != "data_factory.fr5_hypothesis.v2":
        raise ContractError("HYPOTHESIS_SCHEMA")
    fixed = _fixed_contract(value["fixed_contract"])
    report = validate_coverage_report(value["coverage_report"])
    if not isinstance(value["resolver_receipts"], list) or not value["resolver_receipts"]:
        raise ContractError("HYPOTHESIS_RESOLVER_RECEIPTS")
    receipts = [_resolver_receipt(item) for item in value["resolver_receipts"]]
    if [item["resolver_result_digest"] for item in receipts] != sorted(item["resolver_result_digest"] for item in receipts):
        raise ContractError("HYPOTHESIS_RESOLVER_RECEIPTS")
    catalog, bases, poses, pairs = _qualification_catalog(
        value["qualification_catalog"], fixed, report, receipts,
    )
    if (
        value["coverage_report"] != report
        or value["resolver_receipts"] != receipts
        or value["base_conditions"] != bases
        or value["robot_start_poses"] != poses
        or value["allowed_pairs"] != pairs
    ):
        raise ContractError("HYPOTHESIS_CATALOG_DERIVATION")
    _validate_design(fixed, bases, poses, pairs)
    _digest(value["hypothesis_digest"], "HYPOTHESIS_DIGEST")
    if value["hypothesis_digest"] != canonical_digest({key: value[key] for key in value if key != "hypothesis_digest"}):
        raise ContractError("HYPOTHESIS_DIGEST_MISMATCH")
    return copy.deepcopy(dict(value))


def compile_fr5_hypothesis(
    *, fixed_contract: Mapping[str, Any], coverage_report: Mapping[str, Any],
    resolver_results: Sequence[Mapping[str, Any]], qualification_catalog: Mapping[str, Any],
) -> dict[str, Any]:
    fixed = _fixed_contract(fixed_contract)
    report = validate_coverage_report(coverage_report)
    if not isinstance(resolver_results, (list, tuple)) or not resolver_results:
        raise ContractError("HYPOTHESIS_RESOLVER_RESULTS")
    receipts = sorted(
        (
            _resolver_result(
                item,
                feature_contract=fixed["feature_contract"],
            )
            for item in resolver_results
        ),
        key=lambda item: item["resolver_result_digest"],
    )
    catalog, bases, poses, pairs = _qualification_catalog(
        qualification_catalog, fixed, report, receipts,
    )
    _validate_design(fixed, bases, poses, pairs)
    draft = {
        "schema_version": "data_factory.fr5_hypothesis.v2",
        "fixed_contract": fixed,
        "coverage_report": report,
        "resolver_receipts": receipts,
        "base_conditions": bases,
        "robot_start_poses": poses,
        "qualification_catalog": catalog,
        "allowed_pairs": pairs,
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
