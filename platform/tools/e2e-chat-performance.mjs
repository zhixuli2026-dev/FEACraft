import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9238;
const MOCK_PORT = 8998;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-chat-performance`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const largeCode = "```python\n" + "print('value')\n".repeat(3000) + "$x$\nEND_CODE\n```";
const fullResponse = "BEGIN_MARK" + "x".repeat(600000) + "END_MARK";
const mock = http.createServer((req, res) => {
  let body = "";
  req.on("data", chunk => body += chunk);
  req.on("end", () => {
    const parsed = JSON.parse(body || "{}");
    const lastUser = [...(parsed.messages ?? [])].reverse().find(message => message.role === "user")?.content ?? "";
    const content = lastUser === "large code" ? largeCode : fullResponse;
    res.writeHead(200, {"Content-Type": "text/event-stream"});
    res.write(`data: ${JSON.stringify({choices: [{delta: {content}, index: 0}]})}\n\n`);
    res.write(`data: ${JSON.stringify({choices: [{delta: {}, finish_reason: "stop", index: 0}]})}\n\n`);
    res.end();
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
const waitFor = async (expression, timeout = 40000) => {
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
const sendText = async text => {
  await waitFor(`!document.querySelector('[data-testid="agent-input"]').disabled`);
  await evalJs(`(() => {
    const input = document.querySelector('[data-testid="agent-input"]');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, ${JSON.stringify(text)});
    input.dispatchEvent(new Event('input', {bubbles: true}));
    document.querySelector('[data-testid="agent-send"]').click();
  })()`);
  await waitFor(`!document.querySelector('[data-testid="agent-input"]').disabled`);
};

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
  const caseName = `chat-performance-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const profile = await api("/api/v1/provider-profiles", {method: "POST", body: {
    provider: "custom", name: `chat-performance-${Date.now()}`, base_url: `http://127.0.0.1:${MOCK_PORT}`,
    default_model: "mock-performance", api_key: "mock-key",
  }});
  const profileId = profile.body.id;
  const conversation = await api("/api/v1/conversations", {method: "POST", body: {
    case_id: caseId, title: "Chat performance", provider_profile_id: profileId, model: "mock-performance",
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
  })()`);
  await sendText("large code");
  await waitFor(`!!document.querySelector('[data-testid="chat-code-degraded"]')`);
  const codeState = await evalJs(`(() => {
    const code = document.querySelector('[data-testid="chat-code-degraded"]');
    return {text: code.textContent, katex: !!code.querySelector('.katex')};
  })()`);
  if (codeState.katex || !codeState.text.includes("END_CODE") || !codeState.text.includes("$x$")) {
    throw new Error("大代码块未按纯文本降级: " + JSON.stringify(codeState).slice(0, 300));
  }
  console.log("Large code uses plain-text degradation: OK", {caseId, cid});

  await sendText("large response");
  await waitFor(`[...document.querySelectorAll('[data-testid="chat-markdown"]')].at(-1)?.dataset.truncated === 'true'`);
  const visible = await evalJs(`document.querySelector('[data-testid="chat-message-list"]').textContent`);
  if (visible.includes("BEGIN_MARK") || !visible.includes("END_MARK") || !visible.includes("仅显示末 512 KiB")) {
    throw new Error("长回答截断提示/末段不正确");
  }
  await send("Browser.grantPermissions", {origin: "http://localhost:5173", permissions: ["clipboardReadWrite", "clipboardSanitizedWrite"]});
  await evalJs(`[...document.querySelectorAll('[data-testid="chat-copy"]')].at(-1).click()`);
  const clipboard = await evalJs(`navigator.clipboard.readText()`);
  if (clipboard.length !== fullResponse.length || !clipboard.startsWith("BEGIN_MARK") || !clipboard.endsWith("END_MARK")) {
    throw new Error(`复制全文长度不符: ${clipboard.length} != ${fullResponse.length}`);
  }
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp10-chat-performance.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("Long answer truncated render/full-content copy: OK", {caseId, cid, copied: clipboard.length});
} catch (error) {
  const debug = await evalJs(`document.body.textContent.slice(-900)`).catch(() => "");
  console.error("CHAT PERFORMANCE E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
  mock.close();
}
