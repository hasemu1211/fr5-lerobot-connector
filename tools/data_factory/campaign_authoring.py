"""Deterministic, effect-neutral collection campaign authoring contracts.

The compiler selects only pairs already admitted by one frozen P5.8
hypothesis.  Its artifacts describe intent; they never approve planning,
motion, semantic quality, or training.
"""
from __future__ import annotations

import copy
import random
from typing import Any, Mapping, Sequence

from tools.data_factory.experiment_manifest import (
    SLOT_INPUT_FIELDS,
    USAGE_FIELDS,
    _check_budgets,
    _manifest_budget,
    _slot,
    _usage,
    validate_fr5_hypothesis,
)
from tools.data_factory.training_split import GROUPS, validate_program_budget
from tools.data_factory.state_space import validate_state_space_design_profile
from tools.fr5_data_factory import ContractError, DIGEST, SAFE_ID, canonical_digest


DRAFT_SCHEMA = "data_factory.campaign_draft.v1"
DRAFT_SCHEMA_V2 = "data_factory.campaign_draft.v2"
MANIFEST_SCHEMA = "data_factory.collection_campaign_manifest.v1"
MANIFEST_SCHEMA_V2 = "data_factory.collection_campaign_manifest.v2"
RECEIPT_SCHEMA = "data_factory.campaign_compilation_receipt.v1"
SELECTORS = frozenset({"BALANCED_INITIAL", "DIRECT_LIST"})
SELECTOR_VERSION = "campaign-selector-v1"
GROUP_ORDER = {name: index for index, name in enumerate(GROUPS)}

DRAFT_FIELDS = frozenset({
    "schema_version", "draft_id", "revision", "source", "branch", "selector",
    "requested_count", "normalized_seed", "pinned", "excluded", "direct_slots",
    "manifest_id", "manifest_budget", "program_budget",
})
DRAFT_V2_FIELDS = DRAFT_FIELDS | {"state_space_design_profile"}
SOURCE_FIELDS = frozenset({"hypothesis_digest", "catalog_digest", "coverage_digest"})
MANIFEST_FIELDS = frozenset({
    "schema_version", "manifest_id", "kind", "hypothesis_digest", "fixed_contract_digest", "catalog_digest",
    "coverage_digest", "selector", "selector_version", "normalized_seed", "slots",
    "manifest_budget", "program_budget", "planned_usage", "authority", "manifest_digest",
})
MANIFEST_V2_FIELDS = MANIFEST_FIELDS | {"state_space_design_profile"}
RECEIPT_FIELDS = frozenset({
    "schema_version", "receipt_id", "draft_digest", "hypothesis_digest",
    "catalog_digest", "coverage_digest", "eligible_set_digest", "selector",
    "selector_version", "normalized_seed", "score_order", "tie_break",
    "decisions", "selected_manifest_digest", "receipt_digest",
})
DECISION_FIELDS = frozenset({"cell_id", "status", "reason_codes", "score"})
NO_AUTHORITY = "NO_EXECUTION_AUTHORITY"


