#!/usr/bin/env python3

import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import json
import sys
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from fr5_dataset_schema import CAMERA_PROFILES, dataset_features, smolvla_camera_mapping
from fr5_lerobot_recorder import FR5LeRobotRecorder, load_time_offset_profile, parse_args
from time_alignment import estimate_time_offset, interpolate_vector, latest_sample, nearest_sample
from ros_image import image_message_to_rgb
from validate_lerobot_dataset import has_nonfinite_number


class RecorderContractTest(unittest.TestCase):
    def test_vendor_gripper_call_is_nonblocking(self):
        source = (Path(__file__).resolve().parents[1] / "src/frcobot_ros2/fairino_hardware_v3_9_7/src/fairino_hardware_interface.cpp").read_text()
        self.assertRegex(source, r"MoveGripper\([\s\S]{0,300}_gripper_max_time,\s*1,")

    def test_ros_image_conversion_handles_padding_and_rejects_truncation(self):
        message = SimpleNamespace(
            encoding="bgr8", width=2, height=1, step=8,
            data=bytes([0, 0, 255, 0, 255, 0, 99, 99]),
        )
        np.testing.assert_array_equal(image_message_to_rgb(message)[0], [[255, 0, 0], [0, 255, 0]])
        message.data = b"\0"
        with self.assertRaises(ValueError):
            image_message_to_rgb(message)

    def test_buffered_alignment_uses_target_time(self):
        samples = [(1.00, np.array([0.0])), (1.04, np.array([1.0]))]
        value, before, after = interpolate_vector(samples, 1.02, 0.05)
        np.testing.assert_allclose(value, [0.5])
        self.assertEqual((before, after), (1.00, 1.04))
        self.assertEqual(latest_sample(samples, 1.03, 0.05)[0], 1.00)
        self.assertEqual(nearest_sample(samples, 1.03, 0.05)[0], 1.04)
        self.assertIsNone(nearest_sample(samples, 1.20, 0.05))

        robot_times = np.arange(0, 12, 0.005)
        robot_speed = sum(
            amplitude * np.exp(-0.5 * ((robot_times - center) / width) ** 2)
            for center, amplitude, width in ((1, .8, .06), (2.4, 1.2, .08), (4.3, .5, .05), (6.1, 1, .07), (8.8, .7, .06), (10.4, 1.3, .05))
        )
        image_times = np.arange(0.2, 11.8, 1 / 30)
        image_motion = np.interp(image_times + 0.037, robot_times, robot_speed)
        offset, correlation, margin, peak_width = estimate_time_offset(image_times, image_motion, robot_times, robot_speed)
        self.assertAlmostEqual(offset, 0.037, delta=0.002)
        self.assertGreater(correlation, 0.99)
        self.assertGreater(margin, 0.03)
        self.assertLess(peak_width, 0.04)
        for broken in (
            (image_times[:-1], image_motion, robot_times, robot_speed),
            (image_times, image_motion, robot_times[::-1], robot_speed),
            (image_times, np.r_[image_motion[:-1], np.nan], robot_times, robot_speed),
        ):
            with self.assertRaises(ValueError):
                estimate_time_offset(*broken)
        with self.assertRaises(ValueError):
            estimate_time_offset(image_times, image_motion, robot_times, robot_speed, 0.02)

    def test_time_offset_profile_is_bound_to_active_stream(self):
        measurement = {
            "accepted": True, "camera_role": "up", "image_topic": "/camera/up/color/image_raw",
            "image_width": 640, "image_height": 480, "method": "dense_optical_flow_farneback_v1",
            "search_max_offset_ms": 200.0, "offset_ms": 12.0,
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "offsets.json"
            path.write_text(json.dumps({"schema_version": 1, "offsets_ms": {"up": 12.0}, "measurements": {"up": measurement}}))
            self.assertEqual(load_time_offset_profile(path, {"up": measurement["image_topic"]}, 640, 480), {"up": 12.0})
            measurement["accepted"] = "yes"
            path.write_text(json.dumps({"schema_version": 1, "offsets_ms": {"up": 12.0}, "measurements": {"up": measurement}}))
            with self.assertRaises(SystemExit):
                load_time_offset_profile(path, {"up": "/camera/up/color/image_raw"}, 640, 480)

    def test_camera_profiles_match_features(self):
        for cameras in CAMERA_PROFILES.values():
            features = dataset_features(fps=30, height=480, width=640, cameras=cameras, use_videos=True)
            self.assertEqual(
                {key.removeprefix("observation.images.") for key in features if key.startswith("observation.images.")},
                set(cameras),
            )

        self.assertEqual(
            smolvla_camera_mapping(["observation.images.up"]),
            ({"observation.images.up": "observation.images.camera1"}, 2),
        )
        self.assertEqual(
            smolvla_camera_mapping(["observation.images.wrist", "observation.images.up"]),
            ({
                "observation.images.up": "observation.images.camera1",
                "observation.images.wrist": "observation.images.camera2",
            }, 1),
        )
        with self.assertRaises(ValueError):
            smolvla_camera_mapping([])

    def test_quality_gate_accepts_only_timed_rgb_motion(self):
        recorder = FR5LeRobotRecorder.__new__(FR5LeRobotRecorder)
        recorder.args = SimpleNamespace(
            fps=30, min_frames=60, fps_tolerance=0.05, max_frame_gap_factor=2.0,
            max_long_gap_ratio=0.01, max_pause=0.25,
            min_camera_source_fps_ratio=0.75, max_image_repeat_ratio=0.25,
            image_max_age=0.20, state_max_age=0.20, action_max_age=0.05,
            sync_slop=0.033, action_sync_slop=0.033, alignment_delay=0.20,
            min_arm_range=0.01, min_gripper_range=0.001, min_color_delta=1.0,
            allow_monochrome=False, min_brightness=20, max_brightness=235,
            max_clipping=0.20, min_sharpness=20,
        )
        recorder.dataset = SimpleNamespace(meta=SimpleNamespace(total_episodes=0))
        recorder.frames = 90
        recorder.frame_stamps = list(np.arange(90) / 30)
        recorder.sync_spans = [0.005] * 90
        recorder.action_ages = [0.010] * 90
        recorder.state_ages = [0.010] * 90
        recorder.camera_stamps = {"up": [((i - 1) if i % 6 == 5 else i) / 30 for i in range(90)]}
        recorder.image_ages = {"up": [0.010] * 90}
        recorder.image_transport_ages = {"up": [0.010] * 90}
        recorder.camera_names = ("up",)
        recorder.camera_offsets = {"up": 0.0}
        recorder.writer_queue_drops = 0
        recorder.stale_sample_skips = 0
        recorder.missing_action_skips = 0
        recorder.alignment_failures = 0
        recorder.alignment_failure_sources = {
            "state": 0, "arm_action": 0, "gripper_action": 0, "transport": 0, "image.up": 0,
        }
        recorder.writer_error = None
        recorder.image_metrics = {"up": [(10, 120, 0.01, 100)] * 3}
        recorder.action_samples = [np.array([0, 0, 0, 0, value, 0, value / 4]) for value in np.linspace(0, 0.02, 90)]
        recorder.state_samples = [sample.copy() for sample in recorder.action_samples]
        recorder.source_provenance = [{}] * 90
        recorder.enqueue_attempts = 90
        summary, reasons = recorder._quality_summary()
        self.assertEqual(reasons, [])
        self.assertGreater(summary["cameras"]["up"]["repeat_ratio"], 0.10)

        recorder.writer_queue_drops = 1
        _, reasons = recorder._quality_summary()
        self.assertTrue(any("writer queue" in reason for reason in reasons))
        recorder.writer_queue_drops = 0

        recorder.image_metrics = {"up": [(0, 120, 0.01, 100)] * 3}
        _, reasons = recorder._quality_summary()
        self.assertTrue(any("monochrome" in reason for reason in reasons))

        recorder.image_metrics = {"up": [(10, 120, 0.01, 100)] * 3}
        recorder.frame_stamps[45:] = [stamp + 0.14 for stamp in recorder.frame_stamps[45:]]
        _, reasons = recorder._quality_summary()
        self.assertTrue(any("long frame-gap ratio" in reason for reason in reasons))

        recorder.frame_stamps = list(np.arange(90) / 30)
        recorder.action_samples[0][0] = np.nan
        summary, reasons = recorder._quality_summary()
        self.assertTrue(any("NaN/Inf" in reason for reason in reasons))
        json.dumps(summary, allow_nan=False)

        recorder.action_samples[0][0] = 0
        recorder.state_samples = [np.zeros(7) for _ in recorder.action_samples]
        _, reasons = recorder._quality_summary()
        self.assertTrue(any("feedback range" in reason for reason in reasons))

    def test_nonfinite_quality_option_is_rejected(self):
        with patch.object(sys, "argv", ["recorder", "--task", "test", "--fps-tolerance", "nan"]):
            with self.assertRaises(SystemExit):
                parse_args()
        self.assertTrue(has_nonfinite_number({"nested": [1, float("nan")]}))
        self.assertFalse(has_nonfinite_number({"nested": [1, 2.0], "accepted": True}))

if __name__ == "__main__":
    unittest.main()
