import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9235;
const MOCK_PORT = 8994;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-chat-nav`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const mock = http.createServer((req, res) => {
  let body = "";
  req.on("data", chunk => body += chunk);
  req.on("end", () => {
    const parsed = JSON.parse(body || "{}");
    const messages = parsed.messages ?? [];
    const toolDone = messages.at(-1)?.role === "tool";
    const lastUser = [...messages].reverse().find(message => message.role === "user")?.content ?? "";
    let events;
    if (toolDone) {
      events = [{choices: [{delta: {content: "修改完成"}, index: 0}]},
                {choices: [{delta: {}, finish_reason: "stop", index: 0}]}];
    } else if (lastUser.includes("写一下")) {
      events = [{choices: [{delta: {tool_calls: [{index: 0, id: "call_nav",
        function: {name: "propose_file_update", arguments: JSON.stringify({
          path: "E-report/nav-note.md", content: "# Navigation\nnew content\n", reason: "测试修改摘要",
        })}}]}, index: 0}]},
        {choices: [{delta: {}, finish_reason: "tool_calls", index: 0}]}];
    } else {
      events = [{choices: [{delta: {content: `Response: ${lastUser}`}, index: 0}]},
                {choices: [{delta: {}, finish_reason: "stop", index: 0}]}];
    }
    res.writeHead(200, {"Content-Type": "text/event-stream"});
    res.end(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(""));
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
  await waitFor(`document.readyState === 'complete' && (!!document.querySelector('form') || !!document.querySelector('.dv-tab'))`);
  const email = `chat-nav-${Date.now()}@vibeflow.cn`;
  const registered = await evalJs(`fetch('/api/v1/auth/register', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email: '${email}', name: 'chat-nav', password: 'chat-nav-pass-123'})
  }).then(async r => ({status: r.status, body: await r.json()}))`);
  if (registered.status !== 200) throw new Error('独立用户注册失败: ' + JSON.stringify(registered));
  await evalJs(`localStorage.setItem('vf_token', ${JSON.stringify(registered.body.token)})`);
  const workspaces = await api('/api/v1/workspaces');
  const workspace = workspaces.body[0] ?? (await api('/api/v1/workspaces', {method: 'POST', body: {name: `chat-nav-ws-${Date.now()}`}})).body;
  const caseName = `chat-navigation-${Date.now()}`;
  const created = await api(`/api/v1/workspaces/${workspace.id}/cases`, {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const profile = await api("/api/v1/provider-profiles", {method: "POST", body: {
    provider: "custom", name: `chat-nav-${Date.now()}`, base_url: `http://127.0.0.1:${MOCK_PORT}`,
    default_model: "mock-nav", api_key: "mock-key",
  }});
  const profileId = profile.body.id;
  const conversation = await api("/api/v1/conversations", {method: "POST", body: {
    case_id: caseId, title: "Chat navigation", provider_profile_id: profileId, model: "mock-nav",
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
  await sendText("写一下");
  await waitFor(`[...document.querySelectorAll('button')].some(x => x.textContent === '批准执行')`);
  await evalJs(`[...document.querySelectorAll('button')].find(x => x.textContent === '批准执行').click()`);
  await waitFor(`document.querySelector('[data-testid="chat-changes"]')?.textContent?.match(/[1-9]/)`);
  await waitFor(`!!document.querySelector('[data-testid="chat-tool-card"][data-status="completed"][data-kind="edit"]')`);
  const toolText = await evalJs(`document.querySelector('[data-testid="chat-tool-card"][data-status="completed"]').textContent`);
  if (!toolText.includes('已生成文件修改') || !toolText.includes('已完成') || !toolText.includes('文件已写入')) {
    throw new Error('工具卡不是实际完成状态: ' + toolText);
  }
  if (toolText.includes('已批准并执行')) throw new Error('仍存在批准即成功的乐观假状态');
  await sendText("第一句");
  await sendText("第二句");
  await send("Browser.grantPermissions", {origin: "http://localhost:5173", permissions: ["clipboardReadWrite", "clipboardSanitizedWrite"]});

  await evalJs(`[...document.querySelectorAll('[data-testid="chat-copy"]')].at(-1).click()`);
  const assistantClipboard = await evalJs(`navigator.clipboard.readText()`);
  if (!assistantClipboard.includes("第二句")) throw new Error("助手复制不正确: " + assistantClipboard);
  await evalJs(`(() => {
    const message = [...document.querySelectorAll('[data-testid="chat-message"]')].find(x => x.textContent.includes('第一句'));
    message.querySelector('[data-testid="chat-copy"]').click();
  })()`);
  const userClipboard = await evalJs(`navigator.clipboard.readText()`);
  if (userClipboard !== "第一句") throw new Error("用户复制不正确: " + userClipboard);
  console.log("Chat user/assistant clipboard: OK", {caseId, cid});

  await evalJs(`(() => {
    const select = document.querySelector('[data-testid="chat-user-nav"]');
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, '0');
    select.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  console.log("USER NAV DEBUG", await evalJs(`({
    value: document.querySelector('[data-testid="chat-user-nav"]').value,
    target: document.querySelector('[data-testid="chat-message"][data-index="0"]')?.outerHTML?.slice(0, 400),
  })`));
  await waitFor(`document.querySelector('[data-testid="chat-message"][data-index="0"]')?.dataset.highlight === 'true'`);
  for (let i = 0; i < 8; i++) await sendText(`补充问题 ${i + 1}`);
  await evalJs(`(() => {
    const list = document.querySelector('[data-testid="chat-message-list"]');
    if (list.scrollHeight <= list.clientHeight) throw new Error('消息列表不可滚动，无法验证回底部');
    list.scrollTop = 0;
    document.querySelector('[data-testid="chat-bottom"]').click();
  })()`);
  console.log("BOTTOM DEBUG", await evalJs(`(() => {
    const list = document.querySelector('[data-testid="chat-message-list"]');
    return {top: list.scrollTop, height: list.clientHeight, total: list.scrollHeight};
  })()`));
  await waitFor(`(() => {
    const list = document.querySelector('[data-testid="chat-message-list"]');
    return Math.abs(list.scrollTop + list.clientHeight - list.scrollHeight) < 12;
  })()`);
  await evalJs(`document.querySelector('[data-testid="chat-changes"]').click()`);
  await waitFor(`!!document.querySelector('[data-testid="chat-change-item"]')`);
  await evalJs(`document.querySelector('[data-testid="chat-change-item"]').click()`);
  await waitFor(`!!document.querySelector('[data-testid="chat-tool-card"][data-highlight="true"]')`);
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp10-chat-navigation.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("Chat user navigation/bottom/modification summary: OK", {caseId, cid});
} catch (error) {
  const debug = await evalJs(`document.body.textContent.slice(-900)`).catch(() => "");
  console.error("CHAT NAV E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
  mock.close();
}
