"""CPU-only saved-processor tests; model and admission are explicit synthetic seams."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import torch
from safetensors.numpy import save_file

from tools.data_factory.learned_action_adapter import NativeSmolVLA, fake_rgb
from tools.fr5_data_factory import ContractError


def saved_processor_fixture(policy_dir, fault=None):
    """Reusable CPU fixture for Learning's validator and actual Rollout processing.

    Both state files contain identical admitted tensors; only configuration is
    changed. No checkpoint admission, policy weights or dataset are fabricated.
    """
    stats = {"observation.state": {"mean": [1.] * 7, "std": [2.] * 7},
             "action": {"mean": [.01] * 7, "std": [2.] * 7}}
    tensors = {f"{key}.{name}": np.asarray(value, dtype=np.float32)
               for key, values in stats.items() for name, value in values.items()}
    for pipeline, registry in (("policy_preprocessor", "normalizer_processor"),
                               ("policy_postprocessor", "unnormalizer_processor")):
        config = {"features": {"observation.state": {"type": "STATE", "shape": [7]},
                               "action": {"type": "ACTION", "shape": [7]}},
                  "norm_map": {"STATE": "MEAN_STD", "ACTION": "MEAN_STD"}, "eps": 1e-8}
        if pipeline == "policy_preprocessor":
            if fault == "excluded":
                config["normalize_observation_keys"] = []
            elif fault == "missing":
                config["features"].pop("observation.state")
            elif fault == "wrong-type":
                config["features"]["observation.state"]["type"] = "VISUAL"
            elif fault == "identity":
                config["norm_map"]["STATE"] = "IDENTITY"
        if fault == ("inline-state" if pipeline == "policy_preprocessor" else "inline-action"):
            feature = "observation.state" if pipeline == "policy_preprocessor" else "action"
            config["stats"] = {feature: {"mean": [0.] * 7, "std": [1.] * 7}}
        state_file = pipeline + ".safetensors"
        steps = [{"registry_name": registry, "config": config, "state_file": state_file}]
        if pipeline == "policy_preprocessor":
            steps.insert(0, {"registry_name": "to_batch_processor", "config": {}})
        (policy_dir / (pipeline + ".json")).write_text(json.dumps({"name": pipeline, "steps": steps}))
        save_file(tensors, policy_dir / state_file)
    return {"stats": stats}


class SavedProcessorBypassTest(unittest.TestCase):
    def test_config_bypasses_identical_saved_tensors_in_installed_cpu_processors(self):
        from lerobot.policies.factory import make_pre_post_processors
        from safetensors.numpy import load_file
        with tempfile.TemporaryDirectory() as directory:
            policy_dir = Path(directory)
            baseline = saved_processor_fixture(policy_dir)
            pre, post = make_pre_post_processors(SimpleNamespace(), pretrained_path=directory)
            torch.testing.assert_close(pre({"observation.state": torch.zeros(7)})["observation.state"],
                                       torch.full((1, 7), -.5))
            torch.testing.assert_close(post(torch.zeros((1, 1, 7))), torch.full((1, 1, 7), .01))
            for fault in ("excluded", "missing", "wrong-type", "identity", "inline-state", "inline-action"):
                with self.subTest(fault=fault):
                    self.assertEqual(saved_processor_fixture(policy_dir, fault), baseline)
                    for pipeline in ("policy_preprocessor", "policy_postprocessor"):
                        saved = load_file(policy_dir / (pipeline + ".safetensors"))
                        for key, values in baseline["stats"].items():
                            for stat, value in values.items():
                                np.testing.assert_array_equal(saved[f"{key}.{stat}"], np.asarray(value, dtype=np.float32))
                    pre, post = make_pre_post_processors(SimpleNamespace(), pretrained_path=directory)
                    actual = (post(torch.zeros((1, 1, 7))) if fault == "inline-action" else
                              pre({"observation.state": torch.zeros(7)})["observation.state"])
                    torch.testing.assert_close(actual, torch.zeros_like(actual))


class NativePolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policy_dir = self.root / 'pretrained_model'
        self.policy_dir.mkdir()
        (self.root / 'fr5_training_receipt.json').write_text('{}')
        save_file({'synthetic.weight': np.ones(1, dtype=np.float32)}, self.policy_dir / 'model.safetensors')
        self.mean_action = np.array([.01] * 6 + [.012], dtype=np.float32)
        for name, registry, feature, mean in (
            ('policy_preprocessor', 'normalizer_processor', 'observation.state', np.ones(7, dtype=np.float32)),
            ('policy_postprocessor', 'unnormalizer_processor', 'action', self.mean_action),
        ):
            config = {'features': {feature: {'type': 'STATE' if feature == 'observation.state' else 'ACTION', 'shape': [7]}},
                      'norm_map': {'STATE': 'MEAN_STD', 'ACTION': 'MEAN_STD'}, 'eps': 1e-8}
            steps = [{'registry_name': 'device_processor', 'config': {'device': 'cpu'}}]
            if name == 'policy_preprocessor':
                steps.insert(0, {'registry_name': 'to_batch_processor', 'config': {}})
            steps.append({'registry_name': registry, 'config': config, 'state_file': name + '.safetensors'})
            (self.policy_dir / (name + '.json')).write_text(json.dumps({'name': name, 'steps': steps}))
            save_file({feature + '.mean': mean, feature + '.std': np.full(7, 2., dtype=np.float32)},
                      self.policy_dir / (name + '.safetensors'))
        self.admission = mock.patch('tools.validate_training_checkpoint.validate_checkpoint', return_value=(self.policy_dir, self.root))
        self.admission.start()
        self.addCleanup(self.admission.stop)
        self.offline = mock.patch.dict(os.environ, {'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'})
        self.offline.start()
        self.addCleanup(self.offline.stop)

    def test_saved_pre_post_normalization_and_chunk_api_are_consumed(self):
        from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig
        from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy
        policy = mock.Mock()
        def predict(batch):
            self.assertTrue(torch.allclose(batch['observation.state'], torch.full((1, 7), -.5)))
            self.assertEqual(tuple(batch['observation.images.camera1'].shape), (1, 3, 1, 1))
            return torch.zeros((1, 2, 7))
        policy.predict_action_chunk.side_effect = predict
        config = SimpleNamespace(input_features={'observation.state': SimpleNamespace(shape=[7]),
            'observation.images.camera1': object(), 'observation.images.camera2': object()},
            output_features={'action': SimpleNamespace(shape=[7])}, adapt_to_pi_aloha=False, rtc_config=None)
        with mock.patch.object(SmolVLAConfig, 'from_pretrained', return_value=config), \
             mock.patch.object(SmolVLAPolicy, 'from_pretrained', return_value=policy) as load:
            native = NativeSmolVLA.load(self.policy_dir)
        self.assertTrue(load.call_args.kwargs['strict'])
        self.assertTrue(load.call_args.kwargs['local_files_only'])
        value = {'observation.state': [0.] * 7, 'observation.images.camera1': fake_rgb(),
                 'observation.images.camera2': fake_rgb(), 'task': 'synthetic probe'}
        actions = native(value)
        np.testing.assert_allclose(actions, [self.mean_action, self.mean_action])
        policy.forward.assert_not_called()
        policy.reset.assert_called_once()
        # The same saved-processor/model seam is consumed by the real OneJob and
        # canonical validator; only the model and transport are synthetic.
        from tools.data_factory.one_job import OneJob
        from tools.data_factory.motion.pickup_executor import PickupExecutor
        from tools.data_factory.rollout.finite_plan import FinitePolicyInference
        from tests.data_factory.rollout.test_finite_plan import source, observation, XML, Transport, SCENE
        transport = Transport()
        transport.current = [0.] * 7
        executor = PickupExecutor(transport, source_clock=lambda: 10.)
        job = OneJob(lambda _: self.fail("plan-only recorder effect"), executor.process)
        original = source()
        original["steps"][-1]["limits"]["execution_timeout_s"] = 6.
        obs = observation()
        obs["observation.state"] = [0.] * 7
        result = job.plan_learned("native-probe", original, SCENE,
            FinitePolicyInference(native, native.checkpoint, source_clock=lambda: 10.), obs,
            instruction="synthetic probe", robot_description=XML, period_s=1.5)
        self.assertTrue(result["ok"], result)
        frozen = result["plan_envelope"]["plan"]["learned_proposal"]
        self.assertEqual(frozen["checkpoint"], native.checkpoint)
        np.testing.assert_allclose(frozen["actions"], [self.mean_action, self.mean_action])
        self.assertEqual(transport.sent, [])
        from tools.data_factory.run_job import run_learned_plan_only
        import threading
        import time
        (self.root / "robot.urdf").write_text(XML)
        transport2 = Transport()
        transport2.current = [0.] * 7
        executor2 = PickupExecutor(transport2)
        child = SimpleNamespace(request=lambda request, _cancel: executor2.process(request), close=lambda **_: None)
        def capture():
            value = observation()
            value["observation.state"] = [0.] * 7
            value["source_timestamps_s"] = {key: time.time() for key in ("state", "camera1", "camera2")}
            return value
        payload = {"run_id": "native-runner", "urdf": str(self.root / "robot.urdf")}
        with mock.patch.object(NativeSmolVLA, "load", return_value=native):
            result = run_learned_plan_only(payload, threading.Event(), lambda _: None,
                checkpoint=self.policy_dir, observation=capture, instruction="synthetic probe", period_s=1.5,
                resolver=lambda _: ({"normalized_job": {}, "resolved_job_digest": original["resolved_job_digest"]}, original, SCENE),
                executor_factory=lambda _: child)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["finite_learned_plan"]["plan"]["execution_kind"], "FINITE_LEARNED_PROBE")
        self.assertEqual(transport2.sent, [])
        (self.policy_dir / 'model.safetensors').write_bytes(b'changed')
        with self.assertRaisesRegex(ContractError, 'LEARNED_CHECKPOINT_CHANGED'):
            native(value)

    def test_empty_weights_and_missing_normalization_never_reach_model_load(self):
        with mock.patch.object(NativeSmolVLA, '_load_components') as components:
            (self.policy_dir / 'model.safetensors').write_bytes(b'')
            with self.assertRaisesRegex(ContractError, 'LEARNED_CHECKPOINT_LOAD_FAILED'):
                NativeSmolVLA.load(self.policy_dir)
            components.assert_not_called()
        save_file({'synthetic.weight': np.ones(1, dtype=np.float32)}, self.policy_dir / 'model.safetensors')
        (self.policy_dir / 'policy_postprocessor.safetensors').unlink()
        with mock.patch.object(NativeSmolVLA, '_load_components') as components:
            with self.assertRaisesRegex(ContractError, 'LEARNED_CHECKPOINT_LOAD_FAILED'):
                NativeSmolVLA.load(self.policy_dir)
            components.assert_not_called()

    def test_separate_inference_consumers_cannot_reset_one_active_native_model(self):
        from lerobot.policies.factory import make_pre_post_processors
        from tools.data_factory.rollout.finite_plan import FinitePolicyInference
        from tests.data_factory.rollout.test_finite_plan import observation, XML
        pre, post = make_pre_post_processors(SimpleNamespace(), pretrained_path=str(self.policy_dir))
        entered, release = threading.Event(), threading.Event()
        policy = mock.Mock()
        def predict(_):
            if policy.predict_action_chunk.call_count == 1:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test consumer did not release inference")
            return torch.zeros((1, 1, 7))
        policy.predict_action_chunk.side_effect = predict
        with mock.patch.object(NativeSmolVLA, "_load_components", return_value=(policy, pre, post)):
            native = NativeSmolVLA.load(self.policy_dir)
        def propose():
            return FinitePolicyInference(native, native.checkpoint, source_clock=lambda: 10.,
                monotonic_clock=lambda: 10.).propose(observation(), instruction="synthetic probe",
                                                  robot_description=XML, period_s=1.5)
        completed, errors = [], []
        def first_consumer():
            try:
                completed.append(propose())
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=first_consumer)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(ContractError, "LEARNED_POLICY_FAILED") as rejected:
                propose()
            self.assertEqual(str(rejected.exception.__cause__), "LEARNED_REENTRANT_INFERENCE")
            self.assertEqual(policy.reset.call_count, 1)
            self.assertEqual(policy.predict_action_chunk.call_count, 1)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(completed), 1)
        np.testing.assert_allclose(propose()["actions"], completed[0]["actions"])
        policy.predict_action_chunk.side_effect = RuntimeError("synthetic inference failure")
        with self.assertRaisesRegex(ContractError, "LEARNED_POLICY_FAILED"):
            propose()
        policy.predict_action_chunk.side_effect = predict
        np.testing.assert_allclose(propose()["actions"], completed[0]["actions"])

    def test_saved_feature_contract_cannot_disable_state_normalization(self):
        cases = (
            ("policy_preprocessor", "observation.state", "missing"),
            ("policy_preprocessor", "observation.state", "wrong-type"),
            ("policy_preprocessor", "observation.state", "wrong-shape"),
            ("policy_preprocessor", "observation.state", "excluded"),
            ("policy_preprocessor", "observation.state", "string-filter"),
            ("policy_postprocessor", "action", "missing"),
            ("policy_postprocessor", "action", "wrong-type"),
            ("policy_postprocessor", "action", "wrong-shape"),
        )
        for filename, feature, fault in cases:
            with self.subTest(filename=filename, fault=fault):
                path = self.policy_dir / (filename + ".json")
                original = path.read_bytes()
                document = json.loads(original)
                config = document["steps"][-1]["config"]
                if fault == "missing":
                    config["features"].pop(feature)
                elif fault == "wrong-type":
                    config["features"][feature]["type"] = "VISUAL"
                elif fault == "wrong-shape":
                    config["features"][feature]["shape"] = [6]
                elif fault == "excluded":
                    config["normalize_observation_keys"] = []
                else:
                    config["normalize_observation_keys"] = "observation.state"
                path.write_text(json.dumps(document))
                try:
                    with mock.patch.object(NativeSmolVLA, "_load_components", return_value=(object(), object(), object())) as components:
                        with self.assertRaisesRegex(ContractError, "LEARNED_PROCESSOR_FEATURES"):
                            NativeSmolVLA.load(self.policy_dir)
                        components.assert_not_called()
                finally:
                    path.write_bytes(original)

    def test_explicit_state_filter_consumes_saved_normalization(self):
        from lerobot.policies.factory import make_pre_post_processors
        path = self.policy_dir / "policy_preprocessor.json"
        document = json.loads(path.read_text())
        config = document["steps"][-1]["config"]
        batch = {"observation.state": torch.zeros(7)}
        # Reproduce LeRobot's silent bypass independently of our loader guard.
        config["normalize_observation_keys"] = []
        path.write_text(json.dumps(document))
        pre, _ = make_pre_post_processors(SimpleNamespace(), pretrained_path=str(self.policy_dir))
        torch.testing.assert_close(pre(batch)["observation.state"], torch.zeros((1, 7)))
        config["normalize_observation_keys"] = ["observation.state"]
        path.write_text(json.dumps(document))
        pre, post = make_pre_post_processors(SimpleNamespace(), pretrained_path=str(self.policy_dir))
        with mock.patch.object(NativeSmolVLA, "_load_components", return_value=(mock.Mock(), pre, post)):
            native = NativeSmolVLA.load(self.policy_dir)
        torch.testing.assert_close(native.preprocessor(batch)["observation.state"], torch.full((1, 7), -.5))

    def test_native_model_load_exception_is_typed_and_not_a_receipt(self):
        with mock.patch.object(NativeSmolVLA, '_load_components', side_effect=RuntimeError('tensor mismatch')):
            with self.assertRaisesRegex(ContractError, 'LEARNED_CHECKPOINT_LOAD_FAILED'):
                NativeSmolVLA.load(self.policy_dir)

    def test_inline_statistics_cannot_override_valid_saved_tensors(self):
        from lerobot.policies.factory import make_pre_post_processors
        for filename, feature in (("policy_preprocessor", "observation.state"),
                                  ("policy_postprocessor", "action")):
            with self.subTest(filename=filename):
                path = self.policy_dir / (filename + ".json")
                original = path.read_bytes()
                document = json.loads(original)
                document["steps"][-1]["config"]["stats"] = {
                    feature: {"mean": [0.] * 7, "std": [1.] * 7}}
                path.write_text(json.dumps(document))
                try:
                    # The installed library uses inline stats instead of its
                    # saved nonzero means; native admission must reject this.
                    pre, post = make_pre_post_processors(SimpleNamespace(), pretrained_path=str(self.policy_dir))
                    actual = (pre({feature: torch.zeros(7)})[feature]
                              if feature == "observation.state" else post(torch.zeros((1, 1, 7))))
                    torch.testing.assert_close(actual, torch.zeros_like(actual))
                    with mock.patch.object(NativeSmolVLA, "_load_components") as components:
                        with self.assertRaisesRegex(ContractError, "LEARNED_PROCESSOR_NORMALIZATION"):
                            NativeSmolVLA.load(self.policy_dir)
                        components.assert_not_called()
                finally:
                    path.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
