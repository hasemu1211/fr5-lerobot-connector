from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare
from moveit_configs_utils import MoveItConfigsBuilder
from moveit_configs_utils.launches import generate_demo_launch


def _launch_setup(context):
    moveit_config = MoveItConfigsBuilder(
        "fairino5_v6_robot", package_name="fairino5_v6_moveit2_config"
    ).robot_description(
        mappings={
            "use_fake_hardware": LaunchConfiguration(
                "use_fake_hardware"
            ).perform(context),
            "robot_model_file": LaunchConfiguration(
                "robot_model_file"
            ).perform(context),
        }
    ).to_moveit_configs()
    return list(generate_demo_launch(moveit_config).entities)


def generate_launch_description():
    declare_use_fake_hardware = DeclareLaunchArgument(
        "use_fake_hardware", default_value="false"
    )
    declare_robot_model = DeclareLaunchArgument(
        "robot_model_file",
        default_value=PathJoinSubstitution([
            FindPackageShare("fairino_description"), "urdf", "fairino5_v6.urdf"
        ]),
        description="URDF model selection only; does not grant motion qualification.",
    )
    return LaunchDescription([
        declare_use_fake_hardware,
        declare_robot_model,
        OpaqueFunction(function=_launch_setup),
    ])
