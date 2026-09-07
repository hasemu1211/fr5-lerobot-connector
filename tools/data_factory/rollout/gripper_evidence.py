"""Native held-command evidence, carried by the existing dynamic state broadcaster.

The controller calendar is not UTC. A bounded, same-incarnation clock mapping
must be supplied by the runtime owner; this module does not estimate or approve it.
"""
from __future__ import annotations

import copy
import datetime
import math

from tools.fr5_data_factory import ContractError

RESOURCE = "fr5_gripper_execution"
FIELDS = (
    "version", "incarnation_0", "incarnation_1", "incarnation_2", "incarnation_3",
    "generation", "active_generation", "completed_generation", "raw_reference_m",
    "sample_system_s", "sample_steady_s", "command_started_system_s",
    "year", "month", "day", "hour", "minute", "second", "millisecond", "frame",
    "feedback_m", "completion_reason", "pending", "rpc_active", "arm_resumed",
    "stopped", "error", "valid", "completion_year", "completion_month", "completion_day",
    "completion_hour", "completion_minute", "completion_second", "completion_millisecond",
)
CALENDAR = ("year", "month", "day", "hour", "minute", "second", "millisecond")
CLOCK_FIELDS = {"schema_version", "incarnation", "calendar_to_system_offset_s", "uncertainty_s",
                "system_anchor_s", "steady_anchor_s", "valid_until_system_s"}


