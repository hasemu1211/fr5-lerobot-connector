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
        self.assertIn('<label for="frame-select" hidden>', self.html)
        self.assertIn('<label for="motion-select" hidden>', self.html)
        catalog = self.html.split('<fieldset id="catalog-fields"', 1)[1].split("</fieldset>", 1)[0]
        collection_range = self.html.split('<section class="range-panel"', 1)[1].split("</section>", 1)[0]
        self.assertNotIn('id="workspace-select"', catalog)
        self.assertIn('id="workspace-select"', collection_range)
        self.assertIn('id="workspace-select-label"', collection_range)

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

    def test_revision_watch_replaces_500ms_status_polling(self):
        self.assertNotIn("refreshTimer", self.js)
        self.assertNotIn("setTimeout(loadView, 500)", self.js)
        self.assertIn("/api/view/watch?after_revision=${afterRevision}", self.js)
        self.assertIn("new AbortController()", self.js)
        self.assertIn("stopWatch();", self.js)
        submit = self.js.split("async function submitIntent", 1)[1].split("async function loadView", 1)[0]
        self.assertNotIn("stopWatch();", submit)
        self.assertEqual(submit.count("loadView("), 1)

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
            "물체 출발 작업영역",
            "위로 후퇴하고 HOME",
            "QUALIFIED_PROFILE",
            "검증된 등록 프로필",
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

    def test_expired_bridge_token_requires_a_fresh_page(self):
        for marker in (
            "BRIDGE_SESSION_EXPIRED",
            "새 서버에 다시 연결",
            "window.location.reload()",
            "expired bridge token fails closed and offers one fresh-page reconnect",
        ):
            self.assertIn(marker, self.js + self.messages + self.browser)

    def test_recorder_fact_never_invents_an_unknown_status(self):
        self.assertNotIn('"상태 확인 전"', self.js)


if __name__ == "__main__":
    unittest.main()
