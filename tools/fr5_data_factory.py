#!/usr/bin/env python3
"""Fail-closed offline JobSpec validator and A4 datum-frame resolver."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

try:
    from a4_place_yaw.generate_place_yaw_a4 import (PAGE_H_MM, PAGE_W_MM, PLACE0_XY_MM, PRINT_X_MARGIN_MM, PRINT_Y_MARGIN_MM, REGISTRATION_FIRST_INSTALL, REGISTRATION_FIXED_SHEET_SWAP, TRANSFORM_CONTRACT, X_REF_XY_MM, Y_CHECK_XY_MM, family_digest_from_manifest)
except ImportError:
    from tools.a4_place_yaw.generate_place_yaw_a4 import (PAGE_H_MM, PAGE_W_MM, PLACE0_XY_MM, PRINT_X_MARGIN_MM, PRINT_Y_MARGIN_MM, REGISTRATION_FIRST_INSTALL, REGISTRATION_FIXED_SHEET_SWAP, TRANSFORM_CONTRACT, X_REF_XY_MM, Y_CHECK_XY_MM, family_digest_from_manifest)

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
JOB_KEYS = {"schema_version", "job_id", "task", "robot_system_id", "collection_profile_id", "place_id", "cell_calibration_id", "sheet_manifest_digest", "yaw_deg", "x_mm", "y_mm", "object_profile_id", "grasp_profile_id", "instruction", "episode_intent", "operator_or_agent_id", "approval_expiry", "dry_run_required"}
CALIBRATION_KEYS = {"schema_version", "calibration_id", "qualification_status", "robot_system_id", "place_id", "yaw0_manifest_digest", "a4_family_digest", "tcp_digest", "measurement_report_digest", "table_plane_measurement_digest", "center_base_m", "x_ref_base_m", "y_check_base_m", "table_normal_base", "print_source_scale_bar_measured_mm", "scale_bar_measured_mm", "limits", "measured_at"}
LIMIT_KEYS = {"max_scale_error_mm", "min_x_ref_separation_mm", "max_x_ref_distance_error_mm", "max_x_ref_out_of_plane_mm", "max_y_check_residual_mm", "combined_error_bound_mm"}
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
SHEET_KEYS = {"schema_version", "a4_family_digest", "place_id", "sheet_id", "page_mm", "yaw_deg", "place_spacing_mm", "print_calibration", "registration", "transform_contract", "grid_points"}
REGISTRATION_KEYS = {"origin", "x_ref", "verify", "first_install", "fixed_sheet_swap"}
POINT_KEYS = {"point_id", "local_uv_mm", "relative_pose_place0", "sheet_xy_mm", "job_pose"}
RELATIVE_KEYS = {"x_mm", "y_mm", "yaw_deg"}
PROFILE_KEYS = {
    "robot_system": {"schema_version", "robot_system_id", "qualification_status", "base_frame", "tcp_digest", "state_action_schema_digest"},
    "collection_profile": {"schema_version", "collection_profile_id", "qualification_status", "quality_contract_digest"},
    "object_profile": {"schema_version", "object_profile_id", "qualification_status", "object_datum_digest"},
    "grasp_profile": {"schema_version", "grasp_profile_id", "qualification_status", "object_profile_id", "grasp_margin_mm", "grasp_contract_digest"},
}
HOME_CANDIDATE_KEYS = {"schema_version", "home_candidate_id", "robot_system_id", "robot_model_name", "robot_description_digest", "joint_order", "ui_observation_deg", "nominal_target_deg", "observation_source", "feedback_capture_status", "qualification_status", "safety_status", "intended_use_after_qualification"}
HOME_JOINT_ORDER = ["j1", "j2", "j3", "j4", "j5", "j6"]
MOTION_PHASES = ("PREGRASP_PTP", "APPROACH_STOP_LIN", "FINAL_APPROACH_LIN", "GRIPPER_CLOSE", "LIFT_LIN", "LOWER_LIN", "GRIPPER_OPEN", "RETREAT_LIN", "SAFE_POSE_PTP")
MOTION_QUALIFICATION_KEYS = {"schema_version", "motion_qualification_id", "qualification_status", "robot_system_id", "cell_calibration_id", "object_profile_id", "grasp_profile_id", "profile_digests", "home_candidate_digest", "robot_description_digest", "moveit_config_digest", "planning_scene_digest", "frames", "tool_to_tcp", "datum_to_tcp_grasp", "offsets_m", "gripper_positions_m", "qualified_safe_joint_positions_rad", "goal_tolerances", "max_joint_state_age_s", "phase_limits", "qualified_at"}
MOTION_PROFILE_DIGESTS = {"robot_system", "cell_calibration", "object_profile", "grasp_profile"}
MOTION_FRAMES = {"planning_frame", "planning_group", "tool_link"}
MOTION_OFFSETS = {"pregrasp", "approach_stop", "lift", "retreat"}
MOTION_GOAL_TOLERANCES = {"position_m", "orientation_rad", "joint_rad"}


class ContractError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class ContractArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ContractError("CLI_USAGE", message)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("JSON_DUPLICATE_KEY", key)
        result[key] = value
    return result


def _nonfinite(value):
    raise ContractError("JSON_NONFINITE", value)


def canonical_digest(value: object) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractError("JSON_NONFINITE", str(exc)) from exc
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def load_json_strict(source: str | Path) -> dict:
    """Decode one JSON object, including a path supplied as str or Path."""
    if isinstance(source, Path) or (isinstance(source, str) and not source.lstrip().startswith(("{", "[")) and Path(source).exists()):
        try:
            source = Path(source).read_text()
        except OSError as exc:
            raise ContractError("JSON_IO", str(exc)) from exc
    try:
        value = json.loads(str(source), object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except ContractError:
        raise
    except json.JSONDecodeError as exc:
        raise ContractError("JSON_INVALID", str(exc)) from exc
    if not isinstance(value, dict):
        raise ContractError("JSON_ROOT")
    return value


def _number(value, code):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(code, "finite number required")
    return float(value)


def _input_number(value, code):
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError as exc:
            raise ContractError(code, value) from exc
    return _number(value, code)


def _id(value, code="ID_INVALID"):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ContractError(code)
    return value


def _digest(value, code):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ContractError(code)
    return value


def _exact(value, keys, code):
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(code)
    return value


def _vec(value, code):
    if not isinstance(value, list) or len(value) != 3:
        raise ContractError(code)
    return [_number(v, code) for v in value]


def _sub(a, b): return [a[i] - b[i] for i in range(3)]
def _add(a, b): return [a[i] + b[i] for i in range(3)]
def _mul(a, s): return [v * s for v in a]
def _dot(a, b): return sum(a[i] * b[i] for i in range(3))
def _norm(a): return math.sqrt(_dot(a, a))
def _cross(a, b): return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _unit(value, code):
    length = _norm(value)
    if length <= 1e-12:
        raise ContractError(code)
    return _mul(value, 1 / length)


def _timestamp(value, code, *, future=False, now=None):
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        raise ContractError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ContractError("NOW_TIMEZONE")
    if future and parsed <= current:
        raise ContractError("JOB_EXPIRED")
    if not future and parsed > current:
        raise ContractError("CALIBRATION_FUTURE")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_job_spec(job: object, *, now: datetime | None = None) -> dict:
    job = _exact(job, JOB_KEYS, "JOB_KEYS")
    result = dict(job)
    if result["schema_version"] != "data_factory.job.v1": raise ContractError("JOB_SCHEMA")
    for key in ("job_id", "robot_system_id", "collection_profile_id", "place_id", "cell_calibration_id", "object_profile_id", "grasp_profile_id", "operator_or_agent_id"):
        result[key] = _id(result[key], "JOB_ID")
    if result["task"] != "pickup_e2e": raise ContractError("JOB_TASK")
    if result["grasp_profile_id"] != "top_center": raise ContractError("JOB_GRASP")
    if result["episode_intent"] != "nominal pickup": raise ContractError("JOB_INTENT")
    if result["dry_run_required"] is not True: raise ContractError("JOB_DRY_RUN")
    if result["instruction"] != "pick up the object":
        raise ContractError("JOB_TEXT")
    _digest(result["sheet_manifest_digest"], "JOB_DIGEST")
    for key in ("yaw_deg", "x_mm", "y_mm"):
        number = _number(result[key], "JOB_NUMBER")
        result[key] = int(number) if number.is_integer() else number
    result["approval_expiry"] = _timestamp(result["approval_expiry"], "JOB_EXPIRY", future=True, now=now)
    return result


def validate_home_candidate(candidate: object, *, urdf: str | Path, expected_robot_system_id: str) -> dict:
    """Validate a non-executable home-pose candidate against URDF hard limits."""
    candidate = _exact(candidate, HOME_CANDIDATE_KEYS, "HOME_KEYS")
    result = dict(candidate)
    if result["schema_version"] != "data_factory.home_candidate.v1": raise ContractError("HOME_SCHEMA")
    result["home_candidate_id"] = _id(result["home_candidate_id"], "HOME_ID")
    result["robot_system_id"] = _id(result["robot_system_id"], "HOME_ROBOT_ID")
    if result["robot_system_id"] != _id(expected_robot_system_id, "HOME_ROBOT_ID"):
        raise ContractError("HOME_ROBOT_BINDING")
    result["robot_model_name"] = _id(result["robot_model_name"], "HOME_ROBOT_ID")
    _digest(result["robot_description_digest"], "HOME_ROBOT_BINDING")
    if not result["home_candidate_id"].startswith(result["robot_system_id"] + "-home-r"):
        raise ContractError("HOME_ROBOT_BINDING")
    if result["joint_order"] != HOME_JOINT_ORDER: raise ContractError("HOME_JOINT_ORDER")
    for key in ("ui_observation_deg", "nominal_target_deg"):
        if not isinstance(result[key], list) or len(result[key]) != len(HOME_JOINT_ORDER):
            raise ContractError("HOME_JOINT_VALUES")
        result[key] = [_number(value, "HOME_JOINT_VALUES") for value in result[key]]
    if result["observation_source"] != "controller_web_ui": raise ContractError("HOME_SOURCE")
    if result["feedback_capture_status"] != "NOT_CAPTURED": raise ContractError("HOME_FEEDBACK")
    if result["qualification_status"] != "CANDIDATE": raise ContractError("HOME_QUALIFICATION")
    if result["safety_status"] != "NOT_SAFE_FOR_MOTION": raise ContractError("HOME_SAFETY")
    if result["intended_use_after_qualification"] != "SAFE_POSE_PTP": raise ContractError("HOME_INTENDED_MOTION")
    try:
        urdf_bytes = Path(urdf).read_bytes()
        root = ET.fromstring(urdf_bytes)
    except (OSError, ET.ParseError) as exc:
        raise ContractError("HOME_URDF", str(exc)) from exc
    if root.get("name") != result["robot_model_name"] or "sha256:" + hashlib.sha256(urdf_bytes).hexdigest() != result["robot_description_digest"]:
        raise ContractError("HOME_ROBOT_BINDING")
    limits = {}
    for joint in root.findall("joint"):
        name, limit = joint.get("name"), joint.find("limit")
        if name in HOME_JOINT_ORDER and limit is not None:
            try:
                limits[name] = (float(limit.attrib["lower"]), float(limit.attrib["upper"]))
            except (KeyError, ValueError) as exc:
                raise ContractError("HOME_URDF_LIMITS", name) from exc
    if set(limits) != set(HOME_JOINT_ORDER): raise ContractError("HOME_URDF_LIMITS")
    if any(not math.isfinite(value) or lower > upper for lower, upper in limits.values() for value in (lower, upper)):
        raise ContractError("HOME_URDF_LIMITS")
    for key in ("ui_observation_deg", "nominal_target_deg"):
        for name, degrees in zip(HOME_JOINT_ORDER, result[key]):
            radians = math.radians(degrees)
            lower, upper = limits[name]
            if not lower <= radians <= upper: raise ContractError("HOME_JOINT_LIMIT", name)
    return {"candidate_digest": canonical_digest(candidate), "motion_allowed": False, "nominal_target_rad": [math.radians(value) for value in result["nominal_target_deg"]]}


def _safe_profile_path(root: Path, folder: str, ident: str) -> Path:
    root = root.resolve()
    path = (root / folder / f"{_id(ident, 'PROFILE_ID')}.json")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except FileNotFoundError as exc:
        raise ContractError("PROFILE_NOT_FOUND", str(path)) from exc
    except (OSError, ValueError) as exc:
        raise ContractError("PROFILE_PATH", str(path)) from exc
    return resolved


def _profile(root, folder, ident, id_key, schema):
    value = load_json_strict(_safe_profile_path(Path(root), folder, ident))
    kind = schema.removeprefix("data_factory.").removesuffix(".v1")
    _exact(value, PROFILE_KEYS[kind], "PROFILE_SCHEMA")
    if value.get("schema_version") != schema: raise ContractError("PROFILE_SCHEMA")
    if value.get(id_key) != ident or value.get("qualification_status") != "QUALIFIED": raise ContractError("PROFILE_QUALIFICATION")
    for key, item in value.items():
        if key.endswith("_digest"):
            _digest(item, "PROFILE_DIGEST")
    return value


def _document(source, code):
    if isinstance(source, dict):
        try:
            return load_json_strict(json.dumps(source, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ContractError("JSON_NONFINITE", str(exc)) from exc
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            return load_json_strict(path)
        except ContractError as exc:
            if exc.code == "JSON_IO" or not path.exists():
                raise ContractError(code, str(path)) from exc
            raise
    raise ContractError(code)


def _family_digest(manifest):
    try:
        return family_digest_from_manifest(manifest)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("SHEET_SCHEMA", str(exc)) from exc


def _registration(sheet):
    registration = _exact(sheet["registration"], REGISTRATION_KEYS, "SHEET_REGISTRATION")
    result = {}
    for key, expected_id in (("origin", "CENTER"), ("x_ref", "X_REF"), ("verify", "Y_CHECK")):
        marker = registration[key]
        if not isinstance(marker, dict) or set(marker) != {"id", "sheet_xy_mm"} or marker["id"] != expected_id:
            raise ContractError("SHEET_REGISTRATION")
        xy = marker["sheet_xy_mm"]
        if not isinstance(xy, list) or len(xy) != 2:
            raise ContractError("SHEET_REGISTRATION")
        result[key] = [_number(xy[0], "SHEET_REGISTRATION"), _number(xy[1], "SHEET_REGISTRATION")]
    if registration["first_install"] != REGISTRATION_FIRST_INSTALL or registration["fixed_sheet_swap"] != REGISTRATION_FIXED_SHEET_SWAP:
        raise ContractError("SHEET_REGISTRATION")
    return result


def _validate_sheet(sheet):
    _exact(sheet, SHEET_KEYS, "SHEET_SCHEMA")
    if sheet["schema_version"] != "a4_place_yaw.v2":
        raise ContractError("SHEET_SCHEMA")
    _id(sheet["place_id"], "SHEET_SCHEMA")
    _id(sheet["sheet_id"], "SHEET_SCHEMA")
    _digest(sheet["a4_family_digest"], "SHEET_FAMILY_DIGEST")
    yaw = _number(sheet["yaw_deg"], "SHEET_YAW")
    page = _exact(sheet["page_mm"], {"width", "height"}, "SHEET_PAGE")
    if _number(page["width"], "SHEET_PAGE") != PAGE_W_MM or _number(page["height"], "SHEET_PAGE") != PAGE_H_MM:
        raise ContractError("SHEET_PAGE")
    spacing = _number(sheet["place_spacing_mm"], "SHEET_SPACING")
    if spacing <= 0:
        raise ContractError("SHEET_SPACING")
    if sheet["transform_contract"] != TRANSFORM_CONTRACT:
        raise ContractError("SHEET_TRANSFORM")
    print_calibration = _exact(sheet["print_calibration"], {"nominal_scale_bar_mm", "measured_scale_bar_mm", "content_scale_percent"}, "SHEET_PRINT_CALIBRATION")
    nominal = _number(print_calibration["nominal_scale_bar_mm"], "SHEET_PRINT_CALIBRATION")
    measured = _number(print_calibration["measured_scale_bar_mm"], "SHEET_PRINT_CALIBRATION")
    content_scale = _number(print_calibration["content_scale_percent"], "SHEET_PRINT_CALIBRATION")
    if nominal != 100 or measured <= 0 or content_scale <= 0 or abs(content_scale - 10000 / measured) > 0.001:
        raise ContractError("SHEET_PRINT_CALIBRATION")
    registration = _registration(sheet)
    if registration != {"origin": list(PLACE0_XY_MM), "x_ref": list(X_REF_XY_MM), "verify": list(Y_CHECK_XY_MM)}:
        raise ContractError("SHEET_REGISTRATION")
    if not isinstance(sheet["grid_points"], list) or not sheet["grid_points"]:
        raise ContractError("SHEET_GRID")
    seen = set()
    center_count = 0
    for point in sheet["grid_points"]:
        _exact(point, POINT_KEYS, "SHEET_GRID")
        point_id = _id(point["point_id"], "SHEET_GRID")
        if point_id in seen:
            raise ContractError("SHEET_GRID")
        seen.add(point_id)
        uv, sheet_xy = point["local_uv_mm"], point["sheet_xy_mm"]
        if not isinstance(uv, list) or len(uv) != 2 or not isinstance(sheet_xy, list) or len(sheet_xy) != 2:
            raise ContractError("SHEET_GRID")
        u, v = _number(uv[0], "SHEET_GRID"), _number(uv[1], "SHEET_GRID")
        _number(sheet_xy[0], "SHEET_GRID")
        _number(sheet_xy[1], "SHEET_GRID")
        relative = _exact(point["relative_pose_place0"], RELATIVE_KEYS, "SHEET_GRID")
        pose = _exact(point["job_pose"], {"place_id", "yaw_deg", "x_mm", "y_mm"}, "SHEET_GRID")
        expected_pose = {"place_id": sheet["place_id"], "yaw_deg": int(yaw) if yaw.is_integer() else yaw, "x_mm": int(u) if u.is_integer() else u, "y_mm": int(v) if v.is_integer() else v}
        if pose != expected_pose:
            raise ContractError("SHEET_GRID_POSE")
        angle = math.radians(yaw)
        expected_x = math.cos(angle) * u - math.sin(angle) * v
        expected_y = math.sin(angle) * u + math.cos(angle) * v
        if (abs(_number(relative["x_mm"], "SHEET_GRID") - expected_x) > 0.001 or
                abs(_number(relative["y_mm"], "SHEET_GRID") - expected_y) > 0.001 or
                _number(relative["yaw_deg"], "SHEET_GRID") != yaw):
            raise ContractError("SHEET_ROTATION")
        expected_sheet_x = PLACE0_XY_MM[0] + expected_x
        expected_sheet_y = PLACE0_XY_MM[1] + expected_y
        if abs(_number(sheet_xy[0], "SHEET_GRID") - expected_sheet_x) > 0.001 or abs(_number(sheet_xy[1], "SHEET_GRID") - expected_sheet_y) > 0.001:
            raise ContractError("SHEET_GRID")
        if not (PRINT_X_MARGIN_MM <= expected_sheet_x <= PAGE_W_MM - PRINT_X_MARGIN_MM and PRINT_Y_MARGIN_MM <= expected_sheet_y <= PAGE_H_MM - PRINT_Y_MARGIN_MM):
            raise ContractError("SHEET_GRID")
        if point_id == "CENTER":
            center_count += 1
            if u != 0 or v != 0:
                raise ContractError("SHEET_GRID")
    if center_count != 1:
        raise ContractError("SHEET_GRID")


def _sheet_contract(selected, yaw0, job):
    for sheet in (selected, yaw0):
        _validate_sheet(sheet)
        if sheet["a4_family_digest"] != _family_digest(sheet):
            raise ContractError("SHEET_FAMILY_DIGEST")
    if canonical_digest(selected) != job["sheet_manifest_digest"]: raise ContractError("SHEET_DIGEST")
    if selected["a4_family_digest"] != yaw0["a4_family_digest"]: raise ContractError("SHEET_FAMILY")
    if selected.get("place_id") != job["place_id"] or yaw0.get("place_id") != job["place_id"]: raise ContractError("SHEET_PLACE")
    if _number(yaw0.get("yaw_deg"), "SHEET_YAW") != 0: raise ContractError("SHEET_YAW0")
    yaw = _number(selected.get("yaw_deg"), "SHEET_YAW")
    if yaw != job["yaw_deg"]: raise ContractError("SHEET_YAW")
    _continuous_coordinate(selected, job["x_mm"], job["y_mm"])


def _continuous_coordinate(sheet, x_mm, y_mm):
    x, y = _input_number(x_mm, "JOB_BUILDER_INPUT"), _input_number(y_mm, "JOB_BUILDER_INPUT")
    u_values = [_number(point["local_uv_mm"][0], "SHEET_GRID") for point in sheet["grid_points"]]
    v_values = [_number(point["local_uv_mm"][1], "SHEET_GRID") for point in sheet["grid_points"]]
    if not (min(u_values) <= x <= max(u_values) and min(v_values) <= y <= max(v_values)):
        raise ContractError("JOB_COORDINATE_BOUNDS", str((x_mm, y_mm)))
    angle = math.radians(_number(sheet["yaw_deg"], "SHEET_YAW"))
    sheet_x = PLACE0_XY_MM[0] + math.cos(angle) * x - math.sin(angle) * y
    sheet_y = PLACE0_XY_MM[1] + math.sin(angle) * x + math.cos(angle) * y
    if not (PRINT_X_MARGIN_MM <= sheet_x <= PAGE_W_MM - PRINT_X_MARGIN_MM and PRINT_Y_MARGIN_MM <= sheet_y <= PAGE_H_MM - PRINT_Y_MARGIN_MM):
        raise ContractError("JOB_COORDINATE_BOUNDS", str((x_mm, y_mm)))
    return x, y


def _calibration(calibration, job, yaw0, robot, grasp, now):
    calibration = _exact(calibration, CALIBRATION_KEYS, "CALIBRATION_KEYS")
    limits = _exact(calibration["limits"], LIMIT_KEYS, "CALIBRATION_LIMITS")
    if calibration["schema_version"] != "data_factory.cell_calibration.v1":
        raise ContractError("CALIBRATION_ID")
    if calibration["calibration_id"] != job["cell_calibration_id"] or calibration["qualification_status"] != "QUALIFIED":
        raise ContractError("CALIBRATION_ID")
    if calibration["robot_system_id"] != job["robot_system_id"] or calibration["place_id"] != job["place_id"]:
        raise ContractError("CALIBRATION_ID")
    for key in ("yaw0_manifest_digest", "a4_family_digest", "tcp_digest", "measurement_report_digest", "table_plane_measurement_digest"):
        _digest(calibration[key], "CALIBRATION_DIGEST")
    if calibration["yaw0_manifest_digest"] != canonical_digest(yaw0) or calibration["a4_family_digest"] != yaw0["a4_family_digest"]:
        raise ContractError("CALIBRATION_SHEET")
    if calibration["tcp_digest"] != robot.get("tcp_digest"):
        raise ContractError("CALIBRATION_TCP")
    measured_print_scale = _number(calibration["print_source_scale_bar_measured_mm"], "CALIBRATION_PRINT_SCALE")
    if measured_print_scale <= 0:
        raise ContractError("CALIBRATION_PRINT_SCALE")
    manifest_scale = _number(yaw0["print_calibration"]["measured_scale_bar_mm"], "SHEET_PRINT_CALIBRATION")
    if abs(measured_print_scale - manifest_scale) > 0.001:
        raise ContractError("CALIBRATION_PRINT_SCALE")
    _timestamp(calibration["measured_at"], "CALIBRATION_TIMESTAMP", now=now)
    center, xref, ycheck, normal = (_vec(calibration[key], "CALIBRATION_VECTOR") for key in ("center_base_m", "x_ref_base_m", "y_check_base_m", "table_normal_base"))
    z = _unit(normal, "CALIBRATION_NORMAL")
    xdelta = _sub(xref, center)
    out_plane_mm = abs(_dot(xdelta, z)) * 1000
    plane_x = _sub(xdelta, _mul(z, _dot(xdelta, z)))
    x = _unit(plane_x, "CALIBRATION_X_DEGENERATE")
    y = _unit(_cross(z, x), "CALIBRATION_Y_DEGENERATE")
    registration = yaw0.get("registration")
    try:
        origin, ref, verify = (registration[key]["sheet_xy_mm"] for key in ("origin", "x_ref", "verify"))
        if registration["origin"]["id"] != "CENTER" or registration["x_ref"]["id"] != "X_REF" or registration["verify"]["id"] != "Y_CHECK": raise KeyError
        ou, ov, ru, rv, vu, vv = (_number(n, "SHEET_REGISTRATION") for n in (*origin, *ref, *verify))
    except (KeyError, TypeError, ContractError):
        raise ContractError("SHEET_REGISTRATION") from None
    nominal_x, nominal_y = ru-ou, rv-ov
    if nominal_x <= 0 or abs(nominal_y) > 1e-6: raise ContractError("SHEET_REGISTRATION")
    observed_mm = _norm(plane_x) * 1000
    expected_ycheck = _add(center, _add(_mul(x, (vu-ou)/1000), _mul(y, (vv-ov)/1000)))
    y_residual_mm = _norm(_sub(ycheck, expected_ycheck))*1000
    values = {key: _number(value, "CALIBRATION_LIMITS") for key, value in limits.items()}
    if any(v < 0 for v in values.values()) or values["min_x_ref_separation_mm"] <= 0: raise ContractError("CALIBRATION_LIMITS")
    scale_error = abs(_number(calibration["scale_bar_measured_mm"], "CALIBRATION_SCALE") - 100)
    distance_error = abs(observed_mm-nominal_x)
    if observed_mm < values["min_x_ref_separation_mm"]: raise ContractError("CALIBRATION_SEPARATION")
    if scale_error > values["max_scale_error_mm"]: raise ContractError("CALIBRATION_SCALE")
    if distance_error > values["max_x_ref_distance_error_mm"]: raise ContractError("CALIBRATION_DISTANCE")
    if out_plane_mm > values["max_x_ref_out_of_plane_mm"]: raise ContractError("CALIBRATION_OUT_OF_PLANE")
    if y_residual_mm > values["max_y_check_residual_mm"]: raise ContractError("CALIBRATION_Y_CHECK")
    margin = _number(grasp.get("grasp_margin_mm"), "GRASP_MARGIN")
    if margin <= 0: raise ContractError("GRASP_MARGIN")
    if values["combined_error_bound_mm"] > margin: raise ContractError("CALIBRATION_COMBINED_LIMIT")
    combined = scale_error + distance_error + out_plane_mm + y_residual_mm
    if combined > values["combined_error_bound_mm"] or combined > margin: raise ContractError("CALIBRATION_COMBINED_ERROR")
    return {"center": center, "x": x, "y": y, "z": z, "document": calibration}


def validate_job_spec(job, *, paths=None, data=None, config_root, now=None):
    normalized = normalize_job_spec(job, now=now)
    source = data if data is not None else paths
    if not isinstance(source, dict):
        raise ContractError("INPUT_PATHS")
    try:
        selected, yaw0 = _document(source["selected_sheet"], "INPUT_SELECTED_SHEET"), _document(source["yaw0_sheet"], "INPUT_YAW0_SHEET")
    except KeyError as exc:
        raise ContractError("INPUT_PATHS") from exc
    root = Path(config_root)
    robot = _profile(root, "robot_systems", normalized["robot_system_id"], "robot_system_id", "data_factory.robot_system.v1")
    collection = _profile(root, "collection_profiles", normalized["collection_profile_id"], "collection_profile_id", "data_factory.collection_profile.v1")
    object_profile = _profile(root, "objects", normalized["object_profile_id"], "object_profile_id", "data_factory.object_profile.v1")
    grasp = _profile(root, "grasps", normalized["grasp_profile_id"], "grasp_profile_id", "data_factory.grasp_profile.v1")
    if not isinstance(robot.get("base_frame"), str):
        raise ContractError("ROBOT_CONTRACT")
    _digest(robot.get("tcp_digest"), "ROBOT_CONTRACT")
    if grasp.get("object_profile_id") != normalized["object_profile_id"]:
        raise ContractError("GRASP_OBJECT")
    cell_path = _safe_profile_path(root, "cells", normalized["cell_calibration_id"])
    calibration = load_json_strict(cell_path)
    _sheet_contract(selected, yaw0, normalized)
    resolved_calibration = _calibration(calibration, normalized, yaw0, robot, grasp, now)
    input_digests = {"selected_sheet": canonical_digest(selected), "yaw0_sheet": canonical_digest(yaw0), "cell_calibration": canonical_digest(calibration), "robot_system": canonical_digest(robot), "collection_profile": canonical_digest(collection), "object_profile": canonical_digest(object_profile), "grasp_profile": canonical_digest(grasp)}
    resolved_job_digest = canonical_digest({"job": normalized, "input_digests": input_digests})
    return {"normalized_job": normalized, "input_digests": input_digests, "resolved_job_digest": resolved_job_digest, "robot": robot, "calibration": resolved_calibration}


def resolve_pose(validated):
    job, cal, robot = validated["normalized_job"], validated["calibration"], validated["robot"]
    angle = math.radians(job["yaw_deg"])
    x_col = _add(_mul(cal["x"], math.cos(angle)), _mul(cal["y"], math.sin(angle)))
    y_col = _add(_mul(cal["x"], -math.sin(angle)), _mul(cal["y"], math.cos(angle)))
    position = _add(cal["center"], _add(_mul(x_col, job["x_mm"] / 1000), _mul(y_col, job["y_mm"] / 1000)))
    return {"frame_id": robot["base_frame"], "position_base_m": position, "rotation_base_columns": [x_col, y_col, cal["z"]], "resolved_job_digest": validated["resolved_job_digest"], "input_digests": validated["input_digests"]}


def _rotation(columns, code):
    if not isinstance(columns, list) or len(columns) != 3:
        raise ContractError(code)
    result = [_vec(column, code) for column in columns]
    if any(abs(_dot(column, column) - 1) > 1e-9 for column in result) or any(abs(_dot(result[a], result[b])) > 1e-9 for a, b in ((0, 1), (0, 2), (1, 2))) or _norm(_sub(_cross(result[0], result[1]), result[2])) > 1e-9:
        raise ContractError(code)
    return result


def _transform(value, code):
    value = _exact(value, {"translation_m", "rotation_columns"}, code)
    return {"translation_m": _vec(value["translation_m"], code), "rotation_columns": _rotation(value["rotation_columns"], code)}


def _matvec(columns, vector):
    return [sum(columns[column][row] * vector[column] for column in range(3)) for row in range(3)]


def _compose(left, right):
    rotation = [_matvec(left["rotation_columns"], column) for column in right["rotation_columns"]]
    return {"translation_m": _add(left["translation_m"], _matvec(left["rotation_columns"], right["translation_m"])), "rotation_columns": rotation}


def _inverse(transform):
    rotation = [[transform["rotation_columns"][row][column] for row in range(3)] for column in range(3)]
    return {"translation_m": _mul(_matvec(rotation, transform["translation_m"]), -1), "rotation_columns": rotation}


def _urdf_motion_limits(urdf):
    try:
        root = ET.fromstring(Path(urdf).read_bytes())
    except (OSError, ET.ParseError) as exc:
        raise ContractError("MOTION_URDF", str(exc)) from exc
    gripper = []
    for joint in root.findall("joint"):
        limit = joint.find("limit")
        if limit is not None and ("finger" in (joint.get("name") or "") or "gripper" in (joint.get("name") or "")):
            try: gripper.append((float(limit.attrib["lower"]), float(limit.attrib["upper"])))
            except (KeyError, ValueError) as exc: raise ContractError("MOTION_URDF_LIMITS") from exc
    if not gripper or any(not math.isfinite(n) or lower > upper for lower, upper in gripper for n in (lower, upper)):
        raise ContractError("MOTION_URDF_LIMITS")
    return max(lower for lower, _ in gripper), min(upper for _, upper in gripper)


def _validate_motion_qualification(qualification, validated, home, *, urdf, now=None):
    qualification = _exact(qualification, MOTION_QUALIFICATION_KEYS, "MOTION_KEYS")
    if qualification["schema_version"] != "data_factory.motion_qualification.v1": raise ContractError("MOTION_SCHEMA")
    _id(qualification["motion_qualification_id"], "MOTION_ID")
    if qualification["qualification_status"] != "QUALIFIED": raise ContractError("MOTION_STATUS")
    job, digests = validated["normalized_job"], validated["input_digests"]
    for key in ("robot_system_id", "cell_calibration_id", "object_profile_id", "grasp_profile_id"):
        if qualification[key] != job[key]: raise ContractError("MOTION_BINDING")
    profiles = _exact(qualification["profile_digests"], MOTION_PROFILE_DIGESTS, "MOTION_DIGESTS")
    for key, expected in (("robot_system", digests["robot_system"]), ("cell_calibration", digests["cell_calibration"]), ("object_profile", digests["object_profile"]), ("grasp_profile", digests["grasp_profile"])):
        if _digest(profiles[key], "MOTION_DIGESTS") != expected: raise ContractError("MOTION_BINDING")
    if _digest(qualification["home_candidate_digest"], "MOTION_DIGESTS") != home["candidate_digest"]: raise ContractError("MOTION_HOME_BINDING")
    for key in ("robot_description_digest", "moveit_config_digest", "planning_scene_digest"):
        _digest(qualification[key], "MOTION_DIGESTS")
    if qualification["robot_description_digest"] != home["robot_description_digest"]: raise ContractError("MOTION_HOME_BINDING")
    frames = _exact(qualification["frames"], MOTION_FRAMES, "MOTION_FRAMES")
    if (frames["planning_frame"] != "base_link" or frames["planning_frame"] != validated["robot"]["base_frame"] or frames["planning_group"] != "fairino5_v6_group" or
            frames["tool_link"] != "wrist3_link" or any(not isinstance(value, str) or not value for value in frames.values())):
        raise ContractError("MOTION_FRAMES")
    transforms = {key: _transform(qualification[key], "MOTION_TRANSFORM") for key in ("tool_to_tcp", "datum_to_tcp_grasp")}
    offsets = _exact(qualification["offsets_m"], MOTION_OFFSETS, "MOTION_OFFSETS")
    offsets = {key: _number(value, "MOTION_OFFSETS") for key, value in offsets.items()}
    if not (offsets["pregrasp"] > offsets["approach_stop"] > 0 and offsets["lift"] > 0 and offsets["retreat"] > 0): raise ContractError("MOTION_OFFSETS")
    gripper = _exact(qualification["gripper_positions_m"], {"open", "closed"}, "MOTION_GRIPPER")
    gripper = {key: _number(value, "MOTION_GRIPPER") for key, value in gripper.items()}
    lower, upper = _urdf_motion_limits(urdf)
    if any(not lower <= value <= upper for value in gripper.values()) or gripper["open"] <= gripper["closed"]: raise ContractError("MOTION_GRIPPER")
    safe = qualification["qualified_safe_joint_positions_rad"]
    if not isinstance(safe, list) or len(safe) != len(HOME_JOINT_ORDER) or any(abs(_number(value, "MOTION_SAFE_JOINTS") - expected) > 1e-12 for value, expected in zip(safe, home["nominal_target_rad"])): raise ContractError("MOTION_SAFE_JOINTS")
    tolerance = _exact(qualification["goal_tolerances"], MOTION_GOAL_TOLERANCES, "MOTION_TOLERANCES")
    tolerance = {key: _number(value, "MOTION_TOLERANCES") for key, value in tolerance.items()}
    if any(value <= 0 for value in tolerance.values()): raise ContractError("MOTION_TOLERANCES")
    max_joint_state_age_s = _number(qualification["max_joint_state_age_s"], "MOTION_JOINT_STATE_AGE")
    if max_joint_state_age_s <= 0: raise ContractError("MOTION_JOINT_STATE_AGE")
    phase_limits = _exact(qualification["phase_limits"], set(MOTION_PHASES), "MOTION_PHASE_LIMITS")
    normalized_limits = {}
    for phase in MOTION_PHASES:
        limit = phase_limits[phase]
        if phase.startswith("GRIPPER"):
            limit = _exact(limit, {"command_duration_s", "execution_timeout_s", "completion_tolerance_m"}, "MOTION_PHASE_LIMITS")
            values = {key: _number(value, "MOTION_PHASE_LIMITS") for key, value in limit.items()}
        else:
            limit = _exact(limit, {"velocity_scaling", "acceleration_scaling", "planning_timeout_s", "execution_timeout_s"}, "MOTION_PHASE_LIMITS")
            values = {key: _number(value, "MOTION_PHASE_LIMITS") for key, value in limit.items()}
            if values["velocity_scaling"] > .1 or values["acceleration_scaling"] > .1: raise ContractError("MOTION_PHASE_LIMITS")
        if any(value <= 0 for value in values.values()): raise ContractError("MOTION_PHASE_LIMITS")
        normalized_limits[phase] = values
    _timestamp(qualification["qualified_at"], "MOTION_QUALIFIED_AT", now=now)
    return {"digest": canonical_digest(qualification), "frames": frames, "transforms": transforms, "offsets": offsets, "gripper": gripper, "safe": [_number(v, "MOTION_SAFE_JOINTS") for v in safe], "limits": normalized_limits, "tolerances": tolerance, "max_joint_state_age_s": max_joint_state_age_s, "pins": {key: qualification[key] for key in ("robot_description_digest", "moveit_config_digest", "planning_scene_digest")}}


def resolve_motion_program(validated, motion_qualification, home_candidate, *, urdf, expected_robot_system_id, now=None):
    """Resolve a qualification-bound, offline-only motion program; it authorizes no execution."""
    home_raw = load_json_strict(json.dumps(home_candidate, allow_nan=False)) if isinstance(home_candidate, dict) else load_json_strict(home_candidate)
    home = validate_home_candidate(home_raw, urdf=urdf, expected_robot_system_id=expected_robot_system_id)
    qualification_raw = load_json_strict(json.dumps(motion_qualification, allow_nan=False)) if isinstance(motion_qualification, dict) else load_json_strict(motion_qualification)
    q = _validate_motion_qualification(qualification_raw, validated, {**home, "robot_description_digest": home_raw["robot_description_digest"]}, urdf=urdf, now=now)
    pose = resolve_pose(validated)
    datum = {"translation_m": pose["position_base_m"], "rotation_columns": pose["rotation_base_columns"]}
    tcp = _compose(datum, q["transforms"]["datum_to_tcp_grasp"])
    tool_inverse = _inverse(q["transforms"]["tool_to_tcp"])
    def target(offset):
        shifted = {"translation_m": _add(tcp["translation_m"], _mul(datum["rotation_columns"][2], offset)), "rotation_columns": tcp["rotation_columns"]}
        return {"base_tcp": shifted, "base_tool": _compose(shifted, tool_inverse)}
    offsets = {"PREGRASP_PTP": q["offsets"]["pregrasp"], "APPROACH_STOP_LIN": q["offsets"]["approach_stop"], "FINAL_APPROACH_LIN": 0, "LIFT_LIN": q["offsets"]["lift"], "LOWER_LIN": 0, "RETREAT_LIN": q["offsets"]["retreat"]}
    steps = []
    for phase in MOTION_PHASES:
        step = {"phase": phase, "limits": q["limits"][phase]}
        if phase in offsets: step["target"] = target(offsets[phase])
        elif phase.startswith("GRIPPER"): step["gripper_position_m"] = q["gripper"]["closed" if phase == "GRIPPER_CLOSE" else "open"]
        else: step["joint_positions_rad"] = q["safe"]
        if phase == "FINAL_APPROACH_LIN": step["requires_confirmation"] = "PRECONTACT_HUMAN"
        if phase == "LIFT_LIN": step["pause_after"] = "SEMANTIC_VERDICT"
        steps.append(step)
    binding_digests = {**validated["input_digests"], **q["pins"], "motion_qualification": q["digest"], "home_candidate": home["candidate_digest"]}
    return {"schema_version": "fr5.motion_program.v1", "resolved_job_digest": validated["resolved_job_digest"], "binding_digests": binding_digests, "frames": q["frames"], "planning": {"pipeline_id": "pilz_industrial_motion_planner", "ptp_planner_id": "PTP", "lin_planner_id": "LIN", "goal_tolerances": q["tolerances"], "max_joint_state_age_s": q["max_joint_state_age_s"]}, "steps": steps}


def build_job_spec(selected_sheet, *, point_id=None, x_mm=None, y_mm=None, job_id, robot_system_id, collection_profile_id, cell_calibration_id, object_profile_id, grasp_profile_id, operator_or_agent_id, approval_expiry, now=None):
    """Build the fixed pickup JobSpec from an A4 point or bounded coordinate."""
    sheet = _document(selected_sheet, "INPUT_SELECTED_SHEET")
    _validate_sheet(sheet)
    has_xy = x_mm is not None or y_mm is not None
    if (x_mm is None) != (y_mm is None) or (point_id is not None and has_xy):
        raise ContractError("JOB_BUILDER_INPUT", "use point_id or an x_mm/y_mm pair")
    if point_id is None and not has_xy:
        raise ContractError("CLI_INPUT_REQUIRED", "point")
    if point_id is not None:
        matches = [point for point in sheet["grid_points"] if point["point_id"] == point_id]
        if len(matches) != 1:
            raise ContractError("JOB_POINT", str(point_id))
        pose = matches[0]["job_pose"]
    else:
        x_value, y_value = _continuous_coordinate(sheet, x_mm, y_mm)
        pose = {"place_id": sheet["place_id"], "yaw_deg": sheet["yaw_deg"], "x_mm": x_value, "y_mm": y_value}
    return normalize_job_spec({
        "schema_version": "data_factory.job.v1",
        "job_id": job_id,
        "task": "pickup_e2e",
        "robot_system_id": robot_system_id,
        "collection_profile_id": collection_profile_id,
        "place_id": pose["place_id"],
        "cell_calibration_id": cell_calibration_id,
        "sheet_manifest_digest": canonical_digest(sheet),
        "yaw_deg": pose["yaw_deg"],
        "x_mm": pose["x_mm"],
        "y_mm": pose["y_mm"],
        "object_profile_id": object_profile_id,
        "grasp_profile_id": grasp_profile_id,
        "instruction": "pick up the object",
        "episode_intent": "nominal pickup",
        "operator_or_agent_id": operator_or_agent_id,
        "approval_expiry": approval_expiry,
        "dry_run_required": True,
    }, now=now)


def _prompt(label):
    print(f"{label}: ", end="", file=sys.stderr, flush=True)
    value = sys.stdin.readline()
    if not value or not value.strip():
        raise ContractError("CLI_INPUT_REQUIRED", label)
    return value.strip()


def _required(value, label, interactive):
    if value is not None:
        return value
    if not interactive:
        raise ContractError("CLI_INPUT_REQUIRED", label)
    return _prompt(label)


def _select_id_or_number(choice, options):
    if choice in options:
        return choice
    if choice.isdecimal() and 1 <= int(choice) <= len(options):
        return options[int(choice) - 1]
    return None


def _profile_choice(value, *, label, root, folder, interactive):
    if value is not None:
        return value
    if not interactive:
        raise ContractError("CLI_INPUT_REQUIRED", label)
    directory = Path(root) / folder
    options = sorted(path.stem for path in directory.glob("*.json") if SAFE_ID.fullmatch(path.stem)) if directory.is_dir() else []
    if not options:
        raise ContractError("PROFILE_NOT_FOUND", folder)
    if len(options) == 1:
        return options[0]
    print(f"{label} (number or exact ID)", file=sys.stderr)
    for index, option in enumerate(options, 1):
        print(f"  {index}) {option}", file=sys.stderr)
    choice = _prompt(label)
    selected = _select_id_or_number(choice, options)
    if selected is not None:
        return selected
    raise ContractError("CLI_SELECTION", choice)


def _interactive_point(sheet):
    print(f"sheet={sheet['sheet_id']} place={sheet['place_id']} yaw={sheet['yaw_deg']}", file=sys.stderr)
    for index, point in enumerate(sheet["grid_points"], 1):
        pose = point["job_pose"]
        print(f"  {index}) {point['point_id']} ({pose['x_mm']},{pose['y_mm']})", file=sys.stderr)
    choice = _prompt("point (number, exact ID, or x,y)")
    point_id = _select_id_or_number(choice, [point["point_id"] for point in sheet["grid_points"]])
    if point_id is not None:
        return point_id, None, None
    if "," not in choice:
        return choice, None, None
    parts = [part.strip() for part in choice.split(",")]
    if len(parts) != 2:
        raise ContractError("JOB_POINT", choice)
    try:
        return None, float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ContractError("JOB_POINT", choice) from exc


def _cli():
    parser = ContractArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate-job", "resolve-pose"):
        command = sub.add_parser(name)
        command.add_argument("--job", required=True)
        command.add_argument("--selected-sheet", required=True)
        command.add_argument("--yaw0-sheet", required=True)
        command.add_argument("--config-root", required=True)
    motion = sub.add_parser("resolve-motion")
    for name in ("job", "selected-sheet", "yaw0-sheet", "config-root", "motion-qualification", "home-candidate", "urdf", "expected-robot-system-id"):
        motion.add_argument(f"--{name}", required=True)
    home = sub.add_parser("validate-home-candidate")
    home.add_argument("--candidate", required=True)
    home.add_argument("--urdf", required=True)
    home.add_argument("--expected-robot-system-id", required=True)
    builder = sub.add_parser("build-job")
    builder.add_argument("--selected-sheet", required=True)
    builder.add_argument("--yaw0-sheet", required=True)
    builder.add_argument("--config-root", required=True)
    builder.add_argument("--interactive", action="store_true")
    builder.add_argument("--point-id")
    builder.add_argument("--x-mm")
    builder.add_argument("--y-mm")
    for name in ("job-id", "robot-system-id", "collection-profile-id", "cell-calibration-id", "object-profile-id", "grasp-profile-id", "operator-or-agent-id", "approval-expiry"):
        builder.add_argument(f"--{name}")
    try:
        args = parser.parse_args()
        if args.command == "validate-home-candidate":
            text = sys.stdin.read() if args.candidate == "-" else Path(args.candidate).read_text()
            print(json.dumps(validate_home_candidate(load_json_strict(text), urdf=args.urdf, expected_robot_system_id=args.expected_robot_system_id), sort_keys=True, separators=(",", ":"), allow_nan=False)); return 0
        if args.command == "build-job":
            selected = _document(args.selected_sheet, "INPUT_SELECTED_SHEET")
            _validate_sheet(selected)
            point_id, x_mm, y_mm = args.point_id, args.x_mm, args.y_mm
            if point_id is None and x_mm is None and y_mm is None:
                if not args.interactive:
                    raise ContractError("CLI_INPUT_REQUIRED", "point")
                point_id, x_mm, y_mm = _interactive_point(selected)
            root = Path(args.config_root)
            job = build_job_spec(
                selected,
                point_id=point_id,
                x_mm=x_mm,
                y_mm=y_mm,
                job_id=_required(args.job_id, "job_id", args.interactive),
                robot_system_id=_profile_choice(args.robot_system_id, label="robot_system_id", root=root, folder="robot_systems", interactive=args.interactive),
                collection_profile_id=_profile_choice(args.collection_profile_id, label="collection_profile_id", root=root, folder="collection_profiles", interactive=args.interactive),
                cell_calibration_id=_profile_choice(args.cell_calibration_id, label="cell_calibration_id", root=root, folder="cells", interactive=args.interactive),
                object_profile_id=_profile_choice(args.object_profile_id, label="object_profile_id", root=root, folder="objects", interactive=args.interactive),
                grasp_profile_id=_profile_choice(args.grasp_profile_id, label="grasp_profile_id", root=root, folder="grasps", interactive=args.interactive),
                operator_or_agent_id=_required(args.operator_or_agent_id, "operator_or_agent_id", args.interactive),
                approval_expiry=_required(args.approval_expiry, "approval_expiry", args.interactive),
            )
            yaw0 = _document(args.yaw0_sheet, "INPUT_YAW0_SHEET")
            validated = validate_job_spec(job, data={"selected_sheet": selected, "yaw0_sheet": yaw0}, config_root=args.config_root)
            print(json.dumps(validated["normalized_job"], sort_keys=True, separators=(",", ":"), allow_nan=False))
            return 0
        text = sys.stdin.read() if args.job == "-" else Path(args.job).read_text()
        job = load_json_strict(text)
        validated = validate_job_spec(
            job,
            paths={"selected_sheet": args.selected_sheet, "yaw0_sheet": args.yaw0_sheet},
            config_root=args.config_root,
        )
        if args.command == "resolve-motion":
            qualification = load_json_strict(sys.stdin.read() if args.motion_qualification == "-" else Path(args.motion_qualification).read_text())
            candidate = load_json_strict(sys.stdin.read() if args.home_candidate == "-" else Path(args.home_candidate).read_text())
            print(json.dumps(resolve_motion_program(validated, qualification, candidate, urdf=args.urdf, expected_robot_system_id=args.expected_robot_system_id), sort_keys=True, separators=(",", ":"), allow_nan=False)); return 0
        output = resolve_pose(validated) if args.command == "resolve-pose" else {"normalized_job": validated["normalized_job"], "input_digests": validated["input_digests"], "resolved_job_digest": validated["resolved_job_digest"]}
        print(json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False)); return 0
    except (ContractError, OSError, UnicodeError) as exc:
        code = exc.code if isinstance(exc, ContractError) else "JSON_IO" if isinstance(exc, UnicodeError) else "JOB_IO"
        print(json.dumps({"error": {"code": code, "message": str(exc)}}, sort_keys=True), file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
