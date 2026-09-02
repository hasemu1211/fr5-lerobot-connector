"""Read-only aggregation of quality attributes; never a training-admission gate."""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.fr5_data_factory import ContractError, DIGEST, MOTION_QUALIFICATION_KEYS_BY_SCHEMA, SAFE_ID, _cross, _dot, _mul, _sub, _timestamp, _unit, _vec, canonical_digest, load_json_strict, normalize_job_spec, resolve_pose, task_review_checklist_id, validate_rigid_transform
from tools.data_factory.quality.coverage_report import CANDIDATE_FIELDS, PLAN_BINDING_DIGEST_FIELDS, RESOLVED_INPUT_DIGEST_FIELDS, STORED_EPISODE_FIELDS, TECHNICAL_FIELDS, validate_preapproval_evidence
from tools.data_factory.quality.phase_metrics import ATTRIBUTE_SCHEMA, STATUS, quality_attribute


REPORT_SCHEMA = "data_factory.episode_quality.v1"
REPORT_KEYS = frozenset({"schema_version", "run_id", "resolved_job_digest", "plan_digest", "technical_validator", "attributes", "status", "flags"})
ATTRIBUTE_KEYS = frozenset({"schema_version", "attribute", "run_id", "resolved_job_digest", "plan_digest", "source_digests", "status", "metrics", "flags"})
TECHNICAL_REFERENCE_KEYS = frozenset({"schema_version", "status", "result_digest"})
TECHNICAL_REFERENCE_SCHEMA = "data_factory.technical_validator_ref.v1"


