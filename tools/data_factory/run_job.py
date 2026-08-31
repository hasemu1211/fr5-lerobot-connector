#!/usr/bin/env python3
"""Canonical human/AI one-job data-factory runner."""
from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.data_factory.one_job import (
    JsonlProcess,
    OneJob,
    RECORDER_READINESS_CONTRACT,
    TEST_ONLY_READINESS_CONTRACT,
    hil_numeric_gripper_verdict,
)
from tools.data_factory.operator.workflow.intents import CANDIDATE_REVIEW_REASONS
from tools.data_factory.campaign_authorization import (
    validate_campaign_authorization,
    validate_runtime_campaign_scope,
)
from tools.data_factory.cell_state import CellStateStore
from tools.data_factory.episode_ledger import (
    build_lerobot_v3_episode_locator,
    compile_episode_ledger,
    project_episode_state,
    reproject_episode_state,
)
from tools.data_factory.operator.setup.contracts import (
    validate_runtime_episode_binding,
    validate_runtime_planned_start,
    validate_runtime_root_binding,
    validate_test_only_episode_binding,
    validate_test_only_planned_start,
    validate_test_only_root_binding,
)
from tools.data_factory.resource_usage import ResourceMonitor
from tools.data_factory_recovery import write_json_atomic
from tools.data_factory.scene_state import SceneStateStore, release_slot
from tools.data_factory.task_recipe import TASK_IDS, get_task_recipe
from tools.fr5_data_factory import (
    COLLECTION_PROFILE_V2_KEYS,
    ContractArgumentParser,
    ContractError,
    DIGEST,
    SAFE_ID,
    TASK_REVIEW_CHECKLIST_IDS,
    bounded_place_coordinate,
    canonical_digest,
    load_json_strict,
    normalize_job_spec,
    normalize_yaw_deg,
    resolve_motion_program,
    task_review_checklist_id,
    validate_job_spec,
)


COMMAND_SCHEMA = "data_factory.run_job.command.v1"
CAMPAIGN_SCHEMA = "data_factory.campaign.v1"
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
RECYCLE_COORD_KEYS = {"recycle_x_mm", "recycle_y_mm"}
RECYCLE_YAW_KEY = "recycle_yaw_deg"
LIVE_RUN_KEYS = COMMON_RUN_KEYS | {"camera_profile", "dataset_root", "run_root"}
RESPONSE_KEYS = {"schema_version", "op_id", "op", "ok", "code", "state", "run_id", "plan_digest", "data"}
EVENT_KEYS = {"schema_version", "event", "sequence", "origin_op_id", "ok", "code", "state", "run_id", "plan_digest", "data"}
CANDIDATE_ADMISSION_KEYS = {
    "schema_version", "run_id", "operational_gate", "operational_source", "checklist_id",
    "review_context_digest", "semantic_status", "reviewed_by", "reviewed_at", "reason",
}
EPISODE_LEDGER_CONTEXT_FIELDS = frozenset({"manifest", "intent"})
ROOT = Path(__file__).resolve().parents[2]
DATA_PYTHON = str(ROOT / ".venv/bin/python")
EPISODE_LOCATOR_PREFIX = "EPISODE_LOCATOR_JSON:"


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
    keys = set(COMMON_RUN_KEYS if value["mode"] == "plan_only" else LIVE_RUN_KEYS)
    supplied_recycle = set(value) & RECYCLE_COORD_KEYS
    supplied_recycle_yaw = RECYCLE_YAW_KEY in value
    if supplied_recycle:
        if supplied_recycle != RECYCLE_COORD_KEYS:
            raise ContractError("RUN_PAYLOAD")
        keys |= RECYCLE_COORD_KEYS
    if supplied_recycle_yaw:
        if not supplied_recycle:
            raise ContractError("RUN_PAYLOAD")
        keys.add(RECYCLE_YAW_KEY)
    _exact(value, keys, "RUN_PAYLOAD")
    _identifier(value["run_id"], "RUN_ID")
    if not isinstance(value["job"], dict):
        raise ContractError("RUN_JOB")
    for key in keys - {"job"} - RECYCLE_COORD_KEYS - {RECYCLE_YAW_KEY}:
        _text(value[key], "RUN_PAYLOAD")
    for key in supplied_recycle | ({RECYCLE_YAW_KEY} if supplied_recycle_yaw else set()):
        if isinstance(value[key], bool) or not isinstance(value[key], (int, float)) or not math.isfinite(value[key]):
            raise ContractError("RUN_PAYLOAD")
    return copy.deepcopy(value)


def _campaign_manifest(value):
    _exact(value, {"schema_version", "campaign_id", "max_episodes", "episodes"}, "CAMPAIGN_SCHEMA")
    if value["schema_version"] != CAMPAIGN_SCHEMA or value["max_episodes"] != 2 or not isinstance(value["episodes"], list) or len(value["episodes"]) != 2:
        raise ContractError("CAMPAIGN_SCHEMA")
    campaign_id = _identifier(value["campaign_id"], "CAMPAIGN_SCHEMA")
    episodes = []
    for index, item in enumerate(value["episodes"]):
        _exact(item, {"run", "release_role"}, "CAMPAIGN_EPISODE")
        expected_role = "DESTINATION_THEN_NEXT_SOURCE" if index == 0 else "RELEASE_DESTINATION"
        if item["release_role"] != expected_role:
            raise ContractError("CAMPAIGN_EPISODE")
        run = _run_payload(item["run"])
        if run["mode"] != "live" or not RECYCLE_COORD_KEYS <= set(run):
            raise ContractError("CAMPAIGN_EPISODE")
        run["job"] = normalize_job_spec(run["job"])
        if run["job"]["job_id"] != run["run_id"] or run["job"]["robot_system_id"] != run["expected_robot_system_id"]:
            raise ContractError("CAMPAIGN_EPISODE")
        episodes.append({"run": run, "release_role": expected_role})
    first, second = (item["run"] for item in episodes)
    if first["run_id"] == second["run_id"] or any(first[key] != second[key] for key in LIVE_RUN_KEYS - {"run_id", "job"}):
        raise ContractError("CAMPAIGN_CHAIN")
    fixed_job = ("robot_system_id", "collection_profile_id", "place_id", "cell_calibration_id", "object_profile_id", "grasp_profile_id")
    if any(first["job"][key] != second["job"][key] for key in fixed_job) or any(
        first[recycle] != second["job"][coordinate]
        for recycle, coordinate in (("recycle_x_mm", "x_mm"), ("recycle_y_mm", "y_mm"))
    ):
        raise ContractError("CAMPAIGN_CHAIN")
    poses = {
        (first["job"]["x_mm"], first["job"]["y_mm"]),
        (first["recycle_x_mm"], first["recycle_y_mm"]),
        (second["recycle_x_mm"], second["recycle_y_mm"]),
    }
    if len(poses) != 3:
        raise ContractError("CAMPAIGN_CHAIN")
    return {"schema_version": CAMPAIGN_SCHEMA, "campaign_id": campaign_id, "max_episodes": 2, "episodes": episodes}


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


def _scene_binding(validated, release_pose, run_id, root=ROOT / "outputs/data_factory/cells"):
    job = validated["normalized_job"]
    run_id = _identifier(run_id, "SCENE_SLOT_NEXT_RUN")
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
    exclusion_geometry_digest = canonical_digest({
        "shape": "BOX",
        "dimensions_mm": validated["object_profile"]["dimensions_mm"],
    })
    slot = release_slot(
        robot_system_id=job["robot_system_id"],
        pose=release_pose,
        object_profile_id=job["object_profile_id"],
        exclusion_geometry_digest=exclusion_geometry_digest,
    )
    allocation = snapshot["scene_state"].get("slot_allocations", {}).get(slot["slot_id"])
    if allocation is not None and allocation.get("state") not in {"AVAILABLE", "LANDED_FOR_NEXT_SOURCE"}:
        raise ContractError("SCENE_SLOT_NOT_READY")
    binding = {
        "scene_state_digest": snapshot["scene_state_digest"],
        "revision": snapshot["scene_state"]["revision"],
        "object_instance_id": matches[0]["instance_id"],
        "release_slot": slot,
    }
    if matches[0].get("source") in {"ROBOT_RELEASE", "ROBOT_RELEASE_PROXY"}:
        source_slot = release_slot(
            robot_system_id=job["robot_system_id"], pose=pose,
            object_profile_id=job["object_profile_id"],
            exclusion_geometry_digest=exclusion_geometry_digest,
        )
        source_allocation = snapshot["scene_state"].get("slot_allocations", {}).get(source_slot["slot_id"])
        if (
            not isinstance(source_allocation, dict)
            or source_allocation.get("state") != "LANDED_FOR_NEXT_SOURCE"
            or source_allocation.get("role") != "DESTINATION_THEN_NEXT_SOURCE"
            or source_allocation.get("allowed_run_id") != run_id
        ):
            raise ContractError("SCENE_SLOT_NEXT_RUN")
        binding["source_slot"] = {
            "slot_id": source_slot["slot_id"],
            "slot_digest": canonical_digest(source_allocation),
            "allowed_run_id": run_id,
        }
    return binding


