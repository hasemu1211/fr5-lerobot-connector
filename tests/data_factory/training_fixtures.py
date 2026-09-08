"""Shared synthetic approval/launch/checkpoint fixtures; no real authority or model."""

import json
from pathlib import Path
from types import SimpleNamespace

from tools.data_factory import training_approval as approval
from tools.data_factory.training_approval import (
    APPROVAL_SCHEMA, PROVENANCE, SYNTHETIC_SCOPE, compile_episode_training_provenance,
)
from tools.data_factory.training_entrypoint import options, prepare_launch
from tools.fr5_data_factory import canonical_digest
from tools.fr5_training_profile import build_profile, policy_metadata


D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
D3 = "sha256:" + "3" * 64


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


def launch_fixture(root):
    import pyarrow as pa
    import pyarrow.parquet as pq

    selected = [0, 2, 3]
    fixtures = [synthetic_fixture(root, f"episode-{index}", index) for index in selected]
    dataset = Path(fixtures[0][0]["dataset_root"])
    metadata = dataset / "meta"
    (metadata / "episodes/chunk-000").mkdir(parents=True)
    (metadata / "source_provenance").mkdir()
    from tools.fr5_dataset_schema import dataset_features
    info = {"codebase_version": "v3.0", "fps": 30, "total_episodes": 4, "total_frames": 8,
            "features": dataset_features(fps=30, height=480, width=640, cameras=("up", "wrist"), use_videos=True)}
    write_json(metadata / "info.json", info)
    rows = []
    for i in range(4):
        row = {"episode_index": i, "tasks": ["pick up the cube and place it at the destination"], "length": 2}
        for key in ("action", "observation.state", "observation.images.up", "observation.images.wrist"):
            for name in ("min", "max", "mean", "std", "count"):
                number = 1.0 if name == "std" else float(i * 100)
                row[f"stats/{key}/{name}"] = ([2] if name == "count" else
                    [[[number]]] * 3 if key.startswith("observation.images.") else [number] * 7)
        rows.append(row)
    pq.write_table(pa.Table.from_pylist(rows), metadata / "episodes/chunk-000/file-000.parquet")
    for index in range(4):
        (metadata / f"source_provenance/episode-{index:06d}.jsonl").write_text('{"frame_index":0}\n{"frame_index":1}\n')
    identity = approval.current_dataset_identity(dataset, repo_id="tests/synthetic-dataset", dataset_id="synthetic-dataset-r1")
    entries, request_episodes = [], []
    for _, technical, semantic, approved, entry in fixtures:
        index = entry["episode_index"]
        content = approval.current_episode_digest(identity, index)
        semantic["checklist_id"] = "pick-place-v1"
        semantic_path = Path(entry["human_semantic_evidence"]["artifact_path"])
        _, semantic_digest = write_json(semantic_path, semantic)
        entry["human_semantic_evidence"]["artifact_digest"] = semantic_digest
        provenance_path = Path(entry["episode_provenance"]["artifact_path"])
        provenance = json.loads(provenance_path.read_text())
        provenance.update(scope=approval.PRODUCTION_SCOPE, dataset_identity_digest=canonical_digest(identity), episode_content_digest=content)
        _, provenance_digest = write_json(provenance_path, provenance)
        entry["episode_provenance"]["artifact_digest"] = provenance_digest
        approved.update(scope=approval.PRODUCTION_SCOPE, dataset_identity=identity, episode_content_digest=content,
                        episode_provenance_digest=provenance_digest, human_semantic_evidence_digest=semantic_digest)
        _, approved_digest = write_json(Path(entry["training_approval"]["artifact_path"]), approved)
        entry["training_approval"]["artifact_digest"] = approved_digest
        entry.update(dataset_identity_digest=canonical_digest(identity), episode_content_digest=content)
        entries.append(entry)
        request_episodes.append({"episode_id": entry["episode_id"], "episode_index": index,
            "technical_validator_path": entry["technical_validator"]["artifact_path"],
            "human_semantic_evidence_path": str(semantic_path),
            "seed_manifest_path": str(root / f"{entry['episode_id']}.seed-manifest.SYNTHETIC_TEST_ONLY.json"),
            "manifest_slot_id": provenance["manifest_slot_id"]})
    inventory = approval.build_training_approved_inventory(scope=approval.PRODUCTION_SCOPE, dataset_identity=identity, episodes=entries)
    inventory_path = root / "training_approved.json"
    write_json(inventory_path, inventory)
    output = root / "outputs/run"
    argv = ["fixture-lerobot-train", *build_profile("act", policy_metadata(info)),
            f"--dataset.root={dataset}", "--dataset.repo_id=tests/synthetic-dataset", "--dataset.episodes=[0,2,3]",
            "--dataset.eval_split=0.34", f"--output_dir={output}", "--batch_size=2", "--steps=2", "--eval_steps=1", "--save_freq=1"]
    kwargs = dict(dataset=dataset, repo_id="tests/synthetic-dataset", inventory=inventory_path,
                  profile="act", collection_profile="fr5-up-wrist-rgb-30hz-v2", argv=argv)
    request = {"dataset_root": str(dataset), "dataset_id": identity["dataset_id"], "repo_id": identity["repo_id"], "episodes": request_episodes}
    return kwargs, request, inventory


