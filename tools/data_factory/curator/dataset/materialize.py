"""Read a frozen LeRobot source and create one verified hidden candidate."""

from __future__ import annotations

from dataclasses import dataclass
import multiprocessing
import os
from pathlib import Path
import platform
import re
import stat
import time
from typing import Any, Callable

import numpy as np

from ..core.errors import CuratorError
from ..core.filesystem import (
    OwnedDirectory,
    reject_symlink_components,
    remove_owned_directory,
    remove_owned_regular_file,
    write_json_exclusive,
)
from ..core.identity import assert_tree_identity, stable_tree_identity, tree_snapshot
from ..core.jsonio import SAFE_ID, canonical_digest, load_json
from ..profile.registry import ResolvedViewProfile, load_profile_assets
from ..profile.transform import apply_up_view, uint8_hwc
from .lineage import copy_source_provenance, write_candidate_lineage
from .publish import cleanup_candidate, commit_hidden_candidate
from .quality import write_derived_quality
from .source import open_source_dataset, validate_source_contract
from .verify import run_existing_validator, verify_derived_dataset


MATERIALIZATION_SCHEMA = "curator.candidate_materialization.v1"
_REPO_ID = re.compile(r"[^/\s]+/[^/\s]+\Z")
_CPU_COUNT = os.cpu_count() or 2
_IMAGE_WRITER_THREADS = min(8, max(2, _CPU_COUNT // 2))
_ENCODER_THREADS = min(4, max(1, _CPU_COUNT // 4))


def _require_spawn_parallel_encoding() -> None:
    """Select a fork-safe process context before LeRobot starts writer threads."""
    start_method = multiprocessing.get_start_method(allow_none=True)
    if start_method is None:
        try:
            multiprocessing.set_start_method("spawn")
            return
        except RuntimeError:
            start_method = multiprocessing.get_start_method(allow_none=True)
    if start_method != "spawn":
        raise CuratorError(
            "PARALLEL_ENCODING_REQUIRES_SPAWN",
            f"multiprocessing start method is {start_method!r}",
        )


def _repo_id(value: str, code: str) -> str:
    if not isinstance(value, str) or _REPO_ID.fullmatch(value) is None:
        raise CuratorError(code)
    return value


def _resolved_directory(path: str | Path, code: str) -> Path:
    reject_symlink_components(path, code)
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise CuratorError(code, str(exc)) from exc
    if not resolved.is_dir():
        raise CuratorError(code, "directory required")
    return resolved


def _new_output_path(path: str | Path) -> Path:
    target = Path(path)
    reject_symlink_components(target, "CANDIDATE_SYMLINK")
    if target.exists() or target.is_symlink() or target.name in {"", ".", ".."}:
        raise CuratorError("CANDIDATE_EXISTS", str(target))
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise CuratorError("CANDIDATE_PARENT", str(exc)) from exc
    if not parent.is_dir() or parent.is_symlink():
        raise CuratorError("CANDIDATE_PARENT", str(parent))
    return parent / target.name


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _reject_path_overlap(source: Path, output: Path, assets: tuple[Path, ...]) -> None:
    if _overlaps(source, output):
        raise CuratorError("PATH_OVERLAP", f"{source} <> {output}")
    for asset in assets:
        if _overlaps(source, asset) or _overlaps(output, asset):
            raise CuratorError("PATH_OVERLAP", f"dataset <> {asset}")


def _numpy(
    value: Any,
    *,
    dtype: np.dtype[Any],
    shape: tuple[int, ...],
    code: str,
) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    array = np.asarray(value)
    if array.dtype != dtype or array.shape != shape or not np.isfinite(array).all():
        raise CuratorError(code, f"{array.dtype}{array.shape}")
    return np.ascontiguousarray(array)


def _scalar_int(value: Any, code: str) -> int:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    array = np.asarray(value)
    if array.size != 1:
        raise CuratorError(code)
    item = array.reshape(-1)[0].item()
    if isinstance(item, bool) or not isinstance(item, (int, np.integer)):
        raise CuratorError(code)
    return int(item)


def _owner_value(run_id: str, candidate: Path, profile_digest: str) -> dict[str, Any]:
    return {
        "schema_version": "curator.temporary_owner.v1",
        "run_id": run_id,
        "candidate": str(candidate),
        "profile_digest": profile_digest,
    }


@dataclass(frozen=True)
class _Identity:
    device: int
    inode: int


def _identity(path: Path, mode: int, code: str) -> _Identity:
    try:
        details = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise CuratorError(code, str(path)) from exc
    if stat.S_IFMT(details.st_mode) != mode:
        raise CuratorError(code, str(path))
    return _Identity(details.st_dev, details.st_ino)


def _same_identity_at(
    directory_fd: int,
    name: str,
    expected: _Identity,
    mode: int,
) -> bool:
    try:
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_IFMT(details.st_mode) == mode and (
        details.st_dev,
        details.st_ino,
    ) == (expected.device, expected.inode)


def _cleanup_owned(
    temporary: Path,
    marker: Path,
    owner: dict[str, Any],
    temporary_identity: _Identity | None,
    marker_identity: _Identity,
) -> bool:
    """Remove only the exact temporary and marker created by this invocation."""
    if temporary.parent != marker.parent:
        return False
    parent_fd = -1
    try:
        parent_fd = os.open(
            temporary.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        if not _same_identity_at(parent_fd, marker.name, marker_identity, stat.S_IFREG):
            return False
        if load_json(marker, code="TEMP_OWNER") != owner:
            return False
        if not _same_identity_at(parent_fd, marker.name, marker_identity, stat.S_IFREG):
            return False
        if temporary_identity is not None:
            if not _same_identity_at(
                parent_fd, temporary.name, temporary_identity, stat.S_IFDIR
            ):
                return False
            remove_owned_directory(
                OwnedDirectory(
                    str(temporary.parent),
                    temporary.name,
                    temporary_identity.device,
                    temporary_identity.inode,
                )
            )
        elif temporary.exists() or temporary.is_symlink():
            return False
        remove_owned_regular_file(
            marker,
            device=marker_identity.device,
            inode=marker_identity.inode,
        )
        return True
    except (CuratorError, OSError):
        return False
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _remove_owner_marker(
    marker: Path, owner: dict[str, Any], identity: _Identity
) -> str:
    """Best-effort removal after candidate commit; the marker has no authority."""
    parent_fd = -1
    try:
        parent_fd = os.open(
            marker.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        if (
            not _same_identity_at(parent_fd, marker.name, identity, stat.S_IFREG)
            or load_json(marker, code="TEMP_OWNER") != owner
            or not _same_identity_at(parent_fd, marker.name, identity, stat.S_IFREG)
        ):
            return "RETAINED_IDENTITY_AMBIGUOUS"
        remove_owned_regular_file(
            marker,
            device=identity.device,
            inode=identity.inode,
        )
        return "REMOVED"
    except (CuratorError, OSError):
        return "RETAINED_IDENTITY_AMBIGUOUS"
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def materialize_candidate(
    source_root: str | Path,
    candidate_root: str | Path,
    resolved_profile: ResolvedViewProfile,
    *,
    run_id: str,
    source_repo_id: str = "local/curator-source",
    candidate_repo_id: str = "local/curator-derived",
    expected_source_snapshot: dict[str, list[int]] | None = None,
    expected_source_digest: str | None = None,
    frame_observer: Callable[..., None] | None = None,
    _ownership_handoff: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Materialize, fully verify, and durably commit one hidden candidate."""
    started_ns = time.perf_counter_ns()
    stage_started_ns = started_ns
    stage_seconds: dict[str, float] = {}

    def finish_stage(name: str) -> None:
        nonlocal stage_started_ns
        now_ns = time.perf_counter_ns()
        stage_seconds[name] = (now_ns - stage_started_ns) / 1_000_000_000
        stage_started_ns = now_ns

    if not isinstance(run_id, str) or SAFE_ID.fullmatch(run_id) is None:
        raise CuratorError("RUN_ID")
    if not isinstance(resolved_profile, ResolvedViewProfile):
        raise CuratorError("RESOLVED_VIEW_PROFILE")
    source_repo_id = _repo_id(source_repo_id, "SOURCE_REPO_ID")
    candidate_repo_id = _repo_id(candidate_repo_id, "CANDIDATE_REPO_ID")
    if source_repo_id == candidate_repo_id:
        raise CuratorError("REPO_ID_COLLISION")
    if (expected_source_snapshot is None) != (expected_source_digest is None):
        raise CuratorError("SOURCE_EXPECTATION")
    _require_spawn_parallel_encoding()

    profile = resolved_profile.profile
    if profile["physical_binding_status"] != "VERIFIED":
        raise CuratorError("PHYSICAL_BINDING_NOT_VERIFIED")
    source = _resolved_directory(source_root, "SOURCE_ROOT")
    if (source / "meta/quarantine.json").exists() or (
        source / "meta/quarantine.json"
    ).is_symlink():
        raise CuratorError("SOURCE_QUARANTINED")
    candidate = _new_output_path(candidate_root)
    assets = (resolved_profile.config_path, *resolved_profile.spec.asset_paths)
    _reject_path_overlap(source, candidate, assets)
    if expected_source_snapshot is None:
        source_snapshot, source_digest = stable_tree_identity(
            source,
            code="SOURCE_CHANGED_DURING_IDENTITY",
        )
    else:
        source_snapshot = expected_source_snapshot
        source_digest = expected_source_digest
        assert source_digest is not None
        assert_tree_identity(
            source,
            source_snapshot,
            source_digest,
            code="SOURCE_CHANGED_BEFORE_MATERIALIZATION",
        )
    keep_mask, background_plate = load_profile_assets(resolved_profile)
    source_dataset = open_source_dataset(source, source_repo_id)
    features = validate_source_contract(source_dataset, profile)
    if tree_snapshot(source) != source_snapshot:
        raise CuratorError("SOURCE_READER_MUTATION")
    finish_stage("preflight")

    temporary = candidate.parent / f".{candidate.name}.{run_id}.curator-tmp"
    marker = candidate.parent / f".{candidate.name}.{run_id}.curator-owner.json"
    if any(path.exists() or path.is_symlink() for path in (temporary, marker)):
        raise CuratorError("TEMP_EXISTS")
    owner = _owner_value(run_id, candidate, profile["profile_digest"])
    write_json_exclusive(marker, owner)
    marker_identity = _identity(marker, stat.S_IFREG, "TEMP_OWNER_IDENTITY")
    writer: Any = None
    temporary_identity: _Identity | None = None
    writer_closed = False
    candidate_committed = False
    committed: OwnedDirectory | None = None
    temporary_owned: OwnedDirectory | None = None
    candidate_snapshot: dict[str, list[int]] | None = None
    candidate_digest: str | None = None
    try:
        try:
            from lerobot import __version__ as lerobot_version
            from lerobot.configs.video import RGBEncoderConfig
            from lerobot.datasets.lerobot_dataset import LeRobotDataset

            writer = LeRobotDataset.create(
                repo_id=candidate_repo_id,
                fps=source_dataset.meta.fps,
                root=temporary,
                robot_type=source_dataset.meta.robot_type,
                features=features,
                use_videos=True,
                image_writer_threads=_IMAGE_WRITER_THREADS,
                encoder_threads=_ENCODER_THREADS,
                rgb_encoder=RGBEncoderConfig(vcodec="h264", preset="ultrafast", crf=23),
            )
            temporary_identity = _identity(
                temporary, stat.S_IFDIR, "TEMP_DIRECTORY_IDENTITY"
            )
        except Exception as exc:
            raise CuratorError("DERIVED_WRITER_CREATE", str(exc)) from exc

        current_episode = -1
        frame_in_episode = 0
        episode_task: str | None = None
        for index in range(len(source_dataset)):
            row = source_dataset[index]
            episode = _scalar_int(row["episode_index"], "SOURCE_EPISODE_INDEX")
            frame_index = _scalar_int(row["frame_index"], "SOURCE_FRAME_INDEX")
            if episode != current_episode:
                if current_episode >= 0:
                    writer.save_episode(parallel_encoding=True)
                if episode != current_episode + 1:
                    raise CuratorError("SOURCE_EPISODE_ORDER", str(episode))
                current_episode = episode
                frame_in_episode = 0
                episode_task = None
            if frame_index != frame_in_episode:
                raise CuratorError(
                    "SOURCE_FRAME_ORDER", f"episode={episode} frame={frame_index}"
                )
            state = _numpy(
                row["observation.state"],
                dtype=np.dtype("float32"),
                shape=(7,),
                code="SOURCE_STATE",
            )
            action = _numpy(
                row["action"],
                dtype=np.dtype("float32"),
                shape=(7,),
                code="SOURCE_ACTION",
            )
            raw_up = uint8_hwc(
                row["observation.images.up"],
                width=profile["width"],
                height=profile["height"],
                code="SOURCE_UP_FRAME",
            )
            raw_wrist = uint8_hwc(
                row["observation.images.wrist"],
                width=profile["width"],
                height=profile["height"],
                code="SOURCE_WRIST_FRAME",
            )
            task = row.get("task")
            if not isinstance(task, str) or not task or task.strip() != task:
                raise CuratorError("SOURCE_TASK")
            if episode_task is None:
                episode_task = task
            elif task != episode_task:
                raise CuratorError("SOURCE_EPISODE_TASK", f"episode={episode}")
            writer.add_frame(
                {
                    "observation.state": state,
                    "action": action,
                    "observation.images.up": apply_up_view(
                        raw_up, keep_mask, background_plate
                    ),
                    "observation.images.wrist": raw_wrist,
                    "task": task,
                }
            )
            frame_in_episode += 1
        writer.save_episode(parallel_encoding=True)
        writer.finalize()
        writer_closed = True
        writer = None
        finish_stage("materialization")

        source_provenance = copy_source_provenance(
            source,
            temporary,
            source_dataset.meta.total_episodes,
        )
        verification = verify_derived_dataset(
            source_dataset,
            temporary,
            derived_repo_id=candidate_repo_id,
            profile=profile,
            keep_mask=keep_mask,
            background_plate=background_plate,
            frame_observer=frame_observer,
        )
        finish_stage("post_write_verification")
        quality_lineage = write_derived_quality(
            source,
            temporary,
            verification,
            profile["profile_digest"],
        )
        dataset_lineage = write_candidate_lineage(
            temporary,
            source=source,
            source_repo_id=source_repo_id,
            source_digest=source_digest,
            candidate_repo_id=candidate_repo_id,
            profile=profile,
            verification=verification,
            source_provenance=source_provenance,
        )
        candidate_snapshot, candidate_digest = stable_tree_identity(
            temporary,
            code="CANDIDATE_CHANGED_BEFORE_EXISTING_VALIDATOR",
        )
        validator = run_existing_validator(temporary, candidate_repo_id)
        assert_tree_identity(
            temporary,
            candidate_snapshot,
            candidate_digest,
            code="EXISTING_VALIDATOR_MUTATED_CANDIDATE",
        )
        finish_stage("quality_and_existing_validator")
        assert_tree_identity(
            source,
            source_snapshot,
            source_digest,
            code="SOURCE_CHANGED_DURING_MATERIALIZATION",
        )
        finish_stage("source_revalidation")

        temporary_owned = OwnedDirectory.capture(temporary)
        try:
            committed = commit_hidden_candidate(
                temporary_owned,
                candidate,
                expected_snapshot=candidate_snapshot,
            )
        except BaseException:
            try:
                recovered = OwnedDirectory.capture(candidate)
            except (CuratorError, OSError):
                recovered = None
            if recovered is not None and (
                recovered.device,
                recovered.inode,
            ) == (
                temporary_owned.device,
                temporary_owned.inode,
            ):
                committed = recovered
                candidate_committed = True
            raise
        candidate_committed = True
        if (committed.device, committed.inode) != (
            temporary_owned.device,
            temporary_owned.inode,
        ):
            raise CuratorError("CANDIDATE_COMMIT_IDENTITY")
        finish_stage("hidden_candidate_commit")
        marker_cleanup = _remove_owner_marker(marker, owner, marker_identity)
        finish_stage("owner_marker_cleanup")

        total_seconds = (time.perf_counter_ns() - started_ns) / 1_000_000_000
        frames = len(source_dataset)
        result = {
            "schema_version": MATERIALIZATION_SCHEMA,
            "status": "PASS",
            "run_id": run_id,
            "source": {
                "root": str(source),
                "repo_id": source_repo_id,
                "dataset_digest": source_digest,
            },
            "candidate": {
                "root": str(candidate),
                "repo_id": candidate_repo_id,
                "dataset_digest": candidate_digest,
                "ownership": committed.as_json(),
            },
            "runtime": {
                "python": platform.python_version(),
                "lerobot": lerobot_version,
            },
            "encoder": {
                "vcodec": "h264",
                "preset": "ultrafast",
                "crf": 23,
                "parallel_cameras": True,
                "multiprocessing_start_method": "spawn",
                "image_writer_threads": _IMAGE_WRITER_THREADS,
                "encoder_threads_per_camera": _ENCODER_THREADS,
            },
            "performance_observation": {
                "scope": "WALL_TIME_BEFORE_MATERIALIZATION_RESULT",
                "stage_seconds": stage_seconds,
                "total_seconds": total_seconds,
                "materialization_frames_per_second": (
                    frames / max(stage_seconds["materialization"], 1e-9)
                ),
                "end_to_end_frames_per_second": frames / max(total_seconds, 1e-9),
                "authoritative_threshold": False,
            },
            "hidden_candidate_commit": {
                "state": "COMMITTED_DURABLE",
                "rename_noreplace": True,
                "tree_fsync": True,
                "parent_fsync": True,
                "owner_marker_cleanup": marker_cleanup,
            },
            "profile_digest": profile["profile_digest"],
            "collection_camera_profile_binding": {
                "digest": profile["collection_camera_profile_digest"],
                "status": profile["collection_camera_profile_binding_status"],
            },
            "mask_sha256": profile["mask_sha256"],
            "background_plate_sha256": profile["background_plate_sha256"],
            "candidate_authority": "MACHINE_VERIFIED_NOT_HUMAN_APPROVED",
            "verification": verification,
            "recording_quality_lineage": quality_lineage,
            "dataset_lineage": dataset_lineage,
            "existing_validator": validator,
            "training_authority": False,
            "approval_inherited": False,
            "quarantine_inherited": False,
        }
        result["materialization_digest"] = canonical_digest(result)
        if _ownership_handoff is not None:
            _ownership_handoff(result)
        return result
    except BaseException:
        if not candidate_committed and temporary_owned is not None:
            try:
                recovered = OwnedDirectory.capture(candidate)
            except (CuratorError, OSError):
                recovered = None
            if recovered is not None and (
                recovered.device,
                recovered.inode,
            ) == (
                temporary_owned.device,
                temporary_owned.inode,
            ):
                committed = recovered
                candidate_committed = True
        cleanup_safe = writer_closed
        if writer is not None:
            try:
                writer.finalize()
                cleanup_safe = True
            except Exception:
                cleanup_safe = False
            writer = None
        if (
            candidate_committed
            and committed is not None
            and candidate_digest is not None
        ):
            candidate_removed = False
            try:
                cleanup_candidate(committed, candidate_digest)
                candidate_removed = True
            except BaseException:
                pass
            if candidate_removed:
                _cleanup_owned(temporary, marker, owner, None, marker_identity)
        elif not candidate_committed and cleanup_safe:
            _cleanup_owned(
                temporary,
                marker,
                owner,
                temporary_identity,
                marker_identity,
            )
        elif not candidate_committed and temporary_identity is None:
            _cleanup_owned(temporary, marker, owner, None, marker_identity)
        raise


__all__ = ["MATERIALIZATION_SCHEMA", "materialize_candidate"]