def resolve_inputs(
    payload, *, scene_binding_call=_scene_binding, input_transform=None,
):
    validated = validate_job_spec(
        payload["job"],
        paths={"selected_sheet": payload["selected_sheet"], "yaw0_sheet": payload["yaw0_sheet"]},
        config_root=payload["config_root"],
    )
    task_id = validated["normalized_job"]["task"]
    if task_id not in TASK_IDS:
        raise ContractError("TASK_NOT_SUPPORTED")
    if task_id == "pick_place" and not RECYCLE_COORD_KEYS <= set(payload):
        raise ContractError("TASK_DESTINATION_REQUIRED")
    release_pose = {key: validated["normalized_job"][key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")}
    if RECYCLE_COORD_KEYS <= set(payload):
        sheet = _load(payload["selected_sheet"], "INPUT_SELECTED_SHEET")
        coordinate_safety = {
            "object_dimensions_mm": validated["object_profile"]["dimensions_mm"],
            "uncertainty_mm": validated["calibration"]["document"]["limits"][
                "combined_error_bound_mm"
            ],
        }
        if RECYCLE_YAW_KEY in payload:
            recycle_yaw = normalize_yaw_deg(payload[RECYCLE_YAW_KEY])
            x_mm, y_mm = bounded_place_coordinate(
                sheet, payload["recycle_x_mm"], payload["recycle_y_mm"],
                yaw_deg=recycle_yaw, **coordinate_safety,
            )
            release_pose["yaw_deg"] = recycle_yaw
        else:
            x_mm, y_mm = bounded_place_coordinate(
                sheet, payload["recycle_x_mm"], payload["recycle_y_mm"],
                **coordinate_safety,
            )
        release_pose.update(x_mm=x_mm, y_mm=y_mm)
    motion_qualification = _load(
        payload["motion_qualification"], "MOTION_QUALIFICATION_IO",
    )
    if input_transform is not None:
        if not callable(input_transform):
            raise ContractError("INPUT_TRANSFORM")
        transformed = input_transform(validated, motion_qualification)
        if (
            not isinstance(transformed, (tuple, list))
            or len(transformed) != 2
            or any(not isinstance(item, Mapping) for item in transformed)
        ):
            raise ContractError("INPUT_TRANSFORM")
        validated, motion_qualification = (
            copy.deepcopy(dict(item)) for item in transformed
        )
    program = resolve_motion_program(
        validated,
        motion_qualification,
        _load(payload["home_candidate"], "HOME_CANDIDATE_IO"),
        urdf=payload["urdf"],
        expected_robot_system_id=payload["expected_robot_system_id"],
        release_pose=release_pose,
    )
    return validated, program, scene_binding_call(validated, release_pose, payload["run_id"])


def _executor(timeout_s):
    return JsonlProcess(
        [sys.executable, "-u", str(ROOT / "tools/data_factory/motion/pickup_executor.py"), "--factory-jsonl", "--ros-plan-only"],
        timeout_s=timeout_s,
    )


def _live_executor(payload, timeout_s, *, cell_root=None):
    cell_root = ROOT / "outputs/data_factory/cells" if cell_root is None else Path(cell_root)
    return JsonlProcess(
        [
            sys.executable, "-u", str(ROOT / "tools/data_factory/motion/pickup_executor.py"),
            "--factory-jsonl", "--ros-live", "--robot-system-id", payload["expected_robot_system_id"],
            "--cell-state-root", str(cell_root),
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


def _validate_runtime_collection_binding(validated, program):
    """Reject resolver/profile substitution before any live side effect."""
    if not isinstance(validated, dict):
        raise ContractError("COLLECTION_PROFILE_BINDING")
    job = validated.get("normalized_job")
    inputs = validated.get("input_digests")
    profile = validated.get("collection_profile")
    bindings = program.get("binding_digests") if isinstance(program, dict) else None
    if (
        not isinstance(job, dict)
        or not isinstance(inputs, dict)
        or not isinstance(profile, dict)
        or not isinstance(bindings, dict)
        or job.get("collection_profile_id") != profile.get("collection_profile_id")
        or inputs.get("collection_profile") != canonical_digest(profile)
        or validated.get("resolved_job_digest")
        != canonical_digest({"job": job, "input_digests": inputs})
        or program.get("resolved_job_digest") != validated.get("resolved_job_digest")
        or bindings.get("collection_profile") != inputs.get("collection_profile")
    ):
        raise ContractError("COLLECTION_PROFILE_BINDING")


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
            DATA_PYTHON, "-u", str(ROOT / "tools/fr5_lerobot_recorder.py"),
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
    def measure(role):
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
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=CAMERA_WARMUP_TIMEOUT_S,
            )
            return {
                "role": role, "topic": profile["camera_topics"][role],
                "command_digest": canonical_digest(command[1:]),
                "status": "PASS" if completed.returncode == 0 else "FAIL",
                "returncode": completed.returncode,
                "output": _compact_process_output(completed.stdout),
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "role": role, "topic": profile["camera_topics"][role],
                "command_digest": canonical_digest(command[1:]),
                "status": "TIMEOUT", "returncode": None,
                "output": _compact_process_output(exc.stdout),
            }

    attempts = []
    camera_roles = profile["camera_roles"]
    with ThreadPoolExecutor(
        max_workers=len(camera_roles), thread_name_prefix="camera-probe",
    ) as pool:
        for attempt in range(1, CAMERA_WARMUP_ATTEMPTS + 1):
            if cancel.is_set():
                break
            futures = [pool.submit(measure, role) for role in camera_roles]
            roles = [future.result() for future in futures]
            all_passed = all(result["status"] == "PASS" for result in roles)
            attempts.append({
                "attempt": attempt, "roles": roles,
                "status": "PASS" if all_passed else "FAIL",
            })
            if all_passed:
                evidence = {
                    "schema_version": "data_factory.camera_warmup.v1",
                    "run_id": payload["run_id"],
                    "camera_profile": payload["camera_profile"],
                    "attempts": attempts,
                }
                write_json_atomic(
                    _run_dir(payload) / "camera_warmup.json", evidence,
                )
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


def _approval(run_id, digest, operator_id, scope, *, source="HUMAN"):
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    return {
        "source": source, "approval_id": f"{run_id}-approval", "approved_by": operator_id,
        "approval_expiry": expiry, "approval_scope": scope,
    }


def _button_plan_decision(
    provider, *, run_id, plan_digest, approval_scope, decision_binding,
    operator_id, timeout_s, expected_source="LOCAL_UI_BUTTON",
):
    request = {
        "schema_version": "data_factory.plan_decision_request.v1",
        "run_id": run_id,
        "plan_digest": plan_digest,
        "approval_scope": approval_scope,
        "decision_binding": copy.deepcopy(decision_binding),
        "timeout_s": timeout_s,
    }
    try:
        value = provider(copy.deepcopy(request))
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError("PLAN_DECISION_FAILED") from exc
    if value is None:
        return None
    fields = {
        "choice", "run_id", "plan_digest", "approval_scope",
        "decision_binding_digest", "decision_source", "operator_label",
    }
    expected_digest = canonical_digest({
        "run_id": run_id,
        "plan_digest": plan_digest,
        "approval_scope": approval_scope,
        "decision_binding": decision_binding,
    })
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("choice") not in {"APPROVE", "REJECT", "CANCEL"}
        or value.get("run_id") != run_id
        or value.get("plan_digest") != plan_digest
        or value.get("approval_scope") != approval_scope
        or value.get("decision_binding_digest") != expected_digest
        or value.get("decision_source") != expected_source
        or value.get("operator_label") != operator_id
    ):
        raise ContractError("PLAN_DECISION_BINDING")
    return copy.deepcopy(value)


def _operator_checkpoint(
    provider, *, kind, run_id, plan_digest, prompt, choices, evidence,
    operator_id, timeout_s, expected_source="LOCAL_UI_BUTTON",
):
    request = {
        "schema_version": "data_factory.operator_checkpoint_request.v1",
        "kind": kind,
        "run_id": run_id,
        "plan_digest": plan_digest,
        "prompt": prompt,
        "choices": list(choices),
        "evidence": copy.deepcopy(evidence),
        "timeout_s": timeout_s,
    }
    try:
        value = provider(copy.deepcopy(request))
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError("OPERATOR_CHECKPOINT_FAILED") from exc
    if value is None:
        return None
    fields = {
        "kind", "choice", "run_id", "plan_digest", "checkpoint_binding_digest",
        "decision_source", "operator_label",
    }
    bound = {
        key: request[key]
        for key in ("kind", "run_id", "plan_digest", "prompt", "choices", "evidence")
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("kind") != kind
        or value.get("choice") not in choices
        or value.get("run_id") != run_id
        or value.get("plan_digest") != plan_digest
        or value.get("checkpoint_binding_digest") != canonical_digest(bound)
        or value.get("decision_source") != expected_source
        or value.get("operator_label") != operator_id
    ):
        raise ContractError("OPERATOR_CHECKPOINT_BINDING")
    return copy.deepcopy(value)


def _test_only_terminal_projection(
    readiness, *, run_id, collection_profile_digest, approval_scope,
    decision_source, mechanical_proxy, human_semantic_outcome,
):
    fields = {
        "schema_version", "run_id", "transaction_id", "episode_index",
        "collection_profile_digest", "quality_contract_digest",
        "observed_monotonic_ns", "metrics",
    }
    if (
        not isinstance(readiness, dict)
        or set(readiness) != fields
        or readiness.get("schema_version") != "data_factory.recorder_readiness_evidence.v1"
        or readiness.get("run_id") != run_id
        or readiness.get("collection_profile_digest") != collection_profile_digest
        or readiness.get("quality_contract_digest") != canonical_digest(TEST_ONLY_READINESS_CONTRACT)
        or not isinstance(readiness.get("transaction_id"), str)
        or not readiness["transaction_id"]
        or type(readiness.get("episode_index")) is not int
        or readiness["episode_index"] < 0
        or type(readiness.get("observed_monotonic_ns")) is not int
        or not isinstance(readiness.get("metrics"), dict)
        or readiness["metrics"].get("quality_accepted") is not True
    ):
        raise ContractError("TEST_ONLY_READINESS_EVIDENCE")
    if approval_scope == "HIL_NUMERIC_PROXY":
        if mechanical_proxy != "MECHANICAL_GRASP_PROXY_PASS" or human_semantic_outcome != "NOT_MEASURED":
            raise ContractError("TEST_ONLY_PROXY_EVIDENCE")
    elif human_semantic_outcome != "PASS":
        raise ContractError("TEST_ONLY_HUMAN_SEMANTIC_EVIDENCE")
    return {
        "data_disposition": "TEST_ONLY",
        "candidate_admission_written": False,
        "decision_source": decision_source,
        "human_semantic_outcome": human_semantic_outcome,
        "mechanical_grasp_proxy": mechanical_proxy,
        "recorder_readiness": copy.deepcopy(readiness),
        "recorder_readiness_digest": canonical_digest(readiness),
    }


def _operator_summary(result):
    """Only executor-proven geometry may be shown as an execution approval summary."""
    envelope = result.get("plan_envelope")
    summary = envelope.get("operator_summary") if isinstance(envelope, dict) else None
    if not isinstance(summary, dict):
        raise ContractError("OPERATOR_SUMMARY_UNAVAILABLE")
    required = {"path", "flow", "speed", "clearance"}
    if frozenset(summary) not in {frozenset(required), frozenset(required | {"recycle"})} or not isinstance(summary["path"], list) or not all(isinstance(value, str) for value in summary["path"]):
        raise ContractError("OPERATOR_SUMMARY_SCHEMA")
    if not isinstance(summary["speed"], dict) or not summary["speed"]:
        raise ContractError("OPERATOR_SUMMARY_SCHEMA")
    plan = envelope.get("plan")
    steps = plan.get("steps") if isinstance(plan, Mapping) else None
    markers = [
        step.get("phase") for step in steps or []
        if isinstance(step, Mapping)
        and step.get("pause_after") == "SEMANTIC_VERDICT"
    ]
    boundary = markers[0] if len(markers) == 1 else None
    if boundary is None and isinstance(summary.get("recycle"), Mapping):
        boundary = summary["recycle"].get("recording_boundary_after")
    if boundary is None and isinstance(summary.get("flow"), Mapping):
        continuous = summary["flow"].get("continuous_through")
        if continuous in {"LIFT_LIN", "RETREAT_LIN"}:
            boundary = continuous
    allowed_boundaries = {
        get_task_recipe(task_id)["recording_boundary"] for task_id in TASK_IDS
    }
    expected_flow = {
        "continuous_through": boundary,
        "next_human_hold": (
            "POST_LIFT_SEMANTIC" if boundary == "LIFT_LIN"
            else "POST_RETREAT_SEMANTIC"
        ),
    }
    if boundary not in allowed_boundaries or summary["flow"] not in (
        {"continuous_through": "APPROACH_STOP_LIN", "next_human_hold": "PRECONTACT_HUMAN"},
        expected_flow,
    ):
        raise ContractError("OPERATOR_SUMMARY_SCHEMA")
    if not isinstance(summary["clearance"], dict) or summary["clearance"].get("status") != "COLLISION_CHECKED_NO_DISTANCE":
        raise ContractError("OPERATOR_SUMMARY_SCHEMA")
    if "recycle" in summary:
        recycle = summary["recycle"]
        if (
            not isinstance(recycle, dict)
            or set(recycle) != {"recording_boundary_after", "path", "release_slot_id", "release_target", "safe_staging_joint_positions_rad", "plan_digest"}
            or recycle["recording_boundary_after"] != boundary
            or recycle["path"] != ["RECYCLE_APPROACH_PTP", "LOWER_LIN", "GRIPPER_OPEN", "RETREAT_LIN", "SAFE_POSE_PTP"]
            or not all(isinstance(recycle[key], str) and DIGEST.fullmatch(recycle[key]) for key in ("release_slot_id", "plan_digest"))
        ):
            raise ContractError("OPERATOR_SUMMARY_SCHEMA")
    return copy.deepcopy(summary)


def _recover_quality_rejected_recycle(result, summary, cell_store, operator_id, payload, plan_digest):
    execution = result.get("execution_evidence")
    release_evidence = execution.get("release_evidence") if isinstance(execution, dict) else None
    transition = execution.get("scene_transition") if isinstance(execution, dict) else None
    if (
        result.get("code") != "QUALITY_REJECTED"
        or result.get("state") != "ABORTED"
        or result.get("executor_state") != "COMPLETED"
        or result.get("recorder_state") != "ABORTED"
        or not isinstance(summary.get("recycle"), dict)
        or not isinstance(release_evidence, dict)
        or not _release_outcome_landed(release_evidence)
        or release_evidence.get("release_slot_id") != summary["recycle"]["release_slot_id"]
        or not isinstance(transition, dict)
        or not isinstance(transition.get("scene_state_digest"), str)
        or not DIGEST.fullmatch(transition["scene_state_digest"])
        or transition.get("release_evidence_digest") != canonical_digest(release_evidence)
        or result.get("frozen_rows") != result.get("rows_after_recycle")
    ):
        raise ContractError("RECYCLE_EVIDENCE")
    cell = cell_store.read()
    if cell.get("cell_ready") is not False or cell.get("run_id") != payload["run_id"] or cell.get("plan_digest") != plan_digest:
        raise ContractError("POSTREJECT_CELL_STATE")
    return transition["scene_state_digest"], cell_store.acknowledge_ready(
        operator_id, expected_run_id=payload["run_id"], expected_plan_digest=plan_digest,
    )


def _release_outcome_landed(evidence):
    if not isinstance(evidence, Mapping):
        return False
    if evidence.get("schema_version") == "data_factory.recycle_release_evidence.v1":
        return evidence.get("human_verdict") == "LANDED"
    return (
        evidence.get("schema_version") == "data_factory.recycle_release_evidence.v2"
        and evidence.get("release_outcome") in {"LANDED", "EXPECTED_LANDED"}
        and evidence.get("outcome_source") in {
            "HUMAN_TTY", "LOCAL_UI_BUTTON", "CAMPAIGN_CONTROL_PROXY",
        }
    )


def _technical_validator(dataset_root, _payload, profile):
    command = [
        DATA_PYTHON, str(ROOT / "tools/validate_lerobot_dataset.py"), dataset_root,
        "--repo-id", profile["repo_id"],
        "--expected-fps", str(profile["fps"]), "--require-hil-motion",
        "--require-alignment-tail", "--skip-decoded-image-diagnostics",
    ]
    episode_index = None
    if isinstance(_payload, Mapping) and isinstance(_payload.get("run_root"), str) and isinstance(_payload.get("run_id"), str):
        try:
            recorder_result = load_json_strict(_run_dir(_payload) / "result.json")
        except (ContractError, OSError):
            recorder_result = None
        if (
            isinstance(recorder_result, Mapping)
            and recorder_result.get("schema_version") == "data_factory.recorder_result.v1"
            and recorder_result.get("state") == "COMMITTED"
            and type(recorder_result.get("episode_index")) is int
            and recorder_result["episode_index"] >= 0
        ):
            episode_index = recorder_result["episode_index"]
            command.extend(["--episode-locator-index", str(episode_index)])
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180,
    )
    locator = None
    locator_ok = episode_index is None
    if episode_index is not None and completed.returncode == 0:
        lines = [
            line.removeprefix(EPISODE_LOCATOR_PREFIX)
            for line in completed.stdout.splitlines()
            if line.startswith(EPISODE_LOCATOR_PREFIX)
        ]
        if len(lines) == 1:
            try:
                candidate = json.loads(lines[0])
                canonical = build_lerobot_v3_episode_locator(
                    repo_id=profile["repo_id"],
                    episode_index=episode_index,
                    data=candidate.get("data") if isinstance(candidate, Mapping) else None,
                    videos=candidate.get("videos") if isinstance(candidate, Mapping) else None,
                )
                if candidate == canonical:
                    locator, locator_ok = canonical, True
            except (ContractError, json.JSONDecodeError, TypeError):
                pass
    ok = completed.returncode == 0 and locator_ok
    return {
        "ok": ok, "code": "PASS" if ok else "FAIL",
        "result_digest": canonical_digest({"command": command[1:], "returncode": completed.returncode, "output": completed.stdout}),
        "episode_locator": locator,
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


def write_candidate_admission(
    payload, validated, technical_reference, *, operational_source="HUMAN_GATED",
):
    if technical_reference.get("status") != "PASS":
        raise ContractError("CANDIDATE_ADMISSION_TECHNICAL_PASS")
    if operational_source not in {"HUMAN_GATED", "HIL_PROXY"}:
        raise ContractError("CANDIDATE_ADMISSION_OPERATIONAL_SOURCE")
    admission = {
        "schema_version": "data_factory.candidate_admission.v1",
        "run_id": payload["run_id"],
        "operational_gate": "PASS",
        "operational_source": operational_source,
        "checklist_id": task_review_checklist_id(
            validated["normalized_job"]["task"],
        ),
        "review_context_digest": canonical_digest({
            "run_id": payload["run_id"],
            "resolved_job_digest": validated["resolved_job_digest"],
            "plan_digest": technical_reference["plan_digest"],
            "technical_validator_digest": canonical_digest(technical_reference),
        }),
        "semantic_status": "PENDING",
        "reviewed_by": None,
        "reviewed_at": None,
        "reason": None,
    }
    write_json_atomic(_run_dir(payload) / "candidate_admission.json", admission)
    return admission


def bind_candidate_episode_state(ledger_reference, candidate_path):
    """Bind one postcommit candidate to the rewritable ledger projection."""
    if not isinstance(ledger_reference, Mapping):
        raise ContractError("EPISODE_LEDGER_REFERENCE")
    ledger_path = Path(ledger_reference.get("path", ""))
    state_path = Path(ledger_reference.get("state_path", ""))
    candidate_path = Path(candidate_path)
    if (
        ledger_path.name != "episode_ledger.json"
        or state_path.name != "episode_ledger_state.json"
        or candidate_path.name != "candidate_admission.json"
    ):
        raise ContractError("EPISODE_LEDGER_REFERENCE")
    try:
        ledger_path = ledger_path.resolve(strict=True)
        state_path = state_path.resolve(strict=True)
        candidate_path = candidate_path.resolve(strict=True)
    except OSError as exc:
        raise ContractError("EPISODE_LEDGER_REFERENCE") from exc
    if not ledger_path.parent == state_path.parent == candidate_path.parent:
        raise ContractError("EPISODE_LEDGER_REFERENCE")
    ledger = load_json_strict(ledger_path)
    current_state = load_json_strict(state_path)
    candidate = load_json_strict(candidate_path)
    candidate_ref = {
        "artifact_path": str(candidate_path),
        "artifact_digest": canonical_digest(candidate),
    }
    updated = reproject_episode_state(
        ledger=ledger, current_state=current_state, candidate=candidate_ref,
    )
    write_json_atomic(state_path, updated)
    reference = copy.deepcopy(dict(ledger_reference))
    reference.update({
        "state_digest": updated["state_digest"],
        "review_status": updated["review"]["semantic_status"],
        "retention_state": updated["retention"]["retention_state"],
        "reclaim_state": updated["retention"]["reclaim_state"],
        "training_status": updated["review"]["training_status"],
    })
    return reference


def review_candidate_admission(
    path, *, expected_file_digest, expected_review_context_digest, checklist_id,
    semantic_status, reviewed_by, reason=None, clock=lambda: datetime.now(timezone.utc),
):
    """Atomically consume one exact pending candidate review."""
    path = Path(path)
    if (
        path.name != "candidate_admission.json"
        or not isinstance(expected_file_digest, str) or not DIGEST.fullmatch(expected_file_digest)
        or not isinstance(expected_review_context_digest, str) or not DIGEST.fullmatch(expected_review_context_digest)
        or checklist_id not in TASK_REVIEW_CHECKLIST_IDS
        or semantic_status not in {"PASS", "FAIL", "UNCERTAIN"}
        or not isinstance(reviewed_by, str) or reviewed_by == "HUMAN" or not SAFE_ID.fullmatch(reviewed_by)
        or (semantic_status == "PASS" and reason is not None)
        or (semantic_status != "PASS" and reason not in CANDIDATE_REVIEW_REASONS)
    ):
        raise ContractError("CANDIDATE_REVIEW_SCHEMA")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path.parent, flags)
    except OSError as exc:
        raise ContractError("CANDIDATE_REVIEW_PATH") from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if path.is_symlink() or not path.is_file():
            raise ContractError("CANDIDATE_REVIEW_PATH")
        current = _load(path, "CANDIDATE_REVIEW_IO")
        if canonical_digest(current) != expected_file_digest:
            raise ContractError("CANDIDATE_REVIEW_FILE_CHANGED")
        if (
            not isinstance(current, dict) or set(current) != CANDIDATE_ADMISSION_KEYS
            or current.get("schema_version") != "data_factory.candidate_admission.v1"
            or not isinstance(current.get("run_id"), str) or not SAFE_ID.fullmatch(current["run_id"])
            or current.get("operational_gate") != "PASS"
            or current.get("operational_source") not in {"HUMAN_GATED", "HIL_PROXY"}
            or current.get("checklist_id") != checklist_id
            or current.get("review_context_digest") != expected_review_context_digest
            or current.get("semantic_status") != "PENDING"
            or any(current.get(key) is not None for key in ("reviewed_by", "reviewed_at", "reason"))
        ):
            raise ContractError("CANDIDATE_REVIEW_STATE")
        reviewed_at = clock()
        if not isinstance(reviewed_at, datetime) or reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
            raise ContractError("CANDIDATE_REVIEW_TIME")
        updated = {
            **current, "semantic_status": semantic_status, "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "reason": reason,
        }
        write_json_atomic(path, updated)
        return updated
    finally:
        os.close(descriptor)


def _campaign_candidate_reviews(campaign, tty_decision=_tty_decision):
    """Review campaign candidates after their live calls have returned and closed children."""
    reviews = []
    review_enabled = True
    for index, episode in enumerate(campaign["episodes"], 1):
        run = episode["run"]
        checklist_id = task_review_checklist_id(run["job"]["task"])
        run_dir = _run_dir(run)
        path = run_dir / "candidate_admission.json"
        technical = _load(run_dir / "technical_validator.json", "CANDIDATE_REVIEW_IO")
        expected_context = canonical_digest({
            "run_id": run["run_id"],
            "resolved_job_digest": technical.get("resolved_job_digest"),
            "plan_digest": technical.get("plan_digest"),
            "technical_validator_digest": canonical_digest(technical),
        })
        current = _load(path, "CANDIDATE_REVIEW_IO")
        current_digest = canonical_digest(current)
        pending = current.get("semantic_status") == "PENDING" if isinstance(current, dict) else False
        passed = current.get("semantic_status") == "PASS" if isinstance(current, dict) else False
        if (
            not isinstance(current, dict) or set(current) != CANDIDATE_ADMISSION_KEYS
            or current.get("schema_version") != "data_factory.candidate_admission.v1"
            or current.get("run_id") != run["run_id"]
            or current.get("operational_gate") != "PASS"
            or current.get("operational_source") not in {"HUMAN_GATED", "HIL_PROXY"}
            or current.get("checklist_id") != checklist_id
            or current.get("review_context_digest") != expected_context
            or current.get("semantic_status") not in {"PENDING", "PASS", "FAIL", "UNCERTAIN"}
            or pending and any(current.get(key) is not None for key in ("reviewed_by", "reviewed_at", "reason"))
            or not pending and (
                not isinstance(current.get("reviewed_by"), str)
                or current["reviewed_by"] == "HUMAN"
                or not SAFE_ID.fullmatch(current["reviewed_by"])
                or not isinstance(current.get("reviewed_at"), str)
                or not current["reviewed_at"]
            )
            or passed and current.get("reason") is not None
            or not pending and not passed and current.get("reason") not in CANDIDATE_REVIEW_REASONS
        ):
            raise ContractError("CANDIDATE_REVIEW_STATE")
        if pending and review_enabled:
            try:
                decision = tty_decision(
                    f"Review episode {index}/2 run={run['run_id']} technical={technical.get('status')} evidence={run_dir}",
                    ("PASS", "FAIL", "UNCERTAIN", "SKIP"),
                )
                if decision != "SKIP":
                    reason = None if decision == "PASS" else tty_decision("Choose the primary review reason", CANDIDATE_REVIEW_REASONS)
                    current = review_candidate_admission(
                        path, expected_file_digest=current_digest,
                        expected_review_context_digest=expected_context,
                        checklist_id=checklist_id,
                        semantic_status=decision, reviewed_by=run["job"]["operator_or_agent_id"], reason=reason,
                    )
                    current_digest = canonical_digest(current)
            except KeyboardInterrupt:
                review_enabled = False
            except ContractError as exc:
                if exc.code != "HUMAN_TTY_REQUIRED":
                    raise
                review_enabled = False
        reviews.append({
            "run_id": run["run_id"], "path": str(path), "file_digest": current_digest,
            "semantic_status": current["semantic_status"],
        })
    return reviews


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


def _validate_episode_ledger_context(value, *, episode_binding, run_id):
    """Bind campaign-owned immutable inputs before any postcommit file is written."""
    if not isinstance(value, Mapping) or set(value) != EPISODE_LEDGER_CONTEXT_FIELDS:
        raise ContractError("EPISODE_LEDGER_CONTEXT_FIELDS")
    manifest = value["manifest"]
    intent = value["intent"]
    if not isinstance(manifest, Mapping) or not isinstance(intent, Mapping):
        raise ContractError("EPISODE_LEDGER_CONTEXT_FIELDS")
    manifest = copy.deepcopy(dict(manifest))
    intent = copy.deepcopy(dict(intent))
    manifest_digest = manifest.get("manifest_digest")
    intent_digest = intent.get("intent_digest")
    slot = intent.get("slot")
    if (
        not isinstance(manifest_digest, str)
        or not DIGEST.fullmatch(manifest_digest)
        or manifest_digest != canonical_digest({
            key: item for key, item in manifest.items() if key != "manifest_digest"
        })
        or not isinstance(intent_digest, str)
        or not DIGEST.fullmatch(intent_digest)
        or intent_digest != canonical_digest({
            key: item for key, item in intent.items() if key != "intent_digest"
        })
        or intent.get("run_id") != run_id
        or intent.get("manifest_digest") != manifest_digest
        or not isinstance(slot, Mapping)
        or episode_binding.get("manifest_digest") != manifest_digest
        or episode_binding.get("intent_digest") != intent_digest
        or episode_binding.get("slot_digest") != canonical_digest(slot)
    ):
        raise ContractError("EPISODE_LEDGER_CONTEXT_BINDING")
    return {"manifest": manifest, "intent": intent}


def _json_artifact_ref(path, payload):
    path = Path(path).resolve(strict=True)
    return {"artifact_path": str(path), "artifact_digest": canonical_digest(payload)}


def _jsonl_artifact_ref(path, *, selected=None):
    path = Path(path).resolve(strict=True)
    try:
        rows = [load_json_strict(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, ContractError) as exc:
        raise ContractError("EPISODE_LEDGER_ARTIFACT_IO") from exc
    if not rows:
        raise ContractError("EPISODE_LEDGER_ARTIFACT_IO")
    payload = rows if selected is None else selected
    return {"artifact_path": str(path), "artifact_digest": canonical_digest(payload)}


def _lerobot_v3_episode_locator(dataset_root, repo_id, episode_index):
    """Read finalized LeRobot v3 metadata and return file-local shard ranges."""
    try:
        from lerobot.datasets.io_utils import load_episodes, load_info

        root = Path(dataset_root).resolve(strict=True)
        episodes = list(load_episodes(root))
        info = load_info(root)
        matches = [
            row for row in episodes
            if type(row.get("episode_index")) is int
            and row["episode_index"] == episode_index
        ]
        if len(matches) != 1:
            raise ContractError("EPISODE_LEDGER_LOCATOR_EPISODE")
        episode = matches[0]
        data_chunk = int(episode["data/chunk_index"])
        data_file = int(episode["data/file_index"])
        shard = [
            row for row in episodes
            if int(row["data/chunk_index"]) == data_chunk
            and int(row["data/file_index"]) == data_file
        ]
        file_base = min(int(row["dataset_from_index"]) for row in shard)
        dataset_from = int(episode["dataset_from_index"])
        dataset_to = int(episode["dataset_to_index"])
        rows = dataset_to - dataset_from
        if rows <= 0 or int(episode["length"]) != rows:
            raise ContractError("EPISODE_LEDGER_LOCATOR_RANGE")
        videos = []
        for camera_key in sorted(
            key for key, feature in info.features.items()
            if isinstance(feature, Mapping) and feature.get("dtype") == "video"
        ):
            prefix = f"videos/{camera_key}"
            chunk_index = int(episode[f"{prefix}/chunk_index"])
            file_index = int(episode[f"{prefix}/file_index"])
            timestamp_start = float(episode[f"{prefix}/from_timestamp"])
            timestamp_end = float(episode[f"{prefix}/to_timestamp"])
            frame_start = round(timestamp_start * info.fps)
            frame_end = round(timestamp_end * info.fps)
            if frame_end - frame_start != rows:
                raise ContractError("EPISODE_LEDGER_LOCATOR_RANGE")
            videos.append({
                "camera_key": camera_key,
                "chunk_index": chunk_index,
                "file_index": file_index,
                "relative_path": info.video_path.format(
                    video_key=camera_key,
                    chunk_index=chunk_index,
                    file_index=file_index,
                ),
                "file_frame_start": frame_start,
                "file_frame_end_exclusive": frame_end,
                "timestamp_start_s": timestamp_start,
                "timestamp_end_s": timestamp_end,
            })
        return build_lerobot_v3_episode_locator(
            repo_id=repo_id,
            episode_index=episode_index,
            data={
                "chunk_index": data_chunk,
                "file_index": data_file,
                "relative_path": info.data_path.format(
                    chunk_index=data_chunk, file_index=data_file,
                ),
                "file_row_start": dataset_from - file_base,
                "file_row_end_exclusive": dataset_to - file_base,
            },
            videos=videos,
        )
    except ContractError:
        raise
    except (ImportError, KeyError, OSError, TypeError, ValueError) as exc:
        raise ContractError("EPISODE_LEDGER_LOCATOR_IO") from exc


def _write_episode_ledger(
    payload, validated, profile, lifecycle, storage_reference,
    episode_binding, ledger_context, *, episode_locator=None,
):
    """Compile the immutable postcommit join from the existing owner artifacts."""
    context = _validate_episode_ledger_context(
        ledger_context, episode_binding=episode_binding, run_id=payload["run_id"],
    )
    if not isinstance(storage_reference, Mapping):
        raise ContractError("EPISODE_LEDGER_STORAGE_REQUIRED")
    episode_ref = storage_reference.get("episode_ref")
    episode_index = episode_ref.get("episode_index") if isinstance(episode_ref, Mapping) else None
    execution = getattr(lifecycle, "execution_response", None)
    if (
        type(episode_index) is not int
        or episode_index < 0
        or not isinstance(execution, Mapping)
        or execution.get("state") != "COMPLETED"
        or execution.get("ok") is not True
    ):
        raise ContractError("EPISODE_LEDGER_RUNTIME_EVIDENCE")
    run_dir = _run_dir(payload)
    documents = {
        "manifest": (run_dir / "campaign_manifest.json", context["manifest"]),
        "intent": (run_dir / "episode_intent.json", context["intent"]),
        "execution": (run_dir / "execution_response.json", copy.deepcopy(dict(execution))),
        "runtime_binding": (run_dir / "runtime_episode_binding.json", copy.deepcopy(dict(episode_binding))),
    }
    for path, document in documents.values():
        write_json_atomic(path, document)
    dataset_root = Path(payload["dataset_root"]).resolve(strict=True)
    quality_path = dataset_root / "meta/recording_quality.jsonl"
    try:
        quality_rows = [
            load_json_strict(line)
            for line in quality_path.read_text(encoding="utf-8").splitlines()
        ]
    except (OSError, ContractError) as exc:
        raise ContractError("EPISODE_LEDGER_RECORDING_QUALITY_IO") from exc
    matches = [row for row in quality_rows if row.get("episode_index") == episode_index]
    if len(matches) != 1:
        raise ContractError("EPISODE_LEDGER_RECORDING_QUALITY_EPISODE")
    artifacts = {
        "episode": _json_artifact_ref(run_dir / "storage_usage.json", storage_reference),
        "run": _json_artifact_ref(run_dir / "result.json", load_json_strict(run_dir / "result.json")),
        "staging_manifest": _json_artifact_ref(
            run_dir / "staging_manifest.json", load_json_strict(run_dir / "staging_manifest.json"),
        ),
        "manifest": _json_artifact_ref(*documents["manifest"]),
        "intent": _json_artifact_ref(*documents["intent"]),
        "plan": _json_artifact_ref(
            run_dir / "preapproval_evidence.json", load_json_strict(run_dir / "preapproval_evidence.json"),
        ),
        "technical": _json_artifact_ref(
            run_dir / "technical_validator.json", load_json_strict(run_dir / "technical_validator.json"),
        ),
        "source_provenance": _jsonl_artifact_ref(
            dataset_root / "meta/source_provenance" / f"episode-{episode_index:06d}.jsonl",
        ),
        "recording_quality": _jsonl_artifact_ref(quality_path, selected=matches[0]),
        "execution": _json_artifact_ref(*documents["execution"]),
        "runtime_binding": _json_artifact_ref(*documents["runtime_binding"]),
    }
    dataset_digest = canonical_digest({
        "repo_id": profile["repo_id"],
        "dataset_root": str(dataset_root),
        "episode_ref": episode_ref,
    })
    ledger = compile_episode_ledger(
        dataset={
            "dataset_id": f"dataset-{dataset_digest[7:23]}",
            "repo_id": profile["repo_id"],
            "dataset_root": str(dataset_root),
            "dataset_digest": dataset_digest,
        },
        artifacts=artifacts,
        episode_locator=(
            _lerobot_v3_episode_locator(dataset_root, profile["repo_id"], episode_index)
            if episode_locator is None else episode_locator
        ),
    )
    path = run_dir / "episode_ledger.json"
    write_json_atomic(path, ledger)
    state = project_episode_state(ledger=ledger)
    state_path = run_dir / "episode_ledger_state.json"
    write_json_atomic(state_path, state)
    return {
        "schema_version": "data_factory.episode_ledger_reference.v1",
        "path": str(path.resolve(strict=True)),
        "ledger_digest": ledger["ledger_digest"],
        "episode_ref_digest": ledger["episode"]["episode_ref_digest"],
        "technical_status": ledger["admission"]["technical_status"],
        "state_path": str(state_path.resolve(strict=True)),
        "state_digest": state["state_digest"],
        "review_status": state["review"]["semantic_status"],
        "retention_state": state["retention"]["retention_state"],
        "reclaim_state": state["retention"]["reclaim_state"],
        "training_status": "NOT_AUTHORIZED",
    }


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
        if not result["ok"]:
            return _response(ok=False, code=result["code"], state=result["state"], run_id=payload["run_id"], plan_digest=result["plan_digest"])
        envelope = result["plan_envelope"]
        safety = envelope["precommit_safety"]
        collision = envelope["precommit_evidence"]["collision_report"]
        no_motion = envelope["precommit_evidence"]["plan_only_no_motion"]
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
                "operator_summary": _operator_summary(result),
                "recycle_plan_digest": result["plan_envelope"]["operator_summary"].get("recycle", {}).get("plan_digest"),
                "plan_only_checks": {
                    "planning_scene_readback_digest": safety["planning_scene_readback_digest"],
                    "collision_report_digest": safety["collision_report_digest"],
                    "collision_sample_count": collision["sample_count"],
                    "collision_failure_count": collision["failure_count"],
                    "all_valid": collision["all_valid"],
                    "plan_only_no_motion_digest": safety["plan_only_no_motion_digest"],
                    **{key: no_motion[key] for key in ("max_joint_delta_rad", "gripper_delta_m", "execute_goal_count", "gripper_goal_count")},
                },
                "camera_semantic_authority": False,
                "training_authorized": False,
            },
        )
    except ContractError as exc:
        return _response(ok=False, code=exc.code, state="BLOCKED", run_id=payload.get("run_id"))
    except Exception as exc:
        return _response(ok=False, code="RUNNER_FAILED", state="BLOCKED", run_id=payload.get("run_id"), data={"detail": str(exc)})


