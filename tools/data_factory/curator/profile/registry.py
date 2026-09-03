"""Canonical profile/policy resolution and exact external-asset validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

import cv2
import numpy as np

from ..core.errors import CuratorError
from ..core.filesystem import reject_symlink_components
from ..core.identity import file_sha256, read_regular_bytes
from ..core.jsonio import SAFE_ID, canonical_digest
from .geometry import build_keep_mask, geometry_digests, resolve_geometry
from .schema import ViewProfileSpec, load_review_policy, load_view_profile
from .transform import uint8_hwc


REPOSITORY = Path(__file__).resolve().parents[4]
CANONICAL_BINDING_ROOT = REPOSITORY / "config/data_factory/region_bindings"
CANONICAL_COLLECTION_PROFILE_ROOT = (
    REPOSITORY / "config/data_factory/collection_profiles"
)

T = TypeVar("T")


@dataclass(frozen=True)
class ResolvedViewProfile:
    config_path: Path
    config_file_sha256: str
    spec: ViewProfileSpec
    geometry: dict
    layout: dict
    binding: dict
    profile: dict


def _canonical_root(path: str | Path, code: str) -> Path:
    reject_symlink_components(path, code)
    try:
        root = Path(path).resolve(strict=True)
    except OSError as exc:
        raise CuratorError(code, str(exc)) from exc
    if not root.is_dir():
        raise CuratorError(code, f"canonical directory required: {root}")
    return root


def _contained_file(path: Path, root: Path, code: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise CuratorError(code, f"{path} is outside {root}") from exc


def _resolve_one(
    root: str | Path,
    identifier: str | None,
    loader: Callable[[Path], T],
    code: str,
) -> tuple[Path, T]:
    canonical = _canonical_root(root, code)
    if identifier is not None and SAFE_ID.fullmatch(identifier) is None:
        raise CuratorError(code, "unsafe identifier")
    paths = (
        [canonical / f"{identifier}.json"]
        if identifier is not None
        else sorted(canonical.glob("*.json"))
    )
    matches: list[tuple[Path, T]] = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise CuratorError(code, f"regular canonical file required: {path}")
        matches.append((path.resolve(strict=True), loader(path)))
    if len(matches) != 1:
        raise CuratorError(code, f"expected one match, found {len(matches)}")
    return matches[0]


def _decode_rgb(path: Path, *, width: int, height: int, code: str) -> np.ndarray:
    payload = read_regular_bytes(path, code=code)
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise CuratorError(code, str(path))
    return uint8_hwc(
        cv2.cvtColor(image, cv2.COLOR_BGR2RGB),
        width=width,
        height=height,
        code=code,
    )


def _decode_mask(path: Path, *, width: int, height: int) -> np.ndarray:
    payload = read_regular_bytes(path, code="VIEW_PROFILE_MASK_READ")
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if (
        image is None
        or image.shape != (height, width)
        or not set(np.unique(image)).issubset({0, 255})
    ):
        raise CuratorError("VIEW_PROFILE_MASK_CONTRACT")
    return image == 255


def _resolve_profile(
    path: Path,
    spec: ViewProfileSpec,
    *,
    binding_root: str | Path,
    collection_profile_root: str | Path,
) -> ResolvedViewProfile:
    if path.stem != spec.value["profile_id"]:
        raise CuratorError("VIEW_PROFILE_FILENAME")
    canonical_binding = _canonical_root(binding_root, "BINDING_REGISTRY")
    canonical_collection = _canonical_root(
        collection_profile_root, "COLLECTION_PROFILE_REGISTRY"
    )
    _contained_file(
        spec.binding_path, canonical_binding, "VERIFIED_BINDING_NOT_CANONICAL"
    )
    _contained_file(
        spec.collection_profile_path,
        canonical_collection,
        "COLLECTION_PROFILE_NOT_CANONICAL",
    )

    geometry, layout, binding = resolve_geometry(spec)
    if layout["layout_digest"] != spec.value["layout_manifest_digest"]:
        raise CuratorError("VIEW_PROFILE_LAYOUT_DIGEST")
    if binding["binding_digest"] != spec.value["physical_region_binding_digest"]:
        raise CuratorError("VIEW_PROFILE_BINDING_DIGEST")
    if binding["physical_binding_status"] != "VERIFIED":
        raise CuratorError("PHYSICAL_BINDING_NOT_VERIFIED")

    width, height = spec.value["width"], spec.value["height"]
    mask = _decode_mask(spec.keep_mask_path, width=width, height=height)
    expected_mask = build_keep_mask(
        geometry, width, height, spec.value["dilation_margin_px"]
    )
    if not np.array_equal(mask, expected_mask):
        raise CuratorError("VIEW_PROFILE_MASK_GEOMETRY_MISMATCH")
    _decode_rgb(
        spec.background_plate_path,
        width=width,
        height=height,
        code="VIEW_PROFILE_PLATE_CONTRACT",
    )
    _decode_rgb(
        spec.reference_image_path,
        width=width,
        height=height,
        code="VIEW_PROFILE_REFERENCE_CONTRACT",
    )

    profile = {
        "schema_version": "curator.resolved_view_profile.v1",
        "profile_id": spec.value["profile_id"],
        "camera_key": spec.value["camera_key"],
        "width": width,
        "height": height,
        "collection_camera_profile_digest": spec.value[
            "collection_camera_profile_digest"
        ],
        "collection_camera_profile_binding_status": "DECLARED_CONFIG_OBSERVABLE_MATCH",
        "layout_manifest_digest": spec.value["layout_manifest_digest"],
        "physical_region_binding_digest": spec.value["physical_region_binding_digest"],
        "physical_binding_status": binding["physical_binding_status"],
        "labelme_annotation_sha256": spec.value["labelme_annotation_sha256"],
        "reference_image_sha256": spec.value["reference_image_sha256"],
        "reference_frame_index": spec.value["reference_frame_index"],
        "background_plate_frame_indices": spec.value["background_plate_frame_indices"],
        "dilation_margin_px": spec.value["dilation_margin_px"],
        "mask_sha256": spec.value["mask_sha256"],
        "background_plate_sha256": spec.value["background_plate_sha256"],
        **geometry_digests(geometry),
    }
    profile["profile_digest"] = canonical_digest(profile)
    return ResolvedViewProfile(
        config_path=path,
        config_file_sha256=file_sha256(path),
        spec=spec,
        geometry=geometry,
        layout=layout,
        binding=binding,
        profile=profile,
    )


def resolve_view_profile(
    root: str | Path,
    profile_id: str | None = None,
    *,
    binding_root: str | Path = CANONICAL_BINDING_ROOT,
    collection_profile_root: str | Path = CANONICAL_COLLECTION_PROFILE_ROOT,
) -> ResolvedViewProfile:
    path, spec = _resolve_one(
        root, profile_id, load_view_profile, "VIEW_PROFILE_RESOLUTION"
    )
    return _resolve_profile(
        path,
        spec,
        binding_root=binding_root,
        collection_profile_root=collection_profile_root,
    )


def resolve_review_policy(
    root: str | Path, policy_id: str | None = None
) -> tuple[Path, dict]:
    path, value = _resolve_one(
        root, policy_id, load_review_policy, "REVIEW_POLICY_RESOLUTION"
    )
    if path.stem != value["policy_id"]:
        raise CuratorError("REVIEW_POLICY_FILENAME")
    return path, value


def load_profile_assets(resolved: ResolvedViewProfile) -> tuple[np.ndarray, np.ndarray]:
    """Re-read exact external bytes before each long-lived lifecycle boundary."""
    value = resolved.spec.value
    if (
        file_sha256(resolved.spec.keep_mask_path) != value["mask_sha256"]
        or file_sha256(resolved.spec.background_plate_path)
        != value["background_plate_sha256"]
    ):
        raise CuratorError("VIEW_PROFILE_ASSET_CHANGED")
    mask = _decode_mask(
        resolved.spec.keep_mask_path,
        width=value["width"],
        height=value["height"],
    )
    expected = build_keep_mask(
        resolved.geometry,
        value["width"],
        value["height"],
        value["dilation_margin_px"],
    )
    if not np.array_equal(mask, expected):
        raise CuratorError("VIEW_PROFILE_MASK_GEOMETRY_MISMATCH")
    plate = _decode_rgb(
        resolved.spec.background_plate_path,
        width=value["width"],
        height=value["height"],
        code="VIEW_PROFILE_PLATE_CONTRACT",
    )
    return mask, plate


__all__ = [
    "CANONICAL_BINDING_ROOT",
    "CANONICAL_COLLECTION_PROFILE_ROOT",
    "ResolvedViewProfile",
    "load_profile_assets",
    "resolve_review_policy",
    "resolve_view_profile",
]
