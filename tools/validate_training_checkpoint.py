#!/usr/bin/env python3
"""Validate that a LeRobot checkpoint is complete and safe to resume."""

from __future__ import annotations

import argparse
from contextvars import ContextVar
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


REQUIRED_TRAINING_STATE = (
    "optimizer_param_groups.json",
    "optimizer_state.safetensors",
    "rng_state.safetensors",
    "training_step.json",
)

_WARM_START_ANCESTORS = ContextVar("warm_start_ancestors", default=())


def warm_start_binding(value: Path, split: dict, normalization: dict) -> dict:
    """Validate a local parent for a fresh optimizer run on the same learning data."""
    from tools.data_factory.training_receipts import tree_digest
    from tools.fr5_data_factory import canonical_digest, load_json_strict

    policy_dir = normalize_policy_dir(value)
    ancestors = _WARM_START_ANCESTORS.get()
    if policy_dir in ancestors:
        raise ValueError("cyclic warm-start checkpoint lineage")
    token = _WARM_START_ANCESTORS.set((*ancestors, policy_dir))
    try:
        before = tree_digest(policy_dir.parent)
        policy_dir, output = validate_checkpoint(policy_dir)
        manifests = []
        for name in ("fr5_training_split.json", "fr5_training_receipt.json"):
            path = output / name
            if not path.is_file():
                path = Path(str(output) + f".{name}.pending")
            manifests.append(load_json_strict(path))
        parent_split, parent_receipt = manifests
        authority_fields = {"split_digest", "approved_episode_inventory_digest"}
        if ({key: val for key, val in parent_split.items() if key not in authority_fields}
                != {key: val for key, val in split.items() if key not in authority_fields}
                or parent_receipt["normalization"] != normalization):
            raise ValueError("warm-start parent must match dataset, partition, features and TRAIN normalization")
        if before != tree_digest(policy_dir.parent):
            raise ValueError("warm-start checkpoint changed during validation")
        return {"mode": "warm_start", "checkpoint": str(policy_dir),
                "checkpoint_artifact_digest": before,
                "training_receipt_digest": canonical_digest(parent_receipt),
                "split_digest": parent_split["split_digest"],
                "reset": ["optimizer", "scheduler", "rng", "sample_stream", "step"]}
    finally:
        _WARM_START_ANCESTORS.reset(token)


def normalize_policy_dir(value: Path) -> Path:
    path = value.expanduser().resolve()
    if path.is_file():
        path = path.parent
    if (path / "pretrained_model").is_dir():
        path = path / "pretrained_model"
    return path


def validate_normalization_state(policy_dir: Path, normalization: dict, *, profile: str = "smolvla") -> None:
    """Validate saved FR5 normalization semantics and tensors without model loading.

    Resume, offline evaluation and Rollout share this boundary through
    validate_checkpoint. Runtime-specific processor restrictions remain with
    the runtime consumer.
    """
    import numpy as np
    from safetensors.numpy import load_file
    from tools.fr5_training_profile import PROFILE_NAMES

    if profile not in PROFILE_NAMES:
        raise ValueError("checkpoint normalization profile is unknown")
    mode = "MIN_MAX" if profile.startswith("vqbet-") else "MEAN_STD"

    expected = {f"{key}.{name}": np.asarray(value, dtype=np.float32)
                for key, stats in normalization["stats"].items() for name, value in stats.items()}
    for pipeline, registry in (("policy_preprocessor", "normalizer_processor"),
                               ("policy_postprocessor", "unnormalizer_processor")):
        config = json.loads((policy_dir / f"{pipeline}.json").read_text())
        steps = [step for step in config["steps"] if step.get("registry_name") == registry]
        if len(steps) != 1:
            raise ValueError("checkpoint requires one saved normalization processor")
        step = steps[0]
        processor = step.get("config")
        if "class" in step or not isinstance(processor, dict):
            raise ValueError("checkpoint normalization processor config is invalid")
        features = processor.get("features")
        norm_map = processor.get("norm_map")
        required = {"action": "ACTION"}
        if registry == "normalizer_processor":
            required["observation.state"] = "STATE"
            selected = processor.get("normalize_observation_keys")
            if selected is not None and (
                not isinstance(selected, list) or not all(isinstance(key, str) for key in selected)
                or "observation.state" not in selected
            ):
                raise ValueError("checkpoint normalization excludes observation.state")
        for key, feature_type in required.items():
            if not isinstance(features, dict) or features.get(key) != {"type": feature_type, "shape": [7]}:
                raise ValueError("checkpoint normalization feature differs from FR5 contract")
            if not isinstance(norm_map, dict) or norm_map.get(feature_type) != mode:
                raise ValueError("checkpoint normalization mode differs from admitted profile")
        # LeRobot constructor statistics override load_state_dict, even when
        # the saved tensor file itself exactly matches the admitted receipt.
        if processor.get("stats"):
            raise ValueError("checkpoint normalization has inline statistics overriding saved state")
        state_name = step.get("state_file")
        if not isinstance(state_name, str) or Path(state_name).name != state_name:
            raise ValueError("checkpoint normalization state must be a local file")
        state_path = policy_dir / state_name
        if state_path.is_symlink():
            raise ValueError("checkpoint normalization state must be a local file")
        actual = load_file(state_path)
        if set(actual) != set(expected) or any(
            not np.array_equal(actual[key], value) for key, value in expected.items()
        ):
            raise ValueError("checkpoint normalization differs from admitted TRAIN statistics")


