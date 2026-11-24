import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9237;
const MOCK_PORT = 8997;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-chat-markdown`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const mock = http.createServer((req, res) => {
  let body = "";
  req.on("data", chunk => body += chunk);
  req.on("end", () => {
    JSON.parse(body || "{}");
    res.writeHead(200, {"Content-Type": "text/event-stream"});
    const write = (text, finish = false) => {
      const event = finish ? {choices: [{delta: {}, finish_reason: "stop", index: 0}]}
        : {choices: [{delta: {content: text}, index: 0}]};
      res.write(`data: ${JSON.stringify(event)}\n\n`);
    };
    write("First paragraph.\n\n");
    setTimeout(() => write("```python\nprint('x')\n"), 700);
    setTimeout(() => write("\nprint('y')\n```\n\nLast paragraph."), 1400);
    setTimeout(() => { write("", true); res.end(); }, 2100);
  });
});
await new Promise(resolve => mock.listen(MOCK_PORT, "127.0.0.1", resolve));
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
  const caseName = `chat-markdown-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const profile = await api("/api/v1/provider-profiles", {method: "POST", body: {
    provider: "custom", name: `chat-markdown-${Date.now()}`, base_url: `http://127.0.0.1:${MOCK_PORT}`,
    default_model: "mock-markdown", api_key: "mock-key",
  }});
  const profileId = profile.body.id;
  const conversation = await api("/api/v1/conversations", {method: "POST", body: {
    case_id: caseId, title: "Chat markdown", provider_profile_id: profileId, model: "mock-markdown",
  }});
  const cid = conversation.body.id;
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
  await waitFor(`[...document.querySelector('[data-testid="agent-profile"]').options].some(o => o.value === '${profileId}')`);
  await waitFor(`[...document.querySelector('[data-testid="agent-conversation"]').options].some(o => o.value === '${cid}')`);
  await evalJs(`(() => {
    for (const [selector, value] of [['[data-testid="agent-profile"]', '${profileId}'], ['[data-testid="agent-conversation"]', '${cid}']]) {
      const select = document.querySelector(selector);
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, value);
      select.dispatchEvent(new Event('change', {bubbles: true}));
    }
    const input = document.querySelector('[data-testid="agent-input"]');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, 'stream markdown');
    input.dispatchEvent(new Event('input', {bubbles: true}));
  })()`);
  await waitFor(`!document.querySelector('[data-testid="agent-input"]').disabled`);
  await evalJs(`document.querySelector('[data-testid="agent-send"]').click()`);
  await waitFor(`document.querySelector('[data-testid="chat-block"][data-block-index="0"]')?.textContent.trim() === 'First paragraph.'`);
  await evalJs(`(() => { window.__stableFirstBlock = document.querySelector('[data-block-index="0"]'); return true; })()`);
  await waitFor(`document.querySelector('[data-testid="chat-message-list"]')?.textContent?.includes("print('y')")`);
  const preserved = await evalJs(`window.__stableFirstBlock === document.querySelector('[data-block-index="0"]')`);
  if (!preserved) throw new Error("流式追加替换了未变首块 DOM");
  const cursorCount = await evalJs(`document.querySelectorAll('[data-testid="chat-cursor"]').length`);
  if (cursorCount !== 1) throw new Error("流式光标数量不是 1: " + cursorCount);
  await waitFor(`!document.querySelector('[data-testid="agent-input"]').disabled`);
  await waitFor(`document.querySelector('[data-testid="chat-markdown"]')?.dataset.authoritative === 'true'`);
  const replaced = await evalJs(`window.__stableFirstBlock !== document.querySelector('[data-block-index="0"]')`);
  if (!replaced) throw new Error("完成后未从权威全文替换 DOM");
  const completedCursor = await evalJs(`document.querySelectorAll('[data-testid="chat-cursor"]').length`);
  if (completedCursor !== 0) throw new Error("完成后光标未清除");
  const blockCount = await evalJs(`document.querySelectorAll('[data-testid="chat-block"]').length`);
  if (blockCount !== 3) throw new Error("权威全文块数错误: " + blockCount);
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp10-chat-markdown.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("Streaming stable blocks/authoritative rebuild/single cursor: OK", {caseId, cid, blockCount});
} catch (error) {
  const debug = await evalJs(`document.body.textContent.slice(-900)`).catch(() => "");
  console.error("CHAT MARKDOWN E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
  mock.close();
}
