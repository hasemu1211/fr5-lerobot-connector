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
        cls.backend_contract = (ROOT / "backend-contract-proposal.md").read_text(encoding="utf-8")

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
        self.assertIn("selectedOption?.execution_reason", self.js)
        self.assertIn("humanReason(selectedOption.execution_reason)", self.js)
        self.assertIn("호환되지 않는 다른 항목도 함께 조정될 수 있습니다", self.html)
        for label in ("작업 레시피", "등록 물체 프로필", "등록 잡기 프로필"):
            self.assertIn(label, self.html)
        self.assertIn("현재 화면 상태에 포함된 레시피 정보가 있을 때만 표시됩니다", self.html)
        for code, copy in {
            "GENERAL_QUALIFICATION_REQUIRED": "일반 수집 전에 이 조합의 실제 장치 동작을 먼저 확인해야 합니다",
            "TASK_CALLER_NOT_CONFIGURED": "이 작업을 실행할 연결이 아직 설정되지 않았습니다",
            "TASK_LIVE_CALLER_REQUIRED": "이 작업에는 실제 로봇 실행 연결이 필요합니다",
            "CAMERA_REBIND_REQUIRED": "이 카메라를 사용하려면 해당 장치로 수집 서비스를 다시 시작해야 합니다",
            "MOTION_QUALIFICATION_REQUIRED": "이 좌표계에서 실제 로봇 동작을 먼저 확인해야 합니다",
        }.items():
            self.assertIn(f'{code}: "{copy}"', self.messages)

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

    def test_connected_cameras_are_assigned_by_role_and_profile_is_derived(self):
        setup = self.view["camera_setup"]
        self.assertEqual([device["label"] for device in setup["devices"]], ["카메라 1", "카메라 2"])
        self.assertEqual(set(setup["bindings"]), {device["logical_id"] for device in setup["devices"]})
        self.assertEqual(setup["bindings"], {"camera-1": "UP", "camera-2": "UNUSED"})
        for marker in (
            'id="camera-setup"',
            'id="camera-role-list"',
            'id="camera-profile-label"',
            'data-camera-logical-id="${escapeHtml(device.logical_id)}"',
            '["UP", "상단"]',
            '["SIDE", "측면"]',
            '["WRIST", "손목"]',
            '["UNUSED", "사용 안 함"]',
            'submitIntent("update_camera_bindings", {bindings})',
            'const bindings = {...currentView.camera_setup.bindings}',
            'if (occupied) bindings[occupied[0]] = previousRole',
            "setup.available_roles ?? CAMERA_ROLES.map",
            ".filter(([role]) => availableRoles.includes(role))",
            "!availableRoles.includes(role)",
            '${setup.devices.length}대 연결 · ${used.join(" · ")} 사용',
            'section.hidden = !setup',
        ):
            self.assertIn(marker, self.html + self.js)
        self.assertIn('id="camera-select" data-axis="camera" aria-hidden="true" tabindex="-1" hidden', self.html)
        self.assertIn("환경 준비에서 지정한 역할로 자동 설정됩니다", self.html)
        self.assertIn("camera_devices: view.camera_setup?.devices.map", self.js)
        self.assertNotIn("usb-Generic", self.html)

    def test_start_pose_registry_is_separate_and_multi_select(self):
        setup = self.view["start_pose_setup"]
        self.assertEqual(setup["selected_start_pose_ids"], ["fr5-home-r001"])
        self.assertEqual(
            {profile["status"] for profile in setup["profiles"]},
            {"CANDIDATE", "AVAILABLE", "QUALIFICATION_REQUIRED"},
        )
        for marker in (
            'id="start-pose-entry"',
            'id="start-pose-dialog"',
            'id="start-pose-capture-form"',
            'id="start-pose-profile-list"',
            'submitIntent("capture_start_pose", {display_name: displayName})',
            'submitIntent("update_start_pose_selection", {selected_start_pose_ids:',
            'CANDIDATE: "후보"',
            'AVAILABLE: "사용 가능"',
            'QUALIFICATION_REQUIRED: "검증 필요"',
        ):
            self.assertIn(marker, self.html + self.js)
        self.assertIn('id="start-select" data-axis="start" aria-hidden="true" tabindex="-1" hidden', self.html)
        self.assertIn("HOME 복귀는 시작 자세 registry와 별도", self.backend_contract)
        self.assertNotIn("시작 자세를 1개 이상 선택하세요", self.js)

    def test_cartesian_state_space_summary_is_backend_owned(self):
        self.assertEqual(
            self.view["state_space_summary"],
            {
                "selected_start_pose_count": 1,
                "selected_condition_count": 15,
                "eligible_pair_count": 15,
                "planned_count": 3,
            },
        )
        self.assertIn('id="state-space-summary"', self.html)
        for label in ("시작 자세", "A4 기준점", "기준점 조합", "계획된 에피소드"):
            self.assertIn(label, self.js)
        self.assertIn("element.hidden = !summary", self.js)

    def test_camera_terminal_offers_recovery_without_claiming_process_restart(self):
        terminal = self.fixture["states"]["camera_blocked"]
        self.assertEqual(terminal["available_ops"], ["recover_camera_setup"])
        self.assertEqual(terminal["runtime"]["reason_codes"], ["PHYSICAL_CAMERA_TOPIC"])
        self.assertIn('data-recovery-op="recover_camera_setup"', self.js)
        self.assertIn("카메라 다시 연결", self.js + self.messages)
        self.assertIn('submitIntent(button.dataset.recoveryOp, {})', self.js)
        self.assertIn('view.available_ops.includes("recover_camera_setup")', self.js)
        self.assertIn('section.dataset.step !== "environment"', self.js)
        self.assertIn('document.addEventListener("click"', self.js)
        self.assertIn("이전 장치 상태는 다시 확인하기 전까지 사용하지 않습니다", self.js)
        self.assertIn("현재 장치와 역할을 다시 확인해야 합니다", self.js)
        self.assertNotIn("브라우저가 백엔드를 다시 시작", self.html + self.messages)

    def test_camera_projection_fails_closed_and_only_offers_backend_roles(self):
        for marker in (
            '!["READY", "BINDING_REQUIRED", "NO_CAMERA_CONNECTED"].includes(cameraSetup.status)',
            '!["CONNECTED", "CONNECTING", "DISCONNECTED"].includes(device.status)',
            '!availableRoles.includes(cameraSetup.bindings[logicalId])',
            'cameraSetup.status === "READY" && cameraSetup.required_roles.some',
            'CAMERA_ROLE_BINDING_REQUIRED: "연결된 카메라의 사용 위치를 지정해야 합니다"',
            'PHYSICAL_CAMERA_TOPIC: "카메라 영상 연결이 중단되었습니다"',
        ):
            self.assertIn(marker, self.js + self.messages)

    def test_direct_pose_editor_exposes_backend_owned_order_and_bounds(self):
        for marker in (
            'id="current-object-form"',
            'id="current-object-x"',
            'id="current-object-y"',
            'id="current-object-yaw"',
            "view.draft.current_object_pose",
            "current_object_pose:",
            "지금 놓인 물체",
            'id="direct-pose-editor"',
            'id="direct-x-input"',
            'id="direct-y-input"',
            'id="direct-yaw-input"',
            'id="add-direct-pose"',
            'aria-label="입력된 시작 자세와 위치 순서"',
            "view.catalog.workspace_domain",
            "view.draft.direct_poses ?? []",
            "view.draft.direct_pairs",
            'id="direct-start-select"',
            'start_pose_id: document.querySelector("#direct-start-select").value',
            '"add_pair" : "add_pose"',
            "remove_pair: pair",
            '"add_pose"',
            "remove_pose: pose",
        ):
            self.assertIn(marker, self.html + self.js)
        self.assertIn("등록된 셀은 빠른 선택용입니다", self.html)
        self.assertIn('id="direct-selection-count"', self.html)
        self.assertIn('id="cell-grid-disclosure"', self.html)
        self.assertIn("등록된 전체 조건에서 고르기", self.html)
        self.assertIn('disclosure.hidden = !direct', self.js)
        self.assertIn('document.querySelector("#cell-grid").innerHTML = direct ?', self.js)
        self.assertIn("!values.every(Number.isFinite)", self.js)
        self.assertIn("form.checkValidity()", self.js)
        self.assertIn("form.reportValidity()", self.js)
        self.assertNotIn("% 360", self.js)
        self.assertNotIn('name="domain_digest"', self.html)

    def test_workspace_registration_is_native_ordered_and_hidden_without_projection(self):
        registration = self.fixture["states"]["workspace_captured"]["workspace_registration"]
        self.assertEqual(list(registration["captures"]), ["CENTER", "X_REF", "Y_CHECK"])
        self.assertTrue(all(value is True for value in registration["captures"].values()))
        self.assertEqual(registration["display_name"], "놓기 영역 B")
        self.assertIsNone(self.view["workspace_registration"])
        self.assertIsNone(self.fixture["states"]["workspace_absent"]["workspace_registration"])
        self.assertEqual(
            re.findall(r'\["(CENTER|X_REF|Y_CHECK)"', self.js)[:3],
            ["CENTER", "X_REF", "Y_CHECK"],
        )
        for marker in (
            'id="workspace-entry"',
            'id="workspace-dialog"',
            'id="workspace-captures"',
            'id="workspace-name-form"',
            'id="workspace-display-name"',
            'id="workspace-registration-content"',
            'id="source-scale-bar"',
            'id="final-scale-bar"',
            "entry.hidden = !named && !canBegin",
            'view.runtime.workflow_state === "AUTHORING"',
            "티치 펜던트",
            "이 화면은 로봇을 움직이지 않습니다",
        ):
            self.assertIn(marker, self.html + self.js)
        self.assertNotIn("raw_joint_snapshot", json.dumps(self.fixture, ensure_ascii=False))
        self.assertIn('<dt>작업영역 이름</dt>', self.js)
        self.assertNotIn('["작업영역", selectedLabel(view, "workspace")]', self.js)
        self.assertNotIn('["기준 좌표계", selectedLabel(view, "frame")]', self.js)
        self.assertIn("workspace_registration_calibration_id", self.js)
        self.assertNotIn('name="coordinate_frame_name"', self.html)
        self.assertIn("새 작업영역 등록", self.html)
        self.assertIn("작업영역 관리", self.html)
        self.assertNotIn("좌표계 개정본", self.html + self.messages)

    def test_workspace_intents_use_exact_payloads_and_native_measurement_validation(self):
        captured = self.fixture["states"]["workspace_captured"]
        previewed = self.fixture["states"]["workspace_previewed"]
        self.assertEqual(captured["workspace_registration"]["captures"], {"CENTER": True, "X_REF": True, "Y_CHECK": True})
        self.assertIn("preview_workspace", captured["available_ops"])
        self.assertEqual(previewed["workspace_registration"]["preview"]["status"], "CANDIDATE_WITHIN_TOLERANCE")
        for marker in (
            'submitIntent("new_workspace_registration", {display_name: displayName})',
            'submitIntent("capture_workspace_point", {label: button.dataset.captureLabel})',
            'submitIntent("preview_workspace", {source_scale_bar_mm: source, final_scale_bar_mm: final})',
            '["save_workspace", "save_workspace_revision", "discard_workspace_preview"].includes(button.dataset.workspaceOp)',
            'submitIntent(button.dataset.workspaceOp, {preview_digest: digest})',
            "form.checkValidity()",
            "form.reportValidity()",
            'canIntent("save_workspace") ? "save_workspace" : canIntent("save_workspace_revision")',
            'canIntent("discard_workspace_preview")',
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
        self.assertIn("작업영역이 저장되었습니다", self.js)
        self.assertIn("실제 동작 검증이 필요한 상태는 별도로 표시됩니다", self.js)

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
        for fact in ("completed", "current", "next", "recorder"):
            self.assertIn(f'data-fact="{fact}"', self.js)
        for label in ("전체 진행", "현재 에피소드", "다음 에피소드"):
            self.assertIn(label, self.js)
        self.assertIn("runtime.campaign_progress", self.js)
        self.assertIn("현재 에피소드 ·", self.js)
        self.assertIn('id="cancel-campaign"', self.html)
        self.assertIn("RUNNING_CANCEL_UNAVAILABLE", self.js)
        self.assertIn("문제 있음 · 즉시 중단", self.html)

    def test_idle_runtime_accepts_backend_null_progress(self):
        self.assertIn("view.runtime.progress != null", self.js)
        self.assertNotIn("view.runtime.progress !== undefined", self.js)

    def test_results_keep_measurement_review_and_coverage_separate(self):
        results = self.fixture["states"]["results"]
        self.assertEqual(len(results["episode_history"]), 3)
        self.assertTrue(all(item["technical_evidence"]["status"] == "PASS" for item in results["episode_history"]))
        self.assertTrue(all(item["human_semantic"] == "NOT_MEASURED" for item in results["episode_history"]))
        self.assertEqual(results["candidate_review"]["choices"], ["PASS", "FAIL", "UNCERTAIN"])
        self.assertEqual(
            (results["candidate_review"]["episode_number"],
             results["candidate_review"]["queue_remaining"]),
            (2, 2),
        )
        for marker in ("수집 커버리지", "사용 후보", "제외", "보류", "파일 보존과 학습 사용 승인"):
            self.assertIn(marker, self.html + self.js)
        self.assertIn("review_binding_digest: currentView.candidate_review.review_binding_digest", self.js)
        self.assertIn("이미지 품질 또는 대상 가시성 부족", self.messages)
        self.assertNotIn("training_approval", self.js)
        self.assertNotIn("item.admission_state ?? item.human_semantic", self.js)

    def test_terminal_offers_one_clear_same_process_next_campaign_path(self):
        complete = self.fixture["states"]["complete"]
        self.assertEqual(complete["runtime"]["workflow_state"], "TERMINAL")
        self.assertEqual(complete["available_ops"], ["new_campaign_same_settings"])
        self.assertIn("다음 수집 계획", self.html)
        self.assertIn("필요한 항목만 바꿀 수 있습니다", self.html)
        self.assertIn('canIntent("new_campaign_same_settings")', self.js)
        self.assertNotIn('id="edit-new-campaign-action"', self.html)
        self.assertIn("프로세스 재시작 없음", self.html)

    def test_blocked_execution_reuses_existing_new_campaign_operation(self):
        self.assertIn('runtime.workflow_state === "BLOCKED"', self.js)
        self.assertIn('data-recovery-op="new_campaign_same_settings"', self.js)
        self.assertIn("종료된 실행 닫고 새 계획", self.js)

    def test_home_recovery_renders_only_validated_backend_result(self):
        for marker in (
            '["HOME", "ALREADY_HOME"].includes(view.home_recovery.status)',
            "view.home_recovery.gripper_open !== true",
            "![0, 1].includes(view.home_recovery.arm_goal_count)",
            "message(\"status\", recovery.status)",
            'recovery.arm_goal_count === 1 ? "로봇 이동 1회" : "로봇 이동 없음"',
        ):
            self.assertIn(marker, self.js)
        self.assertIn('HOME: "HOME 복귀 완료"', self.messages)
        self.assertIn('ALREADY_HOME: "이미 HOME"', self.messages)
        self.assertNotIn("retry_home", self.js)

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
        self.assertIn('section.dataset.step !== "environment"', self.js)
        self.assertIn('document.querySelector("#setup-subsystems").innerHTML = ""', self.js)
        self.assertNotIn('section.dataset.step !== "execution"; });\n  document.querySelector("#runtime-content").innerHTML = `<div class="recovery-card"', self.js)

    def test_missing_measurement_is_never_rendered_as_fail(self):
        blocked = self.fixture["states"]["blocked"]
        self.assertEqual(blocked["runtime"]["measurement_outcome"], "NOT_AVAILABLE")
        self.assertIn('return "측정 자료 없음"', self.js)
        self.assertIn('NOT_MEASURED: "분류 전"', self.messages)
        self.assertIn('NOT_AVAILABLE: "측정 자료 없음"', self.messages)
        self.assertIn("분류 대기 0개", self.js)
        self.assertNotIn('?? "FAIL"', self.js)

    def test_raw_contract_details_stay_in_collapsed_technical_details(self):
        self.assertIn('<details id="technical-details"', self.html)
        self.assertIn("manifest_digest: view.campaign_envelope?.manifest_digest", self.js)
        self.assertIn("reason_codes: view.runtime.reason_codes", self.js)
        self.assertIn("retention_states: view.episode_history?.map((item) => item.episode_ledger?.retention_state)", self.js)
        self.assertIn("reclaim_states: view.episode_history?.map((item) => item.episode_ledger?.reclaim_state)", self.js)
        self.assertIn("workspace_preview_digest: view.workspace_registration?.preview?.preview_digest", self.js)
        self.assertIn("workspace_id: view.draft.selection.workspace", self.js)
        self.assertIn("frame_id: view.draft.selection.frame", self.js)
        self.assertIn("workspace_registration_calibration_id: view.workspace_registration?.calibration_id", self.js)
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
        for marker in ("REVIEW_CAMPAIGN", "authorize_campaign", "three serial episodes", "same-process next campaign", "direct pair preserves start pose and signed yaw", "unavailable direct-pose domain disables input", "assisted mode omits the full state-space grid", "finite anchors without claiming the continuous domain is fully executable", "absent state-space projections stay hidden", "explicit disclosure reveals the full state-space grid", "camera role change posts one complete binding map", "occupied camera roles swap atomically without duplicates", "workspace hidden when absent", "new independent name before captures", "three ordered workspace captures", "rejected workspace preview exposes one retry path", "digest-bound workspace save", "workspace flow posts no motion intent", "results expose ledger retention and reclaim state", "camera recovery posts an empty intent"):
            self.assertIn(marker, self.browser)


if __name__ == "__main__":
    unittest.main()
