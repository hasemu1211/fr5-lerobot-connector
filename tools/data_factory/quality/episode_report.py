"""Read-only aggregation of quality attributes; never a training-admission gate."""
from __future__ import annotations

import hashlib
import json
import math
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.fr5_data_factory import ContractError, DIGEST, SAFE_ID, canonical_digest, compose_rigid_transform, inverse_rigid_transform, load_json_strict, normalize_job_spec, resolve_pose, validate_rigid_transform
from tools.data_factory.quality.coverage_report import CANDIDATE_FIELDS, PLAN_BINDING_DIGEST_FIELDS, RESOLVED_INPUT_DIGEST_FIELDS, STORED_EPISODE_FIELDS, TECHNICAL_FIELDS
from tools.data_factory.quality.phase_events import validate_phase_event
from tools.data_factory.quality.phase_metrics import ATTRIBUTE_SCHEMA, STATUS, phase_row_windows, quality_attribute


REPORT_SCHEMA = "data_factory.episode_quality.v1"
REPORT_KEYS = frozenset({"schema_version", "run_id", "resolved_job_digest", "plan_digest", "technical_validator", "attributes", "status", "flags"})
ATTRIBUTE_KEYS = frozenset({"schema_version", "attribute", "run_id", "resolved_job_digest", "plan_digest", "source_digests", "status", "metrics", "flags"})
TECHNICAL_REFERENCE_KEYS = frozenset({"schema_version", "status", "result_digest"})
TECHNICAL_REFERENCE_SCHEMA = "data_factory.technical_validator_ref.v1"
FK_TF_QUALIFICATION_KEYS = frozenset({"schema_version", "qualification_status", "resolved_job_digest", "plan_digest", "robot_description_digest", "tcp_digest", "motion_qualification_digest", "recorder_rows_digest", "recorder_ros_clock_type", "phase_events_digest", "joint_order", "same_sample_tf_agreement"})
FK_TF_QUALIFICATION_REFERENCE_KEYS = frozenset({"path", "digest"})
FK_TF_AGREEMENT_KEYS = frozenset({"status", "sample_count", "position_tolerance_m", "orientation_tolerance_rad", "max_position_error_m", "max_orientation_error_rad"})
OBJECT_PHASES = ("PREGRASP_PTP", "APPROACH_STOP_LIN", "FINAL_APPROACH_LIN", "GRIPPER_CLOSE")
JOINT_ORDER = ("j1", "j2", "j3", "j4", "j5", "j6")


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
        value = load_json_strict(reference[f"{name}_path"])
        if canonical_digest(value) != expected:
            raise ContractError("OBJECT_FRAME_SOURCE_DIGEST")
        values[name] = value
    return values


