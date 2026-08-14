#!/usr/bin/env python3
"""Fail-closed recovery for an interrupted data-factory recorder transaction."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CAMERA = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_BINDINGS = {
    "resolved_job_digest", "selected_sheet_digest", "yaw0_sheet_digest",
    "cell_calibration_digest", "robot_system_digest", "collection_profile_digest",
    "object_profile_digest", "grasp_profile_digest",
}
_GUARD_FIELDS = {
    "schema_version", "run_id", "transaction_id", "episode_index", "state",
    "reason_code", "detail", "staging_manifest", "staging_manifest_digest",
}
_MANIFEST_FIELDS = {
    "schema_version", "run_id", "dataset_root", "episode_index", "staging_mode",
    "binding_digests", "camera_staging_dirs", "begin_snapshot",
}
_SNAPSHOT_FIELDS = {
    "total_episodes", "total_frames", "data_parquet", "committed_videos",
    "episode_metadata", "dataset_metadata",
}
_EVENT_FIELDS = {"run_id", "state", "reason_code", "episode_index", "rows", "monotonic_ns"}
_STAGING_OWNER_FILE = ".data_factory_staging_owner.json"


class RecoveryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(f"{code}: {message}")


class DatasetTransactionLock:
    """An advisory lock whose kernel-held fd is released if this process dies."""

    def __init__(self, dataset_root: Path | str) -> None:
        self.path = Path(dataset_root).resolve() / ".data_factory_transaction.lock"
        self.fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RecoveryError("DATASET_TRANSACTION_LOCK", "transaction lock path is a symlink")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.fd = os.open(self.path, flags, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self.fd is not None:
                os.close(self.fd)
            self.fd = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RecoveryError("DATASET_TRANSACTION_BUSY", "dataset transaction lock is held") from exc
            raise RecoveryError("DATASET_TRANSACTION_LOCK", str(exc)) from exc

    def release(self) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None

    def __enter__(self) -> "DatasetTransactionLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def canonical_json_digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _decode_json(text: str, code: str, source: Path | str) -> object:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"duplicate key: {key}")
            value[key] = item
        return value

    def nonfinite(value):
        raise ValueError(f"non-finite number: {value}")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RecoveryError(code, f"invalid JSON: {source}: {exc}") from exc


def _json(path: Path, code: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RecoveryError(code, f"not a regular file: {path}")
    try:
        value = _decode_json(path.read_text(encoding="utf-8"), code, path)
    except OSError as exc:
        raise RecoveryError(code, f"cannot read JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RecoveryError(code, f"JSON object required: {path}")
    return value


def _regular_files(root: Path, relative: str, pattern: str) -> dict[str, list[int]]:
    base = root / relative
    if not base.exists():
        return {}
    if base.is_symlink() or not base.is_dir():
        raise RecoveryError("RECOVERY_SYMLINK", f"unsafe dataset directory: {base}")
    result: dict[str, list[int]] = {}
    for path in sorted(base.rglob(pattern)):
        if path.is_symlink():
            raise RecoveryError("RECOVERY_SYMLINK", f"unsafe dataset entry: {path}")
        if path.is_file():
            details = path.stat()
            result[str(path.relative_to(root))] = [details.st_size, details.st_mtime_ns]
    return result


def dataset_snapshot(dataset_root: Path | str) -> dict:
    root = Path(dataset_root).resolve()
    meta = root / "meta"
    if meta.is_symlink():
        raise RecoveryError("RECOVERY_SYMLINK", f"unsafe dataset metadata directory: {meta}")
    if not meta.is_dir():
        raise RecoveryError("RECOVERY_SNAPSHOT", f"missing dataset metadata directory: {meta}")
    info = _json(meta / "info.json", "RECOVERY_SNAPSHOT")
    episodes, frames = info.get("total_episodes"), info.get("total_frames")
    if type(episodes) is not int or type(frames) is not int or episodes < 0 or frames < 0:
        raise RecoveryError("RECOVERY_SNAPSHOT", "meta/info.json has invalid totals")
    metadata: dict[str, list[int]] = {}
    for relative in ("meta/info.json", "meta/tasks.parquet", "meta/stats.json"):
        path = root / relative
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise RecoveryError("RECOVERY_SYMLINK", f"unsafe dataset metadata: {path}")
            details = path.stat()
            metadata[relative] = [details.st_size, details.st_mtime_ns]
    return {
        "total_episodes": episodes,
        "total_frames": frames,
        "data_parquet": _regular_files(root, "data", "*.parquet"),
        "committed_videos": _regular_files(root, "videos", "*"),
        "episode_metadata": _regular_files(root, "meta/episodes", "*.parquet"),
        "dataset_metadata": metadata,
    }


def _validate_snapshot(snapshot: object) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_FIELDS:
        raise RecoveryError("RECOVERY_MANIFEST", "staging manifest snapshot schema is invalid")
    for key in ("total_episodes", "total_frames"):
        if type(snapshot[key]) is not int or snapshot[key] < 0:
            raise RecoveryError("RECOVERY_MANIFEST", f"invalid snapshot total: {key}")
    for key in _SNAPSHOT_FIELDS - {"total_episodes", "total_frames"}:
        values = snapshot[key]
        if not isinstance(values, dict):
            raise RecoveryError("RECOVERY_MANIFEST", f"invalid snapshot map: {key}")
        for relative, details in values.items():
            path = Path(relative) if isinstance(relative, str) else Path("/")
            if (
                not isinstance(relative, str) or path.is_absolute() or ".." in path.parts
                or not isinstance(details, list) or len(details) != 2
                or any(type(value) is not int or value < 0 for value in details)
            ):
                raise RecoveryError("RECOVERY_MANIFEST", f"invalid snapshot entry: {key}")


def _write_json_at(directory_fd: int, name: str, value: dict) -> None:
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
    try:
        file = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with file:
            json.dump(value, file, sort_keys=True, separators=(",", ":"), allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise


def write_json_atomic(path: Path, value: dict) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(path.parent, flags)
    try:
        _write_json_at(directory_fd, path.name, value)
    finally:
        os.close(directory_fd)


def _owner_record(run_id: str, transaction_id: str, episode: int, manifest_digest: str) -> dict:
    return {
        "schema_version": "data_factory.staging_owner.v1",
        "run_id": run_id,
        "transaction_id": transaction_id,
        "episode_index": episode,
        "staging_manifest_digest": manifest_digest,
    }


def _open_directory(name: str | Path, *, dir_fd: int | None = None) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(name, flags, dir_fd=dir_fd)


def _same_directory(path: Path, directory_fd: int) -> bool:
    try:
        current, held = os.stat(path, follow_symlinks=False), os.fstat(directory_fd)
    except OSError:
        return False
    return stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino)


def _open_or_create_directory(name: str, parent_fd: int) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    return _open_directory(name, dir_fd=parent_fd)


def claim_staging_directories(
    dataset_root: Path | str,
    cameras: tuple[str, ...] | list[str],
    run_id: str,
    transaction_id: str,
    episode: int,
    manifest_digest: str,
) -> None:
    if not cameras or any(not isinstance(camera, str) or not _CAMERA.fullmatch(camera) for camera in cameras):
        raise RecoveryError("RECOVERY_STAGING_OWNER", "camera identity is invalid")
    root_fd = _open_directory(Path(dataset_root).resolve())
    try:
        images_fd = _open_or_create_directory("images", root_fd)
        try:
            for camera in cameras:
                camera_fd = _open_or_create_directory(f"observation.images.{camera}", images_fd)
                try:
                    episode_name = f"episode-{episode:06d}"
                    os.mkdir(episode_name, mode=0o700, dir_fd=camera_fd)
                    episode_fd = _open_directory(episode_name, dir_fd=camera_fd)
                    try:
                        _write_json_at(
                            episode_fd,
                            _STAGING_OWNER_FILE,
                            _owner_record(run_id, transaction_id, episode, manifest_digest),
                        )
                    finally:
                        os.close(episode_fd)
                    os.fsync(camera_fd)
                finally:
                    os.close(camera_fd)
        finally:
            os.close(images_fd)
    finally:
        os.close(root_fd)


def _json_at(directory_fd: int, name: str, code: str) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
        with os.fdopen(fd, encoding="utf-8") as file:
            value = _decode_json(file.read(), code, name)
    except OSError as exc:
        raise RecoveryError(code, f"cannot read JSON file {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise RecoveryError(code, "owned staging marker must be a JSON object")
    return value


def _clear_directory(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(details.st_mode):
            child_fd = _open_directory(name, dir_fd=directory_fd)
            try:
                _clear_directory(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _remove_owned_staging(root_fd: int, cameras: dict, guard: dict) -> None:
    try:
        images_fd = _open_directory("images", dir_fd=root_fd)
    except FileNotFoundError:
        return
    try:
        episode_name = f"episode-{guard['episode_index']:06d}"
        owner = _owner_record(
            guard["run_id"], guard["transaction_id"], guard["episode_index"],
            guard["staging_manifest_digest"],
        )
        for camera in cameras:
            try:
                camera_fd = _open_directory(f"observation.images.{camera}", dir_fd=images_fd)
            except FileNotFoundError:
                continue
            try:
                try:
                    episode_fd = _open_directory(episode_name, dir_fd=camera_fd)
                except FileNotFoundError:
                    continue
                try:
                    if _json_at(episode_fd, _STAGING_OWNER_FILE, "RECOVERY_STAGING_OWNER") != owner:
                        raise RecoveryError("RECOVERY_STAGING_OWNER", "staging ownership marker mismatch")
                    _clear_directory(episode_fd)
                finally:
                    os.close(episode_fd)
                os.rmdir(episode_name, dir_fd=camera_fd)
                os.fsync(camera_fd)
            finally:
                os.close(camera_fd)
    finally:
        os.close(images_fd)


def _durable_unlink_at(directory_fd: int, name: str) -> None:
    os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _validate_guard(root: Path, run_root: Path, meta_fd: int) -> tuple[Path, dict, Path, dict, int]:
    guard_path = root / "meta" / "quarantine.json"
    guard = _json_at(meta_fd, guard_path.name, "RECOVERY_GUARD")
    if set(guard) != _GUARD_FIELDS or guard.get("schema_version") != "data_factory.commit_guard.v2":
        raise RecoveryError("RECOVERY_GUARD", "commit guard schema is not exact v2")
    run_id = guard.get("run_id")
    episode = guard.get("episode_index")
    if not isinstance(run_id, str) or not _ID.fullmatch(run_id) or type(episode) is not int or episode < 0:
        raise RecoveryError("RECOVERY_GUARD", "commit guard run identity is invalid")
    if guard.get("state") not in {"RECORDING", "FROZEN", "COMMITTING", "QUARANTINED_COMMIT"}:
        raise RecoveryError("RECOVERY_GUARD", "commit guard state cannot be recovered")
    if not isinstance(guard.get("transaction_id"), str) or guard["transaction_id"] != f"{run_id}:episode-{episode:06d}":
        raise RecoveryError("RECOVERY_GUARD", "commit guard transaction id is invalid")
    if not all(isinstance(guard.get(key), str) for key in ("reason_code", "detail", "staging_manifest")) or not isinstance(guard.get("staging_manifest_digest"), str) or not _DIGEST.fullmatch(guard["staging_manifest_digest"]):
        raise RecoveryError("RECOVERY_GUARD", "commit guard fields are invalid")
    run_dir = (run_root / run_id).resolve()
    expected = run_dir / "staging_manifest.json"
    if str(expected) != guard["staging_manifest"] or run_dir.parent != run_root:
        raise RecoveryError("RECOVERY_GUARD", "commit guard manifest path is not exact")
    try:
        run_fd = _open_directory(run_dir)
        manifest = _json_at(run_fd, expected.name, "RECOVERY_MANIFEST")
        manifest_digest = canonical_json_digest(manifest)
    except OSError as exc:
        raise RecoveryError("RECOVERY_MANIFEST", f"cannot pin run directory: {exc}") from exc
    except (RecoveryError, TypeError, ValueError) as exc:
        if "run_fd" in locals():
            os.close(run_fd)
        if isinstance(exc, RecoveryError):
            raise
        raise RecoveryError("RECOVERY_MANIFEST", "staging manifest cannot be canonically hashed") from exc
    if manifest_digest != guard["staging_manifest_digest"]:
        os.close(run_fd)
        raise RecoveryError("RECOVERY_MANIFEST", "staging manifest digest mismatch")
    return guard_path, guard, expected, manifest, run_fd


def _validate_manifest(root: Path, run_root: Path, guard: dict, manifest_path: Path, manifest: dict) -> list[Path]:
    if set(manifest) != _MANIFEST_FIELDS or manifest.get("schema_version") != "data_factory.staging_manifest.v1":
        raise RecoveryError("RECOVERY_MANIFEST", "staging manifest schema is not exact v1")
    if manifest.get("run_id") != guard["run_id"] or manifest.get("dataset_root") != str(root) or manifest.get("episode_index") != guard["episode_index"] or manifest.get("staging_mode") != "batch":
        raise RecoveryError("RECOVERY_MANIFEST", "staging manifest identity is invalid")
    bindings = manifest.get("binding_digests")
    if not isinstance(bindings, dict) or set(bindings) != _BINDINGS or any(not isinstance(value, str) or not _DIGEST.fullmatch(value) for value in bindings.values()):
        raise RecoveryError("RECOVERY_MANIFEST", "staging manifest bindings are invalid")
    _validate_snapshot(manifest.get("begin_snapshot"))
    if manifest_path != run_root / guard["run_id"] / "staging_manifest.json":
        raise RecoveryError("RECOVERY_MANIFEST", "staging manifest path is not exact")
    cameras = manifest.get("camera_staging_dirs")
    if not isinstance(cameras, dict) or not cameras:
        raise RecoveryError("RECOVERY_MANIFEST", "camera staging dirs are invalid")
    episode_dir = f"episode-{guard['episode_index']:06d}"
    paths: list[Path] = []
    for camera, value in cameras.items():
        expected = root / "images" / f"observation.images.{camera}" / episode_dir
        if not isinstance(camera, str) or not _CAMERA.fullmatch(camera) or not isinstance(value, str) or value != str(expected):
            raise RecoveryError("RECOVERY_MANIFEST", "camera staging path is not exact")
        cursor = root
        for part in expected.relative_to(root).parts:
            cursor /= part
            if cursor.is_symlink():
                raise RecoveryError("RECOVERY_SYMLINK", f"unsafe staging path: {cursor}")
        if expected.resolve().parent.parent != root / "images":
            raise RecoveryError("RECOVERY_MANIFEST", "camera staging path escapes dataset root")
        if expected.exists() and not expected.is_dir():
            raise RecoveryError("RECOVERY_MANIFEST", f"staging target is not a directory: {expected}")
        paths.append(expected)
    if len(set(paths)) != len(paths):
        raise RecoveryError("RECOVERY_MANIFEST", "duplicate staging paths")
    return paths


def _recovery_events(path: Path, guard: dict, directory_fd: int | None = None) -> tuple[list[dict], dict, bool]:
    try:
        if directory_fd is None:
            if path.is_symlink() or not path.is_file():
                raise OSError("missing or unsafe event log")
            text = path.read_text(encoding="utf-8")
        else:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path.name, flags, dir_fd=directory_fd)
            with os.fdopen(fd, encoding="utf-8") as file:
                text = file.read()
        lines = text.splitlines()
    except OSError as exc:
        raise RecoveryError("RECOVERY_EVENT", f"cannot read event log: {path}: {exc}") from exc
    events = []
    for index, line in enumerate(lines, 1):
        value = _decode_json(line, "RECOVERY_EVENT", f"{path}:{index}")
        if not isinstance(value, dict) or set(value) != _EVENT_FIELDS:
            raise RecoveryError("RECOVERY_EVENT", f"invalid event schema: {path}:{index}")
        if (
            value["run_id"] != guard["run_id"] or value["episode_index"] != guard["episode_index"]
            or not isinstance(value["state"], str) or not isinstance(value["reason_code"], str)
            or type(value["rows"]) is not int or value["rows"] < 0
            or type(value["monotonic_ns"]) is not int or value["monotonic_ns"] < 0
        ):
            raise RecoveryError("RECOVERY_EVENT", f"invalid event identity: {path}:{index}")
        if events and value["monotonic_ns"] < events[-1]["monotonic_ns"]:
            raise RecoveryError("RECOVERY_EVENT", "event timestamps must not decrease")
        events.append(value)
    if not events or events[0]["state"] != "RECORDING" or events[0]["reason_code"] != "BEGIN":
        raise RecoveryError("RECOVERY_EVENT", "event log must begin with RECORDING/BEGIN")
    recovered = {
        "run_id": guard["run_id"], "state": "ABORTED", "reason_code": "RECOVERED_ABORT",
        "episode_index": guard["episode_index"], "rows": 0,
    }
    terminal = events[-1]
    already_recovered = False
    if all(terminal.get(key) == value for key, value in recovered.items()):
        events = events[:-1]
        already_recovered = True
    expected = [("RECORDING", "BEGIN")]
    if guard["state"] == "FROZEN":
        expected.append(("FROZEN", "FROZEN"))
    if [(event["state"], event["reason_code"]) for event in events] != expected:
        raise RecoveryError("RECOVERY_EVENT", "event log does not match the durable guard state")
    return events, recovered, already_recovered


def _append_recovery_event(path: Path, guard: dict, directory_fd: int | None = None) -> None:
    events, event, already_recovered = _recovery_events(path, guard, directory_fd)
    if already_recovered:
        return
    owns_directory_fd = directory_fd is None
    if directory_fd is None:
        directory_fd = _open_directory(path.parent)
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.name, flags, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as file:
            fd = -1
            stamp = max(time.monotonic_ns(), events[-1]["monotonic_ns"] + 1)
            file.write(json.dumps({**event, "monotonic_ns": stamp}, separators=(",", ":")) + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.fsync(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if owns_directory_fd:
            os.close(directory_fd)


def _quarantine(guard_path: Path, guard: dict, reason: str, meta_fd: int | None = None) -> dict:
    guarded = dict(guard, state="QUARANTINED_COMMIT", reason_code=reason, detail=reason)
    try:
        if meta_fd is None:
            write_json_atomic(guard_path, guarded)
        else:
            _write_json_at(meta_fd, guard_path.name, guarded)
        state, detail = "QUARANTINED_COMMIT", ""
    except OSError as exc:
        state, detail = guard.get("state", "UNKNOWN"), f"guard update failed: {exc}"
    return {"ok": False, "state": state, "reason_code": reason, "run_id": guard["run_id"], "detail": detail}


def recover_orphaned_transaction(dataset_root: Path | str, run_root: Path | str) -> dict:
    root, runs = Path(dataset_root).resolve(), Path(run_root).resolve()
    meta = root / "meta"
    if meta.is_symlink():
        return {"ok": False, "state": "QUARANTINED_COMMIT", "reason_code": "RECOVERY_SYMLINK", "detail": f"unsafe dataset metadata directory: {meta}"}
    if not meta.is_dir():
        return {"ok": False, "state": "QUARANTINED_COMMIT", "reason_code": "RECOVERY_SNAPSHOT", "detail": f"missing dataset metadata directory: {meta}"}
    lock = DatasetTransactionLock(root)
    try:
        lock.acquire()
    except RecoveryError as exc:
        reason = "RECOVERY_LOCK_BUSY" if exc.code == "DATASET_TRANSACTION_BUSY" else exc.code
        return {"ok": False, "state": "LOCKED", "reason_code": reason, "detail": exc.message}
    root_fd = meta_fd = run_fd = None
    try:
        try:
            root_fd = _open_directory(root)
            meta_fd = _open_directory("meta", dir_fd=root_fd)
        except OSError as exc:
            return {"ok": False, "state": "QUARANTINED_COMMIT", "reason_code": "RECOVERY_SYMLINK", "detail": str(exc)}
        guard_path = root / "meta" / "quarantine.json"
        try:
            guard_details = os.stat(guard_path.name, dir_fd=meta_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {"ok": True, "state": "NO_GUARD", "reason_code": "NO_ORPHAN"}
        if not stat.S_ISREG(guard_details.st_mode):
            return {"ok": False, "state": "QUARANTINED_COMMIT", "reason_code": "RECOVERY_GUARD", "detail": "commit guard is not a regular file"}
        try:
            guard_path, guard, manifest_path, manifest, run_fd = _validate_guard(root, runs, meta_fd)
            staging_dirs = _validate_manifest(root, runs, guard, manifest_path, manifest)
        except RecoveryError as exc:
            return {"ok": False, "state": "QUARANTINED_COMMIT", "reason_code": exc.code, "detail": exc.message}
        except OSError as exc:
            return _quarantine(guard_path, guard, "RECOVERY_MANIFEST", meta_fd) | {"detail": str(exc)}
        if guard["state"] in {"COMMITTING", "QUARANTINED_COMMIT"}:
            return _quarantine(guard_path, guard, "RECOVERY_COMMIT_UNCERTAIN", meta_fd)
        try:
            events_path = runs / guard["run_id"] / "events.jsonl"
            if (
                not _same_directory(root, root_fd) or not _same_directory(meta, meta_fd)
                or not _same_directory(manifest_path.parent, run_fd)
            ):
                return _quarantine(guard_path, guard, "RECOVERY_DIRECTORY_CHANGED", meta_fd)
            _recovery_events(events_path, guard, run_fd)
            if dataset_snapshot(root) != manifest["begin_snapshot"]:
                return _quarantine(guard_path, guard, "RECOVERY_SNAPSHOT_CHANGED", meta_fd)
            if not _same_directory(root, root_fd):
                return _quarantine(guard_path, guard, "RECOVERY_DIRECTORY_CHANGED", meta_fd)
            _remove_owned_staging(root_fd, manifest["camera_staging_dirs"], guard)
            if any(path.exists() or path.is_symlink() for path in staging_dirs):
                return _quarantine(guard_path, guard, "RECOVERY_STAGING_REMAINS", meta_fd)
            if dataset_snapshot(root) != manifest["begin_snapshot"]:
                return _quarantine(guard_path, guard, "RECOVERY_SNAPSHOT_CHANGED", meta_fd)
            if (
                not _same_directory(root, root_fd) or not _same_directory(meta, meta_fd)
                or not _same_directory(manifest_path.parent, run_fd)
            ):
                return _quarantine(guard_path, guard, "RECOVERY_DIRECTORY_CHANGED", meta_fd)
            result = {
                "schema_version": "data_factory.recorder_result.v1", "run_id": guard["run_id"],
                "transaction_id": guard["transaction_id"], "episode_index": guard["episode_index"],
                "state": "ABORTED", "reason_code": "RECOVERED_ABORT", "rows": 0, "detail": "orphaned transaction recovered",
            }
            _write_json_at(run_fd, "result.json", result)
            _append_recovery_event(events_path, guard, run_fd)
            _durable_unlink_at(meta_fd, guard_path.name)
            return {"ok": True, "state": "ABORTED", "reason_code": "RECOVERED_ABORT", "run_id": guard["run_id"]}
        except RecoveryError as exc:
            return _quarantine(guard_path, guard, exc.code, meta_fd) | {"detail": exc.message}
        except OSError as exc:
            return _quarantine(guard_path, guard, "RECOVERY_ABORT_FAILED", meta_fd) | {"detail": str(exc)}
    finally:
        for fd in (run_fd, meta_fd, root_fd):
            if fd is not None:
                os.close(fd)
        lock.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--run-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = recover_orphaned_transaction(args.dataset_root, args.run_root)
    except RecoveryError as exc:
        result = {"ok": False, "reason_code": exc.code, "detail": exc.message}
    print(json.dumps(result, separators=(",", ":"), allow_nan=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
