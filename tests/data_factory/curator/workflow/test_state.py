from __future__ import annotations

import json
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.workflow.state import (
    append_event,
    load_events,
    project_state,
)


DIGEST = "sha256:" + "1" * 64
ACTOR = {
    "kind": "LOCAL_OS_ACCOUNT",
    "uid": 1000,
    "account": "operator",
    "human_identity_authenticated": False,
}


def request_payload() -> dict:
    return {
        "source": "/source",
        "source_repo_id": "local/source",
        "source_snapshot": {},
        "source_tree_digest": DIGEST,
        "profile_id": "profile",
        "profile_path": "/profile",
        "profile_file_sha256": DIGEST,
        "profile_digest": DIGEST,
        "policy_id": "policy",
        "policy_path": "/policy",
        "policy_file_sha256": DIGEST,
        "policy_digest": DIGEST,
        "candidate_path": "/.candidate",
        "candidate_repo_id": "local/candidate",
        "output_path": "/output",
        "candidate_owner_nonce": "nonce",
        "placement_lineage": "PLACEMENT_LINEAGE_UNPROVEN",
        "training_authority": False,
    }


def candidate_payload(request_digest: str) -> dict:
    return {
        "request_digest": request_digest,
        "candidate": {},
        "candidate_tree_digest": DIGEST,
        "materialization": {},
        "source_tree_digest": DIGEST,
        "profile_digest": DIGEST,
        "policy_digest": DIGEST,
        "candidate_owner_nonce": "nonce",
    }


def ready_payload(request_digest: str) -> dict:
    return {
        "request_digest": request_digest,
        "candidate_tree_digest": DIGEST,
        "source_tree_digest": DIGEST,
        "profile_digest": DIGEST,
        "policy_digest": DIGEST,
        "review_manifest_digest": DIGEST,
        "review_video_sha256": DIGEST,
        "review_manifest_path": "/review/manifest.json",
        "review_video_path": "/review/review.mp4",
    }


