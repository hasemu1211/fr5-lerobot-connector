"""Publish a lossless mapped dataset and explicit, unapproved training request."""
from __future__ import annotations

import copy
from pathlib import Path
import shutil
import tempfile

from tools.fr5_data_factory import canonical_digest, load_json_strict
from tools.data_factory import training_approval as approval
from tools.data_factory.episode_ledger import validate_episode_state
from tools.data_factory.training_split import validate_training_split, selected_train_eval
from tools.fr5_training_profile import read_metadata
from ..core.errors import CuratorError
from ..core.filesystem import OwnedDirectory, reject_symlink_components, write_json_exclusive, remove_owned_directory
from ..core.identity import assert_tree_identity, file_sha256, stable_tree_identity, tree_snapshot
from ..dataset.publish import commit_hidden_candidate
from ..dataset.mapping import write_mapping, verify_mapped_dataset
from ..dataset.source import open_source_dataset
from ..dataset.verify import run_existing_validator


SCHEMA = "curator.mapped_publication.v1"


def _evidence_digest(drafts):
    return canonical_digest([{"provenance": d["provenance"], "semantic": {
        "artifact_path": d["approval_arguments"]["human_semantic_evidence_path"],
        "artifact_digest": d["approval_arguments"]["human_semantic_evidence_digest"]}} for d in drafts])


def _entries(groups, mapping):
    index = {(e["source_index"],e["source_episode_index"]): e["episode_index"] for e in mapping["episodes"]}
    return sorted([{"episode_id": f"mapped-{i}-{d['provenance']['episode_id']}",
                    "episode_index": index[i,d["provenance"]["episode_index"]],
                    "source_index": i, "source_episode_index": d["provenance"]["episode_index"]}
                   for i,group in enumerate(groups) for d in group], key=lambda e:e["episode_index"])


def _protected(sources, groups):
    paths = [Path(s["dataset_identity"]["dataset_root"]) for s in sources]
    paths.extend(Path(s["request_path"]).parent for s in sources)
    for group in groups:
        for draft in group:
            provenance = draft["provenance"]
            if provenance["schema_version"] == approval.DERIVED_PROVENANCE_SCHEMA:
                paths.append(Path(provenance["derivation"]["run_directory"]))
                paths.append(Path(provenance["parent"]["dataset_identity"]["dataset_root"]))
                provenance = provenance["parent"]["provenance"]
            ledger = Path(provenance["episode_ledger"]["artifact_path"])
            paths.append(ledger.parent)
            paths.append(Path(draft["approval_arguments"]["human_semantic_evidence_path"]).parent)
            paths.extend(Path(v["artifact_path"]).parent for v in load_json_strict(ledger)["artifacts"].values())
    return paths


def _parents(sources, output, actor, *, fresh):
    from tools.data_factory.training_entrypoint import _prepare_approvals
    result = []
    for source in sources:
        path = reject_symlink_components(source["request_path"], "MAPPING_REQUEST")
        if file_sha256(path) != source["request_sha256"]:
            raise CuratorError("MAPPING_REQUEST_CHANGED")
        request = load_json_strict(path)
        fields = {"dataset_root", "dataset_id", "repo_id", "episodes"}
        if set(request) not in (fields, fields | {"derivation"}):
            raise CuratorError("MAPPING_SOURCE_REQUEST_REQUIRED")
        # Fresh derived preparation remains the default. Only frozen publication
        # validation omits mutable parent state; all bound artifacts still verify.
        options = {"check_parent_freshness": False} if "derivation" in request and not fresh else {}
        dataset, drafts = _prepare_approvals(request, output, actor, check_targets=False, **options)
        if dataset != source["dataset_identity"]:
            raise CuratorError("MAPPING_SOURCE_CHANGED")
        if "evidence_digest" in source and source["evidence_digest"] != _evidence_digest(drafts):
            raise CuratorError("MAPPING_SOURCE_EVIDENCE_CHANGED")
        for draft in drafts:
            provenance = draft["provenance"]
            if provenance["schema_version"] == approval.DERIVED_PROVENANCE_SCHEMA:
                provenance = provenance["parent"]["provenance"]
            if provenance["schema_version"] != approval.LEDGER_PROVENANCE_SCHEMA:
                raise CuratorError("MAPPING_SOURCE_LEDGER_REQUIRED")
            if fresh:
                ledger_path = Path(provenance["episode_ledger"]["artifact_path"])
                state = validate_episode_state(load_json_strict(ledger_path.parent / "episode_ledger_state.json"),
                                               ledger=load_json_strict(ledger_path))
                args = draft["approval_arguments"]
                if (state["review"]["semantic_status"] != "PASS"
                        or state["candidate"]["artifact_path"] != args["human_semantic_evidence_path"]
                        or state["candidate"]["artifact_digest"] != args["human_semantic_evidence_digest"]):
                    raise CuratorError("MAPPING_SOURCE_REVIEW_CHANGED")
        result.append(drafts)
    return result


