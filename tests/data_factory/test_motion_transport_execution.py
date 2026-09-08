import base64
import math
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from action_msgs.msg import GoalStatus
from control_msgs.action import FollowJointTrajectory
from control_msgs.msg import JointTrajectoryControllerState
from moveit_msgs.msg import MoveItErrorCodes, RobotTrajectory
from rclpy.serialization import deserialize_message, serialize_message
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectoryPoint

from tools.fr5_data_factory import ContractError
from tools.data_factory.motion.moveit_transport import RosMoveItTransport


class TestExecutionTransport(unittest.TestCase):
    def test_initial_snapshot_discovery_budget_does_not_relax_live_freshness(self):
        description = (
            "<robot><ros2_control><hardware>"
            "<plugin>fairino_hardware/FairinoHardwareInterface</plugin>"
            "<param name='gripper_velocity'>20</param>"
            "<param name='gripper_force'>20</param>"
            "<param name='gripper_settle_time_ms'>500</param>"
            "</hardware><joint name='finger_right_joint'/>"
            "</ros2_control></robot>"
        )
        controller_type = "control_msgs/msg/JointTrajectoryControllerState"
        for mode in ("late_description", "missing_description", "invalid_description"):
            with self.subTest(mode=mode):
                clock = [0.0]
                transport = object.__new__(RosMoveItTransport)
                transport.graph_timeout_s = 1.0
                transport.preflight_timeout_s = 5.0
                transport._initial_snapshot_complete = False
                transport._clock = lambda: clock[0]
                transport._robot_description = None
                transport._robot_description_client = None
                transport._joint_state_received_at = None
                transport._arm_controller_received_at = None
                transport._gripper_controller_received_at = None
                transport.node = SimpleNamespace(
                    count_publishers=lambda _: 1,
                    get_topic_names_and_types=lambda: [
                        ("/fairino5_controller/controller_state", [controller_type]),
                        ("/gripper_controller/controller_state", [controller_type]),
                    ],
                )

                def spin(*_, timeout_sec):
                    clock[0] += timeout_sec
                    joint_state = JointState(
                        name=["j1", "j2", "j3", "j4", "j5", "j6"],
                        position=[0.0] * 6,
                    )
                    joint_state.header.stamp.sec = 1234
                    joint_state.header.stamp.nanosec = 5678
                    transport._on_joint_state(joint_state)
                    for callback, name in (
                        (transport._on_arm_controller_state, "j1"),
                        (transport._on_gripper_controller_state, "finger_right_joint"),
                    ):
                        message = JointTrajectoryControllerState()
                        message.joint_names = [name]
                        message.reference.positions = [0.0]
                        message.feedback.positions = [0.0]
                        message.speed_scaling_factor = 1.0
                        callback(message)
                    if clock[0] >= 1.5 and mode != "missing_description":
                        transport._on_robot_description(SimpleNamespace(
                            data=description if mode == "late_description" else "<robot/>",
                        ))

                transport._rclpy = SimpleNamespace(spin_once=spin)
                with mock.patch(
                    "tools.data_factory.motion.moveit_transport.time.monotonic",
                    side_effect=lambda: clock[0],
                ):
                    if mode != "late_description":
                        with self.assertRaises(ContractError) as caught:
                            transport.snapshot(0.1)
                        self.assertEqual(caught.exception.code, "ROS_GRIPPER_SETTINGS_UNVERIFIED")
                        self.assertFalse(transport._initial_snapshot_complete)
                        self.assertLessEqual(clock[0], 5.0)
                        continue
                    snapshot = transport.snapshot(0.1)
                    self.assertEqual(snapshot["joint_state_stamp_ns"], 1234000005678)
                    self.assertEqual(snapshot["gripper_settings"]["velocity_percent"], 20)
                    self.assertTrue(transport._initial_snapshot_complete)
                    self.assertLess(clock[0], 1.6)  # No unconditional five-second sleep.
                    clock[0] += 1.0
                    stale_started = clock[0]
                    transport._rclpy.spin_once = (
                        lambda *_, timeout_sec: clock.__setitem__(0, clock[0] + timeout_sec)
                    )
                    with self.assertRaisesRegex(ContractError, "ROS_JOINT_STATE_STALE"):
                        transport.snapshot(0.1)
                    self.assertAlmostEqual(clock[0] - stale_started, 1.0)

    def test_cancel_race_keeps_non_cancel_terminal_result_pollable(self):
        class Future:
            def __init__(self, value):
                self.value = value

            def done(self):
                return True

            def result(self):
                return self.value

        class Handle:
            def __init__(self, result):
                self.result = result

            def cancel_goal_async(self):
                return Future(SimpleNamespace(goals_canceling=[object()]))

        for status in (
            GoalStatus.STATUS_SUCCEEDED,
            GoalStatus.STATUS_CANCELED,
            GoalStatus.STATUS_ABORTED,
        ):
            with self.subTest(status=status):
                result = Future(SimpleNamespace(status=status))
                active = SimpleNamespace(
                    phase="SAFE_POSE_PTP", type="ARM",
                    goal_handle=Handle(result), result_future=result,
                )
                transport = object.__new__(RosMoveItTransport)
                transport._active = active
                transport._execution_locked = False
                transport._goal_succeeded = GoalStatus.STATUS_SUCCEEDED
                transport._goal_canceled = GoalStatus.STATUS_CANCELED
                transport._goal_aborted = GoalStatus.STATUS_ABORTED
                transport.node = object()
                transport._rclpy = SimpleNamespace(
                    spin_until_future_complete=lambda *_args, **_kwargs: None,
                    spin_once=lambda *_args, **_kwargs: None,
                )

                if status == GoalStatus.STATUS_CANCELED:
                    self.assertIs(transport.cancel_active(1.0), active)
                    self.assertFalse(transport.owns_active_goal)
                    continue
                with self.assertRaisesRegex(
                    ContractError, "ROS_EXEC_CANCEL_NOT_CANCELED",
                ):
                    transport.cancel_active(1.0)
                self.assertTrue(transport.owns_active_goal)
                self.assertEqual(
                    transport.poll_terminal_evidence()["result_status"], status,
                )
                self.assertFalse(transport.owns_active_goal)

    def test_robot_description_parameter_retries_transient_future_failure(self):
        description = (
            "<robot><ros2_control><hardware>"
            "<plugin>fairino_hardware/FairinoHardwareInterface</plugin>"
            "<param name='gripper_velocity'>20</param>"
            "<param name='gripper_force'>50</param>"
            "<param name='gripper_settle_time_ms'>500</param>"
            "</hardware><joint name='finger_right_joint'/>"
            "</ros2_control></robot>"
        )

        class Future:
            def __init__(self, result): self.result_value = result
            def done(self): return True
            def result(self):
                if isinstance(self.result_value, Exception):
                    raise self.result_value
                return self.result_value

        response = SimpleNamespace(values=[SimpleNamespace(
            type=4, string_value=description,
        )])
        for transient_error in (RuntimeError("graph changed"), TimeoutError()):
            with self.subTest(error=type(transient_error).__name__):
                results = iter((transient_error, response))
                client = SimpleNamespace(
                    wait_for_services=lambda **_: True,
                    get_parameters=mock.Mock(
                        side_effect=lambda _: Future(next(results))
                    ),
                )
                transport = object.__new__(RosMoveItTransport)
                transport.node = object()
                transport._robot_description = None
                transport._robot_description_client = client
                transport._parameter_string = 4
                transport._rclpy = SimpleNamespace(
                    spin_once=lambda *args, **kwargs: None,
                )

                transport._load_robot_description_parameter(
                    time.monotonic() + 1.0,
                )

                self.assertEqual(transport._robot_description, description)
                self.assertEqual(client.get_parameters.call_count, 2)

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

        mapping = {"schema_version": "fr5.gripper_source_clock.v1", "incarnation": [1, 2, 3, 4],
                   "calendar_to_system_offset_s": 0., "uncertainty_s": .001,
                   "system_anchor_s": 10., "steady_anchor_s": 10., "valid_until_system_s": 20.}
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
            transport = RosMoveItTransport(node, clock=lambda: clock[0], gripper_source_clock=mapping)
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

        from builtin_interfaces.msg import Duration
        from control_msgs.msg import JointTrajectoryControllerState
        arm_state = JointTrajectoryControllerState(joint_names=["j1"], speed_scaling_factor=.5)
        gripper_state = JointTrajectoryControllerState(joint_names=["finger_right_joint"], speed_scaling_factor=1.)
        for message, position in ((arm_state, 0.), (gripper_state, .01)):
            message.header.stamp.sec = 10
            message.reference.positions = [position]
            message.feedback.positions = [position]
            message.reference.time_from_start = Duration(sec=-1, nanosec=800000000)
            message.feedback.time_from_start = Duration(sec=-1, nanosec=900000000)
        arm_state.output.positions = [-.05]
        from sensor_msgs.msg import JointState
        joint_state = JointState(name=["finger", "j6", "j5", "j4", "j3", "j2", "j1"], position=[0., 6., 5., 4., 3., 2., 1.])
        joint_state.header.stamp.sec, joint_state.header.stamp.nanosec = 10, 123
        def populate(*_, **__):
            node.callbacks["/joint_states"](deserialize_message(serialize_message(joint_state), JointState))
            node.callbacks["/fairino5_controller/controller_state"](deserialize_message(serialize_message(arm_state), JointTrajectoryControllerState))
            node.callbacks["/gripper_controller/controller_state"](deserialize_message(serialize_message(gripper_state), JointTrajectoryControllerState))
            node.callbacks["/robot_description"](SimpleNamespace(data="<robot><ros2_control><hardware><plugin>fairino_hardware/FairinoHardwareInterface</plugin><param name='gripper_velocity'>20</param><param name='gripper_force'>50</param><param name='gripper_settle_time_ms'>500</param></hardware><joint name='finger_right_joint'/></ros2_control></robot>"))
        transport._rclpy = SimpleNamespace(spin_until_future_complete=lambda *args, **kwargs: None, spin_once=populate)
        snapshot = transport.snapshot(1.0)
        self.assertEqual(snapshot["joint_positions"], [1., 2., 3., 4., 5., 6.])
        self.assertEqual(snapshot["joint_state_stamp_ns"], 10_000_000_123)
        transport._joint_state.header.stamp.nanosec = 10**9
        with self.assertRaisesRegex(ContractError, "ROS_JOINT_STATE"):
            transport.snapshot(1.)
        transport._joint_state.header.stamp.nanosec = 123
        self.assertEqual((snapshot["arm_controller"]["ready"], snapshot["arm_controller"]["speed_scaling"]), (True, 0.5))
        self.assertEqual((snapshot["gripper_controller"]["reference_position_m"], snapshot["gripper_controller"]["feedback_position_m"]), (0.01, 0.01))
        self.assertEqual(snapshot["gripper_settings"]["velocity_percent"], 20)
        retained = snapshot["arm_controller"]["sample"]
        self.assertEqual((retained["ros_stamp_ns"], retained["reference_elapsed_ns"], retained["feedback_elapsed_ns"]),
                         (10_000_000_000, -200_000_000, -100_000_000))
        self.assertEqual((retained["reference_positions"], retained["reported_output_positions"]), ([0.], [-.05]))
        self.assertEqual(snapshot["gripper_controller"]["sample"]["reported_output_positions"], [])
        # A later publication may retain an old command output in native JTC.
        # Capture its literal report; never fill an empty output from reference.
        arm_state.header.stamp.nanosec = 1
        populate()
        self.assertEqual(transport.snapshot(1.)["arm_controller"]["sample"]["reported_output_positions"], [-.05])
        self.assertEqual(retained["ros_stamp_ns"], 10_000_000_000)
        arm_state.header.stamp.nanosec = 1_000_000_000
        populate()
        with self.assertRaisesRegex(ContractError, "ROS_CONTROLLER_SAMPLE"):
            transport.snapshot(1.)
        arm_state.header.stamp.nanosec = 0
        populate()

        # Actual constructor subscription, callback and snapshot consume the wire.
        from control_msgs.msg import DynamicJointState, InterfaceValue
        from tools.data_factory.rollout.gripper_evidence import FIELDS, RESOURCE
        raw = DynamicJointState(joint_names=[RESOURCE], interface_values=[InterfaceValue(
            interface_names=list(FIELDS), values=[0.] * len(FIELDS))])
        node.callbacks["/dynamic_joint_states"](deserialize_message(serialize_message(raw), DynamicJointState))
        hardware = transport.snapshot(1.)["gripper_controller"]["hardware_execution"]
        self.assertEqual(hardware["clock_binding"], mapping)
        self.assertEqual(hardware["received_steady_s"], clock[0])
        self.assertEqual(hardware["wire"], dict.fromkeys(FIELDS, 0.))
        # Decoding conveys the record; held admission, not snapshot presence, checks validity.
        transport._robot_description = None
        transport._robot_description_client = SimpleNamespace(
            wait_for_services=lambda **_: True,
            get_parameters=lambda _: Future(SimpleNamespace(values=[SimpleNamespace(
                type=4,
                string_value="<robot><ros2_control><hardware><plugin>fairino_hardware/FairinoHardwareInterface</plugin><param name='gripper_velocity'>20</param><param name='gripper_open_velocity'>10</param><param name='gripper_force'>50</param><param name='gripper_open_force'>45</param><param name='gripper_settle_time_ms'>500</param></hardware><joint name='finger_right_joint'/></ros2_control></robot>",
            )])),
        )
        transport._rclpy.spin_once = lambda *args, **kwargs: None
        self.assertEqual(transport.snapshot(1.0)["gripper_settings"]["force_percent"], 50)
        self.assertEqual(
            transport.snapshot(1.0)["gripper_settings"]["open_force_percent"], 45,
        )
        self.assertEqual(
            transport.snapshot(1.0)["gripper_settings"]["open_velocity_percent"],
            10,
        )

        transport._robot_description = None
        transport._robot_description_client = SimpleNamespace(
            wait_for_services=lambda **_: True,
            get_parameters=lambda _: Future(None, done=False),
        )
        transport._rclpy.spin_once = lambda *args, **kwargs: node.callbacks[
            "/robot_description"
        ](SimpleNamespace(data=(
            "<robot><ros2_control><hardware>"
            "<plugin>fairino_hardware/FairinoHardwareInterface</plugin>"
            "<param name='gripper_velocity'>20</param>"
            "<param name='gripper_force'>50</param>"
            "<param name='gripper_settle_time_ms'>500</param>"
            "</hardware><joint name='finger_right_joint'/>"
            "</ros2_control></robot>"
        )))
        self.assertEqual(
            transport.snapshot(1.0)["gripper_settings"]["settle_time_ms"], 500,
        )

        clock[0] = 12.0
        transport.graph_timeout_s = 0.0
        transport._rclpy.spin_once = lambda *args, **kwargs: None
        with self.assertRaisesRegex(ContractError, "ROS_JOINT_STATE_STALE"):
            transport.snapshot(1.0)
        transport._joint_state_received_at = transport._arm_controller_received_at = transport._gripper_controller_received_at = None
        with self.assertRaisesRegex(ContractError, "ROS_JOINT_STATE_STALE"):
            transport.snapshot(1.0)


