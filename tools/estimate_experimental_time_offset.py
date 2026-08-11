#!/usr/bin/env python3
"""Experimental visual-motion estimate of a fixed camera timestamp offset."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import Image, JointState

from fr5_dataset_schema import ARM_NAMES
from ros_image import image_message_to_rgb
from time_alignment import estimate_time_offset


class OffsetMeasurement(Node):
    def __init__(self, image_topic: str, image_qos: str) -> None:
        super().__init__("fr5_camera_time_offset_calibration")
        self.last_image: tuple[float, np.ndarray] | None = None
        self.last_joint: tuple[float, np.ndarray] | None = None
        self.image_times: list[float] = []
        self.image_motion: list[float] = []
        self.robot_times: list[float] = []
        self.robot_speed: list[float] = []
        self.image_errors = 0
        qos = QoSProfile(depth=4, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE)
        if image_qos == "best-effort":
            qos = qos_profile_sensor_data
        self.create_subscription(Image, image_topic, self._on_image, qos)
        self.create_subscription(JointState, "/joint_states", self._on_joint, qos_profile_sensor_data)

    @staticmethod
    def _stamp(message) -> float:
        return float(message.header.stamp.sec) + message.header.stamp.nanosec * 1e-9

    def _on_image(self, message: Image) -> None:
        stamp = self._stamp(message)
        if stamp <= 0 or (self.last_image is not None and stamp <= self.last_image[0]):
            return
        try:
            image = cv2.cvtColor(image_message_to_rgb(message), cv2.COLOR_RGB2GRAY)
            image = cv2.resize(image, (160, 120), interpolation=cv2.INTER_AREA)
        except Exception as exc:
            self.image_errors += 1
            self.get_logger().warning(f"image sample rejected: {exc}")
            return
        if self.last_image is not None:
            flow = cv2.calcOpticalFlowFarneback(
                self.last_image[1], image, None, 0.5, 3, 15, 3, 5, 1.2, 0
            )
            self.image_times.append((stamp + self.last_image[0]) / 2)
            self.image_motion.append(float(np.percentile(np.linalg.norm(flow, axis=2), 75)))
        self.last_image = stamp, image

    def _on_joint(self, message: JointState) -> None:
        stamp = self._stamp(message)
        values = dict(zip(message.name, message.position))
        if (
            stamp <= 0
            or (self.last_joint is not None and stamp <= self.last_joint[0])
            or not all(name in values for name in ARM_NAMES)
        ):
            return
        joints = np.asarray([values[name] for name in ARM_NAMES], dtype=float)
        if not np.isfinite(joints).all():
            return
        if self.last_joint is not None:
            dt = stamp - self.last_joint[0]
            self.robot_times.append((stamp + self.last_joint[0]) / 2)
            self.robot_speed.append(float(np.linalg.norm(joints - self.last_joint[1]) / dt))
        self.last_joint = stamp, joints


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera-role", choices=("up", "side"), required=True)
    parser.add_argument("--image-topic")
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--max-offset-ms", type=float, default=200.0)
    parser.add_argument("--min-correlation", type=float, default=0.35)
    parser.add_argument("--min-peak-margin", type=float, default=0.03)
    parser.add_argument("--max-peak-width-ms", type=float, default=40.0)
    parser.add_argument("--max-window-drift-ms", type=float, default=20.0)
    parser.add_argument("--image-qos", choices=("reliable", "best-effort"), default="reliable")
    parser.add_argument("--output", type=Path, default=Path("config/time-offsets.json"))
    args = parser.parse_args()
    numeric = [
        args.duration, args.max_offset_ms, args.min_correlation, args.min_peak_margin,
        args.max_peak_width_ms, args.max_window_drift_ms,
    ]
    if not np.isfinite(numeric).all() or args.duration <= 0 or args.max_offset_ms < 50:
        raise SystemExit("finite positive values and max-offset-ms >= 50 are required")
    topic = args.image_topic or f"/camera/{args.camera_role}/color/image_raw"

    rclpy.init()
    node = OffsetMeasurement(topic, args.image_qos)
    print(f"Measuring {topic} for {args.duration:.0f}s. Move one camera-visible joint back and forth several times.")
    deadline = time.monotonic() + args.duration
    try:
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        node.destroy_node()
        rclpy.shutdown()

    try:
        offset, correlation, margin, peak_width = estimate_time_offset(
            node.image_times,
            node.image_motion,
            node.robot_times,
            node.robot_speed,
            args.max_offset_ms / 1000,
        )
        midpoint = len(node.image_times) // 2
        first = estimate_time_offset(
            node.image_times[:midpoint], node.image_motion[:midpoint],
            node.robot_times, node.robot_speed, args.max_offset_ms / 1000,
        )
        second = estimate_time_offset(
            node.image_times[midpoint:], node.image_motion[midpoint:],
            node.robot_times, node.robot_speed, args.max_offset_ms / 1000,
        )
    except ValueError as exc:
        measurement = {
            "camera_role": args.camera_role, "image_topic": topic, "accepted": False,
            "reason": str(exc), "measured_unix_s": time.time(),
        }
        write_profile(args.output, args.camera_role, measurement)
        raise SystemExit(f"REJECTED: {exc}; existing {args.camera_role} offset invalidated") from exc
    offset_ms = offset * 1000
    max_speed = max(node.robot_speed, default=0.0)
    visual_range = float(np.ptp(node.image_motion)) if node.image_motion else 0.0
    window_drift_ms = abs(first[0] - second[0]) * 1000
    accepted = (
        correlation >= args.min_correlation
        and margin >= args.min_peak_margin
        and peak_width * 1000 <= args.max_peak_width_ms
        and first[1] >= args.min_correlation
        and second[1] >= args.min_correlation
        and window_drift_ms <= args.max_window_drift_ms
        and len(node.image_motion) >= 100
        and len(node.robot_speed) >= 300
        and max_speed >= 0.05
        and visual_range > 0
        and abs(offset_ms) < args.max_offset_ms - 1
    )
    measurement = {
        "camera_role": args.camera_role,
        "image_topic": topic,
        "offset_ms": offset_ms,
        "correlation": correlation,
        "peak_margin": margin,
        "peak_width_ms": peak_width * 1000,
        "window_offsets_ms": [first[0] * 1000, second[0] * 1000],
        "window_drift_ms": window_drift_ms,
        "image_motion_samples": len(node.image_motion),
        "robot_motion_samples": len(node.robot_speed),
        "max_robot_speed_rad_s": max_speed,
        "visual_motion_range": visual_range,
        "image_errors": node.image_errors,
        "image_width": 640,
        "image_height": 480,
        "search_max_offset_ms": args.max_offset_ms,
        "method": "dense_optical_flow_farneback_v1",
        "accepted": accepted,
        "measured_unix_s": time.time(),
    }
    print(json.dumps(measurement, indent=2))
    if not accepted:
        write_profile(args.output, args.camera_role, measurement)
        raise SystemExit("REJECTED: weak, broad, or inconsistent correlation; existing role offset invalidated")

    write_profile(args.output, args.camera_role, measurement, offset_ms)
    print(
        f"EXPERIMENTAL ESTIMATE ONLY: wrote {args.output}; independently validate before "
        f"--experimental-time-offset-profile {args.output}"
    )


def write_profile(path: Path, role: str, measurement: dict, offset_ms: float | None = None) -> None:
    profile = {"schema_version": 1, "offsets_ms": {}, "measurements": {}}
    if path.exists():
        profile = json.loads(path.read_text())
    profile.setdefault("measurements", {})[role] = measurement
    if offset_ms is None:
        profile.setdefault("offsets_ms", {}).pop(role, None)
    else:
        profile.setdefault("offsets_ms", {})[role] = offset_ms
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(profile, indent=2) + "\n")
    temporary.replace(path)


if __name__ == "__main__":
    main()
