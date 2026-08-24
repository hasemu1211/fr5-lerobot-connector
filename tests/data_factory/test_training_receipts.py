"""Synthetic tests for strict training and independent reload receipts."""

import copy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.data_factory.training_receipts import (
    FEATURE_BINDING,
    FEATURE_DIGEST,
    RELOAD_RECEIPT_SCHEMA,
    TRAINING_RECEIPT_SCHEMA,
    ReceiptError,
    canonical_digest,
    feature_binding,
    file_digest,
    tree_digest,
    validate_reload_receipt,
    validate_training_receipt,
)


def digest(character: str) -> str:
    return "sha256:" + character * 64


def training_receipt() -> dict:
    argv = ["lerobot-train", "--policy.type=smolvla", "--seed=17"]
    runtime = {
        "python_version": "3.12.3",
        "lerobot_version": "0.6.1",
        "lerobot_source_digest": digest("1"),
        "torch_version": "2.7.1",
        "torch_source_digest": digest("2"),
        "cuda_version": "12.8",
        "cuda_source_digest": digest("3"),
    }
    return {
        "schema_version": TRAINING_RECEIPT_SCHEMA,
        "receipt_id": "train-receipt-1",
        "process_id": "train-process-100",
        "session_id": "train-session-a",
        "dataset_id": "local/fr5-approved-v2",
        "dataset_digest": digest("4"),
        "repository_commit": "a" * 40,
        "source_digest": digest("5"),
        "profile_id": "smolvla-fr5-up-side-v1",
        "profile_digest": digest("6"),
        "collection_profile_digest": digest("c"),
        "normalized_argv": argv,
        "argv_digest": canonical_digest(argv),
        "config_digest": digest("7"),
        "runtime_versions": runtime,
        "runtime_digest": canonical_digest(runtime),
        "approved_episode_inventory_digest": digest("8"),
        "episode_manifest_digest": digest("9"),
        "split_digest": digest("a"),
        "training_seed": 17,
        "feature_binding": feature_binding(),
        "feature_digest": FEATURE_DIGEST,
        "checkpoint_id": "checkpoints/000100",
        "checkpoint_tree_digest": digest("b"),
        "status": "PASS",
    }


def reload_receipt(train: dict) -> dict:
    argv = ["python3", "-m", "tools.reload_smolvla", "--checkpoint=checkpoints/000100"]
    return {
        "schema_version": RELOAD_RECEIPT_SCHEMA,
        "reload_receipt_id": "reload-receipt-1",
        "train_receipt_id": train["receipt_id"],
        "train_receipt_digest": canonical_digest(train),
        "train_process_id": train["process_id"],
        "train_session_id": train["session_id"],
        "reload_process_id": "reload-process-200",
        "reload_session_id": "reload-session-b",
        "repository_commit": train["repository_commit"],
        "source_digest": train["source_digest"],
        "profile_id": train["profile_id"],
        "profile_digest": train["profile_digest"],
        "collection_profile_digest": train["collection_profile_digest"],
        "normalized_argv": argv,
        "argv_digest": canonical_digest(argv),
        "runtime_versions": copy.deepcopy(train["runtime_versions"]),
        "checkpoint_id": train["checkpoint_id"],
        "checkpoint_tree_digest": train["checkpoint_tree_digest"],
        "split_digest": train["split_digest"],
        "runtime_digest": train["runtime_digest"],
        "feature_digest": train["feature_digest"],
        "reload_status": "PASS",
        "task_success_claimed": False,
    }


