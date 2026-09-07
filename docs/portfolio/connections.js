"use strict";
const buttons = [...document.querySelectorAll('[data-handoff]')];
const panels = [...document.querySelectorAll('.handoff-panel')];
function showConnection(key) {
  if (!buttons.some(button => button.dataset.handoff === key)) key = 'selection';
  buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.handoff === key)));
  panels.forEach(panel => { panel.hidden = panel.id !== `handoff-${key}`; });
}
function revealDependencies() {
  if (location.hash === '#dependencies') document.getElementById('dependencies').open = true;
}
addEventListener('hashchange', revealDependencies);
revealDependencies();
if (buttons.length && panels.length) {
  document.querySelector('.handoff-controls').hidden = false;
  showConnection(location.hash.replace('#handoff-', ''));
  buttons.forEach(button => button.addEventListener('click', () => {
    showConnection(button.dataset.handoff);
    try { history.replaceState(null, '', `#handoff-${button.dataset.handoff}`); } catch {}
  }));
  addEventListener('hashchange', () => {
    if (location.hash.startsWith('#handoff-')) showConnection(location.hash.slice(9));
  });
}
