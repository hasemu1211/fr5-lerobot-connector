"""Offline profile authoring without dataset, binding, or approval authority."""

from __future__ import annotations

import os
import secrets
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..core.errors import CuratorError
from ..core.filesystem import (
    OwnedDirectory,
    fsync_directory,
    reject_symlink_components,
    remove_owned_directory,
    remove_owned_regular_file,
    rename_open_file_noreplace,
    write_json_exclusive,
)
from ..core.identity import (
    assert_tree_identity,
    file_sha256,
    read_regular_bytes,
    stable_tree_identity,
)
from ..core.jsonio import DIGEST, SAFE_ID, canonical_digest, exact_fields, load_json
from ..dataset.source import open_source_dataset, validate_source_contract
from ..profile.geometry import (
    build_keep_mask,
    parse_binding,
    parse_layout,
    resolve_geometry,
)
from ..profile.registry import resolve_view_profile
from ..profile.schema import (
    CAMERA_KEY,
    LABELME_VERSION,
    ViewProfileSpec,
    load_view_profile,
)
from ..profile.transform import (
    MAX_BACKGROUND_PLATE_FRAMES,
    apply_up_view,
    make_background_plate,
    uint8_hwc,
)
from ..review.render import ReviewFrame, render_keep_overlay, render_review_mp4

REPOSITORY = Path(__file__).resolve().parents[4]
REQUEST_SCHEMA = "curator.profile_setup_request.v1"
PREVIEW_SCHEMA = "curator.profile_setup_preview.v1"
FINALIZED_SCHEMA = "curator.profile_setup_finalized.v1"
DEFAULT_PROFILE_ID = "fr5-up-wrist-fixed-view-r002"
DEFAULT_DILATION_MARGIN_PX = 12
DEFAULT_PLATE_FRAME_COUNT = MAX_BACKGROUND_PLATE_FRAMES
SETUP_REVIEW_FPS = 10

_REQUEST_FIELDS = {
    "schema_version",
    "setup_id",
    "created_at",
    "profile_id",
    "source",
    "source_repo_id",
    "source_snapshot",
    "source_tree_digest",
    "source_total_episodes",
    "source_total_frames",
    "camera_key",
    "width",
    "height",
    "reference_frame_index",
    "reference_episode_index",
    "reference_episode_frame_index",
    "background_plate_frame_indices",
    "dilation_margin_px",
    "collection_camera_profile",
    "collection_camera_profile_digest",
    "layout_manifest",
    "layout_manifest_digest",
    "physical_region_binding",
    "physical_region_binding_digest",
    "physical_region_assignment_digest",
    "physical_binding_status",
    "asset_directory",
    "reference_image",
    "reference_image_sha256",
    "labelme_annotation",
    "training_authority",
    "request_digest",
}

_PREVIEW_FIELDS = {
    "schema_version",
    "setup_id",
    "preview_id",
    "created_at",
    "purpose",
    "request_digest",
    "source_tree_digest",
    "physical_binding_status",
    "physical_region_assignment_digest",
    "profile_draft",
    "profile_draft_sha256",
    "reference_image_sha256",
    "labelme_annotation_sha256",
    "mask_sha256",
    "background_plate_sha256",
    "boundary_overlay",
    "boundary_overlay_sha256",
    "processed_reference",
    "processed_reference_sha256",
    "review_video",
    "review_video_sha256",
    "review_video_contract",
    "reviewed_global_frame_indices",
    "keep_pixel_fraction",
    "replace_pixel_fraction",
    "artifact_roles",
    "candidate_authority",
    "training_authority",
    "preview_digest",
}

_FINALIZED_FIELDS = {
    "schema_version",
    "setup_id",
    "preview_id",
    "finalized_at",
    "source_tree_digest",
    "preview_digest",
    "physical_region_binding_digest",
    "profile_path",
    "profile_file_sha256",
    "profile_digest",
    "training_authority",
    "receipt_digest",
}


@dataclass(frozen=True)
class ProfileSetupPaths:
    repository: Path
    run_root: Path
    asset_root: Path
    profile_root: Path
    collection_profile: Path
    layout_manifest: Path
    physical_region_binding: Path


def setup_paths(repository: str | Path = REPOSITORY) -> ProfileSetupPaths:
    root = Path(repository).resolve(strict=True)
    return ProfileSetupPaths(
        repository=root,
        run_root=root / "outputs/curator/setup",
        asset_root=root / "datasets/fr5_curator_assets/up-view",
        profile_root=root / "config/data_factory/curator/view_profiles",
        collection_profile=root
        / "config/data_factory/collection_profiles/fr5-up-wrist-rgb-30hz-v2.json",
        layout_manifest=root
        / "tools/a4_place_yaw/zone_artifacts"
        / ("a4_place_a_red_place_b_blue_r002_printcal_096_00mm.json"),
        physical_region_binding=root
        / "config/data_factory/region_bindings/place-a-red-place-b-blue-r002.json",
    )


def _now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _setup_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("profile-%Y%m%dT%H%M%SZ-")
    return timestamp + secrets.token_hex(4)


def _root(path: Path, code: str) -> Path:
    reject_symlink_components(path, code)
    try:
        path.mkdir(parents=True, exist_ok=True)
        result = path.resolve(strict=True)
    except OSError as exc:
        raise CuratorError(code, str(exc)) from exc
    if result.is_symlink() or not result.is_dir():
        raise CuratorError(code, str(result))
    return result


