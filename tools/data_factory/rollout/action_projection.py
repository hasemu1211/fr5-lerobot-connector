"""Pure FR5 gripper projection shared by execution adapters and evidence replay."""
from __future__ import annotations

import math


def project_gripper_position(raw: float, *, upper_m: float, projection_quanta: int) -> dict:
    if (not math.isfinite(raw) or not math.isfinite(upper_m) or upper_m <= 0
            or type(projection_quanta) is not int or projection_quanta < 0):
        raise ValueError("FR5_GRIPPER_PROJECTION_CONFIG")
    quantum = upper_m / 100.0
    slack = projection_quanta * quantum
    if not -slack <= raw <= upper_m + slack:
        raise ValueError(f"FR5_GRIPPER_ACTION_OUT_OF_RANGE: {raw}")
    bounded = min(upper_m, max(0.0, raw))
    percent = math.floor(bounded * 100.0 / upper_m + 0.5)
    return {
        "requested_m": raw,
        "bounded_m": bounded,
        "fairino_percent": percent,
        "projected_m": percent * upper_m / 100.0,
        "quantum_m": quantum,
        "projection_slack_m": slack,
    }
