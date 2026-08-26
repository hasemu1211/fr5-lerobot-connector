const VIEW_SCHEMA = "data_factory.operator_session_view.v1";
const INTENT_SCHEMA = "data_factory.operator_intent.v1";
const RESULT_SCHEMA = "data_factory.operator_intent_result.v1";
const DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/;
const ENUMS = {
  connection_state: new Set(["READY", "STALE", "RECONNECTING", "BLOCKED"]),
  effect_scope: new Set(["FAKE", "PHYSICAL"]),
  lifecycle_action: new Set(["AUTHOR_ONLY", "PLAN_ONLY", "LIVE_COLLECT"]),
  authoring_mode: new Set(["ASSISTED", "DIRECT_EDIT"]),
  selector: new Set(["BALANCED_INITIAL", "DIRECT_LIST"]),
  workflow_state: new Set(["AUTHORING", "AWAITING_APPROVAL", "PAUSED_AWAITING_OPERATOR", "RUNNING", "CANCELLING", "BLOCKED", "TERMINAL"]),
  candidate_status: new Set(["PENDING", "PASS", "FAIL", "UNCERTAIN"]),
};
const SETUP_KEYS = ["host_status", "operator_label", "subsystems"];
const SUBSYSTEM_KEYS = ["label", "status", "detail"];
const CHECKPOINT_KEYS = ["kind", "prompt", "binding_digest", "choices", "evidence"];
const CANDIDATE_REVIEW_KEYS = ["review_binding_digest", "run_id", "status", "choices", "reasons"];

const connectionBanner = document.querySelector("#connection-banner");
const announcer = document.querySelector("#announcer");
const workspaceDialog = document.querySelector("#workspace-dialog");
let currentView;
let lastSession;
let lastRevision = -1;
let lastDigest;
let intentBusy = false;
let rejectedBinding;
let refreshTimer;

const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[character]);

const message = (group, key, fallback = key) => MESSAGE_CATALOG[group]?.[key] ?? fallback;
const operatorToken = () => document.querySelector('meta[name="operator-token"]')?.content.trim() ?? "";
const tokenHeaders = () => {
  const token = operatorToken();
  if (!token) throw new Error("OPERATOR_TOKEN_MISSING");
  return {"X-Operator-Token": token};
};

function assertEnum(name, value) {
  if (!ENUMS[name].has(value)) throw new TypeError(`UNKNOWN_VIEW_ENUM:${name}:${value}`);
}

function assertExactObject(value, keys, code) {
  if (!value || typeof value !== "object" || Array.isArray(value)
      || Object.keys(value).length !== keys.length || keys.some((key) => !Object.hasOwn(value, key))) {
    throw new TypeError(code);
  }
}

function assertStringList(value, code) {
  if (!Array.isArray(value) || !value.length || value.some((item) => typeof item !== "string" || !item)) throw new TypeError(code);
}

function validateGoalTwoProjection(view) {
  if (view.setup !== undefined) {
    assertExactObject(view.setup, SETUP_KEYS, "SETUP_INVALID");
    if (typeof view.setup.host_status !== "string" || !view.setup.host_status
        || typeof view.setup.operator_label !== "string" || !view.setup.operator_label
        || !Array.isArray(view.setup.subsystems) || !view.setup.subsystems.length) throw new TypeError("SETUP_INVALID");
    view.setup.subsystems.forEach((row) => {
      assertExactObject(row, SUBSYSTEM_KEYS, "SETUP_SUBSYSTEM_INVALID");
      if (SUBSYSTEM_KEYS.some((key) => typeof row[key] !== "string" || !row[key])) throw new TypeError("SETUP_SUBSYSTEM_INVALID");
    });
  }
  if (view.operator_checkpoint !== undefined && view.operator_checkpoint !== null) {
    assertExactObject(view.operator_checkpoint, CHECKPOINT_KEYS, "OPERATOR_CHECKPOINT_INVALID");
    const checkpoint = view.operator_checkpoint;
    if (typeof checkpoint.kind !== "string" || !checkpoint.kind || typeof checkpoint.prompt !== "string" || !checkpoint.prompt
        || !DIGEST_PATTERN.test(checkpoint.binding_digest)) throw new TypeError("OPERATOR_CHECKPOINT_INVALID");
    assertStringList(checkpoint.choices, "OPERATOR_CHECKPOINT_INVALID");
    if (!checkpoint.evidence || typeof checkpoint.evidence !== "object" || Array.isArray(checkpoint.evidence)) throw new TypeError("OPERATOR_CHECKPOINT_INVALID");
    if (new Set(checkpoint.choices).size !== checkpoint.choices.length) throw new TypeError("OPERATOR_CHECKPOINT_INVALID");
  }
  if (view.candidate_review !== undefined && view.candidate_review !== null) {
    assertExactObject(view.candidate_review, CANDIDATE_REVIEW_KEYS, "CANDIDATE_REVIEW_INVALID");
    const review = view.candidate_review;
    if (!DIGEST_PATTERN.test(review.review_binding_digest) || typeof review.run_id !== "string" || !review.run_id) throw new TypeError("CANDIDATE_REVIEW_INVALID");
    assertEnum("candidate_status", review.status);
    assertStringList(review.choices, "CANDIDATE_REVIEW_INVALID");
    assertStringList(review.reasons, "CANDIDATE_REVIEW_INVALID");
    if (review.choices.length !== 3 || !["PASS", "FAIL", "UNCERTAIN"].every((choice) => review.choices.includes(choice))) throw new TypeError("CANDIDATE_REVIEW_INVALID");
  }
}

