from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np

from tests.data_factory.curator.support import make_profile_fixture
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.review import render
from tools.data_factory.curator.review.render import (
    REVIEW_HEADER_HEIGHT,
    ReviewFrame,
    render_review_mp4,
    verify_review_video,
)


class RenderTest(unittest.TestCase):
    def test_real_h264_contains_raw_overlay_and_actual_candidate_panels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = make_profile_fixture(root, width=64, height=48)
            output = root / "review.mp4"
            frames = [
                ReviewFrame(
                    raw_up=np.full((48, 64, 3), index * 40, dtype=np.uint8),
                    candidate_up=np.full((48, 64, 3), 255 - index * 40, dtype=np.uint8),
                    clip_id="clip-000",
                    episode_index=0,
                    frame_index=index,
                    timestamp=index / 30,
                    reasons=("seeded_uniform",),
                )
                for index in range(4)
            ]
            video = render_review_mp4(
                frames,
                output,
                keep_mask=fixture.mask,
                geometry=fixture.resolved.geometry,
                width=64,
                height=48,
                fps=10,
                expected_frames=4,
            )
            self.assertEqual(
                video,
                {
                    "codec": "h264",
                    "width": 192,
                    "height": 48 + REVIEW_HEADER_HEIGHT,
                    "frames": 4,
                },
            )
            self.assertEqual(
                verify_review_video(
                    output,
                    width=64,
                    height=48 + REVIEW_HEADER_HEIGHT,
                    frames=4,
                    fps=10,
                )["codec"],
                "h264",
            )
            self.assertEqual(output.stat().st_mode & 0o777, 0o400)

    def test_streaming_encoder_watchdog_bounds_stdin_and_wait(self):
        class ImmediateTimer:
            def __init__(self, _interval, callback):
                self.callback = callback

            def start(self):
                self.callback()

            def cancel(self):
                pass

            def join(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = make_profile_fixture(root, width=64, height=48)
            output = root / "timed-out.mp4"
            frame = ReviewFrame(
                raw_up=np.zeros((48, 64, 3), dtype=np.uint8),
                candidate_up=np.zeros((48, 64, 3), dtype=np.uint8),
                clip_id="clip-000",
                episode_index=0,
                frame_index=0,
                timestamp=0.0,
                reasons=("seeded_uniform",),
            )
            with (
                mock.patch.object(render.threading, "Timer", ImmediateTimer),
                self.assertRaisesRegex(CuratorError, "REVIEW_FFMPEG_TIMEOUT"),
            ):
                render_review_mp4(
                    [frame],
                    output,
                    keep_mask=fixture.mask,
                    geometry=fixture.resolved.geometry,
                    width=64,
                    height=48,
                    fps=10,
                    expected_frames=1,
                )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
