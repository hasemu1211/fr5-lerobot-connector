from datetime import datetime, timezone
from pathlib import Path
import tempfile, unittest
from unittest import mock
from tools.data_factory.curator.review.decision import issue_decision, verify_decision
from tools.data_factory.curator.review.manifest import create_manifest
from tools.data_factory.curator.core.jsonio import write_json_exclusive

class DecisionTest(unittest.TestCase):
    def test_foreground_approve_is_digest_bound_and_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary); video = run / "review.mp4"; video.write_bytes(b"video")
            manifest = create_manifest(run / "review_manifest.json", samples=[], identities={}, video=video)
            write_json_exclusive(run / "review_ready.json", {"review_manifest_digest":manifest["review_manifest_digest"], "candidate_tree_digest":"sha256:"+"1"*64, "source_tree_digest":"sha256:"+"2"*64, "profile_digest":"sha256:"+"3"*64})
            with mock.patch("tools.data_factory.curator.review.decision._read_controlling_tty", return_value="APPROVE"):
                value = issue_decision(run, "operator", clock=lambda:datetime(2026,9,3,tzinfo=timezone.utc))
            self.assertEqual(verify_decision(run), value); self.assertFalse(value["training_authorized"])

if __name__ == "__main__": unittest.main()
