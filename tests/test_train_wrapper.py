#!/usr/bin/env python3

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class CliWrapperTest(unittest.TestCase):
    def test_public_help_and_guards(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts/train_smolvla.sh"
        policy_script = root / "scripts/train_policy.sh"
        policy_help = subprocess.run([policy_script, "--help"], text=True, capture_output=True)
        self.assertEqual(policy_help.returncode, 0)
        self.assertIn("smolvla | act | vqbet-up", policy_help.stdout)
        self.assertIn("--profile", policy_help.stdout)

        help_result = subprocess.run([script, "--help"], text=True, capture_output=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--check-env", help_result.stdout)
        self.assertIn("--dry-run", help_result.stdout)
        self.assertIn("--dataset.eval_split", help_result.stdout)
        self.assertIn("--resume-from", help_result.stdout)
        self.assertIn("--eval_steps", help_result.stdout)
        self.assertIn("--save_freq", help_result.stdout)
        self.assertIn("--root", help_result.stdout)

        guard_result = subprocess.run([script, "missing-dataset"], text=True, capture_output=True)
        self.assertEqual(guard_result.returncode, 2)
        self.assertIn("--batch_size, --steps, --dataset.eval_split, --eval_steps, and --save_freq explicitly", guard_result.stderr)

        profile_guard = subprocess.run([policy_script, "missing-dataset"], text=True, capture_output=True)
        self.assertEqual(profile_guard.returncode, 2)
        self.assertIn("--profile is required", profile_guard.stderr)

        resume_guard = subprocess.run(
            [policy_script, "--resume-from", "/missing/checkpoint", "--dry-run"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(resume_guard.returncode, 2)
        self.assertIn("checkpoint must be under", resume_guard.stderr)

        with TemporaryDirectory() as directory:
            existing_output = Path(directory) / "existing"
            existing_output.mkdir()
            output_guard = subprocess.run(
                [
                    policy_script,
                    "--profile", "smolvla",
                    "--output", existing_output,
                    "missing-dataset",
                    "--batch_size=8", "--steps=200", "--dataset.eval_split=0.2",
                    "--eval_steps=200", "--save_freq=200",
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(output_guard.returncode, 2)
            self.assertIn("Output already exists", output_guard.stderr)

        managed_guard = subprocess.run(
            [
                policy_script,
                "--profile", "smolvla",
                "missing-dataset",
                "--batch_size=8", "--steps=200", "--dataset.eval_split=0.2",
                "--eval_steps=200", "--save_freq=200", "--save_checkpoint=false",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(managed_guard.returncode, 2)
        self.assertIn("--save_checkpoint is managed", managed_guard.stderr)

        validate_help = subprocess.run(
            [root / "scripts/validate_dataset.sh", "--help"], text=True, capture_output=True
        )
        self.assertEqual(validate_help.returncode, 0)
        self.assertIn("--visualize EPISODE_INDEX", validate_help.stdout)
        self.assertIn("--require-approved", validate_help.stdout)
        self.assertIn("--root", validate_help.stdout)

        with TemporaryDirectory() as directory:
            dataset = Path(directory) / "blocked" / "meta"
            dataset.mkdir(parents=True)
            (dataset / "quarantine.json").write_text("{}")
            quarantine_guard = subprocess.run(
                [root / "scripts/validate_dataset.sh", "--root", directory, "blocked"],
                text=True, capture_output=True,
            )
            self.assertEqual(quarantine_guard.returncode, 4)
            self.assertIn("Dataset is quarantined", quarantine_guard.stderr)
            direct_guard = subprocess.run(
                [root / ".venv/bin/python", root / "tools/validate_lerobot_dataset.py", dataset.parent],
                text=True, capture_output=True,
            )
            self.assertEqual(direct_guard.returncode, 1)
            self.assertIn("dataset is quarantined", direct_guard.stderr)
            (dataset / "quarantine.json").unlink()
            (dataset / "quarantine.json").symlink_to("missing-target")
            dangling_guard = subprocess.run(
                [root / "scripts/validate_dataset.sh", "--root", directory, "blocked"],
                text=True, capture_output=True,
            )
            self.assertEqual(dangling_guard.returncode, 4)
            direct_dangling_guard = subprocess.run(
                [root / ".venv/bin/python", root / "tools/validate_lerobot_dataset.py", dataset.parent],
                text=True, capture_output=True,
            )
            self.assertEqual(direct_dangling_guard.returncode, 1)
            self.assertIn("dataset is quarantined", direct_dangling_guard.stderr)

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
