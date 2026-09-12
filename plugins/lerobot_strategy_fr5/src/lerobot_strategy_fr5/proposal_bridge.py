from __future__ import annotations

import math
from typing import Any

import numpy as np
import torch

from tools.fr5_data_factory import ContractError, canonical_digest
from tools.data_factory.rollout.finite_plan import (
    JOINTS,
    PROPOSAL_SCHEMA,
    UNITS,
    validate_proposal,
)

from .action_processor import project_gripper_action
from .chunk_tap import CapturedPolicyChunk
from .evidence import EvidenceSink


def _tensor_record(value: torch.Tensor) -> dict:
    """Small exact chunk payload, not another call to a stateful processor."""
    import hashlib

    array = value.detach().cpu().contiguous().numpy()
    data = array.tobytes()
    return {
        "dtype": array.dtype.str,
        "shape": list(array.shape),
        "data_hex": data.hex(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


ACTION_KEYS = (
    "j1.pos",
    "j2.pos",
    "j3.pos",
    "j4.pos",
    "j5.pos",
    "j6.pos",
    "finger_right_joint.pos",
)


def _finite_float(value: Any, code: str) -> float:
    if isinstance(value, bool):
        raise RuntimeError(code)

    value = float(value)

    if not math.isfinite(value):
        raise RuntimeError(code)

    return value


def _rgb_digest_value(value: Any, code: str) -> dict:
    image = np.asarray(value)

    if (
        image.dtype != np.uint8
        or image.ndim != 3
        or image.shape[2] != 3
    ):
        raise RuntimeError(code)

    image = np.ascontiguousarray(image)

    return {
        "dtype": "uint8",
        "color_space": "RGB",
        "shape": list(image.shape),
        "data": image.tobytes().hex(),
    }


def observation_digest(
    robot_observation: dict,
    *,
    instruction: str,
) -> str:
    if not isinstance(instruction, str) or not instruction.strip():
        raise RuntimeError("FR5_INSTRUCTION")

    try:
        state = [
            _finite_float(
                robot_observation[key],
                "FR5_OBSERVATION_STATE",
            )
            for key in ACTION_KEYS
        ]

        up = _rgb_digest_value(
            robot_observation["up"],
            "FR5_OBSERVATION_UP",
        )
        wrist = _rgb_digest_value(
            robot_observation["wrist"],
            "FR5_OBSERVATION_WRIST",
        )
    except KeyError as exc:
        raise RuntimeError(
            "FR5_OBSERVATION_SCHEMA"
        ) from exc

    # Match FinitePolicyInference's existing digest domain:
    # state + camera1 + camera2 + task.
    return canonical_digest({
        "observation.state": state,
        "observation.images.camera1": up,
        "observation.images.camera2": wrist,
        "task": instruction,
    })


def source_timestamps_from_evidence(
    observation_evidence: dict,
) -> dict[str, float]:
    try:
        source = observation_evidence["source_timestamps_s"]

        result = {
            "state": _finite_float(
                source["state"],
                "FR5_SOURCE_TIMESTAMP",
            ),
            "camera1": _finite_float(
                source["up"],
                "FR5_SOURCE_TIMESTAMP",
            ),
            "camera2": _finite_float(
                source["wrist"],
                "FR5_SOURCE_TIMESTAMP",
            ),
        }
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            "FR5_SOURCE_TIMESTAMP"
        ) from exc

    return result


def project_processed_chunk(
    processed_chunk: torch.Tensor,
    *,
    gripper_upper_m: float = 0.021,
    gripper_projection_quanta: int = 2,
) -> tuple[list[list[float]], dict]:
    if not isinstance(processed_chunk, torch.Tensor):
        raise RuntimeError("FR5_PROCESSED_CHUNK_TYPE")

    value = processed_chunk.detach().cpu()

    if value.ndim == 3:
        if value.shape[0] != 1:
            raise RuntimeError("FR5_PROCESSED_CHUNK_SHAPE")
        value = value[0]

    if (
        value.ndim != 2
        or value.shape[1] != 7
        or not 1 <= value.shape[0] <= 50
    ):
        raise RuntimeError("FR5_PROCESSED_CHUNK_SHAPE")

    rows: list[list[float]] = []
    changed_rows = 0
    max_delta_m = 0.0

    for tensor_row in value:
        row = [
            _finite_float(v.item(), "FR5_PROCESSED_ACTION")
            for v in tensor_row
        ]

        action = dict(zip(ACTION_KEYS, row))

        projected, _metadata = project_gripper_action(
            action,
            upper_m=gripper_upper_m,
            projection_quanta=gripper_projection_quanta,
        )

        # Projection authority is gripper-only.
        for key in ACTION_KEYS[:6]:
            if projected[key] != action[key]:
                raise RuntimeError("FR5_ARM_PROJECTION_FORBIDDEN")

        original_gripper = action[ACTION_KEYS[-1]]
        projected_gripper = projected[ACTION_KEYS[-1]]
        delta = abs(projected_gripper - original_gripper)

        if delta > 0:
            changed_rows += 1

        max_delta_m = max(max_delta_m, delta)

        rows.append([
            _finite_float(
                projected[key],
                "FR5_PROJECTED_ACTION",
            )
            for key in ACTION_KEYS
        ])

    return rows, {
        "rows": len(rows),
        "gripper_projection_rows": changed_rows,
        "max_gripper_projection_delta_m": max_delta_m,
        "gripper_upper_m": float(gripper_upper_m),
        "gripper_projection_quanta": int(
            gripper_projection_quanta
        ),
        "projected_actions_digest": canonical_digest(rows),
    }


def build_finite_proposal(
    capture: CapturedPolicyChunk,
    processed_chunk: torch.Tensor,
    *,
    robot_observation: dict,
    observation_evidence: dict,
    instruction: str,
    checkpoint: dict,
    robot_description: str,
    period_s: float,
    inference_started_at_s: float,
    inference_completed_at_s: float,
    inference_duration_s: float,
    max_observation_age_s: float = 0.3,
    velocity_scaling: float = 0.03,
    gripper_upper_m: float = 0.021,
    gripper_projection_quanta: int = 2,
    evidence_sink: EvidenceSink | None = None,
) -> tuple[dict, dict]:
    if not isinstance(capture, CapturedPolicyChunk):
        raise RuntimeError("FR5_CHUNK_CAPTURE")

    if not isinstance(robot_description, str) or not robot_description:
        raise RuntimeError("FR5_ROBOT_DESCRIPTION")

    try:
        initial_state = [
            _finite_float(
                robot_observation[key],
                "FR5_INITIAL_STATE",
            )
            for key in ACTION_KEYS
        ]
    except KeyError as exc:
        raise RuntimeError("FR5_INITIAL_STATE") from exc

    actions, projection = project_processed_chunk(
        processed_chunk,
        gripper_upper_m=gripper_upper_m,
        gripper_projection_quanta=gripper_projection_quanta,
    )

    started = _finite_float(
        inference_started_at_s,
        "FR5_INFERENCE_TIME",
    )
    completed = _finite_float(
        inference_completed_at_s,
        "FR5_INFERENCE_TIME",
    )
    duration = _finite_float(
        inference_duration_s,
        "FR5_INFERENCE_TIME",
    )

    if completed < started or duration < 0:
        raise RuntimeError("FR5_INFERENCE_TIME")

    proposal = {
        "schema_version": PROPOSAL_SCHEMA,
        "checkpoint": dict(checkpoint),
        "instruction": instruction,
        "observation_digest": observation_digest(
            robot_observation,
            instruction=instruction,
        ),
        "initial_state": initial_state,
        "source_clock": "SYSTEM_TIME",
        "source_timestamps_s":
            source_timestamps_from_evidence(
                observation_evidence
            ),
        "max_observation_age_s":
            float(max_observation_age_s),
        "inference_duration_s": duration,
        "inference_started_at_s": started,
        "inference_completed_at_s": completed,
        "joint_order": list(JOINTS),
        "units": list(UNITS),
        "action_semantics": "ABSOLUTE_JOINT_POSITION",
        "actions": actions,
        "period_s": float(period_s),
        "robot_description": robot_description,
        "velocity_scaling": float(velocity_scaling),
    }

    proposal["proposal_digest"] = canonical_digest(proposal)

    # A candidate is not an admitted proposal or an execution trace. Preserve it
    # before the unchanged validator can reject it (e.g. velocity limits).
    candidate_event = None
    if evidence_sink is not None:
        raw = _tensor_record(capture.raw)
        if raw["sha256"] != capture.raw_sha256:
            raise RuntimeError("FR5_CAPTURE_DIGEST_CHANGED")
        candidate_event = evidence_sink.emit("PROPOSAL_CANDIDATE", {
            "schema_version": "data_factory.rollout_proposal_candidate.v1",
            "chunk_sequence": capture.sequence,
            "raw_chunk": raw,
            "processed_chunk": _tensor_record(processed_chunk),
            "observation_evidence": observation_evidence,
            "proposal_candidate": proposal,
            "projection": projection,
        })

    def publish_validation(status: str, code: str | None):
        if evidence_sink is not None:
            evidence_sink.emit("PROPOSAL_VALIDATION", {
                "candidate_event_digest": candidate_event,
                "status": status,
                "code": code,
                # This function cannot dispatch. This is NOT a whole-run or
                # transport-level assertion that no earlier/later command ran.
                "dispatch_scope": "PROPOSAL_BUILDER_ONLY",
                "dispatch": "NOT_ATTEMPTED",
            })

    try:
        proposal = validate_proposal(proposal)
    except Exception as exc:
        try:
            publish_validation(
                "BLOCKED" if isinstance(exc, ContractError) else "ERROR",
                str(exc) if isinstance(exc, ContractError) else type(exc).__name__,
            )
        except Exception as evidence_error:
            # Keep the original rejection, including when storage fails. A
            # candidate without a matching result remains explicitly incomplete.
            exc.add_note(f"FR5_EVIDENCE_PUBLICATION_FAILED: {type(evidence_error).__name__}")
        raise
    publish_validation("VALID", None)

    evidence = {
        "chunk_sequence": capture.sequence,
        "raw_chunk_sha256": capture.raw_sha256,
        "proposal_digest": proposal["proposal_digest"],
        **projection,
    }

    return proposal, evidence
