from __future__ import annotations

import unittest

import numpy as np

from tools.curator.contracts import CuratorError
from tools.curator.up_view import apply_up_view, make_background_plate


class UpViewTest(unittest.TestCase):
    def test_composite_is_deterministic_and_up_only(self):
        raw = np.arange(4 * 5 * 3, dtype=np.uint8).reshape(4, 5, 3)
        plate = np.full_like(raw, 201)
        keep = np.zeros((4, 5), dtype=bool)
        keep[1:3, 2:4] = True
        first = apply_up_view(raw, keep, plate)
        second = apply_up_view(raw, keep, plate)
        self.assertTrue(np.array_equal(first, second))
        self.assertTrue(np.array_equal(first[keep], raw[keep]))
        self.assertTrue(np.array_equal(first[~keep], plate[~keep]))
        self.assertEqual(apply_up_view.__code__.co_argcount, 3)
        self.assertNotIn("wrist", apply_up_view.__code__.co_varnames)

    def test_temporal_median_and_contract_fail_closed(self):
        low = np.zeros((2, 3, 3), dtype=np.uint8)
        high = np.full_like(low, 100)
        self.assertTrue(np.array_equal(make_background_plate([low, high]), np.full_like(low, 50)))
        with self.assertRaisesRegex(CuratorError, "UP_VIEW_INPUT"):
            apply_up_view(low, np.ones((2, 3), dtype=np.uint8), high)
        with self.assertRaisesRegex(CuratorError, "PLATE_FRAME_CONTRACT"):
            make_background_plate([low, high[:, :2]])


if __name__ == "__main__":
    unittest.main()
