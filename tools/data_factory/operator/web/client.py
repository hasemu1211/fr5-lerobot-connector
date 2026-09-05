"""Small command-line client for the foreground operator loopback bridge."""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import BinaryIO, TextIO
from urllib.parse import urlsplit

from tools.data_factory.operator.web.bridge import MAX_BODY_BYTES, _json_loads
from tools.data_factory.operator.workflow.intents import RESULT_SCHEMA
from tools.fr5_data_factory import ContractError


DEFAULT_ENDPOINT = "http://127.0.0.1:4174"
MAX_RESPONSE_BYTES = 1_048_576
TIMEOUT_SECONDS = 5

EXIT_USAGE = 2
EXIT_TRANSPORT = 3
EXIT_REJECTED = 4


class ClientError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_TRANSPORT):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    authority: str
    origin: str


class _TokenParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "meta":
            return
        values = {str(key).lower(): value for key, value in attrs}
        if str(values.get("name", "")).lower() == "operator-token":
            content = values.get("content")
            if isinstance(content, str):
                self.tokens.append(content)


def _endpoint(raw: str) -> Endpoint:
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ClientError(
            "CLIENT_ENDPOINT", "endpoint must be an explicit loopback HTTP origin",
            EXIT_USAGE,
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ClientError(
            "CLIENT_ENDPOINT", "endpoint must be an explicit loopback HTTP origin",
            EXIT_USAGE,
        )
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise ClientError(
            "CLIENT_ENDPOINT", "endpoint must use a loopback IP literal",
            EXIT_USAGE,
        ) from exc
    if not address.is_loopback or str(address) not in {"127.0.0.1", "::1"}:
        raise ClientError(
            "CLIENT_ENDPOINT", "endpoint must use 127.0.0.1 or ::1",
            EXIT_USAGE,
        )
    port = 80 if port is None else port
    if not 1 <= port <= 65_535:
        raise ClientError("CLIENT_ENDPOINT", "endpoint port is invalid", EXIT_USAGE)
    displayed_host = f"[{address}]" if address.version == 6 else str(address)
    authority = f"{displayed_host}:{port}"
    return Endpoint(str(address), port, authority, f"http://{authority}")


def _request(
    endpoint: Endpoint, method: str, path: str, *, headers=None, body: bytes | None = None,
    ambiguous_on_failure: bool = False,
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection(
        endpoint.host, endpoint.port, timeout=TIMEOUT_SECONDS,
    )
    request_started = False
    try:
        request_started = True
        connection.request(
            method, path, body=body,
            headers={"Host": endpoint.authority, **(headers or {})},
        )
        response = connection.getresponse()
        if 300 <= response.status < 400:
            response.read(MAX_RESPONSE_BYTES + 1)
            raise ClientError("CLIENT_REDIRECT", "redirect responses are refused")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ClientError("CLIENT_RESPONSE_SIZE", "response exceeds the client limit")
        return response.status, payload
    except ClientError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        if ambiguous_on_failure and request_started:
            raise ClientError(
                "CLIENT_TRANSPORT_AMBIGUOUS",
                "intent submission was attempted once; its outcome is unknown and it was not retried",
            ) from exc
        raise ClientError("CLIENT_TRANSPORT", "loopback request failed") from exc
    finally:
        connection.close()


def _bootstrap_token(endpoint: Endpoint) -> str:
    status, payload = _request(endpoint, "GET", "/", headers={"Accept": "text/html"})
    if status != 200:
        raise ClientError("CLIENT_BOOTSTRAP", "operator page rejected token bootstrap")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClientError("CLIENT_BOOTSTRAP", "operator page is not valid UTF-8") from exc
    parser = _TokenParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise ClientError("CLIENT_BOOTSTRAP", "operator page token is invalid") from exc
    if len(parser.tokens) != 1:
        raise ClientError("CLIENT_BOOTSTRAP", "operator page must contain one token")
    token = parser.tokens[0]
    if not 24 <= len(token) <= 4096 or any(not 33 <= ord(character) <= 126 for character in token):
        raise ClientError("CLIENT_BOOTSTRAP", "operator page token is invalid")
    return token


def _json_response(payload: bytes) -> dict:
    try:
        value = _json_loads(payload)
    except (ContractError, ValueError, RecursionError) as exc:
        raise ClientError("CLIENT_RESPONSE_JSON", "backend response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ClientError("CLIENT_RESPONSE_JSON", "backend response must be a JSON object")
    return value


def _read_intent(path: str, stdin: BinaryIO | TextIO) -> bytes:
    try:
        if path == "-":
            payload = stdin.read(MAX_BODY_BYTES + 1)
        else:
            with Path(path).open("rb") as source:
                payload = source.read(MAX_BODY_BYTES + 1)
    except OSError as exc:
        raise ClientError("CLIENT_INTENT_READ", "intent input could not be read", EXIT_USAGE) from exc
    if isinstance(payload, str):
        try:
            payload = payload.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ClientError("CLIENT_INTENT_ENCODING", "intent input is not UTF-8", EXIT_USAGE) from exc
    if not payload or len(payload) > MAX_BODY_BYTES:
        raise ClientError(
            "CLIENT_INTENT_SIZE", f"intent input must be 1..{MAX_BODY_BYTES} bytes",
            EXIT_USAGE,
        )
    return payload


def _emit(stream: TextIO, value: dict) -> None:
    try:
        rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ClientError("CLIENT_RESPONSE_JSON", "backend response is not finite JSON") from exc
    if len(rendered.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ClientError("CLIENT_RESPONSE_SIZE", "response exceeds the client limit")
    stream.write(rendered + "\n")


def _error(error: ClientError) -> dict:
    return {"error": {"code": error.code, "message": error.message}}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="operator loopback HTTP origin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("view", help="print the current read-only operator view")
    submit = commands.add_parser("submit", help="submit exactly one supplied intent envelope")
    submit.add_argument("intent", nargs="?", default="-", help="JSON file, or - for stdin")
    return parser


def main(argv=None, *, stdin=None, stdout=None) -> int:
    output = stdout or sys.stdout
    source = stdin or sys.stdin.buffer
    try:
        args = _parser().parse_args(argv)
        endpoint = _endpoint(args.endpoint)
        intent_body = _read_intent(args.intent, source) if args.command == "submit" else None
        token = _bootstrap_token(endpoint)
        headers = {"Accept": "application/json", "X-Operator-Token": token}
        if args.command == "view":
            status, payload = _request(endpoint, "GET", "/api/view", headers=headers)
        else:
            status, payload = _request(
                endpoint, "POST", "/api/intent", body=intent_body,
                headers={
                    **headers, "Content-Type": "application/json",
                    "Origin": endpoint.origin,
                },
                ambiguous_on_failure=True,
            )
        value = _json_response(payload)
        if args.command == "submit" and (
            value.get("schema_version") != RESULT_SCHEMA
            or type(value.get("ok")) is not bool
            or type(value.get("consumed")) is not bool
            or value["ok"] != value["consumed"]
        ):
            raise ClientError(
                "CLIENT_INTENT_RESULT",
                "invalid intent receipt; outcome is unknown and the request was not retried",
            )
        _emit(output, value)
        if status != 200 or args.command == "submit" and value.get("ok") is not True:
            return EXIT_REJECTED
        return 0
    except ClientError as exc:
        _emit(output, _error(exc))
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
