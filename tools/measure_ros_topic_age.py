#!/usr/bin/env python3
"""Measure ROS header age and source cadence without recording a dataset."""

import argparse
import json
import sys
import time

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import Image, JointState


def stamp_seconds(message) -> float:
    return message.header.stamp.sec + message.header.stamp.nanosec * 1e-9


def source_cadence(stamps: list[float], duration: float) -> tuple[int, float, float]:
    if not stamps:
        return 0, 0.0, 0.0
    intervals = np.diff(stamps)
    unique = 1 + int(np.count_nonzero(intervals))
    span = float(stamps[-1] - stamps[0]) if len(stamps) > 1 else 0.0
    hz = (unique - 1) / span if span > 0 else unique / duration
    return unique, span, hz


def report(name: str, stamps: list[float], ages: list[float], duration: float) -> None:
    if not stamps:
        print(f"{name}: no messages")
        return
    intervals = np.diff(stamps)
    age_ms = np.asarray(ages) * 1000
    _unique, source_span, source_hz = source_cadence(stamps, duration)
    print(
        f"{name}: received={len(stamps)} ({len(stamps)/duration:.2f}Hz), "
        f"source={source_hz:.2f}Hz over {source_span:.2f}s, "
        f"age_ms p50={np.percentile(age_ms, 50):.2f} "
        f"p95={np.percentile(age_ms, 95):.2f} max={age_ms.max():.2f}, "
        f"source_gap_ms p95={np.percentile(intervals, 95)*1000:.2f} "
        f"max={intervals.max()*1000:.2f}" if intervals.size else f"{name}: one message"
    )


def image_gate_failures(
    stamps: list[float],
    ages: list[float],
    duration: float,
    expected_hz: float,
    min_fps_ratio: float,
    max_age_ms: float,
    min_observation_s: float = 2.0,
) -> list[str]:
    if not stamps:
        return ["no image messages"]
    _unique, source_span, source_hz = source_cadence(stamps, duration)
    age_ms = np.asarray(ages) * 1000
    failures = []
    required_observation_s = min(duration, min_observation_s)
    if source_span < required_observation_s:
        failures.append(
            f"source observation {source_span:.2f}s below {required_observation_s:.2f}s"
        )
    if source_hz < expected_hz * min_fps_ratio:
        failures.append(f"source rate {source_hz:.2f}Hz below {expected_hz * min_fps_ratio:.2f}Hz")
    if age_ms.min() < 0:
        failures.append(f"negative image age {age_ms.min():.2f}ms")
    if np.percentile(age_ms, 95) > max_age_ms:
        failures.append(f"image age p95 {np.percentile(age_ms, 95):.2f}ms exceeds {max_age_ms:.2f}ms")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="/camera/up/color/image_raw")
    parser.add_argument("--joint", default="/joint_states")
    parser.add_argument("--metadata", default="/camera/up/color/metadata")
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--reliable-image", action="store_true")
    parser.add_argument("--image-qos-depth", type=int, default=10)
    parser.add_argument("--expected-image-hz", type=float)
    parser.add_argument("--min-image-fps-ratio", type=float, default=0.75)
    parser.add_argument("--max-image-age-ms", type=float, default=300.0)
    parser.add_argument("--min-image-observation-s", type=float, default=2.0)
    args = parser.parse_args()
    if args.duration <= 0 or args.image_qos_depth <= 0:
        parser.error("duration and image-qos-depth must be positive")
    if args.expected_image_hz is not None and (
        args.expected_image_hz <= 0 or not 0 < args.min_image_fps_ratio <= 1
        or args.max_image_age_ms <= 0 or args.min_image_observation_s <= 0
    ):
        parser.error("image gate values must be positive and min-image-fps-ratio must be <= 1")
    rclpy.init()
    node = Node("fr5_topic_age_probe")
    samples = {"image": ([], []), "joint": ([], [])}
    frame_counters = []

    def callback(name):
        def collect(message):
            stamp = stamp_seconds(message)
            if stamp > 0:
                samples[name][0].append(stamp)
                samples[name][1].append(node.get_clock().now().nanoseconds * 1e-9 - stamp)
        return collect

    image_qos = QoSProfile(
        depth=args.image_qos_depth,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    ) if args.reliable_image else qos_profile_sensor_data
    node.create_subscription(Image, args.image, callback("image"), image_qos)
    node.create_subscription(JointState, args.joint, callback("joint"), qos_profile_sensor_data)
    try:
        from realsense2_camera_msgs.msg import Metadata
        node.create_subscription(
            Metadata,
            args.metadata,
            lambda message: frame_counters.append(int(json.loads(message.json_data)["frame_counter"])),
            qos_profile_sensor_data,
        )
    except ImportError:
        pass
    started = time.monotonic()
    while rclpy.ok() and time.monotonic() - started < args.duration:
        rclpy.spin_once(node, timeout_sec=0.1)
    actual_duration = time.monotonic() - started
    for name, (stamps, ages) in samples.items():
        report(name, stamps, ages, actual_duration)
    if len(frame_counters) > 1:
        differences = np.diff(frame_counters)
        print(
            f"metadata: received={len(frame_counters)}, counter_skips={int((differences - 1).clip(min=0).sum())}, "
            f"max_counter_step={int(differences.max())}"
        )
    failures = []
    if args.expected_image_hz is not None:
        failures = image_gate_failures(
            *samples["image"], actual_duration, args.expected_image_hz,
            args.min_image_fps_ratio, args.max_image_age_ms,
            args.min_image_observation_s,
        )
    node.destroy_node()
    rclpy.shutdown()
    if failures:
        print("FAIL: " + "; ".join(failures), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
