from __future__ import annotations

import copy
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.data_factory.motion.home_recovery import (
    transition_to_start,
    transition_to_start_live,
)
from tools.data_factory.motion.moveit_transport import RosMoveItTransport
from tools.fr5_data_factory import ContractError, canonical_digest


ROOT = Path(__file__).resolve().parents[2]
MOTION = json.loads((
    ROOT / "config/data_factory/motion_qualifications/fr5-place-a-wood-cube-r001.json"
).read_text(encoding="utf-8"))
JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]
TARGET = dict(zip(JOINTS, MOTION["qualified_safe_joint_positions_rad"]))


def qualification(**changes):
    value = {
        "schema_version": "data_factory.robot_start_pose_qualification.v1",
        "source": "QUALIFICATION_ARTIFACT",
        "robot_system_id": MOTION["robot_system_id"],
        "robot_start_pose_id": "fr5-lab-a-start-r001",
        "joint_order": JOINTS,
        "target_rad": TARGET,
        "tolerance_rad": {name: 0.01 for name in JOINTS},
        "home_candidate_digest": MOTION["home_candidate_digest"],
        "qualification_status": "QUALIFIED",
        "safety_status": "SAFE_FOR_MOTION",
    }
    value.update(changes)
    value["qualification_digest"] = canonical_digest(value)
    return value


def snapshot(joints, *, gripper=0.021):
    return {
        "joint_positions": list(joints),
        "arm_controller": {"ready": True},
        "gripper_controller": {
            "ready": True,
            "reference_position_m": gripper,
            "feedback_position_m": gripper,
        },
    }


class FakeTransport:
    def __init__(self, snapshots, *, graph=True, plan=True, precommit_error=None):
        self.snapshots = list(snapshots)
        self.graph = graph
        self.plan = plan
        self.precommit_error = precommit_error
        self.started = []
        self.planned = 0
        self.precommits = 0

    def preflight(self):
        ready = {"ready": self.graph}
        return {
            "move_action": ready, "execute_trajectory": ready,
            "gripper": ready, "joint_states": ready,
            "joint_order": JOINTS,
        }

    def snapshot(self, _max_age):
        return self.snapshots.pop(0)

    def plan_arm(self, _phase, _target, joint_target, *_args):
        self.planned += 1
        return {
            "terminal_status": "SUCCEEDED" if self.plan else "FAILED",
            "moveit_success": self.plan,
            "serialized_trajectory": b"start",
            "final_joint_state": list(joint_target),
        }

    def precommit_joint_transition(self, **_kwargs):
        self.precommits += 1
        if self.precommit_error:
            raise ContractError(self.precommit_error)
        return {"evidence_digest": canonical_digest(["joint-transition"])}

    def start_phase(self, step, **_kwargs):
        self.started.append(step["phase"])

    def poll_active(self):
        return object()

    def cancel_active(self, _timeout):
        raise AssertionError("no cancel expected")

    def build_gripper_goal(self, *_args):
        raise AssertionError("start transition must not send a gripper goal")


