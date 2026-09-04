"""Recorder-free transitions to qualified HOME and collection start poses."""
from __future__ import annotations

import base64
import copy
import math
import time
from typing import Any, Mapping

from tools.fr5_data_factory import (
    ContractError,
    DIGEST,
    MOTION_QUALIFICATION_KEYS_BY_SCHEMA,
    SAFE_ID,
    canonical_digest,
    normalize_planning_scene,
)


JOINT_ORDER = ["j1", "j2", "j3", "j4", "j5", "j6"]
POSE_KEYS = frozenset({
    "schema_version", "source", "robot_system_id", "robot_start_pose_id",
    "joint_order", "target_rad", "tolerance_rad", "home_candidate_digest",
    "qualification_status", "safety_status", "qualification_digest",
})


def _canonical(value: object, code: str) -> str:
    try:
        return canonical_digest(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(code, str(exc)) from exc


def _ready_graph(facts: Mapping[str, Any], code: str) -> None:
    for key in ("move_action", "execute_trajectory", "gripper", "joint_states"):
        if not isinstance(facts.get(key), Mapping) or facts[key].get("ready") is not True:
            raise ContractError(code)
    if facts.get("joint_order") != JOINT_ORDER:
        raise ContractError(code)


def _snapshot(value: object, prefix: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{prefix}_SNAPSHOT")
    result = dict(value)
    joints = result.get("joint_positions")
    if (
        not isinstance(joints, list)
        or len(joints) != len(JOINT_ORDER)
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            for item in joints
        )
    ):
        raise ContractError(f"{prefix}_SNAPSHOT")
    for name in ("arm_controller", "gripper_controller"):
        if not isinstance(result.get(name), Mapping) or result[name].get("ready") is not True:
            raise ContractError(f"{prefix}_CONTROLLER")
    return result


def _gripper_delta(snapshot: Mapping[str, Any], open_position: float, prefix: str) -> float:
    try:
        gripper = snapshot["gripper_controller"]
        values = (
            float(gripper["reference_position_m"]),
            float(gripper["feedback_position_m"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"{prefix}_SNAPSHOT", str(exc)) from exc
    if any(not math.isfinite(value) for value in values):
        raise ContractError(f"{prefix}_SNAPSHOT")
    return max(abs(value - open_position) for value in values)


def _raise_if_cancelled(cancel_event, prefix: str) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ContractError(f"{prefix}_CANCELLED")


def _wait_phase(
    transport, *, cancel_timeout_s: float, prefix: str, sleep_call=time.sleep,
    cancel_event=None,
) -> None:
    try:
        while True:
            _raise_if_cancelled(cancel_event, prefix)
            if transport.poll_active() is not None:
                break
            sleep_call(0.01)
    except ContractError as original:
        try:
            transport.cancel_active(cancel_timeout_s)
        except ContractError as exc:
            if exc.code == "ROS_EXEC_NO_ACTIVE":
                raise original
            raise ContractError(f"{prefix}_CANCEL_UNCERTAIN") from exc
        raise


def _validated_motion(value: object, prefix: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{prefix}_QUALIFICATION")
    keys = MOTION_QUALIFICATION_KEYS_BY_SCHEMA.get(value.get("schema_version"))
    if keys is None or set(value) != keys:
        raise ContractError(f"{prefix}_QUALIFICATION")
    motion = copy.deepcopy(dict(value))
    try:
        safe = motion["qualified_safe_joint_positions_rad"]
        scene = normalize_planning_scene(motion["planning_scene"])
        frames = motion["frames"]
        tolerances = motion["goal_tolerances"]
        arm_limits = motion["phase_limits"]["SAFE_POSE_PTP"]
        gripper_limits = motion["phase_limits"]["GRIPPER_OPEN"]
        numbers = (
            float(tolerances["joint_rad"]),
            float(motion["max_joint_state_age_s"]),
            float(arm_limits["velocity_scaling"]),
            float(arm_limits["acceleration_scaling"]),
            float(arm_limits["planning_timeout_s"]),
            float(arm_limits["execution_timeout_s"]),
            float(motion["gripper_positions_m"]["open"]),
            float(gripper_limits["completion_tolerance_m"]),
            float(motion["execution_timeouts_s"]["cancel"]),
        )
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"{prefix}_QUALIFICATION", str(exc)) from exc
    if (
        motion.get("qualification_status") != "QUALIFIED"
        or not isinstance(motion.get("robot_system_id"), str)
        or not SAFE_ID.fullmatch(motion["robot_system_id"])
        or not isinstance(motion.get("home_candidate_digest"), str)
        or not DIGEST.fullmatch(motion["home_candidate_digest"])
        or motion.get("schema_version")
        == "data_factory.motion_qualification.v2"
        and (
            not isinstance(motion.get("planning_scene_profile_id"), str)
            or SAFE_ID.fullmatch(motion["planning_scene_profile_id"]) is None
            or not isinstance(
                motion.get("planning_scene_profile_digest"), str,
            )
            or DIGEST.fullmatch(
                motion["planning_scene_profile_digest"]
            ) is None
        )
        or not isinstance(safe, list)
        or len(safe) != len(JOINT_ORDER)
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            for item in safe
        )
        or not isinstance(scene, Mapping)
        or _canonical(scene, f"{prefix}_QUALIFICATION")
        != motion.get("planning_scene_digest")
        or not isinstance(frames, Mapping)
        or frames.get("planning_group") != "fairino5_v6_group"
        or any(not math.isfinite(item) for item in numbers)
        or not 0 < numbers[0] <= 0.01
        or not 0 < numbers[1] <= 0.1
        or not 0 < numbers[2] <= 0.1
        or not 0 < numbers[3] <= 0.1
        or any(item <= 0 for item in numbers[4:6])
        or not 0 < numbers[6]
        or not 0 < numbers[7] <= numbers[6] / 100 + 1e-6
        or not 0 < numbers[8]
    ):
        raise ContractError(f"{prefix}_QUALIFICATION")
    motion["planning_scene"] = scene
    return motion


def validate_home_recovery_qualification(value: object) -> dict[str, Any]:
    """Validate and normalize HOME inputs before any physical preparation."""
    return _validated_motion(value, "HOME_RECOVERY")


def _validated_start_pose(
    value: object, *, motion: Mapping[str, Any],
) -> tuple[dict[str, Any], list[float], list[float]]:
    if not isinstance(value, Mapping) or set(value) != POSE_KEYS:
        raise ContractError("START_TRANSITION_QUALIFICATION")
    pose = copy.deepcopy(dict(value))
    target, tolerances = pose.get("target_rad"), pose.get("tolerance_rad")
    if (
        not isinstance(target, Mapping)
        or not isinstance(tolerances, Mapping)
        or set(target) != set(JOINT_ORDER)
        or set(tolerances) != set(JOINT_ORDER)
    ):
        raise ContractError("START_TRANSITION_QUALIFICATION")
    pairs = [(target[name], tolerances[name]) for name in JOINT_ORDER]
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(item)
        for pair in pairs for item in pair
    ) or any(tolerance <= 0 for _, tolerance in pairs):
        raise ContractError("START_TRANSITION_QUALIFICATION")
    if (
        pose.get("schema_version")
        != "data_factory.robot_start_pose_qualification.v1"
        or pose.get("source") != "QUALIFICATION_ARTIFACT"
        or pose.get("qualification_status") != "QUALIFIED"
        or pose.get("safety_status") != "SAFE_FOR_MOTION"
        or pose.get("joint_order") != JOINT_ORDER
        or not isinstance(pose.get("robot_system_id"), str)
        or not SAFE_ID.fullmatch(pose["robot_system_id"])
        or not isinstance(pose.get("robot_start_pose_id"), str)
        or not SAFE_ID.fullmatch(pose["robot_start_pose_id"])
        or pose.get("robot_system_id") != motion["robot_system_id"]
        or pose.get("home_candidate_digest") != motion["home_candidate_digest"]
        or not isinstance(pose.get("qualification_digest"), str)
        or not DIGEST.fullmatch(pose["qualification_digest"])
        or pose["qualification_digest"]
        != _canonical(
            {key: item for key, item in pose.items() if key != "qualification_digest"},
            "START_TRANSITION_QUALIFICATION",
        )
    ):
        raise ContractError("START_TRANSITION_QUALIFICATION")
    return pose, [float(item[0]) for item in pairs], [float(item[1]) for item in pairs]


def _transition(
    transport, *, motion: Mapping[str, Any], before: Mapping[str, Any],
    target: list[float], tolerances: list[float], precommit_call,
    prefix: str, sleep_call=time.sleep, cancel_event=None,
) -> dict[str, Any]:
    _raise_if_cancelled(cancel_event, prefix)
    motion_tolerance = float(motion["goal_tolerances"]["joint_rad"])
    max_age = float(motion["max_joint_state_age_s"])
    open_position = float(motion["gripper_positions_m"]["open"])
    gripper_tolerance = float(
        motion["phase_limits"]["GRIPPER_OPEN"]["completion_tolerance_m"]
    )
    if _gripper_delta(before, open_position, prefix) > gripper_tolerance:
        raise ContractError(f"{prefix}_GRIPPER_NOT_OPEN")
    start = [float(item) for item in before["joint_positions"]]
    errors = [abs(actual - expected) for actual, expected in zip(start, target)]
    if all(error <= tolerance for error, tolerance in zip(errors, tolerances)):
        return {
            "status": "ALREADY_AT_TARGET", "arm_goal_count": 0,
            "target_rad": target, "final_rad": start,
            "max_joint_delta_rad": max(errors),
            "precommit_evidence_digest": None,
        }

    planning = {
        "pipeline_id": "pilz_industrial_motion_planner",
        "ptp_planner_id": "PTP", "lin_planner_id": "LIN",
        "goal_tolerances": dict(motion["goal_tolerances"]),
        "max_joint_state_age_s": max_age,
    }
    planning["goal_tolerances"]["joint_rad"] = min(
        motion_tolerance, *tolerances,
    )
    transition_tolerance = planning["goal_tolerances"]["joint_rad"]
    arm_limits = dict(motion["phase_limits"]["SAFE_POSE_PTP"])
    frames = dict(motion["frames"])
    planned = transport.plan_arm(
        "SAFE_POSE_PTP", None, target, arm_limits, frames, planning, start,
    )
    _raise_if_cancelled(cancel_event, prefix)
    if not isinstance(planned, Mapping):
        raise ContractError(f"{prefix}_PLAN")
    final_state = planned.get("final_joint_state")
    if (
        planned.get("terminal_status") != "SUCCEEDED"
        or planned.get("moveit_success") is not True
        or not isinstance(planned.get("serialized_trajectory"), bytes)
        or not planned["serialized_trajectory"]
        or not isinstance(final_state, list)
        or len(final_state) != len(JOINT_ORDER)
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            for item in final_state
        )
        or any(
            abs(actual - expected) > tolerance
            for actual, expected, tolerance in zip(final_state, target, tolerances)
        )
    ):
        raise ContractError(f"{prefix}_PLAN")
    precommit = precommit_call(
        serialized_trajectory=planned["serialized_trajectory"],
        start_joint_state=start,
        final_joint_state=final_state,
        planning_scene=motion["planning_scene"],
        planning_scene_digest=motion["planning_scene_digest"],
        planning_group=frames["planning_group"],
        max_joint_state_age_s=max_age,
        joint_tolerance_rad=transition_tolerance,
        gripper_tolerance_m=gripper_tolerance,
        before_snapshot=before,
    )
    _raise_if_cancelled(cancel_event, prefix)
    if (
        not isinstance(precommit, Mapping)
        or not isinstance(precommit.get("evidence_digest"), str)
        or not DIGEST.fullmatch(precommit["evidence_digest"])
    ):
        raise ContractError(f"{prefix}_PRECOMMIT")
    fresh = _snapshot(transport.snapshot(max_age), prefix)
    if max(abs(a - b) for a, b in zip(fresh["joint_positions"], start)) > transition_tolerance:
        raise ContractError(f"{prefix}_START_CHANGED")
    _raise_if_cancelled(cancel_event, prefix)
    transport.start_phase({
        "phase": "SAFE_POSE_PTP", "type": "ARM",
        "trajectory_b64": base64.b64encode(
            planned["serialized_trajectory"]
        ).decode("ascii"),
        "limits": arm_limits,
    })
    _wait_phase(
        transport,
        cancel_timeout_s=float(motion["execution_timeouts_s"]["cancel"]),
        prefix=prefix, cancel_event=cancel_event,
        sleep_call=sleep_call,
    )
    after = _snapshot(transport.snapshot(max_age), prefix)
    final = [float(item) for item in after["joint_positions"]]
    errors = [abs(actual - expected) for actual, expected in zip(final, target)]
    if any(error > tolerance for error, tolerance in zip(errors, tolerances)):
        raise ContractError(f"{prefix}_FINAL_MISMATCH")
    if _gripper_delta(after, open_position, prefix) > gripper_tolerance:
        raise ContractError(f"{prefix}_GRIPPER_NOT_OPEN")
    return {
        "status": "AT_TARGET", "arm_goal_count": 1,
        "target_rad": target, "final_rad": final,
        "max_joint_delta_rad": max(errors),
        "precommit_evidence_digest": precommit["evidence_digest"],
    }


