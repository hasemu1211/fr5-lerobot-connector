"""Pure offline P6.5 recollection selection and finite manifest contracts."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from tools.data_factory.motion.trajectory_variants import phase_variant_catalog
from tools.data_factory.quality.coverage_report import validate_coverage_report
from tools.fr5_data_factory import ContractError, DIGEST, RFC3339, SAFE_ID, canonical_digest


FAILURE_SCHEMA = "data_factory.synthetic_rollout_failure_evidence.v1"
DECISION_SCHEMA = "data_factory.p6_decision_evidence.v1"
SELECTION_SCHEMA = "data_factory.recollection_selection.v1"
MANIFEST_SCHEMA = "data_factory.recollection_manifest.v1"
MODES = frozenset({"NOMINAL", "VARIANT_TARGETED"})
_MODE_VARIANTS = {"NOMINAL": "DIRECT", "VARIANT_TARGETED": "TWO_STAGE_ALIGN"}
_VARIANT_CATALOG = phase_variant_catalog()
_VARIANT_DIGESTS = {
    item["trajectory_variant_id"]: item["variation_profile_digest"]
    for item in _VARIANT_CATALOG["variants"]
}
FAILURE_FIELDS = frozenset({
    "schema_version", "source", "dataset_digest", "checkpoint_digest",
    "coverage_report_digest", "mode", "variant_id", "variant_digest",
    "under_covered_below", "failures", "failure_evidence_digest",
})
FAILURE_ITEM_FIELDS = frozenset({
    "failure_id", "condition_digest", "qualification_status",
    "condition_qualification_digest", "expected_decision_impact", "phase",
    "reason", "evidence_digest",
})
DECISION_FIELDS = frozenset({
    "schema_version", "source", "dataset_digest", "checkpoint_digest",
    "variant_id", "variant_digest", "variant_catalog_digest",
    "eligibility_status", "ablation_evidence_digest", "decision_digest",
})
SELECTION_FIELDS = frozenset({
    "schema_version", "mode", "target_condition", "coverage_count",
    "bindings", "authority", "selection_digest",
})
BINDING_FIELDS = frozenset({
    "dataset_digest", "checkpoint_digest", "coverage_report_digest",
    "failure_evidence_digest", "selected_failure_digest",
    "selected_failure_evidence_digest", "condition_digest",
    "condition_qualification_digest", "variant_id", "variant_digest",
    "p6_decision_digest",
})
SLOT_INPUT_FIELDS = frozenset({
    "slot_id", "episode_id", "condition_digest", "variant_id",
    "variant_digest", "hil_prompts", "reviews", "pending_reviews",
    "storage_bytes",
})
SLOT_FIELDS = SLOT_INPUT_FIELDS | {"order_index"}
BUDGET_FIELDS = frozenset({
    "max_slots", "used_slots", "max_episodes", "used_episodes",
    "max_hil_prompts", "used_hil_prompts", "max_reviews", "used_reviews",
    "max_pending_reviews", "used_pending_reviews", "max_storage_bytes",
    "used_storage_bytes", "expires_at",
})
USAGE_FIELDS = frozenset({
    "slots", "episodes", "hil_prompts", "reviews", "pending_reviews",
    "storage_bytes",
})
MANIFEST_FIELDS = frozenset({
    "schema_version", "manifest_id", "mode", "target_condition", "bindings",
    "slots", "budget", "planned_usage", "authority", "manifest_digest",
})


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


def _self_digest(value: Mapping[str, Any], field: str, code: str) -> None:
    if _digest(value[field], code) != canonical_digest({key: item for key, item in value.items() if key != field}):
        raise ContractError(code)


def _failure_item(value: object) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, FAILURE_ITEM_FIELDS, "RECOLLECTION_FAILURE_FIELDS")))
    _id(result["failure_id"], "RECOLLECTION_FAILURE_ID")
    _digest(result["condition_digest"], "RECOLLECTION_FAILURE_DIGEST")
    _digest(result["condition_qualification_digest"], "RECOLLECTION_FAILURE_DIGEST")
    _digest(result["evidence_digest"], "RECOLLECTION_FAILURE_DIGEST")
    if result["qualification_status"] not in {"QUALIFIED", "UNQUALIFIED"}:
        raise ContractError("RECOLLECTION_FAILURE_QUALIFICATION")
    if type(result["expected_decision_impact"]) is not bool:
        raise ContractError("RECOLLECTION_FAILURE_IMPACT")
    _id(result["phase"], "RECOLLECTION_FAILURE_PHASE")
    _id(result["reason"], "RECOLLECTION_FAILURE_REASON")
    return result


def validate_rollout_failure_evidence(value: object) -> dict[str, Any]:
    """Validate canonical synthetic rollout-failure evidence without side effects."""
    value = _exact(value, FAILURE_FIELDS, "RECOLLECTION_FAILURE_EVIDENCE_FIELDS")
    result = copy.deepcopy(dict(value))
    if result["schema_version"] != FAILURE_SCHEMA or result["source"] != "SYNTHETIC_TEST_ONLY":
        raise ContractError("RECOLLECTION_FAILURE_EVIDENCE_SCHEMA")
    for field in ("dataset_digest", "checkpoint_digest", "coverage_report_digest", "variant_digest"):
        _digest(result[field], "RECOLLECTION_FAILURE_EVIDENCE_DIGEST")
    mode = result["mode"]
    if mode not in MODES:
        raise ContractError("RECOLLECTION_MODE")
    _id(result["variant_id"], "RECOLLECTION_VARIANT_ID")
    if result["variant_id"] != _MODE_VARIANTS[mode]:
        raise ContractError("RECOLLECTION_VARIANT_MODE")
    if result["variant_digest"] != _VARIANT_DIGESTS[result["variant_id"]]:
        raise ContractError("RECOLLECTION_VARIANT_BINDING")
    _count(result["under_covered_below"], "RECOLLECTION_COVERAGE_TARGET", positive=True)
    if not isinstance(result["failures"], list):
        raise ContractError("RECOLLECTION_FAILURES")
    failures = sorted((_failure_item(item) for item in result["failures"]), key=canonical_digest)
    if len({item["failure_id"] for item in failures}) != len(failures):
        raise ContractError("RECOLLECTION_FAILURE_DUPLICATE")
    result["failures"] = failures
    _self_digest(result, "failure_evidence_digest", "RECOLLECTION_FAILURE_EVIDENCE_DIGEST_MISMATCH")
    return result


def validate_p6_decision_evidence(value: object) -> dict[str, Any]:
    """Validate one observed-eligible P6 decision artifact."""
    result = copy.deepcopy(dict(_exact(value, DECISION_FIELDS, "RECOLLECTION_DECISION_FIELDS")))
    if (
        result["schema_version"] != DECISION_SCHEMA
        or result["source"] not in {"DECISION_ARTIFACT", "SYNTHETIC_TEST_ONLY"}
        or result["eligibility_status"] != "OBSERVED_ELIGIBLE"
    ):
        raise ContractError("RECOLLECTION_DECISION_INELIGIBLE")
    _id(result["variant_id"], "RECOLLECTION_DECISION_VARIANT")
    if result["variant_id"] != "TWO_STAGE_ALIGN":
        raise ContractError("RECOLLECTION_DECISION_VARIANT")
    for field in (
        "dataset_digest", "checkpoint_digest", "variant_digest",
        "variant_catalog_digest", "ablation_evidence_digest",
    ):
        _digest(result[field], "RECOLLECTION_DECISION_DIGEST")
    if (
        result["variant_digest"] != _VARIANT_DIGESTS[result["variant_id"]]
        or result["variant_catalog_digest"] != _VARIANT_CATALOG["catalog_digest"]
    ):
        raise ContractError("RECOLLECTION_DECISION_VARIANT_BINDING")
    _self_digest(result, "decision_digest", "RECOLLECTION_DECISION_DIGEST_MISMATCH")
    return result


def _validated_sources(
    failure_evidence: Mapping[str, Any], coverage_report: Mapping[str, Any],
    p6_decision_evidence: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    evidence = validate_rollout_failure_evidence(failure_evidence)
    report = validate_coverage_report(coverage_report)
    if evidence["coverage_report_digest"] != canonical_digest(report):
        raise ContractError("RECOLLECTION_EVIDENCE_COVERAGE_DIGEST")
    if evidence["mode"] == "NOMINAL":
        if p6_decision_evidence is not None:
            raise ContractError("RECOLLECTION_UNEXPECTED_P6_DECISION")
        return evidence, report, None
    if p6_decision_evidence is None:
        raise ContractError("RECOLLECTION_P6_DECISION_REQUIRED")
    decision = validate_p6_decision_evidence(p6_decision_evidence)
    if any(decision[field] != evidence[field] for field in (
        "dataset_digest", "checkpoint_digest", "variant_id", "variant_digest",
    )):
        raise ContractError("RECOLLECTION_P6_DECISION_BINDING")
    return evidence, report, decision


def select_recollection_target(
    *, failure_evidence: Mapping[str, Any], coverage_report: Mapping[str, Any],
    p6_decision_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Choose one qualified impacted weak cell, or ``None`` when none exists."""
    evidence, report, decision = _validated_sources(
        failure_evidence, coverage_report, p6_decision_evidence,
    )
    cells = {canonical_digest(cell["condition"]): cell for cell in report["cells"]}
    named = {item["condition_digest"] for item in evidence["failures"]}
    if not named.issubset(cells):
        raise ContractError("RECOLLECTION_EVIDENCE_COVERAGE_DISAGREEMENT")

    by_condition: dict[str, list[dict[str, Any]]] = {}
    for failure in evidence["failures"]:
        cell = cells[failure["condition_digest"]]
        if (
            failure["qualification_status"] == "QUALIFIED"
            and failure["expected_decision_impact"]
            and cell["counts"]["human_semantic_pass"] < evidence["under_covered_below"]
        ):
            by_condition.setdefault(failure["condition_digest"], []).append(failure)
    if not by_condition:
        return None
    for failures in by_condition.values():
        if len({item["condition_qualification_digest"] for item in failures}) != 1:
            raise ContractError("RECOLLECTION_FAILURE_QUALIFICATION_CONFLICT")

    condition_digest = min(
        by_condition,
        key=lambda item: (cells[item]["counts"]["human_semantic_pass"], item),
    )
    selected_failure = min(by_condition[condition_digest], key=canonical_digest)
    bindings = {
        "dataset_digest": evidence["dataset_digest"],
        "checkpoint_digest": evidence["checkpoint_digest"],
        "coverage_report_digest": evidence["coverage_report_digest"],
        "failure_evidence_digest": evidence["failure_evidence_digest"],
        "selected_failure_digest": canonical_digest(selected_failure),
        "selected_failure_evidence_digest": selected_failure["evidence_digest"],
        "condition_digest": condition_digest,
        "condition_qualification_digest": selected_failure["condition_qualification_digest"],
        "variant_id": evidence["variant_id"],
        "variant_digest": evidence["variant_digest"],
        "p6_decision_digest": None if decision is None else decision["decision_digest"],
    }
    result = {
        "schema_version": SELECTION_SCHEMA,
        "mode": evidence["mode"],
        "target_condition": copy.deepcopy(cells[condition_digest]["condition"]),
        "coverage_count": cells[condition_digest]["counts"]["human_semantic_pass"],
        "bindings": bindings,
        "authority": "REPORT_ONLY",
    }
    result["selection_digest"] = canonical_digest(result)
    return result


