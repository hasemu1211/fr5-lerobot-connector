from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from tools.data_factory.operator.setup.environment import OperatorEnvironment


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def report(
    robot="MISSING", controller="MISSING", gripper="MISSING", camera="MISSING",
    *, children=None, state="INCOMPLETE",
):
    owners = {
        "robot": "ros-control", "controller": "ros-control",
        "gripper": "ros-control", "camera": "camera-up",
    }
    return {
        "schema_version": "data_factory.operator_stack.v1",
        "state": state,
        "facts": {
            name: {
                "state": value,
                "owner": owners[name] if value in {"READY", "SETUP_REQUIRED"} else None,
            }
            for name, value in {
                "robot": robot, "controller": controller,
                "gripper": gripper, "camera": camera,
            }.items()
        },
        "children": copy.deepcopy(children or {}),
    }


class FakeStack:
    def __init__(self, *observations):
        self.observations = list(observations)
        self.status_calls = self.ensure_calls = self.setup_calls = self.stop_calls = 0

    def status(self):
        self.status_calls += 1
        value = self.observations.pop(0) if len(self.observations) > 1 else self.observations[0]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def ensure(self):
        self.ensure_calls += 1

    def setup_gripper(self):
        self.setup_calls += 1

    def stop(self):
        self.stop_calls += 1
        return copy.deepcopy(self.observations[-1])


class BoundedSettle:
    def __init__(self, checks):
        self.checks = checks
        self.calls = 0

    def __call__(self, check):
        self.calls += 1
        return any(check() for _ in range(self.checks))


class OperatorEnvironmentTests(unittest.TestCase):
    def environment(self, stack, checks=0):
        settle = BoundedSettle(checks)
        return OperatorEnvironment(
            stack, settle_policy=settle, clock=lambda: NOW,
        ), settle

    def test_already_ready_reuses_external_owners_without_actions(self):
        stack = FakeStack(report("READY", "READY", "READY", "READY", state="ATTACHED"))
        environment, settle = self.environment(stack)

        projected = environment.prepare_environment()

        self.assertEqual(projected["state"], "READY")
        self.assertEqual((stack.ensure_calls, stack.setup_calls, settle.calls), (0, 0, 0))

    def test_missing_stack_starts_once_then_requires_fresh_ready_observation(self):
        stack = FakeStack(
            report(), report("READY", "READY", "READY", "READY", state="FOREGROUND_RUNNING"),
        )
        environment, settle = self.environment(stack)

        projected = environment.prepare_environment()

        self.assertEqual(projected["state"], "READY")
        self.assertEqual((stack.ensure_calls, stack.setup_calls, stack.status_calls), (1, 0, 2))
        self.assertEqual(settle.calls, 0)

    def test_missing_motion_bootstraps_once_before_normal_stack_start(self):
        stack = FakeStack(
            report(), report(),
            report("READY", "READY", "READY", "READY", state="FOREGROUND_RUNNING"),
        )
        bootstrap_calls = []
        settle = BoundedSettle(0)
        environment = OperatorEnvironment(
            stack,
            settle_policy=settle,
            bootstrap_missing_motion=lambda: bootstrap_calls.append("gripper-open"),
            clock=lambda: NOW,
        )

        projected = environment.prepare_environment()

        self.assertEqual(projected["state"], "READY")
        self.assertEqual(bootstrap_calls, ["gripper-open"])
        self.assertEqual((stack.ensure_calls, stack.setup_calls, stack.status_calls), (1, 0, 3))

    def test_setup_required_invokes_gripper_setup_once_and_rechecks(self):
        stack = FakeStack(
            report("READY", "READY", "SETUP_REQUIRED", "READY"),
            report("READY", "READY", "READY", "READY", state="ATTACHED"),
        )
        environment, _settle = self.environment(stack)

        projected = environment.prepare_environment()

        self.assertEqual(projected["state"], "READY")
        self.assertEqual((stack.ensure_calls, stack.setup_calls, stack.status_calls), (0, 1, 2))

    def test_ambiguity_query_failure_timeout_and_child_exit_block_exactly(self):
        crashed = {
            "camera_up": {
                "unexpected_exit": True, "stop_failed": False,
                "running": False, "returncode": 23, "stop_requested": False,
                "provides": ["camera"],
            },
        }
        cases = (
            (FakeStack(report("READY", "READY", "READY", "AMBIGUOUS")), 0,
             "OPERATOR_STACK_AMBIGUOUS"),
            (FakeStack(RuntimeError("graph unavailable")), 0,
             "OPERATOR_ENVIRONMENT_QUERY_FAILED"),
            (FakeStack(report()), 2, "OPERATOR_ENVIRONMENT_SETTLE_TIMEOUT"),
            (FakeStack(report(children=crashed, state="FAILED")), 0,
             "OPERATOR_STACK_CHILD_EXITED"),
        )
        for stack, checks, reason in cases:
            with self.subTest(reason=reason):
                environment, _settle = self.environment(stack, checks)
                projected = environment.prepare_environment()
                self.assertEqual(projected["state"], "BLOCKED")
                self.assertEqual(
                    {item["reason"] for item in projected["components"].values()},
                    {reason},
                )

    def test_projection_is_factual_application_schema_without_authority(self):
        stack = FakeStack(report("READY", "READY", "SETUP_REQUIRED", "READY"))
        environment, _settle = self.environment(stack)

        projected = environment.projection()

        self.assertEqual(
            set(projected), {"schema_version", "state", "observed_at", "components"},
        )
        self.assertEqual(projected["schema_version"], "data_factory.operator_environment.v1")
        self.assertEqual(projected["observed_at"], "2026-08-26T12:00:00Z")
        self.assertEqual(set(projected["components"]), {"robot", "controller", "gripper", "camera"})
        self.assertEqual(
            projected["components"]["gripper"],
            {"state": "SETUP_REQUIRED", "owner": "ros-control", "reason": "SETUP_REQUIRED"},
        )
        self.assertTrue({"human", "training", "motion", "authority"}.isdisjoint(projected))

    def test_stop_delegates_owned_child_shutdown_to_stack_only(self):
        stack = FakeStack(report())
        environment, _settle = self.environment(stack)

        environment.stop()

        self.assertEqual((stack.stop_calls, stack.ensure_calls, stack.setup_calls), (1, 0, 0))


if __name__ == "__main__":
    unittest.main()
