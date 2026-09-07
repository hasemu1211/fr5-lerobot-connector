"""Joint-space execution metrics derived only from recorder rows and phase windows."""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from tools.data_factory.quality.phase_events import validate_phase_event_sequence
from tools.data_factory.quality.phase_metrics import phase_row_windows, quality_attribute
from tools.fr5_data_factory import ContractError, DIGEST, canonical_digest


def _vector(row: Mapping[str, Any], key: str) -> list[float]:
    value = row.get(key)
    if not isinstance(value, (list, tuple)) or len(value) != 7:
        raise ContractError("QUALITY_RECORDER_ROW")
    result = [float(item) for item in value]
    if any(not math.isfinite(item) for item in result):
        raise ContractError("QUALITY_RECORDER_ROW")
    return result


def joint_execution_attribute(
    *,
    run_id: str,
    resolved_job_digest: str,
    plan_digest: str,
    plan: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    recorder_rows: Sequence[Mapping[str, Any]],
    recorder_rows_digest: str,
    recorder_ros_clock_type: str,
    stall_epsilon_rad: float,
) -> dict[str, Any]:
    """Report raw joint tracking/progress values; it does not admit or delete data."""
    if not DIGEST.fullmatch(recorder_rows_digest) or not math.isfinite(stall_epsilon_rad) or stall_epsilon_rad <= 0:
        raise ContractError("EXECUTION_QUALITY_CONFIG")
    parsed_events = validate_phase_event_sequence(events, plan=plan)
    source_digests = {
        "phase_events": canonical_digest(parsed_events),
        "recorder_rows": recorder_rows_digest,
        "pickup_plan": canonical_digest(plan),
    }
    flags: list[str] = []
    if source_digests["pickup_plan"] != plan_digest:
        flags.append("PLAN_DIGEST_MISMATCH")
    if any(event["run_id"] != run_id or event["plan_digest"] != plan_digest for event in parsed_events):
        flags.append("PHASE_EVENT_BINDING_MISMATCH")
    windows, join_flags, _ = phase_row_windows(
        events=parsed_events,
        recorder_rows=recorder_rows,
        recorder_ros_clock_type=recorder_ros_clock_type,
        plan=plan,
    )
    flags.extend(join_flags)
    steps = plan.get("steps") if isinstance(plan, Mapping) else None
    if not isinstance(steps, list):
        raise ContractError("EXECUTION_QUALITY_PLAN")
    by_phase = {(step.get("phase"), index): child
                for step in steps if isinstance(step, Mapping)
                for index, child in enumerate(step.get("held_target_segments", [step]))}
    phase_metrics = []
    for window in windows:
        step = by_phase.get((window["phase"], window["segment_index"]))
        if not isinstance(step, Mapping) or step.get("type") != "ARM":
            continue
        target = step.get("final_joint_state")
        if not isinstance(target, list) or len(target) != 6:
            raise ContractError("EXECUTION_QUALITY_PLAN")
        target = [float(item) for item in target]
        indices = window["row_indices"]
        if not indices:
            flags.append(f"PHASE_ROWS_MISSING:{window['phase']}")
            continue
        states = [_vector(recorder_rows[index], "observation.state")[:6] for index in indices]
        actions = [_vector(recorder_rows[index], "action")[:6] for index in indices]
        distances = [math.dist(state, target) for state in states]
        tracking = [max(abs(state[joint] - action[joint]) for joint in range(6)) for state, action in zip(states, actions)]
        deltas = [previous - current for previous, current in zip(distances, distances[1:])]
        phase_metrics.append({
            "phase": window["phase"],
            "segment_index": window["segment_index"],
            "row_count": len(indices),
            "endpoint_joint_error_max_rad": max(abs(states[-1][joint] - target[joint]) for joint in range(6)),
            "tracking_error_max_rad": max(tracking),
            "tracking_error_mean_rad": sum(tracking) / len(tracking),
            "target_distance_start_rad": distances[0],
            "target_distance_end_rad": distances[-1],
            "negative_progress_ratio": None if not deltas else sum(delta < -stall_epsilon_rad for delta in deltas) / len(deltas),
            "stall_ratio": None if not deltas else sum(abs(delta) <= stall_epsilon_rad for delta in deltas) / len(deltas),
        })
    metrics = {
        "stall_epsilon_rad": stall_epsilon_rad,
        "phase_metrics": phase_metrics,
        "tcp_phase_metrics_status": "NOT_AVAILABLE",
        "tcp_phase_metrics_reason": "FK_TF_UNQUALIFIED",
    }
    status = "ERROR" if any(flag.endswith("MISMATCH") for flag in flags) else "NOT_AVAILABLE" if not phase_metrics else "FLAGGED" if flags else "AVAILABLE"
    return quality_attribute(
        attribute="joint_execution_quality",
        run_id=run_id,
        resolved_job_digest=resolved_job_digest,
        plan_digest=plan_digest,
        source_digests=source_digests,
        status=status,
        metrics=metrics,
        flags=flags,
    )
