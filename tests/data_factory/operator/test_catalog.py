import unittest
from pathlib import Path

from tools.data_factory.operator.catalog import (
    load_operator_catalog,
    validate_operator_selection,
)
from tools.fr5_data_factory import ContractError, canonical_digest


ROOT = Path(__file__).resolve().parents[3]


class OperatorCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.device_id = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
        cls.catalog = load_operator_catalog(ROOT, device_ids=[cls.device_id])

    def test_repository_catalog_is_byte_stable_and_exposes_product_axes(self):
        second = load_operator_catalog(ROOT, device_ids=[self.device_id])
        self.assertEqual(self.catalog, second)
        self.assertEqual(
            self.catalog["catalog_digest"],
            canonical_digest({
                key: value for key, value in self.catalog.items()
                if key != "catalog_digest"
            }),
        )
        self.assertEqual(
            set(self.catalog["axes"]),
            {
                "data_mode", "workspace", "frame", "task", "object", "grasp",
                "cell", "start_pose", "motion", "variant", "policy",
                "camera_profile", "camera_device",
            },
        )
        ids = {
            axis: {option["id"] for option in options}
            for axis, options in self.catalog["axes"].items()
        }
        self.assertIn("TEST_COLLECTION", ids["data_mode"])
        self.assertIn("GENERAL_COLLECTION", ids["data_mode"])
        self.assertIn("pickup_e2e", ids["task"])
        self.assertIn("pick_place", ids["task"])
        self.assertIn("wood-cube-25mm-r001", ids["object"])
        self.assertIn("wood-cube-25mm-top-center-r001", ids["grasp"])
        self.assertIn("DIRECT", ids["variant"])
        self.assertIn("TWO_STAGE_ALIGN", ids["variant"])
        self.assertIn(self.device_id, ids["camera_device"])
        self.assertTrue(all(
            set(item["source_digests"]) == set(item["sources"])
            and all(
                value.startswith("sha256:")
                for value in item["source_digests"].values()
            )
            and item["combination_digest"] == canonical_digest({
                key: value for key, value in item.items()
                if key != "combination_digest"
            })
            for item in self.catalog["combinations"]
        ))

        second_device = "usb-Generic_USB2.0_PC_CAMERA_2-video-index0"
        with_two = load_operator_catalog(
            ROOT, device_ids=[self.device_id, second_device],
        )
        bound_devices = {
            item["camera_device_id"] for item in with_two["combinations"]
            if item["camera_profile_id"] == "fr5-up-rgb-30hz-v1"
        }
        self.assertEqual(bound_devices, {self.device_id, second_device})

    def test_selection_uses_one_compatible_combination_and_mode_boundary(self):
        executable = next(
            item for item in self.catalog["combinations"]
            if item["execution"]["TEST_COLLECTION"]["executable"]
        )
        selection = {
            "schema_version": "data_factory.operator_selection.v1",
            "combination_digest": executable["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            "workspace_id": executable["workspace_id"],
            "frame_id": executable["frame_id"],
            "task_id": executable["task_id"],
            "object_id": executable["object_id"],
            "grasp_id": executable["grasp_id"],
            "cell_id": executable["cell_id"],
            "start_pose_id": executable["start_pose_id"],
            "motion_id": executable["motion_id"],
            "variant_id": executable["variant_id"],
            "policy_id": "DETERMINISTIC_SPREAD",
            "camera_profile_id": executable["camera_profile_id"],
            "camera_device_id": executable["camera_device_id"],
        }
        self.assertEqual(
            validate_operator_selection(self.catalog, selection, require_executable=True),
            selection,
        )
        general = {**selection, "data_mode": "GENERAL_COLLECTION"}
        self.assertEqual(
            validate_operator_selection(self.catalog, general)["data_mode"],
            "GENERAL_COLLECTION",
        )
        with self.assertRaisesRegex(ContractError, "OPERATOR_SELECTION_NOT_EXECUTABLE"):
            validate_operator_selection(self.catalog, general, require_executable=True)
        forged = {**selection, "grasp_id": "not-this-object"}
        with self.assertRaisesRegex(ContractError, "OPERATOR_SELECTION_COMBINATION"):
            validate_operator_selection(self.catalog, forged)
