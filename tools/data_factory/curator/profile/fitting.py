"""Native TRAIN split and exact pixel inputs used to fit a view profile."""

from pathlib import Path

from tools.data_factory.training_split import validate_training_split
from tools.fr5_data_factory import ContractError

from ..core.errors import CuratorError
from ..core.filesystem import reject_symlink_components
from ..core.identity import file_sha256
from ..core.jsonio import DIGEST, exact_fields, load_json


def load_fit_split(reference: dict) -> dict:
    ref = exact_fields(reference, {"path", "file_sha256", "split_digest"}, "FIT_SPLIT_REFERENCE")
    if not isinstance(ref["path"], str) or not Path(ref["path"]).is_absolute():
        raise CuratorError("FIT_SPLIT_PATH")
    path = reject_symlink_components(ref["path"], "FIT_SPLIT_PATH")
    if file_sha256(path) != ref["file_sha256"]:
        raise CuratorError("FIT_SPLIT_CHANGED")
    try:
        split = validate_training_split(path)
    except (ContractError, OSError) as exc:
        raise CuratorError("FIT_SPLIT_INVALID", str(exc)) from exc
    if split["schema_version"] != 3 or split["split_digest"] != ref["split_digest"]:
        raise CuratorError("FIT_SPLIT_INVALID")
    if file_sha256(path) != ref["file_sha256"]:
        raise CuratorError("FIT_SPLIT_CHANGED")
    return split


def fit_split_reference(path: str | Path) -> dict:
    try:
        source = reject_symlink_components(path, "FIT_SPLIT_PATH").resolve(strict=True)
    except OSError as exc:
        raise CuratorError("FIT_SPLIT_PATH", str(exc)) from exc
    digest = file_sha256(source)
    value = load_json(source, code="FIT_SPLIT_JSON")
    reference = {"path": str(source), "file_sha256": digest, "split_digest": value.get("split_digest")}
    load_fit_split(reference)
    return reference


def train_frame_ranges(split: dict) -> list[tuple[int, int, int]]:
    """Metadata only: (episode, first global frame, exclusive last frame)."""
    import pyarrow.parquet as pq

    source = Path(split["dataset_identity"]["dataset_root"])
    info = load_json(source / "meta/info.json", code="FIT_METADATA")
    rows = []
    for path in sorted((source / "meta/episodes").rglob("*.parquet")):
        reject_symlink_components(path, "FIT_METADATA")
        rows.extend(pq.read_table(path, columns=["episode_index", "length", "tasks"]).to_pylist())
    rows.sort(key=lambda row: row["episode_index"])
    if (not rows or [row["episode_index"] for row in rows] != list(range(split["total_episodes"]))
            or any(type(row["length"]) is not int or row["length"] <= 0 for row in rows)
            or sum(row["length"] for row in rows) != split["total_frames"]
            or [row["tasks"] for row in rows] != split["episode_tasks"]
            or info["total_frames"] != split["total_frames"]
            or info["total_episodes"] != split["total_episodes"]
            or info["features"] != split["feature_contract"]["dataset_features"]
            or info["fps"] != split["feature_contract"]["fps"]):
        raise CuratorError("FIT_METADATA_BINDING")
    ranges = []
    offset = 0
    for row in rows:
        end = offset + row["length"]
        if row["episode_index"] in split["train_episodes"]:
            ranges.append((row["episode_index"], offset, end))
        offset = end
    return ranges


def validate_profile_fitting(value: object, *, reference_index: int, plate_indices: list[int]) -> dict:
    fit = exact_fields(value, {"training_split", "reference_frame", "background_plate_frames"}, "PROFILE_FITTING_FIELDS")
    split = load_fit_split(fit["training_split"])
    ranges = {episode: (start, end) for episode, start, end in train_frame_ranges(split)}
    plate = fit["background_plate_frames"]
    if not isinstance(plate, list) or len(plate) != len(plate_indices):
        raise CuratorError("PROFILE_FITTING_FRAMES")
    for frame, expected in zip([fit["reference_frame"], *plate], [reference_index, *plate_indices], strict=True):
        frame = exact_fields(frame, {"global_index", "episode_index", "frame_index", "rgb_sha256"}, "PROFILE_FITTING_FRAME")
        if (any(type(frame[key]) is not int or frame[key] < 0 for key in ("global_index", "episode_index", "frame_index"))
                or frame["global_index"] != expected
                or frame["episode_index"] not in ranges
                or not isinstance(frame["rgb_sha256"], str)
                or DIGEST.fullmatch(frame["rgb_sha256"]) is None):
            raise CuratorError("PROFILE_FITTING_FRAME")
        start, end = ranges[frame["episode_index"]]
        if not start <= expected < end or expected != start + frame["frame_index"]:
            raise CuratorError("PROFILE_FITTING_FRAME")
    return fit
