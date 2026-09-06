"""Public offline admission, human approval and delegated local launch connection.

Human approval still requires /dev/tty or the trusted Web UI. A separate,
recorded user-authorized delegation may authorize bounded local training without
being relabeled as a human approval.
"""
from __future__ import annotations

import sys
# Direct execution must not shadow stdlib operator with data_factory/operator.
if __package__ in {None, ""}:
    sys.path.pop(0)

import argparse
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import shlex

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.data_factory import training_approval as approval
from tools.data_factory.training_receipts import compile_launch_receipt, launch_receipt_digest
from tools.data_factory.training_split import compile_launch_split
from tools.fr5_data_factory import ContractError, TASK_CONTRACTS, canonical_digest, load_json_strict
from tools.fr5_training_profile import instruction_task, launch_feature_contract, read_metadata


def options(argv: list[str]) -> dict[str, str]:
    """Reject duplicates: the checked value must be the value consumed by LeRobot."""
    result = {}
    index = 0
    while index < len(argv):
        argument = argv[index]
        if not argument.startswith("--"):
            raise ContractError("TRAINING_ARGUMENT")
        key, equal, value = argument.partition("=")
        if not equal:
            index += 1
            if index >= len(argv) or argv[index].startswith("--"):
                raise ContractError("TRAINING_ARGUMENT_VALUE")
            value = argv[index]
        if key in result:
            raise ContractError("TRAINING_DUPLICATE_ARGUMENT", key)
        result[key] = value
        index += 1
    return result


def selected_episodes(value: str | None, total: int) -> list[int]:
    selected = list(range(total)) if value is None else json.loads(value)
    if (not isinstance(selected, list) or not selected
            or any(type(i) is not int or not 0 <= i < total for i in selected)
            or selected != sorted(set(selected))):
        raise ContractError("TRAINING_SELECTED_EPISODE_SET")
    return selected


def check_inventory(dataset: Path, repo_id: str, inventory: Path, episodes: str | None) -> tuple[dict, dict, list[int]]:
    # Validate authorization before any LeRobot dataset construction or technical decode.
    approved = approval.validate_current_training_inventory(inventory, dataset_root=dataset, repo_id=repo_id)
    metadata = read_metadata(dataset)
    selected = selected_episodes(episodes, metadata["total_episodes"])
    if selected != [episode["episode_index"] for episode in approved["episodes"]]:
        raise ContractError("TRAINING_SELECTED_EPISODE_SET")
    return approved, metadata, selected


