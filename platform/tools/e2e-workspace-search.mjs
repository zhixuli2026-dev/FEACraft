// H07：工作区跨 case 搜索与命中后切换正确算例。
import { execFile } from "node:child_process";
import http from "node:http";
import { writeFileSync } from "node:fs";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const assert = (condition, message) => { if (!condition) throw new Error(message); };
const getJson = path => new Promise((resolve, reject) => {
  http.get({host:"127.0.0.1", port:PORT, path}, response => {
    let body = ""; response.on("data", chunk => body += chunk);
    response.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});

async function main() {
  const outPrefix = process.argv[2] || "workspace-search";
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
    const waitFor = async (expression, message) => {
      for (let i = 0; i < 100; i++) {
        if (await evalJs(expression)) return;
        await sleep(100);
      }
      throw new Error(message);
    };

    await send("Page.enable");
    await send("Page.navigate", {url:"http://localhost:5173/"});
    await sleep(3500);
    if (await evalJs(`!!document.querySelector('form')`)) {
      await evalJs(`(() => {
        const inputs = document.querySelectorAll('input');
        const setVal = (el, value) => {
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, value);
          el.dispatchEvent(new Event('input', {bubbles:true}));
        };
        setVal(inputs[inputs.length - 2], 'dev@vibeflow.cn');
        setVal(inputs[inputs.length - 1], 'devpass123');
        document.querySelector('form').requestSubmit();
      })()`);
      await sleep(3500);
    }

    const fixture = await evalJs(`(async () => {
      const headers = {Authorization:'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type':'application/json'};
      const marker = 'WORKSPACE_UI_' + Date.now();
      const cases = [];
      for (let i = 0; i < 2; i++) {
        const name = 'workspace-ui-' + i + '-' + Date.now();
        const created = await fetch('/api/v1/workspaces/1/cases', {
          method:'POST', headers, body:JSON.stringify({name})
        });
        const item = await created.json();
        await fetch('/api/v1/cases/' + item.id + '/file', {
          method:'PUT', headers, body:JSON.stringify({path:'Ref/shared-marker.txt', content:marker + ' case=' + i})
        });
        await fetch('/api/v1/cases/' + item.id + '/index:rebuild', {method:'POST', headers, body:'{}'});
        cases.push({id:item.id, name});
      }
      return {marker, cases};
    })()`);
    console.log("1. 两个同路径 case fixture:", fixture);

    await send("Page.navigate", {url:"http://localhost:5173/"});
    await sleep(3500);
    await evalJs(`document.querySelector('[data-case-name="${fixture.cases[0].name}"]').click()`);
    await waitFor(`!!document.querySelector('[data-testid="search-scope"]')`, "搜索范围控件未出现");
    await evalJs(`(() => {
      const scope = document.querySelector('[data-testid="search-scope"]');
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(scope, 'workspace');
      scope.dispatchEvent(new Event('change', {bubbles:true}));
      const input = document.querySelector('input[placeholder="搜索文件名/内容…"]');
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, '${fixture.marker}');
      input.dispatchEvent(new Event('input', {bubbles:true}));
    })()`);
    await sleep(150);
    await evalJs(`[...document.querySelectorAll('button')].find(b => b.innerText === '搜').click()`);
    await waitFor(`document.querySelectorAll('[data-testid="search-hit"]').length === 2`, "工作区搜索未返回两个 case");
    const hits = await evalJs(`[...document.querySelectorAll('[data-testid="search-hit"]')].map(hit => ({
      caseId:Number(hit.dataset.caseId), text:hit.innerText
    }))`);
    assert(new Set(hits.map(hit => hit.caseId)).size === 2, "命中未覆盖两个 case");
    assert(hits.every(hit => fixture.cases.some(item => item.id === hit.caseId && hit.text.includes(item.name))), "case 名称/归属错误");
    await evalJs(`document.querySelector('[data-testid="search-scope"]').scrollIntoView({block:'center'})`);
    await sleep(150);
    const resultsShot = await send("Page.captureScreenshot", {format:"png", fromSurface:true});
    writeFileSync(`${outPrefix}-workspace-search.png`, Buffer.from(resultsShot.data, "base64"));
    console.log("2. 工作区范围返回两个带归属的命中 [OK]");

    await evalJs(`document.querySelector('[data-testid="search-hit"][data-case-id="${fixture.cases[1].id}"]').click()`);
    await waitFor(`(() => {
      const selected = document.querySelector('[data-case-name="${fixture.cases[1].name}"]');
      return selected && selected.style.background && selected.style.background !== 'transparent';
    })()`, "点击后未切换第二个 case");
    await waitFor(`[...document.querySelectorAll('.monaco-editor .view-lines')].some(el => el.textContent.includes('${fixture.marker}') && el.textContent.includes('case=1'))`, "编辑器未显示第二个 case 的内容");
    const openShot = await send("Page.captureScreenshot", {format:"png", fromSurface:true});
    writeFileSync(`${outPrefix}-workspace-open.png`, Buffer.from(openShot.data, "base64"));
    console.log("3. 点击命中切换 case 并打开正确同路径文件 [OK]");
    console.log("WORKSPACE SEARCH E2E DONE");
  } finally {
    if (ws) ws.close();
    edge.kill();
  }
}

main().catch(error => { console.error("WORKSPACE SEARCH E2E FAIL:", error.message); process.exit(1); });
