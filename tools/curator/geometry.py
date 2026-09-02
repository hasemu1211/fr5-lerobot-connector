"""Strict profile/LabelMe parsing and planar task-view geometry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Sequence

import cv2
import numpy as np

from tools.curator.contracts import (
    DIGEST,
    SAFE_ID,
    CuratorError,
    canonical_digest,
    exact_fields,
    file_sha256,
    finite_number,
    load_json,
    reject_symlink_components,
)
from tools.curator.up_view import MAX_BACKGROUND_PLATE_FRAMES


PROFILE_REQUEST_SCHEMA = "curator.up_view_profile_request.v1"
LABELME_VERSION = "7.0.4"
LAYOUT_SCHEMA = "a4_workspace_region_layout.v1"
BINDING_SCHEMA = "data_factory.workspace_region_binding.v1"
CAMERA_KEY = "observation.images.up"
PLACES = ("PLACE_A", "PLACE_B")
CORNER_NAMES = ("TL", "TR", "BR", "BL")
TABLE_LABEL = "TABLE_WORK_SURFACE"
MOTION_LABEL = "visual_motion_support"
GROUNDING_LABEL = "grounding_context_support"
_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}\Z")
_CANONICAL_BINDING_ROOT = (
    Path(__file__).resolve().parents[2] / "config" / "data_factory" / "region_bindings"
).resolve()

_REQUEST_FIELDS = {
    "schema_version",
    "profile_id",
    "camera_key",
    "width",
    "height",
    "collection_camera_profile_digest",
    "layout_manifest",
    "layout_manifest_digest",
    "physical_region_binding",
    "physical_region_binding_digest",
    "labelme_annotation",
    "labelme_version",
    "reference_image",
    "reference_image_sha256",
    "reference_frame_index",
    "background_plate_frame_indices",
    "dilation_margin_px",
    "review_bundle",
    "approval_artifact",
}


@dataclass(frozen=True)
class ProfileRequest:
    path: Path
    value: dict[str, Any]
    layout_path: Path
    binding_path: Path
    annotation_path: Path
    reference_image_path: Path
    review_bundle_path: Path
    approval_path: Path


def _path(base: Path, value: object, code: str, *, must_exist: bool) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CuratorError(code, "path string required")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    reject_symlink_components(candidate, code)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as exc:
        raise CuratorError(code, f"{candidate}: {exc}") from exc
    if must_exist and not resolved.is_file():
        raise CuratorError(code, f"regular file required: {resolved}")
    if not must_exist and (resolved.parent.is_symlink() or not resolved.parent.is_dir()):
        raise CuratorError(code, f"existing regular parent required: {resolved.parent}")
    return resolved


def load_profile_request(path: str | Path) -> ProfileRequest:
    reject_symlink_components(path, "PROFILE_PATH")
    try:
        source = Path(path).resolve(strict=True)
    except OSError as exc:
        raise CuratorError("PROFILE_PATH", str(exc)) from exc
    value = exact_fields(load_json(source, code="PROFILE_JSON"), _REQUEST_FIELDS, "PROFILE_FIELDS")
    if value["schema_version"] != PROFILE_REQUEST_SCHEMA:
        raise CuratorError("PROFILE_SCHEMA")
    if not isinstance(value["profile_id"], str) or SAFE_ID.fullmatch(value["profile_id"]) is None:
        raise CuratorError("PROFILE_ID")
    if value["camera_key"] != CAMERA_KEY:
        raise CuratorError("PROFILE_CAMERA_KEY")
    for name in ("width", "height"):
        if type(value[name]) is not int or not 1 <= value[name] <= 16_384:
            raise CuratorError("PROFILE_IMAGE_SIZE", name)
    for name in (
        "collection_camera_profile_digest",
        "layout_manifest_digest",
        "physical_region_binding_digest",
        "reference_image_sha256",
    ):
        if not isinstance(value[name], str) or DIGEST.fullmatch(value[name]) is None:
            raise CuratorError("PROFILE_DIGEST", name)
    if value["labelme_version"] != LABELME_VERSION:
        raise CuratorError("PROFILE_LABELME_VERSION")
    if type(value["reference_frame_index"]) is not int or value["reference_frame_index"] < 0:
        raise CuratorError("PROFILE_REFERENCE_INDEX")
    indices = value["background_plate_frame_indices"]
    if (
        not isinstance(indices, list)
        or not indices
        or len(indices) > MAX_BACKGROUND_PLATE_FRAMES
        or any(type(index) is not int or index < 0 for index in indices)
        or indices != sorted(set(indices))
    ):
        raise CuratorError("PROFILE_PLATE_INDICES")
    if type(value["dilation_margin_px"]) is not int or not 0 <= value["dilation_margin_px"] <= 256:
        raise CuratorError("PROFILE_MARGIN")

    base = source.parent
    layout = _path(base, value["layout_manifest"], "PROFILE_LAYOUT_PATH", must_exist=True)
    binding = _path(base, value["physical_region_binding"], "PROFILE_BINDING_PATH", must_exist=True)
    annotation = _path(base, value["labelme_annotation"], "PROFILE_LABELME_PATH", must_exist=True)
    reference = _path(base, value["reference_image"], "PROFILE_REFERENCE_PATH", must_exist=True)
    bundle = _path(base, value["review_bundle"], "PROFILE_BUNDLE_PATH", must_exist=False)
    approval = _path(base, value["approval_artifact"], "PROFILE_APPROVAL_PATH", must_exist=False)
    if bundle == approval or bundle in approval.parents or approval.parent != bundle.parent:
        raise CuratorError("PROFILE_ARTIFACT_OVERLAP", "approval must be outside immutable review bundle")
    if file_sha256(reference) != value["reference_image_sha256"]:
        raise CuratorError("PROFILE_REFERENCE_DIGEST")
    layout_value = parse_layout(layout)
    if layout_value["layout_digest"] != value["layout_manifest_digest"]:
        raise CuratorError("PROFILE_LAYOUT_DIGEST")
    binding_value = parse_binding(binding, layout_value)
    if binding_value["binding_digest"] != value["physical_region_binding_digest"]:
        raise CuratorError("PROFILE_BINDING_DIGEST")
    if binding_value["physical_binding_status"] == "VERIFIED":
        try:
            binding.relative_to(_CANONICAL_BINDING_ROOT)
        except ValueError as exc:
            raise CuratorError(
                "VERIFIED_BINDING_NOT_CANONICAL",
                f"VERIFIED bindings must come from {_CANONICAL_BINDING_ROOT}",
            ) from exc
    return ProfileRequest(
        source,
        value,
        layout,
        binding,
        annotation,
        reference,
        bundle,
        approval,
    )


def _point(value: object, code: str) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
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
    a: Sequence[float], b: Sequence[float], c: Sequence[float], d: Sequence[float],
) -> bool:
    first = (_orientation(a, b, c), _orientation(a, b, d))
    second = (_orientation(c, d, a), _orientation(c, d, b))
    return first[0] * first[1] <= 0 and second[0] * second[1] <= 0


def _simple_polygon(value: object, code: str, *, positive_winding: bool = False) -> list[list[float]]:
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
        _orientation(polygon[index - 1], polygon[index], polygon[(index + 1) % len(polygon)])
        for index in range(len(polygon))
    ]
    if any(cross <= 1e-9 for cross in crosses):
        raise CuratorError(code, "strictly convex polygon required")
    return polygon


def parse_layout(path: str | Path) -> dict[str, Any]:
    fields = {
        "schema_version", "layout_id", "page_mm", "origin_xy_mm",
        "workspace_regions", "layout_digest",
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
        or value["layout_digest"] != canonical_digest({key: item for key, item in value.items() if key != "layout_digest"})
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
        "schema_version", "layout_id", "layout_digest", "physical_binding_status",
        "bindings", "verified_at", "verified_by", "evidence_digest", "binding_digest",
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
        or value["binding_digest"] != canonical_digest({key: item for key, item in value.items() if key != "binding_digest"})
    ):
        raise CuratorError("BINDING_CONTRACT")
    regions = {region["place_id"]: region["region_id"] for region in layout["workspace_regions"]}
    bindings = value["bindings"]
    if not isinstance(bindings, list) or len(bindings) != len(PLACES):
        raise CuratorError("BINDING_ENDPOINTS")
    seen: set[str] = set()
    for endpoint in bindings:
        exact_fields(endpoint, {"place_id", "frame_id", "region_id"}, "BINDING_ENDPOINT_FIELDS")
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


def _image_polygon(value: object, width: int, height: int, code: str) -> list[list[float]]:
    polygon = _simple_polygon(value, code)
    if any(not 0 <= point[0] < width or not 0 <= point[1] < height for point in polygon):
        raise CuratorError(code, "image coordinate out of bounds")
    return polygon


def _corner_quad(corners: dict[str, list[float]], place: str) -> None:
    polygon = [corners[name] for name in CORNER_NAMES]
    if len({tuple(point) for point in polygon}) != 4:
        raise CuratorError("LABELME_CORNER_ORDER", place)
    _convex_polygon(polygon, "LABELME_CORNER_ORDER")


def parse_labelme(request: ProfileRequest) -> dict[str, Any]:
    fields = {"version", "flags", "shapes", "imagePath", "imageData", "imageHeight", "imageWidth"}
    value = exact_fields(load_json(request.annotation_path, code="LABELME_JSON"), fields, "LABELME_FIELDS")
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
        TABLE_LABEL: [], MOTION_LABEL: [], GROUNDING_LABEL: [],
    }
    corners: dict[str, dict[str, list[float]]] = {place: {} for place in PLACES}
    base_fields = {"label", "points", "group_id", "shape_type", "flags"}
    optional_fields = {"description", "mask"}
    for shape in value["shapes"]:
        if not isinstance(shape, dict) or not base_fields <= set(shape) or set(shape) - base_fields - optional_fields:
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
            if not 0 <= point[0] < request.value["width"] or not 0 <= point[1] < request.value["height"]:
                raise CuratorError("LABELME_POINT_BOUNDS", label)
            place, corner = label.rsplit("_", 1)
            if corner in corners[place]:
                raise CuratorError("LABELME_DUPLICATE", label)
            corners[place][corner] = point
        else:
            polygons[label].append(
                _image_polygon(shape["points"], request.value["width"], request.value["height"], "LABELME_POLYGON")
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
    layout: dict[str, Any], correspondence: dict[str, dict[str, list[float]]],
) -> dict[str, list[list[float]]]:
    width = float(layout["page_mm"]["width"])
    height = float(layout["page_mm"]["height"])
    origin = np.asarray(layout["origin_xy_mm"], dtype=np.float64)
    page = np.asarray([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32)
    regions = {region["place_id"]: region for region in layout["workspace_regions"]}
    projected: dict[str, list[list[float]]] = {}
    for place in PLACES:
        image = np.asarray([correspondence[place][name] for name in CORNER_NAMES], dtype=np.float32)
        homography = cv2.getPerspectiveTransform(page, image)
        if not np.isfinite(homography).all() or abs(float(np.linalg.det(homography))) <= 1e-12:
            raise CuratorError("HOMOGRAPHY_DEGENERATE", place)
        local = np.asarray(regions[place]["polygon_local_xy_mm"], dtype=np.float64)
        page_polygon = (local + origin).astype(np.float32).reshape(1, -1, 2)
        output = cv2.perspectiveTransform(page_polygon, homography)[0]
        if not np.isfinite(output).all():
            raise CuratorError("HOMOGRAPHY_PROJECTION", place)
        projected[place] = [[round(float(x), 6), round(float(y), 6)] for x, y in output]
    return projected


def resolve_geometry(request: ProfileRequest) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    layout = parse_layout(request.layout_path)
    binding = parse_binding(request.binding_path, layout)
    annotation = parse_labelme(request)
    semantic = project_semantic_subregions(layout, annotation["place_plane_correspondence"])
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


def build_keep_mask(geometry: dict[str, Any], width: int, height: int, margin_px: int) -> np.ndarray:
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
        raise CuratorError("KEEP_MASK_COVERAGE", "mask must contain keep and replace pixels")
    return keep.astype(bool)


def geometry_digests(geometry: dict[str, Any]) -> dict[str, str]:
    return {
        "place_plane_correspondence_digest": canonical_digest(geometry["place_plane_correspondence"]),
        "table_work_surface_digest": canonical_digest(geometry["table_work_surface"]),
        "visual_motion_support_digest": canonical_digest(geometry["visual_motion_support"]),
        "grounding_context_support_digest": canonical_digest(geometry["grounding_context_support"]),
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
    "PROFILE_REQUEST_SCHEMA",
    "ProfileRequest",
    "TABLE_LABEL",
    "build_keep_mask",
    "geometry_digests",
    "load_profile_request",
    "parse_binding",
    "parse_labelme",
    "parse_layout",
    "project_semantic_subregions",
    "resolve_geometry",
]