if __name__ == "__main__":
    unittest.main()

class NativeClockPreparationTest(unittest.TestCase):
    def test_only_live_preparation_sets_and_reads_back_same_incarnation(self):
        from control_msgs.msg import DynamicJointState, InterfaceValue
        from tools.data_factory.rollout.gripper_evidence import LIVE_FIELDS, RESOURCE
        from tools.data_factory.motion.moveit_transport import RosMoveItTransport
        binding = {"schema_version":"fr5.gripper_source_clock.v1", "incarnation":[1,2,3,4],
                   "calendar_to_system_offset_s":0., "uncertainty_s":.001,
                   "system_anchor_s":10., "steady_anchor_s":10., "valid_until_system_s":20.}
        for mode in ("live", "plan_only", "wrong_incarnation", "legacy", "readback_mismatch"):
            with self.subTest(mode=mode):
                t = object.__new__(RosMoveItTransport)
                t._gripper_source_clock=binding
                t._allow_clock_configuration=mode!="plan_only"
                t._native_clock_configured_age=None
                t.preflight_timeout_s=t.graph_timeout_s=.1
                t.node=SimpleNamespace(get_name=lambda: "fr5_pickup_plan_only" if mode=="plan_only" else "fr5_pickup_live")
                w=dict.fromkeys(LIVE_FIELDS,0.)
                w.update(version=1. if mode=="legacy" else 2., incarnation_0=99. if mode=="wrong_incarnation" else 1.,
                         incarnation_1=2.,incarnation_2=3.,incarnation_3=4.)
                t._gripper_hardware_state=DynamicJointState(joint_names=[RESOURCE],interface_values=[InterfaceValue(
                    interface_names=list(LIVE_FIELDS),values=[w[k] for k in LIVE_FIELDS])])
                t._gripper_hardware_received_at=10.
                configured=[]
                def set_parameters(parameters):
                    configured.append(parameters[0].value)
                    return SimpleNamespace(result=SimpleNamespace(successful=True))
                client=SimpleNamespace(wait_for_services=lambda **_:True, set_parameters_atomically=set_parameters,
                    get_parameters=lambda _:SimpleNamespace(values=[SimpleNamespace(double_array_value=
                        [] if mode=="readback_mismatch" else configured[-1])]))
                t._AsyncParameterClient=lambda node,name:client
                t._wait=lambda result,*_:result
                if mode in ("wrong_incarnation","legacy","readback_mismatch"):
                    with self.assertRaises(ContractError):t._prepare_native_clock(.3)
                else:t._prepare_native_clock(.3)
                self.assertEqual(len(configured),int(mode in ("live","readback_mismatch")))
                if mode=="live":
                    t._prepare_native_clock(.3)
                    self.assertEqual(len(configured),1)
