import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9231;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-pdf-reader`;
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
const waitFor = async (expression, timeout = 40000) => {
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
const openPdf = async caseName => {
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await ensureDir("E-report", "E-report/latex");
  await ensureDir("E-report/latex", "E-report/latex/build");
  await ensureDir("E-report/latex/build", "E-report/latex/build/main.pdf");
  await evalJs(`document.querySelector('[data-path="E-report/latex/build/main.pdf"]').click()`);
};

ws.on("message", data => {
  const message = JSON.parse(data);
  if (message.method === "Fetch.requestPaused") {
    console.log("SYNCTEX REQUEST", message.params.request.postData ?? "");
    send("Fetch.continueRequest", {requestId: message.params.requestId}).catch(() => {});
  }
});

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
  const caseName = `pdf-reader-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const tex = "\\documentclass{article}\n\\usepackage{hyperref}\n\\begin{document}\n\\section{Introduction}\nIntro range text.\\newpage\n\\section{Results}\nUniqueResultText and conclusion.\n\\end{document}\n";
  await api(`/api/v1/cases/${caseId}/file`, {method: "PUT", body: {path: "E-report/latex/main.tex", content: tex}});
  const build = await api(`/api/v1/cases/${caseId}/latex:build`, {method: "POST", body: {main: "E-report/latex/main.tex", recipe: "pdflatex"}});
  let buildStatus = "";
  for (let i = 0; i < 360; i++) {
    const state = await api(`/api/v1/jobs/${build.body.job_id}`);
    buildStatus = state.body.status;
    if (["succeeded", "failed", "cancelled", "interrupted"].includes(buildStatus)) break;
    await sleep(500);
  }
  if (buildStatus !== "succeeded") throw new Error("PDF 源构建失败: " + buildStatus);
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await openPdf(caseName);
  await waitFor(`document.querySelector('.textLayer')?.textContent?.includes('Intro range text.')`);
  const range = await evalJs(`document.querySelector('[data-testid="pdf-viewer"]').dataset.rangeRequests`);
  if (Number(range) <= 0) throw new Error("PDF.js 未发起 Range 请求");
  await waitFor(`document.querySelectorAll('[data-testid="pdf-outline-item"]').length >= 2`);
  const outline = await evalJs(`[...document.querySelectorAll('[data-testid="pdf-outline-item"]')].map(x => x.textContent)`);
  if (!outline.includes("Introduction") || !outline.includes("Results")) throw new Error("PDF 目录缺失: " + JSON.stringify(outline));
  await evalJs(`[...document.querySelectorAll('[data-testid="pdf-outline-item"]')].find(x => x.textContent === 'Results').click()`);
  await waitFor(`document.querySelector('[data-testid="pdf-page-indicator"]')?.textContent === '2 / 2'`);
  await waitFor(`document.querySelector('.textLayer')?.textContent?.includes('UniqueResultText')`);
  await evalJs(`document.querySelector('[data-testid="pdf-prev"]').click()`);
  await waitFor(`document.querySelector('[data-testid="pdf-page-indicator"]')?.textContent === '1 / 2'`);
  await evalJs(`(() => {
    const input = document.querySelector('[data-testid="pdf-search"]');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, 'UniqueResultText');
    input.dispatchEvent(new Event('input', {bubbles: true}));
  })()`);
  await evalJs(`document.querySelector('[data-testid="pdf-search-run"]').click()`);
  await waitFor(`!!document.querySelector('[data-testid="pdf-search-result"][data-page="2"]')`);
  await evalJs(`document.querySelector('[data-testid="pdf-search-result"][data-page="2"]').click()`);
  await waitFor(`document.querySelector('[data-testid="pdf-page-indicator"]')?.textContent === '2 / 2'`);
  await evalJs(`document.querySelector('[data-testid="pdf-zoom-in"]').click()`);
  await waitFor(`document.querySelector('[data-testid="pdf-viewer"]')?.dataset.scale === '1.4'`);
  console.log("PDF range/outline/search: OK", {caseId, rangeRequests: Number(range)});

  await send("Page.navigate", {url: "http://localhost:5173/"});
  await openPdf(caseName);
  await waitFor(`document.querySelector('[data-testid="pdf-page-indicator"]')?.textContent === '2 / 2'`);
  await waitFor(`document.querySelector('.textLayer')?.textContent?.includes('UniqueResultText')`);
  const restoredScale = await evalJs(`document.querySelector('[data-testid="pdf-viewer"]').dataset.scale`);
  if (restoredScale !== "1.4") throw new Error("缩放未恢复: " + restoredScale);
  console.log("PDF page/scale persistence: OK", {caseId});

  await evalJs(`(() => {
    const spans = [...document.querySelectorAll('.textLayer span')];
    const span = spans.find(x => x.textContent.includes('UniqueResultText'));
    if (!span) throw new Error('选区文本不存在');
    const range = document.createRange();
    range.selectNodeContents(span);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    span.closest('.textLayer').parentElement.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
  })()`);
  await waitFor(`!!document.querySelector('[data-testid="pdf-translate"]')`);
  await evalJs(`document.querySelector('[data-testid="pdf-translate"]').click()`);
  await waitFor(`document.querySelector('[data-testid="pdf-translation"]')?.textContent?.includes('[mock-nonstream]')`, 60000);
  await send("Browser.grantPermissions", {origin: "http://localhost:5173", permissions: ["clipboardReadWrite", "clipboardSanitizedWrite"]});
  await evalJs(`document.querySelector('[data-testid="pdf-copy-translation"]').click()`);
  const clipboard = await evalJs(`navigator.clipboard.readText()`);
  if (!clipboard.includes("[mock-nonstream]")) throw new Error("剪贴板不是实际译文: " + clipboard);
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp09-pdf-reader.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("PDF selection translation and clipboard: OK", {caseId});

  await send("Fetch.enable", {patterns: [{urlPattern: "*/api/v1/cases/*/synctex:query*"}]});
  const target = await evalJs(`(() => {
    const span = [...document.querySelectorAll('.textLayer span')].find(x => x.textContent.includes('UniqueResultText'));
    const r = span.getBoundingClientRect();
    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
  })()`);
  for (const clickCount of [1, 2]) {
    await send("Input.dispatchMouseEvent", {type: "mousePressed", x: target.x, y: target.y,
      button: "left", buttons: 1, clickCount});
    await send("Input.dispatchMouseEvent", {type: "mouseReleased", x: target.x, y: target.y,
      button: "left", buttons: 0, clickCount});
  }
  await waitFor(`!!document.querySelector('[data-editor-path="E-report/latex/main.tex"][data-active="true"]')`, 60000);
  await waitFor(`document.querySelector('[data-testid="editor-cursor-line"]')?.textContent === 'L7'`);
  await waitFor(`document.querySelector('.view-lines')?.textContent?.includes('UniqueResultText') || document.querySelector('.view-lines')?.textContent?.includes('section{Results}')`);
  const syncShot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp09-synctex.png", import.meta.url), Buffer.from(syncShot.data, "base64"));
  console.log("SyncTeX double-click PDF to source: OK", {caseId});

  const largeName = `pdf-range-large-${Date.now()}`;
  const largeCase = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: largeName}});
  const largeId = largeCase.body.id;
  const block = seed => {
    let x = seed, text = "";
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    for (let i = 0; i < 12000; i++) {
      x = (1664525 * x + 1013904223) >>> 0;
      text += alphabet[x % alphabet.length];
      if (i % 80 === 79) text += " ";
    }
    return text;
  };
  const largeTex = "\\documentclass{article}\n\\begin{document}\n" +
    Array.from({length: 30}, (_, i) => `\\section{Section ${i + 1}}\n${block(i + 1)}\\newpage\n`).join("\n") +
    "\\end{document}\n";
  await api(`/api/v1/cases/${largeId}/file`, {method: "PUT", body: {path: "E-report/latex/main.tex", content: largeTex}});
  const largeBuild = await api(`/api/v1/cases/${largeId}/latex:build`, {method: "POST", body: {main: "E-report/latex/main.tex", recipe: "pdflatex"}});
  let largeStatus = "";
  for (let i = 0; i < 720; i++) {
    const state = await api(`/api/v1/jobs/${largeBuild.body.job_id}`);
    largeStatus = state.body.status;
    if (["succeeded", "failed", "cancelled", "interrupted"].includes(largeStatus)) break;
    await sleep(500);
  }
  if (largeStatus !== "succeeded") throw new Error("大型 PDF 构建失败: " + largeStatus);
  const pdfSize = await evalJs(`fetch('/api/v1/cases/${largeId}/raw?path=' + encodeURIComponent('E-report/latex/build/main.pdf'), {
    method: 'HEAD', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')},
  }).then(r => Number(r.headers.get('Content-Length')))`);
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await openPdf(largeName);
  await waitFor(`document.querySelector('.textLayer')?.textContent?.includes('Section 1')`, 60000);
  const largeRange = await evalJs(`({requests: Number(document.querySelector('[data-testid="pdf-viewer"]').dataset.rangeRequests), bytes: Number(document.querySelector('[data-testid="pdf-viewer"]').dataset.rangeBytes)})`);
  if (largeRange.requests < 2 || largeRange.bytes <= 0 || largeRange.bytes >= pdfSize) {
    throw new Error("大型 PDF 未按区间增量读取: " + JSON.stringify({pdfSize, ...largeRange}));
  }
  console.log("Large PDF uses multiple bounded range reads: OK", {caseId: largeId, pdfSize, ...largeRange});
} catch (error) {
  const debug = await evalJs(`({page: document.querySelector('[data-testid="pdf-page-indicator"]')?.textContent ?? '', cursor: document.querySelector('[data-testid="editor-cursor-line"]')?.textContent ?? '', range: document.querySelector('[data-testid="pdf-viewer"]')?.dataset ?? {}, text: document.body.textContent.slice(-800)})`).catch(() => ({}));
  console.error("PDF READER E2E FAIL:", error.message, JSON.stringify(debug));
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
