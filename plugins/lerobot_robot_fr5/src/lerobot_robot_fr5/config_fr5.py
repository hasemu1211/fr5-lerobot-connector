from dataclasses import dataclass

from lerobot.robots import RobotConfig


@RobotConfig.register_subclass("fr5")
@dataclass(kw_only=True)
class FR5Config(RobotConfig):
    robot_system_id: str = "fr5-lab-a-tcp-r002"

    # Fail closed. Must be explicitly enabled for physical HIL.
    physical_io_enabled: bool = False

    gripper_upper_m: float = 0.021
    gripper_projection_quanta: int = 2

    project_root: str = "/home/codelab/Desktop/Project/fr5_ws"
    hardware_overlay: str = "/tmp/fr5-hil-correct-install"
    config_overlay: str = "/tmp/fr5-config-latebind-install"
    sdk_dir: str = (
        "/home/codelab/Desktop/Project/fr5_ws/"
        ".agent-local/work/controller-clock-probe/"
        "sdk-source-0553c35/libfairino/LinuxBuild/bin"
    )

    robot_model_file: str = (
        "/home/codelab/Desktop/Project/fr5_ws/src/"
        "fairino_description/urdf/"
        "fairino5_v6_gripper_opening_candidate.urdf"
    )

    realsense_serial: str = "254622073507"
    wrist_uvc: str = (
        "/dev/v4l/by-id/"
        "usb-Generic_USB2.0_PC_CAMERA-video-index0"
    )

    startup_timeout_s: float = 20.0

    joint_state_topic: str = "/joint_states"
    up_topic: str = "/camera/up/color/image_raw"
    wrist_topic: str = "/camera/wrist/color/image_raw"
    observation_max_age_s: float = 0.3
    observation_timeout_s: float = 1.0
