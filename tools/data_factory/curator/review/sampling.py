"""Streaming review signals and deterministic bounded clip selection."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import math
from typing import Any, Sequence

import cv2
import numpy as np

from ..core.errors import CuratorError


SIGNAL_NAMES = (
    "relative_time_quantile",
    "gripper_transition",
    "arm_state_transition",
    "arm_action_transition",
    "visual_motion",
    "mask_boundary_motion",
    "brightness_extreme",
    "sharpness_extreme",
    "seeded_uniform",
)
_ROW_FIELDS = {
    "dataset_index",
    "episode_index",
    "frame_index",
    "task",
    "timestamp",
    "relative_time",
    "gripper_transition",
    "arm_state_transition",
    "arm_action_transition",
    "visual_motion",
    "mask_boundary_motion",
    "brightness",
    "sharpness",
}


def _array(value: Any, code: str) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    array = np.asarray(value, dtype=np.float64).reshape(-1)
    if array.size < 2 or not np.isfinite(array).all():
        raise CuratorError(code)
    return array


def _integer(value: Any, code: str) -> int:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    array = np.asarray(value)
    if array.size != 1:
        raise CuratorError(code)
    item = array.reshape(-1)[0].item()
    if isinstance(item, bool) or not isinstance(item, (int, np.integer)):
        raise CuratorError(code)
    return int(item)


def _number(value: Any, code: str) -> float:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    array = np.asarray(value)
    if array.size != 1:
        raise CuratorError(code)
    item = array.reshape(-1)[0].item()
    if isinstance(item, bool) or not isinstance(
        item, (int, float, np.integer, np.floating)
    ):
        raise CuratorError(code)
    result = float(item)
    if not math.isfinite(result):
        raise CuratorError(code)
    return result


class ReviewSignalCollector:
    """Retain scalar metadata only; RGB lifetime never exceeds two frames."""

    def __init__(self, keep_mask: np.ndarray) -> None:
        mask = np.asarray(keep_mask)
        if mask.dtype != np.bool_ or mask.ndim != 2:
            raise CuratorError("REVIEW_SIGNAL_MASK")
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        encoded = mask.astype(np.uint8)
        self._boundary = cv2.dilate(encoded, kernel).astype(bool) ^ cv2.erode(
            encoded, kernel
        ).astype(bool)
        self._rows: list[dict[str, Any]] = []
        self._previous_episode: int | None = None
        self._previous_action: np.ndarray | None = None
        self._previous_state: np.ndarray | None = None
        self._previous_raw: np.ndarray | None = None
        self._finished = False

    def observe(
        self,
        *,
        dataset_index: int,
        source_row: dict[str, Any],
        candidate_row: dict[str, Any],
        raw_up: np.ndarray,
        candidate_up: np.ndarray,
    ) -> None:
        if self._finished:
            raise CuratorError("REVIEW_SIGNAL_FINISHED")
        if type(dataset_index) is not int or dataset_index != len(self._rows):
            raise CuratorError("REVIEW_SIGNAL_INDEX")
        episode = _integer(source_row["episode_index"], "REVIEW_SIGNAL_EPISODE")
        frame = _integer(source_row["frame_index"], "REVIEW_SIGNAL_FRAME")
        task = source_row.get("task")
        if not isinstance(task, str) or not task or task.strip() != task:
            raise CuratorError("REVIEW_SIGNAL_TASK")
        action = _array(source_row["action"], "REVIEW_SIGNAL_ACTION")
        state = _array(source_row["observation.state"], "REVIEW_SIGNAL_STATE")
        raw = np.asarray(raw_up, dtype=np.uint8)
        candidate = np.asarray(candidate_up, dtype=np.uint8)
        if raw.ndim != 3 or raw.shape[-1] != 3 or candidate.shape != raw.shape:
            raise CuratorError("REVIEW_SIGNAL_IMAGE")

        same_episode = self._previous_episode == episode
        previous_action = self._previous_action if same_episode else action
        previous_state = self._previous_state if same_episode else state
        previous_raw = self._previous_raw if same_episode else raw
        assert (
            previous_action is not None
            and previous_state is not None
            and previous_raw is not None
        )
        difference = np.abs(raw.astype(np.int16) - previous_raw.astype(np.int16)).mean(
            axis=2
        )
        gray = cv2.cvtColor(candidate, cv2.COLOR_RGB2GRAY)
        boundary_motion = (
            float(difference[self._boundary].mean()) if self._boundary.any() else 0.0
        )
        self._rows.append(
            {
                "dataset_index": dataset_index,
                "episode_index": episode,
                "frame_index": frame,
                "task": task,
                "timestamp": _number(
                    source_row["timestamp"], "REVIEW_SIGNAL_TIMESTAMP"
                ),
                "relative_time": 0.0,
                "gripper_transition": float(
                    abs(action[-1] - previous_action[-1])
                    + abs(state[-1] - previous_state[-1])
                ),
                "arm_state_transition": float(
                    np.linalg.norm(state[:-1] - previous_state[:-1])
                ),
                "arm_action_transition": float(
                    np.linalg.norm(action[:-1] - previous_action[:-1])
                ),
                "visual_motion": float(difference.mean()),
                "mask_boundary_motion": boundary_motion,
                "brightness": float(gray.mean()),
                "sharpness": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
            }
        )
        self._previous_episode = episode
        self._previous_action = action.copy()
        self._previous_state = state.copy()
        self._previous_raw = raw.copy()

    def finish(self) -> list[dict[str, Any]]:
        if self._finished or not self._rows:
            raise CuratorError("REVIEW_SIGNAL_EMPTY")
        self._finished = True
        result = self._rows
        self._rows = []
        groups: dict[tuple[int, str], list[int]] = defaultdict(list)
        for position, row in enumerate(result):
            groups[(row["episode_index"], row["task"])].append(position)
        for positions in groups.values():
            denominator = max(1, len(positions) - 1)
            for offset, position in enumerate(positions):
                result[position]["relative_time"] = offset / denominator
        return result


def _window(positions: list[int], anchor: int, clip_frames: int) -> tuple[int, int]:
    offset = positions.index(anchor)
    start = max(0, min(offset - clip_frames // 2, max(0, len(positions) - clip_frames)))
    return start, min(len(positions), start + clip_frames)


def _seed_key(seed: int, index: int) -> bytes:
    return hashlib.sha256(f"{seed}:{index}".encode()).digest()


def signal_for_reason(reason: str) -> str | None:
    """Map one exact clip reason to the signal it actually covers."""
    if reason == "task_coverage":
        return None
    if reason == "seeded_uniform":
        return reason
    if reason.startswith("relative_time_quantile:"):
        try:
            value = float(reason.removeprefix("relative_time_quantile:"))
        except ValueError as exc:
            raise CuratorError("REVIEW_SAMPLE_REASON", reason) from exc
        if math.isfinite(value) and 0 <= value <= 1:
            return "relative_time_quantile"
    for name in (
        "gripper_transition",
        "arm_state_transition",
        "arm_action_transition",
        "visual_motion",
        "mask_boundary_motion",
    ):
        if reason == f"{name}:max":
            return name
    for name in ("brightness", "sharpness"):
        if reason in {f"{name}:min", f"{name}:max"}:
            return f"{name}_extreme"
    raise CuratorError("REVIEW_SAMPLE_REASON", reason)


def sample_frames(
    rows: Sequence[dict[str, Any]],
    *,
    seed: int,
    max_clips: int,
    clip_frames: int,
    fps: int,
    max_duration_seconds: float,
    relative_time_quantiles: Sequence[float] = (0.1, 0.5, 0.9),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not rows:
        raise CuratorError("REVIEW_SAMPLE_EMPTY")
    if (
        type(seed) is not int
        or type(max_clips) is not int
        or max_clips <= 0
        or type(clip_frames) is not int
        or clip_frames <= 0
        or type(fps) is not int
        or fps <= 0
        or isinstance(max_duration_seconds, bool)
        or not isinstance(max_duration_seconds, (int, float))
        or not math.isfinite(float(max_duration_seconds))
        or max_duration_seconds <= 0
    ):
        raise CuratorError("REVIEW_SAMPLE_POLICY")
    quantiles = [float(value) for value in relative_time_quantiles]
    if not quantiles or any(
        not math.isfinite(value) or not 0 <= value <= 1 for value in quantiles
    ):
        raise CuratorError("REVIEW_SAMPLE_POLICY")

    normalized = rows
    groups: dict[tuple[int, str], list[int]] = defaultdict(list)
    for position, row in enumerate(normalized):
        if set(row) != _ROW_FIELDS or row["dataset_index"] != position:
            raise CuratorError("REVIEW_SAMPLE_ROW", str(position))
        groups[(row["episode_index"], row["task"])].append(position)
    task_groups: dict[str, list[list[int]]] = defaultdict(list)
    for (_episode, task), positions in groups.items():
        task_groups[task].append(positions)
    clip_budget = min(max_clips, int(float(max_duration_seconds) * fps) // clip_frames)
    if clip_budget < 1:
        raise CuratorError("REVIEW_BUDGET_EMPTY")

    reasons: dict[int, set[str]] = defaultdict(set)
    mandatory: set[int] = set()
    ranked_tasks = sorted(
        task_groups,
        key=lambda task: (
            min(_seed_key(seed, positions[0]) for positions in task_groups[task]),
            task,
        ),
    )[:clip_budget]
    for task in ranked_tasks:
        candidates = task_groups[task]
        positions = min(
            candidates,
            key=lambda item: (_seed_key(seed, item[0]), task, item[0]),
        )
        center = positions[len(positions) // 2]
        mandatory.add(center)
        reasons[center].add("task_coverage")
    for positions in groups.values():
        for quantile in quantiles:
            anchor = min(
                positions,
                key=lambda item: (
                    abs(float(normalized[item]["relative_time"]) - quantile),
                    item,
                ),
            )
            reasons[anchor].add(f"relative_time_quantile:{quantile:.3f}")

    maximum_signals = (
        "gripper_transition",
        "arm_state_transition",
        "arm_action_transition",
        "visual_motion",
        "mask_boundary_motion",
    )
    for name in maximum_signals:
        anchor = max(
            range(len(normalized)), key=lambda item: (normalized[item][name], -item)
        )
        reasons[anchor].add(f"{name}:max")
    for name in ("brightness", "sharpness"):
        low = min(
            range(len(normalized)), key=lambda item: (normalized[item][name], item)
        )
        high = max(
            range(len(normalized)), key=lambda item: (normalized[item][name], -item)
        )
        reasons[low].add(f"{name}:min")
        reasons[high].add(f"{name}:max")
    uniform = min(range(len(normalized)), key=lambda item: _seed_key(seed, item))
    reasons[uniform].add("seeded_uniform")

    candidates: dict[tuple[int, str, int, int], dict[str, Any]] = {}
    for anchor, anchor_reasons in reasons.items():
        row = normalized[anchor]
        key = (row["episode_index"], row["task"])
        positions = groups[key]
        start, stop = _window(positions, anchor, clip_frames)
        window_key = (key[0], key[1], start, stop)
        entry = candidates.setdefault(
            window_key,
            {
                "anchor": anchor,
                "reasons": set(),
                "mandatory": False,
                "positions": positions[start:stop],
            },
        )
        entry["reasons"].update(anchor_reasons)
        if anchor in mandatory:
            entry["mandatory"] = True
            entry["anchor"] = anchor

    mandatory_candidates = sorted(
        (item for item in candidates.values() if item["mandatory"]),
        key=lambda item: (-len(item["reasons"]), _seed_key(seed, item["anchor"])),
    )
    ranked = mandatory_candidates[:clip_budget]
    selected_positions = {position for item in ranked for position in item["positions"]}
    remaining = [item for item in candidates.values() if not item["mandatory"]]
    while len(ranked) < clip_budget and remaining:
        remaining.sort(
            key=lambda item: (
                -len(set(item["positions"]) - selected_positions),
                -len(item["reasons"]),
                _seed_key(seed, item["anchor"]),
            )
        )
        item = remaining.pop(0)
        new_positions = set(item["positions"]) - selected_positions
        if not new_positions:
            break
        ranked.append(item)
        selected_positions.update(new_positions)
    covered_tasks = {
        normalized[item["anchor"]]["task"] for item in ranked if item["mandatory"]
    }
    if covered_tasks != set(ranked_tasks):
        raise CuratorError("REVIEW_TASK_COVERAGE")

    clips: list[dict[str, Any]] = []
    for clip_number, item in enumerate(ranked):
        anchor = normalized[item["anchor"]]
        selected = [normalized[position] for position in item["positions"]]
        clips.append(
            {
                "clip_id": f"clip-{clip_number:03d}",
                "episode_index": anchor["episode_index"],
                "task": anchor["task"],
                "anchor_dataset_index": anchor["dataset_index"],
                "anchor_frame_index": anchor["frame_index"],
                "dataset_indices": [row["dataset_index"] for row in selected],
                "frame_indices": [row["frame_index"] for row in selected],
                "reasons": sorted(item["reasons"]),
                "start_relative_seconds": selected[0]["timestamp"]
                - normalized[groups[(anchor["episode_index"], anchor["task"])][0]][
                    "timestamp"
                ],
                "duration_seconds": len(selected) / fps,
            }
        )
    rendered_frames = sum(len(clip["dataset_indices"]) for clip in clips)
    unique_selected_frames = len(
        {dataset_index for clip in clips for dataset_index in clip["dataset_indices"]}
    )
    covered_signals = {
        signal
        for clip in clips
        for reason in clip["reasons"]
        if (signal := signal_for_reason(reason)) is not None
    }
    coverage = {
        "population_frames": len(normalized),
        "rendered_frames": rendered_frames,
        "unique_selected_frames": unique_selected_frames,
        "clip_count": len(clips),
        "episodes": sorted({row["episode_index"] for row in normalized}),
        "tasks": sorted({row["task"] for row in normalized}),
        "covered_episodes": sorted({clip["episode_index"] for clip in clips}),
        "covered_tasks": sorted({clip["task"] for clip in clips}),
        "signals": sorted(covered_signals),
        "max_clips": max_clips,
        "max_duration_seconds": float(max_duration_seconds),
    }
    if rendered_frames / fps > float(max_duration_seconds) + 1e-9:
        raise CuratorError("REVIEW_DURATION_BOUND")
    return clips, coverage


__all__ = [
    "ReviewSignalCollector",
    "SIGNAL_NAMES",
    "sample_frames",
    "signal_for_reason",
]
