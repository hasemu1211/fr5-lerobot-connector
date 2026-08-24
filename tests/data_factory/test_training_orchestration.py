"""Pure-fake checks for fail-closed offline training orchestration."""

from __future__ import annotations

import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

try:
    from tests.data_factory.test_software_contract import synthetic_bundle
except ModuleNotFoundError:  # unittest discovery loads this directory as top-level modules.
    from test_software_contract import synthetic_bundle
from tools.data_factory.training_approval import SYNTHETIC_SCOPE
from tools.data_factory.training_orchestration import (
    normalize_training_request,
    orchestrate_training,
    training_request_bytes,
)
from tools.data_factory.training_receipts import canonical_digest as receipt_digest
from tools.fr5_data_factory import ContractError, canonical_digest


def case(root: Path) -> tuple[dict, dict, dict]:
    bundle = synthetic_bundle(root)
    config = {"batch_size": 2, "policy": "smolvla", "steps": 100}
    train = copy.deepcopy(bundle["training_receipt"])
    train["config_digest"] = receipt_digest(config)
    reload_receipt = copy.deepcopy(bundle["reload_receipt"])
    reload_receipt["train_receipt_digest"] = receipt_digest(train)
    request = {
        "approved_inventory": bundle["approved_inventory"],
        "split": bundle["split"],
        "normalized_argv": train["normalized_argv"],
        "config": config,
        "runtime_versions": train["runtime_versions"],
        "training_seed": train["training_seed"],
        "repository_commit": train["repository_commit"],
        "source_digest": train["source_digest"],
        "profile_id": train["profile_id"],
        "profile_digest": train["profile_digest"],
    }
    return request, train, reload_receipt


class FakeBackend:
    def __init__(self, train: dict, reload_receipt: dict) -> None:
        self.train = copy.deepcopy(train)
        self.reload_receipt = copy.deepcopy(reload_receipt)
        self.timeline: list[str] = []
        self.contexts: dict[str, dict] = {}
        self.fail_stage: str | None = None
        self.partial_stage: str | None = None
        self.checkpoint_overrides: dict = {}

    def _start(self, stage: str, context: dict) -> bool:
        self.timeline.append(stage)
        self.contexts[stage] = context
        if self.fail_stage == stage:
            raise RuntimeError(f"fake {stage} failure")
        return self.partial_stage == stage

    def trainer(self, request: dict) -> dict:
        if self._start("trainer", request):
            return {"status": "PARTIAL"}
        return copy.deepcopy(self.train)

    def checkpoint_validator(self, context: dict) -> dict:
        if self._start("checkpoint", context):
            return {"status": "PASS"}
        train = context["training_receipt"]
        request = context["request"]
        return {
            "status": "PASS",
            "checkpoint_id": train["checkpoint_id"],
            "checkpoint_tree_digest": train["checkpoint_tree_digest"],
            "dataset_digest": request["dataset"]["dataset_digest"],
            "split_digest": request["split_digest"],
            **self.checkpoint_overrides,
        }

    def reloader(self, context: dict) -> dict:
        if self._start("reload", context):
            return {"reload_status": "PARTIAL"}
        return copy.deepcopy(self.reload_receipt)

    def evaluator(self, context: dict) -> dict:
        if self._start("evaluate", context):
            return {}
        return {
            "status": "PASS",
            "metric": "synthetic_offline_loss",
            "samples": 3,
            "loss_mean": 0.25,
        }


def run(request: dict, backend: FakeBackend, *, cancelled=None) -> dict:
    return orchestrate_training(
        request,
        trainer=backend.trainer,
        checkpoint_validator=backend.checkpoint_validator,
        reloader=backend.reloader,
        evaluator=backend.evaluator,
        cancelled=cancelled,
        expected_scope=SYNTHETIC_SCOPE,
    )


