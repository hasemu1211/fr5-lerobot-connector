#!/usr/bin/env python3
"""Fail-closed offline JobSpec validator and A4 datum-frame resolver."""
from __future__ import annotations

import argparse
import copy
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

try:
    from tools.data_factory.workspace_geometry import (
        rotate_xy,
        rotation_envelope,
        safe_rectangle_bounds,
    )
except ImportError:
    from data_factory.workspace_geometry import (
        rotate_xy,
        rotation_envelope,
        safe_rectangle_bounds,
    )

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
JOB_KEYS = {"schema_version", "job_id", "task", "robot_system_id", "collection_profile_id", "place_id", "cell_calibration_id", "sheet_manifest_digest", "yaw_deg", "x_mm", "y_mm", "object_profile_id", "grasp_profile_id", "instruction", "episode_intent", "operator_or_agent_id", "approval_expiry", "dry_run_required"}
TASK_CONTRACTS = {
    "pickup_e2e": {
        "episode_intent": "nominal pickup",
        "instruction_template": "pick up the {object_description}",
        "recording_boundary": "LIFT_LIN",
        "review_checklist_id": "pickup-v2",
    },
    "pick_place": {
        "episode_intent": "nominal pick and place",
        "instruction_template": (
            "pick up the {object_description} and place it at the destination"
        ),
        "recording_boundary": "RETREAT_LIN",
        "review_checklist_id": "pick-place-v1",
    },
}
TASK_REVIEW_CHECKLIST_IDS = frozenset(
    contract["review_checklist_id"] for contract in TASK_CONTRACTS.values()
)
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
    "object_profile": {"schema_version", "object_profile_id", "qualification_status", "description", "dimensions_mm", "datum"},
    "grasp_profile": {"schema_version", "grasp_profile_id", "qualification_status", "object_profile_id", "grasp_kind", "gripper_close"},
}
GRASP_PROFILE_V3_KEYS = PROFILE_KEYS["grasp_profile"] | {
    "object_profile_digest", "grasp_geometry", "gripper_open",
}
GRASP_GEOMETRY_KEYS = {
    "contact_surface", "depth_from_top_mm", "release_clearance_mm",
    "datum_to_tcp_grasp",
}
GRIPPER_OPEN_KEYS = {
    "command_position_m", "velocity_percent", "force_percent",
    "completion_tolerance_m", "evidence_digest",
}
COLLECTION_PROFILE_V2_KEYS = PROFILE_KEYS["collection_profile"] | {
    "camera_profile", "camera_roles", "camera_serials", "camera_topics",
    "fps", "width", "height", "image_qos", "image_qos_depth",
    "writer_queue_size", "encoder_threads", "encoding_mode", "repo_id",
    "encoder_temp_policy", "dataset_incremental_peak_bytes",
    "encoder_temp_peak_bytes", "disk_reserve_bytes", "portability_status",
}
HOME_CANDIDATE_KEYS = {"schema_version", "home_candidate_id", "robot_system_id", "robot_model_name", "robot_description_digest", "joint_order", "ui_observation_deg", "nominal_target_deg", "observation_source", "feedback_capture_status", "qualification_status", "safety_status", "intended_use_after_qualification"}
HOME_JOINT_ORDER = ["j1", "j2", "j3", "j4", "j5", "j6"]
MOTION_PHASES = ("PREGRASP_PTP", "APPROACH_STOP_LIN", "FINAL_APPROACH_LIN", "GRIPPER_CLOSE", "LIFT_LIN", "RECYCLE_APPROACH_PTP", "LOWER_LIN", "GRIPPER_OPEN", "RETREAT_LIN", "SAFE_POSE_PTP")
MOTION_QUALIFICATION_KEYS = {"schema_version", "motion_qualification_id", "qualification_status", "robot_system_id", "cell_calibration_id", "object_profile_id", "grasp_profile_id", "profile_digests", "home_candidate_digest", "robot_description_digest", "moveit_config_digest", "planning_scene_digest", "planning_scene", "frames", "tool_to_tcp", "datum_to_tcp_grasp", "offsets_m", "gripper_positions_m", "qualified_safe_joint_positions_rad", "goal_tolerances", "max_joint_state_age_s", "execution_timeouts_s", "phase_limits", "qualified_at"}
MOTION_QUALIFICATION_V2_KEYS = MOTION_QUALIFICATION_KEYS | {
    "planning_scene_profile_id", "planning_scene_profile_digest",
}
MOTION_QUALIFICATION_KEYS_BY_SCHEMA = {
    "data_factory.motion_qualification.v1": MOTION_QUALIFICATION_KEYS,
    "data_factory.motion_qualification.v2": MOTION_QUALIFICATION_V2_KEYS,
}
MOTION_QUALIFICATION_SCHEMAS = frozenset(
    MOTION_QUALIFICATION_KEYS_BY_SCHEMA
)
MOTION_PROFILE_DIGESTS = {"robot_system", "cell_calibration", "object_profile", "grasp_profile"}
MOTION_FRAMES = {"planning_frame", "planning_group", "tool_link"}
MOTION_OFFSETS = {"pregrasp", "approach_stop", "lift", "retreat"}
MOTION_GOAL_TOLERANCES = {"position_m", "orientation_rad", "joint_rad"}
MOTION_EXECUTION_TIMEOUTS = {"heartbeat_lease", "cancel", "precontact_confirmation", "grasp_verdict", "semantic_verdict"}
PLANNING_SCENE_KEYS = {"frame_id", "floor", "wall"}
PLANNING_SCENE_FLOOR_KEYS = {"id", "dimensions_m", "surface_z_m", "source"}
PLANNING_SCENE_WALL_KEYS = {"id", "dimensions_m", "near_face_y_m", "wall_side", "home_arm_protrusion_base_xy", "j1_home_deg"}
PLANNING_SCENE_PROFILE_KEYS = {
    "schema_version", "planning_scene_profile_id", "qualification_status",
    "robot_system_id", "frame_id", "floor", "wall",
}
PLANNING_SCENE_PROFILE_FLOOR_KEYS = {
    "id", "dimensions_m", "measured_surface_z_m", "collision_margin_m",
    "workspace_datum_tolerance_m", "source_measurement_digest", "source",
}


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


def task_instruction(
    task_id: str, object_description: str, *,
    source_region_id: str | None = None,
    destination_region_id: str | None = None,
    region_binding_verified: bool = False,
) -> str:
    """Return the one collection-time language label for a supported task."""
    contract = TASK_CONTRACTS.get(task_id)
    if contract is None:
        raise ContractError("JOB_TASK")
    if (
        not isinstance(object_description, str)
        or not object_description
        or object_description.strip() != object_description
        or not object_description.isprintable()
    ):
        raise ContractError("OBJECT_DESCRIPTION")
    if (
        type(region_binding_verified) is not bool
        or (source_region_id is None) != (destination_region_id is None)
        or region_binding_verified and source_region_id is None
    ):
        raise ContractError("TASK_REGION_BINDING")
    if source_region_id is not None and (
        task_id != "pick_place"
        or {source_region_id, destination_region_id} != {"RED", "BLUE"}
        or source_region_id == destination_region_id
    ):
        raise ContractError("TASK_REGION_BINDING")
    if source_region_id is not None and region_binding_verified:
        return (
            f"pick up the {object_description} from the "
            f"{source_region_id.lower()} zone and place it in the "
            f"{destination_region_id.lower()} zone"
        )
    return contract["instruction_template"].format(
        object_description=object_description,
    )


def task_review_checklist_id(task_id: str) -> str:
    contract = TASK_CONTRACTS.get(task_id)
    if contract is None:
        raise ContractError("JOB_TASK")
    return contract["review_checklist_id"]


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
    task_contract = TASK_CONTRACTS.get(result["task"])
    if task_contract is None: raise ContractError("JOB_TASK")
    if result["episode_intent"] != task_contract["episode_intent"]: raise ContractError("JOB_INTENT")
    if result["dry_run_required"] is not True: raise ContractError("JOB_DRY_RUN")
    if (
        not isinstance(result["instruction"], str)
        or not 1 <= len(result["instruction"]) <= 120
        or result["instruction"].strip() != result["instruction"]
        or not result["instruction"].isprintable()
    ):
        raise ContractError("JOB_TEXT")
    _digest(result["sheet_manifest_digest"], "JOB_DIGEST")
    for key in ("yaw_deg", "x_mm", "y_mm"):
        number = _number(result[key], "JOB_NUMBER")
        if key == "yaw_deg":
            number = normalize_yaw_deg(number)
        result[key] = int(number) if number.is_integer() else number
    result["approval_expiry"] = _timestamp(result["approval_expiry"], "JOB_EXPIRY", future=True, now=now)
    return result


