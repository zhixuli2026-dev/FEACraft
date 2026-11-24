import { execFile } from "node:child_process";
import http from "node:http";
import { writeFileSync } from "node:fs";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;
const outPath = process.argv[2] || "../docs/shots/wp07-information-focused.png";
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const getJson = (path) => new Promise((resolve, reject) => {
  http.get({ host: "127.0.0.1", port: PORT, path }, (res) => {
    let body = "";
    res.on("data", (chunk) => body += chunk);
    res.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});

const edge = execFile(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profileDir}`, "--window-size=1600,900", "about:blank"], () => {});
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
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails).slice(0, 600));
  return result.result.value;
};
const api = (path, options = {}) => evalJs(`fetch(${JSON.stringify(path)}, {
  headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
  ${options.method ? `method: ${JSON.stringify(options.method)},` : ""}
  ${options.body ? `body: JSON.stringify(${JSON.stringify(options.body)}),` : ""}
}).then(async r => { const j = await r.json().catch(() => ({})); return {status: r.status, body: j}; })`);
const readerPath = "D-post/reader-e2e.vtp";
const vtp = `<?xml version="1.0"?>
<VTKFile type="PolyData" version="1.0" byte_order="LittleEndian"><PolyData><Piece NumberOfPoints="3" NumberOfVerts="0" NumberOfLines="0" NumberOfStrips="0" NumberOfPolys="1"><PointData><DataArray type="Float32" Name="p" format="ascii">1 2 3</DataArray></PointData><Points><DataArray type="Float32" NumberOfComponents="3" format="ascii">0 0 0 1 0 0 0 1 0</DataArray></Points><Polys><DataArray type="Int32" Name="connectivity" format="ascii">0 1 2</DataArray><DataArray type="Int32" Name="offsets" format="ascii">3</DataArray></Polys></Piece></PolyData></VTKFile>`;
let sid = null;

try {
  await send("Page.enable");
  await send("Page.navigate", { url: "http://localhost:5173/" });
  await sleep(3000);
  if (await evalJs(`!!document.querySelector('form')`)) {
    await evalJs(`(() => {
      const inputs = document.querySelectorAll('input');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(inputs[inputs.length - 2], 'dev@vibeflow.cn'); inputs[inputs.length - 2].dispatchEvent(new Event('input', {bubbles: true}));
      setter.call(inputs[inputs.length - 1], 'devpass123'); inputs[inputs.length - 1].dispatchEvent(new Event('input', {bubbles: true}));
      document.querySelector('form').requestSubmit();
    })()`);
    await sleep(3500);
  }
  await evalJs(`document.querySelector('[data-case-name="cavity-demo"]').click()`);
  await sleep(2200);
  const before = (await api("/api/v1/cases/1/viz:sessions")).body.map(s => s.id);
  await api("/api/v1/cases/1/file", { method: "PUT", body: { path: readerPath, content: vtp } });
  await evalJs(`document.querySelector('[data-case-name="cavity-demo"]').click()`);
  await sleep(800);
  let clicked = false;
  for (let i = 0; i < 20; i++) {
    await sleep(500);
    clicked = await evalJs(`(() => {
      const row = document.querySelector('[data-path="${readerPath}"]');
      if (!row) return false;
      row.click();
      return true;
    })()`);
    if (clicked) break;
  }
  if (!clicked) throw new Error("VTP 文件节点不存在");
  let frameBody = "";
  for (let i = 0; i < 45; i++) {
    await sleep(1000);
    const state = await evalJs(`(async () => {
      const sessions = await fetch('/api/v1/cases/1/viz:sessions', {headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}}).then(r => r.json());
      const created = sessions.find(s => !${JSON.stringify(before)}.includes(s.id));
      const doc = document.querySelector('iframe[title="可视化"]')?.contentDocument;
      return {created, body: doc?.body?.innerText ?? ''};
    })()`);
    if (state.created) sid = state.created.id;
    frameBody = state.body;
    if (sid && frameBody.includes("点3") && frameBody.includes("单元1") && !frameBody.includes("令牌无效")) break;
  }
  if (!sid || !frameBody.includes("点3") || frameBody.includes("令牌无效")) {
    throw new Error("VTP 会话/渲染未就绪: " + frameBody.slice(0, 300));
  }
  console.log("VTP render: OK", {sid});

  const infoBody = await evalJs(`document.querySelector('iframe[title="可视化"]').contentDocument.body.innerText`);
  for (const expected of ["点3", "单元1", "数组 p（点）", "1 分量 · float · 12 B"]) {
    if (!infoBody.includes(expected)) throw new Error(`Information 缺少 ${expected}: ${infoBody.slice(-800)}`);
  }
  await evalJs(`(() => {
    const doc = document.querySelector('iframe[title="可视化"]').contentDocument;
    const scroller = doc.querySelector('.v-navigation-drawer__content') ?? doc.querySelector('.v-navigation-drawer');
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  })()`);
  await sleep(400);
  const shot = await send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
  writeFileSync(outPath, Buffer.from(shot.data, "base64"));
  console.log("Information array metadata: OK", outPath);
} catch (error) {
  console.error("INFORMATION E2E FAIL:", error.message);
  process.exitCode = 1;
} finally {
  if (sid) await api(`/api/v1/viz:sessions/${sid}`, { method: "DELETE" });
  await api(`/api/v1/cases/1/file?path=${encodeURIComponent(readerPath)}`, { method: "DELETE" });
  ws.close();
  edge.kill();
}
