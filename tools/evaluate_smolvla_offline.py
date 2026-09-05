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
import os
from pathlib import Path
import random
import statistics
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


def normalize_checkpoint_path(checkpoint: str) -> str:
    path = Path(checkpoint).expanduser()
    nested = path / "pretrained_model"
    return str(nested) if nested.joinpath("config.json").is_file() else checkpoint


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", help="Existing local SmolVLA checkpoint directory")
    parser.add_argument("dataset", type=Path, help="Local LeRobot dataset root")
    parser.add_argument("--repo-id", default="local/fr5_smolvla")
    parser.add_argument(
        "--approved-inventory", type=Path, required=True,
        help="External training_approved_inventory.v2 used by the admitted training launch",
    )
    parser.add_argument(
        "--episodes",
        help="Optional exact confirmation of the v3 split's held-out episode indices",
    )
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--max-batches", type=int, default=0, help="0 evaluates all batches")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--use-amp", action="store_true")
    parser.add_argument("--seed", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate admission and partition lineage without inference or output",
    )
    return parser.parse_args()


def _output_artifact(output_dir: Path, name: str) -> Path:
    final = output_dir / name
    pending = output_dir.with_name(output_dir.name + f".{name}.pending")
    if final.is_file() and not final.is_symlink():
        return final
    if pending.is_file() and not pending.is_symlink():
        return pending
    raise ValueError(f"checkpoint {name} is missing")


def _temporary_output_path(output: Path) -> Path:
    return output.with_suffix(output.suffix + ".tmp")


def _validate_output_target(
    output: Path,
    *,
    dataset: Path,
    checkpoint_root: Path,
) -> None:
    checkpoint_root = checkpoint_root.resolve()
    for candidate in (output.expanduser(), _temporary_output_path(output.expanduser())):
        resolved = candidate.resolve()
        if resolved.is_relative_to(dataset):
            raise ValueError("evaluation output must remain outside the immutable dataset")
        if resolved == checkpoint_root or resolved.is_relative_to(checkpoint_root):
            raise ValueError("evaluation output must not replace an immutable evaluation input")
        if candidate.exists() or candidate.is_symlink():
            raise ValueError(
                "evaluation output requires new paths to preserve every immutable evaluation input"
            )


def admit_evaluation(args: argparse.Namespace) -> dict:
    """Validate existing admission artifacts before loading torch or a policy."""
    from tools.data_factory import training_approval
    from tools.data_factory.training_receipts import (
        canonical_digest,
        tree_digest,
        validate_launch_receipt,
    )
    from tools.data_factory.training_split import validate_training_split
    from tools.validate_training_checkpoint import validate_checkpoint

    if args.batch_size < 1 or args.num_workers < 0 or args.max_batches < 0:
        raise ValueError("batch size must be positive; worker and batch limits must be non-negative")
    policy_dir, output_dir = validate_checkpoint(Path(args.checkpoint))
    split_path = _output_artifact(output_dir, "fr5_training_split.json")
    receipt_path = _output_artifact(output_dir, "fr5_training_receipt.json")
    split = validate_training_split(split_path)
    if split["schema_version"] != 3:
        raise ValueError("offline evaluation requires the current selected-episode split v3")
    receipt = validate_launch_receipt(json.loads(receipt_path.read_text()), split)

    dataset = args.dataset.expanduser().resolve()
    if (
        dataset != Path(split["dataset_identity"]["dataset_root"])
        or args.repo_id != split["repo_id"]
    ):
        raise ValueError("evaluation dataset differs from the admitted training dataset")
    inventory_path = args.approved_inventory.expanduser()
    inventory = training_approval.validate_current_training_inventory(
        inventory_path,
        dataset_root=dataset,
        repo_id=args.repo_id,
        selected_episodes=split["selected_episodes"],
    )
    if (
        inventory["inventory_digest"] != split["approved_episode_inventory_digest"]
        or inventory_path.resolve() != Path(receipt["approved_inventory_path"]).resolve()
    ):
        raise ValueError("approved inventory differs from the admitted training launch")

    feature = split["feature_contract"]
    if feature["profile"] != "smolvla":
        raise ValueError("offline evaluator only accepts an admitted SmolVLA partition")
    episodes = list(split["eval_episodes"])
    if args.episodes is not None and parse_episode_indices(args.episodes) != episodes:
        raise ValueError(f"episodes must exactly match held-out split episodes: {episodes}")
    if (
        not episodes
        or set(episodes) & set(split["train_episodes"])
        or set(episodes) | set(split["train_episodes"]) != set(split["selected_episodes"])
    ):
        raise ValueError("training split is not a disjoint partition of selected episodes")
    if args.output:
        _validate_output_target(
            args.output,
            dataset=dataset,
            checkpoint_root=policy_dir.parent,
        )

    return {
        "checkpoint": str(policy_dir),
        "checkpoint_tree_digest": tree_digest(policy_dir),
        "output_dir": str(output_dir),
        "split_path": str(split_path),
        "receipt_path": str(receipt_path),
        "split": split,
        "receipt": receipt,
        "inventory": inventory,
        "inventory_path": str(inventory_path.resolve()),
        "episodes": episodes,
        "receipt_digest": canonical_digest(receipt),
    }


