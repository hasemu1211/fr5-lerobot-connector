"""Immutable review-bundle and post-write curator verification."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
from typing import Any

import cv2
import numpy as np

from tools.curator.contracts import (
    DIGEST,
    CuratorError,
    canonical_digest,
    exact_fields,
    file_sha256,
    load_json,
    read_regular_bytes,
    reject_symlink_components,
    rename_noreplace,
    tree_identity,
    tree_snapshot,
    write_json_atomic,
)
from tools.curator.geometry import (
    CAMERA_KEY,
    ProfileRequest,
    build_keep_mask,
    geometry_digests,
    load_profile_request,
    resolve_geometry,
)
from tools.curator.up_view import (
    apply_up_view,
    array_digest,
    make_background_plate,
    polygon_crop,
    read_rgb_png,
    render_geometry_overview,
    render_keep_overlay,
    uint8_hwc,
    write_mask_png,
    write_rgb_png,
)


PROFILE_SCHEMA = "curator.up_view_profile.v1"
BUNDLE_SCHEMA = "curator.task_view_review_bundle.v1"
GEOMETRY_SCHEMA = "curator.task_view_geometry.v1"
_BUNDLE_FILES = {
    "background_plate.png",
    "boundary_motion.png",
    "boundary_place_a.png",
    "boundary_place_b.png",
    "geometry.json",
    "keep_mask.png",
    "overlay.png",
    "policy_up.png",
    "profile.json",
    "reference_up.png",
    "overview.png",
}
_PROFILE_FIELDS = {
    "schema_version",
    "profile_id",
    "camera_key",
    "width",
    "height",
    "collection_camera_profile_digest",
    "layout_manifest_digest",
    "physical_region_binding_digest",
    "physical_binding_status",
    "labelme_annotation_sha256",
    "reference_image_sha256",
    "reference_source_pixel_digest",
    "reference_frame_index",
    "background_plate_frame_indices",
    "dilation_margin_px",
    "place_plane_correspondence_digest",
    "table_work_surface_digest",
    "visual_motion_support_digest",
    "grounding_context_support_digest",
    "semantic_subregions_digest",
    "geometry_sha256",
    "mask_sha256",
    "background_plate_sha256",
    "reference_preview_sha256",
    "profile_digest",
}
CODEC_MAX_FRAME_MAE = 18.0
_LEROBOT_VERSION = "0.6.1"
_LEROBOT_GUARD_LOCK = threading.Lock()


@contextmanager
def _deny_lerobot_hub_fallback():
    """Fence the two private 0.6.1 fallback hooks while opening local data."""
    import lerobot
    import lerobot.datasets.dataset_metadata as metadata_module
    import lerobot.datasets.lerobot_dataset as dataset_module
    from lerobot.datasets.lerobot_dataset import LeRobotDataset, LeRobotDatasetMetadata

    contracts = (
        (LeRobotDataset._download, ("self", "download_videos", "token")),
        (
            LeRobotDatasetMetadata._pull_from_repo,
            ("self", "allow_patterns", "ignore_patterns", "token"),
        ),
    )
    if lerobot.__version__ != _LEROBOT_VERSION or any(
        tuple(inspect.signature(function).parameters) != parameters
        for function, parameters in contracts
    ):
        raise CuratorError("LEROBOT_LOCAL_CONTRACT", "expected LeRobot 0.6.1 fallback hooks")

    def denied(*_args, **_kwargs):
        raise CuratorError("SOURCE_LOCAL_INCOMPLETE", "Hub fallback is disabled")

    with _LEROBOT_GUARD_LOCK:
        original = (
            LeRobotDataset._download,
            LeRobotDatasetMetadata._pull_from_repo,
            dataset_module.get_safe_version,
            metadata_module.get_safe_version,
        )
        LeRobotDataset._download = denied
        LeRobotDatasetMetadata._pull_from_repo = denied
        dataset_module.get_safe_version = denied
        metadata_module.get_safe_version = denied
        try:
            yield LeRobotDataset, LeRobotDatasetMetadata
        finally:
            LeRobotDataset._download = original[0]
            LeRobotDatasetMetadata._pull_from_repo = original[1]
            dataset_module.get_safe_version = original[2]
            metadata_module.get_safe_version = original[3]


def _require_local_file(root: Path, relative: Path) -> None:
    path = root / relative
    reject_symlink_components(path, "SOURCE_LOCAL_INCOMPLETE")
    try:
        details = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise CuratorError("SOURCE_LOCAL_INCOMPLETE", str(relative)) from exc
    if not stat.S_ISREG(details.st_mode) or details.st_size <= 0:
        raise CuratorError("SOURCE_LOCAL_INCOMPLETE", str(relative))


def open_source_dataset(root: Path, repo_id: str):
    required = (
        root / "meta" / "info.json",
        root / "meta" / "stats.json",
        root / "meta" / "tasks.parquet",
    )
    if root.is_symlink() or not root.is_dir() or any(path.is_symlink() or not path.is_file() for path in required):
        raise CuratorError("SOURCE_DATASET", "complete local finalized metadata required")
    if not (root / "meta" / "episodes").is_dir():
        raise CuratorError("SOURCE_DATASET", "episode metadata required")
    try:
        with _deny_lerobot_hub_fallback() as (LeRobotDataset, LeRobotDatasetMetadata):
            metadata = LeRobotDatasetMetadata(
                repo_id,
                root=root,
                force_cache_sync=False,
                token=False,
            )
            try:
                episode_metadata_count = len(metadata.episodes)
            except (TypeError, AttributeError) as exc:
                raise CuratorError("SOURCE_LOCAL_INCOMPLETE", "episode metadata") from exc
            if (
                Path(metadata.root).resolve(strict=True) != root.resolve(strict=True)
                or type(metadata.total_episodes) is not int
                or metadata.total_episodes <= 0
                or episode_metadata_count != metadata.total_episodes
            ):
                raise CuratorError("SOURCE_LOCAL_INCOMPLETE", "episode metadata")
            files = {
                metadata.get_data_file_path(episode)
                for episode in range(metadata.total_episodes)
            }
            files.update(
                metadata.get_video_file_path(episode, key)
                for episode in range(metadata.total_episodes)
                for key in metadata.video_keys
            )
            for relative in files:
                _require_local_file(root, relative)
            return LeRobotDataset(
                repo_id,
                root=root,
                force_cache_sync=False,
                download_videos=False,
                return_uint8=True,
                token=False,
            )
    except CuratorError:
        raise
    except Exception as exc:
        raise CuratorError("SOURCE_READER", str(exc)) from exc


def export_reference(
    source_root: str | Path,
    output_path: str | Path,
    frame_index: int,
    *,
    source_repo_id: str = "local/curator-source",
) -> dict[str, Any]:
    """Exclusive-export one exact 640x480 up frame through the official reader."""
    if type(frame_index) is not int or frame_index < 0:
        raise CuratorError("REFERENCE_FRAME_INDEX")
    source = Path(source_root)
    reject_symlink_components(source, "SOURCE_SYMLINK")
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise CuratorError("SOURCE_DATASET", str(exc)) from exc
    if not source.is_dir():
        raise CuratorError("SOURCE_DATASET", "directory required")

    target = Path(output_path)
    reject_symlink_components(target, "REFERENCE_OUTPUT_SYMLINK")
    if target.suffix != ".png" or target.exists() or target.is_symlink():
        raise CuratorError("REFERENCE_OUTPUT", "new .png path required")
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise CuratorError("REFERENCE_OUTPUT", str(exc)) from exc
    if parent.is_symlink() or not parent.is_dir():
        raise CuratorError("REFERENCE_OUTPUT", "regular parent required")
    target = parent / target.name
    if source == target or source in target.parents or target in source.parents:
        raise CuratorError("SOURCE_ARTIFACT_OVERLAP", str(target))

    before = tree_snapshot(source)
    source_digest, _source_files = tree_identity(source)
    if tree_snapshot(source) != before:
        raise CuratorError("SOURCE_CHANGED_DURING_IDENTITY")
    dataset = open_source_dataset(source, source_repo_id)
    try:
        feature = dataset.meta.features[CAMERA_KEY]
    except (AttributeError, KeyError, TypeError) as exc:
        raise CuratorError("SOURCE_UP_FEATURE", str(exc)) from exc
    if (
        feature.get("dtype") != "video"
        or list(feature.get("shape", [])) != [480, 640, 3]
        or feature.get("names") != ["height", "width", "channels"]
        or not 0 <= frame_index < len(dataset)
    ):
        raise CuratorError("SOURCE_UP_FEATURE")
    try:
        reference = uint8_hwc(
            dataset[frame_index][CAMERA_KEY],
            width=640,
            height=480,
            code="SOURCE_UP_FRAME",
        )
    except CuratorError:
        raise
    except (KeyError, IndexError, RuntimeError, TypeError, ValueError) as exc:
        raise CuratorError("SOURCE_UP_FRAME", f"{frame_index}: {exc}") from exc
    if tree_snapshot(source) != before:
        raise CuratorError("SOURCE_READER_MUTATION")
    ok, encoded = cv2.imencode(".png", cv2.cvtColor(reference, cv2.COLOR_RGB2BGR))
    if not ok:
        raise CuratorError("REFERENCE_ENCODE")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    created = False
    try:
        fd = os.open(target, flags, 0o400)
        created = True
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(encoded.tobytes())
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if tree_snapshot(source) != before:
            raise CuratorError("SOURCE_CHANGED_DURING_REFERENCE_EXPORT")
        return {
            "schema_version": "curator.reference_export.v1",
            "reference_image": str(target),
            "reference_image_sha256": file_sha256(target),
            "reference_source_pixel_digest": array_digest(reference),
            "reference_source_dataset_digest": source_digest,
            "reference_frame_index": frame_index,
            "camera_key": CAMERA_KEY,
            "width": 640,
            "height": 480,
            "training_authority": False,
        }
    except FileExistsError as exc:
        raise CuratorError("REFERENCE_OUTPUT_EXISTS", str(target)) from exc
    except OSError as exc:
        if created:
            target.unlink(missing_ok=True)
        raise CuratorError("REFERENCE_WRITE", f"{target}: {exc}") from exc
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise
    finally:
        if fd >= 0:
            os.close(fd)


def _frame(dataset: Any, index: int, request: ProfileRequest) -> np.ndarray:
    if not 0 <= index < len(dataset):
        raise CuratorError("PROFILE_FRAME_INDEX", str(index))
    try:
        value = dataset[index][CAMERA_KEY]
    except (KeyError, IndexError, RuntimeError, TypeError, ValueError) as exc:
        raise CuratorError("SOURCE_UP_FRAME", f"{index}: {exc}") from exc
    return uint8_hwc(
        value,
        width=request.value["width"],
        height=request.value["height"],
        code="SOURCE_UP_FRAME",
    )


def _geometry_document(request: ProfileRequest, geometry: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": GEOMETRY_SCHEMA,
        "width": request.value["width"],
        "height": request.value["height"],
        **geometry,
    }


def _profile_document(
    request: ProfileRequest,
    geometry: dict[str, Any],
    binding: dict[str, Any],
    bundle: Path,
    reference_pixel_digest: str,
) -> dict[str, Any]:
    value = request.value
    profile = {
        "schema_version": PROFILE_SCHEMA,
        "profile_id": value["profile_id"],
        "camera_key": value["camera_key"],
        "width": value["width"],
        "height": value["height"],
        "collection_camera_profile_digest": value["collection_camera_profile_digest"],
        "layout_manifest_digest": value["layout_manifest_digest"],
        "physical_region_binding_digest": value["physical_region_binding_digest"],
        "physical_binding_status": binding["physical_binding_status"],
        "labelme_annotation_sha256": file_sha256(request.annotation_path),
        "reference_image_sha256": value["reference_image_sha256"],
        "reference_source_pixel_digest": reference_pixel_digest,
        "reference_frame_index": value["reference_frame_index"],
        "background_plate_frame_indices": value["background_plate_frame_indices"],
        "dilation_margin_px": value["dilation_margin_px"],
        **geometry_digests(geometry),
        "geometry_sha256": file_sha256(bundle / "geometry.json"),
        "mask_sha256": file_sha256(bundle / "keep_mask.png"),
        "background_plate_sha256": file_sha256(bundle / "background_plate.png"),
        "reference_preview_sha256": file_sha256(bundle / "reference_up.png"),
    }
    profile["profile_digest"] = canonical_digest(profile)
    return profile


def _publish_directory(temporary: Path, target: Path) -> None:
    lock = target.parent / f".{target.name}.curator-publish.lock"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock, flags, 0o600)
    except FileExistsError as exc:
        raise CuratorError("BUNDLE_PUBLISH_BUSY", str(lock)) from exc
    try:
        os.close(fd)
        if target.exists() or target.is_symlink():
            raise CuratorError("BUNDLE_EXISTS", str(target))
        rename_noreplace(temporary, target, code="BUNDLE_EXISTS")
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def create_review_bundle(
    source_root: str | Path,
    profile_request: str | Path,
    *,
    source_repo_id: str = "local/curator-source",
) -> dict[str, Any]:
    """Create one digest-closed preview bundle without creating authority."""
    request = load_profile_request(profile_request)
    source = Path(source_root)
    reject_symlink_components(source, "SOURCE_SYMLINK")
    source = source.resolve(strict=True)
    for artifact in (request.review_bundle_path, request.approval_path):
        if source == artifact or source in artifact.parents or artifact in source.parents:
            raise CuratorError("SOURCE_ARTIFACT_OVERLAP", str(artifact))
    before = tree_snapshot(source)
    source_digest, _source_files = tree_identity(source)
    if request.review_bundle_path.exists() or request.review_bundle_path.is_symlink():
        raise CuratorError("BUNDLE_EXISTS", str(request.review_bundle_path))
    geometry, _layout, binding = resolve_geometry(request)
    keep_mask = build_keep_mask(
        geometry,
        request.value["width"],
        request.value["height"],
        request.value["dilation_margin_px"],
    )
    dataset = open_source_dataset(source, source_repo_id)
    reference = _frame(dataset, request.value["reference_frame_index"], request)
    annotated_reference = read_rgb_png(
        request.reference_image_path,
        width=request.value["width"],
        height=request.value["height"],
    )
    if not np.array_equal(reference, annotated_reference):
        raise CuratorError("REFERENCE_FRAME_MISMATCH")
    plate = make_background_plate(
        [_frame(dataset, index, request) for index in request.value["background_plate_frame_indices"]]
    )
    policy = apply_up_view(reference, keep_mask, plate)
    overlay = render_keep_overlay(reference, keep_mask)
    overview = render_geometry_overview(reference, geometry)

    temporary = Path(tempfile.mkdtemp(
        prefix=f".{request.review_bundle_path.name}.curator-",
        dir=request.review_bundle_path.parent,
    ))
    try:
        shutil.copyfile(request.reference_image_path, temporary / "reference_up.png")
        write_rgb_png(temporary / "background_plate.png", plate)
        write_mask_png(temporary / "keep_mask.png", keep_mask)
        write_rgb_png(temporary / "overlay.png", overlay)
        write_rgb_png(temporary / "policy_up.png", policy)
        write_rgb_png(temporary / "overview.png", overview)
        write_rgb_png(
            temporary / "boundary_place_a.png",
            polygon_crop(overview, [geometry["semantic_subregions"]["PLACE_A"]]),
        )
        write_rgb_png(
            temporary / "boundary_place_b.png",
            polygon_crop(overview, [geometry["semantic_subregions"]["PLACE_B"]]),
        )
        write_rgb_png(
            temporary / "boundary_motion.png",
            polygon_crop(overview, geometry["visual_motion_support"]),
        )
        write_json_atomic(temporary / "geometry.json", _geometry_document(request, geometry))
        profile = _profile_document(request, geometry, binding, temporary, array_digest(reference))
        write_json_atomic(temporary / "profile.json", profile)
        files = {
            path.name: {"sha256": file_sha256(path), "size": path.stat().st_size}
            for path in sorted(temporary.iterdir())
            if path.is_file()
        }
        if set(files) != _BUNDLE_FILES:
            raise CuratorError("BUNDLE_FILE_SET")
        manifest = {
            "schema_version": BUNDLE_SCHEMA,
            "profile_digest": profile["profile_digest"],
            "reference_source_dataset_digest": source_digest,
            "files": files,
        }
        manifest["review_bundle_digest"] = canonical_digest(manifest)
        write_json_atomic(temporary / "manifest.json", manifest)
        if tree_snapshot(source) != before:
            raise CuratorError("SOURCE_CHANGED_DURING_PREVIEW")
        _publish_directory(temporary, request.review_bundle_path)
        return {
            "profile_path": str(request.review_bundle_path / "profile.json"),
            "profile_digest": profile["profile_digest"],
            "review_bundle_digest": manifest["review_bundle_digest"],
            "physical_binding_status": binding["physical_binding_status"],
            "training_authorized": False,
        }
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _bundle_image(path: Path, width: int, height: int) -> np.ndarray:
    return read_rgb_png(path, width=width, height=height)


def _mask(path: Path, width: int, height: int) -> np.ndarray:
    if path.is_symlink() or not path.is_file():
        raise CuratorError("BUNDLE_MASK")
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None or image.shape != (height, width) or not set(np.unique(image)).issubset({0, 255}):
        raise CuratorError("BUNDLE_MASK")
    return image == 255


def verify_review_bundle(profile_request: str | Path) -> tuple[ProfileRequest, dict[str, Any], dict[str, Any]]:
    request = load_profile_request(profile_request)
    root = request.review_bundle_path
    if root.is_symlink() or not root.is_dir():
        raise CuratorError("BUNDLE_ROOT")
    manifest = exact_fields(
        load_json(root / "manifest.json", code="BUNDLE_MANIFEST"),
        {"schema_version", "profile_digest", "reference_source_dataset_digest", "files", "review_bundle_digest"},
        "BUNDLE_MANIFEST_FIELDS",
    )
    if (
        manifest["schema_version"] != BUNDLE_SCHEMA
        or not isinstance(manifest["profile_digest"], str)
        or DIGEST.fullmatch(manifest["profile_digest"]) is None
        or not isinstance(manifest["reference_source_dataset_digest"], str)
        or DIGEST.fullmatch(manifest["reference_source_dataset_digest"]) is None
        or not isinstance(manifest["review_bundle_digest"], str)
        or DIGEST.fullmatch(manifest["review_bundle_digest"]) is None
        or manifest["review_bundle_digest"] != canonical_digest({key: item for key, item in manifest.items() if key != "review_bundle_digest"})
    ):
        raise CuratorError("BUNDLE_MANIFEST_CONTRACT")
    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != _BUNDLE_FILES:
        raise CuratorError("BUNDLE_FILE_SET")
    actual = {
        path.name for path in root.iterdir()
        if path.name != "manifest.json"
    }
    if actual != _BUNDLE_FILES:
        raise CuratorError("BUNDLE_UNEXPECTED_FILE")
    for name, expected in files.items():
        exact_fields(expected, {"sha256", "size"}, "BUNDLE_FILE_ENTRY")
        path = root / name
        if (
            path.is_symlink()
            or not path.is_file()
            or type(expected["size"]) is not int
            or expected["size"] < 0
            or path.stat().st_size != expected["size"]
            or file_sha256(path) != expected["sha256"]
        ):
            raise CuratorError("BUNDLE_FILE_DIGEST", name)

    profile = exact_fields(load_json(root / "profile.json", code="PROFILE_RESOLVED"), _PROFILE_FIELDS, "PROFILE_RESOLVED_FIELDS")
    if (
        profile["schema_version"] != PROFILE_SCHEMA
        or profile["profile_digest"] != canonical_digest({key: item for key, item in profile.items() if key != "profile_digest"})
        or manifest["profile_digest"] != profile["profile_digest"]
    ):
        raise CuratorError("PROFILE_RESOLVED_DIGEST")
    geometry, _layout, binding = resolve_geometry(request)
    current_geometry = _geometry_document(request, geometry)
    if load_json(root / "geometry.json", code="BUNDLE_GEOMETRY") != current_geometry:
        raise CuratorError("BUNDLE_GEOMETRY_MISMATCH")
    expected_profile = {
        "profile_id": request.value["profile_id"],
        "camera_key": request.value["camera_key"],
        "width": request.value["width"],
        "height": request.value["height"],
        "collection_camera_profile_digest": request.value["collection_camera_profile_digest"],
        "layout_manifest_digest": request.value["layout_manifest_digest"],
        "physical_region_binding_digest": request.value["physical_region_binding_digest"],
        "physical_binding_status": binding["physical_binding_status"],
        "labelme_annotation_sha256": file_sha256(request.annotation_path),
        "reference_image_sha256": request.value["reference_image_sha256"],
        "reference_frame_index": request.value["reference_frame_index"],
        "background_plate_frame_indices": request.value["background_plate_frame_indices"],
        "dilation_margin_px": request.value["dilation_margin_px"],
        **geometry_digests(geometry),
        "geometry_sha256": file_sha256(root / "geometry.json"),
        "mask_sha256": file_sha256(root / "keep_mask.png"),
        "background_plate_sha256": file_sha256(root / "background_plate.png"),
        "reference_preview_sha256": file_sha256(root / "reference_up.png"),
    }
    if any(profile.get(key) != value for key, value in expected_profile.items()):
        raise CuratorError("PROFILE_REQUEST_MISMATCH")
    width, height = profile["width"], profile["height"]
    mask = _mask(root / "keep_mask.png", width, height)
    expected_mask = build_keep_mask(geometry, width, height, profile["dilation_margin_px"])
    if not np.array_equal(mask, expected_mask):
        raise CuratorError("BUNDLE_MASK_MISMATCH")
    reference = _bundle_image(root / "reference_up.png", width, height)
    plate = _bundle_image(root / "background_plate.png", width, height)
    if array_digest(reference) != profile["reference_source_pixel_digest"]:
        raise CuratorError("BUNDLE_REFERENCE_PIXEL_DIGEST")
    policy = _bundle_image(root / "policy_up.png", width, height)
    if not np.array_equal(policy, apply_up_view(reference, mask, plate)):
        raise CuratorError("BUNDLE_POLICY_PREVIEW")
    if not np.array_equal(_bundle_image(root / "overlay.png", width, height), render_keep_overlay(reference, mask)):
        raise CuratorError("BUNDLE_OVERLAY")
    if not np.array_equal(_bundle_image(root / "overview.png", width, height), render_geometry_overview(reference, geometry)):
        raise CuratorError("BUNDLE_OVERVIEW")
    return request, profile, manifest


def _approved_bundle_bytes(
    request: ProfileRequest,
    profile: dict[str, Any],
    manifest: dict[str, Any],
    name: str,
    profile_field: str,
) -> bytes:
    payload = read_regular_bytes(request.review_bundle_path / name, code="BUNDLE_ASSET_READ")
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    expected = manifest["files"].get(name)
    if (
        not isinstance(expected, dict)
        or expected.get("sha256") != digest
        or expected.get("size") != len(payload)
        or profile[profile_field] != digest
    ):
        raise CuratorError("BUNDLE_ASSET_DIGEST", name)
    return payload


def load_profile_assets(
    request: ProfileRequest,
    profile: dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray]:
    """Decode the exact approved no-follow bytes once for the whole derivation."""
    mask_payload = _approved_bundle_bytes(
        request, profile, manifest, "keep_mask.png", "mask_sha256",
    )
    plate_payload = _approved_bundle_bytes(
        request, profile, manifest, "background_plate.png", "background_plate_sha256",
    )
    mask_image = cv2.imdecode(np.frombuffer(mask_payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if (
        mask_image is None
        or mask_image.shape != (profile["height"], profile["width"])
        or not set(np.unique(mask_image)).issubset({0, 255})
    ):
        raise CuratorError("BUNDLE_MASK")
    plate_image = cv2.imdecode(np.frombuffer(plate_payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if plate_image is None:
        raise CuratorError("BUNDLE_ASSET_READ", "background_plate.png")
    mask = mask_image == 255
    plate = uint8_hwc(
        cv2.cvtColor(plate_image, cv2.COLOR_BGR2RGB),
        width=profile["width"],
        height=profile["height"],
        code="PNG_SIZE",
    )
    return mask, plate


def _numpy(value: Any) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    return np.asarray(value)


def _scalar(value: Any, code: str) -> Any:
    array = _numpy(value)
    if array.size != 1:
        raise CuratorError(code)
    return array.reshape(-1)[0].item()


def _frame_mae(left: np.ndarray, right: np.ndarray, pixels: np.ndarray | None = None) -> float:
    delta = np.abs(left.astype(np.int16) - right.astype(np.int16))
    if pixels is not None:
        delta = delta[pixels]
    return float(delta.mean()) if delta.size else 0.0


def _image_metrics(image: np.ndarray) -> tuple[float, float, float, float]:
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


def _verify_h264(root: Path) -> list[str]:
    files = sorted((root / "videos").rglob("*.mp4"))
    if not files:
        raise CuratorError("DERIVED_H264", "no MP4 files")
    relative: list[str] = []
    for path in files:
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name", "-of", "json", str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            streams = json.loads(result.stdout).get("streams", [])
        except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            raise CuratorError("DERIVED_H264", f"{path}: {exc}") from exc
        if len(streams) != 1 or streams[0].get("codec_name") != "h264":
            raise CuratorError("DERIVED_H264", f"{path}: {streams}")
        relative.append(path.relative_to(root).as_posix())
    return relative


def verify_derived_dataset(
    source_dataset: Any,
    derived_root: str | Path,
    *,
    derived_repo_id: str,
    profile: dict[str, Any],
    keep_mask: np.ndarray,
    background_plate: np.ndarray,
) -> dict[str, Any]:
    """Full official-loader comparison before atomic publication."""
    try:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        derived = LeRobotDataset(derived_repo_id, root=Path(derived_root), return_uint8=True)
    except Exception as exc:
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
    image_metrics: dict[int, dict[str, list[tuple[float, float, float, float]]]] = {}
    for index in range(len(source_dataset)):
        try:
            source = source_dataset[index]
            output = derived[index]
        except Exception as exc:
            raise CuratorError("DERIVED_FULL_DECODE", f"frame {index}: {exc}") from exc
        for key in ("index", "episode_index", "frame_index", "task_index"):
            if int(_scalar(source[key], "SOURCE_INDEX")) != int(_scalar(output[key], "DERIVED_INDEX")):
                raise CuratorError("DERIVED_INDEX_PRESERVATION", f"{key}:{index}")
        source_timestamp = float(_scalar(source["timestamp"], "SOURCE_TIMESTAMP"))
        output_timestamp = float(_scalar(output["timestamp"], "DERIVED_TIMESTAMP"))
        if abs(source_timestamp - output_timestamp) > 1e-7 or source["task"] != output["task"]:
            raise CuratorError("DERIVED_TASK_TIMESTAMP_PRESERVATION", str(index))
        for key in ("observation.state", "action"):
            if not np.array_equal(_numpy(source[key]), _numpy(output[key])):
                raise CuratorError("DERIVED_NUMERIC_PRESERVATION", f"{key}:{index}")
        raw_up = uint8_hwc(
            source["observation.images.up"],
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
            output["observation.images.up"],
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
        frame_metrics = {
            "up_keep_mae": _frame_mae(output_up, expected_up, keep_mask),
            "up_replace_mae": _frame_mae(output_up, expected_up, ~keep_mask),
            "wrist_mae": _frame_mae(output_wrist, raw_wrist),
        }
        for key, value in frame_metrics.items():
            maxima[key] = max(maxima[key], value)
            if value > CODEC_MAX_FRAME_MAE:
                raise CuratorError("DERIVED_CODEC_BASELINE", f"{key}={value:.3f} frame={index}")
        episode = int(_scalar(source["episode_index"], "SOURCE_EPISODE"))
        if episode not in mapping:
            mapping[episode] = {
                "episode_index": episode,
                "source_from_index": index,
                "derived_from_index": index,
                "frames": 0,
            }
        mapping[episode]["frames"] += 1
        episode_metrics = image_metrics.setdefault(episode, {"up": [], "wrist": []})
        episode_metrics["up"].append(_image_metrics(output_up))
        episode_metrics["wrist"].append(_image_metrics(output_wrist))
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
        cameras: dict[str, dict[str, float]] = {}
        for camera, samples in image_metrics[episode].items():
            values = np.asarray(samples, dtype=np.float64)
            cameras[camera] = {
                "color_delta_mean": float(values[:, 0].mean()),
                "brightness_mean": float(values[:, 1].mean()),
                "clipping_mean": float(values[:, 2].mean()),
                "sharpness_median": float(np.median(values[:, 3])),
            }
        derived_image_metrics.append({"episode_index": episode, "cameras": cameras})
    h264_files = _verify_h264(Path(derived_root))
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
    "BUNDLE_SCHEMA",
    "CODEC_MAX_FRAME_MAE",
    "GEOMETRY_SCHEMA",
    "PROFILE_SCHEMA",
    "create_review_bundle",
    "export_reference",
    "load_profile_assets",
    "open_source_dataset",
    "verify_derived_dataset",
    "verify_review_bundle",
]
