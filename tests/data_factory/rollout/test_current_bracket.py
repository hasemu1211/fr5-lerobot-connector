"""Exact native producer/sampler/release/write, then the real Python wire consumer."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from tools.fr5_data_factory import ContractError
from tools.data_factory.rollout.gripper_evidence import (
    LIVE_FIELDS, RESOURCE, check_hardware, decode_dynamic_state, native_clock_parameter,
)
from tests.data_factory.rollout.test_gripper_evidence import patched_source, method


class CurrentBracketTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from control_msgs.msg import DynamicJointState, InterfaceValue
        from rclpy.serialization import serialize_message, deserialize_message
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        (root / "gripper_execution_evidence.hpp").write_text(patched_source(
            "fairino_hardware_v3_9_7/include/fairino_hardware/gripper_execution_evidence.hpp"))
        source = patched_source("fairino_hardware_v3_9_7/src/fairino_hardware_interface.cpp")
        native = "\n".join(method(source, name) for name in (
            "refresh_gripper_freshness", "gripper_worker", "sample_gripper_evidence",
            "gripper_release_ready", "write", "stop_gripper_worker"))
        fixture = Path(__file__).with_name("gripper_native_fixture.cpp").read_text().split("int main(")[0]
        code = fixture.replace("// NATIVE_METHODS", native) + Path(__file__).with_name("bracket_native_main.cpp").read_text()
        (root / "native.cpp").write_text(code)
        cls.binary = root / "native"
        subprocess.run(["g++", "-std=c++17", "-pthread", str(root / "native.cpp"), "-o", str(cls.binary)], check=True, capture_output=True)

    def packet(self, mode="fresh"):
        now, steady = time.time(), time.monotonic()
        binding = {"schema_version": "fr5.gripper_source_clock.v1", "incarnation": [1,2,3,4],
                   "calendar_to_system_offset_s": 0., "uncertainty_s": .002,
                   "system_anchor_s": now, "steady_anchor_s": steady, "valid_until_system_s": now+.25}
        result = subprocess.run([str(self.binary), mode, ",".join(map(repr, native_clock_parameter(binding, .08)))],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout), binding

    def evidence(self, packet, binding):
        from control_msgs.msg import DynamicJointState, InterfaceValue
        from rclpy.serialization import serialize_message, deserialize_message
        message = DynamicJointState(joint_names=[RESOURCE], interface_values=[InterfaceValue(
            interface_names=packet["names"], values=packet["wire"])])
        decoded = decode_dynamic_state(deserialize_message(serialize_message(message), DynamicJointState), binding, time.monotonic())
        return {"captured_at_s": time.time(), "captured_monotonic_s": time.monotonic(),
                "snapshot": {"gripper_controller": {"hardware_execution": decoded}}}

    def test_completed_proof_survives_new_current_brackets_and_old_expiry(self):
        packet, binding = self.packet()
        self.assertEqual(tuple(packet["names"]), LIVE_FIELDS)
        self.assertTrue(packet["proof_unchanged"])
        self.assertGreater(packet["renewed_arm_sends"], 0)
        evidence = self.evidence(packet, binding)
        self.assertGreater(evidence["captured_at_s"], binding["valid_until_system_s"])
        wire = check_hardware(evidence, evidence["captured_at_s"], evidence["captured_monotonic_s"], .08)
        self.assertEqual(wire["completed_generation"], 1)

    def test_native_missing_expired_wrong_sample_reset_and_stop_block(self):
        for mode in ("expired", "wrong_sample", "absent", "reset", "old_generation", "incomplete", "regressed", "stopped"):
            with self.subTest(mode=mode):
                self.packet(mode)

    def test_python_rejects_relabel_and_incomplete_proof(self):
        packet, binding = self.packet()
        evidence = self.evidence(packet, binding)
        wire = evidence["snapshot"]["gripper_controller"]["hardware_execution"]["wire"]
        for changes in ({"current_valid":0}, {"query_before_steady_s":wire["query_before_steady_s"]-1},
                        {"frame":99}, {"millisecond":999}, {"proof_valid":0}, {"incarnation_0":99},
                        {"query_after_controller_s":wire["query_before_controller_s"]-1}):
            candidate = copy.deepcopy(evidence)
            candidate["snapshot"]["gripper_controller"]["hardware_execution"]["wire"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                check_hardware(candidate, evidence["captured_at_s"], evidence["captured_monotonic_s"], .08)

class PreciseReaderTest(unittest.TestCase):
    def test_actual_parser_keeps_epoch_precision_and_cancellation_has_no_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "precise_controller_clock.hpp").write_text(patched_source(
                "fairino_hardware_v3_9_7/include/fairino_hardware/precise_controller_clock.hpp"))
            (root / "reader.cpp").write_text(r'''
#include "precise_controller_clock.hpp"
#include <cassert>
int main() {
  using fairino_hardware::PreciseControllerClock;
  const std::string prefix="<methodResponse><params><param><value><array><data><value><int>0</int></value><value><double>";
  const std::string suffix="</double></value></data></array></value></param></params></methodResponse>";
  auto a=PreciseControllerClock::parse(prefix+"1788875552807.385"+suffix);
  auto b=PreciseControllerClock::parse(prefix+"1788875552808.385"+suffix);
  assert(b>a && b-a<.002);
  for(const char *bad:{"nan","inf","-1","0"}) {
    bool rejected=false;try{PreciseControllerClock::parse(prefix+bad+suffix);}catch(...){rejected=true;}assert(rejected);
  }
  PreciseControllerClock client("127.0.0.1"); // cancelled before any network call
  bool cancelled=false;try{client.read(10,[]{return true;});}catch(...){cancelled=true;}assert(cancelled);
}
''')
            binary = root / "reader"
            subprocess.run(["g++", "-std=c++17", str(root / "reader.cpp"), "-lcurl", "-ltinyxml2", "-o", str(binary)],
                           check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, timeout=2)

class VendorPatchBuildTest(unittest.TestCase):
    def test_patch_applies_to_pinned_vendor_and_real_translation_unit_compiles(self):
        from tests.data_factory.rollout.test_gripper_evidence import ROOT
        common = Path(subprocess.check_output(["git", "rev-parse", "--git-common-dir"], cwd=ROOT, text=True).strip())
        if not common.is_absolute():
            common = ROOT / common
        vendor = common.resolve().parent / "src/frcobot_ros2"
        if not (vendor / ".git").exists():
            self.skipTest("pinned vendor object database unavailable")
        pinned = subprocess.check_output(["git", "rev-parse", "HEAD:src/frcobot_ros2"], cwd=ROOT, text=True).strip()
        patch = ROOT / "patches/frcobot_ros2.patch"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for section in patch.read_text().split("diff --git ")[1:]:
                old = section.split(" b/", 1)[0][2:]
                if "--- /dev/null\n" in section:
                    continue
                target = root / old
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(subprocess.check_output(["git", "show", f"{pinned}:{old}"], cwd=vendor))
            checked = subprocess.run(["git", "apply", "--check", str(patch)], cwd=root, capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            subprocess.run(["git", "apply", str(patch)], cwd=root, check=True, capture_output=True)
            package = root / "fairino_hardware_v3_9_7"
            vendor_package = vendor / "fairino_hardware_v3_9_7"
            includes = [package / "include", vendor_package / "include", vendor_package / "include/fairino_hardware", vendor_package, Path("/usr/include/eigen3")]
            includes.extend(p for p in Path("/opt/ros/jazzy/include").iterdir() if p.is_dir())
            result = subprocess.run(["g++", "-std=c++17", "-fsyntax-only", *[f"-I{p}" for p in includes],
                                     str(package / "src/fairino_hardware_interface.cpp")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
