from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from tools.data_factory.operator.workflow.intents import (
    ButtonDecisionPort,
    CandidateReviewPort,
    CHECKPOINT_REQUEST_SCHEMA,
    INTENT_SCHEMA,
    OperatorCheckpointPort,
    OperatorIntentCore,
    UnlockedIntent,
)
from ..fixtures import (
    NOW,
    intent,
    review_candidate_admission,
)
from tools.fr5_data_factory import ContractError, canonical_digest


class OperatorIntentCoreTests(unittest.TestCase):
    def test_same_pending_review_survives_progress_but_other_intents_stay_strict(self):
        binding = canonical_digest("pending-candidate")
        state = {
            "progress": 1,
            "available_ops": ["review_candidate", "cancel_session", "edit_draft", "approve_exact_plan"],
            "candidate_review": {"status": "PENDING", "review_binding_digest": binding},
        }
        calls = []
        core = OperatorIntentCore(
            session_id="scoped-review", projection_call=lambda: state,
            handlers={op: lambda payload, view: calls.append(payload) or {}
                      for op in state["available_ops"]}, clock=lambda: NOW,
        )
        viewed = core.snapshot()
        payload = {"review_binding_digest": binding, "choice": "PASS", "reason": None}
        state["progress"] = 2  # Progress arrives after the operator's GET.
        for op in ("cancel_session", "edit_draft", "approve_exact_plan"):
            with self.subTest(op=op), self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_STALE_VIEW"):
                core.consume(intent(viewed, op, payload, f"stale-{op}"))
        request = intent(viewed, "review_candidate", payload, "same-review")
        self.assertTrue(core.consume(request)["consumed"])
        self.assertEqual(calls, [payload])
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
            core.consume(request)

    def test_review_scope_rejects_changed_target_session_invalid_view_or_unavailable_op(self):
        binding = canonical_digest("pending-candidate")
        for case in ("target", "resolved", "session", "negative", "future", "bool", "digest", "unavailable"):
            with self.subTest(case=case):
                state = {
                    "progress": 1, "available_ops": ["review_candidate"],
                    "candidate_review": {"status": "PENDING", "review_binding_digest": binding},
                }
                calls = []
                core = OperatorIntentCore(
                    session_id="scoped-review", projection_call=lambda: state,
                    handlers={"review_candidate": lambda *_: calls.append(True) or {}},
                    clock=lambda: NOW,
                )
                request = intent(core.snapshot(), "review_candidate", {
                    "review_binding_digest": binding, "choice": "PASS", "reason": None,
                })
                state["progress"] = 2
                if case == "target":
                    state["candidate_review"]["review_binding_digest"] = canonical_digest("next-candidate")
                elif case == "resolved":
                    state["candidate_review"]["status"] = "PASS"
                elif case == "session":
                    request["session_id"] = "other-session"
                elif case in {"negative", "future", "bool"}:
                    request["view_revision"] = {"negative": -1, "future": 99, "bool": False}[case]
                elif case == "digest":
                    request["view_digest"] = "malformed"
                elif case == "unavailable":
                    state["available_ops"] = []
                with self.assertRaises(ContractError):
                    core.consume(request)
                self.assertEqual(calls, [])

    def test_revision_digest_replay_and_browser_authority_fail_closed(self):
        state = {"mode": "FAKE", "count": 1, "hardware_calls": 0}

        def update(payload, view):
            if set(payload) != {"count"} or type(payload["count"]) is not int:
                raise ContractError("DRAFT_EDIT_FIELDS")
            state["count"] = payload["count"]
            return {"draft_count": state["count"]}

        core = OperatorIntentCore(
            session_id="session-r001", projection_call=lambda: state,
            handlers={"edit_draft": update}, clock=lambda: NOW,
        )
        before = core.snapshot()
        result = core.consume(intent(before, "edit_draft", {"count": 3}))
        self.assertTrue(result["consumed"])
        self.assertEqual((core.snapshot()["revision"], core.snapshot()["projection"]["count"]), (1, 3))
        self.assertEqual(state["hardware_calls"], 0)
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
            core.consume(intent(before, "edit_draft", {"count": 3}))
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_STALE_VIEW"):
            core.consume(intent(before, "edit_draft", {"count": 4}, "intent-r002"))
        current = core.snapshot()
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_AUTHORITY"):
            core.consume(intent(
                current, "edit_draft", {"source": "HUMAN", "count": 4}, "intent-r003",
            ))
        self.assertEqual(core.snapshot()["revision"], 1)

    def test_revision_wait_wakes_on_transition_and_reobserves_external_state(self):
        state = {"value": 1}
        core = OperatorIntentCore(
            session_id="watch-session-r001", projection_call=lambda: state,
            handlers={"noop": lambda _payload, _view: {}}, clock=lambda: NOW,
        )
        initial = core.snapshot()
        observed = []
        started = threading.Event()

        def wait():
            started.set()
            observed.append(core.wait_for_snapshot(initial["revision"], 1))

        thread = threading.Thread(target=wait)
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        core.transition(lambda: state.update(value=2))
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(
            (observed[0]["revision"], observed[0]["projection"]["value"]),
            (1, 2),
        )

        state["value"] = 3
        heartbeat = core.wait_for_snapshot(observed[0]["revision"], 0.01)
        self.assertEqual(
            (heartbeat["revision"], heartbeat["projection"]["value"]),
            (2, 3),
        )
        with self.assertRaisesRegex(ContractError, "OPERATOR_VIEW_REVISION_FUTURE"):
            core.wait_for_snapshot(heartbeat["revision"] + 1, 0.01)

    def test_existing_waiter_observes_unlocked_intent_while_post_is_running(self):
        state = {"phase": "IDLE", "available_ops": ["run"]}
        run_entered = threading.Event()
        release_run = threading.Event()
        self.addCleanup(release_run.set)

        def run():
            run_entered.set()
            if not release_run.wait(timeout=1):
                raise AssertionError("test did not release intent")
            return None

        def complete(_produced):
            state["phase"] = "DONE"
            return {"phase": "DONE"}, True, None

        def start(_payload, _view):
            state["phase"] = "RUNNING"
            return UnlockedIntent(
                run=run, complete=complete,
                failed=lambda _exc, _produced: (False, None),
            )

        core = OperatorIntentCore(
            session_id="watch-intent-r001", projection_call=lambda: state,
            handlers={"run": start}, clock=lambda: NOW,
        )
        initial = core.snapshot()
        observed = []
        waiter = threading.Thread(target=lambda: observed.append(
            core.wait_for_snapshot(initial["revision"], 1),
        ))
        waiter.start()
        results = []
        post = threading.Thread(target=lambda: results.append(core.consume(intent(
            initial, "run", {}, "watch-intent-r001",
        ))))
        post.start()
        self.assertTrue(run_entered.wait(timeout=1))
        waiter.join(timeout=1)
        self.assertFalse(waiter.is_alive())
        self.assertTrue(post.is_alive())
        self.assertEqual(
            (observed[0]["revision"], observed[0]["projection"]["phase"]),
            (1, "RUNNING"),
        )

        milestone = []
        milestone_waiter = threading.Thread(target=lambda: milestone.append(
            core.wait_for_snapshot(observed[0]["revision"], 1),
        ))
        milestone_waiter.start()
        core.transition(lambda: state.update(phase="MILESTONE"))
        milestone_waiter.join(timeout=1)
        self.assertEqual(
            (milestone[0]["revision"], milestone[0]["projection"]["phase"]),
            (2, "MILESTONE"),
        )
        release_run.set()
        post.join(timeout=1)
        self.assertFalse(post.is_alive())
        self.assertEqual(results[0]["current_view_revision"], 3)

    def test_button_port_binds_exact_plan_without_minting_human_identity(self):
        port = ButtonDecisionPort(
            session_id="session-r001", operator_label="local-operator", clock=lambda: NOW,
        )
        plan_digest = canonical_digest("plan")
        offered = port.offer(
            run_id="run-r001", plan_digest=plan_digest,
            decision_binding={
                "place_alias": "place1", "place_id": "PLACE_A", "yaw_deg": 0,
                "x_mm": 0, "y_mm": 0, "data_disposition": "TEST_ONLY",
            },
            approval_scope="HIL_NUMERIC_PROXY",
        )
        pending = offered["projection"]["pending_plan"]
        self.assertFalse(offered["projection"]["authenticated_human_identity"])
        approved = port.core.consume(intent(
            offered, "approve_exact_plan",
            {"decision_binding_digest": pending["decision_binding_digest"]},
        ))
        self.assertEqual(approved["result"]["choice"], "APPROVE")
        self.assertEqual(approved["result"]["decision_source"], "LOCAL_UI_BUTTON")
        self.assertNotIn("approved_by", approved["result"])
        self.assertEqual(port.wait(0), approved["result"])
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
            port.core.consume(intent(
                offered, "approve_exact_plan",
                {"decision_binding_digest": pending["decision_binding_digest"]},
            ))

        other = ButtonDecisionPort(
            session_id="session-r002", operator_label="local-operator", clock=lambda: NOW,
        )
        snapshot = other.offer(
            run_id="run-r002", plan_digest=canonical_digest("other"),
            decision_binding={"data_disposition": "TEST_ONLY"},
            approval_scope="HUMAN_GATED",
        )
        with self.assertRaisesRegex(ContractError, "BUTTON_PLAN_DIGEST_MISMATCH"):
            other.core.consume(intent(
                snapshot, "approve_exact_plan",
                {"decision_binding_digest": canonical_digest("wrong")},
            ))
        self.assertIsNone(other.wait(0))

    def test_button_port_callable_round_trip_uses_the_same_cas_core(self):
        port = ButtonDecisionPort(
            session_id="session-r003", operator_label="local-operator", clock=lambda: NOW,
        )
        request = {
            "schema_version": "data_factory.plan_decision_request.v1",
            "run_id": "run-r003",
            "plan_digest": canonical_digest("plan-r003"),
            "approval_scope": "HIL_NUMERIC_PROXY",
            "decision_binding": {"data_disposition": "TEST_ONLY"},
            "timeout_s": 1,
        }
        observed = []
        thread = threading.Thread(target=lambda: observed.append(port(request)))
        thread.start()
        for _ in range(100):
            snapshot = port.core.snapshot()
            pending = snapshot["projection"]["pending_plan"]
            if pending is not None:
                break
            threading.Event().wait(0.001)
        else:
            self.fail("button request was not offered")
        port.core.consume(intent(
            snapshot, "approve_exact_plan",
            {"decision_binding_digest": pending["decision_binding_digest"]},
            "intent-r003",
        ))
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual((observed[0]["choice"], observed[0]["plan_digest"]), ("APPROVE", request["plan_digest"]))

    def test_checkpoint_port_binds_backend_evidence_and_accepts_one_choice(self):
        port = OperatorCheckpointPort(operator_label="local-operator")
        request = {
            "schema_version": CHECKPOINT_REQUEST_SCHEMA,
            "kind": "RELEASE_VERDICT",
            "run_id": "run-r004",
            "plan_digest": canonical_digest("plan-r004"),
            "prompt": "Confirm landing, empty gripper, retreat, and safe staging.",
            "choices": ["LANDED", "OFF_SLOT", "UNCERTAIN"],
            "evidence": {"release_evidence_digest": canonical_digest("release-r004")},
            "timeout_s": 1,
        }
        observed = []
        offered = port.offer(request)
        self.assertEqual(offered, port.projection())
        thread = threading.Thread(target=lambda: observed.append(port.wait(1)))
        thread.start()
        for _ in range(100):
            pending = port.projection()
            if pending is not None:
                break
            threading.Event().wait(0.001)
        else:
            self.fail("checkpoint was not offered")
        self.assertEqual(pending["evidence"], request["evidence"])
        with self.assertRaisesRegex(ContractError, "CHECKPOINT_DIGEST_MISMATCH"):
            port.resolve({
                "checkpoint_binding_digest": canonical_digest("wrong"),
                "choice": "LANDED",
            })
        resolved = port.resolve({
            "checkpoint_binding_digest": pending["binding_digest"],
            "choice": "LANDED",
        })
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [resolved])
        self.assertEqual(resolved["decision_source"], "LOCAL_UI_BUTTON")
        self.assertNotIn("reviewed_by", resolved)
        with self.assertRaisesRegex(ContractError, "CHECKPOINT_STATE"):
            port.resolve({
                "checkpoint_binding_digest": pending["binding_digest"],
                "choice": "LANDED",
            })

    def test_candidate_review_handler_keeps_path_private_and_reuses_exact_cas(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "candidate_admission.json"
            context_digest = canonical_digest("review-context")
            admission = {
                "schema_version": "data_factory.candidate_admission.v1",
                "run_id": "run-r005", "operational_gate": "PASS",
                "operational_source": "HIL_PROXY", "checklist_id": "pickup-v2",
                "review_context_digest": context_digest,
                "semantic_status": "PENDING", "reviewed_by": None,
                "reviewed_at": None, "reason": None,
            }
            path.write_text(json.dumps(admission), encoding="utf-8")
            port = CandidateReviewPort(
                operator_label="local-operator",
                review_call=lambda target, **kwargs: review_candidate_admission(
                    target, clock=lambda: NOW, **kwargs,
                ),
            )
            projection = port.offer(
                candidate_path=path, run_id="run-r005",
                expected_file_digest=canonical_digest(admission),
                expected_review_context_digest=context_digest,
            )
            self.assertEqual(set(projection), {
                "review_binding_digest", "run_id", "status", "choices", "reasons",
            })
            self.assertIn("IMAGE_QUALITY_OR_VISIBILITY", projection["reasons"])
            self.assertNotIn(str(path), json.dumps(projection))
            progress = [0]
            core = OperatorIntentCore(
                session_id="review-session-r001",
                projection_call=lambda: {"candidate_review": port.projection(), "progress": progress[0]},
                handlers={"review_candidate": port.resolve}, clock=lambda: NOW,
            )
            snapshot = core.snapshot()
            with self.assertRaisesRegex(ContractError, "CANDIDATE_REVIEW_DIGEST_MISMATCH"):
                core.consume(intent(snapshot, "review_candidate", {
                    "review_binding_digest": canonical_digest("wrong"),
                    "choice": "FAIL", "reason": "TASK_GOAL",
                }, "review-intent-wrong"))
            with self.assertRaisesRegex(ContractError, "CANDIDATE_REVIEW_CHOICE"):
                core.consume(intent(snapshot, "review_candidate", {
                    "review_binding_digest": projection["review_binding_digest"],
                    "choice": "PASS", "reason": "TASK_GOAL",
                }, "review-intent-reason"))
            progress[0] = 1
            result = core.consume(intent(snapshot, "review_candidate", {
                "review_binding_digest": projection["review_binding_digest"],
                "choice": "FAIL", "reason": "TASK_GOAL",
            }, "review-intent-r001"))
            reviewed = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                (reviewed["semantic_status"], reviewed["reviewed_by"], reviewed["reason"]),
                ("FAIL", "local-operator", "TASK_GOAL"),
            )
            self.assertNotEqual(reviewed["reviewed_by"], "HUMAN")
            self.assertFalse(result["result"]["training_authorized"])
            self.assertEqual(port.projection()["status"], "FAIL")
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
                core.consume(intent(snapshot, "review_candidate", {
                    "review_binding_digest": projection["review_binding_digest"],
                    "choice": "FAIL", "reason": "TASK_GOAL",
                }, "review-intent-r001"))