def _object_frame_binding(accepted_episode: Mapping[str, Any], resolved_job: Mapping[str, Any], motion_qualification: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str], str, str]:
    values = _accepted_episode_sources(accepted_episode)
    episode_id = accepted_episode["episode_id"]
    job, preapproval, technical, admission = (values[name] for name in ("job_spec", "preapproval_evidence", "technical_validator", "candidate_admission"))
    job = normalize_job_spec(job, now=datetime.min.replace(tzinfo=timezone.utc))
    envelope = preapproval.get("plan_envelope") if isinstance(preapproval, Mapping) else None
    plan = envelope.get("plan") if isinstance(envelope, Mapping) else None
    bindings = plan.get("binding_digests") if isinstance(plan, Mapping) else None
    safety = envelope.get("precommit_safety") if isinstance(envelope, Mapping) else None
    precommit = envelope.get("precommit_evidence") if isinstance(envelope, Mapping) else None
    if (
        set(preapproval) != {"schema_version", "run_id", "resolved_job_digest", "plan_digest", "plan_envelope", "plan_envelope_digest"}
        or preapproval.get("schema_version") != "data_factory.preapproval_evidence.v1"
        or preapproval.get("run_id") != episode_id
        or not isinstance(envelope, Mapping)
        or set(envelope) != {"plan", "precommit_safety", "precommit_evidence", "operator_summary"}
        or canonical_digest(envelope) != preapproval.get("plan_envelope_digest")
        or not isinstance(plan, Mapping)
        or plan.get("schema_version") != "fr5.pickup_plan.v3"
        or plan.get("run_id") != episode_id
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
        or admission.get("checklist_id") != "pickup-v2"
        or admission.get("semantic_status") != "PASS"
        or not isinstance(admission.get("reviewed_by"), str)
        or not SAFE_ID.fullmatch(admission["reviewed_by"])
        or not isinstance(admission.get("reviewed_at"), str)
        or not admission["reviewed_at"]
        or admission.get("reason") is not None
        or admission.get("review_context_digest") != canonical_digest({"run_id": episode_id, "resolved_job_digest": preapproval["resolved_job_digest"], "plan_digest": preapproval["plan_digest"], "technical_validator_digest": canonical_digest(technical)})
    ):
        raise ContractError("OBJECT_FRAME_ACCEPTED_EPISODE")
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
    if (
        not isinstance(motion_qualification, Mapping)
        or motion_qualification.get("schema_version") != "data_factory.motion_qualification.v1"
        or motion_qualification.get("qualification_status") != "QUALIFIED"
        or canonical_digest(motion_qualification) != bindings["motion_qualification"]
        or any(motion_qualification.get(key) != job.get(key) for key in ("robot_system_id", "cell_calibration_id", "object_profile_id", "grasp_profile_id"))
        or motion_qualification.get("profile_digests") != {key: inputs[key] for key in ("robot_system", "cell_calibration", "object_profile", "grasp_profile")}
        or motion_qualification.get("robot_description_digest") != bindings["robot_description_digest"]
        or not isinstance(motion_qualification.get("frames"), Mapping)
        or motion_qualification["frames"] != {"planning_frame": "base_link", "planning_group": "fairino5_v6_group", "tool_link": "wrist3_link"}
    ):
        raise ContractError("OBJECT_FRAME_BINDING")
    validate_rigid_transform(motion_qualification.get("tool_to_tcp"), "OBJECT_FRAME_BINDING")
    validate_rigid_transform(motion_qualification.get("datum_to_tcp_grasp"), "OBJECT_FRAME_BINDING")
    pose = resolve_pose(resolved_job)
    transform = validate_rigid_transform({"translation_m": pose["position_base_m"], "rotation_columns": pose["rotation_base_columns"]}, "OBJECT_FRAME_BINDING")
    source_digests = {f"accepted_{name}": accepted_episode[f"{name}_digest"] for name in values}
    source_digests.update({f"binding_{key}": value for key, value in bindings.items()})
    source_digests["tcp"] = robot["tcp_digest"]
    return transform, source_digests, resolved_job["resolved_job_digest"], preapproval["plan_digest"]


def _not_available(reason: str) -> dict[str, str]:
    return {"status": "NOT_AVAILABLE", "reason": reason}


def _fk_tf_qualification_reference(reference: Mapping[str, Any] | None) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if reference is None:
        return None, None, "FK_TF_QUALIFICATION_MISSING"
    if not isinstance(reference, Mapping) or set(reference) != FK_TF_QUALIFICATION_REFERENCE_KEYS or not isinstance(reference.get("path"), (str, Path)) or not isinstance(reference.get("digest"), str) or not DIGEST.fullmatch(reference["digest"]):
        return None, None, "FK_TF_QUALIFICATION_REFERENCE_INVALID"
    path = Path(reference["path"])
    if not path.is_file():
        return None, None, "FK_TF_QUALIFICATION_REFERENCE_INVALID"
    try:
        qualification = load_json_strict(path)
        if canonical_digest(qualification) != reference["digest"]:
            return None, None, "FK_TF_QUALIFICATION_DIGEST_MISMATCH"
    except (ContractError, OSError, TypeError, ValueError):
        return None, None, "FK_TF_QUALIFICATION_REFERENCE_INVALID"
    return qualification, reference["digest"], None


def _numbers(text: str | None, default: tuple[float, float, float]) -> list[float]:
    try:
        values = [float(value) for value in text.split()] if text is not None else list(default)
    except (AttributeError, ValueError) as exc:
        raise ContractError("FK_TF_URDF") from exc
    if len(values) != 3 or any(not math.isfinite(value) for value in values):
        raise ContractError("FK_TF_URDF")
    return values


def _rpy_transform(xyz: Sequence[float], rpy: Sequence[float]) -> dict[str, Any]:
    roll, pitch, yaw = rpy
    cr, sr, cp, sp, cy, sy = math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch), math.cos(yaw), math.sin(yaw)
    rows = [[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr], [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr], [-sp, cp * sr, cp * cr]]
    return {"translation_m": list(xyz), "rotation_columns": [[rows[row][column] for row in range(3)] for column in range(3)]}


