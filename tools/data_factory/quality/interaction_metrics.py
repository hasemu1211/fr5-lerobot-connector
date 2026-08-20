"""Gripper/lift interaction metrics derived without visual semantic inference."""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from tools.data_factory.quality.phase_events import validate_phase_event
from tools.data_factory.quality.phase_metrics import phase_row_windows, quality_attribute
from tools.fr5_data_factory import ContractError, DIGEST, canonical_digest


def _gripper(row: Mapping[str, Any], key: str) -> float:
    value = row.get(key)
    if not isinstance(value, (list, tuple)) or len(value) != 7:
        raise ContractError("QUALITY_RECORDER_ROW")
    result = float(value[6])
    if not math.isfinite(result):
        raise ContractError("QUALITY_RECORDER_ROW")
    return result


def interaction_quality_attribute(
    *,
    run_id: str,
    resolved_job_digest: str,
    plan_digest: str,
    plan: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    recorder_rows: Sequence[Mapping[str, Any]],
    recorder_rows_digest: str,
    recorder_ros_clock_type: str,
    execution_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Report qualified contact-window and lift continuity evidence, never camera semantics."""
    if not DIGEST.fullmatch(recorder_rows_digest) or not isinstance(execution_evidence, Mapping):
        raise ContractError("INTERACTION_QUALITY_CONFIG")
    parsed_events = [validate_phase_event(event) for event in events]
    source_digests = {
        "phase_events": canonical_digest(parsed_events),
        "recorder_rows": recorder_rows_digest,
        "pickup_plan": canonical_digest(plan),
        "execution_evidence": canonical_digest(execution_evidence),
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
    )
    flags.extend(join_flags)
    by_phase = {window["phase"]: window for window in windows}
    requirements = plan.get("gripper_requirements") if isinstance(plan, Mapping) else None
    if not isinstance(requirements, Mapping):
        raise ContractError("INTERACTION_QUALITY_PLAN")
    feedback_window = requirements.get("acceptable_feedback_m")
    if not isinstance(feedback_window, Mapping) or set(feedback_window) != {"min", "max"}:
        raise ContractError("INTERACTION_QUALITY_PLAN")

    close = by_phase.get("GRIPPER_CLOSE")
    close_metric = None
    if close and close["row_indices"]:
        rows = close["row_indices"]
        feedback = _gripper(recorder_rows[rows[-1]], "observation.state")
        action = _gripper(recorder_rows[rows[-1]], "action")
        in_window = float(feedback_window["min"]) <= feedback <= float(feedback_window["max"])
        close_metric = {
            "duration_s": close["duration_s"],
            "row_count": len(rows),
            "command_position_m": float(requirements["command_position_m"]),
            "feedback_end_m": feedback,
            "action_end_m": action,
            "acceptable_feedback_m": {"min": float(feedback_window["min"]), "max": float(feedback_window["max"])},
            "feedback_in_window": in_window,
        }
        if not in_window:
            flags.append("GRIPPER_FEEDBACK_OUT_OF_WINDOW")
    else:
        flags.append("GRIPPER_CLOSE_ROWS_MISSING")

    lift = by_phase.get("LIFT_LIN")
    lift_metric = None
    if lift and lift["row_indices"]:
        rows = lift["row_indices"]
        start = _gripper(recorder_rows[rows[0]], "observation.state")
        end = _gripper(recorder_rows[rows[-1]], "observation.state")
        lift_metric = {
            "duration_s": lift["duration_s"],
            "row_count": len(rows),
            "feedback_start_m": start,
            "feedback_end_m": end,
            "continuity_delta_m": abs(end - start),
        }
    else:
        flags.append("LIFT_ROWS_MISSING")

    grasp_verdict = execution_evidence.get("grasp_verdict")
    semantic_verdict = execution_evidence.get("semantic_verdict")
    if grasp_verdict not in {None, "PASS", "FAIL"} or semantic_verdict not in {None, "PASS", "FAIL"}:
        raise ContractError("INTERACTION_QUALITY_EVIDENCE")
    metrics = {
        "gripper_close": close_metric,
        "lift_continuity": lift_metric,
        "grasp_verdict": grasp_verdict,
        "semantic_verdict": semantic_verdict,
        "camera_semantic_authority": False,
    }
    status = "ERROR" if any(flag.endswith("MISMATCH") for flag in flags) else "NOT_AVAILABLE" if close_metric is None and lift_metric is None else "FLAGGED" if flags else "AVAILABLE"
    return quality_attribute(
        attribute="interaction_quality",
        run_id=run_id,
        resolved_job_digest=resolved_job_digest,
        plan_digest=plan_digest,
        source_digests=source_digests,
        status=status,
        metrics=metrics,
        flags=flags,
    )
