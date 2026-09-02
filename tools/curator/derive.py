"""Read-only LeRobot v3 source to isolated, atomically published derived root."""

from __future__ import annotations

from contextlib import suppress
import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
from typing import Any

import numpy as np

from tools.curator.approval import verify_approval
from tools.curator.contracts import (
    SAFE_ID,
    CuratorError,
    assert_tree_identity,
    canonical_digest,
    file_sha256,
    load_json,
    reject_symlink_components,
    rename_noreplace,
    tree_identity,
    tree_snapshot,
    write_json_atomic,
    write_json_exclusive,
)
from tools.curator.up_view import apply_up_view, uint8_hwc
from tools.curator.verify import (
    load_profile_assets,
    open_source_dataset,
    verify_derived_dataset,
    verify_review_bundle,
)


RECEIPT_SCHEMA = "curator.derive_receipt.v1"
_REPO_ID = re.compile(r"[^/\s]+/[^/\s]+\Z")
_CUSTOM_FEATURES = {
    "observation.state",
    "action",
    "observation.images.up",
    "observation.images.wrist",
}


def _repo_id(value: str, code: str) -> str:
    if not isinstance(value, str) or _REPO_ID.fullmatch(value) is None:
        raise CuratorError(code)
    return value


def _resolved_directory(path: str | Path, code: str) -> Path:
    source = Path(path)
    reject_symlink_components(source, code)
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise CuratorError(code, str(exc)) from exc
    if not resolved.is_dir():
        raise CuratorError(code, "directory required")
    return resolved


def _output_path(path: str | Path) -> Path:
    target = Path(path)
    reject_symlink_components(target, "OUTPUT_SYMLINK")
    if target.is_symlink() or target.exists():
        raise CuratorError("OUTPUT_EXISTS", str(target))
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise CuratorError("OUTPUT_PARENT", str(exc)) from exc
    if parent.is_symlink() or not parent.is_dir() or not target.name or target.name in {".", ".."}:
        raise CuratorError("OUTPUT_PARENT", str(parent))
    return parent / target.name


def _no_overlap(source: Path, output: Path, run_dir: Path, bundle: Path) -> None:
    pairs = (
        (source, output), (source, run_dir), (output, run_dir),
        (source, bundle), (output, bundle), (run_dir, bundle),
    )
    for left, right in pairs:
        if left == right or left in right.parents or right in left.parents:
            raise CuratorError("PATH_OVERLAP", f"{left} <> {right}")


def _validate_source_contract(dataset: Any, profile: dict[str, Any]) -> dict[str, Any]:
    try:
        from lerobot.utils.constants import DEFAULT_FEATURES
        from tools.fr5_dataset_schema import FEATURE_NAMES
    except ImportError as exc:
        raise CuratorError("LEROBOT_IMPORT", str(exc)) from exc
    features = dataset.meta.features
    custom = set(features) - set(DEFAULT_FEATURES)
    if custom != _CUSTOM_FEATURES:
        raise CuratorError("SOURCE_FEATURE_SET", str(sorted(custom)))
    for key in ("observation.state", "action"):
        feature = features[key]
        if (
            feature.get("dtype") != "float32"
            or list(feature.get("shape", [])) != [7]
            or feature.get("names") != FEATURE_NAMES
        ):
            raise CuratorError("SOURCE_NUMERIC_FEATURE", key)
    expected_shape = [profile["height"], profile["width"], 3]
    for key in ("observation.images.up", "observation.images.wrist"):
        feature = features[key]
        if (
            feature.get("dtype") != "video"
            or list(feature.get("shape", [])) != expected_shape
            or feature.get("names") != ["height", "width", "channels"]
        ):
            raise CuratorError("SOURCE_VIDEO_FEATURE", key)
    if (
        (profile["width"], profile["height"]) != (640, 480)
        or type(dataset.meta.fps) is not int
        or dataset.meta.fps != 30
        or dataset.meta.robot_type != "fr5_ros2"
        or dataset.meta.total_episodes <= 0
        or len(dataset) <= 0
    ):
        raise CuratorError("SOURCE_DATASET_CONTRACT")
    return copy.deepcopy({key: features[key] for key in _CUSTOM_FEATURES})


