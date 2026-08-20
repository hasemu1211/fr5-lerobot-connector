#!/usr/bin/env python3
"""Canonical human/AI one-job data-factory runner."""
from __future__ import annotations

import copy
import json
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.data_factory.one_job import JsonlProcess, OneJob
from tools.data_factory.cell_state import CellStateStore
from tools.data_factory.resource_usage import ResourceMonitor
from tools.data_factory_recovery import write_json_atomic
from tools.data_factory.scene_state import SceneStateStore
from tools.fr5_data_factory import (
    COLLECTION_PROFILE_V2_KEYS,
    ContractArgumentParser,
    ContractError,
    DIGEST,
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
CAMERA_WARMUP_ATTEMPTS = 2
CAMERA_WARMUP_DURATION_S = 5.0
CAMERA_WARMUP_TIMEOUT_S = 8.0
LIVE_CAMERA_MIN_FPS_RATIO = 0.95
CAMERA_WARMUP_MAX_AGE_MS = 300.0
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


def _live_executor(payload, timeout_s):
    return JsonlProcess(
        [
            sys.executable, "-u", str(ROOT / "tools/data_factory/motion/pickup_executor.py"),
            "--factory-jsonl", "--ros-live", "--robot-system-id", payload["expected_robot_system_id"],
            "--cell-state-root", str(ROOT / "outputs/data_factory/cells"),
            "--phase-events-root", payload["run_root"],
        ],
        timeout_s=timeout_s,
    )


def _collection_profile(validated, payload):
    profile = validated.get("collection_profile")
    if not isinstance(profile, dict) or set(profile) != COLLECTION_PROFILE_V2_KEYS or profile.get("schema_version") != "data_factory.collection_profile.v2":
        raise ContractError("COLLECTION_PROFILE_V2_REQUIRED")
    if profile["camera_profile"] != payload["camera_profile"] or profile["encoding_mode"] != "batch":
        raise ContractError("COLLECTION_PROFILE_MISMATCH")
    if profile["fps"] != 30:
        raise ContractError("COLLECTION_FPS_REQUIRED")
    if not all(profile[key] > 0 for key in ("dataset_incremental_peak_bytes", "encoder_temp_peak_bytes", "disk_reserve_bytes")):
        raise ContractError("COLLECTION_STORAGE_NOT_QUALIFIED")
    return copy.deepcopy(profile)


def _recorder(payload, task, profile, timeout_s):
    dataset_root = Path(payload["dataset_root"]).resolve()
    encoder_temp = dataset_root.parent / f".{dataset_root.name}.encoder_tmp"
    dataset_root.parent.mkdir(parents=True, exist_ok=True)
    encoder_temp.mkdir(exist_ok=True)
    if dataset_root.is_symlink() or encoder_temp.is_symlink() or not encoder_temp.is_dir():
        raise ContractError("ENCODER_TEMP_PATH")
    camera_topics = []
    for role in profile["camera_roles"]:
        camera_topics += [f"--{role}-image", profile["camera_topics"][role]]
    return JsonlProcess(
        [
            sys.executable, "-u", str(ROOT / "tools/fr5_lerobot_recorder.py"),
            "--root", str(dataset_root), "--repo-id", profile["repo_id"], "--task", task, "--resume",
            "--fps", str(profile["fps"]), "--width", str(profile["width"]), "--height", str(profile["height"]),
            "--min-camera-source-fps-ratio", str(LIVE_CAMERA_MIN_FPS_RATIO),
            "--writer-queue-size", str(profile["writer_queue_size"]), "--encoder-threads", str(profile["encoder_threads"]),
            "--image-qos", profile["image_qos"], "--image-qos-depth", str(profile["image_qos_depth"]),
            "--encoder-temp-dir", str(encoder_temp),
            "--dataset-incremental-peak-bytes", str(profile["dataset_incremental_peak_bytes"]),
            "--encoder-temp-peak-bytes", str(profile["encoder_temp_peak_bytes"]),
            "--disk-reserve-bytes", str(profile["disk_reserve_bytes"]),
            "--factory-jsonl", "--batch-video-encoding", "--camera-profile", payload["camera_profile"],
            "--run-root", payload["run_root"], *camera_topics,
        ],
        timeout_s=timeout_s,
    )


def _compact_process_output(value, limit=2000):
    value = value if isinstance(value, str) else ""
    return value[-limit:]


def _camera_warmup(payload, profile, cancel):
    """Prove each configured camera is fresh before the recorder transaction exists."""
    attempts = []
    for attempt in range(1, CAMERA_WARMUP_ATTEMPTS + 1):
        if cancel.is_set():
            break
        roles = []
        all_passed = True
        for role in profile["camera_roles"]:
            if cancel.is_set():
                all_passed = False
                break
            command = [
                sys.executable, str(ROOT / "tools/measure_ros_topic_age.py"),
                "--image", profile["camera_topics"][role],
                "--duration", str(CAMERA_WARMUP_DURATION_S),
                "--expected-image-hz", str(profile["fps"]),
                "--min-image-fps-ratio", str(LIVE_CAMERA_MIN_FPS_RATIO),
                "--max-image-age-ms", str(CAMERA_WARMUP_MAX_AGE_MS),
                "--image-qos-depth", str(profile["image_qos_depth"]),
            ]
            if profile["image_qos"] == "reliable":
                command.append("--reliable-image")
            try:
                completed = subprocess.run(
                    command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    timeout=CAMERA_WARMUP_TIMEOUT_S,
                )
                result = {
                    "role": role, "topic": profile["camera_topics"][role],
                    "command_digest": canonical_digest(command[1:]), "status": "PASS" if completed.returncode == 0 else "FAIL",
                    "returncode": completed.returncode, "output": _compact_process_output(completed.stdout),
                }
            except subprocess.TimeoutExpired as exc:
                result = {
                    "role": role, "topic": profile["camera_topics"][role],
                    "command_digest": canonical_digest(command[1:]), "status": "TIMEOUT", "returncode": None,
                    "output": _compact_process_output(exc.stdout),
                }
            roles.append(result)
            all_passed = all_passed and result["status"] == "PASS"
        attempts.append({"attempt": attempt, "roles": roles, "status": "PASS" if all_passed else "FAIL"})
        if all_passed:
            evidence = {
                "schema_version": "data_factory.camera_warmup.v1", "run_id": payload["run_id"],
                "camera_profile": payload["camera_profile"], "attempts": attempts,
            }
            write_json_atomic(_run_dir(payload) / "camera_warmup.json", evidence)
            return evidence
    evidence = {
        "schema_version": "data_factory.camera_warmup.v1", "run_id": payload["run_id"],
        "camera_profile": payload["camera_profile"], "attempts": attempts,
    }
    write_json_atomic(_run_dir(payload) / "camera_warmup.json", evidence)
    if cancel.is_set():
        return evidence
    raise ContractError("CAMERA_WARMUP_FAILED")


def _timeout_s(program):
    return 10.0 + sum(float(step["limits"].get("planning_timeout_s", 0)) for step in program["steps"])


def _tty_decision(prompt, expected):
    """Use the controlling terminal so machine JSONL cannot mint a HUMAN decision."""
    choices = (expected,) if isinstance(expected, str) else expected
    if (
        not isinstance(choices, tuple)
        or not choices
        or any(not isinstance(choice, str) or not choice for choice in choices)
    ):
        raise ContractError("HUMAN_DECISION_SCHEMA")
    try:
        with open("/dev/tty", "r", encoding="utf-8", buffering=1) as tty_in, open("/dev/tty", "w", encoding="utf-8", buffering=1) as tty_out:
            if not tty_in.isatty() or not tty_out.isatty():
                raise ContractError("HUMAN_TTY_REQUIRED")
            expected_text = " or ".join(repr(choice) for choice in choices)
            tty_out.write(f"{prompt}\nType exactly {expected_text}: ")
            tty_out.flush()
            decision = tty_in.readline().rstrip("\r\n")
            if decision not in choices:
                raise ContractError("HUMAN_CONFIRMATION_FAILED")
            return decision
    except OSError as exc:
        raise ContractError("HUMAN_TTY_REQUIRED") from exc


def _approval(run_id, digest, operator_id, scope):
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    return {
        "source": "HUMAN", "approval_id": f"{run_id}-approval", "approved_by": operator_id,
        "approval_expiry": expiry, "approval_scope": scope,
    }


def _operator_summary(result):
    """Only executor-proven geometry may be shown as an execution approval summary."""
    envelope = result.get("plan_envelope")
    summary = envelope.get("operator_summary") if isinstance(envelope, dict) else None
    if not isinstance(summary, dict):
        raise ContractError("OPERATOR_SUMMARY_UNAVAILABLE")
    required = {"path", "flow", "speed", "clearance"}
    if set(summary) != required or not isinstance(summary["path"], list) or not all(isinstance(value, str) for value in summary["path"]):
        raise ContractError("OPERATOR_SUMMARY_SCHEMA")
    if not isinstance(summary["speed"], dict) or not summary["speed"]:
        raise ContractError("OPERATOR_SUMMARY_SCHEMA")
    if summary["flow"] not in (
        {"continuous_through": "APPROACH_STOP_LIN", "next_human_hold": "PRECONTACT_HUMAN"},
        {"continuous_through": "LIFT_LIN", "next_human_hold": "POST_LIFT_SEMANTIC"},
    ):
        raise ContractError("OPERATOR_SUMMARY_SCHEMA")
    if not isinstance(summary["clearance"], dict) or summary["clearance"].get("status") != "COLLISION_CHECKED_NO_DISTANCE":
        raise ContractError("OPERATOR_SUMMARY_SCHEMA")
    return copy.deepcopy(summary)


def _technical_validator(dataset_root, _payload, profile):
    command = [sys.executable, str(ROOT / "tools/validate_lerobot_dataset.py"), dataset_root, "--expected-fps", str(profile["fps"]), "--require-hil-motion"]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180,
    )
    return {
        "ok": completed.returncode == 0, "code": "PASS" if completed.returncode == 0 else "FAIL",
        "result_digest": canonical_digest({"command": command[1:], "returncode": completed.returncode, "output": completed.stdout}),
    }


