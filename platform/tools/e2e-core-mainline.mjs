// Browser acceptance for solve submission and stage gate feedback, on ui_test_server.py only.
import { execFile } from "node:child_process";
import http from "node:http";
import net from "node:net";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import WebSocket from "ws";

const [url, output] = process.argv.slice(2);
if (!url || !output) throw new Error("Provide isolated web URL and evidence directory");
const root = resolve(output);
mkdirSync(root, { recursive: true });
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const assert = (condition, message) => { if (!condition) throw new Error(message); };
const port = await new Promise(resolve => {
  const server = net.createServer();
  server.listen(0, "127.0.0.1", () => { const value = server.address().port; server.close(() => resolve(value)); });
});
const getTargets = () => new Promise((resolve, reject) => {
  http.get(`http://127.0.0.1:${port}/json/list`, response => {
    let body = ""; response.on("data", chunk => body += chunk);
    response.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});
const edge = execFile(String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`,
  ["--headless=new", "--disable-gpu", `--remote-debugging-port=${port}`,
   `--user-data-dir=${resolve(root, "edge-profile")}`, "--window-size=1600,1000", "about:blank"],
  {windowsHide: true}, () => {});
let ws;
let debugDump;
try {
  let targets;
  for (let i = 0; i < 80; i++) {
    try { targets = await getTargets(); if (targets.length) break; } catch {}
    await sleep(100);
  }
  assert(targets?.length, "Edge CDP did not start");
  ws = new WebSocket(targets.find(target => target.type === "page").webSocketDebuggerUrl);
  await new Promise(resolve => ws.on("open", resolve));
  const submissions = [];
  ws.on("message", data => {
    const event = JSON.parse(data);
    if (event.method === "Network.requestWillBeSent") {
      const request = event.params.request;
      if (request.method === "POST" && /\/cases\/\d+\/jobs$/.test(request.url))
        submissions.push(JSON.parse(request.postData));
    }
  });
  let sequence = 0;
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timeout = setTimeout(() => { ws.off("message", listener); reject(new Error("CDP timeout: " + method)); }, 20000);
    const listener = data => {
      const message = JSON.parse(data);
      if (message.id === id) {
        clearTimeout(timeout); ws.off("message", listener);
        message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
      }
    };
    ws.on("message", listener); ws.send(JSON.stringify({id, method, params}));
  });
  const evaluate = async expression => {
    const result = await send("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  debugDump = async () => {
    const snapshot = await evaluate(`({text:document.body.textContent.slice(-6000),
      tabs:[...document.querySelectorAll('.dv-tab')].map(tab=>({text:tab.textContent,
        rect:tab.getBoundingClientRect().toJSON()})),
      buttons:[...document.querySelectorAll('button')].map(button=>button.textContent)})`);
    writeFileSync(resolve(root, "browser-debug.json"), JSON.stringify(snapshot, null, 2));
    const shot = await send("Page.captureScreenshot", {format: "png", fromSurface: true});
    writeFileSync(resolve(root, "browser-debug.png"), Buffer.from(shot.data, "base64"));
  };
  const waitFor = async (expression, message) => {
    for (let i = 0; i < 150; i++) { if (await evaluate(expression)) return; await sleep(100); }
    throw new Error(message);
  };
  const change = async (selector, value) => {
    await evaluate(`(() => { const field = document.querySelector(${JSON.stringify(selector)});
      if (!field) throw new Error('Missing field');
      const proto = field.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(field, ${JSON.stringify(value)});
      field.dispatchEvent(new Event('input', {bubbles: true}));
      field.dispatchEvent(new Event('change', {bubbles: true})); })()`);
    await sleep(100);
  };
  const activateTab = async name => {
    const point = await evaluate(`(() => { const tab = [...document.querySelectorAll('.dv-tab')]
      .find(tab => tab.textContent.trim() === ${JSON.stringify(name)});
      if (!tab) throw new Error('Missing tab'); tab.scrollIntoView({block:'nearest',inline:'nearest'});
      const r = tab.getBoundingClientRect();
      return {x:r.x+r.width/2, y:r.y+r.height/2}; })()`);
    await send("Input.dispatchMouseEvent", {type: "mousePressed", ...point, button: "left", clickCount: 1});
    await send("Input.dispatchMouseEvent", {type: "mouseReleased", ...point, button: "left", clickCount: 1});
  };
  const clickRun = () => evaluate(`[...document.querySelectorAll('button')].find(button => button.textContent === '运行').click()`);
  await send("Page.enable"); await send("Network.enable");
  await send("Page.navigate", {url});
  await waitFor("document.readyState === 'complete'", "Web did not load");
  const fixture = await evaluate(`(async () => {
    const response = await fetch('/api/v1/auth/register', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:'core-ui-'+Date.now()+'@example.com', name:'core-ui', password:'isolated-ui-pass-123'})});
    const auth = await response.json(); if (!auth.token) throw new Error('Registration failed');
    localStorage.setItem('vf_token', auth.token);
    const headers = {Authorization:'Bearer '+auth.token, 'Content-Type':'application/json'};
    const workspaces = await (await fetch('/api/v1/workspaces', {headers})).json();
    const item = await (await fetch('/api/v1/workspaces/'+workspaces[0].id+'/cases', {method:'POST', headers,
      body:JSON.stringify({name:'core-browser-evidence'})})).json();
    const info = await (await fetch('/api/v1/cases/'+item.id+'/caseDict', {headers})).json();
    await fetch('/api/v1/cases/'+item.id+'/caseDict', {method:'PUT', headers,
      body:JSON.stringify({content:info.content.replace('postProcessing\\n{', 'postProcessing\\n{\\n    runDir C-run/core-ui;')})});
    return {caseId:item.id};
  })()`);
  await send("Page.reload", {ignoreCache: true});
  await waitFor(`!!document.querySelector('[data-case-name="core-browser-evidence"]')`, "Case was not listed");
  await evaluate(`document.querySelector('[data-case-name="core-browser-evidence"]').click()`);
  await activateTab("作业");
  await waitFor(`!!document.querySelector('select[aria-label="作业类型"]')`, "Job type control missing");
  await change('select[aria-label="作业类型"]', "solve");
  await waitFor(`document.querySelector('#solve-run-dir')?.value === 'C-run/core-ui'`, "Configured run directory not loaded");
  await change('input[aria-label="运行命令"]', "echo CORE_UI_OK");
  await change("#solve-run-dir", "../secret");
  await clickRun();
  await waitFor(`document.querySelector('[role="alert"]')?.textContent.includes('C-run')`, "Path rejection not visible");
  assert(submissions.at(-1).type === "solve" && submissions.at(-1).run_dir === "../secret", "Solve request lost run_dir/type");
  await change("#solve-run-dir", "C-run/core-ui");
  await clickRun();
  await waitFor(`document.querySelector('[role="alert"]')?.textContent.includes('网格')`, "Missing mesh rejection not visible");
  await change('select[aria-label="作业类型"]', "shell");
  await clickRun();
  await waitFor(`document.querySelector('input[aria-label="运行命令"]')?.value === ''`, "Successful submit did not clear command");
  await waitFor(`document.body.textContent.includes('成功') && document.body.textContent.includes('CORE_UI_OK')`, "Shell job did not complete in UI");
  assert(submissions.at(-1).type === "shell" && !('run_dir' in submissions.at(-1)), "Shell inherited solve evidence settings");
  await activateTab("caseDict");
  await waitFor(`[...document.querySelectorAll('button')].some(button => button.textContent === 'B 网格')`, "Stage buttons missing");
  await evaluate(`[...document.querySelectorAll('button')].find(button => button.textContent === 'B 网格').click()`);
  await evaluate(`[...document.querySelectorAll('button')].find(button => button.textContent === '确认→B?').click()`);
  await waitFor(`[...document.querySelectorAll('[role="alert"]')].some(node => node.textContent.includes('几何'))`, "Stage rejection not shown");
  const shot = await send("Page.captureScreenshot", {format: "png", fromSurface: true});
  writeFileSync(resolve(root, "core-mainline.png"), Buffer.from(shot.data, "base64"));
  const built = await evaluate(`(async () => {
    const headers = {Authorization:'Bearer '+localStorage.getItem('vf_token'), 'Content-Type':'application/json'};
    const call = async (path, body) => {
      const response = await fetch('/api/v1/cases/${fixture.caseId}'+path, {method:body?'PUT':'POST',headers,
        ...(body ? {body:JSON.stringify(body)} : {})});
      if (!response.ok) throw new Error('Report fixture failed: '+response.status);
      return response.json();
    };
    await call('/file', {path:'Ref/browser.svg', content:'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80"><rect width="200" height="80" fill="blue"/></svg>'});
    await call('/file', {path:'E-report/report.md', content:'# Frozen browser report\\n\\n![plot](../Ref/browser.svg)\\n\\n$x^2$\\n\\n<script>window.__reportAttack=true</script>\\n'});
    return call('/report:build');
  })()`);
  await waitFor(`fetch('/api/v1/jobs/${built.job_id}', {headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')}})
    .then(response=>response.json()).then(job=>job.status==='succeeded')`, "Report did not build");
  await send("Page.reload", {ignoreCache: true});
  await waitFor(`!!document.querySelector('[data-case-name="core-browser-evidence"]')`, "Case missing after report build");
  await evaluate(`document.querySelector('[data-case-name="core-browser-evidence"]').click()`);
  await waitFor(`!!document.querySelector('[data-path="E-report/build"]')`, "Build directory missing");
  await evaluate(`document.querySelector('[data-path="E-report/build"]').click()`);
  await waitFor(`!!document.querySelector('[data-path="E-report/build/report.html"]')`, "HTML missing from tree");
  await evaluate(`document.querySelector('[data-path="E-report/build/report.html"]').click()`);
  await waitFor(`(() => { const root=document.querySelector('[data-testid="document-shadow"]')?.shadowRoot;
    const img=root?.querySelector('img'); return !!root?.querySelector('math') && !!img?.complete && img.naturalWidth>0; })()`,
    "Reader did not render frozen SVG/MathML");
  assert(await evaluate(`!window.__reportAttack && !document.querySelector('[data-testid="document-shadow"]')?.shadowRoot?.querySelector('script')`),
    "Document script sanitization regressed");
  const reportShot = await send("Page.captureScreenshot", {format:"png", fromSurface:true});
  writeFileSync(resolve(root, "frozen-report.png"), Buffer.from(reportShot.data, "base64"));
  await evaluate(`document.querySelector('[data-path="E-report/build/report.docx"]').click()`);
  await waitFor(`(() => { const root=document.querySelector('[data-testid="document-shadow"]')?.shadowRoot;
    const img=root?.querySelector('img'); return !!root?.textContent.includes('Frozen browser report') && !!img?.complete && img.naturalWidth>0; })()`,
    "DOCX reader did not render embedded SVG");
  writeFileSync(resolve(root, "browser-result.json"), JSON.stringify({passed:true,
    checks:["configured runDir", "solve type/payload", "path and missing mesh feedback", "shell recovery", "stage feedback",
      "frozen SVG and MathML reader", "script sanitization", "DOCX embedded image"],
    submissions}, null, 2));
  console.log("CORE_BROWSER_OK: solve/stage feedback + HTML SVG/MathML + script sanitization + DOCX image");
  await send("Browser.close");
} catch (error) {
  await debugDump?.().catch(() => {});
  console.error("CORE_BROWSER_FAIL:", error.message);
  process.exitCode = 1;
} finally {
  ws?.close(); edge.kill();
}
