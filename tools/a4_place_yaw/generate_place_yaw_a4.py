#!/usr/bin/env python3
"""Generate an A4 place/yaw sheet plus the matching robot-readable JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from html import escape
from pathlib import Path


PAGE_W_MM = 297.0
PAGE_H_MM = 210.0
PLACE0_XY_MM = (148.5, 105.0)
X_REF_XY_MM = (277.0, 105.0)
Y_CHECK_XY_MM = (20.0, 185.0)
PRINT_X_MARGIN_MM = 15.0
PRINT_Y_MARGIN_MM = 20.0
REGISTRATION_FIRST_INSTALL = "measure CENTER and X_REF; verify Y_CHECK"
REGISTRATION_FIXED_SHEET_SWAP = "verify CENTER translation; reuse orientation only if the physical sheet locator is unchanged"
TRANSFORM_CONTRACT = {
    "yaw0_place": "T_C_place_yaw0 = Trans(u,v,0)",
    "robot_pose": "T_base_place_yaw = T_base_C * Rz(yaw_deg) * T_C_place_yaw0",
    "position": "p_sheet(place,yaw) = C + Rz(yaw_deg) * [u,v]",
    "object_pose": "T_base_object = T_base_place_yaw * T_place_object_datum",
}
SCHEMA_VERSION = "a4_place_yaw.v2"
NOMINAL_SCALE_BAR_MM = 100.0
REGION_LAYOUT_SCHEMA = "a4_region_layout.v1"
REGION_LAYOUT_ID = "a4-red-blue-r001"


def rotate(u: float, v: float, yaw_deg: float) -> tuple[float, float]:
    yaw = math.radians(yaw_deg)
    return (
        math.cos(yaw) * u - math.sin(yaw) * v,
        math.sin(yaw) * u + math.cos(yaw) * v,
    )


def sheet_to_svg(x: float, y: float) -> tuple[float, float]:
    return x, PAGE_H_MM - y


def fmt(value: float) -> str:
    rounded = round(value, 3)
    return str(int(rounded)) if rounded.is_integer() else f"{rounded:.3f}".rstrip("0")


def yaw_tag(yaw_deg: float) -> str:
    sign = "P" if yaw_deg >= 0 else "M"
    magnitude = f"{abs(yaw_deg):06.2f}".replace(".", "_")
    return f"{sign}{magnitude}"


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    if not cleaned:
        raise ValueError("sheet prefix must contain at least one safe character")
    return cleaned


def canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def print_calibration(measured_scale_mm: float) -> dict:
    if not math.isfinite(measured_scale_mm) or measured_scale_mm <= 0:
        raise ValueError("--measured-scale-mm must be a positive finite number")
    canonical_measurement = round(measured_scale_mm, 2)
    if canonical_measurement <= 0:
        raise ValueError("--measured-scale-mm must be at least 0.01")
    if not math.isclose(measured_scale_mm, canonical_measurement, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("--measured-scale-mm supports at most two decimal places")
    return {
        "nominal_scale_bar_mm": NOMINAL_SCALE_BAR_MM,
        "measured_scale_bar_mm": canonical_measurement,
        "content_scale_percent": round(NOMINAL_SCALE_BAR_MM / canonical_measurement * 100.0, 6),
    }


def family_digest_from_manifest(manifest: dict) -> str:
    """Digest the yaw-invariant page, registration, and local-grid contract."""
    registration = manifest["registration"]
    family = {
        "schema_version": manifest["schema_version"],
        "place_id": manifest["place_id"],
        "page_mm": manifest["page_mm"],
        "place_spacing_mm": manifest["place_spacing_mm"],
        "print_calibration": manifest["print_calibration"],
        "registration_sheet_xy_mm": {
            "origin": registration["origin"]["sheet_xy_mm"],
            "x_ref": registration["x_ref"]["sheet_xy_mm"],
            "verify": registration["verify"]["sheet_xy_mm"],
        },
        "grid_local_uv_mm": [
            {"point_id": point["point_id"], "local_uv_mm": point["local_uv_mm"]}
            for point in manifest["grid_points"]
        ],
    }
    return canonical_digest(family)


def build_places(cols: int, rows: int, spacing_mm: float, yaw_deg: float) -> list[dict]:
    if cols < 1 or rows < 1 or cols % 2 == 0 or rows % 2 == 0:
        raise ValueError("--cols and --rows must be positive odd numbers so CENTER is central")
    if spacing_mm <= 0:
        raise ValueError("--spacing-mm must be positive")

    offsets = []
    for row in range(rows):
        v = (rows // 2 - row) * spacing_mm
        for col in range(cols):
            u = (col - cols // 2) * spacing_mm
            if u or v:
                offsets.append((u, v))

    places = [("CENTER", 0.0, 0.0)]
    places.extend((f"GRID_{index}", u, v) for index, (u, v) in enumerate(offsets, 1))

    result = []
    for point_id, u, v in places:
        dx, dy = rotate(u, v, yaw_deg)
        sheet_x = PLACE0_XY_MM[0] + dx
        sheet_y = PLACE0_XY_MM[1] + dy
        if not (PRINT_X_MARGIN_MM <= sheet_x <= PAGE_W_MM - PRINT_X_MARGIN_MM and PRINT_Y_MARGIN_MM <= sheet_y <= PAGE_H_MM - PRINT_Y_MARGIN_MM):
            raise ValueError(
                f"{point_id} leaves the printable workspace at yaw={yaw_deg}: "
                f"({sheet_x:.1f}, {sheet_y:.1f}) mm; reduce rows/cols/spacing"
            )
        result.append(
            {
                "point_id": point_id,
                "local_uv_mm": [round(u, 3), round(v, 3)],
                "relative_pose_place0": {
                    "x_mm": round(dx, 3),
                    "y_mm": round(dy, 3),
                    "yaw_deg": round(yaw_deg, 3),
                },
                "sheet_xy_mm": [round(sheet_x, 3), round(sheet_y, 3)],
            }
        )
    return result


def svg_text(x: float, y: float, text: str, size: float, **attrs: str) -> str:
    attributes = " ".join(f'{key.replace("_", "-")}="{escape(value)}"' for key, value in attrs.items())
    return f'<text x="{fmt(x)}" y="{fmt(y)}" font-size="{fmt(size)}" {attributes}>{escape(text)}</text>'


def make_svg(
    sheet_id: str,
    yaw_deg: float,
    places: list[dict],
    spacing_mm: float,
    measured_scale_mm: float = NOMINAL_SCALE_BAR_MM,
) -> str:
    calibration = print_calibration(measured_scale_mm)
    content_scale = calibration["content_scale_percent"] / 100.0
    yaw_label = f"{yaw_deg:+.1f}°"
    print_label = (
        "PRINT 100% / FIT OFF"
        if measured_scale_mm == NOMINAL_SCALE_BAR_MM
        else f"PRINT CAL {measured_scale_mm:.2f} -> {NOMINAL_SCALE_BAR_MM:.0f} mm / CONTENT {calibration['content_scale_percent']:.3f}%"
    )
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{PAGE_W_MM}mm" height="{PAGE_H_MM}mm" viewBox="0 0 {PAGE_W_MM} {PAGE_H_MM}">',
        '<defs><clipPath id="workspace"><rect x="12" y="20" width="273" height="170"/></clipPath></defs>',
        '<rect width="297" height="210" fill="white"/>',
        f'<g transform="translate({fmt(PAGE_W_MM / 2)} {fmt(PAGE_H_MM / 2)}) scale({content_scale:.9f}) translate(-{fmt(PAGE_W_MM / 2)} -{fmt(PAGE_H_MM / 2)})">',
        '<rect x="3" y="3" width="291" height="204" fill="none" stroke="#111" stroke-width="0.6"/>',
        '<rect x="7" y="5" width="283" height="13" fill="#111"/>',
        svg_text(11, 14, "A4 PLACE / YAW BOARD", 6.3, fill="white", font_weight="700"),
        svg_text(286, 13.5, f"{sheet_id}   YAW {yaw_label}", 4.2, fill="white", font_weight="700", text_anchor="end"),
        svg_text(10, 202, "PLACE OBJECT DATUM ON MARK / ALIGN FRONT WITH RED GUIDE", 3.3, fill="#111", font_weight="700"),
        svg_text(287, 202, print_label, 3.3, fill="#8a5500", font_weight="700", text_anchor="end"),
    ]

    max_extent = 260.0
    grid_count = int(max_extent // spacing_mm) + 1
    for index in range(-grid_count, grid_count + 1):
        coordinate = index * spacing_mm
        major = index == 0
        color = "#777" if major else "#d5d5d5"
        width = 0.55 if major else 0.22
        for p1, p2 in (((coordinate, -max_extent), (coordinate, max_extent)), ((-max_extent, coordinate), (max_extent, coordinate))):
            dx1, dy1 = rotate(*p1, yaw_deg)
            dx2, dy2 = rotate(*p2, yaw_deg)
            x1, y1 = sheet_to_svg(PLACE0_XY_MM[0] + dx1, PLACE0_XY_MM[1] + dy1)
            x2, y2 = sheet_to_svg(PLACE0_XY_MM[0] + dx2, PLACE0_XY_MM[1] + dy2)
            lines.append(
                f'<line x1="{fmt(x1)}" y1="{fmt(y1)}" x2="{fmt(x2)}" y2="{fmt(y2)}" '
                f'stroke="{color}" stroke-width="{fmt(width)}" clip-path="url(#workspace)"/>'
            )

    for place in places:
        x, y = sheet_to_svg(*place["sheet_xy_mm"])
        u, v = place["local_uv_mm"]
        dx_tick, dy_tick = rotate(7.0, 0.0, yaw_deg)
        tick_x1, tick_y1 = sheet_to_svg(place["sheet_xy_mm"][0] - dx_tick, place["sheet_xy_mm"][1] - dy_tick)
        tick_x2, tick_y2 = sheet_to_svg(place["sheet_xy_mm"][0] + dx_tick, place["sheet_xy_mm"][1] + dy_tick)
        is_origin = place["point_id"] == "CENTER"
        radius = 4.0 if is_origin else 3.0
        stroke = "#6a3d9a" if is_origin else "#111"
        fill = "#f0e5ff" if is_origin else "white"
        lines.extend(
            [
                f'<circle cx="{fmt(x)}" cy="{fmt(y)}" r="{fmt(radius)}" fill="{fill}" stroke="{stroke}" stroke-width="0.75"/>',
                f'<line x1="{fmt(tick_x1)}" y1="{fmt(tick_y1)}" x2="{fmt(tick_x2)}" y2="{fmt(tick_y2)}" stroke="#b85450" stroke-width="0.9"/>',
                svg_text(x, y - radius - 1.5, "C" if is_origin else place["point_id"].replace("GRID_", "G"), 2.8, fill=stroke, font_weight="700", text_anchor="middle"),
                svg_text(x, y + radius + 3.2, f"({fmt(u)},{fmt(v)})", 2.3, fill="#333", text_anchor="middle"),
            ]
        )

    p0x, p0y = sheet_to_svg(*PLACE0_XY_MM)
    xrx, xry = sheet_to_svg(*X_REF_XY_MM)
    ycx, ycy = sheet_to_svg(*Y_CHECK_XY_MM)
    lines.extend(
        [
            f'<line x1="{fmt(p0x)}" y1="{fmt(p0y)}" x2="{fmt(xrx)}" y2="{fmt(xry)}" stroke="#1a5fb4" stroke-width="0.5" stroke-dasharray="2 1"/>',
            f'<circle cx="{fmt(xrx)}" cy="{fmt(xry)}" r="3.8" fill="white" stroke="#1a5fb4" stroke-width="0.8"/>',
            svg_text(xrx - 1, xry - 5.5, "X-REF", 3.0, fill="#0b3d77", font_weight="700", text_anchor="middle"),
            f'<circle cx="{fmt(ycx)}" cy="{fmt(ycy)}" r="3.8" fill="white" stroke="#2d7d46" stroke-width="0.8"/>',
            svg_text(ycx + 5, ycy + 1, "Y-CHECK", 3.0, fill="#1d5a31", font_weight="700"),
            svg_text(148.5, 24, f"CENTER-ROTATED PLACE GRID · FIXED YAW {yaw_label}", 4.0, fill="#7a1f1f", font_weight="700", text_anchor="middle"),
            '<line x1="20" y1="196" x2="120" y2="196" stroke="#111" stroke-width="0.9"/>',
            '<line x1="20" y1="193.5" x2="20" y2="198.5" stroke="#111" stroke-width="0.6"/>',
            '<line x1="120" y1="193.5" x2="120" y2="198.5" stroke="#111" stroke-width="0.6"/>',
            svg_text(70, 192, "100 mm SCALE CHECK", 2.8, fill="#111", font_weight="700", text_anchor="middle"),
            svg_text(287, 196, "REGISTER: CENTER + X-REF  /  VERIFY: Y-CHECK", 2.9, fill="#111", font_weight="700", text_anchor="end"),
            "</g>",
            "</svg>",
        ]
    )
    return "\n".join(lines)


def make_manifest(
    place_id: str,
    sheet_id: str,
    yaw_deg: float,
    places: list[dict],
    spacing_mm: float,
    measured_scale_mm: float = NOMINAL_SCALE_BAR_MM,
) -> dict:
    grid_points = [
        {
            **place,
            "job_pose": {
                "place_id": place_id,
                "yaw_deg": round(yaw_deg, 3),
                "x_mm": place["local_uv_mm"][0],
                "y_mm": place["local_uv_mm"][1],
            },
        }
        for place in places
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "place_id": place_id,
        "sheet_id": sheet_id,
        "page_mm": {"width": PAGE_W_MM, "height": PAGE_H_MM},
        "yaw_deg": round(yaw_deg, 3),
        "place_spacing_mm": spacing_mm,
        "print_calibration": print_calibration(measured_scale_mm),
        "registration": {
            "origin": {"id": "CENTER", "sheet_xy_mm": list(PLACE0_XY_MM)},
            "x_ref": {"id": "X_REF", "sheet_xy_mm": list(X_REF_XY_MM)},
            "verify": {"id": "Y_CHECK", "sheet_xy_mm": list(Y_CHECK_XY_MM)},
            "first_install": REGISTRATION_FIRST_INSTALL,
            "fixed_sheet_swap": REGISTRATION_FIXED_SHEET_SWAP,
        },
        "transform_contract": dict(TRANSFORM_CONTRACT),
        "grid_points": grid_points,
    }
    manifest["a4_family_digest"] = family_digest_from_manifest(manifest)
    return manifest


def make_red_blue_region_layout() -> dict:
    """Return the workspace-independent A4-local red/blue overlay geometry."""
    x_min = PRINT_X_MARGIN_MM - PLACE0_XY_MM[0]
    x_max = PAGE_W_MM - PRINT_X_MARGIN_MM - PLACE0_XY_MM[0]
    y_min = PRINT_Y_MARGIN_MM - PLACE0_XY_MM[1]
    y_max = PAGE_H_MM - PRINT_Y_MARGIN_MM - PLACE0_XY_MM[1]
    layout = {
        "schema_version": REGION_LAYOUT_SCHEMA,
        "layout_id": REGION_LAYOUT_ID,
        "page_mm": {"width": PAGE_W_MM, "height": PAGE_H_MM},
        "origin_xy_mm": list(PLACE0_XY_MM),
        "regions": [
            {
                "region_id": "RED", "display_name": "RED", "color": "#C62828",
                "polygon_local_xy_mm": [
                    [x_min, y_min], [0.0, y_min], [0.0, y_max], [x_min, y_max],
                ],
            },
            {
                "region_id": "BLUE", "display_name": "BLUE", "color": "#1565C0",
                "polygon_local_xy_mm": [
                    [0.0, y_min], [x_max, y_min], [x_max, y_max], [0.0, y_max],
                ],
            },
        ],
    }
    layout["layout_digest"] = canonical_digest(layout)
    return validate_region_layout(layout)


def _signed_area(polygon: list[list[float]]) -> float:
    return 0.5 * sum(
        left[0] * right[1] - right[0] * left[1]
        for left, right in zip(polygon, polygon[1:] + polygon[:1])
    )


def _segments_intersect(
    left_a: list[float], left_b: list[float], right_a: list[float], right_b: list[float],
) -> bool:
    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    values = (
        cross(left_a, left_b, right_a), cross(left_a, left_b, right_b),
        cross(right_a, right_b, left_a), cross(right_a, right_b, left_b),
    )
    if values[0] * values[1] < 0 and values[2] * values[3] < 0:
        return True
    for value, point, start, end in (
        (values[0], right_a, left_a, left_b),
        (values[1], right_b, left_a, left_b),
        (values[2], left_a, right_a, right_b),
        (values[3], left_b, right_a, right_b),
    ):
        if abs(value) < 1e-9 and all(
            min(start[axis], end[axis]) <= point[axis] <= max(start[axis], end[axis])
            for axis in (0, 1)
        ):
            return True
    return False


def validate_region_layout(value: object) -> dict:
    """Validate the one bounded visual layout; this grants no motion eligibility."""
    fields = {
        "schema_version", "layout_id", "page_mm", "origin_xy_mm", "regions",
        "layout_digest",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("REGION_LAYOUT_FIELDS")
    layout = json.loads(json.dumps(value, allow_nan=False))
    if (
        layout["schema_version"] != REGION_LAYOUT_SCHEMA
        or layout["layout_id"] != REGION_LAYOUT_ID
        or layout["page_mm"] != {"width": PAGE_W_MM, "height": PAGE_H_MM}
        or layout["origin_xy_mm"] != list(PLACE0_XY_MM)
        or not isinstance(layout["regions"], list)
        or [item.get("region_id") for item in layout["regions"]] != ["RED", "BLUE"]
        or layout["layout_digest"] != canonical_digest({
            key: item for key, item in layout.items() if key != "layout_digest"
        })
    ):
        raise ValueError("REGION_LAYOUT_CONTRACT")
    boxes = []
    for region in layout["regions"]:
        if (
            not isinstance(region, dict)
            or set(region) != {
                "region_id", "display_name", "color", "polygon_local_xy_mm",
            }
            or region["display_name"] != region["region_id"]
            or re.fullmatch(r"#[0-9A-F]{6}", region["color"]) is None
            or not isinstance(region["polygon_local_xy_mm"], list)
            or len(region["polygon_local_xy_mm"]) < 3
        ):
            raise ValueError("REGION_LAYOUT_REGION")
        polygon = region["polygon_local_xy_mm"]
        if any(
            not isinstance(point, list) or len(point) != 2
            or any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) for item in point)
            for point in polygon
        ) or len({tuple(point) for point in polygon}) != len(polygon):
            raise ValueError("REGION_LAYOUT_POLYGON")
        if _signed_area(polygon) <= 0:
            raise ValueError("REGION_LAYOUT_WINDING")
        if any(
            not 0 <= PLACE0_XY_MM[0] + point[0] <= PAGE_W_MM
            or not 0 <= PLACE0_XY_MM[1] + point[1] <= PAGE_H_MM
            for point in polygon
        ):
            raise ValueError("REGION_LAYOUT_PAGE_BOUNDS")
        edges = list(zip(polygon, polygon[1:] + polygon[:1]))
        for left_index, left in enumerate(edges):
            for right_index, right in enumerate(edges):
                if right_index <= left_index + 1 or {left_index, right_index} == {0, len(edges) - 1}:
                    continue
                if _segments_intersect(*left, *right):
                    raise ValueError("REGION_LAYOUT_SELF_INTERSECTION")
        boxes.append((
            min(point[0] for point in polygon), max(point[0] for point in polygon),
            min(point[1] for point in polygon), max(point[1] for point in polygon),
        ))
    if (
        boxes != [
            (PRINT_X_MARGIN_MM - PLACE0_XY_MM[0], 0.0,
             PRINT_Y_MARGIN_MM - PLACE0_XY_MM[1], PAGE_H_MM - PRINT_Y_MARGIN_MM - PLACE0_XY_MM[1]),
            (0.0, PAGE_W_MM - PRINT_X_MARGIN_MM - PLACE0_XY_MM[0],
             PRINT_Y_MARGIN_MM - PLACE0_XY_MM[1], PAGE_H_MM - PRINT_Y_MARGIN_MM - PLACE0_XY_MM[1]),
        ]
        or min(boxes[0][1], boxes[1][1]) > max(boxes[0][0], boxes[1][0])
        and min(boxes[0][3], boxes[1][3]) > max(boxes[0][2], boxes[1][2])
    ):
        raise ValueError("REGION_LAYOUT_OVERLAP")
    return layout


def make_region_svg(
    layout: dict, measured_scale_mm: float = NOMINAL_SCALE_BAR_MM,
) -> str:
    layout = validate_region_layout(layout)
    calibration = print_calibration(measured_scale_mm)
    content_scale = calibration["content_scale_percent"] / 100.0
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{PAGE_W_MM}mm" height="{PAGE_H_MM}mm" viewBox="0 0 {PAGE_W_MM} {PAGE_H_MM}" data-measured-scale-mm="{fmt(measured_scale_mm)}" data-content-scale-percent="{fmt(calibration["content_scale_percent"])}">',
        '<rect width="297" height="210" fill="white"/>',
        f'<g transform="translate({fmt(PAGE_W_MM / 2)} {fmt(PAGE_H_MM / 2)}) scale({content_scale:.9f}) translate(-{fmt(PAGE_W_MM / 2)} -{fmt(PAGE_H_MM / 2)})">',
    ]
    for region in layout["regions"]:
        sheet = [
            (PLACE0_XY_MM[0] + point[0], PLACE0_XY_MM[1] + point[1])
            for point in region["polygon_local_xy_mm"]
        ]
        svg_points = " ".join(
            f"{fmt(x)},{fmt(PAGE_H_MM - y)}" for x, y in sheet
        )
        center_x = sum(point[0] for point in sheet) / len(sheet)
        center_y = PAGE_H_MM - sum(point[1] for point in sheet) / len(sheet)
        lines.extend([
            f'<polygon points="{svg_points}" fill="{region["color"]}" fill-opacity="0.13" stroke="{region["color"]}" stroke-width="1.2"/>',
            svg_text(
                center_x, center_y + 4, region["display_name"], 12,
                fill=region["color"], font_weight="700", text_anchor="middle",
            ),
        ])
    lines.extend(["</g>", "</svg>"])
    return "\n".join(lines)


def render_pdf(svg_path: Path, pdf_path: Path) -> None:
    """Render one SVG with an already-installed converter."""
    try:
        from reportlab.graphics import renderPDF
        from svglib.svglib import svg2rlg
    except ImportError:
        converter = shutil.which("libreoffice")
        if converter is None:
            raise SystemExit("--pdf requires svglib/reportlab or libreoffice")
        with tempfile.TemporaryDirectory(prefix="a4-place-yaw-pdf-") as directory:
            subprocess.run(
                [
                    converter, "--headless", "--convert-to", "pdf",
                    "--outdir", directory, str(svg_path),
                ],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=30,
            )
            generated = Path(directory) / f"{svg_path.stem}.pdf"
            if not generated.is_file():
                raise SystemExit("PDF converter did not produce an output")
            shutil.copy2(generated, pdf_path)
        return
    renderPDF.drawToFile(svg2rlg(str(svg_path)), str(pdf_path))


def self_check() -> None:
    assert rotate(10, 0, 0) == (10.0, 0.0)
    x, y = rotate(10, 0, 90)
    assert abs(x) < 1e-9 and abs(y - 10) < 1e-9
    places_0 = build_places(3, 3, 20, 0)
    places_30 = build_places(3, 3, 20, 30)
    assert places_0[0]["sheet_xy_mm"] == list(PLACE0_XY_MM)
    assert places_30[0]["sheet_xy_mm"] == list(PLACE0_XY_MM)
    assert places_0[1]["sheet_xy_mm"] != places_30[1]["sheet_xy_mm"]
    d0 = math.dist(places_0[1]["sheet_xy_mm"], PLACE0_XY_MM)
    d30 = math.dist(places_30[1]["sheet_xy_mm"], PLACE0_XY_MM)
    assert abs(d0 - d30) < 1e-3
    manifest = make_manifest("PLACE_A", "PLACE_A_YAW_P030_00", 30, places_30, 20)
    assert manifest["grid_points"][1]["job_pose"] == {
        "place_id": "PLACE_A",
        "yaw_deg": 30,
        "x_mm": places_30[1]["local_uv_mm"][0],
        "y_mm": places_30[1]["local_uv_mm"][1],
    }
    manifest_0 = make_manifest("PLACE_A", "PLACE_A_YAW_P000_00", 0, places_0, 20)
    assert manifest["a4_family_digest"] == manifest_0["a4_family_digest"]
    compensated = make_manifest("PLACE_A", "PLACE_A_YAW_P000_00_PRINTCAL_096_00MM", 0, places_0, 20, 96)
    assert compensated["print_calibration"]["content_scale_percent"] == 104.166667
    assert compensated["a4_family_digest"] != manifest_0["a4_family_digest"]
    assert print_calibration(100)["content_scale_percent"] == 100.0
    region_layout = make_red_blue_region_layout()
    assert validate_region_layout(region_layout) == region_layout
    assert 'data-content-scale-percent="104.167"' in make_region_svg(
        region_layout, 96,
    )
    for invalid_measurement in (96.001, 1e-10):
        try:
            print_calibration(invalid_measurement)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid measurements must not share an output identity")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yaw-deg", nargs="+", type=float)
    parser.add_argument(
        "--red-blue-zone", action="store_true",
        help="also write the bounded A4-local RED/BLUE layout artifacts",
    )
    parser.add_argument("--place-id", default="PLACE_A")
    parser.add_argument("--cols", type=int, default=5)
    parser.add_argument("--rows", type=int, default=3)
    parser.add_argument("--spacing-mm", type=float, default=35.0)
    parser.add_argument(
        "--measured-scale-mm",
        type=float,
        default=NOMINAL_SCALE_BAR_MM,
        help="measured length of this generator's 100 mm scale bar; compensated outputs are isolated automatically",
    )
    parser.add_argument("--pdf", action="store_true", help="also render A4 PDF (requires svglib and reportlab)")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    if not args.yaw_deg and not args.red_blue_zone:
        parser.error("provide --yaw-deg or --red-blue-zone")

    self_check()
    measured_scale_mm = print_calibration(args.measured_scale_mm)["measured_scale_bar_mm"]
    is_compensated = measured_scale_mm != NOMINAL_SCALE_BAR_MM
    measurement_tag = f"{measured_scale_mm:06.2f}".replace(".", "_")
    output_root = (
        args.output_dir / "print_calibration" / f"scale_bar_{measurement_tag}mm"
        if is_compensated
        else args.output_dir
    )
    output_root.mkdir(parents=True, exist_ok=True)
    svg_dir = output_root / "svg"
    json_dir = output_root / "json"
    pdf_dir = output_root / "pdf"
    svg_dir.mkdir(exist_ok=True)
    json_dir.mkdir(exist_ok=True)
    pdf_dir.mkdir(exist_ok=True)
    place_id = safe_id(args.place_id)

    for yaw_deg in args.yaw_deg or []:
        tag = yaw_tag(yaw_deg)
        suffix = f"_PRINTCAL_{measurement_tag}MM" if is_compensated else ""
        sheet_id = f"{place_id}_YAW_{tag}{suffix}"
        places = build_places(args.cols, args.rows, args.spacing_mm, yaw_deg)
        stem = sheet_id.lower()
        svg_path = svg_dir / f"{stem}.svg"
        json_path = json_dir / f"{stem}.json"
        svg_path.write_text(
            make_svg(sheet_id, yaw_deg, places, args.spacing_mm, measured_scale_mm),
            encoding="utf-8",
        )
        json_path.write_text(
            json.dumps(
                make_manifest(place_id, sheet_id, yaw_deg, places, args.spacing_mm, measured_scale_mm),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(svg_path)
        print(json_path)
        if args.pdf:
            pdf_path = pdf_dir / f"{stem}.pdf"
            render_pdf(svg_path, pdf_path)
            print(pdf_path)

    if args.red_blue_zone:
        zone_dir = args.output_dir / "zone_artifacts"
        zone_dir.mkdir(parents=True, exist_ok=True)
        suffix = f"_printcal_{measurement_tag}mm" if is_compensated else ""
        stem = f"a4_red_blue_r001{suffix}"
        layout = make_red_blue_region_layout()
        zone_json = zone_dir / f"{stem}.json"
        zone_svg = zone_dir / f"{stem}.svg"
        zone_json.write_text(
            json.dumps(layout, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        zone_svg.write_text(
            make_region_svg(layout, measured_scale_mm), encoding="utf-8",
        )
        print(zone_json)
        print(zone_svg)
        if args.pdf:
            zone_pdf = zone_dir / f"{stem}.pdf"
            render_pdf(zone_svg, zone_pdf)
            print(zone_pdf)


if __name__ == "__main__":
    main()
