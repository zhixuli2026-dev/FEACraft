// Real Chromium rendering, S3 publication history and original downloads. No LLM.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const [reviewPath,directory,playwrightPath,chromePath]=process.argv.slice(2);
const review=JSON.parse(fs.readFileSync(reviewPath,'utf8'));assert.equal(review.classification,'no_llm');
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
for(const [file,digest] of Object.entries(review.sources))assert.equal(sha(fs.readFileSync(file)),digest,file);
const backend=JSON.parse(fs.readFileSync(path.join(directory,'result.json'),'utf8'));assert(backend.passed);
const account=JSON.parse(fs.readFileSync(path.join(directory,'private/account.json'),'utf8'));
const {chromium}=require(playwrightPath);
const result={passed:false,checks:[],page_errors:[],real_model_calls:0,mock_llm_calls:0,review:reviewPath};
const save=()=>fs.writeFileSync(path.join(directory,'browser-result.json'),JSON.stringify(result,null,2));
const pass=label=>{result.checks.push(label);save();console.log('S3_BROWSER_PASS',label);};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:chromePath});
 try{
  result.standalone=[];
  for(const [version,color,pixel] of [['first','rgb(19, 92, 114)',[20,70,190]],['second','rgb(140, 36, 91)',[230,120,20]]]){
   const context=await browser.newContext({viewport:{width:1000,height:950},deviceScaleFactor:2}),page=await context.newPage(),requests=[];
   page.on('request',r=>{if(/^https?:/.test(r.url()))requests.push(r.url());});
   await page.goto(pathToFileURL(path.resolve(directory,version+'.html')).href);
   const actual=await page.evaluate(async()=>{
    await document.fonts.ready;const img=document.getElementById('responsive');await img.decode();
    const canvas=document.createElement('canvas');canvas.width=canvas.height=1;const ctx=canvas.getContext('2d');ctx.drawImage(img,0,0,1,1);
    const url=getComputedStyle(document.querySelector('.plot')).backgroundImage.match(/^url\(["']?(.*?)["']?\)$/)[1];const background=new Image();background.src=url;await background.decode();
    return {color:getComputedStyle(document.querySelector('h1:not(.title)')).color,pixel:[...ctx.getImageData(0,0,1,1).data].slice(0,3),
      src:img.currentSrc.startsWith('data:'),background:url.startsWith('data:'),font:[...document.fonts].some(f=>f.family==='FrozenReport'&&f.status==='loaded')};
   });
   assert.equal(actual.color,color);assert.deepEqual(actual.pixel,pixel);assert(actual.src&&actual.background&&actual.font);assert.deepEqual(requests,[]);
   result.standalone.push({version,...actual,network_requests:requests.length});await page.screenshot({path:path.join(directory,version+'-standalone.png'),fullPage:true});await context.close();
  }
  pass('downloaded_S3_HTML_old_and_new_CSS_font_background_srcset_render_with_no_network');
  const context=await browser.newContext({viewport:{width:1440,height:1050},deviceScaleFactor:2}),page=await context.newPage();
  page.on('pageerror',e=>result.page_errors.push(e.message));
  await page.goto(backend.base_url);await page.getByRole('textbox',{name:'邮箱',exact:true}).fill(account.email);await page.getByRole('textbox',{name:'密码',exact:true}).fill(account.password);await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.locator(`[data-case-name="${backend.case_name}"]`).click();await page.getByText('caseDict',{exact:true}).last().click();
  await page.getByTestId('report-build-control').getByRole('button',{name:'report.html',exact:true}).click();
  const inspect=async()=>{
   await page.getByTestId('document-shadow').locator('img#responsive').waitFor();
   return page.getByTestId('document-shadow').evaluate(async host=>{const img=host.shadowRoot.getElementById('responsive');await img.decode();
    const canvas=document.createElement('canvas');canvas.width=canvas.height=1;const ctx=canvas.getContext('2d');ctx.drawImage(img,0,0,1,1);
    return {pixel:[...ctx.getImageData(0,0,1,1).data].slice(0,3),set:img.getAttribute('srcset'),math:host.shadowRoot.querySelectorAll('math').length};});
  };
  assert.deepEqual((await inspect()).pixel,[230,120,20]);
  await page.getByTestId('report-history-toggle').click();await page.locator(`[data-publication-id="${backend.first_publication}"]`).click();
  await page.waitForFunction(()=>document.querySelector('[data-testid="document-version"]')?.textContent.includes('版本 1'));
  const old=await inspect();assert.deepEqual(old.pixel,[20,70,190]);assert(old.set&&old.math>0);
  await page.reload();await page.getByTestId('document-shadow').locator('img#responsive').waitFor();assert.deepEqual((await inspect()).pixel,[20,70,190]);
  const download=page.waitForEvent('download');await page.getByTestId('document-download').click();const received=await download;const target=path.join(directory,'ui-old.html');await received.saveAs(target);assert.equal(sha(fs.readFileSync(target)),backend.first_sha256.html);
  await page.screenshot({path:path.join(directory,'history-html.png')});
  pass('actual_S3_reader_selects_old_HTML_preserves_selection_on_reload_and_downloads_original_bytes');
  await page.getByText('caseDict',{exact:true}).last().click();await page.getByTestId('report-build-control').getByRole('button',{name:'report.docx',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[data-testid="document-reader"]')?.getAttribute('data-kind')==='docx'&&document.querySelector('[data-testid="document-shadow"]')?.shadowRoot?.querySelector('img'));
  const docx=await page.getByTestId('document-shadow').evaluate(async host=>{const root=host.shadowRoot,img=root.querySelector('img');await img.decode();const canvas=document.createElement('canvas');canvas.width=canvas.height=1;const ctx=canvas.getContext('2d');ctx.drawImage(img,0,0,1,1);return {pixel:[...ctx.getImageData(0,0,1,1).data].slice(0,3),math:root.querySelectorAll('math').length,table:root.querySelector('table')?.textContent,code:root.querySelector('pre code')?.innerText};});
  assert.deepEqual(docx.pixel,[190,30,40]);assert(docx.math>0);assert(docx.table.includes('S3-ROW'));assert(docx.code.includes('value = 42\nprint(value)'));
  await page.getByTestId('report-history-toggle').click();await page.locator(`[data-publication-id="${backend.second_publication}"]`).click();
  await page.waitForFunction(()=>document.querySelector('[data-testid="document-version"]')?.textContent.includes('版本 3'));
  const docxDownload=page.waitForEvent('download');await page.getByTestId('document-download').click();const receivedDocx=await docxDownload;const docxTarget=path.join(directory,'ui-second.docx');await receivedDocx.saveAs(docxTarget);assert.equal(sha(fs.readFileSync(docxTarget)),backend.second_sha256.docx);
  result.docx=docx;await page.screenshot({path:path.join(directory,'history-docx.png')});
  pass('actual_S3_DOCX_reader_fallback_math_table_code_and_historical_original_download');
  assert.deepEqual(result.page_errors,[]);result.passed=true;save();
 }catch(error){result.error=String(error.stack||error);save();throw error;}
 finally{await browser.close();console.log('S3_BROWSER_EVIDENCE',directory);}
})().catch(error=>{console.error(error);process.exitCode=1;});
