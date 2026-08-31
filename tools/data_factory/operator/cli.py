"""Foreground collection-operator process entrypoint."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.data_factory.operator.composition import build_operator_runtime
from tools.data_factory.operator.workflow.campaign import DEFAULT_GRIPPER_RETUNE
from tools.fr5_data_factory import ContractError


DEFAULT_JOB = (
    "config/data_factory/test_only_physical/goal2-place1/"
    "center-live-p45-20260821-r001.job.json"
)


def _serve(**kwargs) -> int:
    runtime = None
    effect_scope = kwargs.get("effect_scope", "FAKE")
    try:
        runtime = build_operator_runtime(**kwargs)
        print(json.dumps(runtime.announcement, sort_keys=True), flush=True)
        runtime.bridge.serve_forever(startup_call=runtime.startup_call)
    except KeyboardInterrupt:
        return 130
    except (ContractError, OSError) as exc:
        code = exc.code if isinstance(exc, ContractError) else (
            "FAKE_CONSOLE_FAILED"
            if effect_scope == "FAKE" else "OPERATOR_CONSOLE_FAILED"
        )
        print(json.dumps({"error": {"code": code, "message": str(exc)}}, sort_keys=True), flush=True)
        return 2
    finally:
        if runtime is not None:
            runtime.close()
    return 0


def _fake_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the existing operator UI over a foreground FAKE LoopbackBridge",
    )
    parser.add_argument("--port", type=int, default=4174)
    parser.add_argument(
        "--fixture-root",
        help=(
            "Synthetic directory containing hypothesis.json and draft.json; "
            "omitted uses the built-in fixture in a cleaned temporary root"
        ),
    )
    return parser


def _fake_main(argv=None) -> int:
    args = _fake_parser().parse_args(argv)
    return _serve(
        effect_scope="FAKE", port=args.port, fixture_root=args.fixture_root,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve the reusable foreground FR5 collection operator",
    )
    parser.add_argument("--effect-scope", choices=("FAKE", "PHYSICAL"), default="FAKE")
    parser.add_argument("--port", type=int, default=4174)
    parser.add_argument(
        "--repository-root", default=str(Path(__file__).resolve().parents[3]),
    )
    parser.add_argument("--session-id")
    parser.add_argument("--operator-label", default="local-operator")
    parser.add_argument("--camera-device-id")
    parser.add_argument(
        "--job", default=DEFAULT_JOB,
        help="Repository-relative qualified job used only for the initial selection",
    )
    parser.add_argument(
        "--gripper-retune", default=DEFAULT_GRIPPER_RETUNE,
        help="Repository-relative object/grasp TEST_ONLY gripper retune",
    )
    parser.add_argument(
        "--no-auto-prepare", action="store_true",
        help="Show discovery facts without starting missing foreground children",
    )
    args = parser.parse_args(argv)
    values = vars(args)
    values["auto_prepare"] = not values.pop("no_auto_prepare")
    if args.effect_scope == "FAKE":
        values = {
            "effect_scope": "FAKE", "port": args.port,
            "fixture_root": None,
        }
    return _serve(**values)


if __name__ == "__main__":
    raise SystemExit(main())
