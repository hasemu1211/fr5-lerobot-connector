import copy
import http.client
import json
import shutil
import time
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from tools.a4_place_yaw.region_layout import (
    make_red_blue_region_layout,
    workspace_region,
)
from tools.data_factory import run_job
from tools.data_factory.campaign_authoring import compile_collection_campaign
from tools.data_factory.campaign_operator import CampaignOperator, SIDE_EFFECT_COUNTERS
from tools.data_factory.cell_state import CellStateStore
from tools.data_factory.campaign_authorization import (
    build_campaign_authorization,
    build_campaign_envelope,
    validate_runtime_campaign_scope,
)
from tools.data_factory.experiment_manifest import compile_fr5_hypothesis
from tools.data_factory.one_job import OneJob, TEST_ONLY_READINESS_CONTRACT
from tools.data_factory.motion.object_reposition import (
    build_object_reposition_binding,
)
from tools.data_factory.operator.workflow.intents import (
    CandidateReviewPort,
    OperatorIntentCore,
)
from tools.data_factory.operator.web.bridge import LoopbackBridge
from tools.data_factory.operator.catalog import (
    load_operator_catalog,
    project_assisted_poses,
    project_balanced_start_pose_ids,
)
from tools.data_factory.operator.cli import main as operator_console_main
from tools.data_factory.operator.composition import (
    _campaign_authorization_ttl,
    _domain_seed,
    build_physical_operator_application,
    build_physical_operator_console,
    build_physical_runtime,
)
from tools.data_factory.operator.setup.physical import (
    capture_gripper_setup_readback,
    capture_home_snapshot,
    normalize_gripper_after_operator_ready,
    passive_physical_gate,
)
from tools.data_factory.operator.workflow.campaign import (
    OperatorConsole,
    _derive_test_only_gripper_program,
    _campaign_camera_warmup,
    _validate_successful_object_reposition_result,
    build_physical_test_contract,
)
from tools.data_factory.operator.setup.contracts import (
    NO_AUTHORITY,
    build_test_only_root_binding,
    build_test_only_start_binding,
)
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict

from .fixtures import (
    SCENE,
    campaign_draft,
    payload,
    physical_contract,
    qualification_inputs,
    pose_snapshot,
    runtime_motion,
    runtime_validated,
    single_hypothesis,
    single_qualification_inputs,
)


NOW = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
EXPIRES = "2026-08-26T04:00:00Z"


def envelope(view: dict, op: str, payload: dict, intent_id: str) -> dict:
    return {
        "schema_version": "data_factory.operator_intent.v1",
        "intent_id": intent_id,
        "session_id": view["session_id"],
        "view_revision": view["revision"],
        "view_digest": view["view_digest"],
        "op": op,
        "payload": payload,
    }


def successful_reposition_result(
    binding: Mapping[str, Any], scene_digest: str,
) -> dict[str, Any]:
    plan_digest = canonical_digest([binding["continuation_run_id"], "plan"])
    value = {
        "schema_version": "data_factory.object_reposition_result.v2",
        "status": "PASS", "code": "PASS",
        "parent_run_id": binding["parent_run_id"],
        "continuation_run_id": binding["continuation_run_id"],
        "next_run_id": binding["next_run_id"],
        "object_reposition_binding_digest": binding["binding_digest"],
        "plan_digest": plan_digest,
        "resolved_job_digest": canonical_digest("reposition-resolved-job"),
        "scene_state_digest": scene_digest,
        "preapproval_scope_digest": canonical_digest("reposition-preapproval"),
        "plan_artifact_digest": canonical_digest("reposition-plan-artifact"),
        "execution_response": {
            "ok": True, "code": "COMPLETE", "state": "COMPLETED",
            "run_id": binding["continuation_run_id"],
            "plan_digest": plan_digest,
            "data": {
                "scene_transition": {"scene_state_digest": scene_digest},
            },
        },
    }
    value["result_digest"] = canonical_digest(value)
    return value


def production_hypothesis() -> dict[str, Any]:
    fixed, report, resolvers, _bases, _poses, catalog = qualification_inputs()
    digest_maps = []
    for field in (
        "base_condition_qualifications", "robot_start_pose_qualifications",
    ):
        replacements = {}
        for item in catalog[field]:
            old = item["qualification_digest"]
            item["source"] = "QUALIFICATION_ARTIFACT"
            item["qualification_digest"] = canonical_digest({
                key: value for key, value in item.items()
                if key != "qualification_digest"
            })
            replacements[old] = item["qualification_digest"]
        digest_maps.append(replacements)
    for pair in catalog["allowed_pairs"]:
        pair["base_condition_qualification_digest"] = digest_maps[0][
            pair["base_condition_qualification_digest"]
        ]
        pair["robot_start_pose_qualification_digest"] = digest_maps[1][
            pair["robot_start_pose_qualification_digest"]
        ]
    catalog["allowed_pairs"].sort(key=lambda pair: (
        pair["base_condition_qualification_digest"],
        pair["robot_start_pose_qualification_digest"],
    ))
    catalog["source"] = "QUALIFICATION_ARTIFACT"
    catalog["catalog_digest"] = canonical_digest({
        key: value for key, value in catalog.items()
        if key != "catalog_digest"
    })
    return compile_fr5_hypothesis(
        fixed_contract=fixed, coverage_report=report,
        resolver_results=resolvers, qualification_catalog=catalog,
    )


class Harness:
    def __init__(
        self, root: str, *, checkpoint_kind: str = "SEMANTIC_VERDICT",
        terminal_response: dict | None = None, preplan_checkpoint: bool = False,
        setup_request: dict | None = None, setup_resolution_call=None,
    ):
        self.root = root
        self.checkpoint_kind = checkpoint_kind
        self.terminal_response = terminal_response
        self.preplan_checkpoint = preplan_checkpoint
        self.setup_request = setup_request
        self.setup_resolution_call = setup_resolution_call
        self.hypothesis = single_hypothesis()
        self.source_draft = campaign_draft(self.hypothesis, count=1)
        self.scene_digest = self.hypothesis["fixed_contract"]["scene_digest"]
        self.operator_counters = {name: 0 for name in SIDE_EFFECT_COUNTERS}
        self.forbidden = {
            name: 0 for name in (
                "robot", "gripper", "camera", "production_recorder", "dataset",
                "production_run_state", "candidate", "inventory", "coverage", "training",
            )
        }
        self.children = []
        self.operator = None

    def scene(self, _run_id: str) -> dict:
        value = {
            "schema_version": "data_factory.scene_freshness_evidence.v1",
            "scene_digest": self.scene_digest,
            "observed_at": NOW.isoformat().replace("+00:00", "Z"),
        }
        value["evidence_digest"] = canonical_digest(value)
        return value

    def fresh_one_job(self) -> OneJob:
        self.operator_counters["physical_factory"] += 1

        def forbidden(name):
            def call(_request):
                self.forbidden[name] += 1
                return {}
            return call

        child = OneJob(
            forbidden("production_recorder"), forbidden("robot"), clock=lambda: NOW,
            readiness_contract=TEST_ONLY_READINESS_CONTRACT,
        )
        self.children.append(child)
        return child

    def start_binding(
        self, _run_id: str, slot: Mapping[str, Any], _cancel_event,
    ) -> dict:
        manifest = self.operator.manifest
        pose = next(
            item for item in self.hypothesis["robot_start_poses"]
            if item["robot_start_pose_id"] == slot["robot_start_pose_id"]
        )
        target = [pose["target_rad"][joint] for joint in pose["joint_order"]]
        value = {
            "scope": "MOTION_Q_SAFE_START", "data_disposition": "TEST_ONLY",
            "manifest_digest": manifest["manifest_digest"],
            "slot_digest": canonical_digest(slot),
            "robot_start_pose_id": pose["robot_start_pose_id"],
            "robot_start_pose_qualification_digest": pose["qualification_digest"],
            "motion_qualification_id": "motion-q-safe-test",
            "motion_qualification_digest": canonical_digest("motion-q-safe-test"),
            "home_candidate_digest": pose["home_candidate_digest"],
            "joint_order": copy.deepcopy(pose["joint_order"]),
            "target_rad": target, "current_rad": copy.deepcopy(target),
            "tolerance_rad": 0.01, "max_snapshot_age_s": 0.1,
            "snapshot_digest": canonical_digest("fresh-current-snapshot"),
            "status": "BOUND_TEST_ONLY", "authority": copy.deepcopy(NO_AUTHORITY),
        }
        value["binding_digest"] = canonical_digest(value)
        return value

    def operator_factory(self, episode_call) -> CampaignOperator:
        self.operator = CampaignOperator(
            session_id="physical-campaign-r001", lifecycle_owner="local-operator",
            operator_label="local-operator",
            workspace={"workspace_id": "test-workspace", "identity": "TEST_ONLY"},
            hypothesis=self.hypothesis, draft=self.source_draft,
            effect_scope="PHYSICAL", lifecycle_action="LIVE_COLLECT",
            data_disposition="TEST_ONLY",
            subsystems={
                "planner": {"readiness": "READY", "capability": "PLAN", "reason": "INJECTED_TEST"},
                "recorder": {"readiness": "READY", "capability": "TEST_ONLY", "reason": "INJECTED_TEST"},
            },
            expires_at=EXPIRES, initial_scene_digest=self.scene_digest,
            scene_evidence_call=self.scene,
            side_effect_counter_call=lambda: copy.deepcopy(self.operator_counters),
            fake_lifecycle_factory=self.fresh_one_job,
            physical_activation_gate=lambda: True,
            physical_lifecycle_factory=self.fresh_one_job,
            physical_live_call=episode_call,
            physical_root_binding_call=lambda run_id: build_test_only_root_binding(
                self.root, session_id="physical-campaign-r001", run_id=run_id,
            ),
            physical_start_binding_call=self.start_binding,
            repository_root=self.root, clock=lambda: NOW,
        )
        return self.operator

    def episode(
        self, intent, lifecycle, cancel_event, episode_context,
        decision_provider, checkpoint_provider,
    ):
        if self.terminal_response is not None:
            lifecycle.state = "ABORTED"
            raise ContractError(self.terminal_response["code"])
        plan_digest = canonical_digest(["exact-plan", intent["intent_digest"]])
        if self.preplan_checkpoint:
            site = checkpoint_provider({
                "schema_version": "data_factory.operator_checkpoint_request.v1",
                "kind": "PHYSICAL_SCENE_CONFIRMATION", "run_id": intent["run_id"],
                "plan_digest": plan_digest,
                "prompt": "Confirm place1 cube, empty gripper, clear cell, and E-stop monitoring",
                "choices": ["READY", "CANCEL"],
                "evidence": {
                    "checklist": {"place_alias": "place1", "place_id": "PLACE_A"},
                    "data_disposition": "TEST_ONLY",
                },
                "timeout_s": 2.0,
            })
            if site is None or site["choice"] != "READY":
                lifecycle.state = "ABORTED"
                raise ContractError("PAUSED_AWAITING_OPERATOR")
        decision = decision_provider({
            "schema_version": "data_factory.plan_decision_request.v1",
            "run_id": intent["run_id"], "plan_digest": plan_digest,
            "approval_scope": "HIL_NUMERIC_PROXY",
            "decision_binding": {
                "schema_version": "data_factory.test_physical_plan.v1",
                "run_id": intent["run_id"], "plan_digest": plan_digest,
                "intent_digest": intent["intent_digest"],
                "data_disposition": "TEST_ONLY",
            },
            "timeout_s": 2.0,
        })
        if decision is None or decision["choice"] != "APPROVE" or cancel_event.is_set():
            lifecycle.state = "ABORTED"
            raise ContractError("TEST_PLAN_CANCELLED")
        choices = {
            "SEMANTIC_VERDICT": ["PASS", "FAIL"],
            "RELEASE_VERDICT": ["LANDED", "OFF_SLOT", "UNCERTAIN"],
            "SCENE_READY": ["SCENE_READY"],
        }[self.checkpoint_kind]
        checkpoint = checkpoint_provider({
            "schema_version": "data_factory.operator_checkpoint_request.v1",
            "kind": self.checkpoint_kind, "run_id": intent["run_id"],
            "plan_digest": plan_digest,
            "prompt": "Confirm the exact TEST_ONLY physical checkpoint",
            "choices": choices,
            "evidence": {
                "execution_evidence_digest": canonical_digest("execution"),
                "landing_and_final_scene_combined": self.checkpoint_kind == "RELEASE_VERDICT",
            },
            "timeout_s": 2.0,
        })
        expected = {
            "SEMANTIC_VERDICT": "PASS",
            "RELEASE_VERDICT": "LANDED",
            "SCENE_READY": "SCENE_READY",
        }[self.checkpoint_kind]
        if checkpoint is None or checkpoint["choice"] != expected or cancel_event.is_set():
            lifecycle.state = "ABORTED"
            raise ContractError("TEST_CHECKPOINT_CANCELLED")
        lifecycle.state = "COMPLETE"
        technical = {
            "schema_version": "data_factory.seed_technical_result.v1",
            "intent_digest": intent["intent_digest"], "run_id": intent["run_id"],
            "manifest_digest": intent["manifest_digest"],
            "slot_id": intent["slot"]["slot_id"], "status": "PASS",
            "technical_result_digest": canonical_digest("technical-pass"),
            "post_scene_digest": canonical_digest("post-scene"),
            "observed_at": NOW.isoformat().replace("+00:00", "Z"),
        }
        technical["evidence_digest"] = canonical_digest(technical)
        self.scene_digest = technical["post_scene_digest"]
        return {
            "result": {"technical_evidence": technical, "human_semantic": "NOT_MEASURED"},
            "technical_evidence": technical,
        }

    def projection(self) -> dict:
        return {
            "setup": {
                "host_status": "READY", "operator_label": "local-operator",
                "subsystems": [
                    {"label": "host", "status": "READY", "detail": "foreground test"},
                    {"label": "robot", "status": "INJECTED", "detail": "hardware calls 0"},
                ],
            },
            "fixed_lane": {
                "workspace": {
                    "display_name": "Synthetic physical shape", "place_id": "place-r1",
                    "revision": "test-r001", "bounds": "one qualified cell",
                },
                "object_id": "object-r1", "grasp_id": "grasp-r1",
                "task": {"id": "pickup_e2e", "capability": "PHYSICAL_EXECUTABLE"},
                "motion": {"id": "DIRECT", "capability": "PHYSICAL_EXECUTABLE"},
                "start_pose_id": "start-1", "camera_role": "up · TEST_ONLY",
                "profile_id": "fr5-up-rgb-30hz-v1",
            },
            "draft": {
                "draft_id": self.source_draft["draft_id"], "revision": 0,
                "authoring_mode": "ASSISTED", "selector": "BALANCED_INITIAL",
                "selector_version": "v1", "budget": 1, "selected_count": 1,
                "blocked_count": 0, "estimated_minutes": 1,
                "split_summary": "TRAIN 1", "repeat_summary": "x1",
                "coverage_summary": "1/1 selected",
                "cells": [{
                    "cell_id": "one-cell", "x_mm": 10, "y_mm": 0, "yaw_deg": 0,
                    "split": "TRAIN", "repeat": 1, "coverage_count": 0,
                    "selection_state": "SELECTED", "eligibility_status": "ELIGIBLE",
                    "reason_codes": ["QUALIFIED_BASELINE"],
                }],
            },
            "capabilities": [{
                "label": "Task · pickup_e2e", "status": "PHYSICAL_EXECUTABLE",
                "reason_codes": ["INJECTED_TEST"],
            }],
            "workspace_wizard": {
                "capability": "NOT_AVAILABLE",
                "plane_reference": {
                    "id": "test-plane", "digest": canonical_digest("test-plane"),
                    "table_normal_base": [0.0, 0.0, 1.0],
                },
                "source_measurement_mm": None, "final_measurement_mm": None,
                "captures": {"CENTER": False, "X_REF": False, "Y_CHECK": False},
            },
            "effect_counts": copy.deepcopy(self.forbidden),
        }

    def console(
        self, *, episode_call=None, prepare_timeout_s=1.0,
        candidate_review_port=None, campaign_approval_once=False,
        object_reposition_bindings=None,
    ) -> OperatorConsole:
        def forbidden_review(*_args, **_kwargs):
            self.forbidden["candidate"] += 1
            raise AssertionError("TEST_ONLY must not review a production candidate")

        return OperatorConsole(
            session_id="physical-console-r001", operator_label="local-operator",
            campaign_operator_factory=self.operator_factory,
            episode_call=episode_call or self.episode, projection_call=self.projection,
            test_only_paths=self.root, clock=lambda: NOW,
            candidate_review_port=(
                candidate_review_port
                if candidate_review_port is not None
                else CandidateReviewPort(
                    operator_label="local-operator", review_call=forbidden_review,
                )
            ),
            terminal_response_call=lambda: self.terminal_response,
            gripper_setup_request=self.setup_request,
            gripper_setup_resolution_call=self.setup_resolution_call,
            object_reposition_bindings=object_reposition_bindings,
            campaign_approval_once=campaign_approval_once,
            prepare_timeout_s=prepare_timeout_s, close_timeout_s=1.0,
        )


