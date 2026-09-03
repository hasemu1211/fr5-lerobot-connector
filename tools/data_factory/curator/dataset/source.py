"""Strict local-only LeRobot source reader and observable contract."""

from __future__ import annotations

from contextlib import contextmanager
import copy
import inspect
from pathlib import Path
import stat
import threading
from typing import Any

from ..core.errors import CuratorError
from ..core.filesystem import reject_symlink_components

_VERSION = "0.6.1"
_LOCK = threading.Lock()
_CUSTOM = {
    "observation.state",
    "action",
    "observation.images.up",
    "observation.images.wrist",
}


def validate_source_contract(dataset: Any, profile: dict[str, Any]) -> dict[str, Any]:
    try:
        from lerobot.utils.constants import DEFAULT_FEATURES
        from tools.fr5_dataset_schema import FEATURE_NAMES
    except ImportError as exc:
        raise CuratorError("LEROBOT_IMPORT", str(exc)) from exc
    features = dataset.meta.features
    custom = set(features) - set(DEFAULT_FEATURES)
    if custom != _CUSTOM:
        raise CuratorError("SOURCE_FEATURE_SET", str(sorted(custom)))
    for key in ("observation.state", "action"):
        feature = features[key]
        if (
            feature.get("dtype") != "float32"
            or list(feature.get("shape", [])) != [7]
            or feature.get("names") != FEATURE_NAMES
        ):
            raise CuratorError("SOURCE_NUMERIC_FEATURE", key)
    expected = [profile["height"], profile["width"], 3]
    for key in ("observation.images.up", "observation.images.wrist"):
        feature = features[key]
        if (
            feature.get("dtype") != "video"
            or list(feature.get("shape", [])) != expected
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
    return copy.deepcopy({key: features[key] for key in _CUSTOM})


@contextmanager
def _deny_lerobot_hub_fallback():
    import lerobot
    import lerobot.datasets.dataset_metadata as metadata_module
    import lerobot.datasets.lerobot_dataset as dataset_module
    from lerobot.datasets.lerobot_dataset import LeRobotDataset, LeRobotDatasetMetadata

    contracts = (
        (LeRobotDataset._download, ("self", "download_videos", "token")),
        (
            LeRobotDatasetMetadata._pull_from_repo,
            ("self", "allow_patterns", "ignore_patterns", "token"),
        ),
    )
    if lerobot.__version__ != _VERSION or any(
        tuple(inspect.signature(function).parameters) != parameters
        for function, parameters in contracts
    ):
        raise CuratorError(
            "LEROBOT_LOCAL_CONTRACT", "expected LeRobot 0.6.1 fallback hooks"
        )

    def denied(*_args, **_kwargs):
        raise CuratorError("SOURCE_LOCAL_INCOMPLETE", "Hub fallback is disabled")

    with _LOCK:
        original = (
            LeRobotDataset._download,
            LeRobotDatasetMetadata._pull_from_repo,
            dataset_module.get_safe_version,
            metadata_module.get_safe_version,
        )
        LeRobotDataset._download = denied
        LeRobotDatasetMetadata._pull_from_repo = denied
        dataset_module.get_safe_version = denied
        metadata_module.get_safe_version = denied
        try:
            yield LeRobotDataset, LeRobotDatasetMetadata
        finally:
            (
                LeRobotDataset._download,
                LeRobotDatasetMetadata._pull_from_repo,
                dataset_module.get_safe_version,
                metadata_module.get_safe_version,
            ) = original


def require_local_file(root: Path, relative: Path) -> None:
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise CuratorError("SOURCE_LOCAL_PATH_ESCAPE", str(relative))
    path = root / relative
    reject_symlink_components(path, "SOURCE_LOCAL_INCOMPLETE")
    try:
        resolved = path.resolve(strict=True)
        details = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise CuratorError("SOURCE_LOCAL_INCOMPLETE", str(relative)) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CuratorError("SOURCE_LOCAL_PATH_ESCAPE", str(relative)) from exc
    if resolved != path or not stat.S_ISREG(details.st_mode) or details.st_size <= 0:
        raise CuratorError("SOURCE_LOCAL_INCOMPLETE", str(relative))


def open_source_dataset(root: Path, repo_id: str):
    reject_symlink_components(root, "SOURCE_DATASET")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise CuratorError("SOURCE_DATASET", str(exc)) from exc
    required = (
        root / "meta/info.json",
        root / "meta/stats.json",
        root / "meta/tasks.parquet",
    )
    if (
        root.is_symlink()
        or not root.is_dir()
        or any(path.is_symlink() or not path.is_file() for path in required)
        or (root / "meta/episodes").is_symlink()
        or not (root / "meta/episodes").is_dir()
    ):
        raise CuratorError(
            "SOURCE_DATASET", "complete local finalized metadata required"
        )
    try:
        with _deny_lerobot_hub_fallback() as (Dataset, Metadata):
            metadata = Metadata(repo_id, root=root, force_cache_sync=False, token=False)
            if (
                Path(metadata.root).resolve(strict=True) != root
                or type(metadata.total_episodes) is not int
                or metadata.total_episodes <= 0
                or len(metadata.episodes) != metadata.total_episodes
            ):
                raise CuratorError("SOURCE_LOCAL_INCOMPLETE", "episode metadata")
            files = {
                metadata.get_data_file_path(i) for i in range(metadata.total_episodes)
            }
            files.update(
                metadata.get_video_file_path(i, key)
                for i in range(metadata.total_episodes)
                for key in metadata.video_keys
            )
            for relative in files:
                require_local_file(root, relative)
            return Dataset(
                repo_id,
                root=root,
                force_cache_sync=False,
                download_videos=False,
                return_uint8=True,
                token=False,
            )
    except CuratorError:
        raise
    except Exception as exc:
        raise CuratorError("SOURCE_READER", str(exc)) from exc


__all__ = ["open_source_dataset", "require_local_file", "validate_source_contract"]
