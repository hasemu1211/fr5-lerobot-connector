"""Foreground-only owner reuse for operator laptop bringup.

This module is called explicitly by setup code.  It has no thread, timer, recorder
hook, control callback, robot motion, or readiness authority.
"""
from __future__ import annotations

import copy
import os
import subprocess
from collections.abc import Callable, Mapping

from tools.fr5_data_factory import ContractError, SAFE_ID


COMPONENTS = ("robot", "controller", "gripper", "camera")
MOTION_COMPONENTS = frozenset(("robot", "controller", "gripper"))
FACT_STATES = frozenset(("READY", "MISSING", "SETUP_REQUIRED", "AMBIGUOUS"))
_DETACHERS = frozenset((
    "daemon", "daemonize", "launchctl", "nohup", "screen", "service",
    "setsid", "systemctl", "systemd-run", "tmux",
))
_SHELLS = frozenset(("bash", "dash", "fish", "sh", "zsh"))


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and SAFE_ID.fullmatch(value) is not None


class OperatorStack:
    """Attach to discovered owners and start only configured missing children."""

    def __init__(
        self,
        commands: Mapping[str, Mapping[str, object]],
        *,
        discover: Callable[[], Mapping[str, Mapping[str, object]]],
        process_factory: Callable[[tuple[str, ...]], object] = subprocess.Popen,
        gripper_setup: Callable[[dict[str, dict[str, object]]], object] | None = None,
        stop_timeout_s: float = 3.0,
    ) -> None:
        if not callable(discover) or not callable(process_factory):
            raise ContractError("OPERATOR_STACK_CALLABLE")
        if gripper_setup is not None and not callable(gripper_setup):
            raise ContractError("OPERATOR_STACK_CALLABLE")
        if (
            not isinstance(stop_timeout_s, (int, float))
            or isinstance(stop_timeout_s, bool)
            or stop_timeout_s <= 0
        ):
            raise ContractError("OPERATOR_STACK_STOP_TIMEOUT")
        self.commands = self._commands(commands)
        self.discover = discover
        self.process_factory = process_factory
        self.gripper_setup = gripper_setup
        self.stop_timeout_s = float(stop_timeout_s)
        self._children: dict[str, dict[str, object]] = {}
        self._last_facts: dict[str, dict[str, object]] | None = None

    @staticmethod
    def _commands(value: object) -> dict[str, dict[str, object]]:
        if not isinstance(value, Mapping):
            raise ContractError("OPERATOR_STACK_COMMANDS")
        result: dict[str, dict[str, object]] = {}
        claimed: set[str] = set()
        for name, spec in value.items():
            if not _valid_id(name) or not isinstance(spec, Mapping) or set(spec) != {
                "argv", "owner", "provides",
            }:
                raise ContractError("OPERATOR_STACK_COMMAND")
            argv = spec["argv"]
            provides = spec["provides"]
            owner = spec["owner"]
            if (
                not isinstance(argv, (list, tuple))
                or not argv
                or any(not isinstance(part, str) or not part or "\0" in part for part in argv)
                or not isinstance(provides, (list, tuple))
                or not provides
                or any(part not in COMPONENTS for part in provides)
                or len(set(provides)) != len(provides)
                or not _valid_id(owner)
            ):
                raise ContractError("OPERATOR_STACK_COMMAND", str(name))
            executable = os.path.basename(argv[0])
            shell_eval = executable in _SHELLS and any(
                part in ("-c", "-ic", "-lc", "-lic") for part in argv[1:]
            )
            detach_flag = any(
                part in ("--daemon", "--detach")
                or part.startswith("--daemon=")
                or part.startswith("--detach=")
                for part in argv[1:]
            )
            if executable in _DETACHERS or shell_eval or detach_flag:
                raise ContractError("OPERATOR_STACK_FOREGROUND", str(name))
            provided = set(provides)
            if provided & MOTION_COMPONENTS and provided != MOTION_COMPONENTS:
                raise ContractError("OPERATOR_STACK_MOTION_OWNER_SCOPE", str(name))
            if claimed & provided:
                raise ContractError("OPERATOR_STACK_COMPONENT_OVERLAP", str(name))
            claimed.update(provided)
            result[name] = {
                "argv": tuple(argv), "owner": owner, "provides": tuple(provides),
            }
        return result

    def _facts(self) -> dict[str, dict[str, object]]:
        value = self.discover()
        if not isinstance(value, Mapping) or set(value) != set(COMPONENTS):
            raise ContractError("OPERATOR_STACK_FACTS")
        result: dict[str, dict[str, object]] = {}
        for component in COMPONENTS:
            fact = value[component]
            if not isinstance(fact, Mapping) or set(fact) != {"state", "owner"}:
                raise ContractError("OPERATOR_STACK_FACT", component)
            state, owner = fact["state"], fact["owner"]
            if state not in FACT_STATES:
                raise ContractError("OPERATOR_STACK_FACT", component)
            if state in ("READY", "SETUP_REQUIRED"):
                if not _valid_id(owner):
                    raise ContractError("OPERATOR_STACK_FACT_OWNER", component)
            elif owner is not None:
                raise ContractError("OPERATOR_STACK_FACT_OWNER", component)
            if state == "SETUP_REQUIRED" and component != "gripper":
                raise ContractError("OPERATOR_STACK_FACT", component)
            result[component] = {"state": state, "owner": owner}
        self._last_facts = result
        return result

    @staticmethod
    def _measure(record: dict[str, object]) -> int | None:
        code = record["process"].poll()
        if code is not None:
            record["returncode"] = code
            if not record["stop_requested"]:
                record["unexpected_exit"] = True
        return code

    def _active(self, name: str) -> dict[str, object] | None:
        record = self._children.get(name)
        return record if record is not None and self._measure(record) is None else None

    def _validate_observation(self, facts: dict[str, dict[str, object]]) -> None:
        ambiguous = [name for name in COMPONENTS if facts[name]["state"] == "AMBIGUOUS"]
        if ambiguous:
            raise ContractError("OPERATOR_STACK_AMBIGUOUS", ",".join(ambiguous))
        for name, record in self._children.items():
            if self._measure(record) is not None:
                if record["unexpected_exit"] and not record["stop_requested"]:
                    raise ContractError("OPERATOR_STACK_CHILD_EXITED", name)
                continue
            if record["stop_requested"]:
                raise ContractError("OPERATOR_STACK_STOP_INCOMPLETE", name)
            spec = self.commands[name]
            for component in spec["provides"]:
                fact = facts[component]
                if fact["state"] in ("READY", "SETUP_REQUIRED") and fact["owner"] != spec["owner"]:
                    raise ContractError("OPERATOR_STACK_AMBIGUOUS", component)

        motion_active = any(
            self._active(name) is not None
            and bool(set(spec["provides"]) & MOTION_COMPONENTS)
            for name, spec in self.commands.items()
        )
        motion_states = {facts[name]["state"] for name in MOTION_COMPONENTS}
        motion_owners = {
            facts[name]["owner"] for name in MOTION_COMPONENTS
            if facts[name]["state"] in ("READY", "SETUP_REQUIRED")
        }
        if len(motion_owners) > 1:
            raise ContractError("OPERATOR_STACK_AMBIGUOUS", "motion_owner")
        if "SETUP_REQUIRED" in motion_states:
            raise ContractError("OPERATOR_STACK_GRIPPER_SETUP_REQUIRED")
        if "READY" in motion_states and "MISSING" in motion_states and not motion_active:
            raise ContractError("OPERATOR_STACK_PARTIAL_OWNER", "robot")

    def _start(self, name: str) -> None:
        spec = self.commands[name]
        try:
            process = self.process_factory(spec["argv"])
        except Exception as exc:
            raise ContractError("OPERATOR_STACK_START", f"{name}: {exc}") from exc
        if any(not callable(getattr(process, method, None)) for method in (
            "poll", "terminate", "kill", "wait",
        )):
            raise ContractError("OPERATOR_STACK_PROCESS_HANDLE", name)
        record = {
            "process": process,
            "returncode": None,
            "stop_requested": False,
            "terminate_timed_out": False,
            "kill_used": False,
            "stop_failed": False,
            "unexpected_exit": False,
        }
        self._children[name] = record
        code = self._measure(record)
        if code is not None:
            raise ContractError("OPERATOR_STACK_CHILD_EXITED", f"{name}:{code}")

    def _stop_record(self, record: dict[str, object]) -> bool:
        record["stop_requested"] = True
        if self._measure(record) is not None:
            return True
        process = record["process"]
        try:
            process.terminate()
            try:
                record["returncode"] = process.wait(self.stop_timeout_s)
            except subprocess.TimeoutExpired:
                record["terminate_timed_out"] = True
                record["kill_used"] = True
                process.kill()
                record["returncode"] = process.wait(self.stop_timeout_s)
        except (OSError, subprocess.TimeoutExpired):
            record["stop_failed"] = True
            return False
        return True

    def _stop_names(self, names: list[str]) -> list[str]:
        return [name for name in reversed(names) if not self._stop_record(self._children[name])]

    def ensure(self) -> dict[str, object]:
        """Reuse positive owners and start each wholly missing configured child once."""
        facts = self._facts()
        self._validate_observation(facts)
        started: list[str] = []
        try:
            for name, spec in self.commands.items():
                if self._active(name) is not None:
                    continue
                states = [facts[component]["state"] for component in spec["provides"]]
                if all(state == "READY" for state in states):
                    owners = {facts[component]["owner"] for component in spec["provides"]}
                    if len(owners) != 1:
                        raise ContractError("OPERATOR_STACK_AMBIGUOUS", name)
                    continue
                if not all(state == "MISSING" for state in states):
                    raise ContractError("OPERATOR_STACK_PARTIAL_OWNER", name)
                self._start(name)
                started.append(name)

            uncovered = [
                component for component in COMPONENTS
                if facts[component]["state"] == "MISSING"
                and not any(
                    component in spec["provides"] and self._active(name) is not None
                    for name, spec in self.commands.items()
                )
            ]
            if uncovered:
                raise ContractError("OPERATOR_STACK_UNCONFIGURED_MISSING", ",".join(uncovered))
        except Exception as exc:
            failed = self._stop_names(started)
            if failed:
                raise ContractError("OPERATOR_STACK_ROLLBACK_FAILED", ",".join(failed)) from exc
            raise
        return self._snapshot(facts)

    def setup_gripper(self) -> dict[str, object]:
        """Run the injected setup-only callback after proving one motion owner."""
        if self.gripper_setup is None:
            raise ContractError("OPERATOR_STACK_GRIPPER_SETUP_UNAVAILABLE")
        facts = self._facts()
        if any(facts[name]["state"] == "AMBIGUOUS" for name in COMPONENTS):
            raise ContractError("OPERATOR_STACK_AMBIGUOUS")
        expected_states = ("READY", "READY", "SETUP_REQUIRED")
        observed_states = tuple(facts[name]["state"] for name in ("robot", "controller", "gripper"))
        owners = {facts[name]["owner"] for name in MOTION_COMPONENTS}
        if observed_states != expected_states or len(owners) != 1:
            raise ContractError("OPERATOR_STACK_GRIPPER_SETUP_GATE")
        owner = next(iter(owners))
        for name, record in self._children.items():
            code = self._measure(record)
            if record["unexpected_exit"] and not record["stop_requested"]:
                raise ContractError("OPERATOR_STACK_GRIPPER_SETUP_GATE", name)
            if (
                code is None
                and set(self.commands[name]["provides"]) & MOTION_COMPONENTS
                and self.commands[name]["owner"] != owner
            ):
                raise ContractError("OPERATOR_STACK_GRIPPER_SETUP_GATE", name)
        self.gripper_setup(copy.deepcopy(facts))
        after = self._facts()
        if any(
            after[name] != {"state": "READY", "owner": owner}
            for name in MOTION_COMPONENTS
        ):
            raise ContractError("OPERATOR_STACK_GRIPPER_SETUP_FAILED")
        return self._snapshot(after)

    def reconfigure(self, name: str, spec: Mapping[str, object] | None) -> None:
        """Replace one configured owner after boundedly stopping only its child."""
        if not _valid_id(name):
            raise ContractError("OPERATOR_STACK_COMMAND")
        commands: dict[str, Mapping[str, object]] = dict(self.commands)
        if spec is None:
            commands.pop(name, None)
        else:
            commands[name] = spec
        checked = self._commands(commands)
        record = self._children.get(name)
        if record is not None and not self._stop_record(record):
            raise ContractError("OPERATOR_STACK_STOP_FAILED", name)
        self._children.pop(name, None)
        self.commands = checked

    def stop(self) -> dict[str, object]:
        """Boundedly terminate, then kill, only handles created by this instance."""
        failed = self._stop_names(list(self._children))
        if failed:
            raise ContractError("OPERATOR_STACK_STOP_FAILED", ",".join(failed))
        return self._snapshot(self._last_facts)

    def status(self) -> dict[str, object]:
        """Measure child exits and discovery once; no background polling is used."""
        return self._snapshot(self._facts())

    def _snapshot(self, facts: dict[str, dict[str, object]] | None) -> dict[str, object]:
        children = {}
        for name, record in self._children.items():
            running = self._measure(record) is None
            spec = self.commands[name]
            children[name] = {
                "argv": list(spec["argv"]),
                "owner": spec["owner"],
                "provides": list(spec["provides"]),
                "running": running,
                "returncode": record["returncode"],
                "stop_requested": record["stop_requested"],
                "terminate_timed_out": record["terminate_timed_out"],
                "kill_used": record["kill_used"],
                "stop_failed": record["stop_failed"],
                "unexpected_exit": record["unexpected_exit"],
            }
        if any(value["unexpected_exit"] or value["stop_failed"] for value in children.values()):
            state = "FAILED"
        elif any(value["running"] for value in children.values()):
            state = "FOREGROUND_RUNNING"
        elif children and all(value["stop_requested"] for value in children.values()):
            state = "STOPPED"
        elif facts and all(value["state"] == "READY" for value in facts.values()):
            state = "ATTACHED"
        else:
            state = "INCOMPLETE"
        return {
            "schema_version": "data_factory.operator_stack.v1",
            "state": state,
            "facts": copy.deepcopy(facts),
            "children": children,
        }
