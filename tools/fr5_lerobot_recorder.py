#!/usr/bin/env python3
"""Record timestamp-synchronised FR5 ROS observations into one LeRobot v3 collection."""

from __future__ import annotations

import argparse
from collections import deque
import json
import os
import queue
import re
import select
import sys
import threading
import termios
import time
import tty
from pathlib import Path

import cv2
import numpy as np
import rclpy
from control_msgs.msg import JointTrajectoryControllerState
from data_factory_recovery import (
    DatasetTransactionLock,
    RecoveryError,
    canonical_json_digest,
    claim_staging_directories,
    dataset_snapshot,
    write_json_atomic,
)
from fr5_dataset_schema import ARM_NAMES, CAMERA_PROFILES, GRIPPER_NAME, QUALITY_LIMITS, dataset_features
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import Image, JointState
from ros_image import image_message_to_rgb
from time_alignment import interpolate_vector, latest_sample, nearest_sample

class FR5LeRobotRecorder(Node):
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    FREEZING = "FREEZING"
    FROZEN = "FROZEN"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"
    QUARANTINED_COMMIT = "QUARANTINED_COMMIT"

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("fr5_lerobot_recorder")
        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        self.LeRobotDataset = LeRobotDataset
        self.args = args
        self.lock = threading.Lock()
        self.stop_threads = threading.Event()
        self.camera_names = CAMERA_PROFILES[args.camera_profile]
        self.camera_offsets = {
            "up": args.up_time_offset_ms / 1000,
            "side": args.side_time_offset_ms / 1000,
            "wrist": args.wrist_time_offset_ms / 1000,
        }
        self.joint_states: deque[tuple[float, np.ndarray]] = deque(maxlen=400)
        self.arm_actions: deque[tuple[float, np.ndarray]] = deque(maxlen=400)
        self.gripper_actions: deque[tuple[float, float]] = deque(maxlen=400)
        self.camera_frames: dict[str, deque] = {name: deque(maxlen=90) for name in self.camera_names}
        self.dataset = self._open_dataset()
        self.recording = False
        self.episode_state = self.IDLE
        self._transaction: dict | None = None
        self._transaction_lock: DatasetTransactionLock | None = None
        self._buffer_cleared = False
        self.next_target_stamp: float | None = None
        self.frames = 0
        self.started = 0.0
        self.frame_stamps: list[float] = []
        self.sync_spans: list[float] = []
        self.action_ages: list[float] = []
        self.state_ages: list[float] = []
        self.camera_stamps: dict[str, list[float]] = {name: [] for name in self.camera_names}
        self.image_ages: dict[str, list[float]] = {name: [] for name in self.camera_names}
        self.image_transport_ages: dict[str, list[float]] = {name: [] for name in self.camera_names}
        self.image_metrics: dict[str, list[tuple[float, float, float, float]]] = {
            name: [] for name in self.camera_names
        }
        self.action_samples: list[np.ndarray] = []
        self.state_samples: list[np.ndarray] = []
        self.source_provenance: list[dict] = []
        self.enqueue_attempts = 0
        self.writer_queue: queue.Queue = queue.Queue(maxsize=args.writer_queue_size)
        self.writer_queue_drops = 0
        self.stale_sample_skips = 0
        self.missing_action_skips = 0
        self.alignment_failures = 0
        self.alignment_failure_sources = {"state": 0, "arm_action": 0, "gripper_action": 0, "transport": 0}
        self.alignment_failure_sources.update({f"image.{name}": 0 for name in self.camera_names})
        self.writer_error: Exception | None = None
        self.ready_logged = False

        self.create_subscription(JointState, args.joint_states, self._on_joint_state, qos_profile_sensor_data)
        configured_topics = {"up": args.up_image, "side": args.side_image, "wrist": args.wrist_image}
        image_topics = [configured_topics[name] for name in self.camera_names]
        image_qos = QoSProfile(
            depth=args.image_qos_depth,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        ) if args.image_qos == "reliable" else qos_profile_sensor_data
        self.image_subscriptions = [
            self.create_subscription(
                Image, topic, lambda message, camera=name: self._on_image(camera, message), image_qos
            )
            for name, topic in zip(self.camera_names, image_topics)
        ]
        self.create_subscription(JointTrajectoryControllerState, args.arm_state, self._on_arm_state, 20)
        self.create_subscription(JointTrajectoryControllerState, args.gripper_state, self._on_gripper_state, 20)
        self.writer_thread = threading.Thread(target=self._writer_loop, name="fr5-dataset-writer", daemon=True)
        self.sampler_thread = threading.Thread(target=self._sampler_loop, name="fr5-row-sampler", daemon=True)
        self.writer_thread.start()
        self.sampler_thread.start()
        self.get_logger().info(
            f"collection={args.root}; fps={args.fps}; alignment_delay={args.alignment_delay}s; "
            f"image_tolerance={args.sync_slop}s; joint={args.joint_states}; cameras={image_topics}; "
            f"camera_offsets_ms={{{', '.join(f'{name}: {self.camera_offsets[name]*1000:.1f}' for name in self.camera_names)}}}"
        )
        if not args.interactive:
            self.begin_episode()

    def _features(self) -> dict:
        return dataset_features(
            fps=self.args.fps,
            height=self.args.height,
            width=self.args.width,
            cameras=self.camera_names,
            use_videos=not self.args.no_videos,
        )

    def _open_dataset(self):
        root = self.args.root
        info = root / "meta" / "info.json"
        expected = self._features()
        encoder_options = {
            "streaming_encoding": self.args.streaming_encoding and not self.args.no_videos,
            "batch_encoding_size": 1,
            "encoder_threads": self.args.encoder_threads,
            "image_writer_processes": 0,
        }
        if info.exists():
            dataset = self.LeRobotDataset.resume(
                self.args.repo_id, root=root, image_writer_threads=0,
                **encoder_options,
            )
            if dataset.meta.fps != self.args.fps:
                raise SystemExit(f"Existing dataset fps={dataset.meta.fps}, requested fps={self.args.fps}")
            for key, spec in expected.items():
                actual = dataset.meta.features.get(key)
                if actual is None or actual["dtype"] != spec["dtype"] or list(actual["shape"]) != spec["shape"] or actual.get("names") != spec.get("names"):
                    raise SystemExit(f"Existing dataset feature mismatch for {key}: {actual} != {spec}")
            return dataset
        if root.exists() and any(root.iterdir()):
            raise SystemExit(
                f"{root} is not a LeRobot dataset root. Move legacy episode_* directories or choose a new profile."
            )
        if root.exists():
            root.rmdir()
        from lerobot.configs.video import RGBEncoderConfig
        preset = self.args.video_preset or ("ultrafast" if self.args.video_codec == "h264" else None)
        return self.LeRobotDataset.create(
            repo_id=self.args.repo_id,
            fps=self.args.fps,
            root=root,
            robot_type="fr5_ros2",
            features=expected,
            use_videos=not self.args.no_videos,
            image_writer_threads=0,
            rgb_encoder=RGBEncoderConfig(
                vcodec=self.args.video_codec, preset=preset, crf=self.args.video_crf
            ) if not self.args.no_videos else None,
            **encoder_options,
        )

    def _reset_episode(self) -> None:
        self.frames = 0
        self.started = 0.0
        self.frame_stamps.clear()
        self.sync_spans.clear()
        self.action_ages.clear()
        self.state_ages.clear()
        self.camera_stamps = {name: [] for name in self.camera_names}
        self.image_ages = {name: [] for name in self.camera_names}
        self.image_transport_ages = {name: [] for name in self.camera_names}
        self.image_metrics.clear()
        self.image_metrics.update({name: [] for name in self.camera_names})
        self.action_samples.clear()
        self.state_samples.clear()
        self.source_provenance.clear()
        self.enqueue_attempts = 0
        self.writer_queue_drops = 0
        self.stale_sample_skips = 0
        self.missing_action_skips = 0
        self.alignment_failures = 0
        self.alignment_failure_sources = {"state": 0, "arm_action": 0, "gripper_action": 0, "transport": 0}
        self.alignment_failure_sources.update({f"image.{name}": 0 for name in self.camera_names})
        self.writer_error = None
        self.next_target_stamp = None
        self._buffer_cleared = False

    def _result(self, ok: bool, reason_code: str = "OK", **extra) -> dict:
        return {
            "ok": ok,
            "state": self.episode_state,
            "reason_code": reason_code,
            "run_id": self._transaction["run_id"] if self._transaction else None,
            "transaction_id": self._transaction["transaction_id"] if self._transaction else None,
            "episode_index": self._transaction["episode_index"] if self._transaction else self.dataset.meta.total_episodes,
            "metrics": {"rows": self.frames, "writer_queue": self.writer_queue.qsize()},
            "artifacts": self._transaction["artifacts"] if self._transaction else {},
            "detail": "",
            **extra,
        }

    def _append_event(self, reason_code: str) -> None:
        if not self._transaction:
            return
        event = {
            "run_id": self._transaction["run_id"],
            "state": self.episode_state,
            "reason_code": reason_code,
            "episode_index": self._transaction["episode_index"],
            "rows": self.frames,
            "monotonic_ns": time.monotonic_ns(),
        }
        path = self._transaction["events_path"]
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n")
            file.flush()
            os.fsync(file.fileno())

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict) -> None:
        write_json_atomic(path, payload)

    @staticmethod
    def _unlink_durable(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        path.unlink(missing_ok=True)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _dataset_snapshot(self) -> dict:
        return dataset_snapshot(self.args.root)

    def _release_transaction_lock(self) -> None:
        lock = getattr(self, "_transaction_lock", None)
        if lock is not None:
            lock.release()
            self._transaction_lock = None

    def _prepare_transaction(self, context: dict | None) -> dict | None:
        if context is None:
            return None
        if not isinstance(context, dict):
            raise ValueError("transaction context must be a mapping")
        if set(context) != {"run_id", "binding_digests"}:
            raise ValueError("transaction context has unsupported fields")
        run_id = context.get("run_id")
        bindings = context.get("binding_digests")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id):
            raise ValueError("transaction context requires safe run_id")
        if not getattr(self.args, "run_root", None):
            raise ValueError("transaction context requires configured run_root")
        required = {
            "resolved_job_digest", "selected_sheet_digest", "yaw0_sheet_digest", "cell_calibration_digest",
            "robot_system_digest", "collection_profile_digest", "object_profile_digest", "grasp_profile_digest",
        }
        if not isinstance(bindings, dict) or set(bindings) != required or any(
            not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in bindings.values()
        ):
            raise ValueError("transaction context requires exact sha256 binding_digests")
        root = self.args.root.resolve()
        run_root = Path(self.args.run_root).resolve()
        if root == run_root or root in run_root.parents or run_root in root.parents:
            raise ValueError("run_root and dataset root must be separate")
        directory = (run_root / run_id).resolve()
        try:
            directory.relative_to(run_root)
        except ValueError as exc:
            raise ValueError("transaction run_dir must be under configured run_root") from exc
        if directory.exists():
            raise ValueError("transaction run_id already exists")
        guard_path = root / "meta" / "quarantine.json"
        if guard_path.exists() or guard_path.is_symlink():
            raise ValueError("dataset has unresolved data factory commit guard")
        episode_index = self.dataset.meta.total_episodes
        staging_dirs = {
            camera: str(root / "images" / f"observation.images.{camera}" / f"episode-{episode_index:06d}")
            for camera in self.camera_names
        }
        if (root / "images").is_symlink() or any(Path(path).exists() or Path(path).is_symlink() for path in staging_dirs.values()):
            raise ValueError("dataset has pre-existing or unsafe episode staging")
        directory.mkdir(parents=True)
        manifest_path = directory / "staging_manifest.json"
        manifest = {
            "schema_version": "data_factory.staging_manifest.v1",
            "run_id": run_id,
            "dataset_root": str(root),
            "episode_index": episode_index,
            "staging_mode": "batch",
            "binding_digests": dict(bindings),
            "camera_staging_dirs": staging_dirs,
            "begin_snapshot": self._dataset_snapshot(),
        }
        self._write_json_atomic(manifest_path, manifest)
        manifest_digest = canonical_json_digest(manifest)
        self._unlink_durable(root / "meta" / "training_approved.json")
        result_path = directory / "result.json"
        return {
            "run_id": run_id,
            "transaction_id": f"{run_id}:episode-{episode_index:06d}",
            "episode_index": episode_index,
            "events_path": directory / "events.jsonl",
            "result_path": result_path,
            "guard_path": guard_path,
            "begin_snapshot": manifest["begin_snapshot"],
            "staging_manifest_digest": manifest_digest,
            "staging_dirs": tuple(staging_dirs.values()),
            "artifacts": {
                "staging_manifest": str(manifest_path),
                "events": str(directory / "events.jsonl"),
                "result": str(result_path),
                "dataset_commit_guard": str(guard_path),
            },
        }

    def _write_run_result(self, state: str, reason_code: str, detail: str = "") -> None:
        if not self._transaction:
            return
        self._write_json_atomic(self._transaction["result_path"], {
            "schema_version": "data_factory.recorder_result.v1",
            "run_id": self._transaction["run_id"],
            "transaction_id": self._transaction["transaction_id"],
            "episode_index": self._transaction["episode_index"],
            "state": state,
            "reason_code": reason_code,
            "rows": self.frames,
            "detail": detail,
        })

    def _write_commit_guard(self, state: str, reason_code: str, detail: str = "") -> None:
        transaction = self._transaction or {}
        if not transaction and state != self.QUARANTINED_COMMIT:
            return
        self._write_json_atomic(transaction.get("guard_path", self.args.root / "meta" / "quarantine.json"), {
            "schema_version": "data_factory.commit_guard.v2",
            "run_id": transaction.get("run_id"),
            "transaction_id": transaction.get("transaction_id"),
            "episode_index": transaction.get("episode_index", self.dataset.meta.total_episodes),
            "state": state,
            "reason_code": reason_code,
            "detail": detail,
            "staging_manifest": transaction.get("artifacts", {}).get("staging_manifest"),
            "staging_manifest_digest": transaction.get("staging_manifest_digest"),
        })

    def _abort_cleanup_error(self) -> str:
        if not self._transaction:
            return ""
        remaining = [path for path in self._transaction["staging_dirs"] if Path(path).exists()]
        current = self._dataset_snapshot()
        problems = []
        if remaining:
            problems.append("staging remains: " + ", ".join(remaining))
        if current != self._transaction["begin_snapshot"]:
            problems.append("committed dataset snapshot changed")
        return "; ".join(problems)

    def _persist_quarantine(self, reason_code: str, detail: str) -> dict:
        errors = []
        try:
            self._unlink_durable(self.args.root / "meta" / "training_approved.json")
        except Exception as exc:
            errors.append(f"approval invalidation failed: {exc}")
        try:
            self._write_commit_guard(self.QUARANTINED_COMMIT, reason_code, detail)
        except Exception as exc:
            errors.append(f"commit guard update failed: {exc}")
        try:
            self._write_run_result(self.QUARANTINED_COMMIT, reason_code, detail)
        except Exception as exc:
            errors.append(f"result write failed: {exc}")
        try:
            self._append_event(reason_code)
        except Exception as exc:
            errors.append(f"quarantine journal failed: {exc}")
        if errors:
            detail = "; ".join([detail, *errors])
        return self._result(False, reason_code, detail=detail)

    def _finish_abort(self, ok: bool, reason_code: str, detail: str = "") -> dict:
        try:
            self._write_run_result(self.ABORTED, reason_code, detail)
            self._append_event(reason_code)
        except Exception as exc:
            with self.lock:
                self.episode_state = self.QUARANTINED_COMMIT
            return self._persist_quarantine(
                "ABORT_DIAGNOSTIC_FAILED", "; ".join(filter(None, (detail, str(exc))))
            )
        if self._transaction:
            try:
                self._unlink_durable(self._transaction["guard_path"])
            except Exception as exc:
                with self.lock:
                    self.episode_state = self.QUARANTINED_COMMIT
                return self._persist_quarantine("ABORT_GUARD_RELEASE_FAILED", str(exc))
            self._release_transaction_lock()
        return self._result(ok, reason_code, detail=detail)

    def begin_episode(self, transaction: dict | None = None) -> dict:
        with self.lock:
            if self.episode_state not in (self.IDLE, self.ABORTED, self.COMMITTED):
                return self._result(False, "STATE_BEGIN_NOT_ALLOWED")
            if self._transaction is not None:
                return self._result(False, "PROCESS_TRANSACTION_ALREADY_USED")
            if transaction is not None and (
                getattr(self.args, "streaming_encoding", False) or getattr(self.args, "no_videos", False)
            ):
                return self._result(False, "UNSUPPORTED_STAGING_MODE")
            if transaction is not None:
                try:
                    self._transaction_lock = DatasetTransactionLock(self.args.root)
                    self._transaction_lock.acquire()
                except RecoveryError as exc:
                    self._transaction_lock = None
                    return self._result(False, exc.code, detail=str(exc))
            try:
                self._transaction = self._prepare_transaction(transaction)
            except Exception:
                self._release_transaction_lock()
                raise
            self._reset_episode()
            self.episode_state = self.RECORDING
            try:
                self._write_commit_guard(self.RECORDING, "BEGIN")
            except Exception as exc:
                self.episode_state = self.IDLE
                self._release_transaction_lock()
                return self._result(False, "BEGIN_GUARD_FAILED", detail=str(exc))
            if self._transaction:
                try:
                    claim_staging_directories(
                        self.args.root,
                        list(self.camera_names),
                        self._transaction["run_id"],
                        self._transaction["transaction_id"],
                        self._transaction["episode_index"],
                        self._transaction["staging_manifest_digest"],
                    )
                except Exception as exc:
                    self.recording = False
                    self.episode_state = self.QUARANTINED_COMMIT
                    return self._persist_quarantine("BEGIN_STAGING_CLAIM_FAILED", str(exc))
            try:
                self._append_event("BEGIN")
            except Exception as exc:
                self.recording = False
                self.episode_state = self.QUARANTINED_COMMIT
                return self._persist_quarantine("BEGIN_JOURNAL_FAILED", str(exc))
            self.recording = True
            self.started = time.perf_counter()
            self.next_target_stamp = self.get_clock().now().nanoseconds * 1e-9
            result = self._result(True)
        self.get_logger().info(
            f"recording episode_index={self.dataset.meta.total_episodes} (s=save, c=discard, q=discard+quit)"
        )
        return result

    def start_episode(self) -> None:
        result = self.begin_episode()
        if not result["ok"]:
            self.get_logger().warning("episode already recording; press s or c first")

    def _quality_summary(self) -> tuple[dict, list[str]]:
        intervals = np.diff(self.frame_stamps)
        long_gaps = intervals > self.args.max_frame_gap_factor / self.args.fps
        duration = self.frame_stamps[-1] - self.frame_stamps[0] if len(self.frame_stamps) > 1 else 0.0
        effective_fps = (len(self.frame_stamps) - 1) / duration if duration > 0 else 0.0
        actions = np.asarray(self.action_samples, dtype=float)
        states = np.asarray(self.state_samples, dtype=float)
        finite_actions = actions if np.isfinite(actions).all() else np.empty((0, 7))
        finite_states = states if np.isfinite(states).all() else np.empty((0, 7))
        summary = {
            "episode_index": self.dataset.meta.total_episodes,
            "target_fps": self.args.fps,
            "fps_tolerance": self.args.fps_tolerance,
            "max_frame_gap_factor": self.args.max_frame_gap_factor,
            "max_long_gap_ratio": self.args.max_long_gap_ratio,
            "max_pause_s": self.args.max_pause,
            "min_camera_source_fps_ratio": self.args.min_camera_source_fps_ratio,
            "max_image_repeat_ratio": self.args.max_image_repeat_ratio,
            "sync_slop_s": self.args.sync_slop,
            "action_sync_slop_s": self.args.action_sync_slop,
            "alignment_delay_s": self.args.alignment_delay,
            "image_max_age_s": self.args.image_max_age,
            "state_max_age_s": self.args.state_max_age,
            "action_max_age_s": self.args.action_max_age,
            "frames": self.frames,
            "source_duration_s": duration,
            "effective_fps": effective_fps,
            "interval_mean_ms": float(intervals.mean() * 1000) if intervals.size else None,
            "interval_p95_ms": float(np.percentile(intervals, 95) * 1000) if intervals.size else None,
            "interval_max_ms": float(intervals.max() * 1000) if intervals.size else None,
            "long_gap_count": int(long_gaps.sum()),
            "long_gap_ratio": float(long_gaps.mean()) if intervals.size else 0.0,
            "sync_span_p95_ms": float(np.percentile(self.sync_spans, 95) * 1000) if self.sync_spans else None,
            "sync_span_max_ms": float(max(self.sync_spans) * 1000) if self.sync_spans else None,
            "action_age_p95_ms": float(np.percentile(self.action_ages, 95) * 1000) if self.action_ages else None,
            "action_age_max_ms": float(max(self.action_ages) * 1000) if self.action_ages else None,
            "state_age_p95_ms": float(np.percentile(self.state_ages, 95) * 1000) if self.state_ages else None,
            "state_age_max_ms": float(max(self.state_ages) * 1000) if self.state_ages else None,
            "writer_queue_drops": self.writer_queue_drops,
            "stale_sample_skips": self.stale_sample_skips,
            "missing_action_skips": self.missing_action_skips,
            "alignment_failures": self.alignment_failures,
            "alignment_failure_sources": self.alignment_failure_sources.copy(),
            "enqueue_attempts": self.enqueue_attempts,
            "camera_time_offsets_ms": {
                camera: self.camera_offsets[camera] * 1000 for camera in self.camera_names
            },
            "arm_action_range_rad": float(np.ptp(finite_actions[:, :6], axis=0).max()) if finite_actions.size else 0.0,
            "gripper_action_range_m": float(np.ptp(finite_actions[:, 6])) if finite_actions.size else 0.0,
            "arm_feedback_range_rad": float(np.ptp(finite_states[:, :6], axis=0).max()) if finite_states.size else 0.0,
            "gripper_feedback_range_m": float(np.ptp(finite_states[:, 6])) if finite_states.size else 0.0,
            "cameras": {},
            "image_quality_warnings": [],
        }
        reasons = []
        if not np.isfinite(actions).all():
            reasons.append("action contains NaN/Inf")
        if not np.isfinite(states).all():
            reasons.append("state contains NaN/Inf")
        if len(self.source_provenance) != self.frames:
            reasons.append(
                f"source provenance rows {len(self.source_provenance)} != saved rows {self.frames}"
            )
        if self.enqueue_attempts != self.frames + self.writer_queue_drops:
            reasons.append(
                f"enqueue attempts {self.enqueue_attempts} != rows+drops {self.frames + self.writer_queue_drops}"
            )
        if self.writer_error is not None:
            reasons.append(f"dataset writer failed: {self.writer_error}")
        if self.writer_queue_drops:
            reasons.append(f"dataset writer queue dropped {self.writer_queue_drops} row(s)")
        if self.alignment_failures:
            reasons.append(f"timestamp alignment failed for {self.alignment_failures} target row(s)")
        if self.frames < self.args.min_frames:
            reasons.append(f"frames {self.frames} < minimum {self.args.min_frames}")
        if effective_fps and abs(effective_fps - self.args.fps) / self.args.fps > self.args.fps_tolerance:
            reasons.append(f"row fps {effective_fps:.2f} outside {self.args.fps_tolerance:.0%} of {self.args.fps}")
        if intervals.size and intervals.max() > self.args.max_pause:
            reasons.append(f"row pause {intervals.max()*1000:.1f}ms exceeds {self.args.max_pause*1000:.0f}ms")
        if long_gaps.size and long_gaps.mean() > self.args.max_long_gap_ratio:
            reasons.append(f"long frame-gap ratio {long_gaps.mean():.2%} exceeds {self.args.max_long_gap_ratio:.2%}")
        if self.state_ages and max(self.state_ages) > self.args.state_max_age:
            reasons.append(f"state interpolation distance {max(self.state_ages)*1000:.1f}ms exceeds limit")
        if self.action_ages and max(self.action_ages) > max(self.args.action_sync_slop, self.args.action_max_age):
            reasons.append(f"action alignment distance {max(self.action_ages)*1000:.1f}ms exceeds limit")
        for camera, metrics in self.image_metrics.items():
            samples = np.asarray(metrics, dtype=float)
            stamps = np.asarray(self.camera_stamps[camera], dtype=float)
            repeats = np.diff(stamps) == 0 if stamps.size > 1 else np.array([], dtype=bool)
            unique_frames = int(stamps.size - repeats.sum()) if stamps.size else 0
            unique_stamps = np.unique(stamps)
            source_gaps = np.diff(unique_stamps)
            ages = np.asarray(self.image_ages[camera], dtype=float)
            transport_ages = np.asarray(self.image_transport_ages[camera], dtype=float)
            camera_summary = {
                "color_delta_mean": float(samples[:, 0].mean()) if samples.size else None,
                "brightness_mean": float(samples[:, 1].mean()) if samples.size else None,
                "clipping_mean": float(samples[:, 2].mean()) if samples.size else None,
                "sharpness_median": float(np.median(samples[:, 3])) if samples.size else None,
                "unique_source_frames": unique_frames,
                "repeat_count": int(repeats.sum()),
                "repeat_ratio": float(repeats.mean()) if repeats.size else 0.0,
                "source_fps": float((unique_frames - 1) / duration) if duration > 0 and unique_frames > 1 else 0.0,
                "source_gap_max_ms": float(source_gaps.max() * 1000) if source_gaps.size else None,
                "age_p95_ms": float(np.percentile(ages, 95) * 1000) if ages.size else None,
                "age_max_ms": float(ages.max() * 1000) if ages.size else None,
                "age_over_50ms_ratio": float((ages > 0.050).mean()) if ages.size else 0.0,
                "age_over_100ms_ratio": float((ages > 0.100).mean()) if ages.size else 0.0,
                "transport_age_p95_ms": float(np.percentile(transport_ages, 95) * 1000) if transport_ages.size else None,
                "transport_age_max_ms": float(transport_ages.max() * 1000) if transport_ages.size else None,
            }
            summary["cameras"][camera] = camera_summary
            if not samples.size:
                reasons.append(f"{camera} camera has no quality samples")
                continue
            warnings = summary["image_quality_warnings"]
            if camera_summary["color_delta_mean"] < self.args.min_color_delta and not self.args.allow_monochrome:
                warnings.append(f"{camera} image appears monochrome (color delta {camera_summary['color_delta_mean']:.2f})")
            if not self.args.min_brightness <= camera_summary["brightness_mean"] <= self.args.max_brightness:
                warnings.append(f"{camera} brightness {camera_summary['brightness_mean']:.1f} outside diagnostic range")
            if camera_summary["clipping_mean"] > self.args.max_clipping:
                warnings.append(f"{camera} clipping {camera_summary['clipping_mean']:.1%} exceeds diagnostic threshold")
            if camera_summary["sharpness_median"] < self.args.min_sharpness:
                warnings.append(f"{camera} sharpness {camera_summary['sharpness_median']:.1f} below diagnostic threshold")
            if camera_summary["age_max_ms"] > self.args.sync_slop * 1000:
                reasons.append(f"{camera} target alignment error exceeds {self.args.sync_slop*1000:.0f}ms")
            if camera_summary["transport_age_max_ms"] > self.args.image_max_age * 1000:
                reasons.append(f"{camera} transport age exceeds {self.args.image_max_age*1000:.0f}ms")
            if camera_summary["source_fps"] < self.args.fps * self.args.min_camera_source_fps_ratio:
                reasons.append(f"{camera} source fps {camera_summary['source_fps']:.2f} is too low")
            if camera_summary["repeat_ratio"] > self.args.max_image_repeat_ratio:
                reasons.append(f"{camera} image repeat ratio {camera_summary['repeat_ratio']:.1%} is too high")
            if source_gaps.size and source_gaps.max() > self.args.max_pause:
                reasons.append(f"{camera} source pause {source_gaps.max()*1000:.1f}ms exceeds limit")
        return summary, reasons

    def freeze_episode(self) -> dict:
        with self.lock:
            if self.episode_state != self.RECORDING:
                return self._result(False, "STATE_FREEZE_NOT_RECORDING")
            self.recording = False
            self.episode_state = self.FREEZING
        self.writer_queue.join()
        with self.lock:
            if self.episode_state != self.FREEZING:
                return self._result(False, "STATE_FREEZE_INTERRUPTED")
            self.episode_state = self.FROZEN
            try:
                self._write_commit_guard(self.FROZEN, "FROZEN")
                self._append_event("FROZEN")
            except Exception as exc:
                self.episode_state = self.QUARANTINED_COMMIT
                return self._persist_quarantine("FREEZE_DURABILITY_FAILED", str(exc))
            return self._result(True)

    def _clear_episode_buffer_once(self) -> None:
        if not self._buffer_cleared:
            self.dataset.clear_episode_buffer()
            self._buffer_cleared = True

    def abort_episode(self) -> dict:
        if self.episode_state == self.RECORDING:
            self.freeze_episode()
        cleanup_error = None
        with self.lock:
            if self.episode_state != self.FROZEN:
                return self._result(False, "STATE_ABORT_NOT_ALLOWED")
            try:
                self._clear_episode_buffer_once()
            except Exception as exc:
                cleanup_error = exc
                self.recording = False
                self.episode_state = self.QUARANTINED_COMMIT
            else:
                verification_error = self._abort_cleanup_error()
                if verification_error:
                    cleanup_error = RuntimeError(verification_error)
                    self.episode_state = self.QUARANTINED_COMMIT
                else:
                    self.episode_state = self.ABORTED
        if cleanup_error is not None:
            return self._persist_quarantine("ABORT_CLEANUP_FAILED", str(cleanup_error))
        result = self._finish_abort(True, "ABORTED")
        self.get_logger().info(f"episode discarded, frames={self.frames}")
        return result

    def _abort_precommit(self, reason_code: str, exc: Exception, temporary_path: Path | None = None) -> dict:
        detail = str(exc)
        cleanup_error = None
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except Exception as cleanup_exc:
                detail = f"{detail}; temporary cleanup failed: {cleanup_exc}"
                cleanup_error = cleanup_exc
        with self.lock:
            try:
                self._clear_episode_buffer_once()
            except Exception as cleanup_exc:
                cleanup_error = cleanup_error or cleanup_exc
                detail = f"{detail}; buffer cleanup failed: {cleanup_exc}"
                self.episode_state = self.QUARANTINED_COMMIT
            else:
                verification_error = self._abort_cleanup_error()
                if verification_error:
                    cleanup_error = cleanup_error or RuntimeError(verification_error)
                    detail = f"{detail}; {verification_error}"
                    self.episode_state = self.QUARANTINED_COMMIT
                elif cleanup_error is not None:
                    self.episode_state = self.QUARANTINED_COMMIT
                else:
                    self.episode_state = self.ABORTED
            self.recording = False
        if cleanup_error is not None:
            return self._persist_quarantine("PRECOMMIT_CLEANUP_FAILED", detail)
        return self._finish_abort(False, reason_code, detail)

    def commit_episode(self) -> dict:
        with self.lock:
            if self.episode_state != self.FROZEN:
                return self._result(False, "STATE_COMMIT_NOT_FROZEN")
            self.episode_state = self.COMMITTING
        if not self.frames:
            with self.lock:
                self.episode_state = self.FROZEN
                return self._result(False, "QUALITY_NO_SYNCHRONIZED_FRAMES")
        try:
            summary, reasons = self._quality_summary()
        except Exception as exc:
            with self.lock:
                self.episode_state = self.FROZEN
                return self._result(False, "QUALITY_EVALUATION_FAILED", detail=str(exc))
        attempt = {**summary, "accepted": not reasons, "reasons": reasons}
        for warning in summary["image_quality_warnings"]:
            self.get_logger().warning(warning)
        attempts_path = self.args.root / "meta" / "recording_attempts.jsonl"
        if reasons:
            try:
                with attempts_path.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(attempt, ensure_ascii=False, allow_nan=False) + "\n")
            except Exception as exc:
                return self._abort_precommit("PRECOMMIT_DIAGNOSTIC_FAILED", exc)
            self.get_logger().error("episode rejected and discarded: " + "; ".join(reasons) + ". Press r to retry.")
            self.get_logger().info(
                f"rejected diagnostics: stale_skips={self.stale_sample_skips}, "
                f"missing_action_skips={self.missing_action_skips}, queue_drops={self.writer_queue_drops}, "
                f"alignment_sources={self.alignment_failure_sources}"
            )
            with self.lock:
                self.episode_state = self.FROZEN
                return self._result(False, "QUALITY_REJECTED", quality=attempt)
        try:
            provenance_dir = self.args.root / "meta" / "source_provenance"
            provenance_dir.mkdir(parents=True, exist_ok=True)
            provenance_path = provenance_dir / f"episode-{summary['episode_index']:06d}.jsonl"
            temporary_path = provenance_path.with_suffix(".jsonl.tmp")
            with temporary_path.open("w", encoding="utf-8") as file:
                for row in self.source_provenance:
                    file.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")
        except Exception as exc:
            return self._abort_precommit("PRECOMMIT_PROVENANCE_FAILED", exc, locals().get("temporary_path"))
        try:
            self._write_commit_guard(self.COMMITTING, "COMMIT_STARTED")
        except Exception as exc:
            return self._abort_precommit("PRECOMMIT_GUARD_FAILED", exc, temporary_path)
        try:
            self.dataset.save_episode()
            temporary_path.replace(provenance_path)
            quality_path = self.args.root / "meta" / "recording_quality.jsonl"
            with quality_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(summary, ensure_ascii=False, allow_nan=False) + "\n")
            with attempts_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(attempt, ensure_ascii=False, allow_nan=False) + "\n")
            if self._transaction:
                self.dataset.finalize()
            with self.lock:
                self.episode_state = self.COMMITTED
                self._append_event("COMMITTED")
            self._write_run_result(self.COMMITTED, "COMMITTED")
            if self._transaction:
                self._unlink_durable(self._transaction["guard_path"])
                self._release_transaction_lock()
        except Exception as exc:
            with self.lock:
                self.episode_state = self.QUARANTINED_COMMIT
            self.get_logger().error(f"episode save quarantined: {exc}")
            return self._persist_quarantine("QUARANTINED_COMMIT", str(exc))
        self.get_logger().info(
            f"episode saved: index={summary['episode_index']}, frames={self.frames}, row_fps={summary['effective_fps']:.2f}"
        )
        return self._result(True, quality=attempt)

    def episode_status(self) -> dict:
        with self.lock:
            return self._result(
                True,
                writer_error=str(self.writer_error) if self.writer_error else None,
                writer_alive=self.writer_thread.is_alive(),
            )

    def stop_episode(self, discard: bool = False) -> bool:
        if self.episode_state in (self.IDLE, self.COMMITTED, self.ABORTED):
            return True
        if self.episode_state == self.QUARANTINED_COMMIT:
            return False
        if self.episode_state == self.RECORDING:
            self.freeze_episode()
        if discard:
            return self.abort_episode()["ok"]
        result = self.commit_episode()
        if not result["ok"] and result["state"] == self.FROZEN:
            self.abort_episode()
        return result["ok"]

    @staticmethod
    def _stamp(msg) -> float:
        return float(msg.header.stamp.sec) + msg.header.stamp.nanosec * 1e-9

    def _on_arm_state(self, msg: JointTrajectoryControllerState) -> None:
        values = dict(zip(msg.joint_names, msg.reference.positions))
        stamp = self._stamp(msg)
        positions = np.array([values[name] for name in ARM_NAMES], dtype=np.float32) if all(name in values for name in ARM_NAMES) else None
        if stamp > 0 and positions is not None and np.isfinite(positions).all():
            with self.lock:
                if not self.arm_actions or stamp > self.arm_actions[-1][0]:
                    self.arm_actions.append((stamp, positions))

    def _on_gripper_state(self, msg: JointTrajectoryControllerState) -> None:
        values = dict(zip(msg.joint_names, msg.reference.positions))
        stamp = self._stamp(msg)
        position = float(values[GRIPPER_NAME]) if GRIPPER_NAME in values else None
        if stamp > 0 and position is not None and np.isfinite(position):
            with self.lock:
                if not self.gripper_actions or stamp > self.gripper_actions[-1][0]:
                    self.gripper_actions.append((stamp, position))

    def _sample_image_quality(self, camera: str, image: np.ndarray) -> None:
        if self.frames % max(self.args.fps, 1):
            return
        image_f = image.astype(np.float32)
        color_delta = float(
            (np.abs(image_f[..., 0] - image_f[..., 1]).mean() + np.abs(image_f[..., 1] - image_f[..., 2]).mean()) / 2
        )
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        brightness = float(gray.mean())
        clipping = float(((gray <= 5) | (gray >= 250)).mean())
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        self.image_metrics[camera].append((color_delta, brightness, clipping, sharpness))

    def _on_joint_state(self, message: JointState) -> None:
        stamp = self._stamp(message)
        values = dict(zip(message.name, message.position))
        if stamp <= 0 or not all(name in values for name in ARM_NAMES + [GRIPPER_NAME]):
            return
        state = np.array([values[name] for name in ARM_NAMES] + [values[GRIPPER_NAME]], dtype=np.float32)
        if not np.isfinite(state).all():
            return
        with self.lock:
            if not self.joint_states or stamp > self.joint_states[-1][0]:
                self.joint_states.append((stamp, state))

    def _on_image(self, camera: str, message: Image) -> None:
        raw_stamp = self._stamp(message)
        if raw_stamp <= 0:
            return
        if (message.height, message.width) != (self.args.height, self.args.width):
            self.get_logger().warning(
                f"image shape differs from configured {(self.args.height, self.args.width)}; frame skipped"
            )
            return
        corrected_stamp = raw_stamp + self.camera_offsets[camera]
        received_stamp = self.get_clock().now().nanoseconds * 1e-9
        with self.lock:
            frames = self.camera_frames[camera]
            if not frames or corrected_stamp > frames[-1][0]:
                frames.append((corrected_stamp, message, raw_stamp, received_stamp))

    def _sources_ready(self) -> bool:
        return bool(
            len(self.joint_states) >= 2
            and len(self.arm_actions) >= 2
            and self.gripper_actions
            and all(self.camera_frames[camera] for camera in self.camera_names)
        )

    def _aligned_sample(self, target_stamp: float):
        with self.lock:
            state = interpolate_vector(self.joint_states, target_stamp, self.args.state_max_age)
            arm = interpolate_vector(self.arm_actions, target_stamp, self.args.action_sync_slop)
            gripper = latest_sample(self.gripper_actions, target_stamp, self.args.action_max_age)
            camera_samples = {
                camera: nearest_sample(self.camera_frames[camera], target_stamp, self.args.sync_slop)
                for camera in self.camera_names
            }
        missing = []
        if state is None: missing.append("state")
        if arm is None: missing.append("arm_action")
        if gripper is None: missing.append("gripper_action")
        missing.extend(f"image.{camera}" for camera, sample in camera_samples.items() if sample is None)
        if missing:
            for source in missing:
                self.alignment_failure_sources[source] += 1
            return None
        state_value, state_before, state_after = state
        arm_value, arm_before, arm_after = arm
        gripper_stamp, gripper_value = gripper
        transport_ages = {
            camera: sample[3] - sample[2] for camera, sample in camera_samples.items()
        }
        if any(age < 0 or age > self.args.image_max_age for age in transport_ages.values()):
            self.alignment_failure_sources["transport"] += 1
            return None
        images = tuple(camera_samples[camera][1] for camera in self.camera_names)
        action = np.r_[arm_value, gripper_value].astype(np.float32)
        image_errors = {
            camera: abs(camera_samples[camera][0] - target_stamp) for camera in self.camera_names
        }
        state_distance = max(target_stamp - state_before, state_after - target_stamp)
        action_distance = max(
            target_stamp - arm_before, arm_after - target_stamp, target_stamp - gripper_stamp
        )
        provenance = {
            "target_ros_s": target_stamp,
            "joint_bracket_ros_s": [state_before, state_after],
            "arm_action_bracket_ros_s": [arm_before, arm_after],
            "gripper_action_ros_s": gripper_stamp,
            "image_raw_ros_s": {camera: camera_samples[camera][2] for camera in self.camera_names},
            "image_corrected_ros_s": {camera: camera_samples[camera][0] for camera in self.camera_names},
            "image_received_ros_s": {camera: camera_samples[camera][3] for camera in self.camera_names},
        }
        return state_value, action, images, provenance, max(image_errors.values()), action_distance, state_distance

    def print_status(self) -> None:
        duration = self.frame_stamps[-1] - self.frame_stamps[0] if len(self.frame_stamps) > 1 else 0.0
        row_fps = (len(self.frame_stamps) - 1) / duration if duration > 0 else 0.0
        camera_status = []
        for camera, stamps in self.camera_stamps.items():
            repeats = sum(a == b for a, b in zip(stamps, stamps[1:]))
            ratio = repeats / max(1, len(stamps) - 1)
            max_age = max(self.image_ages[camera], default=0.0) * 1000
            camera_status.append(f"{camera}:repeat={ratio:.1%},max_age={max_age:.1f}ms")
        self.get_logger().info(
            f"STATUS state={self.episode_state} recording={self.recording} episode_index={self.dataset.meta.total_episodes} "
            f"rows={self.frames} row_fps={row_fps:.2f} queue={self.writer_queue.qsize()}/{self.args.writer_queue_size} "
            + " ".join(camera_status)
        )

    def _enqueue_aligned_frame(self, target_stamp: float) -> None:
        with self.lock:
            if not self.recording:
                return
        sample = self._aligned_sample(target_stamp)
        if sample is None:
            self.alignment_failures += 1
            return
        state, action, images, provenance, sync_span, action_age, state_age = sample
        with self.lock:
            if not self.recording:
                return
            enqueue_attempt_index = self.enqueue_attempts
            self.enqueue_attempts += 1
            try:
                self.writer_queue.put_nowait(
                    (state, action, images, provenance, sync_span, action_age, state_age, enqueue_attempt_index)
                )
            except queue.Full:
                self.writer_queue_drops += 1

    def _sampler_loop(self) -> None:
        period = 1.0 / self.args.fps
        while not self.stop_threads.is_set():
            now_ros = self.get_clock().now().nanoseconds * 1e-9
            targets = []
            with self.lock:
                if self.recording and self._sources_ready():
                    if not self.ready_logged:
                        self.ready_logged = True
                        self.get_logger().info("READY: buffered robot state, action, and RGB are available; press r to record")
                    if self.next_target_stamp is None:
                        self.next_target_stamp = now_ros
                        self.started = time.perf_counter()
                    ready_until = now_ros - self.args.alignment_delay
                    while self.next_target_stamp <= ready_until and len(targets) < self.args.fps:
                        targets.append(self.next_target_stamp)
                        self.next_target_stamp += period
            for target in targets:
                self._enqueue_aligned_frame(target)
            self.stop_threads.wait(min(period / 4, 0.01))

    def _writer_loop(self) -> None:
        while not self.stop_threads.is_set() or not self.writer_queue.empty():
            try:
                item = self.writer_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._write_frame(*item)
            except Exception as exc:
                with self.lock:
                    self.writer_error = exc
                self.get_logger().error(f"dataset writer failed: {exc}")
            finally:
                self.writer_queue.task_done()

    def _write_frame(
        self, state, action, images, provenance, sync_span, action_age, state_age, enqueue_attempt_index,
    ) -> None:
        with self.lock:
            if self.episode_state not in (self.RECORDING, self.FREEZING):
                return
        images = tuple(image_message_to_rgb(message) for message in images)
        row_stamp = provenance["target_ros_s"]
        frame = {"observation.state": state, "action": action, "task": self.args.task}
        for camera, image in zip(self.camera_names, images):
            frame[f"observation.images.{camera}"] = image
        self.dataset.add_frame(frame)
        self.frames += 1
        self.frame_stamps.append(row_stamp)
        self.sync_spans.append(sync_span)
        self.action_ages.append(action_age)
        self.state_ages.append(state_age)
        self.action_samples.append(action.copy())
        self.state_samples.append(state.copy())
        self.source_provenance.append({
            **provenance,
            "frame_index": self.frames - 1,
            "enqueue_attempt_index": enqueue_attempt_index,
        })
        for camera, image in zip(self.camera_names, images):
            raw_stamp = provenance["image_raw_ros_s"][camera]
            corrected_stamp = provenance["image_corrected_ros_s"][camera]
            received_stamp = provenance["image_received_ros_s"][camera]
            self.camera_stamps[camera].append(raw_stamp)
            self.image_ages[camera].append(abs(corrected_stamp - row_stamp))
            self.image_transport_ages[camera].append(received_stamp - raw_stamp)
            self._sample_image_quality(camera, image)
        if self.frames % self.args.fps == 0:
            elapsed = self.frame_stamps[-1] - self.frame_stamps[0] if self.frames > 1 else 0.0
            row_fps = (self.frames - 1) / elapsed if elapsed > 0 else 0.0
            self.get_logger().info(
                f"frames={self.frames}, row_fps={row_fps:.2f}, image_align={sync_span*1000:.1f}ms, "
                f"action_align={action_age*1000:.1f}ms, j4={action[3]:.4f}, j5={action[4]:.4f}, "
                f"gripper={action[6]:.4f}, "
                f"alignment_failures={self.alignment_failures}"
            )

    def finished(self) -> bool:
        return not self.args.interactive and self.started > 0 and time.perf_counter() - self.started >= self.args.duration

    def close(self) -> None:
        if self.episode_state == self.FREEZING:
            self.writer_queue.join()
            with self.lock:
                if self.episode_state == self.FREEZING:
                    self.episode_state = self.FROZEN
        if self._transaction and self.episode_state == self.RECORDING:
            self.freeze_episode()
        if self._transaction and self.episode_state == self.FROZEN:
            self.abort_episode()
        elif self.episode_state == self.RECORDING and not self.stop_episode():
            self.abort_episode()
            self.writer_queue.join()
            self.get_logger().error("invalid unsaved episode discarded during shutdown")
        elif self.episode_state == self.FROZEN:
            self.abort_episode()
        self.stop_threads.set()
        self.sampler_thread.join(timeout=2)
        self.writer_thread.join(timeout=2)
        try:
            self.dataset.finalize()
        finally:
            self._release_transaction_lock()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("datasets/fr5_episodes"))
    parser.add_argument("--profile", default="", help="Collection directory under --root; this is the trainable dataset root.")
    parser.add_argument("--repo-id", default="local/fr5_smolvla")
    parser.add_argument("--task", required=True)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--sync-slop", type=float, default=0.050, help="Maximum absolute RGB-to-target timestamp error.")
    parser.add_argument("--action-sync-slop", type=float, default=0.050, help="Maximum arm reference interpolation distance.")
    parser.add_argument("--action-max-age", type=float, default=0.050)
    parser.add_argument("--alignment-delay", type=float, default=0.350, help="Bounded wait before matching source-stamped samples around each target.")
    parser.add_argument("--fps-tolerance", type=float, default=0.10)
    parser.add_argument("--max-frame-gap-factor", type=float, default=2.0)
    parser.add_argument("--max-long-gap-ratio", type=float, default=0.01)
    parser.add_argument("--max-pause", type=float, default=0.25)
    parser.add_argument("--min-camera-source-fps-ratio", type=float, default=0.75)
    parser.add_argument("--max-image-repeat-ratio", type=float, default=0.25)
    parser.add_argument("--min-frames", type=int, default=60)
    parser.add_argument("--min-color-delta", type=float, default=1.0, help="Diagnostic warning threshold; does not reject an episode.")
    parser.add_argument("--min-brightness", type=float, default=20.0, help="Diagnostic warning threshold; does not reject an episode.")
    parser.add_argument("--max-brightness", type=float, default=235.0, help="Diagnostic warning threshold; does not reject an episode.")
    parser.add_argument("--max-clipping", type=float, default=0.20, help="Diagnostic warning threshold; does not reject an episode.")
    parser.add_argument("--min-sharpness", type=float, default=20.0, help="Diagnostic warning threshold; does not reject an episode.")
    parser.add_argument("--allow-monochrome", action="store_true")
    parser.add_argument("--image-max-age", type=float, default=0.300, help="Maximum image transport age from ROS header to recorder receipt.")
    parser.add_argument("--state-max-age", type=float, default=0.050, help="Maximum state interpolation distance around a target row.")
    parser.add_argument("--writer-queue-size", type=int, default=128, help="Rows buffered between aligner and LeRobot writer.")
    parser.add_argument("--encoder-threads", type=int, default=2)
    parser.add_argument("--video-codec", default="h264", help="LeRobot RGB codec; use auto only after a local encoder smoke test.")
    parser.add_argument("--video-preset", default=None, help="Codec preset; h264 defaults to ultrafast for capture stability.")
    parser.add_argument("--video-crf", type=float, default=23)
    parser.add_argument("--batch-video-encoding", dest="streaming_encoding", action="store_false")
    parser.set_defaults(streaming_encoding=True)
    parser.add_argument("--joint-states", default="/joint_states")
    parser.add_argument("--up-image", default="/camera/up/color/image_raw")
    parser.add_argument("--side-image", default="/camera/side/color/image_raw")
    parser.add_argument("--wrist-image", default="/camera/wrist/color/image_raw")
    parser.add_argument("--up-time-offset-ms", type=float, default=0.0, help="Externally measured correction added to the up camera header stamp.")
    parser.add_argument("--side-time-offset-ms", type=float, default=0.0, help="Externally measured correction added to the side camera header stamp.")
    parser.add_argument("--wrist-time-offset-ms", type=float, default=0.0, help="Externally measured correction added to the wrist camera header stamp.")
    parser.add_argument("--camera-profile", choices=CAMERA_PROFILES, default="up")
    parser.add_argument("--image-qos", choices=("reliable", "best-effort"), default="reliable")
    parser.add_argument("--image-qos-depth", type=int, default=10, help="Bounded RGB DDS backlog for short control-loop stalls.")
    parser.add_argument("--arm-state", default="/fairino5_controller/controller_state")
    parser.add_argument("--gripper-state", default="/gripper_controller/controller_state")
    parser.add_argument("--no-videos", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Append to an existing dataset root in non-interactive mode.")
    parser.add_argument("--interactive", action="store_true", help="Use r/s/c/q episode controls.")
    args = parser.parse_args()
    if args.profile:
        args.root = args.root / args.profile
    numeric_values = [
        args.duration, args.sync_slop, args.action_sync_slop, args.action_max_age, args.alignment_delay,
        args.fps_tolerance, args.max_frame_gap_factor, args.max_long_gap_ratio, args.max_pause,
        args.min_camera_source_fps_ratio, args.max_image_repeat_ratio, args.min_color_delta,
        args.min_brightness, args.max_brightness, args.max_clipping, args.min_sharpness,
        args.image_max_age, args.state_max_age, args.video_crf,
    ]
    if not np.isfinite(numeric_values).all():
        raise SystemExit("all numeric recorder settings must be finite")
    if args.fps <= 0 or args.duration <= 0 or args.height <= 0 or args.width <= 0 or args.min_frames <= 0:
        raise SystemExit("duration, fps, dimensions, and minimum frames must be positive")
    if args.sync_slop <= 0 or args.action_sync_slop <= 0 or args.action_max_age <= 0 or args.image_max_age <= 0 or args.state_max_age <= 0 or args.alignment_delay <= 0:
        raise SystemExit("fps and synchronization limits must be positive")
    if not 0 <= args.fps_tolerance <= QUALITY_LIMITS["fps_tolerance"]:
        raise SystemExit("fps tolerance exceeds the project hard limit")
    if not 1 <= args.max_frame_gap_factor <= QUALITY_LIMITS["max_frame_gap_factor"]:
        raise SystemExit("frame gap factor exceeds the project hard limit")
    if not 0 <= args.max_long_gap_ratio <= QUALITY_LIMITS["max_long_gap_ratio"]:
        raise SystemExit("long-gap ratio exceeds the project hard limit")
    if not 0 < args.max_pause <= QUALITY_LIMITS["max_pause_s"]:
        raise SystemExit("pause threshold exceeds the project hard limit")
    if not QUALITY_LIMITS["min_camera_source_fps_ratio"] <= args.min_camera_source_fps_ratio <= 1:
        raise SystemExit("camera source FPS ratio is below the project hard limit")
    if not 0 <= args.max_image_repeat_ratio <= QUALITY_LIMITS["max_image_repeat_ratio"]:
        raise SystemExit("image repeat ratio exceeds the project hard limit")
    for value, key in (
        (args.sync_slop, "sync_slop_s"), (args.action_sync_slop, "action_sync_slop_s"),
        (args.action_max_age, "action_max_age_s"), (args.state_max_age, "state_max_age_s"),
        (args.image_max_age, "image_max_age_s"),
    ):
        if value > QUALITY_LIMITS[key]:
            raise SystemExit(f"{key} exceeds the project hard limit")
    if not 0 <= args.min_brightness < args.max_brightness <= 255 or not 0 <= args.max_clipping <= 1:
        raise SystemExit("image quality thresholds are out of range")
    if args.alignment_delay < args.image_max_age + args.sync_slop:
        raise SystemExit("alignment delay must cover image max age plus synchronization slop")
    if not np.isfinite([args.up_time_offset_ms, args.side_time_offset_ms, args.wrist_time_offset_ms]).all():
        raise SystemExit("camera time offsets must be finite")
    if args.writer_queue_size <= 0 or args.image_qos_depth <= 0 or args.encoder_threads < 0:
        raise SystemExit("queue depths must be positive and encoder threads non-negative")
    if not args.interactive and not args.resume and (args.root / "meta" / "info.json").exists():
        raise SystemExit(f"Dataset already exists; use --resume or a new --profile: {args.root}")
    return args


def main() -> None:
    args = parse_args()
    rclpy.init()
    node = FR5LeRobotRecorder(args)
    old_terminal = None
    quality_ok = True
    try:
        if args.interactive:
            if not sys.stdin.isatty():
                raise SystemExit("--interactive requires a terminal stdin")
            old_terminal = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
            print("Keys: r=start, s=save, c=discard, p=status, f=save+finalize, q=discard+quit, h=help", flush=True)
            while rclpy.ok():
                rclpy.spin_once(node, timeout_sec=0.05)
                if select.select([sys.stdin], [], [], 0)[0]:
                    key = sys.stdin.read(1).lower()
                    if key == "r":
                        node.start_episode()
                    elif key == "s":
                        node.stop_episode()
                    elif key == "c":
                        node.stop_episode(discard=True)
                    elif key == "p":
                        node.print_status()
                    elif key == "f":
                        if node.stop_episode():
                            break
                    elif key == "q":
                        node.stop_episode(discard=True)
                        break
                    elif key in ("h", "?"):
                        print("r start | s validate+save | c discard | p status | f save+finalize | q quit", flush=True)
        else:
            while rclpy.ok() and not node.finished():
                rclpy.spin_once(node, timeout_sec=0.1)
            quality_ok = node.stop_episode()
    finally:
        if old_terminal is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_terminal)
        node.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    if not quality_ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
