import { execFile } from "node:child_process";
import { writeFileSync } from "node:fs";
import http from "node:http";
import WebSocket from "ws";
const EDGE = String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT = 9260, profileDir = String.raw`D:\vibeflow_reverse\platform\tools\.edge-profile-msh-preview`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const getJson = path => new Promise((resolve, reject) => {
  http.get({host:"127.0.0.1",port:PORT,path}, res => { let body=""; res.on("data",c=>body+=c); res.on("end",()=>resolve(JSON.parse(body))); }).on("error",reject);
});
const edge = execFile(EDGE,["--headless=new","--disable-gpu",`--remote-debugging-port=${PORT}`,`--user-data-dir=${profileDir}`,"--window-size=1500,900","about:blank"],()=>{});
let targets; for(let i=0;i<40;i++){try{targets=await getJson("/json/list");break;}catch{await sleep(250);}}
if(!targets) throw new Error("Edge CDP 未就绪");
const ws = new WebSocket(targets.find(t=>t.type==="page").webSocketDebuggerUrl); await new Promise(r=>ws.on("open",r));
let id=0;
const send=(method,params={})=>new Promise((resolve,reject)=>{const mid=++id;const onMessage=data=>{const m=JSON.parse(data);if(m.id===mid){ws.off("message",onMessage);m.error?reject(new Error(JSON.stringify(m.error))):resolve(m.result);}};ws.on("message",onMessage);ws.send(JSON.stringify({id:mid,method,params}));});
const evalJs=async expression=>{const r=await send("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails).slice(0,800));return r.result.value;};
const waitFor=async(expression,timeout=30000)=>{const start=Date.now();while(Date.now()-start<timeout){if(await evalJs(expression))return;await sleep(150);}throw new Error("等待超时: "+expression);};
const api=(path,options={})=>evalJs(`fetch(${JSON.stringify(path)},{headers:{Authorization:'Bearer '+localStorage.getItem('vf_token'),'Content-Type':'application/json'},${options.method?`method:${JSON.stringify(options.method)},`:""}${options.body?`body:JSON.stringify(${JSON.stringify(options.body)}),`:""}}).then(async r=>({status:r.status,body:await r.json().catch(()=>({}))}))`);
try{
  await send("Page.enable"); await send("Page.navigate",{url:"http://localhost:5173/"});
  await waitFor(`document.readyState==='complete'&&(!!document.querySelector('form')||!!document.querySelector('[data-case-name]'))`);
  const email=`msh-ui-${Date.now()}@vibeflow.cn`;
  const registered=await evalJs(`fetch('/api/v1/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:'${email}',name:'msh-ui',password:'msh-ui-pass-123'})}).then(async r=>({status:r.status,body:await r.json()}))`);
  if(registered.status!==200)throw new Error("独立用户注册失败"); await evalJs(`localStorage.setItem('vf_token',${JSON.stringify(registered.body.token)})`);
  const workspaces=await api("/api/v1/workspaces"),workspace=workspaces.body[0],caseName=`msh-ui-${Date.now()}`;
  const created=await api(`/api/v1/workspaces/${workspace.id}/cases`,{method:"POST",body:{name:caseName}}),caseId=created.body.id;
  const gmsh='$MeshFormat\n2.2 0 8\n$EndMeshFormat\n$Nodes\n4\n1 0 0 0\n2 1 0 0\n3 0 1 0\n4 0 0 1\n$EndNodes\n$Elements\n1\n1 4 0 1 2 3 4\n$EndElements\n';
  const written=await api(`/api/v1/cases/${caseId}/file`,{method:"PUT",body:{path:"B-mesh/tetra.msh",content:gmsh}}); if(written.status!==200)throw new Error("Gmsh 写入失败");
  await send("Page.navigate",{url:"http://localhost:5173/"}); await waitFor(`!!document.querySelector('[data-case-name="${caseName}"]')`);
  await evalJs(`document.querySelector('[data-case-name="${caseName}"]').click()`); await waitFor(`!!document.querySelector('[data-path="B-mesh"]')`);
  if(!await evalJs(`!!document.querySelector('[data-path="B-mesh/tetra.msh"]')`))await evalJs(`document.querySelector('[data-path="B-mesh"]').click()`);
  await waitFor(`!!document.querySelector('[data-path="B-mesh/tetra.msh"]')`); await evalJs(`document.querySelector('[data-path="B-mesh/tetra.msh"]').click()`);
  await waitFor(`(async()=>{const r=await fetch('/api/v1/cases/${caseId}/viz:sessions',{headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')}});const items=await r.json();return items.some(x=>x.alive&&x.file_path?.endsWith('/preview.vtu'));})()`);
  const sessions=await api(`/api/v1/cases/${caseId}/viz:sessions`),session=sessions.body.find(x=>x.alive&&x.file_path?.endsWith('/preview.vtu'));
  await waitFor(`[...document.querySelectorAll('iframe')].some(frame => { try { const doc=frame.contentDocument; return doc?.querySelector('canvas') && doc.body.innerHTML.length>1000; } catch { return false; } })`);
  await sleep(1000);
  const shot=await send("Page.captureScreenshot",{format:"png"});writeFileSync(new URL("../docs/shots/a03-msh-preview.png",import.meta.url),Buffer.from(shot.data,"base64"));
  await api(`/api/v1/viz:sessions/${session.id}`,{method:"DELETE"});
  console.log("Tree Gmsh click converts VTU and starts live viz session: OK",{caseId});
}catch(error){const debug=await evalJs(`document.body.textContent.slice(-1000)`).catch(()=>"");console.error("MSH PREVIEW E2E FAIL:",error.message,debug);process.exitCode=1;}finally{ws.close();edge.kill();}