class StateTest(unittest.TestCase):
    def test_exact_ambiguous_event_is_recovered_only_after_directory_fsync(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run-ambiguous-event"
            run.mkdir()
            real_fsync = os.fsync
            fsync_calls = 0

            def fail_first_parent_fsync(descriptor):
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError(errno.EIO, "injected first parent fsync failure")
                return real_fsync(descriptor)

            with mock.patch(
                "tools.data_factory.curator.core.filesystem.os.fsync",
                side_effect=fail_first_parent_fsync,
            ):
                request = append_event(run, "request", request_payload(), None)
            self.assertGreaterEqual(fsync_calls, 3)
            self.assertEqual(load_events(run)["request"], request)
            self.assertFalse(any(path.name.endswith(".tmp") for path in run.iterdir()))

    def test_chain_is_immutable_and_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run-1"
            run.mkdir()
            request = append_event(run, "request", request_payload(), None)
            self.assertEqual(load_events(run)["request"], request)
            with self.assertRaisesRegex(CuratorError, "EVENT_EXISTS"):
                append_event(run, "request", request_payload(), None)
            path = run / "request.json"
            changed = json.loads(path.read_text())
            changed["payload"]["output_path"] = "/other"
            path.chmod(0o600)
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(CuratorError, "RUN_EVENT_CONTRACT"):
                load_events(run)

    def test_rejected_receipt_projects_rejected_not_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run-2"
            run.mkdir()
            request = append_event(run, "request", request_payload(), None)
            candidate = append_event(
                run,
                "candidate_ready",
                candidate_payload(request["event_digest"]),
                request["event_digest"],
            )
            ready = append_event(
                run,
                "review_ready",
                ready_payload(request["event_digest"]),
                candidate["event_digest"],
            )
            decision = append_event(
                run,
                "decision",
                {
                    "decision": "REJECT",
                    "actor": ACTOR,
                    "decided_at": "2026-09-03T00:00:00Z",
                    "source_tree_digest": DIGEST,
                    "candidate_tree_digest": DIGEST,
                    "profile_digest": DIGEST,
                    "policy_digest": DIGEST,
                    "review_manifest_digest": DIGEST,
                    "review_video_sha256": DIGEST,
                    "output_path": "/output",
                    "candidate": {},
                    "provenance": "HUMAN_CURATED_CANDIDATE_REJECTED",
                    "training_authorized": False,
                },
                ready["event_digest"],
            )
            append_event(
                run,
                "receipt",
                {
                    "outcome": "REJECTED",
                    "source": {
                        "root": "/source",
                        "repo_id": "local/source",
                        "dataset_digest": DIGEST,
                    },
                    "output": None,
                    "candidate_tree_digest": DIGEST,
                    "profile_digest": DIGEST,
                    "review_manifest_digest": DIGEST,
                    "decision_digest": decision["event_digest"],
                    "training_authority": False,
                    "approval_inherited": False,
                    "committed_durable": False,
                },
                decision["event_digest"],
            )
            self.assertEqual(project_state(run)["status"], "REJECTED")

    def test_self_consistent_event_hash_cannot_cross_bind_other_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run-cross-binding"
            run.mkdir()
            request = append_event(run, "request", request_payload(), None)
            forged = candidate_payload(request["event_digest"])
            forged["source_tree_digest"] = "sha256:" + "2" * 64
            append_event(
                run,
                "candidate_ready",
                forged,
                request["event_digest"],
            )
            with self.assertRaisesRegex(CuratorError, "RUN_CANDIDATE_BINDING"):
                load_events(run)

    def test_self_consistent_receipt_cannot_claim_the_wrong_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run-receipt-binding"
            run.mkdir()
            request = append_event(run, "request", request_payload(), None)
            candidate = append_event(
                run,
                "candidate_ready",
                candidate_payload(request["event_digest"]),
                request["event_digest"],
            )
            ready = append_event(
                run,
                "review_ready",
                ready_payload(request["event_digest"]),
                candidate["event_digest"],
            )
            decision = append_event(
                run,
                "decision",
                {
                    "decision": "REJECT",
                    "actor": ACTOR,
                    "decided_at": "2026-09-03T00:00:00Z",
                    "source_tree_digest": DIGEST,
                    "candidate_tree_digest": DIGEST,
                    "profile_digest": DIGEST,
                    "policy_digest": DIGEST,
                    "review_manifest_digest": DIGEST,
                    "review_video_sha256": DIGEST,
                    "output_path": "/output",
                    "candidate": {},
                    "provenance": "HUMAN_CURATED_CANDIDATE_REJECTED",
                    "training_authorized": False,
                },
                ready["event_digest"],
            )
            append_event(
                run,
                "receipt",
                {
                    "outcome": "REJECTED",
                    "source": {
                        "root": "/wrong-source",
                        "repo_id": "local/source",
                        "dataset_digest": DIGEST,
                    },
                    "output": None,
                    "candidate_tree_digest": DIGEST,
                    "profile_digest": DIGEST,
                    "review_manifest_digest": DIGEST,
                    "decision_digest": decision["event_digest"],
                    "training_authority": False,
                    "approval_inherited": False,
                    "committed_durable": False,
                },
                decision["event_digest"],
            )
            with self.assertRaisesRegex(CuratorError, "RUN_RECEIPT_BINDING"):
                load_events(run)

    def test_illegal_gap_and_unknown_root_json_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run-3"
            run.mkdir()
            (run / "unknown.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(CuratorError, "RUN_UNKNOWN_EVENT"):
                load_events(run)

    def test_symlinked_run_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual_parent = root / "actual"
            actual_parent.mkdir()
            run = actual_parent / "run-symlink-ancestor"
            run.mkdir()
            append_event(run, "request", request_payload(), None)
            linked_parent = root / "linked"
            linked_parent.symlink_to(actual_parent, target_is_directory=True)
            with self.assertRaisesRegex(CuratorError, "RUN_PATH"):
                load_events(linked_parent / run.name)


if __name__ == "__main__":
    unittest.main()
