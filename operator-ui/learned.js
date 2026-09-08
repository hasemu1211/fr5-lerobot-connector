"use strict";
(() => {
  const node = id => document.getElementById(`learned-${id}`);
  const token = document.querySelector('meta[name="operator-token"]').content;
  const labels = {PRECONTACT_HUMAN: "접촉 전 실제 자세 확인", LEARNED_CHUNK_COMPLETE: "청크 실행 완료 · 다음 선택",
    LEARNED_NEXT_PLAN: "다음 청크의 정확한 계획 승인"};
  const choices = {CONTINUE: "다음 청크 준비", PASS: "작업 완료로 판정하고 종료", FAIL: "작업 실패로 판정하고 종료",
    APPROVE: "이 정확한 계획 승인", CANCEL: "취소", CONFIRM: "실제 접촉 전 자세 확인"};
  let view = null, connected = false, busy = false, failures = 0, timer = null;
  const digest = value => typeof value === "string" && /^sha256:[a-f0-9]{64}$/.test(value);
  function render(next) {
    const p = next?.projection;
    if (next?.schema_version !== "data_factory.operator_session_view.v2" || p?.kind !== "LEARNED_RUN"
        || !digest(next.view_digest) || !Number.isInteger(next.revision) || next.revision < 0
        || typeof next.session_id !== "string" || typeof p.run_id !== "string"
        || !Array.isArray(p.available_ops) || p.training_authority !== false
        || p.approval_scope !== "HUMAN_GATED" || !["READY", "ACTIVE", "CANCELLING", "TERMINAL"].includes(p.state)
        || (view && (next.session_id !== view.session_id || p.run_id !== view.projection.run_id || next.revision < view.revision))
        || (p.pending_plan && (!digest(p.pending_plan.decision_binding_digest) || !digest(p.pending_plan.plan_digest)))
        || (p.checkpoint && (!digest(p.checkpoint.binding_digest) || !digest(p.checkpoint.plan_digest)
          || !Array.isArray(p.checkpoint.choices) || p.checkpoint.choices.some(choice => typeof choice !== "string")))) throw new Error("LEARNED_VIEW_CONTRACT");
    view = next; connected = true; failures = 0;
    node("connection").textContent = "현재 실행 소유자와 연결됨";
    node("run").textContent = `${p.run_id} · ${p.instruction || "작업 지시 없음"}`;
    const checkpoint = p.checkpoint, plan = p.pending_plan;
    const envelope = p.state === "TERMINAL" ? p.lifecycle_result?.plan_envelope || p.plan_envelope
      : checkpoint?.evidence?.plan_envelope || p.plan_envelope;
    const learned = envelope?.operator_summary?.learned;
    node("stage").textContent = p.state === "TERMINAL" ? "실행 종료" : p.state === "CANCELLING" ? "취소 요청됨 · 종료 확인 중"
      : checkpoint ? labels[checkpoint.kind] || checkpoint.kind : plan ? "첫 계획 승인" : p.state === "READY" ? "실행 준비 전" : "실행 소유자가 처리 중";
    node("summary").textContent = checkpoint?.prompt || p.latest?.code || "시작을 누르면 기존 실행 소유자가 입력과 계획을 준비합니다. 아직 실행 승인은 없습니다.";
    node("binding").textContent = checkpoint?.plan_digest || plan?.plan_digest || p.result?.plan_digest || "";
    node("plan-summary").textContent = learned
      ? `${learned.actions}개 동작 · 계획 시간 ${learned.duration_s}초 (실제 대기·처리 시간 별도) · 작업 효과 ${learned.task_effectiveness} · 장면 결과 ${learned.scene_outcome}`
      : "아직 확인된 학습 계획이 없습니다.";
    node("evidence").textContent = JSON.stringify(checkpoint?.evidence || plan?.decision_binding || p.latest || {}, null, 2);
    node("plan").textContent = JSON.stringify(envelope || {}, null, 2);
    node("terminal").hidden = p.state !== "TERMINAL";
    node("outcome").textContent = p.error || `${p.result?.state || "UNKNOWN"} · ${p.result?.code || "결과 없음"}`;
    node("diagnostic").textContent = JSON.stringify(p.result?.data || {}, null, 2);
    node("history").textContent = JSON.stringify({result: p.result, lifecycle_result: p.lifecycle_result}, null, 2);
    buttons();
  }
  function buttons() {
    const p = view?.projection, actions = node("actions");
    actions.replaceChildren();
    if (!p) return;
    function add(op, label, payload) {
      if (!p.available_ops.includes(op)) return;
      const button = document.createElement("button");
      button.type = "button"; button.textContent = label; button.dataset.op = op;
      if (payload.choice) button.dataset.choice = payload.choice;
      button.disabled = busy || !connected;
      button.addEventListener("click", () => send(op, payload)); actions.append(button);
    }
    add("start_learned_run", "입력 검증·첫 계획 준비", {});
    if (p.pending_plan) add("approve_exact_plan", "이 정확한 첫 계획 승인", {decision_binding_digest: p.pending_plan.decision_binding_digest});
    if (p.checkpoint) for (const choice of p.checkpoint.choices) add("resolve_checkpoint", choices[choice] || choice,
      {checkpoint_binding_digest: p.checkpoint.binding_digest, choice});
    add("cancel_learned_run", "실행 취소 요청", {});
  }
  async function request(path, options = {}) {
    const response = await fetch(path, {cache: "no-store", signal: AbortSignal.timeout(10000), ...options,
      headers: {"X-Operator-Token": token, ...options.headers}});
    if (response.status === 401 || response.status === 403) throw new Error("LEARNED_SESSION_UNAVAILABLE");
    const value = await response.json();
    if (!response.ok || value.ok === false) throw new Error(value.code || "LEARNED_HTTP_ERROR");
    return value;
  }
  function failed(error) {
    connected = false; failures += 1;
    if (error.name === "SyntaxError" || ["LEARNED_VIEW_CONTRACT", "LEARNED_SESSION_UNAVAILABLE"].includes(error.message)) failures = 3;
    node("connection").textContent = `이전 상태 · 현재 로봇 동작/정지는 확인되지 않음 (${error.message}). 현재 상태를 다시 확인하세요.`;
  }
  function schedule() {
    clearTimeout(timer);
    if (failures < 3 && (!view || view.projection.state !== "TERMINAL" || !connected)) timer = setTimeout(load, 1000);
  }
  async function load() {
    if (busy) return;
    busy = true; buttons();
    try { render(await request("/api/view")); } catch (error) { failed(error); }
    finally { busy = false; buttons(); schedule(); }
  }
  async function send(op, payload) {
    if (busy || !connected || !view.projection.available_ops.includes(op)) return;
    clearTimeout(timer); busy = true; buttons();
    const intent = {schema_version: "data_factory.operator_intent.v1", intent_id: crypto.randomUUID(),
      session_id: view.session_id, view_revision: view.revision, view_digest: view.view_digest, op, payload};
    try { await request("/api/intent", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(intent)}); }
    catch (error) { failed(error); }
    finally { busy = false; await load(); } // Read only; never replay a choice after a lost response.
  }
  node("refresh").addEventListener("click", () => { failures = 0; load(); });
  window.addEventListener("pagehide", () => clearTimeout(timer));
  load();
})();
