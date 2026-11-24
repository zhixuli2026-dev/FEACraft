// Actual S3 documents: passive rendering, invalid DOCX and recovery. No LLM.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const [reviewPath,directory,playwrightPath,chromePath]=process.argv.slice(2);
const review=JSON.parse(fs.readFileSync(reviewPath,'utf8')),sha=b=>crypto.createHash('sha256').update(b).digest('hex');
assert.equal(review.classification,'no_llm');for(const [file,h] of Object.entries(review.sources))assert.equal(sha(fs.readFileSync(file)),h,file);
const backend=JSON.parse(fs.readFileSync(path.join(directory,'result.json'),'utf8')),account=JSON.parse(fs.readFileSync(path.join(directory,'private/account.json'),'utf8'));
const result={passed:false,checks:[],page_errors:[],real_model_calls:0,mock_llm_calls:0,review:reviewPath};
const save=()=>fs.writeFileSync(path.join(directory,'reader-boundaries.json'),JSON.stringify(result,null,2));
const pass=label=>{result.checks.push(label);save();console.log('READER_BOUNDARY_PASS',label);};
const {chromium}=require(playwrightPath);
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:chromePath});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1200}}),page=await context.newPage();page.on('pageerror',e=>result.page_errors.push(e.message));
  const login=await context.request.post(backend.base_url+'/api/v1/auth/login',{data:{email:account.email,password:account.password}});assert(login.ok());const token=(await login.json()).token;
  const api=async(method,suffix,data,extra={})=>{const deadline=Date.now()+15000;while(true){const r=await context.request.fetch(backend.base_url+'/api/v1'+suffix,{method,data,headers:{Authorization:'Bearer '+token},...extra});
   if(r.status()===409&&(await r.json()).detail?.code==='storage_quota_busy'){assert(Date.now()<deadline);await new Promise(r=>setTimeout(r,200));continue;}assert.equal(r.status(),200,method+' '+suffix+' '+r.status());return r;}};
  const prefix=`/cases/${backend.case_id}`;
  const malicious=`<h1 id="safe">SAFE-READER-CONTENT</h1>
<script>window.READER_EXECUTED='script'</script>
<iframe srcdoc="<script>parent.READER_EXECUTED='iframe'</script>"></iframe>
<object data="https://example.invalid/object"></object><embed src="https://example.invalid/embed">
<style>@import 'https://example.invalid/style';</style><link rel="stylesheet" href="https://example.invalid/link">
<p style="background:url('https://example.invalid/background')" onclick="window.READER_EXECUTED='click'">Passive text</p>
<a href="javascript:window.READER_EXECUTED='link'">unsafe link</a>
<svg xmlns="http://www.w3.org/2000/svg" width="120" height="50" onload="window.READER_EXECUTED='svg'"><script>window.READER_EXECUTED='svg-script'</script><rect width="120" height="50" fill="blue"/><image href="https://example.invalid/svg"/><use href="https://example.invalid/use"/></svg>
<img id="safe-image" src="assets/small.png" onerror="window.READER_EXECUTED='image'" srcset="assets/small.png 1x, https://example.invalid/candidate 2x">
<img src="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">`;
  await api('PUT',prefix+'/file',{path:'E-report/boundaries.html',content:malicious});
  const upload=async bytes=>api('POST',prefix+'/upload',undefined,{params:{path:'E-report/recovery.docx'},multipart:{file:{name:'recovery.docx',mimeType:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',buffer:bytes}}});
  await upload(Buffer.from('INVALID DOCX actual uploaded bytes'));
  await page.goto(backend.base_url);await page.getByRole('textbox',{name:'邮箱',exact:true}).fill(account.email);await page.getByRole('textbox',{name:'密码',exact:true}).fill(account.password);await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.locator(`[data-case-name="${backend.case_name}"]`).click();await page.locator('[data-path="E-report"][data-dir="true"]').waitFor();
  if(!await page.locator('[data-path="E-report/boundaries.html"]').count())await page.locator('[data-path="E-report"][data-dir="true"]').click();
  const external=[];page.on('request',r=>{if(new URL(r.url()).hostname==='example.invalid')external.push(r.url());});
  await page.locator('[data-path="E-report/boundaries.html"][data-dir="false"]').click();await page.getByTestId('document-shadow').locator('#safe-image').waitFor();
  const actual=await page.getByTestId('document-shadow').evaluate(async host=>{const root=host.shadowRoot.querySelector('[data-testid="document-content"]'),img=root.querySelector('#safe-image');await img.decode();
   return {content:root.textContent,forbidden:root.querySelectorAll('script,iframe,object,embed,style,link').length,
    attributes:[...root.querySelectorAll('*')].flatMap(e=>[...e.attributes].filter(a=>/^on/i.test(a.name)||a.name==='style'||a.value.startsWith('javascript:')).map(a=>a.name)),
    candidate:img.hasAttribute('srcset'),blob:img.currentSrc.startsWith('blob:'),svg:root.querySelectorAll('svg rect').length};});
  result.passive=actual;save();assert(actual.content.includes('SAFE-READER-CONTENT'));assert.equal(actual.forbidden,0);assert.deepEqual(actual.attributes,[]);assert(!actual.candidate&&actual.blob&&actual.svg===1);assert.deepEqual(external,[]);
  assert.equal(await page.evaluate(()=>window.READER_EXECUTED),undefined);
  pass('actual_S3_HTML_script_handlers_javascript_styles_frames_remote_SVG_and_srcset_removed_local_image_retained');
  await page.locator('[data-path="E-report/recovery.docx"][data-dir="false"]').click();
  await page.getByTestId('document-reader').getByRole('alert').waitFor();assert(await page.getByTestId('document-download').isDisabled());
  assert.equal(await page.getByTestId('document-shadow').locator('img').count(),0);result.invalid_docx_error=await page.getByTestId('document-reader').getByRole('alert').innerText();
  assert(result.invalid_docx_error.includes('无法预览这份 DOCX')&&result.invalid_docx_error.includes('重新上传'));assert(!result.invalid_docx_error.includes('jszip'));
  await page.getByTestId('document-reader').getByRole('alert').screenshot({path:path.join(directory,'invalid-docx.png')});
  pass('genuinely_invalid_uploaded_DOCX_shows_error_clears_old_content_disables_download');
  const original=fs.readFileSync(path.join(directory,'second.docx'));await upload(original);await page.getByTestId('document-reload').click();
  await page.getByTestId('document-shadow').locator('img').waitFor();assert.equal(await page.getByTestId('document-reader').getByRole('alert').count(),0);assert(!await page.getByTestId('document-download').isDisabled());
  const recovery=await page.getByTestId('document-shadow').evaluate(host=>({math:host.shadowRoot.querySelectorAll('math').length,text:host.shadowRoot.textContent}));assert(recovery.math>0&&recovery.text.includes('S3-ROW'));
  const pending=page.waitForEvent('download');await page.getByTestId('document-download').click();const download=await pending;const target=path.join(directory,'recovered-original.docx');await download.saveAs(target);assert.equal(sha(fs.readFileSync(target)),sha(original));
  await page.screenshot({path:path.join(directory,'recovered-docx.png')});pass('actual_file_repair_and_explicit_reload_restore_DOCX_math_table_and_exact_original_download');
  assert.deepEqual(result.page_errors,[]);result.passed=true;save();
 }catch(error){result.error=String(error.stack||error);save();throw error;}
 finally{await browser.close();console.log('READER_BOUNDARY_EVIDENCE',directory);}
})().catch(error=>{console.error(error);process.exitCode=1;});
