// Real owned API + Vite browser. No request interception or fabricated replies.
async page => {
  const assert=(value,message)=>{if(!value)throw Error(message);};
  const origin=new URL(page.url());assert(origin.hostname==='127.0.0.1'&&!['8000','5173'].includes(origin.port),'Owned UI required');
  const checks=[],mutations=[],measurements=[];
  const call=(method,path,body)=>page.evaluate(async({method,path,body})=>{
    const response=await fetch('/api/v1'+path,{method,headers:{Authorization:'Bearer '+localStorage.getItem('vf_token'),'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});
    const text=await response.text();let value;
    try{value=JSON.parse(text);}catch{value={detail:`Non-JSON HTTP ${response.status}: ${text.slice(0,160)}`};}
    return {status:response.status,body:value};
  },{method,path,body});
  const admittedCall=async(method,path,body)=>{
    const until=Date.now()+20000;
    while(true){
      const value=await call(method,path,body);
      if(value.status===409&&value.body.detail?.code==='storage_quota_busy'&&Date.now()<until){await page.waitForTimeout(200);continue;}
      assert(value.status===200,`${method} ${path}: HTTP ${value.status} ${JSON.stringify(value.body).slice(0,500)}`);return value;
    }
  };
  let setup=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('owner-browser-setup')||'null'));
  if(!setup){
    setup=await page.evaluate(async()=>{
      const call=async(method,path,body,token)=>{const response=await fetch('/api/v1'+path,{method,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},...(body===undefined?{}:{body:JSON.stringify(body)})});if(!response.ok)throw Error(method+' '+path+' '+response.status);return response.json();};
      const auth=await call('POST','/auth/register',{email:'owner-browser-'+Date.now()+'@example.com',password:'isolated-browser-123',name:'Actual upload owner browser'});localStorage.setItem('vf_token',auth.token);
      const workspace=(await call('GET','/workspaces',undefined,auth.token))[0];
      const c=await call('POST','/workspaces/'+workspace.id+'/cases',{name:'Legacy upload ownership browser'},auth.token);
      const member=await call('POST','/auth/register',{email:'member-browser-'+Date.now()+'@example.com',password:'isolated-browser-123',name:'Actual collaborator'});
      const me=await call('GET','/auth/me',undefined,member.token);
      const share=await call('POST',`/testing/owner-share/${c.id}/${me.id}`,{},auth.token);
      await call('PUT',`/workspaces/${share.workspace_id}/sharing`,{org_id:share.org_id,role:'member',enabled:true},auth.token);
      const path='Ref/'+('long-legacy-file-'.repeat(8))+'data.bin';
      const upload=await call('POST',`/cases/${c.id}/uploads`,{path,size:524288},auth.token);
      const sent=await fetch(`/api/v1/cases/${c.id}/uploads/${upload.id}?offset=0`,{method:'PUT',headers:{Authorization:'Bearer '+auth.token},body:new Uint8Array(262144).fill(83)});if(!sent.ok)throw Error('Actual part '+sent.status);
      await call('POST',`/testing/owner-seed/${c.id}/${upload.id}`,{user_id:me.id},auth.token);
      const value={caseId:c.id,uploadId:upload.id,memberId:me.id,path,caseName:c.name};sessionStorage.setItem('owner-browser-setup',JSON.stringify(value));return value;
    });
    await page.reload();await page.locator(`[data-case-name="${setup.caseName}"]`).click();
  }
  const prefix=`/cases/${setup.caseId}`,target=`${prefix}/uploads/${setup.uploadId}`;
  const ownerToken=await page.evaluate(()=>localStorage.getItem('vf_token'));
  const observer=request=>{if(request.method()==='POST'&&new URL(request.url()).pathname.endsWith(':reconcile-owner'))mutations.push(new URL(request.url()).pathname);};
  page.on('request',observer);
  try{
    await page.getByTestId('file-legacy-inspect').click();await page.getByTestId('upload-owner-row').waitFor();
    const row=page.getByTestId('upload-owner-row').filter({hasText:setup.path});
    assert(await row.count()===1&&await row.getByTestId('upload-owner-reconcile').count()===1,'Actual legacy row/action missing');
    assert(mutations.length===0,'Inspecting silently moved quota');checks.push('actual_legacy_list_visible_without_automatic_mutation');
    for(const width of [1440,768,390]){
      await page.setViewportSize({width,height:1000});
      for(const theme of ['dark','light']){
        await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
        const panel=page.getByTestId('case-file-actions');
        await panel.evaluate(e=>{e.style.width='230px';e.style.maxWidth='100%';});
        await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
        const size=await panel.evaluate(e=>({client:e.clientWidth,scroll:e.scrollWidth,width:e.getBoundingClientRect().width,parent:e.parentElement.getBoundingClientRect().width}));
        assert(size.scroll<=size.client+1&&size.width<=size.parent+1,'Legacy row overflow '+JSON.stringify(size));
        const button=row.getByTestId('upload-owner-reconcile');
        let focused=false;const focusUntil=Date.now()+1000;
        while(!focused&&Date.now()<focusUntil){
          await page.waitForTimeout(100);await page.keyboard.press('Tab');await button.focus();
          focused=await button.evaluate(e=>e===document.activeElement&&e.matches(':focus-visible')&&getComputedStyle(e).outlineStyle!=='none');
        }
        assert(focused,'Keyboard focus missing after dock layout settled');
        await panel.screenshot({path:`D:/vibeflow_reverse/tmp/upload-owner-ui-render-${origin.port}-${width}-${theme}.png`});
        measurements.push({viewport:width,theme,...size});
      }
    }
    await page.setViewportSize({width:1440,height:1000});await page.getByTestId('case-file-actions').evaluate(e=>e.style.removeProperty('width'));
    await page.evaluate(()=>document.documentElement.dataset.theme='dark');
    checks.push('actual_long_path_230px_panel_three_viewports_both_themes_and_keyboard_focus');
    await call('POST','/testing/owner-limit',{storage_mb:0});
    await row.getByTestId('upload-owner-reconcile').click();
    await page.getByTestId('upload-owner-error').waitFor();
    const quotaRejection=await page.getByTestId('upload-owner-error').innerText();
    assert(quotaRejection.includes('429')||quotaRejection.includes('409')&&quotaRejection.includes('远端写入尚未结算'),'Unexpected zero-plan rejection '+quotaRejection);
    assert(await row.count()===1,'Quota failure removed legacy row');
    let state=await call('GET',`/testing/owner-ledger/${setup.caseId}/${setup.uploadId}`);
    assert(state.body.user_id===setup.memberId&&state.body.events.length===0,'Quota refusal changed owner/audit');
    await call('POST','/testing/owner-limit',{storage_mb:64});checks.push('real_zero_plan_quota_or_active_credit_refusal_retains_row_and_old_owner');
    const pause=await call('POST','/testing/owner-commit-control',{action:'pause_commit',upload_id:setup.uploadId});assert(pause.status===200,'Commit control was not admitted');
    let before=mutations.length,entered=false,busyRefusals=0;
    const until=Date.now()+20000;
    while(!entered){
      before=mutations.length;await row.getByTestId('upload-owner-reconcile').click();
      while(Date.now()<until){
        const control=await call('GET','/testing/owner-commit-control');assert(control.status===200,'Commit observation failed');
        if(control.body.entered){entered=true;break;}
        if(await page.getByTestId('upload-owner-error').count())break;
        await page.waitForTimeout(50);
      }
      if(entered)break;
      const message=await page.getByTestId('upload-owner-error').innerText();
      assert(message.includes('远端写入尚未结算')&&Date.now()<until,'Reconciliation did not reach commit: '+message);
      const retained=await call('GET',`/testing/owner-ledger/${setup.caseId}/${setup.uploadId}`);
      assert(retained.body.user_id===setup.memberId&&retained.body.events.length===0,'Busy rejection changed owner');
      busyRefusals++;await page.waitForTimeout(200);
    }
    assert(await row.getByTestId('upload-owner-reconcile').isDisabled()&&await page.getByTestId('file-legacy-inspect').isDisabled(),'Pending repair permits duplicate actions');
    await row.getByTestId('upload-owner-reconcile').evaluate(e=>{e.click();e.click();});assert(mutations.length===before+1,'Duplicate reconciliation POST');
    await call('POST','/testing/owner-commit-control',{action:'release'});
    await page.getByTestId('upload-owner-notice').filter({hasText:'已修复'}).waitFor();
    await row.waitFor({state:'detached'});
    assert(await page.getByTestId('upload-owner-row').count()===0,'Reconciled row retained in legacy list');
    state=await call('GET',`/testing/owner-ledger/${setup.caseId}/${setup.uploadId}`);
    const progress=await call('GET',target);assert(state.body.events.length===1&&state.body.remaining===262144&&progress.body.offset===262144,'Actual audit or untouched upload progress differs');
    checks.push('pending_commit_disables_duplicate_actions_single_audit_and_preserves_real_upload');
    if(busyRefusals)checks.push('actual_transient_background_credit_refused_then_explicit_retry_after_settlement');
    const after=mutations.length;await page.reload();await page.locator(`[data-case-name="${setup.caseName}"]`).click();await page.getByTestId('file-legacy-inspect').click();
    await page.getByTestId('upload-owner-recovery').filter({hasText:'未发现'}).waitFor();assert(mutations.length===after,'Reload repeated quota transfer');
    checks.push('refresh_reads_reconciled_empty_state_without_repeating_transfer');
    // A new real collaborator sees the row but cannot invoke owner billing.
    const collaborator=await call('POST','/auth/register',{email:'permission-browser-'+Date.now()+'@example.com',password:'isolated-browser-123',name:'Actual permission collaborator'});
    assert(collaborator.status===200,'Actual collaborator registration failed');
    const memberAuth=collaborator.body;
    const memberId=await page.evaluate(async token=>{const r=await fetch('/api/v1/auth/me',{headers:{Authorization:'Bearer '+token}});return (await r.json()).id;},memberAuth.token);
    const shared=await call('POST',`/testing/owner-share/${setup.caseId}/${memberId}`,{});assert(shared.status===200,'Actual sharing setup failed');
    const created=(await admittedCall('POST',prefix+'/uploads',{path:'Ref/collaborator-permission.bin',size:16})).body;
    const seeded=await call('POST',`/testing/owner-seed/${setup.caseId}/${created.id}`,{user_id:memberId});assert(seeded.status===200,'Actual legacy row setup failed');
    await page.evaluate(token=>localStorage.setItem('vf_token',token),memberAuth.token);await page.reload();
    await page.evaluate(async caseId=>{const {useStore}=await import('/src/store.ts');const state=useStore.getState();await state.loadWorkspaces();for(const w of useStore.getState().workspaces){if(!w.shared)continue;await useStore.getState().selectWorkspace(w);const c=useStore.getState().cases.find(x=>x.id===caseId);if(c){await useStore.getState().selectCase(c);return;}}throw Error('Actual shared case not available');},setup.caseId);
    await page.getByTestId('file-legacy-inspect').click();await page.getByTestId('upload-owner-row').waitFor();
    assert(await page.getByTestId('upload-owner-reconcile').count()===0,'Collaborator offered owner billing action');
    assert((await page.getByTestId('upload-owner-recovery').innerText()).includes('请联系项目所有者'),'Permission recovery guidance missing');
    await page.getByTestId('case-file-actions').screenshot({path:`D:/vibeflow_reverse/tmp/upload-owner-ui-member-${origin.port}.png`});
    checks.push('actual_collaborator_sees_guidance_without_owner_only_control');
    await page.evaluate(token=>localStorage.setItem('vf_token',token),ownerToken);await page.reload();await page.locator(`[data-case-name="${setup.caseName}"]`).click();
    await admittedCall('POST',prefix+'/uploads/'+created.id+':reconcile-owner',{});await admittedCall('DELETE',prefix+'/uploads/'+created.id);await admittedCall('DELETE',target);
    const result={passed:true,checks,count:checks.length,measurements,quotaRejection,busyRefusals,caseId:setup.caseId,uploadId:setup.uploadId,real_API:true,model_calls:0,mock_llm_calls:0,browser:page.context().browser().version()};
    await page.evaluate(r=>window.__ownerBrowserResult=r,result);return result;
  }catch(error){const result={passed:false,checks,error:String(error),caseId:setup.caseId,uploadId:setup.uploadId};await page.evaluate(r=>window.__ownerBrowserResult=r,result);throw error;}
  finally{page.off('request',observer);}
}
