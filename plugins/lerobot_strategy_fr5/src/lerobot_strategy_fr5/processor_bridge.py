from __future__ import annotations

from lerobot.lerobot_types import TransitionKey
from lerobot.processor import make_default_robot_action_processor

from .action_processor import (
    FR5BoundedGripperProjection,
    json_action,
    project_gripper_action,
)


def make_fr5_robot_action_processor(
    evidence_sink,
    *,
    upper_m: float = 0.021,
    projection_quanta: int = 2,
):
    pipeline = make_default_robot_action_processor()

    projection = FR5BoundedGripperProjection(
        upper_m=upper_m,
        projection_quanta=projection_quanta,
    )

    pipeline.steps = [*pipeline.steps, projection]
    target_index = len(pipeline.steps) - 1
    pending = {"action": None}

    def before_step(index, transition):
        if index != target_index:
            return

        action = transition.get(TransitionKey.ACTION.value)
        if not isinstance(action, dict):
            raise RuntimeError("FR5_ACTION_EVIDENCE_INPUT")

        requested = json_action(action)
        pending["action"] = requested

        evidence_sink.emit(
            "ACTION_REQUESTED",
            {
                "interface":
                    "lerobot.robot_action_processor.before_step",
                "action": requested,
            },
        )

    def after_step(index, transition):
        if index != target_index:
            return

        requested = pending["action"]
        action = transition.get(TransitionKey.ACTION.value)

        if requested is None or not isinstance(action, dict):
            raise RuntimeError("FR5_ACTION_EVIDENCE_OUTPUT")

        processed = json_action(action)
        expected, projection_meta = project_gripper_action(
            requested,
            upper_m=upper_m,
            projection_quanta=projection_quanta,
        )

        if processed != expected:
            raise RuntimeError("FR5_ACTION_EVIDENCE_MISMATCH")

        evidence_sink.emit(
            "ACTION_PROCESSED",
            {
                "interface":
                    "lerobot.robot_action_processor.after_step",
                "action": processed,
                "gripper_projection": projection_meta,
            },
        )
        pending["action"] = None

    pipeline.before_step_hooks.append(before_step)
    pipeline.after_step_hooks.append(after_step)

    return pipeline
