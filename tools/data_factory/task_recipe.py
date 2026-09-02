"""Pure task recipes and spatial bindings for the shared pickup backbone."""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

from tools.fr5_data_factory import (
    TASK_CONTRACTS,
    ContractError,
    DIGEST,
    SAFE_ID,
    canonical_digest,
    task_instruction,
)


RECIPE_SCHEMA = "data_factory.task_recipe.v1"
CATALOG_SCHEMA = "data_factory.task_recipe_catalog.v1"
BINDING_SCHEMA = "data_factory.task_binding.v2"
EPISODE_INSTRUCTION_SCHEMA = "data_factory.episode_instruction_binding.v1"
TASK_IDS = ("pickup_e2e", "pick_place")

_RECIPE_FIELDS = {
    "schema_version", "task_id", "spatial_roles", "recorded_phases",
    "recording_boundary", "post_recording_phases", "task_terminal",
    "review_checklist_id", "episode_intent", "instruction_template",
    "recipe_digest",
}
_CATALOG_FIELDS = {"schema_version", "recipes", "catalog_digest"}
_BINDING_FIELDS = {
    "schema_version", "task_id", "recipe_digest", "spatial_bindings",
    "binding_digest",
}
_SPATIAL_FIELDS = {
    "role", "workspace_id", "frame_id", "pose", "sheet_digest",
    "family_digest", "region_binding",
}
_POSE_FIELDS = {"place_id", "yaw_deg", "x_mm", "y_mm"}
_REGION_FIELDS = {
    "layout_id", "layout_digest", "region_id", "physical_binding_status",
}
_EPISODE_INSTRUCTION_FIELDS = {
    "schema_version", "task_binding", "object_profile_id",
    "object_profile_digest", "object_description", "instruction",
    "binding_digest",
}


def _phase(phase: str, internal_phase: str, label: str) -> dict[str, str]:
    return {"phase": phase, "internal_phase": internal_phase, "label": label}


def _recipe(
    task_id: str, spatial_roles: list[dict[str, Any]],
    recorded_phases: list[dict[str, str]], recording_boundary: str,
    post_recording_phases: list[dict[str, str]], review_checklist_id: str,
) -> dict[str, Any]:
    contract = TASK_CONTRACTS[task_id]
    if (
        recording_boundary != contract["recording_boundary"]
        or review_checklist_id != contract["review_checklist_id"]
    ):
        raise RuntimeError("task recipe and task contract disagree")
    value = {
        "schema_version": RECIPE_SCHEMA,
        "task_id": task_id,
        "spatial_roles": spatial_roles,
        "recorded_phases": recorded_phases,
        "recording_boundary": recording_boundary,
        "post_recording_phases": post_recording_phases,
        "task_terminal": recording_boundary,
        "review_checklist_id": review_checklist_id,
        "episode_intent": contract["episode_intent"],
        "instruction_template": contract["instruction_template"],
    }
    value["recipe_digest"] = canonical_digest(value)
    return value