class TrainingReceiptTest(unittest.TestCase):
    def test_exact_training_receipt_binds_all_provenance(self):
        receipt = training_receipt()
        self.assertEqual(validate_training_receipt(receipt), receipt)
        self.assertEqual(
            validate_training_receipt(receipt, expected={"dataset_digest": digest("4")}), receipt
        )
        with self.assertRaisesRegex(ReceiptError, "PROVENANCE_MISMATCH"):
            validate_training_receipt(receipt, expected={"dataset_digest": digest("f")})

    def test_training_receipt_rejects_schema_digest_status_and_nonfinite_changes(self):
        for mutate in (
            lambda value: value.update(extra=True),
            lambda value: value.pop("split_digest"),
            lambda value: value.update(status="FAIL"),
            lambda value: value.update(repository_commit="not-a-commit"),
            lambda value: value.update(training_seed=float("nan")),
            lambda value: value.update(argv_digest=digest("f")),
            lambda value: value["runtime_versions"].update(torch_version="changed"),
            lambda value: value.update(checkpoint_tree_digest="sha256:short"),
        ):
            receipt = training_receipt()
            mutate(receipt)
            with self.assertRaises(ReceiptError):
                validate_training_receipt(receipt)
        with self.assertRaises(ReceiptError):
            canonical_digest({"value": float("inf")})

    def test_exact_feature_binding_rejects_synthetic_contract_mismatch(self):
        features = FEATURE_BINDING["features"]
        self.assertEqual(features["observation.state"], {"dtype": "float32", "shape": [7]})
        self.assertEqual(features["action"], {"dtype": "float32", "shape": [7]})
        self.assertEqual(
            features["observation.images.up"]["policy_key"], "observation.images.camera1"
        )
        self.assertEqual(
            features["observation.images.side"]["policy_key"], "observation.images.camera2"
        )
        for key, value in (("collection_profile_id", "other-profile"), ("camera_profile", "up")):
            receipt = training_receipt()
            receipt["feature_binding"][key] = value
            with self.assertRaises(ReceiptError):
                validate_training_receipt(receipt)
        receipt = training_receipt()
        receipt["feature_binding"]["features"]["action"]["shape"] = [6]
        with self.assertRaises(ReceiptError):
            validate_training_receipt(receipt)
        receipt = training_receipt()
        receipt["feature_binding"]["features"]["observation.images.side"]["policy_key"] = "observation.images.camera1"
        with self.assertRaises(ReceiptError):
            validate_training_receipt(receipt)

    def test_independent_reload_pass_does_not_claim_task_success(self):
        train = training_receipt()
        reload = reload_receipt(train)
        self.assertEqual(validate_reload_receipt(reload, train), reload)
        self.assertFalse(reload["task_success_claimed"])

    def test_reload_rejects_provenance_status_and_independence_mismatch(self):
        train = training_receipt()
        changes = (
            ("train_receipt_digest", digest("f")),
            ("checkpoint_tree_digest", digest("f")),
            ("checkpoint_id", "another-checkpoint"),
            ("split_digest", digest("f")),
            ("runtime_digest", digest("f")),
            ("feature_digest", digest("f")),
            ("repository_commit", "b" * 40),
            ("source_digest", digest("f")),
            ("profile_id", "other-profile"),
            ("profile_digest", digest("f")),
            ("collection_profile_digest", digest("f")),
            ("argv_digest", digest("f")),
            ("train_process_id", "other-train-process"),
            ("train_session_id", "other-train-session"),
            ("reload_process_id", train["process_id"]),
            ("reload_session_id", train["session_id"]),
            ("reload_receipt_id", train["receipt_id"]),
            ("reload_status", "FAIL"),
            ("task_success_claimed", True),
        )
        for key, value in changes:
            receipt = reload_receipt(train)
            receipt[key] = value
            with self.subTest(key=key), self.assertRaises(ReceiptError):
                validate_reload_receipt(receipt, train)
        receipt = reload_receipt(train)
        receipt["extra"] = True
        with self.assertRaises(ReceiptError):
            validate_reload_receipt(receipt, train)

    def test_file_and_tree_digests_use_only_temporary_fake_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            first = root / "model.fake"
            second = nested / "config.fake"
            first.write_bytes(b"fake weights")
            second.write_bytes(b"fake config")
            expected = "sha256:" + hashlib.sha256(b"fake weights").hexdigest()
            self.assertEqual(file_digest(first), expected)
            before = tree_digest(root)
            self.assertEqual(before, tree_digest(root))
            second.write_bytes(b"changed fake config")
            self.assertNotEqual(before, tree_digest(root))
            with self.assertRaises(ReceiptError):
                file_digest(root)


if __name__ == "__main__":
    unittest.main()