def normalize_yaw_deg(value: object) -> float:
    """Return one signed representative for a circular yaw angle."""
    normalized = (_number(value, "SHEET_YAW") + 180.0) % 360.0 - 180.0
    return 0.0 if normalized == 0.0 else normalized


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
    kind = schema.removeprefix("data_factory.").rsplit(".v", 1)[0]
    if kind == "collection_profile" and value.get("schema_version") == "data_factory.collection_profile.v2":
        _exact(value, COLLECTION_PROFILE_V2_KEYS, "PROFILE_SCHEMA")
    elif kind == "grasp_profile" and value.get("schema_version") == "data_factory.grasp_profile.v3":
        _exact(value, GRASP_PROFILE_V3_KEYS, "PROFILE_SCHEMA")
    else:
        _exact(value, PROFILE_KEYS[kind], "PROFILE_SCHEMA")
        if value.get("schema_version") != schema: raise ContractError("PROFILE_SCHEMA")
    if value.get(id_key) != ident or value.get("qualification_status") != "QUALIFIED": raise ContractError("PROFILE_QUALIFICATION")
    if kind == "object_profile":
        if value["datum"] != "center": raise ContractError("OBJECT_DATUM")
        description = value["description"]
        if not isinstance(description, str) or not 1 <= len(description) <= 80 or description.strip() != description or not description.isprintable():
            raise ContractError("OBJECT_DESCRIPTION")
        dimensions = value["dimensions_mm"]
        if not isinstance(dimensions, list) or len(dimensions) != 3 or any(_number(item, "OBJECT_DIMENSIONS") <= 0 for item in dimensions):
            raise ContractError("OBJECT_DIMENSIONS")
    elif kind == "collection_profile":
        if value["schema_version"] == "data_factory.collection_profile.v1":
            return value
        camera_roles = {"up": ["up"], "up-side": ["up", "side"], "up-wrist": ["up", "wrist"]}
        if value["camera_profile"] not in camera_roles or value["camera_roles"] != camera_roles[value["camera_profile"]]:
            raise ContractError("COLLECTION_PROFILE")
        roles = set(value["camera_roles"])
        if set(value["camera_serials"]) != roles or set(value["camera_topics"]) != roles:
            raise ContractError("COLLECTION_PROFILE")
        if any(not isinstance(item, str) or not item or "\x00" in item for item in value["camera_serials"].values()):
            raise ContractError("COLLECTION_PROFILE")
        if any(not isinstance(item, str) or not item.startswith("/") or any(char.isspace() for char in item) for item in value["camera_topics"].values()):
            raise ContractError("COLLECTION_PROFILE")
        for key in ("fps", "width", "height", "image_qos_depth", "writer_queue_size"):
            if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] <= 0:
                raise ContractError("COLLECTION_PROFILE")
        if value["fps"] != 30:
            raise ContractError("COLLECTION_PROFILE")
        for key in ("dataset_incremental_peak_bytes", "encoder_temp_peak_bytes", "disk_reserve_bytes"):
            if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0:
                raise ContractError("COLLECTION_PROFILE")
        if (
            isinstance(value["encoder_threads"], bool) or not isinstance(value["encoder_threads"], int)
            or value["encoder_threads"] < 0 or value["encoding_mode"] != "batch"
            or value["image_qos"] not in {"reliable", "best-effort"}
            or value["encoder_temp_policy"] != "DATASET_LOCAL"
            or value["portability_status"] not in {"QUALIFICATION_REQUIRED", "SUPPORTED_8GB"}
            or not isinstance(value["repo_id"], str) or not value["repo_id"] or "\x00" in value["repo_id"]
        ):
            raise ContractError("COLLECTION_PROFILE")
    elif kind == "grasp_profile":
        if value["grasp_kind"] != "top_center": raise ContractError("GRASP_KIND")
        value = {**value, "gripper_close": _gripper_close(value["gripper_close"], "GRASP_CLOSE")}
        if value["schema_version"] == "data_factory.grasp_profile.v3":
            geometry = _exact(
                value["grasp_geometry"], GRASP_GEOMETRY_KEYS,
                "GRASP_GEOMETRY",
            )
            if geometry["contact_surface"] != "TOP":
                raise ContractError("GRASP_GEOMETRY")
            depth = _number(
                geometry["depth_from_top_mm"], "GRASP_GEOMETRY",
            )
            release_clearance = _number(
                geometry["release_clearance_mm"], "GRASP_GEOMETRY",
            )
            if depth <= 0 or release_clearance < 0:
                raise ContractError("GRASP_GEOMETRY")
            transform = validate_rigid_transform(
                geometry["datum_to_tcp_grasp"], "GRASP_GEOMETRY",
            )
            expected_rotation = [
                [1.0, 0.0, 0.0],
                [0.0, -1.0, 0.0],
                [0.0, 0.0, -1.0],
            ]
            if any(
                abs(transform["rotation_columns"][column][row] - expected_rotation[column][row]) > 1e-9
                for column in range(3) for row in range(3)
            ):
                raise ContractError("GRASP_GEOMETRY")
            value = {
                **value,
                "object_profile_digest": _digest(
                    value["object_profile_digest"], "GRASP_OBJECT",
                ),
                "grasp_geometry": {
                    "contact_surface": "TOP",
                    "depth_from_top_mm": depth,
                    "release_clearance_mm": release_clearance,
                    "datum_to_tcp_grasp": transform,
                },
                "gripper_open": _gripper_open(
                    value["gripper_open"], "GRASP_OPEN",
                ),
            }
    else:
        for key, item in value.items():
            if key.endswith("_digest"):
                _digest(item, "PROFILE_DIGEST")
    return value


def _gripper_close(value, code):
    value = _exact(value, {"command_position_m", "acceptable_feedback_m", "velocity_percent", "force_percent", "evidence_digest"}, code)
    feedback = _exact(value["acceptable_feedback_m"], {"min", "max"}, code)
    command = _number(value["command_position_m"], code)
    minimum, maximum = (_number(feedback[key], code) for key in ("min", "max"))
    if any(item <= 0 for item in (command, minimum, maximum)) or not command <= minimum <= maximum:
        raise ContractError(code)
    for key in ("velocity_percent", "force_percent"):
        if isinstance(value[key], bool) or not isinstance(value[key], int) or not 1 <= value[key] <= 100:
            raise ContractError(code)
    _digest(value["evidence_digest"], code)
    return {"command_position_m": command, "acceptable_feedback_m": {"min": minimum, "max": maximum}, "velocity_percent": value["velocity_percent"], "force_percent": value["force_percent"], "evidence_digest": value["evidence_digest"]}


def _gripper_open(value, code):
    value = _exact(value, GRIPPER_OPEN_KEYS, code)
    command = _number(value["command_position_m"], code)
    tolerance = _number(value["completion_tolerance_m"], code)
    if command <= 0 or tolerance <= 0:
        raise ContractError(code)
    for key in ("velocity_percent", "force_percent"):
        if (
            isinstance(value[key], bool) or not isinstance(value[key], int)
            or not 1 <= value[key] <= 100
        ):
            raise ContractError(code)
    _digest(value["evidence_digest"], code)
    return {
        "command_position_m": command,
        "velocity_percent": value["velocity_percent"],
        "force_percent": value["force_percent"],
        "completion_tolerance_m": tolerance,
        "evidence_digest": value["evidence_digest"],
    }


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


def _sheet_contract(selected, yaw0, job, object_profile, calibration):
    for sheet in (selected, yaw0):
        validate_sheet_manifest(sheet)
    if canonical_digest(selected) != job["sheet_manifest_digest"]: raise ContractError("SHEET_DIGEST")
    if selected["a4_family_digest"] != yaw0["a4_family_digest"]: raise ContractError("SHEET_FAMILY")
    if selected.get("place_id") != job["place_id"] or yaw0.get("place_id") != job["place_id"]: raise ContractError("SHEET_PLACE")
    if _number(yaw0.get("yaw_deg"), "SHEET_YAW") != 0: raise ContractError("SHEET_YAW0")
    bounded_place_coordinate(
        selected, job["x_mm"], job["y_mm"], yaw_deg=job["yaw_deg"],
        object_dimensions_mm=object_profile["dimensions_mm"],
        uncertainty_mm=calibration["limits"]["combined_error_bound_mm"],
    )


