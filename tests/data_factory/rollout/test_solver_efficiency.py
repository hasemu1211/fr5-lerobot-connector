"""Tiny CPU fields and native sampling code; no model download, GPU or device IO."""
import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from types import MethodType, SimpleNamespace
from unittest import mock

import torch

from tools.data_factory.rollout.solver_efficiency import (
    METHODS, compare_native, compare_trials, integrate, main, offline_solver,
)


class SolverEfficiencyTest(unittest.TestCase):
    def test_fixed_baselines_match_installed_euler_and_preserve_noise(self):
        from lerobot.policies.common.flow_matching import euler_integrate
        noise = torch.ones((1, 4, 7))
        for method, count in (("fixed10", 10), ("fixed5", 5)):
            result, evidence = integrate(lambda x, t: x, noise, method)
            torch.testing.assert_close(result, euler_integrate(lambda x, t: x, noise, count), rtol=0, atol=0)
            self.assertEqual(evidence["nfe"], count)
        torch.testing.assert_close(noise, torch.ones_like(noise))

    def test_constant_flow_finishes_in_two_evaluations_with_correct_direction(self):
        result, evidence = integrate(lambda x, t: torch.ones_like(x), torch.zeros((1, 4, 7)), "adaptive")
        torch.testing.assert_close(result, -torch.ones_like(result))
        self.assertEqual(evidence["nfe"], 2)
        self.assertEqual(evidence["terminal_t"], 0)
        self.assertFalse(evidence["forced_terminal_jump"])

    def test_high_change_has_bounded_evaluations_and_explicit_forced_tail(self):
        result, evidence = integrate(lambda x, t: x, torch.ones((1, 4, 7)), "adaptive", threshold=.001, max_nfe=4)
        self.assertEqual(evidence["nfe"], 4)
        self.assertTrue(evidence["forced_terminal_jump"])
        self.assertAlmostEqual(sum(step["step"] for step in evidence["steps"]), 1.)
        self.assertTrue(torch.isfinite(result).all())

    def test_zero_midpoint_change_is_not_an_error_or_safety_bound(self):
        noise = torch.zeros((1, 4, 7))
        field = lambda x, t: torch.ones_like(x) * ((t - .5)**2 * (t - 1)**2).reshape(-1, 1, 1)
        result, evidence = integrate(field, noise, "adaptive")
        reference, _ = integrate(field, noise, "fixed10")
        self.assertEqual(evidence["steps"][0]["relative_vector_change"], 0.)
        self.assertGreater((result - reference).abs().max().item(), .01)
        # The exact integral is 1/30; zero two-probe change misses the bend.
        self.assertAlmostEqual(abs(result[0, 0, 0].item() + 1 / 30), 1 / 30)

    def test_invalid_budget_and_nonfinite_field_rejected(self):
        noise = torch.zeros((1, 4, 7))
        for settings in ({"max_nfe": 1}, {"max_nfe": 3}, {"max_nfe": 42}, {"threshold": 0}, {"threshold": float('nan')}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                integrate(lambda x, t: x, noise, "adaptive", **settings)
        for method in METHODS:
            with self.subTest(method=method), self.assertRaises(ValueError):
                integrate(lambda x, t: x + float('nan'), noise, method)

    def test_paired_noise_counterbalanced_order_and_per_dimension_error(self):
        calls = []
        def trial(noise, method):
            calls.append(method)
            result, measured = integrate(lambda x, t: x, noise, method)
            return result, measured, measured["solver_wall_s"]
        report = compare_trials(trial, (1, 2, 7), seeds=(10,), repeats=3, warmups=0)
        self.assertEqual(calls[:3], ["fixed10", "fixed5", "adaptive"])
        self.assertEqual(calls[3:6], ["fixed5", "adaptive", "fixed10"])
        self.assertEqual(len({row["noise_sha256"] for row in report["rows"]}), 1)
        for row in report["rows"]:
            self.assertEqual(len(row["rmse_per_dimension_to_fixed10"]), 7)
            if row["method"] == "fixed10":
                self.assertEqual(row["max_abs_per_dimension_to_fixed10"], [0.] * 7)

    def test_installed_sample_actions_and_saved_processors_reach_offline_comparison(self):
        from lerobot.policies.smolvla import modeling_smolvla as module
        from lerobot.policies.factory import make_pre_post_processors
        from tools.data_factory.learned_action_adapter import NativeSmolVLA, fake_rgb
        from tests.data_factory.rollout.test_native_policy import NativePolicyTest
        fixture = NativePolicyTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config = SimpleNamespace(device="cpu", chunk_size=2, max_action_dim=32,
                                 num_steps=10, use_cache=True, rtc_config=None, adapt_to_pi_aloha=False)
        model = SimpleNamespace(config=config, rtc_processor=None, _rtc_enabled=lambda: False)
        model.embed_prefix = mock.Mock(return_value=(torch.zeros(1, 1, 2), torch.ones(1, 1, dtype=torch.bool),
                                                     torch.zeros(1, 1, dtype=torch.bool)))
        model.vlm_with_expert = SimpleNamespace(forward=mock.Mock(return_value=(None, None)))
        model.denoise_step = mock.Mock(side_effect=lambda **kw: torch.ones_like(kw["x_t"]))
        model.sample_actions = MethodType(module.VLAFlowMatching.sample_actions, model)
        original = model.sample_actions
        original_global = module.euler_integrate
        policy = SimpleNamespace(config=config, model=model, reset=mock.Mock())
        def predict(batch, noise):
            self.assertTrue(torch.allclose(batch["observation.state"], torch.full((1, 7), -.5)))
            return model.sample_actions(None, None, None, None, batch["observation.state"], noise=noise)[..., :7]
        policy.predict_action_chunk = predict
        pre, post = make_pre_post_processors(SimpleNamespace(), pretrained_path=str(fixture.policy_dir))
        with mock.patch.object(NativeSmolVLA, "_load_components", return_value=(policy, pre, post)):
            native = NativeSmolVLA.load(fixture.policy_dir)
        observation = {"observation.state": [0.] * 7, "task": "synthetic offline comparison",
                       "observation.images.camera1": fake_rgb(), "observation.images.camera2": fake_rgb()}
        original_observation = copy.deepcopy(observation)
        report = compare_native(native, observation, seeds=(0,), repeats=1, warmups=0)
        self.assertEqual(report["action_units"], ["rad"] * 6 + ["m"])
        self.assertEqual([row["nfe"] for row in report["rows"]], [10, 5, 2])
        self.assertEqual(model.embed_prefix.call_count, 3)
        self.assertEqual(model.denoise_step.call_count, 17)
        self.assertEqual(observation, original_observation)
        self.assertIs(model.sample_actions, original)
        self.assertIs(module.euler_integrate, original_global)
        self.assertEqual(config.num_steps, 10)
        with self.assertRaisesRegex(RuntimeError, "test unwind"):
            with offline_solver(model, lambda *args: None):
                raise RuntimeError("test unwind")
        self.assertIs(model.sample_actions, original)
        self.assertTrue(native._inference_lock.acquire(blocking=False))
        native._inference_lock.release()
        model.denoise_step.side_effect = RuntimeError("synthetic expert failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic expert failure"):
            compare_native(native, observation, seeds=(0,), repeats=1, warmups=0)
        self.assertIs(model.sample_actions, original)
        self.assertIs(module.euler_integrate, original_global)
        self.assertTrue(native._inference_lock.acquire(blocking=False))
        native._inference_lock.release()

    def test_synthetic_cli_labels_measurement_scope(self):
        output = io.StringIO()
        with redirect_stdout(output):
            main(["--synthetic", "--seeds", "0", "--repeats", "1", "--warmups", "0"])
        report = json.loads(output.getvalue())
        self.assertEqual(report["measurement_scope"], "synthetic_ode_only_not_model_speed")
        self.assertEqual(report["physical_qualification"], "NOT_MEASURED")
        self.assertEqual(set(report["cases"]), {"constant", "linear", "hidden_bend"})


if __name__ == '__main__':
    unittest.main()
