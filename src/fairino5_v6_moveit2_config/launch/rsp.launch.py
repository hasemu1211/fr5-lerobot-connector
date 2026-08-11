from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from moveit_configs_utils import MoveItConfigsBuilder
from moveit_configs_utils.launches import generate_rsp_launch


def generate_launch_description():
    declare_use_fake_hardware = DeclareLaunchArgument(
        "use_fake_hardware", default_value="true"
    )
    moveit_config = (
        MoveItConfigsBuilder("fairino5_v6_robot", package_name="fairino5_v6_moveit2_config")
        .robot_description(mappings={"use_fake_hardware": LaunchConfiguration("use_fake_hardware")})
        .to_moveit_configs()
    )
    ld = LaunchDescription()
    ld.add_action(declare_use_fake_hardware)
    for action in generate_rsp_launch(moveit_config).entities:
        ld.add_action(action)
    return ld
