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
  await sleep(2500);  // 等 iframe worker 就绪
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
  const recog = await evalJs(`(() => {
    const btn = [...document.querySelectorAll('button')].find(b => b.innerText === '识别公式');
    if (!btn) return 'no-btn';
    if (btn.disabled) return 'disabled';
    btn.click(); return 'clicked';
  })()`);
  console.log("3. 识别触发:", recog);

  // 4. 等结果（108MB 模型 + 标量 WASM，给足时间）
  let latex = "";
  for (let i = 0; i < 60; i++) {
    await sleep(3000);
    const st = await evalJs(`(() => {
      const ta = [...document.querySelectorAll('textarea')].find(t => t.readOnly === false && t.style.fontFamily?.includes('Consolas'));
      return ta ? ta.value : '';
    })()`);
    if (st) { latex = st; break; }
    if (i % 5 === 4) {
      const status = await evalJs(`document.body.innerText.match(/正在[^\\n]*|识别失败[^\\n]*/)?.[0] ?? ''`);
      console.log(`   …等待中 (${(i+1)*3}s): ${status}`);
    }
  }
  console.log("4. 识别结果:", JSON.stringify(latex));
  if (!latex) { console.log("TEXO FAIL: 超时未出结果"); ws.close(); edge.kill(); process.exit(1); }

  // 5. 插入编辑器 → Monaco 内容含 LaTeX
  const inserted = await evalJs(`(async () => {
    const btn = [...document.querySelectorAll('button')].find(b => b.innerText === '插入编辑器光标处');
    if (!btn) return 'no-insert-btn';
    btn.click();
    await new Promise(r => setTimeout(r, 800));
    const lines = [...document.querySelectorAll('.monaco-editor .view-lines')].map(d => d.textContent);
    return lines.some(t => t.includes('$')) ? 'inserted' : 'not-visible:' + lines.join('').slice(-120);
  })()`);
  console.log("5. 插入编辑器:", inserted);

  const shot = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
  writeFileSync(`${outPrefix}-texo.png`, Buffer.from(shot.data, "base64"));
  ws.close(); edge.kill();
  console.log("TEXO E2E DONE");
}

main().catch((e) => { console.error("E2E FAIL:", e.message); process.exit(1); });
