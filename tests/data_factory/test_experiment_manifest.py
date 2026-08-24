from __future__ import annotations

import copy
import unittest

from tools.data_factory.experiment_manifest import (
    compile_base_condition,
    compile_fr5_hypothesis,
    compile_robot_start_pose,
    compile_rollout_manifest,
    compile_seed_manifest,
    validate_experiment_manifest,
)
from tools.data_factory.training_split import FR5_FEATURE_CONTRACT
from tools.fr5_data_factory import ContractError, canonical_digest


def digest(value: object) -> str:
    return canonical_digest(value)


def fixed_contract() -> dict:
    return {
        "schema_version": "data_factory.fr5_fixed_contract.v1",
        "robot_system_id": "fr5-r1",
        "task": "pickup_e2e",
        "instruction": "Pick up the object",
        "collection_profile_digest": digest("profile"),
        "feature_contract": copy.deepcopy(FR5_FEATURE_CONTRACT),
        "object_profile_id": "object-r1",
        "grasp_profile_id": "grasp-r1",
        "scene_digest": digest("scene"),
        "cell_calibration_id": "calibration-r1",
        "cell_calibration_digest": digest("calibration"),
        "motion_recipe": "DIRECT",
        "motion_recipe_digest": digest("direct"),
        "pregrasp_digest": digest("pregrasp"),
        "waypoint_digest": digest("waypoint"),
        "trajectory_digest": digest("trajectory"),
    }


def coverage(name: str, *, yaw: float) -> dict:
    fixed = fixed_contract()
    return {
        "task_schema_version": "data_factory.job.v1",
        "task": "pickup_e2e",
        "robot_system_id": "fr5-r1",
        "place_id": name,
        "cell_calibration_id": "calibration-r1",
        "cell_calibration_digest": fixed["cell_calibration_digest"],
        "yaw_deg": yaw,
        "x_mm": 10.0 if name == "cell-a" else 20.0,
        "y_mm": 0.0,
        "object_profile_id": "object-r1",
        "grasp_profile_id": "grasp-r1",
        "motion_recipe_digest": fixed["motion_recipe_digest"],
        "collection_profile_digest": fixed["collection_profile_digest"],
    }


def pose(name: str, offset: float = 0.0) -> dict:
    return compile_robot_start_pose(
        robot_start_pose_id=name,
        target_rad={joint: offset + index / 10 for index, joint in enumerate(("j1", "j2", "j3", "j4", "j5", "j6"))},
        tolerance_rad={joint: 0.01 for joint in ("j1", "j2", "j3", "j4", "j5", "j6")},
        home_candidate_digest=digest(["home", name]),
        qualification_digest=digest(["qualification", name]),
    )


def hypothesis() -> dict:
    a = compile_base_condition(
        coverage("cell-a", yaw=0.0),
        yaw_action_binding_digest=digest("action-yaw-0"),
        dual_view_observability_digest=digest("observable-yaw-0"),
    )
    b = compile_base_condition(
        coverage("cell-b", yaw=90.0),
        yaw_action_binding_digest=digest("action-yaw-90"),
        dual_view_observability_digest=digest("observable-yaw-90"),
    )
    poses = [pose("start-1"), pose("start-2", 0.1), pose("start-3", 0.2)]
    return compile_fr5_hypothesis(
        fixed_contract=fixed_contract(),
        base_conditions=[a, b],
        robot_start_poses=poses,
        allowed_pairs=[
            {"base_condition_digest": a["base_condition_digest"], "robot_start_pose_id": "start-1", "split_groups": ["TRAIN", "ID"]},
            {"base_condition_digest": a["base_condition_digest"], "robot_start_pose_id": "start-2", "split_groups": ["TRAIN", "ID"]},
            {"base_condition_digest": b["base_condition_digest"], "robot_start_pose_id": "start-3", "split_groups": ["OOD"]},
        ],
    )


def budget() -> dict[str, int]:
    return {
        "max_physical_episodes": 10,
        "max_rollout_trials": 10,
        "max_hil_prompts": 10,
        "max_reviews": 10,
        "max_pending_reviews": 10,
        "max_storage_bytes": 10_000,
    }


def program_budget() -> dict[str, int]:
    return {
        "max_rounds": 5, "used_rounds": 0,
        "max_total_physical_episodes": 100, "used_total_physical_episodes": 0,
        "max_total_rollout_trials": 30, "used_total_rollout_trials": 0,
        "max_total_hil_prompts": 100, "used_total_hil_prompts": 0,
        "max_total_reviews": 100, "used_total_reviews": 0,
        "max_pending_reviews": 20, "used_pending_reviews": 0,
        "max_total_storage_bytes": 100_000, "used_total_storage_bytes": 0,
    }


