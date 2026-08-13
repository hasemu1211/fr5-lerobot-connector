#!/usr/bin/env python3
"""Validate that a LeRobot checkpoint is complete and safe to resume."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REQUIRED_TRAINING_STATE = (
    "optimizer_param_groups.json",
    "optimizer_state.safetensors",
    "rng_state.safetensors",
    "training_step.json",
)


def normalize_policy_dir(value: Path) -> Path:
    path = value.expanduser().resolve()
    if path.is_file():
        path = path.parent
    if (path / "pretrained_model").is_dir():
        path = path / "pretrained_model"
    return path


def validate_checkpoint(value: Path, *, verify_dataset: bool = True) -> tuple[Path, Path]:
    policy_dir = normalize_policy_dir(value)
    checkpoint_dir = policy_dir.parent
    if checkpoint_dir.parent.name != "checkpoints":
        raise ValueError("checkpoint must be under <output>/checkpoints/<step>/pretrained_model")

    required = [policy_dir / "config.json", policy_dir / "train_config.json"]
    required.append(policy_dir / "model.safetensors")
    state_dir = checkpoint_dir / "training_state"
    required.extend(state_dir / name for name in REQUIRED_TRAINING_STATE)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError("incomplete checkpoint; missing: " + ", ".join(missing))

    config = json.loads((policy_dir / "train_config.json").read_text())
    if config.get("scheduler") is not None and not (state_dir / "scheduler_state.json").is_file():
        raise ValueError("incomplete checkpoint; missing: " + str(state_dir / "scheduler_state.json"))
    training_step = json.loads((state_dir / "training_step.json").read_text()).get("step")
    if not isinstance(training_step, int) or training_step < 1:
        raise ValueError("training_state/training_step.json has no positive integer step")

    output_dir = checkpoint_dir.parent.parent
    split_path = output_dir / "fr5_training_split.json"
    if not split_path.is_file():
        split_path = output_dir.with_name(output_dir.name + ".fr5_training_split.json.pending")
    if not split_path.is_file():
        raise ValueError(f"connector split manifest is missing: {split_path}")

    split = json.loads(split_path.read_text())
    dataset_cfg = config.get("dataset") or {}
    if split.get("repo_id") != dataset_cfg.get("repo_id"):
        raise ValueError("checkpoint dataset repo_id differs from fr5_training_split.json")
    if split.get("eval_split") != dataset_cfg.get("eval_split"):
        raise ValueError("checkpoint eval_split differs from fr5_training_split.json")

    if verify_dataset:
        from lerobot.datasets.lerobot_dataset import LeRobotDatasetMetadata

        root = Path(dataset_cfg.get("root", "")).expanduser()
        metadata = LeRobotDatasetMetadata(dataset_cfg["repo_id"], root=root)
        if (
            split.get("total_episodes") != metadata.total_episodes
            or split.get("total_frames") != metadata.total_frames
        ):
            raise ValueError("dataset changed after this training split was recorded")

    return policy_dir, output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--shell", action="store_true", help="Print NUL-delimited policy and output paths")
    args = parser.parse_args()
    try:
        policy_dir, output_dir = validate_checkpoint(args.checkpoint)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    if args.shell:
        for path in (policy_dir, output_dir):
            sys.stdout.buffer.write(str(path).encode() + b"\0")
    else:
        print(f"PASS checkpoint={policy_dir} output={output_dir}")


if __name__ == "__main__":
    main()