def transition_to_start(
    transport, *, motion_qualification: Mapping[str, Any],
    robot_start_pose_qualification: Mapping[str, Any], sleep_call=time.sleep,
    cancel_event=None,
) -> dict[str, Any]:
    """Move once to an already-qualified collection start pose, without recording."""
    motion = _validated_motion(motion_qualification, "START_TRANSITION")
    pose, target, tolerances = _validated_start_pose(
        robot_start_pose_qualification, motion=motion,
    )
    _raise_if_cancelled(cancel_event, "START_TRANSITION")
    _ready_graph(transport.preflight(), "START_TRANSITION_GRAPH")
    before = _snapshot(
        transport.snapshot(float(motion["max_joint_state_age_s"])),
        "START_TRANSITION",
    )
    transition = _transition(
        transport, motion=motion, before=before, target=target,
        tolerances=tolerances,
        precommit_call=transport.precommit_joint_transition,
        prefix="START_TRANSITION", sleep_call=sleep_call,
        cancel_event=cancel_event,
    )
    result = {
        "schema_version": "data_factory.start_transition_receipt.v1",
        "status": (
            "ALREADY_AT_START"
            if transition["status"] == "ALREADY_AT_TARGET"
            else "AT_START"
        ),
        "robot_system_id": pose["robot_system_id"],
        "robot_start_pose_id": pose["robot_start_pose_id"],
        "arm_goal_count": transition["arm_goal_count"],
        "gripper_goal_count": 0,
        "target_rad": dict(zip(JOINT_ORDER, transition["target_rad"])),
        "final_rad": dict(zip(JOINT_ORDER, transition["final_rad"])),
        "max_joint_delta_rad": transition["max_joint_delta_rad"],
        "motion_qualification_digest": canonical_digest(motion),
        "robot_start_pose_qualification_digest": pose["qualification_digest"],
        "home_candidate_digest": pose["home_candidate_digest"],
        "precommit_evidence_digest": transition["precommit_evidence_digest"],
        "authority": "NO_EXECUTION_AUTHORITY",
        "training_authority": False,
    }
    result["receipt_digest"] = canonical_digest(result)
    return result


