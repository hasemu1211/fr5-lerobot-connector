"""ROS/UVC adapters for the reusable foreground operator environment.

This module owns discovery and process bring-up only.  It never plans or moves
robot joints, starts a recorder, or writes a dataset.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Mapping

from tools.data_factory.operator_environment import OperatorEnvironment
from tools.data_factory.operator_setup import gripper_setup_projection
from tools.data_factory.operator_stack import OperatorStack
from tools.fr5_data_factory import ContractError


MOTION_OWNER = "fr5-ros2-control"
CAMERA_OWNER = "uvc-up-camera"
EXPECTED_CONTROLLERS = frozenset({
    "fairino5_controller", "gripper_controller", "joint_state_broadcaster",
})
CAMERA_NODE = "/camera/up/color/uvc_up_camera"


class _ForegroundProcessGroup:
    """Keep CLI wrappers and their ROS children under one stoppable owner."""

    def __init__(self, argv: tuple[str, ...], *, cwd: Path) -> None:
        self._process = subprocess.Popen(
            argv, cwd=cwd, process_group=0,
        )

    def poll(self):
        returncode = self._process.poll()
        return None if returncode is not None and self._running() else returncode

    def _running(self) -> bool:
        try:
            os.killpg(self._process.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        returncode = self._process.wait(timeout)
        while self._running():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self._process.args, timeout)
            time.sleep(min(0.05, remaining))
        return returncode

    def terminate(self) -> None:
        os.killpg(self._process.pid, signal.SIGTERM)

    def kill(self) -> None:
        os.killpg(self._process.pid, signal.SIGKILL)


def _default_command(argv: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(
            argv, text=True, capture_output=True, timeout=4.0, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError("OPERATOR_ENVIRONMENT_QUERY_FAILED") from exc
    if completed.returncode != 0:
        raise ContractError("OPERATOR_ENVIRONMENT_QUERY_FAILED")
    return completed.stdout


def _default_process(repository: Path) -> Callable[[tuple[str, ...]], object]:
    return lambda argv: _ForegroundProcessGroup(argv, cwd=repository)


def bounded_settle(
    check: Callable[[], bool], *, timeout_s: float = 25.0, interval_s: float = 0.2,
) -> bool:
    """Poll a read-only predicate within one explicit foreground bound."""
    deadline = time.monotonic() + timeout_s
    while True:
        if check():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval_s, remaining))


def _stop_owned_process(process: object, timeout_s: float = 3.0) -> None:
    try:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout_s)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout_s)
    except (AttributeError, OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError("OPERATOR_ENVIRONMENT_BOOTSTRAP_STOP") from exc


def build_physical_operator_environment(
    *, repository_root: str | Path, camera_device_id: str,
    command_call: Callable[[tuple[str, ...]], str] = _default_command,
    process_factory: Callable[[tuple[str, ...]], object] | None = None,
    gripper_readback_call: Callable[[], Mapping[str, object]],
    gripper_maintenance_call: Callable[[Mapping[str, object]], Mapping[str, object]],
    settle_policy: Callable[[Callable[[], bool]], bool] = bounded_settle,
    controller_ip: str | None = None,
    device_root: str | Path = "/dev/v4l/by-id",
) -> OperatorEnvironment:
    """Build one owner-aware environment for the selected qualified UVC device."""
    repository = Path(repository_root).resolve(strict=True)
    stable_path = Path(device_root) / camera_device_id
    if (
        not camera_device_id.startswith("usb-")
        or "/" in camera_device_id
        or not callable(command_call)
        or not callable(gripper_readback_call)
        or not callable(gripper_maintenance_call)
        or not callable(settle_policy)
    ):
        raise ContractError("OPERATOR_PHYSICAL_ENVIRONMENT_INPUT")
    spawn = process_factory or _default_process(repository)
    if not callable(spawn):
        raise ContractError("OPERATOR_PHYSICAL_ENVIRONMENT_INPUT")
    owned_started = {"motion": False, "camera": False}

    def nodes() -> set[str]:
        return {
            line.strip()
            for line in command_call(("ros2", "node", "list", "--no-daemon")).splitlines()
            if line.strip().startswith("/")
        }

    def controllers() -> set[str]:
        return {
            fields[0]
            for line in command_call(("ros2", "control", "list_controllers")).splitlines()
            if (fields := line.split()) and "active" in fields
        }

    def camera_target(node: str) -> Path:
        value = command_call((
            "ros2", "param", "get", node, "video_device", "--hide-type", "--no-daemon",
        )).strip()
        try:
            return Path(value).resolve(strict=True)
        except OSError as exc:
            raise ContractError("OPERATOR_ENVIRONMENT_CAMERA_BINDING") from exc

    def discover() -> dict[str, dict[str, object]]:
        graph = nodes()
        has_manager = "/controller_manager" in graph
        has_command_server = "/fr_command_server" in graph
        motion: dict[str, dict[str, object]]
        if has_command_server:
            motion = {
                name: {"state": "AMBIGUOUS", "owner": None}
                for name in ("robot", "controller", "gripper")
            }
        elif has_manager:
            active = controllers()
            if EXPECTED_CONTROLLERS <= active:
                projection = gripper_setup_projection(gripper_readback_call())
                gripper_state = {
                    "ATTACHED": "READY",
                    "MAINTENANCE_APPROVAL_REQUIRED": "SETUP_REQUIRED",
                }.get(projection["state"], "AMBIGUOUS")
                motion = {
                    "robot": {"state": "READY", "owner": MOTION_OWNER},
                    "controller": {"state": "READY", "owner": MOTION_OWNER},
                    "gripper": {
                        "state": gripper_state,
                        "owner": MOTION_OWNER if gripper_state != "AMBIGUOUS" else None,
                    },
                }
            else:
                state = "MISSING" if owned_started["motion"] else "AMBIGUOUS"
                motion = {
                    name: {"state": state, "owner": None}
                    for name in ("robot", "controller", "gripper")
                }
        else:
            motion = {
                name: {"state": "MISSING", "owner": None}
                for name in ("robot", "controller", "gripper")
            }

        try:
            selected_target = stable_path.resolve(strict=True)
        except OSError:
            selected_target = None
        uvc_nodes = sorted(
            node for node in graph
            if node.startswith("/camera/") and "uvc_" in node and node.endswith("_camera")
        )
        if CAMERA_NODE in graph and selected_target is not None:
            state = "READY" if camera_target(CAMERA_NODE) == selected_target else "AMBIGUOUS"
        elif CAMERA_NODE in graph:
            state = "AMBIGUOUS"
        else:
            occupied = selected_target is not None and any(
                camera_target(node) == selected_target for node in uvc_nodes
            )
            state = "AMBIGUOUS" if occupied else "MISSING"
        motion["camera"] = {
            "state": state,
            "owner": CAMERA_OWNER if state == "READY" else None,
        }
        return motion

    def tracked_spawn(argv: tuple[str, ...]) -> object:
        process = spawn(argv)
        if "real_robot.launch.py" in argv:
            owned_started["motion"] = True
        elif str(repository / "scripts/start_uvc_camera.sh") in argv:
            owned_started["camera"] = True
        return process

    commands = {
        "robot_stack": {
            "argv": (
                "ros2", "launch", "fairino5_v6_moveit2_config",
                "real_robot.launch.py", "use_fake_hardware:=false", "use_rviz:=false",
            ),
            "owner": MOTION_OWNER,
            "provides": ("robot", "controller", "gripper"),
        },
        "camera_up": {
            "argv": (
                "env", "UVC_ROLE=up", f"UVC_DEVICE={stable_path}", "UVC_FPS=30",
                str(repository / "scripts/start_uvc_camera.sh"),
            ),
            "owner": CAMERA_OWNER,
            "provides": ("camera",),
        },
    }

    def setup_attached_gripper(_facts: dict[str, dict[str, object]]) -> None:
        readback = gripper_readback_call()
        if readback.get("source") != "CONTROLLER_STATE":
            raise ContractError("OPERATOR_ENVIRONMENT_GRIPPER_OWNER")
        result = gripper_maintenance_call(readback)
        if result != {"status": "NORMALIZED", "requires_graph_switch": False}:
            raise ContractError("OPERATOR_ENVIRONMENT_GRIPPER_SETUP")

    stack = OperatorStack(
        commands, discover=discover, process_factory=tracked_spawn,
        gripper_setup=setup_attached_gripper,
    )

    def bootstrap_missing_motion() -> None:
        ip = controller_ip or os.environ.get("FR5_CONTROLLER_IP")
        if not isinstance(ip, str) or not ip.strip():
            raise ContractError("OPERATOR_ENVIRONMENT_CONTROLLER_IP")
        process = tracked_spawn((
            "ros2", "run", "fairino_hardware_v3_9_7", "ros2_cmd_server",
            "--ros-args", "-p", f"robot_ip:={ip.strip()}",
        ))
        try:
            if not settle_policy(lambda: process.poll() is None and "/fr_command_server" in nodes()):
                raise ContractError("OPERATOR_ENVIRONMENT_GRIPPER_BOOTSTRAP")
            readback = gripper_readback_call()
            if readback.get("source") != "COMMAND_SERVER_MAINTENANCE":
                raise ContractError("OPERATOR_ENVIRONMENT_GRIPPER_OWNER")
            projection = gripper_setup_projection(readback)
            if projection["state"] == "MAINTENANCE_APPROVAL_REQUIRED":
                result = gripper_maintenance_call(readback)
                if result != {"status": "NORMALIZED", "requires_graph_switch": True}:
                    raise ContractError("OPERATOR_ENVIRONMENT_GRIPPER_SETUP")
            elif projection["state"] != "ATTACHED":
                raise ContractError("OPERATOR_ENVIRONMENT_GRIPPER_SETUP")
        finally:
            _stop_owned_process(process)
        if not settle_policy(lambda: "/fr_command_server" not in nodes()):
            raise ContractError("OPERATOR_ENVIRONMENT_GRIPPER_OWNER")

    return OperatorEnvironment(
        stack,
        settle_policy=settle_policy,
        bootstrap_missing_motion=bootstrap_missing_motion,
    )


__all__ = ["bounded_settle", "build_physical_operator_environment"]
