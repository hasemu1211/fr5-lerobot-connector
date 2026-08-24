"""Pure fake learned-action stop/fault harness; it has no robot integration."""

from __future__ import annotations

import copy
import math
import time
from typing import Callable


IDLE = "IDLE"
ACTIVE = "ACTIVE"
STOPPED = "STOPPED"
FAULT = "FAULT"
TERMINAL_STATES = {STOPPED, FAULT}
_OBSERVATION_KEYS = {
    "captured_at_s", "observation.state", "observation.images.camera1",
    "observation.images.camera2",
}
_RGB_KEYS = {"dtype", "color_space", "shape", "data"}


class FakeCommandSink:
    """In-memory sink that permits one active fake command owner."""

    def __init__(self, *, fail_send: bool = False):
        self.active_owner: str | None = None
        self.commands: list[tuple[str, tuple[float, ...]]] = []
        self.fail_send = fail_send

    def claim(self, owner: str) -> None:
        if self.active_owner is not None:
            raise RuntimeError("COMMAND_OWNER_BUSY")
        self.active_owner = owner

    def send(self, owner: str, action: tuple[float, ...]) -> None:
        if owner != self.active_owner:
            raise RuntimeError("COMMAND_OWNER_MISMATCH")
        if self.fail_send:
            raise RuntimeError("SINK_FAULT")
        self.commands.append((owner, action))

    def release(self, owner: str) -> None:
        if self.active_owner == owner:
            self.active_owner = None


def fake_rgb(data: bytes = b"\x00\x00\x00", *, height: int = 1, width: int = 1) -> dict:
    if (
        isinstance(height, bool) or isinstance(width, bool) or not isinstance(height, int)
        or not isinstance(width, int) or height < 1 or width < 1 or not isinstance(data, bytes)
        or len(data) != height * width * 3
    ):
        raise ValueError("RGB_FRAME")
    return {"dtype": "uint8", "color_space": "RGB", "shape": [height, width, 3], "data": data}


def fake_observation(
    captured_at_s: float,
    *,
    state=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    camera1: dict | None = None,
    camera2: dict | None = None,
) -> dict:
    return {
        "captured_at_s": captured_at_s,
        "observation.state": list(state),
        "observation.images.camera1": copy.deepcopy(camera1) if camera1 is not None else fake_rgb(),
        "observation.images.camera2": copy.deepcopy(camera2) if camera2 is not None else fake_rgb(),
    }


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _rgb(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _RGB_KEYS:
        raise ValueError("RGB_FRAME")
    shape = value["shape"]
    if (
        value["dtype"] != "uint8" or value["color_space"] != "RGB"
        or not isinstance(shape, list) or len(shape) != 3 or shape[2] != 3
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in shape)
        or not isinstance(value["data"], bytes) or len(value["data"]) != math.prod(shape)
    ):
        raise ValueError("RGB_FRAME")
    return copy.deepcopy(value)


def _action(value: object) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != 7 or not all(
        _finite_number(item) for item in value
    ):
        raise ValueError("INVALID_ACTION")
    return tuple(float(item) for item in value)


class LearnedActionAdapter:
    """Single-goal synchronous adapter for fake policy and fake sink qualification."""

    def __init__(
        self,
        policy: Callable[[dict], object],
        sink: FakeCommandSink,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_observation_age_s: float = 0.3,
        watchdog_timeout_s: float = 1.0,
        owner_id: str = "learned-action-fake",
    ):
        if (
            not callable(policy) or not isinstance(sink, FakeCommandSink) or not callable(clock)
            or not _finite_number(max_observation_age_s) or max_observation_age_s <= 0
            or not _finite_number(watchdog_timeout_s) or watchdog_timeout_s <= 0
            or not isinstance(owner_id, str) or not owner_id
        ):
            raise ValueError("ADAPTER_CONFIG")
        self.policy = policy
        self.sink = sink
        self.clock = clock
        self.max_observation_age_s = float(max_observation_age_s)
        self.watchdog_timeout_s = float(watchdog_timeout_s)
        self.owner_id = owner_id
        self.state = IDLE
        self.terminal_reason: str | None = None
        self.active_goal_id: str | None = None
        self.last_progress_s: float | None = None
        self.policy_calls = 0
        self._owns_sink = False

    def _now(self) -> float:
        value = self.clock()
        if not _finite_number(value):
            raise ValueError("CLOCK")
        return float(value)

    def _terminal(self, state: str, reason: str) -> str:
        if self.state in TERMINAL_STATES:
            return self.state
        self.state = state
        self.terminal_reason = reason
        self.active_goal_id = None
        if self._owns_sink:
            self.sink.release(self.owner_id)
            self._owns_sink = False
        return self.state

    def start(self, goal_id: str) -> str:
        if self.state in TERMINAL_STATES:
            return self.state
        if self.state == ACTIVE:
            return self._terminal(FAULT, "COMPETING_GOAL")
        if not isinstance(goal_id, str) or not goal_id or "\x00" in goal_id:
            return self._terminal(FAULT, "GOAL_ID")
        try:
            now = self._now()
            self.sink.claim(self.owner_id)
            self._owns_sink = True
        except Exception:
            return self._terminal(FAULT, "COMMAND_OWNER")
        self.state = ACTIVE
        self.active_goal_id = goal_id
        self.last_progress_s = now
        return self.state

    def stop(self) -> str:
        return self._terminal(STOPPED, "STOP_REQUESTED")

    def cancel(self) -> str:
        return self._terminal(STOPPED, "CANCELLED")

    def check_watchdog(self) -> str:
        if self.state != ACTIVE:
            return self.state
        try:
            expired = self._now() - self.last_progress_s > self.watchdog_timeout_s
        except Exception:
            return self._terminal(FAULT, "CLOCK")
        return self._terminal(FAULT, "WATCHDOG") if expired else self.state

    def _observation(self, value: object, now: float) -> dict:
        if not isinstance(value, dict) or set(value) != _OBSERVATION_KEYS:
            raise ValueError("OBSERVATION_SCHEMA")
        captured = value["captured_at_s"]
        state = value["observation.state"]
        if (
            not _finite_number(captured) or now < captured or now - captured > self.max_observation_age_s
        ):
            raise ValueError("STALE_OBSERVATION")
        if not isinstance(state, (list, tuple)) or len(state) != 7 or not all(
            _finite_number(item) for item in state
        ):
            raise ValueError("OBSERVATION_STATE")
        return {
            "observation.state": [float(item) for item in state],
            "observation.images.camera1": _rgb(value["observation.images.camera1"]),
            "observation.images.camera2": _rgb(value["observation.images.camera2"]),
        }

    def step(self, observation: object) -> str:
        if self.state != ACTIVE:
            return self.state
        if self.check_watchdog() != ACTIVE:
            return self.state
        try:
            policy_input = self._observation(observation, self._now())
        except Exception as error:
            reason = str(error) if str(error) else "INVALID_OBSERVATION"
            return self._terminal(FAULT, reason)
        try:
            self.policy_calls += 1
            output = self.policy(policy_input)
        except Exception:
            return self._terminal(FAULT, "POLICY_EXCEPTION")
        if self.state != ACTIVE:
            return self.state
        if self.check_watchdog() != ACTIVE:
            return self.state
        try:
            action = _action(output)
        except Exception:
            return self._terminal(FAULT, "INVALID_ACTION")
        try:
            self.sink.send(self.owner_id, action)
            self.last_progress_s = self._now()
        except Exception:
            return self._terminal(FAULT, "SINK_FAULT")
        return self.state
