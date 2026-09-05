from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import unittest
from unittest import mock

from tests.data_factory import test_training_approval as training_fixtures
from tools.data_factory import run_job, training_approval
from tools.data_factory.episode_ledger import project_episode_state
from tools.data_factory.training_entrypoint import prepare_approvals
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.workflow import selection
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
        with (
            mock.patch.object(training_approval, "_confirm_human_training_approval") as confirm,
            mock.patch.object(training_approval, "_prepare_training_approval") as approval_draft,
        ):
            result = export_training_request([run], target, dataset_id="selection-r1")
            request = load_json_strict(target)
            dataset, drafts = prepare_approvals(request, output, "curator-preview-only")
            confirm.assert_not_called()
            approval_draft.assert_not_called()
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
        with self.assertRaisesRegex(CuratorError, "TRAINING_APPROVAL_SCOPE"):
            export_training_request([run], target, dataset_id="selection-r1")
        self.assertEqual(list(output.iterdir()), [])
        fixture, run, output = self.case()
        (fixture.dataset / "meta/quarantine.json").write_text("{}")
        with self.assertRaisesRegex(CuratorError, "TRAINING_DATASET_QUARANTINED"):
            export_training_request([run], output / "request.json", dataset_id="selection-r1")
        self.assertEqual(list(output.iterdir()), [])

    def test_native_metadata_failure_prevents_request_publication(self):
        fixture, run, output = self.case()
        info = fixture.dataset / "meta/info.json"
        metadata = load_json_strict(info)
        metadata["total_episodes"] = 0
        info.write_text(json.dumps(metadata))
        before = snapshot(fixture.dataset), snapshot(run)
        with self.assertRaisesRegex(CuratorError, "TRAINING_METADATA_EPISODES"):
            export_training_request([run], output / "request.json", dataset_id="selection-r1")
        self.assertEqual(list(output.iterdir()), [])
        self.assertEqual((snapshot(fixture.dataset), snapshot(run)), before)

    def test_changed_current_review_is_reopened_for_each_new_request(self):
        """Manual fixture rebinding tests freshness, not supported review transitions."""
        fixture, run, output = self.case()
        original = output / "original.json"
        export_training_request([run], original, dataset_id="selection-r1")
        original_bytes = original.read_bytes()
        ledger = load_json_strict(run / "episode_ledger.json")
        ledger_bytes = (run / "episode_ledger.json").read_bytes()
        for semantic in ("FAIL", "PENDING", "UNCERTAIN", "PASS"):
            with self.subTest(semantic=semantic):
                candidate = fixture._candidate(ledger, semantic, name=f"later-{semantic}.json")
                state = project_episode_state(ledger=ledger, candidate=candidate)
                fixture._json("episode_ledger_state.json", state)
                target = output / f"request-{semantic}.json"
                before = snapshot(fixture.dataset), snapshot(run)
                if semantic == "PASS":
                    export_training_request([run], target, dataset_id="selection-r2")
                    self.assertEqual(
                        load_json_strict(target)["episodes"][0]["human_semantic_evidence_path"],
                        candidate["artifact_path"],
                    )
                else:
                    with self.assertRaisesRegex(CuratorError, "SELECTION_REVIEW_REQUIRED"):
                        export_training_request([run], target, dataset_id="selection-r2")
                    self.assertFalse(target.exists())
                self.assertEqual((snapshot(fixture.dataset), snapshot(run)), before)
                self.assertEqual(original.read_bytes(), original_bytes)
                self.assertEqual((run / "episode_ledger.json").read_bytes(), ledger_bytes)

    def test_review_changes_during_preparation_prevent_publication(self):
        """Inject fixture changes; canonical reviewed decisions cannot change to FAIL."""
        for semantic in ("FAIL", "PENDING", "UNCERTAIN", "PASS"):
            with self.subTest(semantic=semantic):
                fixture, run, output = self.case()
                ledger = load_json_strict(run / "episode_ledger.json")
                before = snapshot(fixture.dataset)
                original_candidate = (run / "candidate.json").read_bytes()
                target = output / "request.json"

                def prepare_then_change(*args, **kwargs):
                    result = prepare_approvals(*args, **kwargs)
                    candidate = fixture._candidate(ledger, semantic, name="later.json")
                    state = project_episode_state(ledger=ledger, candidate=candidate)
                    fixture._json("episode_ledger_state.json", state)
                    return result

                with (
                    mock.patch.object(selection, "prepare_approvals", side_effect=prepare_then_change),
                    self.assertRaisesRegex(CuratorError, "SELECTION_INPUT_CHANGED"),
                ):
                    export_training_request([run], target, dataset_id="selection-r1")
                self.assertEqual(list(output.iterdir()), [])
                self.assertEqual(snapshot(fixture.dataset), before)
                self.assertEqual((run / "candidate.json").read_bytes(), original_candidate)
                self.assertEqual(load_json_strict(run / "episode_ledger.json"), ledger)
                self.assertEqual(
                    load_json_strict(run / "episode_ledger_state.json")["review"]["semantic_status"],
                    semantic,
                )

    def test_concurrent_requests_publish_exactly_one_complete_request(self):
        fixture, run, output = self.case()
        before = snapshot(fixture.dataset), snapshot(run)
        target = output / "request.json"
        ready = Barrier(2)

        def prepare_together(*args, **kwargs):
            result = prepare_approvals(*args, **kwargs)
            ready.wait(timeout=5)
            return result

        def export(dataset_id):
            try:
                export_training_request([run], target, dataset_id=dataset_id)
                return dataset_id, "PUBLISHED"
            except CuratorError as exc:
                return dataset_id, exc.code

        with (
            mock.patch.object(selection, "prepare_approvals", side_effect=prepare_together),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = list(executor.map(export, ("selection-a", "selection-b")))
        self.assertCountEqual([status for _, status in results], ["PUBLISHED", "EVENT_EXISTS"])
        winner = next(dataset_id for dataset_id, status in results if status == "PUBLISHED")
        request = load_json_strict(target)
        self.assertEqual(request["dataset_id"], winner)
        self.assertEqual([entry["episode_index"] for entry in request["episodes"]], [0])
        self.assertEqual(list(output.iterdir()), [target])
        self.assertEqual((snapshot(fixture.dataset), snapshot(run)), before)

    def test_canonical_review_to_request_rejects_pass_to_fail_and_preserves_replay(self):
        fixture, run, output = self.case(semantic="PENDING")
        ledger = load_json_strict(run / "episode_ledger.json")
        pending = fixture._candidate(ledger, "PENDING", name="candidate_admission.json")
        fixture._json("episode_ledger_state.json", project_episode_state(ledger=ledger, candidate=pending))
        # Only initial evidence is a fixture; actual decisions use the public API.
        reviewed = run_job.apply_episode_review(
            run, semantic_status="PASS", reviewed_by="synthetic-reviewer",
        )
        target = output / "request.json"
        export_training_request([run], target, dataset_id="selection-r1")
        before = snapshot(fixture.dataset), snapshot(run), snapshot(output)
        with self.assertRaisesRegex(ContractError, "CANDIDATE_REVIEW_STATE"):
            run_job.apply_episode_review(
                run, semantic_status="FAIL", reviewed_by="synthetic-reviewer", reason="TASK_GOAL",
            )
        self.assertEqual(
            run_job.apply_episode_review(run, semantic_status="PASS", reviewed_by="synthetic-reviewer"),
            reviewed,
        )
        _, drafts = prepare_approvals(load_json_strict(target), output, "curator-preview-only")
        self.assertEqual(len(drafts), 1)
        self.assertEqual((snapshot(fixture.dataset), snapshot(run), snapshot(output)), before)
        self.assertEqual(list(output.iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
