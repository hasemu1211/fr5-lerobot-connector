import copy
import unittest
from pathlib import Path

from tools.data_factory import run_job
from tools.data_factory.operator.catalog import load_operator_catalog
from tools.fr5_data_factory import (
    ContractError,
    canonical_digest,
    load_json_strict,
    resolve_motion_program,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/data_factory"
JOB_PATH = (
    CONFIG / "jobs/center-live-24mm-20260901-r001.job.json"
)
SHEET_A = ROOT / "tools/a4_place_yaw/json/place_a_yaw_p000_00.json"
SHEET_B = CONFIG / "workspace_sheets/place-b-yaw0-r001_yaw0_sheet.json"
MOTION_A = CONFIG / "motion_qualifications/fr5-place-a-wood-cube-24mm-r001.json"
MOTION_B = CONFIG / "motion_qualifications/fr5-place-b-wood-cube-24mm-r001.json"
HOME = CONFIG / "home_candidates/fr5-lab-a-home-r001.json"
URDF = ROOT / "src/fairino_description/urdf/fairino5_v6.urdf"


class HandlingSsotTest(unittest.TestCase):
    def _payload(self, place):
        job = load_json_strict(JOB_PATH)
        sheet, motion = (SHEET_A, MOTION_A)
        if place == "PLACE_B":
            sheet, motion = SHEET_B, MOTION_B
            job.update(
                job_id="place-b-contract-test",
                place_id=place,
                cell_calibration_id="place-b-yaw0-r001",
                sheet_manifest_digest=canonical_digest(load_json_strict(sheet)),
            )
        return {
            "run_id": f"handling-ssot-{place.lower()}",
            "job": job,
            "selected_sheet": str(sheet),
            "yaw0_sheet": str(sheet),
            "config_root": str(CONFIG),
            "motion_qualification": str(motion),
            "home_candidate": str(HOME),
            "urdf": str(URDF),
            "expected_robot_system_id": "fr5-lab-a",
        }, motion

    def test_object_grasp_workspace_and_floor_share_one_geometry(self):
        expected_tcp_z = {"PLACE_A": 0.004043, "PLACE_B": 0.004048}
        for place in expected_tcp_z:
            with self.subTest(place=place):
                payload, motion_path = self._payload(place)
                validated, program, _binding = run_job.resolve_inputs(
                    payload, scene_binding_call=lambda *_args: {},
                )
                grasp = validated["grasp_profile"]
                self.assertEqual(
                    validated["object_profile"]["dimensions_mm"],
                    [24.0, 24.0, 24.0],
                )
                self.assertEqual(
                    grasp["grasp_geometry"]["depth_from_top_mm"], 3.5,
                )
                self.assertEqual(
                    grasp["grasp_geometry"]["datum_to_tcp_grasp"][
                        "translation_m"
                    ][2],
                    0.0085,
                )
                steps = {step["phase"]: step for step in program["steps"]}
                final_z = steps["FINAL_APPROACH_LIN"]["target"]["base_tcp"][
                    "translation_m"
                ][2]
                lower_z = steps["LOWER_LIN"]["target"]["base_tcp"][
                    "translation_m"
                ][2]
                self.assertAlmostEqual(final_z, expected_tcp_z[place], places=12)
                self.assertAlmostEqual(lower_z, expected_tcp_z[place], places=12)
                self.assertGreater(
                    steps["APPROACH_STOP_LIN"]["target"]["base_tcp"][
                        "translation_m"
                    ][2],
                    final_z,
                )
                scene_profile = load_json_strict(
                    CONFIG / "planning_scenes/fr5-table-floor-wall-r002.json"
                )
                measured_floor = scene_profile["floor"][
                    "measured_surface_z_m"
                ]
                self.assertAlmostEqual(
                    final_z - measured_floor, 0.0205, delta=0.00001,
                )

                motion = load_json_strict(motion_path)
                bad_motion = copy.deepcopy(motion)
                bad_motion["datum_to_tcp_grasp"]["translation_m"][2] = 0.0075
                with self.assertRaises(ContractError) as caught:
                    resolve_motion_program(
                        validated, bad_motion, load_json_strict(HOME),
                        urdf=URDF, expected_robot_system_id="fr5-lab-a",
                        planning_scene_profile=scene_profile,
                    )
                self.assertEqual(caught.exception.code, "MOTION_GRASP_BINDING")

                bad_validated = copy.deepcopy(validated)
                bad_validated["calibration"]["center"][2] += 0.002
                with self.assertRaises(ContractError) as caught:
                    resolve_motion_program(
                        bad_validated, motion, load_json_strict(HOME),
                        urdf=URDF, expected_robot_system_id="fr5-lab-a",
                        planning_scene_profile=scene_profile,
                    )
                self.assertEqual(
                    caught.exception.code, "MOTION_WORKSPACE_FLOOR_BINDING",
                )

    def test_gripper_profile_reuses_across_workspaces(self):
        for place in ("PLACE_A", "PLACE_B"):
            with self.subTest(place=place):
                payload, motion_path = self._payload(place)
                validated, program, _binding = run_job.resolve_inputs(
                    payload, scene_binding_call=lambda *_args: {},
                )
                grasp = validated["grasp_profile"]
                motion = load_json_strict(motion_path)
                self.assertEqual(
                    motion["gripper_positions_m"]["closed"],
                    grasp["gripper_close"]["command_position_m"],
                )
                self.assertEqual(
                    program["gripper_requirements"]["force_percent"], 20,
                )
                self.assertEqual(
                    program["gripper_requirements"]["open_force_percent"], 50,
                )
                self.assertEqual(
                    program["gripper_requirements"]["open_velocity_percent"], 10,
                )
                self.assertEqual(
                    grasp["gripper_open"]["force_percent"], 50,
                )
                self.assertEqual(
                    grasp["gripper_open"]["velocity_percent"], 10,
                )
                self.assertEqual(
                    motion["gripper_positions_m"]["open"],
                    grasp["gripper_open"]["command_position_m"],
                )

    def test_catalog_separates_authoring_domains_from_exact_24mm_motion_bindings(self):
        catalog = load_operator_catalog(ROOT)
        domains = {
            (item["workspace_id"], item["frame_id"])
            for item in catalog["workspace_domains"]
            if item["object_id"] == "wood-cube-24mm-r001"
        }
        self.assertTrue({
            ("PLACE_A", "place-a-yaw0-r003"),
            ("PLACE_B", "place-b-yaw0-r001"),
        } <= domains)
        self.assertIn(("PLACE_A", "place-a-yaw0-r002"), domains)
        routes = {
            (
                item["workspace_id"], item["frame_id"], item["task_id"],
                item["motion_id"], item["sources"]["job"],
            )
            for item in catalog["combinations"]
            if item["object_id"] == "wood-cube-24mm-r001"
        }
        expected_job = str(JOB_PATH.relative_to(ROOT))
        route_bindings = {
            (place, frame, task, motion)
            for place, frame, task, motion, _ in routes
        }
        self.assertTrue(
            {
                (
                    "PLACE_A", "place-a-yaw0-r003", "pickup_e2e",
                    "fr5-place-a-wood-cube-24mm-r001",
                ),
                (
                    "PLACE_A", "place-a-yaw0-r003", "pick_place",
                    "fr5-place-a-wood-cube-24mm-r001",
                ),
                (
                    "PLACE_B", "place-b-yaw0-r001", "pickup_e2e",
                    "fr5-place-b-wood-cube-24mm-r001",
                ),
                (
                    "PLACE_B", "place-b-yaw0-r001", "pick_place",
                    "fr5-place-b-wood-cube-24mm-r001",
                ),
            } <= route_bindings,
        )
        self.assertFalse(any(
            place == "PLACE_A" and frame == "place-a-yaw0-r002"
            for place, frame, _task, _motion in route_bindings
        ))
        self.assertIn(expected_job, {source for *_, source in routes})

    def test_production_catalog_exposes_both_tasks_in_both_workspaces(self):
        bindings = {
            "up": "254622073507",
            "wrist": "usb-Generic_USB2.0_PC_CAMERA-video-index0",
        }
        profile = load_json_strict(
            CONFIG / "collection_profiles/fr5-up-wrist-rgb-30hz-v1.json",
        )
        self.assertEqual(
            profile["portability_status"], "QUALIFICATION_REQUIRED",
        )
        catalog = load_operator_catalog(ROOT, device_ids=list(bindings.values()))
        routes = {
            (item["workspace_id"], item["task_id"])
            for item in catalog["combinations"]
            if item["object_id"] == "wood-cube-24mm-r001"
            and item["camera_bindings"] == bindings
            and item["execution"]["GENERAL_COLLECTION"]["executable"]
        }
        self.assertEqual(routes, {
            ("PLACE_A", "pickup_e2e"),
            ("PLACE_A", "pick_place"),
            ("PLACE_B", "pickup_e2e"),
            ("PLACE_B", "pick_place"),
        })


if __name__ == "__main__":
    unittest.main()