def bounded_a4_coordinate(
    *, x_bounds, y_bounds, yaw_deg, x_mm, y_mm,
    object_size_xy_mm=(0.0, 0.0), uncertainty_mm=0.0,
    page_size_mm=(PAGE_W_MM, PAGE_H_MM), origin_xy_mm=PLACE0_XY_MM,
    base_margin_xy_mm=(PRINT_X_MARGIN_MM, PRINT_Y_MARGIN_MM),
):
    """Validate one continuous pose against the shared A4 printable domain."""
    x, y = _input_number(x_mm, "JOB_BUILDER_INPUT"), _input_number(y_mm, "JOB_BUILDER_INPUT")
    try:
        x_min = _input_number(x_bounds["minimum"], "JOB_BUILDER_INPUT")
        x_max = _input_number(x_bounds["maximum"], "JOB_BUILDER_INPUT")
        y_min = _input_number(y_bounds["minimum"], "JOB_BUILDER_INPUT")
        y_max = _input_number(y_bounds["maximum"], "JOB_BUILDER_INPUT")
    except (KeyError, TypeError) as exc:
        raise ContractError("JOB_BUILDER_INPUT") from exc
    if x_min > x_max or y_min > y_max or not (x_min <= x <= x_max and y_min <= y <= y_max):
        raise ContractError("JOB_COORDINATE_BOUNDS", str((x_mm, y_mm)))
    yaw = _input_number(yaw_deg, "SHEET_YAW")
    try:
        safe_x, safe_y = safe_rectangle_bounds(
            page_size_mm=page_size_mm, origin_xy_mm=origin_xy_mm,
            base_margin_xy_mm=base_margin_xy_mm,
            object_size_xy_mm=object_size_xy_mm,
            uncertainty_mm=uncertainty_mm, yaw_deg=yaw,
        )
        sheet_x, sheet_y = rotate_xy((x, y), yaw)
    except ValueError as exc:
        raise ContractError("JOB_BUILDER_INPUT") from exc
    if not (safe_x[0] <= sheet_x <= safe_x[1] and safe_y[0] <= sheet_y <= safe_y[1]):
        raise ContractError("JOB_COORDINATE_BOUNDS", str((x_mm, y_mm)))
    return x, y


def bounded_place_coordinate(
    sheet, x_mm, y_mm, *, yaw_deg=None,
    object_dimensions_mm=None, uncertainty_mm=0.0,
):
    """Validate one continuous local coordinate inside a registered A4 domain."""
    if object_dimensions_mm is None:
        u_values = [_number(point["local_uv_mm"][0], "SHEET_GRID") for point in sheet["grid_points"]]
        v_values = [_number(point["local_uv_mm"][1], "SHEET_GRID") for point in sheet["grid_points"]]
        x_bounds, y_bounds = (
            (min(u_values), max(u_values)), (min(v_values), max(v_values)),
        )
        object_size_xy_mm = (0.0, 0.0)
    else:
        try:
            object_size_xy_mm = tuple(object_dimensions_mm[:2])
            printable = safe_rectangle_bounds(
                page_size_mm=(PAGE_W_MM, PAGE_H_MM),
                origin_xy_mm=PLACE0_XY_MM,
                base_margin_xy_mm=(PRINT_X_MARGIN_MM, PRINT_Y_MARGIN_MM),
                object_size_xy_mm=(0.0, 0.0), uncertainty_mm=0.0,
                yaw_deg=0.0,
            )
            x_bounds, y_bounds = rotation_envelope(*printable)
        except (TypeError, ValueError) as exc:
            raise ContractError("JOB_BUILDER_INPUT") from exc
    return bounded_a4_coordinate(
        x_bounds={"minimum": x_bounds[0], "maximum": x_bounds[1]},
        y_bounds={"minimum": y_bounds[0], "maximum": y_bounds[1]},
        yaw_deg=sheet["yaw_deg"] if yaw_deg is None else yaw_deg,
        x_mm=x_mm, y_mm=y_mm,
        object_size_xy_mm=object_size_xy_mm,
        uncertainty_mm=uncertainty_mm,
    )


def fit_place_calibration(center_base_m, x_ref_base_m, table_normal_base, registration, scale_bar_mm, y_check_base_m=None):
    """Fit the CENTER/X_REF datum axes and independently assess optional Y_CHECK."""
    center, xref, normal = (_vec(value, "CALIBRATION_VECTOR") for value in (center_base_m, x_ref_base_m, table_normal_base))
    z = _unit(normal, "CALIBRATION_NORMAL")
    try:
        origin, ref, verify = (registration[key]["sheet_xy_mm"] for key in ("origin", "x_ref", "verify"))
        if registration["origin"]["id"] != "CENTER" or registration["x_ref"]["id"] != "X_REF" or registration["verify"]["id"] != "Y_CHECK": raise KeyError
        ou, ov, ru, rv, vu, vv = (_number(n, "SHEET_REGISTRATION") for n in (*origin, *ref, *verify))
    except (KeyError, TypeError, ContractError):
        raise ContractError("SHEET_REGISTRATION") from None
    nominal_x = ru - ou
    if nominal_x <= 0 or abs(rv - ov) > 1e-6: raise ContractError("SHEET_REGISTRATION")
    delta = _sub(xref, center); out_plane_mm = abs(_dot(delta, z)) * 1000
    plane_x = _sub(delta, _mul(z, _dot(delta, z))); x = _unit(plane_x, "CALIBRATION_X_DEGENERATE"); y = _unit(_cross(z, x), "CALIBRATION_Y_DEGENERATE")
    metrics = {"xref_observed_mm": _norm(plane_x) * 1000, "xref_distance_error_mm": abs(_norm(plane_x) * 1000 - nominal_x), "xref_out_of_plane_mm": out_plane_mm, "scale_error_mm": abs(_number(scale_bar_mm, "CALIBRATION_SCALE") - 100)}
    ycheck = None
    if y_check_base_m is not None:
        ycheck = _vec(y_check_base_m, "CALIBRATION_VECTOR")
        expected = _add(center, _add(_mul(x, (vu - ou) / 1000), _mul(y, (vv - ov) / 1000)))
        metrics["y_check_residual_mm"] = _norm(_sub(ycheck, expected)) * 1000
    return {"center": center, "x_ref": xref, "y_check": ycheck, "x": x, "y": y, "z": z, "nominal_x_ref_mm": nominal_x, "metrics": metrics}


def validate_sheet_manifest(sheet):
    """Validate one canonical A4 v2 sheet and its recomputed family digest."""
    _validate_sheet(sheet)
    if sheet["a4_family_digest"] != _family_digest(sheet):
        raise ContractError("SHEET_FAMILY_DIGEST")
    return sheet


def validate_yaw0_sheet(sheet):
    """Validate the canonical A4 v2 yaw-zero sheet used for place calibration."""
    validate_sheet_manifest(sheet)
    if _number(sheet["yaw_deg"], "SHEET_YAW") != 0:
        raise ContractError("SHEET_YAW0")
    return sheet


def resolve_place_pose(center_base_m, x_axis_base, y_axis_base, normal_base, yaw_deg, x_mm, y_mm):
    """Resolve a local place coordinate into base_link without authorizing motion."""
    center = _vec(center_base_m, "PLACE_CALIBRATION_VECTOR")
    x_axis, y_axis, normal = (_unit(value, "PLACE_CALIBRATION_AXIS") for value in (x_axis_base, y_axis_base, normal_base))
    if abs(_dot(x_axis, y_axis)) > 1e-9 or abs(_dot(x_axis, normal)) > 1e-9 or abs(_dot(y_axis, normal)) > 1e-9 or _norm(_sub(_cross(x_axis, y_axis), normal)) > 1e-9:
        raise ContractError("PLACE_CALIBRATION_AXIS")
    angle = math.radians(_input_number(yaw_deg, "PLACE_COORDINATE"))
    x_col = _add(_mul(x_axis, math.cos(angle)), _mul(y_axis, math.sin(angle)))
    y_col = _add(_mul(x_axis, -math.sin(angle)), _mul(y_axis, math.cos(angle)))
    position = _add(center, _add(_mul(x_col, _input_number(x_mm, "PLACE_COORDINATE") / 1000), _mul(y_col, _input_number(y_mm, "PLACE_COORDINATE") / 1000)))
    return {"position_base_m": position, "rotation_base_columns": [x_col, y_col, normal]}


