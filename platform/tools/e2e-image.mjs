import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9230;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-image`;
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
const rect = () => evalJs(`(() => { const r = document.querySelector('[data-testid="image-img"]').getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; })()`);

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
  const caseName = `image-viewer-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="320" height="120"><rect width="320" height="120" fill="#16345c"/><circle cx="70" cy="60" r="35" fill="#3b82f6"/><text x="125" y="70" fill="white" font-size="24">ImagePanel</text></svg>`;
  const written = await api(`/api/v1/cases/${caseId}/file`, {method: "PUT", body: {path: "D-post/test.svg", content: svg}});
  if (written.status !== 200) throw new Error("写入 SVG 失败");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await waitFor(`!!document.querySelector('[data-path="D-post/test.svg"]')`);
  await evalJs(`document.querySelector('[data-path="D-post/test.svg"]').click()`);
  await waitFor(`document.querySelector('[data-testid="image-img"]')?.naturalWidth === 320`);
  const initial = await rect();
  await evalJs(`document.querySelector('[data-testid="image-zoom-in"]').click()`);
  await waitFor(`document.querySelector('[data-testid="image-scale"]')?.textContent === '125%'`);
  const zoomed = await rect();
  if (zoomed.width <= initial.width * 1.2) throw new Error("按钮缩放未改变实际渲染尺寸: " + JSON.stringify({initial, zoomed}));
  await evalJs(`document.querySelector('[data-testid="image-reset"]').click()`);
  await waitFor(`document.querySelector('[data-testid="image-scale"]')?.textContent === '100%'`);
  const center = await evalJs(`(() => { const r = document.querySelector('[data-testid="image-img"]').getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()`);
  await send("Input.dispatchMouseEvent", {type: "mouseWheel", x: center.x, y: center.y, deltaX: 0, deltaY: -120});
  await waitFor(`document.querySelector('[data-testid="image-scale"]')?.textContent === '125%'`);
  const beforeDrag = await rect();
  await send("Input.dispatchMouseEvent", {type: "mousePressed", x: center.x, y: center.y, button: "left", buttons: 1, clickCount: 1});
  await send("Input.dispatchMouseEvent", {type: "mouseMoved", x: center.x + 70, y: center.y + 35, button: "left", buttons: 1});
  await send("Input.dispatchMouseEvent", {type: "mouseReleased", x: center.x + 70, y: center.y + 35, button: "left", buttons: 0, clickCount: 1});
  await waitFor(`document.querySelector('[data-testid="image-img"]')?.style.transform.includes('70px')`);
  const afterDrag = await rect();
  if (afterDrag.x - beforeDrag.x < 60 || afterDrag.y - beforeDrag.y < 25) {
    throw new Error("拖拽未改变图像位置: " + JSON.stringify({beforeDrag, afterDrag}));
  }
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp09-image-viewer.png", import.meta.url), Buffer.from(shot.data, "base64"));
  await evalJs(`document.querySelector('[data-testid="image-reset"]').click()`);
  await waitFor(`document.querySelector('[data-testid="image-img"]')?.style.transform.includes('translate(0px, 0px) rotate(0deg) scale(1)')`);
  await evalJs(`document.querySelector('[data-testid="image-rotate"]').click()`);
  await waitFor(`document.querySelector('[data-testid="image-img"]')?.style.transform.includes('rotate(90deg)')`);
  const rotated = await rect();
  if (rotated.height <= rotated.width) throw new Error("90° 旋转未改变实际包围盒: " + JSON.stringify(rotated));
  const fsButton = await evalJs(`(() => {
    const r = document.querySelector('[data-testid="image-fullscreen"]').getBoundingClientRect();
    return {x:r.x+r.width/2,y:r.y+r.height/2};
  })()`);
  await send("Input.dispatchMouseEvent", {type:"mousePressed", x:fsButton.x, y:fsButton.y, button:"left", buttons:1, clickCount:1});
  await send("Input.dispatchMouseEvent", {type:"mouseReleased", x:fsButton.x, y:fsButton.y, button:"left", buttons:0, clickCount:1});
  await waitFor(`document.fullscreenElement?.dataset?.testid === 'image-viewer' || document.querySelector('[data-testid="image-viewer"]')?.dataset.fullscreen === 'true'`);
  const fullscreenShot = await send("Page.captureScreenshot", {format:"png"});
  writeFileSync(new URL("../docs/shots/e17-image-fullscreen.png", import.meta.url), Buffer.from(fullscreenShot.data, "base64"));
  await evalJs(`document.querySelector('[data-testid="image-fullscreen"]').click()`);
  await waitFor(`!document.fullscreenElement && document.querySelector('[data-testid="image-viewer"]')?.dataset.fullscreen === 'false'`);
  await evalJs(`document.querySelector('[data-testid="image-reset"]').click()`);
  await waitFor(`document.querySelector('[data-testid="image-img"]')?.style.transform.includes('rotate(0deg) scale(1)')`);
  console.log("Image rotation/fullscreen/reset: OK", {rotated});
  console.log("Image blob render/zoom/wheel/drag/reset: OK", {caseId, initial, zoomed, afterDrag});
} catch (error) {
  const debug = await evalJs(`({
    viewer: document.querySelector('[data-testid="image-viewer"]')?.dataset?.file ?? '',
    image: document.querySelector('[data-testid="image-img"]') ? {
      width: document.querySelector('[data-testid="image-img"]').naturalWidth,
      height: document.querySelector('[data-testid="image-img"]').naturalHeight,
      complete: document.querySelector('[data-testid="image-img"]').complete,
      src: document.querySelector('[data-testid="image-img"]').src.slice(0, 80),
    } : null,
    error: document.querySelector('[data-testid="image-error"]')?.textContent ?? '',
    text: document.querySelector('[data-testid="image-viewer"]')?.textContent ?? document.body.textContent.slice(-600),
  })`).catch(() => ({}));
  console.error("IMAGE E2E FAIL:", error.message, JSON.stringify(debug));
  const shot = await send("Page.captureScreenshot", {format: "png"}).catch(() => null);
  if (shot) writeFileSync(new URL("../docs/shots/wp09-image-failure.png", import.meta.url), Buffer.from(shot.data, "base64"));
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
