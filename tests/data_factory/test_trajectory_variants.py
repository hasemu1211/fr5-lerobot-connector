import copy
import json
import unittest
from pathlib import Path

try:
    from .test_motion import motion
except ImportError:  # Focused discovery loads this directory as top-level modules.
    from test_motion import motion
from tools.data_factory.motion.trajectory_variants import (
    compile_execution_motion_program,
    compile_motion_program_v3,
    compile_plan_only_candidate,
    legacy_phase_variant_catalog,
    phase_variant_catalog,
    trajectory_variant_binding,
    validate_motion_program_v3,
    validate_phase_variant_catalog,
    validate_plan_only_candidate,
    validate_trajectory_variant_binding,
)
from tools.fr5_data_factory import ContractError, canonical_digest
from tools.fr5_data_factory import load_json_strict


OBJECT_DIMENSIONS_MM = [24.0, 24.0, 24.0]
DESIGN_DIGEST = canonical_digest("trajectory-finite-design-test")
APPROACH_PROFILE = load_json_strict(
    Path(__file__).resolve().parents[2]
    / "config/data_factory/approach_sampling_profiles/"
    "wood-cube-24mm-top-wrist-r001.json"
)

_compile_execution_motion_program = compile_execution_motion_program
_trajectory_variant_binding = trajectory_variant_binding


def _profiled(kwargs, variant):
    result = dict(kwargs)
    if variant == "TWO_STAGE_ALIGN_V2":
        result.setdefault("approach_sampling_profile", APPROACH_PROFILE)
    return result


def compile_execution_motion_program(*args, **kwargs):
    return _compile_execution_motion_program(
        *args, **_profiled(kwargs, kwargs.get("trajectory_variant_id")),
    )


def trajectory_variant_binding(*args, **kwargs):
    return _trajectory_variant_binding(
        *args, **_profiled(kwargs, kwargs.get("trajectory_variant_id")),
    )


def program():
    value = motion(True)
    pregrasp = next(step for step in value["steps"] if step["phase"] == "PREGRASP_PTP")
    approach = next(step for step in value["steps"] if step["phase"] == "APPROACH_STOP_LIN")
    for frame in ("base_tcp", "base_tool"):
        pregrasp["target"][frame]["translation_m"] = [0.0, 0.0, 0.1]
        approach["target"][frame]["translation_m"] = [0.0, 0.0, 0.02]
    return value


class FakeSurface:
    def __init__(self):
        self.calls = []
        self.actions = 0
        self.fail_role = None

    def plan_arm(self, role, target, joint_target, limits, frames, planning, start):
        self.calls.append((role, copy.deepcopy(target), list(start)))
        return {
            "terminal_status": "FAILED" if role == self.fail_role else "SUCCEEDED",
            "moveit_success": role != self.fail_role,
            "serialized_trajectory": role.encode(),
            "final_joint_state": [float(len(self.calls))] * 6,
        }

    def constraint_check(self, _plan):
        return True

    def collision_check(self, _plan):
        return True

    def execute_arm(self, *_args):
        self.actions += 1

    def build_gripper_goal(self, *_args):
        self.actions += 1

    def write_dataset(self, *_args):
        self.actions += 1


def compile_candidate(variant, surface=None, **overrides):
    surface = surface or FakeSurface()
    kwargs = {
        "run_id": "offline-1",
        "trajectory_variant_id": variant,
        "sampling_seed": 7,
        "initial_joint_state": [0.0] * 6,
        "plan_arm": surface.plan_arm,
        "constraint_check": surface.constraint_check,
        "collision_check": surface.collision_check,
    }
    kwargs.update(overrides)
    return compile_plan_only_candidate(program(), **kwargs), surface


