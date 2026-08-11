#!/usr/bin/env python3

import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from tools.evaluate_smolvla_offline import normalize_checkpoint_path, parse_episode_indices, select_eval_episodes


class OfflineEvaluationTest(unittest.TestCase):
    def test_episode_selection_is_explicit_or_task_stratified(self):
        self.assertEqual(parse_episode_indices("3,1,3"), [1, 3])
        with self.assertRaises(ValueError):
            parse_episode_indices("1,-2")

        tasks = [["pick"], ["pick"], ["pick"], ["place"], ["place"]]
        self.assertEqual(select_eval_episodes(tasks, 0.34), [1, 2, 4])
        with self.assertRaises(ValueError):
            select_eval_episodes([["only"]], 0.2)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pretrained_model").mkdir()
            (root / "pretrained_model/config.json").write_text("{}")
            self.assertEqual(normalize_checkpoint_path(str(root)), str(root / "pretrained_model"))


if __name__ == "__main__":
    unittest.main()