def write_normalization_fixture(policy, receipt):
    """Synthetic processor state only; never a real policy/checkpoint success claim."""
    import numpy as np
    from safetensors.numpy import save_file

    stats = {f"{key}.{name}": np.asarray(value, dtype=np.float32)
             for key, values in receipt["normalization"]["stats"].items() for name, value in values.items()}
    for pipeline, registry in (("policy_preprocessor", "normalizer_processor"),
                               ("policy_postprocessor", "unnormalizer_processor")):
        state_file = f"{pipeline}_normalization.safetensors"
        save_file(stats, policy / state_file)
        config = {"features": {"observation.state": {"type": "STATE", "shape": [7]},
                               "action": {"type": "ACTION", "shape": [7]}},
                  "norm_map": {"STATE": "MEAN_STD", "ACTION": "MEAN_STD"}}
        write_json(policy / f"{pipeline}.json", {"steps": [
            {"registry_name": registry, "state_file": state_file, "config": config}]})


def _write_checkpoint_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def admitted_case(root: Path) -> tuple[SimpleNamespace, dict]:
    kwargs, _, _ = launch_fixture(root)
    info = json.loads((kwargs["dataset"] / "meta/info.json").read_text())
    output = root / "outputs/run"
    kwargs.update(profile="smolvla", argv=[
        "fixture-lerobot-train",
        *build_profile("smolvla", policy_metadata(info)),
        f"--dataset.root={kwargs['dataset']}",
        f"--dataset.repo_id={kwargs['repo_id']}",
        "--dataset.episodes=[0,2,3]",
        "--dataset.eval_split=0.34",
        f"--output_dir={output}",
        "--batch_size=2", "--steps=2", "--eval_steps=1", "--save_freq=1",
    ])
    split, receipt = prepare_launch(**kwargs)
    _write_checkpoint_json(output / "fr5_training_split.json", split)
    _write_checkpoint_json(output / "fr5_training_receipt.json", receipt)

    policy_dir = output / "checkpoints/000001/pretrained_model"
    state_dir = policy_dir.parent / "training_state"
    policy_dir.mkdir(parents=True)
    state_dir.mkdir()
    config = {
        "scheduler": None,
        "dataset": {
            "root": str(kwargs["dataset"]), "repo_id": kwargs["repo_id"],
            "episodes": [0, 2, 3], "eval_split": 0.34,
        },
        "policy": {},
        "rename_map": {},
    }
    for key, value in options(split["feature_contract"]["policy_argv"]).items():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        if key.startswith("--policy."):
            config["policy"][key.removeprefix("--policy.")] = parsed
        elif key == "--rename_map":
            config["rename_map"] = parsed
    _write_checkpoint_json(policy_dir / "config.json", config["policy"])
    _write_checkpoint_json(policy_dir / "train_config.json", config)
    (policy_dir / "model.safetensors").write_bytes(b"fixture-model")
    _write_checkpoint_json(state_dir / "optimizer_param_groups.json", {})
    (state_dir / "optimizer_state.safetensors").write_bytes(b"fixture-optimizer")
    (state_dir / "rng_state.safetensors").write_bytes(b"fixture-rng")
    _write_checkpoint_json(state_dir / "training_step.json", {"step": 1})
    write_normalization_fixture(policy_dir, receipt)
    args = SimpleNamespace(
        checkpoint=str(policy_dir), dataset=kwargs["dataset"], repo_id=kwargs["repo_id"],
        approved_inventory=kwargs["inventory"], episodes=None, batch_size=1,
        num_workers=0, max_batches=0, output=root / "evaluation.json",
        seed=1000, device="cpu", use_amp=False,
    )
    return args, split
