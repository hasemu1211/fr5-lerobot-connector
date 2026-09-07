from __future__ import annotations

import io
import json
import socket
import subprocess
import tempfile
import threading
import unittest
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .fixtures import NOW, intent
from tools.data_factory.operator.web import client
from tools.data_factory.operator.web.bridge import LoopbackBridge
from tools.data_factory.operator.workflow.intents import OperatorIntentCore
from tools.fr5_data_factory import ContractError


TOKEN = "client-test-bootstrap-token-that-stays-secret"


class _Boundary:
    def __init__(self, *, root_redirect=False, post_mode="success", view_body=None,
                 token=TOKEN, post_body=None):
        boundary = self
        self.root_redirect = root_redirect
        self.post_mode = post_mode
        self.view_body = view_body or {"kind": "view", "read_only": True}
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def _write(self, status, body=b"", **headers):
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name.replace("_", "-"), value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                boundary.requests.append(("GET", self.path, b"", dict(self.headers)))
                if self.path == "/" and boundary.root_redirect:
                    return self._write(302, Location="http://example.invalid/token")
                if self.path == "/":
                    page = (
                        '<html><head><meta name="operator-token" content="'
                        + token + '"></head></html>'
                    ).encode()
                    return self._write(200, page, Content_Type="text/html; charset=utf-8")
                if self.path == "/api/view":
                    body = boundary.view_body
                    if isinstance(body, dict):
                        body = json.dumps(body).encode()
                    return self._write(200, body, Content_Type="application/json")
                return self._write(500, b"unexpected redirect follow")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                boundary.requests.append(("POST", self.path, body, dict(self.headers)))
                if boundary.post_mode == "disconnect":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if boundary.post_mode == "redirect":
                    return self._write(307, Location="http://example.invalid/intent")
                result = {
                    "schema_version": "data_factory.operator_intent_result.v1",
                    "ok": True, "code": "INTENT_CONSUMED", "consumed": True,
                }
                if post_body is not None:
                    result = post_body
                return self._write(
                    200, json.dumps(result).encode(),
                    Content_Type="application/json",
                )

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("fake boundary did not stop")


class OperatorWebClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        (root / "index.html").write_text(
            "<!doctype html><head><!-- OPERATOR_TOKEN --></head>", encoding="utf-8",
        )
        self.effects = {
            "value": 1, "hardware_calls": 0, "recorder_calls": 0,
            "dataset_writes": 0,
        }
        self.received_payloads = []

        def update(payload, _view):
            if set(payload) != {"value"} or type(payload["value"]) is not int:
                raise ContractError("UPDATE_FIELDS")
            self.received_payloads.append(payload)
            self.effects["value"] = payload["value"]
            return {"value": payload["value"]}

        self.core = OperatorIntentCore(
            session_id="client-http-session-r001",
            projection_call=lambda: self.effects,
            handlers={"update_fixture": update},
            clock=lambda: NOW,
        )
        self.bridge = LoopbackBridge(
            core=self.core, ui_root=root, host="127.0.0.1", port=0, token=TOKEN,
        )
        self.thread = threading.Thread(target=self.bridge.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.bridge.close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.directory.cleanup()

    @property
    def endpoint(self):
        return self.bridge.origin

    def run_client(self, *argv, body=b""):
        output = io.StringIO()
        exit_code = client.main(
            ["--endpoint", self.endpoint, *argv],
            stdin=io.BytesIO(body), stdout=output,
        )
        rendered = output.getvalue()
        self.assertNotIn(TOKEN, rendered)
        return exit_code, json.loads(rendered)

    def test_real_fake_loopback_view_and_exact_submit_succeed(self):
        before = dict(self.effects)
        exit_code, view = self.run_client("view")
        self.assertEqual(exit_code, 0)
        self.assertEqual(view["projection"], before)
        self.assertEqual(self.effects, before)

        envelope = intent(
            view, "update_fixture", {"value": 7}, "client-exact-intent-r001",
        )
        body = json.dumps(envelope, indent=2).encode()
        exit_code, result = self.run_client("submit", body=body)
        self.assertEqual(
            (exit_code, result["ok"], result["intent_id"], result["op"]),
            (0, True, envelope["intent_id"], envelope["op"]),
        )
        self.assertEqual(self.received_payloads, [envelope["payload"]])
        self.assertEqual(
            self.effects,
            {"value": 7, "hardware_calls": 0, "recorder_calls": 0, "dataset_writes": 0},
        )

    def test_backend_replay_stale_and_session_rejections_are_nonzero(self):
        initial = self.core.snapshot()
        accepted = intent(initial, "update_fixture", {"value": 2}, "client-replay-r001")
        accepted_body = json.dumps(accepted).encode()
        self.assertEqual(self.run_client("submit", body=accepted_body)[0], 0)

        cases = (
            (accepted, "OPERATOR_INTENT_REPLAY"),
            (
                intent(initial, "update_fixture", {"value": 3}, "client-stale-r002"),
                "OPERATOR_INTENT_STALE_VIEW",
            ),
            (
                {
                    **intent(
                        self.core.snapshot(), "update_fixture", {"value": 4},
                        "client-session-r003",
                    ),
                    "session_id": "different-session-r001",
                },
                "OPERATOR_INTENT_STALE_VIEW",
            ),
        )
        for envelope, expected_code in cases:
            with self.subTest(expected_code=expected_code, intent_id=envelope["intent_id"]):
                exit_code, result = self.run_client(
                    "submit", body=json.dumps(envelope).encode(),
                )
                self.assertEqual((exit_code, result["ok"], result["code"]), (4, False, expected_code))
        self.assertEqual(self.received_payloads, [{"value": 2}])
        self.assertEqual(
            {key: self.effects[key] for key in ("hardware_calls", "recorder_calls", "dataset_writes")},
            {"hardware_calls": 0, "recorder_calls": 0, "dataset_writes": 0},
        )

    def test_submit_file_sends_original_bytes_once_without_view_refresh(self):
        raw = b'{\n "payload":{"nested":[3,2,1]}, "op":"caller_owned"\n}'
        with _Boundary() as boundary:
            path = Path(self.directory.name) / "intent.json"
            path.write_bytes(raw)
            output = io.StringIO()
            exit_code = client.main(
                ["--endpoint", boundary.origin, "submit", str(path)], stdout=output,
            )
        self.assertEqual(exit_code, 0)
        self.assertNotIn(TOKEN, output.getvalue())
        posts = [item for item in boundary.requests if item[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual((posts[0][1], posts[0][2]), ("/api/intent", raw))
        self.assertEqual(posts[0][3]["X-Operator-Token"], TOKEN)
        self.assertEqual(posts[0][3]["Origin"], boundary.origin)
        self.assertFalse(any(path == "/api/view" for _, path, _, _ in boundary.requests))

    def test_submit_disconnect_is_one_send_with_explicit_ambiguity(self):
        with _Boundary(post_mode="disconnect") as boundary:
            output = io.StringIO()
            exit_code = client.main(
                ["--endpoint", boundary.origin, "submit"],
                stdin=io.BytesIO(b'{"caller":"supplied"}'), stdout=output,
            )
        value = json.loads(output.getvalue())
        self.assertEqual((exit_code, value["error"]["code"]), (3, "CLIENT_TRANSPORT_AMBIGUOUS"))
        self.assertIn("not retried", value["error"]["message"])
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertEqual(sum(method == "POST" for method, *_ in boundary.requests), 1)
        self.assertFalse(any(path == "/api/view" for _, path, _, _ in boundary.requests))

    def test_non_loopback_and_malformed_endpoints_fail_before_transport(self):
        invalid = (
            "https://127.0.0.1:1", "http://0.0.0.0:1", "http://localhost:1",
            "http://127.0.0.1:1/path", "http://user@127.0.0.1:1",
            "http://127.0.0.2:1", "http://[::2]:1",
        )
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                output = io.StringIO()
                exit_code = client.main(["--endpoint", endpoint, "view"], stdout=output)
                value = json.loads(output.getvalue())
                self.assertEqual((exit_code, value["error"]["code"]), (2, "CLIENT_ENDPOINT"))
                self.assertNotIn(TOKEN, output.getvalue())

    def test_bootstrap_and_submit_redirects_are_never_followed(self):
        with _Boundary(root_redirect=True) as boundary:
            output = io.StringIO()
            exit_code = client.main(["--endpoint", boundary.origin, "view"], stdout=output)
        self.assertEqual((exit_code, json.loads(output.getvalue())["error"]["code"]), (3, "CLIENT_REDIRECT"))
        self.assertEqual([(item[0], item[1]) for item in boundary.requests], [("GET", "/")])

        with _Boundary(post_mode="redirect") as boundary:
            output = io.StringIO()
            exit_code = client.main(
                ["--endpoint", boundary.origin, "submit"],
                stdin=io.BytesIO(b'{"exact":"envelope"}'), stdout=output,
            )
        self.assertEqual((exit_code, json.loads(output.getvalue())["error"]["code"]), (3, "CLIENT_REDIRECT"))
        self.assertEqual(sum(method == "POST" for method, *_ in boundary.requests), 1)
        self.assertEqual(len(boundary.requests), 2)

    def test_oversized_backend_response_is_replaced_by_bounded_error(self):
        with _Boundary(view_body=b"{" + b"x" * client.MAX_RESPONSE_BYTES + b"}") as boundary:
            output = io.StringIO()
            exit_code = client.main(["--endpoint", boundary.origin, "view"], stdout=output)
        self.assertEqual((exit_code, json.loads(output.getvalue())["error"]["code"]), (3, "CLIENT_RESPONSE_SIZE"))
        self.assertLess(len(output.getvalue()), 256)
        self.assertNotIn(TOKEN, output.getvalue())

    def test_malformed_json_responses_are_bounded_errors(self):
        for body in (
            b'{"ok":false,"ok":true}', b'{"nested":{"x":1,"x":2}}',
            b'{"value":1e999}', b'{"value":NaN}', b'\xff',
        ):
            with self.subTest(body=body), _Boundary(view_body=body) as boundary:
                output = io.StringIO()
                code = client.main(["--endpoint", boundary.origin, "view"], stdout=output)
                self.assertEqual((code, json.loads(output.getvalue())["error"]["code"]),
                                 (3, "CLIENT_RESPONSE_JSON"))
                self.assertNotIn(TOKEN, output.getvalue())

    def test_header_unsafe_bootstrap_tokens_never_reach_api(self):
        for token in ("short", TOKEN + "\u0100", TOKEN + "\r\n", TOKEN + "\x7f"):
            with self.subTest(token=repr(token)), _Boundary(token=token) as boundary:
                output = io.StringIO()
                code = client.main(["--endpoint", boundary.origin, "view"], stdout=output)
                self.assertEqual((code, json.loads(output.getvalue())["error"]["code"]),
                                 (3, "CLIENT_BOOTSTRAP"))
                self.assertEqual(len(boundary.requests), 1)
                self.assertNotIn(token, output.getvalue())

    def test_invalid_submit_receipt_is_unknown_not_success_and_never_retried(self):
        valid = {"schema_version": client.RESULT_SCHEMA, "ok": True, "consumed": True}
        for result in (
            {**valid, "schema_version": "wrong"}, {**valid, "consumed": False},
            {"schema_version": client.RESULT_SCHEMA, "ok": True},
            {**valid, "ok": 1, "consumed": 1},
        ):
            with self.subTest(result=result), _Boundary(post_body=result) as boundary:
                output = io.StringIO()
                code = client.main(
                    ["--endpoint", boundary.origin, "submit"],
                    stdin=io.BytesIO(b'{"caller":"supplied"}'), stdout=output,
                )
                error = json.loads(output.getvalue())["error"]
                self.assertEqual((code, error["code"]), (3, "CLIENT_INTENT_RESULT"))
                self.assertIn("unknown", error["message"])
                self.assertIn("not retried", error["message"])
                self.assertEqual(sum(item[0] == "POST" for item in boundary.requests), 1)
                self.assertEqual(len(boundary.requests), 2)
                self.assertNotIn(TOKEN, output.getvalue())


class CollectionBrowserRecoveryTest(unittest.TestCase):
    @unittest.skipUnless(Path("/opt/google/chrome/chrome").is_file(), "local Chrome is not installed")
    def test_shipped_collection_browser_recovery_and_existing_journeys(self):
        ui = Path(__file__).resolve().parents[3] / "operator-ui"

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == "/__test__/interrupted-body":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                    self.wfile.write(b'{"schema_version":')
                    self.wfile.flush()
                    self.close_connection = True
                    return
                super().do_GET()

        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ui)))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            for query, minimum in (("?only=motion-preset", 7), ("?only=body-recovery", 21), ("?only=state-recovery", 33), ("", 126)):
                with self.subTest(query=query), tempfile.TemporaryDirectory() as profile:
                    replay = subprocess.run([
                        "/opt/google/chrome/chrome", "--headless=new", "--disable-gpu",
                        "--no-first-run", "--disable-background-networking", "--no-default-browser-check",
                        f"--user-data-dir={profile}", "--dump-dom", "--virtual-time-budget=30000",
                        f"http://127.0.0.1:{server.server_port}/tests/browser-regression.html{query}",
                    ], capture_output=True, text=True, timeout=40)
                    self.assertEqual(replay.returncode, 0, replay.stderr)
                    self.assertIn('data-result="pass"', replay.stdout, replay.stdout[-8000:])
                    result = replay.stdout.split('<pre id="results">', 1)[1].split("</pre>", 1)[0]
                    self.assertGreaterEqual(int(result.split(" checks passed", 1)[0]), minimum)
                    print(result)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
