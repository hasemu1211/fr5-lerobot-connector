"""Render the real launch URDF without starting ROS or hardware."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import xacro


class HardwareConfigurationTest(unittest.TestCase):
    def test_source_clock_is_explicit_opt_in_and_absent_from_fake_hardware(self):
        source = Path(__file__).resolve().parents[3] / "src"
        config = source / "fairino5_v6_moveit2_config/config"
        for setting, fake, expected in ((None, False, "false"), ("true", False, "true"),
                                        ("true", True, None)):
            with self.subTest(setting=setting, fake=fake), patch.dict(os.environ):
                os.environ.pop("FR5_REQUIRE_GRIPPER_SOURCE_CLOCK", None)
                if setting is not None:
                    os.environ["FR5_REQUIRE_GRIPPER_SOURCE_CLOCK"] = setting
                # Resolve the real description from this checkout, not a stale
                # or absent colcon install overlay. Xacro rendering stays real.
                with patch("ament_index_python.packages.get_package_share_directory",
                           return_value=str(source / "fairino_description")) as find_package:
                    document = xacro.process_file(
                        str(config / "fairino5_v6_robot.urdf.xacro"),
                        mappings={"initial_positions_file": str(config / "initial_positions.yaml"),
                                  "use_fake_hardware": str(fake).lower()},
                    )
                find_package.assert_called_once_with("fairino_description")
                hardware = ET.fromstring(document.toxml()).find("ros2_control/hardware")
                self.assertIsNotNone(hardware)
                self.assertEqual(hardware.findtext("param[@name='require_gripper_source_clock']"), expected)
                self.assertEqual(hardware.findtext("plugin"),
                                 "mock_components/GenericSystem" if fake
                                 else "fairino_hardware/FairinoHardwareInterface")
                self.assertIsNone(hardware.find("param[@name='gripper_source_clock_v1']"))


if __name__ == "__main__":
    unittest.main()
