"""Actual transport serializers and retained-goal state, using synthetic clients only."""
import base64
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from action_msgs.msg import GoalStatus
from builtin_interfaces.msg import Duration
from control_msgs.action import FollowJointTrajectory
from moveit_msgs.action import ExecuteTrajectory
from moveit_msgs.msg import RobotTrajectory, RobotState, MoveItErrorCodes
from moveit_msgs.srv import GetStateValidity
from sensor_msgs.msg import JointState
from rclpy.serialization import serialize_message, deserialize_message
from trajectory_msgs.msg import JointTrajectoryPoint

from tools.fr5_data_factory import ContractError
from tools.data_factory.motion.moveit_transport import RosMoveItTransport
from tests.data_factory.rollout.test_finite_plan import proposal, JOINTS, INITIAL, ACTION


class Future:
    def __init__(self, value, done=True):
        self.value, self.complete = value, done
    def done(self):
        return self.complete
    def result(self):
        return self.value


class Handle:
    accepted = True
    def __init__(self):
        result = ExecuteTrajectory.Result()
        result.error_code.val = MoveItErrorCodes.SUCCESS
        self.result = Future(SimpleNamespace(status=GoalStatus.STATUS_SUCCEEDED, result=result), done=False)
        self.cancels = 0
    def get_result_async(self):
        return self.result
    def cancel_goal_async(self):
        self.cancels += 1
        self.result.complete = True
        self.result.value.status = GoalStatus.STATUS_CANCELED
        return Future(SimpleNamespace(goals_canceling=[object()]))


class LearnedTransportTest(unittest.TestCase):
    def transport(self):
        t = object.__new__(RosMoveItTransport)
        t._RobotTrajectory, t._JointTrajectoryPoint, t._Duration = RobotTrajectory, JointTrajectoryPoint, Duration
        t._ExecuteTrajectory, t._FollowJointTrajectory = ExecuteTrajectory, FollowJointTrajectory
        t._serialize_message, t._deserialize_message = serialize_message, deserialize_message
        t._GetStateValidity, t._RobotState, t._JointState = GetStateValidity, RobotState, JointState
        t._goal_succeeded, t._goal_canceled, t._goal_aborted = 4, 5, 6
        t._moveit_success = MoveItErrorCodes.SUCCESS
        t._active, t._execution_locked = None, False
        t._execute_goal_count = t._gripper_goal_count = 0
        t._clock, t.graph_timeout_s = lambda: 10., .1
        t.node = object()
        t._rclpy = SimpleNamespace(spin_until_future_complete=lambda *a, **kw: None, spin_once=lambda *a, **kw: None)
        handle = Handle()
        t.execute_trajectory = mock.Mock()
        t.execute_trajectory.send_goal_async.return_value = Future(handle)
        t.gripper = mock.Mock()
        return t, handle

    def step(self, t):
        p = proposal()
        data = t.build_learned_trajectory(p)
        return {'phase': 'LEARNED_CHUNK', 'type': 'ARM', 'trajectory_b64': base64.b64encode(data).decode(),
                'limits': {'execution_timeout_s': 3.}, 'learned_proposal': p,
                'start_joint_state': INITIAL[:6], 'final_joint_state': ACTION[:6]}

    def test_full_7d_serialization_uses_one_execute_goal_and_one_cancel_owner(self):
        t, handle = self.transport()
        step = self.step(t)
        trajectory = deserialize_message(base64.b64decode(step['trajectory_b64']), RobotTrajectory).joint_trajectory
        self.assertEqual(list(trajectory.joint_names), JOINTS)
        self.assertEqual([list(point.positions) for point in trajectory.points], [INITIAL, ACTION])
        self.assertEqual(trajectory.points[-1].time_from_start.nanosec, 100_000_000)
        with mock.patch('tools.data_factory.motion.moveit_transport.time.time', return_value=10.):
            t.start_phase(step)
            self.assertTrue(t.owns_active_goal)
            with self.assertRaisesRegex(ContractError, 'ROS_EXEC_ACTIVE'):
                t.start_phase(step)
        t.cancel_active(.1)
        self.assertEqual(handle.cancels, 1)
        self.assertEqual(t.execute_trajectory.send_goal_async.call_count, 1)
        t.gripper.send_goal_async.assert_not_called()
        self.assertFalse(t.owns_active_goal)

    def test_serialized_trajectory_cannot_disagree_with_full7d_proposal(self):
        t, _ = self.transport()
        step = self.step(t)
        trajectory = deserialize_message(base64.b64decode(step["trajectory_b64"]), RobotTrajectory)
        trajectory.joint_trajectory.points[-1].positions[-1] = .02
        step["trajectory_b64"] = base64.b64encode(serialize_message(trajectory)).decode()
        with self.assertRaisesRegex(ContractError, "LEARNED_SERIALIZED_ACTION_MISMATCH"):
            t.start_phase(step)
        t.execute_trajectory.send_goal_async.assert_not_called()

    def test_freshness_is_checked_after_goal_deserialization_at_send(self):
        t, _ = self.transport()
        step = self.step(t)
        with mock.patch('tools.data_factory.motion.moveit_transport.time.time', return_value=10.4):
            with self.assertRaisesRegex(ContractError, 'LEARNED_STALE_OBSERVATION'):
                t.start_phase(step)
        t.execute_trajectory.send_goal_async.assert_not_called()

    def test_cancel_during_late_goal_acceptance_retains_and_settles_that_goal(self):
        t, handle = self.transport()
        cancel = threading.Event()
        def send(_):
            cancel.set()
            return Future(handle)
        t.execute_trajectory.send_goal_async.side_effect = send
        with mock.patch('tools.data_factory.motion.moveit_transport.time.time', return_value=10.):
            with self.assertRaisesRegex(ContractError, 'ROS_EXEC_CANCELLED'):
                t.start_phase(self.step(t), cancel_event=cancel, cancel_timeout_s=.1)
        self.assertEqual(handle.cancels, 1)
        self.assertFalse(t.owns_active_goal)
        self.assertEqual(t.execute_trajectory.send_goal_async.call_count, 1)

    def test_collision_samples_include_interpolated_gripper_and_all_robot_links(self):
        t, _ = self.transport()
        step = self.step(t)
        requests = []
        def service(_kind, _endpoint, request, _code):
            requests.append(request)
            return SimpleNamespace(valid=True)
        t._service = service
        plan = {'initial_joint_state': INITIAL[:6], 'steps': [step], 'learned_proposal': step['learned_proposal'],
                'frames': {'planning_group': 'fairino5_v6_group'}}
        report = t._check_plan_collision(plan, INITIAL[-1])
        self.assertTrue(report['all_valid'])
        self.assertTrue(all(request.group_name == '' for request in requests))
        values = [request.robot_state.joint_state.position[-1] for request in requests]
        self.assertEqual(values[-1], ACTION[-1])
        self.assertTrue(any(INITIAL[-1] < v < ACTION[-1] for v in values))


if __name__ == '__main__':
    unittest.main()