def _new_directory(parent: Path, name: str, code: str) -> Path:
    root = _root(parent, code)
    target = root / name
    reject_symlink_components(target, code)
    try:
        target.mkdir(mode=0o700)
        fsync_directory(root)
    except FileExistsError as exc:
        raise CuratorError(code, f"already exists: {target}") from exc
    except OSError as exc:
        raise CuratorError(code, str(exc)) from exc
    return target


def _write_bytes_exclusive(path: Path, payload: bytes, code: str) -> None:
    reject_symlink_components(path, code)
    if not path.parent.is_dir() or path.parent.is_symlink() or path.exists():
        raise CuratorError(code, str(path))
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    details = os.fstat(descriptor)
    temporary = Path(temporary_name)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            rename_open_file_noreplace(descriptor, parent_fd, temporary.name, path.name)
        finally:
            os.close(parent_fd)
        temporary = None
    except BaseException as exc:
        if temporary is not None:
            try:
                remove_owned_regular_file(
                    temporary, device=details.st_dev, inode=details.st_ino
                )
            except (FileNotFoundError, CuratorError):
                pass
        if isinstance(exc, (CuratorError, KeyboardInterrupt, SystemExit)):
            raise
        raise CuratorError(code, str(exc)) from exc
    finally:
        os.close(descriptor)


def _write_rgb(path: Path, image: np.ndarray, code: str) -> None:
    value = uint8_hwc(image, width=image.shape[1], height=image.shape[0], code=code)
    ok, payload = cv2.imencode(".png", cv2.cvtColor(value, cv2.COLOR_RGB2BGR))
    if not ok:
        raise CuratorError(code, "PNG encode failed")
    _write_bytes_exclusive(path, payload.tobytes(), code)


def _write_mask(path: Path, mask: np.ndarray) -> None:
    if mask.dtype != np.bool_ or mask.ndim != 2:
        raise CuratorError("SETUP_MASK", "bool HxW required")
    ok, payload = cv2.imencode(".png", mask.astype(np.uint8) * 255)
    if not ok:
        raise CuratorError("SETUP_MASK", "PNG encode failed")
    _write_bytes_exclusive(path, payload.tobytes(), "SETUP_MASK")


def _scalar(value: object, code: str, *, integer: bool = False) -> int | float:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    array = np.asarray(value)
    if array.size != 1:
        raise CuratorError(code, str(array.shape))
    result = array.reshape(-1)[0].item()
    if integer:
        if isinstance(result, bool) or not isinstance(result, (int, np.integer)):
            raise CuratorError(code, str(result))
        return int(result)
    try:
        number = float(result)
    except (TypeError, ValueError) as exc:
        raise CuratorError(code, str(result)) from exc
    if not np.isfinite(number):
        raise CuratorError(code, str(result))
    return number


def _image(row: dict[str, Any], width: int, height: int) -> np.ndarray:
    return uint8_hwc(
        row[CAMERA_KEY], width=width, height=height, code="SETUP_SOURCE_IMAGE"
    )


def evenly_spaced_indices(total_frames: int, requested: int) -> list[int]:
    if (
        type(total_frames) is not int
        or total_frames <= 0
        or type(requested) is not int
        or not 1 <= requested <= MAX_BACKGROUND_PLATE_FRAMES
    ):
        raise CuratorError("SETUP_PLATE_INDICES")
    count = min(total_frames, requested)
    if count == 1:
        return [0]
    return sorted(
        {
            round(position * (total_frames - 1) / (count - 1))
            for position in range(count)
        }
    )


def _collection_profile_path(value: str | Path, paths: ProfileSetupPaths) -> Path:
    reject_symlink_components(value, "SETUP_COLLECTION_PROFILE")
    try:
        root = paths.collection_profile.parent.resolve(strict=True)
        path = Path(value).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CuratorError("SETUP_COLLECTION_PROFILE", str(exc)) from exc
    if path.parent != root or path.suffix != ".json" or not path.is_file():
        raise CuratorError("SETUP_COLLECTION_PROFILE", str(path))
    return path


def _canonical_inputs(
    paths: ProfileSetupPaths, *, collection_profile: str | Path | None = None
) -> tuple[dict, dict, dict]:
    collection_path = _collection_profile_path(
        paths.collection_profile if collection_profile is None else collection_profile,
        paths,
    )
    collection = load_json(collection_path, code="SETUP_COLLECTION_PROFILE")
    layout = parse_layout(paths.layout_manifest)
    binding = parse_binding(paths.physical_region_binding, layout)
    return collection, layout, binding


def _binding_assignment_digest(binding: dict) -> str:
    """Identify A/B meaning without treating later verification as a new assignment."""
    return canonical_digest(
        {
            "schema_version": binding["schema_version"],
            "layout_id": binding["layout_id"],
            "layout_digest": binding["layout_digest"],
            "bindings": binding["bindings"],
        }
    )


def _new_preview_id(annotation_digest: str) -> str:
    if DIGEST.fullmatch(annotation_digest) is None:
        raise CuratorError("SETUP_ANNOTATION_DIGEST")
    return (
        "preview-"
        + annotation_digest.removeprefix("sha256:")[:16]
        + "-"
        + secrets.token_hex(4)
    )


def _expected_asset(paths: ProfileSetupPaths, profile_id: str) -> Path:
    return paths.asset_root.resolve(strict=True) / profile_id


def _source(path: str | Path) -> Path:
    reject_symlink_components(path, "SETUP_SOURCE")
    try:
        source = Path(path).resolve(strict=True)
    except OSError as exc:
        raise CuratorError("SETUP_SOURCE", str(exc)) from exc
    if (
        source.is_symlink()
        or not source.is_dir()
        or SAFE_ID.fullmatch(source.name) is None
    ):
        raise CuratorError("SETUP_SOURCE", str(source))
    return source


