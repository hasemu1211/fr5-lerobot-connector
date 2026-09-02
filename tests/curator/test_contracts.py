from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.curator.contracts import file_sha256


class ContractsTest(unittest.TestCase):
    def test_file_sha256_streams_without_materializing_the_file(self):
        payload = b"curator-streaming-digest" * 1024
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(payload)
            with mock.patch(
                "tools.curator.contracts.read_regular_bytes",
                side_effect=AssertionError("large files must not be materialized"),
            ):
                self.assertEqual(
                    file_sha256(path),
                    "sha256:" + hashlib.sha256(payload).hexdigest(),
                )


if __name__ == "__main__":
    unittest.main()
