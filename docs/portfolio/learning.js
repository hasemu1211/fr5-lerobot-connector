'use strict';
if (window.FR5_ACTION_COMPARISONS) {
  const controls = ['action-seed', 'action-metric', 'action-episode'].map(id => document.getElementById(id));
  const params = new URLSearchParams(location.search);
  const initial = [params.get('seed'), `${params.get('metric')}_per_axis`, params.get('episode')];
  controls.forEach((control, i) => {
    if ([...control.options].some(option => option.value === initial[i])) control.value = initial[i];
  });
  let renderVersion = 0;
  async function renderActions() {
    const version = ++renderVersion;
    const [seed, metric, episode] = controls.map(control => control.value);
    const comparison = window.FR5_ACTION_COMPARISONS[seed];
    const variants = ['persistence_reference', 'quality3000', 'quality10000'];
    const rows = variants.map(key => episode === 'pooled' ? comparison.summary[key].pooled : comparison.summary[key].per_episode.find(item => item.episode_index === Number(episode)));
    const label = `Seed ${seed} · ${metric === 'mae_per_axis' ? 'MAE' : 'RMSE'} · ${episode === 'pooled' ? '전체 표본 합산' : `Episode ${episode}`} · ${rows[0].valid_action_steps} 유효 step`;
    const results = document.getElementById('action-results');
    const caption = document.getElementById('action-plot-caption');
    results.dataset.loading = 'true'; results.setAttribute('aria-busy', 'true');
    caption.textContent = `${label} · 그래프를 불러오는 중`;
    const src = `assets/action-seed${seed}-${metric.split('_')[0]}-${episode}.svg`;
    let loaded = true;
    try { const next = new Image(); next.src = src; await next.decode(); }
    catch { loaded = false; }
    if (version !== renderVersion) return;
    const image = document.getElementById('action-plot');
    image.hidden = !loaded;
    if (loaded) {
      image.src = src;
      image.alt = `${label}. 여섯 관절은 같은 rad 눈금으로, 그리퍼는 별도의 m 눈금으로 세 설정의 오차를 비교한다. 아래 수치 표에서 각 값을 확인할 수 있다.`;
    }
    const body = document.getElementById('action-body'); body.replaceChildren();
    for (let i = 0; i < 7; i++) {
      const tr = document.createElement('tr');
      const th = document.createElement('th'); th.scope = 'row'; th.textContent = i < 6 ? `J${i + 1} / rad` : 'Gripper / m'; tr.append(th);
      rows.forEach(row => { const td = document.createElement('td'); td.textContent = row[metric][i].toPrecision(5); tr.append(td); });
      body.append(tr);
    }
    caption.textContent = loaded ? label : `${label} · 그래프를 읽지 못했다. 아래 수치 표에서 확인할 수 있다.`;
    document.getElementById('action-caption').textContent = label;
    const query = new URLSearchParams({seed, metric: metric.split('_')[0], episode});
    document.getElementById('action-source').href = `sources/action-comparison.html?${query}#seed-${seed}`;
    try { history.replaceState(null, '', `?${query}${location.hash}`); }
    catch { /* Preserve local-file interaction when browser history is restricted. */ }
    delete results.dataset.loading; results.setAttribute('aria-busy', 'false');
  }
  controls.forEach(control => control.addEventListener('change', renderActions));
  renderActions();
}
