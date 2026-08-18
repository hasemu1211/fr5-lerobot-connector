"""ROS Jazzy plan-only transport for the FR5 pickup executor."""
from __future__ import annotations

import base64
import math
import time
from dataclasses import dataclass

from tools.fr5_data_factory import ContractError


JOINT_ORDER = ["j1", "j2", "j3", "j4", "j5", "j6"]
ACTION_TYPES = {
    "/move_action": "moveit_msgs/action/MoveGroup",
    "/execute_trajectory": "moveit_msgs/action/ExecuteTrajectory",
    "/gripper_controller/follow_joint_trajectory":
        "control_msgs/action/FollowJointTrajectory",
}


@dataclass(slots=True)
class _ActivePhase:
    phase: str
    type: str
    goal_handle: object
    result_future: object
    deadline: float


def _rotation_quaternion(columns):
    """Convert column-major rotation axes to a deterministic xyzw quaternion."""
    matrix = [[columns[column][row] for column in range(3)] for row in range(3)]
    trace = matrix[0][0] + matrix[1][1] + matrix[2][2]
    if trace > 0:
        scale = math.sqrt(trace + 1.0) * 2
        x = (matrix[2][1] - matrix[1][2]) / scale
        y = (matrix[0][2] - matrix[2][0]) / scale
        z = (matrix[1][0] - matrix[0][1]) / scale
        w = scale / 4
    else:
        index = max(range(3), key=lambda item: matrix[item][item])
        if index == 0:
            scale = math.sqrt(1 + matrix[0][0] - matrix[1][1] - matrix[2][2]) * 2
            x, y, z, w = (
                scale / 4,
                (matrix[0][1] + matrix[1][0]) / scale,
                (matrix[0][2] + matrix[2][0]) / scale,
                (matrix[2][1] - matrix[1][2]) / scale,
            )
        elif index == 1:
            scale = math.sqrt(1 + matrix[1][1] - matrix[0][0] - matrix[2][2]) * 2
            x, y, z, w = (
                (matrix[0][1] + matrix[1][0]) / scale,
                scale / 4,
                (matrix[1][2] + matrix[2][1]) / scale,
                (matrix[0][2] - matrix[2][0]) / scale,
            )
        else:
            scale = math.sqrt(1 + matrix[2][2] - matrix[0][0] - matrix[1][1]) * 2
            x, y, z, w = (
                (matrix[0][2] + matrix[2][0]) / scale,
                (matrix[1][2] + matrix[2][1]) / scale,
                scale / 4,
                (matrix[1][0] - matrix[0][1]) / scale,
            )
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    values = [x / norm, y / norm, z / norm, w / norm]
    if values[3] < 0:
        values = [-value for value in values]
    return values


