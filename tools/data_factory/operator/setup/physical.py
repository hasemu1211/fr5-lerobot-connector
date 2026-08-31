"""ROS/camera adapters for the reusable foreground operator environment.

This module owns discovery and process bring-up only.  It never plans or moves
robot joints, starts a recorder, or writes a dataset.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from tools.data_factory.operator.setup.camera import (
    REALSENSE_QUERY_RETRY_DELAYS,
    discover_uvc_device_ids,
)
from tools.data_factory.operator.setup.contracts import (
    gripper_setup_projection,
    normalize_camera_devices,
)
from tools.data_factory.operator.setup.environment import OperatorEnvironment
from tools.data_factory.operator.setup.processes import OperatorStack
from tools.fr5_data_factory import (
    COLLECTION_PROFILE_V2_KEYS,
    ContractError,
    SAFE_ID,
    canonical_digest,
    load_json_strict,
)


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
ROS_DISCOVERY_SPIN_SECONDS = "2"
ROS_PARAMETER_DISCOVERY_SPIN_SECONDS = "0.2"


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
        print(json.dumps({
            "event": "operator_environment_query_failed",
            "argv": list(argv),
            "failure": (
                "TIMEOUT" if isinstance(exc, subprocess.TimeoutExpired)
                else type(exc).__name__
            ),
        }, sort_keys=True), file=sys.stderr, flush=True)
        raise ContractError("OPERATOR_ENVIRONMENT_QUERY_FAILED") from exc
    if completed.returncode != 0:
        print(json.dumps({
            "event": "operator_environment_query_failed",
            "argv": list(argv),
            "failure": "NONZERO_EXIT",
            "returncode": completed.returncode,
        }, sort_keys=True), file=sys.stderr, flush=True)
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
    gripper_readback_call: Callable[
        [set[str] | None, str | None], Mapping[str, object]
    ],
    gripper_maintenance_call: Callable[[Mapping[str, object]], Mapping[str, object]],
    settle_policy: Callable[[Callable[[], bool]], bool] = bounded_settle,
    controller_ip: str | None = None,
    gripper_force_percent: int = 50,
    gripper_open_force_percent: int = 50,
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
        or type(gripper_force_percent) is not int
        or not 1 <= gripper_force_percent <= 100
        or type(gripper_open_force_percent) is not int
        or not 1 <= gripper_open_force_percent <= 100
    ):
        raise ContractError("OPERATOR_PHYSICAL_ENVIRONMENT_INPUT")
    spawn = process_factory or _default_process(repository)
    if not callable(spawn):
        raise ContractError("OPERATOR_PHYSICAL_ENVIRONMENT_INPUT")
    owned_children: dict[str, object] = {}
    discovery_lock = threading.Lock()

    def owned_running(name: str) -> bool:
        process = owned_children.get(name)
        return process is not None and process.poll() is None

    def nodes() -> set[str]:
        return {
            line.strip()
            for line in command_call((
                "ros2", "node", "list", "--no-daemon",
                "--spin-time", ROS_DISCOVERY_SPIN_SECONDS,
            )).splitlines()
            if line.strip().startswith("/")
        }

    def topics() -> set[str]:
        return {
            line.strip()
            for line in command_call((
                "ros2", "topic", "list", "--no-daemon",
                "--spin-time", ROS_DISCOVERY_SPIN_SECONDS,
            )).splitlines()
            if line.strip().startswith("/")
        }

    def controller_listing() -> str:
        return command_call(("ros2", "control", "list_controllers"))

    def parameter(node: str, name: str) -> str:
        return command_call((
            "ros2", "param", "get", node, name, "--hide-type", "--no-daemon",
        )).strip().strip('"')

    def parameters(node: str) -> Mapping[str, Any]:
        try:
            import yaml
        except ImportError as exc:
            raise ContractError("OPERATOR_ENVIRONMENT_CAMERA_BINDING") from exc
        argv = (
            "ros2", "param", "dump", node, "--no-daemon",
            "--spin-time", ROS_PARAMETER_DISCOVERY_SPIN_SECONDS,
            "--timeout", "2",
        )
        try:
            try:
                output = command_call(argv)
            except ContractError as exc:
                if exc.code != "OPERATOR_ENVIRONMENT_QUERY_FAILED":
                    raise
                output = command_call(argv)
            document = yaml.safe_load(output)
            values = document[node]["ros__parameters"]
        except (KeyError, TypeError, yaml.YAMLError) as exc:
            raise ContractError("OPERATOR_ENVIRONMENT_CAMERA_BINDING") from exc
        if not isinstance(values, Mapping):
            raise ContractError("OPERATOR_ENVIRONMENT_CAMERA_BINDING")
        return values

    def camera_target(node: str) -> Path:
        value = parameter(node, "video_device")
        try:
            return Path(value).resolve(strict=True)
        except OSError as exc:
            raise ContractError("OPERATOR_ENVIRONMENT_CAMERA_BINDING") from exc

    def realsense_present(serial: str) -> bool:
        for attempt in range(len(REALSENSE_QUERY_RETRY_DELAYS) + 1):
            try:
                output = command_call(("rs-enumerate-devices", "-s", "--no-dds"))
                break
            except ContractError as exc:
                if (
                    exc.code != "OPERATOR_ENVIRONMENT_QUERY_FAILED"
                    or attempt == len(REALSENSE_QUERY_RETRY_DELAYS)
                ):
                    raise
                time.sleep(REALSENSE_QUERY_RETRY_DELAYS[attempt])
        return re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(serial)}(?![A-Za-z0-9_.-])",
            output,
        ) is not None

    def node_publishes(node: str, topic: str) -> bool:
        output = command_call((
            "ros2", "node", "info", node, "--no-daemon",
            "--spin-time", ROS_DISCOVERY_SPIN_SECONDS,
        ))
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
            if (
                owned_running("camera") and not role_nodes
                and topic in graph_topics and not occupied and present
            ):
                return "MISSING"
            return (
                "AMBIGUOUS"
                if role_nodes or topic in graph_topics or occupied or not present
                else "MISSING"
            )
        if topic not in graph_topics:
            return "MISSING" if owned_running("camera") else "AMBIGUOUS"
        publishes = node_publishes(node, topic)
        if not publishes:
            return "MISSING" if owned_running("camera") else "AMBIGUOUS"
        node_parameters = parameters(node)
        if spec["kind"] == "UVC":
            value = node_parameters.get("video_device")
            try:
                target = Path(value).resolve(strict=True) if isinstance(value, str) else None
            except OSError as exc:
                raise ContractError("OPERATOR_ENVIRONMENT_CAMERA_BINDING") from exc
            return (
                "READY"
                if uvc_present(spec) and target == spec["target"]
                else "AMBIGUOUS"
            )
        serial = node_parameters.get("serial_no")
        return (
            "READY"
            if (
                isinstance(serial, str)
                and serial.lstrip("_") == spec["stable_id"]
                and node_parameters.get("enable_color") is True
                and node_parameters.get("enable_depth") is False
            )
            else "AMBIGUOUS"
        )

    def motion_state(graph: set[str]) -> dict[str, dict[str, object]]:
        has_manager = "/controller_manager" in graph
        has_command_server = "/fr_command_server" in graph
        if has_command_server:
            return {
                name: {"state": "AMBIGUOUS", "owner": None}
                for name in ("robot", "controller", "gripper")
            }
        if has_manager:
            listing = controller_listing()
            active = _controller_names(listing)
            if EXPECTED_CONTROLLERS <= active:
                try:
                    projection = gripper_setup_projection(
                        gripper_readback_call(graph, listing),
                    )
                except ContractError as exc:
                    if (
                        exc.code == "GRIPPER_SETUP_READBACK"
                        and owned_running("motion")
                    ):
                        return {
                            name: {"state": "MISSING", "owner": None}
                            for name in ("robot", "controller", "gripper")
                        }
                    raise
                gripper_state = {
                    "ATTACHED": "READY",
                    "MAINTENANCE_APPROVAL_REQUIRED": "SETUP_REQUIRED",
                }.get(projection["state"], "AMBIGUOUS")
                return {
                    "robot": {"state": "READY", "owner": MOTION_OWNER},
                    "controller": {"state": "READY", "owner": MOTION_OWNER},
                    "gripper": {
                        "state": gripper_state,
                        "owner": MOTION_OWNER if gripper_state != "AMBIGUOUS" else None,
                    },
                }
            else:
                state = "MISSING" if owned_running("motion") else "AMBIGUOUS"
                return {
                    name: {"state": state, "owner": None}
                    for name in ("robot", "controller", "gripper")
                }
        return {
            name: {"state": "MISSING", "owner": None}
            for name in ("robot", "controller", "gripper")
        }

    def camera_state(camera_states: Sequence[str]) -> dict[str, object]:
        state = (
            "MISSING" if not camera_states
            else "READY" if all(item == "READY" for item in camera_states)
            else "MISSING" if all(item == "MISSING" for item in camera_states)
            else "MISSING" if (
                owned_running("camera")
                and set(camera_states) <= {"READY", "MISSING"}
            )
            else "AMBIGUOUS"
        )
        return {
            "state": state,
            "owner": CAMERA_OWNER if state == "READY" else None,
        }

    def discover() -> dict[str, dict[str, object]]:
        with discovery_lock:
            with ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="operator-graph",
            ) as executor:
                graph_future = executor.submit(nodes)
                topics_future = executor.submit(topics)
                graph = graph_future.result()
                graph_topics = topics_future.result()
            specs = tuple(camera_config["specs"])
            with ThreadPoolExecutor(
                max_workers=1 + len(specs), thread_name_prefix="operator-gate",
            ) as executor:
                motion_future = executor.submit(motion_state, graph)
                camera_futures = [
                    executor.submit(role_camera_state, spec, graph, graph_topics)
                    for spec in specs
                ]
                motion = motion_future.result()
                camera_states = [future.result() for future in camera_futures]
            motion["camera"] = camera_state(camera_states)
            return motion

    def tracked_spawn(argv: tuple[str, ...]) -> object:
        process = spawn(argv)
        if "real_robot.launch.py" in argv:
            owned_children["motion"] = process
        elif str(repository / "scripts/start_camera_group.sh") in argv:
            owned_children["camera"] = process
        return process

    commands = {
        "camera_group": _camera_command(repository, collection_profile, camera_specs),
        "robot_stack": {
            "argv": (
                "env", f"FR5_GRIPPER_FORCE={gripper_force_percent}",
                f"FR5_GRIPPER_OPEN_FORCE={gripper_open_force_percent}",
                "ros2", "launch", "fairino5_v6_moveit2_config",
                "real_robot.launch.py", "use_fake_hardware:=false", "use_rviz:=false",
            ),
            "owner": MOTION_OWNER,
            "provides": ("robot", "controller", "gripper"),
        },
    }

    def setup_attached_gripper(_facts: dict[str, dict[str, object]]) -> None:
        readback = gripper_readback_call(None, None)
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
            readback = gripper_readback_call(None, None)
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
def _bounded_command(command: list[str], code: str, *, timeout_s: float = 5) -> str:
    try:
        completed = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout_s, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(code) from exc
    if completed.returncode != 0:
        raise ContractError(code)
    return completed.stdout


def _readonly_command(command: list[str], code: str) -> str:
    return _bounded_command(command, code)


def _controller_names(value: str) -> set[str]:
    return {
        fields[0]
        for line in value.splitlines()
        if (fields := line.split()) and "active" in fields
    }


def _remote_gripper_command(command: str, *, expected_fields: int) -> list[int]:
    output = _bounded_command([
        "ros2", "service", "call", "/fairino_remote_command_service",
        "fairino_msgs/srv/RemoteCmdInterface",
        json.dumps({"cmd_str": command}, separators=(",", ":")),
    ], "GRIPPER_MAINTENANCE_SERVICE", timeout_s=35)
    match = re.search(r"cmd_res(?:=|:)\s*['\"]?(-?\d+(?:,-?\d+)*)", output)
    if match is None:
        raise ContractError("GRIPPER_MAINTENANCE_RESPONSE")
    result = [int(value) for value in match.group(1).split(",")]
    if len(result) != expected_fields:
        raise ContractError("GRIPPER_MAINTENANCE_RESPONSE")
    return result


def capture_gripper_setup_readback(
    node_names: set[str] | None = None,
    controller_listing: str | None = None,
) -> dict[str, Any]:
    """Read one fresh gripper source without opening a second SDK owner."""
    if (node_names is None) != (controller_listing is None):
        raise ContractError("GRIPPER_SETUP_NODE_GRAPH")
    nodes = (
        set(
            line.strip()
            for line in _readonly_command(
                [
                    "ros2", "node", "list", "--no-daemon",
                    "--spin-time", ROS_DISCOVERY_SPIN_SECONDS,
                ],
                "GRIPPER_SETUP_NODE_GRAPH",
            ).splitlines()
            if line.strip()
        )
        if node_names is None else set(node_names)
    )
    command_server = "/fr_command_server" in nodes
    listing = (
        (
            ""
            if command_server and "/controller_manager" not in nodes
            else _readonly_command(
                ["ros2", "control", "list_controllers"],
                "GRIPPER_SETUP_CONTROLLER_GRAPH",
            )
        )
        if controller_listing is None else controller_listing
    )
    controllers = _controller_names(listing)
    normal = {
        "fairino5_controller", "gripper_controller", "joint_state_broadcaster",
    } <= controllers
    if normal and command_server:
        raise ContractError("PHYSICAL_SECOND_MOTION_OWNER")
    if normal:
        output = _readonly_command([
            "ros2", "topic", "echo", "/gripper_controller/controller_state",
            "control_msgs/msg/JointTrajectoryControllerState", "--once",
            "--timeout", "2", "--flow-style", "--no-daemon",
        ], "GRIPPER_SETUP_READBACK")
        try:
            import yaml
        except ImportError as exc:
            raise ContractError("GRIPPER_SETUP_READBACK") from exc
        message_start = re.search(r"(?m)^joint_names:\s*", output)
        if message_start is None:
            raise ContractError("GRIPPER_SETUP_READBACK")
        try:
            message = next(yaml.safe_load_all(output[message_start.start():]))
            names = message["joint_names"]
            reference = message["reference"]["positions"]
            feedback = message["feedback"]["positions"]
            if names != ["finger_right_joint"] or len(reference) != 1 or len(feedback) != 1:
                raise ValueError
            reference_m, feedback_m = float(reference[0]), float(feedback[0])
        except (KeyError, StopIteration, TypeError, ValueError, yaml.YAMLError) as exc:
            raise ContractError("GRIPPER_SETUP_READBACK") from exc
        if not all(math.isfinite(value) for value in (reference_m, feedback_m)):
            raise ContractError("GRIPPER_SETUP_READBACK")
        return {
            "active": True, "position_valid": True, "gripper_index": 1,
            "reference_position_m": reference_m,
            "feedback_position_m": feedback_m,
            "sample_age_s": 0.0, "max_age_s": 0.1,
            "source": "CONTROLLER_STATE",
        }
    if command_server and not listing.strip():
        activation = _remote_gripper_command(
            "GetGripperActivateStatus()", expected_fields=3,
        )
        position = _remote_gripper_command(
            "GetGripperCurPosition()", expected_fields=3,
        )
        active = activation[0] == 0 and activation[1] == 0 and activation[2] & 1 == 1
        valid = position[0] == 0 and position[1] == 0 and 0 <= position[2] <= 100
        position_m = 0.021 * position[2] / 100 if valid else None
        return {
            "active": active, "position_valid": valid, "gripper_index": 1,
            "reference_position_m": position_m,
            "feedback_position_m": position_m,
            "sample_age_s": 0.0, "max_age_s": 0.1,
            "source": "COMMAND_SERVER_MAINTENANCE",
        }
    raise ContractError("GRIPPER_SETUP_NOT_AVAILABLE")


def normalize_gripper_after_operator_ready(
    readback: Mapping[str, Any], *, settle_call: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    """Perform the one approved open-normalization branch; never manage processes."""
    source = readback.get("source") if isinstance(readback, Mapping) else None
    if source == "CONTROLLER_STATE":
        goal = {
            "trajectory": {
                "joint_names": ["finger_right_joint"],
                "points": [{
                    "positions": [0.021],
                    "time_from_start": {"sec": 2, "nanosec": 0},
                }],
            },
            "goal_tolerance": [{"name": "finger_right_joint", "position": 0.000105}],
            "goal_time_tolerance": {"sec": 5, "nanosec": 0},
        }
        output = _bounded_command([
            "ros2", "action", "send_goal",
            "/gripper_controller/follow_joint_trajectory",
            "control_msgs/action/FollowJointTrajectory",
            json.dumps(goal, separators=(",", ":")),
        ], "GRIPPER_MAINTENANCE_ACTION", timeout_s=15)
        if (
            "Goal finished with status: SUCCEEDED" not in output
            or re.search(r"error_code(?:=|:)\s*0\b", output) is None
        ):
            raise ContractError("GRIPPER_MAINTENANCE_ACTION")
        return {"status": "NORMALIZED", "requires_graph_switch": False}
    if source == "COMMAND_SERVER_MAINTENANCE":
        activation = _remote_gripper_command(
            "GetGripperActivateStatus()", expected_fields=3,
        )
        if activation[0] != 0:
            raise ContractError("GRIPPER_MAINTENANCE_ACTION")
        if activation[1] != 0:
            if _remote_gripper_command(
                "ResetAllError()", expected_fields=1,
            ) != [0]:
                raise ContractError("GRIPPER_MAINTENANCE_ACTION")
            activation = _remote_gripper_command(
                "GetGripperActivateStatus()", expected_fields=3,
            )
            if activation[0] != 0 or activation[1] != 0:
                raise ContractError("GRIPPER_MAINTENANCE_ACTION")
        if activation[2] & 1 != 1:
            if _remote_gripper_command(
                "ActGripper(1,0)", expected_fields=1,
            ) != [0]:
                raise ContractError("GRIPPER_MAINTENANCE_ACTION")
            settle_call(1.0)
            if _remote_gripper_command(
                "ActGripper(1,1)", expected_fields=1,
            ) != [0]:
                raise ContractError("GRIPPER_MAINTENANCE_ACTION")
            settle_call(2.0)
        if _remote_gripper_command("MoveGripper(1,100)", expected_fields=1) != [0]:
            raise ContractError("GRIPPER_MAINTENANCE_ACTION")
        done = _remote_gripper_command("GetGripperMotionDone()", expected_fields=3)
        position = _remote_gripper_command("GetGripperCurPosition()", expected_fields=3)
        if done != [0, 0, 1] or position != [0, 0, 100]:
            raise ContractError("GRIPPER_MAINTENANCE_ACTION")
        return {"status": "NORMALIZED", "requires_graph_switch": True}
    raise ContractError("GRIPPER_MAINTENANCE_NOT_AVAILABLE")


def passive_physical_gate(
    *, camera_topic: str, discovered_device_id: str,
    camera_node: str = "/camera/up/color/uvc_up_camera",
    device_kind: str = "UVC", capture_endpoint: str | None = None,
    device_root: str | Path = "/dev/v4l/by-id",
    discovery_call: Callable[[], Sequence[object]] = discover_uvc_device_ids,
) -> dict[str, Any]:
    """Attach only to an already-running graph; perform no lifecycle mutation."""
    def read_graph(args: list[str], code: str) -> str:
        for attempt in range(3):
            try:
                return _readonly_command(args, code)
            except ContractError:
                if attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))
        raise AssertionError("unreachable")

    discovered = normalize_camera_devices(
        discovery_call(), default_kind=device_kind,
    )
    matches = [
        item for item in discovered
        if item["logical_id"] == discovered_device_id
        and item["kind"] == device_kind
    ]
    if not matches:
        raise ContractError("PHYSICAL_CAMERA_BINDING")
    if len(matches) != 1:
        raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
    endpoint = capture_endpoint or matches[0]["capture_endpoint"]
    if endpoint != matches[0]["capture_endpoint"]:
        raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
    stable_target = None
    if device_kind == "UVC":
        stable_path = Path(device_root) / discovered_device_id
        try:
            stable_target = stable_path.resolve(strict=True)
        except OSError as exc:
            raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH") from exc
        if not stable_path.is_symlink() or not stat.S_ISCHR(stable_target.stat().st_mode):
            raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
    controllers = read_graph(
        ["ros2", "control", "list_controllers"], "PHYSICAL_CONTROLLER_GRAPH",
    )
    for name in ("fairino5_controller", "gripper_controller", "joint_state_broadcaster"):
        if not any(line.split()[:1] == [name] and "active" in line.split() for line in controllers.splitlines()):
            raise ContractError("PHYSICAL_CONTROLLER_STATE_MISMATCH")
    nodes = read_graph(
        [
            "ros2", "node", "list", "--no-daemon",
            "--spin-time", ROS_DISCOVERY_SPIN_SECONDS,
        ],
        "PHYSICAL_NODE_GRAPH",
    )
    if "/fr_command_server" in nodes.splitlines():
        raise ContractError("PHYSICAL_SECOND_MOTION_OWNER")
    if read_graph(
        [
            "ros2", "topic", "type", "/joint_states", "--no-daemon",
            "--spin-time", ROS_DISCOVERY_SPIN_SECONDS,
        ],
        "PHYSICAL_JOINT_TOPIC",
    ).strip() != "sensor_msgs/msg/JointState":
        raise ContractError("PHYSICAL_JOINT_TOPIC_MISMATCH")
    if read_graph(
        [
            "ros2", "topic", "type", camera_topic, "--no-daemon",
            "--spin-time", ROS_DISCOVERY_SPIN_SECONDS,
        ],
        "PHYSICAL_CAMERA_TOPIC",
    ).strip() != "sensor_msgs/msg/Image":
        raise ContractError("PHYSICAL_CAMERA_TOPIC_MISMATCH")
    if device_kind == "UVC":
        reported = read_graph(
            [
                "ros2", "param", "get", camera_node, "video_device",
                "--hide-type", "--no-daemon",
            ],
            "PHYSICAL_CAMERA_DEVICE_PARAMETER",
        ).strip()
        try:
            configured_target = Path(reported).resolve(strict=True)
        except OSError as exc:
            raise ContractError("PHYSICAL_CAMERA_DEVICE_MISMATCH") from exc
        if configured_target != stable_target:
            raise ContractError("PHYSICAL_CAMERA_DEVICE_MISMATCH")
        resolved_device = str(stable_target)
    elif device_kind == "REALSENSE":
        parameters = {
            name: read_graph(
                [
                    "ros2", "param", "get", camera_node, name,
                    "--hide-type", "--no-daemon",
                ],
                "PHYSICAL_CAMERA_DEVICE_PARAMETER",
            ).strip().strip('"')
            for name in ("serial_no", "enable_color", "enable_depth")
        }
        if (
            parameters["serial_no"].lstrip("_") != discovered_device_id
            or parameters["enable_color"].lower() != "true"
            or parameters["enable_depth"].lower() != "false"
        ):
            raise ContractError("PHYSICAL_CAMERA_DEVICE_MISMATCH")
        reported = parameters["serial_no"]
        resolved_device = endpoint
    else:
        raise ContractError("PHYSICAL_CAMERA_BINDING_MISMATCH")
    evidence = {
        "schema_version": "data_factory.test_only_camera_transport_binding.v1",
        "device_kind": device_kind,
        "stable_device_id": discovered_device_id,
        "resolved_device": resolved_device,
        "camera_node": camera_node,
        "camera_topic": camera_topic,
        "reported_video_device": reported,
        "topic_type": "sensor_msgs/msg/Image",
        "authority": "TEST_ONLY_TRANSPORT",
    }
    evidence["binding_digest"] = canonical_digest(evidence)
    return evidence


def capture_home_snapshot(
    *, tcp_candidate_manifest: Path, max_age_s: float = 0.1,
) -> dict[str, Any]:
    """Capture a fresh ROS snapshot with one bounded transient retry."""
    command = [
        sys.executable, "-m", "tools.data_factory.motion.pose_snapshot", "capture",
        "--timeout-s", "2", "--max-age-s", str(max_age_s),
        "--tcp-candidate-manifest", str(tcp_candidate_manifest),
    ]
    for attempt in range(2):
        try:
            output = _readonly_command(command, "PHYSICAL_HOME_SNAPSHOT")
        except ContractError:
            if attempt:
                raise
        else:
            return load_json_strict(output.strip())
    raise AssertionError("unreachable")
