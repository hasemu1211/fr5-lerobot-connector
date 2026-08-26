const VIEW_SCHEMA = "data_factory.operator_session_view.v1";
const INTENT_SCHEMA = "data_factory.operator_intent.v1";
const RESULT_SCHEMA = "data_factory.operator_intent_result.v1";
const DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/;
const FLOW = ["environment", "plan", "review", "execution", "results", "next"];
const CATALOG_AXES = ["workspace", "frame", "task", "object", "grasp", "start", "motion", "variant", "camera", "data_mode", "split"];
const WORKSPACE_CAPTURE_ROLES = [
  ["CENTER", "중심점", "새 좌표계의 원점"],
  ["X_REF", "X축 기준점", "CENTER에서 +X 방향"],
  ["Y_CHECK", "Y축 확인점", "계산된 +Y 방향 확인"],
];
const ENUMS = {
  connection_state: new Set(["READY", "STALE", "RECONNECTING", "BLOCKED"]),
  effect_scope: new Set(["FAKE", "PHYSICAL"]),
  lifecycle_action: new Set(["AUTHOR_ONLY", "PLAN_ONLY", "LIVE_COLLECT"]),
  data_disposition: new Set(["TEST_ONLY", "PRODUCTION"]),
  authoring_mode: new Set(["ASSISTED", "DIRECT_EDIT"]),
  workflow_state: new Set(["PREPARING", "AUTHORING", "REVIEW_CAMPAIGN", "AWAITING_APPROVAL", "RUNNING", "CANCELLING", "PAUSED_AWAITING_OPERATOR", "REVIEW_RESULTS", "BLOCKED", "TERMINAL"]),
};

const connectionBanner = document.querySelector("#connection-banner");
const announcer = document.querySelector("#announcer");
const cancelButton = document.querySelector("#cancel-campaign");
let currentView;
let lastSession;
let lastRevision = -1;
let lastDigest;
let intentBusy = false;
let refreshTimer;
let manualStep;
let renderedWorkflow;

const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[character]);
const message = (group, key, fallback = "확인 필요") => MESSAGE_CATALOG[group]?.[key] ?? fallback;
const operatorToken = () => document.querySelector('meta[name="operator-token"]')?.content.trim() ?? "";
const tokenHeaders = () => {
  const token = operatorToken();
  if (!token) throw new Error("OPERATOR_TOKEN_MISSING");
  return {"X-Operator-Token": token};
};

function assertEnum(name, value) {
  if (!ENUMS[name].has(value)) throw new TypeError(`UNKNOWN_VIEW_ENUM:${name}:${value}`);
}

function assertObject(value, code) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError(code);
}

function validateCatalog(catalog, draft) {
  assertObject(catalog, "CATALOG_INVALID");
  assertObject(catalog.axes, "CATALOG_INVALID");
  if (!DIGEST_PATTERN.test(catalog.compatibility_digest)) throw new TypeError("CATALOG_INVALID");
  assertObject(draft.selection, "DRAFT_SELECTION_INVALID");
  CATALOG_AXES.forEach((axis) => {
    const options = catalog.axes[axis];
    if (!Array.isArray(options) || !options.length) throw new TypeError(`CATALOG_AXIS_INVALID:${axis}`);
    const ids = new Set();
    options.forEach((option) => {
      assertObject(option, `CATALOG_OPTION_INVALID:${axis}`);
      if (typeof option.id !== "string" || !option.id || typeof option.label !== "string" || !option.label
          || typeof option.available !== "boolean" || option.available === false && (typeof option.reason !== "string" || !option.reason)
          || ids.has(option.id)) throw new TypeError(`CATALOG_OPTION_INVALID:${axis}`);
      ids.add(option.id);
    });
    const selected = options.find((option) => option.id === draft.selection[axis]);
    if (!selected || !selected.available) throw new TypeError(`DRAFT_SELECTION_INVALID:${axis}`);
  });
}

