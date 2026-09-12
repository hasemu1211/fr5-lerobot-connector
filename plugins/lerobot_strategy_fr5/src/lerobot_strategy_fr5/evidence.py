from __future__ import annotations

import hashlib
import importlib.metadata
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from lerobot.datasets import CODEBASE_VERSION


EVIDENCE_SCHEMA_VERSION = "fr5.lerobot.rollout_evidence.v1"

# Compatibility is audited per LeRobot API family rather than pretending that
# our extension is valid against arbitrary future releases.
SUPPORTED_LEROBOT_VERSIONS = {"0.6.1"}
SUPPORTED_DATASET_CODEBASE_VERSIONS = {"v3.0"}

EVENT_KINDS = {
    "RUN_START",
    "OBSERVATION",
    "POLICY_ACTION",
    "ACTION_REQUESTED",
    "ACTION_PROCESSED",
    "ACTION_SENT",
    "EXECUTION_TERMINAL",
    "FIRST_DEVIATION",
    "FAILURE_OBSERVED",
    "HYPOTHESIS",
    "RECOLLECTION_DECISION",
    "RUN_END",
    "PROPOSAL_CANDIDATE",
    "PROPOSAL_VALIDATION",
}


@dataclass(frozen=True)
class CompatibilityReceipt:
    lerobot_version: str
    lerobot_api_family: str
    dataset_codebase_version: str
    evidence_schema_version: str


def compatibility_receipt() -> CompatibilityReceipt:
    version = importlib.metadata.version("lerobot")

    core = version.split("+", 1)[0]
    parts = core.split(".")
    if len(parts) < 2:
        raise RuntimeError(f"LEROBOT_VERSION_UNPARSEABLE: {version}")

    try:
        major_minor = (int(parts[0]), int(parts[1]))
    except ValueError as exc:
        raise RuntimeError(
            f"LEROBOT_VERSION_UNPARSEABLE: {version}"
        ) from exc

    if version not in SUPPORTED_LEROBOT_VERSIONS:
        raise RuntimeError(
            f"LEROBOT_COMPATIBILITY_UNAUDITED: {version}"
        )

    if CODEBASE_VERSION not in SUPPORTED_DATASET_CODEBASE_VERSIONS:
        raise RuntimeError(
            "LEROBOT_DATASET_COMPATIBILITY_UNAUDITED: "
            f"{CODEBASE_VERSION}"
        )

    return CompatibilityReceipt(
        lerobot_version=version,
        lerobot_api_family=f"{major_minor[0]}.{major_minor[1]}",
        dataset_codebase_version=CODEBASE_VERSION,
        evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
    )


