"""Exercise the installed native queue with synthetic model output, no model load."""

from collections import deque
from types import SimpleNamespace
import unittest

import torch
from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy
from lerobot_strategy_fr5.chunk_tap import ExactSmolVLAChunkTap, _tensor_sha256


class NativeQueueFixture:
    name = "smolvla"
    select_action = SmolVLAPolicy.select_action
    _check_get_actions_condition = SmolVLAPolicy._check_get_actions_condition

    def __init__(self, consumed):
        self.config = SimpleNamespace(chunk_size=50, n_action_steps=consumed)
        self._queues = {"action": deque(maxlen=consumed)}
        self.raw = torch.arange(350, dtype=torch.float32).reshape(1, 50, 7)
        self.calls = 0

    def _rtc_enabled(self):
        return False

    def eval(self):
        return self

    def _prepare_batch(self, batch):
        return batch

    def _get_action_chunk(self, batch, noise=None):
        self.calls += 1
        return self.raw


class ChunkTapTest(unittest.TestCase):
    def attach(self, policy, callback):
        tap = ExactSmolVLAChunkTap(policy=policy, on_chunk=callback)
        tap.install()
        self.addCleanup(tap.remove)
        return tap

    def test_native_queue_owns_consumption_and_reinference(self):
        for consumed in (1, 5, 10, 50):
            with self.subTest(consumed=consumed):
                policy = NativeQueueFixture(consumed)
                records = []
                self.attach(policy, records.append)
                for index in range(consumed):
                    self.assertTrue(torch.equal(policy.select_action({}), policy.raw[:, index]))
                self.assertEqual(policy.calls, 1)
                self.assertEqual(len(records), 1)
                self.assertEqual(tuple(records[0].raw.shape), (1, 50, 7))
                self.assertEqual(records[0].raw_sha256, _tensor_sha256(policy.raw))
                self.assertTrue(torch.equal(policy.select_action({}), policy.raw[:, 0]))
                self.assertEqual(policy.calls, 2)
                self.assertEqual([item.sequence for item in records], [1, 2])

    def test_returns_original_and_retains_independent_full_tensor(self):
        policy = NativeQueueFixture(5)
        records = []
        self.attach(policy, records.append)
        returned = policy._get_action_chunk({})
        self.assertIs(returned, policy.raw)
        self.assertNotEqual(records[0].raw.data_ptr(), returned.data_ptr())
        records[0].raw.fill_(-1)
        self.assertTrue(torch.equal(returned.flatten(), torch.arange(350, dtype=torch.float32)))

    def test_rejects_wrong_prediction_length_even_if_it_matches_consumption(self):
        policy = NativeQueueFixture(5)
        policy.raw = policy.raw[:, :5]
        records = []
        self.attach(policy, records.append)
        with self.assertRaisesRegex(RuntimeError, "FR5_CHUNK_LENGTH: 5 != 50"):
            policy.select_action({})
        self.assertEqual(records, [])
        self.assertEqual(len(policy._queues["action"]), 0)

    def test_rejects_wrong_shape_before_native_queue_consumption(self):
        for shape in ((50, 7), (2, 50, 7), (1, 50, 6)):
            with self.subTest(shape=shape):
                policy = NativeQueueFixture(5)
                policy.raw = torch.zeros(shape)
                records = []
                self.attach(policy, records.append)
                with self.assertRaisesRegex(RuntimeError, "FR5_CHUNK_SHAPE"):
                    policy.select_action({})
                self.assertEqual(records, [])
                self.assertEqual(len(policy._queues["action"]), 0)

    def test_removal_restores_native_method_and_leaves_queue_alone(self):
        policy = NativeQueueFixture(5)
        original = policy._get_action_chunk
        records = []
        tap = self.attach(policy, records.append)
        policy.select_action({})
        tap.remove()
        self.assertEqual(policy._get_action_chunk, original)
        self.assertEqual(len(policy._queues["action"]), 4)
        for _ in range(5):
            policy.select_action({})
        self.assertEqual(policy.calls, 2)
        self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
