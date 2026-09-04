from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from .operator.fixtures import draft, hypothesis

from tools.data_factory.campaign_operator import (
    FAKE_RECORDER_COUNTERS,
    FORBIDDEN_FAKE_COUNTERS,
    SIDE_EFFECT_COUNTERS,
    CampaignOperator,
)
from tools.data_factory.experiment_manifest import SLOT_INPUT_FIELDS
from tools.data_factory.operator.workflow.intents import INTENT_SCHEMA
from tools.data_factory.operator.setup.contracts import (
    build_production_root_binding,
    build_test_only_root_binding,
)
from tools.fr5_data_factory import ContractError, canonical_digest


NOW = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
EXPIRES = "2026-08-25T02:00:00Z"


def operator_intent(view: dict, op: str, payload: dict, name: str) -> dict:
    return {
        "schema_version": INTENT_SCHEMA,
        "intent_id": name,
        "session_id": view["session_id"],
        "view_revision": view["revision"],
        "view_digest": view["view_digest"],
        "op": op,
        "payload": payload,
    }


def send(model: CampaignOperator, op: str, payload: dict, name: str) -> dict:
    return model.core.consume(operator_intent(model.core.snapshot(), op, payload, name))


class FakeLifecycle:
    def __init__(self, counters: dict[str, int]):
        self.state = "IDLE"
        self.counters = counters
        self.cancel_calls = 0

    def begin(self):
        self.counters["fake_recorder_begin"] += 1

    def readiness_status(self):
        self.counters["fake_recorder_readiness_status"] += 1

    def freeze(self):
        self.counters["fake_recorder_freeze"] += 1

    def commit(self):
        self.counters["fake_recorder_commit"] += 1

    def cancel(self):
        self.cancel_calls += 1
        self.state = "CANCELLED"
        return {"state": self.state}


class PureFakePorts:
    def __init__(self, *, technical_status: str = "PASS"):
        self.counters = {name: 0 for name in SIDE_EFFECT_COUNTERS}
        self.fake_factory_calls = 0
        self.activation_calls = 0
        self.children = []
        self.technical_status = technical_status
        self.scene_digest = canonical_digest("SYNTHETIC-scene-0")

    def counter_snapshot(self):
        return self.counters

    def fake_factory(self):
        self.fake_factory_calls += 1
        child = FakeLifecycle(self.counters)
        self.children.append(child)
        return child

    def physical_factory(self):
        self.counters["physical_factory"] += 1
        raise AssertionError("Goal 1 must not construct a physical lifecycle")

    def activate(self):
        self.activation_calls += 1
        return True

    def scene(self, _run_id: str):
        value = {
            "schema_version": "data_factory.scene_freshness_evidence.v1",
            "scene_digest": self.scene_digest,
            "observed_at": NOW.isoformat().replace("+00:00", "Z"),
        }
        value["evidence_digest"] = canonical_digest(value)
        return value

    def _technical(self, intent: dict):
        post_scene = canonical_digest(["SYNTHETIC-scene", intent["run_id"]])
        value = {
            "schema_version": "data_factory.seed_technical_result.v1",
            "intent_digest": intent["intent_digest"],
            "run_id": intent["run_id"],
            "manifest_digest": intent["manifest_digest"],
            "slot_id": intent["slot"]["slot_id"],
            "status": self.technical_status,
            "technical_result_digest": canonical_digest([
                "SYNTHETIC-technical", intent["run_id"], self.technical_status,
            ]),
            "post_scene_digest": post_scene,
            "observed_at": NOW.isoformat().replace("+00:00", "Z"),
        }
        value["evidence_digest"] = canonical_digest(value)
        self.scene_digest = post_scene
        return value

    def plan(self, intent, lifecycle, cancel_event, episode_context):
        if cancel_event.is_set():
            raise ContractError("SYNTHETIC_CANCELLED")
        self.assert_context = episode_context
        lifecycle.state = "COMPLETE"
        technical = self._technical(intent)
        return {
            "result": {"path": "SYNTHETIC_PLAN", "technical_evidence": technical},
            "technical_evidence": technical,
        }

    def live(self, intent, lifecycle, cancel_event, episode_context):
        if cancel_event.is_set():
            raise ContractError("SYNTHETIC_CANCELLED")
        self.assert_context = episode_context
        lifecycle.begin()
        lifecycle.readiness_status()
        lifecycle.freeze()
        lifecycle.commit()
        lifecycle.state = "COMPLETE"
        technical = self._technical(intent)
        return {
            "result": {"path": "SYNTHETIC_LIVE", "technical_evidence": technical},
            "technical_evidence": technical,
        }


