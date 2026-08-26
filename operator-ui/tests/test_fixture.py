import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class GoalTwoOperatorUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((ROOT / "fixtures/states.json").read_text())
        cls.view = cls.fixture["base_view"]
        cls.html = (ROOT / "index.html").read_text()
        cls.js = (ROOT / "app.js").read_text()
        cls.messages = (ROOT / "messages.js").read_text()
        cls.css = (ROOT / "styles.css").read_text()
        cls.browser = (ROOT / "tests/browser-regression.html").read_text()
        cls.docs = "\n".join(
            (ROOT / name).read_text()
            for name in ("README.md", "architecture.md", "backend-contract-proposal.md")
        )

    def test_korean_default_unified_view_contract(self):
        self.assertIn('<html lang="ko">', self.html)
        self.assertEqual(self.view["schema_version"], "data_factory.operator_session_view.v1")
        self.assertEqual(self.view["data_disposition"], "TEST_ONLY")
        self.assertEqual(self.view["effect_scope"], "FAKE")
        self.assertEqual(self.view["lifecycle_action"], "LIVE_COLLECT")
        self.assertIn("통합 상태공간", self.html)

    def test_same_origin_bridge_and_operator_token_contract(self):
        self.assertEqual(self.html.count("<!-- OPERATOR_TOKEN -->"), 1)
        self.assertIn('fetch("/api/view"', self.js)
        self.assertIn('fetch("/api/intent"', self.js)
        self.assertEqual(self.js.count('"X-Operator-Token"'), 1)
        self.assertIn("function unwrapViewEnvelope(value)", self.js)
        self.assertIn('"VIEW_ENVELOPE_INVALID"', self.js)
        self.assertNotIn("Access-Control-Allow-Origin", self.html + self.js)
        self.assertNotIn("WebSocket", self.html + self.js)

    def test_intent_envelope_has_every_digest_bound_field(self):
        match = re.search(r"const envelope = \{(?P<body>.*?)\n  \};", self.js, re.S)
        self.assertIsNotNone(match)
        body = match.group("body")
        for field in ("schema_version", "intent_id", "session_id", "view_revision", "view_digest", "op", "payload"):
            self.assertRegex(body, rf"\b{field}\b")
        self.assertIn('const INTENT_SCHEMA = "data_factory.operator_intent.v1"', self.js)

    def test_approval_is_native_digest_bound_button_without_typed_phrase(self):
        approval = self.fixture["states"]["approval"]["approval"]
        self.assertRegex(approval["plan_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertIn('data-op="approve_exact_plan"', self.js)
        self.assertIn('data-op="reject_plan"', self.js)
        self.assertIn("view_digest: boundView.view_digest", self.js)
        self.assertIn("plan_digest: currentView.approval.plan_digest", self.js)
        self.assertNotIn("approval-input", self.html + self.js)
        self.assertNotIn("typed_phrase", self.html + self.js + self.docs)
        self.assertIn("신원 인증이 아닙니다", self.js)
        self.assertIn("TEST_ONLY 기계적 그리퍼 판정", self.js)
        self.assertIn('approval_scope === "HIL_NUMERIC_PROXY"', self.js)
        self.assertIn('type="checkbox" ${mechanicalProxy ? "checked" : ""} disabled', self.js)
        self.assertEqual(len(approval["operator_summary"]["path"]), 10)
        self.assertEqual(approval["operator_summary"]["path"][-1], "SAFE_POSE_PTP")
        self.assertEqual(
            (approval["preapproval_checklist"]["place_alias"],
             approval["preapproval_checklist"]["place_id"],
             approval["preapproval_checklist"]["full_return_step_count"]),
            ("place1", "PLACE_A", 10),
        )
        self.assertRegex(approval["site_confirmation_digest"], r"^sha256:[0-9a-f]{64}$")
        for marker in ("summary.path.length", "collision_report_digest", "현장 READY 확인 완료"):
            self.assertIn(marker, self.js)

    def test_setup_projection_is_exact_and_exception_is_one_checkpoint(self):
        self.assertEqual(set(self.view["setup"]), {"host_status", "operator_label", "subsystems"})
        self.assertTrue(self.view["setup"]["subsystems"])
        self.assertTrue(all(set(row) == {"label", "status", "detail"} for row in self.view["setup"]["subsystems"]))
        exception = self.fixture["states"]["setup_exception"]
        self.assertEqual(exception["setup"]["host_status"], "READY_WITH_EXCEPTION")
        self.assertEqual(exception["operator_checkpoint"]["kind"], "GRIPPER_MAINTENANCE")
        self.assertEqual(exception["available_ops"], ["resolve_checkpoint"])
        for marker in ('id="setup-panel"', "setup-subsystems", "authenticated HUMAN 아님"):
            self.assertIn(marker, self.html + self.js)

    def test_operator_checkpoints_are_exact_digest_bound_and_not_duplicated(self):
        expected_keys = {"kind", "prompt", "binding_digest", "choices", "evidence"}
        semantic = self.fixture["states"]["semantic"]["operator_checkpoint"]
        release = self.fixture["states"]["release"]["operator_checkpoint"]
        scene_ready = self.fixture["states"]["scene_ready"]["operator_checkpoint"]
        for checkpoint in (semantic, release, scene_ready):
            self.assertEqual(set(checkpoint), expected_keys)
            self.assertRegex(checkpoint["binding_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(semantic["kind"], "SEMANTIC_VERDICT")
        self.assertEqual(semantic["choices"], ["PASS", "FAIL"])
        self.assertEqual(release["kind"], "RELEASE_VERDICT")
        self.assertEqual(release["choices"], ["LANDED", "OFF_SLOT", "UNCERTAIN"])
        self.assertIn("착지", release["prompt"])
        self.assertIn("그리퍼 비움", release["prompt"])
        self.assertIn("후퇴", release["prompt"])
        self.assertIn("안전 스테이징", release["prompt"])
        self.assertEqual(scene_ready["kind"], "SCENE_READY")
        self.assertEqual(scene_ready["choices"], ["SCENE_READY"])
        self.assertIn('canIntent("resolve_checkpoint")', self.js)
        self.assertIn("checkpoint_binding_digest: currentView.operator_checkpoint.binding_digest", self.js)
        self.assertNotIn("checkpoint_binding_digest:", self.html)

    def test_candidate_review_is_exact_and_separate_from_training_approval(self):
        review = self.fixture["states"]["candidate_review"]["candidate_review"]
        self.assertEqual(set(review), {"review_binding_digest", "run_id", "status", "choices", "reasons"})
        self.assertEqual(review["choices"], ["PASS", "FAIL", "UNCERTAIN"])
        self.assertEqual(review["status"], "PENDING")
        self.assertRegex(review["review_binding_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertIn('canIntent("review_candidate")', self.js)
        self.assertIn("review_binding_digest: currentView.candidate_review.review_binding_digest", self.js)
        self.assertIn('const reason = choice === "PASS" ? null', self.js)
        self.assertIn("TRAINING APPROVAL 아님", self.js)
        self.assertNotIn("training_approval", self.js)

    def test_assisted_and_direct_edit_share_one_draft_and_no_extra_tools(self):
        self.assertEqual(self.view["draft"]["authoring_mode"], "ASSISTED")
        self.assertEqual(self.view["draft"]["selector"], "BALANCED_INITIAL")
        self.assertIn('submitIntent("update_draft", {draft_id: currentView.draft.draft_id, authoring_mode:', self.js)
        self.assertIn("ASSISTED and DIRECT_EDIT mutate the same draft through one op", self.browser)
        for forbidden in ("lasso", "LatinHypercube", "scipy", "optimizer", "saved template", "localStorage"):
            self.assertNotIn(forbidden, (self.html + self.js).lower())

    def test_single_screen_contains_every_required_campaign_axis(self):
        rendered_sources = self.html + self.js
        for marker in (
            "workspace/place", "X / Y / yaw", "object", "grasp", "task", "motion", "start",
            "split / repeat", "coverage / selector", "effect_scope", "lifecycle_action", "TEST_ONLY",
            "capability-list", "reason_codes",
        ):
            self.assertIn(marker, rendered_sources)

    def test_pick_place_and_variants_are_not_available(self):
        capabilities = {item["label"]: item for item in self.view["capabilities"]}
        self.assertEqual(capabilities["Task · pick_place"]["status"], "NOT_AVAILABLE")
        self.assertEqual(capabilities["Motion variant · TWO_STAGE_ALIGN"]["status"], "NOT_AVAILABLE")
        self.assertIn("FUTURE_TASK_RECIPE", capabilities["Task · pick_place"]["reason_codes"])
        self.assertIn("NO_PRODUCTION_CALLER", capabilities["Motion variant · TWO_STAGE_ALIGN"]["reason_codes"])

    def test_workspace_wizard_is_qualified_plane_three_point_and_fake_only(self):
        wizard = self.view["workspace_wizard"]
        self.assertEqual(set(wizard["captures"]), {"CENTER", "X_REF", "Y_CHECK"})
        self.assertRegex(wizard["plane_reference"]["digest"], r"^sha256:[0-9a-f]{64}$")
        for text in ("출력 원본 100 mm", "최종 100 mm", "적격 평면", "CENTER → X_REF → Y_CHECK"):
            self.assertIn(text, self.html)
        self.assertIn('currentView.effect_scope !== "FAKE"', self.js)
        self.assertIn('mode: "FAKE"', self.js)

    def test_fail_close_matrix_does_not_queue_or_replay_intents(self):
        self.assertEqual(set(self.fixture["states"]), {
            "draft", "setup_exception", "gripper_normal_graph_required", "approval", "semantic", "release", "scene_ready",
            "candidate_review", "running", "cancel_pending", "blocked", "stale",
            "reconnecting", "physical_toggle", "terminal", "unknown_enum",
        })
        for marker in (
            "BRIDGE_UNAVAILABLE", "VIEW_STALE", "INTENT_REPLAYED", "CANCEL_PENDING",
            "VIEW_REVISION_ROLLBACK", 'window.addEventListener("online"', "reconnect refetches view without replaying an intent",
            "explicit retry may refetch the same rejected view without replaying its intent",
        ):
            self.assertIn(marker, self.messages + self.js + self.browser)
        self.assertNotIn("setInterval", self.js)
        self.assertIn("setTimeout(loadView, 250)", self.js)
        self.assertNotIn("indexedDB", self.js)

    def test_terminal_result_keeps_technical_and_synthetic_provenance_visible(self):
        terminal = self.fixture["states"]["terminal"]["episode_result"]
        self.assertEqual(terminal["technical_evidence"]["status"], "PASS")
        self.assertEqual(terminal["human_semantic"], "NOT_MEASURED")
        self.assertEqual(terminal["synthetic_review"]["reviewed_by"], "TEST_OPERATOR")
        self.assertEqual(terminal["synthetic_coverage_update"]["production_coverage_delta"], 0)
        for marker in ("result-card", "human semantic", "synthetic review", "synthetic coverage"):
            self.assertIn(marker, self.js)
        self.assertIn("candidate review</dt><dd>NOT_APPLICABLE (TEST_ONLY)", self.js)

    def test_fake_and_authority_side_effect_counts_are_zero(self):
        counts = self.view["effect_counts"]
        self.assertTrue(counts)
        self.assertTrue(all(value == 0 for value in counts.values()))
        self.assertEqual(set(counts), {
            "robot_calls", "gripper_calls", "recorder_calls", "dataset_writes",
            "run_state_writes", "production_approvals", "training_authority",
        })
        self.assertIn("PHYSICAL toggle alone sends no plan start or execution intent", self.browser)

    def test_accessibility_floor_uses_native_controls_and_live_status(self):
        for marker in (
            '<a class="skip-link"', '<main id="campaign-desk" tabindex="-1">',
            'role="status" aria-live="polite"', '<dialog id="workspace-dialog"',
            'aria-pressed="${selected}"', 'type="radio"', 'type="number"',
            'data-checkpoint-choice="${escapeHtml(choice)}"', 'data-review-choice="${escapeHtml(choice)}"',
            '<select id="candidate-reason" required>',
        ):
            self.assertIn(marker, self.html + self.js)
        self.assertIn(":focus-visible", self.css)
        self.assertIn("min-height: 44px", self.css)
        self.assertIn("prefers-reduced-motion: reduce", self.css)
        self.assertNotIn("outline: none", self.css)

    def test_dependency_free_fixture_and_docs_match_live_boundary(self):
        self.assertFalse((ROOT / "package.json").exists())
        self.assertNotIn("https://", self.html)
        self.assertIn("GET /api/view", self.docs)
        self.assertIn("POST /api/intent", self.docs)
        self.assertIn("X-Operator-Token", self.docs)
        self.assertIn("TEST_ONLY", self.docs)
        self.assertIn("NOT_AVAILABLE", self.docs)


if __name__ == "__main__":
    unittest.main()
