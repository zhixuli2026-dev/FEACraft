// G11：真实浏览器 error/unhandledrejection 上报与去重。
import { execFile } from "node:child_process";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;
const WEB_URL = process.env.VIBEFLOW_WEB_URL || "http://localhost:5173/";
const RUN_ID = Date.now();
const BROWSER_MARK = `CLIENT_BROWSER_MARK_${RUN_ID}`;
const REJECT_MARK = `CLIENT_REJECT_MARK_${RUN_ID}`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const getJson = path => new Promise((resolve, reject) => {
  http.get({host:"127.0.0.1", port:PORT, path}, response => {
    let body = ""; response.on("data", chunk => body += chunk);
    response.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});

async function main() {
  const edge = execFile(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profileDir}`, "--window-size=1400,800", "about:blank"], () => {});
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
      if (result.exceptionDetails) throw new Error("页面 JS 异常: " + JSON.stringify(result.exceptionDetails).slice(0, 400));
      return result.result.value;
    };
    const waitFor = async (expression, message) => {
      for (let i = 0; i < 100; i++) {
        if (await evalJs(expression)) return;
        await sleep(150);
      }
      const debug = await evalJs(`(async () => {
        const response = await fetch('/api/v1/runtime/client-errors?limit=20', {headers:{Authorization:'Bearer ' + localStorage.getItem('vf_token')}});
        return {status: response.status, body: await response.json().catch(() => ({})), href: location.href};
      })()`).catch(error => ({debugError: error.message}));
      console.error("CLIENT ERROR DEBUG:", JSON.stringify(debug).slice(0, 6000));
      throw new Error(message);
    };

    await send("Page.enable");
    await send("Page.navigate", {url:WEB_URL});
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
    await evalJs(`(() => {
      const error = new Error(${JSON.stringify(BROWSER_MARK)});
      window.dispatchEvent(new ErrorEvent('error', {message:error.message, error}));
      window.dispatchEvent(new ErrorEvent('error', {message:error.message, error}));
      Promise.reject(new Error(${JSON.stringify(REJECT_MARK)}));
    })()`);
    await waitFor(`(async () => {
      const response = await fetch('/api/v1/runtime/client-errors?limit=20', {headers:{Authorization:'Bearer ' + localStorage.getItem('vf_token')}});
      const data = await response.json();
      const messages = data.items.map(item => item.message);
      return messages.filter(message => message === ${JSON.stringify(BROWSER_MARK)}).length === 1 && messages.includes(${JSON.stringify(REJECT_MARK)});
    })()`, "浏览器错误事件未按去重规则上报");
    console.log("CLIENT ERROR E2E DONE");
  } finally {
    if (ws) ws.close();
    edge.kill();
  }
}

main().catch(error => { console.error("CLIENT ERROR E2E FAIL:", error.message); process.exit(1); });
