from __future__ import annotations

import copy
import threading
import unittest
from pathlib import Path
from unittest import mock

from tools.data_factory.collection_seed import derive_domain_seed
from tools.data_factory.motion.object_reposition import (
    build_object_reposition_binding,
)
from tools.data_factory.state_space import (
    sample_yaw_cdf_strata,
    validate_yaw_sampling_profile,
)
from tools.data_factory.operator.workflow.application import (
    CollectionOperatorApplication,
    _project_active_episode_plan,
)
from tools.data_factory.operator.web import projection
from tools.data_factory.operator.workflow.intents import INTENT_SCHEMA, OperatorIntentCore
from tools.data_factory.operator.catalog import (
    project_balanced_start_pose_ids,
    load_operator_catalog,
    project_assisted_poses,
    project_workspace_cycle_poses,
    validate_operator_selection,
)
from tools.data_factory.task_recipe import (
    compile_episode_instruction_binding,
    compile_task_binding,
)
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


ROOT = Path(__file__).resolve().parents[4]


def intent(view, op, payload, suffix):
    return {
        "schema_version": INTENT_SCHEMA,
        "intent_id": f"application-intent-{suffix}",
        "session_id": view["session_id"],
        "view_revision": view["revision"],
        "view_digest": view["view_digest"],
        "op": op,
        "payload": copy.deepcopy(payload),
    }


class StubCampaign:
    def __init__(self, campaign_id, draft, data_disposition):
        self.campaign_id = campaign_id
        self.draft = copy.deepcopy(draft)
        self.data_disposition = data_disposition
        self.state = "AUTHORING"
        self.completed = 0
        self.closed = False
        self.history = []
        self.candidate_pending = False
        self.campaign_coverage = None
        self.reason_codes = []
        self.start_transition_owner = None
        self.core = OperatorIntentCore(
            session_id=campaign_id,
            projection_call=self.projection,
            handlers={
                "compile_draft": self.compile_draft,
                "authorize_campaign": self.authorize_campaign,
                "cancel_session": self.cancel_session,
                "review_candidate": self.review_candidate,
            },
        )

    @property
    def bridge_core(self):
        return self.core

    def compile_draft(self, payload, _view):
        if payload != {
            "draft_id": self.draft["draft_id"],
            "data_disposition": self.data_disposition,
        }:
            raise ContractError("STUB_COMPILE")
        self.state = "REVIEW_CAMPAIGN"
        return {
            "outcome": self.state,
            "manifest_digest": canonical_digest([self.campaign_id, "manifest"]),
            "envelope_digest": canonical_digest([self.campaign_id, "envelope"]),
            "episode_count": self.draft["requested_count"],
        }

    def authorize_campaign(self, payload, _view):
        if set(payload) != {
            "draft_id", "manifest_digest", "envelope_digest", "data_disposition",
        }:
            raise ContractError("STUB_AUTHORIZE")
        self.state = "RUNNING"
        return {"outcome": "RUNNING", "active_child_id": f"{self.campaign_id}-run-1"}

    def cancel_session(self, payload, _view):
        if set(payload) != {"active_child_id"}:
            raise ContractError("STUB_CANCEL")
        self.state = "TERMINAL"
        return {"outcome": "CANCELLED"}

    def review_candidate(self, payload, _view):
        if not self.candidate_pending or set(payload) != {
            "review_binding_digest", "choice", "reason",
        }:
            raise ContractError("STUB_REVIEW")
        self.candidate_pending = False
        return {"outcome": "REVIEWED", "status": payload["choice"]}

    def complete(self):
        self.completed = self.draft["requested_count"]
        self.history = [
            {
                "outcome": "PASS",
                "code": "TECHNICAL_PASS",
                "result_digest": canonical_digest([self.campaign_id, index]),
            }
            for index in range(self.completed)
        ]
        self.state = "TERMINAL"

    def projection(self):
        total = self.draft["requested_count"]
        start_owner_active = (
            isinstance(self.start_transition_owner, dict)
            and self.start_transition_owner.get("active") is True
        )
        result = {
            "runtime": {
                "workflow_state": self.state,
                "measurement_outcome": "PASS" if self.state == "TERMINAL" else "NOT_MEASURED",
                "reason_codes": copy.deepcopy(self.reason_codes),
                "active_child_id": (
                    f"{self.campaign_id}-run-{self.completed + 1}"
                    if self.state == "RUNNING"
                    else self.start_transition_owner["run_id"]
                    if start_owner_active else None
                ),
                **({
                    "motion_owner": copy.deepcopy(self.start_transition_owner),
                    "motion": {
                        "status": "ACTIVE",
                        "label": "시작 자세 전환 종료 확인 대기",
                    },
                } if start_owner_active else {}),
                **({
                    "phase": "EXECUTING", "phase_label": "수집 동작 실행",
                    "progress": 50,
                } if self.state == "RUNNING" else {}),
            },
            "available_ops": (["review_candidate"] if self.candidate_pending else {
                "AUTHORING": ["compile_draft"],
                "REVIEW_CAMPAIGN": ["authorize_campaign"],
                "RUNNING": ["cancel_session"],
            }.get(self.state, [])),
            "draft": {
                "draft_id": self.draft["draft_id"],
                "revision": self.draft["revision"],
                "budget": total,
                "selected_count": total,
                "cells": [],
            },
            "campaign_envelope": (
                None if self.state == "AUTHORING" else {
                    "manifest_digest": canonical_digest([self.campaign_id, "manifest"]),
                    "envelope_digest": canonical_digest([self.campaign_id, "envelope"]),
                    "episode_count": total,
                }
            ),
            "campaign_session": None if self.state == "AUTHORING" else {
                "campaign": {
                    "state": "COMPLETE" if self.state == "TERMINAL" else "READY",
                    "completed_intents": self.completed,
                    "remaining_intents": total - self.completed,
                },
                "start_transition_owner": copy.deepcopy(
                    self.start_transition_owner
                ),
            },
            "episode_history": copy.deepcopy(self.history),
            "effect_counts": {"robot": 0, "dataset": 0, "training": 0},
        }
        if self.campaign_coverage is not None:
            result["campaign_coverage"] = copy.deepcopy(self.campaign_coverage)
        return result

    def close(self):
        self.closed = True


class StubWorkspaceManager:
    LABELS = ("CENTER", "X_REF", "Y_CHECK")

    def __init__(self, sequence, display_name="작업영역"):
        self.sequence = sequence
        self.display_name = display_name
        self.captures = {}
        self.preview = None
        self.promotion = None

    def projection(self):
        return {
            "calibration_id": f"workspace-stub-{self.sequence}",
            "display_name": self.display_name,
            "captures": {label: label in self.captures for label in self.LABELS},
            "preview": copy.deepcopy(self.preview),
            "promotion": copy.deepcopy(self.promotion),
            "execution_authorized": False,
            "training_approved": False,
        }

    def capture(self, label, snapshot):
        if label not in self.LABELS or self.preview is not None:
            raise ContractError("STUB_WORKSPACE_CAPTURE")
        self.captures[label] = copy.deepcopy(snapshot)
        return self.projection()

    def save(self, preview_digest):
        if self.preview is None or preview_digest != self.preview["preview_digest"]:
            raise ContractError("STUB_WORKSPACE_SAVE")
        self.promotion = {
            "calibration_id": self.projection()["calibration_id"],
            "preview_digest": preview_digest,
        }
        return copy.deepcopy(self.promotion)

    def discard_preview(self, preview_digest):
        if (
            self.preview is None
            or self.preview["status"] != "CANDIDATE_OUT_OF_TOLERANCE"
            or preview_digest != self.preview["preview_digest"]
        ):
            raise ContractError("STUB_WORKSPACE_DISCARD")
        self.preview = None
        return self.projection()


class CollectionOperatorApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        device = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
        cls.catalog_fixture = load_operator_catalog(ROOT, device_ids=[device])

    def setUp(self):
        # CollectionOperatorApplication owns a deep copy, so this immutable
        # repository fixture can be shared without leaking state across tests.
        self.catalog = self.catalog_fixture
        combination = next(
            item for item in self.catalog["combinations"]
            if item["execution"]["TEST_COLLECTION"]["executable"]
            and item["task_id"] == "pickup_e2e"
        )
        self.selection = {
            "schema_version": "data_factory.operator_selection.v1",
            "combination_digest": combination["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            "workspace_id": combination["workspace_id"],
            "frame_id": combination["frame_id"],
            "task_id": combination["task_id"],
            "object_id": combination["object_id"],
            "grasp_id": combination["grasp_id"],
            "cell_id": combination["cell_id"],
            "start_pose_id": combination["start_pose_id"],
            "motion_id": combination["motion_id"],
            "variant_id": combination["variant_id"],
            "policy_id": "DETERMINISTIC_SPREAD",
            "camera_profile_id": combination["camera_profile_id"],
            "camera_device_id": combination["camera_device_id"],
        }
        self.environment = {
            "schema_version": "data_factory.operator_environment.v1",
            "state": "SETUP_REQUIRED",
            "observed_at": "2026-08-26T12:00:00Z",
            "components": {
                name: {"state": "MISSING", "owner": None, "reason": "NOT_PREPARED"}
                for name in ("robot", "controller", "gripper", "camera")
            },
        }
        self.campaigns = []

        def prepare():
            ready = copy.deepcopy(self.environment)
            ready["state"] = "READY"
            ready["components"] = {
                name: {"state": "READY", "owner": f"owner-{name}", "reason": "ATTACHED"}
                for name in ready["components"]
            }
            self.environment = ready
            return ready

        def factory(campaign_id, selection, draft):
            self.assertEqual(selection, self.selection)
            campaign = StubCampaign(campaign_id, draft, "TEST_ONLY")
            self.campaigns.append(campaign)
            return campaign

        self.application = CollectionOperatorApplication(
            session_id="collection-application-r001",
            operator_label="local-operator",
            catalog=self.catalog,
            initial_selection=self.selection,
            projector=projection,
            environment_call=lambda: copy.deepcopy(self.environment),
            prepare_environment_call=prepare,
            campaign_factory=factory,
        )
        self.addCleanup(self.application.close)

    def consume(self, op, payload, suffix):
        view = self.application.bridge_core.snapshot()
        return self.application.bridge_core.consume(
            intent(view, op, payload, suffix),
        )["result"]

    def test_active_plan_projects_exact_u64_seeds_and_phase_parameters(self):
        object_profile = load_json_strict(
            ROOT / "config/data_factory/objects/wood-cube-24mm-r001.json",
        )
        grasp_profile = load_json_strict(
            ROOT / "config/data_factory/grasps/"
            "wood-cube-24mm-top-3p5mm-r001.json",
        )
        yaw_profile = validate_yaw_sampling_profile(
            load_json_strict(
                ROOT / "config/data_factory/yaw_sampling_profiles/"
                "wood-cube-24mm-top-r001.json",
            ),
            object_profile=object_profile, grasp_profile=grasp_profile,
        )
        seed = (1 << 63) + 41
        yaw_sample = next(
            item for item in sample_yaw_cdf_strata(
                yaw_profile, sampling_seed=seed,
                sweep_identity={"sweep": "active-plan-r001"},
                strata_count=3, conditioned_yaw_deg=0.0,
            )
            if item["sample_origin"] == "CONDITIONED_SOURCE_ANCHOR"
        )
        phase_parameters = {
            "align_clearance_m": 0.057,
            "view_offset_x_m": 0.004,
            "view_offset_y_m": -0.006,
        }
        trajectory = {
            "schema_version": "data_factory.trajectory_variant_binding.v2",
            "trajectory_variant_id": "TWO_STAGE_ALIGN_V2",
            "variation_profile_digest": canonical_digest("variation"),
            "sampling_seed": seed,
            "sample_rank": 1, "design_size": 3,
            "design_digest": canonical_digest("design"),
            "target_yaw_deg": 0.0,
            "phase_parameters": phase_parameters,
            "phase_parameters_digest": canonical_digest(phase_parameters),
            "motion_program_digest": canonical_digest("motion"),
        }
        trajectory["binding_digest"] = canonical_digest(trajectory)
        plan_digest = canonical_digest("active-plan")
        safety = {
            "schema_version": "data_factory.precommit_safety.v1",
            "run_id": "active-plan-run-r001",
            "approved_plan_digest": plan_digest,
            "scene_binding_digest": canonical_digest("scene"),
            "expected_planning_scene_digest": canonical_digest("expected"),
            "planning_scene_readback_digest": canonical_digest("readback"),
            "collision_report_digest": canonical_digest("collision"),
            "plan_only_no_motion_digest": canonical_digest("no-motion"),
            "post_reset_safe_snapshot_digest": None,
            "status": "PENDING",
        }
        projected = _project_active_episode_plan({
            "plan_digest": plan_digest,
            "approval_scope": "HIL_NUMERIC_PROXY",
            "decision_binding_digest": canonical_digest("decision"),
            "operator_summary": {"path": ["PREGRASP_VIEW_PTP"]},
            "trajectory_variant_binding": trajectory,
            "trajectory_variant_binding_digest": trajectory[
                "binding_digest"
            ],
            "yaw_sample_binding": yaw_sample,
            "yaw_sample_binding_digest": yaw_sample["binding_digest"],
            "precommit_safety": safety,
            "plan_envelope_digest": canonical_digest("envelope"),
            "preapproval_evidence_digest": canonical_digest("preapproval"),
        })
        self.assertEqual(projected["schema_version"],
                         "data_factory.active_episode_plan.v1")
        self.assertEqual(
            projected["trajectory_variant_binding"]["sampling_seed"],
            str(seed),
        )
        self.assertEqual(
            projected["yaw_sample_binding"]["sampling_seed"], str(seed),
        )
        self.assertEqual(
            projected["trajectory_variant_binding"]["phase_parameters"],
            phase_parameters,
        )

    def test_environment_precedes_factory_and_invalid_mode_has_zero_effects(self):
        view = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(view["workflow_state"], "ENVIRONMENT")
        self.assertEqual(view["available_ops"], ["prepare_environment"])
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_OP"):
            self.consume("compile_draft", {
                "draft_id": view["draft"]["draft_id"],
                "data_disposition": "TEST_ONLY",
            }, "early-compile")
        self.assertEqual(self.campaigns, [])

        self.consume("prepare_environment", {}, "prepare")
        view = self.application.bridge_core.snapshot()["projection"]
        with self.assertRaisesRegex(ContractError, "OPERATOR_APPLICATION_SELECTION"):
            self.consume("update_draft", {
                "draft_id": view["draft"]["draft_id"],
                "selection": {"data_mode": "PRODUCTION"},
            }, "general")
        self.assertEqual(self.campaigns, [])

    def test_projection_is_nonblocking_and_keeps_last_measured_environment(self):
        self.consume("prepare_environment", {}, "prepare-before-disconnect")
        self.assertEqual(
            self.application.bridge_core.snapshot()["projection"]["environment"]["state"],
            "READY",
        )

        self.environment["state"] = "SETUP_REQUIRED"
        self.environment["components"]["camera"] = {
            "state": "MISSING", "owner": None, "reason": "NOT_RUNNING",
        }
        blocked_query = mock.Mock(side_effect=AssertionError(
            "browser projection queried the physical environment",
        ))
        self.application.environment_call = blocked_query
        cached = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(cached["environment"]["state"], "READY")
        self.assertEqual(cached["runtime"]["workflow_state"], "AUTHORING")
        self.assertIn("compile_draft", cached["available_ops"])
        self.assertNotIn("campaign_projection", cached["technical_details"])
        blocked_query.assert_not_called()

    def test_blocked_prepare_is_consumed_and_preserves_component_reasons(self):
        blocked = copy.deepcopy(self.environment)
        blocked["state"] = "BLOCKED"
        blocked["components"] = {
            name: {
                "state": "BLOCKED", "owner": None,
                "reason": "OPERATOR_ENVIRONMENT_SETTLE_TIMEOUT",
            }
            for name in blocked["components"]
        }

        def prepare_blocked():
            return copy.deepcopy(blocked)

        self.application.prepare_environment_call = prepare_blocked
        result = self.consume("prepare_environment", {}, "prepare-blocked")
        self.assertEqual(result, {"outcome": "BLOCKED", "environment": blocked})
        view = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(view["environment"], blocked)
        self.assertEqual(view["runtime"]["workflow_state"], "BLOCKED")
        self.assertEqual(view["available_ops"], [])

    def test_prepare_generation_keeps_get_live_and_discards_stale_owner_once(self):
        owners = [
            {
                "started": threading.Event(), "release": threading.Event(),
                "closed": 0,
            }
            for _ in range(2)
        ]
        for owner in owners:
            self.addCleanup(owner["release"].set)
        owner_sequence = iter(owners)

        def owner_call():
            owner = next(owner_sequence)

            def run():
                owner["started"].set()
                owner["release"].wait()
                ready = copy.deepcopy(self.environment)
                ready["state"] = "READY"
                ready["components"] = {
                    name: {
                        "state": "READY", "owner": f"owner-{name}",
                        "reason": "ATTACHED",
                    }
                    for name in ready["components"]
                }
                return ready

            def close():
                owner["closed"] += 1

            return run, close

        self.application.prepare_environment_owner_call = owner_call
        results = []
        first_view = self.application.bridge_core.snapshot()
        first = threading.Thread(target=lambda: results.append(
            self.application.bridge_core.consume(
                intent(first_view, "prepare_environment", {}, "generation-one"),
            )["result"],
        ))
        first.start()
        self.assertTrue(owners[0]["started"].wait(1))

        snapshots = []
        snapshot_done = threading.Event()

        def snapshot():
            snapshots.append(self.application.bridge_core.snapshot())
            snapshot_done.set()

        snapshot_thread = threading.Thread(target=snapshot)
        snapshot_thread.start()
        self.assertTrue(snapshot_done.wait(0.2))
        snapshot_thread.join(1)
        preparing = snapshots[0]
        generation_one = preparing["projection"]["technical_details"][
            "preparation_generation"
        ]
        self.assertEqual(preparing["projection"]["runtime"]["workflow_state"], "PREPARING")
        self.assertEqual(preparing["projection"]["available_ops"], ["cancel_session"])

        cancelled = self.application.bridge_core.consume(
            intent(preparing, "cancel_session", {}, "cancel-generation-one"),
        )["result"]
        self.assertEqual(cancelled, {"outcome": "CANCELLED"})
        self.assertEqual(owners[0]["closed"], 1)

        second_view = self.application.bridge_core.snapshot()
        second = threading.Thread(target=lambda: results.append(
            self.application.bridge_core.consume(
                intent(second_view, "prepare_environment", {}, "generation-two"),
            )["result"],
        ))
        second.start()
        self.assertTrue(owners[1]["started"].wait(1))
        preparing_two = self.application.bridge_core.snapshot()
        self.assertNotEqual(
            preparing_two["projection"]["technical_details"]["preparation_generation"],
            generation_one,
        )

        owners[0]["release"].set()
        first.join(1)
        self.assertFalse(first.is_alive())
        self.assertEqual(results[0], {"outcome": "STALE", "generation": generation_one})
        self.assertEqual(owners[0]["closed"], 1)
        self.assertEqual(
            self.application.bridge_core.snapshot()["projection"]["runtime"]["workflow_state"],
            "PREPARING",
        )

        owners[1]["release"].set()
        second.join(1)
        self.assertFalse(second.is_alive())
        self.assertEqual(results[1]["outcome"], "READY")
        self.assertEqual(owners[1]["closed"], 0)
        self.assertEqual(
            self.application.bridge_core.snapshot()["projection"]["environment"]["state"],
            "READY",
        )

    def test_prepare_exception_and_close_cleanup_each_owner_once(self):
        closed = []

        def failed_owner():
            return (
                lambda: (_ for _ in ()).throw(RuntimeError("prepare failed")),
                lambda: closed.append("failed"),
            )

        self.application.prepare_environment_owner_call = failed_owner
        with self.assertRaisesRegex(RuntimeError, "prepare failed"):
            self.consume("prepare_environment", {}, "prepare-exception")
        self.assertEqual(closed, ["failed"])
        self.assertEqual(
            self.application.bridge_core.snapshot()["projection"]["available_ops"],
            ["prepare_environment"],
        )

        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def blocked_owner():
            def run():
                started.set()
                release.wait()
                return copy.deepcopy(self.environment)
            return run, lambda: closed.append("closed")

        self.application.prepare_environment_owner_call = blocked_owner
        view = self.application.bridge_core.snapshot()
        result = []
        worker = threading.Thread(target=lambda: result.append(
            self.application.bridge_core.consume(
                intent(view, "prepare_environment", {}, "prepare-close"),
            )["result"],
        ))
        worker.start()
        self.assertTrue(started.wait(1))
        self.application.close()
        self.application.close()
        self.assertEqual(closed, ["failed", "closed"])
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["outcome"], "STALE")
        self.assertEqual(closed, ["failed", "closed"])

    def test_physical_home_recovery_is_explicit_and_does_not_create_campaign(self):
        ready = self.application.prepare_environment_call()
        calls = []
        recovery = {
            "schema_version": "data_factory.home_recovery.v1",
            "status": "HOME", "arm_goal_count": 1,
            "gripper_open": True, "target_rad": [0.0] * 6,
            "final_rad": [0.0] * 6,
            "motion_qualification_digest": canonical_digest(["motion"]),
        }
        application = CollectionOperatorApplication(
            session_id="physical-recovery-application-r001",
            operator_label="local-operator",
            catalog=self.catalog,
            initial_selection=self.selection,
            projector=projection,
            environment_call=lambda: copy.deepcopy(ready),
            prepare_environment_call=lambda: copy.deepcopy(ready),
            campaign_factory=lambda *_args: self.fail("recovery created a campaign"),
            home_recovery_call=lambda: calls.append(True) or copy.deepcopy(recovery),
            initial_environment=ready,
            effect_scope="PHYSICAL",
        )
        self.addCleanup(application.close)
        before = application.bridge_core.snapshot()
        self.assertIn("recover_home", before["projection"]["available_ops"])
        result = application.bridge_core.consume(intent(
            before, "recover_home", {}, "explicit-home-recovery",
        ))["result"]
        after = application.bridge_core.snapshot()["projection"]
        self.assertEqual((result["outcome"], calls), ("HOME", [True]))
        self.assertEqual(after["home_recovery"], recovery)
        self.assertEqual(self.campaigns, [])

    def test_physical_home_recovery_is_available_before_environment_prepare(self):
        calls = []
        recovery = {
            "schema_version": "data_factory.home_recovery.v1",
            "status": "ALREADY_HOME", "arm_goal_count": 0,
            "gripper_open": True, "target_rad": [0.0] * 6,
            "final_rad": [0.0] * 6,
            "motion_qualification_digest": canonical_digest(["motion"]),
        }
        application = CollectionOperatorApplication(
            session_id="physical-early-recovery-r001",
            operator_label="local-operator",
            catalog=self.catalog,
            initial_selection=self.selection,
            projector=projection,
            environment_call=lambda: copy.deepcopy(self.environment),
            prepare_environment_call=lambda: self.fail("environment was prepared"),
            campaign_factory=lambda *_args: self.fail("recovery created a campaign"),
            home_recovery_call=lambda: calls.append(True) or copy.deepcopy(recovery),
            initial_environment=self.environment,
            effect_scope="PHYSICAL",
        )
        self.addCleanup(application.close)

        before = application.bridge_core.snapshot()
        self.assertEqual(before["projection"]["workflow_state"], "ENVIRONMENT")
        self.assertIn("prepare_environment", before["projection"]["available_ops"])
        self.assertIn("recover_home", before["projection"]["available_ops"])
        result = application.bridge_core.consume(intent(
            before, "recover_home", {}, "early-home-recovery",
        ))["result"]

        self.assertEqual((result["outcome"], calls), ("ALREADY_HOME", [True]))
        self.assertEqual(self.campaigns, [])

    def test_failed_home_recovery_refreshes_restored_environment(self):
        ready = copy.deepcopy(self.environment)
        ready["state"] = "READY"
        ready["components"] = {
            name: {
                "state": "READY", "owner": f"owner-{name}", "reason": "ATTACHED",
            }
            for name in ready["components"]
        }

        def fail_after_graph_restore():
            self.environment = copy.deepcopy(ready)
            raise ContractError("HOME_RECOVERY_MODE_SWITCH")

        application = CollectionOperatorApplication(
            session_id="physical-failed-recovery-r001",
            operator_label="local-operator",
            catalog=self.catalog,
            initial_selection=self.selection,
            projector=projection,
            environment_call=lambda: copy.deepcopy(self.environment),
            prepare_environment_call=lambda: self.fail("environment was prepared"),
            campaign_factory=lambda *_args: self.fail("recovery created a campaign"),
            home_recovery_call=fail_after_graph_restore,
            initial_environment=self.environment,
            effect_scope="PHYSICAL",
        )
        self.addCleanup(application.close)

        before = application.bridge_core.snapshot()
        with self.assertRaisesRegex(ContractError, "HOME_RECOVERY_MODE_SWITCH"):
            application.bridge_core.consume(intent(
                before, "recover_home", {}, "failed-home-recovery",
            ))

        after = application.bridge_core.snapshot()["projection"]
        self.assertEqual(after["environment"], ready)
        self.assertEqual(after["workflow_state"], "AUTHORING")
        self.assertEqual(self.campaigns, [])

    def test_only_projected_canonical_ops_dispatch_and_legacy_bulk_is_immutable(self):
        self.assertEqual(set(self.application.bridge_core.handlers), {
            "prepare_environment", "update_draft", "compile_draft",
            "edit_campaign_draft", "authorize_campaign", "cancel_session",
            "review_candidate", "new_campaign_same_settings",
            "recover_home",
            "capture_workspace_point", "preview_workspace",
            "discard_workspace_preview",
            "save_workspace",
            "new_workspace_registration",
        })
        initial = self.application.bridge_core.snapshot()
        called = []
        compile_handler = self.application.bridge_core.handlers["compile_draft"]
        self.application.bridge_core.handlers["compile_draft"] = (
            lambda _payload, _view: called.append(True) or {"outcome": "FORGED"}
        )
        try:
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_OP"):
                self.application.bridge_core.consume(intent(initial, "compile_draft", {
                    "draft_id": initial["projection"]["draft"]["draft_id"],
                    "data_disposition": "TEST_ONLY",
                }, "unprojected-compile"))
        finally:
            self.application.bridge_core.handlers["compile_draft"] = compile_handler
        self.assertEqual((called, self.campaigns), ([], []))

        self.consume("prepare_environment", {}, "prepare-public-ssot")
        before = self.application.bridge_core.snapshot()
        legacy = {
            "authoring_mode": "DIRECT", "requested_count": 2,
            "normalized_seed": 9, "pinned": [], "excluded": [],
            "direct_slots": [{"forged": True}],
        }
        with self.assertRaisesRegex(ContractError, "OPERATOR_APPLICATION_STATE"):
            self.application.bridge_core.consume(intent(
                before, "update_draft", legacy, "legacy-bulk-draft",
            ))
        for index, alias in enumerate((
            "update_selection", "cancel_campaign", "review_episode",
        ), 1):
            current = self.application.bridge_core.snapshot()
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_OP"):
                self.application.bridge_core.consume(intent(
                    current, alias, {}, f"legacy-alias-{index}",
                ))
        after = self.application.bridge_core.snapshot()
        self.assertEqual(after["projection"]["draft"], before["projection"]["draft"])
        self.assertEqual(after["projection"]["selection"], before["projection"]["selection"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertNotIn("direct_slots", self.application.draft)
        self.assertEqual(self.campaigns, [])

    def test_projection_is_the_reusable_browser_product_contract(self):
        initial = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(initial["runtime"]["workflow_state"], "PREPARING")
        self.assertEqual(initial["available_ops"], ["prepare_environment"])
        self.assertEqual(initial["setup"]["host_status"], "ACTION_REQUIRED")

        self.consume("prepare_environment", {}, "prepare-product")
        view = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(view["runtime"]["workflow_state"], "AUTHORING")
        self.assertEqual(view["draft"]["repeat"], 1)
        self.assertEqual(
            view["draft"]["normalized_seed"],
            self.application.draft["normalized_seed"],
        )
        self.assertEqual(view["data_disposition"], "TEST_ONLY")
        self.assertEqual(
            set(view["catalog"]["axes"]),
            {
                "workspace", "frame", "task", "object", "grasp", "start",
                "motion", "variant", "camera", "data_mode", "split",
            },
        )
        self.assertTrue(next(
            item for item in view["catalog"]["axes"]["data_mode"]
            if item["id"] == "TEST_ONLY"
        )["available"])
        self.assertTrue(next(
            item for item in view["catalog"]["axes"]["task"]
            if item["id"] == "pick_place"
        )["available"])
        self.consume("update_draft", {
            "draft_id": view["draft"]["draft_id"], "requested_count": 6,
        }, "count-six")
        updated = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(
            (updated["draft"]["requested_count"], updated["draft"]["repeat"]),
            (6, 1),
        )
        self.consume("update_draft", {
            "draft_id": updated["draft"]["draft_id"], "repeat": 2,
        }, "repeat-two")
        updated = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(
            (updated["draft"]["requested_count"], updated["draft"]["repeat"]),
            (6, 2),
        )
        self.assertIn("compile_draft", updated["available_ops"])

    def test_seed_is_session_stable_projected_and_js_safe(self):
        def create(session_id):
            application = CollectionOperatorApplication(
                session_id=session_id,
                operator_label="local-operator",
                catalog=self.catalog,
                initial_selection=self.selection,
                projector=projection,
                environment_call=lambda: copy.deepcopy(self.environment),
                prepare_environment_call=self.application.prepare_environment_call,
                campaign_factory=lambda *_args: self.fail("campaign was requested"),
                initial_environment=self.environment,
            )
            self.addCleanup(application.close)
            return application

        first = create("collection-seed-stable-r001")
        repeated = create("collection-seed-stable-r001")
        different = create("collection-seed-stable-r002")
        seeds = [
            application.bridge_core.snapshot()["projection"]["draft"][
                "normalized_seed"
            ]
            for application in (first, repeated, different)
        ]
        self.assertEqual(seeds[0], seeds[1])
        self.assertNotEqual(seeds[0], seeds[2])
        self.assertTrue(all(0 <= seed <= (1 << 53) - 1 for seed in seeds))

    def test_sampling_provenance_projects_selected_profiles_without_sampling(self):
        catalog = load_operator_catalog(
            ROOT, device_ids=["sampling-camera-a", "sampling-camera-b"],
        )
        combination = next(
            item for item in catalog["combinations"]
            if item["task_id"] == "pickup_e2e"
            and item["object_id"] == "wood-cube-24mm-r001"
            and item["variant_id"] == "TWO_STAGE_ALIGN_V2"
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        selection = {
            "schema_version": "data_factory.operator_selection.v1",
            "combination_digest": combination["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            **{
                field: combination[field]
                for field in (
                    "workspace_id", "frame_id", "task_id", "object_id",
                    "grasp_id", "cell_id", "start_pose_id", "motion_id",
                    "variant_id", "camera_profile_id", "camera_device_id",
                )
            },
            "policy_id": "DETERMINISTIC_SPREAD",
        }
        application = CollectionOperatorApplication(
            session_id="sampling-provenance-r001",
            operator_label="local-operator",
            catalog=catalog,
            initial_selection=selection,
            projector=projection,
            environment_call=lambda: copy.deepcopy(self.environment),
            prepare_environment_call=lambda: copy.deepcopy(self.environment),
            campaign_factory=lambda *_args: self.fail("campaign was requested"),
        )
        self.addCleanup(application.close)

        view = application.bridge_core.snapshot()["projection"]
        provenance = view["sampling_provenance"]
        self.assertEqual(provenance["object_dimensions_mm"], [24, 24, 24])
        self.assertEqual(
            set(provenance["yaw_sampling_profile"]),
            {
                "yaw_sampling_profile_id", "profile_digest", "distribution",
                "canonical_interval_deg", "required_camera_roles",
            },
        )
        self.assertEqual(
            provenance["yaw_sampling_profile"]["canonical_interval_deg"],
            {"minimum": -45.0, "maximum_exclusive": 45.0},
        )
        design = provenance["state_space_design_profile"]
        self.assertEqual(
            set(design),
            {
                "state_space_design_profile_id", "profile_digest",
                "object_profile_id", "object_profile_digest",
                "grasp_profile_id", "grasp_profile_digest",
                "yaw_sampling_profile_id", "yaw_sampling_profile_digest",
                "spatial_strata", "yaw_cdf_strata",
                "derived_yaw_cdf_tiers", "assignment", "execution_order",
                "initial_source_policy",
                "per_workspace_repeat_one_sweep_episode_count",
                "full_cell_yaw_coverage_sweeps",
                "per_workspace_repeat_one_full_cell_yaw_coverage_episode_count",
            },
        )
        self.assertEqual(design["spatial_strata"], {"columns": 5, "rows": 3})
        self.assertEqual(design["yaw_cdf_strata"], 3)
        self.assertEqual(
            [tier["yaw_deg"] for tier in design["derived_yaw_cdf_tiers"]],
            [
                {"minimum": -45.0, "maximum_exclusive": -15.0},
                {"minimum": -15.0, "maximum_exclusive": 15.0},
                {"minimum": 15.0, "maximum_exclusive": 45.0},
            ],
        )
        self.assertEqual(
            design["per_workspace_repeat_one_sweep_episode_count"], 15,
        )
        self.assertEqual(design["full_cell_yaw_coverage_sweeps"], 3)
        self.assertEqual(
            design[
                "per_workspace_repeat_one_full_cell_yaw_coverage_episode_count"
            ],
            45,
        )
        self.assertEqual(
            design["yaw_sampling_profile_digest"],
            provenance["yaw_sampling_profile"]["profile_digest"],
        )
        self.assertEqual(
            set(provenance["approach_sampling_profile"]),
            {
                "approach_sampling_profile_id", "profile_digest",
                "parameter_distribution", "required_camera_roles",
            },
        )
        self.assertEqual(
            provenance["approach_sampling_profile"]["required_camera_roles"],
            ["wrist"],
        )
        self.assertNotIn("sampling_seed", str(provenance))

    def test_experiment_design_controls_derive_coverage_and_preserve_sampler_parity(self):
        catalog = load_operator_catalog(
            ROOT, device_ids=["design-camera-a", "design-camera-b"],
        )
        combination = next(
            item for item in catalog["combinations"]
            if item["task_id"] == "pick_place"
            and item["workspace_id"] == "PLACE_A"
            and item["object_id"] == "wood-cube-24mm-r001"
            and item["variant_id"] == "TWO_STAGE_ALIGN_V2"
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        selection = {
            "schema_version": "data_factory.operator_selection.v1",
            "combination_digest": combination["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            **{
                field: combination[field]
                for field in (
                    "workspace_id", "frame_id", "task_id", "object_id",
                    "grasp_id", "cell_id", "start_pose_id", "motion_id",
                    "variant_id", "camera_profile_id", "camera_device_id",
                )
            },
            "policy_id": "DETERMINISTIC_SPREAD",
        }
        ready = copy.deepcopy(self.environment)
        ready["state"] = "READY"
        ready["components"] = {
            name: {"state": "READY", "owner": "owner", "reason": "ATTACHED"}
            for name in ready["components"]
        }
        setup = {
            "profiles": [{
                "start_pose_id": selection["start_pose_id"],
                "display_name": "HOME", "status": "AVAILABLE",
            }],
            "selected_start_pose_ids": [selection["start_pose_id"]],
        }
        application = CollectionOperatorApplication(
            session_id="experiment-design-controls-r001",
            operator_label="local-operator", catalog=catalog,
            initial_selection=selection, projector=projection,
            environment_call=lambda: copy.deepcopy(ready),
            prepare_environment_call=lambda: copy.deepcopy(ready),
            campaign_factory=lambda *_args: self.fail("campaign was requested"),
            start_pose_setup=setup,
            start_pose_capture_call=lambda _name: copy.deepcopy(setup),
            initial_environment=ready, effect_scope="PHYSICAL",
        )
        self.addCleanup(application.close)

        initial = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(initial, "update_draft", {
            "draft_id": initial["projection"]["draft"]["draft_id"],
            "normalized_seed": 7761137905102010,
        }, "design-confirmed-seed"))
        seeded = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(seeded, "update_draft", {
            "draft_id": initial["projection"]["draft"]["draft_id"],
            "requested_count": 45,
        }, "design-count-45"))
        projected = application.bridge_core.snapshot()["projection"]
        summary = projected["state_space_summary"]
        self.assertEqual(summary["design_shape"], {
            "columns": 5, "rows": 3, "yaw_cdf_strata": 3,
        })
        self.assertEqual(
            (summary["planned_episode_count"], summary["object_position_count"]),
            (45, 46),
        )
        self.assertEqual(
            {
                item["workspace_id"]: (
                    item["planned_episode_count"],
                    item["full_coverage_episode_count"],
                )
                for item in summary["workspace_coverage"]
            },
            {"PLACE_A": (23, 45), "PLACE_B": (22, 45)},
        )
        self.assertEqual(summary["full_coverage_episode_count"], 90)
        sampling_kwargs = {
            "repeat": 1,
            "normalized_seed": derive_domain_seed(
                application.draft["normalized_seed"], "spatial",
            ),
            "yaw_sampling_seed": derive_domain_seed(
                application.draft["normalized_seed"], "yaw",
            ),
        }
        previous_default = project_workspace_cycle_poses(
            application.catalog, application.selection,
            application.draft["current_object_pose"], 45,
            **sampling_kwargs,
        )
        configured_default = project_workspace_cycle_poses(
            application.catalog, application.selection,
            application.draft["current_object_pose"], 45,
            state_space_design_profile=application.draft[
                "state_space_design_profile"
            ],
            **sampling_kwargs,
        )
        self.assertEqual(configured_default, previous_default)
        self.assertEqual(len(configured_default), 46)

        before_invalid = application.bridge_core.snapshot()
        for index, factors in enumerate((
            {"columns": 0, "rows": 3, "yaw_cdf_strata": 3},
            {"columns": -1, "rows": 3, "yaw_cdf_strata": 3},
            {"columns": 3.0, "rows": 3, "yaw_cdf_strata": 3},
            {"columns": 11, "rows": 10, "yaw_cdf_strata": 3},
            {"columns": 2, "rows": 2, "yaw_cdf_strata": 5},
        )):
            with self.assertRaisesRegex(
                ContractError, "OPERATOR_APPLICATION_STATE_SPACE_DESIGN",
            ):
                application.bridge_core.consume(intent(
                    application.bridge_core.snapshot(), "update_draft", {
                        "draft_id": projected["draft"]["draft_id"],
                        "state_space_design_factors": factors,
                    }, f"invalid-design-{index}",
                ))
        self.assertEqual(
            application.bridge_core.snapshot()["projection"]["sampling_provenance"],
            before_invalid["projection"]["sampling_provenance"],
        )

        current = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(current, "update_draft", {
            "draft_id": projected["draft"]["draft_id"],
            "requested_count": 6,
        }, "design-count-six"))
        current = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(current, "update_draft", {
            "draft_id": projected["draft"]["draft_id"],
            "state_space_design_factors": {
                "columns": 4, "rows": 2, "yaw_cdf_strata": 2,
            },
        }, "design-valid"))
        configured = application.bridge_core.snapshot()["projection"]
        profile = application.draft["state_space_design_profile"]
        self.assertEqual(
            configured["sampling_provenance"]["state_space_design_profile"]
            ["spatial_strata"],
            {"columns": 4, "rows": 2},
        )
        self.assertEqual(
            configured["state_space_summary"]["per_workspace_condition_count"],
            16,
        )
        expected = project_workspace_cycle_poses(
            application.catalog, application.selection,
            application.draft["current_object_pose"], 6, repeat=1,
            normalized_seed=derive_domain_seed(
                application.draft["normalized_seed"], "spatial",
            ),
            yaw_sampling_seed=derive_domain_seed(
                application.draft["normalized_seed"], "yaw",
            ),
            state_space_design_profile=profile,
        )
        application.bridge_core.consume(intent(
            application.bridge_core.snapshot(), "update_draft", {
                "draft_id": projected["draft"]["draft_id"],
                "authoring_mode": "DIRECT_EDIT",
            }, "design-materialize",
        ))
        direct = application.bridge_core.snapshot()["projection"]["draft"]
        self.assertEqual(
            [
                {key: pair[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")}
                for pair in direct["direct_pairs"]
            ],
            expected,
        )
        self.assertEqual(len(direct["direct_pairs"]), 7)

    def test_draft_seed_accepts_only_js_safe_nonnegative_integers(self):
        self.consume("prepare_environment", {}, "prepare-seed-validation")
        maximum = (1 << 53) - 1
        for index, value in enumerate((0, maximum), 1):
            view = self.application.bridge_core.snapshot()["projection"]
            self.consume("update_draft", {
                "draft_id": view["draft"]["draft_id"],
                "normalized_seed": value,
            }, f"valid-seed-{index}")
            self.assertEqual(
                self.application.bridge_core.snapshot()["projection"]["draft"][
                    "normalized_seed"
                ],
                value,
            )

        for index, value in enumerate((-1, maximum + 1, True, 1.0, "1"), 1):
            before = self.application.bridge_core.snapshot()
            with self.assertRaisesRegex(
                ContractError, "OPERATOR_APPLICATION_DRAFT",
            ):
                self.application.bridge_core.consume(intent(before, "update_draft", {
                    "draft_id": before["projection"]["draft"]["draft_id"],
                    "normalized_seed": value,
                }, f"invalid-seed-{index}"))
            self.assertEqual(
                self.application.bridge_core.snapshot()["projection"]["draft"][
                    "normalized_seed"
                ],
                maximum,
            )

    def test_direct_authoring_accepts_any_bounded_nonpreset_pose(self):
        self.consume("prepare_environment", {}, "prepare-direct-pose")
        view = self.application.bridge_core.snapshot()["projection"]
        self.consume("update_draft", {
            "draft_id": view["draft"]["draft_id"],
            "repeat": view["draft"]["requested_count"],
        }, "repeat-anchor-direct-pose")
        view = self.application.bridge_core.snapshot()["projection"]
        self.consume("update_draft", {
            "draft_id": view["draft"]["draft_id"],
            "authoring_mode": "DIRECT_EDIT",
        }, "enter-direct-pose")
        view = self.application.bridge_core.snapshot()["projection"]
        materialized = copy.deepcopy(view["draft"]["direct_poses"])
        requested = {
            "place_id": self.selection["workspace_id"],
            "yaw_deg": 197.5,
            "x_mm": 12.5,
            "y_mm": -7.25,
        }
        self.consume("update_draft", {
            "draft_id": view["draft"]["draft_id"], "add_pose": requested,
        }, "add-direct-pose")
        projected = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(projected["draft"]["authoring_mode"], "DIRECT_EDIT")
        self.assertEqual(projected["draft"]["direct_poses"], [*materialized, {
            **requested, "yaw_deg": -162.5,
        }])

        before = copy.deepcopy(projected["draft"])
        with self.assertRaises(ContractError) as rejected:
            self.consume("update_draft", {
                "draft_id": projected["draft"]["draft_id"],
                "add_pose": {**requested, "x_mm": 159.0},
            }, "reject-outside-pose")
        self.assertEqual(rejected.exception.code, "JOB_COORDINATE_BOUNDS")
        self.assertEqual(
            self.application.bridge_core.snapshot()["projection"]["draft"], before,
        )

        self.consume("update_draft", {
            "draft_id": projected["draft"]["draft_id"],
            "remove_pose": {**requested, "yaw_deg": -162.5},
        }, "remove-direct-pose")
        self.assertEqual(
            self.application.bridge_core.snapshot()["projection"]["draft"]["direct_poses"],
            materialized,
        )

    def test_assisted_to_direct_round_trip_preserves_exact_repeat_sequence(self):
        self.consume("prepare_environment", {}, "prepare-assisted-round-trip")
        view = self.application.bridge_core.snapshot()["projection"]
        self.consume("update_draft", {
            "draft_id": view["draft"]["draft_id"], "requested_count": 5,
        }, "round-trip-count")
        view = self.application.bridge_core.snapshot()["projection"]
        self.consume("update_draft", {
            "draft_id": view["draft"]["draft_id"], "repeat": 2,
        }, "round-trip-repeat")
        before = self.application.bridge_core.snapshot()
        anchor = self.application._direct_anchor()
        assisted = project_assisted_poses(
            self.catalog, self.selection, anchor, 5, repeat=2,
            normalized_seed=derive_domain_seed(
                before["projection"]["draft"]["normalized_seed"], "spatial",
            ),
            yaw_sampling_seed=derive_domain_seed(
                before["projection"]["draft"]["normalized_seed"], "yaw",
            ),
        )

        self.consume("update_draft", {
            "draft_id": before["projection"]["draft"]["draft_id"],
            "authoring_mode": "DIRECT_EDIT",
        }, "round-trip-direct")
        direct = self.application.bridge_core.snapshot()
        expected = []
        for pose in assisted:
            if pose != anchor and pose not in expected:
                expected.append(pose)
        cycle = [anchor, *direct["projection"]["draft"]["direct_poses"]]
        rebuilt = [cycle[index % len(cycle)] for index in range(5)]
        self.assertEqual(direct["projection"]["draft"]["direct_poses"], expected)
        self.assertEqual((len(expected), rebuilt), (2, assisted))
        self.assertEqual(
            (direct["projection"]["draft"]["requested_count"],
             direct["projection"]["draft"]["repeat"]),
            (5, 2),
        )
        self.assertEqual(direct["projection"]["selection"]["policy_id"], "DIRECT_SELECTION")
        self.assertNotEqual(direct["view_digest"], before["view_digest"])
        self.assertEqual(self.campaigns, [])

        with self.assertRaisesRegex(ContractError, "OPERATOR_APPLICATION_DRAFT"):
            self.consume("update_draft", {
                "draft_id": direct["projection"]["draft"]["draft_id"],
                "toggle_cell_id": self.selection["cell_id"],
            }, "reject-toggle-cell")

        self.consume("update_draft", {
            "draft_id": direct["projection"]["draft"]["draft_id"],
            "authoring_mode": "ASSISTED",
        }, "round-trip-assisted")
        assisted_again = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(assisted_again["draft"]["direct_poses"], [])
        self.assertEqual(assisted_again["selection"]["policy_id"], "DETERMINISTIC_SPREAD")

    def test_one_axis_choice_atomically_resolves_a_coherent_multi_axis_combination(self):
        catalog = copy.deepcopy(self.catalog)
        original = next(
            item for item in catalog["combinations"]
            if item["combination_digest"] == self.selection["combination_digest"]
        )
        replacement = copy.deepcopy(original)
        changed = {
            "workspace_id": "PLACE_B",
            "frame_id": "place-b-r001",
            "object_id": "wood-cube-b-r001",
            "cell_id": "PLACE_B-yaw0-origin",
            "start_pose_id": "home-b-r001",
        }
        replacement.update(changed)
        replacement["combination_digest"] = canonical_digest({
            key: value for key, value in replacement.items()
            if key != "combination_digest"
        })
        domain = copy.deepcopy(catalog["workspace_domains"][0])
        domain.update(
            domain_id=changed["frame_id"],
            workspace_id=changed["workspace_id"],
            frame_id=changed["frame_id"],
            object_id=changed["object_id"],
            preset_cell_ids=[changed["cell_id"]],
        )
        domain["coverage_region"].update(
            layout_id=None,
            layout_digest=None,
            region_id=None,
            physical_binding_status="NOT_CONFIGURED",
        )
        domain["domain_digest"] = canonical_digest({
            key: value for key, value in domain.items()
            if key != "domain_digest"
        })
        catalog["workspace_domains"].append(domain)
        axis_for_field = {
            "workspace_id": "workspace", "frame_id": "frame",
            "object_id": "object", "cell_id": "cell",
            "start_pose_id": "start_pose",
        }
        for field, identifier in changed.items():
            source = copy.deepcopy(catalog["axes"][axis_for_field[field]][0])
            source.update(id=identifier, label=identifier)
            if field == "cell_id":
                source["metadata"] = {
                    **source["metadata"], "place_id": changed["workspace_id"],
                }
            catalog["axes"][axis_for_field[field]].append(source)
        catalog["combinations"].append(replacement)
        catalog["catalog_digest"] = canonical_digest({
            key: value for key, value in catalog.items() if key != "catalog_digest"
        })
        ready = copy.deepcopy(self.environment)
        ready["state"] = "READY"
        ready["components"] = {
            name: {"state": "READY", "owner": "test-owner", "reason": "ATTACHED"}
            for name in ready["components"]
        }
        application = CollectionOperatorApplication(
            session_id="multi-axis-application-r001",
            operator_label="local-operator", catalog=catalog,
            initial_selection=self.selection,
            projector=projection,
            environment_call=lambda: ready,
            prepare_environment_call=lambda: ready,
            campaign_factory=lambda *_args: self.fail("compile was not requested"),
        )
        self.addCleanup(application.close)
        before = application.bridge_core.snapshot()
        workspace = next(
            option for option in before["projection"]["catalog"]["axes"]["workspace"]
            if option["id"] == "PLACE_B"
        )
        self.assertTrue(workspace["available"])
        draft_id = before["projection"]["draft"]["draft_id"]
        application.bridge_core.consume(intent(before, "update_draft", {
            "draft_id": draft_id, "selection": {"workspace": "PLACE_B"},
        }, "coherent-switch"))
        selected = application.bridge_core.snapshot()["projection"]["selection"]
        self.assertEqual(selected["combination_digest"], replacement["combination_digest"])
        self.assertTrue(all(selected[field] == value for field, value in changed.items()))
        self.assertEqual(
            application.bridge_core.snapshot()["projection"]["draft"]["current_object_pose"]["place_id"],
            "PLACE_B",
        )

    def test_current_object_pose_is_the_single_nonpreset_campaign_anchor(self):
        self.consume("prepare_environment", {}, "prepare-current-object")
        view = self.application.bridge_core.snapshot()["projection"]
        current = {
            "place_id": self.selection["workspace_id"],
            "yaw_deg": -37.5, "x_mm": 12.5, "y_mm": -7.25,
        }
        self.consume("update_draft", {
            "draft_id": view["draft"]["draft_id"],
            "current_object_pose": current,
        }, "set-current-object")
        authored = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(authored["draft"]["current_object_pose"], current)
        self.assertEqual(self.application._direct_anchor(), current)
        self.consume("compile_draft", {
            "draft_id": authored["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "compile-current-object")
        self.assertEqual(self.campaigns[-1].draft["current_object_pose"], current)

    def test_episode_instruction_binding_reaches_the_web_coverage_projection(self):
        self.consume("prepare_environment", {}, "prepare-instruction")
        authoring = self.application.bridge_core.snapshot()["projection"]
        self.consume("compile_draft", {
            "draft_id": authoring["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "compile-instruction")
        campaign = self.campaigns[-1]
        object_profile = {
            "object_profile_id": self.selection["object_id"],
            "description": "24 mm wooden cube",
        }
        coverage = []
        for index in range(campaign.draft["requested_count"]):
            condition = {
                "place_id": self.selection["workspace_id"],
                "yaw_deg": 0.0, "x_mm": float(index), "y_mm": 0.0,
            }
            task_binding = compile_task_binding(
                "pickup_e2e",
                source={
                    "role": "SOURCE",
                    "workspace_id": self.selection["workspace_id"],
                    "frame_id": self.selection["frame_id"],
                    "pose": copy.deepcopy(condition),
                    "sheet_digest": canonical_digest("sheet"),
                    "family_digest": canonical_digest("family"),
                    "region_binding": {
                        "layout_id": None, "layout_digest": None,
                        "region_id": None,
                        "physical_binding_status": "NOT_CONFIGURED",
                    },
                },
            )
            coverage.append({
                "order_index": index,
                "robot_start_pose_id": self.selection["start_pose_id"],
                "coverage_condition": condition,
                "coverage_condition_digest": canonical_digest(condition),
                "task_binding": task_binding,
                "episode_instruction_binding":
                compile_episode_instruction_binding(
                    task_binding, object_profile,
                ),
            })
        grasp_profile = {
            "grasp_profile_id": self.selection["grasp_id"],
            "object_profile_id": self.selection["object_id"],
            "object_profile_digest": canonical_digest(object_profile),
        }
        yaw_profile = {
            "schema_version": "data_factory.yaw_sampling_profile.v2",
            "yaw_sampling_profile_id": "application-yaw-r001",
            "qualification_status": "QUALIFIED",
            "object_profile_id": self.selection["object_id"],
            "object_profile_digest": canonical_digest(object_profile),
            "grasp_profile_id": self.selection["grasp_id"],
            "grasp_profile_digest": canonical_digest(grasp_profile),
            "yaw_target_semantics": "OBJECT_FRAME_MODULO_SYMMETRY",
            "observation_cue": "TEST_FIXTURE",
            "required_camera_roles": ["wrist"],
            "planar_symmetry_order": 4,
            "yaw_equivalence_period_deg": 90.0,
            "canonical_interval_deg": {
                "minimum": -45.0, "maximum_exclusive": 45.0,
            },
            "distribution": {"kind": "STRATIFIED_UNIFORM"},
        }
        yaw_sampling_seed = derive_domain_seed(4_242_424, "yaw")
        yaw_sample = sample_yaw_cdf_strata(
            yaw_profile, sampling_seed=yaw_sampling_seed,
            sweep_identity={"physical_slot": "application-slot-r001"},
            strata_count=1,
        )[0]
        coverage[0]["object_reposition"] = build_object_reposition_binding(
            parent_run_id="instruction-run-r001",
            continuation_run_id="instruction-run-r001",
            next_run_id="instruction-run-r002",
            start_state="HELD_OBJECT",
            source_pose=coverage[0]["coverage_condition"],
            target_pose={
                "place_id": self.selection["workspace_id"],
                "yaw_deg": yaw_sample["source_object_yaw_deg"],
                "x_mm": 4.0, "y_mm": -3.0,
            },
            object_profile=object_profile,
            grasp_profile=grasp_profile,
            yaw_sampling_profile=yaw_profile,
            yaw_sample_binding=yaw_sample,
        )
        campaign.campaign_coverage = coverage

        sequence = self.application.bridge_core.snapshot()["projection"][
            "coverage"
        ]["sequence"]
        self.assertEqual(len(sequence), campaign.draft["requested_count"])
        self.assertTrue(all(
            item["instruction"] == "pick up the 24 mm wooden cube"
            and item["episode_instruction_binding_digest"].startswith("sha256:")
            and item["task_binding_digest"].startswith("sha256:")
            for item in sequence
        ))
        reposition = sequence[0]["object_reposition"]
        self.assertEqual(reposition["recording_scope"], "OUT_OF_DATASET")
        self.assertFalse(reposition["recorder_authorized"])
        self.assertFalse(reposition["dataset_write_authorized"])
        self.assertAlmostEqual(
            reposition["target_pose"]["yaw_deg"],
            yaw_sample["source_object_yaw_deg"],
        )
        self.assertGreater(yaw_sampling_seed, (1 << 53) - 1)
        self.assertIsInstance(
            coverage[0]["object_reposition"]["yaw_sample_binding"][
                "sampling_seed"
            ],
            int,
        )
        self.assertEqual(
            reposition["yaw_sample"]["sampling_seed"],
            str(yaw_sampling_seed),
        )
        self.assertNotIn("yaw_sample_binding", reposition)
        self.assertTrue(all(
            item["object_reposition"] is None for item in sequence[1:]
        ))

    def test_one_process_runs_terminal_campaign_then_creates_fresh_lineage(self):
        self.consume("prepare_environment", {}, "prepare")
        authoring = self.application.bridge_core.snapshot()["projection"]
        self.consume("update_draft", {
            "draft_id": authoring["draft"]["draft_id"],
            "normalized_seed": 100,
        }, "set-terminal-seed")
        authoring = self.application.bridge_core.snapshot()["projection"]
        first_draft_id = authoring["draft"]["draft_id"]
        compiled = self.consume("compile_draft", {
            "draft_id": first_draft_id,
            "data_disposition": "TEST_ONLY",
        }, "compile")
        self.assertEqual(compiled["outcome"], "REVIEW_CAMPAIGN")
        review = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(review["workflow_state"], "REVIEW_CAMPAIGN")
        self.assertEqual(review["campaign_review"]["episode_count"], 3)
        self.consume("authorize_campaign", {
            "draft_id": first_draft_id,
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        }, "authorize")
        running = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(running["available_ops"], ["cancel_session"])
        self.assertEqual(running["runtime"]["progress"], 50)
        self.assertEqual(running["runtime"]["campaign_progress"], 0)

        self.campaigns[0].complete()
        terminal = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(
            (terminal["campaign"]["completed"], terminal["campaign"]["total"]),
            (3, 3),
        )
        self.assertEqual(len(terminal["episodes"]), 3)
        self.assertNotIn("campaign_projection", terminal["technical_details"])
        self.assertEqual(terminal["campaign_envelope"]["episode_count"], 3)
        self.assertEqual(terminal["coverage"]["planned"], 3)
        self.assertEqual(
            terminal["available_ops"],
            ["new_campaign_same_settings"],
        )

        self.consume("new_campaign_same_settings", {}, "new-same")
        second = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(second["workflow_state"], "AUTHORING")
        self.assertEqual(second["draft"]["requested_count"], 3)
        self.assertEqual(second["draft"]["normalized_seed"], 101)
        self.assertNotEqual(second["draft"]["draft_id"], first_draft_id)
        self.assertTrue(self.campaigns[0].closed)

        self.consume("compile_draft", {
            "draft_id": second["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "compile-second")
        self.assertEqual(len(self.campaigns), 2)
        self.assertNotEqual(self.campaigns[0].campaign_id, self.campaigns[1].campaign_id)

    def test_paused_blocked_and_cancelling_campaigns_are_not_terminal(self):
        self.consume("prepare_environment", {}, "prepare-runtime-states")
        authoring = self.application.bridge_core.snapshot()["projection"]
        compiled = self.consume("compile_draft", {
            "draft_id": authoring["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "compile-runtime-states")
        self.consume("authorize_campaign", {
            "draft_id": authoring["draft"]["draft_id"],
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        }, "authorize-runtime-states")

        for state in ("PAUSED_AWAITING_OPERATOR", "CANCELLING"):
            with self.subTest(state=state):
                self.campaigns[0].state = state
                view = self.application.bridge_core.snapshot()["projection"]
                self.assertEqual(view["workflow_state"], state)
                self.assertEqual(view["runtime"]["workflow_state"], state)
                self.assertIsNone(view["runtime"]["progress"])
                self.assertEqual(view["runtime"]["campaign_progress"], 0)
                self.assertEqual(view["available_ops"], [])

        self.campaigns[0].state = "BLOCKED"
        blocked = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(blocked["available_ops"], ["new_campaign_same_settings"])

    def test_settled_blocked_campaign_closes_and_returns_to_fresh_authoring(self):
        self.consume("prepare_environment", {}, "prepare-blocked-recovery")
        authoring = self.application.bridge_core.snapshot()["projection"]
        self.consume("update_draft", {
            "draft_id": authoring["draft"]["draft_id"],
            "normalized_seed": 73,
        }, "set-blocked-seed")
        authoring = self.application.bridge_core.snapshot()["projection"]
        compiled = self.consume("compile_draft", {
            "draft_id": authoring["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "compile-blocked-recovery")
        self.consume("authorize_campaign", {
            "draft_id": authoring["draft"]["draft_id"],
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        }, "authorize-blocked-recovery")
        self.campaigns[0].state = "BLOCKED"

        self.consume("new_campaign_same_settings", {}, "recover-blocked")

        recovered = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(recovered["workflow_state"], "AUTHORING")
        self.assertEqual(recovered["draft"]["requested_count"], 3)
        self.assertEqual(recovered["draft"]["normalized_seed"], 73)
        self.assertTrue(self.campaigns[0].closed)

    def test_uncertain_start_owner_blocks_recovery_and_campaign_replacement(self):
        ready = self.application.prepare_environment_call()
        campaigns = []
        recovery = mock.Mock(side_effect=AssertionError("HOME recovery ran"))

        def factory(campaign_id, _selection, draft):
            campaign = StubCampaign(campaign_id, draft, "TEST_ONLY")
            campaigns.append(campaign)
            return campaign

        application = CollectionOperatorApplication(
            session_id="uncertain-start-application-r001",
            operator_label="local-operator",
            catalog=self.catalog,
            initial_selection=self.selection,
            projector=projection,
            environment_call=lambda: copy.deepcopy(ready),
            prepare_environment_call=lambda: copy.deepcopy(ready),
            campaign_factory=factory,
            home_recovery_call=recovery,
            initial_environment=ready,
            effect_scope="PHYSICAL",
        )
        self.addCleanup(application.close)

        def consume(op, payload, suffix):
            view = application.bridge_core.snapshot()
            return application.bridge_core.consume(
                intent(view, op, payload, suffix),
            )["result"]

        authored = application.bridge_core.snapshot()["projection"]
        compiled = consume("compile_draft", {
            "draft_id": authored["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "uncertain-start-compile")
        consume("authorize_campaign", {
            "draft_id": authored["draft"]["draft_id"],
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        }, "uncertain-start-authorize")
        campaign = campaigns[0]
        campaign.state = "BLOCKED"
        campaign.reason_codes = ["START_TRANSITION_CANCEL_UNCERTAIN"]
        campaign.start_transition_owner = {
            "active": True,
            "run_id": f"{campaign.campaign_id}-run-1",
            "state": "TERMINALITY_UNCERTAIN",
            "owner_reachable": True,
            "action_owner_retained": True,
            "code": "START_TRANSITION_CANCEL_UNCERTAIN",
        }

        blocked = application.bridge_core.snapshot()["projection"]
        self.assertEqual(blocked["available_ops"], [])
        self.assertEqual(
            (blocked["runtime"]["active_child_id"],
             blocked["runtime"]["motion_owner"]),
            (campaign.start_transition_owner["run_id"],
             campaign.start_transition_owner),
        )
        for op in ("recover_home", "new_campaign_same_settings"):
            with self.subTest(op=op), self.assertRaisesRegex(
                ContractError, "OPERATOR_INTENT_OP",
            ):
                consume(op, {}, f"uncertain-start-{op}")
        recovery.assert_not_called()
        self.assertEqual(len(campaigns), 1)
        self.assertFalse(campaign.closed)

        campaign.start_transition_owner = None
        released = application.bridge_core.snapshot()["projection"]
        self.assertEqual(
            released["available_ops"],
            ["recover_home", "new_campaign_same_settings"],
        )

    def test_camera_recovery_preserves_seed(self):
        ready = self.application.prepare_environment_call()
        device_id = self.selection["camera_device_id"]
        camera_setup = {
            "status": "READY", "reason": None, "profile_label": "wrist-camera",
            "devices": [{
                "logical_id": device_id, "label": "wrist", "status": "CONNECTED",
            }],
            "bindings": {device_id: "WRIST"},
            "required_roles": ["WRIST"], "available_roles": ["WRIST"],
        }
        campaigns = []

        def factory(campaign_id, _selection, draft):
            campaign = StubCampaign(campaign_id, draft, "TEST_ONLY")
            campaigns.append(campaign)
            return campaign

        application = CollectionOperatorApplication(
            session_id="camera-seed-recovery-r001",
            operator_label="local-operator",
            catalog=self.catalog,
            initial_selection=self.selection,
            projector=projection,
            environment_call=lambda: copy.deepcopy(ready),
            prepare_environment_call=lambda: copy.deepcopy(ready),
            campaign_factory=factory,
            camera_setup=camera_setup,
            camera_bindings_call=lambda _bindings: self.fail("bindings changed"),
            camera_refresh_call=lambda: {
                "camera_setup": copy.deepcopy(camera_setup),
                "catalog": copy.deepcopy(self.catalog),
                "selection": copy.deepcopy(self.selection),
                "environment": copy.deepcopy(ready),
            },
            initial_environment=ready,
        )
        self.addCleanup(application.close)

        def consume(op, payload, suffix):
            before = application.bridge_core.snapshot()
            return application.bridge_core.consume(
                intent(before, op, payload, suffix),
            )["result"]

        authored = application.bridge_core.snapshot()["projection"]
        consume("update_draft", {
            "draft_id": authored["draft"]["draft_id"], "normalized_seed": 211,
        }, "camera-seed")
        authored = application.bridge_core.snapshot()["projection"]
        compiled = consume("compile_draft", {
            "draft_id": authored["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "camera-seed-compile")
        consume("authorize_campaign", {
            "draft_id": authored["draft"]["draft_id"],
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        }, "camera-seed-authorize")
        campaigns[0].state = "BLOCKED"
        campaigns[0].reason_codes = ["CAMERA_STREAM_FAILED"]

        blocked = application.bridge_core.snapshot()["projection"]
        self.assertIn("recover_camera_setup", blocked["available_ops"])
        consume("recover_camera_setup", {}, "camera-seed-recover")
        recovered = application.bridge_core.snapshot()["projection"]
        self.assertEqual(recovered["draft"]["normalized_seed"], 211)
        self.assertTrue(campaigns[0].closed)

    def test_blocked_recovery_refreshes_environment_before_fresh_authoring(self):
        self.consume("prepare_environment", {}, "prepare-refresh-environment")
        authoring = self.application.bridge_core.snapshot()["projection"]
        compiled = self.consume("compile_draft", {
            "draft_id": authoring["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "compile-refresh-environment")
        self.consume("authorize_campaign", {
            "draft_id": authoring["draft"]["draft_id"],
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        }, "authorize-refresh-environment")
        self.campaigns[0].state = "BLOCKED"
        self.environment["state"] = "SETUP_REQUIRED"
        self.environment["components"]["controller"] = {
            "state": "MISSING", "owner": None, "reason": "NOT_RUNNING",
        }

        result = self.consume(
            "new_campaign_same_settings", {}, "recover-refresh-environment",
        )

        self.assertEqual(result["outcome"], "ENVIRONMENT")
        recovered = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(recovered["workflow_state"], "ENVIRONMENT")
        self.assertEqual(recovered["available_ops"], ["prepare_environment"])
        self.assertEqual(recovered["environment"], self.environment)
        self.assertTrue(self.campaigns[0].closed)

    def test_blocked_recovery_query_failure_keeps_campaign_blocked(self):
        self.consume("prepare_environment", {}, "prepare-refresh-failure")
        authoring = self.application.bridge_core.snapshot()["projection"]
        compiled = self.consume("compile_draft", {
            "draft_id": authoring["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "compile-refresh-failure")
        self.consume("authorize_campaign", {
            "draft_id": authoring["draft"]["draft_id"],
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        }, "authorize-refresh-failure")
        campaign = self.campaigns[0]
        campaign.state = "BLOCKED"
        self.application.environment_call = mock.Mock(
            side_effect=ContractError("OPERATOR_ENVIRONMENT_QUERY_FAILED"),
        )

        with self.assertRaisesRegex(
            ContractError, "OPERATOR_ENVIRONMENT_QUERY_FAILED",
        ):
            self.consume(
                "new_campaign_same_settings", {}, "recover-refresh-failure",
            )

        projection = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(projection["workflow_state"], "BLOCKED")
        self.assertEqual(projection["available_ops"], ["new_campaign_same_settings"])
        self.assertIs(self.application._campaign, campaign)
        self.assertFalse(campaign.closed)

    def test_blocked_candidate_is_reviewed_before_fresh_campaign_is_offered(self):
        self.consume("prepare_environment", {}, "prepare-blocked-review")
        authored = self.application.bridge_core.snapshot()["projection"]
        compiled = self.consume("compile_draft", {
            "draft_id": authored["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "compile-blocked-review")
        self.consume("authorize_campaign", {
            "draft_id": authored["draft"]["draft_id"],
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        }, "authorize-blocked-review")
        campaign = self.campaigns[0]
        campaign.state = "BLOCKED"
        campaign.candidate_pending = True

        blocked = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(blocked["available_ops"], ["review_candidate"])
        self.consume("review_candidate", {
            "review_binding_digest": canonical_digest("review-binding"),
            "choice": "FAIL", "reason": "TASK_GOAL",
        }, "review-blocked")

        recovered = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(recovered["available_ops"], ["new_campaign_same_settings"])

    def test_compiled_campaign_can_be_discarded_before_authorization(self):
        self.consume("prepare_environment", {}, "prepare-edit")
        before = self.application.bridge_core.snapshot()["projection"]
        self.consume("compile_draft", {
            "draft_id": before["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "compile-edit")
        review = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(
            review["available_ops"],
            ["edit_campaign_draft", "authorize_campaign"],
        )

        self.consume("edit_campaign_draft", {}, "edit-before-start")

        edited = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(edited["runtime"]["workflow_state"], "AUTHORING")
        self.assertNotEqual(edited["draft"]["draft_id"], before["draft"]["draft_id"])
        self.assertTrue(self.campaigns[0].closed)

    def test_named_start_pose_selection_projects_exact_direct_pairs(self):
        setup = {
            "profiles": [
                {"start_pose_id": "start-a", "display_name": "시작 A", "status": "AVAILABLE"},
                {"start_pose_id": "start-b", "display_name": "시작 B", "status": "AVAILABLE"},
            ],
            "selected_start_pose_ids": ["start-a", "start-b"],
        }
        captures = []

        def capture(display_name):
            captures.append(display_name)
            return {
                **setup,
                "profiles": [*setup["profiles"], {
                    "start_pose_id": "start-candidate",
                    "display_name": display_name,
                    "status": "CANDIDATE",
                }],
            }

        application = CollectionOperatorApplication(
            session_id="start-space-application-r001",
            operator_label="local-operator", catalog=self.catalog,
            initial_selection=self.selection,
            projector=projection,
            environment_call=lambda: copy.deepcopy(self.environment),
            prepare_environment_call=self.application.prepare_environment_call,
            campaign_factory=lambda *_args: self.fail("campaign was not requested"),
            start_pose_setup=setup, start_pose_capture_call=capture,
        )
        self.addCleanup(application.close)
        current = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(
            current, "prepare_environment", {}, "start-space-prepare",
        ))
        ready = application.bridge_core.snapshot()
        projected = ready["projection"]
        self.assertEqual(
            projected["state_space_summary"]
            ["eligible_start_condition_pair_count"],
            2 * projected["state_space_summary"]
            ["catalog_eligible_condition_count"],
        )
        application.bridge_core.consume(intent(ready, "update_draft", {
            "draft_id": projected["draft"]["draft_id"],
            "authoring_mode": "DIRECT_EDIT",
        }, "start-space-direct"))
        direct = application.bridge_core.snapshot()
        pairs = direct["projection"]["draft"]["direct_pairs"]
        self.assertEqual(len(pairs), 3)
        self.assertEqual(
            [pair["start_pose_id"] for pair in pairs],
            project_balanced_start_pose_ids(
                ["start-a", "start-b"], 3,
                normalized_seed=derive_domain_seed(
                    direct["projection"]["draft"]["normalized_seed"],
                    "start_pose",
                ),
            ),
        )
        application.bridge_core.consume(intent(direct, "capture_start_pose", {
            "display_name": "새 준비 자세",
        }, "start-space-capture"))
        captured_view = application.bridge_core.snapshot()
        captured = captured_view["projection"]
        self.assertEqual(captures, ["새 준비 자세"])
        self.assertEqual(captured["start_pose_setup"]["profiles"][-1]["status"], "CANDIDATE")
        application.bridge_core.consume(intent(captured_view, "update_start_pose_selection", {
            "selected_start_pose_ids": ["start-b"],
        }, "start-space-select"))
        selected = application.bridge_core.snapshot()
        self.assertEqual(
            {pair["start_pose_id"] for pair in selected["projection"]["draft"]["direct_pairs"]},
            {"start-b"},
        )

    def test_profiled_repeat_round_trip_allows_only_configured_run_length(self):
        catalog = load_operator_catalog(
            ROOT, device_ids=["repeat-camera-a", "repeat-camera-b"],
        )
        combination = next(
            item for item in catalog["combinations"]
            if item["task_id"] == "pickup_e2e"
            and item["object_id"] == "wood-cube-24mm-r001"
            and item["variant_id"] == "TWO_STAGE_ALIGN_V2"
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        selection = {
            "schema_version": "data_factory.operator_selection.v1",
            "combination_digest": combination["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            **{
                field: combination[field]
                for field in (
                    "workspace_id", "frame_id", "task_id", "object_id",
                    "grasp_id", "cell_id", "start_pose_id", "motion_id",
                    "variant_id", "camera_profile_id", "camera_device_id",
                )
            },
            "policy_id": "DETERMINISTIC_SPREAD",
        }
        setup = {
            "profiles": [{
                "start_pose_id": selection["start_pose_id"],
                "display_name": "시작 A",
                "status": "AVAILABLE",
            }],
            "selected_start_pose_ids": [selection["start_pose_id"]],
        }
        application = CollectionOperatorApplication(
            session_id="profiled-repeat-r001", operator_label="local-operator",
            catalog=catalog, initial_selection=selection, projector=projection,
            environment_call=lambda: copy.deepcopy(self.environment),
            prepare_environment_call=self.application.prepare_environment_call,
            campaign_factory=lambda *_args: self.fail(
                "campaign was not requested",
            ),
            start_pose_setup=setup,
            start_pose_capture_call=lambda _name: copy.deepcopy(setup),
        )
        self.addCleanup(application.close)
        current = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(
            current, "prepare_environment", {}, "profiled-repeat-prepare",
        ))
        ready = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(ready, "update_draft", {
            "draft_id": ready["projection"]["draft"]["draft_id"],
            "requested_count": 6,
        }, "profiled-repeat-count"))
        counted = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(counted, "update_draft", {
            "draft_id": counted["projection"]["draft"]["draft_id"],
            "repeat": 2,
        }, "profiled-repeat-limit"))
        repeated = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(repeated, "update_draft", {
            "draft_id": repeated["projection"]["draft"]["draft_id"],
            "authoring_mode": "DIRECT_EDIT",
        }, "profiled-repeat-direct"))
        projected = application.bridge_core.snapshot()["projection"]["draft"]
        pairs = projected["direct_pairs"]
        self.assertEqual((len(pairs), projected["repeat"]), (6, 2))
        self.assertTrue(any(
            all(left[field] == right[field] for field in (
                "place_id", "yaw_deg", "x_mm", "y_mm",
            ))
            for left, right in zip(pairs, pairs[1:])
        ))
        self.assertTrue(projected["draft_ready"])
        application.draft["direct_pairs"][2].update({
            field: application.draft["direct_pairs"][0][field]
            for field in ("place_id", "yaw_deg", "x_mm", "y_mm")
        })
        self.assertFalse(application._direct_draft_ready())

    def test_pick_place_keeps_robot_starts_separate_from_n_plus_one_object_nodes(self):
        setup = {
            "profiles": [
                {"start_pose_id": "start-a", "display_name": "시작 A", "status": "AVAILABLE"},
                {"start_pose_id": "start-b", "display_name": "시작 B", "status": "AVAILABLE"},
            ],
            "selected_start_pose_ids": ["start-a", "start-b"],
        }
        application = CollectionOperatorApplication(
            session_id="pick-place-space-application-r001",
            operator_label="local-operator", catalog=self.catalog,
            initial_selection=self.selection,
            projector=projection,
            environment_call=lambda: copy.deepcopy(self.environment),
            prepare_environment_call=self.application.prepare_environment_call,
            campaign_factory=lambda *_args: self.fail("campaign was not requested"),
            start_pose_setup=setup,
            start_pose_capture_call=lambda _name: copy.deepcopy(setup),
        )
        self.addCleanup(application.close)
        current = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(
            current, "prepare_environment", {}, "pick-place-space-prepare",
        ))
        ready = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(ready, "update_draft", {
            "draft_id": ready["projection"]["draft"]["draft_id"],
            "selection": {"task": "pick_place"},
        }, "pick-place-space-task"))
        selected = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(selected, "update_draft", {
            "draft_id": selected["projection"]["draft"]["draft_id"],
            "authoring_mode": "DIRECT_EDIT",
        }, "pick-place-space-direct"))
        draft = application.bridge_core.snapshot()["projection"]["draft"]
        pairs = draft["direct_pairs"]
        self.assertEqual((draft["requested_count"], len(pairs)), (3, 4))
        self.assertEqual(
            [pair["start_pose_id"] for pair in pairs],
            [
                *project_balanced_start_pose_ids(
                    ["start-a", "start-b"], 3,
                    normalized_seed=draft["normalized_seed"],
                ),
                None,
            ],
        )
        self.assertEqual(
            {key: pairs[0][key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")},
            draft["current_object_pose"],
        )
        self.assertTrue(all(
            any(left[key] != right[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm"))
            for left, right in zip(pairs, pairs[1:])
        ))
        self.assertTrue(draft["draft_ready"])

    def test_direct_pick_place_blocks_a_release_unsafe_at_the_recorded_yaw(self):
        combination = next(
            item for item in self.catalog["combinations"]
            if item["execution"]["TEST_COLLECTION"]["executable"]
            and item["task_id"] == "pick_place"
            and item["workspace_id"] == "PLACE_B"
            and item["object_id"] == "wood-cube-24mm-r001"
        )
        selection = {
            "schema_version": "data_factory.operator_selection.v1",
            "combination_digest": combination["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            **{
                field: combination[field]
                for field in (
                    "workspace_id", "frame_id", "task_id", "object_id",
                    "grasp_id", "cell_id", "start_pose_id", "motion_id",
                    "variant_id", "camera_profile_id", "camera_device_id",
                )
            },
            "policy_id": "DETERMINISTIC_SPREAD",
        }
        setup = {
            "profiles": [{
                "start_pose_id": selection["start_pose_id"],
                "display_name": "시작 A", "status": "AVAILABLE",
            }],
            "selected_start_pose_ids": [selection["start_pose_id"]],
        }
        application = CollectionOperatorApplication(
            session_id="unsafe-direct-transition-r001",
            operator_label="local-operator", catalog=self.catalog,
            initial_selection=selection, projector=projection,
            effect_scope="PHYSICAL",
            environment_call=lambda: copy.deepcopy(self.environment),
            prepare_environment_call=self.application.prepare_environment_call,
            campaign_factory=lambda *_args: self.fail(
                "unsafe draft must not reach campaign construction",
            ),
            start_pose_setup=setup,
            start_pose_capture_call=lambda _name: copy.deepcopy(setup),
        )
        self.addCleanup(application.close)
        current = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(
            current, "prepare_environment", {}, "unsafe-direct-prepare",
        ))
        ready = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(ready, "update_draft", {
            "draft_id": ready["projection"]["draft"]["draft_id"],
            "current_object_pose": {
                "place_id": "PLACE_B", "yaw_deg": 44,
                "x_mm": 0, "y_mm": 0,
            },
        }, "unsafe-direct-source"))
        sourced = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(sourced, "update_draft", {
            "draft_id": sourced["projection"]["draft"]["draft_id"],
            "authoring_mode": "DIRECT_EDIT",
        }, "unsafe-direct-mode"))
        application.draft["direct_pairs"][1].update({
            "place_id": "PLACE_A", "yaw_deg": 0,
            "x_mm": 0, "y_mm": 68,
        })

        projected = application.bridge_core.snapshot()["projection"]
        self.assertFalse(projected["draft"]["draft_ready"])
        self.assertEqual(
            projected["draft"]["draft_reason"],
            "DIRECT_YAW_TRANSITION_UNSAFE",
        )
        self.assertNotIn("compile_draft", projected["available_ops"])

    def test_assisted_start_pose_ensemble_is_seeded_balanced_and_stable(self):
        starts = ["start-c", "start-a", "start-b"]
        first = project_balanced_start_pose_ids(
            starts, 8, normalized_seed=1,
        )
        self.assertEqual(
            first,
            [
                "start-b", "start-c", "start-a", "start-b",
                "start-c", "start-a", "start-b", "start-c",
            ],
        )
        self.assertEqual(
            first,
            project_balanced_start_pose_ids(starts, 8, normalized_seed=1),
        )
        counts = [first.count(identifier) for identifier in sorted(starts)]
        self.assertLessEqual(max(counts) - min(counts), 1)
        self.assertEqual(
            [
                project_balanced_start_pose_ids(starts, 1, normalized_seed=seed)[0]
                for seed in range(3)
            ],
            ["start-a", "start-b", "start-c"],
        )

    def test_workspace_registration_is_a_separate_preview_first_product_axis(self):
        managers = []
        snapshots = []
        reloads = []

        def manager_factory(display_name):
            manager = StubWorkspaceManager(len(managers) + 1, display_name)
            managers.append(manager)
            return manager

        def snapshot():
            value = {"raw_joint_snapshot": len(snapshots) + 1}
            snapshots.append(value)
            return value

        def preview(manager, measurements):
            self.assertEqual(
                measurements,
                {"source_scale_bar_mm": 100.0, "final_scale_bar_mm": 100.0},
            )
            if set(manager.captures) != set(manager.LABELS):
                raise ContractError("STUB_WORKSPACE_INCOMPLETE")
            manager.preview = {
                "status": (
                    "CANDIDATE_OUT_OF_TOLERANCE"
                    if getattr(manager, "reject", False)
                    else "CANDIDATE_WITHIN_TOLERANCE"
                ),
                "preview_digest": canonical_digest({
                    "workspace": manager.sequence,
                    "measurements": measurements,
                }),
                "execution_authorized": False,
                "training_approved": False,
            }
            return copy.deepcopy(manager.preview)

        def reload_catalog():
            reloads.append(True)
            return copy.deepcopy(self.catalog)

        application = CollectionOperatorApplication(
            session_id="workspace-product-application-r001",
            operator_label="local-operator",
            catalog=self.catalog,
            initial_selection=self.selection,
            projector=projection,
            environment_call=lambda: copy.deepcopy(self.environment),
            prepare_environment_call=lambda: self.application.prepare_environment_call(),
            campaign_factory=lambda *_args: self.fail("campaign compile was not requested"),
            workspace_manager_factory=manager_factory,
            workspace_snapshot_call=snapshot,
            workspace_preview_call=preview,
            catalog_reload_call=reload_catalog,
        )
        self.addCleanup(application.close)

        initial = application.bridge_core.snapshot()
        self.assertNotIn("capture_workspace_point", initial["projection"]["available_ops"])
        application.bridge_core.consume(intent(
            initial, "prepare_environment", {}, "workspace-prepare",
        ))
        ready = application.bridge_core.snapshot()
        self.assertIn("new_workspace_registration", ready["projection"]["available_ops"])
        application.bridge_core.consume(intent(
            ready, "new_workspace_registration", {"display_name": "놓기 영역 B"},
            "workspace-begin",
        ))

        for label in StubWorkspaceManager.LABELS:
            current = application.bridge_core.snapshot()
            self.assertIn("capture_workspace_point", current["projection"]["available_ops"])
            application.bridge_core.consume(intent(
                current, "capture_workspace_point", {"label": label},
                f"workspace-capture-{label.lower()}",
            ))

        captured = application.bridge_core.snapshot()
        workspace = captured["projection"]["workspace_registration"]
        self.assertEqual(workspace["captures"], {
            "CENTER": True, "X_REF": True, "Y_CHECK": True,
        })
        self.assertNotIn("raw_joint_snapshot", str(workspace))
        self.assertIn("preview_workspace", captured["projection"]["available_ops"])
        application.bridge_core.consume(intent(captured, "preview_workspace", {
            "source_scale_bar_mm": 100.0,
            "final_scale_bar_mm": 100.0,
        }, "workspace-preview"))

        previewed = application.bridge_core.snapshot()
        preview_digest = previewed["projection"]["workspace_registration"][
            "preview"
        ]["preview_digest"]
        self.assertEqual(
            previewed["projection"]["available_ops"],
            ["update_draft", "compile_draft", "save_workspace"],
        )
        application.bridge_core.consume(intent(previewed, "save_workspace", {
            "preview_digest": preview_digest,
        }, "workspace-save"))

        saved = application.bridge_core.snapshot()
        self.assertEqual(len(reloads), 1)
        self.assertEqual(saved["projection"]["selection"], self.selection)
        self.assertEqual(len(saved["projection"]["workspace_registration"]["history"]), 1)
        self.assertIn("new_workspace_registration", saved["projection"]["available_ops"])
        application.bridge_core.consume(intent(
            saved, "new_workspace_registration", {"display_name": "놓기 영역 C"},
            "workspace-new",
        ))
        restarted = application.bridge_core.snapshot()["projection"]
        self.assertEqual(len(managers), 2)
        self.assertTrue(all(
            value is False
            for value in restarted["workspace_registration"]["captures"].values()
        ))
        self.assertEqual(len(restarted["workspace_registration"]["history"]), 1)

        managers[-1].reject = True
        for label in StubWorkspaceManager.LABELS:
            current = application.bridge_core.snapshot()
            application.bridge_core.consume(intent(
                current, "capture_workspace_point", {"label": label},
                f"workspace-retry-capture-{label.lower()}",
            ))
        captured = application.bridge_core.snapshot()
        application.bridge_core.consume(intent(captured, "preview_workspace", {
            "source_scale_bar_mm": 100.0,
            "final_scale_bar_mm": 100.0,
        }, "workspace-rejected-preview"))
        rejected = application.bridge_core.snapshot()
        rejected_preview = rejected["projection"]["workspace_registration"]["preview"]
        self.assertEqual(
            rejected["projection"]["available_ops"],
            ["update_draft", "compile_draft", "discard_workspace_preview"],
        )
        application.bridge_core.consume(intent(
            rejected, "discard_workspace_preview",
            {"preview_digest": rejected_preview["preview_digest"]},
            "workspace-discard-preview",
        ))
        retry = application.bridge_core.snapshot()["projection"]
        self.assertIsNone(retry["workspace_registration"]["preview"])
        self.assertIn("capture_workspace_point", retry["available_ops"])
        self.assertIn("preview_workspace", retry["available_ops"])


if __name__ == "__main__":
    unittest.main()
