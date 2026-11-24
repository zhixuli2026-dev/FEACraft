import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9259;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-step-preview`;
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

try {
  await send("Page.enable");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`document.readyState === 'complete' && (!!document.querySelector('form') || !!document.querySelector('[data-case-name]'))`);
  const email = `step-ui-${Date.now()}@vibeflow.cn`;
  const registered = await evalJs(`fetch('/api/v1/auth/register', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email: '${email}', name: 'step-ui', password: 'step-ui-pass-123'})
  }).then(async r => ({status: r.status, body: await r.json()}))`);
  if (registered.status !== 200) throw new Error("独立用户注册失败");
  await evalJs(`localStorage.setItem('vf_token', ${JSON.stringify(registered.body.token)})`);
  const workspaces = await api("/api/v1/workspaces");
  const workspace = workspaces.body[0];
  const caseName = `step-ui-${Date.now()}`;
  const created = await api(`/api/v1/workspaces/${workspace.id}/cases`, {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const script = 'import cadquery as cq\nbox = cq.Workplane("XY").box(2, 3, 4)\ndef build_geometry(ctx):\n    return box\n';
  const preview = await api(`/api/v1/cases/${caseId}/file`, {method: "PUT", body: {path: "A-geo/geometry.py", content: script}});
  const jobId = preview.body.auto_preview_job;
  await waitFor(`(async () => { const r = await fetch('/api/v1/jobs/${jobId}', {headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')}}); return (await r.json()).status === 'succeeded'; })()`);
  const exported = await api(`/api/v1/cases/${caseId}/geometry:export`, {method: "POST", body: {}});
  await waitFor(`(async () => { const r = await fetch('/api/v1/jobs/${exported.body.job_id}', {headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')}}); return (await r.json()).status === 'succeeded'; })()`);
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await waitFor(`!!document.querySelector('[data-path="A-geo"]')`);
  if (!await evalJs(`!!document.querySelector('[data-path="A-geo/exports"]')`)) {
    await evalJs(`document.querySelector('[data-path="A-geo"]').click()`);
  }
  await waitFor(`!!document.querySelector('[data-path="A-geo/exports"]')`);
  await evalJs(`document.querySelector('[data-path="A-geo/exports"]').click()`);
  await waitFor(`!!document.querySelector('[data-path="A-geo/exports/result.step"]')`);
  await evalJs(`document.querySelector('[data-path="A-geo/exports/result.step"]').click()`);
  await waitFor(`document.body.textContent.includes('STEP 已载入') && document.body.textContent.includes('顶点/')`);
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/a03-step-preview.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("Tree STEP click imports real CAD mesh without scene rewrite: OK", {caseId});
} catch (error) {
  const debug = await evalJs(`document.body.textContent.slice(-1200)`).catch(() => "");
  console.error("STEP PREVIEW E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close(); edge.kill();
}
