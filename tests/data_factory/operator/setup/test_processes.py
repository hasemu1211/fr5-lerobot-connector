from __future__ import annotations

import subprocess
import threading
import unittest

from tools.data_factory.operator.setup.processes import OperatorStack
from tools.fr5_data_factory import ContractError


def facts(robot="MISSING", controller="MISSING", gripper="MISSING", camera="MISSING"):
    owner_by_state = {
        "READY": "ros-control", "SETUP_REQUIRED": "ros-control",
        "MISSING": None, "AMBIGUOUS": None,
    }
    return {
        "robot": {"state": robot, "owner": owner_by_state[robot]},
        "controller": {"state": controller, "owner": owner_by_state[controller]},
        "gripper": {"state": gripper, "owner": owner_by_state[gripper]},
        "camera": {
            "state": camera,
            "owner": "camera-up" if camera in ("READY", "SETUP_REQUIRED") else None,
        },
    }


COMMANDS = {
    "robot_stack": {
        "argv": ["ros2", "launch", "fairino5_v6_moveit2_config", "real_robot.launch.py"],
        "owner": "ros-control",
        "provides": ["robot", "controller", "gripper"],
    },
    "camera_up": {
        "argv": ["scripts/start_realsense_camera.sh"],
        "owner": "camera-up",
        "provides": ["camera"],
    },
}


class FakeProcess:
    def __init__(self, returncode=None, terminate_timeouts=0):
        self.returncode = returncode
        self.terminate_timeouts = terminate_timeouts
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if not self.terminate_timeouts:
            self.returncode = -15

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
        if self.terminate_timeouts:
            self.terminate_timeouts -= 1
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


