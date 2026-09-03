from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from tests.data_factory.curator.support import make_profile_fixture
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.profile.registry import (
    load_profile_assets,
    resolve_view_profile,
)


class RegistryTest(unittest.TestCase):
    def test_canonical_verified_profile_resolves_exact_geometry_and_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = make_profile_fixture(Path(directory), width=200, height=120)
            resolved = resolve_view_profile(
                fixture.paths.profile_root,
                binding_root=fixture.paths.binding_root,
                collection_profile_root=fixture.paths.collection_profile_root,
            )
            mask, plate = load_profile_assets(resolved)
            self.assertEqual(mask.shape, (120, 200))
            self.assertEqual(plate.shape, (120, 200, 3))
            self.assertEqual(resolved.binding["physical_binding_status"], "VERIFIED")

    def test_noncanonical_binding_and_ambiguous_default_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = make_profile_fixture(Path(directory), width=200, height=120)
            other_bindings = Path(directory) / "other-bindings"
            other_bindings.mkdir()
            with self.assertRaisesRegex(CuratorError, "VERIFIED_BINDING_NOT_CANONICAL"):
                resolve_view_profile(
                    fixture.paths.profile_root,
                    binding_root=other_bindings,
                    collection_profile_root=fixture.paths.collection_profile_root,
                )
            shutil.copyfile(
                fixture.profile_path, fixture.paths.profile_root / "second.json"
            )
            with self.assertRaisesRegex(CuratorError, "VIEW_PROFILE_RESOLUTION"):
                resolve_view_profile(
                    fixture.paths.profile_root,
                    binding_root=fixture.paths.binding_root,
                    collection_profile_root=fixture.paths.collection_profile_root,
                )


if __name__ == "__main__":
    unittest.main()
