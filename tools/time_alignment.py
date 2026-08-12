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
