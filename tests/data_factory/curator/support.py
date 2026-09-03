"""Synthetic, tempfile-only fixtures for curator tests."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from tools.data_factory.curator.core.identity import file_sha256
from tools.data_factory.curator.core.jsonio import canonical_digest
from tools.data_factory.curator.profile.geometry import (
    build_keep_mask,
    resolve_geometry,
)
from tools.data_factory.curator.profile.registry import (
    ResolvedViewProfile,
    resolve_view_profile,
)
from tools.data_factory.curator.profile.schema import load_view_profile
from tools.data_factory.curator.workflow.application import WorkflowPaths


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def write_rgb(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, payload = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    if not ok:
        raise RuntimeError("PNG encode failed")
    path.write_bytes(payload.tobytes())


def write_mask(path: Path, mask: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, payload = cv2.imencode(".png", mask.astype(np.uint8) * 255)
    if not ok:
        raise RuntimeError("mask encode failed")
    path.write_bytes(payload.tobytes())


def shape(label: str, points: list[list[float]], shape_type: str) -> dict[str, Any]:
    return {
        "label": label,
        "points": points,
        "group_id": None,
        "description": "",
        "shape_type": shape_type,
        "flags": {},
        "mask": None,
    }


@dataclass(frozen=True)
class ProfileFixture:
    paths: WorkflowPaths
    profile_path: Path
    policy_path: Path
    collection_path: Path
    layout_path: Path
    binding_path: Path
    annotation_path: Path
    reference_path: Path
    mask_path: Path
    plate_path: Path
    resolved: ResolvedViewProfile
    mask: np.ndarray


def make_profile_fixture(
    root: Path,
    *,
    width: int = 640,
    height: int = 480,
    verified: bool = True,
) -> ProfileFixture:
    config = root / "config/data_factory"
    profile_root = config / "curator/view_profiles"
    policy_root = config / "curator/review_policies"
    binding_root = config / "region_bindings"
    collection_root = config / "collection_profiles"
    external = root / "external-assets"
    for directory in (
        profile_root,
        policy_root,
        binding_root,
        collection_root,
        external,
    ):
        directory.mkdir(parents=True)

    collection = {
        "schema_version": "data_factory.collection_profile.v2",
        "collection_profile_id": "synthetic-up-wrist-r001",
        "qualification_status": "QUALIFIED",
        "camera_profile": "up-wrist",
        "camera_roles": ["up", "wrist"],
        "camera_serials": {"up": "SYNTHETIC_UP", "wrist": "SYNTHETIC_WRIST"},
        "camera_topics": {
            "up": "/camera/up/color/image_raw",
            "wrist": "/camera/wrist/color/image_raw",
        },
        "fps": 30,
        "width": width,
        "height": height,
        "image_qos": "reliable",
        "image_qos_depth": 10,
        "writer_queue_size": 16,
        "encoder_threads": 1,
        "encoding_mode": "batch",
        "repo_id": "local/synthetic-source",
        "encoder_temp_policy": "DATASET_LOCAL",
        "dataset_incremental_peak_bytes": 1,
        "encoder_temp_peak_bytes": 1,
        "disk_reserve_bytes": 1,
        "portability_status": "QUALIFICATION_REQUIRED",
        "quality_contract_digest": "sha256:" + "a" * 64,
    }
    collection_path = collection_root / "synthetic-up-wrist-r001.json"
    write_json(collection_path, collection)

    layout = {
        "schema_version": "a4_workspace_region_layout.v1",
        "layout_id": "synthetic-place-ab-r001",
        "page_mm": {"width": 100.0, "height": 50.0},
        "origin_xy_mm": [0.0, 0.0],
        "workspace_regions": [
            {
                "place_id": "PLACE_A",
                "region_id": "RED",
                "display_name": "RED",
                "color": "#cc2222",
                "polygon_local_xy_mm": [[10, 10], [90, 10], [90, 40], [10, 40]],
            },
            {
                "place_id": "PLACE_B",
                "region_id": "BLUE",
                "display_name": "BLUE",
                "color": "#2244cc",
                "polygon_local_xy_mm": [[10, 10], [90, 10], [90, 40], [10, 40]],
            },
        ],
    }
    layout["layout_digest"] = canonical_digest(layout)
    layout_path = external / "layout.json"
    write_json(layout_path, layout)

    binding = {
        "schema_version": "data_factory.workspace_region_binding.v1",
        "layout_id": layout["layout_id"],
        "layout_digest": layout["layout_digest"],
        "physical_binding_status": "VERIFIED" if verified else "PREPARED_NOT_VERIFIED",
        "bindings": [
            {"place_id": "PLACE_A", "frame_id": "place-a-r001", "region_id": "RED"},
            {"place_id": "PLACE_B", "frame_id": "place-b-r001", "region_id": "BLUE"},
        ],
        "verified_at": "2026-09-03T00:00:00Z" if verified else None,
        "verified_by": "test-operator" if verified else None,
        "evidence_digest": "sha256:" + "b" * 64 if verified else None,
    }
    binding["binding_digest"] = canonical_digest(binding)
    binding_path = binding_root / "synthetic-place-ab-r001.json"
    write_json(binding_path, binding)

    reference = np.zeros((height, width, 3), dtype=np.uint8)
    reference[..., 0] = np.arange(width, dtype=np.uint16)[None, :] % 256
    reference[..., 1] = np.arange(height, dtype=np.uint16)[:, None] % 256
    reference[..., 2] = 100
    reference_path = external / "reference.png"
    write_rgb(reference_path, reference)
    margin_x = max(4, width // 32)
    margin_y = max(4, height // 24)
    shapes = [
        shape(
            "TABLE_WORK_SURFACE",
            [
                [margin_x, margin_y],
                [width - margin_x - 1, margin_y],
                [width - margin_x - 1, height - margin_y - 1],
                [margin_x, height - margin_y - 1],
            ],
            "polygon",
        ),
        shape(
            "visual_motion_support",
            [
                [1, height * 0.4],
                [margin_x + 2, height * 0.4],
                [margin_x + 2, height * 0.6],
                [1, height * 0.6],
            ],
            "polygon",
        ),
        shape(
            "grounding_context_support",
            [
                [width - margin_x - 2, height * 0.35],
                [width - 2, height * 0.35],
                [width - 2, height * 0.55],
                [width - margin_x - 2, height * 0.55],
            ],
            "polygon",
        ),
    ]
    corners = {
        "PLACE_A": {
            "TL": [width * 0.10, height * 0.15],
            "TR": [width * 0.44, height * 0.12],
            "BR": [width * 0.46, height * 0.72],
            "BL": [width * 0.08, height * 0.75],
        },
        "PLACE_B": {
            "TL": [width * 0.54, height * 0.14],
            "TR": [width * 0.90, height * 0.17],
            "BR": [width * 0.92, height * 0.76],
            "BL": [width * 0.52, height * 0.73],
        },
    }
    for place, values in corners.items():
        for corner, point in values.items():
            shapes.append(shape(f"{place}_{corner}", [point], "point"))
    annotation = {
        "version": "7.0.4",
        "flags": {},
        "shapes": shapes,
        "imagePath": reference_path.name,
        "imageData": None,
        "imageHeight": height,
        "imageWidth": width,
    }
    annotation_path = external / "reference.json"
    write_json(annotation_path, annotation)

    plate = np.full((height, width, 3), (70, 90, 110), dtype=np.uint8)
    plate_path = external / "background.png"
    write_rgb(plate_path, plate)
    mask_path = external / "keep-mask.png"
    placeholder = np.zeros((height, width), dtype=bool)
    placeholder[margin_y : height - margin_y, margin_x : width - margin_x] = True
    write_mask(mask_path, placeholder)

    profile_id = "synthetic-up-view-r001"
    profile = {
        "schema_version": "curator.view_profile.v1",
        "profile_id": profile_id,
        "camera_key": "observation.images.up",
        "width": width,
        "height": height,
        "collection_camera_profile": str(collection_path),
        "collection_camera_profile_digest": canonical_digest(collection),
        "layout_manifest": str(layout_path),
        "layout_manifest_digest": layout["layout_digest"],
        "physical_region_binding": str(binding_path),
        "physical_region_binding_digest": binding["binding_digest"],
        "labelme_annotation": str(annotation_path),
        "labelme_annotation_sha256": file_sha256(annotation_path),
        "labelme_version": "7.0.4",
        "reference_image": str(reference_path),
        "reference_image_sha256": file_sha256(reference_path),
        "reference_frame_index": 0,
        "background_plate_frame_indices": [0],
        "dilation_margin_px": 2,
        "keep_mask": str(mask_path),
        "mask_sha256": file_sha256(mask_path),
        "background_plate": str(plate_path),
        "background_plate_sha256": file_sha256(plate_path),
    }
    profile_path = profile_root / f"{profile_id}.json"
    write_json(profile_path, profile)
    spec = load_view_profile(profile_path)
    geometry, _layout, _binding = resolve_geometry(spec)
    mask = build_keep_mask(geometry, width, height, profile["dilation_margin_px"])
    write_mask(mask_path, mask)
    profile["mask_sha256"] = file_sha256(mask_path)
    write_json(profile_path, profile)

    policy = {
        "schema_version": "curator.review_policy.v1",
        "policy_id": "bounded-default",
        "seed": 7,
        "max_clips": 4,
        "clip_frames": 2,
        "render_fps": 10,
        "max_duration_seconds": 2,
        "relative_time_quantiles": [0.1, 0.5, 0.9],
    }
    policy_path = policy_root / "bounded-default.json"
    write_json(policy_path, policy)
    paths = WorkflowPaths(
        run_root=root / "outputs/curator/runs",
        output_parent=root / "datasets/fr5_curated",
        profile_root=profile_root,
        policy_root=policy_root,
        binding_root=binding_root,
        collection_profile_root=collection_root,
    )
    if verified:
        resolved = resolve_view_profile(
            profile_root,
            binding_root=binding_root,
            collection_profile_root=collection_root,
        )
    else:
        resolved = None  # type: ignore[assignment]
    return ProfileFixture(
        paths,
        profile_path,
        policy_path,
        collection_path,
        layout_path,
        binding_path,
        annotation_path,
        reference_path,
        mask_path,
        plate_path,
        resolved,
        mask,
    )


def write_source_evidence(source: Path, episodes: int, frames_per_episode: int) -> None:
    quality_rows = []
    provenance = source / "meta/source_provenance"
    provenance.mkdir()
    for episode in range(episodes):
        quality_rows.append(
            {
                "episode_index": episode,
                "frames": frames_per_episode,
                "target_fps": 30,
                "effective_fps": 30.0,
                "interval_max_ms": 1000 / 30,
                "writer_queue_drops": 0,
                "alignment_failures": 0,
                "alignment_failure_sources": {
                    "state": 0,
                    "arm_action": 0,
                    "gripper_action": 0,
                    "transport": 0,
                    "image.up": 0,
                    "image.wrist": 0,
                },
                "action_age_max_ms": 1.0,
                "state_age_max_ms": 1.0,
                "camera_time_offsets_ms": {"up": 0.0, "wrist": 0.0},
                "cameras": {
                    camera: {
                        "age_max_ms": 0.0,
                        "transport_age_max_ms": 1.0,
                        "source_fps": 30.0,
                        "repeat_ratio": 0.0,
                        "source_gap_max_ms": 1000 / 30,
                        "color_delta_mean": 10.0,
                        "brightness_mean": 100.0,
                        "clipping_mean": 0.0,
                        "sharpness_median": 100.0,
                    }
                    for camera in ("up", "wrist")
                },
                "image_quality_warnings": [],
            }
        )
        rows = []
        for frame in range(frames_per_episode):
            timestamp = 100.0 + episode * 10 + frame / 30
            rows.append(
                {
                    "frame_index": frame,
                    "enqueue_attempt_index": frame,
                    "target_ros_s": timestamp,
                    "joint_bracket_ros_s": [timestamp - 0.001, timestamp + 0.001],
                    "arm_action_bracket_ros_s": [timestamp - 0.001, timestamp + 0.001],
                    "gripper_action_ros_s": timestamp - 0.001,
                    "image_raw_ros_s": {"up": timestamp, "wrist": timestamp},
                    "image_corrected_ros_s": {"up": timestamp, "wrist": timestamp},
                    "image_received_ros_s": {
                        "up": timestamp + 0.001,
                        "wrist": timestamp + 0.001,
                    },
                }
            )
        (provenance / f"episode-{episode:06d}.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
    (source / "meta/recording_quality.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in quality_rows),
        encoding="utf-8",
    )


def make_source_dataset(
    root: Path,
    *,
    episodes: int = 2,
    frames_per_episode: int = 2,
) -> Path:
    from lerobot.configs.video import RGBEncoderConfig
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from tools.data_factory.curator.dataset.materialize import (
        _require_spawn_parallel_encoding,
    )
    from tools.fr5_dataset_schema import dataset_features

    _require_spawn_parallel_encoding()
    source = root / "source"
    writer = LeRobotDataset.create(
        repo_id="local/source",
        fps=30,
        root=source,
        robot_type="fr5_ros2",
        features=dataset_features(
            fps=30,
            height=480,
            width=640,
            cameras=("up", "wrist"),
            use_videos=True,
        ),
        use_videos=True,
        image_writer_threads=2,
        encoder_threads=1,
        rgb_encoder=RGBEncoderConfig(vcodec="h264", preset="ultrafast", crf=23),
    )
    yy, xx = np.mgrid[:480, :640]
    for episode in range(episodes):
        task = "pick up the cube from the red zone and place it in the blue zone"
        if episode % 2:
            task = "pick up the cube from the blue zone and place it in the red zone"
        for frame in range(frames_per_episode):
            index = episode * frames_per_episode + frame
            up = np.stack(
                (
                    (xx // 4 + index * 2) % 256,
                    (yy // 3 + index) % 256,
                    ((xx + yy) // 6 + index) % 256,
                ),
                axis=-1,
            ).astype(np.uint8)
            wrist = np.stack(
                (
                    (xx // 5 + 20) % 256,
                    (yy // 4 + index * 2) % 256,
                    ((2 * xx + yy) // 10) % 256,
                ),
                axis=-1,
            ).astype(np.uint8)
            state = np.asarray(
                [0.1 * joint + index * 0.001 for joint in range(6)]
                + [0.02 + index * 0.001],
                dtype=np.float32,
            )
            action = np.asarray(
                [0.11 * joint + index * 0.002 for joint in range(6)]
                + [0.021 + index * 0.001],
                dtype=np.float32,
            )
            writer.add_frame(
                {
                    "observation.state": state,
                    "action": action,
                    "observation.images.up": up,
                    "observation.images.wrist": wrist,
                    "task": task,
                }
            )
        writer.save_episode(parallel_encoding=False)
    writer.finalize()
    write_source_evidence(source, episodes, frames_per_episode)
    return source


__all__ = [
    "ProfileFixture",
    "make_profile_fixture",
    "make_source_dataset",
    "shape",
    "write_json",
    "write_mask",
    "write_rgb",
]
