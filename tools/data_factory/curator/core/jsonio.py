"""Strict canonical JSON primitives."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any

from .errors import CuratorError

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
RFC3339_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")


def canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise CuratorError("JSON_NONFINITE", str(exc)) from exc


def canonical_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise CuratorError("JSON_DUPLICATE_KEY", key)
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise CuratorError("JSON_NONFINITE", value)


def load_json(path: str | Path, *, code: str) -> dict[str, Any]:
    source = Path(path)
    descriptor = -1
    try:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CuratorError(code, f"regular JSON file required: {source}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = json.load(
                stream, object_pairs_hook=_pairs, parse_constant=_nonfinite
            )
    except CuratorError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CuratorError(code, str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise CuratorError(code, "JSON object required")
    return value


def exact_fields(value: object, fields: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CuratorError(
            code,
            f"expected={sorted(fields)} actual={sorted(value) if isinstance(value, dict) else type(value).__name__}",
        )
    return value


def finite_number(value: object, code: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise CuratorError(code, "finite number required")
    return float(value)


__all__ = [
    "DIGEST",
    "RFC3339_UTC",
    "SAFE_ID",
    "canonical_bytes",
    "canonical_digest",
    "exact_fields",
    "finite_number",
    "load_json",
]
