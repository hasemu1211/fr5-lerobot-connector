from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.data_factory.curator.dataset import verify
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.dataset.verify import (
    run_existing_validator,
    verify_h264,
)


class VerifyTest(unittest.TestCase):
    def test_h264_codec_and_exact_file_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = Path("videos/chunk-000/up.mp4")
            path = root / relative
            path.parent.mkdir(parents=True)
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=16x16:r=10:d=0.1",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(path),
                ],
                check=True,
            )
            self.assertEqual(
                verify_h264(root, {relative.as_posix()}), [relative.as_posix()]
            )
            with self.assertRaisesRegex(CuratorError, "DERIVED_H264_FILE_SET"):
                verify_h264(root, {"videos/other.mp4"})

    def test_external_validation_processes_have_bounded_waits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = Path("videos/chunk-000/up.mp4")
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_bytes(b"not-read-while-mocked")
            with (
                mock.patch.object(
                    verify.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired("ffprobe", 30),
                ) as run,
                self.assertRaisesRegex(CuratorError, "DERIVED_H264"),
            ):
                verify_h264(root, {relative.as_posix()})
            self.assertEqual(run.call_args.kwargs["timeout"], 30)

            with (
                mock.patch.object(
                    verify.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired("validator", 300),
                ) as run,
                self.assertRaisesRegex(CuratorError, "EXISTING_VALIDATOR_TIMEOUT"),
            ):
                run_existing_validator(root, "local/source")
            self.assertEqual(run.call_args.kwargs["timeout"], 300)


if __name__ == "__main__":
    unittest.main()
