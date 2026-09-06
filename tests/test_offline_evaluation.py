#!/usr/bin/env python3

from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from tests.test_train_wrapper import launch_fixture, write_normalization_fixture
from tools.data_factory.training_entrypoint import options, prepare_launch
from tools.evaluate_smolvla_offline import (
    admit_evaluation,
    evaluate,
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
    write_normalization_fixture(policy_dir, receipt)
    args = SimpleNamespace(
        checkpoint=str(policy_dir), dataset=kwargs["dataset"], repo_id=kwargs["repo_id"],
        approved_inventory=kwargs["inventory"], episodes=None, batch_size=1,
        num_workers=0, max_batches=0, output=root / "evaluation.json",
        seed=1000, device="cpu", use_amp=False,
    )
    return args, split


def evaluation_argv(args: SimpleNamespace) -> list[str]:
    return [
        "evaluate_smolvla_offline.py", args.checkpoint, str(args.dataset),
        "--repo-id", args.repo_id, "--approved-inventory", str(args.approved_inventory),
        "--output", str(args.output),
    ]


def immutable_input_bytes(args: SimpleNamespace) -> dict[Path, bytes]:
    output_dir = Path(args.checkpoint).parents[2]
    paths = [
        args.approved_inventory,
        output_dir / "fr5_training_split.json",
        output_dir / "fr5_training_receipt.json",
        *(path for path in Path(args.checkpoint).parent.rglob("*") if path.is_file()),
    ]
    return {path: path.read_bytes() for path in paths}


def fake_inference_modules(admission: dict, batches: list[dict]) -> dict[str, ModuleType]:
    class FakeTensor:
        def __init__(self, values, dtype=None):
            self.values = values
            self.dtype = dtype

        def detach(self):
            return self

        def cpu(self):
            return self

        def tolist(self):
            return list(self.values)

        def __iter__(self):
            return iter(self.values)

        def float(self):
            return self

        def __truediv__(self, _value):
            return self

    class FakeDataset:
        def __init__(self, *_args, **_kwargs):
            self.meta = SimpleNamespace(camera_keys=["camera"], episodes=[{"length": 2}] * 4)
            self.batches = batches

    class FakeLoader:
        fetched_count = 0

        def __init__(self, dataset, **_kwargs):
            self.batches = dataset.batches
            type(self).fetched_count = 0

        def __iter__(self):
            for batch in self.batches:
                type(self).fetched_count += 1
                yield batch

        def __len__(self):
            return len(self.batches)

    class FakePolicy:
        config = SimpleNamespace(
            robot_state_feature=SimpleNamespace(shape=(7,)),
            action_feature=SimpleNamespace(shape=(7,)),
        )

        def eval(self):
            return None

        def forward(self, batch, reduction):
            if reduction != "none":
                raise AssertionError(reduction)
            return FakeTensor(batch["loss"]), None

    class FakeConfig:
        @classmethod
        def from_pretrained(cls, _checkpoint):
            return cls()

    torch = ModuleType("torch")
    torch.uint8 = "uint8"
    torch.cuda = SimpleNamespace(is_available=lambda: False, manual_seed_all=lambda _seed: None)
    torch.manual_seed = lambda _seed: None
    torch.no_grad = nullcontext
    torch.autocast = lambda **_kwargs: nullcontext()
    torch_utils = ModuleType("torch.utils")
    torch_data = ModuleType("torch.utils.data")
    torch_data.DataLoader = FakeLoader

    numpy = ModuleType("numpy")
    numpy.random = SimpleNamespace(seed=lambda _seed: None)
    lerobot = ModuleType("lerobot")
    datasets = ModuleType("lerobot.datasets")
    dataset_factory = ModuleType("lerobot.datasets.factory")
    dataset_factory.resolve_delta_timestamps = lambda *_args: {}
    dataset_module = ModuleType("lerobot.datasets.lerobot_dataset")
    dataset_module.LeRobotDataset = FakeDataset
    dataset_module.LeRobotDatasetMetadata = lambda *_args, **_kwargs: SimpleNamespace(
        total_episodes=admission["split"]["total_episodes"],
        total_frames=admission["split"]["total_frames"],
        camera_keys=["camera"],
        features={},
    )
    configs = ModuleType("lerobot.configs")
    configs.FeatureType = SimpleNamespace(ACTION="action")
    policies = ModuleType("lerobot.policies")
    policy_factory = ModuleType("lerobot.policies.factory")
    policy_factory.make_policy = lambda *_args, **_kwargs: FakePolicy()
    policy_factory.make_pre_post_processors = lambda **_kwargs: (lambda batch: batch, None)
    smolvla = ModuleType("lerobot.policies.smolvla")
    smolvla_config = ModuleType("lerobot.policies.smolvla.configuration_smolvla")
    smolvla_config.SmolVLAConfig = FakeConfig
    utils = ModuleType("lerobot.utils")
    feature_utils = ModuleType("lerobot.utils.feature_utils")
    feature_utils.dataset_to_policy_features = lambda _features: {}
    return {
        "numpy": numpy, "torch": torch, "torch.utils": torch_utils,
        "torch.utils.data": torch_data, "lerobot": lerobot,
        "lerobot.datasets": datasets, "lerobot.datasets.factory": dataset_factory,
        "lerobot.datasets.lerobot_dataset": dataset_module, "lerobot.configs": configs,
        "lerobot.policies": policies, "lerobot.policies.factory": policy_factory,
        "lerobot.policies.smolvla": smolvla,
        "lerobot.policies.smolvla.configuration_smolvla": smolvla_config,
        "lerobot.utils": utils, "lerobot.utils.feature_utils": feature_utils,
    }


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

    def test_delegated_evaluation_enters_cache_only_mode_and_restores_it(self):
        from huggingface_hub import constants as hub_constants

        before = os.environ.get("HF_HUB_OFFLINE")
        admission = {"inventory": {"episodes": []}}

        def inside(_args, _admission):
            self.assertTrue(hub_constants.is_offline_mode())
            self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")
            return {"offline": True}

        with mock.patch(
            "tools.data_factory.training_approval.inventory_local_training_delegation",
            return_value={"delegated": True},
        ), mock.patch("tools.evaluate_smolvla_offline._evaluate", side_effect=inside):
            self.assertEqual(evaluate(SimpleNamespace(), admission), {"offline": True})
        self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), before)

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

    def test_output_and_temporary_aliases_cannot_replace_inputs(self):
        cases = ("inventory", "receipt", "split", "checkpoint", "symlink", "hardlink", "temporary")
        for kind in cases:
            with self.subTest(kind=kind), TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
                root = Path(directory)
                args, _ = admitted_case(root)
                output_dir = Path(args.checkpoint).parents[2]
                protected = {
                    "inventory": args.approved_inventory,
                    "receipt": output_dir / "fr5_training_receipt.json",
                    "split": output_dir / "fr5_training_split.json",
                    "checkpoint": Path(args.checkpoint) / "model.safetensors",
                }
                if kind in protected:
                    args.output = protected[kind]
                elif kind == "symlink":
                    args.output = root / "inventory-alias.json"
                    args.output.symlink_to(protected["inventory"])
                elif kind == "hardlink":
                    args.output = root / "checkpoint-alias.bin"
                    os.link(protected["checkpoint"], args.output)
                else:
                    args.output = root / "report.json"
                    Path(str(args.output) + ".tmp").symlink_to(protected["receipt"])
                before = immutable_input_bytes(args)
                with mock.patch.object(sys, "argv", evaluation_argv(args)), mock.patch(
                    "tools.evaluate_smolvla_offline.evaluate", return_value={"unexpected": True}
                ) as inference:
                    with self.assertRaisesRegex(ValueError, "immutable evaluation input"):
                        main()
                inference.assert_not_called()
                self.assertEqual(immutable_input_bytes(args), before)

    def test_external_report_path_is_still_written_atomically(self):
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            args, _ = admitted_case(Path(directory))
            report = {"schema_version": 3, "evaluation_complete": True}
            with mock.patch.object(sys, "argv", evaluation_argv(args)), mock.patch(
                "tools.evaluate_smolvla_offline.evaluate", return_value=report
            ), mock.patch("builtins.print"):
                main()
            self.assertEqual(json.loads(args.output.read_text()), report)
            self.assertFalse(Path(str(args.output) + ".tmp").exists())

    def test_report_rejects_existing_transitive_evidence_and_dangling_links(self):
        for kind in ("training_approval", "episode_provenance", "technical_validator",
                     "human_semantic_evidence", "existing-report", "dangling-output", "dangling-temporary"):
            with self.subTest(kind=kind), TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
                args, _ = admitted_case(Path(directory))
                episode = json.loads(args.approved_inventory.read_text())["episodes"][0]
                if kind in episode:
                    args.output = Path(episode[kind]["artifact_path"])
                elif kind == "existing-report":
                    args.output.write_text("previous report")
                else:
                    target = args.output if kind == "dangling-output" else Path(str(args.output) + ".tmp")
                    target.symlink_to(Path(directory) / "absent")
                before = {p: p.read_bytes() for p in Path(directory).rglob("*") if p.is_file()}
                with mock.patch.object(sys, "argv", evaluation_argv(args)), mock.patch(
                    "tools.evaluate_smolvla_offline.evaluate", return_value={"unexpected": True}
                ) as inference, mock.patch("builtins.print"):
                    with self.assertRaisesRegex(ValueError, "immutable evaluation input"):
                        main()
                inference.assert_not_called()
                self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_report_publication_preserves_files_created_during_inference(self):
        for temporary in (False, True):
            with self.subTest(temporary=temporary), TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
                args, _ = admitted_case(Path(directory))
                target = Path(str(args.output) + ".tmp") if temporary else args.output

                def competing_write(*_args):
                    target.write_bytes(b"concurrent writer")
                    return {"schema_version": 3}

                with mock.patch.object(sys, "argv", evaluation_argv(args)), mock.patch(
                    "tools.evaluate_smolvla_offline.evaluate", side_effect=competing_write
                ), mock.patch("builtins.print"):
                    with self.assertRaises(FileExistsError):
                        main()
                self.assertEqual(target.read_bytes(), b"concurrent writer")
                other = args.output if temporary else Path(str(args.output) + ".tmp")
                self.assertFalse(other.exists())

    def test_multibatch_reports_partial_and_complete_coverage_honestly(self):
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            args, _ = admitted_case(Path(directory))
            admission = admit_evaluation(args)
            batches = [
                {"episode_index": None, "camera": None, "loss": [1.0, 3.0]},
                {"episode_index": None, "camera": None, "loss": [5.0]},
                {"episode_index": None, "camera": None, "loss": [7.0]},
            ]
            for batch, episode_indices in zip(batches, ([2, 2], [3], [3]), strict=True):
                batch["episode_index"] = SimpleNamespace(
                    detach=lambda values=episode_indices: SimpleNamespace(
                        cpu=lambda: SimpleNamespace(tolist=lambda: list(values))
                    )
                )
                batch["camera"] = SimpleNamespace(dtype="float")

            modules = fake_inference_modules(admission, batches)
            with mock.patch.dict(sys.modules, modules), mock.patch(
                "tools.evaluate_smolvla_offline.smolvla_camera_mapping", return_value=({}, [])
            ):
                for limit, complete, expected_batches, expected_episodes in (
                    (1, False, 1, [2]),
                    (2, False, 2, [2, 3]),
                    (3, True, 3, [2, 3]),
                ):
                    with self.subTest(max_batches=limit):
                        args.max_batches = limit
                        with mock.patch("tools.evaluate_smolvla_offline.time.perf_counter", side_effect=[10., 12., 14.]):
                            report = evaluate(args, admission)
                        self.assertEqual(modules["torch.utils.data"].DataLoader.fetched_count, expected_batches)
                        self.assertEqual(report["requested_max_batches"], limit)
                        self.assertEqual(report["available_batches"], 3)
                        self.assertEqual(report["evaluated_batches"], expected_batches)
                        self.assertEqual(report["evaluation_complete"], complete)
                        self.assertEqual(report["episodes"], expected_episodes)
                        self.assertEqual(report["admitted_episodes"], [2, 3])
                        self.assertEqual(report["available_samples"], 4)
                        self.assertEqual(report["episode_metrics"][0], {
                            "episode_index": 2, "samples": 2, "available_samples": 2,
                            "evaluation_complete": True, "loss_mean": 2.0,
                        })
                        self.assertEqual(report["episode_metrics"][1]["samples"], limit - 1)
                        self.assertEqual(report["episode_macro_scope"],
                            "complete_heldout" if complete else "observed_samples_only")
                        if limit == 2:
                            self.assertEqual(report["loss_mean"], 3.0)
                            self.assertEqual(report["episode_macro_loss_mean"], 3.5)
                        elif limit == 1:
                            self.assertIsNone(report["episode_metrics"][1]["loss_mean"])
                        self.assertEqual(report["resource_usage"], {
                            "scope": "evaluation_after_admission", "setup_wall_time_s": 2.,
                            "batches_wall_time_s": 2., "samples_per_second": report["samples"] / 2.,
                            "torch_cuda_peak_allocated_bytes": None,
                        })
                        self.assertEqual(
                            report["evidence_scope"],
                            "admitted_heldout_offline_loss"
                            if complete else "bounded_admitted_heldout_offline_loss",
                        )

    def test_nonfinite_loss_never_publishes_a_report(self):
        for loss in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(loss=loss), TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
                args, _ = admitted_case(Path(directory))
                admission = admit_evaluation(args)
                batches = [{"episode_index": SimpleNamespace(detach=lambda: SimpleNamespace(
                    cpu=lambda: SimpleNamespace(tolist=lambda: [2]))),
                    "camera": SimpleNamespace(dtype="float"), "loss": [loss]}]
                with mock.patch.dict(sys.modules, fake_inference_modules(admission, batches)), \
                        mock.patch("tools.evaluate_smolvla_offline.smolvla_camera_mapping", return_value=({}, [])), \
                        mock.patch("tools.evaluate_smolvla_offline.admit_evaluation", return_value=admission), \
                        mock.patch.object(sys, "argv", evaluation_argv(args)):
                    with self.assertRaisesRegex(RuntimeError, "non-finite loss"):
                        main()
                self.assertFalse(args.output.exists())
                self.assertFalse(Path(str(args.output) + ".tmp").exists())

    def test_cuda_resource_counter_brackets_model_loading_with_injected_backend(self):
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            args, _ = admitted_case(Path(directory))
            admission = admit_evaluation(args)
            args.device = "cuda:0"
            batches = [{"episode_index": SimpleNamespace(detach=lambda: SimpleNamespace(
                cpu=lambda: SimpleNamespace(tolist=lambda: [2]))),
                "camera": SimpleNamespace(dtype="float"), "loss": [1.0]}]
            modules = fake_inference_modules(admission, batches)
            events = []
            cuda = modules["torch"].cuda
            cuda.is_available = lambda: True
            cuda.init = lambda: events.append("init")
            cuda.reset_peak_memory_stats = lambda device: events.append(("reset", device))
            cuda.max_memory_allocated = lambda device: events.append(("peak", device)) or 1234
            factory = modules["lerobot.policies.factory"]
            original = factory.make_policy
            factory.make_policy = lambda *a, **kw: events.append("model") or original(*a, **kw)
            with mock.patch.dict(sys.modules, modules), mock.patch(
                "tools.evaluate_smolvla_offline.smolvla_camera_mapping", return_value=({}, [])
            ):
                report = evaluate(args, admission)
            self.assertEqual(events, ["init", ("reset", "cuda:0"), "model", ("peak", "cuda:0")])
            self.assertEqual(report["resource_usage"]["torch_cuda_peak_allocated_bytes"], 1234)

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
            self.assertNotIn(b"--require-approved", calls)
            self.assertLess(calls.index(b"python"), calls.index(b"validate"))
            self.assertIn(b"--episodes", calls)
            self.assertIn(b"2,3", calls)
            self.assertIn(b"--batch-size", calls)
            self.assertIn(b"4", calls)
            self.assertIn(b"--dry-run", calls)

    def test_public_wrapper_admits_selected_inventory_before_technical_validation(self):
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            (root / "fixture").mkdir()
            args, _ = admitted_case(root / "fixture")
            (root / "scripts").mkdir()
            (root / "tools/data_factory").mkdir(parents=True)
            (root / ".venv").symlink_to(project / ".venv", target_is_directory=True)
            for name in ("evaluate_smolvla.sh", "validate_dataset.sh"):
                shutil.copy2(project / "scripts" / name, root / "scripts" / name)
            for name in ("evaluate_smolvla_offline.py", "data_factory/training_entrypoint.py"):
                (root / "tools" / name).symlink_to(project / "tools" / name)
            # Keep the real shell and admission path; only the heavy video decoder is a stand-in.
            (root / "tools/validate_lerobot_dataset.py").write_text("print('TECHNICAL_VALIDATION_REACHED')\n")
            command = [root / "scripts/evaluate_smolvla.sh",
                       "--approved-inventory", args.approved_inventory,
                       "--root", args.dataset.parent, "--output", args.output,
                       "--dry-run", args.checkpoint, args.dataset.name, "--episodes", "2,3"]
            env = {**os.environ, "FR5_REPO_ID": args.repo_id}
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("inference not run and output not created", result.stdout)
            self.assertIn("TECHNICAL_VALIDATION_REACHED", result.stdout)
            self.assertFalse(args.output.exists())
            args.approved_inventory.unlink()
            rejected = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertNotIn("TECHNICAL_VALIDATION_REACHED", rejected.stdout)
            self.assertFalse(args.output.exists())


if __name__ == "__main__":
    unittest.main()
