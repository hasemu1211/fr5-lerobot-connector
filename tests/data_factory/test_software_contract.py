"""End-to-end synthetic-only checks for the offline software contract."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools.data_factory.experiment_manifest import (
    compile_fr5_hypothesis,
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
    compile_episode_training_provenance,
)
from tools.data_factory.quality.coverage_report import build_coverage_report
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


def synthetic_inventory(root: Path, manifest: dict, hypothesis: dict) -> dict:
    dataset_root = root / "SYNTHETIC_TEST_ONLY_dataset"
    dataset_root.mkdir()
    dataset = {
        "dataset_id": "synthetic-dataset-r1",
        "repo_id": "tests/synthetic-dataset",
        "dataset_root": str(dataset_root),
        "dataset_digest": digest("synthetic-dataset-root"),
    }
    entries = []
    bases = {
        item["base_condition_digest"]: item for item in hypothesis["base_conditions"]
    }
    for index, slot in enumerate(manifest["slots"]):
        episode_id = f"synthetic-episode-{index}"
        technical = {
            "schema_version": "data_factory.technical_validator_result.v1",
            "run_id": episode_id,
            "resolved_job_digest": bases[slot["base_condition_digest"]]["resolved_job_digest"],
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
        provenance = compile_episode_training_provenance(
            scope=SYNTHETIC_SCOPE,
            dataset_identity=dataset,
            episode_id=episode_id,
            episode_index=index,
            episode_content_digest=content_digest,
            technical_validator_path=technical_path,
            technical_validator_digest=technical_digest,
            seed_manifest=manifest,
            manifest_slot_id=slot["slot_id"],
        )
        provenance_path, provenance_digest = write_json(
            root / f"{episode_id}.provenance.SYNTHETIC_TEST_ONLY.json", provenance,
        )
        approval = {
            "schema_version": APPROVAL_SCHEMA,
            "scope": SYNTHETIC_SCOPE,
            "dataset_identity": dataset,
            "episode_id": episode_id,
            "episode_index": index,
            "episode_content_digest": content_digest,
            "technical_validator_digest": technical_digest,
            "human_semantic_evidence_digest": semantic_digest,
            "episode_provenance_digest": provenance_digest,
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
            "episode_provenance": {
                "artifact_path": provenance_path,
                "artifact_digest": provenance_digest,
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


def redigest(value: dict, field: str) -> dict:
    value[field] = digest({key: item for key, item in value.items() if key != field})
    return value


def source_documents() -> dict[str, dict]:
    return {
        "robot_system": {
            "schema_version": "data_factory.robot_system.v1",
            "robot_system_id": "fr5-r1",
            "qualification_status": "QUALIFIED",
            "base_frame": "base_link",
            "tcp_digest": digest("synthetic-tcp"),
        },
        "collection_profile": {
            "schema_version": "data_factory.collection_profile.v1",
            "collection_profile_id": "fr5-dual-rgb-30hz-v1",
            "qualification_status": "QUALIFIED",
        },
        "object_profile": {
            "schema_version": "data_factory.object_profile.v2",
            "object_profile_id": "object-r1",
            "qualification_status": "QUALIFIED",
        },
        "grasp_profile": {
            "schema_version": "data_factory.grasp_profile.v2",
            "grasp_profile_id": "grasp-r1",
            "object_profile_id": "object-r1",
            "qualification_status": "QUALIFIED",
        },
        "cell_calibration": {
            "schema_version": "data_factory.cell_calibration.v1",
            "calibration_id": "calibration-r1",
            "robot_system_id": "fr5-r1",
            "place_id": "place-r1",
            "qualification_status": "QUALIFIED",
        },
    }


def fixed_contract() -> dict:
    documents = source_documents()
    return {
        "schema_version": "data_factory.fr5_fixed_contract.v1",
        "robot_system_id": "fr5-r1",
        "task": "pickup_e2e",
        "instruction": "pick up the synthetic object",
        "collection_profile_digest": digest(documents["collection_profile"]),
        "feature_contract": copy.deepcopy(FR5_FEATURE_CONTRACT),
        "object_profile_id": "object-r1",
        "grasp_profile_id": "grasp-r1",
        "scene_digest": digest("synthetic-scene"),
        "cell_calibration_id": "calibration-r1",
        "cell_calibration_digest": digest(documents["cell_calibration"]),
        "motion_recipe": "DIRECT",
        "motion_recipe_digest": digest("synthetic-direct"),
        "pregrasp_digest": digest("fixed-pregrasp"),
        "waypoint_digest": digest("fixed-waypoint"),
        "trajectory_digest": digest("fixed-trajectory"),
    }


def coverage(*, yaw_deg: int, x_mm: int) -> dict:
    fixed = fixed_contract()
    return {
        "task_schema_version": "data_factory.job.v1",
        "task": fixed["task"],
        "robot_system_id": fixed["robot_system_id"],
        "place_id": "place-r1",
        "cell_calibration_id": fixed["cell_calibration_id"],
        "cell_calibration_digest": fixed["cell_calibration_digest"],
        "yaw_deg": yaw_deg,
        "x_mm": x_mm,
        "y_mm": 0,
        "object_profile_id": fixed["object_profile_id"],
        "grasp_profile_id": fixed["grasp_profile_id"],
        "motion_recipe_digest": fixed["motion_recipe_digest"],
        "collection_profile_digest": fixed["collection_profile_digest"],
    }


def resolver_result(condition: dict, name: str) -> dict:
    documents = source_documents()
    sheet_digest = digest(["synthetic-sheet", name])
    job = {
        "schema_version": "data_factory.job.v1",
        "job_id": f"job-{name}",
        "task": condition["task"],
        "robot_system_id": condition["robot_system_id"],
        "collection_profile_id": "fr5-dual-rgb-30hz-v1",
        "place_id": condition["place_id"],
        "cell_calibration_id": condition["cell_calibration_id"],
        "sheet_manifest_digest": sheet_digest,
        "yaw_deg": condition["yaw_deg"],
        "x_mm": condition["x_mm"],
        "y_mm": condition["y_mm"],
        "object_profile_id": condition["object_profile_id"],
        "grasp_profile_id": condition["grasp_profile_id"],
        "instruction": "pick up the synthetic object",
        "episode_intent": "nominal pickup",
        "operator_or_agent_id": "synthetic-test",
        "approval_expiry": "2099-01-01T00:00:00Z",
        "dry_run_required": True,
    }
    input_digests = {
        "selected_sheet": sheet_digest,
        "yaw0_sheet": digest("synthetic-yaw0"),
        **{key: digest(value) for key, value in documents.items()},
    }
    return {
        "normalized_job": job,
        "input_digests": input_digests,
        "resolved_job_digest": digest({"job": job, "input_digests": input_digests}),
        "robot": documents["robot_system"],
        "collection_profile": documents["collection_profile"],
        "calibration": {
            "center": [0.4, 0.0, 0.1],
            "x": [1.0, 0.0, 0.0],
            "y": [0.0, 1.0, 0.0],
            "z": [0.0, 0.0, 1.0],
            "document": documents["cell_calibration"],
        },
        "object_profile": documents["object_profile"],
        "grasp_profile": documents["grasp_profile"],
    }


def base_qualification(report: dict, resolved: dict, condition: dict, name: str) -> dict:
    return redigest({
        "schema_version": "data_factory.fr5_base_condition_qualification.v1",
        "source": "SYNTHETIC_TEST_ONLY",
        "qualification_status": "QUALIFIED",
        "coverage_report_digest": digest(report),
        "coverage_domain_digest": report["domain_digest"],
        "coverage_condition_digest": digest(condition),
        "resolver_result_digest": digest(resolved),
        "resolved_job_digest": resolved["resolved_job_digest"],
        "yaw_action_binding_digest": digest(["synthetic-yaw-action", name]),
        "dual_view_observability_digest": digest(["synthetic-view", name]),
    }, "qualification_digest")


def pose_qualification(name: str, offset: float) -> dict:
    joints = ("j1", "j2", "j3", "j4", "j5", "j6")
    return redigest({
        "schema_version": "data_factory.robot_start_pose_qualification.v1",
        "source": "SYNTHETIC_TEST_ONLY",
        "robot_system_id": "fr5-r1",
        "robot_start_pose_id": name,
        "joint_order": list(joints),
        "target_rad": {joint: offset + index / 10 for index, joint in enumerate(joints)},
        "tolerance_rad": {joint: 0.01 for joint in joints},
        "home_candidate_digest": digest(["synthetic-home", name]),
        "qualification_status": "QUALIFIED",
        "safety_status": "SAFE_FOR_MOTION",
    }, "qualification_digest")


def synthetic_hypothesis() -> dict:
    fixed = fixed_contract()
    domain = [coverage(yaw_deg=0, x_mm=10), coverage(yaw_deg=90, x_mm=20)]
    report = build_coverage_report(
        collection_profile_id="fr5-dual-rgb-30hz-v1", domain=domain, episodes=[],
    )
    resolvers = [resolver_result(item, name) for item, name in zip(domain, ("a", "b"))]
    bases = [
        base_qualification(report, resolved, condition, name)
        for resolved, condition, name in zip(resolvers, domain, ("a", "b"))
    ]
    poses = [pose_qualification("start-a", 0.0), pose_qualification("start-b", 0.1)]
    pairs = [
        {
            "base_condition_qualification_digest": bases[0]["qualification_digest"],
            "robot_start_pose_qualification_digest": poses[0]["qualification_digest"],
            "split_groups": ["TRAIN", "ID"],
        },
        {
            "base_condition_qualification_digest": bases[1]["qualification_digest"],
            "robot_start_pose_qualification_digest": poses[1]["qualification_digest"],
            "split_groups": ["OOD"],
        },
    ]
    pairs.sort(key=lambda item: (
        item["base_condition_qualification_digest"],
        item["robot_start_pose_qualification_digest"],
    ))
    catalog = redigest({
        "schema_version": "data_factory.fr5_qualification_catalog.v1",
        "source": "SYNTHETIC_TEST_ONLY",
        "qualification_status": "QUALIFIED",
        "fixed_contract_digest": digest(fixed),
        "coverage_report_digest": digest(report),
        "coverage_domain_digest": report["domain_digest"],
        "resolver_result_digests": sorted(digest(item) for item in resolvers),
        "base_condition_qualifications": bases,
        "robot_start_pose_qualifications": poses,
        "allowed_pairs": pairs,
    }, "catalog_digest")
    return compile_fr5_hypothesis(
        fixed_contract=fixed,
        coverage_report=report,
        resolver_results=resolvers,
        qualification_catalog=catalog,
    )


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
    hypothesis = synthetic_hypothesis()
    pair_for = {
        group: next(
            (pair["base_condition_digest"], pair["robot_start_pose_id"])
            for pair in hypothesis["allowed_pairs"] if group in pair["split_groups"]
        )
        for group in ("TRAIN", "ID", "OOD")
    }
    slot_values = (("train-a", "TRAIN"), ("id-a", "ID"), ("ood-b", "OOD"))
    slots = [{
        "slot_id": slot_id,
        "base_condition_digest": pair_for[group][0],
        "robot_start_pose_id": pair_for[group][1],
        "split_group": group,
        "repeat_index": 0,
        "hil_prompts": 1,
        "reviews": 1,
        "pending_reviews": 0,
        "storage_bytes": 100,
    } for slot_id, group in slot_values]
    manifest = compile_seed_manifest(
        manifest_id="synthetic-seed-r1",
        hypothesis=hypothesis,
        slots=slots,
        randomization_seed=17,
        manifest_budget={
            "max_physical_episodes": 4,
            "max_rollout_trials": 1,
            "max_hil_prompts": 4,
            "max_reviews": 4,
            "max_pending_reviews": 1,
            "max_storage_bytes": 400,
        },
        program_budget=program_budget(),
    )
    inventory = synthetic_inventory(root, manifest, hypothesis)
    groups = {"TRAIN": [], "ID": [], "OOD": []}
    for entry, slot in zip(inventory["episodes"], manifest["slots"]):
        groups[slot["split_group"]].append({
            "episode_index": entry["episode_index"],
            "episode_ref_digest": entry["episode_content_digest"],
            "training_approval_digest": entry["training_approval"]["artifact_digest"],
            "base_condition_digest": slot["base_condition_digest"],
            "robot_start_pose_id": slot["robot_start_pose_id"],
        })
    argv = ["lerobot-train", "--policy.type=smolvla", "--seed=17"]
    versions = runtime()
    split = compile_training_split(
        dataset={
            "dataset_root_identity_digest": inventory["dataset_identity"]["dataset_digest"],
            "repo_id": inventory["dataset_identity"]["repo_id"],
            "dataset_info_features_digest": digest("synthetic-dataset-features"),
            "total_episodes": len(inventory["episodes"]),
            "total_frames": 100 * len(inventory["episodes"]),
        },
        bindings={
            "collection_profile_digest": hypothesis["fixed_contract"]["collection_profile_digest"],
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
        "collection_profile_digest": hypothesis["fixed_contract"]["collection_profile_digest"],
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


def read_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def refresh_inventory_and_receipts(bundle: dict) -> None:
    inventory = bundle["approved_inventory"]
    inventory["inventory_digest"] = digest({
        key: inventory[key]
        for key in ("schema_version", "scope", "dataset_identity", "episodes")
    })
    split = bundle["split"]
    split["bindings"]["approved_episode_inventory_digest"] = inventory["inventory_digest"]
    train = bundle["training_receipt"]
    train["approved_episode_inventory_digest"] = inventory["inventory_digest"]
    refresh_split_and_reload(bundle)


def rewrite_provenances(bundle: dict, mutation) -> None:
    inventory = bundle["approved_inventory"]
    pairs = [
        (episode, read_json(episode["episode_provenance"]["artifact_path"]))
        for episode in inventory["episodes"]
    ]
    mutation([provenance for _, provenance in pairs])
    split_entries = {
        item["episode_index"]: item
        for episodes in bundle["split"]["episode_groups"].values()
        for item in episodes
    }
    for episode, provenance in pairs:
        _, provenance_digest = write_json(
            Path(episode["episode_provenance"]["artifact_path"]), provenance,
        )
        episode["episode_provenance"]["artifact_digest"] = provenance_digest
        approval_path = Path(episode["training_approval"]["artifact_path"])
        approval = read_json(str(approval_path))
        approval["episode_provenance_digest"] = provenance_digest
        _, approval_digest = write_json(approval_path, approval)
        episode["training_approval"]["artifact_digest"] = approval_digest
        split_entries[episode["episode_index"]]["training_approval_digest"] = approval_digest
    refresh_inventory_and_receipts(bundle)


def rewrite_resolved_job(bundle: dict, episode_index: int) -> None:
    inventory = bundle["approved_inventory"]
    episode = next(item for item in inventory["episodes"] if item["episode_index"] == episode_index)
    technical_path = Path(episode["technical_validator"]["artifact_path"])
    technical = read_json(str(technical_path))
    technical["resolved_job_digest"] = digest("synthetic-other-resolved-job")
    _, technical_digest = write_json(technical_path, technical)
    episode["technical_validator"]["artifact_digest"] = technical_digest

    semantic_path = Path(episode["human_semantic_evidence"]["artifact_path"])
    semantic = read_json(str(semantic_path))
    semantic["review_context_digest"] = digest({
        "run_id": technical["run_id"],
        "resolved_job_digest": technical["resolved_job_digest"],
        "plan_digest": technical["plan_digest"],
        "technical_validator_digest": technical_digest,
    })
    _, semantic_digest = write_json(semantic_path, semantic)
    episode["human_semantic_evidence"]["artifact_digest"] = semantic_digest

    provenance_path = Path(episode["episode_provenance"]["artifact_path"])
    provenance = read_json(str(provenance_path))
    provenance["technical_validator_digest"] = technical_digest
    provenance["resolved_job_digest"] = technical["resolved_job_digest"]
    _, provenance_digest = write_json(provenance_path, provenance)
    episode["episode_provenance"]["artifact_digest"] = provenance_digest

    approval_path = Path(episode["training_approval"]["artifact_path"])
    approval = read_json(str(approval_path))
    approval["technical_validator_digest"] = technical_digest
    approval["human_semantic_evidence_digest"] = semantic_digest
    approval["episode_provenance_digest"] = provenance_digest
    _, approval_digest = write_json(approval_path, approval)
    episode["training_approval"]["artifact_digest"] = approval_digest
    for episodes in bundle["split"]["episode_groups"].values():
        for item in episodes:
            if item["episode_index"] == episode_index:
                item["training_approval_digest"] = approval_digest
    refresh_inventory_and_receipts(bundle)


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

    def test_episode_provenance_and_manifest_slots_fail_closed(self) -> None:
        def duplicate_slot(provenances: list[dict]) -> None:
            provenances[1]["manifest_slot_id"] = provenances[0]["manifest_slot_id"]

        def swap_slots(provenances: list[dict]) -> None:
            provenances[0]["manifest_slot_id"], provenances[1]["manifest_slot_id"] = (
                provenances[1]["manifest_slot_id"], provenances[0]["manifest_slot_id"],
            )

        def wrong_repeat(provenances: list[dict]) -> None:
            provenances[0]["repeat_index"] += 1

        def wrong_group(provenances: list[dict]) -> None:
            next(item for item in provenances if item["split_group"] == "OOD")["split_group"] = "TRAIN"

        cases = (
            (duplicate_slot, "TRAINING_INVENTORY_DUPLICATE"),
            (swap_slots, "SOFTWARE_CONTRACT_MANIFEST_SLOT_BINDING"),
            (wrong_repeat, "SOFTWARE_CONTRACT_MANIFEST_SLOT_BINDING"),
            (wrong_group, "SOFTWARE_CONTRACT_MANIFEST_SLOT_BINDING"),
        )
        for mutation, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory(
                prefix="SYNTHETIC_TEST_ONLY-"
            ) as directory:
                bundle = synthetic_bundle(Path(directory))
                rewrite_provenances(bundle, mutation)
                with self.assertRaisesRegex(ContractError, code):
                    validate_software_contract(**bundle, expected_scope=SYNTHETIC_SCOPE)

    def test_manifest_slot_set_and_program_budget_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            bundle = synthetic_bundle(Path(directory))
            old_manifest = bundle["seed_manifest"]
            slots = [
                {key: value for key, value in item.items() if key != "order_index"}
                for item in old_manifest["slots"]
            ]
            extra = copy.deepcopy(next(item for item in slots if item["split_group"] == "OOD"))
            extra.update(slot_id="ood-extra", repeat_index=extra["repeat_index"] + 1)
            manifest = compile_seed_manifest(
                manifest_id=old_manifest["manifest_id"],
                hypothesis=bundle["hypothesis"],
                slots=[*slots, extra],
                randomization_seed=old_manifest["randomization_seed"],
                manifest_budget=old_manifest["manifest_budget"],
                program_budget=old_manifest["program_budget"],
            )
            bundle["seed_manifest"] = manifest
            bundle["split"]["bindings"]["episode_manifest_digest"] = manifest["manifest_digest"]
            bundle["training_receipt"]["episode_manifest_digest"] = manifest["manifest_digest"]
            refresh_split_and_reload(bundle)
            with self.assertRaisesRegex(ContractError, "SOFTWARE_CONTRACT_MANIFEST_SLOT_SET"):
                validate_software_contract(**bundle, expected_scope=SYNTHETIC_SCOPE)

        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            bundle = synthetic_bundle(Path(directory))
            bundle["split"]["evaluation_contract"]["program_budget"]["max_rounds"] += 1
            refresh_split_and_reload(bundle)
            with self.assertRaisesRegex(ContractError, "SOFTWARE_CONTRACT_PROGRAM_BUDGET"):
                validate_software_contract(**bundle, expected_scope=SYNTHETIC_SCOPE)

    def test_resolved_job_must_match_qualified_base(self) -> None:
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC_TEST_ONLY-") as directory:
            bundle = synthetic_bundle(Path(directory))
            rewrite_resolved_job(bundle, 0)
            with self.assertRaisesRegex(ContractError, "SOFTWARE_CONTRACT_MANIFEST_SLOT_BINDING"):
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
