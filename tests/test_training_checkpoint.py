#!/usr/bin/env python3

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.validate_training_checkpoint import REQUIRED_TRAINING_STATE, normalize_policy_dir, validate_checkpoint


class TrainingCheckpointTest(unittest.TestCase):
    def test_native_smolvla_missing_slots_preserve_exact_admitted_images(self):
        import copy
        import draccus
        import torch
        from types import SimpleNamespace
        from lerobot.configs import FeatureType, PolicyFeature
        from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig
        from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy
        from tools.validate_training_checkpoint import validate_policy_feature_contract

        inputs = {name: {"type": "VISUAL", "shape": [3, 480, 640]}
                  for name in ("observation.images.camera1", "observation.images.camera2")}
        inputs["observation.state"] = {"type": "STATE", "shape": [7]}
        outputs = {"action": {"type": "ACTION", "shape": [7]}}
        feature = {"profile": "smolvla", "policy_argv": [
            "--policy.empty_cameras=1", "--policy.input_features=" + json.dumps(inputs),
            "--policy.output_features=" + json.dumps(outputs),
        ]}
        native = SmolVLAConfig(device="cpu", push_to_hub=False, empty_cameras=1,
                              resize_imgs_with_padding=(4, 4),
                              input_features={key: PolicyFeature(type=FeatureType(value["type"]),
                                                                 shape=tuple(value["shape"]))
                                              for key, value in inputs.items()},
                              output_features={"action": PolicyFeature(type=FeatureType.ACTION, shape=(7,))})
        # Reproduce the pretrained config's retained slot and the native serializer.
        native.input_features["observation.images.camera3"] = PolicyFeature(
            type=FeatureType.VISUAL, shape=(3, 256, 256))
        native.validate_features()
        saved = draccus.encode(native)
        validate_policy_feature_contract(saved, feature)
        baseline = copy.deepcopy(native)
        baseline.input_features.pop("observation.images.camera3")
        batch = {"observation.images.camera1": torch.full((1, 3, 4, 4), .2),
                 "observation.images.camera2": torch.full((1, 3, 4, 4), .8)}
        actual = SmolVLAPolicy.prepare_images(SimpleNamespace(config=native), batch)
        expected = SmolVLAPolicy.prepare_images(SimpleNamespace(config=baseline), batch)
        torch.testing.assert_close(actual, expected)
        self.assertEqual(len(actual[0]), 3)
        self.assertEqual([mask.tolist() for mask in actual[1]], [[True], [True], [False]])

        for fault in ("unknown", "placeholder_shape", "real_shape", "order", "empty_count", "action"):
            with self.subTest(fault=fault):
                altered = copy.deepcopy(saved)
                if fault == "unknown":
                    altered["input_features"]["observation.images.camera4"] = inputs["observation.images.camera1"]
                elif fault == "placeholder_shape":
                    altered["input_features"]["observation.images.camera3"]["shape"] = [3, 480, 640]
                elif fault == "real_shape":
                    altered["input_features"]["observation.images.camera2"]["shape"] = [3, 256, 256]
                elif fault == "order":
                    first = altered["input_features"].pop("observation.images.camera1")
                    altered["input_features"]["observation.images.camera1"] = first
                elif fault == "empty_count":
                    altered["empty_cameras"] = 2
                else:
                    altered["output_features"]["action"]["shape"] = [6]
                with self.assertRaisesRegex(ValueError, "checkpoint policy"):
                    validate_policy_feature_contract(altered, feature)

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


