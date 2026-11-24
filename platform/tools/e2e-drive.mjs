// CDP 驱动 Edge：登录 → 验证 Shell 渲染 → 截图
// 用法: node e2e-drive.mjs <outPrefix>
import { execFile, execFileSync } from "node:child_process";
import http from "node:http";
import { writeFileSync } from "node:fs";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9223; // 避开 SeaweedFS master 默认的 9333
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function getJson(path) {
  return new Promise((resolve, reject) => {
    http.get({ host: "127.0.0.1", port: PORT, path }, (res) => {
      let b = "";
      res.on("data", (c) => (b += c));
      res.on("end", () => resolve(JSON.parse(b)));
    }).on("error", reject);
  });
}

async function main() {
  const outPrefix = process.argv[2] || "web";
  const edge = execFile(EDGE, [
    "--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profileDir}`,
    "--window-size=1600,900", "about:blank",
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
        if (m.id === mid) {
          ws.off("message", onMsg);
          m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
        }
      };
      ws.on("message", onMsg);
      ws.send(JSON.stringify({ id: mid, method, params }));
    });

  const evalJs = async (expr) => {
    const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error("页面内 JS 异常: " + JSON.stringify(r.exceptionDetails).slice(0, 500) + "\n表达式: " + expr.slice(0, 700));
    return r.result.value;
  };

  let dialogAnswer = "";
  ws.on("message", (data) => {
    const message = JSON.parse(data);
    if (message.method === "Page.javascriptDialogOpening") {
      void send("Page.handleJavaScriptDialog", { accept: true, promptText: dialogAnswer });
    }
  });

  await send("Page.enable");
  await send("Page.navigate", { url: process.env.VIBEFLOW_TEST_WEB || "http://localhost:5173/" });
  await sleep(3000);

  // 登录（profile 里已有 token 时会自动免登，跳过填表）
  const hasForm = await evalJs(`!!document.querySelector('form')`);
  if (hasForm) {
    const fillResult = await evalJs(`
      (() => {
        const inputs = document.querySelectorAll('input');
        if (inputs.length < 2) return 'no-inputs:' + document.body.innerText.slice(0, 100);
        const setVal = (el, v) => {
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
          setter.call(el, v);
          el.dispatchEvent(new Event('input', { bubbles: true }));
        };
        setVal(inputs[inputs.length - 2], 'dev@vibeflow.cn');
        setVal(inputs[inputs.length - 1], 'devpass123');
        return 'filled';
      })()
    `);
    console.log("fill:", fillResult);
    await evalJs(`document.querySelector('form').requestSubmit()`);
    await sleep(3500);
  } else {
    console.log("已免登（token 持久化生效）");
    await sleep(1500);
  }

  const state = await evalJs(`document.body.innerText.slice(0, 400)`);
  console.log("page text:", JSON.stringify(state?.slice(0, 200)));

  // 终端面板验证（提前到算例操作之前，避免后续 DOM 变动干扰）
  await evalJs(`
    (() => {
      const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === '终端');
      if (!tab) return 'no-terminal-tab';
      const r = tab.getBoundingClientRect();
      const x = r.x + r.width/2, y = r.y + r.height/2;
      for (const type of ['pointerdown','mousedown','pointerup','mouseup','click'])
        tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, {bubbles:true, clientX:x, clientY:y, button:0}));
      return 'clicked';
    })()
  `);
  await sleep(2000); // 等 xterm 挂载 + WS 连接
  const focusOk = await evalJs(`
    (() => {
      const ta = document.querySelector('.xterm-helper-textarea');
      if (!ta) return 'no-terminal';
      ta.focus();
      return 'focused';
    })()
  `);
  console.log("terminal focus:", focusOk);
  if (focusOk !== 'focused') throw new Error('终端面板未挂载');
  await send("Input.insertText", { text: "echo WEB_PTY_OK_123\r" });  // 可信事件注入
  await sleep(2500);
  const termState = await evalJs(`
    fetch('/api/v1/terminal/sessions', {
      headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
    }).then(r => r.json()).then(j => JSON.stringify(j))
  `);
  console.log("terminal sessions:", termState);
  if (!termState?.startsWith('{"sessions"')) throw new Error('终端会话 API 未返回已认证列表');
  const termBuf = await evalJs(`
    (() => {
      const all = [...document.querySelectorAll('span')];
      const txt = all.map(s => s.textContent).join('');
      const i = txt.indexOf('WEB_PTY_OK_123');
      return i >= 0 ? 'echo-found' : txt.slice(-150);
    })()
  `);
  console.log("terminal echo:", termBuf);

  // 选中 cavity-demo 算例（稳定属性，不依赖父子文本嵌套）
  const clicked = await evalJs(`
    (() => {
      const row = document.querySelector('[data-case-name="cavity-demo"]');
      if (!row) return 'not-found';
      row.click();
      return 'clicked';
    })()
  `);
  if (clicked !== 'clicked') throw new Error('cavity-demo 未找到: ' + clicked);
  console.log("select case:", clicked);
  await sleep(2500);

  // 打开文件树里的 caseDict（等待真实树节点，不用固定延迟猜测加载完成）
  let opened = 'not-found';
  for (let i = 0; i < 40 && opened === 'not-found'; i++) {
    opened = await evalJs(`
      (() => {
        const el = document.querySelector('[data-path="0-caseDict/caseDict"]');
        if (!el) return 'not-found';
        el.click();
        return 'opened';
      })()
    `);
    if (opened === 'not-found') await sleep(250);
  }
  if (opened !== 'opened') throw new Error('caseDict 文件树节点未找到');
  console.log("open caseDict:", opened);

  // caseDict Monaco 保存：必须走专用 /caseDict，保存后文件和 revision 都更新，最后恢复原文
  const beforeDict = await evalJs(`
    fetch('/api/v1/cases/1/caseDict', {
      cache: 'no-store',
      headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
    }).then(r => r.json())
  `);
  const nextDictText = JSON.stringify(beforeDict.content + "\n// P0_MONACO_SAVE\n");
  let modelSet = 'no-model';
  for (let i = 0; i < 40 && modelSet !== 'set'; i++) {
    modelSet = await evalJs(`
      (() => {
        const activeTab = document.querySelector('[data-editor-path="0-caseDict/caseDict"][data-active="true"]');
        if (!activeTab || !window.monaco?.editor?.getModels()?.length) return 'no-model';
        monaco.editor.getModels()[0].setValue(${nextDictText});
        return 'set';
      })()
    `);
    if (modelSet !== 'set') await sleep(250);
  }
  if (modelSet !== 'set') throw new Error('caseDict Monaco model 未就绪: ' + modelSet);
  await sleep(500);
  const saveClicked = await evalJs(`
    (() => {
      const btn = [...document.querySelectorAll('button')].find(b => b.textContent?.includes('保存 (Ctrl+S)'));
      if (!btn) return 'no-save';
      if (btn.disabled) return 'disabled';
      btn.click();
      return 'clicked';
    })()
  `);
  if (saveClicked !== 'clicked') throw new Error('caseDict 保存按钮不可点击: ' + saveClicked);
  let afterDict;
  for (let i = 0; i < 30; i++) {
    afterDict = await evalJs(`
      fetch('/api/v1/cases/1/caseDict?ts=' + Date.now(), {
        cache: 'no-store',
        headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
      }).then(r => r.json())
    `);
    if (afterDict.content.includes('P0_MONACO_SAVE') && afterDict.dict_revision > beforeDict.dict_revision) break;
    await sleep(500);
  }
  const saved = afterDict.content.includes('P0_MONACO_SAVE') && afterDict.dict_revision > beforeDict.dict_revision;
  await evalJs(`
    fetch('/api/v1/cases/1/caseDict', {
      method: 'PUT',
      headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
      body: JSON.stringify({content: ${JSON.stringify(beforeDict.content)}})
    }).then(r => r.json())
  `);
  if (!saved) throw new Error('caseDict Monaco 保存未生效或 revision 未增加');
  console.log('caseDict Monaco save: OK（专用端点+revision 增加，原文已恢复）');

  // 无扩展名场文件：实际打开 0/U，Monaco 必须使用 OpenFOAM 语言
  const clickTreeRow = async (path) => {
    let result = 'not-found';
    for (let i = 0; i < 120 && result !== 'clicked'; i++) {
      result = await evalJs(`
        (function () {
          var wanted = ${JSON.stringify(path)};
          var rows = document.querySelectorAll('[data-path]');
          for (var i = 0; i < rows.length; i++) {
            if (rows[i].getAttribute('data-path') === wanted) {
              rows[i].click();
              return 'clicked';
            }
          }
          return 'not-found';
        })()
      `);
      if (result !== 'clicked') await sleep(250);
    }
    if (result !== 'clicked') throw new Error(`文件树节点 ${path} 未找到: ${result}`);
    await sleep(350);
  };
  for (const path of ['C-run/case-1', 'C-run/case-1/0', 'C-run/case-1/0/U']) await clickTreeRow(path);
  const activateDockTab = async (title) => {
    const result = await evalJs(`
      (() => {
        const tab = [...document.querySelectorAll('.dv-tab')].find(t => t.textContent?.trim() === ${JSON.stringify(title)});
        if (!tab) return 'no-tab';
        const r = tab.getBoundingClientRect();
        const x = r.x + r.width / 2, y = r.y + r.height / 2;
        for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
          tab.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, {bubbles: true, clientX: x, clientY: y, button: 0}));
        }
        return 'clicked';
      })()
    `);
    if (result !== 'clicked') throw new Error(`页签 ${title} 未找到: ${result}`);
    await sleep(300);
  };
  await sleep(900);
  let foamModel = null;
  for (let i = 0; i < 20; i++) {
    await sleep(500);
    foamModel = await evalJs(`
      (() => {
        const models = window.monaco?.editor?.getModels() ?? [];
        const active = models[models.length - 1];
        const activeTab = document.querySelector('[data-editor-path][data-active="true"]');
        const path = activeTab?.getAttribute('data-editor-path') ?? '';
        return active ? {language: active.getLanguageId(), uri: active.uri.toString(), path} : null;
      })()
    `);
    if (foamModel?.language === 'openfoam-dict' && foamModel.path.endsWith('/0/U')) break;
  }
  if (!foamModel || foamModel.language !== 'openfoam-dict' || !foamModel.path.endsWith('/0/U')) {
    throw new Error('0/U 未按 OpenFOAM 语言打开: ' + JSON.stringify(foamModel));
  }
  console.log('0/U Monaco language: OK', foamModel);

  // .foam 专用路由：按所在目录启动 viz，并只关闭本次新建会话
  const vizBefore = await evalJs(`
    fetch('/api/v1/cases/1/viz:sessions', {headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}})
      .then(r => r.json())
  `);
  const vizBeforeIds = new Set(vizBefore.map(s => s.id));
  await clickTreeRow('C-run/case-1/case-1.foam');
  let foamViz = null;
  for (let i = 0; i < 30; i++) {
    await sleep(1000);
    foamViz = await evalJs(`
      (async () => {
        const sessions = await fetch('/api/v1/cases/1/viz:sessions', {headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}}).then(r => r.json());
        const created = sessions.find(s => !${JSON.stringify([...vizBeforeIds])}.includes(s.id));
        const frame = document.querySelector('iframe[title="可视化"]');
        return {created, frameSrc: frame?.src ?? ''};
      })()
    `);
    if (foamViz.created && foamViz.frameSrc) break;
  }
  if (!foamViz.created || foamViz.created.run_dir !== 'C-run/case-1' ||
      !foamViz.frameSrc.includes(`/viz-proxy/${foamViz.created.id}/`)) {
    throw new Error('.foam 未按 run_dir 打开可视化: ' + JSON.stringify(foamViz));
  }
  await sleep(4000);
  const foamFrameBody = await evalJs(`document.querySelector('iframe[title="可视化"]')?.contentDocument?.body?.innerText ?? ''`);
  if (foamFrameBody.includes('令牌无效')) throw new Error('.foam iframe 子资源鉴权失败');
  await evalJs(`fetch('/api/v1/viz:sessions/${foamViz.created.id}', {
    method: 'DELETE', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
  })`);
  console.log('.foam run_dir viz route: OK', foamViz.created.id);

  // 通用结果文件：文件树直接打开 VTP，必须按 file_path 创建会话
  const readerPath = 'D-post/reader-e2e.vtp';
  const readerVtp = `<?xml version="1.0"?>
<VTKFile type="PolyData" version="1.0" byte_order="LittleEndian"><PolyData><Piece NumberOfPoints="3" NumberOfVerts="0" NumberOfLines="0" NumberOfStrips="0" NumberOfPolys="1"><PointData><DataArray type="Float32" Name="p" format="ascii">1 2 3</DataArray></PointData><Points><DataArray type="Float32" NumberOfComponents="3" format="ascii">0 0 0 1 0 0 0 1 0</DataArray></Points><Polys><DataArray type="Int32" Name="connectivity" format="ascii">0 1 2</DataArray><DataArray type="Int32" Name="offsets" format="ascii">3</DataArray></Polys></Piece></PolyData></VTKFile>`;
  const readerBeforeIds = await evalJs(`fetch('/api/v1/cases/1/viz:sessions', {headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}}).then(r => r.json()).then(j => j.map(s => s.id))`);
  await evalJs(`fetch('/api/v1/cases/1/file', {
    method: 'PUT', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
    body: JSON.stringify({path: ${JSON.stringify(readerPath)}, content: ${JSON.stringify(readerVtp)}})
  })`);
  await evalJs(`document.querySelector('[data-case-name="cavity-demo"]').click()`);
  await sleep(900);
  await clickTreeRow(readerPath);
  await sleep(15000);
  const readerViz = await evalJs(`
    (async () => {
      const sessions = await fetch('/api/v1/cases/1/viz:sessions', {headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}}).then(r => r.json());
      const created = sessions.find(s => !${JSON.stringify([...readerBeforeIds])}.includes(s.id));
      const frame = document.querySelector('iframe[title="可视化"]');
      return {created, frameSrc: frame?.src ?? ''};
    })()
  `);
  if (!readerViz.created || readerViz.created.file_path !== readerPath ||
      !readerViz.frameSrc.includes(`/viz-proxy/${readerViz.created.id}/`)) {
    throw new Error('VTP 未按 file_path 打开可视化: ' + JSON.stringify(readerViz));
  }
  const readerShot = await send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
  writeFileSync(`${outPrefix}-vtp.png`, Buffer.from(readerShot.data, "base64"));
  const readerFrameDiag = await evalJs(`(() => {
    const frame = document.querySelector('iframe[title="可视化"]');
    const doc = frame?.contentDocument;
    return {readyState: doc?.readyState ?? '', body: doc?.body?.innerText?.slice(0, 300) ?? '',
      path: doc?.location?.pathname ?? '',
      canvases: doc ? [...doc.querySelectorAll('canvas')].map(c => ({width: c.width, height: c.height})) : []};
  })()`);
  console.log('VTP iframe diagnostics:', JSON.stringify(readerFrameDiag));
  const readerTokenDiag = await evalJs(`(() => {
    const frame = document.querySelector('iframe[title="可视化"]');
    const src = frame?.src ?? '';
    const queryToken = new URL(src).searchParams.get('token') ?? '';
    const storedToken = localStorage.getItem('vf_token') ?? '';
    return {storedLength: storedToken.length, queryLength: queryToken.length, equal: queryToken === storedToken};
  })()`);
  console.log('VTP token diagnostics:', JSON.stringify(readerTokenDiag));
  const readerFetchDiag = await evalJs(`fetch(${JSON.stringify(readerViz.frameSrc)}).then(async r => ({status: r.status, body: (await r.text()).slice(0, 100)}))`);
  console.log('VTP same-URL fetch diagnostics:', JSON.stringify(readerFetchDiag));
  if (readerFrameDiag.body.includes('令牌无效')) {
    throw new Error('VTP iframe 鉴权失败: ' + JSON.stringify(readerTokenDiag));
  }
  const customColormap = JSON.stringify({schema: 'vibeflow.colormap.v1', points: [
    {position: 0, color: '#000000'}, {position: 0.5, color: '#FFFF00'}, {position: 1, color: '#000000'},
  ]});
  const customSelect = await evalJs(`(() => {
    const doc = document.querySelector('iframe[title="可视化"]').contentDocument;
    const root = [...doc.querySelectorAll('.v-select')].find(el => el.textContent?.includes('预设'));
    const select = root?.querySelector('select');
    if (!select) {
      const field = root?.querySelector('.v-field') ?? root?.querySelector('input') ?? root;
      if (!field) return 'no-select';
      field.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
      field.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
      field.dispatchEvent(new MouseEvent('click', {bubbles: true}));
      return 'menu';
    }
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, 'custom');
    select.dispatchEvent(new Event('input', {bubbles: true}));
    select.dispatchEvent(new Event('change', {bubbles: true}));
    return 'ok';
  })()`);
  if (!['ok', 'menu'].includes(customSelect)) throw new Error('未找到色图预设选择器: ' + customSelect);
  if (customSelect === 'menu') {
    await sleep(400);
    const customOption = await evalJs(`(() => {
      const doc = document.querySelector('iframe[title="可视化"]').contentDocument;
      const item = [...doc.querySelectorAll('.v-list-item, .v-list-item-title')]
        .find(el => el.textContent?.trim() === 'custom');
      if (!item) return 'no-option';
      item.dispatchEvent(new MouseEvent('click', {bubbles: true}));
      return 'ok';
    })()`);
    if (customOption !== 'ok') throw new Error('custom 色图选项未展开: ' + customOption);
  }
  await sleep(600);
  const customApply = await evalJs(`(() => {
    const doc = document.querySelector('iframe[title="可视化"]').contentDocument;
    const textarea = doc.querySelector('textarea');
    if (!textarea) return 'no-textarea';
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(textarea, ${JSON.stringify(customColormap)});
    textarea.dispatchEvent(new Event('input', {bubbles: true}));
    const button = [...doc.querySelectorAll('button')].find(b => b.textContent?.trim() === '应用控制点');
    if (!button) return 'no-apply';
    button.click();
    return 'ok';
  })()`);
  if (customApply !== 'ok') throw new Error('自由控制点编辑失败: ' + customApply);
  await sleep(1500);
  await evalJs(`[...document.querySelector('iframe[title="可视化"]').contentDocument.querySelectorAll('button')].find(b => b.textContent?.trim() === '导出').click()`);
  await sleep(1000);
  const exportedColormap = await evalJs(`fetch('/api/v1/cases/1/file?path=' + encodeURIComponent('D-post/colormap.json'), {
    headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
  }).then(r => r.json()).then(j => j.content ? JSON.parse(j.content) : j)`);
  if (exportedColormap.schema !== 'vibeflow.colormap.v1' ||
      exportedColormap.points?.[1]?.color !== '#FFFF00') {
    throw new Error('自由控制点导出内容错误: ' + JSON.stringify(exportedColormap));
  }
  const customShot = await send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
  writeFileSync(`${outPrefix}-vtp.png`, Buffer.from(customShot.data, "base64"));
  console.log('custom colormap apply/export: OK');
  await evalJs(`fetch('/api/v1/viz:sessions/${readerViz.created.id}', {
    method: 'DELETE', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
  })`);
  await evalJs(`fetch('/api/v1/cases/1/file?path=' + encodeURIComponent(${JSON.stringify(readerPath)}), {
    method: 'DELETE', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
  })`);
  console.log('VTP file_path viz route: OK', readerViz.created.id);

  // 二进制下载：必须 fetch→Blob→objectURL，并在下载后释放，不能只给裸 href
  const docxInfo = await evalJs(`
    fetch('/api/v1/cases/1/file?path=' + encodeURIComponent('E-report/build/report.docx'), {
      headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
    }).then(r => r.json())
  `);
  if (docxInfo.kind !== 'binary') throw new Error('report.docx 不存在或不是二进制: ' + JSON.stringify(docxInfo));
  await evalJs(`
    (() => {
      const build = [...document.querySelectorAll('div')].find(d => d.children.length === 0 && d.textContent?.trim().endsWith('build'));
      if (!build) {
        const root = [...document.querySelectorAll('div')].find(d => d.children.length === 0 && d.textContent?.trim().endsWith('E-report'));
        if (root) root.click();
      }
      return 'root-ready';
    })()
  `);
  await sleep(400);
  await evalJs(`
    (() => {
      const builds = [...document.querySelectorAll('div')].filter(d => d.children.length === 0 && d.textContent?.trim().endsWith('build'));
      if (builds.length) builds[builds.length - 1].click();
      return 'expanded';
    })()
  `);
  await sleep(600);
  const folderDump = await evalJs(`
    [...document.querySelectorAll('div')].filter(d => d.children.length === 0)
      .map(d => d.textContent?.trim()).filter(t => t?.includes('📁')).slice(0, 80)
  `);
  console.log('report folders:', JSON.stringify(folderDump));
  const docxOpened = await evalJs(`
    (() => {
      const els = [...document.querySelectorAll('div')].filter(d =>
        d.textContent?.includes('📄') && d.textContent.trim().endsWith('report.docx'));
      if (!els.length) return 'not-found';
      els[els.length-1].click();
      return 'opened';
    })()
  `);
  if (docxOpened !== 'opened') throw new Error('report.docx 未找到: ' + docxOpened);
  await sleep(1200);
  await evalJs(`
    window.__downloadResp = null;
    window.__blobUrl = null;
    window.__revoked = false;
    const originalFetch = window.fetch;
    window.fetch = async (...args) => {
      const response = await originalFetch(...args);
      if (String(args[0]).includes('/raw')) {
        window.__downloadResp = {url: String(args[0]), status: response.status};
      }
      return response;
    };
    const originalCreate = URL.createObjectURL;
    URL.createObjectURL = (blob) => {
      window.__blobUrl = originalCreate(blob);
      return window.__blobUrl;
    };
    const originalRevoke = URL.revokeObjectURL;
    URL.revokeObjectURL = (url) => {
      window.__revoked = true;
      return originalRevoke(url);
    };
    'instrumented';
  `);
  const downloadClicked = await evalJs(`
    (() => {
      const link = [...document.querySelectorAll('button')].find(a => a.textContent?.trim() === '下载');
      if (!link) return 'no-download';
      link.click();
      return 'clicked';
    })()
  `);
  if (downloadClicked !== 'clicked') throw new Error('二进制下载按钮未找到: ' + downloadClicked);
  let downloadResult = null;
  for (let i = 0; i < 30; i++) {
    await sleep(500);
    downloadResult = await evalJs(`({
      response: window.__downloadResp,
      blobUrl: window.__blobUrl,
      revoked: window.__revoked
    })`);
    if (downloadResult.revoked) break;
  }
  if (!downloadResult.response || downloadResult.response.status !== 200 ||
      !downloadResult.blobUrl?.startsWith('blob:') || !downloadResult.revoked) {
    throw new Error('二进制下载未走认证 Blob 流程: ' + JSON.stringify(downloadResult));
  }
  console.log('binary download: OK（Bearer fetch + Blob URL + revoke）');

  // Agent 聊天验证：切到 Agent 标签 → 新会话 → 发消息 → 等 SSE → 校验 mock 回复
  const tabDump = await evalJs(`
    JSON.stringify([...document.querySelectorAll('.dv-tab, [class*=tab]')].map(t => t.textContent?.trim()).filter(Boolean))
  `);
  console.log("tabs now:", tabDump);
  const agentTab = await evalJs(`
    (() => {
      const clickLikeUser = (el) => {
        const r = el.getBoundingClientRect();
        const x = r.x + r.width/2, y = r.y + r.height/2;
        for (const type of ['pointerdown','mousedown','pointerup','mouseup','click'])
          el.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, {bubbles:true, clientX:x, clientY:y, button:0}));
      };
      const tabs = [...document.querySelectorAll('.dv-tab')].filter(t => t.textContent?.trim() === 'Agent');
      if (tabs.length) { clickLikeUser(tabs[0]); return 'clicked:' + tabs.length; }
      const any = [...document.querySelectorAll('*')].filter(d => d.children.length === 0 && d.textContent?.trim() === 'Agent');
      if (any.length) { clickLikeUser(any[any.length-1]); return 'clicked-fallback'; }
      return 'no-agent-tab';
    })()
  `);
  console.log("agent tab:", agentTab);
  await sleep(800);
  // 选本地 mock profile（不消耗真实 LLM 凭据或配额）
  const profSel = await evalJs(`
    (() => {
      const sel = document.querySelector('[data-testid="agent-profile"]');
      if (!sel) return 'no-select';
      const opts = [...sel.options];
      const mock = opts.findIndex(o => /mock/i.test(o.textContent));
      if (mock < 0) return 'no-mock-option:' + opts.map(o => o.textContent).join('|');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
      setter.call(sel, sel.options[mock].value);
      sel.dispatchEvent(new Event('change', { bubbles: true }));
      return 'selected:' + sel.options[mock].textContent;
    })()
  `);
  console.log("profile select:", profSel);
  if (!profSel.startsWith('selected:')) throw new Error('mock profile 选择失败: ' + profSel);
  await sleep(500);

  const newConv = await evalJs(`
    (() => {
      const btns = [...document.querySelectorAll('button')].filter(b => b.innerText === '新会话');
      if (btns.length) { btns[0].click(); return 'clicked'; }
      return 'no-btn';
    })()
  `);
  console.log("new conv:", newConv);
  await sleep(1200);
  const chatTyped = await evalJs(`
    (() => {
      const ta = document.querySelector('textarea[placeholder*="仿真需求"]');
      if (!ta) return 'no-textarea';
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(ta, '用一句话回答：什么是有限体积法？');
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      return 'typed';
    })()
  `);
  console.log("chat typed:", chatTyped);
  const sent = await evalJs(`
    (() => {
      const btns = [...document.querySelectorAll('button')].filter(b => b.innerText === '发送');
      if (btns.length) { btns[0].click(); return 'clicked'; }
      return 'no-send-btn';
    })()
  `);
  console.log("chat sent:", sent);
  let chatReply = 'waiting';
  for (let i = 0; i < 80 && chatReply === 'waiting'; i++) {
    await sleep(250);
    chatReply = await evalJs(`
      (() => {
        const text = document.body.innerText;
        if (text.includes('[mock]')) return 'mock-reply-found';
        if (text.includes('[错误]')) return 'agent-error:' + text.slice(-500);
        return 'waiting';
      })()
    `);
  }
  console.log("chat reply:", JSON.stringify(chatReply?.slice(0, 150)));
  if (chatReply !== 'mock-reply-found') throw new Error('Agent mock 回复未成功: ' + chatReply);

  // 几何预览验证：切到「几何预览」标签 → 重载 scene.json → 校验对象信息出现
  const geoTab = await evalJs(`
    (() => {
      const clickLikeUser = (el) => {
        const r = el.getBoundingClientRect();
        const x = r.x + r.width/2, y = r.y + r.height/2;
        for (const type of ['pointerdown','mousedown','pointerup','mouseup','click'])
          el.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, {bubbles:true, clientX:x, clientY:y, button:0}));
      };
      const tabs = [...document.querySelectorAll('.dv-tab')].filter(t => t.textContent?.trim() === '几何预览');
      if (!tabs.length) {
        const any = [...document.querySelectorAll('.dv-default-tab-content, .dv-tab')].filter(d => d.textContent?.trim().includes('几何预览'));
        if (any.length) { clickLikeUser(any[any.length-1]); return 'clicked-fallback'; }
        return 'no-geo-tab';
      }
      clickLikeUser(tabs[0]); return 'clicked';
    })()
  `);
  console.log("geo tab:", geoTab);
  await sleep(1500);
  const geoLoad = await evalJs(`
    (() => {
      const btns = [...document.querySelectorAll('button')].filter(b => b.innerText?.includes('重载'));
      if (btns.length) { btns[0].click(); return 'clicked'; }
      return 'no-reload-btn';
    })()
  `);
  console.log("geo reload:", geoLoad);
  await sleep(3000);
  const geoInfo = await evalJs(`
    (() => {
      const txt = document.body.innerText;
      const m = txt.match(/(\\d+) 顶点\\/(\\d+) 面/);
      const canvas = document.querySelector('.dv-panel canvas');
      return JSON.stringify({ objInfo: m ? m[0] : null, hasCanvas: !!canvas });
    })()
  `);
  console.log("geo info:", geoInfo);

  // 保存即自动预览：成功自动刷新，失败保留上一有效场景
  const originalGeometry = await evalJs(`
    fetch('/api/v1/cases/1/file?path=' + encodeURIComponent('A-geo/geometry.py'), {
      headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
    }).then(r => r.json()).then(j => j.content)
  `);
  try {
    await clickTreeRow('A-geo/geometry.py');
    const autoScript = `import cadquery as cq
def build_geometry(ctx):
    return {"auto": cq.Workplane("XY").box(2, 2, 2)}
`;
    await evalJs(`window.monaco.editor.getModels().at(-1).setValue(${JSON.stringify(autoScript)})`);
    await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '保存 (Ctrl+S)').click()`);
    let autoState = null;
    for (let i = 0; i < 30; i++) {
      await sleep(1000);
      autoState = await evalJs(`({text: document.body.innerText, canvases: document.querySelectorAll('canvas').length})`);
      if (autoState.text.includes('auto（')) break;
    }
    if (!autoState?.text.includes('auto（')) throw new Error('保存后自动预览未刷新新对象');
    console.log('auto-preview save/refresh: OK', {canvases: autoState.canvases});
    const previousAutoJob = await evalJs(`document.querySelector('[data-auto-job]')?.getAttribute('data-auto-job') ?? ''`);
    await clickTreeRow('A-geo/geometry.py');
    await evalJs(`window.monaco.editor.getModels().at(-1).setValue('raise RuntimeError("AUTO_FAIL")')`);
    await evalJs(`window.confirm = () => true`);
    await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '保存 (Ctrl+S)').click()`);
    await activateDockTab('几何预览');
    let autoFailureStatus = '';
    let autoFailureInfo = '';
    let autoFailureJob = '';
    for (let i = 0; i < 45; i++) {
      await sleep(1000);
      const state = await evalJs(`({
        status: document.querySelector('[data-testid="geo-status"]')?.textContent ?? '',
        info: document.querySelector('[data-testid="geo-obj-info"]')?.textContent ?? '',
        job: document.querySelector('[data-auto-job]')?.getAttribute('data-auto-job') ?? ''
      })`);
      autoFailureStatus = state.status;
      autoFailureInfo = state.info;
      autoFailureJob = state.job;
      if (autoFailureJob !== previousAutoJob && autoFailureStatus.includes('预览失败')) break;
    }
    if (autoFailureJob === previousAutoJob || !autoFailureStatus.includes('预览失败') || !autoFailureInfo.includes('auto（')) {
      throw new Error('预览失败时未保留上一有效场景: ' + JSON.stringify({autoFailureStatus, autoFailureInfo, autoFailureJob, previousAutoJob}));
    }
    console.log('auto-preview failure keeps last scene: OK', {job: autoFailureJob});
  } finally {
    await evalJs(`fetch('/api/v1/cases/1/file', {
      method: 'PUT', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
      body: JSON.stringify({path: 'A-geo/geometry.py', content: ${JSON.stringify(originalGeometry)}})
    })`);
  }

  // JobsPanel runtime：选择 WSL 并实际执行 pwd，验证不是只传 Windows 默认值
  const jobsTab = await evalJs(`
    (() => {
      const clickLikeUser = (el) => {
        const r = el.getBoundingClientRect();
        const x = r.x + r.width/2, y = r.y + r.height/2;
        for (const type of ['pointerdown','mousedown','pointerup','mouseup','click'])
          el.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, {bubbles:true, clientX:x, clientY:y, button:0}));
      };
      const tabs = [...document.querySelectorAll('.dv-tab')].filter(t => t.textContent?.trim() === '作业');
      if (!tabs.length) return 'no-jobs-tab';
      clickLikeUser(tabs[0]);
      return 'clicked';
    })()
  `);
  if (jobsTab !== 'clicked') throw new Error('作业页签未找到: ' + jobsTab);
  await sleep(800);
  const runtimeSet = await evalJs(`
    (() => {
      const sel = document.querySelector('select[title="选择作业运行环境"]');
      if (!sel) return 'no-runtime-select';
      const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
      setter.call(sel, 'wsl');
      sel.dispatchEvent(new Event('change', { bubbles: true }));
      return 'wsl';
    })()
  `);
  if (runtimeSet !== 'wsl') throw new Error('runtime 下拉框未找到: ' + runtimeSet);
  await sleep(400);
  const cmdSet = await evalJs(`
    (() => {
      const input = document.querySelector('input[placeholder*="运行命令"]');
      if (!input) return 'no-command-input';
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(input, 'pwd');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      return 'pwd';
    })()
  `);
  if (cmdSet !== 'pwd') throw new Error('作业命令输入框未找到: ' + cmdSet);
  const runClicked = await evalJs(`
    (() => {
      const btn = [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '运行');
      if (!btn) return 'no-run';
      btn.click();
      return 'clicked';
    })()
  `);
  if (runClicked !== 'clicked') throw new Error('运行按钮未找到: ' + runClicked);
  await sleep(5000);
  const wslOutput = await evalJs(`document.body.innerText.includes('/mnt/')`);
  if (!wslOutput) throw new Error('WSL pwd 未在作业日志返回 /mnt/ 路径');
  console.log('jobs runtime wsl: OK（实际返回 /mnt/ 路径）');

  // PDF 查看器验证：点 Ref/test.pdf → 查看器标签激活 → canvas 渲染
  const pdfOpen = await evalJs(`
    (() => {
      const els = [...document.querySelectorAll('div')].filter(d => d.innerText?.trim().startsWith('📄') && d.innerText.includes('test.pdf'));
      if (els.length) { els[els.length-1].click(); return 'clicked'; }
      return 'not-found';
    })()
  `);
  console.log("pdf open:", pdfOpen);
  await sleep(4000);
  const pdfState = await evalJs(`
    (() => {
      const canvases = [...document.querySelectorAll('canvas')];
      const viewer = canvases.find(c => c.width > 100 && c.height > 100);
      return JSON.stringify({ canvases: canvases.length, viewerCanvas: !!viewer,
        viewerSize: viewer ? viewer.width + 'x' + viewer.height : null });
    })()
  `);
  console.log("pdf state:", pdfState);


  const shot = await send("Page.captureScreenshot", {
    format: "png", fromSurface: true, captureBeyondViewport: false,
  });
  writeFileSync(`${outPrefix}-shell.png`, Buffer.from(shot.data, "base64"));
  const pngSize = Buffer.from(shot.data, "base64").length;
  console.log("screenshot bytes:", pngSize);

  // Open Editors：双文件标签、全部保存、关闭、路径操作与加入聊天
  const editorA = 'D-post/editor-a.txt';
  const editorB = 'D-post/editor-b.txt';
  for (const [path, content] of [[editorA, 'alpha'], [editorB, 'beta']]) {
    const written = await evalJs(`
      fetch('/api/v1/cases/1/file', {
        method: 'PUT', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
        body: JSON.stringify({path: ${JSON.stringify(path)}, content: ${JSON.stringify(content)}})
      }).then(r => r.ok)
    `);
    if (!written) throw new Error('临时编辑器文件写入失败: ' + path);
  }
  await evalJs(`document.querySelector('[data-case-name="cavity-demo"]').click()`);
  await sleep(800);
  for (const path of [editorA, editorB]) {
    await clickTreeRow(path);
    await sleep(400);
  }
  let editorTabs = 0;
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    editorTabs = await evalJs(`document.querySelectorAll('[data-editor-path]').length`);
    if (editorTabs === 2) break;
  }
  if (editorTabs !== 2) throw new Error('Open Editors 文件标签不是 2 个: ' + editorTabs);
  const dirtyValues = new Map([[editorA, 'ALPHA_DIRTY'], [editorB, 'BETA_DIRTY']]);
  for (const path of [editorA, editorB]) {
    await clickTreeRow(path);
    await sleep(300);
    const value = dirtyValues.get(path);
    await evalJs(`window.monaco.editor.getModels().at(-1).setValue(${JSON.stringify(value)})`);
  }
  const saveAll = await evalJs(`
    (() => {
      const button = [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '全部保存');
      if (!button) return 'no-save-all';
      button.click();
      return 'clicked';
    })()
  `);
  if (saveAll !== 'clicked') throw new Error('全部保存不可用: ' + saveAll);
  let saveAllBusy = 'true';
  for (let i = 0; i < 30 && saveAllBusy === 'true'; i++) {
    await sleep(500);
    saveAllBusy = await evalJs(`document.querySelector('[data-testid="open-editors"]')?.getAttribute('data-saving-all')`);
  }
  if (saveAllBusy !== 'false') throw new Error('全部保存在 15 秒内未完成: ' + JSON.stringify({saveAllBusy, dirtyCount: await evalJs(`document.querySelector('[data-testid="open-editors"]')?.getAttribute('data-dirty-count')`)}));
  for (const [path, expected] of dirtyValues) {
    const content = await evalJs(`
      fetch('/api/v1/cases/1/file?path=' + encodeURIComponent(${JSON.stringify(path)}), {
        headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
      }).then(r => r.json()).then(j => j.content)
    `);
    if (content !== expected) throw new Error(`全部保存未写入 ${path}: ${content}`);
  }
  console.log('Open Editors tabs + save all: OK');
  const saveAllState = await evalJs(`
    (() => {
      const button = [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '全部保存');
      return {disabled: !!button?.disabled, html: button?.outerHTML,
        dirtyCount: document.querySelector('[data-testid="open-editors"]')?.getAttribute('data-dirty-count'),
        savingAll: document.querySelector('[data-testid="open-editors"]')?.getAttribute('data-saving-all'),
        tabs: [...document.querySelectorAll('[data-editor-path]')].map(d => ({text: d.textContent, dirty: d.getAttribute('data-dirty')}))};
    })()
  `);
  if (!saveAllState.disabled) throw new Error('全部保存后仍有未保存标记: ' + JSON.stringify(saveAllState));
  await evalJs(`document.querySelector('[data-editor-path="${editorA}"] button[title="关闭文件"]').click()`);
  await sleep(300);
  const activeAfterClose = await evalJs(`document.querySelector('[data-editor-path="${editorB}"]').getAttribute('data-active')`);
  if (activeAfterClose !== 'true') throw new Error('关闭活动文件后未切换到另一标签');
  const closeAll = await evalJs(`
    (() => {
      window.confirm = () => true;
      const button = [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '全部关闭');
      if (!button) return 'no-close-all';
      button.click();
      return 'clicked';
    })()
  `);
  if (closeAll !== 'clicked') throw new Error('全部关闭不可用: ' + closeAll);
  await sleep(300);
  const afterCloseState = await evalJs(`({
    count: document.querySelectorAll('[data-editor-path]').length,
    tabs: [...document.querySelectorAll('[data-editor-path]')].map(d => d.textContent),
    panel: document.querySelector('[data-testid="open-editors"]')?.textContent ?? ''
  })`);
  editorTabs = afterCloseState.count;
  if (editorTabs !== 0) throw new Error('全部关闭后仍有文件标签: ' + JSON.stringify(afterCloseState));

  await clickTreeRow(editorA);
  await sleep(400);
  await evalJs(`
    Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
      writeText: async text => { window.__copiedPath = text; }
    }});
    [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '复制相对路径').click();
  `);
  const copiedRelative = await evalJs(`window.__copiedPath`);
  if (copiedRelative !== editorA) throw new Error('复制相对路径错误: ' + copiedRelative);
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '复制路径').click()`);
  const copiedStorage = await evalJs(`window.__copiedPath`);
  if (copiedStorage !== `workspaces/1/cases/1/${editorA}`) throw new Error('复制存储路径错误: ' + copiedStorage);

  // 全局路径格式：设置只影响复制显示，可随时切回 POSIX
  await activateDockTab('设置');
  await sleep(400);
  const windowsSet = await evalJs(`
    (() => {
      const sel = document.querySelector('select[title="路径文本格式"]');
      if (!sel) return 'no-path-format';
      const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
      setter.call(sel, 'windows');
      sel.dispatchEvent(new Event('change', {bubbles: true}));
      return 'windows';
    })()
  `);
  if (windowsSet !== 'windows') throw new Error('路径格式选择器未找到: ' + windowsSet);
  await clickTreeRow(editorA);
  await sleep(300);
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '复制相对路径').click()`);
  const windowsPath = await evalJs(`window.__copiedPath`);
  if (windowsPath !== 'D-post\\editor-a.txt') throw new Error('Windows 路径格式错误: ' + windowsPath);
  await activateDockTab('设置');
  await evalJs(`
    (() => {
      const sel = document.querySelector('select[title="路径文本格式"]');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
      setter.call(sel, 'linux');
      sel.dispatchEvent(new Event('change', {bubbles: true}));
    })()
  `);
  await clickTreeRow(editorA);
  await sleep(300);
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '复制相对路径').click()`);
  const linuxPath = await evalJs(`window.__copiedPath`);
  if (linuxPath !== editorA) throw new Error('POSIX 路径格式恢复失败: ' + linuxPath);
  console.log('global path format windows/linux: OK');
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '加入聊天').click()`);
  await sleep(500);
  const chatPath = await evalJs(`document.querySelector('textarea[placeholder*="仿真需求"]').value`);
  if (!chatPath.includes('@' + editorA)) throw new Error('加入聊天未插入文件引用: ' + chatPath);
  console.log('Open Editors close/path/add-to-chat: OK');
  for (const path of [editorA, editorB]) {
    await evalJs(`fetch('/api/v1/cases/1/file?path=' + encodeURIComponent(${JSON.stringify(path)}), {
      method: 'DELETE', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
    })`);
  }

  // 外部修改冲突：取消不得覆盖，确认后才允许覆盖
  const conflictPath = 'D-post/editor-conflict.txt';
  await evalJs(`
    fetch('/api/v1/cases/1/file', {
      method: 'PUT', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
      body: JSON.stringify({path: ${JSON.stringify(conflictPath)}, content: 'BASE'})
    });
    document.querySelector('[data-case-name="cavity-demo"]').click();
  `);
  await sleep(700);
  await clickTreeRow(conflictPath);
  let conflictEditorReady = false;
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    conflictEditorReady = await evalJs(`window.monaco?.editor?.getModels()?.at(-1)?.getValue() === 'BASE'`);
    if (conflictEditorReady) break;
  }
  if (!conflictEditorReady) throw new Error('冲突文件 Monaco 模型未就绪');
  await evalJs(`window.monaco.editor.getModels().at(-1).setValue('EDITOR_DIRTY')`);
  await sleep(1200);
  await evalJs(`fetch('/api/v1/cases/1/file', {
    method: 'PUT', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
    body: JSON.stringify({path: ${JSON.stringify(conflictPath)}, content: 'EXTERNAL'})
  })`);
  await evalJs(`
    window.confirm = () => false;
    [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '保存 (Ctrl+S)').click();
  `);
  await sleep(700);
  const afterCancel = await evalJs(`
    fetch('/api/v1/cases/1/file?path=' + encodeURIComponent(${JSON.stringify(conflictPath)}), {
      headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
    }).then(r => r.json()).then(j => j.content)
  `);
  if (afterCancel !== 'EXTERNAL') throw new Error('取消冲突保存仍覆盖了外部修改: ' + afterCancel);
  await evalJs(`
    window.confirm = () => true;
    [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '保存 (Ctrl+S)').click();
  `);
  let afterConfirm = '';
  for (let i = 0; i < 30; i++) {
    await sleep(500);
    afterConfirm = await evalJs(`
      fetch('/api/v1/cases/1/file?path=' + encodeURIComponent(${JSON.stringify(conflictPath)}), {
        headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
      }).then(r => r.json()).then(j => j.content)
    `);
    if (afterConfirm === 'EDITOR_DIRTY') break;
  }
  if (afterConfirm !== 'EDITOR_DIRTY') throw new Error('确认冲突保存未覆盖: ' + afterConfirm);
  console.log('external edit conflict cancel/confirm: OK');
  await evalJs(`fetch('/api/v1/cases/1/file?path=' + encodeURIComponent(${JSON.stringify(conflictPath)}), {
    method: 'DELETE', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
  })`);

  // @path 行列引用：只把选中的行发送给模型，其他行不得出现在用户消息中
  const rangePath = 'D-post/editor-range.txt';
  await evalJs(`
    fetch('/api/v1/cases/1/file', {
      method: 'PUT', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
      body: JSON.stringify({path: ${JSON.stringify(rangePath)}, content: 'RANGE_LINE_1\\nRANGE_LINE_2\\nRANGE_LINE_3\\nRANGE_LINE_4'})
    });
    document.querySelector('[data-case-name="cavity-demo"]').click();
  `);
  await sleep(700);
  let rangeTreeReady = false;
  for (let i = 0; i < 20; i++) {
    rangeTreeReady = await evalJs(`!!document.querySelector('[data-path="${rangePath}"]')`);
    if (rangeTreeReady) break;
    await sleep(500);
  }
  if (!rangeTreeReady) throw new Error('范围附件文件未进入前端文件树');
  const rangeInput = await evalJs(`
    (() => {
      const input = document.querySelector('textarea[placeholder*="仿真需求"]');
      if (!input) return 'no-input';
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(input, '@D-post/editor-range.txt:2-3');
      input.dispatchEvent(new Event('input', {bubbles: true}));
      return 'ready';
    })()
  `);
  if (rangeInput !== 'ready') throw new Error('范围引用输入未就绪: ' + rangeInput);
  await evalJs(`[...document.querySelectorAll('button')].find(b => b.textContent?.trim() === '发送').click()`);
  let rangeMessage = '';
  for (let i = 0; i < 20; i++) {
    await sleep(500);
    rangeMessage = await evalJs(`document.body.innerText`);
    if (rangeMessage.includes('RANGE_LINE_2') && rangeMessage.includes('RANGE_LINE_3')) break;
  }
  if (!rangeMessage.includes('D-post/editor-range.txt:2-3') ||
      !rangeMessage.includes('RANGE_LINE_2') || !rangeMessage.includes('RANGE_LINE_3') ||
      rangeMessage.includes('RANGE_LINE_1') || rangeMessage.includes('RANGE_LINE_4')) {
    throw new Error('路径范围附件未按 2-3 行发送');
  }
  console.log('@path:2-3 attachment: OK');
  await evalJs(`fetch('/api/v1/cases/1/file?path=' + encodeURIComponent(${JSON.stringify(rangePath)}), {
    method: 'DELETE', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
  })`);

  // 外部 StandardCase：真实 ZIP 文件输入 → 导入 → 新 case 与 marker
  const importName = `ui-import-${Date.now()}`;
  const importZip = String.raw`D:\tmp\vibeflow-e2e-import.zip`;
  execFileSync("python", ["-c",
    "import zipfile\n" +
    `with zipfile.ZipFile(r'${importZip}', 'w', zipfile.ZIP_DEFLATED) as z:\n` +
    "    z.writestr('UIImport/0-caseDict/caseDict', 'workflowState A;\\ncaseMeta { title \\\"UI import\\\"; }\\n')\n" +
    "    z.writestr('UIImport/Ref/ui-marker.txt', 'UI_IMPORT_OK\\n')\n",
  ]);
  const importInput = await evalJs(`
    (() => {
      const input = document.querySelector('input[placeholder="新算例/导入名"]');
      if (!input) return 'no-import-name';
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(input, ${JSON.stringify(importName)});
      input.dispatchEvent(new Event('input', {bubbles: true}));
      return 'ready';
    })()
  `);
  if (importInput !== 'ready') throw new Error('导入名输入未就绪: ' + importInput);
  await send('DOM.enable');
  const doc = await send('DOM.getDocument');
  const fileInput = await send('DOM.querySelector', {
    nodeId: doc.root.nodeId, selector: 'input[data-testid="case-import-file"]',
  });
  if (!fileInput.nodeId) throw new Error('case-import-file 文件输入不存在');
  await send('DOM.setFileInputFiles', {nodeId: fileInput.nodeId, files: [importZip]});
  let imported = null;
  for (let i = 0; i < 60; i++) {
    imported = await evalJs(`
      (() => {
        const row = document.querySelector('[data-case-name="${importName}"]');
        const marker = document.querySelector('[data-path="Ref/ui-marker.txt"]');
        return {caseVisible: !!row, markerVisible: !!marker, body: document.body.innerText.includes('UI_IMPORT_OK'),
          error: document.querySelector('[role="alert"]')?.textContent ?? '',
          importing: document.body.innerText.includes('导入中…')};
      })()
    `);
    if (imported.error) throw new Error('UI 导入接口失败: ' + imported.error);
    if (imported.caseVisible && imported.markerVisible) break;
    await sleep(500);
  }
  if (!imported.caseVisible || !imported.markerVisible) {
    throw new Error('UI 导入未打开新 case 或 marker: ' + JSON.stringify(imported));
  }
  await clickTreeRow('Ref/ui-marker.txt');
  let importedMarkerText = '';
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    importedMarkerText = await evalJs(`window.monaco?.editor?.getModels()?.at(-1)?.getValue() ?? ''`);
    if (importedMarkerText.includes('UI_IMPORT_OK')) break;
  }
  if (!importedMarkerText.includes('UI_IMPORT_OK')) throw new Error('导入 marker 内容不正确');
  console.log('UI StandardCase ZIP import: OK', {...imported, body: true});
  const importedCaseId = await evalJs(`
    fetch('/api/v1/workspaces/1/cases', {
      headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
    }).then(r => r.json()).then(cases => cases.find(c => c.name === ${JSON.stringify(importName)})?.id ?? 0)
  `);
  if (!importedCaseId) throw new Error('导入后未找到新算例 ID');

  // files.changed 驱动文件树自动刷新（不手动 loadTree）
  await sleep(2500); // 等待导入后 currentCase 的 WS effect 完成连接和初始快照
  const watchPath = 'D-post/wp02-watch.txt';
  await evalJs(`fetch('/api/v1/cases/${importedCaseId}/file', {
    method: 'PUT', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token'), 'Content-Type': 'application/json'},
    body: JSON.stringify({path: ${JSON.stringify(watchPath)}, content: 'WATCH'})
  })`);
  let watchCreated = false;
  for (let i = 0; i < 24; i++) {
    await sleep(500);
    watchCreated = await evalJs(`!!document.querySelector('[data-path="${watchPath}"]')`);
    if (watchCreated) break;
  }
  if (!watchCreated) throw new Error('文件树未收到 created files.changed');
  await evalJs(`fetch('/api/v1/cases/${importedCaseId}/file?path=' + encodeURIComponent(${JSON.stringify(watchPath)}), {
    method: 'DELETE', headers: {Authorization: 'Bearer ' + localStorage.getItem('vf_token')}
  })`);
  let watchDeleted = false;
  for (let i = 0; i < 24; i++) {
    await sleep(500);
    watchDeleted = await evalJs(`!document.querySelector('[data-path="${watchPath}"]')`);
    if (watchDeleted) break;
  }
  if (!watchDeleted) throw new Error('文件树未收到 deleted files.changed');
  console.log('file tree files.changed refresh: OK');

  // 算例管理 UI：通过真实 prompt 复制当前 case 并自动选择副本
  const adminCopyName = `admin-ui-copy-${Date.now()}`;
  dialogAnswer = adminCopyName;
  await evalJs(`document.querySelector('button[title="复制算例"]').click()`);
  let adminCopied = false;
  let adminCopyState = null;
  for (let i = 0; i < 60; i++) {
    adminCopyState = await evalJs(`
      (() => {
        const row = document.querySelector('[data-case-name="${adminCopyName}"]');
        return {visible: !!row, selected: !!row && row.style.background !== 'transparent',
          error: document.querySelector('[role="alert"]')?.textContent ?? ''};
      })()
    `);
    if (adminCopyState.error) throw new Error('复制算例接口失败: ' + adminCopyState.error);
    adminCopied = adminCopyState.visible && adminCopyState.selected;
    if (adminCopied) break;
    await sleep(500);
  }
  if (!adminCopied) throw new Error('复制算例后未选中新副本: ' + adminCopyName + ' ' + JSON.stringify(adminCopyState));
  console.log('UI case copy with prompt: OK', adminCopyName);

  // CDP 截图在 headless 下可能拿不到合成帧；兜底：关掉 CDP 实例后复用同一 profile 起 CLI 截图实例
  ws.close();
  edge.kill();
  if (pngSize < 40000) {
    console.log("CDP 截图异常（全黑），改用 --screenshot 兜底…");
    await sleep(1500);
    await new Promise((resolve) => {
      execFile(EDGE, [
        "--headless", "--disable-gpu", "--window-size=1600,900", "--virtual-time-budget=12000",
        `--user-data-dir=${profileDir}`,
        `--screenshot=${outPrefix}-shell-cli.png`,
        "http://localhost:5173/",
      ], () => resolve());
      setTimeout(resolve, 15000);
    });
    console.log("CLI 截图已保存:", `${outPrefix}-shell-cli.png`);
  }
}

main().catch((e) => { console.error("E2E FAIL:", e.message); process.exit(1); });