def _joint_transform(axis: Sequence[float], value: float, joint_type: str) -> dict[str, Any]:
    length = math.sqrt(sum(component * component for component in axis))
    if length <= 1e-12 or not math.isfinite(value):
        raise ContractError("FK_TF_URDF")
    x, y, z = (component / length for component in axis)
    if joint_type == "prismatic":
        return _rpy_transform([value * x, value * y, value * z], [0., 0., 0.])
    cosine, sine, one_minus = math.cos(value), math.sin(value), 1 - math.cos(value)
    rows = [[cosine + x * x * one_minus, x * y * one_minus - z * sine, x * z * one_minus + y * sine], [y * x * one_minus + z * sine, cosine + y * y * one_minus, y * z * one_minus - x * sine], [z * x * one_minus - y * sine, z * y * one_minus + x * sine, cosine + z * z * one_minus]]
    return {"translation_m": [0., 0., 0.], "rotation_columns": [[rows[row][column] for row in range(3)] for column in range(3)]}


def _urdf_chain(urdf: str | Path, expected_digest: str, tool_link: str) -> list[tuple[str, str, dict[str, Any], list[float]]]:
    try:
        data = Path(urdf).read_bytes()
        root = ET.fromstring(data)
    except (OSError, ET.ParseError, TypeError, ValueError) as exc:
        raise ContractError("FK_TF_URDF") from exc
    if "sha256:" + hashlib.sha256(data).hexdigest() != expected_digest or root.tag != "robot":
        raise ContractError("FK_TF_URDF")
    by_child = {}
    for joint in root.findall("joint"):
        parent, child = joint.find("parent"), joint.find("child")
        if parent is None or child is None or "link" not in parent.attrib or "link" not in child.attrib or child.attrib["link"] in by_child:
            raise ContractError("FK_TF_URDF")
        origin, axis = joint.find("origin"), joint.find("axis")
        transform = _rpy_transform(_numbers(origin.get("xyz") if origin is not None else None, (0., 0., 0.)), _numbers(origin.get("rpy") if origin is not None else None, (0., 0., 0.)))
        by_child[child.attrib["link"]] = (parent.attrib["link"], joint.get("name") or "", joint.get("type") or "", transform, _numbers(axis.get("xyz") if axis is not None else None, (1., 0., 0.)))
    chain, child, seen = [], tool_link, set()
    while child != "base_link":
        if child in seen or child not in by_child:
            raise ContractError("FK_TF_URDF")
        seen.add(child)
        parent, name, joint_type, origin, axis = by_child[child]
        if joint_type not in {"fixed", "revolute", "continuous", "prismatic"}:
            raise ContractError("FK_TF_URDF")
        chain.append((name, joint_type, origin, axis))
        child = parent
    chain.reverse()
    if tuple(name for name, joint_type, _, _ in chain if joint_type != "fixed") != JOINT_ORDER:
        raise ContractError("FK_TF_URDF")
    return chain


