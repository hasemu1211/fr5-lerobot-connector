"use strict";

(() => {
  const token = document.querySelector('meta[name="operator-token"]').content;
  const element = (id) => document.getElementById(`training-${id}`);
  const buttons = ["refresh", "prepare", "approve", "refuse"];
  const operations = {prepare: "prepare_training_review", approve: "approve_training_batch", refuse: "refuse_training_batch"};
  const labels = {
    UNPREPARED: "아직 승인하지 않았습니다. 검토할 묶음을 확인하세요.",
    PREPARING: "원본과 판정 근거를 확인하고 있습니다. 아직 승인하지 않았습니다.",
    PREVIEW_NOT_APPROVED: "표시된 에피소드 전체를 검토한 뒤 승인 여부를 선택하세요.",
    PUBLISHING: "선택한 묶음을 다시 검증하고 승인 결과를 저장하고 있습니다.",
    APPROVED: "표시된 묶음의 학습 사용을 승인했습니다. 학습은 시작하지 않았습니다.",
    REFUSED: "승인하지 않았습니다. 데이터와 승인 파일을 변경하지 않았습니다.",
    FAILED: "승인 결과를 확정하지 못했습니다. 자동 재시도하지 않습니다. 출력과 근거를 확인한 뒤 새 검토가 필요합니다.",
  };
  let view = null;
  let busy = false;

  function render(next) {
    if (next.projection?.kind !== "TRAINING_REVIEW") throw new Error("TRAINING_REVIEW_SERVICE_REQUIRED");
    view = next;
    const p = view.projection;
    element("status").textContent = (labels[p.status] || p.status) + (p.error ? ` (${p.error})` : "");
    element("operator").textContent = `설정된 운영자: ${p.operator_label}`;
    for (const [button, op] of Object.entries(operations)) element(button).hidden = !p.available_ops.includes(op);
    element("preview").hidden = !p.preview;
    if (p.preview) {
      const preview = p.preview;
      element("dataset").textContent = preview.dataset_identity.dataset_id;
      element("count").textContent = `${preview.selected_count}개 에피소드 · 기술 검사 PASS · 내용 판정 PASS · 학습 사용 승인 별도`;
      element("identity").textContent = JSON.stringify(preview.dataset_identity);
      element("digest").textContent = preview.batch_digest;
      element("output").textContent = p.inventory_path;
      element("episodes").replaceChildren(...preview.episodes.map((episode) => {
        const row = document.createElement("li");
        row.textContent = `에피소드 ${episode.episode_index} · ${episode.episode_id} · 기술 ${episode.technical_status} · 내용 ${episode.semantic_status} (${episode.reviewer_id})`;
        return row;
      }));
    }
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {cache: "no-store", ...options,
      headers: {"X-Operator-Token": token, ...options.headers}});
    const value = await response.json();
    if (!response.ok || value.ok === false) throw new Error(value.code || "TRAINING_REVIEW_CONNECTION");
    return value;
  }

  async function perform(action) {
    if (busy) return;
    busy = true;
    buttons.forEach((id) => { element(id).disabled = true; });
    try {
      if (action === "refresh") {
        render(await request("/api/view"));
      } else {
        element("status").textContent = action === "prepare" ? labels.PREPARING :
          action === "approve" ? labels.PUBLISHING : "승인하지 않음 선택을 전달하고 있습니다.";
        const intent = {schema_version: "data_factory.operator_intent.v1",
          intent_id: crypto.randomUUID(), session_id: view.session_id,
          view_revision: view.revision, view_digest: view.view_digest,
          op: operations[action], payload: action === "prepare" ? {} : {batch_digest: view.projection.preview.batch_digest}};
        // Never retry a decision automatically, including after a lost response.
        await request("/api/intent", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(intent)});
        render(await request("/api/view"));
      }
    } catch (error) {
      view = null;
      for (const button of Object.keys(operations)) element(button).hidden = true;
      element("status").textContent = `${error.message} · 요청을 반복하지 않습니다. 현재 상태를 다시 확인하고 검토하세요.`;
    } finally {
      busy = false;
      buttons.forEach((id) => { element(id).disabled = false; });
    }
  }
  buttons.forEach((id) => element(id).addEventListener("click", () => perform(id)));
  perform("refresh");
})();
