// E2E 验证可视化面板：启动会话 → iframe 载 trame → 渲染出流场图
// 用法: node e2e-viz.mjs <outPrefix>
import { execFile } from "node:child_process";
import http from "node:http";
import { writeFileSync } from "node:fs";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function getJson(path) {
  return new Promise((resolve, reject) => {
    http.get({ host: "127.0.0.1", port: PORT, path }, (res) => {
      let b = ""; res.on("data", (c) => (b += c)); res.on("end", () => resolve(JSON.parse(b)));
    }).on("error", reject);
  });
}

async function main() {
  const outPrefix = process.argv[2] || "viz";
  const edge = execFile(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profileDir}`, "--window-size=1600,900", "about:blank"], () => {});
  await sleep(2500);
  const targets = await getJson("/json/list");
  const page = targets.find((t) => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => ws.on("open", r));
  let id = 0;
  const send = (m, p = {}) => new Promise((res, rej) => {
    const i = ++id;
    const h = (d) => { const x = JSON.parse(d); if (x.id === i) { ws.off("message", h);
      x.error ? rej(new Error(JSON.stringify(x.error))) : res(x.result); } };
    ws.on("message", h); ws.send(JSON.stringify({ id: i, method: m, params: p }));
  });
  const evalJs = async (e) => {
    const r = await send("Runtime.evaluate", { expression: e, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error("JS: " + JSON.stringify(r.exceptionDetails).slice(0, 300));
    return r.result.value;
  };
  const clickLikeUser = `(el) => {
    const r = el.getBoundingClientRect();
    const x = r.x + r.width/2, y = r.y + r.height/2;
    for (const type of ['pointerdown','mousedown','pointerup','mouseup','click'])
      el.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, {bubbles:true, clientX:x, clientY:y, button:0}));
  }`;

  await send("Page.enable");
  await send("Page.navigate", { url: "http://localhost:5173/" });
  await sleep(3500);
  const hasForm = await evalJs(`!!document.querySelector('form')`);
  if (hasForm) {
    await evalJs(`(() => {
      const inputs = document.querySelectorAll('input');
      const setVal = (el, v) => {
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(el, v); el.dispatchEvent(new Event('input', { bubbles: true }));
      };
      setVal(inputs[inputs.length - 2], 'dev@vibeflow.cn');
      setVal(inputs[inputs.length - 1], 'devpass123');
    })()`);
    await evalJs(`document.querySelector('form').requestSubmit()`);
    await sleep(3500);
  }

  // 选 cavity-demo
  await evalJs(`(() => {
    const rows = [...document.querySelectorAll('div')].filter(d => d.innerText?.includes('cavity-demo'));
    if (rows.length) rows[rows.length-1].click();
  })()`);
  await sleep(2000);

  // 切到可视化标签 → 启动会话
  const vizTab = await evalJs(`(() => {
    const clickLikeUser = ${clickLikeUser};
    const tabs = [...document.querySelectorAll('.dv-tab')].filter(t => t.textContent?.trim() === '可视化');
    if (tabs.length) { clickLikeUser(tabs[0]); return 'clicked'; }
    return 'no-viz-tab';
  })()`);
  console.log("1. 可视化标签:", vizTab);
  await sleep(1500);

  const started = await evalJs(`(() => {
    const btn = [...document.querySelectorAll('button')].find(b => b.innerText?.includes('启动可视化会话'));
    if (!btn) return 'no-start-btn';
    if (btn.disabled) return 'disabled';
    btn.click(); return 'clicked';
  })()`);
  console.log("2. 启动:", started);

  // 等 kernel 就绪 + iframe 挂载（模型/管线加载给足时间）
  let chip = "";
  for (let i = 0; i < 30; i++) {
    await sleep(2000);
    chip = await evalJs(`(() => {
      const chip = [...document.querySelectorAll('span')].find(s => s.textContent?.includes('C-run/case-1') && s.textContent?.includes('#'));
      const iframe = document.querySelector('iframe[title="可视化"]');
      return chip && iframe ? 'ready:' + chip.textContent.trim() : '';
    })()`);
    if (chip) break;
  }
  console.log("3. 会话挂载:", chip || "TIMEOUT");
  if (!chip) { console.log("VIZ E2E FAIL"); ws.close(); edge.kill(); process.exit(1); }

  // 再等 iframe 内 trame 客户端连接+首帧推送
  await sleep(15000);
  const shot = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
  const buf = Buffer.from(shot.data, "base64");
  writeFileSync(`${outPrefix}-viz.png`, buf);
  console.log("4. 截图字节:", buf.length, buf.length > 120000 ? "（含渲染内容）" : "（疑似白屏）");

  ws.close(); edge.kill();
  console.log("VIZ E2E DONE");
}

main().catch((e) => { console.error("E2E FAIL:", e.message); process.exit(1); });
