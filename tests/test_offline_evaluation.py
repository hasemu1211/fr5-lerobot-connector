#!/usr/bin/env python3

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_train_wrapper import launch_fixture
from tools.data_factory.training_entrypoint import options, prepare_launch
from tools.evaluate_smolvla_offline import (
    admit_evaluation,
    main,
    normalize_checkpoint_path,
    parse_episode_indices,
)
from tools.fr5_training_profile import build_profile, policy_metadata


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def admitted_case(root: Path) -> tuple[SimpleNamespace, dict]:
    kwargs, _, _ = launch_fixture(root)
    info = json.loads((kwargs["dataset"] / "meta/info.json").read_text())
    output = root / "outputs/run"
    kwargs.update(profile="smolvla", argv=[
        "fixture-lerobot-train",
        *build_profile("smolvla", policy_metadata(info)),
        f"--dataset.root={kwargs['dataset']}",
        f"--dataset.repo_id={kwargs['repo_id']}",
        "--dataset.episodes=[0,2,3]",
        "--dataset.eval_split=0.34",
        f"--output_dir={output}",
        "--batch_size=2", "--steps=2", "--eval_steps=1", "--save_freq=1",
    ])
    split, receipt = prepare_launch(**kwargs)
    write_json(output / "fr5_training_split.json", split)
    write_json(output / "fr5_training_receipt.json", receipt)

    policy_dir = output / "checkpoints/000001/pretrained_model"
    state_dir = policy_dir.parent / "training_state"
    policy_dir.mkdir(parents=True)
    state_dir.mkdir()
    config = {
        "scheduler": None,
        "dataset": {
            "root": str(kwargs["dataset"]), "repo_id": kwargs["repo_id"],
            "episodes": [0, 2, 3], "eval_split": 0.34,
        },
        "policy": {},
        "rename_map": {},
    }
    for key, value in options(split["feature_contract"]["policy_argv"]).items():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        if key.startswith("--policy."):
            config["policy"][key.removeprefix("--policy.")] = parsed
        elif key == "--rename_map":
            config["rename_map"] = parsed
    write_json(policy_dir / "config.json", {})
    write_json(policy_dir / "train_config.json", config)
    (policy_dir / "model.safetensors").write_bytes(b"fixture-model")
    write_json(state_dir / "optimizer_param_groups.json", {})
    (state_dir / "optimizer_state.safetensors").write_bytes(b"fixture-optimizer")
    (state_dir / "rng_state.safetensors").write_bytes(b"fixture-rng")
    write_json(state_dir / "training_step.json", {"step": 1})
    args = SimpleNamespace(
        checkpoint=str(policy_dir), dataset=kwargs["dataset"], repo_id=kwargs["repo_id"],
        approved_inventory=kwargs["inventory"], episodes=None, batch_size=1,
        num_workers=0, max_batches=0, output=root / "evaluation.json",
    )
    return args, split


