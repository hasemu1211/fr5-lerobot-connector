import importlib.util, pathlib, unittest

class ArchitectureTest(unittest.TestCase):
    def test_old_flat_modules_are_absent_and_root_is_narrow(self):
        for name in ("approval","contracts","derive","geometry","up_view","verify"):
            self.assertIsNone(importlib.util.find_spec(f"tools.data_factory.curator.{name}"))
        import tools.data_factory.curator as curator
        self.assertEqual(curator.__all__, ["CuratorError","apply_up_view"])
    def test_expected_packages_exist(self):
        root=pathlib.Path(__file__).parents[3]/"tools/data_factory/curator"
        self.assertEqual({p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("__")}, {"core","profile","dataset","review","workflow"})

if __name__ == "__main__": unittest.main()
