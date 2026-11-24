import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9244;
const MOCK_PORT = 8998;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-web-search`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const mock = http.createServer((req, res) => {
  let body = "";
  req.on("data", chunk => body += chunk);
  req.on("end", () => {
    const parsed = JSON.parse(body || "{}");
    const done = parsed.messages?.at(-1)?.role === "tool";
    const events = done ? [
      {choices: [{delta: {content: "SEARCH_BROWSER_DONE"}, index: 0}]},
      {choices: [{delta: {}, finish_reason: "stop", index: 0}]},
    ] : [
      {choices: [{delta: {tool_calls: [{index: 0, id: "call_browser_search", function: {
        name: "web_search", arguments: JSON.stringify({query: "cavity browser", max_results: 2}),
      }}]}, index: 0}]},
      {choices: [{delta: {}, finish_reason: "tool_calls", index: 0}]},
    ];
    const payload = events.map(event => `data: ${JSON.stringify(event)}\n\n`).join("");
    res.writeHead(200, {"Content-Type": "text/event-stream", "Content-Length": Buffer.byteLength(payload)});
    res.end(payload);
  });
});
await new Promise(resolve => mock.listen(MOCK_PORT, "127.0.0.1", resolve));
const getJson = path => new Promise((resolve, reject) => {
  http.get({host: "127.0.0.1", port: PORT, path}, res => {
    let body = ""; res.on("data", chunk => body += chunk);
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
}).then(async r => ({status: r.status, body: await r.json().catch(() => ({}))}))`);

try {
  await send("Page.enable");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`document.readyState === 'complete' && (!!document.querySelector('form') || !!document.querySelector('.dv-tab'))`);
  const email = `web-search-ui-${Date.now()}@vibeflow.cn`;
  const registered = await evalJs(`fetch('/api/v1/auth/register', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email: '${email}', name: 'web-search-ui', password: 'web-search-ui-pass-123'})
  }).then(async r => ({status: r.status, body: await r.json()}))`);
  if (registered.status !== 200) throw new Error("独立用户注册失败: " + JSON.stringify(registered));
  await evalJs(`localStorage.setItem('vf_token', ${JSON.stringify(registered.body.token)})`);
  const workspaces = await api("/api/v1/workspaces");
  const workspace = workspaces.body[0];
  const caseName = `web-search-ui-${Date.now()}`;
  const created = await api(`/api/v1/workspaces/${workspace.id}/cases`, {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const profile = await api("/api/v1/provider-profiles", {method: "POST", body: {
    provider: "custom", name: `web-search-ui-${Date.now()}`, base_url: `http://127.0.0.1:${MOCK_PORT}`,
    default_model: "mock-search-ui", api_key: "mock-key",
  }});
  const profileId = profile.body.id;
  const conversation = await api("/api/v1/conversations", {method: "POST", body: {
    case_id: caseId, title: "Web search UI", provider_profile_id: profileId, model: "mock-search-ui",
  }});
  const cid = conversation.body.id;
  const openConversation = async () => {
    await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
    await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
    await waitFor(`[...document.querySelectorAll('.dv-tab')].some(t => t.textContent?.trim() === 'Agent')`);
    await evalJs(`(() => {
      const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === 'Agent');
      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, {bubbles: true, button: 0}));
      }
    })()`);
    await waitFor(`[...document.querySelector('[data-testid="agent-conversation"]').options].some(o => o.value === '${cid}')`);
    await evalJs(`(() => {
      for (const [selector, value] of [['[data-testid="agent-profile"]', '${profileId}'], ['[data-testid="agent-conversation"]', '${cid}']]) {
        const select = document.querySelector(selector);
        Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, value);
        select.dispatchEvent(new Event('change', {bubbles: true}));
      }
    })()`);
  };
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await openConversation();
  await evalJs(`(() => {
    const input = document.querySelector('[data-testid="agent-input"]');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, 'search');
    input.dispatchEvent(new Event('input', {bubbles: true}));
    document.querySelector('[data-testid="agent-send"]').click();
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="chat-tool-card"][data-status="completed"][data-kind="globe"]')`);
  await waitFor(`document.querySelector('[data-testid="chat-tool-card"][data-kind="globe"]').textContent.includes('provider=mock')`);
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await openConversation();
  await waitFor(`!!document.querySelector('[data-testid="chat-tool-card"][data-status="completed"][data-kind="globe"]')`);
  const text = await evalJs(`document.querySelector('[data-testid="chat-tool-card"][data-kind="globe"]').textContent`);
  if (!text.includes("已搜索网页") || !text.includes("已完成") || !text.includes("非实时联网")) {
    throw new Error("web_search 持久化卡片不正确: " + text);
  }
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp10-web-search.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("Web search globe card/completed/reload persistence: OK", {caseId, cid});
} catch (error) {
  console.error("WEB SEARCH E2E FAIL:", error.message, await evalJs(`document.body.textContent.slice(-900)`).catch(() => ""));
  process.exitCode = 1;
} finally {
  ws.close(); edge.kill(); mock.close();
}
