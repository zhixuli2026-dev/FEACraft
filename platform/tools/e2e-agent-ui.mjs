// E2E 验证 Agent 新 UI：阶段按钮 / $技能弹窗 / 权限模式 / diff 提案卡
// 用法: node e2e-agent-ui.mjs <outPrefix>
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
  const outPrefix = process.argv[2] || "agentui";
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
  await evalJs(`(() => {
    const rows = [...document.querySelectorAll('div')].filter(d => d.innerText?.includes('cavity-demo'));
    if (rows.length) rows[rows.length-1].click();
  })()`);
  await sleep(2000);
  await evalJs(`(() => {
    const clickLikeUser = ${clickLikeUser};
    const tabs = [...document.querySelectorAll('.dv-tab')].filter(t => t.textContent?.trim() === 'Agent');
    if (tabs.length) clickLikeUser(tabs[0]);
  })()`);
  await sleep(1200);

  // 1. 阶段按钮（6 个）
  const stages = await evalJs(`(() => {
    const btns = [...document.querySelectorAll('button')].filter(b => /^[A-E几何]|[BCDEV]/.test(b.innerText) && b.innerText.length <= 4);
    return [...new Set(btns.map(b => b.innerText))].join(',');
  })()`);
  console.log("1. 阶段按钮:", stages);

  // 2. 权限下拉存在
  const perm = await evalJs(`(() => {
    const sels = [...document.querySelectorAll('select')];
    return sels.some(s => [...s.options].some(o => o.value === 'full_access')) ? 'perm-select-ok' : 'missing';
  })()`);
  console.log("2. 权限下拉:", perm);

  // 3. 新会话（mock profile）→ $viz 弹窗
  await evalJs(`(() => {
    const bar = document.querySelector('textarea')?.closest('div')?.parentElement;
    const sel = bar?.querySelector('select');
    if (sel) {
      const opts = [...sel.options];
      const mi = opts.findIndex(o => o.textContent?.includes('fork-mock'));
      if (mi >= 0) {
        const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
        setter.call(sel, sel.options[mi].value);
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
  })()`);
  await sleep(400);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll('button')].find(b => b.innerText === '新会话');
    if (b) b.click();
  })()`);
  await sleep(1200);
  const skillPop = await evalJs(`(async () => {
    const ta = document.querySelector('textarea');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(ta, '帮我用 $viz');
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    await new Promise(r => setTimeout(r, 700));
    const pop = [...document.querySelectorAll('div')].find(d => d.innerText?.includes('$vibeflow-viz'));
    return pop ? 'popup-ok' : 'no-popup';
  })()`);
  console.log("3. $技能弹窗:", skillPop);

  // 4. 点弹窗补全 → 清空后发「写一下」→ diff 提案卡
  await evalJs(`(() => {
    const item = [...document.querySelectorAll('div')].find(d => d.innerText?.startsWith('$vibeflow-viz'));
    if (item) item.click();
  })()`);
  await sleep(400);
  await evalJs(`(() => {
    const ta = document.querySelector('textarea');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(ta, '写一下');
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  })()`);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll('button')].find(b => b.innerText === '发送');
    if (b) b.click();
  })()`);
  await sleep(4000);
  const diffCard = await evalJs(`(() => {
    const txt = document.body.innerText;
    const hasApproval = txt.includes('请求执行危险操作');
    const hasDiff = txt.includes('Case Memory') && txt.includes('+');
    const hasStats = /\\+\\d+ −\\d+/.test(txt);
    return JSON.stringify({ hasApproval, hasDiff, hasStats });
  })()`);
  console.log("4. diff 提案卡:", diffCard);

  // 5. 批准 → 文件写入验证
  await evalJs(`(() => {
    const b = [...document.querySelectorAll('button')].find(b => b.innerText === '批准执行');
    if (b) b.click();
  })()`);
  await sleep(4000);
  const applied = await evalJs(`fetch('/api/v1/cases/1/file?path=' + encodeURIComponent('0-caseDict/memory.md'), {
    headers: { Authorization: 'Bearer ' + localStorage.getItem('vf_token') }
  }).then(r => r.json()).then(j => j.content?.includes('MOCK 提案写入') ? 'file-written' : 'not-written:' + (j.content||'').slice(0,50))`);
  console.log("5. 提案落盘:", applied);

  const shot = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
  writeFileSync(`${outPrefix}-agent.png`, Buffer.from(shot.data, "base64"));
  console.log("screenshot:", Buffer.from(shot.data, "base64").length, "bytes");
  ws.close(); edge.kill();
  console.log("AGENT UI E2E DONE");
}
main().catch((e) => { console.error("E2E FAIL:", e.message); process.exit(1); });
