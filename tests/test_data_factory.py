import copy
import hashlib
import importlib.util
import json
import math
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path); module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader; sys.modules[name] = module; spec.loader.exec_module(module); return module

factory = _module("fr5_data_factory", ROOT / "tools/fr5_data_factory.py")
generator = _module("a4_generator_for_test", ROOT / "tools/a4_place_yaw/generate_place_yaw_a4.py")
NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def digest(label):
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


class DataFactoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        for directory in ("robot_systems", "collection_profiles", "objects", "grasps", "cells"): (self.root / directory).mkdir()
        self._write("robot_systems/fr5-lab-a.json", {"schema_version":"data_factory.robot_system.v1","robot_system_id":"fr5-lab-a","qualification_status":"QUALIFIED","base_frame":"base_link","tcp_digest":digest("tcp"),"state_action_schema_digest":digest("state")})
        self._write("collection_profiles/fr5-dual-rgb-30hz-v1.json", {"schema_version":"data_factory.collection_profile.v1","collection_profile_id":"fr5-dual-rgb-30hz-v1","qualification_status":"QUALIFIED","quality_contract_digest":digest("quality")})
        self._write("objects/OBJECT_A.json", {"schema_version":"data_factory.object_profile.v1","object_profile_id":"OBJECT_A","qualification_status":"QUALIFIED","object_datum_digest":digest("datum")})
        self._write("grasps/top_center.json", {"schema_version":"data_factory.grasp_profile.v1","grasp_profile_id":"top_center","qualification_status":"QUALIFIED","object_profile_id":"OBJECT_A","grasp_margin_mm":20,"grasp_contract_digest":digest("grasp")})
        self.yaw0, self.selected = self._sheet(0), self._sheet(30)
        self.job = {"schema_version":"data_factory.job.v1","job_id":"job-1","task":"pickup_e2e","robot_system_id":"fr5-lab-a","collection_profile_id":"fr5-dual-rgb-30hz-v1","place_id":"PLACE_A","cell_calibration_id":"cal-a","sheet_manifest_digest":factory.canonical_digest(self.selected),"yaw_deg":30,"x_mm":-35,"y_mm":35,"object_profile_id":"OBJECT_A","grasp_profile_id":"top_center","instruction":"pick up the object","episode_intent":"nominal pickup","operator_or_agent_id":"operator-1","approval_expiry":"2099-01-01T00:00:00Z","dry_run_required":True}
        self.calibration = {"schema_version":"data_factory.cell_calibration.v1","calibration_id":"cal-a","qualification_status":"QUALIFIED","robot_system_id":"fr5-lab-a","place_id":"PLACE_A","yaw0_manifest_digest":factory.canonical_digest(self.yaw0),"a4_family_digest":self.yaw0["a4_family_digest"],"tcp_digest":digest("tcp"),"measurement_report_digest":digest("measurements"),"table_plane_measurement_digest":digest("plane"),"center_base_m":[1,2,3],"x_ref_base_m":[1.1285,2,3],"y_check_base_m":[0.8715,2.08,3],"table_normal_base":[0,0,1],"print_source_scale_bar_measured_mm":100,"scale_bar_measured_mm":100,"limits":{"max_scale_error_mm":1,"min_x_ref_separation_mm":100,"max_x_ref_distance_error_mm":1,"max_x_ref_out_of_plane_mm":1,"max_y_check_residual_mm":1,"combined_error_bound_mm":10},"measured_at":"2026-08-13T00:00:00Z"}
        self.cell_path = self._write("cells/cal-a.json", self.calibration)

    def tearDown(self): self.temp.cleanup()
    def test_home_candidate_is_non_executable_and_urdf_bounded(self):
        candidate = {
            "schema_version": "data_factory.home_candidate.v1", "home_candidate_id": "fr5-lab-a-home-r001", "robot_system_id": "fr5-lab-a",
            "robot_model_name": "fairino5_v6_robot", "robot_description_digest": "sha256:" + hashlib.sha256((ROOT / "src/fairino_description/urdf/fairino5_v6.urdf").read_bytes()).hexdigest(),
            "joint_order": ["j1", "j2", "j3", "j4", "j5", "j6"],
            "ui_observation_deg": [-89.913, -90.001, 90, -90, -90, 0],
            "nominal_target_deg": [-90, -90, 90, -90, -90, 0],
            "observation_source": "controller_web_ui", "feedback_capture_status": "NOT_CAPTURED", "qualification_status": "CANDIDATE",
            "safety_status": "NOT_SAFE_FOR_MOTION", "intended_use_after_qualification": "SAFE_POSE_PTP",
        }
        urdf = ROOT / "src/fairino_description/urdf/fairino5_v6.urdf"
        validated = factory.validate_home_candidate(candidate, urdf=urdf, expected_robot_system_id="fr5-lab-a")
        self.assertFalse(validated["motion_allowed"])
        self.assertEqual(candidate["ui_observation_deg"], [-89.913, -90.001, 90, -90, -90, 0])
        self.assertEqual(validated["nominal_target_rad"], [math.radians(value) for value in candidate["nominal_target_deg"]])
        self.assertEqual(validated["candidate_digest"], factory.canonical_digest(candidate))
        for changed, code in (
            ({**candidate, "unknown": True}, "HOME_KEYS"),
            ({key: value for key, value in candidate.items() if key != "feedback_capture_status"}, "HOME_KEYS"),
            ({**candidate, "ui_observation_deg": [float("nan")] * 6}, "HOME_JOINT_VALUES"),
            ({**candidate, "joint_order": ["j2", "j1", "j3", "j4", "j5", "j6"]}, "HOME_JOINT_ORDER"),
            ({**candidate, "nominal_target_deg": [180, -90, 90, -90, -90, 0]}, "HOME_JOINT_LIMIT"),
            ({**candidate, "robot_system_id": "other-robot"}, "HOME_ROBOT_BINDING"),
            ({**candidate, "robot_description_digest": digest("other-urdf")}, "HOME_ROBOT_BINDING"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(factory.ContractError) as caught: factory.validate_home_candidate(changed, urdf=urdf, expected_robot_system_id="fr5-lab-a")
                self.assertEqual(caught.exception.code, code)
        relabelled = {**candidate, "robot_system_id": "other-robot", "home_candidate_id": "other-robot-home-r001"}
        with self.assertRaises(factory.ContractError) as caught:
            factory.validate_home_candidate(relabelled, urdf=urdf, expected_robot_system_id="fr5-lab-a")
        self.assertEqual(caught.exception.code, "HOME_ROBOT_BINDING")
        candidate_path = self._write("home.json", candidate)
        command = [sys.executable, str(ROOT / "tools/fr5_data_factory.py"), "validate-home-candidate", "--candidate", str(candidate_path), "--urdf", str(urdf), "--expected-robot-system-id", "fr5-lab-a"]
        run = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), validated)
        missing_urdf = command.copy()
        missing_urdf[missing_urdf.index("--urdf") + 1] = str(urdf.with_name("missing.urdf"))
        failed = subprocess.run(missing_urdf, text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(json.loads(failed.stderr)["error"]["code"], "HOME_URDF")
        invalid_utf8 = subprocess.run(command[:command.index("--candidate") + 1] + ["-"] + command[command.index("--candidate") + 2:], input=b"\xff", capture_output=True)
        self.assertEqual(invalid_utf8.returncode, 2)
        self.assertEqual(json.loads(invalid_utf8.stderr)["error"]["code"], "JSON_IO")

    def test_motion_qualification_resolves_only_bound_evidence(self):
        urdf = ROOT / "src/fairino_description/urdf/fairino5_v6.urdf"
        candidate = {"schema_version":"data_factory.home_candidate.v1","home_candidate_id":"fr5-lab-a-home-r001","robot_system_id":"fr5-lab-a","robot_model_name":"fairino5_v6_robot","robot_description_digest":"sha256:" + hashlib.sha256(urdf.read_bytes()).hexdigest(),"joint_order":["j1","j2","j3","j4","j5","j6"],"ui_observation_deg":[-89.913,-90.001,90,-90,-90,0],"nominal_target_deg":[-90,-90,90,-90,-90,0],"observation_source":"controller_web_ui","feedback_capture_status":"NOT_CAPTURED","qualification_status":"CANDIDATE","safety_status":"NOT_SAFE_FOR_MOTION","intended_use_after_qualification":"SAFE_POSE_PTP"}
        validated = self._validated()
        arm = {"velocity_scaling":.1,"acceleration_scaling":.1,"planning_timeout_s":1,"execution_timeout_s":2}
        grip = {"command_duration_s":1,"execution_timeout_s":2,"completion_tolerance_m":.001}
        qualification = {"schema_version":"data_factory.motion_qualification.v1","motion_qualification_id":"motion-q1","qualification_status":"QUALIFIED","robot_system_id":"fr5-lab-a","cell_calibration_id":"cal-a","object_profile_id":"OBJECT_A","grasp_profile_id":"top_center","profile_digests":{key:validated["input_digests"][key] for key in ("robot_system","cell_calibration","object_profile","grasp_profile")},"home_candidate_digest":factory.canonical_digest(candidate),"robot_description_digest":candidate["robot_description_digest"],"moveit_config_digest":digest("moveit"),"planning_scene_digest":digest("scene"),"frames":{"planning_frame":"base_link","planning_group":"fairino5_v6_group","tool_link":"wrist3_link"},"tool_to_tcp":{"translation_m":[.01,.02,.03],"rotation_columns":[[0,1,0],[-1,0,0],[0,0,1]]},"datum_to_tcp_grasp":{"translation_m":[.1,.2,.3],"rotation_columns":[[1,0,0],[0,0,1],[0,-1,0]]},"offsets_m":{"pregrasp":.1,"approach_stop":.02,"lift":.04,"retreat":.1},"gripper_positions_m":{"open":.02,"closed":.005},"qualified_safe_joint_positions_rad":[math.radians(v) for v in candidate["nominal_target_deg"]],"execution_timeouts_s":{"heartbeat_lease":1,"cancel":1,"precontact_confirmation":30,"semantic_verdict":30},"phase_limits":{phase:(grip if phase.startswith("GRIPPER") else arm) for phase in factory.MOTION_PHASES},"goal_tolerances":{"position_m":.001,"orientation_rad":.01,"joint_rad":.01},"max_joint_state_age_s":.1,"qualified_at":"2026-08-13T00:00:00Z"}
        program = factory.resolve_motion_program(validated, qualification, candidate, urdf=urdf, expected_robot_system_id="fr5-lab-a", now=NOW)
        self.assertEqual(factory.validate_motion_program(program), program)
        self.assertEqual([step["phase"] for step in program["steps"]], list(factory.MOTION_PHASES))
        final = program["steps"][2]["target"]["base_tcp"]["translation_m"]
        for observed, expected in zip(final, [.9387916513, 2.236016, 3.3]): self.assertAlmostEqual(observed, expected, places=7)
        self.assertNotEqual(program["steps"][1]["target"], program["steps"][2]["target"])
        self.assertEqual(program["planning"]["goal_tolerances"], qualification["goal_tolerances"])
        self.assertEqual(program["robot_system_id"], "fr5-lab-a")
        self.assertEqual(program["execution_timeouts_s"], qualification["execution_timeouts_s"])
        self.assertEqual(program["steps"][2]["requires_confirmation"], "PRECONTACT_HUMAN")
        self.assertEqual(program["steps"][4]["pause_after"], "SEMANTIC_VERDICT")
        selected, yaw0, job, qualification_path, candidate_path = (self._write(name, value) for name, value in (("selected.json", self.selected), ("yaw0.json", self.yaw0), ("job.json", self.job), ("motion.json", qualification), ("home.json", candidate)))
        run = subprocess.run([sys.executable, str(ROOT / "tools/fr5_data_factory.py"), "resolve-motion", "--job", str(job), "--selected-sheet", str(selected), "--yaw0-sheet", str(yaw0), "--config-root", str(self.root), "--motion-qualification", str(qualification_path), "--home-candidate", str(candidate_path), "--urdf", str(urdf), "--expected-robot-system-id", "fr5-lab-a"], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), factory.resolve_motion_program(factory.validate_job_spec(self.job, paths={"selected_sheet": selected, "yaw0_sheet": yaw0}, config_root=self.root), qualification, candidate, urdf=urdf, expected_robot_system_id="fr5-lab-a"))
        for bad, code in (({**qualification,"qualification_status":"CANDIDATE"},"MOTION_STATUS"), ({**qualification,"home_candidate_digest":digest("wrong")},"MOTION_HOME_BINDING"), ({key:value for key,value in qualification.items() if key != "goal_tolerances"},"MOTION_KEYS"), ({**qualification,"frames":{**qualification["frames"],"planning_frame":"bogus"}},"MOTION_FRAMES"), ({**qualification,"goal_tolerances":{**qualification["goal_tolerances"],"joint_rad":0}},"MOTION_TOLERANCES"), ({**qualification,"execution_timeouts_s":{**qualification["execution_timeouts_s"],"cancel":0}},"MOTION_EXECUTION_TIMEOUTS"), ({**qualification,"phase_limits":{**qualification["phase_limits"],"LIFT_LIN":{**arm,"velocity_scaling":.2}}},"MOTION_PHASE_LIMITS"), ({**qualification,"datum_to_tcp_grasp":{"translation_m":[0,0,0],"rotation_columns":[[1,0,0],[0,1,0],[0,0,-1]]}},"MOTION_TRANSFORM")):
            with self.subTest(code=code):
                with self.assertRaises(factory.ContractError) as caught: factory.resolve_motion_program(validated, bad, candidate, urdf=urdf, expected_robot_system_id="fr5-lab-a", now=NOW)
                self.assertEqual(caught.exception.code, code)
    def _write(self, relative, value):
        path = self.root / relative; path.parent.mkdir(exist_ok=True); path.write_text(json.dumps(value)); return path
    def _sheet(self, yaw, measured_scale_mm=100):
        return generator.make_manifest("PLACE_A", f"PLACE_A_{yaw}", yaw, generator.build_places(3, 3, 35, yaw), 35, measured_scale_mm)
    def _validated(self, **change):
        job = change.get("job", self.job); selected = change.get("selected", self.selected); yaw0 = change.get("yaw0", self.yaw0)
        calibration = change.get("calibration", self.calibration)
        if "calibration" in change:
            self._write("cells/cal-a.json", calibration)
        elif not self.cell_path.is_symlink():
            self._write("cells/cal-a.json", self.calibration)
        data = {"selected_sheet":selected,"yaw0_sheet":yaw0}
        return factory.validate_job_spec(job, data=data, config_root=self.root, now=NOW)
    def _error(self, code, **change):
        with self.assertRaises(factory.ContractError) as caught: self._validated(**change)
        self.assertEqual(caught.exception.code, code)

    def test_nominal_file_stdin_and_rotation(self):
        validated = self._validated(); pose = factory.resolve_pose(validated)
        self.assertEqual(factory.normalize_job_spec({**self.job,"yaw_deg":30.0}, now=NOW)["yaw_deg"], 30)
        self.assertEqual(pose["resolved_job_digest"], validated["resolved_job_digest"])
        cols = pose["rotation_base_columns"]
        for column in cols:
            self.assertAlmostEqual(sum(value * value for value in column), 1)
        for left, right in ((0, 1), (0, 2), (1, 2)):
            self.assertAlmostEqual(sum(cols[left][i] * cols[right][i] for i in range(3)), 0)
        cross = [cols[0][1] * cols[1][2] - cols[0][2] * cols[1][1], cols[0][2] * cols[1][0] - cols[0][0] * cols[1][2], cols[0][0] * cols[1][1] - cols[0][1] * cols[1][0]]
        for observed, expected in zip(cross, cols[2]):
            self.assertAlmostEqual(observed, expected)
        selected, yaw0, job = (self._write(name, value) for name, value in (("selected.json",self.selected),("yaw0.json",self.yaw0),("job.json",self.job)))
        cell = self.cell_path
        base = [sys.executable,str(ROOT/"tools/fr5_data_factory.py"),"validate-job","--selected-sheet",str(selected),"--yaw0-sheet",str(yaw0),"--config-root",str(self.root)]
        file_run = subprocess.run(base+["--job",str(job)], text=True,capture_output=True)
        stdin_run = subprocess.run(base+["--job","-"], input=json.dumps(self.job), text=True,capture_output=True)
        self.assertEqual((file_run.returncode,stdin_run.returncode),(0,0)); self.assertEqual(json.loads(file_run.stdout),json.loads(stdin_run.stdout))
        self.assertIn("normalized_job", json.loads(file_run.stdout))

    def test_json_job_profile_and_path_boundaries(self):
        for value, code in (({"a":float("nan")},"JSON_NONFINITE"), ('{"a":1,"a":2}',"JSON_DUPLICATE_KEY"), ("[]","JSON_ROOT")):
            with self.subTest(code=code):
                with self.assertRaises(factory.ContractError) as caught:
                    factory.load_json_strict(json.dumps(value, allow_nan=True) if isinstance(value,dict) else value)
                self.assertEqual(caught.exception.code, code)
        for job, code in (({**self.job,"extra":1},"JOB_KEYS"), ({key:value for key,value in self.job.items() if key!="x_mm"},"JOB_KEYS"), ({**self.job,"task":"pick_place"},"JOB_TASK"), ({**self.job,"grasp_profile_id":"side"},"JOB_GRASP"), ({**self.job,"instruction":"pick up"},"JOB_TEXT"), ({**self.job,"x_mm":float("nan")},"JOB_NUMBER"), ({**self.job,"approval_expiry":"2026-08-15 00:00:00Z"},"JOB_EXPIRY"), ({**self.job,"approval_expiry":"2026-08-13T00:00:00Z"},"JOB_EXPIRED")):
            with self.subTest(code=code):
                with self.assertRaises(factory.ContractError) as caught: factory.normalize_job_spec(job,now=NOW)
                self.assertEqual(caught.exception.code,code)
        offset_job = {**self.job, "approval_expiry": "2099-01-01T09:00:00+09:00"}
        self.assertEqual(factory.normalize_job_spec(offset_job, now=NOW)["approval_expiry"], self.job["approval_expiry"])
        self._write("objects/OBJECT_A.json", {"schema_version":"wrong","object_profile_id":"OBJECT_A","qualification_status":"QUALIFIED"}); self._error("PROFILE_SCHEMA")
        self._write("objects/OBJECT_A.json", {"schema_version":"data_factory.object_profile.v1","object_profile_id":"OBJECT_A","qualification_status":"QUALIFIED"})
        with tempfile.TemporaryDirectory() as external_root:
            outside = Path(external_root) / "outside.json"
            outside.write_text(json.dumps({"schema_version":"data_factory.object_profile.v1","object_profile_id":"OBJECT_A","qualification_status":"QUALIFIED"}))
            (self.root/"objects/OBJECT_A.json").unlink()
            (self.root/"objects/OBJECT_A.json").symlink_to(outside)
            self._error("PROFILE_PATH")

    def test_cell_binding_and_a4_integrity(self):
        before = self._validated()["resolved_job_digest"]
        changed = copy.deepcopy(self.calibration)
        changed["measurement_report_digest"] = digest("changed-measurements")
        self.assertNotEqual(before, self._validated(calibration=changed)["resolved_job_digest"])
        bad = copy.deepcopy(self.selected); bad["a4_family_digest"] = "sha256:"+"0"*64; job={**self.job,"sheet_manifest_digest":factory.canonical_digest(bad)}; self._error("SHEET_FAMILY_DIGEST",job=job,selected=bad)
        bad = copy.deepcopy(self.selected); bad["grid_points"][0]["job_pose"]["x_mm"] = 999; job={**self.job,"sheet_manifest_digest":factory.canonical_digest(bad)}; self._error("SHEET_GRID_POSE",job=job,selected=bad)
        bad = copy.deepcopy(self.selected); yaw0 = copy.deepcopy(self.yaw0); bad["grid_points"].append(copy.deepcopy(next(point for point in bad["grid_points"] if point["job_pose"]["x_mm"] == self.job["x_mm"] and point["job_pose"]["y_mm"] == self.job["y_mm"]))); yaw0["grid_points"].append(copy.deepcopy(yaw0["grid_points"][1])); family = generator.family_digest_from_manifest(bad); bad["a4_family_digest"] = family; yaw0["a4_family_digest"] = family; calibration = copy.deepcopy(self.calibration); calibration["yaw0_manifest_digest"] = factory.canonical_digest(yaw0); calibration["a4_family_digest"] = family; job={**self.job,"sheet_manifest_digest":factory.canonical_digest(bad)}; self._error("SHEET_GRID",job=job,selected=bad,yaw0=yaw0,calibration=calibration)
        bad = copy.deepcopy(self.selected); bad["grid_points"][1]["relative_pose_place0"]["x_mm"] += 2; bad["a4_family_digest"] = generator.family_digest_from_manifest(bad); job={**self.job,"sheet_manifest_digest":factory.canonical_digest(bad)}; self._error("SHEET_ROTATION",job=job,selected=bad)
        bad = copy.deepcopy(self.selected); del bad["registration"]; job = {**self.job, "sheet_manifest_digest": factory.canonical_digest(bad)}; self._error("SHEET_SCHEMA", job=job, selected=bad)
        bad = copy.deepcopy(self.calibration); bad["measured_at"]="2026-08-15T00:00:00Z"; self._error("CALIBRATION_FUTURE",calibration=bad)

    def test_calibration_gates_and_yaws(self):
        for yaw in (0,30,90):
            with self.subTest(yaw=yaw):
                selected=self._sheet(yaw); job={**self.job,"yaw_deg":yaw,"sheet_manifest_digest":factory.canonical_digest(selected)}
                pose=factory.resolve_pose(self._validated(job=job,selected=selected)); cols=pose["rotation_base_columns"]
                self.assertAlmostEqual(sum(cols[0][i]*cols[2][i] for i in range(3)),0); self.assertAlmostEqual(sum(cols[1][i]*cols[2][i] for i in range(3)),0)
        yaw0, selected = self._sheet(0, 96), self._sheet(30, 96)
        calibration = copy.deepcopy(self.calibration)
        calibration["yaw0_manifest_digest"] = factory.canonical_digest(yaw0)
        calibration["a4_family_digest"] = yaw0["a4_family_digest"]
        calibration["print_source_scale_bar_measured_mm"] = 96
        job = {**self.job, "sheet_manifest_digest": factory.canonical_digest(selected)}
        nominal = factory.resolve_pose(self._validated())
        compensated = factory.resolve_pose(self._validated(job=job, selected=selected, yaw0=yaw0, calibration=calibration))
        self.assertEqual(nominal["position_base_m"], compensated["position_base_m"])
        cases = (
            ("CALIBRATION_X_DEGENERATE", {"x_ref_base_m": [1, 2, 3]}),
            ("CALIBRATION_OUT_OF_PLANE", {"x_ref_base_m": [1.1285, 2, 3.01]}),
            ("CALIBRATION_DISTANCE", {"x_ref_base_m": [1.2, 2, 3]}),
            ("CALIBRATION_SCALE", {"scale_bar_measured_mm": 102}),
            ("CALIBRATION_PRINT_SCALE", {"print_source_scale_bar_measured_mm": 0}),
            ("CALIBRATION_Y_CHECK", {"y_check_base_m": [.8715, 2.09, 3]}),
            ("CALIBRATION_COMBINED_LIMIT", {"limits": {**self.calibration["limits"], "combined_error_bound_mm": 21}}),
            ("CALIBRATION_COMBINED_ERROR", {"limits": {**self.calibration["limits"], "max_y_check_residual_mm": 100}, "y_check_base_m": [.8715, 2.095, 3]}),
        )
        for code, change in cases:
            with self.subTest(code=code):
                bad=copy.deepcopy(self.calibration); bad.update(change); self._error(code,calibration=bad)

    def test_cli_missing_job_is_stable(self):
        result=subprocess.run([sys.executable,str(ROOT/"tools/fr5_data_factory.py"),"validate-job","--job",str(self.root/"none.json"),"--selected-sheet","x","--yaw0-sheet","x","--config-root",str(self.root)],text=True,capture_output=True)
        self.assertEqual(result.returncode,2); self.assertEqual(json.loads(result.stderr)["error"]["code"],"JOB_IO")

    def test_hardcoded_metric_pose_goldens(self):
        goldens = {
            0: [0.965, 2.035, 3.0],
            30: [0.9521891109, 2.0128108891, 3.0],
            90: [0.965, 1.965, 3.0],
        }
        for yaw, expected in goldens.items():
            with self.subTest(yaw=yaw):
                selected = self._sheet(yaw)
                job = {**self.job, "yaw_deg": yaw, "sheet_manifest_digest": factory.canonical_digest(selected)}
                pose = factory.resolve_pose(self._validated(job=job, selected=selected))
                for observed, golden in zip(pose["position_base_m"], expected):
                    self.assertAlmostEqual(observed, golden, places=8)

    def test_sheet_mutations_fail_closed(self):
        cases = (("page_mm", "SHEET_PAGE"), ("transform_contract", "SHEET_TRANSFORM"), ("grid_points", "SHEET_GRID"))
        for field, code in cases:
            with self.subTest(field=field):
                bad = copy.deepcopy(self.selected)
                if field == "page_mm":
                    bad[field]["width"] = 298
                elif field == "transform_contract":
                    bad[field]["position"] = "wrong"
                else:
                    bad[field][0]["sheet_xy_mm"][0] += 1
                bad["a4_family_digest"] = generator.family_digest_from_manifest(bad)
                job = {**self.job, "sheet_manifest_digest": factory.canonical_digest(bad)}
                self._error(code, job=job, selected=bad)

    def test_a4_v2_contract_golden(self):
        manifest = self._sheet(30, 96)
        self.assertEqual(manifest["schema_version"], "a4_place_yaw.v2")
        self.assertEqual(manifest["page_mm"], {"width": 297.0, "height": 210.0})
        self.assertEqual(manifest["registration"]["origin"], {"id": "CENTER", "sheet_xy_mm": [148.5, 105.0]})
        self.assertEqual(manifest["print_calibration"], {"nominal_scale_bar_mm": 100.0, "measured_scale_bar_mm": 96.0, "content_scale_percent": 104.166667})
        self.assertEqual(manifest["a4_family_digest"], generator.family_digest_from_manifest(manifest))
        center = next(point for point in manifest["grid_points"] if point["point_id"] == "CENTER")
        self.assertEqual(center["job_pose"], {"place_id": "PLACE_A", "yaw_deg": 30, "x_mm": 0, "y_mm": 0})
        manifest["transform_contract"]["position"] = "tampered"
        self.assertEqual(self._sheet(30, 96)["transform_contract"], generator.TRANSFORM_CONTRACT)

    def test_bound_input_changes_resolved_digest(self):
        validated = self._validated()
        baseline = validated["resolved_job_digest"]
        mutations = (
            ("robot_systems/fr5-lab-a.json", "state_action_schema_digest"),
            ("collection_profiles/fr5-dual-rgb-30hz-v1.json", "quality_contract_digest"),
            ("objects/OBJECT_A.json", "object_datum_digest"),
            ("grasps/top_center.json", "grasp_contract_digest"),
        )
        for relative, field in mutations:
            with self.subTest(relative=relative):
                path = self.root / relative
                original = json.loads(path.read_text())
                changed = {**original, field: digest(relative + field)}
                self._write(relative, changed)
                self.assertNotEqual(baseline, self._validated()["resolved_job_digest"])
                self._write(relative, original)

        selected = copy.deepcopy(self.selected)
        selected["sheet_id"] = "PLACE_A_30_REPRINT"
        job = {**self.job, "sheet_manifest_digest": factory.canonical_digest(selected)}
        changed = self._validated(job=job, selected=selected)
        self.assertNotEqual(validated["input_digests"]["selected_sheet"], changed["input_digests"]["selected_sheet"])
        self.assertNotEqual(baseline, changed["resolved_job_digest"])

        yaw0 = copy.deepcopy(self.yaw0)
        yaw0["sheet_id"] = "PLACE_A_0_REPRINT"
        calibration = {**self.calibration, "yaw0_manifest_digest": factory.canonical_digest(yaw0)}
        changed = self._validated(yaw0=yaw0, calibration=calibration)
        self.assertNotEqual(validated["input_digests"]["yaw0_sheet"], changed["input_digests"]["yaw0_sheet"])
        self.assertNotEqual(baseline, changed["resolved_job_digest"])

    def test_profile_shape_and_symlink_confinement(self):
        path = self.root / "grasps/top_center.json"
        profile = json.loads(path.read_text())
        for label, changed in (("extra", {**profile, "future": True}), ("missing", {key: value for key, value in profile.items() if key != "grasp_contract_digest"}), ("type", {**profile, "grasp_margin_mm": "20"}), ("digest", {**profile, "grasp_contract_digest": "bad"})):
            with self.subTest(label=label):
                self._write("grasps/top_center.json", changed)
                self._error("PROFILE_SCHEMA" if label in ("extra", "missing") else "PROFILE_DIGEST" if label == "digest" else "GRASP_MARGIN")
                self._write("grasps/top_center.json", profile)
        with tempfile.TemporaryDirectory() as outside_root:
            outside = Path(outside_root) / "outside.json"
            outside.write_text(path.read_text())
            for relative in ("robot_systems/fr5-lab-a.json", "cells/cal-a.json"):
                with self.subTest(relative=relative):
                    local = self.root / relative
                    original = local.read_text()
                    local.unlink()
                    local.symlink_to(outside)
                    self._error("PROFILE_PATH")
                    local.unlink()
                    local.write_text(original)

    def test_resolve_cli_and_metric_gates(self):
        selected = self._write("selected.json", self.selected)
        yaw0 = self._write("yaw0.json", self.yaw0)
        job_file = self._write("job.json", self.job)
        command = [sys.executable, str(ROOT / "tools/fr5_data_factory.py"), "resolve-pose", "--job", str(job_file), "--selected-sheet", str(selected), "--yaw0-sheet", str(yaw0), "--config-root", str(self.root)]
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), factory.resolve_pose(self._validated()))
        stdin_command = command.copy()
        stdin_command[stdin_command.index("--job") + 1] = "-"
        stdin_result = subprocess.run(stdin_command, input=json.dumps(self.job), text=True, capture_output=True)
        self.assertEqual(stdin_result.returncode, 0, stdin_result.stderr)
        self.assertEqual(json.loads(stdin_result.stdout), json.loads(result.stdout))
        for flag, value, code in (
            ("--selected-sheet", self.root / "missing.json", "INPUT_SELECTED_SHEET"),
            ("--yaw0-sheet", self.root / "missing.json", "INPUT_YAW0_SHEET"),
        ):
            with self.subTest(flag=flag):
                bad = command.copy()
                bad[bad.index(flag) + 1] = str(value)
                failed = subprocess.run(bad, text=True, capture_output=True)
                self.assertEqual(failed.returncode, 2)
                self.assertEqual(json.loads(failed.stderr)["error"]["code"], code)
        calibration = copy.deepcopy(self.calibration)
        calibration["x_ref_base_m"] = [1.1284, 2, 3]
        calibration["scale_bar_measured_mm"] = 99.5
        pose = factory.resolve_pose(self._validated(calibration=calibration))
        baseline = factory.resolve_pose(self._validated(calibration=self.calibration))
        self.assertEqual(pose["position_base_m"], baseline["position_base_m"])

    def test_build_job_accepts_id_coordinates_and_interactive_number(self):
        self.assertEqual(factory._select_id_or_number("1", ["0", "1"]), "1")
        self.assertEqual(factory._select_id_or_number("2", ["A", "B"]), "B")
        selected = self._write("selected.json", self.selected)
        yaw0 = self._write("yaw0.json", self.yaw0)
        base = [
            sys.executable,
            str(ROOT / "tools/fr5_data_factory.py"),
            "build-job",
            "--selected-sheet", str(selected),
            "--yaw0-sheet", str(yaw0),
            "--config-root", str(self.root),
            "--job-id", "job-1",
            "--robot-system-id", "fr5-lab-a",
            "--collection-profile-id", "fr5-dual-rgb-30hz-v1",
            "--cell-calibration-id", "cal-a",
            "--object-profile-id", "OBJECT_A",
            "--grasp-profile-id", "top_center",
            "--operator-or-agent-id", "operator-1",
            "--approval-expiry", "2099-01-01T00:00:00Z",
        ]
        by_id = subprocess.run(base + ["--point-id", "GRID_1"], text=True, capture_output=True)
        continuous_job = {**self.job, "x_mm": -30, "y_mm": 30}
        by_xy = subprocess.run(base + ["--x-mm", "-30", "--y-mm", "30"], text=True, capture_output=True)
        interactive = subprocess.run(base + ["--interactive"], input="2\n", text=True, capture_output=True)
        interactive_id = subprocess.run(base + ["--interactive"], input="GRID_1\n", text=True, capture_output=True)
        interactive_xy = subprocess.run(base + ["--interactive"], input="-30,30\n", text=True, capture_output=True)
        for result in (by_id, interactive, interactive_id):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), self.job)
        for result in (by_xy, interactive_xy):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), continuous_job)
        self.assertIn("GRID_1", interactive.stderr)
        self.assertEqual(interactive.stdout.count("\n"), 1)

        self._write("collection_profiles/z-profile.json", {"schema_version":"data_factory.collection_profile.v1","collection_profile_id":"z-profile","qualification_status":"QUALIFIED","quality_contract_digest":digest("z-quality")})
        profile_base = base.copy()
        profile_index = profile_base.index("--collection-profile-id")
        del profile_base[profile_index:profile_index + 2]
        by_profile_number = subprocess.run(profile_base + ["--interactive", "--point-id", "GRID_1"], input="1\n", text=True, capture_output=True)
        by_profile_id = subprocess.run(profile_base + ["--interactive", "--point-id", "GRID_1"], input="fr5-dual-rgb-30hz-v1\n", text=True, capture_output=True)
        for result in (by_profile_number, by_profile_id):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), self.job)

        validate = [
            sys.executable,
            str(ROOT / "tools/fr5_data_factory.py"),
            "validate-job",
            "--job", "-",
            "--selected-sheet", str(selected),
            "--yaw0-sheet", str(yaw0),
            "--config-root", str(self.root),
        ]
        piped = subprocess.run(validate, input=by_xy.stdout, text=True, capture_output=True)
        self.assertEqual(piped.returncode, 0, piped.stderr)
        self.assertEqual(json.loads(piped.stdout)["normalized_job"], continuous_job)

    def test_build_job_rejects_missing_conflicting_and_unknown_points(self):
        selected = self._write("selected.json", self.selected)
        yaw0 = self._write("yaw0.json", self.yaw0)
        base = [
            sys.executable,
            str(ROOT / "tools/fr5_data_factory.py"),
            "build-job",
            "--selected-sheet", str(selected),
            "--yaw0-sheet", str(yaw0),
            "--config-root", str(self.root),
            "--job-id", "job-1",
            "--robot-system-id", "fr5-lab-a",
            "--collection-profile-id", "fr5-dual-rgb-30hz-v1",
            "--cell-calibration-id", "cal-a",
            "--object-profile-id", "OBJECT_A",
            "--grasp-profile-id", "top_center",
            "--operator-or-agent-id", "operator-1",
            "--approval-expiry", "2099-01-01T00:00:00Z",
        ]
        cases = (
            ([], "CLI_INPUT_REQUIRED"),
            (["--x-mm", "-35"], "JOB_BUILDER_INPUT"),
            (["--point-id", "GRID_1", "--x-mm", "-35", "--y-mm", "35"], "JOB_BUILDER_INPUT"),
            (["--point-id", "GRID_999"], "JOB_POINT"),
            (["--x-mm", "36", "--y-mm", "0"], "JOB_COORDINATE_BOUNDS"),
            (["--x-mm", "nope", "--y-mm", "0"], "JOB_BUILDER_INPUT"),
        )
        for extra, code in cases:
            with self.subTest(code=code):
                result = subprocess.run(base + extra, text=True, capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(json.loads(result.stderr)["error"]["code"], code)
        self._error("JOB_COORDINATE_BOUNDS", job={**self.job, "x_mm": 36, "y_mm": 0})

if __name__ == "__main__": unittest.main()
