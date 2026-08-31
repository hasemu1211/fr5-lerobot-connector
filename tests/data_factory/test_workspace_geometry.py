import math
import unittest

from tools.data_factory.workspace_geometry import (
    progressive_farthest_order,
    rotate_xy,
    rotation_envelope,
    safe_rectangle_bounds,
    stratified_rectangle_samples,
)


class WorkspaceGeometryTests(unittest.TestCase):
    def test_rotation_round_trip_and_envelope(self):
        rotated = rotate_xy((12.5, -7.25), 37.5)
        restored = rotate_xy(rotated, -37.5)
        self.assertAlmostEqual(restored[0], 12.5)
        self.assertAlmostEqual(restored[1], -7.25)
        envelope = rotation_envelope((-120, 120), (-76.5, 76.5))
        radius = math.hypot(120, 76.5)
        self.assertEqual(envelope, ((-radius, radius), (-radius, radius)))

    def test_safe_bounds_include_object_footprint_and_uncertainty(self):
        yaw0 = safe_rectangle_bounds(
            page_size_mm=(297, 210), origin_xy_mm=(148.5, 105),
            base_margin_xy_mm=(15, 20), object_size_xy_mm=(25, 25),
            uncertainty_mm=16, yaw_deg=0,
        )
        yaw45 = safe_rectangle_bounds(
            page_size_mm=(297, 210), origin_xy_mm=(148.5, 105),
            base_margin_xy_mm=(15, 20), object_size_xy_mm=(25, 25),
            uncertainty_mm=16, yaw_deg=45,
        )
        self.assertEqual(yaw0, ((-120.0, 120.0), (-76.5, 76.5)))
        self.assertLess(yaw45[0][1], yaw0[0][1])
        self.assertLess(yaw45[1][1], yaw0[1][1])

    def test_five_by_three_samples_cover_every_stratum_and_spread_prefix(self):
        samples = stratified_rectangle_samples(
            x_bounds=(-120, 120), y_bounds=(-76.5, 76.5),
            columns=5, rows=3, start_xy=(0, 0), count=14,
            seed=0, skip_start_cell=True,
        )
        cells = {(row, column) for _x, _y, row, column in samples}
        self.assertEqual(len(cells), 14)
        self.assertNotIn((1, 2), cells)
        first = samples[:4]
        self.assertLess(min(item[0] for item in first), -60)
        self.assertGreater(max(item[0] for item in first), 60)
        self.assertLess(min(item[1] for item in first), -38.25)
        self.assertGreater(max(item[1] for item in first), 38.25)
        self.assertEqual(
            samples,
            stratified_rectangle_samples(
                x_bounds=(-120, 120), y_bounds=(-76.5, 76.5),
                columns=5, rows=3, start_xy=(0, 0), count=14,
                seed=0, skip_start_cell=True,
            ),
        )

    def test_invalid_geometry_fails_without_partial_samples(self):
        for call in (
            lambda: safe_rectangle_bounds(
                page_size_mm=(10, 10), origin_xy_mm=(5, 5),
                base_margin_xy_mm=(5, 5), object_size_xy_mm=(1, 1),
                uncertainty_mm=0, yaw_deg=0,
            ),
            lambda: rotation_envelope((1, -1), (-1, 1)),
            lambda: progressive_farthest_order(
                [(0, 0), (0, 0)], start_xy=(0, 0), seed=0,
            ),
            lambda: stratified_rectangle_samples(
                x_bounds=(-1, 1), y_bounds=(-1, 1), columns=1, rows=1,
                start_xy=(0, 0), count=1, seed=0, skip_start_cell=True,
            ),
        ):
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()


if __name__ == "__main__":
    unittest.main()
