import { execFile } from "node:child_process";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function getJson(path) {
  return new Promise((resolve, reject) => {
    http.get({ host: "127.0.0.1", port: PORT, path }, (res) => {
      let body = "";
      res.on("data", (chunk) => body += chunk);
      res.on("end", () => resolve(JSON.parse(body)));
    }).on("error", reject);
  });
}

const edge = execFile(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profileDir}`, "--window-size=1500,900", "about:blank"], () => {});
await sleep(2200);
const targets = await getJson("/json/list");
const page = targets.find((target) => target.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve) => ws.on("open", resolve));
let id = 0;
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const mid = ++id;
  const onMessage = (data) => {
    const message = JSON.parse(data);
    if (message.id === mid) {
      ws.off("message", onMessage);
      message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
    }
  };
  ws.on("message", onMessage);
  ws.send(JSON.stringify({ id: mid, method, params }));
});
const evalJs = async (expression) => {
  const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails).slice(0, 600));
  return result.result.value;
};
let dialogAnswer = "";
ws.on("message", (data) => {
  const message = JSON.parse(data);
  if (message.method === "Page.javascriptDialogOpening") {
    void send("Page.handleJavaScriptDialog", { accept: true, promptText: dialogAnswer });
  }
});
const api = (path, options = {}) => evalJs(`fetch(${JSON.stringify(path)}, {
  headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
  ${options.method ? `method: ${JSON.stringify(options.method)},` : ""}
  ${options.body ? `body: JSON.stringify(${JSON.stringify(options.body)}),` : ""}
}).then(async r => { const j = await r.json().catch(() => ({})); return {status: r.status, body: j}; })`);

async function openPath(path) {
  const parts = path.split("/");
  for (let i = 1; i <= parts.length; i++) {
    const current = parts.slice(0, i).join("/");
    const result = await evalJs(`
      (() => {
        const row = [...document.querySelectorAll('[data-path]')].find(d => d.getAttribute('data-path') === ${JSON.stringify(current)});
        if (!row) return 'not-found';
        const next = ${JSON.stringify(parts.slice(0, i + 1).join("/"))};
        const nextRow = [...document.querySelectorAll('[data-path]')].find(d => d.getAttribute('data-path') === next);
        if (${i === parts.length}) { row.click(); return 'clicked'; }
        if (nextRow) return 'expanded';
        row.click();
        return 'expanded';
      })()
    `);
    if (result === "not-found") {
      const debug = await evalJs(`({
        title: document.body.innerText.match(/文件 · .+/)?.[0] ?? '',
        folders: [...document.querySelectorAll('[data-dir="true"]')].map(d => d.getAttribute('data-path')).slice(0, 80)
      })`);
      throw new Error("文件树节点不存在: " + current + " | " + JSON.stringify(debug));
    }
    await sleep(500);
  }
}

async function putFile(path, content) {
  const result = await api("/api/v1/cases/1/file", { method: "PUT", body: { path, content } });
  if (result.status !== 200) throw new Error("写测试文件失败: " + JSON.stringify(result));
  await evalJs(`document.querySelector('[data-case-name="cavity-demo"]').click()`);
  await sleep(500);
}
async function deleteFile(path) {
  await api(`/api/v1/cases/1/file?path=${encodeURIComponent(path)}`, { method: "DELETE" });
}

try {
  await send("Page.enable");
  await send("Page.navigate", { url: "http://localhost:5173/" });
  await sleep(3000);
  if (await evalJs(`!!document.querySelector('form')`)) {
    await evalJs(`
      (() => {
        const inputs = document.querySelectorAll('input');
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(inputs[0], 'dev@vibeflow.cn'); inputs[0].dispatchEvent(new Event('input', {bubbles: true}));
        setter.call(inputs[1], 'devpass123'); inputs[1].dispatchEvent(new Event('input', {bubbles: true}));
        document.querySelector('button[type="submit"]')?.click() || document.querySelector('form button').click();
      })()
    `);
    await sleep(1800);
  }
  await evalJs(`document.querySelector('[data-case-name="cavity-demo"]').click()`);
  await sleep(2500);

  // .foam 路由
  const before = await api("/api/v1/cases/1/viz:sessions");
  const beforeIds = new Set(before.body.map((s) => s.id));
  await openPath("C-run/case-1/case-1.foam");
  await sleep(4000);
  const after = await api("/api/v1/cases/1/viz:sessions");
  const created = after.body.find((s) => !beforeIds.has(s.id));
  const frameSrc = await evalJs(`document.querySelector('iframe[title="可视化"]')?.src ?? ''`);
  if (!created || created.run_dir !== "C-run/case-1" || !frameSrc.includes(`/viz-proxy/${created.id}/`)) {
    throw new Error(".foam 未按 run_dir 打开可视化: " + JSON.stringify({ created, frameSrc }));
  }
  await api(`/api/v1/viz:sessions/${created.id}`, { method: "DELETE" });
  console.log(".foam run_dir viz route: OK", created.id);

  // 外部修改冲突
  const conflict = "D-post/wp02-conflict.txt";
  await putFile(conflict, "BASE");
  await openPath(conflict);
  await evalJs(`window.monaco.editor.getModels().at(-1).setValue('EDITOR_DIRTY')`);
  await sleep(1200);
  await api("/api/v1/cases/1/file", { method: "PUT", body: { path: conflict, content: "EXTERNAL" } });
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '保存 (Ctrl+S)').click()`);
  await sleep(700);
  let content = (await api(`/api/v1/cases/1/file?path=${encodeURIComponent(conflict)}`)).body.content;
  if (content !== "EXTERNAL") throw new Error("冲突保存仍覆盖: " + content);
  if (!await evalJs(`!!document.querySelector('[data-testid="editor-save-error"]')`)) throw new Error("缺少草稿保留提示");
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent === '查看服务器版本').click()`);
  await sleep(500);
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent === '保留草稿，基于此版本继续编辑').click()`);
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '保存 (Ctrl+S)').click()`);
  await sleep(1000);
  content = (await api(`/api/v1/cases/1/file?path=${encodeURIComponent(conflict)}`)).body.content;
  if (content !== "EDITOR_DIRTY") throw new Error("查看服务器版本后条件保存失败: " + content);
  await deleteFile(conflict);
  console.log("external edit conflict preserves draft, then reviewed conditional save: OK");

  // 路径格式
  const pathFile = "D-post/wp02-path.txt";
  await putFile(pathFile, "PATH");
  await openPath(pathFile);
  await evalJs(`Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: async text => { window.__copied = text; }}})`);
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '复制相对路径').click()`);
  if (await evalJs(`window.__copied`) !== pathFile) throw new Error("默认 POSIX 路径错误");
  await evalJs(`
    (() => {
      const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === '设置');
      const r = tab.getBoundingClientRect();
      const x = r.x + r.width / 2, y = r.y + r.height / 2;
      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, {bubbles: true, clientX: x, clientY: y, button: 0}));
      }
      const sel = document.querySelector('select[title="路径文本格式"]');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
      setter.call(sel, 'windows'); sel.dispatchEvent(new Event('change', {bubbles: true}));
    })()
  `);
  await openPath(pathFile);
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '复制相对路径').click()`);
  if (await evalJs(`window.__copied`) !== 'D-post\\wp02-path.txt') throw new Error("Windows 路径格式错误");
  await evalJs(`
    (() => {
      const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === '设置');
      const r = tab.getBoundingClientRect();
      const x = r.x + r.width / 2, y = r.y + r.height / 2;
      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, {bubbles: true, clientX: x, clientY: y, button: 0}));
      }
      const sel = document.querySelector('select[title="路径文本格式"]');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
      setter.call(sel, 'linux'); sel.dispatchEvent(new Event('change', {bubbles: true}));
    })()
  `);
  await openPath(pathFile);
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '复制相对路径').click()`);
  if (await evalJs(`window.__copied`) !== pathFile) throw new Error("恢复 POSIX 路径失败");
  await deleteFile(pathFile);
  console.log("global path format windows/linux: OK");

  // files.changed 驱动文件树自动刷新（不手动 loadTree）
  const watchPath = "Ref/wp02-watch.txt";
  await api("/api/v1/cases/1/file", { method: "PUT", body: { path: watchPath, content: "WATCH" } });
  await sleep(4500);
  const watchCreated = await evalJs(`!!document.querySelector('[data-path="${watchPath}"]')`);
  if (!watchCreated) throw new Error("文件树未收到 created files.changed");
  await api(`/api/v1/cases/1/file?path=${encodeURIComponent(watchPath)}`, { method: "DELETE" });
  await sleep(4500);
  const watchDeleted = await evalJs(`!document.querySelector('[data-path="${watchPath}"]')`);
  if (!watchDeleted) throw new Error("文件树未收到 deleted files.changed");
  console.log("file tree files.changed refresh: OK");

  // 算例复制 prompt
  const copyName = `wp02-ui-copy-${Date.now()}`;
  dialogAnswer = copyName;
  await evalJs(`document.querySelector('button[title="复制算例"]').click()`);
  await sleep(3000);
  if (!await evalJs(`!!document.querySelector('[data-case-name="${copyName}"]')`)) {
    throw new Error("复制算例后未选中副本: " + copyName);
  }
  console.log("UI case copy with prompt: OK", copyName);
} catch (error) {
  console.error("WP02 E2E FAIL:", error.message);
  process.exitCode = 1;
} finally {
  ws.close();
  edge.kill();
}
