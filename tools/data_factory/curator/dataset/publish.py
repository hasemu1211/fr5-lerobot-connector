"""Identity-revalidated candidate cleanup and atomic publication."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import stat
from ..core.jsonio import CuratorError, assert_tree_identity, rename_noreplace, stable_tree_identity

def publish_candidate(candidate: str | Path, output: str | Path, expected_digest: str) -> str:
    source, target = Path(candidate), Path(output)
    snapshot, digest = stable_tree_identity(source, code="CANDIDATE_CHANGED")
    if digest != expected_digest:
        raise CuratorError("CANDIDATE_DIGEST_CHANGED")
    assert_tree_identity(source, snapshot, digest, code="CANDIDATE_CHANGED")
    rename_noreplace(source, target, exists_code="OUTPUT_EXISTS", failure_code="OUTPUT_PUBLISH")
    fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(fd)
    finally: os.close(fd)
    return digest

def cleanup_candidate(candidate: str | Path, expected_digest: str) -> None:
    source = Path(candidate)
    snapshot, digest = stable_tree_identity(source, code="CANDIDATE_CHANGED")
    if digest != expected_digest or source.is_symlink() or not stat.S_ISDIR(source.stat().st_mode):
        raise CuratorError("CANDIDATE_CLEANUP_IDENTITY")
    assert_tree_identity(source, snapshot, digest, code="CANDIDATE_CHANGED")
    shutil.rmtree(source)

__all__ = ["cleanup_candidate", "publish_candidate"]
