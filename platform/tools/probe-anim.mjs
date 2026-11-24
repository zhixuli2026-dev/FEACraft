// Animation E2E：播放/暂停按钮存在 + 导帧 → D-post/animations 出帧序列
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
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 250));
  return r.result.value;
};

await send("Page.enable");
await send("Page.navigate", { url: "http://127.0.0.1:8518/index.html" });
await sleep(9000);

// 1. 播放：点播放图标钮（toolbar 里第二个 v-btn--icon；第一个是 hamburger）→ 时间步应自动前进
const play = await evalJs(`(async () => {
  const t0 = document.body.innerText.match(/t=[\\d.]+/)?.[0] ?? '';
  const iconBtns = [...document.querySelectorAll('button.v-btn--icon')];
  const btn = iconBtns[1];   // [0]=hamburger, [1]=播放
  if (!btn) return 'no-play-btn';
  btn.click();
  await new Promise(r => setTimeout(r, 3000));
  const t1 = document.body.innerText.match(/t=[\\d.]+/)?.[0] ?? '';
  btn.click();   // 暂停
  return JSON.stringify({ t0, t1, advanced: t0 !== t1 });
})()`);
console.log("播放:", play);

// 2. 导帧
const exp = await evalJs(`(async () => {
  const btn = [...document.querySelectorAll('button')].find(b => b.innerText === '导帧');
  if (!btn) return 'no-export-btn';
  btn.click();
  await new Promise(r => setTimeout(r, 8000));
  return document.body.innerText.includes('帧动画帧') ? 'exported' : 'no-status';
})()`);
console.log("导帧:", exp);

// 3. 帧文件落盘验证（页面外的 HTTP 不算 kernel 事——读不了；看 kernel 日志在 tail）
const shot = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
writeFileSync(String.raw`D:\vibeflow_reverse\platform\docs\shots\viz-anim.png`, Buffer.from(shot.data, "base64"));
ws.close(); edge.kill();
process.exit(0);
