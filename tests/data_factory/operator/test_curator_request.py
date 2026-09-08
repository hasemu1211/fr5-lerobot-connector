"""Actual operator composition -> native Curator export, with no media encoding."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest
from unittest import mock

from tests.data_factory.curator.workflow import test_selection as selection_fixture
from tests.data_factory import test_episode_ledger as ledger_fixture
from tests.data_factory.operator import test_object_position as position
from tools.data_factory.episode_ledger import project_episode_state
from tools.data_factory.curator.workflow import selection
from tools.data_factory.operator.web.bridge import LoopbackBridge
from tools.data_factory.training_entrypoint import prepare_approvals
from tools.fr5_data_factory import ContractError, load_json_strict

snapshot = selection_fixture.snapshot
ROOT = Path(__file__).resolve().parents[3]


class CuratorRequestTests(unittest.TestCase):
    def setUp(self):
        self.native = position.ObjectPositionContinuityTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.root = self.native.root
        self.sequence = 0
        self.fixtures = []
        self.add_source()
        self.app = self.native.application("curator-request")

    def add_source(self, semantic="PASS"):
        original = ledger_fixture.EpisodeLedgerTest.setUp
        run_id = f"selected-run-{len(self.fixtures)}"
        def setup(fixture):
            original(fixture)
            fixture.run_id = run_id
            fixture.episode_ref["transaction_id"] = f"{run_id}:episode-000000"
            fixture.evidence = self.root / "outputs/data_factory/runs" / run_id
            fixture.evidence.mkdir(parents=True)
        with mock.patch.object(ledger_fixture.EpisodeLedgerTest, "setUp", setup):
            fixture, run, _ = selection_fixture.SelectionTest.case(self, semantic=semantic)
        ledger = load_json_strict(run / "episode_ledger.json")
        candidate = fixture._candidate(ledger, semantic, name="candidate_admission.json")
        fixture._json("episode_ledger_state.json", project_episode_state(ledger=ledger, candidate=candidate))
        self.fixtures.append(fixture)
        return run

    def send(self, op, payload=None, app=None):
        app = app or self.app
        self.sequence += 1
        return app.core.consume(self.native.request(app, op, payload or {}, f"request-{self.sequence}"))

    def selected(self):
        self.send("refresh_stored_reviews")
        stored = self.app.projection()["stored_reviews"]
        self.assertEqual(stored["status"], "READY", stored)
        self.assertEqual(len(stored["episodes"]), len(self.fixtures), stored)
        return {"items": [{key: episode[key] for key in ("run_id", "selection_digest")}
                          for episode in stored["episodes"]]}

    def result(self, app=None):
        return (app or self.app).projection()["stored_reviews"]["curator_request"]

    def target(self):
        return self.root / "outputs/curator/requests" / (self.result()["request_id"] + ".json")

    def test_actual_native_export_replay_restart_and_current_source_revalidation(self):
        payload = self.selected()
        before = [(snapshot(f.dataset), snapshot(f.evidence)) for f in self.fixtures]
        with mock.patch.object(selection, "export_training_request", wraps=selection.export_training_request) as export:
            self.send("export_curator_request", payload)
            self.assertEqual(self.result()["status"], "REQUEST_NOT_APPROVED", self.result())
            self.send("export_curator_request", payload)
            restarted = self.native.application("request-restarted")
            self.send("recover_curator_request", payload, restarted)
            export.assert_called_once()
        self.assertEqual(self.result(restarted)["status"], "REQUEST_NOT_APPROVED")
        target = self.target()
        contents = target.read_bytes()
        dataset, drafts = prepare_approvals(load_json_strict(target), target.parent, "curator-preview-only")
        self.assertEqual(len(drafts), 1)
        ledger = load_json_strict(self.fixtures[0].evidence / "episode_ledger.json")
        self.assertNotEqual(dataset["dataset_digest"], ledger["dataset"]["dataset_digest"])
        self.assertFalse(self.result()["training_authority"])
        self.assertEqual(self.result()["episodes"][0]["reviewed_by"], "reviewer-1")
        self.assertEqual(list(target.parent.iterdir()), [target])
        self.assertEqual(before, [(snapshot(f.dataset), snapshot(f.evidence)) for f in self.fixtures])
        self.assertNotIn(str(self.root), json.dumps(self.result()))
        self.native.forbidden.assert_not_called()
        # A published request does not excuse changed canonical source evidence.
        Path(ledger["artifacts"]["runtime_binding"]["artifact_path"]).write_text("{}")
        self.send("recover_curator_request", payload)
        self.assertEqual(self.result()["status"], "UNAVAILABLE")
        self.assertEqual(self.result()["publication"], "PRESENT")
        self.assertEqual(target.read_bytes(), contents)

    def test_pending_failed_mixed_and_stale_selections_are_not_silently_filtered(self):
        run = self.fixtures[0].evidence
        for semantic in ("PENDING", "FAIL"):
            ledger = load_json_strict(run / "episode_ledger.json")
            candidate = self.fixtures[0]._candidate(ledger, semantic, name="candidate_admission.json")
            self.fixtures[0]._json("episode_ledger_state.json", project_episode_state(ledger=ledger, candidate=candidate))
            self.send("export_curator_request", self.selected())
            self.assertEqual(self.result()["error"], "SELECTION_REVIEW_REQUIRED", self.result())
            self.assertFalse(self.target().exists())
        stale = self.selected()
        stale["items"][0]["selection_digest"] = "sha256:" + "0" * 64
        self.send("export_curator_request", stale)
        self.assertEqual(self.result()["error"], "CURATOR_REQUEST_SELECTION_CHANGED")
        ledger = load_json_strict(run / "episode_ledger.json")
        candidate = self.fixtures[0]._candidate(ledger, "PASS", name="candidate_admission.json")
        self.fixtures[0]._json("episode_ledger_state.json", project_episode_state(ledger=ledger, candidate=candidate))
        self.add_source()
        self.send("export_curator_request", self.selected())
        self.assertEqual(self.result()["error"], "SELECTION_DATASET_MISMATCH", self.result())
        self.assertFalse(self.target().exists())

    def test_absent_recovery_postpublication_loss_and_conflicting_output(self):
        payload = self.selected()
        with mock.patch.object(selection, "export_training_request", wraps=selection.export_training_request) as export:
            self.send("recover_curator_request", payload)
            self.assertEqual(self.result()["status"], "NOT_PUBLISHED")
            self.assertFalse(self.target().parent.exists())
            export.assert_not_called()
        original = selection.export_training_request
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("response lost after atomic publication")
        with mock.patch.object(selection, "export_training_request", side_effect=lost) as export:
            self.send("export_curator_request", payload)
            self.assertEqual(self.result()["status"], "REQUEST_NOT_APPROVED", self.result())
            export.assert_called_once()
        target = self.target()
        changed = load_json_strict(target)
        changed["episodes"] = []
        target.chmod(0o600)  # Simulate external corruption of the native read-only output.
        target.write_text(json.dumps(changed))
        before = target.read_bytes()
        self.send("export_curator_request", payload)
        self.assertEqual(self.result()["error"], "CURATOR_REQUEST_OUTPUT_CHANGED")
        self.assertEqual(target.read_bytes(), before)

    def test_public_selection_bounds_and_stale_view_reject_before_publication(self):
        payload = self.selected()
        stale_intent = self.native.request(self.app, "export_curator_request", payload, "stale-request")
        self.send("refresh_stored_reviews")
        with mock.patch.object(selection, "export_training_request") as export:
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_STALE_VIEW"):
                self.app.core.consume(stale_intent)
            for invalid in ({"items": []}, {"items": payload["items"] * 2},
                            {"items": payload["items"] * 65}, {**payload, "output": "/tmp/browser-choice"}):
                with self.assertRaisesRegex(ContractError, "CURATOR_REQUEST_SELECTION|OPERATOR_INTENT_OP_UNAVAILABLE"):
                    self.send("export_curator_request", invalid)
                self.send("refresh_stored_reviews")
            export.assert_not_called()
        self.assertFalse((self.root / "outputs/curator/requests").exists())
        self.native.forbidden.assert_not_called()

    def test_selection_cas_shares_native_review_lock_until_export_finishes(self):
        payload = self.selected()
        descriptor = os.open(self.fixtures[0].evidence, os.O_RDONLY | os.O_DIRECTORY)
        original = selection.export_training_request
        def export(*args, **kwargs):
            with self.assertRaises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original(*args, **kwargs)
        try:
            with mock.patch.object(selection, "export_training_request", side_effect=export) as producer:
                self.send("export_curator_request", payload)
                producer.assert_called_once()
            self.assertEqual(self.result()["status"], "REQUEST_NOT_APPROVED")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)

    def test_shipped_web_lost_response_recovers_native_request_without_post_replay(self):
        bridge = LoopbackBridge(core=self.app.core, ui_root=ROOT / "operator-ui", port=0)
        thread = threading.Thread(target=bridge.serve_forever)
        thread.start()
        try:
            process = subprocess.run(["node", "operator-ui/tests/stored-review-recovery.cjs", bridge.origin,
                "operator-ui/app.js", "selected-run-0", "request"], cwd=ROOT,
                capture_output=True, text=True, timeout=90)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result["methods"], ["POST", "GET"])
            self.assertEqual(result["request"]["status"], "REQUEST_NOT_APPROVED")
            self.assertIn("selected-run-0", result["requestCard"])
            self.assertTrue(result["previousRequestHidden"])
            self.assertFalse(result["request"]["training_authority"])
        finally:
            bridge.close()
            thread.join(5)
        self.native.forbidden.assert_not_called()