def number(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError()
        return value
    except (ValueError, OverflowError) as exc:
        raise ContractError("LEARNED_HARDWARE_SCHEMA") from exc


def integer(value, upper):
    value = number(value)
    if not 0 <= value <= upper or value != int(value):
        raise ContractError("LEARNED_HARDWARE_SCHEMA")
    return int(value)


def identity(wire):
    return [integer(wire[f"incarnation_{i}"], 2**32 - 1) for i in range(4)]


def validate_clock_binding(binding):
    try:
        if set(binding) != CLOCK_FIELDS or binding["schema_version"] != "fr5.gripper_source_clock.v1":
            raise ValueError()
        ids = binding["incarnation"]
        if not isinstance(ids, list) or len(ids) != 4 or not any(ids):
            raise ValueError()
        for value in ids:
            integer(value, 2**32 - 1)
        for field in CLOCK_FIELDS - {"schema_version", "incarnation"}:
            number(binding[field])
        if (binding["uncertainty_s"] < 0 or binding["steady_anchor_s"] < 0
                or binding["valid_until_system_s"] <= binding["system_anchor_s"]):
            raise ValueError()
        return copy.deepcopy(binding)
    except (TypeError, KeyError, ValueError, ContractError) as exc:
        raise ContractError("LEARNED_HARDWARE_CLOCK_BINDING") from exc


def decode_dynamic_state(message, binding, received_steady_s):
    """Decode actual control_msgs/DynamicJointState; header receipt is not source time."""
    try:
        names = list(message.joint_names)
        if len(names) != len(set(names)) or len(names) != len(message.interface_values):
            raise ValueError()
        values = message.interface_values[names.index(RESOURCE)]
        fields = list(values.interface_names)
        if len(fields) != len(set(fields)) or set(fields) != set(FIELDS) or len(fields) != len(values.values):
            raise ValueError()
        wire = dict(zip(fields, values.values))
        for value in wire.values():
            number(value)
        return {"wire": wire, "clock_binding": validate_clock_binding(binding),
                "received_steady_s": number(received_steady_s)}
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        raise ContractError("LEARNED_HARDWARE_SCHEMA") from exc


def calendar_s(wire, prefix=""):
    try:
        values = [integer(wire[prefix + key], 9999) for key in CALENDAR]
        if values[-1] > 999:
            raise ValueError()
        # UTC here is only a deterministic calendar encoding. The explicit measured
        # offset below supplies the relationship to this host's SYSTEM clock.
        return datetime.datetime(*values[:6], values[6] * 1000,
                                 tzinfo=datetime.timezone.utc).timestamp()
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ContractError("LEARNED_HARDWARE_SOURCE_CLOCK") from exc


def check_hardware(evidence, now, steady_now, max_age_s, *, completion=False):
    try:
        hw = evidence["snapshot"]["gripper_controller"]["hardware_execution"]
        if not isinstance(hw, dict) or set(hw) != {"wire", "clock_binding", "received_steady_s"}:
            raise ValueError()
        wire = hw["wire"]
        if not isinstance(wire, dict) or set(wire) != set(FIELDS):
            raise ValueError()
        for value in wire.values():
            number(value)
        mapping = validate_clock_binding(hw["clock_binding"])
        if not any(identity(wire)) or identity(wire) != mapping["incarnation"]:
            raise ContractError("LEARNED_HARDWARE_INCARNATION")
        for key in ("generation", "active_generation", "completed_generation"):
            integer(wire[key], 2**53 - 1)
        integer(wire["frame"], 255)
        for key in ("pending", "rpc_active", "arm_resumed", "stopped", "valid"):
            integer(wire[key], 1)
        if wire["version"] != 1 or wire["valid"] != 1:
            raise ContractError("LEARNED_HARDWARE_INVALID")
        if (wire["pending"] or wire["rpc_active"] or wire["stopped"] or wire["error"]
                or not wire["arm_resumed"] or wire["active_generation"]):
            raise ContractError("LEARNED_HARDWARE_UNRESOLVED")
        now, steady_now = number(now), number(steady_now)
        uncertainty = mapping["uncertainty_s"]
        if (not mapping["system_anchor_s"] <= now <= mapping["valid_until_system_s"]
                or abs((now - mapping["system_anchor_s"]) - (steady_now - mapping["steady_anchor_s"])) > uncertainty):
            raise ContractError("LEARNED_HARDWARE_SOURCE_CLOCK")
        # Monotonic age independently rejects frozen SYSTEM/ROS time and newly
        # delivered old broadcaster messages. All clocks here are on one host.
        if not wire["sample_steady_s"] <= number(hw["received_steady_s"]) <= number(evidence["captured_monotonic_s"]) <= steady_now:
            raise ContractError("LEARNED_HARDWARE_STALE")
        for stamp in (hw["received_steady_s"], wire["sample_steady_s"], evidence["captured_monotonic_s"]):
            age = steady_now - number(stamp)
            if not 0 <= age <= max_age_s:
                raise ContractError("LEARNED_HARDWARE_STALE")
        if not 0 <= now - wire["sample_system_s"] <= max_age_s:
            raise ContractError("LEARNED_HARDWARE_STALE")
        source = calendar_s(wire) + mapping["calendar_to_system_offset_s"]
        if source + uncertainty > now or now - (source - uncertainty) > max_age_s:
            raise ContractError("LEARNED_HARDWARE_STALE")
        generation = wire["generation"]
        if generation == 0:
            if completion or wire["completed_generation"] or wire["completion_reason"]:
                raise ContractError("LEARNED_HARDWARE_COMPLETION")
        else:
            if wire["completed_generation"] != generation or wire["completion_reason"] not in (1, 2):
                raise ContractError("LEARNED_HARDWARE_COMPLETION")
            finished = calendar_s(wire, "completion_") + mapping["calendar_to_system_offset_s"]
            if (finished - uncertainty <= wire["command_started_system_s"] or finished > source
                    or (completion and now - (finished - uncertainty) > max_age_s)):
                raise ContractError("LEARNED_HARDWARE_COMPLETION")
        return wire
    except ContractError:
        raise
    except (TypeError, KeyError, ValueError, OverflowError) as exc:
        raise ContractError("LEARNED_HARDWARE_SCHEMA") from exc


def check_transition(start, terminal, *, command):
    """Check association in both the sole executor and the canonical trace reader."""
    try:
        a = start["snapshot"]["gripper_controller"]["hardware_execution"]["wire"]
        b = terminal["snapshot"]["gripper_controller"]["hardware_execution"]["wire"]
        if identity(a) != identity(b):
            raise ContractError("LEARNED_HARDWARE_INCARNATION")
        if b["generation"] != a["generation"] + int(command):
            raise ContractError("LEARNED_HARDWARE_SUPERSEDED")
        if command and b["command_started_system_s"] < start["captured_at_s"]:
            raise ContractError("LEARNED_HARDWARE_COMPLETION")
    except (TypeError, KeyError) as exc:
        raise ContractError("LEARNED_HARDWARE_SCHEMA") from exc
