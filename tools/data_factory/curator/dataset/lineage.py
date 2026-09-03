"""Strict source-to-candidate lineage and byte-preserved episode evidence."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
from typing import Any

from ..core.errors import CuratorError
from ..core.filesystem import write_json_exclusive
from ..core.identity import file_sha256
from ..core.jsonio import canonical_digest, exact_fields, load_json


SCHEMA = "curator.dataset_lineage.v1"
REFERENCE_FIELDS = {"path", "file_sha256", "lineage_digest"}
LINEAGE_FIELDS = {
    "schema_version",
    "source",
    "candidate_repo_id",
    "episode_mapping",
    "transform",
    "source_provenance",
    "external_producer_evidence",
    "training_authority",
    "approval_inherited",
    "lineage_digest",
}
SOURCE_FIELDS = {"root", "repo_id", "dataset_tree_digest"}
MAPPING_FIELDS = {"contract", "episodes", "frames"}
TRANSFORM_FIELDS = {
    "up",
    "wrist",
    "profile_digest",
    "mask_sha256",
    "background_plate_sha256",
}
PROVENANCE_FIELDS = {"copy_contract", "files", "files_digest"}
PROVENANCE_NAME = re.compile(r"episode-(\d{6})\.jsonl\Z")


def _expected_names(episodes: int) -> list[str]:
    if type(episodes) is not int or episodes <= 0:
        raise CuratorError("SOURCE_PROVENANCE_EPISODES")
    return [f"episode-{index:06d}.jsonl" for index in range(episodes)]


def _provenance_files(root: Path, episodes: int) -> list[str]:
    expected = _expected_names(episodes)
    if root.is_symlink() or not root.is_dir():
        raise CuratorError("SOURCE_PROVENANCE_EVIDENCE")
    paths = list(root.iterdir())
    if sorted(path.name for path in paths) != expected or any(
        path.is_symlink() or not path.is_file() for path in paths
    ):
        raise CuratorError("SOURCE_PROVENANCE_EVIDENCE")
    return expected


def _verify_provenance(
    output: Path,
    evidence: object,
    *,
    episodes: int,
) -> dict[str, Any]:
    value = exact_fields(evidence, PROVENANCE_FIELDS, "SOURCE_PROVENANCE_CONTRACT")
    files = value["files"]
    expected = _provenance_files(output / "meta/source_provenance", episodes)
    if (
        value["copy_contract"] != "BYTE_IDENTICAL_EPISODE_FILES"
        or not isinstance(files, dict)
        or sorted(files) != expected
        or value["files_digest"] != canonical_digest(files)
    ):
        raise CuratorError("SOURCE_PROVENANCE_CONTRACT")
    for name in expected:
        if (
            PROVENANCE_NAME.fullmatch(name) is None
            or file_sha256(output / "meta/source_provenance" / name) != files[name]
        ):
            raise CuratorError("SOURCE_PROVENANCE_COPY", name)
    return value


def copy_source_provenance(source: Path, output: Path, episodes: int) -> dict[str, Any]:
    source_root = source / "meta/source_provenance"
    expected = _provenance_files(source_root, episodes)
    target = output / "meta/source_provenance"
    target.mkdir(mode=0o700)
    digests: dict[str, str] = {}
    for name in expected:
        digest = file_sha256(source_root / name)
        shutil.copyfile(source_root / name, target / name)
        if file_sha256(target / name) != digest:
            raise CuratorError("SOURCE_PROVENANCE_COPY", name)
        digests[name] = digest
    evidence = {
        "copy_contract": "BYTE_IDENTICAL_EPISODE_FILES",
        "files": digests,
        "files_digest": canonical_digest(digests),
    }
    _verify_provenance(output, evidence, episodes=episodes)
    return evidence


def write_candidate_lineage(
    output: Path,
    *,
    source: Path,
    source_repo_id: str,
    source_digest: str,
    candidate_repo_id: str,
    profile: dict[str, Any],
    verification: dict[str, Any],
    source_provenance: dict[str, Any],
) -> dict[str, str]:
    lineage = {
        "schema_version": SCHEMA,
        "source": {
            "root": str(source),
            "repo_id": source_repo_id,
            "dataset_tree_digest": source_digest,
        },
        "candidate_repo_id": candidate_repo_id,
        "episode_mapping": {
            "contract": "IDENTICAL_EPISODE_FRAME_INDEX",
            "episodes": verification["episodes"],
            "frames": verification["frames"],
        },
        "transform": {
            "up": "STATIC_KEEP_MASK_BACKGROUND_PLATE_V1_H264_REENCODE",
            "wrist": "NO_PREENCODE_PIXEL_TRANSFORM_H264_REENCODE",
            "profile_digest": profile["profile_digest"],
            "mask_sha256": profile["mask_sha256"],
            "background_plate_sha256": profile["background_plate_sha256"],
        },
        "source_provenance": source_provenance,
        "external_producer_evidence": (
            "PRODUCER_RUN_EVIDENCE_EXTERNAL_UNBOUND_NOT_COPIED"
        ),
        "training_authority": False,
        "approval_inherited": False,
    }
    lineage["lineage_digest"] = canonical_digest(lineage)
    path = output / "meta/curator_lineage.json"
    write_json_exclusive(path, lineage)
    reference = {
        "path": "meta/curator_lineage.json",
        "file_sha256": file_sha256(path),
        "lineage_digest": lineage["lineage_digest"],
    }
    verify_candidate_lineage(
        output,
        reference,
        source=source,
        source_repo_id=source_repo_id,
        source_digest=source_digest,
        candidate_repo_id=candidate_repo_id,
        profile=profile,
        episodes=verification["episodes"],
        frames=verification["frames"],
    )
    return reference


def verify_candidate_lineage(
    output: Path,
    reference: object,
    *,
    source: Path,
    source_repo_id: str,
    source_digest: str,
    candidate_repo_id: str,
    profile: dict[str, Any],
    episodes: int,
    frames: int,
) -> dict[str, Any]:
    expected_reference = exact_fields(
        reference,
        REFERENCE_FIELDS,
        "DATASET_LINEAGE_REFERENCE",
    )
    if expected_reference["path"] != "meta/curator_lineage.json":
        raise CuratorError("DATASET_LINEAGE_REFERENCE")
    path = output / expected_reference["path"]
    if file_sha256(path) != expected_reference["file_sha256"]:
        raise CuratorError("DATASET_LINEAGE_FILE_DIGEST")
    value = exact_fields(
        load_json(path, code="DATASET_LINEAGE_JSON"),
        LINEAGE_FIELDS,
        "DATASET_LINEAGE_FIELDS",
    )
    digest = value["lineage_digest"]
    if digest != canonical_digest(
        {key: item for key, item in value.items() if key != "lineage_digest"}
    ):
        raise CuratorError("DATASET_LINEAGE_DIGEST")
    if digest != expected_reference["lineage_digest"]:
        raise CuratorError("DATASET_LINEAGE_REFERENCE")

    source_value = exact_fields(
        value["source"], SOURCE_FIELDS, "DATASET_LINEAGE_SOURCE"
    )
    mapping = exact_fields(
        value["episode_mapping"], MAPPING_FIELDS, "DATASET_LINEAGE_MAPPING"
    )
    transform = exact_fields(
        value["transform"], TRANSFORM_FIELDS, "DATASET_LINEAGE_TRANSFORM"
    )
    if (
        value["schema_version"] != SCHEMA
        or source_value
        != {
            "root": str(source),
            "repo_id": source_repo_id,
            "dataset_tree_digest": source_digest,
        }
        or value["candidate_repo_id"] != candidate_repo_id
        or mapping
        != {
            "contract": "IDENTICAL_EPISODE_FRAME_INDEX",
            "episodes": episodes,
            "frames": frames,
        }
        or transform
        != {
            "up": "STATIC_KEEP_MASK_BACKGROUND_PLATE_V1_H264_REENCODE",
            "wrist": "NO_PREENCODE_PIXEL_TRANSFORM_H264_REENCODE",
            "profile_digest": profile["profile_digest"],
            "mask_sha256": profile["mask_sha256"],
            "background_plate_sha256": profile["background_plate_sha256"],
        }
        or value["external_producer_evidence"]
        != "PRODUCER_RUN_EVIDENCE_EXTERNAL_UNBOUND_NOT_COPIED"
        or value["training_authority"] is not False
        or value["approval_inherited"] is not False
    ):
        raise CuratorError("DATASET_LINEAGE_CONTRACT")
    _verify_provenance(output, value["source_provenance"], episodes=episodes)
    return value


__all__ = [
    "SCHEMA",
    "copy_source_provenance",
    "verify_candidate_lineage",
    "write_candidate_lineage",
]