function unwrapViewEnvelope(value) {
  if (!value?.projection) return value;
  const fields = new Set(Object.keys(value));
  const expected = ["schema_version", "session_id", "revision", "projection", "generated_at", "view_digest", "authority"];
  if (fields.size !== expected.length || expected.some((field) => !fields.has(field))) throw new TypeError("VIEW_ENVELOPE_INVALID");
  if (!value.projection || typeof value.projection !== "object" || Array.isArray(value.projection)) throw new TypeError("VIEW_PROJECTION_INVALID");
  return {
    ...value.projection,
    schema_version: value.schema_version,
    session_id: value.session_id,
    revision: value.revision,
    generated_at: value.generated_at,
    view_digest: value.view_digest,
  };
}

function validateView(value) {
  const view = unwrapViewEnvelope(value);
  if (!view || view.schema_version !== VIEW_SCHEMA) throw new TypeError("VIEW_SCHEMA_MISMATCH");
  if (!view.session_id || !Number.isInteger(view.revision) || !DIGEST_PATTERN.test(view.view_digest)) throw new TypeError("VIEW_IDENTITY_INVALID");
  assertEnum("connection_state", view.connection_state);
  assertEnum("effect_scope", view.effect_scope);
  assertEnum("lifecycle_action", view.lifecycle_action);
  assertEnum("workflow_state", view.runtime?.workflow_state);
  assertEnum("authoring_mode", view.draft?.authoring_mode);
  assertEnum("selector", view.draft?.selector);
  if (view.data_disposition !== "TEST_ONLY") throw new TypeError("DATA_DISPOSITION_NOT_TEST_ONLY");
  if (!Array.isArray(view.available_ops) || !Array.isArray(view.draft.cells) || !Array.isArray(view.capabilities)) throw new TypeError("VIEW_COLLECTION_INVALID");
  validateGoalTwoProjection(view);
  if (lastSession === view.session_id && (view.revision < lastRevision || (view.revision === lastRevision && lastDigest && lastDigest !== view.view_digest))) {
    throw new TypeError("VIEW_REVISION_ROLLBACK");
  }
  if (rejectedBinding) {
    const sameRejectedView = rejectedBinding.session_id === view.session_id && rejectedBinding.revision === view.revision && rejectedBinding.digest === view.view_digest;
    if (sameRejectedView) throw new TypeError("REJECTED_VIEW_NOT_ADVANCED");
    rejectedBinding = undefined;
  }
  return view;
}

function canIntent(op) {
  return Boolean(currentView && currentView.connection_state === "READY" && !intentBusy && currentView.available_ops.includes(op));
}

function setBanner(text, tone = "info", announce = true) {
  connectionBanner.className = `connection-banner ${tone}`;
  connectionBanner.textContent = text;
  if (announce) announcer.textContent = text;
}

