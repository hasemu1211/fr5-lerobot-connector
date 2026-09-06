"""Pure fake checks for the learned-action command boundary."""

import copy
import unittest

from tools.data_factory.learned_action_adapter import (
    ACTIVE,
    FAULT,
    STOPPED,
    FakeCommandSink,
    LearnedActionAdapter,
    fake_observation,
)


class Clock:
    def __init__(self, value: float = 10.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class LearnedActionAdapterTest(unittest.TestCase):
    def test_valid_7d_dual_camera_observation_sends_one_fake_command(self):
        seen = []

        def policy(observation):
            seen.append(observation)
            return [0, 1, 2, 3, 4, 5, 6]

        clock = Clock()
        sink = FakeCommandSink()
        adapter = LearnedActionAdapter(policy, sink, clock=clock)
        self.assertEqual(adapter.start("goal-1"), ACTIVE)
        self.assertEqual(adapter.step(fake_observation(clock.value)), ACTIVE)
        self.assertEqual(set(seen[0]), {
            "observation.state", "observation.images.camera1", "observation.images.camera2"
        })
        self.assertEqual(sink.commands, [("learned-action-fake", tuple(float(i) for i in range(7)))])
        self.assertEqual(adapter.stop(), STOPPED)
        adapter.step(fake_observation(clock.value))
        self.assertEqual((len(sink.commands), adapter.policy_calls, adapter.active_goal_id), (1, 1, None))

    def test_pre_stop_is_terminal_before_policy_inference(self):
        calls = []
        sink = FakeCommandSink()
        adapter = LearnedActionAdapter(lambda value: calls.append(value), sink, clock=Clock())
        self.assertEqual(adapter.stop(), STOPPED)
        self.assertEqual(adapter.start("never-active"), STOPPED)
        self.assertEqual(adapter.step(fake_observation(10.0)), STOPPED)
        self.assertEqual((calls, sink.commands, sink.active_owner), ([], [], None))

    def test_stale_future_and_nonfinite_observation_fail_without_policy_or_command(self):
        cases = [
            fake_observation(9.0),
            fake_observation(11.0),
            fake_observation(float("nan")),
            fake_observation(10.0, state=[0, 1, 2, 3, 4, 5, float("inf")]),
        ]
        for observation in cases:
            sink = FakeCommandSink()
            adapter = LearnedActionAdapter(lambda value: [0] * 7, sink, clock=Clock())
            adapter.start("goal")
            self.assertEqual(adapter.step(observation), FAULT)
            self.assertEqual((adapter.policy_calls, sink.commands, sink.active_owner), (0, [], None))
            adapter.step(fake_observation(10.0))
            self.assertEqual(sink.commands, [])

    def test_observation_requires_exact_7d_state_and_two_fixed_rgb_cameras(self):
        valid = fake_observation(10.0)
        cases = []
        missing = copy.deepcopy(valid)
        missing.pop("observation.images.camera2")
        cases.append(missing)
        extra = copy.deepcopy(valid)
        extra["observation.images.camera3"] = extra["observation.images.camera1"]
        cases.append(extra)
        wrong_state = copy.deepcopy(valid)
        wrong_state["observation.state"] = [0] * 6
        cases.append(wrong_state)
        wrong_rgb = copy.deepcopy(valid)
        wrong_rgb["observation.images.camera1"]["color_space"] = "BGR"
        cases.append(wrong_rgb)
        wrong_bytes = copy.deepcopy(valid)
        wrong_bytes["observation.images.camera2"]["data"] = b"\x00"
        cases.append(wrong_bytes)
        for observation in cases:
            sink = FakeCommandSink()
            adapter = LearnedActionAdapter(lambda value: [0] * 7, sink, clock=Clock())
            adapter.start("goal")
            self.assertEqual(adapter.step(observation), FAULT)
            self.assertEqual((adapter.policy_calls, sink.commands), (0, []))

    def test_policy_exception_and_invalid_action_fail_closed(self):
        def raises(_value):
            raise RuntimeError("fake policy fault")

        policies = (
            raises,
            lambda _value: [0] * 6,
            lambda _value: [0] * 6 + [float("nan")],
            lambda _value: [0] * 7 + [1],
            lambda _value: "not-an-action",
        )
        for policy in policies:
            sink = FakeCommandSink()
            adapter = LearnedActionAdapter(policy, sink, clock=Clock())
            adapter.start("goal")
            self.assertEqual(adapter.step(fake_observation(10.0)), FAULT)
            self.assertEqual((sink.commands, sink.active_owner, adapter.active_goal_id), ([], None, None))
            adapter.step(fake_observation(10.0))
            self.assertEqual(sink.commands, [])

    def test_sink_fault_and_cancel_allow_no_later_commands(self):
        sink = FakeCommandSink(fail_send=True)
        adapter = LearnedActionAdapter(lambda _value: [0] * 7, sink, clock=Clock())
        adapter.start("goal")
        self.assertEqual(adapter.step(fake_observation(10.0)), FAULT)
        self.assertEqual(sink.commands, [])

        sink = FakeCommandSink()
        adapter = LearnedActionAdapter(lambda _value: [0] * 7, sink, clock=Clock())
        adapter.start("goal")
        self.assertEqual(adapter.cancel(), STOPPED)
        adapter.step(fake_observation(10.0))
        self.assertEqual((sink.commands, adapter.policy_calls), ([], 0))

    def test_reentrant_cancel_and_slow_policy_cannot_send_after_terminal(self):
        clock = Clock()
        sink = FakeCommandSink()
        adapter = None

        def cancels(_value):
            adapter.cancel()
            return [0] * 7

        adapter = LearnedActionAdapter(cancels, sink, clock=clock)
        adapter.start("goal")
        self.assertEqual(adapter.step(fake_observation(clock.value)), STOPPED)
        self.assertEqual(sink.commands, [])

        def too_slow(_value):
            clock.value += 2.0
            return [0] * 7

        clock.value = 20.0
        sink = FakeCommandSink()
        adapter = LearnedActionAdapter(too_slow, sink, clock=clock, watchdog_timeout_s=1.0)
        adapter.start("slow-goal")
        self.assertEqual(adapter.step(fake_observation(20.0)), FAULT)
        self.assertEqual((adapter.terminal_reason, sink.commands), ("WATCHDOG", []))

    def test_fresh_before_inference_but_stale_at_send_is_rejected(self):
        clock, sink = Clock(), FakeCommandSink()
        def policy(_):
            clock.value += .4
            return [0] * 7
        adapter = LearnedActionAdapter(policy, sink, clock=clock, max_observation_age_s=.3)
        adapter.start("goal")
        self.assertEqual(adapter.step(fake_observation(10.)), FAULT)
        self.assertEqual((adapter.terminal_reason, sink.commands), ("STALE_OBSERVATION", []))

    def test_recursive_step_never_sends_two_commands(self):
        clock, sink = Clock(), FakeCommandSink()
        adapter = None
        def policy(_):
            adapter.step(fake_observation(10.))
            return [0] * 7
        adapter = LearnedActionAdapter(policy, sink, clock=clock)
        adapter.start("goal")
        self.assertEqual(adapter.step(fake_observation(10.)), FAULT)
        self.assertEqual((adapter.terminal_reason, sink.commands, adapter.policy_calls), ("REENTRANT_STEP", [], 1))

    def test_watchdog_and_command_owner_prevent_competing_active_goal(self):
        clock = Clock()
        sink = FakeCommandSink()
        first = LearnedActionAdapter(lambda _value: [0] * 7, sink, clock=clock, owner_id="owner-a")
        second = LearnedActionAdapter(lambda _value: [0] * 7, sink, clock=clock, owner_id="owner-b")
        self.assertEqual(first.start("goal-a"), ACTIVE)
        self.assertEqual(second.start("goal-b"), FAULT)
        self.assertEqual((first.state, sink.active_owner, second.active_goal_id), (ACTIVE, "owner-a", None))
        self.assertEqual(first.start("competing-goal"), FAULT)
        self.assertIsNone(sink.active_owner)
        self.assertIsNone(first.active_goal_id)

        sink = FakeCommandSink()
        adapter = LearnedActionAdapter(lambda _value: [0] * 7, sink, clock=clock, watchdog_timeout_s=1.0)
        adapter.start("watchdog-goal")
        clock.value += 2.0
        self.assertEqual(adapter.check_watchdog(), FAULT)
        adapter.step(fake_observation(clock.value))
        self.assertEqual((adapter.terminal_reason, sink.commands, sink.active_owner), ("WATCHDOG", [], None))


if __name__ == "__main__":
    unittest.main()
