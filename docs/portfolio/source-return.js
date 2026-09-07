// Keep the selected observation or comparison when returning from its source excerpt.
function updateSourceReturn() {
  const section = document.getElementById(location.hash.slice(1));
  const observationLink = section?.previousElementSibling;
  const back = document.querySelector('.source-page > a');
  if (back && new URLSearchParams(location.search).get('view') === 'dependencies') {
    back.href = '../architecture.html#dependencies';
    back.textContent = '← 데이터 흐름과 병렬 경로로 돌아가기';
    return;
  }
  const architectureView = new URLSearchParams(location.search).get('view');
  if (back && architectureView === 'acquisition') {
    const section = new URLSearchParams(location.search).get('section');
    back.href = `../acquisition.html#${['mechanism','native-draft','policy','study'].includes(section) ? section : 'mechanism'}`;
    back.textContent = '← 획득 전략 설명으로 돌아가기';
    return;
  }
  if (back && ['closed-loop', 'study'].includes(architectureView)) {
    back.href = `../architecture.html#${architectureView}`;
    back.textContent = '← 폐루프 설명으로 돌아가기';
    return;
  }
  if (back && ['loss', 'actions'].includes(architectureView)) {
    back.href = `../learning.html#${architectureView === 'loss' ? 'comparison' : 'actions'}`;
    back.textContent = '← 학습 비교로 돌아가기';
    return;
  }
  if (back && architectureView === 'cohort') {
    back.href = '../learning.html#cohort';
    back.textContent = '← 평가 대상 보존으로 돌아가기';
    return;
  }
  if (back && architectureView === 'task-language') {
    back.href = '../collection.html#task-language';
    back.textContent = '← 작업 정의와 언어 조건으로 돌아가기';
    return;
  }
  if (back && architectureView === 'approach') {
    back.href = '../collection.html#approach';
    back.textContent = '← 정렬 동작의 접근 설계로 돌아가기';
    return;
  }
  if (back && architectureView === 'workspace') {
    const yaw = new URLSearchParams(location.search).get('yaw');
    const section = location.hash === '#angle-conditions' ? 'all-angle-condition' : 'workspace';
    back.href = `../collection.html?yaw=${['0','45','90'].includes(yaw) ? yaw : '0'}#${section}`;
    back.textContent = '← 배치·파지 설명으로 돌아가기';
    return;
  }
  if (back && architectureView === 'selection') {
    back.href = '../data.html#selection';
    back.textContent = '← 학습 데이터 선별로 돌아가기';
    return;
  }
  const connection = new URLSearchParams(location.search).get('connection');
  if (back && ['collection', 'selection', 'inspection', 'training'].includes(connection)) {
    back.href = `../architecture.html#handoff-${connection}`;
    back.textContent = '← 시스템 연결 설명으로 돌아가기';
    return;
  }
  const view = new URLSearchParams(location.search).get('view');
  if (back && ['model', 'flow'].includes(view)) {
    back.href = `../learning.html${location.search}#${view}`;
    back.textContent = '← 모델 설명으로 돌아가기';
    return;
  }
  if (back?.pathname.endsWith('/learning.html')) {
    back.search = location.search;
    return;
  }
  if (back?.pathname.endsWith('/data.html')) {
    back.href = observationLink?.matches('a[href^="../data.html?episode="]')
      ? observationLink.href : '../data.html#observations';
  }
}
window.addEventListener('hashchange', updateSourceReturn);
updateSourceReturn();