class RosMoveItTransport:
    """Build plan-only MoveGroup requests and serialized gripper goals."""

    def __init__(self, node, *, graph_timeout_s=1.0, clock=time.monotonic):
        try:
            import rclpy
            from action_msgs.msg import GoalStatus
            from builtin_interfaces.msg import Duration
            from control_msgs.action import FollowJointTrajectory
            from control_msgs.msg import JointTolerance, JointTrajectoryControllerState
            from geometry_msgs.msg import Pose
            from moveit_msgs.action import ExecuteTrajectory, MoveGroup
            from moveit_msgs.msg import (
                Constraints,
                JointConstraint,
                OrientationConstraint,
                PositionConstraint,
                RobotState,
                RobotTrajectory,
                MoveItErrorCodes,
            )
            from rclpy.action import ActionClient, get_action_names_and_types
            from rclpy.serialization import deserialize_message, serialize_message
            from sensor_msgs.msg import JointState
            from shape_msgs.msg import SolidPrimitive
            from trajectory_msgs.msg import JointTrajectoryPoint
        except ImportError as exc:
            raise ContractError("ROS_JAZZY_UNAVAILABLE", str(exc)) from exc

        self.node = node
        self.graph_timeout_s = graph_timeout_s
        self._clock = clock
        self._rclpy = rclpy
        self._get_action_names_and_types = get_action_names_and_types
        self._serialize_message = serialize_message
        self._deserialize_message = deserialize_message
        self._Duration = Duration
        self._FollowJointTrajectory = FollowJointTrajectory
        self._ExecuteTrajectory = ExecuteTrajectory
        self._RobotTrajectory = RobotTrajectory
        self._goal_succeeded = GoalStatus.STATUS_SUCCEEDED
        self._goal_canceled = GoalStatus.STATUS_CANCELED
        self._moveit_success = MoveItErrorCodes.SUCCESS
        self._gripper_success = FollowJointTrajectory.Result.SUCCESSFUL
        self._JointTolerance = JointTolerance
        self._Pose = Pose
        self._MoveGroup = MoveGroup
        self._Constraints = Constraints
        self._JointConstraint = JointConstraint
        self._OrientationConstraint = OrientationConstraint
        self._PositionConstraint = PositionConstraint
        self._RobotState = RobotState
        self._JointState = JointState
        self._JointTrajectoryControllerState = JointTrajectoryControllerState
        self._SolidPrimitive = SolidPrimitive
        self._JointTrajectoryPoint = JointTrajectoryPoint
        self.move_group = ActionClient(node, MoveGroup, "/move_action")
        self.execute_trajectory = ActionClient(
            node, ExecuteTrajectory, "/execute_trajectory"
        )
        self.gripper = ActionClient(
            node,
            FollowJointTrajectory,
            "/gripper_controller/follow_joint_trajectory",
        )
        self._joint_state = None
        self._joint_state_received_at = None
        self._arm_controller_state = None
        self._arm_controller_received_at = None
        self._gripper_controller_state = None
        self._gripper_controller_received_at = None
        self._active = None
        self._execution_locked = False
        self._joint_state_subscription = None
        self._arm_controller_subscription = None
        self._gripper_controller_subscription = None
        if hasattr(node, "create_subscription"):
            self._joint_state_subscription = node.create_subscription(
                JointState, "/joint_states", self._on_joint_state, 10
            )
            self._arm_controller_subscription = node.create_subscription(
                JointTrajectoryControllerState,
                "/fairino5_controller/controller_state",
                self._on_arm_controller_state,
                10,
            )
            self._gripper_controller_subscription = node.create_subscription(
                JointTrajectoryControllerState,
                "/gripper_controller/controller_state",
                self._on_gripper_controller_state,
                10,
            )

    def _on_joint_state(self, message):
        self._joint_state = message
        self._joint_state_received_at = self._clock()

    def _on_arm_controller_state(self, message):
        self._arm_controller_state = message
        self._arm_controller_received_at = self._clock()

    def _on_gripper_controller_state(self, message):
        self._gripper_controller_state = message
        self._gripper_controller_received_at = self._clock()

    def _compiled_execution_goal(self, compiled_step):
        if not isinstance(compiled_step, dict):
            raise ContractError("ROS_EXEC_STEP")
        phase = compiled_step.get("phase")
        step_type = compiled_step.get("type")
        encoded = compiled_step.get("trajectory_b64")
        limits = compiled_step.get("limits")
        if (
            not isinstance(phase, str)
            or not phase
            or step_type not in {"ARM", "GRIPPER"}
            or not isinstance(encoded, str)
            or not isinstance(limits, dict)
        ):
            raise ContractError("ROS_EXEC_STEP")
        timeout = limits.get("execution_timeout_s")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ContractError("ROS_EXEC_STEP")
        try:
            serialized = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ContractError("ROS_EXEC_B64", str(exc)) from exc
        if not serialized:
            raise ContractError("ROS_EXEC_B64")
        try:
            if step_type == "ARM":
                goal = self._ExecuteTrajectory.Goal()
                goal.trajectory = self._deserialize_message(
                    serialized, self._RobotTrajectory
                )
                client = self.execute_trajectory
            else:
                goal = self._deserialize_message(serialized, self._FollowJointTrajectory.Goal)
                client = self.gripper
        except RuntimeError as exc:
            raise ContractError("ROS_EXEC_DESERIALIZATION", str(exc)) from exc
        return phase, step_type, goal, client, float(timeout)

    def start_phase(self, compiled_step):
        """Start one approved serialized action and retain its sole active handle."""
        if self._execution_locked or self._active is not None:
            raise ContractError("ROS_EXEC_ACTIVE")
        phase, step_type, goal, client, timeout = self._compiled_execution_goal(compiled_step)
        try:
            sent = client.send_goal_async(goal)
        except RuntimeError as exc:
            raise ContractError("ROS_EXEC_GOAL_FAILED", str(exc)) from exc
        self._execution_locked = True
        handle = self._wait(sent, self.graph_timeout_s, "ROS_EXEC_GOAL_TIMEOUT")
        accepted = getattr(handle, "accepted", None)
        if accepted is False:
            self._execution_locked = False
            raise ContractError("ROS_EXEC_REJECTED")
        if accepted is not True:
            raise ContractError("ROS_EXEC_GOAL_RESPONSE_INVALID")
        try:
            result_future = handle.get_result_async()
        except RuntimeError as exc:
            raise ContractError("ROS_EXEC_RESULT_FAILED", str(exc)) from exc
        self._execution_locked = False
        self._active = _ActivePhase(
            phase, step_type, handle, result_future, self._clock() + timeout
        )
        return self._active

    def poll_active(self):
        """Return None while active, or a successful phase handle when terminal."""
        active = getattr(self, "_active", None)
        if active is None:
            raise ContractError("ROS_EXEC_NO_ACTIVE")
        if self._clock() > active.deadline:
            raise ContractError("ROS_EXEC_RESULT_TIMEOUT")
        try:
            if not active.result_future.done():
                self._rclpy.spin_once(self.node, timeout_sec=0.0)
            if not active.result_future.done():
                return None
            result = active.result_future.result()
        except RuntimeError as exc:
            raise ContractError("ROS_EXEC_RESULT_FAILED", str(exc)) from exc
        self._active = None
        if result.status != self._goal_succeeded:
            self._execution_locked = True
            raise ContractError("ROS_EXEC_FAILED")
        if active.type == "ARM":
            succeeded = result.result.error_code.val == self._moveit_success
        else:
            succeeded = result.result.error_code == self._gripper_success
        if not succeeded:
            self._execution_locked = True
            raise ContractError("ROS_EXEC_FAILED")
        return active

    def cancel_active(self, cancel_timeout_s):
        """Cancel the active action; it is never exposed as active again."""
        active = getattr(self, "_active", None)
        if active is None:
            raise ContractError("ROS_EXEC_NO_ACTIVE")
        if (
            isinstance(cancel_timeout_s, bool)
            or not isinstance(cancel_timeout_s, (int, float))
            or not math.isfinite(cancel_timeout_s)
            or cancel_timeout_s <= 0
        ):
            raise ContractError("ROS_EXEC_CANCEL_TIMEOUT")
        self._execution_locked = True
        try:
            canceled = active.goal_handle.cancel_goal_async()
            response = self._wait(canceled, float(cancel_timeout_s), "ROS_EXEC_CANCEL_ACK_TIMEOUT")
            if not response.goals_canceling:
                raise ContractError("ROS_EXEC_CANCEL_REJECTED")
            result = self._wait(
                active.result_future, float(cancel_timeout_s), "ROS_EXEC_CANCEL_RESULT_TIMEOUT"
            )
        except ContractError:
            raise
        except RuntimeError as exc:
            raise ContractError("ROS_EXEC_CANCEL_FAILED", str(exc)) from exc
        if result.status != self._goal_canceled:
            self._active = None
            raise ContractError("ROS_EXEC_CANCEL_NOT_CANCELED")
        self._active = None
        return active

    def _fresh(self, received_at, max_age_s, code):
        if received_at is None:
            raise ContractError(code)
        age = self._clock() - received_at
        if age < 0 or age > max_age_s:
            raise ContractError(code)
        return age

    def snapshot(self, max_age_s):
        """Return a fresh, complete observation for execution safety checks."""
        if (
            isinstance(max_age_s, bool)
            or not isinstance(max_age_s, (int, float))
            or not math.isfinite(max_age_s)
            or max_age_s < 0
        ):
            raise ContractError("ROS_SNAPSHOT_AGE")
        max_age_s = float(max_age_s)
        deadline = time.monotonic() + self.graph_timeout_s
        while (
            self._joint_state_received_at is None
            or self._arm_controller_received_at is None
            or self._gripper_controller_received_at is None
            or any(
                self._clock() - received_at > max_age_s
                for received_at in (
                    self._joint_state_received_at,
                    self._arm_controller_received_at,
                    self._gripper_controller_received_at,
                )
            )
        ) and time.monotonic() < deadline:
            self._rclpy.spin_once(self.node, timeout_sec=max(0.0, min(0.05, deadline - time.monotonic())))
        joint_age = self._fresh(self._joint_state_received_at, max_age_s, "ROS_JOINT_STATE_STALE")
        arm_age = self._fresh(self._arm_controller_received_at, max_age_s, "ROS_ARM_CONTROLLER_STALE")
        gripper_age = self._fresh(self._gripper_controller_received_at, max_age_s, "ROS_GRIPPER_CONTROLLER_STALE")
        names = list(self._joint_state.name)
        positions = list(self._joint_state.position)
        if len(names) != len(set(names)) or not set(JOINT_ORDER).issubset(names) or len(positions) != len(names):
            raise ContractError("ROS_JOINT_STATE")
        by_name = dict(zip(names, positions))
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in by_name.values()):
            raise ContractError("ROS_JOINT_STATE")
        try:
            arm_publishers = self.node.count_publishers("/fairino5_controller/controller_state")
            gripper_publishers = self.node.count_publishers("/gripper_controller/controller_state")
            topics = dict(self.node.get_topic_names_and_types())
        except RuntimeError as exc:
            raise ContractError("ROS_GRAPH_FAILED", str(exc)) from exc
        controller_type = "control_msgs/msg/JointTrajectoryControllerState"
        arm_type = topics.get("/fairino5_controller/controller_state", [])
        gripper_type = topics.get("/gripper_controller/controller_state", [])
        self._controller_values(self._arm_controller_state, "ROS_ARM_CONTROLLER_STATE")
        self._controller_values(self._gripper_controller_state, "ROS_GRIPPER_CONTROLLER_STATE")
        arm_speed = self._arm_controller_state.speed_scaling_factor
        if not isinstance(arm_speed, (int, float)) or not math.isfinite(arm_speed):
            raise ContractError("ROS_ARM_CONTROLLER_STATE")
        gripper_speed = self._gripper_controller_state.speed_scaling_factor
        if not isinstance(gripper_speed, (int, float)) or not math.isfinite(gripper_speed):
            raise ContractError("ROS_GRIPPER_CONTROLLER_STATE")
        return {
            "joint_positions": [by_name[name] for name in JOINT_ORDER],
            "joint_state_age_s": joint_age,
            "arm_controller": {
                "endpoint": "/fairino5_controller/controller_state",
                "type": controller_type if arm_type == [controller_type] else "|".join(sorted(arm_type)),
                "publisher_count": arm_publishers,
                "ready": arm_type == [controller_type] and arm_publishers > 0,
                "age_s": arm_age,
                "speed_scaling": float(arm_speed),
            },
            "gripper_controller": {
                "endpoint": "/gripper_controller/controller_state",
                "type": controller_type if gripper_type == [controller_type] else "|".join(sorted(gripper_type)),
                "publisher_count": gripper_publishers,
                "ready": gripper_type == [controller_type] and gripper_publishers > 0,
                "age_s": gripper_age,
                "speed_scaling": float(gripper_speed),
            },
        }

    @staticmethod
    def _controller_values(message, code):
        names = list(message.joint_names)
        if not names or len(names) != len(set(names)):
            raise ContractError(code)
        for point in (message.reference, message.feedback):
            positions = list(point.positions)
            if positions:
                if len(positions) != len(names) or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in positions
                ):
                    raise ContractError(code)
                return
        raise ContractError(code)

    def preflight(self):
        deadline = time.monotonic() + self.graph_timeout_s
        while True:
            try:
                actions = dict(self._get_action_names_and_types(self.node))
                topics = dict(self.node.get_topic_names_and_types())
                publisher_count = self.node.count_publishers("/joint_states")
            except RuntimeError as exc:
                raise ContractError("ROS_GRAPH_FAILED", str(exc)) from exc
            graph_ready = (
                all(actions.get(endpoint) == [kind] for endpoint, kind in ACTION_TYPES.items())
                and topics.get("/joint_states") == ["sensor_msgs/msg/JointState"]
                and publisher_count > 0
            )
            if graph_ready or time.monotonic() >= deadline:
                break
            self._rclpy.spin_once(
                self.node,
                timeout_sec=max(0.0, min(0.05, deadline - time.monotonic())),
            )

        clients = {
            "/move_action": self.move_group,
            "/execute_trajectory": self.execute_trajectory,
            "/gripper_controller/follow_joint_trajectory": self.gripper,
        }
        facts = {}
        fact_keys = {
            "/move_action": "move_action",
            "/execute_trajectory": "execute_trajectory",
            "/gripper_controller/follow_joint_trajectory": "gripper",
        }
        for endpoint, expected_type in ACTION_TYPES.items():
            observed = actions.get(endpoint, [])
            exact_type = observed == [expected_type]
            ready = exact_type and clients[endpoint].wait_for_server(
                timeout_sec=self.graph_timeout_s
            )
            facts[fact_keys[endpoint]] = {
                "endpoint": endpoint,
                "type": expected_type if exact_type else "|".join(sorted(observed)),
                "ready": bool(ready),
            }

        joint_types = topics.get("/joint_states", [])
        exact_joint_type = joint_types == ["sensor_msgs/msg/JointState"]
        facts["joint_states"] = {
            "endpoint": "/joint_states",
            "type": (
                "sensor_msgs/msg/JointState"
                if exact_joint_type
                else "|".join(sorted(joint_types))
            ),
            "ready": exact_joint_type and publisher_count > 0,
        }
        facts["joint_order"] = list(JOINT_ORDER)
        return facts

    def _duration(self, seconds):
        nanoseconds = round(seconds * 1_000_000_000)
        return self._Duration(
            sec=nanoseconds // 1_000_000_000,
            nanosec=nanoseconds % 1_000_000_000,
        )

    def _wait(self, future, timeout_s, code):
        try:
            self._rclpy.spin_until_future_complete(
                self.node, future, timeout_sec=timeout_s
            )
            if not future.done():
                raise ContractError(code)
            return future.result()
        except ContractError:
            raise
        except (RuntimeError, TimeoutError) as exc:
            raise ContractError(code, str(exc)) from exc

    def _cancel_planning(self, goal_handle):
        try:
            future = goal_handle.cancel_goal_async()
        except RuntimeError as exc:
            raise ContractError("ROS_PLAN_CANCEL_FAILED", str(exc)) from exc
        response = self._wait(
            future, self.graph_timeout_s, "ROS_PLAN_CANCEL_TIMEOUT"
        )
        if not response.goals_canceling:
            raise ContractError("ROS_PLAN_CANCEL_REJECTED")

    def plan_arm(
        self,
        phase,
        target,
        joint_target,
        limits,
        frames,
        planning,
        start_joint_state,
    ):
        goal = self._move_group_goal(
            phase,
            target,
            joint_target,
            limits,
            frames,
            planning,
            start_joint_state,
        )
        try:
            send_future = self.move_group.send_goal_async(goal)
        except RuntimeError as exc:
            raise ContractError("ROS_PLAN_GOAL_FAILED", str(exc)) from exc
        handle = self._wait(
            send_future, self.graph_timeout_s, "ROS_PLAN_GOAL_TIMEOUT"
        )
        if handle is None or not handle.accepted:
            raise ContractError("ROS_PLAN_REJECTED")
        try:
            result = self._wait(
                handle.get_result_async(),
                limits["planning_timeout_s"] + self.graph_timeout_s,
                "ROS_PLAN_RESULT_TIMEOUT",
            )
        except ContractError as exc:
            if exc.code == "ROS_PLAN_RESULT_TIMEOUT":
                self._cancel_planning(handle)
            raise
        if (
            result.status != self._goal_succeeded
            or result.result.error_code.val != self._moveit_success
        ):
            raise ContractError("ROS_PLAN_FAILED")

        trajectory = result.result.planned_trajectory
        names = list(trajectory.joint_trajectory.joint_names)
        points = trajectory.joint_trajectory.points
        if len(names) != len(set(names)) or set(names) != set(JOINT_ORDER) or not points:
            raise ContractError("ROS_FINAL_JOINTS")
        positions = list(points[-1].positions)
        if len(positions) != len(names) or any(not math.isfinite(value) for value in positions):
            raise ContractError("ROS_FINAL_JOINTS")
        by_name = dict(zip(names, positions))
        serialized = self._serialize_message(trajectory)
        if not serialized:
            raise ContractError("ROS_PLAN_SERIALIZATION")
        return {
            "terminal_status": "SUCCEEDED",
            "moveit_success": True,
            "serialized_trajectory": serialized,
            "final_joint_state": [by_name[name] for name in JOINT_ORDER],
        }

    def _move_group_goal(
        self,
        phase,
        target,
        joint_target,
        limits,
        frames,
        planning,
        start_joint_state,
    ):
        goal = self._MoveGroup.Goal()
        request = goal.request
        request.pipeline_id = planning["pipeline_id"]
        request.planner_id = (
            planning["ptp_planner_id"]
            if phase.endswith("_PTP")
            else planning["lin_planner_id"]
        )
        request.group_name = frames["planning_group"]
        request.num_planning_attempts = 1
        request.allowed_planning_time = limits["planning_timeout_s"]
        request.max_velocity_scaling_factor = limits["velocity_scaling"]
        request.max_acceleration_scaling_factor = limits["acceleration_scaling"]
        request.start_state = self._RobotState(
            joint_state=self._JointState(
                name=list(JOINT_ORDER), position=list(start_joint_state)
            ),
            is_diff=True,
        )
        request.goal_constraints = [
            self._constraints(phase, target, joint_target, frames, planning)
        ]
        goal.planning_options.plan_only = True
        goal.planning_options.planning_scene_diff.is_diff = True
        goal.planning_options.planning_scene_diff.robot_state.is_diff = True
        return goal

    def _constraints(self, phase, target, joint_target, frames, planning):
        constraints = self._Constraints(name=phase)
        tolerances = planning["goal_tolerances"]
        if joint_target is not None:
            constraints.joint_constraints = [
                self._JointConstraint(
                    joint_name=name,
                    position=value,
                    tolerance_above=tolerances["joint_rad"],
                    tolerance_below=tolerances["joint_rad"],
                    weight=1.0,
                )
                for name, value in zip(JOINT_ORDER, joint_target)
            ]
            return constraints

        pose = target["base_tool"]
        primitive = self._SolidPrimitive(
            type=self._SolidPrimitive.SPHERE,
            dimensions=[tolerances["position_m"]],
        )
        region_pose = self._Pose()
        region_pose.position.x, region_pose.position.y, region_pose.position.z = (
            pose["translation_m"]
        )
        region_pose.orientation.w = 1.0
        position = self._PositionConstraint()
        position.header.frame_id = frames["planning_frame"]
        position.link_name = frames["tool_link"]
        position.constraint_region.primitives = [primitive]
        position.constraint_region.primitive_poses = [region_pose]
        position.weight = 1.0

        quaternion = _rotation_quaternion(pose["rotation_columns"])
        orientation = self._OrientationConstraint()
        orientation.header.frame_id = frames["planning_frame"]
        orientation.link_name = frames["tool_link"]
        (
            orientation.orientation.x,
            orientation.orientation.y,
            orientation.orientation.z,
            orientation.orientation.w,
        ) = quaternion
        orientation.absolute_x_axis_tolerance = tolerances["orientation_rad"]
        orientation.absolute_y_axis_tolerance = tolerances["orientation_rad"]
        orientation.absolute_z_axis_tolerance = tolerances["orientation_rad"]
        orientation.weight = 1.0
        constraints.position_constraints = [position]
        constraints.orientation_constraints = [orientation]
        return constraints

    def build_gripper_goal(self, phase, position, limits):
        del phase
        goal = self._FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = ["finger_right_joint"]
        point = self._JointTrajectoryPoint()
        point.positions = [position]
        point.time_from_start = self._duration(limits["command_duration_s"])
        goal.trajectory.points = [point]
        goal.goal_tolerance = [
            self._JointTolerance(
                name="finger_right_joint",
                position=limits["completion_tolerance_m"],
            )
        ]
        goal.goal_time_tolerance = self._duration(
            limits["execution_timeout_s"] - limits["command_duration_s"]
        )
        serialized = self._serialize_message(goal)
        if not serialized:
            raise ContractError("ROS_GRIPPER_SERIALIZATION")
        return serialized


__all__ = ["RosMoveItTransport"]
