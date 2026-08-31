"""Pure workspace bounds and progressive rectangular stratification."""
from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence


def _number(value: object, name: str, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or positive and value <= 0
    ):
        raise ValueError(name)
    return float(value)


def _pair(value: object, name: str, *, positive: bool = False) -> tuple[float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise ValueError(name)
    return tuple(_number(item, name, positive=positive) for item in value)


def rotate_xy(point: Sequence[float], yaw_deg: float) -> tuple[float, float]:
    """Rotate one planar point without attaching it to a robot frame."""
    x, y = _pair(point, "point")
    angle = math.radians(_number(yaw_deg, "yaw_deg"))
    return (
        math.cos(angle) * x - math.sin(angle) * y,
        math.sin(angle) * x + math.cos(angle) * y,
    )


def safe_rectangle_bounds(
    *, page_size_mm: Sequence[float], origin_xy_mm: Sequence[float],
    base_margin_xy_mm: Sequence[float], object_size_xy_mm: Sequence[float],
    uncertainty_mm: float, yaw_deg: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return object-center bounds inside a rectangular workspace."""
    width, height = _pair(page_size_mm, "page_size_mm", positive=True)
    origin_x, origin_y = _pair(origin_xy_mm, "origin_xy_mm")
    base_x, base_y = _pair(base_margin_xy_mm, "base_margin_xy_mm")
    object_x, object_y = _pair(object_size_xy_mm, "object_size_xy_mm")
    uncertainty = _number(uncertainty_mm, "uncertainty_mm")
    if (
        not 0 <= origin_x <= width or not 0 <= origin_y <= height
        or min(base_x, base_y, object_x, object_y, uncertainty) < 0
    ):
        raise ValueError("rectangle")
    angle = math.radians(_number(yaw_deg, "yaw_deg"))
    cosine, sine = abs(math.cos(angle)), abs(math.sin(angle))
    footprint_x = (cosine * object_x + sine * object_y) / 2.0
    footprint_y = (sine * object_x + cosine * object_y) / 2.0
    margin_x = max(base_x, footprint_x + uncertainty)
    margin_y = max(base_y, footprint_y + uncertainty)
    bounds = (
        (margin_x - origin_x, width - margin_x - origin_x),
        (margin_y - origin_y, height - margin_y - origin_y),
    )
    if any(low >= high for low, high in bounds):
        raise ValueError("rectangle")
    return bounds


def rotation_envelope(
    x_bounds: Sequence[float], y_bounds: Sequence[float],
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return a symmetric local-coordinate envelope for a rotated rectangle."""
    x_low, x_high = _pair(x_bounds, "x_bounds")
    y_low, y_high = _pair(y_bounds, "y_bounds")
    if x_low >= x_high or y_low >= y_high:
        raise ValueError("bounds")
    radius = max(
        math.hypot(x, y)
        for x in (x_low, x_high) for y in (y_low, y_high)
    )
    return ((-radius, radius), (-radius, radius))


def _fraction(*parts: object) -> float:
    payload = "\0".join(str(part) for part in parts).encode()
    return int(hashlib.sha256(payload).hexdigest()[:16], 16) / 2**64


def progressive_farthest_order(
    centers: Sequence[Sequence[float]], *, start_xy: Sequence[float], seed: int,
) -> list[int]:
    """Order arbitrary region centers so every short prefix spreads spatially."""
    start = _pair(start_xy, "start_xy")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed")
    checked = [_pair(center, "center") for center in centers]
    if len(set(checked)) != len(checked):
        raise ValueError("centers")
    remaining, selected, current, result = set(range(len(checked))), [start], start, []
    while remaining:
        index = min(remaining, key=lambda candidate: (
            -min(math.dist(checked[candidate], point) for point in selected),
            math.dist(current, checked[candidate]),
            _fraction(seed, candidate, "order"),
            candidate,
        ))
        remaining.remove(index)
        result.append(index)
        current = checked[index]
        selected.append(current)
    return result


def stratified_rectangle_samples(
    *, x_bounds: Sequence[float], y_bounds: Sequence[float], columns: int,
    rows: int, start_xy: Sequence[float], count: int, seed: int,
    pass_index: int = 0, skip_start_cell: bool = False,
) -> list[tuple[float, float, int, int]]:
    """Sample distinct rectangle strata in progressive spatial order."""
    x_low, x_high = _pair(x_bounds, "x_bounds")
    y_low, y_high = _pair(y_bounds, "y_bounds")
    start_x, start_y = _pair(start_xy, "start_xy")
    if (
        x_low >= x_high or y_low >= y_high
        or isinstance(columns, bool) or not isinstance(columns, int) or columns < 1
        or isinstance(rows, bool) or not isinstance(rows, int) or rows < 1
        or isinstance(count, bool) or not isinstance(count, int) or count < 0
        or isinstance(seed, bool) or not isinstance(seed, int) or seed < 0
        or isinstance(pass_index, bool) or not isinstance(pass_index, int) or pass_index < 0
        or type(skip_start_cell) is not bool
    ):
        raise ValueError("stratified_rectangle")
    x_step, y_step = (x_high - x_low) / columns, (y_high - y_low) / rows
    cells = [
        (
            x_low + column * x_step, x_low + (column + 1) * x_step,
            y_low + row * y_step, y_low + (row + 1) * y_step,
            row, column,
        )
        for row in range(rows) for column in range(columns)
    ]
    centers = [((left + right) / 2.0, (bottom + top) / 2.0) for left, right, bottom, top, _row, _column in cells]
    available = set(range(len(cells)))
    if skip_start_cell:
        if not x_low <= start_x <= x_high or not y_low <= start_y <= y_high:
            raise ValueError("start_xy")
        column = min(int((start_x - x_low) / x_step), columns - 1)
        row = min(int((start_y - y_low) / y_step), rows - 1)
        available.remove(row * columns + column)
    if count > len(available):
        raise ValueError("count")
    order = [
        index for index in progressive_farthest_order(
            centers, start_xy=(start_x, start_y), seed=seed + pass_index,
        )
        if index in available
    ][:count]
    result = []
    for index in order:
        left, right, bottom, top, row, column = cells[index]
        result.append((
            left + (right - left) * _fraction(seed, pass_index, row, column, "x"),
            bottom + (top - bottom) * _fraction(seed, pass_index, row, column, "y"),
            row, column,
        ))
    return result