def _cohort(dataset_root, entries, sources, split_reference, fraction, *, groups=None):
    path = Path(split_reference["path"])
    if file_sha256(path) != split_reference["sha256"]:
        raise CuratorError("MAPPING_EVALUATION_CHANGED")
    if "cohort_digest" in split_reference:
        from tools.data_factory.training_entrypoint import revalidate_evaluation_cohort
        from tools.data_factory.training_split import source_episode_identity, resolve_evaluation_cohort
        approval._exact(split_reference, frozenset({"path", "sha256", "cohort_digest"}), "MAPPING_EVALUATION_REFERENCE")
        cohort = revalidate_evaluation_cohort(reject_symlink_components(path, "MAPPING_EVALUATION"))
        if cohort["cohort_digest"] != split_reference["cohort_digest"]:
            raise CuratorError("MAPPING_EVALUATION_CHANGED")
        if groups is None:
            raise CuratorError("MAPPING_EVALUATION_PROVENANCE_REQUIRED")
        parents = {(i, draft["provenance"]["episode_index"]): draft["provenance"]
                   for i, group in enumerate(groups) for draft in group}
        origins = {}
        for entry in entries:
            key = (entry["source_index"], entry["source_episode_index"])
            if key not in parents or entry["episode_index"] in origins:
                raise CuratorError("MAPPING_EVALUATION_SELECTION")
            origins[entry["episode_index"]] = source_episode_identity(parents[key])
        train, heldout = resolve_evaluation_cohort(cohort, origins)
        return {"source_cohort": split_reference, "eval_fraction": None,
                "train_episodes": train, "eval_episodes": heldout}
    split = validate_training_split(path)
    if split["split_digest"] != split_reference["split_digest"]:
        raise CuratorError("MAPPING_EVALUATION_CHANGED")
    matches = [i for i,s in enumerate(sources) if s["dataset_identity"] == split["dataset_identity"]]
    if len(matches) != 1:
        raise CuratorError("MAPPING_EVALUATION_SOURCE")
    expected = [entry["episode_index"] for entry in entries if entry["source_index"] == matches[0]
                and entry["source_episode_index"] in split["eval_episodes"]]
    if len(expected) != len(split["eval_episodes"]):
        raise CuratorError("MAPPING_EVALUATION_SELECTION")
    metadata = read_metadata(dataset_root)
    train, heldout = selected_train_eval(metadata["episode_tasks"], [e["episode_index"] for e in entries], fraction)
    if sorted(heldout) != sorted(expected):
        raise CuratorError("SELECTION_EVALUATION_CHANGED")
    return {"source_split": split_reference, "source_dataset_identity": split["dataset_identity"],
            "source_eval_episodes": split["eval_episodes"], "eval_fraction": fraction,
            "train_episodes": train, "eval_episodes": heldout}


