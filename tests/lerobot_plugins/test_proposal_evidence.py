import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch

from lerobot_strategy_fr5.chunk_tap import CapturedPolicyChunk
from lerobot_strategy_fr5.evidence import JsonlEvidenceSink, inspect_proposal_evidence
from lerobot_strategy_fr5.proposal_bridge import ACTION_KEYS, build_finite_proposal
from tools.data_factory.rollout.finite_plan import JOINTS
from tools.fr5_data_factory import ContractError, canonical_digest


XML = '<robot name="synthetic">' + ''.join(
    f'<joint name="{name}" type="{"prismatic" if i == 6 else "revolute"}">'
    f'<limit lower="{0 if i == 6 else -3}" upper="{.021 if i == 6 else 3}" '
    f'velocity="{.1 if i == 6 else 10}"/></joint>'
    for i, name in enumerate(JOINTS)) + '</robot>'


class ProposalEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "attempt.jsonl"
        self.raw = torch.zeros((1, 2, 7), dtype=torch.float32)
        self.raw[:, :, -1] = .0105
        self.capture = CapturedPolicyChunk(1, self.raw.clone(),
                                          hashlib.sha256(self.raw.numpy().tobytes()).hexdigest())
        self.observation = dict(zip(ACTION_KEYS, [0.] * 6 + [.0105]))
        self.observation.update(up=np.zeros((3, 4, 3), dtype=np.uint8),
                                wrist=np.zeros((3, 4, 3), dtype=np.uint8))
        self.kwargs = dict(
            robot_observation=self.observation,
            observation_evidence={"source_timestamps_s": {k: 10. for k in ("state", "up", "wrist")}},
            instruction="synthetic pick and place",
            checkpoint={"tree_digest": canonical_digest("synthetic-model"),
                        "training_receipt_digest": canonical_digest("synthetic-receipt"),
                        "runtime": "SYNTHETIC_TEST_ONLY"},
            robot_description=XML, period_s=1 / 30,
            inference_started_at_s=10., inference_completed_at_s=10.1, inference_duration_s=.1,
        )

    def create(self, *, blocked=False, close_run=True):
        processed = self.raw.clone()
        if blocked:
            processed[0, 0, 0] = .1
        with JsonlEvidenceSink(self.path, "synthetic-run") as sink:
            sink.emit("RUN_START", {"purpose": "SYNTHETIC_TEST_ONLY"})
            if blocked:
                with self.assertRaisesRegex(ContractError, "LEARNED_VELOCITY_LIMIT"):
                    build_finite_proposal(self.capture, processed, evidence_sink=sink, **self.kwargs)
            else:
                result = build_finite_proposal(self.capture, processed, evidence_sink=sink, **self.kwargs)
            if close_run:
                sink.emit("RUN_END", {"outcome": "BLOCKED" if blocked else "COMPLETED"})
        return None if blocked else result

    def records(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def rewrite(self, records, *, redigest=False):
        if redigest:
            for record in records:
                body = {k: v for k, v in record.items() if k != "event_digest"}
                record["event_digest"] = canonical_digest(body)
        self.path.write_text("".join(json.dumps(r) + "\n" for r in records))

    def test_same_proposal_with_and_without_tap_and_no_input_mutation(self):
        before = self.raw.clone()
        expected = build_finite_proposal(self.capture, self.raw, **self.kwargs)
        self.assertEqual(self.create(), expected)
        self.assertTrue(torch.equal(self.raw, before))
        report = inspect_proposal_evidence(self.path)
        diagnostic = report["diagnostics"][0]["diagnostic"]
        self.assertEqual(diagnostic["validation_status"], "VALID")
        self.assertEqual(diagnostic["task_outcome"], "NOT_EVALUATED")
        self.assertFalse(diagnostic["execution_authorized"])

    def test_velocity_block_retains_exact_raw_processed_and_observation(self):
        self.create(blocked=True)
        candidate = self.records()[1]["payload"]
        self.assertEqual(bytes.fromhex(candidate["raw_chunk"]["data_hex"]), self.raw.numpy().tobytes())
        self.assertEqual(candidate["observation_evidence"], self.kwargs["observation_evidence"])
        self.assertAlmostEqual(candidate["proposal_candidate"]["actions"][0][0], .1)
        self.assertNotEqual(candidate["raw_chunk"]["sha256"], candidate["processed_chunk"]["sha256"])
        diagnostic = inspect_proposal_evidence(self.path)["diagnostics"][0]["diagnostic"]
        self.assertEqual(diagnostic["code"], "LEARNED_VELOCITY_LIMIT")
        self.assertEqual(diagnostic["dispatch_scope"], "PROPOSAL_BUILDER_ONLY")
        self.assertEqual(diagnostic["data_deficit"], "UNKNOWN")
        self.assertNotIn("execution_trace", diagnostic)

    def test_candidate_is_written_before_validator_runs(self):
        from lerobot_strategy_fr5 import proposal_bridge
        original = proposal_bridge.validate_proposal
        def observed(proposal):
            self.assertEqual(self.records()[-1]["kind"], "PROPOSAL_CANDIDATE")
            return original(proposal)
        with patch.object(proposal_bridge, "validate_proposal", side_effect=observed):
            self.create()

    def test_missing_run_end_does_not_erase_completed_attempt(self):
        self.create(blocked=True, close_run=False)
        report = inspect_proposal_evidence(self.path)
        self.assertFalse(report["run_end_observed"])
        self.assertEqual(len(report["diagnostics"]), 1)

    def test_repeat_analysis_same_identity_and_no_file_mutation(self):
        self.create(blocked=True)
        before, stat = self.path.read_bytes(), self.path.stat()
        self.assertEqual(inspect_proposal_evidence(self.path), inspect_proposal_evidence(self.path))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, stat.st_mtime_ns)

    def test_missing_validation_is_incomplete_not_success(self):
        self.create()
        self.rewrite(self.records()[:2])
        report = inspect_proposal_evidence(self.path)
        self.assertEqual(report["diagnostics"], [])
        self.assertEqual(len(report["unfinished_candidate_events"]), 1)

    def test_mutation_reordering_truncation_and_duplicate_result_rejected(self):
        self.create()
        original = self.records()
        variants = []
        changed = copy.deepcopy(original)
        changed[1]["payload"]["proposal_candidate"]["actions"][0][0] = .5
        variants.append(changed)
        variants.append([original[0], original[2], original[1], original[3]])
        variants.append([original[0], original[1], original[3]])
        variants.append(original + [original[2]])
        for rows in variants:
            with self.subTest(rows=rows):
                self.rewrite(rows)
                with self.assertRaises(ContractError):
                    inspect_proposal_evidence(self.path)
        self.path.write_text(json.dumps(original[0])[:-2])
        with self.assertRaises(ContractError):
            inspect_proposal_evidence(self.path)

    def test_numerical_block_cannot_be_relabelled_valid(self):
        self.create(blocked=True)
        rows = self.records()
        rows[2]["payload"].update(status="VALID", code=None)
        self.rewrite(rows, redigest=True)
        with self.assertRaises(ContractError):
            inspect_proposal_evidence(self.path)

    def test_raw_chunk_corruption_even_with_redigested_events_rejected(self):
        self.create()
        rows = self.records()
        rows[1]["payload"]["raw_chunk"]["data_hex"] = "00"
        self.rewrite(rows, redigest=True)
        rows = self.records()
        rows[2]["payload"]["candidate_event_digest"] = rows[1]["event_digest"]
        self.rewrite(rows, redigest=True)
        with self.assertRaises(ContractError):
            inspect_proposal_evidence(self.path)

    def test_duplicate_json_keys_rejected(self):
        self.create()
        self.path.write_text(self.path.read_text().replace('"sequence":0', '"sequence":0,"sequence":0', 1))
        with self.assertRaises(ContractError):
            inspect_proposal_evidence(self.path)

    def test_storage_error_preserves_original_validation_exception(self):
        rejected = ContractError("LEARNED_VELOCITY_LIMIT")
        with JsonlEvidenceSink(self.path, "synthetic-run") as sink:
            emit = sink.emit
            def fail(kind, payload):
                if kind == "PROPOSAL_VALIDATION":
                    raise OSError("full")
                return emit(kind, payload)
            with patch.object(sink, "emit", side_effect=fail), patch(
                    "lerobot_strategy_fr5.proposal_bridge.validate_proposal", side_effect=rejected):
                with self.assertRaises(ContractError) as caught:
                    build_finite_proposal(self.capture, self.raw, evidence_sink=sink, **self.kwargs)
        self.assertIs(caught.exception, rejected)
        self.assertIn("FR5_EVIDENCE_PUBLICATION_FAILED", rejected.__notes__[0])
        self.assertEqual(inspect_proposal_evidence(self.path)["diagnostics"], [])

    def test_capture_mutation_is_not_silently_rebound(self):
        self.capture.raw[0, 0, 0] = 1.
        with JsonlEvidenceSink(self.path, "synthetic-run") as sink:
            with self.assertRaisesRegex(RuntimeError, "FR5_CAPTURE_DIGEST_CHANGED"):
                build_finite_proposal(self.capture, self.raw, evidence_sink=sink, **self.kwargs)

    def test_existing_file_not_overwritten(self):
        self.create()
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            JsonlEvidenceSink(self.path, "again")
        self.assertEqual(self.path.read_bytes(), before)

    def test_unexpected_validator_error_is_not_manipulation_failure(self):
        error = RuntimeError("synthetic validator crash")
        with JsonlEvidenceSink(self.path, "synthetic-run") as sink:
            with patch("lerobot_strategy_fr5.proposal_bridge.validate_proposal", side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    build_finite_proposal(self.capture, self.raw, evidence_sink=sink, **self.kwargs)
        self.assertIs(caught.exception, error)
        diagnostic = inspect_proposal_evidence(self.path)["diagnostics"][0]["diagnostic"]
        self.assertEqual(diagnostic["validation_status"], "ERROR")
        self.assertEqual(diagnostic["task_outcome"], "NOT_EVALUATED")


if __name__ == "__main__":
    unittest.main()
