from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare
from moveit_configs_utils import MoveItConfigsBuilder
from moveit_configs_utils.launches import generate_moveit_rviz_launch


def generate_launch_description():
    declare_use_fake_hardware = DeclareLaunchArgument(
        "use_fake_hardware", default_value="true"
    )
    declare_robot_model = DeclareLaunchArgument(
        "robot_model_file",
        default_value=PathJoinSubstitution([
            FindPackageShare("fairino_description"),
            "urdf",
            "fairino5_v6.urdf",
        ]),
        description="URDF model selection only; does not grant motion qualification.",
    )

    moveit_config = (
        MoveItConfigsBuilder(
            "fairino5_v6_robot",
            package_name="fairino5_v6_moveit2_config",
        )
        .robot_description(
            mappings={
                "use_fake_hardware": LaunchConfiguration(
                    "use_fake_hardware"
                ),
                "robot_model_file": LaunchConfiguration(
                    "robot_model_file"
                ),
            }
        )
        .to_moveit_configs()
    )

    ld = LaunchDescription()
    ld.add_action(declare_use_fake_hardware)
    ld.add_action(declare_robot_model)

    for action in generate_moveit_rviz_launch(moveit_config).entities:
        ld.add_action(action)

    return ld
