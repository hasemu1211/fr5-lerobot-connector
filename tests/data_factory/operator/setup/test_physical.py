from __future__ import annotations

import json
import signal
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from tools.data_factory.operator.setup.physical import (
    _default_process,
    build_physical_operator_environment,
)
from tools.fr5_data_factory import ContractError


UP_DEVICE = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
SIDE_DEVICE = "usb-Second_USB_Camera-video-index0"


def profile(*roles: str, serials: dict[str, str] | None = None) -> dict:
    camera_profile = "up" if roles == ("up",) else "-".join(roles)
    return {
        "schema_version": "data_factory.collection_profile.v2",
        "collection_profile_id": f"test-{camera_profile}-30hz",
        "qualification_status": "QUALIFIED",
        "camera_profile": camera_profile,
        "camera_roles": list(roles),
        "camera_serials": serials or {
            role: "Generic_USB2.0_PC_CAMERA" if role == "up" else "Second_USB_Camera"
            for role in roles
        },
        "camera_topics": {
            role: f"/camera/{role}/color/image_raw" for role in roles
        },
        "fps": 30, "width": 640, "height": 480,
        "image_qos": "reliable", "image_qos_depth": 10,
        "writer_queue_size": 128, "encoder_threads": 2,
        "encoding_mode": "batch", "repo_id": "local/test",
        "encoder_temp_policy": "DATASET_LOCAL",
        "dataset_incremental_peak_bytes": 1,
        "encoder_temp_peak_bytes": 1, "disk_reserve_bytes": 1,
        "portability_status": "QUALIFICATION_REQUIRED",
        "quality_contract_digest": "sha256:" + "0" * 64,
    }


def uvc(root: Path, stable_id: str) -> dict[str, str]:
    return {
        "kind": "UVC", "stable_id": stable_id,
        "capture_endpoint": str(root / stable_id),
    }


class FakeProcess:
    def __init__(self, kind, state):
        self.kind = kind
        self.state = state
        self.returncode = None
        state[kind] = True

    def poll(self):
        return self.state.get(f"{self.kind}_returncode", self.returncode)

    def terminate(self):
        self.returncode = -15
        self.state[self.kind] = False

    def kill(self):
        self.returncode = -9
        self.state[self.kind] = False

    def wait(self, _timeout):
        return self.returncode


