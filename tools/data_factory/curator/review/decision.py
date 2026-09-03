"""Exclusive foreground-TTY decision bound to exact review evidence."""
from __future__ import annotations
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
from typing import Any
from ..core.jsonio import SAFE_ID, CuratorError, canonical_digest, load_json, write_json_exclusive
from .manifest import verify_manifest

DECISION_SCHEMA = "curator.candidate_decision.v1"

def _read_controlling_tty(prompt: str) -> str:
    flags = os.O_RDWR | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open("/dev/tty", flags)
    except OSError as exc:
        raise CuratorError("HUMAN_TTY_REQUIRED", str(exc)) from exc
    try:
        if not stat.S_ISCHR(os.fstat(fd).st_mode) or not os.isatty(fd):
            raise CuratorError("HUMAN_TTY_REQUIRED")
        if os.tcgetpgrp(fd) != os.getpgrp():
            raise CuratorError("HUMAN_TTY_FOREGROUND")
        os.write(fd, prompt.encode())
        line = bytearray()
        while len(line) <= 32:
            byte = os.read(fd, 1)
            if not byte:
                raise CuratorError("HUMAN_TTY_EOF")
            if byte == b"\n":
                return line.rstrip(b"\r").decode("ascii")
            line.extend(byte)
        raise CuratorError("HUMAN_TTY_INPUT_TOO_LONG")
    finally:
        os.close(fd)

def issue_decision(run_dir: str | Path, decided_by: str, *, clock: Any = None) -> dict[str, Any]:
    if not isinstance(decided_by, str) or SAFE_ID.fullmatch(decided_by) is None:
        raise CuratorError("DECIDED_BY")
    run = Path(run_dir)
    ready = load_json(run / "review_ready.json", code="REVIEW_READY")
    manifest = verify_manifest(run / "review_manifest.json", run / "review.mp4")
    if ready.get("review_manifest_digest") != manifest["review_manifest_digest"]:
        raise CuratorError("REVIEW_READY_CHANGED")
    entered = _read_controlling_tty(f"Review {run / 'review.mp4'}\nType APPROVE or REJECT (no default): ")
    if entered not in {"APPROVE", "REJECT"}:
        raise CuratorError("HUMAN_DECISION_REQUIRED")
    if verify_manifest(run / "review_manifest.json", run / "review.mp4") != manifest:
        raise CuratorError("REVIEW_CHANGED_DURING_DECISION")
    now = clock() if clock else datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise CuratorError("DECISION_CLOCK")
    value = {
        "schema_version": DECISION_SCHEMA, "decision": entered, "decided_by": decided_by,
        "decided_at": now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "provenance": "HUMAN_CURATED_CANDIDATE_APPROVED" if entered == "APPROVE" else "HUMAN_CURATED_CANDIDATE_REJECTED",
        "review_manifest_digest": manifest["review_manifest_digest"],
        "candidate_tree_digest": ready["candidate_tree_digest"], "source_tree_digest": ready["source_tree_digest"],
        "profile_digest": ready["profile_digest"], "training_authorized": False,
    }
    value["decision_digest"] = canonical_digest(value)
    write_json_exclusive(run / "decision.json", value)
    return value

def verify_decision(run_dir: str | Path) -> dict[str, Any]:
    value = load_json(Path(run_dir) / "decision.json", code="DECISION_JSON")
    digest = value.pop("decision_digest", None)
    if digest != canonical_digest(value) or value.get("schema_version") != DECISION_SCHEMA:
        raise CuratorError("DECISION_CONTRACT")
    value["decision_digest"] = digest
    return value

__all__ = ["DECISION_SCHEMA", "issue_decision", "verify_decision"]
