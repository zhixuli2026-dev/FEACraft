import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9226;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-documents`;
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
const uploadPng = (caseId, path) => evalJs(`(() => {
  const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='), c => c.charCodeAt(0));
  const form = new FormData();
  form.append('file', new Blob([bytes], {type: 'image/png'}), 'pixel.png');
  return fetch('/api/v1/cases/${caseId}/upload?path=' + encodeURIComponent(${JSON.stringify(path)}), {
    method: 'POST', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}, body: form,
  }).then(async r => ({status: r.status, body: await r.json()}));
})()`);
const openPath = async path => {
  await waitFor(`!!document.querySelector('[data-path="${path}"][data-dir="false"]')`);
  await evalJs(`document.querySelector('[data-path="${path}"]').click()`);
};
const articleText = `document.querySelector('[data-testid="document-shadow"]')?.shadowRoot?.querySelector('article')?.textContent ?? ''`;

try {
  await send("Page.enable");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`document.readyState === 'complete' && (!!document.querySelector('form') || !!document.querySelector('[data-case-name]'))`);
  if (await evalJs(`!!document.querySelector('form')`)) {
    await evalJs(`(() => {
      const inputs = document.querySelectorAll('input');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(inputs[0], 'dev@vibeflow.cn');
      inputs[0].dispatchEvent(new Event('input', {bubbles: true}));
      inputs[0].dispatchEvent(new Event('change', {bubbles: true}));
      setter.call(inputs[1], 'devpass123');
      inputs[1].dispatchEvent(new Event('input', {bubbles: true}));
      inputs[1].dispatchEvent(new Event('change', {bubbles: true}));
      document.querySelector('form').requestSubmit();
    })()`);
    await waitFor(`!document.querySelector('form') && !!document.querySelector('[data-case-name]')`);
  }
  const caseName = `document-reader-${Date.now()}`;
  const created = await api("/api/v1/workspaces/1/cases", {method: "POST", body: {name: caseName}});
  const caseId = created.body.id;
  if (!caseId) throw new Error("创建阅读器算例失败: " + JSON.stringify(created));
  const markdown = "# ReportTitle\n\nIntro paragraph.\n\n| Name | Value |\n|---|---:|\n| Cell42 | 42 |\n\n![](pixel.png)\n\n" +
    Array.from({length: 70}, (_, i) => `Paragraph ${i + 1} with report text.\n`).join("\n") +
    "\n## SectionTwo\n\nFinal section text.\n";
  let result = await api(`/api/v1/cases/${caseId}/file`, {method: "PUT", body: {path: "E-report/report.md", content: markdown}});
  if (result.status !== 200) throw new Error("写入报告 Markdown 失败");
  for (const path of ["pixel.png", "E-report/build/pixel.png"]) {
    const uploaded = await uploadPng(caseId, path);
    if (uploaded.status !== 200) throw new Error("PNG 上传失败: " + JSON.stringify(uploaded));
  }
  const build = await api(`/api/v1/cases/${caseId}/report:build`, {method: "POST"});
  if (!build.body.job_id) throw new Error("pandoc 作业提交失败: " + JSON.stringify(build));
  let buildStatus = "";
  for (let i = 0; i < 240; i++) {
    const state = await api(`/api/v1/jobs/${build.body.job_id}`);
    buildStatus = state.body.status;
    if (["succeeded", "failed", "cancelled", "interrupted"].includes(buildStatus)) break;
    await sleep(500);
  }
  if (buildStatus !== "succeeded") throw new Error("pandoc HTML/DOCX 未生成: " + buildStatus);
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await waitFor(`!!document.querySelector('[data-path="E-report"]')`);
  if (!await evalJs(`!!document.querySelector('[data-path="E-report/build"]')`)) {
    await evalJs(`document.querySelector('[data-path="E-report"]').click()`);
  }
  if (!await evalJs(`!!document.querySelector('[data-path="E-report/build/report.html"]')`)) {
    await evalJs(`document.querySelector('[data-path="E-report/build"]').click()`);
  }
  await openPath("E-report/build/report.html");
  await waitFor(`(${articleText}).includes('ReportTitle')`);
  let content = await evalJs(articleText);
  if (!content.includes("Cell42") || !content.includes("Intro paragraph.")) throw new Error("HTML 表格/正文缺失");
  await waitFor(`(() => {
    const article = document.querySelector('[data-testid="document-shadow"]').shadowRoot?.querySelector('article');
    return !!article && [...article.querySelectorAll('img')].some(x => x.complete && x.naturalWidth > 0);
  })()`);
  const htmlImage = await evalJs(`(() => {
    const article = document.querySelector('[data-testid="document-shadow"]').shadowRoot.querySelector('article');
    const images = [...article.querySelectorAll('img')];
    return {count: images.length, loaded: images.some(x => x.complete && x.naturalWidth > 0), blob: images.some(x => x.src.startsWith('blob:'))};
  })()`);
  if (!htmlImage.count || !htmlImage.loaded || !htmlImage.blob) throw new Error("HTML 鉴权 Blob 图片未显示: " + JSON.stringify(htmlImage));
  if (await evalJs(`!!document.querySelector('[data-testid="document-shadow"]').shadowRoot.querySelector('article').querySelector('script,iframe,form')`)) {
    throw new Error("HTML 阅读器包含危险标签");
  }
  await evalJs(`[...document.querySelectorAll('[data-testid="document-heading"]')].find(x => x.textContent === 'SectionTwo').click()`);
  await waitFor(`document.querySelector('[data-testid="document-shadow"]').scrollTop > 0`);
  console.log("HTML reader text/table/blob image/headings: OK", {caseId});

  await openPath("E-report/build/report.docx");
  await waitFor(`document.querySelector('[data-testid="document-reader"]')?.dataset.kind === 'docx' && (${articleText}).includes('ReportTitle')`, 60000);
  content = await evalJs(articleText);
  if (!content.includes("Cell42") || !content.includes("Final section text.")) throw new Error("DOCX 表格/正文缺失");
  await waitFor(`(() => {
    const article = document.querySelector('[data-testid="document-shadow"]').shadowRoot?.querySelector('article');
    return !!article && [...article.querySelectorAll('img')].some(x => x.complete && x.naturalWidth > 0);
  })()`);
  const docxImage = await evalJs(`(() => {
    const article = document.querySelector('[data-testid="document-shadow"]').shadowRoot.querySelector('article');
    const images = [...article.querySelectorAll('img')];
    return {count: images.length, loaded: images.some(x => x.complete && x.naturalWidth > 0)};
  })()`);
  if (!docxImage.count || !docxImage.loaded) throw new Error("DOCX 图片未显示: " + JSON.stringify(docxImage));
  if (await evalJs(`!!document.querySelector('[data-testid="document-shadow"]').shadowRoot.querySelector('article').querySelector('script,iframe,form,input,object,embed')`)) {
    throw new Error("DOCX 阅读器包含可执行/嵌入标签");
  }
  if (await evalJs(`document.querySelectorAll('[data-testid="document-heading"]').length < 2`)) throw new Error("DOCX 目录缺失");
  const shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/wp09-docx-reader.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("DOCX semantic reader text/table/image/navigation: OK", {caseId});

  const attack = "<h1 id='top'>Attack</h1><script>window.__documentScript=1</script>" +
    "<img src='missing.png' onerror='window.__documentScript=3'><iframe src='https://example.com'></iframe>" +
    "<form><input value='bad'></form><a id='bad' href=\"javascript:window.__documentScript=2\">BadLink</a>" +
    "<a id='go' href='#section'>GoSection</a><div style='position:fixed;inset:0'>StyleRemoved</div>" +
    Array.from({length: 50}, (_, i) => `<p>Attack filler ${i}</p>`).join("") +
    "<h2 id='section'>SafeSection</h2><a id='linked' href='linked.html'>LinkedDocument</a>";
  const linked = "<h1>LinkedDoc</h1><p>Cross file navigation works.</p>";
  for (const [path, source] of [["E-report/build/attack.html", attack], ["E-report/build/linked.html", linked]]) {
    result = await api(`/api/v1/cases/${caseId}/file`, {method: "PUT", body: {path, content: source}});
    if (result.status !== 200) throw new Error("写入 HTML 负例失败");
  }
  await openPath("E-report/build/attack.html");
  await waitFor(`(${articleText}).includes('SafeSection')`);
  const safety = await evalJs(`(() => {
    const article = document.querySelector('[data-testid="document-shadow"]').shadowRoot.querySelector('article');
    return {
      script: typeof window.__documentScript !== 'undefined',
      forbidden: !!article.querySelector('script,iframe,form,input,object,embed'),
      inlineStyle: !!article.querySelector('[style]'),
      eventHandler: [...article.querySelectorAll('*')].some(x => [...x.attributes].some(a => a.name.toLowerCase().startsWith('on'))),
    };
  })()`);
  if (Object.values(safety).some(Boolean)) throw new Error("HTML 消毒失败: " + JSON.stringify(safety));
  await evalJs(`document.querySelector('[data-testid="document-shadow"]').shadowRoot.querySelector('#bad').click()`);
  await waitFor(`document.querySelector('[data-testid="document-notice"]')?.textContent?.includes('阻止')`);
  if (await evalJs(`typeof window.__documentScript !== 'undefined'`)) throw new Error("javascript: 链接触发执行");
  await evalJs(`document.querySelector('[data-testid="document-shadow"]').shadowRoot.querySelector('#go').click()`);
  await waitFor(`document.querySelector('[data-testid="document-shadow"]').scrollTop > 0`);
  await evalJs(`document.querySelector('[data-testid="document-shadow"]').shadowRoot.querySelector('#linked').click()`);
  await waitFor(`document.querySelector('[data-testid="document-reader"]')?.dataset.file === 'E-report/build/linked.html' && (${articleText}).includes('LinkedDoc')`);
  console.log("HTML script/form/iframe/event/style rejection, anchors and cross-file links: OK", {caseId});
} catch (error) {
  const debug = await evalJs(`({reader: document.querySelector('[data-testid="document-reader"]')?.outerHTML?.slice(0, 500) ?? '', hasToken: !!localStorage.getItem('vf_token'), email: document.querySelector('input[type="email"]')?.value ?? '', passwordLength: document.querySelector('input[type="password"]')?.value?.length ?? 0, text: document.body.textContent.slice(-800)})`).catch(() => ({}));
  console.error("DOCUMENT READER E2E FAIL:", error.message, JSON.stringify(debug));
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
