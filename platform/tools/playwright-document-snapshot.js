async (page) => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const origin = new URL(page.url()).origin;
  assert(origin.startsWith('http://127.0.0.1:') && !origin.endsWith(':8000'), 'Isolated UI only');
  const checks = [];
  const call = async (path, data, method) => {
    const token = await page.evaluate(() => localStorage.getItem('vf_token'));
    const response = await page.request.fetch(origin+'/api/v1'+path, {
      method: method || (data ? 'POST' : 'GET'), data,
      headers: { Authorization: 'Bearer '+token }, timeout: 120000,
    });
    assert(response.ok(), path+' returned '+response.status()+' '+await response.text());
    return response.json();
  };
  let fixture = await page.evaluate(() => JSON.parse(localStorage.getItem('snapshot-fixture') || 'null'));
  if (!fixture) {
    const response = await page.request.post(origin+'/api/v1/auth/register', {data:{
      email:'snapshot-'+Date.now()+'@example.com',name:'snapshot',password:'isolated-snapshot-123'}});
    assert(response.ok(), 'Registration failed');
    const auth = await response.json();
    await page.evaluate(token => localStorage.setItem('vf_token',token), auth.token);
    const workspaces = await call('/workspaces');
    const created = await call(`/workspaces/${workspaces[0].id}/cases`, {name:'published-reader-acceptance'});
    await call(`/cases/${created.id}/file`, {path:'Ref/plot.svg',content:
      '<svg xmlns="http://www.w3.org/2000/svg" width="30" height="20"><rect width="30" height="20" fill="blue"/></svg>'}, 'PUT');
    await call(`/cases/${created.id}/file`, {path:'E-report/report.md',content:
      '# PublishedFirst\n\n![Frozen image](../Ref/plot.svg)\n\n$x^2$\n'}, 'PUT');
    const job = await call(`/cases/${created.id}/report:build`, {});
    fixture = {caseId:created.id,jobId:job.job_id,publicationId:job.publication_id};
    await page.evaluate(value => localStorage.setItem('snapshot-fixture', JSON.stringify(value)), fixture);
    await page.reload();
  }
  const settled = async id => {
    for (let i=0;i<240;i++) {
      const job = await call('/jobs/'+id);
      if (['succeeded','failed','cancelled','interrupted'].includes(job.status)) {
        assert(job.status==='succeeded', 'Native report failed: '+JSON.stringify(job));
        return job;
      }
      await page.waitForTimeout(250);
    }
    throw new Error('Native report exceeded browser fixture deadline');
  };
  await settled(fixture.jobId);
  await page.locator('[data-case-name="published-reader-acceptance"]').click();
  const open = async path => page.evaluate(async path => {
    const { useStore } = await import('/src/store.ts');
    const { activatePanel } = await import('/src/shell/dock.ts');
    await useStore.getState().openFile(path);
    activatePanel('viewer');
  }, path);
  const waitContent = async text => page.waitForFunction(text =>
    document.querySelector('[data-testid="document-shadow"]')?.shadowRoot?.querySelector('article')?.textContent?.includes(text), text);
  const content = () => page.getByTestId('document-shadow').locator('article').textContent();
  const download = async () => {
    const pending = page.waitForEvent('download');
    await page.getByTestId('document-download').click();
    const item = await pending;
    const stream = await item.createReadStream();
    const bytes = [];
    for await (const chunk of stream) bytes.push(...chunk);
    return new Uint8Array(bytes);
  };
  const html = 'E-report/build/report.html';
  await open(html);
  await waitContent('PublishedFirst');
  assert((await page.getByTestId('document-version').textContent()).includes('报告版本 1'), 'Published version missing');
  assert(await page.getByTestId('document-shadow').locator('article img').count() > 0, 'Frozen image missing');
  assert(await page.getByTestId('document-shadow').locator('article math').count() > 0, 'Math missing');
  const original = await download();
  checks.push('native_HTML_embedded_image_math_and_version_visible');

  await call(`/cases/${fixture.caseId}/file`, {path:'E-report/report.md',content:'# PublishedSecond\n\nNew report version.\n'}, 'PUT');
  const second = await call(`/cases/${fixture.caseId}/report:build`, {});
  await settled(second.job_id);
  assert(second.publication_id!==fixture.publicationId, 'Publication did not advance');
  assert((await content()).includes('PublishedFirst'), 'New publication replaced an open document');
  const retained = await download();
  assert(retained.length===original.length && retained.every((b,i)=>b===original[i]), 'Download differs from the displayed version');
  checks.push('new_native_publication_keeps_open_view_and_download_identical');
  await page.getByTestId('document-reload').click();
  await waitContent('PublishedSecond');
  assert((await page.getByTestId('document-version').textContent()).includes('报告版本 2'), 'Reload did not choose new version');
  checks.push('explicit_reload_selects_new_publication');

  await call(`/cases/${fixture.caseId}/file`, {path:'E-report/report.md',content:'# UnbuiltInput\n'}, 'PUT');
  await page.getByTestId('document-reload').click();
  await page.getByTestId('document-version').filter({hasText:'输入已变化'}).waitFor();
  assert((await content()).includes('PublishedSecond'), 'Stale report lost its bytes');
  checks.push('changed_input_labels_stale_but_preserves_report');
  await open('E-report/build/report.docx');
  await waitContent('PublishedSecond');
  assert(await page.getByTestId('document-reader').getAttribute('data-kind')==='docx', 'DOCX reader missing');
  const docx = await download();
  const token = await page.evaluate(() => localStorage.getItem('vf_token'));
  const expected = await page.request.get(origin+`/api/v1/cases/${fixture.caseId}/report/artifact?path=E-report/build/report.docx&publication_id=${second.publication_id}`,
    {headers:{Authorization:'Bearer '+token}});
  assert(expected.ok(), 'Published DOCX unavailable');
  const expectedBytes = await expected.body();
  assert(docx.length===expectedBytes.length && docx.every((b,i)=>b===expectedBytes[i]), 'DOCX download changed original ZIP bytes');
  checks.push('native_DOCX_render_and_exact_original_download');

  const ordinary = 'Ref/ordinary.html';
  await call(`/cases/${fixture.caseId}/file`, {path:ordinary,content:'<h1>WorkspaceFirst</h1>'}, 'PUT');
  await open(ordinary);
  await waitContent('WorkspaceFirst');
  await call(`/cases/${fixture.caseId}/file`, {path:ordinary,content:'<h1>WorkspaceSecond</h1>'}, 'PUT');
  assert(new TextDecoder().decode(await download()).includes('WorkspaceFirst'), 'Ordinary download refetched new bytes');
  assert((await page.getByTestId('document-version').textContent()).includes('工作区文件'), 'Draft mislabeled as publication');
  checks.push('workspace_document_download_also_matches_open_original');

  let aliasReads = 0;
  const listener = request => { if (request.url().includes('/raw?') && decodeURIComponent(request.url()).includes(html)) aliasReads++; };
  page.on('request', listener);
  await page.route('**/report/artifact?**', route => route.fulfill({status:409,contentType:'application/json',body:'{"detail":"corrupt fixture"}'}));
  try {
    await open(html);
    await page.getByRole('alert').filter({hasText:'读取失败 (409)'}).waitFor();
    assert(await page.getByTestId('document-download').isDisabled(), 'Failed read still downloadable');
    assert(aliasReads===0, 'Corrupt publication fell back to mutable path');
    checks.push('pinned_read_failure_blocks_download_and_alias_fallback');
  } finally {
    await page.unroute('**/report/artifact?**');
    page.off('request', listener);
  }
  await page.getByTestId('document-reload').click();
  await waitContent('PublishedSecond');
  checks.push('retry_after_read_failure_recovers');
  const result = {passed:true,count:checks.length,checks,fixture,secondPublication:second.publication_id};
  await page.evaluate(value => localStorage.setItem('document-snapshot-result', JSON.stringify(value)),result);
  return result;
}
