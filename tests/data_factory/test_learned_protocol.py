"""Proposal protocol compatibility only; no hardware or model execution."""
import copy
import unittest

from tools.data_factory.learned_action_adapter import fake_rgb
from tools.data_factory.rollout.finite_plan import (
    FinitePolicyInference, JOINTS, validate_proposal,
)
from tools.fr5_data_factory import ContractError, canonical_digest


class LearnedProtocolTest(unittest.TestCase):
    def proposal(self, version):
        state = [0.] * 6 + [.01]
        xml = '<robot name="synthetic">' + ''.join(
            f'<joint name="{name}" type="{"prismatic" if i == 6 else "revolute"}">'
            f'<limit lower="{0 if i == 6 else -3}" upper="{.02 if i == 6 else 3}" velocity="1"/></joint>'
            for i, name in enumerate(JOINTS)
        ) + '</robot>'
        observation = {
            "source_clock": "SYSTEM_TIME",
            "source_timestamps_s": {key: 10. for key in ("state", "camera1", "camera2")},
            "observation.state": state,
            "observation.images.camera1": fake_rgb(),
            "observation.images.camera2": fake_rgb(),
        }
        checkpoint = {"tree_digest": canonical_digest("synthetic-weights"),
                      "training_receipt_digest": canonical_digest("synthetic-receipt"),
                      "runtime": "SYNTHETIC_TEST_ONLY"}
        p = FinitePolicyInference(lambda _: [state], checkpoint, source_clock=lambda: 10.).propose(
            observation, instruction="synthetic protocol check", robot_description=xml, period_s=.1)
        causal = version in (3, 4)
        binding = ({"schema_version": "fr5.gripper_temporal_policy.v1", "incarnation": [1, 2, 3, 4],
                    "max_age_s": .3, "host_clock_tolerance_s": .001} if causal else {
            "schema_version": "fr5.gripper_source_clock.v1", "incarnation": [1, 2, 3, 4],
            "calendar_to_system_offset_s": 0., "uncertainty_s": .002,
            "system_anchor_s": 10., "steady_anchor_s": 10., "valid_until_system_s": 20.})
        p["runtime_inputs"] = {
            "checkpoint": "/synthetic/checkpoint", "device": "cpu",
            "gripper_temporal_policy" if causal else "gripper_source_clock": "/synthetic/clock.json",
            "clock_binding": binding, "hardware_wire_version": version,
            "camera_topics": {"camera1": "/up", "camera2": "/wrist"},
            "camera_mapping": {"observation.images.up": "observation.images.camera1",
                               "observation.images.wrist": "observation.images.camera2"}, "fps": 10.,
        }
        return self.redigest(p)

    @staticmethod
    def redigest(p):
        p["proposal_digest"] = canonical_digest({k: v for k, v in p.items() if k != "proposal_digest"})
        return p

    def test_explicit_protocols_preserve_exact_frozen_input_and_digest(self):
        for version in (2, 3, 4):
            with self.subTest(version=version):
                p = self.proposal(version)
                before = copy.deepcopy(p)
                self.assertEqual(validate_proposal(p), before)
                self.assertEqual(p, before)
                self.assertEqual(p["runtime_inputs"]["hardware_wire_version"], version)

    def test_unknown_noninteger_and_clock_family_mismatch_reject(self):
        for version in (0, 1, 5, 4., True, "4", None, 2):
            with self.subTest(version=version):
                p = self.proposal(4)
                p["runtime_inputs"]["hardware_wire_version"] = version
                with self.assertRaises(ContractError):
                    validate_proposal(self.redigest(p))
        p = self.proposal(2)
        p["runtime_inputs"]["hardware_wire_version"] = 4
        with self.assertRaises(ContractError):
            validate_proposal(self.redigest(p))
