"""Small timestamp-alignment helpers for buffered robot data."""

from __future__ import annotations

import numpy as np


def interpolate_vector(samples, target: float, max_distance: float):
    before = next((sample for sample in reversed(samples) if sample[0] <= target), None)
    after = next((sample for sample in samples if sample[0] >= target), None)
    if before is None or after is None:
        return None
    if target - before[0] > max_distance or after[0] - target > max_distance:
        return None
    if after[0] == before[0]:
        return np.asarray(before[1]).copy(), before[0], after[0]
    weight = (target - before[0]) / (after[0] - before[0])
    value = np.asarray(before[1]) + weight * (np.asarray(after[1]) - np.asarray(before[1]))
    return value.astype(np.float32), before[0], after[0]


def latest_sample(samples, target: float, max_age: float):
    sample = next((sample for sample in reversed(samples) if sample[0] <= target), None)
    return sample if sample is not None and target - sample[0] <= max_age else None


def nearest_sample(samples, target: float, max_distance: float):
    sample = min(samples, key=lambda item: abs(item[0] - target), default=None)
    return sample if sample is not None and abs(sample[0] - target) <= max_distance else None


def estimate_time_offset(
    image_times,
    image_motion,
    robot_times,
    robot_speed,
    max_offset_s: float = 0.2,
    step_s: float = 0.001,
):
    """Return the timestamp correction that best correlates visual and robot motion."""
    image_times = np.asarray(image_times, dtype=float)
    image_motion = np.asarray(image_motion, dtype=float)
    robot_times = np.asarray(robot_times, dtype=float)
    robot_speed = np.asarray(robot_speed, dtype=float)
    if image_times.size != image_motion.size or robot_times.size != robot_speed.size:
        raise ValueError("motion timestamps and values must have matching lengths")
    if min(image_times.size, robot_times.size) < 8:
        raise ValueError("not enough motion samples")
    if not all(np.isfinite(values).all() for values in (image_times, image_motion, robot_times, robot_speed)):
        raise ValueError("motion samples must be finite")
    if not (np.diff(image_times) > 0).all() or not (np.diff(robot_times) > 0).all():
        raise ValueError("motion timestamps must be strictly increasing")
    if not np.isfinite([max_offset_s, step_s]).all() or max_offset_s <= 0 or step_s <= 0:
        raise ValueError("offset search bounds must be finite and positive")
    candidates = np.arange(-max_offset_s, max_offset_s + step_s / 2, step_s)
    scores = np.full(candidates.shape, np.nan)
    for index, offset in enumerate(candidates):
        shifted = image_times + offset
        valid = (shifted >= robot_times[0]) & (shifted <= robot_times[-1])
        if valid.sum() < 8:
            continue
        visual = image_motion[valid]
        robot = np.interp(shifted[valid], robot_times, robot_speed)
        if visual.std() > 0 and robot.std() > 0:
            scores[index] = np.corrcoef(visual, robot)[0, 1]
    if not np.isfinite(scores).any():
        raise ValueError("motion signals cannot be correlated")
    best_index = int(np.nanargmax(scores))
    exclusion = max(step_s, 0.050)
    alternatives = scores[np.abs(candidates - candidates[best_index]) >= exclusion]
    if not np.isfinite(alternatives).any():
        raise ValueError("offset search window is too narrow to prove a unique peak")
    best = float(scores[best_index])
    second = float(np.nanmax(alternatives))
    near_peak = candidates[scores >= best - 0.01]
    peak_width = float(near_peak[-1] - near_peak[0])
    return float(candidates[best_index]), best, float(best - second), peak_width
