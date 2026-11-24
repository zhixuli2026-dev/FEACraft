// E2E 验证本轮新增：作业残差曲线 + Agent 分叉/压缩按钮 + PDF 划词翻译
// 用法: node e2e-new-features.mjs <outPrefix>
import { execFile } from "node:child_process";
import http from "node:http";
import { writeFileSync } from "node:fs";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;
const SOLVE_JOB_ID = 32; // 黄金用例 icoFoam 作业（日志含 Ux/p 残差序列）

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function getJson(path) {
  return new Promise((resolve, reject) => {
    http.get({ host: "127.0.0.1", port: PORT, path }, (res) => {
      let b = ""; res.on("data", (c) => (b += c)); res.on("end", () => resolve(JSON.parse(b)));
    }).on("error", reject);
  });
}

async function main() {
  const outPrefix = process.argv[2] || "newfeat";
  const edge = execFile(EDGE, [
    "--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profileDir}`, "--window-size=1600,900", "about:blank",
  ], () => {});
  await sleep(2500);

  const targets = await getJson("/json/list");
  const page = targets.find((t) => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => ws.on("open", r));

  let id = 0;
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const mid = ++id;
      const onMsg = (data) => {
        const m = JSON.parse(data);
        if (m.id === mid) { ws.off("message", onMsg);
          m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result); }
      };
      ws.on("message", onMsg);
      ws.send(JSON.stringify({ id: mid, method, params }));
    });
  const evalJs = async (expr) => {
    const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error("页面内 JS 异常: " + JSON.stringify(r.exceptionDetails).slice(0, 300));
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

  // 登录（有持久 token 则免登）
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

  // 选 cavity-demo 算例
  await evalJs(`(() => {
    const rows = [...document.querySelectorAll('div')].filter(d => d.innerText?.includes('cavity-demo'));
    if (rows.length) rows[rows.length-1].click();
  })()`);
  await sleep(2000);

  // ===== 1. 作业面板：残差曲线 =====
  await evalJs(`(() => {
    const clickLikeUser = ${clickLikeUser};
    const tabs = [...document.querySelectorAll('.dv-tab')].filter(t => t.textContent?.trim() === '作业');
    if (tabs.length) clickLikeUser(tabs[0]);
  })()`);
  await sleep(2500);
  const jobClick = await evalJs(`(() => {
    const rows = [...document.querySelectorAll('div')].filter(d => d.innerText?.includes('#${SOLVE_JOB_ID} '));
    if (rows.length) { rows[rows.length-1].click(); return 'clicked'; }
    return 'job-not-found';
  })()`);
  await sleep(1500);
  const chartClick = await evalJs(`(() => {
    const btns = [...document.querySelectorAll('button')].filter(b => b.innerText === '残差曲线');
    if (btns.length) { btns[0].click(); return 'clicked'; }
    return 'no-chart-tab';
  })()`);
  await sleep(2500);
  const chartState = await evalJs(`(() => {
    const svg = [...document.querySelectorAll('svg')].find(s => s.querySelector('polyline'));
    const lines = svg ? svg.querySelectorAll('polyline').length : 0;
    const txt = document.body.innerText;
    return JSON.stringify({ job: '${jobClick}', chartTab: '${chartClick}',
      polylines: lines,
      hasSummary: txt.includes('末时刻'), finished: txt.includes('正常结束'),
      legendUx: txt.includes('Ux') });
  })()`);
  console.log("1. 残差曲线:", chartState);

  // ===== 2. Agent 面板：分叉/压缩按钮 + 分叉动作 =====
  await evalJs(`(() => {
    const clickLikeUser = ${clickLikeUser};
    const tabs = [...document.querySelectorAll('.dv-tab')].filter(t => t.textContent?.trim() === 'Agent');
    if (tabs.length) clickLikeUser(tabs[0]);
  })()`);
  await sleep(1200);
  const btns = await evalJs(`(() => {
    const texts = [...document.querySelectorAll('button')].map(b => b.innerText);
    return JSON.stringify({ fork: texts.includes('分叉'), compress: texts.includes('压缩') });
  })()`);
  console.log("2. Agent 按钮:", btns);
  // 选一个有多条消息的会话（fork-mock 冒烟留下的）再分叉
  // conv 下拉 = 「新会话」按钮同一栏里的第二个 select（第一个是 profile 下拉）
  const forked = await evalJs(`(async () => {
    const newBtn = [...document.querySelectorAll('button')].find(b => b.innerText === '新会话');
    if (!newBtn) return 'no-new-btn';
    const convSel = newBtn.parentElement.querySelectorAll('select')[1];
    if (!convSel || !convSel.options.length) return 'no-conv-select';
    const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
    setter.call(convSel, convSel.options[0].value);
    convSel.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise(r => setTimeout(r, 1000));
    const btn = [...document.querySelectorAll('button')].find(b => b.innerText === '分叉');
    if (!btn || btn.disabled) return 'fork-btn-disabled';
    btn.click();
    await new Promise(r => setTimeout(r, 1500));
    const opts = [...convSel.options].map(o => o.textContent);
    return opts.some(t => t?.includes('分叉')) ? 'forked:' + opts[0] : 'no-fork-option:' + opts.join('|');
  })()`);
  console.log("3. 分叉动作:", forked);

  // ===== 4. PDF 划词翻译 =====
  const pdfOpen = await evalJs(`(() => {
    const els = [...document.querySelectorAll('div')].filter(d => d.innerText?.trim().startsWith('📄') && d.innerText.includes('test.pdf'));
    if (els.length) { els[els.length-1].click(); return 'clicked'; }
    return 'not-found';
  })()`);
  await sleep(4500);
  const textLayer = await evalJs(`(() => {
    const tl = document.querySelector('.textLayer');
    return JSON.stringify({ open: '${pdfOpen}', textLayer: !!tl,
      spans: tl ? tl.querySelectorAll('span').length : 0 });
  })()`);
  console.log("4. PDF 文本层:", textLayer);
  // 合成选区（跨两个 span）→ mouseup → 译按钮 → 点击 → 翻译卡
  const transResult = await evalJs(`(async () => {
    const tl = document.querySelector('.textLayer');
    if (!tl) return 'no-textlayer';
    const spans = [...tl.querySelectorAll('span')].filter(s => s.textContent.trim().length > 3);
    if (!spans.length) return 'no-text-spans';
    const s0 = spans[0], s1 = spans.length > 1 ? spans[1] : spans[0];
    const sel = window.getSelection();
    sel.setBaseAndExtent(s0.firstChild, 0, s1.firstChild,
                         Math.min(s1 === s0 ? 12 : 4, s1.textContent.length));
    const wrap = tl.parentElement;
    wrap.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    await new Promise(r => setTimeout(r, 500));
    const btn = [...document.querySelectorAll('button')].find(b => b.innerText === '译');
    if (!btn) return 'no-translate-btn';
    btn.click();
    await new Promise(r => setTimeout(r, 4000));
    const txt = document.body.innerText;
    return txt.includes('[mock-nonstream]') ? 'translation-mock-ok'
         : txt.includes('翻译失败') ? 'translate-failed:' + txt.slice(-200)
         : 'unknown:' + txt.slice(-200);
  })()`);
  console.log("5. 划词翻译:", transResult);

  const shot = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
  writeFileSync(`${outPrefix}-shell.png`, Buffer.from(shot.data, "base64"));
  console.log("screenshot bytes:", Buffer.from(shot.data, "base64").length);

  ws.close(); edge.kill();
}

main().catch((e) => { console.error("E2E FAIL:", e.message); process.exit(1); });
