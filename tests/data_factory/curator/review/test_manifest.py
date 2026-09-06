from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from tests.data_factory.curator.support import make_profile_fixture
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.review.manifest import create_manifest, verify_manifest
from tools.data_factory.curator.review.render import (
    REVIEW_HEADER_HEIGHT,
    ReviewFrame,
    render_review_mp4,
)
from tools.data_factory.curator.review.sampling import sample_frames
from tools.data_factory.curator.workflow.application import _selected_review_frames


class ManifestTest(unittest.TestCase):
    def test_normal_review_cannot_skip_video_by_passing_none(self):
        with self.assertRaisesRegex(CuratorError, "REVIEW_VIDEO_PATH"):
            verify_manifest(Path("not-read.json"), None)

    def test_short_boundary_event_reaches_video_and_manifest_under_fixed_budget(self):
        # Two ordinary episodes compete with one short boundary-motion event.
        # The former new-frame-first ranking omitted the event for length=12.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = make_profile_fixture(root, width=64, height=48)
            for length in (12, 24, 60):
                rows = []
                dataset = []
                for episode, count in enumerate((length, length, 2)):
                    for frame in range(count):
                        rows.append(
                            {
                                "dataset_index": len(rows),
                                "episode_index": episode,
                                "frame_index": frame,
                                "task": "same-task",
                                "timestamp": frame / 30,
                                "relative_time": frame / (count - 1),
                                "gripper_transition": 0.0,
                                "arm_state_transition": 0.0,
                                "arm_action_transition": 0.0,
                                "visual_motion": 0.0,
                                "mask_boundary_motion": (
                                    200.0 if episode == 2 and frame == 1 else 0.0
                                ),
                                "brightness": 100.0,
                                "sharpness": 100.0,
                            }
                        )
                        dataset.append(
                            {
                                "episode_index": episode,
                                "frame_index": frame,
                                "task": "same-task",
                                "timestamp": frame / 30,
                                "observation.images.up": np.full(
                                    (48, 64, 3), 200 if episode == 2 else 80,
                                    dtype=np.uint8,
                                ),
                            }
                        )
                original_rows = copy.deepcopy(rows)
                policy = dict(
                    seed=7, max_clips=3, clip_frames=4, fps=10,
                    max_duration_seconds=1.2,
                )
                clips, coverage = sample_frames(rows, **policy)
                self.assertEqual((clips, coverage), sample_frames(rows, **policy))
                self.assertEqual(rows, original_rows)
                self.assertIn("mask_boundary_motion", coverage["signals"])
                event_clip = next(
                    c for c in clips if "mask_boundary_motion:max" in c["reasons"]
                )
                self.assertIn(len(rows) - 1, event_clip["dataset_indices"])
                self.assertLessEqual(coverage["rendered_frames"], 12)
                self.assertEqual(coverage["population_frames"], 2 * length + 2)

                frames = list(
                    _selected_review_frames(dataset, dataset, clips, width=64, height=48)
                )
                self.assertTrue(
                    any(
                        frame.episode_index == 2 and frame.frame_index == 1
                        and np.all(frame.raw_up == 200) for frame in frames
                    )
                )
                video_path = root / f"review-{length}.mp4"
                video = render_review_mp4(
                    frames, video_path, keep_mask=fixture.mask,
                    geometry=fixture.resolved.geometry, width=64, height=48,
                    fps=10, expected_frames=len(frames),
                )
                # Decode the artifact the human would watch, including the event pixels.
                capture = cv2.VideoCapture(str(video_path))
                event_frames = 0
                try:
                    while True:
                        ok, decoded = capture.read()
                        if not ok:
                            break
                        event_frames += decoded[REVIEW_HEADER_HEIGHT:, :64].mean() > 180
                finally:
                    capture.release()
                self.assertEqual(event_frames, 2)
                identities = {
                    key: "sha256:" + str(index) * 64
                    for index, key in enumerate(
                        (
                            "source_tree_digest", "candidate_tree_digest",
                            "profile_digest", "profile_file_sha256",
                            "policy_digest", "policy_file_sha256",
                            "request_event_digest", "candidate_ready_event_digest",
                        )
                    )
                }
                manifest_path = root / f"manifest-{length}.json"
                created = create_manifest(
                    manifest_path, clips=clips, coverage=coverage,
                    identities=identities, video_path=video_path, video=video, fps=10,
                )
                self.assertEqual(verify_manifest(manifest_path, video_path), created)

                # Fresh evidence moves the extremum; a previous selection is not reused.
                rows[-1]["mask_boundary_motion"] = 0.0
                rows[length + 1]["mask_boundary_motion"] = 300.0
                changed, _ = sample_frames(
                    rows,
                    **{**policy, "max_clips": 5, "max_duration_seconds": 2.0},
                )
                changed_event = next(
                    c for c in changed if "mask_boundary_motion:max" in c["reasons"]
                )
                self.assertIn(length + 1, changed_event["dataset_indices"])
                self.assertNotEqual(changed, clips)

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
