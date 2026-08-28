from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tools.data_factory.operator.registries.workspace as workspace_manager
from tools.data_factory.operator.catalog import load_operator_catalog
from tools.data_factory.operator.setup.contracts import (
    qualified_table_plane_reference,
    select_yaw0_print_profile,
    validate_print_measurements,
)
from tools.data_factory.operator.registries.workspace import WorkspaceManager
from tools.fr5_data_factory import ContractError, SAFE_ID, canonical_digest, load_json_strict


ROOT = Path(__file__).resolve().parents[4]


class WorkspaceManagerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name)
        self.config = self.repository / "config/data_factory"
        shutil.copytree(ROOT / "config/data_factory", self.config)
        self.candidate_root = self.repository / "workspace_candidates"
        self.source_cell = load_json_strict(
            self.config / "cells/place-a-yaw0-r002.json",
        )
        self.plane = qualified_table_plane_reference(self.source_cell)
        self.yaw0 = self.config / "test_only_physical/goal2-place1/yaw0_sheet.json"
        self.tcp = self.config / "test_only_physical/goal2-place1/tcp_candidate_manifest.json"
        tcp_manifest = load_json_strict(self.tcp)
        self.tcp_digest = tcp_manifest["tcp_candidate_digest"]
        self.tcp_manifest_digest = canonical_digest(tcp_manifest)
        self.measurements = validate_print_measurements(
            source_scale_bar_mm=100.0, final_scale_bar_mm=100.0,
        )
        self.points = (
            self.snapshot([1.0, 2.0, 3.0]),
            self.snapshot([1.1285, 2.0, 3.0]),
            self.snapshot([0.8715, 2.08, 3.0]),
        )

    def snapshot(self, point, *, age=0.05, tcp_digest=None):
        rigid = {
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_columns": [
                [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0],
            ],
        }
        return {
            "schema_version": "data_factory.pose_snapshot.v1",
            "frames": {"base": "base_link", "wrist": "wrist3_link"},
            "joint_positions_rad": {
                name: 0.0 for name in ("j1", "j2", "j3", "j4", "j5", "j6")
            },
            "base_wrist": rigid,
            "base_tcp": {
                **rigid, "translation_m": point, "candidate_status": "CANDIDATE",
                "candidate_source_sha256": tcp_digest or self.tcp_digest,
                "manifest_source_sha256": self.tcp_manifest_digest,
            },
            "joint_state_age_s": age,
            "joint_stamp_ns": 1_000_000_000,
            "transform_stamp_ns": 1_000_000_000,
            "ros_sample_age_s": age,
        }

    def manager(self, suffix="B", *, display_name=None):
        return WorkspaceManager(
            session_id=f"workspace-session-{suffix}",
            candidate_root=self.candidate_root,
            config_root=self.config,
            display_name=display_name or f"Place {suffix}",
        )

    def preview(self, manager, **changes):
        values = {
            "center_snapshot": self.points[0],
            "x_ref_snapshot": self.points[1],
            "y_check_snapshot": self.points[2],
            "plane_reference": self.plane,
            "print_measurements": self.measurements,
            "operator_or_agent_id": "TEST_OPERATOR",
            "yaw0_sheet": self.yaw0,
            "tcp_candidate_manifest": self.tcp,
            "tolerance_mm": 1.0,
        }
        values.update(changes)
        return manager.preview(**values)

    def config_bytes(self):
        return {
            path.relative_to(self.config): path.read_bytes()
            for path in self.config.rglob("*") if path.is_file()
        }

    def add_peer_motion(self, manager, *, stale=False):
        motion = load_json_strict(
            self.config
            / "motion_qualifications/fr5-place-a-wood-cube-r001.json",
        )
        cell = load_json_strict(
            self.config / "cells" / f"{manager.calibration_id}.json",
        )
        motion_id = f"fr5-{manager.calibration_id}-wood-cube-r001"
        motion.update(
            motion_qualification_id=motion_id,
            cell_calibration_id=manager.calibration_id,
        )
        motion["profile_digests"]["cell_calibration"] = (
            canonical_digest({"stale": manager.calibration_id})
            if stale else canonical_digest(cell)
        )
        path = self.config / "motion_qualifications" / f"{motion_id}.json"
        path.write_text(json.dumps(motion), encoding="utf-8")
        return path, motion

    def test_preview_then_exact_idempotent_save_creates_three_peer_artifacts(self):
        before = self.config_bytes()
        manager = self.manager()
        other = self.manager("C")
        self.assertNotEqual(manager.calibration_id, other.calibration_id)
        self.assertEqual(manager.place_id, "PLACE_B")
        self.assertEqual(manager.calibration_id, "place-b-yaw0-r001")
        self.assertRegex(manager.calibration_id, SAFE_ID)

        preview = self.preview(manager)
        self.assertEqual(self.config_bytes(), before)
        self.assertEqual(
            (preview["consumer_contract"], preview["execution_authorized"],
             preview["training_approved"]),
            ("PREVIEW_ONLY", False, False),
        )
        artifact = self.candidate_root / manager.calibration_id
        self.assertTrue((artifact / "_complete.json").is_file())

        promotion = manager.save(preview["preview_digest"])
        workspace = self.config / promotion["workspace_relative_path"]
        cell = self.config / promotion["cell_relative_path"]
        sheet = self.config / promotion["yaw0_sheet_relative_path"]
        workspace_document = load_json_strict(workspace)
        self.assertEqual(
            (workspace_document["display_name"], workspace_document["place_id"],
             workspace_document["frame_id"]),
            ("Place B", "PLACE_B", manager.calibration_id),
        )
        self.assertEqual(
            (workspace_document["coordinate_mode"],
             workspace_document["motion_qualification_status"],
             workspace_document["execution_authorized"],
             workspace_document["training_approved"]),
            ("CONTINUOUS_A4_PLANE", "REQUIRED", False, False),
        )
        self.assertEqual(
            (workspace_document["table_plane_provenance"]["source_place_id"],
             workspace_document["table_plane_provenance"]["source_calibration_id"]),
            ("PLACE_A", self.source_cell["calibration_id"]),
        )
        self.assertEqual(load_json_strict(cell)["qualification_status"], "QUALIFIED")
        self.assertEqual(load_json_strict(cell)["place_id"], "PLACE_B")
        stored_sheet = load_json_strict(sheet)
        self.assertEqual(stored_sheet["place_id"], "PLACE_B")
        self.assertEqual(
            {point["job_pose"]["place_id"] for point in stored_sheet["grid_points"]},
            {"PLACE_B"},
        )
        changed = set(self.config_bytes()) - set(before)
        self.assertEqual(
            changed,
            {
                Path("workspaces/PLACE_B.json"),
                Path("cells") / f"{manager.calibration_id}.json",
                Path("workspace_sheets") / f"{manager.calibration_id}_yaw0_sheet.json",
            },
        )
        after = self.config_bytes()
        for path, payload in before.items():
            self.assertEqual(after[path], payload)
        self.assertEqual(manager.save(preview["preview_digest"]), promotion)
        self.assertEqual(self.config_bytes(), after)
        catalog = load_operator_catalog(
            self.repository,
            device_ids=["usb-Generic_USB2.0_PC_CAMERA-video-index0"],
        )
        self.assertIn(
            manager.calibration_id,
            {option["id"] for option in catalog["axes"]["frame"]},
        )
        workspace_option = next(
            option for option in catalog["axes"]["workspace"]
            if option["id"] == manager.place_id
        )
        self.assertEqual(workspace_option["label"], "Place B")
        domains = [
            item for item in catalog["workspace_domains"]
            if item["frame_id"] == manager.calibration_id
        ]
        self.assertEqual(len(domains), 1)
        self.assertEqual(
            (domains[0]["workspace_id"], domains[0]["coordinate_mode"]),
            ("PLACE_B", "CONTINUOUS_A4_PLANE"),
        )
        candidates = [
            item for item in catalog["combinations"]
            if item["frame_id"] == manager.calibration_id
        ]
        candidates = [item for item in candidates if item["camera_bindings"]]
        self.assertTrue(candidates)
        self.assertTrue(all(
            item["authoring"] == {
                "selectable": True,
                "reason": "MOTION_QUALIFICATION_REQUIRED",
            }
            and item["execution"]["TEST_COLLECTION"] == {
                "executable": False,
                "reason": "MOTION_QUALIFICATION_REQUIRED",
            }
            for item in candidates
        ))

    def test_exact_peer_motion_graduates_test_collection_without_a_job_clone(self):
        manager = self.manager("Graduated", display_name="Bench Two")
        promotion = manager.save(self.preview(manager)["preview_digest"])
        motion_path, motion = self.add_peer_motion(manager)
        before = self.config_bytes()

        catalog = load_operator_catalog(
            self.repository,
            device_ids=["usb-Generic_USB2.0_PC_CAMERA-video-index0"],
        )
        self.assertEqual(self.config_bytes(), before)
        candidates = [
            item for item in catalog["combinations"]
            if item["frame_id"] == manager.calibration_id
            and item["camera_profile_id"] == "fr5-up-rgb-30hz-v1"
            and item["camera_bindings"]
        ]
        self.assertTrue(candidates)
        self.assertTrue(all(
            item["motion_id"] == motion["motion_qualification_id"]
            and item["sources"]["motion"]
            == str(motion_path.relative_to(self.repository))
            and item["sources"]["cell"]
            == f"config/data_factory/{promotion['cell_relative_path']}"
            and item["sources"]["yaw0_sheet"]
            == f"config/data_factory/{promotion['yaw0_sheet_relative_path']}"
            and item["sources"]["selected_sheet"]
            == f"config/data_factory/{promotion['yaw0_sheet_relative_path']}"
            and item["sources"]["job"].endswith(".job.json")
            and item["execution"]["TEST_COLLECTION"] == {
                "executable": True,
                "reason": "REGISTERED_WORKSPACE_CALLER",
            }
            and item["execution"]["GENERAL_COLLECTION"] == {
                "executable": False,
                "reason": "GENERAL_QUALIFICATION_REQUIRED",
            }
            for item in candidates
        ))
        jobs = list(self.config.rglob("*.job.json"))
        self.assertEqual(len(jobs), 1)

    def test_stale_peer_motion_digest_remains_non_executable(self):
        manager = self.manager("StaleMotion", display_name="Stale Motion")
        manager.save(self.preview(manager)["preview_digest"])
        motion_path, _motion = self.add_peer_motion(manager, stale=True)
        before = self.config_bytes()

        catalog = load_operator_catalog(
            self.repository,
            device_ids=["usb-Generic_USB2.0_PC_CAMERA-video-index0"],
        )
        self.assertEqual(self.config_bytes(), before)
        relative_motion = str(motion_path.relative_to(self.repository))
        candidates = [
            item for item in catalog["combinations"]
            if item["frame_id"] == manager.calibration_id
            and item["camera_bindings"]
        ]
        self.assertTrue(candidates)
        self.assertTrue(all(
            item["execution"]["TEST_COLLECTION"] == {
                "executable": False,
                "reason": "MOTION_QUALIFICATION_REQUIRED",
            }
            and item["sources"]["motion"] != relative_motion
            for item in candidates
        ))

    def test_compensated_96_source_profile_registers_a_100_mm_physical_sheet(self):
        manager = self.manager("printcal96", display_name="Printcal 96")
        measurements = validate_print_measurements(
            source_scale_bar_mm=96.0, final_scale_bar_mm=100.0,
        )
        sheet = select_yaw0_print_profile(
            ROOT, place_id=self.plane["place_id"], source_scale_bar_mm=96.0,
        )
        preview = self.preview(
            manager, print_measurements=measurements, yaw0_sheet=sheet,
        )
        self.assertEqual(preview["status"], "CANDIDATE_WITHIN_TOLERANCE")
        promotion = manager.save(preview["preview_digest"])
        stored_sheet = load_json_strict(
            self.config / promotion["yaw0_sheet_relative_path"],
        )
        self.assertEqual(
            stored_sheet["print_calibration"],
            {
                "nominal_scale_bar_mm": 100.0,
                "measured_scale_bar_mm": 96.0,
                "content_scale_percent": 104.166667,
            },
        )
        self.assertNotEqual(
            stored_sheet["a4_family_digest"], self.plane["a4_family_digest"],
        )
        self.assertEqual(stored_sheet["place_id"], manager.place_id)

    def test_capture_and_preview_failures_leave_config_unchanged(self):
        cases = []
        cases.append(("missing", {"center_snapshot": None}))
        stale = copy.deepcopy(self.points[0])
        stale["joint_state_age_s"] = 0.51
        cases.append(("stale", {"center_snapshot": stale}))
        wrong_tcp = self.snapshot(
            [1.0, 2.0, 3.0], tcp_digest=canonical_digest("wrong-tcp"),
        )
        cases.append(("tcp", {"center_snapshot": wrong_tcp}))
        wrong_plane = copy.deepcopy(self.plane)
        wrong_plane["table_normal_base"] = [0.0, 1.0, 0.0]
        wrong_plane["reference_digest"] = canonical_digest({
            key: value for key, value in wrong_plane.items()
            if key != "reference_digest"
        })
        cases.append(("plane", {"plane_reference": wrong_plane}))
        wrong_family = copy.deepcopy(load_json_strict(self.yaw0))
        wrong_family["a4_family_digest"] = canonical_digest("wrong-family")
        wrong_family_path = self.repository / "wrong_family.json"
        wrong_family_path.write_text(json.dumps(wrong_family), encoding="utf-8")
        cases.append(("family", {"yaw0_sheet": wrong_family_path}))
        wrong_measurement = copy.deepcopy(self.measurements)
        wrong_measurement["final_scale_bar_measured_mm"] = 90.0
        cases.append(("print-scale", {"print_measurements": wrong_measurement}))

        for suffix, changes in cases:
            with self.subTest(suffix=suffix):
                before = self.config_bytes()
                with self.assertRaises((ContractError, TypeError)):
                    self.preview(self.manager(suffix), **changes)
                self.assertEqual(self.config_bytes(), before)

    def test_capture_projection_hides_snapshots_and_seals_after_preview(self):
        manager = self.manager("capture")
        manager.capture("CENTER", self.points[0])
        manager.capture("X_REF", self.points[1])
        projection = manager.projection()
        self.assertEqual(
            projection["captures"],
            {"CENTER": True, "X_REF": True, "Y_CHECK": False},
        )
        self.assertNotIn("joint_positions_rad", str(projection))
        with self.assertRaisesRegex(ContractError, "WORKSPACE_CAPTURE_INCOMPLETE"):
            manager.preview_captured()

        manager.capture("Y_CHECK", self.points[2])
        preview = manager.preview_captured(
            plane_reference=self.plane,
            print_measurements=self.measurements,
            operator_or_agent_id="TEST_OPERATOR",
            yaw0_sheet=self.yaw0,
            tcp_candidate_manifest=self.tcp,
            tolerance_mm=1.0,
        )
        self.assertEqual(manager.projection()["preview"], preview)
        with self.assertRaisesRegex(ContractError, "WORKSPACE_PREVIEW_EXISTS"):
            manager.capture("CENTER", self.points[0])

    def test_out_of_tolerance_preview_can_be_discarded_and_recaptured(self):
        manager = self.manager("retry")
        rejected = self.preview(
            manager,
            y_check_snapshot=self.snapshot([0.8715, 2.09, 3.0]),
        )
        artifact = self.candidate_root / manager.calibration_id
        self.assertEqual(rejected["status"], "CANDIDATE_OUT_OF_TOLERANCE")
        self.assertTrue(artifact.is_dir())
        with self.assertRaisesRegex(ContractError, "WORKSPACE_PREVIEW_DISCARD"):
            manager.discard_preview(canonical_digest("stale-preview"))
        reset = manager.discard_preview(rejected["preview_digest"])
        self.assertIsNone(reset["preview"])
        self.assertFalse(artifact.exists())
        accepted = self.preview(manager)
        self.assertEqual(accepted["status"], "CANDIDATE_WITHIN_TOLERANCE")

    def test_forged_stale_conflicting_and_partial_save_are_effect_neutral(self):
        rejected_manager = self.manager("out-of-tolerance")
        rejected = self.preview(
            rejected_manager,
            y_check_snapshot=self.snapshot([0.8715, 2.09, 3.0]),
        )
        self.assertEqual(rejected["status"], "CANDIDATE_OUT_OF_TOLERANCE")
        before = self.config_bytes()
        with self.assertRaisesRegex(ContractError, "WORKSPACE_PREVIEW_NOT_SAVEABLE"):
            rejected_manager.save(rejected["preview_digest"])
        self.assertEqual(self.config_bytes(), before)

        stale_manager = self.manager("stale-save")
        stale_preview = self.preview(stale_manager)
        before = self.config_bytes()
        with self.assertRaisesRegex(ContractError, "WORKSPACE_PREVIEW_DIGEST_MISMATCH"):
            stale_manager.save(canonical_digest("stale-preview"))
        self.assertEqual(self.config_bytes(), before)

        forged_manager = self.manager("forged")
        forged_preview = self.preview(forged_manager)
        candidate_path = (
            self.candidate_root / forged_manager.calibration_id
            / "cell_calibration_candidate.json"
        )
        candidate = load_json_strict(candidate_path)
        candidate["center_base_m"][0] += 0.01
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        before = self.config_bytes()
        with self.assertRaisesRegex(ContractError, "WORKSPACE_PREVIEW_FORGED"):
            forged_manager.save(forged_preview["preview_digest"])
        self.assertEqual(self.config_bytes(), before)

        conflict_manager = self.manager("Conflict")
        conflict_preview = self.preview(conflict_manager)
        workspace, cell, sheet = conflict_manager._targets()
        workspace.parent.mkdir(parents=True, exist_ok=True)
        cell.parent.mkdir(parents=True, exist_ok=True)
        sheet.parent.mkdir(parents=True, exist_ok=True)
        workspace.write_text("{}\n", encoding="utf-8")
        cell.write_text("{}\n", encoding="utf-8")
        sheet.write_text("{}\n", encoding="utf-8")
        before = self.config_bytes()
        with self.assertRaisesRegex(ContractError, "WORKSPACE_NAME_CONFLICT"):
            conflict_manager.save(conflict_preview["preview_digest"])
        self.assertEqual(self.config_bytes(), before)

        partial_manager = self.manager("Partial")
        partial_preview = self.preview(partial_manager)
        _workspace, cell, _sheet = partial_manager._targets()
        cell.write_text("{}\n", encoding="utf-8")
        before = self.config_bytes()
        with self.assertRaisesRegex(ContractError, "WORKSPACE_PROMOTION_PARTIAL"):
            partial_manager.save(partial_preview["preview_digest"])
        self.assertEqual(self.config_bytes(), before)

    def test_unsafe_or_existing_name_is_effect_neutral(self):
        before = self.config_bytes()
        for display_name in ("../place-b", "Place A", "   "):
            with self.subTest(display_name=display_name):
                with self.assertRaises(ContractError):
                    self.manager("bad-name", display_name=display_name)
                self.assertEqual(self.config_bytes(), before)

    def test_middle_write_failure_rolls_back_and_exact_retry_succeeds(self):
        manager = self.manager("Atomic")
        preview = self.preview(manager)
        before = self.config_bytes()
        original = workspace_manager._write_json_exclusive
        calls = 0

        def fail_second(path, value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected middle write failure")
            return original(path, value)

        with mock.patch.object(
            workspace_manager, "_write_json_exclusive", side_effect=fail_second,
        ):
            with self.assertRaisesRegex(OSError, "injected middle write failure"):
                manager.save(preview["preview_digest"])
        self.assertEqual(self.config_bytes(), before)
        self.assertFalse(any(path.exists() for path in manager._targets()))

        promotion = manager.save(preview["preview_digest"])
        self.assertEqual(promotion["status"], "PROMOTED")
        self.assertTrue(all(path.is_file() for path in manager._targets()))


if __name__ == "__main__":
    unittest.main()
