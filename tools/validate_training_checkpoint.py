#!/usr/bin/env python3
"""Validate that a LeRobot checkpoint is complete and safe to resume."""

from __future__ import annotations

import argparse
from contextvars import ContextVar
import json
from pathlib import Path
import sys
from typing import Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


REQUIRED_TRAINING_STATE = (
    "optimizer_param_groups.json",
    "optimizer_state.safetensors",
    "rng_state.safetensors",
    "training_step.json",
)

_WARM_START_ANCESTORS = ContextVar("warm_start_ancestors", default=())

CONTINUATION_STATE = "fr5_continuation_state.json"


def advance_sample_cursor(cursor: dict, updates: int, batch_size: int) -> dict:
    """Committed map-style samples, including an uneven final batch; world size one."""
    import math

    size, offset, epoch = cursor["num_frames"], cursor["offset"], cursor["epoch"]
    if (any(type(v) is not int for v in (size, offset, epoch, updates, batch_size))
            or size < 1 or not 0 <= offset < size or min(epoch, updates) < 0 or batch_size < 1):
        raise ValueError("invalid continuation sample cursor")
    first = math.ceil((size - offset) / batch_size)
    if updates < first:
        offset += updates * batch_size
    else:
        epochs, remainder = divmod(updates - first, math.ceil(size / batch_size))
        epoch += 1 + epochs
        offset = remainder * batch_size
    return {"num_frames": size, "epoch": epoch, "offset": offset,
            "samples_consumed": epoch * size + offset}


def continuation_checkpoint_state(policy: Path, receipt: dict) -> dict:
    """Read committed state, or reconstruct only an unresumed native legacy stream."""
    from tools.fr5_data_factory import load_json_strict

    saved = load_json_strict(policy / "train_config.json")
    native = load_json_strict(policy.parent / "training_state/training_step.json")
    step, batch = native.get("step"), native.get("batch_size")
    if (type(step) is not int or not 0 < step <= saved["steps"]
            or type(batch) is not int or batch != saved["batch_size"]
            or type(native.get("num_processes")) is not int or native["num_processes"] != 1):
        raise ValueError("continuation requires native step/batch/world-size evidence")
    scheduler_state = load_json_strict(policy.parent / "training_state/scheduler_state.json")
    groups = json.loads((policy.parent / "training_state/optimizer_param_groups.json").read_text())
    if (not isinstance(groups, list) or not groups or any(not isinstance(group, dict) for group in groups)
            or type(scheduler_state.get("last_epoch")) is not int or scheduler_state["last_epoch"] != step
            or scheduler_state.get("_last_lr") != [group.get("lr") for group in groups]):
        raise ValueError("continuation optimizer/scheduler position differs from native step")
    if (saved["policy"].get("type") != "smolvla" or saved.get("num_workers") != 0
            or saved["policy"].get("use_amp") is not False
            or saved["policy"].get("compile_model", False)
            or saved["policy"].get("drop_n_last_frames", 0) != 0
            or saved["dataset"].get("streaming", False)
            or saved["dataset"].get("image_transforms", {}).get("enable", False)
            or saved.get("sample_weighting") is not None or saved.get("env") is not None
            or saved.get("reward_model") is not None or saved.get("peft") is not None
            or saved.get("scheduler", {}).get("type") != "cosine_decay_with_warmup"):
        raise ValueError("continuation supports only native SmolVLA world1/workers0/noAMP/deterministic transforms")
    size = receipt["normalization"]["stats"]["action"]["count"][0]
    if isinstance(size, bool) or not isinstance(size, (int, float)) or size < 1 or int(size) != size:
        raise ValueError("continuation requires exact TRAIN frame count")
    initial = receipt.get("initialization", {})
    if initial.get("mode") == "continuation":
        import copy
        from tools.data_factory.training_entrypoint import options

        config = options(receipt["normalized_argv"][1:])
        expected = load_json_strict(Path(initial["checkpoint"]) / "train_config.json")
        for key in ("steps", "batch_size", "eval_steps", "save_freq"):
            expected[key] = int(config[f"--{key}"])
        expected["output_dir"] = config["--output_dir"]
        expected["resume"] = True
        actual = copy.deepcopy(saved)
        # Native same-output recovery points at its own earlier checkpoint.
        source = Path(actual["policy"]["pretrained_path"]).resolve()
        source_output = source.parent.parent.parent
        if source != Path(initial["checkpoint"]) and source_output != policy.parent.parent.parent:
            raise ValueError("continuation saved policy source differs from lineage")
        actual["policy"]["pretrained_path"] = expected["policy"]["pretrained_path"]
        actual["checkpoint_path"] = expected.get("checkpoint_path")
        if actual != expected:
            raise ValueError("continuation saved recipe differs from inherited configuration")
        state = load_json_strict(policy.parent / "training_state" / CONTINUATION_STATE)
        import math
        expected_cursor = advance_sample_cursor(initial["cursor"], step - initial["step"], batch)
        if (set(state) != {"schema_version", "step", "cursor", "schedule", "python_gauss", "numpy_gauss"}
                or state["schema_version"] != "fr5-native-continuation-state-v1"
                or type(state["step"]) is not int or state["step"] != step
                or any(type(state["cursor"].get(key)) is not int for key in expected_cursor)
                or state["cursor"] != expected_cursor or state["cursor"]["num_frames"] != int(size)
                or state["schedule"] != initial["schedule"]
                or type(state["schedule"].get("native_horizon")) is not int
                or (state["schedule"].get("hold_from_step") is not None
                    and type(state["schedule"]["hold_from_step"]) is not int)
                or type(state["numpy_gauss"]) not in (int, float)
                or any(value is not None and (type(value) not in (int, float) or not math.isfinite(value))
                       for value in (state["python_gauss"], state["numpy_gauss"]))):
            raise ValueError("continuation checkpoint committed state differs from lineage")
        return state
    if saved.get("resume", False) or (policy.parent / "training_state" / CONTINUATION_STATE).exists():
        raise ValueError("legacy resumed sample history is not reconstructible")
    cursor = advance_sample_cursor({"num_frames": int(size), "epoch": 0, "offset": 0}, step, batch)
    return {"schema_version": "fr5-native-continuation-state-v1", "step": step, "cursor": cursor,
            "schedule": {"native_horizon": saved["steps"], "hold_from_step": None},
            "python_gauss": None, "numpy_gauss": None}


