from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.data_factory.curator.core.jsonio import (
    CuratorError,
    assert_tree_identity,
    file_sha256,
    rename_noreplace,
    stable_tree_identity,
    tree_identity,
    tree_snapshot,
)


class ContractsTest(unittest.TestCase):
    def test_file_sha256_streams_without_materializing_the_file(self):
        payload = b"curator-streaming-digest" * 1024
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(payload)
            with mock.patch(
                "tools.data_factory.curator.core.jsonio.read_regular_bytes",
                side_effect=AssertionError("large files must not be materialized"),
            ):
                self.assertEqual(
                    file_sha256(path),
                    "sha256:" + hashlib.sha256(payload).hexdigest(),
                )

    def test_tree_identity_detects_same_size_preserved_mtime_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "payload.bin"
            path.write_bytes(b"before")
            snapshot = tree_snapshot(root)
            digest, _files = tree_identity(root)
            details = path.stat()
            path.write_bytes(b"change")
            os.utime(path, ns=(details.st_atime_ns, details.st_mtime_ns))
            self.assertEqual(tree_snapshot(root), snapshot)
            with self.assertRaisesRegex(CuratorError, "SOURCE_CHANGED"):
                assert_tree_identity(
                    root,
                    snapshot,
                    digest,
                    code="SOURCE_CHANGED",
                )

    def test_stable_identity_reuses_the_pre_hash_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "payload.bin").write_bytes(b"payload")
            expected, _files = tree_identity(root)
            with mock.patch(
                "tools.data_factory.curator.core.jsonio.tree_snapshot",
                wraps=tree_snapshot,
            ) as snapshot_call:
                snapshot, digest = stable_tree_identity(root, code="SOURCE_CHANGED")
            self.assertEqual(snapshot_call.call_count, 2)
            self.assertEqual(digest, expected)
            self.assertEqual(snapshot, tree_snapshot(root))

    def test_rename_reports_non_exists_errno_as_publish_failure(self):
        class FailingRename:
            argtypes = None
            restype = None

            def __call__(self, *_args):
                return -1

        library = type("Library", (), {"renameat2": FailingRename()})()
        with (
            mock.patch("tools.data_factory.curator.core.jsonio.ctypes.CDLL", return_value=library),
            mock.patch("tools.data_factory.curator.core.jsonio.ctypes.get_errno", return_value=errno.EROFS),
        ):
            with self.assertRaises(CuratorError) as raised:
                rename_noreplace(
                    "/tmp/source",
                    "/tmp/target",
                    exists_code="OUTPUT_EXISTS",
                    failure_code="OUTPUT_PUBLISH",
                )
        self.assertEqual(raised.exception.code, "OUTPUT_PUBLISH")


if __name__ == "__main__":
    unittest.main()
