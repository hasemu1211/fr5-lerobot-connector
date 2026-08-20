"""Pure, post-run phase timing and recorder-window metrics."""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from tools.fr5_data_factory import ContractError, DIGEST, SAFE_ID, canonical_digest
from tools.data_factory.quality.phase_events import validate_phase_event, writer_resource_contract


ATTRIBUTE_SCHEMA = "data_factory.quality_attribute.v1"
STATUS = frozenset({"AVAILABLE", "FLAGGED", "NOT_AVAILABLE", "ERROR"})


def _target_ns(row: Mapping[str, Any]) -> int:
    value = row.get("target_ros_s")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ContractError("RECORDER_TARGET_ROS_TIME")
    return int(round(float(value) * 1_000_000_000))


def quality_attribute(*, attribute: str, run_id: str, resolved_job_digest: str, plan_digest: str, source_digests: Mapping[str, str], status: str, metrics: Mapping[str, Any], flags: Sequence[str]) -> dict[str, Any]:
    if (
        status not in STATUS
        or not isinstance(attribute, str)
        or not attribute
        or not isinstance(run_id, str)
        or not SAFE_ID.fullmatch(run_id)
        or not DIGEST.fullmatch(resolved_job_digest)
        or not DIGEST.fullmatch(plan_digest)
    ):
        raise ContractError("QUALITY_ATTRIBUTE_BINDING")
    if not source_digests or any(not isinstance(key, str) or not DIGEST.fullmatch(value) for key, value in source_digests.items()):
        raise ContractError("QUALITY_SOURCE_DIGEST")
    return {"schema_version": ATTRIBUTE_SCHEMA, "attribute": attribute, "run_id": run_id, "resolved_job_digest": resolved_job_digest, "plan_digest": plan_digest, "source_digests": dict(source_digests), "status": status, "metrics": dict(metrics), "flags": list(dict.fromkeys(flags))}


def phase_intervals(events: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """Build only unambiguous accepted-to-terminal intervals from control events."""
    parsed = [validate_phase_event(event) for event in events]
    flags: list[str] = []
    if not parsed:
        return [], ["PHASE_EVENTS_MISSING"], None
    sequences = [event["sequence"] for event in parsed]
    if sequences != list(range(len(sequences))):
        flags.append("PHASE_EVENT_SEQUENCE_GAP")
    clocks = {event["ros_clock_type"] for event in parsed}
    if len(clocks) != 1:
        flags.append("PHASE_EVENT_CLOCK_MISMATCH")
        return [], flags, None
    pending: dict[tuple[str, int | None], dict[str, Any]] = {}
    intervals: list[dict[str, Any]] = []
    for event in parsed:
        key = (event["phase"], event["segment_index"])
        if event["event"] == "GOAL_ACCEPTED":
            if key in pending:
                flags.append("PHASE_INTERVAL_OVERLAP")
            pending[key] = event
        elif event["event"] == "ACTION_TERMINAL":
            start = pending.pop(key, None)
            if start is None:
                flags.append("PHASE_TERMINAL_WITHOUT_ACCEPTED")
            elif event["event_ros_time_ns"] < start["event_ros_time_ns"]:
                flags.append("PHASE_EVENT_TIME_REVERSED")
            else:
                intervals.append({"phase": event["phase"], "segment_index": event["segment_index"], "segment_count": event["segment_count"], "start_ros_time_ns": start["event_ros_time_ns"], "end_ros_time_ns": event["event_ros_time_ns"], "duration_s": (event["event_ros_time_ns"] - start["event_ros_time_ns"]) / 1_000_000_000, "terminal_action_status": event["action_status"]})
    if pending:
        flags.append("PHASE_TERMINAL_MISSING")
    intervals.sort(key=lambda interval: interval["start_ros_time_ns"])
    if any(current["start_ros_time_ns"] < previous["end_ros_time_ns"] for previous, current in zip(intervals, intervals[1:])):
        flags.append("PHASE_INTERVAL_OVERLAP")
    return intervals, list(dict.fromkeys(flags)), next(iter(clocks))


def phase_row_windows(*, events: Sequence[Mapping[str, Any]], recorder_rows: Sequence[Mapping[str, Any]], recorder_ros_clock_type: str) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """Return row indices only; callers keep the dataset payload in its original owner."""
    intervals, flags, event_clock = phase_intervals(events)
    if event_clock is None or recorder_ros_clock_type != event_clock:
        return [], [*flags, "RECORDER_CLOCK_UNQUALIFIED"], event_clock
    if flags or not intervals:
        return [], [*flags, "RECORDER_ROWS_NOT_JOINED"], event_clock
    targets = [_target_ns(row) for row in recorder_rows]
    if targets != sorted(targets):
        return [], ["RECORDER_ROW_TIME_REVERSED"], event_clock
    windows = [{**interval, "row_indices": []} for interval in intervals]
    for row_index, target in enumerate(targets):
        matches = [index for index, interval in enumerate(intervals) if interval["start_ros_time_ns"] <= target <= interval["end_ros_time_ns"]]
        if len(matches) > 1:
            return [], ["RECORDER_ROW_INTERVAL_AMBIGUOUS"], event_clock
        if matches:
            windows[matches[0]]["row_indices"].append(row_index)
    return windows, [], event_clock


def phase_timing_attribute(*, run_id: str, resolved_job_digest: str, plan_digest: str, events: Sequence[Mapping[str, Any]], recorder_rows: Sequence[Mapping[str, Any]] | None = None, recorder_rows_digest: str | None = None, recorder_ros_clock_type: str | None = None) -> dict[str, Any]:
    """Report phase intervals and, only with an explicit same-clock qualification, row windows."""
    parsed_events = [validate_phase_event(event) for event in events]
    intervals, flags, event_clock = phase_intervals(parsed_events)
    source_digests = {"phase_events": canonical_digest(parsed_events)}
    metrics: dict[str, Any] = {"event_count": len(events), "phase_intervals": intervals, "event_ros_clock_type": event_clock, "row_window_status": "NOT_AVAILABLE", "joined_row_count": 0, "writer_resource_contract": writer_resource_contract()}
    if any(event["run_id"] != run_id or event["plan_digest"] != plan_digest for event in parsed_events):
        flags.append("PHASE_EVENT_BINDING_MISMATCH")
    if recorder_rows is not None:
        if not isinstance(recorder_rows_digest, str) or not DIGEST.fullmatch(recorder_rows_digest):
            raise ContractError("QUALITY_SOURCE_DIGEST")
        source_digests["recorder_rows"] = recorder_rows_digest
        windows, join_flags, _ = phase_row_windows(events=parsed_events, recorder_rows=recorder_rows, recorder_ros_clock_type=recorder_ros_clock_type or "")
        flags.extend(join_flags)
        if windows:
            counts = { (window["phase"], window["segment_index"]): len(window["row_indices"]) for window in windows }
            for interval in intervals:
                interval["row_count"] = counts[(interval["phase"], interval["segment_index"])]
            metrics["row_window_status"] = "AVAILABLE"
            metrics["joined_row_count"] = sum(counts.values())
    status = "ERROR" if "PHASE_EVENT_BINDING_MISMATCH" in flags else "NOT_AVAILABLE" if flags or not intervals else "AVAILABLE"
    return quality_attribute(attribute="phase_timing_integrity", run_id=run_id, resolved_job_digest=resolved_job_digest, plan_digest=plan_digest, source_digests=source_digests, status=status, metrics=metrics, flags=flags)
