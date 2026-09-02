from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np

from tools.curator.contracts import CuratorError, canonical_digest, file_sha256, tree_snapshot, write_json_atomic
from tools.curator.derive import derive_dataset
from tools.curator.verify import create_review_bundle, export_reference
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
                "task": "move the cube from red to blue",
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
        approval = {
            "schema_version": "curator.human_task_view_approval.v1",
            "scope": "HUMAN_TASK_VIEW",
            "profile_id": "synthetic-up-view-r001",
            "profile_digest": self.profile_digest,
            "review_bundle_digest": self.review_digest,
            "approved_by": "operator-1",
            "approved_at": "2026-09-02T00:00:00Z",
            "provenance": "HUMAN_TASK_VIEW_APPROVED",
            "training_authorized": False,
        }
        approval["approval_digest"] = canonical_digest(approval)
        write_json_atomic(self.approval_path, approval)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_source_evidence(self):
        quality = {
            "episode_index": 0,
            "frames": 4,
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
                    "color_delta_mean": 20.0, "brightness_mean": 100.0,
                    "clipping_mean": 0.0, "sharpness_median": 100.0,
                }
                for camera in ("up", "wrist")
            },
        }
        (self.source / "meta" / "recording_quality.jsonl").write_text(
            json.dumps(quality, sort_keys=True) + "\n", encoding="utf-8",
        )
        provenance = self.source / "meta" / "source_provenance"
        provenance.mkdir()
        rows = []
        for index in range(4):
            target = 100.0 + index / 30
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
        (provenance / "episode-000000.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8",
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
            "physical_binding_status": "VERIFIED",
            "bindings": [
                {"place_id": "PLACE_A", "frame_id": "place-a-r001", "region_id": "RED"},
                {"place_id": "PLACE_B", "frame_id": "place-b-r001", "region_id": "BLUE"},
            ],
            "verified_at": "2026-09-02T00:00:00Z",
            "verified_by": "operator-1",
            "evidence_digest": "sha256:" + "e" * 64,
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
        self.assertFalse((output / "meta" / "training_approved.json").exists())
        self.assertFalse((output / "meta" / "quarantine.json").exists())
        quality = json.loads((output / "meta" / "recording_quality.jsonl").read_text().strip())
        self.assertEqual(quality["curator_lineage"]["derived_pixel_metrics"], "RECOMPUTED")
        self.assertEqual(quality["curator_lineage"]["source_timing_evidence"], "PRESERVED")
        self.assertEqual(receipt["verification"]["status"], "PASS")
        self.assertEqual(receipt["verification"]["video_codec"]["expected"], "h264")
        self.assertEqual(receipt["existing_validator"]["status"], "PASS")
        self.assertEqual(receipt["recording_quality_lineage"]["derived_pixel_metrics"], "RECOMPUTED")
        self.assertEqual(receipt["encoder"], {"vcodec": "h264", "preset": "ultrafast", "crf": 23})
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
        with mock.patch(
            "tools.curator.derive.verify_derived_dataset",
            side_effect=CuratorError("INJECTED_POST_WRITE_FAULT"),
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

        write_json_atomic(self.source / "meta" / "quarantine.json", {"reason": "synthetic"})
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


if __name__ == "__main__":
    unittest.main()
