"""Pure validation and operator-intent projection for collection advice."""
from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

from tools.data_factory.candidate_admission import (
    SCHEMA_VERSION as _CANDIDATE_SCHEMA,
)
from tools.data_factory.campaign_authoring import (
    MANIFEST_SCHEMA_V2 as MANIFEST_SCHEMA,
    validate_campaign_compilation_receipt,
)
from tools.data_factory.episode_ledger import (
    EPISODE_LOCATOR_SCHEMA as LOCATOR_SCHEMA,
    SCHEMA_VERSION as LEDGER_SCHEMA,
    STATE_SCHEMA_VERSION as STATE_SCHEMA,
    validate_loaded_episode_evidence,
)
from tools.data_factory.quality.coverage_report import (
    REPORT_SCHEMA as DATA_QUALITY_SCHEMA,
    validate_coverage_report,
)
from tools.fr5_data_factory import (
    ContractError,
    DIGEST,
    RFC3339,
    SAFE_ID,
    canonical_digest,
)


SCHEMA_VERSION = "data_factory.collection_recommendation.v1"
SNAPSHOT_SCHEMA = "data_factory.collection_recommendation_input_snapshot.v1"
EPISODE_REF_SCHEMA = "data_factory.episode_ref.v1"
VIEW_SCHEMA = "data_factory.operator_session_view.v2"
INTENT_SCHEMA = "data_factory.operator_intent.v1"

RECOMMENDATION_FIELDS = frozenset({
    "schema_version", "recommendation_id", "input_snapshot", "claims",
    "suggested_draft_patches", "authority", "recommendation_digest",
})
SNAPSHOT_FIELDS = frozenset({
    "schema_version", "source_commit", "campaign", "episodes",
    "data_quality_analysis_ref", "rollout_evidence_analysis_ref",
    "snapshot_digest",
})
CAMPAIGN_FIELDS = frozenset({
    "schema_version", "manifest_id", "manifest_digest",
})
EPISODE_SNAPSHOT_FIELDS = frozenset({
    "manifest_order_index", "run_id", "episode_index", "dataset_id",
    "dataset_digest", "episode_ref", "locator", "ledger", "state",
    "candidate", "source_provenance_digest", "recording_quality_digest",
})
SCHEMA_DIGEST_FIELDS = frozenset({"schema_version", "digest"})
ANALYSIS_REF_FIELDS = frozenset({
    "availability", "schema_version", "analysis_id", "analysis_digest",
    "reason_codes",
})
CLAIM_FIELDS = frozenset({
    "claim_id", "class", "subject", "value", "evidence_refs",
    "basis_claim_ids", "reason_codes",
})
PATCH_FIELDS = frozenset({"change_id", "field", "value", "basis_claim_ids"})
AUTHORITY_FIELDS = frozenset({
    "recommendation", "dataset_mutation", "candidate_mutation",
    "ledger_mutation", "training_authorization", "motion_authority",
    "gate_bypass", "plan_compile", "campaign_authorization",
})
AUTHORITY = {
    "recommendation": "ADVISORY_ONLY",
    "dataset_mutation": False,
    "candidate_mutation": False,
    "ledger_mutation": False,
    "training_authorization": False,
    "motion_authority": False,
    "gate_bypass": False,
    "plan_compile": False,
    "campaign_authorization": False,
}
EVIDENCE_FIELDS = frozenset({
    "manifest_order_index", "ledger", "state", "candidate", "artifacts",
})
VIEW_FIELDS = frozenset({
    "schema_version", "session_id", "revision", "projection", "generated_at",
    "view_digest", "authority",
})
VIEW_AUTHORITY = {
    "browser": "INTENT_ONLY",
    "lifecycle_owner": "BACKEND",
    "human_identity": "NOT_AUTHENTICATED",
    "training_approval": "SEPARATE",
}
CLAIM_CLASSES = frozenset({"OBSERVED", "SUGGESTED", "UNKNOWN"})
CLAIM_SUBJECTS = frozenset({
    "person", "background", "robot", "coverage", "quality", "rollout",
})
UNKNOWN_REASON_SUBJECTS = {
    "PERSON_LABELS_UNAVAILABLE": "person",
    "BACKGROUND_LABELS_UNAVAILABLE": "background",
    "ROBOT_VARIATION_UNMEASURED": "robot",
    "COVERAGE_NOT_MEASURED": "coverage",
    "DATA_QUALITY_ANALYSIS_UNAVAILABLE": "quality",
    "NO_CANONICAL_PHYSICAL_ROLLOUT_ANALYSIS": "rollout",
}
OBSERVED_VALUE_FIELDS = frozenset({"metric", "count"})
SUGGESTED_VALUES = frozenset({"COLLECT_MORE"})
PATCH_FIELDS_ALLOWLIST = frozenset({
    "requested_count", "repeat", "split", "selection",
    "state_space_design_factors",
})
_MISSING = object()