class CurrentTrainingCheckpointTest(unittest.TestCase):
    def test_saved_config_cannot_bypass_bound_tensors_in_installed_cpu_processors(self):
        import torch
        from lerobot.configs import FeatureType, PolicyFeature
        from lerobot.policies.act.configuration_act import ACTConfig
        from lerobot.policies.factory import make_pre_post_processors
        from safetensors.numpy import load_file
        from tools.validate_training_checkpoint import validate_normalization_state

        normalization = {"stats": {
            "observation.state": {"mean": [1.] * 7, "std": [2.] * 7},
            "action": {"mean": [.01] * 7, "std": [2.] * 7},
        }}
        config = ACTConfig(device="cpu", input_features={
            "observation.state": PolicyFeature(type=FeatureType.STATE, shape=(7,)),
        }, output_features={"action": PolicyFeature(type=FeatureType.ACTION, shape=(7,))})
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            policy = Path(directory)
            pre, post = make_pre_post_processors(config, dataset_stats=normalization["stats"])
            pre.save_pretrained(policy)
            post.save_pretrained(policy)
            validate_normalization_state(policy, normalization, profile="act")
            original = {path: path.read_text() for path in policy.glob("*.json")}
            # Rollout's CPU reproducer: identical saved tensors, six config-only
            # changes that skip, override or misapply the required transform.
            for fault in ("excluded", "missing", "wrong-type", "identity", "inline-state", "inline-action"):
                with self.subTest(fault=fault):
                    for path, content in original.items():
                        path.write_text(content)
                    inverse = fault == "inline-action"
                    path = policy / ("policy_postprocessor.json" if inverse else "policy_preprocessor.json")
                    saved = json.loads(path.read_text())
                    registry = "unnormalizer_processor" if inverse else "normalizer_processor"
                    step = next(step for step in saved["steps"] if step.get("registry_name") == registry)
                    processor = step["config"]
                    if fault == "excluded":
                        processor["normalize_observation_keys"] = []
                    elif fault == "missing":
                        processor["features"].pop("observation.state")
                    elif fault == "wrong-type":
                        processor["features"]["observation.state"]["type"] = "VISUAL"
                    elif fault == "identity":
                        processor["norm_map"]["STATE"] = "IDENTITY"
                    else:
                        key = "action" if inverse else "observation.state"
                        processor["stats"] = {key: {"mean": [0.] * 7, "std": [1.] * 7}}
                    path.write_text(json.dumps(saved))
                    tensors = load_file(policy / step["state_file"])
                    for key, stats in normalization["stats"].items():
                        for stat, value in stats.items():
                            torch.testing.assert_close(torch.from_numpy(tensors[f"{key}.{stat}"]), torch.tensor(value))
                    pre, post = make_pre_post_processors(config, pretrained_path=policy)
                    actual = (post(torch.zeros((1, 1, 7))) if inverse else
                              pre({"observation.state": torch.zeros(7)})["observation.state"])
                    if fault == "wrong-type":
                        # ACT normalizes VISUAL too, but reshapes its statistics
                        # for images: a mislabeled state broadcasts to 7x7.
                        self.assertNotEqual(tuple(actual.shape), (1, 7))
                    else:
                        torch.testing.assert_close(actual, torch.zeros_like(actual))
                    with self.assertRaisesRegex(ValueError, "normalization"):
                        validate_normalization_state(policy, normalization, profile="act")
            for path, content in original.items():
                path.write_text(content)
            pre, post = make_pre_post_processors(config, pretrained_path=policy)
            torch.testing.assert_close(pre({"observation.state": torch.zeros(7)})["observation.state"],
                                       torch.full((1, 7), -.5))
            torch.testing.assert_close(post(torch.zeros((1, 1, 7))), torch.full((1, 1, 7), .01))
            validate_normalization_state(policy, normalization, profile="act")

    def test_installed_cpu_processors_save_the_bound_statistics(self):
        from lerobot.configs import FeatureType, PolicyFeature
        from lerobot.policies.act.configuration_act import ACTConfig
        from lerobot.policies.vqbet.configuration_vqbet import VQBeTConfig
        from lerobot.policies.factory import make_pre_post_processors
        from tests.test_train_wrapper import launch_fixture
        from tools.data_factory.training_entrypoint import prepare_launch
        from tools.validate_training_checkpoint import validate_normalization_state

        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            kwargs, _, _ = launch_fixture(root)
            _, receipt = prepare_launch(**kwargs)
            for profile, policy_config in (("act", ACTConfig), ("vqbet-up", VQBeTConfig)):
                with self.subTest(profile=profile):
                    config = policy_config(device="cpu", input_features={
                        "observation.state": PolicyFeature(type=FeatureType.STATE, shape=(7,)),
                    }, output_features={"action": PolicyFeature(type=FeatureType.ACTION, shape=(7,))})
                    pre, post = make_pre_post_processors(config, dataset_stats=receipt["normalization"]["stats"])
                    policy = root / profile
                    pre.save_pretrained(policy)
                    post.save_pretrained(policy)
                    validate_normalization_state(policy, receipt["normalization"], profile=profile)
                    if profile == "vqbet-up":
                        with self.assertRaisesRegex(ValueError, "normalization mode"):
                            validate_normalization_state(policy, receipt["normalization"], profile="smolvla")

    def test_resume_rechecks_current_inventory_subset_and_same_count_provenance(self):
        import os
        from tests.test_train_wrapper import launch_fixture, write_normalization_fixture
        from tools.data_factory.training_entrypoint import prepare_launch, options
        from tools.data_factory.training_receipts import compile_launch_receipt
        with TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            kwargs, _, _ = launch_fixture(root)
            split, receipt = prepare_launch(**kwargs)
            output = root / "outputs/run"
            policy = output / "checkpoints/000100/pretrained_model"
            state = policy.parent / "training_state"
            state.mkdir(parents=True)
            policy.mkdir()
            (policy / "config.json").write_text("{}")
            (policy / "model.safetensors").touch()
            policy_cfg = {}
            for option, value in options(split["feature_contract"]["policy_argv"]).items():
                if option.startswith("--policy."):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        pass
                    policy_cfg[option.removeprefix("--policy.")] = value
            config = {"dataset": {"root": str(kwargs["dataset"]), "repo_id": kwargs["repo_id"], "episodes": [0, 2, 3], "eval_split": 0.34}, "policy": policy_cfg}
            (policy / "config.json").write_text(json.dumps(policy_cfg))
            (policy / "train_config.json").write_text(json.dumps(config))
            for name in REQUIRED_TRAINING_STATE:
                (state / name).write_text('{"step": 100}' if name == "training_step.json" else "")
            (output / "fr5_training_split.json").write_text(json.dumps(split))
            (output / "fr5_training_receipt.json").write_text(json.dumps(receipt))
            write_normalization_fixture(policy, receipt)
            from tools.data_factory.training_receipts import launch_receipt_digest
            self.assertEqual(receipt["receipt_digest"], launch_receipt_digest(receipt))
            self.assertEqual(validate_checkpoint(policy), (policy, output))
            config["dataset"]["root"] = os.path.relpath(kwargs["dataset"], Path.cwd())
            relative = Path(config["dataset"]["root"])
            relative_kwargs = {**kwargs, "dataset": relative, "argv": [
                f"--dataset.root={relative}" if arg.startswith("--dataset.root=") else arg
                for arg in kwargs["argv"]
            ]}
            relative_split, relative_receipt = prepare_launch(**relative_kwargs)
            self.assertEqual(relative_split, split)
            (output / "fr5_training_receipt.json").write_text(json.dumps(relative_receipt))
            (policy / "train_config.json").write_text(json.dumps(config))
            # A producer-shaped relative saved config must close the same
            # launch -> checkpoint -> reload contract as its absolute form.
            self.assertEqual(validate_checkpoint(policy), (policy, output))
            config["dataset"]["root"] = str(kwargs["dataset"])
            (output / "fr5_training_receipt.json").write_text(json.dumps(receipt))
            (policy / "train_config.json").write_text(json.dumps(config))
            # A pre-saved-view raw receipt remains valid when current launch
            # preparation now enriches raw launches with the explicit binding.
            legacy = compile_launch_receipt(
                split, receipt["normalized_argv"], receipt["approved_inventory_path"]
            )
            self.assertNotIn("observation_view", legacy)
            self.assertEqual(legacy["receipt_digest"], launch_receipt_digest(legacy))
            (output / "fr5_training_receipt.json").write_text(json.dumps(legacy))
            self.assertEqual(validate_checkpoint(policy), (policy, output))
            saved_policy = json.loads(json.dumps(policy_cfg))
            saved_policy["input_features"]["observation.state"]["shape"] = [6]
            (policy / "config.json").write_text(json.dumps(saved_policy))
            with self.assertRaisesRegex(ValueError, "feature contract"):
                validate_checkpoint(policy)
            (policy / "config.json").write_text(json.dumps(policy_cfg))
            from safetensors.numpy import load_file, save_file
            normalizer = policy / "policy_preprocessor_normalization.safetensors"
            tensors = load_file(normalizer)
            tensors["action.mean"] += 1
            save_file(tensors, normalizer)
            with self.assertRaisesRegex(ValueError, "normalization differs"):
                validate_checkpoint(policy)
            write_normalization_fixture(policy, receipt)
            config["dataset"]["episodes"] = [0, 1, 3]
            (policy / "train_config.json").write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "selection"):
                validate_checkpoint(policy)
            config["dataset"]["episodes"] = [0, 2, 3]
            (policy / "train_config.json").write_text(json.dumps(config))
            (kwargs["dataset"] / "meta/source_provenance/episode-000002.jsonl").write_text('{"frame_index":2}\n{"frame_index":1}\n')
            with self.assertRaisesRegex(ValueError, "TRAINING_DATASET_CHANGED"):
                validate_checkpoint(policy)


if __name__ == "__main__":
    unittest.main()
