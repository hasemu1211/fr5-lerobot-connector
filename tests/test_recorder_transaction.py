#!/usr/bin/env python3
"""Deterministic lifecycle tests; no ROS node or hardware is started."""

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
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from fr5_lerobot_recorder import FR5LeRobotRecorder
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

    def save_episode(self):
        self.saves += 1
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
    def make_recorder(self, directory, save_error=None, clear_error=None, finalize_error=None):
        recorder = FR5LeRobotRecorder.__new__(FR5LeRobotRecorder)
        recorder.args = SimpleNamespace(
            root=Path(directory) / "dataset", run_root=Path(directory) / "runs", fps=30,
            writer_queue_size=8, streaming_encoding=False, no_videos=False,
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
            result = recorder.begin_episode(self.transaction(directory))
            self.assertTrue(result["ok"])
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
            self.assertEqual(recorder.dataset.clears, 0)

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
            with self.assertRaisesRegex(ValueError, "already exists"):
                resumed.begin_episode(context)
            nested = self.make_recorder(directory)
            nested.args.run_root = nested.args.root / "runs"
            with self.assertRaisesRegex(ValueError, "must be separate"):
                nested.begin_episode(self.transaction(directory))

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

            def save():
                recorder.dataset.saves += 1
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

            def save_then_block_quality_sidecar():
                recorder.dataset.saves += 1
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
