// Actual Docker API/Pandoc and Chromium. Network faults never fabricate a reply.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const [reviewPath, playwrightPath, chromePath] = process.argv.slice(2);
const review = JSON.parse(fs.readFileSync(reviewPath, 'utf8'));
assert.equal(review.classification, 'no_llm');
for (const [name, hash] of Object.entries(review.sources)) {
  assert.equal(crypto.createHash('sha256').update(fs.readFileSync(name)).digest('hex'), hash, name);
}
const { chromium } = require(playwrightPath);
const directory = path.join('tmp/docker-deployment-20261117', 'report-controls-' + crypto.randomBytes(5).toString('hex'));
fs.mkdirSync(path.join(directory, 'private'), { recursive: true });
const result = { passed: false, checks: [], real_model_calls: 0, mock_llm_calls: 0, jobs: [], page_errors: [],
  network_faults: 'Actual server response lost once; no response body is simulated' };
const save = () => fs.writeFileSync(path.join(directory, 'browser-result.json'), JSON.stringify(result, null, 2));
const pass = name => { result.checks.push(name); save(); console.log('REPORT_UI_PASS', name); };
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
(async () => {
 const browser = await chromium.launch({headless:true, executablePath:chromePath});
 const context = await browser.newContext({viewport:{width:1440,height:1000}, acceptDownloads:true});
 const page = await context.newPage();
 page.on('pageerror', error => result.page_errors.push(error.message));
 const base = 'http://127.0.0.1:62883';
 let token;
 const api = async (method, suffix, data) => {
  const response = await context.request.fetch(base+'/api/v1'+suffix, {method, data,
    headers: token ? {Authorization:'Bearer '+token} : {}, timeout:180000});
  assert.equal(response.status(),200, method+' '+suffix+' '+(await response.text()).slice(0,500));
  return response.json();
 };
 try {
  const account = {email:'report-ui-'+crypto.randomBytes(7).toString('hex')+'@example.com',password:crypto.randomBytes(20).toString('hex'),name:'Report UI acceptance'};
  fs.writeFileSync(path.join(directory,'private/account.json'),JSON.stringify(account));
  const user=await api('POST','/auth/register',account); token=user.token;
  const ws=(await api('GET','/workspaces'))[0].id;
  const name='Report UI '+crypto.randomBytes(4).toString('hex');
  const selected=await api('POST',`/workspaces/${ws}/cases`,{name});
  const other=await api('POST',`/workspaces/${ws}/cases`,{name:name+' other'});
  result.case_id=selected.id; result.other_case_id=other.id;
  const prefix=`/cases/${selected.id}`;
  const put=(file,content)=>api('PUT',prefix+'/file',{path:file,content});
  const marker='LAYOUT_'+crypto.randomBytes(12).toString('hex'); result.marker=marker;
  const markdown=`# 工程报告 · α 温度响应\n\n${marker}\n\n## 方法与公式\n\n边界温度为 $T_0=300\\,K$，解析示意：\n\n$$T(x)=T_0+\\frac{qL}{k}\\left(1-\\frac{x}{L}\\right)$$\n\n## 结果与图表\n\n![线性响应示意图](../D-post/response.svg){width=70%}\n\n| 位置 | 温度 | 说明 |\n|:---|---:|:---|\n| 入口 | 310 K | 中文与 **粗体** |\n| 出口 | 300 K | 保留单位与脚注[^note] |\n\n> 此图用于文档排版验收，不是求解器验证结果。\n\n1. 输入检查\n2. 结果归档\n\n[^note]: 冻结资源与原件下载验收。\n`.replaceAll('\\n','\n');
  await put('D-post/response.svg','<svg xmlns="http://www.w3.org/2000/svg" width="600" height="240" viewBox="0 0 600 240"><rect width="600" height="240" fill="white"/><path d="M50 20V200H570" fill="none" stroke="#333" stroke-width="2"/><path d="M50 40L550 180" stroke="#1760ad" stroke-width="4"/><text x="220" y="230" font-size="18">Position x</text><text x="65" y="30" font-size="18">Temperature T</text></svg>');
  await put('E-report/report.md','---\ncss: remote.css\n---\n\nInvalid external style'.replaceAll('\\n','\n'));
  await put('Ref/svg-safety.html','<h1>SVG safety receipt</h1><svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" onload="window.__svgExecuted=true"><script>window.__svgExecuted=true</script><path d="M0 10H100" stroke="blue"/><image href="http://127.0.0.1:62883/api/health?svg_unfrozen_probe=1"/><use href="http://127.0.0.1:62883/api/health?svg_unfrozen_probe=1#x"/></svg>');
  await page.goto(base);
  await page.getByRole('textbox',{name:'邮箱',exact:true}).fill(account.email);
  await page.getByRole('textbox',{name:'密码',exact:true}).fill(account.password);
  await page.getByRole('button',{name:'登录',exact:true}).click();
  const openCase=async target=>{await page.locator(`[data-case-name="${target}"]`).click(); await page.getByText('caseDict',{exact:true}).last().click(); await page.getByTestId('report-build').scrollIntoViewIfNeeded();};
  await openCase(name);
  await page.getByTestId('report-build').click();
  await page.getByTestId('report-build-error').filter({hasText:'构建请求被拒绝'}).waitFor({timeout:45000});
  assert(await page.getByTestId('report-build').isEnabled());
  assert.equal((await api('GET',prefix+'/jobs')).length,0);
  await page.getByTestId('report-build-control').screenshot({path:path.join(directory,'actual-validation-error.png')});
  pass('actual_invalid_document_rejected_visible_and_retry_enabled');

  await put('E-report/report.md',markdown);
  let lostJob, calls=0;
  const routePattern=base+'/api/v1'+prefix+'/report:build';
  await page.route(routePattern,async route=>{
    calls++;
    const response=await route.fetch(); assert.equal(response.status(),200);
    lostJob=(await response.json()).job_id;
    await route.abort('connectionreset');
  });
  await page.getByTestId('report-build').click();
  await page.getByRole('button',{name:'确认提交结果',exact:true}).waitFor();
  await page.waitForFunction(()=>document.querySelector('[data-testid="report-build-status"]')?.textContent.includes('尚未确认'));
  assert.equal(calls,1); assert(lostJob);
  await page.unroute(routePattern);
  await page.getByRole('button',{name:'确认提交结果',exact:true}).click();
  await page.waitForFunction(id=>Object.values(sessionStorage).some(value=>{try{return JSON.parse(value)?.jobId===id;}catch{return false;}}),lostJob);
  await page.reload(); await openCase(name);
  await page.getByTestId('report-build-status').filter({hasText:'构建完成'}).waitFor({timeout:180000});
  const jobs=await api('GET',prefix+'/jobs'); assert.equal(jobs.length,1); assert.equal(jobs[0].id,lostJob);
  result.jobs.push(lostJob);
  pass('lost_actual_submission_response_replayed_once_and_reload_only_queries');

  const publication=await api('GET',prefix+'/report/results');
  result.publication_id=publication.publication.id;
  for (const kind of ['html','docx']) {
    await page.getByTestId('report-build-control').getByRole('button',{name:'report.'+kind,exact:true}).click();
    await page.waitForFunction(marker=>document.querySelector('[data-testid="document-shadow"]')?.shadowRoot?.querySelector('article')?.textContent.includes(marker),marker);
    const rendered=await page.getByTestId('document-shadow').evaluate(host=>{
      const root=host.shadowRoot;
      return {headings:root.querySelectorAll('h1,h2').length,tables:root.querySelectorAll('table').length,
        math:root.querySelectorAll('math').length,svg:[...root.querySelectorAll('svg')].map(svg=>({paths:svg.querySelectorAll('path').length,width:svg.getBoundingClientRect().width,label:svg.getAttribute('aria-label')})),
        images:[...root.querySelectorAll('img')].map(img=>({loaded:img.complete&&img.naturalWidth>0,src:img.src.slice(0,30)})),
        text:root.querySelector('article').textContent};
    });
    assert(rendered.headings>=3 && rendered.tables>=1); assert(rendered.images.length+rendered.svg.length>=1);
    if(kind==='html')assert(rendered.svg.some(svg=>svg.paths>=2&&svg.width>0&&svg.label));
    await page.waitForFunction(()=>[...document.querySelector('[data-testid="document-shadow"]').shadowRoot.querySelectorAll('img')].every(img=>img.complete&&img.naturalWidth>0));
    if(kind==='html')assert(rendered.math>=2);
    result[kind+'_render']=rendered;
    const pending=page.waitForEvent('download'); await page.getByTestId('document-download').click();
    const download=await pending; const stream=await download.createReadStream(); const chunks=[];
    for await(const chunk of stream)chunks.push(chunk);
    const bytes=Buffer.concat(chunks);
    const artifact=await context.request.get(base+'/api/v1'+prefix+'/report/artifact',{params:{path:'E-report/build/report.'+kind,publication_id:publication.publication.id},headers:{Authorization:'Bearer '+token}});
    assert(artifact.ok() && bytes.equals(await artifact.body()));
    result[kind+'_sha256']=hash(bytes);
    await page.screenshot({path:path.join(directory,'complex-'+kind+'.png')});
    await page.getByTestId('document-shadow').locator('svg,img').first().scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(directory,'complex-'+kind+'-figure.png')});
    pass('actual_'+kind+'_reader_images_tables_headings_and_exact_download');
  }

  const externalSVG=[];
  page.on('request',request=>{if(request.url().includes('svg_unfrozen_probe'))externalSVG.push(request.url());});
  await page.locator('[data-path="Ref/svg-safety.html"]').click();
  await page.waitForFunction(()=>document.querySelector('[data-testid="document-shadow"]')?.shadowRoot?.textContent.includes('SVG safety receipt'));
  const safety=await page.getByTestId('document-shadow').evaluate(host=>({svg:host.shadowRoot.querySelectorAll('svg').length,
    scripts:host.shadowRoot.querySelectorAll('script,[onload],foreignObject').length,executed:Boolean(window.__svgExecuted)}));
  assert(safety.svg===1&&safety.scripts===0&&!safety.executed);assert.equal(externalSVG.length,0);
  result.svg_safety=safety;
  pass('actual_HTML_SVG_keeps_geometry_and_blocks_scripts_and_external_references');

  // Fail only real GET delivery while the server continues its actual report.
  const pollPattern=base+'/api/v1/jobs/*';
  await page.route(pollPattern,route=>route.request().method()==='GET'?route.abort('connectionreset'):route.continue());
  await page.getByTestId('report-build').click();
  await page.getByTestId('report-build-error').filter({hasText:'状态或产物读取失败'}).waitFor({timeout:45000});
  const secondJobs=await api('GET',prefix+'/jobs');assert.equal(secondJobs.length,2);
  const resumedJob=secondJobs.find(job=>job.id!==lostJob).id;result.jobs.push(resumedJob);
  await openCase(other.name);
  assert.equal(await page.getByTestId('report-build-status').count(),0);
  await page.unroute(pollPattern);
  await openCase(name);
  await page.getByTestId('report-build-status').filter({hasText:'构建完成'}).waitFor({timeout:180000});
  assert.equal((await api('GET',prefix+'/jobs')).length,2);
  const beforeCancel=(await api('GET',prefix+'/report/results')).publication.id;
  pass('actual_poll_disconnect_case_switch_and_saved_job_resume_without_new_submission');

  // A real running report is cancelled through its own visible control.
  await page.getByTestId('report-build').click();
  await page.getByRole('button',{name:'取消构建',exact:true}).waitFor();
  const cancelId=await page.evaluate(()=>Object.values(sessionStorage).map(value=>{try{return JSON.parse(value)?.jobId;}catch{return null;}}).find(id=>Number.isSafeInteger(id)));
  assert(cancelId);
  const startedDeadline=Date.now()+45000;let starting;
  do {
    starting=await api('GET','/jobs/'+cancelId);
    if(starting.status==='running'&&starting.attempt?.process_identity)break;
    assert(['queued','running'].includes(starting.status),'Report ended before running cancellation observation');
    await sleep(50);
  }while(Date.now()<startedDeadline);
  assert(starting.status==='running'&&starting.attempt?.process_identity);
  result.cancel_started_job=cancelId;
  await page.getByRole('button',{name:'取消构建',exact:true}).click();
  await page.getByTestId('report-build-status').filter({hasText:'已取消'}).waitFor({timeout:120000});
  const cancelled=(await api('GET',prefix+'/jobs')).find(job=>job.id!==lostJob&&job.id!==resumedJob);
  assert(cancelled && cancelled.status==='cancelled'); result.jobs.push(cancelled.id);
  const deadline=Date.now()+60000; let stopped;
  do { stopped=await api('GET','/jobs/'+cancelled.id); if(stopped.attempt?.process_stop?.verified)break;await sleep(200); } while(Date.now()<deadline);
  assert(stopped.attempt?.process_stop?.verified);
  assert.equal((await api('GET',prefix+'/report/results')).publication.id,beforeCancel);
  pass('actual_running_report_cancelled_stopped_and_previous_publication_preserved');

  await openCase(other.name);
  assert.equal(await page.getByTestId('report-build-status').count(),0);
  assert.equal(await page.getByTestId('report-build-control').locator('li').count(),0);
  await openCase(name);
  for(const theme of ['dark','light']){
    await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
    await page.getByTestId('report-build-control').screenshot({path:path.join(directory,'controls-'+theme+'.png')});
  }
  await page.setViewportSize({width:390,height:844});
  await page.getByTestId('report-build').scrollIntoViewIfNeeded();
  await page.getByTestId('report-build-control').screenshot({path:path.join(directory,'controls-390.png')});
  result.controls_geometry=await page.getByTestId('report-build-control').evaluate(section=>({width:section.clientWidth,scroll:section.scrollWidth,buttons:[...section.querySelectorAll('button')].map(button=>({text:button.textContent,height:button.getBoundingClientRect().height}))}));
  assert(result.controls_geometry.scroll<=result.controls_geometry.width+1);
  assert(result.controls_geometry.buttons.every(button=>button.height>=44));
  assert.equal(result.page_errors.length,0);
  pass('case_switch_isolation_dark_light_and_390px_controls');
  result.passed=true; save(); console.log(JSON.stringify({passed:true,evidence:directory,checks:result.checks.length}));
 }catch(error){result.failure={name:error.name,message:error.message};save();console.error(error.stack);process.exitCode=1;}
 finally{await browser.close();save();}
})();
