// viz kernel 模式切换探针：逐个切显示模式并截图
// 用法: node probe-viz-modes.mjs http://127.0.0.1:8512/index.html D:\tmp\vizmode
import { execFile } from "node:child_process";
import http from "node:http";
import { writeFileSync } from "node:fs";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function getJson(path) {
  return new Promise((resolve, reject) => {
    http.get({ host: "127.0.0.1", port: PORT, path }, (res) => {
      let b = ""; res.on("data", (c) => (b += c)); res.on("end", () => resolve(JSON.parse(b)));
    }).on("error", reject);
  });
}

const url = process.argv[2];
const outPrefix = process.argv[3] || String.raw`D:\tmp\vizmode`;

const edge = execFile(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile2`}`,
  "--window-size=1400,900", "about:blank"], () => {});
await sleep(2500);
const targets = await getJson("/json/list");
const page = targets.find((t) => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.on("open", r));
let id = 0;
const send = (m, p = {}) => new Promise((res, rej) => {
  const i = ++id;
  const h = (d) => { const x = JSON.parse(d); if (x.id === i) { ws.off("message", h);
    x.error ? rej(new Error(JSON.stringify(x.error))) : res(x.result); } };
  ws.on("message", h); ws.send(JSON.stringify({ id: i, method: m, params: p }));
});
const evalJs = async (e) => {
  const r = await send("Runtime.evaluate", { expression: e, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error("JS: " + JSON.stringify(r.exceptionDetails).slice(0, 300));
  return r.result.value;
};

await send("Page.enable");
await send("Page.navigate", { url });
await sleep(10000);

async function shot(tag) {
  const s = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
  const buf = Buffer.from(s.data, "base64");
  writeFileSync(`${outPrefix}-${tag}.png`, buf);
  return buf.length;
}

// 切模式：点「显示模式」v-select 的 input（vuetify3 要点 input 才开菜单）→ 菜单里点目标项
async function pickMode(label) {
  return await evalJs(`(async () => {
    const clickEl = (el) => {
      const r = el.getBoundingClientRect();
      const x = r.x + r.width/2, y = r.y + r.height/2;
      for (const type of ['pointerdown','mousedown','pointerup','mouseup','click'])
        el.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, {bubbles:true, clientX:x, clientY:y, button:0}));
    };
    const modeSel = document.querySelectorAll('.v-select')[0];  // 第一个是显示模式
    if (!modeSel) return 'no-select';
    clickEl(modeSel.querySelector('input') || modeSel);
    await new Promise(r => setTimeout(r, 900));
    const items = [...document.querySelectorAll('.v-list-item')];
    const item = items.find(i => i.textContent.trim() === '${label}');
    if (!item) return 'no-item:' + items.map(i=>i.textContent.trim()).join('|');
    clickEl(item);
    return 'picked';
  })()`);
}

console.log("surface:", await shot("surface"), "bytes");
for (const m of ["切面", "等值面", "流线", "矢量箭头"]) {
  const r = await pickMode(m);
  await sleep(4000);
  console.log(m, "->", r, await shot(m), "bytes");
}
ws.close(); edge.kill();
process.exit(0);
