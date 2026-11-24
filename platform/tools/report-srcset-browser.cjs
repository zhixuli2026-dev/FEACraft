// Real report generation, image selection, frozen downloads and actual UI.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),zlib=require('node:zlib'),assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const [reviewPath,playwrightPath,chromePath]=process.argv.slice(2);
const review=JSON.parse(fs.readFileSync(reviewPath,'utf8'));assert.equal(review.classification,'no_llm');
for(const [file,digest] of Object.entries(review.sources))assert.equal(crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'),digest,file);
const {chromium}=require(playwrightPath),base='http://127.0.0.1:62883';
const directory=path.join('tmp/docker-deployment-20261117','srcset-'+crypto.randomBytes(5).toString('hex'));
fs.mkdirSync(path.join(directory,'private'),{recursive:true});
const result={passed:false,checks:[],jobs:[],real_model_calls:0,mock_llm_calls:0,page_errors:[]};
const save=()=>fs.writeFileSync(path.join(directory,'result.json'),JSON.stringify(result,null,2));
const pass=name=>{result.checks.push(name);save();console.log('SRCSET_PASS',name);};
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
function png(width,height,color){
 const crc=bytes=>{let c=0xffffffff;for(const b of bytes){c^=b;for(let i=0;i<8;i++)c=(c>>>1)^((c&1)?0xedb88320:0);}return(c^0xffffffff)>>>0;};
 const chunk=(name,data)=>{const type=Buffer.from(name),size=Buffer.alloc(4),checksum=Buffer.alloc(4);size.writeUInt32BE(data.length);checksum.writeUInt32BE(crc(Buffer.concat([type,data])));return Buffer.concat([size,type,data,checksum]);};
 const header=Buffer.alloc(13);header.writeUInt32BE(width,0);header.writeUInt32BE(height,4);header[8]=8;header[9]=2;
 const rows=Buffer.alloc(height*(width*3+1));for(let y=0;y<height;y++)for(let x=0;x<width;x++)for(let c=0;c<3;c++)rows[y*(width*3+1)+1+x*3+c]=color[c];
 return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',header),chunk('IDAT',zlib.deflateSync(rows)),chunk('IEND',Buffer.alloc(0))]);
}
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:chromePath});
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();
 page.on('pageerror',e=>result.page_errors.push(e.message));let token;
 const api=async(method,suffix,data)=>{const response=await context.request.fetch(base+'/api/v1'+suffix,{method,data,headers:token?{Authorization:'Bearer '+token}:{},timeout:180000});assert.equal(response.status(),200,method+' '+suffix+' '+(await response.text()).slice(0,200));return response.json();};
 try{
  const account={email:'srcset-'+crypto.randomBytes(6).toString('hex')+'@example.com',password:crypto.randomBytes(24).toString('hex'),name:'Responsive report acceptance'};
  fs.writeFileSync(path.join(directory,'private/account.json'),JSON.stringify(account));token=(await api('POST','/auth/register',account)).token;
  const workspace=(await api('GET','/workspaces'))[0];const selected=await api('POST',`/workspaces/${workspace.id}/cases`,{name:'Responsive report '+crypto.randomBytes(4).toString('hex')});result.case_id=selected.id;
  const prefix=`/cases/${selected.id}`,assets=new Map();
  const upload=async(name,bytes)=>{const response=await context.request.post(base+'/api/v1'+prefix+'/upload',{params:{path:'E-report/'+name},headers:{Authorization:'Bearer '+token},multipart:{file:{name,mimeType:'image/png',buffer:bytes}}});assert(response.ok());assets.set('E-report/'+name,bytes);};
  const red=png(160,80,[190,30,40]),blue=png(320,160,[20,70,190]),green=png(160,80,[30,150,60]),purple=png(320,160,[130,40,170]);
  await upload('image,small.png',red);await upload('wide image.png',blue);await upload('mobile.png',green);await upload('mobile2.png',purple);
  const inline='data:image/png;base64,'+red.toString('base64');
  const markdown=`# Responsive frozen images

<img id="density" src="image,small.png" srcset="image,small.png 1x, wide%20image.png 2x" width="160" alt="density">

<img id="widths" src="image,small.png" srcset="image,small.png 160w, wide%20image.png 320w" sizes="(max-width:600px) 160px, 320px" width="160" alt="widths">

<picture><source media="(max-width:600px)" srcset="mobile.png 1x, mobile2.png 2x"><img id="art" src="wide%20image.png" width="160" alt="art"></picture>

<img id="inline" src="image,small.png" srcset="${inline} 1x, wide%20image.png 2x" width="160" alt="inline">
`;
  const put=content=>api('PUT',prefix+'/file',{path:'E-report/report.md',content});await put(markdown);
  const ordinary=markdown.replace('# Responsive frozen images','<h1>Ordinary responsive images</h1>')
   +'\n<img id="blocked" src="image,small.png" srcset="image,small.png 1x, https://example.invalid/never-load.png 2x" width="160" alt="blocked">';
  await api('PUT',prefix+'/file',{path:'E-report/ordinary.html',content:ordinary});
  const build=async()=>{const submitted=await api('POST',prefix+'/report:build',{});result.jobs.push(submitted.job_id);save();let job;const deadline=Date.now()+180000;
   do{job=await api('GET','/jobs/'+submitted.job_id);if(!['queued','running'].includes(job.status))break;await new Promise(resolve=>setTimeout(resolve,300));}while(Date.now()<deadline);
   fs.writeFileSync(path.join(directory,'job-'+submitted.job_id+'.json'),JSON.stringify(job,null,2));assert.equal(job.status,'succeeded');assert(job.attempt.process_stop.verified);return api('GET',prefix+'/report/results');};
  const download=async(id,ext)=>{const response=await context.request.get(base+'/api/v1'+prefix+'/report/artifact',{params:{publication_id:id,path:'E-report/build/report.'+ext},headers:{Authorization:'Bearer '+token}});assert(response.ok());return response.body();};
  const first=await build();result.first_publication=first.publication.id;
  for(const [file,bytes] of assets)assert.equal(first.manifest.inputs[file]?.sha256,hash(bytes),file);
  const html=await download(first.publication.id,'html'),docx=await download(first.publication.id,'docx');
  fs.writeFileSync(path.join(directory,'first.html'),html);fs.writeFileSync(path.join(directory,'first.docx'),docx);result.original_sha256={html:hash(html),docx:hash(docx)};
  const inspect=async page=>page.evaluate(async()=>{
   const read=async id=>{const img=document.getElementById(id);await img.decode();const canvas=document.createElement('canvas');canvas.width=canvas.height=1;const ctx=canvas.getContext('2d');ctx.drawImage(img,0,0,1,1);return {pixel:[...ctx.getImageData(0,0,1,1).data].slice(0,3),data:img.currentSrc.startsWith('data:')};};
   return {density:await read('density'),widths:await read('widths'),art:await read('art'),inline:await read('inline'),
    descriptors:[...document.querySelectorAll('[srcset]')].map(i=>i.getAttribute('srcset').match(/ [0-9]+[wx]/g)),
    sizes:document.getElementById('widths').getAttribute('sizes')};
  });
  result.rendered=[];
  for(const [width,dpr] of [[1000,1],[390,1],[390,2],[1000,2]]){
   const isolated=await browser.newContext({viewport:{width,height:950},deviceScaleFactor:dpr});const p=await isolated.newPage(),network=[];
   p.on('request',r=>{if(/^https?:/.test(r.url()))network.push(r.url());});
   await p.goto(pathToFileURL(path.resolve(directory,'first.html')).href);const actual=await inspect(p);result.rendered.push({width,dpr,actual});save();
   const oneOf=(pixel,colors)=>assert(colors.some(color=>JSON.stringify(pixel)===JSON.stringify(color)));
   // HTML permits preferring an already available larger candidate. Embedded
   // data has no transfer cost; require valid choices, not a forced low-DPR URL.
   oneOf(actual.density.pixel,dpr===1?[[190,30,40],[20,70,190]]:[[20,70,190]]);assert.deepEqual(actual.inline.pixel,actual.density.pixel);
   oneOf(actual.widths.pixel,width===390&&dpr===1?[[190,30,40],[20,70,190]]:[[20,70,190]]);
   oneOf(actual.art.pixel,width===390?(dpr===1?[[30,150,60],[130,40,170]]:[[130,40,170]]):[[20,70,190]]);
   assert.deepEqual(actual.descriptors,[[' 1x',' 2x'],[' 160w',' 320w'],[' 1x',' 2x'],[' 1x',' 2x']]);assert.equal(actual.sizes,'(max-width:600px) 160px, 320px');
   assert(['density','widths','art','inline'].every(id=>actual[id].data));assert.deepEqual(network,[]);await p.screenshot({path:path.join(directory,`standalone-${width}-${dpr}.png`),fullPage:true});await isolated.close();
  }
  pass('actual_standalone_width_density_picture_inline_data_comma_filename_at_four_viewport_DPR_combinations_no_network');
  await page.goto(base);await page.getByRole('textbox',{name:'邮箱',exact:true}).fill(account.email);await page.getByRole('textbox',{name:'密码',exact:true}).fill(account.password);await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.locator(`[data-case-name="${selected.name}"]`).click();await page.getByText('caseDict',{exact:true}).last().click();
  await page.getByTestId('report-build-control').getByRole('button',{name:'report.html',exact:true}).click();
  await page.getByTestId('document-shadow').locator('img#density').waitFor();
  const ui=await page.getByTestId('document-shadow').evaluate(async host=>{const imgs=[...host.shadowRoot.querySelectorAll('img')];await Promise.all(imgs.map(i=>i.decode()));return {count:imgs.length,selected:imgs.map(i=>i.currentSrc),sets:[...host.shadowRoot.querySelectorAll('[srcset]')].map(i=>i.getAttribute('srcset'))};});
  result.ui=ui;assert.equal(ui.count,4);assert(ui.selected.every(url=>url.startsWith('data:')));assert.equal(ui.sets.length,4);
  pass('actual_published_reader_preserves_embedded_candidates_and_picture_without_live_file_substitution');
  const remoteRequests=[],rawRequests=[],browserToken=await page.evaluate(()=>localStorage.getItem('vf_token'));
  const observe=request=>{const u=new URL(request.url());if(u.hostname==='example.invalid')remoteRequests.push(request.url());
   if(u.pathname===`/api/v1/cases/${selected.id}/raw`&&u.searchParams.get('path')?.endsWith('.png'))rawRequests.push({path:u.searchParams.get('path'),authenticated:request.headers().authorization==='Bearer '+browserToken});};
  page.on('request',observe);await page.locator('[data-path="E-report"][data-dir="true"]').waitFor();
  if(!await page.locator('[data-path="E-report/ordinary.html"]').count())await page.locator('[data-path="E-report"][data-dir="true"]').click();
  await page.locator('[data-path="E-report/ordinary.html"][data-dir="false"]').click();
  await page.getByTestId('document-shadow').locator('img#blocked').waitFor();
  const ordinaryUI=await page.getByTestId('document-shadow').evaluate(async host=>{const imgs=[...host.shadowRoot.querySelectorAll('img')];await Promise.all(imgs.map(i=>i.decode()));return {count:imgs.length,selected:imgs.map(i=>i.currentSrc),blockedSet:host.shadowRoot.getElementById('blocked').hasAttribute('srcset')};});
  page.off('request',observe);assert.equal(ordinaryUI.count,5);assert(ordinaryUI.selected.every(url=>url.startsWith('blob:')||url.startsWith('data:')));assert.equal(ordinaryUI.blockedSet,false);
  assert.deepEqual(remoteRequests,[]);assert(rawRequests.length>=4&&rawRequests.every(r=>r.authenticated));result.ordinary={images:ordinaryUI.count,authenticated_asset_reads:rawRequests.length,remote_requests:0};
  pass('actual_workspace_candidates_use_Bearer_Blob_reads_and_remote_candidate_never_requests_network');
  await page.getByText('caseDict',{exact:true}).last().click();await page.getByTestId('report-build-control').getByRole('button',{name:'report.docx',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[data-testid="document-reader"]')?.getAttribute('data-kind')==='docx'&&document.querySelector('[data-testid="document-shadow"]')?.shadowRoot?.querySelector('img'));
  const fallback=await page.getByTestId('document-shadow').evaluate(async host=>{const imgs=[...host.shadowRoot.querySelectorAll('img')];await Promise.all(imgs.map(i=>i.decode()));return imgs.map(i=>{const canvas=document.createElement('canvas');canvas.width=canvas.height=1;const ctx=canvas.getContext('2d');ctx.drawImage(i,0,0,1,1);return [...ctx.getImageData(0,0,1,1).data].slice(0,3);});});
  assert.deepEqual(fallback,[[190,30,40],[190,30,40],[20,70,190],[190,30,40]]);result.docx_fallback=fallback;
  await page.screenshot({path:path.join(directory,'docx-fallback.png')});
  pass('actual_DOCX_contains_all_four_static_fallback_images_including_picture');
  await upload('wide image.png',png(320,160,[230,120,20]));assert((await api('GET',prefix+'/report/results')).publication.stale);
  assert(html.equals(await download(first.publication.id,'html')));assert(docx.equals(await download(first.publication.id,'docx')));
  const second=await build();result.second_publication=second.publication.id;assert.notEqual(second.publication.id,first.publication.id);
  const secondHTML=await download(second.publication.id,'html');fs.writeFileSync(path.join(directory,'second.html'),secondHTML);
  const updatedContext=await browser.newContext({viewport:{width:1000,height:950},deviceScaleFactor:2});const updated=await updatedContext.newPage();await updated.goto(pathToFileURL(path.resolve(directory,'second.html')).href);assert.deepEqual((await inspect(updated)).density.pixel,[230,120,20]);await updatedContext.close();
  pass('unselected_candidate_bytes_are_frozen_changes_mark_stale_new_publication_changes_and_old_bytes_remain');
  result.rejections=[];
  for(const value of ['image,small.png 1x, missing.png 2x','image,small.png 1x, https://example.com/x.png 2x','../../outside.png 1x','image,small.png 1x, wide%20image.png 1e0x','image,small.png 160w, wide%20image.png 2x','image,small.png 0w','image,small.png 1x 2x']){
   await put(`<img src="image,small.png" srcset="${value}">`);const response=await context.request.post(base+'/api/v1'+prefix+'/report:build',{data:{},headers:{Authorization:'Bearer '+token}});result.rejections.push({value,status:response.status()});assert.equal(response.status(),400);assert.equal((await api('GET',prefix+'/jobs')).length,2);
  }
  await put(markdown);assert(secondHTML.equals(await download(second.publication.id,'html')));
  pass('missing_remote_escape_duplicate_mixed_zero_and_multiple_descriptors_rejected_before_job_creation_old_publication_retained');
  assert.deepEqual(result.page_errors,[]);result.passed=true;save();
 }catch(error){result.error=String(error.stack||error);save();throw error;}
 finally{await browser.close();console.log('SRCSET_EVIDENCE',directory);}
})().catch(error=>{console.error(error);process.exitCode=1;});