def validate_attribute_record(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != ATTRIBUTE_KEYS:
        raise ContractError("QUALITY_ATTRIBUTE_FIELDS")
    if value["schema_version"] != ATTRIBUTE_SCHEMA or value["status"] not in STATUS:
        raise ContractError("QUALITY_ATTRIBUTE_SCHEMA")
    if not isinstance(value["attribute"], str) or not value["attribute"] or not isinstance(value["run_id"], str) or not value["run_id"]:
        raise ContractError("QUALITY_ATTRIBUTE_ID")
    if any(not isinstance(value[key], str) or not DIGEST.fullmatch(value[key]) for key in ("resolved_job_digest", "plan_digest")):
        raise ContractError("QUALITY_ATTRIBUTE_BINDING")
    source_digests = value["source_digests"]
    if not isinstance(source_digests, dict) or not source_digests or any(not isinstance(key, str) or not DIGEST.fullmatch(digest) for key, digest in source_digests.items()):
        raise ContractError("QUALITY_SOURCE_DIGEST")
    if not isinstance(value["metrics"], dict) or not isinstance(value["flags"], list) or any(not isinstance(flag, str) for flag in value["flags"]):
        raise ContractError("QUALITY_ATTRIBUTE_CONTENT")
    return dict(value)


def _technical_reference(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != TECHNICAL_REFERENCE_KEYS:
        raise ContractError("TECHNICAL_VALIDATOR_REFERENCE")
    if value["schema_version"] != TECHNICAL_REFERENCE_SCHEMA or value["status"] not in {"PASS", "FAIL"} or not isinstance(value["result_digest"], str) or not DIGEST.fullmatch(value["result_digest"]):
        raise ContractError("TECHNICAL_VALIDATOR_REFERENCE")
    return dict(value)


def _accepted_episode_sources(reference: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(reference, Mapping) or set(reference) != STORED_EPISODE_FIELDS or not isinstance(reference["episode_id"], str) or not SAFE_ID.fullmatch(reference["episode_id"]):
        raise ContractError("OBJECT_FRAME_ACCEPTED_EPISODE")
    values = {}
    for name in ("job_spec", "preapproval_evidence", "technical_validator", "candidate_admission"):
        expected = reference[f"{name}_digest"]
        if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
            raise ContractError("OBJECT_FRAME_SOURCE_DIGEST")
        path = reference[f"{name}_path"]
        try:
            if not isinstance(path, (str, Path)) or not Path(path).is_file():
                raise ContractError("OBJECT_FRAME_SOURCE_DIGEST")
        except (OSError, TypeError, ValueError) as exc:
            raise ContractError("OBJECT_FRAME_SOURCE_DIGEST") from exc
        value = load_json_strict(Path(path))
        if canonical_digest(value) != expected:
            raise ContractError("OBJECT_FRAME_SOURCE_DIGEST")
        values[name] = value
    return values


def _object_frame_binding(accepted_episode: Mapping[str, Any], resolved_job: Mapping[str, Any], motion_qualification: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str], str, str]:
    values = _accepted_episode_sources(accepted_episode)
    episode_id = accepted_episode["episode_id"]
    job, preapproval, technical, admission = (values[name] for name in ("job_spec", "preapproval_evidence", "technical_validator", "candidate_admission"))
    job = normalize_job_spec(job, now=datetime.min.replace(tzinfo=timezone.utc))
    try:
        preapproval = validate_preapproval_evidence(preapproval)
    except ContractError as exc:
        raise ContractError("OBJECT_FRAME_ACCEPTED_EPISODE") from exc
    if job["job_id"] != episode_id:
        raise ContractError("OBJECT_FRAME_ACCEPTED_EPISODE")
    envelope = preapproval.get("plan_envelope") if isinstance(preapproval, Mapping) else None
    plan = envelope.get("plan") if isinstance(envelope, Mapping) else None
    bindings = plan.get("binding_digests") if isinstance(plan, Mapping) else None
    safety = envelope.get("precommit_safety") if isinstance(envelope, Mapping) else None
    precommit = envelope.get("precommit_evidence") if isinstance(envelope, Mapping) else None
    motion_schema = (
        motion_qualification.get("schema_version")
        if isinstance(motion_qualification, Mapping) else None
    )
    motion_keys = MOTION_QUALIFICATION_KEYS_BY_SCHEMA.get(motion_schema)
    if (
        preapproval.get("run_id") != episode_id
        or not isinstance(envelope, Mapping)
        or set(envelope) != {"plan", "precommit_safety", "precommit_evidence", "operator_summary"}
        or canonical_digest(envelope) != preapproval.get("plan_envelope_digest")
        or not isinstance(plan, Mapping)
        or plan.get("schema_version") != "fr5.pickup_plan.v3"
        or plan.get("run_id") != episode_id
        or plan.get("robot_system_id") != job["robot_system_id"]
        or canonical_digest(plan) != preapproval.get("plan_digest")
        or plan.get("resolved_job_digest") != preapproval.get("resolved_job_digest")
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
        raise ContractError("OBJECT_FRAME_PLAN_BINDING")
    if (
        set(technical) != TECHNICAL_FIELDS
        or technical.get("schema_version") != "data_factory.technical_validator_result.v1"
        or technical.get("run_id") != episode_id
        or technical.get("status") != "PASS"
        or any(not isinstance(technical.get(key), str) or not DIGEST.fullmatch(technical[key]) for key in ("resolved_job_digest", "plan_digest", "result_digest"))
        or not isinstance(technical.get("dataset_root"), str)
        or isinstance(technical.get("expected_fps"), bool)
        or not isinstance(technical.get("expected_fps"), (int, float))
        or not math.isfinite(technical["expected_fps"])
        or technical["expected_fps"] <= 0
        or technical.get("resolved_job_digest") != preapproval["resolved_job_digest"]
        or technical.get("plan_digest") != preapproval["plan_digest"]
        or set(admission) != CANDIDATE_FIELDS
        or admission.get("schema_version") != "data_factory.candidate_admission.v1"
        or admission.get("run_id") != episode_id
        or admission.get("operational_gate") != "PASS"
        or admission.get("operational_source") not in {"HIL_PROXY", "HUMAN_GATED"}
        or admission.get("checklist_id")
        != task_review_checklist_id(job["task"])
        or admission.get("semantic_status") != "PASS"
        or not isinstance(admission.get("reviewed_by"), str)
        or admission["reviewed_by"] == "HUMAN"
        or not SAFE_ID.fullmatch(admission["reviewed_by"])
        or not isinstance(admission.get("reviewed_at"), str)
        or not admission["reviewed_at"]
        or admission.get("reason") is not None
        or admission.get("review_context_digest") != canonical_digest({"run_id": episode_id, "resolved_job_digest": preapproval["resolved_job_digest"], "plan_digest": preapproval["plan_digest"], "technical_validator_digest": canonical_digest(technical)})
    ):
        raise ContractError("OBJECT_FRAME_ACCEPTED_EPISODE")
    _timestamp(admission["reviewed_at"], "OBJECT_FRAME_ACCEPTED_EPISODE", now=datetime.max.replace(tzinfo=timezone.utc))
    if not isinstance(resolved_job, Mapping) or set(resolved_job) != {"normalized_job", "input_digests", "resolved_job_digest", "robot", "collection_profile", "calibration", "object_profile", "grasp_profile"}:
        raise ContractError("OBJECT_FRAME_BINDING")
    inputs = resolved_job["input_digests"]
    calibration = resolved_job["calibration"]
    robot, object_profile, grasp = (resolved_job[key] for key in ("robot", "object_profile", "grasp_profile"))
    if any(not isinstance(value, Mapping) for value in (inputs, calibration, robot, object_profile, grasp, resolved_job["collection_profile"])):
        raise ContractError("OBJECT_FRAME_BINDING")
    if (
        resolved_job["normalized_job"] != job
        or not isinstance(inputs, Mapping)
        or set(inputs) != set(RESOLVED_INPUT_DIGEST_FIELDS)
        or any(not isinstance(value, str) or not DIGEST.fullmatch(value) for value in inputs.values())
        or any(canonical_digest(resolved_job[key]) != inputs[digest_key] for key, digest_key in (("robot", "robot_system"), ("collection_profile", "collection_profile"), ("object_profile", "object_profile"), ("grasp_profile", "grasp_profile")))
        or not isinstance(calibration, Mapping)
        or set(calibration) != {"center", "x", "y", "z", "document"}
        or not isinstance(calibration["document"], Mapping)
        or canonical_digest(calibration["document"]) != inputs["cell_calibration"]
        or canonical_digest({"job": job, "input_digests": dict(inputs)}) != resolved_job["resolved_job_digest"]
        or resolved_job["resolved_job_digest"] != preapproval["resolved_job_digest"]
        or any(inputs[key] != bindings[key] for key in RESOLVED_INPUT_DIGEST_FIELDS)
        or job.get("sheet_manifest_digest") != inputs["selected_sheet"]
        or robot.get("robot_system_id") != job.get("robot_system_id")
        or robot.get("base_frame") != "base_link"
        or calibration["document"].get("calibration_id") != job.get("cell_calibration_id")
        or calibration["document"].get("robot_system_id") != job.get("robot_system_id")
        or calibration["document"].get("place_id") != job.get("place_id")
        or object_profile.get("object_profile_id") != job.get("object_profile_id")
        or object_profile.get("datum") != "center"
        or grasp.get("grasp_profile_id") != job.get("grasp_profile_id")
        or grasp.get("object_profile_id") != job.get("object_profile_id")
        or robot.get("tcp_digest") != calibration["document"].get("tcp_digest")
    ):
        raise ContractError("OBJECT_FRAME_BINDING")
    document = calibration["document"]
    try:
        center, x_ref, _, normal = (_vec(document[key], "OBJECT_FRAME_BINDING") for key in ("center_base_m", "x_ref_base_m", "y_check_base_m", "table_normal_base"))
        z = _unit(normal, "OBJECT_FRAME_BINDING")
        delta = _sub(x_ref, center)
        x = _unit(_sub(delta, _mul(z, _dot(delta, z))), "OBJECT_FRAME_BINDING")
        derived = {"center": center, "x": x, "y": _unit(_cross(z, x), "OBJECT_FRAME_BINDING"), "z": z}
        if any(not math.isclose(actual, expected, rel_tol=0., abs_tol=1e-12) for key, expected_values in derived.items() for actual, expected in zip(_vec(calibration[key], "OBJECT_FRAME_BINDING"), expected_values)):
            raise ContractError("OBJECT_FRAME_BINDING")
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        raise ContractError("OBJECT_FRAME_BINDING") from exc
    if (
        not isinstance(motion_qualification, Mapping)
        or motion_keys is None
        or motion_qualification.get("qualification_status") != "QUALIFIED"
        or canonical_digest(motion_qualification) != bindings["motion_qualification"]
        or any(motion_qualification.get(key) != job.get(key) for key in ("robot_system_id", "cell_calibration_id", "object_profile_id", "grasp_profile_id"))
        or motion_qualification.get("profile_digests") != {key: inputs[key] for key in ("robot_system", "cell_calibration", "object_profile", "grasp_profile")}
        or motion_qualification.get("robot_description_digest") != bindings["robot_description_digest"]
        or not isinstance(motion_qualification.get("frames"), Mapping)
        or motion_qualification["frames"] != {"planning_frame": "base_link", "planning_group": "fairino5_v6_group", "tool_link": "wrist3_link"}
        or motion_schema == "data_factory.motion_qualification.v2"
        and (
            not isinstance(motion_qualification.get("planning_scene_profile_id"), str)
            or SAFE_ID.fullmatch(motion_qualification["planning_scene_profile_id"])
            is None
            or not isinstance(
                motion_qualification.get("planning_scene_profile_digest"), str,
            )
            or DIGEST.fullmatch(
                motion_qualification["planning_scene_profile_digest"]
            ) is None
        )
    ):
        raise ContractError("OBJECT_FRAME_BINDING")
    validate_rigid_transform(motion_qualification.get("tool_to_tcp"), "OBJECT_FRAME_BINDING")
    validate_rigid_transform(motion_qualification.get("datum_to_tcp_grasp"), "OBJECT_FRAME_BINDING")
    pose = resolve_pose({**resolved_job, "calibration": {**calibration, **derived}})
    transform = validate_rigid_transform({"translation_m": pose["position_base_m"], "rotation_columns": pose["rotation_base_columns"]}, "OBJECT_FRAME_BINDING")
    source_digests = {f"accepted_{name}": accepted_episode[f"{name}_digest"] for name in values}
    source_digests.update({f"binding_{key}": value for key, value in bindings.items()})
    source_digests["tcp"] = robot["tcp_digest"]
    return transform, source_digests, resolved_job["resolved_job_digest"], preapproval["plan_digest"]


def _not_available(reason: str) -> dict[str, str]:
    return {"status": "NOT_AVAILABLE", "reason": reason}


def object_frame_context_attribute(*, accepted_episode: Mapping[str, Any], resolved_job: Mapping[str, Any], motion_qualification: Mapping[str, Any]) -> dict[str, Any]:
    """Build declared static Object context without unowned FK/TF claims."""
    transform, source_digests, resolved_job_digest, plan_digest = _object_frame_binding(accepted_episode, resolved_job, motion_qualification)
    fk_metrics = _not_available("FK_TF_QUALIFICATION_MISSING")
    metrics = {
        "frame_id": "base_link", "object_datum": "center", "pose_source": "A4_CALIBRATION_AND_JOB",
        "truth_scope": "DECLARED_STATIC_PREGRASP_TO_CLOSE", "pose_observation": "DECLARED_PLACEMENT_NOT_CAMERA_OBSERVED_ACTUAL_TRUTH",
        "T_base_object_datum_at_begin": transform, "fk_tf_metrics": fk_metrics,
        "post_close_object_pose": _not_available("POST_CLOSE_OBJECT_POSE_UNQUALIFIED"),
    }
    return quality_attribute(attribute="object_frame_context", run_id=accepted_episode["episode_id"], resolved_job_digest=resolved_job_digest, plan_digest=plan_digest, source_digests=source_digests, status="AVAILABLE", metrics=metrics, flags=[fk_metrics["reason"]])


def aggregate_episode_report(attributes: Sequence[Mapping[str, Any]], *, technical_validator: Mapping[str, Any]) -> dict[str, Any]:
    """Bind already-computed attributes; no metric is recalculated or scored here."""
    parsed = [validate_attribute_record(attribute) for attribute in attributes]
    if not parsed:
        raise ContractError("QUALITY_ATTRIBUTES_MISSING")
    binding = tuple(parsed[0][key] for key in ("run_id", "resolved_job_digest", "plan_digest"))
    if any(tuple(attribute[key] for key in ("run_id", "resolved_job_digest", "plan_digest")) != binding for attribute in parsed):
        raise ContractError("QUALITY_ATTRIBUTE_BINDING")
    names = [attribute["attribute"] for attribute in parsed]
    if len(names) != len(set(names)):
        raise ContractError("QUALITY_ATTRIBUTE_DUPLICATE")
    technical = _technical_reference(technical_validator)
    statuses = {attribute["status"] for attribute in parsed}
    status = "ERROR" if "ERROR" in statuses else "FLAGGED" if "FLAGGED" in statuses else "AVAILABLE" if "AVAILABLE" in statuses else "NOT_AVAILABLE"
    flags = list(dict.fromkeys(flag for attribute in parsed for flag in attribute["flags"]))
    if technical["status"] == "FAIL":
        flags.append("TECHNICAL_VALIDATOR_FAIL")
        if status == "AVAILABLE":
            status = "FLAGGED"
    return {"schema_version": REPORT_SCHEMA, "run_id": binding[0], "resolved_job_digest": binding[1], "plan_digest": binding[2], "technical_validator": technical, "attributes": sorted(parsed, key=lambda attribute: attribute["attribute"]), "status": status, "flags": flags}


def write_episode_report(path: str | Path, report: Mapping[str, Any]) -> None:
    """Publish one small report without replacing an existing run artifact."""
    path = Path(path)
    if not isinstance(report, Mapping) or set(report) != REPORT_KEYS or aggregate_episode_report(report["attributes"], technical_validator=report["technical_validator"]) != dict(report):
        raise ContractError("QUALITY_REPORT_SCHEMA")
    if path.name != "episode_quality.json" or not path.parent.is_dir() or path.parent.is_symlink():
        raise ContractError("QUALITY_REPORT_PATH")
    data = (json.dumps(dict(report), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    except (OSError, TypeError, ValueError) as exc:
        raise ContractError("QUALITY_REPORT_IO", str(exc)) from exc


def build_episode_report(
    path: str | Path,
    *,
    run_id: str,
    resolved_job_digest: str,
    plan_digest: str,
    plan: Mapping[str, Any],
    phase_events_path: str | Path,
    recorder_rows: Sequence[Mapping[str, Any]],
    recorder_rows_digest: str,
    recorder_ros_clock_type: str,
    execution_evidence: Mapping[str, Any],
    technical_validator: Mapping[str, Any],
    stall_epsilon_rad: float,
    object_frame_context_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and exclusively publish the one report view; source payloads stay in place."""
    from tools.data_factory.quality.execution_metrics import joint_execution_attribute
    from tools.data_factory.quality.interaction_metrics import interaction_quality_attribute
    from tools.data_factory.quality.phase_events import read_phase_events
    from tools.data_factory.quality.phase_metrics import phase_timing_attribute
    from tools.data_factory.quality.plan_metrics import plan_quality_attribute

    events = read_phase_events(phase_events_path)
    if object_frame_context_inputs is not None:
        required = {"accepted_episode", "resolved_job", "motion_qualification"}
        if not isinstance(object_frame_context_inputs, Mapping) or set(object_frame_context_inputs) != required:
            raise ContractError("OBJECT_FRAME_CONTEXT_INPUTS")
    common = {"run_id": run_id, "resolved_job_digest": resolved_job_digest, "plan_digest": plan_digest}
    row_common = {**common, "plan": plan, "events": events, "recorder_rows": recorder_rows, "recorder_rows_digest": recorder_rows_digest, "recorder_ros_clock_type": recorder_ros_clock_type}
    attributes = [
        plan_quality_attribute(**common, plan=plan),
        phase_timing_attribute(**common, events=events, recorder_rows=recorder_rows, recorder_rows_digest=recorder_rows_digest, recorder_ros_clock_type=recorder_ros_clock_type),
        joint_execution_attribute(**row_common, stall_epsilon_rad=stall_epsilon_rad),
        interaction_quality_attribute(**row_common, execution_evidence=execution_evidence),
    ]
    if object_frame_context_inputs is not None:
        attributes.append(object_frame_context_attribute(**object_frame_context_inputs))
    report = aggregate_episode_report(attributes, technical_validator=technical_validator)
    write_episode_report(path, report)
    return report
