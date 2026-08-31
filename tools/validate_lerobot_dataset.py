#!/usr/bin/env python3
"""Validate FR5 LeRobot structure and source evidence; report image-content diagnostics as warnings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

import cv2
import numpy as np
import pyarrow.parquet as pq

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.fr5_dataset_schema import FEATURE_NAMES, QUALITY_LIMITS
from tools.data_factory.episode_ledger import build_lerobot_v3_episode_locator
from tools.fr5_data_factory import ContractError


EPISODE_LOCATOR_PREFIX = "EPISODE_LOCATOR_JSON:"


def has_nonfinite_number(value) -> bool:
    if isinstance(value, dict):
        return any(has_nonfinite_number(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_nonfinite_number(item) for item in value)
    return isinstance(value, (int, float)) and not isinstance(value, bool) and not np.isfinite(value)


def image_metrics(image: np.ndarray) -> tuple[float, float, float, float]:
    if image.shape[0] == 3:
        image = np.moveaxis(image, 0, -1)
    image = image.astype(np.uint8)
    value = image.astype(np.float32)
    color = float((np.abs(value[..., 0] - value[..., 1]).mean() + np.abs(value[..., 1] - value[..., 2]).mean()) / 2)
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    return color, float(gray.mean()), float(((gray <= 5) | (gray >= 250)).mean()), float(cv2.Laplacian(gray, cv2.CV_64F).var())


def episode_locator(info: dict, episode_rows: list[dict], episode_index: int, repo_id: str) -> dict:
    """Build the canonical ledger locator from metadata already read by this validator."""
    matches = [row for row in episode_rows if int(row["episode_index"]) == episode_index]
    if len(matches) != 1:
        raise ContractError("EPISODE_LEDGER_LOCATOR_EPISODE")
    episode = matches[0]
    data_chunk = int(episode["data/chunk_index"])
    data_file = int(episode["data/file_index"])
    shard = [
        row for row in episode_rows
        if int(row["data/chunk_index"]) == data_chunk
        and int(row["data/file_index"]) == data_file
    ]
    file_base = min(int(row["dataset_from_index"]) for row in shard)
    dataset_from = int(episode["dataset_from_index"])
    dataset_to = int(episode["dataset_to_index"])
    rows = dataset_to - dataset_from
    if rows <= 0 or int(episode["length"]) != rows:
        raise ContractError("EPISODE_LEDGER_LOCATOR_RANGE")
    fps = int(info["fps"])
    videos = []
    for camera_key in sorted(
        key for key, feature in info["features"].items()
        if isinstance(feature, dict) and feature.get("dtype") == "video"
    ):
        prefix = f"videos/{camera_key}"
        chunk_index = int(episode[f"{prefix}/chunk_index"])
        file_index = int(episode[f"{prefix}/file_index"])
        timestamp_start = float(episode[f"{prefix}/from_timestamp"])
        timestamp_end = float(episode[f"{prefix}/to_timestamp"])
        frame_start = round(timestamp_start * fps)
        frame_end = round(timestamp_end * fps)
        if frame_end - frame_start != rows:
            raise ContractError("EPISODE_LEDGER_LOCATOR_RANGE")
        videos.append({
            "camera_key": camera_key,
            "chunk_index": chunk_index,
            "file_index": file_index,
            "relative_path": info["video_path"].format(
                video_key=camera_key,
                chunk_index=chunk_index,
                file_index=file_index,
            ),
            "file_frame_start": frame_start,
            "file_frame_end_exclusive": frame_end,
            "timestamp_start_s": timestamp_start,
            "timestamp_end_s": timestamp_end,
        })
    return build_lerobot_v3_episode_locator(
        repo_id=repo_id,
        episode_index=episode_index,
        data={
            "chunk_index": data_chunk,
            "file_index": data_file,
            "relative_path": info["data_path"].format(
                chunk_index=data_chunk, file_index=data_file,
            ),
            "file_row_start": dataset_from - file_base,
            "file_row_end_exclusive": dataset_to - file_base,
        },
        videos=videos,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--repo-id", default="local/fr5_smolvla")
    parser.add_argument("--expected-fps", type=int)
    parser.add_argument("--require-hil-motion", action="store_true")
    parser.add_argument(
        "--require-alignment-tail", action="store_true",
        help="require each episode to durably reach its recorder freeze watermark",
    )
    parser.add_argument("--min-arm-range", type=float, default=0.01)
    parser.add_argument("--min-gripper-range", type=float, default=0.001)
    parser.add_argument(
        "--skip-decoded-image-diagnostics", action="store_true",
        help="skip duplicate warning-only image sampling; structural video decoding remains mandatory",
    )
    parser.add_argument(
        "--episode-locator-index", type=int,
        help="emit the canonical locator for one validated episode",
    )
    args = parser.parse_args()
    quarantine = args.root / "meta" / "quarantine.json"
    if quarantine.exists() or quarantine.is_symlink():
        raise SystemExit(f"FAIL: dataset is quarantined: {quarantine}")
    if not np.isfinite([args.min_arm_range, args.min_gripper_range]).all() or min(args.min_arm_range, args.min_gripper_range) <= 0:
        raise SystemExit("HIL motion thresholds must be finite and positive")
    failures: list[str] = []
    warnings: list[str] = []
    info_path = args.root / "meta" / "info.json"
    if not info_path.exists():
        raise SystemExit(f"FAIL: {args.root} is not a LeRobot dataset root")
    info = json.loads(info_path.read_text())
    if info.get("codebase_version") != "v3.0": failures.append("dataset is not LeRobot v3.0")
    fps = info.get("fps")
    if not isinstance(fps, int) or fps <= 0: failures.append(f"invalid dataset fps: {fps}")
    if args.expected_fps is not None and fps != args.expected_fps:
        failures.append(f"fps is {fps}, expected {args.expected_fps}")
    if info.get("robot_type") != "fr5_ros2": failures.append(f"robot_type is {info.get('robot_type')}")
    for key in ("observation.state", "action"):
        feature = info.get("features", {}).get(key, {})
        if feature.get("shape") != [7] or feature.get("names") != FEATURE_NAMES:
            failures.append(f"{key} is not the FR5 7D contract")
    camera_keys = sorted(key for key in info.get("features", {}) if key.startswith("observation.images."))
    if not camera_keys: failures.append("no RGB camera feature")
    for key in camera_keys:
        feature = info["features"][key]
        if feature.get("dtype") not in ("video", "image") or feature.get("shape") != [480, 640, 3]:
            failures.append(f"{key} is not RGB 640x480")
    camera_names = [key.removeprefix("observation.images.") for key in camera_keys]
    if info.get("total_episodes", 0) < 1: failures.append("no saved episodes")

    tasks_path = args.root / "meta" / "tasks.parquet"
    valid_task_indices: set[int] = set()
    if not tasks_path.exists():
        failures.append("tasks.parquet missing")
    else:
        task_rows = pq.read_table(tasks_path, columns=["task_index", "task"]).to_pylist()
        valid_task_indices = {int(row["task_index"]) for row in task_rows if str(row["task"]).strip()}
        if len(valid_task_indices) != info.get("total_tasks", 0):
            failures.append("task metadata count or text is invalid")

    episode_metadata_files = sorted((args.root / "meta" / "episodes").rglob("*.parquet"))
    expected_episodes: dict[int, int] = {}
    episode_rows: list[dict] = []
    if not episode_metadata_files:
        failures.append("episode metadata parquet missing")
    else:
        episode_columns = [
            "episode_index", "tasks", "length", "data/chunk_index", "data/file_index",
            "dataset_from_index", "dataset_to_index",
        ]
        for key in camera_keys:
            if info["features"][key].get("dtype") == "video":
                episode_columns.extend([
                    f"videos/{key}/chunk_index", f"videos/{key}/file_index",
                    f"videos/{key}/from_timestamp", f"videos/{key}/to_timestamp",
                ])
        for path in episode_metadata_files:
            episode_rows.extend(pq.read_table(path, columns=episode_columns).to_pylist())
        for row in episode_rows:
            episode_index = int(row["episode_index"])
            length = int(row["length"])
            if episode_index in expected_episodes:
                failures.append(f"duplicate episode metadata for episode {episode_index}")
            expected_episodes[episode_index] = length
            if int(row["dataset_to_index"]) - int(row["dataset_from_index"]) != length:
                failures.append(f"episode {episode_index} metadata range does not match length {length}")
            if not row.get("tasks") or not all(str(task).strip() for task in row["tasks"]):
                failures.append(f"episode {episode_index} has no non-empty task")
        if set(expected_episodes) != set(range(info.get("total_episodes", 0))):
            failures.append("episode metadata indices do not match info.json total_episodes")
        if sum(expected_episodes.values()) != info.get("total_frames", 0):
            failures.append("episode metadata lengths do not match info.json total_frames")

    parquet_files = sorted((args.root / "data").rglob("*.parquet"))
    if not parquet_files:
        failures.append("no frame parquet")
    else:
        tables = [pq.read_table(path, columns=["episode_index", "frame_index", "index", "timestamp", "task_index", "action", "observation.state"]) for path in parquet_files]
        episode_indices = np.concatenate([np.asarray(table["episode_index"]) for table in tables])
        frame_indices = np.concatenate([np.asarray(table["frame_index"]) for table in tables])
        global_indices = np.concatenate([np.asarray(table["index"]) for table in tables])
        timestamps = np.concatenate([np.asarray(table["timestamp"]) for table in tables])
        task_indices = np.concatenate([np.asarray(table["task_index"]) for table in tables])
        actions = np.concatenate([np.asarray(table["action"].to_pylist()) for table in tables])
        states = np.concatenate([np.asarray(table["observation.state"].to_pylist()) for table in tables])
        if len(episode_indices) != info.get("total_frames", 0):
            failures.append(f"data rows {len(episode_indices)} do not match info.json total_frames {info.get('total_frames', 0)}")
        if not np.array_equal(np.sort(global_indices), np.arange(len(global_indices))):
            failures.append("global frame indices are missing or duplicated")
        if not set(map(int, np.unique(task_indices))).issubset(valid_task_indices):
            failures.append("data rows reference missing/empty tasks")
        if not np.isfinite(actions).all(): failures.append("action contains NaN/Inf")
        if not np.isfinite(states).all(): failures.append("observation.state contains NaN/Inf")
        arm_range = float(np.ptp(actions[:, :6], axis=0).max())
        gripper_range = float(np.ptp(actions[:, 6]))
        state_arm_range = float(np.ptp(states[:, :6], axis=0).max())
        state_gripper_range = float(np.ptp(states[:, 6]))
        print(
            f"motion range: action arm={arm_range:.4f}rad gripper={gripper_range:.4f}m; "
            f"feedback arm={state_arm_range:.4f}rad gripper={state_gripper_range:.4f}m"
        )
        for episode_index in np.unique(episode_indices):
            mask = episode_indices == episode_index
            expected_length = expected_episodes.get(int(episode_index))
            if expected_length is None:
                failures.append(f"data contains undeclared episode {episode_index}")
            elif int(mask.sum()) != expected_length:
                failures.append(f"episode {episode_index} has {int(mask.sum())} rows, expected {expected_length}")
            if not np.array_equal(np.sort(frame_indices[mask]), np.arange(int(mask.sum()))):
                failures.append(f"episode {episode_index} frame indices are missing or duplicated")
            order = np.argsort(frame_indices[mask])
            if not np.allclose(timestamps[mask][order], np.arange(int(mask.sum())) / fps, atol=1e-4):
                failures.append(f"episode {episode_index} stored timestamps are not the fixed {fps}Hz timebase")
            episode_timestamps = timestamps[mask][order]
            duration = float(episode_timestamps[-1] - episode_timestamps[0]) if len(episode_timestamps) > 1 else 0.0
            episode_action_arm = float(np.ptp(actions[mask, :6], axis=0).max())
            episode_action_gripper = float(np.ptp(actions[mask, 6]))
            episode_state_arm = float(np.ptp(states[mask, :6], axis=0).max())
            episode_state_gripper = float(np.ptp(states[mask, 6]))
            print(
                f"episode {episode_index}: duration={duration:.2f}s; "
                f"action arm={episode_action_arm:.4f}rad gripper={episode_action_gripper:.4f}m; "
                f"feedback arm={episode_state_arm:.4f}rad gripper={episode_state_gripper:.4f}m"
            )
            if args.require_hil_motion:
                for label, value, minimum in (
                    ("arm action", episode_action_arm, args.min_arm_range),
                    ("gripper action", episode_action_gripper, args.min_gripper_range),
                    ("arm feedback", episode_state_arm, args.min_arm_range),
                    ("gripper feedback", episode_state_gripper, args.min_gripper_range),
                ):
                    if value < minimum:
                        failures.append(f"episode {episode_index} HIL {label} range {value:.4f} is too small")

    quality_path = args.root / "meta" / "recording_quality.jsonl"
    quality_by_episode: dict[int, dict] = {}
    if not quality_path.exists():
        failures.append("recording_quality.jsonl missing; source timing is unverified")
    else:
        quality_lines = quality_path.read_text().splitlines()
        if len(quality_lines) != info.get("total_episodes", 0):
            failures.append(
                f"recording quality entries {len(quality_lines)} do not match episodes {info.get('total_episodes', 0)}"
            )
        for line_number, line in enumerate(quality_lines, 1):
            try:
                quality = json.loads(line)
            except json.JSONDecodeError as exc:
                failures.append(f"quality line {line_number} is invalid JSON: {exc}")
                continue
            if not isinstance(quality, dict) or has_nonfinite_number(quality):
                failures.append(f"quality line {line_number} is not an object or contains NaN/Inf")
                continue
            episode_index = int(quality.get("episode_index", -1))
            if episode_index in quality_by_episode:
                failures.append(f"duplicate recording quality for episode {episode_index}")
            quality_by_episode[episode_index] = quality
            if quality.get("frames") != expected_episodes.get(episode_index):
                failures.append(f"quality line {line_number} frame count does not match episode metadata")
            if quality.get("target_fps", fps) != fps:
                failures.append(f"quality line {line_number} target_fps does not match dataset fps")
            effective = quality.get("effective_fps", 0)
            tolerance = min(quality.get("fps_tolerance", QUALITY_LIMITS["fps_tolerance"]), QUALITY_LIMITS["fps_tolerance"])
            if not effective or abs(effective - fps) / fps > tolerance:
                failures.append(f"quality line {line_number} row fps is outside tolerance")
            if quality.get("long_gap_ratio", 0) > min(quality.get("max_long_gap_ratio", QUALITY_LIMITS["max_long_gap_ratio"]), QUALITY_LIMITS["max_long_gap_ratio"]):
                failures.append(f"quality line {line_number} has too many long frame gaps")
            if quality.get("interval_max_ms", 0) > min(quality.get("max_pause_s", QUALITY_LIMITS["max_pause_s"]), QUALITY_LIMITS["max_pause_s"]) * 1000:
                failures.append(f"quality line {line_number} has an excessive source pause")
            if quality.get("writer_queue_drops", 0):
                failures.append(f"quality line {line_number} dropped writer rows")
            if quality.get("alignment_failures", 0):
                failures.append(f"quality line {line_number} has failed target-time alignments")
            if args.require_alignment_tail:
                tail_target = quality.get("alignment_tail_target_ros_s")
                tail_last = quality.get("alignment_tail_last_row_ros_s")
                if (
                    quality.get("alignment_tail_drained") is not True
                    or isinstance(tail_target, bool)
                    or not isinstance(tail_target, (int, float))
                    or isinstance(tail_last, bool)
                    or not isinstance(tail_last, (int, float))
                    or not np.isfinite([tail_target, tail_last]).all()
                    or tail_last < tail_target - 1.0 / fps
                ):
                    failures.append(
                        f"quality line {line_number} did not durably drain the alignment tail"
                    )
            failure_sources = quality.get("alignment_failure_sources")
            expected_sources = {"state", "arm_action", "gripper_action", "transport"} | {
                f"image.{key.removeprefix('observation.images.')}" for key in camera_keys
            }
            if not isinstance(failure_sources, dict) or not expected_sources.issubset(failure_sources):
                failures.append(f"quality line {line_number} lacks per-source alignment diagnostics")
            elif any(value != 0 for value in failure_sources.values()):
                failures.append(f"quality line {line_number} reports per-source alignment failures")
            if quality.get("action_age_max_ms", 0) > max(
                min(quality.get("action_max_age_s", QUALITY_LIMITS["action_max_age_s"]), QUALITY_LIMITS["action_max_age_s"]),
                min(quality.get("action_sync_slop_s", QUALITY_LIMITS["action_sync_slop_s"]), QUALITY_LIMITS["action_sync_slop_s"]),
            ) * 1000:
                failures.append(f"quality line {line_number} has excessive action alignment error")
            if quality.get("state_age_max_ms", 0) > min(quality.get("state_max_age_s", QUALITY_LIMITS["state_max_age_s"]), QUALITY_LIMITS["state_max_age_s"]) * 1000:
                failures.append(f"quality line {line_number} has stale robot state")
            camera_metrics = quality.get("cameras")
            if not isinstance(camera_metrics, dict) or set(camera_metrics) != set(camera_names):
                failures.append(f"quality line {line_number} camera metrics do not match dataset features")
                camera_metrics = {}
            for camera in camera_names:
                metrics = camera_metrics.get(camera)
                if not isinstance(metrics, dict):
                    continue
                if metrics.get("age_max_ms", 0) > min(quality.get("sync_slop_s", QUALITY_LIMITS["sync_slop_s"]), QUALITY_LIMITS["sync_slop_s"]) * 1000:
                    failures.append(f"quality line {line_number} has excessive {camera} target alignment error")
                if metrics.get("transport_age_max_ms", 0) > min(quality.get("image_max_age_s", QUALITY_LIMITS["image_max_age_s"]), QUALITY_LIMITS["image_max_age_s"]) * 1000:
                    failures.append(f"quality line {line_number} has stale {camera} transport")
                if metrics.get("source_fps", 0) < fps * max(quality.get("min_camera_source_fps_ratio", QUALITY_LIMITS["min_camera_source_fps_ratio"]), QUALITY_LIMITS["min_camera_source_fps_ratio"]):
                    failures.append(f"quality line {line_number} has low {camera} source fps")
                if metrics.get("repeat_ratio", 0) > min(quality.get("max_image_repeat_ratio", QUALITY_LIMITS["max_image_repeat_ratio"]), QUALITY_LIMITS["max_image_repeat_ratio"]):
                    failures.append(f"quality line {line_number} has excessive {camera} frame reuse")
                if (metrics.get("source_gap_max_ms") or 0) > min(quality.get("max_pause_s", QUALITY_LIMITS["max_pause_s"]), QUALITY_LIMITS["max_pause_s"]) * 1000:
                    failures.append(f"quality line {line_number} has a {camera} source pause")
                if metrics.get("color_delta_mean", 0) < 1.0:
                    warnings.append(f"quality line {line_number} has monochrome {camera} samples")
                if not 20 <= metrics.get("brightness_mean", -1) <= 235:
                    warnings.append(f"quality line {line_number} has unusual {camera} brightness")
                if metrics.get("clipping_mean", 1) > 0.20:
                    warnings.append(f"quality line {line_number} has high {camera} clipping")
                if metrics.get("sharpness_median", 0) < 20:
                    warnings.append(f"quality line {line_number} has low-sharpness {camera} samples")
        if set(quality_by_episode) != set(expected_episodes):
            failures.append("recording quality episode indices do not match episode metadata")

    for episode_index, expected_length in expected_episodes.items():
        provenance_path = args.root / "meta" / "source_provenance" / f"episode-{episode_index:06d}.jsonl"
        if not provenance_path.exists():
            failures.append(f"episode {episode_index} source provenance missing; timing cannot be independently verified")
            continue
        try:
            rows = [json.loads(line) for line in provenance_path.read_text().splitlines() if line]
        except json.JSONDecodeError as exc:
            failures.append(f"episode {episode_index} provenance is invalid JSON: {exc}")
            continue
        if any(not isinstance(row, dict) or has_nonfinite_number(row) for row in rows):
            failures.append(f"episode {episode_index} provenance contains a non-object or NaN/Inf")
            continue
        if len(rows) != expected_length:
            failures.append(f"episode {episode_index} provenance rows {len(rows)} do not match length {expected_length}")
            continue
        if [row.get("frame_index") for row in rows] != list(range(expected_length)):
            failures.append(f"episode {episode_index} provenance frame indices are not contiguous")
        if [row.get("enqueue_attempt_index") for row in rows] != list(range(expected_length)):
            failures.append(f"episode {episode_index} provenance exposes a queue drop or reordered write")
        quality = quality_by_episode.get(episode_index, {})
        valid_targets = []
        camera_raw_stamps = {camera: [] for camera in camera_names}
        camera_received_stamps = {camera: [] for camera in camera_names}
        for row_number, row in enumerate(rows):
            target = row.get("target_ros_s")
            joint_bracket = row.get("joint_bracket_ros_s", [])
            arm_bracket = row.get("arm_action_bracket_ros_s", [])
            gripper_stamp = row.get("gripper_action_ros_s")
            raw_images = row.get("image_raw_ros_s", {})
            corrected_images = row.get("image_corrected_ros_s", {})
            received_images = row.get("image_received_ros_s", {})
            values = [target, gripper_stamp, *joint_bracket, *arm_bracket, *raw_images.values(), *corrected_images.values(), *received_images.values()]
            if (
                len(joint_bracket) != 2 or len(arm_bracket) != 2
                or set(raw_images) != set(camera_names)
                or set(corrected_images) != set(camera_names)
                or set(received_images) != set(camera_names)
                or not all(isinstance(value, (int, float)) and np.isfinite(value) for value in values)
            ):
                failures.append(f"episode {episode_index} provenance row {row_number} is incomplete/non-finite")
                continue
            valid_targets.append(target)
            if not joint_bracket[0] <= target <= joint_bracket[1] or max(target - joint_bracket[0], joint_bracket[1] - target) > QUALITY_LIMITS["state_max_age_s"]:
                failures.append(f"episode {episode_index} provenance row {row_number} has invalid state interpolation bracket")
            if not arm_bracket[0] <= target <= arm_bracket[1] or max(target - arm_bracket[0], arm_bracket[1] - target) > QUALITY_LIMITS["action_sync_slop_s"]:
                failures.append(f"episode {episode_index} provenance row {row_number} has invalid arm action bracket")
            if not 0 <= target - gripper_stamp <= QUALITY_LIMITS["action_max_age_s"]:
                failures.append(f"episode {episode_index} provenance row {row_number} has non-causal/stale gripper action")
            offsets = quality.get("camera_time_offsets_ms", {})
            for camera in camera_names:
                if abs((corrected_images[camera] - raw_images[camera]) * 1000 - offsets.get(camera, 0.0)) > 0.01:
                    failures.append(f"episode {episode_index} provenance row {row_number} has inconsistent {camera} time offset")
                if abs(corrected_images[camera] - target) > QUALITY_LIMITS["sync_slop_s"]:
                    failures.append(f"episode {episode_index} provenance row {row_number} exceeds {camera} target alignment tolerance")
                transport_age = received_images[camera] - raw_images[camera]
                if transport_age < 0 or transport_age > QUALITY_LIMITS["image_max_age_s"]:
                    failures.append(f"episode {episode_index} provenance row {row_number} has stale {camera} transport")
                camera_raw_stamps[camera].append(raw_images[camera])
                camera_received_stamps[camera].append(received_images[camera])
            if corrected_images and max(corrected_images.values()) - min(corrected_images.values()) > QUALITY_LIMITS["sync_slop_s"]:
                failures.append(f"episode {episode_index} provenance row {row_number} camera views are too far apart")
        if len(valid_targets) > 1:
            intervals = np.diff(valid_targets)
            provenance_duration = valid_targets[-1] - valid_targets[0]
            effective_fps = len(intervals) / provenance_duration if provenance_duration > 0 else 0.0
            fps_tolerance = min(quality.get("fps_tolerance", QUALITY_LIMITS["fps_tolerance"]), QUALITY_LIMITS["fps_tolerance"])
            if abs(effective_fps - fps) / fps > fps_tolerance:
                failures.append(f"episode {episode_index} provenance row fps is outside tolerance")
            long_gap_ratio = float((intervals > QUALITY_LIMITS["max_frame_gap_factor"] / fps).mean())
            if long_gap_ratio > QUALITY_LIMITS["max_long_gap_ratio"]:
                failures.append(f"episode {episode_index} provenance has too many long row gaps")
            if float(intervals.max()) > QUALITY_LIMITS["max_pause_s"]:
                failures.append(f"episode {episode_index} provenance has an excessive row pause")
        for camera in camera_names:
            raw = np.asarray(camera_raw_stamps[camera])
            received = np.asarray(camera_received_stamps[camera])
            if raw.size > 1 and (np.diff(raw) < 0).any():
                failures.append(f"episode {episode_index} {camera} source timestamps go backwards")
            if received.size > 1 and (np.diff(received) < 0).any():
                failures.append(f"episode {episode_index} {camera} receipt timestamps go backwards")
            unique = np.unique(raw)
            if unique.size > 1 and np.diff(unique).max() > QUALITY_LIMITS["max_pause_s"]:
                failures.append(f"episode {episode_index} {camera} source timestamps contain a pause")

    for key in camera_keys:
        if info["features"][key].get("dtype") != "video":
            continue
        video_files = sorted((args.root / "videos" / key).rglob("*.mp4"))
        decoded_frames = 0
        for path in video_files:
            try:
                result = subprocess.run(
                    ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames", "-of", "json", str(path)],
                    check=True, capture_output=True, text=True,
                )
                streams = json.loads(result.stdout).get("streams", [])
                decoded_frames += int(streams[0]["nb_read_frames"]) if streams else 0
            except (FileNotFoundError, subprocess.CalledProcessError, ValueError, KeyError) as exc:
                failures.append(f"cannot fully decode {path}: {exc}")
        if decoded_frames != info.get("total_frames", 0):
            failures.append(f"{key} decoded video frames {decoded_frames} do not match total_frames")

    if (
        camera_keys and info.get("total_frames", 0)
        and not args.skip_decoded_image_diagnostics
    ):
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        dataset = LeRobotDataset(args.repo_id, root=args.root, return_uint8=True)
        indices = np.linspace(0, len(dataset) - 1, min(100, len(dataset)), dtype=int)
        for key in camera_keys:
            metrics = np.asarray([image_metrics(np.asarray(dataset[int(index)][key])) for index in indices])
            color, brightness, clipping, sharpness = metrics.mean(axis=0)
            print(f"{key}: color={color:.2f} brightness={brightness:.1f} clipping={clipping:.1%} sharpness={sharpness:.1f}")
            if color < 1.0: warnings.append(f"{key} appears monochrome/IR")
            if not 20 <= brightness <= 235: warnings.append(f"{key} brightness is outside diagnostic range")
            if clipping > 0.20: warnings.append(f"{key} clipping exceeds diagnostic threshold 20%")
            if sharpness < 20: warnings.append(f"{key} sharpness is below diagnostic threshold")

    locator = None
    if args.episode_locator_index is not None:
        try:
            if args.episode_locator_index < 0:
                raise ContractError("EPISODE_LEDGER_LOCATOR_EPISODE")
            locator = episode_locator(
                info, episode_rows, args.episode_locator_index, args.repo_id,
            )
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            failures.append(f"cannot build episode locator: {exc}")

    for warning in warnings:
        print(f"WARN: {warning}")
    if failures:
        print("FAIL")
        for failure in failures: print(f"- {failure}")
        raise SystemExit(2)
    if locator is not None:
        print(EPISODE_LOCATOR_PREFIX + json.dumps(locator, sort_keys=True, separators=(",", ":")))
    checked_images = "video frame counts" if args.skip_decoded_image_diagnostics else "RGB decoding"
    print(f"PASS: FR5 dataset structure, source evidence, and {checked_images} ({len(warnings)} warning(s))")


if __name__ == "__main__":
    main()