def _request_path(setup_id: str, paths: ProfileSetupPaths) -> Path:
    if SAFE_ID.fullmatch(setup_id) is None:
        raise CuratorError("SETUP_ID")
    root = paths.run_root.resolve(strict=True)
    target = root / setup_id / "request.json"
    reject_symlink_components(target, "SETUP_RUN")
    return target


def _load_request(setup_id: str, paths: ProfileSetupPaths) -> tuple[Path, dict]:
    path = _request_path(setup_id, paths)
    value = exact_fields(
        load_json(path, code="SETUP_REQUEST_JSON"),
        _REQUEST_FIELDS,
        "SETUP_REQUEST_FIELDS",
    )
    digest = value.get("request_digest")
    unsigned = {key: item for key, item in value.items() if key != "request_digest"}
    snapshot = value.get("source_snapshot")
    indices = value.get("background_plate_frame_indices")
    profile_id = value.get("profile_id")
    source_value = value.get("source")
    asset_value = value.get("asset_directory")
    reference_value = value.get("reference_image")
    annotation_value = value.get("labelme_annotation")
    collection_value = value.get("collection_camera_profile")
    if not isinstance(collection_value, str):
        raise CuratorError("SETUP_REQUEST_CONTRACT")
    collection_path = _collection_profile_path(collection_value, paths)
    digests = (
        value.get("source_tree_digest"),
        value.get("collection_camera_profile_digest"),
        value.get("layout_manifest_digest"),
        value.get("physical_region_binding_digest"),
        value.get("physical_region_assignment_digest"),
        value.get("reference_image_sha256"),
    )
    integers = (
        value.get("source_total_episodes"),
        value.get("source_total_frames"),
        value.get("width"),
        value.get("height"),
        value.get("reference_frame_index"),
        value.get("reference_episode_index"),
        value.get("reference_episode_frame_index"),
        value.get("dilation_margin_px"),
    )
    if (
        value.get("schema_version") != REQUEST_SCHEMA
        or value.get("setup_id") != setup_id
        or not isinstance(profile_id, str)
        or SAFE_ID.fullmatch(profile_id) is None
        or not isinstance(digest, str)
        or DIGEST.fullmatch(digest) is None
        or digest != canonical_digest(unsigned)
        or value.get("camera_key") != CAMERA_KEY
        or value.get("training_authority") is not False
        or any(
            not isinstance(item, str) or DIGEST.fullmatch(item) is None
            for item in digests
        )
        or any(type(item) is not int or item < 0 for item in integers)
        or value.get("source_total_episodes", 0) <= 0
        or value.get("source_total_frames", 0) <= 0
        or (value.get("width"), value.get("height")) != (640, 480)
        or value.get("reference_frame_index", -1) >= value.get("source_total_frames", 0)
        or not 0 <= value.get("dilation_margin_px", -1) <= 256
        or not isinstance(source_value, str)
        or not Path(source_value).is_absolute()
        or value.get("source_repo_id") != f"local/{Path(source_value).name}"
        or not isinstance(asset_value, str)
        or Path(asset_value) != _expected_asset(paths, profile_id)
        or reference_value != str(Path(asset_value) / "reference.png")
        or annotation_value != str(Path(asset_value) / "reference.json")
        or value.get("collection_camera_profile") != str(collection_path)
        or value.get("layout_manifest")
        != str(paths.layout_manifest.resolve(strict=True))
        or value.get("physical_region_binding")
        != str(paths.physical_region_binding.resolve(strict=True))
        or value.get("physical_binding_status")
        not in {"PREPARED_NOT_VERIFIED", "VERIFIED"}
        or not isinstance(snapshot, dict)
        or any(
            not isinstance(name, str)
            or not isinstance(details, list)
            or len(details) != 2
            or any(type(item) is not int or item < 0 for item in details)
            for name, details in snapshot.items()
        )
        or not isinstance(indices, list)
        or indices != sorted(set(indices))
        or not indices
        or len(indices) > MAX_BACKGROUND_PLATE_FRAMES
        or any(type(index) is not int or index < 0 for index in indices)
        or any(index >= value.get("source_total_frames", 0) for index in indices)
    ):
        raise CuratorError("SETUP_REQUEST_CONTRACT")
    return path.parent, value


