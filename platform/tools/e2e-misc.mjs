// E2E 验证：CSV 表格查看器 + 终端快速输入 + CasePanel 派生区
// 用法: node e2e-misc.mjs <outPrefix>
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
  const outPrefix = process.argv[2] || "misc";
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

  // 0. 造一个 CSV 进算例（D-post/data/forces.csv）
  const csvMade = await evalJs(`fetch('/api/v1/cases/1/file', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + localStorage.getItem('vf_token') },
    body: JSON.stringify({ path: 'D-post/data/forces.csv',
      content: 'time,Cd,Cl\\n0.1,1.42,0.02\\n0.2,1.31,0.05\\n0.3,1.22,0.08\\n0.4,1.15,0.10\\n0.5,1.10,0.11\\n' })
  }).then(r => r.status)`);
  console.log("0. CSV 造数据:", csvMade);

  // 选算例
  await evalJs(`(() => {
    const rows = [...document.querySelectorAll('div')].filter(d => d.innerText?.includes('cavity-demo'));
    if (rows.length) rows[rows.length-1].click();
  })()`);
  await sleep(2000);

  // 1. CSV 查看器：顶层目录默认展开，直接点 data → forces.csv → 表格+排序
  for (const seg of ["data", "forces.csv"]) {
    const r = await evalJs(`(() => {
      const els = [...document.querySelectorAll('div')].filter(d => {
        const t = d.innerText?.trim() ?? '';
        return t.endsWith(' ${seg}') && t.length < 40;
      });
      if (!els.length) return 'not-found';
      els[els.length-1].click();
      return 'clicked';
    })()`);
    console.log("   树点击", seg, "->", r);
    await sleep(900);
  }
  await sleep(1500);
  const table = await evalJs(`(async () => {
    const th = [...document.querySelectorAll('th')].find(t => t.textContent?.trim() === 'Cd');
    if (!th) return 'no-table';
    th.click();   // 排序
    await new Promise(r => setTimeout(r, 400));
    const tds = [...document.querySelectorAll('tbody tr')].map(tr => tr.children[1]?.textContent);
    return JSON.stringify({ col1: tds.slice(0, 3) });
  })()`);
  console.log("1. CSV 查看器:", table);

  // 2. 终端快速输入：开面板 → 多行 → Ctrl+Enter → 终端回显
  await evalJs(`(() => {
    const clickLikeUser = ${clickLikeUser};
    const tabs = [...document.querySelectorAll('.dv-tab')].filter(t => t.textContent?.trim() === '终端');
    if (tabs.length) clickLikeUser(tabs[0]);
  })()`);
  await sleep(2000);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll('button')].find(b => b.innerText?.includes('快速输入'));
    if (b) b.click();
  })()`);
  await sleep(500);
  await evalJs(`(() => {
    const ta = [...document.querySelectorAll('textarea')].find(t => t.placeholder?.includes('多行'));
    if (!ta) return;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(ta, 'echo QUICK_A\\necho QUICK_B');
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  })()`);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll('button')].find(b => b.innerText === '发送' && b.closest('div')?.querySelector('textarea'));
    if (b) b.click();
  })()`);
  await sleep(2500);
  const quick = await evalJs(`(() => {
    const t = [...document.querySelectorAll('.terminal')].map(d => d.innerText).join(' ');
    return t.includes('QUICK_A') && t.includes('QUICK_B') ? 'quick-ok' : 'missing:' + t.slice(-80);
  })()`);
  console.log("2. 终端快速输入:", quick);

  // 3. CasePanel 派生区渲染
  await evalJs(`(() => {
    const clickLikeUser = ${clickLikeUser};
    const tabs = [...document.querySelectorAll('.dv-tab')].filter(t => t.textContent?.trim() === 'caseDict');
    if (tabs.length) clickLikeUser(tabs[0]);
  })()`);
  await sleep(1500);
  const derive = await evalJs(`(() => {
    const txt = document.body.innerText;
    return JSON.stringify({ hasRuns: txt.includes('多工况'), hasDeriveBtn: txt.includes('派生'),
      hasConfirmNote: txt.includes('工作流状态') });
  })()`);
  console.log("3. CasePanel 派生区:", derive);

  const shot = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
  writeFileSync(`${outPrefix}-misc.png`, Buffer.from(shot.data, "base64"));
  ws.close(); edge.kill();
  console.log("MISC E2E DONE");
}
main().catch((e) => { console.error("E2E FAIL:", e.message); process.exit(1); });
