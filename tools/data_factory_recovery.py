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
_SNAPSHOT_V1_FIELDS = {
    "total_episodes", "total_frames", "data_parquet", "committed_videos",
    "episode_metadata", "dataset_metadata",
}
_SNAPSHOT_V2_FIELDS = _SNAPSHOT_V1_FIELDS | {
    "schema_version", "source_provenance", "recording_quality",
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


def decode_json_strict(text: str, code: str, source: Path | str) -> object:
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
        value = decode_json_strict(path.read_text(encoding="utf-8"), code, path)
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
    quality_path = root / "meta" / "recording_quality.jsonl"
    if quality_path.exists() or quality_path.is_symlink():
        if quality_path.is_symlink() or not quality_path.is_file():
            raise RecoveryError(
                "RECOVERY_SYMLINK", f"unsafe dataset metadata: {quality_path}",
            )
        quality_bytes = quality_path.read_bytes()
    else:
        quality_bytes = b""
    return {
        "schema_version": "data_factory.dataset_snapshot.v2",
        "total_episodes": episodes,
        "total_frames": frames,
        "data_parquet": _regular_files(root, "data", "*.parquet"),
        "committed_videos": _regular_files(root, "videos", "*"),
        "episode_metadata": _regular_files(root, "meta/episodes", "*.parquet"),
        "dataset_metadata": metadata,
        "source_provenance": _regular_files(
            root, "meta/source_provenance", "*.jsonl",
        ),
        "recording_quality": {
            "size": len(quality_bytes),
            "sha256": "sha256:" + hashlib.sha256(quality_bytes).hexdigest(),
        },
    }


def _validate_snapshot(snapshot: object) -> int:
    if not isinstance(snapshot, dict):
        raise RecoveryError("RECOVERY_MANIFEST", "staging manifest snapshot schema is invalid")
    fields = set(snapshot)
    if fields == _SNAPSHOT_V1_FIELDS:
        version = 1
    elif (
        fields == _SNAPSHOT_V2_FIELDS
        and snapshot.get("schema_version") == "data_factory.dataset_snapshot.v2"
    ):
        version = 2
    else:
        raise RecoveryError("RECOVERY_MANIFEST", "staging manifest snapshot schema is invalid")
    for key in ("total_episodes", "total_frames"):
        if type(snapshot[key]) is not int or snapshot[key] < 0:
            raise RecoveryError("RECOVERY_MANIFEST", f"invalid snapshot total: {key}")
    maps = _SNAPSHOT_V1_FIELDS - {"total_episodes", "total_frames"}
    if version == 2:
        maps = maps | {"source_provenance"}
    for key in maps:
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
    if version == 2:
        quality = snapshot["recording_quality"]
        if (
            not isinstance(quality, dict)
            or set(quality) != {"size", "sha256"}
            or type(quality.get("size")) is not int
            or quality["size"] < 0
            or not isinstance(quality.get("sha256"), str)
            or not _DIGEST.fullmatch(quality["sha256"])
        ):
            raise RecoveryError(
                "RECOVERY_MANIFEST", "invalid recording quality snapshot",
            )
    return version


def dataset_snapshot_unchanged(before: object, after: object) -> bool:
    """Compare current snapshots while retaining interrupted v1 compatibility."""
    version = _validate_snapshot(before)
    _validate_snapshot(after)
    fields = _SNAPSHOT_V1_FIELDS if version == 1 else _SNAPSHOT_V2_FIELDS
    return all(before[field] == after.get(field) for field in fields)


def validate_staging_manifest_contract(
    value: object, dataset_root: Path | str, *, episode_index: int,
    run_id: str | None = None, camera_names: tuple[str, ...] | list[str] | None = None,
) -> dict:
    """Validate the immutable part of a recorder staging manifest.

    This intentionally does not require staging directories to still exist, so
    the same contract can be checked after a successful commit removed them.
    """
    root = Path(dataset_root).resolve()
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
        raise RecoveryError("RECOVERY_MANIFEST", "staging manifest schema is not exact")
    manifest = dict(value)
    manifest_run_id = manifest.get("run_id")
    if (
        manifest.get("schema_version") != "data_factory.staging_manifest.v1"
        or not isinstance(manifest_run_id, str)
        or not _ID.fullmatch(manifest_run_id)
        or run_id is not None and manifest_run_id != run_id
        or type(episode_index) is not int
        or episode_index < 0
        or manifest.get("episode_index") != episode_index
        or manifest.get("dataset_root") != str(root)
        or manifest.get("staging_mode") != "batch"
    ):
        raise RecoveryError("RECOVERY_MANIFEST", "staging manifest identity is invalid")
    bindings = manifest.get("binding_digests")
    if (
        not isinstance(bindings, dict)
        or set(bindings) != _BINDINGS
        or any(
            not isinstance(item, str) or not _DIGEST.fullmatch(item)
            for item in bindings.values()
        )
    ):
        raise RecoveryError("RECOVERY_MANIFEST", "staging manifest bindings are invalid")
    _validate_snapshot(manifest.get("begin_snapshot"))

    cameras = manifest.get("camera_staging_dirs")
    expected_names = None if camera_names is None else set(camera_names)
    if (
        not isinstance(cameras, dict)
        or not cameras
        or expected_names is not None and set(cameras) != expected_names
    ):
        raise RecoveryError("RECOVERY_MANIFEST", "camera staging dirs are invalid")
    episode_dir = f"episode-{episode_index:06d}"
    paths = []
    for camera, path in cameras.items():
        expected = root / "images" / f"observation.images.{camera}" / episode_dir
        if (
            not isinstance(camera, str)
            or not _CAMERA.fullmatch(camera)
            or not isinstance(path, str)
            or path != str(expected)
        ):
            raise RecoveryError("RECOVERY_MANIFEST", "camera staging path is not exact")
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise RecoveryError("RECOVERY_MANIFEST", "duplicate staging paths")
    return manifest


def validate_append_only_snapshot(
    before: object, after: object, *, episode_index: int, camera_count: int,
    dataset_root: Path | str | None = None, require_extended: bool = False,
) -> dict:
    """Prove that one commit appended fresh immutable artifact files only."""
    before_version = _validate_snapshot(before)
    after_version = _validate_snapshot(after)
    if (
        type(episode_index) is not int
        or episode_index < 0
        or type(camera_count) is not int
        or camera_count < 0
        or type(require_extended) is not bool
        or require_extended and before_version != 2
    ):
        raise RecoveryError("RECOVERY_APPEND_IDENTITY", "append identity is invalid")
    if (
        before["total_episodes"] != episode_index
        or after["total_episodes"] != episode_index + 1
        or after["total_frames"] <= before["total_frames"]
    ):
        raise RecoveryError("RECOVERY_APPEND_IDENTITY", "dataset totals are not one append")

    expected_additions = {
        "data_parquet": 1,
        "committed_videos": camera_count,
        "episode_metadata": 1,
    }
    additions = {}
    for category, expected_count in expected_additions.items():
        old_files, new_files = before[category], after[category]
        changed = [
            relative for relative, details in old_files.items()
            if new_files.get(relative) != details
        ]
        if changed:
            raise RecoveryError(
                "RECOVERY_APPEND_MUTATED",
                f"previous {category} artifacts changed: {', '.join(sorted(changed))}",
            )
        added = sorted(set(new_files) - set(old_files))
        if (
            len(added) != expected_count
            or any(new_files[relative][0] <= 0 for relative in added)
        ):
            raise RecoveryError(
                "RECOVERY_APPEND_ARTIFACTS",
                f"unexpected new {category} artifact count or size",
            )
        additions[category] = added
    if before_version == 2:
        if after_version != 2 or dataset_root is None:
            raise RecoveryError(
                "RECOVERY_APPEND_EVIDENCE", "extended append evidence is unavailable",
            )
        old_files, new_files = (
            before["source_provenance"], after["source_provenance"],
        )
        changed = [
            relative for relative, details in old_files.items()
            if new_files.get(relative) != details
        ]
        expected_path = f"meta/source_provenance/episode-{episode_index:06d}.jsonl"
        added = sorted(set(new_files) - set(old_files))
        if (
            changed or added != [expected_path]
            or new_files[expected_path][0] <= 0
        ):
            raise RecoveryError(
                "RECOVERY_APPEND_MUTATED",
                "source provenance is not one immutable append",
            )
        additions["source_provenance"] = added
        quality = before["recording_quality"]
        quality_path = Path(dataset_root).resolve() / "meta" / "recording_quality.jsonl"
        if quality_path.is_symlink() or not quality_path.is_file():
            raise RecoveryError(
                "RECOVERY_APPEND_EVIDENCE", "recording quality log is unavailable",
            )
        with quality_path.open("rb") as file:
            prefix = file.read(quality["size"])
            appended = file.read(1)
        if (
            len(prefix) != quality["size"] or not appended
            or "sha256:" + hashlib.sha256(prefix).hexdigest() != quality["sha256"]
            or after["recording_quality"]["size"] <= quality["size"]
        ):
            raise RecoveryError(
                "RECOVERY_APPEND_MUTATED",
                "recording quality prefix changed or was not appended",
            )
    return {
        "episode_index": episode_index,
        "episode_frames": after["total_frames"] - before["total_frames"],
        "new_artifacts": additions,
    }


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
            value = decode_json_strict(file.read(), code, name)
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
    manifest = validate_staging_manifest_contract(
        manifest, root, episode_index=guard["episode_index"],
        run_id=guard["run_id"],
    )
    if manifest_path != run_root / guard["run_id"] / "staging_manifest.json":
        raise RecoveryError("RECOVERY_MANIFEST", "staging manifest path is not exact")
    cameras = manifest.get("camera_staging_dirs")
    episode_dir = f"episode-{guard['episode_index']:06d}"
    paths: list[Path] = []
    for camera, value in cameras.items():
        expected = root / "images" / f"observation.images.{camera}" / episode_dir
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
        value = decode_json_strict(line, "RECOVERY_EVENT", f"{path}:{index}")
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
            if not dataset_snapshot_unchanged(
                manifest["begin_snapshot"], dataset_snapshot(root),
            ):
                return _quarantine(guard_path, guard, "RECOVERY_SNAPSHOT_CHANGED", meta_fd)
            if not _same_directory(root, root_fd):
                return _quarantine(guard_path, guard, "RECOVERY_DIRECTORY_CHANGED", meta_fd)
            _remove_owned_staging(root_fd, manifest["camera_staging_dirs"], guard)
            if any(path.exists() or path.is_symlink() for path in staging_dirs):
                return _quarantine(guard_path, guard, "RECOVERY_STAGING_REMAINS", meta_fd)
            if not dataset_snapshot_unchanged(
                manifest["begin_snapshot"], dataset_snapshot(root),
            ):
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
