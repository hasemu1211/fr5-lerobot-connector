from __future__ import annotations

import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.data_factory.operator_physical_environment import (
    _default_process,
    build_physical_operator_environment,
)


DEVICE = "usb-Generic_USB2.0_PC_CAMERA-video-index0"


class FakeProcess:
    def __init__(self, kind, state):
        self.kind = kind
        self.state = state
        self.returncode = None
        state[kind] = True

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.state[self.kind] = False

    def kill(self):
        self.returncode = -9
        self.state[self.kind] = False

    def wait(self, _timeout):
        return self.returncode


class PhysicalEnvironmentTests(unittest.TestCase):
    @patch("tools.data_factory.operator_physical_environment.os.killpg")
    @patch("tools.data_factory.operator_physical_environment.subprocess.Popen")
    def test_default_process_stops_the_ros_wrapper_and_children_as_one_group(
        self, popen, killpg,
    ):
        child = Mock(pid=4242, args=("ros2", "run", "pkg", "node"))
        child.poll.return_value = 0
        child.wait.return_value = -15
        popen.return_value = child
        killpg.side_effect = [None, None, ProcessLookupError, None]
        process = _default_process(Path("/tmp/repository"))(("ros2", "run", "pkg", "node"))

        self.assertIsNone(process.poll())
        process.terminate()
        self.assertEqual(process.wait(0.1), -15)
        process.kill()

        popen.assert_called_once_with(
            ("ros2", "run", "pkg", "node"),
            cwd=Path("/tmp/repository"), process_group=0,
        )
        self.assertEqual(
            [call.args for call in killpg.call_args_list],
            [
                (4242, 0), (4242, signal.SIGTERM),
                (4242, 0), (4242, signal.SIGKILL),
            ],
        )

    def build(
        self, root, state, *, external_ready=False,
        external_command_server=False, maintenance_open=False,
    ):
        calls = {"process": [], "maintenance": []}

        def command(argv):
            if argv == ("ros2", "node", "list", "--no-daemon"):
                nodes = []
                if external_command_server or state["maintenance"]:
                    nodes.append("/fr_command_server")
                if external_ready or state["robot"]:
                    nodes.append("/controller_manager")
                if external_ready or state["camera"]:
                    nodes.append("/camera/up/color/uvc_up_camera")
                return "\n".join(nodes)
            if argv == ("ros2", "control", "list_controllers"):
                return (
                    "fairino5_controller active\n"
                    "gripper_controller active\n"
                    "joint_state_broadcaster active\n"
                )
            if argv[:4] == ("ros2", "param", "get", "/camera/up/color/uvc_up_camera"):
                return str(root / DEVICE)
            raise AssertionError(argv)

        def process(argv):
            calls["process"].append(argv)
            kind = (
                "maintenance" if "ros2_cmd_server" in argv
                else "robot" if "real_robot.launch.py" in argv
                else "camera"
            )
            return FakeProcess(kind, state)

        def readback():
            maintenance = external_command_server or state["maintenance"]
            return {
                "active": maintenance_open or not maintenance,
                "position_valid": True,
                "gripper_index": 1,
                "reference_position_m": 0.021,
                "feedback_position_m": 0.021,
                "sample_age_s": 0.0,
                "max_age_s": 0.1,
                "source": (
                    "COMMAND_SERVER_MAINTENANCE" if maintenance
                    else "CONTROLLER_STATE"
                ),
            }

        def maintain(readback):
            calls["maintenance"].append(readback["source"])
            return {
                "status": "NORMALIZED",
                "requires_graph_switch": readback["source"] == "COMMAND_SERVER_MAINTENANCE",
            }

        environment = build_physical_operator_environment(
            repository_root=Path(__file__).resolve().parents[2],
            camera_device_id=DEVICE,
            command_call=command,
            process_factory=process,
            gripper_readback_call=readback,
            gripper_maintenance_call=maintain,
            settle_policy=lambda check: check(),
            controller_ip="192.0.2.1",
            device_root=root,
        )
        return environment, calls

    def test_missing_environment_bootstraps_gripper_then_starts_normal_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / DEVICE).symlink_to("/dev/null")
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, calls = self.build(root, state)

            projected = environment.prepare_environment()

            self.assertEqual(projected["state"], "READY")
            self.assertEqual(calls["maintenance"], ["COMMAND_SERVER_MAINTENANCE"])
            self.assertEqual(
                [
                    "maintenance" if "ros2_cmd_server" in argv
                    else "robot" if "real_robot.launch.py" in argv
                    else "camera"
                    for argv in calls["process"]
                ],
                ["maintenance", "robot", "camera"],
            )
            stopped = environment.stop()
            self.assertEqual(stopped["state"], "SETUP_REQUIRED")
            self.assertFalse(state["robot"] or state["camera"] or state["maintenance"])

    def test_existing_ready_owners_are_reused_without_setup_or_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / DEVICE).symlink_to("/dev/null")
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, calls = self.build(root, state, external_ready=True)

            projected = environment.prepare_environment()

            self.assertEqual(projected["state"], "READY")
            self.assertEqual(calls, {"process": [], "maintenance": []})

    def test_missing_environment_does_not_reopen_an_already_open_gripper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / DEVICE).symlink_to("/dev/null")
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, calls = self.build(root, state, maintenance_open=True)

            self.assertEqual(environment.prepare_environment()["state"], "READY")
            self.assertEqual(calls["maintenance"], [])
            environment.stop()

    def test_external_command_server_is_ambiguous_and_never_mutated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / DEVICE).symlink_to("/dev/null")
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, calls = self.build(
                root, state, external_command_server=True,
            )

            projected = environment.prepare_environment()

            self.assertEqual(projected["state"], "BLOCKED")
            self.assertEqual(calls, {"process": [], "maintenance": []})
            self.assertEqual(
                {item["reason"] for item in projected["components"].values()},
                {"OPERATOR_STACK_AMBIGUOUS"},
            )


if __name__ == "__main__":
    unittest.main()
