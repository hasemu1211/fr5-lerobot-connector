'use strict';
const screens = {
  authoring: '작업·물체·동작 설정과 수집 위치, 각도, 횟수를 선택한다. 자동 선택과 직접 입력은 같은 작업 계획에 반영된다.',
  plan: '시작 전에 각 작업의 위치·각도와 실행 순서를 확인한다. 이 예시는 세 작업으로 구성한 유한한 계획이다.',
  results: '작업별 기술 검사 결과와 수집한 조건을 확인한다. 예시는 FAKE 모드의 세 작업이며 물리 시연의 성공 횟수를 뜻하지 않는다.'
};
let requestVersion = 0;
document.querySelectorAll('[data-screen]').forEach(button => button.addEventListener('click', async () => {
  const version = ++requestVersion;
  const key = button.dataset.screen;
  const src = `assets/collection-${key}.png`;
  const description = document.getElementById('screen-description');
  try {
    const next = new Image(); next.src = src; await next.decode();
    if (version !== requestVersion) return;
    const img = document.getElementById('collection-screen');
    img.src = src; img.alt = `${button.textContent} 단계의 실제 운영 화면. ${screens[key]}`;
    document.getElementById('screen-original').href = src;
    description.textContent = screens[key];
    document.querySelectorAll('[data-screen]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
  } catch {
    if (version === requestVersion) description.textContent = '선택한 화면을 읽지 못했다. 현재 화면을 유지하며 다른 단계를 선택할 수 있다.';
  }
}));
