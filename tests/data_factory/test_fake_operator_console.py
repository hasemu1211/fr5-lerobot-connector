from __future__ import annotations

import contextlib
import http.client
import io
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

try:
    from .test_campaign_authoring import draft
    from .test_experiment_manifest import hypothesis
except ImportError:
    from test_campaign_authoring import draft
    from test_experiment_manifest import hypothesis

from tools.data_factory.campaign_operator import CampaignOperator
from tools.data_factory.fake_operator_console import (
    FAKE_RECORDER_COUNTERS,
    QA_WORKFLOW,
    TEST_OPERATOR,
    ZERO_SENTINELS,
    FakeOperatorConsole,
    build_fake_operator_console,
    main,
    make_fake_one_job,
    new_effect_counters,
    synthetic_fixture,
)
from tools.data_factory.one_job import TEST_ONLY_READINESS_CONTRACT, OneJob
from tools.data_factory.operator_bridge import (
    INTENT_SCHEMA,
    ButtonDecisionPort,
    LoopbackBridge,
)
from tools.fr5_data_factory import ContractError, canonical_digest


NOW = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
EXPIRES = "2026-08-25T02:00:00Z"


def envelope(view: dict, op: str, payload: dict, name: str) -> dict:
    return {
        "schema_version": INTENT_SCHEMA, "intent_id": name,
        "session_id": view["session_id"], "view_revision": view["revision"],
        "view_digest": view["view_digest"], "op": op, "payload": payload,
    }


def send(console: FakeOperatorConsole, op: str, payload: dict, name: str) -> dict:
    view = console.core.snapshot()
    return console.core.consume(envelope(view, op, payload, name))["result"]


def compile_payload(console: FakeOperatorConsole) -> dict:
    return {"draft_id": console.draft["draft_id"], "data_disposition": "TEST_ONLY"}


def approval_payload(console: FakeOperatorConsole) -> dict:
    return {
        "plan_digest": console.episode_plan["plan_digest"],
        "approval_scope": "HIL_NUMERIC_PROXY", "data_disposition": "TEST_ONLY",
    }


def usage(budget: dict, **changes: int) -> dict[str, int]:
    result = {
        "rounds": budget["used_rounds"],
        "physical_episodes": budget["used_total_physical_episodes"],
        "rollout_trials": budget["used_total_rollout_trials"],
        "hil_prompts": budget["used_total_hil_prompts"],
        "reviews": budget["used_total_reviews"],
        "pending_reviews": budget["used_pending_reviews"],
        "storage_bytes": budget["used_total_storage_bytes"],
    }
    result.update(changes)
    return result


