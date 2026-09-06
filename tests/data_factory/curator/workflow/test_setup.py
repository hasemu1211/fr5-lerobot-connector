from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.data_factory.curator.support import (
    make_profile_fixture,
    make_source_dataset,
    write_json,
)
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.core.identity import stable_tree_identity
from tools.data_factory.curator.core.jsonio import canonical_digest
from tools.data_factory.curator.profile.schema import load_view_profile
from tools.data_factory.curator.profile.registry import resolve_view_profile
from tools.data_factory.curator.workflow import setup as setup_workflow
from tools.data_factory.curator.workflow.setup import (
    ProfileSetupPaths,
    evenly_spaced_indices,
    export_profile_setup,
    finalize_profile_setup,
    preview_profile_setup,
    setup_paths,
)


def _fixture(root: Path):
    source = make_source_dataset(
        root / "source-fixture", episodes=1, frames_per_episode=2
    )
    fixture = make_profile_fixture(root / "profile-fixture", verified=False)
    paths = ProfileSetupPaths(
        repository=root,
        run_root=root / "outputs/curator/setup",
        asset_root=root / "datasets/fr5_curator_assets/up-view",
        profile_root=root / "config/data_factory/curator/view_profiles",
        collection_profile=fixture.collection_path,
        layout_manifest=fixture.layout_path,
        physical_region_binding=fixture.binding_path,
    )
    return source, fixture, paths


