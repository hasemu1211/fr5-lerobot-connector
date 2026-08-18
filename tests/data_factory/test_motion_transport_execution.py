import base64
import math
import unittest
from types import SimpleNamespace
from unittest import mock

from action_msgs.msg import GoalStatus
from control_msgs.action import FollowJointTrajectory
from moveit_msgs.msg import MoveItErrorCodes, RobotTrajectory
from rclpy.serialization import deserialize_message, serialize_message
from trajectory_msgs.msg import JointTrajectoryPoint

from tools.fr5_data_factory import ContractError
from tools.data_factory.motion.moveit_transport import RosMoveItTransport


class TestExecutionTransport(unittest.TestCase):
    def test_execute_cancel_and_snapshot_contract(self):
        class Future:
            def __init__(self, value, done=True): self.value, self.complete = value, done
            def done(self): return self.complete
            def result(self): return self.value

        class Handle:
            accepted = True
            def __init__(self, result, canceling=True): self.result, self.canceled, self.canceling = result, 0, canceling
            def get_result_async(self): return self.result
            def cancel_goal_async(self):
                self.canceled += 1
                return Future(SimpleNamespace(goals_canceling=[object()] if self.canceling else []))

        class Client:
            def __init__(self): self.goals, self.handle, self.send_done = [], None, True
            def wait_for_server(self, timeout_sec): return True
            def send_goal_async(self, goal): self.goals.append(goal); return Future(self.handle, self.send_done)

        class Node:
            def __init__(self): self.callbacks = {}
            def create_subscription(self, kind, topic, callback, depth):
                self.callbacks[topic] = callback
                return object()
            def count_publishers(self, topic): return 1
            def get_topic_names_and_types(self):
                kind = ["control_msgs/msg/JointTrajectoryControllerState"]
                return [("/fairino5_controller/controller_state", kind), ("/gripper_controller/controller_state", kind)]

        clock = [10.0]
        clients = {}
        def client_factory(node, kind, topic):
            del node, kind
            clients[topic] = Client()
            return clients[topic]
        node = Node()
        with mock.patch("rclpy.action.ActionClient", side_effect=client_factory):
            transport = RosMoveItTransport(node, clock=lambda: clock[0])
        transport._rclpy = SimpleNamespace(
            spin_until_future_complete=lambda *args, **kwargs: None,
            spin_once=lambda *args, **kwargs: None,
        )

        trajectory = RobotTrajectory()
        trajectory.joint_trajectory.joint_names = ["j1"]
        trajectory.joint_trajectory.points = [JointTrajectoryPoint(positions=[1.0])]
        arm = {"phase": "ARM", "type": "ARM", "trajectory_b64": base64.b64encode(serialize_message(trajectory)).decode(), "limits": {"execution_timeout_s": 2.0}}
        arm_result = transport._ExecuteTrajectory.Result()
        arm_result.error_code.val = MoveItErrorCodes.SUCCESS
        clients["/execute_trajectory"].handle = Handle(Future(SimpleNamespace(status=GoalStatus.STATUS_SUCCEEDED, result=arm_result)))
        active = transport.start_phase(arm)
        sent = clients["/execute_trajectory"].goals[-1]
        self.assertEqual(deserialize_message(serialize_message(sent.trajectory), RobotTrajectory).joint_trajectory.joint_names, ["j1"])
        self.assertEqual(transport.poll_active(), active)

        gripper_goal = transport._FollowJointTrajectory.Goal()
        gripper = {"phase": "GRIPPER", "type": "GRIPPER", "trajectory_b64": base64.b64encode(serialize_message(gripper_goal)).decode(), "limits": {"execution_timeout_s": 2.0}}
        canceled_result = Future(SimpleNamespace(status=GoalStatus.STATUS_CANCELED, result=SimpleNamespace(error_code=0)))
        clients["/gripper_controller/follow_joint_trajectory"].handle = Handle(canceled_result)
        active = transport.start_phase(gripper)
        self.assertEqual(deserialize_message(serialize_message(clients["/gripper_controller/follow_joint_trajectory"].goals[-1]), FollowJointTrajectory.Goal).trajectory.joint_names, [])
        self.assertEqual(transport.cancel_active(1.0), active)
        self.assertIsNone(transport._active)
        with self.assertRaises(ContractError) as caught:
            transport.start_phase(gripper)
        self.assertEqual(caught.exception.code, "ROS_EXEC_ACTIVE")

        with mock.patch("rclpy.action.ActionClient", side_effect=client_factory):
            transport = RosMoveItTransport(node, clock=lambda: clock[0])
        transport._rclpy = SimpleNamespace(
            spin_until_future_complete=lambda *args, **kwargs: None,
            spin_once=lambda *args, **kwargs: None,
        )
        with self.assertRaises(ContractError) as caught:
            transport.start_phase({**arm, "trajectory_b64": "?"})
        self.assertEqual(caught.exception.code, "ROS_EXEC_B64")

        clients["/execute_trajectory"].handle = Handle(Future(SimpleNamespace()))
        clients["/execute_trajectory"].send_done = False
        with self.assertRaises(ContractError) as caught:
            transport.start_phase(arm)
        self.assertEqual(caught.exception.code, "ROS_EXEC_GOAL_TIMEOUT")
        goal_count = len(clients["/execute_trajectory"].goals)
        with self.assertRaises(ContractError) as caught:
            transport.start_phase(arm)
        self.assertEqual(caught.exception.code, "ROS_EXEC_ACTIVE")
        self.assertEqual(len(clients["/execute_trajectory"].goals), goal_count)

        with mock.patch("rclpy.action.ActionClient", side_effect=client_factory):
            transport = RosMoveItTransport(node, clock=lambda: clock[0])
        transport._rclpy = SimpleNamespace(
            spin_until_future_complete=lambda *args, **kwargs: None,
            spin_once=lambda *args, **kwargs: None,
        )
        clients["/execute_trajectory"].handle = None
        with self.assertRaises(ContractError) as caught:
            transport.start_phase(arm)
        self.assertEqual(caught.exception.code, "ROS_EXEC_GOAL_RESPONSE_INVALID")
        goal_count = len(clients["/execute_trajectory"].goals)
        with self.assertRaises(ContractError) as caught:
            transport.start_phase(arm)
        self.assertEqual(caught.exception.code, "ROS_EXEC_ACTIVE")
        self.assertEqual(len(clients["/execute_trajectory"].goals), goal_count)

        with mock.patch("rclpy.action.ActionClient", side_effect=client_factory):
            transport = RosMoveItTransport(node, clock=lambda: clock[0])
        transport._rclpy = SimpleNamespace(
            spin_until_future_complete=lambda *args, **kwargs: None,
            spin_once=lambda *args, **kwargs: None,
        )
        clients["/gripper_controller/follow_joint_trajectory"].handle = Handle(Future(SimpleNamespace(status=GoalStatus.STATUS_ABORTED, result=SimpleNamespace(error_code=0))), canceling=False)
        transport.start_phase(gripper)
        with self.assertRaises(ContractError) as caught:
            transport.cancel_active(1.0)
        self.assertEqual(caught.exception.code, "ROS_EXEC_CANCEL_REJECTED")
        self.assertIsNotNone(transport._active)
        with self.assertRaises(ContractError):
            transport.poll_active()
        with self.assertRaises(ContractError) as caught:
            transport.start_phase(gripper)
        self.assertEqual(caught.exception.code, "ROS_EXEC_ACTIVE")

        node.callbacks["/joint_states"](SimpleNamespace(name=["finger", "j6", "j5", "j4", "j3", "j2", "j1"], position=[0., 6., 5., 4., 3., 2., 1.]))
        point = SimpleNamespace(positions=[0.])
        node.callbacks["/fairino5_controller/controller_state"](SimpleNamespace(joint_names=["j1"], reference=point, feedback=point, speed_scaling_factor=0.5))
        node.callbacks["/gripper_controller/controller_state"](SimpleNamespace(joint_names=["finger"], reference=point, feedback=point, speed_scaling_factor=1.0))
        snapshot = transport.snapshot(1.0)
        self.assertEqual(snapshot["joint_positions"], [1., 2., 3., 4., 5., 6.])
        self.assertEqual((snapshot["arm_controller"]["ready"], snapshot["arm_controller"]["speed_scaling"]), (True, 0.5))
        clock[0] = 12.0
        with self.assertRaisesRegex(ContractError, "ROS_JOINT_STATE_STALE"):
            transport.snapshot(1.0)


if __name__ == "__main__":
    unittest.main()
