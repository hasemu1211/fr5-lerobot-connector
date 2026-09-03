from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.data_factory.curator.support import make_profile_fixture
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.core.filesystem import OwnedDirectory
from tools.data_factory.curator.workflow import application
from tools.data_factory.curator.workflow.application import (
    _decide_locked,
    _decision_actor,
    _exclusive_run,
    prepare,
    status,
)


class ApplicationTest(unittest.TestCase):
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
