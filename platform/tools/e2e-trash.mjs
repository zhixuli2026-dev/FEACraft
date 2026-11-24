// A23：case/workspace 删除与回收站恢复的真实 UI 验证。
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
  const outPrefix = process.argv[2] || "trash";
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
      const suffix = Date.now();
      const wsName = 'trash-ui-' + suffix;
      const workspace = await (await fetch('/api/v1/workspaces', {
        method:'POST', headers, body:JSON.stringify({name:wsName})
      })).json();
      const cases = [];
      for (let i = 0; i < 2; i++) {
        const name = 'trash-ui-case-' + i + '-' + suffix;
        const item = await (await fetch('/api/v1/workspaces/' + workspace.id + '/cases', {
          method:'POST', headers, body:JSON.stringify({name})
        })).json();
        await fetch('/api/v1/cases/' + item.id + '/file', {
          method:'PUT', headers, body:JSON.stringify({path:'Ref/marker.txt', content:'TRASH_UI_' + i})
        });
        cases.push({id:item.id, name});
      }
      return {workspace:{id:workspace.id, name:wsName}, cases};
    })()`);
    console.log("1. 测试工作区与算例:", fixture);

    await send("Page.navigate", {url:"http://localhost:5173/"});
    await sleep(3500);
    await evalJs(`(() => {
      window.confirm = () => true;
      const select = document.querySelector('select');
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, '${fixture.workspace.id}');
      select.dispatchEvent(new Event('change', {bubbles:true}));
    })()`);
    await waitFor(`!!document.querySelector('[data-case-name="${fixture.cases[0].name}"]')`, "测试 case 未加载");
    await evalJs(`document.querySelector('[data-case-name="${fixture.cases[0].name}"]').click()`);
    await waitFor(`!!document.querySelector('[data-testid="case-delete"]')`, "case 删除按钮未出现");
    await evalJs(`(() => {
      const btn = document.querySelector('[data-testid="case-delete"]');
      btn.scrollIntoView({block:'center'}); btn.click();
    })()`);
    await waitFor(`!document.querySelector('[data-case-name="${fixture.cases[0].name}"]')`, "删除后 case 仍在列表");
    await evalJs(`document.querySelector('[data-testid="trash-toggle"]').click()`);
    await waitFor(`!!document.querySelector('[data-trash-name="${fixture.cases[0].name}"]')`, "回收站未显示 case");
    console.log("2. case 删除并出现在回收站 [OK]");

    await evalJs(`document.querySelector('[data-testid="case-restore-${fixture.cases[0].id}"]').click()`);
    await waitFor(`!!document.querySelector('[data-case-name="${fixture.cases[0].name}"]')`, "case 恢复后未回到列表");
    console.log("3. case 从回收站恢复 [OK]");

    await evalJs(`document.querySelector('[data-testid="workspace-delete"]').click()`);
    await waitFor(`!!document.querySelector('[data-trash-name="${fixture.workspace.name}"]')`, "回收站未显示工作区");
    await evalJs(`document.querySelector('[data-testid="trash-panel"]').scrollIntoView({block:'center'})`);
    await sleep(150);
    const trashShot = await send("Page.captureScreenshot", {format:"png", fromSurface:true});
    writeFileSync(`${outPrefix}-trash.png`, Buffer.from(trashShot.data, "base64"));
    console.log("4. workspace 删除并出现在回收站 [OK]");

    await evalJs(`document.querySelector('[data-testid="workspace-restore-${fixture.workspace.id}"]').click()`);
    await waitFor(`(() => {
      const select = document.querySelector('select');
      return select && [...select.options].some(o => o.value === '${fixture.workspace.id}');
    })()`, "workspace 恢复后未回到选择器");
    await waitFor(`!!document.querySelector('[data-case-name="${fixture.cases[0].name}"]') && !!document.querySelector('[data-case-name="${fixture.cases[1].name}"]')`, "workspace 恢复后 case 不完整");
    const finalTrash = await evalJs(`(async () => {
      const response = await fetch('/api/v1/trash', {headers:{Authorization:'Bearer ' + localStorage.getItem('vf_token')}});
      return response.json();
    })()`);
    assert(!finalTrash.cases.some(item => fixture.cases.some(c => c.id === item.id)), "测试 case 仍留在回收站");
    assert(!finalTrash.workspaces.some(item => item.id === fixture.workspace.id), "测试工作区仍留在回收站");
    const restoredShot = await send("Page.captureScreenshot", {format:"png", fromSurface:true});
    writeFileSync(`${outPrefix}-restored.png`, Buffer.from(restoredShot.data, "base64"));
    console.log("5. workspace 与两个 case 全部恢复，测试回收站项清零 [OK]");
    console.log("TRASH E2E DONE");
  } finally {
    if (ws) ws.close();
    edge.kill();
  }
}

main().catch(error => { console.error("TRASH E2E FAIL:", error.message); process.exit(1); });