def recover_home(
    transport, *, motion_qualification: Mapping[str, Any], sleep_call=time.sleep,
) -> dict[str, Any]:
    """Open the gripper, move once to qualified HOME, and verify."""
    motion = validate_home_recovery_qualification(motion_qualification)
    _ready_graph(transport.preflight(), "HOME_RECOVERY_GRAPH")
    max_age = float(motion["max_joint_state_age_s"])
    before = _snapshot(transport.snapshot(max_age), "HOME_RECOVERY")
    open_position = float(motion["gripper_positions_m"]["open"])
    gripper_limits = dict(motion["phase_limits"]["GRIPPER_OPEN"])
    gripper_tolerance = float(gripper_limits["completion_tolerance_m"])
    if _gripper_delta(before, open_position, "HOME_RECOVERY") > gripper_tolerance:
        serialized = transport.build_gripper_goal(
            "GRIPPER_OPEN", open_position, gripper_limits,
        )
        transport.start_phase({
            "phase": "GRIPPER_OPEN", "type": "GRIPPER",
            "trajectory_b64": base64.b64encode(serialized).decode("ascii"),
            "limits": gripper_limits,
        })
        _wait_phase(
            transport,
            cancel_timeout_s=float(motion["execution_timeouts_s"]["cancel"]),
            prefix="HOME_RECOVERY", sleep_call=sleep_call,
        )
        before = _snapshot(transport.snapshot(max_age), "HOME_RECOVERY")

    target = [float(item) for item in motion["qualified_safe_joint_positions_rad"]]
    tolerances = [float(motion["goal_tolerances"]["joint_rad"])] * len(JOINT_ORDER)
    transition = _transition(
        transport, motion=motion, before=before, target=target,
        tolerances=tolerances,
        precommit_call=transport.precommit_home_recovery,
        prefix="HOME_RECOVERY", sleep_call=sleep_call,
    )
    result = {
        "schema_version": "data_factory.home_recovery.v1",
        "status": (
            "ALREADY_HOME"
            if transition["status"] == "ALREADY_AT_TARGET"
            else "HOME"
        ),
        "arm_goal_count": transition["arm_goal_count"],
        "gripper_open": True,
        "target_rad": transition["target_rad"],
        "final_rad": transition["final_rad"],
        "motion_qualification_digest": canonical_digest(motion),
    }
    if transition["arm_goal_count"]:
        result.update({
            "max_joint_delta_rad": transition["max_joint_delta_rad"],
            "precommit_evidence_digest": transition["precommit_evidence_digest"],
        })
        result["result_digest"] = canonical_digest(result)
    return result


