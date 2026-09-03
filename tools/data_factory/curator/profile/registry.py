"""Canonical filename-based profile and policy resolution."""

from pathlib import Path
from typing import Callable, TypeVar

from ..core.jsonio import CuratorError
from .schema import load_review_policy, load_view_profile

T = TypeVar("T")


def _resolve(root: Path, identifier: str | None, loader: Callable[[Path], T], code: str) -> tuple[Path, T]:
    if root.is_symlink() or not root.is_dir():
        raise CuratorError(code, f"canonical directory required: {root}")
    paths = [root / f"{identifier}.json"] if identifier else sorted(root.glob("*.json"))
    matches: list[tuple[Path, T]] = []
    for path in paths:
        try:
            value = loader(path)
        except CuratorError:
            if identifier:
                raise
            continue
        matches.append((path.resolve(), value))
    if len(matches) != 1:
        raise CuratorError(code, f"expected one match, found {len(matches)}")
    return matches[0]


def resolve_view_profile(root: str | Path, profile_id: str | None = None):
    return _resolve(Path(root), profile_id, load_view_profile, "VIEW_PROFILE_RESOLUTION")


def resolve_review_policy(root: str | Path, policy_id: str | None = None):
    return _resolve(Path(root), policy_id, load_review_policy, "REVIEW_POLICY_RESOLUTION")


__all__ = ["resolve_review_policy", "resolve_view_profile"]