def evaluate(args: argparse.Namespace, admission: dict | None = None) -> dict:
    admission = admission or admit_evaluation(args)
    import numpy as np
    import torch
    from torch.utils.data import DataLoader

    from lerobot.datasets.factory import resolve_delta_timestamps
    from lerobot.datasets.lerobot_dataset import LeRobotDataset, LeRobotDatasetMetadata
    from lerobot.configs import FeatureType
    from lerobot.policies.factory import make_policy, make_pre_post_processors
    from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig
    from lerobot.utils.feature_utils import dataset_to_policy_features

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

    checkpoint = admission["checkpoint"]
    metadata = LeRobotDatasetMetadata(args.repo_id, root=args.dataset)
    rename_map, empty_cameras = smolvla_camera_mapping(metadata.camera_keys)
    episodes = admission["episodes"]
    split = admission["split"]
    if metadata.total_episodes != split["total_episodes"] or metadata.total_frames != split["total_frames"]:
        raise ValueError("loaded dataset metadata differs from the admitted partition")

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
    evaluated_batches = 0
    evaluated_episodes: set[int] = set()
    available_batches = len(loader)
    amp = torch.autocast(device_type="cuda") if args.use_amp and device.startswith("cuda") else nullcontext()
    with torch.no_grad(), amp:
        for batch_index, batch in enumerate(loader):
            if args.max_batches and batch_index >= args.max_batches:
                break
            if "episode_index" not in batch:
                raise RuntimeError("evaluation batch is missing episode_index coverage metadata")
            batch_episodes = [int(value) for value in batch["episode_index"].detach().cpu().tolist()]
            if not set(batch_episodes).issubset(episodes):
                raise RuntimeError("evaluation batch contains an episode outside the admitted partition")
            for key in dataset.meta.camera_keys:
                if key in batch and batch[key].dtype == torch.uint8:
                    batch[key] = batch[key].float() / 255.0
            batch = preprocessor(batch)
            per_sample_loss, _ = policy.forward(batch, reduction="none")
            batch_losses = [float(value) for value in per_sample_loss.detach().cpu()]
            if len(batch_losses) != len(batch_episodes):
                raise RuntimeError("loss count differs from evaluated episode coverage metadata")
            losses.extend(batch_losses)
            evaluated_batches += 1
            evaluated_episodes.update(batch_episodes)

    if not losses:
        raise RuntimeError("no samples were evaluated")
    ordered = sorted(losses)
    evaluation_complete = (
        evaluated_batches == available_batches
        and sorted(evaluated_episodes) == episodes
    )
    report = {
        "schema_version": 3,
        "metric": "smolvla_offline_flow_matching_loss",
        "warning": "Offline loss does not measure real-robot task success.",
        "evidence_scope": (
            "admitted_heldout_offline_loss"
            if evaluation_complete
            else "bounded_admitted_heldout_offline_loss"
        ),
        "checkpoint": checkpoint,
        "checkpoint_tree_digest": admission["checkpoint_tree_digest"],
        "dataset": str(args.dataset.resolve()),
        "dataset_digest": split["dataset_identity"]["dataset_digest"],
        "repo_id": args.repo_id,
        "episodes": sorted(evaluated_episodes),
        "admitted_episodes": episodes,
        "selected_episodes": split["selected_episodes"],
        "train_episodes": split["train_episodes"],
        "samples": len(losses),
        "batch_size": args.batch_size,
        "requested_max_batches": args.max_batches,
        "available_batches": available_batches,
        "evaluated_batches": evaluated_batches,
        "evaluation_complete": evaluation_complete,
        "seed": args.seed,
        "device": device,
        "use_amp": args.use_amp,
        "camera_rename_map": rename_map,
        "empty_cameras": empty_cameras,
        "state_dim": state_dim,
        "action_dim": action_dim,
        "approved_inventory": admission["inventory_path"],
        "approved_episode_inventory_digest": split["approved_episode_inventory_digest"],
        "training_split": admission["split_path"],
        "split_digest": split["split_digest"],
        "training_receipt": admission["receipt_path"],
        "training_receipt_digest": admission["receipt_digest"],
        "split_verified": True,
        "loss_mean": statistics.fmean(losses),
        "loss_std": statistics.pstdev(losses),
        "loss_p95": ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)],
    }
    return report


def main() -> None:
    args = parse_args()
    admission = admit_evaluation(args)
    if args.dry_run:
        print(
            "PASS admitted held-out evaluation; inference not run and output not created: "
            f"split={admission['split']['split_digest']} episodes={admission['episodes']}"
        )
        return
    report = evaluate(args, admission)
    serialized = json.dumps(report, indent=2, sort_keys=True)
    print(serialized)
    if args.output:
        output = args.output.expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_output_path(output)
        stream = temporary.open("x", encoding="utf-8")
        try:
            with stream:
                stream.write(serialized + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            # Publish completely without replacing a concurrent writer's file.
            os.link(temporary, output)
        finally:
            temporary.unlink()


if __name__ == "__main__":
    main()
