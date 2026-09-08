// Shipped view validation, review rendering and lost-response handling against native HTTP.
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const {randomUUID} = require("node:crypto");

(async () => {
  const [origin, script, runId, mode = "review"] = process.argv.slice(2);
  const html = await (await fetch(origin)).text();
  const token = html.match(/name="operator-token" content="([^"]+)"/)[1];
  const nodes = new Map();
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, {content: token, textContent: "", innerHTML: "", value: "", dataset: {},
      hidden: false, disabled: false, focus() {}});
    return nodes.get(selector);
  };
  const methods = [];
  let drop = false;
  const context = vm.createContext({
    document: {querySelector: node, activeElement: null}, crypto: {randomUUID}, console,
    AbortController, DOMException, URL, setTimeout, clearTimeout, selectedRun: runId,
    stopWatch() {}, watchView() {}, setBanner() {},
    fetch: async (path, options = {}) => {
      const method = options.method || "GET";
      methods.push(method);
      const response = await fetch(origin + path, {...options, headers: {...options.headers, Origin: origin}});
      if (drop && method === "POST") {
        await response.arrayBuffer();
        throw new Error("synthetic committed review response lost");
      }
      return response;
    },
  });
  const source = readFileSync(script, "utf8");
  vm.runInContext(readFileSync(require("node:path").join(require("node:path").dirname(script), "messages.js"), "utf8"), context);
  const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
  vm.runInContext(source.slice(0, source.indexOf("function canIntent"))
    + between("function catalogOption", "function renderCurrentObjectPose")
    + between("function canIntent", "function canImmediateCancel")
    + between("function measurementLabel", "function renderNext")
    + between("async function submitIntent", "async function submitImmediateCancel")
    + between("async function loadView", '\ndocument.querySelector(".step-rail")')
    + `
      function render(view) { currentView = view; renderResults(view); }
      function failClose(code) { currentView.connection_state = "STALE"; renderResults(currentView); }
      function failViewRequest(error) { if (!currentView) throw error; failClose(String(error)); }
    `, context);
  await vm.runInContext("loadView()", context);
  await vm.runInContext("submitIntent('refresh_stored_reviews', {})", context);
  await vm.runInContext("submitIntent('select_stored_review', {run_id:selectedRun})", context);
  if (mode === "batch") await vm.runInContext("submitIntent('freeze_review_batch', {run_ids:currentView.stored_reviews.episodes.map(item=>item.run_id)})", context);
  if (mode === "return") await vm.runInContext("submitIntent('inspect_stored_episode', {review_binding_digest:currentView.candidate_review.review_binding_digest})", context);
  methods.length = 0;
  drop = true;
  if (mode === "batch") await vm.runInContext("submitIntent('review_stored_batch', {batch_binding_digest:currentView.stored_reviews.batch.selection.batch_binding_digest,choice:'PASS',reason:null,excluded_run_ids:[]})", context);
  else if (mode === "review") await vm.runInContext("submitIntent('review_candidate', {review_binding_digest:currentView.candidate_review.review_binding_digest,choice:'PASS',reason:null})", context);
  else await vm.runInContext(`submitIntent('${mode === "return" ? "return_stored_review" : "inspect_stored_episode"}', {review_binding_digest:currentView.candidate_review.review_binding_digest})`, context);
  console.log(JSON.stringify({methods, review: vm.runInContext("currentView.candidate_review", context),
    inspection: vm.runInContext("currentView.stored_reviews.inspection", context),
    batch: vm.runInContext("currentView.stored_reviews.batch", context),
    batchCard: node("#batch-frozen-items").innerHTML,
    card: node("#review-queue").innerHTML}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