def publish_mapped_training_request(source_requests, output, *, dataset_id, repo_id,
                                    max_copy_bytes, evaluation_split=None, eval_fraction=None,
                                    evaluation_cohort=None):
    """No consent: atomically publish one technically validated request candidate."""
    target = reject_symlink_components(output, "MAPPING_OUTPUT").resolve()
    if target.exists():
        raise CuratorError("OUTPUT_EXISTS")
    if type(max_copy_bytes) is not int or max_copy_bytes <= 0:
        raise CuratorError("MAPPING_COPY_BUDGET")
    if ((evaluation_cohort is None) == (evaluation_split is None)
            or evaluation_cohort is not None and eval_fraction is not None
            or evaluation_split is not None and eval_fraction is None):
        raise CuratorError("MAPPING_EVALUATION_OPTIONS")
    sources = []
    for raw_path in source_requests:
        path = reject_symlink_components(raw_path, "MAPPING_REQUEST").resolve(strict=True)
        request = load_json_strict(path)
        identity = approval.current_dataset_identity(request["dataset_root"], repo_id=request["repo_id"], dataset_id=request["dataset_id"])
        if target.is_relative_to(Path(identity["dataset_root"])) or target.is_relative_to(path.parent):
            raise CuratorError("MAPPING_OUTPUT_OVERLAP")
        sources.append({"request_path": str(path), "request_sha256": file_sha256(path), "dataset_identity": identity})
    if len(sources) < 2 or len({s["dataset_identity"]["dataset_root"] for s in sources}) != len(sources):
        raise CuratorError("MAPPING_MULTIPLE_SOURCES_REQUIRED")
    parents = _parents(sources, target.parent, "curator-preview-only", fresh=True)
    for source, drafts in zip(sources, parents):
        source["evidence_digest"] = _evidence_digest(drafts)
    protected = _protected(sources, parents)
    if any(target.is_relative_to(path) for path in protected):
        raise CuratorError("MAPPING_OUTPUT_OVERLAP")
    size = sum(sum(v[0] for p,v in tree_snapshot(s["dataset_identity"]["dataset_root"]).items() if not p.endswith("/")) for s in sources)
    if size > max_copy_bytes or shutil.disk_usage(target.parent).free < max_copy_bytes:
        raise CuratorError("MAPPING_COPY_BUDGET")
    split_path = reject_symlink_components(
        evaluation_cohort if evaluation_cohort is not None else evaluation_split,
        "MAPPING_EVALUATION").resolve(strict=True)
    if evaluation_cohort is not None:
        from tools.data_factory.training_entrypoint import revalidate_evaluation_cohort
        cohort = revalidate_evaluation_cohort(split_path)
        split_ref = {"path": str(split_path), "sha256": file_sha256(split_path),
                     "cohort_digest": cohort["cohort_digest"]}
        # Check heldout presence and duplicate origins before copying any data.
        planned = [{"episode_index": j, "source_index": i,
                    "source_episode_index": draft["provenance"]["episode_index"]}
                   for j, (i, draft) in enumerate((i, draft) for i, group in enumerate(parents) for draft in group)]
        _cohort(None, planned, sources, split_ref, None, groups=parents)
    else:
        split = validate_training_split(split_path)
        split_ref = {"path": str(split_path), "sha256": file_sha256(split_path), "split_digest": split["split_digest"]}
    if target == split_path or split_path.is_relative_to(target):
        raise CuratorError("MAPPING_OUTPUT_OVERLAP")
    identities = [s["dataset_identity"] for s in sources]
    for source in identities:
        run_existing_validator(source["dataset_root"], source["repo_id"])
    stage = Path(tempfile.mkdtemp(prefix=".curator-mapped-", dir=target.parent))
    owned = OwnedDirectory.capture(stage)
    try:
        from lerobot.datasets.dataset_tools import merge_datasets
        readers = [open_source_dataset(Path(s["dataset_root"]), s["repo_id"]) for s in identities]
        merge_datasets(readers, repo_id, stage / "dataset", concatenate_videos=False, concatenate_data=False)
        mapping = write_mapping(stage / "dataset", repo_id, identities)
        entries = _entries(parents, mapping)
        cohort = _cohort(stage / "dataset", entries, sources, split_ref, eval_fraction, groups=parents)
        validated_snapshot, validated_digest = stable_tree_identity(stage / "dataset", code="MAPPING_DATASET_CHANGED")
        technical = run_existing_validator(stage / "dataset", repo_id)
        assert_tree_identity(stage / "dataset", validated_snapshot, validated_digest, code="MAPPING_DATASET_CHANGED")
        identity = approval.current_dataset_identity(stage / "dataset", repo_id=repo_id, dataset_id=dataset_id)
        identity["dataset_root"] = str(target / "dataset")
        technical = {"schema_version": "curator.mapped_technical.v1", "dataset_identity": identity,
                     "mapping_digest": mapping["mapping_digest"], "validator": technical}
        write_json_exclusive(stage / "technical.json", technical)
        manifest = {"schema_version": SCHEMA, "dataset_identity": identity, "sources": sources,
                    "episodes": entries, "evaluation_cohort": cohort, "mapping_digest": mapping["mapping_digest"],
                    "technical": {"artifact_path": str(target / "technical.json"), "artifact_digest": canonical_digest(technical)}}
        manifest["manifest_digest"] = canonical_digest(manifest)
        write_json_exclusive(stage / "publication.json", manifest)
        reference = {"publication_root": str(target), "manifest_digest": manifest["manifest_digest"]}
        request = {k:identity[k] for k in ("dataset_root","repo_id","dataset_id")}
        request.update(episodes=entries, mapping=reference)
        if "source_cohort" in cohort:
            request["evaluation_cohort"] = cohort["source_cohort"]
        write_json_exclusive(stage / "request.json", request)
        current_parents = _parents(sources, target.parent, "curator-preview-only", fresh=True)
        if _cohort(stage / "dataset", entries, sources, split_ref, eval_fraction, groups=current_parents) != cohort:
            raise CuratorError("MAPPING_EVALUATION_CHANGED")
        verify_mapped_dataset(stage / "dataset", repo_id)
        assert_tree_identity(stage / "dataset", validated_snapshot, validated_digest, code="MAPPING_DATASET_CHANGED")
        snapshot = tree_snapshot(stage)
        if sum(v[0] for p,v in snapshot.items() if not p.endswith("/")) > max_copy_bytes:
            raise CuratorError("MAPPING_COPY_BUDGET")
        commit_hidden_candidate(owned, target, expected_snapshot=snapshot)
    finally:
        if stage.exists():
            remove_owned_directory(owned)
    return {"status": "REQUEST_NOT_APPROVED", "request_path": str(target / "request.json"),
            "mapping": reference, "dataset_identity": identity, "evaluation_cohort": cohort,
            "training_authority": False}


