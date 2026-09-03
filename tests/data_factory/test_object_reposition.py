from __future__ import annotations

import copy
import unittest
from pathlib import Path

from tools.data_factory.motion.object_reposition import (
    build_object_reposition_binding,
    validate_object_reposition_binding,
    yaw_preserving_destination,
)
from tools.data_factory.state_space import (
    sample_yaw_cdf_strata,
    validate_yaw_sampling_profile,
)
from tools.data_factory.workspace_geometry import rotate_xy
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


ROOT = Path(__file__).resolve().parents[2]


class ObjectRepositionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.object_profile = load_json_strict(
            ROOT / "config/data_factory/objects/wood-cube-24mm-r001.json",
        )
        cls.grasp_profile = load_json_strict(
            ROOT / "config/data_factory/grasps/"
            "wood-cube-24mm-top-3p5mm-r001.json",
        )
        cls.profile = validate_yaw_sampling_profile(load_json_strict(
            ROOT / "config/data_factory/yaw_sampling_profiles/"
            "wood-cube-24mm-top-r001.json",
        ), object_profile=cls.object_profile, grasp_profile=cls.grasp_profile)
        cls.yaw_binding = sample_yaw_cdf_strata(
            cls.profile, sampling_seed=81,
            sweep_identity={"workspace_id": "PLACE_A", "node": 1},
            strata_count=1,
        )[0]

    def test_pick_place_destination_preserves_source_yaw(self):
        destination = {
            "place_id": "PLACE_B", "yaw_deg": 31, "x_mm": 8, "y_mm": -9,
        }
        result = yaw_preserving_destination(
            {"place_id": "PLACE_A", "yaw_deg": -17, "x_mm": 0, "y_mm": 0},
            destination,
        )
        self.assertEqual((result["place_id"], result["yaw_deg"]), ("PLACE_B", -17.0))
        for actual, expected in zip(
            rotate_xy((result["x_mm"], result["y_mm"]), result["yaw_deg"]),
            rotate_xy(
                (destination["x_mm"], destination["y_mm"]),
                destination["yaw_deg"],
            ),
        ):
            self.assertAlmostEqual(actual, expected)

    def test_both_entry_states_share_one_non_recording_contract(self):
        target = {
            "place_id": "PLACE_A",
            "yaw_deg": self.yaw_binding["source_object_yaw_deg"],
            "x_mm": 4,
            "y_mm": -6,
        }
        held = build_object_reposition_binding(
            parent_run_id="run-1", continuation_run_id="run-1",
            next_run_id="run-2", start_state="HELD_OBJECT",
            source_pose={"place_id": "PLACE_A", "yaw_deg": 0, "x_mm": 0, "y_mm": 0},
            target_pose=target, object_profile=self.object_profile,
            grasp_profile=self.grasp_profile, yaw_sampling_profile=self.profile,
            yaw_sample_binding=self.yaw_binding,
        )
        surface_source = yaw_preserving_destination(
            {**target, "yaw_deg": 0}, target,
        )
        surface = build_object_reposition_binding(
            parent_run_id="run-2", continuation_run_id="run-2-reposition",
            next_run_id="run-3", start_state="ON_SURFACE",
            source_pose=surface_source, target_pose=target,
            object_profile=self.object_profile, grasp_profile=self.grasp_profile,
            yaw_sampling_profile=self.profile,
            yaw_sample_binding=self.yaw_binding,
        )
        for value, state in ((held, "HELD_OBJECT"), (surface, "ON_SURFACE")):
            self.assertEqual(validate_object_reposition_binding(
                value, object_profile=self.object_profile,
                grasp_profile=self.grasp_profile,
                yaw_sampling_profile=self.profile,
            ), value)
            self.assertEqual(value["start_state"], state)
            self.assertEqual(
                value["execution_stage"],
                "PRECOMMIT_POST_RECORDING"
                if state == "HELD_OBJECT" else "POSTCOMMIT",
            )
            self.assertEqual(value["recording_scope"], "OUT_OF_DATASET")
            self.assertEqual(
                value["object_profile_digest"],
                canonical_digest(self.object_profile),
            )
            self.assertFalse(value["recorder_authorized"])
            self.assertFalse(value["dataset_write_authorized"])

    def test_surface_mode_is_same_position_yaw_only_and_fails_closed(self):
        target = {
            "place_id": "PLACE_A",
            "yaw_deg": self.yaw_binding["source_object_yaw_deg"],
            "x_mm": 4,
            "y_mm": -6,
        }
        with self.assertRaisesRegex(
            ContractError, "OBJECT_REPOSITION_ON_SURFACE_SCOPE",
        ):
            build_object_reposition_binding(
                parent_run_id="run-3", continuation_run_id="run-3-reposition",
                next_run_id="run-4", start_state="ON_SURFACE",
                source_pose={**target, "yaw_deg": 0, "x_mm": 5},
                target_pose=target, object_profile=self.object_profile,
                grasp_profile=self.grasp_profile,
                yaw_sampling_profile=self.profile,
                yaw_sample_binding=self.yaw_binding,
            )
        source = yaw_preserving_destination({**target, "yaw_deg": 0}, target)
        tampered = build_object_reposition_binding(
            parent_run_id="run-3", continuation_run_id="run-3-reposition",
            next_run_id="run-4", start_state="ON_SURFACE",
            source_pose=source, target_pose=target,
            object_profile=self.object_profile, grasp_profile=self.grasp_profile,
            yaw_sampling_profile=self.profile,
            yaw_sample_binding=self.yaw_binding,
        )
        tampered = copy.deepcopy(tampered)
        tampered["recorder_authorized"] = True
        with self.assertRaisesRegex(ContractError, "OBJECT_REPOSITION_BINDING"):
            validate_object_reposition_binding(tampered)

    def test_surface_mode_rejects_numerically_empty_yaw_changes(self):
        target = {
            "place_id": "PLACE_A", "yaw_deg": 5e-10,
            "x_mm": 4, "y_mm": -6,
        }
        source = yaw_preserving_destination(
            {**target, "yaw_deg": 0.0}, target,
        )
        with self.assertRaisesRegex(
            ContractError, "OBJECT_REPOSITION_ON_SURFACE_SCOPE",
        ):
            build_object_reposition_binding(
                parent_run_id="run-3",
                continuation_run_id="run-3-reposition",
                next_run_id="run-4", start_state="ON_SURFACE",
                source_pose=source, target_pose=target,
                object_profile=self.object_profile,
                grasp_profile=self.grasp_profile,
            )

    def test_surface_continuation_is_distinct_and_profile_bound(self):
        target = {
            "place_id": "PLACE_A",
            "yaw_deg": self.yaw_binding["source_object_yaw_deg"],
            "x_mm": 4,
            "y_mm": -6,
        }
        with self.assertRaisesRegex(
            ContractError, "OBJECT_REPOSITION_CONTINUATION",
        ):
            build_object_reposition_binding(
                parent_run_id="run-3", continuation_run_id="run-3",
                next_run_id="run-4", start_state="ON_SURFACE",
                source_pose=yaw_preserving_destination(
                    {**target, "yaw_deg": 0}, target,
                ), target_pose=target,
                object_profile=self.object_profile,
                grasp_profile=self.grasp_profile,
                yaw_sampling_profile=self.profile,
                yaw_sample_binding=self.yaw_binding,
            )
        with self.assertRaisesRegex(
            ContractError, "OBJECT_REPOSITION_YAW_BINDING",
        ):
            build_object_reposition_binding(
                parent_run_id="run-3",
                continuation_run_id="run-3-reposition",
                next_run_id="run-4", start_state="ON_SURFACE",
                source_pose=yaw_preserving_destination(
                    {**target, "yaw_deg": 0}, target,
                ), target_pose=target,
                object_profile=self.object_profile,
                grasp_profile=self.grasp_profile,
                yaw_sample_binding=self.yaw_binding,
            )


if __name__ == "__main__":
    unittest.main()
