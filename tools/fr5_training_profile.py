#!/usr/bin/env python3
"""Build policy-specific LeRobot CLI arguments from FR5 dataset metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from .fr5_dataset_schema import smolvla_camera_mapping
except ImportError:  # Direct script execution.
    from fr5_dataset_schema import smolvla_camera_mapping


PROFILE_NAMES = ("smolvla", "act", "vqbet-up", "vqbet-side", "vqbet-wrist")


def training_normalization(split: dict, *, use_imagenet_stats: bool = True) -> dict:
    """Aggregate frozen TRAIN metadata only; never read frames or rewrite stats.json."""
    import numpy as np
    import pyarrow.parquet as pq
    from lerobot.datasets.compute_stats import aggregate_stats
    from lerobot.datasets.factory import IMAGENET_STATS
    from tools.fr5_data_factory import ContractError

    features = split["feature_contract"]["dataset_features"]
    keys = [key for key in features if key in {"action", "observation.state"}
            or key.startswith("observation.images.")]
    names = ("min", "max", "mean", "std", "count")
    columns = [f"stats/{key}/{name}" for key in keys for name in names]
    rows = []
    root = Path(split["dataset_identity"]["dataset_root"])
    for path in sorted((root / "meta/episodes").rglob("*.parquet")):
        rows.extend(pq.read_table(path, columns=["episode_index", "length", *columns],
            filters=[("episode_index", "in", split["train_episodes"])]).to_pylist())
    if sorted(row["episode_index"] for row in rows) != sorted(split["train_episodes"]):
        raise ContractError("TRAINING_NORMALIZATION_EPISODES")
    episode_stats = []
    for row in sorted(rows, key=lambda row: row["episode_index"]):
        stats = {}
        for key in keys:
            shape = (3, 1, 1) if key.startswith("observation.images.") else (7,)
            stats[key] = {name: np.asarray(row[f"stats/{key}/{name}"], dtype=np.float64)
                          for name in names}
            for name, value in stats[key].items():
                if value.shape != ((1,) if name == "count" else shape) or not np.isfinite(value).all():
                    raise ContractError("TRAINING_NORMALIZATION_STATS")
            count = stats[key]["count"][0]
            if (not 0 < count <= row["length"] or count != int(count)
                    or (key in {"action", "observation.state"} and count != row["length"])
                    or (stats[key]["std"] < 0).any()):
                raise ContractError("TRAINING_NORMALIZATION_STATS")
        episode_stats.append(stats)
    aggregated = aggregate_stats(episode_stats)
    if use_imagenet_stats:
        for key in keys:
            if key.startswith("observation.images."):
                aggregated[key].update({name: np.asarray(value) for name, value in IMAGENET_STATS.items()})
    return {
        "algorithm": "lerobot-0.6.1-train-episode-moments-v1",
        "episodes": list(split["train_episodes"]),
        "use_imagenet_stats": use_imagenet_stats,
        "stats": {key: {name: value.tolist() for name, value in stats.items()}
                  for key, stats in aggregated.items()},
    }


def read_metadata(root: Path) -> dict:
    """Read local v3 metadata without constructing/downloading a dataset or cache."""
    import pyarrow.parquet as pq
    from tools.fr5_data_factory import ContractError, load_json_strict

    info = load_json_strict(root / "meta/info.json")
    rows = []
    for path in sorted((root / "meta/episodes").rglob("*.parquet")):
        rows.extend(pq.read_table(path, columns=["episode_index", "tasks", "length"]).to_pylist())
    rows.sort(key=lambda row: row["episode_index"])
    if (not rows or [r["episode_index"] for r in rows] != list(range(info["total_episodes"]))
            or sum(r["length"] for r in rows) != info["total_frames"]):
        raise ContractError("TRAINING_METADATA_EPISODES")
    return {**info, "episode_tasks": [row["tasks"] for row in rows]}


def policy_metadata(info: dict):
    return SimpleNamespace(features=info["features"], camera_keys=[
        key for key in info["features"] if key.startswith("observation.images.")
    ])


def launch_feature_contract(profile: str, collection_profile_id: str, task: str, info: dict) -> dict:
    from tools.fr5_data_factory import ContractError, TASK_CONTRACTS, _profile, canonical_digest

    collection = _profile(
        Path(__file__).resolve().parents[1] / "config/data_factory",
        "collection_profiles", collection_profile_id, "collection_profile_id", "data_factory.collection_profile.v1",
    )
    if collection["schema_version"] != "data_factory.collection_profile.v2" or task not in TASK_CONTRACTS:
        raise ContractError("TRAINING_FEATURE_SOURCE")
    features = info["features"]
    for key in ("action", "observation.state"):
        if features[key]["shape"] != [7] or features[key]["dtype"] != "float32":
            raise ContractError("TRAINING_FEATURE_DIMENSIONS")
    expected_cameras = {f"observation.images.{role}" for role in collection["camera_roles"]}
    if set(policy_metadata(info).camera_keys) != expected_cameras or info["fps"] != collection["fps"]:
        raise ContractError("TRAINING_COLLECTION_PROFILE")
    for key in expected_cameras:
        if features[key]["shape"] != [collection["height"], collection["width"], 3]:
            raise ContractError("TRAINING_COLLECTION_PROFILE")
    return {
        "profile": profile, "collection_profile_id": collection_profile_id,
        "collection_profile_digest": canonical_digest(collection),
        "camera_profile": collection["camera_profile"], "task": task,
        "task_contract_digest": canonical_digest(TASK_CONTRACTS[task]),
        "dataset_features": features, "fps": info["fps"],
        "policy_argv": build_profile(profile, policy_metadata(info)),
    }


def validate_launch_feature_contract(value: dict) -> dict:
    from tools.fr5_data_factory import ContractError

    expected = launch_feature_contract(value["profile"], value["collection_profile_id"], value["task"], {
        "features": value["dataset_features"], "fps": value["fps"],
    })
    if value != expected:
        raise ContractError("TRAINING_FEATURE_CONTRACT")
    return expected


def instruction_task(instruction: str) -> str:
    """Resolve canonical collection labels, checking specific pick-place forms first."""
    import re
    from tools.fr5_data_factory import ContractError, task_instruction

    for task, regions in (("pick_place", ("RED", "BLUE")), ("pick_place", ("BLUE", "RED")),
                          ("pick_place", None), ("pickup_e2e", None)):
        kwargs = {} if regions is None else dict(source_region_id=regions[0], destination_region_id=regions[1], region_binding_active=True)
        pattern = re.escape(task_instruction(task, "OBJECTTOKEN", **kwargs)).replace("OBJECTTOKEN", ".+")
        if re.fullmatch(pattern, instruction):
            return task
    raise ContractError("TRAINING_TASK_LABEL")


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
            "--rename_map=" + json.dumps(rename_map, separators=(",", ":"), sort_keys=True),
            f"--policy.empty_cameras={empty_cameras}",
            "--policy.input_features=" + json.dumps(input_features, separators=(",", ":"), sort_keys=True),
            "--policy.output_features=" + json.dumps(output_features, separators=(",", ":"), sort_keys=True),
        ]

    if profile == "act":
        input_features = {
            key: {"type": feature.type.value, "shape": list(feature.shape)}
            for key, feature in features.items()
            if feature.type is not FeatureType.ACTION
        }
        return [
            "--policy.type=act",
            "--policy.input_features=" + json.dumps(input_features, separators=(",", ":"), sort_keys=True),
            "--policy.output_features=" + json.dumps(output_features, separators=(",", ":"), sort_keys=True),
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
        "--policy.input_features=" + json.dumps(input_features, separators=(",", ":"), sort_keys=True),
        "--policy.output_features=" + json.dumps(output_features, separators=(",", ":"), sort_keys=True),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=PROFILE_NAMES)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--repo-id", default="local/fr5_connector")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    metadata = policy_metadata(read_metadata(args.dataset))
    try:
        values = build_profile(args.profile, metadata)
    except ValueError as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(values))
        return
    for value in values:
        sys.stdout.buffer.write(value.encode() + b"\0")


if __name__ == "__main__":
    main()
