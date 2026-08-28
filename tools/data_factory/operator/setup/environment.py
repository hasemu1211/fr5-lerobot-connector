"""Factual environment projection and explicit foreground-stack preparation."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from tools.data_factory.operator.setup.processes import (
    COMPONENTS,
    FACT_STATES,
    MOTION_COMPONENTS,
)
from tools.fr5_data_factory import ContractError


SCHEMA_VERSION = "data_factory.operator_environment.v1"
QUERY_FAILED = "OPERATOR_ENVIRONMENT_QUERY_FAILED"
SETTLE_TIMEOUT = "OPERATOR_ENVIRONMENT_SETTLE_TIMEOUT"
_COMPONENT_REASONS = {
    "READY": "ATTACHED",
    "MISSING": "NOT_RUNNING",
    "SETUP_REQUIRED": "SETUP_REQUIRED",
    "AMBIGUOUS": "OPERATOR_STACK_AMBIGUOUS",
}


class OperatorEnvironment:
    """Adapt one OperatorStack to CollectionOperatorApplication callbacks."""

    def __init__(
        self, stack: object, *,
        settle_policy: Callable[[Callable[[], bool]], bool],
        bootstrap_missing_motion: Callable[[], object] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if any(not callable(getattr(stack, name, None)) for name in (
            "status", "ensure", "setup_gripper", "stop",
        )) or not callable(settle_policy) or (
            bootstrap_missing_motion is not None
            and not callable(bootstrap_missing_motion)
        ) or (clock is not None and not callable(clock)):
            raise ContractError("OPERATOR_ENVIRONMENT_INPUT")
        self.stack = stack
        self.settle_policy = settle_policy
        self.bootstrap_missing_motion = bootstrap_missing_motion
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _observed_at(self) -> str:
        value = self.clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ContractError("OPERATOR_ENVIRONMENT_CLOCK")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _exception_reason(exc: Exception, fallback: str) -> str:
        return exc.code if isinstance(exc, ContractError) else fallback

    def _blocked(
        self, reason: str, view: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = view.get("components", {}) if isinstance(view, Mapping) else {}
        components = {
            name: {
                "state": source.get(name, {}).get("state", "BLOCKED"),
                "owner": source.get(name, {}).get("owner"),
                "reason": reason,
            }
            for name in COMPONENTS
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "state": "BLOCKED",
            "observed_at": self._observed_at(),
            "components": components,
        }

    @staticmethod
    def _facts(snapshot: object) -> tuple[dict[str, dict[str, object]], Mapping[str, Any]]:
        if not isinstance(snapshot, Mapping):
            raise ContractError("OPERATOR_ENVIRONMENT_STACK_REPORT")
        facts, children = snapshot.get("facts"), snapshot.get("children")
        if not isinstance(facts, Mapping) or set(facts) != set(COMPONENTS) or not isinstance(children, Mapping):
            raise ContractError("OPERATOR_ENVIRONMENT_STACK_REPORT")
        result = {}
        for name in COMPONENTS:
            fact = facts[name]
            if not isinstance(fact, Mapping) or set(fact) != {"state", "owner"}:
                raise ContractError("OPERATOR_ENVIRONMENT_STACK_REPORT")
            state, owner = fact["state"], fact["owner"]
            if state not in FACT_STATES or (owner is not None and not isinstance(owner, str)):
                raise ContractError("OPERATOR_ENVIRONMENT_STACK_REPORT")
            result[name] = {"state": state, "owner": owner}
        return result, children

    @staticmethod
    def _blocker(
        snapshot: Mapping[str, Any], facts: Mapping[str, Mapping[str, object]],
        children: Mapping[str, Any],
    ) -> str | None:
        records = [record for record in children.values() if isinstance(record, Mapping)]
        if any(record.get("unexpected_exit") is True for record in records):
            return "OPERATOR_STACK_CHILD_EXITED"
        if any(record.get("stop_failed") is True for record in records):
            return "OPERATOR_STACK_STOP_FAILED"
        if any(
            record.get("running") is True and record.get("stop_requested") is True
            for record in records
        ):
            return "OPERATOR_STACK_STOP_INCOMPLETE"
        if any(fact["state"] == "AMBIGUOUS" for fact in facts.values()):
            return "OPERATOR_STACK_AMBIGUOUS"

        motion_owners = {
            facts[name]["owner"] for name in MOTION_COMPONENTS
            if facts[name]["state"] in {"READY", "SETUP_REQUIRED"}
        }
        if len(motion_owners) > 1:
            return "OPERATOR_STACK_AMBIGUOUS"
        motion_states = tuple(facts[name]["state"] for name in (
            "robot", "controller", "gripper",
        ))
        if "SETUP_REQUIRED" in motion_states and (
            motion_states != ("READY", "READY", "SETUP_REQUIRED")
            or len(motion_owners) != 1
        ):
            return "OPERATOR_STACK_GRIPPER_SETUP_GATE"
        if "READY" in motion_states and "MISSING" in motion_states:
            motion_child_running = any(
                record.get("running") is True
                and bool(set(record.get("provides", ())) & MOTION_COMPONENTS)
                for record in records
            )
            if not motion_child_running:
                return "OPERATOR_STACK_PARTIAL_OWNER"
        if snapshot.get("state") == "FAILED":
            return "OPERATOR_ENVIRONMENT_STACK_FAILED"
        return None

    def _project(self, snapshot: object) -> dict[str, Any]:
        facts, children = self._facts(snapshot)
        blocker = self._blocker(snapshot, facts, children)
        components = {
            name: {
                **facts[name],
                "reason": blocker or _COMPONENT_REASONS[facts[name]["state"]],
            }
            for name in COMPONENTS
        }
        state = (
            "BLOCKED" if blocker
            else "READY" if all(fact["state"] == "READY" for fact in facts.values())
            else "SETUP_REQUIRED"
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "state": state,
            "observed_at": self._observed_at(),
            "components": components,
        }

    def projection(self) -> dict[str, Any]:
        """Return one fresh fact query; unreadable state is never called missing."""
        try:
            return self._project(self.stack.status())
        except Exception as exc:
            return self._blocked(self._exception_reason(exc, QUERY_FAILED))

    @staticmethod
    def _needs_gripper_setup(view: Mapping[str, Any]) -> bool:
        return view["components"]["gripper"]["state"] == "SETUP_REQUIRED"

    @staticmethod
    def _has_missing(view: Mapping[str, Any]) -> bool:
        return any(item["state"] == "MISSING" for item in view["components"].values())

    @staticmethod
    def _motion_missing(view: Mapping[str, Any]) -> bool:
        return all(
            view["components"][name]["state"] == "MISSING"
            for name in MOTION_COMPONENTS
        )

    def prepare_environment(self) -> dict[str, Any]:
        """Perform the only setup action, then accept only a fresh all-READY query.

        ``settle_policy`` owns all waiting and must bound calls to its predicate.
        """
        current = self.projection()
        bootstrap_done = setup_done = ensure_done = False

        def advance(view: dict[str, Any]) -> dict[str, Any]:
            nonlocal bootstrap_done, setup_done, ensure_done
            while view["state"] == "SETUP_REQUIRED":
                if (
                    self.bootstrap_missing_motion is not None
                    and self._motion_missing(view)
                    and not bootstrap_done
                ):
                    bootstrap_done = True
                    try:
                        self.bootstrap_missing_motion()
                    except Exception as exc:
                        return self._blocked(
                            self._exception_reason(
                                exc, "OPERATOR_ENVIRONMENT_BOOTSTRAP_FAILED",
                            ),
                            view,
                        )
                    view = self.projection()
                    continue
                if self._needs_gripper_setup(view):
                    if setup_done:
                        return self._blocked("OPERATOR_STACK_GRIPPER_SETUP_FAILED", view)
                    setup_done = True
                    try:
                        self.stack.setup_gripper()
                    except Exception as exc:
                        return self._blocked(
                            self._exception_reason(exc, "OPERATOR_ENVIRONMENT_PREPARE_FAILED"),
                            view,
                        )
                    view = self.projection()
                    continue
                if self._has_missing(view) and not ensure_done:
                    ensure_done = True
                    try:
                        self.stack.ensure()
                    except Exception as exc:
                        return self._blocked(
                            self._exception_reason(exc, "OPERATOR_ENVIRONMENT_PREPARE_FAILED"),
                            view,
                        )
                    view = self.projection()
                    continue
                break
            return view

        if current["state"] != "SETUP_REQUIRED":
            return current
        current = advance(current)
        if current["state"] != "SETUP_REQUIRED":
            return current

        def settled() -> bool:
            nonlocal current
            current = advance(self.projection())
            return current["state"] != "SETUP_REQUIRED"

        try:
            result = self.settle_policy(settled)
        except TimeoutError:
            return self._blocked(SETTLE_TIMEOUT, current)
        except Exception:
            return self._blocked("OPERATOR_ENVIRONMENT_SETTLE_FAILED", current)
        if type(result) is not bool:
            return self._blocked("OPERATOR_ENVIRONMENT_SETTLE_POLICY", current)
        if result and current["state"] in {"READY", "BLOCKED"}:
            return current
        return self._blocked(SETTLE_TIMEOUT, current)

    def stop(self) -> dict[str, Any]:
        """Delegate shutdown so only OperatorStack-owned children can be closed."""
        try:
            self.stack.stop()
            return self._project(self.stack.status())
        except Exception as exc:
            return self._blocked(
                self._exception_reason(exc, "OPERATOR_ENVIRONMENT_STOP_FAILED"),
            )


__all__ = ["OperatorEnvironment"]
