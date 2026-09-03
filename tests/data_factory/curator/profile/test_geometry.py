from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from tools.data_factory.curator.core.jsonio import CuratorError, canonical_digest, file_sha256
from tools.data_factory.curator.profile.geometry import build_keep_mask, load_profile_request, resolve_geometry
from tools.data_factory.curator.profile.transform import MAX_BACKGROUND_PLATE_FRAMES


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _shape(label: str, points: list[list[float]], shape_type: str) -> dict:
    return {
        "label": label,
        "points": points,
        "group_id": None,
        "description": "",
        "shape_type": shape_type,
        "flags": {},
        "mask": None,
    }


class GeometryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.reference = self.root / "reference.png"
        cv2.imwrite(str(self.reference), np.full((120, 200, 3), 127, dtype=np.uint8))
        self.layout = {
            "schema_version": "a4_workspace_region_layout.v1",
            "layout_id": "future-layout-r003",
            "page_mm": {"width": 100.0, "height": 50.0},
            "origin_xy_mm": [0.0, 0.0],
            "workspace_regions": [
                {
                    "place_id": "PLACE_A", "region_id": "RED", "display_name": "RED", "color": "#cc2222",
                    "polygon_local_xy_mm": [[10, 10], [40, 10], [40, 30], [10, 30]],
                },
                {
                    "place_id": "PLACE_B", "region_id": "BLUE", "display_name": "BLUE", "color": "#2244cc",
                    "polygon_local_xy_mm": [[20, 5], [80, 5], [70, 40], [20, 40]],
                },
            ],
        }
        self.layout["layout_digest"] = canonical_digest(self.layout)
        self.layout_path = self.root / "layout.json"
        _write(self.layout_path, self.layout)
        self.binding = {
            "schema_version": "data_factory.workspace_region_binding.v1",
            "layout_id": self.layout["layout_id"],
            "layout_digest": self.layout["layout_digest"],
            "physical_binding_status": "PREPARED_NOT_VERIFIED",
            "bindings": [
                {"place_id": "PLACE_A", "frame_id": "place-a-r003", "region_id": "RED"},
                {"place_id": "PLACE_B", "frame_id": "place-b-r003", "region_id": "BLUE"},
            ],
            "verified_at": None,
            "verified_by": None,
            "evidence_digest": None,
        }
        self.binding["binding_digest"] = canonical_digest(self.binding)
        self.binding_path = self.root / "binding.json"
        _write(self.binding_path, self.binding)
        shapes = [
            _shape("TABLE_WORK_SURFACE", [[10, 10], [190, 10], [190, 110], [10, 110]], "polygon"),
            _shape("visual_motion_support", [[2, 50], [18, 45], [18, 75], [2, 70]], "polygon"),
            _shape("grounding_context_support", [[182, 40], [197, 40], [197, 55], [182, 55]], "polygon"),
        ]
        corners = {
            "PLACE_A": {"TL": [20, 20], "TR": [100, 20], "BR": [100, 80], "BL": [20, 80]},
            "PLACE_B": {"TL": [105, 25], "TR": [185, 35], "BR": [180, 100], "BL": [105, 90]},
        }
        for place, values in corners.items():
            for name, point in values.items():
                shapes.append(_shape(f"{place}_{name}", [point], "point"))
        self.annotation = {
            "version": "7.0.4", "flags": {}, "shapes": shapes,
            "imagePath": self.reference.name, "imageData": None,
            "imageHeight": 120, "imageWidth": 200,
        }
        self.annotation_path = self.root / "reference.json"
        _write(self.annotation_path, self.annotation)
        repository = Path(__file__).resolve().parents[4]
        self.collection_profile = json.loads(
            (
                repository
                / "config/data_factory/collection_profiles/fr5-up-wrist-rgb-30hz-v1.json"
            ).read_text(encoding="utf-8")
        )
        self.collection_profile.update({
            "collection_profile_id": "synthetic-up-wrist-30hz-r001",
            "width": 200,
            "height": 120,
            "repo_id": "local/synthetic",
        })
        self.collection_profile_path = self.root / "collection-profile.json"
        _write(self.collection_profile_path, self.collection_profile)
        self.request = {
            "schema_version": "curator.up_view_profile_request.v2",
            "profile_id": "synthetic-profile-r001",
            "camera_key": "observation.images.up",
            "width": 200,
            "height": 120,
            "collection_camera_profile": self.collection_profile_path.name,
            "collection_camera_profile_digest": canonical_digest(self.collection_profile),
            "layout_manifest": self.layout_path.name,
            "layout_manifest_digest": self.layout["layout_digest"],
            "physical_region_binding": self.binding_path.name,
            "physical_region_binding_digest": self.binding["binding_digest"],
            "labelme_annotation": self.annotation_path.name,
            "labelme_version": "7.0.4",
            "reference_image": self.reference.name,
            "reference_image_sha256": file_sha256(self.reference),
            "reference_frame_index": 0,
            "background_plate_frame_indices": [0],
            "dilation_margin_px": 0,
            "review_bundle": "review",
            "approval_artifact": "approval.json",
        }
        self.request_path = self.root / "request.json"
        _write(self.request_path, self.request)

    def tearDown(self):
        self.temporary.cleanup()

    def test_table_is_primary_and_future_layout_projects_independently(self):
        request = load_profile_request(self.request_path)
        geometry, _layout, _binding = resolve_geometry(request)
        self.assertEqual(geometry["semantic_subregions"]["PLACE_A"][0], [28.0, 32.0])
        self.assertNotEqual(
            geometry["semantic_subregions"]["PLACE_A"],
            geometry["semantic_subregions"]["PLACE_B"],
        )
        mask = build_keep_mask(geometry, 200, 120, 0)
        self.assertTrue(mask[60, 60])  # table pixel outside both semantic polygons
        self.assertTrue(mask[50, 195])  # optional grounding support union
        self.assertTrue(mask[60, 4])  # visual motion support outside table
        self.assertFalse(mask[2, 100])

        changed = json.loads(json.dumps(self.layout))
        changed["workspace_regions"][0]["polygon_local_xy_mm"] = [[5, 5], [50, 5], [45, 35], [5, 35]]
        changed["layout_digest"] = canonical_digest({key: value for key, value in changed.items() if key != "layout_digest"})
        _write(self.layout_path, changed)
        self.request["layout_manifest_digest"] = changed["layout_digest"]
        self.binding["layout_digest"] = changed["layout_digest"]
        self.binding["binding_digest"] = canonical_digest({key: value for key, value in self.binding.items() if key != "binding_digest"})
        _write(self.binding_path, self.binding)
        self.request["physical_region_binding_digest"] = self.binding["binding_digest"]
        _write(self.request_path, self.request)
        projected, _layout, _binding = resolve_geometry(load_profile_request(self.request_path))
        self.assertEqual(projected["semantic_subregions"]["PLACE_A"][0], [24.0, 26.0])

    def test_invalid_corner_order_unknown_field_and_nonfinite_fail(self):
        broken = json.loads(json.dumps(self.annotation))
        by_label = {shape["label"]: shape for shape in broken["shapes"]}
        by_label["PLACE_A_TR"]["points"], by_label["PLACE_A_BL"]["points"] = (
            by_label["PLACE_A_BL"]["points"], by_label["PLACE_A_TR"]["points"],
        )
        _write(self.annotation_path, broken)
        with self.assertRaisesRegex(CuratorError, "LABELME_CORNER_ORDER"):
            resolve_geometry(load_profile_request(self.request_path))

        _write(self.annotation_path, self.annotation)
        self.request["unknown"] = True
        _write(self.request_path, self.request)
        with self.assertRaisesRegex(CuratorError, "PROFILE_FIELDS"):
            load_profile_request(self.request_path)

        del self.request["unknown"]
        self.request["labelme_version"] = "7.1.0"
        _write(self.request_path, self.request)
        with self.assertRaisesRegex(CuratorError, "PROFILE_LABELME_VERSION"):
            load_profile_request(self.request_path)

        self.request["labelme_version"] = "7.0.4"
        _write(self.request_path, self.request)
        text = self.annotation_path.read_text().replace("20]", "NaN]", 1)
        self.annotation_path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(CuratorError, "JSON_NONFINITE"):
            resolve_geometry(load_profile_request(self.request_path))

    def test_verified_binding_must_come_from_producer_registry(self):
        self.binding.update({
            "physical_binding_status": "VERIFIED",
            "verified_at": "2026-09-02T00:00:00Z",
            "verified_by": "operator-1",
            "evidence_digest": "sha256:" + "e" * 64,
        })
        self.binding["binding_digest"] = canonical_digest({
            key: value for key, value in self.binding.items() if key != "binding_digest"
        })
        _write(self.binding_path, self.binding)
        self.request["physical_region_binding_digest"] = self.binding["binding_digest"]
        _write(self.request_path, self.request)
        with self.assertRaisesRegex(CuratorError, "VERIFIED_BINDING_NOT_CANONICAL"):
            load_profile_request(self.request_path)

    def test_background_plate_frame_count_is_bounded_before_decode(self):
        self.request["background_plate_frame_indices"] = list(
            range(MAX_BACKGROUND_PLATE_FRAMES + 1)
        )
        _write(self.request_path, self.request)
        with self.assertRaisesRegex(CuratorError, "PROFILE_PLATE_INDICES"):
            load_profile_request(self.request_path)

    def test_collection_profile_digest_and_observable_contract_are_enforced(self):
        self.collection_profile["fps"] = 29
        _write(self.collection_profile_path, self.collection_profile)
        with self.assertRaisesRegex(CuratorError, "COLLECTION_PROFILE_DIGEST"):
            load_profile_request(self.request_path)

        self.request["collection_camera_profile_digest"] = canonical_digest(
            self.collection_profile
        )
        _write(self.request_path, self.request)
        with self.assertRaisesRegex(CuratorError, "COLLECTION_PROFILE_CONTRACT"):
            load_profile_request(self.request_path)

    def test_collection_profile_full_v2_contract_is_enforced(self):
        invalid_values = {
            "collection_profile_id": "bad/id",
            "quality_contract_digest": "not-a-digest",
            "camera_serials": {"up": "UP", "side": "SIDE"},
            "camera_topics": {"up": "relative", "wrist": "/camera/wrist"},
            "writer_queue_size": True,
            "encoder_threads": -1,
            "portability_status": "UNKNOWN",
        }
        for field, invalid in invalid_values.items():
            with self.subTest(field=field):
                profile = {**self.collection_profile, field: invalid}
                _write(self.collection_profile_path, profile)
                self.request["collection_camera_profile_digest"] = canonical_digest(profile)
                _write(self.request_path, self.request)
                with self.assertRaisesRegex(CuratorError, "COLLECTION_PROFILE_CONTRACT"):
                    load_profile_request(self.request_path)

    def test_semantic_subregion_outside_table_is_rejected(self):
        request = load_profile_request(self.request_path)
        geometry, _layout, _binding = resolve_geometry(request)
        geometry["table_work_surface"] = [
            [50, 40], [190, 40], [190, 110], [50, 110],
        ]
        with self.assertRaisesRegex(CuratorError, "SEMANTIC_OUTSIDE_TABLE"):
            build_keep_mask(geometry, 200, 120, 0)


if __name__ == "__main__":
    unittest.main()
