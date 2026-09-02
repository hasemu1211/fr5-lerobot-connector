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


def _convex_polygon(
    value: object, name: str = "polygon",
) -> list[tuple[float, float]]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) < 3
    ):
        raise ValueError(name)
    polygon = [_pair(point, name) for point in value]
    if len(set(polygon)) != len(polygon):
        raise ValueError(name)
    crosses = []
    for index, point in enumerate(polygon):
        previous = polygon[index - 1]
        following = polygon[(index + 1) % len(polygon)]
        crosses.append(
            (point[0] - previous[0]) * (following[1] - point[1])
            - (point[1] - previous[1]) * (following[0] - point[0])
        )
    if any(value <= 1e-9 for value in crosses):
        raise ValueError(name)
    return polygon


def polygon_bounds(
    polygon: Sequence[Sequence[float]],
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return the axis-aligned bounds of one strict CCW convex polygon."""
    checked = _convex_polygon(polygon)
    return (
        (min(point[0] for point in checked), max(point[0] for point in checked)),
        (min(point[1] for point in checked), max(point[1] for point in checked)),
    )


def point_in_convex_polygon(
    point: Sequence[float], polygon: Sequence[Sequence[float]],
) -> bool:
    """Return whether a point lies in or on one strict CCW convex polygon."""
    x, y = _pair(point, "point")
    checked = _convex_polygon(polygon)
    return all(
        (right[0] - left[0]) * (y - left[1])
        - (right[1] - left[1]) * (x - left[0]) >= -1e-9
        for left, right in zip(checked, checked[1:] + checked[:1])
    )


def safe_convex_polygon(
    *, polygon: Sequence[Sequence[float]],
    object_size_xy_mm: Sequence[float], uncertainty_mm: float, yaw_deg: float,
) -> list[tuple[float, float]]:
    """Erode a convex zone by the rotated object footprint and uncertainty."""
    checked = _convex_polygon(polygon)
    object_x, object_y = _pair(
        object_size_xy_mm, "object_size_xy_mm",
    )
    uncertainty = _number(uncertainty_mm, "uncertainty_mm")
    if min(object_x, object_y, uncertainty) < 0:
        raise ValueError("safe_polygon")
    angle = math.radians(_number(yaw_deg, "yaw_deg"))
    axis_x = (math.cos(angle), math.sin(angle))
    axis_y = (-math.sin(angle), math.cos(angle))
    shifted = []
    for left, right in zip(checked, checked[1:] + checked[:1]):
        dx, dy = right[0] - left[0], right[1] - left[1]
        length = math.hypot(dx, dy)
        inward = (-dy / length, dx / length)
        support = (
            object_x / 2.0 * abs(inward[0] * axis_x[0] + inward[1] * axis_x[1])
            + object_y / 2.0 * abs(inward[0] * axis_y[0] + inward[1] * axis_y[1])
            + uncertainty
        )
        shifted.append((
            (left[0] + inward[0] * support, left[1] + inward[1] * support),
            (dx / length, dy / length),
        ))

    def cross(left, right):
        return left[0] * right[1] - left[1] * right[0]

    result = []
    for index, (point, direction) in enumerate(shifted):
        previous_point, previous_direction = shifted[index - 1]
        denominator = cross(previous_direction, direction)
        if abs(denominator) <= 1e-12:
            raise ValueError("safe_polygon")
        delta = (
            point[0] - previous_point[0], point[1] - previous_point[1],
        )
        distance = cross(delta, direction) / denominator
        result.append((
            previous_point[0] + distance * previous_direction[0],
            previous_point[1] + distance * previous_direction[1],
        ))
    return _convex_polygon(result, "safe_polygon")


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


def _clip_axis(
    polygon: list[tuple[float, float]], *, axis: int, boundary: float,
    keep_greater: bool,
) -> list[tuple[float, float]]:
    def inside(point):
        return point[axis] >= boundary - 1e-9 if keep_greater else point[axis] <= boundary + 1e-9

    def intersection(start, end):
        distance = end[axis] - start[axis]
        if abs(distance) <= 1e-12:
            return start
        ratio = (boundary - start[axis]) / distance
        return (
            start[0] + ratio * (end[0] - start[0]),
            start[1] + ratio * (end[1] - start[1]),
        )

    result = []
    for start, end in zip(polygon[-1:] + polygon[:-1], polygon):
        start_inside, end_inside = inside(start), inside(end)
        if end_inside:
            if not start_inside:
                result.append(intersection(start, end))
            result.append(end)
        elif start_inside:
            result.append(intersection(start, end))
    deduplicated = []
    for point in result:
        if not deduplicated or math.dist(point, deduplicated[-1]) > 1e-9:
            deduplicated.append(point)
    if len(deduplicated) > 1 and math.dist(deduplicated[0], deduplicated[-1]) <= 1e-9:
        deduplicated.pop()
    return deduplicated


def _clip_box(
    polygon: list[tuple[float, float]],
    left: float, right: float, bottom: float, top: float,
) -> list[tuple[float, float]]:
    result = polygon
    for axis, boundary, keep_greater in (
        (0, left, True), (0, right, False),
        (1, bottom, True), (1, top, False),
    ):
        result = _clip_axis(
            result, axis=axis, boundary=boundary, keep_greater=keep_greater,
        )
        if len(result) < 3:
            return []
    area = abs(sum(
        left_point[0] * right_point[1] - right_point[0] * left_point[1]
        for left_point, right_point in zip(result, result[1:] + result[:1])
    )) / 2.0
    return result if area > 1e-9 else []


def stratified_convex_polygon_samples(
    *, polygon: Sequence[Sequence[float]], columns: int, rows: int,
    start_xy: Sequence[float], count: int, seed: int,
    pass_index: int = 0, skip_start_cell: bool = False,
) -> list[tuple[float, float, int, int]]:
    """Sample up to count deterministic, spread points from polygon strata."""
    checked = _convex_polygon(polygon)
    start = _pair(start_xy, "start_xy")
    if (
        isinstance(columns, bool) or not isinstance(columns, int) or columns < 1
        or isinstance(rows, bool) or not isinstance(rows, int) or rows < 1
        or isinstance(count, bool) or not isinstance(count, int) or count < 0
        or isinstance(seed, bool) or not isinstance(seed, int) or seed < 0
        or isinstance(pass_index, bool) or not isinstance(pass_index, int) or pass_index < 0
        or type(skip_start_cell) is not bool
    ):
        raise ValueError("stratified_polygon")
    (x_low, x_high), (y_low, y_high) = polygon_bounds(checked)
    x_step, y_step = (x_high - x_low) / columns, (y_high - y_low) / rows
    cells = []
    for row in range(rows):
        for column in range(columns):
            left, right = x_low + column * x_step, x_low + (column + 1) * x_step
            bottom, top = y_low + row * y_step, y_low + (row + 1) * y_step
            clipped = _clip_box(checked, left, right, bottom, top)
            if clipped:
                center = (
                    sum(point[0] for point in clipped) / len(clipped),
                    sum(point[1] for point in clipped) / len(clipped),
                )
                cells.append((left, right, bottom, top, row, column, clipped, center))
    available = set(range(len(cells)))
    if skip_start_cell:
        if not point_in_convex_polygon(start, checked):
            raise ValueError("start_xy")
        containing = [
            index for index, cell in enumerate(cells)
            if cell[0] - 1e-9 <= start[0] <= cell[1] + 1e-9
            and cell[2] - 1e-9 <= start[1] <= cell[3] + 1e-9
        ]
        if not containing:
            raise ValueError("start_xy")
        available.remove(min(
            containing,
            key=lambda index: (math.dist(start, cells[index][7]), cells[index][4:6]),
        ))
    count = min(count, len(available))
    order = [
        index for index in progressive_farthest_order(
            [cell[7] for cell in cells], start_xy=start,
            seed=seed + pass_index,
        )
        if index in available
    ][:count]
    result = []
    for index in order:
        _left, _right, _bottom, _top, row, column, clipped, center = cells[index]
        selector = _fraction(seed, pass_index, row, column, "vertex")
        vertex = clipped[min(int(selector * len(clipped)), len(clipped) - 1)]
        blend = 0.15 + 0.5 * _fraction(seed, pass_index, row, column, "blend")
        result.append((
            center[0] * (1.0 - blend) + vertex[0] * blend,
            center[1] * (1.0 - blend) + vertex[1] * blend,
            row, column,
        ))
    return result
