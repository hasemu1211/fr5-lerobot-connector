"""Explicit offline collection consumer; no runtime or collection callbacks."""
from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from tools.data_factory.campaign_operator import validate_compiled_authoring_evidence
from tools.data_factory.collection_recommendation import derive_collection_recommendation
from tools.data_factory.episode_ledger import (
    _artifact,
    validate_episode_state,
)
from tools.fr5_data_factory import ContractArgumentParser, ContractError, load_json_strict, canonical_digest


def discover_stored_collection(run_root: str | Path) -> dict:
    """On-demand, one-level discovery; canonical disposition owns eligibility.

    No persisted inventory, watcher, compatibility ranking or runtime callback.
    Feed returned directories to recommend_stored_collection for current task/
    catalog compatibility and repeat discovery when choosing current advice.
    """
    from tools.data_factory.collection_recommendation import _episode_snapshot
    root = Path(run_root)
    if root.is_symlink() or not root.is_dir():
        raise ContractError("COLLECTION_DISCOVERY_ROOT")
    root = root.resolve(strict=True)
    selected, excluded, snapshots, seen = [], [], [], set()
    for path in sorted(root.iterdir()):
        if not path.is_dir() and not path.is_symlink():
            continue
        try:
            if path.is_symlink():
                raise ContractError("COLLECTION_DISCOVERY_SYMLINK")
            if not (path / "episode_ledger.json").is_file():
                raise ContractError("COLLECTION_DISCOVERY_LEDGER_UNAVAILABLE")
            evidence, _protected = _load_run(path)
            summary, identity = _episode_snapshot(evidence, evidence["artifacts"]["manifest"])
            if evidence["artifacts"]["runtime_binding"]["data_disposition"] != "PRODUCTION":
                raise ContractError("COLLECTION_DISCOVERY_NOT_PRODUCTION")
            if evidence["state"]["review"]["semantic_status"] != "PASS":
                raise ContractError("COLLECTION_DISCOVERY_SEMANTIC_PASS_UNAVAILABLE")
            if identity in seen:
                raise ContractError("COLLECTION_RECOMMENDATION_EPISODE_DUPLICATE")
            seen.add(identity)
            selected.append(str(path))
            snapshots.append({"run_directory": str(path), "episode": summary})
        except (ContractError, OSError) as exc:
            excluded.append({"run_directory": str(path), "reason_code":
                             exc.code if isinstance(exc, ContractError) else "COLLECTION_RECOMMENDATION_SOURCE_IO"})
    result = {"availability": "AVAILABLE" if selected else "UNAVAILABLE",
              "run_root": str(root), "run_directories": selected,
              "episodes": snapshots, "excluded": excluded}
    result["discovery_digest"] = canonical_digest(result)
    return result


def _load_run(root: Path) -> tuple[dict, set[Path]]:
    ledger = load_json_strict(root / "episode_ledger.json")
    # State validation already reopens and validates the ledger's full source graph.
    state = validate_episode_state(load_json_strict(root / "episode_ledger_state.json"), ledger=ledger)
    protected = {root, Path(ledger["dataset"]["dataset_root"]).resolve(strict=True)}
    artifacts = {}
    for name, ref in ledger["artifacts"].items():
        _, artifacts[name] = _artifact(ref, name=name, episode_index=ledger["episode"]["episode_index"])
        protected.add(Path(ref["artifact_path"]).resolve(strict=True).parent)
    if state["candidate"] is None:
        raise ContractError("COLLECTION_RECOMMENDATION_CANDIDATE_UNAVAILABLE")
    _, candidate = _artifact(state["candidate"], name="candidate", episode_index=0)
    protected.add(Path(state["candidate"]["artifact_path"]).resolve(strict=True).parent)
    return {"manifest_order_index": artifacts["intent"]["order_index"], "ledger": ledger,
            "state": state, "candidate": candidate, "artifacts": artifacts}, protected


