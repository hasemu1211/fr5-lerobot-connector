#!/usr/bin/env python3

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.fr5_training_profile import build_profile, launch_feature_contract, instruction_task


def metadata(*cameras: str, action_dim: int = 7):
    features = {
        "action": {"dtype": "float32", "shape": [action_dim], "names": None},
        "observation.state": {"dtype": "float32", "shape": [7], "names": None},
    }
    for camera in cameras:
        features[f"observation.images.{camera}"] = {
            "dtype": "video",
            "shape": [480, 640, 3],
            "names": ["height", "width", "channels"],
        }
    return SimpleNamespace(
        features=features,
        camera_keys=[f"observation.images.{camera}" for camera in cameras],
    )


class TrainingProfileTest(unittest.TestCase):
    def test_smolvla_and_act_keep_fr5_features(self):
        smolvla = "\n".join(build_profile("smolvla", metadata("up", "side")))
        self.assertIn('"shape":[7]', smolvla)
        self.assertIn('"observation.images.up":"observation.images.camera1"', smolvla)
        self.assertIn('"observation.images.side":"observation.images.camera2"', smolvla)

        act = "\n".join(build_profile("act", metadata("up", "side")))
        self.assertIn("--policy.type=act", act)
        self.assertIn("observation.images.up", act)
        self.assertIn("observation.images.side", act)

    def test_vqbet_requires_the_selected_single_camera(self):
        vqbet = "\n".join(build_profile("vqbet-side", metadata("up", "side")))
        self.assertIn("--policy.type=vqbet", vqbet)
        self.assertIn("observation.images.side", vqbet)
        self.assertNotIn("observation.images.up", vqbet)
        with self.assertRaises(ValueError):
            build_profile("vqbet-wrist", metadata("up", "side"))

    def test_profiles_reject_non_fr5_action_dimensions(self):
        with self.assertRaises(ValueError):
            build_profile("act", metadata("up", action_dim=6))

    def test_launch_contract_uses_qualified_wrist_profile_and_task_source(self):
        from tools.fr5_data_factory import ContractError, TASK_CONTRACTS, canonical_digest, task_instruction
        info = {"features": metadata("up", "wrist").features, "fps": 30}
        contract = launch_feature_contract("smolvla", "fr5-up-wrist-rgb-30hz-v2", "pick_place", info)
        self.assertEqual(contract["task_contract_digest"], canonical_digest(TASK_CONTRACTS["pick_place"]))
        self.assertEqual(contract["camera_profile"], "up-wrist")
        self.assertIn('"observation.images.wrist":"observation.images.camera2"', "\n".join(contract["policy_argv"]))
        with self.assertRaisesRegex(ContractError, "TRAINING_COLLECTION_PROFILE"):
            launch_feature_contract("act", "fr5-up-side-rgb-30hz-v1", "pick_place", info)
        for task in TASK_CONTRACTS:
            self.assertEqual(instruction_task(task_instruction(task, "wood cube")), task)
        self.assertEqual(instruction_task(task_instruction("pick_place", "cube", source_region_id="RED",
            destination_region_id="BLUE", region_binding_active=True)), "pick_place")


class NativeImageAugmentationTest(unittest.TestCase):
    def test_existing_photometric_preset_is_native_train_only_and_accepts_uint8(self):
        import json
        from pathlib import Path
        import draccus
        import torch
        from lerobot.datasets import factory
        from lerobot.transforms.transforms import ImageTransforms, ImageTransformsConfig
        config = draccus.decode(ImageTransformsConfig, json.loads((Path(__file__).resolve().parents[1]
            / "config/image_transforms/light-photometric.json").read_text()))
        cfg = SimpleNamespace(dataset=SimpleNamespace(eval_split=.34, image_transforms=config,
            repo_id="local/tiny", root=Path("SYNTHETIC_ONLY"), revision=None,
            video_backend="torchcodec", use_imagenet_stats=False), trainable_config=None, tolerance_s=.0001)
        full = SimpleNamespace(episodes=[0, 1, 2], meta=SimpleNamespace(episodes={"tasks": [["pickup"]] * 3}))
        with patch.object(factory, "make_dataset", return_value=full), patch.object(
                factory, "resolve_delta_timestamps", return_value=None), patch.object(factory, "LeRobotDataset") as ctor:
            factory.make_train_eval_datasets(cfg)
        train, development = [call.kwargs for call in ctor.call_args_list]
        self.assertEqual(train["episodes"], [0])
        self.assertEqual(development["episodes"], [1, 2])
        self.assertIsNone(development["image_transforms"])
        transforms = train["image_transforms"]
        self.assertIsInstance(transforms, ImageTransforms)
        self.assertEqual(transforms.tf.n_subset, 2)
        self.assertNotIn("affine", transforms.transforms)
        image = torch.arange(3 * 16 * 16).reshape(3, 16, 16).remainder(256).to(torch.uint8)
        before = image.clone()
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(10)
            augmented = transforms(image)
        self.assertEqual(augmented.shape, image.shape)
        self.assertEqual(augmented.dtype, image.dtype)
        self.assertFalse(torch.equal(augmented, image))
        torch.testing.assert_close(image, before)


