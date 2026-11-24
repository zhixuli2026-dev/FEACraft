async page => {
  const assert=(value,message)=>{if(!value)throw Error(message);};
  const fixture=await page.evaluate(()=>JSON.parse(localStorage.getItem('derivative-history-fixture')));
  assert(fixture && new URL(page.url()).hostname==='127.0.0.1','Isolated native fixture required');
  const origin=new URL(page.url()).origin,token=await page.evaluate(()=>localStorage.getItem('vf_token'));
  const headers={Authorization:'Bearer '+token},prefix=`/api/v1/cases/${fixture.caseId}`;
  const checks=[];
  const get=async path=>{const r=await page.request.get(origin+path,{headers});assert(r.ok(),'API '+r.status());return r;};
  await page.reload();await page.locator('[data-case-name="report-derivative-history"]').click();
  await page.evaluate(async()=>{const {activatePanel}=await import('/src/shell/dock.ts');activatePanel('caseinfo');});
  const area=page.getByTestId('report-derivatives');
  await area.getByRole('button',{name:'草稿、页码 PDF 与 PPT 历史'}).click();
  const select=async id=>{await area.locator(`[data-derivative-id="${id}"]`).click();await area.getByTestId('derivative-state').waitFor();};
  const original=fixture.versions['E-report/report.md'][0];
  await select(original);
  const doc=await (await get(prefix+'/report/derivative?publication_id='+original)).json();
  const source=await (await get(prefix+'/report/derivative-artifact?publication_id='+original+'&path='+encodeURIComponent(doc.files[0].path))).text();
  assert(await area.getByLabel('只读历史草稿').textContent()===source,'Historical draft differs from immutable source');
  assert(!(await area.getByLabel('只读历史草稿').textContent()).includes('MANUAL WORKSPACE DRAFT'),'Read mutable alias');
  assert((await area.getByTestId('derivative-state').textContent()).includes('历史发布'),'Old draft labeled valid');
  checks.push('readonly_native_draft_is_original_despite_manual_workspace_replacement');
  async function downloadMatches(id,role='original',button='下载所选版本原件') {
    const document=await (await get(prefix+'/report/derivative?publication_id='+id)).json();
    const file=document.files.find(f=>f.role===role);assert(file,'Missing artifact role');
    const pending=page.waitForEvent('download');await area.getByRole('button',{name:button,exact:true}).click();
    const download=await pending,stream=await download.createReadStream(),chunks=[];
    for await(const chunk of stream)chunks.push(chunk);
    const expected=await (await get(prefix+'/report/derivative-artifact?publication_id='+id+'&path='+encodeURIComponent(file.path))).body();
    assert(Buffer.concat(chunks).equals(expected),'Download differs from selected version');
    return expected;
  }
  await downloadMatches(original);
  const generated=fixture.versions['E-report/report.generated.md'][1];await select(generated);
  await downloadMatches(generated);await downloadMatches(generated,'previous-draft','下载生成前的草稿备份');
  checks.push('both_draft_channels_and_exact_pre_generation_backup_downloads');
  await area.getByRole('combobox').selectOption('ppt');
  const oldDeck=fixture.versions.ppt[0];await select(oldDeck);await downloadMatches(oldDeck);
  assert((await area.getByText('PPTX 原件保留可编辑内容与样式', {exact:false}).count())===1,'PPT download explanation missing');
  checks.push('PPT_history_download_retains_old_native_deck_after_alias_edit');
  await area.getByRole('combobox').selectOption('pdf-number');const pdf=fixture.versions.pdf[0];await select(pdf);
  assert((await area.getByTestId('derivative-state').textContent()).includes('当前发布已过期'),'Changed source not shown stale');
  const frame=area.getByTitle('所选历史页码 PDF');assert((await frame.getAttribute('src')).startsWith('blob:'),'PDF preview not local pinned bytes');
  const expected=await downloadMatches(pdf);
  const preview=await page.evaluate(async()=>Array.from(new Uint8Array(await (await fetch(document.querySelector('iframe[title="所选历史页码 PDF"]').src)).arrayBuffer())));
  assert(Buffer.from(preview).equals(expected),'PDF preview and download differ');
  checks.push('numbered_PDF_preview_download_and_current_stale_status_share_native_bytes');
  await area.getByRole('combobox').selectOption('draft');await select(original);
  const listPattern='**/report/derivatives?**';
  await page.route(listPattern,route=>route.fulfill({status:503,body:'{"detail":"fixture unavailable"}',contentType:'application/json'}));
  try{
    await area.getByRole('button',{name:'刷新产物列表'}).click();await area.getByRole('alert').waitFor();
    assert(await area.getByLabel('只读历史草稿').textContent()===source,'List error erased selected version');
  }finally{await page.unroute(listPattern);}
  const refreshed=page.waitForResponse(r=>r.url().includes('/report/derivatives?')&&r.ok());
  await area.getByRole('button',{name:'重试列表'}).click();assert(!new URL((await refreshed).url()).searchParams.has('before_id'),'Refresh retry used old cursor');
  await area.getByRole('alert').waitFor({state:'hidden'});
  checks.push('refresh_failure_retains_selection_and_retries_first_page');
  const selectedPattern='**/report/derivative?publication_id='+original;
  await area.getByRole('button',{name:'关闭版本'}).click();
  await page.route(selectedPattern,route=>route.fulfill({status:404,body:'{}',contentType:'application/json'}));
  try{
    await area.locator(`[data-derivative-id="${original}"]`).click();await area.getByRole('alert').waitFor();
    assert(await area.getByRole('button',{name:'下载所选版本原件'}).count()===0,'Missing version allows download');
    assert(await area.getByLabel('只读历史草稿').count()===0,'Missing version fell back to draft alias');
  }finally{await page.unroute(selectedPattern);}
  await area.getByRole('button',{name:'重新读取版本'}).click();await area.getByLabel('只读历史草稿').waitFor();
  checks.push('explicit_missing_version_never_falls_back_and_retry_recovers');
  const artifactPattern='**/report/derivative-artifact?publication_id='+original+'&*';
  await page.route(artifactPattern,route=>route.fulfill({status:200,body:'tampered native artifact'}));
  try{
    await area.getByRole('button',{name:'下载所选版本原件'}).click();
    await area.getByRole('status').filter({hasText:'报告原件与所选版本不一致'}).waitFor();
  }finally{await page.unroute(artifactPattern);}
  await downloadMatches(original);checks.push('digest_mismatch_is_refused_before_download_and_retry_recovers');
  // Exercise the existing generation button's changed download path as well.
  const output='E-report/ui-pinned-'+Date.now()+'.pptx';
  await page.getByTestId('ppt-output').fill(output);await page.getByTestId('ppt-build').click();
  await page.getByTestId('ppt-status').filter({hasText:'PPTX 已生成'}).waitFor({timeout:120000});
  const list=await (await get(prefix+'/report/derivatives?family=ppt&limit=100')).json();
  const published=list.find(row=>row.output===output);assert(published,'UI build has no publication');
  const native=await (await get(prefix+'/report/derivative?publication_id='+published.id)).json();
  const edited=await page.request.put(origin+prefix+'/file',{headers,data:{path:output,content:'CHANGED AFTER UI BUILD'}});assert(edited.ok(),'Alias replacement');
  const waiting=page.waitForEvent('download');await page.getByTestId('ppt-download').click();const file=await waiting,stream=await file.createReadStream(),chunks=[];
  for await(const chunk of stream)chunks.push(chunk);
  const immutable=await (await get(prefix+'/report/derivative-artifact?publication_id='+published.id+'&path='+encodeURIComponent(native.files[0].path))).body();
  assert(Buffer.concat(chunks).equals(immutable),'Generation shortcut downloaded mutable alias');
  checks.push('existing_PPT_generation_download_is_bound_to_completed_job_publication');
  await area.getByRole('combobox').selectOption('draft');await select(original);
  await area.getByLabel('只读历史草稿').focus();
  await page.setViewportSize({width:1440,height:1000});await area.getByLabel('只读历史草稿').scrollIntoViewIfNeeded();
  await page.screenshot({path:'D:/vibeflow_reverse/tmp/N2-derivative-history-desktop.png'});
  await page.setViewportSize({width:1100,height:800});await area.getByLabel('只读历史草稿').scrollIntoViewIfNeeded();
  await page.screenshot({path:'D:/vibeflow_reverse/tmp/N2-derivative-history-narrow.png'});
  const result={passed:true,count:checks.length,checks,caseId:fixture.caseId};
  await page.evaluate(value=>localStorage.setItem('derivative-history-result',JSON.stringify(value)),result);
  return result;
}