function validateView(value) {
  const view = unwrapViewEnvelope(value);
  if (!view || view.schema_version !== VIEW_SCHEMA) throw new TypeError("VIEW_SCHEMA_MISMATCH");
  if (typeof view.session_id !== "string" || !view.session_id || !Number.isInteger(view.revision) || view.revision < 0
      || !DIGEST_PATTERN.test(view.view_digest)) throw new TypeError("VIEW_IDENTITY_INVALID");
  assertEnum("connection_state", view.connection_state);
  assertEnum("effect_scope", view.effect_scope);
  assertEnum("lifecycle_action", view.lifecycle_action);
  assertEnum("data_disposition", view.data_disposition);
  assertObject(view.runtime, "RUNTIME_INVALID");
  assertEnum("workflow_state", view.runtime.workflow_state);
  assertObject(view.setup, "SETUP_INVALID");
  if (typeof view.setup.host_status !== "string" || !Array.isArray(view.setup.subsystems) || !view.setup.subsystems.length) throw new TypeError("SETUP_INVALID");
  view.setup.subsystems.forEach((item) => {
    assertObject(item, "SETUP_INVALID");
    if (typeof item.label !== "string" || !item.label || typeof item.status !== "string" || !item.status || typeof item.detail !== "string") throw new TypeError("SETUP_INVALID");
  });
  assertObject(view.draft, "DRAFT_INVALID");
  assertEnum("authoring_mode", view.draft.authoring_mode);
  if (typeof view.draft.draft_id !== "string" || !view.draft.draft_id || !Number.isInteger(view.draft.requested_count)
      || view.draft.requested_count < 1 || view.draft.requested_count > 100 || !Number.isInteger(view.draft.repeat)
      || view.draft.repeat < 1 || !Array.isArray(view.draft.cells)) throw new TypeError("DRAFT_INVALID");
  validateCatalog(view.catalog, view.draft);
  if (view.draft.direct_poses !== undefined && (!Array.isArray(view.draft.direct_poses)
      || view.draft.direct_poses.some((pose) => !pose || typeof pose !== "object" || Array.isArray(pose)
        || pose.place_id !== view.draft.selection.workspace
        || ![pose.x_mm, pose.y_mm, pose.yaw_deg].every(Number.isFinite)))) throw new TypeError("DIRECT_POSES_INVALID");
  if (view.draft.selection.data_mode !== view.data_disposition) throw new TypeError("DATA_MODE_MISMATCH");
  const cellIds = new Set();
  view.draft.cells.forEach((cell) => {
    assertObject(cell, "DRAFT_CELL_INVALID");
    if (typeof cell.cell_id !== "string" || !cell.cell_id || cellIds.has(cell.cell_id)
        || ![cell.x_mm, cell.y_mm, cell.yaw_deg].every(Number.isFinite)
        || !Number.isInteger(cell.repeat) || cell.repeat < 1 || !Number.isInteger(cell.coverage_count) || cell.coverage_count < 0
        || !["AVAILABLE", "SELECTED", "PINNED", "BLOCKED"].includes(cell.selection_state)
        || !["ELIGIBLE", "BLOCKED"].includes(cell.eligibility_status)
        || !Array.isArray(cell.reason_codes) || cell.reason_codes.some((code) => typeof code !== "string" || !code)) throw new TypeError("DRAFT_CELL_INVALID");
    cellIds.add(cell.cell_id);
  });
  if (!Array.isArray(view.available_ops) || view.available_ops.some((op) => typeof op !== "string" || !op)
      || new Set(view.available_ops).size !== view.available_ops.length) throw new TypeError("AVAILABLE_OPS_INVALID");
  if (view.runtime.workflow_state === "REVIEW_CAMPAIGN") {
    assertObject(view.campaign_envelope, "CAMPAIGN_ENVELOPE_INVALID");
    if (!DIGEST_PATTERN.test(view.campaign_envelope.manifest_digest) || !DIGEST_PATTERN.test(view.campaign_envelope.envelope_digest)
        || view.campaign_envelope.episode_count !== view.draft.requested_count) throw new TypeError("CAMPAIGN_ENVELOPE_INVALID");
  }
  if (view.runtime.workflow_state === "RUNNING" && !view.available_ops.includes("cancel_session")) throw new TypeError("RUNNING_CANCEL_UNAVAILABLE");
  if (view.episode_history !== undefined && (!Array.isArray(view.episode_history)
      || view.episode_history.some((item) => !item || typeof item !== "object" || Array.isArray(item)
        || item.result_digest !== undefined && !DIGEST_PATTERN.test(item.result_digest)))) throw new TypeError("EPISODE_HISTORY_INVALID");
  const sequence = view.coverage?.sequence;
  if (sequence !== undefined && (!Array.isArray(sequence) || sequence.some((item, index) => !item
      || typeof item !== "object" || Array.isArray(item) || item.order_index !== index + 1
      || item.place_id !== view.draft.selection.workspace
      || ![item.x_mm, item.y_mm, item.yaw_deg].every(Number.isFinite)
      || !DIGEST_PATTERN.test(item.coverage_condition_digest)))) throw new TypeError("COVERAGE_SEQUENCE_INVALID");
  if (view.candidate_review !== undefined && view.candidate_review !== null) {
    assertObject(view.candidate_review, "CANDIDATE_REVIEW_INVALID");
    if (!DIGEST_PATTERN.test(view.candidate_review.review_binding_digest)
        || !["PENDING", "PASS", "FAIL", "UNCERTAIN"].includes(view.candidate_review.status)
        || !Array.isArray(view.candidate_review.choices) || !view.candidate_review.choices.every((choice) => ["PASS", "FAIL", "UNCERTAIN"].includes(choice))) throw new TypeError("CANDIDATE_REVIEW_INVALID");
  }
  validateWorkspaceRegistration(view.workspace_registration);
  if (view.runtime.progress !== undefined && (!Number.isFinite(view.runtime.progress) || view.runtime.progress < 0 || view.runtime.progress > 100)) throw new TypeError("RUNTIME_PROGRESS_INVALID");
  if (lastSession === view.session_id && (view.revision < lastRevision
      || view.revision === lastRevision && lastDigest && lastDigest !== view.view_digest)) throw new TypeError("VIEW_REVISION_ROLLBACK");
  return view;
}

function validateWorkspaceRegistration(workspace) {
  if (workspace === undefined || workspace === null) return;
  assertObject(workspace, "WORKSPACE_REGISTRATION_INVALID");
  if (typeof workspace.calibration_id !== "string" || !workspace.calibration_id) throw new TypeError("WORKSPACE_REGISTRATION_INVALID");
  assertObject(workspace.captures, "WORKSPACE_REGISTRATION_INVALID");
  const labels = WORKSPACE_CAPTURE_ROLES.map(([label]) => label);
  if (Object.keys(workspace.captures).length !== labels.length
      || labels.some((label) => typeof workspace.captures[label] !== "boolean")) throw new TypeError("WORKSPACE_REGISTRATION_INVALID");
  if (workspace.preview !== null) {
    assertObject(workspace.preview, "WORKSPACE_PREVIEW_INVALID");
    if (!["CANDIDATE_WITHIN_TOLERANCE", "CANDIDATE_OUT_OF_TOLERANCE"].includes(workspace.preview.status)
        || !DIGEST_PATTERN.test(workspace.preview.preview_digest)) throw new TypeError("WORKSPACE_PREVIEW_INVALID");
  }
  if (workspace.promotion !== null) {
    assertObject(workspace.promotion, "WORKSPACE_PROMOTION_INVALID");
    if (typeof workspace.promotion.calibration_id !== "string" || !workspace.promotion.calibration_id) throw new TypeError("WORKSPACE_PROMOTION_INVALID");
  }
  if (!Array.isArray(workspace.history) || workspace.execution_authorized !== false || workspace.training_approved !== false) throw new TypeError("WORKSPACE_REGISTRATION_INVALID");
}

function unwrapViewEnvelope(value) {
  if (!value?.projection) return value;
  const expected = ["schema_version", "session_id", "revision", "projection", "generated_at", "view_digest", "authority"];
  if (Object.keys(value).length !== expected.length || expected.some((field) => !Object.hasOwn(value, field))) throw new TypeError("VIEW_ENVELOPE_INVALID");
  assertObject(value.projection, "VIEW_PROJECTION_INVALID");
  return {...value.projection, schema_version: value.schema_version, session_id: value.session_id, revision: value.revision, generated_at: value.generated_at, view_digest: value.view_digest};
}

function canIntent(op) {
  return Boolean(currentView && currentView.connection_state === "READY" && !intentBusy && currentView.available_ops.includes(op));
}

function setBanner(text, tone = "info", announce = true) {
  connectionBanner.className = `connection-banner ${tone}`;
  connectionBanner.textContent = text;
  if (announce) announcer.textContent = text;
}

function humanReason(code) {
  return message("reason", code, "현재 조합을 사용할 수 없습니다");
}

