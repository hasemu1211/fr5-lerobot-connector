from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import cv2
import numpy as np

from tools.curator.contracts import (
    CuratorError,
    canonical_digest,
    file_sha256,
    tree_identity,
    tree_snapshot,
    write_json_atomic,
)
from tools.curator.derive import _run_existing_validator, _validate_source_contract, derive_dataset
from tools.curator.verify import (
    _accumulate_metrics,
    _image_metrics,
    _metric_accumulator,
    _require_local_file,
    _summarize_metrics,
    create_review_bundle,
    export_reference,
    open_source_dataset,
    verify_review_bundle,
)
from tools.fr5_dataset_schema import dataset_features


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _shape(label: str, points: list[list[float]], shape_type: str) -> dict:
    return {
        "label": label, "points": points, "group_id": None, "description": "",
        "shape_type": shape_type, "flags": {}, "mask": None,
    }


class DeriveIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from lerobot.configs.video import RGBEncoderConfig
            from lerobot.datasets.lerobot_dataset import LeRobotDataset
        except ImportError as exc:
            raise unittest.SkipTest(f"LeRobot environment unavailable: {exc}") from exc
        cls.RGBEncoderConfig = RGBEncoderConfig
        cls.LeRobotDataset = LeRobotDataset

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        writer = self.LeRobotDataset.create(
            repo_id="local/source",
            fps=30,
            root=self.source,
            robot_type="fr5_ros2",
            features=dataset_features(
                fps=30, height=480, width=640, cameras=("up", "wrist"), use_videos=True,
            ),
            use_videos=True,
            image_writer_threads=2,
            rgb_encoder=self.RGBEncoderConfig(vcodec="h264", preset="ultrafast", crf=23),
        )
        yy, xx = np.mgrid[:480, :640]
        for index in range(4):
            if index == 2:
                writer.save_episode(parallel_encoding=False)
            up = np.stack(
                ((xx // 4 + index * 2) % 256, (yy // 3 + index) % 256, ((xx + yy) // 6 + index) % 256),
                axis=-1,
            ).astype(np.uint8)
            wrist = np.stack(
                ((xx // 5 + 20) % 256, (yy // 4 + index * 2) % 256, ((2 * xx + yy) // 10) % 256),
                axis=-1,
            ).astype(np.uint8)
            state = np.asarray([0.1 * joint + index * 0.001 for joint in range(6)] + [0.02 + index * 0.001], dtype=np.float32)
            action = np.asarray([0.11 * joint + index * 0.002 for joint in range(6)] + [0.021 + index * 0.001], dtype=np.float32)
            writer.add_frame({
                "observation.state": state,
                "action": action,
                "observation.images.up": up,
                "observation.images.wrist": wrist,
                "task": (
                    "move the cube from red to blue"
                    if index < 2
                    else "move the cube from blue to red"
                ),
            })
        writer.save_episode(parallel_encoding=False)
        writer.finalize()
        self._write_source_evidence()
        write_json_atomic(
            self.source / "meta" / "training_approved.json",
            {"schema_version": "synthetic.training_approval.v1", "training_approved": True},
        )

        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.reference = self.assets / "reference.png"
        self.reference_export = export_reference(
            self.source,
            self.reference,
            0,
            source_repo_id="local/source",
        )
        self._write_profile_files()
        result = create_review_bundle(self.source, self.request_path, source_repo_id="local/source")
        self.profile_digest = result["profile_digest"]
        self.review_digest = result["review_bundle_digest"]
        _request, self.resolved_profile, self.review_manifest = verify_review_bundle(
            self.request_path,
        )
        self.mocked_approval = {
            "approved_by": "TEST_ONLY_MOCKED_AUTHORITY",
            "approved_at": "2026-09-02T00:00:00Z",
            "provenance": "TEST_ONLY_MOCKED_AUTHORITY",
            "approval_digest": "sha256:" + "a" * 64,
        }
        write_json_atomic(
            self.approval_path,
            {"scope": "TEST_ONLY_MOCKED_AUTHORITY", "training_authorized": False},
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _write_source_evidence(self):
        qualities = []
        for episode in range(2):
            qualities.append({
                "episode_index": episode,
                "frames": 2,
                "target_fps": 30,
                "effective_fps": 30.0,
                "interval_max_ms": 1000 / 30,
                "writer_queue_drops": 0,
                "alignment_failures": 0,
                "alignment_failure_sources": {
                    "state": 0, "arm_action": 0, "gripper_action": 0, "transport": 0,
                    "image.up": 0, "image.wrist": 0,
                },
                "action_age_max_ms": 1.0,
                "state_age_max_ms": 1.0,
                "camera_time_offsets_ms": {"up": 0.0, "wrist": 0.0},
                "cameras": {
                    camera: {
                        "age_max_ms": 0.0, "transport_age_max_ms": 1.0, "source_fps": 30.0,
                        "repeat_ratio": 0.0, "source_gap_max_ms": 1000 / 30,
                        "color_delta_mean": 0.0, "brightness_mean": 0.0,
                        "clipping_mean": 1.0, "sharpness_median": 0.0,
                    }
                    for camera in ("up", "wrist")
                },
                "image_quality_warnings": [
                    "source pixel warning intentionally stale for curator test"
                ],
            })
        (self.source / "meta" / "recording_quality.jsonl").write_text(
            "".join(json.dumps(quality, sort_keys=True) + "\n" for quality in qualities),
            encoding="utf-8",
        )
        provenance = self.source / "meta" / "source_provenance"
        provenance.mkdir()
        for episode in range(2):
            rows = []
            for index in range(2):
                target = 100.0 + episode * 10 + index / 30
                rows.append({
                    "frame_index": index,
                    "enqueue_attempt_index": index,
                    "target_ros_s": target,
                    "joint_bracket_ros_s": [target - 0.001, target + 0.001],
                    "arm_action_bracket_ros_s": [target - 0.001, target + 0.001],
                    "gripper_action_ros_s": target - 0.001,
                    "image_raw_ros_s": {"up": target, "wrist": target},
                    "image_corrected_ros_s": {"up": target, "wrist": target},
                    "image_received_ros_s": {"up": target + 0.001, "wrist": target + 0.001},
                })
            (provenance / f"episode-{episode:06d}.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )

    def _write_profile_files(self):
        layout = {
            "schema_version": "a4_workspace_region_layout.v1",
            "layout_id": "synthetic-red-blue-r001",
            "page_mm": {"width": 100.0, "height": 50.0},
            "origin_xy_mm": [0.0, 0.0],
            "workspace_regions": [
                {
                    "place_id": "PLACE_A", "region_id": "RED", "display_name": "RED", "color": "#cc2222",
                    "polygon_local_xy_mm": [[10, 10], [90, 10], [90, 40], [10, 40]],
                },
                {
                    "place_id": "PLACE_B", "region_id": "BLUE", "display_name": "BLUE", "color": "#2244cc",
                    "polygon_local_xy_mm": [[10, 10], [90, 10], [90, 40], [10, 40]],
                },
            ],
        }
        layout["layout_digest"] = canonical_digest(layout)
        layout_path = self.assets / "layout.json"
        _write(layout_path, layout)
        binding = {
            "schema_version": "data_factory.workspace_region_binding.v1",
            "layout_id": layout["layout_id"],
            "layout_digest": layout["layout_digest"],
            "physical_binding_status": "PREPARED_NOT_VERIFIED",
            "bindings": [
                {"place_id": "PLACE_A", "frame_id": "place-a-r001", "region_id": "RED"},
                {"place_id": "PLACE_B", "frame_id": "place-b-r001", "region_id": "BLUE"},
            ],
            "verified_at": None,
            "verified_by": None,
            "evidence_digest": None,
        }
        binding["binding_digest"] = canonical_digest(binding)
        binding_path = self.assets / "binding.json"
        _write(binding_path, binding)
        shapes = [
            _shape("TABLE_WORK_SURFACE", [[20, 20], [620, 20], [620, 460], [20, 460]], "polygon"),
            _shape("visual_motion_support", [[2, 200], [30, 195], [30, 270], [2, 265]], "polygon"),
        ]
        corners = {
            "PLACE_A": {"TL": [60, 80], "TR": [280, 60], "BR": [300, 300], "BL": [50, 320]},
            "PLACE_B": {"TL": [340, 70], "TR": [580, 90], "BR": [590, 330], "BL": [330, 310]},
        }
        for place, values in corners.items():
            for name, point in values.items():
                shapes.append(_shape(f"{place}_{name}", [point], "point"))
        annotation_path = self.assets / "reference.json"
        _write(annotation_path, {
            "version": "7.0.4", "flags": {}, "shapes": shapes,
            "imagePath": self.reference.name, "imageData": None,
            "imageHeight": 480, "imageWidth": 640,
        })
        self.approval_path = self.assets / "approval.json"
        request = {
            "schema_version": "curator.up_view_profile_request.v1",
            "profile_id": "synthetic-up-view-r001",
            "camera_key": "observation.images.up",
            "width": 640,
            "height": 480,
            "collection_camera_profile_digest": "sha256:" + "c" * 64,
            "layout_manifest": layout_path.name,
            "layout_manifest_digest": layout["layout_digest"],
            "physical_region_binding": binding_path.name,
            "physical_region_binding_digest": binding["binding_digest"],
            "labelme_annotation": annotation_path.name,
            "labelme_version": "7.0.4",
            "reference_image": self.reference.name,
            "reference_image_sha256": file_sha256(self.reference),
            "reference_frame_index": 0,
            "background_plate_frame_indices": [0],
            "dilation_margin_px": 2,
            "review_bundle": "review",
            "approval_artifact": self.approval_path.name,
        }
        self.request_path = self.assets / "request.json"
        _write(self.request_path, request)

    def test_official_roundtrip_atomic_publish_and_no_authority_inheritance(self):
        before = tree_snapshot(self.source)
        output = self.root / "derived"
        stale_lock = self.root / ".derived.curator-publish.lock"
        stale_lock.write_text("obsolete lock must not block renameat2", encoding="utf-8")
        with mock.patch(
            "tools.curator.derive.verify_approval",
            return_value=(self.mocked_approval, self.resolved_profile, self.review_manifest),
        ):
            receipt = derive_dataset(
                self.source,
                output,
                self.request_path,
                self.approval_path,
                run_dir=self.root / "runs" / "run-1",
                run_id="run-1",
                source_repo_id="local/source",
                output_repo_id="local/derived",
            )
        self.assertEqual(tree_snapshot(self.source), before)
        self.assertEqual(self.reference_export["reference_image_sha256"], file_sha256(self.reference))
        self.assertIs(self.reference_export["training_authority"], False)
        self.assertTrue(output.is_dir())
        self.assertTrue(stale_lock.is_file())
        self.assertFalse((output / "meta" / "training_approved.json").exists())
        self.assertFalse((output / "meta" / "quarantine.json").exists())
        quality_rows = [
            json.loads(line)
            for line in (output / "meta" / "recording_quality.jsonl").read_text().splitlines()
        ]
        self.assertEqual(len(quality_rows), 2)
        for quality in quality_rows:
            self.assertEqual(quality["curator_lineage"]["derived_pixel_metrics"], "RECOMPUTED")
            self.assertEqual(quality["curator_lineage"]["source_timing_evidence"], "PRESERVED")
            self.assertNotIn(
                "source pixel warning intentionally stale for curator test",
                quality["image_quality_warnings"],
            )
        derived = open_source_dataset(output, "local/derived")
        self.assertEqual(derived.meta.total_episodes, 2)
        self.assertEqual(
            [derived[index]["task"] for index in range(len(derived))],
            [
                "move the cube from red to blue",
                "move the cube from red to blue",
                "move the cube from blue to red",
                "move the cube from blue to red",
            ],
        )
        self.assertEqual(receipt["verification"]["status"], "PASS")
        self.assertEqual(len(receipt["verification"]["episode_mapping"]), 2)
        self.assertEqual(receipt["verification"]["video_codec"]["expected"], "h264")
        self.assertEqual(receipt["existing_validator"]["status"], "PASS")
        self.assertEqual(receipt["recording_quality_lineage"]["derived_pixel_metrics"], "RECOMPUTED")
        self.assertEqual(
            {key: receipt["encoder"][key] for key in ("vcodec", "preset", "crf")},
            {"vcodec": "h264", "preset": "ultrafast", "crf": 23},
        )
        self.assertIs(receipt["encoder"]["parallel_cameras"], True)
        self.assertGreater(receipt["encoder"]["image_writer_threads"], 0)
        self.assertGreater(receipt["encoder"]["encoder_threads_per_camera"], 0)
        performance = receipt["performance_observation"]
        self.assertEqual(
            set(performance["stage_seconds"]),
            {
                "preflight",
                "materialization",
                "post_write_verification",
                "quality_and_existing_validator",
                "source_and_authority_revalidation",
                "durable_publication",
                "output_identity",
            },
        )
        self.assertTrue(all(value >= 0 for value in performance["stage_seconds"].values()))
        self.assertGreater(performance["materialization_frames_per_second"], 0)
        self.assertGreater(performance["end_to_end_frames_per_second"], 0)
        self.assertIs(performance["authoritative_threshold"], False)
        self.assertEqual(
            receipt["publication"],
            {
                "state": "COMMITTED_DURABLE",
                "rename_noreplace": True,
                "tree_fsync": True,
                "parent_fsync": True,
            },
        )
        self.assertIs(receipt["training_authority"], False)
        self.assertIs(receipt["approval_inherited"], False)
        self.assertIs(receipt["quarantine_inherited"], False)
        self.assertTrue((self.root / "runs" / "run-1" / "receipt.json").is_file())

    def test_post_write_fault_and_quarantined_source_never_publish(self):
        source_approval = self.source / "meta" / "training_approved.json"
        write_json_atomic(
            source_approval,
            {"schema_version": "synthetic.training_approval.v1", "training_approved": False},
        )
        mismatch_output = self.root / "mismatch-derived"
        with mock.patch(
            "tools.curator.derive.verify_approval",
            return_value=(self.mocked_approval, self.resolved_profile, self.review_manifest),
        ):
            with self.assertRaisesRegex(CuratorError, "SOURCE_REVIEW_MISMATCH"):
                derive_dataset(
                    self.source,
                    mismatch_output,
                    self.request_path,
                    self.approval_path,
                    run_dir=self.root / "runs" / "run-mismatch",
                    run_id="run-mismatch",
                    source_repo_id="local/source",
                    output_repo_id="local/mismatch-derived",
                )
        self.assertFalse(mismatch_output.exists())
        self.assertFalse((self.root / "runs" / "run-mismatch").exists())
        write_json_atomic(
            source_approval,
            {"schema_version": "synthetic.training_approval.v1", "training_approved": True},
        )

        output = self.root / "fault-derived"
        with (
            mock.patch(
                "tools.curator.derive.verify_approval",
                return_value=(self.mocked_approval, self.resolved_profile, self.review_manifest),
            ),
            mock.patch(
                "tools.curator.derive.verify_derived_dataset",
                side_effect=CuratorError("INJECTED_POST_WRITE_FAULT"),
            ),
        ):
            with self.assertRaisesRegex(CuratorError, "INJECTED_POST_WRITE_FAULT"):
                derive_dataset(
                    self.source,
                    output,
                    self.request_path,
                    self.approval_path,
                    run_dir=self.root / "runs" / "run-fault",
                    run_id="run-fault",
                    source_repo_id="local/source",
                    output_repo_id="local/fault-derived",
                )
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".fault-derived*.curator-*")), [])
        failure = json.loads((self.root / "runs" / "run-fault" / "failure.json").read_text())
        self.assertEqual(failure["cleanup_state"], "REMOVED")
        self.assertEqual(failure["writer_shutdown_state"], "FINALIZED_BEFORE_FAILURE")

        write_json_atomic(self.source / "meta" / "quarantine.json", {"reason": "synthetic"})
        with mock.patch(
            "tools.curator.derive.verify_approval",
            return_value=(self.mocked_approval, self.resolved_profile, self.review_manifest),
        ):
            with self.assertRaisesRegex(CuratorError, "SOURCE_QUARANTINED"):
                derive_dataset(
                    self.source,
                    self.root / "quarantine-derived",
                    self.request_path,
                    self.approval_path,
                    run_dir=self.root / "runs" / "run-quarantine",
                    run_id="run-quarantine",
                    source_repo_id="local/source",
                    output_repo_id="local/quarantine-derived",
                )
        self.assertFalse((self.root / "quarantine-derived").exists())

    def test_asset_tamper_during_source_identity_never_publishes(self):
        import tools.curator.derive as derive_module

        original = derive_module.tree_identity
        tampered = False

        def tree_identity_with_tamper(root):
            nonlocal tampered
            result = original(root)
            if Path(root) == self.source and not tampered:
                mask_path = self.assets / "review" / "keep_mask.png"
                payload = bytearray(mask_path.read_bytes())
                payload[-1] ^= 1
                mask_path.write_bytes(payload)
                tampered = True
            return result

        output = self.root / "tampered-derived"
        with (
            mock.patch(
                "tools.curator.derive.verify_approval",
                return_value=(self.mocked_approval, self.resolved_profile, self.review_manifest),
            ),
            mock.patch("tools.curator.derive.tree_identity", side_effect=tree_identity_with_tamper),
        ):
            with self.assertRaisesRegex(CuratorError, "BUNDLE_ASSET_DIGEST"):
                derive_dataset(
                    self.source,
                    output,
                    self.request_path,
                    self.approval_path,
                    run_dir=self.root / "runs" / "run-tamper",
                    run_id="run-tamper",
                    source_repo_id="local/source",
                    output_repo_id="local/tampered-derived",
                )
        self.assertTrue(tampered)
        self.assertFalse(output.exists())

    def test_missing_video_fails_locally_without_source_mutation_or_network_probe(self):
        import lerobot.datasets.dataset_metadata as metadata_module
        import lerobot.datasets.lerobot_dataset as dataset_module

        video = sorted((self.source / "videos").rglob("*.mp4"))[0]
        video.unlink()
        before_snapshot = tree_snapshot(self.source)
        before_identity = tree_identity(self.source)
        with (
            mock.patch.object(dataset_module, "snapshot_download") as data_download,
            mock.patch.object(metadata_module, "snapshot_download") as metadata_download,
        ):
            with self.assertRaisesRegex(CuratorError, "SOURCE_LOCAL_INCOMPLETE"):
                open_source_dataset(self.source, "local/source")
        data_download.assert_not_called()
        metadata_download.assert_not_called()
        self.assertEqual(tree_snapshot(self.source), before_snapshot)
        self.assertEqual(tree_identity(self.source), before_identity)

    def test_same_size_preserved_mtime_source_change_never_publishes(self):
        import tools.curator.derive as derive_module

        original_validator = derive_module._run_existing_validator
        target = self.source / "meta" / "recording_quality.jsonl"

        def mutate_after_validation(root, repo_id):
            result = original_validator(root, repo_id)
            details = target.stat()
            payload = target.read_bytes()
            changed = payload.replace(b'"target_fps": 30', b'"target_fps": 31', 1)
            self.assertEqual(len(changed), len(payload))
            self.assertNotEqual(changed, payload)
            target.write_bytes(changed)
            os.utime(target, ns=(details.st_atime_ns, details.st_mtime_ns))
            return result

        output = self.root / "source-mutated-derived"
        run = self.root / "runs" / "run-source-mutated"
        with (
            mock.patch(
                "tools.curator.derive.verify_approval",
                return_value=(self.mocked_approval, self.resolved_profile, self.review_manifest),
            ),
            mock.patch(
                "tools.curator.derive._run_existing_validator",
                side_effect=mutate_after_validation,
            ),
        ):
            with self.assertRaisesRegex(CuratorError, "SOURCE_CHANGED_DURING_DERIVE"):
                derive_dataset(
                    self.source,
                    output,
                    self.request_path,
                    self.approval_path,
                    run_dir=run,
                    run_id="run-source-mutated",
                    source_repo_id="local/source",
                    output_repo_id="local/source-mutated-derived",
                )
        self.assertFalse(output.exists())
        failure = json.loads((run / "failure.json").read_text())
        self.assertEqual(failure["cleanup_state"], "REMOVED")

    def test_tree_fsync_fault_never_commits_output(self):
        output = self.root / "tree-fsync-derived"
        run = self.root / "runs" / "run-tree-fsync"
        with (
            mock.patch(
                "tools.curator.derive.verify_approval",
                return_value=(self.mocked_approval, self.resolved_profile, self.review_manifest),
            ),
            mock.patch(
                "tools.curator.derive._fsync_tree",
                side_effect=CuratorError("DERIVED_TREE_FSYNC", "injected"),
            ),
        ):
            with self.assertRaisesRegex(CuratorError, "DERIVED_TREE_FSYNC"):
                derive_dataset(
                    self.source,
                    output,
                    self.request_path,
                    self.approval_path,
                    run_dir=run,
                    run_id="run-tree-fsync",
                    source_repo_id="local/source",
                    output_repo_id="local/tree-fsync-derived",
                )
        self.assertFalse(output.exists())
        failure = json.loads((run / "failure.json").read_text())
        self.assertEqual(failure["publication_state"], "UNPUBLISHED")
        self.assertEqual(failure["cleanup_state"], "REMOVED")

    def test_committed_output_survives_parent_fsync_and_receipt_faults(self):
        import tools.curator.derive as derive_module

        original_fsync_directory = derive_module._fsync_directory
        cases = ("parent-fsync", "receipt")
        for case in cases:
            with self.subTest(case=case):
                output = self.root / f"{case}-derived"
                run = self.root / "runs" / f"run-{case}"
                patches = [
                    mock.patch(
                        "tools.curator.derive.verify_approval",
                        return_value=(self.mocked_approval, self.resolved_profile, self.review_manifest),
                    ),
                ]
                expected_code = "OUTPUT_COMMITTED_PARENT_FSYNC_FAILED"
                expected_state = "COMMITTED_PARENT_FSYNC_FAILED"
                if case == "parent-fsync":
                    def fail_output_parent(path):
                        if Path(path) == output.parent:
                            raise OSError("injected parent fsync fault")
                        return original_fsync_directory(path)

                    patches.append(mock.patch(
                        "tools.curator.derive._fsync_directory",
                        side_effect=fail_output_parent,
                    ))
                else:
                    expected_code = "COMMITTED_RECEIPT_FAILED"
                    expected_state = "COMMITTED_RECEIPT_FAILED"

                    def receipt_fault(path, value):
                        if Path(path).name == "receipt.json":
                            raise OSError("injected receipt fault")
                        return write_json_atomic(path, value)

                    patches.append(mock.patch(
                        "tools.curator.derive.write_json_atomic",
                        side_effect=receipt_fault,
                    ))
                with ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    with self.assertRaisesRegex(CuratorError, expected_code):
                        derive_dataset(
                            self.source,
                            output,
                            self.request_path,
                            self.approval_path,
                            run_dir=run,
                            run_id=f"run-{case}",
                            source_repo_id="local/source",
                            output_repo_id=f"local/{case}-derived",
                        )
                self.assertTrue(output.is_dir())
                failure = json.loads((run / "failure.json").read_text())
                self.assertIs(failure["output_committed"], True)
                self.assertEqual(failure["output"], str(output))
                self.assertEqual(failure["publication_state"], expected_state)
                self.assertEqual(failure["cleanup_state"], "NOT_APPLICABLE_OUTPUT_COMMITTED")
                self.assertEqual(failure["writer_shutdown_state"], "FINALIZED_BEFORE_FAILURE")

    def test_writer_add_save_and_finalize_faults_shutdown_before_cleanup(self):
        original_finalize = self.LeRobotDataset.finalize
        cases = ("add", "save", "finalize")
        for case in cases:
            with self.subTest(case=case):
                output = self.root / f"{case}-derived"
                temporary = self.root / f".{case}-derived.run-{case}.curator-tmp"
                observed: list[bool] = []
                finalize_calls = 0

                def finalize(writer):
                    nonlocal finalize_calls
                    finalize_calls += 1
                    observed.append(temporary.is_dir())
                    if case == "finalize" and finalize_calls == 1:
                        raise RuntimeError("injected finalize fault")
                    return original_finalize(writer)

                patches = [
                    mock.patch(
                        "tools.curator.derive.verify_approval",
                        return_value=(self.mocked_approval, self.resolved_profile, self.review_manifest),
                    ),
                    mock.patch.object(self.LeRobotDataset, "finalize", new=finalize),
                ]
                if case == "add":
                    patches.append(mock.patch.object(
                        self.LeRobotDataset,
                        "add_frame",
                        new=lambda _writer, _frame: (_ for _ in ()).throw(
                            RuntimeError("injected add fault")
                        ),
                    ))
                if case == "save":
                    patches.append(mock.patch.object(
                        self.LeRobotDataset,
                        "save_episode",
                        new=lambda _writer, parallel_encoding=False: (_ for _ in ()).throw(
                            RuntimeError("injected save fault")
                        ),
                    ))
                with ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    with self.assertRaises(RuntimeError):
                        derive_dataset(
                            self.source,
                            output,
                            self.request_path,
                            self.approval_path,
                            run_dir=self.root / "runs" / f"run-{case}",
                            run_id=f"run-{case}",
                            source_repo_id="local/source",
                            output_repo_id=f"local/{case}-derived",
                        )
                self.assertTrue(observed)
                self.assertTrue(all(observed))
                self.assertFalse(temporary.exists())
                self.assertFalse(
                    (self.root / f".{case}-derived.run-{case}.curator-owner.json").exists()
                )
                failure = json.loads(
                    (self.root / "runs" / f"run-{case}" / "failure.json").read_text()
                )
                self.assertEqual(failure["cleanup_state"], "REMOVED")
                self.assertEqual(failure["writer_shutdown_state"], "FINALIZED_DURING_FAILURE")

    def test_cleanup_leaks_on_temporary_path_substitution(self):
        output = self.root / "substituted-derived"
        temporary = self.root / ".substituted-derived.run-sub.curator-tmp"
        moved = self.root / "captured-temp-moved"
        original_finalize = self.LeRobotDataset.finalize
        observed: list[bool] = []

        def substitute(_writer, _frame):
            temporary.rename(moved)
            temporary.mkdir()
            (temporary / "replacement-sentinel").write_text("do not delete", encoding="utf-8")
            raise RuntimeError("injected path substitution")

        def finalize(writer):
            observed.append(temporary.is_dir())
            return original_finalize(writer)

        with (
            mock.patch(
                "tools.curator.derive.verify_approval",
                return_value=(self.mocked_approval, self.resolved_profile, self.review_manifest),
            ),
            mock.patch.object(self.LeRobotDataset, "add_frame", new=substitute),
            mock.patch.object(self.LeRobotDataset, "finalize", new=finalize),
        ):
            with self.assertRaisesRegex(RuntimeError, "path substitution"):
                derive_dataset(
                    self.source,
                    output,
                    self.request_path,
                    self.approval_path,
                    run_dir=self.root / "runs" / "run-sub",
                    run_id="run-sub",
                    source_repo_id="local/source",
                    output_repo_id="local/substituted-derived",
                )
        self.assertEqual(observed, [True])
        self.assertTrue((temporary / "replacement-sentinel").is_file())
        self.assertTrue(moved.is_dir())
        self.assertTrue((self.root / ".substituted-derived.run-sub.curator-owner.json").is_file())
        failure = json.loads((self.root / "runs" / "run-sub" / "failure.json").read_text())
        self.assertEqual(failure["cleanup_state"], "RETAINED_IDENTITY_AMBIGUOUS")

    def test_cleanup_leaks_on_owner_marker_path_substitution(self):
        output = self.root / "marker-substituted-derived"
        temporary = self.root / ".marker-substituted-derived.run-marker-sub.curator-tmp"
        marker = self.root / ".marker-substituted-derived.run-marker-sub.curator-owner.json"
        moved_marker = self.root / "captured-owner-marker-moved.json"
        original_finalize = self.LeRobotDataset.finalize

        def substitute(_writer, _frame):
            marker.rename(moved_marker)
            marker.write_text('{"replacement":"do not delete"}', encoding="utf-8")
            raise RuntimeError("injected marker path substitution")

        with (
            mock.patch(
                "tools.curator.derive.verify_approval",
                return_value=(self.mocked_approval, self.resolved_profile, self.review_manifest),
            ),
            mock.patch.object(self.LeRobotDataset, "add_frame", new=substitute),
            mock.patch.object(self.LeRobotDataset, "finalize", new=original_finalize),
        ):
            with self.assertRaisesRegex(RuntimeError, "marker path substitution"):
                derive_dataset(
                    self.source,
                    output,
                    self.request_path,
                    self.approval_path,
                    run_dir=self.root / "runs" / "run-marker-sub",
                    run_id="run-marker-sub",
                    source_repo_id="local/source",
                    output_repo_id="local/marker-substituted-derived",
                )
        self.assertTrue(temporary.is_dir())
        self.assertTrue(moved_marker.is_file())
        self.assertEqual(marker.read_text(encoding="utf-8"), '{"replacement":"do not delete"}')
        failure = json.loads(
            (self.root / "runs" / "run-marker-sub" / "failure.json").read_text()
        )
        self.assertEqual(failure["cleanup_state"], "RETAINED_IDENTITY_AMBIGUOUS")


class DeriveContractTest(unittest.TestCase):
    def test_compact_image_metric_accumulator_preserves_results(self):
        generator = np.random.default_rng(11)
        images = [
            generator.integers(0, 256, size=(8, 10, 3), dtype=np.uint8)
            for _ in range(5)
        ]
        expected = np.asarray([_image_metrics(image) for image in images])
        accumulator = _metric_accumulator()
        for image in images:
            _accumulate_metrics(accumulator, image)
        actual = _summarize_metrics(accumulator)
        self.assertAlmostEqual(actual["color_delta_mean"], float(expected[:, 0].mean()))
        self.assertAlmostEqual(actual["brightness_mean"], float(expected[:, 1].mean()))
        self.assertAlmostEqual(actual["clipping_mean"], float(expected[:, 2].mean()))
        self.assertAlmostEqual(actual["sharpness_median"], float(np.median(expected[:, 3])))
        self.assertEqual(len(accumulator["sharpness"]), len(images))

    def test_local_dataset_paths_cannot_escape_the_source_root(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "source"
            root.mkdir()
            (root / "inside.mp4").write_bytes(b"inside")
            outside = parent / "outside.mp4"
            outside.write_bytes(b"outside")
            _require_local_file(root, Path("inside.mp4"))
            for candidate in (Path("../outside.mp4"), outside):
                with self.subTest(candidate=candidate):
                    with self.assertRaisesRegex(CuratorError, "SOURCE_LOCAL_PATH_ESCAPE"):
                        _require_local_file(root, candidate)

    def test_source_and_existing_validator_are_pinned_to_30_hz(self):
        class Dataset:
            meta = SimpleNamespace(
                features=dataset_features(
                    fps=29,
                    height=480,
                    width=640,
                    cameras=("up", "wrist"),
                    use_videos=True,
                ),
                fps=29,
                robot_type="fr5_ros2",
                total_episodes=1,
            )

            def __len__(self):
                return 1

        with self.assertRaisesRegex(CuratorError, "SOURCE_DATASET_CONTRACT"):
            _validate_source_contract(Dataset(), {"width": 640, "height": 480})

        completed = SimpleNamespace(returncode=0, stdout="PASS\n", stderr="")
        with mock.patch("tools.curator.derive.subprocess.run", return_value=completed) as run:
            _run_existing_validator(Path("/tmp/derived"), "local/derived")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--expected-fps") + 1], "30")


if __name__ == "__main__":
    unittest.main()
