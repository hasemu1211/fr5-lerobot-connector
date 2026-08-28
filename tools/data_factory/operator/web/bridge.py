"""Foreground loopback HTTP transport for operator views and intents."""
from __future__ import annotations

import json
import mimetypes
import secrets
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from tools.data_factory.operator.workflow.intents import (
    OperatorIntentCore,
    RESULT_SCHEMA,
)
from tools.fr5_data_factory import ContractError


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
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except UnicodeDecodeError as exc:
        raise ContractError("BRIDGE_JSON_ENCODING") from exc
    except json.JSONDecodeError as exc:
        raise ContractError("BRIDGE_JSON") from exc


class _ThreadingHTTPServer(ThreadingHTTPServer):
    startup_call = None

    def service_actions(self) -> None:
        if self.startup_call is not None:
            startup_call, self.startup_call = self.startup_call, None
            threading.Thread(target=startup_call, daemon=True).start()


class _IPv6ThreadingHTTPServer(_ThreadingHTTPServer):
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
        server_type = _IPv6ThreadingHTTPServer if host == "::1" else _ThreadingHTTPServer
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
                try:
                    self._headers(status, "application/json; charset=utf-8", len(payload))
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    # The browser may leave while a long snapshot is being built.
                    return

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

    def serve_forever(self, startup_call=None) -> None:
        self.server.startup_call = startup_call
        self.server.serve_forever(poll_interval=0.1)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
