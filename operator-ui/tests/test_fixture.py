import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DataFactoryOperatorUiStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "index.html").read_text(encoding="utf-8")
        cls.js = (ROOT / "app.js").read_text(encoding="utf-8")
        cls.messages = (ROOT / "messages.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "styles.css").read_text(encoding="utf-8")
        cls.browser = (ROOT / "tests/browser-regression.html").read_text(encoding="utf-8")

    def test_minimum_product_dom_contract(self):
        self.assertEqual(
            re.findall(r'data-step="([a-z]+)"', self.html),
            ["environment", "plan", "review", "execution", "results", "next"],
        )
        for element_id in (
            "connection-banner",
            "announcer",
            "camera-setup",
            "camera-role-list",
            "prepare-environment",
            "compile-campaign",
            "cancel-campaign",
            "technical-details",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertEqual(self.html.count("<!-- OPERATOR_TOKEN -->"), 1)

    def test_accessibility_floor(self):
        for marker in (
            '<html lang="ko">',
            '<a class="skip-link"',
            'role="status" aria-live="polite"',
            'aria-label="데이터 수집 단계"',
            'type="radio"',
            'type="number"',
            '<details id="technical-details"',
        ):
            self.assertIn(marker, self.html)
        self.assertIn('aria-pressed="${selected}"', self.js)
        for marker in (":focus-visible", "min-height: 44px", "@media (max-width: 700px)", "prefers-reduced-motion: reduce"):
            self.assertIn(marker, self.css)
        self.assertNotIn("outline: none", self.css)

    def test_native_dependency_and_bridge_boundary(self):
        self.assertFalse((ROOT / "package.json").exists())
        self.assertNotIn("https://", self.html)
        self.assertNotIn("React", self.html + self.js)
        self.assertIn('fetch("/api/view"', self.js)
        self.assertIn('fetch("/api/intent"', self.js)
        self.assertIn('nativeFetch("../fixtures/states.json")', self.browser)
        self.assertIn('<script src="app.js"></script>', self.browser)

    def test_forbidden_copy_and_browser_authority(self):
        product_copy = self.html + self.messages
        for retired in (
            "FIXED LANE",
            "통합 상태공간",
            "authenticated HUMAN",
            "브라우저가 백엔드를 다시 시작",
            "학습 사용 승인됨",
        ):
            self.assertNotIn(retired, product_copy)
        for forbidden in ("approve_exact_plan", "localStorage", "indexedDB", "setInterval"):
            self.assertNotIn(forbidden, self.js)
        self.assertIn("화면은 현재 상태를 표시하고 요청만 보냅니다", self.html)
        self.assertIn("수집 완료는 학습 사용 승인이 아닙니다", self.html)
        self.assertNotIn("usb-Generic", self.html)

    def test_runtime_failures_have_specific_operator_copy(self):
        for code in (
            "RECORDER_READINESS_TIMEOUT",
            "RECORDER_READINESS_ROW_FPS",
            "RECORDER_READINESS_CAMERA_FPS",
            "RECORDER_READINESS_DROPS",
            "RECORDER_READINESS_ALIGNMENT",
            "RECORDER_READINESS_QUALITY",
            "RECORDER_READINESS_SCHEMA",
            "RECORDER_READINESS_STALE",
            "RECORDER_READINESS_MISMATCH",
            "RECORDER_READINESS_TRIM",
            "RECORDER_WRITER_FAULT",
            "RECORDER_SAMPLER_FAULT",
            "RECORDER_FREEZE_TIMEOUT",
            "CANDIDATE_REVIEW_STATE",
        ):
            self.assertIn(f"{code}:", self.messages)
        self.assertIn(
            'typeof code === "string" && code ? code : "확인 필요"',
            self.js,
        )

    def test_pick_place_ui_separates_robot_start_source_destination_and_plan_revision(self):
        for marker in (
            "로봇 시작 자세",
            "물체 출발점 (SOURCE)",
            "직전 도착점은 다음 출발점",
            "최종 집기 접근",
            "destination_pose",
            "작성안 r",
        ):
            self.assertIn(marker, self.html + self.js)

    def test_status_refresh_preserves_candidate_reason_control(self):
        for marker in (
            "reviewRenderKey",
            "reviewBindingDigest",
            'document.activeElement === document.querySelector("#candidate-reason")',
            "status refresh preserves the focused candidate reason control",
        ):
            self.assertIn(marker, self.js + self.browser)


if __name__ == "__main__":
    unittest.main()
