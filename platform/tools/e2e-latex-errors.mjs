import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9224;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-latex-errors`;
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
const page = targets.find(target => target.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
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
const waitFor = async (expression, timeout = 20000) => {
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
      document.querySelector('form button').click();
    })()`);
    await waitFor(`!document.querySelector('form') && !!document.querySelector('[data-case-name]')`);
  }
  const caseName = `latex-errors-ui-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  if (!created.body.id) throw new Error("创建诊断测试算例失败: " + JSON.stringify(created));
  const caseId = created.body.id;
  const errorLine = 83;
  const original = "\\documentclass{article}\n\\begin{document}\n" +
    Array.from({length: 80}, (_, i) => `% filler ${i + 3}`).join("\n") +
    "\n\\vibeflowUndefinedCommand\n\\end{document}\n";
  const written = await api(`/api/v1/cases/${caseId}/file`, {method: "PUT", body: {path: "E-report/latex/main.tex", content: original}});
  if (written.status !== 200) throw new Error("写入失败 main.tex 失败: " + JSON.stringify(written));
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await waitFor(`[...document.querySelectorAll('.dv-tab')].some(t => t.textContent?.trim() === 'caseDict')`);
  await evalJs(`(() => {
    const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === 'caseDict');
    const r = tab.getBoundingClientRect();
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
        {bubbles: true, clientX: x, clientY: y, button: 0}));
    }
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="latex-build"]')`);
  await evalJs(`(() => {
    const select = document.querySelector('[data-testid="latex-recipe"]');
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, 'pdflatex');
    select.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  await evalJs(`(() => {
    const button = document.querySelector('[data-testid="latex-build"]');
    button.click();
    button.click();
  })()`);
  let diagnosticReady = false;
  for (let i = 0; i < 720; i++) {
    const diagnosticError = await evalJs(`document.querySelector('[data-testid="latex-diagnostic-error"]')?.textContent ?? ''`);
    if (diagnosticError) throw new Error(diagnosticError);
    diagnosticReady = await evalJs(`[...document.querySelectorAll('[data-testid="latex-diagnostic"]')].some(x => x.dataset.line === '${errorLine}' && x.textContent.includes('Undefined control sequence'))`);
    if (diagnosticReady) break;
    await sleep(250);
  }
  if (!diagnosticReady) {
    const text = await evalJs(`document.querySelector('[data-testid="latex-build"]').parentElement.parentElement.textContent`);
    throw new Error("真实失败编译未返回第 83 行结构化错误: " + text);
  }
  const jobs = await api(`/api/v1/cases/${caseId}/jobs`);
  const latexJobs = jobs.body.filter(job => job.command.includes("latexmk"));
  if (latexJobs.length !== 1) throw new Error("双重点击产生了重复编译作业: " + JSON.stringify(jobs.body));
  console.log("LaTeX failure diagnostics list and double-click debounce: OK", {caseId, errorLine});
  await evalJs(`(() => {
    const item = [...document.querySelectorAll('[data-testid="latex-diagnostic"]')].find(x => x.dataset.line === '${errorLine}' && x.textContent.includes('Undefined control sequence'));
    item.click();
  })()`);
  await waitFor(`!!document.querySelector('[data-editor-path="E-report/latex/main.tex"][data-active="true"]')`);
  await waitFor(`document.querySelector('.view-lines')?.textContent?.includes('vibeflowUndefinedCommand')`);
  const visibleLines = await evalJs(`document.querySelector('.view-lines').textContent`);
  if (!visibleLines.includes("vibeflowUndefinedCommand")) throw new Error("Monaco 未显示错误行");
  const unchanged = await api(`/api/v1/cases/${caseId}/file?path=` + encodeURIComponent("E-report/latex/main.tex"));
  if (unchanged.body.content !== original) throw new Error("诊断定位修改了源文件");
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp09-latex-errors.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("LaTeX diagnostic source navigation and visible line: OK", {caseId, errorLine});
} catch (error) {
  console.error("LATEX ERRORS E2E FAIL:", error.message);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
