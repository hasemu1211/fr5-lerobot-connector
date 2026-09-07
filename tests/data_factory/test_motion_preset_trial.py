"""Candidate speed trials retain base qualification and TEST_ONLY identity."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from tools import fr5_data_factory as factory
from tools.data_factory import run_job
from tools.data_factory.motion.trajectory_variants import compile_execution_motion_program


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config/data_factory"


class MotionPresetTrialTests(unittest.TestCase):
    def setUp(self):
        self.qualification = factory.load_json_strict(
            CONFIG / "motion_qualifications/fr5-place-a-wood-cube-24mm-r001.json")
        self.preset = factory.load_json_strict(
            CONFIG / "motion_presets/practical-transfer-r001.json")
        self.home = factory.load_json_strict(
            CONFIG / "home_candidates/fr5-lab-a-tcp-r002-home-r001.json")
        self.sheet = CONFIG / "test_only_physical/goal2-place1/yaw0_sheet.json"
        self.job = factory.load_json_strict(CONFIG / "jobs/center-live-24mm-20260903-r002.job.json")
        self.validated = factory.validate_job_spec(
            self.job, paths={"selected_sheet": self.sheet, "yaw0_sheet": self.sheet},
            config_root=CONFIG)
        self.options = {
            "urdf": ROOT / "src/fairino_description/urdf/fairino5_v6.urdf",
            "expected_robot_system_id": self.qualification["robot_system_id"],
            "planning_scene_profile": factory.load_json_strict(
                CONFIG / f"planning_scenes/{self.qualification['planning_scene_profile_id']}.json"),
        }

    def resolve(self, **options):
        return factory.resolve_motion_program(
            self.validated, self.qualification, self.home,
            **self.options, **options)

    def trial(self):
        return self.resolve(motion_preset=self.preset, motion_preset_trial=True)

    def test_trial_preserves_original_qualification_geometry_and_gripper(self):
        original = copy.deepcopy((self.qualification, self.preset))
        base = self.resolve()
        trial = self.trial()
        self.assertEqual(factory.validate_motion_program(trial), trial)
        self.assertNotEqual(factory.canonical_digest(base), factory.canonical_digest(trial))
        self.assertEqual(trial["binding_digests"]["motion_qualification"],
                         factory.canonical_digest(self.qualification))
        self.assertIn("motion_preset_trial", trial["binding_digests"])
        for old, new in zip(base["steps"], trial["steps"]):
            expected = copy.deepcopy(old)
            if old["phase"] in self.preset["phase_scaling"]:
                expected["limits"].update(self.preset["phase_scaling"][old["phase"]])
            self.assertEqual(new, expected)
        for key in ("frames", "planning", "planning_scene", "gripper_requirements", "execution_timeouts_s"):
            self.assertEqual(base[key], trial[key])
        self.assertEqual((self.qualification, self.preset), original)
        self.assertEqual(self.resolve(), base)

    def test_trial_cannot_replace_base_qualification_or_normal_policy_binding(self):
        with self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_QUALIFICATION_REQUIRED"):
            self.resolve(motion_preset=self.preset)
        self.qualification["qualification_status"] = "UNQUALIFIED"
        with self.assertRaisesRegex(factory.ContractError, "MOTION_STATUS"):
            self.trial()

    def test_trial_arguments_and_policy_bounds_are_strict(self):
        for value in (None, 1, "true"):
            with self.subTest(value=value), self.assertRaises(factory.ContractError):
                self.resolve(motion_preset=self.preset, motion_preset_trial=value)
        with self.assertRaises(factory.ContractError):
            self.resolve(motion_preset_trial=True)
        self.preset["phase_scaling"]["PREGRASP_PTP"]["velocity_scaling"] = .11
        with self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_SCALING"):
            self.trial()

    def test_trial_marker_is_bound_to_base_and_preset(self):
        trial = self.trial()
        for key in ("motion_qualification", "motion_preset", "motion_preset_trial"):
            changed = copy.deepcopy(trial)
            changed["binding_digests"][key] = factory.canonical_digest("different")
            with self.subTest(key=key), self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_TRIAL_BINDING"):
                factory.validate_motion_program(changed)

    def test_trajectory_compiler_preserves_trial_identity(self):
        trial = self.trial()
        compiled = compile_execution_motion_program(
            trial, trajectory_variant_id="TWO_STAGE_ALIGN_V2", sampling_seed=1,
            target_yaw_deg=self.job["yaw_deg"],
            object_dimensions_mm=self.validated["object_profile"]["dimensions_mm"],
            approach_sampling_profile=factory.load_json_strict(
                CONFIG / "approach_sampling_profiles/wood-cube-24mm-top-wrist-r001.json"))
        self.assertEqual(compiled["binding_digests"], trial["binding_digests"])
        factory.validate_motion_program(compiled)

    def test_production_rejects_trial_before_external_effects(self):
        trial = self.trial()
        executor, recorder, validator = Mock(), Mock(), Mock()
        result = run_job.run_live(
            {"run_id": "synthetic-preset-trial"}, threading.Event(), lambda _: None,
            resolver=lambda _: (self.validated, trial, {}),
            executor_factory=executor, recorder_factory=recorder, validator_call=validator)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["code"], "MOTION_PRESET_TRIAL_SCOPE", result)
        for effect in (executor, recorder, validator):
            effect.assert_not_called()

    def test_trial_execution_context_survives_stripped_program_markers(self):
        for removed in (("motion_preset_trial",), ("motion_preset_trial", "motion_preset")):
            trial = self.trial()
            for key in removed:
                del trial["binding_digests"][key]
            resolver = Mock(return_value=(self.validated, trial, {}))
            effect = Mock(side_effect=AssertionError("no external effects"))
            with self.subTest(removed=removed), patch.object(run_job, "_prepare_run_dir", effect):
                result = run_job.run_live(
                    {"run_id": "synthetic-stripped-trial"}, threading.Event(), lambda _: None,
                    motion_preset_trial=True, resolver=resolver,
                    executor_factory=effect, recorder_factory=effect,
                    camera_warmup_call=effect, validator_call=effect)
            self.assertEqual(result["code"], "MOTION_PRESET_TRIAL_SCOPE", result)
            resolver.assert_not_called()
            effect.assert_not_called()

    def test_reposition_trial_context_rejects_production_before_binding_or_effects(self):
        resolver = Mock(side_effect=AssertionError("no resolution"))
        effect = Mock(side_effect=AssertionError("no external effects"))
        with patch.object(run_job, "_prepare_run_dir", effect):
            for trial in (True, None, 1, "true"):
                with self.subTest(trial=trial), self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_TRIAL_SCOPE"):
                    run_job.run_object_reposition(
                        {}, {}, threading.Event(), lambda _: None,
                        parent_plan_digest=None, operator_id="synthetic", cell_root=None,
                        resolver=resolver, executor_factory=effect,
                        campaign_authorization=None, data_disposition="PRODUCTION",
                        preapproval_scope=None, motion_preset_trial=trial)
        resolver.assert_not_called()
        effect.assert_not_called()

    def test_native_runner_resolves_trial_without_touching_qualification(self):
        payload = {
            "mode": "plan_only", "run_id": "synthetic-trial", "job": self.job,
            "selected_sheet": str(self.sheet), "yaw0_sheet": str(self.sheet),
            "config_root": str(CONFIG),
            "motion_qualification": str(CONFIG / "motion_qualifications/fr5-place-a-wood-cube-24mm-r001.json"),
            "home_candidate": str(CONFIG / "home_candidates/fr5-lab-a-tcp-r002-home-r001.json"),
            "urdf": str(self.options["urdf"]),
            "expected_robot_system_id": self.qualification["robot_system_id"],
            "motion_preset": {"id": self.preset["motion_preset_id"],
                              "digest": factory.canonical_digest(self.preset)},
        }
        path = Path(payload["motion_qualification"])
        before = path.read_bytes(), path.stat().st_mtime_ns
        checked = run_job._run_payload(payload)
        resolved, program, scene = run_job.resolve_inputs(
            checked, scene_binding_call=lambda *_: {"synthetic": True},
            motion_preset_trial=True)
        self.assertEqual(resolved, self.validated)
        self.assertEqual(program, self.trial())
        self.assertEqual(scene, {"synthetic": True})
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        with self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_QUALIFICATION_REQUIRED"):
            run_job.resolve_inputs(checked, scene_binding_call=lambda *_: {})
        effect = Mock(side_effect=AssertionError("no external effects"))
        with patch.object(run_job, "_prepare_run_dir", effect):
            result = run_job.run_live(
                checked, threading.Event(), lambda _: None,
                executor_factory=effect, recorder_factory=effect,
                camera_warmup_call=effect, validator_call=effect)
        self.assertEqual(result["code"], "MOTION_PRESET_QUALIFICATION_REQUIRED", result)
        effect.assert_not_called()

    def test_cross_workspace_trial_keeps_each_base_binding(self):
        qb = factory.load_json_strict(
            CONFIG / "motion_qualifications/fr5-place-b-wood-cube-24mm-r001.json")
        sheet_b = CONFIG / "workspace_sheets/place-b-yaw0-r001_yaw0_sheet.json"
        a_job = copy.deepcopy(self.job)
        a_job.update(task="pick_place", instruction=factory.task_instruction("pick_place", "24 mm wooden cube"),
                     episode_intent=factory.TASK_CONTRACTS["pick_place"]["episode_intent"])
        b_job = copy.deepcopy(a_job)
        b_job.update(place_id="PLACE_B", cell_calibration_id=qb["cell_calibration_id"],
                     sheet_manifest_digest=factory.canonical_digest(factory.load_json_strict(sheet_b)))
        a = factory.validate_job_spec(a_job, paths={"selected_sheet": self.sheet, "yaw0_sheet": self.sheet}, config_root=CONFIG)
        b = factory.validate_job_spec(b_job, paths={"selected_sheet": sheet_b, "yaw0_sheet": sheet_b}, config_root=CONFIG)
        program = factory.resolve_motion_program(
            a, self.qualification, self.home, **self.options,
            release_validated=b, release_motion_qualification=qb,
            motion_preset=self.preset, motion_preset_trial=True)
        factory.validate_motion_program(program)
        self.assertEqual(program["destination_binding_digests"]["motion_qualification"], factory.canonical_digest(qb))
        self.assertNotEqual(program["binding_digests"]["motion_preset_trial"],
                            program["destination_binding_digests"]["motion_preset_trial"])
        run_job._validate_motion_preset_trial_scope(program, "TEST_ONLY")
        for disposition in ("PRODUCTION", None, "TEST_COLLECTION"):
            with self.subTest(disposition=disposition), self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_TRIAL_SCOPE"):
                run_job._validate_motion_preset_trial_scope(program, disposition)
        changed = copy.deepcopy(program)
        changed["destination_binding_digests"]["motion_preset_trial"] = program["binding_digests"]["motion_preset_trial"]
        with self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_TRIAL_BINDING"):
            factory.validate_motion_program(changed)

    def test_public_offline_cli_resolves_trial_without_promoting_candidate(self):
        arguments = {
            "job": CONFIG / "jobs/center-live-24mm-20260903-r002.job.json",
            "selected-sheet": self.sheet, "yaw0-sheet": self.sheet,
            "config-root": CONFIG,
            "motion-qualification": CONFIG / "motion_qualifications/fr5-place-a-wood-cube-24mm-r001.json",
            "home-candidate": CONFIG / "home_candidates/fr5-lab-a-tcp-r002-home-r001.json",
            "urdf": self.options["urdf"],
            "expected-robot-system-id": self.qualification["robot_system_id"],
            "motion-preset": self.preset["motion_preset_id"],
        }
        command = [sys.executable, str(ROOT / "tools/fr5_data_factory.py"), "resolve-motion",
                   *(part for key, value in arguments.items() for part in (f"--{key}", str(value)))]
        result = subprocess.run(command + ["--trial-motion-preset"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), self.trial())
        normal = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(normal.returncode, 0)
        self.assertIn("MOTION_PRESET_QUALIFICATION_REQUIRED", normal.stdout + normal.stderr)


if __name__ == "__main__":
    unittest.main()