def mapped_publication(reference):
    if set(reference) != {"publication_root", "manifest_digest"}:
        raise CuratorError("MAPPING_REFERENCE")
    root = reject_symlink_components(reference["publication_root"], "MAPPING_PUBLICATION").resolve(strict=True)
    manifest = load_json_strict(reject_symlink_components(root / "publication.json", "MAPPING_PUBLICATION"))
    approval._exact(manifest, frozenset({"schema_version", "dataset_identity", "sources", "episodes",
                                       "evaluation_cohort", "mapping_digest", "technical", "manifest_digest"}), "MAPPING_PUBLICATION_FIELDS")
    if (manifest.get("schema_version") != SCHEMA
            or manifest.get("manifest_digest") != reference["manifest_digest"]
            or canonical_digest({k:v for k,v in manifest.items() if k != "manifest_digest"}) != reference["manifest_digest"]):
        raise CuratorError("MAPPING_PUBLICATION_CHANGED")
    identity = manifest["dataset_identity"]
    if identity["dataset_root"] != str(root / "dataset") or approval.current_dataset_identity(
            root / "dataset", repo_id=identity["repo_id"], dataset_id=identity["dataset_id"]) != identity:
        raise CuratorError("MAPPING_DATASET_CHANGED")
    mapping = verify_mapped_dataset(root / "dataset", identity["repo_id"])
    for source in manifest["sources"]:
        approval._exact(source, frozenset({"request_path", "request_sha256", "dataset_identity", "evidence_digest"}), "MAPPING_SOURCE_FIELDS")
    groups = _parents(manifest["sources"], root.parent, "curator-preview-only", fresh=False)
    if (mapping["sources"] != [s["dataset_identity"] for s in manifest["sources"]]
            or canonical_digest(_entries(groups, mapping)) != canonical_digest(manifest["episodes"])):
        raise CuratorError("MAPPING_SELECTION_CHANGED")
    technical = load_json_strict(reject_symlink_components(root / "technical.json", "MAPPING_TECHNICAL"))
    if (mapping["mapping_digest"] != manifest["mapping_digest"]
            or technical != {"schema_version": "curator.mapped_technical.v1", "dataset_identity": identity,
                             "mapping_digest": mapping["mapping_digest"], "validator": technical.get("validator")}
            or technical["validator"].get("status") != "PASS"
            or manifest["technical"] != {"artifact_path": str(root / "technical.json"), "artifact_digest": canonical_digest(technical)}):
        raise CuratorError("MAPPING_TECHNICAL_CHANGED")
    if _cohort(root / "dataset", manifest["episodes"], manifest["sources"],
               manifest["evaluation_cohort"].get("source_cohort", manifest["evaluation_cohort"].get("source_split")),
               manifest["evaluation_cohort"]["eval_fraction"], groups=groups) != manifest["evaluation_cohort"]:
        raise CuratorError("MAPPING_EVALUATION_CHANGED")
    return manifest


