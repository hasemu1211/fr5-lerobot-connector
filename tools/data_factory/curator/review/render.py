"""Streaming raw | geometry overlay | actual-candidate H.264 rendering."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile
import threading
from typing import Iterable, Sequence

import cv2
import numpy as np

from ..core.errors import CuratorError
from ..core.filesystem import (
    reject_symlink_components,
    remove_owned_regular_file,
    rename_open_file_noreplace,
)
from ..profile.transform import uint8_hwc


REVIEW_HEADER_HEIGHT = 52
REVIEW_FFMPEG_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class ReviewFrame:
    raw_up: np.ndarray
    candidate_up: np.ndarray
    clip_id: str
    episode_index: int
    frame_index: int
    timestamp: float
    reasons: tuple[str, ...]


def _outline(
    image: np.ndarray,
    polygons: Sequence[Sequence[Sequence[float]]],
    color: tuple[int, int, int],
    thickness: int = 2,
) -> None:
    for polygon in polygons:
        points = np.rint(np.asarray(polygon, dtype=np.float64)).astype(np.int32)
        cv2.polylines(image, [points], True, color, thickness, cv2.LINE_AA)


def render_keep_overlay(
    raw_up: np.ndarray,
    keep_mask: np.ndarray,
    geometry: dict,
) -> np.ndarray:
    if keep_mask.shape != raw_up.shape[:2]:
        raise CuratorError("OVERLAY_INPUT")
    colors = np.empty_like(raw_up)
    colors[keep_mask] = (24, 210, 72)
    colors[~keep_mask] = (220, 48, 170)
    image = cv2.addWeighted(raw_up, 0.68, colors, 0.32, 0)
    _outline(image, [geometry["table_work_surface"]], (255, 220, 40), 3)
    _outline(image, geometry["visual_motion_support"], (40, 220, 255), 2)
    _outline(image, geometry["grounding_context_support"], (255, 145, 35), 2)
    place_colors = {"PLACE_A": (235, 45, 45), "PLACE_B": (45, 95, 235)}
    for place, polygon in geometry["semantic_subregions"].items():
        _outline(image, [polygon], place_colors[place], 3)
        anchor = tuple(np.rint(np.asarray(polygon[0])).astype(int))
        cv2.putText(
            image,
            place,
            anchor,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            place_colors[place],
            2,
            cv2.LINE_AA,
        )
    for place, corners in geometry["place_plane_correspondence"].items():
        for name, point in corners.items():
            center = tuple(np.rint(np.asarray(point)).astype(int))
            cv2.circle(image, center, 4, place_colors[place], -1, cv2.LINE_AA)
            cv2.putText(
                image,
                name,
                (center[0] + 5, center[1] - 5),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.38,
                place_colors[place],
                1,
                cv2.LINE_AA,
            )
    return image


def _panel(image: np.ndarray, title: str, detail: str) -> np.ndarray:
    """Put labels outside the evidence pixels so no scene region is hidden."""
    header = np.zeros((REVIEW_HEADER_HEIGHT, image.shape[1], 3), dtype=np.uint8)
    cv2.putText(
        header,
        title,
        (8, 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        header,
        detail,
        (8, 43),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.42,
        (235, 235, 235),
        1,
        cv2.LINE_AA,
    )
    return np.concatenate((header, image), axis=0)


def verify_review_video(
    path: Path,
    *,
    width: int,
    height: int,
    frames: int,
    fps: int | None = None,
) -> dict[str, object]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height,nb_read_frames,avg_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        raise CuratorError("REVIEW_CODEC", str(exc)) from exc
    if len(streams) != 1:
        raise CuratorError("REVIEW_CODEC", str(streams))
    stream = streams[0]
    try:
        observed_frames = int(stream.get("nb_read_frames", "-1"))
    except (TypeError, ValueError) as exc:
        raise CuratorError("REVIEW_CODEC", str(stream)) from exc
    rate = stream.get("avg_frame_rate")
    try:
        numerator, denominator = (int(item) for item in str(rate).split("/", 1))
        observed_fps = numerator / denominator
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise CuratorError("REVIEW_CODEC", str(stream)) from exc
    if (
        stream.get("codec_name") != "h264"
        or stream.get("width") != width * 3
        or stream.get("height") != height
        or observed_frames != frames
        or (fps is not None and abs(observed_fps - fps) > 1e-9)
    ):
        raise CuratorError("REVIEW_CODEC", str(stream))
    return {
        "codec": "h264",
        "width": width * 3,
        "height": height,
        "frames": observed_frames,
        "fps": observed_fps,
    }


def render_review_mp4(
    frames: Iterable[ReviewFrame],
    output: str | Path,
    *,
    keep_mask: np.ndarray,
    geometry: dict,
    width: int,
    height: int,
    fps: int,
    expected_frames: int,
) -> dict[str, object]:
    target = Path(output)
    reject_symlink_components(target, "REVIEW_VIDEO_PATH")
    if (
        target.exists()
        or target.is_symlink()
        or target.suffix != ".mp4"
        or not target.parent.is_dir()
        or target.parent.is_symlink()
    ):
        raise CuratorError("REVIEW_VIDEO_PATH", str(target))
    mask = np.asarray(keep_mask)
    if mask.dtype != np.bool_ or mask.shape != (height, width):
        raise CuratorError("REVIEW_MASK_SHAPE")
    if (
        type(fps) is not int
        or fps <= 0
        or type(expected_frames) is not int
        or expected_frames <= 0
    ):
        raise CuratorError("REVIEW_RENDER_CONTRACT")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}.{secrets.token_hex(4)}.",
        suffix=".mp4",
        dir=target.parent,
    )
    temporary_details = os.fstat(descriptor)
    temporary_identity = (temporary_details.st_dev, temporary_details.st_ino)
    os.close(descriptor)
    temporary = Path(temporary_name)
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width * 3}x{height + REVIEW_HEADER_HEIGHT}",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    count = 0
    temporary_removed = False
    try:
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=errors,
            )
            timed_out = threading.Event()

            def kill_on_timeout() -> None:
                timed_out.set()
                with suppress(ProcessLookupError):
                    process.kill()

            watchdog = threading.Timer(
                REVIEW_FFMPEG_TIMEOUT_SECONDS,
                kill_on_timeout,
            )
            watchdog.daemon = True
            watchdog.start()
            try:
                if process.stdin is None:
                    raise CuratorError("REVIEW_FFMPEG", "stdin unavailable")
                for frame in frames:
                    raw = uint8_hwc(
                        frame.raw_up, width=width, height=height, code="REVIEW_RAW"
                    ).copy()
                    candidate = uint8_hwc(
                        frame.candidate_up,
                        width=width,
                        height=height,
                        code="REVIEW_CANDIDATE",
                    ).copy()
                    overlay = render_keep_overlay(raw, mask, geometry)
                    reason = ",".join(frame.reasons)[:92]
                    detail = (
                        f"{frame.clip_id} ep={frame.episode_index} frame={frame.frame_index} "
                        f"t={frame.timestamp:.3f}s {reason}"
                    )
                    panels = (
                        _panel(raw, "RAW UP", detail),
                        _panel(overlay, "KEEP / GEOMETRY OVERLAY", detail),
                        _panel(candidate, "ACTUAL CANDIDATE H264 DECODE", detail),
                    )
                    process.stdin.write(np.concatenate(panels, axis=1).tobytes())
                    count += 1
                process.stdin.close()
                status = process.wait()
            except BaseException as exc:
                if process.stdin is not None:
                    with suppress(OSError, ValueError):
                        process.stdin.close()
                with suppress(ProcessLookupError):
                    process.kill()
                process.wait(timeout=30)
                if timed_out.is_set():
                    raise CuratorError(
                        "REVIEW_FFMPEG_TIMEOUT",
                        f"{REVIEW_FFMPEG_TIMEOUT_SECONDS}s",
                    ) from exc
                raise
            finally:
                watchdog.cancel()
                watchdog.join()
                if process.stdin is not None:
                    with suppress(OSError, ValueError):
                        process.stdin.close()
            if timed_out.is_set():
                raise CuratorError(
                    "REVIEW_FFMPEG_TIMEOUT",
                    f"{REVIEW_FFMPEG_TIMEOUT_SECONDS}s",
                )
            errors.seek(0)
            detail = errors.read().decode(errors="replace")[-2000:]
        if status != 0 or count != expected_frames:
            raise CuratorError(
                "REVIEW_FFMPEG",
                f"status={status} frames={count}/{expected_frames} {detail}",
            )
        video = verify_review_video(
            temporary,
            width=width,
            height=height + REVIEW_HEADER_HEIGHT,
            frames=count,
            fps=fps,
        )
        video.pop("fps")
        os.chmod(temporary, 0o400, follow_symlinks=False)
        file_fd = os.open(temporary, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(file_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (
                    opened.st_dev,
                    opened.st_ino,
                )
                != temporary_identity
            ):
                raise CuratorError("REVIEW_VIDEO_PATH")
            os.fsync(file_fd)
            parent_fd = os.open(
                target.parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                rename_open_file_noreplace(
                    file_fd,
                    parent_fd,
                    temporary.name,
                    target.name,
                )
                temporary_removed = True
            finally:
                os.close(parent_fd)
        finally:
            os.close(file_fd)
        return video
    except FileExistsError as exc:
        raise CuratorError("REVIEW_VIDEO_EXISTS", str(target)) from exc
    except CuratorError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise CuratorError("REVIEW_FFMPEG", str(exc)) from exc
    finally:
        if not temporary_removed:
            try:
                remove_owned_regular_file(
                    temporary,
                    device=temporary_identity[0],
                    inode=temporary_identity[1],
                )
            except CuratorError:
                pass


__all__ = [
    "ReviewFrame",
    "REVIEW_HEADER_HEIGHT",
    "render_keep_overlay",
    "render_review_mp4",
    "verify_review_video",
]