def _slot(value: object, *, ordered: bool) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, SLOT_FIELDS if ordered else SLOT_INPUT_FIELDS, "RECOLLECTION_SLOT_FIELDS")))
    _id(result["slot_id"], "RECOLLECTION_SLOT_ID")
    _id(result["episode_id"], "RECOLLECTION_EPISODE_ID")
    _id(result["variant_id"], "RECOLLECTION_SLOT_VARIANT")
    _digest(result["condition_digest"], "RECOLLECTION_SLOT_DIGEST")
    _digest(result["variant_digest"], "RECOLLECTION_SLOT_DIGEST")
    for field in ("hil_prompts", "reviews", "pending_reviews", "storage_bytes"):
        _count(result[field], "RECOLLECTION_SLOT_BUDGET", positive=True)
    if ordered:
        _count(result["order_index"], "RECOLLECTION_SLOT_ORDER")
    return result


def _future_timestamp(value: object, now: datetime) -> str:
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        raise ContractError("RECOLLECTION_EXPIRY")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("RECOLLECTION_EXPIRY") from exc
    if parsed.tzinfo is None or parsed <= now.astimezone(timezone.utc):
        raise ContractError("RECOLLECTION_EXPIRED")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _budget(value: object, now: datetime) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, BUDGET_FIELDS, "RECOLLECTION_BUDGET_FIELDS")))
    for field in BUDGET_FIELDS - {"expires_at"}:
        _count(result[field], "RECOLLECTION_BUDGET_VALUE", positive=field.startswith("max_"))
    for resource in ("slots", "episodes", "hil_prompts", "reviews", "pending_reviews", "storage_bytes"):
        if result[f"used_{resource}"] > result[f"max_{resource}"]:
            raise ContractError("RECOLLECTION_BUDGET_VALUE")
    result["expires_at"] = _future_timestamp(result["expires_at"], now)
    return result


