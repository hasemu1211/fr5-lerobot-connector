import math
import unittest

from tools.a4_place_yaw.region_layout import (
    make_red_blue_region_layout,
    workspace_region,
)
from tools.data_factory.workspace_geometry import (
    point_in_convex_polygon,
    polygon_bounds,
    progressive_farthest_order,
    rotate_xy,
    rotation_envelope,
    safe_convex_polygon,
    safe_rectangle_bounds,
    stratified_convex_polygon_samples,
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

    def test_workspace_regions_are_independent_and_eroded_before_sampling(self):
        layout = make_red_blue_region_layout()
        red = workspace_region(layout, "PLACE_A")
        blue = workspace_region(layout, "PLACE_B")
        self.assertEqual((red["region_id"], blue["region_id"]), ("RED", "BLUE"))
        self.assertEqual(
            red["polygon_local_xy_mm"], blue["polygon_local_xy_mm"],
        )
        safe = safe_convex_polygon(
            polygon=red["polygon_local_xy_mm"],
            object_size_xy_mm=(24, 24), uncertainty_mm=16, yaw_deg=0,
        )
        self.assertEqual(polygon_bounds(safe), ((-105.5, 105.5), (-57.0, 57.0)))
        samples = stratified_convex_polygon_samples(
            polygon=safe, columns=5, rows=3, start_xy=(0, 0),
            count=14, seed=0, skip_start_cell=True,
        )
        self.assertEqual(len({(row, column) for _x, _y, row, column in samples}), 14)
        self.assertTrue(all(
            point_in_convex_polygon((x, y), safe)
            for x, y, _row, _column in samples
        ))

    def test_convex_polygon_shape_can_change_without_changing_sampler(self):
        polygon = [(-100, -60), (100, -40), (80, 60), (-90, 70)]
        safe = safe_convex_polygon(
            polygon=polygon, object_size_xy_mm=(24, 24),
            uncertainty_mm=4, yaw_deg=30,
        )
        samples = stratified_convex_polygon_samples(
            polygon=safe, columns=5, rows=3, start_xy=(0, 0),
            count=15, seed=3,
        )
        self.assertGreaterEqual(len(samples), 10)
        self.assertTrue(all(
            point_in_convex_polygon((x, y), safe)
            for x, y, _row, _column in samples
        ))

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
