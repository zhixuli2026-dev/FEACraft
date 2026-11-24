// SCNET 工作台真实浏览器验收（本机 mock，不访问真实账户）。
import { execFile } from "node:child_process";
import http from "node:http";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import WebSocket from "ws";
const EDGE=String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT=9228,APP=process.env.VIBEFLOW_TEST_WEB||"http://localhost:5173/";
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const getJson=path=>new Promise((resolve,reject)=>{http.get({host:"127.0.0.1",port:PORT,path},r=>{let b="";r.on("data",c=>b+=c);r.on("end",()=>resolve(JSON.parse(b)));}).on("error",reject);});
async function main(){
 const out=process.argv[2]||"platform/tools/shots/scnet-workbench";
 const resourceName=`scnet-ui-${Date.now()}`;
 const edge=execFile(EDGE,["--headless=new","--disable-gpu",`--remote-debugging-port=${PORT}`,String.raw`--user-data-dir=D:\vibeflow_reverse\platform\tools\.edge-profile-scnet`,"about:blank"],()=>{});let ws;
 try{
  await sleep(2500);const page=(await getJson("/json/list")).find(t=>t.type==="page");ws=new WebSocket(page.webSocketDebuggerUrl);await new Promise(r=>ws.on("open",r));let id=0;
  const send=(method,params={})=>new Promise((resolve,reject)=>{const mid=++id;const fn=data=>{const m=JSON.parse(data);if(m.id===mid){ws.off("message",fn);m.error?reject(m.error):resolve(m.result);}};ws.on("message",fn);ws.send(JSON.stringify({id:mid,method,params}));});
  const evalJs=async expression=>{const r=await send("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails).slice(0,500));return r.result.value;};
  const waitFor=async(expression,message)=>{for(let i=0;i<180;i++){if(await evalJs(expression))return;await sleep(100);}const diagnostic=await evalJs(`({status:document.querySelector('[data-testid="scnet-status"]')?.textContent,table:document.querySelector('[data-testid="scnet-table"]')?.textContent,body:document.body.innerText.slice(-500)})`);throw new Error(message+" "+JSON.stringify(diagnostic));};
  const clickText=async text=>evalJs(`(()=>{const bs=[...document.querySelectorAll('button')].filter(b=>b.textContent.trim()==='${text}');if(!bs.length)throw new Error('按钮不存在 ${text}');bs[bs.length-1].click();})()`);
  const clickFileAction=async text=>evalJs(`[...document.querySelectorAll('[data-testid="file-actions"] button')].find(b=>b.textContent==='${text}').click()`);
  const setFileForm=async(index,value)=>evalJs(`(()=>{const el=document.querySelectorAll('[data-testid="file-actions"] input')[${index}];Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  const runFileAction=async(text,operation)=>{await clickFileAction(text);await sleep(100);await waitFor(`document.querySelector('[data-testid="scnet-status"]')?.textContent.includes('${operation} 完成')`,`${operation} 未完成`);};
  const runButton=async(text,operation)=>{await clickText(text);await sleep(100);await waitFor(`document.querySelector('[data-testid="scnet-status"]')?.textContent.includes('${operation} 完成')`,`${operation} 未完成`);};
  const createResource=async resource=>{await waitFor(`document.querySelector('[data-testid="scnet-status"]')?.textContent.includes('${resource}.list 完成')`,`${resource} 初始列表未加载`);await evalJs(`(()=>{const el=document.querySelector('[data-testid="scnet-search"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,${JSON.stringify(resourceName)});el.dispatchEvent(new Event('input',{bubbles:true}));})()`);await clickText("手动创建");await sleep(100);await waitFor(`document.querySelector('[data-testid="scnet-status"]')?.textContent.includes('${resource}.list 完成')&&document.querySelector('[data-testid="scnet-table"]')?.textContent.includes('${resourceName}')`,`${resource} 创建后列表未显示资源`);};
  await send("Page.enable");await send("Page.navigate",{url:APP});await sleep(2500);
  await evalJs(`(async()=>{const r=await (await fetch('/api/v1/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:'scnet-ui-'+Date.now()+'@vibeflow.cn',name:'scnet-ui',password:'scnet-ui-pass-123'})})).json();localStorage.setItem('vf_token',r.token);})()`);
  await send("Page.reload",{ignoreCache:true});await sleep(2500);
  console.log("SCNET_PAGE_DEBUG",await evalJs(`({text:document.body.innerText.slice(0,300),tabs:[...document.querySelectorAll('.dv-tab')].map(t=>t.textContent)})`));
  await waitFor(`[...document.querySelectorAll('.dv-tab')].some(t=>t.textContent?.trim()==='SCNET')`,"SCNET 标签未加载");
  await evalJs(`(()=>{const el=[...document.querySelectorAll('.dv-tab')].find(t=>t.textContent?.trim()==='SCNET');const r=el.getBoundingClientRect();const x=r.x+r.width/2,y=r.y+r.height/2;for(const type of ['pointerdown','mousedown','pointerup','mouseup','click'])el.dispatchEvent(new (type.startsWith('pointer')?PointerEvent:MouseEvent)(type,{bubbles:true,clientX:x,clientY:y,button:0}));})()`);
  await waitFor(`!!document.querySelector('[data-testid="scnet-connect"]')`,"连接页未出现");
  await clickText("手动连接");
  await sleep(1000);
  console.log("SCNET_CONNECT_STATUS",await evalJs(`document.querySelector('[data-testid="scnet-status"]')?.textContent`));
  await waitFor(`document.querySelector('[data-testid="scnet-status"]')?.textContent.includes('jobs.list 完成')`,"连接后作业列表未加载");
  await clickText("提交 BASIC 作业");
  await sleep(1000);
  console.log("SCNET_SUBMIT_DEBUG",await evalJs(`({status:document.querySelector('[data-testid="scnet-status"]')?.textContent,requests:performance.getEntriesByType('resource').filter(r=>r.name.includes('jobs.submit')).map(r=>r.name)})`));
  await waitFor(`document.querySelector('[data-testid="scnet-table"]')?.textContent.includes('demo')`,"作业创建未显示");
  await clickText("详情");
  await sleep(1000);
  console.log("SCNET_DETAIL_DEBUG",await evalJs(`({detail:document.querySelector('[data-testid="scnet-detail"]')?.textContent,body:document.body.innerText.slice(-1200),requests:performance.getEntriesByType('resource').filter(r=>r.name.includes('jobs.detail')).map(r=>r.name)})`));
  await waitFor(`document.querySelector('[data-testid="scnet-detail"]')?.textContent.includes('running')`,"作业详情未显示");
  await clickText("关闭");
  for(let i=0;i<5;i++){
    await clickText("提交 BASIC 作业");
    await waitFor(`document.querySelector('[data-testid="scnet-status"]')?.textContent.includes('jobs.list 完成')`,"批量作业列表未刷新");
  }
  console.log("SCNET_PAGE_COUNT",await evalJs(`({table:document.querySelector('[data-testid="scnet-table"]').textContent,buttons:[...document.querySelectorAll('button')].filter(b=>b.textContent==='下一页').map(b=>({disabled:b.disabled,text:b.textContent}))})`));
  await clickText("下一页");
  await waitFor(`document.body.textContent.includes('第 2 页')`,"分页失败");
  await clickText("终止");
  await evalJs(`(()=>{const el=document.querySelector('[data-testid="scnet-job-submit"]').parentElement.querySelector('input');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,${JSON.stringify(resourceName)});el.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  console.log("1. 连接、作业提交/详情/终止/分页 [OK]");

  await evalJs(`document.querySelector('[data-testid="scnet-tab-files"]').click()`);
  await setFileForm(0,"Ref");await runFileAction("新建目录","files.mkdir");
  await setFileForm(0,"Ref/a.txt");await runFileAction("上传","files.upload");
  await runFileAction("重命名","files.rename");
  await setFileForm(0,"Ref/b.txt");await setFileForm(1,"Ref/c.txt");await runFileAction("复制","files.copy");
  await setFileForm(1,"Ref/d.txt");await runFileAction("移动","files.move");
  await setFileForm(0,"Ref/d.txt");await clickFileAction("下载");
  await waitFor(`!document.querySelector('[data-testid="scnet-status"]')?.textContent.includes('files.download…')`,"文件下载请求未结束");
  console.log("SCNET_DOWNLOAD_DEBUG",await evalJs(`({path:document.querySelectorAll('[data-testid="file-actions"] input')[0]?.value,status:document.querySelector('[data-testid="scnet-status"]')?.textContent,result:document.querySelector('[data-testid="scnet-download-result"]')?.textContent,requests:performance.getEntriesByType('resource').filter(r=>r.name.includes('files.download')).map(r=>r.name)})`));
  await waitFor(`document.querySelector('[data-testid="scnet-download-result"]')?.textContent==='ABC'`,"文件下载内容错误");
  await clickText("刷新");await waitFor(`document.querySelector('[data-testid="scnet-status"]')?.textContent.includes('files.list 完成')`,"文件列表未刷新");
  await evalJs(`(()=>{const row=[...document.querySelectorAll('[data-testid="scnet-table"] > div')].find(r=>r.querySelector('span')?.textContent==='Ref/');if(!row)throw new Error('目录行不存在');row.querySelector('button').click();})()`);
  await waitFor(`document.querySelector('[data-testid="scnet-download-result"]')?.textContent.startsWith('UEsD')`,"目录 ZIP 下载失败");
  await setFileForm(0,"Ref/d.txt");await runFileAction("删除","files.delete");
  await waitFor(`!document.querySelector('[data-testid="scnet-table"]')?.textContent.includes('Ref/d.txt')`,"文件删除后仍显示旧行");
  console.log("2. 文件上传/重命名/复制/移动/下载/删除 [OK]");

  await evalJs(`document.querySelector('[data-testid="scnet-tab-containers"]').click()`);
  await createResource("containers");
  await runButton("启动","containers.start");
  await waitFor(`[...document.querySelectorAll('[data-testid="scnet-table"] > div')].some(r=>r.textContent.includes('${resourceName}')&&r.textContent.includes('running'))`,"容器启动状态未刷新");
  await runButton("重启","containers.restart");
  await runButton("执行","containers.exec");
  await runButton("停止","containers.stop");
  await waitFor(`[...document.querySelectorAll('[data-testid="scnet-table"] > div')].some(r=>r.textContent.includes('${resourceName}')&&r.textContent.includes('stopped'))`,"容器停止状态未刷新");
  await runButton("删除","containers.delete");
  await waitFor(`![...document.querySelectorAll('[data-testid="scnet-table"] > div')].some(r=>r.querySelector('span')?.textContent==='${resourceName}')`,"容器删除后仍显示旧行");
  console.log("3. 容器生命周期与 exec [OK]");

  await evalJs(`document.querySelector('[data-testid="scnet-tab-notebooks"]').click()`);
  await createResource("notebooks");
  await runButton("开机","notebooks.start");
  await runButton("关机存环境","notebooks.stop");
  await runButton("重命名","notebooks.rename");
  await runButton("释放","notebooks.release");
  await waitFor(`![...document.querySelectorAll('[data-testid="scnet-table"] > div')].some(r=>r.querySelector('span')?.textContent==='${resourceName}')`,"Notebook 释放后仍显示旧行");
  console.log("4. Notebook 生命周期 [OK]");

  await evalJs(`document.querySelector('[data-testid="scnet-tab-desktops"]').click()`);
  await createResource("desktops");
  console.log("SCNET_DESKTOP_CREATE_OK");
  await runButton("详情","desktops.detail");
  await waitFor(`!!document.querySelector('[data-testid="scnet-detail"]')`,"桌面详情未显示");
  console.log("SCNET_DESKTOP_DETAIL_OK");
  await evalJs(`window.open=(url)=>{window.__opened=String(url);return null;};`);
  console.log("SCNET_DESKTOP_OPEN_INSTALLED");
  await runButton("打开","desktops.open");
  console.log("SCNET_DESKTOP_OPEN_OK");
  const opened=await evalJs(`String(window.__opened || "")`);
  if(!opened?.includes('/connect/desktops/'))throw new Error("桌面打开链接错误 "+opened);
  await runButton("删除","desktops.delete");
  await waitFor(`![...document.querySelectorAll('[data-testid="scnet-table"] > div')].some(r=>r.querySelector('span')?.textContent==='${resourceName}')`,"桌面删除后仍显示旧行");
  console.log("SCNET_DESKTOP_DELETE_OK");
  const leaked=await evalJs(`Object.keys(localStorage).filter(k=>/secret|scnet|access/i.test(k))`);
  if(leaked.length)throw new Error("敏感信息进入 localStorage "+JSON.stringify(leaked));
  const shot=await send("Page.captureScreenshot",{format:"png",fromSurface:true});
  mkdirSync(dirname(`${out}.png`),{recursive:true});writeFileSync(`${out}.png`,Buffer.from(shot.data,"base64"));
  console.log("5. 桌面详情/打开/删除与敏感信息边界 [OK]");
  console.log("SCNET WORKBENCH E2E DONE");
 }finally{if(ws)ws.close();edge.kill();}
}
main().catch(e=>{console.error("SCNET WORKBENCH E2E FAIL:",e.message);process.exit(1);});
