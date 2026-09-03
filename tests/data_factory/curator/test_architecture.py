from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path
import unittest


class ArchitectureTest(unittest.TestCase):
    @property
    def product_root(self) -> Path:
        return Path(__file__).parents[3] / "tools/data_factory/curator"

    def test_old_flat_modules_are_absent_and_root_api_is_narrow(self):
        for name in (
            "approval",
            "contracts",
            "derive",
            "geometry",
            "up_view",
            "verify",
        ):
            self.assertIsNone(
                importlib.util.find_spec(f"tools.data_factory.curator.{name}")
            )
        import tools.data_factory.curator as curator

        self.assertEqual(curator.__all__, ["CuratorError", "apply_up_view"])
        for package in ("core", "profile", "dataset", "review", "workflow"):
            module = importlib.import_module(f"tools.data_factory.curator.{package}")
            self.assertFalse(hasattr(module, "__all__"))

    def test_expected_responsibility_packages_and_owners_exist(self):
        self.assertEqual(
            {
                path.name
                for path in self.product_root.iterdir()
                if path.is_dir() and not path.name.startswith("__")
            },
            {"core", "profile", "dataset", "review", "workflow"},
        )
        owners = {
            "core.errors": "CuratorError",
            "core.jsonio": "load_json",
            "core.identity": "stable_tree_identity",
            "core.filesystem": "OwnedDirectory",
            "profile.schema": "load_view_profile",
            "profile.registry": "resolve_view_profile",
            "profile.geometry": "resolve_geometry",
            "profile.transform": "apply_up_view",
            "dataset.source": "open_source_dataset",
            "dataset.quality": "image_metrics",
            "dataset.lineage": "verify_candidate_lineage",
            "dataset.verify": "verify_derived_dataset",
            "dataset.materialize": "materialize_candidate",
            "dataset.publish": "publish_candidate",
            "review.sampling": "sample_frames",
            "review.render": "render_review_mp4",
            "review.manifest": "verify_manifest",
            "review.decision": "read_foreground_decision",
            "workflow.state": "load_events",
            "workflow.application": "prepare",
        }
        for module_name, symbol in owners.items():
            module = f"tools.data_factory.curator.{module_name}"
            self.assertEqual(
                getattr(importlib.import_module(module), symbol).__module__, module
            )

    def test_imports_follow_core_profile_dataset_review_workflow_cli_direction(self):
        ranks = {"core": 0, "profile": 1, "dataset": 2, "review": 3, "workflow": 4}
        prefix = "tools.data_factory.curator."
        for path in self.product_root.rglob("*.py"):
            relative = path.relative_to(self.product_root)
            if len(relative.parts) < 2 or relative.parts[0] not in ranks:
                continue
            owner_rank = ranks[relative.parts[0]]
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.level:
                        module_name = ".".join(relative.parts[:-1])
                        absolute = importlib.util.resolve_name(
                            "." * node.level + node.module,
                            f"tools.data_factory.curator.{module_name}",
                        )
                        names = [absolute]
                    else:
                        names = [node.module]
                for name in names:
                    if name.startswith(prefix):
                        imported = name[len(prefix) :].split(".", 1)[0]
                        if imported in ranks:
                            self.assertLessEqual(
                                ranks[imported], owner_rank, f"{relative}: {name}"
                            )

    def test_only_materialize_constructs_a_lerobot_dataset_writer(self):
        owners = []
        for path in self.product_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "LeRobotDataset.create(" in text:
                owners.append(path.relative_to(self.product_root).as_posix())
        self.assertEqual(owners, ["dataset/materialize.py"])


if __name__ == "__main__":
    unittest.main()