def _exact(value: object, fields: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(code)
    return copy.deepcopy(dict(value))


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _count(value: object, code: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ContractError(code)
    return value


def _strings(
    value: object, code: str, *, nonempty: bool = False, identifiers: bool = True,
    normalize: bool = False,
) -> list[str]:
    if (
        not isinstance(value, list)
        or nonempty and not value
        or any(
            not isinstance(item, str)
            or not item
            or identifiers and SAFE_ID.fullmatch(item) is None
            for item in value
        )
        or len(value) != len(set(value))
        or not normalize and value != sorted(value)
    ):
        raise ContractError(code)
    return sorted(value)


def _self_digest(value: Mapping[str, Any], field: str, code: str) -> str:
    expected = _digest(value.get(field), code)
    if canonical_digest({key: item for key, item in value.items() if key != field}) != expected:
        raise ContractError(code)
    return expected


def _campaign(
    manifest: object, *, hypothesis: Mapping[str, Any], draft: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    validate_campaign_compilation_receipt(
        receipt, draft=draft, manifest=manifest, hypothesis=hypothesis,
    )
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise ContractError("COLLECTION_RECOMMENDATION_MANIFEST_SCHEMA")
    return copy.deepcopy(dict(manifest))


def _episode_snapshot(
    value: object, manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, str, int]]:
    evidence = _exact(
        value, EVIDENCE_FIELDS, "COLLECTION_RECOMMENDATION_EVIDENCE_FIELDS",
    )
    order_index = _count(
        evidence["manifest_order_index"],
        "COLLECTION_RECOMMENDATION_EVIDENCE_ORDER",
    )
    if order_index >= len(manifest["slots"]):
        raise ContractError("COLLECTION_RECOMMENDATION_EVIDENCE_ORDER")

    checked = validate_loaded_episode_evidence(
        ledger=evidence["ledger"], state=evidence["state"],
        candidate=evidence["candidate"], artifacts=evidence["artifacts"],
    )
    payloads = checked["artifacts"]
    if (
        payloads["manifest"] != manifest
        or payloads["intent"].get("order_index") != order_index
        or payloads["intent"].get("slot") != manifest["slots"][order_index]
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_MANIFEST_ARTIFACT_BINDING")
    if checked["admission"]["technical_status"] != "PASS":
        raise ContractError("COLLECTION_RECOMMENDATION_ADMISSION")

    ledger = checked["ledger"]
    dataset = checked["dataset"]
    ref = checked["episode_ref"]
    expected_dataset_digest = canonical_digest({
        "repo_id": dataset["repo_id"],
        "dataset_root": dataset["dataset_root"],
        "episode_ref": ref,
    })
    if (
        dataset["dataset_digest"] != expected_dataset_digest
        or dataset["dataset_id"] != f"dataset-{expected_dataset_digest[7:23]}"
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_DATASET_DIGEST")

    candidate = checked["candidate"]
    if candidate is None:
        raise ContractError("COLLECTION_RECOMMENDATION_CANDIDATE_BINDING")
    summary = {
        "manifest_order_index": order_index,
        "run_id": checked["run_id"],
        "episode_index": ref["episode_index"],
        "dataset_id": dataset["dataset_id"],
        "dataset_digest": dataset["dataset_digest"],
        "episode_ref": {
            "schema_version": EPISODE_REF_SCHEMA,
            "digest": ledger["episode"]["episode_ref_digest"],
        },
        "locator": {
            "schema_version": LOCATOR_SCHEMA,
            "digest": checked["locator"]["locator_digest"],
        },
        "ledger": {
            "schema_version": LEDGER_SCHEMA,
            "digest": ledger["ledger_digest"],
        },
        "state": {
            "schema_version": STATE_SCHEMA,
            "digest": checked["state"]["state_digest"],
        },
        "candidate": {
            "schema_version": _CANDIDATE_SCHEMA,
            "digest": canonical_digest(candidate),
        },
        "source_provenance_digest": checked["artifact_refs"][
            "source_provenance"
        ]["artifact_digest"],
        "recording_quality_digest": checked["artifact_refs"][
            "recording_quality"
        ]["artifact_digest"],
    }
    identity = (
        dataset["repo_id"], dataset["dataset_root"], ref["episode_index"],
    )
    return summary, identity


def _episode_summaries(
    values: object, manifest: Mapping[str, Any], *, normalize: bool,
) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
        raise ContractError("COLLECTION_RECOMMENDATION_EPISODES")
    checked = [_episode_snapshot(item, manifest) for item in values]
    episodes = [item[0] for item in checked]
    identities = [item[1] for item in checked]
    if normalize:
        episodes.sort(key=lambda item: item["manifest_order_index"])
    if [item["manifest_order_index"] for item in episodes] != list(range(len(episodes))):
        raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_ORDER")
    if len(identities) != len(set(identities)):
        raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_DUPLICATE")
    unique_fields = (
        "run_id", "dataset_digest", "source_provenance_digest",
        "recording_quality_digest",
    )
    if any(len({item[field] for item in episodes}) != len(episodes) for field in unique_fields):
        raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_DUPLICATE")
    for nested in ("episode_ref", "locator", "ledger", "state", "candidate"):
        if len({item[nested]["digest"] for item in episodes}) != len(episodes):
            raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_DUPLICATE")
    return episodes


def _analysis_ref(
    value: object, *, owner: str, artifact: object = _MISSING,
    normalize: bool,
) -> dict[str, Any]:
    ref = _exact(value, ANALYSIS_REF_FIELDS, "COLLECTION_RECOMMENDATION_ANALYSIS_REF_FIELDS")
    reasons = _strings(
        ref["reason_codes"], "COLLECTION_RECOMMENDATION_ANALYSIS_REASONS",
        normalize=normalize,
    )
    if ref["availability"] == "UNAVAILABLE":
        if (
            any(ref[field] is not None for field in ("schema_version", "analysis_id", "analysis_digest"))
            or not reasons
            or owner == "rollout"
            and "NO_CANONICAL_PHYSICAL_ROLLOUT_ANALYSIS" not in reasons
            or artifact is not _MISSING and artifact is not None
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_ANALYSIS_UNAVAILABLE")
        ref["reason_codes"] = reasons
        return ref
    if ref["availability"] != "AVAILABLE" or reasons:
        raise ContractError("COLLECTION_RECOMMENDATION_ANALYSIS_AVAILABILITY")
    if owner == "rollout":
        raise ContractError("COLLECTION_RECOMMENDATION_ROLLOUT_OWNER")
    schema = _identifier(ref["schema_version"], "COLLECTION_RECOMMENDATION_ANALYSIS_SCHEMA")
    analysis_id = _identifier(ref["analysis_id"], "COLLECTION_RECOMMENDATION_ANALYSIS_ID")
    expected_digest = _digest(ref["analysis_digest"], "COLLECTION_RECOMMENDATION_ANALYSIS_DIGEST")
    if schema != DATA_QUALITY_SCHEMA:
        raise ContractError("COLLECTION_RECOMMENDATION_DATA_QUALITY_OWNER")
    if artifact is not _MISSING:
        if not isinstance(artifact, Mapping):
            raise ContractError("COLLECTION_RECOMMENDATION_ANALYSIS_ARTIFACT")
        report = validate_coverage_report(artifact)
        if (
            report["schema_version"] != schema
            or report["collection_profile_id"] != analysis_id
            or canonical_digest(report) != expected_digest
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_ANALYSIS_DIGEST")
    ref["reason_codes"] = []
    return ref


def _analysis_refs(
    data_quality_ref: object, rollout_ref: object, *,
    data_quality_analysis: object = _MISSING,
    rollout_evidence_analysis: object = _MISSING,
    normalize: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    data_quality = _analysis_ref(
        data_quality_ref, owner="data_quality", artifact=data_quality_analysis,
        normalize=normalize,
    )
    rollout = _analysis_ref(
        rollout_ref, owner="rollout", artifact=rollout_evidence_analysis,
        normalize=normalize,
    )
    return data_quality, rollout


def _known_evidence(snapshot: Mapping[str, Any]) -> set[str]:
    result = {snapshot["campaign"]["manifest_digest"]}
    for episode in snapshot["episodes"]:
        result.update({
            episode["dataset_digest"], episode["source_provenance_digest"],
            episode["recording_quality_digest"],
            *(episode[name]["digest"] for name in ("episode_ref", "locator", "ledger", "state", "candidate")),
        })
    for name in ("data_quality_analysis_ref", "rollout_evidence_analysis_ref"):
        if snapshot[name]["availability"] == "AVAILABLE":
            result.add(snapshot[name]["analysis_digest"])
    return result


def _claim_value(claim: Mapping[str, Any]) -> Any:
    if claim["class"] == "UNKNOWN":
        if claim["value"] is not None:
            raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_VALUE")
        return None
    if claim["class"] == "SUGGESTED":
        if (
            claim["subject"] != "coverage"
            or not isinstance(claim["value"], str)
            or claim["value"] not in SUGGESTED_VALUES
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_VALUE")
        return claim["value"]
    if claim["subject"] != "coverage":
        raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_VALUE")
    value = _exact(
        claim["value"], OBSERVED_VALUE_FIELDS,
        "COLLECTION_RECOMMENDATION_CLAIM_VALUE",
    )
    if (
        value["metric"] != "COLLECTED_EPISODE_COUNT"
        or type(value["count"]) is not int
        or value["count"] < 0
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_VALUE")
    return value


def _claims(
    values: object, snapshot: Mapping[str, Any], *, normalize: bool,
) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
        raise ContractError("COLLECTION_RECOMMENDATION_CLAIMS")
    known_evidence = _known_evidence(snapshot)
    result = []
    for raw in values:
        claim = _exact(raw, CLAIM_FIELDS, "COLLECTION_RECOMMENDATION_CLAIM_FIELDS")
        _identifier(claim["claim_id"], "COLLECTION_RECOMMENDATION_CLAIM_ID")
        if claim["class"] not in CLAIM_CLASSES or claim["subject"] not in CLAIM_SUBJECTS:
            raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_TYPE")
        evidence = _strings(
            claim["evidence_refs"], "COLLECTION_RECOMMENDATION_CLAIM_EVIDENCE",
            identifiers=False, normalize=normalize,
        )
        if any(_digest(item, "COLLECTION_RECOMMENDATION_CLAIM_EVIDENCE") not in known_evidence for item in evidence):
            raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_EVIDENCE")
        basis = _strings(
            claim["basis_claim_ids"], "COLLECTION_RECOMMENDATION_CLAIM_BASIS",
            normalize=normalize,
        )
        reasons = _strings(
            claim["reason_codes"], "COLLECTION_RECOMMENDATION_CLAIM_REASONS",
            normalize=normalize,
        )
        if (
            claim["class"] == "OBSERVED" and (not evidence or basis or reasons)
            or claim["class"] == "SUGGESTED"
            and (not basis or reasons != ["COVERAGE_DEFICIT"])
            or claim["class"] == "UNKNOWN" and (basis or not reasons)
            or claim["class"] == "UNKNOWN" and any(
                UNKNOWN_REASON_SUBJECTS.get(reason) != claim["subject"]
                for reason in reasons
            )
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_EPISTEMIC")
        claim.update(
            value=_claim_value(claim), evidence_refs=evidence,
            basis_claim_ids=basis, reason_codes=reasons,
        )
        if (
            claim["class"] == "OBSERVED"
            and (
                claim["value"]["count"] != len(snapshot["episodes"])
                or snapshot["campaign"]["manifest_digest"] not in evidence
            )
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_VALUE")
        result.append(claim)
    if normalize:
        result.sort(key=lambda item: item["claim_id"])
    ids = [claim["claim_id"] for claim in result]
    if len(ids) != len(set(ids)) or ids != sorted(ids):
        raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_DUPLICATE")
    by_id = {claim["claim_id"]: claim for claim in result}
    for claim in result:
        if any(
            basis_id == claim["claim_id"]
            or basis_id not in by_id
            or by_id[basis_id]["class"] != "OBSERVED"
            for basis_id in claim["basis_claim_ids"]
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_CLAIM_BASIS")
    required_unknowns = {
        "person": "PERSON_LABELS_UNAVAILABLE",
        "background": "BACKGROUND_LABELS_UNAVAILABLE",
        "robot": "ROBOT_VARIATION_UNMEASURED",
        "rollout": "NO_CANONICAL_PHYSICAL_ROLLOUT_ANALYSIS",
    }
    if snapshot["data_quality_analysis_ref"]["availability"] == "UNAVAILABLE":
        required_unknowns["quality"] = "DATA_QUALITY_ANALYSIS_UNAVAILABLE"
    elif any(claim["subject"] == "quality" for claim in result):
        raise ContractError("COLLECTION_RECOMMENDATION_NUISANCE_CLAIM")
    for subject, reason in required_unknowns.items():
        matches = [claim for claim in result if claim["subject"] == subject]
        if (
            len(matches) != 1
            or matches[0]["class"] != "UNKNOWN"
            or matches[0]["reason_codes"] != [reason]
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_NUISANCE_CLAIM")
    return result


def _patch_value(field: str, value: object) -> Any:
    if field in {"requested_count", "repeat"}:
        if type(value) is not int or not 1 <= value <= 100:
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_VALUE")
    elif field == "split":
        if value not in {"TRAIN", "ID", "OOD"}:
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_VALUE")
    elif field == "selection":
        if not isinstance(value, Mapping) or len(value) != 1:
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_VALUE")
        axis, selected = next(iter(value.items()))
        _identifier(axis, "COLLECTION_RECOMMENDATION_PATCH_VALUE")
        _identifier(selected, "COLLECTION_RECOMMENDATION_PATCH_VALUE")
    else:
        factors = _exact(
            value, frozenset({"columns", "rows", "yaw_cdf_strata"}),
            "COLLECTION_RECOMMENDATION_PATCH_VALUE",
        )
        columns, rows, yaw = factors["columns"], factors["rows"], factors["yaw_cdf_strata"]
        if (
            type(columns) is not int or type(rows) is not int or type(yaw) is not int
            or not 1 <= columns <= 100 or not 1 <= rows <= 100
            or columns * rows > 100 or not 1 <= yaw <= columns * rows
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_VALUE")
        value = factors
    canonical_digest(value)
    return copy.deepcopy(value)


def _patches(
    values: object, claims: Sequence[Mapping[str, Any]], *, normalize: bool,
) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ContractError("COLLECTION_RECOMMENDATION_PATCHES")
    by_id = {claim["claim_id"]: claim for claim in claims}
    result = []
    for raw in values:
        patch = _exact(raw, PATCH_FIELDS, "COLLECTION_RECOMMENDATION_PATCH_FIELDS")
        _identifier(patch["change_id"], "COLLECTION_RECOMMENDATION_PATCH_ID")
        if patch["field"] not in PATCH_FIELDS_ALLOWLIST:
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_FIELD")
        basis = _strings(
            patch["basis_claim_ids"], "COLLECTION_RECOMMENDATION_PATCH_BASIS",
            nonempty=True, normalize=normalize,
        )
        if any(item not in by_id for item in basis):
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_BASIS")
        if any(
            by_id[item]["class"] == "UNKNOWN"
            for item in basis
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_CAUSAL")
        patch["value"] = _patch_value(patch["field"], patch["value"])
        patch["basis_claim_ids"] = basis
        result.append(patch)
    if normalize:
        result.sort(key=lambda item: item["change_id"])
    ids = [patch["change_id"] for patch in result]
    if len(ids) != len(set(ids)) or ids != sorted(ids):
        raise ContractError("COLLECTION_RECOMMENDATION_PATCH_DUPLICATE")
    return result


def _authority(value: object) -> dict[str, Any]:
    authority = _exact(value, AUTHORITY_FIELDS, "COLLECTION_RECOMMENDATION_AUTHORITY_FIELDS")
    if authority["recommendation"] != "ADVISORY_ONLY" or any(
        type(authority[field]) is not bool or authority[field] is not False
        for field in AUTHORITY_FIELDS - {"recommendation"}
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_AUTHORITY")
    return authority


def build_collection_recommendation(
    *, recommendation_id: str, source_commit: str,
    campaign_manifest: Mapping[str, Any], campaign_hypothesis: Mapping[str, Any],
    campaign_draft: Mapping[str, Any],
    campaign_compilation_receipt: Mapping[str, Any],
    episode_evidence: Sequence[Mapping[str, Any]],
    data_quality_analysis_ref: Mapping[str, Any],
    rollout_evidence_analysis_ref: Mapping[str, Any], claims: Sequence[Mapping[str, Any]],
    suggested_draft_patches: Sequence[Mapping[str, Any]],
    data_quality_analysis: Mapping[str, Any] | None = None,
    rollout_evidence_analysis: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one canonical recommendation from already-loaded evidence."""
    _identifier(recommendation_id, "COLLECTION_RECOMMENDATION_ID")
    if (
        not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_SOURCE_COMMIT")
    manifest = _campaign(
        campaign_manifest, hypothesis=campaign_hypothesis,
        draft=campaign_draft, receipt=campaign_compilation_receipt,
    )
    episodes = _episode_summaries(episode_evidence, manifest, normalize=True)
    data_quality_ref, rollout_ref = _analysis_refs(
        data_quality_analysis_ref, rollout_evidence_analysis_ref,
        data_quality_analysis=data_quality_analysis,
        rollout_evidence_analysis=rollout_evidence_analysis,
        normalize=True,
    )
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA,
        "source_commit": source_commit,
        "campaign": {
            "schema_version": MANIFEST_SCHEMA,
            "manifest_id": manifest["manifest_id"],
            "manifest_digest": manifest["manifest_digest"],
        },
        "episodes": episodes,
        "data_quality_analysis_ref": data_quality_ref,
        "rollout_evidence_analysis_ref": rollout_ref,
    }
    snapshot["snapshot_digest"] = canonical_digest(snapshot)
    checked_claims = _claims(claims, snapshot, normalize=True)
    value = {
        "schema_version": SCHEMA_VERSION,
        "recommendation_id": recommendation_id,
        "input_snapshot": snapshot,
        "claims": checked_claims,
        "suggested_draft_patches": _patches(
            suggested_draft_patches, checked_claims, normalize=True,
        ),
        "authority": copy.deepcopy(AUTHORITY),
    }
    value["recommendation_digest"] = canonical_digest(value)
    return validate_collection_recommendation(value)


def _snapshot(value: object) -> dict[str, Any]:
    snapshot = _exact(value, SNAPSHOT_FIELDS, "COLLECTION_RECOMMENDATION_SNAPSHOT_FIELDS")
    _self_digest(snapshot, "snapshot_digest", "COLLECTION_RECOMMENDATION_SNAPSHOT_DIGEST")
    if snapshot["schema_version"] != SNAPSHOT_SCHEMA:
        raise ContractError("COLLECTION_RECOMMENDATION_SNAPSHOT_SCHEMA")
    source_commit = snapshot["source_commit"]
    if (
        not isinstance(source_commit, str) or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_SOURCE_COMMIT")
    campaign = _exact(snapshot["campaign"], CAMPAIGN_FIELDS, "COLLECTION_RECOMMENDATION_CAMPAIGN_FIELDS")
    if campaign["schema_version"] != MANIFEST_SCHEMA:
        raise ContractError("COLLECTION_RECOMMENDATION_CAMPAIGN_SCHEMA")
    _identifier(campaign["manifest_id"], "COLLECTION_RECOMMENDATION_MANIFEST_ID")
    _digest(campaign["manifest_digest"], "COLLECTION_RECOMMENDATION_MANIFEST_DIGEST")
    episodes = snapshot["episodes"]
    if not isinstance(episodes, list) or not episodes:
        raise ContractError("COLLECTION_RECOMMENDATION_EPISODES")
    normalized_episodes = []
    for index, raw in enumerate(episodes):
        episode = _exact(raw, EPISODE_SNAPSHOT_FIELDS, "COLLECTION_RECOMMENDATION_EPISODE_SNAPSHOT_FIELDS")
        if episode["manifest_order_index"] != index:
            raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_ORDER")
        _identifier(episode["run_id"], "COLLECTION_RECOMMENDATION_RUN_ID")
        _count(episode["episode_index"], "COLLECTION_RECOMMENDATION_EPISODE_INDEX")
        _identifier(episode["dataset_id"], "COLLECTION_RECOMMENDATION_DATASET_ID")
        for field in ("dataset_digest", "source_provenance_digest", "recording_quality_digest"):
            _digest(episode[field], "COLLECTION_RECOMMENDATION_EPISODE_DIGEST")
        expected_schemas = {
            "episode_ref": EPISODE_REF_SCHEMA,
            "locator": LOCATOR_SCHEMA,
            "ledger": LEDGER_SCHEMA,
            "state": STATE_SCHEMA,
            "candidate": _CANDIDATE_SCHEMA,
        }
        for name, schema in expected_schemas.items():
            nested = _exact(episode[name], SCHEMA_DIGEST_FIELDS, "COLLECTION_RECOMMENDATION_EPISODE_REF")
            if nested["schema_version"] != schema:
                raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_REF")
            _digest(nested["digest"], "COLLECTION_RECOMMENDATION_EPISODE_DIGEST")
            episode[name] = nested
        normalized_episodes.append(episode)
    unique_fields = (
        "run_id", "dataset_digest", "source_provenance_digest",
        "recording_quality_digest",
    )
    if any(
        len({item[field] for item in normalized_episodes}) != len(normalized_episodes)
        for field in unique_fields
    ) or any(
        len({item[name]["digest"] for item in normalized_episodes})
        != len(normalized_episodes)
        for name in ("episode_ref", "locator", "ledger", "state", "candidate")
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_DUPLICATE")
    snapshot["campaign"], snapshot["episodes"] = campaign, normalized_episodes
    data_quality_ref, rollout_ref = _analysis_refs(
        snapshot["data_quality_analysis_ref"], snapshot["rollout_evidence_analysis_ref"],
        normalize=False,
    )
    snapshot["data_quality_analysis_ref"] = data_quality_ref
    snapshot["rollout_evidence_analysis_ref"] = rollout_ref
    return snapshot


def validate_collection_recommendation(
    value: object, *, campaign_manifest: Mapping[str, Any] | None = None,
    campaign_hypothesis: Mapping[str, Any] | None = None,
    campaign_draft: Mapping[str, Any] | None = None,
    campaign_compilation_receipt: Mapping[str, Any] | None = None,
    episode_evidence: Sequence[Mapping[str, Any]] | None = None,
    data_quality_analysis: Mapping[str, Any] | None = None,
    rollout_evidence_analysis: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a self-digested value, optionally rejoining supplied evidence."""
    recommendation = _exact(value, RECOMMENDATION_FIELDS, "COLLECTION_RECOMMENDATION_FIELDS")
    _self_digest(
        recommendation, "recommendation_digest",
        "COLLECTION_RECOMMENDATION_DIGEST",
    )
    if recommendation["schema_version"] != SCHEMA_VERSION:
        raise ContractError("COLLECTION_RECOMMENDATION_SCHEMA")
    _identifier(recommendation["recommendation_id"], "COLLECTION_RECOMMENDATION_ID")
    snapshot = _snapshot(recommendation["input_snapshot"])
    claims = _claims(recommendation["claims"], snapshot, normalize=False)
    patches = _patches(
        recommendation["suggested_draft_patches"], claims, normalize=False,
    )
    authority = _authority(recommendation["authority"])
    recommendation.update(
        input_snapshot=snapshot, claims=claims,
        suggested_draft_patches=patches, authority=authority,
    )
    campaign_evidence = (
        campaign_manifest, campaign_hypothesis, campaign_draft,
        campaign_compilation_receipt, episode_evidence,
    )
    if any(item is None for item in campaign_evidence) and any(
        item is not None for item in campaign_evidence
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_EVIDENCE_REQUIRED")
    if all(item is not None for item in campaign_evidence):
        manifest = _campaign(
            campaign_manifest, hypothesis=campaign_hypothesis,
            draft=campaign_draft, receipt=campaign_compilation_receipt,
        )
        episodes = _episode_summaries(
            episode_evidence, manifest, normalize=False,
        )
        if snapshot["campaign"] != {
            "schema_version": MANIFEST_SCHEMA,
            "manifest_id": manifest["manifest_id"],
            "manifest_digest": manifest["manifest_digest"],
        } or snapshot["episodes"] != episodes:
            raise ContractError("COLLECTION_RECOMMENDATION_SNAPSHOT_BINDING")
        checked_refs = _analysis_refs(
            snapshot["data_quality_analysis_ref"],
            snapshot["rollout_evidence_analysis_ref"],
            data_quality_analysis=data_quality_analysis,
            rollout_evidence_analysis=rollout_evidence_analysis,
            normalize=False,
        )
        if checked_refs != (
            snapshot["data_quality_analysis_ref"],
            snapshot["rollout_evidence_analysis_ref"],
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_ANALYSIS_BINDING")
    elif data_quality_analysis is not None or rollout_evidence_analysis is not None:
        raise ContractError("COLLECTION_RECOMMENDATION_EVIDENCE_REQUIRED")
    return recommendation


def project_update_draft_intent(
    recommendation: object, *, selected_change_id: str,
    operator_view: Mapping[str, Any], intent_id: str | None = None,
    data_quality_analysis: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one update_draft intent; never consume or apply it."""
    checked = validate_collection_recommendation(recommendation)
    _analysis_ref(
        checked["input_snapshot"]["data_quality_analysis_ref"],
        owner="data_quality", artifact=data_quality_analysis, normalize=False,
    )
    _analysis_ref(
        checked["input_snapshot"]["rollout_evidence_analysis_ref"],
        owner="rollout", artifact=None, normalize=False,
    )
    _identifier(selected_change_id, "COLLECTION_RECOMMENDATION_PATCH_SELECTION")
    selected = [
        patch for patch in checked["suggested_draft_patches"]
        if patch["change_id"] == selected_change_id
    ]
    if len(selected) != 1:
        raise ContractError("COLLECTION_RECOMMENDATION_PATCH_SELECTION")
    patch = selected[0]
    view = _exact(operator_view, VIEW_FIELDS, "COLLECTION_RECOMMENDATION_VIEW_FIELDS")
    projection = view["projection"]
    if (
        view["schema_version"] != VIEW_SCHEMA
        or not isinstance(projection, Mapping)
        or projection.get("workflow_state") != "AUTHORING"
        or not isinstance(projection.get("available_ops"), list)
        or projection["available_ops"].count("update_draft") != 1
        or view["authority"] != VIEW_AUTHORITY
        or type(view["revision"]) is not int
        or view["revision"] < 0
        or not isinstance(view["generated_at"], str)
        or RFC3339.fullmatch(view["generated_at"]) is None
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_VIEW_STATE")
    session_id = _identifier(view["session_id"], "COLLECTION_RECOMMENDATION_VIEW_SESSION")
    expected_view_digest = canonical_digest({
        "session_id": session_id,
        "revision": view["revision"],
        "projection": projection,
    })
    if view["view_digest"] != expected_view_digest:
        raise ContractError("COLLECTION_RECOMMENDATION_VIEW_STALE")
    draft = projection.get("draft")
    if not isinstance(draft, Mapping):
        raise ContractError("COLLECTION_RECOMMENDATION_VIEW_DRAFT")
    draft_id = _identifier(draft.get("draft_id"), "COLLECTION_RECOMMENDATION_VIEW_DRAFT")
    _count(draft.get("revision"), "COLLECTION_RECOMMENDATION_VIEW_DRAFT")
    field = patch["field"]
    selection = draft.get("selection")
    if field in {"selection", "split"}:
        axis, selected_value = (
            next(iter(patch["value"].items()))
            if field == "selection" else ("split", patch["value"])
        )
        catalog = projection.get("catalog")
        axes = catalog.get("axes") if isinstance(catalog, Mapping) else None
        options = axes.get(axis) if isinstance(axes, Mapping) else None
        matches = [
            option for option in options or []
            if isinstance(option, Mapping) and option.get("id") == selected_value
        ] if isinstance(options, list) else []
        if (
            not isinstance(selection, Mapping)
            or not isinstance(selection.get(axis), str)
            or len(matches) != 1 or matches[0].get("available") is not True
        ):
            raise ContractError("COLLECTION_RECOMMENDATION_PATCH_VALUE")
    elif field in {"requested_count", "repeat"}:
        _count(
            draft.get(field), "COLLECTION_RECOMMENDATION_VIEW_DRAFT",
            positive=True,
        )
    elif (
        draft.get("authoring_mode") != "ASSISTED"
        or not isinstance(projection.get("sampling_provenance"), Mapping)
        or not isinstance(
            projection["sampling_provenance"].get(
                "state_space_design_profile"
            ),
            Mapping,
        )
    ):
        raise ContractError("COLLECTION_RECOMMENDATION_PATCH_VALUE")
    if intent_id is None:
        intent_id = "recommendation-" + canonical_digest({
            "recommendation_digest": checked["recommendation_digest"],
            "change_id": selected_change_id,
            "view_digest": view["view_digest"],
        })[7:31]
    _identifier(intent_id, "COLLECTION_RECOMMENDATION_INTENT_ID")
    return {
        "schema_version": INTENT_SCHEMA,
        "intent_id": intent_id,
        "session_id": session_id,
        "view_revision": view["revision"],
        "view_digest": view["view_digest"],
        "op": "update_draft",
        "payload": {
            "draft_id": draft_id,
            field: copy.deepcopy(patch["value"]),
        },
    }


__all__ = [
    "AUTHORITY", "SCHEMA_VERSION", "SNAPSHOT_SCHEMA",
    "build_collection_recommendation", "project_update_draft_intent",
    "validate_collection_recommendation",
]
