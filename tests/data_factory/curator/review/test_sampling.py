from __future__ import annotations

import unittest

import numpy as np

from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.review.sampling import (
    SIGNAL_NAMES,
    ReviewSignalCollector,
    sample_frames,
    signal_for_reason,
)


class SamplingTest(unittest.TestCase):
    def _rows(self):
        mask = np.zeros((12, 16), dtype=bool)
        mask[2:10, 3:13] = True
        collector = ReviewSignalCollector(mask)
        for index in range(8):
            episode = index // 4
            frame = index % 4
            raw = np.full((12, 16, 3), index * 20, dtype=np.uint8)
            candidate = raw.copy()
            source = {
                "episode_index": np.asarray(episode),
                "frame_index": np.asarray(frame),
                "task": f"task-{episode}",
                "timestamp": np.asarray(episode * 10 + frame / 30),
                "action": np.asarray(
                    [index, 0, 0, 0, 0, 0, index % 2], dtype=np.float32
                ),
                "observation.state": np.asarray(
                    [0, index, 0, 0, 0, 0, (index + 1) % 2], dtype=np.float32
                ),
            }
            collector.observe(
                dataset_index=index,
                source_row=source,
                candidate_row=source,
                raw_up=raw,
                candidate_up=candidate,
            )
        return collector.finish()

    def test_streaming_signals_are_deterministic_bounded_and_cover_tasks(self):
        rows = self._rows()
        arguments = dict(
            seed=7,
            max_clips=4,
            clip_frames=2,
            fps=10,
            max_duration_seconds=1.0,
            relative_time_quantiles=[0.1, 0.5, 0.9],
        )
        self.assertEqual(
            sample_frames(rows, **arguments), sample_frames(rows, **arguments)
        )
        clips, coverage = sample_frames(rows, **arguments)
        self.assertEqual(coverage["covered_episodes"], [0, 1])
        self.assertLessEqual(coverage["rendered_frames"], 10)
        self.assertLessEqual(
            coverage["unique_selected_frames"],
            coverage["rendered_frames"],
        )
        actual_signals = sorted(
            {
                signal
                for clip in clips
                for reason in clip["reasons"]
                if (signal := signal_for_reason(reason)) is not None
            }
        )
        self.assertEqual(coverage["signals"], actual_signals)
        self.assertTrue(set(actual_signals).issubset(SIGNAL_NAMES))
        self.assertTrue(any(row["mask_boundary_motion"] > 0 for row in rows))

        tight_clips, tight_coverage = sample_frames(
            rows,
            seed=7,
            max_clips=2,
            clip_frames=2,
            fps=10,
            max_duration_seconds=1.0,
            relative_time_quantiles=[0.1, 0.5, 0.9],
        )
        self.assertEqual(len(tight_clips), 2)
        self.assertNotEqual(set(tight_coverage["signals"]), set(SIGNAL_NAMES))

    def test_zero_effective_clip_budget_fails(self):
        with self.assertRaisesRegex(CuratorError, "REVIEW_BUDGET_EMPTY"):
            sample_frames(
                self._rows(),
                seed=1,
                max_clips=1,
                clip_frames=30,
                fps=10,
                max_duration_seconds=1.0,
            )

    def test_episode_count_does_not_make_bounded_review_a_bottleneck(self):
        rows = []
        for episode in range(30):
            for frame in range(2):
                index = len(rows)
                rows.append(
                    {
                        "dataset_index": index,
                        "episode_index": episode,
                        "frame_index": frame,
                        "task": "same-task",
                        "timestamp": episode * 10 + frame / 30,
                        "relative_time": float(frame),
                        "gripper_transition": float(index % 3),
                        "arm_state_transition": float(index % 5),
                        "arm_action_transition": float(index % 7),
                        "visual_motion": float(index % 11),
                        "mask_boundary_motion": float(index % 13),
                        "brightness": float(index),
                        "sharpness": float(100 - index),
                    }
                )
        clips, coverage = sample_frames(
            rows,
            seed=7,
            max_clips=4,
            clip_frames=2,
            fps=10,
            max_duration_seconds=1.0,
        )
        self.assertLessEqual(len(clips), 4)
        self.assertEqual(coverage["episodes"], list(range(30)))
        self.assertEqual(coverage["covered_tasks"], ["same-task"])
        self.assertLess(len(coverage["covered_episodes"]), 30)

        for row in rows:
            row["task"] = f"task-{row['episode_index']:03d}"
        many_tasks_clips, many_tasks_coverage = sample_frames(
            rows,
            seed=7,
            max_clips=4,
            clip_frames=2,
            fps=10,
            max_duration_seconds=1.0,
        )
        self.assertEqual(len(many_tasks_clips), 4)
        self.assertEqual(len(many_tasks_coverage["tasks"]), 30)
        self.assertEqual(len(many_tasks_coverage["covered_tasks"]), 4)


if __name__ == "__main__":
    unittest.main()
