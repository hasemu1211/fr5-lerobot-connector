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
            original = setup_workflow._new_directory
            calls = 0

            def fail_between_creates(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise CuratorError("INJECTED_EXPORT_CREATE")
                return original(*args, **kwargs)

            with (
                mock.patch.object(
                    setup_workflow,
                    "_new_directory",
                    side_effect=fail_between_creates,
                ),
                self.assertRaisesRegex(CuratorError, "INJECTED_EXPORT_CREATE"),
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
            annotation["shapes"][1]["points"][0][1] += 1
            write_json(annotation_path, annotation)
            original = setup_workflow._new_directory
            calls = 0

            def fail_between_creates(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise CuratorError("INJECTED_PREVIEW_CREATE")
                return original(*args, **kwargs)

            with (
                mock.patch.object(
                    setup_workflow,
                    "_new_directory",
                    side_effect=fail_between_creates,
                ),
                self.assertRaisesRegex(CuratorError, "INJECTED_PREVIEW_CREATE"),
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
            self.assertTrue(old_review.is_dir())
            self.assertTrue(old_revision.is_dir())

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
