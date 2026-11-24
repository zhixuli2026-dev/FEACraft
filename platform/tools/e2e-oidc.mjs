// J10：真实浏览器 OIDC 授权页→callback→平台 JWT。
import { execFile } from "node:child_process";
import http from "node:http";
import crypto from "node:crypto";
import WebSocket from "ws";

const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9226;
const APP = process.env.VIBEFLOW_TEST_WEB || "http://127.0.0.1:5175/";
let challenge = "";
const provider = http.createServer((req, res) => {
  const url = new URL(req.url, "http://127.0.0.1:9127");
  const json = data => { res.writeHead(200, {"Content-Type":"application/json"}); res.end(JSON.stringify(data)); };
  if (url.pathname === "/.well-known/openid-configuration") return json({
    authorization_endpoint:"http://127.0.0.1:9127/authorize", token_endpoint:"http://127.0.0.1:9127/token",
    userinfo_endpoint:"http://127.0.0.1:9127/userinfo"});
  if (url.pathname === "/authorize") {
    challenge = url.searchParams.get("code_challenge");
    const target = `${APP}oidc/callback?code=mock-code&state=${encodeURIComponent(url.searchParams.get("state"))}`;
    res.writeHead(200, {"Content-Type":"text/html"}); res.end(`<a id="authorize" href="${target}">Authorize mock OIDC</a>`); return;
  }
  if (url.pathname === "/token" && req.method === "POST") {
    let body=""; req.on("data", c=>body+=c); req.on("end",()=>{
      const form=new URLSearchParams(body);
      const actual=crypto.createHash("sha256").update(form.get("code_verifier")).digest("base64url");
      if (actual!==challenge) { res.writeHead(400); res.end("{}"); return; }
      json({access_token:"mock-access-token",token_type:"Bearer"});
    }); return;
  }
  if (url.pathname === "/userinfo") return json({sub:"browser-oidc",email:"oidc-browser@vibeflow.cn",email_verified:true});
  res.writeHead(404); res.end();
});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const getJson = path => new Promise((resolve, reject) => {
  http.get({host:"127.0.0.1",port:PORT,path}, response => { let body=""; response.on("data",c=>body+=c); response.on("end",()=>resolve(JSON.parse(body))); }).on("error",reject);
});

async function main() {
  await new Promise(resolve=>provider.listen(9127,"127.0.0.1",resolve));
  const edge=execFile(EDGE,["--headless=new","--disable-gpu",`--remote-debugging-port=${PORT}`,
    String.raw`--user-data-dir=D:\vibeflow_reverse\platform\tools\.edge-profile-oidc`,"about:blank"],()=>{});
  let ws;
  try {
    await sleep(2500);
    const page=(await getJson("/json/list")).find(t=>t.type==="page");
    ws=new WebSocket(page.webSocketDebuggerUrl); await new Promise(r=>ws.on("open",r));
    let id=0;
    const send=(method,params={})=>new Promise((resolve,reject)=>{
      const mid=++id; const onMessage=data=>{const m=JSON.parse(data); if(m.id===mid){ws.off("message",onMessage);m.error?reject(m.error):resolve(m.result);}};
      ws.on("message",onMessage); ws.send(JSON.stringify({id:mid,method,params}));
    });
    const evalJs=async expression=>{const r=await send("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});
      if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails).slice(0,500));return r.result.value;};
    const waitFor=async(expression,message)=>{for(let i=0;i<150;i++){if(await evalJs(expression))return;await sleep(100);}throw new Error(message);};
    await send("Page.enable"); await send("Page.navigate",{url:APP}); await sleep(2500);
    await waitFor(`!!document.querySelector('[data-testid="oidc-login"]')`,"OIDC 登录按钮未出现");
    await evalJs(`document.querySelector('[data-testid="oidc-login"]').click()`);
    await waitFor(`location.hostname==='127.0.0.1' && location.port==='9127' && !!document.querySelector('#authorize')`,"mock 授权页未打开");
    await evalJs(`document.querySelector('#authorize').click()`);
    await waitFor(`!!localStorage.getItem('vf_token') && location.port!=='9127'`,"callback 未签发平台 token");
    const me=await evalJs(`(async()=>{const r=await fetch('/api/v1/auth/me',{headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')}});return r.json();})()`);
    if(me.email!=="oidc-browser@vibeflow.cn")throw new Error("OIDC 用户错误 "+JSON.stringify(me));
    console.log("J10 browser authorization redirect, PKCE callback, JWT and /auth/me: OK");
    console.log("J10 OIDC E2E DONE");
  } finally { if(ws)ws.close();edge.kill();provider.close(); }
}
main().catch(error=>{console.error("J10 OIDC E2E FAIL:",error.message);provider.close();process.exit(1);});
