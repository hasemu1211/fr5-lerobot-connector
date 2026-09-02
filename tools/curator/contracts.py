"""Small fail-closed primitives shared by the curator boundary modules."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any
import ctypes
import errno


DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
RFC3339_UTC = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z"
)


class CuratorError(ValueError):
    """A stable curator contract failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def canonical_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise CuratorError("JSON_NONFINITE", str(exc)) from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise CuratorError("JSON_DUPLICATE_KEY", key)
        value[key] = item
    return value


def _nonfinite(value: str) -> None:
    raise CuratorError("JSON_NONFINITE", value)


def load_json(path: str | Path, *, code: str) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise CuratorError(code, f"regular JSON file required: {source}")
    try:
        value = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_nonfinite,
        )
    except CuratorError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CuratorError(code, str(exc)) from exc
    if not isinstance(value, dict):
        raise CuratorError(code, "JSON object required")
    return value


def exact_fields(value: object, fields: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise CuratorError(code, f"expected={sorted(fields)} actual={actual}")
    return value


def finite_number(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CuratorError(code, "finite number required")
    return float(value)


def reject_symlink_components(path: str | Path, code: str) -> Path:
    """Reject any existing symlink in an explicitly supplied path."""
    absolute = Path(path).absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise CuratorError(code, f"symlink component rejected: {current}")
    return absolute


def rename_noreplace(source: str | Path, target: str | Path, *, code: str) -> None:
    """Linux atomic directory publication with RENAME_NOREPLACE."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise CuratorError(code, "renameat2(RENAME_NOREPLACE) is unavailable") from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(target),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise CuratorError(code, f"target already exists: {target}")
    raise CuratorError(code, os.strerror(error))


def read_regular_bytes(path: str | Path, *, code: str = "FILE_READ") -> bytes:
    """Read one exact regular file through an O_NOFOLLOW descriptor."""
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
    return "sha256:" + hashlib.sha256(read_regular_bytes(path)).hexdigest()


def write_json_atomic(path: str | Path, value: dict[str, Any]) -> None:
    target = Path(path)
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise CuratorError("WRITE_PARENT", str(target.parent))
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_json_exclusive(path: str | Path, value: dict[str, Any]) -> None:
    target = Path(path)
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise CuratorError("APPROVAL_PARENT", str(target.parent))
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags, 0o400)
    except FileExistsError as exc:
        raise CuratorError("APPROVAL_EXISTS", str(target)) from exc
    except OSError as exc:
        raise CuratorError("APPROVAL_CREATE", f"{target}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise


def tree_snapshot(root: str | Path) -> dict[str, list[int]]:
    """Return a strict metadata snapshot without following dataset symlinks."""
    base = Path(root)
    if base.is_symlink() or not base.is_dir():
        raise CuratorError("TREE_ROOT", f"regular directory required: {base}")
    result: dict[str, list[int]] = {}
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


def tree_identity(root: str | Path) -> tuple[str, dict[str, str]]:
    base = Path(root)
    snapshot = tree_snapshot(base)
    files = {
        relative: file_sha256(base / relative)
        for relative in snapshot
        if not relative.endswith("/")
    }
    return canonical_digest({"files": files}), files


__all__ = [
    "DIGEST",
    "RFC3339_UTC",
    "SAFE_ID",
    "CuratorError",
    "canonical_digest",
    "exact_fields",
    "file_sha256",
    "finite_number",
    "load_json",
    "read_regular_bytes",
    "reject_symlink_components",
    "rename_noreplace",
    "tree_identity",
    "tree_snapshot",
    "write_json_atomic",
    "write_json_exclusive",
]
