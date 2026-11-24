import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const CDP_PORT = 9257;
const MOCK_PORT = 9927;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-parallel`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const sequences = {
  RUN_A: [["A1", 0], ["A2", 250], ["A_DONE", 250]],
  RUN_B: [["B1", 0], ["B_DONE", 150]],
  LONG_A: [["LONG_A_PARTIAL", 0], ["LONG_A_DONE", 4000]],
  LONG_B: [["LONG_B_PARTIAL", 0], ["LONG_B_DONE", 4000]],
};
const mock = http.createServer(async (req, res) => {
  let body = "";
  for await (const chunk of req) body += chunk;
  const request = JSON.parse(body || "{}");
  const content = request.messages?.at(-1)?.content ?? "";
  const sequence = sequences[content] ?? [["DEFAULT", 0]];
  res.writeHead(200, {"Content-Type": "text/event-stream", "Cache-Control": "no-cache"});
  for (const [text, delay] of sequence) {
    if (delay) await sleep(delay);
    if (res.destroyed) return;
    res.write(`data: ${JSON.stringify({choices: [{delta: {content: text}, index: 0}]})}\n\n`);
  }
  if (!res.destroyed) res.end('data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n');
});
await new Promise(resolve => mock.listen(MOCK_PORT, "127.0.0.1", resolve));

const getJson = path => new Promise((resolve, reject) => {
  http.get({host: "127.0.0.1", port: CDP_PORT, path}, res => {
    let body = ""; res.on("data", chunk => body += chunk);
    res.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});
const edge = execFile(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${CDP_PORT}`,
  `--user-data-dir=${profileDir}`, "--window-size=1500,900", "about:blank"], () => {});
