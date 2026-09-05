#!/usr/bin/env python3

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.validate_training_checkpoint import REQUIRED_TRAINING_STATE, normalize_policy_dir, validate_checkpoint


class TrainingCheckpointTest(unittest.TestCase):
    def test_complete_checkpoint_is_accepted_and_partial_is_rejected(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            policy = output / "checkpoints/000100/pretrained_model"
            state = policy.parent / "training_state"
            state.mkdir(parents=True)
            policy.mkdir()
            (policy / "config.json").write_text("{}")
            (policy / "model.safetensors").touch()
            (policy / "train_config.json").write_text(
                json.dumps({"dataset": {"repo_id": "local/fr5", "eval_split": 0.2}})
            )
            for name in REQUIRED_TRAINING_STATE:
                (state / name).write_text('{"step": 100}' if name == "training_step.json" else "")
            (output / "fr5_training_split.json").write_text(
                json.dumps({"repo_id": "local/fr5", "eval_split": 0.2})
            )

            self.assertEqual(normalize_policy_dir(policy.parent), policy)
            self.assertEqual(validate_checkpoint(policy, verify_dataset=False), (policy, output))
            (output / "fr5_training_split.json").rename(
                output.with_name(output.name + ".fr5_training_split.json.pending")
            )
            self.assertEqual(validate_checkpoint(policy, verify_dataset=False), (policy, output))
            (state / "optimizer_state.safetensors").unlink()
            with self.assertRaisesRegex(ValueError, "incomplete checkpoint"):
                validate_checkpoint(policy, verify_dataset=False)


class CurrentTrainingCheckpointTest(unittest.TestCase):
    def test_resume_rechecks_current_inventory_subset_and_same_count_provenance(self):
        from tests.test_train_wrapper import launch_fixture
        from tools.data_factory.training_entrypoint import prepare_launch, options
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            kwargs, _, _ = launch_fixture(root)
            split, receipt = prepare_launch(**kwargs)
            output = root / "outputs/run"
            policy = output / "checkpoints/000100/pretrained_model"
            state = policy.parent / "training_state"
            state.mkdir(parents=True)
            policy.mkdir()
            (policy / "config.json").write_text("{}")
            (policy / "model.safetensors").touch()
            policy_cfg = {}
            for option, value in options(split["feature_contract"]["policy_argv"]).items():
                if option.startswith("--policy."):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        pass
                    policy_cfg[option.removeprefix("--policy.")] = value
            config = {"dataset": {"root": str(kwargs["dataset"]), "repo_id": kwargs["repo_id"], "episodes": [0, 2, 3], "eval_split": 0.34}, "policy": policy_cfg}
            (policy / "train_config.json").write_text(json.dumps(config))
            for name in REQUIRED_TRAINING_STATE:
                (state / name).write_text('{"step": 100}' if name == "training_step.json" else "")
            (output / "fr5_training_split.json").write_text(json.dumps(split))
            (output / "fr5_training_receipt.json").write_text(json.dumps(receipt))
            self.assertEqual(validate_checkpoint(policy), (policy, output))
            config["dataset"]["episodes"] = [0, 1, 3]
            (policy / "train_config.json").write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "selection"):
                validate_checkpoint(policy)
            config["dataset"]["episodes"] = [0, 2, 3]
            (policy / "train_config.json").write_text(json.dumps(config))
            (kwargs["dataset"] / "meta/source_provenance/episode-000002.jsonl").write_text('{"frame_index":2}\n{"frame_index":1}\n')
            with self.assertRaisesRegex(ValueError, "TRAINING_DATASET_CHANGED"):
                validate_checkpoint(policy)


if __name__ == "__main__":
    unittest.main()