def export_profile_setup(
    source: str | Path,
    *,
    profile_id: str = DEFAULT_PROFILE_ID,
    reference_frame_index: int = 0,
    dilation_margin_px: int = DEFAULT_DILATION_MARGIN_PX,
    plate_frame_count: int = DEFAULT_PLATE_FRAME_COUNT,
    _paths: ProfileSetupPaths | None = None,
    _setup_id_value: str | None = None,
) -> dict[str, Any]:
    """Freeze exact source/frame choices and export one LabelMe reference PNG."""
    paths = setup_paths() if _paths is None else _paths
    source_path = _source(source)
    if SAFE_ID.fullmatch(profile_id) is None:
        raise CuratorError("SETUP_PROFILE_ID")
    if type(reference_frame_index) is not int or reference_frame_index < 0:
        raise CuratorError("SETUP_REFERENCE_INDEX")
    if type(dilation_margin_px) is not int or not 0 <= dilation_margin_px <= 256:
        raise CuratorError("SETUP_MARGIN")
    collection, layout, binding = _canonical_inputs(paths)
    width, height = collection.get("width"), collection.get("height")
    if type(width) is not int or type(height) is not int:
        raise CuratorError("SETUP_COLLECTION_PROFILE")
    source_snapshot, source_digest = stable_tree_identity(
        source_path, code="SETUP_SOURCE_CHANGED"
    )
    source_repo_id = f"local/{source_path.name}"
    dataset = open_source_dataset(source_path, source_repo_id)
    validate_source_contract(dataset, {"width": width, "height": height})
    total_frames = len(dataset)
    if reference_frame_index >= total_frames:
        raise CuratorError("SETUP_REFERENCE_INDEX", str(reference_frame_index))
    plate_indices = evenly_spaced_indices(total_frames, plate_frame_count)
    row = dataset[reference_frame_index]
    reference = _image(row, width, height)
    reference_episode = _scalar(row["episode_index"], "SETUP_EPISODE", integer=True)
    reference_episode_frame = _scalar(
        row["frame_index"], "SETUP_EPISODE_FRAME", integer=True
    )
    assert_tree_identity(
        source_path, source_snapshot, source_digest, code="SETUP_SOURCE_CHANGED"
    )

    setup_id = _setup_id() if _setup_id_value is None else _setup_id_value
    if SAFE_ID.fullmatch(setup_id) is None:
        raise CuratorError("SETUP_ID")
    run = _new_directory(paths.run_root, setup_id, "SETUP_RUN_CREATE")
    asset = _new_directory(paths.asset_root, profile_id, "SETUP_ASSET_CREATE")
    reference_path = asset / "reference.png"
    annotation_path = asset / "reference.json"
    _write_rgb(reference_path, reference, "SETUP_REFERENCE_WRITE")
    payload = {
        "schema_version": REQUEST_SCHEMA,
        "setup_id": setup_id,
        "created_at": _now(),
        "profile_id": profile_id,
        "source": str(source_path),
        "source_repo_id": source_repo_id,
        "source_snapshot": source_snapshot,
        "source_tree_digest": source_digest,
        "source_total_episodes": dataset.meta.total_episodes,
        "source_total_frames": total_frames,
        "camera_key": CAMERA_KEY,
        "width": width,
        "height": height,
        "reference_frame_index": reference_frame_index,
        "reference_episode_index": reference_episode,
        "reference_episode_frame_index": reference_episode_frame,
        "background_plate_frame_indices": plate_indices,
        "dilation_margin_px": dilation_margin_px,
        "collection_camera_profile": str(paths.collection_profile.resolve(strict=True)),
        "collection_camera_profile_digest": canonical_digest(collection),
        "layout_manifest": str(paths.layout_manifest.resolve(strict=True)),
        "layout_manifest_digest": layout["layout_digest"],
        "physical_region_binding": str(
            paths.physical_region_binding.resolve(strict=True)
        ),
        "physical_region_binding_digest": binding["binding_digest"],
        "physical_region_assignment_digest": _binding_assignment_digest(binding),
        "physical_binding_status": binding["physical_binding_status"],
        "asset_directory": str(asset),
        "reference_image": str(reference_path),
        "reference_image_sha256": file_sha256(reference_path),
        "labelme_annotation": str(annotation_path),
        "training_authority": False,
    }
    payload["request_digest"] = canonical_digest(payload)
    write_json_exclusive(run / "request.json", payload)
    return {
        "ok": True,
        "status": "ANNOTATION_REQUIRED",
        "setup_id": setup_id,
        "reference_image": str(reference_path),
        "labelme_annotation": str(annotation_path),
        "physical_binding_status": binding["physical_binding_status"],
        "training_authority": False,
    }


def _view_spec(
    request: dict,
    annotation: Path,
    mask: Path,
    plate: Path,
    *,
    reference: Path | None = None,
) -> ViewProfileSpec:
    value = {
        "labelme_version": LABELME_VERSION,
        "width": request["width"],
        "height": request["height"],
    }
    return ViewProfileSpec(
        path=Path(),
        value=value,
        collection_profile_path=Path(request["collection_camera_profile"]),
        layout_path=Path(request["layout_manifest"]),
        binding_path=Path(request["physical_region_binding"]),
        annotation_path=annotation,
        reference_image_path=(
            Path(request["reference_image"]) if reference is None else reference
        ),
        keep_mask_path=mask,
        background_plate_path=plate,
    )


def _review_frames(
    rows: Iterable[tuple[int, dict[str, Any], np.ndarray]],
    mask: np.ndarray,
    plate: np.ndarray,
) -> Iterable[ReviewFrame]:
    for dataset_index, row, raw in rows:
        yield ReviewFrame(
            raw_up=raw,
            candidate_up=apply_up_view(raw, mask, plate),
            clip_id="profile-setup",
            episode_index=int(
                _scalar(row["episode_index"], "SETUP_EPISODE", integer=True)
            ),
            frame_index=int(
                _scalar(row["frame_index"], "SETUP_EPISODE_FRAME", integer=True)
            ),
            timestamp=float(_scalar(row["timestamp"], "SETUP_TIMESTAMP")),
            reasons=(f"global_index:{dataset_index}",),
        )


