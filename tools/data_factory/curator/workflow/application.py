"""Sole lifecycle owner for prepare, status, and human decide."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import math
import os
from pathlib import Path
import pwd
import secrets
from typing import Any, Iterator

import numpy as np

from ..core.errors import CuratorError
from ..core.filesystem import OwnedDirectory, fsync_directory, reject_symlink_components
from ..core.identity import assert_tree_identity, file_sha256, stable_tree_identity
from ..core.jsonio import DIGEST, SAFE_ID, canonical_digest
from ..dataset.materialize import MATERIALIZATION_SCHEMA, materialize_candidate
from ..dataset.publish import (
    candidate_action_path,
    candidate_identity,
    cleanup_candidate,
    publish_candidate,
)
from ..dataset.source import open_source_dataset
from ..profile.registry import (
    CANONICAL_BINDING_ROOT,
    CANONICAL_COLLECTION_PROFILE_ROOT,
    ResolvedViewProfile,
    load_profile_assets,
    resolve_review_policy,
    resolve_view_profile,
)
from ..profile.transform import uint8_hwc
from ..review.decision import read_foreground_decision
from ..review.manifest import create_manifest, verify_manifest
from ..review.render import ReviewFrame, render_review_mp4
from ..review.sampling import ReviewSignalCollector, sample_frames
from .state import RECOVERABLE_FAILURE_STATES, append_event, load_events, project_state


REPOSITORY = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class WorkflowPaths:
    run_root: Path
    output_parent: Path
    profile_root: Path
    policy_root: Path
    binding_root: Path
    collection_profile_root: Path


DEFAULT_PATHS = WorkflowPaths(
    run_root=REPOSITORY / "outputs/curator/runs",
    output_parent=REPOSITORY / "datasets/fr5_curated",
    profile_root=REPOSITORY / "config/data_factory/curator/view_profiles",
    policy_root=REPOSITORY / "config/data_factory/curator/review_policies",
    binding_root=CANONICAL_BINDING_ROOT,
    collection_profile_root=CANONICAL_COLLECTION_PROFILE_ROOT,
)


@dataclass(frozen=True)
class _Configuration:
    profile: ResolvedViewProfile
    policy_path: Path
    policy_file_sha256: str
    policy: dict[str, Any]
    policy_digest: str


@dataclass(frozen=True)
class _Evidence:
    request: dict[str, Any]
    candidate: dict[str, Any]
    ready: dict[str, Any]
    owned: OwnedDirectory
    candidate_snapshot: dict[str, list[int]] | None
    candidate_digest: str


def _run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("curator-%Y%m%dT%H%M%SZ-")
    return timestamp + secrets.token_hex(4)


def _now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _decision_actor() -> dict[str, Any]:
    uid = os.getuid()
    try:
        account = pwd.getpwuid(uid).pw_name
    except KeyError:
        account = str(uid)
    return {
        "kind": "LOCAL_OS_ACCOUNT",
        "uid": uid,
        "account": account,
        "human_identity_authenticated": False,
    }


def _run_path(run_id: str, paths: WorkflowPaths = DEFAULT_PATHS) -> Path:
    if not isinstance(run_id, str) or SAFE_ID.fullmatch(run_id) is None:
        raise CuratorError("RUN_ID")
    try:
        reject_symlink_components(paths.run_root, "RUN_ROOT")
        root = paths.run_root.resolve(strict=True)
    except OSError as exc:
        raise CuratorError("RUN_ROOT", str(exc)) from exc
    reject_symlink_components(root, "RUN_ROOT")
    if not root.is_dir() or root.is_symlink():
        raise CuratorError("RUN_ROOT", str(root))
    return root / run_id


@contextmanager
def _exclusive_run(run: Path) -> Iterator[None]:
    """Serialize foreground decisions without adding mutable run state."""
    reject_symlink_components(run, "RUN_PATH")
    try:
        expected = run.stat(follow_symlinks=False)
        descriptor = os.open(
            run,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise CuratorError("RUN_NOT_FOUND", str(run)) from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = run.stat(follow_symlinks=False)
        opened = os.fstat(descriptor)
        identities = {
            (expected.st_dev, expected.st_ino),
            (current.st_dev, current.st_ino),
            (opened.st_dev, opened.st_ino),
        }
        if len(identities) != 1:
            raise CuratorError("RUN_CHANGED_DURING_LOCK", str(run))
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _ensure_directory(path: Path, code: str) -> Path:
    reject_symlink_components(path, code)
    try:
        path.mkdir(parents=True, exist_ok=True)
        result = path.resolve(strict=True)
    except OSError as exc:
        raise CuratorError(code, str(exc)) from exc
    reject_symlink_components(result, code)
    if not result.is_dir() or result.is_symlink():
        raise CuratorError(code, str(result))
    return result


def _existing_directory(path: Path, code: str) -> Path:
    reject_symlink_components(path, code)
    try:
        result = path.resolve(strict=True)
    except OSError as exc:
        raise CuratorError(code, str(exc)) from exc
    reject_symlink_components(result, code)
    if not result.is_dir() or result.is_symlink():
        raise CuratorError(code, str(result))
    return result


def _source_path(path: str | Path) -> Path:
    reject_symlink_components(path, "SOURCE_ROOT")
    try:
        source = Path(path).resolve(strict=True)
    except OSError as exc:
        raise CuratorError("SOURCE_ROOT", str(exc)) from exc
    if not source.is_dir():
        raise CuratorError("SOURCE_ROOT", str(source))
    return source


def _configuration(
    paths: WorkflowPaths,
    *,
    profile_id: str | None = None,
    policy_id: str | None = None,
) -> _Configuration:
    profile = resolve_view_profile(
        paths.profile_root,
        profile_id,
        binding_root=paths.binding_root,
        collection_profile_root=paths.collection_profile_root,
    )
    policy_path, policy = resolve_review_policy(paths.policy_root, policy_id)
    return _Configuration(
        profile=profile,
        policy_path=policy_path,
        policy_file_sha256=file_sha256(policy_path),
        policy=policy,
        policy_digest=canonical_digest(policy),
    )


def _configuration_matches(request: dict[str, Any], current: _Configuration) -> bool:
    profile = current.profile
    return (
        profile.profile["profile_id"] == request["profile_id"]
        and str(profile.config_path) == request["profile_path"]
        and profile.config_file_sha256 == request["profile_file_sha256"]
        and profile.profile["profile_digest"] == request["profile_digest"]
        and current.policy["policy_id"] == request["policy_id"]
        and str(current.policy_path) == request["policy_path"]
        and current.policy_file_sha256 == request["policy_file_sha256"]
        and current.policy_digest == request["policy_digest"]
    )


def _current_configuration(
    request: dict[str, Any], paths: WorkflowPaths
) -> _Configuration:
    current = _configuration(
        paths,
        profile_id=request["profile_id"],
        policy_id=request["policy_id"],
    )
    if not _configuration_matches(request, current):
        raise CuratorError("CONFIGURATION_CHANGED")
    return current


def _direct_child(path: str, parent: Path, code: str) -> Path:
    candidate = Path(path)
    if (
        not candidate.is_absolute()
        or candidate.parent != parent
        or candidate.name in {"", ".", ".."}
    ):
        raise CuratorError(code, path)
    reject_symlink_components(candidate, code)
    return candidate


def _materialization_contract(
    value: object,
    *,
    run_id: str,
    request: dict[str, Any],
    owned: OwnedDirectory,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CuratorError("MATERIALIZATION_CONTRACT")
    digest = value.get("materialization_digest")
    if (
        value.get("schema_version") != MATERIALIZATION_SCHEMA
        or value.get("status") != "PASS"
        or value.get("run_id") != run_id
        or not isinstance(digest, str)
        or digest
        != canonical_digest(
            {
                key: item
                for key, item in value.items()
                if key != "materialization_digest"
            }
        )
        or value.get("profile_digest") != request["profile_digest"]
        or value.get("training_authority") is not False
        or value.get("approval_inherited") is not False
        or value.get("quarantine_inherited") is not False
    ):
        raise CuratorError("MATERIALIZATION_CONTRACT")
    source = value.get("source")
    candidate = value.get("candidate")
    commit = value.get("hidden_candidate_commit")
    lineage = value.get("dataset_lineage")
    if (
        not isinstance(source, dict)
        or source.get("root") != request["source"]
        or source.get("repo_id") != request["source_repo_id"]
        or source.get("dataset_digest") != request["source_tree_digest"]
        or not isinstance(candidate, dict)
        or candidate.get("root") != request["candidate_path"]
        or candidate.get("repo_id") != request["candidate_repo_id"]
        or candidate.get("ownership") != owned.as_json()
        or not isinstance(candidate.get("dataset_digest"), str)
        or not isinstance(commit, dict)
        or commit.get("state") != "COMMITTED_DURABLE"
        or commit.get("tree_fsync") is not True
        or commit.get("parent_fsync") is not True
        or not isinstance(lineage, dict)
        or set(lineage) != {"path", "file_sha256", "lineage_digest"}
        or lineage.get("path") != "meta/curator_lineage.json"
        or not isinstance(lineage.get("file_sha256"), str)
        or DIGEST.fullmatch(lineage["file_sha256"]) is None
        or not isinstance(lineage.get("lineage_digest"), str)
        or DIGEST.fullmatch(lineage["lineage_digest"]) is None
    ):
        raise CuratorError("MATERIALIZATION_CONTRACT")
    return value


def _review_paths(run: Path) -> tuple[Path, Path, Path]:
    directory = run / "review"
    return directory, directory / "review.mp4", directory / "manifest.json"


def _scalar(value: Any, code: str, *, integer: bool = False) -> int | float:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    array = np.asarray(value)
    if array.size != 1:
        raise CuratorError(code)
    item = array.reshape(-1)[0].item()
    if integer:
        if isinstance(item, bool) or not isinstance(item, (int, np.integer)):
            raise CuratorError(code)
        return int(item)
    if isinstance(item, bool) or not isinstance(
        item, (int, float, np.integer, np.floating)
    ):
        raise CuratorError(code)
    result = float(item)
    if not math.isfinite(result):
        raise CuratorError(code)
    return result


def _selected_review_frames(
    source_dataset: Any,
    candidate_dataset: Any,
    clips: list[dict[str, Any]],
    *,
    width: int,
    height: int,
) -> Iterator[ReviewFrame]:
    for clip in clips:
        for offset, dataset_index in enumerate(clip["dataset_indices"]):
            try:
                source = source_dataset[dataset_index]
                candidate = candidate_dataset[dataset_index]
            except Exception as exc:
                raise CuratorError(
                    "REVIEW_SELECTED_DECODE", str(dataset_index)
                ) from exc
            episode = _scalar(source["episode_index"], "REVIEW_EPISODE", integer=True)
            frame_index = _scalar(source["frame_index"], "REVIEW_FRAME", integer=True)
            candidate_episode = _scalar(
                candidate["episode_index"], "REVIEW_CANDIDATE_EPISODE", integer=True
            )
            candidate_frame = _scalar(
                candidate["frame_index"], "REVIEW_CANDIDATE_FRAME", integer=True
            )
            if (
                episode != clip["episode_index"]
                or frame_index != clip["frame_indices"][offset]
                or candidate_episode != episode
                or candidate_frame != frame_index
                or source["task"] != clip["task"]
                or candidate["task"] != source["task"]
            ):
                raise CuratorError("REVIEW_FRAME_MAPPING", str(dataset_index))
            yield ReviewFrame(
                raw_up=uint8_hwc(
                    source["observation.images.up"],
                    width=width,
                    height=height,
                    code="REVIEW_RAW",
                ),
                candidate_up=uint8_hwc(
                    candidate["observation.images.up"],
                    width=width,
                    height=height,
                    code="REVIEW_CANDIDATE",
                ),
                clip_id=clip["clip_id"],
                episode_index=int(episode),
                frame_index=int(frame_index),
                timestamp=float(_scalar(source["timestamp"], "REVIEW_TIMESTAMP")),
                reasons=tuple(clip["reasons"]),
            )


def _reason_code(exc: BaseException, fallback: str) -> str:
    if isinstance(exc, CuratorError):
        return exc.code
    if isinstance(exc, KeyboardInterrupt):
        return fallback.replace("FAILURE", "INTERRUPTED")
    return fallback


def _record_failure(
    run: Path,
    previous: str,
    exc: BaseException,
    *,
    state: str,
    cleanup_state: str,
    fallback: str,
) -> None:
    try:
        append_event(
            run,
            "failure",
            {
                "state": state,
                "reason_code": _reason_code(exc, fallback),
                "cleanup_state": cleanup_state,
                "resumable": False,
                "training_authority": False,
            },
            previous,
        )
    except Exception:
        pass


def prepare(
    source: str | Path,
    *,
    _paths: WorkflowPaths = DEFAULT_PATHS,
    _run_id_value: str | None = None,
) -> dict[str, Any]:
    """Create and verify a hidden candidate, then emit one bounded review."""
    source_path = _source_path(source)
    if SAFE_ID.fullmatch(source_path.name) is None:
        raise CuratorError("SOURCE_NAME_UNSAFE", source_path.name)
    source_snapshot, source_digest = stable_tree_identity(
        source_path,
        code="SOURCE_CHANGED_DURING_IDENTITY",
    )
    configuration = _configuration(_paths)
    profile = configuration.profile.profile
    keep_mask, _background_plate = load_profile_assets(configuration.profile)

    run_root = _ensure_directory(_paths.run_root, "RUN_ROOT")
    output_parent = _ensure_directory(_paths.output_parent, "OUTPUT_PARENT")
    run_id = _run_id() if _run_id_value is None else _run_id_value
    run = _run_path(run_id, _paths)
    if run.parent.resolve(strict=True) != run_root:
        raise CuratorError("RUN_ROOT")
    try:
        run.mkdir(mode=0o700)
        fsync_directory(run_root)
    except FileExistsError as exc:
        raise CuratorError("RUN_EXISTS", run_id) from exc
    except OSError as exc:
        raise CuratorError("RUN_CREATE", str(exc)) from exc

    output = output_parent / f"{source_path.name}-{profile['profile_id']}"
    candidate = output_parent / f".{output.name}.{run_id}.candidate"
    if output.exists() or output.is_symlink():
        run.rmdir()
        raise CuratorError("OUTPUT_EXISTS", str(output))
    if candidate.exists() or candidate.is_symlink():
        run.rmdir()
        raise CuratorError("CANDIDATE_EXISTS", str(candidate))
    source_repo_id = f"local/{source_path.name}"
    candidate_repo_id = f"local/{output.name}"
    nonce = secrets.token_hex(16)
    request_payload = {
        "source": str(source_path),
        "source_repo_id": source_repo_id,
        "source_snapshot": source_snapshot,
        "source_tree_digest": source_digest,
        "profile_id": profile["profile_id"],
        "profile_path": str(configuration.profile.config_path),
        "profile_file_sha256": configuration.profile.config_file_sha256,
        "profile_digest": profile["profile_digest"],
        "policy_id": configuration.policy["policy_id"],
        "policy_path": str(configuration.policy_path),
        "policy_file_sha256": configuration.policy_file_sha256,
        "policy_digest": configuration.policy_digest,
        "candidate_path": str(candidate),
        "candidate_repo_id": candidate_repo_id,
        "output_path": str(output),
        "candidate_owner_nonce": nonce,
        "placement_lineage": "PLACEMENT_LINEAGE_UNPROVEN",
        "training_authority": False,
    }
    previous: str | None = None
    owned: OwnedDirectory | None = None
    candidate_digest: str | None = None
    materialization_handoff: list[dict[str, Any]] = []
    try:
        request_event = append_event(run, "request", request_payload, None)
        previous = request_event["event_digest"]
        collector = ReviewSignalCollector(keep_mask)
        materialization = materialize_candidate(
            source_path,
            candidate,
            configuration.profile,
            run_id=run_id,
            source_repo_id=source_repo_id,
            candidate_repo_id=candidate_repo_id,
            expected_source_snapshot=source_snapshot,
            expected_source_digest=source_digest,
            frame_observer=collector.observe,
            _ownership_handoff=materialization_handoff.append,
        )
        if materialization_handoff != [materialization]:
            raise CuratorError("MATERIALIZATION_HANDOFF")
        candidate_value = materialization.get("candidate")
        if not isinstance(candidate_value, dict):
            raise CuratorError("MATERIALIZATION_CONTRACT")
        owned = OwnedDirectory.from_json(candidate_value.get("ownership"))
        candidate_digest = candidate_value.get("dataset_digest")
        if not isinstance(candidate_digest, str):
            raise CuratorError("MATERIALIZATION_CONTRACT")
        _materialization_contract(
            materialization,
            run_id=run_id,
            request=request_payload,
            owned=owned,
        )
        assert_tree_identity(
            source_path,
            source_snapshot,
            source_digest,
            code="SOURCE_CHANGED_BEFORE_CANDIDATE_EVENT",
        )
        _current_configuration(request_payload, _paths)
        parent_fd = owned.parent_fd()
        try:
            owned.verify_at(parent_fd)
        finally:
            os.close(parent_fd)
        candidate_payload = {
            "request_digest": request_event["event_digest"],
            "candidate": owned.as_json(),
            "candidate_tree_digest": candidate_digest,
            "materialization": materialization,
            "source_tree_digest": source_digest,
            "profile_digest": profile["profile_digest"],
            "policy_digest": configuration.policy_digest,
            "candidate_owner_nonce": nonce,
        }
        candidate_event = append_event(
            run,
            "candidate_ready",
            candidate_payload,
            request_event["event_digest"],
        )
        previous = candidate_event["event_digest"]

        rows = collector.finish()
        policy = configuration.policy
        clips, coverage = sample_frames(
            rows,
            seed=policy["seed"],
            max_clips=policy["max_clips"],
            clip_frames=policy["clip_frames"],
            fps=policy["render_fps"],
            max_duration_seconds=policy["max_duration_seconds"],
            relative_time_quantiles=policy["relative_time_quantiles"],
        )
        review_directory, video_path, manifest_path = _review_paths(run)
        review_directory.mkdir(mode=0o700)
        fsync_directory(run)
        source_dataset = open_source_dataset(source_path, source_repo_id)
        candidate_dataset = open_source_dataset(candidate, candidate_repo_id)
        expected_frames = sum(len(clip["dataset_indices"]) for clip in clips)
        video = render_review_mp4(
            _selected_review_frames(
                source_dataset,
                candidate_dataset,
                clips,
                width=profile["width"],
                height=profile["height"],
            ),
            video_path,
            keep_mask=keep_mask,
            geometry=configuration.profile.geometry,
            width=profile["width"],
            height=profile["height"],
            fps=policy["render_fps"],
            expected_frames=expected_frames,
        )
        identities = {
            "source_tree_digest": source_digest,
            "candidate_tree_digest": candidate_digest,
            "profile_digest": profile["profile_digest"],
            "profile_file_sha256": configuration.profile.config_file_sha256,
            "policy_digest": configuration.policy_digest,
            "policy_file_sha256": configuration.policy_file_sha256,
            "request_event_digest": request_event["event_digest"],
            "candidate_ready_event_digest": candidate_event["event_digest"],
        }
        manifest = create_manifest(
            manifest_path,
            clips=clips,
            coverage=coverage,
            identities=identities,
            video_path=video_path,
            video=video,
            fps=policy["render_fps"],
        )

        assert_tree_identity(
            source_path,
            source_snapshot,
            source_digest,
            code="SOURCE_CHANGED_BEFORE_REVIEW_EVENT",
        )
        _current_configuration(request_payload, _paths)
        _snapshot, current_candidate_digest = candidate_identity(owned)
        if current_candidate_digest != candidate_digest:
            raise CuratorError("CANDIDATE_CHANGED_BEFORE_REVIEW_EVENT")
        if verify_manifest(manifest_path, video_path) != manifest:
            raise CuratorError("REVIEW_CHANGED_BEFORE_EVENT")
        ready_payload = {
            "request_digest": request_event["event_digest"],
            "candidate_tree_digest": candidate_digest,
            "source_tree_digest": source_digest,
            "profile_digest": profile["profile_digest"],
            "policy_digest": configuration.policy_digest,
            "review_manifest_digest": manifest["review_manifest_digest"],
            "review_video_sha256": manifest["review_video_sha256"],
            "review_manifest_path": str(manifest_path),
            "review_video_path": str(video_path),
        }
        review_event = append_event(run, "review_ready", ready_payload, previous)
        previous = review_event["event_digest"]
        return {
            "ok": True,
            "run_id": run_id,
            "status": "REVIEW_READY",
            "review_video": str(video_path),
            "review_manifest": str(manifest_path),
        }
    except BaseException as exc:
        if (owned is None or candidate_digest is None) and len(
            materialization_handoff
        ) == 1:
            handed_off = materialization_handoff[0]
            candidate_value = handed_off.get("candidate")
            if isinstance(candidate_value, dict):
                try:
                    recovered_owned = OwnedDirectory.from_json(
                        candidate_value.get("ownership")
                    )
                    recovered_digest = candidate_value.get("dataset_digest")
                    if isinstance(recovered_digest, str):
                        owned = recovered_owned
                        candidate_digest = recovered_digest
                except CuratorError:
                    pass
        try:
            current_events = load_events(run)
        except BaseException:
            current_events = None
        if current_events is not None and "review_ready" in current_events:
            # The complete cross-process boundary is already durable. Preserve
            # the exact candidate and let a later decide invocation revalidate it.
            raise
        cleanup_state = "NOT_CREATED_OR_UNPROVEN"
        if (
            current_events is not None
            and "failure" not in current_events
            and owned is not None
            and candidate_digest is not None
        ):
            try:
                cleanup_candidate(owned, candidate_digest)
                cleanup_state = "REMOVED"
            except Exception:
                cleanup_state = "RETAINED_IDENTITY_AMBIGUOUS"
        if current_events is not None and "failure" not in current_events:
            anchor = next(
                current_events[name]["event_digest"]
                for name in ("candidate_ready", "request")
                if name in current_events
            )
            _record_failure(
                run,
                anchor,
                exc,
                state="PREPARE_FAILED",
                cleanup_state=cleanup_state,
                fallback="PREPARE_FAILURE",
            )
        elif current_events is None and previous is None:
            try:
                run.rmdir()
                fsync_directory(run_root)
            except Exception:
                pass
        raise


def _validate_evidence(
    run: Path,
    events: dict[str, dict[str, Any]],
    paths: WorkflowPaths,
    *,
    require_candidate: bool,
) -> _Evidence:
    for name in ("request", "candidate_ready", "review_ready"):
        if name not in events:
            raise CuratorError("RUN_NOT_REVIEW_READY")
    request = events["request"]["payload"]
    candidate = events["candidate_ready"]["payload"]
    ready = events["review_ready"]["payload"]
    source = _source_path(request["source"])
    assert_tree_identity(
        source,
        request["source_snapshot"],
        request["source_tree_digest"],
        code="SOURCE_CHANGED_BEFORE_DECISION",
    )
    _current_configuration(request, paths)

    output_parent = _existing_directory(paths.output_parent, "OUTPUT_PARENT")
    candidate_path = _direct_child(
        request["candidate_path"], output_parent, "CANDIDATE_PATH"
    )
    output_path = _direct_child(request["output_path"], output_parent, "OUTPUT_PATH")
    if not candidate_path.name.startswith(".") or not candidate_path.name.endswith(
        ".candidate"
    ):
        raise CuratorError("CANDIDATE_PATH", str(candidate_path))
    if output_path.name.startswith(".") or source in {candidate_path, output_path}:
        raise CuratorError("OUTPUT_PATH", str(output_path))
    owned = OwnedDirectory.from_json(candidate["candidate"])
    if owned.path != candidate_path:
        raise CuratorError("CANDIDATE_OWNERSHIP_PATH")
    materialization = _materialization_contract(
        candidate["materialization"],
        run_id=run.name,
        request=request,
        owned=owned,
    )
    materialized_candidate = materialization["candidate"]
    digest = candidate["candidate_tree_digest"]
    checks = (
        candidate["request_digest"] == events["request"]["event_digest"],
        candidate["candidate_owner_nonce"] == request["candidate_owner_nonce"],
        candidate["source_tree_digest"] == request["source_tree_digest"],
        candidate["profile_digest"] == request["profile_digest"],
        candidate["policy_digest"] == request["policy_digest"],
        materialized_candidate["dataset_digest"] == digest,
        ready["request_digest"] == events["request"]["event_digest"],
        ready["candidate_tree_digest"] == digest,
        ready["source_tree_digest"] == request["source_tree_digest"],
        ready["profile_digest"] == request["profile_digest"],
        ready["policy_digest"] == request["policy_digest"],
    )
    if not all(checks):
        raise CuratorError("DECISION_DIGEST_CHAIN")

    review_directory, video_path, manifest_path = _review_paths(run)
    if (
        Path(ready["review_video_path"]) != video_path
        or Path(ready["review_manifest_path"]) != manifest_path
        or review_directory.is_symlink()
        or not review_directory.is_dir()
    ):
        raise CuratorError("REVIEW_PATH")
    manifest = verify_manifest(manifest_path, video_path)
    identities = manifest["identities"]
    if (
        manifest["review_manifest_digest"] != ready["review_manifest_digest"]
        or manifest["review_video_sha256"] != ready["review_video_sha256"]
        or identities["source_tree_digest"] != request["source_tree_digest"]
        or identities["candidate_tree_digest"] != digest
        or identities["profile_digest"] != request["profile_digest"]
        or identities["profile_file_sha256"] != request["profile_file_sha256"]
        or identities["policy_digest"] != request["policy_digest"]
        or identities["policy_file_sha256"] != request["policy_file_sha256"]
        or identities["request_event_digest"] != events["request"]["event_digest"]
        or identities["candidate_ready_event_digest"]
        != events["candidate_ready"]["event_digest"]
    ):
        raise CuratorError("REVIEW_DIGEST_CHAIN")

    snapshot: dict[str, list[int]] | None = None
    if require_candidate:
        snapshot, current_digest = candidate_identity(owned)
        if current_digest != digest:
            raise CuratorError("CANDIDATE_CHANGED_BEFORE_DECISION")
    return _Evidence(
        request=request,
        candidate=candidate,
        ready=ready,
        owned=owned,
        candidate_snapshot=snapshot,
        candidate_digest=digest,
    )


def _recorded_action_evidence(
    run: Path,
    events: dict[str, dict[str, Any]],
    paths: WorkflowPaths,
) -> _Evidence:
    """Rebuild only immutable evidence needed after an action already completed."""
    for name in ("request", "candidate_ready", "review_ready", "decision"):
        if name not in events:
            raise CuratorError("RUN_DECISION_INCOMPLETE")
    request = events["request"]["payload"]
    candidate = events["candidate_ready"]["payload"]
    ready = events["review_ready"]["payload"]
    output_parent = _existing_directory(paths.output_parent, "OUTPUT_PARENT")
    candidate_path = _direct_child(
        request["candidate_path"], output_parent, "CANDIDATE_PATH"
    )
    output_path = _direct_child(request["output_path"], output_parent, "OUTPUT_PATH")
    if (
        not candidate_path.name.startswith(".")
        or not candidate_path.name.endswith(".candidate")
        or output_path.name.startswith(".")
    ):
        raise CuratorError("RUN_ACTION_PATH")
    owned = OwnedDirectory.from_json(candidate["candidate"])
    if owned.path != candidate_path:
        raise CuratorError("CANDIDATE_OWNERSHIP_PATH")
    materialization = _materialization_contract(
        candidate["materialization"],
        run_id=run.name,
        request=request,
        owned=owned,
    )
    digest = candidate["candidate_tree_digest"]
    if materialization["candidate"].get("dataset_digest") != digest:
        raise CuratorError("MATERIALIZATION_CONTRACT")
    evidence = _Evidence(
        request=request,
        candidate=candidate,
        ready=ready,
        owned=owned,
        candidate_snapshot=None,
        candidate_digest=digest,
    )
    _validate_decision(events, evidence)
    return evidence


def _validate_decision(
    events: dict[str, dict[str, Any]], evidence: _Evidence
) -> dict[str, Any]:
    if "decision" not in events:
        raise CuratorError("RUN_DECISION_MISSING")
    decision = events["decision"]["payload"]
    if (
        decision["source_tree_digest"] != evidence.request["source_tree_digest"]
        or decision["candidate_tree_digest"] != evidence.candidate_digest
        or decision["profile_digest"] != evidence.request["profile_digest"]
        or decision["policy_digest"] != evidence.request["policy_digest"]
        or decision["review_manifest_digest"]
        != evidence.ready["review_manifest_digest"]
        or decision["review_video_sha256"] != evidence.ready["review_video_sha256"]
        or decision["output_path"] != evidence.request["output_path"]
        or decision["candidate"] != evidence.owned.as_json()
    ):
        raise CuratorError("DECISION_DIGEST_CHAIN")
    return decision


def _receipt_payload(
    evidence: _Evidence,
    decision_event: dict[str, Any],
    outcome: str,
) -> dict[str, Any]:
    request = evidence.request
    return {
        "outcome": outcome,
        "source": {
            "root": request["source"],
            "repo_id": request["source_repo_id"],
            "dataset_digest": request["source_tree_digest"],
        },
        "output": (
            {
                "root": request["output_path"],
                "repo_id": request["candidate_repo_id"],
                "dataset_digest": evidence.candidate_digest,
            }
            if outcome == "PUBLISHED"
            else None
        ),
        "candidate_tree_digest": evidence.candidate_digest,
        "profile_digest": request["profile_digest"],
        "review_manifest_digest": evidence.ready["review_manifest_digest"],
        "decision_digest": decision_event["event_digest"],
        "training_authority": False,
        "approval_inherited": False,
        "committed_durable": outcome == "PUBLISHED",
    }


def _write_receipt(
    run: Path,
    evidence: _Evidence,
    decision_event: dict[str, Any],
    outcome: str,
    previous: str,
) -> dict[str, Any]:
    return append_event(
        run,
        "receipt",
        _receipt_payload(evidence, decision_event, outcome),
        previous,
    )


def _pending_failure(
    run: Path,
    previous: str,
    exc: BaseException,
    *,
    action: str,
    output: str,
    state: str,
    cleanup_state: str,
) -> None:
    append_event(
        run,
        "failure",
        {
            "state": state,
            "reason_code": _reason_code(exc, f"{action}_RECEIPT_FAILURE"),
            "cleanup_state": cleanup_state,
            "resumable": True,
            "training_authority": False,
            "action": action,
            "output": output,
            "reprompt": False,
        },
        previous,
    )


def _existing_receipt(
    run: Path,
    evidence: _Evidence,
    decision_event: dict[str, Any],
    outcome: str,
    previous: str,
) -> dict[str, Any] | None:
    path = run / "receipt.json"
    if not path.exists() and not path.is_symlink():
        return None
    try:
        receipt = load_events(run).get("receipt")
    except BaseException as exc:
        raise CuratorError("RECEIPT_STATE_AMBIGUOUS", str(path)) from exc
    if (
        receipt is None
        or receipt["previous_event_digest"] != previous
        or receipt["payload"] != _receipt_payload(evidence, decision_event, outcome)
    ):
        raise CuratorError("RECEIPT_STATE_AMBIGUOUS", str(path))
    fsync_directory(run)
    return receipt


def _commit_receipt(
    run: Path,
    evidence: _Evidence,
    decision_event: dict[str, Any],
    outcome: str,
    previous: str,
    *,
    failure: dict[str, Any] | None,
    action: str,
    state: str,
    cleanup_state: str,
    pending_code: str,
    output: Path,
) -> dict[str, Any]:
    try:
        return _write_receipt(run, evidence, decision_event, outcome, previous)
    except BaseException as exc:
        existing = _existing_receipt(run, evidence, decision_event, outcome, previous)
        if existing is not None:
            return existing
        if failure is None:
            _pending_failure(
                run,
                decision_event["event_digest"],
                exc,
                action=action,
                output=str(output),
                state=state,
                cleanup_state=cleanup_state,
            )
        raise CuratorError(pending_code, str(output)) from exc


def _published_output_matches(evidence: _Evidence) -> bool:
    output = Path(evidence.request["output_path"])
    try:
        details = output.stat(follow_symlinks=False)
        if output.is_symlink() or not output.is_dir():
            return False
        if (details.st_dev, details.st_ino) != (
            evidence.owned.device,
            evidence.owned.inode,
        ):
            return False
        _snapshot, digest = stable_tree_identity(
            output, code="COMMITTED_OUTPUT_CHANGED"
        )
        return digest == evidence.candidate_digest
    except (CuratorError, OSError):
        return False


def _path_has_owned_identity(path: Path, owned: OwnedDirectory) -> bool:
    try:
        details = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return (
        not path.is_symlink()
        and path.is_dir()
        and (details.st_dev, details.st_ino) == (owned.device, owned.inode)
    )


def _owned_path_state(path: Path, owned: OwnedDirectory) -> str:
    if not path.exists() and not path.is_symlink():
        return "ABSENT"
    return "OWNED" if _path_has_owned_identity(path, owned) else "FOREIGN"


def _finish_recorded_decision(
    run: Path,
    events: dict[str, dict[str, Any]],
    paths: WorkflowPaths,
    *,
    validated_evidence: _Evidence | None = None,
) -> dict[str, Any]:
    decision_event = events["decision"]
    choice = decision_event["payload"]["decision"]
    failure = events.get("failure")
    if failure is not None:
        state = failure["payload"]["state"]
        if state not in RECOVERABLE_FAILURE_STATES:
            raise CuratorError("RUN_TERMINAL_FAILURE")
        expected = "PUBLISH" if choice == "APPROVE" else "REJECT"
        if failure["payload"]["action"] != expected:
            raise CuratorError("RUN_RECOVERY_ACTION")
    previous = (
        failure["event_digest"]
        if failure is not None
        else decision_event["event_digest"]
    )

    output = Path(events["request"]["payload"]["output_path"])
    candidate_path = Path(events["request"]["payload"]["candidate_path"])
    recorded_owner = OwnedDirectory.from_json(
        events["candidate_ready"]["payload"]["candidate"]
    )
    evidence = validated_evidence or _recorded_action_evidence(run, events, paths)
    _validate_decision(events, evidence)
    publish_stage = candidate_action_path(
        recorded_owner,
        evidence.candidate_digest,
        "publish",
    )
    reject_stage = candidate_action_path(
        recorded_owner,
        evidence.candidate_digest,
        "reject",
    )
    candidate_state = _owned_path_state(candidate_path, recorded_owner)
    publish_stage_state = _owned_path_state(publish_stage, recorded_owner)
    reject_stage_state = _owned_path_state(reject_stage, recorded_owner)
    output_state = _owned_path_state(output, recorded_owner)
    if "FOREIGN" in {
        candidate_state,
        publish_stage_state,
        reject_stage_state,
    }:
        raise CuratorError("RUN_ACTION_IDENTITY")

    if choice == "REJECT":
        if output_state == "OWNED":
            raise CuratorError("REJECT_CANDIDATE_ALREADY_PUBLISHED", str(output))
        if publish_stage_state != "ABSENT":
            raise CuratorError("REJECT_PUBLISH_STAGE")
        if candidate_state == "OWNED" and reject_stage_state == "OWNED":
            raise CuratorError("REJECT_CANDIDATE_DUPLICATED")
        if candidate_state == "OWNED" or reject_stage_state == "OWNED":
            try:
                cleanup_candidate(evidence.owned, evidence.candidate_digest)
            except BaseException as exc:
                candidate_after = _owned_path_state(candidate_path, recorded_owner)
                stage_after = _owned_path_state(reject_stage, recorded_owner)
                if "FOREIGN" in {candidate_after, stage_after}:
                    if failure is None:
                        _record_failure(
                            run,
                            decision_event["event_digest"],
                            exc,
                            state="DECISION_FAILED",
                            cleanup_state="RETAINED_IDENTITY_AMBIGUOUS",
                            fallback="REJECT_CLEANUP_FAILURE",
                        )
                    raise
                action_pending = "OWNED" in {candidate_after, stage_after}
                if failure is None:
                    _pending_failure(
                        run,
                        decision_event["event_digest"],
                        exc,
                        action="REJECT",
                        output=str(output),
                        state=(
                            "REJECT_ACTION_PENDING"
                            if action_pending
                            else "REJECTED_RECEIPT_PENDING"
                        ),
                        cleanup_state=(
                            "CANDIDATE_OR_STAGE_RETAINED"
                            if action_pending
                            else "REMOVED_PARENT_FSYNC_PENDING"
                        ),
                    )
                raise
        else:
            fsync_directory(candidate_path.parent)
        if (
            _owned_path_state(candidate_path, recorded_owner) != "ABSENT"
            or _owned_path_state(reject_stage, recorded_owner) != "ABSENT"
        ):
            raise CuratorError("REJECT_CLEANUP_INCOMPLETE")
        receipt = _commit_receipt(
            run,
            evidence,
            decision_event,
            "REJECTED",
            previous,
            failure=failure,
            action="REJECT",
            state="REJECTED_RECEIPT_PENDING",
            cleanup_state="REMOVED",
            pending_code="REJECTED_RECEIPT_PENDING",
            output=output,
        )
        return {
            "ok": True,
            "run_id": run.name,
            "status": "REJECTED",
            "receipt_digest": receipt["event_digest"],
        }

    if reject_stage_state != "ABSENT":
        raise CuratorError("PUBLISH_REJECT_STAGE")
    if output_state == "FOREIGN":
        raise CuratorError("COMMITTED_OUTPUT_IDENTITY")
    locations = {
        "candidate": candidate_state,
        "stage": publish_stage_state,
        "output": output_state,
    }
    if sum(state == "OWNED" for state in locations.values()) != 1:
        raise CuratorError("PUBLISH_ACTION_TOPOLOGY", str(locations))
    if output_state == "OWNED":
        if not _published_output_matches(evidence):
            raise CuratorError("COMMITTED_OUTPUT_IDENTITY")
        fsync_directory(output.parent)
    else:
        try:
            publish_candidate(
                evidence.owned,
                output,
                evidence.candidate_digest,
                verified_snapshot=evidence.candidate_snapshot,
            )
        except BaseException as exc:
            if _published_output_matches(evidence):
                if failure is None:
                    _pending_failure(
                        run,
                        decision_event["event_digest"],
                        exc,
                        action="PUBLISH",
                        output=str(output),
                        state="PUBLISHED_RECEIPT_PENDING",
                        cleanup_state="NOT_APPLICABLE_OUTPUT_COMMITTED",
                    )
                raise CuratorError(
                    "OUTPUT_COMMITTED_RECEIPT_PENDING", str(output)
                ) from exc
            candidate_after = _owned_path_state(candidate_path, recorded_owner)
            stage_after = _owned_path_state(publish_stage, recorded_owner)
            if "FOREIGN" not in {candidate_after, stage_after} and "OWNED" in {
                candidate_after,
                stage_after,
            }:
                if failure is None:
                    _pending_failure(
                        run,
                        decision_event["event_digest"],
                        exc,
                        action="PUBLISH",
                        output=str(output),
                        state="PUBLISH_ACTION_PENDING",
                        cleanup_state="CANDIDATE_OR_STAGE_RETAINED",
                    )
                raise CuratorError("PUBLISH_ACTION_PENDING", str(output)) from exc
            if failure is None:
                _record_failure(
                    run,
                    decision_event["event_digest"],
                    exc,
                    state="DECISION_FAILED",
                    cleanup_state="RETAINED_IDENTITY_AMBIGUOUS",
                    fallback="PUBLISH_FAILURE",
                )
            raise

    receipt = _commit_receipt(
        run,
        evidence,
        decision_event,
        "PUBLISHED",
        previous,
        failure=failure,
        action="PUBLISH",
        state="PUBLISHED_RECEIPT_PENDING",
        cleanup_state="NOT_APPLICABLE_OUTPUT_COMMITTED",
        pending_code="OUTPUT_COMMITTED_RECEIPT_PENDING",
        output=output,
    )
    return {
        "ok": True,
        "run_id": run.name,
        "status": "PUBLISHED",
        "output": str(output),
        "receipt_digest": receipt["event_digest"],
    }


def _abort_before_decision(
    run: Path,
    events: dict[str, dict[str, Any]],
    evidence: _Evidence,
    exc: BaseException,
) -> None:
    """Dispose only while the exact pre-decision event set is still current."""
    try:
        current = load_events(run)
    except BaseException as state_exc:
        raise CuratorError("RUN_CHANGED_DURING_DECISION") from state_exc
    if current != events:
        raise CuratorError("RUN_CHANGED_DURING_DECISION") from exc
    cleanup_state = "CANDIDATE_RETAINED"
    try:
        cleanup_candidate(evidence.owned, evidence.candidate_digest)
        cleanup_state = "REMOVED"
    except Exception:
        cleanup_state = "RETAINED_IDENTITY_AMBIGUOUS"
    _record_failure(
        run,
        events["review_ready"]["event_digest"],
        exc,
        state="DECISION_FAILED",
        cleanup_state=cleanup_state,
        fallback="DECISION_FAILURE",
    )


def _decide_locked(run: Path, paths: WorkflowPaths) -> dict[str, Any]:
    events = load_events(run)
    if "receipt" in events:
        fsync_directory(run)
        return project_state(run)
    if "decision" in events:
        return _finish_recorded_decision(run, events, paths)
    if "failure" in events:
        raise CuratorError("RUN_TERMINAL_FAILURE")
    if set(events) != {"request", "candidate_ready", "review_ready"}:
        raise CuratorError("RUN_NOT_REVIEW_READY")

    evidence = _validate_evidence(run, events, paths, require_candidate=True)
    choice = read_foreground_decision(evidence.ready["review_video_path"])
    current_events = load_events(run)
    if current_events != events:
        raise CuratorError("RUN_CHANGED_DURING_DECISION")
    try:
        current = _validate_evidence(run, current_events, paths, require_candidate=True)
    except BaseException as exc:
        _abort_before_decision(run, events, evidence, exc)
        raise
    if current.request != evidence.request or current.candidate != evidence.candidate:
        raise CuratorError("RUN_CHANGED_DURING_DECISION")

    decision_payload = {
        "decision": choice,
        "actor": _decision_actor(),
        "decided_at": _now(),
        "source_tree_digest": current.request["source_tree_digest"],
        "candidate_tree_digest": current.candidate_digest,
        "profile_digest": current.request["profile_digest"],
        "policy_digest": current.request["policy_digest"],
        "review_manifest_digest": current.ready["review_manifest_digest"],
        "review_video_sha256": current.ready["review_video_sha256"],
        "output_path": current.request["output_path"],
        "candidate": current.owned.as_json(),
        "provenance": (
            "HUMAN_CURATED_CANDIDATE_APPROVED"
            if choice == "APPROVE"
            else "HUMAN_CURATED_CANDIDATE_REJECTED"
        ),
        "training_authorized": False,
    }
    decision_event = append_event(
        run,
        "decision",
        decision_payload,
        events["review_ready"]["event_digest"],
    )
    recorded = load_events(run)
    if recorded["decision"] != decision_event:
        raise CuratorError("DECISION_REVALIDATION")
    post_decision = _validate_evidence(run, recorded, paths, require_candidate=True)
    _validate_decision(recorded, post_decision)
    return _finish_recorded_decision(
        run,
        recorded,
        paths,
        validated_evidence=post_decision,
    )


def decide(
    run_id: str,
    *,
    _paths: WorkflowPaths = DEFAULT_PATHS,
) -> dict[str, Any]:
    """Bind a foreground human choice, then publish or remove that exact candidate."""
    run = _run_path(run_id, _paths)
    with _exclusive_run(run):
        return _decide_locked(run, _paths)


def status(
    run_id: str,
    *,
    _paths: WorkflowPaths = DEFAULT_PATHS,
) -> dict[str, object]:
    return project_state(_run_path(run_id, _paths))


__all__ = ["WorkflowPaths", "decide", "prepare", "status"]