function failClose(code, detail = "") {
  clearTimeout(refreshTimer);
  refreshTimer = undefined;
  currentView = undefined;
  document.body.dataset.bridge = "blocked";
  document.querySelector("#connection-dot").className = "connection-dot blocked";
  document.querySelector("#connection-label").textContent = "작업 차단";
  document.querySelector("#session-label").textContent = code;
  document.querySelectorAll("button:not(.icon-button), input, select").forEach((control) => { control.disabled = true; });
  const reason = message("reason", code, code);
  setBanner(`${reason}. ${detail || "새 intent를 보내지 않습니다. 최신 화면을 다시 읽으세요."}`, "bad");
  document.querySelector("#runtime-content").innerHTML = `<p class="runtime-state bad"><strong>${escapeHtml(code)}</strong><span>bridge unavailable/stale/unknown 상태에서는 모든 campaign intent가 0입니다.</span></p>`;
  document.querySelector("#intent-buttons").innerHTML = '<button id="retry-view" type="button">최신 화면 다시 읽기</button>';
  document.querySelector("#retry-view").disabled = false;
  document.querySelector("#retry-view").addEventListener("click", () => {
    rejectedBinding = undefined;
    loadView();
  }, {once: true});
}

function renderConnection(view) {
  const ready = view.connection_state === "READY";
  document.body.dataset.bridge = ready ? "ready" : "blocked";
  document.querySelector("#connection-dot").className = `connection-dot ${ready ? "ready" : "blocked"}`;
  document.querySelector("#connection-label").textContent = message("status", view.connection_state);
  document.querySelector("#session-label").textContent = `${view.session_id} · view r${view.revision}`;
  setBanner(
    ready ? "로컬 브리지의 최신 revision과 digest에 연결되었습니다." : `${message("status", view.connection_state)}. intent는 보내지 않습니다.`,
    ready ? "good" : "bad",
    false,
  );
}

function renderScopes(view) {
  for (const control of document.querySelectorAll('[name="effect_scope"]')) {
    control.checked = control.value === view.effect_scope;
    control.disabled = !canIntent("set_effect_scope");
  }
  for (const control of document.querySelectorAll('[name="lifecycle_action"]')) {
    control.checked = control.value === view.lifecycle_action;
    control.disabled = !canIntent("set_lifecycle_action");
  }
  for (const control of document.querySelectorAll('[name="authoring_mode"]')) {
    control.checked = control.value === view.draft.authoring_mode;
    control.disabled = !canIntent("update_draft");
  }
  document.querySelector("#budget-input").value = view.draft.budget;
  document.querySelector("#budget-input").disabled = !canIntent("update_draft");
  document.querySelector("#apply-budget").disabled = !canIntent("update_draft");
}

function renderSetup(view) {
  const panel = document.querySelector("#setup-panel");
  if (!view.setup) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  document.querySelector("#setup-summary").innerHTML = `<strong>${escapeHtml(view.setup.host_status)}</strong><span>${escapeHtml(view.setup.operator_label)} · local label, authenticated HUMAN 아님</span>`;
  document.querySelector("#setup-subsystems").innerHTML = view.setup.subsystems.map((row) => `
    <div><dt>${escapeHtml(row.label)}</dt><dd><strong>${escapeHtml(row.status)}</strong><span>${escapeHtml(row.detail)}</span></dd></div>`).join("");
}

function renderWorkspace(view) {
  const workspace = view.fixed_lane.workspace;
  document.querySelector("#workspace-meta").innerHTML = `
    <div><span>작업영역</span><strong>${escapeHtml(workspace.display_name)}</strong><code>${escapeHtml(workspace.place_id)}@${escapeHtml(workspace.revision)}</code></div>
    <div><span>좌표</span><strong>X / Y / yaw</strong><code>${escapeHtml(workspace.bounds)}</code></div>
    <div><span>선택기</span><strong>${escapeHtml(view.draft.selector)}</strong><code>${escapeHtml(view.draft.selector_version)}</code></div>`;

  document.querySelector("#cell-grid").innerHTML = view.draft.cells.map((cell) => {
    const selected = ["SELECTED", "PINNED"].includes(cell.selection_state);
    const blocked = cell.eligibility_status === "BLOCKED";
    const reason = cell.reason_codes.join(", ");
    const label = `X ${cell.x_mm} mm, Y ${cell.y_mm} mm, yaw ${cell.yaw_deg}도, ${cell.split}, 반복 ${cell.repeat}, coverage ${cell.coverage_count}, ${cell.selection_state}, 이유 ${reason}`;
    return `<button type="button" class="cell ${selected ? "selected" : ""} ${blocked ? "blocked" : ""}" data-cell-id="${escapeHtml(cell.cell_id)}"
      aria-pressed="${selected}" aria-label="${escapeHtml(label)}" ${blocked || !canIntent("update_draft") ? "disabled" : ""}>
      <span class="cell-coordinate">X ${escapeHtml(cell.x_mm)} · Y ${escapeHtml(cell.y_mm)}</span>
      <strong>${escapeHtml(cell.yaw_deg)}°</strong>
      <span>${escapeHtml(cell.split)} · ×${escapeHtml(cell.repeat)}</span>
      <small>coverage ${escapeHtml(cell.coverage_count)}</small>
      <code>${escapeHtml(reason)}</code>
    </button>`;
  }).join("");
}

