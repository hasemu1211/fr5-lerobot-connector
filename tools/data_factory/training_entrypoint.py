"""Public offline admission, human approval and selected-episode launch connection.

No consent is accepted from arguments, stdin, environment variables or JSON.
Single-episode and exact-batch decisions both require a controlling /dev/tty.
"""
from __future__ import annotations

import sys
# Direct execution must not shadow stdlib operator with data_factory/operator.
if __package__ in {None, ""}:
    sys.path.pop(0)

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.data_factory import training_approval as approval
from tools.data_factory.training_receipts import compile_launch_receipt
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


def prepare_launch(*, dataset: Path, repo_id: str, inventory: Path,
                   profile: str, collection_profile: str, argv: list[str]) -> tuple[dict, dict]:
    from importlib.metadata import version

    if version("lerobot") != "0.6.1":
        raise ContractError("TRAINING_SPLIT_RUNTIME_UNVERIFIED", "This split adapter is verified against LeRobot 0.6.1")
    config = options(argv[1:])
    # Streaming, renamed roots, alternate episode order and resume are separate contracts.
    if (config.get("--dataset.root") != str(dataset)
            or config.get("--dataset.repo_id") != repo_id
            or config.get("--dataset.streaming", "false") != "false"
            or "--config_path" in config or "--resume" in config):
        raise ContractError("TRAINING_COMMAND_DATASET")
    approved, metadata, selected = check_inventory(dataset, repo_id, inventory, config.get("--dataset.episodes"))
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
        if provenance["schema_version"] == approval.LEDGER_PROVENANCE_SCHEMA:
            ledger = load_json_strict(Path(provenance["episode_ledger"]["artifact_path"]))
            if ledger["bindings"]["collection_profile_digest"] != feature["collection_profile_digest"]:
                raise ContractError("TRAINING_COLLECTION_PROFILE_LEDGER_BINDING")
    for key, value in options(feature["policy_argv"]).items():
        if config.get(key) != value:
            raise ContractError("TRAINING_POLICY_FEATURE_BINDING")
    split = compile_launch_split(
        inventory=approved, metadata=metadata, selected=selected,
        fraction=float(config["--dataset.eval_split"]), feature_contract=feature,
    )
    return split, compile_launch_receipt(split, argv, str(inventory))


def launch(*, dataset: Path, repo_id: str, inventory: Path, profile: str,
           collection_profile: str, argv: list[str], dry_run: bool = False,
           runner=subprocess.run) -> int:
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
    output.parent.mkdir(parents=True, exist_ok=True)
    written = []
    try:
        for path, value in zip(pending, (split, receipt)):
            approval._write_exclusive(path, value, "TRAINING_OUTPUT_EXISTS")
            written.append(path)
        return runner(argv, check=False).returncode
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


def prepare_approvals(request: dict, output: Path, approved_by: str) -> tuple[dict, list[dict]]:
    approval._exact(request, frozenset({"dataset_root", "dataset_id", "repo_id", "episodes"}), "TRAINING_PREAPPROVAL_FIELDS")
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
            f"  [{args['episode_index']}] {args['episode_id']} — technical PASS; semantic PASS by {draft['reviewer_id']}",
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


def approve(request: dict, output: Path, approved_by: str, *, dry_run: bool) -> dict:
    # Freeze caller-owned selection as well as the evidence. There is no caller
    # supplied confirmation, consent flag, or production confirmation callback.
    request = copy.deepcopy(request)
    output = output.resolve() if not output.is_symlink() else output
    dataset, drafts = prepare_approvals(request, output, approved_by)
    reviewed_at = datetime.now(timezone.utc)
    documents = [approval._prepare_training_approval(
        **{**draft["approval_arguments"], "episode_provenance_path": draft["provenance"]},
        clock=lambda: reviewed_at,
    ) for draft in drafts]
    batch_digest = approval._batch_digest(documents)
    summary = _batch_summary(dataset, drafts, batch_digest, output, approved_by)
    if dry_run:
        return {"status": "PREVIEW_NOT_APPROVED", "dataset_identity": dataset, "episodes": drafts,
                "inventory_path": str(output / "training_approved.json"),
                "human_confirmation": "REQUIRED_ONCE_FOR_EXACT_BATCH_ON_DEV_TTY", "review_summary": summary}
    approval._confirm_human_training_approval("APPROVE BATCH " + batch_digest.removeprefix("sha256:")[:12], summary=summary)
    # The complete source graph (including seed/ledger sources, metadata, and
    # dataset bytes) must still derive exactly what the human just reviewed.
    if prepare_approvals(request, output, approved_by) != (dataset, drafts):
        raise ContractError("TRAINING_INPUT_CHANGED")
    rechecked = [approval._prepare_training_approval(
        **{**draft["approval_arguments"], "episode_provenance_path": draft["provenance"]},
        clock=lambda: reviewed_at,
    ) for draft in drafts]
    if rechecked != documents:
        raise ContractError("TRAINING_INPUT_CHANGED")
    entries = []
    for draft, document in zip(drafts, documents):
        args = draft["approval_arguments"]
        issued = {**document, "schema_version": approval.BATCH_APPROVAL_SCHEMA, "batch_digest": batch_digest}
        approval.validate_training_approval(issued)
        approval._write_exclusive(Path(args["episode_provenance_path"]), draft["provenance"], "TRAINING_APPROVAL_EXISTS")
        approval._write_exclusive(Path(draft["output_path"]), issued, "TRAINING_APPROVAL_EXISTS")
        entries.append({
            "dataset_identity_digest": canonical_digest(dataset),
            "episode_id": args["episode_id"], "episode_index": args["episode_index"],
            "episode_content_digest": args["episode_content_digest"],
            "technical_validator": {"artifact_path": args["technical_validator_path"], "artifact_digest": args["technical_validator_digest"], "status": "PASS"},
            "human_semantic_evidence": {"artifact_path": args["human_semantic_evidence_path"], "artifact_digest": args["human_semantic_evidence_digest"], "status": "PASS", "reviewer_id": draft["reviewer_id"]},
            "episode_provenance": {"artifact_path": args["episode_provenance_path"], "artifact_digest": args["episode_provenance_digest"]},
            "training_approval": {"artifact_path": draft["output_path"], "artifact_digest": canonical_digest(issued), "provenance": approval.PROVENANCE},
        })
    inventory = approval.build_training_approved_inventory(scope=approval.PRODUCTION_SCOPE, dataset_identity=dataset, episodes=entries)
    if approval.current_dataset_identity(request["dataset_root"], repo_id=request["repo_id"], dataset_id=request["dataset_id"]) != dataset:
        raise ContractError("TRAINING_DATASET_CHANGED")
    approval.write_training_approved_inventory(output / "training_approved.json", inventory)
    return inventory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
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
        if args.mode == "approve":
            print(json.dumps(approve(load_json_strict(args.request), args.output_dir.resolve(), args.approved_by, dry_run=args.dry_run), indent=2, sort_keys=True))
        elif args.mode == "check":
            check_inventory(args.dataset, args.repo_id, args.approved_inventory, args.episodes)
            print("PASS current human-approved inventory and exact selected episodes")
        else:
            argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            raise SystemExit(launch(dataset=args.dataset, repo_id=args.repo_id, inventory=args.approved_inventory,
                profile=args.profile, collection_profile=args.collection_profile, argv=argv, dry_run=args.dry_run))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