def _acquisition_context(value):
    """Read the existing canonical scene, retaining the caller's native catalog.

    The catalog is a replayable software input, not a fresh device observation.
    Consumption must pass its current catalog, selection and scene again.
    """
    required = {"catalog", "selection", "scene_state_path", "object_instance_id",
                "requested_count", "normalized_seed", "repeat", "expected_scene_digest"}
    if not isinstance(value, dict) or set(value) != required:
        raise ContractError("COLLECTION_ACQUISITION_INPUT_FIELDS")
    if not isinstance(value["scene_state_path"], str) or not value["scene_state_path"]:
        raise ContractError("COLLECTION_ACQUISITION_SCENE_PATH")
    path = Path(value["scene_state_path"])
    if path.is_symlink():
        raise ContractError("COLLECTION_ACQUISITION_SCENE_PATH")
    scene = load_json_strict(path)
    if canonical_digest(scene) != value["expected_scene_digest"]:
        raise ContractError("COLLECTION_ACQUISITION_SCENE_CHANGED")
    return {**{key: item for key, item in value.items()
               if key not in {"scene_state_path", "expected_scene_digest"}}, "scene_state": scene}


def _publish(destination: Path, documents: dict) -> None:
    """Publish both files together; cooperating callers reuse identical output."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # A short root-directory lock covers publication, not source analysis.
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if destination.is_symlink() or destination.exists():
            if (destination.is_symlink() or not destination.is_dir()
                    or set(path.name for path in destination.iterdir()) != set(documents)):
                raise ContractError("COLLECTION_RECOMMENDATION_OUTPUT_CONFLICT")
            for name, value in documents.items():
                path = destination / name
                if path.is_symlink() or load_json_strict(path) != value:
                    raise ContractError("COLLECTION_RECOMMENDATION_OUTPUT_CONFLICT")
            return
        with tempfile.TemporaryDirectory(prefix=".pending-", dir=destination.parent) as temporary:
            staged = Path(temporary) / "result"
            staged.mkdir()
            for name, value in documents.items():
                with (staged / name).open("x", encoding="utf-8") as stream:
                    json.dump(value, stream, sort_keys=True, indent=2)
                    stream.write("\n")
            staged.rename(destination)
    finally:
        os.close(descriptor)


def recommend_stored_collection(
    *, run_directories: Sequence[str | Path], source_commit: str,
    output_root: str | Path | None = None,
    acquisition: dict | None = None,
    expected_recommendation_digest: str | None = None,
    rollout_lifecycle_path: str | Path | None = None,
) -> dict:
    """Load canonical run evidence and optionally publish immutable derived files.

    source_commit is a caller-supplied implementation label, not verified running
    code identity or a historical run commit. This consumer does not attest Git.
    Legacy mode requires retained compiled authoring. Acquisition mode uses each
    run's canonical ledger/manifest and explicit current caller inputs; it never
    reconstructs missing historical authoring or claims current execution rights.
    Optional rollout input is the original terminal OneJob result. The rollout
    owner rederives its diagnostic; a free-floating diagnostic is insufficient.
    """
    if not run_directories:
        raise ContractError("COLLECTION_RECOMMENDATION_RUNS_REQUIRED")
    provenance = {"source_commit": source_commit, "verification": "CALLER_SUPPLIED_UNVERIFIED"}
    try:
        sources = None
        evidence = []
        protected = set()
        lifecycle = None
        diagnostic = None
        if rollout_lifecycle_path is not None:
            from tools.data_factory.rollout.evidence_boundary import build_run_diagnostic
            lifecycle_path = Path(rollout_lifecycle_path)
            if lifecycle_path.is_symlink():
                raise ContractError("COLLECTION_RECOMMENDATION_ROLLOUT_SOURCE_PATH")
            lifecycle = load_json_strict(lifecycle_path)
            diagnostic = build_run_diagnostic(lifecycle)
            protected.add(lifecycle_path.resolve(strict=True).parent)
        context = None if acquisition is None else _acquisition_context(acquisition)
        roots = []
        for directory in run_directories:
            root = Path(directory).resolve(strict=True)
            if context is None:
                authoring_path = root / "compiled_authoring_evidence.json"
                if not authoring_path.is_file():
                    raise ContractError("COLLECTION_RECOMMENDATION_AUTHORING_UNAVAILABLE")
                authoring = validate_compiled_authoring_evidence(load_json_strict(authoring_path))
                if sources is not None and sources != authoring:
                    raise ContractError("COLLECTION_RECOMMENDATION_AUTHORING_MISMATCH")
                sources = authoring
            loaded, paths = _load_run(root)
            protected.update(paths)
            if context is None and loaded["artifacts"]["manifest"] != sources["manifest"]:
                raise ContractError("COLLECTION_RECOMMENDATION_AUTHORING_MISMATCH")
            evidence.append(loaded)
            roots.append(root)
        report, recommendation = derive_collection_recommendation(
            compiled_authoring=sources, episode_evidence=evidence, source_commit=source_commit,
            **({"acquisition": context} if context is not None else {}),
            rollout_lifecycle_result=lifecycle,
        )
        if lifecycle is not None:
            if (lifecycle_path.is_symlink() or load_json_strict(lifecycle_path) != lifecycle
                    or any(_load_run(root)[0] != previous for root, previous in zip(roots, evidence))
                    or any(load_json_strict(root / "compiled_authoring_evidence.json") != sources for root in roots)):
                raise ContractError("COLLECTION_RECOMMENDATION_ROLLOUT_INPUT_CHANGED")
        if context is not None:
            protected.add(Path(acquisition["scene_state_path"]).resolve(strict=True).parent)
            if (_acquisition_context(acquisition) != context
                    or any(_load_run(root)[0] != previous for root, previous in zip(roots, evidence))):
                raise ContractError("COLLECTION_ACQUISITION_INPUT_CHANGED")
        if (expected_recommendation_digest is not None
                and expected_recommendation_digest != recommendation["recommendation_digest"]):
            raise ContractError("COLLECTION_ACQUISITION_INPUT_CHANGED")
    except (ContractError, OSError) as exc:
        return {
            "availability": "UNAVAILABLE",
            "reason_codes": [str(exc) if isinstance(exc, ContractError)
                             else "COLLECTION_RECOMMENDATION_SOURCE_IO"],
            "data_quality_analysis": None, "recommendation": None, "output_path": None,
            "implementation_provenance": provenance,
        }
    output_path = None
    if output_root is not None:
        destination = Path(output_root).resolve()
        if any(destination.is_relative_to(path) or path.is_relative_to(destination)
               for path in protected):
            raise ContractError("COLLECTION_RECOMMENDATION_OUTPUT_OVERLAP")
        destination = destination / recommendation["recommendation_digest"].removeprefix("sha256:")
        documents = {"coverage_report.json": report, "collection_recommendation.json": recommendation}
        if diagnostic is not None:
            documents["rollout_diagnostic.json"] = diagnostic
        _publish(destination, documents)
        output_path = str(destination)
    return {
        "availability": "AVAILABLE", "reason_codes": (
            ["COLLECTION_RECOMMENDATION_ROLLOUT_NO_SUPPORTED_COLLECTION_PATCH"]
            if diagnostic is not None and not recommendation["suggested_draft_patches"] else []
        ),
        "data_quality_analysis": report, "recommendation": recommendation,
        "output_path": output_path,
        "implementation_provenance": provenance,
        **({"rollout_evidence_analysis": diagnostic} if diagnostic is not None else {}),
    }


def main(argv=None) -> int:
    parser = ContractArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--run-dir", action="append")
    sources.add_argument("--run-root", help="on-demand canonical production PASS discovery, one directory level")
    parser.add_argument("--source-commit", required=True, help="caller-supplied implementation commit label (not attested)")
    parser.add_argument("--output-root", help="optional exclusive derived output root")
    parser.add_argument("--acquisition-input", help="saved native catalog/selection, budget and canonical scene reference")
    parser.add_argument("--expected-recommendation-digest", help="reject stale advice before publication")
    parser.add_argument("--rollout-lifecycle", help="original terminal learned_lifecycle_result.json (requires retained campaign authoring)")
    try:
        args = parser.parse_args(argv)
        discovery = None if args.run_root is None else discover_stored_collection(args.run_root)
        if discovery is not None and not discovery["run_directories"]:
            print(json.dumps({"availability": "UNAVAILABLE", "reason_codes": ["COLLECTION_DISCOVERY_NO_ELIGIBLE_RUNS"],
                              "data_quality_analysis": None, "recommendation": None, "output_path": None,
                              "discovery": discovery}, sort_keys=True))
            return 2
        result = recommend_stored_collection(
            run_directories=args.run_dir if discovery is None else discovery["run_directories"], source_commit=args.source_commit,
            output_root=args.output_root,
            acquisition=None if args.acquisition_input is None else load_json_strict(args.acquisition_input),
            expected_recommendation_digest=args.expected_recommendation_digest,
            rollout_lifecycle_path=args.rollout_lifecycle,
        )
        if discovery is not None:
            result["discovery"] = discovery
        print(json.dumps(result, sort_keys=True))
        return 0 if result["availability"] == "AVAILABLE" else 2
    except (ContractError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
