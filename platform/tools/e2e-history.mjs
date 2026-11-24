import { execFile, execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9243;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-history`;
const PYTHON = String.raw`D:\vibeflow_reverse\platform\server\.venv\Scripts\python.exe`;
const SEED = String.raw`D:\vibeflow_reverse\platform\tools\seed_history.py`;
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

try {
  await send("Page.enable");
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
  const caseName = `history-ui-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const conversation = await api("/api/v1/conversations", {method: "POST", body: {
    case_id: caseId, title: "History UI", model: "",
  }});
  const cid = conversation.body.id;
  execFileSync(PYTHON, [SEED, String(cid), "300"], {cwd: String.raw`D:\vibeflow_reverse\platform\server`});
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await waitFor(`[...document.querySelectorAll('.dv-tab')].some(t => t.textContent?.trim() === 'Agent')`);
  await evalJs(`(() => {
    const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === 'Agent');
    const r = tab.getBoundingClientRect();
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
        {bubbles: true, clientX: x, clientY: y, button: 0}));
    }
  })()`);
  await waitFor(`[...document.querySelector('[data-testid="agent-conversation"]').options].some(o => o.value === '${cid}')`);
  await evalJs(`(() => {
    const select = document.querySelector('[data-testid="agent-conversation"]');
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, '${cid}');
    select.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  await waitFor(`document.querySelector('[data-testid="chat-message-list"]')?.textContent?.includes('m299')`);
  if (await evalJs(`document.querySelector('[data-testid="chat-message-list"]').textContent.includes('m0')`)) {
    throw new Error("首屏已包含最早消息，分页无效");
  }
  await waitFor(`!!document.querySelector('[data-testid="chat-load-older"]')`);
  await evalJs(`document.querySelector('[data-testid="chat-load-older"]').click()`);
  await waitFor(`document.querySelector('[data-testid="chat-message-list"]')?.textContent?.includes('m0')`);
  await waitFor(`!document.querySelector('[data-testid="chat-load-older"]')`);
  const count = await evalJs(`document.querySelectorAll('[data-testid="chat-message"]').length`);
  if (count !== 300) throw new Error("加载后消息数量不是 300: " + count);
  const first = await evalJs(`document.querySelector('[data-testid="chat-message"]').textContent`);
  const last = await evalJs(`[...document.querySelectorAll('[data-testid="chat-message"]')].at(-1).textContent`);
  if (!first.includes("m0") || !last.includes("m299")) throw new Error("历史顺序错误: " + first + " / " + last);
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp10-history.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("History latest page/older prepend/order/no duplicates: OK", {caseId, cid, count});
} catch (error) {
  const debug = await evalJs(`document.body.textContent.slice(-900)`).catch(() => "");
  console.error("HISTORY E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
