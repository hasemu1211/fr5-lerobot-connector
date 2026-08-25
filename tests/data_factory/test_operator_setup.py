from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

try:
    from .test_campaign_authoring import draft
    from .test_experiment_manifest import hypothesis
except ImportError:
    from test_campaign_authoring import draft
    from test_experiment_manifest import hypothesis
from tools.data_factory.campaign_authoring import compile_collection_campaign
from tools.data_factory.operator_setup import (
    build_camera_binding_candidate,
    build_test_only_root_binding,
    build_test_only_start_binding,
    gripper_setup_projection,
    qualified_table_plane_reference,
    validate_print_measurements,
    validate_test_only_root_binding,
    validate_test_only_start_binding,
)
from tools.fr5_data_factory import ContractError, canonical_digest


ROOT = Path(__file__).parents[2]


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text())


def pose_snapshot(target: list[float], *, age: float = 0.05) -> dict:
    rigid = {
        "translation_m": [0.0, 0.0, 0.0],
        "rotation_columns": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    }
    return {
        "schema_version": "data_factory.pose_snapshot.v1",
        "frames": {"base": "base_link", "wrist": "wrist3_link"},
        "joint_positions_rad": dict(zip(("j1", "j2", "j3", "j4", "j5", "j6"), target)),
        "base_wrist": rigid,
        "base_tcp": {
            **rigid,
            "candidate_status": "QUALIFIED",
            "candidate_source_sha256": canonical_digest("tcp"),
            "manifest_source_sha256": canonical_digest("tcp-manifest"),
        },
        "joint_state_age_s": age,
        "joint_stamp_ns": 1_000_000_000,
        "transform_stamp_ns": 1_000_000_000,
        "ros_sample_age_s": age,
    }