function renderTechnical(rows) {
  document.querySelector("#technical-content").innerHTML = Object.entries(rows)
    .filter(([, value]) => value !== undefined && value !== null)
    .map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd><code>${escapeHtml(typeof value === "object" ? JSON.stringify(value) : value)}</code></dd></div>`).join("");
}

function failClose(code, detail = "") {
  clearTimeout(refreshTimer);
  refreshTimer = undefined;
  currentView = undefined;
  document.body.dataset.bridge = "blocked";
  document.querySelector("#connection-dot").className = "connection-dot blocked";
  document.querySelector("#connection-label").textContent = "복구 필요";
  document.querySelector("#session-label").textContent = "최신 상태를 다시 확인하세요";
  document.querySelectorAll("button, input, select").forEach((control) => { control.disabled = true; });
  if (document.querySelector("#workspace-dialog").open) document.querySelector("#workspace-dialog").close();
  cancelButton.hidden = true;
  setBanner(`${humanReason(code)}. 요청을 보내지 않았습니다. 최신 상태를 다시 불러오세요.`, "bad");
  document.querySelectorAll(".flow-step").forEach((section) => { section.hidden = section.dataset.step !== "execution"; });
  document.querySelector("#runtime-content").innerHTML = `<div class="recovery-card"><strong>실행하지 않았습니다</strong><p>화면을 새로 읽은 뒤 현재 조건과 진행 상황을 다시 확인하세요.</p><button id="retry-view" type="button">최신 상태 다시 불러오기</button></div>`;
  const retry = document.querySelector("#retry-view");
  retry.disabled = false;
  retry.addEventListener("click", () => loadView(), {once: true});
  renderTechnical({error_code: code, detail});
}

function setupReady(view) {
  return ["READY", "READY_WITH_EXCEPTION"].includes(view.setup.host_status);
}

function workflowStep(view) {
  const state = view.runtime.workflow_state;
  if (state === "PREPARING" || !setupReady(view)) return "environment";
  if (state === "AUTHORING") return "plan";
  if (["REVIEW_CAMPAIGN", "AWAITING_APPROVAL"].includes(state)) return "review";
  if (["RUNNING", "CANCELLING", "PAUSED_AWAITING_OPERATOR", "BLOCKED"].includes(state)) return "execution";
  if (state === "REVIEW_RESULTS"
      || (state === "TERMINAL" && view.episode_history?.length)
      || view.candidate_review?.status === "PENDING") return "results";
  return "next";
}

function unlockedSteps(view) {
  const unlocked = new Set(["environment"]);
  if (setupReady(view)) unlocked.add("plan");
  if (view.campaign_envelope) unlocked.add("review");
  if (view.campaign_authorization || ["RUNNING", "CANCELLING", "PAUSED_AWAITING_OPERATOR", "BLOCKED", "REVIEW_RESULTS", "TERMINAL"].includes(view.runtime.workflow_state)) unlocked.add("execution");
  if (view.episode_history?.length || ["REVIEW_RESULTS", "TERMINAL"].includes(view.runtime.workflow_state)) unlocked.add("results");
  if (view.runtime.workflow_state === "TERMINAL") unlocked.add("next");
  return unlocked;
}

function showStep(name, focus = false) {
  if (!currentView || !unlockedSteps(currentView).has(name)) return;
  document.querySelectorAll(".flow-step").forEach((section) => { section.hidden = section.dataset.step !== name; });
  document.querySelectorAll("[data-step-target]").forEach((button) => {
    const active = button.dataset.stepTarget === name;
    if (active) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
    button.classList.toggle("active", active);
  });
  manualStep = name;
  if (focus) document.querySelector(`#step-${name}`).focus({preventScroll: true});
}

function renderSteps(view) {
  const unlocked = unlockedSteps(view);
  document.querySelectorAll("[data-step-target]").forEach((button) => {
    button.disabled = !unlocked.has(button.dataset.stepTarget);
    button.closest("li").classList.toggle("complete", FLOW.indexOf(button.dataset.stepTarget) < FLOW.indexOf(workflowStep(view)));
  });
  if (renderedWorkflow !== view.runtime.workflow_state) manualStep = undefined;
  renderedWorkflow = view.runtime.workflow_state;
  showStep(manualStep && unlocked.has(manualStep) ? manualStep : workflowStep(view));
}

function renderConnection(view) {
  const ready = view.connection_state === "READY";
  document.body.dataset.bridge = ready ? "ready" : "blocked";
  document.querySelector("#connection-dot").className = `connection-dot ${ready ? "ready" : "blocked"}`;
  document.querySelector("#connection-label").textContent = message("connection", view.connection_state);
  document.querySelector("#session-label").textContent = ready ? "최신 상태가 연결되었습니다" : "새 요청은 차단됩니다";
  setBanner(ready ? "최신 수집 상태를 표시하고 있습니다." : `${message("connection", view.connection_state)}. 최신 상태 전에는 실행하지 않습니다.`, ready ? "good" : "bad", false);
}

function renderSetup(view) {
  document.querySelector("#setup-summary").innerHTML = `<strong>${escapeHtml(message("status", view.setup.host_status, view.setup.summary ?? "환경 상태 확인됨"))}</strong><span>${escapeHtml(view.setup.summary ?? "현재 장치 상태를 아래에서 확인하세요.")}</span>`;
  document.querySelector("#setup-subsystems").innerHTML = view.setup.subsystems.map((item) => {
    const label = message("status", item.status, message("reason", item.status, "확인 필요"));
    const tone = item.status === "READY" ? "ready" : item.status === "CONNECTING" ? "waiting" : "attention";
    return `<li><span class="device-state ${tone}" aria-hidden="true"></span><div><strong>${escapeHtml(item.label)}</strong><p>${escapeHtml(item.detail)}</p></div><b>${escapeHtml(label)}</b></li>`;
  }).join("");
  document.querySelector("#environment-next").disabled = !setupReady(view);
  document.querySelector("#prepare-environment").hidden = !canIntent("prepare_environment");
}

function catalogOption(view, axis, id) {
  return view.catalog.axes[axis].find((option) => option.id === id);
}

function directPoseDomain(view) {
  const domain = view.catalog.workspace_domain;
  const validBounds = (bounds) => bounds && Number.isFinite(bounds.minimum) && Number.isFinite(bounds.maximum) && bounds.minimum <= bounds.maximum;
  if (!domain || typeof domain !== "object" || Array.isArray(domain)
      || domain.workspace_id !== view.draft.selection.workspace || domain.frame_id !== view.draft.selection.frame
      || domain.coordinate_mode !== "CONTINUOUS_A4_PLANE" || !validBounds(domain.x_mm) || !validBounds(domain.y_mm)
      || !domain.yaw_deg || !Number.isFinite(domain.yaw_deg.minimum) || !Number.isFinite(domain.yaw_deg.maximum_exclusive)
      || domain.yaw_deg.minimum >= domain.yaw_deg.maximum_exclusive) return null;
  return domain;
}