function renderSummary(view) {
  document.querySelector("#campaign-id").textContent = `${view.draft.draft_id} · r${view.draft.revision}`;
  document.querySelector("#draft-summary").innerHTML = `
    <div><strong>${escapeHtml(view.draft.selected_count)}</strong><span>선택</span></div>
    <div><strong>${escapeHtml(view.draft.budget)}</strong><span>목표 횟수</span></div>
    <div><strong>${escapeHtml(view.draft.blocked_count)}</strong><span>차단</span></div>
    <div><strong>${escapeHtml(view.draft.estimated_minutes)}분</strong><span>예상</span></div>`;
}

function renderInspector(view) {
  const lane = view.fixed_lane;
  const rows = [
    ["workspace/place", `${lane.workspace.display_name} · ${lane.workspace.place_id}@${lane.workspace.revision}`],
    ["object", lane.object_id], ["grasp", lane.grasp_id],
    ["task", `${lane.task.id} · ${lane.task.capability}`],
    ["motion", `${lane.motion.id} · ${lane.motion.capability}`],
    ["start", lane.start_pose_id], ["camera/profile", `${lane.camera_role} · ${lane.profile_id}`],
    ["split / repeat", `${view.draft.split_summary} · ${view.draft.repeat_summary}`],
    ["coverage / selector", `${view.draft.coverage_summary} · ${view.draft.selector}`],
  ];
  document.querySelector("#lane-details").innerHTML = rows.map(([term, value]) => `<div><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");

  document.querySelector("#capability-list").innerHTML = view.capabilities.map((item) => `
    <li><div><strong>${escapeHtml(item.label)}</strong><span class="capability ${escapeHtml(item.status.toLowerCase())}">${escapeHtml(item.status)}</span></div>
      <p>${escapeHtml(message("capability", item.status))}</p>
      <code>${escapeHtml(item.reason_codes.join(" · "))}</code></li>`).join("");

  document.querySelector("#effect-counts").innerHTML = Object.entries(view.effect_counts).map(([name, count]) => `
    <div><dt>${escapeHtml(name)}</dt><dd>${escapeHtml(count)}</dd></div>`).join("");
}

function renderEpisodeResult(result, view) {
  if (result == null) return "";
  if (!result || typeof result !== "object" || Array.isArray(result)
      || typeof result.outcome !== "string" || typeof result.code !== "string"
      || typeof result.human_semantic !== "string" || !DIGEST_PATTERN.test(result.result_digest)) {
    throw new TypeError("EPISODE_RESULT_INVALID");
  }
  const technical = result.technical_evidence?.status ?? "NOT_AVAILABLE";
  let rows = `<div><dt>technical</dt><dd>${escapeHtml(technical)}</dd></div>
    <div><dt>human semantic</dt><dd>${escapeHtml(result.human_semantic)}</dd></div>`;
  if (result.synthetic_review) {
    rows += `<div><dt>synthetic review</dt><dd>${escapeHtml(result.synthetic_review.reviewed_by)} · ${escapeHtml(result.synthetic_review.review_source)}</dd></div>`;
  }
  if (result.synthetic_coverage_update) {
    rows += `<div><dt>synthetic coverage</dt><dd>${escapeHtml(result.synthetic_coverage_update.source)} · +${escapeHtml(result.synthetic_coverage_update.synthetic_review_delta)} · production +${escapeHtml(result.synthetic_coverage_update.production_coverage_delta)}</dd></div>`;
  }
  if (view.data_disposition === "TEST_ONLY" && view.candidate_review == null) {
    rows += "<div><dt>candidate review</dt><dd>NOT_APPLICABLE (TEST_ONLY)</dd></div>";
  }
  return `<div class="approval-card result-card" aria-label="마지막 TEST_ONLY 결과"><p><strong>${escapeHtml(result.outcome)} · ${escapeHtml(result.code)}</strong></p><dl>${rows}</dl><code>${escapeHtml(result.result_digest)}</code></div>`;
}

function renderOperatorCheckpoint(view) {
  const checkpoint = view.operator_checkpoint;
  if (checkpoint == null) return "";
  const choices = canIntent("resolve_checkpoint") ? checkpoint.choices.map((choice) => `
    <button type="button" data-checkpoint-choice="${escapeHtml(choice)}">${escapeHtml(message("choice", choice))}</button>`).join("") : "";
  return `<section class="checkpoint-card" aria-labelledby="checkpoint-title">
    <p class="eyebrow">OPERATOR CHECKPOINT · ${escapeHtml(checkpoint.kind)}</p>
    <h4 id="checkpoint-title">${escapeHtml(checkpoint.prompt)}</h4>
    <dl class="checkpoint-evidence">${Object.entries(checkpoint.evidence).map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(typeof value === "object" ? JSON.stringify(value) : value)}</dd></div>`).join("")}</dl>
    ${choices ? `<div class="checkpoint-actions">${choices}</div>` : ""}
  </section>`;
}

function renderCandidateReview(view) {
  const review = view.candidate_review;
  if (review == null) return "";
  const pending = review.status === "PENDING" && canIntent("review_candidate");
  const reason = pending ? `<label class="review-reason" for="candidate-reason">FAIL / UNCERTAIN 사유
    <select id="candidate-reason" required><option value="">사유 선택</option>${review.reasons.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(message("review_reason", item))}</option>`).join("")}</select></label>` : "";
  const choices = pending ? review.choices.map((choice) => `
    <button type="button" data-review-choice="${escapeHtml(choice)}">${escapeHtml(message("candidate_choice", choice))}</button>`).join("") : "";
  return `<section class="candidate-review-card" aria-labelledby="candidate-review-title">
    <p class="eyebrow">CANDIDATE REVIEW · TRAINING APPROVAL 아님</p>
    <h4 id="candidate-review-title">후보 실행 ${escapeHtml(review.run_id)}</h4>
    <p><strong>${escapeHtml(review.status)}</strong> · 실행 후 기술 근거를 검토합니다. 이 결정은 학습 승인을 만들지 않습니다.</p>
    ${reason}${choices ? `<div class="checkpoint-actions">${choices}</div>` : ""}
  </section>`;
}

function renderRuntime(view) {
  const runtime = view.runtime;
  const reason = runtime.reason_codes?.[0];
  const status = message("status", reason, message("status", runtime.workflow_state));
  const detail = message("workflow", reason, message("workflow", runtime.workflow_state));
  let body = `<p class="runtime-state"><strong>${escapeHtml(status)}</strong><span>${escapeHtml(detail)}</span></p>`;
  if (runtime.reason_codes?.length) body += `<p class="reason-line"><span>reason</span><code>${escapeHtml(runtime.reason_codes.join(" · "))}</code></p>`;
  if (runtime.workflow_state === "AWAITING_APPROVAL") {
    if (!DIGEST_PATTERN.test(view.approval?.plan_digest)) throw new TypeError("APPROVAL_DIGEST_INVALID");
    const mechanicalProxy = view.approval.approval_scope === "HIL_NUMERIC_PROXY";
    const summary = view.approval.operator_summary;
    const pathText = Array.isArray(summary?.path)
      ? `${summary.path.length} phases · ${summary.path.join(" → ")}` : "NOT_AVAILABLE";
    const flowText = summary?.flow && typeof summary.flow === "object"
      ? `${summary.flow.continuous_through ?? "NOT_AVAILABLE"}까지 연속 · 다음 hold ${summary.flow.next_human_hold ?? "NOT_AVAILABLE"}`
      : "NOT_AVAILABLE";
    const clearanceText = summary?.clearance && typeof summary.clearance === "object"
      ? `${summary.clearance.status ?? "NOT_AVAILABLE"} · ${summary.clearance.collision_report_digest ?? "NOT_AVAILABLE"}`
      : "NOT_AVAILABLE";
    const speedText = summary?.speed && typeof summary.speed === "object"
      ? `velocity ${summary.speed.max_velocity_scaling ?? "NOT_AVAILABLE"} · acceleration ${summary.speed.max_acceleration_scaling ?? "NOT_AVAILABLE"}`
      : "NOT_AVAILABLE";
    const summaryRows = summary && typeof summary === "object" ? `
      <dl class="approval-evidence">
        <div><dt>full path</dt><dd>${escapeHtml(pathText)}</dd></div>
        <div><dt>flow</dt><dd>${escapeHtml(flowText)}</dd></div>
        <div><dt>clearance</dt><dd>${escapeHtml(clearanceText)}</dd></div>
        <div><dt>speed</dt><dd>${escapeHtml(speedText)}</dd></div>
      </dl>` : "";
    const checklist = view.approval.preapproval_checklist;
    const checklistRows = checklist && typeof checklist === "object" ? `
      <section class="site-checklist" aria-label="승인된 현장 checklist"><strong>현장 READY 확인 완료</strong><dl>
        ${Object.entries(checklist).map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(typeof value === "object" ? JSON.stringify(value) : value)}</dd></div>`).join("")}
      </dl><code>${escapeHtml(view.approval.site_confirmation_digest ?? "NOT_AVAILABLE")}</code></section>` : "";
    body += `<div class="approval-card"><p>이 화면과 계획에만 결속</p><code>${escapeHtml(view.approval.plan_digest)}</code>
      <dl><div><dt>scope</dt><dd>${escapeHtml(view.approval.approval_scope)}</dd></div><div><dt>paths</dt><dd>${escapeHtml(view.approval.test_only_paths)}</dd></div></dl>
      ${summaryRows}${checklistRows}
      <label class="sealed-toggle"><input type="checkbox" ${mechanicalProxy ? "checked" : ""} disabled> TEST_ONLY 기계적 그리퍼 판정 · ${mechanicalProxy ? "ON" : "OFF"} (이 계획에 고정)</label>
      <p><strong>버튼은 local operator checkpoint이며 신원 인증이 아닙니다.</strong> backend가 scene/start/expiry/replay를 다시 검증하고 single-use로 소비합니다.</p></div>`;
  }
  if (["RUNNING", "CANCELLING"].includes(runtime.workflow_state)) {
    const progress = runtime.progress;
    if (typeof progress !== "number" || !Number.isFinite(progress) || progress < 0 || progress > 100) throw new TypeError("PROGRESS_INVALID");
    body += `<div class="progress-block"><div><span>${escapeHtml(runtime.phase)}</span><strong>${escapeHtml(progress)}%</strong></div><progress max="100" value="${escapeHtml(progress)}">${escapeHtml(progress)}%</progress><p>${escapeHtml(runtime.detail)}</p></div>`;
  }
  body += renderEpisodeResult(view.episode_result, view);
  body += renderOperatorCheckpoint(view);
  body += renderCandidateReview(view);
  document.querySelector("#runtime-content").innerHTML = body;

  const buttons = [];
  if (canIntent("compile_draft")) buttons.push(`<button type="button" data-op="compile_draft">${message("action", "compile_draft")}</button>`);
  if (canIntent("approve_exact_plan")) buttons.push(`<button type="button" data-op="approve_exact_plan">${message("action", "approve_exact_plan")}</button>`);
  if (canIntent("reject_plan")) buttons.push(`<button class="danger-button" type="button" data-op="reject_plan">${message("action", "reject_plan")}</button>`);
  if (canIntent("cancel_session")) buttons.push(`<button class="danger-button" type="button" data-op="cancel_session">${message("action", "cancel_session")}</button>`);
  document.querySelector("#intent-buttons").innerHTML = buttons.join("");
}

