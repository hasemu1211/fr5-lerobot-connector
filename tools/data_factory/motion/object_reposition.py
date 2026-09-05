"""Shared, non-recording object reposition contract for serial campaigns."""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

from tools.data_factory.state_space import (
    validate_yaw_sample_binding,
    validate_yaw_sampling_profile,
)
from tools.data_factory.workspace_geometry import rotate_xy
from tools.fr5_data_factory import (
    ContractError,
    DIGEST,
    SAFE_ID,
    canonical_digest,
    normalize_yaw_deg,
)


SCHEMA = "data_factory.object_reposition_binding.v2"
START_STATES = frozenset({"HELD_OBJECT", "ON_SURFACE"})
EXECUTION_STAGES = {
    "HELD_OBJECT": "PRECOMMIT_POST_RECORDING",
    "ON_SURFACE": "POSTCOMMIT",
}
_POSE_FIELDS = frozenset({"place_id", "yaw_deg", "x_mm", "y_mm"})
_FIELDS = frozenset({
    "schema_version", "parent_run_id", "continuation_run_id", "next_run_id",
    "execution_stage", "recording_scope", "start_state",
    "source_pose", "target_pose", "yaw_sample_binding",
    "yaw_sample_binding_digest", "object_profile_id", "object_profile_digest",
    "grasp_profile_id", "grasp_profile_digest", "yaw_sampling_profile_id",
    "yaw_sampling_profile_digest", "motion_recipe", "recorder_authorized",
    "dataset_write_authorized", "binding_digest",
})


