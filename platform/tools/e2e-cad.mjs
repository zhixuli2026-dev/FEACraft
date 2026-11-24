import { execFile } from "node:child_process";
import http from "node:http";
import { createHash } from "node:crypto";
import { writeFileSync } from "node:fs";
import WebSocket from "ws";

const outPath = process.argv[2] || "../docs/shots/wp08-cad-focused.png";

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
  for (let i = 0; i < 30; i++) {
    await sleep(250);
    if (await evalJs(`!!window.monaco?.editor?.getModels()?.length`)) break;
  }
  await setModel(`import cadquery as cq
def build_geometry(ctx):
    return {"auto": cq.Workplane("XY").box(2, 2, 2),
            "aux": cq.Workplane("XY").box(2, 2, 2).translate((5, 0, 0))}
`);
  const previousAutoJob = await evalJs(`document.querySelector('[data-auto-job]')?.getAttribute('data-auto-job') ?? ''`);
  await save();
  let autoJob = "";
  for (let i = 0; i < 30; i++) {
    await sleep(250);
    autoJob = await evalJs(`document.querySelector('[data-auto-job]')?.getAttribute('data-auto-job') ?? ''`);
    if (autoJob && autoJob !== previousAutoJob) break;
  }
  if (!autoJob || autoJob === previousAutoJob) throw new Error("保存后未产生新的自动预览作业");
  let autoJobStatus = "";
  for (let i = 0; i < 300; i++) {
    await sleep(1000);
    autoJobStatus = (await api(`/api/v1/jobs/${autoJob}`)).body.status;
    if (autoJobStatus === "succeeded") break;
    if (["failed", "cancelled"].includes(autoJobStatus)) {
      throw new Error(`自动预览作业 #${autoJob} ${autoJobStatus}`);
    }
  }
  if (autoJobStatus !== "succeeded") throw new Error(`自动预览作业 #${autoJob} 超时: ${autoJobStatus}`);
  let success = false;
  for (let i = 0; i < 30; i++) {
    await sleep(500);
    const state = await evalJs(`({canvas: document.querySelectorAll('canvas').length,
      auto: !!document.querySelector('[data-object-name="auto"]'),
      aux: !!document.querySelector('[data-object-name="aux"]')})`);
    if (state.auto && state.aux) {
      success = true;
      console.log("auto-preview save/refresh: OK", {canvas: state.canvas, job: autoJob});
      break;
    }
  }
  if (!success) {
    const debug = await evalJs(`({status: document.querySelector('[data-testid="geo-status"]')?.textContent ?? '', info: document.querySelector('[data-testid="geo-obj-info"]')?.textContent ?? ''})`);
    throw new Error("自动预览未刷新新对象: " + JSON.stringify(debug));
  }

  // 对象树：折叠、可见性、选择和状态保留
  console.log("CAD controls:", await evalJs(`({tree: !!document.querySelector('[data-testid="cad-tree"]'), auto: !!document.querySelector('[data-object-name="auto"]'), buttons: [...document.querySelectorAll('button')].map(b => b.textContent?.trim()).filter(t => ['收起对象','对象','导出 STEP/STL','截图'].includes(t))})`));
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '收起对象').click()`);
  if (await evalJs(`!!document.querySelector('[data-testid="cad-tree"]')`)) throw new Error("CAD 对象树未折叠");
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '对象').click()`);
  await evalJs(`document.querySelector('[data-object-name="auto"] input').click()`);
  await sleep(250);
  if (await evalJs(`document.querySelector('[data-object-name="auto"]').getAttribute('data-visible')`) !== "false") {
    throw new Error("CAD 对象未隐藏");
  }
  await evalJs(`document.querySelector('[data-object-name="auto"] input').click()`);
  await sleep(250);
  if (await evalJs(`document.querySelector('[data-object-name="auto"]').getAttribute('data-visible')`) !== "true") {
    throw new Error("CAD 对象未恢复可见");
  }
  await evalJs(`document.querySelector('[data-object-name="auto"] span').click()`);
  await sleep(250);
  if (await evalJs(`document.querySelector('[data-object-name="auto"]').getAttribute('data-selected')`) !== "true") {
    throw new Error("CAD 对象未选中");
  }
  console.log("CAD tree visibility/select: OK");

  // cadscene：真实 WS clear/replace 驱动对象树刷新，随后恢复原对象继续其他断言
  const sceneBefore = await evalJs(`fetch('/api/v1/cases/1/file?path=' + encodeURIComponent('A-geo/scene.json'), {
    headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
  }).then(r => r.json()).then(j => JSON.parse(j.content))`);
  await evalJs(`(async () => {
    const response = await fetch('/api/v1/connection-tickets', {method:'POST',
      headers:{Authorization:'Bearer '+localStorage.getItem('vf_token'),'Content-Type':'application/json'},
      body:JSON.stringify({scope:'cadscene',case_id:1})});
    if (!response.ok) throw new Error('CAD connection authorization: '+response.status);
    const {ticket} = await response.json();
    window.__cadWs = new WebSocket((location.protocol === 'https:' ? 'wss' : 'ws') + '://' + location.host + '/ws/cadscene?case_id=1&token=' + encodeURIComponent(ticket));
  })()`);
  for (let i = 0; i < 20; i++) {
    await sleep(100);
    if (await evalJs(`window.__cadWs?.readyState === 1`)) break;
  }
  await evalJs(`window.__cadMessages = []; window.__cadWs.onmessage = event => { try { window.__cadMessages.push(JSON.parse(event.data)); } catch {} }`);
  await evalJs(`window.__cadWs.send(JSON.stringify({type: 'scene.clear'}))`);
  let clearMessages = [];
  for (let i = 0; i < 30; i++) {
    await sleep(500);
    clearMessages = await evalJs(`window.__cadMessages`);
    if (clearMessages.some(m => m.type === 'scene.clear' || m.type === 'error')) break;
  }
  if (!clearMessages.some(m => m.type === 'scene.clear') || clearMessages.some(m => m.type === 'error')) {
    throw new Error("scene.clear 未收到成功回执: " + JSON.stringify(clearMessages));
  }
  for (let i = 0; i < 30; i++) {
    await sleep(500);
    if (!(await evalJs(`!!document.querySelector('[data-object-name="auto"]')`))) break;
  }
  const clearedScene = await api("/api/v1/cases/1/geometry/scene");
  const clearedFile = await api("/api/v1/cases/1/file?path=" + encodeURIComponent("A-geo/scene.json"));
  const clearedFileObjects = clearedFile.body.content ? JSON.parse(clearedFile.body.content).objects : null;
  if (clearedScene.status !== 200 || clearedScene.body.objects?.length !== 0 ||
      clearedFile.status !== 200 || clearedFileObjects?.length !== 0) {
    throw new Error("scene.clear 后事实源未清空: " + JSON.stringify({scene: clearedScene.body, file: clearedFile.body}));
  }
  if (await evalJs(`!!document.querySelector('[data-object-name="auto"]')`)) throw new Error("scene.clear 未清空对象树");
  await evalJs(`window.__cadMessages = []`);
  await evalJs(`window.__cadWs.send(JSON.stringify({type: 'scene.replace', objects: ${JSON.stringify(sceneBefore.objects)}}))`);
  let replaceMessages = [];
  for (let i = 0; i < 30; i++) {
    await sleep(500);
    replaceMessages = await evalJs(`window.__cadMessages`);
    if (replaceMessages.some(m => m.type === 'scene.replace' || m.type === 'error')) break;
  }
  if (!replaceMessages.some(m => m.type === 'scene.replace') || replaceMessages.some(m => m.type === 'error')) {
    throw new Error("scene.replace 未收到成功回执: " + JSON.stringify(replaceMessages));
  }
  let autoRestored = false;
  let auxRestored = false;
  for (let i = 0; i < 30; i++) {
    await sleep(500);
    autoRestored = await evalJs(`!!document.querySelector('[data-object-name="auto"]')`);
    auxRestored = await evalJs(`!!document.querySelector('[data-object-name="aux"]')`);
    if (autoRestored && auxRestored) break;
  }
  const restoredScene = await api("/api/v1/cases/1/geometry/scene");
  const restoredNames = restoredScene.body.objects?.map(o => o.name) ?? [];
  if (!autoRestored || !auxRestored || !restoredNames.includes("auto") || !restoredNames.includes("aux")) {
    throw new Error("scene.replace 未恢复对象树: " + JSON.stringify({autoRestored, auxRestored, restoredNames}));
  }
  await evalJs(`document.querySelector('[data-object-name="auto"] span').click()`);
  await sleep(250);
  if (await evalJs(`document.querySelector('[data-object-name="auto"]').getAttribute('data-selected')`) !== "true") {
    throw new Error("scene.replace 后无法重新选中对象");
  }
  await evalJs(`window.__cadWs.close()`);
  console.log("cadscene clear/replace: OK");

  // 真实导出作业
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '导出 STEP/STL').click()`);
  let exportStatus = "";
  for (let i = 0; i < 180; i++) {
    await sleep(1000);
    exportStatus = await evalJs(`document.querySelector('[data-testid="geo-status"]')?.textContent ?? ''`);
    if (exportStatus.includes("导出完成")) break;
    if (exportStatus.includes("失败")) throw new Error("STEP/STL 导出失败: " + exportStatus);
  }
  if (!exportStatus.includes("导出完成")) {
    console.log("export recent jobs:", (await api("/api/v1/cases/1/jobs")).body.slice(0, 3).map(j => ({id: j.id, status: j.status, type: j.type})));
    throw new Error("导出超时: " + exportStatus);
  }
  const tree = await api("/api/v1/cases/1/tree");
  const paths = tree.body.entries.map(e => e.path);
  if (!paths.some(p => p.startsWith("A-geo/exports/") && p.endsWith(".step")) ||
      !paths.some(p => p.startsWith("A-geo/exports/") && p.endsWith(".stl"))) {
    throw new Error("导出目录缺少 STEP/STL");
  }
  console.log("STEP/STL export: OK");

  // 截图不依赖真实下载目录：拦截 data URL 验证内容和文件名
  await evalJs(`(() => {
    window.__cadDownload = null;
    const originalClick = HTMLAnchorElement.prototype.click;
    window.__cadOriginalClick = originalClick;
    HTMLAnchorElement.prototype.click = function() {
      if (this.download) { window.__cadDownload = {name: this.download, href: this.href}; return; }
      return originalClick.call(this);
    };
  })()`);
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '截图').click()`);
  const download = await evalJs(`window.__cadDownload`);
  if (!download?.name?.endsWith("-cad.png") || !download.href.startsWith("data:image/png;base64,")) {
    throw new Error("CAD 截图 data URL 不正确: " + JSON.stringify(download));
  }
  await evalJs(`(() => { HTMLAnchorElement.prototype.click = window.__cadOriginalClick; return true; })()`);
  const shot = await send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
  writeFileSync(outPath, Buffer.from(shot.data, "base64"));
  console.log("CAD screenshot: OK", outPath);

  const canvasHash = async () => {
    const rect = await evalJs(`(() => {
      const canvas = document.querySelector('[data-testid="geo-canvas-host"] canvas');
      if (!canvas) return null;
      const r = canvas.getBoundingClientRect();
      return {x: r.x, y: r.y, width: r.width, height: r.height};
    })()`);
    if (!rect || rect.width <= 0 || rect.height <= 0) throw new Error('CAD canvas 不可见');
    const shot = await send("Page.captureScreenshot", {format: "png", fromSurface: true,
      captureBeyondViewport: false, clip: {...rect, scale: 1}});
    return createHash("sha256").update(Buffer.from(shot.data, "base64")).digest("hex");
  };
  await evalJs(`(() => {
    const select = document.querySelector('[data-testid="cad-compare"]');
    select.value = 'aux';
    select.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  await sleep(300);
  const properties = await evalJs(`document.querySelector('[data-testid="cad-properties"]')?.textContent ?? ''`);
  if (!properties.includes('尺寸 2 × 2 × 2') || !properties.includes('中心距离 5.000')) {
    throw new Error('CAD 属性或 bbox 中心距离错误: ' + properties);
  }
  await evalJs(`document.querySelector('[data-testid="cad-camera-x"]').click()`);
  await sleep(250);
  const xView = await canvasHash();
  await evalJs(`document.querySelector('[data-testid="cad-camera-y"]').click()`);
  await sleep(250);
  const yView = await canvasHash();
  if (xView === yView) throw new Error('CAD 相机预设未改变视角');
  await evalJs(`document.querySelector('[data-testid="cad-camera-x"]').click()`);
  await sleep(250);
  const baseline = await canvasHash();

  await evalJs(`[...document.querySelectorAll('label')].find(el => el.textContent?.trim() === '爆炸').querySelector('input').click()`);
  await sleep(1000);
  if (await evalJs(`document.querySelector('[data-testid="cad-explode"]').getAttribute('data-explode')`) !== "0.5") {
    throw new Error("爆炸未启用");
  }
  const exploded = await canvasHash();
  if (exploded === baseline) throw new Error("爆炸未改变渲染");
  await evalJs(`[...document.querySelectorAll('label')].find(el => el.textContent?.trim() === '爆炸').querySelector('input').click()`);
  await sleep(1000);
  if (await canvasHash() !== baseline) throw new Error("爆炸关闭后未恢复原始位置");

  const setClip = async (value) => {
    await evalJs(`(() => {
      const select = document.querySelector('[data-testid="cad-clip-axis"]');
      select.value = ${JSON.stringify(value)};
      select.dispatchEvent(new Event('change', {bubbles: true}));
    })()`);
    await sleep(400);
  };
  await setClip('z');
  const clipped = await canvasHash();
  if (clipped === baseline) throw new Error("剖切未改变渲染");
  await setClip('');
  if (await canvasHash() !== baseline) throw new Error("剖切关闭后未恢复");

  await evalJs(`document.querySelector('[data-testid="cad-zebra"]').click()`);
  await sleep(1000);
  if (!(await evalJs(`document.querySelector('[data-testid="cad-zebra"]').checked`))) {
    throw new Error("Zebra 未启用");
  }
  const zebra = await canvasHash();
  if (zebra === baseline) throw new Error("Zebra 未改变渲染");
  await evalJs(`(() => {
    const count = document.querySelector('[data-testid="cad-zebra-count"]');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(count, '4');
    count.dispatchEvent(new Event('input', {bubbles: true}));
    const opacity = document.querySelector('[data-testid="cad-zebra-opacity"]');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(opacity, '1');
    opacity.dispatchEvent(new Event('input', {bubbles: true}));
    document.querySelector('[data-testid="geo-canvas-host"] canvas').dispatchEvent(new WheelEvent('wheel', {deltaY: -500, bubbles: true}));
  })()`);
  await sleep(500);
  const zebraShot = await send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
  writeFileSync(outPath, Buffer.from(zebraShot.data, "base64"));
  await evalJs(`document.querySelector('[data-testid="geo-canvas-host"] canvas').dispatchEvent(new WheelEvent('wheel', {deltaY: 500, bubbles: true}))`);
  await evalJs(`document.querySelector('[data-testid="cad-zebra"]').click()`);
  await sleep(400);
  if (await canvasHash() !== baseline) throw new Error("Zebra 关闭后未恢复");
  await evalJs(`[...document.querySelectorAll('label')].find(el => el.textContent?.trim() === '爆炸').querySelector('input').click()`);
  await evalJs(`document.querySelector('[data-testid="cad-anim"]').click()`);
  await sleep(1000);
  if (await evalJs(`document.querySelector('[data-testid="cad-explode"]').getAttribute('data-explode')`) !== "0") {
    throw new Error("动画与爆炸未互斥");
  }
  const animated = await canvasHash();
  if (animated === baseline) throw new Error("CAD 动画未改变渲染");
  await evalJs(`document.querySelector('[data-testid="cad-anim"]').click()`);
  await sleep(400);
  if (await canvasHash() !== baseline) throw new Error("动画停止后未恢复");
  console.log("CAD explode/clip/zebra: OK", outPath);

  // B07：Fit、边线、透明、主题与 Base64 当前视图协议
  const b07Api = await evalJs(`({fit: typeof window.vfFit, load: typeof window.vfLoadSceneFromBase64,
    edges: typeof window.vfSetEdges, transparent: typeof window.vfSetTransparent, theme: typeof window.setVibeFlowTheme})`);
  if (Object.values(b07Api).some(type => type !== "function")) {
    throw new Error("B07 窗口接口缺失: " + JSON.stringify(b07Api));
  }
  await evalJs(`document.querySelector('[data-testid="cad-edges"]').click()`);
  await sleep(400);
  if (await evalJs(`document.querySelector('[data-testid="geo-canvas-host"]').getAttribute('data-edges')`) !== "false") {
    throw new Error("边线 UI 未关闭");
  }
  const withoutEdges = await canvasHash();
  if (withoutEdges === baseline) throw new Error("边线关闭未改变渲染");
  await evalJs(`window.vfSetEdges(true)`);
  await sleep(400);
  if (await canvasHash() !== baseline) throw new Error("边线恢复后渲染不一致");

  await evalJs(`document.querySelector('[data-testid="cad-transparent"]').click()`);
  await sleep(400);
  if (await evalJs(`document.querySelector('[data-testid="geo-canvas-host"]').getAttribute('data-transparent')`) !== "true") {
    throw new Error("透明 UI 未启用");
  }
  const transparent = await canvasHash();
  if (transparent === baseline) throw new Error("透明未改变渲染");
  await evalJs(`window.vfSetTransparent(false)`);
  await sleep(400);
  if (await canvasHash() !== baseline) throw new Error("透明关闭后未恢复");

  await evalJs(`(() => {
    const select = document.querySelector('[data-testid="cad-theme"]');
    select.value = 'light';
    select.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  await sleep(400);
  const lightState = await evalJs(`({host: document.querySelector('[data-testid="geo-canvas-host"]').getAttribute('data-theme'),
    root: document.documentElement.getAttribute('data-vf-theme'), body: document.body.getAttribute('data-vf-theme')})`);
  if (JSON.stringify(lightState) !== JSON.stringify({host: "light", root: "light", body: "light"})) {
    throw new Error("主题未同步到 viewer/document: " + JSON.stringify(lightState));
  }
  const light = await canvasHash();
  if (light === baseline) throw new Error("浅色主题未改变渲染");
  await evalJs(`window.setVibeFlowTheme('dark')`);
  await sleep(400);
  if (await canvasHash() !== baseline) throw new Error("深色主题恢复后渲染不一致");

  await evalJs(`document.querySelector('[data-testid="cad-fit"]').click()`);
  await sleep(400);
  const fitted = await canvasHash();
  await evalJs(`document.querySelector('[data-testid="geo-canvas-host"] canvas').dispatchEvent(new WheelEvent('wheel', {deltaY: -500, bubbles: true}))`);
  await sleep(400);
  const zoomed = await canvasHash();
  if (zoomed === fitted) throw new Error("Fit 后缩放未改变视图");
  await evalJs(`window.vfFit()`);
  await sleep(400);
  if (await canvasHash() !== fitted) throw new Error("Fit 不能恢复确定的取景");

  const base64Scene = {...sceneBefore, objects: [{...sceneBefore.objects[0], name: "base64-立方体"}]};
  const base64Encoded = Buffer.from(JSON.stringify(base64Scene), "utf8").toString("base64");
  await evalJs(`window.vfSetEdges(false); window.vfSetTransparent(true); window.setVibeFlowTheme('light');
    window.vfLoadSceneFromBase64(${JSON.stringify(base64Encoded)})`);
  let base64Loaded = false;
  for (let i = 0; i < 30; i++) {
    await sleep(250);
    base64Loaded = await evalJs(`!!document.querySelector('[data-object-name="base64-立方体"]')`);
    if (base64Loaded) break;
  }
  if (!base64Loaded || await evalJs(`!!document.querySelector('[data-object-name="auto"]')`)) {
    throw new Error("Base64 未替换当前对象树");
  }
  const base64Preferences = await evalJs(`({edges: document.querySelector('[data-testid="geo-canvas-host"]').getAttribute('data-edges'),
    transparent: document.querySelector('[data-testid="geo-canvas-host"]').getAttribute('data-transparent'),
    theme: document.querySelector('[data-testid="geo-canvas-host"]').getAttribute('data-theme')})`);
  if (JSON.stringify(base64Preferences) !== JSON.stringify({edges: "false", transparent: "true", theme: "light"})) {
    throw new Error("Base64 重载未保留显示偏好: " + JSON.stringify(base64Preferences));
  }
  const serverAfterBase64 = await api("/api/v1/cases/1/geometry/scene");
  const serverNamesAfterBase64 = serverAfterBase64.body.objects?.map(o => o.name) ?? [];
  if (!serverNamesAfterBase64.includes("auto") || !serverNamesAfterBase64.includes("aux") || serverNamesAfterBase64.includes("base64-立方体")) {
    throw new Error("Base64 当前视图意外改写服务器事实源: " + JSON.stringify(serverNamesAfterBase64));
  }
  const base64Shot = await send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
  writeFileSync(outPath.replace(/\.png$/i, "-base64.png"), Buffer.from(base64Shot.data, "base64"));
  await evalJs(`window.vfLoadSceneFromBase64('%%%')`);
  await sleep(300);
  const invalidBase64 = await evalJs(`({status: document.querySelector('[data-testid="geo-status"]')?.textContent ?? '',
    kept: !!document.querySelector('[data-object-name="base64-立方体"]')})`);
  if (!invalidBase64.status.includes("Base64 场景加载失败") || !invalidBase64.kept) {
    throw new Error("非法 Base64 未保留上一有效场景: " + JSON.stringify(invalidBase64));
  }
  const restoreEncoded = Buffer.from(JSON.stringify(sceneBefore), "utf8").toString("base64");
  await evalJs(`window.vfLoadSceneFromBase64(${JSON.stringify(restoreEncoded)})`);
  let nativeRestored = false;
  for (let i = 0; i < 30; i++) {
    await sleep(250);
    nativeRestored = await evalJs(`!!document.querySelector('[data-object-name="auto"]') && !!document.querySelector('[data-object-name="aux"]')`);
    if (nativeRestored) break;
  }
  if (!nativeRestored) throw new Error("Base64 原场景恢复失败");
  await evalJs(`window.vfSetEdges(true); window.vfSetTransparent(false); window.setVibeFlowTheme('dark');
    document.querySelector('[data-object-name="auto"] span').click()`);
  await sleep(300);
  console.log("CAD Base64/Fit/edges/transparency/theme: OK");
} catch (error) {
  console.error("CAD E2E FAIL:", error.message);
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
  try {
    await evalJs(`window.__cadWs?.close()`);
  } catch { /* 页面已关闭或 CDP 已断开 */ }
  ws.close();
  edge.kill();
}
