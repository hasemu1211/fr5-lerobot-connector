#!/usr/bin/env python3

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class CliWrapperTest(unittest.TestCase):
    def test_public_help_and_guards(self):
        root = Path(__file__).resolve().parents[1]
        policy_script = root / "scripts/train_policy.sh"
        policy_help = subprocess.run([policy_script, "--help"], text=True, capture_output=True)
        self.assertEqual(policy_help.returncode, 0)
        self.assertIn("smolvla | act | vqbet-up", policy_help.stdout)
        self.assertIn("--profile", policy_help.stdout)

        help_result = subprocess.run(
            [policy_script, "--profile", "smolvla", "--help"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--check-env", help_result.stdout)
        self.assertIn("--dry-run", help_result.stdout)
        self.assertIn("--dataset.eval_split", help_result.stdout)
        self.assertIn("--resume-from", help_result.stdout)
        self.assertIn("--eval_steps", help_result.stdout)
        self.assertIn("--save_freq", help_result.stdout)
        self.assertIn("--root", help_result.stdout)

        guard_result = subprocess.run(
            [policy_script, "--profile", "smolvla", "missing-dataset"],
            text=True,
            capture_output=True,
        )
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

# These fixtures deliberately exercise the production CLI schema in temporary
# directories; they grant no authority over real datasets and never run training.
import copy
import json
from types import SimpleNamespace
from unittest import mock

from tests.data_factory.training_fixtures import launch_fixture, write_normalization_fixture, write_json, snapshot
from tools.data_factory import training_approval as approval
from tools.data_factory.training_entrypoint import approve, launch, prepare_launch, prepare_approvals
from tools.fr5_data_factory import ContractError, canonical_digest


class TrainingLaunchConnectionTest(unittest.TestCase):
    def _warm_start_case(self, root):
        from tests.test_offline_evaluation import admitted_case

        args, split = admitted_case(root)
        parent = Path(args.checkpoint)
        receipt = json.loads((parent.parents[2] / "fr5_training_receipt.json").read_text())
        output = root / "outputs/child"
        argv = [f"--policy.path={parent}" if arg.startswith("--policy.path=") else
                f"--output_dir={output}" if arg.startswith("--output_dir=") else arg
                for arg in receipt["normalized_argv"]]
        kwargs = dict(dataset=args.dataset, repo_id=args.repo_id, inventory=args.approved_inventory,
                      profile="smolvla", collection_profile=split["feature_contract"]["collection_profile_id"], argv=argv)
        return parent, output, kwargs

    def test_warm_start_binds_parent_and_child_reload_and_resume(self):
        import shutil
        from tools.validate_training_checkpoint import validate_checkpoint
        from tools.data_factory.training_entrypoint import resume_training

        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            parent, output, kwargs = self._warm_start_case(Path(directory))
            before = {p: p.read_bytes() for p in parent.parents[2].rglob("*") if p.is_file()}
            split, receipt = prepare_launch(**kwargs)
            self.assertEqual(receipt["initialization"]["checkpoint"], str(parent))
            self.assertEqual(receipt["initialization"]["mode"], "warm_start")
            self.assertEqual(receipt["initialization"]["reset"], ["optimizer", "scheduler", "rng", "sample_stream", "step"])
            output.mkdir(parents=True)
            write_json(output / "fr5_training_split.json", split)
            write_json(output / "fr5_training_receipt.json", receipt)
            child = output / "checkpoints/000001/pretrained_model"
            shutil.copytree(parent.parent, child.parent)
            config = json.loads((child / "train_config.json").read_text())
            config["policy"]["pretrained_path"] = str(parent)
            write_json(child / "train_config.json", config)
            self.assertEqual(validate_checkpoint(child), (child, output))
            with mock.patch("tools.data_factory.training_entrypoint.run_native_training", return_value=0) as native:
                self.assertEqual(resume_training(child), 0)
            self.assertIn("--resume=true", native.call_args.args[0])
            self.assertIn(f"--config_path={child / 'train_config.json'}", native.call_args.args[0])
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            config["policy"]["pretrained_path"] = "lerobot/smolvla_base"
            write_json(child / "train_config.json", config)
            with self.assertRaisesRegex(ValueError, "warm-start parent"):
                validate_checkpoint(child)

    def test_checkpoint_revalidates_each_warm_start_ancestor_once_per_call(self):
        import shutil
        from tools.validate_training_checkpoint import validate_checkpoint

        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            parent, output, kwargs = self._warm_start_case(root)
            for generation in range(2):
                split, receipt = prepare_launch(**kwargs)
                output.mkdir(parents=True)
                write_json(output / "fr5_training_split.json", split)
                write_json(output / "fr5_training_receipt.json", receipt)
                child = output / "checkpoints/000001/pretrained_model"
                shutil.copytree(parent.parent, child.parent)
                config = json.loads((child / "train_config.json").read_text())
                config["policy"]["pretrained_path"] = str(parent)
                write_json(child / "train_config.json", config)
                parent = child
                output = root / f"outputs/descendant-{generation}"
                kwargs["argv"] = [
                    f"--policy.path={parent}" if arg.startswith("--policy.path=") else
                    f"--output_dir={output}" if arg.startswith("--output_dir=") else arg
                    for arg in kwargs["argv"]
                ]
            # Three immutable checkpoints require three current admissions, not
            # recursively duplicated checks or a cache that outlives this call.
            with mock.patch("tools.data_factory.training_entrypoint.prepare_launch", wraps=prepare_launch) as admission:
                self.assertEqual(validate_checkpoint(child), (child, child.parents[2]))
                self.assertEqual(admission.call_count, 3)
                admission.reset_mock()
                self.assertEqual(validate_checkpoint(child), (child, child.parents[2]))
                self.assertEqual(admission.call_count, 3)
            receipt_path = child.parents[2] / "fr5_training_receipt.json"
            altered = json.loads(receipt_path.read_text())
            altered["normalization"]["stats"]["action"]["mean"][0] += 1
            write_json(receipt_path, altered)
            with self.assertRaises((ValueError, ContractError)):
                validate_checkpoint(child)

    def test_warm_start_rejects_mismatched_cohort_normalization_and_parent_output(self):
        from tools.validate_training_checkpoint import warm_start_binding

        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            parent, output, kwargs = self._warm_start_case(Path(directory))
            split, receipt = prepare_launch(**kwargs)
            altered = copy.deepcopy(split)
            altered["train_episodes"] = list(reversed(split["train_episodes"])) + [999]
            with self.assertRaisesRegex(ValueError, "must match"):
                warm_start_binding(parent, altered, receipt["normalization"])
            with self.assertRaisesRegex(ValueError, "must match"):
                warm_start_binding(parent, split, {})
            kwargs["argv"] = [f"--output_dir={parent.parents[2] / 'child'}" if arg.startswith("--output_dir=") else arg
                              for arg in kwargs["argv"]]
            with self.assertRaisesRegex(ContractError, "TRAINING_OUTPUT_INSIDE_PARENT"):
                prepare_launch(**kwargs)
            self.assertFalse(output.exists())

    def test_warm_start_revalidates_parent_before_any_output(self):
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            parent, output, kwargs = self._warm_start_case(Path(directory))
            calls = 0

            def changing_parent(**arguments):
                nonlocal calls
                result = prepare_launch(**arguments)
                if arguments["argv"] == kwargs["argv"]:
                    calls += 1
                    if calls == 1:
                        (parent / "model.safetensors").write_bytes(b"SYNTHETIC_CHANGED_PARENT")
                return result

            with mock.patch("tools.data_factory.training_entrypoint.prepare_launch", side_effect=changing_parent), \
                    mock.patch("tools.data_factory.training_entrypoint.run_native_training") as native:
                with self.assertRaisesRegex(ContractError, "TRAINING_INPUT_CHANGED"):
                    launch(**kwargs)
            native.assert_not_called()
            self.assertFalse(output.exists())
            self.assertFalse(list(output.parent.glob("child.*.pending")))

    def test_warm_start_rejects_forged_or_cyclic_initialization(self):
        from tools.data_factory.training_receipts import validate_launch_receipt, ReceiptError

        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            parent, _, kwargs = self._warm_start_case(Path(directory))
            split, receipt = prepare_launch(**kwargs)
            forged = copy.deepcopy(receipt)
            forged["initialization"]["checkpoint_artifact_digest"] = "sha256:" + "0" * 64
            forged["receipt_digest"] = canonical_digest({k: v for k, v in forged.items() if k != "receipt_digest"})
            with self.assertRaises(ReceiptError):
                validate_launch_receipt(forged, split)
            path = parent.parents[2] / "fr5_training_receipt.json"
            cyclic = json.loads(path.read_text())
            cyclic["normalized_argv"] = [f"--policy.path={parent}" if arg.startswith("--policy.path=") else arg
                                         for arg in cyclic["normalized_argv"]]
            write_json(path, cyclic)
            with self.assertRaisesRegex(ValueError, "cyclic warm-start"):
                prepare_launch(**kwargs)

    def test_warm_start_native_adapter_rechecks_parent_after_policy_loading(self):
        import sys
        from types import ModuleType
        from tools.data_factory.training_entrypoint import run_native_training

        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            parent, _, kwargs = self._warm_start_case(Path(directory))
            split, receipt = prepare_launch(**kwargs)
            native = ModuleType("lerobot.scripts.lerobot_train")
            native.make_train_eval_datasets = mock.Mock()

            def changed_during_load():
                (parent / "model.safetensors").write_bytes(b"SYNTHETIC_CHANGED_DURING_LOAD")
                return object()

            native.make_policy = changed_during_load
            native.main = lambda: native.make_policy()
            with mock.patch.dict(sys.modules, {"lerobot.scripts.lerobot_train": native}):
                with self.assertRaisesRegex(ContractError, "TRAINING_RUNTIME_WARM_START"):
                    run_native_training(kwargs["argv"], split, receipt)
            self.assertIs(native.make_policy, changed_during_load)

    def test_warm_start_public_wrapper_dry_run_reaches_native_admission(self):
        import os
        import shutil
        import sys

        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            parent, output, kwargs = self._warm_start_case(root)
            shell = root / "shell"
            (shell / "scripts").mkdir(parents=True)
            shutil.copy2(project / "scripts/train_policy.sh", shell / "scripts/train_policy.sh")
            # Disposable test checkout references the installed interpreter and
            # current product sources; no workspace/shared environment changes.
            (shell / ".venv").symlink_to(sys.prefix, target_is_directory=True)
            (shell / "tools").symlink_to(project / "tools", target_is_directory=True)
            (shell / "config").symlink_to(project / "config", target_is_directory=True)
            result = subprocess.run([
                shell / "scripts/train_policy.sh", "--warm-start-from", parent,
                "--profile", "smolvla", "--root", kwargs["dataset"].parent,
                "--output", output, "--approved-inventory", kwargs["inventory"],
                "--collection-profile", kwargs["collection_profile"], "--dry-run",
                kwargs["dataset"].name, "none", "--dataset.episodes=[0,2,3]",
                "--dataset.eval_split=0.34", "--batch_size=4", "--steps=100",
                "--eval_steps=100", "--save_freq=100",
                "--policy.optimizer_lr=5e-5", "--policy.scheduler_decay_lr=2.5e-6",
            ], env={**os.environ, "FR5_REPO_ID": kwargs["repo_id"], "PYTHONDONTWRITEBYTECODE": "1"},
                text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"--policy.path={parent}", result.stdout)
            self.assertIn("--policy.optimizer_lr=5e-5", result.stdout)
            self.assertIn("--policy.scheduler_decay_lr=2.5e-6", result.stdout)
            self.assertIn('"mode": "warm_start"', result.stdout)
            self.assertIn('optimizer, scheduler, RNG, sample stream and step reset', result.stdout)
            self.assertFalse(output.exists())

    def test_native_consumer_keeps_official_split_and_excludes_nontrain_statistics(self):
        import math
        import os
        import sys
        from types import ModuleType
        from lerobot.datasets import factory
        from tools.data_factory.training_entrypoint import run_native_training
        from tools.fr5_training_profile import training_normalization

        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            kwargs, _, _ = launch_fixture(Path(directory))
            kwargs["argv"] = [arg.replace("eval_split=0.34", "eval_split=0.2") for arg in kwargs["argv"]]
            split, receipt = prepare_launch(**kwargs)
            stats = receipt["normalization"]["stats"]
            self.assertEqual(split["train_episodes"], [0, 2])
            self.assertEqual(stats["action"]["mean"], [100.0] * 7)
            self.assertAlmostEqual(stats["action"]["std"][0], math.sqrt(10001))
            self.assertEqual(stats["action"]["count"], [4])
            before = snapshot(kwargs["dataset"])
            cfg = SimpleNamespace(dataset=SimpleNamespace(root=str(kwargs["dataset"]),
                repo_id=kwargs["repo_id"], episodes=[0, 2, 3], eval_split=0.2,
                streaming=False, use_imagenet_stats=True, revision=None, video_backend="pyav",
                image_transforms=SimpleNamespace(enable=False)), trainable_config=None, tolerance_s=1e-4)
            full = SimpleNamespace(episodes=[0, 2, 3], meta=SimpleNamespace(
                episodes={"tasks": split["episode_tasks"]}))
            created = []
            def dataset(*_args, **values):
                value = SimpleNamespace(episodes=values["episodes"], meta=SimpleNamespace(
                    stats={"action": {"mean": [99999.0] * 7}}, camera_keys=[]))
                created.append(value)
                return value
            native = ModuleType("lerobot.scripts.lerobot_train")
            native.make_train_eval_datasets = factory.make_train_eval_datasets
            def main():
                train, heldout = native.make_train_eval_datasets(cfg)
                self.assertEqual((train.episodes, heldout.episodes), ([0, 2], [3]))
                self.assertEqual(train.meta.stats["action"]["mean"].tolist(), [100.0] * 7)
                self.assertEqual(heldout.meta.stats["action"]["mean"].tolist(), [100.0] * 7)
            native.main = main
            original_argv = sys.argv
            with mock.patch.dict(sys.modules, {"lerobot.scripts.lerobot_train": native}), \
                    mock.patch.object(factory, "make_dataset", return_value=full), \
                    mock.patch.object(factory, "resolve_delta_timestamps", return_value={}), \
                    mock.patch.object(factory, "LeRobotDataset", side_effect=dataset):
                self.assertEqual(run_native_training(kwargs["argv"], split, receipt), 0)
                cfg.dataset.root = os.path.relpath(kwargs["dataset"], Path.cwd())
                self.assertEqual(run_native_training(kwargs["argv"], split, receipt), 0)
                cfg.dataset.root = str(kwargs["dataset"] / "different-root")
                with self.assertRaisesRegex(ContractError, "TRAINING_RUNTIME_DATASET"):
                    run_native_training(kwargs["argv"], split, receipt)
                cfg.dataset.root = str(kwargs["dataset"])
                cfg.dataset.episodes = [0, 1, 3]
                with self.assertRaisesRegex(ContractError, "TRAINING_RUNTIME_DATASET"):
                    run_native_training(kwargs["argv"], split, receipt)
            self.assertIs(sys.argv, original_argv)
            self.assertIs(native.make_train_eval_datasets, factory.make_train_eval_datasets)
            self.assertEqual(len(created), 4)
            self.assertEqual(snapshot(kwargs["dataset"]), before)
            self.assertEqual(training_normalization(split), receipt["normalization"])

    def test_normalization_rejects_missing_or_malformed_train_metadata(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from tools.fr5_training_profile import training_normalization

        for kind in ("missing", "shape", "nonfinite", "count"):
            with self.subTest(kind=kind), TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
                kwargs, _, _ = launch_fixture(Path(directory))
                split, _ = prepare_launch(**kwargs)
                path = next((kwargs["dataset"] / "meta/episodes").rglob("*.parquet"))
                rows = pq.read_table(path).to_pylist()
                if kind == "missing":
                    rows = rows[1:]
                else:
                    key = "stats/action/count" if kind == "count" else "stats/action/mean"
                    rows[0][key] = {"shape": [0.0], "nonfinite": [float("nan")] * 7, "count": [99]}[kind]
                pq.write_table(pa.Table.from_pylist(rows), path)
                with self.assertRaisesRegex(ContractError, "TRAINING_NORMALIZATION"):
                    training_normalization(split)

    def test_selected_subset_matches_receipt_and_dry_run_is_nonmutating(self):
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            kwargs, _, _ = launch_fixture(root)
            before = snapshot(root)
            split, receipt = prepare_launch(**kwargs)
            self.assertEqual(split["selected_episodes"], [0, 2, 3])
            self.assertEqual(split["train_episodes"], [0])
            self.assertEqual(split["eval_episodes"], [2, 3])
            self.assertEqual(receipt["split_digest"], split["split_digest"])
            self.assertEqual(receipt["feature_contract"], split["feature_contract"])
            self.assertEqual(receipt["feature_contract"]["camera_profile"], "up-wrist")
            self.assertEqual(receipt["feature_contract"]["task"], "pick_place")
            self.assertEqual(receipt["status"], "ADMITTED_NOT_TRAINED")
            from tools.data_factory.training_receipts import validate_launch_receipt, ReceiptError
            forged = copy.deepcopy(receipt)
            forged["eval_episodes"] = [3]
            with self.assertRaises(ReceiptError):
                validate_launch_receipt(forged, split)
            runner = mock.Mock()
            with mock.patch("builtins.print"):
                self.assertEqual(launch(**kwargs, dry_run=True, runner=runner), 0)
            runner.assert_not_called()
            self.assertEqual(snapshot(root), before)

    def test_missing_stale_forged_and_changed_provenance_fail_before_runner_or_output(self):
        for kind in ("missing", "legacy", "forged", "same-count-provenance", "same-count-payload", "selection", "duplicate", "synthetic", "camera-profile", "remote", "environment"):
            with self.subTest(kind=kind), TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
                root = Path(directory)
                kwargs, _, inventory = launch_fixture(root)
                if kind == "missing":
                    kwargs["inventory"].unlink()
                elif kind == "legacy":
                    write_json(kwargs["inventory"], {"approved": True})
                elif kind == "forged":
                    inventory["episodes"][0]["training_approval"]["artifact_digest"] = "sha256:" + "0" * 64
                    inventory["inventory_digest"] = canonical_digest({k: v for k, v in inventory.items() if k != "inventory_digest"})
                    write_json(kwargs["inventory"], inventory)
                elif kind == "same-count-provenance":
                    (kwargs["dataset"] / "meta/source_provenance/episode-000002.jsonl").write_text('{"frame_index":9}\n{"frame_index":1}\n')
                elif kind == "same-count-payload":
                    (kwargs["dataset"] / "unchanged.marker").write_text("different bytes\n")
                elif kind == "selection":
                    kwargs["argv"] = [a.replace("[0,2,3]", "[0,1,3]") for a in kwargs["argv"]]
                elif kind == "duplicate":
                    kwargs["argv"].append("--dataset.episodes=[0,1,3]")
                elif kind == "synthetic":
                    inventory["scope"] = approval.SYNTHETIC_SCOPE
                    write_json(kwargs["inventory"], inventory)
                elif kind == "remote":
                    kwargs["argv"].append("--job.target=remote")
                elif kind == "environment":
                    kwargs["argv"].append("--env.type=pusht")
                else:
                    kwargs["collection_profile"] = "fr5-up-side-rgb-30hz-v1"
                before = snapshot(root)
                runner = mock.Mock()
                with self.assertRaises((ValueError, OSError)):
                    launch(**kwargs, runner=runner)
                runner.assert_not_called()
                self.assertEqual(snapshot(root), before)
                self.assertFalse((root / "outputs").exists())

    def test_launch_writes_bound_receipts_only_after_admission_with_fixture_runner(self):
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            kwargs, _, _ = launch_fixture(root)
            before = snapshot(kwargs["dataset"])
            def fake_runner(argv, check):
                self.assertEqual(argv, kwargs["argv"])
                (root / "outputs/run").mkdir()
                return SimpleNamespace(returncode=9)
            self.assertEqual(launch(**kwargs, runner=fake_runner), 9)
            split = json.loads((root / "outputs/run/fr5_training_split.json").read_text())
            receipt = json.loads((root / "outputs/run/fr5_training_receipt.json").read_text())
            self.assertEqual(receipt["split_digest"], split["split_digest"])
            self.assertEqual(snapshot(kwargs["dataset"]), before)

    def test_delegated_fresh_launch_runs_the_runner_in_cache_only_mode(self):
        import os
        from huggingface_hub import constants as hub_constants

        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            dataset = root / "dataset"
            dataset.mkdir()
            output = root / "outputs/run"
            inventory = root / "inventory.json"
            split = {
                "selected_episodes": [0],
                "dataset_identity": {"dataset_root": str(dataset)},
                "repo_id": "tests/local",
            }
            receipt = {
                "status": "ADMITTED_NOT_TRAINED",
                "approved_inventory_path": str(inventory),
            }
            argv = ["fixture-lerobot-train", f"--output_dir={output}"]

            def runner(_argv, check):
                self.assertFalse(check)
                self.assertTrue(hub_constants.is_offline_mode())
                self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")
                output.mkdir(parents=True)
                return SimpleNamespace(returncode=0)

            with mock.patch(
                "tools.data_factory.training_entrypoint.prepare_launch",
                return_value=(split, receipt),
            ), mock.patch.object(
                approval, "validate_current_training_inventory", return_value={"episodes": []},
            ), mock.patch.object(
                approval, "inventory_local_training_delegation", return_value={"delegated": True},
            ):
                self.assertEqual(launch(
                    dataset=dataset, repo_id="tests/local", inventory=inventory,
                    profile="smolvla", collection_profile="fixture", argv=argv, runner=runner,
                ), 0)

    def test_fresh_native_launch_reaches_trainer_for_human_and_delegated_inventory(self):
        import os

        for delegated in (False, True):
            with self.subTest(delegated=delegated), TemporaryDirectory(
                prefix="SYNTHETIC_TEST_ONLY-",
            ) as directory:
                root = Path(directory)
                dataset = root / "dataset"
                dataset.mkdir()
                inventory = root / "inventory.json"
                split = {
                    "selected_episodes": [0],
                    "dataset_identity": {"dataset_root": str(dataset)},
                    "repo_id": "tests/local",
                }
                receipt = {"approved_inventory_path": str(inventory)}
                argv = ["fixture-lerobot-train", f"--output_dir={root / 'run'}"]

                def native(_argv, _split, _receipt):
                    self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "1" if delegated else None)
                    return 0

                with mock.patch(
                    "tools.data_factory.training_entrypoint.prepare_launch",
                    return_value=(split, receipt),
                ), mock.patch.object(
                    approval, "validate_current_training_inventory", return_value={"episodes": []},
                ), mock.patch.object(
                    approval, "inventory_local_training_delegation",
                    return_value={"delegated": True} if delegated else None,
                ), mock.patch(
                    "tools.data_factory.training_entrypoint._run_native_training",
                    side_effect=native,
                ) as native_runner:
                    self.assertEqual(launch(
                        dataset=dataset, repo_id="tests/local", inventory=inventory,
                        profile="smolvla", collection_profile="fixture", argv=argv,
                    ), 0)
                native_runner.assert_called_once_with(argv, split, receipt)

    def test_delegated_request_launch_recovers_same_input_without_rerun(self):
        from tools.data_factory.training_entrypoint import run_delegated_request

        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            kwargs, request, _ = launch_fixture(root)
            authority_root = root / "delegated"
            authority_root.mkdir()
            approval_output = authority_root / "batch"
            approval_output.mkdir()
            delegation = {
                "schema_version": approval.DELEGATION_SCHEMA,
                "delegation_id": "synthetic-launch-r1",
                "scope": approval.PRODUCTION_SCOPE,
                "delegated_by": "workspace-user",
                "authorized_actor": "local-training-owner",
                "authorization_source_ref": "synthetic-test-only",
                "dataset": {"repo_id": request["repo_id"], "dataset_root": request["dataset_root"]},
                "output_root": str(authority_root),
                "profiles": ["smolvla"],
                "limits": {"max_steps": 2, "max_batch_size": 2, "max_checkpoints": 2},
                "authority": copy.deepcopy(approval.DELEGATION_AUTHORITY),
            }
            delegation_path = root / "delegation.json"
            write_json(delegation_path, delegation)
            output = authority_root / "run"
            calls = []
            retry_errors = []
            retry_call = None

            def runner(_argv, check):
                self.assertFalse(check)
                calls.append(True)
                output.mkdir(parents=True)
                (output / "checkpoints/000001/pretrained_model").mkdir(parents=True)
                if retry_call is not None:
                    try:
                        from concurrent.futures import ThreadPoolExecutor
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            pool.submit(retry_call).result(timeout=15)
                    except ContractError as error:
                        retry_errors.append(str(error))
                self.assertEqual(validated, [])
                self.assertEqual(evaluated, [])
                return SimpleNamespace(returncode=0)

            validated, evaluated = [], []
            def validator(checkpoint):
                validated.append(checkpoint)
                return checkpoint, output
            def evaluator(checkpoint, dataset, repo_id, inventory_path, output_dir):
                evaluated.append((checkpoint, dataset, repo_id, inventory_path, output_dir))
                return {"evaluation_complete": True, "evidence_scope": "synthetic_injected_native_consumer"}

            retry_call = lambda: run_delegated_request(
                request, approval_output=approval_output,
                authorized_actor="local-training-owner", delegation_path=delegation_path,
                profile="smolvla", collection_profile=kwargs["collection_profile"],
                output=output, steps=2, batch_size=2, eval_split=0.34, eval_steps=1,
                save_freq=1, runner=runner, checkpoint_validator=validator, evaluator=evaluator,
            )

            # Interrupt the real publisher after its first exclusive write.
            original_write = approval._write_exclusive
            def interrupted_write(path, value, code):
                original_write(path, value, code)
                raise OSError("injected publication interruption")
            with mock.patch.object(approval, "_write_exclusive", side_effect=interrupted_write):
                with self.assertRaisesRegex(OSError, "injected publication"):
                    retry_call()
            partial = snapshot(approval_output)
            self.assertFalse((approval_output / "training_approved.json").exists())
            with self.assertRaisesRegex(ContractError, "TRAINING_AUTHORIZATION_RECOVERY_INCOMPLETE"):
                retry_call()
            self.assertEqual(snapshot(approval_output), partial)
            self.assertEqual(calls, [])
            approval_output = authority_root / "complete-batch"
            approval_output.mkdir()

            first = run_delegated_request(
                request, approval_output=approval_output,
                authorized_actor="local-training-owner", delegation_path=delegation_path,
                profile="smolvla", collection_profile=kwargs["collection_profile"],
                output=output, steps=2, batch_size=2, eval_split=0.34, eval_steps=1,
                save_freq=1, runner=runner, checkpoint_validator=validator, evaluator=evaluator,
            )
            second = run_delegated_request(
                request, approval_output=approval_output,
                authorized_actor="local-training-owner", delegation_path=delegation_path,
                profile="smolvla", collection_profile=kwargs["collection_profile"],
                output=output, steps=2, batch_size=2, eval_split=0.34, eval_steps=1,
                save_freq=1, runner=runner, checkpoint_validator=validator, evaluator=evaluator,
            )
            self.assertEqual(first["status"], "EVALUATED_CHECKPOINT")
            self.assertEqual(first["returncode"], 0)
            self.assertEqual(second["status"], "EVALUATED_EXISTING_OUTPUT")
            self.assertEqual(calls, [True])
            self.assertEqual(len(retry_errors), 1)
            self.assertIn("TRAINING_OUTPUT_PENDING", retry_errors[0])
            self.assertEqual(first["evaluation"]["evidence_scope"], "synthetic_injected_native_consumer")
            self.assertEqual(len(validated), 2)
            self.assertEqual(len(evaluated), 2)
            pending = Path(str(output) + ".fr5_training_split.json.pending")
            pending.write_text("active")
            with self.assertRaisesRegex(ContractError, "TRAINING_OUTPUT_PENDING"):
                run_delegated_request(
                    request, approval_output=approval_output,
                    authorized_actor="local-training-owner", delegation_path=delegation_path,
                    profile="smolvla", collection_profile=kwargs["collection_profile"],
                    output=output, steps=2, batch_size=2, eval_split=0.34, eval_steps=1,
                    save_freq=1, runner=runner, checkpoint_validator=validator, evaluator=evaluator,
                )
            pending.unlink()
            saved_receipt = json.loads((output / "fr5_training_receipt.json").read_text())
            saved_receipt["normalized_argv"] = ["different-input"]
            write_json(output / "fr5_training_receipt.json", saved_receipt)
            with self.assertRaisesRegex(ContractError, "TRAINING_OUTPUT_RECOVERY_MISMATCH"):
                run_delegated_request(
                    request, approval_output=approval_output,
                    authorized_actor="local-training-owner", delegation_path=delegation_path,
                    profile="smolvla", collection_profile=kwargs["collection_profile"],
                    output=output, steps=2, batch_size=2, eval_split=0.34, eval_steps=1,
                    save_freq=1, runner=runner, checkpoint_validator=validator, evaluator=evaluator,
                )
            self.assertEqual(calls, [True])

            # Failure before output creation retains receipts and cannot rerun.
            failed_output = authority_root / "failed"
            failure_runner = mock.Mock(return_value=SimpleNamespace(returncode=7))
            failure_kwargs = dict(
                approval_output=approval_output, authorized_actor="local-training-owner",
                delegation_path=delegation_path, profile="smolvla",
                collection_profile=kwargs["collection_profile"], output=failed_output,
                steps=2, batch_size=2, eval_split=0.34, eval_steps=1, save_freq=1,
                runner=failure_runner, checkpoint_validator=validator, evaluator=evaluator,
            )
            failed = run_delegated_request(request, **failure_kwargs)
            self.assertEqual(failed["status"], "TRAINING_FAILED")
            self.assertEqual(failed["returncode"], 7)
            with self.assertRaisesRegex(ContractError, "TRAINING_OUTPUT_PENDING"):
                run_delegated_request(request, **failure_kwargs)
            failure_runner.assert_called_once()

            # A crash between the two final manifest renames is not settled.
            interrupted_output = authority_root / "interrupted"
            def interrupted_runner(argv, check):
                (interrupted_output / "checkpoints/000001/pretrained_model").mkdir(parents=True)
                return SimpleNamespace(returncode=0)
            recovery_kwargs = {**failure_kwargs, "output": interrupted_output,
                               "runner": mock.Mock(side_effect=interrupted_runner)}
            original_rename = Path.rename
            def interrupted_rename(path, target):
                original_rename(path, target)
                raise OSError("injected final publication interruption")
            with mock.patch.object(Path, "rename", interrupted_rename):
                with self.assertRaisesRegex(OSError, "injected final publication"):
                    run_delegated_request(request, **recovery_kwargs)
            before = snapshot(authority_root)
            with self.assertRaisesRegex(ContractError, "TRAINING_OUTPUT_PENDING"):
                run_delegated_request(request, **recovery_kwargs)
            recovery_kwargs["runner"].assert_called_once()
            self.assertEqual(snapshot(authority_root), before)

    def test_planning_cohort_survives_remapping_without_authority(self):
        from tools.data_factory.training_entrypoint import prepare_evaluation_cohort, revalidate_evaluation_cohort
        from tools.data_factory.training_split import resolve_evaluation_cohort, validate_evaluation_cohort
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            _, request, _ = launch_fixture(root)
            request_path = root / "request.json"
            write_json(request_path, request)
            before = snapshot(root)
            value = prepare_evaluation_cohort(request_path, evidence_directory=root, eval_fraction=.34)
            self.assertEqual(snapshot(root), before)
            self.assertIs(value["training_authority"], False)
            self.assertNotIn("approved_episode_inventory_digest", value)
            path = root / "cohort.json"
            write_json(path, value)
            self.assertEqual(revalidate_evaluation_cohort(path), value)
            origins = {10: value["train"][0], 20: value["eval"][0], 30: value["eval"][1]}
            self.assertEqual(resolve_evaluation_cohort(value, origins), ([10], [20, 30]))
            extra = {**value["train"][0], "episode_index": 99}
            self.assertEqual(resolve_evaluation_cohort(value, {**origins, 0: extra}), ([0, 10], [20, 30]))
            with self.assertRaisesRegex(ContractError, "COHORT_OVERLAP"):
                resolve_evaluation_cohort(value, {**origins, 40: value["eval"][0]})
            with self.assertRaisesRegex(ContractError, "COHORT_MISSING_HELDOUT"):
                resolve_evaluation_cohort(value, {10: value["train"][0]})
            bad = copy.deepcopy(value)
            bad["train"].append(bad["eval"][0])
            bad["cohort_digest"] = canonical_digest({k:v for k,v in bad.items() if k != "cohort_digest"})
            with self.assertRaisesRegex(ContractError, "COHORT_OVERLAP"):
                validate_evaluation_cohort(bad)
            request_path.write_text(request_path.read_text() + " ")
            with self.assertRaisesRegex(ContractError, "COHORT_SOURCE_CHANGED"):
                revalidate_evaluation_cohort(path)

    def test_mapped_derived_preview_separates_original_review(self):
        from tools.data_factory.training_entrypoint import PreparedApprovalBatch
        original = {"schema_version": approval.LEDGER_PROVENANCE_SCHEMA, "episode_index": 7}
        derived = {"schema_version": approval.DERIVED_PROVENANCE_SCHEMA, "episode_index": 7,
                   "parent": {"dataset_identity": {"dataset_id": "original"}, "provenance": original},
                   "curator_review": {"coverage": {"population_frames": 100, "reviewed_frames": 4}}}
        provenance = {"schema_version": approval.MAPPED_PROVENANCE_SCHEMA,
                      "parent": {"dataset_identity": {"dataset_id": "derived"}, "provenance": derived},
                      "mapping": {"synthetic": True}}
        snapshot_value = {"dataset": {"dataset_id": "mapped"}, "batch_digest": "synthetic",
            "drafts": [{"approval_arguments": {"episode_id": "synthetic", "episode_index": 0},
                        "reviewer_id": "synthetic-reviewer", "provenance": provenance}]}
        episode = PreparedApprovalBatch(json.dumps(snapshot_value)).preview["episodes"][0]
        self.assertEqual(episode["semantic_status"], "NOT_ASSERTED")
        self.assertEqual(episode["parent_semantic_status"], "NOT_ASSERTED")
        self.assertEqual(episode["parent_dataset_identity"], {"dataset_id": "derived"})
        self.assertEqual(episode["original_parent_semantic_status"], "PASS")
        self.assertEqual(episode["original_parent_dataset_identity"], {"dataset_id": "original"})
        self.assertEqual(episode["original_source_episode_index"], 7)
        self.assertEqual(episode["curator_review"], derived["curator_review"])
        provenance["parent"] = derived["parent"]
        raw = PreparedApprovalBatch(json.dumps(snapshot_value)).preview["episodes"][0]
        self.assertEqual(raw["parent_semantic_status"], "PASS")
        self.assertNotIn("original_parent_semantic_status", raw)
        self.assertNotIn("curator_review", raw)

    def test_public_cohort_union_preserves_roles_and_revalidates_sources(self):
        import sys
        from tools.data_factory import training_entrypoint as entry
        from tools.data_factory.training_split import resolve_evaluation_cohort, compose_evaluation_cohorts
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            paths = []
            for name in ("old", "new"):
                source = root / name
                source.mkdir()
                _, request, _ = launch_fixture(source)
                rp = source / "request.json"
                write_json(rp, request)
                cp = source / "cohort.json"
                before = snapshot(source)
                value = entry.prepare_evaluation_cohort(rp, evidence_directory=source,
                    **({"eval_fraction": .34} if name == "old" else {"eval_episodes": [2]}))
                self.assertEqual(snapshot(source), before)
                if name == "new":
                    self.assertEqual(value["evaluation_episode_indices"], [2])
                    for invalid in ([], [1], [2, 2], [0, 2, 3], [True], [-1]):
                        with self.subTest(invalid=invalid), self.assertRaisesRegex(ContractError, "COHORT_PARTITION"):
                            entry.prepare_evaluation_cohort(rp, evidence_directory=source, eval_episodes=invalid)
                else:
                    self.assertNotIn("evaluation_episode_indices", value)
                    self.assertEqual(set(value), {"schema_version", "training_authority", "request",
                        "dataset_identity", "eval_fraction", "train", "eval", "cohort_digest"})
                write_json(cp, value)
                paths.append(cp)
            authority_before = {str(p): p.read_bytes() for p in root.rglob("training_approved.json")}
            output = root / "union.json"
            argv = ["training_entrypoint", "compose-cohorts", "--cohort", str(paths[0]),
                    "--cohort", str(paths[1]), "--output", str(output)]
            with mock.patch.object(sys, "argv", argv):
                entry.main()
            value = entry.revalidate_evaluation_cohort(output)
            self.assertIs(value["training_authority"], False)
            origins = {i: row for i, row in enumerate(value["train"] + value["eval"])}
            train, evaluation = resolve_evaluation_cohort(value, origins)
            self.assertEqual([origins[i] for i in train], value["train"])
            self.assertEqual([origins[i] for i in evaluation], value["eval"])
            extra = {**value["train"][0], "episode_index": 100}
            self.assertIn(100, resolve_evaluation_cohort(value, {**origins, 100: extra})[0])
            with self.assertRaisesRegex(ContractError, "COHORT_OVERLAP"):
                compose_evaluation_cohorts([value["cohorts"][0], value["cohorts"][0]])
            for changed_content in (False, True):
                conflicting = copy.deepcopy(value["cohorts"][0])
                conflicting["train"], conflicting["eval"] = conflicting["eval"], conflicting["train"]
                if changed_content:
                    conflicting["eval"][0]["episode_content_digest"] = "sha256:" + "f" * 64
                conflicting["cohort_digest"] = canonical_digest({k: v for k, v in conflicting.items()
                                                                  if k != "cohort_digest"})
                with self.subTest(changed_content=changed_content), self.assertRaisesRegex(ContractError, "COHORT_OVERLAP"):
                    compose_evaluation_cohorts([value["cohorts"][0], conflicting])
            with self.assertRaisesRegex(ContractError, "COHORT_MISSING_HELDOUT"):
                resolve_evaluation_cohort(value, {i: origins[i] for i in train})
            rp.write_text(rp.read_text() + " ")
            with self.assertRaisesRegex(ContractError, "COHORT_SOURCE_CHANGED"):
                entry.revalidate_evaluation_cohort(output)
            self.assertEqual({str(p): p.read_bytes() for p in root.rglob("training_approved.json")},
                             authority_before)

    def test_explicit_cohort_drives_admitted_native_partitions(self):
        import sys
        from types import ModuleType
        from lerobot.datasets import factory
        from tools.data_factory.training_entrypoint import prepare_evaluation_cohort, run_native_training
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            kwargs, request, _ = launch_fixture(root)
            request["episodes"] = [e for e in request["episodes"] if e["episode_index"] in (0, 3)]
            request_path = root / "request.json"
            write_json(request_path, request)
            cohort = prepare_evaluation_cohort(request_path, evidence_directory=root, eval_fraction=.2)
            cohort_path = root / "cohort.json"
            write_json(cohort_path, cohort)
            kwargs["argv"].append(f"--fr5.evaluation_cohort={cohort_path}")
            split, receipt = prepare_launch(**kwargs)
            self.assertEqual((split["train_episodes"], split["eval_episodes"]), ([0, 2], [3]))
            self.assertEqual(receipt["normalization"]["episodes"], [0, 2])
            cfg = SimpleNamespace(dataset=SimpleNamespace(root=str(kwargs["dataset"]),
                repo_id=kwargs["repo_id"], episodes=[0, 2, 3], eval_split=.34,
                streaming=False, use_imagenet_stats=True, revision=None, video_backend="pyav",
                image_transforms=SimpleNamespace(enable=False)), trainable_config=None, tolerance_s=1e-4)
            native = ModuleType("lerobot.scripts.lerobot_train")
            native.make_train_eval_datasets = mock.Mock(side_effect=AssertionError("fraction factory used"))
            def dataset(*args, **values):
                return SimpleNamespace(episodes=values["episodes"], meta=SimpleNamespace(stats={}))
            def main():
                self.assertFalse(any(a.startswith("--fr5.") for a in sys.argv))
                train, heldout = native.make_train_eval_datasets(cfg)
                self.assertEqual((train.episodes, heldout.episodes), ([0, 2], [3]))
                self.assertEqual(train.meta.stats["action"]["mean"].tolist(), [100.] * 7)
                self.assertEqual(heldout.meta.stats["action"]["mean"].tolist(), [100.] * 7)
            native.main = main
            before = snapshot(kwargs["dataset"])
            with mock.patch.dict(sys.modules, {"lerobot.scripts.lerobot_train": native}), \
                 mock.patch.object(factory, "LeRobotDatasetMetadata"), \
                 mock.patch.object(factory, "resolve_delta_timestamps", return_value={}), \
                 mock.patch.object(factory, "LeRobotDataset", side_effect=dataset):
                self.assertEqual(run_native_training(kwargs["argv"], split, receipt), 0)
            self.assertEqual(snapshot(kwargs["dataset"]), before)
            # Tampering either the source or the selected identity cannot enter training.
            cohort_path.write_text("{}")
            with self.assertRaises(ContractError):
                prepare_launch(**kwargs)

    def test_explicit_cohort_survives_saved_checkpoint_admission(self):
        from tests.test_offline_evaluation import admitted_case
        from tools.data_factory.training_entrypoint import prepare_evaluation_cohort, options
        from tools.validate_training_checkpoint import validate_checkpoint
        from tools.evaluate_smolvla_offline import admit_evaluation
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            args, old_split = admitted_case(root)
            inventory = json.loads(args.approved_inventory.read_text())
            request = {k: inventory["dataset_identity"][k] for k in ("dataset_root", "dataset_id", "repo_id")}
            request["episodes"] = []
            for e in inventory["episodes"]:
                if e["episode_index"] == 2:
                    continue
                provenance = json.loads(Path(e["episode_provenance"]["artifact_path"]).read_text())
                request["episodes"].append(dict(episode_id=e["episode_id"], episode_index=e["episode_index"],
                    technical_validator_path=e["technical_validator"]["artifact_path"],
                    human_semantic_evidence_path=e["human_semantic_evidence"]["artifact_path"],
                    seed_manifest_path=str(root / f"{e['episode_id']}.seed-manifest.SYNTHETIC_TEST_ONLY.json"),
                    manifest_slot_id=provenance["manifest_slot_id"]))
            rp=root / "request.json"
            write_json(rp, request)
            cp=root / "cohort.json"
            write_json(cp, prepare_evaluation_cohort(rp, evidence_directory=root, eval_fraction=.2))
            output=Path(args.checkpoint).parents[2]
            argv=json.loads((output / "fr5_training_receipt.json").read_text())["normalized_argv"]
            argv.append(f"--fr5.evaluation_cohort={cp}")
            split, receipt=prepare_launch(dataset=args.dataset, repo_id=args.repo_id, inventory=args.approved_inventory,
                profile="smolvla", collection_profile=old_split["feature_contract"]["collection_profile_id"], argv=argv)
            write_json(output / "fr5_training_split.json", split)
            write_json(output / "fr5_training_receipt.json", receipt)
            write_normalization_fixture(Path(args.checkpoint), receipt)
            self.assertEqual(validate_checkpoint(Path(args.checkpoint)), (Path(args.checkpoint), output))
            self.assertEqual(admit_evaluation(args)["episodes"], [3])
            cp.write_text("{}")
            with self.assertRaises(ValueError):
                validate_checkpoint(Path(args.checkpoint))

    def test_curator_request_cohort_reaches_public_recipe_without_override(self):
        self._curator_cohort_public_recipe(union=False)

    def test_curator_union_cohort_reaches_public_admitted_partitions(self):
        self._curator_cohort_public_recipe(union=True)

    def _curator_cohort_public_recipe(self, *, union):
        import sys
        import io
        from tests.data_factory.curator.support import make_mapping_cohort_case
        from tools.data_factory.curator.workflow.mapping import publish_mapped_training_request
        from tools.data_factory import training_entrypoint as training
        requests, sources, root, mapping_options, cohort = make_mapping_cohort_case(self.addCleanup)
        expected_train, expected_eval = [0, 2, 4, 5], [7]
        if union:
            from tools.data_factory.training_split import compose_evaluation_cohorts
            new = training.prepare_evaluation_cohort(requests[1], evidence_directory=root, eval_episodes=[2])
            cohort = compose_evaluation_cohorts([cohort, new])
            write_json(mapping_options["evaluation_cohort"], cohort)
            expected_train, expected_eval = [0, 4, 5], [2, 7]
        requests.reverse()
        result = publish_mapped_training_request(requests, root / "mapped", **mapping_options)
        request_path = Path(result["request_path"])
        request = json.loads(request_path.read_text())
        authority_root = root / "learning"
        authority_root.mkdir()
        batch = authority_root / "batch"
        batch.mkdir()
        delegation = dict(schema_version=approval.DELEGATION_SCHEMA, delegation_id="synthetic-cohort-r1",
            scope=approval.PRODUCTION_SCOPE, delegated_by="workspace-user", authorized_actor="learning-fixture",
            authorization_source_ref="SYNTHETIC_TEST_ONLY", dataset={k:request[k] for k in ("repo_id", "dataset_root")},
            output_root=str(authority_root), profiles=["smolvla"],
            limits=dict(max_steps=2,max_batch_size=2,max_checkpoints=1), authority=copy.deepcopy(approval.DELEGATION_AUTHORITY))
        delegation_path=root / "delegation.json"
        write_json(delegation_path, delegation)
        argv=["training_entrypoint.py", "run-delegated", "--request", str(request_path),
              "--approval-output", str(batch), "--delegation", str(delegation_path),
              "--authorized-actor", "learning-fixture", "--profile", "smolvla",
              "--collection-profile", "fr5-up-wrist-rgb-30hz-v2", "--output", str(authority_root / "run"),
              "--steps", "2", "--batch-size", "1", "--eval-split", ".4", "--eval-steps", "2", "--save-freq", "2"]
        before=snapshot(authority_root)
        with mock.patch.object(sys, "argv", argv+["--evaluation-cohort",str(root / "other.json")]), \
             mock.patch.object(training, "delegate_training_batch") as issue, \
             mock.patch.object(training, "run_native_training") as trainer, \
             mock.patch("sys.stderr",new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as failed:
                training.main()
            self.assertEqual(failed.exception.code,2)
            issue.assert_not_called(); trainer.assert_not_called()
        self.assertEqual(snapshot(authority_root),before)
        def native(command, split, receipt):
            self.assertEqual(split["train_episodes"],expected_train)
            self.assertEqual(split["eval_episodes"],expected_eval)
            self.assertEqual(receipt["normalization"]["episodes"],expected_train)
            self.assertEqual(split["evaluation_cohort"]["cohort"]["cohort_digest"],cohort["cohort_digest"])
            self.assertIn("--fr5.evaluation_cohort",training.options(command[1:]))
            return 0  # No checkpoint: public CLI must report this truthfully.
        with mock.patch.object(sys,"argv",argv), mock.patch.object(training,"run_native_training",side_effect=native) as trainer, \
             mock.patch("sys.stdout",new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as ended:
                training.main()
            self.assertEqual(ended.exception.code,1)
        trainer.assert_called_once()

    def test_unsupported_act_native_evaluation_rejected_before_authority(self):
        from tools.data_factory.training_entrypoint import run_delegated_request

        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            _, request, _ = launch_fixture(root)
            before = snapshot(root)
            with self.assertRaisesRegex(ContractError, "TRAINING_EVALUATOR_UNSUPPORTED"):
                run_delegated_request(
                    request, approval_output=root / "authority", authorized_actor="actor",
                    delegation_path=root / "missing-delegation.json", profile="act",
                    collection_profile="fixture", output=root / "run", steps=2,
                    batch_size=1, eval_split=0.34, eval_steps=1, save_freq=1,
                )
            self.assertEqual(snapshot(root), before)

    def test_cli_failed_or_checkpointless_run_returns_nonzero(self):
        import sys
        from tools.data_factory import training_entrypoint

        for status in ("TRAINING_FAILED", "TRAINING_RETURNED_NO_CHECKPOINT", "EXISTING_OUTPUT"):
            with mock.patch.object(training_entrypoint, "run_delegated_request",
                                   return_value={"status": status}):
                argv = ["training_entrypoint.py", "run-delegated", "--request", "r",
                        "--approval-output", "a", "--delegation", "d", "--authorized-actor", "x",
                        "--profile", "smolvla", "--collection-profile", "c", "--output", "o",
                        "--steps", "1", "--batch-size", "1", "--eval-split", "0.2",
                        "--eval-steps", "1", "--save-freq", "1"]
                with mock.patch.object(sys, "argv", argv), mock.patch.object(
                        training_entrypoint, "load_json_strict", return_value={}):
                    with self.assertRaises(SystemExit) as raised:
                        training_entrypoint.main()
                self.assertEqual(raised.exception.code, 1)

    def test_public_shell_dry_run_and_validator_reject_legacy_marker(self):
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            kwargs, _, _ = launch_fixture(root)
            before = snapshot(root)
            args = [str(project / "scripts/train_policy.sh"), "--profile", "act", "--collection-profile", kwargs["collection_profile"],
                "--approved-inventory", str(kwargs["inventory"]), "--root", str(kwargs["dataset"].parent),
                "--output", str(root / "outputs/run"), "--dry-run", kwargs["dataset"].name,
                "--dataset.episodes=[0,2,3]", "--batch_size=2", "--steps=2", "--dataset.eval_split=0.34", "--eval_steps=1", "--save_freq=1"]
            import os
            result = subprocess.run(args, capture_output=True, text=True, env={**os.environ, "FR5_REPO_ID": kwargs["repo_id"]})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"eval_episodes": [2, 3]', result.stdout)
            for option in (
                "--policy.push_to_hub=false",
                "--save_checkpoint_to_hub=false",
                "--wandb.enable=false",
            ):
                self.assertIn(option, result.stdout)
            explicit = [
                *args, "--policy.push_to_hub=true",
                "--save_checkpoint_to_hub=true", "--wandb.enable=true",
            ]
            explicit_result = subprocess.run(
                explicit, capture_output=True, text=True,
                env={**os.environ, "FR5_REPO_ID": kwargs["repo_id"]},
            )
            self.assertEqual(explicit_result.returncode, 0, explicit_result.stderr)
            for option in (
                "--policy.push_to_hub", "--save_checkpoint_to_hub", "--wandb.enable",
            ):
                self.assertIn(f"{option}=true", explicit_result.stdout)
                self.assertNotIn(f"{option}=false", explicit_result.stdout)
            self.assertEqual(snapshot(root), before)
            write_json(kwargs["dataset"] / "meta/training_approved.json", {"approved": True})
            result = subprocess.run([str(project / "scripts/validate_dataset.sh"), "--root", str(kwargs["dataset"].parent), "--require-approved", kwargs["dataset"].name], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("legacy", result.stderr)
            # Public non-dry launch also stops before technical decode or training.
            args.remove("--dry-run")
            write_json(kwargs["inventory"], {"approved": True})
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "outputs").exists())

    def test_preapproval_preview_and_denied_tty_never_issue_consent(self):
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            _, request, _ = launch_fixture(root)
            output = root / "human-approvals"
            output.mkdir()
            before = snapshot(root)
            with mock.patch.object(approval, "_confirm_human_training_approval") as confirm:
                preview = approve(request, output, "fixture-human", dry_run=True)
                confirm.assert_not_called()
            self.assertEqual(preview["status"], "PREVIEW_NOT_APPROVED")
            self.assertEqual(snapshot(root), before)
            with mock.patch.object(approval, "_confirm_human_training_approval", side_effect=ContractError("HUMAN_TTY_REQUIRED")):
                with self.assertRaisesRegex(ContractError, "HUMAN_TTY_REQUIRED"):
                    approve(request, output, "fixture-human", dry_run=False)
            self.assertFalse(list(output.glob("*.approval.json")))
            self.assertFalse((output / "training_approved.json").exists())

    def test_human_approval_connection_uses_existing_contract_and_external_inventory(self):
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            kwargs, request, _ = launch_fixture(root)
            output = root / "human-approvals"
            output.mkdir()
            before = snapshot(kwargs["dataset"])
            # Test double only: no real controlling-terminal consent is manufactured.
            with mock.patch.object(approval, "_confirm_human_training_approval") as confirm:
                issued = approve(request, output, "fixture-human", dry_run=False)
            confirm.assert_called_once()
            documents = [json.loads(path.read_text()) for path in sorted(output.glob("*.approval.json"))]
            self.assertEqual(len(documents), 3)
            batch_digest = approval._batch_digest(documents)
            self.assertEqual(confirm.call_args.args, ("APPROVE BATCH " + batch_digest.removeprefix("sha256:")[:12],))
            self.assertIn("Selected episodes (3): 0, 2, 3", confirm.call_args.kwargs["summary"])
            self.assertTrue(all(document["schema_version"] == approval.BATCH_APPROVAL_SCHEMA
                and document["batch_digest"] == batch_digest for document in documents))
            self.assertEqual(approval.validate_current_training_inventory(output / "training_approved.json",
                dataset_root=kwargs["dataset"], repo_id=kwargs["repo_id"], selected_episodes=[0, 2, 3]), issued)
            self.assertEqual(snapshot(kwargs["dataset"]), before)


if __name__ == "__main__":
    unittest.main()
