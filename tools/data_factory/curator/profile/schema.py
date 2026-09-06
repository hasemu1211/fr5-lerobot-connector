"""Strict canonical view-profile and review-policy schemas."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.fr5_data_factory import COLLECTION_PROFILE_V2_KEYS

from ..core.errors import CuratorError
from ..core.filesystem import reject_symlink_components
from ..core.identity import file_sha256
from ..core.jsonio import DIGEST, SAFE_ID, canonical_digest, exact_fields, load_json
from .transform import MAX_BACKGROUND_PLATE_FRAMES
from .fitting import validate_profile_fitting


VIEW_PROFILE_SCHEMA = "curator.view_profile.v1"
FITTED_VIEW_PROFILE_SCHEMA = "curator.view_profile.v2"
REVIEW_POLICY_SCHEMA = "curator.review_policy.v1"
CAMERA_KEY = "observation.images.up"
LABELME_VERSION = "7.0.4"

_VIEW_PROFILE_FIELDS = {
    "schema_version",
    "profile_id",
    "camera_key",
    "width",
    "height",
    "collection_camera_profile",
    "collection_camera_profile_digest",
    "layout_manifest",
    "layout_manifest_digest",
    "physical_region_binding",
    "physical_region_binding_digest",
    "labelme_annotation",
    "labelme_annotation_sha256",
    "labelme_version",
    "reference_image",
    "reference_image_sha256",
    "reference_frame_index",
    "background_plate_frame_indices",
    "dilation_margin_px",
    "keep_mask",
    "mask_sha256",
    "background_plate",
    "background_plate_sha256",
}
_REVIEW_POLICY_FIELDS = {
    "schema_version",
    "policy_id",
    "seed",
    "max_clips",
    "clip_frames",
    "render_fps",
    "max_duration_seconds",
    "relative_time_quantiles",
}


@dataclass(frozen=True)
class ViewProfileSpec:
    """Validated config plus exact, no-symlink asset paths."""

    path: Path
    value: dict[str, Any]
    collection_profile_path: Path
    layout_path: Path
    binding_path: Path
    annotation_path: Path
    reference_image_path: Path
    keep_mask_path: Path
    background_plate_path: Path

    @property
    def asset_paths(self) -> tuple[Path, ...]:
        return (
            self.collection_profile_path,
            self.layout_path,
            self.binding_path,
            self.annotation_path,
            self.reference_image_path,
            self.keep_mask_path,
            self.background_plate_path,
        ) + ((Path(self.value["fitting"]["training_split"]["path"]),)
             if "fitting" in self.value else ())


def _asset_path(base: Path, value: object, code: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CuratorError(code, "path string required")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    reject_symlink_components(candidate, code)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise CuratorError(code, f"{candidate}: {exc}") from exc
    if not resolved.is_file():
        raise CuratorError(code, f"regular file required: {resolved}")
    return resolved


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise CuratorError(code, "sha256 digest required")
    return value


def _validate_collection_profile(
    path: Path, expected_digest: str, width: int, height: int
) -> None:
    profile = exact_fields(
        load_json(path, code="COLLECTION_PROFILE_JSON"),
        COLLECTION_PROFILE_V2_KEYS,
        "COLLECTION_PROFILE_FIELDS",
    )
    if canonical_digest(profile) != expected_digest:
        raise CuratorError("COLLECTION_PROFILE_DIGEST")
    if (
        profile["schema_version"] != "data_factory.collection_profile.v2"
        or not isinstance(profile["collection_profile_id"], str)
        or SAFE_ID.fullmatch(profile["collection_profile_id"]) is None
        or profile["qualification_status"] != "QUALIFIED"
        or not isinstance(profile["quality_contract_digest"], str)
        or DIGEST.fullmatch(profile["quality_contract_digest"]) is None
        or profile["camera_profile"] != "up-wrist"
        or profile["camera_roles"] != ["up", "wrist"]
        or profile["fps"] != 30
        or profile["width"] != width
        or profile["height"] != height
        or profile["image_qos"] not in {"reliable", "best-effort"}
        or profile["encoding_mode"] != "batch"
        or profile["encoder_temp_policy"] != "DATASET_LOCAL"
        or profile["portability_status"]
        not in {"QUALIFICATION_REQUIRED", "SUPPORTED_8GB"}
        or not isinstance(profile["repo_id"], str)
        or not profile["repo_id"]
        or "\x00" in profile["repo_id"]
    ):
        raise CuratorError("COLLECTION_PROFILE_CONTRACT")
    serials = profile["camera_serials"]
    topics = profile["camera_topics"]
    if (
        not isinstance(serials, dict)
        or set(serials) != {"up", "wrist"}
        or any(
            not isinstance(item, str) or not item or "\x00" in item
            for item in serials.values()
        )
        or not isinstance(topics, dict)
        or set(topics) != {"up", "wrist"}
        or any(
            not isinstance(item, str)
            or not item.startswith("/")
            or any(character.isspace() for character in item)
            for item in topics.values()
        )
        or any(
            type(profile[key]) is not int or profile[key] <= 0
            for key in (
                "fps",
                "width",
                "height",
                "image_qos_depth",
                "writer_queue_size",
            )
        )
        or type(profile["encoder_threads"]) is not int
        or profile["encoder_threads"] < 0
        or any(
            type(profile[key]) is not int or profile[key] < 0
            for key in (
                "dataset_incremental_peak_bytes",
                "encoder_temp_peak_bytes",
                "disk_reserve_bytes",
            )
        )
    ):
        raise CuratorError("COLLECTION_PROFILE_CONTRACT")


def load_view_profile(path: str | Path) -> ViewProfileSpec:
    reject_symlink_components(path, "VIEW_PROFILE_PATH")
    try:
        source = Path(path).resolve(strict=True)
    except OSError as exc:
        raise CuratorError("VIEW_PROFILE_PATH", str(exc)) from exc
    document = load_json(source, code="VIEW_PROFILE_JSON")
    fitted = isinstance(document, dict) and document.get("schema_version") == FITTED_VIEW_PROFILE_SCHEMA
    value = exact_fields(
        document,
        _VIEW_PROFILE_FIELDS | ({"fitting"} if fitted else set()),
        "VIEW_PROFILE_FIELDS",
    )
    if value["schema_version"] not in {VIEW_PROFILE_SCHEMA, FITTED_VIEW_PROFILE_SCHEMA}:
        raise CuratorError("VIEW_PROFILE_SCHEMA")
    if (
        not isinstance(value["profile_id"], str)
        or SAFE_ID.fullmatch(value["profile_id"]) is None
    ):
        raise CuratorError("VIEW_PROFILE_ID")
    if value["camera_key"] != CAMERA_KEY:
        raise CuratorError("VIEW_PROFILE_CAMERA_KEY")
    for name in ("width", "height"):
        if type(value[name]) is not int or not 1 <= value[name] <= 16_384:
            raise CuratorError("VIEW_PROFILE_IMAGE_SIZE", name)
    for name in (
        "collection_camera_profile_digest",
        "layout_manifest_digest",
        "physical_region_binding_digest",
        "labelme_annotation_sha256",
        "reference_image_sha256",
        "mask_sha256",
        "background_plate_sha256",
    ):
        _digest(value[name], "VIEW_PROFILE_DIGEST")
    if value["labelme_version"] != LABELME_VERSION:
        raise CuratorError("VIEW_PROFILE_LABELME_VERSION")
    if (
        type(value["reference_frame_index"]) is not int
        or value["reference_frame_index"] < 0
    ):
        raise CuratorError("VIEW_PROFILE_REFERENCE_INDEX")
    indices = value["background_plate_frame_indices"]
    if (
        not isinstance(indices, list)
        or not indices
        or len(indices) > MAX_BACKGROUND_PLATE_FRAMES
        or any(type(index) is not int or index < 0 for index in indices)
        or indices != sorted(set(indices))
    ):
        raise CuratorError("VIEW_PROFILE_PLATE_INDICES")
    if (
        type(value["dilation_margin_px"]) is not int
        or not 0 <= value["dilation_margin_px"] <= 256
    ):
        raise CuratorError("VIEW_PROFILE_MARGIN")
    if fitted:
        validate_profile_fitting(value["fitting"], reference_index=value["reference_frame_index"],
                                 plate_indices=value["background_plate_frame_indices"])

    base = source.parent
    collection = _asset_path(
        base, value["collection_camera_profile"], "VIEW_PROFILE_COLLECTION_PATH"
    )
    layout = _asset_path(base, value["layout_manifest"], "VIEW_PROFILE_LAYOUT_PATH")
    binding = _asset_path(
        base, value["physical_region_binding"], "VIEW_PROFILE_BINDING_PATH"
    )
    annotation = _asset_path(
        base, value["labelme_annotation"], "VIEW_PROFILE_LABELME_PATH"
    )
    reference = _asset_path(
        base, value["reference_image"], "VIEW_PROFILE_REFERENCE_PATH"
    )
    mask = _asset_path(base, value["keep_mask"], "VIEW_PROFILE_MASK_PATH")
    plate = _asset_path(base, value["background_plate"], "VIEW_PROFILE_PLATE_PATH")
    _validate_collection_profile(
        collection,
        value["collection_camera_profile_digest"],
        value["width"],
        value["height"],
    )
    for asset, field, code in (
        (annotation, "labelme_annotation_sha256", "VIEW_PROFILE_LABELME_DIGEST"),
        (reference, "reference_image_sha256", "VIEW_PROFILE_REFERENCE_DIGEST"),
        (mask, "mask_sha256", "VIEW_PROFILE_MASK_DIGEST"),
        (plate, "background_plate_sha256", "VIEW_PROFILE_PLATE_DIGEST"),
    ):
        if file_sha256(asset) != value[field]:
            raise CuratorError(code)
    return ViewProfileSpec(
        source, value, collection, layout, binding, annotation, reference, mask, plate
    )


def load_review_policy(path: str | Path) -> dict[str, Any]:
    value = exact_fields(
        load_json(path, code="REVIEW_POLICY_JSON"),
        _REVIEW_POLICY_FIELDS,
        "REVIEW_POLICY_FIELDS",
    )
    quantiles = value["relative_time_quantiles"]
    if (
        value["schema_version"] != REVIEW_POLICY_SCHEMA
        or not isinstance(value["policy_id"], str)
        or SAFE_ID.fullmatch(value["policy_id"]) is None
        or type(value["seed"]) is not int
        or type(value["max_clips"]) is not int
        or not 1 <= value["max_clips"] <= 32
        or type(value["clip_frames"]) is not int
        or not 1 <= value["clip_frames"] <= 300
        or type(value["render_fps"]) is not int
        or not 1 <= value["render_fps"] <= 60
        or isinstance(value["max_duration_seconds"], bool)
        or not isinstance(value["max_duration_seconds"], (int, float))
        or not 1 <= float(value["max_duration_seconds"]) <= 300
        or not isinstance(quantiles, list)
        or not quantiles
        or len(quantiles) > 9
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not 0 <= float(item) <= 1
            for item in quantiles
        )
        or [float(item) for item in quantiles]
        != sorted(set(float(item) for item in quantiles))
    ):
        raise CuratorError("REVIEW_POLICY_CONTRACT")
    return value


__all__ = [
    "CAMERA_KEY",
    "LABELME_VERSION",
    "REVIEW_POLICY_SCHEMA",
    "VIEW_PROFILE_SCHEMA",
    "ViewProfileSpec",
    "load_review_policy",
    "load_view_profile",
]
