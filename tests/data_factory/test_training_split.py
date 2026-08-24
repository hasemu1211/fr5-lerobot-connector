from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools.data_factory.training_split import (
    FR5_FEATURE_CONTRACT,
    compile_training_split,
    validate_training_split,
)
from tools.fr5_data_factory import ContractError, canonical_digest


def digest(value: object) -> str:
    return canonical_digest(value)


def program_budget() -> dict[str, int]:
    return {
        "max_rounds": 5, "used_rounds": 0,
        "max_total_physical_episodes": 100, "used_total_physical_episodes": 0,
        "max_total_rollout_trials": 20, "used_total_rollout_trials": 0,
        "max_total_hil_prompts": 100, "used_total_hil_prompts": 0,
        "max_total_reviews": 100, "used_total_reviews": 0,
        "max_pending_reviews": 20, "used_pending_reviews": 0,
        "max_total_storage_bytes": 100_000, "used_total_storage_bytes": 0,
    }


def episode(index: int, condition: str, pose: str) -> dict[str, object]:
    return {
        "episode_index": index,
        "episode_ref_digest": digest(["episode", index]),
        "training_approval_digest": digest(["approval", index]),
        "base_condition_digest": digest(condition),
        "robot_start_pose_id": pose,
    }


def compile_valid() -> dict:
    return compile_training_split(
        dataset={
            "dataset_root_identity_digest": digest("root"),
            "repo_id": "local/fr5_connector",
            "dataset_info_features_digest": digest("info-features"),
            "total_episodes": 8,
            "total_frames": 800,
        },
        bindings={
            "collection_profile_digest": digest("profile"),
            "normalized_command_digest": digest("command"),
            "runtime_digest": digest("runtime"),
            "approved_episode_manifest_digest": digest("approved-manifest"),
        },
        episode_groups={
            "TRAIN": [episode(0, "a", "pose-1"), episode(1, "a", "pose-2")],
            "ID": [episode(2, "a", "pose-1")],
            "OOD": [episode(3, "b", "pose-3")],
        },
        program_budget=program_budget(),
    )


class TrainingSplitTests(unittest.TestCase):
    def test_legacy_v1_is_exact_read_only_validation(self) -> None:
        legacy = {
            "schema_version": 1,
            "repo_id": "local/fr5_connector",
            "total_episodes": 5,
            "total_frames": 50,
            "eval_split": 0.2,
            "eval_episodes": [4],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "fr5_training_split.json")
            original = json.dumps(legacy, indent=2) + "\n"
            path.write_text(original)
            self.assertEqual(validate_training_split(path), legacy)
            self.assertEqual(path.read_text(), original)

    def test_legacy_rejects_non_numeric_version_unknown_and_bad_indices(self) -> None:
        legacy = {
            "schema_version": 1, "repo_id": "local/fr5", "total_episodes": 2,
            "total_frames": 20, "eval_split": 0.5, "eval_episodes": [1],
        }
        for mutation in (
            lambda item: item.update(schema_version="1"),
            lambda item: item.update(extra=True),
            lambda item: item.update(eval_episodes=[1, 1]),
            lambda item: item.update(eval_split=float("nan")),
        ):
            value = copy.deepcopy(legacy)
            mutation(value)
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_training_split(value)

    def test_compiler_emits_only_digest_bound_v2(self) -> None:
        split = compile_valid()
        self.assertEqual(split["schema_version"], 2)
        self.assertEqual(split["feature_contract"], FR5_FEATURE_CONTRACT)
        self.assertEqual(split["evaluation_contract"]["schema_version"], "data_factory.evaluation_contract.v1")
        self.assertEqual(split["evaluation_contract"]["outcomes"], ["TERMINAL", "PARTIAL", "FAILURE"])
        self.assertEqual(split, validate_training_split(split))

    def test_v2_binds_every_provenance_and_split_digest(self) -> None:
        split = compile_valid()
        for path in (
            ("dataset", "dataset_info_features_digest"),
            ("bindings", "collection_profile_digest"),
            ("bindings", "normalized_command_digest"),
            ("bindings", "runtime_digest"),
            ("bindings", "approved_episode_manifest_digest"),
        ):
            changed = copy.deepcopy(split)
            changed[path[0]][path[1]] = digest(["changed", path])
            with self.subTest(path=path), self.assertRaisesRegex(ContractError, "SPLIT_DIGEST_MISMATCH"):
                validate_training_split(changed)

    def test_v2_rejects_feature_contract_changes(self) -> None:
        split = compile_valid()
        split["feature_contract"]["camera_mapping"] = {"up": "camera2", "side": "camera1"}
        with self.assertRaisesRegex(ContractError, "SPLIT_FEATURE_CONTRACT"):
            validate_training_split(split)

    def test_id_must_repeat_exact_training_factor_cell(self) -> None:
        split = compile_valid()
        split["episode_groups"]["ID"][0]["robot_start_pose_id"] = "pose-3"
        split["split_digest"] = canonical_digest({key: value for key, value in split.items() if key != "split_digest"})
        with self.assertRaisesRegex(ContractError, "SPLIT_ID_NOT_TRAIN_CELL"):
            validate_training_split(split)

    def test_ood_must_hold_out_a_full_condition_or_pose_value(self) -> None:
        split = compile_valid()
        split["episode_groups"]["OOD"][0].update(
            base_condition_digest=digest("a"), robot_start_pose_id="pose-2"
        )
        split["split_digest"] = canonical_digest({key: value for key, value in split.items() if key != "split_digest"})
        with self.assertRaisesRegex(ContractError, "SPLIT_OOD_NOT_FACTOR_HOLDOUT"):
            validate_training_split(split)

    def test_groups_are_explicit_nonempty_sorted_and_disjoint(self) -> None:
        split = compile_valid()
        cases = []
        empty = copy.deepcopy(split)
        empty["episode_groups"]["ID"] = []
        cases.append(empty)
        duplicate = copy.deepcopy(split)
        duplicate["episode_groups"]["ID"][0]["episode_index"] = 0
        cases.append(duplicate)
        unsorted = copy.deepcopy(split)
        unsorted["episode_groups"]["TRAIN"].reverse()
        cases.append(unsorted)
        for value in cases:
            value["split_digest"] = canonical_digest({key: item for key, item in value.items() if key != "split_digest"})
            with self.subTest(), self.assertRaises(ContractError):
                validate_training_split(value)

    def test_exhausted_program_budget_fails_closed(self) -> None:
        budget = program_budget()
        budget["used_rounds"] = budget["max_rounds"]
        with self.assertRaisesRegex(ContractError, "PROGRAM_BUDGET_EXHAUSTED"):
            compile_training_split(
                dataset=compile_valid()["dataset"],
                bindings=compile_valid()["bindings"],
                episode_groups=compile_valid()["episode_groups"],
                program_budget=budget,
            )

    def test_invalid_compile_has_no_output_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            groups = compile_valid()["episode_groups"]
            groups["OOD"][0]["base_condition_digest"] = "not-a-digest"
            with self.assertRaises(ContractError):
                compile_training_split(
                    dataset=compile_valid()["dataset"],
                    bindings=compile_valid()["bindings"],
                    episode_groups=groups,
                    program_budget=program_budget(),
                )
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
