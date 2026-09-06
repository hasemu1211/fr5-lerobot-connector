// Preserve the observation being inspected when returning from its source excerpt.
function updateObservationReturn() {
  const section = document.getElementById(location.hash.slice(1));
  const observationLink = section?.previousElementSibling;
  const back = document.querySelector('.source-page > a');
  if (back) {
    back.href = observationLink?.matches('a[href^="../data.html?episode="]')
      ? observationLink.href : '../data.html#observations';
  }
}
window.addEventListener('hashchange', updateObservationReturn);
updateObservationReturn();