def _live(node_name: str, unavailable_code: str, call) -> dict[str, Any]:
    try:
        import rclpy
        from tools.data_factory.motion.moveit_transport import RosMoveItTransport
    except ImportError as exc:
        raise ContractError(unavailable_code) from exc
    owned_context = not rclpy.ok()
    node = None
    try:
        if owned_context:
            rclpy.init()
        node = rclpy.create_node(node_name)
        return call(RosMoveItTransport(node))
    finally:
        if node is not None:
            node.destroy_node()
        if owned_context and rclpy.ok():
            rclpy.shutdown()


def recover_home_live(*, motion_qualification: Mapping[str, Any]) -> dict[str, Any]:
    """Run HOME recovery in this foreground process and release its ROS node."""
    return _live(
        "fr5_operator_home_recovery", "HOME_RECOVERY_ROS_UNAVAILABLE",
        lambda transport: recover_home(
            transport, motion_qualification=motion_qualification,
        ),
    )


def transition_to_start_live(
    *, motion_qualification: Mapping[str, Any],
    robot_start_pose_qualification: Mapping[str, Any],
    cancel_event=None,
) -> dict[str, Any]:
    """Run a qualified start transition in this process and release its node."""
    return _live(
        "fr5_operator_start_transition", "START_TRANSITION_ROS_UNAVAILABLE",
        lambda transport: transition_to_start(
            transport,
            motion_qualification=motion_qualification,
            robot_start_pose_qualification=robot_start_pose_qualification,
            cancel_event=cancel_event,
        ),
    )


__all__ = [
    "recover_home", "recover_home_live",
    "validate_home_recovery_qualification",
    "transition_to_start", "transition_to_start_live",
]
