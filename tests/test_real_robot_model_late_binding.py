from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
LAUNCH = (
    ROOT
    / "src/fairino5_v6_moveit2_config/launch/real_robot.launch.py"
)


class RealRobotModelLateBindingTest(unittest.TestCase):
    def test_robot_model_selection_is_resolved_in_launch_context(self):
        text = LAUNCH.read_text()

        self.assertIn("OpaqueFunction", text)
        self.assertIn("def _launch_setup(context):", text)
        self.assertIn(
            '"robot_model_file": LaunchConfiguration(',
            text,
        )
        self.assertIn(
            '"robot_model_file"\n            ).perform(context)',
            text,
        )
        self.assertIn(
            "OpaqueFunction(function=_launch_setup)",
            text,
        )


if __name__ == "__main__":
    unittest.main()