def _positive_option(config: dict[str, str], key: str) -> int:
    try:
        value = int(config[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("TRAINING_DELEGATION_LIMITS") from exc
    if value < 1:
        raise ContractError("TRAINING_DELEGATION_LIMITS")
    return value


def _enforce_delegated_launch(
    inventory: dict, *, dataset: Path, repo_id: str, profile: str,
    config: dict[str, str],
) -> bool:
    delegation = approval.inventory_local_training_delegation(inventory)
    if delegation is None:
        return False
    if (
        delegation["dataset"] != {"dataset_root": str(dataset.resolve()), "repo_id": repo_id}
        or profile not in delegation["profiles"]
        or config.get("--job.target", "local") != "local"
        or config.get("--dataset.streaming", "false") != "false"
        or config.get("--dataset.repo_type", "dataset") != "dataset"
        or any(key.startswith(("--env.", "--reward_model.")) for key in config)
        or config.get("--policy.push_to_hub") != "false"
        or config.get("--save_checkpoint_to_hub") != "false"
        or config.get("--wandb.enable") != "false"
    ):
        raise ContractError("TRAINING_DELEGATION_SCOPE")
    output_root = Path(delegation["output_root"])
    if output_root.is_symlink() or not output_root.is_dir():
        raise ContractError("TRAINING_DELEGATION_OUTPUT")
    if "--output_dir" not in config:
        raise ContractError("TRAINING_DELEGATION_OUTPUT")
    output = Path(config.get("--output_dir", "")).resolve()
    if not output.is_relative_to(output_root.resolve()):
        raise ContractError("TRAINING_DELEGATION_OUTPUT")
    steps = _positive_option(config, "--steps")
    batch_size = _positive_option(config, "--batch_size")
    save_frequency = _positive_option(config, "--save_freq")
    limits = delegation["limits"]
    save_checkpoints = config.get("--save_checkpoint", "true")
    if save_checkpoints not in {"true", "false"}:
        raise ContractError("TRAINING_DELEGATION_LIMITS")
    checkpoints = math.ceil(steps / save_frequency) if save_checkpoints == "true" else 0
    if (
        steps > limits["max_steps"]
        or batch_size > limits["max_batch_size"]
        or checkpoints > limits["max_checkpoints"]
    ):
        raise ContractError("TRAINING_DELEGATION_LIMITS")
    return True


def _saved_launch_options(saved: dict, output: Path) -> dict[str, str]:
    """Project the fields the resume CLI will consume from train_config.json."""
    dataset, policy = saved.get("dataset"), saved.get("policy")
    wandb, job = saved.get("wandb"), saved.get("job")
    if not all(isinstance(value, dict) for value in (dataset, policy, wandb, job)):
        raise ContractError("TRAINING_DELEGATION_SAVED_CONFIG")

    def boolean(value: object) -> str:
        if type(value) is not bool:
            raise ContractError("TRAINING_DELEGATION_SAVED_CONFIG")
        return str(value).lower()

    config = {
        "--dataset.root": str(dataset.get("root", "")),
        "--dataset.repo_id": str(dataset.get("repo_id", "")),
        "--dataset.repo_type": str(dataset.get("repo_type", "dataset")),
        "--dataset.streaming": boolean(dataset.get("streaming", False)),
        "--output_dir": str(output),
        "--steps": str(saved.get("steps", "")),
        "--batch_size": str(saved.get("batch_size", "")),
        "--save_freq": str(saved.get("save_freq", "")),
        "--save_checkpoint": boolean(saved.get("save_checkpoint")),
        "--policy.push_to_hub": boolean(policy.get("push_to_hub")),
        "--save_checkpoint_to_hub": boolean(saved.get("save_checkpoint_to_hub")),
        "--wandb.enable": boolean(wandb.get("enable")),
    }
    if job.get("target") is not None:
        config["--job.target"] = str(job["target"])
    if saved.get("env") is not None:
        config["--env.saved"] = "configured"
    if saved.get("reward_model") is not None:
        config["--reward_model.saved"] = "configured"
    return config


def _delegated_training(split: dict, receipt: dict) -> bool:
    inventory = approval.validate_current_training_inventory(
        receipt["approved_inventory_path"],
        dataset_root=split["dataset_identity"]["dataset_root"],
        repo_id=split["repo_id"], selected_episodes=split["selected_episodes"],
    )
    return approval.inventory_local_training_delegation(inventory) is not None


def prepare_launch(*, dataset: Path, repo_id: str, inventory: Path,
                   profile: str, collection_profile: str, argv: list[str]) -> tuple[dict, dict]:
    from importlib.metadata import version

    if version("lerobot") != "0.6.1":
        raise ContractError("TRAINING_SPLIT_RUNTIME_UNVERIFIED", "This split adapter is verified against LeRobot 0.6.1")
    config = options(argv[1:])
    if config.get("--job.target", "local") != "local" or any(key.startswith("--env.") for key in config):
        raise ContractError("TRAINING_LOCAL_OFFLINE_ONLY")
    # Streaming, renamed roots, alternate episode order and resume are separate contracts.
    if (not config.get("--dataset.root")
            or Path(config["--dataset.root"]).expanduser().resolve() != dataset.expanduser().resolve()
            or config.get("--dataset.repo_id") != repo_id
            or config.get("--dataset.streaming", "false") != "false"
            or "--config_path" in config or "--resume" in config):
        raise ContractError("TRAINING_COMMAND_DATASET")
    approved, metadata, selected = check_inventory(dataset, repo_id, inventory, config.get("--dataset.episodes"))
    _enforce_delegated_launch(
        approved, dataset=dataset, repo_id=repo_id, profile=profile, config=config,
    )
    checklist_to_task = {contract["review_checklist_id"]: task for task, contract in TASK_CONTRACTS.items()}
    tasks = set()
    for episode in approved["episodes"]:
        semantic = load_json_strict(Path(episode["human_semantic_evidence"]["artifact_path"]))
        task = checklist_to_task[semantic["checklist_id"]]
        labels = metadata["episode_tasks"][episode["episode_index"]]
        if not labels or any(instruction_task(label) != task for label in labels):
            raise ContractError("TRAINING_TASK_EVIDENCE_BINDING")
        tasks.add(task)
    # ACT/VQ-BeT have no language conditioning; keep the first baseline one task/instruction.
    if len(tasks) != 1 or (profile != "smolvla" and len({tuple(metadata["episode_tasks"][i]) for i in selected}) != 1):
        raise ContractError("TRAINING_MIXED_TASK_SCOPE")
    feature = launch_feature_contract(profile, collection_profile, tasks.pop(), metadata)
    for episode in approved["episodes"]:
        provenance = load_json_strict(Path(episode["episode_provenance"]["artifact_path"]))
        if provenance["schema_version"] == approval.DERIVED_PROVENANCE_SCHEMA:
            provenance = provenance["parent"]["provenance"]
        if provenance["schema_version"] == approval.LEDGER_PROVENANCE_SCHEMA:
            ledger = load_json_strict(Path(provenance["episode_ledger"]["artifact_path"]))
            if ledger["bindings"]["collection_profile_digest"] != feature["collection_profile_digest"]:
                raise ContractError("TRAINING_COLLECTION_PROFILE_LEDGER_BINDING")
    for key, value in options(feature["policy_argv"]).items():
        if config.get(key) != value:
            if key == "--policy.path" and profile == "smolvla":
                # The receipt compiler validates and binds the exact local parent.
                continue
            raise ContractError("TRAINING_POLICY_FEATURE_BINDING")
    split = compile_launch_split(
        inventory=approved, metadata=metadata, selected=selected,
        fraction=float(config["--dataset.eval_split"]), feature_contract=feature,
    )
    receipt = compile_launch_receipt(split, argv, str(inventory))
    # Validate and persist the single saved observation-view binding before any
    # trainer construction.  Curator remains the producer of derived evidence;
    # this call only consumes it and records the exact raw/baked representation.
    from tools.validate_training_checkpoint import validate_saved_observation_view
    receipt["observation_view"] = validate_saved_observation_view(split, receipt)
    receipt["receipt_digest"] = launch_receipt_digest(receipt)
    if "initialization" in receipt:
        parent_output = Path(receipt["initialization"]["checkpoint"]).parents[2]
        if Path(config["--output_dir"]).resolve().is_relative_to(parent_output):
            raise ContractError("TRAINING_OUTPUT_INSIDE_PARENT")
    return split, receipt


def launch(*, dataset: Path, repo_id: str, inventory: Path, profile: str,
           collection_profile: str, argv: list[str], dry_run: bool = False,
           runner=None) -> int:
    kwargs = dict(dataset=dataset, repo_id=repo_id, inventory=inventory, profile=profile,
                  collection_profile=collection_profile, argv=argv)
    split, receipt = prepare_launch(**kwargs)
    output = Path(options(argv[1:])["--output_dir"])
    pending = [Path(str(output) + f".{name}.pending") for name in ("fr5_training_split.json", "fr5_training_receipt.json")]
    if output.exists() or output.is_symlink() or any(p.exists() or p.is_symlink() for p in pending):
        raise ContractError("TRAINING_OUTPUT_EXISTS")
    if output.resolve().is_relative_to(dataset.resolve()):
        raise ContractError("TRAINING_OUTPUT_INSIDE_DATASET")
    if dry_run:
        print(json.dumps({"split": split, "receipt": receipt}, sort_keys=True))
        print("Command: " + shlex.join(argv))
        return 0
    # Recheck all bytes/references immediately before the first output write.
    if prepare_launch(**kwargs) != (split, receipt):
        raise ContractError("TRAINING_INPUT_CHANGED")
    local_offline = _delegated_training(split, receipt)
    output.parent.mkdir(parents=True, exist_ok=True)
    written = []
    try:
        for path, value in zip(pending, (split, receipt)):
            approval._write_exclusive(path, value, "TRAINING_OUTPUT_EXISTS")
            written.append(path)
        if runner is not None:
            with approval.local_hf_offline(local_offline):
                return runner(argv, check=False).returncode
        return run_native_training(argv, split, receipt)
    finally:
        for path in written:
            if output.is_dir() and not output.is_symlink():
                target = output / path.name.removeprefix(output.name + ".").removesuffix(".pending")
                # No overwrite of another producer's receipt.
                if target.exists() or target.is_symlink():
                    raise ContractError("TRAINING_OUTPUT_EXISTS", str(target))
                path.rename(target)
            else:
                path.unlink()


def run_native_training(argv: list[str], split: dict, receipt: dict) -> int:
    with approval.local_hf_offline(_delegated_training(split, receipt)):
        return _run_native_training(argv, split, receipt)


def _run_native_training(argv: list[str], split: dict, receipt: dict) -> int:
    """Use the official trainer with admitted partitions and TRAIN-only normalization.

    LeRobot 0.6.1 selects samples but retains global metadata statistics. Adapt
    its dataset factory in this process only, before policy/processors are made.
    """
    from lerobot.scripts import lerobot_train
    import numpy as np

    original_factory = lerobot_train.make_train_eval_datasets
    original_policy_factory = getattr(lerobot_train, "make_policy", None)
    original_argv = sys.argv

    def admitted_datasets(cfg):
        dataset = cfg.dataset
        if "initialization" in receipt and not cfg.resume:
            from tools.validate_training_checkpoint import warm_start_binding

            parent = receipt["initialization"]["checkpoint"]
            if (str(cfg.policy.pretrained_path) != parent
                    or warm_start_binding(Path(parent), split, receipt["normalization"]) != receipt["initialization"]):
                raise ContractError("TRAINING_RUNTIME_WARM_START")
        if (Path(dataset.root).expanduser().resolve() != Path(split["dataset_identity"]["dataset_root"]).expanduser().resolve()
                or dataset.repo_id != split["repo_id"]
                or (dataset.episodes or list(range(split["total_episodes"]))) != split["selected_episodes"]
                or dataset.eval_split != split["eval_split"] or dataset.streaming
                or dataset.use_imagenet_stats != receipt["normalization"]["use_imagenet_stats"]):
            raise ContractError("TRAINING_RUNTIME_DATASET")
        train, heldout = original_factory(cfg)
        for actual, expected in ((train, split["train_episodes"]), (heldout, split["eval_episodes"])):
            if actual is None or actual.episodes != expected:
                raise ContractError("TRAINING_RUNTIME_SPLIT")
            actual.meta.stats = {key: {name: np.asarray(value) for name, value in stats.items()}
                                 for key, stats in receipt["normalization"]["stats"].items()}
        return train, heldout

    def admitted_policy(*args, **kwargs):
        from tools.data_factory.training_receipts import tree_digest

        policy = original_policy_factory(*args, **kwargs)
        initialization = receipt["initialization"]
        if tree_digest(Path(initialization["checkpoint"]).parent) != initialization["checkpoint_artifact_digest"]:
            raise ContractError("TRAINING_RUNTIME_WARM_START")
        return policy

    try:
        sys.argv = list(argv)
        lerobot_train.make_train_eval_datasets = admitted_datasets
        if "initialization" in receipt:
            lerobot_train.make_policy = admitted_policy
        lerobot_train.main()
        return 0
    finally:
        lerobot_train.make_train_eval_datasets = original_factory
        if "initialization" in receipt:
            lerobot_train.make_policy = original_policy_factory
        sys.argv = original_argv


def resume_training(checkpoint: Path) -> int:
    from tools.validate_training_checkpoint import validate_checkpoint

    policy, output = validate_checkpoint(checkpoint)
    # Checkpoint validation accepts pending manifests from an interrupted launch.
    values = []
    for name in ("fr5_training_split.json", "fr5_training_receipt.json"):
        path = output / name
        if not path.is_file():
            path = Path(str(output) + f".{name}.pending")
        values.append(load_json_strict(path))
    split, receipt = values
    inventory = approval.validate_current_training_inventory(
        receipt["approved_inventory_path"],
        dataset_root=split["dataset_identity"]["dataset_root"],
        repo_id=split["repo_id"], selected_episodes=split["selected_episodes"],
    )
    delegated = _enforce_delegated_launch(
        inventory, dataset=Path(split["dataset_identity"]["dataset_root"]),
        repo_id=split["repo_id"], profile=split["feature_contract"]["profile"],
        config=options(receipt["normalized_argv"][1:]),
    )
    if delegated:
        saved = load_json_strict(policy / "train_config.json")
        _enforce_delegated_launch(
            inventory, dataset=Path(split["dataset_identity"]["dataset_root"]),
            repo_id=split["repo_id"], profile=split["feature_contract"]["profile"],
            config=_saved_launch_options(saved, output),
        )
    return run_native_training([receipt["normalized_argv"][0], "--resume=true",
        f"--config_path={policy / 'train_config.json'}", f"--output_dir={output}"],
        split, receipt)


def prepare_approvals(request: dict, output: Path, approved_by: str) -> tuple[dict, list[dict]]:
    return _prepare_approvals(request, output, approved_by, check_targets=True)


def _prepare_approvals(request: dict, output: Path, approved_by: str, *, check_targets: bool) -> tuple[dict, list[dict]]:
    fields = {"dataset_root", "dataset_id", "repo_id", "episodes"}
    approval._exact(request, frozenset(fields | ({"derivation"} if "derivation" in request else set())), "TRAINING_PREAPPROVAL_FIELDS")
    dataset = approval.current_dataset_identity(request["dataset_root"], repo_id=request["repo_id"], dataset_id=request["dataset_id"])
    if output.resolve().is_relative_to(Path(dataset["dataset_root"])) or output.is_symlink() or not output.is_dir():
        raise ContractError("TRAINING_APPROVAL_OUTPUT_EXTERNAL_DIRECTORY")
    approval._id(approved_by, "TRAINING_APPROVER_ID")
    metadata = read_metadata(Path(dataset["dataset_root"]))
    sources = request["episodes"]
    if not isinstance(sources, list) or not sources:
        raise ContractError("TRAINING_INVENTORY_EPISODES")
    indices = [entry["episode_index"] for entry in sources]
    selected_episodes(json.dumps(indices), metadata["total_episodes"])
    if "derivation" in request:
        evidence = approval._derived_publication(request["derivation"], dataset)
        parent = evidence["parent_dataset_identity"]
        protected = [Path(request["derivation"]["run_directory"])]
        protected.extend(Path(entry["episode_ledger_path"]).parent for entry in sources if "episode_ledger_path" in entry)
        if any(output.resolve().is_relative_to(path.resolve()) for path in protected):
            raise ContractError("TRAINING_DERIVATION_OUTPUT_OVERLAP")
        parent_request = {key: parent[key] for key in ("dataset_root", "dataset_id", "repo_id")}
        parent_request["episodes"] = sources
        parent_dataset, drafts = _prepare_approvals(parent_request, output, approved_by, check_targets=check_targets)
        if parent_dataset != parent:
            raise ContractError("TRAINING_DERIVATION_PARENT")
        for draft in drafts:
            provenance = approval.compile_derived_training_provenance(
                dataset=dataset, derivation=request["derivation"], parent_draft=draft,
            )
            args = draft["approval_arguments"]
            args.update(dataset_identity=dataset,
                        episode_content_digest=provenance["episode_content_digest"],
                        technical_validator_path=evidence["technical"]["artifact_path"],
                        technical_validator_digest=evidence["technical"]["artifact_digest"],
                        episode_provenance_digest=canonical_digest(provenance))
            draft["provenance"] = provenance
        return dataset, drafts
    drafts = []
    for source in sources:
        base_keys = {"episode_id", "episode_index", "technical_validator_path", "human_semantic_evidence_path"}
        source_keys = {"episode_ledger_path"} if "episode_ledger_path" in source else {"seed_manifest_path", "manifest_slot_id"}
        approval._exact(source, frozenset(base_keys | source_keys), "TRAINING_PREAPPROVAL_EPISODE_FIELDS")
        technical_path = Path(source["technical_validator_path"]).resolve()
        semantic_path = Path(source["human_semantic_evidence_path"]).resolve()
        technical_digest = canonical_digest(load_json_strict(technical_path))
        semantic_digest = canonical_digest(load_json_strict(semantic_path))
        technical = approval._technical(str(technical_path), technical_digest, episode_id=source["episode_id"], dataset_root=dataset["dataset_root"])
        semantic = approval._semantic(str(semantic_path), semantic_digest, episode_id=source["episode_id"], technical=technical)
        content_digest = approval.current_episode_digest(dataset, source["episode_index"])
        if "episode_ledger_path" in source:
            provenance = approval.compile_ledger_training_provenance(dataset_identity=dataset, episode_ledger_path=source["episode_ledger_path"])
            if (provenance["episode_id"] != source["episode_id"] or provenance["episode_index"] != source["episode_index"]
                    or provenance["technical_validator_digest"] != technical_digest):
                raise ContractError("TRAINING_LEDGER_BINDING")
        else:
            provenance = approval.compile_episode_training_provenance(
                scope=approval.PRODUCTION_SCOPE, dataset_identity=dataset,
                episode_id=source["episode_id"], episode_index=source["episode_index"], episode_content_digest=content_digest,
                technical_validator_path=technical_path, technical_validator_digest=technical_digest,
                seed_manifest=source["seed_manifest_path"], manifest_slot_id=source["manifest_slot_id"],
            )
        provenance_path = output / f"{source['episode_id']}.provenance.json"
        target = output / f"{source['episode_id']}.approval.json"
        if check_targets:
            for path in (provenance_path, target):
                approval._target(path, "TRAINING_APPROVAL_EXISTS")
        kwargs = dict(scope=approval.PRODUCTION_SCOPE, dataset_identity=dataset,
            episode_id=source["episode_id"], episode_index=source["episode_index"], episode_content_digest=content_digest,
            technical_validator_path=str(technical_path), technical_validator_digest=technical_digest,
            human_semantic_evidence_path=str(semantic_path), human_semantic_evidence_digest=semantic_digest,
            episode_provenance_path=str(provenance_path), episode_provenance_digest=canonical_digest(provenance), approved_by=approved_by)
        drafts.append({"output_path": str(target), "approval_arguments": kwargs, "provenance": provenance,
                       "reviewer_id": semantic["reviewed_by"]})
    approval._unique_episodes([d["approval_arguments"] for d in drafts], [d["provenance"] for d in drafts])
    if check_targets:
        approval._target(output / "training_approved.json", "TRAINING_INVENTORY_EXISTS")
    return dataset, drafts


def _batch_summary(dataset: dict, drafts: list[dict], batch_digest: str, output: Path, approved_by: str) -> str:
    lines = [
        "HUMAN TRAINING APPROVAL — EXACT FROZEN BATCH",
        f"Dataset: {dataset['dataset_id']}  Repo: {dataset['repo_id']}",
        f"Root: {dataset['dataset_root']}",
        f"Frozen revision: {dataset['dataset_digest']}",
        f"Approver: {approved_by}",
        f"Selected episodes ({len(drafts)}): " + ", ".join(str(d["approval_arguments"]["episode_index"]) for d in drafts),
    ]
    for draft in drafts:
        args = draft["approval_arguments"]
        lines.extend([
            f"  [{args['episode_index']}] {args['episode_id']} — technical PASS; " + (
                "parent semantic PASS only; child semantic NOT_ASSERTED; bounded Curator visual publication"
                if draft["provenance"]["schema_version"] == approval.DERIVED_PROVENANCE_SCHEMA
                else f"semantic PASS by {draft['reviewer_id']}"),
            f"    Content: {args['episode_content_digest']}",
            f"    Technical: {args['technical_validator_digest']} ({args['technical_validator_path']})",
            f"    Semantic: {args['human_semantic_evidence_digest']} ({args['human_semantic_evidence_path']})",
            f"    Provenance: {args['episode_provenance_digest']} ({draft['provenance']['schema_version']})",
        ])
    lines.extend([
        f"Exact batch binding: {batch_digest}",
        f"New inventory: {output / 'training_approved.json'}",
        "One decision approves every listed episode in this revision for training admission.",
        "It does not start training or grant robot/hardware execution authority.",
        "Any other response refuses the whole batch; no approval or inventory will be published.",
    ])
    return "\n".join(lines)


@dataclass(frozen=True)
class PreparedApprovalBatch:
    """Server-held value, never deserialized from browser input or a consent token.

    Preparing or displaying it grants no approval. The server owns the human
    decision, configured approver identity and access to publish_approval_batch.
    """

    _snapshot: str

    @property
    def preview(self) -> dict:
        value = json.loads(self._snapshot)
        return {
            "status": "PREVIEW_NOT_APPROVED", "dataset_identity": value["dataset"],
            "selected_count": len(value["drafts"]),
            "episodes": [{"episode_id": draft["approval_arguments"]["episode_id"],
                          "episode_index": draft["approval_arguments"]["episode_index"],
                          "technical_status": "PASS", "semantic_status": (
                              "NOT_ASSERTED" if draft["provenance"]["schema_version"] == approval.DERIVED_PROVENANCE_SCHEMA else "PASS"),
                          "reviewer_id": draft["reviewer_id"],
                          **({"parent_semantic_status": "PASS",
                              "parent_dataset_identity": draft["provenance"]["parent"]["dataset_identity"],
                              "curator_review": draft["provenance"]["curator_review"]}
                             if draft["provenance"]["schema_version"] == approval.DERIVED_PROVENANCE_SCHEMA else {})} for draft in value["drafts"]],
            "batch_digest": value["batch_digest"], "starts_training": False,
            "limitations": ["Approves only this exact frozen batch for training admission.",
                            "Does not establish learning performance or authorize robot execution."],
        }


def _approval_documents(drafts: list[dict], reviewed_at: datetime) -> list[dict]:
    return [approval._prepare_training_approval(
        **{**draft["approval_arguments"], "episode_provenance_path": draft["provenance"]},
        clock=lambda: reviewed_at,
    ) for draft in drafts]


def _delegation_reference(path: Path, *, actor: str, dataset: dict) -> tuple[dict, dict]:
    path = path.expanduser()
    if path.is_symlink() or not path.is_file():
        raise ContractError("TRAINING_DELEGATION_ARTIFACT")
    path = path.resolve()
    value = load_json_strict(path)
    delegation = approval.validate_local_training_delegation(
        value, authorized_actor=actor, dataset=dataset,
    )
    return delegation, {"artifact_path": str(path), "artifact_digest": canonical_digest(value)}


def _delegation_output(delegation: dict, output: Path, dataset: dict) -> None:
    root = Path(delegation["output_root"])
    if root.is_symlink() or not root.is_dir():
        raise ContractError("TRAINING_DELEGATION_OUTPUT")
    resolved = output.resolve()
    if (
        output.is_symlink() or not output.is_dir()
        or not resolved.is_relative_to(root.resolve())
        or resolved.is_relative_to(Path(dataset["dataset_root"]))
    ):
        raise ContractError("TRAINING_DELEGATION_OUTPUT")


def _delegated_documents(
    drafts: list[dict], *, actor: str, authorized_at: datetime,
    delegation_reference: dict,
) -> list[dict]:
    documents = []
    for draft in drafts:
        human_shape = approval._prepare_training_approval(
            **{**draft["approval_arguments"], "episode_provenance_path": draft["provenance"]},
            clock=lambda: authorized_at,
        )
        documents.append({
            **{key: human_shape[key] for key in approval.APPROVAL_KEYS
               if key not in {"approved_by", "approved_at", "provenance"}},
            "schema_version": approval.DELEGATED_APPROVAL_SCHEMA,
            "authorized_actor": actor,
            "authorized_at": human_shape["approved_at"],
            "provenance": approval.DELEGATED_PROVENANCE,
            "delegation": copy.deepcopy(delegation_reference),
        })
    batch_digest = approval.delegated_batch_digest(documents)
    result = [{**document, "batch_digest": batch_digest} for document in documents]
    for document in result:
        approval.validate_training_authorization(document)
    return result


def delegate_training_batch(
    request: dict, output: Path, authorized_actor: str, delegation_path: Path,
    *, clock=lambda: datetime.now(timezone.utc),
) -> dict:
    """Issue an exact batch under recorded user-authorized local delegation."""
    request = copy.deepcopy(request)
    actor = approval._id(authorized_actor, "TRAINING_DELEGATION_ACTOR")
    output = output.resolve() if not output.is_symlink() else output
    dataset, drafts = prepare_approvals(request, output, actor)
    delegation, reference = _delegation_reference(
        delegation_path, actor=actor, dataset=dataset,
    )
    _delegation_output(delegation, output, dataset)
    authorized_at = clock()
    if not isinstance(authorized_at, datetime) or authorized_at.tzinfo is None:
        raise ContractError("TRAINING_AUTHORIZATION_TIME")
    documents = _delegated_documents(
        drafts, actor=actor, authorized_at=authorized_at,
        delegation_reference=reference,
    )
    directory = output.stat()
    value = {
        "request": request, "output": str(output), "approved_by": actor,
        "output_identity": [directory.st_dev, directory.st_ino],
        "dataset": dataset, "drafts": drafts, "documents": documents,
        "authorized_at": authorized_at.isoformat(),
        "delegation_path": reference["artifact_path"], "delegation": delegation,
        "delegation_reference": reference,
    }
    return _publish_authorization_batch(
        value, documents=documents, provenance=approval.DELEGATED_PROVENANCE,
        revalidate=_revalidate_delegated_batch,
    )


def _revalidate_delegated_batch(value: dict, *, check_targets: bool) -> None:
    output = Path(value["output"])
    if _prepare_approvals(
        value["request"], output, value["approved_by"], check_targets=check_targets,
    ) != (value["dataset"], value["drafts"]):
        raise ContractError("TRAINING_INPUT_CHANGED")
    delegation, reference = _delegation_reference(
        Path(value["delegation_path"]), actor=value["approved_by"], dataset=value["dataset"],
    )
    _delegation_output(delegation, output, value["dataset"])
    if (
        delegation != value["delegation"]
        or reference != value["delegation_reference"]
        or _delegated_documents(
            value["drafts"], actor=value["approved_by"],
            authorized_at=datetime.fromisoformat(value["authorized_at"]),
            delegation_reference=value["delegation_reference"],
        ) != value["documents"]
    ):
        raise ContractError("TRAINING_INPUT_CHANGED")


def prepare_approval_batch(request: dict, output: Path, approved_by: str) -> PreparedApprovalBatch:
    """Prepare without writes; arguments come from trusted server configuration."""
    request = copy.deepcopy(request)
    output = output.resolve() if not output.is_symlink() else output
    dataset, drafts = prepare_approvals(request, output, approved_by)
    reviewed_at = datetime.now(timezone.utc)
    documents = _approval_documents(drafts, reviewed_at)
    batch_digest = approval._batch_digest(documents)
    directory = output.stat()
    return PreparedApprovalBatch(json.dumps({
        "request": request, "output": str(output), "approved_by": approved_by,
        "output_identity": [directory.st_dev, directory.st_ino],
        "dataset": dataset, "drafts": drafts, "documents": documents,
        "reviewed_at": reviewed_at.isoformat(), "batch_digest": batch_digest,
    }, sort_keys=True, separators=(",", ":"), allow_nan=False))


def _revalidate_approval_batch(value: dict, *, check_targets: bool) -> None:
    # Reopen the complete source graph, even after per-episode publication.
    if _prepare_approvals(value["request"], Path(value["output"]), value["approved_by"],
                          check_targets=check_targets) != (value["dataset"], value["drafts"]):
        raise ContractError("TRAINING_INPUT_CHANGED")
    if _approval_documents(value["drafts"], datetime.fromisoformat(value["reviewed_at"])) != value["documents"]:
        raise ContractError("TRAINING_INPUT_CHANGED")


def publish_approval_batch(prepared: PreparedApprovalBatch) -> dict:
    """Publish only after the trusted caller's explicit human decision.

    No JSON/dict confirmation or callback is accepted. This function does not
    authenticate a human: the CLI or Web UI owns that interaction boundary.
    Concurrent publishers serialize on the existing output directory; exclusive
    artifacts reject replay. A partial attempt cannot publish an inventory.
    """
    if type(prepared) is not PreparedApprovalBatch:
        raise ContractError("TRAINING_PREPARED_BATCH_REQUIRED")
    value = json.loads(prepared._snapshot)
    documents = [
        {**document, "schema_version": approval.BATCH_APPROVAL_SCHEMA,
         "batch_digest": value["batch_digest"]}
        for document in value["documents"]
    ]
    for document in documents:
        approval.validate_training_approval(document)
    return _publish_authorization_batch(
        value, documents=documents, provenance=approval.PROVENANCE,
        revalidate=_revalidate_approval_batch,
    )


def _publish_authorization_batch(
    value: dict, *, documents: list[dict], provenance: str, revalidate,
) -> dict:
    """Publish either authority mode through one locked exact-batch transaction."""
    dataset, drafts = value["dataset"], value["drafts"]
    output = Path(value["output"])
    fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ContractError("TRAINING_APPROVAL_BUSY") from exc
        directory = os.fstat(fd)
        if [directory.st_dev, directory.st_ino] != value["output_identity"]:
            raise ContractError("TRAINING_APPROVAL_OUTPUT_CHANGED")
        revalidate(value, check_targets=True)
        entries = []
        for draft, document in zip(drafts, documents):
            args = draft["approval_arguments"]
            approval._write_exclusive(
                Path(args["episode_provenance_path"]), draft["provenance"],
                "TRAINING_APPROVAL_EXISTS",
            )
            approval._write_exclusive(
                Path(draft["output_path"]), document, "TRAINING_APPROVAL_EXISTS",
            )
            entries.append({
                "dataset_identity_digest": canonical_digest(dataset),
                "episode_id": args["episode_id"], "episode_index": args["episode_index"],
                "episode_content_digest": args["episode_content_digest"],
                "technical_validator": {"artifact_path": args["technical_validator_path"], "artifact_digest": args["technical_validator_digest"], "status": "PASS"},
                "human_semantic_evidence": {"artifact_path": args["human_semantic_evidence_path"], "artifact_digest": args["human_semantic_evidence_digest"], "status": ("PARENT_PASS" if draft["provenance"]["schema_version"] == approval.DERIVED_PROVENANCE_SCHEMA else "PASS"), "reviewer_id": draft["reviewer_id"]},
                "episode_provenance": {"artifact_path": args["episode_provenance_path"], "artifact_digest": args["episode_provenance_digest"]},
                "training_approval": {"artifact_path": draft["output_path"], "artifact_digest": canonical_digest(document), "provenance": provenance},
            })
        inventory = approval.build_training_approved_inventory(
            scope=approval.PRODUCTION_SCOPE, dataset_identity=dataset, episodes=entries,
        )
        revalidate(value, check_targets=False)
        directory = output.stat()
        if [directory.st_dev, directory.st_ino] != value["output_identity"]:
            raise ContractError("TRAINING_APPROVAL_OUTPUT_CHANGED")
        approval.write_training_approved_inventory(output / "training_approved.json", inventory)
        return inventory
    finally:
        os.close(fd)


def approve(request: dict, output: Path, approved_by: str, *, dry_run: bool) -> dict:
    prepared = prepare_approval_batch(request, output, approved_by)
    value = json.loads(prepared._snapshot)
    dataset, drafts = value["dataset"], value["drafts"]
    output, batch_digest = Path(value["output"]), value["batch_digest"]
    summary = _batch_summary(dataset, drafts, batch_digest, output, approved_by)
    if dry_run:
        return {"status": "PREVIEW_NOT_APPROVED", "dataset_identity": dataset, "episodes": drafts,
                "inventory_path": str(output / "training_approved.json"),
                "human_confirmation": "REQUIRED_ONCE_FOR_EXACT_BATCH_ON_DEV_TTY", "review_summary": summary}
    approval._confirm_human_training_approval("APPROVE BATCH " + batch_digest.removeprefix("sha256:")[:12], summary=summary)
    return publish_approval_batch(prepared)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    resume = sub.add_parser("resume", help="Resume an admitted checkpoint with its TRAIN normalization")
    resume.add_argument("--checkpoint", type=Path, required=True)
    human = sub.add_parser("approve", help="Preview a frozen revision; issue approval only through a controlling human TTY",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Request JSON (paths reference existing evidence; no consent field):
{"dataset_root":"/absolute/frozen/dataset", "dataset_id":"revision-id",
 "repo_id":"local/dataset", "episodes":[
   {"episode_id":"run-id", "episode_index":0,
    "technical_validator_path":"/evidence/technical_validator_result.json",
    "human_semantic_evidence_path":"/evidence/candidate_admission.json",
    "episode_ledger_path":"/evidence/episode_ledger.json"}]}

List the exact sorted selected episodes. Stop collection before reviewing a revision.
Technical PASS, semantic PASS, and human training approval remain independent.
Existing production Collection ledger evidence is required. Seed-based callers
may replace episode_ledger_path with seed_manifest_path and manifest_slot_id.
This command does not invent manifest assignments, transfer Curator approval,
rewrite relocated source roots, or approve unreviewed episodes.
Run with --dry-run first, then without it in the human's terminal. Review the
exact frozen batch summary and confirm once on /dev/tty for all listed episodes.
Per-episode evidence remains independent. The output inventory is
OUTPUT_DIR/training_approved.json and must remain outside the dataset.
An interrupted publication may leave exclusive per-episode artifacts; an incomplete
batch cannot form a valid inventory. Preserve them and use a new output directory
for a new reviewed attempt. This command never starts training or robot execution.''')
    human.add_argument("--request", type=Path, required=True)
    human.add_argument("--output-dir", type=Path, required=True, help="Existing directory outside the dataset; new exclusive artifacts only")
    human.add_argument("--approved-by", required=True)
    human.add_argument("--dry-run", "--preview", action="store_true")
    delegated = sub.add_parser(
        "delegate", help="Issue a frozen batch under an existing local delegation",
    )
    delegated.add_argument("--request", type=Path, required=True)
    delegated.add_argument("--output-dir", type=Path, required=True)
    delegated.add_argument("--delegation", type=Path, required=True)
    delegated.add_argument("--authorized-actor", required=True)
    for mode in ("check", "launch"):
        command = sub.add_parser(mode)
        command.add_argument("--dataset", type=Path, required=True)
        command.add_argument("--repo-id", required=True)
        command.add_argument("--approved-inventory", type=Path, required=True)
        if mode == "check":
            command.add_argument("--episodes")
        else:
            command.add_argument("--profile", required=True)
            command.add_argument("--collection-profile", required=True)
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.mode == "resume":
            raise SystemExit(resume_training(args.checkpoint))
        elif args.mode == "approve":
            print(json.dumps(approve(load_json_strict(args.request), args.output_dir.resolve(), args.approved_by, dry_run=args.dry_run), indent=2, sort_keys=True))
        elif args.mode == "delegate":
            print(json.dumps(delegate_training_batch(
                load_json_strict(args.request), args.output_dir,
                args.authorized_actor, args.delegation,
            ), indent=2, sort_keys=True))
        elif args.mode == "check":
            check_inventory(args.dataset, args.repo_id, args.approved_inventory, args.episodes)
            print("PASS current training-authorized inventory and exact selected episodes")
        else:
            argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            raise SystemExit(launch(dataset=args.dataset, repo_id=args.repo_id, inventory=args.approved_inventory,
                profile=args.profile, collection_profile=args.collection_profile, argv=argv, dry_run=args.dry_run))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
