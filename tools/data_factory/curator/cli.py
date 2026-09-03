"""CLI for the optional v1.2 curator workflow."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from .core.errors import CuratorError
from .workflow.application import decide, prepare, status


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CuratorError("CLI_ARGUMENTS", message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("prepare", allow_abbrev=False)
    make.add_argument("--source", type=Path, required=True)
    for name in ("status", "decide"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--run", required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    try:
        args = _parser().parse_args(argv)
        if args.command == "prepare":
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