_PICKUP_PHASES = [
    _phase("SOURCE_PREGRASP_PTP", "PREGRASP_PTP", "Source pregrasp"),
    _phase("SOURCE_APPROACH_STOP_LIN", "APPROACH_STOP_LIN", "Source approach stop"),
    _phase("SOURCE_FINAL_APPROACH_LIN", "FINAL_APPROACH_LIN", "Source final approach"),
    _phase("SOURCE_GRIPPER_CLOSE", "GRIPPER_CLOSE", "Close gripper at source"),
    _phase("SOURCE_LIFT_LIN", "LIFT_LIN", "Lift from source"),
]
_DESTINATION_PHASES = [
    _phase("DESTINATION_APPROACH_PTP", "RECYCLE_APPROACH_PTP", "Destination approach"),
    _phase("DESTINATION_LOWER_LIN", "LOWER_LIN", "Lower at destination"),
    _phase("DESTINATION_RELEASE", "GRIPPER_OPEN", "Release at destination"),
    _phase("DESTINATION_RETREAT_LIN", "RETREAT_LIN", "Retreat from destination"),
]
_RECIPES = (
    _recipe(
        "pickup_e2e",
        [{"role": "SOURCE", "required": True}, {"role": "NEXT_SOURCE_RESET", "required": False}],
        copy.deepcopy(_PICKUP_PHASES),
        "LIFT_LIN",
        [
            _phase("NEXT_SOURCE_RESET_APPROACH_PTP", "RECYCLE_APPROACH_PTP", "Next-source reset approach"),
            _phase("NEXT_SOURCE_RESET_LOWER_LIN", "LOWER_LIN", "Lower for next-source reset"),
            _phase("NEXT_SOURCE_RESET_RELEASE", "GRIPPER_OPEN", "Release for next-source reset"),
            _phase("NEXT_SOURCE_RESET_RETREAT_LIN", "RETREAT_LIN", "Retreat from next-source reset"),
            _phase("SAFE_POSE_PTP", "SAFE_POSE_PTP", "Return to safe pose"),
        ],
        "pickup-v2",
    ),
    _recipe(
        "pick_place",
        [{"role": "SOURCE", "required": True}, {"role": "DESTINATION", "required": True}],
        copy.deepcopy(_PICKUP_PHASES + _DESTINATION_PHASES),
        "RETREAT_LIN",
        [_phase("SAFE_POSE_PTP", "SAFE_POSE_PTP", "Return to safe pose")],
        "pick-place-v1",
    ),
)


def get_task_recipe(task_id: str) -> dict[str, Any]:
    """Return one canonical recipe without sharing mutable state."""
    if task_id not in TASK_IDS:
        raise ContractError("TASK_RECIPE_ID")
    return copy.deepcopy(_RECIPES[TASK_IDS.index(task_id)])


def task_catalog() -> dict[str, Any]:
    """Return the canonical two-task catalog in stable task and phase order."""
    value = {
        "schema_version": CATALOG_SCHEMA,
        "recipes": copy.deepcopy(list(_RECIPES)),
    }
    value["catalog_digest"] = canonical_digest(value)
    return value