let targets;
for (let i = 0; i < 40; i++) {
  try { targets = await getJson("/json/list"); break; } catch { await sleep(250); }
}
if (!targets) throw new Error("Edge CDP 未就绪");
const ws = new WebSocket(targets.find(target => target.type === "page").webSocketDebuggerUrl);
await new Promise(resolve => ws.on("open", resolve));
let id = 0;
const sendCdp = (method, params = {}) => new Promise((resolve, reject) => {
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
  const result = await sendCdp("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails).slice(0, 900));
  return result.result.value;
};
const waitFor = async (expression, timeout = 20000) => {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    if (await evalJs(expression)) return;
    await sleep(100);
  }
  throw new Error("等待超时: " + expression);
};
const api = (path, options = {}) => evalJs(`fetch(${JSON.stringify(path)}, {
  headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
  ${options.method ? `method: ${JSON.stringify(options.method)},` : ""}
  ${options.body ? `body: JSON.stringify(${JSON.stringify(options.body)}),` : ""}
}).then(async r => { const j = await r.json().catch(() => ({})); return {status: r.status, body: j}; })`);
const selectConversation = async cid => {
  await waitFor(`[...document.querySelector('[data-testid="agent-conversation"]').options].some(o => o.value === '${cid}')`);
  await evalJs(`(() => {
    const select = document.querySelector('[data-testid="agent-conversation"]');
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, '${cid}');
    select.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  await waitFor(`document.querySelector('[data-testid="agent-conversation"]').value === '${cid}' && !document.querySelector('[data-testid="agent-conversation"]').disabled`);
};
const sendMessage = async text => {
  await evalJs(`(() => {
    const input = document.querySelector('[data-testid="agent-input"]');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, ${JSON.stringify(text)});
    input.dispatchEvent(new Event('input', {bubbles: true}));
  })()`);
  await waitFor(`!document.querySelector('[data-testid="agent-send"]').disabled`);
  await evalJs(`document.querySelector('[data-testid="agent-send"]').click()`);
};
const visibleText = `document.querySelector('[data-testid="chat-message-list"]').textContent`;

try {
  await sendCdp("Page.enable");
  await sendCdp("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`document.readyState === 'complete' && (!!document.querySelector('form') || !!document.querySelector('[data-case-name]'))`);
  const email = `parallel-ui-${Date.now()}@vibeflow.cn`;
  const registered = await evalJs(`fetch('/api/v1/auth/register', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email: '${email}', name: 'parallel-ui', password: 'parallel-ui-pass-123'})
  }).then(async r => ({status: r.status, body: await r.json()}))`);
  if (registered.status !== 200) throw new Error("独立用户注册失败: " + JSON.stringify(registered));
  await evalJs(`localStorage.setItem('vf_token', ${JSON.stringify(registered.body.token)})`);
  const workspaces = await api("/api/v1/workspaces");
  const workspace = workspaces.body[0];
  const caseName = `parallel-ui-${Date.now()}`;
  const created = await api(`/api/v1/workspaces/${workspace.id}/cases`, {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const profile = await api("/api/v1/provider-profiles", {method: "POST", body: {
    provider: "custom", name: `parallel-${Date.now()}`, base_url: `http://127.0.0.1:${MOCK_PORT}`,
    default_model: "mock-parallel", api_key: "mock-key",
  }});
  const profileId = profile.body.id;
  if (!profileId) throw new Error("测试 profile 创建失败: " + JSON.stringify(profile));
  const makeConv = title => api("/api/v1/conversations", {method: "POST", body: {
    case_id: caseId, title, provider_profile_id: profileId, model: "mock-parallel",
  }});
  const convA = (await makeConv("Parallel A")).body.id;
  const convB = (await makeConv("Parallel B")).body.id;
  if (!convA || !convB) throw new Error("测试会话创建失败");
  await sendCdp("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await waitFor(`[...document.querySelectorAll('.dv-tab')].some(t => t.textContent?.trim() === 'Agent')`);
  await evalJs(`(() => {
    const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === 'Agent');
    const r = tab.getBoundingClientRect(); const x = r.x + r.width / 2, y = r.y + r.height / 2;
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
        {bubbles: true, clientX: x, clientY: y, button: 0}));
    }
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="agent-profile"]')`);
  await selectConversation(convA);
  await waitFor(`[...document.querySelector('[data-testid="agent-profile"]').options].some(o => o.value === '${profileId}')`);
  await evalJs(`(() => {
    const select = document.querySelector('[data-testid="agent-profile"]');
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, '${profileId}');
    select.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  await waitFor(`document.querySelector('[data-testid="agent-profile"]').value === '${profileId}' && document.querySelector('[data-testid="conv-model"]').value === 'mock-parallel'`);
  await sendMessage("RUN_A");
  await waitFor(`${visibleText}.includes('A1')`);
  await selectConversation(convB);
  if (await evalJs(`${visibleText}.includes('A1')`)) throw new Error("A 输出泄漏到 B");
  await sendMessage("RUN_B");
  await waitFor(`${visibleText}.includes('B_DONE')`);
  await sleep(500);
  if (await evalJs(`${visibleText}.includes('A_DONE')`)) throw new Error("后台 A 完成文本写入 B");
  await selectConversation(convA);
  await waitFor(`${visibleText}.includes('A_DONE')`);
  if (await evalJs(`${visibleText}.includes('B_DONE')`)) throw new Error("B 文本写入 A");
  console.log("1. A/B 并行输出归属、后台继续与返回恢复 [OK]", {convA, convB});

  await sendMessage("LONG_A");
  await waitFor(`${visibleText}.includes('LONG_A_PARTIAL')`);
  await selectConversation(convB);
  await sendMessage("LONG_B");
  await waitFor(`${visibleText}.includes('LONG_B_PARTIAL')`);
  await waitFor(`!document.querySelector('[data-testid="agent-stop"]').disabled`);
  await evalJs(`document.querySelector('[data-testid="agent-stop"]').click()`);
  await waitFor(`${visibleText}.includes('已取消')`);
  await selectConversation(convA);
  await waitFor(`${visibleText}.includes('LONG_A_DONE')`);
  const finalB = await api(`/api/v1/conversations/${convB}`);
  const finalA = await api(`/api/v1/conversations/${convA}`);
  const runsB = finalB.body.events.filter(e => e.type === 'run_state').map(e => e.payload.status);
  const runsA = finalA.body.events.filter(e => e.type === 'run_state').map(e => e.payload.status);
  if (runsB.at(-1) !== 'cancelled' || runsA.at(-1) !== 'completed') {
    throw new Error(`停止归属错误: A=${runsA}, B=${runsB}`);
  }
  const shot = await sendCdp("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/f39-parallel-sessions.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("2. 停止 B 仅取消 B，A 继续完成；DB 终态一致 [OK]");
} catch (error) {
  const debug = await evalJs(`JSON.stringify({chat: document.querySelector('[data-testid="chat-message-list"]')?.textContent, status: document.querySelector('[data-testid="profile-status"]')?.textContent, profile: document.querySelector('[data-testid="agent-profile"]')?.value, cid: document.querySelector('[data-testid="agent-conversation"]')?.value, streaming: !document.querySelector('[data-testid="agent-stop"]')?.disabled})`).catch(() => "");
  console.error("PARALLEL SESSIONS E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close(); edge.kill();
  await new Promise(resolve => mock.close(resolve));
}
