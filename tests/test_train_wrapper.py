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

from tests.data_factory.test_training_approval import synthetic_fixture, write_json, snapshot
from tools.data_factory import training_approval as approval
from tools.data_factory.training_entrypoint import approve, launch, prepare_launch, prepare_approvals
from tools.fr5_data_factory import ContractError, canonical_digest
from tools.fr5_training_profile import build_profile, policy_metadata


def launch_fixture(root):
    import pyarrow as pa
    import pyarrow.parquet as pq

    selected = [0, 2, 3]
    fixtures = [synthetic_fixture(root, f"episode-{index}", index) for index in selected]
    dataset = Path(fixtures[0][0]["dataset_root"])
    metadata = dataset / "meta"
    (metadata / "episodes/chunk-000").mkdir(parents=True)
    (metadata / "source_provenance").mkdir()
    from tools.fr5_dataset_schema import dataset_features
    info = {"codebase_version": "v3.0", "fps": 30, "total_episodes": 4, "total_frames": 8,
            "features": dataset_features(fps=30, height=480, width=640, cameras=("up", "wrist"), use_videos=True)}
    write_json(metadata / "info.json", info)
    rows = []
    for i in range(4):
        row = {"episode_index": i, "tasks": ["pick up the cube and place it at the destination"], "length": 2}
        for key in ("action", "observation.state", "observation.images.up", "observation.images.wrist"):
            for name in ("min", "max", "mean", "std", "count"):
                number = 1.0 if name == "std" else float(i * 100)
                row[f"stats/{key}/{name}"] = ([2] if name == "count" else
                    [[[number]]] * 3 if key.startswith("observation.images.") else [number] * 7)
        rows.append(row)
    pq.write_table(pa.Table.from_pylist(rows), metadata / "episodes/chunk-000/file-000.parquet")
    for index in range(4):
        (metadata / f"source_provenance/episode-{index:06d}.jsonl").write_text('{"frame_index":0}\n{"frame_index":1}\n')
    identity = approval.current_dataset_identity(dataset, repo_id="tests/synthetic-dataset", dataset_id="synthetic-dataset-r1")
    entries, request_episodes = [], []
    for _, technical, semantic, approved, entry in fixtures:
        index = entry["episode_index"]
        content = approval.current_episode_digest(identity, index)
        semantic["checklist_id"] = "pick-place-v1"
        semantic_path = Path(entry["human_semantic_evidence"]["artifact_path"])
        _, semantic_digest = write_json(semantic_path, semantic)
        entry["human_semantic_evidence"]["artifact_digest"] = semantic_digest
        provenance_path = Path(entry["episode_provenance"]["artifact_path"])
        provenance = json.loads(provenance_path.read_text())
        provenance.update(scope=approval.PRODUCTION_SCOPE, dataset_identity_digest=canonical_digest(identity), episode_content_digest=content)
        _, provenance_digest = write_json(provenance_path, provenance)
        entry["episode_provenance"]["artifact_digest"] = provenance_digest
        approved.update(scope=approval.PRODUCTION_SCOPE, dataset_identity=identity, episode_content_digest=content,
                        episode_provenance_digest=provenance_digest, human_semantic_evidence_digest=semantic_digest)
        _, approved_digest = write_json(Path(entry["training_approval"]["artifact_path"]), approved)
        entry["training_approval"]["artifact_digest"] = approved_digest
        entry.update(dataset_identity_digest=canonical_digest(identity), episode_content_digest=content)
        entries.append(entry)
        request_episodes.append({"episode_id": entry["episode_id"], "episode_index": index,
            "technical_validator_path": entry["technical_validator"]["artifact_path"],
            "human_semantic_evidence_path": str(semantic_path),
            "seed_manifest_path": str(root / f"{entry['episode_id']}.seed-manifest.SYNTHETIC_TEST_ONLY.json"),
            "manifest_slot_id": provenance["manifest_slot_id"]})
    inventory = approval.build_training_approved_inventory(scope=approval.PRODUCTION_SCOPE, dataset_identity=identity, episodes=entries)
    inventory_path = root / "training_approved.json"
    write_json(inventory_path, inventory)
    output = root / "outputs/run"
    argv = ["fixture-lerobot-train", *build_profile("act", policy_metadata(info)),
            f"--dataset.root={dataset}", "--dataset.repo_id=tests/synthetic-dataset", "--dataset.episodes=[0,2,3]",
            "--dataset.eval_split=0.34", f"--output_dir={output}", "--batch_size=2", "--steps=2", "--eval_steps=1", "--save_freq=1"]
    kwargs = dict(dataset=dataset, repo_id="tests/synthetic-dataset", inventory=inventory_path,
                  profile="act", collection_profile="fr5-up-wrist-rgb-30hz-v2", argv=argv)
    request = {"dataset_root": str(dataset), "dataset_id": identity["dataset_id"], "repo_id": identity["repo_id"], "episodes": request_episodes}
    return kwargs, request, inventory


def write_normalization_fixture(policy, receipt):
    """Synthetic processor state only; never a real policy/checkpoint success claim."""
    import numpy as np
    from safetensors.numpy import save_file

    stats = {f"{key}.{name}": np.asarray(value, dtype=np.float32)
             for key, values in receipt["normalization"]["stats"].items() for name, value in values.items()}
    for pipeline, registry in (("policy_preprocessor", "normalizer_processor"),
                               ("policy_postprocessor", "unnormalizer_processor")):
        state_file = f"{pipeline}_normalization.safetensors"
        save_file(stats, policy / state_file)
        config = {"features": {"observation.state": {"type": "STATE", "shape": [7]},
                               "action": {"type": "ACTION", "shape": [7]}},
                  "norm_map": {"STATE": "MEAN_STD", "ACTION": "MEAN_STD"}}
        write_json(policy / f"{pipeline}.json", {"steps": [
            {"registry_name": registry, "state_file": state_file, "config": config}]})


class TrainingLaunchConnectionTest(unittest.TestCase):
    def test_native_consumer_keeps_official_split_and_excludes_nontrain_statistics(self):
        import math
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
                cfg.dataset.episodes = [0, 1, 3]
                with self.assertRaisesRegex(ContractError, "TRAINING_RUNTIME_DATASET"):
                    run_native_training(kwargs["argv"], split, receipt)
            self.assertIs(sys.argv, original_argv)
            self.assertIs(native.make_train_eval_datasets, factory.make_train_eval_datasets)
            self.assertEqual(len(created), 2)
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
