#!/usr/bin/env python3
"""Deterministic lifecycle tests; no ROS node or hardware is started."""

import io
import json
import os
import queue
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
from types import ModuleType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from fr5_lerobot_recorder import (
    FR5LeRobotRecorder,
    parse_args,
    process_recorder_control_line,
    run_recorder_control_jsonl,
)
import fr5_lerobot_recorder as recorder_module
import data_factory_recovery as recovery
from data_factory_recovery import DatasetTransactionLock, RecoveryError, canonical_json_digest, dataset_snapshot, recover_orphaned_transaction


class _Logger:
    def info(self, *_): pass
    def warning(self, *_): pass
    def error(self, *_): pass


class _Dataset:
    def __init__(self, save_error=None, clear_error=None, finalize_error=None):
        self.meta = SimpleNamespace(total_episodes=7, total_frames=123)
        self.save_error = save_error
        self.clear_error = clear_error
        self.finalize_error = finalize_error
        self.saves = 0
        self.clears = 0
        self.finalizes = 0
        self.finalized = False
        self.parallel_encoding = None

    def save_episode(self, parallel_encoding=True):
        self.saves += 1
        self.parallel_encoding = parallel_encoding
        if self.save_error:
            raise self.save_error

    def clear_episode_buffer(self):
        self.clears += 1
        if self.clear_error:
            raise self.clear_error
        for camera in self.camera_names:
            shutil.rmtree(
                self.root / "images" / f"observation.images.{camera}" / f"episode-{self.meta.total_episodes:06d}",
                ignore_errors=True,
            )

    def finalize(self):
        if self.finalized:
            return
        self.finalizes += 1
        if self.finalize_error:
            raise self.finalize_error
        self.finalized = True


