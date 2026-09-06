// Replay the shipped UI against a temporary real bridge; lose one response after it arrives.
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const {randomUUID} = require("node:crypto");

(async () => {
  const [origin, script, action, failRead] = process.argv.slice(2);
  const html = await (await fetch(origin)).text();
  const token = html.match(/name="operator-token" content="([^"]+)"/)[1];
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      hidden: true, disabled: false, textContent: "",
      addEventListener(_name, callback) { this.click = callback; },
      replaceChildren() {},
    });
    return elements.get(id);
  };
  const requests = [];
  let dropResponse = false;
  let responseLost = false;
  const request = async (path, options = {}) => {
    const method = options.method || "GET";
    requests.push({method, path});
    if (responseLost && failRead === "true" && method === "GET") throw new Error("fixture read unavailable");
    const response = await fetch(origin + path, {...options,
      headers: {...options.headers, Origin: origin}});
    if (dropResponse && method === "POST") {
      await response.arrayBuffer();
      responseLost = true;
      throw new Error("fixture response lost");
    }
    return response;
  };
  vm.runInNewContext(readFileSync(script, "utf8"), {
    document: {querySelector: () => ({content: token}), getElementById: element,
      createElement: () => ({})},
    fetch: request, crypto: {randomUUID},
  });
  const deadline = Date.now() + 5000;
  while (element("training-refresh").disabled) {
    if (Date.now() > deadline) throw new Error("UI initialization did not finish");
    await new Promise(setImmediate);
  }
  if (action !== "prepare") await element("training-prepare").click();
  requests.length = 0;
  dropResponse = true;
  await element(`training-${action}`).click();
  const canonical = await (await fetch(origin + "/api/view", {
    headers: {"X-Operator-Token": token},
  })).json();
  console.log(JSON.stringify({requests, canonical, status: element("training-status").textContent,
    visibleActions: ["prepare", "approve", "refuse"].filter((id) => !element(`training-${id}`).hidden),
    refreshEnabled: !element("training-refresh").disabled}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
