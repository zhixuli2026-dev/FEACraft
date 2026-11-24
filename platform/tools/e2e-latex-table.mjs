import { execFile } from "node:child_process";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;
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
await sleep(2200);
const targets = await getJson("/json/list");
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
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails).slice(0, 500));
  return result.result.value;
};
const api = (path, options = {}) => evalJs(`fetch(${JSON.stringify(path)}, {
  headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
  ${options.method ? `method: ${JSON.stringify(options.method)},` : ""}
  ${options.body ? `body: JSON.stringify(${JSON.stringify(options.body)}),` : ""}
}).then(async r => { const j = await r.json().catch(() => ({})); return {status: r.status, body: j}; })`);
const setInput = async (selector, value, textarea = false) => {
  const found = await evalJs(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) return false;
    const proto = ${textarea ? "HTMLTextAreaElement" : "HTMLInputElement"}.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, ${JSON.stringify(String(value))});
    element.dispatchEvent(new Event('input', {bubbles: true}));
    element.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  })()`);
  if (!found) throw new Error("输入控件不存在: " + selector);
};
const click = async selector => {
  const found = await evalJs(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) return false;
    element.click();
    return true;
  })()`);
  if (!found) throw new Error("按钮不存在: " + selector);
};

try {
  await send("Page.enable");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await sleep(3000);
  if (await evalJs(`!!document.querySelector('form')`)) {
    await evalJs(`(() => {
      const inputs = document.querySelectorAll('input');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(inputs[0], 'dev@vibeflow.cn'); inputs[0].dispatchEvent(new Event('input', {bubbles: true}));
      setter.call(inputs[1], 'devpass123'); inputs[1].dispatchEvent(new Event('input', {bubbles: true}));
      document.querySelector('form button').click();
    })()`);
    await sleep(1800);
  }
  const caseName = `latex-table-ui-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  if (!created.body.id) throw new Error("创建表格测试算例失败: " + JSON.stringify(created));
  const caseId = created.body.id;
  const original = "\\documentclass{article}\n\\begin{document}\nTable UI test.\n\\end{document}\n";
  const written = await api(`/api/v1/cases/${caseId}/file`, {method: "PUT", body: {path: "E-report/latex/main.tex", content: original}});
  if (written.status !== 200) throw new Error("写入 main.tex 失败: " + JSON.stringify(written));
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await sleep(3000);
  let selected = false;
  for (let i = 0; i < 80; i++) {
    await sleep(250);
    selected = await evalJs(`(() => {
      const row = document.querySelector('[data-case-name="${caseName}"]');
      if (!row) return false;
      row.click();
      return true;
    })()`);
    if (selected) break;
  }
  if (!selected) throw new Error("新建表格算例未出现在文件树");
  await sleep(1200);
  await evalJs(`(() => {
    const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === 'caseDict');
    if (!tab) return false;
    const r = tab.getBoundingClientRect();
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
        {bubbles: true, clientX: x, clientY: y, button: 0}));
    }
    return true;
  })()`);
  let editorReady = false;
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    editorReady = await evalJs(`!!document.querySelector('[data-testid="latex-table-editor"]')`);
    if (editorReady) break;
  }
  if (!editorReady) throw new Error("caseDict 表格编辑器未挂载");
  await evalJs(`document.querySelector('[data-testid="latex-table-editor"]').scrollIntoView()`);
  await setInput('[data-testid="latex-table-headers"]', "A&B,值");
  await setInput('[data-testid="latex-table-rows"]', "x_1,%$#", true);
  await click('[data-testid="latex-table-generate"]');
  let preview = "";
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    preview = await evalJs(`document.querySelector('[data-testid="latex-table-preview"]')?.textContent ?? ''`);
    if (preview) break;
  }
  if (!preview) {
    const debug = await evalJs(`document.querySelector('[data-testid="latex-table-editor"]').parentElement.textContent`);
    throw new Error("表格预览未生成: " + debug);
  }
  if (!preview.includes("A\\&B") || !preview.includes("\\toprule")) {
    throw new Error("表格预览缺少转义或 booktabs: " + preview);
  }
  await click('[data-testid="latex-table-insert"]');
  let status = "";
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    status = await evalJs(`document.querySelector('[data-testid="latex-table-editor"]').parentElement.textContent`);
    if (status.includes("表格已插入")) break;
  }
  if (!status.includes("表格已插入")) throw new Error("表格插入状态不正确: " + status);
  const content = (await api(`/api/v1/cases/${caseId}/file?path=` + encodeURIComponent("E-report/latex/main.tex"))).body.content;
  if (!content.includes("A\\&B") || !content.includes("\\begin{tabular}") || content.indexOf("\\begin{tabular}") > content.indexOf("\\end{document}")) {
    throw new Error("表格未正确插入 main.tex: " + content);
  }
  const backup = status.split("原文件已备份为 ").pop().trim();
  const backupContent = (await api(`/api/v1/cases/${caseId}/file?path=` + encodeURIComponent(backup))).body.content;
  if (backupContent !== original) throw new Error("表格备份不是原始 main.tex: " + backup);
  console.log("LaTeX table UI generate/insert/backup: OK", {caseId, backup});
} catch (error) {
  console.error("LATEX TABLE E2E FAIL:", error.message);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