def validate_cell_calibration_document(calibration, *, yaw0, robot, required_status, now=None):
    """Validate one cell document independently of a specific JobSpec or grasp."""
    calibration = _exact(calibration, CALIBRATION_KEYS, "CALIBRATION_KEYS")
    limits = _exact(calibration["limits"], LIMIT_KEYS, "CALIBRATION_LIMITS")
    if calibration["schema_version"] != "data_factory.cell_calibration.v1":
        raise ContractError("CALIBRATION_ID")
    for key in ("calibration_id", "robot_system_id", "place_id"):
        _id(calibration[key], "CALIBRATION_ID")
    if calibration["qualification_status"] != required_status:
        raise ContractError("CALIBRATION_ID")
    if calibration["robot_system_id"] != robot.get("robot_system_id"):
        raise ContractError("CALIBRATION_ID")
    validate_yaw0_sheet(yaw0)
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
    fit = fit_place_calibration(calibration["center_base_m"], calibration["x_ref_base_m"], calibration["table_normal_base"], yaw0["registration"], calibration["scale_bar_measured_mm"], calibration["y_check_base_m"])
    center, x, y, z, metrics = fit["center"], fit["x"], fit["y"], fit["z"], fit["metrics"]
    values = {key: _number(value, "CALIBRATION_LIMITS") for key, value in limits.items()}
    if any(v < 0 for v in values.values()) or values["min_x_ref_separation_mm"] <= 0: raise ContractError("CALIBRATION_LIMITS")
    if metrics["xref_observed_mm"] < values["min_x_ref_separation_mm"]: raise ContractError("CALIBRATION_SEPARATION")
    if metrics["scale_error_mm"] > values["max_scale_error_mm"]: raise ContractError("CALIBRATION_SCALE")
    if metrics["xref_distance_error_mm"] > values["max_x_ref_distance_error_mm"]: raise ContractError("CALIBRATION_DISTANCE")
    if metrics["xref_out_of_plane_mm"] > values["max_x_ref_out_of_plane_mm"]: raise ContractError("CALIBRATION_OUT_OF_PLANE")
    if metrics["y_check_residual_mm"] > values["max_y_check_residual_mm"]: raise ContractError("CALIBRATION_Y_CHECK")
    combined = sum(value for key, value in metrics.items() if key != "xref_observed_mm")
    if combined > values["combined_error_bound_mm"]: raise ContractError("CALIBRATION_COMBINED_ERROR")
    return {"center": center, "x": x, "y": y, "z": z, "limits": values, "combined_error_mm": combined, "document": calibration}


def _calibration(calibration, job, yaw0, robot, now):
    resolved = validate_cell_calibration_document(calibration, yaw0=yaw0, robot=robot, required_status="QUALIFIED", now=now)
    if calibration["calibration_id"] != job["cell_calibration_id"] or calibration["robot_system_id"] != job["robot_system_id"] or calibration["place_id"] != job["place_id"]:
        raise ContractError("CALIBRATION_ID")
    return {key: resolved[key] for key in ("center", "x", "y", "z", "document")}


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
    object_profile = _profile(root, "objects", normalized["object_profile_id"], "object_profile_id", "data_factory.object_profile.v2")
    grasp = _profile(root, "grasps", normalized["grasp_profile_id"], "grasp_profile_id", "data_factory.grasp_profile.v2")
    if not isinstance(robot.get("base_frame"), str):
        raise ContractError("ROBOT_CONTRACT")
    _digest(robot.get("tcp_digest"), "ROBOT_CONTRACT")
    if grasp.get("object_profile_id") != normalized["object_profile_id"]:
        raise ContractError("GRASP_OBJECT")
    if grasp["schema_version"] == "data_factory.grasp_profile.v3":
        geometry = grasp["grasp_geometry"]
        dimensions = object_profile["dimensions_mm"]
        depth = geometry["depth_from_top_mm"]
        translation = geometry["datum_to_tcp_grasp"]["translation_m"]
        if (
            grasp["object_profile_digest"] != canonical_digest(object_profile)
            or depth >= dimensions[2]
            or abs(translation[0]) > 1e-9
            or abs(translation[1]) > 1e-9
            or abs(
                translation[2]
                - (dimensions[2] / 2.0 - depth) / 1000.0
            ) > 1e-9
        ):
            raise ContractError("GRASP_OBJECT")
    if normalized["instruction"] != task_instruction(
        normalized["task"], object_profile["description"],
    ):
        raise ContractError("JOB_TEXT")
    cell_path = _safe_profile_path(root, "cells", normalized["cell_calibration_id"])
    calibration = load_json_strict(cell_path)
    resolved_calibration = _calibration(calibration, normalized, yaw0, robot, now)
    _sheet_contract(selected, yaw0, normalized, object_profile, calibration)
    input_digests = {"selected_sheet": canonical_digest(selected), "yaw0_sheet": canonical_digest(yaw0), "cell_calibration": canonical_digest(calibration), "robot_system": canonical_digest(robot), "collection_profile": canonical_digest(collection), "object_profile": canonical_digest(object_profile), "grasp_profile": canonical_digest(grasp)}
    resolved_job_digest = canonical_digest({"job": normalized, "input_digests": input_digests})
    return {"normalized_job": normalized, "input_digests": input_digests, "resolved_job_digest": resolved_job_digest, "robot": robot, "collection_profile": collection, "calibration": resolved_calibration, "object_profile": object_profile, "grasp_profile": grasp}


def resolve_pose(validated):
    job, cal, robot = validated["normalized_job"], validated["calibration"], validated["robot"]
    pose = resolve_place_pose(cal["center"], cal["x"], cal["y"], cal["z"], job["yaw_deg"], job["x_mm"], job["y_mm"])
    return {"frame_id": robot["base_frame"], **pose, "resolved_job_digest": validated["resolved_job_digest"], "input_digests": validated["input_digests"]}


def _rotation(columns, code):
    if not isinstance(columns, list) or len(columns) != 3:
        raise ContractError(code)
    result = [_vec(column, code) for column in columns]
    if any(abs(_dot(column, column) - 1) > 1e-9 for column in result) or any(abs(_dot(result[a], result[b])) > 1e-9 for a, b in ((0, 1), (0, 2), (1, 2))) or _norm(_sub(_cross(result[0], result[1]), result[2])) > 1e-9:
        raise ContractError(code)
    return result


def validate_rigid_transform(value, code="TRANSFORM"):
    value = _exact(value, {"translation_m", "rotation_columns"}, code)
    return {"translation_m": _vec(value["translation_m"], code), "rotation_columns": _rotation(value["rotation_columns"], code)}


def _matvec(columns, vector):
    return [sum(columns[column][row] * vector[column] for column in range(3)) for row in range(3)]


def compose_rigid_transform(left, right):
    rotation = [_matvec(left["rotation_columns"], column) for column in right["rotation_columns"]]
    return {"translation_m": _add(left["translation_m"], _matvec(left["rotation_columns"], right["translation_m"])), "rotation_columns": rotation}


def inverse_rigid_transform(transform):
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


def _planning_scene(value):
    value = _exact(value, PLANNING_SCENE_KEYS, "MOTION_PLANNING_SCENE")
    if value["frame_id"] != "base_link":
        raise ContractError("MOTION_PLANNING_SCENE")
    floor = _exact(value["floor"], PLANNING_SCENE_FLOOR_KEYS, "MOTION_PLANNING_SCENE")
    wall = _exact(value["wall"], PLANNING_SCENE_WALL_KEYS, "MOTION_PLANNING_SCENE")
    for item in (floor, wall):
        _id(item["id"], "MOTION_PLANNING_SCENE")
        dimensions = item["dimensions_m"]
        if not isinstance(dimensions, list) or len(dimensions) != 3:
            raise ContractError("MOTION_PLANNING_SCENE")
        item["dimensions_m"] = [_number(number, "MOTION_PLANNING_SCENE") for number in dimensions]
        if any(number <= 0 for number in item["dimensions_m"]):
            raise ContractError("MOTION_PLANNING_SCENE")
    if not isinstance(floor["source"], str) or not floor["source"] or not floor["source"].isprintable():
        raise ContractError("MOTION_PLANNING_SCENE")
    floor["surface_z_m"] = _number(floor["surface_z_m"], "MOTION_PLANNING_SCENE")
    wall["near_face_y_m"] = _number(wall["near_face_y_m"], "MOTION_PLANNING_SCENE")
    wall["j1_home_deg"] = _number(wall["j1_home_deg"], "MOTION_PLANNING_SCENE")
    direction = wall["home_arm_protrusion_base_xy"]
    if not isinstance(direction, list) or len(direction) != 2:
        raise ContractError("MOTION_PLANNING_SCENE")
    wall["home_arm_protrusion_base_xy"] = [_number(number, "MOTION_PLANNING_SCENE") for number in direction]
    if abs(sum(number * number for number in wall["home_arm_protrusion_base_xy"]) - 1) > 1e-9 or wall["wall_side"] != "opposite_home_arm_protrusion":
        raise ContractError("MOTION_PLANNING_SCENE")
    return {"frame_id": value["frame_id"], "floor": floor, "wall": wall}


