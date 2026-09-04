from __future__ import annotations

import copy
import unittest
from pathlib import Path

from tools.data_factory.state_space import (
    YAW_BINDING_SCHEMA,
    bind_yaw_sample_to_state_space,
    canonical_yaw_for_profile,
    configure_state_space_design_profile,
    rotating_balanced_yaw_ranks,
    sample_yaw_cdf_strata,
    validate_approach_sampling_profile,
    validate_configured_state_space_design_profile,
    validate_state_space_design_profile,
    validate_yaw_sample_binding,
    validate_yaw_sampling_profile,
    yaw_cdf_strata_bounds,
)
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


ROOT = Path(__file__).resolve().parents[2]


class YawStateSpaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.object_profile = load_json_strict(
            ROOT / "config/data_factory/objects/wood-cube-24mm-r001.json",
        )
        cls.grasp_profile = load_json_strict(
            ROOT / "config/data_factory/grasps/"
            "wood-cube-24mm-top-3p5mm-r001.json",
        )
        cls.profile = load_json_strict(
            ROOT / "config/data_factory/yaw_sampling_profiles/"
            "wood-cube-24mm-top-r001.json",
        )
        cls.collection_profile = load_json_strict(
            ROOT / "config/data_factory/collection_profiles/"
            "fr5-up-wrist-rgb-30hz-v2.json",
        )
        cls.approach_profile = load_json_strict(
            ROOT / "config/data_factory/approach_sampling_profiles/"
            "wood-cube-24mm-top-wrist-r001.json",
        )
        cls.state_space_design_profile = load_json_strict(
            ROOT / "config/data_factory/state_space_design_profiles/"
            "wood-cube-24mm-a4-cdf3-r001.json",
        )

    def test_profile_is_explicitly_bound_to_object_and_grasp(self):
        checked = validate_yaw_sampling_profile(
            self.profile,
            object_profile=self.object_profile,
            grasp_profile=self.grasp_profile,
        )

        self.assertEqual(checked["planar_symmetry_order"], 4)
        self.assertEqual(checked["yaw_equivalence_period_deg"], 90.0)
        self.assertEqual(
            checked["canonical_interval_deg"],
            {"minimum": -45.0, "maximum_exclusive": 45.0},
        )
        self.assertTrue(checked["profile_digest"].startswith("sha256:"))
        self.assertEqual(checked["required_camera_roles"], ["wrist"])
        self.assertEqual(checked["distribution"], {"kind": "STRATIFIED_UNIFORM"})

        tampered = copy.deepcopy(self.profile)
        tampered["object_profile_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ContractError, "YAW_PROFILE_OBJECT_BINDING"):
            validate_yaw_sampling_profile(
                tampered,
                object_profile=self.object_profile,
                grasp_profile=self.grasp_profile,
            )

    def test_approach_distribution_is_bound_to_object_grasp_and_wrist_view(self):
        checked = validate_approach_sampling_profile(
            self.approach_profile,
            object_profile=self.object_profile,
            grasp_profile=self.grasp_profile,
            collection_profile=self.collection_profile,
        )
        self.assertEqual(checked["required_camera_roles"], ["wrist"])
        self.assertEqual(
            checked["parameter_distribution"]["align_clearance_m"],
            {
                "kind": "TRUNCATED_NORMAL", "mean": 0.0575,
                "standard_deviation": 0.00125,
                "minimum": 0.055, "maximum": 0.06,
            },
        )
        self.assertEqual(
            checked["parameter_distribution"]["view_offset_xy_m"]
            ["maximum_radius_fraction"],
            0.5,
        )
        up_only = copy.deepcopy(self.collection_profile)
        up_only["collection_profile_id"] = "up-only"
        up_only["camera_roles"] = ["up"]
        with self.assertRaisesRegex(
            ContractError, "APPROACH_PROFILE_COLLECTION_BINDING",
        ):
            validate_approach_sampling_profile(
                self.approach_profile,
                object_profile=self.object_profile,
                grasp_profile=self.grasp_profile,
                collection_profile=up_only,
            )

    def test_state_space_design_owns_configurable_spatial_and_yaw_factors(self):
        yaw = validate_yaw_sampling_profile(
            self.profile,
            object_profile=self.object_profile,
            grasp_profile=self.grasp_profile,
        )
        checked = validate_state_space_design_profile(
            self.state_space_design_profile,
            object_profile=self.object_profile,
            grasp_profile=self.grasp_profile,
            yaw_sampling_profile=yaw,
        )

        self.assertEqual(checked["spatial_strata"], {"columns": 5, "rows": 3})
        self.assertEqual(checked["yaw_cdf_strata"], 3)
        self.assertTrue(checked["profile_digest"].startswith("sha256:"))

        changed = copy.deepcopy(self.state_space_design_profile)
        changed["spatial_strata"] = {"columns": 4, "rows": 4}
        changed["yaw_cdf_strata"] = 4
        changed_checked = validate_state_space_design_profile(
            changed,
            object_profile=self.object_profile,
            grasp_profile=self.grasp_profile,
            yaw_sampling_profile=yaw,
        )
        self.assertEqual(changed_checked["spatial_strata"], {
            "columns": 4, "rows": 4,
        })
        self.assertEqual(changed_checked["yaw_cdf_strata"], 4)

        tampered = copy.deepcopy(self.state_space_design_profile)
        tampered["yaw_sampling_profile_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(
            ContractError, "STATE_SPACE_DESIGN_YAW_BINDING",
        ):
            validate_state_space_design_profile(
                tampered,
                object_profile=self.object_profile,
                grasp_profile=self.grasp_profile,
                yaw_sampling_profile=yaw,
            )

    def test_configured_design_changes_only_factors_and_has_stable_identity(self):
        source = validate_state_space_design_profile(
            self.state_space_design_profile,
        )
        unchanged = configure_state_space_design_profile(source, {
            "columns": 5, "rows": 3, "yaw_cdf_strata": 3,
        })
        self.assertEqual(unchanged, source)

        factors = {"columns": 4, "rows": 2, "yaw_cdf_strata": 2}
        first = configure_state_space_design_profile(source, factors)
        second = configure_state_space_design_profile(
            copy.deepcopy(source), copy.deepcopy(factors),
        )
        self.assertEqual(first, second)
        self.assertEqual(first["spatial_strata"], {"columns": 4, "rows": 2})
        self.assertEqual(first["yaw_cdf_strata"], 2)
        self.assertIn("-n4x2x2", first["state_space_design_profile_id"])
        self.assertEqual(
            validate_configured_state_space_design_profile(
                first, source_profile=source,
            ),
            first,
        )

        tampered = copy.deepcopy(first)
        tampered.pop("profile_digest")
        tampered["assignment"] = "OTHER"
        with self.assertRaisesRegex(ContractError, "STATE_SPACE_DESIGN_SCHEMA"):
            validate_configured_state_space_design_profile(
                tampered, source_profile=source,
            )
        for invalid in (
            {"columns": 0, "rows": 2, "yaw_cdf_strata": 2},
            {"columns": -1, "rows": 2, "yaw_cdf_strata": 2},
            {"columns": 2.0, "rows": 2, "yaw_cdf_strata": 2},
            {"columns": 11, "rows": 10, "yaw_cdf_strata": 2},
            {"columns": 2, "rows": 2, "yaw_cdf_strata": 5},
        ):
            with self.assertRaisesRegex(
                ContractError, "STATE_SPACE_DESIGN_FACTORS",
            ):
                configure_state_space_design_profile(source, invalid)

    def test_rotating_fractional_factor_is_balanced_without_xy_yaw_locking(self):
        sweeps = [
            rotating_balanced_yaw_ranks(
                15, 3, sweep_index=index,
                anchor_cell_index=7, anchor_yaw_rank=1,
            )
            for index in range(3)
        ]
        self.assertEqual(sweeps[0][7], 1)
        self.assertTrue(all(
            [ranks.count(rank) for rank in range(3)] == [5, 5, 5]
            for ranks in sweeps
        ))
        self.assertTrue(all(
            {sweep[cell] for sweep in sweeps} == {0, 1, 2}
            for cell in range(15)
        ))

        nondefault_grid = rotating_balanced_yaw_ranks(
            16, 3, sweep_index=0,
            anchor_cell_index=5, anchor_yaw_rank=2,
        )
        counts = [nondefault_grid.count(rank) for rank in range(3)]
        self.assertLessEqual(max(counts) - min(counts), 1)

    def test_three_uniform_cdf_tiers_condition_on_the_real_source_yaw(self):
        checked = validate_yaw_sampling_profile(
            self.profile,
            object_profile=self.object_profile,
            grasp_profile=self.grasp_profile,
        )
        values = sample_yaw_cdf_strata(
            checked, sampling_seed=123,
            sweep_identity={"campaign": "c1", "sweep": 0},
            strata_count=3, conditioned_yaw_deg=0.0,
        )

        self.assertEqual([item["sample_rank"] for item in values], [0, 1, 2])
        self.assertEqual(values[1]["sample_origin"], "CONDITIONED_SOURCE_ANCHOR")
        self.assertEqual(values[1]["source_object_yaw_deg"], 0.0)
        self.assertTrue(-45.0 < values[0]["source_object_yaw_deg"] < -15.0)
        self.assertTrue(-15.0 <= values[1]["source_object_yaw_deg"] < 15.0)
        self.assertTrue(15.0 <= values[2]["source_object_yaw_deg"] < 45.0)
        changed = sample_yaw_cdf_strata(
            checked, sampling_seed=124,
            sweep_identity={"campaign": "c1", "sweep": 0},
            strata_count=3, conditioned_yaw_deg=0.0,
        )
        self.assertEqual(changed[1]["source_object_yaw_deg"], 0.0)
        self.assertEqual(changed[1]["yaw_sample_quantile"], 0.5)
        self.assertNotEqual(
            [values[0]["source_object_yaw_deg"], values[2]["source_object_yaw_deg"]],
            [changed[0]["source_object_yaw_deg"], changed[2]["source_object_yaw_deg"]],
        )
        self.assertEqual(yaw_cdf_strata_bounds(checked, 3), [
            {
                "sample_rank": 0,
                "quantile": {"minimum": 0.0, "maximum_exclusive": 1 / 3},
                "yaw_deg": {"minimum": -45.0, "maximum_exclusive": -15.0},
            },
            {
                "sample_rank": 1,
                "quantile": {"minimum": 1 / 3, "maximum_exclusive": 2 / 3},
                "yaw_deg": {"minimum": -15.0, "maximum_exclusive": 15.0},
            },
            {
                "sample_rank": 2,
                "quantile": {"minimum": 2 / 3, "maximum_exclusive": 1.0},
                "yaw_deg": {"minimum": 15.0, "maximum_exclusive": 45.0},
            },
        ])
        equivalent = sample_yaw_cdf_strata(
            checked, sampling_seed=123,
            sweep_identity={"campaign": "c1", "sweep": 0},
            strata_count=3, conditioned_yaw_deg=90.0,
        )[1]
        self.assertEqual(canonical_yaw_for_profile(checked, 90.0), 0.0)
        self.assertEqual(equivalent["source_object_yaw_deg"], 90.0)
        self.assertEqual(equivalent["canonical_object_yaw_deg"], 0.0)
        self.assertEqual(equivalent["grasp_yaw_deg"], 90.0)
        self.assertEqual(
            validate_yaw_sample_binding(equivalent, profile=checked),
            equivalent,
        )

    def test_slot_binding_preserves_exact_design_cell(self):
        yaw = validate_yaw_sampling_profile(
            self.profile, object_profile=self.object_profile,
            grasp_profile=self.grasp_profile,
        )
        design = validate_state_space_design_profile(
            self.state_space_design_profile,
            object_profile=self.object_profile,
            grasp_profile=self.grasp_profile,
            yaw_sampling_profile=yaw,
        )
        pre_slot = sample_yaw_cdf_strata(
            yaw, sampling_seed=(1 << 64) - 1,
            sweep_identity={"campaign": "slot-binding-r001"},
            strata_count=3,
        )[0]
        slotted = bind_yaw_sample_to_state_space(
            pre_slot, state_space_design_profile=design,
            spatial_cell_index=7, spatial_row=1, spatial_column=2,
        )
        self.assertEqual(slotted["schema_version"], YAW_BINDING_SCHEMA)
        self.assertEqual(
            {
                key: slotted[key] for key in (
                    "state_space_design_profile_id",
                    "state_space_design_profile_digest",
                    "spatial_cell_index", "spatial_row", "spatial_column",
                )
            },
            {
                "state_space_design_profile_id": design[
                    "state_space_design_profile_id"
                ],
                "state_space_design_profile_digest": design["profile_digest"],
                "spatial_cell_index": 7, "spatial_row": 1,
                "spatial_column": 2,
            },
        )
        self.assertEqual(
            validate_yaw_sample_binding(
                slotted, profile=yaw, state_space_design_profile=design,
            ),
            slotted,
        )
        tampered = copy.deepcopy(slotted)
        tampered["spatial_cell_index"] = 8
        tampered["binding_digest"] = canonical_digest({
            key: value for key, value in tampered.items()
            if key != "binding_digest"
        })
        with self.assertRaisesRegex(
            ContractError, "YAW_BINDING_STATE_SPACE",
        ):
            validate_yaw_sample_binding(
                tampered, state_space_design_profile=design,
            )

        self.assertEqual(
            validate_yaw_sample_binding(pre_slot, profile=yaw), pre_slot,
        )

if __name__ == "__main__":
    unittest.main()
