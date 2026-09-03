"""CLI for the optional v1.2 curator workflow."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from .core.errors import CuratorError
from .workflow.application import decide, prepare
from .workflow.state import project_state

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None: raise CuratorError("CLI_ARGUMENTS", message)

def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("prepare")
    make.add_argument("--source", type=Path, required=True)
    make.add_argument("--run-root", type=Path, default=Path("outputs/curator/runs"))
    make.add_argument("--output-parent", type=Path, default=Path("datasets/fr5_curated"))
    make.add_argument("--view-profile-root", type=Path, default=Path("config/data_factory/curator/view_profiles"))
    make.add_argument("--review-policy-root", type=Path, default=Path("config/data_factory/curator/review_policies"))
    make.add_argument("--profile-id"); make.add_argument("--policy-id")
    for name in ("status", "decide"):
        command = commands.add_parser(name); command.add_argument("--run", required=True)
    return parser

def main(argv: list[str] | None = None) -> None:
    try:
        args = _parser().parse_args(argv)
        if args.command == "prepare":
            result = prepare(args.source, run_root=args.run_root, output_parent=args.output_parent, view_profile_root=args.view_profile_root, review_policy_root=args.review_policy_root, profile_id=args.profile_id, policy_id=args.policy_id)
        elif args.command == "status": result = project_state(Path("outputs/curator/runs") / args.run)
        else: result = decide(Path("outputs/curator/runs") / args.run)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except CuratorError as exc:
        print(json.dumps({"ok":False,"reason_code":exc.code,"detail":exc.detail}, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        print(json.dumps({"ok":False,"reason_code":"UNEXPECTED_RUNTIME_FAILURE","detail":type(exc).__name__}, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        raise SystemExit(2) from None

__all__ = ["main"]
