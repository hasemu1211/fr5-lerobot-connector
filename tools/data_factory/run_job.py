#!/usr/bin/env python3
"""Canonical human/AI entrypoint for one side-effect-free factory plan."""
from __future__ import annotations

import copy
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.data_factory.one_job import JsonlProcess, OneJob
from tools.data_factory.scene_state import SceneStateStore
from tools.fr5_data_factory import (
    ContractArgumentParser,
    ContractError,
    SAFE_ID,
    canonical_digest,
    load_json_strict,
    resolve_motion_program,
    validate_job_spec,
)


COMMAND_SCHEMA = "data_factory.run_job.command.v1"
RESPONSE_SCHEMA = "data_factory.run_job.response.v1"
EVENT_SCHEMA = "data_factory.run_job.event.v1"
CONTROL_QUEUE_MAX = 32
COMMAND_KEYS = {"schema_version", "op_id", "op", "payload"}
COMMON_RUN_KEYS = {
    "mode", "run_id", "job", "selected_sheet", "yaw0_sheet", "config_root",
    "motion_qualification", "home_candidate", "urdf", "expected_robot_system_id",
}
LIVE_RUN_KEYS = COMMON_RUN_KEYS | {"camera_profile", "dataset_root", "run_root"}
RESPONSE_KEYS = {"schema_version", "op_id", "op", "ok", "code", "state", "run_id", "plan_digest", "data"}
EVENT_KEYS = {"schema_version", "event", "sequence", "origin_op_id", "ok", "code", "state", "run_id", "plan_digest", "data"}
ROOT = Path(__file__).resolve().parents[2]


def _exact(value, keys, code):
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(code)
    return value


def _text(value, code):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ContractError(code)
    return value


def _identifier(value, code):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ContractError(code)
    return value


def _response(*, op_id=None, op=None, ok=False, code="ERROR", state="IDLE", run_id=None, plan_digest=None, data=None):
    return {
        "schema_version": RESPONSE_SCHEMA,
        "op_id": op_id,
        "op": op,
        "ok": ok,
        "code": code,
        "state": state,
        "run_id": run_id,
        "plan_digest": plan_digest,
        "data": data,
    }


def _event(response, origin_op_id):
    value = {
        "schema_version": EVENT_SCHEMA,
        "event": "RESULT",
        "sequence": 1,
        "origin_op_id": origin_op_id,
        **{key: copy.deepcopy(response[key]) for key in ("ok", "code", "state", "run_id", "plan_digest", "data")},
    }
    _exact(value, EVENT_KEYS, "RUNNER_EVENT")
    return value


def _run_payload(value):
    if not isinstance(value, dict) or value.get("mode") not in {"plan_only", "live"}:
        raise ContractError("RUN_PAYLOAD")
    keys = COMMON_RUN_KEYS if value["mode"] == "plan_only" else LIVE_RUN_KEYS
    _exact(value, keys, "RUN_PAYLOAD")
    _identifier(value["run_id"], "RUN_ID")
    if not isinstance(value["job"], dict):
        raise ContractError("RUN_JOB")
    for key in keys - {"job"}:
        _text(value[key], "RUN_PAYLOAD")
    return copy.deepcopy(value)


def _command(value):
    _exact(value, COMMAND_KEYS, "COMMAND_SCHEMA")
    if value["schema_version"] != COMMAND_SCHEMA:
        raise ContractError("COMMAND_SCHEMA")
    op_id = _identifier(value["op_id"], "COMMAND_SCHEMA")
    op = value["op"]
    if op == "run":
        payload = _run_payload(value["payload"])
    elif op == "status":
        payload = _exact(value["payload"], {"run_id"}, "STATUS_SCHEMA")
        _identifier(payload["run_id"], "STATUS_SCHEMA")
    elif op == "cancel":
        payload = _exact(value["payload"], {"run_id", "reason_code"}, "CANCEL_SCHEMA")
        _identifier(payload["run_id"], "CANCEL_SCHEMA")
        _identifier(payload["reason_code"], "CANCEL_SCHEMA")
    else:
        raise ContractError("COMMAND_SCHEMA")
    return op_id, op, copy.deepcopy(payload)


def _load(path, code):
    try:
        return load_json_strict(Path(path).read_text(encoding="utf-8"))
    except ContractError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ContractError(code, str(exc)) from exc


