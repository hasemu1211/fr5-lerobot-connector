from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest import mock

from tools.data_factory.curator.cli import _parser, main


class CliTest(unittest.TestCase):
    def test_only_prepare_status_decide_and_no_abbreviated_flags(self):
        parser = _parser()
        self.assertEqual(
            set(parser._subparsers._group_actions[0].choices),
            {"prepare", "status", "decide"},
        )
        for arguments in (["derive"], ["prepare", "--sou", "/tmp/source"]):
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(arguments)
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(
                json.loads(stderr.getvalue())["reason_code"], "CLI_ARGUMENTS"
            )

    def test_status_dispatches_through_public_application_boundary(self):
        output = io.StringIO()
        with (
            mock.patch(
                "tools.data_factory.curator.cli.status",
                return_value={"ok": True, "status": "REVIEW_READY"},
            ) as call,
            redirect_stdout(output),
        ):
            main(["status", "--run", "run-1"])
        call.assert_called_once_with("run-1")
        self.assertEqual(json.loads(output.getvalue())["status"], "REVIEW_READY")


if __name__ == "__main__":
    unittest.main()
