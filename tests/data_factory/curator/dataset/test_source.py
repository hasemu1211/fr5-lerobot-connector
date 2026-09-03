from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.dataset.source import (
    require_local_file,
    validate_source_contract,
)
from tools.fr5_dataset_schema import dataset_features


class SourceTest(unittest.TestCase):
    def test_observable_30hz_up_wrist_contract(self):
        features = dataset_features(
            fps=30,
            height=480,
            width=640,
            cameras=("up", "wrist"),
            use_videos=True,
        )

        class Dataset:
            def __len__(self) -> int:
                return 1

        dataset = Dataset()
        dataset.meta = SimpleNamespace(
            features=features,
            fps=30,
            robot_type="fr5_ros2",
            total_episodes=1,
        )
        result = validate_source_contract(dataset, {"width": 640, "height": 480})
        self.assertEqual(
            set(result),
            {
                "observation.state",
                "action",
                "observation.images.up",
                "observation.images.wrist",
            },
        )
        dataset.meta.fps = 29
        with self.assertRaisesRegex(CuratorError, "SOURCE_DATASET_CONTRACT"):
            validate_source_contract(dataset, {"width": 640, "height": 480})

    def test_local_file_containment_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "inside.bin").write_bytes(b"x")
            require_local_file(root, Path("inside.bin"))
            with self.assertRaisesRegex(CuratorError, "SOURCE_LOCAL_PATH_ESCAPE"):
                require_local_file(root, Path("../outside.bin"))
            (root / "link.bin").symlink_to(root / "inside.bin")
            with self.assertRaisesRegex(CuratorError, "SOURCE_LOCAL_INCOMPLETE"):
                require_local_file(root, Path("link.bin"))


if __name__ == "__main__":
    unittest.main()
