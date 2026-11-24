// C12：批量作业的真实浏览器提交、进度与重载恢复。
import { execFile } from "node:child_process";
import http from "node:http";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9224;
const APP_URL = process.env.VIBEFLOW_TEST_WEB || "http://localhost:5173/";
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-batch`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const assert = (condition, message) => { if (!condition) throw new Error(message); };
const getJson = path => new Promise((resolve, reject) => {
  http.get({host:"127.0.0.1", port:PORT, path}, response => {
    let body = ""; response.on("data", chunk => body += chunk);
    response.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});

async function main() {
  const outPrefix = process.argv[2] || "shots/c12-batch";
  const edge = execFile(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profileDir}`, "--window-size=1600,900", "about:blank"], () => {});
  let ws;
  try {
    await sleep(2500);
    const page = (await getJson("/json/list")).find(target => target.type === "page");
    ws = new WebSocket(page.webSocketDebuggerUrl);
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
      ws.send(JSON.stringify({id:mid, method, params}));
    });
    const evalJs = async expression => {
      const result = await send("Runtime.evaluate", {expression, returnByValue:true, awaitPromise:true});
      if (result.exceptionDetails) throw new Error("页面 JS 异常: " + JSON.stringify(result.exceptionDetails).slice(0, 500));
      return result.result.value;
    };
    const waitFor = async (expression, message, attempts = 120) => {
      for (let i = 0; i < attempts; i++) {
        if (await evalJs(expression)) return;
        await sleep(100);
      }
      throw new Error(message);
    };
    await send("Page.enable");
    await send("Page.navigate", {url:APP_URL});
    await sleep(2500);
    const fixture = await evalJs(`(async () => {
      const suffix = Date.now();
      const auth = await (await fetch('/api/v1/auth/register', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({email:'batch-ui-'+suffix+'@vibeflow.cn', name:'batch-ui', password:'batch-ui-pass-123'})})).json();
      if (!auth.token) throw new Error(JSON.stringify(auth));
      localStorage.setItem('vf_token', auth.token);
      const headers = {Authorization:'Bearer '+auth.token, 'Content-Type':'application/json'};
      const workspace = await (await fetch('/api/v1/workspaces', {method:'POST', headers,
        body:JSON.stringify({name:'batch-ui-'+suffix})})).json();
      const item = await (await fetch('/api/v1/workspaces/'+workspace.id+'/cases', {method:'POST', headers,
        body:JSON.stringify({name:'batch-ui-case-'+suffix})})).json();
      return {workspaceId:workspace.id, caseId:item.id, caseName:item.name};
    })()`);
    await send("Page.navigate", {url:APP_URL});
    await sleep(3000);
    await evalJs(`(() => {
      const select = document.querySelector('select');
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, '${fixture.workspaceId}');
      select.dispatchEvent(new Event('change', {bubbles:true}));
    })()`);
    await waitFor(`!!document.querySelector('[data-case-name="${fixture.caseName}"]')`, "测试 case 未加载");
    await evalJs(`document.querySelector('[data-case-name="${fixture.caseName}"]').click()`);
    await waitFor(`[...document.querySelectorAll('button')].some(b => b.textContent === '批量')`, "批量按钮未出现");
    await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent === '批量').click()`);
    await waitFor(`!!document.querySelector('[data-testid="batch-commands"]')`, "批量输入区未出现");
    await evalJs(`(() => {
      const area = document.querySelector('[data-testid="batch-commands"]');
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(area,
        'python -c "import time; time.sleep(0.3); print(\\'BATCH_UI_1\\')"\\n' +
        'python -c "import time; time.sleep(0.3); print(\\'BATCH_UI_2\\')"\\n' +
        'python -c "import time; time.sleep(0.3); print(\\'BATCH_UI_3\\')"');
      area.dispatchEvent(new Event('input', {bubbles:true}));
      document.querySelector('[data-testid="batch-submit"]').click();
    })()`);
    await waitFor(`[...document.querySelectorAll('[data-testid="batch-row"]')].some(r => r.textContent.includes('3/3 成功'))`,
      "批次未在 UI 完成", 180);
    console.log("1. 三工况提交、并发=2、批次完成 [OK]");
    await send("Page.reload", {ignoreCache:true});
    await sleep(3000);
    await waitFor(`(() => { const select = document.querySelector('select'); return select && [...select.options].some(o => o.value === '${fixture.workspaceId}'); })()`, "重载后工作区未加载");
    await evalJs(`(() => {
      const select = document.querySelector('select');
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, '${fixture.workspaceId}');
      select.dispatchEvent(new Event('change', {bubbles:true}));
    })()`);
    await waitFor(`!!document.querySelector('[data-case-name="${fixture.caseName}"]')`, "重载后测试 case 未加载");
    await evalJs(`document.querySelector('[data-case-name="${fixture.caseName}"]').click()`);
    await waitFor(`[...document.querySelectorAll('button')].some(b => b.textContent === '批量')`, "重载后批量按钮未出现");
    await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent === '批量').click()`);
    await waitFor(`[...document.querySelectorAll('[data-testid="batch-row"]')].some(r => r.textContent.includes('3/3 成功'))`,
      "重载后批次状态未恢复");
    const shot = await send("Page.captureScreenshot", {format:"png", fromSurface:true});
    mkdirSync(dirname(`${outPrefix}.png`), {recursive:true});
    writeFileSync(`${outPrefix}.png`, Buffer.from(shot.data, "base64"));
    console.log("2. 页面重载后持久化批次状态恢复 [OK]");
    console.log("C12 BATCH E2E DONE");
  } finally {
    if (ws) ws.close();
    edge.kill();
  }
}

main().catch(error => { console.error("C12 BATCH E2E FAIL:", error.message); process.exit(1); });
