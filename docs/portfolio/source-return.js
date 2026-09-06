// Keep the selected observation or comparison when returning from its source excerpt.
function updateSourceReturn() {
  const section = document.getElementById(location.hash.slice(1));
  const observationLink = section?.previousElementSibling;
  const back = document.querySelector('.source-page > a');
  if (back?.pathname.endsWith('/learning.html')) {
    back.search = location.search;
    return;
  }
  if (back) {
    back.href = observationLink?.matches('a[href^="../data.html?episode="]')
      ? observationLink.href : '../data.html#observations';
  }
}
window.addEventListener('hashchange', updateSourceReturn);
updateSourceReturn();
