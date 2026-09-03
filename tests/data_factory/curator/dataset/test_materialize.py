from __future__ import annotations

import inspect
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tools.data_factory.curator.core import filesystem
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.core.filesystem import write_json_exclusive
from tools.data_factory.curator.dataset.materialize import (
    _cleanup_owned,
    _identity,
    _owner_value,
    _require_spawn_parallel_encoding,
    _remove_owner_marker,
    materialize_candidate,
)


class MaterializeTest(unittest.TestCase):
    def test_candidate_api_has_resolved_profile_and_no_approval_or_publish_argument(
        self,
    ):
        parameters = inspect.signature(materialize_candidate).parameters
        self.assertIn("resolved_profile", parameters)
        self.assertIn("expected_source_digest", parameters)
        self.assertNotIn("approval", parameters)
        self.assertNotIn("output_repo_id", parameters)

    def test_parallel_encoding_requires_spawn(self):
        with (
            mock.patch(
                "tools.data_factory.curator.dataset.materialize.multiprocessing.get_start_method",
                return_value="fork",
            ),
            self.assertRaisesRegex(CuratorError, "PARALLEL_ENCODING_REQUIRES_SPAWN"),
        ):
            _require_spawn_parallel_encoding()

    def test_cleanup_refuses_temporary_or_marker_path_substitution(self):
        for substitution in ("temporary", "marker"):
            with (
                self.subTest(substitution=substitution),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                temporary = root / ".candidate.run.curator-tmp"
                marker = root / ".candidate.run.curator-owner.json"
                candidate = root / ".candidate"
                temporary.mkdir()
                (temporary / "payload").write_text("owned", encoding="utf-8")
                owner = _owner_value("run", candidate, "sha256:" + "1" * 64)
                write_json_exclusive(marker, owner)
                temporary_identity = _identity(
                    temporary,
                    stat.S_IFDIR,
                    "TEMP_DIRECTORY_IDENTITY",
                )
                marker_identity = _identity(
                    marker,
                    stat.S_IFREG,
                    "TEMP_OWNER_IDENTITY",
                )
                moved = root / f"moved-{substitution}"
                if substitution == "temporary":
                    temporary.rename(moved)
                    temporary.mkdir()
                    (temporary / "replacement").write_text("keep", encoding="utf-8")
                else:
                    marker.rename(moved)
                    marker.write_text('{"replacement":"keep"}', encoding="utf-8")

                self.assertFalse(
                    _cleanup_owned(
                        temporary,
                        marker,
                        owner,
                        temporary_identity,
                        marker_identity,
                    )
                )
                self.assertTrue(moved.exists())
                if substitution == "temporary":
                    self.assertEqual(
                        (temporary / "replacement").read_text(encoding="utf-8"),
                        "keep",
                    )
                    self.assertTrue(marker.exists())
                else:
                    self.assertEqual(
                        marker.read_text(encoding="utf-8"),
                        '{"replacement":"keep"}',
                    )
                    self.assertTrue(temporary.exists())

    def test_owner_marker_check_use_substitution_preserves_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / ".candidate.run.curator-owner.json"
            candidate = root / ".candidate"
            owner = _owner_value("run", candidate, "sha256:" + "1" * 64)
            write_json_exclusive(marker, owner)
            identity = _identity(marker, stat.S_IFREG, "TEMP_OWNER_IDENTITY")
            original_rename = filesystem._rename_noreplace_syscall
            substituted = False

            def substitute_marker(source_fd, source_name, target_fd, target_name):
                nonlocal substituted
                if source_name == marker.name and not substituted:
                    os.rename(
                        source_name,
                        "parked-marker",
                        src_dir_fd=source_fd,
                        dst_dir_fd=source_fd,
                    )
                    replacement = os.open(
                        source_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
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

            with mock.patch.object(
                filesystem,
                "_rename_noreplace_syscall",
                side_effect=substitute_marker,
            ):
                result = _remove_owner_marker(marker, owner, identity)
            self.assertEqual(result, "RETAINED_IDENTITY_AMBIGUOUS")
            self.assertTrue(substituted)
            self.assertTrue((root / "parked-marker").is_file())
            replacements = [
                path
                for path in root.glob("*.curator-delete")
                if path.read_bytes() == b"replacement"
            ]
            self.assertEqual(len(replacements), 1)


if __name__ == "__main__":
    unittest.main()