class NativeTrainingConfigurationTest(unittest.TestCase):
    """Exercise installed configuration/scheduler consumers on CPU, without a model or dataset."""

    def config(self, **kwargs):
        from lerobot.configs.default import DatasetConfig
        from lerobot.configs.train import TrainPipelineConfig
        from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig

        policy = kwargs.pop("policy", SmolVLAConfig(device="cpu", push_to_hub=False))
        config = TrainPipelineConfig(dataset=DatasetConfig(repo_id="local/config-test"), policy=policy, **kwargs)
        with patch("sys.argv", ["config-test"]):
            config.validate()
        return config

    def test_policy_preset_resolves_over_top_level_optimizer_override(self):
        from lerobot.optim.optimizers import AdamWConfig
        from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig

        overridden = self.config(optimizer=AdamWConfig(lr=5e-5))
        self.assertEqual(overridden.optimizer.lr, 1e-4)
        resolved = self.config(policy=SmolVLAConfig(device="cpu", push_to_hub=False, optimizer_lr=5e-5))
        self.assertEqual(resolved.optimizer.lr, 5e-5)
        self.assertEqual(resolved.scheduler.peak_lr, 5e-5)
        self.assertEqual(resolved.to_dict()["optimizer"]["lr"], 5e-5)

    def test_explicit_native_schedule_requires_disabling_policy_preset(self):
        from lerobot.optim.optimizers import AdamWConfig
        from lerobot.optim.schedulers import ConstantWithWarmupSchedulerConfig

        scheduler = ConstantWithWarmupSchedulerConfig(num_warmup_steps=20)
        resolved = self.config(use_policy_training_preset=False, optimizer=AdamWConfig(lr=5e-5),
                               scheduler=scheduler)
        self.assertIs(resolved.scheduler, scheduler)
        self.assertEqual(resolved.optimizer.lr, 5e-5)
        with self.assertRaisesRegex(ValueError, "Optimizer and Scheduler must be set"):
            self.config(use_policy_training_preset=False, optimizer=AdamWConfig(lr=5e-5))

    def test_same_parent_lower_lr_cli_preserves_matched_schedule_and_parent(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import torch
        from lerobot.configs.default import DatasetConfig
        from lerobot.configs.train import TrainPipelineConfig
        from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig

        # Resolve the real --policy.path/--policy.* consumer, without model weights,
        # dataset access or a trainer. Top-level --optimizer.lr alone is insufficient.
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            SmolVLAConfig(device="cpu", push_to_hub=False).save_pretrained(parent)
            original = (parent / "config.json").read_bytes()
            traces = []
            for peak in (1e-4, 5e-5):
                cfg = TrainPipelineConfig(dataset=DatasetConfig(repo_id="local/config-test"), steps=8000)
                with patch("sys.argv", ["config-test", f"--policy.path={parent}",
                        f"--policy.optimizer_lr={peak}", "--policy.scheduler_decay_lr=2.5e-6",
                        "--policy.scheduler_warmup_steps=1000", "--policy.scheduler_decay_steps=30000"]):
                    cfg.validate()
                self.assertFalse(cfg.resume)
                self.assertEqual(cfg.policy.pretrained_path, parent)
                self.assertEqual(cfg.optimizer.lr, peak)
                self.assertEqual(cfg.scheduler.peak_lr, peak)
                optimizer = cfg.optimizer.build([torch.nn.Parameter(torch.zeros(1))])
                scheduler = cfg.scheduler.build(optimizer, cfg.steps)
                lrs = [optimizer.param_groups[0]["lr"]]
                for _ in range(cfg.steps):
                    optimizer.step()
                    scheduler.step()
                    lrs.append(optimizer.param_groups[0]["lr"])
                self.assertAlmostEqual(lrs[0], peak / 267, places=12)
                self.assertAlmostEqual(lrs[4000], (peak + 2.5e-6) / 2, places=12)
                self.assertAlmostEqual(lrs[8000], 2.5e-6, places=12)
                self.assertEqual(max(range(len(lrs)), key=lrs.__getitem__), 266)
                traces.append(lrs)
            self.assertTrue(all(low < high for high, low in zip(traces[0][:-1], traces[1][:-1])))
            self.assertEqual((parent / "config.json").read_bytes(), original)

    def test_short_native_horizon_reaches_decay_floor_and_changes_schedule_prefix(self):
        import torch

        config = self.config(steps=200)
        parameter = torch.nn.Parameter(torch.zeros(1))
        optimizer = config.optimizer.build([parameter])
        scheduler = config.scheduler.build(optimizer, config.steps)
        short_initial_lr = optimizer.param_groups[0]["lr"]
        for _ in range(6):
            optimizer.step()
            scheduler.step()
        self.assertGreater(optimizer.param_groups[0]["lr"], 0.99 * config.optimizer.lr)
        for _ in range(194):
            optimizer.step()
            scheduler.step()
        self.assertAlmostEqual(optimizer.param_groups[0]["lr"], config.scheduler.decay_lr, places=12)
        # Saved nominal config alone does not describe the built schedule's warmup.
        self.assertEqual(config.to_dict()["scheduler"]["num_warmup_steps"], 1000)
        longer_optimizer = config.optimizer.build([parameter])
        config.scheduler.build(longer_optimizer, 1000)
        self.assertNotEqual(short_initial_lr, longer_optimizer.param_groups[0]["lr"])


if __name__ == "__main__":
    unittest.main()
