// Real Pandoc builds, frozen CSS assets and downloaded standalone HTML in Chromium.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const [reviewPath,playwrightPath,chromePath]=process.argv.slice(2);
const review=JSON.parse(fs.readFileSync(reviewPath,'utf8'));assert.equal(review.classification,'no_llm');
for(const [name,digest] of Object.entries(review.sources))assert.equal(crypto.createHash('sha256').update(fs.readFileSync(name)).digest('hex'),digest,name);
const {chromium}=require(playwrightPath);
const directory=path.join('tmp/docker-deployment-20261117','report-css-'+crypto.randomBytes(5).toString('hex'));
fs.mkdirSync(path.join(directory,'private'),{recursive:true});
const result={passed:false,checks:[],jobs:[],real_model_calls:0,mock_llm_calls:0,page_errors:[]};
const save=()=>fs.writeFileSync(path.join(directory,'result.json'),JSON.stringify(result,null,2));
const pass=name=>{result.checks.push(name);save();console.log('REPORT_CSS_PASS',name);};
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const base='http://127.0.0.1:62883';
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:chromePath});
 const context=await browser.newContext({viewport:{width:1200,height:1000}});
 const page=await context.newPage();page.on('pageerror',e=>result.page_errors.push(e.message));
 let token;
 const api=async(method,suffix,data)=>{
  const response=await context.request.fetch(base+'/api/v1'+suffix,{method,data,headers:token?{Authorization:'Bearer '+token}:{},timeout:180000});
  assert.equal(response.status(),200,method+' '+suffix+' '+(await response.text()).slice(0,300));return response.json();
 };
 try{
  const ready=Date.now()+90000;
  while(true){const r=await context.request.get(base+'/api/health');if(r.ok()&&(await r.json()).ok)break;
   assert(Date.now()<ready,'Public API did not become ready');await pause(500);}
  const account={email:'css-'+crypto.randomBytes(8).toString('hex')+'@example.com',password:crypto.randomBytes(24).toString('hex'),name:'Actual CSS acceptance'};
  fs.writeFileSync(path.join(directory,'private/account.json'),JSON.stringify(account));
  token=(await api('POST','/auth/register',account)).token;
  const ws=(await api('GET','/workspaces'))[0].id;
  const selected=await api('POST',`/workspaces/${ws}/cases`,{name:'Frozen report styles'});result.case_id=selected.id;
  const prefix=`/cases/${selected.id}`;
  const contents=new Map();
  const put=async(file,text)=>{await api('PUT',prefix+'/file',{path:file,content:text});contents.set(file,Buffer.from(text));};
  const font=fs.readFileSync('platform/web/node_modules/katex/dist/fonts/KaTeX_Main-Regular.woff2');
  const fontPath='E-report/assets/reading.woff2';
  const upload=await context.request.post(base+'/api/v1'+prefix+'/upload',{params:{path:fontPath},headers:{Authorization:'Bearer '+token},
   multipart:{file:{name:'reading.woff2',mimeType:'font/woff2',buffer:font}}});assert(upload.ok());contents.set(fontPath,font);
  const svg=color=>`<svg xmlns="http://www.w3.org/2000/svg" width="160" height="80" viewBox="0 0 160 80"><rect width="160" height="80" fill="${color}"/><path d="M15 65L70 20L145 40" fill="none" stroke="white" stroke-width="4"/></svg>`;
  await put('E-report/assets/plot.svg',svg('#165a72'));
  const main=String.raw`@import "sub/theme.css" screen;
@font-face { font-family: FrozenReport; src: url("../assets/reading.woff2") format("woff2"); }
body { max-width: 760px; margin: 40px auto; padding: 24px; background: #fff; }
h1 { font-size: 36px; }
.frozen-font { font-family: FrozenReport; font-size: 22px; }
.plot { width:160px; height:80px; background-image:url('../assets/plot.svg'); }
@media (max-width:600px) { h1 { font-size:24px; } body { margin:0; padding:18px; } }
`;
  await put('E-report/styles/main.css',main);
  await put('E-report/styles/sub/theme.css','@import "tokens.css";\n.plot { border:3px solid #203040; }');
  await put('E-report/styles/sub/tokens.css','h1 { color:#135c72; }');
  await put('E-report/styles/extra.css','.linked-note { font-weight:700; color:#355d2c; }');
  const markdown=`---
title: "冻结样式验收"
css:
  - styles/main.css
---

# 报告样式与引用资源

这是实际导出文件的排版验收，示意图不代表求解结果。

<link rel="stylesheet" href="styles/extra.css">

<p class="linked-note">来自额外样式表</p>

<p class="frozen-font">Frozen font: energy = 42</p>

<div class="plot"></div>

<style>.inline-rule { background-image:url('assets/plot.svg'); width:160px; height:80px; }</style>

<div class="inline-rule"></div>

<div class="inline-attribute" style="background-image:url('assets/plot.svg');width:160px;height:80px"></div>

| 项目 | 结果 |
|:---|---:|
| 温度 | 300 K |
`;
  await put('E-report/report.md',markdown);
  const build=async()=>{
   const started=await api('POST',prefix+'/report:build',{});result.jobs.push(started.job_id);save();
   const deadline=Date.now()+180000;let job;
   do{job=await api('GET','/jobs/'+started.job_id);if(!['queued','running'].includes(job.status))break;await pause(300);}while(Date.now()<deadline);
   fs.writeFileSync(path.join(directory,'job-'+started.job_id+'.json'),JSON.stringify(job,null,2));
   assert.equal(job.status,'succeeded');assert(job.attempt.process_stop.verified);
   return api('GET',prefix+'/report/results');
  };
  const download=async(id,ext)=>{const response=await context.request.get(base+'/api/v1'+prefix+'/report/artifact',{
   params:{publication_id:id,path:'E-report/build/report.'+ext},headers:{Authorization:'Bearer '+token}});assert(response.ok());return response.body();};
  const first=await build();const firstId=first.publication.id;result.first_publication=firstId;
  for(const [file,bytes] of contents)assert.equal(first.manifest.inputs[file]?.sha256,hash(bytes),file);
  const html=await download(firstId,'html'),docx=await download(firstId,'docx');
  fs.writeFileSync(path.join(directory,'first.html'),html);fs.writeFileSync(path.join(directory,'first.docx'),docx);
  result.first_sha256={html:hash(html),docx:hash(docx)};
  const remote=[];page.on('request',request=>{if(/^https?:/.test(request.url()))remote.push(request.url());});
  await page.goto(pathToFileURL(path.resolve(directory,'first.html')).href);
  await page.evaluate(()=>document.fonts.ready);
  const rendered=await page.evaluate(async()=>{
   const image=async selector=>{const background=getComputedStyle(document.querySelector(selector)).backgroundImage;
    const url=background.match(/^url\(["']?(.*?)["']?\)$/)?.[1];const i=new Image();i.src=url||'';await i.decode();return {width:i.naturalWidth,height:i.naturalHeight,data:url.startsWith('data:')};};
   return {heading:getComputedStyle(document.querySelector('h1:not(.title)')).color,
    headingSize:getComputedStyle(document.querySelector('h1:not(.title)')).fontSize,
    font:[...document.fonts].map(f=>({family:f.family,status:f.status})),
    linked:getComputedStyle(document.querySelector('.linked-note')).color,
    images:await Promise.all(['.plot','.inline-rule','.inline-attribute'].map(image))};
  });
  result.rendered=rendered;assert.equal(rendered.heading,'rgb(19, 92, 114)');assert.equal(rendered.headingSize,'36px');
  assert.equal(rendered.linked,'rgb(53, 93, 44)');assert(rendered.font.some(f=>f.family==='FrozenReport'&&f.status==='loaded'));
  assert(rendered.images.every(i=>i.width===160&&i.height===80&&i.data));assert.deepEqual(remote,[]);
  await page.screenshot({path:path.join(directory,'standalone-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.locator('h1:not(.title)').evaluate(h=>getComputedStyle(h).fontSize),'24px');
  await page.screenshot({path:path.join(directory,'standalone-390.png'),fullPage:true});
  pass('actual_standalone_HTML_nested_imports_font_images_link_inline_CSS_media_query_and_no_network');
  pass('all_selected_CSS_font_image_and_markdown_bytes_frozen_in_actual_manifest');

  await put('E-report/styles/sub/tokens.css','h1 { color:#7a3550; }');
  await put('E-report/assets/plot.svg',svg('#7a3550'));
  const stale=await api('GET',prefix+'/report/results');assert(stale.publication.stale);
  assert(html.equals(await download(firstId,'html')));assert(docx.equals(await download(firstId,'docx')));
  const second=await build();result.second_publication=second.publication.id;
  assert.notEqual(second.publication.id,firstId);
  for(const [file,bytes] of contents)assert.equal(second.manifest.inputs[file]?.sha256,hash(bytes),file);
  const secondHTML=await download(second.publication.id,'html');fs.writeFileSync(path.join(directory,'second.html'),secondHTML);
  await page.goto(pathToFileURL(path.resolve(directory,'second.html')).href);await page.evaluate(()=>document.fonts.ready);
  assert.equal(await page.locator('h1:not(.title)').evaluate(h=>getComputedStyle(h).color),'rgb(122, 53, 80)');
  assert(html.equals(await download(firstId,'html')));
  pass('nested_CSS_and_image_change_marks_source_stale_new_render_changes_old_download_unchanged');

  const failures=[['cycle','@import "main.css";'],['missing','h1 { background:url("absent.png"); }'],
   ['remote_import','@import "https://example.com/remote.css";'],['escaped_remote',String.raw`h1 { background: u\72l("https://example.com/image.png"); }`]];
  result.rejections=[];
  for(const [name,css] of failures){await put('E-report/styles/main.css',css);
   const response=await context.request.post(base+'/api/v1'+prefix+'/report:build',{data:{},headers:{Authorization:'Bearer '+token}});
   assert.equal(response.status(),400,name+' '+await response.text());result.rejections.push({name,status:response.status()});
   assert.equal((await api('GET',prefix+'/jobs')).length,2);assert(secondHTML.equals(await download(second.publication.id,'html')));
  }
  await put('E-report/styles/main.css',main);
  pass('actual_CSS_cycle_missing_resource_remote_import_and_escaped_remote_URL_rejected_old_publication_preserved');
  assert.equal(result.page_errors.length,0);result.passed=true;save();console.log(JSON.stringify({passed:true,evidence:directory,checks:result.checks.length}));
 }catch(error){result.failure={name:error.name,message:error.message};save();console.error(error.stack);process.exitCode=1;}
 finally{await browser.close();save();}
})();
