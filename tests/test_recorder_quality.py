#!/usr/bin/env python3

import unittest
from collections import deque
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import json
import subprocess
import sys
import tarfile
import tempfile
import threading
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from fr5_dataset_schema import CAMERA_PROFILES, dataset_features, smolvla_camera_mapping
from fr5_lerobot_recorder import FR5LeRobotRecorder, parse_args
from time_alignment import interpolate_vector, latest_sample, nearest_sample
from ros_image import image_message_to_rgb
from measure_ros_topic_age import image_gate_failures
from validate_lerobot_dataset import (
    _video_frame_counts,
    has_nonfinite_number,
    transient_gripper_zero_dropouts,
)


class RecorderContractTest(unittest.TestCase):
    def test_robot_control_keeps_gripper_off_realtime_xmlrpc_path(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "src/frcobot_ros2/fairino_hardware_v3_9_7/src/fairino_hardware_interface.cpp").read_text()
        recorder_source = (root / "tools/fr5_lerobot_recorder.py").read_text()
        setup_source = (
            root / "tools/data_factory/operator/setup/physical.py"
        ).read_text()
        command_source = (root / "src/frcobot_ros2/fairino_hardware_v3_9_7/src/command_server.cpp").read_text()
        self.assertIn("JOINT_STATE_QOS_DEPTH = 20", recorder_source)
        self.assertIn("JointState, args.joint_states, self._on_joint_state, joint_state_qos", recorder_source)
        self.assertIn(
            '"name": "finger_right_joint", "position": 0.021 / 100 + 1e-6',
            setup_source,
        )
        write_body = source.split("FairinoHardwareInterface::write", 1)[1].split(
            "void FairinoHardwareInterface::gripper_worker", 1
        )[0]
        self.assertNotIn("MoveGripper", write_body)
        self.assertNotIn("SetRobotRealtimeStateConfig", source)
        self.assertIn("SetCmdRpyCallback(capture_udp_command_reply)", source)
        self.assertNotIn("ActGripper", source)
        activation_body = source.split("FairinoHardwareInterface::on_activate", 1)[1].split(
            "FairinoHardwareInterface::on_deactivate", 1
        )[0]
        self.assertLess(
            activation_body.index("GetRobotErrorCode"),
            activation_body.index("ServoMoveStart()"),
        )
        self.assertLess(
            activation_body.index("StopMotion()"),
            activation_body.index("ServoMoveEnd()"),
        )
        self.assertLess(
            activation_body.index("ServoMoveEnd()"),
            activation_body.index("ServoMoveStart()"),
        )
        self.assertNotIn("MotionQueueClear()", activation_body)
        self.assertNotIn("sleep_for(200ms)", activation_body)
        self.assertIn("sleep_for(250ms)", activation_body)
        self.assertIn("sleep_for(100ms)", activation_body)
        self.assertIn("send_udp_command_and_observe", activation_body)
        self.assertIn("std::chrono::milliseconds(50)", source)
        self.assertIn("return send_error == 0 && controller_error == 0;", source)
        self.assertIn("stale_pause_is_safe_to_clear", activation_body)
        self.assertIn("program_state == 1", activation_body)
        self.assertIn("mc_queue_len == 0", activation_body)
        self.assertIn("currentLuaFileName[0] == '\\0'", activation_body)
        self.assertIn("lastServoTarget[joint]", activation_body)
        self.assertIn("ResumeMotion()", activation_body)
        self.assertIn("activation_state.robot_state != 1", activation_body)
        self.assertIn("Servo transport qualification failed", activation_body)
        start_failure = activation_body.split("if (stop_result != 0", 1)[1].split(
            "if (_has_arm) {", 2
        )[0]
        self.assertIn("ServoMoveEnd(1)", start_failure)
        self.assertNotIn("ResetAllError", activation_body)
        read_body = source.split("FairinoHardwareInterface::read", 1)[1].split(
            "FairinoHardwareInterface::write", 1
        )[0]
        deactivate_body = source.split("FairinoHardwareInterface::on_deactivate", 1)[1].split(
            "FairinoHardwareInterface::read", 1
        )[0]
        self.assertNotIn("GetGripperCurPosition", read_body)
        self.assertLess(deactivate_body.index("stop_gripper_worker()"), deactivate_body.index("ServoMoveEnd(1)"))
        self.assertIn("_arm_stream_paused = true", write_body)
        self.assertIn("if (_arm_stream_paused.load()) return", write_body)
        self.assertNotIn("_restart_servo_after_gripper", source)
        self.assertLess(
            write_body.index("udp_command_error.load()"),
            write_body.index("_arm_stream_paused.load()"),
        )
        worker_body = source.split("void FairinoHardwareInterface::gripper_worker", 1)[1]
        self.assertIn("GetRobotRealTimeState", worker_body)
        self.assertNotIn("GetGripperCurPosition", worker_body)
        self.assertIn("feedback <= 100", worker_body)
        self.assertNotIn("GetRobotRealTimeState failed in non-realtime worker", worker_body)
        self.assertIn("_gripper_cv.wait(lock, [this]", worker_body)
        self.assertLess(worker_body.index("MoveGripper("), worker_body.index("ServoMoveStart(1)"))
        self.assertLess(worker_body.index("ServoMoveStart(1)"), worker_body.index("_arm_stream_paused = false"))
        self.assertIn("_gripper_command_generation.load() == command_generation", worker_body)
        self.assertIn(
            "if (feedback_is_plausible &&",
            worker_body,
        )
        self.assertIn("Holding last valid gripper feedback", worker_body)
        self.assertIn("Realtime gripper feedback recovered", worker_body)
        self.assertIn(
            "if (motion_done != 0 && feedback_is_plausible)", worker_body,
        )
        self.assertNotIn(
            "Completed gripper motion has implausible feedback", worker_body,
        )
        settle_branch = worker_body.split(
            "if (feedback_is_plausible && observed_movement &&", 1
        )[1].split("if (std::chrono::steady_clock::now() >= deadline)", 1)[0]
        self.assertIn("Gripper motion settled away from target", settle_branch)
        self.assertIn("resume_arm = true", settle_branch)
        self.assertNotIn("_gripper_error =", settle_branch)
        pending_supersession = worker_body.split(
            "_gripper_cv.wait_for(lock, 50ms", 1
        )[1].split("continue;", 1)[0]
        self.assertIn("_pending_gripper_position.has_value()", pending_supersession)
        self.assertIn("Superseding unsettled gripper command", pending_supersession)
        self.assertLess(
            pending_supersession.index("_pending_gripper_position.has_value()"),
            pending_supersession.index(
                "std::chrono::steady_clock::now() >= deadline"
            ),
        )
        self.assertRegex(write_body, r"ServoJ\([^;]+,\s*0,\s*1\)")
        self.assertRegex(
            source,
            r"void FairinoHardwareInterface::gripper_worker\(\)[\s\S]+MoveGripper\([\s\S]{0,300}_gripper_max_time,\s*1,",
        )
        self.assertNotIn("_ptr_robot->~FRRobot()", command_source)
        self.assertNotIn("_ptr_robot.reset()", source + command_source)
        self.assertIn("(void)_ptr_robot.release()", source)
        self.assertIn("(void)_ptr_robot.release()", command_source)
        rpc_failure = source.split("if(returncode != 0){", 1)[1].split("}else{", 1)[0]
        self.assertIn("_ptr_robot->CloseRPC()", rpc_failure)
        self.assertIn("(void)_ptr_robot.release()", rpc_failure)

    def test_moveit_and_ros2_control_action_contracts_are_preserved(self):
        root = Path(__file__).resolve().parents[1]
        controllers = (root / "src/fairino5_v6_moveit2_config/config/ros2_controllers.yaml").read_text()
        moveit = (root / "src/fairino5_v6_moveit2_config/config/moveit_controllers.yaml").read_text()
        hardware = (root / "src/fairino5_v6_moveit2_config/config/fairino5_v6_robot.ros2_control.xacro").read_text()
        srdf = (root / "src/fairino5_v6_moveit2_config/config/fairino5_v6_robot.srdf").read_text()
        preflight = (root / "scripts/preflight_collection.sh").read_text()
        for name in ("fairino5_controller", "gripper_controller"):
            self.assertIn(name, controllers)
            self.assertIn(name, moveit)
        self.assertIn("action_ns: follow_joint_trajectory", moveit)
        self.assertRegex(
            moveit,
            r"gripper_controller:[\s\S]+allowed_goal_duration_margin: 5\.0",
        )
        self.assertIn('<joint name="finger_right_joint">', hardware)
        self.assertIn('name="gripper_settle_time_ms"', hardware)
        self.assertRegex(srdf, r'<group_state name="open" group="gripper">\s*<joint name="finger_right_joint" value="0\.021"/>')
        self.assertRegex(srdf, r'<group_state name="closed" group="gripper">\s*<joint name="finger_right_joint" value="0"/>')
        self.assertIn("/fr_command_server opens a second FAIRINO SDK session", preflight)
        self.assertIn('up-side) CAMERA_TOPICS=("$UP_TOPIC" "/camera/side/color/image_raw")', preflight)
        self.assertIn("--expected-image-hz", preflight)
        self.assertIn("--max-image-age-ms 300", preflight)
        arm_controller = controllers.split("\nfairino5_controller:\n", 1)[1].split(
            "\ngripper_controller:\n", 1
        )[0]
        self.assertIn("goal_time: 5.0", arm_controller)
        for joint in ("j1", "j2", "j3", "j4", "j5", "j6"):
            self.assertRegex(arm_controller, rf"{joint}:\n\s+goal: 0\.01")
        self.assertIn("goal: 0.000105", controllers)

    def test_camera_preflight_rejects_bad_clock_domain_or_rate(self):
        stamps = list(np.arange(150) / 30)
        self.assertEqual(image_gate_failures(stamps, [0.05] * 150, 5, 30, 0.75, 300), [])
        discovery_delayed = list(np.arange(72) / 30)
        self.assertEqual(
            image_gate_failures(
                discovery_delayed, [0.05] * len(discovery_delayed),
                5, 30, 0.95, 300,
            ),
            [],
        )
        self.assertTrue(image_gate_failures(stamps, [-0.01] * 150, 5, 30, 0.75, 300))
        self.assertTrue(image_gate_failures(stamps[:50], [0.05] * 50, 5, 30, 0.75, 300))
        self.assertTrue(image_gate_failures(stamps, [0.4] * 150, 5, 30, 0.75, 300))

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
        recorder.episode_state = recorder.FROZEN
        recorder.alignment_tail_drained = True
        recorder.frames = 90
        recorder.frame_stamps = list(np.arange(90) / 30)
        recorder.sync_spans = [0.005] * 90
        recorder.action_ages = [0.010] * 90
        recorder.state_ages = [0.010] * 90
        recorder.camera_source_stamps = {"up": list(np.arange(90) / 30)}
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
        self.assertAlmostEqual(summary["cameras"]["up"]["source_fps"], 30.0)
        self.assertGreater(summary["cameras"]["up"]["repeat_ratio"], 0.10)

        recorder.camera_source_stamps = {"up": list(np.arange(60) / 20)}
        _, reasons = recorder._quality_summary()
        self.assertTrue(any("source fps 20.00 is too low" in reason for reason in reasons))
        recorder.camera_source_stamps = {"up": list(np.arange(90) / 30)}
        snapshot = recorder._quality_snapshot()
        recorder.frame_stamps.append(99.0)
        self.assertTrue(snapshot["accepted"])
        self.assertEqual(snapshot["frames"], 90)
        recorder.frame_stamps.pop()

        recorder.writer_queue_drops = 1
        _, reasons = recorder._quality_summary()
        self.assertTrue(any("writer queue" in reason for reason in reasons))
        recorder.writer_queue_drops = 0

        recorder.image_metrics = {"up": [(0, 250, 0.25, 10)] * 3}
        snapshot = recorder._quality_snapshot()
        self.assertTrue(snapshot["accepted"])
        self.assertEqual(snapshot["reasons"], [])
        self.assertEqual(len(snapshot["image_quality_warnings"]), 4)

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
        self.assertFalse(any("range" in reason for reason in reasons))

    def test_camera_source_cadence_tracks_callbacks_only_during_task_window(self):
        recorder = FR5LeRobotRecorder.__new__(FR5LeRobotRecorder)
        recorder.args = SimpleNamespace(height=2, width=3)
        recorder.lock = threading.Lock()
        recorder.camera_offsets = {"up": 0.0}
        recorder.camera_frames = {"up": deque(maxlen=3)}
        recorder.camera_source_stamps = {"up": []}
        recorder.recording = True
        recorder.episode_state = recorder.RECORDING
        recorder._stamp = lambda message: message.stamp
        recorder.get_clock = lambda: SimpleNamespace(
            now=lambda: SimpleNamespace(nanoseconds=2_000_000_000),
        )
        recorder.get_logger = lambda: SimpleNamespace(warning=lambda _message: None)
        message = SimpleNamespace(stamp=1.0, height=2, width=3)

        recorder._on_image("up", message)
        recorder.episode_state = recorder.FREEZING
        recorder._on_image("up", SimpleNamespace(stamp=1.1, height=2, width=3))

        self.assertEqual(recorder.camera_source_stamps["up"], [1.0])
        self.assertEqual(len(recorder.camera_frames["up"]), 2)

    def test_nonfinite_quality_option_is_rejected(self):
        with patch.object(sys, "argv", ["recorder", "--task", "test", "--fps-tolerance", "nan"]):
            with self.assertRaises(SystemExit):
                parse_args()
        self.assertTrue(has_nonfinite_number({"nested": [1, float("nan")]}))
        self.assertFalse(has_nonfinite_number({"nested": [1, 2.0], "accepted": True}))

    def test_transient_gripper_zero_feedback_is_rejected_without_deleting_rows(self):
        states = np.zeros((9, 7), dtype=np.float32)
        actions = np.zeros((9, 7), dtype=np.float32)
        states[:, 6] = [0.021, 0.021, 0.0, 0.0, 0.0119, 0.0118, 0.0, 0.0, 0.0]
        actions[:, 6] = [0.021, 0.01176, 0.01176, 0.01176, 0.01176, 0.0, 0.0, 0.0, 0.0]
        dropouts = transient_gripper_zero_dropouts(
            states,
            actions,
            np.zeros(9, dtype=np.int64),
            np.arange(9, dtype=np.int64),
        )
        self.assertEqual(dropouts, [{
            "episode_index": 0,
            "frame_start": 2,
            "frame_end": 3,
        }])

    def test_video_frame_counts_are_bounded_parallel_and_ordered(self):
        barrier = threading.Barrier(4)
        lock = threading.Lock()
        started = 0
        worker_names = set()

        def probe(path):
            nonlocal started
            with lock:
                started += 1
                should_wait = started <= 4
                worker_names.add(threading.current_thread().name)
            if should_wait:
                barrier.wait(timeout=2.0)
            return int(path.name), None

        paths = [Path(str(index)) for index in range(6)]
        with patch("validate_lerobot_dataset._video_frame_count", side_effect=probe):
            counts = _video_frame_counts(paths)
        self.assertEqual(counts, [(index, None) for index in range(6)])
        self.assertEqual(len(worker_names), 4)

    def test_collection_defaults_and_supported_camera_path(self):
        root = Path(__file__).resolve().parents[1]
        with patch.object(sys, "argv", ["recorder", "--task", "test"]):
            args = parse_args()
        self.assertEqual(args.fps, 30)
        self.assertEqual(args.writer_queue_size, 128)

        recorder_source = (root / "tools/fr5_lerobot_recorder.py").read_text()
        collect_source = (root / "scripts/collect.sh").read_text()
        validator_source = (root / "tools/validate_lerobot_dataset.py").read_text()
        camera_launcher = (root / "scripts/start_uvc_camera.sh").read_text()
        setup = (root / "scripts/setup_notebook.sh").read_text()
        usb_cam_timestamp = (root / "src/usb_cam/include/usb_cam/utils.hpp").read_text()
        self.assertNotIn("experimental-time-offset", recorder_source + collect_source)
        self.assertNotIn("estimate_time_offset", (root / "tools/time_alignment.py").read_text())
        self.assertIn("--require-hil-motion", validator_source)
        for unsupported_gate in ("max-static-action-ratio", "max-tracking-error-ratio", "max-episode-duration"):
            self.assertNotIn(unsupported_gate, validator_source)
        self.assertIn("usb_cam_node_exe", camera_launcher)
        self.assertIn("FR5_COLLECTION_FPS:-30", camera_launcher)
        self.assertIn("/dev/v4l/by-id", camera_launcher)
        self.assertIn("git submodule update --init --recursive", setup)
        self.assertNotIn("rm -rf src/frcobot_ros2", setup)
        self.assertIn("epoch_time.tv_sec * 1000000 + epoch_time.tv_usec;", usb_cam_timestamp)

    def test_vendor_patch_applies_to_the_pinned_submodule(self):
        root = Path(__file__).resolve().parents[1]
        submodule = root / "src/frcobot_ros2"
        patch_path = root / "patches/frcobot_ros2.patch"
        headers = [
            line.split(" b/", 1)[1]
            for line in patch_path.read_text().splitlines()
            if line.startswith("diff --git ")
        ]
        self.assertEqual(headers, [
            "fairino_hardware_v3_9_7/include/fairino_hardware/fairino_hardware_interface.hpp",
            "fairino_hardware_v3_9_7/src/CNDE_thread.cpp",
            "fairino_hardware_v3_9_7/src/command_server.cpp",
            "fairino_hardware_v3_9_7/src/fairino_hardware_interface.cpp",
        ])
        pinned = subprocess.check_output(
            ["git", "ls-tree", "HEAD", "src/frcobot_ros2"], cwd=root, text=True
        ).split()[2]
        for paths in ([], ["fairino_hardware_v3_9_7", "fairino_msgs"]):
            archive = subprocess.check_output(["git", "archive", pinned, *paths], cwd=submodule)
            with tempfile.TemporaryDirectory() as directory:
                with tarfile.open(fileobj=BytesIO(archive)) as source:
                    source.extractall(directory, filter="data")
                for command in (
                    ["git", "apply", "--check", patch_path],
                    ["git", "apply", patch_path],
                    ["git", "apply", "--reverse", "--check", patch_path],
                    ["git", "apply", "--reverse", patch_path],
                    ["git", "apply", "--check", patch_path],
                ):
                    result = subprocess.run(command, cwd=directory, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

if __name__ == "__main__":
    unittest.main()
