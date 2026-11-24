// H03：真实作业残差图硬回归：initial/final、log/linear、PNG 导出。
import { execFile } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223;
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const assert = (condition, message) => { if (!condition) throw new Error(message); };

function getJson(path) {
  return new Promise((resolve, reject) => {
    http.get({host: "127.0.0.1", port: PORT, path}, response => {
      let body = "";
      response.on("data", chunk => body += chunk);
      response.on("end", () => resolve(JSON.parse(body)));
    }).on("error", reject);
  });
}

async function main() {
  const outPrefix = process.argv[2] || "residual";
  const downloadDir = mkdtempSync(join(tmpdir(), "vf-residual-download-"));
  const edge = execFile(EDGE, [
    "--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profileDir}`, "--window-size=1600,900", "about:blank",
  ], () => {});
  let ws;
  try {
    await sleep(2500);
    const targets = await getJson("/json/list");
    const page = targets.find(target => target.type === "page");
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
      ws.send(JSON.stringify({id: mid, method, params}));
    });
    const evalJs = async expression => {
      const result = await send("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
      if (result.exceptionDetails) throw new Error("页面 JS 异常: " + JSON.stringify(result.exceptionDetails).slice(0, 500));
      return result.result.value;
    };
    const waitFor = async (expression, message) => {
      for (let i = 0; i < 100; i++) {
        if (await evalJs(expression)) return;
        await sleep(100);
      }
      throw new Error(message);
    };
    const clickLikeUser = `(el) => {
      const r = el.getBoundingClientRect();
      const x = r.x + r.width / 2, y = r.y + r.height / 2;
      for (const type of ['pointerdown','mousedown','pointerup','mouseup','click'])
        el.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type,
          {bubbles:true, clientX:x, clientY:y, button:0}));
    }`;

    await send("Page.enable");
    await send("Page.navigate", {url: "http://localhost:5173/"});
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

    const fixture = await evalJs(`(async () => {
      const headers = {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'};
      const cases = await (await fetch('/api/v1/workspaces/1/cases', {headers})).json();
      const caseId = cases.find(c => c.name === 'cavity-demo').id;
      const lines = [
        'echo Time = 0.1',
        'echo smoothSolver: Solving for Ux, Initial residual = 0.1, Final residual = 0.01, No Iterations 2',
        'echo GAMG: Solving for p, Initial residual = 0.2, Final residual = 0.02, No Iterations 3',
        'echo ExecutionTime = 1.0 s',
        'echo Time = 0.2',
        'echo smoothSolver: Solving for Ux, Initial residual = 0.03, Final residual = 0.003, No Iterations 2',
        'echo GAMG: Solving for p, Initial residual = 0.04, Final residual = 0.004, No Iterations 3',
        'echo ExecutionTime = 2.0 s',
        'echo End',
      ];
      const created = await fetch('/api/v1/cases/' + caseId + '/jobs', {
        method: 'POST', headers, body: JSON.stringify({type:'shell', runtime:'windows', command:lines.join(' && ')})
      });
      const job = await created.json();
      for (let i = 0; i < 100; i++) {
        const state = await (await fetch('/api/v1/jobs/' + job.id, {headers})).json();
        if (state.status === 'succeeded') return {caseId, jobId:job.id};
        if (state.status !== 'running' && state.status !== 'queued') throw new Error(JSON.stringify(state));
        await new Promise(r => setTimeout(r, 100));
      }
      throw new Error('作业超时');
    })()`);
    console.log("1. 确定性残差作业:", fixture);

    await evalJs(`(() => {
      const rows = [...document.querySelectorAll('div')].filter(d => d.innerText?.includes('cavity-demo'));
      if (rows.length) rows[rows.length - 1].click();
    })()`);
    await sleep(1500);
    await evalJs(`(() => {
      const clickLikeUser = ${clickLikeUser};
      const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === '作业');
      clickLikeUser(tab);
    })()`);
    await waitFor(`[...document.querySelectorAll('span[title]')].some(s => s.textContent.includes('#${fixture.jobId}'))`, "新作业未出现在列表");
    await evalJs(`(() => {
      const label = [...document.querySelectorAll('span[title]')].find(s => s.textContent.includes('#${fixture.jobId}'));
      label.parentElement.click();
      [...document.querySelectorAll('button')].find(b => b.innerText === '残差曲线').click();
    })()`);
    await waitFor(`(() => {
      const svg = [...document.querySelectorAll('svg')].find(s => s.textContent.includes('粗线=final'));
      return svg && svg.querySelectorAll('polyline').length === 4;
    })()`, "残差图未生成四条曲线");

    const chart = await evalJs(`(() => {
      const svg = [...document.querySelectorAll('svg')].find(s => s.textContent.includes('粗线=final'));
      const lines = [...svg.querySelectorAll('polyline')].map(line => ({
        points:line.getAttribute('points'), dash:line.getAttribute('stroke-dasharray'), width:line.getAttribute('stroke-width')
      }));
      window.__residualLogPoints = lines.map(line => line.points).join('|');
      return {lines, text:svg.textContent, body:document.body.innerText};
    })()`);
    assert(chart.lines.filter(line => line.dash).length === 2, "缺少两条 initial 虚线");
    assert(chart.lines.filter(line => !line.dash && line.width === "1.6").length === 2, "缺少两条 final 粗线");
    assert(chart.lines.every(line => line.points.trim().split(/\s+/).length === 2), "每个序列应有两个时刻点");
    assert(/1e-?\d/.test(chart.text), "对数轴刻度缺失");
    assert(chart.body.includes("末时刻 t=0.2") && chart.body.includes("耗时 2.0s") && chart.body.includes("正常结束"), "摘要内容不完整");
    assert(chart.text.includes("Ux") && chart.text.includes("p"), "Ux/p 图例不完整");
    console.log("2. initial/final 双序列与对数轴 [OK]");

    await evalJs(`[...document.querySelectorAll('button')].find(b => b.innerText === '对数轴').click()`);
    await sleep(250);
    const linear = await evalJs(`(() => {
      const svg = [...document.querySelectorAll('svg')].find(s => s.textContent.includes('粗线=final'));
      return {
        button:[...document.querySelectorAll('button')].some(b => b.innerText === '线性轴'),
        text:svg.textContent,
        points:[...svg.querySelectorAll('polyline')].map(line => line.getAttribute('points')).join('|'),
        changed:[...svg.querySelectorAll('polyline')].map(line => line.getAttribute('points')).join('|') !== window.__residualLogPoints,
      };
    })()`);
    assert(linear.button && linear.changed && !/1e-?\d/.test(linear.text), "线性轴切换未改变刻度/坐标");
    console.log("3. 对数→线性真实坐标变化 [OK]");

    await send("Browser.setDownloadBehavior", {behavior:"allow", downloadPath:downloadDir});
    await evalJs(`[...document.querySelectorAll('button')].find(b => b.innerText === '导出 PNG').click()`);
    let exportName;
    for (let i = 0; i < 50; i++) {
      exportName = readdirSync(downloadDir).find(name => name.endsWith(".png"));
      if (exportName) break;
      await sleep(100);
    }
    assert(exportName === "residual_t0.2.png", `PNG 文件名错误: ${exportName}`);
    const png = readFileSync(join(downloadDir, exportName));
    assert(png.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])), "导出文件不是 PNG");
    assert(png.readUInt32BE(16) === 1280 && png.readUInt32BE(20) === 520, "PNG 应为 1280×520");
    assert(png.length > 5000, `PNG 内容异常小: ${png.length}`);
    writeFileSync(`${outPrefix}-residual.png`, png);
    const shot = await send("Page.captureScreenshot", {format:"png", fromSurface:true});
    writeFileSync(`${outPrefix}-residual-ui.png`, Buffer.from(shot.data, "base64"));
    console.log(`4. 真实 PNG 导出 [OK] ${png.length} bytes, 1280×520`);
    console.log("RESIDUAL E2E DONE");
  } finally {
    if (ws) ws.close();
    edge.kill();
    rmSync(downloadDir, {recursive:true, force:true});
  }
}

main().catch(error => { console.error("RESIDUAL E2E FAIL:", error.message); process.exit(1); });
