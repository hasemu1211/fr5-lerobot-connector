"""One bounded release tail, compiled after the learned controller settles.

This is a plan compiler, not an executor or a physical grasp classifier.  The
native transport owns the measured contact evidence; absence is unavailable.
"""
import base64
import copy
import math
import time

from tools.fr5_data_factory import ContractError, canonical_digest, validate_motion_program
from tools.data_factory.readiness import RECORDER_READINESS_CONTRACT

PHASES = ("RECYCLE_APPROACH_PTP", "LOWER_LIN", "GRIPPER_OPEN", "RETREAT_LIN", "SAFE_POSE_PTP")


def closure_plateau(plan, snapshot, now, steady_now, *, endpoint=False):
    """Consume the existing native settled-away proof and calibrated range.

    Hardware completion reason 2 is produced after observed movement and the
    configured stable-feedback dwell. Reuse it; a new timer, a single in-range
    sample, or a policy endpoint cannot manufacture that proof here.
    """
    from tools.data_factory.rollout.gripper_evidence import check_hardware, identity
    try:
        source = plan["learned_source_program"]
        required = source["gripper_requirements"]
        controller = snapshot["gripper_controller"]
        observed = {"snapshot": snapshot, "captured_at_s": now,
                    "captured_monotonic_s": steady_now}
        wire = check_hardware(observed, now, steady_now, source["planning"]["max_joint_state_age_s"])
        feedback, reference = controller["feedback_position_m"], controller["reference_position_m"]
        accepted = required["acceptable_feedback_m"]
        # Same reference tolerance/range as PickupExecutor's existing qualified
        # gripper feedback consumer. No new physical contact threshold.
        reason = wire["completion_reason"]
        if (controller["ready"] is not True or reason not in ((1, 2) if endpoint else (2,))
                or wire["generation"] <= 0
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in (feedback, reference))
                or abs(reference-required["command_position_m"]) > 1e-9
                or abs(wire["raw_reference_m"]-reference) > 1e-9
                or abs(wire["feedback_m"]-feedback) > 1e-9
                or reason == 2 and feedback <= reference
                or not accepted["min"] <= feedback <= accepted["max"]):
            raise ContractError("MECHANICAL_CLOSURE_PLATEAU_UNAVAILABLE")
        return {"status": "CALIBRATED_CLOSURE_PLATEAU" if reason == 2 else "CALIBRATED_ENDPOINT_COMPLETION",
                "source": "NATIVE_SETTLED_AWAY_COMPLETION" if reason == 2 else "NATIVE_MOTION_DONE_COMPLETION",
                "generation": wire["generation"], "incarnation": identity(wire),
                "command_position_m": reference, "feedback_position_m": feedback,
                "acceptable_feedback_m": copy.deepcopy(accepted),
                "gripper_evidence_digest": required["evidence_digest"],
                "grasp_profile_digest": source["binding_digests"]["grasp_profile"],
                "object_profile_digest": source["binding_digests"]["object_profile"],
                "snapshot_digest": canonical_digest(snapshot), "physical_success": False}
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("MECHANICAL_CLOSURE_PLATEAU_UNAVAILABLE") from exc


def check_illumination(sample, parent_plan, now):
    """Check source age at the boundary/send, never extend a source timestamp."""
    if (not isinstance(sample, dict) or sample.get("kind") != "CURRENT_REQUIRED_ILLUMINATION"
            or type(sample.get("brightness_mean")) not in (int, float)
            or not RECORDER_READINESS_CONTRACT["min_scene_brightness"] <= sample["brightness_mean"] <= 255):
        raise ContractError("MECHANICAL_ILLUMINATION_UNAVAILABLE")
    stamp, expiry = sample.get("source_timestamp_s"), sample.get("valid_until_s")
    age = parent_plan["learned_proposal"]["max_observation_age_s"]
    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (stamp, expiry, now, age))
            or not 0 < age <= 5 or expiry != stamp + age or not stamp <= now < expiry):
        raise ContractError("MECHANICAL_ILLUMINATION_STALE")


def validate_terminal_evidence(value, parent_plan):
    try:
        terminal=value["plan"]
        if (terminal["terminal_digest"] != canonical_digest({k:v for k,v in terminal.items() if k!="terminal_digest"})
                or terminal["parent_plan_digest"] != canonical_digest(parent_plan)
                or terminal["source_program_digest"] != canonical_digest(parent_plan["learned_source_program"])
                or terminal["control_source"] != "QUALIFIED_MECHANICAL_RELEASE"
                or terminal["recording_scope"] != "OUT_OF_DATASET"
                or terminal["semantic_success"] != "NOT_MEASURED"
                or tuple(s["phase"] for s in terminal["steps"]) != PHASES
                or value["status"] not in {"PLANNED","EXECUTING","COMPLETED","FAILED"}
                or value["terminal_phases"] != list(PHASES[:len(value["terminal_phases"])])
                or value["status"] == "COMPLETED" and value["terminal_phases"] != list(PHASES)):
            raise ContractError("MECHANICAL_TERMINAL_BINDING")
    except (KeyError,TypeError,ValueError) as exc:
        raise ContractError("MECHANICAL_TERMINAL_BINDING") from exc
    return copy.deepcopy(value)