def make_operator(
    directory: str, *, effect_scope: str = "FAKE",
    lifecycle_action: str = "LIVE_COLLECT", count: int = 1,
    technical_status: str = "PASS", current_usage=None, clock=None,
    operator_label: str = "TEST_OPERATOR", data_disposition: str = "TEST_ONLY",
) -> tuple[CampaignOperator, PureFakePorts]:
    contract = hypothesis()
    ports = PureFakePorts(technical_status=technical_status)
    model = CampaignOperator(
        session_id=f"campaign-{effect_scope.lower()}-{lifecycle_action.lower()}",
        lifecycle_owner="TEST_OPERATOR",
        workspace={
            "workspace_id": "SYNTHETIC-workspace",
            "identity": "SYNTHETIC",
            "fixture_root": directory,
        },
        hypothesis=contract,
        draft=draft(contract, count=count),
        effect_scope=effect_scope,
        lifecycle_action=lifecycle_action,
        data_disposition=data_disposition,
        subsystems={
            "workspace": {"readiness": "READY", "capability": "AUTHOR", "reason": "SYNTHETIC"},
            "planner": {"readiness": "READY", "capability": "PLAN", "reason": "SYNTHETIC"},
            "recorder": {"readiness": "READY", "capability": "FAKE_ONLY", "reason": "SYNTHETIC"},
        },
        expires_at=EXPIRES,
        initial_scene_digest=ports.scene_digest,
        scene_evidence_call=ports.scene,
        side_effect_counter_call=ports.counter_snapshot,
        fake_lifecycle_factory=ports.fake_factory,
        fake_plan_call=ports.plan,
        fake_live_call=ports.live,
        physical_lifecycle_factory=ports.physical_factory,
        physical_plan_call=ports.plan,
        physical_live_call=ports.live,
        repository_root=directory,
        current_usage=current_usage,
        clock=clock or (lambda: NOW),
        operator_label=operator_label,
    )
    return model, ports


