from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.curator import approval
from tools.curator.contracts import CuratorError, reject_symlink_components, rename_noreplace, write_json_atomic


class ApprovalTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.request = SimpleNamespace(
            approval_path=self.root / "approval.json",
            review_bundle_path=self.root / "review",
        )
        self.profile = {
            "profile_id": "profile-r001",
            "profile_digest": "sha256:" + "1" * 64,
            "physical_binding_status": "VERIFIED",
        }
        self.manifest = {"review_bundle_digest": "sha256:" + "2" * 64}

    def tearDown(self):
        self.temporary.cleanup()

    def test_exact_phrase_and_exclusive_artifact_without_test_issuance(self):
        phrase = approval.approval_phrase(
            self.profile["profile_digest"], self.manifest["review_bundle_digest"],
        )
        self.assertEqual(
            phrase,
            "APPROVE HUMAN_TASK_VIEW_APPROVED sha256:"
            + "1" * 64
            + " sha256:"
            + "2" * 64,
        )
        with (
            mock.patch.object(approval, "verify_review_bundle", return_value=(self.request, self.profile, self.manifest)),
            mock.patch.object(approval, "_read_controlling_tty", return_value=phrase),
            mock.patch.object(approval, "write_json_exclusive") as exclusive,
        ):
            issued = approval.issue_approval(
                self.root / "request.json",
                "operator-1",
                clock=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc),
            )
        exclusive.assert_called_once_with(self.request.approval_path, issued)
        self.assertEqual(issued["provenance"], "HUMAN_TASK_VIEW_APPROVED")
        self.assertEqual(issued["issuance_path"], "FOREGROUND_CONTROLLING_/dev/tty")
        self.assertEqual(
            issued["identity_assurance"],
            "LOCAL_TTY_PRESENCE_NOT_CRYPTOGRAPHIC_IDENTITY",
        )
        self.assertIs(issued["training_authorized"], False)
        self.assertFalse(self.request.approval_path.exists())

    def test_prepared_ai_wrong_phrase_existing_and_missing_tty_fail_closed(self):
        prepared = {**self.profile, "physical_binding_status": "PREPARED_NOT_VERIFIED"}
        with mock.patch.object(approval, "verify_review_bundle", return_value=(self.request, prepared, self.manifest)):
            with self.assertRaisesRegex(CuratorError, "PHYSICAL_BINDING_NOT_VERIFIED"):
                approval.issue_approval(self.root / "request.json", "operator-1")
        with self.assertRaisesRegex(CuratorError, "APPROVED_BY_AUTOMATED"):
            approval.issue_approval(self.root / "request.json", "codex-agent")

        phrase = approval.approval_phrase(self.profile["profile_digest"], self.manifest["review_bundle_digest"])
        with (
            mock.patch.object(approval, "verify_review_bundle", return_value=(self.request, self.profile, self.manifest)),
            mock.patch.object(approval, "_read_controlling_tty", return_value=phrase + " "),
        ):
            with self.assertRaisesRegex(CuratorError, "HUMAN_APPROVAL_PHRASE"):
                approval.issue_approval(self.root / "request.json", "operator-1")

        self.request.approval_path.write_text("occupied", encoding="utf-8")
        with mock.patch.object(approval, "verify_review_bundle", return_value=(self.request, self.profile, self.manifest)):
            with self.assertRaisesRegex(CuratorError, "APPROVAL_EXISTS"):
                approval.issue_approval(self.root / "request.json", "operator-1")
        self.request.approval_path.unlink()
        with mock.patch.object(approval.os, "open", side_effect=OSError("no controlling tty")) as opened:
            with self.assertRaisesRegex(CuratorError, "HUMAN_TTY_REQUIRED"):
                approval._read_controlling_tty("prompt")
        self.assertEqual(opened.call_args.args[0], "/dev/tty")

    def test_test_fixture_provenance_is_never_accepted(self):
        artifact = {
            "schema_version": "curator.human_task_view_approval.v2",
            "scope": "HUMAN_TASK_VIEW",
            "profile_id": self.profile["profile_id"],
            "profile_digest": self.profile["profile_digest"],
            "review_bundle_digest": self.manifest["review_bundle_digest"],
            "approved_by": "operator-1",
            "approved_at": "2026-09-02T00:00:00Z",
            "provenance": "TEST_ONLY",
            "issuance_path": "TEST_ONLY",
            "identity_assurance": "TEST_ONLY_MOCKED_AUTHORITY",
            "training_authorized": False,
            "approval_digest": "sha256:" + "3" * 64,
        }
        write_json_atomic(self.request.approval_path, artifact)
        with mock.patch.object(approval, "verify_review_bundle", return_value=(self.request, self.profile, self.manifest)):
            with self.assertRaisesRegex(CuratorError, "APPROVAL_CONTRACT"):
                approval.verify_approval(self.root / "request.json", self.request.approval_path)

    def test_no_clobber_publication_and_symlink_components(self):
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        target.mkdir()
        (source / "owned").write_text("source", encoding="utf-8")
        (target / "racer").write_text("target", encoding="utf-8")
        with self.assertRaisesRegex(CuratorError, "OUTPUT_EXISTS"):
            rename_noreplace(source, target, code="OUTPUT_EXISTS")
        self.assertEqual((source / "owned").read_text(), "source")
        self.assertEqual((target / "racer").read_text(), "target")

        link = self.root / "link"
        link.symlink_to(source, target_is_directory=True)
        with self.assertRaisesRegex(CuratorError, "SYMLINK"):
            reject_symlink_components(link / "owned", "SYMLINK")


if __name__ == "__main__":
    unittest.main()
