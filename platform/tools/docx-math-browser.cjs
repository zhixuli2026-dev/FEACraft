// Real Docker/Pandoc artifacts, actual Chromium reader and byte downloads. No LLM.
const fs=require('node:fs'), path=require('node:path'), crypto=require('node:crypto'), assert=require('node:assert/strict');
const [reviewPath,playwrightPath,chromePath]=process.argv.slice(2);
const review=JSON.parse(fs.readFileSync(reviewPath,'utf8'));
assert.equal(review.classification,'no_llm');
for(const [name,digest] of Object.entries(review.sources))assert.equal(crypto.createHash('sha256').update(fs.readFileSync(name)).digest('hex'),digest,name);
const {chromium}=require(playwrightPath);
const JSZip=require(path.resolve('platform/web/node_modules/jszip'));
const directory=path.join('tmp/docker-deployment-20261117','docx-math-'+crypto.randomBytes(5).toString('hex'));
fs.mkdirSync(directory,{recursive:true});
const result={passed:false,checks:[],real_model_calls:0,mock_llm_calls:0,page_errors:[]};
const save=()=>fs.writeFileSync(path.join(directory,'browser-result.json'),JSON.stringify(result,null,2));
const pass=name=>{result.checks.push(name);save();console.log('DOCX_MATH_PASS',name);};
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const base='http://127.0.0.1:62883';
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:chromePath});
 const context=await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
 const page=await context.newPage();page.on('pageerror',e=>result.page_errors.push(e.message));
 let token;
 const request=async(method,suffix,data)=>{
  const res=await context.request.fetch(base+'/api/v1'+suffix,{method,data,headers:token?{Authorization:'Bearer '+token}:{},timeout:180000});
  assert.equal(res.status(),200,method+' '+suffix+' '+(await res.text()).slice(0,250));return res.json();
 };
 const waitReader=async()=>{await page.getByTestId('document-reader').filter({has:page.getByTestId('document-shadow')}).waitFor();
  await page.waitForFunction(()=>!!document.querySelector('[data-testid="document-shadow"]')?.shadowRoot?.querySelector('article'));};
 const inspect=()=>page.getByTestId('document-shadow').evaluate(host=>{
  const root=host.shadowRoot, formulas=[...root.querySelectorAll('math')];
  return {count:formulas.length,math:formulas.map(m=>({text:m.textContent,html:m.outerHTML,display:m.getAttribute('display'),
   width:m.getBoundingClientRect().width,height:m.getBoundingClientRect().height})),headings:root.querySelectorAll('h1,h2,h3').length,
   images:[...root.querySelectorAll('img')].map(i=>i.complete&&i.naturalWidth>0),text:root.querySelector('article').textContent};
 });
 const download=async()=>{const pending=page.waitForEvent('download');await page.getByTestId('document-download').click();
  const stream=await(await pending).createReadStream();const chunks=[];for await(const b of stream)chunks.push(b);return Buffer.concat(chunks);};
 const openCase=async(name)=>{await page.locator(`[data-case-name="${name}"]`).click();await page.getByText('caseDict',{exact:true}).last().click();};
 const report=async()=>{await page.getByTestId('report-build-control').getByRole('button',{name:'report.docx',exact:true}).click();await waitReader();};
 try{
  const account=JSON.parse(fs.readFileSync('tmp/docker-deployment-20261117/report-controls-5ecb3a7bb8/private/account.json','utf8'));
  token=(await request('POST','/auth/login',{email:account.email,password:account.password})).token;
  const workspace=(await request('GET','/workspaces'))[0];
  const cases=await request('GET',`/workspaces/${workspace.id}/cases`);
  const previous=cases.find(c=>c.id===13);assert(previous);
  const current=await request('POST',`/workspaces/${workspace.id}/cases`,{name:'DOCX Math '+crypto.randomBytes(4).toString('hex')});
  result.case_id=current.id;
  await page.goto(base);await page.getByRole('textbox',{name:'邮箱',exact:true}).fill(account.email);
  await page.getByRole('textbox',{name:'密码',exact:true}).fill(account.password);await page.getByRole('button',{name:'登录',exact:true}).click();
  await openCase(previous.name);await report();
  const old=await inspect();result.original_report=old;assert.equal(old.count,2);assert(old.images.length&&old.images.every(Boolean));
  assert(old.math.every(m=>m.width>0&&m.height>0));assert(old.math[1].html.includes('mfrac'));assert(old.math[0].text.includes('300'));
  const previousPublication=await request('GET','/cases/13/report/results');
  const previousArtifact=await context.request.get(base+'/api/v1/cases/13/report/artifact',{
   params:{path:'E-report/build/report.docx',publication_id:previousPublication.publication.id},headers:{Authorization:'Bearer '+token}});
  assert(previousArtifact.ok());assert((await download()).equals(await previousArtifact.body()));
  await page.screenshot({path:path.join(directory,'original-report-formulas.png')});
  pass('previous_actual_DOCX_now_shows_both_equations_SVG_and_exact_original_download');

  const md=String.raw`# DOCX 公式结构验收

## 行内与顺序

开始 $T_0=300\,K$ 中间 $x_i^2$ 结束。重复 $x_i^2$。

## 分式与根式

$$T(x)=T_0+\frac{qL}{k}\left(1-\frac{x}{L}\right)$$

$$\sqrt{a^2+b^2}+\sqrt[3]{x}$$

## 矩阵与算子

$$A=\begin{pmatrix}1&2\\3&4\end{pmatrix}$$

$$S=\sum_{i=1}^{n} i^2$$

$$I=\int_0^1 x^2\,dx$$

$$\lim_{x\to 0}\frac{\sin x}{x}=1$$

$$\hat{x}+\bar{y}+\vec{v}$$

$$\binom{n}{k}$$

正文脚注[^math]。

[^math]: 脚注公式 $E=mc^2$。
`;
  await request('PUT',`/cases/${current.id}/file`,{path:'E-report/report.md',content:md});
  const submitted=await request('POST',`/cases/${current.id}/report:build`,{});result.job_id=submitted.job_id;save();
  let job;const deadline=Date.now()+180000;
  do{job=await request('GET','/jobs/'+submitted.job_id);if(!['queued','running'].includes(job.status))break;
   await new Promise(resolve=>setTimeout(resolve,400));}while(Date.now()<deadline);
  assert.equal(job.status,'succeeded');assert(job.attempt.process_stop.verified);result.stop_verified=true;
  await openCase(current.name);await report();
  const actual=await inspect();result.complex=actual;
  assert.equal(actual.count,12);assert(actual.math.every(m=>m.width>0&&m.height>0));
  const markup=actual.math.map(m=>m.html).join('');
  for(const tag of ['mfrac','msub','msubsup','msqrt','mroot','mtable','mtr','mtd','munder','mover'])assert(markup.includes('<'+tag),tag);
  assert(actual.text.includes('开始 T0=300'));assert(actual.text.includes('中间 xi2'));assert(actual.text.includes('结束。重复 xi2'));
  assert(actual.text.includes('脚注公式 E=mc2'));assert(!actual.text.includes('VFMATH')&&!actual.text.includes('公式未能预览'));
  const matrix=actual.math.find(m=>m.html.includes('<mtable'));assert(matrix.text.includes('1234'));
  assert(markup.includes('∑')&&markup.includes('∫')&&markup.includes('→'));
  const published=await request('GET',`/cases/${current.id}/report/results`);
  const response=await context.request.get(base+`/api/v1/cases/${current.id}/report/artifact`,{params:{path:'E-report/build/report.docx',publication_id:published.publication.id},headers:{Authorization:'Bearer '+token}});
  assert(response.ok());const bytes=await response.body();assert(bytes.equals(await download()));
  fs.writeFileSync(path.join(directory,'actual-report.docx'),bytes);result.docx_sha256=hash(bytes);
  await page.screenshot({path:path.join(directory,'complex-formulas-top.png')});
  await page.getByTestId('document-shadow').locator('math').nth(6).scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(directory,'complex-formulas-bottom.png')});
  pass('actual_Pandoc_DOCX_12_formulas_order_scripts_fractions_roots_matrix_operators_footnote_and_exact_download');

  // Real file fault fixture: replace one OMML construct, upload the resulting DOCX.
  const zip=await JSZip.loadAsync(bytes);let xml=await zip.file('word/document.xml').async('string');
  assert(xml.includes('<m:f>'));xml=xml.replace('<m:f>','<m:unknownOperator>').replace('</m:f>','</m:unknownOperator>');
  zip.file('word/document.xml',xml);const bad=await zip.generateAsync({type:'nodebuffer'});
  const uploaded=await context.request.post(base+`/api/v1/cases/${current.id}/upload`,{params:{path:'Ref/unsupported.docx'},
   headers:{Authorization:'Bearer '+token},multipart:{file:{name:'unsupported.docx',mimeType:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',buffer:bad}}});
  assert(uploaded.ok());await page.reload();await openCase(current.name);
  await page.locator('[data-path="Ref/unsupported.docx"]').click();
  await page.getByTestId('document-reader').filter({has:page.getByTestId('document-version').filter({hasText:'工作区文件'})}).waitFor();
  await page.getByTestId('document-shadow').getByText('[公式未能预览，请下载原件查看]',{exact:true}).waitFor();
  const fault=await inspect();result.unsupported=fault;assert.equal(fault.count,11);
  assert(fault.text.includes('公式未能预览，请下载原件查看'));assert(!fault.text.includes('VFMATH'));
  assert(bad.equals(await download()));
  await page.screenshot({path:path.join(directory,'unsupported-visible.png')});
  pass('actual_uploaded_DOCX_unsupported_operator_visible_and_original_unchanged');
  assert.equal(result.page_errors.length,0);result.passed=true;save();console.log(JSON.stringify({passed:true,evidence:directory,checks:result.checks.length}));
 }catch(error){result.failure={name:error.name,message:error.message};save();console.error(error.stack);process.exitCode=1;}
 finally{await browser.close();save();}
})();
