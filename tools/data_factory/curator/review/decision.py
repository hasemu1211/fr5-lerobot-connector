"""Foreground controlling-TTY candidate choice; workflow records authority."""

from __future__ import annotations

import os
import stat

from ..core.errors import CuratorError


def read_foreground_decision(review_path: str) -> str:
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
        os.write(fd, f"Review {review_path}\n".encode())
        while True:
            os.write(fd, b"Type APPROVE or REJECT (no default): ")
            line = bytearray()
            while len(line) <= 32:
                byte = os.read(fd, 1)
                if not byte:
                    raise CuratorError("HUMAN_TTY_EOF")
                if byte == b"\n":
                    try:
                        value = line.rstrip(b"\r").decode("ascii")
                    except UnicodeError as exc:
                        raise CuratorError("HUMAN_TTY_ASCII") from exc
                    if value in {"APPROVE", "REJECT"}:
                        return value
                    os.write(fd, b"Invalid choice; no decision was recorded.\n")
                    break
                line.extend(byte)
            else:
                raise CuratorError("HUMAN_TTY_INPUT_TOO_LONG")
    finally:
        os.close(fd)


__all__ = ["read_foreground_decision"]
