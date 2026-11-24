// Inspector E2E：选中对象 → 参数表单 → 应用
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
await send("Page.navigate", { url: "http://127.0.0.1:8516/index.html" });
await sleep(9000);

const sel = await evalJs(`(async () => {
  const rows=[...document.querySelectorAll('.v-list-item')];
  const row=rows.find(r=>r.textContent?.includes('VF_mid'));
  if(!row) return 'no-row: ' + rows.map(r=>r.textContent?.slice(0,30)).join('|');
  row.click();
  await new Promise(r=>setTimeout(r,1500));
  const t=document.body.innerText;
  return JSON.stringify({ inspector: t.includes('属性 · VF_mid'), axisCtl: t.includes('法向'),
    posCtl: t.includes('位置'), seedCtl: t.includes('种子数') });
})()`);
console.log("Inspector 选中:", sel);

const apply = await evalJs(`(async () => {
  const btn=[...document.querySelectorAll('button')].find(b=>b.innerText==='应用');
  if(!btn) return 'no-apply';
  btn.click();
  await new Promise(r=>setTimeout(r,1500));
  return 'applied';
})()`);
console.log("应用:", apply);

const shot = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
writeFileSync(String.raw`D:\vibeflow_reverse\platform\docs\shots\viz-inspector.png`, Buffer.from(shot.data, "base64"));
console.log("shot:", Buffer.from(shot.data, "base64").length);
ws.close(); edge.kill();
process.exit(0);
