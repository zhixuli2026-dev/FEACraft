// Inspect actual exported HTML/DOCX from report-office-acceptance, no inference.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const [reviewPath,directory,playwrightPath,chromePath]=process.argv.slice(2);
const review=JSON.parse(fs.readFileSync(reviewPath,'utf8'));assert.equal(review.classification,'no_llm');
for(const [file,digest] of Object.entries(review.sources))assert.equal(crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'),digest,file);
const {chromium}=require(playwrightPath),base='http://127.0.0.1:62883';
const result={passed:false,checks:[],real_model_calls:0,mock_llm_calls:0,page_errors:[]};
const save=()=>fs.writeFileSync(path.join(directory,'browser-result.json'),JSON.stringify(result,null,2));
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:chromePath});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true}),page=await context.newPage();
  page.on('pageerror',e=>result.page_errors.push(e.message));
  const account=JSON.parse(fs.readFileSync(path.join(directory,'private/account.json'),'utf8'));
  const prepared=JSON.parse(fs.readFileSync(path.join(directory,'result.json'),'utf8'));assert(prepared.prepared);
  await page.goto(base);await page.getByRole('textbox',{name:'邮箱',exact:true}).fill(account.email);
  await page.getByRole('textbox',{name:'密码',exact:true}).fill(account.password);await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.locator(`[data-case-name="${prepared.case_name}"]`).click();
  await page.locator('[data-path="E-report"][data-dir="true"]').waitFor();
  if(!await page.locator('[data-path="E-report/build"]').count())await page.locator('[data-path="E-report"][data-dir="true"]').click();
  await page.locator('[data-path="E-report/build"][data-dir="true"]').waitFor();
  if(!await page.locator('[data-path="E-report/build/report.html"]').count())await page.locator('[data-path="E-report/build"][data-dir="true"]').click();
  for(const ext of ['html','docx']){
   const file='E-report/build/report.'+ext;
   await page.locator(`[data-path="${file}"][data-dir="false"]`).click();
   await page.waitForFunction(file=>{
    const panel=document.querySelector('[data-testid="document-reader"]');
    return panel?.getAttribute('data-file')===file && panel.querySelector('[data-testid="document-shadow"]')?.shadowRoot?.querySelector('table');
   },file);
   const host=page.getByTestId('document-shadow');
   await host.evaluate(async h=>{await Promise.all([...h.shadowRoot.querySelectorAll('img')].map(i=>i.decode()));});
   const actual=await host.evaluate(h=>{
    const a=h.shadowRoot.querySelector('article');return {text:a.textContent,rows:a.querySelectorAll('table tr').length,
     math:a.querySelectorAll('math').length,figures:a.querySelectorAll('img,svg').length,nestedList:!!a.querySelector('ol ul'),
     bold:[...a.querySelectorAll('strong')].map(n=>n.textContent),italic:[...a.querySelectorAll('em')].map(n=>n.textContent),
     code:[...a.querySelectorAll('pre')].map(n=>n.innerText),links:[...a.querySelectorAll('a')].map(a=>({text:a.textContent,href:a.getAttribute('href')}))};
   });result[ext]=actual;save();
   assert.equal(actual.rows,101);assert.equal(actual.math,3);assert(actual.figures>0);assert(actual.nestedList);
   assert(actual.bold.includes('加粗')&&actual.italic.includes('斜体'));
   assert(actual.code.some(text=>text.includes('temperature = 300\nprint(temperature)')));
   for(let i=1;i<=100;i++)assert(actual.text.includes('ROW-'+String(i).padStart(3,'0')));
   for(const marker of ['FOOTNOTE-ORIGINAL','FIGURE-ORIGINAL','END-OF-REPORT','中文表格内容 100'])assert(actual.text.includes(marker),marker);
   await host.getByRole('link',{name:'结果章节',exact:true}).click();
   await page.getByTestId('document-notice').filter({hasText:'已定位 结果章节'}).waitFor();
   const target=await host.evaluate(h=>({scroll:h.scrollTop,focused:h.shadowRoot.activeElement?.textContent}));assert(target.scroll>500);assert.equal(target.focused,'结果章节');
   await page.screenshot({path:path.join(directory,ext+'-semantic-bottom.png')});
   await page.getByTestId('document-heading').filter({hasText:'排版内容'}).click();
   await page.screenshot({path:path.join(directory,ext+'-semantic-top.png')});
   const pending=page.waitForEvent('download');await page.getByTestId('document-download').click();
   const stream=await(await pending).createReadStream();const chunks=[];for await(const b of stream)chunks.push(b);
   assert(Buffer.concat(chunks).equals(fs.readFileSync(path.join(directory,'report.'+ext))));
   result.checks.push('actual_'+ext+'_100_table_rows_nested_lists_emphasis_code_3_equations_SVG_footnote_bookmark_and_exact_download');save();
   console.log('OFFICE_BROWSER_PASS',ext);
  }
  assert.deepEqual(result.page_errors,[]);result.passed=true;save();
 }catch(error){result.error=String(error.stack||error);save();throw error;}
 finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