def _scene_binding(validated, root=ROOT / "outputs/data_factory/cells"):
    job = validated["normalized_job"]
    snapshot = SceneStateStore(root, job["robot_system_id"]).snapshot()
    pose = {key: job[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")}
    matches = [
        item for item in snapshot["scene_state"]["objects"].values()
        if item.get("object_profile_id") == job["object_profile_id"]
        and item.get("state") == "ON_SURFACE"
        and item.get("pose") == pose
    ]
    if len(matches) != 1:
        raise ContractError("SCENE_OBJECT_NOT_READY" if not matches else "SCENE_OBJECT_AMBIGUOUS")
    return {
        "scene_state_digest": snapshot["scene_state_digest"],
        "revision": snapshot["scene_state"]["revision"],
        "object_instance_id": matches[0]["instance_id"],
    }


def resolve_inputs(payload, *, scene_binding_call=_scene_binding):
    validated = validate_job_spec(
        payload["job"],
        paths={"selected_sheet": payload["selected_sheet"], "yaw0_sheet": payload["yaw0_sheet"]},
        config_root=payload["config_root"],
    )
    if validated["normalized_job"]["task"] != "pickup_e2e":
        raise ContractError("TASK_NOT_SUPPORTED")
    program = resolve_motion_program(
        validated,
        _load(payload["motion_qualification"], "MOTION_QUALIFICATION_IO"),
        _load(payload["home_candidate"], "HOME_CANDIDATE_IO"),
        urdf=payload["urdf"],
        expected_robot_system_id=payload["expected_robot_system_id"],
    )
    return validated, program, scene_binding_call(validated)


def _executor(timeout_s):
    return JsonlProcess(
        [sys.executable, "-u", str(ROOT / "tools/data_factory/motion/pickup_executor.py"), "--factory-jsonl", "--ros-plan-only"],
        timeout_s=timeout_s,
    )


def run_plan_only(payload, cancel, publish, *, resolver=resolve_inputs, executor_factory=_executor):
    """Resolve and plan once; recorder, dataset, camera, and robot execution stay absent."""
    try:
        validated, program, scene_binding = resolver(payload)
        if cancel.is_set():
            return _response(ok=False, code="CANCELLED", state="CANCELLED", run_id=payload["run_id"])
        publish(_response(ok=True, code="PLANNING", state="PLANNING", run_id=payload["run_id"], data={
            "resolved_job_digest": validated["resolved_job_digest"],
            "motion_program_digest": canonical_digest(program),
        }))
        timeout_s = 10.0 + sum(
            float(step["limits"].get("planning_timeout_s", 0))
            for step in program["steps"]
        )
        executor = executor_factory(timeout_s)
        try:
            def recorder_forbidden(_):
                raise ContractError("PLAN_ONLY_RECORDER_FORBIDDEN")

            result = OneJob(recorder_forbidden, lambda request: executor.request(request, cancel)).plan_only(payload["run_id"], program, scene_binding)
        except KeyboardInterrupt:
            cancel.set()
            raise
        finally:
            try:
                executor.close(timeout_s=1.0 if cancel.is_set() else None)
            except ContractError:
                if not cancel.is_set():
                    raise
        if cancel.is_set():
            return _response(ok=False, code="CANCELLED", state="CANCELLED", run_id=payload["run_id"])
        return _response(
            ok=result["ok"],
            code=result["code"],
            state=result["state"],
            run_id=result["run_id"],
            plan_digest=result["plan_digest"],
            data={
                "mode": "plan_only",
                "normalized_job": validated["normalized_job"],
                "resolved_job_digest": validated["resolved_job_digest"],
                "motion_program_digest": canonical_digest(program),
                "scene_binding": scene_binding,
                "camera_semantic_authority": False,
                "training_authorized": False,
            },
        )
    except ContractError as exc:
        return _response(ok=False, code=exc.code, state="BLOCKED", run_id=payload.get("run_id"))
    except Exception as exc:
        return _response(ok=False, code="RUNNER_FAILED", state="BLOCKED", run_id=payload.get("run_id"), data={"detail": str(exc)})


class RunSession:
    """One worker owns child I/O; the main thread owns JSONL output."""

    def __init__(self, run_call=run_plan_only):
        self.run_call = run_call
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.events = queue.Queue(maxsize=1)
        self.worker = None
        self.used = False
        self.origin_op_id = self.run_id = self.cancel_reason = None
        self.snapshot = _response()

    def _publish(self, value):
        with self.lock:
            self.snapshot = copy.deepcopy(value)

    def _work(self, payload):
        try:
            result = self.run_call(payload, self.cancel_event, self._publish)
            _exact(result, RESPONSE_KEYS, "RUNNER_RESULT")
        except ContractError as exc:
            result = _response(code=exc.code, state="BLOCKED", run_id=self.run_id)
        except Exception as exc:
            result = _response(code="RUNNER_FAILED", state="BLOCKED", run_id=self.run_id, data={"detail": str(exc)})
        with self.lock:
            if self.cancel_event.is_set() and (result["ok"] or result["code"] == "CANCELLED"):
                result = _response(ok=False, code=self.cancel_reason or "CANCELLED", state="CANCELLED", run_id=self.run_id, plan_digest=result.get("plan_digest"), data=result.get("data"))
            self.snapshot = copy.deepcopy(result)
        self.events.put(_event(result, self.origin_op_id))

    def process(self, value):
        try:
            op_id, op, payload = _command(value)
        except ContractError as exc:
            return _response(code=exc.code)
        if op == "run":
            if payload["mode"] == "live":
                return _response(op_id=op_id, op=op, code="LIVE_NOT_QUALIFIED", state="REJECTED", run_id=payload["run_id"], data={"camera_semantic_authority": False})
            with self.lock:
                if self.worker is not None and self.worker.is_alive():
                    return _response(op_id=op_id, op=op, code="RUN_ACTIVE", state=self.snapshot["state"], run_id=self.run_id, plan_digest=self.snapshot["plan_digest"])
                if self.used:
                    return _response(op_id=op_id, op=op, code="ONE_JOB_ONLY", state=self.snapshot["state"], run_id=self.run_id, plan_digest=self.snapshot["plan_digest"])
                self.used, self.origin_op_id, self.run_id = True, op_id, payload["run_id"]
                self.snapshot = _response(ok=True, code="RUNNING", state="RUNNING", run_id=self.run_id, data={"mode": "plan_only"})
                self.worker = threading.Thread(target=self._work, args=(payload,), daemon=True)
                self.worker.start()
            return _response(op_id=op_id, op=op, ok=True, code="RUNNING", state="RUNNING", run_id=self.run_id, data={"mode": "plan_only"})
        with self.lock:
            if payload["run_id"] != self.run_id:
                return _response(op_id=op_id, op=op, code="RUN_NOT_FOUND", run_id=payload["run_id"])
            current = copy.deepcopy(self.snapshot)
            active = self.worker is not None and self.worker.is_alive()
            if op == "status":
                return _response(op_id=op_id, op=op, ok=True, code="STATUS", state=current["state"], run_id=self.run_id, plan_digest=current["plan_digest"], data=current["data"])
            if not active:
                return _response(op_id=op_id, op=op, code="CANCEL_STATE", state=current["state"], run_id=self.run_id, plan_digest=current["plan_digest"])
            self.cancel_reason = payload["reason_code"]
            self.cancel_event.set()
            return _response(op_id=op_id, op=op, ok=True, code="CANCEL_REQUESTED", state="CANCEL_REQUESTED", run_id=self.run_id, plan_digest=current["plan_digest"])

    def input_closed(self, reason="INPUT_EOF"):
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                self.cancel_reason = reason
                self.cancel_event.set()
                return True
        return False


def run_jsonl(input_stream, output_stream, session=None):
    session = session or RunSession()
    incoming = queue.Queue(maxsize=CONTROL_QUEUE_MAX)

    def read():
        try:
            for line in input_stream:
                incoming.put(("line", line))
            incoming.put(("eof", None))
        except Exception:
            incoming.put(("error", None))

    threading.Thread(target=read, daemon=True).start()
    eof = False
    terminal_ok = None
    while True:
        try:
            event = session.events.get_nowait()
        except queue.Empty:
            event = None
        if event is not None:
            output_stream.write(json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            output_stream.flush()
            terminal_ok = event["ok"]
            if eof:
                return terminal_ok
        if eof:
            if session.worker is None or not session.worker.is_alive():
                return terminal_ok if terminal_ok is not None else not session.used
            session.worker.join(0.05)
            continue
        try:
            kind, value = incoming.get(timeout=0.05)
        except queue.Empty:
            continue
        if kind == "line":
            try:
                result = session.process(load_json_strict(value))
            except ContractError as exc:
                result = _response(code=exc.code)
            output_stream.write(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            output_stream.flush()
            continue
        eof = True
        if kind == "error" and not session.input_closed("CONTROL_INPUT_FAILED"):
            output_stream.write(json.dumps(_response(code="CONTROL_INPUT_FAILED"), sort_keys=True, separators=(",", ":")) + "\n")
            output_stream.flush()
            return False
        if kind == "eof":
            session.input_closed("INPUT_EOF")


def _prompt(name):
    if not sys.stdin.isatty():
        raise ContractError("CLI_INPUT_REQUIRED", name)
    print(f"{name}: ", end="", file=sys.stderr, flush=True)
    value = sys.stdin.readline().strip()
    if not value:
        raise ContractError("CLI_INPUT_REQUIRED", name)
    return value


def _build_job(selected_sheet, yaw0_sheet, config_root):
    result = subprocess.run(
        [
            sys.executable, str(ROOT / "tools/fr5_data_factory.py"), "build-job", "--interactive",
            "--selected-sheet", selected_sheet, "--yaw0-sheet", yaw0_sheet, "--config-root", config_root,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise ContractError("JOB_BUILD_FAILED")
    return load_json_strict(result.stdout)


def _human_payload(args):
    names = ("run_id", "selected_sheet", "yaw0_sheet", "config_root", "motion_qualification", "home_candidate", "urdf", "expected_robot_system_id")
    values = {name: getattr(args, name) or _prompt(name) for name in names}
    if args.job is None:
        if not sys.stdin.isatty():
            raise ContractError("CLI_INPUT_REQUIRED", "job")
        job = _build_job(values["selected_sheet"], values["yaw0_sheet"], values["config_root"])
    else:
        job = load_json_strict(sys.stdin.read() if args.job == "-" else Path(args.job).read_text(encoding="utf-8"))
    payload = {"mode": args.mode, **values, "job": job}
    if args.mode == "live":
        for name in ("camera_profile", "dataset_root", "run_root"):
            payload[name] = getattr(args, name) or _prompt(name)
    elif any(getattr(args, name) is not None for name in ("camera_profile", "dataset_root", "run_root")):
        raise ContractError("RUN_PAYLOAD")
    return _run_payload(payload)


def _parser():
    parser = ContractArgumentParser(description=__doc__)
    parser.add_argument("--factory-jsonl", action="store_true")
    parser.add_argument("--mode", choices=("plan_only", "live"), default="plan_only")
    for name in ("run-id", "job", "selected-sheet", "yaw0-sheet", "config-root", "motion-qualification", "home-candidate", "urdf", "expected-robot-system-id", "camera-profile", "dataset-root", "run-root"):
        parser.add_argument(f"--{name}")
    return parser


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
        if args.factory_jsonl:
            if any(getattr(args, name) is not None for name in vars(args) if name not in {"factory_jsonl", "mode"}) or args.mode != "plan_only":
                raise ContractError("CLI_USAGE")
            return 0 if run_jsonl(sys.stdin, sys.stdout) else 2
        payload = _human_payload(args)
        cancel = threading.Event()
        job = payload["job"]
        print(
            f"run={payload['run_id']} mode={payload['mode']} target=({job.get('place_id')},{job.get('yaw_deg')},{job.get('x_mm')},{job.get('y_mm')})",
            file=sys.stderr,
        )
        result = run_plan_only(payload, cancel, lambda _: None) if payload["mode"] == "plan_only" else _response(code="LIVE_NOT_QUALIFIED", state="REJECTED", run_id=payload["run_id"], data={"camera_semantic_authority": False})
    except KeyboardInterrupt:
        result = _response(code="CANCELLED", state="CANCELLED")
    except (ContractError, OSError, UnicodeError) as exc:
        result = _response(code=exc.code if isinstance(exc, ContractError) else "RUNNER_IO")
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
