"""Deterministic up-camera plate compositing and review rendering."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np

from tools.data_factory.curator.contracts import CuratorError


MAX_BACKGROUND_PLATE_FRAMES = 31


def uint8_hwc(value: Any, *, width: int, height: int, code: str = "IMAGE") -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    image = np.asarray(value)
    if image.shape == (3, height, width):
        image = np.moveaxis(image, 0, -1)
    if image.shape != (height, width, 3) or image.dtype != np.uint8:
        raise CuratorError(code, f"expected uint8[{height},{width},3], got {image.dtype}{image.shape}")
    return np.ascontiguousarray(image)


def read_rgb_png(path: str | Path, *, width: int, height: int) -> np.ndarray:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise CuratorError("PNG_READ", f"regular file required: {source}")
    image = cv2.imread(str(source), cv2.IMREAD_COLOR)
    if image is None:
        raise CuratorError("PNG_READ", str(source))
    return uint8_hwc(cv2.cvtColor(image, cv2.COLOR_BGR2RGB), width=width, height=height, code="PNG_SIZE")


def write_rgb_png(path: str | Path, image: np.ndarray) -> None:
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise CuratorError("PNG_EXISTS", str(target))
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        raise CuratorError("PNG_IMAGE")
    if not cv2.imwrite(str(target), cv2.cvtColor(image, cv2.COLOR_RGB2BGR)):
        raise CuratorError("PNG_WRITE", str(target))


def write_mask_png(path: str | Path, mask: np.ndarray) -> None:
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise CuratorError("PNG_EXISTS", str(target))
    if mask.ndim != 2 or mask.dtype != np.bool_:
        raise CuratorError("MASK_IMAGE")
    if not cv2.imwrite(str(target), mask.astype(np.uint8) * 255):
        raise CuratorError("PNG_WRITE", str(target))


def array_digest(value: np.ndarray) -> str:
    array = np.ascontiguousarray(value)
    digest = hashlib.sha256()
    digest.update(str(array.dtype).encode())
    digest.update(b"\0")
    digest.update(",".join(str(item) for item in array.shape).encode())
    digest.update(b"\0")
    digest.update(array.tobytes())
    return "sha256:" + digest.hexdigest()


def make_background_plate(frames: Sequence[np.ndarray]) -> np.ndarray:
    count = len(frames)
    if not 1 <= count <= MAX_BACKGROUND_PLATE_FRAMES:
        raise CuratorError("PLATE_FRAMES")
    shape = frames[0].shape
    if any(frame.dtype != np.uint8 or frame.shape != shape for frame in frames):
        raise CuratorError("PLATE_FRAME_CONTRACT")
    if count == 1:
        return frames[0].copy()
    stacked = np.stack(frames, axis=0)
    upper = count // 2
    if count % 2:
        stacked.partition(upper, axis=0)
        return stacked[upper].copy()
    lower = upper - 1
    stacked.partition((lower, upper), axis=0)
    return (
        (stacked[lower].astype(np.uint16) + stacked[upper].astype(np.uint16)) // 2
    ).astype(np.uint8)


def apply_up_view(raw_up: np.ndarray, keep_mask: np.ndarray, background_plate: np.ndarray) -> np.ndarray:
    """Apply the one pure policy-image transform; wrist is absent by design."""
    if (
        raw_up.dtype != np.uint8
        or raw_up.ndim != 3
        or raw_up.shape[-1] != 3
        or keep_mask.dtype != np.bool_
        or keep_mask.shape != raw_up.shape[:2]
        or background_plate.dtype != np.uint8
        or background_plate.shape != raw_up.shape
    ):
        raise CuratorError("UP_VIEW_INPUT")
    return np.where(keep_mask[..., None], raw_up, background_plate).astype(np.uint8, copy=False)


def render_keep_overlay(raw_up: np.ndarray, keep_mask: np.ndarray) -> np.ndarray:
    if keep_mask.shape != raw_up.shape[:2]:
        raise CuratorError("OVERLAY_INPUT")
    colors = np.empty_like(raw_up)
    colors[keep_mask] = (24, 210, 72)
    colors[~keep_mask] = (220, 48, 170)
    return cv2.addWeighted(raw_up, 0.62, colors, 0.38, 0)


def _outline(image: np.ndarray, polygons: Sequence[Sequence[Sequence[float]]], color: tuple[int, int, int]) -> None:
    for polygon in polygons:
        points = np.rint(np.asarray(polygon, dtype=np.float64)).astype(np.int32)
        cv2.polylines(image, [points], True, color, 2, cv2.LINE_AA)


def render_geometry_overview(raw_up: np.ndarray, geometry: dict[str, Any]) -> np.ndarray:
    image = raw_up.copy()
    _outline(image, [geometry["table_work_surface"]], (255, 220, 40))
    _outline(image, geometry["visual_motion_support"], (40, 220, 255))
    _outline(image, geometry["grounding_context_support"], (255, 145, 35))
    colors = {"PLACE_A": (230, 40, 40), "PLACE_B": (40, 90, 235)}
    for place, polygon in geometry["semantic_subregions"].items():
        _outline(image, [polygon], colors[place])
        anchor = tuple(np.rint(np.asarray(polygon[0])).astype(int))
        cv2.putText(image, place, anchor, cv2.FONT_HERSHEY_SIMPLEX, 0.55, colors[place], 2, cv2.LINE_AA)
    for place, corners in geometry["place_plane_correspondence"].items():
        for name, point in corners.items():
            center = tuple(np.rint(np.asarray(point)).astype(int))
            cv2.circle(image, center, 4, colors[place], -1, cv2.LINE_AA)
            cv2.putText(
                image,
                name,
                (center[0] + 5, center[1] - 5),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                colors[place],
                1,
                cv2.LINE_AA,
            )
    return image


def polygon_crop(image: np.ndarray, polygons: Sequence[Sequence[Sequence[float]]], margin: int = 24) -> np.ndarray:
    points = np.concatenate([np.asarray(polygon, dtype=np.float64) for polygon in polygons], axis=0)
    x0 = max(0, int(np.floor(points[:, 0].min())) - margin)
    y0 = max(0, int(np.floor(points[:, 1].min())) - margin)
    x1 = min(image.shape[1], int(np.ceil(points[:, 0].max())) + margin + 1)
    y1 = min(image.shape[0], int(np.ceil(points[:, 1].max())) + margin + 1)
    if x0 >= x1 or y0 >= y1:
        raise CuratorError("REVIEW_CROP")
    return image[y0:y1, x0:x1].copy()


__all__ = [
    "MAX_BACKGROUND_PLATE_FRAMES",
    "apply_up_view",
    "array_digest",
    "make_background_plate",
    "polygon_crop",
    "read_rgb_png",
    "render_geometry_overview",
    "render_keep_overlay",
    "uint8_hwc",
    "write_mask_png",
    "write_rgb_png",
]
