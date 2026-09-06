"""Strict, side-effect-free provenance receipts for an FR5 training checkpoint."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping


TRAINING_RECEIPT_SCHEMA = "data_factory.training_checkpoint_receipt.v1"
RELOAD_RECEIPT_SCHEMA = "data_factory.independent_reload_receipt.v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_TRAINING_KEYS = {
    "schema_version", "receipt_id", "process_id", "session_id", "dataset_id",
    "dataset_digest", "repository_commit", "source_digest", "profile_id",
    "profile_digest", "collection_profile_digest", "normalized_argv", "argv_digest", "config_digest",
    "runtime_versions", "runtime_digest", "approved_episode_inventory_digest",
    "episode_manifest_digest", "split_digest", "training_seed", "feature_binding",
    "feature_digest", "checkpoint_id", "checkpoint_tree_digest", "status",
}
_RELOAD_KEYS = {
    "schema_version", "reload_receipt_id", "train_receipt_id", "train_receipt_digest",
    "train_process_id", "train_session_id", "reload_process_id", "reload_session_id",
    "repository_commit", "source_digest", "profile_id", "profile_digest",
    "collection_profile_digest", "normalized_argv", "argv_digest",
    "runtime_versions", "runtime_digest",
    "checkpoint_id", "checkpoint_tree_digest", "split_digest",
    "feature_digest", "reload_status", "task_success_claimed",
}
_RUNTIME_KEYS = {
    "python_version", "lerobot_version", "lerobot_source_digest", "torch_version",
    "torch_source_digest", "cuda_version", "cuda_source_digest",
}

FEATURE_BINDING = {
    "collection_profile_id": "fr5-dual-rgb-30hz-v1",
    "camera_profile": "up-side",
    "features": {
        "observation.state": {"dtype": "float32", "shape": [7]},
        "action": {"dtype": "float32", "shape": [7]},
        "observation.images.up": {
            "color_space": "RGB",
            "policy_key": "observation.images.camera1",
        },
        "observation.images.side": {
            "color_space": "RGB",
            "policy_key": "observation.images.camera2",
        },
    },
}


class ReceiptError(ValueError):
    """A receipt is malformed or does not match its claimed provenance."""


def canonical_digest(value: object) -> str:
    """Digest a JSON value without accepting NaN or Infinity."""
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ReceiptError("NON_CANONICAL_VALUE") from error
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def launch_receipt_digest(value: Mapping) -> str:
    """Digest a launch receipt body without recursively hashing its digest."""
    return canonical_digest({key: item for key, item in value.items() if key != "receipt_digest"})


def file_digest(path: str | Path) -> str:
    """Digest exactly one caller-supplied regular file."""
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ReceiptError("FILE_PATH")
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256")
    return "sha256:" + digest.hexdigest()


def tree_digest(path: str | Path) -> str:
    """Digest caller-supplied tree names and file digests in canonical order."""
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise ReceiptError("TREE_PATH")
    entries = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    if any(item.is_symlink() or not (item.is_dir() or item.is_file()) for item in entries):
        raise ReceiptError("TREE_PATH")
    digest = hashlib.sha256()
    for item in entries:
        if not item.is_file():
            continue
        relative = item.relative_to(root).as_posix().encode("utf-8")
        content = file_digest(item).encode("ascii")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def normalize_argv(argv: object) -> list[str]:
    """Return the only accepted receipt representation of a command line."""
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ReceiptError("ARGV")
    result = []
    for item in argv:
        if not isinstance(item, str) or not item or item != item.strip() or "\x00" in item:
            raise ReceiptError("ARGV")
        result.append(item)
    return result


def feature_binding() -> dict:
    return copy.deepcopy(FEATURE_BINDING)


FEATURE_DIGEST = canonical_digest(FEATURE_BINDING)


def _exact(value: object, keys: set[str], code: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ReceiptError(code)
    return value


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ReceiptError(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ReceiptError(code)
    return value


def _runtime(value: object) -> dict:
    runtime = _exact(value, _RUNTIME_KEYS, "RUNTIME")
    for key in ("python_version", "lerobot_version", "torch_version", "cuda_version"):
        _text(runtime[key], "RUNTIME")
    for key in ("lerobot_source_digest", "torch_source_digest", "cuda_source_digest"):
        _digest(runtime[key], "RUNTIME")
    return runtime


def validate_feature_binding(value: object) -> dict:
    if value != FEATURE_BINDING or canonical_digest(value) != FEATURE_DIGEST:
        raise ReceiptError("FEATURE_BINDING")
    return copy.deepcopy(FEATURE_BINDING)


def validate_training_receipt(
    value: object, *, expected: Mapping[str, object] | None = None
) -> dict:
    """Validate an exact training/checkpoint receipt and its derived digests."""
    receipt = _exact(value, _TRAINING_KEYS, "TRAINING_RECEIPT_SCHEMA")
    if receipt["schema_version"] != TRAINING_RECEIPT_SCHEMA or receipt["status"] != "PASS":
        raise ReceiptError("TRAINING_RECEIPT_STATUS")
    for key in (
        "receipt_id", "process_id", "session_id", "dataset_id", "profile_id", "checkpoint_id"
    ):
        _text(receipt[key], "TRAINING_RECEIPT_IDENTITY")
    if not isinstance(receipt["repository_commit"], str) or not _COMMIT.fullmatch(
        receipt["repository_commit"]
    ):
        raise ReceiptError("REPOSITORY_COMMIT")
    for key in (
        "dataset_digest", "source_digest", "profile_digest", "collection_profile_digest",
        "argv_digest", "config_digest",
        "runtime_digest", "approved_episode_inventory_digest", "episode_manifest_digest",
        "split_digest", "feature_digest", "checkpoint_tree_digest",
    ):
        _digest(receipt[key], "TRAINING_RECEIPT_DIGEST")
    argv = normalize_argv(receipt["normalized_argv"])
    if canonical_digest(argv) != receipt["argv_digest"]:
        raise ReceiptError("ARGV_DIGEST")
    runtime = _runtime(receipt["runtime_versions"])
    if canonical_digest(runtime) != receipt["runtime_digest"]:
        raise ReceiptError("RUNTIME_DIGEST")
    validate_feature_binding(receipt["feature_binding"])
    if receipt["feature_digest"] != FEATURE_DIGEST:
        raise ReceiptError("FEATURE_DIGEST")
    seed = receipt["training_seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ReceiptError("TRAINING_SEED")
    if expected is not None:
        if not isinstance(expected, Mapping) or not set(expected) <= _TRAINING_KEYS:
            raise ReceiptError("EXPECTED_PROVENANCE")
        if any(receipt[key] != expected_value for key, expected_value in expected.items()):
            raise ReceiptError("PROVENANCE_MISMATCH")
    return copy.deepcopy(receipt)


def validate_reload_receipt(value: object, train_receipt: object) -> dict:
    """Validate an independently produced reload PASS against one train receipt."""
    train = validate_training_receipt(train_receipt)
    receipt = _exact(value, _RELOAD_KEYS, "RELOAD_RECEIPT_SCHEMA")
    if (
        receipt["schema_version"] != RELOAD_RECEIPT_SCHEMA
        or receipt["reload_status"] != "PASS"
        or receipt["task_success_claimed"] is not False
    ):
        raise ReceiptError("RELOAD_STATUS")
    for key in (
        "reload_receipt_id", "train_receipt_id", "train_process_id", "train_session_id",
        "reload_process_id", "reload_session_id", "profile_id", "checkpoint_id",
    ):
        _text(receipt[key], "RELOAD_IDENTITY")
    if not isinstance(receipt["repository_commit"], str) or not _COMMIT.fullmatch(
        receipt["repository_commit"]
    ):
        raise ReceiptError("RELOAD_REPOSITORY_COMMIT")
    for key in (
        "train_receipt_digest", "source_digest", "profile_digest",
        "collection_profile_digest", "argv_digest", "checkpoint_tree_digest",
        "split_digest", "runtime_digest", "feature_digest",
    ):
        _digest(receipt[key], "RELOAD_DIGEST")
    argv = normalize_argv(receipt["normalized_argv"])
    if canonical_digest(argv) != receipt["argv_digest"]:
        raise ReceiptError("RELOAD_ARGV_DIGEST")
    runtime = _runtime(receipt["runtime_versions"])
    if canonical_digest(runtime) != receipt["runtime_digest"]:
        raise ReceiptError("RELOAD_RUNTIME_DIGEST")
    expected = {
        "train_receipt_id": train["receipt_id"],
        "train_receipt_digest": canonical_digest(train),
        "train_process_id": train["process_id"],
        "train_session_id": train["session_id"],
        "repository_commit": train["repository_commit"],
        "source_digest": train["source_digest"],
        "profile_id": train["profile_id"],
        "profile_digest": train["profile_digest"],
        "collection_profile_digest": train["collection_profile_digest"],
        "checkpoint_id": train["checkpoint_id"],
        "checkpoint_tree_digest": train["checkpoint_tree_digest"],
        "split_digest": train["split_digest"],
        "runtime_digest": train["runtime_digest"],
        "feature_digest": train["feature_digest"],
    }
    if any(receipt[key] != expected_value for key, expected_value in expected.items()):
        raise ReceiptError("RELOAD_PROVENANCE")
    if (
        receipt["reload_receipt_id"] == train["receipt_id"]
        or receipt["reload_process_id"] == train["process_id"]
        or receipt["reload_session_id"] == train["session_id"]
    ):
        raise ReceiptError("RELOAD_NOT_INDEPENDENT")
    return copy.deepcopy(receipt)


def compile_launch_receipt(split: Mapping, argv: list[str], inventory_path: str) -> dict:
    """Record admitted launch inputs; never claim checkpoint/reload or training PASS."""
    from tools.data_factory.training_split import validate_training_split
    from tools.data_factory.training_entrypoint import options
    from tools.fr5_training_profile import training_normalization

    split = validate_training_split(split)
    if split["schema_version"] != 3:
        raise ReceiptError("LAUNCH_SPLIT_REQUIRED")
    argv = normalize_argv(argv)
    imagenet = options(argv[1:]).get("--dataset.use_imagenet_stats", "true")
    if imagenet not in {"true", "false"}:
        raise ReceiptError("TRAINING_NORMALIZATION_OPTION")
    value = {
        "schema_version": "data_factory.training_launch_receipt.v2",
        "status": "ADMITTED_NOT_TRAINED",
        "approved_inventory_path": str(Path(inventory_path).resolve()),
        "approved_episode_inventory_digest": split["approved_episode_inventory_digest"],
        "dataset_identity": split["dataset_identity"],
        "split_digest": split["split_digest"],
        "selected_episodes": split["selected_episodes"],
        "train_episodes": split["train_episodes"], "eval_episodes": split["eval_episodes"],
        "feature_contract": split["feature_contract"],
        "normalization": training_normalization(split, use_imagenet_stats=imagenet == "true"),
        "normalized_argv": argv, "argv_digest": canonical_digest(argv),
    }
    source = options(argv[1:]).get("--policy.path")
    base = options(split["feature_contract"]["policy_argv"]).get("--policy.path")
    if source != base:
        from tools.validate_training_checkpoint import warm_start_binding

        if split["feature_contract"]["profile"] != "smolvla" or not source:
            raise ReceiptError("TRAINING_WARM_START_PROFILE")
        value["initialization"] = warm_start_binding(Path(source), split, value["normalization"])
    return {**value, "receipt_digest": launch_receipt_digest(value)}


def validate_launch_receipt(value: Mapping, split: Mapping) -> dict:
    expected = compile_launch_receipt(split, value["normalized_argv"], value["approved_inventory_path"])
    if "observation_view" in value:
        try:
            from tools.validate_training_checkpoint import validate_saved_observation_view
            expected["observation_view"] = validate_saved_observation_view(split, expected)
        except (OSError, TypeError, ValueError, KeyError) as exc:
            raise ReceiptError("OBSERVATION_VIEW_BINDING") from exc
        expected["receipt_digest"] = launch_receipt_digest(expected)
    if value != expected:
        raise ReceiptError("LAUNCH_RECEIPT_BINDING")
    return expected