def preview_profile_setup(
    setup_id: str,
    *,
    _paths: ProfileSetupPaths | None = None,
    _preview_id_value: str | None = None,
) -> dict[str, Any]:
    """Compile exact transform assets and review-only evidence from LabelMe JSON."""
    paths = setup_paths() if _paths is None else _paths
    run, request = _load_request(setup_id, paths)
    if any(
        path.exists() or path.is_symlink()
        for path in (run / "view-profile-final.json", run / "finalized.json")
    ):
        raise CuratorError("SETUP_FINALIZATION_STARTED")
    source = Path(request["source"])
    assert_tree_identity(
        source,
        request["source_snapshot"],
        request["source_tree_digest"],
        code="SETUP_SOURCE_CHANGED",
    )
    collection, layout, binding = _canonical_inputs(
        paths, collection_profile=request["collection_camera_profile"]
    )
    if (
        canonical_digest(collection) != request["collection_camera_profile_digest"]
        or str(paths.layout_manifest.resolve(strict=True)) != request["layout_manifest"]
        or layout["layout_digest"] != request["layout_manifest_digest"]
        or str(paths.physical_region_binding.resolve(strict=True))
        != request["physical_region_binding"]
        or binding["binding_digest"] != request["physical_region_binding_digest"]
    ):
        raise CuratorError("SETUP_INPUT_CHANGED")
    asset = Path(request["asset_directory"])
    annotation = Path(request["labelme_annotation"])
    if annotation != asset / "reference.json":
        raise CuratorError("SETUP_ANNOTATION_PATH")
    reject_symlink_components(annotation, "SETUP_ANNOTATION_PATH")
    annotation_digest = file_sha256(annotation)
    preview_id = (
        _new_preview_id(annotation_digest)
        if _preview_id_value is None
        else _preview_id_value
    )
    if SAFE_ID.fullmatch(preview_id) is None:
        raise CuratorError("SETUP_PREVIEW_ID")
    superseded = _superseded_preview_owners(run, request, preview_id)
    working_spec = _view_spec(request, annotation, Path(), Path())
    geometry, _layout, _binding = resolve_geometry(working_spec)
    mask = build_keep_mask(
        geometry,
        request["width"],
        request["height"],
        request["dilation_margin_px"],
    )

    dataset = open_source_dataset(source, request["source_repo_id"])
    validate_source_contract(
        dataset, {"width": request["width"], "height": request["height"]}
    )
    if (
        len(dataset) != request["source_total_frames"]
        or dataset.meta.total_episodes != request["source_total_episodes"]
    ):
        raise CuratorError("SETUP_SOURCE_COUNTS_CHANGED")
    rows = []
    for index in request["background_plate_frame_indices"]:
        if index >= len(dataset):
            raise CuratorError("SETUP_PLATE_INDEX", str(index))
        row = dataset[index]
        rows.append((index, row, _image(row, request["width"], request["height"])))
    plate = make_background_plate([item[2] for item in rows])
    reference_row = dataset[request["reference_frame_index"]]
    reference = _image(reference_row, request["width"], request["height"])
    if (
        _scalar(reference_row["episode_index"], "SETUP_EPISODE", integer=True)
        != request["reference_episode_index"]
        or _scalar(reference_row["frame_index"], "SETUP_EPISODE_FRAME", integer=True)
        != request["reference_episode_frame_index"]
    ):
        raise CuratorError("SETUP_REFERENCE_KEY_CHANGED")
    encoded_reference = cv2.imread(str(request["reference_image"]), cv2.IMREAD_COLOR)
    if (
        encoded_reference is None
        or file_sha256(request["reference_image"]) != request["reference_image_sha256"]
        or not np.array_equal(
            cv2.cvtColor(encoded_reference, cv2.COLOR_BGR2RGB), reference
        )
    ):
        raise CuratorError("SETUP_REFERENCE_SOURCE_MISMATCH")
    assert_tree_identity(
        source,
        request["source_snapshot"],
        request["source_tree_digest"],
        code="SETUP_SOURCE_CHANGED",
    )

    review = _new_directory(run / "previews", preview_id, "SETUP_PREVIEW_CREATE")
    revision = _new_directory(
        asset / "revisions", preview_id, "SETUP_ASSET_REVISION_CREATE"
    )
    reference_path = revision / "reference.png"
    annotation_path = revision / "reference.json"
    mask_path = revision / "keep-mask.png"
    plate_path = revision / "background-plate.png"
    _write_bytes_exclusive(
        reference_path,
        read_regular_bytes(request["reference_image"], code="SETUP_REFERENCE_READ"),
        "SETUP_REFERENCE_SNAPSHOT_WRITE",
    )
    _write_bytes_exclusive(
        annotation_path,
        read_regular_bytes(annotation, code="SETUP_ANNOTATION_READ"),
        "SETUP_ANNOTATION_SNAPSHOT_WRITE",
    )
    if (
        file_sha256(reference_path) != request["reference_image_sha256"]
        or file_sha256(annotation_path) != annotation_digest
    ):
        raise CuratorError("SETUP_ASSET_SNAPSHOT_MISMATCH")
    _write_mask(mask_path, mask)
    _write_rgb(plate_path, plate, "SETUP_PLATE_WRITE")

    spec = _view_spec(
        request,
        annotation_path,
        mask_path,
        plate_path,
        reference=reference_path,
    )
    geometry, _layout, _binding = resolve_geometry(spec)
    profile = {
        "schema_version": "curator.view_profile.v1",
        "profile_id": request["profile_id"],
        "camera_key": CAMERA_KEY,
        "width": request["width"],
        "height": request["height"],
        "collection_camera_profile": request["collection_camera_profile"],
        "collection_camera_profile_digest": request["collection_camera_profile_digest"],
        "layout_manifest": request["layout_manifest"],
        "layout_manifest_digest": request["layout_manifest_digest"],
        "physical_region_binding": request["physical_region_binding"],
        "physical_region_binding_digest": request["physical_region_binding_digest"],
        "labelme_annotation": str(annotation_path),
        "labelme_annotation_sha256": annotation_digest,
        "labelme_version": LABELME_VERSION,
        "reference_image": str(reference_path),
        "reference_image_sha256": request["reference_image_sha256"],
        "reference_frame_index": request["reference_frame_index"],
        "background_plate_frame_indices": request["background_plate_frame_indices"],
        "dilation_margin_px": request["dilation_margin_px"],
        "keep_mask": str(mask_path),
        "mask_sha256": file_sha256(mask_path),
        "background_plate": str(plate_path),
        "background_plate_sha256": file_sha256(plate_path),
    }
    draft_path = review / "view-profile-draft.json"
    write_json_exclusive(draft_path, profile)
    checked = load_view_profile(draft_path)
    checked_geometry, _checked_layout, _checked_binding = resolve_geometry(checked)
    if checked_geometry != geometry:
        raise CuratorError("SETUP_GEOMETRY_CHANGED")

    overlay_path = review / "boundary-overlay.png"
    processed_path = review / "processed-reference.png"
    video_path = review / "boundary-review.mp4"
    _write_rgb(
        overlay_path,
        render_keep_overlay(reference, mask, geometry),
        "SETUP_OVERLAY_WRITE",
    )
    _write_rgb(
        processed_path,
        apply_up_view(reference, mask, plate),
        "SETUP_PROCESSED_WRITE",
    )
    video = render_review_mp4(
        _review_frames(rows, mask, plate),
        video_path,
        keep_mask=mask,
        geometry=geometry,
        width=request["width"],
        height=request["height"],
        fps=SETUP_REVIEW_FPS,
        expected_frames=len(rows),
        candidate_panel_title="TRANSFORM PREVIEW - NOT DATASET",
    )
    assert_tree_identity(
        source,
        request["source_snapshot"],
        request["source_tree_digest"],
        code="SETUP_SOURCE_CHANGED",
    )
    manifest = {
        "schema_version": PREVIEW_SCHEMA,
        "setup_id": setup_id,
        "preview_id": preview_id,
        "created_at": _now(),
        "purpose": "REVIEW_ONLY_NOT_TRAINING_DATA",
        "request_digest": request["request_digest"],
        "source_tree_digest": request["source_tree_digest"],
        "physical_binding_status": binding["physical_binding_status"],
        "physical_region_assignment_digest": request[
            "physical_region_assignment_digest"
        ],
        "profile_draft": str(draft_path),
        "profile_draft_sha256": file_sha256(draft_path),
        "reference_image_sha256": request["reference_image_sha256"],
        "labelme_annotation_sha256": annotation_digest,
        "mask_sha256": profile["mask_sha256"],
        "background_plate_sha256": profile["background_plate_sha256"],
        "boundary_overlay": str(overlay_path),
        "boundary_overlay_sha256": file_sha256(overlay_path),
        "processed_reference": str(processed_path),
        "processed_reference_sha256": file_sha256(processed_path),
        "review_video": str(video_path),
        "review_video_sha256": file_sha256(video_path),
        "review_video_contract": video,
        "reviewed_global_frame_indices": request["background_plate_frame_indices"],
        "keep_pixel_fraction": float(mask.mean()),
        "replace_pixel_fraction": float((~mask).mean()),
        "artifact_roles": {
            "profile_provenance_assets": {
                "location": "EXTERNAL_PROFILE_ASSETS_NOT_EMBEDDED_IN_DATASET",
                "files": [str(annotation_path), str(reference_path)],
                "dataset_effect": "VALIDATION_AND_LINEAGE_DIGESTS_ONLY",
            },
            "training_transform_inputs": {
                "location": "EXTERNAL_PROFILE_ASSETS_NOT_EMBEDDED_IN_DATASET",
                "files": [str(mask_path), str(plate_path)],
                "dataset_effect": "UP_PIXELS_AND_LINEAGE_DIGESTS_ONLY",
            },
            "review_only_outputs": {
                "location": "SETUP_RUN_OUTPUTS",
                "files": [
                    str(overlay_path),
                    str(processed_path),
                    str(video_path),
                ],
                "forbidden_from_dataset": True,
            },
        },
        "candidate_authority": False,
        "training_authority": False,
    }
    manifest["preview_digest"] = canonical_digest(manifest)
    write_json_exclusive(review / "preview.json", manifest)
    checked_preview, _checked_spec = _load_preview(run, request, preview_id)
    if checked_preview != manifest:
        raise CuratorError("SETUP_PREVIEW_COMMIT")
    removed_preview_ids = []
    for old_preview_id, old_review, old_revision in superseded:
        remove_owned_directory(old_review)
        remove_owned_directory(old_revision)
        removed_preview_ids.append(old_preview_id)
    return {
        "ok": True,
        "status": "BOUNDARY_REVIEW_REQUIRED",
        "setup_id": setup_id,
        "preview_id": preview_id,
        "boundary_overlay": str(overlay_path),
        "processed_reference": str(processed_path),
        "review_video": str(video_path),
        "removed_superseded_preview_ids": removed_preview_ids,
        "physical_binding_status": binding["physical_binding_status"],
        "candidate_authority": False,
        "training_authority": False,
    }


