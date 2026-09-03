"""Owned candidate durability, cleanup, and atomic no-replace publication."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

from ..core.errors import CuratorError
from ..core.filesystem import (
    OwnedDirectory,
    fsync_directory,
    remove_owned_directory,
    rename_noreplace_at,
)
from ..core.identity import stable_tree_identity, tree_snapshot
from ..core.jsonio import DIGEST


def candidate_action_path(
    owned: OwnedDirectory,
    expected_digest: str,
    action: str,
) -> Path:
    """Derive a crash-recoverable stage name from immutable candidate evidence."""
    if action not in {"publish", "reject"} or DIGEST.fullmatch(expected_digest) is None:
        raise CuratorError("CANDIDATE_ACTION_STAGE")
    binding = "\0".join(
        (
            "curator.candidate_action.v1",
            owned.parent,
            owned.name,
            str(owned.device),
            str(owned.inode),
            expected_digest,
            action,
        )
    )
    suffix = hashlib.sha256(binding.encode()).hexdigest()[:32]
    return Path(owned.parent) / f".curator-{action}-{suffix}.stage"


def fsync_candidate_tree(
    owned: OwnedDirectory,
    *,
    expected_snapshot: dict[str, list[int]] | None = None,
) -> None:
    """Fsync a closed candidate without following links or loading file bodies."""
    parent_fd = owned.parent_fd()
    try:
        owned.verify_at(parent_fd)
        root = owned.path
        snapshot = (
            tree_snapshot(root) if expected_snapshot is None else expected_snapshot
        )
        directories = [root]
        for relative in sorted(snapshot):
            path = root / relative.rstrip("/")
            if relative.endswith("/"):
                directories.append(path)
                continue
            try:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise CuratorError("CANDIDATE_FSYNC", str(path))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except CuratorError:
                raise
            except OSError as exc:
                raise CuratorError("CANDIDATE_FSYNC", f"{path}: {exc}") from exc
        for directory in sorted(
            directories,
            key=lambda item: len(item.relative_to(root).parts),
            reverse=True,
        ):
            fsync_directory(directory)
        owned.verify_at(parent_fd)
        if tree_snapshot(root) != snapshot:
            raise CuratorError("CANDIDATE_CHANGED_DURING_FSYNC")
    finally:
        os.close(parent_fd)


def candidate_identity(
    owned: OwnedDirectory,
) -> tuple[dict[str, list[int]], str]:
    parent_fd = owned.parent_fd()
    try:
        owned.verify_at(parent_fd)
        snapshot, digest = stable_tree_identity(owned.path, code="CANDIDATE_CHANGED")
        owned.verify_at(parent_fd)
    finally:
        os.close(parent_fd)
    return snapshot, digest


def verify_owned_candidate(owned: OwnedDirectory, expected_digest: str) -> str:
    _snapshot, digest = candidate_identity(owned)
    if digest != expected_digest:
        raise CuratorError("CANDIDATE_DIGEST_CHANGED")
    return digest


def commit_hidden_candidate(
    owned_temporary: OwnedDirectory,
    candidate_path: str | Path,
    *,
    expected_snapshot: dict[str, list[int]],
) -> OwnedDirectory:
    """Commit a finalized temporary tree as the run-owned hidden candidate."""
    fsync_candidate_tree(owned_temporary, expected_snapshot=expected_snapshot)
    rename_noreplace_at(owned_temporary, candidate_path)
    return OwnedDirectory.capture(candidate_path)


def cleanup_candidate(owned: OwnedDirectory, expected_digest: str) -> None:
    stage_path = candidate_action_path(owned, expected_digest, "reject")
    staged = OwnedDirectory(
        owned.parent,
        stage_path.name,
        owned.device,
        owned.inode,
    )
    source_exists = owned.path.exists() or owned.path.is_symlink()
    stage_exists = stage_path.exists() or stage_path.is_symlink()
    if source_exists:
        verify_owned_candidate(owned, expected_digest)
    elif stage_exists:
        parent_fd = staged.parent_fd()
        try:
            staged.verify_at(parent_fd)
        finally:
            os.close(parent_fd)
    else:
        fsync_directory(owned.parent)
        return
    remove_owned_directory(owned, staging_path=stage_path)
    fsync_directory(owned.parent)


def publish_candidate(
    owned: OwnedDirectory,
    output: str | Path,
    expected_digest: str,
    *,
    verified_snapshot: dict[str, list[int]] | None = None,
) -> str:
    """Revalidate, fsync, and atomically publish the exact human-reviewed tree."""
    staging_path = candidate_action_path(owned, expected_digest, "publish")
    staged = OwnedDirectory(
        owned.parent,
        staging_path.name,
        owned.device,
        owned.inode,
    )
    source_exists = owned.path.exists() or owned.path.is_symlink()
    stage_exists = staging_path.exists() or staging_path.is_symlink()
    if source_exists and stage_exists:
        raise CuratorError("CANDIDATE_ACTION_DUPLICATED")
    if not source_exists and not stage_exists:
        raise CuratorError("CANDIDATE_ACTION_MISSING")
    current = owned if source_exists else staged
    observed_snapshot = tree_snapshot(current.path)
    baseline = observed_snapshot if verified_snapshot is None else verified_snapshot
    if observed_snapshot != baseline:
        raise CuratorError("CANDIDATE_CHANGED_BEFORE_PUBLISH")
    fsync_candidate_tree(current, expected_snapshot=baseline)
    final_snapshot, digest = candidate_identity(current)
    if final_snapshot != baseline or digest != expected_digest:
        raise CuratorError("CANDIDATE_DIGEST_CHANGED")
    try:
        if source_exists:
            rename_noreplace_at(owned, staging_path)
        verify_owned_candidate(staged, expected_digest)
        rename_noreplace_at(staged, output)
    except BaseException:
        output_path = Path(output)
        if _has_owned_identity(output_path, owned):
            # The exact reviewed inode reached its final name. The workflow can
            # recover a missing parent fsync/receipt without re-prompting.
            raise
        if _has_owned_identity(staging_path, staged) and not (
            owned.path.exists() or owned.path.is_symlink()
        ):
            try:
                rename_noreplace_at(staged, owned.path)
            except BaseException:
                pass
        raise
    return expected_digest


def _has_owned_identity(path: Path, owned: OwnedDirectory) -> bool:
    try:
        details = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return (
        not path.is_symlink()
        and stat.S_ISDIR(details.st_mode)
        and (details.st_dev, details.st_ino) == (owned.device, owned.inode)
    )


__all__ = [
    "candidate_action_path",
    "cleanup_candidate",
    "candidate_identity",
    "commit_hidden_candidate",
    "fsync_candidate_tree",
    "fsync_directory",
    "publish_candidate",
    "verify_owned_candidate",
]
