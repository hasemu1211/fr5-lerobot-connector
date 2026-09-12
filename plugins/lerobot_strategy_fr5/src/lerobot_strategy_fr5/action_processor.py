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

    raw = requested[GRIPPER_KEY]
    quantum = upper_m / 100.0
    slack = projection_quanta * quantum

    if not -slack <= raw <= upper_m + slack:
        raise ValueError(f"FR5_GRIPPER_ACTION_OUT_OF_RANGE: {raw}")

    bounded = min(upper_m, max(0.0, raw))
    percent = math.floor(bounded * 100.0 / upper_m + 0.5)
    projected = percent * upper_m / 100.0

    processed = dict(requested)
    processed[GRIPPER_KEY] = projected

    return processed, {
        "requested_m": raw,
        "bounded_m": bounded,
        "fairino_percent": percent,
        "projected_m": projected,
        "quantum_m": quantum,
        "projection_slack_m": slack,
    }


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