def _numpy(value: Any, *, dtype: np.dtype[Any], shape: tuple[int, ...], code: str) -> np.ndarray:
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


def _copy_source_provenance(source: Path, output: Path, episodes: int) -> None:
    provenance = source / "meta" / "source_provenance"
    if provenance.is_symlink() or not provenance.is_dir():
        raise CuratorError("SOURCE_PROVENANCE_EVIDENCE")
    expected = {f"episode-{index:06d}.jsonl" for index in range(episodes)}
    actual = {path.name for path in provenance.iterdir()}
    if actual != expected or any(path.is_symlink() or not path.is_file() for path in provenance.iterdir()):
        raise CuratorError("SOURCE_PROVENANCE_EVIDENCE")
    target = output / "meta" / "source_provenance"
    target.mkdir(mode=0o700)
    for name in sorted(expected):
        shutil.copyfile(provenance / name, target / name)


def _strict_json_object(text: str, code: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise CuratorError(code, f"duplicate key: {key}")
            value[key] = item
        return value

    def nonfinite(value: str) -> None:
        raise CuratorError(code, f"non-finite: {value}")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except CuratorError:
        raise
    except json.JSONDecodeError as exc:
        raise CuratorError(code, str(exc)) from exc
    if not isinstance(value, dict):
        raise CuratorError(code, "object required")
    return value


def _write_derived_quality(
    source: Path,
    output: Path,
    verification: dict[str, Any],
    profile_digest: str,
) -> dict[str, Any]:
    source_path = source / "meta" / "recording_quality.jsonl"
    if source_path.is_symlink() or not source_path.is_file():
        raise CuratorError("SOURCE_QUALITY_EVIDENCE")
    try:
        source_lines = source_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CuratorError("SOURCE_QUALITY_EVIDENCE", str(exc)) from exc
    metrics = {
        item["episode_index"]: item["cameras"]
        for item in verification["derived_image_metrics"]
    }
    if len(source_lines) != verification["episodes"] or set(metrics) != set(range(verification["episodes"])):
        raise CuratorError("SOURCE_QUALITY_EVIDENCE", "episode mismatch")
    derived_lines = []
    seen: set[int] = set()
    source_sha256 = file_sha256(source_path)
    for line in source_lines:
        quality = _strict_json_object(line, "SOURCE_QUALITY_EVIDENCE")
        episode = quality.get("episode_index")
        cameras = quality.get("cameras")
        if (
            type(episode) is not int
            or episode in seen
            or episode not in metrics
            or not isinstance(cameras, dict)
            or set(cameras) != {"up", "wrist"}
            or "curator_lineage" in quality
            or not isinstance(quality.get("image_quality_warnings"), list)
            or any(not isinstance(item, str) for item in quality["image_quality_warnings"])
        ):
            raise CuratorError("SOURCE_QUALITY_EVIDENCE", str(episode))
        for camera in ("up", "wrist"):
            if not isinstance(cameras[camera], dict):
                raise CuratorError("SOURCE_QUALITY_EVIDENCE", camera)
            cameras[camera].update(metrics[episode][camera])
        quality["image_quality_warnings"] = _derived_image_quality_warnings(metrics[episode])
        quality["curator_lineage"] = {
            "schema_version": "curator.derived_recording_quality_lineage.v1",
            "source_recording_quality_sha256": source_sha256,
            "source_timing_evidence": "PRESERVED",
            "derived_pixel_metrics": "RECOMPUTED",
            "source_image_quality_warnings": "REPLACED_FROM_DERIVED_METRICS",
            "profile_digest": profile_digest,
            "training_authority": False,
        }
        derived_lines.append(json.dumps(quality, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
        seen.add(episode)
    if seen != set(metrics):
        raise CuratorError("SOURCE_QUALITY_EVIDENCE", "missing episode")
    target = output / "meta" / "recording_quality.jsonl"
    try:
        with target.open("x", encoding="utf-8") as stream:
            stream.write("\n".join(derived_lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise CuratorError("DERIVED_QUALITY_WRITE", str(exc)) from exc
    return {
        "source_recording_quality_sha256": source_sha256,
        "derived_recording_quality_sha256": file_sha256(target),
        "source_timing_evidence": "PRESERVED",
        "derived_pixel_metrics": "RECOMPUTED",
    }


def _derived_image_quality_warnings(cameras: dict[str, dict[str, float]]) -> list[str]:
    warnings: list[str] = []
    for camera in ("up", "wrist"):
        metrics = cameras[camera]
        if metrics["color_delta_mean"] < 1.0:
            warnings.append(
                f"{camera} image appears monochrome (color delta {metrics['color_delta_mean']:.2f})"
            )
        if not 20 <= metrics["brightness_mean"] <= 235:
            warnings.append(
                f"{camera} brightness {metrics['brightness_mean']:.1f} outside diagnostic range"
            )
        if metrics["clipping_mean"] > 0.20:
            warnings.append(
                f"{camera} clipping {metrics['clipping_mean']:.1%} exceeds diagnostic threshold"
            )
        if metrics["sharpness_median"] < 20:
            warnings.append(
                f"{camera} sharpness {metrics['sharpness_median']:.1f} below diagnostic threshold"
            )
    return warnings


def _run_existing_validator(root: Path, repo_id: str) -> dict[str, Any]:
    repository = Path(__file__).resolve().parents[2]
    command = [
        sys.executable,
        str(repository / "tools" / "validate_lerobot_dataset.py"),
        str(root),
        "--repo-id",
        repo_id,
        "--expected-fps",
        "30",
        "--skip-decoded-image-diagnostics",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = "\n".join((result.stdout + "\n" + result.stderr).splitlines()[-20:])
        raise CuratorError("EXISTING_VALIDATOR_FAILED", detail)
    lines = [line for line in result.stdout.splitlines() if line]
    return {
        "status": "PASS",
        "returncode": result.returncode,
        "stdout_sha256": "sha256:" + hashlib.sha256(result.stdout.encode()).hexdigest(),
        "summary": lines[-1] if lines else "PASS",
    }


def _owner_value(run_id: str, output: Path, profile_digest: str) -> dict[str, Any]:
    return {
        "schema_version": "curator.temporary_owner.v1",
        "run_id": run_id,
        "output": str(output),
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


def _same_identity(path: Path, expected: _Identity, mode: int) -> bool:
    try:
        details = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_IFMT(details.st_mode) == mode
        and (details.st_dev, details.st_ino) == (expected.device, expected.inode)
    )


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
    return (
        stat.S_IFMT(details.st_mode) == mode
        and (details.st_dev, details.st_ino) == (expected.device, expected.inode)
    )


def _cleanup_owned(
    temporary: Path,
    marker: Path,
    owner: dict[str, Any],
    temporary_identity: _Identity | None,
    marker_identity: _Identity,
) -> bool:
    """Best-effort cleanup anchored to the captured sibling parent directory."""
    if (
        temporary_identity is None
        or temporary.parent != marker.parent
        or not getattr(shutil.rmtree, "avoids_symlink_attacks", False)
    ):
        return False
    parent_fd = -1
    marker_fd = -1
    try:
        parent_fd = os.open(
            temporary.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        if (
            not _same_identity_at(parent_fd, marker.name, marker_identity, stat.S_IFREG)
            or not _same_identity_at(parent_fd, temporary.name, temporary_identity, stat.S_IFDIR)
        ):
            return False
        marker_fd = os.open(
            marker.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        marker_details = os.fstat(marker_fd)
        if (
            not stat.S_ISREG(marker_details.st_mode)
            or (marker_details.st_dev, marker_details.st_ino)
            != (marker_identity.device, marker_identity.inode)
        ):
            return False
        with os.fdopen(marker_fd, "r", encoding="utf-8") as stream:
            marker_fd = -1
            current = _strict_json_object(stream.read(), "TEMP_OWNER")
        if (
            current != owner
            or not _same_identity_at(parent_fd, marker.name, marker_identity, stat.S_IFREG)
            or not _same_identity_at(parent_fd, temporary.name, temporary_identity, stat.S_IFDIR)
        ):
            return False
        shutil.rmtree(temporary.name, dir_fd=parent_fd)
        if not _same_identity_at(parent_fd, marker.name, marker_identity, stat.S_IFREG):
            return False
        os.unlink(marker.name, dir_fd=parent_fd)
        return True
    except (CuratorError, OSError, UnicodeError):
        return False
    finally:
        if marker_fd >= 0:
            os.close(marker_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _publish(temporary: Path, output: Path) -> None:
    """Commit the no-clobber rename; durability is recorded separately."""
    if output.exists() or output.is_symlink():
        raise CuratorError("OUTPUT_EXISTS", str(output))
    rename_noreplace(
        temporary,
        output,
        exists_code="OUTPUT_EXISTS",
        failure_code="OUTPUT_PUBLISH",
    )


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _fsync_tree(root: Path) -> None:
    """Make every finalized file and nested directory durable before rename."""
    snapshot = tree_snapshot(root)
    directories = [root]
    try:
        for relative in sorted(snapshot):
            path = root / relative.rstrip("/")
            if relative.endswith("/"):
                directories.append(path)
                continue
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise CuratorError("DERIVED_TREE_FSYNC", str(path))
                os.fsync(fd)
            finally:
                os.close(fd)
        for directory in sorted(
            directories,
            key=lambda item: len(item.relative_to(root).parts),
            reverse=True,
        ):
            _fsync_directory(directory)
    except CuratorError:
        raise
    except OSError as exc:
        raise CuratorError("DERIVED_TREE_FSYNC", str(exc)) from exc
    if tree_snapshot(root) != snapshot:
        raise CuratorError("DERIVED_TREE_FSYNC", "tree changed during fsync")


def derive_dataset(
    source_root: str | Path,
    output_root: str | Path,
    profile_request: str | Path,
    approval_path: str | Path,
    *,
    run_dir: str | Path,
    run_id: str,
    source_repo_id: str = "local/curator-source",
    output_repo_id: str = "local/curator-derived",
) -> dict[str, Any]:
    """Materialize, verify, and atomically publish one isolated v3 dataset."""
    if not isinstance(run_id, str) or SAFE_ID.fullmatch(run_id) is None:
        raise CuratorError("RUN_ID")
    source_repo_id = _repo_id(source_repo_id, "SOURCE_REPO_ID")
    output_repo_id = _repo_id(output_repo_id, "OUTPUT_REPO_ID")
    approval, profile, review = verify_approval(profile_request, approval_path)
    request, current_profile, current_review = verify_review_bundle(profile_request)
    if current_profile != profile or current_review != review:
        raise CuratorError("PROFILE_REVIEW_CHANGED")
    approval_artifact_sha256 = file_sha256(request.approval_path)
    source = _resolved_directory(source_root, "SOURCE_ROOT")
    if (source / "meta" / "quarantine.json").exists() or (source / "meta" / "quarantine.json").is_symlink():
        raise CuratorError("SOURCE_QUARANTINED")
    output = _output_path(output_root)
    run = Path(run_dir)
    reject_symlink_components(run, "RUN_SYMLINK")
    if run.is_symlink() or run.exists():
        raise CuratorError("RUN_EXISTS", str(run))
    run_parent = run.parent.resolve(strict=False)
    run = run_parent / run.name
    _no_overlap(source, output, run, request.review_bundle_path)
    source_before = tree_snapshot(source)
    source_digest, _source_files = tree_identity(source)
    if tree_snapshot(source) != source_before:
        raise CuratorError("SOURCE_CHANGED_DURING_IDENTITY")
    if source_digest != review["reference_source_dataset_digest"]:
        raise CuratorError("SOURCE_REVIEW_MISMATCH")
    keep_mask, background_plate = load_profile_assets(request, profile, review)
    source_dataset = open_source_dataset(source, source_repo_id)
    features = _validate_source_contract(source_dataset, profile)
    if tree_snapshot(source) != source_before:
        raise CuratorError("SOURCE_READER_MUTATION")
    run_parent.mkdir(parents=True, exist_ok=True)
    run.mkdir(mode=0o700)
    write_json_atomic(
        run / "owner.json",
        {"schema_version": "curator.run_owner.v1", "run_id": run_id, "output": str(output)},
    )

    temporary = output.parent / f".{output.name}.{run_id}.curator-tmp"
    marker = output.parent / f".{output.name}.{run_id}.curator-owner.json"
    if temporary.exists() or temporary.is_symlink() or marker.exists() or marker.is_symlink():
        raise CuratorError("TEMP_EXISTS")
    owner = _owner_value(run_id, output, profile["profile_digest"])
    write_json_exclusive(marker, owner)
    marker_identity = _identity(marker, stat.S_IFREG, "TEMP_OWNER_IDENTITY")
    writer = None
    temporary_identity: _Identity | None = None
    writer_closed = False
    published = False
    publication_state = "UNPUBLISHED"
    try:
        try:
            from lerobot import __version__ as lerobot_version
            from lerobot.configs.video import RGBEncoderConfig
            from lerobot.datasets.lerobot_dataset import LeRobotDataset

            writer = LeRobotDataset.create(
                repo_id=output_repo_id,
                fps=source_dataset.meta.fps,
                root=temporary,
                robot_type=source_dataset.meta.robot_type,
                features=features,
                use_videos=True,
                image_writer_threads=2,
                rgb_encoder=RGBEncoderConfig(vcodec="h264", preset="ultrafast", crf=23),
            )
            temporary_identity = _identity(temporary, stat.S_IFDIR, "TEMP_DIRECTORY_IDENTITY")
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
                    writer.save_episode(parallel_encoding=False)
                if episode != current_episode + 1:
                    raise CuratorError("SOURCE_EPISODE_ORDER", str(episode))
                current_episode = episode
                frame_in_episode = 0
                episode_task = None
            if frame_index != frame_in_episode:
                raise CuratorError("SOURCE_FRAME_ORDER", f"episode={episode} frame={frame_index}")
            state = _numpy(row["observation.state"], dtype=np.dtype("float32"), shape=(7,), code="SOURCE_STATE")
            action = _numpy(row["action"], dtype=np.dtype("float32"), shape=(7,), code="SOURCE_ACTION")
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
            writer.add_frame({
                "observation.state": state,
                "action": action,
                "observation.images.up": apply_up_view(raw_up, keep_mask, background_plate),
                "observation.images.wrist": raw_wrist,
                "task": task,
            })
            frame_in_episode += 1
        writer.save_episode(parallel_encoding=False)
        writer.finalize()
        writer_closed = True
        writer = None
        _copy_source_provenance(source, temporary, source_dataset.meta.total_episodes)
        verification = verify_derived_dataset(
            source_dataset,
            temporary,
            derived_repo_id=output_repo_id,
            profile=profile,
            keep_mask=keep_mask,
            background_plate=background_plate,
        )
        quality_lineage = _write_derived_quality(
            source, temporary, verification, profile["profile_digest"],
        )
        validator = _run_existing_validator(temporary, output_repo_id)
        assert_tree_identity(
            source,
            source_before,
            source_digest,
            code="SOURCE_CHANGED_DURING_DERIVE",
        )
        current_approval, current_profile, current_review = verify_approval(
            profile_request, approval_path,
        )
        if (
            current_approval != approval
            or current_profile != profile
            or current_review != review
            or file_sha256(request.approval_path) != approval_artifact_sha256
        ):
            raise CuratorError("APPROVAL_BUNDLE_CHANGED_BEFORE_PUBLISH")
        _fsync_tree(temporary)
        _publish(temporary, output)
        published = True
        publication_state = "COMMITTED_PARENT_FSYNC_PENDING"
        try:
            _fsync_directory(output.parent)
        except Exception as fsync_error:
            publication_state = "COMMITTED_PARENT_FSYNC_FAILED"
            raise CuratorError(
                "OUTPUT_COMMITTED_PARENT_FSYNC_FAILED", str(output),
            ) from fsync_error
        publication_state = "COMMITTED_DURABLE_RECEIPT_PENDING"
        if not _same_identity(marker, marker_identity, stat.S_IFREG):
            publication_state = "COMMITTED_OWNER_MARKER_AMBIGUOUS"
            raise CuratorError("OUTPUT_COMMITTED_OWNER_MARKER_AMBIGUOUS", str(output))
        marker.unlink()
        output_digest, _output_files = tree_identity(output)
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "run_id": run_id,
            "status": "PASS",
            "source": {"root": str(source), "repo_id": source_repo_id, "dataset_digest": source_digest},
            "output": {"root": str(output), "repo_id": output_repo_id, "dataset_digest": output_digest},
            "runtime": {"python": platform.python_version(), "lerobot": lerobot_version},
            "encoder": {"vcodec": "h264", "preset": "ultrafast", "crf": 23},
            "publication": {
                "state": "COMMITTED_DURABLE",
                "rename_noreplace": True,
                "tree_fsync": True,
                "parent_fsync": True,
            },
            "profile_digest": profile["profile_digest"],
            "mask_sha256": profile["mask_sha256"],
            "background_plate_sha256": profile["background_plate_sha256"],
            "review_bundle_digest": review["review_bundle_digest"],
            "task_view_approval": {
                "artifact": str(request.approval_path),
                "artifact_sha256": approval_artifact_sha256,
                "approved_by": approval["approved_by"],
                "approved_at": approval["approved_at"],
                "provenance": approval["provenance"],
                "approval_digest": approval["approval_digest"],
                "training_authorized": False,
            },
            "verification": verification,
            "recording_quality_lineage": quality_lineage,
            "existing_validator": validator,
            "training_authority": False,
            "approval_inherited": False,
            "quarantine_inherited": False,
            "receipt_authority": "NON_AUTHORITATIVE",
        }
        receipt["receipt_digest"] = canonical_digest(receipt)
        try:
            write_json_atomic(run / "receipt.json", receipt)
        except Exception as receipt_error:
            receipt_path = run / "receipt.json"
            try:
                recorded = load_json(receipt_path, code="RECEIPT_READ") == receipt
            except CuratorError:
                recorded = False
            publication_state = (
                "COMMITTED_RECEIPT_DURABILITY_UNCONFIRMED"
                if recorded
                else "COMMITTED_RECEIPT_FAILED"
            )
            raise CuratorError(publication_state, str(output)) from receipt_error
        publication_state = "COMMITTED_RECEIPT_RECORDED"
        return receipt
    except Exception as exc:
        cleanup_safe = writer_closed
        writer_shutdown_state = (
            "FINALIZED_BEFORE_FAILURE"
            if writer_closed
            else "WRITER_CREATE_NOT_CONFIRMED"
        )
        if writer is not None:
            try:
                writer.finalize()
                cleanup_safe = True
                writer_closed = True
                writer_shutdown_state = "FINALIZED_DURING_FAILURE"
            except Exception:
                cleanup_safe = False
                writer_shutdown_state = "FINALIZE_FAILED"
            writer = None
        if published:
            cleanup_state = "NOT_APPLICABLE_OUTPUT_COMMITTED"
        elif not cleanup_safe:
            cleanup_state = "RETAINED_WRITER_SHUTDOWN_UNCONFIRMED"
        else:
            cleaned = _cleanup_owned(
                temporary,
                marker,
                owner,
                temporary_identity,
                marker_identity,
            )
            cleanup_state = "REMOVED" if cleaned else "RETAINED_IDENTITY_AMBIGUOUS"
        with suppress(Exception):
            write_json_atomic(
                run / "failure.json",
                {
                    "schema_version": "curator.derive_failure.v2",
                    "run_id": run_id,
                    "reason_code": exc.code if isinstance(exc, CuratorError) else "DERIVE_FAILURE",
                    "publication_state": publication_state,
                    "output_committed": published,
                    "output": str(output) if published else None,
                    "writer_shutdown_state": writer_shutdown_state,
                    "cleanup_state": cleanup_state,
                    "temporary_artifact": str(temporary),
                    "owner_marker_artifact": str(marker),
                    "training_authority": False,
                },
            )
        raise


__all__ = ["RECEIPT_SCHEMA", "derive_dataset"]
