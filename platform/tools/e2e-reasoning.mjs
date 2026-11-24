import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9234;
const MOCK_PORT = 8993;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-reasoning`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const mock = http.createServer((req, res) => {
  let body = "";
  req.on("data", chunk => body += chunk);
  req.on("end", () => {
    const parsed = JSON.parse(body || "{}");
    if (req.url !== "/v1/chat/completions" || parsed.reasoning_effort !== "medium") {
      res.writeHead(400, {"Content-Type": "application/json"});
      res.end(JSON.stringify({error: "reasoning_effort=medium required"}));
      return;
    }
    res.writeHead(200, {"Content-Type": "text/event-stream"});
    res.write(`data: ${JSON.stringify({choices: [{delta: {reasoning_content: "Mock reasoning process."}, index: 0}]})}\n\n`);
    setTimeout(() => {
      res.write(`data: ${JSON.stringify({choices: [{delta: {content: "Final visible answer."}, index: 0}]})}\n\n`);
      res.write(`data: ${JSON.stringify({choices: [{delta: {}, finish_reason: "stop", index: 0}]})}\n\n`);
      res.end();
    }, 700);
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
const activateAgent = async (caseName, profileId, cid) => {
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
};

try {
  await send("Page.enable");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`document.readyState === 'complete' && (!!document.querySelector('form') || !!document.querySelector('[data-case-name]'))`);
  const email = `reasoning-ui-${Date.now()}@vibeflow.cn`;
  const registered = await evalJs(`fetch('/api/v1/auth/register', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email: '${email}', name: 'reasoning-ui', password: 'reasoning-ui-pass-123'})
  }).then(async r => ({status: r.status, body: await r.json()}))`);
  if (registered.status !== 200) throw new Error("独立用户注册失败: " + JSON.stringify(registered));
  await evalJs(`localStorage.setItem('vf_token', ${JSON.stringify(registered.body.token)})`);
  const workspaces = await api("/api/v1/workspaces");
  const workspace = workspaces.body[0];
  const caseName = `reasoning-ui-${Date.now()}`;
  const created = await api(`/api/v1/workspaces/${workspace.id}/cases`, {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const profile = await api("/api/v1/provider-profiles", {method: "POST", body: {
    provider: "custom", name: `reasoning-ui-${Date.now()}`, base_url: `http://127.0.0.1:${MOCK_PORT}`,
    default_model: "mock-reasoning", api_key: "mock-key",
  }});
  const profileId = profile.body.id;
  const conversation = await api("/api/v1/conversations", {method: "POST", body: {
    case_id: caseId, title: "Reasoning UI", provider_profile_id: profileId, model: "mock-reasoning",
  }});
  const cid = conversation.body.id;
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await activateAgent(caseName, profileId, cid);
  await evalJs(`(() => {
    const effort = document.querySelector('[data-testid="reasoning-effort"]');
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(effort, 'medium');
    effort.dispatchEvent(new Event('change', {bubbles: true}));
    const input = document.querySelector('[data-testid="agent-input"]');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, '请展示推理');
    input.dispatchEvent(new Event('input', {bubbles: true}));
  })()`);
  await waitFor(`!document.querySelector('[data-testid="agent-input"]').disabled`);
  await evalJs(`document.querySelector('[data-testid="agent-send"]').click()`);
  await waitFor(`document.querySelector('[data-testid="reasoning-content"]')?.textContent === 'Mock reasoning process.'`);
  await waitFor(`[...document.querySelectorAll('.chat-md')].some(x => x.textContent.trim() === 'Final visible answer.')`);
  await waitFor(`!document.querySelector('[data-testid="agent-input"]').disabled`);
  await waitFor(`!document.querySelector('[data-testid="reasoning-content"]')`);
  const finalTexts = await evalJs(`[...document.querySelectorAll('.chat-md')].map(x => x.textContent)`);
  if (finalTexts.filter(text => text.trim() === "Final visible answer.").length !== 1) {
    throw new Error("最终回答数量不正确: " + JSON.stringify(finalTexts));
  }
  await evalJs(`document.querySelector('[data-testid="reasoning-toggle"]').click()`);
  await waitFor(`document.querySelector('[data-testid="reasoning-content"]')?.textContent === 'Mock reasoning process.'`);
  console.log("Reasoning live card/final separation: OK", {caseId, cid});

  await send("Page.navigate", {url: "http://localhost:5173/"});
  await activateAgent(caseName, profileId, cid);
  await waitFor(`!!document.querySelector('[data-testid="reasoning-toggle"]')`);
  await evalJs(`document.querySelector('[data-testid="reasoning-toggle"]').click()`);
  await waitFor(`document.querySelector('[data-testid="reasoning-content"]')?.textContent === 'Mock reasoning process.'`);
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp10-reasoning.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("Reasoning persisted collapsed card after reload: OK", {caseId, cid});
} catch (error) {
  const debug = await evalJs(`document.body.textContent.slice(-900)`).catch(() => "");
  console.error("REASONING E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
  mock.close();
}