class FakeFactory:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class OperatorStackTests(unittest.TestCase):
    def test_ambiguity_blocks_before_process_creation(self):
        observed = facts("READY", "READY", "AMBIGUOUS", "READY")
        factory = FakeFactory()
        stack = OperatorStack(COMMANDS, discover=lambda: observed, process_factory=factory)
        with self.assertRaises(ContractError) as caught:
            stack.ensure()
        self.assertEqual(caught.exception.code, "OPERATOR_STACK_AMBIGUOUS")
        self.assertEqual(factory.calls, [])

    def test_invalid_or_detaching_command_is_rejected(self):
        invalid = dict(COMMANDS)
        invalid["camera_up"] = {
            "argv": "scripts/start_realsense_camera.sh",
            "owner": "camera-up",
            "provides": ["camera"],
        }
        with self.assertRaises(ContractError) as caught:
            OperatorStack(invalid, discover=facts)
        self.assertEqual(caught.exception.code, "OPERATOR_STACK_COMMAND")
        with self.assertRaises(ContractError) as caught:
            OperatorStack({
                "camera_up": {
                    "argv": ["systemctl", "start", "camera"],
                    "owner": "camera-up",
                    "provides": ["camera"],
                },
            }, discover=facts)
        self.assertEqual(caught.exception.code, "OPERATOR_STACK_FOREGROUND")

    def test_immediate_crash_is_reported_and_measurable(self):
        crashed = FakeProcess(returncode=23)
        stack = OperatorStack(
            {"camera_up": COMMANDS["camera_up"]},
            discover=lambda: facts("READY", "READY", "READY", "MISSING"),
            process_factory=FakeFactory(crashed),
        )
        with self.assertRaises(ContractError) as caught:
            stack.ensure()
        self.assertEqual(caught.exception.code, "OPERATOR_STACK_CHILD_EXITED")
        child = stack.status()["children"]["camera_up"]
        self.assertEqual(child["returncode"], 23)
        self.assertTrue(child["unexpected_exit"])

    def test_partial_start_rolls_back_only_started_child(self):
        robot = FakeProcess()
        factory = FakeFactory(robot, OSError("camera failed"))
        stack = OperatorStack(COMMANDS, discover=facts, process_factory=factory)
        with self.assertRaises(ContractError) as caught:
            stack.ensure()
        self.assertEqual(caught.exception.code, "OPERATOR_STACK_START")
        self.assertEqual(robot.terminate_calls, 1)
        self.assertEqual(robot.kill_calls, 0)
        self.assertEqual(stack.status()["children"]["robot_stack"]["returncode"], -15)

    def test_parallel_start_failure_cleans_up_a_distinct_running_sibling(self):
        robot = FakeProcess()
        camera_started = threading.Event()

        def factory(argv):
            if "real_robot.launch.py" in argv:
                self.assertTrue(camera_started.wait(1.0))
                return robot
            camera_started.set()
            raise OSError("camera failed")

        stack = OperatorStack(COMMANDS, discover=facts, process_factory=factory)
        with self.assertRaises(ContractError) as caught:
            stack.ensure()

        self.assertEqual(caught.exception.code, "OPERATOR_STACK_START")
        self.assertEqual((robot.terminate_calls, robot.kill_calls), (1, 0))

    def test_stop_timeout_escalates_to_kill_with_the_same_bound(self):
        camera = FakeProcess(terminate_timeouts=1)
        stack = OperatorStack(
            {"camera_up": COMMANDS["camera_up"]},
            discover=lambda: facts("READY", "READY", "READY", "MISSING"),
            process_factory=FakeFactory(camera), stop_timeout_s=0.25,
        )
        stack.ensure()
        stopped = stack.stop()["children"]["camera_up"]
        self.assertEqual((camera.terminate_calls, camera.kill_calls), (1, 1))
        self.assertEqual(camera.wait_timeouts, [0.25, 0.25])
        self.assertTrue(stopped["terminate_timed_out"])
        self.assertTrue(stopped["kill_used"])
        self.assertEqual(stopped["returncode"], -9)

    def test_existing_external_owners_are_reused_and_never_killed(self):
        external_robot = FakeProcess()
        external_camera = FakeProcess()
        observed = facts("READY", "READY", "READY", "READY")
        factory = FakeFactory()
        stack = OperatorStack(COMMANDS, discover=lambda: observed, process_factory=factory)
        self.assertEqual(stack.ensure()["state"], "ATTACHED")
        self.assertEqual(stack.stop()["children"], {})
        self.assertEqual(factory.calls, [])
        self.assertEqual(
            (external_robot.terminate_calls, external_robot.kill_calls,
             external_camera.terminate_calls, external_camera.kill_calls),
            (0, 0, 0, 0),
        )

    def test_ensure_is_idempotent_and_later_exit_is_measurable(self):
        robot, camera = FakeProcess(), FakeProcess()
        factory = FakeFactory(robot, camera)
        stack = OperatorStack(COMMANDS, discover=facts, process_factory=factory)
        stack.ensure()
        stack.ensure()
        self.assertEqual(len(factory.calls), 2)
        camera.returncode = 7
        report = stack.status()
        self.assertEqual(report["state"], "FAILED")
        self.assertTrue(report["children"]["camera_up"]["unexpected_exit"])
        with self.assertRaises(ContractError) as caught:
            stack.ensure()
        self.assertEqual(caught.exception.code, "OPERATOR_STACK_CHILD_EXITED")

    def test_gripper_setup_callback_requires_and_rechecks_one_owner(self):
        observed = facts("READY", "READY", "SETUP_REQUIRED", "READY")
        calls = []

        def setup(snapshot):
            calls.append(snapshot)
            observed["gripper"] = {"state": "READY", "owner": "ros-control"}

        stack = OperatorStack({}, discover=lambda: observed, gripper_setup=setup)
        self.assertEqual(stack.setup_gripper()["state"], "ATTACHED")
        self.assertEqual(len(calls), 1)

        observed["gripper"] = {"state": "SETUP_REQUIRED", "owner": "other-owner"}
        with self.assertRaises(ContractError) as caught:
            stack.setup_gripper()
        self.assertEqual(caught.exception.code, "OPERATOR_STACK_GRIPPER_SETUP_GATE")
        self.assertEqual(len(calls), 1)

    def test_reconfigure_replaces_only_the_named_owned_child(self):
        robot, old_camera, new_camera = FakeProcess(), FakeProcess(), FakeProcess()
        factory = FakeFactory(robot, old_camera, new_camera)
        stack = OperatorStack(COMMANDS, discover=facts, process_factory=factory)
        stack.ensure()
        replacement = {
            "argv": ["scripts/start_camera_group.sh", "up", "UVC", "/dev/camera"],
            "owner": "camera-group", "provides": ["camera"],
        }

        stack.reconfigure("camera_up", replacement)
        stack.ensure()

        self.assertEqual(robot.terminate_calls, 0)
        self.assertEqual(old_camera.terminate_calls, 1)
        self.assertEqual(new_camera.terminate_calls, 0)
        self.assertEqual(stack.commands["camera_up"]["owner"], "camera-group")

    def test_invalid_reconfigure_does_not_stop_the_current_child(self):
        camera = FakeProcess()
        stack = OperatorStack(
            {"camera_up": COMMANDS["camera_up"]},
            discover=lambda: facts("READY", "READY", "READY", "MISSING"),
            process_factory=FakeFactory(camera),
        )
        stack.ensure()

        with self.assertRaises(ContractError):
            stack.reconfigure("camera_up", {
                "argv": ["systemctl", "start", "camera"],
                "owner": "camera-up", "provides": ["camera"],
            })

        self.assertEqual(camera.terminate_calls, 0)
        self.assertIsNone(camera.poll())


if __name__ == "__main__":
    unittest.main()
