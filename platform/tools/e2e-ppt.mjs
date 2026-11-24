import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9227;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-ppt`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
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
const setInput = async (selector, value, textarea = false) => {
  const ok = await evalJs(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) return false;
    const proto = ${textarea ? "HTMLTextAreaElement" : "HTMLInputElement"}.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, ${JSON.stringify(value)});
    element.dispatchEvent(new Event('input', {bubbles: true}));
    element.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  })()`);
  if (!ok) throw new Error("控件不存在: " + selector);
};

try {
  await send("Page.enable");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`document.readyState === 'complete' && (!!document.querySelector('form') || !!document.querySelector('[data-case-name]'))`);
  if (await evalJs(`!!document.querySelector('form')`)) {
    await evalJs(`(() => {
      const inputs = document.querySelectorAll('input');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(inputs[0], 'dev@vibeflow.cn');
      inputs[0].dispatchEvent(new Event('input', {bubbles: true}));
      inputs[0].dispatchEvent(new Event('change', {bubbles: true}));
      setter.call(inputs[1], 'devpass123');
      inputs[1].dispatchEvent(new Event('input', {bubbles: true}));
      inputs[1].dispatchEvent(new Event('change', {bubbles: true}));
      document.querySelector('form').requestSubmit();
    })()`);
    await waitFor(`!document.querySelector('form') && !!document.querySelector('[data-case-name]')`);
  }
  const caseName = `ppt-ui-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  if (!caseId) throw new Error("创建 PPT UI 算例失败");
  const uploaded = await evalJs(`(() => {
    const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='), c => c.charCodeAt(0));
    const form = new FormData();
    form.append('file', new Blob([bytes], {type: 'image/png'}), 'pixel.png');
    return fetch('/api/v1/cases/${caseId}/upload?path=pixel.png', {
      method: 'POST', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}, body: form,
    }).then(async r => ({status: r.status, body: await r.json()}));
  })()`);
  if (uploaded.status !== 200) throw new Error("上传 PPT 图片失败");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await waitFor(`[...document.querySelectorAll('.dv-tab')].some(t => t.textContent?.trim() === 'caseDict')`);
  await evalJs(`(() => {
    const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === 'caseDict');
    const r = tab.getBoundingClientRect();
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
        {bubbles: true, clientX: x, clientY: y, button: 0}));
    }
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="ppt-editor"]')`);
  const output = `E-report/build/ui-${Date.now()}.pptx`;
  await setInput('[data-testid="ppt-title"]', "NativePptTitle");
  await setInput('[data-testid="ppt-bullets"]', "NativeAlpha\nNativeBeta", true);
  await setInput('[data-testid="ppt-table-headers"]', "Metric,Value");
  await setInput('[data-testid="ppt-table-rows"]', "Mesh,42\nSolve,OK", true);
  await setInput('[data-testid="ppt-image"]', "pixel.png");
  await setInput('[data-testid="ppt-template"]', "");
  await setInput('[data-testid="ppt-output"]', output);
  await evalJs(`(() => {
    const button = document.querySelector('[data-testid="ppt-build"]');
    button.scrollIntoView();
    button.click();
    button.click();
  })()`);
  await waitFor(`document.querySelector('[data-testid="ppt-status"]')?.textContent?.includes('PPTX 已生成')`, 60000);
  const jobs = await api(`/api/v1/cases/${caseId}/jobs`);
  const workers = jobs.body.filter(job => job.command.includes("ppt_worker.py"));
  if (workers.length !== 1) throw new Error("双重点击产生重复 PPT 作业: " + JSON.stringify(jobs.body));
  const jobId = workers[0].id;
  const spec = await api(`/api/v1/cases/${caseId}/file?path=` + encodeURIComponent(`.vibeflow/ppt-jobs/job-${jobId}.json`));
  const specText = spec.body.content ?? "";
  for (const marker of ["NativePptTitle", "NativeAlpha", "NativeBeta", "Mesh", "pixel.png"]) {
    if (!specText.includes(marker)) throw new Error("UI job spec 缺少 " + marker + ": " + specText);
  }
  const raw = await evalJs(`fetch('/api/v1/cases/${caseId}/raw?path=' + encodeURIComponent(${JSON.stringify(output)}), {
    headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')},
  }).then(async r => { const data = await r.arrayBuffer(); return {status: r.status, size: data.byteLength, head: [...new Uint8Array(data).slice(0, 4)]}; })`);
  if (raw.status !== 200 || raw.size < 1000 || JSON.stringify(raw.head) !== JSON.stringify([80, 75, 3, 4])) {
    throw new Error("PPTX 原始产物无效: " + JSON.stringify(raw));
  }
  await evalJs(`document.querySelector('[data-testid="ppt-download"]').click()`);
  await waitFor(`document.querySelector('[data-testid="ppt-status"]')?.textContent?.includes('已开始下载')`);
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp09-ppt-builder.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("PPT UI spec/native artifact/download/debounce: OK", {caseId, jobId, bytes: raw.size});
} catch (error) {
  const debug = await evalJs(`({status: document.querySelector('[data-testid="ppt-status"]')?.textContent ?? '', text: document.body.textContent.slice(-800)})`).catch(() => ({}));
  console.error("PPT UI E2E FAIL:", error.message, JSON.stringify(debug));
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
