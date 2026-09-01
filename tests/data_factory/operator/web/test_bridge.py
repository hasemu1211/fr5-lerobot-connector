from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from ..fixtures import (
    NOW,
    intent,
    review_candidate_admission,
)
from tools.data_factory.operator.web.bridge import LoopbackBridge
from tools.data_factory.operator.workflow.intents import (
    CandidateReviewPort,
    OperatorIntentCore,
)
from tools.fr5_data_factory import ContractError, canonical_digest


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
            watch_timeout_s=0.02,
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

    def test_client_disconnect_during_response_is_quiet(self):
        handler = object.__new__(self.bridge._handler())
        handler._headers = lambda *_args: None

        class DisconnectedClient:
            def write(self, _payload):
                raise BrokenPipeError

        handler.wfile = DisconnectedClient()
        self.assertIsNone(handler._json(200, {"ok": True}))

    def test_startup_call_runs_once_after_serve_loop_begins(self):
        calls = []
        started = threading.Event()
        bridge = LoopbackBridge(
            core=self.core, ui_root=self.bridge.ui_root,
            host="127.0.0.1", port=0,
            token="second-fixed-test-token-long-enough",
        )

        def startup():
            calls.append("started")
            started.set()

        thread = threading.Thread(
            target=bridge.serve_forever, kwargs={"startup_call": startup},
        )
        thread.start()
        try:
            self.assertTrue(started.wait(timeout=2))
            bridge.server.service_actions()
            self.assertEqual(calls, ["started"])
        finally:
            bridge.close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

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

    def test_watch_reuses_snapshot_envelope_and_observes_external_heartbeat(self):
        headers = {"X-Operator-Token": self.bridge.token}
        initial = self.core.snapshot()
        observed = []
        started = threading.Event()

        def watch():
            started.set()
            observed.append(self.request(
                "GET", f"/api/view/watch?after_revision={initial['revision']}",
                headers=headers,
            ))

        thread = threading.Thread(target=watch)
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        self.core.transition(lambda: self.state.update(value=2))
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        status, _, body = observed[0]
        transitioned = json.loads(body)
        self.assertEqual((status, transitioned["revision"], transitioned["projection"]["value"]), (200, 1, 2))
        self.assertEqual(set(transitioned), set(initial))

        self.state["value"] = 3
        status, _, body = self.request(
            "GET", f"/api/view/watch?after_revision={transitioned['revision']}",
            headers=headers,
        )
        heartbeat = json.loads(body)
        self.assertEqual((status, heartbeat["revision"], heartbeat["projection"]["value"]), (200, 2, 3))

    def test_watch_query_and_future_revision_fail_closed(self):
        headers = {"X-Operator-Token": self.bridge.token}
        for path in (
            "/api/view/watch",
            "/api/view/watch?revision=0",
            "/api/view/watch?after_revision=-1",
            "/api/view/watch?after_revision=00",
            "/api/view/watch?after_revision=0&after_revision=0",
            "/api/view/watch?after_revision=0&extra=1",
        ):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path, headers=headers)
                self.assertEqual((status, json.loads(body)["code"]), (400, "BRIDGE_WATCH_QUERY"))
        status, _, body = self.request(
            "GET", "/api/view/watch?after_revision=1", headers=headers,
        )
        self.assertEqual(
            (status, json.loads(body)["code"]),
            (409, "OPERATOR_VIEW_REVISION_FUTURE"),
        )
        status, _, body = self.request("GET", "/api/view/watch?after_revision=0")
        self.assertEqual((status, json.loads(body)["code"]), (403, "BRIDGE_TOKEN"))

    def test_close_wakes_a_pending_watch_before_its_timeout(self):
        core = OperatorIntentCore(
            session_id="closing-watch-r001", projection_call=lambda: {"value": 1},
            handlers={"noop": lambda _payload, _view: {}}, clock=lambda: NOW,
        )
        initial = core.snapshot()
        entered = threading.Event()
        original_wait = core.wait_for_snapshot

        def observed_wait(*args, **kwargs):
            entered.set()
            return original_wait(*args, **kwargs)

        core.wait_for_snapshot = observed_wait
        bridge = LoopbackBridge(
            core=core, ui_root=self.bridge.ui_root, host="127.0.0.1", port=0,
            token="closing-watch-token-that-is-long-enough", watch_timeout_s=5,
        )
        server_thread = threading.Thread(target=bridge.serve_forever)
        server_thread.start()
        result = []

        def watch():
            connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
            connection.request(
                "GET", f"/api/view/watch?after_revision={initial['revision']}",
                headers={"X-Operator-Token": bridge.token},
            )
            response = connection.getresponse()
            result.append((response.status, json.loads(response.read())["code"]))
            connection.close()

        client_thread = threading.Thread(target=watch)
        client_thread.start()
        self.assertTrue(entered.wait(timeout=1))
        bridge.close()
        client_thread.join(timeout=1)
        server_thread.join(timeout=1)
        self.assertFalse(client_thread.is_alive())
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(result, [(503, "BRIDGE_CLOSED")])

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

    def test_candidate_pass_fail_and_uncertain_use_the_real_loopback_handler(self):
        for index, (choice, reason) in enumerate((
            ("PASS", None), ("FAIL", "TASK_GOAL"), ("UNCERTAIN", "UNKNOWN"),
        ), 1):
            with self.subTest(choice=choice):
                run_id = f"run-http-review-r00{index}"
                candidate_root = Path(self.directory.name) / f"candidate-{index}"
                candidate_root.mkdir()
                path = candidate_root / "candidate_admission.json"
                context_digest = canonical_digest(["http-review", choice])
                admission = {
                    "schema_version": "data_factory.candidate_admission.v1",
                    "run_id": run_id, "operational_gate": "PASS",
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
                    candidate_path=path, run_id=run_id,
                    expected_file_digest=canonical_digest(admission),
                    expected_review_context_digest=context_digest,
                )
                core = OperatorIntentCore(
                    session_id=f"review-http-session-r00{index}",
                    projection_call=lambda: {
                        "candidate_review": port.projection(),
                        "available_ops": ["review_candidate"],
                    },
                    handlers={"review_candidate": port.resolve}, clock=lambda: NOW,
                )
                bridge = LoopbackBridge(
                    core=core, ui_root=self.directory.name, host="127.0.0.1", port=0,
                    token=f"candidate-review-loopback-token-r00{index}",
                )
                thread = threading.Thread(target=bridge.serve_forever)
                thread.start()
                try:
                    headers = {"X-Operator-Token": bridge.token}
                    connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
                    connection.request("GET", "/api/view", headers=headers)
                    response = connection.getresponse()
                    snapshot = json.loads(response.read())
                    connection.close()
                    self.assertEqual(response.status, 200)

                    payload = intent(snapshot, "review_candidate", {
                        "review_binding_digest": projection["review_binding_digest"],
                        "choice": choice, "reason": reason,
                    }, f"review-http-intent-r00{index}")
                    connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
                    connection.request(
                        "POST", "/api/intent",
                        body=json.dumps(payload, separators=(",", ":")),
                        headers={
                            "Origin": bridge.origin,
                            "X-Operator-Token": bridge.token,
                            "Content-Type": "application/json",
                        },
                    )
                    response = connection.getresponse()
                    result = json.loads(response.read())
                    connection.close()
                    self.assertEqual((response.status, result["consumed"]), (200, True))
                    self.assertFalse(result["result"]["training_authorized"])
                    reviewed = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(
                        (reviewed["semantic_status"], reviewed["reviewed_by"], reviewed["reason"]),
                        (choice, "local-operator", reason),
                    )
                finally:
                    bridge.close()
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