function renderDirectPoseEditor(view, editable) {
  const editor = document.querySelector("#direct-pose-editor");
  const direct = view.draft.authoring_mode === "DIRECT_EDIT";
  const domain = directPoseDomain(view);
  const enabled = direct && editable && Boolean(domain);
  editor.hidden = !direct;
  [["#direct-x-input", domain?.x_mm], ["#direct-y-input", domain?.y_mm]].forEach(([selector, bounds]) => {
    const input = document.querySelector(selector);
    if (bounds) {
      input.min = bounds.minimum;
      input.max = bounds.maximum;
    } else {
      input.removeAttribute("min");
      input.removeAttribute("max");
    }
    input.disabled = !enabled;
  });
  document.querySelector("#direct-yaw-input").disabled = !enabled;
  const poses = view.draft.direct_poses ?? [];
  const anchor = view.draft.cells.find((cell) => ["SELECTED", "PINNED"].includes(cell.selection_state)) ?? view.draft.cells[0];
  const required = 1 + poses.length;
  document.querySelector("#add-direct-pose").disabled = !enabled || required >= view.draft.requested_count;
  document.querySelector("#direct-domain-status").textContent = !domain
    ? "현재 작업영역의 직접 입력 범위를 사용할 수 없습니다."
    : required > view.draft.requested_count
      ? `현재 ${required}개 자세가 필요합니다. 에피소드 수를 ${required}회 이상으로 늘리거나 자세를 삭제하세요.`
      : `현재 시작 위치를 첫 조건으로 두고, 아래 전체 조건 목록을 표시 순서대로 ${view.draft.requested_count}회까지 반복합니다. 입력 범위: X ${domain.x_mm.minimum}~${domain.x_mm.maximum} mm, Y ${domain.y_mm.minimum}~${domain.y_mm.maximum} mm.`;
  const anchorRow = `<li><span>1</span><strong>X ${escapeHtml(anchor?.x_mm ?? 0)} · Y ${escapeHtml(anchor?.y_mm ?? 0)} · ${escapeHtml(anchor?.yaw_deg ?? 0)}°</strong><em>현재 시작 위치</em></li>`;
  document.querySelector("#direct-pose-list").innerHTML = anchorRow + poses.map((pose, index) => `<li><span>${index + 2}</span><strong>X ${escapeHtml(pose.x_mm)} · Y ${escapeHtml(pose.y_mm)} · ${escapeHtml(pose.yaw_deg)}°</strong><button type="button" class="secondary-button" data-pose-index="${index}" aria-label="${index + 2}번째 직접 자세 삭제" ${enabled ? "" : "disabled"}>삭제</button></li>`).join("");
}

function renderCatalog(view) {
  const editable = view.runtime.workflow_state === "AUTHORING" && canIntent("update_draft");
  const direct = view.draft.authoring_mode === "DIRECT_EDIT";
  const domain = directPoseDomain(view);
  const anchor = view.draft.cells.find((cell) => ["SELECTED", "PINNED"].includes(cell.selection_state)) ?? view.draft.cells[0];
  const directPoses = view.draft.direct_poses ?? [];
  const samePose = (cell, pose) => Boolean(pose) && cell.x_mm === pose.x_mm && cell.y_mm === pose.y_mm && cell.yaw_deg === pose.yaw_deg;
  const directFull = 1 + directPoses.length >= view.draft.requested_count;
  CATALOG_AXES.forEach((axis) => {
    const select = document.querySelector(`[data-axis="${axis}"]`);
    const selected = view.draft.selection[axis];
    select.innerHTML = view.catalog.axes[axis].map((option) => {
      const unavailable = option.available ? "" : ` — ${humanReason(option.reason)}`;
      return `<option value="${escapeHtml(option.id)}" ${option.id === selected ? "selected" : ""} ${option.available ? "" : "disabled"}>${escapeHtml(option.label + unavailable)}</option>`;
    }).join("");
    select.disabled = !editable;
    const hint = document.querySelector(`[data-axis-hint="${axis}"]`);
    if (hint) hint.textContent = catalogOption(view, axis, selected)?.description ?? "";
  });
  document.querySelectorAll('[name="authoring_mode"]').forEach((control) => {
    control.checked = control.value === view.draft.authoring_mode;
    control.disabled = !editable;
  });
  document.querySelector("#count-input").value = view.draft.requested_count;
  document.querySelector("#repeat-input").value = view.draft.repeat;
  document.querySelector("#count-input").disabled = !editable;
  document.querySelector("#repeat-input").disabled = !editable || direct;
  document.querySelector("#repeat-input").closest("label").hidden = direct;
  document.querySelector("#workspace-domain-summary").textContent = domain
    ? `현재 작성 범위: X ${domain.x_mm.minimum}~${domain.x_mm.maximum} mm, Y ${domain.y_mm.minimum}~${domain.y_mm.maximum} mm, yaw ${domain.yaw_deg.minimum}° 이상 ${domain.yaw_deg.maximum_exclusive}° 미만. 등록된 셀은 빠른 기준점입니다.`
    : "현재 작업영역의 작성 범위를 사용할 수 없습니다.";
  const executionBlocked = view.draft.execution_ready === false;
  const executionReason = view.draft.execution_reason ?? view.catalog.selection_execution?.reason ?? view.draft.draft_reason;
  document.querySelector("#plan-lock-reason").textContent = executionBlocked
    ? humanReason(executionReason)
    : view.draft.draft_ready === false ? humanReason(view.draft.draft_reason)
    : editable ? "변경할 때마다 최신 조합을 다시 확인합니다." : "현재 단계에서는 계획을 변경할 수 없습니다.";
  document.querySelector("#compile-campaign").hidden = false;
  document.querySelector("#compile-campaign").disabled = !canIntent("compile_draft");

  renderDirectPoseEditor(view, editable);
  document.querySelector("#cell-grid").innerHTML = view.draft.cells.map((cell) => {
    const selected = direct
      ? samePose(cell, anchor) || directPoses.some((pose) => samePose(cell, pose))
      : ["SELECTED", "PINNED"].includes(cell.selection_state);
    const fixedAnchor = direct && samePose(cell, anchor);
    const available = cell.eligibility_status === "ELIGIBLE";
    const disabled = !editable || !direct || !available || fixedAnchor || !selected && directFull;
    const reason = available ? (selected ? "수집에 포함됨" : "선택 가능") : humanReason(cell.reason_codes?.[0]);
    const cellDetail = direct ? `${cell.split} · ${cell.repeat}회` : selected ? "현재 시작점" : "빠른 기준점";
    return `<button type="button" class="cell ${selected ? "selected" : ""} ${available ? "" : "blocked"}" data-cell-id="${escapeHtml(cell.cell_id)}" aria-pressed="${selected}" aria-label="X ${escapeHtml(cell.x_mm)} mm, Y ${escapeHtml(cell.y_mm)} mm, ${escapeHtml(cell.yaw_deg)}도, ${escapeHtml(reason)}" ${disabled ? "disabled" : ""}>
      <span>X ${escapeHtml(cell.x_mm)} · Y ${escapeHtml(cell.y_mm)}</span><strong>${escapeHtml(cell.yaw_deg)}°</strong><small>${escapeHtml(cellDetail)}</small><em>${escapeHtml(reason)}</em></button>`;
  }).join("");
}

