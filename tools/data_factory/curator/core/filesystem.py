"""Exclusive evidence writes and parent-dirfd anchored path ownership."""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import errno
import json
import os
from pathlib import Path
import secrets
import stat

from .errors import CuratorError


def reject_symlink_components(path: str | Path, code: str) -> Path:
    if "\x00" in os.fspath(path):
        raise CuratorError(code, "NUL in path")
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise CuratorError(code, f"symlink component rejected: {current}")
    return absolute


def _payload(value: dict) -> bytes:
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


def _rename_noreplace_syscall(
    source_fd: int,
    source_name: str,
    target_fd: int,
    target_name: str,
) -> None:
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise CuratorError("OUTPUT_PUBLISH_UNSUPPORTED", "renameat2") from exc
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if function(
        source_fd,
        os.fsencode(source_name),
        target_fd,
        os.fsencode(target_name),
        1,
    ):
        error = ctypes.get_errno()
        raise CuratorError(
            "OUTPUT_EXISTS" if error == errno.EEXIST else "OUTPUT_PUBLISH",
            os.strerror(error),
        )


def _recover_mismatched_rename(
    source_fd: int,
    source_name: str,
    target_fd: int,
    target_name: str,
) -> str:
    """Move a mismatched target away without deleting any unowned inode."""
    recovery = "retained_at_target"
    try:
        _rename_noreplace_syscall(
            target_fd,
            target_name,
            source_fd,
            source_name,
        )
        recovery = "restored_to_source"
    except CuratorError:
        quarantine = (
            f".{target_name}.{os.getpid()}.{secrets.token_hex(12)}.curator-ambiguous"
        )
        try:
            _rename_noreplace_syscall(
                target_fd,
                target_name,
                target_fd,
                quarantine,
            )
            recovery = f"preserved_as_{quarantine}"
        except CuratorError:
            pass
    try:
        _fsync_rename_parents(source_fd, target_fd)
    except OSError:
        recovery += "_fsync_ambiguous"
    return recovery


def _same_directory(left_fd: int, right_fd: int) -> bool:
    left = os.fstat(left_fd)
    right = os.fstat(right_fd)
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _fsync_rename_parents(source_fd: int, target_fd: int) -> None:
    os.fsync(source_fd)
    if not _same_directory(source_fd, target_fd):
        os.fsync(target_fd)


def rename_open_file_noreplace(
    descriptor: int,
    parent_fd: int,
    source_name: str,
    target_name: str,
) -> None:
    """Rename the exact opened regular file to a new name in its parent."""
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        raise CuratorError("OUTPUT_COMMITTED_IDENTITY")
    identity = (opened.st_dev, opened.st_ino)
    try:
        source = os.stat(source_name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise CuratorError("OUTPUT_COMMITTED_IDENTITY", source_name) from exc
    if (
        not stat.S_ISREG(source.st_mode)
        or (
            source.st_dev,
            source.st_ino,
        )
        != identity
    ):
        raise CuratorError("OUTPUT_COMMITTED_IDENTITY", source_name)
    _rename_noreplace_syscall(parent_fd, source_name, parent_fd, target_name)
    target = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(target.st_mode)
        or (
            target.st_dev,
            target.st_ino,
        )
        != identity
    ):
        recovery = _recover_mismatched_rename(
            parent_fd,
            source_name,
            parent_fd,
            target_name,
        )
        raise CuratorError("OUTPUT_COMMITTED_IDENTITY_AMBIGUOUS", recovery)
    try:
        os.fsync(parent_fd)
    except OSError as exc:
        raise CuratorError("OUTPUT_PUBLISH", str(exc)) from exc


def fsync_directory(path: str | Path) -> None:
    """Durably persist one existing directory entry set."""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise CuratorError("DIRECTORY_FSYNC", str(path))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except CuratorError:
        raise
    except OSError as exc:
        raise CuratorError("DIRECTORY_FSYNC", f"{path}: {exc}") from exc


def _temporary_event(parent_fd: int, target_name: str) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(32):
        name = f".{target_name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        try:
            return os.open(name, flags, 0o400, dir_fd=parent_fd), name
        except FileExistsError:
            continue
        except OSError as exc:
            raise CuratorError("EVENT_CREATE", f"{target_name}: {exc}") from exc
    raise CuratorError("EVENT_CREATE", f"temporary name exhaustion: {target_name}")


