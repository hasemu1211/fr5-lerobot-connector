"""Tiny CPU fields and native sampling code; no model download, GPU or device IO."""
import io
import json
import unittest
from contextlib import redirect_stdout

import torch

from tools.data_factory.rollout.solver_efficiency import (
    METHODS, compare_trials, integrate, main, tensor_digest,
)


class SolverEfficiencyTest(unittest.TestCase):
    def test_bfloat16_raw_byte_digest_and_action_dtype_metadata(self):
        import hashlib
        value = torch.tensor([[1., -2.], [3., 4.]], dtype=torch.bfloat16).transpose(0, 1)
        original = value.clone()
        raw = bytes(value.contiguous().view(torch.uint8).flatten().tolist())
        self.assertEqual(tensor_digest(value), hashlib.sha256(raw).hexdigest())
        self.assertEqual(tensor_digest(value), tensor_digest(value.contiguous()))
        self.assertNotEqual(tensor_digest(value), tensor_digest(value.float()))
        torch.testing.assert_close(value, original)
        def trial(noise, method):
            result, measured = integrate(lambda x, t: x, noise, method)
            return result.to(torch.bfloat16), measured, measured["solver_wall_s"]
        report = compare_trials(trial, (1, 2, 7), seeds=(0,), repeats=1, warmups=0)
        self.assertEqual(report["noise_dtype"], "float32")
        for row in report["rows"]:
            self.assertEqual(row["action_dtype"], "torch.bfloat16")
            self.assertEqual(row["action_shape"], [1, 2, 7])

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