class TrainingOrchestrationTest(unittest.TestCase):
    def test_success_is_ordered_and_has_zero_real_effects_or_authority(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            request, train, reload_receipt = case(root)
            backend = FakeBackend(train, reload_receipt)
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*") if path.is_file()
            }

            result = run(request, backend)

            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*") if path.is_file()
            }
            self.assertEqual(backend.timeline, ["trainer", "checkpoint", "reload", "evaluate"])
            self.assertEqual(before, after)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["request_digest"], receipt_digest(result["request"]))
            self.assertEqual(result["training_receipt"], train)
            self.assertEqual(result["reload_receipt"], reload_receipt)
            self.assertFalse(result["production_artifact_issued"])
            self.assertFalse(result["human_authority"])
            self.assertFalse(result["training_authority"])

    def test_cancel_before_or_between_stages_stops_later_calls(self) -> None:
        with TemporaryDirectory() as directory:
            request, train, reload_receipt = case(Path(directory))
            before = FakeBackend(train, reload_receipt)
            with self.assertRaisesRegex(ContractError, "TRAINING_ORCHESTRATION_CANCELLED"):
                run(request, before, cancelled=lambda: True)
            self.assertEqual(before.timeline, [])

            after_train = FakeBackend(train, reload_receipt)
            with self.assertRaisesRegex(ContractError, "TRAINING_ORCHESTRATION_CANCELLED"):
                run(request, after_train, cancelled=lambda: after_train.timeline == ["trainer"])
            self.assertEqual(after_train.timeline, ["trainer"])

    def test_partial_output_from_any_stage_fails_closed(self) -> None:
        expected_timelines = {
            "trainer": ["trainer"],
            "checkpoint": ["trainer", "checkpoint"],
            "reload": ["trainer", "checkpoint", "reload"],
            "evaluate": ["trainer", "checkpoint", "reload", "evaluate"],
        }
        with TemporaryDirectory() as directory:
            request, train, reload_receipt = case(Path(directory))
            for stage, expected in expected_timelines.items():
                backend = FakeBackend(train, reload_receipt)
                backend.partial_stage = stage
                with self.subTest(stage=stage), self.assertRaises(ContractError):
                    run(request, backend)
                self.assertEqual(backend.timeline, expected)

    def test_dataset_split_and_checkpoint_digest_mismatches_stop_downstream(self) -> None:
        with TemporaryDirectory() as directory:
            request, train, reload_receipt = case(Path(directory))

            dataset_request = copy.deepcopy(request)
            dataset_request["split"]["dataset"]["dataset_root_identity_digest"] = receipt_digest("other")
            dataset_request["split"]["split_digest"] = canonical_digest({
                key: value for key, value in dataset_request["split"].items()
                if key != "split_digest"
            })
            backend = FakeBackend(train, reload_receipt)
            with self.assertRaisesRegex(ContractError, "TRAINING_ORCHESTRATION_DATASET"):
                run(dataset_request, backend)
            self.assertEqual(backend.timeline, [])

            bad_train = copy.deepcopy(train)
            bad_train["split_digest"] = receipt_digest("other-split")
            backend = FakeBackend(bad_train, reload_receipt)
            with self.assertRaisesRegex(ContractError, "TRAINING_ORCHESTRATION_TRAINING_RECEIPT"):
                run(request, backend)
            self.assertEqual(backend.timeline, ["trainer"])

            backend = FakeBackend(train, reload_receipt)
            backend.checkpoint_overrides["checkpoint_tree_digest"] = receipt_digest("other-checkpoint")
            with self.assertRaisesRegex(ContractError, "TRAINING_ORCHESTRATION_CHECKPOINT_BINDING"):
                run(request, backend)
            self.assertEqual(backend.timeline, ["trainer", "checkpoint"])

    def test_injected_stage_failures_never_call_later_stages(self) -> None:
        expected_timelines = {
            "trainer": ["trainer"],
            "reload": ["trainer", "checkpoint", "reload"],
            "evaluate": ["trainer", "checkpoint", "reload", "evaluate"],
        }
        error_stages = {"trainer": "TRAINER", "reload": "RELOADER", "evaluate": "EVALUATOR"}
        with TemporaryDirectory() as directory:
            request, train, reload_receipt = case(Path(directory))
            for stage, expected in expected_timelines.items():
                backend = FakeBackend(train, reload_receipt)
                backend.fail_stage = stage
                with self.subTest(stage=stage), self.assertRaisesRegex(
                    ContractError, f"TRAINING_ORCHESTRATION_{error_stages[stage]}_FAILED"
                ):
                    run(request, backend)
                self.assertEqual(backend.timeline, expected)

    def test_reload_must_be_independent_and_evaluation_waits_for_its_validation(self) -> None:
        with TemporaryDirectory() as directory:
            request, train, reload_receipt = case(Path(directory))
            reload_receipt["reload_process_id"] = train["process_id"]
            backend = FakeBackend(train, reload_receipt)
            with self.assertRaisesRegex(ContractError, "TRAINING_ORCHESTRATION_RELOAD_RECEIPT"):
                run(request, backend)
            self.assertEqual(backend.timeline, ["trainer", "checkpoint", "reload"])
            self.assertNotIn("evaluate", backend.contexts)

    def test_normalized_request_is_byte_stable_and_detached_from_input(self) -> None:
        with TemporaryDirectory() as directory:
            request, _, _ = case(Path(directory))
            reordered = copy.deepcopy(request)
            reordered["config"] = {
                "steps": request["config"]["steps"],
                "policy": request["config"]["policy"],
                "batch_size": request["config"]["batch_size"],
            }
            first = normalize_training_request(request, expected_scope=SYNTHETIC_SCOPE)
            second = normalize_training_request(reordered, expected_scope=SYNTHETIC_SCOPE)
            self.assertEqual(training_request_bytes(first), training_request_bytes(second))
            request["config"]["steps"] = 999
            self.assertEqual(first["config"]["steps"], 100)


if __name__ == "__main__":
    unittest.main()
