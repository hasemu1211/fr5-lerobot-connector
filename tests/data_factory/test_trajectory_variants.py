import copy
import json
import unittest

try:
    from tests.data_factory.test_motion import motion
except ModuleNotFoundError:
    from test_motion import motion
from tools.data_factory.motion.trajectory_variants import (
    compile_motion_program_v3,
    compile_plan_only_candidate,
    phase_variant_catalog,
    validate_motion_program_v3,
    validate_phase_variant_catalog,
    validate_plan_only_candidate,
)
from tools.fr5_data_factory import ContractError, canonical_digest


def program():
    value = motion(True)
    approach = next(step for step in value["steps"] if step["phase"] == "APPROACH_STOP_LIN")
    for frame in ("base_tcp", "base_tool"):
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
    def test_catalog_is_exact_finite_and_byte_stable(self):
        first, second = phase_variant_catalog(), phase_variant_catalog()
        self.assertEqual(first, validate_phase_variant_catalog(first))
        self.assertEqual(
            [item["trajectory_variant_id"] for item in first["variants"]],
            ["DIRECT", "TWO_STAGE_ALIGN"],
        )
        self.assertEqual(
            [item["segment_roles"] for item in first["variants"]],
            [["ENDPOINT"], ["NEAR_GRASP", "FINAL_ALIGN"]],
        )
        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":")),
            json.dumps(second, sort_keys=True, separators=(",", ":")),
        )
        self.assertEqual(
            first["catalog_digest"],
            canonical_digest({key: value for key, value in first.items() if key != "catalog_digest"}),
        )

    def test_direct_is_one_exact_endpoint_segment_and_stable_candidate(self):
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

    def test_two_stage_order_chain_and_exact_final_endpoint(self):
        source = program()
        endpoint = next(step for step in source["steps"] if step["phase"] == "FINAL_APPROACH_LIN")["target"]
        candidate, surface = compile_candidate("TWO_STAGE_ALIGN")
        self.assertEqual([call[0] for call in surface.calls], ["NEAR_GRASP", "FINAL_ALIGN"])
        self.assertEqual(surface.calls[1][2], [1.0] * 6)
        self.assertEqual(candidate["plan"]["steps"][0]["final_joint_state"], candidate["plan"]["steps"][1]["start_joint_state"])
        self.assertEqual(candidate["plan"]["steps"][-1]["target"], endpoint)
        self.assertEqual(candidate["plan"]["steps"][-1]["final_joint_state"], [2.0] * 6)
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

        catalog = phase_variant_catalog()
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


if __name__ == "__main__":
    unittest.main()
