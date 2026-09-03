"""FFmpeg H.264 raw | overlay | actual-candidate review rendering."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Iterable

import numpy as np

from ..core.jsonio import CuratorError
from ..profile.transform import render_keep_overlay, uint8_hwc


def render_review_mp4(
    frames: Iterable[tuple[np.ndarray, np.ndarray, np.ndarray]],
    output: str | Path,
    *,
    keep_mask: np.ndarray,
    width: int,
    height: int,
    fps: int,
) -> None:
    target = Path(output)
    keep_mask = np.asarray(keep_mask, dtype=bool)
    if keep_mask.shape != (height, width):
        raise CuratorError("REVIEW_MASK_SHAPE")
    if target.exists() or target.is_symlink() or not target.parent.is_dir():
        raise CuratorError("REVIEW_VIDEO_PATH", str(target))
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width * 3}x{height}",
        "-r", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
        "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p", str(target),
    ]
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise CuratorError("FFMPEG_UNAVAILABLE", str(exc)) from exc
    count = 0
    assert process.stdin is not None
    try:
        for raw, _ignored_overlay, candidate in frames:
            raw = uint8_hwc(raw, width=width, height=height, code="REVIEW_RAW")
            candidate = uint8_hwc(candidate, width=width, height=height, code="REVIEW_CANDIDATE")
            panel = np.concatenate((raw, render_keep_overlay(raw, keep_mask), candidate), axis=1)
            process.stdin.write(panel.tobytes())
            count += 1
        process.stdin.close()
        stderr = process.stderr.read() if process.stderr is not None else b""
        status = process.wait()
    except BaseException:
        process.kill()
        process.wait()
        target.unlink(missing_ok=True)
        raise
    if status or count == 0:
        target.unlink(missing_ok=True)
        raise CuratorError("REVIEW_FFMPEG", stderr.decode(errors="replace")[-1000:])


__all__ = ["render_review_mp4"]
