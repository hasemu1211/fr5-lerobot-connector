// Shipped JS against real LoopbackBridge; only the DOM and lost response are injected.
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const {randomUUID} = require("node:crypto");
(async () => {
  const [origin, script, mode] = process.argv.slice(2);
  const html = await (await fetch(origin)).text();
  const token = html.match(/name="operator-token" content="([^"]+)"/)[1];
  const nodes = new Map(), requests = [], stages = [], decisionViews = [];
  const make = () => ({dataset: {}, children: [], listeners: {}, textContent: "", hidden: false,
    addEventListener(name, call) { this.listeners[name] = call; },
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; }});
  const element = id => { if (!nodes.has(id)) nodes.set(id, make()); return nodes.get(id); };
  let pagehide, dropped = false;
  vm.runInNewContext(readFileSync(script, "utf8"), {
    document: {querySelector: () => ({content: token}), getElementById: element, createElement: make},
    crypto: {randomUUID}, AbortSignal, setTimeout, clearTimeout,
    window: {addEventListener: (_name, call) => { pagehide = call; }},
    fetch: async (path, options = {}) => {
      const intent = options.body && JSON.parse(options.body);
      if (intent) requests.push(intent);
      const response = await fetch(origin + path, {...options, headers: {...options.headers, Origin: origin}});
      if (mode === "lost" && intent?.op === "approve_exact_plan" && !dropped) {
        await response.arrayBuffer(); dropped = true; throw new Error("synthetic response loss");
      }
      return response;
    },
  });
  const deadline = Date.now() + 30000;
  let chunks = 0;
  try {
    while (element("learned-terminal").hidden !== false || !element("learned-outcome").textContent) {
      if (Date.now() > deadline) throw new Error("journey timeout: " + element("learned-connection").textContent + " " + element("learned-stage").textContent);
      const buttons = element("learned-actions").children.filter(b => !b.disabled);
      const stage = element("learned-stage").textContent;
      if (stage && stages.at(-1) !== stage) stages.push(stage);
      let button = buttons.find(b => b.dataset.op === "start_learned_run" || b.dataset.op === "approve_exact_plan");
      if (!button && buttons.some(b => b.dataset.choice === "CONFIRM")) button = buttons.find(b => b.dataset.choice === "CONFIRM");
      if (!button && buttons.some(b => b.dataset.choice === "CONTINUE")) {
        button = buttons.find(b => b.dataset.choice === (chunks++ === 0 ? "CONTINUE" : "PASS"));
      }
      if (!button && buttons.some(b => b.dataset.choice === "APPROVE")) button = buttons.find(b => b.dataset.choice === (mode === "cancel" ? "CANCEL" : "APPROVE"));
      if (button) {
        decisionViews.push({op: button.dataset.op, choice: button.dataset.choice,
          nativeView: await (await fetch(origin + "/api/view", {headers: {"X-Operator-Token": token}})).text(),
          plan: JSON.parse(element("learned-plan").textContent || "{}"),
          binding: element("learned-binding").textContent,
          summary: element("learned-plan-summary").textContent});
        await button.listeners.click();
      }
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    const canonical = await (await fetch(origin + "/api/view", {headers: {"X-Operator-Token": token}})).json();
    console.log(JSON.stringify({requests, stages, decisionViews, dropped, canonical,
      outcome: element("learned-outcome").textContent, history: element("learned-history").textContent}));
  } finally { pagehide(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
