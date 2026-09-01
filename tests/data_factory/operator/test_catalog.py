import unittest
from pathlib import Path

from tools.data_factory.operator.catalog import (
    load_operator_catalog,
    project_workspace_cycle_poses,
    resolve_workspace_cycle_selections,
    validate_operator_pose,
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
        self.assertEqual(
            {option["label"] for option in self.catalog["axes"]["motion"]},
            {"검증된 접근·집기·이송 경로"},
        )
        self.assertEqual(
            {option["id"]: option["label"] for option in self.catalog["axes"]["variant"]},
            {"DIRECT": "직선 1단계", "TWO_STAGE_ALIGN": "직선 2단계 정렬"},
        )
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

    def test_pick_place_workspace_cycle_uses_each_endpoint_domain(self):
        catalog = load_operator_catalog(
            ROOT, device_ids=["camera-up", "camera-wrist"],
        )
        source = next(
            item for item in catalog["combinations"]
            if item["workspace_id"] == "PLACE_A"
            and item["frame_id"] == "place-a-yaw0-r003"
            and item["task_id"] == "pick_place"
            and item["object_id"] == "wood-cube-24mm-r001"
            and item["cell_id"] == "PLACE_A-yaw0-CENTER"
            and item["camera_profile_id"] == "fr5-up-wrist-rgb-30hz-v1"
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        selection = {
            "schema_version": "data_factory.operator_selection.v2",
            "combination_digest": source["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            **{
                field: source[field]
                for field in (
                    "workspace_id", "frame_id", "task_id", "object_id",
                    "grasp_id", "cell_id", "start_pose_id", "motion_id",
                    "variant_id", "camera_profile_id", "camera_device_id",
                    "camera_bindings", "camera_binding_digest",
                )
            },
            "policy_id": "DETERMINISTIC_SPREAD",
        }
        start = {
            "place_id": "PLACE_A", "yaw_deg": 0,
            "x_mm": 0, "y_mm": 0,
        }

        a_cycle = resolve_workspace_cycle_selections(catalog, selection, 2)
        self.assertEqual(
            [(item["workspace_id"], item["frame_id"]) for item in a_cycle],
            [
                ("PLACE_A", "place-a-yaw0-r003"),
                ("PLACE_B", "place-b-yaw0-r001"),
                ("PLACE_A", "place-a-yaw0-r003"),
            ],
        )
        poses = project_workspace_cycle_poses(catalog, selection, start, 2)
        self.assertEqual([item["place_id"] for item in poses], [
            "PLACE_A", "PLACE_B", "PLACE_A",
        ])
        self.assertTrue(all(
            validate_operator_pose(catalog, endpoint, pose) == pose
            for endpoint, pose in zip(a_cycle, poses)
        ))

        b_selection = a_cycle[1]
        b_start = {
            "place_id": "PLACE_B", "yaw_deg": 0,
            "x_mm": 0, "y_mm": 0,
        }
        b_cycle = resolve_workspace_cycle_selections(catalog, b_selection, 3)
        self.assertEqual(
            [item["workspace_id"] for item in b_cycle],
            ["PLACE_B", "PLACE_A", "PLACE_B", "PLACE_A"],
        )
        self.assertEqual(
            [item["place_id"] for item in project_workspace_cycle_poses(
                catalog, b_selection, b_start, 3,
            )],
            ["PLACE_B", "PLACE_A", "PLACE_B", "PLACE_A"],
        )
