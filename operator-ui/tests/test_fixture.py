import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DataFactoryOperatorUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((ROOT / "fixtures/states.json").read_text(encoding="utf-8"))
        cls.view = cls.fixture["base_view"]
        cls.html = (ROOT / "index.html").read_text(encoding="utf-8")
        cls.js = (ROOT / "app.js").read_text(encoding="utf-8")
        cls.messages = (ROOT / "messages.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "styles.css").read_text(encoding="utf-8")
        cls.browser = (ROOT / "tests/browser-regression.html").read_text(encoding="utf-8")

    def test_product_is_a_six_step_collection_flow(self):
        self.assertIn("<h1>로봇 학습 데이터 수집</h1>", self.html)
        self.assertEqual(
            re.findall(r'data-step="([a-z]+)"', self.html),
            ["environment", "plan", "review", "execution", "results", "next"],
        )
        for label in ("환경 준비", "수집 계획", "계획 확인", "실행", "결과", "다음 수집"):
            self.assertIn(label, self.html)
        for retired in ("FIXED LANE", "통합 상태공간", "한 평면", "authenticated HUMAN", "한 번", "파일럿", "테스트 수집"):
            self.assertNotIn(retired, self.html + self.messages)

    def test_catalog_owns_every_required_control_and_unavailable_reason(self):
        axes = self.view["catalog"]["axes"]
        self.assertEqual(
            set(axes),
            {"workspace", "frame", "task", "object", "grasp", "start", "motion", "variant", "camera", "data_mode", "split"},
        )
        for axis in axes:
            self.assertGreater(len(axes[axis]), 0)
            self.assertIn(f'data-axis="{axis}"', self.html)
        self.assertFalse(next(item for item in axes["task"] if item["id"] == "pick_place")["available"])
        self.assertFalse(next(item for item in axes["variant"] if item["id"] == "TWO_STAGE_ALIGN")["available"])
        self.assertIn('option.available ? "" : "disabled"', self.js)
        self.assertIn("humanReason(option.reason)", self.js)
        self.assertIn("호환되지 않는 다른 항목도 함께 조정될 수 있습니다", self.html)

    def test_count_repeat_split_and_shared_authoring_are_editable(self):
        self.assertEqual(self.view["draft"]["requested_count"], 3)
        self.assertGreater(self.view["draft"]["requested_count"], 1)
        self.assertIn('id="count-input" type="number" min="1" max="100"', self.html)
        self.assertIn("조건별 최대 반복", self.html)
        self.assertIn("현재 작성 범위", self.js)
        self.assertIn('id="repeat-input" type="number" min="1" max="100"', self.html)
        self.assertIn('id="prepare-environment"', self.html)
        self.assertIn('value="ASSISTED"> 자동 선택', self.html)
        self.assertIn('value="DIRECT_EDIT"> 직접 선택', self.html)
        self.assertIn('submitIntent("update_draft"', self.js)
        self.assertIn('event.target.closest("[data-cell-id]")', self.js)
        self.assertIn('[pose ? "remove_pose" : "add_pose"]', self.js)

    def test_direct_pose_editor_exposes_backend_owned_order_and_bounds(self):
        for marker in (
            'id="direct-pose-editor"',
            'id="direct-x-input"',
            'id="direct-y-input"',
            'id="direct-yaw-input"',
            'id="add-direct-pose"',
            'aria-label="입력된 직접 자세 순서"',
            "view.catalog.workspace_domain",
            "view.draft.direct_poses ?? []",
            "add_pose:",
            "remove_pose: pose",
        ):
            self.assertIn(marker, self.html + self.js)
        self.assertIn("등록된 셀은 빠른 선택용입니다", self.html)
        self.assertIn("!values.every(Number.isFinite)", self.js)
        self.assertIn("form.checkValidity()", self.js)
        self.assertIn("form.reportValidity()", self.js)
        self.assertNotIn("% 360", self.js)
        self.assertNotIn('name="domain_digest"', self.html)

    def test_workspace_registration_is_native_ordered_and_hidden_without_projection(self):
        registration = self.view["workspace_registration"]
        self.assertEqual(list(registration["captures"]), ["CENTER", "X_REF", "Y_CHECK"])
        self.assertTrue(all(value is False for value in registration["captures"].values()))
        self.assertIsNone(self.fixture["states"]["workspace_absent"]["workspace_registration"])
        self.assertEqual(
            re.findall(r'\["(CENTER|X_REF|Y_CHECK)"', self.js)[:3],
            ["CENTER", "X_REF", "Y_CHECK"],
        )
        for marker in (
            'id="workspace-entry"',
            'id="workspace-dialog"',
            'id="workspace-captures"',
            'id="source-scale-bar"',
            'id="final-scale-bar"',
            'entry.hidden = !workspace',
            'view.runtime.workflow_state === "AUTHORING"',
            "티치 펜던트",
            "이 화면은 로봇 동작 명령을 보내지 않습니다",
        ):
            self.assertIn(marker, self.html + self.js)
        self.assertNotIn("raw_joint_snapshot", json.dumps(self.fixture, ensure_ascii=False))

    def test_workspace_intents_use_exact_payloads_and_native_measurement_validation(self):
        captured = self.fixture["states"]["workspace_captured"]
        previewed = self.fixture["states"]["workspace_previewed"]
        self.assertEqual(captured["workspace_registration"]["captures"], {"CENTER": True, "X_REF": True, "Y_CHECK": True})
        self.assertIn("preview_workspace", captured["available_ops"])
        self.assertEqual(previewed["workspace_registration"]["preview"]["status"], "CANDIDATE_WITHIN_TOLERANCE")
        for marker in (
            'submitIntent("capture_workspace_point", {label: button.dataset.captureLabel})',
            'submitIntent("preview_workspace", {source_scale_bar_mm: source, final_scale_bar_mm: final})',
            'submitIntent("save_workspace_revision", {preview_digest: digest})',
            'submitIntent("new_workspace_registration", {})',
            "form.checkValidity()",
            "form.reportValidity()",
            'canIntent("save_workspace_revision")',
        ):
            self.assertIn(marker, self.js)
        self.assertIn('min="96" max="104"', self.html)

    def test_workspace_save_refreshes_catalog_without_minting_authority(self):
        saved = self.fixture["states"]["workspace_saved"]
        registration = saved["workspace_registration"]
        self.assertEqual(saved["available_ops"], ["update_draft", "compile_draft", "new_workspace_registration"])
        self.assertEqual(registration["promotion"]["status"], "PROMOTED")
        self.assertEqual(len(registration["history"]), 1)
        self.assertFalse(registration["execution_authorized"])
        self.assertFalse(registration["training_approved"])
        self.assertIn(registration["promotion"]["calibration_id"], {item["id"] for item in saved["catalog"]["axes"]["frame"]})
        self.assertIn("카탈로그를 같은 프로세스에서 새로고침했습니다", self.js)
        self.assertIn("이 저장은 로봇 실행 권한이나 학습 승인을 만들지 않습니다", self.js)

    def test_saved_frame_is_authorable_while_motion_compile_stays_unavailable(self):
        blocked = self.fixture["states"]["workspace_motion_required"]
        self.assertFalse(blocked["draft"]["execution_ready"])
        self.assertEqual(
            blocked["draft"]["execution_reason"],
            "MOTION_QUALIFICATION_REQUIRED",
        )
        self.assertIn("update_draft", blocked["available_ops"])
        self.assertNotIn("compile_draft", blocked["available_ops"])
        self.assertIn("view.draft.execution_ready === false", self.js)
        self.assertIn("view.draft.execution_reason ?? view.catalog.selection_execution?.reason", self.js)
        self.assertIn('document.querySelector("#compile-campaign").hidden = false', self.js)
        self.assertIn('document.querySelector("#compile-campaign").disabled = !canIntent("compile_draft")', self.js)
        self.assertIn("MOTION_QUALIFICATION_REQUIRED", self.messages)

    def test_review_campaign_has_one_digest_bound_authorization(self):
        review = self.fixture["states"]["review_campaign"]
        self.assertEqual(review["runtime"]["workflow_state"], "REVIEW_CAMPAIGN")
        self.assertEqual(
            review["available_ops"],
            ["edit_campaign_draft", "authorize_campaign"],
        )
        self.assertEqual(review["campaign_envelope"]["episode_count"], 3)
        for field in ("manifest_digest", "envelope_digest"):
            self.assertRegex(review["campaign_envelope"][field], r"^sha256:[0-9a-f]{64}$")
            self.assertIn(f"{field}: currentView.campaign_envelope.{field}", self.js)
        self.assertIn('canIntent("authorize_campaign")', self.js)
        self.assertIn('submitIntent("edit_campaign_draft"', self.js)
        self.assertIn("수집 시작", self.html + self.messages)
        self.assertNotIn("approve_exact_plan", self.js)

    def test_continuous_execution_projects_progress_and_reachable_cancel(self):
        running = self.fixture["states"]["running"]
        campaign = running["campaign_session"]["campaign"]
        self.assertEqual(
            (campaign["completed_intents"], campaign["remaining_intents"], running["runtime"]["current_episode"], running["runtime"]["next_episode"]),
            (1, 2, 2, 3),
        )
        self.assertEqual(running["available_ops"], ["cancel_session"])
        for fact in ("completed", "total", "current", "next", "recorder"):
            self.assertIn(f'data-fact="{fact}"', self.js)
        self.assertIn('id="cancel-campaign"', self.html)
        self.assertIn("RUNNING_CANCEL_UNAVAILABLE", self.js)
        self.assertIn("문제 있음 · 즉시 중단", self.html)

    def test_results_keep_measurement_review_and_coverage_separate(self):
        results = self.fixture["states"]["results"]
        self.assertEqual(len(results["episode_history"]), 3)
        self.assertTrue(all(item["technical_evidence"]["status"] == "PASS" for item in results["episode_history"]))
        self.assertTrue(all(item["human_semantic"] == "NOT_MEASURED" for item in results["episode_history"]))
        self.assertEqual(results["candidate_review"]["choices"], ["PASS", "FAIL", "UNCERTAIN"])
        for marker in ("수집 커버리지", "작업 성공", "작업 실패", "판정 보류", "데이터 보존 상태와 학습 사용 승인"):
            self.assertIn(marker, self.html + self.js)
        self.assertIn("review_binding_digest: currentView.candidate_review.review_binding_digest", self.js)
        self.assertNotIn("training_approval", self.js)
        self.assertNotIn("item.admission_state ?? item.human_semantic", self.js)

    def test_terminal_offers_one_clear_same_process_next_campaign_path(self):
        complete = self.fixture["states"]["complete"]
        self.assertEqual(complete["runtime"]["workflow_state"], "TERMINAL")
        self.assertEqual(complete["available_ops"], ["new_campaign_same_settings"])
        self.assertIn("다음 캠페인 계획", self.html)
        self.assertIn("필요한 항목만 바꿀 수 있습니다", self.html)
        self.assertIn('canIntent("new_campaign_same_settings")', self.js)
        self.assertNotIn('id="edit-new-campaign-action"', self.html)
        self.assertIn("프로세스 재시작 없음", self.html)

    def test_fail_close_uses_available_ops_and_never_replays(self):
        for marker in (
            'currentView.available_ops.includes(op)',
            "VIEW_REVISION_ROLLBACK",
            "VERSION_CONFLICT",
            "최신 상태 다시 불러오기",
            'window.addEventListener("online"',
        ):
            self.assertIn(marker, self.js + self.messages)
        self.assertNotIn("setInterval", self.js)
        self.assertNotIn("localStorage", self.js)
        self.assertNotIn("indexedDB", self.js)
        self.assertIn("요청은 실행되지 않았습니다", self.js)

    def test_missing_measurement_is_never_rendered_as_fail(self):
        blocked = self.fixture["states"]["blocked"]
        self.assertEqual(blocked["runtime"]["measurement_outcome"], "NOT_AVAILABLE")
        self.assertIn('return "측정 자료 없음"', self.js)
        self.assertIn('NOT_MEASURED: "사후 검토를 수행하지 않음"', self.messages)
        self.assertIn('NOT_AVAILABLE: "측정 자료 없음"', self.messages)
        self.assertIn("사후 검토를 수행하지 않았습니다", self.js)
        self.assertNotIn('?? "FAIL"', self.js)

    def test_raw_contract_details_stay_in_collapsed_technical_details(self):
        self.assertIn('<details id="technical-details"', self.html)
        self.assertIn("manifest_digest: view.campaign_envelope?.manifest_digest", self.js)
        self.assertIn("reason_codes: view.runtime.reason_codes", self.js)
        self.assertIn("workspace_preview_digest: view.workspace_registration?.preview?.preview_digest", self.js)
        self.assertNotIn("digest", self.html.lower())
        self.assertNotIn("reason code", self.html.lower())
        self.assertNotIn("lifecycle owner", self.html.lower())

    def test_accessibility_and_zoom_floor_are_preserved(self):
        for marker in (
            '<html lang="ko">',
            '<a class="skip-link"',
            'role="status" aria-live="polite"',
            'aria-label="데이터 수집 단계"',
            'type="radio"',
            'type="number"',
            '<details id="technical-details"',
            'aria-pressed="${selected}"',
        ):
            self.assertIn(marker, self.html + self.js)
        self.assertIn(":focus-visible", self.css)
        self.assertIn("min-height: 44px", self.css)
        self.assertIn("@media (max-width: 700px)", self.css)
        self.assertIn("prefers-reduced-motion: reduce", self.css)
        self.assertNotIn("outline: none", self.css)

    def test_native_dependency_boundary_and_browser_fixture(self):
        self.assertFalse((ROOT / "package.json").exists())
        self.assertNotIn("https://", self.html)
        self.assertNotIn("React", self.html + self.js)
        self.assertIn('fetch("/api/view"', self.js)
        self.assertIn('fetch("/api/intent"', self.js)
        self.assertEqual(self.html.count("<!-- OPERATOR_TOKEN -->"), 1)
        for marker in ("REVIEW_CAMPAIGN", "authorize_campaign", "three serial episodes", "same-process next campaign", "raw yaw reaches backend", "unavailable direct-pose domain disables input", "workspace hidden when absent", "three ordered workspace captures", "digest-bound workspace save", "workspace flow posts no motion intent"):
            self.assertIn(marker, self.browser)


if __name__ == "__main__":
    unittest.main()
