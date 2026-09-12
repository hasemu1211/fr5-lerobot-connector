"""CPU-small native trainer/state tests. No model download, decoding or CUDA."""

import copy
from contextlib import ExitStack
import json
import multiprocessing
from pathlib import Path
import random
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import torch
from safetensors.torch import load_file, save_file

from tools.data_factory.training_continuation import native_continuation
from tools.data_factory.training_receipts import tree_digest
from tools.validate_training_checkpoint import (
    CONTINUATION_STATE, advance_sample_cursor, continuation_checkpoint_state, continuation_argv,
)


def setUpModule():
    # Native loader/optimizer setup may settle the platform default context.
    # Restore even an unset method so later Curator tests can select spawn.
    previous_method = multiprocessing.get_start_method(allow_none=True)
    unittest.addModuleCleanup(multiprocessing.set_start_method, previous_method, force=True)
    # Existing injected consumer tests replace this module through sys.modules.
    # Do not leave a newly cached package attribute that bypasses their fixture.
    import lerobot.scripts
    global _previous_trainer, _previous_attribute
    _previous_trainer = sys.modules.get("lerobot.scripts.lerobot_train")
    _previous_attribute = getattr(lerobot.scripts, "lerobot_train", None)


def tearDownModule():
    import lerobot.scripts
    if _previous_trainer is None:
        sys.modules.pop("lerobot.scripts.lerobot_train", None)
    else:
        sys.modules["lerobot.scripts.lerobot_train"] = _previous_trainer
    if _previous_attribute is None:
        vars(lerobot.scripts).pop("lerobot_train", None)
    else:
        lerobot.scripts.lerobot_train = _previous_attribute


class TinyDataset(torch.utils.data.Dataset):
    num_frames = 13
    num_episodes = 1
    episodes = [0]
    absolute_to_relative_idx = None
    meta = SimpleNamespace(episodes={"dataset_from_index": [0], "dataset_to_index": [13]},
                           has_language_columns=False, camera_keys=[], stats={})

    def __len__(self):
        return self.num_frames

    def __getitem__(self, index):
        return {"action": torch.tensor([float(index)])}


class TinyPolicy(torch.nn.Module):
    def __init__(self, cfg, trace, cached_gaussians=True):
        super().__init__()
        self.config = cfg
        self.weight = torch.nn.Parameter(torch.tensor([.1, -.2]))
        if cfg.pretrained_path:
            self.load_state_dict(load_file(Path(cfg.pretrained_path) / "model.safetensors"))
        self.trace = trace
        self.cached_gaussians = cached_gaussians

    def get_optim_params(self):
        return self.parameters()

    def forward(self, batch):
        noise = torch.randn(2)
        # Exercise native RNG plus caches omitted/rounded by native serialization.
        scalar = (random.gauss(0, 1) + float(np.random.normal()) if self.cached_gaussians
                  else random.random() + float(np.random.random()))
        loss = (self.weight - noise - batch["action"].mean() / 13 - scalar).square().sum()
        self.trace.append({"indices": batch["action"].flatten().tolist(), "noise": noise.tolist(),
                           "scalar": scalar, "loss": loss.item(), "training": self.training})
        return loss, {}

    def save_pretrained(self, directory, **kwargs):
        directory.mkdir(parents=True, exist_ok=True)
        self.config.save_pretrained(directory)
        save_file(self.state_dict(), directory / "model.safetensors")


class IdentityProcessor:
    def __call__(self, batch):
        return batch

    def save_pretrained(self, directory):
        pass


class NativeContinuationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="SYNTHETIC_CONTINUATION-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("torch.cuda.is_available", return_value=False))
        self.stack.enter_context(patch.dict("os.environ", {"WORLD_SIZE": "1"}))
        from lerobot.scripts import lerobot_train
        self.trainer = lerobot_train
        self.trace = []
        self.stack.enter_context(patch.object(self.trainer, "make_train_eval_datasets",
                                              return_value=(TinyDataset(), None)))
        self.stack.enter_context(patch.object(self.trainer, "make_policy",
                                              side_effect=lambda cfg, **_: TinyPolicy(cfg, self.trace)))
        self.stack.enter_context(patch.object(self.trainer, "make_pre_post_processors",
                                              return_value=(IdentityProcessor(), IdentityProcessor())))
        native_update = self.trainer.update_policy

        def record(*args, **kwargs):
            result = native_update(*args, **kwargs)
            self.trace[-1].update(lr=result[0].lr.val, weights=args[1].weight.detach().tolist())
            return result

        # A recording Mock retains every accelerator in call_args, including its
        # persistent workers across segments. Only the explicit scalar trace is needed.
        self.stack.enter_context(patch.object(self.trainer, "update_policy", record))

    def initial(self, workers=0, eval_steps=0, batch_size=1):
        from lerobot.configs.default import DatasetConfig
        from lerobot.configs.train import TrainPipelineConfig
        from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig
        cfg = TrainPipelineConfig(dataset=DatasetConfig(repo_id="local/tiny", root=self.root, eval_split=.2),
            policy=SmolVLAConfig(device="cpu", push_to_hub=False), output_dir=self.root / "parent",
            steps=12, batch_size=batch_size, num_workers=workers, save_freq=3, log_freq=0,
            eval_steps=eval_steps, prefetch_factor=4, persistent_workers=bool(workers),
            dataloader_multiprocessing_context="spawn" if workers else None)
        with patch("sys.argv", ["tiny"]):
            self.trainer.train(cfg)
        parent = cfg.output_dir / "checkpoints/000003/pretrained_model"
        # This mimics an admitted legacy parent; no real authority is issued.
        self.receipt = {"normalization": {"stats": {"action": {"count": [13]}}}}
        return parent

    def run_segment(self, parent, state, schedule, end, name, eval_steps=0):
        from lerobot.configs.train import TrainPipelineConfig
        cfg = TrainPipelineConfig.from_pretrained(parent / "train_config.json")
        cfg.output_dir = self.root / name
        cfg.steps, cfg.batch_size, cfg.save_freq, cfg.resume = end, 4, end, True
        cfg.eval_steps = eval_steps
        self.trace.clear()
        native_save = self.trainer.save_checkpoint

        def check_worker_lifetime(**kwargs):
            if state.get("persistent_eval_started"):
                # Four TRAIN workers may remain, but completed EVAL must not
                # retain another pool during native checkpoint serialization.
                self.assertEqual(len(multiprocessing.active_children()), 4)
            return native_save(**kwargs)

        with patch("sys.argv", ["tiny", f"--config_path={parent / 'train_config.json'}"]):
            with patch.object(self.trainer, "save_checkpoint", check_worker_lifetime):
                with native_continuation(self.trainer, checkpoint=parent.parent, state=state,
                                         schedule=schedule, batch_size=4):
                    self.trainer.train(cfg)
        child = cfg.output_dir / f"checkpoints/{end:06d}/pretrained_model"
        child_state = json.loads((child.parent / "training_state" / CONTINUATION_STATE).read_text())
        return child, child_state, copy.deepcopy(self.trace)

    def test_two_successive_resumes_match_native_uninterrupted_trace_and_state(self):
        parent = self.initial()
        original = tree_digest(parent.parent)
        state = continuation_checkpoint_state(parent, self.receipt)
        for mode in ("preserve", "hold"):
            with self.subTest(mode=mode):
                schedule = {"native_horizon": 12, "hold_from_step": 3 if mode == "hold" else None}
                reference, expected_state, expected = self.run_segment(parent, state, schedule, 11, mode + "-reference")
                first, first_state, actual = self.run_segment(parent, state, schedule, 5, mode + "-first")
                first_digest = tree_digest(first.parent)
                second, second_state, part = self.run_segment(first, first_state, schedule, 7, mode + "-second")
                actual += part
                second_digest = tree_digest(second.parent)
                final, final_state, part = self.run_segment(second, second_state, schedule, 11, mode + "-final")
                actual += part
                self.assertEqual(actual, expected)
                self.assertEqual(final_state, expected_state)
                for filename in ("optimizer_state.safetensors", "rng_state.safetensors"):
                    a = load_file(reference.parent / "training_state" / filename)
                    b = load_file(final.parent / "training_state" / filename)
                    self.assertEqual(set(a), set(b))
                    for key in a:
                        torch.testing.assert_close(a[key], b[key], rtol=0, atol=0)
                self.assertEqual(json.loads((reference.parent / "training_state/scheduler_state.json").read_text()),
                                 json.loads((final.parent / "training_state/scheduler_state.json").read_text()))
                self.assertEqual(tree_digest(first.parent), first_digest)
                self.assertEqual(tree_digest(second.parent), second_digest)
                self.assertEqual(tree_digest(parent.parent), original)
                self.assertEqual([len(row["indices"]) for row in actual], [4, 4, 2, 4, 4, 4, 1, 4])
                self.assertEqual(final_state["cursor"], {"num_frames": 13, "epoch": 2,
                                                       "offset": 4, "samples_consumed": 30})

    def test_epoch_boundary_resume_keeps_ordinary_iterator_rng_draw(self):
        parent = self.initial()
        state = continuation_checkpoint_state(parent, self.receipt)
        schedule = state["schedule"]
        _, _, expected = self.run_segment(parent, state, schedule, 9, "boundary-reference")
        first, first_state, actual = self.run_segment(parent, state, schedule, 6, "boundary-first")
        self.assertEqual(first_state["cursor"]["offset"], 0)
        _, _, part = self.run_segment(first, first_state, schedule, 9, "boundary-final")
        self.assertEqual(actual + part, expected)

    def test_saved_native_child_validates_recipe_cursor_and_rng_caches(self):
        parent = self.initial()
        state = continuation_checkpoint_state(parent, self.receipt)
        child, child_state, _ = self.run_segment(parent, state, state["schedule"], 7, "validate")
        receipt = {**self.receipt, "initialization": {"mode": "continuation", "checkpoint": str(parent),
                   "step": state["step"], "cursor": state["cursor"], "schedule": state["schedule"]},
                   "normalized_argv": ["tiny", f"--output_dir={self.root / 'validate'}", "--steps=7",
                                       "--batch_size=4", "--eval_steps=0", "--save_freq=7"]}
        self.assertEqual(continuation_checkpoint_state(child, receipt), child_state)
        state_file = child.parent / "training_state" / CONTINUATION_STATE
        for field, value in (("cursor", {**child_state["cursor"], "samples_consumed": 99}),
                             ("python_gauss", "forged"), ("schedule", {"native_horizon": 99})):
            with self.subTest(field=field):
                state_file.write_text(json.dumps({**child_state, field: value}))
                with self.assertRaisesRegex(ValueError, "committed state"):
                    continuation_checkpoint_state(child, receipt)
        state_file.write_text(json.dumps(child_state))
        config_file = child / "train_config.json"
        cfg = json.loads(config_file.read_text())
        cfg["optimizer"]["lr"] *= 2
        config_file.write_text(json.dumps(cfg))
        with self.assertRaisesRegex(ValueError, "inherited configuration"):
            continuation_checkpoint_state(child, receipt)

    def test_lr_prefix_mismatch_fails_before_update_and_restores_hooks(self):
        from tools.fr5_data_factory import ContractError
        parent = self.initial()
        state = continuation_checkpoint_state(parent, self.receipt)
        original = self.trainer.compute_sampler_state
        with self.assertRaisesRegex(ContractError, "LR_PREFIX"):
            self.run_segment(parent, state, {"native_horizon": 24, "hold_from_step": None}, 7, "bad-prefix")
        self.assertFalse(self.trace)
        self.assertIs(self.trainer.compute_sampler_state, original)

    def test_native_development_evaluation_rng_is_preserved_at_matching_cadence(self):
        parent = self.initial()
        state = continuation_checkpoint_state(parent, self.receipt)
        with patch.object(self.trainer, "make_train_eval_datasets", return_value=(TinyDataset(), TinyDataset())):
            _, _, expected = self.run_segment(parent, state, state["schedule"], 9, "eval-reference", 2)
            first, first_state, actual = self.run_segment(parent, state, state["schedule"], 5, "eval-first", 2)
            _, _, part = self.run_segment(first, first_state, state["schedule"], 9, "eval-final", 2)
        self.assertEqual(actual + part, expected)

    def persistent_worker_case(self, eval_steps, first_end, second_end):
        # No omitted Gaussian cache in this legacy fixture. Existing tests above
        # separately cover the continuation sidecar's cached Gaussian states.
        with patch.object(self.trainer, "make_policy", side_effect=lambda cfg, **_: TinyPolicy(
                cfg, self.trace, cached_gaussians=False)), patch.object(
                self.trainer, "make_train_eval_datasets", return_value=(TinyDataset(), TinyDataset())):
            parent = self.initial(workers=4, eval_steps=eval_steps, batch_size=4)
            # One case has evaluated before the step3 save; the other first
            # evaluates after resume. Their base-seed histories differ.
            full_trace = copy.deepcopy(self.trace)
            expected = full_trace[3 + (4 if eval_steps == 3 else 0):]
            original = tree_digest(parent.parent)
            state = continuation_checkpoint_state(parent, self.receipt)
            self.assertEqual(state["persistent_eval_started"], eval_steps == 3)
            first, first_state, actual = self.run_segment(parent, state, state["schedule"], first_end, "workers-first", eval_steps)
            self.assertEqual(first_state["cursor"]["offset"], 0 if first_end == 4 else 4)
            self.assertEqual(first_state["persistent_eval_started"], eval_steps == 3)
            first_digest = tree_digest(first.parent)
            second, second_state, part = self.run_segment(first, first_state, state["schedule"], second_end, "workers-second", eval_steps)
            actual += part
            second_digest = tree_digest(second.parent)
            final, final_state, part = self.run_segment(second, second_state, state["schedule"], 12, "workers-final", eval_steps)
            actual += part
            if state["persistent_eval_started"]:
                # Negative control, intentionally bypassing admission: losing the
                # EVAL history adds a seed draw and changes subsequent policy RNG.
                _, _, wrong = self.run_segment(parent, {**state, "persistent_eval_started": False},
                                               state["schedule"], 7, "wrong-eval-history", eval_steps)
                self.assertNotEqual(wrong[-1]["noise"], expected[len(wrong) - 1]["noise"])
        self.assertEqual(actual, expected)
        self.assertEqual([len(row["indices"]) for row in actual if row["training"]], [1, 4, 4, 4, 1, 4, 4, 4, 1])
        self.assertEqual(final_state["cursor"], {"num_frames": 13, "epoch": 3, "offset": 0, "samples_consumed": 39})
        self.assertTrue(final_state["persistent_eval_started"])
        reference = parent.parents[1] / "000012"
        for filename in ("optimizer_state.safetensors", "rng_state.safetensors"):
            a = load_file(reference / "training_state" / filename)
            b = load_file(final.parent / "training_state" / filename)
            for key in a:
                torch.testing.assert_close(a[key], b[key], rtol=0, atol=0)
        self.assertEqual(json.loads((reference / "training_state/scheduler_state.json").read_text()),
                         json.loads((final.parent / "training_state/scheduler_state.json").read_text()))
        for checkpoint, digest in ((parent, original), (first, first_digest), (second, second_digest)):
            self.assertEqual(tree_digest(checkpoint.parent), digest)

    def test_persistent_workers_match_legacy_then_two_resumes_with_evaluation(self):
        self.persistent_worker_case(eval_steps=3, first_end=4, second_end=7)

    def test_persistent_first_evaluation_keeps_seed_draw_after_resume(self):
        self.persistent_worker_case(eval_steps=6, first_end=5, second_end=9)

    def test_public_same_output_recovery_twice_with_native_saved_sources(self):
        from tests.data_factory.training_fixtures import admitted_case
        from tests.data_factory.training_fixtures import write_normalization_fixture
        from tools.data_factory.training_entrypoint import prepare_launch, continue_training, resume_training
        from tools.validate_training_checkpoint import validate_checkpoint
        from lerobot.configs.default import DatasetConfig
        from lerobot.configs.train import TrainPipelineConfig
        from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig

        fixture = self.root / "admission"
        fixture.mkdir()
        args, split = admitted_case(fixture)
        base = Path(args.checkpoint)
        receipt = json.loads((base.parents[2] / "fr5_training_receipt.json").read_text())
        policy_json = json.loads((base / "config.json").read_text())
        policy_json.pop("path", None)
        policy_json.update(type="smolvla", device="cpu", push_to_hub=False)
        (base / "config.json").write_text(json.dumps(policy_json))
        save_file({"weight": torch.tensor([.1, -.2])}, base / "model.safetensors")
        cfg = TrainPipelineConfig(
            dataset=DatasetConfig(repo_id=args.repo_id, root=args.dataset,
                                  episodes=split["selected_episodes"], eval_split=split["eval_split"]),
            policy=SmolVLAConfig.from_pretrained(base), output_dir=self.root / "native-parent",
            steps=4, batch_size=1, num_workers=0, save_freq=1, eval_steps=4, log_freq=0,
            rename_map=json.loads(next(x.split("=", 1)[1] for x in receipt["normalized_argv"]
                                       if x.startswith("--rename_map="))))
        cfg.policy.pretrained_path = base
        argv = continuation_argv(base, receipt, output=cfg.output_dir, steps=4,
                                  batch_size=1, eval_steps=4, save_freq=1, schedule="hold")
        argv = [arg for arg in argv if not arg.startswith("--fr5.")]
        argv = ["--policy.path=lerobot/smolvla_base" if arg.startswith("--policy.path=") else arg for arg in argv]
        split, receipt = prepare_launch(dataset=args.dataset, repo_id=args.repo_id,
            inventory=args.approved_inventory, profile="smolvla",
            collection_profile=split["feature_contract"]["collection_profile_id"], argv=argv)

        def dataset(episodes):
            value = TinyDataset()
            value.episodes, value.num_episodes, value.num_frames = episodes, len(episodes), 2 * len(episodes)
            value.meta = SimpleNamespace(episodes={"dataset_from_index": [0, 2, 4, 6],
                "dataset_to_index": [2, 4, 6, 8]}, has_language_columns=False, camera_keys=[], stats={})
            value.absolute_to_relative_idx = {2 * episode + offset: 2 * index + offset
                for index, episode in enumerate(episodes) for offset in range(2)}
            return value

        class BoundProcessor(IdentityProcessor):
            def save_pretrained(self, directory):
                write_normalization_fixture(directory, receipt)

        with patch.object(self.trainer, "make_train_eval_datasets", side_effect=lambda _: (
                dataset(split["train_episodes"]), dataset(split["eval_episodes"]))), patch.object(
                self.trainer, "make_pre_post_processors", return_value=(BoundProcessor(), BoundProcessor())):
            with patch("sys.argv", ["tiny"]):
                self.trainer.train(cfg)
            for name, value in (("fr5_training_split.json", split), ("fr5_training_receipt.json", receipt)):
                (cfg.output_dir / name).write_text(json.dumps(value))
            parent = cfg.output_dir / "checkpoints/000001/pretrained_model"
            validate_checkpoint(parent)
            parent_digest = tree_digest(parent.parent)

            def launch(output):
                return continue_training(parent, output=output, inventory=args.approved_inventory,
                    steps=7, batch_size=4, eval_steps=4, save_freq=2, schedule="hold")

            self.trace.clear()
            reference = self.root / "public-reference"
            self.assertEqual(launch(reference), 0)
            expected = copy.deepcopy(self.trace)
            self.trace.clear()
            output = self.root / "public-recovered"
            native_last = self.trainer.update_last_checkpoint

            def interrupt_after_save(checkpoint):
                native_last(checkpoint)
                raise InterruptedError("injected interruption after native checkpoint")

            with patch.object(self.trainer, "update_last_checkpoint", side_effect=interrupt_after_save):
                with self.assertRaises(InterruptedError):
                    launch(output)
            first = output / "checkpoints/000002/pretrained_model"
            first_digest = tree_digest(first.parent)
            # Simulate abrupt termination before launch's finally published manifests.
            for name in ("fr5_training_split.json", "fr5_training_receipt.json"):
                (output / name).rename(Path(str(output) + f".{name}.pending"))
            with patch.object(self.trainer, "update_last_checkpoint", side_effect=interrupt_after_save):
                with self.assertRaises(InterruptedError):
                    resume_training(first)
            second = output / "checkpoints/000004/pretrained_model"
            self.assertEqual(json.loads((second / "train_config.json").read_text())["policy"]["pretrained_path"], str(first))
            validate_checkpoint(second)
            second_digest = tree_digest(second.parent)
            self.assertEqual(resume_training(second), 0)
            final = output / "checkpoints/000007/pretrained_model"
            validate_checkpoint(final)
            self.assertEqual(self.trace, expected)
            self.assertEqual(tree_digest(parent.parent), parent_digest)
            self.assertEqual(tree_digest(first.parent), first_digest)
            self.assertEqual(tree_digest(second.parent), second_digest)
            for filename in ("optimizer_state.safetensors", "rng_state.safetensors"):
                expected_tensors = load_file(reference / "checkpoints/000007/training_state" / filename)
                actual_tensors = load_file(final.parent / "training_state" / filename)
                for key in expected_tensors:
                    torch.testing.assert_close(actual_tensors[key], expected_tensors[key], rtol=0, atol=0)
            for filename in ("scheduler_state.json", CONTINUATION_STATE):
                self.assertEqual(json.loads((reference / "checkpoints/000007/training_state" / filename).read_text()),
                                 json.loads((final.parent / "training_state" / filename).read_text()))
            config_file = final / "train_config.json"
            saved = json.loads(config_file.read_text())
            self.assertTrue(saved["resume"])
            self.assertEqual(saved["policy"]["pretrained_path"], str(second))
            saved["policy"]["pretrained_path"] = str(reference / "checkpoints/000004/pretrained_model")
            config_file.write_text(json.dumps(saved))
            with self.assertRaisesRegex(ValueError, "source differs from lineage"):
                validate_checkpoint(final)


