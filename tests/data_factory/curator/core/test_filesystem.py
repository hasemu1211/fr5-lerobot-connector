from __future__ import annotations

import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.core import filesystem
from tools.data_factory.curator.core.filesystem import (
    OwnedDirectory,
    remove_owned_directory,
    rename_noreplace_at,
    write_json_exclusive,
)


class FilesystemTest(unittest.TestCase):
    def test_exclusive_write_and_owned_name_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "event.json"
            write_json_exclusive(path, {"ok": True})
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)
            with self.assertRaisesRegex(CuratorError, "EVENT_EXISTS"):
                write_json_exclusive(path, {"ok": False})
            with self.assertRaisesRegex(CuratorError, "OWNED_DIRECTORY_CONTRACT"):
                OwnedDirectory.from_json(
                    {"parent": str(root), "name": "../escape", "device": 1, "inode": 2}
                )

    def test_rename_error_is_stable(self):
        class FailingRename:
            argtypes = None
            restype = None

            def __call__(self, *_args):
                return -1

        library = type("Library", (), {"renameat2": FailingRename()})()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            with (
                mock.patch(
                    "tools.data_factory.curator.core.filesystem.ctypes.CDLL",
                    return_value=library,
                ),
                mock.patch(
                    "tools.data_factory.curator.core.filesystem.ctypes.get_errno",
                    return_value=errno.EROFS,
                ),
                self.assertRaisesRegex(CuratorError, "OUTPUT_PUBLISH"),
            ):
                rename_noreplace_at(
                    OwnedDirectory.capture(source), Path(directory) / "target"
                )

    def test_failed_parent_fsync_preserves_exact_event_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            real_fsync = os.fsync
            calls = 0

            def fail_parent_once(descriptor):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError(errno.EIO, "injected parent fsync failure")
                return real_fsync(descriptor)

            with (
                mock.patch(
                    "tools.data_factory.curator.core.filesystem.os.fsync",
                    side_effect=fail_parent_once,
                ),
                self.assertRaisesRegex(CuratorError, "EVENT_COMMIT_AMBIGUOUS"),
            ):
                write_json_exclusive(path, {"ok": True})
            self.assertEqual(path.read_text(encoding="utf-8"), '{"ok":true}')

    def test_failed_commit_never_removes_a_replacement_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            real_fsync = os.fsync
            calls = 0

            def replace_before_parent_failure(descriptor):
                nonlocal calls
                calls += 1
                if calls == 2:
                    path.unlink()
                    path.write_text("replacement", encoding="utf-8")
                    raise OSError(errno.EIO, "injected parent fsync failure")
                return real_fsync(descriptor)

            with (
                mock.patch(
                    "tools.data_factory.curator.core.filesystem.os.fsync",
                    side_effect=replace_before_parent_failure,
                ),
                self.assertRaisesRegex(CuratorError, "EVENT_COMMIT_AMBIGUOUS"),
            ):
                write_json_exclusive(path, {"ok": True})
            self.assertEqual(path.read_text(encoding="utf-8"), "replacement")

    def test_partial_temporary_write_never_exposes_final_event_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "event.json"
            real_write = os.write
            calls = 0

            def short_then_fail(descriptor, payload):
                nonlocal calls
                calls += 1
                if calls == 1:
                    partial = bytes(payload[: max(1, len(payload) // 2)])
                    return real_write(descriptor, partial)
                raise OSError(errno.EIO, "injected partial write")

            with (
                mock.patch(
                    "tools.data_factory.curator.core.filesystem.os.write",
                    side_effect=short_then_fail,
                ),
                self.assertRaisesRegex(CuratorError, "EVENT_WRITE"),
            ):
                write_json_exclusive(path, {"payload": "x" * 100})
            self.assertFalse(path.exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_event_rename_rejects_substituted_temp_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "event.json"
            original_rename = filesystem._rename_noreplace_syscall
            substituted = False

            def substitute_then_rename(source_fd, source_name, target_fd, target_name):
                nonlocal substituted
                if target_name == target.name and not substituted:
                    os.rename(
                        source_name,
                        "parked-original",
                        src_dir_fd=source_fd,
                        dst_dir_fd=source_fd,
                    )
                    replacement = os.open(
                        source_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o400,
                        dir_fd=source_fd,
                    )
                    try:
                        os.write(replacement, b"replacement")
                    finally:
                        os.close(replacement)
                    substituted = True
                return original_rename(
                    source_fd,
                    source_name,
                    target_fd,
                    target_name,
                )

            with (
                mock.patch.object(
                    filesystem,
                    "_rename_noreplace_syscall",
                    side_effect=substitute_then_rename,
                ),
                self.assertRaisesRegex(CuratorError, "EVENT_COMMIT_AMBIGUOUS"),
            ):
                write_json_exclusive(target, {"trusted": True})
            self.assertFalse(target.exists())
            replacements = [
                path
                for path in root.iterdir()
                if path.name.endswith(".tmp") and path.read_bytes() == b"replacement"
            ]
            self.assertEqual(len(replacements), 1)

    def test_owned_cleanup_never_recurses_into_replacement_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "owned.bin").write_bytes(b"owned")
            owned = OwnedDirectory.capture(candidate)
            original_fwalk = os.fwalk
            substituted = False

            def substitute_then_walk(*args, **kwargs):
                nonlocal substituted
                if not substituted:
                    staged = next(root.glob("*.curator-delete"))
                    staged.rename(root / "parked-owned")
                    staged.mkdir()
                    (staged / "sentinel").write_bytes(b"replacement")
                    substituted = True
                return original_fwalk(*args, **kwargs)

            with (
                mock.patch.object(os, "fwalk", side_effect=substitute_then_walk),
                self.assertRaisesRegex(CuratorError, "OWNED_DIRECTORY_CHANGED"),
            ):
                remove_owned_directory(owned)
            replacement = next(root.glob("*.curator-delete"))
            self.assertEqual((replacement / "sentinel").read_bytes(), b"replacement")
            self.assertTrue(substituted)

    def test_cross_directory_rename_fsyncs_both_parents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_parent = root / "source-parent"
            target_parent = root / "target-parent"
            source_parent.mkdir()
            target_parent.mkdir()
            source = source_parent / "candidate"
            source.mkdir()
            expected = {
                (source_parent.stat().st_dev, source_parent.stat().st_ino),
                (target_parent.stat().st_dev, target_parent.stat().st_ino),
            }
            observed: set[tuple[int, int]] = set()
            real_fsync = os.fsync

            def record_fsync(descriptor):
                details = os.fstat(descriptor)
                if (details.st_dev, details.st_ino) in expected:
                    observed.add((details.st_dev, details.st_ino))
                return real_fsync(descriptor)

            with mock.patch.object(os, "fsync", side_effect=record_fsync):
                rename_noreplace_at(
                    OwnedDirectory.capture(source), target_parent / "published"
                )
            self.assertEqual(observed, expected)


if __name__ == "__main__":
    unittest.main()
