import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9229;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-csv`;
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
const setSearch = async value => {
  await evalJs(`(() => {
    const input = document.querySelector('[data-testid="csv-search"]');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, ${JSON.stringify(value)});
    input.dispatchEvent(new Event('input', {bubbles: true}));
  })()`);
};

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
  const caseName = `csv-reader-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const csv = `name,note,value\nalpha,"comma,inside",1\nbeta,"quote ""inside""",2\ngamma,"first line\nsecond line",3\n space ," keep ",4\n`;
  let result = await api(`/api/v1/cases/${caseId}/file`, {method: "PUT", body: {path: "D-post/data.csv", content: csv}});
  if (result.status !== 200) throw new Error("写入 CSV 失败");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await waitFor(`!!document.querySelector('[data-path="D-post/data.csv"]')`);
  await evalJs(`document.querySelector('[data-path="D-post/data.csv"]').click()`);
  await waitFor(`document.querySelectorAll('[data-testid="csv-viewer"] tbody tr').length === 4`);
  const initial = await evalJs(`(() => ({
    text: document.querySelector('[data-testid="csv-viewer"]').textContent,
    cells: [...document.querySelectorAll('[data-testid="csv-viewer"] tbody tr')].map(r => [...r.cells].map(c => c.textContent)),
  }))()`);
  for (const marker of ["comma,inside", 'quote "inside"', "first line\nsecond line", " space ", " keep "]) {
    if (!initial.text.includes(marker)) throw new Error("CSV 内容缺失 " + JSON.stringify(marker));
  }
  if (initial.cells.length !== 4) throw new Error("引号内换行被误拆: " + JSON.stringify(initial.cells));
  await evalJs(`document.querySelector('[data-testid="csv-header"][data-column="2"]').click()`);
  await evalJs(`document.querySelector('[data-testid="csv-header"][data-column="2"]').click()`);
  const firstValue = await evalJs(`document.querySelector('[data-testid="csv-viewer"] tbody tr').cells[2].textContent`);
  if (firstValue !== "4") throw new Error("数值降序失败: " + firstValue);
  await setSearch("second line");
  await waitFor(`document.querySelectorAll('[data-testid="csv-viewer"] tbody tr').length === 1`);
  const filtered = await evalJs(`document.querySelector('[data-testid="csv-viewer"] tbody tr').textContent`);
  if (!filtered.includes("gamma") || !filtered.includes("first line\nsecond line")) throw new Error("多行字段搜索失败: " + filtered);
  await setSearch("");
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp09-csv-reader.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("CSV logical rows/quotes/newlines/sort/search: OK", {caseId});

  result = await api(`/api/v1/cases/${caseId}/file`, {method: "PUT", body: {path: "D-post/bad.csv", content: 'a,b\n"unterminated,2'}});
  if (result.status !== 200) throw new Error("写入坏 CSV 失败");
  await waitFor(`!!document.querySelector('[data-path="D-post/bad.csv"]')`);
  await evalJs(`document.querySelector('[data-path="D-post/bad.csv"]').click()`);
  await waitFor(`document.querySelector('[data-testid="csv-warning"]')?.textContent?.includes('解析警告')`);
  console.log("Malformed CSV warning: OK", {caseId});
} catch (error) {
  const debug = await evalJs(`document.querySelector('[data-testid="csv-viewer"]')?.textContent ?? document.body.textContent.slice(-600)`).catch(() => "");
  console.error("CSV E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