def continuation_argv(parent: Path, parent_receipt: dict, *, output: Path, steps: int,
                      batch_size: int, eval_steps: int, save_freq: int, schedule: str) -> list[str]:
    """Inherit all recipe/data settings; expose only the declared continuation choices."""
    from tools.data_factory.training_entrypoint import options

    config = options(parent_receipt["normalized_argv"][1:])
    config.update({"--policy.path": str(parent), "--output_dir": str(output), "--steps": str(steps),
                   "--batch_size": str(batch_size), "--eval_steps": str(eval_steps), "--save_freq": str(save_freq),
                   "--fr5.continue_from": str(parent), "--fr5.continuation_schedule": schedule})
    return [parent_receipt["normalized_argv"][0], *[f"{k}={v}" for k, v in config.items()]]


def continuation_binding(value: Path, split: dict, normalization: dict, argv: list[str]) -> dict:
    from tools.data_factory.training_entrypoint import options
    from tools.data_factory.training_receipts import tree_digest
    from tools.fr5_data_factory import canonical_digest, load_json_strict

    binding = warm_start_binding(value, split, normalization)
    parent = Path(binding["checkpoint"])
    output = parent.parent.parent.parent
    receipt_path = output / "fr5_training_receipt.json"
    # A continuation requires settled parent publication, unlike legacy inspection.
    parent_receipt = load_json_strict(receipt_path)
    state = continuation_checkpoint_state(parent, parent_receipt)
    if (tree_digest(parent.parent) != binding["checkpoint_artifact_digest"]
            or canonical_digest(parent_receipt) != binding["training_receipt_digest"]):
        raise ValueError("continuation parent changed during state binding")
    config = options(argv[1:])
    mode = config.get("--fr5.continuation_schedule")
    steps, batch, evaluation, save = (int(config[k]) for k in
                                     ("--steps", "--batch_size", "--eval_steps", "--save_freq"))
    destination = Path(config["--output_dir"]).expanduser().resolve()
    if (mode not in {"preserve", "hold"} or steps <= state["step"] or batch < 1
            or not 1 <= evaluation <= steps or not 1 <= save <= steps
            or destination.is_relative_to(output) or output.is_relative_to(destination)):
        raise ValueError("invalid continuation output, horizon or schedule")
    expected = continuation_argv(parent, parent_receipt, output=destination, steps=steps,
                                 batch_size=batch, eval_steps=evaluation, save_freq=save, schedule=mode)
    if options(expected[1:]) != config:
        raise ValueError("continuation must inherit the parent's complete recipe")
    schedule = dict(state["schedule"])
    if mode == "hold" and schedule["hold_from_step"] is None:
        schedule["hold_from_step"] = state["step"]
    return {**binding, "mode": "continuation", "reset": [], "step": state["step"],
            "cursor": state["cursor"], "schedule": schedule}


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


