const steps = ["Setup", "Readiness", "Exact approval", "Progress", "Review", "Recovery"];
const select = document.querySelector("#fixture-select");
const announcer = document.querySelector("#announcer");
let states;

const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);

function renderSteps(active) {
  document.querySelector("#workflow-steps").innerHTML = steps.map((step, index) => `
    <li class="${index === active ? "active" : index < active ? "complete" : ""}"
        ${index === active ? 'aria-current="step"' : ""}>
      <span>${String(index + 1).padStart(2, "0")}</span>${escapeHtml(step)}
    </li>`).join("");
}

function renderSetup(setup) {
  document.querySelector("#setup-details").innerHTML = Object.entries(setup).map(([term, value]) => `
    <div><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
}

function approvalControl(state) {
  if (!state.approval) return "";
  return `<form id="approval-form" class="approval-form">
    <label for="approval-input">Type the exact approval phrase</label>
    <code>${escapeHtml(state.approval.command)}</code>
    <input id="approval-input" autocomplete="off" spellcheck="false"
      aria-describedby="approval-hint" data-expected="${escapeHtml(state.approval.command)}">
    <p id="approval-hint" class="support-copy">This only checks text and previews the running fixture. The backend must still validate scene, start state, expiry, and digest.</p>
    <button type="submit">Verify fixture phrase</button>
  </form>`;
}

function reviewControl(state) {
  if (!state.review) return "";
  return `<fieldset class="review-control">
    <legend>Preview a semantic review intent</legend>
    ${state.review.options.map((option) => `<button type="button" data-review="${escapeHtml(option)}">${escapeHtml(option)}</button>`).join("")}
    <label for="review-reason">Primary reason for FAIL or UNCERTAIN
      <select id="review-reason">${state.review.reasons.map((reason) => `<option>${escapeHtml(reason)}</option>`).join("")}</select>
    </label>
    <p class="support-copy">No candidate file is changed. A future bridge must submit the exact file and review-context digests to backend compare-and-swap.</p>
  </fieldset>`;
}

function progress(state) {
  if (state.progress === undefined) return "";
  return `<div class="progress-block">
    <div><span>Episode ${escapeHtml(state.episode)}</span><strong>${state.progress}%</strong></div>
    <progress max="100" value="${state.progress}">${state.progress}%</progress>
    <p>${escapeHtml(state.phase)}</p>
  </div>`;
}

function render(key, announce = true) {
  const state = states[key];
  select.value = key;
  renderSteps(state.step);
  renderSetup(state.setup);
  document.querySelector("#state-content").innerHTML = `
    <header class="state-heading">
      <div>
        <p class="eyebrow">${escapeHtml(state.kicker)}</p>
        <h2 id="state-title">${escapeHtml(state.title)}</h2>
      </div>
      <span class="status ${escapeHtml(state.tone)}">${escapeHtml(state.status)}</span>
    </header>
    <p class="state-summary">${escapeHtml(state.summary)}</p>
    ${progress(state)}
    ${state.digest ? `<div class="digest"><span>Exact plan digest</span><code>${escapeHtml(state.digest)}</code></div>` : ""}
    ${state.nextAction ? `<section class="next-action" aria-labelledby="next-heading"><p class="eyebrow">Next safe action</p><h3 id="next-heading">${escapeHtml(state.nextAction.title)}</h3><p>${escapeHtml(state.nextAction.detail)}</p>${state.nextAction.command ? `<code>${escapeHtml(state.nextAction.command)}</code>` : ""}</section>` : ""}
    ${approvalControl(state)}${reviewControl(state)}`;
  document.querySelector("#evidence-list").innerHTML = state.evidence.map((item) => `<li><span>${escapeHtml(item.label)}</span><code>${escapeHtml(item.value)}</code></li>`).join("");
  document.querySelector("#authority-copy").textContent = state.authority;
  bindControls();
  if (announce) announcer.textContent = `${state.title}${/[.!?]$/.test(state.title) ? "" : "."} ${state.status}. ${state.summary}`;
}

function bindControls() {
  document.querySelector("#approval-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = document.querySelector("#approval-input");
    if (input.value !== input.dataset.expected) {
      input.setCustomValidity("The phrase must match the displayed digest exactly.");
      input.reportValidity();
      announcer.textContent = "Approval phrase does not match the exact plan digest.";
      return;
    }
    input.setCustomValidity("");
    render("running");
  });
  document.querySelectorAll("[data-review]").forEach((button) => button.addEventListener("click", () => {
    const reason = ["FAIL", "UNCERTAIN"].includes(button.dataset.review) ? ` with reason ${document.querySelector("#review-reason").value}` : "";
    announcer.textContent = `${button.dataset.review}${reason} review intent previewed. No backend or candidate artifact was changed.`;
  }));
}

fetch("fixtures/states.json")
  .then((response) => {
    if (!response.ok) throw new Error(`Fixture request failed: ${response.status}`);
    return response.json();
  })
  .then((fixtureStates) => {
    states = fixtureStates;
    Object.entries(states).forEach(([key, state]) => select.add(new Option(state.label, key)));
    select.addEventListener("change", () => render(select.value));
    const requestedState = new URLSearchParams(location.search).get("state");
    render(Object.hasOwn(states, requestedState) ? requestedState : "ready", false);
  })
  .catch((error) => {
    document.querySelector("#state-content").innerHTML = `<h2 id="state-title">Fixture unavailable</h2><p>${escapeHtml(error.message)}</p><p>Preview with <code>make -C operator-ui preview</code>; opening index.html directly cannot load JSON fixtures.</p>`;
    console.error(error);
  });
