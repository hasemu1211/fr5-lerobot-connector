from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.core.identity import (
    assert_tree_identity,
    file_sha256,
    stable_tree_identity,
    tree_identity,
    tree_snapshot,
)


class IdentityTest(unittest.TestCase):
    def test_file_digest_streams_and_stable_identity_reuses_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"curator-stream" * 1024
            (root / "payload.bin").write_bytes(payload)
            self.assertEqual(
                file_sha256(root / "payload.bin"),
                "sha256:" + hashlib.sha256(payload).hexdigest(),
            )
            expected, _files = tree_identity(root)
            with mock.patch(
                "tools.data_factory.curator.core.identity.tree_snapshot",
                wraps=tree_snapshot,
            ) as snapshot_call:
                _snapshot, digest = stable_tree_identity(root, code="CHANGED")
            self.assertEqual(snapshot_call.call_count, 2)
            self.assertEqual(digest, expected)

    def test_same_size_preserved_mtime_mutation_is_detected(self):
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
                assert_tree_identity(root, snapshot, digest, code="SOURCE_CHANGED")


if __name__ == "__main__":
    unittest.main()
