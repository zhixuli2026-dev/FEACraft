import { execFile } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9245;
const MOCK_PORT = 8999;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-goal`;
const downloadDir = mkdtempSync(join(tmpdir(), "vibeflow-goal-export-"));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const mock = http.createServer((req, res) => {
  let body = "";
  req.on("data", chunk => body += chunk);
  req.on("end", () => {
    const parsed = JSON.parse(body || "{}");
    const lastUser = [...(parsed.messages || [])].reverse().find(message => message.role === "user")?.content || "";
    const continuing = lastUser.includes("继续执行当前目标");
    const done = lastUser === "look" || parsed.messages?.at(-1)?.role === "tool";
    if (lastUser === "look") {
      const content = parsed.messages.at(-1).content;
      const hasImage = Array.isArray(content) && content.some(part => part.type === "image_url");
      if (!hasImage) throw new Error("图片消息缺少 image_url");
    }
    const events = done ? [
      {choices: [{delta: {content: "GOAL_BROWSER_DONE"}, index: 0}]},
      {choices: [{delta: {}, finish_reason: "stop", index: 0}]},
    ] : [
      {choices: [{delta: {tool_calls: [{index: 0, id: continuing ? "call_goal_complete" : "call_browser_goal", function: {
        name: "update_goal", arguments: JSON.stringify(continuing ? {
          objective: "验证 Goal 浏览器持久化", acceptance_criteria: ["目标条可见", "重载后保留"],
          budget: {max_turns: 4, max_tool_calls: 9}, status: "completed", stop_reason: "浏览器验收完成",
        } : {objective: "验证 Goal 浏览器持久化",
          acceptance_criteria: ["目标条可见", "重载后保留"], budget: {max_turns: 4, max_tool_calls: 9}}),
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
  const email = `goal-ui-${Date.now()}@vibeflow.cn`;
  const registered = await evalJs(`fetch('/api/v1/auth/register', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email: '${email}', name: 'goal-ui', password: 'goal-ui-pass-123'})
  }).then(async r => ({status: r.status, body: await r.json()}))`);
  if (registered.status !== 200) throw new Error("独立用户注册失败: " + JSON.stringify(registered));
  await evalJs(`localStorage.setItem('vf_token', ${JSON.stringify(registered.body.token)})`);
  const workspaces = await api("/api/v1/workspaces");
  const workspace = workspaces.body[0];
  const caseName = `goal-ui-${Date.now()}`;
  const created = await api(`/api/v1/workspaces/${workspace.id}/cases`, {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const profile = await api("/api/v1/provider-profiles", {method: "POST", body: {
    provider: "custom", name: `goal-ui-${Date.now()}`, base_url: `http://127.0.0.1:${MOCK_PORT}`,
    default_model: "mock-goal-ui", api_key: "mock-key",
  }});
  const profileId = profile.body.id;
  const conversation = await api("/api/v1/conversations", {method: "POST", body: {
    case_id: caseId, title: "Goal UI", provider_profile_id: profileId, model: "mock-goal-ui",
  }});
  const cid = conversation.body.id;
  const openConversation = async (conversationId = cid) => {
    await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
    await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
    await waitFor(`[...document.querySelectorAll('.dv-tab')].some(t => t.textContent?.trim() === 'Agent')`);
    await evalJs(`(() => {
      const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === 'Agent');
      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, {bubbles: true, button: 0}));
      }
    })()`);
    await waitFor(`[...document.querySelector('[data-testid="agent-conversation"]').options].some(o => o.value === '${conversationId}')`);
    await evalJs(`(() => {
      for (const [selector, value] of [['[data-testid="agent-profile"]', '${profileId}'], ['[data-testid="agent-conversation"]', '${conversationId}']]) {
        const select = document.querySelector(selector);
        Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, value);
        select.dispatchEvent(new Event('change', {bubbles: true}));
      }
    })()`);
  };
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await openConversation();
  await waitFor(`!!document.querySelector('[data-testid="agent-input"]') && !document.querySelector('[data-testid="agent-input"]').disabled`);
  await evalJs(`(() => {
    const input = document.querySelector('[data-testid="agent-input"]');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, 'goal');
    input.dispatchEvent(new Event('input', {bubbles: true}));
    document.querySelector('[data-testid="agent-send"]').click();
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="goal-strip"][data-status="active"][data-revision="1"]')`);
  await waitFor(`document.querySelector('[data-testid="goal-strip"]').textContent.includes('目标条可见')`);
  await waitFor(`document.querySelector('[data-testid="chat-message-list"]').textContent.includes('GOAL_BROWSER_DONE')`);
  await evalJs(`document.querySelector('[data-testid="goal-run"]').click()`);
  await waitFor(`!!document.querySelector('[data-testid="goal-strip"][data-status="completed"][data-revision="2"]')`);
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await openConversation();
  await waitFor(`!!document.querySelector('[data-testid="goal-strip"][data-status="completed"][data-revision="2"]')`);
  const text = await evalJs(`document.querySelector('[data-testid="goal-strip"]').textContent`);
  if (!text.includes('验证 Goal 浏览器持久化') || !text.includes('4 轮/9 工具') || !text.includes('重载后保留') || !text.includes('浏览器验收完成')) {
    throw new Error("Goal 持久化目标条不正确: " + text);
  }
  await waitFor(`document.querySelector('[data-testid="chat-message-list"]').textContent.includes('GOAL_BROWSER_DONE') &&
    !!document.querySelector('[data-testid="chat-branch"]')`);
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp10-goal.png", import.meta.url), Buffer.from(shot.data, "base64"));
  await evalJs(`window.confirm = () => true; document.querySelector('[data-testid="chat-branch"]').click()`);
  await waitFor(`document.querySelector('[data-testid="agent-conversation"]').value &&
    document.querySelector('[data-testid="agent-conversation"]').value !== '${cid}' &&
    document.querySelector('[data-testid="agent-conversation"]').selectedOptions[0].textContent.includes('分叉')`);
  const branchCid = await evalJs(`document.querySelector('[data-testid="agent-conversation"]').value`);
  await waitFor(`document.querySelector('[data-testid="chat-message-list"]').textContent.includes('GOAL_BROWSER_DONE')`);
  await evalJs(`document.querySelector('[data-testid="agent-delete"]').click()`);
  await waitFor(`![...document.querySelector('[data-testid="agent-conversation"]').options].some(o => o.value === '${branchCid}')`);
  await evalJs(`(() => {
    const select = document.querySelector('[data-testid="agent-conversation"]');
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, '${cid}');
    select.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="goal-strip"]')`);
  await send("Browser.setDownloadBehavior", {behavior: "allow", downloadPath: downloadDir});
  await evalJs(`document.querySelector('[data-testid="agent-export"]').click()`);
  let exportName;
  for (let i = 0; i < 50; i++) {
    exportName = readdirSync(downloadDir).find(name => name.endsWith('.jsonl'));
    if (exportName) break;
    await sleep(100);
  }
  if (!exportName) throw new Error("会话 JSONL 未下载");
  const exported = readFileSync(join(downloadDir, exportName), "utf8").trim().split(/\r?\n/).map(JSON.parse);
  if (exported[0].schema !== 'vibeflow.agent.session.v1' || !exported.some(event => event.payload?.content === 'GOAL_BROWSER_DONE')) {
    throw new Error("下载的会话 JSONL 内容不正确");
  }
  await evalJs(`window.prompt = () => 'Renamed Goal UI'; window.confirm = () => true;
    document.querySelector('[data-testid="agent-rename"]').click()`);
  await waitFor(`[...document.querySelector('[data-testid="agent-conversation"]').options].some(o => o.textContent === 'Renamed Goal UI')`);
  await evalJs(`document.querySelector('[data-testid="agent-delete"]').click()`);
  await waitFor(`!document.querySelector('[data-testid="goal-strip"]') &&
    ![...document.querySelector('[data-testid="agent-conversation"]').options].some(o => o.value === '${cid}')`);
  const plan = await api('/api/v1/conversations', {method: 'POST', body: {
    case_id: caseId, title: 'Plan UI', provider_profile_id: profileId,
    model: 'mock-goal-ui', permission_mode: 'plan',
  }});
  const planId = plan.body.id;
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await openConversation(planId);
  await waitFor(`!!document.querySelector('[data-testid="approve-plan"]')`);
  await evalJs(`document.querySelector('[data-testid="approve-plan"]').click()`);
  await waitFor(`!document.querySelector('[data-testid="approve-plan"]')`);
  const approvedPlan = await api(`/api/v1/conversations/${planId}`);
  if (approvedPlan.body.permission_mode !== 'approve_writes') throw new Error('批准 Plan 后权限未恢复');
  await evalJs(`(() => {
    const input = document.querySelector('[data-testid="agent-input"]');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, '$vibeflow-abaqus');
    input.dispatchEvent(new Event('input', {bubbles: true}));
  })()`);
  await waitFor(`[...document.querySelectorAll('[data-testid="agent-input"]')].length > 0 &&
    [...document.querySelectorAll('div')].some(d => d.textContent.includes('VibeFlow Abaqus') && d.style.cursor === 'pointer')`);
  await evalJs(`[...document.querySelectorAll('div')].find(d => d.textContent.includes('VibeFlow Abaqus') && d.style.cursor === 'pointer').click()`);
  const skillPrompt = await evalJs(`document.querySelector('[data-testid="agent-input"]').value`);
  if (!skillPrompt.startsWith('Use $vibeflow-abaqus')) throw new Error('openai.yaml 默认提示未插入: ' + skillPrompt);
  await evalJs(`(() => {
    const input = document.querySelector('[data-testid="agent-input"]');
    const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z3XcAAAAASUVORK5CYII='), c => c.charCodeAt(0));
    const file = new File([bytes], 'pixel.png', {type: 'image/png'});
    const transfer = new DataTransfer(); transfer.items.add(file);
    input.dispatchEvent(new ClipboardEvent('paste', {clipboardData: transfer, bubbles: true}));
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="attachment-chip"]')`);
  await evalJs(`(() => {
    const input = document.querySelector('[data-testid="agent-input"]');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, 'look');
    input.dispatchEvent(new Event('input', {bubbles: true}));
    document.querySelector('[data-testid="agent-send"]').click();
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="chat-image"]')`);
  await waitFor(`document.querySelector('[data-testid="chat-image"]').naturalWidth === 1`);
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await openConversation(planId);
  await waitFor(`document.querySelector('[data-testid="chat-image"]')?.naturalWidth === 1`);
  const imageShot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp10-image-attachment.png", import.meta.url), Buffer.from(imageShot.data, "base64"));
  await evalJs(`(() => {
    const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === '终端');
    const r = tab.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
        {bubbles: true, clientX: x, clientY: y, button: 0}));
    }
  })()`);
  await waitFor(`[...document.querySelectorAll('button')].some(b => b.textContent.includes('快速输入'))`);
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('快速输入')).click()`);
  await waitFor(`!!document.querySelector('textarea[placeholder*="多行命令"]')`);
  await evalJs(`(() => {
    const input = document.querySelector('textarea[placeholder*="多行命令"]');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, 'echo TERMINAL_AGENT_OK');
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', ctrlKey: true, bubbles: true}));
  })()`);
  await waitFor(`document.querySelector('.terminal')?.textContent.includes('TERMINAL_AGENT_OK')`);
  await evalJs(`document.querySelector('[data-testid="terminal-to-agent"]').click()`);
  await waitFor(`document.querySelector('[data-testid="agent-input"]')?.value.includes('TERMINAL_AGENT_OK')`);
  console.log("Goal strip/branch/export/reload/rename/delete/plan/skill/image/terminal output: OK", {caseId, cid, branchCid, exportName, planId});
} catch (error) {
  console.error("GOAL E2E FAIL:", error.message, await evalJs(`document.body.textContent.slice(-900)`).catch(() => ""));
  process.exitCode = 1;
} finally {
  ws.close(); edge.kill(); mock.closeAllConnections(); mock.close();
  rmSync(downloadDir, {recursive: true, force: true});
}