function scheduleStatusRefresh(view) {
  clearTimeout(refreshTimer);
  refreshTimer = undefined;
  if (view.connection_state === "READY" && ["RUNNING", "CANCELLING"].includes(view.runtime.workflow_state)) {
    refreshTimer = setTimeout(loadView, 250);
  }
}

function renderWizard(view) {
  const wizard = view.workspace_wizard;
  const fakeOnly = view.effect_scope === "FAKE" && wizard.capability === "OFFLINE_ONLY";
  const measurementsEnabled = fakeOnly && canIntent("capture_workspace_point");
  document.querySelector("#open-workspace").disabled = !fakeOnly || !canIntent("capture_workspace_point");
  document.querySelector("#plane-reference").innerHTML = `<strong>${escapeHtml(wizard.plane_reference.id)}</strong><code>${escapeHtml(wizard.plane_reference.digest)}</code><span>table_normal_base ${escapeHtml(wizard.plane_reference.table_normal_base.join(", "))}</span>`;
  document.querySelector("#source-measurement").value = wizard.source_measurement_mm ?? "";
  document.querySelector("#final-measurement").value = wizard.final_measurement_mm ?? "";
  document.querySelector("#source-measurement").disabled = !measurementsEnabled;
  document.querySelector("#final-measurement").disabled = !measurementsEnabled;
  document.querySelector("#capture-buttons").innerHTML = ["CENTER", "X_REF", "Y_CHECK"].map((point) => `
    <button class="capture-button" type="button" data-capture="${point}" ${!fakeOnly || !canIntent("capture_workspace_point") ? "disabled" : ""}>
      <span>${point}</span><strong>${wizard.captures[point] ? "캡처됨" : "FAKE 캡처"}</strong></button>`).join("");
  const complete = ["CENTER", "X_REF", "Y_CHECK"].every((point) => wizard.captures[point]);
  document.querySelector("#save-workspace").disabled = !fakeOnly || !complete || !canIntent("save_workspace_revision");
}

