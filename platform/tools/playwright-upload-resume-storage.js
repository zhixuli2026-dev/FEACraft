// Incremental IndexedDB contention / bounded hashing / final render verification in Vite.
async page=>{
  const checks=await page.evaluate(async()=>{
    const m=await import('/src/utils/uploadResume.ts');
    const assert=(v,message)=>{if(!v)throw Error(message);};const checks=[];
    const content=new Uint8Array(17*1024*1024+13).fill(83);
    const file=new File([content],'original.bin');
    let peak=0;const read=Blob.prototype.arrayBuffer;
    Blob.prototype.arrayBuffer=function(){peak=Math.max(peak,this.size);return read.call(this);};
    let source;
    try {
      source=await m.identifyFile(file,()=>false,()=>{});
      content[content.length-1]=84;
      let rejected=false;
      try {await m.identifyFile(new File([content],'same-name.bin'),()=>false,()=>{},source);}
      catch(error) {rejected=error.message.includes('内容与原文件不同');}
      assert(rejected&&peak===4*1024*1024,'Last block verification or bounded read failed');
    } finally {Blob.prototype.arrayBuffer=read;}
    checks.push('last_byte_mismatch_rejected_with_maximum_4MiB_hash_reads');
    const scope='test:'+crypto.randomUUID(),clientId=crypto.randomUUID();
    const base={schema:1,scope,clientId,path:'Ref/test.bin',source,upload:null,
      version:{path:'Ref/test.bin',exists:false,content_sha256:null,size:0,protected:false}};
    const upload=id=>({id,path:base.path,size:source.size,offset:0});
    try {
      await m.saveResume(base,true);
      const outcomes=await Promise.allSettled([
        m.saveResume({...base,upload:upload('a'.repeat(32))}),m.saveResume({...base,upload:upload('b'.repeat(32))})]);
      assert(outcomes.filter(r=>r.status==='fulfilled').length===1,'Two sessions bound same prepared intent');
      const winner=await m.readResume(scope);assert(!!winner.upload,'No winning session');
      checks.push('atomic_same_intent_two_session_race_accepts_only_one_binding');
      await m.removeResume(winner);
      let rejected=false;try {await m.saveResume(winner);}catch {rejected=true;}
      assert(rejected&&await m.readResume(scope)===null,'Cancelled record resurrected');
      checks.push('stale_async_save_cannot_resurrect_cancelled_record');
      const replacement={...base,clientId:crypto.randomUUID()};await m.saveResume(replacement,true);
      await m.removeResume(winner);
      rejected=false;try {await m.saveResume(winner);}catch {rejected=true;}
      assert(rejected&&(await m.readResume(scope)).clientId===replacement.clientId,'Old tab overwrote or removed new intent');
      await m.removeResume(replacement);checks.push('old_tab_cannot_overwrite_or_remove_new_upload_intent');
    } finally {
      const value=await m.readResume(scope);if(value)await m.removeResume(value);
    }
    return checks;
  });
  const context=await page.evaluate(async()=>{
    const {useStore}=await import('/src/store.ts');return {caseId:useStore.getState().currentCase.id,caseName:useStore.getState().currentCase.name};
  });
  let used=false;
  const pattern=`**/api/v1/cases/${context.caseId}/uploads/*?offset=*`;
  const handler=async route=>{if(used)return route.continue();used=true;await route.fetch();await route.abort('failed');};
  await page.setViewportSize({width:1440,height:1000});
  await page.getByTestId('tree-upload-file').setInputFiles({name:'超长文件名-'.repeat(12)+'恢复.bin',mimeType:'application/octet-stream',buffer:Buffer.alloc(17*1024*1024,83)});
  await page.getByTestId('file-operation-inspect').click();
  await page.waitForFunction(()=>!document.querySelector('[data-testid="file-operation-submit"]')?.disabled);
  await page.route(pattern,handler);
  try {
    await page.getByTestId('file-operation-submit').click();
    await page.getByTestId('file-operation-error').filter({hasText:'网络请求中断'}).waitFor();
  } finally {await page.unroute(pattern,handler);}
  await page.reload();await page.locator(`[data-case-name="${context.caseName}"]`).click();
  await page.waitForFunction(()=>document.querySelector('progress[aria-label="文件上传进度"]')?.value===4194304);
  const measurements=[];
  for(const width of [1440,768,390]) {
    await page.setViewportSize({width,height:1000});
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))));
    for(const theme of ['dark','light']) {
      await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
      const panel=page.getByTestId('case-file-actions');
      const layout=await panel.evaluate(e=>{
        const box=e.getBoundingClientRect(),parent=e.parentElement.getBoundingClientRect();
        return {width:box.width,parent:parent.width,scroll:e.scrollWidth,client:e.clientWidth};
      });
      if(layout.scroll>layout.client+1||layout.width>layout.parent+1)throw Error('Panel clips at '+width+' '+theme+JSON.stringify(layout));
      await page.keyboard.press('Tab');await page.getByRole('button',{name:'重新选择原文件',exact:true}).focus();
      if(await page.getByRole('button',{name:'重新选择原文件',exact:true}).evaluate(e=>getComputedStyle(e).outlineStyle)==='none')throw Error('Missing keyboard focus');
      await panel.screenshot({path:`tmp/mainline-N2-browser-upload-evidence/resume-final-${theme}-${width}.png`});
      measurements.push({viewport:width,theme,...layout});
    }
  }
  checks.push('actual_panel_and_parent_bounds_at_1440_768_390_dark_light_long_filename_keyboard');
  await page.setViewportSize({width:1440,height:1000});await page.evaluate(()=>document.documentElement.dataset.theme='dark');
  await page.getByTestId('file-operation-cancel').click();await page.getByTestId('file-operation-form').waitFor({state:'detached'});
  const result={passed:true,count:checks.length,checks,measurements,browser:page.context().browser().version()};
  await page.evaluate(r=>window.__uploadStorageEvidence=r,result);return result;
}
