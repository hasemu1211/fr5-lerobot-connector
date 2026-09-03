from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tests.data_factory.curator.support import make_profile_fixture, write_json
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.profile.schema import (
    load_review_policy,
    load_view_profile,
)


class SchemaTest(unittest.TestCase):
    def test_profile_and_policy_exact_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = make_profile_fixture(Path(directory), width=200, height=120)
            self.assertEqual(
                load_view_profile(fixture.profile_path).value["profile_id"],
                "synthetic-up-view-r001",
            )
            self.assertEqual(load_review_policy(fixture.policy_path)["render_fps"], 10)
            policy = json.loads(fixture.policy_path.read_text(encoding="utf-8"))
            policy["render_fps"] = True
            write_json(fixture.policy_path, policy)
            with self.assertRaisesRegex(CuratorError, "REVIEW_POLICY_CONTRACT"):
                load_review_policy(fixture.policy_path)

    def test_unknown_field_and_asset_digest_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = make_profile_fixture(Path(directory), width=200, height=120)
            profile = json.loads(fixture.profile_path.read_text(encoding="utf-8"))
            profile["unknown"] = True
            write_json(fixture.profile_path, profile)
            with self.assertRaisesRegex(CuratorError, "VIEW_PROFILE_FIELDS"):
                load_view_profile(fixture.profile_path)
            del profile["unknown"]
            profile["mask_sha256"] = "sha256:" + "0" * 64
            write_json(fixture.profile_path, profile)
            with self.assertRaisesRegex(CuratorError, "VIEW_PROFILE_MASK_DIGEST"):
                load_view_profile(fixture.profile_path)


if __name__ == "__main__":
    unittest.main()