def validate_policy_feature_contract(policy: dict, feature: dict) -> None:
    """Match saved policy features, including SmolVLA's inert native image slots."""
    from tools.data_factory.training_entrypoint import options

    for option, expected in options(feature["policy_argv"]).items():
        if not option.startswith("--policy.") or option == "--policy.path":
            continue
        key = option.removeprefix("--policy.")
        try:
            expected = json.loads(expected)
        except json.JSONDecodeError:
            pass
        actual = policy.get(key)
        if key == "input_features" and isinstance(actual, dict) and isinstance(expected, dict):
            # The pretrained parser retains camera3; native validate_features adds
            # empty_camera_0. Neither is a dataset input. prepare_images emits at
            # most empty_cameras masked blanks, independent of missing-slot names.
            extras = {}
            if (feature["profile"] == "smolvla" and policy.get("empty_cameras") == 1
                    and {name for name, value in expected.items() if value.get("type") == "VISUAL"}
                    == {"observation.images.camera1", "observation.images.camera2"}):
                extras = {
                    "observation.images.camera3": {"type": "VISUAL", "shape": [3, 256, 256]},
                    "observation.images.empty_camera_0": {"type": "VISUAL", "shape": [3, 480, 640]},
                }
            if any(name not in extras or value != extras[name]
                   for name, value in actual.items() if name not in expected):
                raise ValueError("checkpoint policy has unadmitted image/input features")
            actual = {name: value for name, value in actual.items() if name in expected}
            if actual != expected:
                raise ValueError("checkpoint policy differs from admitted feature contract")
            if ([name for name in actual if actual[name].get("type") == "VISUAL"]
                    != [name for name in expected if expected[name].get("type") == "VISUAL"]):
                raise ValueError("checkpoint policy camera order differs from admitted feature contract")
        if actual != expected:
            raise ValueError("checkpoint policy differs from admitted feature contract")


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
    if verify_dataset:
        from tools.data_factory.training_entrypoint import prepare_launch, options
        from tools.data_factory.training_split import validate_training_split

        split = validate_training_split(split)
        if split["schema_version"] != 3:
            raise ValueError("legacy split is not strong lineage proof; resume requires a current launch receipt")
        receipt_path = output_dir / "fr5_training_receipt.json"
        if not receipt_path.is_file():
            receipt_path = Path(str(output_dir) + ".fr5_training_receipt.json.pending")
        receipt = json.loads(receipt_path.read_text())
        root = Path(dataset_cfg.get("root", "")).expanduser()
        if (str(root) != split["dataset_identity"]["dataset_root"]
                or dataset_cfg.get("repo_id") != split["dataset_identity"]["repo_id"]
                or dataset_cfg.get("eval_split") != split["eval_split"]
                or (dataset_cfg.get("episodes") or list(range(split["total_episodes"]))) != split["selected_episodes"]):
            raise ValueError("checkpoint dataset selection differs from admitted launch")
        feature = split["feature_contract"]
        validate_policy_feature_contract(config.get("policy", {}), feature)
        validate_policy_feature_contract(json.loads((policy_dir / "config.json").read_text()), feature)
        expected_policy = options(feature["policy_argv"])
        for option, expected in expected_policy.items():
            if option == "--rename_map" and config.get("rename_map", {}) != json.loads(expected):
                raise ValueError("checkpoint camera mapping differs from admitted feature contract")
        current_split, current_receipt = prepare_launch(
            dataset=root, repo_id=dataset_cfg["repo_id"], inventory=Path(receipt["approved_inventory_path"]),
            profile=feature["profile"], collection_profile=feature["collection_profile_id"], argv=receipt["normalized_argv"],
        )
        if current_split != split or current_receipt != receipt:
            raise ValueError("dataset or launch provenance changed after training admission")
        # prepare_launch recompiles the complete receipt, including each parent.
        # Its exact comparison above supplies current validation without a second
        # recursive recompilation or any cross-call authority cache.
        if "initialization" in receipt and not config.get("resume", False):
            if config.get("policy", {}).get("pretrained_path") != receipt["initialization"]["checkpoint"]:
                raise ValueError("checkpoint warm-start parent differs from admitted initialization")
        validate_normalization_state(policy_dir, receipt["normalization"], profile=feature["profile"])
        return policy_dir, output_dir

    # Historical inspection only; this never grants permission to resume.
    if split.get("repo_id") != dataset_cfg.get("repo_id"):
        raise ValueError("checkpoint dataset repo_id differs from fr5_training_split.json")
    if split.get("eval_split") != dataset_cfg.get("eval_split"):
        raise ValueError("checkpoint eval_split differs from fr5_training_split.json")

    return policy_dir, output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--shell", action="store_true", help="Print NUL-delimited policy and output paths")
    parser.add_argument("--json", action="store_true", help="Print policy and output paths as a JSON array")
    args = parser.parse_args()
    try:
        policy_dir, output_dir = validate_checkpoint(args.checkpoint)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps([str(policy_dir), str(output_dir)]))
    elif args.shell:
        for path in (policy_dir, output_dir):
            sys.stdout.buffer.write(str(path).encode() + b"\0")
    else:
        print(f"PASS checkpoint={policy_dir} output={output_dir}")


if __name__ == "__main__":
    main()