class CampaignOperatorTests(unittest.TestCase):
    def test_production_live_uses_the_same_serial_session_and_one_job_dag(self):
        with tempfile.TemporaryDirectory() as directory:
            model, ports = make_operator(
                directory, effect_scope="PHYSICAL", lifecycle_action="LIVE_COLLECT",
                count=2, data_disposition="PRODUCTION",
            )
            model.physical_activation_gate = ports.activate
            children, contexts = [], []

            def physical_factory():
                ports.counters["physical_factory"] += 1
                child = FakeLifecycle(ports.counters)
                children.append(child)
                return child

            model.physical_lifecycle_factory = physical_factory

            def seal(value: dict) -> dict:
                return {**value, "binding_digest": canonical_digest(value)}

            model.physical_root_binding_call = lambda run_id: seal({
                "session_id": model.session_id, "run_id": run_id,
                "data_disposition": "PRODUCTION",
            })
            model.physical_start_binding_call = lambda _run_id, slot, _cancel: seal({
                "data_disposition": "PRODUCTION", "slot_id": slot["slot_id"],
            })

            def production_live(intent, lifecycle, _cancel, context):
                contexts.append(context)
                lifecycle.state = "COMPLETE"
                technical = ports._technical(intent)
                return {
                    "result": {"path": "SYNTHETIC_PRODUCTION", "technical_evidence": technical},
                    "technical_evidence": technical,
                }

            model.physical_live_call = production_live

            def checked(value, **_kwargs):
                return dict(value)

            with (
                mock.patch(
                    "tools.data_factory.campaign_operator.validate_runtime_root_binding",
                    side_effect=checked,
                ),
                mock.patch(
                    "tools.data_factory.campaign_operator.validate_runtime_start_binding",
                    side_effect=checked,
                ),
                mock.patch(
                    "tools.data_factory.campaign_session.validate_runtime_root_binding",
                    side_effect=checked,
                ),
                mock.patch(
                    "tools.data_factory.campaign_session.validate_runtime_start_binding",
                    side_effect=checked,
                ),
            ):
                send(model, "compile_draft", {}, "compile-production-r001")
                first = send(
                    model, "run_next", {"run_id": "production-run-0"},
                    "run-production-r001",
                )["result"]
                second = send(
                    model, "run_next", {"run_id": "production-run-1"},
                    "run-production-r002",
                )["result"]

            self.assertEqual(first["campaign"]["state"], "READY")
            self.assertEqual(second["campaign"]["state"], "COMPLETE")
            self.assertEqual((ports.activation_calls, ports.counters["physical_factory"]), (1, 2))
            self.assertEqual(len({id(child) for child in children}), 2)
            self.assertEqual(
                [context["data_disposition"] for context in contexts],
                ["PRODUCTION", "PRODUCTION"],
            )
            self.assertEqual(
                [context["root_binding"]["run_id"] for context in contexts],
                ["production-run-0", "production-run-1"],
            )
            self.assertTrue(all(ports.counters[name] == 0 for name in FAKE_RECORDER_COUNTERS))
            self.assertTrue(all(
                ports.counters[name] == 0
                for name in ("robot", "gripper", "camera", "production_recorder", "dataset", "run_state")
            ))

    def test_production_rejects_cross_disposition_bindings_before_child_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            model, ports = make_operator(
                directory, effect_scope="PHYSICAL", lifecycle_action="LIVE_COLLECT",
                data_disposition="PRODUCTION",
            )
            model.physical_activation_gate = ports.activate
            model.physical_start_binding_call = lambda _run_id, _slot, _cancel: {
                "data_disposition": "PRODUCTION",
            }
            model.physical_root_binding_call = lambda run_id: build_test_only_root_binding(
                directory, session_id=model.session_id, run_id=run_id,
            )
            send(model, "compile_draft", {}, "compile-cross-root-r001")
            with self.assertRaisesRegex(ContractError, "CAMPAIGN_OPERATOR_PHYSICAL_ROOT_BINDING"):
                send(
                    model, "run_next", {"run_id": "production-run-root"},
                    "run-cross-root-r001",
                )
            self.assertEqual((ports.activation_calls, ports.counters["physical_factory"]), (0, 0))
            self.assertEqual(sum(ports.counters.values()), 0)

            model, ports = make_operator(
                directory, effect_scope="PHYSICAL", lifecycle_action="LIVE_COLLECT",
                data_disposition="PRODUCTION",
            )
            model.physical_activation_gate = ports.activate
            model.physical_root_binding_call = lambda run_id: build_production_root_binding(
                directory, session_id=model.session_id, run_id=run_id,
                dataset_root=f"{directory}/datasets/fr5_episodes/production-dataset",
            )
            model.physical_start_binding_call = lambda _run_id, _slot, _cancel: {
                "data_disposition": "TEST_ONLY", "binding_digest": canonical_digest("start"),
            }
            send(model, "compile_draft", {}, "compile-cross-start-r001")
            with mock.patch(
                "tools.data_factory.campaign_operator.validate_runtime_start_binding",
                side_effect=lambda value, **_kwargs: dict(value),
            ):
                with self.assertRaisesRegex(ContractError, "CAMPAIGN_OPERATOR_PHYSICAL_START_BINDING"):
                    send(
                        model, "run_next", {"run_id": "production-run-start"},
                        "run-cross-start-r001",
                    )
            self.assertEqual((ports.activation_calls, ports.counters["physical_factory"]), (1, 0))
            self.assertTrue(all(ports.counters[name] == 0 for name in FAKE_RECORDER_COUNTERS))
            self.assertTrue(all(
                ports.counters[name] == 0
                for name in ("robot", "gripper", "camera", "production_recorder", "dataset", "run_state")
            ))

    def test_fake_scope_rejects_production_disposition(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ContractError, "CAMPAIGN_OPERATOR_DISPOSITION"):
                make_operator(directory, data_disposition="PRODUCTION")

    def test_operator_label_is_caller_provided_without_human_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            model, _ = make_operator(
                directory, effect_scope="PHYSICAL", lifecycle_action="PLAN_ONLY",
                operator_label="local-operator",
            )
            projection = model.projection()
            self.assertEqual(projection["operator_identity"], "local-operator")
            self.assertEqual(projection["authority"]["human_review"], "NONE")

    def test_invalid_physical_ports_never_activate_or_allow_a_later_intent(self):
        cases = (
            ("callback", "PLAN_ONLY", "physical_plan_call", object()),
            ("factory", "PLAN_ONLY", "physical_lifecycle_factory", object()),
            ("root", "PLAN_ONLY", "repository_root", object()),
            ("start-port", "PLAN_ONLY", "physical_start_binding_call", object()),
            ("root-port", "LIVE_COLLECT", "physical_root_binding_call", lambda _run_id: {}),
        )
        with tempfile.TemporaryDirectory() as directory:
            for disposition in ("TEST_ONLY", "PRODUCTION"):
                for name, action, field, invalid in cases:
                    with self.subTest(disposition=disposition, port=name):
                        model, ports = make_operator(
                            directory, effect_scope="PHYSICAL", lifecycle_action=action,
                            data_disposition=disposition,
                        )
                        model.physical_activation_gate = ports.activate
                        model.physical_start_binding_call = lambda _run_id, _slot, _cancel: {}
                        setattr(model, field, invalid)
                        send(
                            model, "compile_draft", {},
                            f"compile-invalid-{disposition.lower()}-{name}",
                        )

                        for attempt in range(2):
                            with self.assertRaises(ContractError):
                                send(
                                    model, "run_next", {"run_id": f"SYNTHETIC-run-{attempt}"},
                                    f"run-invalid-{disposition.lower()}-{name}-{attempt}",
                                )
                            self.assertEqual(ports.activation_calls, 0)
                            self.assertEqual(ports.fake_factory_calls, 0)
                            self.assertEqual(sum(ports.counters.values()), 0)

    def test_physical_campaign_gates_run_before_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            contract = hypothesis()
            program = draft(contract, count=1)["program_budget"]
            full = {
                "rounds": 0, "physical_episodes": 0, "rollout_trials": 0,
                "hil_prompts": 0, "reviews": 0,
                "pending_reviews": program["max_pending_reviews"],
                "storage_bytes": 0,
            }
            for case in ("expired", "stale", "scene-digest", "quota"):
                with self.subTest(case=case):
                    model, ports = make_operator(
                        directory, effect_scope="PHYSICAL", lifecycle_action="PLAN_ONLY",
                        current_usage=full if case == "quota" else None,
                        clock=(
                            (lambda: datetime(2026, 8, 25, 2, 0, tzinfo=timezone.utc))
                            if case == "expired" else None
                        ),
                    )
                    model.physical_activation_gate = ports.activate
                    start_calls = []
                    model.physical_start_binding_call = lambda run_id, _slot, _cancel: start_calls.append(run_id) or {}
                    if case in {"stale", "scene-digest"}:
                        observed = NOW - timedelta(seconds=6) if case == "stale" else NOW

                        def invalid_scene(_run_id, observed=observed, case=case):
                            value = {
                                "schema_version": "data_factory.scene_freshness_evidence.v1",
                                "scene_digest": canonical_digest("wrong") if case == "scene-digest" else ports.scene_digest,
                                "observed_at": observed.isoformat().replace("+00:00", "Z"),
                            }
                            value["evidence_digest"] = canonical_digest(value)
                            return value

                        model.scene_evidence_call = invalid_scene
                    send(model, "compile_draft", {}, f"compile-{case}")
                    blocked = send(
                        model, "run_next", {"run_id": f"SYNTHETIC-run-{case}"},
                        f"run-{case}",
                    )["result"]
                    self.assertFalse(blocked["ok"])
                    self.assertEqual(ports.activation_calls, 0)
                    self.assertEqual(start_calls, [])
                    self.assertEqual(ports.fake_factory_calls, 0)
                    self.assertEqual(sum(ports.counters.values()), 0)

    def test_current_start_port_runs_only_after_physical_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            for action in ("PLAN_ONLY", "LIVE_COLLECT"):
                with self.subTest(action=action):
                    current = [NOW]
                    model, ports = make_operator(
                        directory, effect_scope="PHYSICAL", lifecycle_action=action,
                        clock=lambda: current[0],
                    )
                    timeline = []

                    def scene(_run_id):
                        timeline.append("scene")
                        value = {
                            "schema_version": "data_factory.scene_freshness_evidence.v1",
                            "scene_digest": ports.scene_digest,
                            "observed_at": current[0].isoformat().replace("+00:00", "Z"),
                        }
                        value["evidence_digest"] = canonical_digest(value)
                        return value

                    def activate():
                        timeline.append("activate")
                        current[0] += timedelta(seconds=6)
                        return ports.activate()

                    model.scene_evidence_call = scene
                    model.physical_activation_gate = activate
                    model.physical_start_binding_call = (
                        lambda _run_id, _slot, _cancel: timeline.append("start") or {}
                    )
                    if action == "LIVE_COLLECT":
                        model.physical_root_binding_call = lambda run_id: (
                            timeline.append("root")
                            or build_test_only_root_binding(
                                directory, session_id=model.session_id, run_id=run_id,
                            )
                        )
                    send(model, "compile_draft", {}, f"compile-start-{action}")
                    with self.assertRaisesRegex(ContractError, "RUNTIME_START_DISPOSITION"):
                        send(
                            model, "run_next", {"run_id": "SYNTHETIC-run-start"},
                            f"run-start-{action}",
                        )
                    self.assertEqual(
                        timeline,
                        (["root"] if action == "LIVE_COLLECT" else [])
                        + ["scene", "activate", "start", "scene"],
                    )
                    self.assertEqual(ports.activation_calls, 1)
                    self.assertEqual(ports.fake_factory_calls, 0)
                    self.assertEqual(sum(ports.counters.values()), 0)

    def test_assisted_and_direct_edit_share_effect_neutral_draft_and_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            model, ports = make_operator(directory, lifecycle_action="AUTHOR_ONLY", count=1)
            first = model.core.snapshot()
            original_id = first["projection"]["draft"]["draft_id"]
            assisted = send(model, "update_draft", {
                "authoring_mode": "ASSISTED",
                "requested_count": 2,
                "normalized_seed": 17,
                "pinned": [],
                "excluded": [],
                "direct_slots": [],
            }, "assisted-r001")
            self.assertEqual(assisted["result"]["selector"], "BALANCED_INITIAL")
            send(model, "compile_draft", {}, "compile-r001")
            compiled_view = model.core.snapshot()
            slots = [
                {key: item[key] for key in SLOT_INPUT_FIELDS}
                for item in compiled_view["projection"]["compiled"]["manifest"]["slots"]
            ]
            direct = send(model, "update_draft", {
                "authoring_mode": "DIRECT_EDIT",
                "requested_count": len(slots),
                "normalized_seed": 17,
                "pinned": [],
                "excluded": [],
                "direct_slots": slots,
            }, "direct-r001")
            self.assertEqual(direct["result"]["selector"], "DIRECT_LIST")
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_STALE_VIEW"):
                model.core.consume(operator_intent(compiled_view, "compile_draft", {}, "stale-r001"))

            reconnected = model.core.snapshot()["projection"]
            self.assertEqual((reconnected["draft"]["draft_id"], reconnected["draft"]["selector"]), (original_id, "DIRECT_LIST"))
            self.assertFalse({"effect_scope", "lifecycle_action", "approval", "scene_truth"} & set(reconnected["draft"]))
            send(model, "compile_draft", {}, "compile-r002")
            final = model.core.snapshot()["projection"]
            self.assertEqual(final["compiled"]["manifest"]["authority"], "NO_EXECUTION_AUTHORITY")
            self.assertEqual(set(final["authority"].values()), {"NONE"})
            self.assertEqual(final["operator_identity"], "TEST_OPERATOR")
            self.assertTrue(final["workspace"]["fixture_root"].startswith(directory))
            self.assertTrue(final["catalog"])
            self.assertEqual(sum(ports.counters.values()), 0)

            for index, forbidden in enumerate(("source", "reviewed_by", "scene_truth"), 1):
                view = model.core.snapshot()
                with self.subTest(forbidden=forbidden), self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_AUTHORITY"):
                    model.core.consume(operator_intent(
                        view, "update_draft", {forbidden: "HUMAN"}, f"forbidden-r00{index}",
                    ))

    def test_six_cell_matrix_is_pure_fake_or_fails_before_physical_factory(self):
        with tempfile.TemporaryDirectory() as directory:
            for effect_scope in ("FAKE", "PHYSICAL"):
                for action in ("AUTHOR_ONLY", "PLAN_ONLY", "LIVE_COLLECT"):
                    with self.subTest(effect_scope=effect_scope, action=action):
                        model, ports = make_operator(
                            directory, effect_scope=effect_scope, lifecycle_action=action,
                        )
                        send(model, "compile_draft", {}, f"compile-{effect_scope.lower()}-{action.lower()}")
                        if action == "AUTHOR_ONLY":
                            view = model.core.snapshot()
                            with self.assertRaisesRegex(ContractError, "CAMPAIGN_OPERATOR_AUTHOR_ONLY"):
                                model.core.consume(operator_intent(
                                    view, "run_next", {"run_id": "SYNTHETIC-run-0"},
                                    f"run-{effect_scope.lower()}-{action.lower()}",
                                ))
                            self.assertEqual(model.core.snapshot()["projection"]["campaign"]["state"], "AUTHOR_ONLY")
                        elif effect_scope == "PHYSICAL":
                            view = model.core.snapshot()
                            self.assertEqual(view["projection"]["aggregate"]["reason"], "PHYSICAL_ACTIVATION_REQUIRED")
                            with self.assertRaisesRegex(ContractError, "CAMPAIGN_OPERATOR_PHYSICAL_ACTIVATION_REQUIRED"):
                                model.core.consume(operator_intent(
                                    view, "run_next", {"run_id": "SYNTHETIC-run-0"},
                                    f"run-{effect_scope.lower()}-{action.lower()}",
                                ))
                        else:
                            result = send(
                                model, "run_next", {"run_id": "SYNTHETIC-run-0"},
                                f"run-{effect_scope.lower()}-{action.lower()}",
                            )["result"]
                            self.assertTrue(result["ok"])
                            self.assertEqual(result["campaign"]["state"], "COMPLETE")
                            self.assertEqual(
                                result["result"]["technical_evidence"]["status"], "PASS",
                            )

                        counters = model.core.snapshot()["projection"]["side_effect_counters"]
                        expected = 1 if effect_scope == "FAKE" and action == "LIVE_COLLECT" else 0
                        self.assertTrue(all(counters[name] == expected for name in FAKE_RECORDER_COUNTERS))
                        self.assertTrue(all(counters[name] == 0 for name in FORBIDDEN_FAKE_COUNTERS))
                        self.assertEqual(ports.fake_factory_calls, int(effect_scope == "FAKE" and action != "AUTHOR_ONLY"))

    def test_technical_fail_returns_evidence_then_blocks_later_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            model, ports = make_operator(directory, count=2, technical_status="FAIL")
            send(model, "compile_draft", {}, "compile-fail-r001")
            failed = send(model, "run_next", {"run_id": "SYNTHETIC-run-0"}, "run-fail-r001")["result"]
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["code"], "SEED_CAMPAIGN_TECHNICAL_NOT_PASS")
            reconnected = model.core.snapshot()
            self.assertEqual(reconnected["projection"]["campaign"]["campaign"]["state"], "BLOCKED")
            self.assertEqual(ports.fake_factory_calls, 1)
            self.assertTrue(all(ports.counters[name] == 1 for name in FAKE_RECORDER_COUNTERS))

            with self.assertRaisesRegex(ContractError, "CAMPAIGN_OPERATOR_TERMINAL"):
                model.core.consume(operator_intent(
                    reconnected, "run_next", {"run_id": "SYNTHETIC-run-1"}, "later-r001",
                ))
            self.assertEqual(ports.fake_factory_calls, 1)
            self.assertTrue(all(ports.counters[name] == 0 for name in FORBIDDEN_FAKE_COUNTERS))

    def test_cancel_seals_compiled_campaign_without_constructing_a_child(self):
        with tempfile.TemporaryDirectory() as directory:
            model, ports = make_operator(directory, count=2)
            send(model, "compile_draft", {}, "compile-cancel-r001")
            cancelled = send(model, "cancel_campaign", {}, "cancel-r001")["result"]
            self.assertEqual(cancelled["campaign"]["state"], "CANCELLED")
            reconnected = model.core.snapshot()
            self.assertEqual(reconnected["projection"]["campaign"]["state"], "CANCELLED")
            with self.assertRaisesRegex(ContractError, "CAMPAIGN_OPERATOR_CANCELLED"):
                model.core.consume(operator_intent(
                    reconnected, "run_next", {"run_id": "SYNTHETIC-run-0"}, "later-cancel-r001",
                ))
            self.assertEqual(ports.fake_factory_calls, 0)
            self.assertEqual(sum(ports.counters.values()), 0)

    def test_usage_is_enforced_by_the_seed_campaign_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            contract = hypothesis()
            program = draft(contract, count=1)["program_budget"]
            usage = {
                "rounds": 0, "physical_episodes": 0, "rollout_trials": 0,
                "hil_prompts": 0, "reviews": 0,
                "pending_reviews": program["max_pending_reviews"],
                "storage_bytes": 0,
            }
            model, ports = make_operator(directory, current_usage=usage)
            send(model, "compile_draft", {}, "compile-usage-r001")
            blocked = send(
                model, "run_next", {"run_id": "SYNTHETIC-run-0"}, "run-usage-r001",
            )["result"]
            self.assertEqual(
                (blocked["ok"], blocked["code"]),
                (False, "SEED_CAMPAIGN_PENDING_REVIEW_CEILING"),
            )
            self.assertEqual(
                model.projection()["campaign"]["campaign"]["state"], "BLOCKED",
            )
            self.assertEqual(sum(ports.counters.values()), 0)

    def test_projection_and_cancel_remain_available_while_episode_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            model, ports = make_operator(directory)
            entered = threading.Event()

            def waiting(_intent, lifecycle, cancel_event, _episode_context):
                lifecycle.begin()
                lifecycle.readiness_status()
                lifecycle.state = "RUNNING"
                entered.set()
                if not cancel_event.wait(1):
                    raise AssertionError("cancel did not reach the active fake episode")
                raise ContractError("SYNTHETIC_CANCELLED")

            model.fake_live_call = waiting
            send(model, "compile_draft", {}, "compile-active-r001")
            outcome = {}
            worker = threading.Thread(
                target=lambda: outcome.update(model.run_next(
                    {"run_id": "SYNTHETIC-run-0"}, model.core.snapshot(),
                )),
            )
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(model.projection()["campaign"]["active_child"])
            self.assertEqual(model.cancel_campaign({}, {})["campaign"]["state"], "CANCELLED")
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcome["code"], "SYNTHETIC_CANCELLED")
            self.assertTrue(all(ports.counters[name] == 0 for name in FORBIDDEN_FAKE_COUNTERS))


if __name__ == "__main__":
    unittest.main()