function render(view) {
  currentView = view;
  lastSession = view.session_id;
  lastRevision = view.revision;
  lastDigest = view.view_digest;
  renderConnection(view);
  renderScopes(view);
  renderSetup(view);
  renderWorkspace(view);
  renderSummary(view);
  renderInspector(view);
  renderRuntime(view);
  renderWizard(view);
  scheduleStatusRefresh(view);
}

function intentPayload(op) {
  if (op === "compile_draft") return {draft_id: currentView.draft.draft_id, data_disposition: "TEST_ONLY"};
  if (["approve_exact_plan", "reject_plan"].includes(op)) return {
    plan_digest: currentView.approval.plan_digest,
    approval_scope: currentView.approval.approval_scope,
    data_disposition: "TEST_ONLY",
  };
  if (op === "cancel_session") return {active_child_id: currentView.runtime.active_child_id};
  return {};
}

async function submitIntent(op, payload = intentPayload(op)) {
  if (!canIntent(op)) return failClose("VIEW_STALE", `허용되지 않은 ${op} intent를 보내지 않았습니다.`);
  const boundView = currentView;
  intentBusy = true;
  render(boundView);
  const envelope = {
    schema_version: INTENT_SCHEMA,
    intent_id: crypto.randomUUID(),
    session_id: boundView.session_id,
    view_revision: boundView.revision,
    view_digest: boundView.view_digest,
    op,
    payload,
  };
  try {
    const response = await fetch("/api/intent", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {...tokenHeaders(), "Content-Type": "application/json"},
      body: JSON.stringify(envelope),
    });
    const result = await response.json();
    if (result.schema_version !== RESULT_SCHEMA || typeof result.ok !== "boolean" || typeof result.consumed !== "boolean" || result.ok !== result.consumed) {
      throw new TypeError("INTENT_RESULT_INVALID");
    }
    if (!response.ok || !result.ok) {
      const code = result.code || `HTTP_${response.status}`;
      rejectedBinding = {session_id: boundView.session_id, revision: boundView.revision, digest: boundView.view_digest};
      setBanner(`${message("reason", code, code)}. 소비되지 않았으며 자동 재시도하지 않습니다.`, "bad");
    }
  } catch (error) {
    intentBusy = false;
    failClose("BRIDGE_UNAVAILABLE", error.message);
    return;
  }
  await loadView({releaseIntent: true});
}