class TrajectoryVariantTest(unittest.TestCase):
    def test_v2_requires_an_explicit_bound_approach_profile(self):
        with self.assertRaisesRegex(
            ContractError, "VARIANT_APPROACH_PROFILE_REQUIRED",
        ):
            _compile_execution_motion_program(
                program(), trajectory_variant_id="TWO_STAGE_ALIGN_V2",
                sampling_seed=0, target_yaw_deg=0.0,
                object_dimensions_mm=OBJECT_DIMENSIONS_MM,
            )

    def test_execution_binding_records_exact_seed_parameters_and_program(self):
        source = program()
        compiled = compile_execution_motion_program(
            source, trajectory_variant_id="TWO_STAGE_ALIGN_V2",
            sampling_seed=23, target_yaw_deg=45.0,
            object_dimensions_mm=OBJECT_DIMENSIONS_MM,
            sample_rank=2, design_size=7, design_digest=DESIGN_DIGEST,
        )

        binding = trajectory_variant_binding(
            compiled, trajectory_variant_id="TWO_STAGE_ALIGN_V2",
            sampling_seed=23, target_yaw_deg=45.0,
            object_dimensions_mm=OBJECT_DIMENSIONS_MM,
            sample_rank=2, design_size=7, design_digest=DESIGN_DIGEST,
        )

        self.assertEqual(
            binding["schema_version"],
            "data_factory.trajectory_variant_binding.v2",
        )
        self.assertEqual(binding["sampling_seed"], 23)
        self.assertEqual(
            (binding["sample_rank"], binding["design_size"]), (2, 7),
        )
        self.assertEqual(binding["design_digest"], DESIGN_DIGEST)
        self.assertEqual(binding["target_yaw_deg"], 45.0)
        self.assertEqual(
            binding["motion_program_digest"], canonical_digest(compiled),
        )
        self.assertEqual(
            binding["phase_parameters_digest"],
            canonical_digest(binding["phase_parameters"]),
        )
        self.assertEqual(
            binding["binding_digest"],
            canonical_digest({
                key: value for key, value in binding.items()
                if key != "binding_digest"
            }),
        )
        legacy = copy.deepcopy(binding)
        legacy["schema_version"] = "data_factory.trajectory_variant_binding.v1"
        for field in ("sample_rank", "design_size", "design_digest"):
            legacy.pop(field)
        legacy["binding_digest"] = canonical_digest({
            key: value for key, value in legacy.items()
            if key != "binding_digest"
        })
        self.assertEqual(validate_trajectory_variant_binding(legacy), legacy)

    def test_execution_binding_rejects_the_frozen_legacy_catalog(self):
        compiled = compile_execution_motion_program(
            program(), trajectory_variant_id="DIRECT", sampling_seed=7,
            target_yaw_deg=0.0,
        )
        with self.assertRaisesRegex(ContractError, "VARIANT_CATALOG_SCHEMA"):
            trajectory_variant_binding(
                compiled, trajectory_variant_id="DIRECT",
                sampling_seed=7, target_yaw_deg=0.0,
                catalog=legacy_phase_variant_catalog(),
            )

    def test_finite_design_stratifies_each_v2_parameter(self):
        design_size = 11
        bindings = []
        for rank in range(design_size):
            compiled = compile_execution_motion_program(
                program(), trajectory_variant_id="TWO_STAGE_ALIGN_V2",
                sampling_seed=1000 + rank, target_yaw_deg=0.0,
                object_dimensions_mm=OBJECT_DIMENSIONS_MM,
                sample_rank=rank, design_size=design_size,
                design_digest=DESIGN_DIGEST,
            )
            bindings.append(trajectory_variant_binding(
                compiled, trajectory_variant_id="TWO_STAGE_ALIGN_V2",
                sampling_seed=1000 + rank, target_yaw_deg=0.0,
                object_dimensions_mm=OBJECT_DIMENSIONS_MM,
                sample_rank=rank, design_size=design_size,
                design_digest=DESIGN_DIGEST,
            ))

        for key in (
            "align_clearance_quantile", "view_offset_radial_quantile",
            "view_offset_angle_quantile",
        ):
            self.assertEqual(
                {
                    min(int(item["phase_parameters"][key] * design_size), design_size - 1)
                    for item in bindings
                },
                set(range(design_size)),
            )
        self.assertEqual(
            bindings,
            [
                trajectory_variant_binding(
                    compile_execution_motion_program(
                        program(), trajectory_variant_id="TWO_STAGE_ALIGN_V2",
                        sampling_seed=1000 + rank, target_yaw_deg=0.0,
                        object_dimensions_mm=OBJECT_DIMENSIONS_MM,
                        sample_rank=rank, design_size=design_size,
                        design_digest=DESIGN_DIGEST,
                    ),
                    trajectory_variant_id="TWO_STAGE_ALIGN_V2",
                    sampling_seed=1000 + rank, target_yaw_deg=0.0,
                    object_dimensions_mm=OBJECT_DIMENSIONS_MM,
                    sample_rank=rank, design_size=design_size,
                    design_digest=DESIGN_DIGEST,
                )
                for rank in range(design_size)
            ],
        )
    def test_two_stage_v2_applies_target_yaw_only_at_visible_clearance(self):
        source = program()
        yaw_90 = [[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
        for step in source["steps"][:3]:
            for frame in ("base_tcp", "base_tool"):
                step["target"][frame]["rotation_columns"] = copy.deepcopy(yaw_90)

        compiled = compile_execution_motion_program(
            source, trajectory_variant_id="TWO_STAGE_ALIGN_V2",
            target_yaw_deg=90.0, sampling_seed=0,
            object_dimensions_mm=OBJECT_DIMENSIONS_MM,
        )
        pregrasp, align, descend = compiled["steps"][:3]
        identity = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        for frame in ("base_tcp", "base_tool"):
            self.assertLessEqual(
                abs(pregrasp["target"][frame]["translation_m"][2] - 0.0575),
                0.0025,
            )
            self.assertEqual(pregrasp["target"][frame]["rotation_columns"], identity)
            self.assertEqual(align["target"][frame]["rotation_columns"], yaw_90)
            xy_offset = [
                pregrasp["target"][frame]["translation_m"][index]
                - align["target"][frame]["translation_m"][index]
                for index in (0, 1)
            ]
            self.assertGreater(sum(value * value for value in xy_offset), 0.0)
            self.assertLessEqual(sum(value * value for value in xy_offset), 0.012 ** 2)
            self.assertEqual(
                descend["target"][frame]["translation_m"][:2],
                align["target"][frame]["translation_m"][:2],
            )
            self.assertEqual(descend["target"][frame]["rotation_columns"], yaw_90)
        self.assertEqual(compiled["steps"][3:], source["steps"][3:])

        sampled = {
            round(compile_execution_motion_program(
                source, trajectory_variant_id="TWO_STAGE_ALIGN_V2",
                target_yaw_deg=90.0, sampling_seed=seed,
                object_dimensions_mm=OBJECT_DIMENSIONS_MM,
            )["steps"][0]["target"]["base_tcp"]["translation_m"][2], 9)
            for seed in range(8)
        }
        self.assertGreater(len(sampled), 4)
        self.assertTrue(all(0.055 <= value <= 0.06 for value in sampled))

    def test_view_offset_is_object_relative_truncated_bivariate_normal(self):
        source = program()
        dimensions = [40.0, 20.0, 10.0]
        for seed in range(64):
            compiled = compile_execution_motion_program(
                source, trajectory_variant_id="TWO_STAGE_ALIGN_V2",
                target_yaw_deg=0.0, sampling_seed=seed,
                object_dimensions_mm=dimensions,
            )
            view, align = compiled["steps"][:2]
            dx, dy = [
                view["target"]["base_tcp"]["translation_m"][index]
                - align["target"]["base_tcp"]["translation_m"][index]
                for index in (0, 1)
            ]
            self.assertLessEqual((dx / 0.020) ** 2 + (dy / 0.010) ** 2, 1.0 + 1e-8)

        identity_source = program()
        rotated_source = program()
        yaw_90 = [[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
        for step in rotated_source["steps"][:3]:
            for frame in ("base_tcp", "base_tool"):
                step["target"][frame]["rotation_columns"] = copy.deepcopy(yaw_90)
        direct_axes = compile_execution_motion_program(
            identity_source, trajectory_variant_id="TWO_STAGE_ALIGN_V2",
            target_yaw_deg=0.0, sampling_seed=7,
            object_dimensions_mm=dimensions,
        )["steps"][:2]
        rotated_axes = compile_execution_motion_program(
            rotated_source, trajectory_variant_id="TWO_STAGE_ALIGN_V2",
            target_yaw_deg=90.0, sampling_seed=7,
            object_dimensions_mm=dimensions,
        )["steps"][:2]
        direct_offset = [
            direct_axes[0]["target"]["base_tcp"]["translation_m"][index]
            - direct_axes[1]["target"]["base_tcp"]["translation_m"][index]
            for index in (0, 1)
        ]
        rotated_offset = [
            rotated_axes[0]["target"]["base_tcp"]["translation_m"][index]
            - rotated_axes[1]["target"]["base_tcp"]["translation_m"][index]
            for index in (0, 1)
        ]
        self.assertAlmostEqual(rotated_offset[0], -direct_offset[1])
        self.assertAlmostEqual(rotated_offset[1], direct_offset[0])

        compiled = compile_execution_motion_program(
            identity_source, trajectory_variant_id="TWO_STAGE_ALIGN_V2",
            target_yaw_deg=0.0, sampling_seed=7,
            object_dimensions_mm=dimensions,
        )
        binding = trajectory_variant_binding(
            compiled, trajectory_variant_id="TWO_STAGE_ALIGN_V2",
            target_yaw_deg=0.0, sampling_seed=7,
            object_dimensions_mm=dimensions,
        )
        self.assertEqual(binding["phase_parameters"]["object_size_xy_m"], [0.04, 0.02])
        self.assertEqual(binding["phase_parameters"]["view_offset_radius_xy_m"], [0.02, 0.01])
        self.assertEqual(binding["phase_parameters"]["view_offset_standard_deviation_xy_m"], [0.008, 0.004])

        with self.assertRaisesRegex(ContractError, "VARIANT_OBJECT_DIMENSIONS"):
            compile_execution_motion_program(
                source, trajectory_variant_id="TWO_STAGE_ALIGN_V2",
                target_yaw_deg=0.0, sampling_seed=0,
            )

    def test_tilted_approach_keeps_view_offset_in_clearance_plane(self):
        source = program()
        pregrasp = source["steps"][0]["target"]
        approach = source["steps"][1]["target"]
        endpoint = source["steps"][2]["target"]
        for frame in ("base_tcp", "base_tool"):
            pregrasp[frame]["translation_m"] = [0.01, -0.02, 0.1]
            approach[frame]["translation_m"] = [0.002, -0.004, 0.02]
            endpoint[frame]["translation_m"] = [0.0, 0.0, 0.0]

        compiled = compile_execution_motion_program(
            source, trajectory_variant_id="TWO_STAGE_ALIGN_V2",
            target_yaw_deg=0.0, sampling_seed=7,
            object_dimensions_mm=OBJECT_DIMENSIONS_MM,
        )
        view, align, descend = compiled["steps"][:3]
        axis = [0.01, -0.02, 0.1]
        length = sum(value * value for value in axis) ** 0.5
        axis = [value / length for value in axis]
        offset = [
            view["target"]["base_tcp"]["translation_m"][index]
            - align["target"]["base_tcp"]["translation_m"][index]
            for index in range(3)
        ]
        self.assertAlmostEqual(sum(
            left * right for left, right in zip(axis, offset)
        ), 0.0)
        self.assertEqual(
            align["target"]["base_tcp"]["rotation_columns"],
            descend["target"]["base_tcp"]["rotation_columns"],
        )

    def test_catalog_is_exact_finite_and_byte_stable(self):
        first, second = phase_variant_catalog(), phase_variant_catalog()
        self.assertEqual(first, validate_phase_variant_catalog(first))
        self.assertEqual(
            [item["trajectory_variant_id"] for item in first["variants"]],
            ["DIRECT", "TWO_STAGE_ALIGN_V2"],
        )
        self.assertEqual(
            [item["segment_roles"] for item in first["variants"]],
            [["ENDPOINT"], ["ALIGN_AT_CLEARANCE", "DESCEND_LOCKED"]],
        )
        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":")),
            json.dumps(second, sort_keys=True, separators=(",", ":")),
        )
        self.assertEqual(
            first["catalog_digest"],
            canonical_digest({key: value for key, value in first.items() if key != "catalog_digest"}),
        )

    def test_legacy_direct_candidate_remains_stable_and_plan_only(self):
        source = program()
        endpoint = next(step for step in source["steps"] if step["phase"] == "FINAL_APPROACH_LIN")["target"]
        compiled = compile_motion_program_v3(
            source, trajectory_variant_id="DIRECT", sampling_seed=7,
        )
        final = next(step for step in compiled["steps"] if step["phase"] == "FINAL_APPROACH_LIN")
        self.assertNotIn("target", final)
        self.assertEqual(final["segments"], [{
            "segment_index": 0, "segment_role": "ENDPOINT", "target": endpoint,
            "limits": next(step for step in source["steps"] if step["phase"] == "FINAL_APPROACH_LIN")["limits"],
        }])
        candidate, surface = compile_candidate("DIRECT")
        again, _ = compile_candidate("DIRECT")
        self.assertEqual(candidate, again)
        self.assertEqual([call[0] for call in surface.calls], ["ENDPOINT"])
        self.assertEqual((candidate["status"], candidate["authority_scope"], candidate["execution_authorized"]), ("PRECHECK_ELIGIBLE", "PLAN_ONLY", False))
        self.assertNotIn("observed", json.dumps(candidate).lower())
        self.assertEqual(surface.actions, 0)

    def test_legacy_two_stage_order_chain_and_exact_final_endpoint(self):
        source = program()
        endpoint = next(step for step in source["steps"] if step["phase"] == "FINAL_APPROACH_LIN")["target"]
        candidate, surface = compile_candidate("TWO_STAGE_ALIGN")
        self.assertEqual([call[0] for call in surface.calls], ["NEAR_GRASP", "FINAL_ALIGN"])
        self.assertEqual(surface.calls[1][2], [1.0] * 6)
        self.assertEqual(candidate["plan"]["steps"][0]["final_joint_state"], candidate["plan"]["steps"][1]["start_joint_state"])
        self.assertEqual(candidate["plan"]["steps"][-1]["target"], endpoint)
        self.assertEqual(candidate["plan"]["steps"][-1]["final_joint_state"], [2.0] * 6)
        self.assertEqual(
            [item["segment_index"] for item in candidate["plan_quality"]["metrics"]["phase_metrics"]],
            [0, 1],
        )
        self.assertEqual(
            candidate,
            validate_plan_only_candidate(
                candidate, motion_program_v2=source,
                constraint_check=surface.constraint_check,
                collision_check=surface.collision_check,
            ),
        )
        self.assertEqual(surface.actions, 0)

    def test_invalid_input_catalog_planner_and_hard_gates_fail_closed(self):
        invalid = program()
        invalid["schema_version"] = "fr5.motion_program.v1"
        surface = FakeSurface()
        with self.assertRaisesRegex(ContractError, "MOTION_PROGRAM_SCHEMA"):
            compile_plan_only_candidate(
                invalid, run_id="offline-1", trajectory_variant_id="DIRECT", sampling_seed=0,
                initial_joint_state=[0.0] * 6, plan_arm=surface.plan_arm,
                constraint_check=surface.constraint_check, collision_check=surface.collision_check,
            )
        self.assertEqual((surface.calls, surface.actions), ([], 0))

        catalog = legacy_phase_variant_catalog()
        catalog["variants"][0]["qualification_status"] = "CANDIDATE"
        with self.assertRaisesRegex(ContractError, "VARIANT_CATALOG_UNQUALIFIED"):
            compile_candidate("DIRECT", surface, catalog=catalog)
        self.assertEqual((surface.calls, surface.actions), ([], 0))

        surface.fail_role = "FINAL_ALIGN"
        with self.assertRaisesRegex(ContractError, "VARIANT_PLANNER_FAILED"):
            compile_candidate("TWO_STAGE_ALIGN", surface)
        self.assertEqual((len(surface.calls), surface.actions), (2, 0))

        for name, overrides, code in (
            ("constraint", {"constraint_check": lambda _plan: False}, "VARIANT_CONSTRAINT"),
            ("collision", {"collision_check": lambda _plan: False}, "VARIANT_COLLISION"),
        ):
            checked = FakeSurface()
            with self.subTest(name=name), self.assertRaisesRegex(ContractError, code):
                compile_candidate("DIRECT", checked, **overrides)
            self.assertEqual(checked.actions, 0)

    def test_endpoint_chain_and_canonical_digest_tampering_is_named(self):
        source = program()
        candidate, surface = compile_candidate("TWO_STAGE_ALIGN")
        endpoint = copy.deepcopy(candidate)
        endpoint["motion_program"]["steps"][2]["segments"][-1]["target"]["base_tcp"]["translation_m"][0] = 1.0
        with self.assertRaisesRegex(ContractError, "VARIANT_ENDPOINT"):
            validate_plan_only_candidate(
                endpoint, motion_program_v2=source,
                constraint_check=surface.constraint_check, collision_check=surface.collision_check,
            )

        chain = copy.deepcopy(candidate)
        chain["plan"]["steps"][1]["start_joint_state"][0] = 9.0
        with self.assertRaisesRegex(ContractError, "VARIANT_CHAIN"):
            validate_plan_only_candidate(
                chain, motion_program_v2=source,
                constraint_check=surface.constraint_check, collision_check=surface.collision_check,
            )

        digest = copy.deepcopy(candidate)
        digest["candidate_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ContractError, "VARIANT_CANDIDATE_DIGEST"):
            validate_plan_only_candidate(
                digest, motion_program_v2=source,
                constraint_check=surface.constraint_check, collision_check=surface.collision_check,
            )

        compiled = compile_motion_program_v3(source, trajectory_variant_id="DIRECT", sampling_seed=0)
        compiled["candidate_spec_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ContractError, "VARIANT_CANDIDATE_SPEC_DIGEST"):
            validate_motion_program_v3(compiled, motion_program_v2=source)
        self.assertEqual(surface.actions, 0)

    def test_legacy_two_stage_remains_plan_only_and_replayable(self):
        source = program()
        catalog = legacy_phase_variant_catalog()
        compiled = compile_motion_program_v3(
            source, trajectory_variant_id="TWO_STAGE_ALIGN",
            sampling_seed=0, catalog=catalog,
        )
        self.assertEqual(compiled["schema_version"], "fr5.motion_program.v3")
        self.assertEqual(
            compiled,
            validate_motion_program_v3(
                compiled, motion_program_v2=source, catalog=catalog,
            ),
        )
        candidate, surface = compile_candidate(
            "TWO_STAGE_ALIGN", catalog=catalog,
        )
        self.assertEqual(
            candidate["schema_version"],
            "data_factory.trajectory_variant_candidate.v1",
        )
        self.assertFalse(candidate["execution_authorized"])
        self.assertEqual(
            candidate,
            validate_plan_only_candidate(
                candidate, motion_program_v2=source,
                constraint_check=surface.constraint_check,
                collision_check=surface.collision_check,
                catalog=catalog,
            ),
        )


if __name__ == "__main__":
    unittest.main()
