#!/usr/bin/env python3
"""Evaluate a trained SmolVLA checkpoint on held-out LeRobot episodes.

This reports deterministic offline flow-matching loss. It does not measure
real-robot task success and never sends robot commands.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import math
from pathlib import Path
import random
import statistics

try:
    from .fr5_dataset_schema import smolvla_camera_mapping
except ImportError:  # Direct script execution.
    from fr5_dataset_schema import smolvla_camera_mapping


def parse_episode_indices(value: str) -> list[int]:
    try:
        episodes = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    except ValueError as error:
        raise ValueError("episode indices must be comma-separated integers") from error
    if not episodes or any(index < 0 for index in episodes):
        raise ValueError("episode indices must be non-negative")
    return episodes


def select_eval_episodes(episode_tasks: list[list[str]], fraction: float) -> list[int]:
    if not 0 < fraction < 1:
        raise ValueError("eval split must be between 0 and 1")
    grouped: dict[str, list[int]] = {}
    for index, tasks in enumerate(episode_tasks):
        key = tasks[0] if tasks else ""
        grouped.setdefault(key, []).append(index)
    selected: list[int] = []
    for task, episodes in grouped.items():
        count = math.ceil(len(episodes) * fraction)
        if count >= len(episodes):
            raise ValueError(
                f"eval split leaves no training episode for task {task!r}; "
                "collect more episodes or pass --episodes explicitly"
            )
        selected.extend(episodes[-count:])
    return sorted(selected)


def normalize_checkpoint_path(checkpoint: str) -> str:
    path = Path(checkpoint).expanduser()
    nested = path / "pretrained_model"
    return str(nested) if nested.joinpath("config.json").is_file() else checkpoint


def find_training_split(checkpoint: str) -> Path | None:
    path = Path(checkpoint).expanduser()
    if not path.exists():
        return None
    for directory in (path, *path.parents):
        candidate = directory / "fr5_training_split.json"
        if candidate.is_file():
            return candidate
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", help="Local checkpoint directory or Hugging Face model id")
    parser.add_argument("dataset", type=Path, help="Local LeRobot dataset root")
    parser.add_argument("--repo-id", default="local/fr5_smolvla")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--episodes", help="Comma-separated held-out episode indices")
    selection.add_argument(
        "--eval-split",
        type=float,
        help="Use the last fraction of episodes per task, matching LeRobot train splitting",
    )
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--max-batches", type=int, default=0, help="0 evaluates all batches")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--use-amp", action="store_true")
    parser.add_argument(
        "--allow-unverified-split",
        action="store_true",
        help="Allow evaluation when no matching fr5_training_split.json can be found",
    )
    parser.add_argument("--seed", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def evaluate(args: argparse.Namespace) -> dict:
    import numpy as np
    import torch
    from torch.utils.data import DataLoader

    from lerobot.datasets.factory import resolve_delta_timestamps
    from lerobot.datasets.lerobot_dataset import LeRobotDataset, LeRobotDatasetMetadata
    from lerobot.configs import FeatureType
    from lerobot.policies.factory import make_policy, make_pre_post_processors
    from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig
    from lerobot.utils.feature_utils import dataset_to_policy_features

    if args.batch_size < 1 or args.num_workers < 0 or args.max_batches < 0:
        raise ValueError("batch size must be positive; worker and batch limits must be non-negative")
    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is not available")

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    checkpoint = normalize_checkpoint_path(args.checkpoint)
    metadata = LeRobotDatasetMetadata(args.repo_id, root=args.dataset)
    rename_map, empty_cameras = smolvla_camera_mapping(metadata.camera_keys)
    episode_tasks = metadata.episodes["tasks"]
    episodes = (
        parse_episode_indices(args.episodes)
        if args.episodes
        else select_eval_episodes(episode_tasks, args.eval_split)
    )
    invalid = [index for index in episodes if index >= metadata.total_episodes]
    if invalid:
        raise ValueError(f"episode indices are outside dataset range: {invalid}")

    split_path = find_training_split(checkpoint)
    split_verified = False
    if split_path:
        split = json.loads(split_path.read_text())
        expected = set(split.get("eval_episodes", []))
        if split.get("repo_id") != args.repo_id or split.get("total_episodes") != metadata.total_episodes or split.get("total_frames") != metadata.total_frames:
            raise ValueError(f"training split does not match this dataset: {split_path}")
        if not set(episodes).issubset(expected):
            raise ValueError(f"selected episodes are not held out by {split_path}: expected subset of {sorted(expected)}")
        split_verified = True
    elif not args.allow_unverified_split:
        raise ValueError("fr5_training_split.json not found; use the matching training output or pass --allow-unverified-split for diagnostics only")

    policy_config = SmolVLAConfig.from_pretrained(checkpoint)
    policy_config.pretrained_path = checkpoint
    policy_config.device = device
    policy_config.empty_cameras = empty_cameras
    features = dataset_to_policy_features(metadata.features)
    policy_config.input_features = {
        rename_map.get(key, key): feature
        for key, feature in features.items()
        if feature.type is not FeatureType.ACTION
    }
    policy = make_policy(policy_config, ds_meta=metadata, rename_map=rename_map)
    state_dim = policy.config.robot_state_feature.shape[0]
    action_dim = policy.config.action_feature.shape[0]
    if (state_dim, action_dim) != (7, 7):
        raise RuntimeError(f"FR5 policy feature mismatch: state={state_dim}, action={action_dim}, expected 7/7")
    policy.eval()

    preprocessor, _ = make_pre_post_processors(
        policy_cfg=policy_config,
        pretrained_path=checkpoint,
        preprocessor_overrides={
            "device_processor": {"device": device},
            "rename_observations_processor": {"rename_map": rename_map},
        },
    )
    dataset = LeRobotDataset(
        args.repo_id,
        root=args.dataset,
        episodes=episodes,
        delta_timestamps=resolve_delta_timestamps(policy_config, metadata),
        return_uint8=True,
    )
    loader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
        pin_memory=device.startswith("cuda"),
    )

    losses: list[float] = []
    amp = torch.autocast(device_type="cuda") if args.use_amp and device.startswith("cuda") else nullcontext()
    with torch.no_grad(), amp:
        for batch_index, batch in enumerate(loader):
            if args.max_batches and batch_index >= args.max_batches:
                break
            for key in dataset.meta.camera_keys:
                if key in batch and batch[key].dtype == torch.uint8:
                    batch[key] = batch[key].float() / 255.0
            batch = preprocessor(batch)
            per_sample_loss, _ = policy.forward(batch, reduction="none")
            losses.extend(float(value) for value in per_sample_loss.detach().cpu())

    if not losses:
        raise RuntimeError("no samples were evaluated")
    ordered = sorted(losses)
    report = {
        "schema_version": 1,
        "metric": "smolvla_offline_flow_matching_loss",
        "warning": "Offline loss does not measure real-robot task success.",
        "checkpoint": checkpoint,
        "dataset": str(args.dataset.resolve()),
        "repo_id": args.repo_id,
        "episodes": episodes,
        "samples": len(losses),
        "batch_size": args.batch_size,
        "seed": args.seed,
        "device": device,
        "use_amp": args.use_amp,
        "camera_rename_map": rename_map,
        "empty_cameras": empty_cameras,
        "state_dim": state_dim,
        "action_dim": action_dim,
        "training_split": str(split_path) if split_path else None,
        "split_verified": split_verified,
        "loss_mean": statistics.fmean(losses),
        "loss_std": statistics.pstdev(losses),
        "loss_p95": ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)],
    }
    return report


def main() -> None:
    args = parse_args()
    report = evaluate(args)
    serialized = json.dumps(report, indent=2, sort_keys=True)
    print(serialized)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(serialized + "\n")
        temporary.replace(args.output)


if __name__ == "__main__":
    main()