def prepare_mapped_approvals(request, output, approved_by, *, check_targets=True):
    """Native preparation hook; root integrates dispatch, no consent is issued."""
    manifest = approval._mapped_publication(request["mapping"])
    identity = manifest["dataset_identity"]
    expected = {k:identity[k] for k in ("dataset_root","repo_id","dataset_id")}
    expected.update(episodes=manifest["episodes"], mapping=request["mapping"])
    if "source_cohort" in manifest["evaluation_cohort"]:
        expected["evaluation_cohort"] = manifest["evaluation_cohort"]["source_cohort"]
    if request != expected:
        raise CuratorError("MAPPING_REQUEST_CHANGED")
    output = reject_symlink_components(output, "MAPPING_APPROVAL_OUTPUT").resolve(strict=True)
    if output.is_relative_to(Path(request["mapping"]["publication_root"])):
        raise CuratorError("MAPPING_OUTPUT_OVERLAP")
    groups = _parents(manifest["sources"], output, approved_by, fresh=True)
    if any(output.is_relative_to(path) for path in _protected(manifest["sources"], groups)):
        raise CuratorError("MAPPING_OUTPUT_OVERLAP")
    result = []
    for entry in manifest["episodes"]:
        parent = next(d for d in groups[entry["source_index"]] if d["provenance"]["episode_index"] == entry["source_episode_index"])
        draft = copy.deepcopy(parent)
        provenance = approval.compile_mapped_training_provenance(identity, request["mapping"], entry, parent)
        args = draft["approval_arguments"]
        args.update(dataset_identity=identity, episode_id=entry["episode_id"], episode_index=entry["episode_index"],
                    episode_content_digest=provenance["episode_content_digest"],
                    technical_validator_path=manifest["technical"]["artifact_path"],
                    technical_validator_digest=manifest["technical"]["artifact_digest"],
                    episode_provenance_path=str(output / f"{entry['episode_id']}.provenance.json"),
                    episode_provenance_digest=canonical_digest(provenance))
        draft.update(output_path=str(output / f"{entry['episode_id']}.approval.json"), provenance=provenance)
        if check_targets:
            approval._target(Path(draft["output_path"]), "TRAINING_APPROVAL_EXISTS")
            approval._target(Path(args["episode_provenance_path"]), "TRAINING_APPROVAL_EXISTS")
        result.append(draft)
    if check_targets:
        approval._target(output / "training_approved.json", "TRAINING_INVENTORY_EXISTS")
    approval._unique_episodes([d["approval_arguments"] for d in result], [d["provenance"] for d in result])
    _parents(manifest["sources"], output, approved_by, fresh=True)
    return identity, result
