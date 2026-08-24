const select = document.querySelector("#fixture-select");
const languageSelect = document.querySelector("#language-select");
const announcer = document.querySelector("#announcer");
const requestedLanguage = new URLSearchParams(location.search).get("lang");
let currentLanguage = Object.hasOwn(MESSAGE_CATALOG, requestedLanguage) ? requestedLanguage : "en";
let states;
let loadError;

const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);

function message(key, values = {}) {
  return MESSAGE_CATALOG[currentLanguage].ui[key].replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ""));
}

function localizedState(key) {
  const source = states[key];
  const copy = MESSAGE_CATALOG[currentLanguage].states?.[key];
  if (!copy) return source;
  const localized = {...source, ...copy};
  if (source.nextAction) {
    localized.nextAction = {...source.nextAction, ...copy.nextAction};
    localized.nextAction.command = source.nextAction.command;
  }
  return localized;
}

function applyStaticMessages() {
  document.documentElement.lang = currentLanguage;
  document.title = message("pageTitle");
  languageSelect.value = currentLanguage;
  document.querySelector("#language-current").textContent = message("currentLanguage");
  document.querySelectorAll("[data-message]").forEach((element) => {
    element.textContent = message(element.dataset.message);
  });
  document.querySelectorAll("[data-message-aria-label]").forEach((element) => {
    element.setAttribute("aria-label", message(element.dataset.messageAriaLabel));
  });
}

function renderSteps(active) {
  document.querySelector("#workflow-steps").innerHTML = MESSAGE_CATALOG[currentLanguage].ui.steps.map((step, index) => `
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
    <label for="approval-input">${escapeHtml(message("approvalLabel"))}</label>
    <code>${escapeHtml(state.approval.command)}</code>
    <input id="approval-input" autocomplete="off" spellcheck="false"
      aria-describedby="approval-hint" data-expected="${escapeHtml(state.approval.command)}">
    <p id="approval-hint" class="support-copy">${escapeHtml(message("approvalHint"))}</p>
    <button type="submit">${escapeHtml(message("approvalButton"))}</button>
  </form>`;
}

function reviewControl(state) {
  if (!state.review) return "";
  return `<form id="review-form"><fieldset class="review-control">
    <legend>${escapeHtml(message("reviewLegend"))}</legend>
    <div class="review-options">${state.review.options.map((option) => `<label><input type="radio" name="review" value="${escapeHtml(option)}" required>${escapeHtml(option)}</label>`).join("")}</div>
    <label id="review-reason-field" for="review-reason" hidden>${escapeHtml(message("reviewReason"))}
      <select id="review-reason" name="reason" required disabled>
        <option value="">${escapeHtml(message("reviewReasonPlaceholder"))}</option>
        ${state.review.reasons.map((reason) => `<option>${escapeHtml(reason)}</option>`).join("")}
      </select>
    </label>
    <button type="submit">${escapeHtml(message("reviewButton"))}</button>
    <p class="support-copy">${escapeHtml(message("reviewHelp"))}</p>
  </fieldset></form>`;
}

function progress(state) {
  if (state.progress === undefined) return "";
  if (typeof state.progress !== "number" || !Number.isFinite(state.progress) || state.progress < 0 || state.progress > 100) {
    throw new TypeError("Progress must be a finite number from 0 to 100.");
  }
  const percent = escapeHtml(state.progress);
  return `<div class="progress-block">
    <div><span>${escapeHtml(message("episode"))} ${escapeHtml(state.episode)}</span><strong>${percent}%</strong></div>
    <progress max="100" value="${percent}">${percent}%</progress>
    <p>${escapeHtml(state.phase)}</p>
  </div>`;
}

function render(key, announce = true) {
  const state = localizedState(key);
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
    ${state.digest ? `<div class="digest"><span>${escapeHtml(message("exactDigest"))}</span><code>${escapeHtml(state.digest)}</code></div>` : ""}
    ${state.nextAction ? `<section class="next-action" aria-labelledby="next-heading"><p class="eyebrow">${escapeHtml(message("nextAction"))}</p><h3 id="next-heading">${escapeHtml(state.nextAction.title)}</h3><p>${escapeHtml(state.nextAction.detail)}</p>${state.nextAction.command ? `<code>${escapeHtml(state.nextAction.command)}</code>` : ""}</section>` : ""}
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
      input.setCustomValidity(message("approvalMismatch"));
      input.reportValidity();
      announcer.textContent = message("approvalMismatchAnnouncement");
      return;
    }
    input.setCustomValidity("");
    render("running", false);
    announcer.textContent = message("approvalMatched");
  });
  document.querySelector("#approval-input")?.addEventListener("input", (event) => event.currentTarget.setCustomValidity(""));
  const reviewForm = document.querySelector("#review-form");
  reviewForm?.addEventListener("change", (event) => {
    if (event.target.name !== "review") return;
    const needsReason = ["FAIL", "UNCERTAIN"].includes(event.target.value);
    const reasonField = document.querySelector("#review-reason-field");
    const reason = document.querySelector("#review-reason");
    reasonField.hidden = !needsReason;
    reason.disabled = !needsReason;
    reason.value = "";
    announcer.textContent = message(needsReason ? "reviewNeedsReason" : "reviewNoReason", {decision: event.target.value});
  });
  reviewForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(reviewForm);
    const decision = data.get("review");
    const reason = data.has("reason") ? message("reviewWithReason", {reason: data.get("reason")}) : "";
    announcer.textContent = message("reviewPreviewed", {decision, reason});
  });
}

function renderError() {
  const errorMessage = message(loadError.status ? "fixtureErrorStatus" : "fixtureError", {status: loadError.status});
  document.querySelector("#state-content").innerHTML = `<h2 id="state-title">${escapeHtml(message("fixtureUnavailable"))}</h2><p>${escapeHtml(errorMessage)}</p><p>${escapeHtml(message("fixturePreviewHelp"))}</p>`;
}

function applyLanguage(announce = true) {
  applyStaticMessages();
  if (states) {
    const key = Object.hasOwn(states, select.value) ? select.value : "setup";
    select.replaceChildren(...Object.entries(states).map(([stateKey]) => new Option(localizedState(stateKey).label, stateKey)));
    render(key, false);
  } else if (loadError) {
    renderError();
  }
  if (announce) announcer.textContent = message("languageChanged");
}

languageSelect.addEventListener("change", () => {
  currentLanguage = languageSelect.value;
  const url = new URL(location.href);
  url.searchParams.set("lang", currentLanguage);
  history.replaceState(null, "", url);
  applyLanguage();
});
applyLanguage(false);

fetch("fixtures/states.json")
  .then((response) => {
    if (!response.ok) {
      const error = new Error(`Fixture request failed: ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return response.json();
  })
  .then((fixtureStates) => {
    states = fixtureStates;
    select.addEventListener("change", () => render(select.value));
    const requestedState = new URLSearchParams(location.search).get("state");
    select.replaceChildren(...Object.entries(states).map(([key]) => new Option(localizedState(key).label, key)));
    render(Object.hasOwn(states, requestedState) ? requestedState : "setup", false);
  })
  .catch((error) => {
    loadError = error;
    renderError();
    console.error(error);
  });
