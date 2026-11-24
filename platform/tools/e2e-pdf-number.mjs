import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9225;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-pdf-number`;
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
const ensureDir = async (path, child) => {
  await waitFor(`!!document.querySelector('[data-path="${path}"]')`);
  if (!await evalJs(`!!document.querySelector('[data-path="${child}"]')`)) {
    await evalJs(`document.querySelector('[data-path="${path}"]').click()`);
  }
  await waitFor(`!!document.querySelector('[data-path="${child}"]')`);
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
      document.querySelector('form button').click();
    })()`);
    await waitFor(`!document.querySelector('form') && !!document.querySelector('[data-case-name]')`);
  }
  const caseName = `pdf-number-ui-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  if (!caseId) throw new Error("创建页码 UI 算例失败: " + JSON.stringify(created));
  const tex = "\\documentclass{article}\n\\begin{document}\nSource page one.\\newpage\nSource page two.\n\\end{document}\n";
  await api(`/api/v1/cases/${caseId}/file`, {method: "PUT", body: {path: "E-report/latex/main.tex", content: tex}});
  const build = await api(`/api/v1/cases/${caseId}/latex:build`, {method: "POST", body: {main: "E-report/latex/main.tex", recipe: "pdflatex"}});
  if (!build.body.job_id) throw new Error("PDF 源构建失败: " + JSON.stringify(build));
  let buildStatus = "";
  for (let i = 0; i < 360; i++) {
    const state = await api(`/api/v1/jobs/${build.body.job_id}`);
    buildStatus = state.body.status;
    if (["succeeded", "failed", "cancelled", "interrupted"].includes(buildStatus)) break;
    await sleep(500);
  }
  if (buildStatus !== "succeeded") throw new Error("源 LaTeX PDF 未成功: " + buildStatus);
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await ensureDir("E-report", "E-report/latex");
  await ensureDir("E-report/latex", "E-report/latex/build");
  await ensureDir("E-report/latex/build", "E-report/latex/build/main.pdf");
  await evalJs(`document.querySelector('[data-path="E-report/latex/build/main.pdf"]').click()`);
  await waitFor(`document.querySelector('.textLayer')?.textContent?.includes('Source page one.')`);
  await evalJs(`(() => {
    const button = document.querySelector('[data-testid="pdf-number"]');
    button.click();
    button.click();
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="pdf-viewer"][data-file="E-report/latex/build/main.numbered.pdf"]')`, 60000);
  await waitFor(`document.querySelector('.textLayer')?.textContent?.includes('1 / 2')`);
  const firstText = await evalJs(`document.querySelector('.textLayer').textContent`);
  if (!firstText.includes("Source page one.") || !firstText.includes("1 / 2")) {
    throw new Error("第一页原文或页码缺失: " + firstText);
  }
  const next = await evalJs(`(() => {
    const buttons = [...document.querySelectorAll('button')];
    const button = buttons.find(x => x.textContent === '下一页');
    button.click();
    return true;
  })()`);
  if (!next) throw new Error("下一页按钮不可用");
  await waitFor(`document.querySelector('.textLayer')?.textContent?.includes('2 / 2')`);
  const secondText = await evalJs(`document.querySelector('.textLayer').textContent`);
  if (!secondText.includes("Source page two.") || !secondText.includes("2 / 2")) {
    throw new Error("第二页原文或页码缺失: " + secondText);
  }
  const jobs = await api(`/api/v1/cases/${caseId}/jobs`);
  const numberJobs = jobs.body.filter(job => job.command.includes("pdf_number_worker.py"));
  if (numberJobs.length !== 1) throw new Error("双重点击产生重复页码作业: " + JSON.stringify(jobs.body));
  const source = await api(`/api/v1/cases/${caseId}/raw?path=` + encodeURIComponent("E-report/latex/build/main.pdf"));
  if (source.status !== 200) throw new Error("源 PDF 不可读取: " + JSON.stringify(source));
  await evalJs(`(() => {
    const scroll = document.querySelector('[data-testid="pdf-page-scroll"]');
    scroll.scrollTop = scroll.scrollHeight;
    return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  })()`);
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp09-pdf-numbered.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("PDF numbering UI, text layers, source preservation and debounce: OK", {caseId, jobId: numberJobs[0].id});
} catch (error) {
  const debug = await evalJs(`({
    numberStatus: document.querySelector('[data-testid="pdf-number-status"]')?.textContent ?? '',
    activeEditors: [...document.querySelectorAll('[data-editor-path][data-active="true"]')].map(x => x.dataset.editorPath),
    viewerText: document.querySelector('.textLayer')?.textContent?.slice(0, 300) ?? '',
    body: document.body.textContent.slice(-1000),
  })`).catch(() => ({}));
  console.error("PDF NUMBER E2E FAIL:", error.message, JSON.stringify(debug));
  const shot = await send("Page.captureScreenshot", {format: "png"}).catch(() => null);
  if (shot) writeFileSync(new URL("../docs/shots/wp09-pdf-number-failure.png", import.meta.url), Buffer.from(shot.data, "base64"));
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
