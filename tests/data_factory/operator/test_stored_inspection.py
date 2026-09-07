"""Exact stored-review inspection through native application and process seams."""
import json
import hashlib
import shutil
import subprocess
import threading
import unittest
from unittest import mock

from tests.data_factory.operator import test_stored_reviews as fixtures
from tools.data_factory.operator.workflow.inspection import NativeInspection, verify_target
from tools.fr5_data_factory import ContractError


class Viewer:
    def __init__(self):
        self.opens = []
        self.value = {"status": "CLOSED"}

    def open(self, dataset, index, **kwargs):
        verify_target(dataset)
        self.opens.append((dataset, index, kwargs["expected_locator"]))
        self.value = {"status": "READY", "url": "http://127.0.0.1:12345/",
                      "mapping": {"frames": 2}, "read_only": True}
        return self.value

    def snapshot(self):
        return self.value

    def close(self):
        self.value = {"status": "CLOSED"}

    def video(self):
        return b"bounded synthetic media"


class StoredInspectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.StoredReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app = self.fixture.native.application("stored-inspection")
        self.viewer = Viewer()
        self.app.stored_reviews.inspector = self.viewer
        review = self.fixture.select(self.app)
        self.payload = {"review_binding_digest": review["review_binding_digest"]}

    def send(self, op, payload=None):
        return self.fixture.send(self.app, op, self.payload if payload is None else payload)

    def inspection(self):
        return self.app.projection()["stored_reviews"]["inspection"]

    def test_open_uses_current_append_identity_and_return_keeps_same_review(self):
        review = self.app.projection()["candidate_review"]
        self.send("inspect_stored_episode")
        dataset, index, locator = self.viewer.opens[0]
        record = self.app.stored_reviews.entries[review["run_id"]]
        self.assertNotEqual(dataset["dataset_digest"], record["dataset"]["dataset_digest"])
        self.assertEqual(index, review["episode_index"])
        self.assertEqual(locator, record["locator"])
        self.assertNotIn("dataset_root", json.dumps(self.inspection()))
        with mock.patch("tools.data_factory.operator.workflow.stored_reviews.current_dataset_identity",
                        side_effect=AssertionError("poll must not hash")):
            self.assertEqual(self.inspection()["status"], "READY")
        self.send("return_stored_review")
        self.assertEqual(self.inspection()["status"], "CLOSED")
        self.assertEqual(self.app.projection()["candidate_review"], review)
        self.assertIn("review_candidate", self.app.projection()["available_ops"])
        self.fixture.assert_immutable()

    def test_append_after_open_marks_snapshot_stale_without_blocking_review(self):
        self.send("inspect_stored_episode")
        (self.fixture.root / "review-data/new-append.json").write_text("{}")
        self.send("return_stored_review")
        self.assertEqual(self.inspection()["status"], "STALE")
        self.assertEqual(self.viewer.snapshot()["status"], "CLOSED")
        self.assertIn("review_candidate", self.app.projection()["available_ops"])
        self.assertIsNone(self.app._campaign)
        self.fixture.assert_immutable()

    def test_failure_is_optional_and_wrong_binding_and_replay_do_not_open(self):
        for payload in ({"review_binding_digest": "wrong"}, {**self.payload, "dataset_root": "/tmp/other"}):
            with self.assertRaisesRegex(ContractError, "INSPECTION_TARGET"):
                self.send("inspect_stored_episode", payload)
            self.fixture.select(self.app)
        before = self.fixture.native.request(self.app, "inspect_stored_episode", self.payload, "once")
        with mock.patch.object(self.viewer, "open", side_effect=ContractError("INSPECTION_EXPORT_FAILED")):
            self.app.core.consume(before)
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
            self.app.core.consume(before)
        self.assertEqual(self.inspection()["status"], "FAILED")
        self.assertIn("review_candidate", self.app.projection()["available_ops"])
        self.assertEqual(self.viewer.opens, [])
        self.fixture.assert_immutable()

    def test_selection_refresh_and_close_cancel_late_open_without_attaching_result(self):
        for action in ("select", "refresh", "close"):
            with self.subTest(action=action):
                self.fixture.select(self.app)
                entered, release = threading.Event(), threading.Event()
                errors = []
                def delayed(_dataset, _index, **kwargs):
                    entered.set()
                    self.assertTrue(release.wait(5))
                    self.assertTrue(kwargs["cancelled"].is_set())
                    return {"status": "READY", "url": "http://127.0.0.1:12345/"}
                def run():
                    try:
                        self.send("inspect_stored_episode")
                    except Exception as exc:
                        errors.append(exc)
                with mock.patch.object(self.viewer, "open", side_effect=delayed):
                    thread = threading.Thread(target=run)
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(5))
                        view = self.app.core.snapshot()["projection"]
                        self.assertTrue(view["stored_reviews"]["busy"])
                        self.assertEqual(view["workflow_state"], "AUTHORING")
                        if action == "select":
                            self.app.stored_reviews.select(self.fixture.runs[1].name)
                        elif action == "refresh":
                            self.app.stored_reviews.refresh()
                        else:
                            self.app.stored_reviews.close()
                    finally:
                        release.set()
                        thread.join(5)
                self.assertEqual(errors, [])
                self.assertEqual(self.inspection(), {"status": "CLOSED", "target": None})
        self.fixture.assert_immutable()

    def test_cancelled_native_open_cannot_spawn_or_create_temp(self):
        owned = NativeInspection()
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch.object(owned, "_spawn", side_effect=AssertionError("no child")):
            with self.assertRaisesRegex(ContractError, "INSPECTION_CLOSED"):
                owned.open({}, 0, cancelled=cancelled)
        self.assertIsNone(owned._directory)
        self.assertEqual(owned.snapshot()["status"], "CLOSED")
        self.app.close()
        with self.assertRaisesRegex(ContractError, "INSPECTION_CLOSED"):
            self.app.stored_reviews.inspect(self.payload)
        self.assertEqual(self.viewer.opens, [])

    def test_native_failure_cleanup_does_not_masquerade_as_selection_cancellation(self):
        owner = self.app.stored_reviews.inspector = NativeInspection()
        with mock.patch.object(owner, "_spawn", side_effect=ContractError("INSPECTION_EXPORT_FAILED")):
            self.send("inspect_stored_episode")
        self.assertEqual(self.inspection()["status"], "FAILED")
        self.assertEqual(self.inspection()["error"], "INSPECTION_EXPORT_FAILED")
        self.assertIn("inspect_stored_episode", self.app.projection()["available_ops"])
        self.assertIn("review_candidate", self.app.projection()["available_ops"])
        self.assertIsNone(owner._directory)
        self.assertFalse(self.app.stored_reviews._inspection_cancelled.is_set())

    def test_media_transport_requires_token_exact_target_and_owned_selection(self):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from urllib.parse import quote
        from tools.data_factory.operator.web.bridge import LoopbackBridge
        self.send("inspect_stored_episode")
        binding = self.inspection()["inspection_binding_digest"]
        bridge = LoopbackBridge(core=self.app.core, ui_root=fixtures.ROOT / "operator-ui", port=0,
                                inspection_video_call=self.app.stored_reviews.video)
        thread = threading.Thread(target=bridge.serve_forever)
        thread.start()
        url = bridge.origin + "/api/episode-inspection/video?review_digest=" + quote(binding)
        try:
            with self.assertRaises(HTTPError) as denied:
                urlopen(url)
            self.assertEqual(denied.exception.code, 403)
            headers = {"X-Operator-Token": bridge.token}
            with urlopen(Request(url, headers=headers)) as response:
                self.assertEqual(response.headers["Content-Type"], "video/mp4")
                self.assertEqual(response.read(), self.viewer.video())
            for wrong, status in ((url + "&path=/tmp/other", 400),
                                  (url.replace(quote(binding), quote("sha256:" + "0" * 64)), 409)):
                with self.assertRaises(HTTPError) as denied:
                    urlopen(Request(wrong, headers=headers))
                self.assertEqual(denied.exception.code, status)
            self.send("select_stored_review", {"run_id": self.fixture.runs[1].name})
            with self.assertRaises(HTTPError) as stale:
                urlopen(Request(url, headers=headers))
            self.assertEqual(stale.exception.code, 409)
        finally:
            bridge.close()
            thread.join(5)
        self.fixture.assert_immutable()

    def test_slow_export_leaves_native_collection_observation_and_cancel_available(self):
        from tests.data_factory.operator.workflow.test_application import StubCampaign
        campaign = StubCampaign("independent-collection", self.app.draft, "PRODUCTION")
        campaign.state = "RUNNING"
        self.app._campaign = campaign
        entered, release = threading.Event(), threading.Event()
        errors = []
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("export not released")
            return {"status": "FAILED", "error": "INSPECTION_EXPORT_FAILED"}
        def run():
            try:
                self.send("inspect_stored_episode")
            except Exception as exc:
                errors.append(exc)
        with mock.patch.object(self.viewer, "open", side_effect=delayed):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                view = self.app.core.snapshot()["projection"]
                self.assertEqual(view["workflow_state"], "RUNNING")
                self.assertIn("cancel_session", view["available_ops"])
                self.send("cancel_session", {"active_child_id": view["runtime"]["active_child_id"]})
                self.assertEqual(campaign.state, "TERMINAL")
                self.assertTrue(thread.is_alive())
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(errors, [])
        self.fixture.assert_immutable()

    def test_shipped_open_and_return_response_loss_never_replay_or_decide(self):
        from tools.data_factory.operator.web.bridge import LoopbackBridge
        bridge = LoopbackBridge(core=self.app.core, ui_root=fixtures.ROOT / "operator-ui", port=0)
        thread = threading.Thread(target=bridge.serve_forever)
        thread.start()
        try:
            for mode in ("inspect", "return"):
                with self.subTest(mode=mode):
                    result = subprocess.run(["node", "operator-ui/tests/stored-review-recovery.cjs", bridge.origin,
                        "operator-ui/app.js", self.fixture.runs[0].name, mode], cwd=fixtures.ROOT,
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertEqual(report["methods"], ["POST", "GET"])
                    self.assertEqual(report["inspection"]["status"], "READY" if mode == "inspect" else "CLOSED")
                    self.assertEqual(report["review"]["status"], "PENDING")
                    self.assertFalse(report["review"]["training_authorized"])
            self.assertEqual(len(self.viewer.opens), 2)
        finally:
            bridge.close()
            thread.join(5)
        self.fixture.assert_immutable()


class NativeStoredInspectionTest(unittest.TestCase):
    """Four synthetic frames, actual native ledger/composition/video export."""
    def setUp(self):
        from tests.data_factory.curator.support import make_source_dataset
        from tools.data_factory import run_job
        self.native = fixtures.position_fixture.ObjectPositionContinuityTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.root = self.native.root
        source = self.root / "media/source"
        evidence = fixtures.RecommendationFixture(dataset_root=str(source),
            evidence_root=str(self.root / "outputs/data_factory/runs"))
        evidence.evidence = [evidence.episode(0, 0), evidence.episode(1, 1)]
        self.runs = evidence.store()
        shutil.rmtree(source)  # Replace this fixture's dummy locator files only.
        make_source_dataset(source.parent, episodes=2, frames_per_episode=2)
        for item, run in zip(evidence.evidence, self.runs):
            ledger = item["ledger"]
            ledger["episode"]["lerobot_v3_locator"] = run_job._lerobot_v3_episode_locator(
                source, ledger["dataset"]["repo_id"], ledger["episode"]["episode_index"])
            item["candidate"].update(semantic_status="PENDING", reviewed_by=None, reviewed_at=None, reason=None)
            evidence.rebind_episode(item)
            for filename, value in (("episode_ledger.json", ledger), ("episode_ledger_state.json", item["state"]),
                                    ("candidate_admission.json", item["candidate"])):
                (run / filename).write_text(json.dumps(value))
        self.immutable = {path: hashlib.sha256(path.read_bytes()).hexdigest()
            for directory in [source, *self.runs] for path in directory.rglob("*") if path.is_file()}
        self.app = self.native.application("native-stored-inspection")
        self.sequence = 0
        self.send("refresh_stored_reviews", {})
        self.send("select_stored_review", {"run_id": self.runs[0].name})

    def send(self, op, payload):
        self.sequence += 1
        return self.app.core.consume(self.native.request(self.app, op, payload, f"native-{self.sequence}"))

    def assert_immutable(self):
        self.assertEqual(self.immutable, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.immutable})
        self.native.forbidden.assert_not_called()

    def test_actual_selected_episode_export_return_and_cleanup(self):
        review = self.app.projection()["candidate_review"]
        payload = {"review_binding_digest": review["review_binding_digest"]}
        self.send("inspect_stored_episode", payload)
        owner = self.app.stored_reviews.inspector
        value = self.app.projection()["stored_reviews"]["inspection"]
        self.assertEqual(value["status"], "READY", value)
        self.assertEqual(value["mapping"]["frames"], 2)
        self.assertEqual(value["mapping"]["episode_index"], 0)
        self.assertEqual(value["mapping"]["first_global_index"], 0)
        self.assertEqual(set(value["mapping"]["features"]), {
            "observation.images.up", "observation.images.wrist", "action", "observation.state"})
        process, directory = owner._process, owner._directory.name
        from pathlib import Path
        self.assertNotIn("url", value)
        self.assertEqual(value["mapping"]["camera_order"], ["UP", "WRIST"])
        media = self.app.stored_reviews.video(value["inspection_binding_digest"])
        self.assertEqual(len(media), value["mapping"]["mp4_bytes"])
        self.assertEqual("sha256:" + hashlib.sha256(media).hexdigest(), value["mapping"]["media_digest"])
        with self.assertRaisesRegex(ContractError, "INSPECTION_TARGET_CHANGED"):
            self.app.stored_reviews.video("sha256:" + "0" * 64)
        self.assertIsNotNone(process.poll())  # No viewer/encoder remains after preparation.
        self.send("return_stored_review", payload)
        self.assertIsNotNone(process.poll())
        self.assertFalse(Path(directory).exists())
        self.assertEqual(self.app.projection()["candidate_review"], review)
        with self.assertRaises(ContractError):
            self.app.stored_reviews.video(value["inspection_binding_digest"])
        self.assert_immutable()

    def test_nonzero_shard_slice_matches_source_pixels_and_camera_order(self):
        import cv2
        import numpy as np
        from pathlib import Path

        def decode(path):
            capture = cv2.VideoCapture(str(path))
            frames = []
            try:
                while True:
                    ok, frame = capture.read()
                    if not ok:
                        break
                    frames.append(frame.astype(np.int16))
            finally:
                capture.release()
            return frames

        self.send("select_stored_review", {"run_id": self.runs[1].name})
        store = self.app.stored_reviews
        record = store.entries[store.selected]
        source_frames = []
        for video in record["locator"]["videos"]:
            self.assertGreater(video["timestamp_start_s"], 0)
            frames = decode(Path(record["dataset"]["dataset_root"]) / video["relative_path"])
            self.assertEqual(len(frames), 4)
            self.assertEqual(len({hashlib.sha256(frame.tobytes()).digest() for frame in frames}), 4)
            source_frames.append(frames)
        review = self.app.projection()["candidate_review"]
        payload = {"review_binding_digest": review["review_binding_digest"]}
        self.send("inspect_stored_episode", payload)
        value = self.app.projection()["stored_reviews"]["inspection"]
        self.assertEqual(value["status"], "READY", value)
        self.assertEqual(value["mapping"]["camera_order"], ["UP", "WRIST"])
        self.assertEqual(value["mapping"]["first_global_index"], 2)
        output = decode(Path(store.inspector._directory.name) / "episode.mp4")
        self.assertEqual(len(output), 2)
        for frame_index, frame in enumerate(output):
            self.assertEqual(frame.shape, (480, 1280, 3))
            for camera_index, camera in enumerate(source_frames):
                panel = frame[:, camera_index * 640:(camera_index + 1) * 640]
                errors = [float(np.abs(panel - source).mean()) for source in camera]
                # Lossy inspection copy must match the selected frame more closely
                # than every other frame, including the neighboring episode.
                self.assertLess(errors[2 + frame_index], 3, errors)
                self.assertEqual(int(np.argmin(errors)), 2 + frame_index, errors)
                swapped = source_frames[1 - camera_index][2 + frame_index]
                self.assertLess(errors[2 + frame_index], float(np.abs(panel - swapped).mean()))
        self.send("return_stored_review", payload)
        self.assertEqual(self.app.projection()["candidate_review"], review)
        self.assert_immutable()
