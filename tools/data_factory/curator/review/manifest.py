"""Digest-closed review manifest."""

from pathlib import Path
from typing import Any

from ..core.jsonio import CuratorError, canonical_digest, file_sha256, load_json, write_json_exclusive


def create_manifest(path: str | Path, *, samples: list[dict[str, Any]], identities: dict[str, str], video: str | Path) -> dict[str, Any]:
    value = {
        "schema_version": "curator.review_manifest.v1",
        "samples": samples,
        "identities": identities,
        "review_video_sha256": file_sha256(video),
    }
    value["review_manifest_digest"] = canonical_digest(value)
    write_json_exclusive(path, value)
    return value


def verify_manifest(path: str | Path, video: str | Path) -> dict[str, Any]:
    value = load_json(path, code="REVIEW_MANIFEST_JSON")
    digest = value.pop("review_manifest_digest", None)
    if digest != canonical_digest(value) or value.get("review_video_sha256") != file_sha256(video):
        raise CuratorError("REVIEW_MANIFEST_DIGEST")
    value["review_manifest_digest"] = digest
    return value


__all__ = ["create_manifest", "verify_manifest"]