class RecorderTransactionTest(unittest.TestCase):
    CONTROL_RESPONSE_FIELDS = {
        "schema_version", "op_id", "op", "ok", "state", "reason_code", "run_id",
        "transaction_id", "episode_index", "metrics", "artifacts", "detail",
    }

    def test_resume_preserves_the_configured_video_encoder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "dataset"
            (root / "meta").mkdir(parents=True)
            (root / "meta" / "info.json").write_text("{}")
            features = {"observation.images.up": {"dtype": "video", "shape": [480, 640, 3], "names": None}}
            dataset = SimpleNamespace(meta=SimpleNamespace(fps=30, features=features))
            api = SimpleNamespace(resume=mock.Mock(return_value=dataset))
            recorder = FR5LeRobotRecorder.__new__(FR5LeRobotRecorder)
            recorder.LeRobotDataset = api
            recorder.args = SimpleNamespace(
                root=root, repo_id="local/test", fps=30, no_videos=False,
                streaming_encoding=False, encoder_threads=2,
                video_preset=None, video_codec="h264", video_crf=23,
            )
            recorder._features = lambda: features
            self.assertIs(recorder._open_dataset(), dataset)
            encoder = api.resume.call_args.kwargs["rgb_encoder"]
            self.assertEqual((encoder.vcodec, encoder.preset, encoder.crf), ("h264", "ultrafast", 23))

    def make_recorder(self, directory, save_error=None, clear_error=None, finalize_error=None):
        recorder = FR5LeRobotRecorder.__new__(FR5LeRobotRecorder)
        recorder.args = SimpleNamespace(
            root=Path(directory) / "dataset", run_root=Path(directory) / "runs", fps=30,
            writer_queue_size=8, streaming_encoding=False, no_videos=False,
            min_frames=60, fps_tolerance=0.10, max_frame_gap_factor=2.0,
            max_long_gap_ratio=0.01, max_pause=0.25, min_camera_source_fps_ratio=0.75,
            max_image_repeat_ratio=0.25, sync_slop=0.05, action_sync_slop=0.05,
            alignment_delay=0.35, image_max_age=0.30, state_max_age=0.05,
            action_max_age=0.05, min_color_delta=1.0, allow_monochrome=False,
            min_brightness=20, max_brightness=235, max_clipping=0.20, min_sharpness=20,
        )
        recorder.args.root.mkdir(parents=True, exist_ok=True)
        meta = recorder.args.root / "meta"
        meta.mkdir(exist_ok=True)
        info = meta / "info.json"
        if not info.exists():
            info.write_text(json.dumps({"total_episodes": 7, "total_frames": 123}))
        recorder.lock = threading.Lock()
        recorder.stop_threads = threading.Event()
        recorder.writer_queue = queue.Queue()
        recorder.writer_thread = threading.Thread()
        recorder.dataset = _Dataset(save_error, clear_error, finalize_error)
        recorder.dataset.root = recorder.args.root
        recorder.dataset.camera_names = ("up", "side")
        recorder.camera_names = ("up", "side")
        recorder.camera_offsets = {"up": 0.0, "side": 0.0}
        recorder.episode_state = recorder.IDLE
        recorder.recording = False
        recorder._transaction = None
        recorder._transaction_lock = None
        recorder._buffer_cleared = False
        recorder.frames = 0
        recorder.started = 0.0
        recorder.next_target_stamp = None
        recorder.frame_stamps = []
        recorder.sync_spans = []
        recorder.action_ages = []
        recorder.state_ages = []
        recorder.camera_stamps = {name: [] for name in recorder.camera_names}
        recorder.image_ages = {name: [] for name in recorder.camera_names}
        recorder.image_transport_ages = {name: [] for name in recorder.camera_names}
        recorder.image_metrics = {name: [] for name in recorder.camera_names}
        recorder.action_samples = []
        recorder.state_samples = []
        recorder.source_provenance = []
        recorder.enqueue_attempts = recorder.writer_queue_drops = 0
        recorder.stale_sample_skips = recorder.missing_action_skips = recorder.alignment_failures = 0
        recorder.alignment_failure_sources = {}
        recorder.writer_error = None
        recorder.get_logger = lambda: _Logger()
        recorder.get_clock = lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=1_000_000_000))
        recorder._wait_for_sources = lambda: True
        return recorder

    @staticmethod
    def transaction(directory):
        return {
            "run_id": "run-001",
            "binding_digests": {
                "resolved_job_digest": "sha256:" + "a" * 64,
                "selected_sheet_digest": "sha256:" + "b" * 64,
                "yaw0_sheet_digest": "sha256:" + "c" * 64,
                "cell_calibration_digest": "sha256:" + "d" * 64,
                "robot_system_digest": "sha256:" + "e" * 64,
                "collection_profile_digest": "sha256:" + "f" * 64,
                "object_profile_digest": "sha256:" + "0" * 64,
                "grasp_profile_digest": "sha256:" + "1" * 64,
            },
        }

    def test_begin_writes_manifest_and_event_before_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            del recorder._wait_for_sources
            ready = []
            recorder._sources_ready = lambda: bool(ready)
            with mock.patch.object(recorder_module.rclpy, "spin_once", side_effect=lambda *_args, **_kwargs: ready.append(True)):
                result = recorder.begin_episode(self.transaction(directory))
            self.assertTrue(result["ok"])
            self.assertEqual(ready, [True])
            run_dir = Path(directory) / "runs" / "run-001"
            manifest = json.loads((run_dir / "staging_manifest.json").read_text())
            self.assertEqual(manifest["episode_index"], 7)
            self.assertEqual(manifest["staging_mode"], "batch")
            self.assertEqual(manifest["begin_snapshot"]["total_frames"], 123)
            self.assertEqual(
                manifest["camera_staging_dirs"]["up"],
                str((Path(directory) / "dataset" / "images" / "observation.images.up" / "episode-000007").resolve()),
            )
            self.assertEqual(json.loads((run_dir / "events.jsonl").read_text())["state"], "RECORDING")
            self.assertEqual(recorder.episode_state, recorder.RECORDING)
            self.assertEqual(
                json.loads((recorder.args.root / "meta" / "quarantine.json").read_text())["state"],
                recorder.RECORDING,
            )
            marker = json.loads((recorder.args.root / "meta" / "quarantine.json").read_text())
            self.assertEqual(marker["schema_version"], "data_factory.commit_guard.v2")
            self.assertEqual(marker["staging_manifest_digest"], canonical_json_digest(manifest))

    def test_factory_storage_reserve_device_and_boundary_matrix(self):
        for same_device, required, dataset_free, temp_free, accepted in (
            (True, (110, 110), 109, 109, False),
            (True, (110, 110), 110, 110, True),
            (True, (110, 110), 111, 111, True),
            (False, (60, 70), 59, 70, False),
            (False, (60, 70), 60, 69, False),
            (False, (60, 70), 60, 70, True),
            (False, (60, 70), 61, 71, True),
        ):
            with self.subTest(same_device=same_device, dataset_free=dataset_free, temp_free=temp_free), tempfile.TemporaryDirectory() as directory:
                recorder = self.make_recorder(directory)
                recorder.args.dataset_incremental_peak_bytes = 40
                recorder.args.encoder_temp_peak_bytes = 50
                recorder.args.disk_reserve_bytes = 20
                recorder.args.encoder_temp_dir = recorder.args.root
                sample = {
                    "dataset": {"path": str(recorder.args.root), "device": 1, "free_bytes": dataset_free, "total_bytes": 1000},
                    "encoder_temp": {"path": str(recorder.args.root), "device": 1 if same_device else 2, "free_bytes": temp_free, "total_bytes": 1000},
                }
                with mock.patch.object(recorder, "_storage_sample", return_value=sample):
                    result = recorder.begin_episode(self.transaction(directory))
                self.assertEqual(result["ok"], accepted)
                if accepted:
                    self.assertEqual(recorder._storage_monitor["required_free_bytes_by_device"], {
                        "1": required[0], **({} if same_device else {"2": required[1]})
                    })
                else:
                    self.assertEqual(result["reason_code"], "DISK_RESERVE")
                    self.assertFalse((Path(directory) / "runs" / "run-001").exists())

    def test_committed_factory_result_exposes_storage_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.args.dataset_incremental_peak_bytes = 40
            recorder.args.encoder_temp_peak_bytes = 50
            recorder.args.disk_reserve_bytes = 20
            sample = {
                "dataset": {"path": str(recorder.args.root), "device": 1, "free_bytes": 200, "total_bytes": 1000},
                "encoder_temp": {"path": str(recorder.args.root), "device": 1, "free_bytes": 200, "total_bytes": 1000},
            }
            with mock.patch.object(recorder, "_storage_sample", return_value=sample), mock.patch.object(recorder, "_tree_bytes", return_value=123):
                self.assertTrue(recorder.begin_episode(self.transaction(directory))["ok"])
                recorder.frames = 1
                recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, [])
                recorder.freeze_episode()
                result = recorder.commit_episode()
            self.assertTrue(result["ok"])
            storage = result["metrics"]["storage_usage"]
            self.assertEqual(
                {key: storage[key] for key in ("episode_index", "transaction_id", "staging_manifest_digest")},
                {"episode_index": 7, "transaction_id": "run-001:episode-000007", "staging_manifest_digest": recorder._transaction["staging_manifest_digest"]},
            )
            self.assertEqual((storage["dataset_bytes_before"], storage["dataset_bytes_after"]), (123, 123))
            self.assertEqual(storage["free_bytes_before_by_device"], {"1": 200})
            self.assertEqual(storage["free_bytes_by_device"], {"1": 200})
            self.assertEqual(storage["temp_peak_bytes_by_device"], {"1": 0})

    def test_commit_probe_captures_transient_encoder_temp_peak(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            temp_dir = Path(directory) / "encoder-temp"
            temp_dir.mkdir()
            recorder.args.dataset_incremental_peak_bytes = 1
            recorder.args.encoder_temp_peak_bytes = 1
            sample = {
                "dataset": {"path": str(recorder.args.root), "device": 1, "free_bytes": 200, "total_bytes": 1000},
                "encoder_temp": {"path": str(temp_dir), "device": 1, "free_bytes": 200, "total_bytes": 1000},
            }
            transient = temp_dir / "encode.tmp"
            initial_sample = threading.Event()
            observed = threading.Event()
            tree_bytes = recorder._tree_bytes

            def observe_temp(path):
                value = tree_bytes(path)
                if Path(path) == temp_dir:
                    initial_sample.set()
                    if transient.exists():
                        observed.set()
                return value

            def save_with_transient(parallel_encoding=True):
                self.assertTrue(initial_sample.is_set())
                transient.write_bytes(b"x" * 4096)
                self.assertTrue(observed.wait(1))
                transient.unlink()
                recorder.dataset.saves += 1

            recorder.dataset.save_episode = save_with_transient
            with mock.patch.object(recorder, "_storage_sample", return_value=sample), mock.patch.object(recorder, "_tree_bytes", side_effect=observe_temp):
                self.assertTrue(recorder.begin_episode(self.transaction(directory))["ok"])
                recorder.frames = 1
                recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, [])
                recorder.freeze_episode()
                result = recorder.commit_episode()
            self.assertTrue(result["ok"])
            self.assertGreaterEqual(result["metrics"]["storage_usage"]["temp_peak_bytes_by_device"]["1"], 4096)

    def test_encoder_temp_probe_start_waits_for_initial_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder._storage_monitor = {
                "begin": {"encoder_temp": {"path": directory, "device": 1}},
                "temp_bytes_before_by_device": {"1": 0}, "temp_peak_bytes_by_device": {"1": 0},
            }
            entered, release, returned = threading.Event(), threading.Event(), threading.Event()

            def blocked_sample(_path):
                entered.set()
                release.wait(1)
                return 0

            probe = []

            def start_probe():
                probe.append(recorder._start_encoder_temp_probe())
                returned.set()

            with mock.patch.object(recorder, "_tree_bytes", side_effect=blocked_sample):
                starter = threading.Thread(target=start_probe)
                starter.start()
                self.assertTrue(entered.wait(1))
                self.assertFalse(returned.wait(0.1))
                release.set()
                starter.join(1)
                self.assertTrue(returned.is_set())
                recorder._stop_encoder_temp_probe(probe[0])

    def test_encoder_temp_probe_start_timeout_joins_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder._storage_monitor = {
                "begin": {"encoder_temp": {"path": directory, "device": 1}},
                "temp_bytes_before_by_device": {"1": 0}, "temp_peak_bytes_by_device": {"1": 0},
            }
            threads = []
            real_thread = threading.Thread

            def capture_thread(*args, **kwargs):
                thread = real_thread(*args, **kwargs)
                threads.append(thread)
                return thread

            def slow_sample(_path):
                time.sleep(1.1)
                return 0

            with mock.patch.object(recorder_module.threading, "Thread", side_effect=capture_thread), \
                    mock.patch.object(recorder, "_tree_bytes", side_effect=slow_sample):
                with self.assertRaisesRegex(RuntimeError, "encoder temp probe startup timed out"):
                    recorder._start_encoder_temp_probe()
            self.assertEqual(len(threads), 1)
            self.assertFalse(threads[0].is_alive())

    def test_storage_status_latches_existing_writer_error_off_hot_path(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder._storage_monitor = {
                "reserve_bytes": 20, "dataset_incremental_peak_bytes": 40,
                "encoder_temp_peak_bytes": 50, "required_free_bytes_by_device": {"1": 110},
                "begin": {}, "dataset_bytes_before": 0, "temp_bytes_before_by_device": {"1": 0}, "temp_peak_bytes_by_device": {"1": 0},
                "last_check_monotonic": 0.0,
            }
            sample = {
                "dataset": {"path": str(recorder.args.root), "device": 1, "free_bytes": 109, "total_bytes": 1000},
                "encoder_temp": {"path": str(recorder.args.root), "device": 1, "free_bytes": 109, "total_bytes": 1000},
            }
            with mock.patch.object(recorder, "_storage_sample", return_value=sample), mock.patch.object(recorder, "_tree_bytes", return_value=0):
                recorder.episode_status()
            self.assertEqual(str(recorder.writer_error), "DISK_RESERVE_LOW")

    def test_post_encode_disk_reserve_low_quarantines_retained_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.args.dataset_incremental_peak_bytes = 40
            recorder.args.encoder_temp_peak_bytes = 50
            recorder.args.disk_reserve_bytes = 20
            healthy = {
                "dataset": {"path": str(recorder.args.root), "device": 1, "free_bytes": 110, "total_bytes": 1000},
                "encoder_temp": {"path": str(recorder.args.root), "device": 1, "free_bytes": 110, "total_bytes": 1000},
            }
            low = {
                "dataset": {"path": str(recorder.args.root), "device": 1, "free_bytes": 109, "total_bytes": 1000},
                "encoder_temp": {"path": str(recorder.args.root), "device": 1, "free_bytes": 109, "total_bytes": 1000},
            }
            with mock.patch.object(recorder, "_storage_sample", side_effect=(healthy, low)), mock.patch.object(recorder, "_tree_bytes", return_value=123):
                self.assertTrue(recorder.begin_episode(self.transaction(directory))["ok"])
                recorder.frames = 1
                recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, [])
                recorder.freeze_episode()
                result = recorder.commit_episode()
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason_code"], "DISK_RESERVE_LOW")
            self.assertEqual(recorder.episode_state, recorder.QUARANTINED_COMMIT)
            self.assertEqual(recorder.dataset.saves, 1)
            self.assertEqual(recorder.dataset.clears, 0)
            self.assertTrue((recorder.args.root / "meta" / "source_provenance" / "episode-000007.jsonl").exists())
            self.assertEqual(json.loads((recorder.args.root / "meta" / "quarantine.json").read_text())["reason_code"], "DISK_RESERVE_LOW")
            self.assertEqual(json.loads((Path(directory) / "runs" / "run-001" / "result.json").read_text())["reason_code"], "DISK_RESERVE_LOW")

    def test_factory_streaming_rejected_without_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.args.streaming_encoding = True
            approval = recorder.args.root / "meta" / "training_approved.json"
            approval.parent.mkdir(exist_ok=True)
            approval.write_text("{}")
            result = recorder.begin_episode(self.transaction(directory))
            self.assertEqual(result["reason_code"], "UNSUPPORTED_STAGING_MODE")
            self.assertTrue(approval.exists())
            self.assertFalse((Path(directory) / "runs").exists())
            self.assertEqual(recorder.episode_state, recorder.IDLE)

    def test_factory_no_video_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.args.no_videos = True
            result = recorder.begin_episode(self.transaction(directory))
            self.assertEqual(result["reason_code"], "UNSUPPORTED_STAGING_MODE")
            self.assertFalse((Path(directory) / "runs").exists())

    def test_factory_begin_invalidates_training_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            approval = recorder.args.root / "meta" / "training_approved.json"
            approval.parent.mkdir(exist_ok=True)
            approval.write_text("{}")
            self.assertTrue(recorder.begin_episode(self.transaction(directory))["ok"])
            self.assertFalse(approval.exists())

    def test_legacy_begin_has_no_run_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            self.assertTrue(recorder.begin_episode()["ok"])
            self.assertFalse((Path(directory) / "runs").exists())

    def test_status_is_read_only_and_reports_writer_health(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode()
            status = recorder.episode_status()
            self.assertEqual(status["state"], recorder.RECORDING)
            self.assertIn("writer_alive", status)
            self.assertEqual(
                set(status["metrics"]),
                {"rows", "writer_queue", "writer_queue_high_water", "writer_queue_drops", "alignment_failures", "observed_monotonic_ns", "quality_snapshot"},
            )
            snapshot = status["metrics"]["quality_snapshot"]
            self.assertFalse(snapshot["accepted"])
            self.assertTrue(any("minimum 60" in reason for reason in snapshot["reasons"]))
            recorder.frames = 1
            self.assertEqual(snapshot["frames"], 0)
            self.assertEqual(recorder.dataset.clears, 0)
            self.assertEqual(recorder.dataset.saves, 0)
            self.assertEqual(recorder.episode_state, recorder.RECORDING)

    def test_jsonl_commands_are_strict_idempotent_core_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            cache = {}
            begin = {
                "schema_version": "data_factory.recorder_command.v1",
                "op_id": "begin-1",
                "op": "begin",
                "transaction": self.transaction(directory),
            }
            first = process_recorder_control_line(recorder, json.dumps(begin), cache)
            self.assertTrue(first["ok"])
            self.assertEqual(first, process_recorder_control_line(recorder, json.dumps(begin), cache))
            self.assertEqual(recorder.episode_state, recorder.RECORDING)

            freeze = {"schema_version": begin["schema_version"], "op_id": "freeze-1", "op": "freeze"}
            self.assertTrue(process_recorder_control_line(recorder, json.dumps(freeze), cache)["ok"])
            abort = {"schema_version": begin["schema_version"], "op_id": "abort-1", "op": "abort"}
            result = process_recorder_control_line(recorder, json.dumps(abort), cache)
            self.assertTrue(result["ok"])
            self.assertEqual(result["state"], recorder.ABORTED)
            self.assertEqual(recorder.dataset.clears, 1)

    def test_jsonl_response_schema_is_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            status = process_recorder_control_line(recorder, json.dumps({
                "schema_version": "data_factory.recorder_command.v1",
                "op_id": "status-1",
                "op": "status",
            }), {})
            self.assertEqual(set(status), self.CONTROL_RESPONSE_FIELDS | {"writer_error", "writer_alive"})
            self.assertIsInstance(status["ok"], bool)
            self.assertIsInstance(status["metrics"], dict)
            failure = process_recorder_control_line(recorder, "[]", {})
            self.assertEqual(set(failure), self.CONTROL_RESPONSE_FIELDS)
            self.assertIsNone(failure["op_id"])
            self.assertIsNone(failure["op"])

    def test_jsonl_protocol_violation_aborts_active_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            begin = {
                "schema_version": "data_factory.recorder_command.v1",
                "op_id": "begin-1",
                "op": "begin",
                "transaction": self.transaction(directory),
            }
            cache = {}
            process_recorder_control_line(recorder, json.dumps(begin), cache)
            response = process_recorder_control_line(
                recorder,
                '{"schema_version":"data_factory.recorder_command.v1","op_id":"x","op":"status","op":"abort"}',
                cache,
            )
            self.assertEqual(response["reason_code"], "CONTROL_INVALID_JSON")
            self.assertEqual(response["state"], recorder.ABORTED)
            self.assertEqual(set(response), self.CONTROL_RESPONSE_FIELDS | {"abort_reason_code"})
            self.assertEqual(recorder.dataset.clears, 1)

    def test_jsonl_rejects_nonfinite_extra_and_nonobject_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            cache = {}
            cases = (
                ("[]", "CONTROL_REQUEST_TYPE"),
                ('{"schema_version":"data_factory.recorder_command.v1","op_id":"x","op":"status","extra":1}', "CONTROL_FIELDS"),
                ('{"schema_version":"data_factory.recorder_command.v1","op_id":"x","op":"status","value":NaN}', "CONTROL_INVALID_JSON"),
            )
            for line, code in cases:
                self.assertEqual(process_recorder_control_line(recorder, line, cache)["reason_code"], code)
            self.assertEqual(recorder.episode_state, recorder.IDLE)
            self.assertEqual(recorder.dataset.clears, 0)

    def test_jsonl_op_id_conflict_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            cache = {}
            begin = {
                "schema_version": "data_factory.recorder_command.v1",
                "op_id": "same-id",
                "op": "begin",
                "transaction": self.transaction(directory),
            }
            process_recorder_control_line(recorder, json.dumps(begin), cache)
            conflict = {
                "schema_version": "data_factory.recorder_command.v1",
                "op_id": "same-id",
                "op": "status",
            }
            response = process_recorder_control_line(recorder, json.dumps(conflict), cache)
            self.assertEqual(response["reason_code"], "CONTROL_OP_ID_CONFLICT")
            self.assertEqual(response["state"], recorder.ABORTED)
            self.assertEqual(recorder.dataset.clears, 1)

    def test_jsonl_loop_emits_only_responses_and_eof_aborts(self):
        schema = "data_factory.recorder_command.v1"
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            commands = [
                {"schema_version": schema, "op_id": "begin-1", "op": "begin", "transaction": self.transaction(directory)},
                {"schema_version": schema, "op_id": "status-1", "op": "status"},
                {"schema_version": schema, "op_id": "abort-1", "op": "abort"},
            ]
            output = io.StringIO()
            self.assertTrue(run_recorder_control_jsonl(
                recorder,
                io.StringIO("".join(json.dumps(command) + "\n" for command in commands)),
                output,
                lambda: time.sleep(0.001),
            ))
            responses = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([item["op_id"] for item in responses], ["begin-1", "status-1", "abort-1"])
            self.assertTrue(all(item["schema_version"] == "data_factory.recorder_response.v1" for item in responses))

        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            begin = {"schema_version": schema, "op_id": "begin-1", "op": "begin", "transaction": self.transaction(directory)}
            self.assertFalse(run_recorder_control_jsonl(
                recorder,
                io.StringIO(json.dumps(begin) + "\n"),
                io.StringIO(),
                lambda: time.sleep(0.001),
            ))
            self.assertEqual(recorder.episode_state, recorder.ABORTED)
            self.assertEqual(recorder.dataset.clears, 1)

        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            status = {"schema_version": schema, "op_id": "status-1", "op": "status"}
            self.assertTrue(run_recorder_control_jsonl(
                recorder,
                io.StringIO(json.dumps(status) + "\n"),
                io.StringIO(),
                lambda: time.sleep(0.001),
            ))

    def test_jsonl_loop_commit_is_terminal_and_saves_once(self):
        schema = "data_factory.recorder_command.v1"
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            begin = {"schema_version": schema, "op_id": "begin-1", "op": "begin", "transaction": self.transaction(directory)}
            freeze = {"schema_version": schema, "op_id": "freeze-1", "op": "freeze"}
            commit = {"schema_version": schema, "op_id": "commit-1", "op": "commit"}
            release = threading.Event()

            def commands():
                yield json.dumps(begin) + "\n"
                release.wait(timeout=1)
                yield json.dumps(freeze) + "\n"
                yield json.dumps(commit) + "\n"

            def spin_once():
                if recorder.episode_state == recorder.RECORDING:
                    recorder.frames = 1
                    recorder._quality_summary = lambda: ({
                        "episode_index": 7,
                        "effective_fps": 30.0,
                        "image_quality_warnings": [],
                    }, [])
                    release.set()
                time.sleep(0.001)

            output = io.StringIO()
            self.assertTrue(run_recorder_control_jsonl(recorder, commands(), output, spin_once))
            responses = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([item["state"] for item in responses], [recorder.RECORDING, recorder.FROZEN, recorder.COMMITTED])
            self.assertEqual(set(responses[-1]), self.CONTROL_RESPONSE_FIELDS | {"quality"})
            self.assertIsInstance(responses[-1]["quality"], dict)
            self.assertEqual(recorder.dataset.saves, 1)
            self.assertEqual(recorder.dataset.finalizes, 1)

    def test_jsonl_input_error_is_not_clean_eof(self):
        schema = "data_factory.recorder_command.v1"

        def broken_after(command):
            yield json.dumps(command) + "\n"
            raise OSError("pipe read failed")

        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            status = {"schema_version": schema, "op_id": "status-1", "op": "status"}
            output = io.StringIO()
            self.assertFalse(run_recorder_control_jsonl(
                recorder, broken_after(status), output, lambda: time.sleep(0.001)
            ))
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["reason_code"], "CONTROL_INPUT_FAILED")

        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            begin = {"schema_version": schema, "op_id": "begin-1", "op": "begin", "transaction": self.transaction(directory)}
            self.assertFalse(run_recorder_control_jsonl(
                recorder, broken_after(begin), io.StringIO(), lambda: time.sleep(0.001)
            ))
            self.assertEqual(recorder.episode_state, recorder.ABORTED)
            self.assertEqual(recorder.dataset.clears, 1)

    def test_factory_jsonl_cli_requires_run_root_and_batch_video(self):
        base = ["recorder", "--task", "pick up", "--factory-jsonl"]
        with mock.patch.object(sys, "argv", base):
            with self.assertRaisesRegex(SystemExit, "requires --run-root"):
                parse_args()
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(sys, "argv", base + ["--run-root", str(Path(directory) / "runs")]):
                with self.assertRaisesRegex(SystemExit, "requires batch video encoding"):
                    parse_args()
            with mock.patch.object(sys, "argv", base + [
                "--run-root", str(Path(directory) / "runs"),
                "--root", str(Path(directory) / "dataset"),
                "--batch-video-encoding",
            ]):
                self.assertTrue(parse_args().factory_jsonl)
        with mock.patch.object(sys, "argv", base + ["--interactive"]), mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args()

    def test_factory_jsonl_main_reserves_stdout_for_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.close = lambda: None
            recorder.destroy_node = lambda: None
            status = json.dumps({
                "schema_version": "data_factory.recorder_command.v1",
                "op_id": "status-1",
                "op": "status",
            }) + "\n"
            output = io.StringIO()
            args = SimpleNamespace(factory_jsonl=True, interactive=False)
            with (
                mock.patch.object(recorder_module, "parse_args", return_value=args),
                mock.patch.object(recorder_module, "FR5LeRobotRecorder", return_value=recorder),
                mock.patch.object(recorder_module.rclpy, "init"),
                mock.patch.object(recorder_module.rclpy, "spin_once", side_effect=lambda *_args, **_kwargs: time.sleep(0.001)),
                mock.patch.object(recorder_module.rclpy, "ok", return_value=False),
                mock.patch.object(recorder_module.sys, "stdin", io.StringIO(status)),
                mock.patch.object(recorder_module.sys, "stdout", output),
                mock.patch.dict(os.environ, {}, clear=False),
            ):
                recorder_module.main()
                self.assertEqual(os.environ["RCUTILS_LOGGING_USE_STDOUT"], "0")
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["op_id"], "status-1")

    def test_constructor_preserves_legacy_autostart_and_defers_factory_begin(self):
        with tempfile.TemporaryDirectory() as directory:
            def arguments(*extra):
                argv = [
                    "recorder", "--task", "pick up", "--root", str(Path(directory) / "dataset"),
                    *extra,
                ]
                with mock.patch.object(sys, "argv", argv):
                    return parse_args()

            thread = SimpleNamespace(start=lambda: None, is_alive=lambda: True)
            lerobot = ModuleType("lerobot")
            lerobot.__path__ = []
            datasets = ModuleType("lerobot.datasets")
            datasets.__path__ = []
            dataset_module = ModuleType("lerobot.datasets.lerobot_dataset")
            dataset_module.LeRobotDataset = object
            patches = (
                mock.patch.dict(sys.modules, {
                    "lerobot": lerobot,
                    "lerobot.datasets": datasets,
                    "lerobot.datasets.lerobot_dataset": dataset_module,
                }),
                mock.patch.object(recorder_module.Node, "__init__", return_value=None),
                mock.patch.object(FR5LeRobotRecorder, "_open_dataset", return_value=_Dataset()),
                mock.patch.object(FR5LeRobotRecorder, "create_subscription", return_value=None),
                mock.patch.object(FR5LeRobotRecorder, "get_logger", return_value=_Logger()),
                mock.patch.object(recorder_module.threading, "Thread", return_value=thread),
                mock.patch.object(FR5LeRobotRecorder, "begin_episode", return_value={"ok": True}),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6] as begin:
                FR5LeRobotRecorder(arguments())
                begin.assert_called_once_with()
                begin.reset_mock()
                FR5LeRobotRecorder(arguments(
                    "--factory-jsonl", "--batch-video-encoding", "--run-root", str(Path(directory) / "runs")
                ))
                begin.assert_not_called()

    def test_transaction_rejects_invalid_digest_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            context = self.transaction(directory)
            context["binding_digests"].pop("grasp_profile_digest")
            with self.assertRaisesRegex(ValueError, "exact sha256"):
                recorder.begin_episode(context)

    def test_transaction_rejects_reused_run_and_nested_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            context = self.transaction(directory)
            recorder.begin_episode(context)
            self.assertTrue(recorder.abort_episode()["ok"])
            self.assertFalse((recorder.args.root / "meta" / "quarantine.json").exists())
            self.assertEqual(
                recorder.begin_episode({**context, "run_id": "run-002"})["reason_code"],
                "PROCESS_TRANSACTION_ALREADY_USED",
            )
            resumed = self.make_recorder(directory)
            with self.assertRaises(RecoveryError) as conflict:
                resumed.begin_episode(context)
            self.assertEqual(conflict.exception.code, "RUN_EVIDENCE_DIRECTORY_CONFLICT")
            nested = self.make_recorder(directory)
            nested.args.run_root = nested.args.root / "runs"
            with self.assertRaisesRegex(ValueError, "must be separate"):
                nested.begin_episode(self.transaction(directory))

    def test_transaction_accepts_only_bound_runner_preapproval_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "runs" / "run-001"
            run_dir.mkdir(parents=True)
            evidence = {
                "camera_warmup.json": "data_factory.camera_warmup.v1",
                "preapproval_evidence.json": "data_factory.preapproval_evidence.v1",
            }
            for name, schema in evidence.items():
                (run_dir / name).write_text(json.dumps({"schema_version": schema, "run_id": "run-001"}))
            recorder = self.make_recorder(directory)
            self.assertTrue(recorder.begin_episode(self.transaction(directory))["ok"])
            self.assertTrue((run_dir / "staging_manifest.json").is_file())
            self.assertEqual(set(evidence), {path.name for path in run_dir.iterdir() if path.name in evidence})
            self.assertTrue(recorder.abort_episode()["ok"])

            conflict_dir = Path(directory) / "runs" / "run-002"
            conflict_dir.mkdir()
            (conflict_dir / "unexpected.json").write_text("{}")
            conflict = self.make_recorder(directory)
            request = {
                "schema_version": "data_factory.recorder_command.v1",
                "op_id": "begin-conflict",
                "op": "begin",
                "transaction": {**self.transaction(directory), "run_id": "run-002"},
            }
            response = process_recorder_control_line(conflict, json.dumps(request), {})
            self.assertEqual(response["reason_code"], "RUN_EVIDENCE_DIRECTORY_CONFLICT")
            self.assertEqual(conflict.episode_state, conflict.IDLE)

    def test_begin_journal_failure_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder._append_event = lambda *_: (_ for _ in ()).throw(OSError("journal unavailable"))
            result = recorder.begin_episode(self.transaction(directory))
            self.assertEqual(result["reason_code"], "BEGIN_JOURNAL_FAILED")
            self.assertEqual(recorder.episode_state, recorder.QUARANTINED_COMMIT)
            self.assertFalse(recorder.recording)
            self.assertTrue((recorder.args.root / "meta" / "quarantine.json").exists())

    def test_freeze_drains_queued_work_then_blocks_late_append(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode()
            written = []
            recorder._write_frame = lambda value: written.append(value) if recorder.episode_state in (recorder.RECORDING, recorder.FREEZING) else None
            recorder.writer_thread = threading.Thread(target=recorder._writer_loop, daemon=True)
            recorder.writer_thread.start()
            recorder.writer_queue.put(("queued",))
            self.assertTrue(recorder.freeze_episode()["ok"])
            recorder._write_frame("late")
            self.assertEqual(written, ["queued"])
            frames_before = recorder.frames
            FR5LeRobotRecorder._write_frame(recorder, None, None, (), {}, 0, 0, 0, 0)
            self.assertEqual(recorder.frames, frames_before)
            self.assertEqual(recorder.dataset.saves, 0)
            recorder.stop_threads.set()
            recorder.writer_thread.join(1)

    def test_readiness_prefix_is_discarded_without_reopening_the_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            self.assertTrue(recorder.begin_episode(self.transaction(directory))["ok"])
            transaction_id = recorder._transaction["transaction_id"]
            recorder.frames = 60
            recorder.frame_stamps = [index / 30 for index in range(60)]
            recorder.source_provenance = [{} for _ in range(60)]
            result = recorder.trim_readiness_prefix()
            self.assertEqual(
                (result["ok"], result["state"], result["reason_code"], result["metrics"]["rows"]),
                (True, recorder.RECORDING, "READINESS_PREFIX_TRIMMED", 0),
            )
            self.assertEqual(recorder.dataset.clears, 1)
            self.assertEqual(recorder._transaction["transaction_id"], transaction_id)
            self.assertTrue(recorder.recording)
            self.assertEqual(recorder.next_target_stamp, 1.0)

            recorder.writer_error = RuntimeError("writer failed")
            blocked = recorder.trim_readiness_prefix()
            self.assertEqual((blocked["ok"], blocked["reason_code"]), (False, "READINESS_PREFIX_UNSAFE"))
            self.assertEqual(recorder.dataset.clears, 1)

    def test_abort_is_idempotent_and_recorder_is_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode()
            self.assertTrue(recorder.abort_episode()["ok"])
            self.assertEqual(recorder.dataset.clears, 1)
            self.assertFalse(recorder.abort_episode()["ok"])
            self.assertEqual(recorder.dataset.clears, 1)
            self.assertTrue(recorder.begin_episode()["ok"])

    def test_commit_requires_frozen_quality_and_saves_once(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode()
            recorder.frames = 1
            recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, [])
            self.assertFalse(recorder.commit_episode()["ok"])
            recorder.freeze_episode()
            self.assertTrue(recorder.commit_episode()["ok"])
            self.assertEqual(recorder.dataset.saves, 1)
            self.assertFalse(recorder.dataset.parallel_encoding)
            self.assertFalse((recorder.args.root / "meta" / "quarantine.json").exists())
            self.assertFalse(recorder.commit_episode()["ok"])
            self.assertEqual(recorder.dataset.saves, 1)

    def test_concurrent_commit_allows_one_save(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode()
            recorder.frames = 1
            recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, [])
            entered = threading.Event()
            release = threading.Event()

            def save(parallel_encoding=True):
                recorder.dataset.saves += 1
                recorder.dataset.parallel_encoding = parallel_encoding
                entered.set()
                release.wait(1)

            recorder.dataset.save_episode = save
            recorder.freeze_episode()
            first = threading.Thread(target=recorder.commit_episode)
            first.start()
            self.assertTrue(entered.wait(1))
            second = recorder.commit_episode()
            release.set()
            first.join(1)
            self.assertFalse(second["ok"])
            self.assertEqual(second["reason_code"], "STATE_COMMIT_NOT_FROZEN")
            self.assertEqual(recorder.dataset.saves, 1)

    def test_commit_exception_quarantines_without_clear_and_records_event(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory, RuntimeError("save failed"))
            recorder.begin_episode(self.transaction(directory))
            recorder.frames = 1
            recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, [])
            recorder.freeze_episode()
            result = recorder.commit_episode()
            self.assertEqual(result["reason_code"], "QUARANTINED_COMMIT")
            self.assertEqual(recorder.dataset.clears, 0)
            events = (Path(directory) / "runs" / "run-001" / "events.jsonl").read_text()
            self.assertIn("QUARANTINED_COMMIT", events)
            marker = json.loads((recorder.args.root / "meta" / "quarantine.json").read_text())
            result_file = json.loads((Path(directory) / "runs" / "run-001" / "result.json").read_text())
            self.assertEqual(marker["state"], recorder.QUARANTINED_COMMIT)
            self.assertEqual(result_file["state"], recorder.QUARANTINED_COMMIT)

    def test_success_removes_commit_guard_after_durable_result(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode(self.transaction(directory))
            recorder.frames = 1
            recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, [])
            marker = recorder.args.root / "meta" / "quarantine.json"
            guard_seen_during_finalize = []
            finalize = recorder.dataset.finalize
            recorder.dataset.finalize = lambda: (guard_seen_during_finalize.append(marker.exists()), finalize())[1]
            recorder.freeze_episode()
            self.assertTrue(recorder.commit_episode()["ok"])
            self.assertEqual(guard_seen_during_finalize, [True])
            self.assertFalse(marker.exists())
            result_file = json.loads((Path(directory) / "runs" / "run-001" / "result.json").read_text())
            self.assertEqual(result_file["state"], recorder.COMMITTED)

    def test_finalize_failure_keeps_factory_dataset_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory, finalize_error=OSError("footer failed"))
            recorder.begin_episode(self.transaction(directory))
            recorder.frames = 1
            recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, [])
            recorder.freeze_episode()
            result = recorder.commit_episode()
            self.assertEqual(result["reason_code"], "QUARANTINED_COMMIT")
            self.assertEqual((recorder.dataset.saves, recorder.dataset.finalizes), (1, 1))
            self.assertTrue((recorder.args.root / "meta" / "quarantine.json").exists())

    def test_sidecar_fault_after_save_quarantines(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode(self.transaction(directory))
            recorder.frames = 1
            recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, [])

            def save_then_block_quality_sidecar(parallel_encoding=True):
                recorder.dataset.saves += 1
                recorder.dataset.parallel_encoding = parallel_encoding
                (recorder.args.root / "meta" / "recording_quality.jsonl").mkdir()

            recorder.dataset.save_episode = save_then_block_quality_sidecar
            recorder.freeze_episode()
            self.assertEqual(recorder.commit_episode()["state"], recorder.QUARANTINED_COMMIT)
            self.assertEqual(recorder.dataset.clears, 0)

    def test_quality_diagnostic_write_failure_aborts_before_save(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode()
            recorder.frames = 1
            recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, ["quality failed"])
            attempts = recorder.args.root / "meta" / "recording_attempts.jsonl"
            attempts.parent.mkdir(exist_ok=True)
            attempts.mkdir()
            recorder.freeze_episode()
            result = recorder.commit_episode()
            self.assertEqual(result["reason_code"], "PRECOMMIT_DIAGNOSTIC_FAILED")
            self.assertEqual(recorder.episode_state, recorder.ABORTED)
            self.assertEqual((recorder.dataset.saves, recorder.dataset.clears), (0, 1))

    def test_provenance_serialization_failure_aborts_before_save(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode()
            recorder.frames = 1
            recorder.source_provenance = [{"not_json": {1}}]
            recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, [])
            recorder.freeze_episode()
            result = recorder.commit_episode()
            self.assertEqual(result["reason_code"], "PRECOMMIT_PROVENANCE_FAILED")
            self.assertEqual(recorder.episode_state, recorder.ABORTED)
            self.assertEqual((recorder.dataset.saves, recorder.dataset.clears), (0, 1))
            self.assertFalse((recorder.args.root / "meta" / "source_provenance" / "episode-000007.jsonl.tmp").exists())

    def test_precommit_cleanup_failure_is_not_reported_aborted(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory, clear_error=OSError("cleanup failed"))
            recorder.begin_episode(self.transaction(directory))
            recorder.frames = 1
            recorder.source_provenance = [{"not_json": {1}}]
            recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, [])
            recorder.freeze_episode()
            result = recorder.commit_episode()
            self.assertEqual(result["reason_code"], "PRECOMMIT_CLEANUP_FAILED")
            self.assertEqual(recorder.episode_state, recorder.QUARANTINED_COMMIT)

    def test_provenance_temp_cleanup_failure_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode(self.transaction(directory))
            recorder.frames = 1
            recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, [])
            temporary = recorder.args.root / "meta" / "source_provenance" / "episode-000007.jsonl.tmp"
            temporary.mkdir(parents=True)
            recorder.freeze_episode()
            result = recorder.commit_episode()
            self.assertEqual(result["reason_code"], "PRECOMMIT_CLEANUP_FAILED")
            self.assertEqual(recorder.episode_state, recorder.QUARANTINED_COMMIT)

    def test_abort_cleanup_failure_is_durably_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory, clear_error=OSError("cleanup failed"))
            recorder.begin_episode(self.transaction(directory))
            result = recorder.abort_episode()
            self.assertEqual(result["reason_code"], "ABORT_CLEANUP_FAILED")
            self.assertEqual(recorder.episode_state, recorder.QUARANTINED_COMMIT)
            self.assertTrue((recorder.args.root / "meta" / "quarantine.json").exists())

    def test_abort_requires_empty_staging_and_unchanged_committed_snapshot(self):
        for mutation in ("staging", "committed"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                recorder = self.make_recorder(directory)
                recorder.begin_episode(self.transaction(directory))
                if mutation == "staging":
                    recorder.dataset.clear_episode_buffer = lambda: None
                else:
                    data = recorder.args.root / "data" / "chunk-000" / "file-000.parquet"
                    data.parent.mkdir(parents=True)
                    data.write_bytes(b"changed")
                result = recorder.abort_episode()
                self.assertEqual(result["reason_code"], "ABORT_CLEANUP_FAILED")
                self.assertEqual(recorder.episode_state, recorder.QUARANTINED_COMMIT)

    def test_existing_quarantine_blocks_new_factory_run(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            marker = recorder.args.root / "meta" / "quarantine.json"
            marker.parent.mkdir(exist_ok=True)
            marker.write_text("{}")
            with self.assertRaisesRegex(ValueError, "unresolved data factory commit guard"):
                recorder.begin_episode(self.transaction(directory))

    def test_inflight_begin_guard_blocks_second_factory_process(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.make_recorder(directory)
            first.begin_episode(self.transaction(directory))
            second = self.make_recorder(directory)
            context = self.transaction(directory)
            context["run_id"] = "run-002"
            self.assertEqual(second.begin_episode(context)["reason_code"], "DATASET_TRANSACTION_BUSY")
            first.freeze_episode()
            self.assertEqual(second.begin_episode(context)["reason_code"], "DATASET_TRANSACTION_BUSY")

    def test_factory_lock_is_held_until_clean_abort_and_guard_tracks_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            self.assertTrue(recorder.begin_episode(self.transaction(directory))["ok"])
            contender = DatasetTransactionLock(recorder.args.root)
            with self.assertRaises(RecoveryError) as caught:
                contender.acquire()
            self.assertEqual(caught.exception.code, "DATASET_TRANSACTION_BUSY")
            self.assertTrue(recorder.freeze_episode()["ok"])
            marker = json.loads((recorder.args.root / "meta" / "quarantine.json").read_text())
            self.assertEqual(marker["state"], recorder.FROZEN)
            self.assertTrue(recorder.abort_episode()["ok"])
            contender.acquire()
            contender.release()

    def test_factory_refuses_preexisting_episode_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            staging = recorder.args.root / "images" / "observation.images.up" / "episode-000007"
            staging.mkdir(parents=True)
            sentinel = staging / "user-file.bin"
            sentinel.write_bytes(b"keep")
            with self.assertRaisesRegex(ValueError, "pre-existing or unsafe episode staging"):
                recorder.begin_episode(self.transaction(directory))
            self.assertEqual(sentinel.read_bytes(), b"keep")
            self.assertFalse(recorder.args.run_root.exists())
            lock = DatasetTransactionLock(recorder.args.root)
            lock.acquire()
            lock.release()

    def test_recording_and_frozen_orphans_recover_only_manifest_staging(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen), tempfile.TemporaryDirectory() as directory:
                recorder = self.make_recorder(directory)
                self.assertTrue(recorder.begin_episode(self.transaction(directory))["ok"])
                if frozen:
                    self.assertTrue(recorder.freeze_episode()["ok"])
                for staging in recorder._transaction["staging_dirs"]:
                    path = Path(staging)
                    (path / "frame.png").write_bytes(b"staged")
                unrelated = recorder.args.root / "images" / "keep" / "sentinel.bin"
                unrelated.parent.mkdir(parents=True)
                unrelated.write_bytes(b"keep")
                before = dataset_snapshot(recorder.args.root)
                recorder._release_transaction_lock()  # Simulated abrupt process death.

                result = recover_orphaned_transaction(recorder.args.root, recorder.args.run_root)
                self.assertEqual(result["reason_code"], "RECOVERED_ABORT")
                self.assertFalse((recorder.args.root / "meta" / "quarantine.json").exists())
                self.assertTrue(all(not Path(path).exists() for path in recorder._transaction["staging_dirs"]))
                self.assertEqual(unrelated.read_bytes(), b"keep")
                self.assertEqual(dataset_snapshot(recorder.args.root), before)
                run_dir = recorder.args.run_root / "run-001"
                self.assertEqual(json.loads((run_dir / "result.json").read_text())["state"], recorder.ABORTED)
                events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
                self.assertEqual(sum(event["reason_code"] == "RECOVERED_ABORT" for event in events), 1)
                self.assertEqual(recover_orphaned_transaction(recorder.args.root, recorder.args.run_root)["reason_code"], "NO_ORPHAN")
                if not frozen:
                    cli = subprocess.run(
                        [sys.executable, str(Path(__file__).resolve().parents[1] / "tools/data_factory_recovery.py"), "--dataset-root", str(recorder.args.root), "--run-root", str(recorder.args.run_root)],
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(cli.returncode, 0, cli.stderr)
                    self.assertEqual(json.loads(cli.stdout)["reason_code"], "NO_ORPHAN")

    def test_recovery_requires_complete_journal_and_owned_staging(self):
        for failure in ("journal", "owner"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                recorder = self.make_recorder(directory)
                recorder.begin_episode(self.transaction(directory))
                staging = Path(recorder._transaction["staging_dirs"][0])
                sentinel = staging / "user-file.bin"
                sentinel.write_bytes(b"keep")
                if failure == "journal":
                    (recorder.args.run_root / "run-001" / "events.jsonl").write_text("not-json\n")
                else:
                    (staging / ".data_factory_staging_owner.json").unlink()
                recorder._release_transaction_lock()

                result = recover_orphaned_transaction(recorder.args.root, recorder.args.run_root)
                self.assertEqual(result["reason_code"], "RECOVERY_EVENT" if failure == "journal" else "RECOVERY_STAGING_OWNER")
                self.assertEqual(sentinel.read_bytes(), b"keep")
                self.assertTrue((recorder.args.root / "meta" / "quarantine.json").exists())

    def test_recovery_detects_same_size_committed_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            data = recorder.args.root / "data" / "chunk-000" / "file-000.parquet"
            data.parent.mkdir(parents=True)
            data.write_bytes(b"old-data")
            recorder.begin_episode(self.transaction(directory))
            before = data.stat()
            data.write_bytes(b"new-data")
            os.utime(data, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            recorder._release_transaction_lock()

            result = recover_orphaned_transaction(recorder.args.root, recorder.args.run_root)
            self.assertEqual(result["reason_code"], "RECOVERY_SNAPSHOT_CHANGED")
            self.assertEqual(data.read_bytes(), b"new-data")
            self.assertTrue(Path(recorder._transaction["staging_dirs"][0]).exists())

    def test_recovery_atomic_writes_ignore_predictable_temp_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode(self.transaction(directory))
            Path(recorder._transaction["staging_dirs"][0], "frame.png").write_bytes(b"staged")
            external = Path(directory) / "outside.txt"
            external.write_text("keep")
            guard = recorder.args.root / "meta" / "quarantine.json"
            result_path = recorder.args.run_root / "run-001" / "result.json"
            guard.with_name(guard.name + ".tmp").symlink_to(external)
            result_path.with_name(result_path.name + ".tmp").symlink_to(external)
            recorder._release_transaction_lock()

            self.assertEqual(recover_orphaned_transaction(recorder.args.root, recorder.args.run_root)["reason_code"], "RECOVERED_ABORT")
            self.assertEqual(external.read_text(), "keep")

    def test_recovery_delete_is_anchored_against_parent_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode(self.transaction(directory))
            Path(recorder._transaction["staging_dirs"][0], "frame.png").write_bytes(b"staged")
            recorder._release_transaction_lock()
            original_validate = recovery._validate_manifest
            moved = Path(directory) / "owned-images"
            outside = Path(directory) / "outside-images"
            outside_episode = outside / "observation.images.up" / "episode-000007"
            outside_episode.mkdir(parents=True)
            outside_sentinel = outside_episode / "sentinel.bin"
            outside_sentinel.write_bytes(b"keep")

            def validate_then_swap(*args):
                paths = original_validate(*args)
                (recorder.args.root / "images").rename(moved)
                (recorder.args.root / "images").symlink_to(outside, target_is_directory=True)
                return paths

            with mock.patch.object(recovery, "_validate_manifest", side_effect=validate_then_swap):
                result = recover_orphaned_transaction(recorder.args.root, recorder.args.run_root)
            self.assertEqual(result["reason_code"], "RECOVERY_ABORT_FAILED")
            self.assertEqual(outside_sentinel.read_bytes(), b"keep")
            self.assertTrue((moved / "observation.images.up" / "episode-000007" / "frame.png").exists())

    def test_recovery_guard_is_anchored_against_late_meta_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode(self.transaction(directory))
            Path(recorder._transaction["staging_dirs"][0], "frame.png").write_bytes(b"staged")
            recorder._release_transaction_lock()
            original_snapshot = recovery.dataset_snapshot
            calls = 0
            moved = Path(directory) / "owned-meta"
            outside = Path(directory) / "outside-meta"
            outside.mkdir()
            outside_guard = outside / "quarantine.json"
            outside_guard.write_text("keep")

            def snapshot_then_swap(root):
                nonlocal calls
                snapshot = original_snapshot(root)
                calls += 1
                if calls == 2:
                    (recorder.args.root / "meta").rename(moved)
                    (recorder.args.root / "meta").symlink_to(outside, target_is_directory=True)
                return snapshot

            with mock.patch.object(recovery, "dataset_snapshot", side_effect=snapshot_then_swap):
                result = recover_orphaned_transaction(recorder.args.root, recorder.args.run_root)
            self.assertEqual(result["reason_code"], "RECOVERY_DIRECTORY_CHANGED")
            self.assertEqual(outside_guard.read_text(), "keep")
            self.assertEqual(json.loads((moved / "quarantine.json").read_text())["state"], "QUARANTINED_COMMIT")

    def test_recovery_pins_run_directory_before_manifest_read(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode(self.transaction(directory))
            staging = Path(recorder._transaction["staging_dirs"][0])
            (staging / "frame.png").write_bytes(b"staged")
            recorder._release_transaction_lock()
            run_dir = recorder.args.run_root / "run-001"
            moved = recorder.args.run_root / "moved-run"
            original_json_at = recovery._json_at

            def read_then_swap(directory_fd, name, code):
                value = original_json_at(directory_fd, name, code)
                if name == "staging_manifest.json":
                    run_dir.rename(moved)
                    run_dir.mkdir()
                return value

            with mock.patch.object(recovery, "_json_at", side_effect=read_then_swap):
                result = recover_orphaned_transaction(recorder.args.root, recorder.args.run_root)
            self.assertEqual(result["reason_code"], "RECOVERY_DIRECTORY_CHANGED")
            self.assertTrue(staging.exists())
            self.assertTrue((recorder.args.root / "meta" / "quarantine.json").exists())

    def test_recovery_pins_dataset_root_before_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode(self.transaction(directory))
            Path(recorder._transaction["staging_dirs"][0], "frame.png").write_bytes(b"owned")
            recorder._release_transaction_lock()
            original_snapshot = recovery.dataset_snapshot
            moved = Path(directory) / "owned-dataset"
            replacement_sentinel = recorder.args.root / "images" / "observation.images.up" / "episode-000007" / "sentinel.bin"
            swapped = False

            def snapshot_then_swap(root):
                nonlocal swapped
                snapshot = original_snapshot(root)
                if not swapped:
                    recorder.args.root.rename(moved)
                    replacement_sentinel.parent.mkdir(parents=True)
                    replacement_sentinel.write_bytes(b"keep")
                    swapped = True
                return snapshot

            with mock.patch.object(recovery, "dataset_snapshot", side_effect=snapshot_then_swap):
                result = recover_orphaned_transaction(recorder.args.root, recorder.args.run_root)
            self.assertEqual(result["reason_code"], "RECOVERY_DIRECTORY_CHANGED")
            self.assertEqual(replacement_sentinel.read_bytes(), b"keep")
            self.assertTrue((moved / "images" / "observation.images.up" / "episode-000007" / "frame.png").exists())
            self.assertEqual(json.loads((moved / "meta" / "quarantine.json").read_text())["state"], "QUARANTINED_COMMIT")

    def test_recovery_keeps_ambiguous_changed_and_unsafe_transactions_quarantined(self):
        for failure in ("committing", "snapshot", "outside", "symlink", "meta_symlink"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                recorder = self.make_recorder(directory)
                recorder.begin_episode(self.transaction(directory))
                staging = Path(recorder._transaction["staging_dirs"][0])
                (staging / "frame.png").write_bytes(b"staged")
                if failure == "committing":
                    recorder._write_commit_guard(recorder.COMMITTING, "COMMIT_STARTED")
                elif failure == "snapshot":
                    data = recorder.args.root / "data" / "chunk-000" / "file-000.parquet"
                    data.parent.mkdir(parents=True)
                    data.write_bytes(b"committed delta")
                elif failure == "outside":
                    manifest_path = recorder.args.run_root / "run-001" / "staging_manifest.json"
                    manifest = json.loads(manifest_path.read_text())
                    outside = Path(directory) / "outside"
                    outside.mkdir()
                    (outside / "sentinel").write_text("keep")
                    manifest["camera_staging_dirs"]["up"] = str(outside)
                    recorder._write_json_atomic(manifest_path, manifest)
                    marker_path = recorder.args.root / "meta" / "quarantine.json"
                    marker = json.loads(marker_path.read_text())
                    marker["staging_manifest_digest"] = canonical_json_digest(manifest)
                    recorder._write_json_atomic(marker_path, marker)
                elif failure == "symlink":
                    outside = Path(directory) / "outside"
                    outside.mkdir()
                    (outside / "sentinel").write_text("keep")
                    shutil.rmtree(staging)
                    staging.symlink_to(outside, target_is_directory=True)
                else:
                    outside = Path(directory) / "outside-meta"
                    meta = recorder.args.root / "meta"
                    meta.rename(outside)
                    (outside / "sentinel").write_text("keep")
                    meta.symlink_to(outside, target_is_directory=True)
                recorder._release_transaction_lock()

                result = recover_orphaned_transaction(recorder.args.root, recorder.args.run_root)
                expected = {
                    "committing": "RECOVERY_COMMIT_UNCERTAIN",
                    "snapshot": "RECOVERY_SNAPSHOT_CHANGED",
                    "outside": "RECOVERY_MANIFEST",
                    "symlink": "RECOVERY_SYMLINK",
                    "meta_symlink": "RECOVERY_SYMLINK",
                }[failure]
                self.assertEqual(result["reason_code"], expected)
                self.assertTrue((recorder.args.root / "meta" / "quarantine.json").exists())
                self.assertTrue(staging.exists())
                if failure in ("outside", "symlink", "meta_symlink"):
                    self.assertEqual((outside / "sentinel").read_text(), "keep")

    def test_dataset_lock_is_released_by_sigkill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "dataset"
            script = """
from data_factory_recovery import DatasetTransactionLock
import sys, time
lock = DatasetTransactionLock(sys.argv[1])
lock.acquire()
print('LOCKED', flush=True)
time.sleep(60)
"""
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(root)],
                cwd=Path(__file__).resolve().parents[1] / "tools",
                text=True,
                stdout=subprocess.PIPE,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "LOCKED")
                with self.assertRaises(RecoveryError) as caught:
                    DatasetTransactionLock(root).acquire()
                self.assertEqual(caught.exception.code, "DATASET_TRANSACTION_BUSY")
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
                lock = DatasetTransactionLock(root)
                lock.acquire()
                lock.release()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                if process.stdout is not None:
                    process.stdout.close()

    def test_final_commit_event_fault_quarantines(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode(self.transaction(directory))
            recorder.frames = 1
            recorder._quality_summary = lambda: ({"episode_index": 7, "effective_fps": 30.0, "image_quality_warnings": []}, [])
            append_event = recorder._append_event
            recorder._append_event = lambda code: (_ for _ in ()).throw(OSError("final event failed")) if code == "COMMITTED" else append_event(code)
            recorder.freeze_episode()
            self.assertEqual(recorder.commit_episode()["state"], recorder.QUARANTINED_COMMIT)

    def test_legacy_stop_noops_after_terminal_states(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            for state in (recorder.IDLE, recorder.COMMITTED, recorder.ABORTED):
                recorder.episode_state = state
                self.assertTrue(recorder.stop_episode())
            recorder.episode_state = recorder.QUARANTINED_COMMIT
            self.assertFalse(recorder.stop_episode())

    def test_close_aborts_frozen_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode()
            recorder.freeze_episode()
            recorder.sampler_thread = SimpleNamespace(join=lambda *_, **__: None)
            recorder.writer_thread = SimpleNamespace(join=lambda *_, **__: None)
            recorder.close()
            self.assertEqual(recorder.episode_state, recorder.ABORTED)
            self.assertEqual(recorder.dataset.clears, 1)

    def test_close_settles_freezing_before_abort(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = self.make_recorder(directory)
            recorder.begin_episode(self.transaction(directory))
            recorder.recording = False
            recorder.episode_state = recorder.FREEZING
            recorder.sampler_thread = SimpleNamespace(join=lambda *_, **__: None)
            recorder.writer_thread = SimpleNamespace(join=lambda *_, **__: None)
            recorder.close()
            self.assertEqual(recorder.episode_state, recorder.ABORTED)
            self.assertEqual(recorder.dataset.clears, 1)


if __name__ == "__main__":
    unittest.main()
