#!/usr/bin/env python3
"""Fail-closed offline JobSpec validator and A4 datum-frame resolver."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
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


class ContractError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


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
    matches = 0
    for point in selected["grid_points"]:
        if point["job_pose"] == {"place_id": job["place_id"], "yaw_deg": job["yaw_deg"], "x_mm": job["x_mm"], "y_mm": job["y_mm"]}:
            matches += 1
    if matches != 1: raise ContractError("SHEET_GRID_MATCH")


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


def _cli():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate-job", "resolve-pose"):
        command = sub.add_parser(name)
        command.add_argument("--job", required=True)
        command.add_argument("--selected-sheet", required=True)
        command.add_argument("--yaw0-sheet", required=True)
        command.add_argument("--config-root", required=True)
    args = parser.parse_args()
    try:
        text = sys.stdin.read() if args.job == "-" else Path(args.job).read_text()
        job = load_json_strict(text)
        validated = validate_job_spec(
            job,
            paths={"selected_sheet": args.selected_sheet, "yaw0_sheet": args.yaw0_sheet},
            config_root=args.config_root,
        )
        output = resolve_pose(validated) if args.command == "resolve-pose" else {"normalized_job": validated["normalized_job"], "input_digests": validated["input_digests"], "resolved_job_digest": validated["resolved_job_digest"]}
        print(json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False)); return 0
    except (ContractError, OSError) as exc:
        code = exc.code if isinstance(exc, ContractError) else "JOB_IO"
        print(json.dumps({"error": {"code": code, "message": str(exc)}}, sort_keys=True), file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