def write_json_exclusive(path: str | Path, value: dict) -> None:
    """Atomically expose one complete, durable, no-replace JSON file."""
    payload = _payload(value)
    requested = Path(path)
    if (
        requested.name in {"", ".", ".."}
        or Path(requested.name).name != requested.name
        or "\x00" in requested.name
    ):
        raise CuratorError("EVENT_PATH", str(requested))
    try:
        parent = reject_symlink_components(requested.parent, "EVENT_PARENT").resolve(
            strict=True
        )
    except OSError as exc:
        raise CuratorError("EVENT_PARENT", str(requested.parent)) from exc
    if not parent.is_dir():
        raise CuratorError("EVENT_PARENT", str(parent))
    target = parent / requested.name
    parent_fd = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor = -1
    temporary_name: str | None = None
    identity: tuple[int, int] | None = None
    target_linked = False
    try:
        descriptor, temporary_name = _temporary_event(parent_fd, target.name)
        details = os.fstat(descriptor)
        identity = (details.st_dev, details.st_ino)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short event write")
            view = view[written:]
        os.fsync(descriptor)
        try:
            _rename_noreplace_syscall(
                parent_fd,
                temporary_name,
                parent_fd,
                target.name,
            )
        except CuratorError as rename_exc:
            if rename_exc.code == "OUTPUT_EXISTS":
                raise CuratorError("EVENT_EXISTS", str(target)) from rename_exc
            raise
        target_linked = True
        linked = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if (linked.st_dev, linked.st_ino) != identity:
            recovery = _recover_mismatched_rename(
                parent_fd,
                temporary_name,
                parent_fd,
                target.name,
            )
            raise CuratorError("EVENT_COMMIT_IDENTITY", recovery)
        temporary_name = None
        os.fsync(parent_fd)
        descriptor_to_close = descriptor
        descriptor = -1
        try:
            os.close(descriptor_to_close)
        except OSError:
            # All bytes and both directory-entry transitions are already
            # durable.  A close error cannot revoke the committed event.
            pass
    except BaseException as exc:
        cleanup_error: BaseException | None = None
        if temporary_name is not None and identity is not None:
            try:
                remove_owned_regular_file(
                    parent / temporary_name,
                    device=identity[0],
                    inode=identity[1],
                )
                temporary_name = None
            except FileNotFoundError:
                temporary_name = None
            except BaseException as cleanup_exc:
                cleanup_error = cleanup_error or cleanup_exc
        if descriptor >= 0:
            descriptor_to_close = descriptor
            descriptor = -1
            try:
                os.close(descriptor_to_close)
            except BaseException as cleanup_exc:
                cleanup_error = cleanup_error or cleanup_exc
        if cleanup_error is not None:
            raise CuratorError(
                "EVENT_COMMIT_AMBIGUOUS",
                f"{target}: {cleanup_error}",
            ) from exc
        if target_linked:
            # Once the final name may have been exposed, never delete by that
            # mutable name. The caller validates the exact immutable payload and
            # retries the parent fsync before accepting the event.
            raise CuratorError("EVENT_COMMIT_AMBIGUOUS", str(target)) from exc
        if isinstance(exc, (CuratorError, KeyboardInterrupt, SystemExit)):
            raise
        raise CuratorError("EVENT_WRITE", f"{target}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


@dataclass(frozen=True)
class OwnedDirectory:
    parent: str
    name: str
    device: int
    inode: int

    @classmethod
    def capture(cls, path: str | Path) -> "OwnedDirectory":
        target = Path(path)
        if (
            target.name in ("", ".", "..")
            or "\x00" in target.name
            or target.is_symlink()
        ):
            raise CuratorError("OWNED_PATH")
        parent = reject_symlink_components(target.parent, "OWNED_PARENT").resolve(
            strict=True
        )
        parent_fd = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            details = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        finally:
            os.close(parent_fd)
        if not stat.S_ISDIR(details.st_mode):
            raise CuratorError("OWNED_DIRECTORY")
        return cls(str(parent), target.name, details.st_dev, details.st_ino)

    @property
    def path(self) -> Path:
        return Path(self.parent) / self.name

    def parent_fd(self) -> int:
        reject_symlink_components(self.parent, "OWNED_PARENT")
        descriptor = os.open(
            self.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise CuratorError("OWNED_PARENT")
        return descriptor

    def verify_at(self, parent_fd: int) -> None:
        try:
            details = os.stat(self.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise CuratorError("OWNED_DIRECTORY_CHANGED", str(exc)) from exc
        if not stat.S_ISDIR(details.st_mode) or (details.st_dev, details.st_ino) != (
            self.device,
            self.inode,
        ):
            raise CuratorError("OWNED_DIRECTORY_CHANGED")

    def as_json(self) -> dict:
        return {
            "parent": self.parent,
            "name": self.name,
            "device": self.device,
            "inode": self.inode,
        }

    @classmethod
    def from_json(cls, value: object) -> "OwnedDirectory":
        if not isinstance(value, dict) or set(value) != {
            "parent",
            "name",
            "device",
            "inode",
        }:
            raise CuratorError("OWNED_DIRECTORY_FIELDS")
        if (
            not isinstance(value["parent"], str)
            or not Path(value["parent"]).is_absolute()
            or not isinstance(value["name"], str)
            or value["name"] in ("", ".", "..")
            or Path(value["name"]).name != value["name"]
            or "\x00" in value["name"]
            or type(value["device"]) is not int
            or value["device"] < 0
            or type(value["inode"]) is not int
            or value["inode"] <= 0
        ):
            raise CuratorError("OWNED_DIRECTORY_CONTRACT")
        return cls(value["parent"], value["name"], value["device"], value["inode"])


def _owned_directory_present(owned: OwnedDirectory, parent_fd: int) -> bool:
    try:
        details = os.stat(owned.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CuratorError("OWNED_DIRECTORY_CHANGED", str(exc)) from exc
    if not stat.S_ISDIR(details.st_mode) or (details.st_dev, details.st_ino) != (
        owned.device,
        owned.inode,
    ):
        raise CuratorError("OWNED_DIRECTORY_CHANGED")
    return True


def remove_owned_directory(
    owned: OwnedDirectory,
    *,
    staging_path: str | Path | None = None,
) -> None:
    """Remove a captured tree; an explicit stage makes interruption resumable."""
    parent = Path(owned.parent)
    staged_path = (
        parent / f".{owned.name}.{os.getpid()}.{secrets.token_hex(12)}.curator-delete"
        if staging_path is None
        else Path(staging_path)
    )
    if (
        staged_path.parent != parent
        or staged_path.name in {"", ".", "..", owned.name}
        or Path(staged_path.name).name != staged_path.name
        or "\x00" in staged_path.name
    ):
        raise CuratorError("OWNED_STAGE_PATH")
    staged = OwnedDirectory(
        owned.parent,
        staged_path.name,
        owned.device,
        owned.inode,
    )
    parent_fd = staged.parent_fd()
    try:
        source_present = _owned_directory_present(owned, parent_fd)
        staged_present = _owned_directory_present(staged, parent_fd)
    finally:
        os.close(parent_fd)
    if source_present and staged_present:
        raise CuratorError("OWNED_DIRECTORY_DUPLICATED")
    if not source_present and not staged_present:
        raise CuratorError("OWNED_DIRECTORY_MISSING")
    if source_present:
        try:
            rename_noreplace_at(owned, staged_path)
        except BaseException:
            parent_fd = staged.parent_fd()
            try:
                if not _owned_directory_present(staged, parent_fd):
                    raise
            finally:
                os.close(parent_fd)
            # The exact inode moved even if durability reporting was interrupted.

    parent_fd = staged.parent_fd()
    descriptor = -1
    try:
        descriptor = os.open(
            staged.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode) or (details.st_dev, details.st_ino) != (
            staged.device,
            staged.inode,
        ):
            raise CuratorError("OWNED_DIRECTORY_CHANGED")
        for _root, directories, files, directory_fd in os.fwalk(
            ".",
            topdown=False,
            follow_symlinks=False,
            dir_fd=descriptor,
        ):
            for name in files:
                os.unlink(name, dir_fd=directory_fd)
            for name in directories:
                os.rmdir(name, dir_fd=directory_fd)
        staged.verify_at(parent_fd)
        os.rmdir(staged.name, dir_fd=parent_fd)
        if os.fstat(descriptor).st_nlink != 0:
            raise CuratorError("OWNED_DIRECTORY_REMOVE_AMBIGUOUS")
        os.fsync(parent_fd)
    except CuratorError:
        raise
    except OSError as exc:
        raise CuratorError("OWNED_DIRECTORY_REMOVE", str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def remove_owned_regular_file(
    path: str | Path,
    *,
    device: int,
    inode: int,
) -> None:
    """Remove one captured regular file only after private-name isolation."""
    target = Path(path)
    if (
        target.name in {"", ".", ".."}
        or Path(target.name).name != target.name
        or "\x00" in target.name
    ):
        raise CuratorError("OWNED_FILE_PATH")
    parent = reject_symlink_components(target.parent, "OWNED_FILE_PARENT").resolve(
        strict=True
    )
    parent_fd = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    staged_name = f".{target.name}.{os.getpid()}.{secrets.token_hex(12)}.curator-delete"
    try:
        details = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(details.st_mode) or (details.st_dev, details.st_ino) != (
            device,
            inode,
        ):
            raise CuratorError("OWNED_FILE_CHANGED")
        try:
            _rename_noreplace_syscall(
                parent_fd,
                target.name,
                parent_fd,
                staged_name,
            )
            os.fsync(parent_fd)
        except BaseException:
            try:
                staged = os.stat(staged_name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                raise
            if (staged.st_dev, staged.st_ino) != (device, inode):
                raise CuratorError("OWNED_FILE_CHANGED")
        staged = os.stat(staged_name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(staged.st_mode) or (staged.st_dev, staged.st_ino) != (
            device,
            inode,
        ):
            raise CuratorError("OWNED_FILE_CHANGED")
        descriptor = os.open(
            staged_name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (device, inode):
                raise CuratorError("OWNED_FILE_CHANGED")
            current = os.stat(staged_name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (device, inode):
                raise CuratorError("OWNED_FILE_CHANGED")
            links_before = opened.st_nlink
            os.unlink(staged_name, dir_fd=parent_fd)
            if os.fstat(descriptor).st_nlink != links_before - 1:
                raise CuratorError("OWNED_FILE_REMOVE_AMBIGUOUS")
        finally:
            os.close(descriptor)
        os.fsync(parent_fd)
    except CuratorError:
        raise
    except OSError as exc:
        raise CuratorError("OWNED_FILE_REMOVE", str(exc)) from exc
    finally:
        os.close(parent_fd)


def rename_noreplace_at(source: OwnedDirectory, target: str | Path) -> None:
    target = Path(target)
    if (
        target.name in ("", ".", "..")
        or Path(target.name).name != target.name
        or "\x00" in target.name
    ):
        raise CuratorError("OUTPUT_PATH")
    target_parent = reject_symlink_components(target.parent, "OUTPUT_PARENT").resolve(
        strict=True
    )
    source_fd = source.parent_fd()
    target_fd = os.open(
        target_parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        source.verify_at(source_fd)
        _rename_noreplace_syscall(source_fd, source.name, target_fd, target.name)
        details = os.stat(target.name, dir_fd=target_fd, follow_symlinks=False)
        if (details.st_dev, details.st_ino) != (source.device, source.inode):
            recovery = _recover_mismatched_rename(
                source_fd,
                source.name,
                target_fd,
                target.name,
            )
            raise CuratorError("OUTPUT_COMMITTED_IDENTITY_AMBIGUOUS", recovery)
        try:
            _fsync_rename_parents(source_fd, target_fd)
        except OSError as exc:
            raise CuratorError("OUTPUT_PUBLISH", str(exc)) from exc
    finally:
        os.close(source_fd)
        os.close(target_fd)


__all__ = [
    "OwnedDirectory",
    "fsync_directory",
    "reject_symlink_components",
    "remove_owned_directory",
    "remove_owned_regular_file",
    "rename_open_file_noreplace",
    "rename_noreplace_at",
    "write_json_exclusive",
]
