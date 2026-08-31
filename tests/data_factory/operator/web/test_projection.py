import unittest
from pathlib import Path

from tools.data_factory.operator.catalog import (
    load_operator_catalog,
    validate_operator_selection,
)
from tools.data_factory.operator.web.projection import project_catalog
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


ROOT = Path(__file__).resolve().parents[4]
DEVICE = "usb-Generic_USB2.0_PC_CAMERA-video-index0"


class OperatorProjectionTests(unittest.TestCase):
    @staticmethod
    def selection(combination: dict) -> dict:
        return {
            "schema_version": "data_factory.operator_selection.v1",
            "combination_digest": combination["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            **{
                field: combination[field]
                for field in (
                    "workspace_id", "frame_id", "task_id", "object_id",
                    "grasp_id", "cell_id", "start_pose_id", "motion_id",
                    "variant_id", "camera_profile_id", "camera_device_id",
                )
            },
            "policy_id": "DETERMINISTIC_SPREAD",
        }

    def test_registered_workspace_projects_continuous_bounds_and_105_presets(self):
        catalog = load_operator_catalog(ROOT, device_ids=[DEVICE])
        domains = [
            item for item in catalog["workspace_domains"]
            if item["workspace_id"] == "PLACE_A"
            and item["frame_id"] == "place-a-yaw0-r002"
        ]
        self.assertEqual(len(domains), 1)
        domain = domains[0]
        self.assertEqual(set(domain), {
            "domain_id", "workspace_id", "frame_id", "coordinate_mode",
            "object_id", "coverage_region",
            "a4_family_digest", "yaw0_manifest_digest", "x_mm", "y_mm",
            "yaw_deg", "preset_cell_ids", "execution_gate", "domain_digest",
        })
        self.assertEqual(
            (
                domain["domain_id"], domain["coordinate_mode"],
                domain["x_mm"], domain["y_mm"], domain["yaw_deg"],
                domain["execution_gate"],
            ),
            (
                "place-a-yaw0-r002@wood-cube-25mm-r001",
                "CONTINUOUS_A4_PLANE",
                domain["x_mm"], domain["y_mm"],
                {"minimum": -180.0, "maximum_exclusive": 180.0},
                "FRESH_PLAN_IK_COLLISION_ENDPOINT_PER_SLOT",
            ),
        )
        self.assertEqual(domain["object_id"], "wood-cube-25mm-r001")
        self.assertGreater(domain["x_mm"]["maximum"], 150)
        self.assertEqual(
            domain["coverage_region"],
            {
                "shape": "RECTANGLE",
                "page_size_mm": [297.0, 210.0],
                "origin_xy_mm": [148.5, 105.0],
                "base_margin_xy_mm": [15.0, 20.0],
                "object_size_xy_mm": [25.0, 25.0],
                "uncertainty_mm": 16.0,
                "strata": {"columns": 5, "rows": 3},
                "coordinate_contract": "SHEET_XY_EQUALS_RZ_YAW_TIMES_LOCAL_XY",
            },
        )
        calibration = load_json_strict(
            ROOT / "config/data_factory/cells/place-a-yaw0-r002.json",
        )
        self.assertEqual(
            (domain["a4_family_digest"], domain["yaw0_manifest_digest"]),
            (calibration["a4_family_digest"], calibration["yaw0_manifest_digest"]),
        )
        preset_ids = {
            option["id"] for option in catalog["axes"]["cell"]
            if option["metadata"].get("place_id") == "PLACE_A"
        }
        self.assertEqual((len(preset_ids), set(domain["preset_cell_ids"])), (105, preset_ids))
        self.assertEqual(
            domain["domain_digest"],
            canonical_digest({
                key: value for key, value in domain.items()
                if key != "domain_digest"
            }),
        )
        combination = next(
            item for item in catalog["combinations"]
            if item["frame_id"] == domain["frame_id"]
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        projected = project_catalog(
            catalog, self.selection(combination), split="TRAIN",
        )
        self.assertEqual(projected["workspace_domain"], domain)
        general = next(
            item for item in projected["axes"]["data_mode"]
            if item["id"] == "PRODUCTION"
        )
        self.assertEqual(
            general["description"],
            "기술 검사와 사후 검토 대상인 일반 수집",
        )

    def test_pick_place_visibility_matches_current_caller_registration(self):
        catalog = load_operator_catalog(ROOT, device_ids=[DEVICE])
        option = next(
            item for item in catalog["axes"]["task"] if item["id"] == "pick_place"
        )
        pick_place_combinations = [
            item for item in catalog["combinations"]
            if item["task_id"] == "pick_place"
        ]
        self.assertTrue(option["registered"])
        self.assertTrue(pick_place_combinations)
        self.assertTrue(any(
            item["execution"]["TEST_COLLECTION"]["executable"]
            for item in pick_place_combinations
        ))
        self.assertEqual(
            {
                item["sources"]["job"]
                for item in pick_place_combinations
            },
            {
                "config/data_factory/test_only_physical/goal2-place1/"
                "center-live-p45-20260821-r001.job.json"
            },
        )
