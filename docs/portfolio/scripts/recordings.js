'use strict';
for (const group of document.querySelectorAll('[data-recording-selector]')) {
  const buttons = [...group.querySelectorAll('[data-recording-choice]')];
  const panels = [...group.querySelectorAll('[data-recording-panel]')];
  function select(task) {
    if (!buttons.some(button => button.dataset.recordingChoice === task)) task = 'pick';
    for (const button of buttons) button.setAttribute('aria-pressed', String(button.dataset.recordingChoice === task));
    for (const panel of panels) {
      panel.hidden = panel.dataset.recordingPanel !== task;
      if (panel.hidden) panel.querySelector('video').pause();
    }
  }
  select(new URLSearchParams(location.search).get('task'));
  for (const button of buttons) button.addEventListener('click', () => {
    select(button.dataset.recordingChoice);
    const url = new URL(location.href);
    url.searchParams.set('task', button.dataset.recordingChoice);
    history.replaceState(null, '', url.href);
  });
}
if (location.hash === '#evaluation-recording') document.getElementById('evaluation-recording')?.setAttribute('open', '');
