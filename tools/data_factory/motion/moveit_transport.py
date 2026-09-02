"""ROS Jazzy plan-only transport for the FR5 pickup executor."""
from __future__ import annotations

import base64
import math
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from tools.fr5_data_factory import ContractError, canonical_digest


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

    def __init__(
        self, node, *, graph_timeout_s=1.0, preflight_timeout_s=5.0,
        clock=time.monotonic,
    ):
        try:
            import rclpy
            from action_msgs.msg import GoalStatus
            from builtin_interfaces.msg import Duration
            from control_msgs.action import FollowJointTrajectory
            from control_msgs.msg import JointTolerance, JointTrajectoryControllerState
            from geometry_msgs.msg import Pose
            from moveit_msgs.action import ExecuteTrajectory, MoveGroup
            from moveit_msgs.msg import (
                CollisionObject,
                Constraints,
                JointConstraint,
                OrientationConstraint,
                PositionConstraint,
                RobotState,
                RobotTrajectory,
                PlanningScene,
                PlanningSceneComponents,
                MoveItErrorCodes,
            )
            from moveit_msgs.srv import ApplyPlanningScene, GetPlanningScene, GetStateValidity
            from rclpy.action import ActionClient, get_action_names_and_types
            from rclpy.parameter_client import AsyncParameterClient
            from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
            from rcl_interfaces.msg import ParameterType
            from rclpy.serialization import deserialize_message, serialize_message
            from sensor_msgs.msg import JointState
            from shape_msgs.msg import SolidPrimitive
            from std_msgs.msg import String
            from trajectory_msgs.msg import JointTrajectoryPoint
        except ImportError as exc:
            raise ContractError("ROS_JAZZY_UNAVAILABLE", str(exc)) from exc

        self.node = node
        self.graph_timeout_s = graph_timeout_s
        self.preflight_timeout_s = preflight_timeout_s
        self._clock = clock
        self._rclpy = rclpy
        self._get_action_names_and_types = get_action_names_and_types
        self._serialize_message = serialize_message
        self._deserialize_message = deserialize_message
        self._Duration = Duration
        self._FollowJointTrajectory = FollowJointTrajectory
        self._ExecuteTrajectory = ExecuteTrajectory
        self._RobotTrajectory = RobotTrajectory
        self._CollisionObject = CollisionObject
        self._PlanningScene = PlanningScene
        self._PlanningSceneComponents = PlanningSceneComponents
        self._ApplyPlanningScene = ApplyPlanningScene
        self._GetPlanningScene = GetPlanningScene
        self._GetStateValidity = GetStateValidity
        self._goal_succeeded = GoalStatus.STATUS_SUCCEEDED
        self._goal_canceled = GoalStatus.STATUS_CANCELED
        self._moveit_success = MoveItErrorCodes.SUCCESS
        self._gripper_success = FollowJointTrajectory.Result.SUCCESSFUL
        self._parameter_string = ParameterType.PARAMETER_STRING
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
        self._robot_description = None
        self._active = None
        self._execution_locked = False
        self._execute_goal_count = 0
        self._gripper_goal_count = 0
        self._service_clients = {}
        self._joint_state_subscription = None
        self._arm_controller_subscription = None
        self._gripper_controller_subscription = None
        self._robot_description_subscription = None
        self._robot_description_client = None
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
            self._robot_description_subscription = node.create_subscription(
                String,
                "/robot_description",
                self._on_robot_description,
                QoSProfile(
                    depth=1,
                    durability=DurabilityPolicy.TRANSIENT_LOCAL,
                    reliability=ReliabilityPolicy.RELIABLE,
                ),
            )
        if hasattr(node, "create_client"):
            self._robot_description_client = AsyncParameterClient(
                node, "/robot_state_publisher"
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

    def _on_robot_description(self, message):
        self._robot_description = message.data

    def _service(self, service_type, endpoint, request, code):
        if not hasattr(self.node, "create_client"):
            raise ContractError(code)
        client = self._service_clients.get(endpoint)
        if client is None:
            client = self.node.create_client(service_type, endpoint)
            self._service_clients[endpoint] = client
        try:
            if not client.wait_for_service(timeout_sec=self.graph_timeout_s):
                raise ContractError(code)
            return self._wait(client.call_async(request), self.graph_timeout_s, code)
        except ContractError:
            raise
        except RuntimeError as exc:
            raise ContractError(code, str(exc)) from exc

    def _collision_object(self, identifier, dimensions, position, frame_id):
        if (
            not isinstance(identifier, str) or not identifier
            or not isinstance(dimensions, list) or len(dimensions) != 3
            or not isinstance(position, list) or len(position) != 3
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in dimensions)
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in position)
        ):
            raise ContractError("PLANNING_SCENE_SCHEMA")
        item = self._CollisionObject()
        item.header.frame_id = frame_id
        item.id = identifier
        item.pose.position.x, item.pose.position.y, item.pose.position.z = map(float, position)
        item.pose.orientation.w = 1.0
        primitive = self._SolidPrimitive(type=self._SolidPrimitive.BOX, dimensions=list(map(float, dimensions)))
        pose = self._Pose()
        pose.orientation.w = 1.0
        item.primitives, item.primitive_poses = [primitive], [pose]
        item.operation = self._CollisionObject.ADD
        return item

    def _planning_scene_objects(self, spec):
        if not isinstance(spec, dict) or set(spec) != {"frame_id", "floor", "wall"}:
            raise ContractError("PLANNING_SCENE_SCHEMA")
        frame_id, floor, wall = spec["frame_id"], spec["floor"], spec["wall"]
        if not isinstance(frame_id, str) or not frame_id:
            raise ContractError("PLANNING_SCENE_SCHEMA")
        if not isinstance(floor, dict) or not isinstance(wall, dict):
            raise ContractError("PLANNING_SCENE_SCHEMA")
        try:
            floor_position = [0.0, 0.0, float(floor["surface_z_m"]) - float(floor["dimensions_m"][2]) / 2]
            wall_position = [0.0, float(wall["near_face_y_m"]) - float(wall["dimensions_m"][1]) / 2, 0.0]
            return [
                self._collision_object(floor["id"], floor["dimensions_m"], floor_position, frame_id),
                self._collision_object(wall["id"], wall["dimensions_m"], wall_position, frame_id),
            ]
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ContractError("PLANNING_SCENE_SCHEMA", str(exc)) from exc

    def _apply_and_readback_scene(self, spec, expected_digest):
        if canonical_digest(spec) != expected_digest:
            raise ContractError("PLANNING_SCENE_BINDING")
        expected = self._planning_scene_objects(spec)
        request = self._ApplyPlanningScene.Request()
        request.scene = self._PlanningScene(is_diff=True)
        request.scene.world.collision_objects = expected
        response = self._service(self._ApplyPlanningScene, "/apply_planning_scene", request, "PLANNING_SCENE_APPLY")
        if not getattr(response, "success", False):
            raise ContractError("PLANNING_SCENE_APPLY")
        query = self._GetPlanningScene.Request()
        query.components = self._PlanningSceneComponents(components=self._PlanningSceneComponents.WORLD_OBJECT_GEOMETRY)
        response = self._service(self._GetPlanningScene, "/get_planning_scene", query, "PLANNING_SCENE_READ")
        observed = getattr(getattr(getattr(response, "scene", None), "world", None), "collision_objects", None)
        by_id = {item.id: item for item in observed or []}
        if set(by_id) != {item.id for item in expected}:
            raise ContractError("PLANNING_SCENE_MISMATCH")
        readback = []
        for item in expected:
            actual = by_id[item.id]
            if (
                actual.header.frame_id != item.header.frame_id
                or len(actual.primitives) != 1 or len(actual.primitive_poses) != 1
                or actual.primitives[0].type != item.primitives[0].type
                or list(actual.primitives[0].dimensions) != list(item.primitives[0].dimensions)
                or any(abs(a - b) > 1e-9 for a, b in zip(
                    (actual.pose.position.x, actual.pose.position.y, actual.pose.position.z),
                    (item.pose.position.x, item.pose.position.y, item.pose.position.z),
                ))
                or any(abs(a - b) > 1e-9 for a, b in zip(
                    (actual.pose.orientation.x, actual.pose.orientation.y, actual.pose.orientation.z, actual.pose.orientation.w),
                    (item.pose.orientation.x, item.pose.orientation.y, item.pose.orientation.z, item.pose.orientation.w),
                ))
                or any(abs(a - b) > 1e-9 for a, b in zip(
                    (actual.primitive_poses[0].position.x, actual.primitive_poses[0].position.y, actual.primitive_poses[0].position.z),
                    (item.primitive_poses[0].position.x, item.primitive_poses[0].position.y, item.primitive_poses[0].position.z),
                ))
                or any(abs(a - b) > 1e-9 for a, b in zip(
                    (actual.primitive_poses[0].orientation.x, actual.primitive_poses[0].orientation.y, actual.primitive_poses[0].orientation.z, actual.primitive_poses[0].orientation.w),
                    (item.primitive_poses[0].orientation.x, item.primitive_poses[0].orientation.y, item.primitive_poses[0].orientation.z, item.primitive_poses[0].orientation.w),
                ))
            ):
                raise ContractError("PLANNING_SCENE_MISMATCH")
            readback.append({
                "id": item.id,
                "frame_id": item.header.frame_id,
                "primitive_type": int(item.primitives[0].type),
                "dimensions_m": list(item.primitives[0].dimensions),
                "pose_position_m": [item.pose.position.x, item.pose.position.y, item.pose.position.z],
                "pose_orientation_xyzw": [item.pose.orientation.x, item.pose.orientation.y, item.pose.orientation.z, item.pose.orientation.w],
                "primitive_pose_position_m": [item.primitive_poses[0].position.x, item.primitive_poses[0].position.y, item.primitive_poses[0].position.z],
                "primitive_pose_orientation_xyzw": [item.primitive_poses[0].orientation.x, item.primitive_poses[0].orientation.y, item.primitive_poses[0].orientation.z, item.primitive_poses[0].orientation.w],
            })
        return readback

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
        if step_type == "ARM":
            self._execute_goal_count += 1
        else:
            self._gripper_goal_count += 1
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
        self._load_robot_description_parameter(deadline)
        while (
            self._joint_state_received_at is None
            or self._arm_controller_received_at is None
            or self._gripper_controller_received_at is None
            or self._robot_description is None
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
        gripper_values = self._controller_values(
            self._gripper_controller_state, "ROS_GRIPPER_CONTROLLER_STATE"
        )
        if "finger_right_joint" not in gripper_values["reference"]:
            raise ContractError("ROS_GRIPPER_CONTROLLER_STATE")
        arm_speed = self._arm_controller_state.speed_scaling_factor
        if not isinstance(arm_speed, (int, float)) or not math.isfinite(arm_speed):
            raise ContractError("ROS_ARM_CONTROLLER_STATE")
        gripper_speed = self._gripper_controller_state.speed_scaling_factor
        if not isinstance(gripper_speed, (int, float)) or not math.isfinite(gripper_speed):
            raise ContractError("ROS_GRIPPER_CONTROLLER_STATE")
        return {
            "joint_positions": [by_name[name] for name in JOINT_ORDER],
            "joint_state_age_s": joint_age,
            "gripper_settings": self._gripper_settings(),
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
                "reference_position_m": gripper_values["reference"]["finger_right_joint"],
                "feedback_position_m": gripper_values["feedback"]["finger_right_joint"],
            },
        }

    def _load_robot_description_parameter(self, deadline):
        client = self._robot_description_client
        if self._robot_description is not None or client is None:
            return
        parameter_future = None
        parameter_finished = False
        while self._robot_description is None and time.monotonic() < deadline:
            if not parameter_finished and parameter_future is None:
                try:
                    if client.wait_for_services(timeout_sec=0.0):
                        parameter_future = client.get_parameters(["robot_description"])
                except (RuntimeError, TimeoutError):
                    pass
            if parameter_future is not None and parameter_future.done():
                parameter_finished = True
                try:
                    response = parameter_future.result()
                except (RuntimeError, TimeoutError):
                    response = None
                    parameter_finished = False
                parameter_future = None
                values = getattr(response, "values", None)
                candidate = (
                    values[0].string_value
                    if isinstance(values, (list, tuple))
                    and len(values) == 1
                    and values[0].type == self._parameter_string
                    and isinstance(values[0].string_value, str)
                    and values[0].string_value
                    else None
                )
                if candidate is not None:
                    try:
                        self._parse_gripper_settings(candidate)
                    except ContractError:
                        pass
                    else:
                        self._robot_description = candidate
                        return
            self._rclpy.spin_once(
                self.node,
                timeout_sec=max(0.0, min(0.05, deadline - time.monotonic())),
            )
            if self._robot_description is not None:
                try:
                    self._parse_gripper_settings(self._robot_description)
                except ContractError:
                    self._robot_description = None
                else:
                    return

    def _gripper_settings(self):
        return self._parse_gripper_settings(self._robot_description)

    @staticmethod
    def _parse_gripper_settings(robot_description):
        try:
            root = ET.fromstring(robot_description)
        except (TypeError, ET.ParseError) as exc:
            raise ContractError("ROS_GRIPPER_SETTINGS_UNVERIFIED", str(exc)) from exc
        blocks = []
        for control in root.findall(".//ros2_control"):
            if control.find("./joint[@name='finger_right_joint']") is None:
                continue
            hardware = control.find("hardware")
            if hardware is not None:
                blocks.append(hardware)
        if len(blocks) != 1:
            raise ContractError("ROS_GRIPPER_SETTINGS_UNVERIFIED")
        plugin = (blocks[0].findtext("plugin") or "").strip()
        params = {
            item.get("name"): (item.text or "").strip()
            for item in blocks[0].findall("param")
        }
        try:
            settings = {
                "hardware_plugin": plugin,
                "velocity_percent": int(params["gripper_velocity"]),
                "open_velocity_percent": int(params.get(
                    "gripper_open_velocity", params["gripper_velocity"],
                )),
                "force_percent": int(params["gripper_force"]),
                "open_force_percent": int(params.get(
                    "gripper_open_force", params["gripper_force"],
                )),
                "settle_time_ms": int(params["gripper_settle_time_ms"]),
            }
        except (KeyError, ValueError) as exc:
            raise ContractError("ROS_GRIPPER_SETTINGS_UNVERIFIED", str(exc)) from exc
        if (
            plugin not in {
                "fairino_hardware/FairinoHardwareInterface",
                "mock_components/GenericSystem",
            }
            or not 1 <= settings["velocity_percent"] <= 100
            or not 1 <= settings["open_velocity_percent"] <= 100
            or not 1 <= settings["force_percent"] <= 100
            or not 1 <= settings["open_force_percent"] <= 100
            or not 50 <= settings["settle_time_ms"] <= 10000
        ):
            raise ContractError("ROS_GRIPPER_SETTINGS_UNVERIFIED")
        return settings

    @staticmethod
    def _controller_values(message, code):
        names = list(message.joint_names)
        if not names or len(names) != len(set(names)):
            raise ContractError(code)
        result = {}
        for label, point in (("reference", message.reference), ("feedback", message.feedback)):
            positions = list(point.positions)
            if len(positions) != len(names) or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in positions
            ):
                raise ContractError(code)
            result[label] = {name: float(value) for name, value in zip(names, positions)}
        return result

    def preflight(self):
        clients = {
            "/move_action": self.move_group,
            "/execute_trajectory": self.execute_trajectory,
            "/gripper_controller/follow_joint_trajectory": self.gripper,
        }
        deadline = time.monotonic() + self.preflight_timeout_s
        server_ready = {endpoint: False for endpoint in clients}
        while True:
            try:
                actions = dict(self._get_action_names_and_types(self.node))
                topics = dict(self.node.get_topic_names_and_types())
                server_ready = {
                    endpoint: bool(client.server_is_ready())
                    for endpoint, client in clients.items()
                }
            except RuntimeError as exc:
                raise ContractError("ROS_GRAPH_FAILED", str(exc)) from exc
            joint_sample_ready = self._joint_state is not None
            graph_ready = (
                all(actions.get(endpoint) == [kind] for endpoint, kind in ACTION_TYPES.items())
                and all(server_ready.values())
                and topics.get("/joint_states") == ["sensor_msgs/msg/JointState"]
                and joint_sample_ready
            )
            if graph_ready or time.monotonic() >= deadline:
                break
            self._rclpy.spin_once(
                self.node,
                timeout_sec=max(0.0, min(0.05, deadline - time.monotonic())),
            )

        facts = {}
        fact_keys = {
            "/move_action": "move_action",
            "/execute_trajectory": "execute_trajectory",
            "/gripper_controller/follow_joint_trajectory": "gripper",
        }
        for endpoint, expected_type in ACTION_TYPES.items():
            observed = actions.get(endpoint, [])
            exact_type = observed == [expected_type]
            facts[fact_keys[endpoint]] = {
                "endpoint": endpoint,
                "type": expected_type if exact_type else "|".join(sorted(observed)),
                "ready": exact_type and server_ready[endpoint],
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
            "ready": exact_joint_type and joint_sample_ready,
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

    def _check_plan_collision(self, plan, initial_gripper):
        request_type = self._GetStateValidity.Request
        gripper = float(initial_gripper)
        samples, failures = [], []

        def check(label, joints):
            if len(joints) != len(JOINT_ORDER) or any(not math.isfinite(value) for value in joints):
                raise ContractError("COLLISION_STATE")
            request = request_type()
            request.group_name = plan["frames"]["planning_group"]
            request.robot_state = self._RobotState(
                joint_state=self._JointState(
                    name=[*JOINT_ORDER, "finger_right_joint"],
                    position=[*map(float, joints), float(gripper)],
                )
            )
            response = self._service(self._GetStateValidity, "/check_state_validity", request, "COLLISION_SERVICE")
            evidence = {"label": label, "joints_rad": list(map(float, joints)), "finger_right_joint_m": float(gripper), "valid": bool(getattr(response, "valid", False))}
            samples.append(evidence)
            if not evidence["valid"]:
                failures.append(evidence)

        check("initial", plan["initial_joint_state"])
        for step in plan["steps"]:
            if step["type"] == "GRIPPER":
                gripper = step.get("gripper_position_m", plan["gripper_requirements"]["command_position_m"])
                check(step["phase"], step["final_joint_state"])
                continue
            try:
                trajectory = self._deserialize_message(
                    base64.b64decode(step["trajectory_b64"], validate=True), self._RobotTrajectory
                ).joint_trajectory
                names, points = list(trajectory.joint_names), list(trajectory.points)
            except (ValueError, RuntimeError, TypeError) as exc:
                raise ContractError("COLLISION_TRAJECTORY", str(exc)) from exc
            if names != JOINT_ORDER or not points:
                raise ContractError("COLLISION_TRAJECTORY")
            previous, previous_time = step["start_joint_state"], -1.0
            for index, point in enumerate(points):
                current = list(point.positions)
                seconds = point.time_from_start.sec + point.time_from_start.nanosec / 1e9
                if len(current) != len(JOINT_ORDER) or seconds <= previous_time:
                    raise ContractError("COLLISION_TRAJECTORY")
                for part in range(1, 5):
                    ratio = part / 5
                    check(f"{step['phase']}:interp:{index}:{part}", [a + (b - a) * ratio for a, b in zip(previous, current)])
                check(f"{step['phase']}:point:{index}", current)
                previous, previous_time = current, seconds
        report = {"schema_version": "data_factory.collision_report.v1", "plan_digest": canonical_digest(plan), "sample_count": len(samples), "samples": samples, "failure_count": len(failures), "all_valid": not failures}
        if failures:
            raise ContractError("COLLISION_DETECTED")
        return report

    def precommit_safety(self, plan, planning_scene, before_snapshot):
        """Prove scene/readback, serialized-plan collision, and plan-only no-motion."""
        if not isinstance(plan, dict) or not isinstance(before_snapshot, dict):
            raise ContractError("PRECOMMIT_SAFETY_SCHEMA")
        expected = plan["binding_digests"]["planning_scene_digest"]
        readback = self._apply_and_readback_scene(planning_scene, expected)
        try:
            before_gripper = before_snapshot["gripper_controller"]
            initial_gripper = float(before_gripper["feedback_position_m"])
            initial_reference = float(before_gripper["reference_position_m"])
            open_step = next(step for step in plan["steps"] if step["phase"] == "GRIPPER_OPEN")
            open_target = float(open_step["gripper_position_m"])
            gripper_tolerance = float(open_step["limits"]["completion_tolerance_m"])
            if abs(initial_gripper - open_target) > gripper_tolerance or abs(initial_reference - open_target) > gripper_tolerance:
                raise ContractError("GRIPPER_INITIAL_NOT_OPEN")
            collision = self._check_plan_collision(plan, initial_gripper)
            after_snapshot = self.snapshot(plan["planning"]["max_joint_state_age_s"])
            before_joints, after_joints = before_snapshot["joint_positions"], after_snapshot["joint_positions"]
            joint_delta = max(abs(a - b) for a, b in zip(before_joints, after_joints))
            gripper_delta = abs(
                float(before_snapshot["gripper_controller"]["feedback_position_m"])
                - float(after_snapshot["gripper_controller"]["feedback_position_m"])
            )
            tolerance = float(plan["planning"]["goal_tolerances"]["joint_rad"])
        except ContractError:
            raise
        except (KeyError, TypeError, ValueError, StopIteration) as exc:
            raise ContractError("PRECOMMIT_SAFETY_SCHEMA", str(exc)) from exc
        plan_digest = canonical_digest(plan)
        readback_payload = {
            "schema_version": "data_factory.planning_scene_readback.v1",
            "run_id": plan["run_id"],
            "plan_digest": plan_digest,
            "expected_planning_scene_digest": expected,
            "objects": readback,
        }
        no_motion = {
            "schema_version": "data_factory.plan_only_no_motion.v1",
            "run_id": plan["run_id"],
            "plan_digest": plan_digest,
            "before_snapshot": before_snapshot,
            "after_snapshot": after_snapshot,
            "max_joint_delta_rad": joint_delta,
            "gripper_delta_m": gripper_delta,
            "execute_goal_count": self._execute_goal_count,
            "gripper_goal_count": self._gripper_goal_count,
        }
        if joint_delta > tolerance or gripper_delta > gripper_tolerance or self._execute_goal_count or self._gripper_goal_count:
            raise ContractError("PLAN_ONLY_MOVED_ROBOT")
        safety = {
            "schema_version": "data_factory.precommit_safety.v1",
            "run_id": plan["run_id"],
            "approved_plan_digest": plan_digest,
            "scene_binding_digest": canonical_digest(plan["scene_binding"]),
            "expected_planning_scene_digest": expected,
            "planning_scene_readback_digest": canonical_digest(readback_payload),
            "collision_report_digest": canonical_digest(collision),
            "plan_only_no_motion_digest": canonical_digest(no_motion),
            "post_reset_safe_snapshot_digest": None,
            "status": "PENDING",
        }
        return {
            "precommit_safety": safety,
            "precommit_evidence": {
                "schema_version": "data_factory.precommit_evidence.v1",
                "run_id": plan["run_id"],
                "approved_plan_digest": plan_digest,
                "scene_binding_digest": canonical_digest(plan["scene_binding"]),
                "expected_planning_scene_digest": expected,
                "planning_scene_readback": readback_payload,
                "collision_report": collision,
                "plan_only_no_motion": no_motion,
            },
        }

    def precommit_joint_transition(
        self, *, serialized_trajectory, start_joint_state, final_joint_state,
        planning_scene, planning_scene_digest, planning_group,
        max_joint_state_age_s, joint_tolerance_rad, gripper_tolerance_m,
        before_snapshot,
    ):
        """Check one exact joint-target trajectory without issuing a goal."""
        numeric = (
            [
                *start_joint_state, *final_joint_state,
                max_joint_state_age_s, joint_tolerance_rad, gripper_tolerance_m,
            ]
            if isinstance(start_joint_state, list)
            and isinstance(final_joint_state, list)
            else []
        )
        if (
            not isinstance(serialized_trajectory, bytes)
            or not serialized_trajectory
            or not isinstance(start_joint_state, list)
            or not isinstance(final_joint_state, list)
            or len(start_joint_state) != len(JOINT_ORDER)
            or len(final_joint_state) != len(JOINT_ORDER)
            or not isinstance(planning_group, str)
            or not planning_group
            or not isinstance(before_snapshot, dict)
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in numeric
            )
            or max_joint_state_age_s <= 0
            or joint_tolerance_rad <= 0
            or gripper_tolerance_m <= 0
        ):
            raise ContractError("JOINT_TRANSITION_PRECOMMIT")
        execute_goals_before = self._execute_goal_count
        gripper_goals_before = self._gripper_goal_count
        readback = self._apply_and_readback_scene(
            planning_scene, planning_scene_digest,
        )
        try:
            initial_gripper = float(
                before_snapshot["gripper_controller"]["feedback_position_m"]
            )
            plan = {
                "frames": {"planning_group": planning_group},
                "initial_joint_state": list(map(float, start_joint_state)),
                "steps": [{
                    "phase": "SAFE_POSE_PTP",
                    "type": "ARM",
                    "trajectory_b64": base64.b64encode(
                        serialized_trajectory
                    ).decode("ascii"),
                    "start_joint_state": list(map(float, start_joint_state)),
                    "final_joint_state": list(map(float, final_joint_state)),
                }],
            }
            collision = self._check_plan_collision(plan, initial_gripper)
            after_snapshot = self.snapshot(max_joint_state_age_s)
            joint_delta = max(abs(a - b) for a, b in zip(
                before_snapshot["joint_positions"],
                after_snapshot["joint_positions"],
            ))
            gripper_delta = abs(
                float(before_snapshot["gripper_controller"]["feedback_position_m"])
                - float(after_snapshot["gripper_controller"]["feedback_position_m"])
            )
        except ContractError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("JOINT_TRANSITION_PRECOMMIT", str(exc)) from exc
        if (
            joint_delta > joint_tolerance_rad
            or gripper_delta > gripper_tolerance_m
            or self._execute_goal_count != execute_goals_before
            or self._gripper_goal_count != gripper_goals_before
        ):
            raise ContractError("JOINT_TRANSITION_PLAN_ONLY_MOVED")
        evidence = {
            "schema_version": "data_factory.joint_transition_precommit.v1",
            "planning_scene_digest": planning_scene_digest,
            "planning_scene_readback": readback,
            "collision_report": collision,
            "before_snapshot": before_snapshot,
            "after_snapshot": after_snapshot,
            "max_joint_delta_rad": joint_delta,
            "gripper_delta_m": gripper_delta,
            "execute_goal_count": self._execute_goal_count,
            "gripper_goal_count": self._gripper_goal_count,
            "execute_goal_count_delta": self._execute_goal_count - execute_goals_before,
            "gripper_goal_count_delta": self._gripper_goal_count - gripper_goals_before,
        }
        evidence["evidence_digest"] = canonical_digest(evidence)
        return evidence

    def precommit_home_recovery(self, **kwargs):
        """Backward-compatible HOME evidence alias for the generic check."""
        try:
            evidence = self.precommit_joint_transition(**kwargs)
        except ContractError as exc:
            aliases = {
                "JOINT_TRANSITION_PRECOMMIT": "HOME_RECOVERY_PRECOMMIT",
                "JOINT_TRANSITION_PLAN_ONLY_MOVED":
                    "HOME_RECOVERY_PLAN_ONLY_MOVED",
            }
            if exc.code in aliases:
                raise ContractError(aliases[exc.code], str(exc)) from exc
            raise
        evidence["schema_version"] = "data_factory.home_recovery_precommit.v1"
        evidence.pop("evidence_digest")
        evidence["evidence_digest"] = canonical_digest(evidence)
        return evidence

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

    def arm_trajectory_duration_s(self, serialized):
        """Return the approved trajectory's controller time horizon."""
        try:
            trajectory = self._deserialize_message(serialized, self._RobotTrajectory)
            points = trajectory.joint_trajectory.points
            end = points[-1].time_from_start
            duration = float(end.sec) + float(end.nanosec) / 1_000_000_000
        except (IndexError, RuntimeError, TypeError, ValueError) as exc:
            raise ContractError("ROS_TRAJECTORY_DURATION", str(exc)) from exc
        if not math.isfinite(duration) or duration <= 0:
            raise ContractError("ROS_TRAJECTORY_DURATION")
        return duration

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
        request.allowed_planning_time = float(limits["planning_timeout_s"])
        request.max_velocity_scaling_factor = float(limits["velocity_scaling"])
        request.max_acceleration_scaling_factor = float(limits["acceleration_scaling"])
        request.start_state = self._RobotState(
            joint_state=self._JointState(
                name=list(JOINT_ORDER), position=list(map(float, start_joint_state))
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
                    position=float(value),
                    tolerance_above=float(tolerances["joint_rad"]),
                    tolerance_below=float(tolerances["joint_rad"]),
                    weight=1.0,
                )
                for name, value in zip(JOINT_ORDER, joint_target)
            ]
            return constraints

        pose = target["base_tool"]
        primitive = self._SolidPrimitive(
            type=self._SolidPrimitive.SPHERE,
            dimensions=[float(tolerances["position_m"])],
        )
        region_pose = self._Pose()
        region_pose.position.x, region_pose.position.y, region_pose.position.z = (
            map(float, pose["translation_m"])
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
        orientation.absolute_x_axis_tolerance = float(tolerances["orientation_rad"])
        orientation.absolute_y_axis_tolerance = float(tolerances["orientation_rad"])
        orientation.absolute_z_axis_tolerance = float(tolerances["orientation_rad"])
        orientation.weight = 1.0
        constraints.position_constraints = [position]
        constraints.orientation_constraints = [orientation]
        return constraints

    def build_gripper_goal(self, phase, position, limits):
        del phase
        goal = self._FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = ["finger_right_joint"]
        endpoint = self._JointTrajectoryPoint()
        endpoint.positions = [float(position)]
        completion_check = self._JointTrajectoryPoint()
        completion_check.positions = [float(position)]
        completion_check.time_from_start = self._duration(limits["command_duration_s"])
        goal.trajectory.points = [endpoint, completion_check]
        goal.goal_tolerance = [
            self._JointTolerance(
                name="finger_right_joint",
                position=float(limits["completion_tolerance_m"]),
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
