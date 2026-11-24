async page => {
  const origin=new URL(page.url()).origin;
  if(!origin.startsWith('http://127.0.0.1:')||origin.endsWith(':8000'))throw Error('Isolated API only');
  let fixture=await page.evaluate(()=>JSON.parse(localStorage.getItem('latex-history-fixture')||'null'));
  if(fixture)return {seeded:true,caseId:fixture.caseId};
  const registration=await page.request.post(origin+'/api/v1/auth/register',{data:{email:'latex-history-'+Date.now()+'@example.com',name:'History',password:'isolated-latex-123'}});
  if(!registration.ok())throw Error('Registration failed');
  const {token}=await registration.json();await page.evaluate(t=>localStorage.setItem('vf_token',t),token);
  const call=async(path,body,method)=>{
    const r=await page.request.fetch(origin+'/api/v1'+path,{method:method||(body?'POST':'GET'),data:body,headers:{Authorization:'Bearer '+token},timeout:120000});
    if(!r.ok())throw Error(path+' returned '+r.status()+' '+await r.text());return r.json();
  };
  const spaces=await call('/workspaces');
  const created=await call(`/workspaces/${spaces[0].id}/cases`,{name:'latex-history-reader'});
  const main='E-report/latex/main.tex';
  const first='\\documentclass{article}\n\\usepackage{hyperref}\n\\begin{document}\n\\section{Introduction}\nIntro text.\\newpage\n\\section{Results}\nUniqueResultText and conclusion.\n\\end{document}\n';
  fixture={caseId:created.id,main,first,versions:[]};
  for(const [index,source] of [first,'\n\n'+first.replace('UniqueResultText','NewResultText')].entries()){
    await call(`/cases/${created.id}/file`,{path:main,content:source},'PUT');
    const queued=await call(`/cases/${created.id}/latex:build`,{main,recipe:'pdflatex'});
    let done=false;
    for(let i=0;i<440;i++){
      const job=await call('/jobs/'+queued.job_id);
      if(['succeeded','failed','cancelled','interrupted'].includes(job.status)){
        if(job.status!=='succeeded')throw Error('Native LaTeX failed '+job.status+' '+job.log_backlog);
        done=true;break;
      }
      await page.waitForTimeout(500);
    }
    if(!done)throw Error('Native build exceeded deadline');
    fixture.versions.push({id:queued.publication_id,jobId:queued.job_id,index});fixture.pdf=queued.pdf;
  }
  await page.evaluate(f=>localStorage.setItem('latex-history-fixture',JSON.stringify(f)),fixture);
  await page.reload();await page.locator('[data-case-name="latex-history-reader"]').click();
  await page.evaluate(async path=>{const {useStore}=await import('/src/store.ts');const {activatePanel}=await import('/src/shell/dock.ts');await useStore.getState().openFile(path);activatePanel('viewer');},fixture.pdf);
  await page.waitForFunction(()=>document.querySelector('.textLayer')?.textContent?.includes('Introduction'));
  await page.setViewportSize({width:1440,height:1000});
  await page.screenshot({path:'D:/vibeflow_reverse/tmp/N2-latex-history-before.png'});
  return {seeded:true,caseId:fixture.caseId,versions:fixture.versions};
}
