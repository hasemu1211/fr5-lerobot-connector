"use strict";

(() => {
  const token = document.querySelector('meta[name="operator-token"]').content;
  const element = (id) => document.getElementById(`curator-${id}`);
  const video = element("video");
  const buttons = ["approve", "reject", "recover", "refresh"];
  const labels = {
    REVIEW_READY: "영상과 표본 범위를 검토한 뒤 이 후보의 승인 여부를 선택하세요.",
    PUBLISHED: "이 후보를 공개했습니다. 원본은 보존되며 학습 사용 승인은 별도입니다.",
    REJECTED: "이 후보를 반려하고 정리했습니다. 원본은 보존했습니다.",
  };
  let view = null;
  let boundRun = null;
  let busy = false;
  let mediaDigest = null;
  let mediaUrl = null;
  let mediaReady = false;

  function updateButtons() {
    const p = view?.projection;
    const review = p?.review;
    const available = !p?.request_pending && p?.available_ops.includes("decide_curator_candidate");
    element("approve").hidden = !available || !!review.decision || !review.allowed_decisions.includes("APPROVE");
    element("reject").hidden = !available || !!review.decision || !review.allowed_decisions.includes("REJECT");
    element("recover").hidden = !available || !review.decision || !review.allowed_decisions.includes(review.decision.decision);
    for (const id of buttons) element(id).disabled = busy || (id !== "refresh" && !review?.decision && !mediaReady);
  }

  function render(next) {
    const p = next.projection;
    const r = p?.review;
    if (next.schema_version !== "data_factory.operator_session_view.v2" || p?.kind !== "CURATOR_REVIEW"
        || !r || !/^sha256:[a-f0-9]{64}$/.test(r.review_ready_digest) || !Array.isArray(r.allowed_decisions)
        || r.allowed_decisions.some((choice) => !["APPROVE", "REJECT"].includes(choice))
        || !Array.isArray(r.clips) || !Array.isArray(p.available_ops) || r.training_authority !== false
        || typeof r.media_available !== "boolean"
        || (boundRun !== null && boundRun !== r.run_id)) throw new Error("CURATOR_REVIEW_SERVICE_CHANGED");
    boundRun = r.run_id;
    view = next;
    element("status").textContent = p.request_pending
      ? "선택한 결정의 검증과 저장이 진행 중입니다. 현재 상태를 확인하고 있습니다."
      : labels[r.status] || (r.decision ? "결정은 기록되었습니다. 저장된 선택으로 결과를 마무리할 수 있습니다." : `현재 상태: ${r.status}`);
    if (r.failure && !r.receipt) element("status").textContent += ` (${r.failure.code || r.failure.state})`;
    element("run").textContent = `검토 대상 · ${r.run_id}`;
    const actor = r.decision?.actor;
    element("actor").textContent = actor ? `기록된 결정: ${r.decision.decision} · 서버 계정 ${actor.account} (개인 신원 미인증)`
      : "명시적인 사람의 선택을 서버의 로컬 OS 계정으로 기록합니다. 개인 신원 인증은 제공하지 않습니다.";
    const c = r.coverage;
    element("coverage").textContent = c
      ? `${c.clip_count}개 장면 · ${c.unique_selected_frames}/${c.population_frames} 프레임 · 에피소드 ${c.covered_episodes.length}/${c.episodes.length}개 · 작업 ${c.covered_tasks.length}/${c.tasks.length}개. 선택된 표본만 표시합니다.`
      : "현재 검토 영상과 표본 범위를 다시 검증할 수 없습니다. 저장된 결정 결과는 별도로 확인했습니다.";
    let reviewOffset = 0;
    element("clips").replaceChildren(...r.clips.map((clip, index) => {
      // Clips are concatenated in manifest order; start_relative_seconds is source time.
      const seekTime = reviewOffset;
      reviewOffset += clip.duration_seconds;
      const row = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      const time = document.createElement("span");
      time.textContent = `${seekTime.toFixed(1)}초`;
      const detail = document.createElement("span");
      detail.textContent = `${index + 1}. 에피소드 ${clip.episode_index} · ${clip.task}`;
      const reasons = document.createElement("small");
      reasons.textContent = `${clip.duration_seconds.toFixed(1)}초 분량 · 원본 ${clip.start_relative_seconds.toFixed(2)}초부터 · ${clip.reasons.join(" · ")}`;
      detail.append(reasons);
      button.append(time, detail);
      button.addEventListener("click", () => {
        if (mediaReady) { video.currentTime = seekTime; video.focus(); }
      });
      row.append(button);
      return row;
    }));
    element("result").hidden = !r.receipt;
    if (r.receipt) {
      element("result-summary").textContent = `${r.receipt.outcome} · ${r.decision.provenance} · 학습 권한 없음`;
      element("output").textContent = r.receipt.output?.root || "후보 정리 완료";
    }
    element("details").textContent = JSON.stringify({review_ready_digest: r.review_ready_digest,
      identities: r.identities, decision: r.decision, receipt: r.receipt,
      media_error: r.media_error, failure_history: r.failure}, null, 2);
    updateButtons();
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {cache: "no-store", signal: AbortSignal.timeout(30000), ...options,
      headers: {"X-Operator-Token": token, ...options.headers}});
    if (options.media && response.ok) return response.blob();
    const value = await response.json();
    if (!response.ok || value.ok === false) throw new Error(value.code || "CURATOR_REVIEW_CONNECTION");
    return value;
  }

  function clearMedia() {
    mediaReady = false;
    mediaDigest = null;
    if (mediaUrl) URL.revokeObjectURL(mediaUrl);
    mediaUrl = null;
    video.pause();
    video.removeAttribute("src");
    video.load();
  }

  async function loadMedia() {
    const review = view.projection.review;
    if (!review.media_available) {
      clearMedia();
      element("media-status").textContent = `검토 영상을 사용할 수 없습니다. 저장된 결정 결과는 유지됩니다. (${review.media_error.reason_code})`;
      updateButtons();
      return;
    }
    const digest = review.review_ready_digest;
    if (mediaDigest === digest && mediaUrl) return;
    clearMedia();
    element("media-status").textContent = "검토 식별값에 결속된 영상을 읽고 있습니다.";
    updateButtons();
    try {
      const blob = await request(`/api/curator-review/video?review_digest=${digest}`, {media: true});
      mediaUrl = URL.createObjectURL(blob);
      mediaDigest = digest;
      video.src = mediaUrl;
    } catch (error) {
      element("media-status").textContent = `${error.message} · 영상을 확인할 수 없습니다. 현재 상태 확인으로 다시 읽으세요.`;
    }
  }

  video.addEventListener("loadeddata", () => {
    if (!mediaDigest || !view?.projection.review.media_available) return;
    mediaReady = true;
    element("media-status").textContent = "원본 · overlay · 실제 후보 영상 준비 완료";
    updateButtons();
  });
  video.addEventListener("error", () => {
    mediaReady = false;
    mediaDigest = null;
    element("media-status").textContent = "검토 영상을 재생할 수 없습니다. 현재 상태 확인으로 다시 읽으세요.";
    updateButtons();
  });

  async function reconcile() {
    render(await request("/api/view"));
    // Bounded reads only. A lost response never creates another decision request.
    for (let attempt = 0; view.projection.request_pending && attempt < 6; attempt += 1) {
      render(await request(`/api/view/watch?after_revision=${view.revision}`));
    }
    if (view.projection.request_pending) element("status").textContent += " 아직 처리 중입니다. 잠시 뒤 현재 상태를 확인하세요.";
    await loadMedia();
  }

  async function perform(action) {
    if (busy) return;
    busy = true;
    updateButtons();
    try {
      if (action === "refresh") {
        await reconcile();
      } else {
        const review = view.projection.review;
        const choice = action === "recover" ? review.decision.decision : action === "approve" ? "APPROVE" : "REJECT";
        const intent = {schema_version: "data_factory.operator_intent.v1", intent_id: crypto.randomUUID(),
          session_id: view.session_id, view_revision: view.revision, view_digest: view.view_digest,
          op: "decide_curator_candidate", payload: {choice, expected_review_digest: review.review_ready_digest}};
        element("status").textContent = "선택한 결정을 검증하고 저장하고 있습니다.";
        let failure = null;
        try {
          await request("/api/intent", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(intent)});
        } catch (error) { failure = error.message; }
        await reconcile();
        if (failure) element("status").textContent += ` 요청을 반복하지 않고 저장된 상태를 확인했습니다. (${failure})`;
      }
    } catch (error) {
      view = null;
      element("status").textContent = `${error.message} · 결과를 확인하지 못했습니다. 요청을 반복하지 않습니다. 현재 상태를 다시 확인하세요.`;
    } finally {
      busy = false;
      updateButtons();
    }
  }
  buttons.forEach((id) => element(id).addEventListener("click", () => perform(id)));
  perform("refresh");
})();