function renderWorkspaceRegistration(view) {
  const workspace = view.runtime.workflow_state === "AUTHORING" ? view.workspace_registration : null;
  const entry = document.querySelector("#workspace-entry");
  const dialog = document.querySelector("#workspace-dialog");
  entry.hidden = !workspace;
  document.querySelector("#open-workspace").disabled = intentBusy;
  if (!workspace) {
    if (dialog.open) dialog.close();
    document.querySelector("#workspace-selection").innerHTML = "";
    document.querySelector("#workspace-captures").innerHTML = "";
    document.querySelector("#workspace-preview-form").hidden = true;
    document.querySelector("#workspace-preview-status").innerHTML = "";
    document.querySelector("#workspace-promotion").innerHTML = "";
    document.querySelector("#workspace-next-action").textContent = "";
    return;
  }

  document.querySelector("#workspace-selection").innerHTML = [
    ["선택 작업영역", `${selectedLabel(view, "workspace")} · ${view.draft.selection.workspace}`],
    ["선택 좌표계", `${selectedLabel(view, "frame")} · ${view.draft.selection.frame}`],
    ["새 좌표계", workspace.calibration_id],
  ].map(([term, value]) => `<div><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");

  const canCapture = canIntent("capture_workspace_point");
  document.querySelector("#workspace-captures").innerHTML = WORKSPACE_CAPTURE_ROLES.map(([label, title, detail]) => {
    const captured = workspace.captures[label];
    const action = canCapture ? `<button type="button" class="secondary-button" data-capture-label="${label}">${captured ? "다시 캡처" : "이 위치 캡처"}</button>` : "";
    return `<li data-capture-role="${label}"><span>${label}</span><div><strong>${title}</strong><p>${detail}</p></div><b>${captured ? "캡처됨" : "미캡처"}</b>${action}</li>`;
  }).join("");

  const allCaptured = WORKSPACE_CAPTURE_ROLES.every(([label]) => workspace.captures[label]);
  const previewForm = document.querySelector("#workspace-preview-form");
  const canPreview = canIntent("preview_workspace");
  previewForm.hidden = !allCaptured || Boolean(workspace.preview) || Boolean(workspace.promotion);
  previewForm.querySelectorAll("input").forEach((input) => { input.disabled = !canPreview; });
  document.querySelector("#preview-workspace").hidden = !canPreview;

  const previewStatus = document.querySelector("#workspace-preview-status");
  if (workspace.preview) {
    const withinTolerance = workspace.preview.status === "CANDIDATE_WITHIN_TOLERANCE";
    previewStatus.innerHTML = `<div class="notice ${withinTolerance ? "workspace-pass" : "workspace-fail"}"><strong>${withinTolerance ? "미리보기 통과" : "미리보기 저장 불가"}</strong><span>${withinTolerance ? "세 점과 두 실측값으로 계산한 좌표계가 허용 범위 안입니다." : "계산한 좌표계가 허용 범위를 벗어나 저장할 수 없습니다."}</span></div>${canIntent("save_workspace_revision") ? `<button type="button" data-workspace-op="save_workspace_revision">${message("action", "save_workspace_revision")}</button>` : ""}`;
  } else {
    previewStatus.innerHTML = "";
  }

  const promotion = document.querySelector("#workspace-promotion");
  promotion.innerHTML = workspace.promotion ? `<div class="notice workspace-pass"><strong>변경 불가능한 좌표계 개정본이 저장되었습니다.</strong><span>좌표계 카탈로그를 같은 프로세스에서 새로고침했습니다. 이 저장은 로봇 실행 권한이나 학습 승인을 만들지 않습니다.</span></div>${canIntent("new_workspace_registration") ? `<button type="button" data-workspace-op="new_workspace_registration">${message("action", "new_workspace_registration")}</button>` : ""}` : "";

  let nextAction;
  if (workspace.promotion) nextAction = canIntent("new_workspace_registration") ? "다음 작업: 수집 계획으로 돌아가거나 다른 좌표계를 등록합니다." : "다음 작업: 수집 계획으로 돌아갑니다.";
  else if (workspace.preview) nextAction = canIntent("save_workspace_revision") ? "다음 작업: 현재 미리보기로 좌표계를 저장합니다." : "다음 작업 없음: 현재 미리보기는 저장할 수 없습니다.";
  else if (allCaptured) nextAction = canPreview ? "다음 작업: 원본과 최종 100 mm 눈금을 실측해 미리보기를 만듭니다." : "다음 작업 없음: 현재 상태에서는 미리보기를 만들 수 없습니다.";
  else {
    const [label, title] = WORKSPACE_CAPTURE_ROLES.find(([role]) => !workspace.captures[role]);
    nextAction = canCapture ? `다음 작업: ${label} ${title}을 캡처합니다.` : `다음 작업 없음: ${label} 캡처 요청을 사용할 수 없습니다.`;
  }
  document.querySelector("#workspace-next-action").textContent = nextAction;
}

function selectedLabel(view, axis) {
  return catalogOption(view, axis, view.draft.selection[axis])?.label ?? "확인 필요";
}

function renderReview(view) {
  const direct = view.draft.authoring_mode === "DIRECT_EDIT";
  const plannedCells = view.coverage?.cells ?? [];
  const sequence = view.coverage?.sequence ?? [];
  const range = direct
    ? `${view.draft.requested_count}회 · ${plannedCells.length || 1 + (view.draft.direct_poses?.length ?? 0)}개 자세를 표시 순서로 실행 · ${selectedLabel(view, "split")}`
    : `${view.draft.requested_count}회 · 조건별 최대 ${view.draft.repeat}회 · ${selectedLabel(view, "split")}`;
  const rows = [
    ["작업영역", `${selectedLabel(view, "workspace")} · ${selectedLabel(view, "frame")}`],
    ["작업", selectedLabel(view, "task")],
    ["물체와 잡기", `${selectedLabel(view, "object")} · ${selectedLabel(view, "grasp")}`],
    ["로봇 동작", `${selectedLabel(view, "motion")} · ${selectedLabel(view, "variant")}`],
    ["시작 자세", selectedLabel(view, "start")],
    ["카메라", selectedLabel(view, "camera")],
    ["수집 범위", range],
    ["데이터 모드", selectedLabel(view, "data_mode")],
  ];
  if (view.campaign_review?.speed_limit) rows.push(["속도 상한", view.campaign_review.speed_limit]);
  document.querySelector("#review-summary").innerHTML = rows.map(([term, value]) => `<div><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
  const reviewPlan = document.querySelector("#review-plan");
  reviewPlan.hidden = sequence.length === 0;
  document.querySelector("#review-plan-list").innerHTML = sequence.map((item) => `<li><span>${escapeHtml(item.order_index)}</span><strong>X ${escapeHtml(item.x_mm)} · Y ${escapeHtml(item.y_mm)} · ${escapeHtml(item.yaw_deg)}°</strong></li>`).join("");
  document.querySelector("#review-actions").innerHTML = canIntent("authorize_campaign") ? `<button type="button" data-op="authorize_campaign">${message("action", "authorize_campaign")}</button>` : "";
  document.querySelector("#review-back").disabled = !canIntent("edit_campaign_draft");
}

function campaignStatus(view) {
  return view.campaign_session?.campaign ?? view.campaign_operator?.campaign ?? view.campaign_status ?? {};
}

function renderFacts(view) {
  const state = campaignStatus(view);
  const total = view.campaign_envelope?.episode_count ?? view.draft.requested_count;
  const completed = state.completed_intents ?? view.episode_history?.length ?? 0;
  const current = view.runtime.current_episode ?? (view.runtime.workflow_state === "RUNNING" ? completed + 1 : null);
  const next = view.runtime.next_episode ?? (current && current < total ? current + 1 : null);
  const recorder = view.runtime.recorder?.label ?? view.runtime.recorder?.status ?? "상태 확인 전";
  const facts = document.querySelector("#campaign-facts");
  facts.hidden = !view.campaign_envelope && !view.episode_history?.length;
  facts.innerHTML = `<div data-fact="completed"><span>완료</span><strong>${escapeHtml(completed)}/${escapeHtml(total)}</strong></div>
    <div data-fact="total"><span>전체</span><strong>${escapeHtml(total)}회</strong></div>
    <div data-fact="current"><span>현재</span><strong>${current ? `${escapeHtml(current)}/${escapeHtml(total)}` : "없음"}</strong></div>
    <div data-fact="next"><span>다음</span><strong>${next ? `${escapeHtml(next)}/${escapeHtml(total)}` : "없음"}</strong></div>
    <div data-fact="recorder"><span>기록기</span><strong>${escapeHtml(recorder)}</strong></div>`;
}

function renderRuntime(view) {
  const runtime = view.runtime;
  const stateText = message("workflow", runtime.workflow_state);
  const reason = runtime.reason_codes?.length ? humanReason(runtime.reason_codes[0]) : "";
  let html = `<div class="runtime-state"><span class="pulse" aria-hidden="true"></span><div><strong>${escapeHtml(stateText)}</strong>${reason ? `<p>${escapeHtml(reason)}</p>` : ""}</div></div>`;
  if (Number.isFinite(runtime.progress) && runtime.progress >= 0 && runtime.progress <= 100) {
    html += `<div class="progress-block"><div><span>${escapeHtml(runtime.phase_label ?? "현재 에피소드")}</span><strong>${escapeHtml(runtime.progress)}%</strong></div><progress max="100" value="${escapeHtml(runtime.progress)}">${escapeHtml(runtime.progress)}%</progress><p>${escapeHtml(runtime.detail ?? "")}</p></div>`;
  }
  if (runtime.recorder) {
    const recorderRows = [["상태", runtime.recorder.label ?? message("status", runtime.recorder.status)]];
    if (Number.isInteger(runtime.recorder.frames)) recorderRows.push(["기록 프레임", `${runtime.recorder.frames}`]);
    if (Number.isFinite(runtime.recorder.fps)) recorderRows.push(["관측 속도", `${runtime.recorder.fps} fps`]);
    html += `<dl class="runtime-evidence">${recorderRows.map(([term, value]) => `<div><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}</dl>`;
  }
  document.querySelector("#runtime-content").innerHTML = html;
  const showCancel = ["RUNNING", "CANCELLING", "PAUSED_AWAITING_OPERATOR"].includes(runtime.workflow_state);
  cancelButton.hidden = !showCancel;
  cancelButton.disabled = runtime.workflow_state === "CANCELLING" || !canIntent("cancel_session");
  cancelButton.textContent = runtime.workflow_state === "CANCELLING" ? "중단 처리 중" : message("action", "cancel_session");
}

function measurementLabel(value) {
  if (value === undefined || value === null || value === "") return "측정 자료 없음";
  return message("status", value, "확인 필요");
}

function semanticReviewLabel(value) {
  return {PASS: "작업 성공", FAIL: "작업 실패", UNCERTAIN: "판정 보류"}[value] ?? measurementLabel(value);
}

function episodeNoteLabel(item) {
  const semantic = item.human_semantic ?? item.episode_ledger?.review_status;
  return semantic === "NOT_MEASURED" ? message("status", semantic) : semanticReviewLabel(semantic);
}

function renderResults(view) {
  const history = view.episode_history ?? [];
  document.querySelector("#episode-results").innerHTML = history.length ? history.map((item, index) => {
    const technical = item.technical_evidence?.status ?? item.technical_status;
    return `<li><span>${index + 1}</span><div><strong>에피소드 ${index + 1}</strong><p>기술 검사 ${escapeHtml(measurementLabel(technical))}</p></div><b>${escapeHtml(episodeNoteLabel(item))}</b></li>`;
  }).join("") : "<li class=\"empty-result\">완료된 에피소드가 없습니다.</li>";

  const cells = view.coverage?.cells ?? view.draft.cells;
  document.querySelector("#coverage-summary").innerHTML = cells.map((cell) => {
    const count = Number.isInteger(cell.collected_count) ? cell.collected_count : Number.isInteger(cell.coverage_count) ? cell.coverage_count : 0;
    const target = Number.isInteger(cell.target_count) && cell.target_count > 0 ? cell.target_count : Math.max(view.draft.repeat, 1);
    return `<div><span>X ${escapeHtml(cell.x_mm)} · Y ${escapeHtml(cell.y_mm)} · ${escapeHtml(cell.yaw_deg)}°</span><progress max="${escapeHtml(target)}" value="${escapeHtml(Math.min(count, target))}">${escapeHtml(count)}/${escapeHtml(target)}</progress><strong>${escapeHtml(count)}/${escapeHtml(target)}</strong></div>`;
  }).join("");

  const review = view.candidate_review;
  if (!review) {
    const passed = history.filter((item) => (item.technical_evidence?.status ?? item.technical_status) === "PASS").length;
    document.querySelector("#review-queue").innerHTML = `<div class="notice"><strong>사후 검토를 수행하지 않았습니다.</strong><span>${escapeHtml(passed)}개 에피소드가 기술 검사를 통과했습니다. 보존 상태와 학습 사용 승인은 별도입니다.</span></div>`;
    return;
  }
  const pending = review.status === "PENDING" && canIntent("review_candidate");
  const reasons = Array.isArray(review.reasons) ? review.reasons : [];
  document.querySelector("#review-queue").innerHTML = `<section class="review-card" aria-labelledby="review-queue-title"><div><p class="step-number">작업 결과 검토</p><h3 id="review-queue-title">이 에피소드의 작업 결과를 판정하세요.</h3></div>
    ${pending ? `<label for="candidate-reason">실패 또는 보류 이유<select id="candidate-reason" required><option value="">이유 선택</option>${reasons.map((reason) => `<option value="${escapeHtml(reason)}">${escapeHtml(message("review_reason", reason))}</option>`).join("")}</select></label>
    <div class="admission-actions"><button type="button" data-review-choice="PASS">작업 성공</button><button type="button" data-review-choice="FAIL">작업 실패</button><button type="button" data-review-choice="UNCERTAIN">판정 보류</button></div>` : `<p>${escapeHtml(semanticReviewLabel(review.status))}</p>`}
    <p class="cell-help">이 판정은 작업 결과만 기록합니다. 데이터 보존 상태와 학습 사용 승인을 바꾸지 않습니다.</p></section>`;
}

function renderNext(view) {
  document.querySelector("#results-next").disabled = view.runtime.workflow_state !== "TERMINAL";
  document.querySelector("#same-settings-action").innerHTML = canIntent("new_campaign_same_settings") ? `<button type="button" data-op="new_campaign_same_settings">${message("action", "new_campaign_same_settings")}</button>` : "<p>현재 상태에서는 새 캠페인을 만들 수 없습니다.</p>";
}

function renderTechnicalDetails(view) {
  renderTechnical({
    session_id: view.session_id,
    view_revision: view.revision,
    view_digest: view.view_digest,
    draft_id: view.draft.draft_id,
    draft_revision: view.draft.revision,
    compatibility_digest: view.catalog.compatibility_digest,
    manifest_digest: view.campaign_envelope?.manifest_digest,
    envelope_digest: view.campaign_envelope?.envelope_digest,
    authorization_digest: view.campaign_authorization?.authorization_digest,
    workspace_preview_digest: view.workspace_registration?.preview?.preview_digest,
    workspace_promotion_digest: view.workspace_registration?.promotion?.promotion_digest,
    reason_codes: view.runtime.reason_codes,
    result_digests: view.episode_history?.map((item) => item.result_digest).filter(Boolean),
    ledger_digests: view.episode_history?.map((item) => item.episode_ledger?.ledger_digest).filter(Boolean),
    retention_states: view.episode_history?.map((item) => item.episode_ledger?.reclaim_state).filter(Boolean),
    effect_counts: view.effect_counts,
  });
}

function render(view) {
  currentView = view;
  lastSession = view.session_id;
  lastRevision = view.revision;
  lastDigest = view.view_digest;
  renderConnection(view);
  renderSetup(view);
  renderCatalog(view);
  renderWorkspaceRegistration(view);
  renderReview(view);
  renderFacts(view);
  renderRuntime(view);
  renderResults(view);
  renderNext(view);
  renderTechnicalDetails(view);
  renderSteps(view);
  scheduleStatusRefresh(view);
}

function scheduleStatusRefresh(view) {
  clearTimeout(refreshTimer);
  refreshTimer = undefined;
  if (view.connection_state === "READY" && ["PREPARING", "RUNNING", "CANCELLING"].includes(view.runtime.workflow_state)) refreshTimer = setTimeout(loadView, 500);
}

function intentPayload(op) {
  if (op === "compile_draft") return {draft_id: currentView.draft.draft_id, data_disposition: currentView.data_disposition};
  if (op === "authorize_campaign") return {
    draft_id: currentView.draft.draft_id,
    manifest_digest: currentView.campaign_envelope.manifest_digest,
    envelope_digest: currentView.campaign_envelope.envelope_digest,
    data_disposition: currentView.data_disposition,
  };
  if (op === "cancel_session") return {active_child_id: currentView.runtime.active_child_id};
  return currentView.next_campaign?.actions?.[op]?.payload ?? {};
}

async function submitIntent(op, payload = intentPayload(op)) {
  if (!canIntent(op)) return failClose("VIEW_STALE", "OP_NOT_AVAILABLE");
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
  let rejectionCode;
  try {
    const response = await fetch("/api/intent", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {...tokenHeaders(), "Content-Type": "application/json"},
      body: JSON.stringify(envelope),
    });
    const result = await response.json();
    if (result.schema_version !== RESULT_SCHEMA || typeof result.ok !== "boolean" || typeof result.consumed !== "boolean" || result.ok !== result.consumed) throw new TypeError("INTENT_RESULT_INVALID");
    if (!response.ok || !result.ok) {
      const code = typeof result.code === "string" ? result.code : "VERSION_CONFLICT";
      rejectionCode = code;
    }
  } catch (error) {
    intentBusy = false;
    failClose("BRIDGE_UNAVAILABLE", error.message);
    return;
  }
  await loadView({releaseIntent: true, rejectionCode});
}

async function loadView({releaseIntent = false, rejectionCode} = {}) {
  setBanner("최신 상태를 다시 읽고 있습니다. 이 동안 요청을 보내지 않습니다.", "info", false);
  try {
    const response = await fetch("/api/view", {method: "GET", credentials: "same-origin", cache: "no-store", headers: tokenHeaders()});
    if (!response.ok) throw new Error(`HTTP_${response.status}`);
    const view = validateView(await response.json());
    if (releaseIntent) intentBusy = false;
    if (view.connection_state !== "READY") {
      failClose(view.runtime.reason_codes?.[0] ?? "VIEW_STALE", view.connection_state);
      return;
    }
    render(view);
    if (rejectionCode) setBanner(`${humanReason(rejectionCode)}. 요청은 실행되지 않았습니다.`, "bad");
  } catch (error) {
    if (releaseIntent) intentBusy = false;
    const stale = error.message === "VIEW_REVISION_ROLLBACK";
    const code = error.message.startsWith("UNKNOWN_VIEW_ENUM") ? "VIEW_STALE" : stale ? error.message : error.message === "RUNNING_CANCEL_UNAVAILABLE" ? "VIEW_STALE" : "BRIDGE_UNAVAILABLE";
    failClose(code, error.message);
  }
}

document.querySelector(".step-rail").addEventListener("click", (event) => {
  const button = event.target.closest("[data-step-target]");
  if (button) showStep(button.dataset.stepTarget, true);
});
document.querySelector("#environment-next").addEventListener("click", () => showStep("plan", true));
document.querySelector("#prepare-environment").addEventListener("click", () => submitIntent("prepare_environment", {}));
document.querySelector("#open-workspace").addEventListener("click", () => {
  if (currentView?.runtime.workflow_state === "AUTHORING" && currentView.workspace_registration) document.querySelector("#workspace-dialog").showModal();
});
document.querySelector("#close-workspace").addEventListener("click", () => document.querySelector("#workspace-dialog").close());
document.querySelector("#workspace-captures").addEventListener("click", (event) => {
  const button = event.target.closest("[data-capture-label]");
  if (button && WORKSPACE_CAPTURE_ROLES.some(([label]) => label === button.dataset.captureLabel)) {
    submitIntent("capture_workspace_point", {label: button.dataset.captureLabel});
  }
});
document.querySelector("#workspace-preview-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const source = document.querySelector("#source-scale-bar").valueAsNumber;
  const final = document.querySelector("#final-scale-bar").valueAsNumber;
  if (!canIntent("preview_workspace") || !Number.isFinite(source) || !Number.isFinite(final) || !form.checkValidity()) return form.reportValidity();
  submitIntent("preview_workspace", {source_scale_bar_mm: source, final_scale_bar_mm: final});
});
document.querySelector("#workspace-preview-status").addEventListener("click", (event) => {
  const button = event.target.closest('[data-workspace-op="save_workspace_revision"]');
  const digest = currentView?.workspace_registration?.preview?.preview_digest;
  if (button && DIGEST_PATTERN.test(digest)) submitIntent("save_workspace_revision", {preview_digest: digest});
});
document.querySelector("#workspace-promotion").addEventListener("click", (event) => {
  if (event.target.closest('[data-workspace-op="new_workspace_registration"]')) submitIntent("new_workspace_registration", {});
});
document.querySelector("#plan-back").addEventListener("click", () => showStep("environment", true));
document.querySelector("#review-back").addEventListener("click", () => submitIntent("edit_campaign_draft", {}));
document.querySelector("#results-next").addEventListener("click", () => showStep("next", true));
document.querySelector("#catalog-fields").addEventListener("change", (event) => {
  if (event.target.matches("select[data-axis]")) submitIntent("update_draft", {draft_id: currentView.draft.draft_id, selection: {[event.target.dataset.axis]: event.target.value}});
});
document.querySelector("#authoring-mode").addEventListener("change", (event) => {
  if (event.target.name === "authoring_mode") submitIntent("update_draft", {draft_id: currentView.draft.draft_id, authoring_mode: event.target.value});
});
["count", "repeat"].forEach((field) => document.querySelector(`#${field}-input`).addEventListener("change", (event) => {
  const value = event.target.valueAsNumber;
  if (!Number.isInteger(value) || value < Number(event.target.min) || value > Number(event.target.max)) return event.target.reportValidity();
  submitIntent("update_draft", {draft_id: currentView.draft.draft_id, [field === "count" ? "requested_count" : "repeat"]: value});
}));
document.querySelector("#split-select").addEventListener("change", (event) => submitIntent("update_draft", {draft_id: currentView.draft.draft_id, split: event.target.value}));
document.querySelector("#cell-grid").addEventListener("click", (event) => {
  const button = event.target.closest("[data-cell-id]");
  const cell = button && currentView?.draft.cells.find((item) => item.cell_id === button.dataset.cellId);
  if (!cell || currentView.draft.authoring_mode !== "DIRECT_EDIT") return;
  const anchor = currentView.draft.cells.find((item) => ["SELECTED", "PINNED"].includes(item.selection_state)) ?? currentView.draft.cells[0];
  const samePose = (pose) => pose && ["x_mm", "y_mm", "yaw_deg"].every((field) => pose[field] === cell[field]);
  if (samePose(anchor)) return;
  const pose = currentView.draft.direct_poses?.find(samePose);
  submitIntent("update_draft", {
    draft_id: currentView.draft.draft_id,
    [pose ? "remove_pose" : "add_pose"]: pose ?? {
      place_id: currentView.draft.selection.workspace,
      x_mm: cell.x_mm, y_mm: cell.y_mm, yaw_deg: cell.yaw_deg,
    },
  });
});
document.querySelector("#direct-pose-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!currentView || !canIntent("update_draft") || !directPoseDomain(currentView)) return;
  const form = event.currentTarget;
  const values = ["#direct-x-input", "#direct-y-input", "#direct-yaw-input"].map((selector) => document.querySelector(selector).valueAsNumber);
  if (!values.every(Number.isFinite) || !form.checkValidity()) return form.reportValidity();
  submitIntent("update_draft", {
    draft_id: currentView.draft.draft_id,
    add_pose: {place_id: currentView.draft.selection.workspace, x_mm: values[0], y_mm: values[1], yaw_deg: values[2]},
  });
});
document.querySelector("#direct-pose-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-pose-index]");
  const pose = button && currentView?.draft.direct_poses?.[Number(button.dataset.poseIndex)];
  if (pose && canIntent("update_draft")) submitIntent("update_draft", {draft_id: currentView.draft.draft_id, remove_pose: pose});
});
document.querySelector("#compile-campaign").addEventListener("click", () => submitIntent("compile_draft"));
document.querySelector("#review-actions").addEventListener("click", (event) => {
  const button = event.target.closest("[data-op]");
  if (button) submitIntent(button.dataset.op);
});
cancelButton.addEventListener("click", () => submitIntent("cancel_session"));
document.querySelector("#review-queue").addEventListener("click", (event) => {
  const button = event.target.closest("[data-review-choice]");
  if (!button) return;
  const choice = button.dataset.reviewChoice;
  const select = document.querySelector("#candidate-reason");
  const reason = choice === "PASS" ? null : select?.value || null;
  if (choice !== "PASS" && !reason) return select.reportValidity();
  submitIntent("review_candidate", {review_binding_digest: currentView.candidate_review.review_binding_digest, choice, reason});
});
document.querySelector("#same-settings-action").addEventListener("click", (event) => {
  const button = event.target.closest("[data-op]");
  if (button) submitIntent(button.dataset.op);
});
window.addEventListener("offline", () => failClose("BRIDGE_UNAVAILABLE", "BROWSER_OFFLINE"));
window.addEventListener("online", () => { setBanner("연결을 확인한 뒤 최신 상태를 다시 읽습니다. 이전 요청은 다시 보내지 않습니다.", "info"); loadView(); });

loadView();
