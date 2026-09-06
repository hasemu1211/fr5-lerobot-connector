"""CLI for the optional curator workflow and one-time profile setup."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core.errors import CuratorError
from .core.jsonio import load_json
from .workflow.application import decide, prepare, status
from .workflow.selection import export_training_request
from .workflow.setup import (
    DEFAULT_DILATION_MARGIN_PX,
    DEFAULT_PLATE_FRAME_COUNT,
    DEFAULT_PROFILE_ID,
    export_profile_setup,
    finalize_profile_setup,
    preview_profile_setup,
    setup_paths,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CuratorError("CLI_ARGUMENTS", message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("prepare", allow_abbrev=False)
    make.add_argument("--source", type=Path, required=True)
    selection = commands.add_parser("training-request", allow_abbrev=False)
    selection.add_argument("--run-dir", type=Path, action="append", required=True)
    selection.add_argument("--output", type=Path, required=True)
    selection.add_argument("--dataset-id", required=True)
    selection.add_argument("--derivation", type=Path, help="published Curator reference JSON; parent evidence remains parent-only")
    selection.add_argument("--eval-split", type=float)
    selection.add_argument("--expected-eval-episode", type=int, action="append")
    for name in ("status", "decide"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--run", required=True)
    setup = commands.add_parser("setup", allow_abbrev=False)
    setup_commands = setup.add_subparsers(dest="setup_command", required=True)
    export = setup_commands.add_parser("export", allow_abbrev=False)
    export.add_argument("--source", type=Path, required=True)
    export.add_argument("--profile-id", default=DEFAULT_PROFILE_ID)
    export.add_argument("--reference-index", type=int)
    export.add_argument("--fit-split", type=Path,
                        help="native TRAIN split for reference/background fitting; grants no authority")
    export.add_argument(
        "--dilation-margin-px", type=int, default=DEFAULT_DILATION_MARGIN_PX
    )
    export.add_argument("--plate-frames", type=int, default=DEFAULT_PLATE_FRAME_COUNT)
    for name in ("preview", "finalize"):
        command = setup_commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--run", required=True)
        if name == "finalize":
            command.add_argument("--preview", required=True)
    for command in setup_commands.choices.values():
        command.add_argument("--repository", type=Path)
    return parser


def main(argv: list[str] | None = None) -> None:
    try:
        args = _parser().parse_args(argv)
        if args.command == "setup":
            paths = setup_paths(args.repository) if args.repository else setup_paths()
            if args.setup_command == "export":
                result = export_profile_setup(
                    args.source,
                    profile_id=args.profile_id,
                    reference_frame_index=args.reference_index,
                    dilation_margin_px=args.dilation_margin_px,
                    plate_frame_count=args.plate_frames,
                    fit_split=args.fit_split,
                    _paths=paths,
                )
            elif args.setup_command == "preview":
                result = preview_profile_setup(args.run, _paths=paths)
            else:
                result = finalize_profile_setup(args.run, args.preview, _paths=paths)
        elif args.command == "training-request":
            cohort = {} if args.eval_split is None and args.expected_eval_episode is None else {
                "eval_split": args.eval_split,
                "expected_eval_episodes": args.expected_eval_episode,
            }
            result = export_training_request(
                args.run_dir, args.output, dataset_id=args.dataset_id, **cohort,
                **({"derivation": load_json(args.derivation, code="DERIVATION_REFERENCE_JSON")} if args.derivation else {}),
            )
        elif args.command == "prepare":
            result = prepare(args.source)
        elif args.command == "status":
            result = status(args.run)
        else:
            result = decide(args.run)
        print(
            json.dumps(
                result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
    except CuratorError as exc:
        print(
            json.dumps(
                {"ok": False, "reason_code": exc.code, "detail": exc.detail},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        print(
            json.dumps(
                {"ok": False, "reason_code": "INTERRUPTED"}, separators=(",", ":")
            ),
            file=sys.stderr,
        )
        raise SystemExit(130) from None
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason_code": "UNEXPECTED_RUNTIME_FAILURE",
                    "detail": type(exc).__name__,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


__all__ = ["main"]