def validate_saved_observation_view(split: Mapping, receipt: Mapping) -> dict:
    """Return the one saved observation-view contract shared by Learning/Rollout.

    Curator remains the producer of publication, profile and transform facts.  This
    consumer only reuses that evidence and checks that every fitted frame belongs to
    this launch's TRAIN partition.  Raw checkpoints intentionally return a raw
    representation; they do not acquire derived-view semantics by inference.
    """
    from tools.data_factory.training_approval import (
        DERIVED_PROVENANCE_SCHEMA,
        EPISODE_PROVENANCE_SCHEMA,
        LEDGER_PROVENANCE_SCHEMA,
        MAPPED_PROVENANCE_SCHEMA,
        validate_current_training_inventory,
    )
    from tools.data_factory.curator.core.errors import CuratorError
    from tools.data_factory.training_split import validate_training_split
    from tools.fr5_data_factory import load_json_strict
    from tools.data_factory.curator.workflow.derivation import published_training_evidence
    from tools.data_factory.curator.profile.registry import resolve_view_profile
    from tools.data_factory.curator.profile.schema import load_view_profile
    from tools.data_factory.training_receipts import file_digest

    split = validate_training_split(split)
    if not isinstance(receipt, Mapping):
        raise ValueError("saved observation-view receipt is invalid")
    inventory_path = receipt.get("approved_inventory_path")
    if not isinstance(inventory_path, str):
        raise ValueError("saved observation-view inventory is missing")
    inventory = validate_current_training_inventory(
        inventory_path,
        dataset_root=split["dataset_identity"]["dataset_root"],
        repo_id=split["repo_id"],
        selected_episodes=split["selected_episodes"],
    )
    from tools.data_factory.training_split import source_episode_identity
    from tools.fr5_data_factory import canonical_digest

    derived = []
    train_origins, eval_origins = set(), set()
    mapped_view = False
    for episode in inventory["episodes"]:
        provenance = load_json_strict(Path(episode["episode_provenance"]["artifact_path"]))
        origin = canonical_digest(source_episode_identity(provenance))
        (train_origins if episode["episode_index"] in split["train_episodes"] else eval_origins).add(origin)
        application_dataset = split["dataset_identity"]
        if provenance.get("schema_version") == MAPPED_PROVENANCE_SCHEMA:
            mapped_view = True
            application_dataset = provenance["parent"]["dataset_identity"]
            provenance = provenance["parent"]["provenance"]
        if provenance.get("schema_version") == DERIVED_PROVENANCE_SCHEMA:
            derived.append((provenance["derivation"], application_dataset))
        elif provenance.get("schema_version") not in {EPISODE_PROVENANCE_SCHEMA, LEDGER_PROVENANCE_SCHEMA}:
            raise ValueError("saved observation-view provenance is unknown")
    if not derived:
        return {"representation": "raw", "transform_application": "none",
                "training_transform": "raw_once"}
    if len(derived) != len(inventory["episodes"]) or train_origins & eval_origins:
        raise ValueError("saved observation-view derivation is inconsistent across episodes")
    publications = {}
    for reference, application_dataset in derived:
        key = canonical_digest(reference)
        if key not in publications:
            try:
                publications[key] = published_training_evidence(reference)
            except (CuratorError, OSError, ValueError) as exc:
                raise ValueError("saved observation-view publication is invalid") from exc
        publication = publications[key]
        if any(application_dataset[k] != publication["output"][v] for k, v in
               (("dataset_root", "root"), ("repo_id", "repo_id"), ("dataset_digest", "dataset_digest"))):
            raise ValueError("saved observation-view dataset binding differs from launch")
    evidence = next(iter(publications.values()))
    if any(p["view_profile"] != evidence["view_profile"] or p["transform"] != evidence["transform"]
           for p in publications.values()):
        raise ValueError("saved observation-view derivation is inconsistent across episodes")
    output = evidence["output"]
    profile_path = Path(evidence["view_profile"]["path"])
    if file_digest(profile_path) != evidence["view_profile"]["file_sha256"]:
        raise ValueError("saved observation-view profile changed")
    spec = load_view_profile(profile_path)
    resolved = resolve_view_profile(
        profile_path.parent,
        spec.value["profile_id"],
        binding_root=spec.binding_path.parent,
        collection_profile_root=spec.collection_profile_path.parent,
    )
    if resolved.profile["profile_digest"] != evidence["view_profile"]["profile_digest"]:
        raise ValueError("saved observation-view assets differ from publication")
    fitting = spec.value.get("fitting")
    if spec.value.get("schema_version") != "curator.view_profile.v2" or not isinstance(fitting, dict):
        raise ValueError("saved observation-view lacks TRAIN fitting evidence")
    frames = [fitting.get("reference_frame"), *fitting.get("background_plate_frames", [])]
    if not frames or any(not isinstance(frame, dict) for frame in frames):
        raise ValueError("saved observation-view fitted frame is outside child TRAIN")
    fit_split_path = Path(fitting["training_split"]["path"])
    if file_digest(fit_split_path) != fitting["training_split"]["file_sha256"]:
        raise ValueError("saved observation-view fitting split changed")
    fit_split = validate_training_split(fit_split_path)
    if fit_split["split_digest"] != fitting["training_split"]["split_digest"]:
        raise ValueError("saved observation-view fitting split digest differs")
    fit_train = set(fit_split["train_episodes"])
    if any(frame["episode_index"] not in fit_train for frame in frames):
        raise ValueError("saved observation-view fitted frame is outside fitting TRAIN")
    for frame in frames:
        index = frame["episode_index"]
        origin = {"dataset_identity_digest": canonical_digest(fit_split["dataset_identity"]),
                  "episode_index": index,
                  "episode_content_digest": fit_split["episode_content_digests"][str(index)]}
        if "evaluation_cohort" in fit_split:
            origin = fit_split["evaluation_cohort"]["origins"][str(index)]
        if canonical_digest(origin) not in train_origins or canonical_digest(origin) in eval_origins:
            raise ValueError("saved observation-view fitted frame is outside child TRAIN")
    return {
        **({"fitting_dataset_identity": fit_split["dataset_identity"],
            "application_publications": [
                {"reference": next(ref for ref, _ in derived if canonical_digest(ref) == k),
                 "dataset": publications[k]["output"],
                 "parent_dataset_identity": publications[k]["parent_dataset_identity"],
                 "lineage_digest": publications[k]["lineage_digest"]}
                for k in sorted(publications)]}
           if mapped_view else {"parent_dataset_identity": evidence["parent_dataset_identity"],
                                "lineage_digest": evidence["lineage_digest"]}),
        "representation": "baked",
        "transform_application": "rollout_once",
        "training_transform": "baked_once",
        "dataset": ({**output, "root": split["dataset_identity"]["dataset_root"],
                     "repo_id": split["repo_id"], "dataset_digest": split["dataset_identity"]["dataset_digest"]}
                    if mapped_view else output),
        "view_profile": evidence["view_profile"],
        "transform": evidence["transform"],
    }


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
        if (root.expanduser().resolve() != Path(split["dataset_identity"]["dataset_root"]).expanduser().resolve()
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
            dataset=root.expanduser().resolve(), repo_id=dataset_cfg["repo_id"], inventory=Path(receipt["approved_inventory_path"]),
            profile=feature["profile"], collection_profile=feature["collection_profile_id"], argv=receipt["normalized_argv"],
        )
        # Receipts written before saved observation-view binding was introduced
        # are still valid for raw launches.  Compare their original canonical
        # shape while keeping the enriched binding mandatory for derived data.
        if ("observation_view" not in receipt
                and current_receipt.get("observation_view", {}).get("representation") == "raw"):
            current_receipt = dict(current_receipt)
            current_receipt.pop("observation_view")
            from tools.data_factory.training_receipts import launch_receipt_digest
            current_receipt["receipt_digest"] = launch_receipt_digest(current_receipt)
        if current_split != split or current_receipt != receipt:
            raise ValueError("dataset or launch provenance changed after training admission")
        # prepare_launch recompiles the complete receipt, including each parent.
        # Its exact comparison above supplies current validation without a second
        # recursive recompilation or any cross-call authority cache.
        if "initialization" in receipt and not config.get("resume", False):
            if config.get("policy", {}).get("pretrained_path") != receipt["initialization"]["checkpoint"]:
                raise ValueError("checkpoint warm-start parent differs from admitted initialization")
        validate_normalization_state(policy_dir, receipt["normalization"], profile=feature["profile"])
        if receipt.get("initialization", {}).get("mode") == "continuation":
            continuation_checkpoint_state(policy_dir, receipt)
        # Saved observation-view provenance is validated at the same boundary as
        # normalization and dataset lineage; consumers can call the public helper
        # to obtain the exact raw-versus-baked representation without reapplying a
        # Curator transform.
        validate_saved_observation_view(split, receipt)
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
