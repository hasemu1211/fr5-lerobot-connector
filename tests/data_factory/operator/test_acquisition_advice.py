"""Stored native evidence through the actual Collection composition, without devices."""
import copy
import functools
import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest import mock

from tests.data_factory import test_collection_recommendation as evidence_fixture
from tests.data_factory.operator import test_object_position as position_fixture
from tools.data_factory.campaign_authoring import compile_collection_campaign
from tools.data_factory.operator.catalog import selected_motion_preset
from tools.data_factory.operator.composition import build_physical_operator_application
from tools.data_factory.operator.web.bridge import LoopbackBridge
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


class AcquisitionAdviceTests(unittest.TestCase):
    def setUp(self):
        self.native = position_fixture.ObjectPositionContinuityTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.root = self.native.root
        self.sequence = 0
        seed_app = self.application("evidence-seed")
        self.send(seed_app, "compile_draft", {"draft_id": seed_app.draft["draft_id"], "data_disposition": "PRODUCTION"})
        self.fixture, self.runs = self.store_evidence(seed_app)
        self.source = {"run_directories": self.runs, "scene_state_path": str(self.native.cells / self.native.robot / "scene_state.json")}
        self.app = self.application("acquisition-consumer")

    def store_evidence(self, seed_app, name="evidence"):
        owner = seed_app._campaign.campaign_operator
        fixture = evidence_fixture.RecommendationFixture(dataset_root=str(self.root / (name + "-data")),
            evidence_root=str(self.root / "outputs/data_factory/runs" if name == "evidence" else self.root / name))
        fixture.hypothesis = copy.deepcopy(owner.hypothesis)
        fixture.draft = copy.deepcopy(owner.draft)
        fixture.manifest, fixture.receipt = compile_collection_campaign(fixture.draft, hypothesis=fixture.hypothesis)
        endpoints = {item["workspace_id"]: item for item in seed_app.catalog["combinations"]
                     if item["combination_digest"] in {entry["combination_digest"] for entry in seed_app._workspace_cycle()}}
        fixture.evidence = []
        for index in range(2):
            slot = fixture.manifest["slots"][index]
            condition = next(item["coverage_condition"] for item in fixture.hypothesis["base_conditions"]
                             if item["base_condition_digest"] == slot["base_condition_digest"])
            digests = endpoints[condition["place_id"]]["source_digests"]
            # Bind the synthetic artifact builder to this real compiled domain.
            def digest(value):
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and value[0] in {"cell", "object", "grasp"}:
                    return digests[value[0]]
                return canonical_digest(value)
            with mock.patch.object(evidence_fixture, "digest", side_effect=digest):
                fixture.evidence.append(fixture.episode(index, index))
            episode = fixture.evidence[-1]
            runtime = episode["artifacts"]["runtime_binding"]
            runtime.update(schema_version="data_factory.production_episode_binding.v1", data_disposition="PRODUCTION",
                           state_initialization_digest=None, scene_observation_digest=canonical_digest("synthetic-observation"))
            evidence_fixture.redigest(runtime, "binding_digest")
            fixture.rebind_episode(episode)
        return fixture, fixture.store()

    def send(self, app, op, payload=None):
        self.sequence += 1
        return app.core.consume(self.native.request(app, op, payload or {}, f"acquisition-{self.sequence}"))

    def application(self, session, source=None, rollout=None):
        app = self.native.application(session, builder=functools.partial(
            build_physical_operator_application, collection_evidence_call=source,
            **({"rollout_lifecycle_path": rollout} if rollout is not None else {})))
        self.send(app, "update_camera_bindings", {"bindings": {
            "usb-Generic_USB2.0_PC_CAMERA-video-index0": "UP", "usb-Generic_USB2.0_PC_CAMERA_2-video-index0": "WRIST"}})
        preset = load_json_strict(self.root / "config/data_factory/motion_presets/demonstration-rhythm-r001.json")
        for value in ({"selection": {"task": "pick_place"}}, {"selection": {"variant": "TWO_STAGE_ALIGN_V2"}},
                      {"motion_preset": {"id": preset["motion_preset_id"], "digest": canonical_digest(preset)}},
                      {"requested_count": 2}):
            self.send(app, "update_draft", {"draft_id": app.draft["draft_id"], **value})
        return app

    def refresh(self):
        self.send(self.app, "refresh_collection_advice")
        advice = self.app.projection()["collection_advice"]
        self.assertEqual(advice["status"], "READY", advice)
        return advice

    def test_original_rollout_condition_applies_and_compiles_after_recovery(self):
        # Existing physical composition and compiler, synthetic stored outcomes;
        # no device, model, motion, recorder or approval callback is available.
        seed = self.application("pickup-source")
        self.send(seed, "update_draft", {"draft_id": seed.draft["draft_id"], "selection": {"task": "pickup_e2e"}})
        self.send(seed, "compile_draft", {"draft_id": seed.draft["draft_id"], "data_disposition": "PRODUCTION"})
        fixture, pickup_runs = self.store_evidence(seed, "pickup-evidence")
        lifecycle = evidence_fixture.learned_lifecycle(fixture, order=1, reviewed=True)
        plan = lifecycle["plan_envelope"]["plan"]
        preset = selected_motion_preset(seed.catalog, seed.draft["motion_preset"])
        qualification = preset["qualifications"][seed.selection["motion_id"]]["digest"]
        plan["binding_digests"]["motion_qualification"] = qualification
        plan["learned_source_program"]["binding_digests"]["motion_qualification"] = qualification
        plan["binding_digests"]["motion_preset"] = preset["digest"]
        plan["learned_source_program"]["binding_digests"]["motion_preset"] = preset["digest"]
        receipt = copy.deepcopy(next(r for r in fixture.hypothesis["resolver_receipts"]
                                    if r["resolved_job_digest"] == plan["resolved_job_digest"]))
        inputs = {key: plan["binding_digests"][key] for key in receipt["input_digests"]}
        resolved = canonical_digest({"job": receipt["normalized_job"], "input_digests": inputs})
        plan["resolved_job_digest"] = plan["learned_source_program"]["resolved_job_digest"] = resolved
        plan["scene_binding"] = {"scene_state_digest": canonical_digest("synthetic-original-scene"),
                                 "revision": 1, "object_instance_id": "cube"}
        lifecycle["scene_binding"] = copy.deepcopy(plan["scene_binding"])
        evidence_fixture.AcquisitionRolloutTests.rebind(lifecycle)
        root = self.root / "outputs/data_factory/runs" / lifecycle["run_id"]
        root.mkdir()
        path = root / "learned_lifecycle_result.json"
        path.write_text(json.dumps(lifecycle))
        preapproval = {"schema_version": "data_factory.preapproval_evidence.v4",
            "run_id": lifecycle["run_id"], "plan_digest": lifecycle["plan_digest"],
            "resolved_job_digest": resolved, "plan_envelope": lifecycle["plan_envelope"],
            "plan_envelope_digest": canonical_digest(lifecycle["plan_envelope"]),
            "resolved_inputs": {"normalized_job": receipt["normalized_job"],
                                "input_digests": inputs, "resolved_job_digest": resolved}}
        (root / "preapproval_evidence.json").write_text(json.dumps(preapproval))
        source = {"run_directories": pickup_runs, "scene_state_path": self.source["scene_state_path"],
                  "rollout_lifecycle_path": str(path)}
        self.app = self.application("pickup-recollect", source=lambda: source, rollout=path)
        self.send(self.app, "update_draft", {"draft_id": self.app.draft["draft_id"], "selection": {"task": "pickup_e2e"}})
        self.send(self.app, "update_draft", {"draft_id": self.app.draft["draft_id"], "normalized_seed": 987})
        original_pose = {k: receipt["normalized_job"][k] for k in ("place_id", "x_mm", "y_mm", "yaw_deg")}
        self.assertNotEqual(original_pose, self.app.draft["current_object_pose"])
        before = self.native.scene.read()["objects"], self.native.cell.read(), path.read_bytes()
        self.assertEqual(qualification, selected_motion_preset(self.app.catalog, self.app.draft["motion_preset"])
                         ["qualifications"][self.app.selection["motion_id"]]["digest"])
        advice = self.refresh()
        self.assertEqual(advice["recommendation"]["sampling"]["authoring_mode"], "DIRECT_EDIT")
        self.assertEqual(advice["recommendation"]["object_poses"], [position_fixture.POSE, original_pose])
        retained = path.read_bytes()
        unchanged_draft = copy.deepcopy(self.app.draft)
        path.write_text("{}")
        with self.assertRaisesRegex(ContractError, "COLLECTION_ADVICE_STALE"):
            self.send(self.app, "choose_collection_advice", {"choice": "APPLY", "expected_recommendation_digest": advice["recommendation_digest"]})
        self.assertEqual(self.app.draft, unchanged_draft)
        path.write_bytes(retained)
        self.send(self.app, "choose_collection_advice", {"choice": "APPLY", "expected_recommendation_digest": advice["recommendation_digest"]})
        self.assertEqual(self.app.draft["authoring_mode"], "DIRECT_EDIT")
        self.assertEqual(self.app.projection()["collection_advice"]["status"], "APPLIED")
        path.write_text("{}")
        with self.assertRaisesRegex(ContractError, "COLLECTION_ADVICE_STALE"):
            self.send(self.app, "compile_draft", {"draft_id": self.app.draft["draft_id"], "data_disposition": "PRODUCTION"})
        self.assertIsNone(self.app._campaign)
        path.write_bytes(retained)
        self.send(self.app, "compile_draft", {"draft_id": self.app.draft["draft_id"], "data_disposition": "PRODUCTION"})
        owner = self.app._campaign.campaign_operator
        conditions = {b["base_condition_digest"]: b["coverage_condition"] for b in owner.hypothesis["base_conditions"]}
        self.assertEqual([{k: conditions[s["base_condition_digest"]][k] for k in original_pose}
                          for s in owner.manifest["slots"]], [position_fixture.POSE, original_pose])
        self.assertIsNone(self.app.projection()["campaign_authorization"])
        self.assertEqual((self.native.scene.read()["objects"], self.native.cell.read(), path.read_bytes()), before)
        self.native.forbidden.assert_not_called()

    def test_native_evidence_apply_and_compile_preserve_pose_and_authority(self):
        from tools.data_factory import collection_recommendation_io as io
        probe = mock.patch.object(io, "discover_stored_collection", wraps=io.discover_stored_collection)
        discovery = probe.start()
        self.addCleanup(probe.stop)
        before = self.native.scene.snapshot(), self.native.cell.read(), self.native.episode.read_bytes()
        self.app.core.snapshot()
        self.assertEqual(discovery.call_count, 0)
        advice = self.refresh()
        self.assertEqual(discovery.call_count, 1)
        expected = copy.deepcopy(self.app._collection_advice["recommendation"])
        self.assertEqual(expected["observed_semantic_pass_by_source"], {"PLACE_A": 1, "PLACE_B": 1})
        self.assertEqual(expected["object_poses"][0], position_fixture.POSE)
        self.assertEqual(len(expected["object_poses"]), 3)
        self.assertNotIn(str(self.root), json.dumps(advice))
        retained = copy.deepcopy(self.app._collection_advice)
        self.app._collection_advice.pop("mode")
        self.app._collection_advice["status"] = "UNAVAILABLE"
        self.assertNotIn(str(self.root), json.dumps(self.app._advice_projection()))
        self.app._collection_advice = retained
        self.send(self.app, "choose_collection_advice", {"choice": "APPLY", "expected_recommendation_digest": advice["recommendation_digest"]})
        self.assertIsNone(self.app._campaign)
        self.assertEqual(self.app.draft["authoring_mode"], "ASSISTED")
        self.assertEqual(self.app.projection()["collection_advice"]["status"], "APPLIED")
        self.assertEqual(discovery.call_count, 2)
        self.assertEqual((self.native.scene.snapshot(), self.native.cell.read(), self.native.episode.read_bytes()), before)
        self.send(self.app, "compile_draft", {"draft_id": self.app.draft["draft_id"], "data_disposition": "PRODUCTION"})
        self.assertEqual(self.app.projection()["workflow_state"], "REVIEW_CAMPAIGN")
        self.assertEqual(len(self.app._campaign.campaign_operator.manifest["slots"]), 2)
        self.assertIsNone(self.app.projection()["campaign_authorization"])
        self.assertEqual(discovery.call_count, 3)
        # The existing compiler may rebind the released slot to the new run;
        # it must retain the original release and exact object position.
        self.assertEqual(self.native.scene.read()["objects"], before[0]["scene_state"]["objects"])
        self.assertEqual(self.native.cell.read(), before[1])
        self.assertEqual(self.native.episode.read_bytes(), before[2])
        self.native.forbidden.assert_not_called()

    def test_accepted_advice_revalidates_catalog_and_stored_evidence_before_compile(self):
        advice = self.refresh()
        before = copy.deepcopy(self.app.draft)
        with mock.patch.object(self.app, "catalog_reload_call", return_value={"catalog_digest": canonical_digest("changed-catalog")}):
            with self.assertRaisesRegex(ContractError, "COLLECTION_ADVICE_STALE"):
                self.send(self.app, "choose_collection_advice", {"choice": "APPLY", "expected_recommendation_digest": advice["recommendation_digest"]})
        self.assertEqual(self.app.draft, before)
        self.send(self.app, "choose_collection_advice", {"choice": "APPLY", "expected_recommendation_digest": advice["recommendation_digest"]})
        # Corrupt only a synthetic stored candidate; discovery must exclude it.
        (self.runs[1] / "candidate_admission.json").write_text("{}")
        before = self.native.scene.snapshot(), self.native.cell.read()
        with self.assertRaisesRegex(ContractError, "COLLECTION_ADVICE_STALE"):
            self.send(self.app, "compile_draft", {"draft_id": self.app.draft["draft_id"], "data_disposition": "PRODUCTION"})
        self.assertEqual((self.native.scene.snapshot(), self.native.cell.read()), before)
        self.assertIsNone(self.app._campaign)
        self.native.forbidden.assert_not_called()

    def test_edits_constraints_and_changed_source_cannot_overwrite_draft(self):
        advice = self.refresh()
        payload = {"choice": "APPLY", "expected_recommendation_digest": advice["recommendation_digest"]}
        self.send(self.app, "update_draft", {"draft_id": self.app.draft["draft_id"], "normalized_seed": 88})
        edited = copy.deepcopy(self.app.draft)
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_OP"):
            self.send(self.app, "choose_collection_advice", payload)
        self.assertEqual(self.app.draft, edited)
        fresh = self.refresh()
        self.assertNotEqual(fresh["recommendation_digest"], advice["recommendation_digest"])
        from tools.data_factory.operator.workflow.collection_advice import derive_next_draft
        for constraint in ({"pinned": ["existing-pin"]}, {"excluded": ["existing-exclusion"]},
                           {"state_space_design_profile": {"custom": "preserved"}}):
            constrained = {**copy.deepcopy(self.app.draft), **constraint}
            checked, candidate = derive_next_draft(self.source, catalog=self.app.catalog, selection=self.app.selection,
                                                  draft=constrained, paired=True)
            self.assertEqual(checked["reason_codes"], ["COLLECTION_ADVICE_CONSTRAINED_DRAFT"])
            self.assertIsNone(candidate)
        self.send(self.app, "update_draft", {"draft_id": self.app.draft["draft_id"], "authoring_mode": "DIRECT_EDIT"})
        edited = copy.deepcopy(self.app.draft)
        self.send(self.app, "refresh_collection_advice")
        self.assertEqual(self.app.projection()["collection_advice"]["reason_codes"], ["COLLECTION_ADVICE_CONSTRAINED_DRAFT"])
        self.assertEqual(self.app.draft, edited)
        self.send(self.app, "update_draft", {"draft_id": self.app.draft["draft_id"], "authoring_mode": "ASSISTED"})
        advice = self.refresh()
        self.native.cell.mark_blocked("EXECUTION_IN_PROGRESS", "newer-motion", canonical_digest("newer-plan"))
        with self.assertRaisesRegex(ContractError, "COLLECTION_ADVICE_STALE"):
            self.send(self.app, "choose_collection_advice", {"choice": "APPLY", "expected_recommendation_digest": advice["recommendation_digest"]})
        self.send(self.app, "refresh_collection_advice")
        self.assertEqual(self.app.projection()["collection_advice"]["reason_codes"], ["COLLECTION_ADVICE_SOURCE_CHANGED"])
        self.assertIsNone(self.app._campaign)
        self.native.forbidden.assert_not_called()

    def test_native_compiler_mismatch_rejects_before_campaign_or_source_effects(self):
        from tools.data_factory.operator import composition
        advice = self.refresh()
        self.send(self.app, "choose_collection_advice", {"choice": "APPLY", "expected_recommendation_digest": advice["recommendation_digest"]})
        before = self.native.scene.snapshot(), self.native.cell.read()
        sampler = composition.project_workspace_cycle_poses
        def changed(*args, **kwargs):
            poses = sampler(*args, **kwargs)
            poses[1]["x_mm"] += .1
            return poses
        with mock.patch.object(composition, "project_workspace_cycle_poses", side_effect=changed):
            with self.assertRaisesRegex(ContractError, "COLLECTION_ADVICE_COMPILER_MISMATCH"):
                self.send(self.app, "compile_draft", {"draft_id": self.app.draft["draft_id"], "data_disposition": "PRODUCTION"})
        self.assertIsNone(self.app._campaign)
        self.assertEqual((self.native.scene.snapshot(), self.native.cell.read()), before)
        self.native.forbidden.assert_not_called()

    def test_shipped_ui_apply_keep_response_loss_and_no_post_replay(self):
        for choice in ("APPLY", "KEEP"):
            app = self.app if choice == "APPLY" else self.application("keep-consumer")
            bridge = LoopbackBridge(core=app.core, ui_root=evidence_fixture.ROOT / "operator-ui", port=0)
            thread = threading.Thread(target=bridge.serve_forever)
            thread.start()
            try:
                process = subprocess.run(["node", "operator-ui/tests/collection-advice-recovery.cjs", bridge.origin,
                                          "operator-ui/app.js", choice], cwd=evidence_fixture.ROOT,
                                         capture_output=True, text=True, timeout=60)
                self.assertEqual(process.returncode, 0, process.stderr)
                result = json.loads(process.stdout)
                self.assertEqual([item["method"] for item in result["requests"]], ["POST", "GET"])
                advice = result["canonical"]["projection"]["collection_advice"]
                self.assertEqual(advice["status"], "APPLIED" if choice == "APPLY" else "KEPT")
                self.assertIn("수집 제안", result["conditions"])
                self.assertIn("→", result["conditions"])
                self.assertNotIn(str(self.root), json.dumps(advice))
                self.assertTrue(result["applyHidden"])
                with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_OP"):
                    self.send(app, "choose_collection_advice", {"choice": "KEEP" if choice == "APPLY" else "APPLY",
                                                               "expected_recommendation_digest": advice["recommendation_digest"]})
                self.assertIsNone(app._campaign)
            finally:
                bridge.close()
                thread.join(5)
        self.native.forbidden.assert_not_called()


class AcquisitionLaunchTests(unittest.TestCase):
    def test_cli_forwards_explicit_original_source_without_starting_runtime(self):
        from tools.data_factory.operator import cli
        with mock.patch.object(cli, "_serve", return_value=0) as serve:
            self.assertEqual(cli.main(["--effect-scope", "PHYSICAL", "--no-auto-prepare",
                                      "--rollout-lifecycle", "/synthetic/learned_lifecycle_result.json"]), 0)
        self.assertEqual(serve.call_args.kwargs["rollout_lifecycle"], Path("/synthetic/learned_lifecycle_result.json"))
        self.assertFalse(serve.call_args.kwargs["auto_prepare"])

    def test_rollout_source_cannot_silently_enter_an_unrelated_runtime(self):
        from tools.data_factory.operator.composition import build_operator_runtime
        for scope in ("FAKE", "LEARNED_RUN", "TRAINING_REVIEW", "CURATOR_REVIEW"):
            with self.subTest(scope=scope), self.assertRaisesRegex(ContractError, "COLLECTION_ROLLOUT_CONFIGURATION"):
                build_operator_runtime(effect_scope=scope, rollout_lifecycle="/synthetic/not-read.json")
