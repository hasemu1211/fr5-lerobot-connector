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