def _run_dir(payload):
    run_root = Path(payload["run_root"]).resolve()
    run_dir = (run_root / payload["run_id"]).resolve()
    try:
        run_dir.relative_to(run_root)
    except ValueError as exc:
        raise ContractError("VALIDATOR_REFERENCE_PATH") from exc
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise ContractError("VALIDATOR_REFERENCE_PATH")
    return run_dir


def _prepare_run_dir(payload):
    """Create one confined evidence directory before any live child process starts."""
    supplied_root = Path(payload["run_root"])
    if supplied_root.is_symlink():
        raise ContractError("VALIDATOR_REFERENCE_PATH")
    run_root = supplied_root.resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    if not run_root.is_dir() or run_root.is_symlink():
        raise ContractError("VALIDATOR_REFERENCE_PATH")
    run_dir = run_root / payload["run_id"]
    try:
        run_dir.mkdir()
    except FileExistsError as exc:
        raise ContractError("RUN_DIRECTORY_EXISTS") from exc
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ContractError("VALIDATOR_REFERENCE_PATH")
    return run_dir


def _write_preapproval_evidence(payload, validated, planned):
    """Persist exactly the executor envelope that the human is about to approve."""
    envelope = planned.get("plan_envelope") if isinstance(planned, dict) else None
    if not isinstance(envelope, dict) or set(envelope) != {"plan", "precommit_safety", "precommit_evidence", "operator_summary"}:
        raise ContractError("PREAPPROVAL_EVIDENCE")
    plan = envelope["plan"]
    safety = envelope["precommit_safety"]
    precommit_evidence = envelope["precommit_evidence"]
    digest = planned.get("plan_digest")
    if not isinstance(plan, dict) or canonical_digest(plan) != digest or not isinstance(safety, dict):
        raise ContractError("PREAPPROVAL_EVIDENCE")
    required = {
        "schema_version", "run_id", "approved_plan_digest", "scene_binding_digest",
        "expected_planning_scene_digest", "planning_scene_readback_digest", "collision_report_digest",
        "plan_only_no_motion_digest", "post_reset_safe_snapshot_digest", "status",
    }
    if set(safety) != required or safety.get("run_id") != payload["run_id"] or safety.get("approved_plan_digest") != digest:
        raise ContractError("PREAPPROVAL_EVIDENCE")
    if not isinstance(precommit_evidence, dict) or set(precommit_evidence) != {
        "schema_version", "run_id", "approved_plan_digest", "scene_binding_digest",
        "expected_planning_scene_digest", "planning_scene_readback", "collision_report", "plan_only_no_motion",
    }:
        raise ContractError("PREAPPROVAL_EVIDENCE")
    if (
        precommit_evidence.get("schema_version") != "data_factory.precommit_evidence.v1"
        or precommit_evidence.get("run_id") != payload["run_id"]
        or precommit_evidence.get("approved_plan_digest") != digest
        or precommit_evidence.get("scene_binding_digest") != safety["scene_binding_digest"]
        or precommit_evidence.get("expected_planning_scene_digest") != safety["expected_planning_scene_digest"]
        or any(canonical_digest(precommit_evidence[key]) != safety[digest_key] for key, digest_key in (
            ("planning_scene_readback", "planning_scene_readback_digest"),
            ("collision_report", "collision_report_digest"),
            ("plan_only_no_motion", "plan_only_no_motion_digest"),
        ))
    ):
        raise ContractError("PREAPPROVAL_EVIDENCE")
    evidence = {
        "schema_version": "data_factory.preapproval_evidence.v1",
        "run_id": payload["run_id"],
        "resolved_job_digest": validated["resolved_job_digest"],
        "plan_digest": digest,
        "plan_envelope": copy.deepcopy(envelope),
        "plan_envelope_digest": canonical_digest(envelope),
    }
    write_json_atomic(_run_dir(payload) / "preapproval_evidence.json", evidence)
    return evidence


