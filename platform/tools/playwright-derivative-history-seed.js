async page => {
  const origin=new URL(page.url()).origin;
  if(!origin.startsWith('http://127.0.0.1:')||origin.endsWith(':8000'))throw Error('Disposable UI only');
  const cached=await page.evaluate(()=>JSON.parse(localStorage.getItem('derivative-history-fixture')||'null'));
  if(cached)return {seeded:true,caseId:cached.caseId};
  const {token,caseId}=await page.evaluate(()=>({token:localStorage.getItem('vf_token'),caseId:Number(localStorage.getItem('derivative-history-case'))}));
  if(!token||!caseId)throw Error('Register isolated browser fixture first');
  const headers={Authorization:'Bearer '+token},prefix=`/cases/${caseId}`;
  const call=async(path,body,method)=>{
    const r=await page.request.fetch(origin+'/api/v1'+path,{method:method||(body?'POST':'GET'),data:body,headers,timeout:120000});
    if(!r.ok())throw Error(path+' '+r.status()+' '+await r.text());return r.json();
  };
  const wait=async created=>{
    for(let i=0;i<360;i++){
      const job=await call('/jobs/'+created.job_id);
      if(['succeeded','failed','cancelled','interrupted'].includes(job.status)){
        if(job.status!=='succeeded')throw Error('Native worker '+job.status);
        return created.publication_id||job.attempt.outputs.publication.id;
      }
      await page.waitForTimeout(500);
    }
    throw Error('Native worker deadline');
  };
  const fixture={caseId,versions:{}};
  for(const output of ['E-report/report.md','E-report/report.generated.md']){
    const ids=[];
    for(const overwrite of [false,true])ids.push(await wait(await call(prefix+'/report:compose?wait=false',{output,overwrite})));
    fixture.versions[output]=ids;
    await call(prefix+'/file',{path:output,content:'MANUAL WORKSPACE DRAFT'},'PUT');
  }
  const r=await page.request.post(origin+'/api/v1'+prefix+'/upload?path=Ref/source.pdf',{
    headers,multipart:{file:{name:'source.pdf',mimeType:'application/pdf',buffer:Buffer.from('JVBERi0xLjMKJeLjz9MKMSAwIG9iago8PAovUHJvZHVjZXIgKHB5cGRmKQovVGl0bGUgKE9yaWdpbmFsIHByaXZhdGUgc291cmNlKQo+PgplbmRvYmoKMiAwIG9iago8PAovVHlwZSAvUGFnZXMKL0NvdW50IDIKL0tpZHMgWyA0IDAgUiA1IDAgUiBdCj4+CmVuZG9iagozIDAgb2JqCjw8Ci9UeXBlIC9DYXRhbG9nCi9QYWdlcyAyIDAgUgo+PgplbmRvYmoKNCAwIG9iago8PAovVHlwZSAvUGFnZQovUmVzb3VyY2VzIDw8Cj4+Ci9NZWRpYUJveCBbIDAuMCAwLjAgMzAwIDQwMCBdCi9QYXJlbnQgMiAwIFIKPj4KZW5kb2JqCjUgMCBvYmoKPDwKL1R5cGUgL1BhZ2UKL1Jlc291cmNlcyA8PAo+PgovTWVkaWFCb3ggWyAwLjAgMC4wIDMwMCA0MDAgXQovUGFyZW50IDIgMCBSCj4+CmVuZG9iagp4cmVmCjAgNgowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMTUgMDAwMDAgbiAKMDAwMDAwMDA4NyAwMDAwMCBuIAowMDAwMDAwMTUyIDAwMDAwIG4gCjAwMDAwMDAyMDEgMDAwMDAgbiAKMDAwMDAwMDI5NSAwMDAwMCBuIAp0cmFpbGVyCjw8Ci9TaXplIDYKL1Jvb3QgMyAwIFIKL0luZm8gMSAwIFIKPj4Kc3RhcnR4cmVmCjM4OQolJUVPRgo=','base64')}}});
  if(!r.ok())throw Error('PDF upload '+r.status());
  const pdfId=await wait(await call(prefix+'/pdf:number',{input:'Ref/source.pdf',output:'E-report/numbered.pdf'}));
  fixture.versions.pdf=[pdfId];
  await call(prefix+'/file',{path:'Ref/source.pdf',content:'source changed after generation'},'PUT');
  const deckIds=[];
  for(let i=0;i<2;i++){
    if(i)await call(prefix+'/file?path=E-report/deck.pptx',undefined,'DELETE');
    deckIds.push(await wait(await call(prefix+'/ppt:build',{output:'E-report/deck.pptx',title:'History deck '+i,
      slides:[{title:'Historical slide '+i,text:'Native editable content '+i}]})));
  }
  fixture.versions.ppt=deckIds;
  await call(prefix+'/file',{path:'E-report/deck.pptx',content:'MANUAL PPT ALIAS'},'PUT');
  await page.evaluate(value=>localStorage.setItem('derivative-history-fixture',JSON.stringify(value)),fixture);
  await page.reload();await page.locator('[data-case-name="report-derivative-history"]').click();
  await page.evaluate(async()=>{const {activatePanel}=await import('/src/shell/dock.ts');activatePanel('caseinfo');});
  return {seeded:true,caseId,versions:fixture.versions};
}
