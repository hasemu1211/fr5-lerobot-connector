"""Native finite learned proposals; no command sink, approval or dataset writer.

A proposal is a frozen full-seven-joint trajectory. It is not an online policy
license. PickupExecutor remains the only executor and owns cancellation.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import math
import threading
import time
import xml.etree.ElementTree as ET

from tools.fr5_data_factory import ContractError, DIGEST, canonical_digest
from tools.data_factory.learned_action_adapter import _action, _rgb

PROGRAM_SCHEMA = "fr5.learned_motion_program.v1"
PROPOSAL_SCHEMA = "data_factory.finite_learned_proposal.v1"
HELD_PROPOSAL_SCHEMA = "data_factory.finite_learned_held_target_proposal.v1"
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
    if isinstance(value, dict) and "runtime_inputs" in value:
        fields.add("runtime_inputs")
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] not in {PROPOSAL_SCHEMA, HELD_PROPOSAL_SCHEMA}:
        raise ContractError("LEARNED_PROPOSAL_SCHEMA")
    p = copy.deepcopy(value)
    if p["proposal_digest"] != canonical_digest({k: v for k, v in p.items() if k != "proposal_digest"}):
        raise ContractError("LEARNED_PROPOSAL_DIGEST")
    if "runtime_inputs" in p:
        inputs = p["runtime_inputs"]
        if (not isinstance(inputs, dict) or set(inputs) - {"warmup"} != {"checkpoint", "device", "gripper_source_clock", "clock_binding", "camera_topics", "camera_mapping", "fps"}
                or any(not isinstance(inputs[k], str) or not inputs[k] for k in ("checkpoint", "gripper_source_clock"))
                or not isinstance(inputs["device"], str) or inputs["device"] not in {"cpu", "cuda"}
                or not isinstance(inputs["camera_topics"], dict) or set(inputs["camera_topics"]) != {"camera1", "camera2"}
                or any(not isinstance(t, str) or not t.startswith("/") for t in inputs["camera_topics"].values())
                or not isinstance(inputs["camera_mapping"], dict) or len(inputs["camera_mapping"]) != 2
                or any(not isinstance(k, str) or not k.startswith("observation.images.") or not isinstance(v, str)
                       for k, v in inputs["camera_mapping"].items())
                or set(inputs["camera_mapping"].values()) != {"observation.images.camera1", "observation.images.camera2"}
                or abs(_number(inputs["fps"], "LEARNED_HORIZON") * _number(p["period_s"], "LEARNED_HORIZON") - 1) > 1e-9):
            raise ContractError("LEARNED_RUNTIME_INPUTS")
        if "warmup" in inputs:
            warmup = inputs["warmup"]
            if (not isinstance(warmup, dict) or set(warmup) != {"input_kind", "image_shape", "instruction_digest", "device", "model_calls", "output_disposition", "rng_state_restored", "duration_s", "inference_duration_s"}
                    or warmup["input_kind"] != "SYNTHETIC_ZERO_RGB_STATE"
                    or not isinstance(warmup["image_shape"], list) or len(warmup["image_shape"]) != 3
                    or any(type(n) is not int or n < 1 for n in warmup["image_shape"]) or warmup["image_shape"][-1] != 3
                    or warmup["instruction_digest"] != canonical_digest(p["instruction"])
                    or warmup["device"] != inputs["device"] or type(warmup["model_calls"]) is not int or warmup["model_calls"] != 1
                    or warmup["output_disposition"] != "DISCARDED" or warmup["rng_state_restored"] is not True
                    or not 0 <= _number(warmup["inference_duration_s"], "LEARNED_WARMUP_INPUT") <= _number(warmup["duration_s"], "LEARNED_WARMUP_INPUT")):
                raise ContractError("LEARNED_WARMUP_INPUT")
        from .gripper_evidence import validate_clock_binding
        validate_clock_binding(inputs["clock_binding"])
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
    # A held gripper reference is governed by the bound hardware settings and
    # completion contract, not a claim about finger speed between samples.
    checked = 6 if p["schema_version"] == HELD_PROPOSAL_SCHEMA else 7
    for previous, row in zip(rows, rows[1:]):
        if any(abs(v - old) / period > limit[2] * scaling + 1e-9
               for old, v, limit in zip(previous[:checked], row[:checked], limits[:checked])):
            raise ContractError("LEARNED_VELOCITY_LIMIT")
    return p


def held_target_segments(source, proposal):
    """Freeze exact runs of bound references; never classify or round model output."""
    p = validate_proposal(proposal)
    if p["schema_version"] != HELD_PROPOSAL_SCHEMA:
        raise ContractError("LEARNED_HELD_TARGET_SCHEMA")
    close = next(s for s in source["steps"] if s["phase"] == "GRIPPER_CLOSE")
    opened = next(s for s in source["steps"] if s["phase"] == "GRIPPER_OPEN")
    if "release_position_m" in opened or close["gripper_position_m"] == opened["gripper_position_m"]:
        raise ContractError("LEARNED_HELD_PROFILE_UNSUPPORTED")
    required = source["gripper_requirements"]
    profiles = {
        close["gripper_position_m"]: (close["limits"], required["acceptable_feedback_m"]),
        opened["gripper_position_m"]: (opened["limits"], {
            "min": opened["gripper_position_m"] - opened["limits"]["completion_tolerance_m"],
            "max": opened["gripper_position_m"] + opened["limits"]["completion_tolerance_m"],
        }),
    }
    segments, previous_target = [], p["initial_state"][-1]
    low, high, _ = _limits(p["robot_description"])[-1]
    duration = len(p["actions"]) * p["period_s"]
    for target, group in itertools.groupby(enumerate(p["actions"]), key=lambda item: item[1][-1]):
        indices = [index for index, _ in group]
        # Same numerical reference tolerance as the existing executor completion
        # check, allowing float32 representation without changing the action.
        matches = [bound for bound in profiles if abs(target - bound) <= 1e-9]
        if len(matches) != 1:
            raise ContractError("LEARNED_UNBOUND_GRIPPER_TARGET")
        limits, feedback = profiles[matches[0]]
        feedback = {"min": max(low, feedback["min"]), "max": min(high, feedback["max"])}
        bound = {"gripper_position_m": target, "acceptable_feedback_m": copy.deepcopy(feedback),
                 "gripper_limits": copy.deepcopy(limits)}
        if target != previous_target:
            segments.append({"type": "GRIPPER", "action_range": [indices[0], indices[0]], **bound})
            duration += limits["command_duration_s"]
        segments.append({"type": "ARM", "action_range": [indices[0], indices[-1] + 1], **bound})
        previous_target = target
    if duration > 5:
        raise ContractError("LEARNED_HELD_HORIZON")
    return segments


def _execution_state(step, evidence, now):
    try:
        observed = evidence["snapshot"]
        elapsed = _number(now, "LEARNED_SOURCE_CLOCK") - _number(evidence["captured_at_s"], "LEARNED_SOURCE_CLOCK")
        ages = [_number(age, "LEARNED_STALE_STATE") for age in
                (observed["joint_state_age_s"], observed["arm_controller"]["age_s"], observed["gripper_controller"]["age_s"])]
        if elapsed < 0 or any(age < 0 or age + elapsed > step["max_joint_state_age_s"] for age in ages):
            raise ContractError("LEARNED_STALE_STATE")
        if any(observed[key]["ready"] is not True for key in ("arm_controller", "gripper_controller")):
            raise ContractError("CONTROLLER_NOT_READY")
        # A successful action/reference match cannot authorize an arm handoff
        # while a controller reports paused time. Positive scaling alone is
        # still not same-command hardware completion or physical acknowledgement.
        for key in ("arm_controller", "gripper_controller"):
            if _number(observed[key]["speed_scaling"], "LEARNED_STATE_SCHEMA") <= 0:
                raise ContractError("LEARNED_CONTROLLER_PAUSED")
            from tools.data_factory.motion.moveit_transport import validate_controller_sample
            validate_controller_sample(observed[key])
        gripper = observed["gripper_controller"]
        state = list(_action([*observed["joint_positions"], gripper["feedback_position_m"]]))
        limits = _limits(step["learned_proposal"]["robot_description"])
        if any(not low <= value <= high for value, (low, high, _) in zip(state, limits)):
            raise ContractError("LEARNED_JOINT_LIMIT")
        return state
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("LEARNED_STATE_SCHEMA") from exc


def check_execution_start(step, evidence, now, *, steady_now):
    """Check current execution evidence, never renew frozen inference inputs."""
    try:
        if "initial_hardware_binding" in step and step["initial_hardware_binding"] is None:
            raise ContractError("LEARNED_HARDWARE_UNBOUND")
        state = _execution_state(step, evidence, now)
        observed = evidence["snapshot"]
        stamps = [observed["joint_state_stamp_ns"],
                  *[observed[k]["sample"]["ros_stamp_ns"] for k in ("arm_controller", "gripper_controller")]]
        for stamp in stamps:
            if type(stamp) is not int or not 0 <= now - stamp / 1e9 <= step["max_joint_state_age_s"]:
                raise ContractError("LEARNED_STALE_STATE")
        if "action_range" in step:
            check_segment_observation(step, evidence, now, steady_now=steady_now)
        else:
            if any(abs(a - b) > step["joint_tolerance_rad"] for a, b in zip(state[:6], step["start_joint_state"])):
                raise ContractError("START_STATE_MISMATCH")
            initial = step["learned_proposal"]["initial_state"][-1]
            if any(abs(observed["gripper_controller"][k] - initial) > step["gripper_tolerance_m"]
                   for k in ("reference_position_m", "feedback_position_m")):
                raise ContractError("LEARNED_START_STATE")
        from .gripper_evidence import check_hardware, identity, integer
        wire = check_hardware(evidence, now, steady_now, step["max_joint_state_age_s"])
        inputs = step["learned_proposal"].get("runtime_inputs")
        if inputs is not None and observed["gripper_controller"]["hardware_execution"]["clock_binding"] != inputs["clock_binding"]:
            raise ContractError("LEARNED_HARDWARE_CLOCK_BINDING")
        if "initial_hardware_binding" in step:
            binding = step["initial_hardware_binding"]
            if (not isinstance(binding, dict) or set(binding) != {"incarnation", "generation"}
                    or not isinstance(binding["incarnation"], list) or len(binding["incarnation"]) != 4):
                raise ContractError("LEARNED_HARDWARE_SCHEMA")
            for part in binding["incarnation"]:
                integer(part, 2**32 - 1)
            integer(binding["generation"], 2**53 - 1)
            if identity(wire) != binding["incarnation"]:
                raise ContractError("LEARNED_HARDWARE_INCARNATION")
            if wire["generation"] != binding["generation"]:
                raise ContractError("LEARNED_HARDWARE_SUPERSEDED")
        return state
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("LEARNED_STATE_SCHEMA") from exc


def check_segment_observation(segment, evidence, now, *, terminal=False, steady_now=None, allow_pending=False):
    """Admit a fresh observation against frozen targets; never rewrite a knot."""
    try:
        observed = evidence["snapshot"]
        state = _execution_state(segment, evidence, now)
        gripper = observed["gripper_controller"]
        expected = segment["final_joint_state"] if terminal else segment["start_joint_state"]
        if any(abs(a - b) > segment["joint_tolerance_rad"] for a, b in zip(state[:6], expected)):
            raise ContractError("LEARNED_TERMINAL_STATE" if terminal else "START_STATE_MISMATCH")
        if terminal or segment["type"] == "ARM":
            reference = _number(gripper["reference_position_m"], "GRIPPER_FEEDBACK_OUT_OF_RANGE")
            bound = segment["acceptable_feedback_m"]
            if abs(reference - segment["gripper_position_m"]) > 1e-9 or not bound["min"] <= state[-1] <= bound["max"]:
                raise ContractError("GRIPPER_FEEDBACK_OUT_OF_RANGE")
        from .gripper_evidence import check_hardware
        wire = check_hardware(evidence, now,
            evidence["captured_monotonic_s"] if steady_now is None else steady_now,
            segment["max_joint_state_age_s"], completion=terminal and segment["type"] == "GRIPPER",
            allow_pending=allow_pending and segment["type"] == "GRIPPER" and not terminal)
        if terminal or segment["type"] == "ARM":
            if (abs(wire["raw_reference_m"] - segment["gripper_position_m"]) > 1e-9
                    or not bound["min"] <= wire["feedback_m"] <= bound["max"]):
                raise ContractError("GRIPPER_FEEDBACK_OUT_OF_RANGE")
        return state
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("LEARNED_STATE_SCHEMA") from exc


def _materialize(source, proposal, boundary="LEARNED_CHUNK_COMPLETE"):
    # Existing source qualification is retained verbatim as context, not relabeled
    # as evidence of learned effectiveness or physical qualification.
    arm = next(step for step in source["steps"] if step["phase"] == "SAFE_POSE_PTP")
    learned = {"phase": "LEARNED_CHUNK", "limits": copy.deepcopy(arm["limits"]),
               "requires_confirmation": "PRECONTACT_HUMAN", "pause_after": boundary}
    if proposal["schema_version"] == HELD_PROPOSAL_SCHEMA:
        learned["held_target_segments"] = held_target_segments(source, proposal)
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
    # Preserve old frozen programs verbatim; new programs distinguish a chunk
    # boundary from the task verdict and recorder freeze.
    steps = value.get("steps")
    boundary = steps[0].get("pause_after") if isinstance(steps, list) and len(steps) == 1 and isinstance(steps[0], dict) else None
    if boundary not in {"SEMANTIC_VERDICT", "LEARNED_CHUNK_COMPLETE"}:
        raise ContractError("LEARNED_PROGRAM_BINDING")
    expected = _materialize(source, p, boundary)
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

    def propose(self, observation, *, instruction, robot_description, period_s, max_observation_age_s=.3, velocity_scaling=.1, held_gripper_targets=False, runtime_inputs=None):
        if not self._lock.acquire(blocking=False):
            self.cancel()
            raise ContractError("LEARNED_REENTRANT_INFERENCE")
        try:
            if self._cancel.is_set():
                raise ContractError("LEARNED_CANCELLED")
            if self._used:
                raise ContractError("LEARNED_INFERENCE_ALREADY_USED")
            self._used = True
            if (type(held_gripper_targets) is not bool or not isinstance(instruction, str) or not instruction.strip()
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
            p = {"schema_version": HELD_PROPOSAL_SCHEMA if held_gripper_targets else PROPOSAL_SCHEMA, "checkpoint": self.checkpoint,
                 "instruction": instruction, "observation_digest": canonical_digest(digest_input),
                 "initial_state": initial, "source_clock": "SYSTEM_TIME", "source_timestamps_s": stamps,
                 "max_observation_age_s": age, "joint_order": JOINTS, "units": UNITS,
                 "action_semantics": "ABSOLUTE_JOINT_POSITION", "period_s": period_s,
                 "robot_description": robot_description, "velocity_scaling": velocity_scaling}
            p["inference_started_at_s"] = self.source_clock()
            if runtime_inputs is not None:
                p["runtime_inputs"] = copy.deepcopy(runtime_inputs)
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
    held = p["schema_version"] == HELD_PROPOSAL_SCHEMA
    current_start = "initial_hardware_binding" in plan["steps"][0]
    if isinstance(trace, dict) and "terminal_observation" in trace:
        fields.add("terminal_observation")
    if held:
        fields.add("segments")
    elif current_start:
        fields.add("start_observation")
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
    if current_start and not held:
        start = trace["start_observation"]
        if start is not None:
            check_execution_start(plan["steps"][0], start, start["captured_at_s"], steady_now=start["captured_monotonic_s"])
        elif trace["status"] == "COMPLETED" or trace["terminal_phases"]:
            raise ContractError("LEARNED_TRACE_TERMINAL")
    if "terminal_observation" in trace:
        terminal = trace["terminal_observation"]
        if not isinstance(terminal, dict) or set(terminal) != {"captured_at_s", "captured_monotonic_s", "snapshot"}:
            raise ContractError("LEARNED_TRACE_TERMINAL")
        if held or not trace["terminal_phases"] or trace["terminal_state"] != _execution_state(plan["steps"][0], terminal, terminal["captured_at_s"]):
            raise ContractError("LEARNED_TRACE_TERMINAL")
        from .gripper_evidence import check_hardware, identity
        wire = check_hardware(terminal, terminal["captured_at_s"], terminal["captured_monotonic_s"], plan["steps"][0]["max_joint_state_age_s"])
        if identity(wire) != plan["steps"][0]["initial_hardware_binding"]["incarnation"]:
            raise ContractError("LEARNED_HARDWARE_INCARNATION")
    if held:
        segments = plan["steps"][0]["held_target_segments"]
        evidence = trace["segments"]
        if (not isinstance(evidence, list) or len(evidence) > len(segments)
                or (trace["status"] == "COMPLETED" and len(evidence) != len(segments))):
            raise ContractError("LEARNED_TRACE_TERMINAL")
        from .gripper_evidence import check_transition
        previous_terminal = -math.inf
        for index, item in enumerate(evidence):
            if (not isinstance(item, dict) or set(item) != {"segment_index", "segment_digest", "start_observation", "terminal_observation"}
                    or item["segment_index"] != index or item["segment_digest"] != canonical_digest(segments[index])):
                raise ContractError("LEARNED_TRACE_BINDING")
            try:
                started = _number(item["start_observation"]["captured_at_s"], "LEARNED_TRACE_TERMINAL")
                completed = _number(item["terminal_observation"]["captured_at_s"], "LEARNED_TRACE_TERMINAL")
            except (KeyError, TypeError) as exc:
                raise ContractError("LEARNED_TRACE_TERMINAL") from exc
            if not previous_terminal <= started <= completed:
                raise ContractError("LEARNED_TRACE_TERMINAL")
            previous_terminal = completed
            action = item["terminal_observation"].get("action_terminal")
            if action is not None:
                # Older traces omit this optional observation. New held waits retain
                # actual JTC terminal time separately from native handoff completion.
                try:
                    if (set(action) != {"result_status", "error_code", "observed_at_s", "observed_monotonic_s"}
                            or type(action["result_status"]) is not int or action["result_status"] != 4
                            or type(action["error_code"]) is not int or action["error_code"] != 0
                            or segments[index]["type"] != "GRIPPER"
                            or not started <= _number(action["observed_at_s"], "LEARNED_TRACE_TERMINAL") <= completed
                            or not item["start_observation"]["captured_monotonic_s"] <= _number(
                                action["observed_monotonic_s"], "LEARNED_TRACE_TERMINAL") <= item["terminal_observation"]["captured_monotonic_s"]):
                        raise ContractError("LEARNED_TRACE_TERMINAL")
                except (TypeError, KeyError) as exc:
                    raise ContractError("LEARNED_TRACE_TERMINAL") from exc
            check_transition(item["start_observation"], item["terminal_observation"], command=segments[index]["type"] == "GRIPPER")
            if index:
                check_transition(evidence[index - 1]["terminal_observation"], item["start_observation"], command=False)
            for key, terminal in (("start_observation", False), ("terminal_observation", True)):
                check_segment_observation(segments[index], item[key], item[key]["captured_at_s"], terminal=terminal)
                if current_start and not terminal:
                    check_execution_start(segments[index], item[key], item[key]["captured_at_s"],
                                          steady_now=item[key]["captured_monotonic_s"])
        if evidence:
            last = evidence[-1]["terminal_observation"]
            if trace["terminal_state"] != check_segment_observation(segments[len(evidence) - 1], last, last["captured_at_s"], terminal=True):
                raise ContractError("LEARNED_TRACE_TERMINAL")
    return copy.deepcopy(trace)


def validate_execution_history(plan, history):
    """Validate retained exact chunks in the existing execution evidence owner."""
    if not isinstance(history, list):
        raise ContractError("LEARNED_HISTORY_BINDING")
    previous_plan, previous_trace, previous_chunk = None, None, None
    seen_plans, approvals = set(), set()
    for index, item in enumerate(history):
        try:
            if set(item) != {"plan_envelope", "approval", "state", "execution_evidence"} or item["state"] != "LEARNED_CHUNK_COMPLETE":
                raise ContractError("LEARNED_HISTORY_BINDING")
            old = item["plan_envelope"]["plan"]
            digest = canonical_digest(old)
            approval = item["approval"]
            if (digest in seen_plans or approval["approval_id"] in approvals
                    or approval["approval_scope"] != "HUMAN_GATED" or approval["plan_digest"] != digest
                    or approval["run_id"] != plan["run_id"] or old["run_id"] != plan["run_id"]
                    or approval["resolved_job_digest"] != old["resolved_job_digest"]
                    or item["plan_envelope"]["precommit_safety"]["approved_plan_digest"] != digest
                    or old["learned_source_program"] != plan["learned_source_program"]
                    or old["learned_proposal"]["checkpoint"] != plan["learned_proposal"]["checkpoint"]):
                raise ContractError("LEARNED_HISTORY_BINDING")
            binding = old.get("learned_continuation")
            expected = {"previous_plan_digest": previous_plan, "previous_trace_digest": previous_trace,
                        "previous_chunk_digest": previous_chunk, "chunk_index": index}
            if index == 0 and binding is not None or index and canonical_digest(binding) != canonical_digest(expected):
                raise ContractError("LEARNED_HISTORY_BINDING")
            trace = validate_execution_trace(old, item["execution_evidence"]["learned_execution"])
            if trace["failure_code"] is not None or trace["terminal_phases"] != ["LEARNED_CHUNK"]:
                raise ContractError("LEARNED_HISTORY_BINDING")
            seen_plans.add(digest)
            approvals.add(approval["approval_id"])
            previous_plan, previous_trace, previous_chunk = digest, trace["trace_digest"], canonical_digest(item)
        except (KeyError, TypeError) as exc:
            raise ContractError("LEARNED_HISTORY_BINDING") from exc
    expected = {"previous_plan_digest": previous_plan, "previous_trace_digest": previous_trace,
                "previous_chunk_digest": previous_chunk, "chunk_index": len(history)}
    if (history and (canonical_digest(plan.get("learned_continuation")) != canonical_digest(expected) or canonical_digest(plan) in seen_plans)
            or not history and "learned_continuation" in plan):
        raise ContractError("LEARNED_HISTORY_BINDING")
    return copy.deepcopy(history)


def proposal_summary(p):
    return {"proposal_digest": p["proposal_digest"], "instruction": p["instruction"],
            "checkpoint_tree_digest": p["checkpoint"]["tree_digest"],
            "actions": len(p["actions"]), "duration_s": len(p["actions"]) * p["period_s"],
            "units": p["units"], "task_effectiveness": "UNKNOWN", "scene_outcome": "UNKNOWN",
            "automatic_recovery": False, "online_policy_authorized": False}
