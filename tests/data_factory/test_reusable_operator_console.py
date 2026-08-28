from __future__ import annotations

import copy
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from tools.data_factory import run_job
from tools.data_factory.campaign_authoring import compile_collection_campaign
from tools.data_factory.campaign_authorization import (
    build_campaign_authorization,
    build_campaign_envelope,
)
from tools.data_factory.one_job import OneJob, TEST_ONLY_READINESS_CONTRACT
from tools.data_factory.operator_console import (
    OperatorConsole,
    build_physical_test_contract,
)
from tools.data_factory.operator_setup import NO_AUTHORITY
from tools.fr5_data_factory import ContractError, canonical_digest

try:
    from .test_experiment_manifest import single_qualification_inputs
    from .test_motion import SCENE
    from .test_operator_console import EXPIRES, Harness, NOW, envelope
    from .test_run_job import payload, runtime_motion, runtime_validated
except ImportError:
    from test_experiment_manifest import single_qualification_inputs
    from test_motion import SCENE
    from test_operator_console import EXPIRES, Harness, NOW, envelope
    from test_run_job import payload, runtime_motion, runtime_validated


def physical_contract(count: int):
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
            field: job[field]
            for field in (
                "robot_system_id", "cell_calibration_id", "object_profile_id",
                "grasp_profile_id",
            )
        },
        "home_candidate_digest": canonical_digest(home),
        "qualified_safe_joint_positions_rad": [0.0] * 6,
        "goal_tolerances": {"joint_rad": 0.01},
    }
    return build_physical_test_contract(
        resolved_job=resolved,
        motion_qualification=motion,
        home_candidate=home,
        scene_digest=canonical_digest("reusable-test-scene"),
        draft_id="reusable-draft-r001",
        manifest_id="reusable-manifest-r001",
        requested_count=count,
    )


class ReusableHarness(Harness):
    TERMINAL = {"ABORTED", "BLOCKED", "CANCELLED", "COMPLETE", "QUARANTINED_COMMIT"}

    def __init__(
        self, root: str, *, count: int = 3, wrong_plan_scope: bool = False,
        wrong_checkpoint_scope: bool = False, block_until_cancel: bool = False,
    ):
        super().__init__(root)
        self.hypothesis, self.source_draft = physical_contract(count)
        self.scene_digest = self.hypothesis["fixed_contract"]["scene_digest"]
        self.count = count
        self.wrong_plan_scope = wrong_plan_scope
        self.wrong_checkpoint_scope = wrong_checkpoint_scope
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

    def start_binding(self, _run_id: str, slot: Mapping[str, Any]) -> dict:
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
        for field in ("wrong_plan_scope", "wrong_checkpoint_scope"):
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
