"""Native finite learned proposals; no command sink, approval or dataset writer.

A proposal is a frozen full-seven-joint trajectory. It is not an online policy
license. PickupExecutor remains the only executor and owns cancellation.
"""
from __future__ import annotations

import copy
import hashlib
import math
import threading
import time
import xml.etree.ElementTree as ET

from tools.fr5_data_factory import ContractError, DIGEST, canonical_digest
from tools.data_factory.learned_action_adapter import _action, _rgb

PROGRAM_SCHEMA = "fr5.learned_motion_program.v1"
PROPOSAL_SCHEMA = "data_factory.finite_learned_proposal.v1"
JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6", "finger_right_joint"]
UNITS = ["rad"] * 6 + ["m"]


def _number(value, code):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ContractError(code)
        return float(value)
    except OverflowError as exc:
        raise ContractError(code) from exc


def check_freshness(proposal, now):
    now = _number(now, "LEARNED_SOURCE_CLOCK")
    stamps = proposal["source_timestamps_s"]
    if any(now < stamp or now - stamp > proposal["max_observation_age_s"] for stamp in stamps.values()):
        raise ContractError("LEARNED_STALE_OBSERVATION")


def _limits(xml):
    try:
        root = ET.fromstring(xml)
        joints = {joint.get("name"): joint for joint in root.findall("joint")}
        result = []
        for name in JOINTS:
            joint = joints[name]
            if joint.get("type") != ("prismatic" if name == JOINTS[-1] else "revolute"):
                raise ValueError("joint type")
            limit = joint.find("limit")
            values = [float(limit.attrib[key]) for key in ("lower", "upper", "velocity")]
            if not all(math.isfinite(v) for v in values) or values[0] >= values[1] or values[2] <= 0:
                raise ValueError("limit")
            result.append(values)
        return result
    except (ET.ParseError, KeyError, AttributeError, TypeError, ValueError) as exc:
        raise ContractError("LEARNED_URDF_LIMITS") from exc


def validate_proposal(value):
    fields = {"schema_version", "checkpoint", "instruction", "observation_digest", "initial_state",
              "source_clock", "source_timestamps_s", "max_observation_age_s", "inference_duration_s",
              "inference_started_at_s", "inference_completed_at_s", "joint_order", "units", "action_semantics", "actions",
              "period_s", "robot_description", "velocity_scaling", "proposal_digest"}
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != PROPOSAL_SCHEMA:
        raise ContractError("LEARNED_PROPOSAL_SCHEMA")
    p = copy.deepcopy(value)
    if p["proposal_digest"] != canonical_digest({k: v for k, v in p.items() if k != "proposal_digest"}):
        raise ContractError("LEARNED_PROPOSAL_DIGEST")
    if (p["joint_order"] != JOINTS or p["units"] != UNITS or p["action_semantics"] != "ABSOLUTE_JOINT_POSITION"
            or p["source_clock"] != "SYSTEM_TIME"):
        raise ContractError("LEARNED_ACTION_CONTRACT")
    checkpoint = p["checkpoint"]
    if (not isinstance(checkpoint, dict) or set(checkpoint) != {"tree_digest", "training_receipt_digest", "runtime"}
            or checkpoint["runtime"] not in {"lerobot-0.6.1-native", "SYNTHETIC_TEST_ONLY"}
            or any(not isinstance(checkpoint[k], str) or not DIGEST.fullmatch(checkpoint[k])
                   for k in ("tree_digest", "training_receipt_digest"))):
        raise ContractError("LEARNED_CHECKPOINT_BINDING")
    if not isinstance(p["instruction"], str) or not p["instruction"].strip():
        raise ContractError("LEARNED_INSTRUCTION")
    if not isinstance(p["observation_digest"], str) or not DIGEST.fullmatch(p["observation_digest"]):
        raise ContractError("LEARNED_OBSERVATION_DIGEST")
    stamps = p["source_timestamps_s"]
    if not isinstance(stamps, dict) or set(stamps) != {"state", "camera1", "camera2"}:
        raise ContractError("LEARNED_SOURCE_CLOCK")
    for stamp in stamps.values():
        _number(stamp, "LEARNED_SOURCE_CLOCK")
    period = _number(p["period_s"], "LEARNED_HORIZON")
    age = _number(p["max_observation_age_s"], "LEARNED_SOURCE_CLOCK")
    latency = _number(p["inference_duration_s"], "LEARNED_INFERENCE_TIMEOUT")
    scaling = _number(p["velocity_scaling"], "LEARNED_LIMITS")
    actions = p["actions"]
    if (not isinstance(actions, list) or not 1 <= len(actions) <= 50
            or period < 1 / 30 or period * len(actions) > 5 or age <= 0
            or latency < 0 or latency > age or not 0 < scaling <= .1):
        raise ContractError("LEARNED_HORIZON")
    started = _number(p["inference_started_at_s"], "LEARNED_SOURCE_CLOCK")
    completed = _number(p["inference_completed_at_s"], "LEARNED_SOURCE_CLOCK")
    if completed < started:
        raise ContractError("LEARNED_SOURCE_CLOCK")
    check_freshness(p, completed)
    try:
        rows = [_action(p["initial_state"]), *[_action(row) for row in actions]]
    except ValueError as exc:
        raise ContractError("LEARNED_ACTION_7D") from exc
    limits = _limits(p["robot_description"])
    for row in rows:
        if any(not low <= v <= high for v, (low, high, _) in zip(row, limits)):
            raise ContractError("LEARNED_JOINT_LIMIT")
    for previous, row in zip(rows, rows[1:]):
        if any(abs(v - old) / period > limit[2] * scaling + 1e-9
               for old, v, limit in zip(previous, row, limits)):
            raise ContractError("LEARNED_VELOCITY_LIMIT")
    return p


