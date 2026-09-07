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
  const connection = new URLSearchParams(location.search).get('connection');
  if (back && ['collection', 'selection', 'inspection', 'training'].includes(connection)) {
    back.href = `../architecture.html#handoff-${connection}`;
    back.textContent = '← 시스템 연결 설명으로 돌아가기';
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
