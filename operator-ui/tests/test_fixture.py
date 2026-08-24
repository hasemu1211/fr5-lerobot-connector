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
        cls.messages = (ROOT / "messages.js").read_text()
        cls.css = (ROOT / "styles.css").read_text()
        cls.makefile = (ROOT / "Makefile").read_text()
        cls.architecture = (ROOT / "architecture.md").read_text()
        cls.browser_regression = (ROOT / "tests/browser-regression.html").read_text()

    def test_required_states_and_safe_next_actions(self):
        self.assertEqual(set(self.states), {"setup", "ready", "blocked", "approval", "running", "review", "recovery"})
        self.assertEqual(self.states["setup"]["step"], 0)
        for name, state in self.states.items():
            self.assertIn("authority", state, name)
            self.assertIn("evidence", state, name)
        for name in ("setup", "ready", "blocked", "running", "review", "recovery"):
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
        self.assertIn('name="reason" required disabled', self.js)
        self.assertIn("reasonField.hidden = !needsReason", self.js)

    def test_progress_and_corrected_approval_have_browser_regressions(self):
        self.assertIn('typeof state.progress !== "number"', self.js)
        self.assertIn("Number.isFinite(state.progress)", self.js)
        self.assertIn('addEventListener("input"', self.js)
        for marker in ("hostile progress is rejected", "corrected exact phrase advances", "${decision} omits reason"):
            self.assertIn(marker, self.browser_regression)

    def test_accessibility_basics_are_present(self):
        for text in ('<main id="workspace"', 'role="status"', 'aria-live="polite"', 'aria-current="step"'):
            self.assertIn(text, self.html + self.js)
        for text in ('for="language-select"', 'aria-describedby="language-current"', 'data-message-aria-label="fixtureMode"'):
            self.assertIn(text, self.html)
        self.assertIn(":focus-visible", self.css)
        self.assertIn("prefers-reduced-motion", self.css)

    def test_errors_are_announced_and_inactive_steps_use_aa_color(self):
        self.assertIn("forcedLoadFailure", self.browser_regression)
        self.assertIn("initial load failure is announced", self.browser_regression)
        self.assertIn("language switch announcement is preserved after load failure", self.browser_regression)
        self.assertIn('function renderError(announce = true)', self.js)
        self.assertIn('renderError(false)', self.js)
        self.assertIn('.workflow-steps li { padding: .8rem .5rem 0 0; color: var(--muted);', self.css)
        self.assertNotIn("color: #6c7b89", self.css)

    def test_static_language_catalog_covers_all_states(self):
        self.assertLess(self.html.index('src="messages.js"'), self.html.index('src="app.js"'))
        self.assertIn('new URLSearchParams(location.search).get("lang")', self.js)
        self.assertIn('document.documentElement.lang = currentLanguage', self.js)
        self.assertIn('announcer.textContent = message("languageChanged")', self.js)
        for key in self.states:
            self.assertIn(f"      {key}: {{", self.messages)
        for text in ("설정 필요", "셀에서 새 계획을 만들 준비가 되었습니다", "상단 카메라에 최신 프레임이 없습니다", "여기에 표시된 계획만 승인하세요", "픽업이 진행 중입니다", "성공적인 픽업 시연입니까?", "다시 계획하기 전에 실제 장면을 복구하세요"):
            self.assertIn(text, self.messages)

    def test_korean_catalog_keeps_protected_tokens(self):
        self.assertIn("const localized = {...source, ...copy}", self.js)
        self.assertIn("localized.nextAction.command = source.nextAction.command", self.js)
        for token in ("/camera/up/color/image_raw", "fr5-up-rgb-30hz-v1", "RELEASE_UNCONFIRMED", "CAMERA_WARMUP_FAILED"):
            self.assertIn(token, self.messages)
        self.assertIn("Korean status and reason codes preserve canonical bytes", self.browser_regression)

    def test_no_package_toolchain_or_external_asset(self):
        self.assertFalse((ROOT / "package.json").exists())
        self.assertNotIn("https://", self.html)
        self.assertNotIn("http://", self.html)
        self.assertNotIn("localStorage", self.js + self.messages)
        self.assertNotIn("fetch(\"http", self.js)

    def test_fixture_query_uses_own_property_lookup(self):
        self.assertIn("Object.hasOwn(states, requestedState)", self.js)
        self.assertIn('requestedState : "setup"', self.js)

    def test_recovery_copy_does_not_invent_preflight_and_runner_uses_direnv(self):
        product_copy = json.dumps(self.states) + self.architecture
        self.assertNotIn("preflight_collection.sh " + "--ready", product_copy)
        self.assertNotIn("`python3 -m " + "tools.data_factory.run_job", self.architecture)
        self.assertTrue(self.states["ready"]["nextAction"]["command"].startswith("direnv exec . python3 -m tools.data_factory.run_job"))

    def test_python_commands_use_repository_direnv(self):
        self.assertEqual(self.makefile.count('direnv exec "$(DIRENV_ROOT)"'), 2)


if __name__ == "__main__":
    unittest.main()
