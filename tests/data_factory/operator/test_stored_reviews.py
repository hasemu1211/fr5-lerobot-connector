"""Committed-review restart and CAS through real application composition."""
import hashlib
import json
import subprocess
import threading
import unittest
from unittest import mock

from tests.data_factory.test_collection_recommendation import RecommendationFixture, ROOT
from tests.data_factory.operator import test_object_position as position_fixture
from tools.data_factory import run_job
from tools.data_factory.operator.web.bridge import LoopbackBridge
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


class StoredReviewTests(unittest.TestCase):
    def setUp(self):
        self.native = position_fixture.ObjectPositionContinuityTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.root = self.native.root
        fixture = RecommendationFixture(dataset_root=str(self.root / "review-data"),
            evidence_root=str(self.root / "outputs/data_factory/runs"))
        for episode in fixture.evidence:
            episode["candidate"].update(semantic_status="PENDING", reviewed_by=None, reviewed_at=None, reason=None)
            fixture.rebind_episode(episode)
        self.runs = fixture.store()
        aborted = self.runs[0].parent / "aborted-before-motion"
        aborted.mkdir()
        (aborted / "readiness_failure.json").write_text('{}')
        self.immutable = {path: hashlib.sha256(path.read_bytes()).hexdigest()
            for directory in self.runs for path in directory.iterdir()
            if path.name not in {"candidate_admission.json", "episode_ledger_state.json"}}
        self.sequence = 0

    def send(self, app, op, payload=None):
        self.sequence += 1
        return app.core.consume(self.native.request(app, op, payload or {}, f"review-{self.sequence}"))

    def select(self, app, index=0):
        self.send(app, "refresh_stored_reviews")
        self.send(app, "select_stored_review", {"run_id": self.runs[index].name})
        return app.projection()["candidate_review"]

    def assert_immutable(self):
        self.assertEqual(self.immutable, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.immutable})
        self.assertEqual(self.native.episode.read_bytes(), self.native.original)
        self.native.forbidden.assert_not_called()

    def test_fresh_application_and_new_campaign_keep_committed_reviews(self):
        app = self.native.application("fresh-review")
        self.assertIsNone(app._campaign)
        self.assertEqual(app.projection()["stored_reviews"]["status"], "NOT_CHECKED")
        review = self.select(app)
        stored = app.projection()["stored_reviews"]
        self.assertEqual(len(stored["episodes"]), 2)
        self.assertEqual(stored["excluded_count"], 0)
        self.assertEqual(review["run_id"], self.runs[0].name)
        self.assertEqual(review["status"], "PENDING")
        self.assertNotIn(str(self.root), json.dumps(stored))
        # Existing compiler creates a new campaign; review ownership stays at app lifetime.
        self.send(app, "update_camera_bindings", {"bindings": {
            "usb-Generic_USB2.0_PC_CAMERA-video-index0": "UP", "usb-Generic_USB2.0_PC_CAMERA_2-video-index0": "WRIST"}})
        self.send(app, "update_draft", {"draft_id": app.draft["draft_id"], "requested_count": 1})
        self.send(app, "compile_draft", {"draft_id": app.draft["draft_id"], "data_disposition": "PRODUCTION"})
        self.assertIsNotNone(app._campaign)
        self.assertEqual(app.projection()["candidate_review"]["review_binding_digest"], review["review_binding_digest"])
        self.send(app, "review_candidate", {"review_binding_digest": review["review_binding_digest"], "choice": "PASS", "reason": None})
        self.assertEqual(app.projection()["candidate_review"]["status"], "PASS")
        self.assertFalse(app.projection()["candidate_review"]["training_authorized"])
        restarted = self.native.application("next-process")
        self.assertEqual(self.select(restarted)["status"], "PASS")
        self.assertEqual(restarted.projection()["candidate_review"]["reviewed_by"], "local-operator")
        self.assert_immutable()

    def test_shipped_web_review_response_loss_reads_result_without_post_replay(self):
        app = self.native.application("web-persisted-review")
        bridge = LoopbackBridge(core=app.core, ui_root=ROOT / "operator-ui", port=0)
        thread = threading.Thread(target=bridge.serve_forever)
        thread.start()
        try:
            process = subprocess.run(["node", "operator-ui/tests/stored-review-recovery.cjs", bridge.origin,
                                      "operator-ui/app.js", self.runs[0].name], cwd=ROOT,
                                     capture_output=True, text=True, timeout=90)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result["methods"], ["POST", "GET"])
            self.assertEqual(result["review"]["status"], "PASS")
            self.assertIn(self.runs[0].name, result["card"])
            self.assertFalse(result["review"]["training_authorized"])
            self.assertNotIn(str(self.root), result["card"])
        finally:
            bridge.close()
            thread.join(5)
        self.assertIsNone(app._campaign)
        self.assert_immutable()

    def test_restart_recovers_candidate_cas_before_ledger_projection(self):
        app = self.native.application("interrupted-review")
        review = self.select(app)
        payload = {"review_binding_digest": review["review_binding_digest"], "choice": "UNCERTAIN", "reason": "UNKNOWN"}
        with mock.patch.object(run_job, "bind_candidate_episode_state", side_effect=OSError("lost projection")):
            with self.assertRaises(OSError):
                self.send(app, "review_candidate", payload)
        candidate = load_json_strict(self.runs[0] / "candidate_admission.json")
        self.assertEqual(candidate["semantic_status"], "UNCERTAIN")
        self.assertNotIn("review_candidate", app.projection()["available_ops"])
        restarted = self.native.application("recover-recorded-decision")
        with mock.patch.object(run_job, "review_candidate_admission", side_effect=AssertionError("no repeated decision")):
            recovered = self.select(restarted)
        self.assertEqual(recovered["status"], "UNCERTAIN")
        self.assertEqual(recovered["reviewed_by"], candidate["reviewed_by"])
        self.assertEqual(recovered["reviewed_at"], candidate["reviewed_at"])
        state = load_json_strict(self.runs[0] / "episode_ledger_state.json")
        self.assertEqual(state["review"]["semantic_status"], "UNCERTAIN")
        self.assertEqual(state["review"]["training_status"], "NOT_AUTHORIZED")
        self.assert_immutable()

    def test_stale_external_decision_and_unknown_run_reject_then_refresh(self):
        app = self.native.application("external-review")
        review = self.select(app)
        for value in ("../other", "unknown-run", str(self.runs[0])):
            with self.subTest(run_id=value), self.assertRaises(ContractError):
                self.send(app, "select_stored_review", {"run_id": value})
        self.send(app, "refresh_stored_reviews")
        path = self.runs[0] / "candidate_admission.json"
        candidate = load_json_strict(path)
        run_job.review_candidate_admission(path, expected_file_digest=canonical_digest(candidate),
            expected_review_context_digest=candidate["review_context_digest"], checklist_id=candidate["checklist_id"],
            semantic_status="FAIL", reviewed_by="external-reviewer", reason="TASK_GOAL")
        with self.assertRaises(ContractError):
            self.send(app, "review_candidate", {"review_binding_digest": review["review_binding_digest"], "choice": "PASS", "reason": None})
        self.send(app, "refresh_stored_reviews")
        result = app.projection()["candidate_review"]
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["reviewed_by"], "external-reviewer")
        self.assertNotIn("review_candidate", app.projection()["available_ops"])
        self.assert_immutable()

    def test_stale_binding_and_changed_context_cannot_issue_a_decision(self):
        app = self.native.application("stale-review")
        review = self.select(app)
        path = self.runs[0] / "candidate_admission.json"
        original = path.read_bytes()
        with mock.patch.object(run_job, "review_candidate_admission", side_effect=AssertionError("no decision")):
            with self.assertRaisesRegex(ContractError, "CANDIDATE_REVIEW_DIGEST_MISMATCH"):
                self.send(app, "review_candidate", {"review_binding_digest": canonical_digest("stale"), "choice": "PASS", "reason": None})
            self.assertEqual(path.read_bytes(), original)
            self.send(app, "refresh_stored_reviews")
            candidate = load_json_strict(path)
            candidate["review_context_digest"] = canonical_digest("wrong-context")
            path.write_text(json.dumps(candidate))
            with self.assertRaises(ContractError):
                self.send(app, "review_candidate", {"review_binding_digest": review["review_binding_digest"], "choice": "PASS", "reason": None})
            self.send(app, "refresh_stored_reviews")
        projection = app.projection()["stored_reviews"]
        self.assertEqual(projection["excluded_count"], 1)
        self.assertEqual(len(projection["episodes"]), 1)
        self.assertEqual(load_json_strict(path)["semantic_status"], "PENDING")
        self.assert_immutable()

    def freeze(self, app, indices=(0, 1)):
        self.send(app, "refresh_stored_reviews")
        self.send(app, "freeze_review_batch", {"run_ids": [self.runs[index].name for index in indices]})
        return app.projection()["stored_reviews"]["batch"]["selection"]

    def batch_payload(self, selection, *, choice="PASS", excluded=()):
        return {"batch_binding_digest": selection["batch_binding_digest"], "choice": choice,
                "reason": None if choice == "PASS" else "TASK_GOAL",
                "excluded_run_ids": list(excluded)}

    def test_frozen_batch_one_decision_two_native_candidates_and_replay_rejected(self):
        app = self.native.application("batch-review")
        selection = self.freeze(app)
        self.assertNotIn(str(self.root), json.dumps(selection))
        payload = self.batch_payload(selection)
        request = self.native.request(app, "review_stored_batch", payload, "one-batch-choice")
        app.core.consume(request)
        batch = app.projection()["stored_reviews"]["batch"]
        self.assertEqual(batch["state"], "FINISHED")
        self.assertEqual([item["status"] for item in batch["items"]], ["PASS", "PASS"])
        self.assertEqual({item["reviewed_by"] for item in batch["items"]}, {"local-operator"})
        self.assertTrue(all(item["reviewed_at"] and not item["training_authorized"] for item in batch["items"]))
        with mock.patch.object(run_job, "review_candidate_admission", side_effect=AssertionError("never replay decisions")):
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
                app.core.consume(request)
            with self.assertRaises(ContractError):
                self.send(app, "review_stored_batch", payload)
            self.send(app, "recover_review_batch", {"selection": selection})
        self.assertEqual(app.projection()["stored_reviews"]["batch"]["state"], "RECOVERED")
        self.assertNotIn("review_stored_batch", app.projection()["available_ops"])
        self.assert_immutable()

    def test_batch_exclusion_keeps_individual_differing_decision_and_exact_selection(self):
        app = self.native.application("batch-exclude")
        selection = self.freeze(app)
        self.send(app, "select_stored_review", {"run_id": self.runs[1].name})
        review = app.projection()["candidate_review"]
        self.send(app, "review_candidate", {"review_binding_digest": review["review_binding_digest"], "choice": "FAIL", "reason": "TASK_GOAL"})
        self.send(app, "review_stored_batch", self.batch_payload(selection, excluded=[self.runs[1].name]))
        batch = app.projection()["stored_reviews"]["batch"]
        self.assertEqual(batch["selection"], selection)
        self.assertEqual([item["status"] for item in batch["items"]], ["PASS", "FAIL"])
        self.assertEqual(batch["items"][1]["reviewed_at"], app.projection()["candidate_review"]["reviewed_at"])
        self.assert_immutable()

    def test_batch_preflight_stale_last_item_rejects_before_any_new_decision(self):
        app = self.native.application("batch-stale")
        selection = self.freeze(app)
        path = self.runs[1] / "candidate_admission.json"
        candidate = load_json_strict(path)
        run_job.review_candidate_admission(path, expected_file_digest=canonical_digest(candidate),
            expected_review_context_digest=candidate["review_context_digest"], checklist_id=candidate["checklist_id"],
            semantic_status="UNCERTAIN", reviewed_by="other-reviewer", reason="UNKNOWN")
        with mock.patch.object(run_job, "review_candidate_admission", side_effect=AssertionError("preflight must precede all writes")):
            self.send(app, "review_stored_batch", self.batch_payload(selection))
        batch = app.projection()["stored_reviews"]["batch"]
        self.assertEqual(batch["state"], "INTERRUPTED")
        self.assertEqual([item["status"] for item in batch["items"]], ["PENDING", "UNCERTAIN"])
        self.assertEqual(batch["items"][1]["reviewed_by"], "other-reviewer")
        self.assert_immutable()

    def test_batch_process_interruption_reconciles_each_item_without_resuming(self):
        app = self.native.application("batch-interrupt")
        selection = self.freeze(app)
        original = app.stored_reviews._decide
        count = 0
        def interrupted(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise SystemExit("synthetic process termination before second candidate")
            return original(*args)
        with mock.patch.object(app.stored_reviews, "_decide", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.send(app, "review_stored_batch", self.batch_payload(selection))
        restarted = self.native.application("batch-restarted")
        with mock.patch.object(run_job, "review_candidate_admission", side_effect=AssertionError("no resume/retry")):
            self.send(restarted, "recover_review_batch", {"selection": selection})
        batch = restarted.projection()["stored_reviews"]["batch"]
        self.assertEqual(batch["state"], "RECOVERED")
        self.assertEqual([item["status"] for item in batch["items"]], ["PASS", "PENDING"])
        self.assertNotIn("review_stored_batch", restarted.projection()["available_ops"])
        self.assert_immutable()

    def test_batch_candidate_write_then_failed_projection_recovers_native_receipt_only(self):
        app = self.native.application("batch-projection-loss")
        selection = self.freeze(app)
        with mock.patch.object(run_job, "bind_candidate_episode_state", side_effect=OSError("interrupted ledger projection")):
            self.send(app, "review_stored_batch", self.batch_payload(selection))
        batch = app.projection()["stored_reviews"]["batch"]
        self.assertEqual(batch["state"], "INTERRUPTED")
        self.assertEqual(batch["items"][0]["binding_status"], "UNAVAILABLE")
        self.assertEqual(batch["items"][1]["status"], "PENDING")
        with mock.patch.object(run_job, "review_candidate_admission", side_effect=AssertionError("recorded decision must not be sent again")):
            self.send(app, "recover_review_batch", {"selection": selection})
        self.assertEqual([item["status"] for item in app.projection()["stored_reviews"]["batch"]["items"]], ["PASS", "PENDING"])
        self.assert_immutable()

    def test_batch_bounds_and_frozen_binding_cannot_be_replaced_by_browser(self):
        app = self.native.application("batch-binding")
        selection = self.freeze(app)
        for ids in ([], [self.runs[0].name] * 65, ["../other"], [self.runs[0].name] * 2):
            with self.subTest(ids=ids), self.assertRaises(ContractError):
                self.send(app, "freeze_review_batch", {"run_ids": ids})
            self.send(app, "refresh_stored_reviews")
        with self.assertRaises(ContractError):
            self.send(app, "review_stored_batch", {**self.batch_payload(selection), "batch_binding_digest": canonical_digest("other")})
        self.send(app, "refresh_stored_reviews")
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_AUTHORITY"):
            self.send(app, "review_stored_batch", {**self.batch_payload(selection), "reviewed_by": "forged"})
        self.assertTrue(all(load_json_strict(run / "candidate_admission.json")["semantic_status"] == "PENDING" for run in self.runs))
        self.assert_immutable()

    def test_shipped_web_batch_lost_response_reads_per_item_result_once(self):
        app = self.native.application("batch-web-lost-response")
        bridge = LoopbackBridge(core=app.core, ui_root=ROOT / "operator-ui", port=0)
        thread = threading.Thread(target=bridge.serve_forever)
        thread.start()
        try:
            process = subprocess.run(["node", "operator-ui/tests/stored-review-recovery.cjs", bridge.origin,
                "operator-ui/app.js", self.runs[0].name, "batch"], cwd=ROOT, capture_output=True, text=True, timeout=90)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result["methods"], ["POST", "GET"])
            self.assertEqual(result["batch"]["state"], "FINISHED")
            self.assertEqual([item["status"] for item in result["batch"]["items"]], ["PASS", "PASS"])
            for run in self.runs:
                self.assertIn(run.name, result["batchCard"])
            self.assertIn("local-operator", result["batchCard"])
        finally:
            bridge.close()
            thread.join(5)
        self.assert_immutable()

    def test_changed_batch_ledger_is_unavailable_and_cannot_judge_earlier_item(self):
        app = self.native.application("batch-ledger-changed")
        selection = self.freeze(app)
        path = self.runs[1] / "episode_ledger.json"
        original = path.read_bytes()
        try:
            value = load_json_strict(path)
            value["admission"]["review_context_digest"] = canonical_digest("wrong-context")
            path.write_text(json.dumps(value))
            with mock.patch.object(run_job, "review_candidate_admission", side_effect=AssertionError("changed ledger grants no decisions")):
                self.send(app, "review_stored_batch", self.batch_payload(selection))
            batch = app.projection()["stored_reviews"]["batch"]
            self.assertEqual(batch["state"], "INTERRUPTED")
            self.assertEqual(batch["items"][1]["binding_status"], "UNAVAILABLE")
            self.assertEqual(batch["items"][0]["status"], "PENDING")
        finally:
            path.write_bytes(original)
        self.assert_immutable()

    def test_recovered_decision_cannot_mask_changed_operational_authority(self):
        app = self.native.application("batch-authority-changed")
        selection = self.freeze(app)
        self.send(app, "review_stored_batch", self.batch_payload(selection))
        path = self.runs[0] / "candidate_admission.json"
        candidate = load_json_strict(path)
        candidate["operational_source"] = "HUMAN_GATED" if candidate["operational_source"] == "HIL_PROXY" else "HIL_PROXY"
        path.write_text(json.dumps(candidate))
        record = app.stored_reviews.entries[self.runs[0].name]
        run_job.bind_candidate_episode_state(record["reference"], path)
        self.send(app, "recover_review_batch", {"selection": selection})
        batch = app.projection()["stored_reviews"]["batch"]
        self.assertEqual(batch["items"][0]["binding_status"], "CHANGED")
        self.assertEqual(batch["items"][1]["binding_status"], "DECIDED")
        self.assert_immutable()
