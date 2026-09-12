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
