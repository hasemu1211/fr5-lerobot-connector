from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.data_factory.experiment_manifest import compile_robot_start_pose
from tools.data_factory.start_pose_registry import (
    compile_start_pose_profile,
    list_start_pose_profiles,
    project_robot_start_pose_qualification,
    save_start_pose_profile,
    validate_start_pose_profile,
)
from tools.fr5_data_factory import ContractError, canonical_digest


JOINTS = ("j1", "j2", "j3", "j4", "j5", "j6")
NOW = datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc)


def snapshot(*, start: float = 0.0, age_s: float = 0.1) -> dict:
    return {
        "schema_version": "data_factory.start_pose_joint_snapshot.v1",
        "source": "READ_ONLY_JOINT_STATE",
        "robot_system_id": "fr5-lab-a",
        "joint_order": list(JOINTS),
        "joint_positions_rad": {
            joint: start + index / 10 for index, joint in enumerate(JOINTS)
        },
        "captured_at": (NOW - timedelta(seconds=age_s)).isoformat().replace("+00:00", "Z"),
    }


def profile(
    start_pose_id: str = "start-ready-r001", *, start: float = 0.0,
    qualification_status: str = "CANDIDATE", safety_status: str = "UNASSESSED",
    recovery_home: str = "qualified-recovery-home",
) -> dict:
    return compile_start_pose_profile(
        start_pose_id=start_pose_id,
        display_name="Ready pose",
        robot_system_id="fr5-lab-a",
        snapshot=snapshot(start=start),
        tolerance_rad={joint: 0.01 for joint in JOINTS},
        recovery_home_digest=canonical_digest(recovery_home),
        qualification_status=qualification_status,
        safety_status=safety_status,
        max_snapshot_age_s=0.5,
        now=NOW,
    )


class StartPoseRegistryTests(unittest.TestCase):
    def test_candidate_roundtrips_as_read_only_profile(self) -> None:
        value = profile()
        self.assertEqual(value, validate_start_pose_profile(value))
        self.assertEqual(value["target_rad"], snapshot()["joint_positions_rad"])
        self.assertEqual(value["capture_provenance"]["source"], "READ_ONLY_JOINT_STATE")
        self.assertEqual(value["authority"], "NO_EXECUTION_AUTHORITY")
        self.assertNotIn("plan_digest", value)

    def test_safe_qualified_profile_projects_existing_pose_contract(self) -> None:
        value = profile(
            qualification_status="QUALIFIED", safety_status="SAFE_FOR_MOTION",
        )
        qualification = project_robot_start_pose_qualification(value)
        self.assertEqual(qualification["source"], "QUALIFICATION_ARTIFACT")
        self.assertEqual(
            qualification["home_candidate_digest"], value["recovery_home_digest"],
        )
        projected = compile_robot_start_pose(qualification=qualification)
        self.assertEqual(projected["robot_start_pose_id"], value["start_pose_id"])
        self.assertEqual(projected["target_rad"], value["target_rad"])

    def test_candidate_or_unsafe_profile_cannot_project(self) -> None:
        for value in (
            profile(),
            profile(qualification_status="QUALIFIED", safety_status="NOT_SAFE_FOR_MOTION"),
        ):
            with self.subTest(status=value["safety_status"]), self.assertRaisesRegex(
                ContractError, "START_POSE_NOT_QUALIFIED",
            ):
                project_robot_start_pose_qualification(value)

    def test_exclusive_save_is_idempotent_and_registry_is_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "start_poses"
            second = profile("start-b-r001", start=0.2)
            first = profile("start-a-r001")
            save_start_pose_profile(root, second, now=NOW)
            saved = save_start_pose_profile(root, first, now=NOW)
            self.assertEqual(saved, first)
            self.assertEqual(
                save_start_pose_profile(root, first, now=NOW + timedelta(days=1)), first,
            )
            self.assertEqual(
                [item["start_pose_id"] for item in list_start_pose_profiles(root)],
                ["start-a-r001", "start-b-r001"],
            )
            self.assertFalse(any(path.name.startswith(".") for path in root.iterdir()))

            conflict = profile("start-a-r001", start=0.4)
            with self.assertRaisesRegex(ContractError, "START_POSE_ID_CONFLICT"):
                save_start_pose_profile(root, conflict, now=NOW)
            self.assertEqual(list_start_pose_profiles(root)[0], first)

            other_home = profile("start-c-r001", recovery_home="other-home")
            with self.assertRaisesRegex(
                ContractError, "START_POSE_RECOVERY_HOME_CONFLICT",
            ):
                save_start_pose_profile(root, other_home, now=NOW)

    def test_invalid_or_stale_input_never_creates_registry(self) -> None:
        cases = []
        malformed = snapshot()
        malformed["joint_positions_rad"].pop("j6")
        cases.append(("malformed", {"snapshot": malformed}))
        nonfinite = snapshot()
        nonfinite["joint_positions_rad"]["j1"] = float("nan")
        cases.append(("nonfinite", {"snapshot": nonfinite}))
        cases.append(("stale", {"snapshot": snapshot(age_s=1.0)}))
        cases.append(("source", {"snapshot": {**snapshot(), "source": "CALLER_ASSERTED"}}))
        cases.append(("status", {"qualification_status": "APPROVED"}))

        for name, replacement in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "start_poses"
                kwargs = {
                    "start_pose_id": "start-invalid-r001",
                    "display_name": "Invalid pose",
                    "robot_system_id": "fr5-lab-a",
                    "snapshot": snapshot(),
                    "tolerance_rad": {joint: 0.01 for joint in JOINTS},
                    "recovery_home_digest": canonical_digest("qualified-recovery-home"),
                    "qualification_status": "CANDIDATE",
                    "safety_status": "UNASSESSED",
                    "max_snapshot_age_s": 0.5,
                    "now": NOW,
                }
                kwargs.update(replacement)
                with self.assertRaises(ContractError):
                    save_start_pose_profile(
                        root, compile_start_pose_profile(**kwargs), now=NOW,
                    )
                self.assertFalse(root.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "start_poses"
            forged = copy.deepcopy(profile())
            forged["target_rad"]["j1"] += 0.1
            with self.assertRaisesRegex(ContractError, "START_POSE_CAPTURE_DIGEST_MISMATCH"):
                save_start_pose_profile(root, forged, now=NOW)
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