class EvidenceSink(Protocol):
    def emit(self, kind: str, payload: dict[str, Any]) -> str:
        ...

    def close(self) -> None:
        ...


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class JsonlEvidenceSink:
    """Temporary sidecar sink behind a stable rollout-evidence interface.

    When LeRobot exposes a native evidence/provenance sink, replace this class,
    not the callers producing rollout evidence.
    """

    def __init__(self, path: str | Path, run_id: str):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("EVIDENCE_RUN_ID")

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if self.path.exists():
            raise FileExistsError(f"EVIDENCE_ALREADY_EXISTS: {self.path}")

        self.run_id = run_id
        self.compatibility = compatibility_receipt()
        self._seq = 0
        self._handle = self.path.open(
            "x",
            encoding="utf-8",
            buffering=1,
        )

    def emit(self, kind: str, payload: dict[str, Any]) -> str:
        if kind not in EVENT_KINDS:
            raise ValueError(f"EVIDENCE_EVENT_KIND: {kind}")
        if not isinstance(payload, dict):
            raise TypeError("EVIDENCE_PAYLOAD")

        # Keep observed fact and causal interpretation structurally distinct.
        if kind == "FAILURE_OBSERVED" and (
            "cause" in payload or "hypothesis" in payload
        ):
            raise ValueError("FAILURE_OBSERVED_MUST_NOT_CONTAIN_CAUSE")

        event = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "sequence": self._seq,
            "kind": kind,
            "wall_time_ns": time.time_ns(),
            "monotonic_time_ns": time.monotonic_ns(),
            "compatibility": asdict(self.compatibility),
            "payload": payload,
        }

        digest = "sha256:" + hashlib.sha256(
            _canonical_bytes(event)
        ).hexdigest()

        record = {
            **event,
            "event_digest": digest,
        }

        self._handle.write(
            _canonical_bytes(record).decode("utf-8") + "\n"
        )
        self._seq += 1
        return digest

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.flush()
            self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def inspect_proposal_evidence(path: str | Path) -> dict[str, Any]:
    """Verify a sidecar prefix and derive proposal diagnostics, without IO to FR5.

    Digests detect accidental changes, not forgery. Missing RUN_END means only
    that run completeness is unknown; an observed proposal result can survive it.
    A truncated final JSON line is not accepted as a complete event.
    """
    from tools.data_factory.rollout.evidence_boundary import build_proposal_diagnostic
    from tools.fr5_data_factory import ContractError, load_json_strict

    candidates = {}
    diagnostics = []
    run_id = None
    ended = False
    started = False
    count = 0
    compatibility = None
    with Path(path).open(encoding="utf-8") as handle:
        for sequence, line in enumerate(handle):
            try:
                # The same strict JSON reader rejects duplicate keys/NaN.
                record = load_json_strict(line)
                digest = record.pop("event_digest")
                expected_fields = {"schema_version", "run_id", "sequence", "kind",
                                   "wall_time_ns", "monotonic_time_ns", "compatibility", "payload"}
                if (not line.endswith("\n") or set(record) != expected_fields or ended
                        or record["schema_version"] != EVIDENCE_SCHEMA_VERSION
                        or type(record["sequence"]) is not int or record["sequence"] != sequence
                        or record["kind"] not in EVENT_KINDS
                        or not isinstance(record["payload"], dict)
                        or not isinstance(record["run_id"], str) or not record["run_id"]
                        or any(type(record[k]) is not int or record[k] < 0
                               for k in ("wall_time_ns", "monotonic_time_ns"))
                        or digest != "sha256:" + hashlib.sha256(_canonical_bytes(record)).hexdigest()):
                    raise ValueError("event")
                if run_id is None:
                    run_id = record["run_id"]
                    compatibility = record["compatibility"]
                    if (not isinstance(compatibility, dict)
                            or set(compatibility) != {"lerobot_version", "lerobot_api_family",
                                                      "dataset_codebase_version", "evidence_schema_version"}
                            or compatibility["lerobot_version"] not in SUPPORTED_LEROBOT_VERSIONS
                            or compatibility["lerobot_api_family"] != "0.6"
                            or compatibility["dataset_codebase_version"] not in SUPPORTED_DATASET_CODEBASE_VERSIONS
                            or compatibility["evidence_schema_version"] != EVIDENCE_SCHEMA_VERSION):
                        raise ValueError("compatibility")
                if record["run_id"] != run_id or record["compatibility"] != compatibility:
                    raise ValueError("run")
                kind, payload = record["kind"], record["payload"]
                if kind == "RUN_START":
                    if sequence != 0:
                        raise ValueError("start")
                    started = True
                elif kind == "RUN_END":
                    if not started:
                        raise ValueError("end")
                    ended = True
                elif kind == "PROPOSAL_CANDIDATE":
                    candidates[digest] = payload
                elif kind == "PROPOSAL_VALIDATION":
                    candidate_digest = payload["candidate_event_digest"]
                    candidate = candidates.pop(candidate_digest)
                    diagnostic = build_proposal_diagnostic(candidate, payload)
                    diagnostics.append({"run_id": run_id, "candidate_event_digest": candidate_digest,
                                        "validation_event_digest": digest, "diagnostic": diagnostic})
                count += 1
            except (KeyError, TypeError, ValueError) as exc:
                raise ContractError(f"ROLLOUT_PROPOSAL_EVIDENCE_INVALID: line {sequence + 1}") from exc
    return {"run_id": run_id, "events": count, "run_end_observed": ended,
            "diagnostics": diagnostics, "unfinished_candidate_events": list(candidates),
            "execution_authorized": False, "training_authorized": False}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Read-only FR5 proposal-attempt diagnostics")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect_proposal_evidence(args.path), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