def _exact(value: object, fields: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(code)
    return value


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ContractError(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ContractError(code)
    return value


def _string_list(value: object, code: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in value)
        or value != sorted(value)
        or len(value) != len(set(value))
    ):
        raise ContractError(code)
    return list(value)


def _source(hypothesis: Mapping[str, Any]) -> dict[str, str]:
    return {
        "hypothesis_digest": hypothesis["hypothesis_digest"],
        "catalog_digest": hypothesis["qualification_catalog"]["catalog_digest"],
        "coverage_digest": canonical_digest(hypothesis["coverage_report"]),
    }


def _validate_source(value: object, hypothesis: Mapping[str, Any]) -> dict[str, str]:
    result = copy.deepcopy(dict(_exact(value, SOURCE_FIELDS, "CAMPAIGN_DRAFT_SOURCE")))
    for item in result.values():
        _digest(item, "CAMPAIGN_DRAFT_SOURCE")
    if result != _source(hypothesis):
        raise ContractError("CAMPAIGN_DRAFT_SOURCE_MISMATCH")
    return result


def validate_campaign_draft(value: object, *, hypothesis: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one mutable session draft without attaching an effect scope."""
    hypothesis = validate_fr5_hypothesis(hypothesis)
    if not isinstance(value, Mapping):
        raise ContractError("CAMPAIGN_DRAFT_FIELDS")
    schema = value.get("schema_version")
    fields = DRAFT_V2_FIELDS if schema == DRAFT_SCHEMA_V2 else DRAFT_FIELDS
    result = copy.deepcopy(dict(_exact(value, fields, "CAMPAIGN_DRAFT_FIELDS")))
    if schema not in {DRAFT_SCHEMA, DRAFT_SCHEMA_V2}:
        raise ContractError("CAMPAIGN_DRAFT_SCHEMA")
    _identifier(result["draft_id"], "CAMPAIGN_DRAFT_ID")
    _identifier(result["manifest_id"], "CAMPAIGN_MANIFEST_ID")
    if type(result["revision"]) is not int or result["revision"] < 0:
        raise ContractError("CAMPAIGN_DRAFT_REVISION")
    result["source"] = _validate_source(result["source"], hypothesis)
    if result["branch"] != "INITIAL_SEED" or result["selector"] not in SELECTORS:
        raise ContractError("CAMPAIGN_DRAFT_SELECTOR")
    if type(result["requested_count"]) is not int or result["requested_count"] <= 0:
        raise ContractError("CAMPAIGN_DRAFT_COUNT")
    if type(result["normalized_seed"]) is not int or result["normalized_seed"] < 0:
        raise ContractError("CAMPAIGN_DRAFT_SEED")
    result["pinned"] = _string_list(result["pinned"], "CAMPAIGN_DRAFT_PINNED")
    result["excluded"] = _string_list(result["excluded"], "CAMPAIGN_DRAFT_EXCLUDED")
    if set(result["pinned"]) & set(result["excluded"]):
        raise ContractError("CAMPAIGN_DRAFT_OVERRIDE_CONFLICT")
    if not isinstance(result["direct_slots"], list):
        raise ContractError("CAMPAIGN_DRAFT_DIRECT")
    result["direct_slots"] = [_slot(item, ordered=False) for item in result["direct_slots"]]
    if result["selector"] == "DIRECT_LIST":
        if len(result["direct_slots"]) != result["requested_count"]:
            raise ContractError("CAMPAIGN_DRAFT_DIRECT")
    elif result["direct_slots"]:
        raise ContractError("CAMPAIGN_DRAFT_DIRECT")
    result["manifest_budget"] = _manifest_budget(result["manifest_budget"])
    result["program_budget"] = validate_program_budget(result["program_budget"])
    if schema == DRAFT_SCHEMA_V2:
        result["state_space_design_profile"] = (
            validate_state_space_design_profile(
                result["state_space_design_profile"],
            )
        )
    forbidden = {
        "effect_scope", "lifecycle_action", "approval", "scene", "lease",
        "execution_authorized", "training_approved",
    }
    if forbidden & set(result):
        raise ContractError("CAMPAIGN_DRAFT_AUTHORITY")
    return result


def campaign_cell_id(
    base_condition_digest: str, robot_start_pose_id: str, split_group: str,
    repeat_index: int,
) -> str:
    """Return a compact stable identifier for one admitted condition/start row."""
    _digest(base_condition_digest, "CAMPAIGN_CELL")
    _identifier(robot_start_pose_id, "CAMPAIGN_CELL")
    if split_group not in GROUPS or type(repeat_index) is not int or repeat_index < 0:
        raise ContractError("CAMPAIGN_CELL")
    suffix = canonical_digest([
        base_condition_digest, robot_start_pose_id, split_group, repeat_index,
    ]).removeprefix("sha256:")[:20]
    return f"cell-{split_group.lower()}-{repeat_index:03d}-{suffix}"


def _coverage_counts(hypothesis: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    result = {}
    for cell in hypothesis["coverage_report"]["cells"]:
        result[canonical_digest(cell["condition"])] = copy.deepcopy(cell["counts"])
    return result


def _base_counts(hypothesis: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    coverage = _coverage_counts(hypothesis)
    result = {}
    for base in hypothesis["base_conditions"]:
        counts = coverage.get(base["coverage_condition_digest"])
        if counts is None:
            raise ContractError("CAMPAIGN_COVERAGE_BINDING")
        result[base["base_condition_digest"]] = counts
    return result


def _score(slot: Mapping[str, Any], counts: Mapping[str, int]) -> list[Any]:
    # Lower is better: least semantic coverage, then least collection/repeat,
    # then the declared split and canonical condition/start identity.
    return [
        counts["human_semantic_pass"] + slot["repeat_index"],
        counts["collected"] + slot["repeat_index"],
        GROUP_ORDER[slot["split_group"]],
        slot["base_condition_digest"],
        slot["robot_start_pose_id"],
        slot["repeat_index"],
    ]


def _candidate_slots(
    hypothesis: Mapping[str, Any], count: int,
    budget_template: Mapping[str, int],
) -> list[dict[str, Any]]:
    candidates = []
    for repeat_index in range(count):
        for pair in hypothesis["allowed_pairs"]:
            for group in pair["split_groups"]:
                candidates.append({
                    "slot_id": campaign_cell_id(
                        pair["base_condition_digest"], pair["robot_start_pose_id"],
                        group, repeat_index,
                    ),
                    "base_condition_digest": pair["base_condition_digest"],
                    "robot_start_pose_id": pair["robot_start_pose_id"],
                    "split_group": group,
                    "repeat_index": repeat_index,
                    "hil_prompts": budget_template["hil_prompts"],
                    "reviews": budget_template["reviews"],
                    "pending_reviews": budget_template["pending_reviews"],
                    "storage_bytes": budget_template["storage_bytes"],
                })
    return candidates


def _slot_template(draft: Mapping[str, Any]) -> dict[str, int]:
    count = draft["requested_count"]
    budget = draft["manifest_budget"]
    return {
        "hil_prompts": min(1, budget["max_hil_prompts"] // count),
        "reviews": min(1, budget["max_reviews"] // count),
        "pending_reviews": 0,
        "storage_bytes": max(1, budget["max_storage_bytes"] // count),
    }


def _allowed(hypothesis: Mapping[str, Any]) -> dict[tuple[str, str], set[str]]:
    return {
        (pair["base_condition_digest"], pair["robot_start_pose_id"]): set(pair["split_groups"])
        for pair in hypothesis["allowed_pairs"]
    }


def _validate_selected_slots(
    slots: Sequence[Mapping[str, Any]], hypothesis: Mapping[str, Any], code: str,
) -> list[dict[str, Any]]:
    allowed = _allowed(hypothesis)
    result = [_slot(item, ordered=False) for item in slots]
    keys = []
    for item in result:
        pair = (item["base_condition_digest"], item["robot_start_pose_id"])
        if pair not in allowed or item["split_group"] not in allowed[pair]:
            raise ContractError(code)
        expected_id = campaign_cell_id(*pair, item["split_group"], item["repeat_index"])
        if item["slot_id"] != expected_id:
            raise ContractError(code)
        keys.append((item["split_group"], *pair, item["repeat_index"]))
    if len(keys) != len(set(keys)):
        raise ContractError(code)
    return result


def _decisions(
    considered: Sequence[Mapping[str, Any]], selected_ids: set[str],
    excluded_ids: set[str], pinned_ids: set[str], counts: Mapping[str, Mapping[str, int]],
) -> list[dict[str, Any]]:
    result = []
    for item in sorted(considered, key=lambda slot: slot["slot_id"]):
        pending = counts[item["base_condition_digest"]]["pending_review"] > 0
        if item["slot_id"] in selected_ids:
            reasons = ["USER_PINNED"] if item["slot_id"] in pinned_ids else ["COVERAGE_DEFICIT"]
            status = "SELECTED"
        elif item["slot_id"] in excluded_ids:
            reasons, status = ["USER_EXCLUDED"], "EXCLUDED"
        elif pending:
            reasons, status = ["PENDING_REVIEW"], "INELIGIBLE"
        else:
            reasons, status = ["NOT_SELECTED_COUNT_LIMIT"], "ELIGIBLE_NOT_SELECTED"
        result.append({
            "cell_id": item["slot_id"], "status": status,
            "reason_codes": reasons, "score": _score(item, counts[item["base_condition_digest"]]),
        })
    return result


def validate_collection_campaign_manifest(
    value: object, *, hypothesis: Mapping[str, Any],
) -> dict[str, Any]:
    hypothesis = validate_fr5_hypothesis(hypothesis)
    if not isinstance(value, Mapping):
        raise ContractError("COLLECTION_MANIFEST_FIELDS")
    schema = value.get("schema_version")
    fields = MANIFEST_V2_FIELDS if schema == MANIFEST_SCHEMA_V2 else MANIFEST_FIELDS
    result = copy.deepcopy(dict(_exact(value, fields, "COLLECTION_MANIFEST_FIELDS")))
    if schema not in {MANIFEST_SCHEMA, MANIFEST_SCHEMA_V2} or result["kind"] != "collection":
        raise ContractError("COLLECTION_MANIFEST_SCHEMA")
    _identifier(result["manifest_id"], "CAMPAIGN_MANIFEST_ID")
    expected_source = _source(hypothesis)
    if any(result[field] != expected_source[field] for field in expected_source):
        raise ContractError("COLLECTION_MANIFEST_SOURCE_MISMATCH")
    if result["fixed_contract_digest"] != canonical_digest(hypothesis["fixed_contract"]):
        raise ContractError("COLLECTION_MANIFEST_FIXED_DIGEST")
    if result["selector"] not in SELECTORS or result["selector_version"] != SELECTOR_VERSION:
        raise ContractError("COLLECTION_MANIFEST_SELECTOR")
    if type(result["normalized_seed"]) is not int or result["normalized_seed"] < 0:
        raise ContractError("COLLECTION_MANIFEST_SEED")
    if not isinstance(result["slots"], list) or not result["slots"]:
        raise ContractError("COLLECTION_MANIFEST_SLOTS")
    ordered = []
    for index, item in enumerate(result["slots"]):
        slot = _slot(item, ordered=True)
        if slot["order_index"] != index:
            raise ContractError("COLLECTION_MANIFEST_ORDER")
        ordered.append(slot)
    source_slots = [{key: item[key] for key in SLOT_INPUT_FIELDS} for item in ordered]
    _validate_selected_slots(source_slots, hypothesis, "COLLECTION_MANIFEST_DISALLOWED_PAIR")
    if len({item["slot_id"] for item in ordered}) != len(ordered):
        raise ContractError("COLLECTION_MANIFEST_DUPLICATE")
    manifest_budget = _manifest_budget(result["manifest_budget"])
    program_budget = validate_program_budget(result["program_budget"])
    planned = _usage("seed", ordered)
    if _exact(result["planned_usage"], USAGE_FIELDS, "COLLECTION_MANIFEST_USAGE") != planned:
        raise ContractError("COLLECTION_MANIFEST_USAGE")
    _check_budgets(manifest_budget, program_budget, planned)
    if result["authority"] != NO_AUTHORITY:
        raise ContractError("COLLECTION_MANIFEST_AUTHORITY")
    if schema == MANIFEST_SCHEMA_V2:
        result["state_space_design_profile"] = (
            validate_state_space_design_profile(
                result["state_space_design_profile"],
            )
        )
    if result["manifest_digest"] != canonical_digest({key: result[key] for key in result if key != "manifest_digest"}):
        raise ContractError("COLLECTION_MANIFEST_DIGEST_MISMATCH")
    return result


def _validate_state_space_design_binding(
    draft: Mapping[str, Any], manifest: Mapping[str, Any],
) -> None:
    draft_v2 = draft["schema_version"] == DRAFT_SCHEMA_V2
    manifest_v2 = manifest["schema_version"] == MANIFEST_SCHEMA_V2
    if (
        draft_v2 != manifest_v2
        or draft_v2
        and draft["state_space_design_profile"]
        != manifest["state_space_design_profile"]
    ):
        raise ContractError("CAMPAIGN_STATE_SPACE_DESIGN_BINDING")


def validate_campaign_compilation_receipt(
    value: object, *, draft: Mapping[str, Any], manifest: Mapping[str, Any],
    hypothesis: Mapping[str, Any],
) -> dict[str, Any]:
    hypothesis = validate_fr5_hypothesis(hypothesis)
    draft = validate_campaign_draft(draft, hypothesis=hypothesis)
    manifest = validate_collection_campaign_manifest(manifest, hypothesis=hypothesis)
    _validate_state_space_design_binding(draft, manifest)
    result = copy.deepcopy(dict(_exact(value, RECEIPT_FIELDS, "CAMPAIGN_RECEIPT_FIELDS")))
    if result["schema_version"] != RECEIPT_SCHEMA:
        raise ContractError("CAMPAIGN_RECEIPT_SCHEMA")
    _identifier(result["receipt_id"], "CAMPAIGN_RECEIPT_ID")
    source = _source(hypothesis)
    if (
        result["draft_digest"] != canonical_digest(draft)
        or any(result[field] != source[field] for field in source)
        or result["selector"] != draft["selector"]
        or result["selector_version"] != SELECTOR_VERSION
        or result["normalized_seed"] != draft["normalized_seed"]
        or result["selected_manifest_digest"] != manifest["manifest_digest"]
    ):
        raise ContractError("CAMPAIGN_RECEIPT_BINDING")
    _digest(result["eligible_set_digest"], "CAMPAIGN_RECEIPT_ELIGIBLE")
    if result["score_order"] != [
        "human_semantic_pass_plus_repeat", "collected_plus_repeat", "split_group",
        "base_condition_digest", "robot_start_pose_id", "repeat_index",
    ] or result["tie_break"] != "CANONICAL_CELL_ID_ASC":
        raise ContractError("CAMPAIGN_RECEIPT_SELECTOR")
    if not isinstance(result["decisions"], list) or not result["decisions"]:
        raise ContractError("CAMPAIGN_RECEIPT_DECISIONS")
    ids = []
    for decision in result["decisions"]:
        item = _exact(decision, DECISION_FIELDS, "CAMPAIGN_RECEIPT_DECISION")
        ids.append(_identifier(item["cell_id"], "CAMPAIGN_RECEIPT_DECISION"))
        if item["status"] not in {"SELECTED", "EXCLUDED", "INELIGIBLE", "ELIGIBLE_NOT_SELECTED"}:
            raise ContractError("CAMPAIGN_RECEIPT_DECISION")
        if not isinstance(item["reason_codes"], list) or not item["reason_codes"] or any(
            not isinstance(code, str) or not SAFE_ID.fullmatch(code) for code in item["reason_codes"]
        ) or not isinstance(item["score"], list):
            raise ContractError("CAMPAIGN_RECEIPT_DECISION")
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise ContractError("CAMPAIGN_RECEIPT_DECISIONS")
    selected = {item["slot_id"] for item in manifest["slots"]}
    if {item["cell_id"] for item in result["decisions"] if item["status"] == "SELECTED"} != selected:
        raise ContractError("CAMPAIGN_RECEIPT_BINDING")
    if result["receipt_digest"] != canonical_digest({key: result[key] for key in result if key != "receipt_digest"}):
        raise ContractError("CAMPAIGN_RECEIPT_DIGEST_MISMATCH")
    expected_manifest, expected_receipt = _compile_documents(draft, hypothesis=hypothesis)
    if manifest != expected_manifest or result != expected_receipt:
        raise ContractError("CAMPAIGN_RECEIPT_BINDING")
    return result


def _compile_documents(
    draft: Mapping[str, Any], *, hypothesis: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    hypothesis = validate_fr5_hypothesis(hypothesis)
    draft = validate_campaign_draft(draft, hypothesis=hypothesis)
    counts = _base_counts(hypothesis)
    if draft["selector"] == "DIRECT_LIST":
        considered = _validate_selected_slots(
            draft["direct_slots"], hypothesis, "CAMPAIGN_DIRECT_DISALLOWED_PAIR",
        )
        if any(counts[item["base_condition_digest"]]["pending_review"] for item in considered):
            raise ContractError("CAMPAIGN_PENDING_REVIEW")
        selected = list(considered)
        selected_ids = {item["slot_id"] for item in selected}
        if draft["pinned"] and not set(draft["pinned"]).issubset(selected_ids):
            raise ContractError("CAMPAIGN_PIN_OUTSIDE_SELECTION")
        if set(draft["excluded"]) & selected_ids:
            raise ContractError("CAMPAIGN_DIRECT_EXCLUDED")
    else:
        considered = _candidate_slots(hypothesis, draft["requested_count"], _slot_template(draft))
        by_id = {item["slot_id"]: item for item in considered}
        if not set(draft["pinned"] + draft["excluded"]).issubset(by_id):
            raise ContractError("CAMPAIGN_OVERRIDE_OUTSIDE_DOMAIN")
        if len(draft["pinned"]) > draft["requested_count"]:
            raise ContractError("CAMPAIGN_PIN_COUNT")
        eligible = [
            item for item in considered
            if item["slot_id"] not in set(draft["excluded"])
            and counts[item["base_condition_digest"]]["pending_review"] == 0
        ]
        eligible_ids = {item["slot_id"] for item in eligible}
        if not set(draft["pinned"]).issubset(eligible_ids):
            raise ContractError("CAMPAIGN_PIN_INELIGIBLE")
        pinned = [by_id[item] for item in draft["pinned"]]
        remaining = sorted(
            (item for item in eligible if item["slot_id"] not in set(draft["pinned"])),
            key=lambda item: (_score(item, counts[item["base_condition_digest"]]), item["slot_id"]),
        )
        selected = pinned + remaining[: draft["requested_count"] - len(pinned)]
        if len(selected) != draft["requested_count"]:
            raise ContractError("CAMPAIGN_NO_ELIGIBLE_CELL")
        selected_ids = {item["slot_id"] for item in selected}

    selected = copy.deepcopy(selected)
    if draft["selector"] == "DIRECT_LIST":
        # Direct authoring is an explicit sequence; canonical validation makes
        # the caller's order byte-stable without silently reshuffling it.
        pass
    else:
        pinned_ids = set(draft["pinned"])
        anchors = sorted(
            (item for item in selected if item["slot_id"] in pinned_ids),
            key=lambda item: item["slot_id"],
        )
        remainder = sorted(
            (item for item in selected if item["slot_id"] not in pinned_ids),
            key=lambda item: item["slot_id"],
        )
        random.Random(draft["normalized_seed"]).shuffle(remainder)
        selected = anchors + remainder
    ordered = [{**item, "order_index": index} for index, item in enumerate(selected)]
    manifest = {
        "schema_version": (
            MANIFEST_SCHEMA_V2
            if draft["schema_version"] == DRAFT_SCHEMA_V2
            else MANIFEST_SCHEMA
        ),
        "manifest_id": draft["manifest_id"],
        "kind": "collection",
        **_source(hypothesis),
        "fixed_contract_digest": canonical_digest(hypothesis["fixed_contract"]),
        "selector": draft["selector"],
        "selector_version": SELECTOR_VERSION,
        "normalized_seed": draft["normalized_seed"],
        "slots": ordered,
        "manifest_budget": copy.deepcopy(draft["manifest_budget"]),
        "program_budget": copy.deepcopy(draft["program_budget"]),
        "planned_usage": _usage("seed", ordered),
        "authority": NO_AUTHORITY,
        **(
            {
                "state_space_design_profile": copy.deepcopy(
                    draft["state_space_design_profile"],
                ),
            }
            if draft["schema_version"] == DRAFT_SCHEMA_V2 else {}
        ),
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    decisions = _decisions(
        considered, selected_ids, set(draft["excluded"]), set(draft["pinned"]), counts,
    )
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "receipt_id": f"{draft['manifest_id']}-compile",
        "draft_digest": canonical_digest(draft),
        **_source(hypothesis),
        "eligible_set_digest": canonical_digest([
            item["cell_id"] for item in decisions if item["status"] != "INELIGIBLE"
        ]),
        "selector": draft["selector"],
        "selector_version": SELECTOR_VERSION,
        "normalized_seed": draft["normalized_seed"],
        "score_order": [
            "human_semantic_pass_plus_repeat", "collected_plus_repeat", "split_group",
            "base_condition_digest", "robot_start_pose_id", "repeat_index",
        ],
        "tie_break": "CANONICAL_CELL_ID_ASC",
        "decisions": decisions,
        "selected_manifest_digest": manifest["manifest_digest"],
    }
    receipt["receipt_digest"] = canonical_digest(receipt)
    return manifest, receipt


def compile_collection_campaign(
    draft: Mapping[str, Any], *, hypothesis: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile a byte-stable subset manifest and its mandatory receipt."""
    manifest, receipt = _compile_documents(draft, hypothesis=hypothesis)
    manifest = validate_collection_campaign_manifest(manifest, hypothesis=hypothesis)
    receipt = validate_campaign_compilation_receipt(
        receipt, draft=draft, manifest=manifest, hypothesis=hypothesis,
    )
    return manifest, receipt


def direct_draft_from_manifest(
    draft: Mapping[str, Any], manifest: Mapping[str, Any], *, hypothesis: Mapping[str, Any],
) -> dict[str, Any]:
    """Round-trip an automatic result into the same authoring draft shape."""
    draft = validate_campaign_draft(draft, hypothesis=hypothesis)
    manifest = validate_collection_campaign_manifest(manifest, hypothesis=hypothesis)
    _validate_state_space_design_binding(draft, manifest)
    result = copy.deepcopy(draft)
    result["revision"] += 1
    result["selector"] = "DIRECT_LIST"
    result["requested_count"] = len(manifest["slots"])
    result["direct_slots"] = [
        {key: item[key] for key in SLOT_INPUT_FIELDS} for item in manifest["slots"]
    ]
    selected = {item["slot_id"] for item in result["direct_slots"]}
    result["pinned"] = sorted(selected & set(result["pinned"]))
    result["excluded"] = sorted(set(result["excluded"]) - selected)
    return validate_campaign_draft(result, hypothesis=hypothesis)
