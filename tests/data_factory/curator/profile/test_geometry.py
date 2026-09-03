from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tests.data_factory.curator.support import make_profile_fixture, write_json
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.core.identity import file_sha256
from tools.data_factory.curator.profile.geometry import (
    build_keep_mask,
    resolve_geometry,
)
from tools.data_factory.curator.profile.schema import load_view_profile


class GeometryTest(unittest.TestCase):
    def test_table_is_primary_and_ab_are_semantic_subregions(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = make_profile_fixture(Path(directory), width=200, height=120)
            spec = load_view_profile(fixture.profile_path)
            geometry, _layout, _binding = resolve_geometry(spec)
            mask = build_keep_mask(geometry, 200, 120, spec.value["dilation_margin_px"])
            self.assertTrue(mask[60, 100])
            self.assertFalse(mask[0, 100])
            self.assertNotEqual(
                geometry["semantic_subregions"]["PLACE_A"],
                geometry["semantic_subregions"]["PLACE_B"],
            )
            self.assertTrue(geometry["visual_motion_support"])
            self.assertTrue(geometry["grounding_context_support"])

    def test_flipped_page_corner_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = make_profile_fixture(Path(directory), width=200, height=120)
            annotation = json.loads(fixture.annotation_path.read_text(encoding="utf-8"))
            points = {item["label"]: item for item in annotation["shapes"]}
            points["PLACE_A_TR"]["points"], points["PLACE_A_BL"]["points"] = (
                points["PLACE_A_BL"]["points"],
                points["PLACE_A_TR"]["points"],
            )
            write_json(fixture.annotation_path, annotation)
            profile = json.loads(fixture.profile_path.read_text(encoding="utf-8"))
            profile["labelme_annotation_sha256"] = file_sha256(fixture.annotation_path)
            write_json(fixture.profile_path, profile)
            with self.assertRaisesRegex(CuratorError, "LABELME_CORNER_ORDER"):
                resolve_geometry(load_view_profile(fixture.profile_path))


if __name__ == "__main__":
    unittest.main()