def validate_planning_scene_profile(value, *, expected_robot_system_id):
    """Validate one reusable floor/wall authority and resolve its collision scene."""
    profile = _exact(
        value, PLANNING_SCENE_PROFILE_KEYS, "PLANNING_SCENE_PROFILE",
    )
    if (
        profile["schema_version"] != "data_factory.planning_scene_profile.v1"
        or profile["qualification_status"] != "QUALIFIED"
        or profile["robot_system_id"] != expected_robot_system_id
        or profile["frame_id"] != "base_link"
    ):
        raise ContractError("PLANNING_SCENE_PROFILE")
    _id(profile["planning_scene_profile_id"], "PLANNING_SCENE_PROFILE")
    _id(profile["robot_system_id"], "PLANNING_SCENE_PROFILE")
    floor = _exact(
        profile["floor"], PLANNING_SCENE_PROFILE_FLOOR_KEYS,
        "PLANNING_SCENE_PROFILE",
    )
    _id(floor["id"], "PLANNING_SCENE_PROFILE")
    dimensions = floor["dimensions_m"]
    if not isinstance(dimensions, list) or len(dimensions) != 3:
        raise ContractError("PLANNING_SCENE_PROFILE")
    dimensions = [
        _number(number, "PLANNING_SCENE_PROFILE") for number in dimensions
    ]
    measured = _number(
        floor["measured_surface_z_m"], "PLANNING_SCENE_PROFILE",
    )
    margin = _number(
        floor["collision_margin_m"], "PLANNING_SCENE_PROFILE",
    )
    tolerance = _number(
        floor["workspace_datum_tolerance_m"],
        "PLANNING_SCENE_PROFILE",
    )
    if (
        any(number <= 0 for number in dimensions)
        or margin < 0 or tolerance <= 0
        or not isinstance(floor["source"], str) or not floor["source"]
        or not floor["source"].isprintable()
    ):
        raise ContractError("PLANNING_SCENE_PROFILE")
    _digest(
        floor["source_measurement_digest"], "PLANNING_SCENE_PROFILE",
    )
    wall = _exact(
        profile["wall"], PLANNING_SCENE_WALL_KEYS,
        "PLANNING_SCENE_PROFILE",
    )
    scene = _planning_scene({
        "frame_id": profile["frame_id"],
        "floor": {
            "id": floor["id"],
            "dimensions_m": dimensions,
            "surface_z_m": measured + margin,
            "source": floor["source"],
        },
        "wall": wall,
    })
    return {
        "document": profile,
        "digest": canonical_digest(profile),
        "planning_scene": scene,
        "planning_scene_digest": canonical_digest(scene),
        "measured_surface_z_m": measured,
        "collision_margin_m": margin,
        "workspace_datum_tolerance_m": tolerance,
    }


def _validate_motion_qualification(
    qualification, validated, home, *, urdf,
    planning_scene_profile=None, now=None,
):
    schema = qualification.get("schema_version") if isinstance(qualification, dict) else None
    keys = MOTION_QUALIFICATION_KEYS_BY_SCHEMA.get(schema)
    if keys is None:
        raise ContractError("MOTION_SCHEMA")
    qualification = _exact(qualification, keys, "MOTION_KEYS")
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
    planning_scene = _planning_scene(qualification["planning_scene"])
    if canonical_digest(planning_scene) != qualification["planning_scene_digest"]:
        raise ContractError("MOTION_PLANNING_SCENE_BINDING")
    if schema == "data_factory.motion_qualification.v2":
        if planning_scene_profile is None:
            raise ContractError("MOTION_PLANNING_SCENE_BINDING")
        scene_profile = validate_planning_scene_profile(
            planning_scene_profile,
            expected_robot_system_id=job["robot_system_id"],
        )
        if (
            qualification["planning_scene_profile_id"]
            != planning_scene_profile.get("planning_scene_profile_id")
            or _digest(
                qualification["planning_scene_profile_digest"],
                "MOTION_DIGESTS",
            ) != scene_profile["digest"]
            or planning_scene != scene_profile["planning_scene"]
            or qualification["planning_scene_digest"]
            != scene_profile["planning_scene_digest"]
        ):
            raise ContractError("MOTION_PLANNING_SCENE_BINDING")
        object_height_m = validated["object_profile"]["dimensions_mm"][2] / 1000.0
        object_center_z_m = validated["calibration"]["center"][2]
        expected_center_z_m = (
            scene_profile["measured_surface_z_m"] + object_height_m / 2.0
        )
        if abs(object_center_z_m - expected_center_z_m) > scene_profile[
            "workspace_datum_tolerance_m"
        ]:
            raise ContractError("MOTION_WORKSPACE_FLOOR_BINDING")
    if qualification["robot_description_digest"] != home["robot_description_digest"]: raise ContractError("MOTION_HOME_BINDING")
    frames = _exact(qualification["frames"], MOTION_FRAMES, "MOTION_FRAMES")
    if (frames["planning_frame"] != "base_link" or frames["planning_frame"] != validated["robot"]["base_frame"] or frames["planning_group"] != "fairino5_v6_group" or
            frames["tool_link"] != "wrist3_link" or any(not isinstance(value, str) or not value for value in frames.values())):
        raise ContractError("MOTION_FRAMES")
    transforms = {key: validate_rigid_transform(qualification[key], "MOTION_TRANSFORM") for key in ("tool_to_tcp", "datum_to_tcp_grasp")}
    if schema == "data_factory.motion_qualification.v2":
        grasp = validated["grasp_profile"]
        if (
            grasp.get("schema_version") != "data_factory.grasp_profile.v3"
            or transforms["datum_to_tcp_grasp"]
            != grasp["grasp_geometry"]["datum_to_tcp_grasp"]
        ):
            raise ContractError("MOTION_GRASP_BINDING")
    offsets = _exact(qualification["offsets_m"], MOTION_OFFSETS, "MOTION_OFFSETS")
    offsets = {key: _number(value, "MOTION_OFFSETS") for key, value in offsets.items()}
    if not (offsets["pregrasp"] > offsets["approach_stop"] > 0 and offsets["lift"] > 0 and offsets["retreat"] > 0): raise ContractError("MOTION_OFFSETS")
    gripper = _exact(qualification["gripper_positions_m"], {"open", "closed"}, "MOTION_GRIPPER")
    gripper = {key: _number(value, "MOTION_GRIPPER") for key, value in gripper.items()}
    lower, upper = _urdf_motion_limits(urdf)
    if any(not lower <= value <= upper for value in gripper.values()) or gripper["open"] <= gripper["closed"]: raise ContractError("MOTION_GRIPPER")
    grasp_profile = validated["grasp_profile"]
    close = copy.deepcopy(grasp_profile["gripper_close"])
    if grasp_profile.get("schema_version") == "data_factory.grasp_profile.v3":
        opened = grasp_profile["gripper_open"]
        close.update(
            open_velocity_percent=opened["velocity_percent"],
            open_force_percent=opened["force_percent"],
        )
    if gripper["closed"] != close["command_position_m"]: raise ContractError("MOTION_GRIPPER")
    if (
        schema == "data_factory.motion_qualification.v2"
        and gripper["open"]
        != validated["grasp_profile"]["gripper_open"]["command_position_m"]
    ):
        raise ContractError("MOTION_GRIPPER")
    safe = qualification["qualified_safe_joint_positions_rad"]
    if not isinstance(safe, list) or len(safe) != len(HOME_JOINT_ORDER) or any(abs(_number(value, "MOTION_SAFE_JOINTS") - expected) > 1e-12 for value, expected in zip(safe, home["nominal_target_rad"])): raise ContractError("MOTION_SAFE_JOINTS")
    tolerance = _exact(qualification["goal_tolerances"], MOTION_GOAL_TOLERANCES, "MOTION_TOLERANCES")
    tolerance = {key: _number(value, "MOTION_TOLERANCES") for key, value in tolerance.items()}
    if any(value <= 0 for value in tolerance.values()): raise ContractError("MOTION_TOLERANCES")
    max_joint_state_age_s = _number(qualification["max_joint_state_age_s"], "MOTION_JOINT_STATE_AGE")
    if max_joint_state_age_s <= 0: raise ContractError("MOTION_JOINT_STATE_AGE")
    execution_timeouts = _exact(qualification["execution_timeouts_s"], MOTION_EXECUTION_TIMEOUTS, "MOTION_EXECUTION_TIMEOUTS")
    execution_timeouts = {key: _number(value, "MOTION_EXECUTION_TIMEOUTS") for key, value in execution_timeouts.items()}
    if any(value <= 0 for value in execution_timeouts.values()): raise ContractError("MOTION_EXECUTION_TIMEOUTS")
    phase_limits = _exact(qualification["phase_limits"], set(MOTION_PHASES), "MOTION_PHASE_LIMITS")
    normalized_limits = {}
    for phase in MOTION_PHASES:
        limit = phase_limits[phase]
        if phase.startswith("GRIPPER"):
            limit = _exact(limit, {"command_duration_s", "execution_timeout_s", "completion_tolerance_m"}, "MOTION_PHASE_LIMITS")
            values = {key: _number(value, "MOTION_PHASE_LIMITS") for key, value in limit.items()}
            if values["execution_timeout_s"] <= values["command_duration_s"]: raise ContractError("MOTION_PHASE_LIMITS")
            if phase == "GRIPPER_CLOSE" and (close["acceptable_feedback_m"]["max"] <= close["command_position_m"] or values["completion_tolerance_m"] != close["acceptable_feedback_m"]["max"] - close["command_position_m"]): raise ContractError("MOTION_PHASE_LIMITS")
            if (
                schema == "data_factory.motion_qualification.v2"
                and phase == "GRIPPER_OPEN"
                and values["completion_tolerance_m"]
                != validated["grasp_profile"]["gripper_open"][
                    "completion_tolerance_m"
                ]
            ):
                raise ContractError("MOTION_PHASE_LIMITS")
        else:
            limit = _exact(limit, {"velocity_scaling", "acceleration_scaling", "planning_timeout_s", "execution_timeout_s"}, "MOTION_PHASE_LIMITS")
            values = {key: _number(value, "MOTION_PHASE_LIMITS") for key, value in limit.items()}
            if values["velocity_scaling"] > .1 or values["acceleration_scaling"] > .1: raise ContractError("MOTION_PHASE_LIMITS")
        if any(value <= 0 for value in values.values()): raise ContractError("MOTION_PHASE_LIMITS")
        normalized_limits[phase] = values
    _timestamp(qualification["qualified_at"], "MOTION_QUALIFIED_AT", now=now)
    return {"digest": canonical_digest(qualification), "frames": frames, "planning_scene": planning_scene, "transforms": transforms, "offsets": offsets, "gripper": gripper, "gripper_requirements": close, "safe": [_number(v, "MOTION_SAFE_JOINTS") for v in safe], "limits": normalized_limits, "tolerances": tolerance, "max_joint_state_age_s": max_joint_state_age_s, "execution_timeouts_s": execution_timeouts, "pins": {key: qualification[key] for key in ("robot_description_digest", "moveit_config_digest", "planning_scene_digest")}}


