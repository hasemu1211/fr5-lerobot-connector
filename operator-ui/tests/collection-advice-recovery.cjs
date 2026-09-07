// Shipped Collection validation, command, recovery and advice rendering against native HTTP.
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const {randomUUID} = require("node:crypto");

(async () => {
  const [origin, script, choice = "APPLY", failRead = "false"] = process.argv.slice(2);
  const html = await (await fetch(origin)).text();
  const token = html.match(/name="operator-token" content="([^"]+)"/)[1];
  const nodes = new Map();
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, {content: token, textContent: "", innerHTML: "", hidden: false, disabled: false});
    return nodes.get(selector);
  };
  const requests = [];
  let drop = false, lost = false;
  const context = vm.createContext({
    document: {querySelector: node}, crypto: {randomUUID}, console, AbortController, DOMException, setTimeout, clearTimeout,
    stopWatch() {}, watchView() {}, setBanner() {},
    fetch: async (path, options = {}) => {
      const method = options.method || "GET";
      requests.push({method, path, payload: options.body ? JSON.parse(options.body).payload : null});
      if (lost && failRead === "true" && method === "GET") throw new Error("synthetic recovery read failure");
      const response = await fetch(origin + path, {...options, headers: {...options.headers, Origin: origin}});
      if (drop && method === "POST") {
        await response.arrayBuffer();
        lost = true;
        throw new Error("synthetic committed response lost");
      }
      return response;
    },
  });
  const source = readFileSync(script, "utf8");
  const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
  vm.runInContext(source.slice(0, source.indexOf("function canIntent"))
    + between("function catalogOption", "function renderCurrentObjectPose")
    + between("function canIntent", "function canImmediateCancel")
    + between("function renderCollectionAdvice", "function render(view)")
    + between("async function submitIntent", "async function submitImmediateCancel")
    + between("async function loadView", '\ndocument.querySelector(".step-rail")')
    + `
      function render(view) { currentView = view; renderCollectionAdvice(view); }
      function failClose(code) { currentView.connection_state = "STALE"; renderCollectionAdvice(currentView); }
      function failViewRequest(error) { if (!currentView) throw error; failClose(String(error)); }
    `, context);
  await vm.runInContext("loadView()", context);
  await vm.runInContext("submitIntent('refresh_collection_advice', {})", context);
  if (node("#collection-advice-apply").disabled) throw new Error(node("#collection-advice-status").textContent);
  requests.length = 0;
  drop = true;
  context.selectedChoice = choice;
  await vm.runInContext("submitIntent('choose_collection_advice', {choice:selectedChoice, expected_recommendation_digest:currentView.collection_advice.recommendation_digest})", context);
  const canonical = await (await fetch(origin + "/api/view", {headers: {"X-Operator-Token": token}})).json();
  console.log(JSON.stringify({requests, canonical,
    status: node("#collection-advice-status").textContent,
    conditions: node("#collection-advice-conditions").innerHTML,
    applyDisabled: node("#collection-advice-apply").disabled,
    applyHidden: node("#collection-advice-apply").hidden,
  }));
})().catch((error) => {console.error(error); process.exitCode = 1;});
