"""Read-only aggregation of quality attributes; never a training-admission gate."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.fr5_data_factory import ContractError, DIGEST
from tools.data_factory.quality.phase_metrics import ATTRIBUTE_SCHEMA, STATUS


REPORT_SCHEMA = "data_factory.episode_quality.v1"
REPORT_KEYS = frozenset({"schema_version", "run_id", "resolved_job_digest", "plan_digest", "technical_validator", "attributes", "status", "flags"})
ATTRIBUTE_KEYS = frozenset({"schema_version", "attribute", "run_id", "resolved_job_digest", "plan_digest", "source_digests", "status", "metrics", "flags"})
TECHNICAL_REFERENCE_KEYS = frozenset({"schema_version", "status", "result_digest"})
TECHNICAL_REFERENCE_SCHEMA = "data_factory.technical_validator_ref.v1"


def validate_attribute_record(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != ATTRIBUTE_KEYS:
        raise ContractError("QUALITY_ATTRIBUTE_FIELDS")
    if value["schema_version"] != ATTRIBUTE_SCHEMA or value["status"] not in STATUS:
        raise ContractError("QUALITY_ATTRIBUTE_SCHEMA")
    if not isinstance(value["attribute"], str) or not value["attribute"] or not isinstance(value["run_id"], str) or not value["run_id"]:
        raise ContractError("QUALITY_ATTRIBUTE_ID")
    if any(not isinstance(value[key], str) or not DIGEST.fullmatch(value[key]) for key in ("resolved_job_digest", "plan_digest")):
        raise ContractError("QUALITY_ATTRIBUTE_BINDING")
    source_digests = value["source_digests"]
    if not isinstance(source_digests, dict) or not source_digests or any(not isinstance(key, str) or not DIGEST.fullmatch(digest) for key, digest in source_digests.items()):
        raise ContractError("QUALITY_SOURCE_DIGEST")
    if not isinstance(value["metrics"], dict) or not isinstance(value["flags"], list) or any(not isinstance(flag, str) for flag in value["flags"]):
        raise ContractError("QUALITY_ATTRIBUTE_CONTENT")
    return dict(value)


def _technical_reference(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != TECHNICAL_REFERENCE_KEYS:
        raise ContractError("TECHNICAL_VALIDATOR_REFERENCE")
    if value["schema_version"] != TECHNICAL_REFERENCE_SCHEMA or value["status"] not in {"PASS", "FAIL"} or not isinstance(value["result_digest"], str) or not DIGEST.fullmatch(value["result_digest"]):
        raise ContractError("TECHNICAL_VALIDATOR_REFERENCE")
    return dict(value)


def aggregate_episode_report(attributes: Sequence[Mapping[str, Any]], *, technical_validator: Mapping[str, Any]) -> dict[str, Any]:
    """Bind already-computed attributes; no metric is recalculated or scored here."""
    parsed = [validate_attribute_record(attribute) for attribute in attributes]
    if not parsed:
        raise ContractError("QUALITY_ATTRIBUTES_MISSING")
    binding = tuple(parsed[0][key] for key in ("run_id", "resolved_job_digest", "plan_digest"))
    if any(tuple(attribute[key] for key in ("run_id", "resolved_job_digest", "plan_digest")) != binding for attribute in parsed):
        raise ContractError("QUALITY_ATTRIBUTE_BINDING")
    names = [attribute["attribute"] for attribute in parsed]
    if len(names) != len(set(names)):
        raise ContractError("QUALITY_ATTRIBUTE_DUPLICATE")
    technical = _technical_reference(technical_validator)
    statuses = {attribute["status"] for attribute in parsed}
    status = "ERROR" if "ERROR" in statuses else "FLAGGED" if "FLAGGED" in statuses else "AVAILABLE" if "AVAILABLE" in statuses else "NOT_AVAILABLE"
    flags = list(dict.fromkeys(flag for attribute in parsed for flag in attribute["flags"]))
    if technical["status"] == "FAIL":
        flags.append("TECHNICAL_VALIDATOR_FAIL")
        if status == "AVAILABLE":
            status = "FLAGGED"
    return {"schema_version": REPORT_SCHEMA, "run_id": binding[0], "resolved_job_digest": binding[1], "plan_digest": binding[2], "technical_validator": technical, "attributes": sorted(parsed, key=lambda attribute: attribute["attribute"]), "status": status, "flags": flags}


def write_episode_report(path: str | Path, report: Mapping[str, Any]) -> None:
    """Publish one small report without replacing an existing run artifact."""
    path = Path(path)
    if not isinstance(report, Mapping) or set(report) != REPORT_KEYS or aggregate_episode_report(report["attributes"], technical_validator=report["technical_validator"]) != dict(report):
        raise ContractError("QUALITY_REPORT_SCHEMA")
    if path.name != "episode_quality.json" or not path.parent.is_dir() or path.parent.is_symlink():
        raise ContractError("QUALITY_REPORT_PATH")
    data = (json.dumps(dict(report), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    except (OSError, TypeError, ValueError) as exc:
        raise ContractError("QUALITY_REPORT_IO", str(exc)) from exc


def build_episode_report(
    path: str | Path,
    *,
    run_id: str,
    resolved_job_digest: str,
    plan_digest: str,
    plan: Mapping[str, Any],
    phase_events_path: str | Path,
    recorder_rows: Sequence[Mapping[str, Any]],
    recorder_rows_digest: str,
    recorder_ros_clock_type: str,
    execution_evidence: Mapping[str, Any],
    technical_validator: Mapping[str, Any],
    stall_epsilon_rad: float,
) -> dict[str, Any]:
    """Build and exclusively publish the one report view; source payloads stay in place."""
    from tools.data_factory.quality.execution_metrics import joint_execution_attribute
    from tools.data_factory.quality.interaction_metrics import interaction_quality_attribute
    from tools.data_factory.quality.phase_events import read_phase_events
    from tools.data_factory.quality.phase_metrics import phase_timing_attribute
    from tools.data_factory.quality.plan_metrics import plan_quality_attribute

    events = read_phase_events(phase_events_path)
    common = {"run_id": run_id, "resolved_job_digest": resolved_job_digest, "plan_digest": plan_digest}
    row_common = {**common, "plan": plan, "events": events, "recorder_rows": recorder_rows, "recorder_rows_digest": recorder_rows_digest, "recorder_ros_clock_type": recorder_ros_clock_type}
    attributes = [
        plan_quality_attribute(**common, plan=plan),
        phase_timing_attribute(**common, events=events, recorder_rows=recorder_rows, recorder_rows_digest=recorder_rows_digest, recorder_ros_clock_type=recorder_ros_clock_type),
        joint_execution_attribute(**row_common, stall_epsilon_rad=stall_epsilon_rad),
        interaction_quality_attribute(**row_common, execution_evidence=execution_evidence),
    ]
    report = aggregate_episode_report(attributes, technical_validator=technical_validator)
    write_episode_report(path, report)
    return report
