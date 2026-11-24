import { execFile } from "node:child_process";
import http from "node:http";
import { createHash } from "node:crypto";
import { writeFileSync } from "node:fs";
import WebSocket from "ws";

const outPath = process.argv[2] || "../docs/shots/wp08-cad-studio.png";
const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
const settleFrames = async (count = 2) => evalJs(`new Promise(resolve => {
  let frame = 0;
  const next = () => {
    frame += 1;
    if (frame >= ${count}) resolve(true);
    else requestAnimationFrame(next);
  };
  requestAnimationFrame(next);
})`);
const getJson = path => new Promise((resolve, reject) => {
  http.get({host: "127.0.0.1", port: PORT, path}, res => {
    let body = "";
    res.on("data", chunk => body += chunk);
    res.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});
const edge = execFile(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profileDir}`, "--window-size=1500,900", "about:blank"], () => {});
await sleep(2200);
const targets = await getJson("/json/list");
const page = targets.find(target => target.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(resolve => ws.on("open", resolve));
const browserErrors = [];
ws.on("message", data => {
  const message = JSON.parse(data);
  if (message.method === "Runtime.exceptionThrown") browserErrors.push(message.params.exceptionDetails);
  if (message.method === "Runtime.consoleAPICalled" && message.params.type === "error") {
    browserErrors.push(message.params.args.map(arg => arg.value ?? arg.description ?? arg.type).join(" "));
  }
});
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
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails).slice(0, 600));
  return result.result.value;
};
const api = path => evalJs(`fetch(${JSON.stringify(path)}, {
  headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
}).then(async r => ({status: r.status, body: await r.json()}))`);
const canvasHash = async (region = null) => {
  const rect = await evalJs(`(() => {
    const canvas = document.querySelector('[data-testid="geo-canvas-host"] canvas');
    if (!canvas) return null;
    const r = canvas.getBoundingClientRect();
    return {x: r.x, y: r.y, width: r.width, height: r.height};
  })()`);
  if (!rect || rect.width <= 0 || rect.height <= 0) throw new Error("CAD canvas 不可见");
  const clip = region ? {
    x: rect.x + rect.width * region.x, y: rect.y + rect.height * region.y,
    width: rect.width * region.width, height: rect.height * region.height, scale: 1,
  } : {...rect, scale: 1};
  const shot = await send("Page.captureScreenshot", {format: "png", fromSurface: true,
    captureBeyondViewport: false, clip});
  return createHash("sha256").update(Buffer.from(shot.data, "base64")).digest("hex");
};
const click = async selector => {
  const found = await evalJs(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) return false;
    element.click();
    return true;
  })()`);
  if (!found) throw new Error("控件不存在: " + selector);
};
const setSelect = async (selector, value) => {
  const found = await evalJs(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) return false;
    element.value = ${JSON.stringify(value)};
    element.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  })()`);
  if (!found) throw new Error("下拉控件不存在: " + selector);
};
const setInput = async (selector, value) => {
  const found = await evalJs(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) return false;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(element, ${JSON.stringify(String(value))});
    element.dispatchEvent(new Event('input', {bubbles: true}));
    element.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  })()`);
  if (!found) throw new Error("输入控件不存在: " + selector);
};
const setTextarea = async (selector, value) => {
  const found = await evalJs(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) return false;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    setter.call(element, ${JSON.stringify(String(value))});
    element.dispatchEvent(new Event('input', {bubbles: true}));
    element.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  })()`);
  if (!found) throw new Error("文本域不存在: " + selector);
};
const hostState = async name => evalJs(`document.querySelector('[data-testid="geo-canvas-host"]').getAttribute(${JSON.stringify(name)})`);

try {
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await sleep(3000);
  if (await evalJs(`!!document.querySelector('form')`)) {
    await evalJs(`(() => {
      const inputs = document.querySelectorAll('input');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(inputs[0], 'dev@vibeflow.cn'); inputs[0].dispatchEvent(new Event('input', {bubbles: true}));
      setter.call(inputs[1], 'devpass123'); inputs[1].dispatchEvent(new Event('input', {bubbles: true}));
      document.querySelector('form button').click();
    })()`);
    await sleep(1800);
  }
  let caseReady = false;
  for (let i = 0; i < 240; i++) {
    await sleep(250);
    caseReady = await evalJs(`(() => {
      const row = document.querySelector('[data-case-name="cavity-demo"]');
      if (!row) return false;
      row.click();
      return true;
    })()`);
    if (caseReady) break;
  }
  if (!caseReady) {
    const debug = await evalJs(`({form: !!document.querySelector('form'), token: !!localStorage.getItem('vf_token'),
      body: document.body.innerText.slice(0, 1200)})`);
    const workspaces = await api("/api/v1/workspaces");
    throw new Error("cavity-demo 未就绪: " + JSON.stringify({debug, workspaces}));
  }
  await sleep(1500);
  let geometryTab = false;
  for (let i = 0; i < 120 && !geometryTab; i++) {
    geometryTab = await evalJs(`(() => {
    const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === '几何预览');
    if (!tab) return false;
    const r = tab.getBoundingClientRect();
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
        {bubbles: true, clientX: x, clientY: y, button: 0}));
    }
    return true;
  })()`);
    if (!geometryTab) await sleep(250);
  }
  if (!geometryTab) throw new Error("几何预览页签不存在");
  let panelReady = false;
  for (let i = 0; i < 120; i++) {
    await sleep(250);
    panelReady = await evalJs(`!!document.querySelector('[data-testid="geo-canvas-host"]') && typeof window.vfLoadSceneFromBase64 === 'function'`);
    if (panelReady) break;
  }
  if (!panelReady) throw new Error("GeoPanel 未挂载");
  const scene = (await api("/api/v1/cases/1/geometry/scene")).body;
  if (!scene.objects?.length) throw new Error("没有可用于 B09 的真实场景");
  const firstName = scene.objects[0].name;
  const firstSelector = `[data-object-name=${JSON.stringify(firstName)}]`;
  const encoded = Buffer.from(JSON.stringify(scene), "utf8").toString("base64");
  console.log("B09 before Base64:", await evalJs(`({tree: !!document.querySelector('[data-testid="cad-tree"]'),
    host: !!document.querySelector('[data-testid="geo-canvas-host"]'), canvases: document.querySelectorAll('canvas').length,
    status: document.querySelector('[data-testid="geo-status"]')?.textContent ?? ''})`));
  await evalJs(`window.vfLoadSceneFromBase64(${JSON.stringify(encoded)})`);
  console.log("B09 after Base64:", await evalJs(`({status: document.querySelector('[data-testid="geo-status"]')?.textContent ?? '',
    info: document.querySelector('[data-testid="geo-obj-info"]')?.textContent ?? '',
    names: [...document.querySelectorAll('[data-object-name]')].map(el => el.getAttribute('data-object-name'))})`));
  let ready = false;
  for (let i = 0; i < 120; i++) {
    await sleep(250);
    ready = await evalJs(`!!document.querySelector(${JSON.stringify(firstSelector)}) && !!document.querySelector('[data-testid="geo-canvas-host"] canvas')`);
    if (ready) break;
  }
  if (!ready) {
    const debug = await evalJs(`({status: document.querySelector('[data-testid="geo-status"]')?.textContent ?? '',
      info: document.querySelector('[data-testid="geo-obj-info"]')?.textContent ?? '',
      names: [...document.querySelectorAll('[data-object-name]')].map(el => el.getAttribute('data-object-name')),
      loader: typeof window.vfLoadSceneFromBase64})`);
    throw new Error("B09 场景未就绪: " + JSON.stringify({firstName, ...debug}));
  }
  await evalJs(`window.vfFit()`);
  await sleep(400);
  const baseline = await canvasHash();

  for (const selector of ["cad-ortho", "cad-control", "cad-studio", "cad-ambient", "cad-direct", "cad-metalness", "cad-roughness"]) {
    if (!await evalJs(`!!document.querySelector('[data-testid="${selector}"]')`)) {
      throw new Error("B09 控件缺失: " + selector);
    }
  }

  await click('[data-testid="cad-ortho"]');
  await sleep(400);
  if (await hostState("data-ortho") !== "true") throw new Error("正交状态未启用");
  const ortho = await canvasHash();
  if (ortho === baseline) throw new Error("正交切换未改变渲染");
  await click('[data-testid="cad-ortho"]');
  await sleep(400);
  if (await canvasHash() !== baseline) throw new Error("正交关闭后未恢复透视");
  console.log("B09 ortho/perspective: OK");

  await setSelect('[data-testid="cad-control"]', "trackball");
  await sleep(300);
  if (await hostState("data-control") !== "trackball") throw new Error("trackball 状态未生效");
  const beforeDrag = await canvasHash();
  const rect = await evalJs(`(() => { const r = document.querySelector('[data-testid="geo-canvas-host"] canvas').getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; })()`);
  const x = rect.x + rect.width * 0.55, y = rect.y + rect.height * 0.55;
  await send("Input.dispatchMouseEvent", {type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1});
  await send("Input.dispatchMouseEvent", {type: "mouseMoved", x: x + 90, y: y + 35, button: "left", buttons: 1});
  await send("Input.dispatchMouseEvent", {type: "mouseReleased", x: x + 90, y: y + 35, button: "left", buttons: 0, clickCount: 1});
  await sleep(400);
  if (await canvasHash() === beforeDrag) throw new Error("trackball 拖动未改变视角");
  await setSelect('[data-testid="cad-control"]', "orbit");
  await evalJs(`window.vfFit()`);
  await sleep(400);
  const beforeStudio = await canvasHash();
  console.log("B09 trackball drag: OK");

  await click('[data-testid="cad-studio"]');
  await sleep(800);
  if (await hostState("data-studio") !== "true") throw new Error("studio 环境未启用");
  const studio = await canvasHash();
  if (studio === beforeStudio) throw new Error("studio 环境未改变 PBR 渲染");
  const shot = await send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
  writeFileSync(outPath, Buffer.from(shot.data, "base64"));
  await click('[data-testid="cad-studio"]');
  await sleep(400);
  console.log("B09 studio environment: OK", outPath);

  for (const [selector, value, initial, stateName] of [
    ["cad-ambient", 0.2, 1, "data-ambient"],
    ["cad-direct", 4, 1.1, "data-direct"],
    ["cad-metalness", 0.9, 0.3, "data-metalness"],
    ["cad-roughness", 0.1, 0.65, "data-roughness"],
  ]) {
    const before = await canvasHash();
    await setInput(`[data-testid="${selector}"]`, value);
    await sleep(350);
    if (await hostState(stateName) !== String(value)) throw new Error(`${selector} 状态未更新`);
    if (await canvasHash() === before) throw new Error(`${selector} 未改变渲染`);
    await setInput(`[data-testid="${selector}"]`, initial);
    await sleep(350);
  }
  console.log("B09 material controls: OK");

  const beforeQuad = await canvasHash();
  await setSelect('[data-testid="cad-view-mode"]', "quad");
  await sleep(500);
  if (await hostState("data-view-mode") !== "quad") throw new Error("四宫格状态未生效");
  if (await canvasHash() === beforeQuad) throw new Error("四宫格未改变渲染");
  const quadrantHashes = [];
  for (const region of [
    {x: 0, y: 0, width: 0.5, height: 0.5}, {x: 0.5, y: 0, width: 0.5, height: 0.5},
    {x: 0, y: 0.5, width: 0.5, height: 0.5}, {x: 0.5, y: 0.5, width: 0.5, height: 0.5},
  ]) quadrantHashes.push(await canvasHash(region));
  if (new Set(quadrantHashes).size < 2) throw new Error("四宫格各视口没有不同视角");
  const quadShot = await send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
  writeFileSync(outPath.replace(/\.png$/i, "-quad.png"), Buffer.from(quadShot.data, "base64"));
  await setSelect('[data-testid="cad-view-mode"]', "single");
  await sleep(500);
  if (await hostState("data-view-mode") !== "single" || await canvasHash() !== beforeQuad) {
    throw new Error("四宫格切回单视图后未恢复");
  }
  console.log("B09 quad/single viewports: OK");

  const beforeEffects = await canvasHash();
  await click('[data-testid="cad-shadow"]');
  await sleep(700);
  if (await hostState("data-shadow") !== "true") throw new Error("阴影状态未启用");
  if (await canvasHash() === beforeEffects) throw new Error("阴影未改变渲染");
  const shadowShot = await send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
  writeFileSync(outPath.replace(/\.png$/i, "-shadow.png"), Buffer.from(shadowShot.data, "base64"));
  await click('[data-testid="cad-shadow"]');
  await sleep(500);
  if (await canvasHash() !== beforeEffects) throw new Error("阴影关闭后未恢复");
  console.log("B09 shadow map: OK");

  await click('[data-testid="cad-ao"]');
  await sleep(1200);
  if (await hostState("data-ao") !== "true") throw new Error("SSAO 状态未启用");
  await settleFrames(3);
  const afterAOHash = await canvasHash();
  if (afterAOHash === beforeEffects) throw new Error("SSAO 未改变渲染");
  const aoShot = await send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
  writeFileSync(outPath.replace(/\.png$/i, "-ao.png"), Buffer.from(aoShot.data, "base64"));
  await click('[data-testid="cad-ao"]');
  await sleep(600);
  await settleFrames(3);
  if (await canvasHash() !== beforeEffects) throw new Error("SSAO 关闭后未恢复");
  await click('[data-testid="cad-ao"]');
  await sleep(400);
  await setSelect('[data-testid="cad-view-mode"]', "quad");
  await sleep(400);
  if (await hostState("data-ao") !== "false" || await hostState("data-view-mode") !== "quad") {
    throw new Error("SSAO 与四宫格未互斥");
  }
  await setSelect('[data-testid="cad-view-mode"]', "single");
  console.log("B09 SSAO and quad exclusion: OK");

  const beforeCap = await canvasHash();
  await setSelect('[data-testid="cad-clip-axis"]', "z");
  await sleep(800);
  const capCount = Number(await hostState("data-clip-caps"));
  if (!Number.isInteger(capCount) || capCount < 1) throw new Error("未生成真实剖面端盖: " + capCount);
  if (await canvasHash() === beforeCap) throw new Error("真实剖切未改变渲染");
  const capShot = await send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
  writeFileSync(outPath.replace(/\.png$/i, "-cap.png"), Buffer.from(capShot.data, "base64"));
  await setSelect('[data-testid="cad-clip-axis"]', "");
  await sleep(600);
  if (Number(await hostState("data-clip-caps")) !== 0 || await canvasHash() !== beforeCap) {
    throw new Error("关闭剖切后未恢复源网格");
  }
  console.log("B11 solid clip caps and source restore: OK");

  const beforeExplodeMotion = await canvasHash();
  if (!await evalJs(`!!document.querySelector('[data-testid="cad-explode-animate"]')`)) {
    throw new Error("B12 爆炸动画过渡控件缺失");
  }
  await evalJs(`[...document.querySelectorAll('label')].find(el => el.textContent?.trim() === '爆炸').querySelector('input').click()`);
  let sawIntermediate = false;
  let reachedTarget = false;
  for (let i = 0; i < 40; i++) {
    await sleep(50);
    const progress = Number(await hostState("data-explode-progress"));
    if (progress > 0 && progress < 0.5) sawIntermediate = true;
    if (progress === 0.5) { reachedTarget = true; break; }
  }
  if (!sawIntermediate || !reachedTarget) {
    throw new Error("爆炸没有真实时间插值: " + JSON.stringify({sawIntermediate, reachedTarget}));
  }
  await click('[data-testid="cad-explode-animate"]');
  await evalJs(`[...document.querySelectorAll('label')].find(el => el.textContent?.trim() === '爆炸').querySelector('input').click()`);
  await sleep(100);
  if (Number(await hostState("data-explode-progress")) !== 0) throw new Error("关闭爆炸后进度未归零");
  await evalJs(`[...document.querySelectorAll('label')].find(el => el.textContent?.trim() === '爆炸').querySelector('input').click()`);
  await sleep(100);
  if (Number(await hostState("data-explode-progress")) !== 0.5) throw new Error("关闭动画过渡后未立即到位");
  await evalJs(`document.querySelector('[data-testid="cad-anim"]').click()`);
  await sleep(120);
  if (Number(await hostState("data-explode-progress")) !== 0 ||
      !(await evalJs(`document.querySelector('[data-testid="cad-anim"]').checked`))) {
    throw new Error("普通动画与爆炸运动未互斥");
  }
  await evalJs(`document.querySelector('[data-testid="cad-anim"]').click()`);
  await sleep(400);
  if (await canvasHash() !== beforeExplodeMotion) throw new Error("动画互斥后未恢复源位置");
  console.log("B12 explode transition and animation exclusion: OK");

  const beforeTrack = await canvasHash();
  await click('[data-testid="cad-track-apply"]');
  await sleep(300);
  if (Number(await hostState("data-track-count")) !== 8 || Number(await hostState("data-track-duration")) !== 1) {
    throw new Error("八类轨道未正确应用");
  }
  await setInput('[data-testid="cad-track-time"]', 0.5);
  await sleep(350);
  const sampledTrack = await canvasHash();
  if (Number(await hostState("data-track-time")) !== 0.5 || sampledTrack === beforeTrack) {
    throw new Error("轨道 0.5s 采样未改变真实对象 transform");
  }
  await click('[data-testid="cad-track-play"]');
  await sleep(250);
  await click('[data-testid="cad-track-pause"]');
  const pausedTime = Number(await hostState("data-track-time"));
  if (!(pausedTime > 0.5) || await hostState("data-track-playing") !== "false") {
    throw new Error("轨道播放/暂停状态错误: " + pausedTime);
  }
  await click('[data-testid="cad-track-stop"]');
  await sleep(350);
  if (Number(await hostState("data-track-time")) !== 0 || await canvasHash() !== beforeTrack) {
    throw new Error("停止轨道未恢复源位置");
  }
  await evalJs(`(() => {
    window.__trackDownload = null;
    window.__trackOriginalClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function() {
      if (this.download) { window.__trackDownload = this.download; return; }
      return window.__trackOriginalClick.call(this);
    };
  })()`);
  await click('[data-testid="cad-track-export"]');
  const trackDownload = await evalJs(`window.__trackDownload`);
  await evalJs(`HTMLAnchorElement.prototype.click = window.__trackOriginalClick`);
  if (trackDownload !== "cad-animation.json") throw new Error("轨道 JSON 导出文件名错误: " + trackDownload);
  await setTextarea('[data-testid="cad-track-json"]', '{"schema":"bad"}');
  await click('[data-testid="cad-track-apply"]');
  await sleep(150);
  if (!(await evalJs(`document.querySelector('[data-testid="cad-track-status"]').textContent`)).includes("轨道错误") ||
      await canvasHash() !== beforeTrack) {
    throw new Error("非法轨道未保留上一有效场景");
  }
  console.log("B13 eight-track editor playback/export/errors: OK");

  const beforeZebraConfig = await canvasHash();
  await click('[data-testid="cad-zebra"]');
  await sleep(500);
  const blackwhite = await canvasHash();
  if (blackwhite === beforeZebraConfig) throw new Error("默认 Zebra 未改变渲染");
  await setSelect('[data-testid="cad-zebra-scheme"]', "colorful");
  await sleep(350);
  const colorful = await canvasHash();
  if (colorful === blackwhite || await hostState("data-zebra-scheme") !== "colorful") {
    throw new Error("colorful 配色未生效");
  }
  const colorfulShot = await send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
  writeFileSync(outPath.replace(/\.png$/i, "-zebra-colorful.png"), Buffer.from(colorfulShot.data, "base64"));
  await setSelect('[data-testid="cad-zebra-scheme"]', "grayscale");
  await sleep(350);
  const grayscale = await canvasHash();
  if (grayscale === colorful) throw new Error("grayscale 配色未改变渲染");
  await setSelect('[data-testid="cad-zebra-mapping"]', "reflection");
  await sleep(350);
  const reflection = await canvasHash();
  if (reflection === grayscale || await hostState("data-zebra-mapping") !== "reflection") {
    throw new Error("reflection mapping 未生效");
  }
  await setInput('[data-testid="cad-zebra-angle"]', 90);
  await sleep(350);
  const angled = await canvasHash();
  if (angled === reflection || Number(await hostState("data-zebra-angle")) !== 90) {
    throw new Error("Zebra 角度未改变渲染");
  }
  await setInput('[data-testid="cad-zebra-opacity"]', 0);
  await sleep(350);
  const zeroOpacity = await canvasHash();
  if (zeroOpacity === angled) throw new Error("Zebra opacity=0 未改变渲染");
  await setInput('[data-testid="cad-zebra-opacity"]', 0.35);
  await click('[data-testid="cad-zebra"]');
  await sleep(400);
  if (await canvasHash() !== beforeZebraConfig) throw new Error("Zebra 关闭后未恢复原材质");
  console.log("B22 color schemes mapping angle opacity: OK");
} catch (error) {
  console.error("CAD VIEW E2E FAIL:", error.message,
    browserErrors.length ? JSON.stringify(browserErrors.slice(-3)).slice(0, 2400) : "");
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
