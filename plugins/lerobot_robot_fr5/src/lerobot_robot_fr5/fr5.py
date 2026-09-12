from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from lerobot.robots import Robot

from .config_fr5 import FR5Config
from .observation import FR5RosObservationBridge


class FR5(Robot):
    """Thin LeRobot boundary around the qualified FR5 ROS stack."""

    config_class = FR5Config
    name = "fr5"

    JOINTS = (
        "j1",
        "j2",
        "j3",
        "j4",
        "j5",
        "j6",
        "finger_right_joint",
    )

    CAMERA_TOPICS = (
        "/camera/up/color/image_raw",
        "/camera/wrist/color/image_raw",
    )

    def __init__(self, config: FR5Config):
        super().__init__(config)
        self.config = config

        self._camera_proc: subprocess.Popen | None = None
        self._robot_proc: subprocess.Popen | None = None
        self._camera_log = None
        self._robot_log = None
        self._connected = False
        self._ros_env: dict[str, str] | None = None
        self._evidence_sink = None
        self._pending_evidence = []
        self._observation_bridge = None

    def _record_evidence(self, kind, payload) -> None:
        if self._evidence_sink is not None:
            self._evidence_sink.emit(kind, payload)
            return

        if len(self._pending_evidence) >= 16:
            raise RuntimeError("FR5_EVIDENCE_BUFFER_OVERFLOW")

        self._pending_evidence.append((kind, payload))

    def attach_evidence_sink(self, sink) -> None:
        if self._evidence_sink is not None:
            raise RuntimeError("FR5_EVIDENCE_SINK_ALREADY_ATTACHED")

        self._evidence_sink = sink

        for kind, payload in self._pending_evidence:
            sink.emit(kind, payload)

        self._pending_evidence.clear()

    @property
    def observation_features(self) -> dict[str, type | tuple]:
        return {
            **{f"{joint}.pos": float for joint in self.JOINTS},
            "up": (480, 640, 3),
            "wrist": (480, 640, 3),
        }

    @property
    def action_features(self) -> dict[str, type]:
        return {f"{joint}.pos": float for joint in self.JOINTS}

    @property
    def is_connected(self) -> bool:
        return (
            self._connected
            and self._camera_proc is not None
            and self._camera_proc.poll() is None
            and self._robot_proc is not None
            and self._robot_proc.poll() is None
        )

    def _build_ros_env(self) -> dict[str, str]:
        cfg = self.config

        required = (
            Path(cfg.hardware_overlay) / "local_setup.bash",
            Path(cfg.config_overlay) / "local_setup.bash",
            Path(cfg.sdk_dir) / "libfairino.so.2.3.7",
            Path(cfg.robot_model_file),
            Path(cfg.project_root) / "scripts/start_camera_group.sh",
        )
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise RuntimeError(f"FR5_RUNTIME_PATH_MISSING: {missing}")

        script = f"""
unset AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH
source /opt/ros/jazzy/setup.bash
source {shlex.quote(str(Path(cfg.config_overlay) / "local_setup.bash"))}
source {shlex.quote(str(Path(cfg.hardware_overlay) / "local_setup.bash"))}
env -0
"""
        result = subprocess.run(
            ["bash", "-c", script],
            check=True,
            stdout=subprocess.PIPE,
        )

        env: dict[str, str] = {}
        for entry in result.stdout.split(b"\0"):
            if not entry or b"=" not in entry:
                continue
            key, value = entry.split(b"=", 1)
            env[key.decode()] = value.decode()

        old_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = (
            cfg.sdk_dir if not old_ld else f"{cfg.sdk_dir}:{old_ld}"
        )
        env["FR5_REQUIRE_GRIPPER_SOURCE_CLOCK"] = "true"
        env["FR5_REQUIRE_COHERENT_SNAPSHOT"] = "true"

        checks = (
            (
                "fairino_hardware_v3_9_7",
                str(Path(cfg.hardware_overlay) / "fairino_hardware_v3_9_7"),
            ),
            (
                "fairino5_v6_moveit2_config",
                str(Path(cfg.config_overlay) / "fairino5_v6_moveit2_config"),
            ),
        )
        for package, expected in checks:
            actual = subprocess.check_output(
                ["ros2", "pkg", "prefix", package],
                env=env,
                text=True,
            ).strip()
            if actual != expected:
                raise RuntimeError(
                    f"FR5_OVERLAY_MISMATCH: {package}: "
                    f"{actual} != {expected}"
                )

        return env

    @staticmethod
    def _terminate_group(proc: subprocess.Popen | None) -> None:
        if proc is None:
            return

        # start_new_session=True makes proc.pid the process-group id.
        # The leader may already have exited while ROS descendants remain.
        pgid = proc.pid

        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass

    def _assert_no_existing_camera_stack(self) -> None:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        )

        conflicts = []

        for line in result.stdout.splitlines():
            is_wrist = "uvc_wrist_camera" in line

            is_up = (
                "realsense2_camera" in line
                and (
                    "camera_name:=up" in line
                    or "__node:=up" in line
                    or f"serial_no:=_{self.config.realsense_serial}" in line
                )
            )

            is_group = (
                "start_camera_group.sh" in line
                and self.config.realsense_serial in line
            )

            if is_wrist or is_up or is_group:
                conflicts.append(line.strip())

        if conflicts:
            raise RuntimeError(
                "FR5_CAMERA_STACK_ALREADY_RUNNING:\n"
                + "\n".join(conflicts)
            )

    def _wait_camera_topics(self, env: dict[str, str]) -> None:
        deadline = time.monotonic() + self.config.startup_timeout_s

        while time.monotonic() < deadline:
            if self._camera_proc is None or self._camera_proc.poll() is not None:
                raise RuntimeError("FR5_CAMERA_GROUP_EXITED")

            ready = True
            for topic in self.CAMERA_TOPICS:
                result = subprocess.run(
                    ["ros2", "topic", "info", topic],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                ready &= result.returncode == 0

            if ready:
                return

            time.sleep(0.25)

        raise RuntimeError("FR5_CAMERA_STARTUP_TIMEOUT")

    def _wait_robot_stack(self, env: dict[str, str]) -> None:
        deadline = time.monotonic() + self.config.startup_timeout_s

        while time.monotonic() < deadline:
            if self._robot_proc is None or self._robot_proc.poll() is not None:
                raise RuntimeError("FR5_ROBOT_STACK_EXITED")

            result = subprocess.run(
                ["ros2", "node", "list"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if (
                result.returncode == 0
                and "controller_manager" in result.stdout
            ):
                return

            time.sleep(0.25)

        raise RuntimeError("FR5_ROBOT_STARTUP_TIMEOUT")

    def connect(self, calibrate: bool = True) -> None:
        if not self.config.physical_io_enabled:
            raise RuntimeError(
                "FR5_PHYSICAL_IO_REQUIRES_EXPLICIT_ENABLE"
            )

        if self.is_connected:
            return

        env = self._build_ros_env()
        self._ros_env = env

        self._assert_no_existing_camera_stack()

        work = (
            Path(self.config.project_root)
            / ".agent-local/work/lerobot-fr5"
        )
        work.mkdir(parents=True, exist_ok=True)

        try:
            self._camera_log = open(work / "camera-group.log", "ab")
            self._robot_log = open(work / "real-robot.log", "ab")

            camera_cmd = [
                str(
                    Path(self.config.project_root)
                    / "scripts/start_camera_group.sh"
                ),
                "up",
                "REALSENSE",
                self.config.realsense_serial,
                "wrist",
                "UVC",
                self.config.wrist_uvc,
            ]

            self._camera_proc = subprocess.Popen(
                camera_cmd,
                cwd=self.config.project_root,
                env=env,
                stdout=self._camera_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._wait_camera_topics(env)

            if self._camera_proc.poll() is not None:
                raise RuntimeError("FR5_CAMERA_GROUP_EXITED")

            robot_cmd = [
                "ros2",
                "launch",
                "fairino5_v6_moveit2_config",
                "real_robot.launch.py",
                "use_fake_hardware:=false",
                f"robot_model_file:={self.config.robot_model_file}",
            ]

            self._robot_proc = subprocess.Popen(
                robot_cmd,
                cwd=self.config.project_root,
                env=env,
                stdout=self._robot_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._wait_robot_stack(env)

            if self._camera_proc.poll() is not None:
                raise RuntimeError("FR5_CAMERA_GROUP_EXITED")
            if self._robot_proc.poll() is not None:
                raise RuntimeError("FR5_ROBOT_STACK_EXITED")

            self._observation_bridge = FR5RosObservationBridge(
                joint_topic=self.config.joint_state_topic,
                up_topic=self.config.up_topic,
                wrist_topic=self.config.wrist_topic,
                max_age_s=self.config.observation_max_age_s,
                timeout_s=self.config.observation_timeout_s,
                initial_timeout_s=self.config.startup_timeout_s,
            )
            self._observation_bridge.start()

            self._connected = True

            if not self.is_connected:
                raise RuntimeError("FR5_CONNECTION_HEALTH")

        except Exception:
            self.disconnect()
            raise

    @property
    def is_calibrated(self) -> bool:
        return True

    def calibrate(self) -> None:
        pass

    def configure(self) -> None:
        pass

    def get_observation(self) -> dict[str, Any]:
        if not self._connected:
            raise ConnectionError("FR5_PLUGIN_NOT_CONNECTED")

        if self._camera_proc is None or self._camera_proc.poll() is not None:
            raise RuntimeError("FR5_CAMERA_GROUP_EXITED")

        if self._robot_proc is None or self._robot_proc.poll() is not None:
            raise RuntimeError("FR5_ROBOT_STACK_EXITED")

        if self._observation_bridge is None:
            raise RuntimeError("FR5_OBSERVATION_BRIDGE_NOT_STARTED")

        observation, evidence = self._observation_bridge.read()
        self._record_evidence("OBSERVATION", evidence)
        return observation

    def send_action(self, action: dict[str, Any]) -> dict[str, Any]:
        # Deliberately fail closed until the FR5 action processor,
        # chunk safety strategy, and actual-sent-action evidence are bound.
        raise RuntimeError("FR5_ACTION_NOT_AUTHORIZED")

    def disconnect(self) -> None:
        if self._observation_bridge is not None:
            self._observation_bridge.close()
            self._observation_bridge = None

        # Stop robot/control first, then camera producers.
        self._terminate_group(self._robot_proc)
        self._terminate_group(self._camera_proc)

        self._robot_proc = None
        self._camera_proc = None
        self._connected = False
        self._ros_env = None

        for handle_name in ("_robot_log", "_camera_log"):
            handle = getattr(self, handle_name, None)
            if handle is not None:
                try:
                    handle.close()
                finally:
                    setattr(self, handle_name, None)