def _materialize(source, proposal):
    # Existing source qualification is retained verbatim as context, not relabeled
    # as evidence of learned effectiveness or physical qualification.
    arm = next(step for step in source["steps"] if step["phase"] == "SAFE_POSE_PTP")
    learned = {"phase": "LEARNED_CHUNK", "limits": copy.deepcopy(arm["limits"]),
               "requires_confirmation": "PRECONTACT_HUMAN", "pause_after": "SEMANTIC_VERDICT"}
    return {**copy.deepcopy(source), "schema_version": PROGRAM_SCHEMA,
            "source_program": copy.deepcopy(source), "learned_proposal": copy.deepcopy(proposal),
            "steps": [learned]}


def validate_learned_program(value):
    from tools.fr5_data_factory import validate_motion_program
    if not isinstance(value, dict) or value.get("schema_version") != PROGRAM_SCHEMA:
        raise ContractError("LEARNED_PROGRAM_SCHEMA")
    source = value.get("source_program")
    if not isinstance(source, dict) or source.get("schema_version") != "fr5.motion_program.v2":
        raise ContractError("LEARNED_SOURCE_PROGRAM")
    source = validate_motion_program(copy.deepcopy(source))
    p = validate_proposal(value.get("learned_proposal"))
    description_digest = "sha256:" + hashlib.sha256(p["robot_description"].encode()).hexdigest()
    if description_digest != source["binding_digests"]["robot_description_digest"]:
        raise ContractError("LEARNED_ROBOT_BINDING")
    if p["velocity_scaling"] > min(step["limits"]["velocity_scaling"] for step in source["steps"] if "velocity_scaling" in step["limits"]):
        raise ContractError("LEARNED_LIMITS")
    expected = _materialize(source, p)
    if value != expected:
        raise ContractError("LEARNED_PROGRAM_BINDING")
    return expected


def compile_program(source, proposal):
    return validate_learned_program(_materialize(source, proposal))


