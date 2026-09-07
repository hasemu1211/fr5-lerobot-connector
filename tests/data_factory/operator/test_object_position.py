from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.data_factory.operator import test_composition as fixtures
from tools.data_factory.cell_state import CellStateStore
from tools.data_factory.scene_state import SceneStateStore, release_slot
from tools.data_factory.operator.composition import build_physical_operator_application
from tools.data_factory.operator.workflow.intents import INTENT_SCHEMA
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


JOB = "config/data_factory/jobs/center-live-24mm-20260903-r002.job.json"
POSE = {"place_id": "PLACE_A", "x_mm": -88.7533061520591,
        "y_mm": 20.198281706564718, "yaw_deg": -24.734637526256734}


class ObjectPositionContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixtures.OperatorConsoleTests.portable_repository(self.root)
        self.job = load_json_strict(self.root / JOB)
        self.robot = self.job["robot_system_id"]
        self.cells = self.root / "outputs/data_factory/cells"
        self.scene = SceneStateStore(self.cells, self.robot)
        self.cell = CellStateStore(self.cells, self.robot)
        self.plan = canonical_digest("synthetic-executed-plan")
        self.forbidden = mock.Mock(side_effect=AssertionError("no physical effects"))
        self.scene.update_object(instance_id="cube", object_profile_id=self.job["object_profile_id"],
                                 state="ON_SURFACE", pose={**POSE, "x_mm": 0}, source="HUMAN", updated_by="fixture")
        self.cell.mark_blocked("EXECUTION_IN_PROGRESS", "completed-e2", self.plan)
        before = self.scene.snapshot()
        self.slot = release_slot(robot_system_id=self.robot, pose=POSE,
                                 object_profile_id=self.job["object_profile_id"],
                                 exclusion_geometry_digest=canonical_digest({"shape": "BOX", "dimensions_mm": [24.0, 24.0, 24.0]}),
                                 role="DESTINATION_THEN_NEXT_SOURCE")
        evidence = {
            "schema_version": "data_factory.recycle_release_evidence.v2",
            "run_id": "completed-e2", "plan_digest": self.plan, "release_slot_id": self.slot["slot_id"],
            "expected_scene_state_digest": before["scene_state_digest"], "expected_scene_revision": before["scene_state"]["revision"],
            "gripper_reference_m": .021, "gripper_feedback_m": .021,
            "terminal_phases": ["RECYCLE_APPROACH_PTP", "LOWER_LIN", "GRIPPER_OPEN", "RETREAT_LIN", "SAFE_POSE_PTP"],
            "post_retreat_snapshot_digest": canonical_digest("synthetic-terminal"), "next_start_tolerance_rad": .01,
            "release_outcome": "EXPECTED_LANDED", "outcome_source": "CAMPAIGN_CONTROL_PROXY",
            "decided_by": "fixture", "decided_at": "2026-09-07T00:00:00Z",
        }
        self.scene.transition_release(instance_id="cube", release_slot=self.slot, evidence=evidence,
                                      updated_by="pickup-executor", expected_digest=before["scene_state_digest"],
                                      expected_revision=before["scene_state"]["revision"], allowed_next_run_id="old-e3")
        self.cell.acknowledge_ready("fixture", expected_run_id="completed-e2", expected_plan_digest=self.plan)
        # Original episode evidence is immutable; consumers only read it.
        self.episode = self.root / "outputs/data_factory/runs/completed-e2/execution_response.json"
        self.episode.parent.mkdir(parents=True)
        from tools.data_factory_recovery import write_json_atomic
        write_json_atomic(self.episode, {"release_evidence": evidence, "scene_transition": self.scene.snapshot()})
        self.original = self.episode.read_bytes()

    def read(self):
        return self.scene.object_position(object_profile_id=self.job["object_profile_id"], dimensions_mm=[24.0, 24.0, 24.0])

    def application(self, session="restarted-collection", builder=build_physical_operator_application, data_mode="GENERAL_COLLECTION"):
        environment = {"schema_version": "data_factory.operator_environment.v1", "state": "READY",
                       "observed_at": "2026-09-07T00:00:00Z", "components": {
                           name: {"state": "READY", "owner": "fixture", "reason": "ATTACHED"}
                           for name in ("robot", "controller", "gripper", "camera")}}
        app, _ = builder(
            repository_root=self.root, session_id=session, operator_label="local-operator", job_path=JOB,
            environment_call=lambda: environment, prepare_environment_call=lambda: environment,
            initial_environment=environment, initial_data_mode=data_mode,
            production_dataset_root=self.root / "datasets/fr5_episodes/uncreated-dataset", gripper_retune_path=None,
            camera_environment_call=lambda *_: environment,
            discovery_call=lambda: ["usb-Generic_USB2.0_PC_CAMERA-video-index0", "usb-Generic_USB2.0_PC_CAMERA_2-video-index0"],
            activation_call=self.forbidden, snapshot_call=self.forbidden, run_live_call=self.forbidden,
            gripper_readback_call=lambda: {"active": True, "position_valid": True, "gripper_index": 1,
                                          "reference_position_m": .021, "feedback_position_m": .021,
                                          "sample_age_s": 0., "max_age_s": .1, "source": "CONTROLLER_STATE"})
        self.addCleanup(app.close)
        return app

    def request(self, app, op, payload, identifier):
        view = app.bridge_core.snapshot()
        return {"schema_version": INTENT_SCHEMA, "intent_id": identifier, "session_id": view["session_id"],
                "view_revision": view["revision"], "view_digest": view["view_digest"], "op": op, "payload": payload}

    def test_native_restart_retains_endpoint_after_recorder_failure_without_writes(self):
        self.cell.mark_blocked("TECHNICAL_VALIDATOR_FAILED", "completed-e2", self.plan)
        before = self.scene.snapshot()
        app = self.application()
        view = app.bridge_core.snapshot()["projection"]
        self.assertEqual(view["draft"]["current_object_pose"], POSE)
        self.assertEqual(view["draft"]["object_position"]["source"], "ROBOT_RELEASE_PROXY")
        self.assertEqual(view["draft"]["object_position"]["status"], "AVAILABLE")
        self.assertEqual(self.scene.snapshot(), before)
        self.assertFalse(self.cell.read()["cell_ready"])
        self.assertEqual(self.episode.read_bytes(), self.original)
        self.assertFalse((self.root / "datasets/fr5_episodes/uncreated-dataset").exists())
        self.forbidden.assert_not_called()

    def test_restart_selects_recorded_workspace_and_exact_pose(self):
        before = self.scene.snapshot()
        pose = {**POSE, "place_id": "PLACE_B", "x_mm": 12.125, "y_mm": 3.5}
        slot = release_slot(robot_system_id=self.robot, pose=pose, object_profile_id=self.job["object_profile_id"],
                            exclusion_geometry_digest=self.slot["exclusion_geometry_digest"])
        evidence = copy.deepcopy(load_json_strict(self.episode)["release_evidence"])
        evidence.update(run_id="completed-b", release_slot_id=slot["slot_id"],
                        expected_scene_state_digest=before["scene_state_digest"], expected_scene_revision=before["scene_state"]["revision"])
        self.cell.mark_blocked("EXECUTION_IN_PROGRESS", "completed-b", self.plan)
        self.scene.transition_release(instance_id="cube", release_slot=slot, evidence=evidence, updated_by="pickup-executor",
                                      expected_digest=before["scene_state_digest"], expected_revision=before["scene_state"]["revision"])
        self.cell.acknowledge_ready("fixture", expected_run_id="completed-b", expected_plan_digest=self.plan)
        app = self.application()
        self.assertEqual(app.selection["workspace_id"], "PLACE_B")
        self.assertEqual(app.draft["current_object_pose"], pose)
        self.assertEqual(self.episode.read_bytes(), self.original)
        self.forbidden.assert_not_called()

    def test_newer_execution_and_unknown_block_reuse_but_leave_ui_and_override(self):
        self.cell.mark_blocked("EXECUTION_IN_PROGRESS", "interrupted-e3", canonical_digest("new-plan"))
        app = self.application()
        view = app.bridge_core.snapshot()["projection"]
        self.assertEqual(view["draft"]["object_position"]["reason"], "OBJECT_POSITION_NEWER_EXECUTION")
        self.assertNotIn("compile_draft", view["available_ops"])
        self.assertIn("update_draft", view["available_ops"])
        self.scene.update_object(instance_id="cube", object_profile_id=self.job["object_profile_id"], state="UNKNOWN",
                                 source="ROBOT_ACTION", updated_by="pickup-executor")
        self.assertEqual(app.bridge_core.snapshot()["projection"]["draft"]["object_position"]["status"], "BLOCKED")
        before_cell = self.cell.read()
        app.bridge_core.consume(self.request(app, "update_draft", {"draft_id": app.draft["draft_id"], "current_object_pose": POSE}, "manual-after-interruption"))
        self.assertEqual(self.read()["source"], "HUMAN")
        self.assertEqual(self.cell.read(), before_cell)
        self.assertFalse(self.cell.read()["cell_ready"])
        self.forbidden.assert_not_called()

    def test_explicit_web_intent_override_survives_response_loss_and_restart(self):
        app = self.application()
        moved = {**POSE, "x_mm": 12.125, "y_mm": -9.75}
        request = self.request(app, "update_draft", {"draft_id": app.draft["draft_id"], "current_object_pose": moved}, "manual-move")
        app.bridge_core.consume(request)  # response lost; recover through GET only
        self.assertEqual(app.bridge_core.snapshot()["projection"]["draft"]["current_object_pose"], moved)
        self.assertEqual(self.read()["source"], "HUMAN")
        revision = self.scene.read()["revision"]
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
            app.bridge_core.consume(request)
        self.assertEqual(self.scene.read()["revision"], revision)
        restarted = self.application("second-restart")
        self.assertEqual(restarted.draft["current_object_pose"], moved)
        self.assertEqual(self.episode.read_bytes(), self.original)
        self.forbidden.assert_not_called()

    def test_stale_scene_cas_requires_fresh_derivation_and_preserves_later_edits(self):
        app = self.application()
        request = self.request(app, "update_draft", {"draft_id": app.draft["draft_id"], "current_object_pose": {**POSE, "x_mm": 7}}, "stale-move")
        moved = {**POSE, "x_mm": 19.25}
        self.scene.update_object(instance_id="cube", object_profile_id=self.job["object_profile_id"], state="ON_SURFACE",
                                 source="HUMAN", updated_by="other-operator", pose=moved)
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_STALE_VIEW"):
            app.bridge_core.consume(request)
        self.assertEqual(app.bridge_core.snapshot()["projection"]["draft"]["object_position"]["status"], "STALE")
        app.bridge_core.consume(self.request(app, "update_draft", {"draft_id": app.draft["draft_id"], "requested_count": 3}, "count"))
        app.bridge_core.consume(self.request(app, "refresh_object_position", {}, "refresh"))
        self.assertEqual(app.draft["current_object_pose"], moved)
        self.assertEqual(app.draft["requested_count"], 3)
        self.assertEqual(self.episode.read_bytes(), self.original)

    def test_same_process_blocked_campaign_uses_durable_release_without_complete(self):
        from tests.data_factory.operator.workflow.test_application import StubCampaign
        app = self.application()
        app.draft["current_object_pose"] = {**POSE, "x_mm": 0, "y_mm": 0, "yaw_deg": 0}
        campaign = StubCampaign(app._id("campaign"), app.draft, "PRODUCTION")
        campaign.state = "BLOCKED"
        campaign.completed = 2
        app._campaign = campaign
        app.bridge_core.consume(self.request(app, "new_campaign_same_settings", {}, "after-error"))
        self.assertTrue(campaign.closed)
        self.assertEqual(app.draft["current_object_pose"], POSE)
        self.assertEqual(app.draft["object_position"]["source"], "ROBOT_RELEASE_PROXY")
        self.assertEqual(self.episode.read_bytes(), self.original)
        self.forbidden.assert_not_called()

    def test_consumed_source_without_new_landing_and_conflicting_plan_reject(self):
        snapshot = self.scene.snapshot()
        slot = snapshot["scene_state"]["slot_allocations"][self.slot["slot_id"]]
        self.scene.consume_next_source(slot_id=self.slot["slot_id"], run_id="old-e3",
                                       expected_scene_digest=snapshot["scene_state_digest"],
                                       expected_slot_digest=canonical_digest(slot))
        self.assertEqual(self.read()["status"], "BLOCKED")
        self.assertNotIn("compile_draft", self.application().bridge_core.snapshot()["projection"]["available_ops"])
        self.assertEqual(self.episode.read_bytes(), self.original)

    def test_native_compile_preserves_release_provenance_and_requires_new_authorization(self):
        app = self.application()
        app.bridge_core.consume(self.request(app, "update_camera_bindings", {"bindings": {
            "usb-Generic_USB2.0_PC_CAMERA-video-index0": "UP", "usb-Generic_USB2.0_PC_CAMERA_2-video-index0": "WRIST",
        }}, "cameras"))
        app.bridge_core.consume(self.request(app, "update_draft", {"draft_id": app.draft["draft_id"], "requested_count": 1}, "count"))
        before = self.scene.snapshot()
        app.bridge_core.consume(self.request(app, "compile_draft", {"draft_id": app.draft["draft_id"], "data_disposition": "PRODUCTION"}, "compile"))
        view = app.bridge_core.snapshot()["projection"]
        self.assertEqual(view["workflow_state"], "REVIEW_CAMPAIGN")
        self.assertIsNone(view["campaign_authorization"])
        after = self.scene.snapshot()
        self.assertEqual(after["scene_state"]["objects"], before["scene_state"]["objects"])
        old_slot = before["scene_state"]["slot_allocations"][self.slot["slot_id"]]
        new_slot = after["scene_state"]["slot_allocations"][self.slot["slot_id"]]
        new_run = f"{app._id('campaign')}-run-1"
        self.assertEqual(new_slot, {**old_slot, "allowed_run_id": new_run})
        self.assertIn("authorize_campaign", view["available_ops"])
        from tools.data_factory.run_job import _scene_binding
        validated = {"normalized_job": {**self.job, **POSE}, "object_profile": {"dimensions_mm": [24.0, 24.0, 24.0]}}
        binding = _scene_binding(validated, {**POSE, "x_mm": 10}, new_run, root=self.cells)
        self.assertEqual(binding["source_slot"]["allowed_run_id"], new_run)
        self.assertEqual(self.scene.snapshot(), after)  # resolving a plan remains read-only
        with self.assertRaisesRegex(ContractError, "SCENE_SLOT_NEXT_RUN"):
            _scene_binding(validated, {**POSE, "x_mm": 10}, "old-e3", root=self.cells)
        self.assertEqual(self.episode.read_bytes(), self.original)
        self.forbidden.assert_not_called()

    def compile_trial(self, builder=build_physical_operator_application, session="restarted-collection"):
        app = self.application(session=session, builder=builder, data_mode="TEST_COLLECTION")
        self.assertEqual(app.draft["current_object_pose"], POSE)
        self.assertEqual(app.projection()["draft"]["object_position"]["source"], "ROBOT_RELEASE_PROXY")

        def consume(op, payload, identifier):
            return app.bridge_core.consume(self.request(app, op, payload, identifier))

        consume("update_camera_bindings", {"bindings": {
            "usb-Generic_USB2.0_PC_CAMERA-video-index0": "UP", "usb-Generic_USB2.0_PC_CAMERA_2-video-index0": "WRIST",
        }}, "test-cameras")
        draft_id = app.draft["draft_id"]
        preset = load_json_strict(self.root / "config/data_factory/motion_presets/demonstration-rhythm-r001.json")
        for index, value in enumerate((
            {"motion_preset": {"id": preset["motion_preset_id"], "digest": canonical_digest(preset)}},
            {"selection": {"task": "pick_place"}}, {"selection": {"variant": "TWO_STAGE_ALIGN_V2"}},
            {"requested_count": 2}, {"authoring_mode": "DIRECT_EDIT"},
        )):
            consume("update_draft", {"draft_id": draft_id, **value}, f"test-choice-{index}")
        for index, pair in enumerate(reversed(copy.deepcopy(app.draft["direct_pairs"][1:]))):
            consume("update_draft", {"draft_id": draft_id, "remove_pair": pair}, f"test-remove-{index}")
        destination = {**POSE, "place_id": "PLACE_B", "x_mm": 0, "y_mm": 0}
        for index, pair in enumerate(({**destination, "start_pose_id": app.draft["selected_start_pose_ids"][0]},
                                      {**POSE, "start_pose_id": None})):
            consume("update_draft", {"draft_id": draft_id, "add_pair": pair}, f"test-add-{index}")
        before = self.scene.snapshot(), self.cell.read()
        source_objects = copy.deepcopy(self.scene.read()["objects"])
        compiled = consume("compile_draft", {"draft_id": draft_id, "data_disposition": "TEST_ONLY"}, "test-compile")["result"]
        owner = app._campaign.campaign_operator
        self.assertEqual(len(owner.manifest["slots"]), 2)
        self.assertEqual((self.scene.snapshot(), self.cell.read()), before)
        self.assertEqual(self.scene.read()["objects"], source_objects)
        self.assertEqual(self.read()["source"], "ROBOT_RELEASE_PROXY")
        self.assertIsNone(app.projection()["campaign_authorization"])
        authorization = self.request(app, "authorize_campaign", {
            "draft_id": draft_id, "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"], "data_disposition": "TEST_ONLY",
        }, "test-authorize")
        return app, authorization

    def test_native_test_ab_a_uses_physical_scene_and_isolated_data_at_executor_boundary(self):
        from tools.data_factory import run_job
        observed = []
        motion = load_json_strict(self.root / "config/data_factory/motion_qualifications/fr5-place-a-wood-cube-24mm-r001.json")

        def live(payload, cancel, publish, **kwargs):
            roots = kwargs["runtime_root_binding"]
            self.assertEqual(roots["cell_root"], str(self.cells))
            self.assertEqual(roots["scene_scope"], "PHYSICAL_CELL")
            self.assertFalse(kwargs["candidate_writer_enabled"])
            self.assertIs(kwargs["motion_preset_trial"], True)
            observed.append(copy.deepcopy(roots))
            kwargs.update(camera_warmup_call=lambda *_: {}, recorder_factory=self.forbidden, validator_call=self.forbidden)
            with mock.patch.object(run_job, "JsonlProcess", side_effect=ContractError("SYNTHETIC_EXECUTOR_BOUNDARY")) as child:
                result = run_job.run_live(payload, cancel, publish, **kwargs)
                argv = child.call_args.args[0]
                self.assertEqual(argv[argv.index("--cell-state-root") + 1], str(self.cells))
                child.assert_called_once()
                return result

        def builder(**kwargs):
            kwargs.update(run_live_call=live, activation_call=lambda: True,
                          snapshot_call=lambda: fixtures.pose_snapshot(motion["qualified_safe_joint_positions_rad"], age=.01))
            return build_physical_operator_application(**kwargs)

        app, authorization = self.compile_trial(builder)
        source_objects = copy.deepcopy(self.scene.read()["objects"])
        app.bridge_core.consume(authorization)
        result = app._campaign.wait_for_episode(5.)
        self.assertEqual(result["code"], "SYNTHETIC_EXECUTOR_BOUNDARY", result)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.scene.read()["objects"], source_objects)
        self.assertEqual(self.episode.read_bytes(), self.original)
        self.forbidden.assert_not_called()

    def test_physical_test_stale_authorization_and_manual_override_share_native_source(self):
        app, authorization = self.compile_trial()
        before = self.scene.snapshot()
        self.cell.mark_blocked("EXECUTION_IN_PROGRESS", "other-test", canonical_digest("other-plan"))
        with self.assertRaises(ContractError):
            app.bridge_core.consume(authorization)
        with self.assertRaisesRegex(ContractError, "OBJECT_POSITION_CHANGED"):
            app._campaign.authorize_campaign(authorization["payload"], {})
        self.assertIsNone(app._campaign.campaign_authorization)
        self.assertEqual(self.scene.snapshot(), before)
        restarted = self.application("test-restart", data_mode="TEST_COLLECTION")
        self.assertEqual(restarted.draft["object_position"]["reason"], "OBJECT_POSITION_NEWER_EXECUTION")
        moved = {**POSE, "x_mm": 11.125}
        request = self.request(restarted, "update_draft", {"draft_id": restarted.draft["draft_id"],
                                                        "current_object_pose": moved}, "reported-move")
        cell = self.cell.read()
        restarted.bridge_core.consume(request)
        self.assertEqual(self.read()["source"], "HUMAN")
        self.assertEqual(self.read()["pose"], moved)
        self.assertEqual(self.cell.read(), cell)
        with self.assertRaises(ContractError):
            restarted.bridge_core.consume(request)
        self.assertEqual(self.episode.read_bytes(), self.original)
        self.forbidden.assert_not_called()

    def test_native_two_leg_test_release_and_interruption_are_visible_to_production(self):
        from tools.data_factory import run_job
        motion = load_json_strict(self.root / "config/data_factory/motion_qualifications/fr5-place-a-wood-cube-24mm-r001.json")
        destination = {**POSE, "place_id": "PLACE_B", "x_mm": 0, "y_mm": 0}
        for outcome in ("return", "recorder_failure_at_b", "unknown_return"):
            with self.subTest(outcome=outcome):
                # Restore only the isolated synthetic fixture between scenarios.
                if outcome != "return":
                    from tools.data_factory_recovery import write_json_atomic
                    write_json_atomic(self.cells / self.robot / "scene_state.json", baseline_scene)
                    write_json_atomic(self.cells / self.robot / "state.json", baseline_cell)
                baseline_scene, baseline_cell = self.scene.read(), self.cell.read()
                observed = []

                def live(payload, cancel, publish, **kwargs):
                    roots = kwargs["runtime_root_binding"]
                    self.assertEqual(roots["cell_root"], str(self.cells))
                    self.assertFalse(kwargs["candidate_writer_enabled"])
                    self.assertTrue(kwargs["motion_preset_trial"])
                    run_job._prepare_run_dir(payload)
                    validated, program, binding = kwargs["resolver"](payload)
                    source = {key: validated["normalized_job"][key] for key in POSE}
                    target = {key: payload["destination"]["job"][key] for key in POSE}
                    self.assertEqual((source, target), (POSE, destination) if not observed else (destination, POSE))
                    self.assertEqual(self.read()["pose"], source)
                    for step in program["steps"]:
                        if not step["phase"].startswith("GRIPPER"):
                            self.assertEqual((step["limits"]["velocity_scaling"], step["limits"]["acceleration_scaling"]), (.1, .1))
                    observed.append((source, target))
                    slot = binding["source_slot"]
                    self.scene.consume_next_source(slot_id=slot["slot_id"], run_id=payload["run_id"],
                                                   expected_scene_digest=binding["scene_state_digest"], expected_slot_digest=slot["slot_digest"])
                    plan = canonical_digest(program)
                    self.cell.mark_blocked("EXECUTION_IN_PROGRESS", payload["run_id"], plan)
                    # These are simulated device results through the existing scene owner.
                    # No action, recorder, dataset commit or image evidence is fabricated.
                    kwargs["one_job"].state = "COMPLETE"
                    if outcome == "unknown_return" and len(observed) == 2:
                        self.scene.update_object(instance_id="cube", object_profile_id=self.job["object_profile_id"],
                                                 state="UNKNOWN", source="ROBOT_ACTION", updated_by="pickup-executor")
                        self.cell.mark_blocked("ROS_EXEC_FAILED", payload["run_id"], plan)
                        return {"ok": False, "code": "ROS_EXEC_FAILED", "state": "BLOCKED"}
                    before = self.scene.snapshot()
                    evidence = copy.deepcopy(load_json_strict(self.episode)["release_evidence"])
                    evidence.update(run_id=payload["run_id"], plan_digest=plan,
                                    release_slot_id=binding["release_slot"]["slot_id"],
                                    expected_scene_state_digest=before["scene_state_digest"],
                                    expected_scene_revision=before["scene_state"]["revision"])
                    released = self.scene.transition_release(
                        instance_id="cube", release_slot=binding["release_slot"], evidence=evidence,
                        updated_by="pickup-executor", expected_digest=before["scene_state_digest"],
                        expected_revision=before["scene_state"]["revision"], allowed_next_run_id=binding.get("allowed_next_run_id"),
                    )
                    if outcome == "recorder_failure_at_b":
                        self.cell.mark_blocked("TECHNICAL_VALIDATOR_FAILED", payload["run_id"], plan)
                        return {"ok": False, "code": "TECHNICAL_VALIDATOR_FAILED", "state": "BLOCKED"}
                    self.cell.acknowledge_ready("synthetic-executor", expected_run_id=payload["run_id"], expected_plan_digest=plan)
                    return {"ok": True, "data": {"technical_validator": {"status": "PASS", "plan_digest": plan},
                            "postcommit_scene_state_digest": released["scene_state_digest"], "episode_ledger": {}}}

                def builder(**kwargs):
                    kwargs.update(run_live_call=live, activation_call=lambda: True,
                                  snapshot_call=lambda: fixtures.pose_snapshot(motion["qualified_safe_joint_positions_rad"], age=.01))
                    return build_physical_operator_application(**kwargs)

                app, authorization = self.compile_trial(builder, session=f"trial-{outcome}")
                # Dataset/ledger admission is outside this simulated-motion test.
                # Actual run_live/executor routing is exercised separately above.
                with mock.patch.object(run_job, "bind_candidate_episode_state", return_value={}), \
                        mock.patch.object(run_job, "read_candidate_episode_state", return_value=None):
                    app._campaign.candidate_state_observe_call = lambda *_args, **_kwargs: None
                    app.bridge_core.consume(authorization)
                    result = app._campaign.wait_for_episode(10.)
                self.assertEqual(result["code"], {"return": "TECHNICAL_PASS", "recorder_failure_at_b": "TECHNICAL_VALIDATOR_FAILED",
                                                 "unknown_return": "ROS_EXEC_FAILED"}[outcome], result)
                self.assertEqual(len(observed), 1 if outcome == "recorder_failure_at_b" else 2)
                production = self.application(f"production-after-{outcome}")
                position = production.projection()["draft"]["object_position"]
                if outcome == "unknown_return":
                    self.assertEqual(position["status"], "BLOCKED")
                    self.assertNotIn("compile_draft", production.projection()["available_ops"])
                else:
                    self.assertEqual(position["status"], "AVAILABLE")
                    self.assertEqual(production.draft["current_object_pose"], destination if outcome == "recorder_failure_at_b" else POSE)
                    self.assertEqual(position["source"], "ROBOT_RELEASE_PROXY")
                with self.assertRaises(ContractError):
                    app.bridge_core.consume(authorization)
                self.assertEqual(len(observed), 1 if outcome == "recorder_failure_at_b" else 2)
                self.assertEqual(self.episode.read_bytes(), self.original)
                app.close()
                production.close()
        self.forbidden.assert_not_called()


if __name__ == "__main__":
    unittest.main()
