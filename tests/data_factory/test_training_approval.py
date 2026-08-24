import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.data_factory import training_approval
from tools.data_factory.training_approval import (
    APPROVAL_SCHEMA,
    INVENTORY_SCHEMA,
    PRODUCTION_SCOPE,
    PROVENANCE,
    SYNTHETIC_SCOPE,
    build_training_approved_inventory,
    issue_training_approval,
    validate_training_approved_inventory,
    write_training_approved_inventory,
)
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
D3 = "sha256:" + "3" * 64


class FakeTTY(io.StringIO):
    def __init__(self, value="", *, tty=True):
        super().__init__(value)
        self.tty = tty

    def isatty(self):
        return self.tty

    def __exit__(self, *_args):
        return None


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    return str(path), canonical_digest(value)


def synthetic_fixture(root, episode_id="episode-1", episode_index=0):
    dataset_root = root / "SYNTHETIC_TEST_ONLY_dataset"
    dataset_root.mkdir(exist_ok=True)
    (dataset_root / "unchanged.marker").write_text("synthetic fixture\n", encoding="utf-8")
    run_state = root / "SYNTHETIC_TEST_ONLY_run_state"
    run_state.mkdir(exist_ok=True)
    (run_state / "unchanged.marker").write_text("synthetic fixture\n", encoding="utf-8")
    dataset = {
        "dataset_id": "synthetic-dataset-r1",
        "repo_id": "tests/synthetic-dataset",
        "dataset_root": str(dataset_root),
        "dataset_digest": D1,
    }
    technical = {
        "schema_version": "data_factory.technical_validator_result.v1",
        "run_id": episode_id,
        "resolved_job_digest": D1,
        "plan_digest": D2,
        "dataset_root": str(dataset_root),
        "expected_fps": 30,
        "status": "PASS",
        "result_digest": D3,
    }
    technical_path, technical_digest = write_json(root / f"{episode_id}.technical.SYNTHETIC_TEST_ONLY.json", technical)
    semantic = {
        "schema_version": "data_factory.candidate_admission.v1",
        "run_id": episode_id,
        "operational_gate": "PASS",
        "operational_source": "HUMAN_GATED",
        "checklist_id": "pickup-v2",
        "review_context_digest": canonical_digest({
            "run_id": episode_id,
            "resolved_job_digest": technical["resolved_job_digest"],
            "plan_digest": technical["plan_digest"],
            "technical_validator_digest": technical_digest,
        }),
        "semantic_status": "PASS",
        "reviewed_by": "synthetic-reviewer-1",
        "reviewed_at": "2026-08-24T00:00:00Z",
        "reason": None,
    }
    semantic_path, semantic_digest = write_json(root / f"{episode_id}.semantic.SYNTHETIC_TEST_ONLY.json", semantic)
    approval = {
        "schema_version": APPROVAL_SCHEMA,
        "scope": SYNTHETIC_SCOPE,
        "dataset_identity": dataset,
        "episode_id": episode_id,
        "episode_index": episode_index,
        "episode_content_digest": D2,
        "technical_validator_digest": technical_digest,
        "human_semantic_evidence_digest": semantic_digest,
        "approved_by": "synthetic-approver-1",
        "approved_at": "2026-08-24T00:01:00Z",
        "provenance": PROVENANCE,
    }
    approval_path, approval_digest = write_json(root / f"{episode_id}.approval.SYNTHETIC_TEST_ONLY.json", approval)
    entry = {
        "dataset_identity_digest": canonical_digest(dataset),
        "episode_id": episode_id,
        "episode_index": episode_index,
        "episode_content_digest": D2,
        "technical_validator": {"artifact_path": technical_path, "artifact_digest": technical_digest, "status": "PASS"},
        "human_semantic_evidence": {
            "artifact_path": semantic_path,
            "artifact_digest": semantic_digest,
            "status": "PASS",
            "reviewer_id": "synthetic-reviewer-1",
        },
        "training_approval": {
            "artifact_path": approval_path,
            "artifact_digest": approval_digest,
            "provenance": PROVENANCE,
        },
    }
    return dataset, technical, semantic, approval, entry


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class TrainingApprovalTest(unittest.TestCase):
    def test_synthetic_inventory_is_canonical_strict_and_immutable(self):
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            dataset, _, _, _, first = synthetic_fixture(root, "episode-1", 1)
            _, _, _, _, second = synthetic_fixture(root, "episode-0", 0)
            inventory = build_training_approved_inventory(
                scope=SYNTHETIC_SCOPE, dataset_identity=dataset, episodes=[first, second],
            )
            self.assertEqual(inventory["schema_version"], INVENTORY_SCHEMA)
            self.assertEqual([item["episode_index"] for item in inventory["episodes"]], [0, 1])
            self.assertEqual(
                inventory["inventory_digest"],
                canonical_digest({key: inventory[key] for key in ("schema_version", "scope", "dataset_identity", "episodes")}),
            )
            self.assertEqual(
                validate_training_approved_inventory(inventory, expected_scope=SYNTHETIC_SCOPE), inventory,
            )
            target = root / "training_approved_inventory.SYNTHETIC_TEST_ONLY.json"
            self.assertEqual(
                write_training_approved_inventory(target, inventory, expected_scope=SYNTHETIC_SCOPE), target,
            )
            self.assertEqual(load_json_strict(target), inventory)
            original = target.read_bytes()
            with self.assertRaisesRegex(ContractError, "TRAINING_INVENTORY_EXISTS"):
                write_training_approved_inventory(target, inventory, expected_scope=SYNTHETIC_SCOPE)
            self.assertEqual(target.read_bytes(), original)

    def test_exact_keys_and_nonfinite_fail_before_output(self):
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            dataset, _, _, _, entry = synthetic_fixture(root)
            inventory = build_training_approved_inventory(scope=SYNTHETIC_SCOPE, dataset_identity=dataset, episodes=[entry])
            target = root / "never-written.json"
            before = snapshot(root)
            for forged in (
                {**inventory, "unknown": True},
                {**inventory, "episodes": [{**inventory["episodes"][0], "episode_index": float("nan")}]},
            ):
                with self.subTest(forged=tuple(forged)):
                    with self.assertRaises(ContractError):
                        write_training_approved_inventory(target, forged, expected_scope=SYNTHETIC_SCOPE)
                    self.assertFalse(target.exists())
                    self.assertEqual(snapshot(root), before)

    def test_evidence_failures_are_fail_closed_without_side_effects(self):
        cases = (
            ("malformed", lambda technical, semantic: semantic.update(extra=True)),
            ("technical_non_pass", lambda technical, semantic: technical.update(status="FAIL")),
            ("semantic_non_pass", lambda technical, semantic: semantic.update(semantic_status="FAIL", reason="TASK_GOAL")),
            ("placeholder_reviewer", lambda technical, semantic: semantic.update(reviewed_by="HUMAN")),
        )
        for name, mutate in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
                root = Path(directory)
                dataset, technical, semantic, _, entry = synthetic_fixture(root)
                mutate(technical, semantic)
                if name == "technical_non_pass":
                    path, digest = write_json(Path(entry["technical_validator"]["artifact_path"]), technical)
                    entry["technical_validator"].update(artifact_path=path, artifact_digest=digest)
                else:
                    path, digest = write_json(Path(entry["human_semantic_evidence"]["artifact_path"]), semantic)
                    entry["human_semantic_evidence"].update(artifact_path=path, artifact_digest=digest)
                    if name != "placeholder_reviewer":
                        entry["human_semantic_evidence"]["reviewer_id"] = semantic["reviewed_by"]
                target = root / "never-written.json"
                before = snapshot(root)
                with self.assertRaises(ContractError):
                    build_training_approved_inventory(scope=SYNTHETIC_SCOPE, dataset_identity=dataset, episodes=[entry])
                self.assertFalse(target.exists())
                self.assertEqual(snapshot(root), before)

    def test_stale_artifact_and_approval_binding_mismatch_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            dataset, _, semantic, approval, entry = synthetic_fixture(root)
            semantic["reviewed_at"] = "2026-08-24T00:02:00Z"
            write_json(Path(entry["human_semantic_evidence"]["artifact_path"]), semantic)
            before = snapshot(root)
            with self.assertRaisesRegex(ContractError, "TRAINING_SEMANTIC_ARTIFACT"):
                build_training_approved_inventory(scope=SYNTHETIC_SCOPE, dataset_identity=dataset, episodes=[entry])
            self.assertEqual(snapshot(root), before)

            dataset, _, _, approval, entry = synthetic_fixture(root, "episode-2", 2)
            approval["episode_content_digest"] = D3
            path, digest = write_json(Path(entry["training_approval"]["artifact_path"]), approval)
            entry["training_approval"].update(artifact_path=path, artifact_digest=digest)
            before = snapshot(root)
            with self.assertRaisesRegex(ContractError, "TRAINING_APPROVAL_BINDING"):
                build_training_approved_inventory(scope=SYNTHETIC_SCOPE, dataset_identity=dataset, episodes=[entry])
            self.assertEqual(snapshot(root), before)

    def test_duplicate_episode_id_or_index_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            dataset, _, _, _, entry = synthetic_fixture(root)
            before = snapshot(root)
            with self.assertRaisesRegex(ContractError, "TRAINING_INVENTORY_DUPLICATE"):
                build_training_approved_inventory(scope=SYNTHETIC_SCOPE, dataset_identity=dataset, episodes=[entry, copy.deepcopy(entry)])
            self.assertEqual(snapshot(root), before)

    def test_issuance_rejects_synthetic_scope_and_non_tty_without_writes(self):
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            dataset, _, _, _, entry = synthetic_fixture(root)
            target = root / "must-not-exist.json"
            arguments = {
                "dataset_identity": dataset,
                "episode_id": entry["episode_id"],
                "episode_index": entry["episode_index"],
                "episode_content_digest": entry["episode_content_digest"],
                "technical_validator_path": entry["technical_validator"]["artifact_path"],
                "technical_validator_digest": entry["technical_validator"]["artifact_digest"],
                "human_semantic_evidence_path": entry["human_semantic_evidence"]["artifact_path"],
                "human_semantic_evidence_digest": entry["human_semantic_evidence"]["artifact_digest"],
                "approved_by": "synthetic-approver-1",
                "clock": lambda: datetime(2026, 8, 24, tzinfo=timezone.utc),
            }
            before = snapshot(root)
            with self.assertRaisesRegex(ContractError, "TRAINING_APPROVAL_SCOPE"):
                issue_training_approval(target, scope=SYNTHETIC_SCOPE, **arguments)
            with mock.patch("builtins.open", side_effect=[FakeTTY(tty=False), FakeTTY(tty=False)]):
                with self.assertRaisesRegex(ContractError, "HUMAN_TTY_REQUIRED"):
                    issue_training_approval(target, scope=PRODUCTION_SCOPE, **arguments)
            self.assertFalse(target.exists())
            self.assertEqual(snapshot(root), before)

    def test_valid_tty_path_reaches_exclusive_writer_without_creating_production_artifact(self):
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            dataset, _, _, _, entry = synthetic_fixture(root)
            target = root / "mocked-production-output.json"
            phrase = f"{PROVENANCE} {dataset['dataset_id']} {entry['episode_id']} {entry['episode_index']}"
            with mock.patch("builtins.open", side_effect=[FakeTTY(phrase + "\n"), FakeTTY()]), mock.patch.object(
                training_approval, "_write_exclusive"
            ) as writer:
                approval = issue_training_approval(
                    target,
                    scope=PRODUCTION_SCOPE,
                    dataset_identity=dataset,
                    episode_id=entry["episode_id"],
                    episode_index=entry["episode_index"],
                    episode_content_digest=entry["episode_content_digest"],
                    technical_validator_path=entry["technical_validator"]["artifact_path"],
                    technical_validator_digest=entry["technical_validator"]["artifact_digest"],
                    human_semantic_evidence_path=entry["human_semantic_evidence"]["artifact_path"],
                    human_semantic_evidence_digest=entry["human_semantic_evidence"]["artifact_digest"],
                    approved_by="synthetic-approver-1",
                    clock=lambda: datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            self.assertEqual((approval["scope"], approval["provenance"]), (PRODUCTION_SCOPE, PROVENANCE))
            writer.assert_called_once()
            self.assertFalse(target.exists())

    def test_existing_approval_target_fails_before_tty_and_is_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            dataset, _, _, _, entry = synthetic_fixture(root)
            target = root / "existing.json"
            target.write_text("owned\n", encoding="utf-8")
            with mock.patch.object(training_approval, "_confirm_human_training_approval") as confirmation:
                with self.assertRaisesRegex(ContractError, "TRAINING_APPROVAL_EXISTS"):
                    issue_training_approval(
                        target,
                        scope=PRODUCTION_SCOPE,
                        dataset_identity=dataset,
                        episode_id=entry["episode_id"],
                        episode_index=entry["episode_index"],
                        episode_content_digest=entry["episode_content_digest"],
                        technical_validator_path=entry["technical_validator"]["artifact_path"],
                        technical_validator_digest=entry["technical_validator"]["artifact_digest"],
                        human_semantic_evidence_path=entry["human_semantic_evidence"]["artifact_path"],
                        human_semantic_evidence_digest=entry["human_semantic_evidence"]["artifact_digest"],
                        approved_by="synthetic-approver-1",
                    )
            confirmation.assert_not_called()
            self.assertEqual(target.read_text(encoding="utf-8"), "owned\n")


if __name__ == "__main__":
    unittest.main()
