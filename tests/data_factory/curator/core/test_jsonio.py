from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.core.jsonio import canonical_digest, load_json


class JsonIoTest(unittest.TestCase):
    def test_canonical_digest_is_order_independent_and_strict(self):
        self.assertEqual(
            canonical_digest({"b": 2, "a": 1}), canonical_digest({"a": 1, "b": 2})
        )
        with self.assertRaisesRegex(CuratorError, "JSON_NONFINITE"):
            canonical_digest({"value": float("nan")})

    def test_duplicate_nonfinite_and_symlink_json_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.json"
            source.write_text('{"a":1,"a":2}', encoding="utf-8")
            with self.assertRaisesRegex(CuratorError, "JSON_DUPLICATE_KEY"):
                load_json(source, code="TEST_JSON")
            source.write_text('{"a":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(CuratorError, "JSON_NONFINITE"):
                load_json(source, code="TEST_JSON")
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            source.unlink()
            source.symlink_to(target)
            with self.assertRaisesRegex(CuratorError, "TEST_JSON"):
                load_json(source, code="TEST_JSON")


if __name__ == "__main__":
    unittest.main()
