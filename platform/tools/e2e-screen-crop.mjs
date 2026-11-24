// E20：假视频源的真实浏览器屏幕框选流程，不捕获真实显示器。
import { execFile } from "node:child_process";
import http from "node:http";
import WebSocket from "ws";
const EDGE=String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`;
const PORT=9227,APP=process.env.VIBEFLOW_TEST_WEB||"http://localhost:5173/";
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const getJson=path=>new Promise((resolve,reject)=>{http.get({host:"127.0.0.1",port:PORT,path},r=>{let b="";r.on("data",c=>b+=c);r.on("end",()=>resolve(JSON.parse(b)));}).on("error",reject);});
async function main(){
 const edge=execFile(EDGE,["--headless=new","--disable-gpu",`--remote-debugging-port=${PORT}`,String.raw`--user-data-dir=D:\vibeflow_reverse\platform\tools\.edge-profile-screen-crop`,"about:blank"],()=>{});let ws;
 try{
  await sleep(2500);const page=(await getJson("/json/list")).find(t=>t.type==="page");ws=new WebSocket(page.webSocketDebuggerUrl);await new Promise(r=>ws.on("open",r));let id=0;
  const send=(method,params={})=>new Promise((resolve,reject)=>{const mid=++id;const fn=data=>{const m=JSON.parse(data);if(m.id===mid){ws.off("message",fn);m.error?reject(m.error):resolve(m.result);}};ws.on("message",fn);ws.send(JSON.stringify({id:mid,method,params}));});
  const evalJs=async expression=>{const r=await send("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails).slice(0,500));return r.result.value;};
  const waitFor=async(expression,message)=>{for(let i=0;i<150;i++){if(await evalJs(expression))return;await sleep(100);}throw new Error(message);};
  await send("Page.enable");await send("Page.navigate",{url:APP});await sleep(2500);
  await evalJs(`(async()=>{const suffix=Date.now();const headers={'Content-Type':'application/json'};const r=await (await fetch('/api/v1/auth/register',{method:'POST',headers,body:JSON.stringify({email:'crop-'+suffix+'@vibeflow.cn',name:'crop',password:'crop-pass-123'})})).json();localStorage.setItem('vf_token',r.token);const auth={...headers,Authorization:'Bearer '+r.token};const ws=await (await fetch('/api/v1/workspaces',{headers:auth})).json();const c=await (await fetch('/api/v1/workspaces/'+ws[0].id+'/cases',{method:'POST',headers:auth,body:JSON.stringify({name:'crop-case'})})).json();window.__caseName=c.name;})()`);
  await send("Page.reload",{ignoreCache:true});await sleep(2500);
  await waitFor(`!!document.querySelector('[data-case-name="crop-case"]')`,"测试 case 未加载");
  await evalJs(`document.querySelector('[data-case-name="crop-case"]').click()`);
  await waitFor(`[...document.querySelectorAll('.dv-tab')].some(t=>t.textContent?.trim()==='公式识别')`,"公式识别标签未加载");
  await evalJs(`(()=>{const el=[...document.querySelectorAll('.dv-tab')].find(t=>t.textContent?.trim()==='公式识别');const r=el.getBoundingClientRect();const x=r.x+r.width/2,y=r.y+r.height/2;for(const type of ['pointerdown','mousedown','pointerup','mouseup','click'])el.dispatchEvent(new (type.startsWith('pointer')?PointerEvent:MouseEvent)(type,{bubbles:true,clientX:x,clientY:y,button:0}));})()`);
  await waitFor(`!!document.querySelector('[data-testid="screen-capture"]')`,"屏幕框选按钮未出现");
  await evalJs(`(()=>{const canvas=document.createElement('canvas');canvas.width=400;canvas.height=300;const ctx=canvas.getContext('2d');ctx.fillStyle='white';ctx.fillRect(0,0,400,300);ctx.fillStyle='black';ctx.font='40px serif';ctx.fillText('x^2+y^2',50,150);window.__cropStream=canvas.captureStream(10);navigator.mediaDevices.getDisplayMedia=async()=>window.__cropStream;document.querySelector('[data-testid="screen-capture"]').click();})()`);
  await waitFor(`document.querySelector('video')?.videoWidth===400`,"假视频未播放");
  const rect=await evalJs(`(()=>{const r=document.querySelector('[data-testid="screen-crop-area"]').getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};})()`);
  const point=(x,y)=>({x:rect.x+x,y:rect.y+y});
  let p=point(50,60);await send("Input.dispatchMouseEvent",{type:"mousePressed",...p,button:"left",clickCount:1});
  p=point(200,180);await send("Input.dispatchMouseEvent",{type:"mouseMoved",...p,button:"left",buttons:1});
  await send("Input.dispatchMouseEvent",{type:"mouseReleased",...p,button:"left",clickCount:1});
  await waitFor(`!!document.querySelector('[data-testid="screen-crop-selection"]')`,"未生成裁剪框");
  await evalJs(`document.querySelector('[data-testid="screen-crop-apply"]').click()`);
  await waitFor(`document.querySelector('img[alt="待识别"]')?.src.startsWith('data:image/png')`,"未生成 PNG");
  const result=await evalJs(`({length:document.querySelector('img[alt="待识别"]').src.length,stopped:window.__cropStream.getTracks().every(t=>t.readyState==='ended')})`);
  if(result.length<1000||!result.stopped)throw new Error("裁剪或停止轨道失败 "+JSON.stringify(result));
  console.log("E20 click start, drag crop, PNG and stopped tracks with mock stream: OK");console.log("E20 SCREEN CROP E2E DONE");
 }finally{if(ws)ws.close();edge.kill();}
}
main().catch(e=>{console.error("E20 SCREEN CROP FAIL:",e.message);process.exit(1);});
