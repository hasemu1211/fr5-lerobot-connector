from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.dataset.lineage import (
    copy_source_provenance,
    verify_candidate_lineage,
    write_candidate_lineage,
)


class LineageTest(unittest.TestCase):
    def test_lineage_binds_source_mapping_transform_and_byte_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "candidate"
            (source / "meta/source_provenance").mkdir(parents=True)
            (output / "meta").mkdir(parents=True)
            for episode in range(2):
                (
                    source / "meta/source_provenance" / f"episode-{episode:06d}.jsonl"
                ).write_text(
                    f'{{"episode":{episode}}}\n',
                    encoding="utf-8",
                )
            profile = {
                "profile_digest": "sha256:" + "1" * 64,
                "mask_sha256": "sha256:" + "2" * 64,
                "background_plate_sha256": "sha256:" + "3" * 64,
            }
            provenance = copy_source_provenance(source, output, 2)
            reference = write_candidate_lineage(
                output,
                source=source,
                source_repo_id="local/source",
                source_digest="sha256:" + "4" * 64,
                candidate_repo_id="local/candidate",
                profile=profile,
                verification={"episodes": 2, "frames": 4},
                source_provenance=provenance,
            )
            lineage = verify_candidate_lineage(
                output,
                reference,
                source=source,
                source_repo_id="local/source",
                source_digest="sha256:" + "4" * 64,
                candidate_repo_id="local/candidate",
                profile=profile,
                episodes=2,
                frames=4,
            )
            self.assertEqual(
                lineage["episode_mapping"]["contract"],
                "IDENTICAL_EPISODE_FRAME_INDEX",
            )
            copied = output / "meta/source_provenance/episode-000001.jsonl"
            copied.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(CuratorError, "SOURCE_PROVENANCE_COPY"):
                verify_candidate_lineage(
                    output,
                    reference,
                    source=source,
                    source_repo_id="local/source",
                    source_digest="sha256:" + "4" * 64,
                    candidate_repo_id="local/candidate",
                    profile=profile,
                    episodes=2,
                    frames=4,
                )


if __name__ == "__main__":
    unittest.main()