async function loadView({releaseIntent = false} = {}) {
  setBanner("최신 화면을 다시 읽고 있습니다. 이 동안 intent는 0입니다.", "info", false);
  try {
    const response = await fetch("/api/view", {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: tokenHeaders(),
    });
    if (!response.ok) throw new Error(`HTTP_${response.status}`);
    const view = validateView(await response.json());
    if (releaseIntent) intentBusy = false;
    render(view);
    if (view.connection_state !== "READY") announcer.textContent = `${message("status", view.connection_state)}. 모든 intent가 차단되었습니다.`;
  } catch (error) {
    if (releaseIntent) intentBusy = false;
    const stale = ["VIEW_REVISION_ROLLBACK", "REJECTED_VIEW_NOT_ADVANCED"].includes(error.message);
    const code = error.message.startsWith("UNKNOWN_VIEW_ENUM") ? "UNKNOWN_VIEW_ENUM" : stale ? "VIEW_STALE" : "BRIDGE_UNAVAILABLE";
    failClose(code, error.message);
  }
}

document.querySelector("#effect-scope").addEventListener("change", (event) => {
  if (event.target.name === "effect_scope") submitIntent("set_effect_scope", {effect_scope: event.target.value});
});
document.querySelector("#lifecycle-action").addEventListener("change", (event) => {
  if (event.target.name === "lifecycle_action") submitIntent("set_lifecycle_action", {lifecycle_action: event.target.value});
});
document.querySelector("#authoring-mode").addEventListener("change", (event) => {
  if (event.target.name === "authoring_mode") submitIntent("update_draft", {draft_id: currentView.draft.draft_id, authoring_mode: event.target.value});
});
document.querySelector("#apply-budget").addEventListener("click", () => {
  const budget = document.querySelector("#budget-input").valueAsNumber;
  if (!Number.isInteger(budget) || budget < 1) return document.querySelector("#budget-input").reportValidity();
  submitIntent("update_draft", {draft_id: currentView.draft.draft_id, budget});
});
document.querySelector("#cell-grid").addEventListener("click", (event) => {
  const button = event.target.closest("[data-cell-id]");
  if (button) submitIntent("update_draft", {draft_id: currentView.draft.draft_id, toggle_cell_id: button.dataset.cellId});
});
document.querySelector("#intent-buttons").addEventListener("click", (event) => {
  const button = event.target.closest("[data-op]");
  if (button) submitIntent(button.dataset.op);
});
document.querySelector("#runtime-content").addEventListener("click", (event) => {
  const checkpointButton = event.target.closest("[data-checkpoint-choice]");
  if (checkpointButton) {
    submitIntent("resolve_checkpoint", {
      checkpoint_binding_digest: currentView.operator_checkpoint.binding_digest,
      choice: checkpointButton.dataset.checkpointChoice,
    });
    return;
  }
  const reviewButton = event.target.closest("[data-review-choice]");
  if (!reviewButton) return;
  const choice = reviewButton.dataset.reviewChoice;
  const select = document.querySelector("#candidate-reason");
  const reason = choice === "PASS" ? null : select.value || null;
  if (choice !== "PASS" && !reason) return select.reportValidity();
  submitIntent("review_candidate", {
    review_binding_digest: currentView.candidate_review.review_binding_digest,
    choice,
    reason,
  });
});
document.querySelector("#open-workspace").addEventListener("click", () => workspaceDialog.showModal());
document.querySelector("#capture-buttons").addEventListener("click", (event) => {
  const button = event.target.closest("[data-capture]");
  if (!button || currentView.effect_scope !== "FAKE") return;
  submitIntent("capture_workspace_point", {
    draft_id: currentView.draft.draft_id,
    mode: "FAKE",
    point: button.dataset.capture,
    source_measurement_mm: document.querySelector("#source-measurement").valueAsNumber,
    final_measurement_mm: document.querySelector("#final-measurement").valueAsNumber,
    plane_reference_digest: currentView.workspace_wizard.plane_reference.digest,
  });
});
document.querySelector("#save-workspace").addEventListener("click", () => submitIntent("save_workspace_revision", {
  draft_id: currentView.draft.draft_id,
  mode: "FAKE",
  source_measurement_mm: document.querySelector("#source-measurement").valueAsNumber,
  final_measurement_mm: document.querySelector("#final-measurement").valueAsNumber,
}));
window.addEventListener("offline", () => failClose("BRIDGE_UNAVAILABLE", "브라우저가 offline입니다."));
window.addEventListener("online", () => {
  setBanner("재연결 후 최신 view를 확인합니다. 이전 intent는 다시 보내지 않습니다.", "info");
  loadView();
});

loadView();
