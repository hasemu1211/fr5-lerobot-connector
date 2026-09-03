"""Deterministic, bounded review-frame sampling."""

from __future__ import annotations

from collections import defaultdict
import hashlib
from typing import Any, Sequence

import numpy as np


def sample_frames(rows: Sequence[dict[str, Any]], *, seed: int, max_clips: int) -> list[dict[str, Any]]:
    """Cover every episode/task before filling deterministic transition extrema."""
    if not rows or max_clips < 1:
        return []
    groups: dict[tuple[int, str], list[int]] = defaultdict(list)
    scores: list[tuple[float, int]] = []
    previous: np.ndarray | None = None
    for index, row in enumerate(rows):
        episode, task = int(row["episode_index"]), str(row["task"])
        groups[(episode, task)].append(index)
        current = np.asarray(row.get("action", ()), dtype=np.float64).reshape(-1)
        score = float(np.linalg.norm(current - previous)) if previous is not None and current.shape == previous.shape else 0.0
        scores.append((score, index))
        previous = current
    selected: dict[int, set[str]] = defaultdict(set)
    for key, indices in sorted(groups.items()):
        for label, offset in (("episode_start", 0), ("episode_middle", len(indices) // 2), ("episode_end", -1)):
            selected[indices[offset]].add(label)
    salt = seed.to_bytes(16, "big", signed=True)
    ranked = sorted(scores, key=lambda item: (-item[0], hashlib.sha256(salt + str(item[1]).encode()).digest()))
    for _score, index in ranked:
        selected[index].add("action_transition")
        if len(selected) >= max_clips:
            break
    chosen = sorted(selected)[:max_clips]
    return [{"dataset_index": index, "reasons": sorted(selected[index])} for index in chosen]


__all__ = ["sample_frames"]
