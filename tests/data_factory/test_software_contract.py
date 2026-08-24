"""End-to-end synthetic-only checks for the offline P5.8a contract."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools.data_factory.experiment_manifest import (
    compile_base_condition,
    compile_fr5_hypothesis,
    compile_robot_start_pose,
    compile_seed_manifest,
)
from tools.data_factory.learned_action_adapter import (
    ACTIVE,
    STOPPED,
    FakeCommandSink,
    LearnedActionAdapter,
    fake_observation,
)
from tools.data_factory.software_contract import CONTRACT_READY, validate_software_contract
from tools.data_factory.training_approval import (
    APPROVAL_SCHEMA,
    PROVENANCE,
    SYNTHETIC_SCOPE,
    build_training_approved_inventory,
)
from tools.data_factory.training_receipts import (
    FEATURE_DIGEST,
    RELOAD_RECEIPT_SCHEMA,
    TRAINING_RECEIPT_SCHEMA,
    canonical_digest as receipt_digest,
    feature_binding,
)
from tools.data_factory.training_split import FR5_FEATURE_CONTRACT, compile_training_split
from tools.fr5_data_factory import ContractError, canonical_digest


def digest(value: object) -> str:
    return canonical_digest(value)


def write_json(path: Path, value: object) -> tuple[str, str]:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return str(path), digest(value)


def synthetic_inventory(root: Path) -> dict:
    dataset_root = root / "SYNTHETIC_TEST_ONLY_dataset"
    dataset_root.mkdir()
    dataset = {
        "dataset_id": "synthetic-dataset-r1",
        "repo_id": "tests/synthetic-dataset",
        "dataset_root": str(dataset_root),
        "dataset_digest": digest("synthetic-dataset-root"),
    }
    entries = []
    for index in range(3):
        episode_id = f"synthetic-episode-{index}"
        technical = {
            "schema_version": "data_factory.technical_validator_result.v1",
            "run_id": episode_id,
            "resolved_job_digest": digest(["job", index]),
            "plan_digest": digest(["plan", index]),
            "dataset_root": str(dataset_root),
            "expected_fps": 30,
            "status": "PASS",
            "result_digest": digest(["technical-result", index]),
        }
        technical_path, technical_digest = write_json(
            root / f"{episode_id}.technical.SYNTHETIC_TEST_ONLY.json", technical,
        )
        semantic = {
            "schema_version": "data_factory.candidate_admission.v1",
            "run_id": episode_id,
            "operational_gate": "PASS",
            "operational_source": "HUMAN_GATED",
            "checklist_id": "pickup-v2",
            "review_context_digest": digest({
                "run_id": episode_id,
                "resolved_job_digest": technical["resolved_job_digest"],
                "plan_digest": technical["plan_digest"],
                "technical_validator_digest": technical_digest,
            }),
            "semantic_status": "PASS",
            "reviewed_by": f"synthetic-reviewer-{index}",
            "reviewed_at": "2026-08-24T00:00:00Z",
            "reason": None,
        }
        semantic_path, semantic_digest = write_json(
            root / f"{episode_id}.semantic.SYNTHETIC_TEST_ONLY.json", semantic,
        )
        content_digest = digest(["synthetic-episode-content", index])
        approval = {
            "schema_version": APPROVAL_SCHEMA,
            "scope": SYNTHETIC_SCOPE,
            "dataset_identity": dataset,
            "episode_id": episode_id,
            "episode_index": index,
            "episode_content_digest": content_digest,
            "technical_validator_digest": technical_digest,
            "human_semantic_evidence_digest": semantic_digest,
            "approved_by": f"synthetic-approver-{index}",
            "approved_at": "2026-08-24T00:01:00Z",
            "provenance": PROVENANCE,
        }
        approval_path, approval_digest = write_json(
            root / f"{episode_id}.approval.SYNTHETIC_TEST_ONLY.json", approval,
        )
        entries.append({
            "dataset_identity_digest": digest(dataset),
            "episode_id": episode_id,
            "episode_index": index,
            "episode_content_digest": content_digest,
            "technical_validator": {
                "artifact_path": technical_path,
                "artifact_digest": technical_digest,
                "status": "PASS",
            },
            "human_semantic_evidence": {
                "artifact_path": semantic_path,
                "artifact_digest": semantic_digest,
                "status": "PASS",
                "reviewer_id": semantic["reviewed_by"],
            },
            "training_approval": {
                "artifact_path": approval_path,
                "artifact_digest": approval_digest,
                "provenance": PROVENANCE,
            },
        })
    return build_training_approved_inventory(
        scope=SYNTHETIC_SCOPE, dataset_identity=dataset, episodes=entries,
    )


def fixed_contract(profile_digest: str) -> dict:
    return {
        "schema_version": "data_factory.fr5_fixed_contract.v1",
        "robot_system_id": "fr5-r1",
        "task": "pickup_e2e",
        "instruction": "Pick up the object",
        "collection_profile_digest": profile_digest,
        "feature_contract": copy.deepcopy(FR5_FEATURE_CONTRACT),
        "object_profile_id": "object-r1",
        "grasp_profile_id": "grasp-r1",
        "scene_digest": digest("synthetic-scene"),
        "cell_calibration_id": "calibration-r1",
        "cell_calibration_digest": digest("synthetic-calibration"),
        "motion_recipe": "DIRECT",
        "motion_recipe_digest": digest("DIRECT"),
        "pregrasp_digest": digest("fixed-pregrasp"),
        "waypoint_digest": digest("fixed-waypoint"),
        "trajectory_digest": digest("fixed-trajectory"),
    }


def coverage(place_id: str, yaw_deg: float, fixed: dict) -> dict:
    return {
        "task_schema_version": "data_factory.job.v1",
        "task": fixed["task"],
        "robot_system_id": fixed["robot_system_id"],
        "place_id": place_id,
        "cell_calibration_id": fixed["cell_calibration_id"],
        "cell_calibration_digest": fixed["cell_calibration_digest"],
        "yaw_deg": yaw_deg,
        "x_mm": 10.0 if place_id == "cell-a" else 20.0,
        "y_mm": 0.0,
        "object_profile_id": fixed["object_profile_id"],
        "grasp_profile_id": fixed["grasp_profile_id"],
        "motion_recipe_digest": fixed["motion_recipe_digest"],
        "collection_profile_digest": fixed["collection_profile_digest"],
    }


def program_budget() -> dict:
    return {
        "max_rounds": 3, "used_rounds": 0,
        "max_total_physical_episodes": 10, "used_total_physical_episodes": 0,
        "max_total_rollout_trials": 10, "used_total_rollout_trials": 0,
        "max_total_hil_prompts": 10, "used_total_hil_prompts": 0,
        "max_total_reviews": 10, "used_total_reviews": 0,
        "max_pending_reviews": 10, "used_pending_reviews": 0,
        "max_total_storage_bytes": 10_000, "used_total_storage_bytes": 0,
    }


def runtime() -> dict:
    return {
        "python_version": "3.12.synthetic",
        "lerobot_version": "0.6.1.synthetic",
        "lerobot_source_digest": digest("synthetic-lerobot-source"),
        "torch_version": "2.11.synthetic",
        "torch_source_digest": digest("synthetic-torch-source"),
        "cuda_version": "12.8.synthetic",
        "cuda_source_digest": digest("synthetic-cuda-source"),
    }


def synthetic_bundle(root: Path) -> dict:
    inventory = synthetic_inventory(root)
    profile_digest = digest("synthetic-collection-profile")
    fixed = fixed_contract(profile_digest)
    base_a = compile_base_condition(
        coverage("cell-a", 0.0, fixed),
        yaw_action_binding_digest=digest("synthetic-yaw-action-0"),
        dual_view_observability_digest=digest("synthetic-dual-view-cue-0"),
    )
    base_b = compile_base_condition(
        coverage("cell-b", 90.0, fixed),
        yaw_action_binding_digest=digest("synthetic-yaw-action-90"),
        dual_view_observability_digest=digest("synthetic-dual-view-cue-90"),
    )
    joints = ("j1", "j2", "j3", "j4", "j5", "j6")
    pose_a = compile_robot_start_pose(
        robot_start_pose_id="start-a",
        target_rad={joint: index / 10 for index, joint in enumerate(joints)},
        tolerance_rad={joint: 0.01 for joint in joints},
        home_candidate_digest=digest("synthetic-home-a"),
        qualification_digest=digest("synthetic-qualification-a"),
    )
    pose_b = compile_robot_start_pose(
        robot_start_pose_id="start-b",
        target_rad={joint: 0.1 + index / 10 for index, joint in enumerate(joints)},
        tolerance_rad={joint: 0.01 for joint in joints},
        home_candidate_digest=digest("synthetic-home-b"),
        qualification_digest=digest("synthetic-qualification-b"),
    )
    hypothesis = compile_fr5_hypothesis(
        fixed_contract=fixed,
        base_conditions=[base_a, base_b],
        robot_start_poses=[pose_a, pose_b],
        allowed_pairs=[
            {
                "base_condition_digest": base_a["base_condition_digest"],
                "robot_start_pose_id": "start-a",
                "split_groups": ["TRAIN", "ID"],
            },
            {
                "base_condition_digest": base_b["base_condition_digest"],
                "robot_start_pose_id": "start-b",
                "split_groups": ["OOD"],
            },
        ],
    )
    slot_values = (
        ("train-a", base_a, "start-a", "TRAIN"),
        ("id-a", base_a, "start-a", "ID"),
        ("ood-b", base_b, "start-b", "OOD"),
    )
    slots = [{
        "slot_id": slot_id,
        "base_condition_digest": base["base_condition_digest"],
        "robot_start_pose_id": pose_id,
        "split_group": group,
        "repeat_index": 0,
        "hil_prompts": 1,
        "reviews": 1,
        "pending_reviews": 0,
        "storage_bytes": 100,
    } for slot_id, base, pose_id, group in slot_values]
    manifest = compile_seed_manifest(
        manifest_id="synthetic-seed-r1",
        hypothesis=hypothesis,
        slots=slots,
        randomization_seed=17,
        manifest_budget={
            "max_physical_episodes": 3,
            "max_rollout_trials": 1,
            "max_hil_prompts": 3,
            "max_reviews": 3,
            "max_pending_reviews": 1,
            "max_storage_bytes": 300,
        },
        program_budget=program_budget(),
    )
    groups = {"TRAIN": [], "ID": [], "OOD": []}
    cells = (("TRAIN", base_a, "start-a"), ("ID", base_a, "start-a"), ("OOD", base_b, "start-b"))
    for entry, (group, base, pose_id) in zip(inventory["episodes"], cells):
        groups[group].append({
            "episode_index": entry["episode_index"],
            "episode_ref_digest": entry["episode_content_digest"],
            "training_approval_digest": entry["training_approval"]["artifact_digest"],
            "base_condition_digest": base["base_condition_digest"],
            "robot_start_pose_id": pose_id,
        })
    argv = ["lerobot-train", "--policy.type=smolvla", "--seed=17"]
    versions = runtime()
    split = compile_training_split(
        dataset={
            "dataset_root_identity_digest": inventory["dataset_identity"]["dataset_digest"],
            "repo_id": inventory["dataset_identity"]["repo_id"],
            "dataset_info_features_digest": digest("synthetic-dataset-features"),
            "total_episodes": 3,
            "total_frames": 300,
        },
        bindings={
            "collection_profile_digest": profile_digest,
            "normalized_command_digest": receipt_digest(argv),
            "runtime_digest": receipt_digest(versions),
            "approved_episode_inventory_digest": inventory["inventory_digest"],
            "episode_manifest_digest": manifest["manifest_digest"],
        },
        episode_groups=groups,
        program_budget=program_budget(),
    )
    train = {
        "schema_version": TRAINING_RECEIPT_SCHEMA,
        "receipt_id": "synthetic-train-receipt",
        "process_id": "synthetic-train-process",
        "session_id": "synthetic-train-session",
        "dataset_id": inventory["dataset_identity"]["dataset_id"],
        "dataset_digest": inventory["dataset_identity"]["dataset_digest"],
        "repository_commit": "a" * 40,
        "source_digest": digest("synthetic-repository-source"),
        "profile_id": "smolvla-fr5-up-side-v1",
        "profile_digest": digest("synthetic-training-profile"),
        "collection_profile_digest": profile_digest,
        "normalized_argv": argv,
        "argv_digest": receipt_digest(argv),
        "config_digest": digest("synthetic-training-config"),
        "runtime_versions": versions,
        "runtime_digest": receipt_digest(versions),
        "approved_episode_inventory_digest": inventory["inventory_digest"],
        "episode_manifest_digest": manifest["manifest_digest"],
        "split_digest": split["split_digest"],
        "training_seed": 17,
        "feature_binding": feature_binding(),
        "feature_digest": FEATURE_DIGEST,
        "checkpoint_id": "synthetic-checkpoint-17",
        "checkpoint_tree_digest": digest("synthetic-checkpoint-tree"),
        "status": "PASS",
    }
    reload_argv = ["python3", "-m", "synthetic_reload", "--checkpoint=synthetic-checkpoint-17"]
    reload = {
        "schema_version": RELOAD_RECEIPT_SCHEMA,
        "reload_receipt_id": "synthetic-reload-receipt",
        "train_receipt_id": train["receipt_id"],
        "train_receipt_digest": receipt_digest(train),
        "train_process_id": train["process_id"],
        "train_session_id": train["session_id"],
        "reload_process_id": "synthetic-reload-process",
        "reload_session_id": "synthetic-reload-session",
        "repository_commit": train["repository_commit"],
        "source_digest": train["source_digest"],
        "profile_id": train["profile_id"],
        "profile_digest": train["profile_digest"],
        "collection_profile_digest": train["collection_profile_digest"],
        "normalized_argv": reload_argv,
        "argv_digest": receipt_digest(reload_argv),
        "runtime_versions": copy.deepcopy(train["runtime_versions"]),
        "runtime_digest": train["runtime_digest"],
        "checkpoint_id": train["checkpoint_id"],
        "checkpoint_tree_digest": train["checkpoint_tree_digest"],
        "split_digest": train["split_digest"],
        "feature_digest": train["feature_digest"],
        "reload_status": "PASS",
        "task_success_claimed": False,
    }
    return {
        "approved_inventory": inventory,
        "split": split,
        "hypothesis": hypothesis,
        "seed_manifest": manifest,
        "training_receipt": train,
        "reload_receipt": reload,
    }


def refresh_split_and_reload(bundle: dict) -> None:
    split = bundle["split"]
    split["split_digest"] = canonical_digest({
        key: value for key, value in split.items() if key != "split_digest"
    })
    train = bundle["training_receipt"]
    train["split_digest"] = split["split_digest"]
    reload = bundle["reload_receipt"]
    reload["split_digest"] = train["split_digest"]
    reload["train_receipt_digest"] = receipt_digest(train)


class SoftwareContractTests(unittest.TestCase):
    def test_synthetic_bundle_is_contract_ready_and_7d_fake_stops(self) -> None:
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            bundle = synthetic_bundle(Path(directory))
            ready = validate_software_contract(**bundle, expected_scope=SYNTHETIC_SCOPE)
            self.assertEqual((ready["status"], ready["scope"]), (CONTRACT_READY, SYNTHETIC_SCOPE))
            self.assertEqual(
                ready["readiness_digest"],
                canonical_digest({key: value for key, value in ready.items() if key != "readiness_digest"}),
            )

            clock = lambda: 10.0
            sink = FakeCommandSink()
            adapter = LearnedActionAdapter(lambda _observation: [0.0] * 7, sink, clock=clock)
            self.assertEqual(adapter.start("synthetic-goal"), ACTIVE)
            self.assertEqual(adapter.step(fake_observation(10.0)), ACTIVE)
            self.assertEqual(adapter.stop(), STOPPED)
            adapter.step(fake_observation(10.0))
            self.assertEqual(len(sink.commands), 1)

    def test_cross_artifact_mismatch_fails_closed(self) -> None:
        mutations = (
            lambda bundle: bundle["training_receipt"].update(dataset_id="other-synthetic-dataset"),
            lambda bundle: bundle["training_receipt"].update(
                collection_profile_digest=digest("other-profile")
            ),
            lambda bundle: bundle["training_receipt"].update(
                approved_episode_inventory_digest=digest("other-inventory")
            ),
            lambda bundle: bundle["training_receipt"].update(
                episode_manifest_digest=digest("other-manifest")
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                prefix="SYNTHETIC_TEST_ONLY-"
            ) as directory:
                bundle = synthetic_bundle(Path(directory))
                mutation(bundle)
                bundle["reload_receipt"]["train_receipt_digest"] = receipt_digest(
                    bundle["training_receipt"]
                )
                with self.assertRaises(ContractError):
                    validate_software_contract(**bundle, expected_scope=SYNTHETIC_SCOPE)

    def test_split_inventory_episode_and_repo_mismatch_fails_after_valid_digests(self) -> None:
        mutations = (
            lambda split: split["dataset"].update(repo_id="tests/other-synthetic-dataset"),
            lambda split: split["bindings"].update(
                approved_episode_inventory_digest=digest("other-inventory")
            ),
            lambda split: split["episode_groups"]["ID"][0].update(
                episode_ref_digest=digest("other-episode")
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                prefix="SYNTHETIC_TEST_ONLY-"
            ) as directory:
                bundle = synthetic_bundle(Path(directory))
                mutation(bundle["split"])
                refresh_split_and_reload(bundle)
                with self.assertRaises(ContractError):
                    validate_software_contract(**bundle, expected_scope=SYNTHETIC_SCOPE)


if __name__ == "__main__":
    unittest.main()
