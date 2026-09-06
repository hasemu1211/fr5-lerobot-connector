from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.data_factory.curator.support import (
    make_profile_fixture,
    make_source_dataset,
    write_json,
)
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.core.filesystem import OwnedDirectory
from tools.data_factory.curator.core.identity import stable_tree_identity
from tools.data_factory.curator.review.manifest import verify_manifest
from tools.data_factory.curator.workflow import application
from tools.data_factory.curator.workflow.application import (
    _decide_locked,
    _decision_actor,
    _exclusive_run,
    prepare,
    review_candidate,
    status,
    submit_human_review_decision,
)
from tools.data_factory.curator.workflow.state import load_events


class ApplicationTest(unittest.TestCase):
    def test_terminal_receipt_remains_readable_when_review_media_is_unavailable(self):
        for choice, outcome in (("APPROVE", "PUBLISHED"), ("REJECT", "REJECTED")):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = make_source_dataset(root, episodes=1, frames_per_episode=2)
                fixture = make_profile_fixture(root)
                before = stable_tree_identity(source, code="TEST_SOURCE_IDENTITY")
                prepared = prepare(source, _paths=fixture.paths, _run_id_value="media-failure")
                shown = review_candidate("media-failure", _paths=fixture.paths)
                run = fixture.paths.run_root / "media-failure"
                original_events = load_events(run)
                video = Path(prepared["review_video"])
                original_video = video.read_bytes()
                # Deliberate damage to test-owned frozen media, not a native review update.
                video.parent.chmod(0o700)
                video.chmod(0o600)
                kwargs = dict(decision=choice, expected_review_digest=shown["review_ready_digest"], _paths=fixture.paths)
                video.write_bytes(original_video + b"corrupted")
                for operation in (
                    lambda: review_candidate("media-failure", _paths=fixture.paths),
                    lambda: submit_human_review_decision("media-failure", **kwargs),
                ):
                    with self.assertRaisesRegex(CuratorError, "REVIEW_VIDEO_DIGEST"):
                        operation()
                self.assertEqual(load_events(run), original_events)
                video.write_bytes(original_video)

                write_receipt = application._write_receipt
                def corrupt_after_commit(*args, **options):
                    result = write_receipt(*args, **options)
                    video.write_bytes(original_video + b"corrupted after commit")
                    return result

                with mock.patch.object(application, "_write_receipt", side_effect=corrupt_after_commit) as commit:
                    result = submit_human_review_decision("media-failure", **kwargs)
                self.assertEqual(commit.call_count, 1)
                recorded = load_events(run)
                self.assertEqual(result["status"], outcome)
                self.assertEqual(result["receipt"], recorded["receipt"]["payload"])
                self.assertFalse(result["media_available"])
                self.assertEqual(result["media_error"], {"reason_code": "REVIEW_VIDEO_DIGEST"})
                self.assertEqual(result["allowed_decisions"], [])
                self.assertEqual(result["clips"], [])
                for field in ("review_video_path", "review_manifest_path", "coverage", "identities"):
                    self.assertIsNone(result[field])
                self.assertFalse(result["training_authority"])
                with self.assertRaisesRegex(CuratorError, "REVIEW_CHANGED"):
                    submit_human_review_decision("media-failure", **{
                        **kwargs, "expected_review_digest": "sha256:" + "0" * 64,
                    })
                with self.assertRaisesRegex(CuratorError, "DECISION_CONFLICT"):
                    submit_human_review_decision("media-failure", **{
                        **kwargs, "decision": "REJECT" if choice == "APPROVE" else "APPROVE",
                    })
                with mock.patch.object(application, "publish_candidate", side_effect=AssertionError("duplicate publication")), \
                     mock.patch.object(application, "cleanup_candidate", side_effect=AssertionError("duplicate cleanup")):
                    self.assertEqual(review_candidate("media-failure", _paths=fixture.paths), result)
                    self.assertEqual(submit_human_review_decision("media-failure", **kwargs), result)
                self.assertEqual(load_events(run), recorded)
                video.unlink()
                missing = review_candidate("media-failure", _paths=fixture.paths)
                self.assertFalse(missing["media_available"])
                self.assertEqual(missing["receipt"], result["receipt"])
                self.assertEqual(missing["allowed_decisions"], [])
                video.write_bytes(original_video)
                restored = review_candidate("media-failure", _paths=fixture.paths)
                self.assertTrue(restored["media_available"])
                self.assertIsNone(restored["media_error"])
                self.assertTrue(restored["clips"])
                manifest_path = Path(prepared["review_manifest"])
                manifest_path.chmod(0o600)
                manifest_path.write_text("{}")
                damaged_manifest = review_candidate("media-failure", _paths=fixture.paths)
                self.assertFalse(damaged_manifest["media_available"])
                self.assertEqual(damaged_manifest["receipt"], result["receipt"])
                if choice == "APPROVE":
                    output = Path(result["receipt"]["output"]["root"])
                    (output / "meta/info.json").chmod(0o600)
                    (output / "meta/info.json").write_bytes(b"changed published data")
                    with self.assertRaisesRegex(CuratorError, "COMMITTED_OUTPUT_CHANGED"):
                        review_candidate("media-failure", _paths=fixture.paths)
                self.assertEqual(stable_tree_identity(source, code="TEST_SOURCE_IDENTITY"), before)
                self.assertEqual(load_events(run), recorded)

    def test_web_review_choices_bind_exact_evidence_and_replay_without_tty(self):
        for choice, terminal in (("APPROVE", "PUBLISHED"), ("REJECT", "REJECTED")):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = make_source_dataset(root, episodes=2, frames_per_episode=2)
                fixture = make_profile_fixture(root)
                before = stable_tree_identity(source, code="TEST_SOURCE_IDENTITY")
                prepared = prepare(source, _paths=fixture.paths, _run_id_value="web-review")
                run = fixture.paths.run_root / prepared["run_id"]
                initial_events = load_events(run)
                shown = review_candidate(prepared["run_id"], _paths=fixture.paths)
                self.assertEqual(shown["status"], "REVIEW_READY")
                self.assertEqual(shown["allowed_decisions"], ["APPROVE", "REJECT"])
                self.assertTrue(shown["clips"])
                self.assertEqual(shown["review_video_path"], prepared["review_video"])
                self.assertEqual(load_events(run), initial_events)
                self.assertIsNone(shown["decision"])
                self.assertFalse(shown["training_authority"])
                kwargs = dict(expected_review_digest=shown["review_ready_digest"], _paths=fixture.paths)
                for invalid in (None, "", "approve", [], True):
                    with self.assertRaisesRegex(CuratorError, "REVIEW_DECISION"):
                        submit_human_review_decision("web-review", decision=invalid, **kwargs)
                with self.assertRaisesRegex(CuratorError, "REVIEW_CHANGED"):
                    submit_human_review_decision("web-review", decision=choice,
                        expected_review_digest="sha256:" + "0" * 64, _paths=fixture.paths)
                if choice == "APPROVE":
                    prepare(source, _paths=fixture.paths, _run_id_value="other-review")
                    other = review_candidate("other-review", _paths=fixture.paths)
                    with self.assertRaisesRegex(CuratorError, "REVIEW_CHANGED"):
                        submit_human_review_decision("web-review", decision=choice,
                            expected_review_digest=other["review_ready_digest"], _paths=fixture.paths)
                self.assertEqual(load_events(run), initial_events)
                with mock.patch.object(application, "read_foreground_decision", side_effect=AssertionError("TTY forbidden")):
                    with ThreadPoolExecutor(max_workers=2) as workers:
                        results = list(workers.map(lambda _: submit_human_review_decision(
                            "web-review", decision=choice, **kwargs), range(2)))
                    self.assertEqual(results[0], results[1])
                    result = results[0]
                    recorded = load_events(run)
                    self.assertEqual(result["status"], terminal)
                    self.assertEqual(result["allowed_decisions"], [])
                    self.assertEqual(submit_human_review_decision("web-review", decision=choice, **kwargs), result)
                    self.assertEqual(review_candidate("web-review", _paths=fixture.paths), result)
                    with self.assertRaisesRegex(CuratorError, "DECISION_CONFLICT"):
                        submit_human_review_decision("web-review", decision="REJECT" if choice == "APPROVE" else "APPROVE", **kwargs)
                    with self.assertRaisesRegex(CuratorError, "REVIEW_CHANGED"):
                        submit_human_review_decision("web-review", decision=choice,
                            expected_review_digest="sha256:" + "0" * 64, _paths=fixture.paths)
                    self.assertEqual(load_events(run), recorded)
                self.assertFalse(result["receipt"]["approval_inherited"])
                self.assertFalse(result["decision"]["training_authorized"])
                self.assertEqual(stable_tree_identity(source, code="TEST_SOURCE_IDENTITY"), before)

    def test_web_decision_recovers_publication_after_receipt_response_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_source_dataset(root, episodes=1, frames_per_episode=2)
            fixture = make_profile_fixture(root)
            prepare(source, _paths=fixture.paths, _run_id_value="web-recovery")
            shown = review_candidate("web-recovery", _paths=fixture.paths)
            kwargs = dict(decision="APPROVE", expected_review_digest=shown["review_ready_digest"], _paths=fixture.paths)
            with mock.patch.object(application, "_write_receipt", side_effect=OSError("injected receipt failure")):
                with self.assertRaisesRegex(CuratorError, "OUTPUT_COMMITTED_RECEIPT_PENDING"):
                    submit_human_review_decision("web-recovery", **kwargs)
            run = fixture.paths.run_root / "web-recovery"
            decision = load_events(run)["decision"]
            pending = review_candidate("web-recovery", _paths=fixture.paths)
            self.assertEqual(pending["status"], "PUBLISHED_RECEIPT_PENDING")
            self.assertEqual(pending["allowed_decisions"], ["APPROVE"])
            video = Path(pending["review_video_path"])
            original_video = video.read_bytes()
            video.chmod(0o600)
            video.write_bytes(original_video + b"corrupted while receipt pending")
            with self.assertRaisesRegex(CuratorError, "REVIEW_VIDEO_DIGEST"):
                review_candidate("web-recovery", _paths=fixture.paths)
            self.assertNotIn("receipt", load_events(run))
            video.write_bytes(original_video)
            with mock.patch.object(application, "publish_candidate", side_effect=AssertionError("already published")):
                result = submit_human_review_decision("web-recovery", **kwargs)
            self.assertEqual(result["status"], "PUBLISHED")
            self.assertEqual(load_events(run)["decision"], decision)

    def test_native_prepare_review_preserves_source_and_rejects_changed_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_source_dataset(root, episodes=3, frames_per_episode=3)
            fixture = make_profile_fixture(root)
            before = stable_tree_identity(source, code="TEST_SOURCE_IDENTITY")
            prepared = prepare(
                source,
                _paths=fixture.paths,
                _run_id_value="curator-native-review",
            )
            self.assertEqual(prepared["status"], "REVIEW_READY")
            manifest = verify_manifest(
                prepared["review_manifest"], prepared["review_video"],
            )
            self.assertEqual(manifest["coverage"]["population_frames"], 9)
            self.assertEqual(len(manifest["coverage"]["covered_tasks"]), 2)
            self.assertEqual(
                stable_tree_identity(source, code="TEST_SOURCE_IDENTITY"), before
            )
            run = fixture.paths.run_root / prepared["run_id"]
            events = load_events(run)
            self.assertEqual(set(events), {"request", "candidate_ready", "review_ready"})
            self.assertIs(events["request"]["payload"]["training_authority"], False)
            application._validate_evidence(
                run, events, fixture.paths, require_candidate=True
            )
            policy_bytes = fixture.policy_path.read_bytes()
            policy = json.loads(policy_bytes)
            policy["seed"] += 1
            write_json(fixture.policy_path, policy)
            with self.assertRaisesRegex(CuratorError, "CONFIGURATION_CHANGED"):
                application._validate_evidence(
                    run, events, fixture.paths, require_candidate=True
                )
            fixture.policy_path.write_bytes(policy_bytes)
            fixture.profile_path.write_bytes(fixture.profile_path.read_bytes() + b"\n")
            with self.assertRaisesRegex(CuratorError, "CONFIGURATION_CHANGED"):
                application._validate_evidence(
                    run, events, fixture.paths, require_candidate=True
                )
            self.assertEqual(
                stable_tree_identity(source, code="TEST_SOURCE_IDENTITY"), before
            )
            self.assertEqual(load_events(run), events)
            self.assertFalse(Path(events["request"]["payload"]["output_path"]).exists())

    def test_unverified_producer_binding_creates_no_run_or_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = make_profile_fixture(root, width=640, height=480, verified=False)
            source = root / "source"
            source.mkdir()
            (source / "placeholder").write_bytes(b"read-only")
            with self.assertRaisesRegex(CuratorError, "PHYSICAL_BINDING_NOT_VERIFIED"):
                prepare(
                    source, _paths=fixture.paths, _run_id_value="curator-unverified"
                )
            self.assertFalse(fixture.paths.run_root.exists())
            self.assertFalse(fixture.paths.output_parent.exists())

    def test_run_lock_serializes_decision_owners(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "curator-lock-test"
            run.mkdir()
            first_entered = threading.Event()
            release_first = threading.Event()
            second_entered = threading.Event()

            def first_owner():
                with _exclusive_run(run):
                    first_entered.set()
                    release_first.wait(timeout=2)

            def second_owner():
                with _exclusive_run(run):
                    second_entered.set()

            first = threading.Thread(target=first_owner, daemon=True)
            second = threading.Thread(target=second_owner, daemon=True)
            first.start()
            self.assertTrue(first_entered.wait(timeout=1))
            second.start()
            self.assertFalse(second_entered.wait(timeout=0.1))
            release_first.set()
            self.assertTrue(second_entered.wait(timeout=1))
            first.join(timeout=1)
            second.join(timeout=1)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())

    def test_status_rejects_symlinked_run_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "actual-runs"
            actual.mkdir()
            linked = root / "linked-runs"
            linked.symlink_to(actual, target_is_directory=True)
            fixture = make_profile_fixture(root / "fixture")
            paths = replace(fixture.paths, run_root=linked)
            with self.assertRaisesRegex(CuratorError, "RUN_ROOT"):
                status("curator-symlink-test", _paths=paths)

    def test_prepare_rejects_symlinked_output_parent_before_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = make_profile_fixture(root)
            source = root / "source"
            source.mkdir()
            (source / "placeholder").write_bytes(b"read-only")
            fixture.paths.output_parent.mkdir(parents=True)
            actual = root / "actual-derived"
            fixture.paths.output_parent.rename(actual)
            fixture.paths.output_parent.symlink_to(actual, target_is_directory=True)
            with (
                mock.patch.object(
                    application,
                    "materialize_candidate",
                    side_effect=AssertionError("materialization must not start"),
                ),
                self.assertRaisesRegex(CuratorError, "OUTPUT_PARENT"),
            ):
                prepare(
                    source,
                    _paths=fixture.paths,
                    _run_id_value="curator-output-symlink",
                )

    def test_decision_actor_comes_from_uid_database_not_environment(self):
        with (
            mock.patch.dict(
                os.environ,
                {"USER": "spoofed", "LOGNAME": "spoofed", "USERNAME": "spoofed"},
            ),
            mock.patch.object(
                application.pwd,
                "getpwuid",
                return_value=SimpleNamespace(pw_name="uid-database-account"),
            ),
        ):
            actor = _decision_actor()
        self.assertEqual(actor["uid"], os.getuid())
        self.assertEqual(actor["account"], "uid-database-account")
        self.assertIs(actor["human_identity_authenticated"], False)

    def test_decision_only_crash_state_never_reprompts_or_repeats_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = make_profile_fixture(root)
            fixture.paths.output_parent.mkdir(parents=True)
            run = fixture.paths.run_root / "curator-decision-only"
            run.mkdir(parents=True)
            candidate_path = fixture.paths.output_parent / ".candidate"
            candidate_path.mkdir()
            owned = OwnedDirectory.capture(candidate_path)
            for choice in ("APPROVE", "REJECT"):
                events = {
                    "request": {
                        "payload": {
                            "candidate_path": str(candidate_path),
                            "output_path": str(fixture.paths.output_parent / "output"),
                        }
                    },
                    "candidate_ready": {"payload": {"candidate": owned.as_json()}},
                    "decision": {
                        "event_digest": "sha256:" + "1" * 64,
                        "payload": {"decision": choice},
                    },
                }
                with (
                    self.subTest(choice=choice),
                    mock.patch.object(application, "load_events", return_value=events),
                    mock.patch.object(
                        application,
                        "read_foreground_decision",
                        side_effect=AssertionError("must not reprompt"),
                    ),
                    self.assertRaisesRegex(CuratorError, "RUN_DECISION_INCOMPLETE"),
                ):
                    _decide_locked(run, fixture.paths)


if __name__ == "__main__":
    unittest.main()
