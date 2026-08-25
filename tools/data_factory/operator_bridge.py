#!/usr/bin/env python3
"""Foreground, loopback-only operator intent bridge.

The bridge transports snapshots and bounded intents.  It never owns a campaign,
scene, cell, recorder, robot, approval, review, or training state machine.
"""
from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit

from tools.fr5_data_factory import ContractError, DIGEST, SAFE_ID, canonical_digest


VIEW_SCHEMA = "data_factory.operator_session_view.v1"
INTENT_SCHEMA = "data_factory.operator_intent.v1"
RESULT_SCHEMA = "data_factory.operator_intent_result.v1"
INTENT_FIELDS = frozenset({
    "schema_version", "intent_id", "session_id", "view_revision",
    "view_digest", "op", "payload",
})
FORBIDDEN_BROWSER_FIELDS = frozenset({
    "source", "approved_by", "reviewed_by", "reviewed_at", "scene_truth",
    "current_scene", "transition_target", "training_approved", "semantic_pass",
})
MAX_BODY_BYTES = 65_536


def _json_loads(payload: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError("BRIDGE_JSON_DUPLICATE_KEY")
            result[key] = value
        return result

    def constant(_value):
        raise ContractError("BRIDGE_JSON_NONFINITE")

    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except UnicodeDecodeError as exc:
        raise ContractError("BRIDGE_JSON_ENCODING") from exc
    except json.JSONDecodeError as exc:
        raise ContractError("BRIDGE_JSON") from exc


def _forbidden(value: object) -> bool:
    if isinstance(value, Mapping):
        return bool(FORBIDDEN_BROWSER_FIELDS & set(value)) or any(_forbidden(item) for item in value.values())
    if isinstance(value, list):
        return any(_forbidden(item) for item in value)
    return False


class OperatorIntentCore:
    """Atomic projection/CAS facade around existing owner callbacks."""

    def __init__(
        self, *, session_id: str, projection_call: Callable[[], Mapping[str, Any]],
        handlers: Mapping[str, Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any]]],
        clock=None,
    ):
        if not isinstance(session_id, str) or not SAFE_ID.fullmatch(session_id):
            raise ContractError("OPERATOR_SESSION_ID")
        if not callable(projection_call) or not isinstance(handlers, Mapping) or not handlers:
            raise ContractError("OPERATOR_CORE_CALLABLE")
        if any(not isinstance(name, str) or not SAFE_ID.fullmatch(name) or not callable(call) for name, call in handlers.items()):
            raise ContractError("OPERATOR_CORE_CALLABLE")
        self.session_id = session_id
        self.projection_call = projection_call
        self.handlers = dict(handlers)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._revision = 0
        self._consumed: set[str] = set()
        self._lock = threading.RLock()

    def _projection(self) -> dict[str, Any]:
        value = self.projection_call()
        if not isinstance(value, Mapping):
            raise ContractError("OPERATOR_VIEW_PROJECTION")
        return copy.deepcopy(dict(value))

    def _snapshot_locked(self) -> dict[str, Any]:
        generated = self.clock()
        if not isinstance(generated, datetime) or generated.tzinfo is None or generated.utcoffset() is None:
            raise ContractError("OPERATOR_VIEW_CLOCK")
        projection = self._projection()
        bound = {
            "session_id": self.session_id,
            "revision": self._revision,
            "projection": projection,
        }
        return {
            "schema_version": VIEW_SCHEMA,
            **bound,
            "generated_at": generated.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "view_digest": canonical_digest(bound),
            "authority": {
                "browser": "INTENT_ONLY",
                "lifecycle_owner": "BACKEND",
                "human_identity": "NOT_AUTHENTICATED",
                "training_approval": "SEPARATE",
            },
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def transition(self, change: Callable[[], None]) -> dict[str, Any]:
        """Publish an owner-side transition without letting the browser name it."""
        if not callable(change):
            raise ContractError("OPERATOR_CORE_CALLABLE")
        with self._lock:
            change()
            self._revision += 1
            return self._snapshot_locked()

    def consume(self, value: object) -> dict[str, Any]:
        with self._lock:
            if not isinstance(value, Mapping) or set(value) != INTENT_FIELDS or value.get("schema_version") != INTENT_SCHEMA:
                raise ContractError("OPERATOR_INTENT_FIELDS")
            intent = copy.deepcopy(dict(value))
            intent_id = intent["intent_id"]
            if not isinstance(intent_id, str) or not SAFE_ID.fullmatch(intent_id):
                raise ContractError("OPERATOR_INTENT_ID")
            if intent_id in self._consumed:
                raise ContractError("OPERATOR_INTENT_REPLAY")
            current = self._snapshot_locked()
            if (
                intent["session_id"] != self.session_id
                or type(intent["view_revision"]) is not int
                or intent["view_revision"] != current["revision"]
                or not isinstance(intent["view_digest"], str)
                or not DIGEST.fullmatch(intent["view_digest"])
                or intent["view_digest"] != current["view_digest"]
            ):
                raise ContractError("OPERATOR_INTENT_STALE_VIEW")
            op = intent["op"]
            if not isinstance(op, str) or op not in self.handlers:
                raise ContractError("OPERATOR_INTENT_OP")
            payload = intent["payload"]
            if not isinstance(payload, dict) or _forbidden(payload):
                raise ContractError("OPERATOR_INTENT_AUTHORITY")
            result = self.handlers[op](copy.deepcopy(payload), current)
            if not isinstance(result, Mapping):
                raise ContractError("OPERATOR_INTENT_RESULT")
            self._consumed.add(intent_id)
            self._revision += 1
            latest = self._snapshot_locked()
            return {
                "schema_version": RESULT_SCHEMA,
                "ok": True,
                "code": "INTENT_CONSUMED",
                "consumed": True,
                "intent_id": intent_id,
                "op": op,
                "result": copy.deepcopy(dict(result)),
                "current_view_revision": latest["revision"],
                "current_view_digest": latest["view_digest"],
            }


class ButtonDecisionPort:
    """One pending exact-plan button decision; it does not mint an approval."""

    def __init__(self, *, session_id: str, operator_label: str, clock=None):
        if not isinstance(operator_label, str) or not SAFE_ID.fullmatch(operator_label):
            raise ContractError("BUTTON_OPERATOR_LABEL")
        self.operator_label = operator_label
        self._pending = None
        self._decision = None
        self._condition = threading.Condition()
        self.core = OperatorIntentCore(
            session_id=session_id,
            projection_call=self._projection,
            handlers={
                "approve_exact_plan": self._approve,
                "reject_plan": self._reject,
                "cancel_plan": self._cancel,
            },
            clock=clock,
        )

    def _projection(self) -> dict[str, Any]:
        return {
            "decision_state": "IDLE" if self._pending is None else "AWAITING_BUTTON",
            "pending_plan": copy.deepcopy(self._pending),
            "decision_source": "LOCAL_UI_BUTTON",
            "operator_label": self.operator_label,
            "authenticated_human_identity": False,
        }

    def offer(
        self, *, run_id: str, plan_digest: str, decision_binding: Mapping[str, Any],
        approval_scope: str,
    ) -> dict[str, Any]:
        if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
            raise ContractError("BUTTON_PLAN_BINDING")
        if not isinstance(plan_digest, str) or not DIGEST.fullmatch(plan_digest):
            raise ContractError("BUTTON_PLAN_BINDING")
        if approval_scope not in {"HUMAN_GATED", "HIL_NUMERIC_PROXY"} or not isinstance(decision_binding, Mapping):
            raise ContractError("BUTTON_PLAN_BINDING")

        def change():
            with self._condition:
                if self._pending is not None or self._decision is not None:
                    raise ContractError("BUTTON_PLAN_ACTIVE")
                bound = {
                    "run_id": run_id,
                    "plan_digest": plan_digest,
                    "approval_scope": approval_scope,
                    "decision_binding": copy.deepcopy(dict(decision_binding)),
                }
                bound["decision_binding_digest"] = canonical_digest(bound)
                self._pending = bound

        return self.core.transition(change)

    def _consume_choice(self, choice: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._condition:
            if self._pending is None or self._decision is not None:
                raise ContractError("BUTTON_PLAN_STATE")
            if set(payload) != {"decision_binding_digest"} or payload["decision_binding_digest"] != self._pending["decision_binding_digest"]:
                raise ContractError("BUTTON_PLAN_DIGEST_MISMATCH")
            self._decision = {
                "choice": choice,
                "run_id": self._pending["run_id"],
                "plan_digest": self._pending["plan_digest"],
                "approval_scope": self._pending["approval_scope"],
                "decision_binding_digest": self._pending["decision_binding_digest"],
                "decision_source": "LOCAL_UI_BUTTON",
                "operator_label": self.operator_label,
            }
            self._pending = None
            self._condition.notify_all()
            return copy.deepcopy(self._decision)

    def _approve(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        return self._consume_choice("APPROVE", payload)

    def _reject(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        return self._consume_choice("REJECT", payload)

    def _cancel(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        return self._consume_choice("CANCEL", payload)

    def wait(self, timeout_s: float | None = None) -> dict[str, Any] | None:
        if timeout_s is not None and (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s < 0):
            raise ContractError("BUTTON_WAIT_TIMEOUT")
        deadline = None if timeout_s is None else time.monotonic() + float(timeout_s)
        with self._condition:
            while self._decision is None:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return copy.deepcopy(self._decision)

    def __call__(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        """Adapt the button core to the narrow run_live decision callback."""
        fields = {
            "schema_version", "run_id", "plan_digest", "approval_scope",
            "decision_binding", "timeout_s",
        }
        if (
            not isinstance(request, Mapping)
            or set(request) != fields
            or request.get("schema_version") != "data_factory.plan_decision_request.v1"
        ):
            raise ContractError("BUTTON_DECISION_REQUEST")
        self.offer(
            run_id=request["run_id"], plan_digest=request["plan_digest"],
            decision_binding=request["decision_binding"],
            approval_scope=request["approval_scope"],
        )
        return self.wait(request["timeout_s"])


class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class LoopbackBridge:
    """Serve one intent core and the static UI from a visible foreground process."""

    def __init__(
        self, *, core: OperatorIntentCore, ui_root: str | Path,
        host: str = "127.0.0.1", port: int = 0, token: str | None = None,
    ):
        if host not in {"127.0.0.1", "::1"}:
            raise ContractError("BRIDGE_LOOPBACK_REQUIRED")
        if type(port) is not int or not 0 <= port <= 65_535:
            raise ContractError("BRIDGE_PORT")
        self.core = core
        self.ui_root = Path(ui_root).resolve(strict=True)
        self.token = token or secrets.token_urlsafe(32)
        if not isinstance(self.token, str) or len(self.token) < 24:
            raise ContractError("BRIDGE_TOKEN")
        server_type = _IPv6ThreadingHTTPServer if host == "::1" else ThreadingHTTPServer
        self.server = server_type((host, port), self._handler())
        self.server.daemon_threads = False
        self.host = host
        self.port = self.server.server_address[1]
        displayed_host = f"[{host}]" if host == "::1" else host
        self.origin = f"http://{displayed_host}:{self.port}"
        self.allowed_hosts = {
            f"{displayed_host}:{self.port}",
            *({f"localhost:{self.port}"} if host == "127.0.0.1" else set()),
        }
        self.allowed_origins = {f"http://{item}" for item in self.allowed_hosts}

    def _handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "FR5OperatorBridge/1"

            def log_message(self, format, *args):
                # Never risk logging the anti-CSRF token or an intent body.
                return

            def _headers(self, status: int, content_type: str, length: int):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()

            def _json(self, status: int, value: Mapping[str, Any]):
                payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                self._headers(status, "application/json; charset=utf-8", len(payload))
                self.wfile.write(payload)

            def _error(self, status: int, code: str):
                self._json(status, {
                    "schema_version": RESULT_SCHEMA, "ok": False, "code": code,
                    "consumed": False,
                })

            def _host_ok(self) -> bool:
                return self.headers.get("Host") in bridge.allowed_hosts

            def do_GET(self):
                if not self._host_ok():
                    return self._error(HTTPStatus.BAD_REQUEST, "BRIDGE_HOST")
                path = urlsplit(self.path).path
                if path == "/api/view":
                    if self.headers.get("X-Operator-Token") != bridge.token:
                        return self._error(HTTPStatus.FORBIDDEN, "BRIDGE_TOKEN")
                    try:
                        return self._json(HTTPStatus.OK, bridge.core.snapshot())
                    except ContractError as exc:
                        return self._error(HTTPStatus.CONFLICT, exc.code)
                relative = "index.html" if path == "/" else unquote(path.lstrip("/"))
                if not relative or ".." in Path(relative).parts:
                    return self._error(HTTPStatus.NOT_FOUND, "BRIDGE_STATIC_PATH")
                candidate = bridge.ui_root / relative
                if candidate.is_symlink():
                    return self._error(HTTPStatus.NOT_FOUND, "BRIDGE_STATIC_PATH")
                target = candidate.resolve(strict=False)
                try:
                    target.relative_to(bridge.ui_root)
                except ValueError:
                    return self._error(HTTPStatus.NOT_FOUND, "BRIDGE_STATIC_PATH")
                if not target.is_file() or target.is_symlink():
                    return self._error(HTTPStatus.NOT_FOUND, "BRIDGE_STATIC_PATH")
                payload = target.read_bytes()
                if relative == "index.html":
                    marker = b"<!-- OPERATOR_TOKEN -->"
                    injection = (
                        '<meta name="operator-token" content="' + bridge.token + '">'
                    ).encode()
                    if marker not in payload:
                        return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "BRIDGE_TOKEN_MARKER")
                    payload = payload.replace(marker, injection, 1)
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                    content_type += "; charset=utf-8"
                self._headers(HTTPStatus.OK, content_type, len(payload))
                self.wfile.write(payload)

            def do_POST(self):
                if not self._host_ok():
                    return self._error(HTTPStatus.BAD_REQUEST, "BRIDGE_HOST")
                if urlsplit(self.path).path != "/api/intent":
                    return self._error(HTTPStatus.NOT_FOUND, "BRIDGE_ROUTE")
                if self.headers.get("Origin") not in bridge.allowed_origins:
                    return self._error(HTTPStatus.FORBIDDEN, "BRIDGE_ORIGIN")
                if self.headers.get("X-Operator-Token") != bridge.token:
                    return self._error(HTTPStatus.FORBIDDEN, "BRIDGE_TOKEN")
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "BRIDGE_CONTENT_TYPE")
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    return self._error(HTTPStatus.LENGTH_REQUIRED, "BRIDGE_CONTENT_LENGTH")
                if not 0 < length <= MAX_BODY_BYTES:
                    return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "BRIDGE_BODY_SIZE")
                try:
                    intent = _json_loads(self.rfile.read(length))
                    result = bridge.core.consume(intent)
                except ContractError as exc:
                    return self._error(HTTPStatus.CONFLICT, exc.code)
                return self._json(HTTPStatus.OK, result)

        return Handler

    def serve_forever(self) -> None:
        self.server.serve_forever(poll_interval=0.1)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Serve the FR5 operator UI over a foreground loopback bridge")
    parser.add_argument("--host", choices=("127.0.0.1", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4174)
    parser.add_argument("--ui-root", default=str(Path(__file__).resolve().parents[2] / "operator-ui"))
    args = parser.parse_args(argv)
    state = {"status": "FIXTURE_ONLY", "hardware_calls": 0, "next_action": "USE_FAKE_MODE"}
    core = OperatorIntentCore(
        session_id="fixture-session",
        projection_call=lambda: state,
        handlers={"refresh_fixture": lambda payload, view: {"refreshed": payload == {}}},
    )
    bridge = None
    try:
        bridge = LoopbackBridge(core=core, ui_root=args.ui_root, host=args.host, port=args.port)
        print(json.dumps({
            "status": "LISTENING", "url": bridge.origin,
            "effect_scope": "FAKE", "hardware_calls": 0,
        }, sort_keys=True), flush=True)
        bridge.serve_forever()
    except (ContractError, OSError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            return 130
        code = exc.code if isinstance(exc, ContractError) else "BRIDGE_FAILED"
        print(json.dumps({"error": {"code": code, "message": str(exc)}}, sort_keys=True), flush=True)
        return 2
    finally:
        if bridge is not None:
            bridge.server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
