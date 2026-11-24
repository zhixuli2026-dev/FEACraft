import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9258;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-error-lifecycle`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
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
    await sleep(150);
  }
  throw new Error("等待超时: " + expression);
};
const api = (path, options = {}) => evalJs(`fetch(${JSON.stringify(path)}, {
  headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
  ${options.method ? `method: ${JSON.stringify(options.method)},` : ""}
  ${options.body ? `body: JSON.stringify(${JSON.stringify(options.body)}),` : ""}
}).then(async r => { const j = await r.json().catch(() => ({})); return {status: r.status, body: j}; })`);
const setInput = async (selector, value) => evalJs(`(() => {
  const input = document.querySelector(${JSON.stringify(selector)});
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, ${JSON.stringify(value)});
  input.dispatchEvent(new Event('input', {bubbles: true}));
})()`);

try {
  await send("Page.enable");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`document.readyState === 'complete' && (!!document.querySelector('form') || !!document.querySelector('[data-case-name]'))`);
  const email = `error-life-ui-${Date.now()}@vibeflow.cn`;
  const registered = await evalJs(`fetch('/api/v1/auth/register', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email: '${email}', name: 'error-life-ui', password: 'error-life-ui-pass-123'})
  }).then(async r => ({status: r.status, body: await r.json()}))`);
  if (registered.status !== 200) throw new Error("独立用户注册失败");
  await evalJs(`localStorage.setItem('vf_token', ${JSON.stringify(registered.body.token)})`);
  const workspaces = await api("/api/v1/workspaces");
  const workspace = workspaces.body[0];
  const caseName = `error-life-ui-${Date.now()}`;
  const created = await api(`/api/v1/workspaces/${workspace.id}/cases`, {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const dict = await api(`/api/v1/cases/${caseId}/caseDict`);
  const block = `errors\n{\n    summary { total 1; open 1; blocking 1; resolved 0; lastErrorId "mesh-ui"; }\n    entries { mesh-ui { title "UI mesh error"; stage mesh; category meshQuality; severity error; status open; blocking true; message "negative Jacobian"; } }\n}`;
  const content = dict.body.content.replace("errors\n{\n    // entries { }\n}", block);
  const written = await api(`/api/v1/cases/${caseId}/caseDict`, {method: "PUT", body: {content}});
  if (written.status !== 200) throw new Error("错误夹具写入失败: " + JSON.stringify(written));
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await waitFor(`[...document.querySelectorAll('.dv-tab')].some(t => t.textContent?.trim() === 'caseDict')`);
  await evalJs(`(() => {
    const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === 'caseDict');
    const r = tab.getBoundingClientRect(); const x = r.x + r.width / 2, y = r.y + r.height / 2;
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
        {bubbles: true, clientX: x, clientY: y, button: 0}));
    }
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="error-entry-mesh-ui"]')`);
  await setInput('[data-testid="error-fix-mesh-ui"]', 'Refine boundary mesh');
  await evalJs(`document.querySelector('[data-testid="error-propose-mesh-ui"]').click()`);
  await waitFor(`document.querySelector('[data-testid="error-entry-mesh-ui"]').dataset.status === 'fixProposed'`);
  await setInput('[data-testid="error-resolution-mesh-ui"]', 'Refined boundary layer');
  await evalJs(`document.querySelector('[data-testid="error-fixed-mesh-ui"]').click()`);
  await waitFor(`document.querySelector('[data-testid="error-entry-mesh-ui"]').dataset.status === 'fixed'`);
  await setInput('[data-testid="error-verification-mesh-ui"]', 'checkMesh all metrics passed');
  await evalJs(`document.querySelector('[data-testid="error-verified-mesh-ui"]').click()`);
  await waitFor(`document.querySelector('[data-testid="error-entry-mesh-ui"]').dataset.status === 'verified'`);
  const verified = await api(`/api/v1/cases/${caseId}/caseDict`);
  const summary = verified.body.parsed.errors.summary;
  if (Number(summary.open) !== 0 || Number(summary.blocking) !== 0 || Number(summary.resolved) !== 1) {
    throw new Error("验证后的 summary 不正确: " + JSON.stringify(summary));
  }
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await waitFor(`[...document.querySelectorAll('.dv-tab')].some(t => t.textContent?.trim() === 'caseDict')`);
  await evalJs(`(() => {
    const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === 'caseDict');
    const r = tab.getBoundingClientRect(); const x = r.x + r.width / 2, y = r.y + r.height / 2;
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
        {bubbles: true, clientX: x, clientY: y, button: 0}));
    }
  })()`);
  await waitFor(`document.querySelector('[data-testid="error-entry-mesh-ui"]')?.dataset.status === 'verified'`);
  await evalJs(`document.querySelector('[data-testid="error-entry-mesh-ui"]').scrollIntoView({block:'center'})`);
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/a08-error-lifecycle.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("CasePanel error fixProposed/fixed/verified, summary and reload persistence: OK", {caseId});
} catch (error) {
  const debug = await evalJs(`document.querySelector('[data-testid="error-lifecycle"]')?.textContent || document.body.textContent.slice(-900)`).catch(() => "");
  console.error("ERROR LIFECYCLE E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close(); edge.kill();
}
