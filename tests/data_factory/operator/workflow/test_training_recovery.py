"""No hardware or trainer: real review transactions on the existing synthetic fixture."""
import json
import subprocess
import threading
import unittest
from pathlib import Path

from tests.data_factory import test_training_approval as fixtures
from tools.data_factory.operator.web.bridge import LoopbackBridge
from tools.data_factory.operator.workflow.training_review import TrainingReviewApplication


ROOT = Path(__file__).resolve().parents[4]


class TrainingRecoveryTest(unittest.TestCase):
    def test_lost_response_reads_once_without_repeating_the_decision(self):
        for action, failed_read, status, label in (
            ("prepare", False, "PREVIEW_NOT_APPROVED", "표시된 에피소드 전체를 검토"),
            ("approve", False, "APPROVED", "학습 사용을 승인했습니다"),
            ("refuse", False, "REFUSED", "승인하지 않았습니다"),
            ("approve", True, "APPROVED", "현재 상태를 다시 확인"),
        ):
            with self.subTest(action=action, failed_read=failed_read):
                fixture = fixtures.NativeBatchTrainingApprovalTest()
                self.addCleanup(fixture.doCleanups)
                fixture.setUp()
                application = TrainingReviewApplication(request=fixture.request,
                    output=fixture.output, approved_by="fixture-human")
                bridge = LoopbackBridge(core=application.bridge_core, ui_root=ROOT / "operator-ui",
                    index_page="training.html", port=0)
                thread = threading.Thread(target=bridge.serve_forever)
                thread.start()
                try:
                    replay = subprocess.run(["node", str(ROOT / "operator-ui/tests/training-recovery.cjs"),
                        bridge.origin, str(ROOT / "operator-ui/training.js"), action,
                        str(failed_read).lower()], capture_output=True, text=True, timeout=15)
                    self.assertEqual(replay.returncode, 0, replay.stderr)
                    report = json.loads(replay.stdout)
                    print(json.dumps({"action": action, "failed_read": failed_read,
                        "status": report["status"], "requests": report["requests"],
                        "canonical_status": report["canonical"]["projection"]["status"]}, ensure_ascii=False))
                    self.assertEqual(report["canonical"]["projection"]["status"], status)
                    self.assertFalse(report["canonical"]["projection"]["starts_training"])
                    self.assertEqual(report["requests"], [
                        {"method": "POST", "path": "/api/intent"},
                        {"method": "GET", "path": "/api/view"}])
                    self.assertIn(label, report["status"])
                    self.assertEqual(report["visibleActions"],
                        ["approve", "refuse"] if action == "prepare" else [])
                    self.assertTrue(report["refreshEnabled"])
                    if action == "approve":
                        fixtures.validate_training_approved_inventory(
                            fixtures.load_json_strict(fixture.output / "training_approved.json"))
                    else:
                        self.assertEqual(list(fixture.output.iterdir()), [])
                finally:
                    bridge.close()
                    thread.join(2)
                self.assertFalse(thread.is_alive())
