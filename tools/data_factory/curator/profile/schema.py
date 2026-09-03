"""Strict review-policy schema and view-profile loader."""

from pathlib import Path
from typing import Any

from ..core.jsonio import SAFE_ID, CuratorError, exact_fields, load_json
from .geometry import ProfileRequest, load_profile_request

REVIEW_POLICY_SCHEMA = "curator.review_policy.v1"
_POLICY_FIELDS = {"schema_version", "policy_id", "seed", "max_clips", "clip_frames", "render_fps"}


def load_view_profile(path: str | Path) -> ProfileRequest:
    return load_profile_request(path)


def load_review_policy(path: str | Path) -> dict[str, Any]:
    value = exact_fields(load_json(path, code="REVIEW_POLICY_JSON"), _POLICY_FIELDS, "REVIEW_POLICY_FIELDS")
    if (
        value["schema_version"] != REVIEW_POLICY_SCHEMA
        or not isinstance(value["policy_id"], str)
        or SAFE_ID.fullmatch(value["policy_id"]) is None
        or not isinstance(value["seed"], int)
        or isinstance(value["seed"], bool)
        or not isinstance(value["max_clips"], int)
        or not 1 <= value["max_clips"] <= 32
        or not isinstance(value["clip_frames"], int)
        or not 1 <= value["clip_frames"] <= 300
        or value["render_fps"] not in range(1, 61)
    ):
        raise CuratorError("REVIEW_POLICY_CONTRACT")
    return value


__all__ = ["REVIEW_POLICY_SCHEMA", "load_review_policy", "load_view_profile"]
