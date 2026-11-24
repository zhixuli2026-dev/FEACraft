// J05：组织邀请、接受、角色和禁用的真实浏览器管理台验证。
import { execFile } from "node:child_process";
import http from "node:http";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9225;
const APP_URL = process.env.VIBEFLOW_TEST_WEB || "http://localhost:5173/";
const profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-org`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const getJson = path => new Promise((resolve, reject) => {
  http.get({host:"127.0.0.1", port:PORT, path}, response => {
    let body = ""; response.on("data", chunk => body += chunk);
    response.on("end", () => resolve(JSON.parse(body)));
  }).on("error", reject);
});

async function main() {
  const args = process.argv.slice(2);
  const outPrefix = args.find(arg => arg !== "--members-only") || "shots/j05-org";
  const membersOnly = args.includes("--members-only");
  const edge = execFile(EDGE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profileDir}`, "--window-size=1600,900", "about:blank"], () => {});
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
      if (result.exceptionDetails) throw new Error("页面 JS 异常: " + JSON.stringify(result.exceptionDetails).slice(0, 600));
      return result.result.value;
    };
    const waitFor = async (expression, message, attempts = 200) => {
      for (let i = 0; i < attempts; i++) {
        if (await evalJs(expression)) return;
        await sleep(100);
      }
      throw new Error(message);
    };
    await send("Page.enable");
    await send("Page.navigate", {url:APP_URL});
    await sleep(2500);
    const fixture = await evalJs(`(async () => {
      const suffix = Date.now();
      const register = async label => {
        const response = await (await fetch('/api/v1/auth/register', {method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({email:'org-ui-'+label+'-'+suffix+'@vibeflow.cn', name:label, password:'org-ui-pass-123'})})).json();
        if (!response.token) throw new Error(JSON.stringify(response));
        return response;
      };
      const admin = await register('admin');
      const member = await register('member');
      localStorage.setItem('vf_token', admin.token);
      const mine = await (await fetch('/api/v1/org/mine', {headers:{Authorization:'Bearer '+admin.token}})).json();
      const workspaces = await (await fetch('/api/v1/workspaces', {headers:{Authorization:'Bearer '+admin.token}})).json();
      return {adminToken:admin.token, memberToken:member.token, memberId:member.user.id,
        memberEmail:member.user.email, orgId:mine.org.id, adminWorkspaceId:workspaces[0].id};
    })()`);
    await send("Page.reload", {ignoreCache:true});
    await waitFor(`!!document.querySelector('[data-testid="org-admin"]')`, "组织管理台未加载");
    await waitFor(`document.querySelector('[data-testid="billing-current"]')?.textContent === 'free' &&
      ['free', 'pro', 'team'].every(code => document.querySelector('[data-testid="plan-' + code + '"]'))`,
      "个人组织的免费订阅或套餐目录未显示");
    console.log("J04. 免费订阅与套餐目录 [OK]");
    if (!membersOnly) {
      await waitFor(`document.querySelector('[data-testid="runtime-monitor"]')?.textContent.includes('数据库 ✓')`, "运行时监控未加载");
      console.log("0. 数据库/存储/作业运行时监控 [OK]");
    }
    await evalJs(`(() => {
      const input = document.querySelector('[data-testid="org-invite-email"]');
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, '${fixture.memberEmail}');
      input.dispatchEvent(new Event('input', {bubbles:true}));
      document.querySelector('[data-testid="org-invite-submit"]').click();
    })()`);
    await waitFor(`document.querySelector('[data-testid="org-member-${fixture.memberId}"]')?.textContent.includes('invited')`, "邀请未显示");
    await waitFor(`document.querySelector('[data-testid="org-seats"]')?.textContent.includes('2 / 2')`, "邀请未预留 seat");
    console.log("1. admin 邀请与 seat 预留 [OK]");

    await evalJs(`localStorage.setItem('vf_token', '${fixture.memberToken}')`);
    await send("Page.reload", {ignoreCache:true});
    await waitFor(`!!document.querySelector('[data-testid="org-accept-${fixture.orgId}"]')`, "被邀请人未看到邀请");
    await evalJs(`document.querySelector('[data-testid="org-accept-${fixture.orgId}"]').click()`);
    await waitFor(`!document.querySelector('[data-testid="org-accept-${fixture.orgId}"]')`, "接受后邀请仍显示");
    console.log("2. 被邀请人接受邀请 [OK]");

    await evalJs(`localStorage.setItem('vf_token', '${fixture.adminToken}')`);
    await send("Page.reload", {ignoreCache:true});
    await waitFor(`document.querySelector('[data-testid="org-member-${fixture.memberId}"]')?.textContent.includes('member')`, "admin 未看到 active 成员");
    const setRole = async role => {
      await evalJs(`(() => {
        const select = document.querySelector('[aria-label="成员角色 ${fixture.memberId}"]');
        Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, '${role}');
        select.dispatchEvent(new Event('change', {bubbles:true}));
      })()`);
      await waitFor(`document.querySelector('[aria-label="成员角色 ${fixture.memberId}"]')?.value === '${role}'`, "角色未变为 " + role);
    };
    await setRole('viewer');
    await setRole('disabled');
    console.log("3. viewer 角色、禁用与 seat 释放 [OK]");
    await waitFor(`document.querySelector('[data-testid="org-seats"]')?.textContent.includes('1 / 2')`, "禁用未释放 seat");
    await evalJs(`document.querySelector('[data-testid="org-audit-load"]').click()`);
    await waitFor(`document.querySelector('[data-testid="org-audit"]')?.textContent.includes('哈希链有效')`, "审计链状态未显示");
    await waitFor(`[...document.querySelectorAll('[data-testid="org-audit-event"]')].some(e => e.textContent.includes('org.member.role_changed'))`, "角色变更审计未显示");
    console.log("4. 审计中心哈希链与角色变更事件 [OK]");
    if (!membersOnly) {
      await evalJs(`document.querySelector('[data-testid="plan-pro"]').click()`);
      await waitFor(`document.querySelector('[data-testid="billing-current"]')?.textContent === 'pro'`, "mock pro 切换未生效");
      const beforeRenew = await evalJs(`document.querySelector('[data-testid="billing-console"]').textContent`);
      await evalJs(`document.querySelector('[data-testid="billing-renew"]').click()`);
      await waitFor(`document.querySelector('[data-testid="billing-console"]').textContent !== ${JSON.stringify(beforeRenew)}`, "续费后到期时间未更新");
      await evalJs(`document.querySelector('[data-testid="billing-cancel"]').click()`);
      await waitFor(`document.querySelector('[data-testid="billing-current"]')?.textContent === '无生效订阅'`, "取消订阅未生效");
      console.log("5. mock 套餐切换、续费与取消 [OK]");
    }
    await evalJs(`document.querySelector('[data-testid="org-audit"]').scrollIntoView({block:'center'})`);
    await sleep(200);
    const shot = await send("Page.captureScreenshot", {format:"png", fromSurface:true});
    mkdirSync(dirname(`${outPrefix}.png`), {recursive:true});
    writeFileSync(`${outPrefix}.png`, Buffer.from(shot.data, "base64"));
    if (membersOnly) {
      console.log("J05 ORG ADMIN E2E DONE");
      return;
    }
    await evalJs(`fetch('/api/v1/org/${fixture.orgId}/members/${fixture.memberId}', {method:'PATCH',headers:{Authorization:'Bearer '+localStorage.getItem('vf_token'),'Content-Type':'application/json'},body:JSON.stringify({role:'member'})})`);
    await evalJs(`window.prompt = () => 'member'; document.querySelector('[data-testid="workspace-share"]').click()`);
    await waitFor(`(async()=>{const r=await fetch('/api/v1/workspaces/${fixture.adminWorkspaceId}/sharing',{headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')}});const d=await r.json();return d.enabled&&d.role==='member';})()`, "工作区共享未保存");
    await evalJs(`localStorage.setItem('vf_token', '${fixture.memberToken}')`);
    await send("Page.reload", {ignoreCache:true});
    await sleep(2500);
    const shared = await evalJs(`(async()=>{const r=await fetch('/api/v1/workspaces',{headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')}});const rows=await r.json();return rows.find(w=>w.id===${fixture.adminWorkspaceId});})()`);
    if (!shared?.shared || shared.access_role !== 'member') throw new Error("成员未看到共享工作区 " + JSON.stringify(shared));
    console.log("6. owner 分享与成员共享列表 [OK]");
    console.log("J05/J11 ORG ADMIN E2E DONE");
  } finally {
    if (ws) ws.close();
    edge.kill();
  }
}

main().catch(error => { console.error("J05 ORG ADMIN E2E FAIL:", error.message); process.exit(1); });