class ProfileSetupTest(unittest.TestCase):
    def test_native_train_fit_survives_preview_and_finalization_without_heldout_pixels(self):
        from tools.data_factory.training_approval import current_dataset_identity, current_episode_digest
        from tools.data_factory.training_split import compile_launch_split
        from tools.fr5_training_profile import read_metadata, launch_feature_contract
        from tools.data_factory.curator.workflow.application import prepare, review_candidate
        from tools.data_factory.curator.workflow.state import load_events

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_source_dataset(root / "source-fixture", episodes=6, frames_per_episode=2)
            fixture = make_profile_fixture(root / "profile-fixture", verified=False)
            paths = ProfileSetupPaths(
                repository=root, run_root=root / "outputs/setup", asset_root=root / "assets",
                profile_root=root / "published-profiles", collection_profile=fixture.collection_path,
                layout_manifest=fixture.layout_path, physical_region_binding=fixture.binding_path,
            )
            before = stable_tree_identity(source, code="TEST_SOURCE")
            identity = current_dataset_identity(source, repo_id="local/synthetic-source", dataset_id="synthetic-fit")
            metadata = read_metadata(source)
            selected = [0, 2, 3, 5]
            # Synthetic inventory fields only; no approval is issued or model run.
            split = compile_launch_split(
                inventory={"dataset_identity": identity, "inventory_digest": "sha256:" + "1" * 64,
                           "episodes": [{"episode_index": i, "episode_content_digest": current_episode_digest(identity, i)} for i in selected]},
                metadata=metadata, selected=selected, fraction=.2,
                feature_contract=launch_feature_contract("smolvla", "fr5-up-wrist-rgb-30hz-v2", "pickup_e2e", metadata),
            )
            self.assertEqual(split["train_episodes"], [0, 3])
            self.assertEqual(split["eval_episodes"], [2, 5])
            split_path = root / "native-split.json"
            write_json(split_path, split)
            split_bytes = split_path.read_bytes()
            with self.assertRaisesRegex(CuratorError, "FIT_SPLIT_PATH"):
                export_profile_setup(source, fit_split=root / "missing-split.json", _paths=paths)
            self.assertFalse(paths.run_root.exists())
            with self.assertRaisesRegex(CuratorError, "SETUP_FIT_REFERENCE"):
                export_profile_setup(source, fit_split=split_path, reference_frame_index=4, _paths=paths)
            self.assertFalse(paths.run_root.exists())
            with self.assertRaisesRegex(CuratorError, "SETUP_FIT_SOURCE"):
                changed = {**split, "dataset_identity": {**identity, "dataset_digest": "sha256:" + "9" * 64}}
                changed["split_digest"] = canonical_digest({k: v for k, v in changed.items() if k != "split_digest"})
                write_json(root / "wrong-source-split.json", changed)
                export_profile_setup(source, fit_split=root / "wrong-source-split.json", _paths=paths)
            self.assertFalse(paths.run_root.exists())

            exported = export_profile_setup(source, fit_split=split_path, reference_frame_index=7, plate_frame_count=3,
                profile_id="train-fitted-view", _paths=paths, _setup_id_value="fit-setup")
            request_path = paths.run_root / "fit-setup/request.json"
            request = json.loads(request_path.read_bytes())
            self.assertEqual(request["schema_version"], "curator.profile_setup_request.v2")
            self.assertEqual(request["background_plate_frame_indices"], [0, 6, 7])
            annotation = json.loads(fixture.annotation_path.read_bytes())
            annotation["imagePath"] = Path(exported["reference_image"]).name
            write_json(Path(exported["labelme_annotation"]), annotation)
            split_path.write_bytes(split_bytes + b"\n")
            with self.assertRaisesRegex(CuratorError, "FIT_SPLIT_CHANGED"):
                preview_profile_setup("fit-setup", _paths=paths)
            self.assertFalse((paths.run_root / "fit-setup/previews").exists())
            split_path.write_bytes(split_bytes)
            preview = preview_profile_setup("fit-setup", _paths=paths)
            draft_path = Path(preview["review_video"]).with_name("view-profile-draft.json")
            draft = load_view_profile(draft_path)
            fit = draft.value["fitting"]
            self.assertEqual(fit["training_split"]["split_digest"], split["split_digest"])
            self.assertEqual([row["episode_index"] for row in fit["background_plate_frames"]], [0, 3, 3])
            self.assertEqual([row["frame_index"] for row in fit["background_plate_frames"]], [0, 0, 1])
            self.assertEqual(fit["reference_frame"]["episode_index"], 3)
            self.assertEqual(fit["reference_frame"]["frame_index"], 1)
            self.assertTrue(all(row["rgb_sha256"].startswith("sha256:") for row in fit["background_plate_frames"]))
            altered = json.loads(draft_path.read_bytes())
            altered["fitting"]["reference_frame"]["episode_index"] = 5
            write_json(root / "heldout-reference-profile.json", altered)
            with self.assertRaisesRegex(CuratorError, "PROFILE_FITTING_FRAME"):
                load_view_profile(root / "heldout-reference-profile.json")
            with self.assertRaisesRegex(CuratorError, "SETUP_PHYSICAL_BINDING_NOT_VERIFIED"):
                finalize_profile_setup("fit-setup", preview["preview_id"], _paths=paths)
            # Qualify only this synthetic fixture to exercise final profile publication.
            binding = json.loads(fixture.binding_path.read_bytes())
            binding.update(physical_binding_status="VERIFIED", verified_at="2026-09-03T00:00:00Z",
                           verified_by="synthetic-operator", evidence_digest="sha256:" + "d" * 64)
            binding["binding_digest"] = canonical_digest({k: v for k, v in binding.items() if k != "binding_digest"})
            write_json(fixture.binding_path, binding)
            finalized = finalize_profile_setup("fit-setup", preview["preview_id"], _paths=paths)
            resolved = resolve_view_profile(paths.profile_root, "train-fitted-view",
                binding_root=fixture.binding_path.parent, collection_profile_root=fixture.collection_path.parent)
            self.assertEqual(resolved.profile["schema_version"], "curator.resolved_view_profile.v2")
            self.assertEqual(resolved.profile["fitting"], fit)
            self.assertEqual(resolved.profile["profile_digest"], finalized["profile_digest"])
            consumer_paths = replace(fixture.paths, profile_root=paths.profile_root)
            prepared = prepare(source, _paths=consumer_paths, _run_id_value="train-fit-consumer")
            shown = review_candidate(prepared["run_id"], _paths=consumer_paths)
            self.assertEqual(shown["status"], "REVIEW_READY")
            self.assertFalse(shown["training_authority"])
            events = load_events(consumer_paths.run_root / prepared["run_id"])
            candidate = Path(events["request"]["payload"]["candidate_path"])
            lineage = json.loads((candidate / "meta/curator_lineage.json").read_bytes())
            self.assertEqual(lineage["transform"]["profile_digest"], resolved.profile["profile_digest"])
            self.assertFalse(lineage["approval_inherited"])
            self.assertFalse(Path(events["request"]["payload"]["output_path"]).exists())
            self.assertEqual(stable_tree_identity(source, code="TEST_SOURCE"), before)
            self.assertEqual(split_path.read_bytes(), split_bytes)
            # A different native selection excludes episode 0; no implicit frame 0 fit.
            alternate_selected = [1, 2, 3, 4, 5]
            alternate = compile_launch_split(
                inventory={"dataset_identity": identity, "inventory_digest": "sha256:" + "2" * 64,
                           "episodes": [{"episode_index": i, "episode_content_digest": current_episode_digest(identity, i)} for i in alternate_selected]},
                metadata=metadata, selected=alternate_selected, fraction=.2,
                feature_contract=split["feature_contract"],
            )
            write_json(root / "alternate-split.json", alternate)
            export_profile_setup(source, fit_split=root / "alternate-split.json", profile_id="alternate-fit",
                                 _paths=paths, _setup_id_value="alternate-setup")
            alternate_request = json.loads((paths.run_root / "alternate-setup/request.json").read_bytes())
            self.assertEqual(alternate_request["reference_frame_index"], 2)
            self.assertEqual(alternate_request["background_plate_frame_indices"], [2, 3, 4, 5, 6, 7])
            self.assertEqual(stable_tree_identity(source, code="TEST_SOURCE"), before)
            split_path.write_bytes(split_bytes + b"\n")
            with self.assertRaisesRegex(CuratorError, "FIT_SPLIT_CHANGED"):
                load_view_profile(paths.profile_root / "train-fitted-view.json")
            with self.assertRaisesRegex(CuratorError, "FIT_SPLIT_CHANGED"):
                review_candidate(prepared["run_id"], _paths=consumer_paths)
            self.assertEqual(load_events(consumer_paths.run_root / prepared["run_id"]), events)

    def test_indices_are_bounded_deterministic_and_cover_endpoints(self):
        self.assertEqual(evenly_spaced_indices(1, 31), [0])
        self.assertEqual(evenly_spaced_indices(5, 3), [0, 2, 4])
        self.assertEqual(len(evenly_spaced_indices(1000, 31)), 31)
        self.assertEqual(evenly_spaced_indices(1000, 31)[-1], 999)

    def test_default_paths_bind_current_main_collection_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_paths(Path(temporary))
        self.assertEqual(
            paths.collection_profile.name,
            "fr5-up-wrist-rgb-30hz-v2.json",
        )

    def test_new_default_coexists_with_preserved_r002_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _fixture_value, paths = _fixture(root)
            preserved = paths.asset_root / "fr5-up-wrist-fixed-view-r002"
            write_json(preserved / "reference.json", {"historical": "r002"})
            before = stable_tree_identity(preserved, code="TEST_R002")

            exported = export_profile_setup(
                source,
                plate_frame_count=2,
                _paths=paths,
                _setup_id_value="setup-r003",
            )

            self.assertEqual(
                Path(exported["reference_image"]).parent.name,
                "fr5-up-wrist-fixed-view-r003",
            )
            request = json.loads(
                (paths.run_root / "setup-r003/request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(request["profile_id"], "fr5-up-wrist-fixed-view-r003")
            self.assertEqual(stable_tree_identity(preserved, code="TEST_R002"), before)

    def test_export_failures_remove_owned_state_and_exact_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _fixture_value, paths = _fixture(root)
            preserved = export_profile_setup(
                source,
                profile_id="preserved-view-r001",
                plate_frame_count=2,
                _paths=paths,
                _setup_id_value="setup-preserved",
            )
            preserved_run = paths.run_root / "setup-preserved"
            preserved_asset = Path(preserved["reference_image"]).parent
            preserved_before = (
                stable_tree_identity(preserved_run, code="TEST_PRESERVED_EXPORT"),
                stable_tree_identity(preserved_asset, code="TEST_PRESERVED_EXPORT"),
            )
            original = setup_workflow._new_directory
            calls = 0

            def interrupt_after_create(*args, **kwargs):
                nonlocal calls
                calls += 1
                owned = original(*args, **kwargs)
                if calls == 2:
                    raise KeyboardInterrupt
                return owned

            with (
                mock.patch.object(
                    setup_workflow,
                    "_new_directory",
                    side_effect=interrupt_after_create,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                export_profile_setup(
                    source,
                    profile_id="retry-view-r001",
                    plate_frame_count=2,
                    _paths=paths,
                    _setup_id_value="setup-retry",
                )
            self.assertFalse((paths.run_root / "setup-retry").exists())
            self.assertFalse((paths.asset_root / "retry-view-r001").exists())
            self.assertEqual(
                (
                    stable_tree_identity(preserved_run, code="TEST_PRESERVED_EXPORT"),
                    stable_tree_identity(preserved_asset, code="TEST_PRESERVED_EXPORT"),
                ),
                preserved_before,
            )

            with (
                mock.patch.object(
                    setup_workflow,
                    "_write_rgb",
                    side_effect=KeyboardInterrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                export_profile_setup(
                    source,
                    profile_id="retry-view-r001",
                    plate_frame_count=2,
                    _paths=paths,
                    _setup_id_value="setup-retry",
                )
            self.assertFalse((paths.run_root / "setup-retry").exists())
            self.assertFalse((paths.asset_root / "retry-view-r001").exists())

            result = export_profile_setup(
                source,
                profile_id="retry-view-r001",
                plate_frame_count=2,
                _paths=paths,
                _setup_id_value="setup-retry",
            )
            self.assertEqual(result["status"], "ANNOTATION_REQUIRED")
            self.assertEqual(
                (
                    stable_tree_identity(preserved_run, code="TEST_PRESERVED_EXPORT"),
                    stable_tree_identity(preserved_asset, code="TEST_PRESERVED_EXPORT"),
                ),
                preserved_before,
            )

    def test_preview_failures_preserve_prior_evidence_and_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, fixture, paths = _fixture(root)
            exported = export_profile_setup(
                source,
                profile_id="preview-retry-r001",
                plate_frame_count=2,
                _paths=paths,
                _setup_id_value="setup-preview-retry",
            )
            annotation_path = Path(exported["labelme_annotation"])
            annotation = json.loads(fixture.annotation_path.read_text(encoding="utf-8"))
            annotation["imagePath"] = Path(exported["reference_image"]).name
            write_json(annotation_path, annotation)
            preview_profile_setup(
                "setup-preview-retry",
                _paths=paths,
                _preview_id_value="preview-old",
            )
            old_review = paths.run_root / "setup-preview-retry/previews/preview-old"
            old_revision = paths.asset_root / "preview-retry-r001/revisions/preview-old"
            old_before = (
                stable_tree_identity(old_review, code="TEST_PRESERVED_PREVIEW"),
                stable_tree_identity(old_revision, code="TEST_PRESERVED_PREVIEW"),
            )
            annotation["shapes"][1]["points"][0][1] += 1
            write_json(annotation_path, annotation)
            original = setup_workflow._new_directory
            calls = 0

            def interrupt_after_create(*args, **kwargs):
                nonlocal calls
                calls += 1
                owned = original(*args, **kwargs)
                if calls == 2:
                    raise KeyboardInterrupt
                return owned

            with (
                mock.patch.object(
                    setup_workflow,
                    "_new_directory",
                    side_effect=interrupt_after_create,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                preview_profile_setup(
                    "setup-preview-retry",
                    _paths=paths,
                    _preview_id_value="preview-new",
                )
            self.assertTrue(old_review.is_dir())
            self.assertTrue(old_revision.is_dir())
            self.assertFalse(
                (paths.run_root / "setup-preview-retry/previews/preview-new").exists()
            )
            self.assertFalse(
                (paths.asset_root / "preview-retry-r001/revisions/preview-new").exists()
            )
            self.assertEqual(
                (
                    stable_tree_identity(old_review, code="TEST_PRESERVED_PREVIEW"),
                    stable_tree_identity(old_revision, code="TEST_PRESERVED_PREVIEW"),
                ),
                old_before,
            )

            with (
                mock.patch.object(
                    setup_workflow,
                    "render_review_mp4",
                    side_effect=KeyboardInterrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                preview_profile_setup(
                    "setup-preview-retry",
                    _paths=paths,
                    _preview_id_value="preview-new",
                )
            self.assertTrue(old_review.is_dir())
            self.assertTrue(old_revision.is_dir())
            self.assertFalse(
                (paths.run_root / "setup-preview-retry/previews/preview-new").exists()
            )
            self.assertFalse(
                (paths.asset_root / "preview-retry-r001/revisions/preview-new").exists()
            )

            result = preview_profile_setup(
                "setup-preview-retry",
                _paths=paths,
                _preview_id_value="preview-new",
            )
            self.assertEqual(result["status"], "BOUNDARY_REVIEW_REQUIRED")
            self.assertEqual(
                (
                    stable_tree_identity(old_review, code="TEST_PRESERVED_PREVIEW"),
                    stable_tree_identity(old_revision, code="TEST_PRESERVED_PREVIEW"),
                ),
                old_before,
            )

    def test_old_request_pins_profile_through_preview_and_verified_finalize(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source_dataset(
                root / "source-fixture", episodes=1, frames_per_episode=2
            )
            source_before = stable_tree_identity(source, code="TEST_SOURCE")
            fixture = make_profile_fixture(root / "profile-fixture", verified=False)
            paths = ProfileSetupPaths(
                repository=root,
                run_root=root / "outputs/curator/setup",
                asset_root=root / "datasets/fr5_curator_assets/up-view",
                profile_root=root / "config/data_factory/curator/view_profiles",
                collection_profile=fixture.collection_path,
                layout_manifest=fixture.layout_path,
                physical_region_binding=fixture.binding_path,
            )
            exported = export_profile_setup(
                source,
                profile_id="production-view-r001",
                plate_frame_count=2,
                _paths=paths,
                _setup_id_value="setup-1",
            )
            request_path = paths.run_root / "setup-1/request.json"
            request_bytes = request_path.read_bytes()
            request = json.loads(request_bytes)
            pinned_collection = json.loads(
                fixture.collection_path.read_text(encoding="utf-8")
            )
            pinned_collection_path = fixture.collection_path.resolve(strict=True)
            pinned_collection_digest = canonical_digest(pinned_collection)
            self.assertEqual(
                request["collection_camera_profile"], str(pinned_collection_path)
            )
            self.assertEqual(
                request["collection_camera_profile_digest"], pinned_collection_digest
            )
            annotation = json.loads(fixture.annotation_path.read_text(encoding="utf-8"))
            annotation["imagePath"] = Path(exported["reference_image"]).name
            write_json(Path(exported["labelme_annotation"]), annotation)

            preview = preview_profile_setup("setup-1", _paths=paths)
            self.assertEqual(preview["status"], "BOUNDARY_REVIEW_REQUIRED")
            self.assertFalse(preview["candidate_authority"])
            self.assertFalse(preview["training_authority"])
            self.assertTrue(Path(preview["boundary_overlay"]).is_file())
            self.assertTrue(Path(preview["review_video"]).is_file())
            first_preview_id = preview["preview_id"]
            self.assertEqual(
                Path(preview["review_video"]).parent,
                paths.run_root / "setup-1/previews" / first_preview_id,
            )
            self.assertFalse(paths.profile_root.exists())
            self.assertEqual(
                stable_tree_identity(source, code="TEST_SOURCE"), source_before
            )

            current_collection = fixture.collection_path.with_name("current-v2.json")
            current_profile = {
                **pinned_collection,
                "collection_profile_id": "synthetic-up-wrist-r002",
                "writer_queue_size": 32,
            }
            write_json(current_collection, current_profile)
            current_collection_path = current_collection.resolve(strict=True)
            current_collection_digest = canonical_digest(current_profile)
            self.assertNotEqual(current_collection_path, pinned_collection_path)
            self.assertNotEqual(current_collection_digest, pinned_collection_digest)
            paths = replace(paths, collection_profile=current_collection)

            annotation["shapes"][1]["points"][0][1] += 1
            write_json(Path(exported["labelme_annotation"]), annotation)
            preview = preview_profile_setup("setup-1", _paths=paths)
            self.assertNotEqual(preview["preview_id"], first_preview_id)
            self.assertEqual(preview["removed_superseded_preview_ids"], [])
            self.assertTrue(
                (paths.run_root / "setup-1/previews" / first_preview_id).is_dir()
            )
            self.assertTrue(
                (
                    paths.asset_root
                    / "production-view-r001/revisions"
                    / first_preview_id
                ).is_dir()
            )
            self.assertEqual(
                sorted(
                    path.name
                    for path in (paths.run_root / "setup-1/previews").iterdir()
                ),
                sorted([first_preview_id, preview["preview_id"]]),
            )
            manifest = json.loads(
                (Path(preview["review_video"]).parent / "preview.json").read_text(
                    encoding="utf-8"
                )
            )
            draft = json.loads(
                Path(manifest["profile_draft"]).read_text(encoding="utf-8")
            )
            self.assertEqual(request_path.read_bytes(), request_bytes)
            self.assertEqual(
                draft["collection_camera_profile"], str(pinned_collection_path)
            )
            self.assertEqual(
                draft["collection_camera_profile_digest"], pinned_collection_digest
            )
            self.assertNotEqual(
                draft["collection_camera_profile"], str(current_collection_path)
            )
            self.assertNotEqual(
                draft["collection_camera_profile_digest"], current_collection_digest
            )
            roles = manifest["artifact_roles"]
            self.assertTrue(roles["review_only_outputs"]["forbidden_from_dataset"])
            self.assertTrue(
                all(
                    str(paths.asset_root) in path
                    for path in roles["training_transform_inputs"]["files"]
                )
            )
            self.assertTrue(
                all(
                    str(paths.run_root) in path
                    for path in roles["review_only_outputs"]["files"]
                )
            )
            with self.assertRaisesRegex(
                CuratorError, "SETUP_PHYSICAL_BINDING_NOT_VERIFIED"
            ):
                finalize_profile_setup("setup-1", preview["preview_id"], _paths=paths)

            binding = json.loads(fixture.binding_path.read_text(encoding="utf-8"))
            original_frame_id = binding["bindings"][0]["frame_id"]
            binding.update(
                {
                    "physical_binding_status": "VERIFIED",
                    "verified_at": "2026-09-03T00:00:00Z",
                    "verified_by": "test-operator",
                    "evidence_digest": "sha256:" + "d" * 64,
                }
            )
            binding["binding_digest"] = canonical_digest(
                {
                    key: value
                    for key, value in binding.items()
                    if key != "binding_digest"
                }
            )
            binding["bindings"][0]["frame_id"] = "different-assignment"
            binding["binding_digest"] = canonical_digest(
                {
                    key: value
                    for key, value in binding.items()
                    if key != "binding_digest"
                }
            )
            write_json(fixture.binding_path, binding)
            with self.assertRaisesRegex(
                CuratorError, "SETUP_PHYSICAL_BINDING_NOT_VERIFIED"
            ):
                finalize_profile_setup("setup-1", preview["preview_id"], _paths=paths)

            binding["bindings"][0]["frame_id"] = original_frame_id
            binding["binding_digest"] = canonical_digest(
                {
                    key: value
                    for key, value in binding.items()
                    if key != "binding_digest"
                }
            )
            write_json(fixture.binding_path, binding)
            finalized = finalize_profile_setup(
                "setup-1", preview["preview_id"], _paths=paths
            )
            self.assertEqual(finalized["status"], "PROFILE_FINALIZED")
            self.assertFalse(finalized["training_authority"])
            self.assertTrue(Path(finalized["profile_path"]).is_file())
            self.assertEqual(
                finalize_profile_setup("setup-1", preview["preview_id"], _paths=paths),
                finalized,
            )
            with self.assertRaisesRegex(CuratorError, "SETUP_FINALIZATION_STARTED"):
                preview_profile_setup("setup-1", _paths=paths)
            profile_text = Path(finalized["profile_path"]).read_text(encoding="utf-8")
            self.assertNotIn(str(paths.run_root), profile_text)
            self.assertNotIn("boundary-overlay", profile_text)
            self.assertIn(preview["preview_id"], profile_text)
            self.assertEqual(
                stable_tree_identity(source, code="TEST_SOURCE"), source_before
            )


if __name__ == "__main__":
    unittest.main()
