async (page) => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const fixture = await page.evaluate(() => JSON.parse(localStorage.getItem('snapshot-fixture')));
  assert(fixture && new URL(page.url()).hostname==='127.0.0.1', 'Owned browser fixture required');
  let release;
  let started;
  let finished;
  const gate = new Promise(resolve => { release=resolve; });
  const entered = new Promise(resolve => { started=resolve; });
  const exited = new Promise(resolve => { finished=resolve; });
  const pattern = '**/report/artifact?**';
  await page.route(pattern, async route => {
    started();
    await gate;
    try { await route.continue(); } catch {} finally { finished(); }
  });
  try {
    await page.getByTestId('document-reload').click();
    await entered;
    assert(await page.getByTestId('document-download').isDisabled(), 'Loading has old enabled download');
    await page.evaluate(async () => {
      const call = async (path, body, method) => {
        const r=await fetch('/api/v1'+path,{method:method||(body?'POST':'GET'),
          headers:{Authorization:'Bearer '+localStorage.getItem('vf_token'),'Content-Type':'application/json'},
          body:body?JSON.stringify(body):undefined});
        if(!r.ok) throw new Error(path+' '+r.status);
        return r.json();
      };
      const workspaces = await call('/workspaces');
      const created = await call(`/workspaces/${workspaces[0].id}/cases`,{name:'document-race-other-case'});
      await call(`/cases/${created.id}/file`,{path:'E-report/build/report.html',content:'<h1>OtherCaseDraft</h1>'},'PUT');
      const {useStore} = await import('/src/store.ts');
      const {activatePanel} = await import('/src/shell/dock.ts');
      await useStore.getState().selectCase(created);
      await useStore.getState().openFile('E-report/build/report.html');
      activatePanel('viewer');
    });
    await page.getByTestId('document-version').filter({hasText:'工作区文件'}).waitFor();
    release();
    await exited;
    await page.waitForFunction(() => document.querySelector('[data-testid="document-shadow"]')
      ?.shadowRoot?.querySelector('article')?.textContent?.includes('OtherCaseDraft'));
    const original = await page.evaluate(async () => {
      let blob;
      const original = URL.createObjectURL;
      URL.createObjectURL = value => {blob=value;return original(value);};
      try {
        document.querySelector('[data-testid="document-download"]').click();
        return await blob.text();
      } finally {URL.createObjectURL=original;}
    });
    assert(original==='<h1>OtherCaseDraft</h1>', 'Late old case request replaced downloadable bytes');
    const result = {passed:true,count:2,checks:[
      'late_old_case_read_does_not_replace_new_case_view_or_download',
      'unpublished_canonical_report_explicitly_shown_as_workspace_file']};
    await page.evaluate(value=>localStorage.setItem('document-snapshot-races-result',JSON.stringify(value)),result);
    return result;
  } finally {
    release();
    await page.unroute(pattern);
  }
}
