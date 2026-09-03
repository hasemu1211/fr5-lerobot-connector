from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.data_factory.curator.cli import main


class CliTest(unittest.TestCase):
    def _failure(self, argv: list[str]) -> tuple[dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaisesRegex(SystemExit, "2"):
                main(argv)
        self.assertEqual(stdout.getvalue(), "")
        text = stderr.getvalue()
        self.assertNotIn("Traceback", text)
        return json.loads(text), text

    def test_missing_path_is_stable_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing-profile.json"
            failure, _text = self._failure([
                "approve-profile", "--profile", str(missing), "--approved-by", "operator-1",
            ])
        self.assertEqual(failure["reason_code"], "PROFILE_PATH")
        self.assertIs(failure["ok"], False)

    def test_unexpected_runtime_failure_is_stable_json(self):
        with mock.patch(
            "tools.data_factory.curator.cli.create_review_bundle",
            side_effect=RuntimeError("unstable internal detail"),
        ):
            failure, _text = self._failure([
                "preview-profile", "--source", "/missing/source", "--profile", "/missing/profile",
            ])
        self.assertEqual(failure, {
            "detail": "RuntimeError",
            "ok": False,
            "reason_code": "UNEXPECTED_RUNTIME_FAILURE",
        })

    def test_argument_failure_is_stable_json(self):
        failure, _text = self._failure(["derive"])
        self.assertEqual(failure["reason_code"], "CLI_ARGUMENTS")


if __name__ == "__main__":
    unittest.main()
