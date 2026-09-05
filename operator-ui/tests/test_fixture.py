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
            "seed-input",
            "experiment-design-form",
            "design-columns-input",
            "design-rows-input",
            "design-yaw-input",
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
        self.assertIn('<label for="seed-input">Campaign seed<input id="seed-input" type="number" min="0" max="9007199254740991" step="1" inputmode="numeric"></label>', self.html)

    def test_campaign_seed_is_projected_reviewed_and_updated_exactly(self):
        self.assertIn('!Number.isSafeInteger(view.draft.normalized_seed) || view.draft.normalized_seed < 0', self.js)
        self.assertIn('["Campaign seed", String(view.draft.normalized_seed)]', self.js)
        self.assertIn('normalized_seed: view.draft.normalized_seed', self.js)
        self.assertIn('normalized_seed: normalizedSeed', self.js)
        self.assertIn('"normalized_seed": 0', (ROOT / "fixtures/states.json").read_text(encoding="utf-8"))
        self.assertIn("campaign seed posts one exact draft update", self.browser)

    def test_experiment_design_is_backend_owned_and_coverage_terms_are_distinct(self):
        for marker in (
            "state_space_design_factors",
            'state_space_design_factors: {columns, rows, yaw_cdf_strata}',
            "등록된 적격 조건",
            "현재 자동 실험 설계",
            "모든 작업영역 A4-local 설계 완전 coverage",
            "실제 색상/시트 결속 검증 전",
            "물리 A4 결속",
            "catalog_eligible_condition_count",
            "per_workspace_condition_count",
            "full_coverage_episode_count",
            "물체 위치 ${summary.object_position_count}개",
            "최초 source + 각 destination",
            "shape.columns !== design.spatial_strata.columns",
        ):
            self.assertIn(marker, self.js)
        self.assertNotIn("A4 기준점", self.html + self.js)
        self.assertNotIn("Math.random", self.js)
        self.assertIn(
            "experiment design posts one atomic backend update",
            self.browser,
        )

    def test_sampling_and_reposition_provenance_are_read_only(self):
        fixture = (ROOT / "fixtures/states.json").read_text(encoding="utf-8")
        for marker in (
            "validateSamplingProvenance(view.sampling_provenance)",
            "validateStateSpaceDesignProfile(provenance.state_space_design_profile, provenance.yaw_sampling_profile)",
            'sampling_provenance: view.sampling_provenance',
            "object_reposition_bindings: view.coverage?.sequence?.map",
            '"yaw_sampling_profile_id"',
            '"approach_sampling_profile_id"',
            "item?.object_reposition",
            'reposition.recording_scope !== "OUT_OF_DATASET"',
            "recorder/dataset 쓰기 없음",
            'typeof yaw.sampling_seed !== "string"',
            "BigInt(yaw.sampling_seed) > 18446744073709551615n",
            'profile.assignment !== "ROTATING_BALANCED_FRACTIONAL_FACTORIAL"',
            'profile.execution_order !== "CONTIGUOUS_YAW_BLOCKS"',
            'profile.initial_source_policy !== "CONDITION_ON_OBSERVED_SOURCE"',
            'stateSpaceDesignText(provenance.state_space_design_profile)',
            "validateStateSpaceSlot(item?.state_space_slot, view.sampling_provenance?.state_space_design_profile)",
            'typeof yaw.sampling_seed !== "string"',
            "stateSpaceSlotText(item.state_space_slot)",
            "validateActiveEpisodePlan(view.active_episode_plan)",
            "validateActiveEpisodePlanCoherence(view)",
            "validateRuntimeRepositionEvidence(view)",
            'typeof trajectory.sampling_seed !== "string"',
            "현재 실행할 정확한 궤적",
            '"data_factory.yaw_sample_binding.v4"',
            '"object_reposition_plan_artifact_digest"',
            '"object_reposition_collision_report_digest"',
            '"object_reposition_plan_only_no_motion_digest"',
            "object_reposition_runtime_evidence: view.runtime.evidence",
            "설계 정격(endpoint당, repeat=1)",
            "실제 campaign prefix는 아래 slot binding 기준",
        ):
            self.assertIn(marker, self.js)
        self.assertNotIn('if (provenance === undefined || provenance === null) return;', self.js)
        for marker in (
            '"object_dimensions_mm": [24, 24, 24]',
            '"state_space_design_profile_id": "wood-cube-24mm-a4-cdf3-r001"',
            '"approach_sampling_profile_id": "wood-cube-24mm-top-wrist-approach-r001"',
            '"schema_version": "data_factory.yaw_sample_binding.v4"',
        ):
            self.assertIn(marker, fixture)
        self.assertIn('"sampling_seed": "5128904136610758680"', fixture)
        self.assertIn(
            "64-bit derived yaw seed survives JSON and renders exactly without Number coercion",
            self.browser,
        )
        self.assertIn(
            "yaw sampling seed rejects numbers and non-canonical or out-of-range decimal strings",
            self.browser,
        )
        self.assertIn(
            "active plan renders exact u64 trajectory seed, phase parameters, and collision evidence",
            self.browser,
        )
        for marker in (
            "missing sampling provenance fails closed",
            "active plan must match selected recipe, current coverage yaw binding, trajectory target, and runtime child",
            "runtime reposition evidence rejects a malformed exact digest",
            "runtime reposition evidence must match the current coverage binding",
            "runtime renders the exact reposition binding, plan, collision, and no-motion evidence without another request",
            "cross-artifact yaw tolerates sub-nanodegree serialization drift",
        ):
            self.assertIn(marker, self.browser)
        self.assertNotIn("Number.isSafeInteger(yaw.sampling_seed)", self.js)
        self.assertNotIn("Math.random", self.js)

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
        submit = self.js.split("async function submitIntent", 1)[1].split(
            "async function submitImmediateCancel", 1,
        )[0]
        self.assertNotIn("stopWatch();", submit)
        self.assertEqual(submit.count("loadView("), 1)

    def test_immediate_cancel_is_independent_and_latched(self):
        cancel = self.js.split(
            "async function submitImmediateCancel", 1,
        )[1].split("async function loadView", 1)[0]
        self.assertNotIn("intentBusy", cancel)
        self.assertIn("cancelPending = true", cancel)
        self.assertIn('fetch("/api/intent"', cancel)
        for marker in (
            "immediate cancel remains reachable while an ordinary intent is busy",
            "immediate cancel latches before its response while RUNNING remains authoritative",
            "busy-path cancel emits exactly one independent POST",
            "cancel remains single-submit after the ordinary intent completes",
        ):
            self.assertIn(marker, self.browser)

    def test_pre_episode_failure_does_not_render_or_renumber_history(self):
        self.assertIn(
            "pre-episode failure preserves 32 canonical episodes and never renders episode 33",
            self.browser,
        )

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
            "PHYSICAL_HOME_SNAPSHOT",
            "CANDIDATE_REVIEW_STATE",
            "DIRECT_YAW_TRANSITION_UNSAFE",
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
            "VLA 지시문",
            "episode_instruction_binding_digest",
        ):
            self.assertIn(marker, self.html + self.js)

    def test_status_refresh_preserves_only_same_candidate_reason_draft(self):
        for marker in (
            "reviewRenderKey",
            "reviewBindingDigest",
            "sameBinding",
            "reasonDraft",
            "reviewReasonDraft",
            "reasons.includes(reasonDraft)",
            "same candidate refresh preserves the chosen reason after focus loss",
            "changed candidate binding clears the prior reason draft",
        ):
            self.assertIn(marker, self.js + self.browser)

    def test_live_revision_preserves_explicit_results_and_stale_review_is_single_shot(self):
        for marker in (
            "renderedWorkflowStep",
            "explicit results view survives a live workflow transition",
            "stale semantic review requires an explicit human retry",
            "stale semantic review is never replayed",
            "최신 분류 대상을 다시 확인하고 분류를 다시 선택하세요",
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

    def test_runtime_separates_motion_authority_from_recorder_state(self):
        for marker in (
            '"NOT_AUTHORIZED", "DISPATCHING", "ACTIVE", "PAUSED_AT_GATE"',
            'data-fact="motion"',
            '["로봇 동작", runtime.motion.label]',
            '["기록기", runtime.recorder.label',
        ):
            self.assertIn(marker, self.js)

    def test_live_details_and_results_prioritize_operator_action(self):
        self.assertIn(
            '<details class="runtime-evidence"><summary>현재 실행할 정확한 궤적</summary>',
            self.js,
        )
        results = self.html.split('id="step-results"', 1)[1].split("</section>", 1)[0]
        self.assertLess(results.index('id="review-queue"'), results.index('class="result-layout"'))


if __name__ == "__main__":
    unittest.main()
