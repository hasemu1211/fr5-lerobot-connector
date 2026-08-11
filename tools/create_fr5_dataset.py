#!/usr/bin/env python3
"""Create an empty LeRobot v3 dataset root for FR5 ROS recording."""

from __future__ import annotations

import argparse
from pathlib import Path

from fr5_dataset_schema import CAMERA_PROFILES, dataset_features


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("datasets/fr5_smolvla"))
    parser.add_argument("--repo-id", default="local/fr5_smolvla")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--no-videos", action="store_true", help="Store camera frames as images.")
    parser.add_argument("--camera-profile", choices=CAMERA_PROFILES, default="up")
    args = parser.parse_args()

    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    cameras = CAMERA_PROFILES[args.camera_profile]
    features = dataset_features(
        fps=args.fps, height=args.height, width=args.width, cameras=cameras, use_videos=not args.no_videos
    )

    if args.root.exists() and any(args.root.iterdir()):
        raise SystemExit(f"Refusing to overwrite non-empty dataset root: {args.root}")
    if args.root.exists():
        args.root.rmdir()

    dataset = LeRobotDataset.create(
        repo_id=args.repo_id, fps=args.fps, root=args.root, robot_type="fr5_ros2",
        features=features, use_videos=not args.no_videos,
    )
    dataset.finalize()
    print(f"created {args.root} (LeRobot v3; {args.fps} FPS; 7D state/action; cameras={','.join(cameras)})")


if __name__ == "__main__":
    main()
