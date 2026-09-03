"""Strict profile/LabelMe parsing and planar task-view geometry."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Sequence

import cv2
import numpy as np

from tools.data_factory.curator.core.jsonio import (
    DIGEST,
    SAFE_ID,
    canonical_digest,
    exact_fields,
    finite_number,
    load_json,
)
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.profile.schema import (
    CAMERA_KEY,
    LABELME_VERSION,
    ViewProfileSpec,
)


LAYOUT_SCHEMA = "a4_workspace_region_layout.v1"
BINDING_SCHEMA = "data_factory.workspace_region_binding.v1"
PLACES = ("PLACE_A", "PLACE_B")
CORNER_NAMES = ("TL", "TR", "BR", "BL")
TABLE_LABEL = "TABLE_WORK_SURFACE"
MOTION_LABEL = "visual_motion_support"
GROUNDING_LABEL = "grounding_context_support"
_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}\Z")


def _point(value: object, code: str) -> list[float]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 2
    ):
        raise CuratorError(code, "two coordinates required")
    return [finite_number(value[0], code), finite_number(value[1], code)]


def _signed_area(points: Sequence[Sequence[float]]) -> float:
    return 0.5 * sum(
        left[0] * right[1] - right[0] * left[1]
        for left, right in zip(points, (*points[1:], points[0]))
    )


def _orientation(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(
    a: Sequence[float],
    b: Sequence[float],
    c: Sequence[float],
    d: Sequence[float],
) -> bool:
    first = (_orientation(a, b, c), _orientation(a, b, d))
    second = (_orientation(c, d, a), _orientation(c, d, b))
    return first[0] * first[1] <= 0 and second[0] * second[1] <= 0


def _simple_polygon(
    value: object, code: str, *, positive_winding: bool = False
) -> list[list[float]]:
    if not isinstance(value, list) or len(value) < 3:
        raise CuratorError(code, "polygon requires at least three points")
    polygon = [_point(point, code) for point in value]
    if len({tuple(point) for point in polygon}) != len(polygon):
        raise CuratorError(code, "duplicate polygon point")
    area = _signed_area(polygon)
    if abs(area) <= 1e-6 or positive_winding and area <= 0:
        raise CuratorError(code, "degenerate or flipped polygon")
    count = len(polygon)
    for left in range(count):
        a, b = polygon[left], polygon[(left + 1) % count]
        for right in range(left + 1, count):
            if right in {left, (left + 1) % count} or (right + 1) % count == left:
                continue
            c, d = polygon[right], polygon[(right + 1) % count]
            if _segments_intersect(a, b, c, d):
                raise CuratorError(code, "self-intersecting polygon")
    return polygon


def _convex_polygon(value: object, code: str) -> list[list[float]]:
    polygon = _simple_polygon(value, code, positive_winding=True)
    crosses = [
        _orientation(
            polygon[index - 1], polygon[index], polygon[(index + 1) % len(polygon)]
        )
        for index in range(len(polygon))
    ]
    if any(cross <= 1e-9 for cross in crosses):
        raise CuratorError(code, "strictly convex polygon required")
    return polygon


def parse_layout(path: str | Path) -> dict[str, Any]:
    fields = {
        "schema_version",
        "layout_id",
        "page_mm",
        "origin_xy_mm",
        "workspace_regions",
        "layout_digest",
    }
    value = exact_fields(load_json(path, code="LAYOUT_JSON"), fields, "LAYOUT_FIELDS")
    page = exact_fields(value["page_mm"], {"width", "height"}, "LAYOUT_PAGE_FIELDS")
    width = finite_number(page["width"], "LAYOUT_PAGE")
    height = finite_number(page["height"], "LAYOUT_PAGE")
    origin = _point(value["origin_xy_mm"], "LAYOUT_ORIGIN")
    if (
        value["schema_version"] != LAYOUT_SCHEMA
        or not isinstance(value["layout_id"], str)
        or SAFE_ID.fullmatch(value["layout_id"]) is None
        or width <= 0
        or height <= 0
        or not 0 <= origin[0] <= width
        or not 0 <= origin[1] <= height
        or not isinstance(value["layout_digest"], str)
        or DIGEST.fullmatch(value["layout_digest"]) is None
        or value["layout_digest"]
        != canonical_digest(
            {key: item for key, item in value.items() if key != "layout_digest"}
        )
    ):
        raise CuratorError("LAYOUT_CONTRACT")
    regions = value["workspace_regions"]
    if not isinstance(regions, list) or len(regions) != len(PLACES):
        raise CuratorError("LAYOUT_REGIONS")
    by_place: dict[str, dict[str, Any]] = {}
    for region in regions:
        exact_fields(
            region,
            {"place_id", "region_id", "display_name", "color", "polygon_local_xy_mm"},
            "LAYOUT_REGION_FIELDS",
        )
        place = region["place_id"]
        if (
            place not in PLACES
            or place in by_place
            or not isinstance(region["region_id"], str)
            or SAFE_ID.fullmatch(region["region_id"]) is None
            or not isinstance(region["display_name"], str)
            or not region["display_name"].isprintable()
            or not isinstance(region["color"], str)
            or _HEX_COLOR.fullmatch(region["color"]) is None
        ):
            raise CuratorError("LAYOUT_REGION")
        polygon = _convex_polygon(region["polygon_local_xy_mm"], "LAYOUT_POLYGON")
        if any(
            not 0 <= origin[0] + point[0] <= width
            or not 0 <= origin[1] + point[1] <= height
            for point in polygon
        ):
            raise CuratorError("LAYOUT_PAGE_BOUNDS", place)
        by_place[place] = region
    if set(by_place) != set(PLACES):
        raise CuratorError("LAYOUT_PLACES")
    return value


def parse_binding(path: str | Path, layout: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "schema_version",
        "layout_id",
        "layout_digest",
        "physical_binding_status",
        "bindings",
        "verified_at",
        "verified_by",
        "evidence_digest",
        "binding_digest",
    }
    value = exact_fields(load_json(path, code="BINDING_JSON"), fields, "BINDING_FIELDS")
    status = value["physical_binding_status"]
    if (
        value["schema_version"] != BINDING_SCHEMA
        or value["layout_id"] != layout["layout_id"]
        or value["layout_digest"] != layout["layout_digest"]
        or status not in {"PREPARED_NOT_VERIFIED", "VERIFIED"}
        or not isinstance(value["binding_digest"], str)
        or DIGEST.fullmatch(value["binding_digest"]) is None
        or value["binding_digest"]
        != canonical_digest(
            {key: item for key, item in value.items() if key != "binding_digest"}
        )
    ):
        raise CuratorError("BINDING_CONTRACT")
    regions = {
        region["place_id"]: region["region_id"]
        for region in layout["workspace_regions"]
    }
    bindings = value["bindings"]
    if not isinstance(bindings, list) or len(bindings) != len(PLACES):
        raise CuratorError("BINDING_ENDPOINTS")
    seen: set[str] = set()
    for endpoint in bindings:
        exact_fields(
            endpoint, {"place_id", "frame_id", "region_id"}, "BINDING_ENDPOINT_FIELDS"
        )
        place = endpoint["place_id"]
        if (
            place not in PLACES
            or place in seen
            or endpoint["region_id"] != regions[place]
            or not isinstance(endpoint["frame_id"], str)
            or SAFE_ID.fullmatch(endpoint["frame_id"]) is None
        ):
            raise CuratorError("BINDING_ENDPOINT")
        seen.add(place)
    evidence = (value["verified_at"], value["verified_by"], value["evidence_digest"])
    if status == "PREPARED_NOT_VERIFIED":
        if any(item is not None for item in evidence):
            raise CuratorError("BINDING_EVIDENCE")
    elif (
        not isinstance(evidence[0], str)
        or _RFC3339.fullmatch(evidence[0]) is None
        or not isinstance(evidence[1], str)
        or SAFE_ID.fullmatch(evidence[1]) is None
        or not isinstance(evidence[2], str)
        or DIGEST.fullmatch(evidence[2]) is None
    ):
        raise CuratorError("BINDING_EVIDENCE")
    return value


def _image_polygon(
    value: object, width: int, height: int, code: str
) -> list[list[float]]:
    polygon = _simple_polygon(value, code)
    if any(
        not 0 <= point[0] < width or not 0 <= point[1] < height for point in polygon
    ):
        raise CuratorError(code, "image coordinate out of bounds")
    return polygon


def _corner_quad(corners: dict[str, list[float]], place: str) -> None:
    polygon = [corners[name] for name in CORNER_NAMES]
    if len({tuple(point) for point in polygon}) != 4:
        raise CuratorError("LABELME_CORNER_ORDER", place)
    _convex_polygon(polygon, "LABELME_CORNER_ORDER")


def parse_labelme(request: ViewProfileSpec) -> dict[str, Any]:
    fields = {
        "version",
        "flags",
        "shapes",
        "imagePath",
        "imageData",
        "imageHeight",
        "imageWidth",
    }
    value = exact_fields(
        load_json(request.annotation_path, code="LABELME_JSON"),
        fields,
        "LABELME_FIELDS",
    )
    if (
        value["version"] != request.value["labelme_version"]
        or value["flags"] != {}
        or value["imageData"] is not None
        or value["imageHeight"] != request.value["height"]
        or value["imageWidth"] != request.value["width"]
        or not isinstance(value["imagePath"], str)
        or Path(value["imagePath"]).name != request.reference_image_path.name
        or not isinstance(value["shapes"], list)
    ):
        raise CuratorError("LABELME_CONTRACT")
    allowed = {TABLE_LABEL, MOTION_LABEL, GROUNDING_LABEL}
    allowed.update(f"{place}_{corner}" for place in PLACES for corner in CORNER_NAMES)
    polygons: dict[str, list[list[list[float]]]] = {
        TABLE_LABEL: [],
        MOTION_LABEL: [],
        GROUNDING_LABEL: [],
    }
    corners: dict[str, dict[str, list[float]]] = {place: {} for place in PLACES}
    base_fields = {"label", "points", "group_id", "shape_type", "flags"}
    optional_fields = {"description", "mask"}
    for shape in value["shapes"]:
        if (
            not isinstance(shape, dict)
            or not base_fields <= set(shape)
            or set(shape) - base_fields - optional_fields
        ):
            raise CuratorError("LABELME_SHAPE_FIELDS")
        label = shape["label"]
        if (
            label not in allowed
            or shape["group_id"] is not None
            or shape["flags"] != {}
            or shape.get("description") not in {None, ""}
            or shape.get("mask") is not None
        ):
            raise CuratorError("LABELME_SHAPE_CONTRACT", str(label))
        is_corner = any(label.startswith(f"{place}_") for place in PLACES)
        expected_type = "point" if is_corner else "polygon"
        if shape["shape_type"] != expected_type:
            raise CuratorError("LABELME_SHAPE_TYPE", label)
        if is_corner:
            if not isinstance(shape["points"], list) or len(shape["points"]) != 1:
                raise CuratorError("LABELME_POINT", label)
            point = _point(shape["points"][0], "LABELME_POINT")
            if (
                not 0 <= point[0] < request.value["width"]
                or not 0 <= point[1] < request.value["height"]
            ):
                raise CuratorError("LABELME_POINT_BOUNDS", label)
            place, corner = label.rsplit("_", 1)
            if corner in corners[place]:
                raise CuratorError("LABELME_DUPLICATE", label)
            corners[place][corner] = point
        else:
            polygons[label].append(
                _image_polygon(
                    shape["points"],
                    request.value["width"],
                    request.value["height"],
                    "LABELME_POLYGON",
                )
            )
    if len(polygons[TABLE_LABEL]) != 1 or not polygons[MOTION_LABEL]:
        raise CuratorError("LABELME_REQUIRED_POLYGONS")
    for place in PLACES:
        if set(corners[place]) != set(CORNER_NAMES):
            raise CuratorError("LABELME_REQUIRED_CORNERS", place)
        _corner_quad(corners[place], place)
    return {
        "table_work_surface": polygons[TABLE_LABEL][0],
        "visual_motion_support": polygons[MOTION_LABEL],
        "grounding_context_support": polygons[GROUNDING_LABEL],
        "place_plane_correspondence": corners,
    }


def project_semantic_subregions(
    layout: dict[str, Any],
    correspondence: dict[str, dict[str, list[float]]],
) -> dict[str, list[list[float]]]:
    width = float(layout["page_mm"]["width"])
    height = float(layout["page_mm"]["height"])
    origin = np.asarray(layout["origin_xy_mm"], dtype=np.float64)
    page = np.asarray(
        [[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32
    )
    regions = {region["place_id"]: region for region in layout["workspace_regions"]}
    projected: dict[str, list[list[float]]] = {}
    for place in PLACES:
        image = np.asarray(
            [correspondence[place][name] for name in CORNER_NAMES], dtype=np.float32
        )
        homography = cv2.getPerspectiveTransform(page, image)
        if (
            not np.isfinite(homography).all()
            or abs(float(np.linalg.det(homography))) <= 1e-12
        ):
            raise CuratorError("HOMOGRAPHY_DEGENERATE", place)
        local = np.asarray(regions[place]["polygon_local_xy_mm"], dtype=np.float64)
        page_polygon = (local + origin).astype(np.float32).reshape(1, -1, 2)
        output = cv2.perspectiveTransform(page_polygon, homography)[0]
        if not np.isfinite(output).all():
            raise CuratorError("HOMOGRAPHY_PROJECTION", place)
        projected[place] = [[round(float(x), 6), round(float(y), 6)] for x, y in output]
    return projected


def resolve_geometry(
    request: ViewProfileSpec,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    layout = parse_layout(request.layout_path)
    binding = parse_binding(request.binding_path, layout)
    annotation = parse_labelme(request)
    semantic = project_semantic_subregions(
        layout, annotation["place_plane_correspondence"]
    )
    width, height = request.value["width"], request.value["height"]
    for place, polygon in semantic.items():
        _image_polygon(polygon, width, height, "SEMANTIC_SUBREGION_BOUNDS")
    geometry = {
        **annotation,
        "semantic_subregions": semantic,
    }
    return geometry, layout, binding


def _fill(mask: np.ndarray, polygons: Sequence[Sequence[Sequence[float]]]) -> None:
    for polygon in polygons:
        points = np.rint(np.asarray(polygon, dtype=np.float64)).astype(np.int32)
        cv2.fillPoly(mask, [points], 1)


def build_keep_mask(
    geometry: dict[str, Any], width: int, height: int, margin_px: int
) -> np.ndarray:
    """Rasterize table-wide support first; A/B remain semantic subregions."""
    table = np.zeros((height, width), dtype=np.uint8)
    _fill(table, [geometry["table_work_surface"]])
    for place, polygon in geometry["semantic_subregions"].items():
        region = np.zeros_like(table)
        _fill(region, [polygon])
        if np.any((region == 1) & (table == 0)):
            raise CuratorError("SEMANTIC_OUTSIDE_TABLE", place)
    keep = table.copy()
    _fill(keep, geometry["visual_motion_support"])
    _fill(keep, geometry["grounding_context_support"])
    if margin_px:
        size = 2 * margin_px + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
        keep = cv2.dilate(keep, kernel)
    if not keep.any() or keep.all():
        raise CuratorError(
            "KEEP_MASK_COVERAGE", "mask must contain keep and replace pixels"
        )
    return keep.astype(bool)


def geometry_digests(geometry: dict[str, Any]) -> dict[str, str]:
    return {
        "place_plane_correspondence_digest": canonical_digest(
            geometry["place_plane_correspondence"]
        ),
        "table_work_surface_digest": canonical_digest(geometry["table_work_surface"]),
        "visual_motion_support_digest": canonical_digest(
            geometry["visual_motion_support"]
        ),
        "grounding_context_support_digest": canonical_digest(
            geometry["grounding_context_support"]
        ),
        "semantic_subregions_digest": canonical_digest(geometry["semantic_subregions"]),
    }


__all__ = [
    "BINDING_SCHEMA",
    "CAMERA_KEY",
    "CORNER_NAMES",
    "GROUNDING_LABEL",
    "LABELME_VERSION",
    "LAYOUT_SCHEMA",
    "MOTION_LABEL",
    "PLACES",
    "TABLE_LABEL",
    "build_keep_mask",
    "geometry_digests",
    "parse_binding",
    "parse_labelme",
    "parse_layout",
    "project_semantic_subregions",
    "resolve_geometry",
]
