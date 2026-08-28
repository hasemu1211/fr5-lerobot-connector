"""ROS/camera adapters for the reusable foreground operator environment.

This module owns discovery and process bring-up only.  It never plans or moves
robot joints, starts a recorder, or writes a dataset.
"""
from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.data_factory.operator_environment import OperatorEnvironment
from tools.data_factory.operator_setup import gripper_setup_projection
from tools.data_factory.operator_stack import OperatorStack
from tools.fr5_data_factory import COLLECTION_PROFILE_V2_KEYS, ContractError, SAFE_ID


MOTION_OWNER = "fr5-ros2-control"
CAMERA_OWNER = "camera-group"
EXPECTED_CONTROLLERS = frozenset({
    "fairino5_controller", "gripper_controller", "joint_state_broadcaster",
})
CAMERA_DEVICE_FIELDS = frozenset({"kind", "stable_id", "capture_endpoint"})
CAMERA_ROLES = frozenset({"up", "side", "wrist"})
RUNTIME_CAMERA_BINDING = "RUNTIME_BINDING_REQUIRED"
CAMERA_PROFILES = {
    "up": ("up",),
    "up-side": ("up", "side"),
    "up-wrist": ("up", "wrist"),
}


class PhysicalOperatorEnvironment(OperatorEnvironment):
    """Operator environment with a camera-only foreground rebind seam."""

    def __init__(self, *args, rebind_call, stop_cameras_call, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._rebind_call = rebind_call
        self._stop_cameras_call = stop_cameras_call

    def rebind_cameras(
        self, collection_profile: Mapping[str, Any],
        camera_devices: Mapping[str, Mapping[str, str]],
    ) -> dict[str, Any]:
        return self._rebind_call(collection_profile, camera_devices)

    def stop_cameras(self) -> dict[str, Any]:
        return self._stop_cameras_call()


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


def _camera_node(role: str, kind: str) -> str:
    return (
        f"/camera/{role}/color/uvc_{role}_camera"
        if kind == "UVC" else f"/camera/{role}"
    )


def _validated_camera_specs(
    profile: Mapping[str, Any], devices: Mapping[str, Mapping[str, str]],
    *, device_root: str | Path,
) -> tuple[dict[str, Any], ...]:
    """Reduce a validated profile plus exact role bindings to launch specs."""
    if (
        not isinstance(profile, Mapping)
        or set(profile) != COLLECTION_PROFILE_V2_KEYS
        or profile.get("schema_version") != "data_factory.collection_profile.v2"
        or profile.get("qualification_status") != "QUALIFIED"
        or not isinstance(devices, Mapping)
    ):
        raise ContractError("OPERATOR_PHYSICAL_CAMERA_PROFILE")
    roles = profile.get("camera_roles")
    topics = profile.get("camera_topics")
    serials = profile.get("camera_serials")
    expected_roles = CAMERA_PROFILES.get(profile.get("camera_profile"))
    if (
        not isinstance(roles, list)
        or tuple(roles) != expected_roles
        or not 1 <= len(roles) <= 2
        or set(roles) != set(devices)
        or not isinstance(topics, Mapping)
        or set(roles) != set(topics)
        or not isinstance(serials, Mapping)
        or set(roles) != set(serials)
        or any(role not in CAMERA_ROLES for role in roles)
        or any(
            isinstance(profile.get(key), bool)
            or not isinstance(profile.get(key), int)
            or profile[key] <= 0
            for key in ("fps", "width", "height")
        )
    ):
        raise ContractError("OPERATOR_PHYSICAL_CAMERA_PROFILE")

    root = Path(device_root)
    specs: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    endpoints: set[str] = set()
    physical_devices: set[tuple[str, str]] = set()
    for role in roles:
        value = devices[role]
        if not isinstance(value, Mapping) or set(value) != CAMERA_DEVICE_FIELDS:
            raise ContractError("OPERATOR_PHYSICAL_CAMERA_BINDING")
        kind, stable_id, capture_endpoint = (
            value["kind"], value["stable_id"], value["capture_endpoint"],
        )
        if (
            kind not in {"UVC", "REALSENSE"}
            or not isinstance(stable_id, str)
            or not stable_id
            or "\0" in stable_id
            or not isinstance(capture_endpoint, str)
            or not capture_endpoint
            or "\0" in capture_endpoint
            or (kind, stable_id) in identities
            or capture_endpoint in endpoints
        ):
            raise ContractError("OPERATOR_PHYSICAL_CAMERA_BINDING")
        serial = profile["camera_serials"][role]
        topic = profile["camera_topics"][role]
        if (
            not isinstance(serial, str)
            or not serial
            or topic != f"/camera/{role}/color/image_raw"
        ):
            raise ContractError("OPERATOR_PHYSICAL_CAMERA_PROFILE")
        spec: dict[str, Any] = {
            "role": role, "kind": kind, "stable_id": stable_id,
            "capture_endpoint": capture_endpoint, "topic": topic,
            "node": _camera_node(role, kind),
        }
        if kind == "UVC":
            endpoint = Path(capture_endpoint)
            expected = root / stable_id
            if (
                not stable_id.startswith("usb-")
                or "/" in stable_id
                or endpoint != expected
                or (
                    serial != RUNTIME_CAMERA_BINDING
                    and serial not in stable_id
                )
            ):
                raise ContractError("OPERATOR_PHYSICAL_CAMERA_BINDING")
            try:
                target = endpoint.resolve(strict=True)
                if not endpoint.is_symlink() or not stat.S_ISCHR(target.stat().st_mode):
                    raise OSError
            except OSError as exc:
                raise ContractError("OPERATOR_PHYSICAL_CAMERA_DEVICE_STALE") from exc
            spec["target"] = target
            physical_device = (kind, str(target))
        elif (
            SAFE_ID.fullmatch(stable_id) is None
            or capture_endpoint != stable_id
            or serial not in {RUNTIME_CAMERA_BINDING, stable_id}
        ):
            raise ContractError("OPERATOR_PHYSICAL_CAMERA_BINDING")
        else:
            physical_device = (kind, stable_id)
        if physical_device in physical_devices:
            raise ContractError("OPERATOR_PHYSICAL_CAMERA_BINDING")
        identities.add((kind, stable_id))
        endpoints.add(capture_endpoint)
        physical_devices.add(physical_device)
        specs.append(spec)
    return tuple(specs)


def _camera_command(
    repository: Path, profile: Mapping[str, Any],
    specs: tuple[Mapping[str, Any], ...],
) -> dict[str, object]:
    argv = [
        "env", f"CAMERA_FPS={profile['fps']}",
        f"CAMERA_WIDTH={profile['width']}",
        f"CAMERA_HEIGHT={profile['height']}",
        str(repository / "scripts/start_camera_group.sh"),
    ]
    for spec in specs:
        argv.extend((spec["role"], spec["kind"], spec["capture_endpoint"]))
    return {"argv": tuple(argv), "owner": CAMERA_OWNER, "provides": ("camera",)}


def build_physical_operator_environment(
    *, repository_root: str | Path, collection_profile: Mapping[str, Any],
    camera_devices: Mapping[str, Mapping[str, str]],
    command_call: Callable[[tuple[str, ...]], str] = _default_command,
    process_factory: Callable[[tuple[str, ...]], object] | None = None,
    gripper_readback_call: Callable[[], Mapping[str, object]],
    gripper_maintenance_call: Callable[[Mapping[str, object]], Mapping[str, object]],
    settle_policy: Callable[[Callable[[], bool]], bool] = bounded_settle,
    controller_ip: str | None = None,
    device_root: str | Path = "/dev/v4l/by-id",
) -> OperatorEnvironment:
    """Build one owner-aware environment for an exact one/two-camera role map."""
    repository = Path(repository_root).resolve(strict=True)
    camera_specs = _validated_camera_specs(
        collection_profile, camera_devices, device_root=device_root,
    )
    camera_config: dict[str, Any] = {"specs": camera_specs}
    if (
        not callable(command_call)
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

    def topics() -> set[str]:
        return {
            line.strip()
            for line in command_call(("ros2", "topic", "list", "--no-daemon")).splitlines()
            if line.strip().startswith("/")
        }

    def controllers() -> set[str]:
        return {
            fields[0]
            for line in command_call(("ros2", "control", "list_controllers")).splitlines()
            if (fields := line.split()) and "active" in fields
        }

    def parameter(node: str, name: str) -> str:
        return command_call((
            "ros2", "param", "get", node, name, "--hide-type", "--no-daemon",
        )).strip().strip('"')

    def camera_target(node: str) -> Path:
        value = parameter(node, "video_device")
        try:
            return Path(value).resolve(strict=True)
        except OSError as exc:
            raise ContractError("OPERATOR_ENVIRONMENT_CAMERA_BINDING") from exc

    def realsense_present(serial: str) -> bool:
        output = command_call(("rs-enumerate-devices", "-s", "--no-dds"))
        return re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(serial)}(?![A-Za-z0-9_.-])",
            output,
        ) is not None

    def node_publishes(node: str, topic: str) -> bool:
        output = command_call(("ros2", "node", "info", node, "--no-daemon"))
        return any(
            line.strip() == f"{topic}: sensor_msgs/msg/Image"
            for line in output.splitlines()
        )

    def uvc_present(spec: Mapping[str, Any]) -> bool:
        try:
            endpoint = Path(spec["capture_endpoint"])
            return (
                endpoint.is_symlink()
                and endpoint.resolve(strict=True) == spec["target"]
                and stat.S_ISCHR(spec["target"].stat().st_mode)
            )
        except OSError:
            return False

    def role_camera_state(
        spec: Mapping[str, Any], graph: set[str], graph_topics: set[str],
    ) -> str:
        node, topic = spec["node"], spec["topic"]
        role_prefix = f"/camera/{spec['role']}"
        role_nodes = {
            item for item in graph
            if item == role_prefix or item.startswith(role_prefix + "/")
        }
        if node not in graph:
            occupied = False
            if spec["kind"] == "UVC":
                occupied = any(
                    camera_target(item) == spec["target"]
                    for item in graph
                    if item.startswith("/camera/") and "/uvc_" in item
                    and item.endswith("_camera")
                )
                present = uvc_present(spec)
            else:
                present = realsense_present(spec["stable_id"])
                occupied = any(
                    parameter(item, "serial_no").lstrip("_") == spec["stable_id"]
                    for item in graph
                    if item.startswith("/camera/") and item.count("/") == 2
                )
            return (
                "AMBIGUOUS"
                if role_nodes or topic in graph_topics or occupied or not present
                else "MISSING"
            )
        if topic not in graph_topics or not node_publishes(node, topic):
            return "AMBIGUOUS"
        if spec["kind"] == "UVC":
            return (
                "READY"
                if uvc_present(spec) and camera_target(node) == spec["target"]
                else "AMBIGUOUS"
            )
        serial = parameter(node, "serial_no").lstrip("_")
        color = parameter(node, "enable_color").lower()
        depth = parameter(node, "enable_depth").lower()
        return (
            "READY"
            if serial == spec["stable_id"] and color == "true" and depth == "false"
            else "AMBIGUOUS"
        )

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

        graph_topics = topics()
        camera_states = [
            role_camera_state(spec, graph, graph_topics)
            for spec in camera_config["specs"]
        ]
        state = (
            "MISSING" if not camera_states
            else "READY" if all(item == "READY" for item in camera_states)
            else "MISSING" if all(item == "MISSING" for item in camera_states)
            else "AMBIGUOUS"
        )
        motion["camera"] = {
            "state": state,
            "owner": CAMERA_OWNER if state == "READY" else None,
        }
        return motion

    def tracked_spawn(argv: tuple[str, ...]) -> object:
        process = spawn(argv)
        if "real_robot.launch.py" in argv:
            owned_started["motion"] = True
        elif str(repository / "scripts/start_camera_group.sh") in argv:
            owned_started["camera"] = True
        return process

    commands = {
        "camera_group": _camera_command(repository, collection_profile, camera_specs),
        "robot_stack": {
            "argv": (
                "ros2", "launch", "fairino5_v6_moveit2_config",
                "real_robot.launch.py", "use_fake_hardware:=false", "use_rviz:=false",
            ),
            "owner": MOTION_OWNER,
            "provides": ("robot", "controller", "gripper"),
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

    environment: PhysicalOperatorEnvironment

    def rebind_cameras(
        profile: Mapping[str, Any], devices: Mapping[str, Mapping[str, str]],
    ) -> dict[str, Any]:
        specs = _validated_camera_specs(profile, devices, device_root=device_root)
        stack.reconfigure("camera_group", _camera_command(repository, profile, specs))
        camera_config["specs"] = specs
        return environment.projection()

    def stop_cameras() -> dict[str, Any]:
        stack.reconfigure("camera_group", None)
        camera_config["specs"] = ()
        return environment.projection()

    environment = PhysicalOperatorEnvironment(
        stack,
        settle_policy=settle_policy,
        bootstrap_missing_motion=bootstrap_missing_motion,
        rebind_call=rebind_cameras,
        stop_cameras_call=stop_cameras,
    )
    return environment


__all__ = [
    "PhysicalOperatorEnvironment", "bounded_settle",
    "build_physical_operator_environment",
]