def _close_runtime_child(child, cancel):
    if child is None:
        return
    preserved = getattr(child, "preserved", False)
    try:
        if preserved:
            child.release(timeout_s=1.0)
        else:
            child.close(timeout_s=1.0 if cancel.is_set() else None)
    except ContractError:
        if not cancel.is_set() and not preserved:
            raise


def run_live(payload, cancel, publish, *, resolver=resolve_inputs, executor_factory=_live_executor,
             recorder_factory=_recorder, validator_call=_technical_validator, tty_decision=_tty_decision,
             camera_warmup_call=_camera_warmup, before_approval=None, one_job=None,
             decision_provider=None, approval_scope="HUMAN_GATED",
             decision_timeout_s=None, checkpoint_provider=None,
             checkpoint_timeout_s=None, test_only_root_binding=None,
             test_only_episode_binding=None, test_only_start_binding=None,
             runtime_root_binding=None, runtime_episode_binding=None,
             runtime_start_binding=None,
             episode_ledger_context=None,
             preapproval_checklist=None,
             campaign_authorization=None,
             candidate_writer_enabled=True,
             repository_root=ROOT, clock=None):
    """Public single HIL run: plan and human approval precede recorder begin and motion."""
    executor = recorder = resource_monitor = warmup_pool = None
    resource_finished = False
    profile = None
    try:
        current_clock = clock or (lambda: datetime.now(timezone.utc))
        if not callable(current_clock):
            raise ContractError("RUNNER_CLOCK")
        if approval_scope not in {"HUMAN_GATED", "HIL_NUMERIC_PROXY"}:
            raise ContractError("APPROVAL_SCOPE")
        if checkpoint_provider is not None and not callable(checkpoint_provider):
            raise ContractError("OPERATOR_CHECKPOINT_PROVIDER")
        if checkpoint_timeout_s is not None and (
            isinstance(checkpoint_timeout_s, bool)
            or not isinstance(checkpoint_timeout_s, (int, float))
            or checkpoint_timeout_s < 0
        ):
            raise ContractError("OPERATOR_CHECKPOINT_TIMEOUT")
        if before_approval is not None and decision_provider is not None:
            raise ContractError("PLAN_DECISION_AMBIGUOUS")
        legacy_bindings = any(item is not None for item in (
            test_only_root_binding, test_only_episode_binding,
            test_only_start_binding,
        ))
        generic_bindings = any(item is not None for item in (
            runtime_root_binding, runtime_episode_binding, runtime_start_binding,
        ))
        if legacy_bindings and generic_bindings:
            raise ContractError("RUNTIME_BINDING_AMBIGUOUS")
        if legacy_bindings:
            runtime_root_binding = test_only_root_binding
            runtime_episode_binding = test_only_episode_binding
            runtime_start_binding = test_only_start_binding
        bound_runtime = runtime_root_binding is not None
        roots = None
        data_disposition = "PRODUCTION"
        if bound_runtime:
            roots = (
                validate_test_only_root_binding(
                    runtime_root_binding, repository_root=repository_root,
                )
                if legacy_bindings
                else validate_runtime_root_binding(
                    runtime_root_binding, repository_root=repository_root,
                )
            )
            data_disposition = (
                "TEST_ONLY" if legacy_bindings else roots["data_disposition"]
            )
            expected_writers = data_disposition == "PRODUCTION"
            if (
                roots["run_id"] != payload.get("run_id")
                or roots["run_root"] != str(Path(payload.get("run_root", "")).resolve())
                or roots["dataset_root"] != str(Path(payload.get("dataset_root", "")).resolve())
                or candidate_writer_enabled is not expected_writers
                or decision_provider is None
                or runtime_episode_binding is None
                or runtime_start_binding is None
                or data_disposition == "PRODUCTION"
                and episode_ledger_context is None
                or legacy_bindings and data_disposition != "TEST_ONLY"
            ):
                raise ContractError(f"{data_disposition}_RUN_BINDING")
            cell_root = Path(roots["cell_root"])
        else:
            if (
                candidate_writer_enabled is not True
                or runtime_episode_binding is not None
                or runtime_start_binding is not None
                or episode_ledger_context is not None
            ):
                raise ContractError("CANDIDATE_WRITER_SCOPE")
            cell_root = ROOT / "outputs/data_factory/cells"
        test_only = bound_runtime and data_disposition == "TEST_ONLY"
        if preapproval_checklist is not None and (
            not bound_runtime
            or checkpoint_provider is None
            or not isinstance(preapproval_checklist, Mapping)
            or not preapproval_checklist
        ):
            raise ContractError("PREAPPROVAL_CHECKLIST_SCOPE")
        if bound_runtime and resolver is resolve_inputs:
            validated, program, scene_binding = resolve_inputs(
                payload,
                scene_binding_call=lambda validated, release_pose, run_id: _scene_binding(
                    validated, release_pose, run_id, root=cell_root,
                ),
            )
        else:
            validated, program, scene_binding = resolver(payload)
        episode_binding = None
        _validate_runtime_collection_binding(validated, program)
        if bound_runtime:
            episode_binding = (
                validate_test_only_episode_binding(
                    runtime_episode_binding, roots=roots, normalized_job=validated,
                )
                if legacy_bindings
                else validate_runtime_episode_binding(
                    runtime_episode_binding, roots=roots, normalized_job=validated,
                )
            )
            try:
                expires_at = datetime.fromisoformat(
                    episode_binding["expires_at"].replace("Z", "+00:00")
                )
            except (AttributeError, ValueError) as exc:
                raise ContractError(f"{data_disposition}_EPISODE_EXPIRY") from exc
            if expires_at.astimezone(timezone.utc) <= current_clock().astimezone(timezone.utc):
                raise ContractError(f"{data_disposition}_EPISODE_EXPIRED")
        if campaign_authorization is not None:
            if (
                episode_binding is None
                or approval_scope != "HIL_NUMERIC_PROXY"
            ):
                raise ContractError("CAMPAIGN_AUTHORIZATION_BINDING")
            validate_campaign_authorization(
                campaign_authorization, now=current_clock(),
                operator_label=validated["normalized_job"]["operator_or_agent_id"],
                manifest_digest=episode_binding["manifest_digest"],
                data_disposition=data_disposition,
            )
            validate_runtime_campaign_scope(
                campaign_authorization, resolved_inputs=validated,
                motion_program=program, episode_binding=episode_binding,
                now=current_clock(),
            )
            if episode_ledger_context is None:
                raise ContractError("EPISODE_LEDGER_CONTEXT_REQUIRED")
        ledger_context = (
            _validate_episode_ledger_context(
                episode_ledger_context,
                episode_binding=episode_binding,
                run_id=payload["run_id"],
            )
            if episode_ledger_context is not None else None
        )
        profile = _collection_profile(validated, payload)
        if cancel.is_set():
            return _response(ok=False, code="CANCELLED", state="CANCELLED", run_id=payload["run_id"])
        cell_store = CellStateStore(cell_root, payload["expected_robot_system_id"])
        scene_store = SceneStateStore(cell_root, payload["expected_robot_system_id"])
        cell = cell_store.read()
        if cell.get("robot_system_id") != payload["expected_robot_system_id"] or cell.get("cell_ready") is not True:
            return _response(ok=False, code="CELL_NOT_READY", state="BLOCKED", run_id=payload["run_id"])
        _prepare_run_dir(payload)
        publish(_response(
            ok=True, code="PLANNING", state="PLANNING",
            run_id=payload["run_id"], data={"mode": "live", "progress": 10},
        ))
        warmup_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="camera-warmup",
        )
        camera_warmup_future = warmup_pool.submit(
            camera_warmup_call, payload, profile, cancel,
        )
        publish(_response(
            ok=True, code="CAMERA_WARMUP", state="PREPARING",
            run_id=payload["run_id"],
            data={"mode": "live", "progress": 25 if bound_runtime else 5},
        ))
        timeout_s = _timeout_s(program)
        executor = (
            executor_factory(payload, timeout_s, cell_root=cell_root)
            if executor_factory is _live_executor
            else executor_factory(payload, timeout_s)
        )
        forbidden = lambda _request: (_ for _ in ()).throw(ContractError("LIVE_RECORDER_NOT_STARTED"))
        if one_job is None:
            arguments = (forbidden, lambda request: executor.request(request, cancel))
            job = (
                OneJob(*arguments, cell_state_call=cell_store.read,
                       readiness_contract=RECORDER_READINESS_CONTRACT)
                if bound_runtime else OneJob(*arguments, cell_state_call=cell_store.read)
            )
        else:
            if getattr(one_job, "state", None) != "IDLE":
                raise ContractError("ONE_JOB_NOT_FRESH")
            expected_readiness = RECORDER_READINESS_CONTRACT if bound_runtime else None
            if getattr(one_job, "readiness_contract", None) != expected_readiness:
                raise ContractError("ONE_JOB_READINESS_SCOPE")
            job = one_job
            job.recorder_call = forbidden
            job.executor_call = lambda request: executor.request(request, cancel)
            job.cell_state_call = cell_store.read
        planned = None
        plan_error = None
        try:
            planned = job.plan_only(payload["run_id"], program, scene_binding)
        except Exception as exc:
            plan_error = exc
        camera_warmup = None
        warmup_error = None
        try:
            camera_warmup = camera_warmup_future.result()
        except Exception as exc:
            warmup_error = exc
        finally:
            warmup_pool.shutdown(wait=True)
            warmup_pool = None
        if plan_error is not None:
            raise plan_error
        if not planned["ok"]:
            return _response(ok=False, code=planned["code"], state=planned["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
        planned_start = (
            (
                validate_test_only_planned_start
                if legacy_bindings else validate_runtime_planned_start
            )(
                start_binding=runtime_start_binding, episode_binding=episode_binding,
                motion_program=program, plan=planned["plan_envelope"]["plan"],
            )
            if bound_runtime else None
        )
        runtime_projection = {}
        if bound_runtime:
            runtime_projection = {
                "runtime_episode_binding_digest": episode_binding["binding_digest"],
                "runtime_planned_start": copy.deepcopy(planned_start),
            }
            if test_only:
                runtime_projection.update({
                    "test_only_episode_binding_digest": episode_binding["binding_digest"],
                    "test_only_planned_start": copy.deepcopy(planned_start),
                })
        summary = _operator_summary(planned)
        preapproval_evidence = _write_preapproval_evidence(payload, validated, planned)
        if warmup_error is not None:
            if not isinstance(warmup_error, ContractError):
                raise warmup_error
            data = {
                "mode": "live", "measurement_outcome": "FAIL",
                "recorder_goal_count": 0, "execute_goal_count": 0,
                "camera_semantic_authority": False,
                "training_authorized": False,
            }
            if bound_runtime:
                data.update({
                    "operator_summary": summary,
                    "preapproval_evidence_digest": canonical_digest(preapproval_evidence),
                    **runtime_projection,
                })
            return _response(
                ok=False, code=warmup_error.code, state="BLOCKED",
                run_id=payload["run_id"], plan_digest=planned["plan_digest"],
                data=data,
            )
        if cancel.is_set():
            return _response(
                ok=False, code="CANCELLED", state="CANCELLED",
                run_id=payload["run_id"], plan_digest=planned["plan_digest"],
                data={
                    "measurement_outcome": "NOT_MEASURED",
                    "preapproval_evidence_digest": canonical_digest(preapproval_evidence),
                    "recorder_goal_count": 0, "execute_goal_count": 0,
                    "training_authorized": False,
                } if bound_runtime else None,
            )
        operator_id = validated["normalized_job"]["operator_or_agent_id"]
        site_confirmation = None
        if preapproval_checklist is not None:
            displayed_place = preapproval_checklist.get("place_alias")
            if not isinstance(displayed_place, str) or not displayed_place:
                displayed_place = "workspace"
            site_confirmation = _operator_checkpoint(
                checkpoint_provider,
                kind="PHYSICAL_SCENE_CONFIRMATION", run_id=payload["run_id"],
                plan_digest=planned["plan_digest"],
                prompt=(
                    f"Confirm the displayed {displayed_place} binding, object pose, empty "
                    f"gripper, clear cell, and E-stop monitoring before this {data_disposition} dispatch."
                ),
                choices=("READY", "CANCEL"), operator_id=operator_id,
                timeout_s=checkpoint_timeout_s,
                evidence={
                    "checklist": copy.deepcopy(dict(preapproval_checklist)),
                    "operator_summary": copy.deepcopy(summary),
                    "planned_start_evidence_digest": canonical_digest(planned_start),
                    "data_disposition": data_disposition,
                },
                expected_source=(
                    "CAMPAIGN_AUTHORIZATION"
                    if campaign_authorization is not None else "LOCAL_UI_BUTTON"
                ),
            )
            if site_confirmation is None or site_confirmation["choice"] != "READY":
                return _response(
                    ok=False, code="PAUSED_AWAITING_OPERATOR", state="PLANNED",
                    run_id=payload["run_id"], plan_digest=planned["plan_digest"],
                    data={
                        "measurement_outcome": "NOT_MEASURED",
                        "operator_summary": summary,
                        "preapproval_evidence_digest": canonical_digest(preapproval_evidence),
                        **runtime_projection,
                        "recorder_goal_count": 0, "execute_goal_count": 0,
                        "training_authorized": False,
                    },
                )
        publish(_response(ok=True, code="AWAITING_HUMAN_APPROVAL", state="PLANNED", run_id=payload["run_id"], plan_digest=planned["plan_digest"], data={
            "mode": "live", "operator_summary": summary, "resolved_job_digest": validated["resolved_job_digest"],
            "scene_binding": scene_binding, "preapproval_evidence_digest": canonical_digest(preapproval_evidence),
            "camera_warmup_digest": canonical_digest(camera_warmup), "progress": 35,
            **(runtime_projection if bound_runtime else {}),
            "camera_semantic_authority": False, "training_authorized": False,
        }))
        recycle_text = f" recycle={summary['recycle']}" if "recycle" in summary else ""
        approval_prompt = (
            f"Plan {planned['plan_digest']} path={' > '.join(summary['path'])} flow={summary['flow']} "
            f"clearance={summary['clearance']} speed={summary['speed']}{recycle_text}"
        )
        decision_source = "TTY"
        if decision_provider is not None:
            decision = _button_plan_decision(
                decision_provider,
                run_id=payload["run_id"], plan_digest=planned["plan_digest"],
                approval_scope=approval_scope,
                decision_binding={
                    "resolved_job_digest": validated["resolved_job_digest"],
                    "scene_binding_digest": canonical_digest(scene_binding),
                    "operator_summary_digest": canonical_digest(summary),
                    "operator_summary": copy.deepcopy(summary),
                    "preapproval_checklist": (
                        None if preapproval_checklist is None
                        else copy.deepcopy(dict(preapproval_checklist))
                    ),
                    "site_confirmation_digest": (
                        None if site_confirmation is None
                        else canonical_digest(site_confirmation)
                    ),
                    "data_disposition": data_disposition,
                    "root_binding_digest": roots["binding_digest"] if bound_runtime else None,
                    "episode_binding": copy.deepcopy(episode_binding),
                    **({
                        "start_binding_digest": planned_start["start_binding_digest"],
                        "planned_start_evidence": copy.deepcopy(planned_start),
                    } if bound_runtime else {}),
                },
                operator_id=operator_id, timeout_s=decision_timeout_s,
                expected_source=(
                    "CAMPAIGN_AUTHORIZATION"
                    if campaign_authorization is not None else "LOCAL_UI_BUTTON"
                ),
            )
            if decision is None:
                return _response(ok=False, code="PAUSED_AWAITING_OPERATOR", state="PLANNED", run_id=payload["run_id"], plan_digest=planned["plan_digest"], data={
                    "measurement_outcome": "NOT_MEASURED", "recorder_goal_count": 0,
                    "execute_goal_count": 0,
                    **(runtime_projection if bound_runtime else {}),
                    "training_authorized": False,
                })
            if decision["choice"] != "APPROVE":
                code = "PLAN_REJECTED" if decision["choice"] == "REJECT" else "CANCELLED"
                return _response(ok=False, code=code, state="CANCELLED", run_id=payload["run_id"], plan_digest=planned["plan_digest"], data={
                    "measurement_outcome": "NOT_MEASURED", "recorder_goal_count": 0,
                    "execute_goal_count": 0,
                    **(runtime_projection if bound_runtime else {}),
                    "training_authorized": False,
                })
            decision_source = decision["decision_source"]
        elif before_approval is not None:
            before_approval(approval_prompt, planned)
        else:
            tty_decision(approval_prompt, f"APPROVE {planned['plan_digest']}")
        if cancel.is_set():
            return _response(ok=False, code="CANCELLED", state="CANCELLED", run_id=payload["run_id"], plan_digest=planned["plan_digest"])
        approval_source = (
            "CAMPAIGN_AUTHORIZATION"
            if campaign_authorization is not None
            else decision_source if bound_runtime else "HUMAN"
        )
        approved = job.approve(_approval(
            payload["run_id"], planned["plan_digest"], operator_id,
            approval_scope, source=approval_source,
        ))
        if not approved["ok"]:
            return _response(ok=False, code=approved["code"], state=approved["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
        publish(_response(
            ok=True, code="RECORDER_STARTING", state="RUNNING",
            run_id=payload["run_id"], plan_digest=planned["plan_digest"],
            data={"mode": "live", "progress": 40},
        ))
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
            readiness_failure = started.get("readiness_failure_evidence")
            if isinstance(readiness_failure, dict):
                write_json_atomic(
                    _run_dir(payload) / "readiness_failure.json",
                    readiness_failure,
                )
            return _response(
                ok=False, code=started["code"], state=started["state"],
                run_id=payload["run_id"], plan_digest=planned["plan_digest"],
                data={
                    "mode": "live",
                    "readiness_failure": copy.deepcopy(readiness_failure),
                    "recorder_goal_count": 1,
                    "execute_goal_count": 0,
                    "camera_semantic_authority": False,
                    "training_authorized": False,
                },
            )
        publish(_response(
            ok=True, code="EXECUTING", state="RUNNING",
            run_id=payload["run_id"], plan_digest=planned["plan_digest"],
            data={"mode": "live", "progress": 50},
        ))
        decisions = queue.Queue(maxsize=1)
        pending = None
        mechanical_proxy = None
        human_semantic_outcome = "NOT_MEASURED"
        while True:
            if cancel.is_set():
                result = job.cancel()
                return _response(ok=False, code=result["code"], state=result["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
            poll_started = time.monotonic()
            result = job.poll()
            poll_elapsed = time.monotonic() - poll_started
            finalization_recorder = getattr(
                resource_monitor, "record_finalization_round_trip", None,
            )
            if (
                result.get("state") in {"AWAITING_CELL_READY", "COMMITTED"}
                and callable(finalization_recorder)
            ):
                finalization_recorder(poll_elapsed)
            else:
                resource_monitor.record_control_round_trip(poll_elapsed)
            if not result["ok"]:
                if result["code"] == "QUALITY_REJECTED" and "recycle" in summary:
                    transition_digest, cell = _recover_quality_rejected_recycle(
                        result, summary, cell_store, operator_id, payload, planned["plan_digest"],
                    )
                    return _response(ok=False, code=result["code"], state=result["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"], data={
                        "mode": "live", "operator_summary": summary,
                        "camera_warmup_digest": canonical_digest(camera_warmup),
                        "postreject_scene_state_digest": transition_digest, "postreject_cell_state": cell,
                        "frozen_rows": result["frozen_rows"], "rows_after_recycle": result["rows_after_recycle"],
                        "camera_semantic_authority": False, "training_authorized": False,
                    })
                return _response(ok=False, code=result["code"], state=result["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
            if result["state"] in {"AWAITING_CELL_READY", "COMMITTED"}:
                publish(_response(
                    ok=True, code="VALIDATING", state="RUNNING",
                    run_id=payload["run_id"], plan_digest=planned["plan_digest"],
                    data={"mode": "live", "progress": 90},
                ))
                test_only_projection = (
                    _test_only_terminal_projection(
                        result.get("readiness_evidence"), run_id=payload["run_id"],
                        collection_profile_digest=validated["input_digests"]["collection_profile"],
                        approval_scope=approval_scope, decision_source=decision_source,
                        mechanical_proxy=mechanical_proxy,
                        human_semantic_outcome=human_semantic_outcome,
                    )
                    if test_only else {}
                )
                technical = validator_call(payload["dataset_root"], payload, profile)
                if (
                    not isinstance(technical, dict)
                    or type(technical.get("ok")) is not bool
                    or technical.get("code") not in {"PASS", "FAIL"}
                    or technical["ok"] != (technical["code"] == "PASS")
                    or not isinstance(technical.get("result_digest"), str)
                    or not DIGEST.fullmatch(technical["result_digest"])
                ):
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
                ledger_reference = None
                if ledger_context is not None and storage_reference is not None:
                    try:
                        ledger_reference = _write_episode_ledger(
                            payload, validated, profile, job, storage_reference,
                            episode_binding, ledger_context,
                            episode_locator=technical.get("episode_locator"),
                        )
                    except (ContractError, OSError) as exc:
                        postcommit_error = postcommit_error or (
                            exc.code if isinstance(exc, ContractError)
                            else "EPISODE_LEDGER_WRITE_ERROR"
                        )
                terminal_error = postcommit_error or (None if technical["ok"] else "TECHNICAL_VALIDATOR_FAILED")
                if terminal_error is not None:
                    # A committed episode remains forensic evidence, but cannot unlock the cell.
                    cell = cell_store.mark_blocked(terminal_error, payload["run_id"], planned["plan_digest"])
                    return _response(ok=False, code=terminal_error, state="BLOCKED", run_id=payload["run_id"], plan_digest=planned["plan_digest"], data={
                        "mode": "live", "operator_summary": summary, "technical_validator": validator_reference,
                        "storage_usage": storage_reference, "resource_usage": resource_reference,
                        "episode_ledger": ledger_reference,
                        "camera_warmup_digest": canonical_digest(camera_warmup),
                        "postcommit_cell_state": cell,
                        "camera_semantic_authority": False, "training_authorized": False,
                    })
                if "recycle" in summary:
                    execution = result.get("execution_evidence")
                    release_evidence = execution.get("release_evidence") if isinstance(execution, dict) else None
                    transition = execution.get("scene_transition") if isinstance(execution, dict) else None
                    if (
                        not isinstance(release_evidence, dict)
                        or not _release_outcome_landed(release_evidence)
                        or release_evidence.get("release_slot_id") != summary["recycle"]["release_slot_id"]
                        or not isinstance(transition, dict)
                        or not isinstance(transition.get("scene_state_digest"), str)
                        or not DIGEST.fullmatch(transition["scene_state_digest"])
                        or transition.get("release_evidence_digest") != canonical_digest(release_evidence)
                        or result.get("frozen_rows") != result.get("rows_after_recycle")
                    ):
                        raise ContractError("RECYCLE_EVIDENCE")
                    cell = cell_store.acknowledge_ready(
                        operator_id, expected_run_id=payload["run_id"], expected_plan_digest=planned["plan_digest"],
                    )
                    finished = job.finish()
                    if not finished["ok"] or finished["state"] != "COMPLETE":
                        raise ContractError("CELL_READY_REQUIRED")
                    if candidate_writer_enabled:
                        admission = write_candidate_admission(
                            payload, validated, validator_reference,
                        )
                        if ledger_reference is not None and isinstance(admission, Mapping):
                            ledger_reference = bind_candidate_episode_state(
                                ledger_reference,
                                _run_dir(payload) / "candidate_admission.json",
                            )
                    return _response(ok=True, code="VALIDATED", state="COMPLETE", run_id=payload["run_id"], plan_digest=planned["plan_digest"], data={
                        "mode": "live", "operator_summary": summary, "technical_validator": validator_reference,
                        "storage_usage": storage_reference, "resource_usage": resource_reference,
                        "episode_ledger": ledger_reference,
                        "camera_warmup_digest": canonical_digest(camera_warmup),
                        "postcommit_scene_state_digest": transition["scene_state_digest"], "postcommit_cell_state": cell,
                        "frozen_rows": result["frozen_rows"], "rows_after_recycle": result["rows_after_recycle"],
                        **test_only_projection,
                        **(runtime_projection if bound_runtime else {}),
                        "camera_semantic_authority": False, "training_authorized": False,
                    })
                target = validated["normalized_job"]
                scene_prompt = (
                    "Confirm the robot is stopped, gripper is empty, path is clear, and the object is reset at "
                    f"({target['place_id']},{target['yaw_deg']},{target['x_mm']},{target['y_mm']})"
                )
                if checkpoint_provider is None:
                    tty_decision(scene_prompt, f"SCENE_READY {planned['plan_digest']}")
                else:
                    checkpoint = _operator_checkpoint(
                        checkpoint_provider,
                        kind="SCENE_READY", run_id=payload["run_id"],
                        plan_digest=planned["plan_digest"], prompt=scene_prompt,
                        choices=("SCENE_READY",), operator_id=operator_id,
                        timeout_s=checkpoint_timeout_s,
                        evidence={
                            "scene_binding_digest": canonical_digest(scene_binding),
                            "target_pose": {
                                key: target[key]
                                for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
                            },
                            "technical_result_digest": technical["result_digest"],
                            "validator_reference_digest": canonical_digest(validator_reference),
                            "data_disposition": data_disposition,
                        },
                    )
                    if checkpoint is None:
                        raise ContractError("OPERATOR_CHECKPOINT_TIMEOUT")
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
                if candidate_writer_enabled:
                    admission = write_candidate_admission(
                        payload, validated, validator_reference,
                    )
                    if ledger_reference is not None and isinstance(admission, Mapping):
                        ledger_reference = bind_candidate_episode_state(
                            ledger_reference,
                            _run_dir(payload) / "candidate_admission.json",
                        )
                return _response(ok=True, code="VALIDATED", state="COMPLETE", run_id=payload["run_id"], plan_digest=planned["plan_digest"], data={
                    "mode": "live", "operator_summary": summary, "technical_validator": validator_reference,
                    "storage_usage": storage_reference, "resource_usage": resource_reference,
                    "episode_ledger": ledger_reference,
                    "camera_warmup_digest": canonical_digest(camera_warmup),
                    "postcommit_scene_state_digest": scene["scene_state_digest"], "postcommit_cell_state": cell,
                    **test_only_projection,
                    **(runtime_projection if bound_runtime else {}),
                    "camera_semantic_authority": False, "training_authorized": False,
                })
            if result["state"] in {"GRASP_VERDICT", "SEMANTIC_VERDICT"}:
                if result["state"] == "SEMANTIC_VERDICT":
                    publish(_response(
                        ok=True,
                        code=(
                            "RECYCLING"
                            if summary.get("recycle", {}).get(
                                "recording_boundary_after"
                            ) == "LIFT_LIN"
                            else "FINALIZING"
                        ),
                        state="RUNNING",
                        run_id=payload["run_id"], plan_digest=planned["plan_digest"],
                        data={"mode": "live", "progress": 70},
                    ))
                if approval_scope == "HIL_NUMERIC_PROXY":
                    decision = hil_numeric_gripper_verdict(
                        result["state"], result.get("execution_evidence"),
                        program.get("gripper_requirements"),
                    )
                    acted = (job.grasp_verdict if result["state"] == "GRASP_VERDICT" else job.semantic_verdict)(
                        decision, operator_id, source="HIL_PROXY",
                    )
                    if not acted["ok"]:
                        return _response(ok=False, code=acted["code"], state=acted["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"], data={
                            "mechanical_grasp_proxy": "MECHANICAL_GRASP_PROXY_FAIL",
                            "human_semantic_outcome": "NOT_MEASURED", "training_authorized": False,
                        })
                    mechanical_proxy = "MECHANICAL_GRASP_PROXY_PASS"
                    continue
                if pending is None:
                    pending = result["state"]
                    prompt = (
                        "Confirm the physical grasp; PASS continues to lift, FAIL aborts"
                        if result["state"] == "GRASP_VERDICT"
                        else "Confirm the completed episode; PASS commits, FAIL discards"
                    )
                    checkpoint_evidence = {
                        "execution_evidence": copy.deepcopy(result.get("execution_evidence")),
                        "operator_summary_digest": canonical_digest(summary),
                        "approval_scope": approval_scope,
                        "data_disposition": data_disposition,
                    }

                    def verdict_in_background(
                        state=result["state"], text=prompt,
                        evidence=checkpoint_evidence,
                    ):
                        try:
                            if checkpoint_provider is None:
                                decision = tty_decision(text, ("PASS", "FAIL"))
                            else:
                                checkpoint = _operator_checkpoint(
                                    checkpoint_provider,
                                    kind=state, run_id=payload["run_id"],
                                    plan_digest=planned["plan_digest"], prompt=text,
                                    choices=("PASS", "FAIL"), operator_id=operator_id,
                                    timeout_s=checkpoint_timeout_s,
                                    evidence=evidence,
                                )
                                if checkpoint is None:
                                    raise ContractError("OPERATOR_CHECKPOINT_TIMEOUT")
                                decision = checkpoint["choice"]
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
                if state == "SEMANTIC_VERDICT":
                    human_semantic_outcome = decision
                continue
            if result["state"] == "RELEASE_VERDICT":
                if "recycle" not in summary:
                    raise ContractError("RECYCLE_EVIDENCE")
                recycle = summary["recycle"]
                if pending is None:
                    pending = result["state"]
                    release_prompt = (
                        f"Confirm object inside release slot {recycle['release_target']}, gripper empty, "
                        f"retreat complete, and safe staging {recycle['safe_staging_joint_positions_rad']}; "
                        f"recycle={recycle['plan_digest']}"
                    )
                    release_checkpoint_evidence = {
                        "release_target": copy.deepcopy(recycle["release_target"]),
                        "safe_staging_joint_positions_rad": copy.deepcopy(
                            recycle["safe_staging_joint_positions_rad"]
                        ),
                        "recycle_plan_digest": recycle["plan_digest"],
                        "execution_evidence_digest": canonical_digest(
                            result.get("execution_evidence")
                        ),
                        "data_disposition": data_disposition,
                    }

                    def release_in_background(evidence=release_checkpoint_evidence):
                        try:
                            if checkpoint_provider is None:
                                decision = tty_decision(
                                    release_prompt,
                                    (f"LANDED {recycle['plan_digest']}", "OFF_SLOT", "UNCERTAIN"),
                                )
                                decision = "LANDED" if decision.startswith("LANDED ") else decision
                                source = "HUMAN"
                            else:
                                checkpoint = _operator_checkpoint(
                                    checkpoint_provider,
                                    kind="RELEASE_VERDICT", run_id=payload["run_id"],
                                    plan_digest=planned["plan_digest"], prompt=release_prompt,
                                    choices=("LANDED", "OFF_SLOT", "UNCERTAIN"),
                                    operator_id=operator_id, timeout_s=checkpoint_timeout_s,
                                    evidence=evidence,
                                    expected_source=(
                                        "CAMPAIGN_CONTROL_PROXY"
                                        if campaign_authorization is not None
                                        else "LOCAL_UI_BUTTON"
                                    ),
                                )
                                if checkpoint is None:
                                    raise ContractError("OPERATOR_CHECKPOINT_TIMEOUT")
                                decision = checkpoint["choice"]
                                source = checkpoint["decision_source"]
                            decisions.put(("RELEASE_VERDICT", (decision, source)))
                        except Exception as exc:
                            decisions.put(("RELEASE_VERDICT", exc))

                    threading.Thread(target=release_in_background, daemon=True).start()
                try:
                    state, release_decision = decisions.get_nowait()
                except queue.Empty:
                    time.sleep(0.05)
                    continue
                pending = None
                if state != result["state"] or isinstance(release_decision, Exception):
                    cancelled = job.cancel()
                    return _response(ok=False, code=cancelled["code"], state=cancelled["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
                decision, source = release_decision
                acted = job.release_verdict(decision, operator_id, source=source)
                if not acted["ok"]:
                    return _response(ok=False, code=acted["code"], state=acted["state"], run_id=payload["run_id"], plan_digest=planned["plan_digest"])
                publish(_response(
                    ok=True, code="FINALIZING", state="RUNNING",
                    run_id=payload["run_id"], plan_digest=planned["plan_digest"],
                    data={"mode": "live", "progress": 80},
                ))
                continue
            if result["state"] == "PRECONTACT_HUMAN":
                if campaign_authorization is not None:
                    confirmed = job.confirm(
                        operator_id, source="CAMPAIGN_AUTHORIZATION",
                    )
                    if not confirmed["ok"]:
                        return _response(
                            ok=False, code=confirmed["code"], state=confirmed["state"],
                            run_id=payload["run_id"], plan_digest=planned["plan_digest"],
                        )
                    continue
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
        if warmup_pool is not None:
            warmup_pool.shutdown(wait=True)
        if resource_monitor is not None and not resource_finished:
            try:
                resource_monitor.finish({}, collection_settings=profile)
            except Exception:
                pass
        for child in (recorder, executor):
            _close_runtime_child(child, cancel)


def resolve_campaign_episode_inputs(
    payload, *, release_role, next_run_id=None, source_slot=None,
    cell_root=ROOT / "outputs/data_factory/cells", resolver=None,
):
    """Resolve one serial episode and seal its exact source/destination scene edge."""
    if release_role not in {"RELEASE_DESTINATION", "DESTINATION_THEN_NEXT_SOURCE"}:
        raise ContractError("CAMPAIGN_RELEASE_SLOT")
    if (release_role == "DESTINATION_THEN_NEXT_SOURCE") != (next_run_id is not None):
        raise ContractError("SCENE_SLOT_NEXT_RUN")
    root = Path(cell_root).resolve()
    resolver = resolve_inputs if resolver is None else resolver
    if not callable(resolver):
        raise ContractError("CAMPAIGN_RESOLVER")
    validated, program, binding = resolver(
        payload,
        scene_binding_call=lambda checked, release_pose, run_id: _scene_binding(
            checked, release_pose, run_id, root=root,
        ),
    )
    slot = binding.get("release_slot")
    if not isinstance(slot, dict):
        raise ContractError("CAMPAIGN_RELEASE_SLOT")
    binding = {**binding, "release_slot": {**slot, "role": release_role}}
    if next_run_id is not None:
        binding["allowed_next_run_id"] = _identifier(
            next_run_id, "SCENE_SLOT_NEXT_RUN",
        )
    if source_slot is not None and binding.get("source_slot") != source_slot:
        raise ContractError("SCENE_SLOT_NEXT_RUN")
    return validated, program, binding


def _campaign_episode(payload, cancel, publish, release_role, next_run_id, source_slot=None, before_approval=None):
    def campaign_resolver(value):
        return resolve_campaign_episode_inputs(
            value, release_role=release_role, next_run_id=next_run_id,
            source_slot=source_slot,
        )

    return run_live(payload, cancel, publish, resolver=campaign_resolver, before_approval=before_approval)


def run_campaign(payload, cancel, publish, *, episode_call=_campaign_episode,
                 scene_store_factory=SceneStateStore, tty_decision=_tty_decision):
    """Run exactly two ordinary live episodes, stopping before any later episode on fault."""
    campaign_id = payload.get("campaign_id") if isinstance(payload, dict) else None
    try:
        campaign = _campaign_manifest(payload)
        campaign_id = campaign["campaign_id"]
        digest = canonical_digest(campaign)
        results = []
        runs = campaign["episodes"]
        source_slot = None
        next_plan_digest = None
        approval_used = False
        rejected_episode = False
        for index, episode in enumerate(runs):
            if cancel.is_set():
                return _response(code="CANCELLED", state="CANCELLED", run_id=campaign_id, data={
                    "campaign_digest": digest, "episodes": results, "training_authorized": False,
                })
            next_run_id = runs[1]["run"]["run_id"] if index == 0 else None
            before_approval = None
            if index == 1:
                def approve_next(approval_prompt, planned):
                    nonlocal approval_used, next_plan_digest
                    envelope = planned.get("plan_envelope") if isinstance(planned, dict) else None
                    plan = envelope.get("plan") if isinstance(envelope, dict) else None
                    binding = plan.get("scene_binding") if isinstance(plan, dict) else None
                    plan_digest = planned.get("plan_digest") if isinstance(planned, dict) else None
                    if (
                        approval_used
                        or not isinstance(plan_digest, str)
                        or not DIGEST.fullmatch(plan_digest)
                        or not isinstance(binding, dict)
                        or binding.get("source_slot") != source_slot
                    ):
                        raise ContractError("CAMPAIGN_NEXT_PLAN")
                    tty_decision(
                        f"{approval_prompt}; confirm the chain slot landing, empty gripper, and clear next path",
                        f"LANDED_AND_APPROVE_NEXT {plan_digest}",
                    )
                    approval_used, next_plan_digest = True, plan_digest

                before_approval = approve_next
            result = episode_call(
                episode["run"], cancel, publish, episode["release_role"], next_run_id,
                source_slot, before_approval,
            )
            _exact(result, RESPONSE_KEYS, "CAMPAIGN_EPISODE_RESULT")
            if result["run_id"] != episode["run"]["run_id"]:
                raise ContractError("CAMPAIGN_EPISODE_RESULT")
            results.append(copy.deepcopy(result))
            if (
                index == 0
                and not result["ok"]
                and result["code"] == "QUALITY_REJECTED"
                and result["state"] == "ABORTED"
                and isinstance(result["plan_digest"], str)
                and DIGEST.fullmatch(result["plan_digest"])
            ):
                store = scene_store_factory(ROOT / "outputs/data_factory/cells", episode["run"]["expected_robot_system_id"])
                snapshot = store.snapshot()
                next_job = runs[1]["run"]["job"]
                source_pose = {key: next_job[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")}
                objects = [
                    item for item in snapshot["scene_state"]["objects"].values()
                    if item.get("object_profile_id") == next_job["object_profile_id"]
                    and item.get("state") == "ON_SURFACE"
                    and item.get("source") in {"ROBOT_RELEASE", "ROBOT_RELEASE_PROXY"}
                    and item.get("pose") == source_pose
                ]
                slots = [
                    (slot_id, slot) for slot_id, slot in snapshot["scene_state"].get("slot_allocations", {}).items()
                    if slot.get("state") == "LANDED_FOR_NEXT_SOURCE"
                    and slot.get("role") == "DESTINATION_THEN_NEXT_SOURCE"
                    and slot.get("allowed_run_id") == next_run_id
                    and slot.get("evidence_run_id") == episode["run"]["run_id"]
                    and slot.get("evidence_plan_digest") == result["plan_digest"]
                ]
                if len(objects) != 1 or len(slots) != 1:
                    return _response(code=result["code"], state=result["state"], run_id=campaign_id, data={
                        "campaign_digest": digest, "episodes": results, "training_authorized": False,
                    })
                slot_id, slot = slots[0]
                source_slot = {"slot_id": slot_id, "slot_digest": canonical_digest(slot), "allowed_run_id": next_run_id}
                rejected_episode = True
                publish(_response(
                    ok=True, code="EPISODE_REJECTED_CONTINUING", state="RUNNING", run_id=campaign_id,
                    data={"campaign_digest": digest, "rejected_episodes": 1, "training_authorized": False},
                ))
                continue
            if not result["ok"] or result["code"] != "VALIDATED" or result["state"] != "COMPLETE":
                return _response(code=result["code"], state=result["state"], run_id=campaign_id, data={
                    "campaign_digest": digest, "episodes": results, "training_authorized": False,
                })
            data = result["data"]
            technical = data.get("technical_validator") if isinstance(data, dict) else None
            if not isinstance(technical, dict) or technical.get("run_id") != episode["run"]["run_id"] or technical.get("status") != "PASS":
                raise ContractError("CAMPAIGN_TECHNICAL_PASS")
            if index == 0:
                summary = data.get("operator_summary")
                recycle = summary.get("recycle") if isinstance(summary, dict) else None
                slot_id = recycle.get("release_slot_id") if isinstance(recycle, dict) else None
                store = scene_store_factory(ROOT / "outputs/data_factory/cells", episode["run"]["expected_robot_system_id"])
                snapshot = store.snapshot()
                if data.get("postcommit_scene_state_digest") != snapshot["scene_state_digest"]:
                    raise ContractError("SCENE_STATE_CHANGED")
                slot = snapshot["scene_state"].get("slot_allocations", {}).get(slot_id)
                if (
                    not isinstance(slot, dict)
                    or slot.get("state") != "LANDED_FOR_NEXT_SOURCE"
                    or slot.get("role") != "DESTINATION_THEN_NEXT_SOURCE"
                    or slot.get("allowed_run_id") != next_run_id
                ):
                    raise ContractError("SCENE_SLOT_NEXT_RUN")
                source_slot = {"slot_id": slot_id, "slot_digest": canonical_digest(slot), "allowed_run_id": next_run_id}
            else:
                if not approval_used:
                    raise ContractError("CAMPAIGN_NEXT_PLAN")
                store = scene_store_factory(ROOT / "outputs/data_factory/cells", episode["run"]["expected_robot_system_id"])
                snapshot = store.snapshot()
                consumed = snapshot["scene_state"].get("slot_allocations", {}).get(source_slot["slot_id"])
                if (
                    data.get("postcommit_scene_state_digest") != snapshot["scene_state_digest"]
                    or not isinstance(consumed, dict)
                    or consumed.get("state") != "CONSUMED_PENDING_REVIEW"
                    or consumed.get("allowed_run_id") != episode["run"]["run_id"]
                ):
                    raise ContractError("SCENE_SLOT_NEXT_RUN")
            publish(_response(
                ok=True, code="EPISODE_COMPLETE", state="RUNNING", run_id=campaign_id,
                data={"campaign_digest": digest, "completed_episodes": len(results), "training_authorized": False},
            ))
        return _response(ok=not rejected_episode, code="CAMPAIGN_PARTIAL" if rejected_episode else "CAMPAIGN_COMPLETE", state="COMPLETE", run_id=campaign_id, data={
            "campaign_digest": digest, "next_plan_digest": next_plan_digest,
            "episodes": results, "training_authorized": False,
        })
    except ContractError as exc:
        return _response(code=exc.code, state="BLOCKED", run_id=campaign_id)
    except Exception as exc:
        return _response(code="RUNNER_FAILED", state="BLOCKED", run_id=campaign_id, data={"detail": str(exc)})


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
    recycle = {name: getattr(args, name, None) for name in ("recycle_x_mm", "recycle_y_mm")}
    if any(value is not None for value in recycle.values()):
        if any(value is None for value in recycle.values()):
            raise ContractError("RUN_PAYLOAD")
        payload.update(recycle)
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
    parser.add_argument("--recycle-x-mm", type=float)
    parser.add_argument("--recycle-y-mm", type=float)
    return parser


def _campaign_parser():
    parser = ContractArgumentParser(description="Run one bounded two-episode supervised campaign.")
    parser.add_argument("--manifest", required=True)
    return parser


def _review_parser():
    parser = ContractArgumentParser(description="Review one completed campaign without starting live children.")
    parser.add_argument("--campaign", required=True)
    return parser


def main(argv=None):
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        if arguments[:1] == ["campaign"]:
            args = _campaign_parser().parse_args(arguments[1:])
            payload = _campaign_manifest(load_json_strict(Path(args.manifest).read_text(encoding="utf-8")))
            result = run_campaign(payload, threading.Event(), lambda _: None)
            if result["ok"]:
                reviews = _campaign_candidate_reviews(payload)
                result["data"]["candidate_admissions"] = reviews
                if any(item["semantic_status"] == "PENDING" for item in reviews):
                    result["code"] = "CANDIDATE_SEMANTIC_PENDING"
            print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
            return 0 if result["ok"] else 2
        if arguments[:1] == ["review"]:
            args = _review_parser().parse_args(arguments[1:])
            payload = _campaign_manifest(load_json_strict(Path(args.campaign).read_text(encoding="utf-8")))
            reviews = _campaign_candidate_reviews(payload)
            pending = any(item["semantic_status"] == "PENDING" for item in reviews)
            result = _response(
                ok=True, code="CANDIDATE_SEMANTIC_PENDING" if pending else "REVIEW_COMPLETE", state="COMPLETE",
                run_id=payload["campaign_id"], data={"candidate_admissions": reviews, "training_authorized": False},
            )
            print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
            return 0
        args = _parser().parse_args(arguments)
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
