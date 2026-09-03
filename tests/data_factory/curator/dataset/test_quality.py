from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from tools.data_factory.curator.dataset.quality import (
    accumulate_metrics,
    derived_image_quality_warnings,
    metric_accumulator,
    summarize_metrics,
    write_derived_quality,
)


class QualityTest(unittest.TestCase):
    def test_compact_accumulator_and_warning_diagnostics(self):
        accumulator = metric_accumulator()
        accumulate_metrics(accumulator, np.full((8, 8, 3), 127, dtype=np.uint8))
        summary = summarize_metrics(accumulator)
        warnings = derived_image_quality_warnings({"up": summary, "wrist": summary})
        self.assertEqual(accumulator["count"], 1)
        self.assertTrue(any("monochrome" in item for item in warnings))

    def test_quality_lineage_recomputes_pixels_without_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source/meta"
            output = root / "output/meta"
            source.mkdir(parents=True)
            output.mkdir(parents=True)
            row = {
                "episode_index": 0,
                "cameras": {"up": {}, "wrist": {}},
                "image_quality_warnings": ["stale"],
            }
            (source / "recording_quality.jsonl").write_text(
                json.dumps(row) + "\n", encoding="utf-8"
            )
            cameras = {
                key: {
                    "color_delta_mean": 10.0,
                    "brightness_mean": 100.0,
                    "clipping_mean": 0.0,
                    "sharpness_median": 100.0,
                }
                for key in ("up", "wrist")
            }
            result = write_derived_quality(
                root / "source",
                root / "output",
                {
                    "episodes": 1,
                    "derived_image_metrics": [{"episode_index": 0, "cameras": cameras}],
                },
                "sha256:" + "1" * 64,
            )
            derived = json.loads((output / "recording_quality.jsonl").read_text())
            self.assertEqual(result["derived_pixel_metrics"], "RECOMPUTED")
            self.assertEqual(
                derived["curator_lineage"]["source_timing_evidence"], "PRESERVED"
            )
            self.assertNotIn("stale", derived["image_quality_warnings"])


if __name__ == "__main__":
    unittest.main()