def _base_tcp_rows(*, urdf: str | Path, robot_description_digest: str, tool_link: str, tool_to_tcp: Mapping[str, Any], recorder_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    chain = _urdf_chain(urdf, robot_description_digest, tool_link)
    tcp = validate_rigid_transform(tool_to_tcp, "FK_TF_TCP")
    transforms = []
    for row in recorder_rows:
        state = row.get("observation.state") if isinstance(row, Mapping) else None
        if not isinstance(state, Sequence) or isinstance(state, (str, bytes)) or len(state) < len(JOINT_ORDER):
            raise ContractError("FK_TF_RECORDER_ROW")
        values = {name: value for name, value in zip(JOINT_ORDER, state)}
        transform = _rpy_transform([0., 0., 0.], [0., 0., 0.])
        for name, joint_type, origin, axis in chain:
            transform = compose_rigid_transform(transform, origin)
            if joint_type != "fixed":
                value = values[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ContractError("FK_TF_RECORDER_ROW")
                transform = compose_rigid_transform(transform, _joint_transform(axis, float(value), joint_type))
        transforms.append(compose_rigid_transform(transform, tcp))
    return transforms


def _fk_tf_metrics(*, qualification: Mapping[str, Any] | None, qualification_digest: str | None, qualification_error: str | None, urdf: str | Path | None, motion_qualification: Mapping[str, Any], recorder_rows: Sequence[Mapping[str, Any]], recorder_rows_digest: str, recorder_ros_clock_type: str, events: Sequence[Mapping[str, Any]], object_transform: Mapping[str, Any], run_id: str, resolved_job_digest: str, plan_digest: str, source_digests: Mapping[str, str]) -> dict[str, Any]:
    if qualification_error is not None:
        return _not_available(qualification_error)
    if not isinstance(qualification, Mapping) or set(qualification) != FK_TF_QUALIFICATION_KEYS or qualification.get("schema_version") != "data_factory.fk_tf_qualification.v1":
        return _not_available("FK_TF_QUALIFICATION_INVALID")
    if qualification.get("qualification_status") != "QUALIFIED":
        return _not_available("FK_TF_QUALIFICATION_NOT_QUALIFIED")
    try:
        parsed_rows = list(recorder_rows)
        if any(not isinstance(row, Mapping) for row in parsed_rows):
            return _not_available("FK_TF_RECORDER_ROW_INVALID")
        recorder_rows_payload_digest = canonical_digest(parsed_rows)
    except (ContractError, TypeError, ValueError):
        return _not_available("FK_TF_RECORDER_ROW_INVALID")
    if not isinstance(recorder_rows_digest, str) or not DIGEST.fullmatch(recorder_rows_digest) or recorder_rows_payload_digest != recorder_rows_digest:
        return _not_available("FK_TF_RECORDER_ROWS_DIGEST_MISMATCH")
    try:
        parsed_events = [validate_phase_event(event) for event in events]
        phase_events_digest = canonical_digest(parsed_events)
    except (ContractError, TypeError, ValueError):
        return _not_available("FK_TF_PHASE_EVENTS_INVALID")
    if any(event["run_id"] != run_id or event["plan_digest"] != plan_digest for event in parsed_events):
        return _not_available("FK_TF_PHASE_EVENT_BINDING_MISMATCH")
    try:
        expected = {
            "resolved_job_digest": resolved_job_digest, "plan_digest": plan_digest,
            "robot_description_digest": source_digests["binding_robot_description_digest"], "tcp_digest": source_digests["tcp"],
            "motion_qualification_digest": source_digests["binding_motion_qualification"], "recorder_rows_digest": recorder_rows_digest,
            "recorder_ros_clock_type": recorder_ros_clock_type,
            "phase_events_digest": phase_events_digest, "joint_order": list(JOINT_ORDER),
        }
    except (ContractError, KeyError, TypeError, ValueError):
        return _not_available("FK_TF_QUALIFICATION_BINDING_MISMATCH")
    if any(qualification.get(key) != value for key, value in expected.items()):
        return _not_available("FK_TF_QUALIFICATION_BINDING_MISMATCH")
    agreement = qualification.get("same_sample_tf_agreement")
    if not isinstance(agreement, Mapping) or set(agreement) != FK_TF_AGREEMENT_KEYS:
        return _not_available("FK_TF_QUALIFICATION_INVALID")
    numbers = [agreement[key] for key in ("position_tolerance_m", "orientation_tolerance_rad", "max_position_error_m", "max_orientation_error_rad")]
    if agreement.get("status") != "PASS" or isinstance(agreement.get("sample_count"), bool) or not isinstance(agreement.get("sample_count"), int) or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in numbers):
        return _not_available("FK_TF_AGREEMENT_NOT_QUALIFIED")
    if agreement["sample_count"] <= 0 or numbers[0] <= 0 or numbers[1] <= 0 or numbers[2] < 0 or numbers[3] < 0 or numbers[2] > numbers[0] or numbers[3] > numbers[1]:
        return _not_available("FK_TF_AGREEMENT_NOT_QUALIFIED")
    if source_digests.get("accepted_fk_tf_qualification") != qualification_digest:
        return _not_available("FK_TF_QUALIFICATION_PROVENANCE_UNAVAILABLE")
    if urdf is None:
        return _not_available("FK_TF_QUALIFICATION_BINDING_MISMATCH")
    try:
        windows, flags, _ = phase_row_windows(events=parsed_events, recorder_rows=parsed_rows, recorder_ros_clock_type=recorder_ros_clock_type)
    except (ContractError, KeyError, TypeError, ValueError):
        return _not_available("FK_TF_PHASE_ROWS_NOT_AVAILABLE")
    selected = [window for phase in OBJECT_PHASES for window in windows if window["phase"] == phase]
    if flags or len(selected) != len(OBJECT_PHASES) or any(not window["row_indices"] for window in selected):
        return _not_available("FK_TF_PHASE_ROWS_NOT_AVAILABLE")
    indices = sorted({index for window in selected for index in window["row_indices"]})
    try:
        parsed = dict(zip(indices, _base_tcp_rows(urdf=urdf, robot_description_digest=qualification["robot_description_digest"], tool_link=motion_qualification["frames"]["tool_link"], tool_to_tcp=motion_qualification["tool_to_tcp"], recorder_rows=[parsed_rows[index] for index in indices])))
    except ContractError as exc:
        return _not_available("FK_TF_QUALIFICATION_BINDING_MISMATCH" if exc.code in {"FK_TF_URDF", "FK_TF_TCP"} else "FK_TF_SAMPLE_BINDING_MISMATCH")
    inverse_object = inverse_rigid_transform(object_transform)
    object_tcp = {index: compose_rigid_transform(inverse_object, transform) for index, transform in parsed.items()}
    scalars = []
    for phase, window in zip(OBJECT_PHASES, selected):
        indices = window["row_indices"]
        path_m = sum(math.dist(object_tcp[left]["translation_m"], object_tcp[right]["translation_m"]) for left, right in zip(indices, indices[1:]))
        scalars.append({"phase": phase, "start_row_index": indices[0], "end_row_index": indices[-1], "tcp_translation_path_m": path_m})
    close_index = selected[-1]["row_indices"][-1]
    return {"status": "AVAILABLE", "close_row_reference": {"row_index": close_index, "target_ros_s": parsed_rows[close_index]["target_ros_s"]}, "T_object_tcp_at_close": object_tcp[close_index], "phase_scalars": scalars}


def object_frame_context_attribute(*, accepted_episode: Mapping[str, Any], resolved_job: Mapping[str, Any], motion_qualification: Mapping[str, Any], recorder_rows: Sequence[Mapping[str, Any]], recorder_rows_digest: str, recorder_ros_clock_type: str, events: Sequence[Mapping[str, Any]], fk_tf_qualification: Mapping[str, Any] | None = None, urdf: str | Path | None = None) -> dict[str, Any]:
    """Build declared static Object–EE context; per-row FK transforms stay transient."""
    transform, source_digests, resolved_job_digest, plan_digest = _object_frame_binding(accepted_episode, resolved_job, motion_qualification)
    if isinstance(recorder_rows_digest, str) and DIGEST.fullmatch(recorder_rows_digest):
        source_digests["recorder_rows"] = recorder_rows_digest
    try:
        source_digests["recorder_rows_payload"] = canonical_digest(list(recorder_rows))
        source_digests["phase_events"] = canonical_digest([validate_phase_event(event) for event in events])
    except (ContractError, TypeError, ValueError):
        pass
    qualification, qualification_digest, qualification_error = _fk_tf_qualification_reference(fk_tf_qualification)
    if qualification_digest is not None:
        source_digests["fk_tf_qualification"] = qualification_digest
    fk_metrics = _fk_tf_metrics(qualification=qualification, qualification_digest=qualification_digest, qualification_error=qualification_error, urdf=urdf, motion_qualification=motion_qualification, recorder_rows=recorder_rows, recorder_rows_digest=recorder_rows_digest, recorder_ros_clock_type=recorder_ros_clock_type, events=events, object_transform=transform, run_id=accepted_episode["episode_id"], resolved_job_digest=resolved_job_digest, plan_digest=plan_digest, source_digests=source_digests)
    metrics = {
        "frame_id": "base_link", "object_datum": "center", "pose_source": "A4_CALIBRATION_AND_JOB",
        "truth_scope": "DECLARED_STATIC_PREGRASP_TO_CLOSE", "pose_observation": "DECLARED_PLACEMENT_NOT_CAMERA_OBSERVED_ACTUAL_TRUTH",
        "T_base_object_datum_at_begin": transform, "fk_tf_metrics": fk_metrics,
        "post_close_object_pose": _not_available("POST_CLOSE_OBJECT_POSE_UNQUALIFIED"),
    }
    flags = [] if fk_metrics["status"] == "AVAILABLE" else [fk_metrics["reason"]]
    return quality_attribute(attribute="object_frame_context", run_id=accepted_episode["episode_id"], resolved_job_digest=resolved_job_digest, plan_digest=plan_digest, source_digests=source_digests, status="AVAILABLE", metrics=metrics, flags=flags)


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
        allowed = required | {"fk_tf_qualification", "urdf"}
        if not isinstance(object_frame_context_inputs, Mapping) or not required <= set(object_frame_context_inputs) <= allowed:
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
        attributes.append(object_frame_context_attribute(**object_frame_context_inputs, recorder_rows=recorder_rows, recorder_rows_digest=recorder_rows_digest, recorder_ros_clock_type=recorder_ros_clock_type, events=events))
    report = aggregate_episode_report(attributes, technical_validator=technical_validator)
    write_episode_report(path, report)
    return report
