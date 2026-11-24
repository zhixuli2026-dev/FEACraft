import { execFile } from "node:child_process";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const getJson = (path) => new Promise((resolve, reject) => {
  http.get({ host: "127.0.0.1", port: PORT, path }, (res) => {
    let body = "";
    res.on("data", (chunk) => body += chunk);
    res.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});

const edge = execFile(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profileDir}`, "--window-size=1500,900", "about:blank"], () => {});
await sleep(2200);
const targets = await getJson("/json/list");
const page = targets.find((target) => target.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve) => ws.on("open", resolve));
let id = 0;
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const mid = ++id;
  const onMessage = (data) => {
    const message = JSON.parse(data);
    if (message.id === mid) {
      ws.off("message", onMessage);
      message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
    }
  };
  ws.on("message", onMessage);
  ws.send(JSON.stringify({ id: mid, method, params }));
});
const evalJs = async (expression) => {
  const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails).slice(0, 500));
  return result.result.value;
};
const api = (path, options = {}) => evalJs(`fetch(${JSON.stringify(path)}, {
  headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
  ${options.method ? `method: ${JSON.stringify(options.method)},` : ""}
  ${options.body ? `body: JSON.stringify(${JSON.stringify(options.body)}),` : ""}
}).then(async r => { const j = await r.json().catch(() => ({})); return {status: r.status, body: j}; })`);
const clickPath = async (path) => {
  const found = await evalJs(`(() => {
    const row = [...document.querySelectorAll('[data-path]')].find(d => d.getAttribute('data-path') === ${JSON.stringify(path)});
    if (!row) return false;
    row.click();
    return true;
  })()`);
  if (!found) throw new Error("找不到文件: " + path);
  await sleep(1000);
};
const save = async () => {
  const found = await evalJs(`(() => {
    const button = [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '保存 (Ctrl+S)');
    if (!button) return false;
    button.click();
    return true;
  })()`);
  if (!found) throw new Error("保存按钮不存在");
};
const setModel = async (content) => evalJs(`window.monaco.editor.getModels().at(-1).setValue(${JSON.stringify(content)})`);

let original = null;
try {
  await send("Page.enable");
  await send("Page.navigate", { url: "http://localhost:5173/" });
  await sleep(3000);
  if (await evalJs(`!!document.querySelector('form')`)) {
    await evalJs(`
      (() => {
        const inputs = document.querySelectorAll('input');
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(inputs[0], 'dev@vibeflow.cn'); inputs[0].dispatchEvent(new Event('input', {bubbles: true}));
        setter.call(inputs[1], 'devpass123'); inputs[1].dispatchEvent(new Event('input', {bubbles: true}));
        document.querySelector('form button').click();
      })()
    `);
    await sleep(1800);
  }
  await evalJs(`document.querySelector('[data-case-name="cavity-demo"]').click()`);
  await sleep(2200);
  original = (await api("/api/v1/cases/1/file?path=" + encodeURIComponent("A-geo/geometry.py"))).body.content;
  await clickPath("A-geo/geometry.py");
  await setModel(`import cadquery as cq
def build_geometry(ctx):
    return {"auto": cq.Workplane("XY").box(2, 2, 2)}
`);
  await save();
  let success = false;
  for (let i = 0; i < 30; i++) {
    await sleep(1000);
    const state = await evalJs(`({text: document.body.innerText, canvas: document.querySelectorAll('canvas').length})`);
    if (state.text.includes("auto（")) {
      success = true;
      console.log("auto-preview save/refresh: OK", {canvas: state.canvas});
      break;
    }
  }
  if (!success) throw new Error("自动预览未刷新新对象");
  const previousAutoJob = await evalJs(`document.querySelector('[data-auto-job]')?.getAttribute('data-auto-job') ?? ''`);
  await clickPath("A-geo/geometry.py");
  await setModel("raise RuntimeError('AUTO_FAIL')");
  await evalJs(`window.confirm = () => true`);
  await save();
  await evalJs(`
    (() => {
      const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === '几何预览');
      const r = tab.getBoundingClientRect();
      const x = r.x + r.width / 2, y = r.y + r.height / 2;
      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, {bubbles: true, clientX: x, clientY: y, button: 0}));
      }
    })()
  `);
  let geoStatus = "";
  let objInfoText = "";
  let autoJob = "";
  for (let i = 0; i < 45; i++) {
    await sleep(1000);
    const state = await evalJs(`({
      status: document.querySelector('[data-testid="geo-status"]')?.textContent ?? '',
      info: document.querySelector('[data-testid="geo-obj-info"]')?.textContent ?? '',
      job: document.querySelector('[data-auto-job]')?.getAttribute('data-auto-job') ?? ''
    })`);
    geoStatus = state.status;
    objInfoText = state.info;
    autoJob = state.job;
    if (autoJob !== previousAutoJob && geoStatus.includes("预览失败")) break;
  }
  const recentJobs = (await api("/api/v1/cases/1/jobs")).body.slice(0, 3);
  console.log("auto-preview recent jobs:", recentJobs.map(j => ({id: j.id, status: j.status, type: j.type, exit: j.exit_code})));
  const hasError = geoStatus.includes("预览失败");
  const hasAuto = objInfoText.includes("auto（");
  console.log("auto-preview failure flags:", { hasError, hasAuto, geoStatus, autoJob });
  if (autoJob === previousAutoJob || !hasError || !hasAuto) {
    throw new Error("预览失败时未保留上一有效场景");
  }
  console.log("auto-preview failure keeps last scene: OK");
} catch (error) {
  console.error("AUTO-PREVIEW FAIL:", error.message);
  process.exitCode = 1;
} finally {
  if (typeof original === "string") {
    try {
      const restored = await api("/api/v1/cases/1/file", { method: "PUT", body: { path: "A-geo/geometry.py", content: original } });
      if (restored.status !== 200) throw new Error(`HTTP ${restored.status}`);
    } catch (error) {
      console.error("AUTO-PREVIEW RESTORE FAIL:", error.message);
      process.exitCode = 1;
    }
  }
  ws.close();
  edge.kill();
}
