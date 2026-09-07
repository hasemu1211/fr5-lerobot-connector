"""Software-only policy/qualification replay; synthetic qualification is not hardware evidence."""
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from tools import fr5_data_factory as factory
from tools.data_factory import run_job
from tools.data_factory.motion.pickup_executor import PickupExecutor
from tests.data_factory.test_motion import T, SCENE, snapshot

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "config/data_factory"


class MotionPresetTests(unittest.TestCase):
    def setUp(self):
        self.use_preset("practical-transfer-r001")
        self.home = factory.load_json_strict(CONFIG / "home_candidates/fr5-lab-a-tcp-r002-home-r001.json")
        self.urdf = ROOT / "src/fairino_description/urdf/fairino5_v6.urdf"

    def use_preset(self, identifier):
        self.preset = factory.load_json_strict(CONFIG / f"motion_presets/{identifier}.json")
        self.binding = {"id": self.preset["motion_preset_id"], "digest": factory.canonical_digest(self.preset)}

    def endpoint(self, place):
        suffix = place.lower()
        qualification = factory.load_json_strict(CONFIG / f"motion_qualifications/fr5-place-{suffix}-wood-cube-24mm-r001.json")
        sheet_path = CONFIG / ("test_only_physical/goal2-place1/yaw0_sheet.json" if place == "A" else "workspace_sheets/place-b-yaw0-r001_yaw0_sheet.json")
        sheet = factory.load_json_strict(sheet_path)
        job = factory.load_json_strict(CONFIG / "jobs/center-live-24mm-20260903-r002.job.json")
        job.update(job_id="synthetic-preset", task="pick_place", place_id=f"PLACE_{place}", cell_calibration_id=qualification["cell_calibration_id"],
                   sheet_manifest_digest=factory.canonical_digest(sheet),
                   instruction=factory.task_instruction("pick_place", "24 mm wooden cube"), episode_intent=factory.TASK_CONTRACTS["pick_place"]["episode_intent"])
        validated = factory.validate_job_spec(job, paths={"selected_sheet": sheet_path, "yaw0_sheet": sheet_path}, config_root=CONFIG)
        return validated, qualification, sheet_path

    def resolve(self, validated, qualification, **kwargs):
        scene = factory.load_json_strict(CONFIG / f"planning_scenes/{qualification['planning_scene_profile_id']}.json")
        return factory.resolve_motion_program(validated, qualification, self.home, urdf=self.urdf,
                                             expected_robot_system_id=qualification["robot_system_id"],
                                             planning_scene_profile=scene, **kwargs)

    def test_native_ab_policy_exact_plans_and_preserved_gripper(self):
        a, qa, _ = self.endpoint("A")
        b, qb, _ = self.endpoint("B")
        originals = copy.deepcopy([qa, qb, self.preset])
        legacy = self.resolve(a, qa, release_validated=b, release_motion_qualification=qb)
        candidates = [factory.prepare_motion_preset_qualification(q, self.preset) for q in (qa, qb)]
        self.assertTrue(all(q["qualification_status"] == "UNQUALIFIED" and q["qualified_at"] is None for q in candidates))
        with self.assertRaisesRegex(factory.ContractError, "MOTION_STATUS"):
            self.resolve(a, candidates[0], motion_preset=self.preset)
        # Fixture-only authority: production qualification is exclusively root-owned.
        for candidate, original in zip(candidates, (qa, qb)):
            candidate.update(qualification_status="QUALIFIED", qualified_at=original["qualified_at"])
        program = self.resolve(a, candidates[0], release_validated=b, release_motion_qualification=candidates[1], motion_preset=self.preset)
        self.assertEqual(factory.validate_motion_program(program), program)
        self.assertEqual(program["binding_digests"]["motion_preset"], self.binding["digest"])
        self.assertEqual(program["destination_binding_digests"]["motion_preset"], self.binding["digest"])
        for old, new in zip(legacy["steps"], program["steps"]):
            if new["phase"] in self.preset["phase_scaling"]:
                expected = copy.deepcopy(old)
                expected["limits"].update(self.preset["phase_scaling"][new["phase"]])
                self.assertEqual(new, expected)
            else:
                self.assertEqual(new, old)
        for key in ("planning", "frames", "planning_scene", "gripper_requirements", "execution_timeouts_s"):
            self.assertEqual(program[key], legacy[key])
        transport = T()
        requirements = program["gripper_requirements"]
        transport.snapshot = lambda *_: snapshot(velocity=requirements["velocity_percent"], force=requirements["force_percent"],
                                                 open_velocity=requirements["open_velocity_percent"], open_force=requirements["open_force_percent"])
        executor = PickupExecutor(transport)
        request = {"schema_version": "fr5.pickup_executor.command.v4", "op_id": "preset-plan", "op": "plan",
                   "payload": {"run_id": "synthetic-preset", "motion_program": program, "scene_binding": SCENE}}
        response = executor.process(request)
        self.assertTrue(response["ok"], response)
        plan = response["data"]["plan"]
        self.assertEqual(plan["binding_digests"]["motion_preset"], self.binding["digest"])
        self.assertEqual(plan["motion_program_digest"], factory.canonical_digest(program))
        self.assertEqual(response, executor.process(request))
        self.assert_episode_roundtrip(a, candidates[0], program, response)
        # The same exact plan is persisted by the existing episode evidence owner.
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "synthetic-preset").mkdir()
            payload = {"run_id": "synthetic-preset", "run_root": directory, "config_root": str(CONFIG)}
            trajectory = run_job._trajectory_binding(payload, a, program)
            evidence = run_job._write_preapproval_evidence(payload, a, {
                "plan_digest": response["plan_digest"], "plan_envelope": response["data"],
            }, trajectory)
            stored = factory.load_json_strict(Path(directory) / "synthetic-preset/preapproval_evidence.json")
            self.assertEqual(stored, evidence)
            self.assertEqual(stored["plan_envelope"]["plan"]["binding_digests"]["motion_preset"], self.binding["digest"])
        self.assertEqual([qa, qb, self.preset], originals)
        self.assertEqual(legacy, self.resolve(a, qa, release_validated=b, release_motion_qualification=qb))

    def assert_episode_roundtrip(self, validated, qualification, program, response):
        from tests.data_factory.test_episode_ledger import EpisodeLedgerTest
        from tools.data_factory.collection_seed import trajectory_sampling_binding
        from tools.data_factory.episode_ledger import validate_episode_ledger
        from tools.data_factory.quality.coverage_report import build_and_publish_coverage_report
        from tools.data_factory.quality.episode_report import object_frame_context_attribute
        fixture = EpisodeLedgerTest()
        fixture.setUp()
        try:
            fixture.run_id = response["run_id"]
            fixture.episode_ref.update(transaction_id=f"{fixture.run_id}:episode-000000", resolved_job_digest=validated["resolved_job_digest"])
            refs = fixture._artifacts()
            loaded = fixture._loaded_artifacts(refs)
            job = validated["normalized_job"]
            staging = loaded["staging_manifest"]
            staging["binding_digests"].update({f"{key}_digest": value for key, value in validated["input_digests"].items()})
            loaded["episode"]["episode_ref"]["staging_manifest_digest"] = factory.canonical_digest(staging)
            manifest, intent, runtime = (loaded[key] for key in ("manifest", "intent", "runtime_binding"))
            intent["fixed_contract"]["collection_profile_digest"] = validated["input_digests"]["collection_profile"]
            intent["intent_digest"] = factory.canonical_digest({key: value for key, value in intent.items() if key != "intent_digest"})
            runtime.update({key: job[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")})
            runtime["intent_digest"] = intent["intent_digest"]
            runtime["binding_digest"] = factory.canonical_digest({key: value for key, value in runtime.items() if key != "binding_digest"})
            design = trajectory_sampling_binding(manifest["normalized_seed"], intent["slot"], manifest["slots"])
            payload = {"run_id": fixture.run_id, "run_root": str(fixture.base), "config_root": str(CONFIG),
                       "trajectory_variant_id": "DIRECT", "trajectory_sampling_seed": design.pop("sampling_seed"), "trajectory_sampling_design": design}
            (fixture.base / fixture.run_id).mkdir()
            campaign = {"manifest_digest": manifest["manifest_digest"], "intent_digest": intent["intent_digest"], "slot_id": intent["slot"]["slot_id"],
                        "slot_digest": factory.canonical_digest(intent["slot"]), "runtime_episode_binding_digest": runtime["binding_digest"]}
            preapproval = run_job._write_preapproval_evidence(payload, validated, {"plan_digest": response["plan_digest"], "plan_envelope": response["data"]},
                                                            run_job._trajectory_binding(payload, validated, program), campaign_binding=campaign)
            loaded["plan"] = preapproval
            loaded["technical"]["plan_digest"] = response["plan_digest"]
            loaded["execution"]["plan_digest"] = response["plan_digest"]
            loaded["execution"]["data"]["precommit_safety"] = {**response["data"]["precommit_safety"], "status": "PASS", "post_reset_safe_snapshot_digest": factory.canonical_digest("synthetic-terminal")}
            for key in ("episode", "staging_manifest", "intent", "runtime_binding", "plan", "technical", "execution"):
                refs[key] = fixture._json(f"native-{key}.json", loaded[key])
            ledger = fixture._compile(refs)
            self.assertEqual(validate_episode_ledger(ledger), ledger)
            self.assertEqual(ledger["admission"]["training_status"], "NOT_AUTHORIZED")
            # Synthetic human review fixture only; the policy grants no semantic approval.
            candidate_ref = fixture._candidate(ledger, semantic_status="PASS")
            candidate = factory.load_json_strict(Path(candidate_ref["artifact_path"]))
            candidate["checklist_id"] = factory.task_review_checklist_id(job["task"])
            accepted = {"episode_id": fixture.run_id}
            for name, value in (("job_spec", job), ("preapproval_evidence", preapproval), ("technical_validator", loaded["technical"]), ("candidate_admission", candidate)):
                ref = fixture._json(f"quality-{name}.json", value)
                accepted.update({f"{name}_path": ref["artifact_path"], f"{name}_digest": ref["artifact_digest"]})
            attribute = object_frame_context_attribute(accepted_episode=accepted, resolved_job=validated, motion_qualification=qualification)
            self.assertEqual(attribute["status"], "AVAILABLE")
            condition = {key: job[key] for key in ("task", "robot_system_id", "place_id", "cell_calibration_id", "yaw_deg", "x_mm", "y_mm", "object_profile_id", "grasp_profile_id")}
            condition.update(task_schema_version=job["schema_version"], cell_calibration_digest=validated["input_digests"]["cell_calibration"],
                             motion_recipe_digest=factory.canonical_digest(qualification), collection_profile_digest=validated["input_digests"]["collection_profile"])
            path = build_and_publish_coverage_report(collection_profile_id=job["collection_profile_id"], domain=[condition], stored_episodes=[accepted], root=fixture.base / "coverage")
            report = factory.load_json_strict(path)
            self.assertEqual(report["cells"][0]["counts"]["human_semantic_pass"], 1)
            self.assertEqual(report["cells"][0]["counts"]["human_training_approved"], 0)
            malformed = copy.deepcopy(preapproval)
            malformed["plan_envelope"]["plan"]["binding_digests"]["motion_preset"] = "invalid"
            malformed["plan_digest"] = factory.canonical_digest(malformed["plan_envelope"]["plan"])
            for key in ("precommit_safety", "precommit_evidence"):
                malformed["plan_envelope"][key]["approved_plan_digest"] = malformed["plan_digest"]
            malformed["plan_envelope_digest"] = factory.canonical_digest(malformed["plan_envelope"])
            ref = fixture._json("malformed-preset-evidence.json", malformed)
            bad_source = {**accepted, "preapproval_evidence_path": ref["artifact_path"], "preapproval_evidence_digest": ref["artifact_digest"]}
            with self.assertRaisesRegex(factory.ContractError, "COVERAGE_PLAN_EVIDENCE"):
                build_and_publish_coverage_report(collection_profile_id=job["collection_profile_id"], domain=[condition], stored_episodes=[bad_source], root=fixture.base / "rejected-coverage")
            self.assertFalse((fixture.base / "rejected-coverage").exists())
        finally:
            fixture.doCleanups()

    def test_runner_resolves_same_policy_for_both_endpoints(self):
        a, qa, sheet_a = self.endpoint("A")
        b, qb, sheet_b = self.endpoint("B")
        prepared = [factory.prepare_motion_preset_qualification(q, self.preset) for q in (qa, qb)]
        for value, base in zip(prepared, (qa, qb)):
            value.update(qualification_status="QUALIFIED", qualified_at=base["qualified_at"])
        payload = {"mode": "plan_only", "run_id": "synthetic-preset", "config_root": str(CONFIG),
                   "job": a["normalized_job"], "selected_sheet": str(sheet_a), "yaw0_sheet": str(sheet_a),
                   "motion_qualification": prepared[0], "home_candidate": self.home, "urdf": str(self.urdf),
                   "expected_robot_system_id": qa["robot_system_id"], "motion_preset": self.binding,
                   "destination": {"job": b["normalized_job"], "selected_sheet": str(sheet_b), "yaw0_sheet": str(sheet_b), "motion_qualification": prepared[1]}}
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("a.json", "b.json", "home.json")]
            for path, value in zip(paths, (*prepared, self.home)):
                path.write_text(json.dumps(value))
            payload.update(motion_qualification=str(paths[0]), home_candidate=str(paths[2]))
            payload["destination"]["motion_qualification"] = str(paths[1])
            checked = run_job._run_payload(payload)
            _, program, _ = run_job.resolve_inputs(checked, scene_binding_call=lambda *_: SCENE)
            payload["motion_preset"] = {**self.binding, "digest": "sha256:" + "0" * 64}
            with self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_BINDING"):
                run_job.resolve_inputs(payload, scene_binding_call=lambda *_: SCENE)
        self.assertEqual(program, self.resolve(a, prepared[0], release_validated=b, release_motion_qualification=prepared[1], motion_preset=self.preset))

    def test_stale_missing_malformed_and_inherited_qualification_reject(self):
        a, qa, _ = self.endpoint("A")
        aliased = copy.deepcopy(qa)
        aliased["phase_limits"]["PREGRASP_PTP"] = aliased["phase_limits"]["FINAL_APPROACH_LIN"]
        prepared = factory.prepare_motion_preset_qualification(aliased, self.preset)
        self.assertEqual(prepared["phase_limits"]["PREGRASP_PTP"]["velocity_scaling"], .1)
        self.assertEqual(prepared["phase_limits"]["FINAL_APPROACH_LIN"]["velocity_scaling"], .03)
        with self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_QUALIFICATION_REQUIRED"):
            self.resolve(a, qa, motion_preset=self.preset)
        qualified = factory.prepare_motion_preset_qualification(qa, self.preset)
        qualified.update(qualification_status="QUALIFIED", qualified_at=qa["qualified_at"])
        for mutate in (lambda q: q["motion_preset"].update(digest="sha256:" + "0" * 64),
                       lambda q: q["phase_limits"]["PREGRASP_PTP"].update(acceleration_scaling=.03)):
            changed = copy.deepcopy(qualified)
            mutate(changed)
            with self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_BINDING"):
                self.resolve(a, changed, motion_preset=self.preset)
        for mutate in (lambda p: p["phase_scaling"].pop("LOWER_LIN"),
                       lambda p: p["phase_scaling"]["PREGRASP_PTP"].update(velocity_scaling=True),
                       lambda p: p["phase_scaling"]["PREGRASP_PTP"].update(acceleration_scaling=.11),
                       lambda p: p["phase_scaling"].update(GRIPPER_OPEN={})):
            changed = copy.deepcopy(self.preset)
            mutate(changed)
            with self.assertRaises(factory.ContractError):
                factory.validate_motion_preset(changed)
        with self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_BINDING"):
            factory.load_motion_preset(CONFIG, {**self.binding, "digest": "sha256:" + "0" * 64})

    def test_cli_preparation_is_unqualified_and_canonical(self):
        _, qa, sheet = self.endpoint("A")
        process = subprocess.run([sys.executable, str(ROOT / "tools/fr5_data_factory.py"), "prepare-motion-preset",
                                  "--config-root", str(CONFIG), "--motion-preset", self.binding["id"],
                                  "--motion-qualification", str(CONFIG / "motion_qualifications/fr5-place-a-wood-cube-24mm-r001.json")],
                                 text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(process.stdout), factory.prepare_motion_preset_qualification(qa, self.preset))
        with tempfile.TemporaryDirectory() as directory:
            qualified = json.loads(process.stdout)
            qualified.update(qualification_status="QUALIFIED", qualified_at=qa["qualified_at"])
            path = Path(directory) / "synthetic-qualified.json"
            path.write_text(json.dumps(qualified))
            job = CONFIG / "jobs/center-live-24mm-20260903-r002.job.json"
            arguments = {"job": job, "selected-sheet": sheet, "yaw0-sheet": sheet, "config-root": CONFIG,
                         "motion-qualification": path, "home-candidate": CONFIG / "home_candidates/fr5-lab-a-tcp-r002-home-r001.json",
                         "urdf": self.urdf, "expected-robot-system-id": qa["robot_system_id"], "motion-preset": self.binding["id"]}
            resolved = subprocess.run([sys.executable, str(ROOT / "tools/fr5_data_factory.py"), "resolve-motion",
                                       *(part for key, value in arguments.items() for part in (f"--{key}", str(value)))], text=True, capture_output=True, check=True)
            validated = factory.validate_job_spec(factory.load_json_strict(job), paths={"selected_sheet": sheet, "yaw0_sheet": sheet}, config_root=CONFIG)
            self.assertEqual(json.loads(resolved.stdout), self.resolve(validated, qualified, motion_preset=self.preset))

    def test_home_recovery_revalidates_before_transport_and_preserves_gates(self):
        from tests.data_factory.test_home_recovery import FakeTransport, snapshot as home_snapshot
        from tools.data_factory.motion.home_recovery import recover_home
        _, qa, _ = self.endpoint("A")
        qualified = factory.prepare_motion_preset_qualification(qa, self.preset)
        qualified.update(qualification_status="QUALIFIED", qualified_at=qa["qualified_at"])
        target = qualified["qualified_safe_joint_positions_rad"]
        start = [value + .2 for value in target]
        transport = FakeTransport([home_snapshot(start), home_snapshot(start), home_snapshot(target)])
        result = recover_home(transport, motion_qualification=qualified, motion_preset=self.preset, sleep_call=lambda _: None)
        self.assertEqual(result["status"], "HOME")
        self.assertEqual(result["motion_qualification_digest"], factory.canonical_digest(qualified))
        self.assertEqual(transport.started, ["SAFE_POSE_PTP"])
        for policy in (None, {**self.preset, "purpose": "changed"}):
            untouched = FakeTransport([])
            with self.assertRaises(factory.ContractError):
                recover_home(untouched, motion_qualification=qualified, motion_preset=policy)
            self.assertEqual(untouched.started, [])

    def test_real_application_compiles_bound_ab_preset_and_rejects_source_change(self):
        from tools.data_factory.operator.composition import build_physical_operator_application
        from tools.data_factory.operator.workflow.intents import INTENT_SCHEMA
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(CONFIG, root / "config/data_factory")
            urdf = root / self.urdf.relative_to(ROOT)
            urdf.parent.mkdir(parents=True)
            shutil.copy2(self.urdf, urdf)
            prepared_paths = []
            for place in ("A", "B"):
                _, base, _ = self.endpoint(place)
                prepared = factory.prepare_motion_preset_qualification(base, self.preset)
                prepared.update(qualification_status="QUALIFIED", qualified_at=base["qualified_at"])
                path = root / "config/data_factory/motion_qualifications" / f"synthetic-preset-{place}.json"
                path.write_text(json.dumps(prepared))
                prepared_paths.append(path)
            environment = {"schema_version": "data_factory.operator_environment.v1", "state": "READY", "observed_at": "2026-09-07T00:00:00Z",
                           "components": {name: {"state": "READY", "owner": "synthetic", "reason": "ATTACHED"} for name in ("robot", "controller", "gripper", "camera")}}
            forbidden = mock.Mock(side_effect=AssertionError("no live effects"))
            application, _ = build_physical_operator_application(
                repository_root=root, session_id="synthetic-preset-application", operator_label="local-operator",
                environment_call=lambda: environment, prepare_environment_call=lambda: environment,
                initial_environment=environment, gripper_retune_path=None,
                job_path="config/data_factory/jobs/center-live-24mm-20260903-r002.job.json",
                camera_environment_call=lambda *_: environment,
                discovery_call=lambda: ["usb-Generic_USB2.0_PC_CAMERA-video-index0", "usb-Generic_USB2.0_PC_CAMERA_2-video-index0"],
                activation_call=lambda: True, run_live_call=forbidden, snapshot_call=forbidden,
                initial_motion_preset=self.binding["id"],
                gripper_readback_call=lambda: {"active": True, "position_valid": True, "gripper_index": 1, "reference_position_m": .021, "feedback_position_m": .021,
                                               "sample_age_s": 0., "max_age_s": .1, "source": "CONTROLLER_STATE"})
            try:
                def consume(op, payload, identifier):
                    view = application.bridge_core.snapshot()
                    return application.bridge_core.consume({"schema_version": INTENT_SCHEMA, "intent_id": identifier, "session_id": view["session_id"],
                                                            "view_revision": view["revision"], "view_digest": view["view_digest"], "op": op, "payload": payload})
                consume("update_camera_bindings", {"bindings": {"usb-Generic_USB2.0_PC_CAMERA-video-index0": "UP", "usb-Generic_USB2.0_PC_CAMERA_2-video-index0": "WRIST"}}, "preset-cameras")
                draft_id = application.draft["draft_id"]
                consume("update_draft", {"draft_id": draft_id, "selection": {"task": "pick_place"}}, "preset-task")
                consume("update_draft", {"draft_id": draft_id, "requested_count": 1}, "preset-count")
                view = application.bridge_core.snapshot()["projection"]
                self.assertEqual(next(item for item in view["motion_presets"] if item["id"] == self.binding["id"])["status"], "QUALIFIED")
                self.assertIn("compile_draft", view["available_ops"], view["draft"])
                source_bytes = prepared_paths[0].read_bytes()
                changed = json.loads(source_bytes)
                changed["qualified_at"] = "2099-01-01T00:00:00Z"
                prepared_paths[0].write_text(json.dumps(changed))
                with self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_BINDING"):
                    consume("compile_draft", {"draft_id": draft_id, "data_disposition": "TEST_ONLY"}, "preset-stale-source")
                self.assertIsNone(application._campaign)
                prepared_paths[0].write_bytes(source_bytes)
                consume("compile_draft", {"draft_id": draft_id, "data_disposition": "TEST_ONLY"}, "preset-compile")
                self.assertEqual(application.bridge_core.snapshot()["projection"]["workflow_state"], "REVIEW_CAMPAIGN")
                expected = {factory.canonical_digest(factory.load_json_strict(path)) for path in prepared_paths}
                fixed = application._campaign.campaign_operator.hypothesis["fixed_contract"]
                self.assertEqual({endpoint["motion_recipe_digest"] for endpoint in fixed["endpoint_bindings"]}, expected)
                prepared_paths[0].write_text(json.dumps(changed))
                owner = application._campaign.campaign_operator
                with self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_BINDING"):
                    owner.physical_start_binding_call("synthetic-start", owner.manifest["slots"][0], threading.Event())
                forbidden.assert_not_called()
            finally:
                application.close()

    def test_native_candidate_trial_compiles_runs_and_keeps_recovery_qualified(self):
        self.use_preset("demonstration-rhythm-r001")
        self.assertEqual(set(self.preset["phase_scaling"]), {phase for phase in factory.MOTION_PHASES if not phase.startswith("GRIPPER")})
        self.assertEqual(len(self.preset["phase_scaling"]), 8)
        self.assertTrue(all(value == {"velocity_scaling": .1, "acceleration_scaling": .1} for value in self.preset["phase_scaling"].values()))
        from tools.data_factory.operator.composition import build_physical_operator_application
        from tests.data_factory.operator.test_composition import envelope, pose_snapshot
        from tools.data_factory.motion import home_recovery
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(CONFIG, root / "config/data_factory")
            urdf = root / self.urdf.relative_to(ROOT)
            urdf.parent.mkdir(parents=True)
            shutil.copy2(self.urdf, urdf)
            # This test exercises a candidate endpoint, even after B is registered.
            (root / "config/data_factory/motion_qualifications/fr5-place-b-wood-cube-24mm-r001-demonstration-rhythm-r001.json").unlink()
            sources = {path: path.read_bytes() for path in (root / "config/data_factory/motion_qualifications").glob("*.json")}
            _, qa, _ = self.endpoint("A")
            opened = {"active": True, "position_valid": True, "gripper_index": 1,
                      "reference_position_m": .021, "feedback_position_m": .021,
                      "sample_age_s": 0., "max_age_s": .1, "source": "CONTROLLER_STATE"}
            environment = {"schema_version": "data_factory.operator_environment.v1", "state": "READY", "observed_at": "2026-09-07T00:00:00Z",
                           "components": {name: {"state": "READY", "owner": "synthetic", "reason": "ATTACHED"} for name in ("robot", "controller", "gripper", "camera")}}
            observed = []
            forbidden = mock.Mock(side_effect=AssertionError("no device or recorder callback"))
            stop = mock.Mock(side_effect=factory.ContractError("SYNTHETIC_EXECUTOR_BOUNDARY"))

            def live(payload, cancel, publish, **kwargs):
                self.assertIs(kwargs["motion_preset_trial"], True)
                self.assertIs(kwargs["candidate_writer_enabled"], False)
                resolver = kwargs["resolver"]

                def resolve(value):
                    result = resolver(value)
                    observed.append((copy.deepcopy(value), copy.deepcopy(result), kwargs))
                    return result

                kwargs.update(resolver=resolve, executor_factory=stop, recorder_factory=forbidden,
                              validator_call=forbidden, camera_warmup_call=lambda *_: {})
                return run_job.run_live(payload, cancel, publish, **kwargs)

            live_call = mock.Mock(side_effect=live)
            application, _ = build_physical_operator_application(
                repository_root=root, session_id="native-preset-trial", operator_label="local-operator",
                environment_call=lambda: environment, prepare_environment_call=lambda: environment,
                initial_environment=environment, gripper_retune_path=None,
                job_path="config/data_factory/jobs/center-live-24mm-20260903-r002.job.json",
                camera_environment_call=lambda *_: environment,
                discovery_call=lambda: ["usb-Generic_USB2.0_PC_CAMERA-video-index0", "usb-Generic_USB2.0_PC_CAMERA_2-video-index0"],
                activation_call=lambda: True, run_live_call=live_call,
                snapshot_call=lambda: pose_snapshot(qa["qualified_safe_joint_positions_rad"], age=.01),
                gripper_readback_call=lambda: opened)
            self.addCleanup(application.close)

            def request(op, payload, identifier):
                return envelope(application.bridge_core.snapshot(), op, payload, identifier)

            def consume(op, payload, identifier):
                return application.bridge_core.consume(request(op, payload, identifier))

            consume("update_camera_bindings", {"bindings": {"usb-Generic_USB2.0_PC_CAMERA-video-index0": "UP", "usb-Generic_USB2.0_PC_CAMERA_2-video-index0": "WRIST"}}, "trial-cameras")
            draft_id = application.draft["draft_id"]
            consume("update_draft", {"draft_id": draft_id, "motion_preset": self.binding}, "trial-preset-choice")
            self.assertEqual(application.projection()["draft"]["motion_preset"], self.binding)
            consume("update_draft", {"draft_id": draft_id, "selection": {"task": "pick_place"}}, "trial-task")
            consume("update_draft", {"draft_id": draft_id, "requested_count": 2}, "trial-count")
            self.assertEqual(next(item for item in application.projection()["motion_presets"] if item["id"] == self.binding["id"])["status"], "TRIAL_AVAILABLE")
            # HOME is the actual native recovery consumer, stopped at its transport seam.
            with mock.patch.object(home_recovery, "recover_home_live", side_effect=factory.ContractError("SYNTHETIC_HOME_BOUNDARY")) as home:
                with self.assertRaisesRegex(factory.ContractError, "SYNTHETIC_HOME_BOUNDARY"):
                    application.home_recovery_call()
                self.assertEqual(home.call_args.kwargs, {"motion_qualification": qa})
            stale = request("compile_draft", {"draft_id": draft_id, "data_disposition": "TEST_ONLY"}, "trial-stale")
            consume("update_draft", {"draft_id": draft_id, "normalized_seed": 17}, "trial-edit")
            with self.assertRaises(factory.ContractError):
                application.bridge_core.consume(stale)
            self.assertIsNone(application._campaign)
            compile_request = request("compile_draft", {"draft_id": draft_id, "data_disposition": "TEST_ONLY"}, "trial-compile")
            compiled = application.bridge_core.consume(compile_request)["result"]
            campaign = application._campaign
            with self.assertRaises(factory.ContractError):
                application.bridge_core.consume(compile_request)
            self.assertIs(application._campaign, campaign)
            owner = campaign.campaign_operator
            self.assertEqual(len(owner.manifest["slots"]), 2)
            preset_path = root / "config/data_factory/motion_presets" / f"{self.binding['id']}.json"
            original_preset = preset_path.read_bytes()
            changed = json.loads(original_preset)
            changed["purpose"] += " changed"
            preset_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_BINDING"):
                owner.physical_start_binding_call("stale-trial-start", owner.manifest["slots"][0], threading.Event())
            live_call.assert_not_called()
            preset_path.write_bytes(original_preset)
            authorize = request("authorize_campaign", {"draft_id": application.projection()["draft"]["draft_id"], "manifest_digest": compiled["manifest_digest"],
                                "envelope_digest": compiled["envelope_digest"], "data_disposition": "TEST_ONLY"}, "trial-authorize")
            with mock.patch.object(home_recovery, "transition_to_start_live", side_effect=lambda **kw: {
                "status": "ALREADY_AT_START", "robot_start_pose_id": kw["robot_start_pose_qualification"]["robot_start_pose_id"]}) as transition:
                application.bridge_core.consume(authorize)
                result = campaign.wait_for_episode(5.)
                self.assertEqual(result["code"], "SYNTHETIC_EXECUTOR_BOUNDARY", result)
                transition.assert_not_called()  # Already at the qualified HOME start.
            self.assertEqual(len(observed), 1)
            payload, (validated, program, _scene), kwargs = observed[0]
            self.assertEqual(program["binding_digests"]["motion_preset"], self.binding["digest"])
            self.assertIn("motion_preset_trial", program["destination_binding_digests"])
            legacy_payload = copy.deepcopy(payload)
            legacy_payload.pop("motion_preset")
            _, legacy_program, _ = run_job.resolve_inputs(legacy_payload, scene_binding_call=lambda *_: SCENE)
            for key in ("frames", "planning", "planning_scene", "gripper_requirements", "execution_timeouts_s"):
                self.assertEqual(program[key], legacy_program[key])
            for old, new in zip(legacy_program["steps"], program["steps"]):
                expected = copy.deepcopy(old)
                if old["phase"] in self.preset["phase_scaling"]:
                    expected["limits"].update(self.preset["phase_scaling"][old["phase"]])
                self.assertEqual(new, expected)
            release = next(step for step in program["steps"] if step["phase"] == "GRIPPER_OPEN")
            self.assertEqual([release[key] for key in ("release_position_m", "release_hold_s", "gripper_position_m")], [.0126, .5, .021])
            for step in program["steps"]:
                if step["phase"] in self.preset["phase_scaling"]:
                    self.assertEqual({key: step["limits"][key] for key in ("velocity_scaling", "acceleration_scaling")}, self.preset["phase_scaling"][step["phase"]])
            # Reuse the native continuation payload builder and bound resolver.
            source = kwargs["object_reposition_source_payload"]
            self.assertIsNotNone(source)
            continuation = run_job._object_reposition_payload(payload, kwargs["object_reposition_binding"], source_payload=source)
            _, reposition_program, _ = kwargs["object_reposition_resolver"](continuation, scene_binding_call=lambda *_: SCENE)
            self.assertEqual(reposition_program["binding_digests"]["motion_preset"], self.binding["digest"])
            self.assertIn("motion_preset_trial", reposition_program["binding_digests"])
            for step in reposition_program["steps"]:
                if step["phase"] in self.preset["phase_scaling"]:
                    self.assertEqual({key: step["limits"][key] for key in ("velocity_scaling", "acceleration_scaling")}, self.preset["phase_scaling"][step["phase"]])
            with self.assertRaises(factory.ContractError):
                application.bridge_core.consume(authorize)
            self.assertEqual(live_call.call_count, 1)
            stop.assert_called_once()
            forbidden.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in sources}, sources)

    def test_native_production_keeps_candidate_blocked_before_effects(self):
        # This policy has no engineering registration at either endpoint.
        self.use_preset("practical-transfer-r001")
        from functools import partial
        from tests.data_factory.operator.test_object_position import ObjectPositionContinuityTests
        from tools.data_factory.operator.composition import build_physical_operator_application, build_physical_operator_console
        fixture = ObjectPositionContinuityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.application(builder=partial(build_physical_operator_application, initial_motion_preset=self.binding["id"]))
        scene = fixture.scene.snapshot()
        view = app.projection()
        self.assertEqual(next(item for item in view["motion_presets"] if item["id"] == self.binding["id"])["status"], "QUALIFICATION_REQUIRED")
        self.assertNotIn("compile_draft", view["available_ops"])
        request = fixture.request(app, "compile_draft", {"draft_id": app.draft["draft_id"], "data_disposition": "PRODUCTION"}, "production-candidate")
        for _ in range(2):
            with self.assertRaises(factory.ContractError):
                app.bridge_core.consume(request)
        self.assertIsNone(app._campaign)
        with self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_TRIAL_SCOPE"):
            build_physical_operator_console(repository_root=fixture.root, session_id="forbidden-trial", run_id="forbidden-trial",
                                            operator_label="fixture", motion_preset=self.binding, motion_preset_trial=True,
                                            data_disposition="PRODUCTION", run_live_call=fixture.forbidden)
        fixture.forbidden.assert_not_called()
        self.assertEqual(fixture.scene.snapshot(), scene)
        self.assertEqual(fixture.episode.read_bytes(), fixture.original)
        self.assertFalse((fixture.root / "datasets/fr5_episodes/uncreated-dataset").exists())

    def test_registered_a_production_compiles_but_unqualified_b_remains_blocked(self):
        self.use_preset("demonstration-rhythm-r001")
        from tools.data_factory.operator.catalog import motion_geometry_digest, selected_motion_preset
        from tests.data_factory.operator.test_object_position import ObjectPositionContinuityTests
        a, base, _ = self.endpoint("A")
        qualified = factory.load_json_strict(CONFIG / "motion_qualifications/fr5-place-a-wood-cube-24mm-r001-demonstration-rhythm-r001.json")
        expected = factory.prepare_motion_preset_qualification(base, self.preset)
        expected.update(qualification_status="QUALIFIED", qualified_at="2026-09-07T06:19:08Z")
        self.assertEqual(qualified, expected)
        self.assertNotEqual(qualified["qualified_at"], base["qualified_at"])
        self.assertEqual(motion_geometry_digest(qualified), motion_geometry_digest(base))
        b, qb, _ = self.endpoint("B")
        with self.assertRaisesRegex(factory.ContractError, "MOTION_PRESET_QUALIFICATION_REQUIRED"):
            self.resolve(a, qualified, release_validated=b, release_motion_qualification=qb, motion_preset=self.preset)

        fixture = ObjectPositionContinuityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        # Keep the missing-endpoint rejection independent of production registry growth.
        (fixture.root / "config/data_factory/motion_qualifications/fr5-place-b-wood-cube-24mm-r001-demonstration-rhythm-r001.json").unlink()
        app = fixture.application()

        def consume(op, payload, identifier):
            return app.bridge_core.consume(fixture.request(app, op, payload, identifier))

        consume("update_camera_bindings", {"bindings": {
            "usb-Generic_USB2.0_PC_CAMERA-video-index0": "UP", "usb-Generic_USB2.0_PC_CAMERA_2-video-index0": "WRIST",
        }}, "registered-cameras")
        draft_id = app.draft["draft_id"]
        consume("update_draft", {"draft_id": draft_id, "motion_preset": self.binding}, "registered-choice")
        consume("update_draft", {"draft_id": draft_id, "requested_count": 6}, "registered-count")
        preset = selected_motion_preset(app.catalog, self.binding)
        self.assertEqual(set(preset["qualifications"]), {base["motion_qualification_id"]})
        self.assertEqual(preset["qualifications"][base["motion_qualification_id"]]["digest"], factory.canonical_digest(qualified))
        self.assertEqual(next(item for item in app.projection()["motion_presets"] if item["id"] == self.binding["id"])["status"], "QUALIFIED")

        consume("update_draft", {"draft_id": draft_id, "selection": {"task": "pick_place"}}, "unqualified-b")
        self.assertIn("PLACE_B", {item["workspace_id"] for item in app._workspace_cycle()})
        self.assertEqual(next(item for item in app.projection()["motion_presets"] if item["id"] == self.binding["id"])["status"], "QUALIFICATION_REQUIRED")
        self.assertNotIn("compile_draft", app.projection()["available_ops"])
        scene = fixture.scene.snapshot()
        with self.assertRaises(factory.ContractError):
            consume("compile_draft", {"draft_id": draft_id, "data_disposition": "PRODUCTION"}, "blocked-b-compile")
        self.assertIsNone(app._campaign)
        self.assertEqual(fixture.scene.snapshot(), scene)
        fixture.forbidden.assert_not_called()

        consume("update_draft", {"draft_id": draft_id, "selection": {"task": "pickup_e2e"}}, "registered-a")
        resolved = []
        native_resolver = run_job.resolve_inputs

        def observe(payload, **kwargs):
            self.assertIs(kwargs["motion_preset_trial"], False)
            result = native_resolver(payload, **kwargs)
            resolved.append(result[1])
            return result

        with mock.patch.object(run_job, "resolve_inputs", side_effect=observe):
            consume("compile_draft", {"draft_id": draft_id, "data_disposition": "PRODUCTION"}, "registered-compile")
        view = app.projection()
        self.assertEqual(view["workflow_state"], "REVIEW_CAMPAIGN")
        self.assertIsNone(view["campaign_authorization"])
        self.assertEqual(len(app._campaign.campaign_operator.manifest["slots"]), 6)
        self.assertTrue(resolved)
        for program in resolved:
            self.assertEqual(program["binding_digests"]["motion_qualification"], factory.canonical_digest(qualified))
            self.assertEqual(program["binding_digests"]["motion_preset"], self.binding["digest"])
            self.assertNotIn("motion_preset_trial", program["binding_digests"])
        fixture.forbidden.assert_not_called()
        self.assertEqual(fixture.episode.read_bytes(), fixture.original)
        self.assertFalse((fixture.root / "datasets/fr5_episodes/uncreated-dataset").exists())

    def test_registered_ab_production_uses_exact_policy_without_trial_or_effects(self):
        self.use_preset("demonstration-rhythm-r001")
        from tests.data_factory.operator.test_object_position import ObjectPositionContinuityTests
        a, qa, _ = self.endpoint("A")
        b, qb, _ = self.endpoint("B")
        qualified = [factory.load_json_strict(CONFIG / f"motion_qualifications/fr5-place-{place}-wood-cube-24mm-r001-demonstration-rhythm-r001.json") for place in ("a", "b")]
        expected = factory.prepare_motion_preset_qualification(qb, self.preset)
        expected.update(qualification_status="QUALIFIED", qualified_at="2026-09-07T08:12:15Z")
        self.assertEqual(qualified[1], expected)
        for source, destination, source_q, destination_q in ((a, b, *qualified), (b, a, *reversed(qualified))):
            program = self.resolve(source, source_q, release_validated=destination, release_motion_qualification=destination_q, motion_preset=self.preset)
            self.assertEqual(program["binding_digests"]["motion_preset"], self.binding["digest"])
            self.assertEqual(program["destination_binding_digests"]["motion_preset"], self.binding["digest"])
            self.assertNotIn("motion_preset_trial", program["binding_digests"])
        fixture = ObjectPositionContinuityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.application()
        for index, (op, payload) in enumerate((
            ("update_camera_bindings", {"bindings": {"usb-Generic_USB2.0_PC_CAMERA-video-index0": "UP", "usb-Generic_USB2.0_PC_CAMERA_2-video-index0": "WRIST"}}),
            ("update_draft", {"draft_id": app.draft["draft_id"], "motion_preset": self.binding}),
            ("update_draft", {"draft_id": app.draft["draft_id"], "selection": {"task": "pick_place"}}),
            ("update_draft", {"draft_id": app.draft["draft_id"], "requested_count": 2}),
            ("compile_draft", {"draft_id": app.draft["draft_id"], "data_disposition": "PRODUCTION"}),
        )):
            app.bridge_core.consume(fixture.request(app, op, payload, f"registered-ab-{index}"))
        view = app.projection()
        self.assertEqual(view["workflow_state"], "REVIEW_CAMPAIGN")
        self.assertIsNone(view["campaign_authorization"])
        self.assertEqual(len(app._campaign.campaign_operator.manifest["slots"]), 2)
        fixture.forbidden.assert_not_called()
        self.assertEqual(fixture.episode.read_bytes(), fixture.original)
        self.assertFalse((fixture.root / "datasets/fr5_episodes/uncreated-dataset").exists())

    def test_trial_distinct_start_uses_native_base_qualified_transition(self):
        self.use_preset("demonstration-rhythm-r001")
        from tests.data_factory.operator.test_composition import OperatorConsoleTests, envelope, pose_snapshot
        from tests.data_factory.test_home_recovery import FakeTransport, snapshot as home_snapshot
        from tools.data_factory.motion import home_recovery
        from tools.data_factory.operator.composition import build_physical_operator_console
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            OperatorConsoleTests.portable_repository(root)
            from tools.data_factory.operator.workflow.campaign import _home_start_pose
            _, motion, _ = self.endpoint("A")
            qualification = _home_start_pose(motion, self.home, motion["robot_system_id"], source="QUALIFICATION_ARTIFACT")
            home = motion["qualified_safe_joint_positions_rad"]
            target = [value + .05 for value in home]
            qualification.update(robot_start_pose_id="synthetic-trial-start", target_rad=dict(zip(qualification["joint_order"], target)))
            qualification["qualification_digest"] = factory.canonical_digest({key: value for key, value in qualification.items() if key != "qualification_digest"})
            snapshots = iter([pose_snapshot(home, age=.01), pose_snapshot(target, age=.01)])
            transport = FakeTransport([home_snapshot(home), home_snapshot(home), home_snapshot(target)])
            transport.precommit_joint_transition = mock.Mock(return_value={"evidence_digest": factory.canonical_digest("synthetic-start")})
            transport.start_phase = mock.Mock(side_effect=lambda step, **_: transport.started.append(step["phase"]))

            def native_transition(**kwargs):
                self.assertEqual(kwargs["motion_qualification"], motion)
                self.assertNotIn("motion_preset", kwargs)
                return home_recovery.transition_to_start(transport, **kwargs, sleep_call=lambda _: None)

            forbidden = mock.Mock(side_effect=AssertionError("no physical callback"))
            console, _ = build_physical_operator_console(
                repository_root=root, session_id="trial-start", run_id="trial-start-run", operator_label="fixture",
                job_path="config/data_factory/jobs/center-live-24mm-20260903-r002.job.json",
                motion_qualification_path="config/data_factory/motion_qualifications/fr5-place-a-wood-cube-24mm-r001.json",
                home_candidate_path="config/data_factory/home_candidates/fr5-lab-a-tcp-r002-home-r001.json",
                motion_preset=self.binding, motion_preset_trial=True, gripper_retune_path=None,
                discovery_call=lambda: ["usb-Goal2_Camera-video-index0"], activation_call=forbidden,
                snapshot_call=lambda: next(snapshots), run_live_call=forbidden,
                selected_start_pose_qualifications=[qualification], environment_prepared=True)
            try:
                view = console.bridge_core.snapshot()
                console.bridge_core.consume(envelope(view, "compile_draft", {"draft_id": view["projection"]["draft"]["draft_id"],
                                             "data_disposition": "TEST_ONLY"}, "trial-start-compile"))
                with mock.patch.object(home_recovery, "transition_to_start_live", side_effect=native_transition) as transition:
                    binding = console.campaign_operator.physical_start_binding_call(
                        "trial-start-run", console.campaign_operator.manifest["slots"][0], threading.Event())
                transition.assert_called_once()
                self.assertEqual(binding["current_rad"], target)
                self.assertEqual(transport.started, ["SAFE_POSE_PTP"])
                self.assertEqual(transport.start_phase.call_args.args[0]["limits"], motion["phase_limits"]["SAFE_POSE_PTP"])
                forbidden.assert_not_called()
            finally:
                console.close()
