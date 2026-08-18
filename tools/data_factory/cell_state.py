"""Durable, fail-closed cell readiness state."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tools.data_factory_recovery import RecoveryError, decode_json_strict, write_json_atomic
from tools.fr5_data_factory import ContractError, DIGEST, RFC3339, SAFE_ID


STATE_KEYS = {"schema_version", "robot_system_id", "cell_ready", "reason_code", "run_id", "plan_digest", "acknowledged_by", "updated_at"}
SCHEMA_VERSION = "data_factory.cell_state.v1"


class CellStateStore:
    def __init__(self, root: Path | str, robot_system_id: str) -> None:
        self.root = Path(root).absolute()
        self.robot_system_id = self._safe_id(robot_system_id, "STATE_ROBOT_ID")

    @staticmethod
    def _safe_id(value: object, code: str) -> str:
        if not isinstance(value, str) or value in (".", "..") or not SAFE_ID.fullmatch(value):
            raise ContractError(code)
        return value

    def _root_exists(self, *, create: bool = False) -> bool:
        current = Path(self.root.anchor)
        for part in self.root.parts[1:]:
            current /= part
            if current.is_symlink():
                raise ContractError("STATE_PATH")
            if not current.exists():
                if not create:
                    return False
                current.mkdir(mode=0o700)
            if current.is_symlink() or not current.is_dir():
                raise ContractError("STATE_PATH")
        return True

    def _path(self, *, create_robot: bool = False) -> Path:
        if not self._root_exists(create=create_robot):
            return self.root / self.robot_system_id / "state.json"
        robot = self.root / self.robot_system_id
        if robot.is_symlink():
            raise ContractError("STATE_PATH")
        if not robot.exists() and create_robot:
            robot.mkdir(mode=0o700)
        if not robot.exists():
            return robot / "state.json"
        if not robot.is_dir() or robot.is_symlink():
            raise ContractError("STATE_PATH")
        state = robot / "state.json"
        if state.is_symlink():
            raise ContractError("STATE_PATH")
        try:
            state.relative_to(self.root)
        except ValueError as exc:
            raise ContractError("STATE_PATH") from exc
        return state

    def _validate(self, value: object) -> dict:
        if not isinstance(value, dict) or set(value) != STATE_KEYS:
            raise ContractError("STATE_SCHEMA")
        if value["schema_version"] != SCHEMA_VERSION or value["robot_system_id"] != self.robot_system_id:
            raise ContractError("STATE_SCHEMA")
        if type(value["cell_ready"]) is not bool:
            raise ContractError("STATE_SCHEMA")
        for key in ("reason_code", "run_id", "acknowledged_by"):
            self._safe_id(value[key], "STATE_SCHEMA")
        if not isinstance(value["plan_digest"], str) or not DIGEST.fullmatch(value["plan_digest"]):
            raise ContractError("STATE_SCHEMA")
        if not isinstance(value["updated_at"], str) or not RFC3339.fullmatch(value["updated_at"]):
            raise ContractError("STATE_SCHEMA")
        try:
            parsed = datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError("STATE_SCHEMA") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ContractError("STATE_SCHEMA")
        return value

    def read(self) -> dict:
        state = self._path()
        if not state.exists():
            return {"schema_version": SCHEMA_VERSION, "robot_system_id": self.robot_system_id, "cell_ready": False, "reason_code": "STATE_MISSING", "run_id": "NONE", "plan_digest": "sha256:" + "0" * 64, "acknowledged_by": "UNACKNOWLEDGED", "updated_at": "1970-01-01T00:00:00Z"}
        if not state.is_file():
            raise ContractError("STATE_PATH")
        try:
            return self._validate(decode_json_strict(state.read_text(encoding="utf-8"), "STATE_JSON", state))
        except (OSError, RecoveryError) as exc:
            raise ContractError("STATE_JSON", str(exc)) from exc

    def mark_blocked(self, reason_code: str, run_id: str, plan_digest: str) -> dict:
        reason_code = self._safe_id(reason_code, "STATE_REASON")
        run_id = self._safe_id(run_id, "STATE_RUN_ID")
        if not isinstance(plan_digest, str) or not DIGEST.fullmatch(plan_digest):
            raise ContractError("STATE_PLAN_DIGEST")
        state = self._path(create_robot=True)
        value = {"schema_version": SCHEMA_VERSION, "robot_system_id": self.robot_system_id, "cell_ready": False, "reason_code": reason_code, "run_id": run_id, "plan_digest": plan_digest, "acknowledged_by": "UNACKNOWLEDGED", "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        write_json_atomic(state, value)
        return value

    def acknowledge_ready(self, acknowledged_by: str) -> dict:
        acknowledged_by = self._safe_id(acknowledged_by, "STATE_ACKNOWLEDGER")
        current = self.read()
        value = {**current, "cell_ready": True, "reason_code": "HUMAN_ACKNOWLEDGED", "acknowledged_by": acknowledged_by, "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        write_json_atomic(self._path(create_robot=True), value)
        return value