def _write_validator_reference(payload, validated, plan_digest, profile, technical):
    run_dir = _run_dir(payload)
    reference = {
        "schema_version": "data_factory.technical_validator_result.v1", "run_id": payload["run_id"],
        "resolved_job_digest": validated["resolved_job_digest"], "plan_digest": plan_digest,
        "dataset_root": str(Path(payload["dataset_root"]).resolve()), "expected_fps": profile["fps"],
        "status": technical["code"], "result_digest": technical["result_digest"],
    }
    write_json_atomic(run_dir / "technical_validator.json", reference)
    return reference


def _write_storage_reference(payload, validated, profile, recorder_evidence):
    metrics = recorder_evidence.get("metrics") if isinstance(recorder_evidence, dict) else None
    storage = metrics.get("storage_usage") if isinstance(metrics, dict) else None
    required = {
        "episode_index", "transaction_id", "staging_manifest_digest", "disk_reserve_bytes",
        "dataset_incremental_peak_bytes", "encoder_temp_peak_bytes", "required_free_bytes_by_device",
        "dataset_bytes_before", "dataset_bytes_after", "free_bytes_before_by_device",
        "free_bytes_by_device", "temp_peak_bytes_by_device", "filesystems",
    }
    if not isinstance(storage, dict) or set(storage) != required:
        raise ContractError("STORAGE_REFERENCE_ERROR")
    if (
        type(storage["episode_index"]) is not int or storage["episode_index"] < 0
        or not isinstance(storage["transaction_id"], str) or not storage["transaction_id"]
        or not isinstance(storage["staging_manifest_digest"], str) or not DIGEST.fullmatch(storage["staging_manifest_digest"])
        or type(storage["dataset_bytes_before"]) is not int or type(storage["dataset_bytes_after"]) is not int
        or storage["dataset_bytes_before"] < 0 or storage["dataset_bytes_after"] < storage["dataset_bytes_before"]
    ):
        raise ContractError("STORAGE_REFERENCE_ERROR")
    filesystems = storage["filesystems"]
    if not isinstance(filesystems, dict) or set(filesystems) != {"dataset", "encoder_temp"}:
        raise ContractError("STORAGE_REFERENCE_ERROR")
    normalized_filesystems = {}
    for role, value in filesystems.items():
        if (
            not isinstance(value, dict) or set(value) != {"path", "device", "free_bytes", "total_bytes"}
            or not isinstance(value["path"], str) or type(value["device"]) is not int
            or type(value["free_bytes"]) is not int or type(value["total_bytes"]) is not int
        ):
            raise ContractError("STORAGE_REFERENCE_ERROR")
        normalized_filesystems[role] = {key: value[key] for key in ("path", "device", "total_bytes")}
    for key in ("free_bytes_before_by_device", "free_bytes_by_device", "temp_peak_bytes_by_device"):
        value = storage[key]
        if not isinstance(value, dict) or any(not isinstance(device, str) or type(size) is not int or size < 0 for device, size in value.items()):
            raise ContractError("STORAGE_REFERENCE_ERROR")
    reference = {
        "schema_version": "data_factory.storage_usage.v1",
        "run_id": payload["run_id"],
        "episode_ref": {
            "schema_version": "data_factory.episode_ref.v1", "repo_id": profile["repo_id"],
            "episode_index": storage["episode_index"], "transaction_id": storage["transaction_id"],
            "resolved_job_digest": validated["resolved_job_digest"],
            "staging_manifest_digest": storage["staging_manifest_digest"],
        },
        "dataset_filesystem": normalized_filesystems["dataset"],
        "encoder_temp_filesystem": normalized_filesystems["encoder_temp"],
        "dataset_bytes_before": storage["dataset_bytes_before"],
        "dataset_bytes_after": storage["dataset_bytes_after"],
        "dataset_delta_bytes": storage["dataset_bytes_after"] - storage["dataset_bytes_before"],
        "temporary_peak_bytes_by_filesystem": copy.deepcopy(storage["temp_peak_bytes_by_device"]),
        "free_bytes_before": copy.deepcopy(storage["free_bytes_before_by_device"]),
        "free_bytes_after": copy.deepcopy(storage["free_bytes_by_device"]),
        "reference_scan_status": "NOT_AVAILABLE", "dataset_prunable": [],
    }
    write_json_atomic(_run_dir(payload) / "storage_usage.json", reference)
    return reference


