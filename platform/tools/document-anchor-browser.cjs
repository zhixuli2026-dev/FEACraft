// Actual Docker API, uploaded documents and Chromium navigation. No model calls.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const [reviewPath,playwrightPath,chromePath]=process.argv.slice(2);
const review=JSON.parse(fs.readFileSync(reviewPath,'utf8'));assert.equal(review.classification,'no_llm');
for(const [name,digest] of Object.entries(review.sources))assert.equal(crypto.createHash('sha256').update(fs.readFileSync(name)).digest('hex'),digest,name);
const {chromium}=require(playwrightPath);
const directory=path.join('tmp/docker-deployment-20261117','document-anchor-'+crypto.randomBytes(5).toString('hex'));
fs.mkdirSync(path.join(directory,'private'),{recursive:true});
const result={passed:false,checks:[],real_model_calls:0,mock_llm_calls:0,page_errors:[]};
const save=()=>fs.writeFileSync(path.join(directory,'result.json'),JSON.stringify(result,null,2));
const pass=name=>{result.checks.push(name);save();console.log('DOCUMENT_ANCHOR_PASS',name);};
const base='http://127.0.0.1:62883',source='E-report/start.html',target='E-report/chapters.html';
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:chromePath});
 const context=await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
 const page=await context.newPage();page.on('pageerror',e=>result.page_errors.push(e.message));
 let token;
 const api=async(method,suffix,data)=>{
  const r=await context.request.fetch(base+'/api/v1'+suffix,{method,data,headers:token?{Authorization:'Bearer '+token}:{},timeout:60000});
  assert.equal(r.status(),200,method+' '+suffix);return r.json();
 };
 const openFile=async file=>{
  await page.locator('[data-path="E-report"][data-dir="true"]').waitFor();
  if(!await page.locator(`[data-path="${file}"]`).count())await page.locator('[data-path="E-report"][data-dir="true"]').click();
  await page.locator(`[data-path="${file}"][data-dir="false"]`).click();await reader(file);
 };
 const reader=async file=>{
  await page.waitForFunction(file=>{
   const panel=document.querySelector('[data-testid="document-reader"]');
   return panel?.getAttribute('data-file')===file && !!panel.querySelector('[data-testid="document-shadow"]')?.shadowRoot?.querySelector('article');
  },file);
 };
 const location=()=>page.getByTestId('document-shadow').evaluate(host=>{
  const h=host.shadowRoot.getElementById('结果')||host.shadowRoot.getElementById('vf-document-heading-2');
  return {scroll:host.scrollTop,offset:h?h.getBoundingClientRect().top-host.getBoundingClientRect().top:null,
   height:host.clientHeight,focused:host.shadowRoot.activeElement?.id||null};
 });
 try{
  const account={email:'anchor-'+crypto.randomBytes(7).toString('hex')+'@example.com',password:crypto.randomBytes(24).toString('hex'),name:'Document navigation acceptance'};
  fs.writeFileSync(path.join(directory,'private/account.json'),JSON.stringify(account));
  token=(await api('POST','/auth/register',account)).token;
  const workspace=(await api('GET','/workspaces'))[0];
  const selected=await api('POST',`/workspaces/${workspace.id}/cases`,{name:'Document anchors '+crypto.randomBytes(4).toString('hex')});result.case_id=selected.id;
  const sibling=await api('POST',`/workspaces/${workspace.id}/cases`,{name:'Unrelated document '+crypto.randomBytes(4).toString('hex')});result.sibling_case_id=sibling.id;
  const put=(id,file,content)=>api('PUT',`/cases/${id}/file`,{path:file,content});
  await put(selected.id,source,'<h1>文档链接验收</h1><p><a href="chapters.html#%E7%BB%93%E6%9E%9C">跳转另一文件结果</a></p>'
   +'<p><a href="chapters.html#missing">跳转不存在章节</a></p><p><a href="chapters.html#%ZZ">错误编码章节</a></p>'
   +'<p><a href="formulas.docx#vf-document-heading-2">打开DOCX分式章节</a></p><p><a href="absent.html#result">不存在文件</a></p>');
  const chapter='<h1>章节文档</h1>'+Array.from({length:65},(_,i)=>`<p>前置段落 ${i+1}：用于检查实际滚动位置。</p>`).join('')
   +'<h2 id="结果">结果章节</h2><p>这里是目标位置。</p><a href="chapters.html#top">同文件回到开头</a>'
   +'<h2>后续内容</h2>'+Array.from({length:30},()=>'<p>附录段落。</p>').join('');
  await put(selected.id,target,chapter.replace('<h1>','<h1 id="top">'));
  await put(sibling.id,target,'<h1>另一个算例</h1><h2 id="结果">不能收到之前的跳转</h2>');
  const docx=fs.readFileSync('tmp/docker-deployment-20261117/docx-math-064c29af78/actual-report.docx');
  const upload=await context.request.post(base+`/api/v1/cases/${selected.id}/upload`,{params:{path:'E-report/formulas.docx'},headers:{Authorization:'Bearer '+token},multipart:{file:{name:'formulas.docx',mimeType:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',buffer:docx}}});assert(upload.ok());
  await page.goto(base);await page.getByRole('textbox',{name:'邮箱',exact:true}).fill(account.email);
  await page.getByRole('textbox',{name:'密码',exact:true}).fill(account.password);await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.locator(`[data-case-name="${selected.name}"]`).click();await openFile(source);
  await page.getByTestId('document-shadow').getByRole('link',{name:'跳转另一文件结果',exact:true}).click();await reader(target);
  await page.waitForFunction(()=>document.querySelector('[data-testid="document-shadow"]')?.scrollTop>1000,{},{timeout:6000}).catch(()=>{});
  result.cross_file=await location();await page.screenshot({path:path.join(directory,'cross-file.png')});save();
  assert(result.cross_file.scroll>1000 && result.cross_file.offset>=-2 && result.cross_file.offset<80,'Cross-file fragment must scroll to the requested heading');
  assert.equal(result.cross_file.focused,'结果','Keyboard focus must follow the target');
  pass('actual_cross_HTML_file_encoded_Chinese_anchor_scroll_and_keyboard_focus');
  await page.getByTestId('document-shadow').getByRole('link',{name:'同文件回到开头',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[data-testid="document-shadow"]')?.scrollTop<60);
  pass('same_file_path_fragment_returns_to_first_heading');
  await openFile(source);await page.getByTestId('document-shadow').getByRole('link',{name:'跳转不存在章节',exact:true}).click();await reader(target);
  await page.getByTestId('document-notice').filter({hasText:'未找到文档锚点：missing'}).waitFor();
  await openFile(source);await page.getByTestId('document-shadow').getByRole('link',{name:'错误编码章节',exact:true}).click();
  await page.getByTestId('document-notice').filter({hasText:'锚点编码无效'}).waitFor();assert.equal(await page.getByTestId('document-reader').getAttribute('data-file'),source);
  await page.getByTestId('document-shadow').getByRole('link',{name:'不存在文件',exact:true}).click();
  await page.getByTestId('document-notice').filter({hasText:'链接打开失败'}).waitFor();
  pass('missing_anchor_malformed_encoding_and_actual_404_are_explicit_and_recoverable');
  await page.getByTestId('document-shadow').getByRole('link',{name:'打开DOCX分式章节',exact:true}).click();await reader('E-report/formulas.docx');
  await page.getByTestId('document-notice').filter({hasText:'已定位 分式与根式'}).waitFor();
  const docxPosition=await location();result.docx_position=docxPosition;assert(docxPosition.offset>=-2 && docxPosition.offset<80);assert.equal(docxPosition.focused,'vf-document-heading-2');
  const pending=page.waitForEvent('download');await page.getByTestId('document-download').click();const stream=await(await pending).createReadStream();const chunks=[];for await(const b of stream)chunks.push(b);assert(Buffer.concat(chunks).equals(docx));
  await page.screenshot({path:path.join(directory,'docx-anchor.png')});
  pass('actual_DOCX_conversion_completes_before_jump_and_original_download_is_unchanged');
  await openFile(source);
  let release;const gate=new Promise(resolve=>release=resolve);let started;const observed=new Promise(resolve=>started=resolve);
  await page.route('**/raw?*',async route=>{
   const url=new URL(route.request().url());
   if(url.pathname===`/api/v1/cases/${selected.id}/raw`&&url.searchParams.get('path')===target){
    const response=await route.fetch();started();await gate;await route.fulfill({response}).catch(()=>{});
   }else await route.continue();
  });
  await page.getByTestId('document-shadow').getByRole('link',{name:'跳转另一文件结果',exact:true}).click();
  await Promise.race([observed,new Promise((_,reject)=>setTimeout(()=>reject(new Error('Actual target read not observed')),10000))]);
  await page.locator(`[data-case-name="${sibling.name}"]`).click();await openFile(target);release();await page.unrouteAll({behavior:'wait'});
  const isolated=await page.getByTestId('document-shadow').evaluate(h=>({text:h.shadowRoot.querySelector('article').textContent,scroll:h.scrollTop}));
  assert(isolated.text.includes('另一个算例')&&!isolated.text.includes('前置段落'));assert.equal(isolated.scroll,0);assert.equal(await page.getByTestId('document-notice').count(),0);
  pass('delayed_actual_response_and_pending_anchor_do_not_cross_case_selection');
  assert.deepEqual(result.page_errors,[]);result.passed=true;save();
 }catch(error){result.error=String(error.stack||error);save();await page.screenshot({path:path.join(directory,'failure.png')}).catch(()=>{});throw error;}
 finally{await browser.close();console.log('DOCUMENT_ANCHOR_EVIDENCE',directory);}
})().catch(error=>{console.error(error);process.exitCode=1;});