class FinitePolicyInference:
    """One invocation per instance; cancellation fences late CPU/GPU results.

    The caller owns model runtime qualification. This object never owns goals.
    """
    def __init__(self, policy, checkpoint, *, source_clock=time.time, monotonic_clock=time.monotonic, cancel_event=None):
        self.policy = policy
        self.checkpoint = copy.deepcopy(checkpoint)
        self.source_clock = source_clock
        self.monotonic_clock = monotonic_clock
        self._lock = threading.Lock()
        self._cancel = cancel_event if cancel_event is not None else threading.Event()
        self._used = False

    def cancel(self):
        self._cancel.set()

    def propose(self, observation, *, instruction, robot_description, period_s, max_observation_age_s=.3, velocity_scaling=.1):
        if not self._lock.acquire(blocking=False):
            self.cancel()
            raise ContractError("LEARNED_REENTRANT_INFERENCE")
        try:
            if self._cancel.is_set():
                raise ContractError("LEARNED_CANCELLED")
            if self._used:
                raise ContractError("LEARNED_INFERENCE_ALREADY_USED")
            self._used = True
            if (not isinstance(instruction, str) or not instruction.strip()
                    or not 1 / 30 <= _number(period_s, "LEARNED_HORIZON") <= 5
                    or not 0 < _number(velocity_scaling, "LEARNED_LIMITS") <= .1):
                raise ContractError("LEARNED_INFERENCE_CONFIG")
            _limits(robot_description)
            fields = {"source_clock", "source_timestamps_s", "observation.state",
                      "observation.images.camera1", "observation.images.camera2"}
            if not isinstance(observation, dict) or set(observation) != fields or observation["source_clock"] != "SYSTEM_TIME":
                raise ContractError("LEARNED_OBSERVATION_SCHEMA")
            try:
                initial = list(_action(observation["observation.state"]))
                cameras = {key: _rgb(observation[key]) for key in fields if key.startswith("observation.images.")}
                stamps = copy.deepcopy(observation["source_timestamps_s"])
                if set(stamps) != {"state", "camera1", "camera2"}:
                    raise ValueError("timestamps")
                for stamp in stamps.values():
                    _number(stamp, "LEARNED_SOURCE_CLOCK")
                age = _number(max_observation_age_s, "LEARNED_SOURCE_CLOCK")
                if age <= 0:
                    raise ValueError("age")
            except (ValueError, TypeError) as exc:
                raise ContractError("LEARNED_OBSERVATION_SCHEMA") from exc
            input_value = {"observation.state": initial, **cameras, "task": instruction}
            digest_input = copy.deepcopy(input_value)
            for key in cameras:
                digest_input[key]["data"] = cameras[key]["data"].hex()
            p = {"schema_version": PROPOSAL_SCHEMA, "checkpoint": self.checkpoint,
                 "instruction": instruction, "observation_digest": canonical_digest(digest_input),
                 "initial_state": initial, "source_clock": "SYSTEM_TIME", "source_timestamps_s": stamps,
                 "max_observation_age_s": age, "joint_order": JOINTS, "units": UNITS,
                 "action_semantics": "ABSOLUTE_JOINT_POSITION", "period_s": period_s,
                 "robot_description": robot_description, "velocity_scaling": velocity_scaling}
            p["inference_started_at_s"] = self.source_clock()
            check_freshness(p, p["inference_started_at_s"])
            started = _number(self.monotonic_clock(), "LEARNED_SOURCE_CLOCK")
            try:
                actions = self.policy(copy.deepcopy(input_value))
            except Exception as exc:
                raise ContractError("LEARNED_POLICY_FAILED") from exc
            if self._cancel.is_set():
                raise ContractError("LEARNED_CANCELLED")
            p.update(actions=actions, inference_duration_s=self.monotonic_clock() - started,
                     inference_completed_at_s=self.source_clock())
            check_freshness(p, p["inference_completed_at_s"])
            p["proposal_digest"] = canonical_digest(p)
            return validate_proposal(p)
        finally:
            self._lock.release()


def validate_execution_trace(plan, trace):
    """Validate the sole executor's trace against its frozen plan, not task success."""
    p = validate_proposal(plan.get("learned_proposal"))
    fields = {"schema_version", "proposal_digest", "plan_digest", "checkpoint", "status",
              "failure_code", "terminal_state", "terminal_phases", "task_effectiveness",
              "scene_outcome", "cell_ready", "online_policy_authorized", "trace_digest"}
    if not isinstance(trace, dict) or set(trace) != fields:
        raise ContractError("LEARNED_TRACE_SCHEMA")
    if (trace["schema_version"] != "data_factory.finite_learned_execution.v1"
            or trace["proposal_digest"] != p["proposal_digest"] or trace["checkpoint"] != p["checkpoint"]
            or trace["plan_digest"] != canonical_digest(plan)
            or trace["trace_digest"] != canonical_digest({k: v for k, v in trace.items() if k != "trace_digest"})
            or trace["status"] not in {"PENDING", "COMPLETED", "FAILED"}
            or trace["task_effectiveness"] != "UNKNOWN" or trace["scene_outcome"] != "UNKNOWN"
            or trace["cell_ready"] is not False or trace["online_policy_authorized"] is not False):
        raise ContractError("LEARNED_TRACE_BINDING")
    if trace["status"] == "FAILED":
        if not isinstance(trace["failure_code"], str) or not trace["failure_code"]:
            raise ContractError("LEARNED_TRACE_FAILURE")
    elif trace["failure_code"] is not None:
        raise ContractError("LEARNED_TRACE_FAILURE")
    if trace["terminal_phases"] not in ([], ["LEARNED_CHUNK"]):
        raise ContractError("LEARNED_TRACE_TERMINAL")
    if trace["terminal_state"] is not None:
        try:
            _action(trace["terminal_state"])
        except ValueError as exc:
            raise ContractError("LEARNED_TRACE_TERMINAL") from exc
    if trace["status"] == "COMPLETED" and (trace["terminal_state"] is None or trace["terminal_phases"] != ["LEARNED_CHUNK"]):
        raise ContractError("LEARNED_TRACE_TERMINAL")
    return copy.deepcopy(trace)


def proposal_summary(p):
    return {"proposal_digest": p["proposal_digest"], "instruction": p["instruction"],
            "checkpoint_tree_digest": p["checkpoint"]["tree_digest"],
            "actions": len(p["actions"]), "duration_s": len(p["actions"]) * p["period_s"],
            "units": p["units"], "task_effectiveness": "UNKNOWN", "scene_outcome": "UNKNOWN",
            "automatic_recovery": False, "online_policy_authorized": False}
