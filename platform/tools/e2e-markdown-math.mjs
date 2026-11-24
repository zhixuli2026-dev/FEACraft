import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9228;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-markdown-math`;
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
  const caseName = `markdown-math-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  const source = `# MathPreview\n\nInline $E=mc^2$ and \\(a+b\\).\n\n$$\n\\nabla \\cdot \\mathbf{u}=0\n$$\n\n\\[\nx_i^2\n\\]\n\n\`$not_math$\`\n\n<script>window.__markdownScript=1</script>\n\n$\\href{javascript:alert(1)}{click}$`;
  const written = await api(`/api/v1/cases/${caseId}/file`, {method: "PUT", body: {path: "E-report/math.md", content: source}});
  if (written.status !== 200) throw new Error("写入数学 Markdown 失败");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await waitFor(`!!document.querySelector('[data-path="E-report/math.md"]')`);
  await evalJs(`document.querySelector('[data-path="E-report/math.md"]').click()`);
  await waitFor(`!!document.querySelector('[data-testid="markdown-preview-toggle"]')`);
  await evalJs(`document.querySelector('[data-testid="markdown-preview-toggle"]').click()`);
  await waitFor(`document.querySelectorAll('[data-testid="markdown-preview"] .katex').length >= 4`);
  const result = await evalJs(`(() => {
    const preview = document.querySelector('[data-testid="markdown-preview"]');
    const code = preview.querySelector('code');
    return {
      text: preview.textContent,
      displays: preview.querySelectorAll('.katex-display').length,
      code: code?.textContent ?? '',
      codeHasMath: !!code?.querySelector('.katex'),
      scripts: preview.querySelectorAll('script').length,
      links: preview.querySelectorAll('a').length,
      executed: typeof window.__markdownScript !== 'undefined',
    };
  })()`);
  if (!result.text.includes("MathPreview") || result.displays < 2) throw new Error("显示公式缺失: " + JSON.stringify(result));
  if (result.code !== "$not_math$" || result.codeHasMath) throw new Error("代码被误解析为公式: " + JSON.stringify(result));
  if (result.scripts || result.links || result.executed) throw new Error("Markdown/Math 安全边界失败: " + JSON.stringify(result));
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp09-markdown-math.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("Markdown math preview, code exclusion and script/link rejection: OK", {caseId, displays: result.displays});
} catch (error) {
  const debug = await evalJs(`document.querySelector('[data-testid="markdown-preview"]')?.textContent ?? document.body.textContent.slice(-600)`).catch(() => "");
  console.error("MARKDOWN MATH E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
