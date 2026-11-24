// G12：点击启动的语音输入、转写、停止与 API 不可用提示（假识别器）。
import { execFile } from "node:child_process";
import http from "node:http";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;
const WEB_URL = process.env.VIBEFLOW_WEB_URL || "http://localhost:5173/";
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const getJson = path => new Promise((resolve, reject) => {
  http.get({host:"127.0.0.1", port:PORT, path}, response => {
    let body = ""; response.on("data", chunk => body += chunk);
    response.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});

async function main() {
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
      for (let i = 0; i < 100; i++) {
        if (await evalJs(expression)) return;
        await sleep(150);
      }
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
      const select = document.querySelector('select');
      const option = [...select.options].find(o => o.textContent === 'Default');
      if (option) {
        Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, option.value);
        select.dispatchEvent(new Event('change', {bubbles:true}));
      }
    })()`);
    await waitFor(`!!document.querySelector('[data-case-name="cavity-demo"]')`, "cavity-demo 未加载");
    await evalJs(`document.querySelector('[data-case-name="cavity-demo"]').click()`);
    await sleep(1000);
    await evalJs(`(() => {
      const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === 'Agent');
      const r = tab.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
      for (const type of ['pointerdown','mousedown','pointerup','mouseup','click'])
        tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
          {bubbles:true, clientX:x, clientY:y, button:0}));
    })()`);
    console.log("Agent 文本:", await evalJs(`document.body.innerText.slice(-700)`));
    await waitFor(`[...document.querySelectorAll('button')].some(b => b.textContent === '新会话' || b.textContent === '语音输入')`, "Agent 未就绪");
    if (await evalJs(`[...document.querySelectorAll('button')].some(b => b.textContent === '新会话')`)) {
      await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent === '新会话').click()`);
      await sleep(500);
    }
    await evalJs(`(() => {
      window.__voiceStarts = 0; window.__voiceStops = 0;
      window.SpeechRecognition = class {
        start() { window.__voiceStarts++; window.__voice = this; }
        stop() { window.__voiceStops++; this.onend?.(); }
      };
    })()`);
    if (await evalJs(`window.__voiceStarts`) !== 0) throw new Error("点击前已启动语音识别");
    await waitFor(`document.querySelector('[data-testid="agent-voice"]') && !document.querySelector('[data-testid="agent-voice"]').disabled`, "语音按钮未启用");
    const voicePoint = await evalJs(`(() => {
      const el = document.querySelector('[data-testid="agent-voice"]');
      el.scrollIntoView({block:'center'});
      const r = el.getBoundingClientRect();
      return {x:r.x+r.width/2,y:r.y+r.height/2};
    })()`);
    await send("Input.dispatchMouseEvent", {type:"mousePressed", x:voicePoint.x, y:voicePoint.y, button:"left", buttons:1, clickCount:1});
    await send("Input.dispatchMouseEvent", {type:"mouseReleased", x:voicePoint.x, y:voicePoint.y, button:"left", buttons:0, clickCount:1});
    await waitFor(`window.__voiceStarts === 1`, "点击后未启动识别");
    await evalJs(`window.__voice.onresult({results:[[{transcript:'VOICE_TEST_TRANSCRIPT'}]]})`);
    await waitFor(`document.querySelector('[data-testid="agent-input"]')?.value.includes('VOICE_TEST_TRANSCRIPT')`, "转写未进入 Agent 输入框");
    await evalJs(`document.querySelector('[data-testid="agent-voice"]').click()`);
    await waitFor(`window.__voiceStops === 1`, "再次点击未停止识别");
    console.log("1. 点击启动/转写/停止且点击前零启动 [OK]");

    await evalJs(`(() => {
      window.SpeechRecognition = undefined; window.webkitSpeechRecognition = undefined;
      document.querySelector('[data-testid="agent-voice"]').click();
    })()`);
    await waitFor(`document.querySelector('[data-testid="agent-voice"]')?.textContent.includes('不支持语音识别')`, "API 不可用提示缺失");
    console.log("2. API 不可用提示 [OK]");
    console.log("VOICE INPUT E2E DONE");
  } finally {
    if (ws) ws.close();
    edge.kill();
  }
}

main().catch(error => { console.error("VOICE INPUT E2E FAIL:", error.message); process.exit(1); });
