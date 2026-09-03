"""Full derived-dataset verification and existing-validator integration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

import numpy as np

from ..core.errors import CuratorError
from ..profile.schema import CAMERA_KEY
from ..profile.transform import apply_up_view, uint8_hwc
from .quality import accumulate_metrics, metric_accumulator, summarize_metrics
from .source import open_source_dataset


CODEC_MAX_FRAME_MAE = 18.0
FFPROBE_TIMEOUT_SECONDS = 30
VALIDATOR_TIMEOUT_SECONDS = 300
FrameObserver = Callable[..., None]


def _numpy(value: Any) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    return np.asarray(value)


def _scalar(value: Any, code: str) -> Any:
    array = _numpy(value)
    if array.size != 1:
        raise CuratorError(code)
    return array.reshape(-1)[0].item()


def _expected_video_paths(dataset: Any) -> set[str]:
    expected: set[str] = set()
    try:
        for episode in range(dataset.meta.total_episodes):
            for key in dataset.meta.video_keys:
                expected.add(
                    Path(dataset.meta.get_video_file_path(episode, key)).as_posix()
                )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise CuratorError("DERIVED_VIDEO_METADATA", str(exc)) from exc
    return expected


def verify_h264(root: str | Path, expected_paths: set[str]) -> list[str]:
    base = Path(root)
    actual = {
        path.relative_to(base).as_posix()
        for path in (base / "videos").rglob("*.mp4")
        if path.is_file() and not path.is_symlink()
    }
    if not actual or actual != expected_paths:
        raise CuratorError(
            "DERIVED_H264_FILE_SET",
            f"expected={sorted(expected_paths)} actual={sorted(actual)}",
        )
    for relative in sorted(actual):
        path = base / relative
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "json",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=FFPROBE_TIMEOUT_SECONDS,
            )
            streams = json.loads(result.stdout).get("streams", [])
        except (
            FileNotFoundError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
        ) as exc:
            raise CuratorError("DERIVED_H264", f"{path}: {exc}") from exc
        if len(streams) != 1 or streams[0].get("codec_name") != "h264":
            raise CuratorError("DERIVED_H264", f"{path}: {streams}")
    return sorted(actual)


def run_existing_validator(root: str | Path, repo_id: str) -> dict[str, Any]:
    repository = Path(__file__).resolve().parents[4]
    command = [
        sys.executable,
        str(repository / "tools/validate_lerobot_dataset.py"),
        str(root),
        "--repo-id",
        repo_id,
        "--expected-fps",
        "30",
        "--skip-decoded-image-diagnostics",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=VALIDATOR_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CuratorError("EXISTING_VALIDATOR_TIMEOUT", str(exc)) from exc
    if result.returncode != 0:
        detail = "\n".join((result.stdout + "\n" + result.stderr).splitlines()[-20:])
        raise CuratorError("EXISTING_VALIDATOR_FAILED", detail)
    lines = [line for line in result.stdout.splitlines() if line]
    return {
        "status": "PASS",
        "returncode": result.returncode,
        "stdout_sha256": "sha256:" + hashlib.sha256(result.stdout.encode()).hexdigest(),
        "summary": lines[-1] if lines else "PASS",
    }


def verify_derived_dataset(
    source_dataset: Any,
    derived_root: str | Path,
    *,
    derived_repo_id: str,
    profile: dict[str, Any],
    keep_mask: np.ndarray,
    background_plate: np.ndarray,
    frame_observer: FrameObserver | None = None,
) -> dict[str, Any]:
    """Decode every frame and compare all preserved and transformed features."""
    try:
        derived = open_source_dataset(Path(derived_root), derived_repo_id)
    except CuratorError as exc:
        raise CuratorError("DERIVED_READER", str(exc)) from exc
    if (
        len(source_dataset) != len(derived)
        or source_dataset.meta.total_episodes != derived.meta.total_episodes
        or source_dataset.meta.fps != derived.meta.fps
        or source_dataset.meta.robot_type != derived.meta.robot_type
        or source_dataset.meta.features != derived.meta.features
    ):
        raise CuratorError("DERIVED_METADATA_PRESERVATION")
    if not len(derived):
        raise CuratorError("DERIVED_EMPTY")

    mapping: dict[int, dict[str, int]] = {}
    maxima = {"up_keep_mae": 0.0, "up_replace_mae": 0.0, "wrist_mae": 0.0}
    image_metrics: dict[int, dict[str, dict[str, Any]]] = {}
    replace_mask = ~keep_mask
    for index in range(len(source_dataset)):
        try:
            source = source_dataset[index]
            output = derived[index]
        except Exception as exc:
            raise CuratorError("DERIVED_FULL_DECODE", f"frame {index}: {exc}") from exc
        for key in ("index", "episode_index", "frame_index", "task_index"):
            if int(_scalar(source[key], "SOURCE_INDEX")) != int(
                _scalar(output[key], "DERIVED_INDEX")
            ):
                raise CuratorError("DERIVED_INDEX_PRESERVATION", f"{key}:{index}")
        source_timestamp = float(_scalar(source["timestamp"], "SOURCE_TIMESTAMP"))
        output_timestamp = float(_scalar(output["timestamp"], "DERIVED_TIMESTAMP"))
        if (
            abs(source_timestamp - output_timestamp) > 1e-7
            or source["task"] != output["task"]
        ):
            raise CuratorError("DERIVED_TASK_TIMESTAMP_PRESERVATION", str(index))
        for key in ("observation.state", "action"):
            if not np.array_equal(_numpy(source[key]), _numpy(output[key])):
                raise CuratorError("DERIVED_NUMERIC_PRESERVATION", f"{key}:{index}")

        raw_up = uint8_hwc(
            source[CAMERA_KEY],
            width=profile["width"],
            height=profile["height"],
            code="SOURCE_UP_FRAME",
        )
        raw_wrist = uint8_hwc(
            source["observation.images.wrist"],
            width=profile["width"],
            height=profile["height"],
            code="SOURCE_WRIST_FRAME",
        )
        output_up = uint8_hwc(
            output[CAMERA_KEY],
            width=profile["width"],
            height=profile["height"],
            code="DERIVED_UP_FRAME",
        )
        output_wrist = uint8_hwc(
            output["observation.images.wrist"],
            width=profile["width"],
            height=profile["height"],
            code="DERIVED_WRIST_FRAME",
        )
        expected_up = apply_up_view(raw_up, keep_mask, background_plate)
        up_delta = np.abs(output_up.astype(np.int16) - expected_up.astype(np.int16))
        wrist_delta = np.abs(output_wrist.astype(np.int16) - raw_wrist.astype(np.int16))
        keep_delta = up_delta[keep_mask]
        replace_delta = up_delta[replace_mask]
        frame_metrics = {
            "up_keep_mae": float(keep_delta.mean()) if keep_delta.size else 0.0,
            "up_replace_mae": float(replace_delta.mean())
            if replace_delta.size
            else 0.0,
            "wrist_mae": float(wrist_delta.mean()) if wrist_delta.size else 0.0,
        }
        for key, value in frame_metrics.items():
            maxima[key] = max(maxima[key], value)
            if value > CODEC_MAX_FRAME_MAE:
                raise CuratorError(
                    "DERIVED_CODEC_BASELINE", f"{key}={value:.3f} frame={index}"
                )

        episode = int(_scalar(source["episode_index"], "SOURCE_EPISODE"))
        if episode not in mapping:
            mapping[episode] = {
                "episode_index": episode,
                "source_from_index": index,
                "derived_from_index": index,
                "frames": 0,
            }
            image_metrics[episode] = {
                "up": metric_accumulator(),
                "wrist": metric_accumulator(),
            }
        mapping[episode]["frames"] += 1
        accumulate_metrics(image_metrics[episode]["up"], output_up)
        accumulate_metrics(image_metrics[episode]["wrist"], output_wrist)
        if frame_observer is not None:
            try:
                frame_observer(
                    dataset_index=index,
                    source_row=source,
                    candidate_row=output,
                    raw_up=raw_up,
                    candidate_up=output_up,
                )
            except CuratorError:
                raise
            except Exception as exc:
                raise CuratorError(
                    "REVIEW_SIGNAL_OBSERVER", f"frame {index}: {exc}"
                ) from exc

    expected_episodes = list(range(source_dataset.meta.total_episodes))
    if list(mapping) != expected_episodes:
        raise CuratorError("DERIVED_EPISODE_ORDER")
    forbidden = [
        path.relative_to(derived_root).as_posix()
        for path in Path(derived_root).rglob("*")
        if path.name == "training_approved.json" or "quarantine" in path.name.casefold()
    ]
    if forbidden:
        raise CuratorError("DERIVED_AUTHORITY_INHERITANCE", str(forbidden))
    derived_image_metrics = []
    for episode in expected_episodes:
        cameras = {
            camera: summarize_metrics(accumulator)
            for camera, accumulator in image_metrics[episode].items()
        }
        derived_image_metrics.append({"episode_index": episode, "cameras": cameras})
    h264_files = verify_h264(Path(derived_root), _expected_video_paths(derived))
    return {
        "schema_version": "curator.post_write_verification.v1",
        "status": "PASS",
        "episodes": source_dataset.meta.total_episodes,
        "frames": len(source_dataset),
        "episode_mapping": [mapping[index] for index in expected_episodes],
        "state_action_task_timestamp_preserved": True,
        "official_loader_full_decode": True,
        "video_codec": {"expected": "h264", "verified_files": h264_files},
        "derived_image_metrics": derived_image_metrics,
        "up_transform": {
            "camera_key": CAMERA_KEY,
            "max_keep_mae": maxima["up_keep_mae"],
            "max_replace_mae": maxima["up_replace_mae"],
            "codec_max_frame_mae": CODEC_MAX_FRAME_MAE,
        },
        "wrist_passthrough": {
            "camera_key": "observation.images.wrist",
            "preencode_semantic_transform": False,
            "max_noop_encode_mae": maxima["wrist_mae"],
            "codec_max_frame_mae": CODEC_MAX_FRAME_MAE,
        },
        "training_authority": False,
        "approval_inherited": False,
        "quarantine_inherited": False,
    }


__all__ = [
    "CODEC_MAX_FRAME_MAE",
    "run_existing_validator",
    "verify_derived_dataset",
    "verify_h264",
]
