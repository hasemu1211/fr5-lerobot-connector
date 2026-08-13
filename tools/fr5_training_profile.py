#!/usr/bin/env python3
"""Build policy-specific LeRobot CLI arguments from FR5 dataset metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

try:
    from .fr5_dataset_schema import smolvla_camera_mapping
except ImportError:  # Direct script execution.
    from fr5_dataset_schema import smolvla_camera_mapping


PROFILE_NAMES = ("smolvla", "act", "vqbet-up", "vqbet-side", "vqbet-wrist")


def build_profile(profile: str, metadata) -> list[str]:
    from lerobot.configs import FeatureType
    from lerobot.utils.feature_utils import dataset_to_policy_features

    if profile not in PROFILE_NAMES:
        raise ValueError(f"unknown training profile: {profile}")

    features = dataset_to_policy_features(metadata.features)
    output_features = {
        key: {"type": feature.type.value, "shape": list(feature.shape)}
        for key, feature in features.items()
        if feature.type is FeatureType.ACTION
    }
    if output_features != {"action": {"type": "ACTION", "shape": [7]}}:
        raise ValueError(f"FR5 profile requires one 7D action feature, got {output_features}")

    if profile == "smolvla":
        rename_map, empty_cameras = smolvla_camera_mapping(list(metadata.camera_keys))
        input_features = {
            rename_map.get(key, key): {"type": feature.type.value, "shape": list(feature.shape)}
            for key, feature in features.items()
            if feature.type is not FeatureType.ACTION
        }
        return [
            "--policy.path=lerobot/smolvla_base",
            "--rename_map=" + json.dumps(rename_map, separators=(",", ":")),
            f"--policy.empty_cameras={empty_cameras}",
            "--policy.input_features=" + json.dumps(input_features, separators=(",", ":")),
            "--policy.output_features=" + json.dumps(output_features, separators=(",", ":")),
        ]

    if profile == "act":
        input_features = {
            key: {"type": feature.type.value, "shape": list(feature.shape)}
            for key, feature in features.items()
            if feature.type is not FeatureType.ACTION
        }
        return [
            "--policy.type=act",
            "--policy.input_features=" + json.dumps(input_features, separators=(",", ":")),
            "--policy.output_features=" + json.dumps(output_features, separators=(",", ":")),
        ]

    camera = profile.removeprefix("vqbet-")
    camera_key = f"observation.images.{camera}"
    if camera_key not in metadata.camera_keys:
        available = ", ".join(key.rsplit(".", 1)[-1] for key in metadata.camera_keys)
        raise ValueError(f"profile {profile} requires camera {camera!r}; available: {available}")
    input_features = {
        key: {"type": features[key].type.value, "shape": list(features[key].shape)}
        for key in ("observation.state", camera_key)
    }
    return [
        "--policy.type=vqbet",
        "--policy.input_features=" + json.dumps(input_features, separators=(",", ":")),
        "--policy.output_features=" + json.dumps(output_features, separators=(",", ":")),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=PROFILE_NAMES)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--repo-id", default="local/fr5_connector")
    args = parser.parse_args()

    from lerobot.datasets.lerobot_dataset import LeRobotDatasetMetadata

    metadata = LeRobotDatasetMetadata(args.repo_id, root=args.dataset)
    try:
        values = build_profile(args.profile, metadata)
    except ValueError as error:
        parser.error(str(error))
    for value in values:
        sys.stdout.buffer.write(value.encode() + b"\0")


if __name__ == "__main__":
    main()