class OperatorSetupTests(unittest.TestCase):
    def test_test_only_roots_are_exact_isolated_and_effect_free(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory).resolve()
            value = build_test_only_root_binding(
                repository, session_id="session-r001", run_id="run-r001",
            )
            self.assertEqual(value, validate_test_only_root_binding(value, repository_root=repository))
            self.assertEqual(
                Path(value["run_root"]),
                repository / "outputs/data_factory/test_only_physical/session-r001/runs",
            )
            self.assertEqual(
                Path(value["dataset_root"]),
                repository / "datasets/test_only_physical/session-r001/run-r001",
            )
            self.assertFalse(value["production_writers_enabled"])
            self.assertEqual(list(repository.iterdir()), [])

            for outside in (
                repository / "outputs/data_factory/runs",
                repository / "datasets/fr5_episodes/run-r001",
                repository.parent / "escape",
            ):
                with self.subTest(outside=outside), self.assertRaisesRegex(
                    ContractError, "TEST_ONLY_ROOT_(MISMATCH|OUTSIDE_REPOSITORY)",
                ):
                    build_test_only_root_binding(
                        repository, session_id="session-r001", run_id="run-r001",
                        run_root=outside,
                    )

    def test_symlink_and_digest_tampering_fail_before_directory_creation(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            repository = Path(directory).resolve()
            link_parent = repository / "outputs/data_factory/test_only_physical"
            link_parent.mkdir(parents=True)
            (link_parent / "session-r001").symlink_to(other, target_is_directory=True)
            with self.assertRaises(ContractError):
                build_test_only_root_binding(
                    repository, session_id="session-r001", run_id="run-r001",
                )

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory).resolve()
            value = build_test_only_root_binding(
                repository, session_id="session-r001", run_id="run-r001",
            )
            value["production_writers_enabled"] = True
            with self.assertRaisesRegex(ContractError, "TEST_ONLY_ROOT_AUTHORITY"):
                validate_test_only_root_binding(value, repository_root=repository)

    def test_motion_q_safe_start_binds_one_slot_and_fresh_home_snapshot(self):
        contract = hypothesis()
        source = draft(contract, count=1)
        manifest, _ = compile_collection_campaign(source, hypothesis=contract)
        motion = load("config/data_factory/motion_qualifications/fr5-place-a-wood-cube-r001.json")
        home = load("config/data_factory/home_candidates/fr5-lab-a-home-r001.json")
        target = motion["qualified_safe_joint_positions_rad"]
        binding = build_test_only_start_binding(
            manifest=manifest, hypothesis=contract, motion_qualification=motion,
            home_candidate=home, current_snapshot=pose_snapshot(target),
        )
        self.assertEqual(binding, validate_test_only_start_binding(binding, manifest=manifest))
        self.assertEqual(binding["scope"], "MOTION_Q_SAFE_START")
        self.assertEqual(set(binding["authority"].values()), {"NONE"})
        self.assertEqual(binding["home_candidate_digest"], canonical_digest(home))

        for changed, code in (
            (pose_snapshot(target, age=0.11), "TEST_ONLY_START_STALE"),
            (pose_snapshot([target[0] + 0.011, *target[1:]]), "TEST_ONLY_START_OUTSIDE_HOME"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(ContractError, code):
                build_test_only_start_binding(
                    manifest=manifest, hypothesis=contract,
                    motion_qualification=motion, home_candidate=home,
                    current_snapshot=changed,
                )
        wrong_home = {**home, "home_candidate_id": "other-home"}
        with self.assertRaisesRegex(ContractError, "TEST_ONLY_START_HOME_DIGEST"):
            build_test_only_start_binding(
                manifest=manifest, hypothesis=contract, motion_qualification=motion,
                home_candidate=wrong_home, current_snapshot=pose_snapshot(target),
            )

    def test_start_binding_rejects_multi_slot_and_never_invents_homing(self):
        contract = hypothesis()
        manifest, _ = compile_collection_campaign(draft(contract, count=2), hypothesis=contract)
        motion = load("config/data_factory/motion_qualifications/fr5-place-a-wood-cube-r001.json")
        home = load("config/data_factory/home_candidates/fr5-lab-a-home-r001.json")
        with self.assertRaisesRegex(ContractError, "TEST_ONLY_START_EXACT_ONE_SLOT"):
            build_test_only_start_binding(
                manifest=manifest, hypothesis=contract, motion_qualification=motion,
                home_candidate=home,
                current_snapshot=pose_snapshot(motion["qualified_safe_joint_positions_rad"]),
            )

    def test_workspace_plane_print_camera_and_gripper_are_non_authoritative(self):
        cell = load("config/data_factory/cells/place-a-yaw0-r002.json")
        plane = qualified_table_plane_reference(cell)
        self.assertEqual(plane["source_artifact_digest"], canonical_digest(cell))
        self.assertEqual(plane["status"], "QUALIFIED_REFERENCE")
        measurement = validate_print_measurements(
            source_scale_bar_mm=100.0, final_scale_bar_mm=99.0,
        )
        self.assertEqual(measurement["status"], "FINAL_PRINT_MEASUREMENT_BOUND")
        with self.assertRaisesRegex(ContractError, "WORKSPACE_FINAL_PRINT_OUT_OF_TOLERANCE"):
            validate_print_measurements(source_scale_bar_mm=100, final_scale_bar_mm=95)

        profile = load("config/data_factory/collection_profiles/fr5-up-rgb-30hz-v1.json")
        camera = build_camera_binding_candidate(
            binding_id="camera-binding-r001", device_kind="UVC",
            stable_device_id="usb-camera-001", intended_role="up",
            collection_profile=profile, connected=True,
        )
        self.assertEqual(
            (camera["connection_state"], camera["placement_status"], camera["production_qualified"]),
            ("CONNECTED", "UNPLACED", False),
        )
        self.assertEqual(
            gripper_setup_projection({"active": True, "position_valid": True, "gripper_index": 1})["state"],
            "ATTACHED",
        )
        self.assertEqual(
            gripper_setup_projection({"active": False, "position_valid": False, "gripper_index": 1}),
            {
                "state": "MAINTENANCE_APPROVAL_REQUIRED",
                "supported_action": "REQUEST_ACTIVATE_AND_NORMALIZE",
                "maintenance_call_count": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
