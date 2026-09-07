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

    def application(self, session="restarted-collection", builder=build_physical_operator_application):
        environment = {"schema_version": "data_factory.operator_environment.v1", "state": "READY",
                       "observed_at": "2026-09-07T00:00:00Z", "components": {
                           name: {"state": "READY", "owner": "fixture", "reason": "ATTACHED"}
                           for name in ("robot", "controller", "gripper", "camera")}}
        app, _ = builder(
            repository_root=self.root, session_id=session, operator_label="local-operator", job_path=JOB,
            environment_call=lambda: environment, prepare_environment_call=lambda: environment,
            initial_environment=environment, initial_data_mode="GENERAL_COLLECTION",
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


if __name__ == "__main__":
    unittest.main()
