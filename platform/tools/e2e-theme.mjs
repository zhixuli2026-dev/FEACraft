// G08：全局 Light/Dark 设置、Monaco/终端/CAD 同步与重载持久化。
import { execFile } from "node:child_process";
import http from "node:http";
import { writeFileSync } from "node:fs";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const WEB_URL = process.env.VIBEFLOW_WEB_URL || "http://localhost:5173/";
const getJson = path => new Promise((resolve, reject) => {
  http.get({host:"127.0.0.1", port:PORT, path}, response => {
    let body = ""; response.on("data", chunk => body += chunk);
    response.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});

async function main() {
  const outPrefix = process.argv[2] || "theme";
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
    const waitFor = async (expression, message, timeout = 15000) => {
      for (let i = 0; i < Math.ceil(timeout / 150); i++) {
        if (await evalJs(expression)) return;
        await sleep(150);
      }
      throw new Error(message);
    };
    const activate = async name => evalJs(`(() => {
      const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === '${name}');
      if (!tab) return false;
      const r = tab.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
      for (const type of ['pointerdown','mousedown','pointerup','mouseup','click'])
        tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
          {bubbles:true, clientX:x, clientY:y, button:0}));
      return true;
    })()`);
    const setTheme = async value => evalJs(`(() => {
      const select = document.querySelector('[data-testid="app-theme"]');
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, '${value}');
      select.dispatchEvent(new Event('change', {bubbles:true}));
    })()`);

    await send("Page.enable");
    await send("Runtime.enable");
    await send("Log.enable");
    await send("Network.enable");
    ws.on("message", data => {
      const message = JSON.parse(data);
      if (message.method === "Runtime.exceptionThrown" ||
          (message.method === "Runtime.consoleAPICalled" && message.params.type === "error") ||
          (message.method === "Log.entryAdded" && message.params.entry.level === "error")) {
        console.log("BROWSER_ERROR", JSON.stringify(message.params).slice(0, 1200));
      }
      if (message.method === "Network.loadingFailed") {
        console.log("REQUEST_FAILED", message.params.errorText, message.params.requestId);
      }
    });
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
    await activate("设置");
    await waitFor(`!!document.querySelector('[data-testid="app-theme"]')`, "主题设置未出现");
    await setTheme("light");
    await waitFor(`document.documentElement.dataset.theme === 'light' && getComputedStyle(document.body).backgroundColor === 'rgb(244, 246, 250)'`, "全局浅色未生效");
    console.log("1. 设置与根浅色变量 [OK]");
    await evalJs(`document.querySelector('[data-testid="setup-save"]').click()`);
    await waitFor(`document.querySelector('[data-testid="stage-checks"]') && !document.querySelector('[role="alert"]')`, "Setup 原值保存/重新探测失败");
    console.log("1b. Setup 原值保存并重新探测 [OK]");

    await evalJs(`(() => {
      const select = document.querySelector('select');
      const option = [...select.options].find(o => o.textContent === 'Default');
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, option.value);
      select.dispatchEvent(new Event('change', {bubbles:true}));
    })()`);
    await waitFor(`!!document.querySelector('[data-case-name="cavity-demo"]')`, "cavity-demo 未加载");
    await evalJs(`document.querySelector('[data-case-name="cavity-demo"]').click()`);
    await sleep(1500);
    await waitFor(`!!document.querySelector('[data-path="D-post/notes.md"]')`, "测试文本文件未加载");
    const filePoint = await evalJs(`(() => {
      const el = document.querySelector('[data-path="D-post/notes.md"]');
      el.scrollIntoView({block:'center'});
      const r = el.getBoundingClientRect();
      return {x:r.x+r.width/2,y:r.y+r.height/2};
    })()`);
    await send("Input.dispatchMouseEvent", {type:"mousePressed", x:filePoint.x, y:filePoint.y, button:"left", buttons:1, clickCount:1});
    await send("Input.dispatchMouseEvent", {type:"mouseReleased", x:filePoint.x, y:filePoint.y, button:"left", buttons:0, clickCount:1});
    await sleep(2000);
    await activate("编辑器");
    await waitFor(`document.querySelector('.monaco-editor') && getComputedStyle(document.querySelector('.monaco-editor')).backgroundColor === 'rgb(255, 255, 254)'`, "Monaco 未切浅色", 30000);
    console.log("2. Monaco 浅色 [OK]");

    await activate("终端");
    await waitFor(`document.querySelector('[data-testid="terminal-light-sgr"]')?.textContent === '深色 SGR'`, "终端未同步浅色 SGR");
    await activate("几何预览");
    await waitFor(`document.querySelector('[data-testid="geo-canvas-host"]')?.dataset.theme === 'light'`, "CAD 未同步浅色");
    console.log("3. 终端与 CAD 主题同步 [OK]");

    await activate("编辑器");
    const shot = await send("Page.captureScreenshot", {format:"png", fromSurface:true});
    writeFileSync(`${outPrefix}-light-theme.png`, Buffer.from(shot.data, "base64"));
    await send("Page.navigate", {url:WEB_URL});
    await sleep(3500);
    await waitFor(`document.documentElement.dataset.theme === 'light' && localStorage.getItem('vf_theme') === 'light'`, "浅色未跨重载保持");
    await activate("设置");
    await waitFor(`document.querySelector('[data-testid="app-theme"]')?.value === 'light'`, "重载后设置值不是 light");
    console.log("4. 浅色跨重载持久化 [OK]");

    await setTheme("dark");
    await waitFor(`document.documentElement.dataset.theme === 'dark' && localStorage.getItem('vf_theme') === 'dark'`, "无法恢复 Dark");
    console.log("5. 恢复 Dark [OK]");
    console.log("GLOBAL THEME E2E DONE");
  } finally {
    if (ws) ws.close();
    edge.kill();
  }
}

main().catch(error => { console.error("GLOBAL THEME E2E FAIL:", error.message); process.exit(1); });
