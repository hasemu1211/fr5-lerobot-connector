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
    validate_episode_ledger,
    validate_episode_state,
)
from tools.fr5_data_factory import ContractArgumentParser, ContractError, load_json_strict


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
) -> dict:
    """Load canonical run evidence and optionally publish immutable derived files.

    source_commit is a caller-supplied implementation label, not verified running
    code identity or a historical run commit. This consumer does not attest Git.
    Missing/invalid legacy sources are unavailable; never consult current config.
    """
    if not run_directories:
        raise ContractError("COLLECTION_RECOMMENDATION_RUNS_REQUIRED")
    provenance = {"source_commit": source_commit, "verification": "CALLER_SUPPLIED_UNVERIFIED"}
    try:
        sources = None
        evidence = []
        protected = set()
        for directory in run_directories:
            root = Path(directory).resolve(strict=True)
            protected.add(root)
            authoring_path = root / "compiled_authoring_evidence.json"
            if not authoring_path.is_file():
                raise ContractError("COLLECTION_RECOMMENDATION_AUTHORING_UNAVAILABLE")
            authoring = validate_compiled_authoring_evidence(load_json_strict(authoring_path))
            if sources is not None and sources != authoring:
                raise ContractError("COLLECTION_RECOMMENDATION_AUTHORING_MISMATCH")
            sources = authoring
            ledger = validate_episode_ledger(load_json_strict(root / "episode_ledger.json"))
            state = validate_episode_state(
                load_json_strict(root / "episode_ledger_state.json"), ledger=ledger,
            )
            protected.add(Path(ledger["dataset"]["dataset_root"]).resolve(strict=True))
            artifacts = {}
            for name, ref in ledger["artifacts"].items():
                _, artifacts[name] = _artifact(
                    ref, name=name, episode_index=ledger["episode"]["episode_index"],
                )
                protected.add(Path(ref["artifact_path"]).resolve(strict=True).parent)
            if artifacts["manifest"] != authoring["manifest"]:
                raise ContractError("COLLECTION_RECOMMENDATION_AUTHORING_MISMATCH")
            if state["candidate"] is None:
                raise ContractError("COLLECTION_RECOMMENDATION_CANDIDATE_UNAVAILABLE")
            _, candidate = _artifact(state["candidate"], name="candidate", episode_index=0)
            protected.add(Path(state["candidate"]["artifact_path"]).resolve(strict=True).parent)
            evidence.append({
                "manifest_order_index": artifacts["intent"]["order_index"],
                "ledger": ledger, "state": state, "candidate": candidate, "artifacts": artifacts,
            })
        report, recommendation = derive_collection_recommendation(
            compiled_authoring=sources, episode_evidence=evidence, source_commit=source_commit,
        )
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
        _publish(destination, documents)
        output_path = str(destination)
    return {
        "availability": "AVAILABLE", "reason_codes": [],
        "data_quality_analysis": report, "recommendation": recommendation,
        "output_path": output_path,
        "implementation_provenance": provenance,
    }


def main(argv=None) -> int:
    parser = ContractArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", action="append", required=True)
    parser.add_argument("--source-commit", required=True, help="caller-supplied implementation commit label (not attested)")
    parser.add_argument("--output-root", required=True, help="exclusive derived output root")
    try:
        args = parser.parse_args(argv)
        result = recommend_stored_collection(
            run_directories=args.run_dir, source_commit=args.source_commit,
            output_root=args.output_root,
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["availability"] == "AVAILABLE" else 2
    except (ContractError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
