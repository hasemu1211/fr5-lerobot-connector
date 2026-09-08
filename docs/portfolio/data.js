'use strict';
const observations = window.FR5_OBSERVATIONS;
if (observations) {
  const episodes = [...new Set(observations.map(item => item.episode))];
  const params = new URLSearchParams(location.search);
  let episode = episodes.includes(Number(params.get("episode"))) ? Number(params.get("episode")) : episodes[0];
  let moment = params.has("moment") && [0, 1, 2].includes(Number(params.get("moment"))) ? Number(params.get("moment")) : 1;
  let renderVersion = 0;
  function choices(id, name, values, labels, selected, change) {
    const parent = document.getElementById(id);
    values.forEach((value, i) => {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'radio'; input.name = name; input.value = value;
      input.checked = value === selected;
      input.addEventListener('change', () => { change(value); render(); });
      const text = document.createElement('span'); text.textContent = labels[i];
      label.append(input, text); parent.append(label);
    });
  }
  function renderAction(item) {
    const width = 500, height = 322;
    const parts = [];
    for (let axis = 0; axis < 7; axis++) {
      const values = item.recordedAction.map(row => row[axis] * (axis === 6 ? 1000 : 1));
      const low = Math.min(...values), high = Math.max(...values);
      const span = Math.max(high - low, axis === 6 ? .01 : .0001);
      const center = (high + low) / 2, y = 23 + axis * 45;
      const points = values.map((v, i) => `${72 + i * 290 / 49},${y - (v - center) / span * 25}`).join(' ');
      const label = axis === 6 ? 'Gripper' : `J${axis + 1}`;
      const format = value => value.toFixed(axis === 6 ? 2 : 3).replace('-', '−');
      parts.push(`<text x="0" y="${y + 4}">${label}</text><line x1="72" x2="362" y1="${y}" y2="${y}" stroke="#d7dfe3"/><polyline points="${points}" fill="none" stroke="${axis === 6 ? '#b75a17' : '#285c73'}" stroke-width="2"/><text x="490" y="${y + 4}" text-anchor="end" class="sample-range">${format(low)} ~ ${format(high)}</text>`);
    }
    document.getElementById('sample-action').innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" aria-hidden="true">${parts.join('')}</svg>`;
    document.getElementById('sample-action').setAttribute('aria-label', `같은 관측부터 50개 시점의 시연 목표 동작. 각 관절과 그리퍼의 축별 최소·최대 눈금이다.`);
    document.getElementById('sample-task').textContent = item.task;
  }
  async function render() {
    const version = ++renderVersion;
    const item = observations.filter(item => item.episode === episode)[moment];
    const study = document.getElementById('observations');
    study.dataset.loading = 'true'; study.setAttribute('aria-busy', 'true');
    document.getElementById('frame-caption').textContent = `Pick 시연 · 선택한 두 관측을 읽고 있습니다`;
    document.getElementById('state-values').replaceChildren();
    try {
      await Promise.all(item.images.map(src => { const image = new Image(); image.src = src; return image.decode(); }));
    } catch {
      if (version === renderVersion) {
        document.getElementById('frame-caption').textContent = '관측 이미지를 읽지 못했습니다. 다른 시점을 선택하거나 원본과 추출 범위를 확인해 주세요.';
        study.setAttribute('aria-busy', 'false');
      }
      return;
    }
    if (version !== renderVersion) return;
    delete study.dataset.loading; study.setAttribute('aria-busy', 'false');
    try { history.replaceState(null, '', `?episode=${episode}&moment=${moment}${location.hash}`); }
    catch { /* A browser may restrict local-file history; keep the observation usable. */ }
    ['up', 'wrist'].forEach((camera, i) => {
      const image = document.getElementById(`${camera}-image`);
      image.hidden = false;
      image.src = item.images[i];
      image.alt = `Pick 시연 ${item.timestamp.toFixed(3)}초의 카메라 ${i + 1} 관측`;
    });
    document.getElementById('frame-caption').textContent = `Pick 시연 · ${item.timestamp.toFixed(3)}초 · 시연의 ${[10, 50, 90][moment]}% 위치`;
    document.getElementById('frame-source').href = `sources/observations.html#ep${episode}-f${item.frame}`;
    renderAction(item);
    const state = document.getElementById('state-values'); state.replaceChildren();
    item.state.forEach((value, i) => {
      const cell = document.createElement('div');
      const label = document.createElement('small'); label.textContent = i < 6 ? `J${i + 1} / rad` : 'G / mm';
      const number = document.createElement('span'); number.textContent = (value * (i < 6 ? 1 : 1000)).toFixed(i < 6 ? 3 : 2);
      cell.append(label, number); state.append(cell);
    });
  }
  choices('episode-options', 'episode', episodes, episodes.map(value => window.FR5_RECORDING_LABELS[value]), episode, value => { episode = value; });
  choices('moment-options', 'moment', [0, 1, 2], ['10%', '50%', '90%'], moment, value => { moment = value; });
  render();
}