def _load_preview(
    run: Path,
    request: dict,
    preview_id: str,
) -> tuple[dict, ViewProfileSpec]:
    if SAFE_ID.fullmatch(preview_id) is None:
        raise CuratorError("SETUP_PREVIEW_ID")
    directory = run / "previews" / preview_id
    reject_symlink_components(directory, "SETUP_PREVIEW_PATH")
    preview = exact_fields(
        load_json(directory / "preview.json", code="SETUP_PREVIEW_JSON"),
        _PREVIEW_FIELDS,
        "SETUP_PREVIEW_FIELDS",
    )
    unsigned = {key: item for key, item in preview.items() if key != "preview_digest"}
    draft_path = directory / "view-profile-draft.json"
    overlay_path = directory / "boundary-overlay.png"
    processed_path = directory / "processed-reference.png"
    video_path = directory / "boundary-review.mp4"
    asset = Path(request["asset_directory"]) / "revisions" / preview_id
    expected_roles = {
        "profile_provenance_assets": {
            "location": "EXTERNAL_PROFILE_ASSETS_NOT_EMBEDDED_IN_DATASET",
            "files": [str(asset / "reference.json"), str(asset / "reference.png")],
            "dataset_effect": "VALIDATION_AND_LINEAGE_DIGESTS_ONLY",
        },
        "training_transform_inputs": {
            "location": "EXTERNAL_PROFILE_ASSETS_NOT_EMBEDDED_IN_DATASET",
            "files": [
                str(asset / "keep-mask.png"),
                str(asset / "background-plate.png"),
            ],
            "dataset_effect": "UP_PIXELS_AND_LINEAGE_DIGESTS_ONLY",
        },
        "review_only_outputs": {
            "location": "SETUP_RUN_OUTPUTS",
            "files": [str(overlay_path), str(processed_path), str(video_path)],
            "forbidden_from_dataset": True,
        },
    }
    if (
        preview.get("schema_version") != PREVIEW_SCHEMA
        or preview.get("setup_id") != request["setup_id"]
        or preview.get("preview_id") != preview_id
        or preview.get("purpose") != "REVIEW_ONLY_NOT_TRAINING_DATA"
        or preview.get("request_digest") != request["request_digest"]
        or preview.get("source_tree_digest") != request["source_tree_digest"]
        or preview.get("physical_binding_status") != request["physical_binding_status"]
        or preview.get("physical_region_assignment_digest")
        != request["physical_region_assignment_digest"]
        or preview.get("profile_draft") != str(draft_path)
        or preview.get("boundary_overlay") != str(overlay_path)
        or preview.get("processed_reference") != str(processed_path)
        or preview.get("review_video") != str(video_path)
        or preview.get("reviewed_global_frame_indices")
        != request["background_plate_frame_indices"]
        or preview.get("artifact_roles") != expected_roles
        or preview.get("candidate_authority") is not False
        or preview.get("training_authority") is not False
        or preview.get("preview_digest") != canonical_digest(unsigned)
    ):
        raise CuratorError("SETUP_PREVIEW_CONTRACT")
    for path, field in (
        (draft_path, "profile_draft_sha256"),
        (overlay_path, "boundary_overlay_sha256"),
        (processed_path, "processed_reference_sha256"),
        (video_path, "review_video_sha256"),
    ):
        if file_sha256(path) != preview[field]:
            raise CuratorError("SETUP_PREVIEW_ARTIFACT_CHANGED", path.name)
    spec = load_view_profile(draft_path)
    expected_assets = (
        asset / "reference.json",
        asset / "reference.png",
        asset / "keep-mask.png",
        asset / "background-plate.png",
    )
    observed_assets = (
        spec.annotation_path,
        spec.reference_image_path,
        spec.keep_mask_path,
        spec.background_plate_path,
    )
    if observed_assets != expected_assets:
        raise CuratorError("SETUP_PREVIEW_ASSET_BOUNDARY")
    if (
        preview["reference_image_sha256"] != spec.value["reference_image_sha256"]
        or preview["labelme_annotation_sha256"]
        != spec.value["labelme_annotation_sha256"]
        or preview["mask_sha256"] != spec.value["mask_sha256"]
        or preview["background_plate_sha256"] != spec.value["background_plate_sha256"]
    ):
        raise CuratorError("SETUP_PREVIEW_PROFILE_DIGEST_CHAIN")
    try:
        keep_fraction = float(preview["keep_pixel_fraction"])
        replace_fraction = float(preview["replace_pixel_fraction"])
    except (TypeError, ValueError) as exc:
        raise CuratorError("SETUP_PREVIEW_COVERAGE") from exc
    if (
        not np.isfinite(keep_fraction)
        or not np.isfinite(replace_fraction)
        or not 0 < keep_fraction < 1
        or abs(keep_fraction + replace_fraction - 1.0) > 1e-12
    ):
        raise CuratorError("SETUP_PREVIEW_COVERAGE")
    return preview, spec


