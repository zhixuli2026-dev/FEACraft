// G04：真实 PTY 大输出、队列、滚动跟随、稳定光标与浅色 SGR。
import { execFile } from "node:child_process";
import http from "node:http";
import { writeFileSync } from "node:fs";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const getJson = path => new Promise((resolve, reject) => {
  http.get({host:"127.0.0.1", port:PORT, path}, response => {
    let body = ""; response.on("data", chunk => body += chunk);
    response.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});

async function main() {
  const outPrefix = process.argv[2] || "terminal-output";
  const edge = execFile(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profileDir}`, "--window-size=1500,900", "about:blank"], () => {});
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
    const waitFor = async (expression, message, timeout = 30000) => {
      const start = Date.now();
      while (Date.now() - start < timeout) {
        if (await evalJs(expression)) return;
        await sleep(200);
      }
      throw new Error(message || expression);
    };
    const quickSend = async command => {
      await evalJs(`(() => {
        const input = document.querySelector('textarea[placeholder*="多行命令"]');
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, ${JSON.stringify(command)});
        input.dispatchEvent(new Event('input', {bubbles:true}));
        input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', ctrlKey:true, bubbles:true}));
      })()`);
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
    await evalJs(`(async () => {
      const response = await fetch('/api/v1/terminal/sessions', {headers:{Authorization:'Bearer ' + localStorage.getItem('vf_token')}});
      const data = await response.json();
      window.__initialTerminalIds = data.sessions.map(s => s.id);
    })()`);
    await evalJs(`(() => {
      const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === '终端');
      const r = tab.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
      for (const type of ['pointerdown','mousedown','pointerup','mouseup','click'])
        tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
          {bubbles:true, clientX:x, clientY:y, button:0}));
    })()`);
    await waitFor(`[...document.querySelectorAll('button')].some(b => b.textContent.includes('快速输入'))`, "终端未就绪");
    await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('快速输入')).click()`);
    await waitFor(`!!document.querySelector('textarea[placeholder*="多行命令"]')`, "快速输入未打开");
    await sleep(1000);

    await quickSend(`python -c "import sys;sys.stdout.write('A'*3000000);print('\\nG04_FINAL')"`);
    await waitFor(`document.querySelector('.vf-output-active') !== null`, "大输出未进入稳定输出状态", 15000);
    console.log("1. 大输出期间 vf-output-active（稳定光标）[OK]");
    await waitFor(`document.querySelector('.terminal')?.textContent.includes('G04_FINAL')`, "最终 marker 未保留", 90000);
    await waitFor(`document.querySelector('.vf-output-active') === null`, "输出结束后未恢复交互状态");
    const bottom = await evalJs(`(() => {
      const v = document.querySelector('.xterm-viewport');
      return Math.abs(v.scrollHeight - v.clientHeight - v.scrollTop) < 8;
    })()`);
    if (!bottom) throw new Error("输出后未跟随到底部");
    console.log("2. 3MiB 输出最终 marker 与底部跟随 [OK]");

    await evalJs(`document.querySelector('.xterm-viewport').scrollTop = 0`);
    await quickSend("echo G04_TOP");
    await sleep(600);
    const top = await evalJs(`document.querySelector('.xterm-viewport').scrollTop`);
    if (top > 2) throw new Error("用户置顶后输出抢走滚动位置: " + top);
    await evalJs(`document.querySelector('.xterm-viewport').scrollTop = document.querySelector('.xterm-viewport').scrollHeight`);
    await sleep(250);
    await waitFor(`document.querySelector('.terminal')?.textContent.includes('G04_TOP')`, "回到底部后顶部测试输出不可见");
    await quickSend("echo G04_BOTTOM");
    await waitFor(`document.querySelector('.terminal')?.textContent.includes('G04_BOTTOM')`, "底部测试输出未出现");
    console.log("3. 手动置顶不被输出抢滚动 [OK]");

    await evalJs(`document.querySelector('[data-testid="terminal-light-sgr"]').click()`);
    await waitFor(`document.querySelector('[data-testid="terminal-light-sgr"]')?.style.background === 'rgb(241, 245, 249)'`, "浅色 SGR 未切换");
    await quickSend(`python -c "print('\\x1b[40mG04_LIGHT\\x1b[0m')"`);
    await waitFor(`document.querySelector('.terminal')?.textContent.includes('G04_LIGHT')`, "浅色 SGR 输出未出现");
    const shot = await send("Page.captureScreenshot", {format:"png", fromSurface:true});
    writeFileSync(`${outPrefix}-light-sgr.png`, Buffer.from(shot.data, "base64"));
    console.log("4. 浅色 SGR 模式与输出 [OK]");
    console.log("TERMINAL OUTPUT E2E DONE");
  } finally {
    if (ws) {
      try {
        const send = (method, params = {}) => new Promise((resolve, reject) => {
          const mid = 999999 + Math.floor(Math.random() * 1000);
          const onMessage = data => {
            const message = JSON.parse(data);
            if (message.id === mid) { ws.off("message", onMessage); resolve(message.result); }
          };
          ws.on("message", onMessage);
          ws.send(JSON.stringify({id:mid, method, params}));
        });
        await send("Runtime.evaluate", {expression:`(async () => {
          const response = await fetch('/api/v1/terminal/sessions', {headers:{Authorization:'Bearer ' + localStorage.getItem('vf_token')}});
          const data = await response.json();
          for (const session of data.sessions.filter(s => !window.__initialTerminalIds.includes(s.id)))
            await fetch('/api/v1/terminal/sessions/' + session.id, {method:'DELETE', headers:{Authorization:'Bearer ' + localStorage.getItem('vf_token')}});
        })()`, awaitPromise:true});
      } catch {}
      ws.close();
    }
    edge.kill();
  }
}

main().catch(error => { console.error("TERMINAL OUTPUT E2E FAIL:", error.message); process.exit(1); });