class ContinuationCursorTest(unittest.TestCase):
    def test_cursor_counts_committed_partial_batches_after_batch_change(self):
        cursor = {"num_frames": 13, "epoch": 0, "offset": 0}
        cursor = advance_sample_cursor(cursor, 3, 1)
        cursor = advance_sample_cursor(cursor, 2, 4)
        self.assertEqual(cursor["samples_consumed"], 11)
        cursor = advance_sample_cursor(cursor, 4, 2)
        self.assertEqual(cursor, {"num_frames": 13, "epoch": 1, "offset": 6, "samples_consumed": 19})
        with self.assertRaises(ValueError):
            advance_sample_cursor(cursor, -1, 2)


class ContinuationAdmissionTest(unittest.TestCase):
    """Existing temporary approval fixtures; no video generation or model loading."""
    def setUp(self):
        from tests.data_factory.training_fixtures import admitted_case
        self.tmp = TemporaryDirectory(prefix="SYNTHETIC_CONTINUATION_ADMISSION-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.args, self.split = admitted_case(self.root)
        self.parent = Path(self.args.checkpoint)
        self.output = self.root / "outputs/continued"
        self.receipt_path = self.parent.parents[2] / "fr5_training_receipt.json"
        self.receipt = json.loads(self.receipt_path.read_text())
        cfg = json.loads((self.parent / "train_config.json").read_text())
        cfg.update(steps=2, batch_size=2, num_workers=0, resume=False, checkpoint_path=None,
                   scheduler={"type": "cosine_decay_with_warmup"})
        cfg["policy"].update(type="smolvla", use_amp=False, pretrained_path="lerobot/smolvla_base")
        (self.parent / "train_config.json").write_text(json.dumps(cfg))
        training = self.parent.parent / "training_state"
        for name, value in (("training_step.json", {"step": 1, "batch_size": 2, "num_processes": 1}),
                            ("scheduler_state.json", {"last_epoch": 1, "_last_lr": [.0001]}),
                            ("optimizer_param_groups.json", [{"lr": .0001}])):
            (training / name).write_text(json.dumps(value))
        self.argv = continuation_argv(self.parent, self.receipt, output=self.output,
            steps=6, batch_size=4, eval_steps=2, save_freq=2, schedule="hold")
        self.kwargs = dict(dataset=self.args.dataset, repo_id=self.args.repo_id,
            inventory=self.args.approved_inventory, profile="smolvla",
            collection_profile=self.split["feature_contract"]["collection_profile_id"], argv=self.argv)

    def persistent_parent(self):
        config_file = self.parent / "train_config.json"
        cfg = json.loads(config_file.read_text())
        cfg.update(num_workers=4, prefetch_factor=4, persistent_workers=True,
                   dataloader_multiprocessing_context="spawn", eval_steps=1)
        config_file.write_text(json.dumps(cfg))
        return config_file, cfg

    def test_persistent_parent_admission_and_entrypoint_inherit_workers_and_eval_history(self):
        self.persistent_parent()
        self.test_admission_binds_parent_without_reset_and_dry_run_uses_existing_launch()
        self.test_native_entrypoint_uses_parent_state_and_only_allowed_native_overrides()
        from tools.data_factory.training_entrypoint import prepare_launch
        _, receipt = prepare_launch(**self.kwargs)
        self.assertTrue(receipt["initialization"]["persistent_eval_started"])

    def test_unsupported_workers_or_stochastic_transforms_still_fail(self):
        config_file, cfg = self.persistent_parent()
        for changes in ({"num_workers": 2}, {"num_workers": True}, {"prefetch_factor": 2},
                        {"persistent_workers": False}, {"dataloader_multiprocessing_context": "fork"},
                        {"dataset": {**cfg["dataset"], "image_transforms": {"enable": True}}}):
            with self.subTest(changes=changes):
                config_file.write_text(json.dumps({**cfg, **changes}))
                with self.assertRaisesRegex(ValueError, "deterministic transforms"):
                    continuation_checkpoint_state(self.parent, self.receipt)
        self.assertFalse(self.output.exists())

    def test_admission_binds_parent_without_reset_and_dry_run_uses_existing_launch(self):
        from tools.data_factory.training_entrypoint import prepare_launch, continue_training
        from contextlib import redirect_stdout
        import io
        before = tree_digest(self.parent.parents[2])
        _, receipt = prepare_launch(**self.kwargs)
        initial = receipt["initialization"]
        self.assertEqual(initial["mode"], "continuation")
        self.assertEqual(initial["reset"], [])
        self.assertEqual(initial["schedule"], {"native_horizon": 2, "hold_from_step": 1})
        self.assertEqual(initial["cursor"]["samples_consumed"], 2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(continue_training(self.parent, output=self.output,
                inventory=self.args.approved_inventory, steps=6, batch_size=4, eval_steps=2,
                save_freq=2, schedule="hold", dry_run=True), 0)
        self.assertFalse(self.output.exists())
        self.assertEqual(tree_digest(self.parent.parents[2]), before)

    def test_recipe_parent_history_and_output_changes_fail_before_publication(self):
        from tools.data_factory.training_entrypoint import prepare_launch, launch
        for key, value in (("--policy.optimizer_lr", "0.1"), ("--output_dir", str(self.parent.parents[2])),
                           ("--steps", "1"), ("--fr5.continuation_schedule", "rewarm")):
            with self.subTest(key=key):
                argv = [arg for arg in self.argv if not arg.startswith(key + "=")] + [f"{key}={value}"]
                with self.assertRaises(ValueError):
                    prepare_launch(**{**self.kwargs, "argv": argv})
        cfg_file = self.parent / "train_config.json"
        cfg = json.loads(cfg_file.read_text())
        cfg["resume"] = True
        cfg_file.write_text(json.dumps(cfg))
        with self.assertRaisesRegex(ValueError, "not reconstructible"):
            launch(**self.kwargs, runner=lambda *_: self.fail("runner reached"))
        self.assertFalse(self.output.exists())

    def test_native_entrypoint_uses_parent_state_and_only_allowed_native_overrides(self):
        import sys
        from lerobot.scripts import lerobot_train
        from tools.data_factory.training_entrypoint import prepare_launch, _run_native_training, options
        _, receipt = prepare_launch(**self.kwargs)
        seen = []
        with patch.object(lerobot_train, "main", side_effect=lambda: seen.append(options(sys.argv[1:]))):
            self.assertEqual(_run_native_training(self.argv, self.split, receipt), 0)
        self.assertEqual(seen, [{"--resume": "true", "--config_path": str(self.parent / "train_config.json"),
            "--output_dir": str(self.output), "--steps": "6", "--batch_size": "4",
            "--eval_steps": "2", "--save_freq": "2"}])

    def test_saved_child_and_second_binding_revalidate_all_ancestors(self):
        import shutil
        from tools.data_factory.training_entrypoint import prepare_launch
        from tools.validate_training_checkpoint import validate_checkpoint, continuation_binding
        split, receipt = prepare_launch(**self.kwargs)
        self.output.mkdir()
        for name, value in (("fr5_training_split.json", split), ("fr5_training_receipt.json", receipt)):
            (self.output / name).write_text(json.dumps(value))
        child = self.output / "checkpoints/000006/pretrained_model"
        shutil.copytree(self.parent.parent, child.parent)
        config_file = child / "train_config.json"
        cfg = json.loads(config_file.read_text())
        cfg.update(steps=6, batch_size=4, eval_steps=2, save_freq=2, resume=True, output_dir=str(self.output))
        cfg["policy"]["pretrained_path"] = str(self.parent)
        config_file.write_text(json.dumps(cfg))
        training = child.parent / "training_state"
        (training / "training_step.json").write_text(json.dumps({"step": 6, "batch_size": 4, "num_processes": 1}))
        (training / "scheduler_state.json").write_text(json.dumps({"last_epoch": 6, "_last_lr": [.0001]}))
        initial = receipt["initialization"]
        state = {"schema_version": "fr5-native-continuation-state-v1", "step": 6,
                 "cursor": advance_sample_cursor(initial["cursor"], 5, 4), "schedule": initial["schedule"],
                 "python_gauss": None, "numpy_gauss": 0.0}
        if "persistent_eval_started" in initial:
            state.update(schema_version="fr5-native-continuation-state-v2", persistent_eval_started=True)
        (training / CONTINUATION_STATE).write_text(json.dumps(state))
        self.assertEqual(validate_checkpoint(child), (child, self.output))
        if "persistent_eval_started" in state:
            for wrong in (False, 1, None):
                (training / CONTINUATION_STATE).write_text(json.dumps({**state, "persistent_eval_started": wrong}))
                with self.assertRaisesRegex(ValueError, "committed state"):
                    continuation_checkpoint_state(child, receipt)
            (training / CONTINUATION_STATE).write_text(json.dumps(state))
        argv = continuation_argv(child, receipt, output=self.root / "outputs/second", steps=10,
                                  batch_size=2, eval_steps=2, save_freq=2, schedule="preserve")
        binding = continuation_binding(child, split, receipt["normalization"], argv)
        self.assertEqual(binding["cursor"], state["cursor"])
        self.assertEqual(binding["schedule"], initial["schedule"])
        # Mutated ancestor bytes invalidate the already bound child.
        (self.parent / "model.safetensors").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "differs|binding|provenance changed"):
            validate_checkpoint(child)

    def test_persistent_child_eval_history_and_ancestor_binding_are_validated(self):
        self.persistent_parent()
        self.test_saved_child_and_second_binding_revalidate_all_ancestors()

    def test_persistent_child_records_first_eval_after_legacy_parent(self):
        config_file, cfg = self.persistent_parent()
        cfg["eval_steps"] = 2  # legacy step1 has not evaluated yet
        config_file.write_text(json.dumps(cfg))
        self.assertFalse(continuation_checkpoint_state(self.parent, self.receipt)["persistent_eval_started"])
        self.test_saved_child_and_second_binding_revalidate_all_ancestors()

    def test_public_wrapper_dry_run_and_mutual_exclusion(self):
        import os
        import subprocess
        project = Path(__file__).resolve().parents[1]
        command = [str(project / "scripts/train_policy.sh"), "--continue-from", str(self.parent),
                   "--output", str(self.output), "--approved-inventory", str(self.args.approved_inventory),
                   "--dry-run", "--steps", "6", "--batch-size", "4", "--eval-steps", "2",
                   "--save-freq", "2", "--continuation-schedule", "hold"]
        result = subprocess.run(command, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"mode": "continuation"', result.stdout)
        self.assertFalse(self.output.exists())
        result = subprocess.run(command[:1] + ["--resume-from", str(self.parent)] + command[1:],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
