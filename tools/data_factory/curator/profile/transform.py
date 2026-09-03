"""Pure NumPy up-camera transform shared by offline and runtime paths."""

from __future__ import annotations

import hashlib
from typing import Any, Sequence

import numpy as np

from tools.data_factory.curator.core.errors import CuratorError


MAX_BACKGROUND_PLATE_FRAMES = 31


def uint8_hwc(
    value: Any, *, width: int, height: int, code: str = "IMAGE"
) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    image = np.asarray(value)
    if image.shape == (3, height, width):
        image = np.moveaxis(image, 0, -1)
    if image.shape != (height, width, 3) or image.dtype != np.uint8:
        raise CuratorError(
            code, f"expected uint8[{height},{width},3], got {image.dtype}{image.shape}"
        )
    return np.ascontiguousarray(image)


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


def apply_up_view(
    raw_up: np.ndarray, keep_mask: np.ndarray, background_plate: np.ndarray
) -> np.ndarray:
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
    return np.where(keep_mask[..., None], raw_up, background_plate).astype(
        np.uint8, copy=False
    )


__all__ = [
    "MAX_BACKGROUND_PLATE_FRAMES",
    "apply_up_view",
    "array_digest",
    "make_background_plate",
    "uint8_hwc",
]
