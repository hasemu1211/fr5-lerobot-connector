from __future__ import annotations

from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import cv2
import numpy as np

from tests.data_factory.curator.support import make_profile_fixture, make_source_dataset
from tools.data_factory.curator.core import filesystem
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.core.filesystem import OwnedDirectory
from tools.data_factory.curator.core.identity import file_sha256, tree_snapshot
from tools.data_factory.curator.dataset import materialize
from tools.data_factory.curator.dataset import publish as dataset_publish
from tools.data_factory.curator.dataset.publish import candidate_action_path
from tools.data_factory.curator.dataset.source import open_source_dataset
from tools.data_factory.curator.profile.registry import load_profile_assets
from tools.data_factory.curator.profile.transform import apply_up_view, uint8_hwc
from tools.data_factory.curator.workflow import application
from tools.data_factory.curator.workflow.application import decide, prepare, status
from tools.data_factory.curator.workflow.state import load_events


class CandidateFlowTest(unittest.TestCase):
    def test_writer_faults_shutdown_before_owned_cleanup(self):
        try:
            from lerobot.datasets.lerobot_dataset import LeRobotDataset
        except ImportError as exc:
            self.skipTest(f"LeRobot unavailable: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_source_dataset(root, episodes=1, frames_per_episode=1)
            fixture = make_profile_fixture(root)
            fixture.paths.output_parent.mkdir(parents=True)
            source_before = tree_snapshot(source)
            original_finalize = LeRobotDataset.finalize

            for fault in ("add", "save", "finalize", "interrupt"):
                with self.subTest(fault=fault):
                    run_id = f"writer-fault-{fault}"
                    candidate = fixture.paths.output_parent / f"{fault}-candidate"
                    temporary = (
                        candidate.parent / f".{candidate.name}.{run_id}.curator-tmp"
                    )
                    marker = (
                        candidate.parent
                        / f".{candidate.name}.{run_id}.curator-owner.json"
                    )
                    finalize_observations: list[bool] = []
                    finalize_calls = 0

                    def finalize(writer):
                        nonlocal finalize_calls
                        finalize_calls += 1
                        finalize_observations.append(temporary.is_dir())
                        if fault == "finalize" and finalize_calls == 1:
                            raise RuntimeError("injected finalize fault")
                        return original_finalize(writer)

                    patches = [
                        mock.patch.object(LeRobotDataset, "finalize", new=finalize)
                    ]
                    if fault in {"add", "interrupt"}:
                        exception = (
                            KeyboardInterrupt()
                            if fault == "interrupt"
                            else RuntimeError("injected add fault")
                        )
                        patches.append(
                            mock.patch.object(
                                LeRobotDataset,
                                "add_frame",
                                side_effect=exception,
                            )
                        )
                    elif fault == "save":
                        patches.append(
                            mock.patch.object(
                                LeRobotDataset,
                                "save_episode",
                                side_effect=RuntimeError("injected save fault"),
                            )
                        )

                    expected_exception = (
                        KeyboardInterrupt if fault == "interrupt" else RuntimeError
                    )
                    with self.assertRaises(expected_exception), ExitStack() as stack:
                        for patcher in patches:
                            stack.enter_context(patcher)
                        materialize.materialize_candidate(
                            source,
                            candidate,
                            fixture.resolved,
                            run_id=run_id,
                            source_repo_id="local/source",
                            candidate_repo_id=f"local/{fault}-candidate",
                        )
                    self.assertTrue(finalize_observations)
                    self.assertTrue(all(finalize_observations))
                    self.assertFalse(candidate.exists())
                    self.assertFalse(temporary.exists())
                    self.assertFalse(marker.exists())
                    self.assertEqual(tree_snapshot(source), source_before)

    def test_existing_validator_cannot_mutate_the_candidate_it_validates(self):
        try:
            import lerobot  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"LeRobot unavailable: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_source_dataset(root, episodes=1, frames_per_episode=1)
            fixture = make_profile_fixture(root)
            original_validator = materialize.run_existing_validator

            def validate_then_mutate(candidate_root, repo_id):
                result = original_validator(candidate_root, repo_id)
                evidence = (
                    Path(candidate_root) / "meta/source_provenance/episode-000000.jsonl"
                )
                with evidence.open("ab") as stream:
                    stream.write(b"{}\n")
                return result

            with mock.patch.object(
                materialize,
                "run_existing_validator",
                side_effect=validate_then_mutate,
            ):
                with self.assertRaisesRegex(
                    CuratorError,
                    "EXISTING_VALIDATOR_MUTATED_CANDIDATE",
                ):
                    prepare(
                        source,
                        _paths=fixture.paths,
                        _run_id_value="curator-validator-mutation",
                    )
            self.assertFalse(
                (fixture.paths.output_parent / "source-synthetic-up-view-r001").exists()
            )
            self.assertFalse(
                any(
                    path.name.endswith(".candidate") or ".curator-tmp" in path.name
                    for path in fixture.paths.output_parent.iterdir()
                )
            )

            original_commit = materialize.commit_hidden_candidate

            def commit_then_interrupt(*args, **kwargs):
                original_commit(*args, **kwargs)
                raise KeyboardInterrupt

            hidden_commit_run = "curator-hidden-commit-interrupt"
            with mock.patch.object(
                materialize,
                "commit_hidden_candidate",
                side_effect=commit_then_interrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    prepare(
                        source,
                        _paths=fixture.paths,
                        _run_id_value=hidden_commit_run,
                    )
            hidden_events = load_events(fixture.paths.run_root / hidden_commit_run)
            hidden_candidate = Path(
                hidden_events["request"]["payload"]["candidate_path"]
            )
            self.assertFalse(hidden_candidate.exists())
            self.assertFalse(
                any(
                    hidden_commit_run in path.name
                    for path in fixture.paths.output_parent.iterdir()
                )
            )

            original_append = application.append_event

            def append_candidate_then_interrupt(run, event, payload, previous):
                result = original_append(run, event, payload, previous)
                if event == "candidate_ready":
                    raise KeyboardInterrupt
                return result

            interrupted_run = "curator-candidate-event-interrupt"
            with mock.patch.object(
                application,
                "append_event",
                side_effect=append_candidate_then_interrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    prepare(
                        source,
                        _paths=fixture.paths,
                        _run_id_value=interrupted_run,
                    )
            interrupted_events = load_events(fixture.paths.run_root / interrupted_run)
            self.assertEqual(
                interrupted_events["failure"]["previous_event_digest"],
                interrupted_events["candidate_ready"]["event_digest"],
            )
            interrupted_candidate = Path(
                interrupted_events["request"]["payload"]["candidate_path"]
            )
            self.assertFalse(interrupted_candidate.exists())

    def test_materializer_return_interrupt_uses_caller_owned_handoff_for_cleanup(self):
        try:
            import lerobot  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"LeRobot unavailable: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_source_dataset(root, episodes=1, frames_per_episode=1)
            fixture = make_profile_fixture(root)
            original_materialize = application.materialize_candidate

            def return_then_interrupt(*args, **kwargs):
                original_materialize(*args, **kwargs)
                raise KeyboardInterrupt

            run_id = "curator-materializer-return-interrupt"
            with (
                mock.patch.object(
                    application,
                    "materialize_candidate",
                    side_effect=return_then_interrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                prepare(source, _paths=fixture.paths, _run_id_value=run_id)
            events = load_events(fixture.paths.run_root / run_id)
            candidate = Path(events["request"]["payload"]["candidate_path"])
            self.assertFalse(candidate.exists())
            self.assertEqual(events["failure"]["payload"]["cleanup_state"], "REMOVED")
            self.assertFalse(
                any(
                    run_id in path.name
                    for path in fixture.paths.output_parent.iterdir()
                )
            )

    def test_hidden_candidate_parent_fsync_failure_cleans_exact_committed_inode(self):
        try:
            import lerobot  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"LeRobot unavailable: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_source_dataset(root, episodes=1, frames_per_episode=1)
            fixture = make_profile_fixture(root)
            fixture.paths.output_parent.mkdir(parents=True)
            parent_identity = (
                fixture.paths.output_parent.stat().st_dev,
                fixture.paths.output_parent.stat().st_ino,
            )
            run_id = "curator-hidden-parent-fsync"
            candidate = (
                fixture.paths.output_parent
                / f".source-synthetic-up-view-r001.{run_id}.candidate"
            )
            real_fsync = os.fsync
            failed = False

            def fail_after_hidden_rename(descriptor):
                nonlocal failed
                details = os.fstat(descriptor)
                if (
                    not failed
                    and candidate.exists()
                    and (details.st_dev, details.st_ino) == parent_identity
                ):
                    failed = True
                    raise OSError(errno.EIO, "injected hidden parent fsync failure")
                return real_fsync(descriptor)

            with (
                mock.patch.object(
                    filesystem.os,
                    "fsync",
                    side_effect=fail_after_hidden_rename,
                ),
                self.assertRaisesRegex(CuratorError, "OUTPUT_PUBLISH"),
            ):
                prepare(source, _paths=fixture.paths, _run_id_value=run_id)
            self.assertTrue(failed)
            self.assertFalse(candidate.exists())
            self.assertFalse(
                any(
                    run_id in path.name
                    for path in fixture.paths.output_parent.iterdir()
                )
            )

    def test_publish_parent_fsync_failure_recovers_without_reprompt_or_live_source(
        self,
    ):
        try:
            import lerobot  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"LeRobot unavailable: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_source_dataset(root, episodes=1, frames_per_episode=1)
            fixture = make_profile_fixture(root)
            run_id = "curator-publish-parent-fsync"
            prepare(source, _paths=fixture.paths, _run_id_value=run_id)
            events = load_events(fixture.paths.run_root / run_id)
            output = Path(events["request"]["payload"]["output_path"])
            output_parent_identity = (
                output.parent.stat().st_dev,
                output.parent.stat().st_ino,
            )
            real_fsync = os.fsync
            failed = False

            def fail_after_publish_rename(descriptor):
                nonlocal failed
                details = os.fstat(descriptor)
                if (
                    not failed
                    and output.exists()
                    and (details.st_dev, details.st_ino) == output_parent_identity
                ):
                    failed = True
                    raise OSError(errno.EIO, "injected publish parent fsync failure")
                return real_fsync(descriptor)

            with (
                mock.patch.object(
                    application,
                    "read_foreground_decision",
                    return_value="APPROVE",
                ),
                mock.patch.object(
                    filesystem.os,
                    "fsync",
                    side_effect=fail_after_publish_rename,
                ),
                self.assertRaisesRegex(
                    CuratorError, "OUTPUT_COMMITTED_RECEIPT_PENDING"
                ),
            ):
                application.decide(run_id, _paths=fixture.paths)
            self.assertTrue(failed)
            self.assertTrue(output.is_dir())
            pending = load_events(fixture.paths.run_root / run_id)
            self.assertEqual(
                pending["failure"]["payload"]["state"],
                "PUBLISHED_RECEIPT_PENDING",
            )

            source.rename(root / "archived-source")
            fixture.profile_path.unlink()
            fixture.policy_path.unlink()
            with mock.patch.object(
                application,
                "read_foreground_decision",
                side_effect=AssertionError("recovery must not reprompt"),
            ):
                recovered = application.decide(run_id, _paths=fixture.paths)
            self.assertEqual(recovered["status"], "PUBLISHED")

    def test_completed_actions_without_failure_event_recover_for_both_decisions(self):
        try:
            import lerobot  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"LeRobot unavailable: {exc}")
        for choice, expected_status in (
            ("APPROVE", "PUBLISHED"),
            ("REJECT", "REJECTED"),
        ):
            with (
                self.subTest(choice=choice),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                source = make_source_dataset(root, episodes=1, frames_per_episode=1)
                fixture = make_profile_fixture(root)
                run_id = f"curator-decision-only-{choice.lower()}"
                prepare(source, _paths=fixture.paths, _run_id_value=run_id)
                with (
                    mock.patch.object(
                        application,
                        "read_foreground_decision",
                        return_value=choice,
                    ),
                    mock.patch.object(
                        application,
                        "_write_receipt",
                        side_effect=OSError("injected pre-receipt interruption"),
                    ),
                    mock.patch.object(
                        application,
                        "_pending_failure",
                        side_effect=KeyboardInterrupt,
                    ),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    application.decide(run_id, _paths=fixture.paths)
                run = fixture.paths.run_root / run_id
                interrupted = load_events(run)
                self.assertIn("decision", interrupted)
                self.assertNotIn("failure", interrupted)
                self.assertNotIn("receipt", interrupted)

                source.rename(root / "archived-source")
                fixture.profile_path.unlink()
                fixture.policy_path.unlink()
                with mock.patch.object(
                    application,
                    "read_foreground_decision",
                    side_effect=AssertionError("recovery must not reprompt"),
                ):
                    recovered = application.decide(run_id, _paths=fixture.paths)
                self.assertEqual(recovered["status"], expected_status)

    def test_reject_resumes_exact_action_stage_after_cleanup_interruption(self):
        try:
            import lerobot  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"LeRobot unavailable: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_source_dataset(root, episodes=1, frames_per_episode=1)
            fixture = make_profile_fixture(root)
            run_id = "curator-reject-action-stage"
            prepare(source, _paths=fixture.paths, _run_id_value=run_id)
            interrupted = False
            original_fwalk = os.fwalk

            def interrupt_first_cleanup(*args, **kwargs):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt
                return original_fwalk(*args, **kwargs)

            with (
                mock.patch.object(
                    application,
                    "read_foreground_decision",
                    return_value="REJECT",
                ),
                mock.patch.object(
                    filesystem.os,
                    "fwalk",
                    side_effect=interrupt_first_cleanup,
                ),
                mock.patch.object(
                    application,
                    "_pending_failure",
                    side_effect=KeyboardInterrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                application.decide(run_id, _paths=fixture.paths)
            run = fixture.paths.run_root / run_id
            events = load_events(run)
            self.assertIn("decision", events)
            self.assertNotIn("failure", events)
            self.assertNotIn("receipt", events)
            owner = OwnedDirectory.from_json(
                events["candidate_ready"]["payload"]["candidate"]
            )
            digest = events["candidate_ready"]["payload"]["candidate_tree_digest"]
            stage = candidate_action_path(owner, digest, "reject")
            self.assertFalse(owner.path.exists())
            self.assertTrue(stage.is_dir())

            source.rename(root / "archived-source")
            fixture.profile_path.unlink()
            fixture.policy_path.unlink()
            with mock.patch.object(
                application,
                "read_foreground_decision",
                side_effect=AssertionError("recovery must not reprompt"),
            ):
                recovered = application.decide(run_id, _paths=fixture.paths)
            self.assertEqual(recovered["status"], "REJECTED")
            self.assertFalse(stage.exists())

    def test_publish_resumes_exact_action_stage_after_interruption(self):
        try:
            import lerobot  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"LeRobot unavailable: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_source_dataset(root, episodes=1, frames_per_episode=1)
            fixture = make_profile_fixture(root)
            run_id = "curator-publish-action-stage"
            prepare(source, _paths=fixture.paths, _run_id_value=run_id)

            def stage_then_interrupt(owned, _output, digest, **_kwargs):
                stage = candidate_action_path(owned, digest, "publish")
                filesystem.rename_noreplace_at(owned, stage)
                raise KeyboardInterrupt

            with (
                mock.patch.object(
                    application,
                    "read_foreground_decision",
                    return_value="APPROVE",
                ),
                mock.patch.object(
                    application,
                    "publish_candidate",
                    side_effect=stage_then_interrupt,
                ),
                self.assertRaisesRegex(CuratorError, "PUBLISH_ACTION_PENDING"),
            ):
                application.decide(run_id, _paths=fixture.paths)
            run = fixture.paths.run_root / run_id
            events = load_events(run)
            self.assertEqual(
                events["failure"]["payload"]["state"],
                "PUBLISH_ACTION_PENDING",
            )
            owner = OwnedDirectory.from_json(
                events["candidate_ready"]["payload"]["candidate"]
            )
            digest = events["candidate_ready"]["payload"]["candidate_tree_digest"]
            stage = candidate_action_path(owner, digest, "publish")
            output = Path(events["request"]["payload"]["output_path"])
            self.assertFalse(owner.path.exists())
            self.assertTrue(stage.is_dir())
            self.assertFalse(output.exists())

            source.rename(root / "archived-source")
            fixture.profile_path.unlink()
            fixture.policy_path.unlink()
            with mock.patch.object(
                application,
                "read_foreground_decision",
                side_effect=AssertionError("recovery must not reprompt"),
            ):
                recovered = application.decide(run_id, _paths=fixture.paths)
            self.assertEqual(recovered["status"], "PUBLISHED")
            self.assertTrue(output.is_dir())
            self.assertFalse(stage.exists())

    def test_decide_rejects_output_ancestor_symlink_before_prompt_or_action(self):
        try:
            import lerobot  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"LeRobot unavailable: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_source_dataset(root, episodes=1, frames_per_episode=1)
            fixture = make_profile_fixture(root)
            run_id = "curator-output-ancestor-symlink"
            prepare(source, _paths=fixture.paths, _run_id_value=run_id)
            relocated = root / "relocated-derived"
            fixture.paths.output_parent.rename(relocated)
            fixture.paths.output_parent.symlink_to(relocated, target_is_directory=True)
            with (
                mock.patch.object(
                    application,
                    "read_foreground_decision",
                    side_effect=AssertionError("prompt must not open"),
                ),
                self.assertRaisesRegex(CuratorError, "OUTPUT_PARENT"),
            ):
                application.decide(run_id, _paths=fixture.paths)
            self.assertTrue(
                any(path.name.endswith(".candidate") for path in relocated.iterdir())
            )

    def test_real_h264_traceable_candidates_publish_and_reject_recovery(self):
        try:
            import lerobot  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"LeRobot unavailable: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_source_dataset(root, episodes=2, frames_per_episode=2)
            fixture = make_profile_fixture(root)

            rejected_run = "curator-integration-reject"
            approved_run = "curator-integration-approve"
            rejected_prepared = prepare(
                source,
                _paths=fixture.paths,
                _run_id_value=rejected_run,
            )
            original_append = application.append_event

            def append_review_then_interrupt(run, event, payload, previous):
                result = original_append(run, event, payload, previous)
                if event == "review_ready":
                    raise KeyboardInterrupt
                return result

            with mock.patch.object(
                application,
                "append_event",
                side_effect=append_review_then_interrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    prepare(
                        source,
                        _paths=fixture.paths,
                        _run_id_value=approved_run,
                    )
            approved_prepared = status(approved_run, _paths=fixture.paths)
            self.assertEqual(rejected_prepared["status"], "REVIEW_READY")
            self.assertEqual(approved_prepared["status"], "REVIEW_READY")
            self.assertFalse(
                (fixture.paths.output_parent / "source-synthetic-up-view-r001").exists()
            )
            run = fixture.paths.run_root / approved_run
            events = load_events(run)
            request = events["request"]["payload"]
            candidate_path = Path(request["candidate_path"])
            manifest = json.loads(
                (run / "review/manifest.json").read_text(encoding="utf-8")
            )
            selected = manifest["clips"][0]["dataset_indices"][0]

            lineage = json.loads(
                (candidate_path / "meta/curator_lineage.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(lineage["schema_version"], "curator.dataset_lineage.v1")
            self.assertEqual(
                lineage["source"],
                {
                    "root": str(source),
                    "repo_id": request["source_repo_id"],
                    "dataset_tree_digest": request["source_tree_digest"],
                },
            )
            self.assertEqual(
                lineage["episode_mapping"],
                {
                    "contract": "IDENTICAL_EPISODE_FRAME_INDEX",
                    "episodes": 2,
                    "frames": 4,
                },
            )
            self.assertEqual(
                lineage["transform"]["profile_digest"], request["profile_digest"]
            )
            self.assertEqual(
                lineage["transform"]["mask_sha256"],
                fixture.resolved.profile["mask_sha256"],
            )
            self.assertEqual(
                lineage["transform"]["background_plate_sha256"],
                fixture.resolved.profile["background_plate_sha256"],
            )
            self.assertEqual(
                lineage["external_producer_evidence"],
                "PRODUCER_RUN_EVIDENCE_EXTERNAL_UNBOUND_NOT_COPIED",
            )
            self.assertIs(lineage["training_authority"], False)
            for name, digest in lineage["source_provenance"]["files"].items():
                source_evidence = source / "meta/source_provenance" / name
                derived_evidence = candidate_path / "meta/source_provenance" / name
                self.assertEqual(
                    source_evidence.read_bytes(), derived_evidence.read_bytes()
                )
                self.assertEqual(file_sha256(derived_evidence), digest)

            source_dataset = open_source_dataset(source, request["source_repo_id"])
            candidate_dataset = open_source_dataset(
                candidate_path, request["candidate_repo_id"]
            )
            mask, plate = load_profile_assets(fixture.resolved)
            raw = uint8_hwc(
                source_dataset[selected]["observation.images.up"],
                width=640,
                height=480,
            )
            candidate = uint8_hwc(
                candidate_dataset[selected]["observation.images.up"],
                width=640,
                height=480,
            )
            expected = apply_up_view(raw, mask, plate)
            self.assertLess(
                float(
                    np.abs(
                        candidate.astype(np.int16) - expected.astype(np.int16)
                    ).mean()
                ),
                8.0,
            )

            capture = cv2.VideoCapture(str(run / "review/review.mp4"))
            ok, bgr = capture.read()
            capture.release()
            self.assertTrue(ok)
            review_rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            actual_panel = review_rgb[:, 1280:1920]
            self.assertLess(
                float(
                    np.abs(
                        actual_panel[-480:].astype(np.int16)
                        - candidate.astype(np.int16)
                    ).mean()
                ),
                12.0,
            )

            original_write_receipt = application._write_receipt
            original_publish = application.publish_candidate

            def write_then_interrupt(*args, **kwargs):
                original_write_receipt(*args, **kwargs)
                raise KeyboardInterrupt

            def publish_then_interrupt(*args, **kwargs):
                original_publish(*args, **kwargs)
                raise OSError("simulated interruption after durable publish")

            with mock.patch.object(
                application,
                "read_foreground_decision",
                side_effect=KeyboardInterrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    decide(approved_run, _paths=fixture.paths)
            self.assertTrue(candidate_path.is_dir())
            self.assertEqual(
                status(approved_run, _paths=fixture.paths)["status"], "REVIEW_READY"
            )
            self.assertNotIn("failure", load_events(run))

            prompt_entered = threading.Event()
            release_prompt = threading.Event()
            second_started = threading.Event()
            second_done = threading.Event()
            prompt_count: list[int] = []
            results: list[dict] = []
            errors: list[BaseException] = []

            def blocking_approve(_video_path):
                prompt_count.append(1)
                prompt_entered.set()
                if not release_prompt.wait(timeout=5):
                    raise TimeoutError("approval prompt was not released")
                return "APPROVE"

            def decision_worker(*, second: bool = False):
                if second:
                    second_started.set()
                try:
                    results.append(decide(approved_run, _paths=fixture.paths))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if second:
                        second_done.set()

            with (
                mock.patch.object(
                    application,
                    "read_foreground_decision",
                    side_effect=blocking_approve,
                ),
                mock.patch.object(
                    application,
                    "_write_receipt",
                    side_effect=write_then_interrupt,
                ),
                mock.patch.object(
                    application,
                    "publish_candidate",
                    side_effect=publish_then_interrupt,
                ),
            ):
                first = threading.Thread(target=decision_worker, daemon=True)
                second = threading.Thread(
                    target=decision_worker,
                    kwargs={"second": True},
                    daemon=True,
                )
                first.start()
                self.assertTrue(prompt_entered.wait(timeout=2))
                second.start()
                self.assertTrue(second_started.wait(timeout=1))
                self.assertFalse(second_done.wait(timeout=0.1))
                self.assertEqual(len(prompt_count), 1)
                release_prompt.set()
                first.join(timeout=10)
                second.join(timeout=10)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())

            self.assertEqual(len(prompt_count), 1)
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], CuratorError)
            self.assertEqual(errors[0].code, "OUTPUT_COMMITTED_RECEIPT_PENDING")
            approved = results[0]
            output = Path(approved["output"])
            self.assertEqual(approved["status"], "PUBLISHED")
            self.assertTrue(output.is_dir())
            self.assertFalse((output / "meta/training_approved.json").exists())
            self.assertEqual(
                status(approved_run, _paths=fixture.paths)["status"], "PUBLISHED"
            )
            approved_events = load_events(fixture.paths.run_root / approved_run)
            self.assertEqual(
                approved_events["failure"]["payload"]["state"],
                "PUBLISHED_RECEIPT_PENDING",
            )
            for event_name in ("decision", "failure", "receipt"):
                self.assertEqual(len(list(run.glob(f"{event_name}.json"))), 1)
            receipt = approved_events["receipt"]["payload"]
            self.assertIs(receipt["training_authority"], False)
            self.assertIs(receipt["approval_inherited"], False)
            actor = approved_events["decision"]["payload"]["actor"]
            self.assertEqual(actor["uid"], os.getuid())
            self.assertIs(actor["human_identity_authenticated"], False)

            rejected_events = load_events(fixture.paths.run_root / rejected_run)
            rejected_candidate = Path(
                rejected_events["request"]["payload"]["candidate_path"]
            )
            with (
                mock.patch.object(
                    application,
                    "read_foreground_decision",
                    return_value="REJECT",
                ),
                mock.patch.object(
                    dataset_publish,
                    "fsync_directory",
                    side_effect=CuratorError("DIRECTORY_FSYNC", "simulated"),
                ),
            ):
                with self.assertRaisesRegex(CuratorError, "DIRECTORY_FSYNC"):
                    decide(rejected_run, _paths=fixture.paths)
            self.assertFalse(rejected_candidate.exists())
            self.assertTrue(output.is_dir())
            self.assertEqual(
                status(rejected_run, _paths=fixture.paths)["status"],
                "REJECTED_RECEIPT_PENDING",
            )
            archived_source = root / "archived-source"
            source.rename(archived_source)
            fixture.profile_path.unlink()
            fixture.policy_path.unlink()
            with (
                mock.patch.object(
                    application,
                    "read_foreground_decision",
                    side_effect=AssertionError("recovery must not reprompt"),
                ),
                mock.patch.object(
                    application,
                    "_write_receipt",
                    side_effect=OSError("simulated interruption before receipt commit"),
                ),
            ):
                with self.assertRaisesRegex(CuratorError, "REJECTED_RECEIPT_PENDING"):
                    decide(rejected_run, _paths=fixture.paths)
            with mock.patch.object(
                application,
                "read_foreground_decision",
                side_effect=AssertionError("recovery must not reprompt"),
            ):
                rejected = decide(rejected_run, _paths=fixture.paths)
            self.assertEqual(rejected["status"], "REJECTED")
            self.assertEqual(
                status(rejected_run, _paths=fixture.paths)["status"], "REJECTED"
            )
            self.assertTrue(output.is_dir())


if __name__ == "__main__":
    unittest.main()
