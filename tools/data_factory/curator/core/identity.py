"""Descriptor-based streaming file and strict tree identities."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

from .errors import CuratorError
from .jsonio import canonical_digest


def read_regular_bytes(path: str | Path, *, code: str = "FILE_READ") -> bytes:
    source = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise CuratorError(code, f"{source}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CuratorError(code, f"regular file required: {source}")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            return stream.read()
    finally:
        if fd >= 0:
            os.close(fd)


def file_sha256(path: str | Path) -> str:
    source = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise CuratorError("FILE_READ", f"{source}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CuratorError("FILE_READ", f"regular file required: {source}")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    finally:
        if fd >= 0:
            os.close(fd)


def tree_snapshot(root: str | Path) -> dict[str, list[int]]:
    base = Path(root)
    if base.is_symlink() or not base.is_dir():
        raise CuratorError("TREE_ROOT", f"regular directory required: {base}")
    result = {}
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base).as_posix()
        if path.is_symlink():
            raise CuratorError("TREE_SYMLINK", relative)
        details = path.stat(follow_symlinks=False)
        if stat.S_ISDIR(details.st_mode):
            result[relative + "/"] = [0, details.st_mtime_ns]
        elif stat.S_ISREG(details.st_mode):
            result[relative] = [details.st_size, details.st_mtime_ns]
        else:
            raise CuratorError("TREE_SPECIAL_FILE", relative)
    return result


def _identity(base: Path, snapshot: dict[str, list[int]]) -> tuple[str, dict[str, str]]:
    files = {
        name: file_sha256(base / name) for name in snapshot if not name.endswith("/")
    }
    return canonical_digest({"files": files}), files


def tree_identity(root: str | Path) -> tuple[str, dict[str, str]]:
    base = Path(root)
    return _identity(base, tree_snapshot(base))


def stable_tree_identity(
    root: str | Path, *, code: str
) -> tuple[dict[str, list[int]], str]:
    base = Path(root)
    before = tree_snapshot(base)
    digest, _ = _identity(base, before)
    if tree_snapshot(base) != before:
        raise CuratorError(code, "metadata changed")
    return before, digest


def assert_tree_identity(
    root: str | Path,
    expected_snapshot: dict[str, list[int]],
    expected_digest: str,
    *,
    code: str,
) -> None:
    before = tree_snapshot(root)
    if before != expected_snapshot:
        raise CuratorError(code, "metadata changed")
    digest, _ = _identity(Path(root), before)
    if digest != expected_digest or tree_snapshot(root) != before:
        raise CuratorError(code, "payload changed")


__all__ = [
    "assert_tree_identity",
    "file_sha256",
    "read_regular_bytes",
    "stable_tree_identity",
    "tree_identity",
    "tree_snapshot",
]
