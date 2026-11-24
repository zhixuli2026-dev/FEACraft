import { execFile } from "node:child_process";
import { cpSync, writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";
const EDGE=String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`,PORT=9261,profileDir=String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-udf-workflow`;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const getJson=path=>new Promise((resolve,reject)=>{http.get({host:"127.0.0.1",port:PORT,path},res=>{let body="";res.on("data",c=>body+=c);res.on("end",()=>resolve(JSON.parse(body)));}).on("error",reject);});
const edge=execFile(EDGE,["--headless=new","--disable-gpu",`--remote-debugging-port=${PORT}`,`--user-data-dir=${profileDir}`,"--window-size=1500,900","about:blank"],()=>{});
let targets;for(let i=0;i<40;i++){try{targets=await getJson("/json/list");break;}catch{await sleep(250);}}
if(!targets)throw new Error("Edge CDP 未就绪");
const ws=new WebSocket(targets.find(t=>t.type==="page").webSocketDebuggerUrl);await new Promise(r=>ws.on("open",r));
let id=0;const send=(method,params={})=>new Promise((resolve,reject)=>{const mid=++id;const onMessage=data=>{const m=JSON.parse(data);if(m.id===mid){ws.off("message",onMessage);m.error?reject(new Error(JSON.stringify(m.error))):resolve(m.result);}};ws.on("message",onMessage);ws.send(JSON.stringify({id:mid,method,params}));});
const evalJs=async expression=>{const r=await send("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails).slice(0,800));return r.result.value;};
const waitFor=async(expression,timeout=240000)=>{const start=Date.now();while(Date.now()-start<timeout){if(await evalJs(expression))return;await sleep(500);}throw new Error("等待超时: "+expression);};
const api=(path,options={})=>evalJs(`fetch(${JSON.stringify(path)},{headers:{Authorization:'Bearer '+localStorage.getItem('vf_token'),'Content-Type':'application/json'},${options.method?`method:${JSON.stringify(options.method)},`:""}${options.body?`body:JSON.stringify(${JSON.stringify(options.body)}),`:""}}).then(async r=>({status:r.status,body:await r.json().catch(()=>({}))}))`);
try{
  await send("Page.enable");await send("Page.navigate",{url:"http://localhost:5173/"});await waitFor(`document.readyState==='complete'&&(!!document.querySelector('form')||!!document.querySelector('[data-case-name]'))`);
  const email=`udf-ui-${Date.now()}@vibeflow.cn`,registered=await evalJs(`fetch('/api/v1/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:'${email}',name:'udf-ui',password:'udf-ui-pass-123'})}).then(async r=>({status:r.status,body:await r.json()}))`);
  if(registered.status!==200)throw new Error("独立用户注册失败");await evalJs(`localStorage.setItem('vf_token',${JSON.stringify(registered.body.token)})`);
  const userId=registered.body.user.id,workspaces=await api("/api/v1/workspaces"),workspace=workspaces.body[0],caseName=`udf-ui-${Date.now()}`;
  const created=await api(`/api/v1/workspaces/${workspace.id}/cases`,{method:"POST",body:{name:caseName}}),caseId=created.body.id;
  const source=String.raw`D:\vibeflow_reverse\platform\server\data\files\workspaces\1\cases\1\C-run\case-1`,target=`D:/vibeflow_reverse/platform/server/data/files/workspaces/${userId}/cases/${caseId}/C-run/case-udf`;
  for(const name of ["system","constant","0"])cpSync(`${source}/${name}`,`${target}/${name}`,{recursive:true});
  await send("Page.navigate",{url:"http://localhost:5173/"});await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`);
  await waitFor(`[...document.querySelectorAll('.dv-tab')].some(t=>t.textContent?.trim()==='caseDict')`);
  await evalJs(`(()=>{const tab=[...document.querySelectorAll('.dv-tab')].find(t=>t.textContent?.trim()==='caseDict');const r=tab.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;for(const type of ['pointerdown','mousedown','pointerup','mouseup','click'])tab.dispatchEvent(new(type.startsWith('pointer')?PointerEvent:MouseEvent)(type,{bubbles:true,clientX:x,clientY:y,button:0}));})()`);
  await waitFor(`!!document.querySelector('[data-testid="udf-section"]')`);
  await evalJs(`document.querySelector('[data-testid="udf-scaffold"]').click()`);await waitFor(`document.querySelector('[data-testid="udf-stage"]').textContent==='D1–D3'`);
  await evalJs(`document.querySelector('[data-testid="udf-build"]').click()`);await waitFor(`document.querySelector('[data-testid="udf-stage"]').textContent==='D4'`);
  await evalJs(`document.querySelector('[data-testid="udf-integrate"]').click()`);await waitFor(`document.querySelector('[data-testid="udf-stage"]').textContent==='D5'`);
  await evalJs(`document.querySelector('[data-testid="udf-section"]').scrollIntoView({block:'center'})`);
  const shot=await send("Page.captureScreenshot",{format:"png"});writeFileSync(new URL("../docs/shots/a10-udf-d5.png",import.meta.url),Buffer.from(shot.data,"base64"));
  console.log("CasePanel UDF D1-D5 scaffold/wmake/CDLL/icoFoam: OK",{caseId});
}catch(error){const debug=await evalJs(`document.querySelector('[data-testid="udf-section"]')?.textContent||document.body.textContent.slice(-900)`).catch(()=>"");console.error("UDF WORKFLOW E2E FAIL:",error.message,debug);process.exitCode=1;}finally{ws.close();edge.kill();}
