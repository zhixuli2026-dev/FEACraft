// 探针：直接测 pdf.min.js 的加载行为
import { execFile } from "node:child_process";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const edge = execFile(EDGE, ["--headless=new", "--disable-gpu",
  "--remote-debugging-port=9223",
  `--user-data-dir=${String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`}`,
  "about:blank"], () => {});
await sleep(2500);
const targets = await new Promise((res, rej) => http.get({ host: "127.0.0.1", port: 9223, path: "/json/list" },
  (r) => { let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => res(JSON.parse(b))); }).on("error", rej));
const page = targets.find((t) => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.on("open", r));
let id = 0; const pending = new Map();
ws.on("message", (d) => { const m = JSON.parse(d); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
const send = (method, params = {}) => new Promise((res) => { const mid = ++id; pending.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); });
const evalJs = async (e) => {
  const r = await send("Runtime.evaluate", { expression: e, returnByValue: true, awaitPromise: true });
  return r.result?.result?.value ?? JSON.stringify(r.result?.exceptionDetails ?? r);
};

await send("Page.navigate", { url: "http://localhost:5173/" });
await sleep(4000);
console.log("load pdf.js:", await evalJs(`
  new Promise((resolve) => {
    const sc = document.createElement('script');
    sc.src = '/pdfjs/pdf.min.js';
    sc.onload = () => resolve('loaded pdfjsLib=' + (typeof window.pdfjsLib));
    sc.onerror = (e) => resolve('onerror fired: ' + e.type);
    document.head.appendChild(sc);
  })
`));
console.log("getDocument test:", await evalJs(`
  (async () => {
    try {
      const res = await fetch('/api/v1/cases/1/raw?path=Ref/test.pdf', {
        headers: { Authorization: 'Bearer ' + localStorage.getItem('vf_token') } });
      const data = await res.arrayBuffer();
      const doc = await window.pdfjsLib.getDocument({ data }).promise;
      return 'pages=' + doc.numPages;
    } catch (e) { return 'ERR: ' + (e?.message || String(e)); }
  })()
`));
ws.close(); edge.kill();
