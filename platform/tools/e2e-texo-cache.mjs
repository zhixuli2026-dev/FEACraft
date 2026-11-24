// E2E 验证 texo 公式识别联动：生成公式图 → 上传算例 → TexoPanel 识别 → LaTeX → 插入编辑器
// 用法: node e2e-texo.mjs <outPrefix>
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
  const outPrefix = process.argv[2] || "texo";
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
    if (r.exceptionDetails) throw new Error("页面内 JS 异常: " + JSON.stringify(r.exceptionDetails).slice(0, 400));
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

  // 选算例
  await evalJs(`(() => {
    const rows = [...document.querySelectorAll('div')].filter(d => d.innerText?.includes('cavity-demo'));
    if (rows.length) rows[rows.length-1].click();
  })()`);
  await sleep(2000);

  // 隔离重复运行：只清理本测试专用 Edge profile 中的 Texo receipt。
  await evalJs(`(async () => {
    const index = JSON.parse(localStorage.getItem('vf_texo_receipt_index') || '[]');
    for (const hash of index) localStorage.removeItem('vf_texo_receipt_' + hash);
    localStorage.removeItem('vf_texo_receipt_index');
    const cache = await caches.open('vf-texo-receipts-v1');
    for (const request of await cache.keys()) await cache.delete(request);
  })()`);

  // 1. 画布生成公式图（黑字白底 "E = m c^2"）→ 上传到算例 Ref/
  const uploaded = await evalJs(`(async () => {
    const cv = document.createElement('canvas');
    cv.width = 320; cv.height = 96;
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 320, 96);
    ctx.fillStyle = '#000'; ctx.font = 'italic 44px serif';
    ctx.fillText('E = mc', 30, 62);
    ctx.font = 'italic 24px serif'; ctx.fillText('2', 218, 42);
    const blob = await new Promise(r => cv.toBlob(r, 'image/png'));
    const fd = new FormData();
    fd.append('file', blob, 'formula-e.png');
    const res = await fetch('/api/v1/cases/1/upload?path=' + encodeURIComponent('Ref/formula-e.png'), {
      method: 'POST', headers: { Authorization: 'Bearer ' + localStorage.getItem('vf_token') }, body: fd,
    });
    return res.status;
  })()`);
  console.log("1. 公式图上传:", uploaded);

  // 上传发生在 UI 之外 → 重新点选算例触发文件树刷新
  await evalJs(`(() => {
    const rows = [...document.querySelectorAll('div')].filter(d => d.innerText?.includes('cavity-demo'));
    if (rows.length) rows[rows.length-1].click();
  })()`);
  await sleep(2000);

  // 2. 打开一个编辑器文件（caseDict）作为插入目标
  await evalJs(`(() => {
    const els = [...document.querySelectorAll('div')].filter(d => d.innerText?.trim().startsWith('📄') && d.innerText.includes('caseDict'));
    if (els.length) els[els.length-1].click();
  })()`);
  await sleep(1500);

  // 3. 切到公式识别标签 → 选图 → 识别
  await evalJs(`(() => {
    const clickLikeUser = ${clickLikeUser};
    const tabs = [...document.querySelectorAll('.dv-tab')].filter(t => t.textContent?.trim() === '公式识别');
    if (tabs.length) clickLikeUser(tabs[0]);
  })()`);
  const workerLoaded = await evalJs(`(async () => {
    for (let i = 0; i < 100; i++) {
      const status = document.body.innerText;
      if (status.includes('页面已加载') || status.includes('首次识别需加载')) return true;
      await new Promise(r => setTimeout(r, 100));
    }
    return false;
  })()`);
  if (!workerLoaded) {
    console.log('TEXO CACHE FAIL: 计算 iframe 未完成加载');
    ws.close(); edge.kill(); process.exit(1);
  }
  const picked = await evalJs(`(async () => {
    const sel = [...document.querySelectorAll('select')].find(s =>
      [...s.options].some(o => o.textContent?.includes('formula-e.png')));
    if (!sel) return 'no-img-option';
    const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
    const opt = [...sel.options].find(o => o.textContent.includes('formula-e.png'));
    setter.call(sel, opt.value);
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise(r => setTimeout(r, 1200));
    return 'picked:' + opt.value;
  })()`);
  console.log("2. 选图:", picked);
  const injected = await evalJs(`(async () => {
    window.__texoCalls = 0;
    for (let i = 0; i < 50; i++) {
      const w = document.querySelector('iframe')?.contentWindow;
      if (w) {
        w.vfTexoRecognize = async (preview, req) => {
          window.__texoCalls++;
          const payload = btoa('E=mc^2');
          w.console.info('VIBEFLOW_TEXO:result:' + req + ':' + payload);
        };
        w.console.info('VIBEFLOW_TEXO:ready');
      }
      await new Promise(r => setTimeout(r, 100));
      const btn = [...document.querySelectorAll('button')].find(b => b.innerText === '识别公式');
      if (btn && !btn.disabled) return true;
    }
    return false;
  })()`);
  if (!injected) {
    const state = await evalJs(`({status: document.body.innerText.match(/等待页面加载[^\\n]*|页面已加载[^\\n]*|识别引擎加载中[^\\n]*/)?.[0], preview: !!document.querySelector('img[alt="待识别"]')})`);
    console.log('TEXO CACHE FAIL: 无法注入假识别器', state);
    ws.close(); edge.kill(); process.exit(1);
  }
  const recog = await evalJs(`(() => {
    const btn = [...document.querySelectorAll('button')].find(b => b.innerText === '识别公式');
    if (!btn) return 'no-btn';
    if (btn.disabled) return 'disabled';
    btn.click(); return 'clicked';
  })()`);
  console.log("3. 首次识别触发:", recog);
  const firstCheck = await evalJs(`(async () => {
    for (let i = 0; i < 20; i++) {
      const index = JSON.parse(localStorage.getItem('vf_texo_receipt_index') || '[]');
      const latex = [...document.querySelectorAll('textarea')].some(t => t.value === 'E=mc^2');
      if (index.length && latex) return {calls: window.__texoCalls, receipts: index.length, latex};
      await new Promise(r => setTimeout(r, 100));
    }
    return {calls: window.__texoCalls, receipts: 0, latex: false};
  })()`);
  console.log("4. 首次识别与 localStorage receipt:", firstCheck);
  if (firstCheck.calls !== 1 || firstCheck.receipts < 1 || !firstCheck.latex) {
    console.log("TEXO CACHE FAIL: 首次识别未生成完整 receipt");
    ws.close(); edge.kill(); process.exit(1);
  }

  await evalJs(`(() => {
    const btn = [...document.querySelectorAll('button')].find(b => b.innerText === '识别公式');
    btn.click();
  })()`);
  await sleep(800);
  const cacheCheck = await evalJs(`(async () => {
    const status = document.body.innerText;
    const cache = await caches.open('vf-texo-receipts-v1');
    const keys = await cache.keys();
    return {calls: window.__texoCalls, cacheHit: status.includes('缓存命中'), keys: keys.length};
  })()`);
  console.log("5. 缓存命中与 CacheStorage:", cacheCheck);
  if (cacheCheck.calls !== 1 || !cacheCheck.cacheHit || cacheCheck.keys < 1) {
    console.log("TEXO CACHE FAIL: 第二次未走 receipt 或重复调用模型");
    ws.close(); edge.kill(); process.exit(1);
  }

  const shot = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
  writeFileSync(`${outPrefix}-texo-cache.png`, Buffer.from(shot.data, "base64"));
  ws.close(); edge.kill();
  console.log("TEXO CACHE E2E DONE");
}

main().catch((e) => { console.error("E2E FAIL:", e.message); process.exit(1); });