class FakeOperatorConsoleTests(unittest.TestCase):
    def make(
        self, root: str, *, fault=None, technical_status="PASS", current_usage=None,
        expires_at=EXPIRES, count=1,
    ) -> FakeOperatorConsole:
        contract = hypothesis()
        console = build_fake_operator_console(
            hypothesis=contract, draft=draft(contract, count=count), fixture_root=root,
            session_id=f"fake-console-{id(root)}-{fault or technical_status}",
            expires_at=expires_at, fault=fault, technical_status=technical_status,
            current_usage=current_usage, clock=lambda: NOW,
        )
        self.addCleanup(console.close)
        return console

    def assert_zero_sentinels(self, console: FakeOperatorConsole) -> None:
        self.assertTrue(all(console.counters[name] == 0 for name in ZERO_SENTINELS))

    def assert_no_later_intent(self, console: FakeOperatorConsole, name: str) -> None:
        calls = console.factory_calls
        with self.assertRaises(ContractError):
            send(console, "compile_draft", compile_payload(console), f"later-{name}")
        self.assertEqual(console.factory_calls, calls)

    def test_real_owner_path_returns_exact_evidence_review_and_ordered_trace(self):
        with tempfile.TemporaryDirectory() as root:
            contract = hypothesis()
            counters, trace, children = new_effect_counters(), [], []

            def factory():
                child = make_fake_one_job(
                    trace=trace, counters=counters, clock=lambda: NOW,
                )
                children.append(child)
                return child

            console = FakeOperatorConsole(
                session_id="fake-console-r001", hypothesis=contract,
                draft=draft(contract, count=1), fixture_root=root,
                one_job_factory=factory, counters=counters, trace=trace,
                expires_at=EXPIRES, clock=lambda: NOW,
            )
            self.addCleanup(console.close)
            compiled = send(console, "compile_draft", compile_payload(console), "compile-r001")
            self.assertEqual(compiled["outcome"], "AWAITING_APPROVAL")
            self.assertIs(type(console.campaign_operator), CampaignOperator)
            self.assertIs(type(console.button_port), ButtonDecisionPort)
            self.assertEqual((console.factory_calls, len(children)), (1, 1))
            self.assertIs(type(children[0]), OneJob)
            self.assertEqual(children[0].readiness_contract, TEST_ONLY_READINESS_CONTRACT)
            self.assertTrue(children[0].allow_synthetic_test_operator)
            self.assertIs(children[0], console.session.active_lifecycle)

            plan, intent = compiled["episode_plan"], console.intent
            self.assertEqual(plan["normalized_seed"], console.manifest["normalized_seed"])
            self.assertEqual(plan["base_condition"], intent["base_condition"])
            self.assertEqual(plan["robot_start_pose"], intent["robot_start_pose"])
            self.assertEqual(plan["slot"], intent["slot"])
            self.assertEqual(plan["budget_digests"], intent["budget_digests"])
            self.assertEqual(
                plan["resolver_result"]["resolver_result_digest"],
                intent["base_condition"]["resolver_result_digest"],
            )
            port_view = console.button_port.core.snapshot()
            port_pending = port_view["projection"]["pending_plan"]
            self.assertEqual(port_pending["decision_binding_digest"], plan["decision_binding_digest"])
            self.assertEqual(
                port_pending["decision_binding"],
                {key: value for key, value in plan.items() if key != "decision_binding_digest"},
            )
            context = plan["episode_context"]
            self.assertEqual(
                context["context_digest"],
                canonical_digest({key: value for key, value in context.items() if key != "context_digest"}),
            )
            self.assertEqual((context["effect_scope"], context["data_disposition"]), ("FAKE", "TEST_ONLY"))
            self.assertEqual((context["root_binding"], context["start_binding"]), (None, None))
            tampered = {**context, "effect_scope": "PHYSICAL"}
            with self.assertRaisesRegex(ContractError, "FAKE_CONSOLE_EPISODE_CONTEXT"):
                console._episode_context(intent, tampered)

            accepted = send(
                console, "approve_exact_plan", approval_payload(console), "approve-r001",
            )
            self.assertEqual(accepted["outcome"], "RUNNING")
            self.assertEqual(accepted["decision"]["decision_source"], "LOCAL_UI_BUTTON")
            consumed_button = console._port_intent(
                port_view, "approve_exact_plan", port_pending["decision_binding_digest"],
                "button-approve-0",
            )
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
                console.button_port.core.consume(consumed_button)
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_STALE_VIEW"):
                console.button_port.core.consume(console._port_intent(
                    port_view, "approve_exact_plan", port_pending["decision_binding_digest"],
                    "button-stale-r001",
                ))
            result = console.wait_for_episode()
            self.assertEqual((result["outcome"], result["code"]), ("PASS", "TECHNICAL_PASS"))
            self.assertEqual(result["technical_evidence"]["intent_digest"], intent["intent_digest"])
            self.assertEqual(result["technical_evidence"]["post_scene_digest"], console._scene_digest)
            self.assertEqual(result["intent_binding"]["normalized_seed"], plan["normalized_seed"])
            self.assertEqual(result["intent_binding"]["slot"], plan["slot"])
            self.assertEqual(result["intent_binding"]["budget_digests"], plan["budget_digests"])
            self.assertEqual(result["intent_binding"]["episode_plan_digest"], canonical_digest(plan))
            self.assertEqual(result["intent_binding"]["episode_context_digest"], context["context_digest"])
            self.assertEqual((result["one_job"]["frozen_rows"], result["one_job"]["rows_after_recycle"]), (60, 60))
            self.assertEqual(result["campaign"]["state"], "COMPLETE")
            self.assertEqual(result["human_semantic"], "NOT_MEASURED")
            self.assertEqual(result["synthetic_review"]["reviewed_by"], TEST_OPERATOR)
            self.assertEqual(result["synthetic_review"]["human_semantic"], "NOT_MEASURED")
            self.assertEqual(result["synthetic_coverage_update"]["production_coverage_delta"], 0)
            self.assertEqual(result["result_digest"], canonical_digest({
                key: value for key, value in result.items() if key != "result_digest"
            }))
            self.assertFalse(console.session.status()["active_child"])
            self.assertTrue(all(counters[name] == 1 for name in FAKE_RECORDER_COUNTERS))
            self.assert_zero_sentinels(console)

            def values(value):
                if isinstance(value, dict):
                    return [item for child in value.values() for item in values(child)]
                if isinstance(value, list):
                    return [item for child in value for item in values(child)]
                return [value]

            self.assertNotIn("HUMAN", values(console.core.snapshot()))
            self.assertEqual(trace, [
                "factory:OneJob:1", f"context:{context['context_digest']}",
                "executor:plan", "console:AWAITING_APPROVAL",
                "button:APPROVE", "one_job:approve", "executor:approve",
                "one_job:start", "recorder:begin", "recorder:readiness_status",
                "executor:execute", "recorder:status", "executor:heartbeat",
                "recorder:freeze", f"semantic:{TEST_OPERATOR}:HIL_PROXY:PASS",
                "executor:semantic_verdict", "recorder:status", "executor:heartbeat",
                f"release:{TEST_OPERATOR}:LANDED", "executor:release_verdict",
                "recorder:status", "executor:heartbeat", "recorder:commit",
                "one_job:finish", "cell:TEST_OPERATOR_ACKNOWLEDGED",
                "campaign:technical_PASS", "review:TEST_OPERATOR:SYNTHETIC",
                "coverage:SYNTHETIC_TEST_ONLY:+1",
            ])

    def test_existing_loopback_bridge_serves_ui_and_nonblocking_real_handlers(self):
        with tempfile.TemporaryDirectory() as root:
            console = self.make(root)
            bridge = LoopbackBridge(
                core=console.bridge_core,
                ui_root=Path(__file__).resolve().parents[2] / "operator-ui",
                host="127.0.0.1", port=0,
                token="fixed-fake-console-token-long-enough",
            )
            thread = threading.Thread(target=bridge.serve_forever)
            thread.start()
            headers = {
                "Host": f"127.0.0.1:{bridge.port}",
                "Origin": f"http://127.0.0.1:{bridge.port}",
                "X-Operator-Token": bridge.token, "Content-Type": "application/json",
            }
            try:
                connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
                connection.request("GET", "/")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn(b"operator-token", response.read())
                connection.close()

                connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
                connection.request("GET", "/api/view")
                self.assertEqual(connection.getresponse().status, 403)
                connection.close()

                connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
                connection.request("GET", "/api/view", headers={"X-Operator-Token": bridge.token})
                view = json.loads(connection.getresponse().read())
                connection.close()
                body = json.dumps(envelope(
                    view, "compile_draft", compile_payload(console), "http-compile-r001",
                ))
                connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
                connection.request("POST", "/api/intent", body=body, headers=headers)
                response = connection.getresponse()
                result = json.loads(response.read())
                connection.close()
                self.assertEqual((response.status, result["result"]["outcome"]), (200, "AWAITING_APPROVAL"))
                self.assertIs(type(console.session.active_lifecycle), OneJob)

                connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
                connection.request("GET", "/api/view", headers={"X-Operator-Token": bridge.token})
                view = json.loads(connection.getresponse().read())
                connection.close()
                body = json.dumps(envelope(
                    view, "approve_exact_plan", {
                        "plan_digest": view["projection"]["approval"]["plan_digest"],
                        "approval_scope": "HIL_NUMERIC_PROXY", "data_disposition": "TEST_ONLY",
                    }, "http-approve-r001",
                ))
                connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
                connection.request("POST", "/api/intent", body=body, headers=headers)
                response = connection.getresponse()
                result = json.loads(response.read())
                connection.close()
                self.assertEqual((response.status, result["result"]["outcome"]), (200, "RUNNING"))

                connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
                connection.request("GET", "/api/view", headers={"X-Operator-Token": bridge.token})
                running = json.loads(connection.getresponse().read())
                connection.close()
                self.assertEqual(running["projection"]["runtime"]["workflow_state"], "RUNNING")
                self.assertIn("cancel_session", running["projection"]["available_ops"])
                self.assertEqual(console.wait_for_episode()["outcome"], "PASS")
            finally:
                bridge.close()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_reject_cancel_before_approval_and_button_replay_are_single_use(self):
        with tempfile.TemporaryDirectory() as root:
            for choice in ("REJECT", "CANCEL"):
                with self.subTest(choice=choice):
                    console = self.make(root)
                    send(console, "compile_draft", compile_payload(console), f"compile-{choice}")
                    view = console.core.snapshot()
                    if choice == "REJECT":
                        op, payload = "reject_plan", approval_payload(console)
                    else:
                        op = "cancel_session"
                        payload = {"active_child_id": console.intent["run_id"]}
                    sent = envelope(view, op, payload, f"{choice.lower()}-r001")
                    result = console.core.consume(sent)["result"]
                    self.assertIn(result["outcome"], {choice, "CANCELLING"})
                    final = console.wait_for_episode()
                    self.assertEqual(final["outcome"], choice)
                    self.assertFalse(console.session.status()["active_child"])
                    with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
                        console.core.consume(sent)
                    self.assert_no_later_intent(console, choice)
                    self.assertTrue(all(console.counters[name] == 0 for name in FAKE_RECORDER_COUNTERS))
                    self.assert_zero_sentinels(console)

    def test_stale_and_wrong_digest_do_not_unblock_port_or_begin(self):
        with tempfile.TemporaryDirectory() as root:
            console = self.make(root)
            initial = console.core.snapshot()
            compile_intent = envelope(
                initial, "compile_draft", compile_payload(console), "compile-r001",
            )
            console.core.consume(compile_intent)
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
                console.core.consume(compile_intent)
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_STALE_VIEW"):
                console.core.consume(envelope(
                    initial, "compile_draft", compile_payload(console), "compile-stale-r001",
                ))
            with self.assertRaisesRegex(ContractError, "FAKE_CONSOLE_PLAN_DIGEST_MISMATCH"):
                send(console, "approve_exact_plan", {
                    "plan_digest": canonical_digest("wrong"),
                    "approval_scope": "HIL_NUMERIC_PROXY", "data_disposition": "TEST_ONLY",
                }, "wrong-plan-r001")
            pending = console.button_port.core.snapshot()["projection"]["pending_plan"]
            self.assertIsNotNone(pending)
            self.assertEqual(console.factory_calls, 1)
            self.assertEqual(console.counters["fake_recorder_begin"], 0)
            self.assert_zero_sentinels(console)

    def test_cancel_after_approval_is_visible_and_stops_before_begin(self):
        with tempfile.TemporaryDirectory() as root:
            console = self.make(root)
            send(console, "compile_draft", compile_payload(console), "compile-cancel-running")
            accepted = send(
                console, "approve_exact_plan", approval_payload(console), "approve-cancel-running",
            )
            self.assertEqual(accepted["outcome"], "RUNNING")
            running = console.core.snapshot()["projection"]
            self.assertEqual(running["runtime"]["workflow_state"], "RUNNING")
            cancelled = send(console, "cancel_session", {
                "active_child_id": running["runtime"]["active_child_id"],
            }, "cancel-running")
            self.assertEqual(cancelled["outcome"], "CANCELLING")
            final = console.wait_for_episode()
            self.assertEqual(final["campaign"]["state"], "CANCELLED")
            self.assertEqual(console.counters["fake_recorder_begin"], 0)
            self.assert_no_later_intent(console, "cancel-running")
            self.assert_zero_sentinels(console)

    def test_readiness_rate_drop_and_fault_fail_close_before_execute(self):
        expected = {
            "readiness_rate": "RECORDER_READINESS_ROW_FPS",
            "readiness_drop": "RECORDER_READINESS_DROPS",
            "readiness_fault": "RECORDER_WRITER_FAULT",
        }
        with tempfile.TemporaryDirectory() as root:
            for fault, code in expected.items():
                with self.subTest(fault=fault):
                    console = self.make(root, fault=fault)
                    send(console, "compile_draft", compile_payload(console), f"compile-{fault}")
                    self.assertEqual(send(
                        console, "approve_exact_plan", approval_payload(console), f"approve-{fault}",
                    )["outcome"], "RUNNING")
                    result = console.wait_for_episode()
                    self.assertEqual((result["outcome"], result["code"]), ("FAIL", code))
                    self.assertNotIn("executor:execute", console.trace)
                    self.assertEqual(
                        (console.counters["fake_recorder_begin"], console.counters["fake_recorder_readiness_status"]),
                        (1, 1),
                    )
                    self.assertEqual(
                        (console.counters["fake_recorder_freeze"], console.counters["fake_recorder_commit"]),
                        (0, 0),
                    )
                    self.assert_no_later_intent(console, fault)
                    self.assert_zero_sentinels(console)

    def test_executor_plan_and_technical_fail_have_no_later_intent(self):
        with tempfile.TemporaryDirectory() as root:
            planned = self.make(root, fault="executor_plan")
            failed = send(planned, "compile_draft", compile_payload(planned), "compile-plan-fail")
            self.assertEqual((failed["outcome"], failed["code"]), ("FAIL", "SYNTHETIC_EXECUTOR_PLAN_FAIL"))
            self.assertEqual(planned.factory_calls, 1)
            self.assertTrue(all(planned.counters[name] == 0 for name in FAKE_RECORDER_COUNTERS))
            self.assert_no_later_intent(planned, "executor-plan")
            self.assert_zero_sentinels(planned)

            technical = self.make(root, technical_status="FAIL")
            send(technical, "compile_draft", compile_payload(technical), "compile-technical-fail")
            send(
                technical, "approve_exact_plan", approval_payload(technical),
                "approve-technical-fail",
            )
            failed = technical.wait_for_episode()
            self.assertEqual(
                (failed["outcome"], failed["code"]),
                ("FAIL", "SEED_CAMPAIGN_TECHNICAL_NOT_PASS"),
            )
            self.assertEqual(failed["technical_evidence"]["status"], "FAIL")
            self.assertTrue(all(technical.counters[name] == 1 for name in FAKE_RECORDER_COUNTERS))
            self.assertIsNone(failed.get("synthetic_review"))
            self.assert_no_later_intent(technical, "technical")
            self.assert_zero_sentinels(technical)

    def test_pending_review_quota_and_expiry_fail_in_seed_campaign_before_effects(self):
        with tempfile.TemporaryDirectory() as root:
            contract = hypothesis()
            program = draft(contract, count=1)["program_budget"]
            cases = (
                ("pending", usage(program, pending_reviews=program["max_pending_reviews"]), EXPIRES, "SEED_CAMPAIGN_PENDING_REVIEW_CEILING"),
                ("quota", usage(program, physical_episodes=program["max_total_physical_episodes"]), EXPIRES, "SEED_CAMPAIGN_PROGRAM_QUOTA"),
                ("expiry", None, NOW.isoformat().replace("+00:00", "Z"), "SEED_CAMPAIGN_EXPIRED"),
            )
            for name, current, expiry, code in cases:
                with self.subTest(name=name):
                    console = build_fake_operator_console(
                        hypothesis=contract, draft=draft(contract, count=1), fixture_root=root,
                        session_id=f"fake-{name}", expires_at=expiry,
                        current_usage=current, clock=lambda: NOW,
                    )
                    self.addCleanup(console.close)
                    failed = send(console, "compile_draft", compile_payload(console), f"compile-{name}")
                    self.assertEqual((failed["outcome"], failed["code"]), ("FAIL", code))
                    self.assertEqual(console.factory_calls, 1)
                    self.assertEqual(console.session.status()["campaign"]["state"], "BLOCKED")
                    self.assertFalse(console.session.status()["active_child"])
                    self.assert_no_later_intent(console, name)
                    self.assertTrue(all(console.counters[key] == 0 for key in FAKE_RECORDER_COUNTERS))
                    self.assert_zero_sentinels(console)

    def test_two_episodes_get_two_distinct_one_jobs_only_after_prior_pass(self):
        with tempfile.TemporaryDirectory() as root:
            console = self.make(root, count=2)
            children = []
            original = console._one_job_factory

            def factory():
                child = original()
                children.append(child)
                return child

            console._one_job_factory = factory
            first = send(console, "compile_draft", compile_payload(console), "compile-first")
            first_intent = first["episode_plan"]["intent_digest"]
            send(console, "approve_exact_plan", approval_payload(console), "approve-first")
            self.assertEqual(console.wait_for_episode()["campaign"]["state"], "READY")
            self.assertEqual(console.factory_calls, 1)

            second = send(console, "compile_draft", compile_payload(console), "compile-second")
            self.assertEqual(console.factory_calls, 2)
            self.assertNotEqual(second["episode_plan"]["intent_digest"], first_intent)
            self.assertIsNot(children[0], children[1])
            send(console, "approve_exact_plan", approval_payload(console), "approve-second")
            self.assertEqual(console.wait_for_episode()["campaign"]["state"], "COMPLETE")
            self.assertTrue(all(console.counters[name] == 2 for name in FAKE_RECORDER_COUNTERS))
            self.assert_zero_sentinels(console)

    def test_ui_partial_authoring_and_fake_workspace_wizard_are_effect_neutral(self):
        with tempfile.TemporaryDirectory() as root:
            console = self.make(root)
            draft_id = console.draft["draft_id"]
            send(console, "update_draft", {"draft_id": draft_id, "budget": 2}, "budget")
            changed = send(console, "update_draft", {
                "draft_id": draft_id, "authoring_mode": "DIRECT_EDIT",
            }, "direct")
            self.assertEqual(changed["authoring_mode"], "DIRECT_EDIT")
            changed = send(console, "update_draft", {
                "draft_id": draft_id, "authoring_mode": "ASSISTED",
            }, "assisted")
            self.assertEqual(changed["authoring_mode"], "ASSISTED")
            send(console, "update_draft", {
                "draft_id": draft_id, "authoring_mode": "DIRECT_EDIT",
            }, "direct-again")
            view = console.core.snapshot()["projection"]
            available = next(
                item["cell_id"] for item in view["draft"]["cells"]
                if item["selection_state"] == "AVAILABLE"
            )
            send(console, "update_draft", {
                "draft_id": draft_id, "toggle_cell_id": available,
            }, "toggle")
            plane = view["workspace_wizard"]["plane_reference"]["digest"]
            with self.assertRaisesRegex(ContractError, "FAKE_CONSOLE_WORKSPACE_CAPTURE"):
                send(console, "capture_workspace_point", {
                    "draft_id": draft_id, "mode": "FAKE", "point": "CENTER",
                    "source_measurement_mm": 100, "final_measurement_mm": 99,
                    "plane_reference_digest": canonical_digest("wrong-plane"),
                }, "capture-wrong-plane")
            with self.assertRaisesRegex(ContractError, "WORKSPACE_FINAL_PRINT_OUT_OF_TOLERANCE"):
                send(console, "capture_workspace_point", {
                    "draft_id": draft_id, "mode": "FAKE", "point": "CENTER",
                    "source_measurement_mm": 100, "final_measurement_mm": 95,
                    "plane_reference_digest": plane,
                }, "capture-invalid-measurement")
            for index, point in enumerate(("CENTER", "X_REF", "Y_CHECK")):
                send(console, "capture_workspace_point", {
                    "draft_id": draft_id, "mode": "FAKE", "point": point,
                    "source_measurement_mm": 100, "final_measurement_mm": 99,
                    "plane_reference_digest": plane,
                }, f"capture-{index}")
            saved = send(console, "save_workspace_revision", {
                "draft_id": draft_id, "mode": "FAKE",
                "source_measurement_mm": 100, "final_measurement_mm": 99,
            }, "save")
            self.assertEqual((saved["mode"], saved["identity"]), ("FAKE", "SYNTHETIC"))
            self.assertEqual(console.factory_calls, 0)
            self.assertTrue(all(console.counters[name] == 0 for name in FAKE_RECORDER_COUNTERS))
            self.assert_zero_sentinels(console)

    def test_default_fixture_cli_and_projection_support_under_ten_minute_qa(self):
        contract, source = synthetic_fixture()
        self.assertEqual(contract["qualification_catalog"]["source"], "SYNTHETIC_TEST_ONLY")
        self.assertEqual(source["schema_version"], "data_factory.campaign_draft.v1")
        output = io.StringIO()
        with patch.object(LoopbackBridge, "serve_forever", autospec=True), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--port", "0"]), 0)
        startup = json.loads(output.getvalue().strip())
        self.assertEqual((startup["effect_scope"], startup["operator_identity"]), ("FAKE", TEST_OPERATOR))
        self.assertFalse(Path(startup["fixture_root"]).exists())
        self.assertEqual(tuple(startup["qa_workflow"]), QA_WORKFLOW)
        self.assertLessEqual(len(QA_WORKFLOW), 6)
        self.assertNotIn("copy", " ".join(QA_WORKFLOW).lower())


if __name__ == "__main__":
    unittest.main()
