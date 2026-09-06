"""Strict digest-closed manifest for one bounded candidate review video."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..core.errors import CuratorError
from ..core.filesystem import write_json_exclusive
from ..core.identity import file_sha256
from ..core.jsonio import DIGEST, SAFE_ID, canonical_digest, exact_fields, load_json
from .render import verify_review_video
from .sampling import SIGNAL_NAMES, signal_for_reason


SCHEMA = "curator.review_manifest.v2"
FIELDS = {
    "schema_version",
    "clips",
    "coverage",
    "duration_seconds",
    "video",
    "identities",
    "review_video_sha256",
    "review_manifest_digest",
}
IDENTITY_FIELDS = {
    "source_tree_digest",
    "candidate_tree_digest",
    "profile_digest",
    "profile_file_sha256",
    "policy_digest",
    "policy_file_sha256",
    "request_event_digest",
    "candidate_ready_event_digest",
}
CLIP_FIELDS = {
    "clip_id",
    "episode_index",
    "task",
    "anchor_dataset_index",
    "anchor_frame_index",
    "dataset_indices",
    "frame_indices",
    "reasons",
    "start_relative_seconds",
    "duration_seconds",
}
COVERAGE_FIELDS = {
    "population_frames",
    "rendered_frames",
    "unique_selected_frames",
    "clip_count",
    "episodes",
    "tasks",
    "covered_episodes",
    "covered_tasks",
    "signals",
    "max_clips",
    "max_duration_seconds",
}
VIDEO_FIELDS = {"codec", "width", "height", "frames", "fps"}


def _integer(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _number(value: object, *, positive: bool = False) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and (float(value) > 0 if positive else float(value) >= 0)
    )


def _string_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item and item.isprintable() for item in value)
        and value == sorted(set(value))
    )


def _integer_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_integer(item) for item in value)
        and value == sorted(set(value))
    )


def _validate(value: dict[str, Any], *, video_path: str | Path | None) -> dict[str, Any]:
    exact_fields(value, FIELDS, "REVIEW_MANIFEST_FIELDS")
    if value["schema_version"] != SCHEMA:
        raise CuratorError("REVIEW_MANIFEST_SCHEMA")
    digest = value["review_manifest_digest"]
    if (
        not isinstance(digest, str)
        or DIGEST.fullmatch(digest) is None
        or digest
        != canonical_digest(
            {
                key: item
                for key, item in value.items()
                if key != "review_manifest_digest"
            }
        )
    ):
        raise CuratorError("REVIEW_MANIFEST_DIGEST")
    identities = exact_fields(value["identities"], IDENTITY_FIELDS, "REVIEW_IDENTITIES")
    if any(
        not isinstance(item, str) or DIGEST.fullmatch(item) is None
        for item in identities.values()
    ):
        raise CuratorError("REVIEW_IDENTITIES")

    video = exact_fields(value["video"], VIDEO_FIELDS, "REVIEW_VIDEO_FIELDS")
    if (
        video["codec"] != "h264"
        or not _integer(video["width"], minimum=3)
        or video["width"] % 3
        or not _integer(video["height"], minimum=1)
        or not _integer(video["frames"], minimum=1)
        or not _integer(video["fps"], minimum=1)
    ):
        raise CuratorError("REVIEW_VIDEO_CONTRACT")
    duration = value["duration_seconds"]
    if (
        not _number(duration, positive=True)
        or abs(float(duration) - video["frames"] / video["fps"]) > 1e-9
    ):
        raise CuratorError("REVIEW_DURATION_CONTRACT")
    review_digest = value["review_video_sha256"]
    if (
        not isinstance(review_digest, str)
        or DIGEST.fullmatch(review_digest) is None
        or (video_path is not None and review_digest != file_sha256(video_path))
    ):
        raise CuratorError("REVIEW_VIDEO_DIGEST")
    if video_path is not None:
        verify_review_video(
            Path(video_path),
            width=video["width"] // 3,
            height=video["height"],
            frames=video["frames"],
            fps=video["fps"],
        )

    coverage = exact_fields(
        value["coverage"], COVERAGE_FIELDS, "REVIEW_COVERAGE_FIELDS"
    )
    if (
        not _integer(coverage["population_frames"], minimum=1)
        or not _integer(coverage["rendered_frames"], minimum=1)
        or not _integer(coverage["unique_selected_frames"], minimum=1)
        or coverage["unique_selected_frames"] > coverage["population_frames"]
        or coverage["unique_selected_frames"] > coverage["rendered_frames"]
        or not _integer(coverage["clip_count"], minimum=1)
        or not _integer(coverage["max_clips"], minimum=1)
        or not _number(coverage["max_duration_seconds"], positive=True)
        or not _integer_list(coverage["episodes"])
        or not _string_list(coverage["tasks"])
        or not _integer_list(coverage["covered_episodes"])
        or not _string_list(coverage["covered_tasks"])
        or not _string_list(coverage["signals"])
        or not set(coverage["covered_episodes"]).issubset(coverage["episodes"])
        or not set(coverage["covered_tasks"]).issubset(coverage["tasks"])
        or coverage["clip_count"] > coverage["max_clips"]
        or float(duration) > float(coverage["max_duration_seconds"]) + 1e-9
    ):
        raise CuratorError("REVIEW_COVERAGE_CONTRACT")

    clips = value["clips"]
    if not isinstance(clips, list) or len(clips) != coverage["clip_count"]:
        raise CuratorError("REVIEW_CLIPS_CONTRACT")
    clip_ids: set[str] = set()
    rendered_frames = 0
    unique_selected_frames: set[int] = set()
    covered_signals: set[str] = set()
    covered_episodes: set[int] = set()
    covered_tasks: set[str] = set()
    for clip in clips:
        exact_fields(clip, CLIP_FIELDS, "REVIEW_CLIP_FIELDS")
        dataset_indices = clip["dataset_indices"]
        frame_indices = clip["frame_indices"]
        if (
            not isinstance(clip["clip_id"], str)
            or SAFE_ID.fullmatch(clip["clip_id"]) is None
            or clip["clip_id"] in clip_ids
            or not _integer(clip["episode_index"])
            or not isinstance(clip["task"], str)
            or not clip["task"]
            or clip["task"].strip() != clip["task"]
            or not _integer(clip["anchor_dataset_index"])
            or not _integer(clip["anchor_frame_index"])
            or not _integer_list(dataset_indices)
            or not _integer_list(frame_indices)
            or len(dataset_indices) != len(frame_indices)
            or dataset_indices
            != list(
                range(dataset_indices[0], dataset_indices[0] + len(dataset_indices))
            )
            or frame_indices
            != list(range(frame_indices[0], frame_indices[0] + len(frame_indices)))
            or clip["anchor_dataset_index"] not in dataset_indices
            or clip["anchor_frame_index"] not in frame_indices
            or not _string_list(clip["reasons"])
            or not _number(clip["start_relative_seconds"])
            or not _number(clip["duration_seconds"], positive=True)
            or abs(
                float(clip["duration_seconds"]) - len(dataset_indices) / video["fps"]
            )
            > 1e-9
        ):
            raise CuratorError("REVIEW_CLIP_CONTRACT", str(clip.get("clip_id")))
        clip_ids.add(clip["clip_id"])
        rendered_frames += len(dataset_indices)
        unique_selected_frames.update(dataset_indices)
        covered_episodes.add(clip["episode_index"])
        covered_tasks.add(clip["task"])
        covered_signals.update(
            signal
            for reason in clip["reasons"]
            if (signal := signal_for_reason(reason)) is not None
        )
    if (
        rendered_frames != coverage["rendered_frames"]
        or rendered_frames != video["frames"]
        or len(unique_selected_frames) != coverage["unique_selected_frames"]
        or coverage["covered_episodes"] != sorted(covered_episodes)
        or coverage["covered_tasks"] != sorted(covered_tasks)
    ):
        raise CuratorError("REVIEW_COVERAGE_DERIVATION")
    if set(coverage["signals"]) - set(SIGNAL_NAMES) or coverage["signals"] != sorted(
        covered_signals
    ):
        raise CuratorError("REVIEW_SIGNAL_COVERAGE")
    return value


def create_manifest(
    path: str | Path,
    *,
    clips: list[dict[str, Any]],
    coverage: dict[str, Any],
    identities: dict[str, str],
    video_path: str | Path,
    video: dict[str, object],
    fps: int,
) -> dict[str, Any]:
    if (
        type(fps) is not int
        or fps <= 0
        or not isinstance(video, dict)
        or type(video.get("frames")) is not int
        or video["frames"] <= 0
    ):
        raise CuratorError("REVIEW_VIDEO_CONTRACT")
    video_value = {**video, "fps": fps}
    value = {
        "schema_version": SCHEMA,
        "clips": clips,
        "coverage": coverage,
        "duration_seconds": video_value["frames"] / fps,
        "video": video_value,
        "identities": identities,
        "review_video_sha256": file_sha256(video_path),
    }
    value["review_manifest_digest"] = canonical_digest(value)
    _validate(value, video_path=video_path)
    write_json_exclusive(path, value)
    return value


def verify_manifest(path: str | Path, video_path: str | Path) -> dict[str, Any]:
    if video_path is None:
        raise CuratorError("REVIEW_VIDEO_PATH")
    value = load_json(path, code="REVIEW_MANIFEST_JSON")
    return _validate(value, video_path=video_path)


def verify_recorded_manifest(path: str | Path, *, expected_digest: str) -> dict[str, Any]:
    """Read coverage bound by a completed decision; never qualify playback/decision.

    The caller must first validate the terminal receipt that owns this digest.
    Ordinary review and decision validation still require the complete video.
    """
    value = _validate(load_json(path, code="REVIEW_MANIFEST_JSON"), video_path=None)
    if value["review_manifest_digest"] != expected_digest:
        raise CuratorError("REVIEW_MANIFEST_DIGEST")
    return value


__all__ = ["create_manifest", "verify_manifest"]
