"""The curator's sole authority artifact: exact controlling-TTY human approval."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
from typing import Any

from tools.data_factory.curator.contracts import (
    DIGEST,
    RFC3339_UTC,
    SAFE_ID,
    CuratorError,
    canonical_digest,
    exact_fields,
    load_json,
    reject_symlink_components,
    write_json_exclusive,
)
from tools.data_factory.curator.verify import verify_review_bundle


APPROVAL_SCHEMA = "curator.human_task_view_approval.v2"
PROVENANCE = "HUMAN_TASK_VIEW_APPROVED"
ISSUANCE_PATH = "FOREGROUND_CONTROLLING_/dev/tty"
IDENTITY_ASSURANCE = "LOCAL_TTY_PRESENCE_NOT_CRYPTOGRAPHIC_IDENTITY"
_APPROVAL_FIELDS = {
    "schema_version",
    "scope",
    "profile_id",
    "profile_digest",
    "review_bundle_digest",
    "approved_by",
    "approved_at",
    "provenance",
    "issuance_path",
    "identity_assurance",
    "training_authorized",
    "approval_digest",
}
_AUTOMATED_IDENTITIES = {
    "agent", "ai", "automation", "claude", "codex", "fixture", "gemini",
    "gpt", "model", "robot", "test",
}


def _human_id(value: object) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise CuratorError("APPROVED_BY")
    tokens = {token for token in re.split(r"[._-]+", value.casefold()) if token}
    if tokens & _AUTOMATED_IDENTITIES:
        raise CuratorError("APPROVED_BY_AUTOMATED", value)
    return value


def approval_phrase(profile_digest: str, review_bundle_digest: str) -> str:
    return f"APPROVE {PROVENANCE} {profile_digest} {review_bundle_digest}"


def _read_controlling_tty(prompt: str) -> str:
    """Read only the foreground process's exact controlling /dev/tty."""
    flags = os.O_RDWR | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open("/dev/tty", flags)
    except OSError as exc:
        raise CuratorError("HUMAN_TTY_REQUIRED", str(exc)) from exc
    try:
        details = os.fstat(fd)
        if not stat.S_ISCHR(details.st_mode) or not os.isatty(fd):
            raise CuratorError("HUMAN_TTY_REQUIRED")
        try:
            foreground = os.tcgetpgrp(fd)
        except OSError as exc:
            raise CuratorError("HUMAN_TTY_REQUIRED", str(exc)) from exc
        if foreground != os.getpgrp():
            raise CuratorError("HUMAN_TTY_FOREGROUND")
        os.write(fd, prompt.encode())
        line = bytearray()
        while len(line) <= 4096:
            chunk = os.read(fd, 1)
            if not chunk:
                raise CuratorError("HUMAN_TTY_EOF")
            if chunk == b"\n":
                break
            line.extend(chunk)
        else:
            raise CuratorError("HUMAN_TTY_INPUT_TOO_LONG")
        try:
            return bytes(line).rstrip(b"\r").decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise CuratorError("HUMAN_TTY_UTF8") from exc
    finally:
        os.close(fd)


def issue_approval(
    profile_request: str | Path,
    approved_by: str,
    *,
    clock: Any = None,
) -> dict[str, Any]:
    """Exclusive-create approval after exact foreground /dev/tty confirmation."""
    human = _human_id(approved_by)
    request, profile, manifest = verify_review_bundle(profile_request)
    if profile["physical_binding_status"] != "VERIFIED":
        raise CuratorError("PHYSICAL_BINDING_NOT_VERIFIED")
    if request.approval_path.exists() or request.approval_path.is_symlink():
        raise CuratorError("APPROVAL_EXISTS", str(request.approval_path))
    phrase = approval_phrase(profile["profile_digest"], manifest["review_bundle_digest"])
    entered = _read_controlling_tty(
        "\nReview every full-resolution file in "
        f"{request.review_bundle_path}\nprofile_digest={profile['profile_digest']}\n"
        f"review_bundle_digest={manifest['review_bundle_digest']}\n"
        f"Type exactly:\n{phrase}\n> "
    )
    if entered != phrase:
        raise CuratorError("HUMAN_APPROVAL_PHRASE")
    # Close the review/typing race before issuing the exclusive artifact.
    current_request, current_profile, current_manifest = verify_review_bundle(profile_request)
    if (
        current_request.approval_path != request.approval_path
        or current_profile["profile_digest"] != profile["profile_digest"]
        or current_manifest["review_bundle_digest"] != manifest["review_bundle_digest"]
    ):
        raise CuratorError("APPROVAL_REVIEW_CHANGED")
    now = clock() if clock is not None else datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise CuratorError("APPROVAL_CLOCK")
    approval = {
        "schema_version": APPROVAL_SCHEMA,
        "scope": "HUMAN_TASK_VIEW",
        "profile_id": profile["profile_id"],
        "profile_digest": profile["profile_digest"],
        "review_bundle_digest": manifest["review_bundle_digest"],
        "approved_by": human,
        "approved_at": now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "provenance": PROVENANCE,
        "issuance_path": ISSUANCE_PATH,
        "identity_assurance": IDENTITY_ASSURANCE,
        "training_authorized": False,
    }
    approval["approval_digest"] = canonical_digest(approval)
    write_json_exclusive(request.approval_path, approval)
    return approval


def verify_approval(
    profile_request: str | Path,
    approval_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    request, profile, manifest = verify_review_bundle(profile_request)
    if approval_path is None:
        path = request.approval_path
    else:
        reject_symlink_components(approval_path, "APPROVAL_PATH")
        try:
            path = Path(approval_path).resolve(strict=True)
        except OSError as exc:
            raise CuratorError("APPROVAL_PATH", str(exc)) from exc
    if path != request.approval_path:
        raise CuratorError("APPROVAL_PATH_MISMATCH")
    approval = exact_fields(load_json(path, code="APPROVAL_JSON"), _APPROVAL_FIELDS, "APPROVAL_FIELDS")
    if (
        approval["schema_version"] != APPROVAL_SCHEMA
        or approval["scope"] != "HUMAN_TASK_VIEW"
        or approval["profile_id"] != profile["profile_id"]
        or approval["profile_digest"] != profile["profile_digest"]
        or approval["review_bundle_digest"] != manifest["review_bundle_digest"]
        or _human_id(approval["approved_by"]) != approval["approved_by"]
        or not isinstance(approval["approved_at"], str)
        or RFC3339_UTC.fullmatch(approval["approved_at"]) is None
        or approval["provenance"] != PROVENANCE
        or approval["issuance_path"] != ISSUANCE_PATH
        or approval["identity_assurance"] != IDENTITY_ASSURANCE
        or approval["training_authorized"] is not False
        or not isinstance(approval["approval_digest"], str)
        or DIGEST.fullmatch(approval["approval_digest"]) is None
        or approval["approval_digest"] != canonical_digest({
            key: item for key, item in approval.items() if key != "approval_digest"
        })
        or profile["physical_binding_status"] != "VERIFIED"
    ):
        raise CuratorError("APPROVAL_CONTRACT")
    return approval, profile, manifest


__all__ = [
    "APPROVAL_SCHEMA",
    "PROVENANCE",
    "IDENTITY_ASSURANCE",
    "ISSUANCE_PATH",
    "approval_phrase",
    "issue_approval",
    "verify_approval",
]
