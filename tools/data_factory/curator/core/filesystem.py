"""Fail-closed filesystem operations."""

from .jsonio import reject_symlink_components, rename_noreplace, write_json_atomic, write_json_exclusive

__all__ = ["reject_symlink_components", "rename_noreplace", "write_json_atomic", "write_json_exclusive"]
