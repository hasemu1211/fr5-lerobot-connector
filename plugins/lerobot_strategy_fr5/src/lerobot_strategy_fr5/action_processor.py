from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from lerobot.configs import PipelineFeatureType, PolicyFeature
from lerobot.processor import (
    ProcessorStepRegistry,
    RobotAction,
    RobotActionProcessorStep,
)
from tools.data_factory.rollout.action_projection import project_gripper_position

GRIPPER_KEY = "finger_right_joint.pos"


def json_action(action: RobotAction) -> dict[str, float]:
    out = {}
    for key, value in action.items():
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"FR5_NONFINITE_ACTION: {key}")
        out[key] = value
    return out


def project_gripper_action(
    action: RobotAction,
    *,
    upper_m: float,
    projection_quanta: int,
):
    requested = json_action(action)

    if GRIPPER_KEY not in requested:
        raise ValueError("FR5_GRIPPER_ACTION_MISSING")

    metadata = project_gripper_position(
        requested[GRIPPER_KEY], upper_m=upper_m, projection_quanta=projection_quanta,
    )
    processed = dict(requested)
    processed[GRIPPER_KEY] = metadata["projected_m"]
    return processed, metadata


@ProcessorStepRegistry.register("fr5/bounded_gripper_projection")
@dataclass
class FR5BoundedGripperProjection(RobotActionProcessorStep):
    upper_m: float = 0.021
    projection_quanta: int = 2

    def action(self, action: RobotAction) -> RobotAction:
        processed, _ = project_gripper_action(
            action,
            upper_m=self.upper_m,
            projection_quanta=self.projection_quanta,
        )
        return processed

    def get_config(self) -> dict[str, Any]:
        return {
            "upper_m": self.upper_m,
            "projection_quanta": self.projection_quanta,
        }

    def transform_features(
        self,
        features: dict[PipelineFeatureType, dict[str, PolicyFeature]],
    ):
        return features
