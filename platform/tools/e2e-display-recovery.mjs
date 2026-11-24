import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9256;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-display-recovery`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const getJson = path => new Promise((resolve, reject) => {
  http.get({host: "127.0.0.1", port: PORT, path}, res => {
    let body = ""; res.on("data", chunk => body += chunk);
    res.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});
const edge = execFile(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profileDir}`, "--window-size=1400,850", "about:blank"], () => {});
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
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails).slice(0, 1000));
  return result.result.value;
};
const waitFor = async (expression, timeout = 15000) => {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    if (await evalJs(expression)) return;
    await sleep(100);
  }
  throw new Error("等待超时: " + expression);
};

try {
  await send("Page.enable");
  await send("Page.navigate", {url: "http://localhost:5173/"});
  await waitFor(`document.readyState === 'complete' && !!document.querySelector('#root')`);
  const mounted = await evalJs(`(async () => {
    const ReactModule = await import('/node_modules/.vite/deps/react.js');
    const React = ReactModule.default ?? ReactModule;
    const ReactDomModule = await import('/node_modules/.vite/deps/react-dom_client.js');
    const createRoot = ReactDomModule.createRoot ?? ReactDomModule.default?.createRoot;
    const {default: Boundary} = await import('/src/shell/ClientErrorBoundary.tsx');
    const host = document.createElement('div'); host.id = 'recovery-fixture';
    host.style.cssText = 'position:fixed;right:40px;top:40px;z-index:9999;width:480px;background:#0b1220;color:#d7dce5;border:1px solid #475569;border-radius:10px;padding:12px';
    document.body.appendChild(host);
    window.__messagePosts = 0;
    window.__originalRecoveryFetch = window.fetch;
    window.fetch = (...args) => {
      const url = String(args[0]); const method = String(args[1]?.method || 'GET').toUpperCase();
      if (method === 'POST' && /\\/conversations\\/\\d+\\/messages$/.test(url)) window.__messagePosts++;
      return window.__originalRecoveryFetch(...args);
    };
    window.__allowRecoveryRender = false;
    window.__recoveryErrors = 0;
    function Flaky() {
      if (!window.__allowRecoveryRender) { window.__recoveryErrors++; throw new Error('DISPLAY_RECOVERY_MARK'); }
      return React.createElement('div', {id: 'recovery-success', style: {padding: 24, fontSize: 18}},
        'DISPLAY_RECOVERED_WITHOUT_REPLAY');
    }
    const root = createRoot(host);
    window.__recoveryRoot = root;
    window.__renderRecoveryFixture = () => root.render(React.createElement(Boundary, null, React.createElement(Flaky)));
    window.__renderRecoveryFixture();
    return true;
  })()`);
  if (!mounted) throw new Error("真实 ErrorBoundary fixture 未挂载");
  await waitFor(`document.querySelector('#recovery-fixture [role="alert"]')?.dataset.recoveryAttempt === '1'`);
  await evalJs(`window.__allowRecoveryRender = true`);
  await waitFor(`document.querySelector('#recovery-success')?.textContent === 'DISPLAY_RECOVERED_WITHOUT_REPLAY' && document.querySelector('#recovery-fixture [data-display-generation]')?.dataset.displayGeneration === '1'`);
  let shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/f48-display-recovered.png", import.meta.url), Buffer.from(shot.data, "base64"));

  await evalJs(`window.__allowRecoveryRender = false; window.__renderRecoveryFixture()`);
  await waitFor(`document.querySelector('#recovery-fixture [role="alert"]')?.dataset.recoveryAttempt === '3' && document.querySelector('#recovery-fixture [role="alert"]')?.dataset.recovering === 'false' && document.querySelector('#recovery-fixture [role="alert"]')?.dataset.displayGeneration === '3'`);
  await sleep(900);
  const state = await evalJs(`({
    errors: window.__recoveryErrors,
    generation: document.querySelector('#recovery-fixture [role="alert"]')?.dataset.displayGeneration,
    attempt: document.querySelector('#recovery-fixture [role="alert"]')?.dataset.recoveryAttempt,
    recovering: document.querySelector('#recovery-fixture [role="alert"]')?.dataset.recovering,
    reload: [...document.querySelectorAll('#recovery-fixture button')].some(b => b.textContent === '重新加载'),
    messagePosts: window.__messagePosts,
  })`);
  if (state.errors < 4 || state.generation !== '3' || state.attempt !== '3' || state.recovering !== 'false' || !state.reload || state.messagePosts !== 0) {
    throw new Error("显示恢复次数/代次/无重放断言失败: " + JSON.stringify(state));
  }
  shot = await send("Page.captureScreenshot", {format: "png"});
  writeFileSync(new URL("../docs/shots/f48-display-fallback.png", import.meta.url), Buffer.from(shot.data, "base64"));
  console.log("Real ErrorBoundary transient recovery, 3-retry cap, generation and zero model replay: OK", state);
} catch (error) {
  const debug = await evalJs(`document.querySelector('#recovery-fixture')?.textContent || document.body.textContent.slice(-800)`).catch(() => "");
  console.error("DISPLAY RECOVERY E2E FAIL:", error.message, debug);
  process.exitCode = 1;
} finally {
  await evalJs(`(() => { window.__recoveryRoot?.unmount(); document.querySelector('#recovery-fixture')?.remove(); if (window.__originalRecoveryFetch) window.fetch = window.__originalRecoveryFetch; })()`).catch(() => {});
  ws.close(); edge.kill();
}
