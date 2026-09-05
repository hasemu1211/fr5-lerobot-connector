import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.data_factory import training_approval
from tools.data_factory.training_approval import (
    APPROVAL_SCHEMA,
    EPISODE_PROVENANCE_SCHEMA,
    INVENTORY_SCHEMA,
    PRODUCTION_SCOPE,
    PROVENANCE,
    SYNTHETIC_SCOPE,
    build_training_approved_inventory,
    compile_episode_training_provenance,
    issue_training_approval,
    validate_episode_training_provenance,
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
    slot = {
        "slot_id": f"slot-{episode_id}",
        "base_condition_digest": canonical_digest(["base-condition", episode_id]),
        "robot_start_pose_id": f"start-{episode_id}",
        "split_group": ("TRAIN", "ID", "OOD")[episode_index % 3],
        "repeat_index": episode_index // 3,
        "hil_prompts": 1,
        "reviews": 1,
        "pending_reviews": 0,
        "storage_bytes": 100,
        "order_index": 0,
    }
    manifest = {
        "schema_version": "data_factory.seed_manifest.v1",
        "manifest_id": f"seed-{episode_id}",
        "kind": "seed",
        "hypothesis_digest": canonical_digest(["hypothesis", episode_id]),
        "fixed_contract_digest": canonical_digest(["fixed-contract", episode_id]),
        "randomization_seed": episode_index,
        "slots": [slot],
        "manifest_budget": {"SYNTHETIC_TEST_ONLY": 1},
        "program_budget": {"SYNTHETIC_TEST_ONLY": 1},
        "planned_usage": {"SYNTHETIC_TEST_ONLY": 1},
        "authority": "NO_EXECUTION_AUTHORITY",
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    manifest_path, _ = write_json(
        root / f"{episode_id}.seed-manifest.SYNTHETIC_TEST_ONLY.json", manifest,
    )
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
    episode_provenance = compile_episode_training_provenance(
        scope=SYNTHETIC_SCOPE,
        dataset_identity=dataset,
        episode_id=episode_id,
        episode_index=episode_index,
        episode_content_digest=D2,
        technical_validator_path=technical_path,
        technical_validator_digest=technical_digest,
        seed_manifest=manifest_path,
        manifest_slot_id=slot["slot_id"],
    )
    provenance_path, provenance_digest = write_json(
        root / f"{episode_id}.provenance.SYNTHETIC_TEST_ONLY.json", episode_provenance,
    )
    approval = {
        "schema_version": APPROVAL_SCHEMA,
        "scope": SYNTHETIC_SCOPE,
        "dataset_identity": dataset,
        "episode_id": episode_id,
        "episode_index": episode_index,
        "episode_content_digest": D2,
        "technical_validator_digest": technical_digest,
        "human_semantic_evidence_digest": semantic_digest,
        "episode_provenance_digest": provenance_digest,
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
        "episode_provenance": {
            "artifact_path": provenance_path,
            "artifact_digest": provenance_digest,
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
            provenance = load_json_strict(inventory["episodes"][0]["episode_provenance"]["artifact_path"])
            self.assertEqual(provenance["schema_version"], EPISODE_PROVENANCE_SCHEMA)
            self.assertEqual(
                (provenance["manifest_slot_id"], provenance["split_group"], provenance["repeat_index"]),
                ("slot-episode-0", "TRAIN", 0),
            )
            self.assertEqual([item["episode_index"] for item in inventory["episodes"]], [0, 1])
            self.assertEqual(
                inventory["inventory_digest"],
                canonical_digest({key: inventory[key] for key in ("schema_version", "scope", "dataset_identity", "episodes")}),
            )
            self.assertEqual(
                validate_training_approved_inventory(inventory, expected_scope=SYNTHETIC_SCOPE), inventory,
            )
            approval_digest = inventory["episodes"][1]["training_approval"]["artifact_digest"]
            first["training_approval"]["artifact_digest"] = D3
            self.assertEqual(
                inventory["episodes"][1]["training_approval"]["artifact_digest"], approval_digest,
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

    def test_provenance_is_derived_from_exact_technical_and_seed_sources(self):
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            dataset, technical, _, _, entry = synthetic_fixture(root, "episode-4", 4)
            provenance = load_json_strict(entry["episode_provenance"]["artifact_path"])
            self.assertEqual(
                validate_episode_training_provenance(provenance, expected_scope=SYNTHETIC_SCOPE),
                provenance,
            )
            self.assertEqual(provenance["dataset_identity_digest"], canonical_digest(dataset))
            self.assertEqual(provenance["resolved_job_digest"], technical["resolved_job_digest"])
            self.assertEqual(
                (
                    provenance["seed_manifest_id"], provenance["manifest_slot_id"],
                    provenance["split_group"], provenance["repeat_index"],
                    provenance["base_condition_digest"], provenance["robot_start_pose_id"],
                ),
                (
                    "seed-episode-4", "slot-episode-4", "ID", 1,
                    canonical_digest(["base-condition", "episode-4"]), "start-episode-4",
                ),
            )
            with self.assertRaisesRegex(ContractError, "TRAINING_EPISODE_PROVENANCE_FIELDS"):
                validate_episode_training_provenance(
                    {**provenance, "caller_annotation": "untrusted"},
                    expected_scope=SYNTHETIC_SCOPE,
                )

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

    def test_provenance_artifact_is_reloaded_and_exactly_bound_by_approval(self):
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            dataset, _, _, _, entry = synthetic_fixture(root)
            provenance_path = Path(entry["episode_provenance"]["artifact_path"])
            provenance = load_json_strict(provenance_path)
            provenance["base_condition_digest"] = D3
            write_json(provenance_path, provenance)
            before = snapshot(root)
            with self.assertRaisesRegex(ContractError, "TRAINING_EPISODE_PROVENANCE_ARTIFACT"):
                build_training_approved_inventory(
                    scope=SYNTHETIC_SCOPE, dataset_identity=dataset, episodes=[entry],
                )
            self.assertEqual(snapshot(root), before)

            dataset, _, _, approval, entry = synthetic_fixture(root, "episode-5", 5)
            provenance_path = Path(entry["episode_provenance"]["artifact_path"])
            provenance = load_json_strict(provenance_path)
            provenance["base_condition_digest"] = D3
            _, provenance_digest = write_json(provenance_path, provenance)
            entry["episode_provenance"]["artifact_digest"] = provenance_digest
            before = snapshot(root)
            with self.assertRaisesRegex(ContractError, "TRAINING_APPROVAL_BINDING"):
                build_training_approved_inventory(
                    scope=SYNTHETIC_SCOPE, dataset_identity=dataset, episodes=[entry],
                )
            self.assertEqual(snapshot(root), before)

            dataset, _, _, approval, entry = synthetic_fixture(root, "episode-6", 6)
            provenance_path = Path(entry["episode_provenance"]["artifact_path"])
            provenance = load_json_strict(provenance_path)
            provenance["resolved_job_digest"] = D3
            _, provenance_digest = write_json(provenance_path, provenance)
            entry["episode_provenance"]["artifact_digest"] = provenance_digest
            approval["episode_provenance_digest"] = provenance_digest
            approval_path = Path(entry["training_approval"]["artifact_path"])
            _, approval_digest = write_json(approval_path, approval)
            entry["training_approval"]["artifact_digest"] = approval_digest
            before = snapshot(root)
            with self.assertRaisesRegex(ContractError, "TRAINING_EPISODE_PROVENANCE_BINDING"):
                build_training_approved_inventory(
                    scope=SYNTHETIC_SCOPE, dataset_identity=dataset, episodes=[entry],
                )
            self.assertEqual(snapshot(root), before)

    def test_duplicate_episode_id_or_index_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            dataset, _, _, _, entry = synthetic_fixture(root)
            before = snapshot(root)
            with self.assertRaisesRegex(ContractError, "TRAINING_INVENTORY_DUPLICATE"):
                build_training_approved_inventory(scope=SYNTHETIC_SCOPE, dataset_identity=dataset, episodes=[entry, copy.deepcopy(entry)])
            self.assertEqual(snapshot(root), before)

    def test_duplicate_seed_slot_binding_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            root = Path(directory)
            dataset, _, _, _, first = synthetic_fixture(root, "episode-1", 1)
            _, _, _, second_approval, second = synthetic_fixture(root, "episode-2", 2)
            first_provenance = load_json_strict(first["episode_provenance"]["artifact_path"])
            second_provenance_path = Path(second["episode_provenance"]["artifact_path"])
            second_provenance = load_json_strict(second_provenance_path)
            for field in (
                "seed_manifest_id", "seed_manifest_digest", "manifest_slot_id", "split_group",
                "repeat_index", "base_condition_digest", "robot_start_pose_id",
            ):
                second_provenance[field] = first_provenance[field]
            _, provenance_digest = write_json(second_provenance_path, second_provenance)
            second["episode_provenance"]["artifact_digest"] = provenance_digest
            second_approval["episode_provenance_digest"] = provenance_digest
            approval_path = Path(second["training_approval"]["artifact_path"])
            _, approval_digest = write_json(approval_path, second_approval)
            second["training_approval"]["artifact_digest"] = approval_digest
            before = snapshot(root)
            with self.assertRaisesRegex(ContractError, "TRAINING_INVENTORY_DUPLICATE"):
                build_training_approved_inventory(
                    scope=SYNTHETIC_SCOPE, dataset_identity=dataset, episodes=[first, second],
                )
            self.assertEqual(snapshot(root), before)

    def test_issuance_rejects_synthetic_scope_or_provenance_without_writes(self):
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
                "episode_provenance_path": entry["episode_provenance"]["artifact_path"],
                "episode_provenance_digest": entry["episode_provenance"]["artifact_digest"],
                "approved_by": "synthetic-approver-1",
            }
            before = snapshot(root)
            with mock.patch.object(training_approval, "_confirm_human_training_approval") as confirmation, mock.patch.object(
                training_approval, "_write_exclusive",
            ) as writer:
                with self.assertRaisesRegex(ContractError, "TRAINING_APPROVAL_SCOPE"):
                    issue_training_approval(target, scope=SYNTHETIC_SCOPE, **arguments)
                with self.assertRaisesRegex(ContractError, "TRAINING_APPROVAL_SCOPE"):
                    issue_training_approval(target, scope=PRODUCTION_SCOPE, **arguments)
            confirmation.assert_not_called()
            writer.assert_not_called()
            self.assertFalse(target.exists())
            self.assertEqual(snapshot(root), before)

    def test_human_confirmation_is_tty_only_and_exact(self):
        with mock.patch("builtins.open", side_effect=[FakeTTY(tty=False), FakeTTY(tty=False)]):
            with self.assertRaisesRegex(ContractError, "HUMAN_TTY_REQUIRED"):
                training_approval._confirm_human_training_approval("synthetic-confirmation")
        with mock.patch("builtins.open", side_effect=[FakeTTY("wrong-digest\n"), FakeTTY()]):
            with self.assertRaisesRegex(ContractError, "HUMAN_CONFIRMATION_FAILED"):
                training_approval._confirm_human_training_approval("synthetic-confirmation")

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
                        episode_provenance_path=entry["episode_provenance"]["artifact_path"],
                        episode_provenance_digest=entry["episode_provenance"]["artifact_digest"],
                        approved_by="synthetic-approver-1",
                    )
            confirmation.assert_not_called()
            self.assertEqual(target.read_text(encoding="utf-8"), "owned\n")


class CollectionLedgerTrainingApprovalTest(unittest.TestCase):
    def ledger_case(self, *, production=True):
        from tests.data_factory.test_episode_ledger import EpisodeLedgerTest
        import pyarrow as pa
        import pyarrow.parquet as pq
        from tools.fr5_dataset_schema import dataset_features

        fixture = EpisodeLedgerTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        artifacts = fixture._artifacts()
        if production:
            path = Path(artifacts["runtime_binding"]["artifact_path"])
            runtime = json.loads(path.read_text())
            runtime.update(schema_version="data_factory.production_episode_binding.v1", data_disposition="PRODUCTION",
                           state_initialization_digest=None, scene_observation_digest=D1)
            runtime["binding_digest"] = canonical_digest({k: v for k, v in runtime.items() if k != "binding_digest"})
            artifacts["runtime_binding"] = fixture._json("runtime-production.json", runtime)
        ledger = fixture._compile(artifacts)
        ledger_ref = fixture._json("episode_ledger.json", ledger)
        semantic = fixture._candidate(ledger, "PASS")
        metadata = fixture.dataset / "meta"
        (metadata / "source_provenance").mkdir(parents=True)
        (metadata / "source_provenance/episode-000000.jsonl").write_bytes(Path(artifacts["source_provenance"]["artifact_path"]).read_bytes())
        (metadata / "episodes/chunk-000").mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist([{"episode_index": 0, "tasks": ["pick up the cube"], "length": 2}]), metadata / "episodes/chunk-000/file-000.parquet")
        write_json(metadata / "info.json", {"fps": 30, "total_episodes": 1, "total_frames": 2,
            "features": dataset_features(fps=30, height=480, width=640, cameras=("up", "wrist"), use_videos=True)})
        request = {"dataset_root": str(fixture.dataset), "dataset_id": "frozen-r1", "repo_id": fixture.dataset_identity["repo_id"],
                   "episodes": [{"episode_id": fixture.run_id, "episode_index": 0,
                       "technical_validator_path": artifacts["technical"]["artifact_path"],
                       "human_semantic_evidence_path": semantic["artifact_path"],
                       "episode_ledger_path": ledger_ref["artifact_path"]}]}
        output = fixture.base / "training-approval"
        output.mkdir()
        return fixture, request, output

    def test_public_approval_uses_collection_ledger_without_seed_or_inherited_consent(self):
        from tools.data_factory.training_entrypoint import approve
        fixture, request, output = self.ledger_case()
        before = snapshot(fixture.dataset)
        original_ledger = Path(request["episodes"][0]["episode_ledger_path"]).read_bytes()
        with mock.patch.object(training_approval, "_confirm_human_training_approval") as confirm:
            preview = approve(request, output, "fixture-human", dry_run=True)
            confirm.assert_not_called()
            self.assertEqual(list(output.iterdir()), [])
            issued = approve(request, output, "fixture-human", dry_run=False)
            confirm.assert_called_once()
        self.assertEqual(preview["episodes"][0]["provenance"]["schema_version"], training_approval.LEDGER_PROVENANCE_SCHEMA)
        self.assertNotIn("seed_manifest_digest", preview["episodes"][0]["provenance"])
        training_approval.validate_current_training_inventory(output / "training_approved.json",
            dataset_root=fixture.dataset, repo_id=request["repo_id"], selected_episodes=[0])
        self.assertEqual(issued["episodes"][0]["episode_id"], fixture.run_id)
        self.assertEqual(snapshot(fixture.dataset), before)
        self.assertEqual(Path(request["episodes"][0]["episode_ledger_path"]).read_bytes(), original_ledger)
        ledger = json.loads(original_ledger)
        self.assertEqual(ledger["admission"]["training_status"], "NOT_AUTHORIZED")
        # Source changes invalidate the new inventory even if its self-digest is intact.
        Path(ledger["artifacts"]["runtime_binding"]["artifact_path"]).write_text("{}")
        with self.assertRaises(ContractError):
            validate_training_approved_inventory(output / "training_approved.json")

    def test_test_only_collection_cannot_be_promoted_by_approval_request(self):
        from tools.data_factory.training_entrypoint import approve
        _, request, output = self.ledger_case(production=False)
        with self.assertRaisesRegex(ContractError, "TRAINING_APPROVAL_SCOPE"):
            approve(request, output, "fixture-human", dry_run=True)
        self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