def _pose(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _POSE_FIELDS:
        raise ContractError("OBJECT_REPOSITION_POSE")
    if (
        not isinstance(value.get("place_id"), str)
        or SAFE_ID.fullmatch(value["place_id"]) is None
    ):
        raise ContractError("OBJECT_REPOSITION_POSE")
    numbers = []
    for field in ("yaw_deg", "x_mm", "y_mm"):
        item = value.get(field)
        if (
            isinstance(item, bool) or not isinstance(item, (int, float))
            or not math.isfinite(item)
        ):
            raise ContractError("OBJECT_REPOSITION_POSE")
        numbers.append(float(item))
    yaw, x_mm, y_mm = numbers
    return {
        "place_id": value["place_id"],
        "yaw_deg": normalize_yaw_deg(yaw),
        "x_mm": x_mm,
        "y_mm": y_mm,
    }


def yaw_preserving_destination(
    source_pose: Mapping[str, Any], destination_pose: Mapping[str, Any],
) -> dict[str, Any]:
    """Use destination sheet position/workspace while preserving source yaw."""
    source = _pose(source_pose)
    destination = _pose(destination_pose)
    if source["yaw_deg"] == destination["yaw_deg"]:
        # A same-frame round trip adds float drift to the exact next-source
        # pose and its scene slot digest, despite requiring no rotation.
        return destination
    sheet_xy = rotate_xy(
        (destination["x_mm"], destination["y_mm"]),
        destination["yaw_deg"],
    )
    local_xy = rotate_xy(sheet_xy, -source["yaw_deg"])
    return {
        **destination, "yaw_deg": source["yaw_deg"],
        "x_mm": local_xy[0], "y_mm": local_xy[1],
    }


def _same_surface_position(
    source: Mapping[str, Any], target: Mapping[str, Any],
) -> bool:
    if source["place_id"] != target["place_id"]:
        return False
    source_xy = rotate_xy(
        (source["x_mm"], source["y_mm"]), source["yaw_deg"],
    )
    target_xy = rotate_xy(
        (target["x_mm"], target["y_mm"]), target["yaw_deg"],
    )
    return all(
        math.isclose(left, right, rel_tol=0.0, abs_tol=1e-8)
        for left, right in zip(source_xy, target_xy)
    )


def _same_yaw(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=0.0, abs_tol=1e-9)


def build_object_reposition_binding(
    *, parent_run_id: str, continuation_run_id: str, next_run_id: str | None,
    start_state: str,
    source_pose: Mapping[str, Any], target_pose: Mapping[str, Any],
    object_profile: Mapping[str, Any], grasp_profile: Mapping[str, Any],
    yaw_sampling_profile: Mapping[str, Any] | None = None,
    yaw_sample_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind one reusable post-recording move without granting data writes."""
    run_ids = (parent_run_id, continuation_run_id)
    if any(
        not isinstance(value, str) or len(value) > 128
        or SAFE_ID.fullmatch(value) is None
        for value in run_ids
    ) or next_run_id is not None and (
        not isinstance(next_run_id, str) or len(next_run_id) > 128
        or SAFE_ID.fullmatch(next_run_id) is None
    ):
        raise ContractError("OBJECT_REPOSITION_RUN")
    if start_state not in START_STATES:
        raise ContractError("OBJECT_REPOSITION_START_STATE")
    if (
        start_state == "HELD_OBJECT"
        and continuation_run_id != parent_run_id
        or start_state == "ON_SURFACE"
        and (
            continuation_run_id == parent_run_id
            or next_run_id is None
            or continuation_run_id == next_run_id
        )
    ):
        raise ContractError("OBJECT_REPOSITION_CONTINUATION")
    source, target = _pose(source_pose), _pose(target_pose)
    if start_state == "ON_SURFACE" and (
        not _same_surface_position(source, target)
        or _same_yaw(source["yaw_deg"], target["yaw_deg"])
    ):
        raise ContractError("OBJECT_REPOSITION_ON_SURFACE_SCOPE")
    if not isinstance(object_profile, Mapping) or not isinstance(
        grasp_profile, Mapping,
    ):
        raise ContractError("OBJECT_REPOSITION_PROFILE")
    object_id = object_profile.get("object_profile_id")
    grasp_id = grasp_profile.get("grasp_profile_id")
    if (
        not isinstance(object_id, str) or SAFE_ID.fullmatch(object_id) is None
        or not isinstance(grasp_id, str) or SAFE_ID.fullmatch(grasp_id) is None
        or grasp_profile.get("object_profile_id") != object_id
        or grasp_profile.get("object_profile_digest") is not None
        and grasp_profile["object_profile_digest"]
        != canonical_digest(object_profile)
    ):
        raise ContractError("OBJECT_REPOSITION_PROFILE")
    if (yaw_sampling_profile is None) != (yaw_sample_binding is None):
        raise ContractError("OBJECT_REPOSITION_YAW_BINDING")
    yaw_profile = (
        None if yaw_sampling_profile is None
        else validate_yaw_sampling_profile(
            yaw_sampling_profile,
            object_profile=object_profile,
            grasp_profile=grasp_profile,
        )
    )
    yaw_binding = (
        None if yaw_sample_binding is None
        else validate_yaw_sample_binding(
            yaw_sample_binding, profile=yaw_profile,
        )
    )
    if yaw_binding is not None and abs(
        normalize_yaw_deg(yaw_binding["source_object_yaw_deg"])
        - target["yaw_deg"]
    ) > 1e-9:
        raise ContractError("OBJECT_REPOSITION_YAW_BINDING")
    value = {
        "schema_version": SCHEMA,
        "parent_run_id": parent_run_id,
        "continuation_run_id": continuation_run_id,
        "next_run_id": next_run_id,
        "execution_stage": EXECUTION_STAGES[start_state],
        "recording_scope": "OUT_OF_DATASET",
        "start_state": start_state,
        "source_pose": source,
        "target_pose": target,
        "yaw_sample_binding": copy.deepcopy(yaw_binding),
        "yaw_sample_binding_digest": (
            None if yaw_binding is None else yaw_binding["binding_digest"]
        ),
        "object_profile_id": object_id,
        "object_profile_digest": canonical_digest(object_profile),
        "grasp_profile_id": grasp_id,
        "grasp_profile_digest": canonical_digest(grasp_profile),
        "yaw_sampling_profile_id": (
            None if yaw_profile is None
            else yaw_profile["yaw_sampling_profile_id"]
        ),
        "yaw_sampling_profile_digest": (
            None if yaw_profile is None else yaw_profile["profile_digest"]
        ),
        "motion_recipe": "DIRECT",
        "recorder_authorized": False,
        "dataset_write_authorized": False,
    }
    value["binding_digest"] = canonical_digest(value)
    return value


def validate_object_reposition_binding(
    value: Mapping[str, Any], *,
    object_profile: Mapping[str, Any] | None = None,
    grasp_profile: Mapping[str, Any] | None = None,
    yaw_sampling_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise ContractError("OBJECT_REPOSITION_SCHEMA")
    result = copy.deepcopy(dict(value))
    for field in (
        "parent_run_id", "continuation_run_id", "object_profile_id",
        "grasp_profile_id",
    ):
        item = result.get(field)
        if (
            not isinstance(item, str) or len(item) > 128
            or SAFE_ID.fullmatch(item) is None
        ):
            raise ContractError("OBJECT_REPOSITION_SCHEMA")
    next_run_id = result.get("next_run_id")
    if next_run_id is not None and (
        not isinstance(next_run_id, str) or len(next_run_id) > 128
        or SAFE_ID.fullmatch(next_run_id) is None
    ):
        raise ContractError("OBJECT_REPOSITION_SCHEMA")
    if (
        result.get("schema_version") != SCHEMA
        or result.get("start_state") not in START_STATES
        or result.get("execution_stage")
        != EXECUTION_STAGES.get(result.get("start_state"))
        or result.get("recording_scope") != "OUT_OF_DATASET"
        or result.get("motion_recipe") != "DIRECT"
        or result.get("recorder_authorized") is not False
        or result.get("dataset_write_authorized") is not False
        or any(
            not isinstance(result.get(field), str)
            or DIGEST.fullmatch(result[field]) is None
            for field in (
                "object_profile_digest", "grasp_profile_digest",
                "binding_digest",
            )
        )
        or result["start_state"] == "HELD_OBJECT"
        and result["continuation_run_id"] != result["parent_run_id"]
        or result["start_state"] == "ON_SURFACE"
        and (
            result["continuation_run_id"] == result["parent_run_id"]
            or next_run_id is None
            or result["continuation_run_id"] == next_run_id
        )
    ):
        raise ContractError("OBJECT_REPOSITION_BINDING")
    source, target = _pose(result.get("source_pose")), _pose(
        result.get("target_pose"),
    )
    if result["source_pose"] != source or result["target_pose"] != target:
        raise ContractError("OBJECT_REPOSITION_POSE")
    if result["start_state"] == "ON_SURFACE" and (
        not _same_surface_position(source, target)
        or _same_yaw(source["yaw_deg"], target["yaw_deg"])
    ):
        raise ContractError("OBJECT_REPOSITION_ON_SURFACE_SCOPE")
    yaw_binding = result.get("yaw_sample_binding")
    yaw_profile_id = result.get("yaw_sampling_profile_id")
    yaw_profile_digest = result.get("yaw_sampling_profile_digest")
    yaw_metadata = (
        result.get("yaw_sample_binding_digest"), yaw_profile_id,
        yaw_profile_digest,
    )
    if yaw_binding is None and any(item is not None for item in yaw_metadata):
        raise ContractError("OBJECT_REPOSITION_YAW_BINDING")
    if yaw_binding is not None and any(item is None for item in yaw_metadata):
        raise ContractError("OBJECT_REPOSITION_YAW_BINDING")
    if yaw_binding is not None:
        checked_yaw = validate_yaw_sample_binding(
            yaw_binding, profile=yaw_sampling_profile,
        )
        if (
            result["yaw_sample_binding_digest"]
            != checked_yaw["binding_digest"]
            or yaw_profile_id != checked_yaw["yaw_sampling_profile_id"]
            or yaw_profile_digest
            != checked_yaw["yaw_sampling_profile_digest"]
            or abs(
                normalize_yaw_deg(checked_yaw["source_object_yaw_deg"])
                - target["yaw_deg"]
            ) > 1e-9
        ):
            raise ContractError("OBJECT_REPOSITION_YAW_BINDING")
    if object_profile is not None or grasp_profile is not None:
        if object_profile is None or grasp_profile is None:
            raise ContractError("OBJECT_REPOSITION_PROFILE")
        if (
            result["object_profile_id"]
            != object_profile.get("object_profile_id")
            or result["object_profile_digest"]
            != canonical_digest(object_profile)
            or result["grasp_profile_id"]
            != grasp_profile.get("grasp_profile_id")
            or result["grasp_profile_digest"]
            != canonical_digest(grasp_profile)
            or grasp_profile.get("object_profile_id")
            != result["object_profile_id"]
        ):
            raise ContractError("OBJECT_REPOSITION_PROFILE")
    if yaw_sampling_profile is not None:
        if object_profile is None or grasp_profile is None:
            raise ContractError("OBJECT_REPOSITION_PROFILE")
        checked_profile = validate_yaw_sampling_profile(
            yaw_sampling_profile,
            object_profile=object_profile,
            grasp_profile=grasp_profile,
        )
        if (
            yaw_profile_id != checked_profile["yaw_sampling_profile_id"]
            or yaw_profile_digest != checked_profile["profile_digest"]
        ):
            raise ContractError("OBJECT_REPOSITION_YAW_BINDING")
    if result["binding_digest"] != canonical_digest({
        key: item for key, item in result.items() if key != "binding_digest"
    }):
        raise ContractError("OBJECT_REPOSITION_BINDING")
    return result


__all__ = [
    "EXECUTION_STAGES", "SCHEMA", "START_STATES",
    "build_object_reposition_binding",
    "validate_object_reposition_binding", "yaw_preserving_destination",
]
