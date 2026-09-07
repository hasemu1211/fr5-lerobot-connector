"""Synthetic native review authority, with the costly viewer isolated at its process seam."""
import copy
import json
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from tests.data_factory import test_training_approval as fixtures
from tests.data_factory.operator.fixtures import intent
from tools.data_factory.operator.workflow import inspection
from tools.data_factory.operator.workflow.training_review import TrainingReviewApplication
from tools.fr5_data_factory import ContractError
from tools.data_factory.operator.web.bridge import LoopbackBridge


class Viewer:
    def __init__(self):
        self.opens = 0
        self.closes = 0
        self.value = {"status": "CLOSED"}

    def open(self, dataset, index):
        inspection.verify_target(dataset)
        self.opens += 1
        self.value = {"status": "READY", "url": "http://127.0.0.1:12345/",
                      "mapping": {"episode_index": index, "frames": 2, "last_frame_index": 1,
                          "first_global_index": 4, "last_global_index": 5, "rrd_bytes": 100,
                          "features": {"action": {"names": ["j1"]}},
                          "versions": {"lerobot": "fixture", "rerun": "fixture"}}, "read_only": True}
        return self.value

    def snapshot(self):
        return self.value

    def close(self):
        self.closes += 1
        self.value = {"status": "CLOSED"}


class InspectionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NativeBatchTrainingApprovalTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.viewer = Viewer()
        self.app = TrainingReviewApplication(request=self.fixture.request,
            output=self.fixture.output, approved_by="fixture-human", inspection=self.viewer)
        self.addCleanup(self.app.close)
        self.consume("prepare_training_review", {})
        self.preview = self.view()["projection"]["preview"]
        self.payload = {"batch_digest": self.preview["batch_digest"], "episode_index": 2}

    def view(self):
        return self.app.bridge_core.snapshot()

    def consume(self, op, payload):
        return self.app.bridge_core.consume(intent(self.view(), op, payload, str(time.time_ns())))

    def test_open_return_preserves_exact_batch_authority_and_replay_cannot_reopen(self):
        before = self.view()
        request = intent(before, "inspect_training_episode", self.payload, "open-once")
        self.app.bridge_core.consume(request)
        opened = self.view()["projection"]
        self.assertEqual(opened["preview"], self.preview)
        self.assertEqual(opened["status"], "PREVIEW_NOT_APPROVED")
        self.assertEqual(opened["inspection"]["target"]["episode_index"], 2)
        self.assertNotIn("dataset_root", json.dumps(opened["inspection"]))
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
            self.app.bridge_core.consume(request)
        self.assertEqual(self.viewer.opens, 1)
        self.consume("return_training_review", {"batch_digest": self.preview["batch_digest"]})
        returned = self.view()["projection"]
        self.assertEqual(returned["inspection"]["status"], "CLOSED")
        self.assertEqual(returned["preview"], self.preview)
        self.assertEqual(returned["status"], "PREVIEW_NOT_APPROVED")
        self.assertFalse(returned["starts_training"])
        self.assertEqual(list(self.fixture.output.iterdir()), [])

    def test_wrong_episode_path_batch_and_stale_view_reject_without_viewer(self):
        for payload in ({**self.payload, "episode_index": 1},
                        {**self.payload, "episode_index": True},
                        {**self.payload, "dataset_root": "/tmp/other"},
                        {**self.payload, "batch_digest": "sha256:" + "0" * 64}):
            with self.subTest(payload=payload), self.assertRaisesRegex(ContractError, "INSPECTION_TARGET"):
                self.consume("inspect_training_episode", payload)
        stale = intent(self.view(), "inspect_training_episode", self.payload, "stale")
        self.consume("refuse_training_batch", {"batch_digest": self.preview["batch_digest"]})
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_STALE_VIEW"):
            self.app.bridge_core.consume(stale)
        self.assertEqual(self.viewer.opens, 0)

    def test_changed_and_missing_target_never_claim_current_return(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.consume("inspect_training_episode", self.payload)
                source = self.fixture.dataset / "meta/info.json"
                before = source.read_bytes()
                if missing:
                    source.unlink()
                else:
                    source.write_bytes(before + b"\n")
                self.consume("return_training_review", {"batch_digest": self.preview["batch_digest"]})
                self.assertEqual(self.view()["projection"]["inspection"]["status"], "STALE")
                self.assertEqual(self.view()["projection"]["preview"], self.preview)
                self.assertEqual(list(self.fixture.output.iterdir()), [])
                source.write_bytes(before)

    def test_viewer_failure_does_not_fail_or_retry_training_review(self):
        with mock.patch.object(self.viewer, "open", side_effect=ContractError("INSPECTION_EXPORT_FAILED")):
            with self.assertRaisesRegex(ContractError, "INSPECTION_EXPORT_FAILED"):
                self.consume("inspect_training_episode", self.payload)
        projection = self.view()["projection"]
        self.assertEqual(projection["status"], "PREVIEW_NOT_APPROVED")
        self.assertEqual(projection["inspection"]["status"], "FAILED")
        self.assertIn("refuse_training_batch", projection["available_ops"])
        self.consume("refuse_training_batch", {"batch_digest": self.preview["batch_digest"]})
        self.assertEqual(list(self.fixture.output.iterdir()), [])

    def test_changed_target_rejects_open_before_viewer_creation(self):
        (self.fixture.dataset / "changed.json").write_text("{}")
        with self.assertRaisesRegex(ContractError, "INSPECTION_TARGET_CHANGED"):
            self.consume("inspect_training_episode", self.payload)
        self.assertEqual(self.viewer.opens, 0)
        self.assertEqual(self.view()["projection"]["status"], "PREVIEW_NOT_APPROVED")

    def test_shipped_ui_recovers_open_and_return_response_loss_without_repeating(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[4]
        for action in ("inspect", "return"):
            with self.subTest(action=action):
                viewer = Viewer()
                app = TrainingReviewApplication(request=self.fixture.request, output=self.fixture.output,
                    approved_by="fixture-human", inspection=viewer)
                self.addCleanup(app.close)
                bridge = LoopbackBridge(core=app.bridge_core, ui_root=root / "operator-ui",
                    index_page="training.html", port=0)
                thread = threading.Thread(target=bridge.serve_forever)
                thread.start()
                try:
                    replay = subprocess.run(["node", str(root / "operator-ui/tests/training-recovery.cjs"),
                        bridge.origin, str(root / "operator-ui/training.js"), action, "false"],
                        capture_output=True, text=True, timeout=15)
                    self.assertEqual(replay.returncode, 0, replay.stderr)
                    report = json.loads(replay.stdout)
                    self.assertEqual(report["requests"], [{"method": "POST", "path": "/api/intent"},
                                                          {"method": "GET", "path": "/api/view"}])
                    self.assertEqual(report["canonical"]["projection"]["status"], "PREVIEW_NOT_APPROVED")
                    self.assertEqual(report["canonical"]["projection"]["inspection"]["status"],
                                     "READY" if action == "inspect" else "CLOSED")
                    self.assertEqual(viewer.opens, 1)
                    self.assertEqual(list(self.fixture.output.iterdir()), [])
                finally:
                    bridge.close()
                    thread.join(2)


class InspectionBoundaryTest(unittest.TestCase):
    def test_native_tuple_and_json_list_shapes_preserve_only_feature_labels(self):
        from tools.fr5_dataset_schema import dataset_features
        features = dataset_features(fps=30, height=480, width=640, cameras=("up", "wrist"), use_videos=True)
        features["observation.images.up"]["private_path"] = "/private/source"
        for shape in ((480, 640, 3), [480, 640, 3]):
            features["observation.images.up"]["shape"] = shape
            self.assertNotIn("/private", json.dumps(inspection.feature_projection(features)))
        features["observation.images.up"]["shape"] = (1080, 1920, 3)
        with self.assertRaisesRegex(ContractError, "INSPECTION_FEATURES"):
            inspection.feature_projection(features)

    def test_canonical_mapping_rejects_gaps_wrong_episode_local_frame_and_limit(self):
        rows = [{"index": 100 + i, "episode_index": 29, "frame_index": i, "timestamp": i / 30} for i in range(3)]
        self.assertEqual(inspection.frame_mapping(rows, 29)["last_global_index"], 102)
        for field, value in (("index", 999), ("episode_index", 28), ("frame_index", 4)):
            changed = copy.deepcopy(rows)
            changed[1][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ContractError, "INSPECTION_FRAME_MAPPING"):
                inspection.frame_mapping(changed, 29)
        with self.assertRaisesRegex(ContractError, "INSPECTION_FRAME_LIMIT"):
            inspection.frame_mapping(rows * inspection.MAX_FRAMES, 29)

    def test_timeout_memory_and_owned_cleanup_leave_unrelated_process_alive(self):
        owned = inspection.NativeInspection()
        self.addCleanup(owned.close)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        self.addCleanup(lambda: (other.terminate(), other.wait(timeout=3)))
        child = owned._spawn([sys.executable, "-c", "import time; time.sleep(20)"], subprocess.DEVNULL)
        with self.assertRaisesRegex(ContractError, "INSPECTION_TIME_LIMIT"):
            owned._check(child, time.monotonic() - 1)
        with mock.patch.object(inspection, "MAX_RSS_BYTES", 0):
            with self.assertRaisesRegex(ContractError, "INSPECTION_MEMORY_LIMIT"):
                owned._check(child, time.monotonic() + 10)
        owned.close()
        self.assertIsNotNone(child.poll())
        self.assertIsNone(other.poll())

    def test_expiry_stops_own_viewer_and_reports_failure(self):
        owned = inspection.NativeInspection()
        self.addCleanup(owned.close)
        child = owned._spawn([sys.executable, "-c", "import time; time.sleep(20)"], subprocess.DEVNULL)
        with mock.patch.object(inspection, "VIEWER_SECONDS", 0):
            owned._watch(child, owned._stop)
        self.assertIsNotNone(child.poll())
        self.assertEqual(owned.snapshot(), {"status": "FAILED", "error": "INSPECTION_TIME_LIMIT"})
