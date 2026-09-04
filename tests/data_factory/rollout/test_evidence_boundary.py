import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.data_factory.rollout.evidence_boundary import (
    ContractError, PACKET_SCHEMA, build_packet, inspect_directory, validate_packet,
)
from tools.fr5_data_factory import canonical_digest


class EvidenceBoundaryTest(unittest.TestCase):
    def _inputs(self):
        binding = {"task_id": "pick_place", "binding_digest": "sha256:" + "1" * 64}
        instruction = {"task_binding": binding, "binding_digest": "sha256:" + "2" * 64}
        ledger = {"episode": {"run_id": "run-1", "episode_ref_digest": "sha256:" + "3" * 64}, "ledger_digest": "sha256:" + "4" * 64, "admission": {"technical_status": "PASS"}}
        state = {"state_digest": "sha256:" + "5" * 64}
        candidate = {"run_id": "run-1", "semantic_status": "NOT_MEASURED"}
        return binding, instruction, ledger, state, candidate

    def test_unknowns_are_explicit_and_packet_is_reproducible(self):
        values = self._inputs()
        with patch("tools.data_factory.rollout.evidence_boundary.validate_task_binding", side_effect=lambda x: copy.deepcopy(x)), patch("tools.data_factory.rollout.evidence_boundary.validate_episode_instruction_binding", side_effect=lambda x: copy.deepcopy(x)), patch("tools.data_factory.rollout.evidence_boundary.validate_episode_ledger", side_effect=lambda x: copy.deepcopy(x)), patch("tools.data_factory.rollout.evidence_boundary.validate_episode_state", side_effect=lambda value, ledger: copy.deepcopy(value)):
            first = build_packet(task_binding=values[0], episode_instruction=values[1], ledger=values[2], state=values[3], candidate_admission=None)
            second = build_packet(task_binding=values[0], episode_instruction=values[1], ledger=values[2], state=values[3], candidate_admission=None)
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], PACKET_SCHEMA)
        self.assertEqual(first["rollout_evidence_analysis"]["effectiveness"], "UNKNOWN")
        self.assertEqual(first["rollout_evidence_analysis"]["training_authorization"], "UNKNOWN")
        self.assertEqual(validate_packet(first), first)

    def test_tampering_digest_is_rejected(self):
        values = self._inputs()
        with patch("tools.data_factory.rollout.evidence_boundary.validate_task_binding", side_effect=lambda x: copy.deepcopy(x)), patch("tools.data_factory.rollout.evidence_boundary.validate_episode_instruction_binding", side_effect=lambda x: copy.deepcopy(x)), patch("tools.data_factory.rollout.evidence_boundary.validate_episode_ledger", side_effect=lambda x: copy.deepcopy(x)), patch("tools.data_factory.rollout.evidence_boundary.validate_episode_state", side_effect=lambda value, ledger: copy.deepcopy(value)):
            packet = build_packet(task_binding=values[0], episode_instruction=values[1], ledger=values[2], state=values[3])
        packet["identity"]["task_id"] = "changed"
        with self.assertRaises(ContractError):
            validate_packet(packet)

    def test_stdout_inspection_reads_only_frozen_inputs(self):
        values = self._inputs()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for filename, value in zip(("task_binding.json", "episode_instruction.json", "episode_ledger.json", "episode_state.json"), values[:4]):
                (root / filename).write_text(json.dumps(value), encoding="utf-8")
            with patch("tools.data_factory.rollout.evidence_boundary.validate_task_binding", side_effect=lambda x: copy.deepcopy(x)), patch("tools.data_factory.rollout.evidence_boundary.validate_episode_instruction_binding", side_effect=lambda x: copy.deepcopy(x)), patch("tools.data_factory.rollout.evidence_boundary.validate_episode_ledger", side_effect=lambda x: copy.deepcopy(x)), patch("tools.data_factory.rollout.evidence_boundary.validate_episode_state", side_effect=lambda value, ledger: copy.deepcopy(value)):
                before = sorted(root.iterdir())
                packet = inspect_directory(root)
                self.assertEqual(before, sorted(root.iterdir()))
                self.assertEqual(packet["packet_digest"], canonical_digest({key: value for key, value in packet.items() if key != "packet_digest"}))


if __name__ == "__main__":
    unittest.main()