def validate_task_recipe(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RECIPE_FIELDS:
        raise ContractError("TASK_RECIPE_SCHEMA")
    task_id = value.get("task_id")
    if task_id not in TASK_IDS or dict(value) != get_task_recipe(task_id):
        raise ContractError("TASK_RECIPE_CONTRACT")
    return get_task_recipe(task_id)


def validate_task_catalog(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CATALOG_FIELDS:
        raise ContractError("TASK_CATALOG_SCHEMA")
    expected = task_catalog()
    if dict(value) != expected:
        raise ContractError("TASK_CATALOG_CONTRACT")
    return expected


def _identifier(value: object) -> bool:
    return isinstance(value, str) and SAFE_ID.fullmatch(value) is not None


def validate_region_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate semantic workspace-region evidence without granting authority."""
    if not isinstance(value, Mapping) or set(value) != _REGION_FIELDS:
        raise ContractError("TASK_BINDING_REGION")
    status = value.get("physical_binding_status")
    if status == "NOT_CONFIGURED":
        if any(value.get(field) is not None for field in (
            "layout_id", "layout_digest", "region_id",
        )):
            raise ContractError("TASK_BINDING_REGION")
    elif (
        status not in {"PREPARED_NOT_VERIFIED", "VERIFIED"}
        or not _identifier(value.get("layout_id"))
        or not _identifier(value.get("region_id"))
        or not isinstance(value.get("layout_digest"), str)
        or DIGEST.fullmatch(value["layout_digest"]) is None
    ):
        raise ContractError("TASK_BINDING_REGION")
    return {
        "layout_id": value["layout_id"],
        "layout_digest": value["layout_digest"],
        "region_id": value["region_id"],
        "physical_binding_status": status,
    }


def _spatial(value: Mapping[str, Any], expected_role: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SPATIAL_FIELDS:
        raise ContractError("TASK_BINDING_SPATIAL")
    pose = value.get("pose")
    if not isinstance(pose, Mapping) or set(pose) != _POSE_FIELDS:
        raise ContractError("TASK_BINDING_POSE")
    region = validate_region_binding(value.get("region_binding"))
    coordinates = (pose.get("yaw_deg"), pose.get("x_mm"), pose.get("y_mm"))
    if (
        value.get("role") != expected_role
        or any(not _identifier(value.get(field)) for field in ("workspace_id", "frame_id"))
        or not _identifier(pose.get("place_id"))
        or any(
            isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
            for item in coordinates
        )
        or any(
            not isinstance(value.get(field), str) or DIGEST.fullmatch(value[field]) is None
            for field in ("sheet_digest", "family_digest")
        )
    ):
        raise ContractError("TASK_BINDING_SPATIAL")
    return {
        "role": expected_role,
        "workspace_id": value["workspace_id"],
        "frame_id": value["frame_id"],
        "pose": {
            "place_id": pose["place_id"],
            "yaw_deg": float(pose["yaw_deg"]),
            "x_mm": float(pose["x_mm"]),
            "y_mm": float(pose["y_mm"]),
        },
        "sheet_digest": value["sheet_digest"],
        "family_digest": value["family_digest"],
        "region_binding": region,
    }


def _make_binding(task_id: str, bindings: list[dict[str, Any]]) -> dict[str, Any]:
    value = {
        "schema_version": BINDING_SCHEMA,
        "task_id": task_id,
        "recipe_digest": get_task_recipe(task_id)["recipe_digest"],
        "spatial_bindings": copy.deepcopy(bindings),
    }
    value["binding_digest"] = canonical_digest(value)
    return value


def _same_location(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left[field] == right[field] for field in _SPATIAL_FIELDS - {"role"})


def compile_task_binding(
    task_id: str, *, source: Mapping[str, Any],
    destination: Mapping[str, Any] | None = None,
    next_source_reset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind already-resolved spatial inputs without qualifying coordinates or granting execution."""
    get_task_recipe(task_id)
    checked_source = _spatial(source, "SOURCE")
    if task_id == "pickup_e2e":
        if destination is not None:
            raise ContractError("TASK_BINDING_ROLES")
        bindings = [checked_source]
        if next_source_reset is not None:
            bindings.append(_spatial(next_source_reset, "NEXT_SOURCE_RESET"))
    else:
        if destination is None or next_source_reset is not None:
            raise ContractError("TASK_BINDING_ROLES")
        checked_destination = _spatial(destination, "DESTINATION")
        if _same_location(checked_source, checked_destination):
            raise ContractError("TASK_BINDING_DISTINCT")
        bindings = [checked_source, checked_destination]
    return _make_binding(task_id, bindings)


def validate_task_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _BINDING_FIELDS:
        raise ContractError("TASK_BINDING_SCHEMA")
    task_id = value.get("task_id")
    recipe = get_task_recipe(task_id)
    if value.get("schema_version") != BINDING_SCHEMA or value.get("recipe_digest") != recipe["recipe_digest"]:
        raise ContractError("TASK_BINDING_RECIPE")
    raw = value.get("spatial_bindings")
    expected_roles = (
        ("SOURCE",) if task_id == "pickup_e2e" and isinstance(raw, list) and len(raw) == 1
        else tuple(item["role"] for item in recipe["spatial_roles"])
    )
    if not isinstance(raw, list) or len(raw) != len(expected_roles):
        raise ContractError("TASK_BINDING_ROLES")
    checked = [_spatial(item, role) for item, role in zip(raw, expected_roles)]
    if task_id == "pick_place" and _same_location(checked[0], checked[1]):
        raise ContractError("TASK_BINDING_DISTINCT")
    expected = _make_binding(task_id, checked)
    if value.get("binding_digest") != expected["binding_digest"]:
        raise ContractError("TASK_BINDING_DIGEST")
    return expected


def task_binding_instruction(
    value: Mapping[str, Any], object_description: str,
) -> str:
    """Derive one episode label; zone words require verified endpoint bindings."""
    binding = validate_task_binding(value)
    if binding["task_id"] != "pick_place":
        return task_instruction(binding["task_id"], object_description)
    source, destination = binding["spatial_bindings"]
    source_region = source["region_binding"]
    destination_region = destination["region_binding"]
    verified = (
        source_region["physical_binding_status"] == "VERIFIED"
        and destination_region["physical_binding_status"] == "VERIFIED"
        and source_region["layout_digest"]
        == destination_region["layout_digest"]
    )
    return task_instruction(
        "pick_place", object_description,
        source_region_id=(source_region["region_id"] if verified else None),
        destination_region_id=(
            destination_region["region_id"] if verified else None
        ),
        region_binding_verified=verified,
    )


def _object_language_identity(value: Mapping[str, Any]) -> tuple[str, str, str]:
    if not isinstance(value, Mapping):
        raise ContractError("EPISODE_INSTRUCTION_OBJECT")
    identifier = value.get("object_profile_id")
    description = value.get("description")
    if (
        not _identifier(identifier)
        or not isinstance(description, str)
        or not description.strip()
        or description != description.strip()
        or "\x00" in description
    ):
        raise ContractError("EPISODE_INSTRUCTION_OBJECT")
    return identifier, description, canonical_digest(dict(value))


def compile_episode_instruction_binding(
    task_binding: Mapping[str, Any], object_profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind the exact spatial task and object wording used for one episode."""
    checked_task = validate_task_binding(task_binding)
    object_profile_id, description, object_digest = _object_language_identity(
        object_profile,
    )
    result = {
        "schema_version": EPISODE_INSTRUCTION_SCHEMA,
        "task_binding": checked_task,
        "object_profile_id": object_profile_id,
        "object_profile_digest": object_digest,
        "object_description": description,
        "instruction": task_binding_instruction(checked_task, description),
    }
    result["binding_digest"] = canonical_digest(result)
    return result


def validate_episode_instruction_binding(
    value: Mapping[str, Any], *,
    object_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate an episode label; an optional profile closes external identity."""
    if not isinstance(value, Mapping) or set(value) != _EPISODE_INSTRUCTION_FIELDS:
        raise ContractError("EPISODE_INSTRUCTION_SCHEMA")
    checked_task = validate_task_binding(value.get("task_binding"))
    if (
        value.get("schema_version") != EPISODE_INSTRUCTION_SCHEMA
        or not _identifier(value.get("object_profile_id"))
        or not isinstance(value.get("object_profile_digest"), str)
        or DIGEST.fullmatch(value["object_profile_digest"]) is None
        or not isinstance(value.get("object_description"), str)
        or not value["object_description"].strip()
        or value["object_description"] != value["object_description"].strip()
        or "\x00" in value["object_description"]
        or value.get("instruction") != task_binding_instruction(
            checked_task, value["object_description"],
        )
    ):
        raise ContractError("EPISODE_INSTRUCTION_CONTRACT")
    result = {
        "schema_version": EPISODE_INSTRUCTION_SCHEMA,
        "task_binding": checked_task,
        "object_profile_id": value["object_profile_id"],
        "object_profile_digest": value["object_profile_digest"],
        "object_description": value["object_description"],
        "instruction": value["instruction"],
    }
    result["binding_digest"] = canonical_digest(result)
    if value.get("binding_digest") != result["binding_digest"]:
        raise ContractError("EPISODE_INSTRUCTION_DIGEST")
    if object_profile is not None:
        identifier, description, digest = _object_language_identity(object_profile)
        if (
            result["object_profile_id"] != identifier
            or result["object_description"] != description
            or result["object_profile_digest"] != digest
        ):
            raise ContractError("EPISODE_INSTRUCTION_OBJECT")
    return result
