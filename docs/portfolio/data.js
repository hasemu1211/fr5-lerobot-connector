'use strict';
const observations = window.FR5_OBSERVATIONS;
if (observations) {
  const episodes = [...new Set(observations.map(item => item.episode))];
  const params = new URLSearchParams(location.search);
  let episode = episodes.includes(Number(params.get("episode"))) ? Number(params.get("episode")) : episodes[0];
  let moment = [0, 1, 2].includes(Number(params.get("moment"))) ? Number(params.get("moment")) : 0;
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
  async function render() {
    const version = ++renderVersion;
    const item = observations.filter(item => item.episode === episode)[moment];
    const study = document.getElementById('observations');
    study.dataset.loading = 'true'; study.setAttribute('aria-busy', 'true');
    document.getElementById('frame-caption').textContent = `Episode ${episode} · 선택한 두 관측을 읽고 있습니다`;
    document.getElementById('frame-source').href = `sources/observations.html#ep${episode}-f${item.frame}`;
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
      image.alt = `Episode ${episode}, frame ${item.frame}의 ${i === 0 ? '상부' : '손목'} 카메라 저장 관측`;
    });
    document.getElementById('frame-caption').textContent = `Episode ${episode} · frame ${item.frame} · ${item.timestamp.toFixed(3)} s · ${[10, 50, 90][moment]}% 위치의 표본`;
    document.getElementById('frame-source').href = `sources/observations.html#ep${episode}-f${item.frame}`;
    const state = document.getElementById('state-values'); state.replaceChildren();
    item.state.forEach((value, i) => {
      const cell = document.createElement('div');
      const label = document.createElement('small'); label.textContent = i < 6 ? `J${i + 1} / rad` : 'GRIPPER / m';
      const number = document.createElement('span'); number.textContent = value.toFixed(i < 6 ? 5 : 6);
      cell.append(label, number); state.append(cell);
    });
  }
  choices('episode-options', 'episode', episodes, episodes.map(String), episode, value => { episode = value; });
  choices('moment-options', 'moment', [0, 1, 2], ['10%', '50%', '90%'], moment, value => { moment = value; });
  render();
}
