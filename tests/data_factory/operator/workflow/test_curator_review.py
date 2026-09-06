"""Native synthetic candidate evidence through the production Web consumer boundary."""
import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from tests.data_factory.curator.support import make_profile_fixture, make_source_dataset
from tests.data_factory.operator.fixtures import intent
from tools.data_factory.curator.core.identity import stable_tree_identity
from tools.data_factory.curator.workflow import application as curator
from tools.data_factory.curator.workflow.state import load_events
from tools.data_factory.operator.composition import build_operator_runtime
from tools.data_factory.operator.workflow.curator_review import CuratorReviewApplication
from tools.fr5_data_factory import ContractError


ROOT = Path(__file__).resolve().parents[4]


class CuratorReviewTest(unittest.TestCase):
    def fixture(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        fixture = make_profile_fixture(root)
        source = make_source_dataset(root, episodes=1, frames_per_episode=2)
        curator.prepare(source, _paths=fixture.paths, _run_id_value="web-review")
        app = CuratorReviewApplication(run_id="web-review", paths=fixture.paths)
        return fixture, source, app

    def bridge(self, fixture):
        runtime = build_operator_runtime(effect_scope="CURATOR_REVIEW", port=0,
            curator_run_id="web-review", curator_paths=fixture.paths)
        thread = threading.Thread(target=runtime.bridge.serve_forever)
        thread.start()
        def close():
            runtime.bridge.close()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.addCleanup(close)
        return runtime.bridge

    def request(self, bridge, method, path, *, body=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=30)
        headers = {"Origin": bridge.origin, "Content-Type": "application/json"}
        if token:
            headers["X-Operator-Token"] = bridge.token
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_native_choices_lost_response_refresh_and_authority(self):
        for action, failed_read, terminal in (("approve", False, "PUBLISHED"),
                                              ("reject", False, "REJECTED"),
                                              ("approve", True, "PUBLISHED")):
            with self.subTest(action=action, failed_read=failed_read):
                fixture, source, app = self.fixture()
                before = stable_tree_identity(source, code="SOURCE_TEST")
                bridge = self.bridge(fixture)
                with mock.patch.object(curator, "read_foreground_decision", side_effect=AssertionError("TTY forbidden")):
                    result = subprocess.run(["node", str(ROOT / "operator-ui/tests/curator-recovery.cjs"),
                        bridge.origin, str(ROOT / "operator-ui/curator.js"), action, str(failed_read).lower()],
                        capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual([(r["method"], r["path"]) for r in report["requests"]],
                                 [("POST", "/api/intent"), ("GET", "/api/view")])
                self.assertEqual(set(report["requests"][0]["payload"]), {"choice", "expected_review_digest"})
                review = report["canonical"]["projection"]["review"]
                self.assertEqual(review["status"], terminal)
                self.assertFalse(review["training_authority"])
                self.assertFalse(review["receipt"]["approval_inherited"])
                self.assertEqual(review["decision"]["actor"]["kind"], "LOCAL_OS_ACCOUNT")
                self.assertFalse(review["decision"]["actor"]["human_identity_authenticated"])
                self.assertEqual(report["visibleActions"], [])
                self.assertTrue(report["refreshEnabled"])
                self.assertIn("결과를 확인하지 못했습니다" if failed_read else "저장된 상태를 확인했습니다", report["status"])
                # A fresh adapter/session reads the same durable decision; browser state is dispensable.
                self.assertEqual(app.projection()["review"], review)
                self.assertEqual(stable_tree_identity(source, code="SOURCE_TEST"), before)
                events = load_events(fixture.paths.run_root / "web-review")
                self.assertEqual(set(events), {"request", "candidate_ready", "review_ready", "decision", "receipt"})
                print(json.dumps({"action": action, "lost_response": True, "failed_read": failed_read,
                                  "requests": len(report["requests"]), "status": review["status"]}))

    def test_native_media_binding_stale_wrong_run_and_recorded_recovery(self):
        fixture, source, app = self.fixture()
        bridge = self.bridge(fixture)
        with mock.patch.object(curator, "review_candidate", wraps=curator.review_candidate) as read:
            shown = bridge.core.snapshot()
        self.assertEqual(read.call_count, 1)
        review = shown["projection"]["review"]
        digest = review["review_ready_digest"]
        self.assertNotIn("review_video_path", review)
        self.assertNotIn("review_manifest_path", review)
        path = "/api/curator-review/video?review_digest=" + digest
        self.assertEqual(self.request(bridge, "GET", path, token=False)[0], 403)
        status, video = self.request(bridge, "GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(video, app.review_video(digest))
        for query in ("?path=/etc/passwd", "?review_digest=" + digest + "&path=x", "?review_digest=x"):
            self.assertEqual(self.request(bridge, "GET", "/api/curator-review/video" + query)[0], 400)
        self.assertEqual(self.request(bridge, "GET", "/api/curator-review/video?review_digest=sha256:" + "0" * 64)[0], 409)
        payload = {"choice": "APPROVE", "expected_review_digest": digest}
        for extra in ("actor", "run_id", "output_path"):
            with self.assertRaisesRegex(ContractError, "CURATOR_REVIEW_PAYLOAD"):
                bridge.core.consume(intent(shown, "decide_curator_candidate", {**payload, extra: "forged"}, extra))
        with self.assertRaisesRegex(ContractError, "CURATOR_REVIEW_PAYLOAD"):
            bridge.core.consume(intent(shown, "decide_curator_candidate", {**payload, "choice": []}, "bad-choice"))
        curator.prepare(source, _paths=fixture.paths, _run_id_value="other-review")
        other = curator.review_candidate("other-review", _paths=fixture.paths)
        for bad_digest in ("sha256:" + "0" * 64, other["review_ready_digest"]):
            current = bridge.core.snapshot()
            with self.assertRaisesRegex(ContractError, "REVIEW_CHANGED"):
                bridge.core.consume(intent(current, "decide_curator_candidate",
                    {**payload, "expected_review_digest": bad_digest}, "bad-" + bad_digest[-12:]))
        self.assertNotIn("decision", load_events(fixture.paths.run_root / "web-review"))
        entered, release = threading.Event(), threading.Event()
        errors = []
        def receipt_failure(*_args, **_kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("pending view blocked on native decision lock")
            raise OSError("fixture receipt failure")
        def approve():
            try:
                bridge.core.consume(intent(bridge.core.snapshot(), "decide_curator_candidate", payload, "approve"))
            except ContractError as exc:
                errors.append(exc.code)
        with mock.patch.object(curator, "_write_receipt", side_effect=receipt_failure):
            worker = threading.Thread(target=approve)
            worker.start()
            try:
                self.assertTrue(entered.wait(10))
                with mock.patch.object(curator, "review_candidate", side_effect=AssertionError("pending read must not rehash")):
                    in_progress = bridge.core.snapshot()["projection"]
                    self.assertTrue(in_progress["request_pending"])
                    self.assertEqual(in_progress["available_ops"], [])
            finally:
                release.set()
                worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ["OUTPUT_COMMITTED_RECEIPT_PENDING"])
        pending = bridge.core.snapshot()
        self.assertEqual(pending["projection"]["review"]["allowed_decisions"], ["APPROVE"])
        decision = pending["projection"]["review"]["decision"]
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_STALE_VIEW"):
            bridge.core.consume(intent(shown, "decide_curator_candidate", payload, "stale"))
        with self.assertRaisesRegex(ContractError, "DECISION_CONFLICT"):
            bridge.core.consume(intent(pending, "decide_curator_candidate", {**payload, "choice": "REJECT"}, "conflict"))
        with mock.patch.object(curator, "publish_candidate", side_effect=AssertionError("must not republish")):
            result = subprocess.run(["node", str(ROOT / "operator-ui/tests/curator-recovery.cjs"),
                bridge.origin, str(ROOT / "operator-ui/curator.js"), "recover", "false"],
                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("PUBLISHED_RECEIPT_PENDING", json.loads(result.stdout)["status"])
        terminal = bridge.core.snapshot()["projection"]["review"]
        self.assertEqual(terminal["status"], "PUBLISHED")
        self.assertEqual(terminal["decision"], decision)
        self.assertEqual(terminal["allowed_decisions"], [])
        native_read = curator.review_candidate
        media = Path(native_read("web-review", _paths=fixture.paths)["review_video_path"])
        original = media.read_bytes()
        mode = media.stat().st_mode
        def change_after_validation(*args, **kwargs):
            review = native_read(*args, **kwargs)
            media.chmod(0o600)  # Deliberate tamper of this test-owned, frozen synthetic clip.
            media.write_bytes(b"changed after native validation")
            return review
        try:
            with mock.patch.object(curator, "review_candidate", side_effect=change_after_validation):
                with self.assertRaisesRegex(ContractError, "REVIEW_CHANGED"):
                    app.review_video(digest)
        finally:
            media.write_bytes(original)
            media.chmod(mode)

    def test_review_configuration_cannot_fall_back_to_collection_or_training(self):
        for scope, kwargs in (("FAKE", {"curator_run_id": "r"}),
                              ("CURATOR_REVIEW", {}),
                              ("CURATOR_REVIEW", {"curator_run_id": "r", "training_request": "x"})):
            with self.subTest(scope=scope, kwargs=kwargs), self.assertRaisesRegex(ContractError, "CURATOR_REVIEW_CONFIGURATION"):
                build_operator_runtime(effect_scope=scope, **kwargs)

    def test_committed_receipt_survives_postcommit_media_failure(self):
        fixture, _source, app = self.fixture()
        bridge = self.bridge(fixture)
        native = curator.review_candidate("web-review", _paths=fixture.paths)
        video = Path(native["review_video_path"])
        write_receipt = curator._write_receipt
        def corrupt_after_commit(*args, **kwargs):
            result = write_receipt(*args, **kwargs)
            video.chmod(0o600)
            video.write_bytes(b"synthetic media corruption after durable receipt")
            return result
        with mock.patch.object(curator, "_write_receipt", side_effect=corrupt_after_commit):
            report = subprocess.run(["node", str(ROOT / "operator-ui/tests/curator-recovery.cjs"),
                bridge.origin, str(ROOT / "operator-ui/curator.js"), "approve", "false"],
                capture_output=True, text=True, timeout=45)
        self.assertEqual(report.returncode, 0, report.stderr)
        result = json.loads(report.stdout)
        events = load_events(fixture.paths.run_root / "web-review")
        self.assertEqual(events["receipt"]["payload"]["outcome"], "PUBLISHED")
        self.assertIn("projection", result["canonical"], result)
        review = result["canonical"]["projection"]["review"]
        self.assertEqual(review["receipt"], events["receipt"]["payload"])
        self.assertEqual(review["allowed_decisions"], [])
        self.assertFalse(review["media_available"])
        self.assertEqual(review["media_error"]["reason_code"], "REVIEW_VIDEO_DIGEST")
        self.assertIsNone(result["videoSource"])
        self.assertIn("영상을 사용할 수 없습니다", result["mediaStatus"])
        self.assertEqual(result["visibleActions"], [])
        self.assertIn("공개했습니다", result["status"])