class OperatorConsoleTests(unittest.TestCase):
    def test_domain_seeds_are_deterministic_and_slot_order_independent(self):
        slot = {
            "slot_id": "stable-slot-r001",
            "base_condition_digest": canonical_digest("condition"),
            "robot_start_pose_id": "start-r001",
            "split_group": "TRAIN",
            "repeat_index": 0,
        }
        slot_digest = canonical_digest(slot)
        first = _domain_seed(7, "trajectory", slot_digest)
        reordered = [
            _domain_seed(7, "trajectory", canonical_digest({
                key: value for key, value in {**slot, "order_index": order}.items()
                if key != "order_index"
            }))
            for order in (0, 9)
        ]

        self.assertEqual(first, _domain_seed(7, "trajectory", slot_digest))
        self.assertEqual(reordered, [first, first])
        self.assertEqual(len({
            _domain_seed(7, "spatial"),
            _domain_seed(7, "start_pose"),
            first,
        }), 3)
        self.assertNotIn(first, {7, 8})
        self.assertNotEqual(first, _domain_seed(8, "trajectory", slot_digest))
        next_slot_digest = canonical_digest({
            **slot, "slot_id": "stable-slot-r002", "repeat_index": 1,
        })
        self.assertTrue(
            {
                _domain_seed(7, "trajectory", slot_digest),
                _domain_seed(7, "trajectory", next_slot_digest),
            }.isdisjoint({
                _domain_seed(8, "trajectory", slot_digest),
                _domain_seed(8, "trajectory", next_slot_digest),
            })
        )

    def test_surface_reposition_evidence_is_fail_closed_and_sealed_in_history(self):
        object_profile = {"object_profile_id": "object-r1"}
        grasp_profile = {
            "grasp_profile_id": "grasp-r1", "object_profile_id": "object-r1",
        }
        binding = build_object_reposition_binding(
            parent_run_id="physical-console-r001",
            continuation_run_id="physical-console-r001-reposition",
            next_run_id="physical-console-r001-next",
            start_state="ON_SURFACE",
            source_pose={
                "place_id": "PLACE_A", "yaw_deg": 0,
                "x_mm": 0, "y_mm": 0,
            },
            target_pose={
                "place_id": "PLACE_A", "yaw_deg": 30,
                "x_mm": 0, "y_mm": 0,
            },
            object_profile=object_profile, grasp_profile=grasp_profile,
        )
        scene_digest = canonical_digest("repositioned-scene")
        result = successful_reposition_result(binding, scene_digest)
        for field, value, redigest in (
            ("status", "FAIL", True),
            ("object_reposition_binding_digest", canonical_digest("wrong"), True),
            ("result_digest", canonical_digest("wrong-result"), False),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ContractError, "OPERATOR_CONSOLE_REPOSITION_RESULT",
            ):
                tampered = copy.deepcopy(result)
                tampered[field] = value
                if redigest:
                    tampered["result_digest"] = canonical_digest({
                        key: item for key, item in tampered.items()
                        if key != "result_digest"
                    })
                _validate_successful_object_reposition_result(
                    tampered, binding, post_scene_digest=scene_digest,
                    code="OPERATOR_CONSOLE_REPOSITION_RESULT",
                )

        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            console = harness.console(object_reposition_bindings=[binding])
            self.addCleanup(console.close)
            console.campaign_operator.compile_draft({}, {})
            slot = console.campaign_operator.manifest["slots"][0]
            console._active_intent_projection = {
                "run_id": console.run_id, "order_index": 0,
                "slot_id": slot["slot_id"], "slot_digest": canonical_digest(slot),
            }
            console._workflow = "RUNNING"
            evidence = {
                "object_reposition_binding_digest": binding["binding_digest"],
                "object_reposition_run_id": binding["continuation_run_id"],
                "object_reposition_plan_digest": result["plan_digest"],
                "object_reposition_plan_artifact_digest": result[
                    "plan_artifact_digest"
                ],
                "object_reposition_collision_report_digest": canonical_digest(
                    "collision-report",
                ),
                "object_reposition_plan_only_no_motion_digest": canonical_digest(
                    "plan-only-no-motion",
                ),
            }
            for field in (
                "object_reposition_plan_artifact_digest",
                "object_reposition_collision_report_digest",
                "object_reposition_plan_only_no_motion_digest",
            ):
                with self.subTest(runtime_field=field), self.assertRaisesRegex(
                    ContractError, "OPERATOR_CONSOLE_RUNTIME_EVENT",
                ):
                    malformed = copy.deepcopy(evidence)
                    malformed[field] = "not-a-digest"
                    console.publish_runtime({
                        "code": "OBJECT_REPOSITION_PLANNED",
                        "run_id": console.run_id, "data": malformed,
                    })
            console.publish_runtime({
                "code": "VALIDATING", "run_id": console.run_id,
            })
            self.assertEqual(console.projection()["runtime"]["progress"], 90)
            console.publish_runtime({
                "code": "OBJECT_REPOSITION_PLANNED",
                "run_id": console.run_id, "data": evidence,
            })
            runtime = console.projection()["runtime"]
            self.assertEqual(runtime["progress"], 92)
            self.assertEqual(runtime["evidence"], evidence)
            self.assertEqual(runtime["motion"]["status"], "NOT_AUTHORIZED")
            console.publish_runtime({
                "code": "OBJECT_REPOSITION_EXECUTING",
                "run_id": console.run_id, "data": evidence,
            })
            runtime = console.projection()["runtime"]
            self.assertEqual(runtime["progress"], 93)
            self.assertEqual(
                runtime["motion"]["status"], "ACTIVE_POST_RECORDING",
            )
            self.assertEqual(runtime["recorder"]["status"], "COMMITTED")

            sealed = console._publish_outcome({
                "ok": True, "campaign": {"state": "COMPLETE"},
                "result": {
                    "technical_evidence": {
                        "status": "PASS", "post_scene_digest": scene_digest,
                    },
                    "human_semantic": "NOT_MEASURED",
                    "terminal_object_pose": copy.deepcopy(binding["target_pose"]),
                    "object_reposition": result,
                },
            })
            self.assertFalse(sealed)
            projection = console.projection()
            self.assertEqual(
                projection["terminal_object_pose"], binding["target_pose"],
            )
            self.assertEqual(
                projection["episode_result"]["object_reposition"], result,
            )
            self.assertEqual(
                projection["episode_history"][0]["object_reposition"], result,
            )

    def test_held_reposition_keeps_release_terminal_pose_without_a_result(self):
        release_pose = {
            "place_id": "PLACE_A", "yaw_deg": 15,
            "x_mm": 8, "y_mm": -4,
        }
        binding = build_object_reposition_binding(
            parent_run_id="physical-console-r001",
            continuation_run_id="physical-console-r001",
            next_run_id=None, start_state="HELD_OBJECT",
            source_pose={
                "place_id": "PLACE_A", "yaw_deg": 0,
                "x_mm": 0, "y_mm": 0,
            },
            target_pose=release_pose,
            object_profile={"object_profile_id": "object-r1"},
            grasp_profile={
                "grasp_profile_id": "grasp-r1", "object_profile_id": "object-r1",
            },
        )
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            console = harness.console(object_reposition_bindings=[binding])
            self.addCleanup(console.close)
            console.campaign_operator.compile_draft({}, {})
            slot = console.campaign_operator.manifest["slots"][0]
            console._active_intent_projection = {
                "run_id": console.run_id, "order_index": 0,
                "slot_id": slot["slot_id"], "slot_digest": canonical_digest(slot),
            }
            console._publish_outcome({
                "ok": True, "campaign": {"state": "COMPLETE"},
                "result": {
                    "technical_evidence": {"status": "PASS"},
                    "terminal_object_pose": release_pose,
                },
            })
            projected = console.projection()["episode_result"]
            self.assertEqual(projected["terminal_object_pose"], release_pose)
            self.assertNotIn("object_reposition", projected)

    def test_surface_preapproval_exactly_binds_campaign_and_next_endpoint(self):
        parent_run_id = "campaign-run-1"
        next_run_id = "campaign-run-2"
        binding = build_object_reposition_binding(
            parent_run_id=parent_run_id,
            continuation_run_id="campaign-run-1-reposition",
            next_run_id=next_run_id, start_state="ON_SURFACE",
            source_pose={
                "place_id": "PLACE_B", "yaw_deg": 0,
                "x_mm": 0, "y_mm": 0,
            },
            target_pose={
                "place_id": "PLACE_B", "yaw_deg": 30,
                "x_mm": 0, "y_mm": 0,
            },
            object_profile={"object_profile_id": "object-r1"},
            grasp_profile={
                "grasp_profile_id": "grasp-r1", "object_profile_id": "object-r1",
            },
        )
        current_slot = {
            "slot_id": "campaign-slot-1", "order_index": 0,
            "base_condition_digest": canonical_digest("current-base"),
        }
        next_base_digest = canonical_digest("next-base")
        next_resolved_digest = canonical_digest("next-resolved")
        next_slot = {
            "slot_id": "campaign-slot-2", "order_index": 1,
            "base_condition_digest": next_base_digest,
        }
        manifest_digest = canonical_digest("campaign-manifest")
        envelope_digest = canonical_digest("campaign-envelope")
        authorization_digest = canonical_digest("campaign-authorization")
        intent_digest = canonical_digest("current-intent")
        fixed_endpoint = {
            "workspace_id": "PLACE_B",
            "cell_calibration_id": "place-b-frame",
            "cell_calibration_digest": canonical_digest("place-b-calibration"),
            "motion_recipe_digest": canonical_digest("place-b-motion"),
        }
        destination = {
            "role": "DESTINATION", "workspace_id": "PLACE_B",
            "frame_id": "place-b-frame",
            "pose": copy.deepcopy(binding["source_pose"]),
            "sheet_digest": canonical_digest("place-b-sheet"),
            "family_digest": canonical_digest("a4-family"),
            "region_binding": {
                "layout_id": "layout-r1",
                "layout_digest": canonical_digest("layout"),
                "region_id": "BLUE",
                "physical_binding_status": "PREPARED_NOT_VERIFIED",
            },
        }
        operator = mock.Mock()
        operator.manifest = {
            "manifest_digest": manifest_digest,
            "slots": [current_slot, next_slot],
        }
        operator.hypothesis = {
            "fixed_contract": {
                "schema_version": "data_factory.fr5_fixed_contract.v2",
                "endpoint_bindings": [fixed_endpoint],
            },
            "base_conditions": [{
                "base_condition_digest": next_base_digest,
                "resolved_job_digest": next_resolved_digest,
            }],
        }
        console = object.__new__(OperatorConsole)
        console._object_reposition_bindings = [binding, None]
        console._active_intent_projection = {
            "run_id": parent_run_id, "intent_digest": intent_digest,
            "order_index": 0, "slot_id": current_slot["slot_id"],
            "slot_digest": canonical_digest(current_slot),
        }
        console._run_index = 0
        console.run_id = parent_run_id
        console._run_id_factory = lambda index: f"campaign-run-{index + 1}"
        console._task_bindings = [{"spatial_bindings": [destination]}, None]
        console._campaign_envelope = {
            "envelope_digest": envelope_digest,
            "manifest_digest": manifest_digest,
        }
        console.campaign_operator = operator
        episode_binding = {
            "manifest_digest": manifest_digest,
            "intent_digest": intent_digest,
            "run_id": parent_run_id,
            "slot_digest": canonical_digest(current_slot),
            "root_binding_digest": canonical_digest("root-binding"),
            "start_binding_digest": canonical_digest("start-binding"),
        }
        episode_binding["binding_digest"] = canonical_digest(episode_binding)
        request = {
            "run_id": parent_run_id,
            "plan_digest": canonical_digest("parent-plan"),
        }
        decision_binding = {
            "preapproval_evidence_digest": canonical_digest(
                "parent-preapproval-evidence",
            ),
        }
        endpoint = {
            "run_id": next_run_id,
            "workspace_id": destination["workspace_id"],
            "frame_id": destination["frame_id"],
            "source_pose": copy.deepcopy(binding["source_pose"]),
            "target_pose": copy.deepcopy(binding["target_pose"]),
            "sheet_digest": destination["sheet_digest"],
            "family_digest": destination["family_digest"],
            "region_binding": copy.deepcopy(destination["region_binding"]),
            "cell_calibration_digest": fixed_endpoint[
                "cell_calibration_digest"
            ],
            "motion_qualification_digest": fixed_endpoint[
                "motion_recipe_digest"
            ],
        }
        scope = {
            "schema_version": "data_factory.object_reposition_preapproval.v1",
            "parent_run_id": parent_run_id,
            "parent_plan_digest": request["plan_digest"],
            "parent_preapproval_evidence_digest": decision_binding[
                "preapproval_evidence_digest"
            ],
            "campaign_authorization_digest": authorization_digest,
            "campaign_envelope_digest": envelope_digest,
            "manifest_digest": manifest_digest,
            "intent_digest": intent_digest,
            "runtime_episode_binding_digest": episode_binding["binding_digest"],
            "current_slot_digest": canonical_digest(current_slot),
            "next_slot": copy.deepcopy(next_slot),
            "next_slot_digest": canonical_digest(next_slot),
            "next_slot_endpoint": endpoint,
            "next_slot_endpoint_digest": canonical_digest(endpoint),
            "continuation_run_id": binding["continuation_run_id"],
            "next_run_id": next_run_id,
            "object_reposition_binding_digest": binding["binding_digest"],
            "motion_payload_digest": canonical_digest("motion-payload"),
            "resolved_job_digest": next_resolved_digest,
            "motion_program_digest": canonical_digest("motion-program"),
        }
        scope["scope_digest"] = canonical_digest(scope)
        authorization = {"authorization_digest": authorization_digest}
        self.assertEqual(
            console._validated_object_reposition_preapproval(
                scope, request=request, decision_binding=decision_binding,
                episode_binding=episode_binding, authorization=authorization,
            ),
            scope,
        )

        def tampered(field, value):
            changed = copy.deepcopy(scope)
            changed[field] = value
            changed["scope_digest"] = canonical_digest({
                key: item for key, item in changed.items()
                if key != "scope_digest"
            })
            return changed

        wrong_endpoint = copy.deepcopy(endpoint)
        wrong_endpoint["sheet_digest"] = canonical_digest("wrong-sheet")
        endpoint_scope = copy.deepcopy(scope)
        endpoint_scope["next_slot_endpoint"] = wrong_endpoint
        endpoint_scope["next_slot_endpoint_digest"] = canonical_digest(
            wrong_endpoint,
        )
        endpoint_scope["scope_digest"] = canonical_digest({
            key: item for key, item in endpoint_scope.items()
            if key != "scope_digest"
        })
        cases = (
            tampered("parent_plan_digest", canonical_digest("wrong-plan")),
            tampered(
                "campaign_authorization_digest", canonical_digest("wrong-auth"),
            ),
            tampered(
                "object_reposition_binding_digest", canonical_digest("wrong-binding"),
            ),
            endpoint_scope,
        )
        for changed in cases:
            with self.assertRaisesRegex(
                ContractError, "OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH",
            ):
                console._validated_object_reposition_preapproval(
                    changed, request=request, decision_binding=decision_binding,
                    episode_binding=episode_binding, authorization=authorization,
                )

        console._object_reposition_bindings[0] = None
        self.assertIsNone(console._validated_object_reposition_preapproval(
            None, request=request, decision_binding=decision_binding,
            episode_binding=episode_binding, authorization=authorization,
        ))
        with self.assertRaisesRegex(
            ContractError, "OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH",
        ):
            console._validated_object_reposition_preapproval(
                scope, request=request, decision_binding=decision_binding,
                episode_binding=episode_binding, authorization=authorization,
            )

    def test_console_preserves_production_authoring_without_opening_effects(self):
        hypothesis = production_hypothesis()
        source_draft = campaign_draft(hypothesis, count=1)
        counters = {name: 0 for name in SIDE_EFFECT_COUNTERS}

        def factory(episode_call):
            return CampaignOperator(
                session_id="production-campaign-r001",
                lifecycle_owner="local-operator", operator_label="local-operator",
                workspace={"workspace_id": "qualified-workspace", "identity": "PRODUCTION"},
                hypothesis=hypothesis, draft=source_draft,
                effect_scope="PHYSICAL", lifecycle_action="LIVE_COLLECT",
                data_disposition="PRODUCTION",
                subsystems={
                    "planner": {
                        "readiness": "READY", "capability": "PLAN",
                        "reason": "QUALIFICATION_ARTIFACT",
                    },
                },
                expires_at=EXPIRES,
                initial_scene_digest=hypothesis["fixed_contract"]["scene_digest"],
                scene_evidence_call=lambda _run_id: {},
                side_effect_counter_call=lambda: copy.deepcopy(counters),
                fake_lifecycle_factory=lambda: None,
                physical_activation_gate=lambda: True,
                physical_lifecycle_factory=lambda: None,
                physical_live_call=episode_call,
                physical_root_binding_call=lambda _run_id: {},
                physical_start_binding_call=lambda _run_id, _slot, _cancel: {},
                clock=lambda: NOW,
            )

        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory)
            harness.hypothesis = hypothesis
            harness.source_draft = source_draft
            console = OperatorConsole(
                session_id="production-console-r001", operator_label="local-operator",
                campaign_operator_factory=factory,
                episode_call=lambda *_args: {}, projection_call=harness.projection,
                test_only_paths=directory, campaign_approval_once=True,
                clock=lambda: NOW,
            )
            try:
                self.assertEqual(console.projection()["data_disposition"], "PRODUCTION")
                result = console.compile_draft({
                    "draft_id": source_draft["draft_id"],
                    "data_disposition": "PRODUCTION",
                }, {})
                self.assertEqual(result["outcome"], "REVIEW_CAMPAIGN")
                self.assertEqual(counters, {name: 0 for name in SIDE_EFFECT_COUNTERS})
            finally:
                console.close()

    def test_product_application_dispatches_executable_general_mode_to_production_factory(self):
        repository = Path(__file__).resolve().parents[3]
        device = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
        catalog = load_operator_catalog(repository, device_ids=[device])
        for combination in catalog["combinations"]:
            if combination["execution"]["TEST_COLLECTION"]["executable"]:
                combination["execution"]["GENERAL_COLLECTION"] = {
                    "executable": True, "reason": "GENERAL_CALLER_READY",
                }
                combination["combination_digest"] = canonical_digest({
                    key: value for key, value in combination.items()
                    if key != "combination_digest"
                })
        catalog["combinations"].sort(
            key=lambda combination: combination["combination_digest"],
        )
        catalog["catalog_digest"] = canonical_digest({
            key: value for key, value in catalog.items()
            if key != "catalog_digest"
        })

        hypothesis = production_hypothesis()
        counters = {name: 0 for name in SIDE_EFFECT_COUNTERS}
        created = []

        def production_factory(campaign_id, selected, app_draft):
            source_draft = campaign_draft(hypothesis, count=app_draft["requested_count"])
            source_draft.update(
                draft_id=app_draft["draft_id"],
                manifest_id=f"{campaign_id}-manifest",
            )

            def owner_factory(episode_call):
                return CampaignOperator(
                    session_id=campaign_id, lifecycle_owner="local-operator",
                    operator_label="local-operator",
                    workspace={"workspace_id": selected["workspace_id"], "identity": "PRODUCTION"},
                    hypothesis=hypothesis, draft=source_draft,
                    effect_scope="PHYSICAL", lifecycle_action="LIVE_COLLECT",
                    data_disposition="PRODUCTION",
                    subsystems={
                        "planner": {
                            "readiness": "READY", "capability": "PLAN",
                            "reason": "QUALIFICATION_ARTIFACT",
                        },
                    },
                    expires_at=EXPIRES,
                    initial_scene_digest=hypothesis["fixed_contract"]["scene_digest"],
                    scene_evidence_call=lambda _run_id: {},
                    side_effect_counter_call=lambda: copy.deepcopy(counters),
                    fake_lifecycle_factory=lambda: None,
                    physical_activation_gate=lambda: True,
                    physical_lifecycle_factory=lambda: None,
                    physical_live_call=episode_call,
                    physical_root_binding_call=lambda _run_id: {},
                    physical_start_binding_call=lambda _run_id, _slot, _cancel: {},
                    clock=lambda: NOW,
                )

            harness = Harness(str(repository))
            harness.hypothesis = hypothesis
            harness.source_draft = source_draft
            console = OperatorConsole(
                session_id=campaign_id, operator_label="local-operator",
                campaign_operator_factory=owner_factory,
                episode_call=lambda *_args: {}, projection_call=harness.projection,
                test_only_paths="production-bound-roots",
                campaign_approval_once=True, clock=lambda: NOW,
            )
            created.append((copy.deepcopy(selected), console))
            return console

        environment = {
            "schema_version": "data_factory.operator_environment.v1",
            "state": "READY", "observed_at": "2026-08-26T03:00:00Z",
            "components": {
                name: {"state": "READY", "owner": f"owner-{name}", "reason": "ATTACHED"}
                for name in ("robot", "controller", "gripper", "camera")
            },
        }
        application, _context = build_physical_operator_application(
            repository_root=repository, session_id="general-dispatch-r001",
            operator_label="local-operator",
            environment_call=lambda: copy.deepcopy(environment),
            prepare_environment_call=lambda: copy.deepcopy(environment),
            selected_camera_device_id=device, discovery_call=lambda: [device],
            initial_catalog=catalog, initial_camera_devices=[device],
            initial_environment=environment,
            production_campaign_factory=production_factory, clock=lambda: NOW,
        )
        try:
            authoring = application.bridge_core.snapshot()
            application.bridge_core.consume(envelope(
                authoring, "update_draft", {
                    "draft_id": authoring["projection"]["draft"]["draft_id"],
                    "selection": {"data_mode": "PRODUCTION"},
                }, "select-general-r001",
            ))
            authoring = application.bridge_core.snapshot()
            result = application.bridge_core.consume(envelope(
                authoring, "compile_draft", {
                    "draft_id": authoring["projection"]["draft"]["draft_id"],
                    "data_disposition": "PRODUCTION",
                }, "compile-general-r001",
            ))["result"]
            self.assertEqual(result["outcome"], "REVIEW_CAMPAIGN")
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0][0]["data_mode"], "GENERAL_COLLECTION")
            self.assertEqual(
                created[0][1].campaign_operator.data_disposition, "PRODUCTION",
            )
            self.assertEqual(counters, {name: 0 for name in SIDE_EFFECT_COUNTERS})
        finally:
            application.close()

    def test_campaign_camera_warmup_reuses_only_the_exact_bound_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = {}
            transport = {"binding_digest": canonical_digest("camera-binding")}
            profile = {"collection_profile_id": "profile-a", "fps": 30}
            calls = []

            def measure(payload, active_profile, _cancel):
                calls.append((payload["run_id"], canonical_digest(active_profile)))
                return {
                    "schema_version": "data_factory.camera_warmup.v1",
                    "run_id": payload["run_id"], "camera_profile": "up",
                    "attempts": [{"attempt": 1, "roles": [], "status": "PASS"}],
                }

            def payload(run_id):
                (root / run_id).mkdir()
                return {
                    "run_id": run_id, "run_root": str(root),
                    "camera_profile": "up",
                }

            first = _campaign_camera_warmup(
                cache=cache, transport=transport, payload=payload("run-1"),
                profile=profile, cancel=threading.Event(), measure_call=measure,
            )
            second = _campaign_camera_warmup(
                cache=cache, transport=transport, payload=payload("run-2"),
                profile=profile, cancel=threading.Event(), measure_call=measure,
            )
            changed = _campaign_camera_warmup(
                cache=cache, transport=transport, payload=payload("run-3"),
                profile={**profile, "fps": 29}, cancel=threading.Event(),
                measure_call=measure,
            )
            self.assertEqual([item[0] for item in calls], ["run-1", "run-3"])
            self.assertEqual(first["schema_version"], "data_factory.camera_warmup.v1")
            self.assertEqual(
                second["source_evidence_digest"],
                canonical_digest(first),
            )
            self.assertEqual(
                (second["schema_version"], second["run_id"], second["status"]),
                ("data_factory.camera_warmup_reuse.v1", "run-2", "REUSED_PASS"),
            )
            self.assertEqual(changed["run_id"], "run-3")
            self.assertEqual(
                json.loads((root / "run-2/camera_warmup_reuse.json").read_text()),
                second,
            )
            self.assertFalse((root / "run-2/camera_warmup.json").exists())

    @staticmethod
    def gripper_setup_request() -> dict:
        return {
            "schema_version": "data_factory.operator_checkpoint_request.v1",
            "kind": "GRIPPER_MAINTENANCE", "run_id": "run-setup-r001",
            "plan_digest": canonical_digest("setup-only-binding"),
            "prompt": "Confirm empty gripper and clear cell before one normalization.",
            "choices": ["READY", "CANCEL"],
            "evidence": {
                "setup_only": True, "plan_exists": False,
                "readback_digest": canonical_digest("gripper-readback"),
            },
            "timeout_s": None,
        }

    @staticmethod
    def portable_repository(target: Path) -> None:
        source = Path(__file__).resolve().parents[3]
        shutil.copytree(source / "config/data_factory", target / "config/data_factory")
        urdf = target / "src/fairino_description/urdf/fairino5_v6.urdf"
        urdf.parent.mkdir(parents=True)
        shutil.copy2(source / "src/fairino_description/urdf/fairino5_v6.urdf", urdf)

    @staticmethod
    def qualified_home_start(target: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        motion = load_json_strict(
            target / "config/data_factory/motion_qualifications/"
            "fr5-place-a-wood-cube-r001.json",
        )
        home = load_json_strict(
            target / "config/data_factory/home_candidates/fr5-lab-a-home-r001.json",
        )
        qualification = {
            "schema_version": "data_factory.robot_start_pose_qualification.v1",
            "source": "QUALIFICATION_ARTIFACT",
            "robot_system_id": motion["robot_system_id"],
            "robot_start_pose_id": home["home_candidate_id"],
            "joint_order": copy.deepcopy(home["joint_order"]),
            "target_rad": dict(zip(
                home["joint_order"], motion["qualified_safe_joint_positions_rad"],
            )),
            "tolerance_rad": dict.fromkeys(
                home["joint_order"], motion["goal_tolerances"]["joint_rad"],
            ),
            "home_candidate_digest": canonical_digest(home),
            "qualification_status": "QUALIFIED",
            "safety_status": "SAFE_FOR_MOTION",
        }
        qualification["qualification_digest"] = canonical_digest(qualification)
        return motion, qualification

    @staticmethod
    def start_bridge(console: OperatorConsole):
        bridge = LoopbackBridge(
            core=console.bridge_core,
            ui_root=Path(__file__).resolve().parents[3] / "operator-ui",
            host="127.0.0.1", port=0,
            token="operator-console-loopback-test-token",
        )
        thread = threading.Thread(target=bridge.serve_forever)
        thread.start()
        return bridge, thread

    @staticmethod
    def request_json(bridge: LoopbackBridge, method: str, path: str, body=None):
        headers = {"X-Operator-Token": bridge.token}
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":"), allow_nan=False)
            headers.update({
                "Origin": bridge.origin,
                "Content-Type": "application/json",
            })
        connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        connection.close()
        return response.status, value

    def wait_for_http_projection(
        self, bridge: LoopbackBridge, key: str,
    ) -> tuple[dict, object]:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status, view = self.request_json(bridge, "GET", "/api/view")
            self.assertEqual(status, 200)
            value = view["projection"].get(key)
            if value is not None:
                return view, value
            time.sleep(0.005)
        self.fail(f"timed out waiting for HTTP projection {key}")

    def wait_for(self, console: OperatorConsole, key: str) -> dict:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            view = console.bridge_core.snapshot()
            if view["projection"].get(key) is not None:
                return view
            time.sleep(0.005)
        self.fail(f"timed out waiting for {key}")

    def test_physical_contract_is_exact_single_camera_one_slot_and_test_only(self):
        profile = {
            "schema_version": "data_factory.collection_profile.v2",
            "collection_profile_id": "fr5-up-rgb-30hz-v1",
            "qualification_status": "QUALIFIED",
        }
        _, _, resolvers, _, _, _ = single_qualification_inputs(
            collection_profile=profile,
        )
        resolved = resolvers[0]
        job = resolved["normalized_job"]
        home = {
            "schema_version": "data_factory.home_candidate.v1",
            "robot_system_id": job["robot_system_id"],
        }
        motion = {
            "schema_version": "data_factory.motion_qualification.v1",
            "qualification_status": "QUALIFIED",
            **{
                field: job[field] for field in (
                    "robot_system_id", "cell_calibration_id", "object_profile_id",
                    "grasp_profile_id",
                )
            },
            "home_candidate_digest": canonical_digest(home),
            "qualified_safe_joint_positions_rad": [0.0] * 6,
            "goal_tolerances": {"joint_rad": 0.01},
        }
        contract, draft = build_physical_test_contract(
            resolved_job=resolved, motion_qualification=motion,
            home_candidate=home, scene_digest=canonical_digest("test-only-scene"),
            draft_id="physical-draft-r001", manifest_id="physical-manifest-r001",
        )
        self.assertEqual(
            contract["fixed_contract"]["feature_contract"]["camera_mapping"],
            {"up": "camera1"},
        )
        self.assertEqual(
            (len(contract["base_conditions"]), len(contract["robot_start_poses"]),
             len(contract["allowed_pairs"]), contract["allowed_pairs"][0]["split_groups"]),
            (1, 1, 1, ["TRAIN"]),
        )
        self.assertEqual(draft["requested_count"], 1)
        self.assertEqual(contract["qualification_catalog"]["source"], "SYNTHETIC_TEST_ONLY")

    def test_passive_gate_binds_selected_uvc_character_device_to_ros_publisher(self):
        with tempfile.TemporaryDirectory() as directory:
            device_root = Path(directory)
            token = "usb-Goal2_Camera-video-index0"
            (device_root / token).symlink_to("/dev/null")
            commands = []

            def command(args, _code):
                commands.append(args)
                if args[:3] == ["ros2", "control", "list_controllers"]:
                    return (
                        "fairino5_controller active\n"
                        "gripper_controller active\n"
                        "joint_state_broadcaster active\n"
                    )
                if args[:3] == ["ros2", "node", "list"]:
                    return "/camera/up/color/uvc_up_camera\n"
                if args[:4] == ["ros2", "topic", "type", "/joint_states"]:
                    return "sensor_msgs/msg/JointState\n"
                if args[:4] == [
                    "ros2", "topic", "type", "/camera/up/color/image_raw",
                ]:
                    return "sensor_msgs/msg/Image\n"
                if args[:4] == ["ros2", "param", "get", "/camera/up/color/uvc_up_camera"]:
                    return "/dev/null\n"
                raise AssertionError(args)

            with mock.patch(
                "tools.data_factory.operator.setup.physical._readonly_command",
                side_effect=command,
            ):
                evidence = passive_physical_gate(
                    camera_topic="/camera/up/color/image_raw",
                    discovered_device_id=token,
                    device_root=device_root,
                    discovery_call=lambda: [token],
                )
            self.assertEqual(evidence["stable_device_id"], token)
            self.assertEqual(evidence["resolved_device"], "/dev/null")
            self.assertEqual(evidence["reported_video_device"], "/dev/null")
            self.assertEqual(evidence["authority"], "TEST_ONLY_TRANSPORT")
            self.assertTrue(all(
                "--no-daemon" in args
                for args in commands
                if args[1] in {"node", "topic", "param"}
            ))
            self.assertTrue(all(
                args[-2:] == ["--spin-time", "2"]
                for args in commands
                if args[1] in {"node", "topic"}
            ))
            self.assertEqual(
                evidence["binding_digest"],
                canonical_digest({
                    key: value for key, value in evidence.items()
                    if key != "binding_digest"
                }),
            )

            camera_reads = 0

            def transient_camera_command(args, code):
                nonlocal camera_reads
                if args[:4] == [
                    "ros2", "topic", "type", "/camera/up/color/image_raw",
                ]:
                    camera_reads += 1
                    if camera_reads == 1:
                        raise ContractError(code)
                return command(args, code)

            with mock.patch(
                "tools.data_factory.operator.setup.physical._readonly_command",
                side_effect=transient_camera_command,
            ):
                transient_evidence = passive_physical_gate(
                    camera_topic="/camera/up/color/image_raw",
                    discovered_device_id=token,
                    device_root=device_root,
                    discovery_call=lambda: [token],
                )
            self.assertEqual(camera_reads, 2)
            self.assertEqual(transient_evidence["topic_type"], "sensor_msgs/msg/Image")

            joint_reads = 0

            def transient_joint_command(args, code):
                nonlocal joint_reads
                if args[:4] == ["ros2", "topic", "type", "/joint_states"]:
                    joint_reads += 1
                    if joint_reads < 3:
                        raise ContractError(code)
                return command(args, code)

            with (
                mock.patch(
                    "tools.data_factory.operator.setup.physical._readonly_command",
                    side_effect=transient_joint_command,
                ),
                mock.patch("tools.data_factory.operator.setup.physical.time.sleep") as pause,
            ):
                passive_physical_gate(
                    camera_topic="/camera/up/color/image_raw",
                    discovered_device_id=token,
                    device_root=device_root,
                    discovery_call=lambda: [token],
                )
            self.assertEqual(joint_reads, 3)
            self.assertEqual(pause.call_args_list, [mock.call(0.1), mock.call(0.2)])

            def mismatched_command(args, code):
                result = command(args, code)
                return "/dev/zero\n" if args[:4] == [
                    "ros2", "param", "get", "/camera/up/color/uvc_up_camera",
                ] else result

            with mock.patch(
                "tools.data_factory.operator.setup.physical._readonly_command",
                side_effect=mismatched_command,
            ):
                with self.assertRaisesRegex(
                    ContractError, "PHYSICAL_CAMERA_DEVICE_MISMATCH",
                ):
                    passive_physical_gate(
                        camera_topic="/camera/up/color/image_raw",
                        discovered_device_id=token,
                        device_root=device_root,
                        discovery_call=lambda: [token],
                    )

            for discovered, expected in (
                ([token, token], "CAMERA_BINDING_DISCOVERY"),
                ([token], "PHYSICAL_CAMERA_BINDING_MISMATCH"),
            ):
                with self.subTest(discovered=discovered, expected=expected):
                    if len(discovered) == 1:
                        (device_root / token).unlink()
                    try:
                        with self.assertRaisesRegex(ContractError, expected):
                            passive_physical_gate(
                                camera_topic="/camera/up/color/image_raw",
                                discovered_device_id=token,
                                device_root=device_root,
                                discovery_call=lambda discovered=discovered: discovered,
                            )
                    finally:
                        if not (device_root / token).exists():
                            (device_root / token).symlink_to("/dev/null")

    def test_home_snapshot_retries_one_transient_read_failure(self):
        snapshot = {"schema_version": "data_factory.pose_snapshot.v1"}
        read = mock.Mock(side_effect=(
            ContractError("PHYSICAL_HOME_SNAPSHOT"), json.dumps(snapshot),
        ))
        with mock.patch(
            "tools.data_factory.operator.setup.physical._readonly_command", read,
        ):
            self.assertEqual(
                capture_home_snapshot(tcp_candidate_manifest=Path("tcp.json")),
                snapshot,
            )
        self.assertEqual(read.call_count, 2)
        self.assertEqual(
            read.call_args_list[0].args[0][3:8],
            ["capture", "--timeout-s", "2", "--max-age-s", "0.1"],
        )

        read = mock.Mock(side_effect=ContractError("PHYSICAL_HOME_SNAPSHOT"))
        with (
            mock.patch(
                "tools.data_factory.operator.setup.physical._readonly_command", read,
            ),
            self.assertRaisesRegex(ContractError, "PHYSICAL_HOME_SNAPSHOT"),
        ):
            capture_home_snapshot(tcp_candidate_manifest=Path("tcp.json"))
        self.assertEqual(read.call_count, 2)

    def test_gripper_ros_adapter_reads_one_owner_and_runs_only_sealed_normalization(self):
        controller_message = """A message was lost!!!
    total count change:1
    total count: 1---
header:
  stamp: {sec: 1, nanosec: 0}
joint_names: [finger_right_joint]
reference:
  positions: [0.012]
feedback:
  positions: [0.012]
---
"""

        def controller_read(args, _code):
            if args[:3] == ["ros2", "node", "list"]:
                return "/camera/up/color/uvc_up_camera\n"
            if args[:3] == ["ros2", "control", "list_controllers"]:
                return (
                    "fairino5_controller active\n"
                    "gripper_controller active\n"
                    "joint_state_broadcaster active\n"
                )
            if args[:3] == ["ros2", "topic", "echo"]:
                return controller_message
            raise AssertionError(args)

        with mock.patch(
            "tools.data_factory.operator.setup.physical._readonly_command",
            side_effect=controller_read,
        ):
            readback = capture_gripper_setup_readback()
        self.assertEqual(
            (readback["source"], readback["active"],
             readback["reference_position_m"], readback["feedback_position_m"]),
            ("CONTROLLER_STATE", True, 0.012, 0.012),
        )
        snapshot_read = mock.Mock(side_effect=controller_read)
        with mock.patch(
            "tools.data_factory.operator.setup.physical._readonly_command",
            snapshot_read,
        ):
            capture_gripper_setup_readback(
                {"/controller_manager"},
                "fairino5_controller active\ngripper_controller active\n"
                "joint_state_broadcaster active\n",
            )
        self.assertEqual(snapshot_read.call_count, 1)
        self.assertEqual(snapshot_read.call_args.args[0][:3], ["ros2", "topic", "echo"])
        malformed_read_remote = mock.Mock()
        with (
            mock.patch(
                "tools.data_factory.operator.setup.physical._readonly_command",
                side_effect=lambda args, _code: (
                    "/controller_manager\n"
                    if args[:3] == ["ros2", "node", "list"] else
                    "fairino5_controller active\ngripper_controller active\n"
                    "joint_state_broadcaster active\n"
                    if args[:3] == ["ros2", "control", "list_controllers"] else
                    "A message was lost!!!\n"
                ),
            ),
            mock.patch(
                "tools.data_factory.operator.setup.physical._remote_robot_command",
                malformed_read_remote,
            ),
            self.assertRaisesRegex(ContractError, "GRIPPER_SETUP_READBACK"),
        ):
            capture_gripper_setup_readback()
        malformed_read_remote.assert_not_called()
        with mock.patch(
            "tools.data_factory.operator.setup.physical._bounded_command",
            return_value="Result:\n  error_code: 0\nGoal finished with status: SUCCEEDED\n",
        ) as action:
            result = normalize_gripper_after_operator_ready(readback)
        self.assertEqual(result, {"status": "NORMALIZED", "requires_graph_switch": False})
        self.assertEqual(action.call_count, 1)
        self.assertEqual(action.call_args.args[0][0:4], [
            "ros2", "action", "send_goal",
            "/gripper_controller/follow_joint_trajectory",
        ])

        server_read = mock.Mock(side_effect=(
            [0, 0, 0], [0, 0, 0],
        ))
        with (
            mock.patch(
                "tools.data_factory.operator.setup.physical._readonly_command",
                side_effect=(
                    lambda args, _code: "/fr_command_server\n"
                    if args[:3] == ["ros2", "node", "list"]
                    else (_ for _ in ()).throw(AssertionError(args))
                ),
            ),
            mock.patch(
                "tools.data_factory.operator.setup.physical._remote_robot_command",
                server_read,
            ),
        ):
            maintenance_readback = capture_gripper_setup_readback()
        self.assertEqual(
            (maintenance_readback["source"], maintenance_readback["active"]),
            ("COMMAND_SERVER_MAINTENANCE", False),
        )

        remote = mock.Mock()
        with (
            mock.patch(
                "tools.data_factory.operator.setup.physical._readonly_command",
                side_effect=(
                    lambda args, _code: "/controller_manager\n/fr_command_server\n"
                    if args[:3] == ["ros2", "node", "list"]
                    else (_ for _ in ()).throw(
                        ContractError("GRIPPER_SETUP_CONTROLLER_GRAPH")
                    )
                ),
            ),
            mock.patch(
                "tools.data_factory.operator.setup.physical._remote_robot_command",
                remote,
            ),
            self.assertRaisesRegex(ContractError, "GRIPPER_SETUP_CONTROLLER_GRAPH"),
        ):
            capture_gripper_setup_readback()
        remote.assert_not_called()

        remote.reset_mock()
        with (
            mock.patch(
                "tools.data_factory.operator.setup.physical._readonly_command",
                side_effect=(
                    lambda args, _code: "/controller_manager\n/fr_command_server\n"
                    if args[:3] == ["ros2", "node", "list"]
                    else "gripper_controller inactive\n"
                ),
            ),
            mock.patch(
                "tools.data_factory.operator.setup.physical._remote_robot_command",
                remote,
            ),
            self.assertRaisesRegex(ContractError, "GRIPPER_SETUP_NOT_AVAILABLE"),
        ):
            capture_gripper_setup_readback()
        remote.assert_not_called()
        commands = []

        def server_command(command, *, expected_fields):
            commands.append((command, expected_fields))
            return {
                "GetGripperActivateStatus()": [0, 0, 0],
                "ActGripper(1,0)": [0], "ActGripper(1,1)": [0],
                "MoveGripper(1,100)": [0],
                "GetGripperMotionDone()": [0, 0, 1],
                "GetGripperCurPosition()": [0, 0, 100],
            }[command]

        settled = []
        with mock.patch(
            "tools.data_factory.operator.setup.physical._remote_robot_command",
            side_effect=server_command,
        ):
            result = normalize_gripper_after_operator_ready(
                maintenance_readback, settle_call=settled.append,
            )
        self.assertEqual(result, {"status": "NORMALIZED", "requires_graph_switch": True})
        self.assertEqual(settled, [1.0, 2.0])
        self.assertEqual(
            [command for command, _ in commands],
            [
                "GetGripperActivateStatus()", "ActGripper(1,0)",
                "ActGripper(1,1)", "MoveGripper(1,100)",
                "GetGripperMotionDone()", "GetGripperCurPosition()",
            ],
        )

        commands.clear()
        remote = mock.Mock(side_effect=(
            [0, 3, 1], [0], [0, 0, 1], [0], [0, 0, 1], [0, 0, 100],
        ))
        with mock.patch(
            "tools.data_factory.operator.setup.physical._remote_robot_command", remote,
        ):
            result = normalize_gripper_after_operator_ready(maintenance_readback)
        self.assertEqual(result, {"status": "NORMALIZED", "requires_graph_switch": True})
        self.assertEqual(
            [call.args[0] for call in remote.call_args_list],
            [
                "GetGripperActivateStatus()", "ResetAllError()",
                "GetGripperActivateStatus()", "MoveGripper(1,100)",
                "GetGripperMotionDone()", "GetGripperCurPosition()",
            ],
        )

    def test_real_physical_composition_projects_and_resolves_gripper_setup_before_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            closed = {
                "active": True, "position_valid": True, "gripper_index": 1,
                "reference_position_m": 0.012, "feedback_position_m": 0.012,
                "sample_age_s": 0.0, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE",
            }
            opened = {
                **closed, "reference_position_m": 0.021,
                "feedback_position_m": 0.021,
            }
            readbacks = [closed, closed, opened]
            maintenance = mock.Mock(return_value={
                "status": "NORMALIZED", "requires_graph_switch": False,
            })
            console, context = build_physical_operator_console(
                repository_root=root,
                session_id="goal2-physical-composition-r001",
                run_id="goal2-place1-composition-r001",
                operator_label="local-operator",
                discovery_call=lambda: ["usb-Goal2_Camera-video-index0"],
                activation_call=lambda: True,
                gripper_readback_call=lambda: copy.deepcopy(readbacks.pop(0)),
                gripper_maintenance_call=maintenance,
                clock=lambda: NOW,
            )
            try:
                view = console.bridge_core.snapshot()
                projection = view["projection"]
                self.assertEqual(projection["setup"]["host_status"], "READY_WITH_EXCEPTION")
                self.assertEqual(
                    projection["operator_checkpoint"]["kind"], "GRIPPER_MAINTENANCE",
                )
                self.assertEqual(projection["available_ops"], ["resolve_checkpoint"])
                checkpoint = projection["operator_checkpoint"]
                result = console.bridge_core.consume(envelope(
                    view, "resolve_checkpoint", {
                        "checkpoint_binding_digest": checkpoint["binding_digest"],
                        "choice": "READY",
                    }, "physical-gripper-ready-r001",
                ))["result"]
                after = console.bridge_core.snapshot()["projection"]
                self.assertEqual(
                    (result["outcome"], after["setup"]["host_status"],
                     after["available_ops"], after["effect_counts"]["gripper"]),
                    ("READY", "READY", ["compile_draft"], 1),
                )
                self.assertEqual(context["production_writers_enabled"], False)
                self.assertEqual(context["gripper_setup"]["state"], "MAINTENANCE_APPROVAL_REQUIRED")
                tuning = projection["gripper_tuning"]
                self.assertEqual(
                    (tuning["object_profile_id"], tuning["grasp_profile_id"],
                     tuning["command_percent"],
                     tuning["acceptable_feedback_percent"],
                     tuning["velocity_percent"], tuning["force_percent"],
                     tuning["open_velocity_percent"],
                     tuning["open_force_percent"], tuning["status"]),
                    ("wood-cube-25mm-r001", "wood-cube-25mm-top-center-r001",
                     56.0, {"min": 56.0, "max": 58.0},
                     20, 20, 10, 50,
                     "CANDIDATE_PENDING_HIL"),
                )
                self.assertFalse(tuning["production_authority"])
                self.assertFalse(tuning["training_authority"])
                self.assertEqual(
                    context["motion_qualification_digest"],
                    context["base_motion_qualification_digest"],
                )
                maintenance.assert_called_once_with(closed)
                self.assertEqual(readbacks, [])
                self.assertIsNone(console.episode_worker)
            finally:
                console.close()

    def test_default_resolver_keeps_qualified_54_percent_grasp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            config = root / "config/data_factory"
            goal = config / "test_only_physical/goal2-place1"
            payload = {
                "mode": "plan_only", "run_id": "default-grasp-r001",
                "job": load_json_strict(goal / "center-live-p45-20260821-r001.job.json"),
                "selected_sheet": str(goal / "yaw0_sheet.json"),
                "yaw0_sheet": str(goal / "yaw0_sheet.json"),
                "config_root": str(config),
                "motion_qualification": str(
                    config / "motion_qualifications/fr5-place-a-wood-cube-r001.json"
                ),
                "home_candidate": str(
                    config / "home_candidates/fr5-lab-a-home-r001.json"
                ),
                "urdf": str(root / "src/fairino_description/urdf/fairino5_v6.urdf"),
                "expected_robot_system_id": "fr5-lab-a",
            }
            resolved, program, _ = run_job.resolve_inputs(
                payload, scene_binding_call=lambda *_args: {},
            )
            close = resolved["grasp_profile"]["gripper_close"]
            self.assertEqual(close["command_position_m"], 0.01134)
            self.assertEqual(
                close["acceptable_feedback_m"], {"min": 0.01134, "max": 0.01218},
            )
            self.assertEqual(
                program["gripper_requirements"]["evidence_digest"],
                "sha256:77ca8fbbd45a18c5f5ce230b7494a05678c3810da1e3e51164766d6f15e6c22f",
            )

    def test_test_only_gripper_retune_rejects_out_of_envelope_before_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            path = root / (
                "config/data_factory/test_only_physical/goal2-place1/"
                "gripper-retune-wood-cube-25mm-top-center-r002.json"
            )
            retune = load_json_strict(path)
            retune["acceptable_feedback_m"]["max"] = 0.01219
            retune["retune_digest"] = canonical_digest({
                key: value for key, value in retune.items()
                if key != "retune_digest"
            })
            path.write_text(json.dumps(retune), encoding="utf-8")
            readback = mock.Mock(side_effect=AssertionError("gripper read before static validation"))
            with self.assertRaisesRegex(
                ContractError, "TEST_ONLY_GRIPPER_RETUNE_ENVELOPE",
            ):
                build_physical_operator_console(
                    repository_root=root,
                    session_id="retune-envelope-r001",
                    run_id="retune-envelope-run-r001",
                    operator_label="local-operator",
                    gripper_retune_path=(
                        "config/data_factory/test_only_physical/goal2-place1/"
                        "gripper-retune-wood-cube-25mm-top-center-r002.json"
                    ),
                    discovery_call=lambda: ["usb-Goal2_Camera-video-index0"],
                    activation_call=lambda: True,
                    gripper_readback_call=readback,
                    clock=lambda: NOW,
                )
            self.assertFalse((root / "outputs").exists())
            self.assertFalse((root / "datasets").exists())
            readback.assert_not_called()

    def test_test_only_gripper_retune_v2_reduces_force_without_an_extra_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            config = root / "config/data_factory"
            goal = config / "test_only_physical/goal2-place1"
            request = {
                "mode": "plan_only", "run_id": "force-retune-r004",
                "job": load_json_strict(
                    goal / "center-live-p45-20260821-r001.job.json"
                ),
                "selected_sheet": str(goal / "yaw0_sheet.json"),
                "yaw0_sheet": str(goal / "yaw0_sheet.json"),
                "config_root": str(config),
                "motion_qualification": str(
                    config / "motion_qualifications/fr5-place-a-wood-cube-r001.json"
                ),
                "home_candidate": str(
                    config / "home_candidates/fr5-lab-a-home-r001.json"
                ),
                "urdf": str(
                    root / "src/fairino_description/urdf/fairino5_v6.urdf"
                ),
                "expected_robot_system_id": "fr5-lab-a",
            }
            resolved, program, _ = run_job.resolve_inputs(
                request, scene_binding_call=lambda *_args: {},
            )
            motion = load_json_strict(request["motion_qualification"])
            retune = load_json_strict(
                goal / "gripper-retune-wood-cube-25mm-top-center-r004.json"
            )
            tuned = _derive_test_only_gripper_program(
                resolved, motion, program, retune,
            )
            self.assertEqual(
                (
                    program["gripper_requirements"]["force_percent"],
                    tuned["gripper_requirements"]["force_percent"],
                    tuned["gripper_requirements"]["command_position_m"],
                    tuned["gripper_requirements"]["acceptable_feedback_m"],
                ),
                (
                    50, 25, 0.021 * 56 / 100,
                    {"min": 0.021 * 57 / 100, "max": 0.021 * 58 / 100},
                ),
            )
            self.assertEqual(
                [step["phase"] for step in tuned["steps"]],
                [step["phase"] for step in program["steps"]],
            )
            stronger = copy.deepcopy(retune)
            stronger["force_percent"] = 51
            stronger["retune_digest"] = canonical_digest({
                key: value for key, value in stronger.items()
                if key != "retune_digest"
            })
            with self.assertRaisesRegex(
                ContractError, "TEST_ONLY_GRIPPER_RETUNE_ENVELOPE",
            ):
                _derive_test_only_gripper_program(
                    resolved, motion, program, stronger,
                )
            release_retune = load_json_strict(
                goal / "gripper-retune-wood-cube-25mm-top-center-r007.json"
            )
            release_tuned = _derive_test_only_gripper_program(
                resolved, motion, program, release_retune,
            )
            self.assertEqual(
                release_tuned["gripper_requirements"][
                    "open_velocity_percent"
                ],
                10,
            )
            independent = copy.deepcopy(release_retune)
            independent.update(
                schema_version="data_factory.test_only_gripper_retune.v4",
                retune_id="wood-cube-25mm-top-center-test-only-r008",
                velocity_percent=19,
                open_force_percent=49,
            )
            independent["retune_digest"] = canonical_digest({
                key: value for key, value in independent.items()
                if key != "retune_digest"
            })
            independent_tuned = _derive_test_only_gripper_program(
                resolved, motion, program, independent,
            )
            self.assertEqual(
                tuple(
                    independent_tuned["gripper_requirements"][key]
                    for key in (
                        "velocity_percent", "force_percent",
                        "open_velocity_percent", "open_force_percent",
                    )
                ),
                (19, 20, 10, 49),
            )
            too_fast = copy.deepcopy(release_retune)
            too_fast["open_velocity_percent"] = 21
            too_fast["retune_digest"] = canonical_digest({
                key: value for key, value in too_fast.items()
                if key != "retune_digest"
            })
            with self.assertRaisesRegex(
                ContractError, "TEST_ONLY_GRIPPER_RETUNE_ENVELOPE",
            ):
                _derive_test_only_gripper_program(
                    resolved, motion, program, too_fast,
                )

    def test_real_physical_composition_auto_attaches_fresh_open_gripper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            opened = {
                "active": True, "position_valid": True, "gripper_index": 1,
                "reference_position_m": 0.021, "feedback_position_m": 0.021,
                "sample_age_s": 0.0, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE",
            }
            maintenance = mock.Mock()
            console, _ = build_physical_operator_console(
                repository_root=root,
                session_id="goal2-physical-attached-r001",
                run_id="goal2-place1-attached-r001",
                operator_label="local-operator",
                discovery_call=lambda: ["usb-Goal2_Camera-video-index0"],
                activation_call=lambda: True,
                gripper_readback_call=lambda: copy.deepcopy(opened),
                gripper_maintenance_call=maintenance,
                clock=lambda: NOW,
            )
            try:
                projection = console.bridge_core.snapshot()["projection"]
                self.assertEqual(
                    (projection["setup"]["host_status"], projection["available_ops"],
                     projection["operator_checkpoint"],
                     projection["effect_counts"]["gripper"]),
                    ("READY", ["compile_draft"], None, 0),
                )
                maintenance.assert_not_called()
                self.assertIsNone(console.episode_worker)
            finally:
                console.close()

    def test_one_pick_place_episode_compiles_start_source_and_distinct_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            job = load_json_strict(
                root / "config/data_factory/jobs/"
                "center-live-24mm-20260903-r002.job.json",
            )
            source = {
                key: job[key]
                for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
            }
            destination = {
                "place_id": "PLACE_A", "yaw_deg": 0,
                "x_mm": 0, "y_mm": -35,
            }
            opened = {
                "active": True, "position_valid": True, "gripper_index": 1,
                "reference_position_m": 0.021, "feedback_position_m": 0.021,
                "sample_age_s": 0.0, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE",
            }
            camera_up = "usb-Goal2_Camera-video-index0"
            camera_wrist = "usb-Goal2_Wrist_Camera-video-index0"
            console, context = build_physical_operator_console(
                repository_root=root,
                session_id="pick-place-one-episode-r001",
                run_id="pick-place-one-run-r001",
                operator_label="local-operator",
                job_path=(
                    "config/data_factory/jobs/"
                    "center-live-24mm-20260903-r002.job.json"
                ),
                motion_qualification_path=(
                    "config/data_factory/motion_qualifications/"
                    "fr5-place-a-wood-cube-24mm-r001.json"
                ),
                home_candidate_path=(
                    "config/data_factory/home_candidates/"
                    "fr5-lab-a-tcp-r002-home-r001.json"
                ),
                tcp_candidate_manifest=(
                    "config/data_factory/tcp_candidates/"
                    "fr5-lab-a-tcp-r002.json"
                ),
                gripper_retune_path=None,
                collection_profile_path=(
                    "config/data_factory/collection_profiles/"
                    "fr5-up-wrist-rgb-30hz-v2.json"
                ),
                discovery_call=lambda: [camera_up, camera_wrist],
                selected_camera_bindings={
                    "up": camera_up, "wrist": camera_wrist,
                },
                activation_call=lambda: True,
                gripper_readback_call=lambda: copy.deepcopy(opened),
                task_id="pick_place", requested_count=1,
                trajectory_variant_id="TWO_STAGE_ALIGN_V2",
                direct_pose_sequence=[source, destination],
                clock=lambda: NOW,
            )
            try:
                current = console.bridge_core.snapshot()
                compiled = console.bridge_core.consume(envelope(
                    current, "compile_draft", {
                        "draft_id": current["projection"]["draft"]["draft_id"],
                        "data_disposition": "TEST_ONLY",
                    }, "compile-pick-place-one-r001",
                ))["result"]
                review = console.bridge_core.snapshot()["projection"]
                coverage = review["campaign_coverage"]
                self.assertEqual((compiled["episode_count"], len(coverage)), (1, 1))
                self.assertEqual(
                    console.campaign_operator.hypothesis["fixed_contract"]["task"],
                    "pick_place",
                )
                self.assertEqual(
                    console.campaign_operator.hypothesis["fixed_contract"]["motion_recipe"],
                    "TWO_STAGE_ALIGN_V2",
                )
                self.assertEqual(
                    console.campaign_operator.hypothesis["fixed_contract"]["schema_version"],
                    "data_factory.fr5_fixed_contract.v3",
                )
                self.assertEqual(
                    len(console.campaign_operator.hypothesis["base_conditions"]),
                    2,
                )
                self.assertEqual(
                    {
                        key: coverage[0]["coverage_condition"][key]
                        for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
                    },
                    source,
                )
                self.assertEqual(coverage[0]["destination_pose"], {
                    **destination,
                    "yaw_deg": 0.0, "x_mm": 0.0, "y_mm": -35.0,
                })
                self.assertEqual(
                    [
                        item["role"]
                        for item in coverage[0]["task_binding"]["spatial_bindings"]
                    ],
                    ["SOURCE", "DESTINATION"],
                )
                self.assertEqual(context["requested_count"], 1)
                self.assertIsNone(console.episode_worker)
            finally:
                console.close()

    def test_cross_workspace_pick_place_binds_endpoints_and_rejects_unsafe_yaw_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            devices = [
                "usb-Cross_UP-video-index0",
                "usb-Cross_WRIST-video-index0",
            ]
            poses = [
                {"place_id": "PLACE_A", "yaw_deg": 0, "x_mm": 0, "y_mm": 0},
                {"place_id": "PLACE_B", "yaw_deg": 0, "x_mm": 0, "y_mm": 0},
                {"place_id": "PLACE_A", "yaw_deg": 0, "x_mm": 0, "y_mm": -35},
            ]
            region_layout = make_red_blue_region_layout()

            def prepared_region(place_id):
                region = workspace_region(region_layout, place_id)
                return {
                    "layout_id": region_layout["layout_id"],
                    "layout_digest": region_layout["layout_digest"],
                    "region_id": region["region_id"],
                    "physical_binding_status": "PREPARED_NOT_VERIFIED",
                }

            workspace_bindings = {
                "PLACE_A": {
                    "frame_id": "place-a-yaw0-r003",
                    "region_binding": prepared_region("PLACE_A"),
                    "selected_sheet": "config/data_factory/test_only_physical/goal2-place1/yaw0_sheet.json",
                    "yaw0_sheet": "config/data_factory/test_only_physical/goal2-place1/yaw0_sheet.json",
                    "motion_qualification": "config/data_factory/motion_qualifications/fr5-place-a-wood-cube-24mm-r001.json",
                },
                "PLACE_B": {
                    "frame_id": "place-b-yaw0-r001",
                    "region_binding": prepared_region("PLACE_B"),
                    "selected_sheet": "config/data_factory/workspace_sheets/place-b-yaw0-r001_yaw0_sheet.json",
                    "yaw0_sheet": "config/data_factory/workspace_sheets/place-b-yaw0-r001_yaw0_sheet.json",
                    "motion_qualification": "config/data_factory/motion_qualifications/fr5-place-b-wood-cube-24mm-r001.json",
                },
            }
            opened = {
                "active": True, "position_valid": True, "gripper_index": 1,
                "reference_position_m": 0.021, "feedback_position_m": 0.021,
                "sample_age_s": 0.0, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE",
            }
            console, _context = build_physical_operator_console(
                repository_root=root,
                session_id="cross-workspace-r001",
                run_id="cross-workspace-run-r001",
                operator_label="local-operator",
                job_path="config/data_factory/jobs/center-live-24mm-20260903-r002.job.json",
                yaw0_sheet=workspace_bindings["PLACE_A"]["yaw0_sheet"],
                motion_qualification_path=workspace_bindings["PLACE_A"]["motion_qualification"],
                home_candidate_path="config/data_factory/home_candidates/fr5-lab-a-tcp-r002-home-r001.json",
                collection_profile_path="config/data_factory/collection_profiles/fr5-up-wrist-rgb-30hz-v2.json",
                gripper_retune_path=None,
                workspace_bindings=workspace_bindings,
                discovery_call=lambda: devices,
                selected_camera_bindings={"up": devices[0], "wrist": devices[1]},
                gripper_readback_call=lambda: copy.deepcopy(opened),
                activation_call=lambda: True,
                task_id="pick_place", requested_count=2,
                direct_pose_sequence=poses,
                clock=lambda: NOW,
            )
            try:
                view = console.bridge_core.snapshot()
                console.bridge_core.consume(envelope(
                    view, "compile_draft", {
                        "draft_id": view["projection"]["draft"]["draft_id"],
                        "data_disposition": "TEST_ONLY",
                    }, "compile-cross-workspace-r001",
                ))
                coverage = console.bridge_core.snapshot()["projection"][
                    "campaign_coverage"
                ]
                self.assertEqual(
                    [
                        item["episode_instruction_binding"]["instruction"]
                        for item in coverage
                    ],
                    [
                        "pick up the 24 mm wooden cube from the red zone and place it in the blue zone",
                        "pick up the 24 mm wooden cube from the blue zone and place it in the red zone",
                    ],
                )
                self.assertTrue(all(
                    item["episode_instruction_binding"]["task_binding"]
                    == item["task_binding"]
                    for item in coverage
                ))
                hypothesis = console.campaign_operator.hypothesis
                manifest = console.campaign_operator.manifest
                self.assertEqual(
                    hypothesis["fixed_contract"]["schema_version"],
                    "data_factory.fr5_fixed_contract.v2",
                )
                self.assertEqual(
                    [item["workspace_id"] for item in hypothesis["fixed_contract"]["endpoint_bindings"]],
                    ["PLACE_A", "PLACE_B"],
                )
                home = load_json_strict(
                    root / "config/data_factory/home_candidates/fr5-lab-a-tcp-r002-home-r001.json",
                )
                motions = {
                    place: load_json_strict(root / binding["motion_qualification"])
                    for place, binding in workspace_bindings.items()
                }
                bound = []
                for slot in manifest["slots"]:
                    base = next(
                        item for item in hypothesis["base_conditions"]
                        if item["base_condition_digest"] == slot["base_condition_digest"]
                    )
                    receipt = next(
                        item for item in hypothesis["resolver_receipts"]
                        if item["resolver_result_digest"] == base["resolver_result_digest"]
                    )
                    place = receipt["normalized_job"]["place_id"]
                    motion = motions[place]
                    start = build_test_only_start_binding(
                        manifest=manifest, hypothesis=hypothesis, slot=slot,
                        motion_qualification=motion, home_candidate=home,
                        current_snapshot=pose_snapshot(
                            motion["qualified_safe_joint_positions_rad"], age=0.01,
                        ),
                    )
                    bound.append((place, start["motion_qualification_id"]))
                self.assertEqual(bound, [
                    ("PLACE_A", "fr5-place-a-wood-cube-24mm-r001"),
                    ("PLACE_B", "fr5-place-b-wood-cube-24mm-r001"),
                ])
            finally:
                console.close()

            unsafe_transition = [
                {
                    "place_id": "PLACE_B", "yaw_deg": 44.0,
                    "x_mm": 0.0, "y_mm": 0.0,
                },
                {
                    "place_id": "PLACE_A", "yaw_deg": 0.0,
                    "x_mm": 0.0, "y_mm": 68.0,
                },
            ]
            with self.assertRaises(ContractError) as raised:
                build_physical_operator_console(
                    repository_root=root,
                    session_id="cross-workspace-unsafe-r001",
                    run_id="cross-workspace-unsafe-run-r001",
                    operator_label="local-operator",
                    job_path=(
                        "config/data_factory/jobs/"
                        "center-live-24mm-20260903-r002.job.json"
                    ),
                    yaw0_sheet=workspace_bindings["PLACE_B"]["yaw0_sheet"],
                    motion_qualification_path=workspace_bindings["PLACE_B"][
                        "motion_qualification"
                    ],
                    home_candidate_path=(
                        "config/data_factory/home_candidates/"
                        "fr5-lab-a-tcp-r002-home-r001.json"
                    ),
                    collection_profile_path=(
                        "config/data_factory/collection_profiles/"
                        "fr5-up-wrist-rgb-30hz-v2.json"
                    ),
                    gripper_retune_path=None,
                    workspace_bindings=workspace_bindings,
                    discovery_call=lambda: devices,
                    selected_camera_bindings={
                        "up": devices[0], "wrist": devices[1],
                    },
                    gripper_readback_call=lambda: copy.deepcopy(opened),
                    activation_call=lambda: True,
                    task_id="pick_place", requested_count=1,
                    job_binding={
                        "place_id": "PLACE_B",
                        "cell_calibration_id": "place-b-yaw0-r001",
                        "object_profile_id": "wood-cube-24mm-r001",
                        "grasp_profile_id": (
                            "wood-cube-24mm-top-3p5mm-r001"
                        ),
                    },
                    initial_object_pose=unsafe_transition[0],
                    direct_pose_sequence=unsafe_transition,
                    clock=lambda: NOW,
                )
            self.assertEqual(raised.exception.code, "JOB_COORDINATE_BOUNDS")

    def test_physical_episode_consumes_hypothesis_resolver_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            motion = json.loads((
                root / "config/data_factory/motion_qualifications/"
                "fr5-place-a-wood-cube-r001.json"
            ).read_text(encoding="utf-8"))
            tcp = json.loads((
                root / "config/data_factory/test_only_physical/goal2-place1/"
                "tcp_candidate_manifest.json"
            ).read_text(encoding="utf-8"))
            identity = {
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_columns": [
                    [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                    [0.0, 0.0, 1.0],
                ],
            }
            snapshot = {
                "schema_version": "data_factory.pose_snapshot.v1",
                "frames": {"base": "base_link", "wrist": "wrist3_link"},
                "joint_positions_rad": dict(zip(
                    ("j1", "j2", "j3", "j4", "j5", "j6"),
                    motion["qualified_safe_joint_positions_rad"],
                )),
                "base_wrist": copy.deepcopy(identity),
                "base_tcp": {
                    **copy.deepcopy(identity),
                    "candidate_status": "CANDIDATE_MODEL_DERIVED",
                    "candidate_source_sha256": tcp["tcp_candidate_digest"],
                    "manifest_source_sha256": canonical_digest(tcp),
                },
                "joint_state_age_s": 0.01,
                "joint_stamp_ns": 1_000_000_000,
                "transform_stamp_ns": 1_000_000_000,
                "ros_sample_age_s": 0.01,
            }
            opened = {
                "active": True, "position_valid": True, "gripper_index": 1,
                "reference_position_m": 0.021, "feedback_position_m": 0.021,
                "sample_age_s": 0.0, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE",
            }
            scope_observed = {}
            runtime_observed = {}
            recorder_runtime_observed = {}
            motion_runtime_observed = {}

            def validate_scope_then_stop(
                payload, _cancel, publish, resolver, campaign_authorization,
                runtime_episode_binding, decision_provider,
                checkpoint_provider, dataset_validation_scope, **_kwargs,
            ):
                publish({"code": "PLANNING", "run_id": payload["run_id"]})
                runtime_observed.update(
                    console.bridge_core.snapshot()["projection"]["runtime"],
                )
                for code in (
                    "RECORDER_STARTING", "MOTION_STARTING", "EXECUTING",
                    "PRECONTACT_REVIEW", "GRASP_REVIEW", "SEMANTIC_REVIEW",
                    "RECYCLING", "RELEASE_REVIEW", "FINALIZING", "VALIDATING",
                ):
                    publish({"code": code, "run_id": payload["run_id"]})
                    runtime = console.bridge_core.snapshot()["projection"][
                        "runtime"
                    ]
                    recorder_runtime_observed[code] = copy.deepcopy(
                        runtime.get("recorder")
                    )
                    motion_runtime_observed[code] = copy.deepcopy(
                        runtime.get("motion")
                    )
                validated, program, _scene = resolver(payload)
                envelope = campaign_authorization["envelope"]
                job = validated["normalized_job"]
                digests = validated["input_digests"]
                base_close = validated["grasp_profile"]["gripper_close"]
                retune = load_json_strict(
                    root / "config/data_factory/test_only_physical/goal2-place1/"
                    "gripper-retune-wood-cube-25mm-top-center-r008.json"
                )
                scope_observed.update({
                    "incremental_validation": dataset_validation_scope
                    == "INCREMENTAL",
                    "trajectory_seed": payload[
                        run_job.TRAJECTORY_SAMPLING_SEED_KEY
                    ] == _domain_seed(
                        7,
                        "trajectory",
                        canonical_digest({
                            key: console.campaign_operator.manifest["slots"][0][key]
                            for key in (
                                "slot_id", "base_condition_digest",
                                "robot_start_pose_id", "split_group",
                                "repeat_index",
                            )
                        }),
                    ) and payload[
                        run_job.TRAJECTORY_SAMPLING_SEED_KEY
                    ] != 7,
                    "manifest": runtime_episode_binding["manifest_digest"]
                    == envelope["manifest_digest"],
                    "slot": runtime_episode_binding["slot_digest"]
                    in envelope["slot_digests"],
                    "start_pose": runtime_episode_binding["robot_start_pose_id"]
                    in envelope["allowed_start_pose_ids"],
                    "disposition": runtime_episode_binding["data_disposition"]
                    == envelope["data_disposition"],
                    "collection": digests["collection_profile"]
                    == envelope["collection_profile_digest"],
                    "motion": program["binding_digests"]["motion_qualification"]
                    == envelope["motion_qualification_digest"],
                    "retune_command": program["gripper_requirements"][
                        "command_position_m"
                    ] == 0.021 * 56 / 100,
                    "retune_feedback": program["gripper_requirements"][
                        "acceptable_feedback_m"
                    ] == {
                        "min": 0.021 * 56 / 100,
                        "max": 0.021 * 58 / 100,
                    },
                    "retune_open_velocity": program["gripper_requirements"][
                        "open_velocity_percent"
                    ] == 10,
                    "retune_close_step": next(
                        step for step in program["steps"]
                        if step["phase"] == "GRIPPER_CLOSE"
                    )["gripper_position_m"] == 0.021 * 56 / 100,
                    "retune_tolerance": next(
                        step for step in program["steps"]
                        if step["phase"] == "GRIPPER_CLOSE"
                    )["limits"]["completion_tolerance_m"]
                    == 0.021 * 58 / 100 - 0.021 * 56 / 100,
                    "qualified_grasp_unchanged": (
                        validated["grasp_profile"]["qualification_status"] == "QUALIFIED"
                        and base_close["command_position_m"] == 0.01134
                        and base_close["evidence_digest"]
                        != program["gripper_requirements"]["evidence_digest"]
                    ),
                    "retune_evidence": program["gripper_requirements"][
                        "evidence_digest"
                    ] == retune["retune_digest"],
                    **{
                        field: job[field] == envelope[field]
                        for field in (
                            "robot_system_id", "task", "object_profile_id",
                            "grasp_profile_id", "cell_calibration_id",
                        )
                    },
                })
                validate_runtime_campaign_scope(
                    campaign_authorization, resolved_inputs=validated,
                    motion_program=program,
                    episode_binding=runtime_episode_binding, now=NOW,
                )
                plan_digest = canonical_digest(["plan", payload["run_id"]])
                summary = {
                    "path": ["PREGRASP_PTP", "APPROACH_STOP_LIN"],
                    "flow": {
                        "continuous_through": "LIFT_LIN",
                        "next_human_hold": "POST_LIFT_SEMANTIC",
                    },
                    "speed": {
                        "max_velocity_scaling": 0.03,
                        "max_acceleration_scaling": 0.03,
                    },
                    "clearance": {
                        "status": "COLLISION_CHECKED_NO_DISTANCE",
                        "collision_report_digest": canonical_digest("collision"),
                    },
                }
                site = checkpoint_provider({
                    "schema_version": "data_factory.operator_checkpoint_request.v1",
                    "kind": "PHYSICAL_SCENE_CONFIRMATION",
                    "run_id": payload["run_id"], "plan_digest": plan_digest,
                    "prompt": "Confirm the exact TEST_ONLY scene",
                    "choices": ["READY", "CANCEL"],
                    "evidence": {
                        "checklist": {"place_alias": "place1"},
                        "operator_summary": summary,
                        "planned_start_evidence_digest": canonical_digest("start"),
                        "data_disposition": "TEST_ONLY",
                    },
                    "timeout_s": 2.0,
                })
                decision = decision_provider({
                    "schema_version": "data_factory.plan_decision_request.v1",
                    "run_id": payload["run_id"], "plan_digest": plan_digest,
                    "approval_scope": "HIL_NUMERIC_PROXY",
                    "decision_binding": {
                        "operator_summary": summary,
                        "data_disposition": "TEST_ONLY",
                        "episode_binding": copy.deepcopy(runtime_episode_binding),
                        "object_reposition_preapproval": None,
                    },
                    "timeout_s": 2.0,
                })
                scope_observed.update({
                    "site_before_plan": site["choice"] == "READY",
                    "campaign_plan": decision["choice"] == "APPROVE",
                })
                raise ContractError("EXPECTED_LIVE_BOUND")

            live = mock.Mock(side_effect=validate_scope_then_stop)
            runtime_gate = threading.Barrier(2)

            def activate_runtime_gate():
                runtime_gate.wait(timeout=1)
                return True

            def read_runtime_gripper():
                runtime_gate.wait(timeout=1)
                return copy.deepcopy(opened)

            console, _ = build_physical_operator_console(
                repository_root=root,
                session_id="goal2-physical-receipt-r001",
                run_id="goal2-place1-receipt-r001",
                operator_label="local-operator",
                discovery_call=lambda: ["usb-Goal2_Camera-video-index0"],
                activation_call=activate_runtime_gate,
                snapshot_call=lambda: copy.deepcopy(snapshot),
                gripper_readback_call=read_runtime_gripper,
                run_live_call=live,
                environment_prepared=True,
                normalized_seed=7,
                clock=lambda: NOW,
            )
            try:
                current = console.bridge_core.snapshot()
                compiled = console.bridge_core.consume(envelope(
                    current, "compile_draft", {
                        "draft_id": current["projection"]["draft"]["draft_id"],
                        "data_disposition": "TEST_ONLY",
                    }, "compile-physical-receipt-r001",
                ))["result"]
                review = console.bridge_core.snapshot()
                console.bridge_core.consume(envelope(
                    review, "authorize_campaign", {
                        "draft_id": review["projection"]["draft"]["draft_id"],
                        "manifest_digest": compiled["manifest_digest"],
                        "envelope_digest": compiled["envelope_digest"],
                        "data_disposition": "TEST_ONLY",
                    }, "authorize-physical-receipt-r001",
                ))
                retune = load_json_strict(
                    root / "config/data_factory/test_only_physical/goal2-place1/"
                    "gripper-retune-wood-cube-25mm-top-center-r008.json"
                )
                fixed = console.campaign_operator.hypothesis["fixed_contract"]
                self.assertEqual(
                    fixed["trajectory_digest"],
                    canonical_digest({
                        "motion_qualification": fixed["motion_recipe_digest"],
                        "recipe": "DIRECT",
                        "test_only_gripper_retune": retune["retune_digest"],
                    }),
                )
                self.assertEqual(
                    console.wait_for_episode(1.0)["code"], "EXPECTED_LIVE_BOUND",
                )
                self.assertEqual(
                    scope_observed, dict.fromkeys(scope_observed, True),
                )
                self.assertEqual(runtime_observed["phase"], "PLANNING")
                self.assertEqual(runtime_observed["phase_label"], "경로 계획 및 충돌 검사")
                self.assertNotIn("recorder", runtime_observed)
                self.assertEqual(runtime_observed["motion"]["status"], "NOT_AUTHORIZED")
                self.assertEqual(recorder_runtime_observed, {
                    "RECORDER_STARTING": {
                        "status": "CONNECTING", "label": "기록 준비 중",
                    },
                    "MOTION_STARTING": {"status": "RECORDING", "label": "기록 중"},
                    "EXECUTING": {"status": "RECORDING", "label": "기록 중"},
                    "PRECONTACT_REVIEW": {"status": "RECORDING", "label": "기록 중"},
                    "GRASP_REVIEW": {"status": "RECORDING", "label": "기록 중"},
                    "SEMANTIC_REVIEW": {"status": "FROZEN", "label": "녹화 완료"},
                    "RECYCLING": {"status": "FROZEN", "label": "녹화 완료"},
                    "RELEASE_REVIEW": {"status": "FROZEN", "label": "녹화 완료"},
                    "FINALIZING": {"status": "FROZEN", "label": "녹화 완료"},
                    "VALIDATING": {"status": "COMMITTED", "label": "저장 완료"},
                })
                self.assertEqual(
                    [motion_runtime_observed[code]["status"] for code in (
                        "RECORDER_STARTING", "MOTION_STARTING", "EXECUTING",
                        "PRECONTACT_REVIEW", "GRASP_REVIEW", "SEMANTIC_REVIEW",
                        "RECYCLING", "RELEASE_REVIEW", "FINALIZING", "VALIDATING",
                    )],
                    [
                        "NOT_AUTHORIZED", "DISPATCHING", "ACTIVE",
                        "PAUSED_AT_GATE", "PAUSED_AT_GATE", "PAUSED_AT_GATE",
                        "ACTIVE_POST_RECORDING", "PAUSED_AT_GATE", "COMPLETE",
                        "COMPLETE",
                    ],
                )
                live.assert_called_once()
            finally:
                console.close()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            console, _ = build_physical_operator_console(
                repository_root=root,
                session_id="goal2-physical-unavailable-r001",
                run_id="goal2-place1-unavailable-r001",
                operator_label="local-operator",
                discovery_call=lambda: ["usb-Goal2_Camera-video-index0"],
                activation_call=lambda: True,
                gripper_readback_call=lambda: (_ for _ in ()).throw(
                    ContractError("GRIPPER_SETUP_NOT_AVAILABLE")
                ),
                clock=lambda: NOW,
            )
            try:
                projection = console.bridge_core.snapshot()["projection"]
                self.assertEqual(
                    (projection["setup"]["host_status"],
                     projection["runtime"]["workflow_state"],
                     projection["runtime"]["measurement_outcome"],
                     projection["available_ops"]),
                    ("BLOCKED", "BLOCKED", "NOT_AVAILABLE", []),
                )
                self.assertIsNone(console.episode_worker)
            finally:
                console.close()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            wrong_index = {
                "active": True, "position_valid": True, "gripper_index": 2,
                "reference_position_m": 0.021, "feedback_position_m": 0.021,
                "sample_age_s": 0.0, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE",
            }
            console, _ = build_physical_operator_console(
                repository_root=root,
                session_id="goal2-physical-binding-r001",
                run_id="goal2-place1-binding-r001",
                operator_label="local-operator",
                discovery_call=lambda: ["usb-Goal2_Camera-video-index0"],
                activation_call=lambda: True,
                gripper_readback_call=lambda: copy.deepcopy(wrong_index),
                clock=lambda: NOW,
            )
            try:
                projection = console.bridge_core.snapshot()["projection"]
                self.assertEqual(
                    (projection["setup"]["host_status"],
                     projection["runtime"]["workflow_state"],
                     projection["runtime"]["measurement_outcome"],
                     projection["available_ops"]),
                    ("BLOCKED", "BLOCKED", "FAIL", []),
                )
                self.assertIsNone(console.episode_worker)
            finally:
                console.close()

    def test_product_application_defers_campaign_roots_until_environment_and_compile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            default_job = (
                root / "config/data_factory/test_only_physical/goal2-place1"
                / "center-live-p45-20260821-r001.job.json"
            )
            duplicate_job = load_json_strict(default_job)
            duplicate_job["job_id"] = "same-pose-other-job-r001"
            default_job.with_name("same-pose-other-job-r001.job.json").write_text(
                json.dumps(duplicate_job), encoding="utf-8",
            )
            environment = {
                "schema_version": "data_factory.operator_environment.v1",
                "state": "SETUP_REQUIRED",
                "observed_at": "2026-08-26T03:00:00Z",
                "components": {
                    name: {"state": "MISSING", "owner": None, "reason": "NOT_PREPARED"}
                    for name in ("robot", "controller", "gripper", "camera")
                },
            }

            def prepare():
                environment["state"] = "READY"
                environment["components"] = {
                    name: {"state": "READY", "owner": f"owner-{name}", "reason": "ATTACHED"}
                    for name in environment["components"]
                }
                return copy.deepcopy(environment)

            opened = {
                "active": True, "position_valid": True, "gripper_index": 1,
                "reference_position_m": 0.021, "feedback_position_m": 0.021,
                "sample_age_s": 0.0, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE",
            }
            device = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
            other_device = "usb-Generic_USB2.0_PC_CAMERA_2-video-index0"
            activation = mock.Mock(return_value=True)
            gripper_readback = mock.Mock(return_value=copy.deepcopy(opened))
            application, context = build_physical_operator_application(
                repository_root=root,
                session_id="product-application-r001",
                operator_label="local-operator",
                environment_call=lambda: copy.deepcopy(environment),
                prepare_environment_call=prepare,
                selected_camera_device_id=device,
                discovery_call=lambda: [device, other_device],
                activation_call=activation,
                gripper_readback_call=gripper_readback,
                run_live_call=mock.Mock(side_effect=AssertionError("live before authorization")),
                clock=lambda: NOW,
            )
            try:
                selected_combination = next(
                    item for item in application.catalog["combinations"]
                    if item["combination_digest"]
                    == application.selection["combination_digest"]
                )
                self.assertEqual(
                    selected_combination["sources"]["job"],
                    str(default_job.relative_to(root)),
                )
                self.assertFalse((root / "outputs").exists())
                initial = application.bridge_core.snapshot()
                self.assertEqual(
                    initial["projection"]["runtime"]["workflow_state"], "PREPARING",
                )
                application.bridge_core.consume(envelope(
                    initial, "prepare_environment", {}, "prepare-product-r001",
                ))
                self.assertFalse((root / "outputs").exists())
                authoring = application.bridge_core.snapshot()
                draft = authoring["projection"]["draft"]
                camera_options = authoring["projection"]["catalog"]["axes"]["camera"]
                self.assertLessEqual(
                    {
                        f"fr5-up-rgb-30hz-v1@{device}",
                        f"fr5-up-rgb-30hz-v1@{other_device}",
                    },
                    {item["id"] for item in camera_options},
                )
                camera_by_id = {item["id"]: item for item in camera_options}
                self.assertTrue(
                    camera_by_id[f"fr5-up-rgb-30hz-v1@{device}"]["available"],
                )
                inactive = camera_by_id[f"fr5-up-rgb-30hz-v1@{other_device}"]
                self.assertFalse(inactive["available"])
                self.assertEqual(inactive["reason"], "CAMERA_REBIND_REQUIRED")
                with self.assertRaisesRegex(
                    ContractError, "OPERATOR_APPLICATION_SELECTION",
                ):
                    application.bridge_core.consume(envelope(
                        authoring, "update_draft", {
                            "draft_id": draft["draft_id"],
                            "selection": {
                                "camera": f"fr5-up-rgb-30hz-v1@{other_device}",
                            },
                        }, "switch-unprepared-camera-r001",
                    ))
                self.assertFalse((root / "outputs").exists())
                authoring = application.bridge_core.snapshot()
                current_object_pose = {
                    "place_id": "PLACE_A", "yaw_deg": -37.5,
                    "x_mm": 12.5, "y_mm": -7.25,
                }
                application.bridge_core.consume(envelope(
                    authoring, "update_draft", {
                        "draft_id": authoring["projection"]["draft"]["draft_id"],
                        "current_object_pose": current_object_pose,
                    }, "set-current-object-r001",
                ))
                authoring = application.bridge_core.snapshot()
                application.bridge_core.consume(envelope(
                    authoring, "update_draft", {
                        "draft_id": authoring["projection"]["draft"]["draft_id"],
                        "requested_count": 1,
                    }, "repeat-pose-count-one-r001",
                ))
                authoring = application.bridge_core.snapshot()
                application.bridge_core.consume(envelope(
                    authoring, "update_draft", {
                        "draft_id": authoring["projection"]["draft"]["draft_id"],
                        "authoring_mode": "DIRECT_EDIT",
                    }, "repeat-pose-direct-r001",
                ))
                authoring = application.bridge_core.snapshot()
                application.bridge_core.consume(envelope(
                    authoring, "update_draft", {
                        "draft_id": authoring["projection"]["draft"]["draft_id"],
                        "requested_count": 3,
                    }, "repeat-pose-count-three-r001",
                ))
                authoring = application.bridge_core.snapshot()
                self.assertEqual(authoring["projection"]["draft"]["direct_poses"], [])
                materialized_poses = [
                    {
                        key: pair[key]
                        for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
                    }
                    for pair in authoring["projection"]["draft"]["direct_pairs"]
                ]
                self.assertEqual(materialized_poses[0], current_object_pose)
                probe_draft = copy.deepcopy(application.draft)
                probe_draft["draft_id"] = "repeat-pose-probe-r001-draft"
                repeat_probe = application.campaign_factory(
                    "repeat-pose-probe-r001", copy.deepcopy(application.selection),
                    probe_draft,
                )
                try:
                    repeat_probe.compile_draft({
                        "draft_id": probe_draft["draft_id"],
                        "data_disposition": "TEST_ONLY",
                    }, {})
                    probe_owner = repeat_probe.campaign_operator
                    probe_bases = {
                        item["base_condition_digest"]: item
                        for item in probe_owner.hypothesis["base_conditions"]
                    }
                    probe_resolvers = {
                        item["resolved_job_digest"]: item
                        for item in probe_owner.hypothesis["resolver_receipts"]
                    }
                    probe_poses = []
                    for slot in probe_owner.manifest["slots"]:
                        job = probe_resolvers[
                            probe_bases[slot["base_condition_digest"]][
                                "resolved_job_digest"
                            ]
                        ]["normalized_job"]
                        probe_poses.append({
                            key: job[key]
                            for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
                        })
                    self.assertEqual(probe_poses, materialized_poses)
                finally:
                    repeat_probe.close()
                direct_poses = [
                    {"place_id": "PLACE_A", "yaw_deg": 45, "x_mm": 10, "y_mm": 5},
                    {"place_id": "PLACE_A", "yaw_deg": -180, "x_mm": -10, "y_mm": -5},
                ]
                for index, pose in enumerate(direct_poses, 1):
                    replaced = authoring["projection"]["draft"]["direct_pairs"][1]
                    application.bridge_core.consume(envelope(
                        authoring, "update_draft", {
                            "draft_id": authoring["projection"]["draft"]["draft_id"],
                            "remove_pair": replaced,
                        }, f"remove-direct-pair-r{index:03d}",
                    ))
                    authoring = application.bridge_core.snapshot()
                    application.bridge_core.consume(envelope(
                        authoring, "update_draft", {
                            "draft_id": authoring["projection"]["draft"]["draft_id"],
                            "add_pair": {
                                "start_pose_id": replaced["start_pose_id"], **pose,
                            },
                        }, f"add-direct-pair-r{index:03d}",
                    ))
                    authoring = application.bridge_core.snapshot()
                compiled = application.bridge_core.consume(envelope(
                    authoring, "compile_draft", {
                        "draft_id": draft["draft_id"],
                        "data_disposition": "TEST_ONLY",
                    }, "compile-product-r001",
                ))["result"]
                self.assertEqual(compiled["outcome"], "REVIEW_CAMPAIGN")
                activation.assert_not_called()
                gripper_readback.assert_not_called()
                self.assertTrue((
                    root / "outputs/data_factory/test_only_physical"
                    / "product-application-r001-campaign-0001"
                ).is_dir())
                campaign = application._campaign.campaign_operator
                bases = {
                    item["base_condition_digest"]: item
                    for item in campaign.hypothesis["base_conditions"]
                }
                resolvers = {
                    item["resolved_job_digest"]: item
                    for item in campaign.hypothesis["resolver_receipts"]
                }
                observed = []
                for slot in campaign.manifest["slots"]:
                    job = resolvers[
                        bases[slot["base_condition_digest"]]["resolved_job_digest"]
                    ]["normalized_job"]
                    observed.append({
                        key: job[key]
                        for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
                    })
                self.assertEqual(observed, [
                    current_object_pose,
                    *direct_poses,
                ])
                coverage = application.bridge_core.snapshot()["projection"][
                    "coverage"
                ]["cells"]
                self.assertEqual([
                    {
                        key: cell[key]
                        for key in ("x_mm", "y_mm", "yaw_deg", "target_count")
                    }
                    for cell in coverage
                ], [
                    {"x_mm": 12.5, "y_mm": -7.25, "yaw_deg": -37.5, "target_count": 1},
                    {"x_mm": 10, "y_mm": 5, "yaw_deg": 45, "target_count": 1},
                    {"x_mm": -10, "y_mm": -5, "yaw_deg": -180, "target_count": 1},
                ])
                self.assertEqual(context["production_writers_enabled"], False)
                self.assertIsNone(application._campaign.episode_worker)
            finally:
                application.close()

    def test_physical_application_uses_shared_continuous_assisted_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            environment = {
                "schema_version": "data_factory.operator_environment.v1",
                "state": "SETUP_REQUIRED",
                "observed_at": "2026-08-26T03:00:00Z",
                "components": {
                    name: {"state": "MISSING", "owner": None, "reason": "NOT_PREPARED"}
                    for name in ("robot", "controller", "gripper", "camera")
                },
            }

            def prepare():
                environment["state"] = "READY"
                environment["components"] = {
                    name: {"state": "READY", "owner": f"owner-{name}", "reason": "ATTACHED"}
                    for name in environment["components"]
                }
                return copy.deepcopy(environment)

            opened = {
                "active": True, "position_valid": True, "gripper_index": 1,
                "reference_position_m": 0.021, "feedback_position_m": 0.021,
                "sample_age_s": 0.0, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE",
            }
            device = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
            live = mock.Mock(side_effect=AssertionError("live before authorization"))
            application, _context = build_physical_operator_application(
                repository_root=root,
                session_id="assisted-projection-physical-r001",
                operator_label="local-operator",
                environment_call=lambda: copy.deepcopy(environment),
                prepare_environment_call=prepare,
                selected_camera_device_id=device,
                discovery_call=lambda: [device],
                activation_call=lambda: True,
                gripper_readback_call=lambda: copy.deepcopy(opened),
                run_live_call=live,
                clock=lambda: NOW,
            )
            try:
                current = application.bridge_core.snapshot()
                application.bridge_core.consume(envelope(
                    current, "prepare_environment", {}, "assisted-prepare-r001",
                ))
                authoring = application.bridge_core.snapshot()
                draft = authoring["projection"]["draft"]
                master_seed = application.draft["normalized_seed"]
                with (
                    mock.patch(
                        "tools.data_factory.operator.composition.project_assisted_poses",
                        wraps=project_assisted_poses,
                    ) as assisted_projection,
                    mock.patch(
                        "tools.data_factory.operator.composition."
                        "project_balanced_start_pose_ids",
                        wraps=project_balanced_start_pose_ids,
                    ) as start_projection,
                ):
                    compiled = application.bridge_core.consume(envelope(
                        authoring, "compile_draft", {
                            "draft_id": draft["draft_id"],
                            "data_disposition": "TEST_ONLY",
                        }, "assisted-compile-r001",
                    ))["result"]
                assisted_projection.assert_called_once()
                self.assertEqual(assisted_projection.call_args.args[3], 3)
                self.assertEqual(
                    assisted_projection.call_args.kwargs,
                    {
                        "repeat": 1,
                        "normalized_seed": _domain_seed(master_seed, "spatial"),
                        "yaw_sampling_seed": _domain_seed(master_seed, "yaw"),
                    },
                )
                start_projection.assert_called_once_with(
                    application.draft["selected_start_pose_ids"], 3,
                    normalized_seed=_domain_seed(master_seed, "start_pose"),
                )
                self.assertEqual(compiled["episode_count"], 3)
                campaign = application._campaign.campaign_operator
                bases = {
                    item["base_condition_digest"]: item["coverage_condition"]
                    for item in campaign.hypothesis["base_conditions"]
                }
                selected = [
                    bases[item["base_condition_digest"]]
                    for item in campaign.manifest["slots"]
                ]
                poses = [
                    tuple(item[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm"))
                    for item in selected
                ]
                presets = {
                    tuple(item["metadata"][key] for key in (
                        "place_id", "yaw_deg", "x_mm", "y_mm",
                    ))
                    for item in application.catalog["axes"]["cell"]
                    if item["metadata"].get("place_id") == "PLACE_A"
                }
                self.assertEqual(poses[0], ("PLACE_A", 0, 0, 0))
                self.assertEqual(len(set(poses)), 3)
                self.assertTrue(all(item not in presets for item in poses[1:]))
                self.assertIsNone(application._campaign.episode_worker)
                live.assert_not_called()
            finally:
                application.close()

    def test_product_workspace_registration_captures_previews_saves_and_refreshes_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            device = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
            environment = {
                "schema_version": "data_factory.operator_environment.v1",
                "state": "SETUP_REQUIRED",
                "observed_at": "2026-08-26T03:00:00Z",
                "components": {
                    name: {"state": "MISSING", "owner": None, "reason": "NOT_PREPARED"}
                    for name in ("robot", "controller", "gripper", "camera")
                },
            }

            def prepare():
                environment["state"] = "READY"
                environment["components"] = {
                    name: {"state": "READY", "owner": f"owner-{name}", "reason": "ATTACHED"}
                    for name in environment["components"]
                }
                return copy.deepcopy(environment)

            tcp_manifest = json.loads((
                root / "config/data_factory/test_only_physical/goal2-place1/"
                "tcp_candidate_manifest.json"
            ).read_text(encoding="utf-8"))
            tcp_digest = tcp_manifest["tcp_candidate_digest"]
            manifest_digest = canonical_digest(tcp_manifest)
            identity = {
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_columns": [
                    [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                    [0.0, 0.0, 1.0],
                ],
            }

            def pose(point):
                return {
                    "schema_version": "data_factory.pose_snapshot.v1",
                    "frames": {"base": "base_link", "wrist": "wrist3_link"},
                    "joint_positions_rad": {
                        name: 0.0 for name in ("j1", "j2", "j3", "j4", "j5", "j6")
                    },
                    "base_wrist": copy.deepcopy(identity),
                    "base_tcp": {
                        **copy.deepcopy(identity),
                        "translation_m": point,
                        "candidate_status": "CANDIDATE_MODEL_DERIVED",
                        "candidate_source_sha256": tcp_digest,
                        "manifest_source_sha256": manifest_digest,
                    },
                    "joint_state_age_s": 0.05,
                    "joint_stamp_ns": 1_000_000_000,
                    "transform_stamp_ns": 1_000_000_000,
                    "ros_sample_age_s": 0.05,
                }

            snapshots = iter((
                pose([1.0, 2.0, 3.0]),
                pose([1.1285, 2.0, 3.0]),
                pose([0.8715, 2.08, 3.0]),
            ))
            live = mock.Mock(side_effect=AssertionError("workspace registration called live"))
            application, _context = build_physical_operator_application(
                repository_root=root,
                session_id="workspace-physical-application-r001",
                operator_label="local-operator",
                environment_call=lambda: copy.deepcopy(environment),
                prepare_environment_call=prepare,
                selected_camera_device_id=device,
                discovery_call=lambda: [device],
                snapshot_call=lambda: next(snapshots),
                run_live_call=live,
                clock=lambda: NOW,
            )
            try:
                current = application.bridge_core.snapshot()
                application.bridge_core.consume(envelope(
                    current, "prepare_environment", {}, "workspace-prepare-r001",
                ))
                self.assertFalse((root / "outputs").exists())
                current = application.bridge_core.snapshot()
                application.bridge_core.consume(envelope(
                    current, "new_workspace_registration", {
                        "display_name": "Fixture Workspace",
                    }, "workspace-new-r001",
                ))

                for label in ("CENTER", "X_REF", "Y_CHECK"):
                    current = application.bridge_core.snapshot()
                    application.bridge_core.consume(envelope(
                        current, "capture_workspace_point", {"label": label},
                        f"workspace-capture-{label.lower()}-r001",
                    ))
                captured = application.bridge_core.snapshot()
                registration = captured["projection"]["workspace_registration"]
                self.assertTrue(all(registration["captures"].values()))
                self.assertNotIn("joint_positions_rad", str(registration))

                application.bridge_core.consume(envelope(
                    captured, "preview_workspace", {
                        "source_scale_bar_mm": 96.0,
                        "final_scale_bar_mm": 100.0,
                    }, "workspace-preview-r001",
                ))
                previewed = application.bridge_core.snapshot()
                preview = previewed["projection"]["workspace_registration"]["preview"]
                self.assertEqual(preview["status"], "CANDIDATE_WITHIN_TOLERANCE")
                self.assertFalse(preview["execution_authorized"])

                application.bridge_core.consume(envelope(
                    previewed, "save_workspace", {
                        "preview_digest": preview["preview_digest"],
                    }, "workspace-save-r001",
                ))
                saved = application.bridge_core.snapshot()["projection"]
                calibration_id = saved["workspace_registration"]["promotion"][
                    "calibration_id"
                ]
                self.assertIn(
                    calibration_id,
                    {item["id"] for item in saved["catalog"]["axes"]["frame"]},
                )
                self.assertEqual(len(saved["workspace_registration"]["history"]), 1)
                frame = next(
                    item for item in saved["catalog"]["axes"]["frame"]
                    if item["id"] == calibration_id
                )
                self.assertTrue(frame["available"])
                self.assertFalse(frame["execution_ready"])
                self.assertEqual(
                    frame["execution_reason"], "MOTION_QUALIFICATION_REQUIRED",
                )
                promotion = saved["workspace_registration"]["promotion"]
                selected_camera_digest = saved["selection"]["camera_binding_digest"]
                self.assertEqual(
                    load_json_strict(
                        root / "config/data_factory"
                        / promotion["yaw0_sheet_relative_path"],
                    )["print_calibration"]["measured_scale_bar_mm"],
                    96.0,
                )

                current = application.bridge_core.snapshot()
                application.bridge_core.consume(envelope(
                    current, "update_draft", {
                        "draft_id": current["projection"]["draft"]["draft_id"],
                        "selection": {"frame": calibration_id},
                    }, "workspace-select-r001",
                ))
                selected = application.bridge_core.snapshot()
                self.assertEqual(
                    selected["projection"]["selection"]["frame_id"], calibration_id,
                )
                self.assertEqual(
                    selected["projection"]["selection"]["camera_binding_digest"],
                    selected_camera_digest,
                )
                self.assertFalse(selected["projection"]["draft"]["execution_ready"])
                self.assertNotIn(
                    "compile_draft", selected["projection"]["available_ops"],
                )
                application.bridge_core.consume(envelope(
                    selected, "update_draft", {
                        "draft_id": selected["projection"]["draft"]["draft_id"],
                        "add_pose": {
                            "place_id": selected["projection"]["selection"]["workspace_id"],
                            "yaw_deg": 33,
                            "x_mm": 10, "y_mm": 5,
                        },
                    }, "workspace-pose-r001",
                ))
                blocked = application.bridge_core.snapshot()
                with self.assertRaisesRegex(
                    ContractError, "OPERATOR_INTENT_OP",
                ):
                    application.bridge_core.consume(envelope(
                        blocked, "compile_draft", {
                            "draft_id": blocked["projection"]["draft"]["draft_id"],
                            "data_disposition": "TEST_ONLY",
                        }, "workspace-compile-blocked-r001",
                    ))
                self.assertFalse((
                    root / "outputs/data_factory/test_only_physical"
                ).exists())
                live.assert_not_called()
            finally:
                application.close()

    def test_physical_main_serves_blocked_shell_with_zero_cameras_and_zero_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            captured = {}

            class ShellBridge:
                def __init__(self, *, core, **_kwargs):
                    captured["view"] = core.snapshot()["projection"]
                    self.origin = "http://127.0.0.1:4174"
                    self.server = mock.Mock()

                def serve_forever(self, startup_call=None):
                    self.startup_call = startup_call
                    captured["startup_call"] = startup_call
                    captured["served"] = True

            physical_environment = mock.Mock(
                side_effect=AssertionError("zero-camera shell started hardware"),
            )
            with (
                mock.patch(
                    "tools.data_factory.operator.composition.discover_camera_devices",
                    return_value=[],
                ),
                mock.patch(
                    "tools.data_factory.operator.composition.LoopbackBridge", ShellBridge,
                ),
                mock.patch(
                    "tools.data_factory.operator.setup.physical."
                    "build_physical_operator_environment",
                    physical_environment,
                ),
                mock.patch("builtins.print"),
            ):
                result = operator_console_main([
                    "--effect-scope", "PHYSICAL",
                    "--repository-root", str(root),
                    "--session-id", "zero-camera-shell-r001",
                ])

            self.assertEqual(result, 0)
            self.assertTrue(captured["served"])
            self.assertIsNone(captured["startup_call"])
            view = captured["view"]
            self.assertEqual(view["runtime"]["workflow_state"], "BLOCKED")
            self.assertEqual(view["available_ops"], [])
            self.assertEqual(view["environment"]["components"]["camera"], {
                "state": "MISSING", "owner": None,
                "reason": "DEVICE_NOT_CONNECTED",
            })
            camera = view["catalog"]["axes"]["camera"]
            self.assertTrue(camera)
            self.assertTrue(all(
                item["available"] is False
                and item["reason"] == "DEVICE_NOT_CONNECTED"
                for item in camera
            ))
            self.assertIsNone(view["campaign"])
            self.assertEqual(view["effect_counts"], {})
            self.assertFalse((root / "outputs").exists())
            physical_environment.assert_not_called()

    def test_physical_runtime_reuses_the_initial_environment_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            device = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
            descriptor = {
                "logical_id": device,
                "label": "USB camera 1",
                "status": "CONNECTED",
                "kind": "UVC",
                "capture_endpoint": f"/dev/v4l/by-id/{device}",
            }
            observed = {
                "schema_version": "data_factory.operator_environment.v1",
                "state": "SETUP_REQUIRED",
                "observed_at": "2026-08-31T03:00:00Z",
                "components": {
                    name: {
                        "state": "MISSING", "owner": None,
                        "reason": "NOT_RUNNING",
                    }
                    for name in ("robot", "controller", "gripper", "camera")
                },
            }
            environment = mock.Mock()
            environment.projection.return_value = copy.deepcopy(observed)

            class ShellBridge:
                def __init__(self, *, core, **_kwargs):
                    self.core = core
                    self.origin = "http://127.0.0.1:4174"
                    self.server = mock.Mock()

            with (
                mock.patch(
                    "tools.data_factory.operator.composition.discover_camera_devices",
                    return_value=[descriptor],
                ),
                mock.patch(
                    "tools.data_factory.operator.composition."
                    "build_physical_operator_environment",
                    return_value=environment,
                ) as environment_builder,
                mock.patch(
                    "tools.data_factory.operator.composition.LoopbackBridge",
                    ShellBridge,
                ),
            ):
                runtime = build_physical_runtime(
                    repository_root=root,
                    session_id="single-observation-r001",
                    camera_device_id=device,
                    data_mode="TEST_COLLECTION",
                    gripper_retune=(
                        "config/data_factory/test_only_physical/goal2-place1/"
                        "gripper-retune-wood-cube-25mm-top-center-r008.json"
                    ),
                    auto_prepare=False,
                )
            try:
                self.assertEqual(runtime.announcement["environment_state"], "SETUP_REQUIRED")
                environment.projection.assert_called_once_with()
                self.assertEqual(
                    environment_builder.call_args.kwargs[
                        "gripper_velocity_percent"
                    ],
                    20,
                )
                self.assertEqual(
                    environment_builder.call_args.kwargs["gripper_force_percent"],
                    20,
                )
                self.assertEqual(
                    environment_builder.call_args.kwargs[
                        "gripper_open_velocity_percent"
                    ],
                    10,
                )
                self.assertEqual(
                    environment_builder.call_args.kwargs[
                        "gripper_open_force_percent"
                    ],
                    50,
                )
            finally:
                runtime.close()

    def test_product_application_auto_selects_one_stable_camera_without_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            device = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
            blocked = {
                "schema_version": "data_factory.operator_environment.v1",
                "state": "BLOCKED", "observed_at": "2026-08-26T03:00:00Z",
                "components": {
                    name: {"state": "BLOCKED", "owner": None, "reason": "TEST_BLOCK"}
                    for name in ("robot", "controller", "gripper", "camera")
                },
            }
            environment_query = mock.Mock(side_effect=AssertionError(
                "cached product view repeated physical environment discovery",
            ))
            catalog = load_operator_catalog(root, device_ids=[device])
            discovery = mock.Mock(side_effect=AssertionError(
                "initial catalog snapshot repeated camera discovery",
            ))
            application, context = build_physical_operator_application(
                repository_root=root,
                session_id="one-camera-auto-select-r001",
                operator_label="local-operator",
                environment_call=environment_query,
                prepare_environment_call=lambda: self.fail("prepare was exposed"),
                initial_environment=blocked,
                initial_catalog=catalog,
                discovery_call=discovery,
                run_live_call=mock.Mock(side_effect=AssertionError("live was called")),
                clock=lambda: NOW,
            )
            try:
                view = application.bridge_core.snapshot()["projection"]
                self.assertEqual(context["camera_device_id"], device)
                self.assertEqual(view["selection"]["camera_device_id"], device)
                selected_camera = next(
                    item for item in view["catalog"]["axes"]["camera"]
                    if item["id"]
                    == f"fr5-up-rgb-30hz-v1@{device}"
                )
                self.assertTrue(selected_camera["available"])
                self.assertEqual(view["available_ops"], [])
                self.assertFalse((root / "outputs").exists())
                environment_query.assert_not_called()
                discovery.assert_not_called()
            finally:
                application.close()

    def test_product_application_exposes_only_the_active_job_handling_family(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            device = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
            blocked = {
                "schema_version": "data_factory.operator_environment.v1",
                "state": "BLOCKED", "observed_at": "2026-08-26T03:00:00Z",
                "components": {
                    name: {
                        "state": "BLOCKED", "owner": None,
                        "reason": "TEST_BLOCK",
                    }
                    for name in ("robot", "controller", "gripper", "camera")
                },
            }
            application, _context = build_physical_operator_application(
                repository_root=root,
                session_id="active-handling-family-r001",
                operator_label="local-operator",
                environment_call=lambda: copy.deepcopy(blocked),
                prepare_environment_call=lambda: copy.deepcopy(blocked),
                initial_environment=blocked,
                initial_catalog=load_operator_catalog(root, device_ids=[device]),
                job_path=(
                    "config/data_factory/jobs/"
                    "center-live-24mm-20260903-r002.job.json"
                ),
                run_live_call=mock.Mock(
                    side_effect=AssertionError("live was called"),
                ),
                clock=lambda: NOW,
            )
            try:
                view = application.bridge_core.snapshot()["projection"]
                self.assertEqual(
                    [item["id"] for item in view["catalog"]["axes"]["object"]],
                    ["wood-cube-24mm-r001"],
                )
                self.assertEqual(
                    [item["id"] for item in view["catalog"]["axes"]["grasp"]],
                    ["wood-cube-24mm-top-3p5mm-r001"],
                )
                self.assertEqual(
                    {
                        item["sources"]["job"]
                        for item in application.catalog["combinations"]
                    },
                    {
                        "config/data_factory/jobs/"
                        "center-live-24mm-20260903-r002.job.json"
                    },
                )
                self.assertFalse((root / "outputs").exists())
            finally:
                application.close()

    def test_product_application_wires_explicit_home_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            device = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
            ready = {
                "schema_version": "data_factory.operator_environment.v1",
                "state": "READY", "observed_at": "2026-08-26T03:00:00Z",
                "components": {
                    name: {"state": "READY", "owner": f"owner-{name}", "reason": "ATTACHED"}
                    for name in ("robot", "controller", "gripper", "camera")
                },
            }
            recovery = {
                "schema_version": "data_factory.home_recovery.v1",
                "status": "HOME", "arm_goal_count": 1,
                "gripper_open": True, "target_rad": [0.0] * 6,
                "final_rad": [0.0] * 6,
                "motion_qualification_digest": canonical_digest(["motion"]),
            }
            recover = mock.Mock(return_value=copy.deepcopy(recovery))
            application, _context = build_physical_operator_application(
                repository_root=root,
                session_id="wired-home-recovery-r001",
                operator_label="local-operator",
                environment_call=lambda: copy.deepcopy(ready),
                prepare_environment_call=lambda: copy.deepcopy(ready),
                initial_environment=ready,
                initial_catalog=load_operator_catalog(root, device_ids=[device]),
                home_recovery_call=recover,
                run_live_call=mock.Mock(side_effect=AssertionError("live was called")),
                clock=lambda: NOW,
            )
            try:
                before = application.bridge_core.snapshot()
                result = application.bridge_core.consume(envelope(
                    before, "recover_home", {}, "wired-home-recovery",
                ))["result"]
                self.assertEqual(result["outcome"], "HOME")
                self.assertEqual(result["home_recovery"], recovery)
                recover.assert_called_once_with()
                self.assertFalse((root / "outputs").exists())
            finally:
                application.close()

    def test_default_home_recovery_validates_before_preparing_motion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            device = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
            ready = {
                "schema_version": "data_factory.operator_environment.v1",
                "state": "READY", "observed_at": "2026-08-26T03:00:00Z",
                "components": {
                    name: {
                        "state": "READY", "owner": f"owner-{name}",
                        "reason": "ATTACHED",
                    }
                    for name in ("robot", "controller", "gripper", "camera")
                },
            }
            recovery = {
                "schema_version": "data_factory.home_recovery.v1",
                "status": "HOME", "arm_goal_count": 1,
                "gripper_open": True, "target_rad": [0.0] * 6,
                "final_rad": [0.0] * 6,
                "motion_qualification_digest": canonical_digest(["motion"]),
            }
            cell_store = CellStateStore(
                root / "outputs/data_factory/cells", "fr5-lab-a-tcp-r002",
            )
            blocked = cell_store.mark_blocked(
                "ROS_EXEC_FAILED", "old-run", canonical_digest("old-plan"),
            )
            catalog = load_operator_catalog(root, device_ids=[device])
            for combination in catalog["combinations"]:
                if combination["execution"]["TEST_COLLECTION"]["executable"]:
                    combination["execution"]["GENERAL_COLLECTION"] = {
                        "executable": True, "reason": "GENERAL_CALLER_READY",
                    }
                    combination["combination_digest"] = canonical_digest({
                        key: value for key, value in combination.items()
                        if key != "combination_digest"
                    })
            catalog["combinations"].sort(
                key=lambda value: value["combination_digest"],
            )
            catalog["catalog_digest"] = canonical_digest({
                key: value for key, value in catalog.items()
                if key != "catalog_digest"
            })
            events = []
            application, _context = build_physical_operator_application(
                repository_root=root,
                session_id="prepared-home-recovery-r001",
                operator_label="local-operator",
                environment_call=lambda: copy.deepcopy(ready),
                prepare_environment_call=lambda: copy.deepcopy(ready),
                initial_environment=ready,
                home_recovery_prepare_call=lambda: events.append("prepare") or {
                    "status": "READY",
                },
                run_live_call=mock.Mock(side_effect=AssertionError("live was called")),
                job_path=(
                    "config/data_factory/jobs/"
                    "center-live-24mm-20260903-r002.job.json"
                ),
                initial_data_mode="GENERAL_COLLECTION",
                production_dataset_root=(
                    root / "datasets/fr5_episodes/recovery-test"
                ),
                initial_catalog=catalog,
                clock=lambda: NOW,
            )
            try:
                self.assertEqual(
                    application.selection["data_mode"], "GENERAL_COLLECTION",
                )
                with (
                    mock.patch(
                        "tools.data_factory.operator.composition.CellStateStore",
                        wraps=CellStateStore,
                    ) as recovery_cell_store,
                    mock.patch(
                        "tools.data_factory.motion.home_recovery."
                        "validate_home_recovery_qualification",
                        side_effect=lambda value: events.append("validate")
                        or value,
                    ),
                    mock.patch(
                        "tools.data_factory.motion.home_recovery."
                        "recover_home_live",
                        side_effect=lambda **_kwargs: events.append("recover")
                        or copy.deepcopy(recovery),
                    ),
                ):
                    before = application.bridge_core.snapshot()
                    result = application.bridge_core.consume(envelope(
                        before, "recover_home", {}, "prepared-home-recovery",
                    ))["result"]
                self.assertEqual(result["outcome"], "HOME")
                self.assertEqual(events, ["validate", "prepare", "recover"])
                self.assertEqual(
                    recovery_cell_store.call_args.args,
                    (
                        root / "outputs/data_factory/cells",
                        "fr5-lab-a-tcp-r002",
                    ),
                )
                self.assertEqual(
                    cell_store.read(),
                    {
                        **blocked,
                        "cell_ready": True,
                        "reason_code": "HUMAN_ACKNOWLEDGED",
                        "acknowledged_by": "local-operator",
                        "updated_at": cell_store.read()["updated_at"],
                    },
                )
            finally:
                application.close()

    def test_product_application_captures_named_start_as_non_executable_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            device = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
            ready = {
                "schema_version": "data_factory.operator_environment.v1",
                "state": "READY", "observed_at": "2026-08-26T03:00:00Z",
                "components": {
                    name: {"state": "READY", "owner": f"owner-{name}", "reason": "ATTACHED"}
                    for name in ("robot", "controller", "gripper", "camera")
                },
            }
            rigid = {
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_columns": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            }
            tcp = load_json_strict(
                root / "config/data_factory/test_only_physical/goal2-place1/"
                "tcp_candidate_manifest.json"
            )
            snapshot = {
                "schema_version": "data_factory.pose_snapshot.v1",
                "frames": {"base": "base_link", "wrist": "wrist3_link"},
                "joint_positions_rad": {
                    joint: index / 10
                    for index, joint in enumerate(("j1", "j2", "j3", "j4", "j5", "j6"))
                },
                "base_wrist": copy.deepcopy(rigid),
                "base_tcp": {
                    **copy.deepcopy(rigid), "candidate_status": "CANDIDATE",
                    "candidate_source_sha256": tcp["tcp_candidate_digest"],
                    "manifest_source_sha256": canonical_digest(tcp),
                },
                "joint_state_age_s": 0.01, "ros_sample_age_s": 0.01,
                "joint_stamp_ns": 1_000_000_000,
                "transform_stamp_ns": 1_000_000_000,
            }
            application, _context = build_physical_operator_application(
                repository_root=root, session_id="named-start-r001",
                operator_label="local-operator",
                environment_call=lambda: copy.deepcopy(ready),
                prepare_environment_call=lambda: self.fail("prepare called"),
                selected_camera_device_id=device,
                discovery_call=lambda: [device],
                snapshot_call=lambda: copy.deepcopy(snapshot),
                run_live_call=mock.Mock(side_effect=AssertionError("live called")),
                clock=lambda: NOW,
            )
            try:
                before = application.bridge_core.snapshot()
                self.assertEqual(
                    before["projection"]["start_pose_setup"]["selected_start_pose_ids"],
                    ["fr5-lab-a-home-r001"],
                )
                result = application.bridge_core.consume(envelope(
                    before, "capture_start_pose", {"display_name": "Inspection ready"},
                    "capture-named-start-r001",
                ))["result"]
                candidates = [
                    item for item in result["start_pose_setup"]["profiles"]
                    if item["status"] == "CANDIDATE"
                ]
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0]["display_name"], "Inspection ready")
                self.assertEqual(
                    result["start_pose_setup"]["selected_start_pose_ids"],
                    ["fr5-lab-a-home-r001"],
                )
                saved = list((root / "config/data_factory/start_poses").glob("*.json"))
                self.assertEqual(len(saved), 1)
                self.assertEqual(load_json_strict(saved[0])["authority"], "NO_EXECUTION_AUTHORITY")
            finally:
                application.close()

    def test_preplan_site_checkpoint_returns_to_browser_before_plan_approval(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, preplan_checkpoint=True)
            console = harness.console()
            offered = threading.Event()
            release_offer = threading.Event()
            original_offer = console.button_port.offer

            def delayed_offer(*args, **kwargs):
                result = original_offer(*args, **kwargs)
                offered.set()
                release_offer.wait(1.0)
                return result

            console.button_port.offer = delayed_offer
            try:
                initial = console.bridge_core.snapshot()
                result = console.bridge_core.consume(envelope(
                    initial, "compile_draft", {
                        "draft_id": harness.source_draft["draft_id"],
                        "data_disposition": "TEST_ONLY",
                    }, "compile-site-r001",
                ))["result"]
                self.assertEqual(result["outcome"], "AWAITING_CHECKPOINT")
                view = console.bridge_core.snapshot()
                checkpoint = view["projection"]["operator_checkpoint"]
                self.assertEqual(checkpoint["kind"], "PHYSICAL_SCENE_CONFIRMATION")
                self.assertEqual(checkpoint["choices"], ["READY", "CANCEL"])
                self.assertEqual(
                    checkpoint["evidence"]["checklist"],
                    {"place_alias": "place1", "place_id": "PLACE_A"},
                )
                console.bridge_core.consume(envelope(
                    view, "resolve_checkpoint", {
                        "checkpoint_binding_digest": checkpoint["binding_digest"],
                        "choice": "READY",
                    }, "site-ready-r001",
                ))
                self.assertTrue(offered.wait(1.0))
                snapshots = []
                snapshot_started = threading.Event()
                snapshot_done = threading.Event()

                def take_snapshot():
                    snapshot_started.set()
                    snapshots.append(console.bridge_core.snapshot())
                    snapshot_done.set()

                snapshot_thread = threading.Thread(target=take_snapshot)
                snapshot_thread.start()
                self.assertTrue(snapshot_started.wait(1.0))
                published_before_outer_transition = snapshot_done.wait(0.05)
                release_offer.set()
                snapshot_thread.join(1.0)
                self.assertFalse(published_before_outer_transition)
                self.assertFalse(snapshot_thread.is_alive())
                approval_view = snapshots[0]
                approval = approval_view["projection"]["approval"]
                self.assertRegex(approval["plan_digest"], r"^sha256:[0-9a-f]{64}$")
                console.bridge_core.consume(envelope(
                    approval_view, "reject_plan", {
                        "plan_digest": approval["plan_digest"],
                        "approval_scope": approval["approval_scope"],
                        "data_disposition": "TEST_ONLY",
                    }, "site-reject-r001",
                ))
            finally:
                release_offer.set()
                console.close()

    def test_slow_physical_preparation_returns_running_without_cancelling_owner(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            release = threading.Event()
            episode_started = threading.Event()

            def slow_episode(*args):
                episode_started.set()
                release.wait()
                return harness.episode(*args)

            console = harness.console(
                episode_call=slow_episode, prepare_timeout_s=0.01,
            )
            try:
                initial = console.bridge_core.snapshot()
                result = console.bridge_core.consume(envelope(
                    initial, "compile_draft", {
                        "draft_id": harness.source_draft["draft_id"],
                        "data_disposition": "TEST_ONLY",
                    }, "compile-slow-r001",
                ))["result"]
                self.assertEqual(result, {
                    "outcome": "RUNNING",
                    "active_child_id": "physical-console-r001-run-0",
                })
                self.assertTrue(episode_started.wait(1.0))
                snapshots = []
                snapshot_done = threading.Event()

                def snapshot():
                    snapshots.append(console.bridge_core.snapshot()["projection"])
                    snapshot_done.set()

                snapshot_thread = threading.Thread(target=snapshot)
                snapshot_thread.start()
                self.assertTrue(snapshot_done.wait(0.2))
                snapshot_thread.join(1.0)
                self.assertFalse(snapshot_thread.is_alive())
                projection = snapshots[0]
                self.assertEqual(projection["runtime"]["workflow_state"], "RUNNING")
                self.assertEqual(harness.operator_counters["physical_factory"], 1)
                self.assertEqual(len(harness.children), 1)
                self.assertIs(
                    harness.operator._session.active_lifecycle,
                    harness.children[0],
                )
                self.assertFalse(harness.operator._session.cancel_event.is_set())
                self.assertTrue(console.episode_worker.is_alive())
                self.assertIsNone(projection["approval"])
                self.assertIsNone(projection["operator_checkpoint"])
                self.assertTrue(all(value == 0 for value in harness.forbidden.values()))
                self.assertTrue(all(
                    value == 0
                    for name, value in harness.operator_counters.items()
                    if name != "physical_factory"
                ))
                release.set()
                approval_view = self.wait_for(console, "approval")
                self.assertEqual(
                    approval_view["projection"]["runtime"]["workflow_state"],
                    "AWAITING_APPROVAL",
                )
                approval = approval_view["projection"]["approval"]
                console.bridge_core.consume(envelope(
                    approval_view, "reject_plan", {
                        "plan_digest": approval["plan_digest"],
                        "approval_scope": approval["approval_scope"],
                        "data_disposition": "TEST_ONLY",
                    }, "reject-slow-r001",
                ))
                self.assertEqual(console.wait_for_episode(1.0)["outcome"], "REJECT")
            finally:
                release.set()
                console.close()

    def test_real_composition_snapshot_failure_precedes_transition_and_all_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            _motion, qualification = self.qualified_home_start(root)
            opened = {
                "active": True, "position_valid": True, "gripper_index": 1,
                "reference_position_m": 0.021, "feedback_position_m": 0.021,
                "sample_age_s": 0.0, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE",
            }
            snapshot = mock.Mock(
                side_effect=ContractError("PHYSICAL_HOME_SNAPSHOT"),
            )
            transition = mock.Mock(
                side_effect=AssertionError("start transition ran before HOME evidence"),
            )
            live = mock.Mock(side_effect=AssertionError("live episode ran"))
            candidate = mock.Mock(side_effect=AssertionError("candidate write ran"))
            ledger = mock.Mock(side_effect=AssertionError("ledger write ran"))
            with (
                mock.patch.object(run_job, "write_candidate_admission", candidate),
                mock.patch.object(run_job, "bind_candidate_episode_state", ledger),
            ):
                console, _context = build_physical_operator_console(
                    repository_root=root,
                    session_id="snapshot-order-r001",
                    run_id="snapshot-order-run-r001",
                    operator_label="local-operator",
                    discovery_call=lambda: ["usb-Goal2_Camera-video-index0"],
                    activation_call=lambda: True,
                    snapshot_call=snapshot,
                    gripper_readback_call=lambda: copy.deepcopy(opened),
                    run_live_call=live,
                    selected_start_pose_qualifications=[qualification],
                    start_transition_call=transition,
                    environment_prepared=True,
                    clock=lambda: NOW,
                )
                try:
                    current = console.bridge_core.snapshot()
                    compiled = console.bridge_core.consume(envelope(
                        current, "compile_draft", {
                            "draft_id": current["projection"]["draft"]["draft_id"],
                            "data_disposition": "TEST_ONLY",
                        }, "compile-snapshot-order-r001",
                    ))["result"]
                    review = console.bridge_core.snapshot()
                    before_paths = {
                        path.relative_to(root) for path in root.rglob("*")
                    }
                    console.bridge_core.consume(envelope(
                        review, "authorize_campaign", {
                            "draft_id": review["projection"]["draft"]["draft_id"],
                            "manifest_digest": compiled["manifest_digest"],
                            "envelope_digest": compiled["envelope_digest"],
                            "data_disposition": "TEST_ONLY",
                        }, "authorize-snapshot-order-r001",
                    ))

                    result = console.wait_for_episode(1.0)
                    projection = console.bridge_core.snapshot()["projection"]
                    self.assertEqual(
                        (result["outcome"], result["code"]),
                        ("FAIL", "PHYSICAL_HOME_SNAPSHOT"),
                    )
                    self.assertEqual(
                        (projection["runtime"]["workflow_state"],
                         projection["runtime"]["reason_codes"]),
                        ("BLOCKED", ["PHYSICAL_HOME_SNAPSHOT"]),
                    )
                    snapshot.assert_called_once_with()
                    transition.assert_not_called()
                    live.assert_not_called()
                    candidate.assert_not_called()
                    ledger.assert_not_called()
                    self.assertTrue(all(
                        count == 0 for count in projection["effect_counts"].values()
                    ))
                    self.assertEqual(
                        {path.relative_to(root) for path in root.rglob("*")},
                        before_paths,
                    )
                finally:
                    console.close()

    def test_cancel_interrupts_start_transition_before_child_or_live_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            motion, qualification = self.qualified_home_start(root)
            opened = {
                "active": True, "position_valid": True, "gripper_index": 1,
                "reference_position_m": 0.021, "feedback_position_m": 0.021,
                "sample_age_s": 0.0, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE",
            }
            entered = threading.Event()
            calls = []

            def snapshot():
                calls.append("snapshot")
                return pose_snapshot(
                    motion["qualified_safe_joint_positions_rad"], age=0.01,
                )

            def transition(_motion, _qualification, cancel_event):
                calls.append("transition")
                entered.set()
                if not cancel_event.wait(1.0):
                    raise AssertionError("cancel did not interrupt start transition")
                calls.append("cancelled")
                raise ContractError("START_TRANSITION_CANCELLED")

            live = mock.Mock(side_effect=AssertionError("cancelled episode ran"))
            console, _context = build_physical_operator_console(
                repository_root=root,
                session_id="cancel-start-r001",
                run_id="cancel-start-run-r001",
                operator_label="local-operator",
                discovery_call=lambda: ["usb-Goal2_Camera-video-index0"],
                activation_call=lambda: True,
                snapshot_call=snapshot,
                gripper_readback_call=lambda: copy.deepcopy(opened),
                run_live_call=live,
                selected_start_pose_qualifications=[qualification],
                start_transition_call=transition,
                environment_prepared=True,
                clock=lambda: NOW,
            )
            try:
                current = console.bridge_core.snapshot()
                compiled = console.bridge_core.consume(envelope(
                    current, "compile_draft", {
                        "draft_id": current["projection"]["draft"]["draft_id"],
                        "data_disposition": "TEST_ONLY",
                    }, "compile-cancel-start-r001",
                ))["result"]
                review = console.bridge_core.snapshot()
                console.bridge_core.consume(envelope(
                    review, "authorize_campaign", {
                        "draft_id": review["projection"]["draft"]["draft_id"],
                        "manifest_digest": compiled["manifest_digest"],
                        "envelope_digest": compiled["envelope_digest"],
                        "data_disposition": "TEST_ONLY",
                    }, "authorize-cancel-start-r001",
                ))
                self.assertTrue(entered.wait(1.0))
                running = console.bridge_core.snapshot()
                cancelled = console.bridge_core.consume(envelope(
                    running, "cancel_session", {
                        "active_child_id": running["projection"]["runtime"][
                            "active_child_id"
                        ],
                    }, "cancel-start-r001",
                ))["result"]
                self.assertEqual(cancelled["outcome"], "CANCELLING")
                result = console.wait_for_episode(1.0)
                projection = console.bridge_core.snapshot()["projection"]
                self.assertEqual(calls, ["snapshot", "transition", "cancelled"])
                self.assertEqual(
                    (result["outcome"], result["code"],
                     projection["runtime"]["workflow_state"]),
                    ("CANCEL", "PLAN_CANCELLED", "TERMINAL"),
                )
                self.assertTrue(console.session.cancel_event.is_set())
                with self.assertRaisesRegex(
                    ContractError, "CAMPAIGN_OPERATOR_TERMINAL",
                ):
                    console.campaign_operator.run_next(
                        {"run_id": "cancel-start-run-r001"}, {},
                    )
                live.assert_not_called()
                self.assertTrue(all(
                    count == 0 for count in projection["effect_counts"].values()
                ))
            finally:
                console.close()

    def test_pre_episode_snapshot_failure_is_published_without_effects(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.start_binding = mock.Mock(
                side_effect=ContractError("PHYSICAL_HOME_SNAPSHOT"),
            )
            console = harness.console(
                campaign_approval_once=True,
                object_reposition_bindings=[None],
            )
            try:
                initial = console.bridge_core.snapshot()
                compiled = console.bridge_core.consume(envelope(
                    initial, "compile_draft", {
                        "draft_id": harness.source_draft["draft_id"],
                        "data_disposition": "TEST_ONLY",
                    }, "compile-home-snapshot-failure",
                ))["result"]
                review = console.bridge_core.snapshot()
                started = console.bridge_core.consume(envelope(
                    review, "authorize_campaign", {
                        "draft_id": harness.source_draft["draft_id"],
                        "manifest_digest": compiled["manifest_digest"],
                        "envelope_digest": compiled["envelope_digest"],
                        "data_disposition": "TEST_ONLY",
                    }, "authorize-home-snapshot-failure",
                ))["result"]
                self.assertEqual(started["outcome"], "RUNNING")

                result = console.wait_for_episode(1.0)
                projection = console.bridge_core.snapshot()["projection"]
                self.assertEqual(
                    (result["outcome"], result["code"]),
                    ("FAIL", "PHYSICAL_HOME_SNAPSHOT"),
                )
                self.assertEqual(
                    (projection["runtime"]["workflow_state"],
                     projection["runtime"]["measurement_outcome"],
                     projection["runtime"]["reason_codes"],
                     projection["runtime"]["active_child_id"]),
                    ("BLOCKED", "NOT_AVAILABLE", ["PHYSICAL_HOME_SNAPSHOT"], None),
                )
                self.assertIsNone(result["intent_binding"])
                self.assertFalse(console.episode_worker.is_alive())
                harness.start_binding.assert_called_once()
                self.assertEqual(harness.operator_counters["physical_factory"], 0)
                self.assertTrue(all(value == 0 for value in harness.forbidden.values()))
                self.assertEqual(list(Path(root).rglob("*")), [])
            finally:
                console.close()

    def test_measured_physical_activation_mismatch_projects_fail(self):
        for code, expected_measurement in (
            ("PHYSICAL_SECOND_MOTION_OWNER", "FAIL"),
            ("PHYSICAL_CAMERA_BINDING_MISMATCH", "FAIL"),
            ("PHYSICAL_CAMERA_DEVICE_MISMATCH", "FAIL"),
            ("PHYSICAL_CAMERA_TOPIC", "NOT_AVAILABLE"),
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.portable_repository(root)
                opened = {
                    "active": True, "position_valid": True, "gripper_index": 1,
                    "reference_position_m": 0.021, "feedback_position_m": 0.021,
                    "sample_age_s": 0.0, "max_age_s": 0.1,
                    "source": "CONTROLLER_STATE",
                }

                activation = mock.Mock(side_effect=ContractError(code))

                console, context = build_physical_operator_console(
                    repository_root=root,
                    session_id=f"goal2-physical-fail-{code.lower()}",
                    run_id=f"goal2-place1-fail-{code.lower()}",
                    operator_label="local-operator",
                    discovery_call=lambda: ["usb-Goal2_Camera-video-index0"],
                    activation_call=activation,
                    gripper_readback_call=lambda: copy.deepcopy(opened),
                    clock=lambda: NOW,
                )
                try:
                    projection = console.bridge_core.snapshot()["projection"]
                    self.assertEqual(
                        (projection["runtime"]["workflow_state"],
                         projection["runtime"]["measurement_outcome"],
                         projection["available_ops"]),
                        ("BLOCKED", expected_measurement, []),
                    )
                    self.assertEqual(
                        projection["runtime"]["reason_codes"], [code],
                    )
                    activation.assert_called_once_with()
                    self.assertIsNone(console.episode_worker)
                    self.assertTrue(all(
                        value == 0 for value in projection["effect_counts"].values()
                    ))
                    self.assertEqual(context["production_writers_enabled"], False)
                finally:
                    console.close()

    def test_gripper_setup_cancel_stale_and_ready_are_single_use_before_campaign(self):
        for choice in ("CANCEL", "READY"):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as root:
                calls = []

                def resolve(decision):
                    calls.append(decision)
                    return {
                        "state": "ATTACHED", "supported_action": "VERIFY",
                        "maintenance_call_count": 1,
                        "readback_digest": canonical_digest("fresh-open-readback"),
                    }

                harness = Harness(
                    root, setup_request=self.gripper_setup_request(),
                    setup_resolution_call=resolve,
                )
                console = harness.console()
                try:
                    view = console.bridge_core.snapshot()
                    checkpoint = view["projection"]["operator_checkpoint"]
                    self.assertEqual(
                        (view["projection"]["available_ops"], checkpoint["kind"]),
                        (["resolve_checkpoint"], "GRIPPER_MAINTENANCE"),
                    )
                    with self.assertRaisesRegex(ContractError, "CHECKPOINT_DIGEST_MISMATCH"):
                        console.bridge_core.consume(envelope(
                            view, "resolve_checkpoint", {
                                "checkpoint_binding_digest": canonical_digest("stale"),
                                "choice": choice,
                            }, f"gripper-stale-{choice.lower()}",
                        ))
                    self.assertEqual(calls, [])
                    current = console.bridge_core.snapshot()
                    intent = envelope(
                        current, "resolve_checkpoint", {
                            "checkpoint_binding_digest": checkpoint["binding_digest"],
                            "choice": choice,
                        }, f"gripper-{choice.lower()}",
                    )
                    result = console.bridge_core.consume(intent)["result"]
                    projection = console.bridge_core.snapshot()["projection"]
                    if choice == "CANCEL":
                        self.assertEqual(
                            (result["outcome"], projection["runtime"]["workflow_state"], calls),
                            ("PAUSED", "PAUSED_AWAITING_OPERATOR", []),
                        )
                    else:
                        self.assertEqual(
                            (result["outcome"], projection["runtime"]["workflow_state"],
                             projection["available_ops"], len(calls)),
                            ("READY", "AUTHORING", ["compile_draft"], 1),
                        )
                    self.assertEqual(harness.operator_counters["physical_factory"], 0)
                    with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
                        console.bridge_core.consume(intent)
                    self.assertEqual(len(calls), 0 if choice == "CANCEL" else 1)
                finally:
                    console.close()

    def test_gripper_setup_partial_failure_blocks_before_campaign(self):
        for code, expected in (
            ("GRIPPER_MAINTENANCE_ACTION", ("BLOCKED", "FAIL", "BLOCKED")),
            ("GRIPPER_NORMAL_GRAPH_REQUIRED", (
                "PAUSED", "NOT_MEASURED", "PAUSED_AWAITING_OPERATOR",
            )),
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                calls = []

                def fail(_decision):
                    calls.append("maintenance")
                    raise ContractError(code)

                harness = Harness(
                    root, setup_request=self.gripper_setup_request(),
                    setup_resolution_call=fail,
                )
                console = harness.console()
                try:
                    view = console.bridge_core.snapshot()
                    checkpoint = view["projection"]["operator_checkpoint"]
                    result = console.bridge_core.consume(envelope(
                        view, "resolve_checkpoint", {
                            "checkpoint_binding_digest": checkpoint["binding_digest"],
                            "choice": "READY",
                        }, f"gripper-fail-{code.lower()}",
                    ))["result"]
                    projection = console.bridge_core.snapshot()["projection"]
                    self.assertEqual(
                        (result["outcome"], result["measurement_outcome"],
                         projection["runtime"]["workflow_state"]),
                        expected,
                    )
                    self.assertEqual(calls, ["maintenance"])
                    self.assertEqual(harness.operator_counters["physical_factory"], 0)
                finally:
                    console.close()

    def test_physical_console_projects_exact_plan_checkpoint_and_zero_effects(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            console = harness.console()
            self.addCleanup(console.close)
            self.assertIs(type(console.bridge_core), OperatorIntentCore)
            self.assertIs(type(console.candidate_review_port), CandidateReviewPort)
            initial = console.bridge_core.snapshot()
            compile_intent = envelope(initial, "compile_draft", {
                "draft_id": harness.source_draft["draft_id"],
                "data_disposition": "TEST_ONLY",
            }, "compile-r001")
            compiled = console.bridge_core.consume(compile_intent)["result"]
            self.assertEqual(compiled["outcome"], "AWAITING_APPROVAL")
            self.assertEqual(len(harness.children), 1)
            self.assertIs(type(harness.children[0]), OneJob)
            self.assertIs(harness.operator._session.active_lifecycle, harness.children[0])
            self.assertFalse(console.episode_worker.daemon)

            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
                console.bridge_core.consume(compile_intent)
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_STALE_VIEW"):
                console.bridge_core.consume(envelope(
                    initial, "compile_draft", compile_intent["payload"], "compile-stale-r001",
                ))

            approval_view = console.bridge_core.snapshot()
            projection = approval_view["projection"]
            self.assertEqual(projection["data_disposition"], "TEST_ONLY")
            self.assertIsNone(projection["candidate_review"])
            self.assertEqual(projection["candidate_review_status"], "NOT_APPLICABLE")
            self.assertEqual(projection["episode_plan"]["plan_digest"], projection["approval"]["plan_digest"])
            self.assertTrue({
                "setup", "operator_checkpoint", "candidate_review", "fixed_lane",
                "draft", "capabilities", "runtime", "approval", "effect_counts",
            } <= set(projection))
            wrong = envelope(approval_view, "approve_exact_plan", {
                "plan_digest": canonical_digest("wrong"),
                "approval_scope": "HIL_NUMERIC_PROXY", "data_disposition": "TEST_ONLY",
            }, "approve-wrong-r001")
            with self.assertRaisesRegex(ContractError, "OPERATOR_CONSOLE_PLAN_DIGEST_MISMATCH"):
                console.bridge_core.consume(wrong)
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_AUTHORITY"):
                console.bridge_core.consume(envelope(approval_view, "approve_exact_plan", {
                    **wrong["payload"], "approved_by": "HUMAN",
                }, "approve-authority-r001"))

            approved = envelope(approval_view, "approve_exact_plan", {
                "plan_digest": projection["approval"]["plan_digest"],
                "approval_scope": "HIL_NUMERIC_PROXY", "data_disposition": "TEST_ONLY",
            }, "approve-r001")
            console.bridge_core.consume(approved)
            checkpoint_view = self.wait_for(console, "operator_checkpoint")
            checkpoint = checkpoint_view["projection"]["operator_checkpoint"]
            self.assertEqual(set(checkpoint), {"kind", "prompt", "binding_digest", "choices", "evidence"})
            with self.assertRaisesRegex(ContractError, "CHECKPOINT_DIGEST_MISMATCH"):
                console.bridge_core.consume(envelope(checkpoint_view, "resolve_checkpoint", {
                    "checkpoint_binding_digest": canonical_digest("wrong"), "choice": "PASS",
                }, "checkpoint-wrong-r001"))
            with self.assertRaisesRegex(ContractError, "CHECKPOINT_FIELDS"):
                console.bridge_core.consume(envelope(checkpoint_view, "resolve_checkpoint", {
                    "checkpoint_binding_digest": checkpoint["binding_digest"],
                    "choice": "PASS", "path": "/tmp/not-browser-authority",
                }, "checkpoint-path-r001"))
            console.bridge_core.consume(envelope(checkpoint_view, "resolve_checkpoint", {
                "checkpoint_binding_digest": checkpoint["binding_digest"], "choice": "PASS",
            }, "checkpoint-r001"))

            result = console.wait_for_episode(1.0)
            self.assertEqual((result["outcome"], result["code"]), ("PASS", "TECHNICAL_PASS"))
            condition = harness.hypothesis["base_conditions"][0]["coverage_condition"]
            self.assertEqual(result["intent_binding"]["coverage_condition"], condition)
            self.assertEqual(
                result["intent_binding"]["coverage_condition_digest"],
                canonical_digest(condition),
            )
            self.assertEqual(
                result["intent_binding"]["binding_digest"],
                canonical_digest({
                    key: value for key, value in result["intent_binding"].items()
                    if key != "binding_digest"
                }),
            )
            self.assertFalse(console.episode_worker.is_alive())
            self.assertFalse(harness.operator._session.status()["active_child"])
            self.assertEqual(harness.operator_counters["physical_factory"], 1)
            self.assertTrue(all(value == 0 for value in harness.forbidden.values()))
            self.assertTrue(all(harness.operator_counters[name] == 0 for name in (
                "robot", "gripper", "camera", "production_recorder", "dataset",
                "run_state", "candidate", "inventory", "training",
            )))

    def test_three_episode_reviews_queue_without_blocking_campaign_and_preserve_authority(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root).resolve()
            statuses = {}
            review_calls = []

            def review_call(
                path, *, semantic_status, reviewed_by, checklist_id, **_kwargs,
            ):
                run_id = Path(path).parent.name
                review_calls.append((str(path), semantic_status, checklist_id))
                statuses[str(path)] = semantic_status
                return {
                    "run_id": run_id, "semantic_status": semantic_status,
                    "reviewed_by": reviewed_by,
                }

            port = CandidateReviewPort(
                operator_label="local-operator", review_call=review_call,
            )
            harness = Harness(root)
            console = harness.console(
                candidate_review_port=port, campaign_approval_once=True,
            )
            self.addCleanup(console.close)

            def ledger_reference(run_id):
                return {
                    "path": str(root_path / run_id / "episode_ledger.json"),
                    "state_path": str(root_path / run_id / "episode_ledger_state.json"),
                    "review_status": "PENDING", "retention_state": "PRESERVE",
                    "reclaim_state": "NOT_EVALUATED",
                    "training_status": "NOT_AUTHORIZED",
                }

            bind_fault = {"armed": True}

            def bind_state(reference, path):
                if bind_fault["armed"]:
                    bind_fault["armed"] = False
                    raise OSError("synthetic ledger write fault")
                return {
                    **reference,
                    "review_status": statuses[str(path)],
                    "retention_state": "PRESERVE",
                    "training_status": "NOT_AUTHORIZED",
                }

            with mock.patch.object(
                run_job, "bind_candidate_episode_state",
                side_effect=bind_state,
            ):
                console.candidate_state_bind_call = (
                    run_job.bind_candidate_episode_state
                )
                for index in range(3):
                    run_id = f"review-run-{index + 1}"
                    candidate_path = root_path / run_id / "candidate_admission.json"
                    condition = {
                        "place_id": "PLACE_A", "yaw_deg": index * 45,
                        "x_mm": index * 10, "y_mm": 0,
                    }
                    console._active_intent_projection = {
                        "run_id": run_id,
                        "coverage_condition": condition,
                    }
                    continuation = console._publish_outcome({
                        "ok": True,
                        "campaign": {
                            "state": "READY" if index < 2 else "COMPLETE",
                        },
                        "result": {
                            "technical_evidence": {"status": "PASS"},
                            "human_semantic": "NOT_MEASURED",
                            "episode_ledger": ledger_reference(run_id),
                            "candidate_review_offer": {
                                "candidate_path": str(candidate_path),
                                "run_id": run_id,
                                "checklist_id": "pick-place-v1",
                                "expected_file_digest": canonical_digest([run_id, "file"]),
                                "expected_review_context_digest": canonical_digest(
                                    [run_id, "context"],
                                ),
                                "ledger_reference": ledger_reference(run_id),
                            },
                        },
                    })
                    self.assertEqual(continuation, index < 2)
                    if index < 2:
                        self.assertEqual(
                            port.projection()["run_id"], "review-run-1",
                        )
                        self.assertEqual(
                            console.projection()["available_ops"],
                            ["review_candidate", "cancel_session"],
                        )

                projected = console.projection()
                self.assertEqual(
                    (projected["candidate_review"]["run_id"],
                     projected["candidate_review"]["episode_number"],
                     projected["candidate_review"]["queue_remaining"]),
                    ("review-run-1", 1, 3),
                )
                self.assertNotIn(str(root_path), json.dumps(projected))

                with self.assertRaisesRegex(
                    ContractError, "CANDIDATE_REVIEW_DIGEST_MISMATCH",
                ):
                    console.review_candidate({
                        "review_binding_digest": canonical_digest("stale"),
                        "choice": "PASS", "reason": None,
                    })
                self.assertEqual(statuses, {})

                first = console.projection()["candidate_review"]
                first_payload = {
                    "review_binding_digest": first["review_binding_digest"],
                    "choice": "PASS", "reason": None,
                }
                with self.assertRaisesRegex(
                    ContractError, "OPERATOR_CONSOLE_CANDIDATE_STATE",
                ):
                    console.review_candidate(first_payload)
                self.assertIn("review_candidate", console.projection()["available_ops"])
                unrelated = {"intent_binding": None, "human_semantic": "NOT_MEASURED"}
                console._episode_history.append(unrelated)
                resolved = console.review_candidate(first_payload)
                console._episode_history.remove(unrelated)
                self.assertEqual(resolved["remaining_reviews"], 2)
                self.assertEqual(
                    review_calls,
                    [(str(root_path / "review-run-1" / "candidate_admission.json"),
                      "PASS", "pick-place-v1")],
                )

                choices = (
                    ("FAIL", "TASK_GOAL"),
                    ("UNCERTAIN", "UNKNOWN"),
                )
                for index, (choice, reason) in enumerate(choices, start=1):
                    current = console.projection()["candidate_review"]
                    resolved = console.review_candidate({
                        "review_binding_digest": current["review_binding_digest"],
                        "choice": choice, "reason": reason,
                    })
                    self.assertEqual(resolved["remaining_reviews"], 2 - index)

                history = console.projection()["episode_history"]
                self.assertEqual(
                    [item["human_semantic"] for item in history],
                    ["PASS", "FAIL", "UNCERTAIN"],
                )
                self.assertTrue(all(
                    item["episode_ledger"]["retention_state"] == "PRESERVE"
                    and item["episode_ledger"]["training_status"] == "NOT_AUTHORIZED"
                    for item in history
                ))
                self.assertNotIn("review_candidate", console.projection()["available_ops"])

    def test_cancel_unblocks_the_single_worker_and_closes_without_a_thread_leak(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            console = harness.console()
            initial = console.bridge_core.snapshot()
            console.bridge_core.consume(envelope(initial, "compile_draft", {
                "draft_id": harness.source_draft["draft_id"],
                "data_disposition": "TEST_ONLY",
            }, "compile-cancel-r001"))
            view = console.bridge_core.snapshot()
            started = time.monotonic()
            cancelled = console.bridge_core.consume(envelope(view, "cancel_session", {
                "active_child_id": view["projection"]["runtime"]["active_child_id"],
            }, "cancel-r001"))["result"]
            self.assertEqual(cancelled["outcome"], "CANCELLING")
            self.assertLess(time.monotonic() - started, 1.0)
            result = console.wait_for_episode(1.0)
            self.assertIn(result["outcome"], {"CANCEL", "FAIL"})
            console.close()
            self.assertFalse(console.episode_worker.is_alive())
            self.assertEqual(len(harness.children), 1)
            self.assertTrue(all(value == 0 for value in harness.forbidden.values()))

    def test_real_loopback_covers_semantic_release_and_scene_ready_choices(self):
        cases = (
            ("SEMANTIC_VERDICT", "PASS", "PASS"),
            ("SEMANTIC_VERDICT", "FAIL", "FAIL"),
            ("RELEASE_VERDICT", "LANDED", "PASS"),
            ("RELEASE_VERDICT", "OFF_SLOT", "FAIL"),
            ("RELEASE_VERDICT", "UNCERTAIN", "FAIL"),
            ("SCENE_READY", "SCENE_READY", "PASS"),
        )
        for checkpoint_kind, choice, expected_outcome in cases:
            with self.subTest(checkpoint_kind=checkpoint_kind, choice=choice):
                with tempfile.TemporaryDirectory() as root:
                    harness = Harness(root, checkpoint_kind=checkpoint_kind)
                    console = harness.console()
                    bridge, thread = self.start_bridge(console)
                    try:
                        status, initial = self.request_json(bridge, "GET", "/api/view")
                        self.assertEqual(status, 200)
                        status, result = self.request_json(
                            bridge, "POST", "/api/intent",
                            envelope(initial, "compile_draft", {
                                "draft_id": harness.source_draft["draft_id"],
                                "data_disposition": "TEST_ONLY",
                            }, f"compile-{checkpoint_kind.lower()}-{choice.lower()}"),
                        )
                        self.assertEqual((status, result["consumed"]), (200, True))
                        approval_view, approval = self.wait_for_http_projection(
                            bridge, "approval",
                        )
                        status, result = self.request_json(
                            bridge, "POST", "/api/intent",
                            envelope(approval_view, "approve_exact_plan", {
                                "plan_digest": approval["plan_digest"],
                                "approval_scope": approval["approval_scope"],
                                "data_disposition": "TEST_ONLY",
                            }, f"approve-{checkpoint_kind.lower()}-{choice.lower()}"),
                        )
                        self.assertEqual((status, result["consumed"]), (200, True))
                        checkpoint_view, checkpoint = self.wait_for_http_projection(
                            bridge, "operator_checkpoint",
                        )
                        self.assertEqual(checkpoint["kind"], checkpoint_kind)
                        if checkpoint_kind == "RELEASE_VERDICT":
                            self.assertTrue(
                                checkpoint["evidence"]["landing_and_final_scene_combined"],
                            )
                        status, result = self.request_json(
                            bridge, "POST", "/api/intent",
                            envelope(checkpoint_view, "resolve_checkpoint", {
                                "checkpoint_binding_digest": checkpoint["binding_digest"],
                                "choice": choice,
                            }, f"checkpoint-{checkpoint_kind.lower()}-{choice.lower()}"),
                        )
                        self.assertEqual((status, result["consumed"]), (200, True))
                        episode = console.wait_for_episode(1.0)
                        self.assertEqual(episode["outcome"], expected_outcome)
                        status, terminal = self.request_json(bridge, "GET", "/api/view")
                        self.assertEqual(status, 200)
                        self.assertIn(
                            terminal["projection"]["runtime"]["workflow_state"],
                            {"TERMINAL", "BLOCKED"},
                        )
                        self.assertTrue(all(value == 0 for value in harness.forbidden.values()))
                    finally:
                        console.close()
                        bridge.close()
                        thread.join(timeout=2)
                        self.assertFalse(thread.is_alive())

    def test_real_loopback_plan_reject_cancel_reconnect_stale_and_replay(self):
        for op, expected in (("reject_plan", "REJECT"), ("cancel_session", "CANCEL")):
            with self.subTest(op=op):
                with tempfile.TemporaryDirectory() as root:
                    harness = Harness(root)
                    console = harness.console()
                    bridge, thread = self.start_bridge(console)
                    try:
                        status, initial = self.request_json(bridge, "GET", "/api/view")
                        self.assertEqual(status, 200)
                        compile_intent = envelope(initial, "compile_draft", {
                            "draft_id": harness.source_draft["draft_id"],
                            "data_disposition": "TEST_ONLY",
                        }, f"compile-{op}")
                        status, _ = self.request_json(
                            bridge, "POST", "/api/intent", compile_intent,
                        )
                        self.assertEqual(status, 200)
                        status, replay = self.request_json(
                            bridge, "POST", "/api/intent", compile_intent,
                        )
                        self.assertEqual((status, replay["code"]), (409, "OPERATOR_INTENT_REPLAY"))
                        stale_intent = envelope(
                            initial, "compile_draft", compile_intent["payload"],
                            f"stale-{op}",
                        )
                        status, stale = self.request_json(
                            bridge, "POST", "/api/intent", stale_intent,
                        )
                        self.assertEqual((status, stale["code"]), (409, "OPERATOR_INTENT_STALE_VIEW"))
                        status, reconnected = self.request_json(bridge, "GET", "/api/view")
                        self.assertEqual(status, 200)
                        self.assertEqual(reconnected["projection"]["runtime"]["workflow_state"], "AWAITING_APPROVAL")
                        approval = reconnected["projection"]["approval"]
                        payload = (
                            {
                                "plan_digest": approval["plan_digest"],
                                "approval_scope": approval["approval_scope"],
                                "data_disposition": "TEST_ONLY",
                            }
                            if op == "reject_plan" else
                            {"active_child_id": reconnected["projection"]["runtime"]["active_child_id"]}
                        )
                        status, result = self.request_json(
                            bridge, "POST", "/api/intent",
                            envelope(reconnected, op, payload, f"finish-{op}"),
                        )
                        self.assertEqual((status, result["consumed"]), (200, True))
                        episode = console.wait_for_episode(1.0)
                        self.assertEqual(episode["outcome"], expected)
                        self.assertTrue(all(value == 0 for value in harness.forbidden.values()))
                    finally:
                        console.close()
                        bridge.close()
                        thread.join(timeout=2)
                        self.assertFalse(thread.is_alive())

    def test_non_ok_live_response_preserves_paused_unavailable_and_fail_axes(self):
        cases = (
            (
                {"ok": False, "code": "PAUSED_AWAITING_OPERATOR", "state": "PLANNED",
                 "data": {"measurement_outcome": "NOT_MEASURED"}},
                ("PAUSED_AWAITING_OPERATOR", "NOT_MEASURED", "PAUSED"),
            ),
            (
                {"ok": False, "code": "PHYSICAL_CAMERA_TOPIC", "state": "BLOCKED",
                 "data": {"measurement_outcome": "NOT_AVAILABLE"}},
                ("BLOCKED", "NOT_AVAILABLE", "NOT_AVAILABLE"),
            ),
            (
                {"ok": False, "code": "CAMERA_WARMUP_RATE", "state": "BLOCKED",
                 "data": {"measurement_outcome": "FAIL"}},
                ("BLOCKED", "FAIL", "FAIL"),
            ),
        )
        for terminal, expected in cases:
            with self.subTest(code=terminal["code"]):
                with tempfile.TemporaryDirectory() as root:
                    harness = Harness(root, terminal_response=terminal)
                    console = harness.console()
                    try:
                        initial = console.bridge_core.snapshot()
                        result = console.bridge_core.consume(envelope(
                            initial, "compile_draft", {
                                "draft_id": harness.source_draft["draft_id"],
                                "data_disposition": "TEST_ONLY",
                            }, f"compile-terminal-{terminal['code'].lower()}"),
                        )["result"]
                        view = console.bridge_core.snapshot()["projection"]
                        self.assertEqual(
                            (view["runtime"]["workflow_state"],
                             view["runtime"]["measurement_outcome"],
                             result["outcome"]),
                            expected,
                        )
                        self.assertIsNone(result["technical_evidence"])
                    finally:
                        console.close()


class ReusableHarness(Harness):
    TERMINAL = {"ABORTED", "BLOCKED", "CANCELLED", "COMPLETE", "QUARANTINED_COMMIT"}

    def __init__(
        self, root: str, *, count: int = 3, wrong_plan_scope: bool = False,
        wrong_checkpoint_scope: bool = False,
        wrong_trajectory_binding: bool = False,
        block_until_cancel: bool = False,
    ):
        super().__init__(root)
        self.hypothesis, self.source_draft = physical_contract(count)
        self.scene_digest = self.hypothesis["fixed_contract"]["scene_digest"]
        self.count = count
        self.wrong_plan_scope = wrong_plan_scope
        self.wrong_checkpoint_scope = wrong_checkpoint_scope
        self.wrong_trajectory_binding = wrong_trajectory_binding
        self.block_until_cancel = block_until_cancel
        self.max_active = 0
        self.overlap = False
        self.intents = []
        self.contexts = []
        self.plan_exchanges = []
        self.checkpoint_exchanges = []
        self.episode_entered = threading.Event()

    def fresh_one_job(self) -> OneJob:
        active = sum(child.state not in self.TERMINAL for child in self.children)
        self.overlap = self.overlap or active > 0
        self.max_active = max(self.max_active, active + 1)
        return super().fresh_one_job()

    def start_binding(
        self, _run_id: str, slot: Mapping[str, Any], _cancel_event,
    ) -> dict:
        pose = next(
            item for item in self.hypothesis["robot_start_poses"]
            if item["robot_start_pose_id"] == slot["robot_start_pose_id"]
        )
        target = [pose["target_rad"][joint] for joint in pose["joint_order"]]
        value = {
            "scope": "MOTION_Q_SAFE_START",
            "data_disposition": "TEST_ONLY",
            "manifest_digest": self.operator.manifest["manifest_digest"],
            "slot_digest": canonical_digest(slot),
            "robot_start_pose_id": pose["robot_start_pose_id"],
            "robot_start_pose_qualification_digest": pose["qualification_digest"],
            "motion_qualification_id": "motion-q-safe-reusable-test",
            "motion_qualification_digest": canonical_digest("motion-q-safe-reusable-test"),
            "home_candidate_digest": pose["home_candidate_digest"],
            "joint_order": copy.deepcopy(pose["joint_order"]),
            "target_rad": target,
            "current_rad": copy.deepcopy(target),
            "tolerance_rad": 0.01,
            "max_snapshot_age_s": 0.1,
            "snapshot_digest": canonical_digest(["fresh-start", slot["slot_id"]]),
            "status": "BOUND_TEST_ONLY",
            "authority": copy.deepcopy(NO_AUTHORITY),
        }
        value["binding_digest"] = canonical_digest(value)
        return value

    def projection(self) -> dict:
        value = super().projection()
        value["draft"].update(
            budget=self.count,
            selected_count=self.count,
            split_summary=f"TRAIN {self.count}",
            repeat_summary=f"x{self.count}",
            coverage_summary=f"{self.count}/{self.count} selected",
        )
        value["draft"]["cells"][0]["repeat"] = self.count
        return value

    @staticmethod
    def _checkpoint_request(kind: str, run_id: str, plan_digest: str) -> dict:
        if kind == "PHYSICAL_SCENE_CONFIRMATION":
            evidence = {
                "data_disposition": "TEST_ONLY",
                "checklist": {"place_alias": "place1"},
                "operator_summary": {"path": ["PREGRASP_PTP", "LIFT_LIN"]},
                "planned_start_evidence_digest": canonical_digest(
                    ["planned-start", run_id],
                ),
            }
        else:
            evidence = {
                "data_disposition": "TEST_ONLY",
                "execution_evidence_digest": canonical_digest(["execution", run_id]),
                "release_target": {"place_id": "place-r1", "x_mm": 10, "y_mm": 0},
                "safe_staging_joint_positions_rad": [0.0] * 6,
                "landing_and_final_scene_combined": True,
            }
        return {
            "schema_version": "data_factory.operator_checkpoint_request.v1",
            "kind": kind,
            "run_id": run_id,
            "plan_digest": plan_digest,
            "prompt": f"Confirm {kind}",
            "choices": (
                ["READY", "CANCEL"]
                if kind == "PHYSICAL_SCENE_CONFIRMATION"
                else ["LANDED", "OFF_SLOT", "UNCERTAIN"]
            ),
            "evidence": evidence,
            "timeout_s": 1.0,
        }

    def episode(
        self, intent, lifecycle, cancel_event, episode_context,
        decision_provider, checkpoint_provider,
    ):
        self.intents.append(copy.deepcopy(intent))
        self.contexts.append(copy.deepcopy(episode_context))
        plan_digest = canonical_digest(["fresh-plan", intent["intent_digest"]])
        episode_binding = {
            "manifest_digest": intent["manifest_digest"],
            "intent_digest": intent["intent_digest"],
            "run_id": (
                "forged-run" if self.wrong_plan_scope else intent["run_id"]
            ),
            "slot_digest": intent["slot_digest"],
            "root_binding_digest": episode_context["root_binding"]["binding_digest"],
            "start_binding_digest": episode_context["start_binding"]["binding_digest"],
        }
        phase_parameters = {}
        trajectory = {
            "schema_version": "data_factory.trajectory_variant_binding.v2",
            "trajectory_variant_id": "DIRECT",
            "variation_profile_digest": canonical_digest("direct-profile"),
            "sampling_seed": 0,
            "sample_rank": 0,
            "design_size": 1,
            "design_digest": canonical_digest("direct-design"),
            "target_yaw_deg": 0.0,
            "phase_parameters": phase_parameters,
            "phase_parameters_digest": canonical_digest(phase_parameters),
            "motion_program_digest": canonical_digest(["program", intent["run_id"]]),
        }
        trajectory["binding_digest"] = canonical_digest(trajectory)
        if self.wrong_trajectory_binding:
            trajectory["phase_parameters"]["forged"] = True
        request = {
            "schema_version": "data_factory.plan_decision_request.v1",
            "run_id": intent["run_id"],
            "plan_digest": plan_digest,
            "approval_scope": "HIL_NUMERIC_PROXY",
            "decision_binding": {
                "data_disposition": "TEST_ONLY",
                "episode_binding": episode_binding,
                "operator_summary": {
                    "path": ["PREGRASP_PTP", "LIFT_LIN"],
                    "speed": {"joint_scale": 0.2},
                    "clearance": {"minimum_m": 0.05},
                    "flow": {"pickup": True, "same_cell_recycle": True},
                },
                "trajectory_variant_binding": trajectory,
                "trajectory_variant_binding_digest": trajectory["binding_digest"],
                "yaw_sample_binding": None,
                "yaw_sample_binding_digest": None,
                "precommit_safety": {"approved_plan_digest": plan_digest},
                "plan_envelope_digest": canonical_digest(
                    ["envelope", intent["run_id"]],
                ),
                "preapproval_evidence_digest": canonical_digest(
                    ["preapproval", intent["run_id"]],
                ),
                "object_reposition_preapproval": None,
            },
            "timeout_s": 1.0,
        }
        for kind in ("PHYSICAL_SCENE_CONFIRMATION",):
            checkpoint_request = self._checkpoint_request(
                kind,
                "forged-run" if self.wrong_checkpoint_scope else intent["run_id"],
                plan_digest,
            )
            checkpoint = checkpoint_provider(copy.deepcopy(checkpoint_request))
            self.checkpoint_exchanges.append(
                (checkpoint_request, copy.deepcopy(checkpoint)),
            )
            expected = "READY" if kind == "PHYSICAL_SCENE_CONFIRMATION" else "LANDED"
            if checkpoint is None or checkpoint["choice"] != expected:
                raise ContractError("TEST_CHECKPOINT_NOT_APPROVED")

        decision = decision_provider(copy.deepcopy(request))
        self.plan_exchanges.append((request, copy.deepcopy(decision)))
        if decision is None or decision["choice"] != "APPROVE":
            raise ContractError("TEST_PLAN_NOT_APPROVED")

        self.episode_entered.set()
        if self.block_until_cancel:
            if not cancel_event.wait(1.0):
                raise ContractError("TEST_CANCEL_TIMEOUT")
            lifecycle.state = "CANCELLED"
            raise ContractError("TEST_CANCELLED")

        checkpoint_request = self._checkpoint_request(
            "RELEASE_VERDICT",
            "forged-run" if self.wrong_checkpoint_scope else intent["run_id"],
            plan_digest,
        )
        checkpoint = checkpoint_provider(copy.deepcopy(checkpoint_request))
        self.checkpoint_exchanges.append((checkpoint_request, copy.deepcopy(checkpoint)))
        if checkpoint is None or checkpoint["choice"] != "LANDED":
            raise ContractError("TEST_CHECKPOINT_NOT_APPROVED")

        lifecycle.state = "COMPLETE"
        post_scene_digest = canonical_digest(["post-scene", intent["run_id"]])
        technical = {
            "schema_version": "data_factory.seed_technical_result.v1",
            "intent_digest": intent["intent_digest"],
            "run_id": intent["run_id"],
            "manifest_digest": intent["manifest_digest"],
            "slot_id": intent["slot"]["slot_id"],
            "status": "PASS",
            "technical_result_digest": canonical_digest(["technical", intent["run_id"]]),
            "post_scene_digest": post_scene_digest,
            "observed_at": NOW.isoformat().replace("+00:00", "Z"),
        }
        technical["evidence_digest"] = canonical_digest(technical)
        self.scene_digest = post_scene_digest
        return {
            "result": {
                "technical_evidence": technical,
                "human_semantic": "NOT_MEASURED",
            },
            "technical_evidence": technical,
        }

    def console(self) -> OperatorConsole:
        value = OperatorConsole(
            session_id="reusable-console-r001",
            run_id="reusable-run-1",
            operator_label="local-operator",
            campaign_operator_factory=self.operator_factory,
            episode_call=self.episode,
            projection_call=self.projection,
            test_only_paths=self.root,
            campaign_approval_once=True,
            run_id_factory=lambda index: f"reusable-run-{index + 1}",
            prepare_timeout_s=1.0,
            close_timeout_s=1.0,
            clock=lambda: NOW,
        )
        self.console_instance = value
        return value


def start_campaign(console: OperatorConsole, harness: ReusableHarness, suffix: str):
    initial = console.bridge_core.snapshot()
    compiled = console.bridge_core.consume(envelope(
        initial,
        "compile_draft",
        {
            "draft_id": harness.source_draft["draft_id"],
            "data_disposition": "TEST_ONLY",
        },
        f"compile-{suffix}",
    ))["result"]
    review = console.bridge_core.snapshot()
    authorization = console.bridge_core.consume(envelope(
        review,
        "authorize_campaign",
        {
            "draft_id": harness.source_draft["draft_id"],
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        },
        f"authorize-{suffix}",
    ))["result"]
    return compiled, authorization


class ReusableOperatorConsoleTests(unittest.TestCase):
    def test_campaign_authorization_ttl_scales_with_episode_count(self):
        self.assertEqual(_campaign_authorization_ttl(1), timedelta(hours=1))
        self.assertEqual(_campaign_authorization_ttl(22), timedelta(hours=1))
        self.assertEqual(_campaign_authorization_ttl(45), timedelta(minutes=105))
        self.assertEqual(_campaign_authorization_ttl(100), timedelta(minutes=215))
        for invalid in (True, 0, 101):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ContractError, "PHYSICAL_CONSOLE_REQUESTED_COUNT",
            ):
                _campaign_authorization_ttl(invalid)

    def test_requested_count_scales_budgets_and_compiles_multiple_slots(self):
        hypothesis, draft = physical_contract(3)
        manifest, _ = compile_collection_campaign(draft, hypothesis=hypothesis)
        self.assertEqual((draft["requested_count"], len(manifest["slots"])), (3, 3))
        self.assertEqual(
            {
                key: draft["manifest_budget"][key]
                for key in (
                    "max_physical_episodes", "max_rollout_trials",
                    "max_hil_prompts", "max_reviews",
                )
            },
            {key: 3 for key in (
                "max_physical_episodes", "max_rollout_trials",
                "max_hil_prompts", "max_reviews",
            )},
        )
        self.assertEqual(draft["manifest_budget"]["max_storage_bytes"], 3 * 2_147_483_648)
        self.assertEqual(draft["program_budget"]["max_total_physical_episodes"], 3)
        self.assertEqual(draft["program_budget"]["max_total_storage_bytes"], 3 * 2_147_483_648)
        self.assertEqual(draft["manifest_budget"]["max_pending_reviews"], 3)
        self.assertEqual(draft["program_budget"]["max_pending_reviews"], 3)

        for invalid in (True, 0, 101):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ContractError, "PHYSICAL_CONSOLE_REQUESTED_COUNT",
            ):
                physical_contract(invalid)

    def test_one_authorization_runs_three_fresh_digest_bound_serial_episodes(self):
        with tempfile.TemporaryDirectory() as root:
            harness = ReusableHarness(root)
            console = harness.console()
            self.addCleanup(console.close)
            compiled, started = start_campaign(console, harness, "success")
            self.assertEqual(compiled["outcome"], "REVIEW_CAMPAIGN")
            self.assertEqual(compiled["episode_count"], 3)
            self.assertEqual(started["outcome"], "RUNNING")
            terminal = console.wait_for_episode(2.0)
            view = console.bridge_core.snapshot()["projection"]

            self.assertEqual((terminal["outcome"], terminal["code"]), ("PASS", "TECHNICAL_PASS"))
            self.assertEqual(view["campaign_session"]["campaign"]["state"], "COMPLETE")
            self.assertEqual(view["campaign_session"]["campaign"]["completed_intents"], 3)
            self.assertEqual(set(view["campaign_operator"]), {"campaign"})
            self.assertEqual(len(view["episode_history"]), 3)
            self.assertEqual(len(harness.children), 3)
            self.assertEqual(len({id(child) for child in harness.children}), 3)
            self.assertEqual(harness.max_active, 1)
            self.assertFalse(harness.overlap)
            self.assertEqual([child.state for child in harness.children], ["COMPLETE"] * 3)
            self.assertEqual(len({item["run_id"] for item in harness.intents}), 3)
            self.assertEqual(len({item["plan_digest"] for item, _ in harness.plan_exchanges}), 3)
            self.assertEqual(len({item["root_binding"]["binding_digest"] for item in harness.contexts}), 3)
            self.assertEqual(len({item["start_binding"]["binding_digest"] for item in harness.contexts}), 3)
            self.assertTrue(all(value == 0 for value in harness.forbidden.values()))

            authorization = console.campaign_authorization
            self.assertEqual(
                authorization["envelope"]["manifest_digest"],
                compiled["manifest_digest"],
            )
            self.assertEqual(authorization["envelope"]["episode_count"], 3)
            self.assertEqual(
                started["campaign_authorization_digest"],
                authorization["authorization_digest"],
            )
            for request, decision in harness.plan_exchanges:
                self.assertEqual(
                    (decision["run_id"], decision["plan_digest"], decision["choice"]),
                    (request["run_id"], request["plan_digest"], "APPROVE"),
                )
                self.assertEqual(
                    decision["decision_binding_digest"],
                    canonical_digest({
                        "run_id": request["run_id"],
                        "plan_digest": request["plan_digest"],
                        "approval_scope": request["approval_scope"],
                        "decision_binding": request["decision_binding"],
                    }),
                )
                self.assertEqual(
                    request["decision_binding"]["episode_binding"]["manifest_digest"],
                    authorization["envelope"]["manifest_digest"],
                )
                self.assertEqual(decision["decision_source"], "CAMPAIGN_AUTHORIZATION")
            for request, decision in harness.checkpoint_exchanges:
                bound = {
                    key: request[key]
                    for key in (
                        "kind", "run_id", "plan_digest", "prompt", "choices", "evidence",
                    )
                }
                self.assertEqual(decision["checkpoint_binding_digest"], canonical_digest(bound))
                self.assertEqual((decision["run_id"], decision["plan_digest"]),
                                 (request["run_id"], request["plan_digest"]))
                self.assertEqual(
                    decision["decision_source"],
                    "CAMPAIGN_AUTHORIZATION"
                    if request["kind"] == "PHYSICAL_SCENE_CONFIRMATION"
                    else "CAMPAIGN_CONTROL_PROXY",
                )

            with self.assertRaisesRegex(
                ContractError, "OPERATOR_CONSOLE_CAMPAIGN_AUTHORIZATION",
            ):
                console.authorize_campaign({
                    "draft_id": harness.source_draft["draft_id"],
                    "manifest_digest": compiled["manifest_digest"],
                    "envelope_digest": compiled["envelope_digest"],
                    "data_disposition": "TEST_ONLY",
                }, {})

    def test_automatic_plan_and_checkpoint_reject_wrong_episode_scope(self):
        for field in (
            "wrong_plan_scope", "wrong_checkpoint_scope",
            "wrong_trajectory_binding",
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                harness = ReusableHarness(root, **{field: True})
                console = harness.console()
                try:
                    start_campaign(console, harness, field)
                    result = console.wait_for_episode(2.0)
                    view = console.bridge_core.snapshot()["projection"]
                    self.assertEqual(
                        (result["outcome"], result["code"]),
                        ("FAIL", "OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH"),
                    )
                    self.assertEqual(view["campaign_session"]["campaign"]["state"], "BLOCKED")
                    self.assertEqual(len(harness.children), 1)
                    self.assertEqual(len(harness.intents), 1)
                finally:
                    console.close()

    def test_negative_cancel_stops_before_a_second_episode(self):
        with tempfile.TemporaryDirectory() as root:
            harness = ReusableHarness(root, block_until_cancel=True)
            console = harness.console()
            try:
                start_campaign(console, harness, "cancel")
                self.assertTrue(harness.episode_entered.wait(1.0))
                view = console.bridge_core.snapshot()
                cancelled = console.bridge_core.consume(envelope(
                    view,
                    "cancel_session",
                    {"active_child_id": view["projection"]["runtime"]["active_child_id"]},
                    "cancel-reusable",
                ))["result"]
                self.assertEqual(cancelled["outcome"], "CANCELLING")
                result = console.wait_for_episode(2.0)
                projection = console.bridge_core.snapshot()["projection"]
                self.assertEqual(result["outcome"], "CANCEL")
                self.assertEqual(projection["campaign_session"]["campaign"]["state"], "CANCELLED")
                self.assertEqual(len(harness.children), 1)
                self.assertEqual(len(harness.intents), 1)
                self.assertEqual(harness.operator_counters["physical_factory"], 1)
                with self.assertRaisesRegex(
                    ContractError, "CAMPAIGN_OPERATOR_TERMINAL",
                ):
                    harness.operator.run_next({"run_id": "reusable-run-2"}, {})
                self.assertEqual(len(harness.plan_exchanges), 1)
            finally:
                console.close()

    def test_bad_campaign_authorization_fails_before_executor_recorder_or_files(self):
        validated = runtime_validated(job={
            "task": "pickup_e2e",
            "robot_system_id": "fr5-lab-a",
            "operator_or_agent_id": "operator",
            "instruction": "pick up",
        })
        authorization_hypothesis, authorization_draft = physical_contract(3)
        authorization_manifest, authorization_receipt = compile_collection_campaign(
            authorization_draft, hypothesis=authorization_hypothesis,
        )
        campaign_envelope = build_campaign_envelope(
            source_draft=authorization_draft, manifest=authorization_manifest,
            compilation_receipt=authorization_receipt,
            hypothesis=authorization_hypothesis, effect_scope="PHYSICAL",
            lifecycle_action="LIVE_COLLECT", data_disposition="TEST_ONLY",
        )
        base = build_campaign_authorization(
            authorization_id="authorization-r001", operator_label="operator",
            envelope=campaign_envelope, approved_at="2026-08-26T00:00:00Z",
            expires_at="2099-01-01T00:00:00Z",
        )
        cases = {"forged": copy.deepcopy(base)}
        cases["forged"]["envelope"]["episode_count"] = 4
        cases["forged"]["authorization_digest"] = canonical_digest({
            key: value for key, value in cases["forged"].items()
            if key != "authorization_digest"
        })
        cases["expired"] = build_campaign_authorization(
            authorization_id="authorization-r001", operator_label="operator",
            envelope=campaign_envelope, approved_at="2026-08-25T00:00:00Z",
            expires_at="2026-08-25T01:00:00Z",
        )
        cases["wrong_scope"] = copy.deepcopy(base)

        expected = {
            "forged": "CAMPAIGN_ENVELOPE_BINDING",
            "expired": "CAMPAIGN_AUTHORIZATION_EXPIRED",
            "wrong_scope": "CAMPAIGN_AUTHORIZATION_BINDING",
        }
        for name, authorization in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                live_payload = payload("live")
                live_payload.update(
                    run_root=str(root / "runs"),
                    dataset_root=str(root / "dataset"),
                )
                roots = {
                    "session_id": "authorization-session",
                    "run_id": live_payload["run_id"],
                    "data_disposition": "TEST_ONLY",
                    "run_root": str((root / "runs").resolve()),
                    "cell_root": str((root / "cells").resolve()),
                    "dataset_root": str((root / "dataset").resolve()),
                    "production_writers_enabled": False,
                    "binding_digest": canonical_digest("roots"),
                }
                episode = {
                    "expires_at": "2099-01-01T00:00:00Z",
                    "manifest_digest": (
                        canonical_digest("other-manifest")
                        if name == "wrong_scope"
                        else campaign_envelope["manifest_digest"]
                    ),
                    "slot_digest": campaign_envelope["slot_digests"][0],
                    "robot_start_pose_id": campaign_envelope["allowed_start_pose_ids"][0],
                    "data_disposition": "TEST_ONLY",
                }
                executor = mock.Mock()
                recorder = mock.Mock()
                warmup = mock.Mock()
                with (
                    mock.patch.object(
                        run_job, "validate_test_only_root_binding", return_value=roots,
                    ),
                    mock.patch.object(
                        run_job, "validate_test_only_episode_binding", return_value=episode,
                    ),
                ):
                    result = run_job.run_live(
                        live_payload,
                        threading.Event(),
                        lambda _event: None,
                        resolver=lambda _payload: (
                            validated, runtime_motion(validated), SCENE,
                        ),
                        executor_factory=executor,
                        recorder_factory=recorder,
                        camera_warmup_call=warmup,
                        decision_provider=lambda _request: None,
                        approval_scope="HIL_NUMERIC_PROXY",
                        test_only_root_binding={"fixture": True},
                        test_only_episode_binding={"fixture": True},
                        test_only_start_binding={"fixture": True},
                        campaign_authorization=authorization,
                        candidate_writer_enabled=False,
                        repository_root=root,
                    )
                self.assertEqual((result["code"], result["state"]),
                                 (expected[name], "BLOCKED"))
                executor.assert_not_called()
                recorder.assert_not_called()
                warmup.assert_not_called()
                self.assertEqual(list(root.iterdir()), [])

    def test_legacy_mode_keeps_per_episode_plan_and_checkpoint_buttons(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            console = harness.console()
            try:
                initial = console.bridge_core.snapshot()
                compiled = console.bridge_core.consume(envelope(
                    initial,
                    "compile_draft",
                    {
                        "draft_id": harness.source_draft["draft_id"],
                        "data_disposition": "TEST_ONLY",
                    },
                    "compile-legacy-reusable",
                ))["result"]
                self.assertFalse(console.campaign_approval_once)
                self.assertEqual(compiled["outcome"], "AWAITING_APPROVAL")
                approval_view = console.bridge_core.snapshot()
                approval = approval_view["projection"]["approval"]
                self.assertEqual(
                    approval_view["projection"]["available_ops"],
                    ["approve_exact_plan", "reject_plan", "cancel_session"],
                )
                console.bridge_core.consume(envelope(
                    approval_view,
                    "approve_exact_plan",
                    {
                        "plan_digest": approval["plan_digest"],
                        "approval_scope": approval["approval_scope"],
                        "data_disposition": "TEST_ONLY",
                    },
                    "approve-legacy-reusable",
                ))
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    checkpoint_view = console.bridge_core.snapshot()
                    checkpoint = checkpoint_view["projection"]["operator_checkpoint"]
                    if checkpoint is not None:
                        break
                    time.sleep(0.005)
                else:
                    self.fail("legacy checkpoint was not projected")
                self.assertEqual(checkpoint["kind"], "SEMANTIC_VERDICT")
                self.assertEqual(
                    checkpoint_view["projection"]["available_ops"],
                    ["resolve_checkpoint", "cancel_session"],
                )
                console.bridge_core.consume(envelope(
                    checkpoint_view,
                    "resolve_checkpoint",
                    {
                        "checkpoint_binding_digest": checkpoint["binding_digest"],
                        "choice": "PASS",
                    },
                    "checkpoint-legacy-reusable",
                ))
                self.assertEqual(console.wait_for_episode(1.0)["outcome"], "PASS")
            finally:
                console.close()


if __name__ == "__main__":
    unittest.main()
