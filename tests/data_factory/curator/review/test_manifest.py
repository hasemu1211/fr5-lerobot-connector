from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import numpy as np

from tests.data_factory.curator.support import make_profile_fixture
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.review.manifest import create_manifest, verify_manifest
from tools.data_factory.curator.review.render import ReviewFrame, render_review_mp4


class ManifestTest(unittest.TestCase):
    def test_manifest_closes_video_identity_and_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = make_profile_fixture(root, width=64, height=48)
            video_path = root / "review.mp4"
            video = render_review_mp4(
                [
                    ReviewFrame(
                        np.zeros((48, 64, 3), dtype=np.uint8),
                        np.ones((48, 64, 3), dtype=np.uint8),
                        "clip-000",
                        0,
                        0,
                        0.0,
                        ("seeded_uniform",),
                    )
                ],
                video_path,
                keep_mask=fixture.mask,
                geometry=fixture.resolved.geometry,
                width=64,
                height=48,
                fps=10,
                expected_frames=1,
            )
            clips = [
                {
                    "clip_id": "clip-000",
                    "episode_index": 0,
                    "task": "task",
                    "anchor_dataset_index": 0,
                    "anchor_frame_index": 0,
                    "dataset_indices": [0],
                    "frame_indices": [0],
                    "reasons": ["seeded_uniform"],
                    "start_relative_seconds": 0.0,
                    "duration_seconds": 0.1,
                }
            ]
            coverage = {
                "population_frames": 1,
                "rendered_frames": 1,
                "unique_selected_frames": 1,
                "clip_count": 1,
                "episodes": [0],
                "tasks": ["task"],
                "covered_episodes": [0],
                "covered_tasks": ["task"],
                "signals": ["seeded_uniform"],
                "max_clips": 1,
                "max_duration_seconds": 1.0,
            }
            identities = {
                key: "sha256:" + str(index) * 64
                for index, key in enumerate(
                    (
                        "source_tree_digest",
                        "candidate_tree_digest",
                        "profile_digest",
                        "profile_file_sha256",
                        "policy_digest",
                        "policy_file_sha256",
                        "request_event_digest",
                        "candidate_ready_event_digest",
                    )
                )
            }
            manifest_path = root / "manifest.json"
            created = create_manifest(
                manifest_path,
                clips=clips,
                coverage=coverage,
                identities=identities,
                video_path=video_path,
                video=video,
                fps=10,
            )
            self.assertEqual(verify_manifest(manifest_path, video_path), created)
            video_path.chmod(0o600)
            with video_path.open("ab") as stream:
                stream.write(b"tamper")
            with self.assertRaisesRegex(CuratorError, "REVIEW_VIDEO_DIGEST"):
                verify_manifest(manifest_path, video_path)


if __name__ == "__main__":
    unittest.main()
