"""Persistent workspace/frame-to-region binding without motion authority."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path

from tools.a4_place_yaw.region_layout import (
    WORKSPACE_REGIONS,
    make_red_blue_region_layout,
    validate_region_layout,
    workspace_region,
)
from tools.fr5_data_factory import (
    ContractError,
    DIGEST,
    RFC3339,
    SAFE_ID,
    canonical_digest,
    load_json_strict,
)


REGION_BINDING_SCHEMA = "data_factory.workspace_region_binding.v1"
DEFAULT_REGION_BINDING = Path(
    "config/data_factory/region_bindings/place-a-red-place-b-blue-r002.json"
)


def validate_workspace_region_binding(
    value: Mapping[str, object], layout: Mapping[str, object],
) -> dict:
    """Validate persisted physical semantics; this grants no motion authority."""
    fields = {
        "schema_version", "layout_id", "layout_digest",
        "physical_binding_status", "bindings", "verified_at", "verified_by",
        "evidence_digest", "binding_digest",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError("WORKSPACE_REGION_BINDING_FIELDS")
    result = copy.deepcopy(dict(value))
    checked_layout = validate_region_layout(layout)
    status = result.get("physical_binding_status")
    bindings = result.get("bindings")
    if (
        result.get("schema_version") != REGION_BINDING_SCHEMA
        or result.get("layout_id") != checked_layout["layout_id"]
        or result.get("layout_digest") != checked_layout["layout_digest"]
        or status not in {"PREPARED_NOT_VERIFIED", "VERIFIED"}
        or not isinstance(bindings, list)
        or len(bindings) != len(WORKSPACE_REGIONS)
        or result.get("binding_digest") != canonical_digest({
            key: item for key, item in result.items() if key != "binding_digest"
        })
    ):
        raise ContractError("WORKSPACE_REGION_BINDING_CONTRACT")
    expected = [(place_id, region_id) for place_id, region_id, _color in WORKSPACE_REGIONS]
    for item, (place_id, region_id) in zip(bindings, expected):
        if (
            not isinstance(item, Mapping)
            or set(item) != {"place_id", "frame_id", "region_id"}
            or (item.get("place_id"), item.get("region_id"))
            != (place_id, region_id)
            or not isinstance(item.get("frame_id"), str)
            or SAFE_ID.fullmatch(item["frame_id"]) is None
        ):
            raise ContractError("WORKSPACE_REGION_BINDING_ENDPOINT")
    verified_fields = (
        result.get("verified_at"), result.get("verified_by"),
        result.get("evidence_digest"),
    )
    if status == "PREPARED_NOT_VERIFIED":
        if any(item is not None for item in verified_fields):
            raise ContractError("WORKSPACE_REGION_BINDING_EVIDENCE")
    elif (
        not isinstance(verified_fields[0], str)
        or RFC3339.fullmatch(verified_fields[0]) is None
        or not isinstance(verified_fields[1], str)
        or SAFE_ID.fullmatch(verified_fields[1]) is None
        or not isinstance(verified_fields[2], str)
        or DIGEST.fullmatch(verified_fields[2]) is None
    ):
        raise ContractError("WORKSPACE_REGION_BINDING_EVIDENCE")
    return result


def load_workspace_region_binding(
    repository: str | Path, layout: Mapping[str, object],
) -> dict | None:
    """Load the optional exact binding; absence leaves regions unverified."""
    path = Path(repository).resolve(strict=True) / DEFAULT_REGION_BINDING
    if not path.exists():
        return None
    try:
        return validate_workspace_region_binding(load_json_strict(path), layout)
    except OSError as exc:
        raise ContractError("WORKSPACE_REGION_BINDING_IO") from exc


def validate_region_endpoint_authority(
    repository: str | Path, *, place_id: str, frame_id: str,
    region_binding: Mapping[str, object],
) -> dict:
    """Require a VERIFIED language claim to match the persisted physical binding."""
    fields = {
        "layout_id", "layout_digest", "region_id", "physical_binding_status",
    }
    if (
        not isinstance(region_binding, Mapping)
        or set(region_binding) != fields
        or not isinstance(place_id, str)
        or SAFE_ID.fullmatch(place_id) is None
        or not isinstance(frame_id, str)
        or SAFE_ID.fullmatch(frame_id) is None
    ):
        raise ContractError("WORKSPACE_REGION_AUTHORITY")
    claimed = copy.deepcopy(dict(region_binding))
    if claimed["physical_binding_status"] != "VERIFIED":
        return claimed
    try:
        layout = make_red_blue_region_layout()
        zone = workspace_region(layout, place_id)
        persisted = load_workspace_region_binding(repository, layout)
    except (ValueError, ContractError) as exc:
        raise ContractError("WORKSPACE_REGION_AUTHORITY") from exc
    endpoints = {
        item["place_id"]: item
        for item in (
            [] if persisted is None else persisted["bindings"]
        )
    }
    endpoint = endpoints.get(place_id)
    if (
        persisted is None
        or persisted["physical_binding_status"] != "VERIFIED"
        or endpoint != {
            "place_id": place_id,
            "frame_id": frame_id,
            "region_id": zone["region_id"],
        }
        or claimed != {
            "layout_id": layout["layout_id"],
            "layout_digest": layout["layout_digest"],
            "region_id": zone["region_id"],
            "physical_binding_status": "VERIFIED",
        }
    ):
        raise ContractError("WORKSPACE_REGION_AUTHORITY")
    return claimed


__all__ = [
    "DEFAULT_REGION_BINDING", "REGION_BINDING_SCHEMA",
    "load_workspace_region_binding", "validate_region_endpoint_authority",
    "validate_workspace_region_binding",
]