class PhysicalEnvironmentTests(unittest.TestCase):
    @patch("tools.data_factory.operator.setup.physical.os.killpg")
    @patch("tools.data_factory.operator.setup.physical.subprocess.Popen")
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
        self, root: Path, state: dict[str, bool], *,
        collection_profile: dict | None = None,
        camera_devices: dict | None = None,
        external_ready: bool = False,
        external_command_server: bool = False,
        maintenance_open: bool = False,
        partial_camera_roles: set[str] | None = None,
        realsense_connected: bool = True,
        realsense_depth: bool = False,
        gripper_velocity_percent: int = 20,
        gripper_force_percent: int = 50,
        gripper_open_velocity_percent: int = 20,
    ):
        collection_profile = collection_profile or profile("up")
        camera_devices = camera_devices or {"up": uvc(root, UP_DEVICE)}
        calls = {"process": [], "maintenance": []}

        def active_roles() -> set[str]:
            if external_ready or state["camera"]:
                return set(collection_profile["camera_roles"])
            return set(partial_camera_roles or ())

        def command_result(argv):
            if state.get("query_forbidden"):
                raise AssertionError("owned child liveness must precede ROS discovery")
            if argv == (
                "ros2", "node", "list", "--no-daemon", "--spin-time", "2",
            ):
                nodes = []
                if external_command_server or state["maintenance"]:
                    nodes.append("/fr_command_server")
                if external_ready or state["robot"]:
                    nodes.append("/controller_manager")
                roles = active_roles()
                if state.get("camera_node_lag") and state["camera"]:
                    state["camera_node_lag"] -= 1
                    roles = set()
                for role in roles:
                    descriptor = camera_devices[role]
                    nodes.append(
                        f"/camera/{role}/color/uvc_{role}_camera"
                        if descriptor["kind"] == "UVC" else f"/camera/{role}"
                    )
                return "\n".join(nodes)
            if argv == (
                "ros2", "topic", "list", "--no-daemon", "--spin-time", "2",
            ):
                return "\n".join(
                    collection_profile["camera_topics"][role]
                    for role in active_roles()
                )
            if argv == ("ros2", "control", "list_controllers"):
                return (
                    "fairino5_controller active\n"
                    "gripper_controller active\n"
                    "joint_state_broadcaster active\n"
                )
            if argv[:3] == ("ros2", "node", "info"):
                role = next(
                    role for role in collection_profile["camera_roles"]
                    if argv[3].startswith(f"/camera/{role}")
                )
                return (
                    "Publishers:\n"
                    f"  {collection_profile['camera_topics'][role]}: sensor_msgs/msg/Image\n"
                )
            if argv[:3] == ("ros2", "param", "get"):
                node, name = argv[3], argv[4]
                role = next(
                    role for role in collection_profile["camera_roles"]
                    if node.startswith(f"/camera/{role}")
                )
                descriptor = camera_devices[role]
                if name == "video_device":
                    return str(Path(descriptor["capture_endpoint"]).resolve(strict=True))
                if name == "serial_no":
                    return "_" + descriptor["stable_id"]
                if name == "enable_color":
                    return "true"
                if name == "enable_depth":
                    return "true" if realsense_depth else "false"
            if argv[:3] == ("ros2", "param", "dump"):
                if state.get("parameter_query_failures"):
                    state["parameter_query_failures"] -= 1
                    raise ContractError("OPERATOR_ENVIRONMENT_QUERY_FAILED")
                node = argv[3]
                role = next(
                    role for role in collection_profile["camera_roles"]
                    if node.startswith(f"/camera/{role}")
                )
                descriptor = camera_devices[role]
                parameters = (
                    {
                        "video_device": str(
                            Path(descriptor["capture_endpoint"]).resolve(strict=True)
                        ),
                    }
                    if descriptor["kind"] == "UVC"
                    else {
                        "serial_no": "_" + descriptor["stable_id"],
                        "enable_color": True,
                        "enable_depth": realsense_depth,
                    }
                )
                return json.dumps({node: {"ros__parameters": parameters}})
            if argv == ("rs-enumerate-devices", "-s", "--no-dds"):
                if state.get("realsense_query_failures"):
                    state["realsense_query_failures"] -= 1
                    raise ContractError("OPERATOR_ENVIRONMENT_QUERY_FAILED")
                serials = [
                    item["stable_id"] for item in camera_devices.values()
                    if item["kind"] == "REALSENSE"
                ]
                return "\n".join(serials) if realsense_connected else ""
            raise AssertionError(argv)

        def command(argv):
            state.setdefault("command_calls", []).append(argv)
            activity_lock = state.get("activity_lock")
            if activity_lock is None:
                return command_result(argv)
            with activity_lock:
                state["active_queries"] = state.get("active_queries", 0) + 1
                state["max_active_queries"] = max(
                    state.get("max_active_queries", 0), state["active_queries"],
                )
            try:
                time.sleep(0.005)
                return command_result(argv)
            finally:
                with activity_lock:
                    state["active_queries"] -= 1

        def process(argv):
            calls["process"].append(argv)
            kind = (
                "maintenance" if "ros2_cmd_server" in argv
                else "robot" if "real_robot.launch.py" in argv
                else "camera"
            )
            return FakeProcess(kind, state)

        def readback(_node_names=None, _controller_listing=None):
            maintenance = external_command_server or state["maintenance"]
            if (
                not maintenance
                and state.get("gripper_readback_failures", 0) > 0
            ):
                state["gripper_readback_failures"] -= 1
                raise ContractError("GRIPPER_SETUP_READBACK")
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
            repository_root=Path(__file__).resolve().parents[4],
            collection_profile=collection_profile,
            camera_devices=camera_devices,
            command_call=command,
            process_factory=process,
            gripper_readback_call=readback,
            gripper_maintenance_call=maintain,
            settle_policy=lambda check: check(),
            controller_ip="192.0.2.1",
            gripper_velocity_percent=gripper_velocity_percent,
            gripper_force_percent=gripper_force_percent,
            gripper_open_velocity_percent=gripper_open_velocity_percent,
            device_root=root,
        )
        return environment, calls

    @staticmethod
    def make_uvc_links(root: Path) -> None:
        (root / UP_DEVICE).symlink_to("/dev/null")
        (root / SIDE_DEVICE).symlink_to("/dev/zero")

    def test_single_camera_missing_environment_starts_one_aggregate_camera_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, calls = self.build(
                root, state, gripper_velocity_percent=19,
                gripper_force_percent=25,
                gripper_open_velocity_percent=10,
            )

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
                ["maintenance", "camera", "robot"],
            )
            camera = calls["process"][1]
            self.assertIn("CAMERA_FPS=30", camera)
            self.assertEqual(camera[-3:], ("up", "UVC", str(root / UP_DEVICE)))
            robot = calls["process"][2]
            self.assertEqual(
                robot[:5],
                (
                    "env", "FR5_GRIPPER_VELOCITY=19",
                    "FR5_GRIPPER_FORCE=25",
                    "FR5_GRIPPER_OPEN_FORCE=50",
                    "FR5_GRIPPER_OPEN_VELOCITY=10",
                ),
            )
            self.assertEqual(environment.stop()["state"], "SETUP_REQUIRED")
            self.assertFalse(state["robot"] or state["camera"] or state["maintenance"])

    def test_owned_camera_topic_can_arrive_before_its_node(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            state = {
                "maintenance": False, "robot": False, "camera": False,
                "camera_node_lag": 1,
            }
            environment, _calls = self.build(root, state)

            projected = environment.prepare_environment()

            self.assertEqual(projected["state"], "READY")

    def test_owned_motion_retries_a_transient_first_gripper_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            state = {
                "maintenance": False, "robot": False, "camera": False,
                "gripper_readback_failures": 1,
            }
            environment, calls = self.build(root, state)

            self.assertEqual(environment.prepare_environment()["state"], "READY")
            self.assertEqual(state["gripper_readback_failures"], 0)
            self.assertEqual(
                sum("real_robot.launch.py" in argv for argv in calls["process"]),
                1,
            )
            environment.stop()

    def test_realsense_presence_retries_one_failed_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            serial = "254622073507"
            state = {
                "maintenance": False, "robot": False, "camera": False,
                "realsense_query_failures": 1,
            }
            environment, _calls = self.build(
                root, state,
                collection_profile=profile(
                    "up", serials={"up": "RUNTIME_BINDING_REQUIRED"},
                ),
                camera_devices={
                    "up": {
                        "kind": "REALSENSE", "stable_id": serial,
                        "capture_endpoint": serial,
                    },
                },
            )

            with patch(
                "tools.data_factory.operator.setup.physical.time.sleep",
            ) as retry_wait:
                projected = environment.projection()

            self.assertEqual(projected["state"], "SETUP_REQUIRED")
            self.assertEqual(state["realsense_query_failures"], 0)
            self.assertEqual(
                state["command_calls"].count(
                    ("rs-enumerate-devices", "-s", "--no-dds"),
                ),
                2,
            )
            retry_wait.assert_called_once_with(0.5)

    def test_camera_parameter_discovery_retries_one_failed_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            serial = "254622073507"
            state = {
                "maintenance": False, "robot": False, "camera": False,
                "parameter_query_failures": 1,
            }
            environment, _calls = self.build(
                root, state, external_ready=True,
                collection_profile=profile(
                    "up", serials={"up": "RUNTIME_BINDING_REQUIRED"},
                ),
                camera_devices={
                    "up": {
                        "kind": "REALSENSE", "stable_id": serial,
                        "capture_endpoint": serial,
                    },
                },
            )

            projected = environment.projection()

            self.assertEqual(projected["state"], "READY")
            self.assertEqual(state["parameter_query_failures"], 0)
            self.assertEqual(
                len([
                    call for call in state["command_calls"]
                    if call[:3] == ("ros2", "param", "dump")
                ]),
                2,
            )

    def test_existing_ready_owners_are_reused_without_setup_or_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, calls = self.build(root, state, external_ready=True)

            self.assertEqual(environment.prepare_environment()["state"], "READY")
            self.assertEqual(calls, {"process": [], "maintenance": []})

    def test_prepared_owned_environment_liveness_avoids_graph_rediscovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, _calls = self.build(root, state)
            self.assertEqual(environment.prepare_environment()["state"], "READY")
            state["query_forbidden"] = True

            self.assertEqual(environment.liveness()["state"], "READY")

            state["camera_returncode"] = 7
            blocked = environment.liveness()
            self.assertEqual(blocked["state"], "BLOCKED")
            self.assertEqual(
                blocked["components"]["camera"]["reason"],
                "OPERATOR_STACK_CHILD_EXITED",
            )

    def test_external_environment_liveness_keeps_fresh_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, _calls = self.build(root, state, external_ready=True)
            self.assertEqual(environment.projection()["state"], "READY")
            before = len(state["command_calls"])

            self.assertEqual(environment.liveness()["state"], "READY")

            self.assertGreater(len(state["command_calls"]), before)

    def test_concurrent_projections_serialize_transactions_and_parallelize_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            state = {
                "maintenance": False, "robot": False, "camera": False,
                "activity_lock": threading.Lock(),
            }
            environment, _calls = self.build(root, state, external_ready=True)
            start = threading.Barrier(2)

            def project():
                start.wait(timeout=1)
                return environment.projection()

            with ThreadPoolExecutor(max_workers=2) as executor:
                projections = [future.result() for future in (
                    executor.submit(project), executor.submit(project),
                )]

            self.assertEqual([item["state"] for item in projections], ["READY", "READY"])
            self.assertEqual(state["max_active_queries"], 2)
            self.assertEqual(
                state["command_calls"].count((
                    "ros2", "node", "list", "--no-daemon", "--spin-time", "2",
                )),
                2,
            )

    def test_graph_discovery_uses_the_measured_stable_spin_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, _calls = self.build(root, state, external_ready=True)

            self.assertEqual(environment.projection()["state"], "READY")
            self.assertIn(
                ("ros2", "node", "list", "--no-daemon", "--spin-time", "2"),
                state["command_calls"],
            )
            self.assertIn(
                ("ros2", "topic", "list", "--no-daemon", "--spin-time", "2"),
                state["command_calls"],
            )

    def test_camera_rebind_and_stop_preserve_the_owned_motion_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, calls = self.build(root, state)
            self.assertEqual(environment.prepare_environment()["state"], "READY")
            process_count = len(calls["process"])
            maintenance_count = len(calls["maintenance"])
            (root / UP_DEVICE).unlink()
            (root / UP_DEVICE).symlink_to("/dev/zero")
            rebound = environment.rebind_cameras(
                profile("up"), {"up": uvc(root, UP_DEVICE)},
            )

            self.assertEqual(rebound["state"], "SETUP_REQUIRED")
            self.assertTrue(state["robot"])
            self.assertFalse(state["camera"])
            self.assertEqual(len(calls["process"]), process_count)
            self.assertEqual(len(calls["maintenance"]), maintenance_count)

            self.assertEqual(environment.prepare_environment()["state"], "READY")
            self.assertTrue(state["robot"] and state["camera"])
            self.assertEqual(len(calls["process"]), process_count + 1)
            self.assertNotIn(
                "real_robot.launch.py", calls["process"][-1],
                "camera rebind restarted the motion owner",
            )
            stopped = environment.stop_cameras()
            self.assertEqual(stopped["state"], "SETUP_REQUIRED")
            self.assertTrue(state["robot"])
            self.assertFalse(state["camera"])

    def test_invalid_camera_rebind_preserves_existing_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, calls = self.build(root, state)
            self.assertEqual(environment.prepare_environment()["state"], "READY")
            process_count = len(calls["process"])
            maintenance_count = len(calls["maintenance"])
            invalid = profile("up")
            invalid["camera_roles"] = ["side"]

            with self.assertRaisesRegex(
                ContractError, "OPERATOR_PHYSICAL_CAMERA_PROFILE",
            ):
                environment.rebind_cameras(
                    invalid, {"up": uvc(root, UP_DEVICE)},
                )

            self.assertTrue(state["robot"] and state["camera"])
            self.assertEqual(len(calls["process"]), process_count)
            self.assertEqual(len(calls["maintenance"]), maintenance_count)

    def test_dual_uvc_realsense_roles_share_one_owner_and_require_rgb_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            dual = profile(
                "up", "side",
                serials={
                    "up": "RUNTIME_BINDING_REQUIRED",
                    "side": "RUNTIME_BINDING_REQUIRED",
                },
            )
            devices = {
                "up": uvc(root, UP_DEVICE),
                "side": {
                    "kind": "REALSENSE", "stable_id": "RS123",
                    "capture_endpoint": "RS123",
                },
            }
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, calls = self.build(
                root, state, collection_profile=dual, camera_devices=devices,
                external_ready=True,
            )

            ready = environment.prepare_environment()
            self.assertEqual(ready["components"]["camera"], {
                "state": "READY", "owner": "camera-group", "reason": "ATTACHED",
            })
            self.assertEqual(calls["process"], [])

            blocked, blocked_calls = self.build(
                root, state, collection_profile=dual, camera_devices=devices,
                external_ready=True, realsense_depth=True,
            )
            self.assertEqual(blocked.prepare_environment()["state"], "BLOCKED")
            self.assertEqual(blocked_calls["process"], [])

    def test_partial_dual_graph_blocks_without_starting_any_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            dual = profile("up", "side")
            devices = {"up": uvc(root, UP_DEVICE), "side": uvc(root, SIDE_DEVICE)}
            state = {"maintenance": False, "robot": True, "camera": False}
            environment, calls = self.build(
                root, state, collection_profile=dual, camera_devices=devices,
                partial_camera_roles={"up"},
            )

            self.assertEqual(environment.prepare_environment()["state"], "BLOCKED")
            self.assertEqual(calls, {"process": [], "maintenance": []})

    def test_nested_camera_child_exit_projects_before_discovery_within_two_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, _calls = self.build(root, state)
            self.assertEqual(environment.prepare_environment()["state"], "READY")
            state["camera_returncode"] = 17
            state["query_forbidden"] = True

            started = time.monotonic()
            projected = environment.projection()

            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(projected["state"], "BLOCKED")
            self.assertEqual(
                {item["reason"] for item in projected["components"].values()},
                {"OPERATOR_STACK_CHILD_EXITED"},
            )

    def test_missing_realsense_serial_blocks_before_starting_any_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            rs_profile = profile("up", serials={"up": "RS123"})
            devices = {"up": {
                "kind": "REALSENSE", "stable_id": "RS123",
                "capture_endpoint": "RS123",
            }}
            state = {"maintenance": False, "robot": True, "camera": False}
            environment, calls = self.build(
                root, state, collection_profile=rs_profile,
                camera_devices=devices, realsense_connected=False,
            )

            self.assertEqual(environment.prepare_environment()["state"], "BLOCKED")
            self.assertEqual(calls, {"process": [], "maintenance": []})

    def test_stale_duplicate_and_profile_mismatch_fail_before_process_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            spawn = Mock(side_effect=AssertionError("process side effect"))
            common = dict(
                repository_root=Path(__file__).resolve().parents[4],
                command_call=Mock(side_effect=AssertionError("query side effect")),
                process_factory=spawn,
                gripper_readback_call=Mock(), gripper_maintenance_call=Mock(),
                device_root=root,
            )
            stale = {"up": uvc(root, "usb-missing-video-index0")}
            with self.assertRaisesRegex(ContractError, "OPERATOR_PHYSICAL_CAMERA_DEVICE_STALE"):
                build_physical_operator_environment(
                    collection_profile=profile(
                        "up", serials={"up": "usb-missing"},
                    ),
                    camera_devices=stale, **common,
                )
            dual = profile(
                "up", "side",
                serials={
                    "up": "Generic_USB2.0_PC_CAMERA",
                    "side": "Generic_USB2.0_PC_CAMERA",
                },
            )
            alias = "usb-Generic_USB2.0_PC_CAMERA-alias-video-index0"
            (root / alias).symlink_to("/dev/null")
            duplicate = {"up": uvc(root, UP_DEVICE), "side": uvc(root, alias)}
            with self.assertRaisesRegex(ContractError, "OPERATOR_PHYSICAL_CAMERA_BINDING"):
                build_physical_operator_environment(
                    collection_profile=dual, camera_devices=duplicate, **common,
                )
            wrong_roles = profile("up")
            wrong_roles["camera_roles"] = ["up", "side"]
            with self.assertRaisesRegex(ContractError, "OPERATOR_PHYSICAL_CAMERA_PROFILE"):
                build_physical_operator_environment(
                    collection_profile=wrong_roles,
                    camera_devices={"up": uvc(root, UP_DEVICE)}, **common,
                )
            spawn.assert_not_called()

    def test_missing_environment_does_not_reopen_an_already_open_gripper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
            state = {"maintenance": False, "robot": False, "camera": False}
            environment, calls = self.build(root, state, maintenance_open=True)

            self.assertEqual(environment.prepare_environment()["state"], "READY")
            self.assertEqual(calls["maintenance"], [])
            environment.stop()

    def test_external_command_server_is_ambiguous_and_never_mutated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_uvc_links(root)
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
