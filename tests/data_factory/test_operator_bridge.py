from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.data_factory.operator_bridge import (
    ButtonDecisionPort,
    INTENT_SCHEMA,
    LoopbackBridge,
    OperatorIntentCore,
)
from tools.fr5_data_factory import ContractError, canonical_digest


NOW = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)


def intent(snapshot: dict, op: str, payload: dict, name: str = "intent-r001") -> dict:
    return {
        "schema_version": INTENT_SCHEMA,
        "intent_id": name,
        "session_id": snapshot["session_id"],
        "view_revision": snapshot["revision"],
        "view_digest": snapshot["view_digest"],
        "op": op,
        "payload": payload,
    }


class OperatorIntentCoreTests(unittest.TestCase):
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


class LoopbackBridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        (root / "index.html").write_text(
            '<!doctype html><html><head><!-- OPERATOR_TOKEN --></head><body>fixture</body></html>',
            encoding="utf-8",
        )
        (root / "app.js").write_text("const fixture = true;", encoding="utf-8")
        self.state = {"mode": "FAKE", "hardware_calls": 0, "value": 1}

        def update(payload, _view):
            if set(payload) != {"value"} or type(payload["value"]) is not int:
                raise ContractError("UPDATE_FIELDS")
            self.state["value"] = payload["value"]
            return {"value": self.state["value"]}

        self.core = OperatorIntentCore(
            session_id="http-session-r001", projection_call=lambda: self.state,
            handlers={"update_fixture": update}, clock=lambda: NOW,
        )
        self.bridge = LoopbackBridge(
            core=self.core, ui_root=root, host="127.0.0.1", port=0,
            token="fixed-test-token-that-is-long-enough",
        )
        self.thread = threading.Thread(target=self.bridge.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.bridge.close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.directory.cleanup()

    def request(self, method: str, path: str, *, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.bridge.port, timeout=2)
        payload = body
        if isinstance(body, dict):
            payload = json.dumps(body, separators=(",", ":"), allow_nan=False)
        connection.request(method, path, body=payload, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, response.getheaders(), data

    def post(self, payload, *, token=None, origin=None, host=None):
        host = host or f"127.0.0.1:{self.bridge.port}"
        headers = {
            "Host": host,
            "Origin": origin or f"http://{host}",
            "X-Operator-Token": token or self.bridge.token,
            "Content-Type": "application/json",
        }
        return self.request("POST", "/api/intent", body=payload, headers=headers)

    def test_real_loopback_static_snapshot_and_intent_round_trip(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'<meta name="operator-token" content="fixed-test-token-that-is-long-enough">', body)
        self.assertIn(("Cache-Control", "no-store"), headers)
        view_headers = {"X-Operator-Token": self.bridge.token}
        status, _, body = self.request("GET", "/api/view", headers=view_headers)
        self.assertEqual(status, 200)
        snapshot = json.loads(body)
        status, _, body = self.post(intent(
            snapshot, "update_fixture", {"value": 2}, "http-intent-r001",
        ))
        result = json.loads(body)
        self.assertEqual((status, result["consumed"], self.state), (200, True, {"mode": "FAKE", "hardware_calls": 0, "value": 2}))
        status, _, body = self.request("GET", "/api/view", headers=view_headers)
        reconnected = json.loads(body)
        self.assertEqual((reconnected["revision"], reconnected["projection"]["value"]), (1, 2))

    def test_origin_token_host_stale_replay_and_malformed_json_are_rejected(self):
        for token in (None, "wrong-token-that-is-still-long-enough"):
            headers = {} if token is None else {"X-Operator-Token": token}
            status, _, body = self.request("GET", "/api/view", headers=headers)
            self.assertEqual((status, json.loads(body)["code"]), (403, "BRIDGE_TOKEN"))

        snapshot = self.core.snapshot()
        valid = intent(snapshot, "update_fixture", {"value": 2}, "http-intent-r001")
        cases = (
            ({"token": "wrong-token-that-is-still-long-enough"}, 403, "BRIDGE_TOKEN"),
            ({"origin": "http://evil.invalid"}, 403, "BRIDGE_ORIGIN"),
            ({"host": "evil.invalid"}, 400, "BRIDGE_HOST"),
        )
        for kwargs, expected, code in cases:
            with self.subTest(code=code):
                status, _, body = self.post(valid, **kwargs)
                self.assertEqual((status, json.loads(body)["code"]), (expected, code))
        self.assertEqual(self.core.snapshot()["revision"], 0)

        status, _, body = self.post(valid)
        self.assertEqual(status, 200)
        status, _, body = self.post(valid)
        self.assertEqual((status, json.loads(body)["code"]), (409, "OPERATOR_INTENT_REPLAY"))
        stale = intent(snapshot, "update_fixture", {"value": 3}, "http-intent-r002")
        status, _, body = self.post(stale)
        self.assertEqual((status, json.loads(body)["code"]), (409, "OPERATOR_INTENT_STALE_VIEW"))

        headers = {
            "Host": f"127.0.0.1:{self.bridge.port}",
            "Origin": f"http://127.0.0.1:{self.bridge.port}",
            "X-Operator-Token": self.bridge.token,
            "Content-Type": "application/json",
        }
        duplicate = '{"schema_version":"data_factory.operator_intent.v1","schema_version":"duplicate"}'
        status, _, body = self.request("POST", "/api/intent", body=duplicate, headers=headers)
        self.assertEqual((status, json.loads(body)["code"]), (409, "BRIDGE_JSON_DUPLICATE_KEY"))
        nonfinite = json.dumps(valid).replace('"value": 2', '"value": NaN')
        status, _, body = self.request("POST", "/api/intent", body=nonfinite, headers=headers)
        self.assertEqual((status, json.loads(body)["code"]), (409, "BRIDGE_JSON_NONFINITE"))

    def test_non_loopback_and_static_traversal_are_refused(self):
        with self.assertRaisesRegex(ContractError, "BRIDGE_LOOPBACK_REQUIRED"):
            LoopbackBridge(core=self.core, ui_root=self.directory.name, host="0.0.0.0")
        status, _, body = self.request("GET", "/../outside")
        self.assertEqual((status, json.loads(body)["code"]), (404, "BRIDGE_STATIC_PATH"))


if __name__ == "__main__":
    unittest.main()
