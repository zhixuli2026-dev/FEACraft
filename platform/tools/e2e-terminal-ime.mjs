// G03：IME 标点 beforeinput/compositionend 去重、中文与多行快速输入。
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
  const outPrefix = process.argv[2] || "terminal-ime";
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
    const waitFor = async (expression, message) => {
      for (let i = 0; i < 150; i++) {
        if (await evalJs(expression)) return;
        await sleep(200);
      }
      throw new Error(message);
    };
    const enter = async () => {
      await send("Input.dispatchKeyEvent", {type:"keyDown", key:"Enter", code:"Enter", windowsVirtualKeyCode:13});
      await send("Input.dispatchKeyEvent", {type:"keyUp", key:"Enter", code:"Enter", windowsVirtualKeyCode:13});
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
      window.__initialTerminalIds = (await response.json()).sessions.map(s => s.id);
    })()`);
    await evalJs(`(() => {
      const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === '终端');
      const r = tab.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
      for (const type of ['pointerdown','mousedown','pointerup','mouseup','click'])
        tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
          {bubbles:true, clientX:x, clientY:y, button:0}));
    })()`);
    await waitFor(`document.querySelector('.xterm-helper-textarea')?.dataset.vfImePatch === '1'`, "IME patch 未安装");
    await sleep(1200);
    await evalJs(`document.querySelector('.xterm-helper-textarea').focus()`);
    await send("Input.insertText", {text:"echo IME_PUNCT"});
    await evalJs(`(() => {
      const textarea = document.querySelector('.xterm-helper-textarea');
      textarea.dispatchEvent(new InputEvent('beforeinput', {data:'，', bubbles:true, cancelable:true}));
      textarea.dispatchEvent(new CompositionEvent('compositionend', {data:'，', bubbles:true, cancelable:true}));
    })()`);
    await enter();
    await waitFor(`document.querySelector('.terminal')?.textContent.includes('IME_PUNCT，')`, "IME 标点未进入 PTY");
    const punctuation = await evalJs(`document.querySelector('.terminal').textContent.includes('IME_PUNCT，，')`);
    if (punctuation) throw new Error("同一标点在 PTY 中重复发送");
    console.log("1. beforeinput+compositionend 同标点只发送一次 [OK]");

    await send("Input.insertText", {text:"echo 中文_OK"});
    await enter();
    await waitFor(`document.querySelector('.terminal')?.textContent.includes('中文_OK')`, "普通中文输入被拦截");
    console.log("2. 普通中文输入不受影响 [OK]");

    await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('快速输入')).click()`);
    await evalJs(`(() => {
      const input = document.querySelector('textarea[placeholder*="多行命令"]');
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, 'echo MULTI_1\\necho MULTI_2');
      input.dispatchEvent(new Event('input', {bubbles:true}));
    })()`);
    await sleep(200);
    await evalJs(`document.querySelector('textarea[placeholder*="多行命令"]').dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', ctrlKey:true, bubbles:true}))`);
    await waitFor(`document.querySelector('.terminal')?.textContent.includes('MULTI_2')`, "多行快速输入未执行");
    await waitFor(`document.querySelector('.terminal')?.textContent.includes('MULTI_2')`, "多行快速输入未执行");
    await waitFor(`document.querySelector('.vf-output-active') === null`, "多行输出未收尾");
    await sleep(500);
    const wheelCenter = await evalJs(`(() => { const r=document.querySelector('.terminal').getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()`);
    await send("Input.dispatchMouseEvent", {type:"mouseWheel", x:wheelCenter.x, y:wheelCenter.y, deltaX:0, deltaY:-1000});
    await waitFor(`document.querySelector('.xterm-rows')?.textContent.includes('MULTI_1')`, "滚动历史中缺少第一条多行命令结果");
    const shot = await send("Page.captureScreenshot", {format:"png", fromSurface:true});
    writeFileSync(`${outPrefix}-ime.png`, Buffer.from(shot.data, "base64"));
    console.log("3. 多行快速输入 [OK]");
    console.log("TERMINAL IME E2E DONE");
  } finally {
    if (ws) {
      try {
        const mid = 999999;
        const result = new Promise(resolve => {
          const onMessage = data => {
            const message = JSON.parse(data);
            if (message.id === mid) { ws.off("message", onMessage); resolve(message.result); }
          };
          ws.on("message", onMessage);
          ws.send(JSON.stringify({id:mid, method:"Runtime.evaluate", params:{expression:`(async () => {
            const response = await fetch('/api/v1/terminal/sessions', {headers:{Authorization:'Bearer ' + localStorage.getItem('vf_token')}});
            const data = await response.json();
            for (const session of data.sessions.filter(s => !window.__initialTerminalIds.includes(s.id)))
              await fetch('/api/v1/terminal/sessions/' + session.id, {method:'DELETE', headers:{Authorization:'Bearer ' + localStorage.getItem('vf_token')}});
          })()`, awaitPromise:true}}));
        });
        await result;
      } catch {}
      ws.close();
    }
    edge.kill();
  }
}

main().catch(error => { console.error("TERMINAL IME E2E FAIL:", error.message); process.exit(1); });
