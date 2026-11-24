import { execFile } from "node:child_process";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9232;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-cite`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const getJson = path => new Promise((resolve, reject) => {
  http.get({host: "127.0.0.1", port: PORT, path}, res => {
    let body = "";
    res.on("data", chunk => body += chunk);
    res.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});
const edge = execFile(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profileDir}`, "--window-size=1500,900", "about:blank"], () => {});
let targets;
for (let i = 0; i < 40; i++) {
  try { targets = await getJson("/json/list"); break; } catch { await sleep(250); }
}
if (!targets) throw new Error("Edge CDP 未就绪");
const ws = new WebSocket(targets.find(target => target.type === "page").webSocketDebuggerUrl);
await new Promise(resolve => ws.on("open", resolve));
let id = 0;
let citationMode = "success";
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const mid = ++id;
  const onMessage = data => {
    const message = JSON.parse(data);
    if (message.id === mid) {
      ws.off("message", onMessage);
      message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
    }
  };
  ws.on("message", onMessage);
  ws.send(JSON.stringify({id: mid, method, params}));
});
const evalJs = async expression => {
  const result = await send("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails).slice(0, 800));
  return result.result.value;
};
const waitFor = async (expression, timeout = 30000) => {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    if (await evalJs(expression)) return;
    await sleep(250);
  }
  throw new Error("等待超时: " + expression);
};
const api = (path, options = {}) => evalJs(`fetch(${JSON.stringify(path)}, {
  headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
  ${options.method ? `method: ${JSON.stringify(options.method)},` : ""}
  ${options.body ? `body: JSON.stringify(${JSON.stringify(options.body)}),` : ""}
}).then(async r => { const j = await r.json().catch(() => ({})); return {status: r.status, body: j}; })`);
ws.on("message", data => {
  const message = JSON.parse(data);
  if (message.method !== "Fetch.requestPaused") return;
  const {requestId} = message.params;
  const payload = citationMode === "success" ? {
    status: 200,
    body: {items: [{key_hint: "doe2026", title: "Mocked Semantic Result", authors: ["Jane Doe"], year: "2026",
      id: "doi:10.1000/mock", source: "semanticscholar",
      bibtex: "@article{doe2026,\n  title = {Mocked Semantic Result},\n  author = {Jane Doe},\n  year = {2026},\n  note = {doi:10.1000/mock}\n}"}]},
  } : {status: 503, body: {detail: "Semantic Scholar 限流，请稍后重试"}};
  send("Fetch.fulfillRequest", {
    requestId, responseCode: payload.status,
    responseHeaders: [{name: "Content-Type", value: "application/json"}],
    body: Buffer.from(JSON.stringify(payload.body)).toString("base64"),
  }).catch(error => console.error("Fetch mock failed", error));
});

try {
  await send("Page.enable");
  await send("Fetch.enable", {patterns: [{urlPattern: "*/api/v1/cite/search*"}]});
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`document.readyState === 'complete' && (!!document.querySelector('form') || !!document.querySelector('[data-case-name]'))`);
  if (await evalJs(`!!document.querySelector('form')`)) {
    await evalJs(`(() => {
      const inputs = document.querySelectorAll('input');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(inputs[0], 'dev@vibeflow.cn'); inputs[0].dispatchEvent(new Event('input', {bubbles: true}));
      setter.call(inputs[1], 'devpass123'); inputs[1].dispatchEvent(new Event('input', {bubbles: true}));
      document.querySelector('form').requestSubmit();
    })()`);
    await waitFor(`!document.querySelector('form') && !!document.querySelector('[data-case-name]')`);
  }
  const caseName = `cite-ui-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await waitFor(`[...document.querySelectorAll('.dv-tab')].some(t => t.textContent?.trim() === '公式调色板')`);
  await evalJs(`(() => {
    const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === '公式调色板');
    const r = tab.getBoundingClientRect();
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
        {bubbles: true, clientX: x, clientY: y, button: 0}));
    }
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="cite-source"]')`);
  await evalJs(`(() => {
    const select = document.querySelector('[data-testid="cite-source"]');
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, 'semanticscholar');
    select.dispatchEvent(new Event('change', {bubbles: true}));
    const input = document.querySelector('[data-testid="cite-query"]');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, 'OpenFOAM');
    input.dispatchEvent(new Event('input', {bubbles: true}));
  })()`);
  await evalJs(`document.querySelector('[data-testid="cite-search-run"]').click()`);
  await waitFor(`document.querySelector('[data-testid="cite-online-item"]')?.textContent?.includes('Mocked Semantic Result')`);
  const apaPreview = await evalJs(`document.querySelector('[data-testid="cite-preview"]').textContent`);
  if (!apaPreview.includes('Doe, J. (2026). Mocked Semantic Result.')) throw new Error("APA 预览不正确: " + apaPreview);
  await evalJs(`(() => {
    const select = document.querySelector('[data-testid="cite-style"]');
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, 'ieee');
    select.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  await waitFor(`document.querySelector('[data-testid="cite-preview"]')?.textContent?.includes('[1] J. Doe')`);
  await evalJs(`document.querySelector('[data-testid="cite-add"]').click()`);
  let bib = "";
  for (let i = 0; i < 40; i++) {
    const result = await api(`/api/v1/cases/${caseId}/file?path=` + encodeURIComponent("E-report/latex/refs.bib"));
    bib = result.body.content ?? "";
    if (bib.includes("Mocked Semantic Result")) break;
    await sleep(250);
  }
  if (!bib.includes("Mocked Semantic Result") || !bib.includes("10.1000/mock")) {
    throw new Error("引用未写入 refs.bib: " + bib);
  }
  console.log("Citation UI mocked result and refs.bib append: OK", {caseId});
  citationMode = "error";
  await evalJs(`document.querySelector('[data-testid="cite-search-run"]').click()`);
  await waitFor(`document.querySelector('[data-testid="cite-error"]')?.textContent?.includes('503')`);
  console.log("Citation UI visible 503 failure: OK", {caseId});
} catch (error) {
  const debug = await evalJs(`document.body.textContent.slice(-800)`).catch(() => "");
  console.error("CITE UI E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
