"""Offline P5 coverage accounting; this module grants no admission authority."""
from __future__ import annotations

import copy
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.data_factory.candidate_admission import validate_candidate_admission
from tools.data_factory.motion.object_reposition import (
    validate_object_reposition_binding,
)
from tools.data_factory.motion.trajectory_variants import (
    validate_trajectory_variant_binding,
)
from tools.data_factory.task_recipe import validate_episode_instruction_binding
from tools.data_factory.state_space import validate_yaw_sample_binding
from tools.data_factory_recovery import write_json_atomic
from tools.fr5_data_factory import ContractArgumentParser, ContractError, DIGEST, SAFE_ID, canonical_digest, load_json_strict, normalize_job_spec, task_review_checklist_id


REPORT_SCHEMA = "data_factory.coverage_report.v1"
DOMAIN_SCHEMA = "data_factory.coverage_domain.v1"
STORED_EPISODES_SCHEMA = "data_factory.coverage_stored_episodes.v2"
CONDITION_FIELDS = (
    "task_schema_version", "task", "robot_system_id", "place_id",
    "cell_calibration_id", "cell_calibration_digest", "yaw_deg", "x_mm", "y_mm",
    "object_profile_id", "grasp_profile_id", "motion_recipe_digest",
    "collection_profile_digest",
)
COUNTS = (
    "collected", "technical_pass_candidate", "pending_review",
    "human_semantic_pass", "human_training_approved", "rejected", "quarantined",
)
ADMISSION_STATES = frozenset({
    "COLLECTED", "TECHNICAL_PASS_CANDIDATE", "PENDING_REVIEW",
    "HUMAN_SEMANTIC_PASS", "HUMAN_TRAINING_APPROVED", "REJECTED", "QUARANTINED",
})
SLOT_STATES = frozenset({"AVAILABLE", "PENDING", "RESERVED", "CONSUMED", "QUARANTINED"})
BLOCKED_SLOT_STATES = SLOT_STATES - {"AVAILABLE"}
CONTINUITY_FIELDS = (
    "phase_continuity", "phase_status_flags", "close_feedback_in_window",
    "lift_feedback_delta", "terminal_to_next_gap",
)
EVIDENCE_STATUS = frozenset({"AVAILABLE", "FLAGGED", "NOT_AVAILABLE"})
EVIDENCE_DIGESTS = frozenset({"job_spec", "technical_validator_result", "candidate_admission"})
REPORT_FIELDS = frozenset({"schema_version", "collection_profile_id", "domain_digest", "cells", "suggest_next", "authority"})
STORED_EPISODE_FIELDS = frozenset({
    "episode_id", "job_spec_path", "job_spec_digest", "technical_validator_path",
    "technical_validator_digest", "candidate_admission_path", "candidate_admission_digest",
    "preapproval_evidence_path", "preapproval_evidence_digest",
})
RESOLVED_INPUT_DIGEST_FIELDS = (
    "selected_sheet", "yaw0_sheet", "cell_calibration", "robot_system",
    "collection_profile", "object_profile", "grasp_profile",
)
PLAN_BINDING_DIGEST_FIELDS = frozenset({
    *RESOLVED_INPUT_DIGEST_FIELDS, "robot_description_digest", "moveit_config_digest",
    "planning_scene_digest", "motion_qualification", "home_candidate",
})
PREAPPROVAL_V1_FIELDS = frozenset({
    "schema_version", "run_id", "resolved_job_digest", "plan_digest",
    "plan_envelope", "plan_envelope_digest",
})
PREAPPROVAL_V2_FIELDS = PREAPPROVAL_V1_FIELDS | frozenset({
    "episode_instruction_binding", "episode_instruction_binding_digest",
})
PREAPPROVAL_V4_FIELDS = PREAPPROVAL_V1_FIELDS | frozenset({
    "trajectory_variant_binding", "trajectory_variant_binding_digest",
    "campaign_binding", "object_reposition_binding",
    "object_reposition_binding_digest",
    "yaw_sample_binding", "yaw_sample_binding_digest",
})
PREAPPROVAL_CAMPAIGN_BINDING_FIELDS = frozenset({
    "manifest_digest", "intent_digest", "slot_id", "slot_digest",
    "runtime_episode_binding_digest",
})
TECHNICAL_FIELDS = frozenset({
    "schema_version", "run_id", "resolved_job_digest", "plan_digest", "dataset_root",
    "expected_fps", "status", "result_digest",
})