def slot(name: str, pair: tuple[str, str], group: str, repeat: int = 0) -> dict:
    return {
        "slot_id": name,
        "base_condition_digest": pair[0],
        "robot_start_pose_id": pair[1],
        "split_group": group,
        "repeat_index": repeat,
        "hil_prompts": 1,
        "reviews": 1,
        "pending_reviews": 0,
        "storage_bytes": 100,
    }


def pairs(value: dict) -> tuple[tuple[str, str], tuple[str, str], tuple[str, str]]:
    return tuple(
        (item["base_condition_digest"], item["robot_start_pose_id"])
        for item in value["allowed_pairs"]
    )  # type: ignore[return-value]


def seed_slots(value: dict) -> list[dict]:
    one, two, held_out = pairs(value)
    return [
        slot("train-1", one, "TRAIN"),
        slot("train-2", two, "TRAIN"),
        slot("id-1", one, "ID"),
        slot("ood-1", held_out, "OOD"),
    ]


class ExperimentManifestTests(unittest.TestCase):
    def test_hypothesis_is_finite_explicit_and_not_a_cartesian_product(self) -> None:
        value = hypothesis()
        self.assertEqual(len(value["base_conditions"]), 2)
        self.assertEqual(len(value["robot_start_poses"]), 3)
        self.assertEqual(len(value["allowed_pairs"]), 3)
        self.assertEqual(value["fixed_contract"]["motion_recipe"], "DIRECT")
        self.assertEqual(value["fixed_contract"]["feature_contract"]["camera_mapping"], {"up": "camera1", "side": "camera2"})

    def test_start_pose_has_exact_six_joint_contract_and_no_yaw(self) -> None:
        value = pose("start-1")
        self.assertEqual(set(value["target_rad"]), {"j1", "j2", "j3", "j4", "j5", "j6"})
        self.assertEqual(set(value["tolerance_rad"]), set(value["target_rad"]))
        self.assertNotIn("yaw_deg", value)
        value["yaw_deg"] = 0
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_START_POSE_FIELDS"):
            compile_fr5_hypothesis(
                fixed_contract=fixed_contract(),
                base_conditions=hypothesis()["base_conditions"],
                robot_start_poses=[value, pose("start-2"), pose("start-3")],
                allowed_pairs=hypothesis()["allowed_pairs"],
            )

    def test_nonfinite_or_unqualified_start_pose_fails(self) -> None:
        with self.assertRaises(ContractError):
            compile_robot_start_pose(
                robot_start_pose_id="bad", target_rad={joint: float("nan") for joint in ("j1", "j2", "j3", "j4", "j5", "j6")},
                tolerance_rad={joint: 0.01 for joint in ("j1", "j2", "j3", "j4", "j5", "j6")},
                home_candidate_digest=digest("home"), qualification_digest=digest("qualification"),
            )
        value = hypothesis()
        value["robot_start_poses"][0]["qualification_status"] = "CANDIDATE"
        value["robot_start_poses"][0]["start_pose_digest"] = canonical_digest({key: item for key, item in value["robot_start_poses"][0].items() if key != "start_pose_digest"})
        value["hypothesis_digest"] = canonical_digest({key: item for key, item in value.items() if key != "hypothesis_digest"})
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_START_POSE_UNQUALIFIED"):
            compile_seed_manifest(
                manifest_id="seed", hypothesis=value, slots=seed_slots(hypothesis()),
                randomization_seed=7, manifest_budget=budget(), program_budget=program_budget(),
            )

    def test_mixed_fixed_axis_and_unobservable_action_variation_fail(self) -> None:
        valid = hypothesis()
        mixed = copy.deepcopy(valid["base_conditions"])
        changed = coverage("cell-b", yaw=90.0)
        changed["grasp_profile_id"] = "grasp-r2"
        mixed[1] = compile_base_condition(
            changed, yaw_action_binding_digest=digest("action-yaw-90"),
            dual_view_observability_digest=digest("observable-yaw-90"),
        )
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_MIXED_FIXED_AXIS"):
            compile_fr5_hypothesis(
                fixed_contract=fixed_contract(), base_conditions=mixed,
                robot_start_poses=valid["robot_start_poses"], allowed_pairs=valid["allowed_pairs"],
            )

        aliased = copy.deepcopy(valid["base_conditions"])
        aliased[1] = compile_base_condition(
            coverage("cell-b", yaw=90.0),
            yaw_action_binding_digest=digest("different-action"),
            dual_view_observability_digest=aliased[0]["dual_view_observability_digest"],
        )
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_UNOBSERVABLE_POLICY_VARIATION"):
            compile_fr5_hypothesis(
                fixed_contract=fixed_contract(), base_conditions=aliased,
                robot_start_poses=valid["robot_start_poses"], allowed_pairs=valid["allowed_pairs"],
            )

    def test_seed_manifest_is_balanced_finite_and_deterministically_randomized(self) -> None:
        value = hypothesis()
        kwargs = {
            "manifest_id": "seed-r1", "hypothesis": value, "slots": seed_slots(value),
            "randomization_seed": 47, "manifest_budget": budget(),
            "program_budget": program_budget(),
        }
        first = compile_seed_manifest(**kwargs)
        second = compile_seed_manifest(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(first["planned_usage"]["physical_episodes"], 4)
        self.assertEqual(first["planned_usage"]["rollout_trials"], 0)
        self.assertEqual([item["order_index"] for item in first["slots"]], list(range(4)))
        self.assertEqual(first, validate_experiment_manifest(first, hypothesis=value))

    def test_rollout_manifest_uses_only_id_and_ood_trials(self) -> None:
        value = hypothesis()
        one, _, held_out = pairs(value)
        result = compile_rollout_manifest(
            manifest_id="rollout-r1", hypothesis=value,
            slots=[slot("id", one, "ID"), slot("ood", held_out, "OOD")],
            randomization_seed=9, manifest_budget=budget(), program_budget=program_budget(),
        )
        self.assertEqual(result["planned_usage"]["physical_episodes"], 2)
        self.assertEqual(result["planned_usage"]["rollout_trials"], 2)

    def test_disallowed_pair_and_duplicate_slot_fail_closed(self) -> None:
        value = hypothesis()
        slots = seed_slots(value)
        slots[-1]["robot_start_pose_id"] = "start-1"
        with self.assertRaisesRegex(ContractError, "MANIFEST_DISALLOWED_PAIR"):
            compile_seed_manifest(
                manifest_id="bad", hypothesis=value, slots=slots, randomization_seed=1,
                manifest_budget=budget(), program_budget=program_budget(),
            )
        duplicate = seed_slots(value)
        duplicate[1]["slot_id"] = duplicate[0]["slot_id"]
        with self.assertRaisesRegex(ContractError, "MANIFEST_SLOT_DUPLICATE"):
            compile_seed_manifest(
                manifest_id="bad", hypothesis=value, slots=duplicate, randomization_seed=1,
                manifest_budget=budget(), program_budget=program_budget(),
            )

    def test_unbalanced_implicit_train_cell_fails(self) -> None:
        value = hypothesis()
        slots = seed_slots(value)
        one, _, _ = pairs(value)
        slots.insert(1, slot("train-extra", one, "TRAIN", repeat=1))
        with self.assertRaisesRegex(ContractError, "MANIFEST_UNBALANCED_TRAIN"):
            compile_seed_manifest(
                manifest_id="bad", hypothesis=value, slots=slots, randomization_seed=1,
                manifest_budget=budget(), program_budget=program_budget(),
            )

    def test_manifest_and_cumulative_budgets_fail_when_oversubscribed_or_exhausted(self) -> None:
        value = hypothesis()
        small = budget()
        small["max_physical_episodes"] = 3
        with self.assertRaisesRegex(ContractError, "MANIFEST_BUDGET_OVERSUBSCRIBED"):
            compile_seed_manifest(
                manifest_id="bad", hypothesis=value, slots=seed_slots(value),
                randomization_seed=1, manifest_budget=small, program_budget=program_budget(),
            )
        exhausted = program_budget()
        exhausted["used_pending_reviews"] = exhausted["max_pending_reviews"]
        with self.assertRaisesRegex(ContractError, "PROGRAM_BUDGET_EXHAUSTED"):
            compile_seed_manifest(
                manifest_id="bad", hypothesis=value, slots=seed_slots(value),
                randomization_seed=1, manifest_budget=budget(), program_budget=exhausted,
            )

    def test_budget_counts_are_inputs_and_digest_tampering_fails(self) -> None:
        value = hypothesis()
        custom = budget()
        custom["max_storage_bytes"] = 401
        manifest = compile_seed_manifest(
            manifest_id="seed", hypothesis=value, slots=seed_slots(value),
            randomization_seed=2, manifest_budget=custom, program_budget=program_budget(),
        )
        self.assertEqual(manifest["manifest_budget"]["max_storage_bytes"], 401)
        manifest["manifest_budget"]["max_storage_bytes"] = 402
        with self.assertRaisesRegex(ContractError, "MANIFEST_DIGEST_MISMATCH"):
            validate_experiment_manifest(manifest, hypothesis=value)

    def test_invalid_inputs_never_create_artifact_or_execution_authority(self) -> None:
        value = hypothesis()
        slots = seed_slots(value)
        slots[0]["storage_bytes"] = -1
        with self.assertRaises(ContractError):
            compile_seed_manifest(
                manifest_id="bad", hypothesis=value, slots=slots, randomization_seed=1,
                manifest_budget=budget(), program_budget=program_budget(),
            )
        valid = compile_seed_manifest(
            manifest_id="seed", hypothesis=value, slots=seed_slots(value),
            randomization_seed=1, manifest_budget=budget(), program_budget=program_budget(),
        )
        self.assertEqual(valid["authority"], "NO_EXECUTION_AUTHORITY")


if __name__ == "__main__":
    unittest.main()
