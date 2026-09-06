'use strict';
if (window.FR5_ACTION_COMPARISONS) {
  const controls = ['action-seed', 'action-metric', 'action-episode'].map(id => document.getElementById(id));
  function renderActions() {
    const [seed, metric, episode] = controls.map(control => control.value);
    const comparison = window.FR5_ACTION_COMPARISONS[seed];
    const variants = ['persistence_reference', 'quality3000', 'quality10000'];
    const rows = variants.map(key => episode === 'pooled' ? comparison.summary[key].pooled : comparison.summary[key].per_episode.find(item => item.episode_index === Number(episode)));
    const body = document.getElementById('action-body'); body.replaceChildren();
    for (let i = 0; i < 7; i++) {
      const tr = document.createElement('tr');
      const th = document.createElement('th'); th.scope = 'row'; th.textContent = i < 6 ? `J${i + 1} / rad` : 'Gripper / m'; tr.append(th);
      rows.forEach(row => { const td = document.createElement('td'); td.textContent = row[metric][i].toPrecision(5); tr.append(td); });
      body.append(tr);
    }
    document.getElementById('action-caption').textContent = `Seed ${seed} · ${metric === 'mae_per_axis' ? 'MAE' : 'RMSE'} · ${episode === 'pooled' ? '전체 표본 합산' : `Episode ${episode}`} · ${rows[0].valid_action_steps} 유효 step`;
  }
  controls.forEach(control => control.addEventListener('change', renderActions));
  renderActions();
}
