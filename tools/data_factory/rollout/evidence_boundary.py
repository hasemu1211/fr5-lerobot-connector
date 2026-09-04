"""Deterministic, read-only RolloutEvidencePacket.v1 producer."""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from tools.data_factory.episode_ledger import validate_episode_ledger, validate_episode_state
from tools.data_factory.quality.coverage_report import CANDIDATE_FIELDS
from tools.data_factory.task_recipe import validate_episode_instruction_binding, validate_task_binding
from tools.fr5_data_factory import ContractError, DIGEST, canonical_digest, load_json_strict

PACKET_SCHEMA = "data_factory.rollout_evidence_packet.v1"
UNKNOWN = "UNKNOWN"
PARTIAL = "PARTIAL"
SUPPORTED = "SUPPORTED"
_STATUSES = frozenset({UNKNOWN, PARTIAL, SUPPORTED})
_SOURCE_FILES = {
    "task_binding": "task_binding.json",
    "episode_instruction": "episode_instruction.json",
    "ledger": "episode_ledger.json",
    "state": "episode_state.json",
    "candidate_admission": "candidate_admission.json",
}


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _status(value: object, code: str) -> str:
    if value not in _STATUSES:
        raise ContractError(code)
    return value


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _candidate(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != CANDIDATE_FIELDS:
        raise ContractError("ROLLOUT_EVIDENCE_CANDIDATE_FIELDS")
    return copy.deepcopy(dict(value))


def _evidence(status: str, *, digest: str | None = None, path: str | None = None) -> dict[str, Any]:
    result = {"status": _status(status, "ROLLOUT_EVIDENCE_STATUS"), "digest": digest, "path": path}
    if digest is not None:
        _digest(digest, "ROLLOUT_EVIDENCE_DIGEST")
    if path is not None and (not isinstance(path, str) or not path or "\x00" in path):
        raise ContractError("ROLLOUT_EVIDENCE_PATH")
    return result


def build_packet(
    *, task_binding: Mapping[str, Any], episode_instruction: Mapping[str, Any],
    ledger: Mapping[str, Any], state: Mapping[str, Any],
    candidate_admission: Mapping[str, Any] | None = None,
    trace: Mapping[str, Any] | None = None, checkpoint: Mapping[str, Any] | None = None,
    policy_row: Mapping[str, Any] | None = None, clock: Mapping[str, Any] | None = None,
    purpose: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate current contracts and return a packet; never writes or executes."""
    binding = validate_task_binding(task_binding)
    instruction = validate_episode_instruction_binding(episode_instruction)
    checked_ledger = validate_episode_ledger(ledger)
    checked_state = validate_episode_state(state, ledger=checked_ledger)
    candidate = _candidate(candidate_admission)
    if candidate is not None and candidate.get("run_id") != checked_ledger["episode"]["run_id"]:
        raise ContractError("ROLLOUT_EVIDENCE_CANDIDATE_BINDING")
    if instruction["task_binding"] != binding:
        raise ContractError("ROLLOUT_EVIDENCE_TASK_BINDING")
    identity = {
        "task_id": binding["task_id"],
        "task_binding_digest": binding["binding_digest"],
        "instruction_binding_digest": instruction["binding_digest"],
        "episode_ref_digest": checked_ledger["episode"]["episode_ref_digest"],
        "ledger_digest": checked_ledger["ledger_digest"],
        "state_digest": checked_state["state_digest"],
        "candidate_admission_digest": None if candidate is None else canonical_digest(candidate),
    }
    dq = {
        "identity": copy.deepcopy(identity),
        "technical_status": checked_ledger["admission"]["technical_status"],
        "candidate_status": UNKNOWN if candidate is None else candidate["semantic_status"],
        "trace": _evidence(UNKNOWN if trace is None else PARTIAL),
        "checkpoint": _evidence(UNKNOWN if checkpoint is None else PARTIAL),
    }
    re = {
        "identity": copy.deepcopy(identity),
        "trace": _evidence(UNKNOWN if trace is None else PARTIAL),
        "checkpoint": _evidence(UNKNOWN if checkpoint is None else PARTIAL),
        "policy_row": _evidence(UNKNOWN if policy_row is None else PARTIAL),
        "clock": _evidence(UNKNOWN if clock is None else PARTIAL),
        "purpose": _evidence(UNKNOWN if purpose is None else PARTIAL),
        "effectiveness": UNKNOWN,
        "execution": UNKNOWN,
        "promotion": UNKNOWN,
        "physical_verification": UNKNOWN,
        "curator_approval": UNKNOWN,
        "training_authorization": UNKNOWN,
    }
    packet = {
        "schema_version": PACKET_SCHEMA,
        "identity": identity,
        "data_quality_analysis": _plain(_freeze(dq)),
        "rollout_evidence_analysis": _plain(_freeze(re)),
        "limitations": [
            "Missing trace, checkpoint, policy-row, clock, and purpose inputs remain UNKNOWN.",
            "This packet grants no execution, promotion, physical verification, curator, or training authority.",
        ],
    }
    packet["packet_digest"] = canonical_digest(packet)
    return validate_packet(packet)


def validate_packet(value: Mapping[str, Any]) -> dict[str, Any]:
    """Fail-closed validation for the canonical JSON boundary."""
    fields = {"schema_version", "identity", "data_quality_analysis", "rollout_evidence_analysis", "limitations", "packet_digest"}
    if not isinstance(value, Mapping) or set(value) != fields or value["schema_version"] != PACKET_SCHEMA:
        raise ContractError("ROLLOUT_EVIDENCE_PACKET_FIELDS")
    expected = _digest(value["packet_digest"], "ROLLOUT_EVIDENCE_PACKET_DIGEST")
    if canonical_digest({key: item for key, item in value.items() if key != "packet_digest"}) != expected:
        raise ContractError("ROLLOUT_EVIDENCE_PACKET_DIGEST")
    identity = value["identity"]
    if not isinstance(identity, Mapping) or any(
        key not in identity for key in ("task_id", "task_binding_digest", "instruction_binding_digest", "episode_ref_digest", "ledger_digest", "state_digest")
    ):
        raise ContractError("ROLLOUT_EVIDENCE_PACKET_IDENTITY")
    for key in ("task_binding_digest", "instruction_binding_digest", "episode_ref_digest", "ledger_digest", "state_digest"):
        _digest(identity[key], "ROLLOUT_EVIDENCE_PACKET_IDENTITY")
    if identity.get("candidate_admission_digest") is not None:
        _digest(identity["candidate_admission_digest"], "ROLLOUT_EVIDENCE_PACKET_IDENTITY")
    if not isinstance(value["limitations"], list) or any(not isinstance(item, str) for item in value["limitations"]):
        raise ContractError("ROLLOUT_EVIDENCE_PACKET_LIMITATIONS")
    return copy.deepcopy(dict(value))


def _load_sources(root: Path) -> dict[str, Any]:
    values = {}
    for name, filename in _SOURCE_FILES.items():
        path = root / filename
        try:
            value = load_json_strict(path)
        except (OSError, ContractError) as exc:
            if name == "candidate_admission" and not path.exists():
                values[name] = None
                continue
            raise ContractError("ROLLOUT_EVIDENCE_INPUT") from exc
        values[name] = value
    return values


def inspect_directory(root: str | Path) -> dict[str, Any]:
    """Read frozen JSON inputs and printable packet; no output path is accepted."""
    directory = Path(root)
    if not directory.is_dir() or directory.is_symlink():
        raise ContractError("ROLLOUT_EVIDENCE_INPUT_ROOT")
    values = _load_sources(directory)
    return build_packet(
        task_binding=values["task_binding"], episode_instruction=values["episode_instruction"],
        ledger=values["ledger"], state=values["state"],
        candidate_admission=values["candidate_admission"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_directory", type=Path)
    args = parser.parse_args(argv)
    try:
        packet = inspect_directory(args.input_directory)
    except (ContractError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
