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
    LIVE_FIELDS, CAUSAL_FIELDS, RESOURCE, check_hardware, decode_dynamic_state, native_clock_parameter, native_temporal_parameter,
)
from tests.data_factory.rollout.test_gripper_evidence import patched_source, method


def temporal_policy(*, incarnation=(1, 2, 3, 4), max_age_s=.08, host_clock_tolerance_s=.002):
    """CPU fixture; deployed values must come from the existing approved limits."""
    return {"schema_version": "fr5.gripper_temporal_policy.v1", "incarnation": list(incarnation),
            "max_age_s": max_age_s, "host_clock_tolerance_s": host_clock_tolerance_s}


class CurrentBracketTest(unittest.TestCase):
    def test_mapping_expiry_falsifiers(self):
        for mode, moves, completed in (("expiry_first", 1, 1), ("expiry_second", 2, 2), ("expiry_during", 1, 1)):
            packet, _ = self.packet(mode)
            self.assertEqual(packet, {"moves": moves, "completed": completed, "error": 0})
            print(mode, packet, flush=True)

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
            "certified_gripper_observation", "refresh_gripper_freshness", "gripper_worker", "sample_gripper_evidence",
            "gripper_release_ready", "write", "stop_gripper_worker"))
        fixture = Path(__file__).with_name("gripper_native_fixture.cpp").read_text().split("int main(")[0]
        code = fixture.replace("// NATIVE_METHODS", native) + Path(__file__).with_name("bracket_native_main.cpp").read_text()
        (root / "native.cpp").write_text(code)
        cls.binary = root / "native"
        subprocess.run(["g++", "-std=c++17", "-pthread", str(root / "native.cpp"), "-o", str(cls.binary)], check=True, capture_output=True)

    def packet(self, mode="fresh", *, command_budget_ms=None, query_delay_ms=0):
        policy = temporal_policy()
        argv = [str(self.binary), mode, ",".join(map(repr, native_temporal_parameter(policy)))]
        if command_budget_ms is not None or query_delay_ms:
            argv.extend((str(command_budget_ms or 0), str(query_delay_ms)))
        result = subprocess.run(argv,
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout), policy

    def evidence(self, packet, binding):
        from control_msgs.msg import DynamicJointState, InterfaceValue
        from rclpy.serialization import serialize_message, deserialize_message
        message = DynamicJointState(joint_names=[RESOURCE], interface_values=[InterfaceValue(
            interface_names=packet["names"], values=packet["wire"])])
        # Replay at the actual native certificate's observation time. The source
        # has stopped: Python import/serialization delay is not a live sample age.
        at, mono = packet["wire"][40:42] if len(packet["wire"]) >= 42 else packet["wire"][9:11]
        decoded = decode_dynamic_state(deserialize_message(serialize_message(message), DynamicJointState), binding, mono)
        return {"captured_at_s": at, "captured_monotonic_s": mono,
                "snapshot": {"gripper_controller": {"hardware_execution": decoded}}}

    def test_delayed_wire_replay_does_not_relabel_stale_data_as_live(self):
        packet, policy = self.packet()
        time.sleep(.1)  # Reproduce Python being scheduled after the 80 ms live bound.
        evidence = self.evidence(packet, policy)
        at, mono = evidence["captured_at_s"], evidence["captured_monotonic_s"]
        self.assertEqual((at, mono), tuple(packet["wire"][40:42]))
        check_hardware(evidence, at, mono, .08)
        with self.assertRaisesRegex(ContractError, "LEARNED_HARDWARE_STALE"):
            check_hardware(evidence, at+.1, mono+.1, .08)

    def test_completed_proof_survives_new_current_brackets_and_old_expiry(self):
        packet, binding = self.packet()
        self.assertEqual(tuple(packet["names"]), CAUSAL_FIELDS)
        self.assertTrue(packet["proof_unchanged"])
        self.assertGreater(packet["renewed_arm_sends"], 0)
        evidence = self.evidence(packet, binding)
        self.assertGreater(evidence["captured_at_s"] - packet["wire"][44], .15)
        wire = check_hardware(evidence, evidence["captured_at_s"], evidence["captured_monotonic_s"], .08)
        self.assertEqual(wire["completed_generation"], 1)

    def test_native_missing_expired_wrong_sample_reset_and_stop_block(self):
        for mode in ("expired", "wrong_sample", "absent", "reset", "old_generation", "incomplete", "regressed", "stopped", "pre_ack", "terminal_wrong_generation", "terminal_wrong_reference",
                     "terminal_wrong_incarnation", "terminal_stale", "terminal_tie", "terminal_fault", "terminal_wrong_done"):
            with self.subTest(mode=mode):
                self.packet(mode)

    def test_actual_causal_command_activity_and_faults(self):
        for mode in ("command_old_done", "command_changed_old_done", "command_stale", "command_stop", "command_error",
                     "command_policy_change", "command_late_resume", "command_cancel_query", "command_99", "command_busy_done", "command_settled"):
            with self.subTest(mode=mode):
                packet, _ = self.packet(mode)
                success = mode in ("command_99", "command_busy_done", "command_settled")
                self.assertEqual(packet["completed"], int(success), packet)
                self.assertEqual(packet["moves"], 0 if mode=="command_policy_change" else 1)
                if success:
                    self.assertEqual(packet["error"], 0)

    def test_settled_completion_budget_is_separate_from_sample_freshness(self):
        # Three causal observations plus two 50 ms polls need more than the old
        # 120 ms fixture budget when each clock query takes only 5 ms.
        for budget, completed, error in ((120, 0, -1), (None, 1, 0)):
            with self.subTest(command_budget_ms=budget):
                packet, policy = self.packet("command_settled", command_budget_ms=budget, query_delay_ms=5)
                self.assertEqual(packet, {"moves": 1, "completed": completed, "error": error})
                self.assertEqual(policy["max_age_s"], .08)

    def test_python_causal_terminal_proof_and_archive_separation(self):
        from tools.data_factory.rollout.gripper_evidence import calendar_s
        packet, policy = self.packet()
        evidence = self.evidence(packet, policy)
        w = evidence["snapshot"]["gripper_controller"]["hardware_execution"]["wire"]
        changes = ({"ack_steady_s": w["terminal_query_before_steady_s"]+.001},
                   {"terminal_generation": 2}, {"terminal_reference_m": .1}, {"terminal_incarnation_0": 9},
                   {"terminal_query_before_steady_s": w["terminal_query_before_steady_s"]-1},
                   {"terminal_query_before_controller_s": calendar_s(w,"completion_")-.001},
                   {"terminal_motion_done": 0}, {"terminal_fault": 1}, {"proof_valid":0},
                   {"proof_clock_version": 1}, {"terminal_max_age_s": 99})
        for change in changes:
            candidate = copy.deepcopy(evidence)
            candidate["snapshot"]["gripper_controller"]["hardware_execution"]["wire"].update(change)
            with self.subTest(change=change), self.assertRaises(ContractError):
                check_hardware(candidate, evidence["captured_at_s"], evidence["captured_monotonic_s"], .08)
        # Historical terminal remains readable; it cannot be handed off as newly complete.
        with self.assertRaises(ContractError):
            check_hardware(evidence, evidence["captured_at_s"], evidence["captured_monotonic_s"], .08, completion=True)
        # A v3 policy cannot promote old v2 evidence to the new live contract.
        old = copy.deepcopy(evidence)
        old["snapshot"]["gripper_controller"]["hardware_execution"]["wire"] = {k:w[k] for k in LIVE_FIELDS}
        old["snapshot"]["gripper_controller"]["hardware_execution"]["wire"]["version"] = 2
        with self.assertRaises(ContractError):
            check_hardware(old, evidence["captured_at_s"], evidence["captured_monotonic_s"], .08)

    def test_archived_v2_mapping_proof_still_reads_after_expiry(self):
        packet, policy = self.packet()
        evidence = self.evidence(packet, policy)
        hw = evidence["snapshot"]["gripper_controller"]["hardware_execution"]
        w = {k: hw["wire"][k] for k in LIVE_FIELDS}
        w["version"] = 2
        at, mono = w["proof_system_s"], w["proof_steady_s"]
        binding = {"schema_version":"fr5.gripper_source_clock.v1", "incarnation":[1,2,3,4],
                   "calendar_to_system_offset_s":0., "uncertainty_s":.002,
                   "system_anchor_s":at-.1, "steady_anchor_s":mono-.1, "valid_until_system_s":at+.05}
        values = native_clock_parameter(binding, .08)
        for key, value in zip(LIVE_FIELDS[46:57], values):
            w[key] = value
        hw.update(wire=w, clock_binding=binding)
        self.assertGreater(evidence["captured_at_s"], binding["valid_until_system_s"])
        self.assertEqual(check_hardware(evidence, evidence["captured_at_s"], evidence["captured_monotonic_s"], .08)["version"], 2)

    def test_bootstrap_identity_decode_does_not_grant_readiness(self):
        from tools.data_factory.rollout.gripper_evidence import FIELDS, identity
        packet, policy = self.packet()
        for names, version in ((FIELDS, 1), (CAUSAL_FIELDS, 3)):
            startup = {"names": list(names), "wire": packet["wire"][:len(names)]}
            startup["wire"][0] = version
            startup["wire"][27] = 0
            if version == 3:
                startup["wire"][35] = 0
            evidence = self.evidence(startup, policy)
            self.assertEqual(identity(evidence["snapshot"]["gripper_controller"]["hardware_execution"]["wire"]), [1,2,3,4])
            with self.assertRaises(ContractError):
                check_hardware(evidence, evidence["captured_at_s"], evidence["captured_monotonic_s"], .08)

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
