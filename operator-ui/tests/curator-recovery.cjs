// Shipped UI, real native HTTP transaction; discard a response after the owner commits.
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const {randomUUID} = require("node:crypto");

(async () => {
  const [origin, script, action, failRead] = process.argv.slice(2);
  const html = await (await fetch(origin)).text();
  const token = html.match(/name="operator-token" content="([^"]+)"/)[1];
  const elements = new Map();
  const node = () => ({hidden: true, disabled: false, textContent: "", listeners: {},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    children: [], replaceChildren(...items) { this.children = items; },
    append(...items) { this.children.push(...items); },
    removeAttribute(name) { if (name === "src") this.srcValue = null; }, load() {}, pause() {}, focus() {},
    set src(value) { this.srcValue = value; this.listeners.loadeddata(); }});
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, node());
    return elements.get(id);
  };
  const requests = [];
  let drop = false;
  let lost = false;
  vm.runInNewContext(readFileSync(script, "utf8"), {
    document: {querySelector: () => ({content: token}), getElementById: element, createElement: node},
    crypto: {randomUUID}, URL, AbortSignal,
    fetch: async (path, options = {}) => {
      const method = options.method || "GET";
      requests.push({method, path, payload: options.body ? JSON.parse(options.body).payload : null});
      if (lost && failRead === "true" && method === "GET") throw new Error("fixture read unavailable");
      const response = await fetch(origin + path, {...options, headers: {...options.headers, Origin: origin}});
      if (drop && method === "POST") {
        await response.arrayBuffer();
        lost = true;
        throw new Error("fixture response lost");
      }
      return response;
    },
  });
  const deadline = Date.now() + 15000;
  while (element("curator-refresh").disabled) {
    if (Date.now() > deadline) throw new Error("UI initialization timeout");
    await new Promise(setImmediate);
  }
  if (element(`curator-${action}`).hidden || element(`curator-${action}`).disabled) {
    throw new Error("native choice unavailable: " + element("curator-status").textContent);
  }
  const initial = await (await fetch(origin + "/api/view", {headers: {"X-Operator-Token": token}})).json();
  let offset = 0;
  initial.projection.review.clips.forEach((clip, index) => {
    element("curator-clips").children[index].children[0].listeners.click();
    if (element("curator-video").currentTime !== offset) throw new Error("clip seeks source instead of concatenated review time");
    offset += clip.duration_seconds;
  });
  requests.length = 0;
  drop = true;
  await element(`curator-${action}`).listeners.click();
  const canonical = await (await fetch(origin + "/api/view", {headers: {"X-Operator-Token": token}})).json();
  console.log(JSON.stringify({requests, canonical, status: element("curator-status").textContent,
    mediaStatus: element("curator-media-status").textContent, videoSource: element("curator-video").srcValue,
    visibleActions: ["approve", "reject", "recover"].filter((id) => !element(`curator-${id}`).hidden),
    refreshEnabled: !element("curator-refresh").disabled}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
