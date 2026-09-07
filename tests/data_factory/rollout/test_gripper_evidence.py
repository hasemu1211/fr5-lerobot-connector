"""CPU native-method extraction and real ROS serializer acceptance; no ROS init."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.fr5_data_factory import ContractError
from tools.data_factory.rollout.gripper_evidence import (
    FIELDS, RESOURCE, check_hardware, decode_dynamic_state, validate_clock_binding,
)

ROOT = Path(__file__).resolve().parents[3]


def added_source(path):
    patch = (ROOT / "patches/frcobot_ros2.patch").read_text()
    section = patch.split(f"diff --git a/{path} b/{path}\n", 1)[1].split("diff --git ", 1)[0]
    return "".join(line[1:] for line in section.splitlines(True) if line.startswith("+") and not line.startswith("+++"))


def method(source, name):
    start = source.index("void FairinoHardwareInterface::" + name + "(")
    opening = source.index("{", start)
    depth = 1
    for end in range(opening + 1, len(source)):
        depth += (source[end] == "{") - (source[end] == "}")
        if not depth:
            return source[start:end + 1] + "\n"
    raise AssertionError("native method incomplete")


class NativeGripperEvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        path = Path(cls.temp.name)
        header = added_source("fairino_hardware_v3_9_7/include/fairino_hardware/gripper_execution_evidence.hpp")
        (path / "gripper_execution_evidence.hpp").write_text(header)
        source = added_source("fairino_hardware_v3_9_7/src/fairino_hardware_interface.cpp")
        native = method(source, "gripper_worker") + method(source, "sample_gripper_evidence")
        fixture = Path(__file__).with_name("gripper_native_fixture.cpp").read_text()
        (path / "native.cpp").write_text(fixture.replace("// NATIVE_METHODS", native))
        cls.binary = path / "native"
        result = subprocess.run(["g++", "-std=c++17", "-pthread", str(path / "native.cpp"), "-o", str(cls.binary)], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stderr)

    def run_native(self, scenario):
        result = subprocess.run([str(self.binary), scenario], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_actual_worker_stop_error_supersession_and_completion(self):
        for scenario in ("completed", "settled", "cached", "superseded", "stop_before_move", "error_before_move",
                         "stop_during_move", "stop_before_resume", "stop_during_resume", "move_error", "resume_error"):
            with self.subTest(scenario=scenario):
                self.run_native(scenario)

    def test_invalid_clock_file_rejects_before_ros_initialization(self):
        from tools.data_factory.motion.pickup_executor import main
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clock.json"
            path.write_text('{"schema_version":"unknown"}')
            with mock.patch("rclpy.init") as init, mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit) as caught:
                    main(["--factory-jsonl", "--ros-plan-only", "--gripper-source-clock", str(path)])
            self.assertEqual(caught.exception.code, 2)
            init.assert_not_called()

    def test_native_export_round_trip_and_cached_completion_rejection(self):
        from control_msgs.msg import DynamicJointState, InterfaceValue
        from rclpy.serialization import deserialize_message, serialize_message
        for scenario in ("completed", "cached"):
            packet = self.run_native(scenario)
            message = DynamicJointState(joint_names=[RESOURCE], interface_values=[InterfaceValue(
                interface_names=packet["names"], values=packet["wire"])])
            self.assertEqual(tuple(packet["names"]), FIELDS)
            wire = dict(zip(FIELDS, packet["wire"]))
            mapping = {"schema_version": "fr5.gripper_source_clock.v1",
                       "incarnation": [wire[f"incarnation_{i}"] for i in range(4)],
                       "calendar_to_system_offset_s": 0., "uncertainty_s": .001,
                       "system_anchor_s": 10., "steady_anchor_s": 20., "valid_until_system_s": 11.}
            decoded = decode_dynamic_state(deserialize_message(serialize_message(message), DynamicJointState), mapping, 20.2)
            self.assertEqual(list(decoded["wire"].values()), packet["wire"])
            # Worker uses actual host time for command start; this synthetic replay
            # maps its command instant to 10.0 while retaining exact native source calendar.
            decoded["wire"]["command_started_system_s"] = 10.
            evidence = {"captured_at_s": 10.2, "captured_monotonic_s": 20.2,
                        "snapshot": {"gripper_controller": {"hardware_execution": decoded}}}
            if scenario == "completed":
                self.assertEqual(check_hardware(evidence, 10.2, 20.2, .3, completion=True)["completed_generation"], 1)
            else:
                # Fresh local sample/receipt never repairs the pre-command completion frame.
                with self.assertRaisesRegex(ContractError, "LEARNED_HARDWARE_COMPLETION"):
                    check_hardware(evidence, 10.2, 20.2, .3, completion=True)
            for changes, expected in (({"incarnation": [0, 0, 0, 0]}, "LEARNED_HARDWARE_CLOCK_BINDING"),
                                      ({"uncertainty_s": float("nan")}, "LEARNED_HARDWARE_CLOCK_BINDING")):
                with self.assertRaisesRegex(ContractError, expected):
                    validate_clock_binding({**mapping, **changes})
            message.interface_values[0].interface_names[0] = FIELDS[1]
            with self.assertRaisesRegex(ContractError, "LEARNED_HARDWARE_SCHEMA"):
                decode_dynamic_state(message, mapping, 20.2)


if __name__ == "__main__":
    unittest.main()
