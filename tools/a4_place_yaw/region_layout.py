"""Pure A4 workspace-region contract shared by print and planning projections."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence


PAGE_W_MM = 297.0
PAGE_H_MM = 210.0
PLACE0_XY_MM = (148.5, 105.0)
PRINT_X_MARGIN_MM = 15.0
PRINT_Y_MARGIN_MM = 20.0
REGION_LAYOUT_SCHEMA = "a4_workspace_region_layout.v1"
REGION_LAYOUT_ID = "a4-place-a-red-place-b-blue-r002"
REGION_LAYOUT_REVISION = "r002"
WORKSPACE_REGIONS = (
    ("PLACE_A", "RED", "#C62828"),
    ("PLACE_B", "BLUE", "#1565C0"),
)


def _digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _signed_area(polygon: Sequence[Sequence[float]]) -> float:
    return 0.5 * sum(
        left[0] * right[1] - right[0] * left[1]
        for left, right in zip(polygon, (*polygon[1:], polygon[0]))
    )


def _validate_convex_polygon(value: object) -> list[list[float]]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) < 3
    ):
        raise ValueError("REGION_LAYOUT_POLYGON")
    polygon = []
    for point in value:
        if (
            not isinstance(point, Sequence)
            or isinstance(point, (str, bytes))
            or len(point) != 2
            or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(item)
                for item in point
            )
        ):
            raise ValueError("REGION_LAYOUT_POLYGON")
        polygon.append([float(point[0]), float(point[1])])
    if len({tuple(point) for point in polygon}) != len(polygon):
        raise ValueError("REGION_LAYOUT_POLYGON")
    if _signed_area(polygon) <= 0:
        raise ValueError("REGION_LAYOUT_WINDING")
    crosses = []
    for index, point in enumerate(polygon):
        previous = polygon[index - 1]
        following = polygon[(index + 1) % len(polygon)]
        crosses.append(
            (point[0] - previous[0]) * (following[1] - point[1])
            - (point[1] - previous[1]) * (following[0] - point[0])
        )
    if any(value <= 1e-9 for value in crosses):
        raise ValueError("REGION_LAYOUT_CONVEX")
    if any(
        not 0 <= PLACE0_XY_MM[0] + point[0] <= PAGE_W_MM
        or not 0 <= PLACE0_XY_MM[1] + point[1] <= PAGE_H_MM
        for point in polygon
    ):
        raise ValueError("REGION_LAYOUT_PAGE_BOUNDS")
    return polygon


def a4_printable_polygon() -> list[list[float]]:
    """Return the common A4-local printable polygon without region semantics."""
    return [
        [PRINT_X_MARGIN_MM - PLACE0_XY_MM[0], PRINT_Y_MARGIN_MM - PLACE0_XY_MM[1]],
        [PAGE_W_MM - PRINT_X_MARGIN_MM - PLACE0_XY_MM[0], PRINT_Y_MARGIN_MM - PLACE0_XY_MM[1]],
        [PAGE_W_MM - PRINT_X_MARGIN_MM - PLACE0_XY_MM[0], PAGE_H_MM - PRINT_Y_MARGIN_MM - PLACE0_XY_MM[1]],
        [PRINT_X_MARGIN_MM - PLACE0_XY_MM[0], PAGE_H_MM - PRINT_Y_MARGIN_MM - PLACE0_XY_MM[1]],
    ]


def make_red_blue_region_layout() -> dict:
    """Return PLACE_A=RED and PLACE_B=BLUE as independent full-sheet zones."""
    polygon = a4_printable_polygon()
    result = {
        "schema_version": REGION_LAYOUT_SCHEMA,
        "layout_id": REGION_LAYOUT_ID,
        "page_mm": {"width": PAGE_W_MM, "height": PAGE_H_MM},
        "origin_xy_mm": list(PLACE0_XY_MM),
        "workspace_regions": [
            {
                "place_id": place_id,
                "region_id": region_id,
                "display_name": region_id,
                "color": color,
                "polygon_local_xy_mm": copy.deepcopy(polygon),
            }
            for place_id, region_id, color in WORKSPACE_REGIONS
        ],
    }
    result["layout_digest"] = _digest(result)
    return validate_region_layout(result)


def validate_region_layout(value: object) -> dict:
    """Validate identity and convex polygons without granting motion authority."""
    fields = {
        "schema_version", "layout_id", "page_mm", "origin_xy_mm",
        "workspace_regions", "layout_digest",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("REGION_LAYOUT_FIELDS")
    layout = copy.deepcopy(dict(value))
    if (
        layout["schema_version"] != REGION_LAYOUT_SCHEMA
        or layout["layout_id"] != REGION_LAYOUT_ID
        or layout["page_mm"] != {"width": PAGE_W_MM, "height": PAGE_H_MM}
        or layout["origin_xy_mm"] != list(PLACE0_XY_MM)
        or not isinstance(layout["workspace_regions"], list)
        or [
            (item.get("place_id"), item.get("region_id"))
            for item in layout["workspace_regions"]
            if isinstance(item, Mapping)
        ] != [(place_id, region_id) for place_id, region_id, _color in WORKSPACE_REGIONS]
        or layout["layout_digest"] != _digest({
            key: item for key, item in layout.items() if key != "layout_digest"
        })
    ):
        raise ValueError("REGION_LAYOUT_CONTRACT")
    for region, (place_id, region_id, color) in zip(
        layout["workspace_regions"], WORKSPACE_REGIONS,
    ):
        if (
            not isinstance(region, Mapping)
            or set(region) != {
                "place_id", "region_id", "display_name", "color",
                "polygon_local_xy_mm",
            }
            or (
                region["place_id"], region["region_id"],
                region["display_name"], region["color"],
            ) != (place_id, region_id, region_id, color)
        ):
            raise ValueError("REGION_LAYOUT_REGION")
        _validate_convex_polygon(region["polygon_local_xy_mm"])
    return layout


def workspace_region(layout: object, place_id: str) -> dict:
    """Resolve one exact workspace region from a validated layout."""
    checked = validate_region_layout(layout)
    matches = [
        region for region in checked["workspace_regions"]
        if region["place_id"] == place_id
    ]
    if len(matches) != 1:
        raise ValueError("REGION_LAYOUT_PLACE")
    return copy.deepcopy(matches[0])


__all__ = [
    "PAGE_H_MM", "PAGE_W_MM", "PLACE0_XY_MM", "PRINT_X_MARGIN_MM",
    "PRINT_Y_MARGIN_MM", "REGION_LAYOUT_ID", "REGION_LAYOUT_REVISION",
    "WORKSPACE_REGIONS", "a4_printable_polygon", "make_red_blue_region_layout",
    "validate_region_layout", "workspace_region",
]