def compile_terminal(transport, *, plan, grant, snapshot, contact, remaining_s):
    source = validate_motion_program(copy.deepcopy(plan["learned_source_program"]))
    if (not isinstance(contact, dict) or contact.get("status") != "AVAILABLE"
            or contact.get("source_program_digest") != canonical_digest(source)
            or contact.get("snapshot_digest") != canonical_digest(snapshot)
            or contact.get("scene_binding") != plan["scene_binding"]
            or contact.get("qualification_source_digest") != source["binding_digests"]["motion_qualification"]
            or contact.get("semantics") != "MODEL_BASED_EXPECTATION"
            or contact.get("physical_success") is not False):
        raise ContractError("MECHANICAL_CONTACT_UNAVAILABLE")
    illumination = contact.get("illumination")
    check_illumination(illumination, plan, time.time())
    if type(contact.get("valid_until_s")) not in (int,float) or not math.isfinite(contact["valid_until_s"]):
        raise ContractError("MECHANICAL_CONTACT_STALE")
    steps = [copy.deepcopy(step) for step in source["steps"] if step["phase"] in PHASES]
    if tuple(step["phase"] for step in steps) != PHASES:
        raise ContractError("MECHANICAL_TERMINAL_SCOPE")
    opened = next(step for step in steps if step["phase"] == "GRIPPER_OPEN")
    if "release_position_m" not in opened or "release_hold_s" not in opened:
        raise ContractError("MECHANICAL_STAGED_OPEN_REQUIRED")
    # The reserve is original authority, not a new deadline. Include native
    # command result/cancellation margins in each already-selected phase bound.
    duration = sum(step["limits"]["execution_timeout_s"] for step in steps)
    duration += next(step["limits"]["execution_timeout_s"] for step in steps if step["phase"] == "GRIPPER_OPEN")
    if duration > min(remaining_s, grant["terminal_reserve_s"]):
        raise ContractError("MECHANICAL_TERMINAL_BUDGET")
    state = list(snapshot["joint_positions"])
    gripper = snapshot["gripper_controller"]
    if (len(state) != 6 or any(type(v) not in (int, float) or not math.isfinite(v) for v in state)
            or snapshot["arm_controller"]["ready"] is not True or gripper["ready"] is not True):
        raise ContractError("MECHANICAL_TERMINAL_STATE")
    scene_readback=transport.prepare_mechanical_terminal(source, contact)
    compiled = []
    future_scene = None
    for step in steps:
        limits = step["limits"]
        item = {**step, "start_joint_state": state}
        item.pop("pause_after", None)
        item.pop("requires_confirmation", None)
        if step["phase"] == "GRIPPER_OPEN":
            # Preserve the calibrated partial-open hold, then full-open command.
            if "release_position_m" not in step or "release_hold_s" not in step:
                raise ContractError("MECHANICAL_STAGED_OPEN_REQUIRED")
            encoded = transport.build_gripper_goal(step["phase"], step["release_position_m"],
                {**limits, "command_duration_s": step["release_hold_s"]})
            full = transport.build_gripper_goal(step["phase"], step["gripper_position_m"], limits)
            item.update(type="GRIPPER", final_joint_state=state,
                        continuation_trajectory_b64=base64.b64encode(full).decode("ascii"))
            future_scene = transport.mechanical_future_scene(source, contact, state)
        else:
            result = transport.plan_arm(step["phase"], step.get("target"), step.get("joint_positions_rad"),
                                        limits, source["frames"], source["planning"], state,
                                        **({"planning_scene_diff": future_scene} if future_scene is not None else {}))
            if result.get("terminal_status") != "SUCCEEDED" or result.get("moveit_success") is not True:
                raise ContractError("MECHANICAL_TERMINAL_INFEASIBLE")
            encoded = result["serialized_trajectory"]
            state = list(result["final_joint_state"])
            if len(state)!=6 or any(type(v) not in (int,float) or not math.isfinite(v) for v in state):
                raise ContractError("MECHANICAL_TERMINAL_STATE")
            duration_reader=getattr(transport,"arm_trajectory_duration_s",None)
            if callable(duration_reader) and duration_reader(encoded)+2 > limits["execution_timeout_s"]:
                raise ContractError("EXECUTION_TIMEOUT_INSUFFICIENT")
            item.update(type="ARM", final_joint_state=state)
        if not isinstance(encoded, bytes) or not encoded:
            raise ContractError("MECHANICAL_TERMINAL_TRAJECTORY")
        item["trajectory_b64"] = base64.b64encode(encoded).decode("ascii")
        compiled.append(item)
    terminal = {"schema_version": "data_factory.mechanical_terminal.v1", "run_id": plan["run_id"],
        "control_source": "QUALIFIED_MECHANICAL_RELEASE", "parent_plan_digest": canonical_digest(plan),
        "source_program_digest": canonical_digest(source), "grant_digest": grant["grant_digest"],
        "deadline_s": grant["deadline_s"], "initial_snapshot": copy.deepcopy(snapshot),
        "planning": copy.deepcopy(source["planning"]), "planning_scene": copy.deepcopy(source["planning_scene"]),
        "frames": copy.deepcopy(source["frames"]), "gripper_requirements":copy.deepcopy(source["gripper_requirements"]),
        "contact_evidence": copy.deepcopy(contact), "steps": compiled,
        "scene_readback_digest":scene_readback,
        "semantic_success": "NOT_MEASURED", "recording_scope": "OUT_OF_DATASET"}
    terminal["precommit_evidence"]=transport.check_mechanical_terminal(terminal, source)
    terminal["terminal_digest"] = canonical_digest(terminal)
    return terminal
