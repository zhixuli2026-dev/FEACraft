import { execFile, execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9246;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-checkpoint`;
const PYTHON = String.raw`D:\vibeflow_reverse\platform\server\.venv\Scripts\python.exe`;
const SEED = String.raw`D:\vibeflow_reverse\platform\tools\seed_checkpoint.py`;
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
  const caseName = `checkpoint-ui-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const conversation = await api("/api/v1/conversations", {method: "POST", body: {
    case_id: caseId, title: "Checkpoint UI", model: "",
  }});
  const cid = conversation.body.id;
  execFileSync(PYTHON, [SEED, String(cid), "checkpoint-ui"], {cwd: String.raw`D:\vibeflow_reverse\platform\server`});
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
  await waitFor(`document.querySelector('[data-testid="chat-message-list"]')?.textContent?.includes('PARTIAL_RECOVERY')`);
  const recovered = await evalJs(`[...document.querySelectorAll('[data-testid="chat-message"]')].filter(m => m.textContent.includes('PARTIAL_RECOVERY')).length`);
  if (recovered !== 1) throw new Error("中断半截回答数量不是 1: " + recovered);
  const text = await evalJs(`document.querySelector('[data-testid="chat-message-list"]').textContent`);
  if (!text.includes('已中断')) throw new Error("未显示已中断状态: " + text);
  const runTiming = await evalJs(`(() => {
    const message = [...document.querySelectorAll('[data-testid="chat-message"]')]
      .find(m => m.textContent.includes('PARTIAL_RECOVERY'));
    const timing = message?.querySelector('[data-testid="run-duration"]');
    return timing ? {runId: timing.dataset.runId, status: timing.dataset.status, text: timing.textContent} : null;
  })()`);
  if (!runTiming || runTiming.runId !== 'checkpoint-ui' || runTiming.status !== 'interrupted' || !runTiming.text.includes('秒')) {
    throw new Error("中断回答未按 run_id 显示持久化耗时: " + JSON.stringify(runTiming));
  }
  if (await evalJs(`!!document.querySelector('[data-testid="chat-branch"]')`)) {
    throw new Error("中断回答不应提供分支按钮");
  }
  await evalJs(`document.querySelector('[data-testid="reasoning-toggle"]').click()`);
  await waitFor(`document.querySelector('[data-testid="reasoning-content"]')?.textContent === 'REASONING_RECOVERY'`);
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp10-checkpoint.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("Checkpoint interrupted body/reasoning recovery without duplicate: OK", {caseId, cid});
} catch (error) {
  const debug = await evalJs(`document.body.textContent.slice(-900)`).catch(() => "");
  console.error("CHECKPOINT E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
