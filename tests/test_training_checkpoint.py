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


if __name__ == "__main__":
    unittest.main()
