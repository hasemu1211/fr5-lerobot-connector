from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.data_factory.curator.core import filesystem
from tools.data_factory.curator.core.filesystem import OwnedDirectory
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.dataset.publish import (
    candidate_identity,
    cleanup_candidate,
    commit_hidden_candidate,
    publish_candidate,
)


class PublishTest(unittest.TestCase):
    def test_hidden_commit_final_publish_and_exact_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temporary = root / "temporary"
            temporary.mkdir()
            (temporary / "payload.bin").write_bytes(b"candidate")
            owned_temporary = OwnedDirectory.capture(temporary)
            snapshot, digest = candidate_identity(owned_temporary)
            candidate = root / ".candidate"
            owned = commit_hidden_candidate(
                owned_temporary,
                candidate,
                expected_snapshot=snapshot,
            )
            verified_snapshot, verified_digest = candidate_identity(owned)
            self.assertEqual(verified_digest, digest)
            output = root / "published"
            publish_candidate(
                owned,
                output,
                digest,
                verified_snapshot=verified_snapshot,
            )
            self.assertEqual((output / "payload.bin").read_bytes(), b"candidate")
            self.assertFalse(candidate.exists())

            reject = root / ".reject"
            reject.mkdir()
            (reject / "payload.bin").write_bytes(b"reject")
            reject_owned = OwnedDirectory.capture(reject)
            _snapshot, reject_digest = candidate_identity(reject_owned)
            cleanup_candidate(reject_owned, reject_digest)
            self.assertFalse(reject.exists())

    def test_publish_rehashes_same_size_mtime_restored_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / ".candidate"
            candidate.mkdir()
            payload = candidate / "payload.bin"
            payload.write_bytes(b"reviewed")
            owned = OwnedDirectory.capture(candidate)
            snapshot, digest = candidate_identity(owned)
            previous = payload.stat()
            payload.write_bytes(b"tampered")
            os.utime(payload, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            self.assertEqual(snapshot, candidate_identity(owned)[0])

            output = root / "published"
            with self.assertRaisesRegex(CuratorError, "CANDIDATE_DIGEST_CHANGED"):
                publish_candidate(
                    owned,
                    output,
                    digest,
                    verified_snapshot=snapshot,
                )
            self.assertTrue(candidate.is_dir())
            self.assertFalse(output.exists())

    def test_publish_never_exposes_a_substituted_staging_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / ".candidate"
            candidate.mkdir()
            (candidate / "payload.bin").write_bytes(b"reviewed")
            owned = OwnedDirectory.capture(candidate)
            snapshot, digest = candidate_identity(owned)
            output = root / "published"
            original_rename = filesystem._rename_noreplace_syscall
            substituted = False

            def substitute_before_publish(
                source_fd, source_name, target_fd, target_name
            ):
                nonlocal substituted
                if target_name == output.name and not substituted:
                    os.rename(
                        source_name,
                        "parked-reviewed",
                        src_dir_fd=source_fd,
                        dst_dir_fd=source_fd,
                    )
                    os.mkdir(source_name, dir_fd=source_fd)
                    replacement_fd = os.open(
                        source_name,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                        dir_fd=source_fd,
                    )
                    try:
                        sentinel = os.open(
                            "sentinel",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=replacement_fd,
                        )
                        os.close(sentinel)
                    finally:
                        os.close(replacement_fd)
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
                    side_effect=substitute_before_publish,
                ),
                self.assertRaisesRegex(
                    CuratorError, "OUTPUT_COMMITTED_IDENTITY_AMBIGUOUS"
                ),
            ):
                publish_candidate(
                    owned,
                    output,
                    digest,
                    verified_snapshot=snapshot,
                )
            self.assertTrue(substituted)
            self.assertFalse(output.exists())
            self.assertEqual(
                (root / "parked-reviewed/payload.bin").read_bytes(), b"reviewed"
            )
            replacement = next(root.glob(".curator-publish-*.stage"))
            self.assertTrue((replacement / "sentinel").is_file())


if __name__ == "__main__":
    unittest.main()
