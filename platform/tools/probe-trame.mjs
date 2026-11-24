// 实时 CDP 探查 trame 客户端（拿 console 输出 + 截图）
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

const url = process.argv[2] || "http://127.0.0.1:8501/index.html";
const waitS = Number(process.argv[3] || 10);
const out = process.argv[4] || String.raw`D:\tmp\trame-live.png`;

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
  const h = (d) => { const x = JSON.parse(d); if (x.id === i) { ws.off("message", h); x.error ? rej(new Error(JSON.stringify(x.error))) : res(x.result); } };
  ws.on("message", h); ws.send(JSON.stringify({ id: i, method: m, params: p }));
});
ws.on("message", (d) => {
  const m = JSON.parse(d);
  if (m.method === "Runtime.consoleAPICalled") {
    const args = (m.params.args || []).map(a => a.value ?? a.description ?? "").join(" ");
    console.log("[console]", m.params.type, args.slice(0, 250));
  }
  if (m.method === "Log.entryAdded") {
    const e = m.params.entry;
    console.log("[log]", e.level, (e.text || "").slice(0, 250), e.url || "");
  }
});
await send("Runtime.enable");
await send("Log.enable");
await send("Page.enable");
await send("Page.navigate", { url });
await sleep(waitS * 1000);
const state = await send("Runtime.evaluate", { expression: `document.body.innerText.slice(0,300)`, returnByValue: true });
console.log("body:", JSON.stringify(state.result.value?.slice(0, 200)));
const shot = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
writeFileSync(out, Buffer.from(shot.data, "base64"));
console.log("shot bytes:", Buffer.from(shot.data, "base64").length, "->", out);
ws.close(); edge.kill();
process.exit(0);