def _superseded_preview_owners(
    run: Path,
    request: dict,
    current_preview_id: str,
) -> list[tuple[str, OwnedDirectory, OwnedDirectory]]:
    """Validate and capture prior unfinalized previews for post-commit removal."""
    root = run / "previews"
    if not root.exists():
        return []
    reject_symlink_components(root, "SETUP_PREVIEW_NAMESPACE")
    if not root.is_dir():
        raise CuratorError("SETUP_PREVIEW_NAMESPACE", str(root))
    result = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if directory.name == current_preview_id:
            continue
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or SAFE_ID.fullmatch(directory.name) is None
        ):
            raise CuratorError("SETUP_PREVIEW_NAMESPACE", str(directory))
        _load_preview(run, request, directory.name)
        revision = Path(request["asset_directory"]) / "revisions" / directory.name
        result.append(
            (
                directory.name,
                OwnedDirectory.capture(directory),
                OwnedDirectory.capture(revision),
            )
        )
    return result


def _finalized_result(
    receipt_path: Path,
    final_path: Path,
    *,
    setup_id: str,
    preview_id: str,
    request: dict,
    preview: dict,
    binding: dict,
) -> dict[str, Any] | None:
    if not receipt_path.exists():
        return None
    receipt = exact_fields(
        load_json(receipt_path, code="SETUP_FINALIZED_JSON"),
        _FINALIZED_FIELDS,
        "SETUP_FINALIZED_FIELDS",
    )
    digest = receipt.get("receipt_digest")
    if (
        receipt.get("schema_version") != FINALIZED_SCHEMA
        or receipt.get("setup_id") != setup_id
        or receipt.get("preview_id") != preview_id
        or receipt.get("source_tree_digest") != request["source_tree_digest"]
        or receipt.get("preview_digest") != preview["preview_digest"]
        or receipt.get("physical_region_binding_digest") != binding["binding_digest"]
        or receipt.get("profile_path") != str(final_path)
        or receipt.get("training_authority") is not False
        or not isinstance(digest, str)
        or digest
        != canonical_digest(
            {key: item for key, item in receipt.items() if key != "receipt_digest"}
        )
        or file_sha256(receipt["profile_path"]) != receipt.get("profile_file_sha256")
    ):
        raise CuratorError("SETUP_FINALIZED_CHANGED")
    return {
        "ok": True,
        "status": "PROFILE_FINALIZED",
        "setup_id": setup_id,
        "preview_id": preview_id,
        "profile_path": receipt["profile_path"],
        "profile_digest": receipt["profile_digest"],
        "training_authority": False,
    }


