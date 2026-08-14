#!/usr/bin/env python3
"""Generate an A4 place/yaw sheet plus the matching robot-readable JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from html import escape
from pathlib import Path


PAGE_W_MM = 297.0
PAGE_H_MM = 210.0
PLACE0_XY_MM = (148.5, 105.0)
X_REF_XY_MM = (277.0, 105.0)
Y_CHECK_XY_MM = (20.0, 185.0)
SCHEMA_VERSION = "a4_place_yaw.v2"
NOMINAL_SCALE_BAR_MM = 100.0


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
        if not (15 <= sheet_x <= PAGE_W_MM - 15 and 20 <= sheet_y <= PAGE_H_MM - 20):
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
            "first_install": "measure CENTER and X_REF; verify Y_CHECK",
            "fixed_sheet_swap": "verify CENTER translation; reuse orientation only if the physical sheet locator is unchanged",
        },
        "transform_contract": {
            "yaw0_place": "T_C_place_yaw0 = Trans(u,v,0)",
            "robot_pose": "T_base_place_yaw = T_base_C * Rz(yaw_deg) * T_C_place_yaw0",
            "position": "p_sheet(place,yaw) = C + Rz(yaw_deg) * [u,v]",
            "object_pose": "T_base_object = T_base_place_yaw * T_place_object_datum",
        },
        "grid_points": grid_points,
    }
    manifest["a4_family_digest"] = family_digest_from_manifest(manifest)
    return manifest


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
    for invalid_measurement in (96.001, 1e-10):
        try:
            print_calibration(invalid_measurement)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid measurements must not share an output identity")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yaw-deg", nargs="+", type=float, required=True)
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

    for yaw_deg in args.yaw_deg:
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
            try:
                from reportlab.graphics import renderPDF
                from svglib.svglib import svg2rlg
            except ImportError as error:
                raise SystemExit("--pdf requires installed svglib and reportlab") from error
            pdf_path = pdf_dir / f"{stem}.pdf"
            renderPDF.drawToFile(svg2rlg(str(svg_path)), str(pdf_path))
            print(pdf_path)


if __name__ == "__main__":
    main()