class StartTransitionTests(unittest.TestCase):
    @staticmethod
    def transport(client):
        value = object.__new__(RosMoveItTransport)
        value._active = None
        value._execution_locked = False
        value._execute_goal_count = 0
        value._gripper_goal_count = 0
        value.graph_timeout_s = 0.01
        value._clock = lambda: 0.0
        value._rclpy = SimpleNamespace(
            spin_until_future_complete=lambda *args, **kwargs: None,
            spin_once=lambda *args, **kwargs: None,
        )
        value.node = object()
        value._goal_succeeded = 4
        value._goal_canceled = 5
        value._goal_aborted = 6
        value._compiled_execution_goal = lambda _step: (
            "SAFE_POSE_PTP", "ARM", object(), client, 1.0,
        )
        return value

    def test_transport_owns_cancellation_before_dispatch_and_during_acceptance(self):
        class Future:
            def __init__(self, value=None, *, done=True, on_result=None):
                self.value = value
                self.complete = done
                self.on_result = on_result

            def done(self):
                return self.complete

            def result(self):
                if self.on_result is not None:
                    self.on_result()
                return self.value

        class Handle:
            accepted = True

            def __init__(self, event, *, canceling=True):
                self.event = event
                self.canceling = canceling
                self.cancel_count = 0
                self.result_future = Future(SimpleNamespace(status=5))

            def get_result_async(self):
                return self.result_future

            def cancel_goal_async(self):
                self.cancel_count += 1
                return Future(SimpleNamespace(
                    goals_canceling=[object()] if self.canceling else [],
                ))

        event = threading.Event()
        client = SimpleNamespace(send_goal_async=mock.Mock())
        transport = self.transport(client)
        event.set()
        with self.assertRaises(ContractError) as caught:
            transport.start_phase({}, cancel_event=event, cancel_timeout_s=0.1)
        self.assertEqual(caught.exception.code, "ROS_EXEC_CANCELLED")
        client.send_goal_async.assert_not_called()
        self.assertEqual(transport._execute_goal_count, 0)
        self.assertIsNone(transport._active)

        for canceling, expected in (
            (True, "ROS_EXEC_CANCELLED"),
            (False, "ROS_EXEC_CANCEL_UNCERTAIN"),
        ):
            with self.subTest(canceling=canceling):
                event = threading.Event()
                handle = Handle(event, canceling=canceling)
                goal_future = Future(handle, on_result=event.set)
                client = SimpleNamespace(
                    send_goal_async=mock.Mock(),
                )
                transport = self.transport(client)

                def dispatch(_goal):
                    self.assertIsNotNone(transport._active)
                    self.assertTrue(transport._execution_locked)
                    return goal_future

                client.send_goal_async.side_effect = dispatch
                with self.assertRaises(ContractError) as caught:
                    transport.start_phase(
                        {}, cancel_event=event, cancel_timeout_s=0.1,
                    )
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual((client.send_goal_async.call_count, handle.cancel_count), (1, 1))
                self.assertTrue(transport._execution_locked)
                if canceling:
                    self.assertIsNone(transport._active)
                else:
                    self.assertIsNotNone(transport._active)
                with self.assertRaisesRegex(ContractError, "ROS_EXEC_ACTIVE"):
                    transport.start_phase(
                        {}, cancel_event=event, cancel_timeout_s=0.1,
                    )
                self.assertEqual(client.send_goal_async.call_count, 1)
                if not canceling:
                    terminal = transport.poll_terminal_evidence()
                    self.assertEqual(
                        (terminal["terminal"], terminal["goal_acceptance"],
                         terminal["result_status"]),
                        (True, "ACCEPTED", 5),
                    )
                    self.assertFalse(transport.owns_active_goal)
                    self.assertEqual(handle.cancel_count, 1)

        event = threading.Event()
        pending = Future(done=False)
        client = SimpleNamespace(send_goal_async=mock.Mock(return_value=pending))
        transport = self.transport(client)
        transport._rclpy.spin_until_future_complete = (
            lambda *args, **kwargs: event.set()
        )
        with self.assertRaises(ContractError) as caught:
            transport.start_phase({}, cancel_event=event, cancel_timeout_s=0.1)
        self.assertEqual(caught.exception.code, "ROS_EXEC_CANCEL_UNCERTAIN")
        self.assertIs(transport._active.goal_future, pending)
        with self.assertRaisesRegex(ContractError, "ROS_EXEC_ACTIVE"):
            transport.start_phase({}, cancel_event=event, cancel_timeout_s=0.1)
        self.assertEqual(client.send_goal_async.call_count, 1)

    def test_live_cancel_uncertainty_retains_one_owner_until_rejection(self):
        class Future:
            def __init__(self):
                self.complete = False
                self.value = None

            def done(self):
                return self.complete

            def result(self):
                return self.value

        class OwnerEvent(threading.Event):
            def __init__(self):
                super().__init__()
                self.claims = 0
                self.finishes = []
                self.owner = None

            def claim_start_transition(self):
                self.claims += 1

            def finish_start_transition(self, code):
                self.finishes.append(code)

            def retain_start_transition_owner(self, owner):
                self.owner = owner

        pending = Future()
        client = SimpleNamespace(
            send_goal_async=mock.Mock(return_value=pending),
        )
        transport = self.transport(client)
        event = OwnerEvent()
        transport.node = node = mock.Mock()
        transport._rclpy.spin_until_future_complete = (
            lambda *_args, **_kwargs: event.set()
        )
        transport.preflight = lambda: {
            name: {"ready": True}
            for name in (
                "move_action", "execute_trajectory", "gripper", "joint_states",
            )
        } | {"joint_order": JOINTS}
        start = [item + 0.2 for item in TARGET.values()]
        transport.snapshot = mock.Mock(side_effect=[snapshot(start), snapshot(start)])
        transport.plan_arm = lambda _phase, _target, joint_target, *_args: {
            "terminal_status": "SUCCEEDED",
            "moveit_success": True,
            "serialized_trajectory": b"start",
            "final_joint_state": list(joint_target),
        }
        transport.precommit_joint_transition = lambda **_kwargs: {
            "evidence_digest": canonical_digest(["joint-transition"]),
        }
        transport.cancel_active = mock.Mock(wraps=transport.cancel_active)
        context = [False]

        def init():
            context[0] = True

        def shutdown():
            context[0] = False

        with (
            mock.patch("rclpy.ok", side_effect=lambda: context[0]),
            mock.patch("rclpy.init", side_effect=init) as initialize,
            mock.patch("rclpy.create_node", return_value=node),
            mock.patch("rclpy.shutdown", side_effect=shutdown) as stop,
            mock.patch(
                "tools.data_factory.motion.moveit_transport.RosMoveItTransport",
                return_value=transport,
            ),
        ):
            with self.assertRaisesRegex(
                ContractError, "START_TRANSITION_CANCEL_UNCERTAIN",
            ):
                transition_to_start_live(
                    motion_qualification=MOTION,
                    robot_start_pose_qualification=qualification(),
                    cancel_event=event,
                )
            self.assertEqual((event.claims, event.finishes), (
                1, ["START_TRANSITION_CANCEL_UNCERTAIN"],
            ))
            self.assertTrue(transport.owns_active_goal)
            self.assertIsNotNone(event.owner)
            self.assertEqual(
                (client.send_goal_async.call_count,
                 transport.cancel_active.call_count),
                (1, 1),
            )
            node.destroy_node.assert_not_called()
            stop.assert_not_called()
            self.assertIsNone(event.owner())
            node.destroy_node.assert_not_called()

            pending.value = SimpleNamespace(accepted=False)
            pending.complete = True
            evidence = event.owner()
            self.assertEqual(
                (evidence["terminal"], evidence["goal_acceptance"]),
                (True, "REJECTED"),
            )
            self.assertFalse(transport.owns_active_goal)
            self.assertEqual(
                (client.send_goal_async.call_count,
                 transport.cancel_active.call_count),
                (1, 1),
            )
            initialize.assert_called_once_with()
            node.destroy_node.assert_called_once_with()
            stop.assert_called_once_with()

    def test_cancel_stops_start_transition_before_or_during_motion(self):
        cancelled = threading.Event()
        cancelled.set()
        untouched = FakeTransport([])
        with self.assertRaisesRegex(ContractError, "START_TRANSITION_CANCELLED"):
            transition_to_start(
                untouched,
                motion_qualification=MOTION,
                robot_start_pose_qualification=qualification(),
                cancel_event=cancelled,
            )
        self.assertEqual((untouched.planned, untouched.precommits, untouched.started), (0, 0, []))

        class WaitingTransport(FakeTransport):
            def __init__(self, snapshots):
                super().__init__(snapshots)
                self.cancelled = 0

            def poll_active(self):
                cancelled.set()
                return None

            def cancel_active(self, _timeout):
                self.cancelled += 1
                return {"state": "CANCELLED"}

        cancelled.clear()
        target = list(TARGET.values())
        start = [item + 0.2 for item in target]
        waiting = WaitingTransport([snapshot(start), snapshot(start)])
        with self.assertRaisesRegex(ContractError, "START_TRANSITION_CANCELLED"):
            transition_to_start(
                waiting,
                motion_qualification=MOTION,
                robot_start_pose_qualification=qualification(),
                cancel_event=cancelled,
                sleep_call=lambda _seconds: None,
            )
        self.assertEqual((waiting.started, waiting.cancelled), (["SAFE_POSE_PTP"], 1))

    def test_qualified_transition_or_noop_and_fail_closed_inputs(self):
        target = list(TARGET.values())
        start = [item + 0.2 for item in target]
        transport = FakeTransport([
            snapshot(start), snapshot(start), snapshot(target),
        ])
        result = transition_to_start(
            transport,
            motion_qualification=MOTION,
            robot_start_pose_qualification=qualification(),
            sleep_call=lambda _seconds: None,
        )
        self.assertEqual(
            (result["schema_version"], result["status"]),
            ("data_factory.start_transition_receipt.v1", "AT_START"),
        )
        self.assertEqual((result["arm_goal_count"], result["gripper_goal_count"]), (1, 0))
        self.assertEqual((transport.planned, transport.precommits, transport.started), (1, 1, ["SAFE_POSE_PTP"]))
        self.assertEqual(result["robot_start_pose_qualification_digest"], qualification()["qualification_digest"])
        self.assertEqual((result["authority"], result["training_authority"]), ("NO_EXECUTION_AUTHORITY", False))
        self.assertEqual(
            result["receipt_digest"],
            canonical_digest({key: item for key, item in result.items() if key != "receipt_digest"}),
        )

        noop = FakeTransport([snapshot(target)])
        receipt = transition_to_start(
            noop,
            motion_qualification=MOTION,
            robot_start_pose_qualification=qualification(),
        )
        self.assertEqual((receipt["status"], receipt["arm_goal_count"]), ("ALREADY_AT_START", 0))
        self.assertEqual((noop.planned, noop.precommits, noop.started), (0, 0, []))

        wrong = qualification(home_candidate_digest=canonical_digest("other-home"))
        untouched = FakeTransport([])
        with self.assertRaisesRegex(ContractError, "START_TRANSITION_QUALIFICATION"):
            transition_to_start(
                untouched,
                motion_qualification=MOTION,
                robot_start_pose_qualification=wrong,
            )
        self.assertEqual((untouched.planned, untouched.precommits, untouched.started), (0, 0, []))

        closed = FakeTransport([snapshot(start, gripper=0.012)])
        with self.assertRaisesRegex(ContractError, "START_TRANSITION_GRIPPER_NOT_OPEN"):
            transition_to_start(
                closed,
                motion_qualification=copy.deepcopy(MOTION),
                robot_start_pose_qualification=qualification(),
            )
        self.assertEqual((closed.planned, closed.precommits, closed.started), (0, 0, []))

        drifted = [item + 0.02 for item in start]
        terminal_miss = [item + 0.02 for item in target]
        failures = (
            (FakeTransport([], graph=False), "START_TRANSITION_GRAPH", 0),
            (FakeTransport([snapshot(start)], plan=False), "START_TRANSITION_PLAN", 0),
            (
                FakeTransport(
                    [snapshot(start)], precommit_error="COLLISION_DETECTED",
                ),
                "COLLISION_DETECTED", 0,
            ),
            (
                FakeTransport([snapshot(start), snapshot(drifted)]),
                "START_TRANSITION_START_CHANGED", 0,
            ),
            (
                FakeTransport([
                    snapshot(start), snapshot(start), snapshot(terminal_miss),
                ]),
                "START_TRANSITION_FINAL_MISMATCH", 1,
            ),
        )
        for failed, code, arm_goals in failures:
            with self.subTest(code=code), self.assertRaisesRegex(ContractError, code):
                transition_to_start(
                    failed,
                    motion_qualification=MOTION,
                    robot_start_pose_qualification=qualification(),
                    sleep_call=lambda _seconds: None,
                )
            self.assertEqual(len(failed.started), arm_goals)


if __name__ == "__main__":
    unittest.main()
