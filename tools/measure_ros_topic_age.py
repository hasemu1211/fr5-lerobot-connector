#!/usr/bin/env python3
"""Measure ROS header age and source cadence without recording a dataset."""

import argparse
import json
import time

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import Image, JointState


def stamp_seconds(message) -> float:
    return message.header.stamp.sec + message.header.stamp.nanosec * 1e-9


def report(name: str, stamps: list[float], ages: list[float], duration: float) -> None:
    if not stamps:
        print(f"{name}: no messages")
        return
    intervals = np.diff(stamps)
    age_ms = np.asarray(ages) * 1000
    unique = 1 + int(np.count_nonzero(intervals)) if stamps else 0
    print(
        f"{name}: received={len(stamps)} ({len(stamps)/duration:.2f}Hz), "
        f"source={unique/duration:.2f}Hz, age_ms p50={np.percentile(age_ms, 50):.2f} "
        f"p95={np.percentile(age_ms, 95):.2f} max={age_ms.max():.2f}, "
        f"source_gap_ms p95={np.percentile(intervals, 95)*1000:.2f} "
        f"max={intervals.max()*1000:.2f}" if intervals.size else f"{name}: one message"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="/camera/up/color/image_raw")
    parser.add_argument("--joint", default="/joint_states")
    parser.add_argument("--metadata", default="/camera/up/color/metadata")
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--reliable-image", action="store_true")
    parser.add_argument("--image-qos-depth", type=int, default=10)
    args = parser.parse_args()
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
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
