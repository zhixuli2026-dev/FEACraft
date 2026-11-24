// Real API + IndexedDB recovery acceptance. Run only against a disposable Vite preview.
async page => {
  page.setDefaultTimeout(90000);
  const assert=(v,message)=>{if(!v)throw Error(message);};
  const origin=new URL(page.url());
  assert(origin.hostname==="127.0.0.1"&&!["8000","5173"].includes(origin.port),"Disposable preview required");
  const checks=[],mutations=[],routes=[];
  const fixture=await page.evaluate(async()=>{
    const a=await fetch('/api/v1/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:`resume-${Date.now()}@example.com`,name:'resume-test',password:'isolated-resume-123'})}).then(r=>r.json());
    if(!a.token)throw Error('register failed');localStorage.setItem('vf_token',a.token);
    const headers={Authorization:'Bearer '+a.token,'Content-Type':'application/json'};
    const workspaces=await fetch('/api/v1/workspaces',{headers}).then(r=>r.json());
    const cases=[];
    for(const name of ['resume-browser','resume-other'])cases.push(await fetch(`/api/v1/workspaces/${workspaces[0].id}/cases`,
      {method:'POST',headers,body:JSON.stringify({name})}).then(r=>r.json()));
    return {caseId:cases[0].id,otherId:cases[1].id,scope:`${a.user.id}:${cases[0].id}`};
  });
  const prefix=`/api/v1/cases/${fixture.caseId}`;
  const recordRequest=req=>{if(req.url().startsWith(origin.origin+prefix)&&['POST','PUT','DELETE'].includes(req.method()))
    mutations.push({method:req.method(),path:new URL(req.url()).pathname,offset:new URL(req.url()).searchParams.get('offset')});};
  page.on('request',recordRequest);
  const call=(method,path,body)=>page.evaluate(async({method,path,body})=>{
    const r=await fetch(path,{method,headers:{Authorization:'Bearer '+localStorage.getItem('vf_token'),'Content-Type':'application/json'},
      ...(body===undefined?{}:{body:JSON.stringify(body)})});return {status:r.status,body:await r.json()};
  },{method,path,body});
  const version=async name=>(await call('GET',prefix+'/file:version?path='+encodeURIComponent('Ref/'+name))).body;
  const put=async(name,content)=>call('PUT',prefix+'/file',{path:'Ref/'+name,content,expected_sha256:(await version(name)).content_sha256});
  const form=page.getByTestId('file-operation-form'),submit=page.getByTestId('file-operation-submit');
  const success=()=>form.waitFor({state:'detached'});
  const idle=()=>page.waitForFunction(()=>!document.querySelector('[data-testid="file-operation-cancel"]')?.disabled);
  const read=()=>page.evaluate(()=>new Promise((resolve,reject)=>{
    const request=indexedDB.open('vf-upload-resume',1);request.onerror=()=>reject(request.error);
    request.onsuccess=()=>{const db=request.result,tx=db.transaction('uploads'),r=tx.objectStore('uploads').getAll();
      r.onsuccess=()=>resolve(r.result);tx.oncomplete=()=>db.close();};
  }));
  const write=value=>page.evaluate(value=>new Promise((resolve,reject)=>{
    const request=indexedDB.open('vf-upload-resume',1);request.onerror=()=>reject(request.error);
    request.onsuccess=()=>{const db=request.result,tx=db.transaction('uploads','readwrite');tx.objectStore('uploads').put(value);
      tx.oncomplete=()=>{db.close();resolve(true);};tx.onabort=()=>reject(tx.error);};
  }),value);
  const reload=async()=>{await page.reload();await page.locator('[data-case-name="resume-browser"]').click();await page.getByTestId('case-file-actions').waitFor();};
  const data=Buffer.alloc(17*1024*1024+13,83);
  const select=async(name,buffer=data)=>{
    await page.getByTestId('tree-upload-file').setInputFiles({name,mimeType:'application/octet-stream',buffer});
    await page.getByTestId('file-operation-inspect').click();
    await page.waitForFunction(()=>!document.querySelector('[data-testid="file-operation-submit"]')?.disabled);
  };
  const selectOriginal=async(name,buffer=data)=>{
    await idle();await page.getByTestId('file-resume-source').setInputFiles({name,mimeType:'application/octet-stream',buffer});
  };
  const cancel=async()=>{await idle();await page.getByTestId('file-operation-cancel').click();await success();};
  const drop=async(pattern,method)=>{
    let used=false;
    const handler=async route=>{
      if(used||route.request().method()!==method)return route.continue();used=true;
      await route.fetch();await route.abort('failed');
    };
    await page.route(pattern,handler);routes.push([pattern,handler]);
    return ()=>page.unroute(pattern,handler);
  };
  const interrupted=async name=>{
    await select(name);const undo=await drop('**'+prefix+'/uploads/*?offset=*','PUT');
    await submit.click();await page.getByTestId('file-operation-error').filter({hasText:'网络请求中断'}).waitFor();await undo();
    const row=(await read()).find(r=>r.scope===fixture.scope&&r.path==='Ref/'+name);assert(row?.upload,'No persisted upload');return row;
  };
  try {
    await reload();
    const row=await interrupted('reload.bin'),before=mutations.length;
    assert(row.upload.offset===0,'Lost response fixture must leave stale client offset');
    assert(JSON.stringify(row).length<4096&&!('file' in row)&&!('token' in row),'Persisted bytes or credentials');
    await reload();await idle();
    assert(await page.getByRole('progressbar',{name:'文件上传进度'}).getAttribute('value')==='4194304','Server offset not restored');
    assert(await submit.isDisabled(),'Resume enabled without original file');
    assert(mutations.length===before,'Reload mutated server');
    checks.push('reload_recovers_server_offset_without_mutation_or_file_bytes_in_storage');
    await selectOriginal('reload.bin',Buffer.alloc(data.length,84));
    await page.getByTestId('file-operation-error').filter({hasText:'内容与原文件不同'}).waitFor();
    assert(await submit.isDisabled()&&mutations.length===before,'Wrong source wrote bytes');
    checks.push('same_name_size_different_contents_rejected_before_network_write');
    await page.setViewportSize({width:768,height:1000});
    await page.getByTestId('case-file-actions').evaluate(e=>{e.style.width='230px';});
    for(const theme of ['dark','light']) {
      await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
      const layout=await page.getByTestId('case-file-actions').evaluate(e=>({width:e.clientWidth,scroll:e.scrollWidth}));
      assert(layout.scroll<=layout.width+1,'Resume UI overflow '+theme);
      await page.keyboard.press('Tab');await page.getByRole('button',{name:'重新选择原文件',exact:true}).focus();
      assert(await page.getByRole('button',{name:'重新选择原文件',exact:true}).evaluate(e=>getComputedStyle(e).outlineStyle)!=='none','Missing focus outline');
      await page.getByTestId('case-file-actions').screenshot({path:`tmp/mainline-N2-browser-upload-evidence/resume-${theme}-narrow.png`});
    }
    await page.setViewportSize({width:1440,height:1000});
    await page.getByTestId('case-file-actions').evaluate(e=>e.style.removeProperty('width'));
    await page.evaluate(()=>document.documentElement.dataset.theme='dark');
    checks.push('dark_light_230px_panel_wrap_and_keyboard_focus');
    await selectOriginal('renamed-copy.bin');await page.getByTestId('file-operation-notice').filter({hasText:'校验通过'}).waitFor();
    await submit.click();await success();
    const offsets=mutations.filter(r=>r.path.endsWith(row.upload.id)&&r.method==='PUT').map(r=>Number(r.offset));
    assert(offsets.join(',')==='0,4194304,8388608,12582912,16777216','Duplicated chunk after reload');
    const equal=await page.evaluate(async({prefix,size})=>{
      const r=await fetch(prefix+'/raw?path=Ref/reload.bin',{headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')}});
      const bytes=new Uint8Array(await r.arrayBuffer());return bytes.length===size&&bytes.every(b=>b===83);
    },{prefix,size:data.length});
    assert(equal,'Final file bytes differ');checks.push('renamed_matching_source_resumes_without_duplicate_chunks_and_matches_all_bytes');

    await select('receipt.bin');let undo=await drop('**'+prefix+'/uploads/*:complete','POST');
    await submit.click();await page.getByTestId('file-operation-error').filter({hasText:'网络请求中断'}).waitFor();await undo();
    const edited=await put('receipt.bin','LATER EDIT');
    assert(edited.status===200,'Later edit rejected: '+edited.status+' '+JSON.stringify(edited.body));
    assert((await version('receipt.bin')).size===10,'Later edit not visible before reload');
    const afterEdit=mutations.length;
    await reload();await page.getByTestId('file-operation-notice').filter({hasText:'已上传 Ref/receipt.bin'}).waitFor();
    const reloaded=await call('GET',prefix+'/file:version?path='+encodeURIComponent('Ref/receipt.bin'));
    assert(mutations.length===afterEdit,'Receipt recovery made unexpected requests: '+JSON.stringify(mutations.slice(afterEdit)));
    assert(reloaded.status===200&&reloaded.body.size===10,'Receipt recovery could not verify the saved edit: '+JSON.stringify(reloaded));
    assert(!(await read()).some(r=>r.scope===fixture.scope&&r.path==='Ref/receipt.bin'),'Completed record retained');
    checks.push('reload_resolves_committed_receipt_without_source_or_republish_and_preserves_later_edit');

    const missing=await interrupted('missing.bin');await call('DELETE',prefix+'/uploads/'+missing.upload.id);
    const beforeMissing=mutations.length;await reload();
    await page.getByTestId('file-operation-error').filter({hasText:'找不到这次上传'}).waitFor();
    assert(mutations.length===beforeMissing&&!(await version('missing.bin')).exists,'Missing session recreated automatically');
    await cancel();checks.push('missing_server_session_preserves_uncertainty_until_explicit_cancel');

    const scoped=await interrupted('isolation.bin');
    await page.locator('[data-case-name="resume-other"]').click();await page.getByTestId('case-file-actions').waitFor();
    assert(await form.count()===0,'Upload leaked into other case');
    await reload();await idle();assert(await form.count()===1,'Original case lost upload');
    const otherTab=await page.context().newPage();
    try {
      await otherTab.goto(origin.origin);await otherTab.locator('[data-case-name="resume-browser"]').click();
      await otherTab.getByTestId('file-upload-status').waitFor();
      await otherTab.waitForFunction(()=>!document.querySelector('[data-testid="file-operation-cancel"]')?.disabled);
      await otherTab.getByTestId('file-operation-cancel').click();await otherTab.getByTestId('file-operation-form').waitFor({state:'detached'});
      await page.getByTestId('file-upload-status').click();
      await page.getByTestId('file-operation-error').filter({hasText:'找不到这次上传'}).waitFor();
      assert(!(await read()).some(r=>r.clientId===scoped.clientId),'Stale tab recreated cancelled record');
    } finally {await otherTab.close();}
    await cancel();checks.push('case_isolation_and_other_tab_cancel_cannot_resurrect_record');

    // A valid record under a different account scope remains invisible and untouched.
    const foreign={...scoped,scope:`999999:${fixture.caseId}`};await write(foreign);await reload();
    await page.waitForFunction(()=>!document.querySelector('[data-testid="tree-upload-file"]')?.disabled);
    assert(await form.count()===0&&(await read()).some(r=>r.scope===foreign.scope),'Other account record rebound');
    checks.push('other_account_scope_is_not_loaded_or_removed');

    const corrupt=await interrupted('corrupt.bin');await call('DELETE',prefix+'/uploads/'+corrupt.upload.id);
    await write({...corrupt,source:{...corrupt.source,blocks:['bad']}});await reload();
    await page.getByTestId('file-operation-error').filter({hasText:'本机上传记录已损坏'}).waitFor();
    const count=mutations.length;await page.getByRole('button',{name:'清除本机损坏记录'}).click();
    await page.getByTestId('file-operation-notice').filter({hasText:'已清除本机损坏记录'}).waitFor();
    assert(mutations.length===count,'Corrupt record caused server deletion');checks.push('corrupt_metadata_requires_explicit_local_only_clear');

    // Block the browser database only in this tab; large upload creation fails safely.
    await page.evaluate(()=>{window.__realIdbOpen=indexedDB.open.bind(indexedDB);indexedDB.open=()=>{throw new DOMException('disabled','SecurityError');};});
    await select('storage-blocked.bin');const blockedBefore=mutations.length;await submit.click();
    await page.getByTestId('file-operation-error').filter({hasText:'无法保存本机上传进度'}).waitFor();
    assert(mutations.length===blockedBefore,'Upload started before durable metadata existed');
    await cancel();await select('small.txt',Buffer.from('inline remains available'));await submit.click();await success();
    await page.evaluate(()=>{indexedDB.open=window.__realIdbOpen;delete window.__realIdbOpen;});
    checks.push('unavailable_browser_storage_blocks_large_upload_before_network_but_allows_inline');

    // The server commits creation before the intercepted response is lost.
    await select('create-lost.bin');undo=await drop('**'+prefix+'/uploads','POST');
    await submit.click();await page.getByTestId('file-operation-error').filter({hasText:'网络请求中断'}).waitFor();await undo();
    const prepared=(await read()).find(r=>r.scope===fixture.scope&&r.path==='Ref/create-lost.bin');assert(prepared&&!prepared.upload,'Unknown creation claimed an id');
    const outstanding=(await call('GET',prefix+'/uploads')).body.filter(r=>r.path===prepared.path);
    assert(outstanding.length===1 && outstanding[0].remaining_bytes===data.length,'Expected one charged upload');
    const previousCreates=mutations.filter(r=>r.method==='POST'&&r.path===prefix+'/uploads').length;
    await reload();await selectOriginal('same.bin');await page.getByTestId('file-operation-notice').filter({hasText:'校验通过'}).waitFor();
    await submit.click();await success();assert((await version('create-lost.bin')).size===data.length,'Prepared retry failed');
    assert(mutations.filter(r=>r.method==='POST'&&r.path===prefix+'/uploads').length===previousCreates,'Refresh created a second upload');
    assert(!(await call('GET',prefix+'/uploads')).body.some(r=>r.path===prepared.path),'Finished upload still charged');
    checks.push('lost_creation_response_reuses_original_session_and_releases_reservation');
    const result={passed:true,count:checks.length,checks,caseId:fixture.caseId,real_api:true,isolated:true,
      browser:page.context().browser().version(),file_bytes:data.length};
    await page.evaluate(r=>window.__uploadResumeEvidence=r,result);return result;
  } catch(error) {
    const result={passed:false,checks,error:String(error)};await page.evaluate(r=>window.__uploadResumeEvidence=r,result);throw error;
  } finally {
    for(const [pattern,handler] of routes)await page.unroute(pattern,handler);page.off('request',recordRequest);
  }
}
