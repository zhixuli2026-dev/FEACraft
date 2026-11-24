import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9236;
const MOCK_PORT = 8996;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-agent-stop`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const mock = http.createServer((req, res) => {
  let body = "";
  req.on("data", chunk => body += chunk);
  req.on("end", () => {
    const parsed = JSON.parse(body || "{}");
    const fast = parsed.messages?.some(message => message.content === "second run");
    res.writeHead(200, {"Content-Type": "text/event-stream"});
    res.write(`data: ${JSON.stringify({choices: [{delta: {content: fast ? "done" : "partial"}, index: 0}]})}\n\n`);
    const timer = setTimeout(() => {
      res.write(`data: ${JSON.stringify({choices: [{delta: {}, finish_reason: "stop", index: 0}]})}\n\n`);
      res.end();
    }, fast ? 50 : 5000);
    res.on("close", () => clearTimeout(timer));
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
  await waitFor(`!document.querySelector('[data-testid="agent-input"]').disabled`);
};
const sendText = async text => {
  await evalJs(`(() => {
    const input = document.querySelector('[data-testid="agent-input"]');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, ${JSON.stringify(text)});
    input.dispatchEvent(new Event('input', {bubbles: true}));
    document.querySelector('[data-testid="agent-send"]').click();
  })()`);
};

try {
  await send("Page.enable");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`document.readyState === 'complete' && (!!document.querySelector('form') || !!document.querySelector('[data-case-name]'))`);
  const email = `agent-stop-ui-${Date.now()}@vibeflow.cn`;
  const registered = await evalJs(`fetch('/api/v1/auth/register', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email: '${email}', name: 'agent-stop-ui', password: 'agent-stop-ui-pass-123'})
  }).then(async r => ({status: r.status, body: await r.json()}))`);
  if (registered.status !== 200) throw new Error("独立用户注册失败: " + JSON.stringify(registered));
  await evalJs(`localStorage.setItem('vf_token', ${JSON.stringify(registered.body.token)})`);
  const workspaces = await api("/api/v1/workspaces");
  const workspace = workspaces.body[0];
  const caseName = `agent-stop-${Date.now()}`;
  const created = await api(`/api/v1/workspaces/${workspace.id}/cases`, {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const profile = await api("/api/v1/provider-profiles", {method: "POST", body: {
    provider: "custom", name: `agent-stop-${Date.now()}`, base_url: `http://127.0.0.1:${MOCK_PORT}`,
    default_model: "mock-stop", api_key: "mock-key",
  }});
  const profileId = profile.body.id;
  const conversation = await api("/api/v1/conversations", {method: "POST", body: {
    case_id: caseId, title: "Agent stop", provider_profile_id: profileId, model: "mock-stop",
  }});
  const cid = conversation.body.id;
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await activateAgent(caseName, profileId, cid);
  await sendText("start slow run");
  await waitFor(`document.querySelector('[data-testid="chat-message-list"]')?.textContent?.includes('partial')`);
  await evalJs(`document.querySelector('[data-testid="agent-stop"]').click()`);
  await waitFor(`!document.querySelector('[data-testid="agent-input"]').disabled`);
  await waitFor(`document.querySelector('[data-testid="chat-message-list"]')?.textContent?.includes('Agent · 已取消')`);
  const cancelledText = await evalJs(`document.querySelector('[data-testid="chat-message-list"]').textContent`);
  if (!cancelledText.includes("partial")) throw new Error("取消后部分内容丢失");
  console.log("Agent stop marks partial response cancelled: OK", {caseId, cid});

  await send("Page.navigate", {url: "http://localhost:5173/"});
  await activateAgent(caseName, profileId, cid);
  await waitFor(`document.querySelector('[data-testid="chat-message-list"]')?.textContent?.includes('Agent · 已取消')`);
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp10-agent-stop.png", import.meta.url), Buffer.from(shot.data, "base64"));
  await sendText("second run");
  await waitFor(`[...document.querySelectorAll('.chat-md')].some(x => x.textContent.trim() === 'done')`);
  const last = await evalJs(`[...document.querySelectorAll('[data-testid="chat-message"]')].at(-1).textContent`);
  if (last.includes("已取消") || !last.includes("done")) throw new Error("下一回合未正常完成: " + last);
  console.log("Cancelled state persists and next run succeeds: OK", {caseId, cid});
} catch (error) {
  const debug = await evalJs(`document.body.textContent.slice(-900)`).catch(() => "");
  console.error("AGENT STOP E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
  mock.close();
}
