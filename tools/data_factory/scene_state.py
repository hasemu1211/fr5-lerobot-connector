#!/usr/bin/env python3
"""Small runtime object-state store for episode-to-episode replanning."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.data_factory.cell_state import CellStateStore
from tools.data_factory_recovery import RecoveryError, decode_json_strict, write_json_atomic
from tools.fr5_data_factory import ContractArgumentParser, ContractError, DIGEST, RFC3339, SAFE_ID, canonical_digest


SCHEMA_V1 = "data_factory.scene_state.v1"
SCHEMA_VERSION = "data_factory.scene_state.v2"
SCENE_V1_KEYS = {"schema_version", "robot_system_id", "revision", "objects", "updated_at"}
SCENE_KEYS = SCENE_V1_KEYS | {"slot_allocations"}
OBJECT_KEYS = {"instance_id", "object_profile_id", "state", "pose", "source", "updated_by", "updated_at"}
POSE_KEYS = {"place_id", "yaw_deg", "x_mm", "y_mm"}
OBJECT_STATES = {"ON_SURFACE", "HELD", "UNKNOWN"}
SOURCES = {"HUMAN", "AI", "ROBOT_ACTION", "ROBOT_RELEASE", "PERCEPTION"}
SCENE_BINDING_KEYS = {"scene_state_digest", "revision", "object_instance_id"}
RELEASE_SLOT_KEYS = {"slot_id", "robot_system_id", "pose", "object_profile_id", "exclusion_geometry_digest", "role"}
SLOT_KEYS = {"state", "role", "allowed_run_id", "evidence_run_id", "evidence_plan_digest", "evidence_digest", "updated_at"}
SLOT_STATES = {"AVAILABLE", "RESERVED", "LANDED_FOR_NEXT_SOURCE", "CONSUMED_PENDING_REVIEW", "QUARANTINED"}
SLOT_ROLES = {"PICK_SOURCE", "RELEASE_DESTINATION", "DESTINATION_THEN_NEXT_SOURCE"}
RELEASE_EVIDENCE_KEYS = {
    "schema_version", "run_id", "plan_digest", "release_slot_id",
    "expected_scene_state_digest", "expected_scene_revision",
    "gripper_reference_m", "gripper_feedback_m", "terminal_phases",
    "post_retreat_snapshot_digest", "next_start_tolerance_rad", "human_verdict",
}


def _id(value: object, code: str) -> str:
    if not isinstance(value, str) or value in {".", ".."} or not SAFE_ID.fullmatch(value):
        raise ContractError(code)
    return value


def _number(value: object, code: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(code)
    number = float(value)
    return int(number) if number.is_integer() else number


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        raise ContractError("SCENE_TIMESTAMP")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("SCENE_TIMESTAMP") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("SCENE_TIMESTAMP")
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if value != normalized:
        raise ContractError("SCENE_TIMESTAMP")
    return normalized


def _pose(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != POSE_KEYS:
        raise ContractError("SCENE_POSE")
    return {
        "place_id": _id(value["place_id"], "SCENE_POSE"),
        **{key: _number(value[key], "SCENE_POSE") for key in ("yaw_deg", "x_mm", "y_mm")},
    }


def release_slot(*, robot_system_id: str, pose: dict, object_profile_id: str, exclusion_geometry_digest: str, role: str = "RELEASE_DESTINATION") -> dict:
    """Build the one canonical same-slot recycle binding used by P4.5."""
    robot_system_id = _id(robot_system_id, "SCENE_SLOT")
    pose = _pose(pose)
    object_profile_id = _id(object_profile_id, "SCENE_SLOT")
    if not isinstance(exclusion_geometry_digest, str) or not DIGEST.fullmatch(exclusion_geometry_digest) or role not in SLOT_ROLES:
        raise ContractError("SCENE_SLOT")
    identity = {
        "robot_system_id": robot_system_id,
        **pose,
        "object_profile_id": object_profile_id,
        "exclusion_geometry_digest": exclusion_geometry_digest,
    }
    return {
        "slot_id": canonical_digest(identity),
        "robot_system_id": robot_system_id,
        "pose": pose,
        "object_profile_id": object_profile_id,
        "exclusion_geometry_digest": exclusion_geometry_digest,
        "role": role,
    }


def validate_release_slot(value: object, robot_system_id: str) -> dict:
    if not isinstance(value, dict) or set(value) != RELEASE_SLOT_KEYS:
        raise ContractError("SCENE_SLOT")
    normalized = release_slot(
        robot_system_id=robot_system_id,
        pose=value["pose"],
        object_profile_id=value["object_profile_id"],
        exclusion_geometry_digest=value["exclusion_geometry_digest"],
        role=value["role"],
    )
    if value["slot_id"] != normalized["slot_id"]:
        raise ContractError("SCENE_SLOT")
    return normalized


def _validate(value: object, robot_system_id: str) -> dict:
    if not isinstance(value, dict) or frozenset(value) not in {frozenset(SCENE_V1_KEYS), frozenset(SCENE_KEYS)}:
        raise ContractError("SCENE_SCHEMA")
    if value["schema_version"] not in {SCHEMA_V1, SCHEMA_VERSION} or value["robot_system_id"] != robot_system_id:
        raise ContractError("SCENE_SCHEMA")
    if (
        value["schema_version"] == SCHEMA_V1 and set(value) != SCENE_V1_KEYS
        or value["schema_version"] == SCHEMA_VERSION and set(value) != SCENE_KEYS
    ):
        raise ContractError("SCENE_SCHEMA")
    if type(value["revision"]) is not int or value["revision"] < 0 or not isinstance(value["objects"], dict):
        raise ContractError("SCENE_SCHEMA")
    _timestamp(value["updated_at"])
    for instance_id, item in value["objects"].items():
        if not isinstance(item, dict) or set(item) != OBJECT_KEYS or instance_id != item.get("instance_id"):
            raise ContractError("SCENE_OBJECT")
        _id(instance_id, "SCENE_OBJECT")
        _id(item["object_profile_id"], "SCENE_OBJECT")
        _id(item["updated_by"], "SCENE_OBJECT")
        if item["state"] not in OBJECT_STATES or item["source"] not in SOURCES:
            raise ContractError("SCENE_OBJECT")
        _timestamp(item["updated_at"])
        if item["state"] == "ON_SURFACE":
            _pose(item["pose"])
        elif item["pose"] is not None:
            raise ContractError("SCENE_POSE")
    slots = value.get("slot_allocations", {})
    if not isinstance(slots, dict):
        raise ContractError("SCENE_SLOT")
    for slot_id, item in slots.items():
        if not isinstance(slot_id, str) or not DIGEST.fullmatch(slot_id) or not isinstance(item, dict) or set(item) != SLOT_KEYS:
            raise ContractError("SCENE_SLOT")
        if item["state"] not in SLOT_STATES or item["role"] not in SLOT_ROLES:
            raise ContractError("SCENE_SLOT")
        for key in ("allowed_run_id", "evidence_run_id"):
            _id(item[key], "SCENE_SLOT")
        for key in ("evidence_plan_digest", "evidence_digest"):
            if not isinstance(item[key], str) or not DIGEST.fullmatch(item[key]):
                raise ContractError("SCENE_SLOT")
        _timestamp(item["updated_at"])
    return value


def validate_scene_binding(value: object) -> dict:
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(SCENE_BINDING_KEYS), frozenset(SCENE_BINDING_KEYS | {"release_slot"}),
    }:
        raise ContractError("SCENE_BINDING")
    if not isinstance(value["scene_state_digest"], str) or not DIGEST.fullmatch(value["scene_state_digest"]):
        raise ContractError("SCENE_BINDING")
    if type(value["revision"]) is not int or value["revision"] < 0:
        raise ContractError("SCENE_BINDING")
    _id(value["object_instance_id"], "SCENE_BINDING")
    result = dict(value)
    if "release_slot" in value:
        if not isinstance(value["release_slot"], dict):
            raise ContractError("SCENE_BINDING")
        result["release_slot"] = validate_release_slot(value["release_slot"], value["release_slot"].get("robot_system_id"))
    return result


class SceneStateStore:
    def __init__(self, root: Path | str, robot_system_id: str) -> None:
        self._cell = CellStateStore(root, robot_system_id)
        self.robot_system_id = self._cell.robot_system_id

    def _path(self, *, create: bool = False) -> Path:
        return self._cell.runtime_path("scene_state.json", create_robot=create)

    def read(self) -> dict:
        path = self._path()
        if not path.exists():
            return {"schema_version": SCHEMA_V1, "robot_system_id": self.robot_system_id, "revision": 0, "objects": {}, "updated_at": "1970-01-01T00:00:00Z"}
        if not path.is_file() or path.is_symlink():
            raise ContractError("SCENE_PATH")
        try:
            return _validate(decode_json_strict(path.read_text(encoding="utf-8"), "SCENE_JSON", path), self.robot_system_id)
        except (OSError, RecoveryError) as exc:
            raise ContractError("SCENE_JSON", str(exc)) from exc

    def snapshot(self) -> dict:
        scene = self.read()
        return {"scene_state": scene, "scene_state_digest": canonical_digest(scene)}

    @contextmanager
    def locked_snapshot(self, expected_digest: str):
        if not isinstance(expected_digest, str) or not DIGEST.fullmatch(expected_digest):
            raise ContractError("SCENE_BINDING")
        lock_path = self._cell.runtime_path("scene_state.lock", create_robot=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            snapshot = self.snapshot()
            if snapshot["scene_state_digest"] != expected_digest:
                raise ContractError("SCENE_STATE_CHANGED")
            yield snapshot
        finally:
            os.close(descriptor)

    def transition_release(
        self,
        *,
        instance_id: str,
        release_slot: dict,
        evidence: dict,
        updated_by: str,
        expected_digest: str,
        expected_revision: int,
    ) -> dict:
        """Publish the physical object and its slot in one scene-v2 revision."""
        instance_id = _id(instance_id, "SCENE_OBJECT")
        updated_by = _id(updated_by, "SCENE_OBJECT")
        release_slot = validate_release_slot(release_slot, self.robot_system_id)
        if not isinstance(expected_digest, str) or not DIGEST.fullmatch(expected_digest) or type(expected_revision) is not int or expected_revision < 0:
            raise ContractError("SCENE_BINDING")
        if not isinstance(evidence, dict) or set(evidence) != RELEASE_EVIDENCE_KEYS:
            raise ContractError("RELEASE_EVIDENCE")
        expected_terminals = ["RECYCLE_APPROACH_PTP", "LOWER_LIN", "GRIPPER_OPEN", "RETREAT_LIN", "SAFE_POSE_PTP"]
        if (
            evidence["schema_version"] != "data_factory.recycle_release_evidence.v1"
            or evidence["release_slot_id"] != release_slot["slot_id"]
            or evidence["expected_scene_state_digest"] != expected_digest
            or evidence["expected_scene_revision"] != expected_revision
            or evidence["human_verdict"] not in {"LANDED", "OFF_SLOT", "UNCERTAIN"}
            or not isinstance(evidence["terminal_phases"], list)
            or evidence["terminal_phases"] != expected_terminals[:len(evidence["terminal_phases"])]
            or evidence["human_verdict"] == "LANDED" and evidence["terminal_phases"] != expected_terminals
        ):
            raise ContractError("RELEASE_EVIDENCE")
        _id(evidence["run_id"], "RELEASE_EVIDENCE")
        for key in ("plan_digest", "post_retreat_snapshot_digest"):
            if not isinstance(evidence[key], str) or not DIGEST.fullmatch(evidence[key]):
                raise ContractError("RELEASE_EVIDENCE")
        for key in ("gripper_reference_m", "gripper_feedback_m"):
            if evidence[key] is None and evidence["human_verdict"] != "LANDED":
                continue
            if _number(evidence[key], "RELEASE_EVIDENCE") < 0:
                raise ContractError("RELEASE_EVIDENCE")
        if _number(evidence["next_start_tolerance_rad"], "RELEASE_EVIDENCE") < 0:
            raise ContractError("RELEASE_EVIDENCE")

        lock_path = self._cell.runtime_path("scene_state.lock", create_robot=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            current = self.read()
            if canonical_digest(current) != expected_digest or current["revision"] != expected_revision:
                raise ContractError("SCENE_STATE_CHANGED")
            item = current["objects"].get(instance_id)
            if not isinstance(item, dict) or item.get("object_profile_id") != release_slot["object_profile_id"]:
                raise ContractError("SCENE_OBJECT_NOT_READY")
            slots = dict(current.get("slot_allocations", {}))
            prior_slot = slots.get(release_slot["slot_id"])
            if prior_slot is not None and prior_slot.get("state") != "AVAILABLE":
                raise ContractError("SCENE_SLOT_UNAVAILABLE")

            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            landed = evidence["human_verdict"] == "LANDED"
            evidence_digest = canonical_digest(evidence)
            objects = dict(current["objects"])
            objects[instance_id] = {
                "instance_id": instance_id,
                "object_profile_id": release_slot["object_profile_id"],
                "state": "ON_SURFACE" if landed else "UNKNOWN",
                "pose": release_slot["pose"] if landed else None,
                "source": "ROBOT_RELEASE" if landed else "ROBOT_ACTION",
                "updated_by": updated_by,
                "updated_at": now,
            }
            slots[release_slot["slot_id"]] = {
                "state": "CONSUMED_PENDING_REVIEW" if landed else "QUARANTINED",
                "role": release_slot["role"],
                "allowed_run_id": evidence["run_id"],
                "evidence_run_id": evidence["run_id"],
                "evidence_plan_digest": evidence["plan_digest"],
                "evidence_digest": evidence_digest,
                "updated_at": now,
            }
            scene = _validate({
                "schema_version": SCHEMA_VERSION,
                "robot_system_id": self.robot_system_id,
                "revision": current["revision"] + 1,
                "objects": objects,
                "slot_allocations": slots,
                "updated_at": now,
            }, self.robot_system_id)
            write_json_atomic(self._path(create=True), scene)
            return {
                "scene_state": scene,
                "scene_state_digest": canonical_digest(scene),
                "release_evidence_digest": evidence_digest,
            }
        finally:
            os.close(descriptor)

    def update_object(
        self,
        *,
        instance_id: str,
        object_profile_id: str,
        state: str,
        source: str,
        updated_by: str,
        pose: dict | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        instance_id = _id(instance_id, "SCENE_OBJECT")
        object_profile_id = _id(object_profile_id, "SCENE_OBJECT")
        updated_by = _id(updated_by, "SCENE_OBJECT")
        if state not in OBJECT_STATES or source not in SOURCES:
            raise ContractError("SCENE_OBJECT")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise ContractError("SCENE_REVISION")
        if state == "ON_SURFACE":
            if not isinstance(pose, dict) or set(pose) != POSE_KEYS:
                raise ContractError("SCENE_POSE")
            pose = {
                "place_id": _id(pose["place_id"], "SCENE_POSE"),
                **{key: _number(pose[key], "SCENE_POSE") for key in ("yaw_deg", "x_mm", "y_mm")},
            }
        elif pose is not None:
            raise ContractError("SCENE_POSE")
        lock_path = self._cell.runtime_path("scene_state.lock", create_robot=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            current = self.read()
            if expected_revision is not None and current["revision"] != expected_revision:
                raise ContractError("SCENE_REVISION_CONFLICT")
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            objects = dict(current["objects"])
            objects[instance_id] = {
                "instance_id": instance_id,
                "object_profile_id": object_profile_id,
                "state": state,
                "pose": pose,
                "source": source,
                "updated_by": updated_by,
                "updated_at": now,
            }
            scene = _validate({**current, "revision": current["revision"] + 1, "objects": objects, "updated_at": now}, self.robot_system_id)
            write_json_atomic(self._path(create=True), scene)
            return {"scene_state": scene, "scene_state_digest": canonical_digest(scene)}
        finally:
            os.close(descriptor)


def main(argv=None) -> int:
    parser = ContractArgumentParser()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default="outputs/data_factory/cells")
    common.add_argument("--robot-system-id", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show", parents=[common])
    for name in ("set-surface", "set-held", "mark-unknown"):
        command = commands.add_parser(name, parents=[common])
        command.add_argument("--instance-id", required=True)
        command.add_argument("--object-profile-id", required=True)
        command.add_argument("--source", choices=sorted(SOURCES), required=True)
        command.add_argument("--updated-by", required=True)
        command.add_argument("--expect-revision", type=int)
        if name == "set-surface":
            command.add_argument("--place-id", required=True)
            command.add_argument("--yaw-deg", type=float, required=True)
            command.add_argument("--x-mm", type=float, required=True)
            command.add_argument("--y-mm", type=float, required=True)
    try:
        args = parser.parse_args(argv)
        store = SceneStateStore(args.root, args.robot_system_id)
        if args.command == "show":
            result = store.snapshot()
        else:
            pose = None
            if args.command == "set-surface":
                pose = {"place_id": args.place_id, "yaw_deg": args.yaw_deg, "x_mm": args.x_mm, "y_mm": args.y_mm}
            result = store.update_object(
                instance_id=args.instance_id,
                object_profile_id=args.object_profile_id,
                state={"set-surface": "ON_SURFACE", "set-held": "HELD", "mark-unknown": "UNKNOWN"}[args.command],
                source=args.source,
                updated_by=args.updated_by,
                pose=pose,
                expected_revision=args.expect_revision,
            )
    except ContractError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({"error": {"code": "SCENE_IO", "message": str(exc)}}, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