def _usage(slots: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "slots": len(slots),
        "episodes": len(slots),
        "hil_prompts": sum(item["hil_prompts"] for item in slots),
        "reviews": sum(item["reviews"] for item in slots),
        "pending_reviews": sum(item["pending_reviews"] for item in slots),
        "storage_bytes": sum(item["storage_bytes"] for item in slots),
    }


def _check_budget(budget: Mapping[str, Any], usage: Mapping[str, int]) -> None:
    if any(budget[f"used_{resource}"] + usage[resource] > budget[f"max_{resource}"] for resource in USAGE_FIELDS):
        raise ContractError("RECOLLECTION_BUDGET_EXHAUSTED")


def _selection_bindings(selection: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(_exact(selection["bindings"], BINDING_FIELDS, "RECOLLECTION_BINDING_FIELDS")))


def validate_recollection_manifest(
    value: object, *, failure_evidence: Mapping[str, Any],
    coverage_report: Mapping[str, Any],
    p6_decision_evidence: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate an immutable non-authoritative recollection manifest."""
    result = copy.deepcopy(dict(_exact(value, MANIFEST_FIELDS, "RECOLLECTION_MANIFEST_FIELDS")))
    if result["schema_version"] != MANIFEST_SCHEMA:
        raise ContractError("RECOLLECTION_MANIFEST_SCHEMA")
    _id(result["manifest_id"], "RECOLLECTION_MANIFEST_ID")
    selection = select_recollection_target(
        failure_evidence=failure_evidence, coverage_report=coverage_report,
        p6_decision_evidence=p6_decision_evidence,
    )
    if selection is None:
        raise ContractError("RECOLLECTION_TARGET_UNAVAILABLE")
    if (
        result["mode"] != selection["mode"]
        or result["target_condition"] != selection["target_condition"]
        or _selection_bindings(result) != selection["bindings"]
    ):
        raise ContractError("RECOLLECTION_MANIFEST_BINDING")
    if not isinstance(result["slots"], list) or not result["slots"]:
        raise ContractError("RECOLLECTION_SLOTS")
    slots = [_slot(item, ordered=True) for item in result["slots"]]
    if [item["order_index"] for item in slots] != list(range(len(slots))):
        raise ContractError("RECOLLECTION_SLOT_ORDER")
    source_slots = [{key: item[key] for key in SLOT_INPUT_FIELDS} for item in slots]
    if source_slots != sorted(source_slots, key=lambda item: (item["slot_id"], item["episode_id"])):
        raise ContractError("RECOLLECTION_SLOT_ORDER")
    if len({item["slot_id"] for item in slots}) != len(slots) or len({item["episode_id"] for item in slots}) != len(slots):
        raise ContractError("RECOLLECTION_SLOT_DUPLICATE")
    bindings = selection["bindings"]
    if any(
        item["condition_digest"] != bindings["condition_digest"]
        or item["variant_id"] != bindings["variant_id"]
        or item["variant_digest"] != bindings["variant_digest"]
        for item in slots
    ):
        raise ContractError("RECOLLECTION_SLOT_BINDING")
    check_now = now or datetime.now(timezone.utc)
    if check_now.tzinfo is None:
        raise ContractError("RECOLLECTION_NOW")
    budget = _budget(result["budget"], check_now)
    if budget != result["budget"]:
        raise ContractError("RECOLLECTION_BUDGET_CANONICAL")
    usage = _usage(slots)
    if _exact(result["planned_usage"], USAGE_FIELDS, "RECOLLECTION_USAGE") != usage:
        raise ContractError("RECOLLECTION_USAGE")
    _check_budget(budget, usage)
    if result["authority"] != "NO_EXECUTION_AUTHORITY":
        raise ContractError("RECOLLECTION_AUTHORITY")
    _self_digest(result, "manifest_digest", "RECOLLECTION_MANIFEST_DIGEST_MISMATCH")
    return result


def compile_recollection_manifest(
    *, manifest_id: str, failure_evidence: Mapping[str, Any],
    coverage_report: Mapping[str, Any], slots: Sequence[Mapping[str, Any]],
    budget: Mapping[str, Any], p6_decision_evidence: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Compile one finite manifest, or ``None`` when evidence names no useful cell."""
    _id(manifest_id, "RECOLLECTION_MANIFEST_ID")
    selection = select_recollection_target(
        failure_evidence=failure_evidence, coverage_report=coverage_report,
        p6_decision_evidence=p6_decision_evidence,
    )
    if selection is None:
        return None
    if not isinstance(slots, (list, tuple)) or not slots:
        raise ContractError("RECOLLECTION_SLOTS")
    normalized = [_slot(item, ordered=False) for item in slots]
    normalized.sort(key=lambda item: (item["slot_id"], item["episode_id"]))
    ordered = [{**item, "order_index": index} for index, item in enumerate(normalized)]
    check_now = now or datetime.now(timezone.utc)
    if check_now.tzinfo is None:
        raise ContractError("RECOLLECTION_NOW")
    normalized_budget = _budget(budget, check_now)
    draft = {
        "schema_version": MANIFEST_SCHEMA,
        "manifest_id": manifest_id,
        "mode": selection["mode"],
        "target_condition": selection["target_condition"],
        "bindings": selection["bindings"],
        "slots": ordered,
        "budget": normalized_budget,
        "planned_usage": _usage(ordered),
        "authority": "NO_EXECUTION_AUTHORITY",
    }
    draft["manifest_digest"] = canonical_digest(draft)
    return validate_recollection_manifest(
        draft, failure_evidence=failure_evidence, coverage_report=coverage_report,
        p6_decision_evidence=p6_decision_evidence, now=check_now,
    )
