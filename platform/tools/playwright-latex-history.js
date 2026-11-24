async page => {
  const assert=(ok,message)=>{if(!ok)throw Error(message);};
  const fixture=await page.evaluate(()=>JSON.parse(localStorage.getItem('latex-history-fixture')));
  assert(fixture&&new URL(page.url()).hostname==='127.0.0.1','Owned native LaTeX fixture required');
  const token=await page.evaluate(()=>localStorage.getItem('vf_token'));
  const origin=new URL(page.url()).origin,headers={Authorization:'Bearer '+token};
  const base=`${origin}/api/v1/cases/${fixture.caseId}`,old=fixture.versions[0].id,latest=fixture.versions[1].id;
  const url=base+'/latex/artifact?publication_id='+old+'&path='+encodeURIComponent(fixture.pdf);
  const checks=[];
  const head=await page.request.head(url,{headers});assert(head.ok(),'Published HEAD failed');
  const version=head.headers()['x-vibeflow-file-version'],length=Number(head.headers()['content-length']);
  assert(/^[0-9a-f]{64}$/.test(version)&&length>1000&&(await head.body()).length===0,'HEAD proof invalid');
  const full=await page.request.get(url,{headers});assert(full.ok(),'Historical PDF unavailable');
  const bytes=await full.body();assert(bytes.length===length,'Historical length differs');
  const part=await page.request.get(url,{headers:{...headers,Range:'bytes=0-99','X-Vibeflow-File-Version':version}});
  assert(part.status()===206&&part.headers()['content-range']===`bytes 0-99/${length}`
    &&(await part.body()).equals(bytes.subarray(0,100)),'Historical range differs');
  const wrong=await page.request.get(url,{headers:{...headers,Range:'bytes=0-99','X-Vibeflow-File-Version':'0'.repeat(64)}});
  assert(wrong.status()===409,'Mismatched version accepted');
  checks.push('real_native_immutable_HEAD_range_full_download_and_wrong_version_refusal');
  const list=await page.request.get(base+'/latex/publications?main='+encodeURIComponent(fixture.main)+'&limit=1',{headers});
  const first=await list.json();assert(first.length===1&&first[0].id===latest,'Latest page differs');
  const next=await page.request.get(base+'/latex/publications?main='+encodeURIComponent(fixture.main)+'&limit=1&before_id='+first[0].id,{headers});
  assert((await next.json())[0].id===old,'Historical page lost native report');
  checks.push('real_native_versions_reachable_with_channel_cursor');
  await page.setViewportSize({width:1440,height:1000});await page.reload();
  await page.locator('[data-case-name="latex-history-reader"]').click();
  await page.evaluate(async path=>{const {useStore}=await import('/src/store.ts');const {activatePanel}=await import('/src/shell/dock.ts');await useStore.getState().openFile(path);activatePanel('viewer');},fixture.pdf);
  const ready=text=>page.waitForFunction(text=>document.querySelector('.textLayer')?.textContent?.includes(text),text);
  await ready('Introduction');await page.getByTestId('pdf-next').click();await ready('NewResultText');
  await page.getByTestId('report-history-toggle').click();
  await page.locator(`[data-publication-id="${old}"]`).click();await ready('UniqueResultText');
  assert((await page.getByTestId('pdf-publication').textContent()).includes('历史发布，不代表当前有效结果'),'Historical version mislabeled');
  checks.push('PDFjs_reads_selected_older_publication_and_distinguishes_current_status');
  const pending=page.waitForEvent('download');await page.getByTestId('pdf-download').click();
  const download=await pending,stream=await download.createReadStream(),chunks=[];
  for await(const chunk of stream)chunks.push(chunk);
  assert(Buffer.concat(chunks).equals(bytes),'Viewer download does not match displayed old PDF');
  checks.push('download_matches_selected_old_native_PDF_bytes');
  let queryBody;
  const listener=request=>{if(request.url().endsWith('/synctex:query'))queryBody=request.postDataJSON();};
  page.on('request',listener);
  try {
    await page.locator('.textLayer').evaluate(element=>{
      const bounds=element.parentElement.getBoundingClientRect();
      const scale=Number(document.querySelector('[data-testid="pdf-viewer"]').dataset.scale);
      element.dispatchEvent(new MouseEvent('dblclick',{bubbles:true,clientX:bounds.left+134*scale,clientY:bounds.top+157*scale}));
    });
    await page.getByTestId('pdf-frozen-source').waitFor();
  } finally {page.off('request',listener);}
  assert(queryBody?.publication_id===old,'Source query did not use displayed publication');
  assert((await page.getByTestId('pdf-frozen-source').textContent()).includes('UniqueResultText'),'Current source substituted for historical');
  assert(await page.locator('.pdf-source-current').getAttribute('data-line')==='7','Wrong old source line');
  const current=await page.request.get(base+'/file?path='+encodeURIComponent(fixture.main),{headers});
  assert((await current.json()).content.includes('NewResultText'),'Source lookup changed working source');
  checks.push('real_SyncTeX_uses_old_publication_and_shows_read_only_old_source_line_without_working_edit');
  await page.screenshot({path:'D:/vibeflow_reverse/tmp/N2-latex-history-source.png'});
  await page.getByRole('button',{name:'关闭源码',exact:true}).click();
  await page.getByTestId('pdf-reload').click();await ready('NewResultText');
  checks.push('explicit_reload_returns_latest_version_and_clears_old_source');
  const pattern='**/latex/results?**';
  await page.route(pattern,route=>{
    if(new URL(route.request().url()).searchParams.get('publication_id')===old)
      return route.fulfill({status:404,contentType:'application/json',body:'{}'});
    return route.continue();
  });
  try {
    await page.locator(`[data-publication-id="${old}"]`).click();
    await page.getByTestId('pdf-error').filter({hasText:'无法核验 PDF 发布版本 (404)'}).waitFor();
    assert(await page.getByTestId('pdf-viewer').count()===0,'Failed history remains interactive');
  } finally {await page.unroute(pattern);}
  await page.getByTestId('pdf-reload').click();await ready('NewResultText');
  checks.push('missing_selected_history_cannot_fallback_to_mutable_alias_and_reload_recovers');
  const result={passed:true,count:checks.length,checks,fixture:{caseId:fixture.caseId,old,latest,pdf:fixture.pdf}};
  await page.evaluate(value=>localStorage.setItem('latex-history-result',JSON.stringify(value)),result);
  return result;
}
