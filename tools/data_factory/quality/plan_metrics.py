"""Plan-only metrics derived from one immutable compiled pickup plan."""
from __future__ import annotations

import base64
import binascii
import math
from typing import Any, Mapping

from tools.data_factory.quality.phase_metrics import quality_attribute
from tools.fr5_data_factory import ContractError, canonical_digest


def _joints(value: Any) -> list[float]:
    if not isinstance(value, list) or len(value) != 6:
        raise ContractError("PLAN_QUALITY_JOINTS")
    joints = [float(item) for item in value]
    if any(not math.isfinite(item) for item in joints):
        raise ContractError("PLAN_QUALITY_JOINTS")
    return joints


def plan_quality_attribute(*, run_id: str, resolved_job_digest: str, plan_digest: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Report deterministic plan facts; trajectory/TCP shape stays unavailable until qualified."""
    if not isinstance(plan, Mapping) or plan.get("schema_version") != "fr5.pickup_plan.v3":
        raise ContractError("PLAN_QUALITY_PLAN")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ContractError("PLAN_QUALITY_PLAN")
    flags = []
    if canonical_digest(plan) != plan_digest or plan.get("run_id") != run_id or plan.get("resolved_job_digest") != resolved_job_digest:
        flags.append("PLAN_BINDING_MISMATCH")
    metrics = []
    previous = _joints(plan.get("initial_joint_state"))
    chain_error = 0.0
    for step in steps:
        if not isinstance(step, Mapping) or step.get("type") not in {"ARM", "GRIPPER"} or not isinstance(step.get("phase"), str):
            raise ContractError("PLAN_QUALITY_STEP")
        start, final = _joints(step.get("start_joint_state")), _joints(step.get("final_joint_state"))
        chain_error = max(chain_error, *(abs(left - right) for left, right in zip(previous, start)))
        encoded = step.get("trajectory_b64")
        continuation = step.get("continuation_trajectory_b64")
        if not isinstance(encoded, str) or continuation is not None and not isinstance(continuation, str):
            raise ContractError("PLAN_QUALITY_TRAJECTORY")
        try:
            payload_bytes = sum(
                len(base64.b64decode(value, validate=True))
                for value in (encoded, continuation) if value is not None
            )
        except (binascii.Error, ValueError) as exc:
            raise ContractError("PLAN_QUALITY_TRAJECTORY") from exc
        if payload_bytes == 0:
            raise ContractError("PLAN_QUALITY_TRAJECTORY")
        has_index = "segment_index" in step
        if has_index != ("segment_count" in step):
            raise ContractError("PLAN_QUALITY_SEGMENT")
        segment_index = step.get("segment_index", 0)
        segment_count = step.get("segment_count")
        if has_index and (
            type(segment_index) is not int or type(segment_count) is not int
            or segment_index < 0 or segment_count <= segment_index
        ):
            raise ContractError("PLAN_QUALITY_SEGMENT")
        metrics.append({
            "phase": step["phase"],
            "type": step["type"],
            "segment_index": segment_index,
            "serialized_bytes": payload_bytes,
            "start_to_final_joint_distance_rad": math.dist(start, final),
        })
        previous = final
    return quality_attribute(
        attribute="plan_quality",
        run_id=run_id,
        resolved_job_digest=resolved_job_digest,
        plan_digest=plan_digest,
        source_digests={"pickup_plan": canonical_digest(plan)},
        status="ERROR" if flags else "AVAILABLE",
        metrics={
            "phase_metrics": metrics,
            "chain_error_max_rad": chain_error,
            "trajectory_shape_status": "NOT_AVAILABLE",
            "trajectory_shape_reason": "ROS_TRAJECTORY_TIMELINE_UNQUALIFIED",
            "tcp_plan_metrics_status": "NOT_AVAILABLE",
            "tcp_plan_metrics_reason": "FK_TF_UNQUALIFIED",
        },
        flags=flags,
    )
