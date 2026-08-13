#!/usr/bin/env python3

import unittest
from types import SimpleNamespace

from tools.fr5_training_profile import build_profile


def metadata(*cameras: str, action_dim: int = 7):
    features = {
        "action": {"dtype": "float32", "shape": [action_dim], "names": None},
        "observation.state": {"dtype": "float32", "shape": [7], "names": None},
    }
    for camera in cameras:
        features[f"observation.images.{camera}"] = {
            "dtype": "video",
            "shape": [480, 640, 3],
            "names": ["height", "width", "channels"],
        }
    return SimpleNamespace(
        features=features,
        camera_keys=[f"observation.images.{camera}" for camera in cameras],
    )


class TrainingProfileTest(unittest.TestCase):
    def test_smolvla_and_act_keep_fr5_features(self):
        smolvla = "\n".join(build_profile("smolvla", metadata("up", "side")))
        self.assertIn('"shape":[7]', smolvla)
        self.assertIn('"observation.images.up":"observation.images.camera1"', smolvla)
        self.assertIn('"observation.images.side":"observation.images.camera2"', smolvla)

        act = "\n".join(build_profile("act", metadata("up", "side")))
        self.assertIn("--policy.type=act", act)
        self.assertIn("observation.images.up", act)
        self.assertIn("observation.images.side", act)

    def test_vqbet_requires_the_selected_single_camera(self):
        vqbet = "\n".join(build_profile("vqbet-side", metadata("up", "side")))
        self.assertIn("--policy.type=vqbet", vqbet)
        self.assertIn("observation.images.side", vqbet)
        self.assertNotIn("observation.images.up", vqbet)
        with self.assertRaises(ValueError):
            build_profile("vqbet-wrist", metadata("up", "side"))

    def test_profiles_reject_non_fr5_action_dimensions(self):
        with self.assertRaises(ValueError):
            build_profile("act", metadata("up", action_dim=6))


if __name__ == "__main__":
    unittest.main()
