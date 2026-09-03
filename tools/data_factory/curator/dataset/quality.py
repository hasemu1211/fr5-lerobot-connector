"""Derived image metrics and non-authoritative recording-quality lineage."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..core.errors import CuratorError
from ..core.identity import file_sha256, read_regular_bytes


def image_metrics(image: np.ndarray) -> tuple[float, float, float, float]:
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[-1] != 3:
        raise CuratorError("DERIVED_IMAGE_METRICS")
    value = image.astype(np.float32)
    color = float(
        (
            np.abs(value[..., 0] - value[..., 1]).mean()
            + np.abs(value[..., 1] - value[..., 2]).mean()
        )
        / 2
    )
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    return (
        color,
        float(gray.mean()),
        float(((gray <= 5) | (gray >= 250)).mean()),
        float(cv2.Laplacian(gray, cv2.CV_64F).var()),
    )


def metric_accumulator() -> dict[str, Any]:
    return {
        "count": 0,
        "color_delta_total": 0.0,
        "brightness_total": 0.0,
        "clipping_total": 0.0,
        "sharpness": [],
    }


def accumulate_metrics(accumulator: dict[str, Any], image: np.ndarray) -> None:
    color, brightness, clipping, sharpness = image_metrics(image)
    accumulator["count"] += 1
    accumulator["color_delta_total"] += color
    accumulator["brightness_total"] += brightness
    accumulator["clipping_total"] += clipping
    accumulator["sharpness"].append(sharpness)


def summarize_metrics(accumulator: dict[str, Any]) -> dict[str, float]:
    count = accumulator["count"]
    if type(count) is not int or count <= 0:
        raise CuratorError("DERIVED_IMAGE_METRICS")
    result = {
        "color_delta_mean": accumulator["color_delta_total"] / count,
        "brightness_mean": accumulator["brightness_total"] / count,
        "clipping_mean": accumulator["clipping_total"] / count,
        "sharpness_median": float(np.median(accumulator["sharpness"])),
    }
    if not all(math.isfinite(value) for value in result.values()):
        raise CuratorError("DERIVED_IMAGE_METRICS")
    return result


def derived_image_quality_warnings(cameras: dict[str, dict[str, float]]) -> list[str]:
    warnings: list[str] = []
    for camera in ("up", "wrist"):
        metrics = cameras[camera]
        if metrics["color_delta_mean"] < 1.0:
            warnings.append(
                f"{camera} image appears monochrome (color delta {metrics['color_delta_mean']:.2f})"
            )
        if not 20 <= metrics["brightness_mean"] <= 235:
            warnings.append(
                f"{camera} brightness {metrics['brightness_mean']:.1f} outside diagnostic range"
            )
        if metrics["clipping_mean"] > 0.20:
            warnings.append(
                f"{camera} clipping {metrics['clipping_mean']:.1%} exceeds diagnostic threshold"
            )
        if metrics["sharpness_median"] < 20:
            warnings.append(
                f"{camera} sharpness {metrics['sharpness_median']:.1f} below diagnostic threshold"
            )
    return warnings


def _strict_json_line(text: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise CuratorError("SOURCE_QUALITY_EVIDENCE", f"duplicate key: {key}")
            value[key] = item
        return value

    def nonfinite(value: str) -> None:
        raise CuratorError("SOURCE_QUALITY_EVIDENCE", f"non-finite: {value}")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except CuratorError:
        raise
    except json.JSONDecodeError as exc:
        raise CuratorError("SOURCE_QUALITY_EVIDENCE", str(exc)) from exc
    if not isinstance(value, dict):
        raise CuratorError("SOURCE_QUALITY_EVIDENCE", "object required")
    return value


def write_derived_quality(
    source: str | Path,
    output: str | Path,
    verification: dict[str, Any],
    profile_digest: str,
) -> dict[str, Any]:
    source_path = Path(source) / "meta/recording_quality.jsonl"
    try:
        payload = read_regular_bytes(source_path, code="SOURCE_QUALITY_EVIDENCE")
        source_lines = payload.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise CuratorError("SOURCE_QUALITY_EVIDENCE", str(exc)) from exc
    metrics = {
        item["episode_index"]: item["cameras"]
        for item in verification["derived_image_metrics"]
    }
    if len(source_lines) != verification["episodes"] or set(metrics) != set(
        range(verification["episodes"])
    ):
        raise CuratorError("SOURCE_QUALITY_EVIDENCE", "episode mismatch")
    derived_lines: list[str] = []
    seen: set[int] = set()
    source_sha256 = file_sha256(source_path)
    for line in source_lines:
        quality = _strict_json_line(line)
        episode = quality.get("episode_index")
        cameras = quality.get("cameras")
        if (
            type(episode) is not int
            or episode in seen
            or episode not in metrics
            or not isinstance(cameras, dict)
            or set(cameras) != {"up", "wrist"}
            or "curator_lineage" in quality
            or not isinstance(quality.get("image_quality_warnings"), list)
            or any(
                not isinstance(item, str) for item in quality["image_quality_warnings"]
            )
        ):
            raise CuratorError("SOURCE_QUALITY_EVIDENCE", str(episode))
        for camera in ("up", "wrist"):
            if not isinstance(cameras[camera], dict):
                raise CuratorError("SOURCE_QUALITY_EVIDENCE", camera)
            cameras[camera].update(metrics[episode][camera])
        quality["image_quality_warnings"] = derived_image_quality_warnings(
            metrics[episode]
        )
        quality["curator_lineage"] = {
            "schema_version": "curator.derived_recording_quality_lineage.v1",
            "source_recording_quality_sha256": source_sha256,
            "source_timing_evidence": "PRESERVED",
            "derived_pixel_metrics": "RECOMPUTED",
            "source_image_quality_warnings": "REPLACED_FROM_DERIVED_METRICS",
            "profile_digest": profile_digest,
            "training_authority": False,
        }
        derived_lines.append(
            json.dumps(
                quality,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        seen.add(episode)
    if seen != set(metrics):
        raise CuratorError("SOURCE_QUALITY_EVIDENCE", "missing episode")

    target = Path(output) / "meta/recording_quality.jsonl"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags, 0o400)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(derived_lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise CuratorError("DERIVED_QUALITY_WRITE", str(exc)) from exc
    return {
        "source_recording_quality_sha256": source_sha256,
        "derived_recording_quality_sha256": file_sha256(target),
        "source_timing_evidence": "PRESERVED",
        "derived_pixel_metrics": "RECOMPUTED",
    }


__all__ = [
    "accumulate_metrics",
    "derived_image_quality_warnings",
    "image_metrics",
    "metric_accumulator",
    "summarize_metrics",
    "write_derived_quality",
]
