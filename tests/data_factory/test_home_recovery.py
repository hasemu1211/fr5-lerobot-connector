from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.data_factory.motion.home_recovery import recover_home
from tools.fr5_data_factory import ContractError, canonical_digest


ROOT = Path(__file__).resolve().parents[2]
MOTION = json.loads((
    ROOT / "config/data_factory/motion_qualifications/fr5-place-a-wood-cube-r001.json"
).read_text(encoding="utf-8"))
TARGET = MOTION["qualified_safe_joint_positions_rad"]


def snapshot(joints, gripper=0.021):
    return {
        "joint_positions": list(joints), "joint_state_age_s": 0.0,
        "gripper_settings": {},
        "arm_controller": {"ready": True},
        "gripper_controller": {
            "ready": True, "reference_position_m": gripper,
            "feedback_position_m": gripper,
        },
    }


class FakeTransport:
    def __init__(
        self, snapshots, *, precommit_error=None,
        poll_error=None, cancel_error=None,
    ):
        self.snapshots = list(snapshots)
        self.started = []
        self.cancelled = 0
        self.precommit_error = precommit_error
        self.poll_error = poll_error
        self.cancel_error = cancel_error

    def preflight(self):
        ready = {"ready": True}
        return {
            "move_action": ready, "execute_trajectory": ready,
            "gripper": ready, "joint_states": ready,
            "joint_order": ["j1", "j2", "j3", "j4", "j5", "j6"],
        }

    def snapshot(self, _max_age):
        return self.snapshots.pop(0)

    def build_gripper_goal(self, *_args):
        return b"open"

    def plan_arm(self, _phase, _target, joint_target, *_args):
        return {
            "terminal_status": "SUCCEEDED", "moveit_success": True,
            "serialized_trajectory": b"home", "final_joint_state": list(joint_target),
        }

    def precommit_home_recovery(self, **_kwargs):
        if self.precommit_error:
            raise ContractError(self.precommit_error)
        return {"evidence_digest": canonical_digest(["precommit"])}

    def start_phase(self, step):
        self.started.append(step["phase"])

    def poll_active(self):
        if self.poll_error:
            raise ContractError(self.poll_error)
        return object()

    def cancel_active(self, _timeout):
        self.cancelled += 1
        if self.cancel_error:
            raise ContractError(self.cancel_error)


class HomeRecoveryTests(unittest.TestCase):
    def test_open_plan_collision_gate_execute_and_verify_home(self):
        start = [value + 0.2 for value in TARGET]
        transport = FakeTransport([
            snapshot(start, 0.012), snapshot(start), snapshot(start), snapshot(TARGET),
        ])
        result = recover_home(
            transport, motion_qualification=MOTION,
            sleep_call=lambda _seconds: None,
        )
        self.assertEqual(result["status"], "HOME")
        self.assertEqual(result["arm_goal_count"], 1)
        self.assertEqual(transport.started, ["GRIPPER_OPEN", "SAFE_POSE_PTP"])
        self.assertEqual(transport.cancelled, 0)

    def test_already_open_home_has_no_action_goal(self):
        transport = FakeTransport([snapshot(TARGET)])
        result = recover_home(
            transport, motion_qualification=MOTION,
            sleep_call=lambda _seconds: None,
        )
        self.assertEqual((result["status"], result["arm_goal_count"]), ("ALREADY_HOME", 0))
        self.assertEqual(transport.started, [])

    def test_precommit_failure_has_zero_arm_goal(self):
        start = [value + 0.2 for value in TARGET]
        transport = FakeTransport(
            [snapshot(start)], precommit_error="COLLISION_DETECTED",
        )
        with self.assertRaisesRegex(ContractError, "COLLISION_DETECTED"):
            recover_home(
                transport, motion_qualification=MOTION,
                sleep_call=lambda _seconds: None,
            )
        self.assertEqual(transport.started, [])

    def test_final_receipt_requires_gripper_to_remain_open(self):
        start = [value + 0.2 for value in TARGET]
        transport = FakeTransport([
            snapshot(start), snapshot(start), snapshot(TARGET, 0.0),
        ])
        with self.assertRaisesRegex(
            ContractError, "HOME_RECOVERY_GRIPPER_NOT_OPEN",
        ):
            recover_home(
                transport, motion_qualification=MOTION,
                sleep_call=lambda _seconds: None,
            )
        self.assertEqual(transport.started, ["SAFE_POSE_PTP"])

    def test_terminal_action_failure_is_not_masked_by_cancel(self):
        start = [value + 0.2 for value in TARGET]
        transport = FakeTransport(
            [snapshot(start), snapshot(start)],
            poll_error="ROS_EXEC_FAILED", cancel_error="ROS_EXEC_NO_ACTIVE",
        )
        with self.assertRaisesRegex(ContractError, "ROS_EXEC_FAILED"):
            recover_home(
                transport, motion_qualification=MOTION,
                sleep_call=lambda _seconds: None,
            )
        self.assertEqual(transport.cancelled, 1)

    def test_active_cancel_failure_remains_uncertain(self):
        start = [value + 0.2 for value in TARGET]
        transport = FakeTransport(
            [snapshot(start), snapshot(start)],
            poll_error="ROS_EXEC_RESULT_TIMEOUT",
            cancel_error="ROS_EXEC_CANCEL_ACK_TIMEOUT",
        )
        with self.assertRaisesRegex(
            ContractError, "HOME_RECOVERY_CANCEL_UNCERTAIN",
        ):
            recover_home(
                transport, motion_qualification=MOTION,
                sleep_call=lambda _seconds: None,
            )
        self.assertEqual(transport.cancelled, 1)


if __name__ == "__main__":
    unittest.main()