def resolve_motion_program(
    validated, motion_qualification, home_candidate, *, urdf,
    expected_robot_system_id, release_pose=None,
    release_validated=None, release_motion_qualification=None,
    planning_scene_profile=None, now=None,
):
    """Resolve a qualification-bound, offline-only motion program; it authorizes no execution."""
    home_raw = load_json_strict(json.dumps(home_candidate, allow_nan=False)) if isinstance(home_candidate, dict) else load_json_strict(home_candidate)
    home = validate_home_candidate(home_raw, urdf=urdf, expected_robot_system_id=expected_robot_system_id)
    qualification_raw = load_json_strict(json.dumps(motion_qualification, allow_nan=False)) if isinstance(motion_qualification, dict) else load_json_strict(motion_qualification)
    q = _validate_motion_qualification(
        qualification_raw, validated,
        {**home, "robot_description_digest": home_raw["robot_description_digest"]},
        urdf=urdf, planning_scene_profile=planning_scene_profile, now=now,
    )
    pose = resolve_pose(validated)
    job = validated["normalized_job"]
    cross_workspace = (
        release_validated is not None
        or release_motion_qualification is not None
    )
    if cross_workspace and (
        not isinstance(release_validated, dict)
        or release_motion_qualification is None
        or release_pose is not None
    ):
        raise ContractError("MOTION_ENDPOINT_BINDING")
    destination_q = None
    if cross_workspace:
        destination_job = release_validated.get("normalized_job")
        destination_inputs = release_validated.get("input_digests")
        if (
            not isinstance(destination_job, dict)
            or not isinstance(destination_inputs, dict)
            or job.get("task") != "pick_place"
            or destination_job.get("task") != job["task"]
            or destination_job.get("place_id") == job.get("place_id")
            or any(
                destination_job.get(field) != job.get(field)
                for field in (
                    "robot_system_id", "collection_profile_id",
                    "object_profile_id", "grasp_profile_id", "instruction",
                    "episode_intent",
                )
            )
            or any(
                destination_inputs.get(field)
                != validated.get("input_digests", {}).get(field)
                for field in (
                    "robot_system", "collection_profile", "object_profile",
                    "grasp_profile",
                )
            )
        ):
            raise ContractError("MOTION_ENDPOINT_BINDING")
        destination_q = _validate_motion_qualification(
            release_motion_qualification, release_validated,
            {**home, "robot_description_digest": home_raw["robot_description_digest"]},
            urdf=urdf, planning_scene_profile=planning_scene_profile, now=now,
        )
        compatible_fields = (
            "frames", "planning_scene", "transforms", "offsets", "gripper",
            "gripper_requirements", "safe", "limits", "tolerances",
            "max_joint_state_age_s", "execution_timeouts_s", "pins",
        )
        if any(destination_q[field] != q[field] for field in compatible_fields):
            raise ContractError("MOTION_ENDPOINT_COMPATIBILITY")
        release_pose = {
            key: destination_job[key]
            for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
        }
        release_resolved = resolve_pose(release_validated)
    elif release_pose is None:
        release_pose = {key: job[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")}
    release_pose = _exact(
        release_pose, {"place_id", "yaw_deg", "x_mm", "y_mm"},
        "MOTION_RELEASE_POSE",
    )
    if not cross_workspace and release_pose["place_id"] != job["place_id"]:
        raise ContractError("MOTION_RELEASE_POSE")
    if job["task"] == "pick_place" and all(
        release_pose[key] == job[key]
        for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
    ):
        raise ContractError("TASK_BINDING_DISTINCT")
    if not cross_workspace:
        release_resolved = resolve_place_pose(
            validated["calibration"]["center"], validated["calibration"]["x"], validated["calibration"]["y"], validated["calibration"]["z"],
            _number(release_pose["yaw_deg"], "MOTION_RELEASE_POSE"),
            _number(release_pose["x_mm"], "MOTION_RELEASE_POSE"),
            _number(release_pose["y_mm"], "MOTION_RELEASE_POSE"),
        )
    datum = {"translation_m": pose["position_base_m"], "rotation_columns": pose["rotation_base_columns"]}
    release_datum = {"translation_m": release_resolved["position_base_m"], "rotation_columns": release_resolved["rotation_base_columns"]}
    tool_inverse = inverse_rigid_transform(q["transforms"]["tool_to_tcp"])
    release_clearance_m = (
        validated["grasp_profile"]["grasp_geometry"][
            "release_clearance_mm"
        ] / 1000.0
        if validated["grasp_profile"].get("schema_version")
        == "data_factory.grasp_profile.v3"
        else 0.0
    )
    def target(frame, offset):
        tcp = compose_rigid_transform(frame, q["transforms"]["datum_to_tcp_grasp"])
        shifted = {"translation_m": _add(tcp["translation_m"], _mul(frame["rotation_columns"][2], offset)), "rotation_columns": tcp["rotation_columns"]}
        return {"base_tcp": shifted, "base_tool": compose_rigid_transform(shifted, tool_inverse)}
    offsets = {
        "PREGRASP_PTP": (datum, q["offsets"]["pregrasp"]),
        "APPROACH_STOP_LIN": (datum, q["offsets"]["approach_stop"]),
        "FINAL_APPROACH_LIN": (datum, 0),
        "LIFT_LIN": (datum, q["offsets"]["lift"]),
        "RECYCLE_APPROACH_PTP": (release_datum, q["offsets"]["pregrasp"]),
        "LOWER_LIN": (release_datum, release_clearance_m),
        "RETREAT_LIN": (release_datum, q["offsets"]["retreat"]),
    }
    recording_boundary = TASK_CONTRACTS[job["task"]]["recording_boundary"]
    steps = []
    for phase in MOTION_PHASES:
        step = {"phase": phase, "limits": q["limits"][phase]}
        if phase in offsets: step["target"] = target(*offsets[phase])
        elif phase.startswith("GRIPPER"): step["gripper_position_m"] = q["gripper"]["closed" if phase == "GRIPPER_CLOSE" else "open"]
        else: step["joint_positions_rad"] = q["safe"]
        if phase == recording_boundary: step["pause_after"] = "SEMANTIC_VERDICT"
        steps.append(step)
    binding_digests = {**validated["input_digests"], **q["pins"], "motion_qualification": q["digest"], "home_candidate": home["candidate_digest"]}
    result = {
        "schema_version": (
            "fr5.motion_program.v4" if cross_workspace
            else "fr5.motion_program.v2"
        ),
        "robot_system_id": validated["normalized_job"]["robot_system_id"],
        "resolved_job_digest": validated["resolved_job_digest"],
        "binding_digests": binding_digests,
        "frames": q["frames"], "planning_scene": q["planning_scene"],
        "planning": {
            "pipeline_id": "pilz_industrial_motion_planner",
            "ptp_planner_id": "PTP", "lin_planner_id": "LIN",
            "goal_tolerances": q["tolerances"],
            "max_joint_state_age_s": q["max_joint_state_age_s"],
        },
        "gripper_requirements": q["gripper_requirements"],
        "execution_timeouts_s": q["execution_timeouts_s"],
        "steps": steps,
    }
    if cross_workspace:
        endpoint_bindings = sorted(
            [
                {
                    "workspace_id": job["place_id"],
                    "cell_calibration_id": job["cell_calibration_id"],
                    "cell_calibration_digest": validated["input_digests"][
                        "cell_calibration"
                    ],
                    "motion_recipe_digest": q["digest"],
                },
                {
                    "workspace_id": destination_job["place_id"],
                    "cell_calibration_id": destination_job[
                        "cell_calibration_id"
                    ],
                    "cell_calibration_digest": release_validated[
                        "input_digests"
                    ]["cell_calibration"],
                    "motion_recipe_digest": destination_q["digest"],
                },
            ],
            key=lambda item: (
                item["workspace_id"], item["cell_calibration_id"]
            ),
        )
        result.update(
            destination_resolved_job_digest=(
                release_validated["resolved_job_digest"]
            ),
            destination_binding_digests={
                **release_validated["input_digests"], **destination_q["pins"],
                "motion_qualification": destination_q["digest"],
                "home_candidate": home["candidate_digest"],
            },
            endpoint_bindings=endpoint_bindings,
            endpoint_bindings_digest=canonical_digest(endpoint_bindings),
        )
    return result


def validate_motion_program(value):
    """Validate the exact offline motion-program contract emitted above."""
    keys = {"schema_version", "robot_system_id", "resolved_job_digest", "binding_digests", "frames", "planning_scene", "planning", "gripper_requirements", "execution_timeouts_s", "steps"}
    schema = value.get("schema_version") if isinstance(value, dict) else None
    if schema == "fr5.motion_program.v4":
        keys |= {
            "destination_resolved_job_digest",
            "destination_binding_digests",
            "endpoint_bindings", "endpoint_bindings_digest",
        }
    value = _exact(value, keys, "MOTION_PROGRAM_SCHEMA")
    if schema not in {"fr5.motion_program.v2", "fr5.motion_program.v4"}: raise ContractError("MOTION_PROGRAM_SCHEMA")
    _id(value["robot_system_id"], "MOTION_PROGRAM_ROBOT_ID")
    _digest(value["resolved_job_digest"], "MOTION_PROGRAM_DIGEST")
    binding_keys = {"selected_sheet", "yaw0_sheet", "cell_calibration", "robot_system", "collection_profile", "object_profile", "grasp_profile", "robot_description_digest", "moveit_config_digest", "planning_scene_digest", "motion_qualification", "home_candidate"}
    bindings = _exact(value["binding_digests"], binding_keys, "MOTION_PROGRAM_BINDING")
    for item in bindings.values(): _digest(item, "MOTION_PROGRAM_BINDING")
    if schema == "fr5.motion_program.v4":
        _digest(
            value["destination_resolved_job_digest"],
            "MOTION_PROGRAM_DIGEST",
        )
        destination_bindings = _exact(
            value["destination_binding_digests"], binding_keys,
            "MOTION_PROGRAM_BINDING",
        )
        for item in destination_bindings.values():
            _digest(item, "MOTION_PROGRAM_BINDING")
        endpoint_keys = {
            "workspace_id", "cell_calibration_id",
            "cell_calibration_digest", "motion_recipe_digest",
        }
        endpoint_bindings = value["endpoint_bindings"]
        if not isinstance(endpoint_bindings, list) or len(endpoint_bindings) != 2:
            raise ContractError("MOTION_ENDPOINT_BINDING")
        for endpoint in endpoint_bindings:
            endpoint = _exact(
                endpoint, endpoint_keys, "MOTION_ENDPOINT_BINDING",
            )
            _id(endpoint["workspace_id"], "MOTION_ENDPOINT_BINDING")
            _id(endpoint["cell_calibration_id"], "MOTION_ENDPOINT_BINDING")
            _digest(
                endpoint["cell_calibration_digest"],
                "MOTION_ENDPOINT_BINDING",
            )
            _digest(
                endpoint["motion_recipe_digest"],
                "MOTION_ENDPOINT_BINDING",
            )
        if (
            endpoint_bindings != sorted(
                endpoint_bindings,
                key=lambda item: (
                    item["workspace_id"], item["cell_calibration_id"]
                ),
            )
            or len({item["workspace_id"] for item in endpoint_bindings}) != 2
            or len({item["cell_calibration_id"] for item in endpoint_bindings}) != 2
            or {
                (
                    item["cell_calibration_digest"],
                    item["motion_recipe_digest"],
                )
                for item in endpoint_bindings
            } != {
                (
                    bindings["cell_calibration"],
                    bindings["motion_qualification"],
                ),
                (
                    destination_bindings["cell_calibration"],
                    destination_bindings["motion_qualification"],
                ),
            }
            or value["endpoint_bindings_digest"]
            != canonical_digest(endpoint_bindings)
        ):
            raise ContractError("MOTION_ENDPOINT_BINDING")
        _digest(
            value["endpoint_bindings_digest"], "MOTION_ENDPOINT_BINDING",
        )
        if (
            value["destination_resolved_job_digest"]
            == value["resolved_job_digest"]
            or destination_bindings["cell_calibration"]
            == bindings["cell_calibration"]
            or destination_bindings["motion_qualification"]
            == bindings["motion_qualification"]
            or any(
                destination_bindings[key] != bindings[key]
                for key in (
                    "robot_system", "collection_profile", "object_profile",
                    "grasp_profile", "robot_description_digest",
                    "moveit_config_digest", "planning_scene_digest",
                    "home_candidate",
                )
            )
        ):
            raise ContractError("MOTION_ENDPOINT_COMPATIBILITY")
    planning_scene = _planning_scene(value["planning_scene"])
    if canonical_digest(planning_scene) != bindings["planning_scene_digest"]: raise ContractError("MOTION_PROGRAM_PLANNING_SCENE")
    raw_requirements = value["gripper_requirements"]
    open_settings = {}
    if isinstance(raw_requirements, dict):
        raw_requirements = dict(raw_requirements)
        for key in ("open_velocity_percent", "open_force_percent"):
            if key in raw_requirements:
                open_settings[key] = raw_requirements.pop(key)
    requirements = _gripper_close(raw_requirements, "MOTION_PROGRAM_GRIPPER")
    for key, setting in open_settings.items():
        if (
            isinstance(setting, bool) or not isinstance(setting, int)
            or not 1 <= setting <= 100
            or key == "open_velocity_percent"
            and setting > requirements["velocity_percent"]
        ):
            raise ContractError("MOTION_PROGRAM_GRIPPER")
        requirements[key] = setting
    if requirements["acceptable_feedback_m"]["max"] <= requirements["command_position_m"]: raise ContractError("MOTION_PROGRAM_GRIPPER")
    frames = _exact(value["frames"], MOTION_FRAMES, "MOTION_PROGRAM_FRAMES")
    if frames != {"planning_frame": "base_link", "planning_group": "fairino5_v6_group", "tool_link": "wrist3_link"}: raise ContractError("MOTION_PROGRAM_FRAMES")
    planning = _exact(value["planning"], {"pipeline_id", "ptp_planner_id", "lin_planner_id", "goal_tolerances", "max_joint_state_age_s"}, "MOTION_PROGRAM_PLANNING")
    if planning["pipeline_id"] != "pilz_industrial_motion_planner" or planning["ptp_planner_id"] != "PTP" or planning["lin_planner_id"] != "LIN": raise ContractError("MOTION_PROGRAM_PLANNING")
    _exact(planning["goal_tolerances"], MOTION_GOAL_TOLERANCES, "MOTION_PROGRAM_PLANNING")
    for item in planning["goal_tolerances"].values():
        if _number(item, "MOTION_PROGRAM_PLANNING") <= 0: raise ContractError("MOTION_PROGRAM_PLANNING")
    if _number(planning["max_joint_state_age_s"], "MOTION_PROGRAM_PLANNING") <= 0: raise ContractError("MOTION_PROGRAM_PLANNING")
    execution_timeouts = _exact(value["execution_timeouts_s"], MOTION_EXECUTION_TIMEOUTS, "MOTION_PROGRAM_TIMEOUTS")
    if any(_number(item, "MOTION_PROGRAM_TIMEOUTS") <= 0 for item in execution_timeouts.values()): raise ContractError("MOTION_PROGRAM_TIMEOUTS")
    if not isinstance(value["steps"], list) or [step.get("phase") if isinstance(step, dict) else None for step in value["steps"]] != list(MOTION_PHASES): raise ContractError("MOTION_PROGRAM_PHASES")
    final_step, close_step = value["steps"][2], value["steps"][3]
    legacy_precontact = "requires_confirmation" in final_step
    legacy_grasp_verdict = "pause_after" in close_step
    if legacy_precontact != legacy_grasp_verdict:
        raise ContractError("MOTION_PROGRAM_MARKER")
    if legacy_precontact and final_step["requires_confirmation"] != "PRECONTACT_HUMAN":
        raise ContractError("MOTION_PROGRAM_MARKER")
    if legacy_grasp_verdict and close_step["pause_after"] != "GRASP_VERDICT":
        raise ContractError("MOTION_PROGRAM_GRIPPER")
    recording_boundaries = {
        contract["recording_boundary"] for contract in TASK_CONTRACTS.values()
    }
    semantic_steps = [
        step for step in value["steps"]
        if step.get("pause_after") == "SEMANTIC_VERDICT"
    ]
    if (
        len(semantic_steps) != 1
        or semantic_steps[0]["phase"] not in recording_boundaries
    ):
        raise ContractError("MOTION_PROGRAM_MARKER")
    semantic_phase = semantic_steps[0]["phase"]
    for step in value["steps"]:
        phase = step["phase"]
        extras = {"phase", "limits", "target"} if phase in {"PREGRASP_PTP", "APPROACH_STOP_LIN", "FINAL_APPROACH_LIN", "LIFT_LIN", "RECYCLE_APPROACH_PTP", "LOWER_LIN", "RETREAT_LIN"} else {"phase", "limits", "joint_positions_rad"} if phase == "SAFE_POSE_PTP" else {"phase", "limits", "gripper_position_m"}
        if legacy_precontact and phase == "FINAL_APPROACH_LIN": extras.add("requires_confirmation")
        if legacy_grasp_verdict and phase == "GRIPPER_CLOSE": extras.add("pause_after")
        if phase == semantic_phase: extras.add("pause_after")
        _exact(step, extras, "MOTION_PROGRAM_STEP")
        limit = step["limits"]
        if phase.startswith("GRIPPER"):
            _exact(limit, {"command_duration_s", "execution_timeout_s", "completion_tolerance_m"}, "MOTION_PROGRAM_LIMITS")
        else:
            _exact(limit, {"velocity_scaling", "acceleration_scaling", "planning_timeout_s", "execution_timeout_s"}, "MOTION_PROGRAM_LIMITS")
        for item in limit.values():
            if _number(item, "MOTION_PROGRAM_LIMITS") <= 0: raise ContractError("MOTION_PROGRAM_LIMITS")
        if phase.startswith("GRIPPER") and _number(limit["execution_timeout_s"], "MOTION_PROGRAM_LIMITS") <= _number(limit["command_duration_s"], "MOTION_PROGRAM_LIMITS"): raise ContractError("MOTION_PROGRAM_LIMITS")
        if not phase.startswith("GRIPPER") and (
                _number(limit["velocity_scaling"], "MOTION_PROGRAM_LIMITS") > .1 or
                _number(limit["acceleration_scaling"], "MOTION_PROGRAM_LIMITS") > .1):
            raise ContractError("MOTION_PROGRAM_LIMITS")
        if "target" in step:
            target = _exact(step["target"], {"base_tcp", "base_tool"}, "MOTION_PROGRAM_TARGET")
            validate_rigid_transform(target["base_tcp"], "MOTION_PROGRAM_TARGET"); validate_rigid_transform(target["base_tool"], "MOTION_PROGRAM_TARGET")
        if "joint_positions_rad" in step:
            if not isinstance(step["joint_positions_rad"], list) or len(step["joint_positions_rad"]) != 6: raise ContractError("MOTION_PROGRAM_JOINTS")
            [_number(item, "MOTION_PROGRAM_JOINTS") for item in step["joint_positions_rad"]]
        if "gripper_position_m" in step:
            position = _number(step["gripper_position_m"], "MOTION_PROGRAM_GRIPPER")
            if phase == "GRIPPER_CLOSE" and position != requirements["command_position_m"]: raise ContractError("MOTION_PROGRAM_GRIPPER")
        if phase == "GRIPPER_CLOSE" and _number(step["limits"]["completion_tolerance_m"], "MOTION_PROGRAM_LIMITS") != requirements["acceptable_feedback_m"]["max"] - requirements["command_position_m"]: raise ContractError("MOTION_PROGRAM_GRIPPER")
        if phase == semantic_phase and step["pause_after"] != "SEMANTIC_VERDICT": raise ContractError("MOTION_PROGRAM_MARKER")
    return value


def build_job_spec(selected_sheet, *, point_id=None, x_mm=None, y_mm=None, job_id, robot_system_id, collection_profile_id, cell_calibration_id, object_profile_id, object_description, grasp_profile_id, operator_or_agent_id, approval_expiry, task="pickup_e2e", now=None):
    """Build one supported JobSpec from an A4 point or bounded coordinate."""
    if not isinstance(object_description, str) or not 1 <= len(object_description) <= 80 or object_description.strip() != object_description or not object_description.isprintable():
        raise ContractError("OBJECT_DESCRIPTION")
    sheet = _document(selected_sheet, "INPUT_SELECTED_SHEET")
    validate_sheet_manifest(sheet)
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
        x_value, y_value = bounded_place_coordinate(sheet, x_mm, y_mm)
        pose = {"place_id": sheet["place_id"], "yaw_deg": sheet["yaw_deg"], "x_mm": x_value, "y_mm": y_value}
    return normalize_job_spec({
        "schema_version": "data_factory.job.v1",
        "job_id": job_id,
        "task": task,
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
        "instruction": task_instruction(task, object_description),
        "episode_intent": TASK_CONTRACTS.get(task, {}).get("episode_intent"),
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
    builder.add_argument("--task", choices=tuple(TASK_CONTRACTS), default="pickup_e2e")
    builder.add_argument("--point-id")
    builder.add_argument("--x-mm")
    builder.add_argument("--y-mm")
    for name in ("job-id", "robot-system-id", "collection-profile-id", "cell-calibration-id", "object-profile-id", "grasp-profile-id", "operator-or-agent-id", "approval-expiry"):
        builder.add_argument(f"--{name}")
    try:
        args = parser.parse_args()
        if args.command == "validate-home-candidate":
            text = sys.stdin.buffer.read().decode("utf-8") if args.candidate == "-" else Path(args.candidate).read_text()
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
            object_profile_id = _profile_choice(args.object_profile_id, label="object_profile_id", root=root, folder="objects", interactive=args.interactive)
            object_description = _profile(root, "objects", object_profile_id, "object_profile_id", "data_factory.object_profile.v2")["description"]
            job = build_job_spec(
                selected,
                point_id=point_id,
                x_mm=x_mm,
                y_mm=y_mm,
                job_id=_required(args.job_id, "job_id", args.interactive),
                robot_system_id=_profile_choice(args.robot_system_id, label="robot_system_id", root=root, folder="robot_systems", interactive=args.interactive),
                collection_profile_id=_profile_choice(args.collection_profile_id, label="collection_profile_id", root=root, folder="collection_profiles", interactive=args.interactive),
                cell_calibration_id=_profile_choice(args.cell_calibration_id, label="cell_calibration_id", root=root, folder="cells", interactive=args.interactive),
                object_profile_id=object_profile_id,
                object_description=object_description,
                grasp_profile_id=_profile_choice(args.grasp_profile_id, label="grasp_profile_id", root=root, folder="grasps", interactive=args.interactive),
                operator_or_agent_id=_required(args.operator_or_agent_id, "operator_or_agent_id", args.interactive),
                approval_expiry=_required(args.approval_expiry, "approval_expiry", args.interactive),
                task=args.task,
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
            scene_profile = None
            if qualification.get("schema_version") == "data_factory.motion_qualification.v2":
                scene_profile = load_json_strict(_safe_profile_path(
                    Path(args.config_root), "planning_scenes",
                    qualification.get("planning_scene_profile_id"),
                ))
            print(json.dumps(resolve_motion_program(validated, qualification, candidate, urdf=args.urdf, expected_robot_system_id=args.expected_robot_system_id, planning_scene_profile=scene_profile), sort_keys=True, separators=(",", ":"), allow_nan=False)); return 0
        output = resolve_pose(validated) if args.command == "resolve-pose" else {"normalized_job": validated["normalized_job"], "input_digests": validated["input_digests"], "resolved_job_digest": validated["resolved_job_digest"]}
        print(json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False)); return 0
    except (ContractError, OSError, UnicodeError) as exc:
        code = exc.code if isinstance(exc, ContractError) else "JSON_IO" if isinstance(exc, UnicodeError) else "JOB_IO"
        print(json.dumps({"error": {"code": code, "message": str(exc)}}, sort_keys=True), file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