class OfflineEvaluationTest(unittest.TestCase):
    def test_episode_parser_and_checkpoint_normalization(self):
        self.assertEqual(parse_episode_indices("3,1,3"), [1, 3])
        with self.assertRaises(ValueError):
            parse_episode_indices("1,-2")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pretrained_model").mkdir()
            (root / "pretrained_model/config.json").write_text("{}")
            self.assertEqual(normalize_checkpoint_path(str(root)), str(root / "pretrained_model"))

    def test_admission_uses_exact_v3_heldout_partition(self):
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            args, split = admitted_case(Path(directory))
            admission = admit_evaluation(args)
            self.assertEqual(admission["episodes"], [2, 3])
            self.assertEqual(admission["split"]["split_digest"], split["split_digest"])

            args.episodes = "3"
            with self.assertRaisesRegex(ValueError, "exactly match"):
                admit_evaluation(args)

    def test_approval_dataset_and_partition_failures_are_closed(self):
        cases = (
            "missing-inventory", "stale-inventory", "modified-dataset",
            "training-overlap", "legacy-partition",
        )
        for kind in cases:
            with self.subTest(kind=kind), TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
                args, split = admitted_case(Path(directory))
                if kind == "missing-inventory":
                    args.approved_inventory.unlink()
                elif kind == "stale-inventory":
                    inventory = json.loads(args.approved_inventory.read_text())
                    inventory["inventory_digest"] = "sha256:" + "0" * 64
                    write_json(args.approved_inventory, inventory)
                elif kind == "modified-dataset":
                    (args.dataset / "changed-after-approval").write_text("changed\n")
                elif kind == "training-overlap":
                    split["train_episodes"] = [0, 2]
                    from tools.fr5_data_factory import canonical_digest
                    split["split_digest"] = canonical_digest({
                        key: value for key, value in split.items() if key != "split_digest"
                    })
                    write_json(Path(args.checkpoint).parents[2] / "fr5_training_split.json", split)
                else:
                    write_json(Path(args.checkpoint).parents[2] / "fr5_training_split.json", {
                        "schema_version": 1, "repo_id": args.repo_id,
                        "total_episodes": 4, "total_frames": 8,
                        "eval_split": 0.34, "eval_episodes": [2, 3],
                    })
                with self.assertRaises((OSError, ValueError)):
                    admit_evaluation(args)

    def test_dry_run_never_calls_inference_or_creates_output(self):
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            args, _ = admitted_case(Path(directory))
            argv = [
                "evaluate_smolvla_offline.py", args.checkpoint, str(args.dataset),
                "--repo-id", args.repo_id, "--approved-inventory", str(args.approved_inventory),
                "--output", str(args.output), "--dry-run",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch(
                "tools.evaluate_smolvla_offline.evaluate"
            ) as inference, mock.patch("builtins.print"):
                main()
            inference.assert_not_called()
            self.assertFalse(args.output.exists())

    def test_direct_script_dry_run_without_pythonpath_from_another_directory(self):
        script = Path(__file__).resolve().parents[1] / "tools/evaluate_smolvla_offline.py"
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            args, _ = admitted_case(root)
            result = subprocess.run(
                [sys.executable, str(script), args.checkpoint, str(args.dataset),
                 "--repo-id", args.repo_id, "--approved-inventory", str(args.approved_inventory),
                 "--output", str(args.output), "--dry-run"],
                cwd=root,
                env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("inference not run and output not created", result.stdout)
            self.assertFalse(args.output.exists())

    def test_public_wrapper_forwards_inventory_partition_and_dry_run(self):
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / ".venv/bin").mkdir(parents=True)
            shutil.copy2(project / "scripts/evaluate_smolvla.sh", root / "scripts/evaluate_smolvla.sh")
            trace = root / "trace"
            (root / "scripts/validate_dataset.sh").write_text(
                "#!/usr/bin/env bash\nprintf 'validate\\0%s\\0' \"$@\" >> \"$TRACE\"\n"
            )
            (root / ".venv/bin/python").write_text(
                "#!/usr/bin/env bash\nprintf 'python\\0%s\\0' \"$@\" >> \"$TRACE\"\n"
            )
            (root / "scripts/validate_dataset.sh").chmod(0o755)
            (root / ".venv/bin/python").chmod(0o755)
            inventory = root / "approval/inventory.json"
            result = subprocess.run([
                root / "scripts/evaluate_smolvla.sh",
                "--approved-inventory", inventory,
                "--root", root / "datasets", "--output", root / "report.json", "--dry-run",
                root / "checkpoint", "fixture-dataset", "--episodes", "2,3", "--batch-size", "4",
            ], env={**os.environ, "TRACE": str(trace), "FR5_REPO_ID": "tests/repo"},
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = trace.read_bytes().split(b"\0")
            self.assertIn(str(inventory).encode(), calls)
            self.assertIn(b"--require-approved", calls)
            self.assertIn(b"--episodes", calls)
            self.assertIn(b"2,3", calls)
            self.assertIn(b"--batch-size", calls)
            self.assertIn(b"4", calls)
            self.assertIn(b"--dry-run", calls)


if __name__ == "__main__":
    unittest.main()