def finalize_profile_setup(
    setup_id: str,
    preview_id: str,
    *,
    _paths: ProfileSetupPaths | None = None,
) -> dict[str, Any]:
    """Publish one exact preview's config after producer binding verification."""
    paths = setup_paths() if _paths is None else _paths
    run, request = _load_request(setup_id, paths)
    preview, _spec = _load_preview(run, request, preview_id)
    assert_tree_identity(
        request["source"],
        request["source_snapshot"],
        request["source_tree_digest"],
        code="SETUP_SOURCE_CHANGED",
    )
    collection, layout, binding = _canonical_inputs(
        paths, collection_profile=request["collection_camera_profile"]
    )
    if (
        binding["physical_binding_status"] != "VERIFIED"
        or _binding_assignment_digest(binding)
        != request["physical_region_assignment_digest"]
        or layout["layout_digest"] != request["layout_manifest_digest"]
        or canonical_digest(collection) != request["collection_camera_profile_digest"]
    ):
        raise CuratorError("SETUP_PHYSICAL_BINDING_NOT_VERIFIED")

    root = _root(paths.profile_root, "SETUP_PROFILE_ROOT")
    final_path = root / f"{request['profile_id']}.json"
    receipt_path = run / "finalized.json"
    existing = _finalized_result(
        receipt_path,
        final_path,
        setup_id=setup_id,
        preview_id=preview_id,
        request=request,
        preview=preview,
        binding=binding,
    )
    if existing is not None:
        resolved = resolve_view_profile(
            root,
            request["profile_id"],
            binding_root=paths.physical_region_binding.parent,
            collection_profile_root=paths.collection_profile.parent,
        )
        if resolved.profile["profile_digest"] != existing["profile_digest"]:
            raise CuratorError("SETUP_FINALIZED_CHANGED")
        return existing

    draft_path = Path(preview["profile_draft"])
    profile = load_json(draft_path, code="SETUP_PROFILE_DRAFT")
    profile["physical_region_binding_digest"] = binding["binding_digest"]
    candidate_path = run / "view-profile-final.json"
    if candidate_path.exists():
        if load_json(candidate_path, code="SETUP_PROFILE_FINAL") != profile:
            raise CuratorError("SETUP_PROFILE_FINAL_CHANGED")
    else:
        write_json_exclusive(candidate_path, profile)
    load_view_profile(candidate_path)
    assert_tree_identity(
        request["source"],
        request["source_snapshot"],
        request["source_tree_digest"],
        code="SETUP_SOURCE_CHANGED",
    )
    if final_path.exists():
        if file_sha256(final_path) != file_sha256(candidate_path):
            raise CuratorError("SETUP_PROFILE_EXISTS", str(final_path))
    else:
        _write_bytes_exclusive(
            final_path,
            read_regular_bytes(candidate_path, code="SETUP_PROFILE_FINAL_READ"),
            "SETUP_PROFILE_PUBLISH",
        )
    resolved = resolve_view_profile(
        root,
        request["profile_id"],
        binding_root=paths.physical_region_binding.parent,
        collection_profile_root=paths.collection_profile.parent,
    )
    receipt = {
        "schema_version": FINALIZED_SCHEMA,
        "setup_id": setup_id,
        "preview_id": preview_id,
        "finalized_at": _now(),
        "source_tree_digest": request["source_tree_digest"],
        "preview_digest": preview["preview_digest"],
        "physical_region_binding_digest": binding["binding_digest"],
        "profile_path": str(final_path),
        "profile_file_sha256": file_sha256(final_path),
        "profile_digest": resolved.profile["profile_digest"],
        "training_authority": False,
    }
    receipt["receipt_digest"] = canonical_digest(receipt)
    write_json_exclusive(receipt_path, receipt)
    return {
        "ok": True,
        "status": "PROFILE_FINALIZED",
        "setup_id": setup_id,
        "preview_id": preview_id,
        "profile_path": str(final_path),
        "profile_digest": resolved.profile["profile_digest"],
        "training_authority": False,
    }


__all__ = [
    "DEFAULT_DILATION_MARGIN_PX",
    "DEFAULT_PLATE_FRAME_COUNT",
    "DEFAULT_PROFILE_ID",
    "ProfileSetupPaths",
    "evenly_spaced_indices",
    "export_profile_setup",
    "finalize_profile_setup",
    "preview_profile_setup",
    "setup_paths",
]
