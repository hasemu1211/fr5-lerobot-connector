"""Immutable campaign envelope, one authorization, and episode containment."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Mapping

from tools.data_factory.campaign_authoring import (
    validate_campaign_compilation_receipt,
    validate_campaign_draft,
    validate_collection_campaign_manifest,
)
from tools.data_factory.experiment_manifest import validate_fr5_hypothesis
from tools.fr5_data_factory import ContractError, DIGEST, RFC3339, SAFE_ID, canonical_digest


ENVELOPE_SCHEMA = "data_factory.campaign_execution_envelope.v1"
SCHEMA = "data_factory.campaign_authorization.v1"
DECISION_MODE = "ONE_CAMPAIGN_APPROVAL_NEGATIVE_FEEDBACK"
ENVELOPE_AUTHORITY = {
    "plan": "VALIDATED_PROGRAM_WITHIN_COMPILED_SLOT",
    "scope_expansion": "NONE",
    "semantic_pass": "NONE",
    "training_approval": "NONE",
}
AUTHORITY = {
    "execution": "ENVELOPE_CONTAINMENT_ONLY",
    "scope_expansion": "NONE",
    "semantic_pass": "NONE",
    "training_approval": "NONE",
}
ENVELOPE_FIELDS = frozenset({
    "schema_version", "draft_digest", "manifest_digest",
    "compilation_receipt_digest", "hypothesis_digest",
    "fixed_contract_digest", "catalog_digest", "coverage_digest",
    "episode_count", "slot_digests", "allowed_start_pose_ids",
    "effect_scope", "lifecycle_action", "data_disposition",
    "robot_system_id", "task", "object_profile_id", "grasp_profile_id",
    "cell_calibration_id", "collection_profile_digest", "motion_recipe",
    "motion_qualification_digest", "motion_scope_digest", "qualified_caller", "root_policy",
    "camera_policy", "authority", "envelope_digest",
})
FIELDS = frozenset({
    "schema_version", "authorization_id", "operator_label", "envelope",
    "envelope_digest", "decision_mode", "approved_at", "expires_at",
    "authority", "authorization_digest",
})
EPISODE_SCOPE_FIELDS = frozenset({
    "manifest_digest", "intent_digest", "run_id", "slot_digest",
    "root_binding_digest", "start_binding_digest",
})


def _timestamp(value: object, code: str) -> datetime:
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        raise ContractError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    normalized = parsed.astimezone(timezone.utc)
    if value != normalized.isoformat().replace("+00:00", "Z"):
        raise ContractError(code)
    return normalized


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise ContractError(code)
    return value


def build_campaign_envelope(
    *, source_draft: Mapping[str, Any], manifest: Mapping[str, Any],
    compilation_receipt: Mapping[str, Any], hypothesis: Mapping[str, Any],
    effect_scope: str, lifecycle_action: str, data_disposition: str,
) -> dict[str, Any]:
    """Bind the finite compiled intent to the only runtime scope it may use."""
    checked_hypothesis = validate_fr5_hypothesis(hypothesis)
    checked_draft = validate_campaign_draft(source_draft, hypothesis=checked_hypothesis)
    checked_manifest = validate_collection_campaign_manifest(
        manifest, hypothesis=checked_hypothesis,
    )
    checked_receipt = validate_campaign_compilation_receipt(
        compilation_receipt, draft=checked_draft, manifest=checked_manifest,
        hypothesis=checked_hypothesis,
    )
    if (
        effect_scope not in {"FAKE", "PHYSICAL"}
        or lifecycle_action not in {"PLAN_ONLY", "LIVE_COLLECT"}
        or data_disposition not in {"TEST_ONLY", "PRODUCTION"}
        or effect_scope == "FAKE" and data_disposition != "TEST_ONLY"
    ):
        raise ContractError("CAMPAIGN_ENVELOPE_SCOPE")
    fixed = checked_hypothesis["fixed_contract"]
    slot_digests = [canonical_digest(slot) for slot in checked_manifest["slots"]]
    allowed_start_pose_ids = sorted({
        slot["robot_start_pose_id"] for slot in checked_manifest["slots"]
    })
    motion_scope = {
        "fixed_contract_digest": canonical_digest(fixed),
        "feature_contract_digest": canonical_digest(fixed["feature_contract"]),
        "motion_recipe_digest": fixed["motion_recipe_digest"],
        "collection_profile_digest": fixed["collection_profile_digest"],
        "allowed_pairs_digest": canonical_digest(checked_hypothesis["allowed_pairs"]),
    }
    value = {
        "schema_version": ENVELOPE_SCHEMA,
        "draft_digest": canonical_digest(checked_draft),
        "manifest_digest": checked_manifest["manifest_digest"],
        "compilation_receipt_digest": checked_receipt["receipt_digest"],
        "hypothesis_digest": checked_hypothesis["hypothesis_digest"],
        "fixed_contract_digest": canonical_digest(fixed),
        "catalog_digest": checked_hypothesis["qualification_catalog"]["catalog_digest"],
        "coverage_digest": canonical_digest(checked_hypothesis["coverage_report"]),
        "episode_count": len(checked_manifest["slots"]),
        "slot_digests": slot_digests,
        "allowed_start_pose_ids": allowed_start_pose_ids,
        "effect_scope": effect_scope,
        "lifecycle_action": lifecycle_action,
        "data_disposition": data_disposition,
        "robot_system_id": fixed["robot_system_id"],
        "task": fixed["task"],
        "object_profile_id": fixed["object_profile_id"],
        "grasp_profile_id": fixed["grasp_profile_id"],
        "cell_calibration_id": fixed["cell_calibration_id"],
        "collection_profile_digest": fixed["collection_profile_digest"],
        "motion_recipe": fixed["motion_recipe"],
        "motion_qualification_digest": fixed["motion_recipe_digest"],
        "motion_scope_digest": canonical_digest(motion_scope),
        "qualified_caller": "RUN_LIVE_ONE_JOB",
        "root_policy": (
            "ISOLATED_TEST_ONLY" if data_disposition == "TEST_ONLY"
            else "CONFIGURED_PRODUCTION"
        ),
        "camera_policy": "QUALIFIED_PROFILE_AND_FRESH_DEVICE_BINDING",
        "authority": copy.deepcopy(ENVELOPE_AUTHORITY),
    }
    value["envelope_digest"] = canonical_digest(value)
    return validate_campaign_envelope(value)


def validate_campaign_envelope(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != ENVELOPE_FIELDS:
        raise ContractError("CAMPAIGN_ENVELOPE_BINDING")
    result = copy.deepcopy(dict(value))
    if (
        result["schema_version"] != ENVELOPE_SCHEMA
        or type(result["episode_count"]) is not int
        or not 1 <= result["episode_count"] <= 100
        or not isinstance(result["slot_digests"], list)
        or len(result["slot_digests"]) != result["episode_count"]
        or len(set(result["slot_digests"])) != result["episode_count"]
        or any(not isinstance(item, str) or DIGEST.fullmatch(item) is None for item in result["slot_digests"])
        or not isinstance(result["allowed_start_pose_ids"], list)
        or result["allowed_start_pose_ids"] != sorted(set(result["allowed_start_pose_ids"]))
        or any(not isinstance(item, str) or SAFE_ID.fullmatch(item) is None for item in result["allowed_start_pose_ids"])
        or result["effect_scope"] not in {"FAKE", "PHYSICAL"}
        or result["lifecycle_action"] not in {"PLAN_ONLY", "LIVE_COLLECT"}
        or result["data_disposition"] not in {"TEST_ONLY", "PRODUCTION"}
        or result["effect_scope"] == "FAKE" and result["data_disposition"] != "TEST_ONLY"
        or result["qualified_caller"] != "RUN_LIVE_ONE_JOB"
        or result["root_policy"] != (
            "ISOLATED_TEST_ONLY" if result["data_disposition"] == "TEST_ONLY"
            else "CONFIGURED_PRODUCTION"
        )
        or result["camera_policy"] != "QUALIFIED_PROFILE_AND_FRESH_DEVICE_BINDING"
        or result["authority"] != ENVELOPE_AUTHORITY
        or result["envelope_digest"] != canonical_digest({
            key: result[key] for key in result if key != "envelope_digest"
        })
    ):
        raise ContractError("CAMPAIGN_ENVELOPE_BINDING")
    for field in (
        "draft_digest", "manifest_digest", "compilation_receipt_digest",
        "hypothesis_digest", "fixed_contract_digest", "catalog_digest",
        "coverage_digest", "collection_profile_digest", "motion_qualification_digest",
        "motion_scope_digest",
    ):
        _digest(result[field], "CAMPAIGN_ENVELOPE_BINDING")
    for field in (
        "robot_system_id", "task", "object_profile_id", "grasp_profile_id",
        "cell_calibration_id", "motion_recipe",
    ):
        _identifier(result[field], "CAMPAIGN_ENVELOPE_BINDING")
    return result


def validate_runtime_campaign_scope(
    authorization: Mapping[str, Any], *, resolved_inputs: Mapping[str, Any],
    episode_binding: Mapping[str, Any], now: datetime,
) -> dict[str, Any]:
    """Fail before effects when resolved runtime inputs escape the envelope."""
    checked = validate_campaign_authorization(authorization, now=now)
    envelope = checked["envelope"]
    job = resolved_inputs.get("normalized_job")
    digests = resolved_inputs.get("input_digests")
    if (
        not isinstance(job, Mapping)
        or not isinstance(digests, Mapping)
        or not isinstance(episode_binding, Mapping)
        or episode_binding.get("manifest_digest") != envelope["manifest_digest"]
        or episode_binding.get("slot_digest") not in envelope["slot_digests"]
        or episode_binding.get("robot_start_pose_id")
        not in envelope["allowed_start_pose_ids"]
        or episode_binding.get("data_disposition") != envelope["data_disposition"]
        or digests.get("collection_profile") != envelope["collection_profile_digest"]
        or digests.get("motion_qualification")
        != envelope["motion_qualification_digest"]
        or any(
            job.get(field) != envelope[field]
            for field in (
                "robot_system_id", "task", "object_profile_id", "grasp_profile_id",
                "cell_calibration_id",
            )
        )
    ):
        raise ContractError("CAMPAIGN_AUTHORIZATION_SCOPE_MISMATCH")
    return checked


def build_campaign_authorization(
    *, authorization_id: str, operator_label: str,
    envelope: Mapping[str, Any], approved_at: str, expires_at: str,
) -> dict[str, Any]:
    checked_envelope = validate_campaign_envelope(envelope)
    value = {
        "schema_version": SCHEMA,
        "authorization_id": authorization_id,
        "operator_label": operator_label,
        "envelope": checked_envelope,
        "envelope_digest": checked_envelope["envelope_digest"],
        "decision_mode": DECISION_MODE,
        "approved_at": approved_at,
        "expires_at": expires_at,
        "authority": copy.deepcopy(AUTHORITY),
    }
    value["authorization_digest"] = canonical_digest(value)
    return validate_campaign_authorization(value)


def validate_campaign_authorization(
    value: object, *, now: datetime | None = None,
    operator_label: str | None = None, envelope: Mapping[str, Any] | None = None,
    manifest_digest: str | None = None, data_disposition: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != FIELDS:
        raise ContractError("CAMPAIGN_AUTHORIZATION_BINDING")
    result = copy.deepcopy(dict(value))
    checked_envelope = validate_campaign_envelope(result["envelope"])
    if (
        result["schema_version"] != SCHEMA
        or not isinstance(result["authorization_id"], str)
        or SAFE_ID.fullmatch(result["authorization_id"]) is None
        or not isinstance(result["operator_label"], str)
        or SAFE_ID.fullmatch(result["operator_label"]) is None
        or result["envelope_digest"] != checked_envelope["envelope_digest"]
        or result["decision_mode"] != DECISION_MODE
        or result["authority"] != AUTHORITY
        or result["authorization_digest"] != canonical_digest({
            key: result[key] for key in result if key != "authorization_digest"
        })
        or operator_label is not None and result["operator_label"] != operator_label
        or envelope is not None
        and checked_envelope != validate_campaign_envelope(envelope)
        or manifest_digest is not None
        and checked_envelope["manifest_digest"] != manifest_digest
        or data_disposition is not None
        and checked_envelope["data_disposition"] != data_disposition
    ):
        raise ContractError("CAMPAIGN_AUTHORIZATION_BINDING")
    approved = _timestamp(result["approved_at"], "CAMPAIGN_AUTHORIZATION_BINDING")
    expires = _timestamp(result["expires_at"], "CAMPAIGN_AUTHORIZATION_BINDING")
    if approved >= expires:
        raise ContractError("CAMPAIGN_AUTHORIZATION_BINDING")
    if now is not None:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ContractError("CAMPAIGN_AUTHORIZATION_BINDING")
        current = now.astimezone(timezone.utc)
        if approved > current or current >= expires:
            raise ContractError("CAMPAIGN_AUTHORIZATION_EXPIRED")
    result["envelope"] = checked_envelope
    return result


def validate_authorized_episode_scope(
    authorization: Mapping[str, Any], *, run_id: object, plan_digest: object,
    active_run_id: object, active_intent_digest: object,
    data_disposition: object, episode_binding: Mapping[str, Any],
    expected_plan_digest: str | None = None,
    expected_envelope: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    checked = validate_campaign_authorization(
        authorization, now=now, envelope=expected_envelope,
    )
    envelope = checked["envelope"]
    if (
        not isinstance(run_id, str) or SAFE_ID.fullmatch(run_id) is None
        or not isinstance(plan_digest, str) or DIGEST.fullmatch(plan_digest) is None
        or run_id != active_run_id
        or not isinstance(active_intent_digest, str)
        or DIGEST.fullmatch(active_intent_digest) is None
        or data_disposition != envelope["data_disposition"]
        or expected_plan_digest is not None and plan_digest != expected_plan_digest
        or not isinstance(episode_binding, Mapping)
        or set(episode_binding) != EPISODE_SCOPE_FIELDS
        or episode_binding.get("manifest_digest") != envelope["manifest_digest"]
        or episode_binding.get("intent_digest") != active_intent_digest
        or episode_binding.get("run_id") != run_id
        or episode_binding.get("slot_digest") not in envelope["slot_digests"]
        or any(
            not isinstance(episode_binding.get(field), str)
            or DIGEST.fullmatch(episode_binding[field]) is None
            for field in EPISODE_SCOPE_FIELDS - {"run_id"}
        )
    ):
        raise ContractError("OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH")
