// Recovery edges: last chunk response loss, file-free completion, cancellation after commit.
async page=>{
  const assert=(v,message)=>{if(!v)throw Error(message);};const checks=[];
  const context=await page.evaluate(async()=>{
    const {useStore}=await import('/src/store.ts');return {id:useStore.getState().currentCase.id,name:useStore.getState().currentCase.name};
  });
  const prefix=`/api/v1/cases/${context.id}`,data=Buffer.alloc(17*1024*1024+13,67);
  const select=async name=>{
    await page.waitForFunction(()=>!document.querySelector('[data-testid="tree-upload-file"]')?.disabled);
    await page.getByTestId('tree-upload-file').setInputFiles({name,mimeType:'application/octet-stream',buffer:data});
    await page.getByTestId('file-operation-inspect').click();
    await page.waitForFunction(()=>!document.querySelector('[data-testid="file-operation-submit"]')?.disabled);
  };
  const drop=async(pattern)=>{
    let used=false;const handler=async route=>{if(used)return route.continue();used=true;await route.fetch();await route.abort('failed');};
    await page.route(pattern,handler);return ()=>page.unroute(pattern,handler);
  };
  const reload=async()=>{await page.reload();await page.locator(`[data-case-name="${context.name}"]`).click();};
  await select('file-free-finish.bin');
  let undo=await drop('**'+prefix+'/uploads/*?offset=16777216');
  try {
    await page.getByTestId('file-operation-submit').click();
    await page.getByTestId('file-operation-error').filter({hasText:'网络请求中断'}).waitFor();
  } finally {await undo();}
  await reload();
  await page.getByTestId('file-operation-notice').filter({hasText:'所有分块已上传'}).waitFor();
  assert(await page.getByTestId('file-operation-submit').isEnabled(),'Complete requires missing file');
  await page.getByRole('button',{name:'完成上传',exact:true}).click();
  await page.getByTestId('file-operation-form').waitFor({state:'detached'});
  const final=await page.evaluate(async prefix=>{
    const r=await fetch(prefix+'/raw?path=Ref/file-free-finish.bin',{headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')}});
    const b=new Uint8Array(await r.arrayBuffer());return {size:b.length,equal:b.every(v=>v===67)};
  },prefix);
  assert(final.size===data.length&&final.equal,'File-free completion changed bytes');
  checks.push('lost_final_chunk_response_allows_completion_after_reload_without_file_reselection');

  await select('cancel-after-commit.bin');undo=await drop('**'+prefix+'/uploads/*:complete');
  try {
    await page.getByTestId('file-operation-submit').click();
    await page.getByTestId('file-operation-error').filter({hasText:'网络请求中断'}).waitFor();
  } finally {await undo();}
  await page.evaluate(async prefix=>{
    const headers={Authorization:'Bearer '+localStorage.getItem('vf_token'),'Content-Type':'application/json'};
    const path='Ref/cancel-after-commit.bin';
    const version=await fetch(prefix+'/file:version?path='+encodeURIComponent(path),{headers});
    if(!version.ok)throw Error('fixture version failed '+version.status);
    const r=await fetch(prefix+'/file',{method:'PUT',headers,
      body:JSON.stringify({path,content:'LATER EDIT',expected_sha256:(await version.json()).content_sha256})});
    if(!r.ok)throw Error('fixture edit failed '+r.status+' '+await r.text());
  },prefix);
  await page.getByTestId('file-operation-cancel').click();
  await page.getByTestId('file-operation-notice').filter({hasText:'已上传 Ref/cancel-after-commit.bin'}).waitFor();
  const size=await page.evaluate(async prefix=>(await fetch(prefix+'/file:version?path=Ref/cancel-after-commit.bin',
    {headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')}}).then(r=>r.json())).size,prefix);
  assert(size===10,'Cancel after committed receipt modified target');
  checks.push('cancel_after_unknown_commit_reports_completed_and_preserves_later_edit');

  await select('reselect-while-paused.bin');undo=await drop('**'+prefix+'/uploads/*?offset=0');
  try {
    await page.getByTestId('file-operation-submit').click();
    await page.getByTestId('file-operation-error').filter({hasText:'网络请求中断'}).waitFor();
  } finally {await undo();}
  await page.getByTestId('file-resume-source').setInputFiles({name:'different.bin',mimeType:'application/octet-stream',buffer:Buffer.alloc(data.length,68)});
  await page.getByTestId('file-operation-error').filter({hasText:'内容与原文件不同'}).waitFor();
  assert(await page.getByTestId('file-operation-submit').isDisabled(),'Rejected reselection left stale source enabled');
  await page.getByTestId('file-resume-source').setInputFiles({name:'original-copy.bin',mimeType:'application/octet-stream',buffer:data});
  await page.getByTestId('file-operation-notice').filter({hasText:'校验通过'}).waitFor();
  assert(await page.getByTestId('file-operation-submit').isEnabled(),'Matching reselection cannot resume');
  await page.getByTestId('file-operation-cancel').click();await page.getByTestId('file-operation-form').waitFor({state:'detached'});
  checks.push('reselect_with_retained_file_rejects_wrong_content_and_accepts_matching_copy');
  const result={passed:true,count:checks.length,checks,real_api:true,browser:page.context().browser().version()};
  await page.evaluate(r=>window.__uploadCompletionEvidence=r,result);return result;
}