def validate_preapproval_campaign_binding(
    value: object,
) -> dict[str, Any] | None:
    """Validate the exact optional campaign slot bound before approval."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != (
        PREAPPROVAL_CAMPAIGN_BINDING_FIELDS
    ):
        raise ContractError("PREAPPROVAL_CAMPAIGN_BINDING")
    result = copy.deepcopy(dict(value))
    if any(
        not isinstance(item, str)
        or (
            SAFE_ID.fullmatch(item) is None
            if field == "slot_id" else DIGEST.fullmatch(item) is None
        )
        for field, item in result.items()
    ):
        raise ContractError("PREAPPROVAL_CAMPAIGN_BINDING")
    return result


def validate_preapproval_evidence(value: object) -> dict[str, Any]:
    """Accept legacy evidence and validate the optional episode language binding."""
    if not isinstance(value, Mapping):
        raise ContractError("PREAPPROVAL_EVIDENCE_SCHEMA")
    schema = value.get("schema_version")
    fields = (
        PREAPPROVAL_V1_FIELDS
        if schema == "data_factory.preapproval_evidence.v1"
        else PREAPPROVAL_V2_FIELDS
        if schema == "data_factory.preapproval_evidence.v2"
        else (
            PREAPPROVAL_V4_FIELDS
            | ({"episode_instruction_binding", "episode_instruction_binding_digest"}
               if "episode_instruction_binding" in value else set())
        )
        if schema == "data_factory.preapproval_evidence.v4"
        else frozenset()
    )
    if set(value) != fields:
        raise ContractError("PREAPPROVAL_EVIDENCE_SCHEMA")
    result = copy.deepcopy(dict(value))
    if schema in {
        "data_factory.preapproval_evidence.v2",
        "data_factory.preapproval_evidence.v4",
    } and "episode_instruction_binding" in result:
        try:
            instruction = validate_episode_instruction_binding(
                result["episode_instruction_binding"],
            )
        except ContractError as exc:
            raise ContractError("PREAPPROVAL_EVIDENCE_SCHEMA") from exc
        if (
            result["episode_instruction_binding_digest"]
            != instruction["binding_digest"]
        ):
            raise ContractError("PREAPPROVAL_EVIDENCE_SCHEMA")
    if schema == "data_factory.preapproval_evidence.v4":
        trajectory = result["trajectory_variant_binding"]
        reposition = result["object_reposition_binding"]
        campaign = result["campaign_binding"]
        try:
            checked_trajectory = validate_trajectory_variant_binding(trajectory)
            checked_reposition = (
                None if reposition is None
                else validate_object_reposition_binding(reposition)
            )
            checked_campaign = validate_preapproval_campaign_binding(campaign)
        except ContractError as exc:
            raise ContractError("PREAPPROVAL_EVIDENCE_SCHEMA") from exc
        if (
            result["trajectory_variant_binding_digest"]
            != checked_trajectory["binding_digest"]
            or reposition is None
            and result["object_reposition_binding_digest"] is not None
            or checked_reposition is not None
            and result["object_reposition_binding_digest"]
            != checked_reposition["binding_digest"]
            or campaign != checked_campaign
        ):
            raise ContractError("PREAPPROVAL_EVIDENCE_SCHEMA")
    if schema == "data_factory.preapproval_evidence.v4":
        yaw_sample = result["yaw_sample_binding"]
        try:
            checked_yaw = (
                None if yaw_sample is None
                else validate_yaw_sample_binding(yaw_sample)
            )
        except ContractError as exc:
            raise ContractError("PREAPPROVAL_EVIDENCE_SCHEMA") from exc
        if (
            checked_yaw is None
            and result["yaw_sample_binding_digest"] is not None
            or checked_yaw is not None
            and result["yaw_sample_binding_digest"]
            != checked_yaw["binding_digest"]
        ):
            raise ContractError("PREAPPROVAL_EVIDENCE_SCHEMA")
    return result


def _condition(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(CONDITION_FIELDS):
        raise ContractError("COVERAGE_CONDITION_FIELDS")
    result = copy.deepcopy(dict(value))
    for key in CONDITION_FIELDS:
        item = result[key]
        if key.endswith("_digest"):
            if not isinstance(item, str) or not DIGEST.fullmatch(item):
                raise ContractError("COVERAGE_CONDITION_DIGEST")
        elif key in {"yaw_deg", "x_mm", "y_mm"}:
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
                raise ContractError("COVERAGE_CONDITION_NUMBER")
        elif not isinstance(item, str) or not item or (key not in {"task_schema_version", "task"} and not SAFE_ID.fullmatch(item)):
            raise ContractError("COVERAGE_CONDITION_ID")
    return result


def _key(condition: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(condition[field] for field in CONDITION_FIELDS)


def _validate_domain_axes(
    conditions: Sequence[Mapping[str, Any]], code: str,
) -> None:
    common_fields = (
        "task_schema_version", "task", "robot_system_id",
        "collection_profile_digest",
    )
    expected = tuple(conditions[0][field] for field in common_fields)
    if any(
        tuple(item[field] for field in common_fields) != expected
        for item in conditions[1:]
    ):
        raise ContractError(code)
    endpoints = {
        (
            item["place_id"], item["cell_calibration_id"],
            item["cell_calibration_digest"],
        )
        for item in conditions
    }
    if len(endpoints) == 1:
        return
    if conditions[0]["task"] != "pick_place" or len(endpoints) != 2:
        raise ContractError(code)
    if (
        len({item[0] for item in endpoints}) != 2
        or len({item[1] for item in endpoints}) != 2
        or len({item["object_profile_id"] for item in conditions}) != 1
        or len({item["grasp_profile_id"] for item in conditions}) != 1
    ):
        raise ContractError(code)


def _continuity(value: object) -> dict[str, dict[str, Any]]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping) or any(key not in CONTINUITY_FIELDS for key in value):
        raise ContractError("COVERAGE_CONTINUITY_FIELDS")
    result = {}
    for field in CONTINUITY_FIELDS:
        evidence = value.get(field, {"status": "NOT_AVAILABLE", "value": None, "flags": []})
        if (not isinstance(evidence, Mapping) or set(evidence) != {"status", "value", "flags"}
                or not isinstance(evidence["status"], str) or evidence["status"] not in EVIDENCE_STATUS or not isinstance(evidence["flags"], list)
                or any(not isinstance(flag, str) for flag in evidence["flags"])):
            raise ContractError("COVERAGE_CONTINUITY_EVIDENCE")
        item = evidence["value"]
        valid = {
            "phase_continuity": type(item) is bool,
            "phase_status_flags": isinstance(item, list) and all(isinstance(flag, str) for flag in item),
            "close_feedback_in_window": type(item) is bool,
            "lift_feedback_delta": not isinstance(item, bool) and isinstance(item, (int, float)) and math.isfinite(item),
            "terminal_to_next_gap": not isinstance(item, bool) and isinstance(item, (int, float)) and math.isfinite(item),
        }[field]
        if evidence["status"] == "NOT_AVAILABLE":
            valid = item is None
        if not valid:
            raise ContractError("COVERAGE_CONTINUITY_EVIDENCE")
        result[field] = copy.deepcopy(dict(evidence))
    return result


def build_coverage_report(
    *, collection_profile_id: str, domain: Sequence[Mapping[str, Any]],
    episodes: Sequence[Mapping[str, Any]], slots: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Count stored evidence and suggest one unblocked least-approved domain condition."""
    if not isinstance(collection_profile_id, str) or not SAFE_ID.fullmatch(collection_profile_id) or not domain:
        raise ContractError("COVERAGE_PROFILE")
    conditions = sorted((_condition(item) for item in domain), key=_key)
    keys = [_key(item) for item in conditions]
    if len(keys) != len(set(keys)):
        raise ContractError("COVERAGE_DOMAIN_DUPLICATE")
    _validate_domain_axes(conditions, "COVERAGE_MIXED_DOMAIN")

    cells = {_key(item): {"condition": item, "counts": {name: 0 for name in COUNTS}, "trajectory_continuity": []} for item in conditions}
    episode_ids: set[str] = set()
    for episode in episodes:
        if not isinstance(episode, Mapping) or set(episode) != {"episode_id", "condition", "admission_state", "evidence_digests", "trajectory_continuity"}:
            raise ContractError("COVERAGE_EPISODE_FIELDS")
        episode_id, state = episode["episode_id"], episode["admission_state"]
        if not isinstance(episode_id, str) or not SAFE_ID.fullmatch(episode_id) or episode_id in episode_ids or state not in ADMISSION_STATES:
            raise ContractError("COVERAGE_EPISODE_ID")
        episode_ids.add(episode_id)
        condition = _condition(episode["condition"])
        if _key(condition) not in cells:
            raise ContractError("COVERAGE_EPISODE_OUTSIDE_DOMAIN")
        digests = episode["evidence_digests"]
        if not isinstance(digests, Mapping) or set(digests) != EVIDENCE_DIGESTS or any(not isinstance(v, str) or not DIGEST.fullmatch(v) for v in digests.values()):
            raise ContractError("COVERAGE_EPISODE_EVIDENCE")
        counts = cells[_key(condition)]["counts"]
        counts["collected"] += 1
        if state in {"TECHNICAL_PASS_CANDIDATE", "PENDING_REVIEW", "HUMAN_SEMANTIC_PASS", "HUMAN_TRAINING_APPROVED"}:
            counts["technical_pass_candidate"] += 1
        if state == "PENDING_REVIEW": counts["pending_review"] += 1
        if state in {"HUMAN_SEMANTIC_PASS", "HUMAN_TRAINING_APPROVED"}: counts["human_semantic_pass"] += 1
        if state == "HUMAN_TRAINING_APPROVED": counts["human_training_approved"] += 1
        if state == "REJECTED": counts["rejected"] += 1
        if state == "QUARANTINED": counts["quarantined"] += 1
        cells[_key(condition)]["trajectory_continuity"].append({"episode_id": episode_id, **_continuity(episode["trajectory_continuity"])})

    blocked = set()
    for slot in slots:
        if not isinstance(slot, Mapping) or set(slot) != {"condition", "state"} or slot["state"] not in SLOT_STATES:
            raise ContractError("COVERAGE_SLOT_FIELDS")
        condition = _condition(slot["condition"])
        if _key(condition) not in cells:
            raise ContractError("COVERAGE_SLOT_OUTSIDE_DOMAIN")
        if slot["state"] in BLOCKED_SLOT_STATES:
            blocked.add(_key(condition))

    ordered = [cells[key] for key in sorted(cells)]
    eligible = [cell for cell in ordered if _key(cell["condition"]) not in blocked and not cell["counts"]["pending_review"]]
    suggestion = min(eligible, key=lambda cell: (cell["counts"]["human_semantic_pass"], _key(cell["condition"]))) if eligible else None
    return {
        "schema_version": REPORT_SCHEMA,
        "collection_profile_id": collection_profile_id,
        "domain_digest": canonical_digest(conditions),
        "cells": ordered,
        "suggest_next": None if suggestion is None else copy.deepcopy(suggestion["condition"]),
        "authority": "REPORT_ONLY",
    }


