"""Lossless native merge mapping; original recording evidence stays attributable."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import pyarrow as pa
import pyarrow.parquet as pq

from tools.fr5_data_factory import canonical_digest, load_json_strict
from ..core.errors import CuratorError
from ..core.identity import file_sha256
from ..core.filesystem import reject_symlink_components, write_json_exclusive
from .source import open_source_dataset


MAPPING_FILE = "meta/curator_mapping.json"
SCHEMA = "curator.dataset_mapping.v1"


def _quality(root):
    rows = [json.loads(line) for line in (root / "meta/recording_quality.jsonl").read_text().splitlines() if line]
    result = {row["episode_index"]: row for row in rows}
    if len(result) != len(rows):
        raise CuratorError("MAPPING_QUALITY_DUPLICATE")
    return result


def _rows(dataset, episode):
    table = pq.read_table(dataset.root / dataset.meta.get_data_file_path(episode))
    import pyarrow.compute as pc
    return table.filter(pc.equal(table["episode_index"], episode))


def _preserved_equal(left, right, names):
    for name in names:
        a, b = left[name], right[name]
        # Native pandas aggregation repacks fixed-size vectors as Arrow lists.
        # Permit only that container change, with identical element dtype/values.
        if (pa.types.is_fixed_size_list(a.type) and pa.types.is_list(b.type)
                and a.type.value_type == b.type.value_type):
            b = b.cast(a.type)
        if not a.equals(b):
            return False
    return True


def _sources(identities):
    from tools.data_factory.training_approval import current_dataset_identity
    result = []
    roots = set()
    for identity in identities:
        root = Path(identity["dataset_root"])
        if root in roots or (root / MAPPING_FILE).exists():
            raise CuratorError("MAPPING_SOURCE_DUPLICATE_OR_NESTED")
        roots.add(root)
        if current_dataset_identity(root, repo_id=identity["repo_id"], dataset_id=identity["dataset_id"]) != identity:
            raise CuratorError("MAPPING_SOURCE_CHANGED")
        result.append(open_source_dataset(root, identity["repo_id"]))
    if len(result) < 2:
        raise CuratorError("MAPPING_MULTIPLE_SOURCES_REQUIRED")
    return result


def write_mapping(root: Path, repo_id: str, identities: list[dict]) -> dict:
    """Rebind only episode numbers in the quality projection, never source facts."""
    sources = _sources(identities)
    entries, quality = [], []
    episode_offset = frame_offset = 0
    provenance = root / "meta/source_provenance"
    provenance.mkdir()
    for source_index, source in enumerate(sources):
        records = _quality(source.root)
        for episode in range(source.meta.total_episodes):
            target = episode_offset + episode
            original = source.root / f"meta/source_provenance/episode-{episode:06d}.jsonl"
            shutil.copyfile(original, provenance / f"episode-{target:06d}.jsonl")
            quality.append({**records[episode], "episode_index": target})
            entries.append({"source_index": source_index, "source_episode_index": episode,
                            "episode_index": target, "global_frame_offset": frame_offset})
        episode_offset += source.meta.total_episodes
        frame_offset += len(source)
    with (root / "meta/recording_quality.jsonl").open("x") as stream:
        stream.writelines(json.dumps(row, allow_nan=False, sort_keys=True) + "\n" for row in quality)
    value = {"schema_version": SCHEMA, "repo_id": repo_id, "sources": identities, "episodes": entries}
    value["mapping_digest"] = canonical_digest(value)
    write_json_exclusive(root / MAPPING_FILE, value)
    return verify_mapped_dataset(root, repo_id)


def verify_mapped_dataset(root: Path, repo_id: str) -> dict:
    """Verify full source/destination correspondence without model or approval calls."""
    value = load_json_strict(reject_symlink_components(root / MAPPING_FILE, "MAPPING_CONTRACT"))
    if (set(value) != {"schema_version", "repo_id", "sources", "episodes", "mapping_digest"}
            or value["schema_version"] != SCHEMA or value["repo_id"] != repo_id
            or canonical_digest({k:v for k,v in value.items() if k != "mapping_digest"}) != value["mapping_digest"]):
        raise CuratorError("MAPPING_CONTRACT")
    sources = _sources(value["sources"])
    child = open_source_dataset(root, repo_id)
    quality = _quality(root)
    expected = []
    episode_offset = frame_offset = 0
    hashes = {}

    def digest(path):
        if path not in hashes:
            hashes[path] = file_sha256(path)
        return hashes[path]

    for source_index, source in enumerate(sources):
        records = _quality(source.root)
        for episode in range(source.meta.total_episodes):
            target = episode_offset + episode
            expected.append({"source_index": source_index, "source_episode_index": episode,
                             "episode_index": target, "global_frame_offset": frame_offset})
            left, right = _rows(source, episode), _rows(child, target)
            preserved = [name for name in left.column_names if name not in {"episode_index", "index", "task_index"}]
            if (left.column_names != right.column_names or not left.num_rows
                    or not _preserved_equal(left, right, preserved)
                    or right["index"].to_pylist() != [i+frame_offset for i in left["index"].to_pylist()]):
                raise CuratorError("MAPPING_FRAME_CHANGED", str(target))
            source_tasks = [source.meta.tasks.index[i] for i in left["task_index"].to_pylist()]
            child_tasks = [child.meta.tasks.index[i] for i in right["task_index"].to_pylist()]
            a, b = source.meta.episodes[episode], child.meta.episodes[target]
            if source_tasks != child_tasks or a["tasks"] != b["tasks"]:
                raise CuratorError("MAPPING_TASK_CHANGED", str(target))
            if set(source.meta.video_keys) != set(child.meta.video_keys):
                raise CuratorError("MAPPING_VIDEO_CHANGED")
            for key in source.meta.video_keys:
                if (digest(source.root / source.meta.get_video_file_path(episode, key)) !=
                        digest(root / child.meta.get_video_file_path(target, key))
                        or any(a[f"videos/{key}/{part}"] != b[f"videos/{key}/{part}"]
                               for part in ("from_timestamp", "to_timestamp"))):
                    raise CuratorError("MAPPING_VIDEO_CHANGED", str(target))
            if (quality.get(target) != {**records[episode], "episode_index": target}
                    or digest(source.root / f"meta/source_provenance/episode-{episode:06d}.jsonl") !=
                    digest(root / f"meta/source_provenance/episode-{target:06d}.jsonl")):
                raise CuratorError("MAPPING_TIMING_CHANGED", str(target))
        episode_offset += source.meta.total_episodes
        frame_offset += len(source)
    if (canonical_digest(value["episodes"]) != canonical_digest(expected) or child.meta.total_episodes != episode_offset
            or len(child) != frame_offset or set(quality) != set(range(episode_offset))):
        raise CuratorError("MAPPING_EPISODE_SET")
    # Reopen byte identities after potentially lengthy Parquet/video checks.
    _sources(value["sources"])
    return value
