"use strict";

(() => {
  const token = document.querySelector('meta[name="operator-token"]').content;
  const element = (id) => document.getElementById(`training-${id}`);
  const buttons = ["refresh", "prepare", "approve", "refuse", "return"];
  const operations = {prepare: "prepare_training_review", approve: "approve_training_batch", refuse: "refuse_training_batch", return: "return_training_review"};
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
  let viewerWindow = null;

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
      const statuses = (field) => [...new Set(preview.episodes.map((episode) => episode[field] || "확인되지 않음"))].join(", ") || "확인되지 않음";
      element("count").textContent = `${preview.selected_count}개 에피소드 · 기술 검사 ${statuses("technical_status")} · 내용 판정 ${statuses("semantic_status")} · 학습 사용 승인 별도`;
      element("identity").textContent = JSON.stringify(preview.dataset_identity);
      element("digest").textContent = preview.batch_digest;
      element("output").textContent = p.inventory_path;
      element("episodes").replaceChildren(...preview.episodes.map((episode) => {
        const row = document.createElement("li");
        const semantic = episode.semantic_status === "NOT_ASSERTED" ? "NOT_ASSERTED (별도 판정 없음)" : `${episode.semantic_status || "확인되지 않음"} (${episode.reviewer_id || "검토자 미표시"})`;
        row.textContent = `에피소드 ${episode.episode_index} · ${episode.episode_id} · 기술 ${episode.technical_status || "확인되지 않음"} · 내용 ${semantic}`;
        if (episode.parent_semantic_status) row.textContent += ` · 부모 내용 ${episode.parent_semantic_status} (${episode.reviewer_id || "검토자 미표시"})`;
        if (episode.parent_dataset_identity) row.textContent += ` · 부모 데이터셋 ${episode.parent_dataset_identity.dataset_id} (${episode.parent_dataset_identity.dataset_digest})`;
        if (episode.mapping) row.textContent += ` · 원본 에피소드 ${episode.source_episode_index} → ${episode.episode_index} · 이미지 변환 없음 · 결합 ${episode.mapping.manifest_digest}`;
        const coverage = episode.curator_review?.coverage;
        if (coverage) row.textContent += ` · Curator 묶음 검토 범위: 에피소드 ${coverage.covered_episodes.length}/${coverage.episodes.length} · 프레임 ${coverage.unique_selected_frames}/${coverage.population_frames}`;
        if (p.available_ops.includes("inspect_training_episode")) {
          const inspect = document.createElement("button");
          inspect.type = "button";
          inspect.className = "secondary-button";
          inspect.textContent = "영상·action/state 탐색";
          inspect.addEventListener("click", () => perform("inspect", episode.episode_index));
          row.appendChild(inspect);
        }
        return row;
      }));
    }
    const inspection = p.inspection;
    element("inspection").hidden = !inspection?.target;
    element("inspection-link").hidden = inspection?.status !== "READY";
    if (inspection?.target) {
      const target = inspection.target;
      const messages = {PREPARING: "읽기 전용 탐색을 준비하고 있습니다. 현재 상태 확인으로 진행을 확인할 수 있습니다.",
        READY: "이 에피소드를 새 탭에서 살펴본 뒤 여기로 돌아오세요. 탐색은 최대 15분 유지됩니다.",
        CLOSED: "탐색을 종료했습니다. 같은 검토 대상임을 다시 확인했습니다.",
        FAILED: "탐색을 열거나 유지하지 못했습니다. 검토와 승인 상태는 변경하지 않았습니다.",
        STALE: "검토 대상이 변경되었거나 없어졌습니다. 이전 영상으로 현재 데이터를 판단하지 마세요."};
      element("inspection-status").textContent = (messages[inspection.status] || inspection.status) + (inspection.error ? ` (${inspection.error})` : "");
      element("inspection-target").textContent = `${target.dataset_id} · episode ${target.episode_index} · ${target.episode_id} · ${target.dataset_digest}`;
      const m = inspection.mapping;
      element("inspection-mapping").textContent = m ? `frame_index 0–${m.last_frame_index} = 원본 frame · global index ${m.first_global_index}–${m.last_global_index}. ${m.frames} frames · ${m.rrd_bytes} bytes. action/state: ${m.features.action.names.join(", ")}. FR5 j1–j6: rad, gripper.pos: m. 설치 버전 LeRobot ${m.versions.lerobot} / Rerun ${m.versions.rerun}.` : "";
      if (inspection.status === "READY") {
        const url = new URL(inspection.url);
        if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.username || url.password) throw new Error("INSPECTION_LOOPBACK_REQUIRED");
        element("inspection-link").href = url.href;
      }
    }
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {cache: "no-store", ...options,
      headers: {"X-Operator-Token": token, ...options.headers}});
    const value = await response.json();
    if (!response.ok || value.ok === false) throw new Error(value.code || "TRAINING_REVIEW_CONNECTION");
    return value;
  }

  async function perform(action, episodeIndex) {
    if (busy) return;
    busy = true;
    buttons.forEach((id) => { element(id).disabled = true; });
    try {
      if (action === "refresh") {
        render(await request("/api/view"));
      } else {
        element("status").textContent = action === "inspect" ? "에피소드 탐색을 준비하고 있습니다." : action === "return" ? "같은 검토 대상을 다시 확인하고 있습니다." : action === "prepare" ? labels.PREPARING :
          action === "approve" ? labels.PUBLISHING : "승인하지 않음 선택을 전달하고 있습니다.";
        const intent = {schema_version: "data_factory.operator_intent.v1",
          intent_id: crypto.randomUUID(), session_id: view.session_id,
          view_revision: view.revision, view_digest: view.view_digest,
          op: action === "inspect" ? "inspect_training_episode" : operations[action],
          payload: action === "prepare" ? {} : {batch_digest: view.projection.preview.batch_digest,
            ...(action === "inspect" ? {episode_index: episodeIndex} : {})}};
        // Never retry a decision automatically, including after a lost response.
        let responseFailed = false;
        try {
          await request("/api/intent", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(intent)});
        } catch (_error) {
          responseFailed = true;
        }
        render(await request("/api/view"));
        if (action === "return") {
          if (viewerWindow && !viewerWindow.closed) viewerWindow.close();
          viewerWindow = null;
          element("episodes").focus();
        }
        if (responseFailed) element("status").textContent += " 요청을 반복하지 않고 현재 검토 상태를 다시 확인했습니다.";
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
  element("inspection-link").addEventListener("click", (event) => {
    event.preventDefault();
    if (viewerWindow && !viewerWindow.closed) viewerWindow.focus();
    else {
      viewerWindow = window.open(element("inspection-link").href, "_blank");
      if (viewerWindow) viewerWindow.opener = null;
    }
  });
  perform("refresh");
})();
