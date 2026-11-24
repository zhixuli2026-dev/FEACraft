import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9240;
const MOCK_PORT = 9000;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-profile-models`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const mock = http.createServer((req, res) => {
  if (req.url !== "/v1/models") {
    res.writeHead(404);
    res.end();
    return;
  }
  const auth = req.headers.authorization ?? "";
  if (auth !== "Bearer mock-key") {
    res.writeHead(401);
    res.end();
    return;
  }
  res.writeHead(200, {"Content-Type": "application/json"});
  res.end(JSON.stringify({data: [{id: "model-a"}, {id: "model-b"}]}));
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
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails).slice(0, 800) + "\nEXPR: " + expression.slice(0, 300));
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
const setInput = async (selector, value) => {
  await evalJs(`(() => {
    const input = document.querySelector(${JSON.stringify(selector)});
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, ${JSON.stringify(value)});
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
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
  const caseName = `profile-ui-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const profile = await api("/api/v1/provider-profiles", {method: "POST", body: {
    provider: "custom", name: "EditableProfile", base_url: `http://127.0.0.1:${MOCK_PORT}`,
    default_model: "model-a", api_key: "mock-key",
  }});
  const profileId = profile.body.id;
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
  await evalJs(`(() => {
    const select = document.querySelector('[data-testid="agent-profile"]');
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, '${profileId}');
    select.dispatchEvent(new Event('change', {bubbles: true}));
    document.querySelector('[data-testid="profile-edit"]').click();
  })()`);
  await setInput('[data-testid="profile-edit-name"]', "EditedProfile");
  await setInput('[data-testid="profile-default-model"]', "model-b");
  await evalJs(`document.querySelector('[data-testid="profile-save"]').click()`);
  await waitFor(`document.querySelector('[data-testid="profile-status"]')?.textContent === 'profile 已保存'`);
  await evalJs(`document.querySelector('[data-testid="profile-edit"]').click()`);
  await evalJs(`document.querySelector('[data-testid="profile-test"]').click()`);
  await waitFor(`document.querySelector('[data-testid="profile-status"]')?.textContent?.includes('连接成功')`);
  await evalJs(`document.querySelector('[data-testid="profile-models"]').click()`);
  await waitFor(`document.querySelector('[data-testid="profile-status"]')?.textContent === '已获取 2 个模型'`);
  const convModel = await evalJs(`document.querySelector('[data-testid="conv-model"]').value`);
  if (convModel !== "model-b") throw new Error("默认模型未更新: " + convModel);
  await evalJs(`[...document.querySelectorAll('button')].find(x => x.textContent === '新会话').click()`);
  let conversations = { body: [] };
  for (let i = 0; i < 40; i++) {
    conversations = await api(`/api/v1/conversations?caseId=${caseId}`);
    if (conversations.body.length) break;
    await sleep(250);
  }
  const createdConv = conversations.body.find(conv => conv.title.startsWith("会话"));
  if (!createdConv || createdConv.model !== "model-b") {
    throw new Error("新会话未使用动态 model-b: " + JSON.stringify(conversations.body));
  }
  console.log("Profile edit/test/dynamic model/new conversation: OK", {caseId, profileId});

  await evalJs(`(() => {
    if (!document.querySelector('[data-testid="profile-base-url"]')) {
      document.querySelector('[data-testid="profile-edit"]').click();
    }
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="profile-base-url"]')`);
  await setInput('[data-testid="profile-base-url"]', "http://127.0.0.1:1");
  await evalJs(`document.querySelector('[data-testid="profile-save"]').click()`);
  await waitFor(`document.querySelector('[data-testid="profile-status"]')?.textContent === 'profile 已保存'`);
  await evalJs(`document.querySelector('[data-testid="profile-models"]').click()`);
  await waitFor(`document.querySelector('[data-testid="profile-status"]')?.textContent?.includes('模型列表回退')`);
  const fallbackModel = await evalJs(`document.querySelector('[data-testid="conv-model"]').value`);
  if (fallbackModel !== "model-b") throw new Error("离线回退清空或改变当前模型: " + fallbackModel);
  await evalJs(`(() => {
    if (!document.querySelector('[data-testid="profile-base-url"]')) {
      document.querySelector('[data-testid="profile-edit"]').click();
    }
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="profile-base-url"]')`);
  await setInput('[data-testid="profile-base-url"]', `http://127.0.0.1:${MOCK_PORT}`);
  await evalJs(`document.querySelector('[data-testid="profile-save"]').click()`);
  await waitFor(`document.querySelector('[data-testid="profile-status"]')?.textContent === 'profile 已保存'`);
  await evalJs(`document.querySelector('[data-testid="profile-edit"]').click()`);
  await evalJs(`document.querySelector('[data-testid="profile-test"]').click()`);
  await waitFor(`document.querySelector('[data-testid="profile-status"]')?.textContent?.includes('连接成功')`);
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp10-profile-models.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("Offline fallback preserves model and recovers: OK", {caseId, profileId});
} catch (error) {
  const debug = await evalJs(`document.body.textContent.slice(-900)`).catch(() => "");
  console.error("PROFILE UI E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
  mock.close();
}
