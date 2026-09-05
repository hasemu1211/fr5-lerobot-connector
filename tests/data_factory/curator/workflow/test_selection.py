from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest import mock

from tests.data_factory import test_training_approval as training_fixtures
from tools.data_factory import training_approval
from tools.data_factory.episode_ledger import project_episode_state
from tools.data_factory.training_entrypoint import prepare_approvals
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.workflow.selection import export_training_request
from tools.fr5_data_factory import ContractError, load_json_strict


snapshot = training_fixtures.snapshot


class SelectionTest(unittest.TestCase):
    def case(self, *, semantic="PASS", production=True):
        fixture, native_request, output = (
            training_fixtures.CollectionLedgerTrainingApprovalTest.ledger_case(
                self, production=production,
            )
        )
        ledger_path = Path(native_request["episodes"][0]["episode_ledger_path"])
        ledger = load_json_strict(ledger_path)
        candidate = fixture._candidate(ledger, semantic)
        state = project_episode_state(ledger=ledger, candidate=candidate)
        fixture._json("episode_ledger_state.json", state)
        return fixture, ledger_path.parent, output

    def test_selected_request_is_consumed_by_native_training_preparation_without_consent(self):
        fixture, run, output = self.case()
        before = snapshot(fixture.dataset), snapshot(run)
        target = output / "request.json"
        with mock.patch.object(training_approval, "_confirm_human_training_approval") as confirm:
            result = export_training_request([run], target, dataset_id="selection-r1")
            request = load_json_strict(target)
            dataset, drafts = prepare_approvals(request, output, "curator-preview-only")
            confirm.assert_not_called()
        self.assertEqual(result["status"], "REQUEST_NOT_APPROVED")
        self.assertFalse(result["training_authority"])
        self.assertEqual(result["episode_indices"], [0])
        self.assertEqual(request["dataset_id"], "selection-r1")
        self.assertNotEqual(
            dataset["dataset_digest"],
            load_json_strict(run / "episode_ledger.json")["dataset"]["dataset_digest"],
        )
        self.assertEqual(dataset["dataset_root"], str(fixture.dataset))
        self.assertEqual(drafts[0]["provenance"]["episode_ledger"]["artifact_path"], str(run / "episode_ledger.json"))
        self.assertEqual((snapshot(fixture.dataset), snapshot(run)), before)
        self.assertEqual(list(output.iterdir()), [target])

    def test_duplicate_and_mixed_dataset_selections_fail_without_output(self):
        _, run, output = self.case()
        _, other, _ = self.case()
        for runs, code in (
            ([], "SELECTION_RUNS_REQUIRED"),
            ([run, run], "SELECTION_DUPLICATE_EPISODE"),
            ([run, other], "SELECTION_DATASET_MISMATCH"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(CuratorError, code):
                export_training_request(runs, output / "request.json", dataset_id="selection-r1")
            self.assertEqual(list(output.iterdir()), [])

    def test_pending_and_changed_review_evidence_never_silently_drop_selected_run(self):
        for semantic in ("PENDING", "FAIL", "UNCERTAIN"):
            with self.subTest(semantic=semantic):
                _, run, output = self.case(semantic=semantic)
                with self.assertRaisesRegex(CuratorError, "SELECTION_REVIEW_REQUIRED"):
                    export_training_request([run], output / "request.json", dataset_id="selection-r1")
                self.assertEqual(list(output.iterdir()), [])
        _, run, output = self.case()
        state = load_json_strict(run / "episode_ledger_state.json")
        candidate = Path(state["candidate"]["artifact_path"])
        changed = load_json_strict(candidate)
        changed["reviewed_by"] = "changed-reviewer"
        candidate.write_text(json.dumps(changed))
        with self.assertRaisesRegex(CuratorError, "SELECTION_SOURCE_INVALID"):
            export_training_request([run], output / "request.json", dataset_id="selection-r1")
        self.assertEqual(list(output.iterdir()), [])

    def test_replay_preserves_existing_request_and_native_consumer_reopens_sources(self):
        _, run, output = self.case()
        target = output / "request.json"
        export_training_request([run], target, dataset_id="selection-r1")
        before = target.read_bytes()
        with self.assertRaisesRegex(CuratorError, "EVENT_EXISTS"):
            export_training_request([run], target, dataset_id="selection-r1")
        self.assertEqual(target.read_bytes(), before)
        ledger = load_json_strict(run / "episode_ledger.json")
        Path(ledger["artifacts"]["runtime_binding"]["artifact_path"]).write_text("{}")
        with self.assertRaises(ContractError):
            prepare_approvals(load_json_strict(target), output, "curator-preview-only")
        self.assertEqual(list(output.iterdir()), [target])
        self.assertEqual(target.read_bytes(), before)

    def test_output_cannot_overlap_source_or_follow_symlinks(self):
        fixture, run, output = self.case()
        before = snapshot(fixture.dataset), snapshot(run)
        for target in (fixture.dataset / "request.json", run / "request.json"):
            with self.assertRaisesRegex(CuratorError, "SELECTION_OUTPUT_OVERLAP"):
                export_training_request([run], target, dataset_id="selection-r1")
        linked = output / "linked"
        linked.symlink_to(fixture.dataset, target_is_directory=True)
        with self.assertRaisesRegex(CuratorError, "SELECTION_OUTPUT"):
            export_training_request([run], linked / "request.json", dataset_id="selection-r1")
        self.assertEqual((snapshot(fixture.dataset), snapshot(run)), before)

    def test_native_training_scope_and_quarantine_gates_are_preserved(self):
        _, run, output = self.case(production=False)
        target = output / "request.json"
        export_training_request([run], target, dataset_id="selection-r1")
        with self.assertRaisesRegex(ContractError, "TRAINING_APPROVAL_SCOPE"):
            prepare_approvals(load_json_strict(target), output, "curator-preview-only")
        self.assertEqual(list(output.iterdir()), [target])
        fixture, run, output = self.case()
        (fixture.dataset / "meta/quarantine.json").write_text("{}")
        with self.assertRaisesRegex(CuratorError, "TRAINING_DATASET_QUARANTINED"):
            export_training_request([run], output / "request.json", dataset_id="selection-r1")
        self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