def validate_coverage_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one canonical report without publishing it."""
    if not isinstance(report, Mapping) or set(report) != REPORT_FIELDS or report.get("schema_version") != REPORT_SCHEMA or report.get("authority") != "REPORT_ONLY":
        raise ContractError("COVERAGE_REPORT_SCHEMA")
    profile = report.get("collection_profile_id")
    if not isinstance(profile, str) or not SAFE_ID.fullmatch(profile):
        raise ContractError("COVERAGE_PROFILE")
    cells = report.get("cells")
    if not isinstance(cells, list) or not cells:
        raise ContractError("COVERAGE_REPORT_CELLS")
    conditions = []
    for cell in cells:
        if not isinstance(cell, Mapping) or set(cell) != {"condition", "counts", "trajectory_continuity"}:
            raise ContractError("COVERAGE_REPORT_CELLS")
        condition = _condition(cell["condition"])
        counts = cell["counts"]
        continuity = cell["trajectory_continuity"]
        if (not isinstance(counts, Mapping) or set(counts) != set(COUNTS)
                or any(type(count) is not int or count < 0 for count in counts.values())
                or not isinstance(continuity, list)):
            raise ContractError("COVERAGE_REPORT_CELLS")
        for evidence in continuity:
            if not isinstance(evidence, Mapping) or set(evidence) != {"episode_id", *CONTINUITY_FIELDS} or not isinstance(evidence["episode_id"], str) or not SAFE_ID.fullmatch(evidence["episode_id"]):
                raise ContractError("COVERAGE_REPORT_CELLS")
            _continuity({field: evidence[field] for field in CONTINUITY_FIELDS})
        conditions.append(condition)
    if conditions != sorted(conditions, key=_key) or len({_key(item) for item in conditions}) != len(conditions):
        raise ContractError("COVERAGE_REPORT_DOMAIN")
    _validate_domain_axes(conditions, "COVERAGE_REPORT_DOMAIN")
    if report.get("domain_digest") != canonical_digest(conditions):
        raise ContractError("COVERAGE_REPORT_DOMAIN")
    suggestion = report.get("suggest_next")
    if suggestion is not None and _key(_condition(suggestion)) not in {_key(item) for item in conditions}:
        raise ContractError("COVERAGE_REPORT_SUGGESTION")
    canonical_digest(report)
    return copy.deepcopy(dict(report))


def write_coverage_report(report: Mapping[str, Any], *, root: str | Path = "outputs/data_factory/coverage") -> Path:
    """Atomically publish only the canonical profile-owned coverage report."""
    value = validate_coverage_report(report)
    target = Path(root) / value["collection_profile_id"] / "coverage_report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(target, value)
    return target


def build_and_publish_coverage_report(
    *, collection_profile_id: str, domain: Sequence[Mapping[str, Any]],
    stored_episodes: Sequence[Mapping[str, Any]], slots: Sequence[Mapping[str, Any]] = (),
    root: str | Path = "outputs/data_factory/coverage",
) -> Path:
    """Strictly load stored P5 evidence, normalize it, and publish one report."""
    conditions = [_condition(item) for item in domain]
    episodes = []
    for source in stored_episodes:
        if not isinstance(source, Mapping) or set(source) != STORED_EPISODE_FIELDS:
            raise ContractError("COVERAGE_STORED_EPISODE_FIELDS")
        episode_id = source["episode_id"]
        if not isinstance(episode_id, str) or not SAFE_ID.fullmatch(episode_id):
            raise ContractError("COVERAGE_STORED_EPISODE_ID")
        values = {}
        for name in ("job_spec", "preapproval_evidence", "technical_validator", "candidate_admission"):
            expected = source[f"{name}_digest"]
            if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
                raise ContractError("COVERAGE_STORED_DIGEST")
            value = load_json_strict(source[f"{name}_path"])
            if canonical_digest(value) != expected:
                raise ContractError("COVERAGE_STORED_DIGEST")
            values[name] = value

        job = normalize_job_spec(values["job_spec"], now=datetime.min.replace(tzinfo=timezone.utc))
        try:
            preapproval = validate_preapproval_evidence(
                values["preapproval_evidence"],
            )
        except ContractError as exc:
            raise ContractError("COVERAGE_PLAN_EVIDENCE") from exc
        technical = values["technical_validator"]
        admission = values["candidate_admission"]
        envelope = preapproval.get("plan_envelope") if isinstance(preapproval, Mapping) else None
        plan = envelope.get("plan") if isinstance(envelope, Mapping) else None
        safety = envelope.get("precommit_safety") if isinstance(envelope, Mapping) else None
        precommit = envelope.get("precommit_evidence") if isinstance(envelope, Mapping) else None
        bindings = plan.get("binding_digests") if isinstance(plan, Mapping) else None
        if (
            preapproval.get("run_id") != episode_id
            or not isinstance(envelope, Mapping)
            or set(envelope) != {"plan", "precommit_safety", "precommit_evidence", "operator_summary"}
            or canonical_digest(envelope) != preapproval.get("plan_envelope_digest")
            or not isinstance(plan, Mapping)
            or plan.get("schema_version") != "fr5.pickup_plan.v3"
            or plan.get("run_id") != episode_id
            or canonical_digest(plan) != preapproval.get("plan_digest")
            or not isinstance(safety, Mapping)
            or safety.get("schema_version") != "data_factory.precommit_safety.v1"
            or safety.get("run_id") != episode_id
            or safety.get("approved_plan_digest") != preapproval.get("plan_digest")
            or not isinstance(precommit, Mapping)
            or precommit.get("schema_version") != "data_factory.precommit_evidence.v1"
            or precommit.get("run_id") != episode_id
            or precommit.get("approved_plan_digest") != preapproval.get("plan_digest")
            or not isinstance(bindings, Mapping)
            or set(bindings) != PLAN_BINDING_DIGEST_FIELDS
            or any(not isinstance(value, str) or not DIGEST.fullmatch(value) for value in bindings.values())
        ):
            raise ContractError("COVERAGE_PLAN_EVIDENCE")
        resolved_job_digest = canonical_digest({
            "job": job,
            "input_digests": {name: bindings[name] for name in RESOLVED_INPUT_DIGEST_FIELDS},
        })
        if (
            plan.get("resolved_job_digest") != resolved_job_digest
            or preapproval.get("resolved_job_digest") != resolved_job_digest
            or plan.get("robot_system_id") != job["robot_system_id"]
        ):
            raise ContractError("COVERAGE_JOB_BINDING")
        if (
            set(technical) != TECHNICAL_FIELDS
            or technical.get("schema_version") != "data_factory.technical_validator_result.v1"
            or technical.get("run_id") != episode_id
            or technical.get("status") not in {"PASS", "FAIL"}
            or any(not isinstance(technical.get(key), str) or not DIGEST.fullmatch(technical[key]) for key in ("resolved_job_digest", "plan_digest", "result_digest"))
            or not isinstance(technical.get("dataset_root"), str)
            or isinstance(technical.get("expected_fps"), bool)
            or not isinstance(technical.get("expected_fps"), (int, float))
            or not math.isfinite(technical["expected_fps"])
            or technical["expected_fps"] <= 0
        ):
            raise ContractError("COVERAGE_TECHNICAL_VALIDATOR")
        if technical["resolved_job_digest"] != resolved_job_digest or technical["plan_digest"] != preapproval["plan_digest"]:
            raise ContractError("COVERAGE_PLAN_BINDING")
        try:
            admission = validate_candidate_admission(admission)
        except ContractError as exc:
            raise ContractError("COVERAGE_CANDIDATE_ADMISSION") from exc
        if (
            admission["run_id"] != episode_id
            or admission["checklist_id"]
            != task_review_checklist_id(job["task"])
            or admission["review_context_digest"] != canonical_digest({
                "run_id": episode_id,
                "resolved_job_digest": resolved_job_digest,
                "plan_digest": preapproval["plan_digest"],
                "technical_validator_digest": canonical_digest(technical),
            })
        ):
            raise ContractError("COVERAGE_CANDIDATE_ADMISSION")
        pending = admission["semantic_status"] == "PENDING"

        matches = [condition for condition in conditions if job["job_id"] == episode_id and job["schema_version"] == condition["task_schema_version"] and all(job[field] == condition[field] for field in (
            "task", "robot_system_id", "place_id", "cell_calibration_id",
            "yaw_deg", "x_mm", "y_mm", "object_profile_id", "grasp_profile_id",
        )) and job["collection_profile_id"] == collection_profile_id
            and condition["cell_calibration_digest"] == bindings["cell_calibration"]
            and condition["motion_recipe_digest"] == bindings["motion_qualification"]
            and condition["collection_profile_digest"] == bindings["collection_profile"]]
        if len(matches) != 1:
            raise ContractError("COVERAGE_JOB_BINDING")
        state = (
            "REJECTED" if technical["status"] == "FAIL" or admission["operational_gate"] == "FAIL" or admission["semantic_status"] in {"FAIL", "UNCERTAIN"}
            else "PENDING_REVIEW" if pending else "HUMAN_SEMANTIC_PASS"
        )
        episodes.append({
            "episode_id": episode_id, "condition": matches[0], "admission_state": state,
            "evidence_digests": {
                "job_spec": source["job_spec_digest"],
                "technical_validator_result": source["technical_validator_digest"],
                "candidate_admission": source["candidate_admission_digest"],
            },
            "trajectory_continuity": {},
        })
    return write_coverage_report(
        build_coverage_report(collection_profile_id=collection_profile_id, domain=conditions, episodes=episodes, slots=slots),
        root=root,
    )


def main(argv=None) -> int:
    parser = ContractArgumentParser(description=__doc__)
    parser.add_argument("--domain-manifest", required=True)
    parser.add_argument("--stored-episodes", required=True)
    parser.add_argument("--output-root", required=True)
    try:
        args = parser.parse_args(argv)
        domain = load_json_strict(Path(args.domain_manifest).read_text(encoding="utf-8"))
        stored = load_json_strict(Path(args.stored_episodes).read_text(encoding="utf-8"))
        if set(domain) != {"schema_version", "collection_profile_id", "conditions", "slots"} or domain.get("schema_version") != DOMAIN_SCHEMA:
            raise ContractError("COVERAGE_DOMAIN_MANIFEST")
        if set(stored) != {"schema_version", "episodes"} or stored.get("schema_version") != STORED_EPISODES_SCHEMA:
            raise ContractError("COVERAGE_STORED_EPISODES_MANIFEST")
        if not isinstance(domain["conditions"], list) or not isinstance(domain["slots"], list) or not isinstance(stored["episodes"], list):
            raise ContractError("COVERAGE_MANIFEST_VALUES")
        path = build_and_publish_coverage_report(
            collection_profile_id=domain["collection_profile_id"],
            domain=domain["conditions"], stored_episodes=stored["episodes"],
            slots=domain["slots"], root=args.output_root,
        )
        print(json.dumps(load_json_strict(path), sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0
    except (ContractError, OSError, UnicodeError) as exc:
        print(exc.code if isinstance(exc, ContractError) else "COVERAGE_IO", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