def _write_resource_reference(payload, monitor, recorder_evidence, profile):
    metrics = recorder_evidence.get("metrics") if isinstance(recorder_evidence, dict) else {}
    report = monitor.finish(metrics if isinstance(metrics, dict) else {}, collection_settings=profile)
    write_json_atomic(_run_dir(payload) / "resource_usage.json", report)
    return report


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
        timeout_s = _timeout_s(program)
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


def run_live(payload, cancel, publish, *, resolver=resolve_inputs, executor_factory=_live_executor,
             recorder_factory=_recorder, validator_call=_technical_validator, tty_decision=_tty_decision,
             camera_warmup_call=_camera_warmup):
    """Public single HIL run: plan and human approval precede recorder begin and motion."""
    executor = recorder = resource_monitor = None
    resource_finished = False
    profile = None
    try:
        validated, program, scene_binding = resolver(payload)
        profile = _collection_profile(validated, payload)
        if cancel.is_set():
            return _response(ok=False, code="CANCELLED", state="CANCELLED", run_id=payload["run_id"])
        _prepare_run_dir(payload)
        camera_warmup = camera_warmup_call(payload, profile, cancel)
        if cancel.is_set():
            return _response(ok=False, code="CANCELLED", state="CANCELLED", run_id=payload["run_id"])
        timeout_s = _timeout_s(program)
        executor = executor_factory(payload, timeout_s)
        preflight = executor.request({
            "schema_version": "fr5.pickup_executor.command.v4", "op_id": "00-preflight", "op": "preflight",
            "payload": {"motion_program": program},
        }, cancel)
        if not isinstance(preflight, dict):
            raise ContractError("PREFLIGHT_RESPONSE")
        if preflight.get("ok") is not True:
            code = preflight.get("code")
            raise ContractError(code if isinstance(code, str) and SAFE_ID.fullmatch(code) else "PREFLIGHT_FAILED")
        if preflight.get("code") != "PREFLIGHT_OK":
            raise ContractError("PREFLIGHT_RESPONSE")
        forbidden = lambda _request: (_ for _ in ()).throw(ContractError("LIVE_RECORDER_NOT_STARTED"))
        cell_root = ROOT / "outputs/data_factory/cells"
        cell_store = CellStateStore(cell_root, payload["expected_robot_system_id"])
        scene_store = SceneStateStore(cell_root, payload["expected_robot_system_id"])
        job = OneJob(forbidden, lambda request: executor.request(request, cancel), cell_state_call=cell_store.read)
        planned = job.plan_only(payload["run_id"], program, scene_binding)
        if not planned["ok"]:
            return _response(ok=False, code=planned["code"], state=planned["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
        summary = _operator_summary(planned)
        preapproval_evidence = _write_preapproval_evidence(payload, validated, planned)
        publish(_response(ok=True, code="AWAITING_HUMAN_APPROVAL", state="PLANNED", run_id=payload["run_id"], plan_digest=planned["plan_digest"], data={
            "mode": "live", "operator_summary": summary, "resolved_job_digest": validated["resolved_job_digest"],
            "scene_binding": scene_binding, "preapproval_evidence_digest": canonical_digest(preapproval_evidence),
            "camera_warmup_digest": canonical_digest(camera_warmup),
            "camera_semantic_authority": False, "training_authorized": False,
        }))
        operator_id = validated["normalized_job"]["operator_or_agent_id"]
        tty_decision(
            f"Plan {planned['plan_digest']} path={' > '.join(summary['path'])} flow={summary['flow']} "
            f"clearance={summary['clearance']} speed={summary['speed']}",
            f"APPROVE {planned['plan_digest']}",
        )
        if cancel.is_set():
            return _response(ok=False, code="CANCELLED", state="CANCELLED", run_id=payload["run_id"], plan_digest=planned["plan_digest"])
        approved = job.approve(_approval(payload["run_id"], planned["plan_digest"], operator_id, "HUMAN_GATED"))
        if not approved["ok"]:
            return _response(ok=False, code=approved["code"], state=approved["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
        resource_monitor = ResourceMonitor(
            payload["run_id"], validated["input_digests"]["collection_profile"]
        ).start()
        if isinstance(getattr(getattr(executor, "process", None), "pid", None), int):
            resource_monitor.set_pid("executor", executor.process.pid)
        recorder = recorder_factory(payload, validated["normalized_job"]["instruction"], profile, timeout_s)
        if isinstance(getattr(getattr(recorder, "process", None), "pid", None), int):
            resource_monitor.set_pid("recorder", recorder.process.pid)
        job.recorder_call = recorder
        started = job.start()
        if not started["ok"]:
            return _response(ok=False, code=started["code"], state=started["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
        decisions = queue.Queue(maxsize=1)
        pending = None
        while True:
            if cancel.is_set():
                result = job.cancel()
                return _response(ok=False, code=result["code"], state=result["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
            poll_started = time.monotonic()
            result = job.poll()
            resource_monitor.record_control_round_trip(time.monotonic() - poll_started)
            if not result["ok"]:
                return _response(ok=False, code=result["code"], state=result["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
            if result["state"] in {"AWAITING_CELL_READY", "COMMITTED"}:
                technical = validator_call(payload["dataset_root"], payload, profile)
                if not isinstance(technical, dict) or type(technical.get("ok")) is not bool or technical.get("code") not in {"PASS", "FAIL"} or not isinstance(technical.get("result_digest"), str):
                    raise ContractError("TECHNICAL_VALIDATOR_SCHEMA")
                validator_reference = _write_validator_reference(payload, validated, planned["plan_digest"], profile, technical)
                cell = cell_store.read()
                if cell.get("cell_ready") is not False or cell.get("run_id") != payload["run_id"] or cell.get("plan_digest") != planned["plan_digest"]:
                    raise ContractError("POSTCOMMIT_CELL_STATE")
                postcommit_error = None
                storage_reference = None
                try:
                    storage_reference = _write_storage_reference(payload, validated, profile, result.get("recorder_evidence"))
                except (ContractError, OSError) as exc:
                    postcommit_error = exc.code if isinstance(exc, ContractError) else "STORAGE_REFERENCE_ERROR"
                resource_reference = None
                try:
                    resource_reference = _write_resource_reference(payload, resource_monitor, result.get("recorder_evidence"), profile)
                    resource_finished = True
                    if resource_reference["sampling"]["status"] != "AVAILABLE":
                        postcommit_error = postcommit_error or "RESOURCE_EVIDENCE_ERROR"
                except (ContractError, OSError, ValueError):
                    resource_finished = True
                    postcommit_error = postcommit_error or "RESOURCE_EVIDENCE_ERROR"
                terminal_error = postcommit_error or (None if technical["ok"] else "TECHNICAL_VALIDATOR_FAILED")
                if terminal_error is not None:
                    # A committed episode remains forensic evidence, but cannot unlock the cell.
                    cell = cell_store.mark_blocked(terminal_error, payload["run_id"], planned["plan_digest"])
                    return _response(ok=False, code=terminal_error, state="BLOCKED", run_id=payload["run_id"], plan_digest=planned["plan_digest"], data={
                        "mode": "live", "operator_summary": summary, "technical_validator": validator_reference,
                        "storage_usage": storage_reference, "resource_usage": resource_reference,
                        "camera_warmup_digest": canonical_digest(camera_warmup),
                        "postcommit_cell_state": cell,
                        "camera_semantic_authority": False, "training_authorized": False,
                    })
                target = validated["normalized_job"]
                tty_decision(
                    "Confirm the robot is stopped, gripper is empty, path is clear, and the object is reset at "
                    f"({target['place_id']},{target['yaw_deg']},{target['x_mm']},{target['y_mm']})",
                    f"SCENE_READY {planned['plan_digest']}",
                )
                scene = scene_store.update_object(
                    instance_id=scene_binding["object_instance_id"],
                    object_profile_id=target["object_profile_id"], state="ON_SURFACE", source="HUMAN",
                    updated_by=operator_id,
                    pose={key: target[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")},
                    expected_revision=scene_binding["revision"],
                )
                cell = cell_store.acknowledge_ready(
                    operator_id, expected_run_id=payload["run_id"], expected_plan_digest=planned["plan_digest"],
                )
                finished = job.finish()
                if not finished["ok"] or finished["state"] != "COMPLETE":
                    raise ContractError("CELL_READY_REQUIRED")
                return _response(ok=True, code="VALIDATED", state="COMPLETE", run_id=payload["run_id"], plan_digest=planned["plan_digest"], data={
                    "mode": "live", "operator_summary": summary, "technical_validator": validator_reference,
                    "storage_usage": storage_reference, "resource_usage": resource_reference,
                    "camera_warmup_digest": canonical_digest(camera_warmup),
                    "postcommit_scene_state_digest": scene["scene_state_digest"], "postcommit_cell_state": cell,
                    "camera_semantic_authority": False, "training_authorized": False,
                })
            if result["state"] in {"GRASP_VERDICT", "SEMANTIC_VERDICT"}:
                if pending is None:
                    pending = result["state"]
                    prompt = (
                        "Confirm the physical grasp; PASS continues to lift, FAIL aborts"
                        if result["state"] == "GRASP_VERDICT"
                        else "Confirm the completed episode; PASS commits, FAIL discards"
                    )

                    def verdict_in_background(state=result["state"], text=prompt):
                        try:
                            decision = tty_decision(text, ("PASS", "FAIL"))
                            if decision not in {"PASS", "FAIL"}:
                                raise ContractError("HUMAN_CONFIRMATION_FAILED")
                            decisions.put((state, decision))
                        except Exception as exc:
                            decisions.put((state, exc))

                    threading.Thread(target=verdict_in_background, daemon=True).start()
                try:
                    state, decision = decisions.get_nowait()
                except queue.Empty:
                    time.sleep(0.05)
                    continue
                pending = None
                if state != result["state"] or isinstance(decision, Exception):
                    cancelled = job.cancel()
                    return _response(ok=False, code=cancelled["code"], state=cancelled["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
                acted = (job.grasp_verdict if state == "GRASP_VERDICT" else job.semantic_verdict)(decision, operator_id, source="HUMAN")
                if not acted["ok"]:
                    return _response(ok=False, code=acted["code"], state=acted["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
                continue
            if result["state"] == "PRECONTACT_HUMAN":
                if pending is None:
                    pending = result["state"]
                    def confirm_in_background():
                        try:
                            tty_decision("Confirm the physical precontact pose", f"CONFIRM {planned['plan_digest']}")
                            decisions.put(("PRECONTACT_HUMAN", None))
                        except Exception as exc:
                            decisions.put(("PRECONTACT_HUMAN", exc))
                    threading.Thread(target=confirm_in_background, daemon=True).start()
                try:
                    state, decision = decisions.get_nowait()
                except queue.Empty:
                    time.sleep(0.05)
                    continue
                pending = None
                if state != result["state"] or isinstance(decision, Exception):
                    cancelled = job.cancel()
                    return _response(ok=False, code=cancelled["code"], state=cancelled["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
                confirmed = job.confirm(operator_id)
                if not confirmed["ok"]:
                    return _response(ok=False, code=confirmed["code"], state=confirmed["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
                continue
            time.sleep(0.05)
    except ContractError as exc:
        return _response(ok=False, code=exc.code, state="BLOCKED", run_id=payload.get("run_id"))
    except Exception as exc:
        return _response(ok=False, code="RUNNER_FAILED", state="BLOCKED", run_id=payload.get("run_id"), data={"detail": str(exc)})
    finally:
        if resource_monitor is not None and not resource_finished:
            try:
                resource_monitor.finish({}, collection_settings=profile)
            except Exception:
                pass
        for child in (recorder, executor):
            if child is not None:
                try:
                    child.close(timeout_s=1.0 if cancel.is_set() else None)
                except ContractError:
                    if not cancel.is_set():
                        raise


def _run_mode(payload, cancel, publish):
    return run_live(payload, cancel, publish) if payload["mode"] == "live" else run_plan_only(payload, cancel, publish)


class RunSession:
    """One worker owns child I/O; the main thread owns JSONL output."""

    def __init__(self, run_call=_run_mode):
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
            with self.lock:
                if self.worker is not None and self.worker.is_alive():
                    return _response(op_id=op_id, op=op, code="RUN_ACTIVE", state=self.snapshot["state"], run_id=self.run_id, plan_digest=self.snapshot["plan_digest"])
                if self.used:
                    return _response(op_id=op_id, op=op, code="ONE_JOB_ONLY", state=self.snapshot["state"], run_id=self.run_id, plan_digest=self.snapshot["plan_digest"])
                self.used, self.origin_op_id, self.run_id = True, op_id, payload["run_id"]
                self.snapshot = _response(ok=True, code="RUNNING", state="RUNNING", run_id=self.run_id, data={"mode": payload["mode"]})
                self.worker = threading.Thread(target=self._work, args=(payload,), daemon=True)
                self.worker.start()
            return _response(op_id=op_id, op=op, ok=True, code="RUNNING", state="RUNNING", run_id=self.run_id, data={"mode": payload["mode"]})
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
        result = run_plan_only(payload, cancel, lambda _: None) if payload["mode"] == "plan_only" else run_live(payload, cancel, lambda _: None)
    except KeyboardInterrupt:
        result = _response(code="CANCELLED", state="CANCELLED")
    except (ContractError, OSError, UnicodeError) as exc:
        result = _response(code=exc.code if isinstance(exc, ContractError) else "RUNNER_IO")
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
