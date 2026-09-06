from __future__ import annotations

import io
import json
from pathlib import Path
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tools.data_factory.curator.cli import _parser, main


class CliTest(unittest.TestCase):
    def test_training_request_passes_explicit_comparison_cohort(self):
        with mock.patch("tools.data_factory.curator.cli.export_training_request", return_value={}) as call, redirect_stdout(io.StringIO()):
            main(["training-request", "--run-dir", "/runs/a", "--dataset-id", "comparison",
                  "--output", "/outputs/request.json", "--eval-split", "0.3",
                  "--expected-eval-episode", "8", "--expected-eval-episode", "9"])
        self.assertEqual(call.call_args.kwargs, {"dataset_id": "comparison", "eval_split": .3,
                                                "expected_eval_episodes": [8, 9]})

    def test_training_request_keeps_explicit_selection_and_nonapproval_boundary(self):
        output = io.StringIO()
        with (
            mock.patch(
                "tools.data_factory.curator.cli.export_training_request",
                return_value={"status": "REQUEST_NOT_APPROVED", "training_authority": False},
            ) as call,
            redirect_stdout(output),
        ):
            main([
                "training-request", "--run-dir", "/runs/b", "--run-dir", "/runs/a",
                "--dataset-id", "selection-r1", "--output", "/outputs/request.json",
            ])
        call.assert_called_once_with(
            [Path("/runs/b"), Path("/runs/a")], Path("/outputs/request.json"),
            dataset_id="selection-r1",
        )
        self.assertFalse(json.loads(output.getvalue())["training_authority"])

    def test_only_supported_commands_and_no_abbreviated_flags(self):
        parser = _parser()
        self.assertEqual(
            set(parser._subparsers._group_actions[0].choices),
            {"prepare", "status", "decide", "setup", "training-request"},
        )
        setup = parser.parse_args(["setup", "export", "--source", "/tmp/source"])
        self.assertEqual(setup.profile_id, "fr5-up-wrist-fixed-view-r003")
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

    def test_setup_preview_dispatches_without_training_authority(self):
        output = io.StringIO()
        with (
            mock.patch(
                "tools.data_factory.curator.cli.setup_paths", return_value="paths"
            ),
            mock.patch(
                "tools.data_factory.curator.cli.preview_profile_setup",
                return_value={"ok": True, "training_authority": False},
            ) as call,
            redirect_stdout(output),
        ):
            main(["setup", "preview", "--run", "setup-1"])
        call.assert_called_once_with("setup-1", _paths="paths")
        self.assertFalse(json.loads(output.getvalue())["training_authority"])

    def test_setup_finalize_binds_the_exact_preview(self):
        output = io.StringIO()
        with (
            mock.patch(
                "tools.data_factory.curator.cli.setup_paths", return_value="paths"
            ),
            mock.patch(
                "tools.data_factory.curator.cli.finalize_profile_setup",
                return_value={"ok": True, "training_authority": False},
            ) as call,
            redirect_stdout(output),
        ):
            main(
                [
                    "setup",
                    "finalize",
                    "--run",
                    "setup-1",
                    "--preview",
                    "preview-1",
                ]
            )
        call.assert_called_once_with("setup-1", "preview-1", _paths="paths")
        self.assertFalse(json.loads(output.getvalue())["training_authority"])


if __name__ == "__main__":
    unittest.main()
