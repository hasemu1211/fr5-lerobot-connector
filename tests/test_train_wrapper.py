#!/usr/bin/env python3

import subprocess
import unittest
from pathlib import Path


class CliWrapperTest(unittest.TestCase):
    def test_public_help_and_guards(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts/train_smolvla.sh"
        help_result = subprocess.run([script, "--help"], text=True, capture_output=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--check-env", help_result.stdout)
        self.assertIn("--dry-run", help_result.stdout)
        self.assertIn("--dataset.eval_split", help_result.stdout)
        self.assertIn("--root", help_result.stdout)

        guard_result = subprocess.run([script, "missing-dataset"], text=True, capture_output=True)
        self.assertEqual(guard_result.returncode, 2)
        self.assertIn("--batch_size, --steps, and --dataset.eval_split explicitly", guard_result.stderr)

        validate_help = subprocess.run(
            [root / "scripts/validate_dataset.sh", "--help"], text=True, capture_output=True
        )
        self.assertEqual(validate_help.returncode, 0)
        self.assertIn("--visualize EPISODE_INDEX", validate_help.stdout)
        self.assertIn("--require-approved", validate_help.stdout)
        self.assertIn("--root", validate_help.stdout)

        collect_help = subprocess.run(
            [root / "scripts/collect.sh", "--help"], text=True, capture_output=True
        )
        self.assertEqual(collect_help.returncode, 0)
        self.assertIn("--dry-run", collect_help.stdout)

        evaluate_help = subprocess.run(
            [root / "scripts/evaluate_smolvla.sh", "--help"], text=True, capture_output=True
        )
        self.assertEqual(evaluate_help.returncode, 0)
        self.assertIn("--episodes", evaluate_help.stdout)
        self.assertIn("offline", evaluate_help.stdout.lower())


if __name__ == "__main__":
    unittest.main()
