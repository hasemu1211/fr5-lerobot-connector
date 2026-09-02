"""CLI routing for the optional curator vertical slice."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys

from tools.curator.approval import issue_approval
from tools.curator.contracts import CuratorError
from tools.curator.derive import derive_dataset
from tools.curator.verify import create_review_bundle, export_reference


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CuratorError("CLI_ARGUMENTS", message)


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export-reference", help="export one exact official-reader up frame")
    export.add_argument("--source", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--frame-index", type=int, required=True)
    export.add_argument("--source-repo-id", default="local/curator-source")

    preview = commands.add_parser("preview-profile", help="create an immutable raw/overlay/policy review bundle")
    preview.add_argument("--source", type=Path, required=True)
    preview.add_argument("--profile", type=Path, required=True)
    preview.add_argument("--source-repo-id", default="local/curator-source")

    approve = commands.add_parser("approve-profile", help="issue exact controlling-TTY human task-view approval")
    approve.add_argument("--profile", type=Path, required=True)
    approve.add_argument("--approved-by", required=True)

    derive = commands.add_parser("derive", help="create a verified isolated LeRobot v3 root")
    derive.add_argument("--source", type=Path, required=True)
    derive.add_argument("--output", type=Path, required=True)
    derive.add_argument("--profile", type=Path, required=True)
    derive.add_argument("--approval", type=Path, required=True)
    derive.add_argument("--run-root", type=Path, default=Path("outputs/curator/runs"))
    derive.add_argument("--run-id")
    derive.add_argument("--source-repo-id", default="local/curator-source")
    derive.add_argument("--output-repo-id")
    return parser


def _run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"curator-{stamp}-{secrets.token_hex(4)}"


def main(argv: list[str] | None = None) -> None:
    try:
        args = _parser().parse_args(argv)
        if args.command == "export-reference":
            result = export_reference(
                args.source,
                args.output,
                args.frame_index,
                source_repo_id=args.source_repo_id,
            )
        elif args.command == "preview-profile":
            result = create_review_bundle(
                args.source,
                args.profile,
                source_repo_id=args.source_repo_id,
            )
        elif args.command == "approve-profile":
            result = issue_approval(args.profile, args.approved_by)
        else:
            run_id = args.run_id or _run_id()
            output_repo_id = args.output_repo_id or f"local/{args.output.name}"
            result = derive_dataset(
                args.source,
                args.output,
                args.profile,
                args.approval,
                run_dir=args.run_root / run_id,
                run_id=run_id,
                source_repo_id=args.source_repo_id,
                output_repo_id=output_repo_id,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except CuratorError as exc:
        print(
            json.dumps(
                {"ok": False, "reason_code": exc.code, "detail": exc.detail},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason_code": "UNEXPECTED_RUNTIME_FAILURE",
                    "detail": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


__all__ = ["main"]
