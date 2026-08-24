import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class FixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.states = json.loads((ROOT / "fixtures/states.json").read_text())
        cls.html = (ROOT / "index.html").read_text()
        cls.js = (ROOT / "app.js").read_text()
        cls.css = (ROOT / "styles.css").read_text()
        cls.makefile = (ROOT / "Makefile").read_text()

    def test_required_states_and_safe_next_actions(self):
        self.assertEqual(set(self.states), {"ready", "blocked", "approval", "running", "review", "recovery"})
        for name, state in self.states.items():
            self.assertIn("authority", state, name)
            self.assertIn("evidence", state, name)
        for name in ("ready", "blocked", "running", "review", "recovery"):
            self.assertIn("nextAction", self.states[name], name)

    def test_exact_approval_is_digest_bound_and_not_an_execution_button(self):
        approval = self.states["approval"]
        digest = approval["digest"]
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(approval["approval"]["command"], f"APPROVE {digest}")
        self.assertNotIn("fetch(\"/api", self.js)
        self.assertNotIn("Execute", self.html + self.js)

    def test_review_uses_backend_reason_enum(self):
        review = self.states["review"]["review"]
        self.assertEqual(review["options"], ["PASS", "FAIL", "UNCERTAIN", "SKIP"])
        self.assertIn("TRAJECTORY_FLOW", review["reasons"])
        self.assertIn("review-reason", self.js)

    def test_accessibility_basics_are_present(self):
        for text in ('<main id="workspace"', 'role="status"', 'aria-live="polite"', 'aria-current="step"'):
            self.assertIn(text, self.html + self.js)
        self.assertIn(":focus-visible", self.css)
        self.assertIn("prefers-reduced-motion", self.css)

    def test_no_package_toolchain_or_external_asset(self):
        self.assertFalse((ROOT / "package.json").exists())
        self.assertNotIn("https://", self.html)
        self.assertNotIn("http://", self.html)

    def test_fixture_query_uses_own_property_lookup(self):
        self.assertIn("Object.hasOwn(states, requestedState)", self.js)

    def test_python_commands_use_repository_direnv(self):
        self.assertEqual(self.makefile.count('direnv exec "$(DIRENV_ROOT)"'), 2)


if __name__ == "__main__":
    unittest.main()
