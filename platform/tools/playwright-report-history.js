async (page) => {
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const fixture = await page.evaluate(() => JSON.parse(localStorage.getItem('document-snapshot-result')));
  assert(fixture?.passed && new URL(page.url()).hostname === '127.0.0.1', 'Owned native report fixture required');
  const caseId = fixture.fixture.caseId, older = fixture.fixture.publicationId;
  const checks = [];
  await page.setViewportSize({width:1440,height:1000});
  await page.reload();
  const open = async path => page.evaluate(async path => {
    const {useStore} = await import('/src/store.ts');
    const {activatePanel} = await import('/src/shell/dock.ts');
    await useStore.getState().openFile(path); activatePanel('viewer');
  }, path);
  await page.locator('[data-case-name="published-reader-acceptance"]').click();
  const ready = async text => page.waitForFunction(text => document.querySelector('[data-testid="document-shadow"]')
    ?.shadowRoot?.querySelector('article')?.textContent?.includes(text), text);
  await open('E-report/build/report.html'); await ready('PublishedSecond');
  await page.getByTestId('report-history-toggle').click();
  await page.locator(`[data-publication-id="${older}"]`).click(); await ready('PublishedFirst');
  assert((await page.getByTestId('document-version').textContent()).includes('历史发布，不代表当前有效结果'), 'Old report mislabeled current');
  assert(await page.locator(`[data-publication-id="${older}"]`).getAttribute('aria-pressed') === 'true','Selection not announced');
  checks.push('real_native_HTML_history_selection_marks_historical_not_current_valid');
  async function downloadMatches(path) {
    const pending = page.waitForEvent('download'); await page.getByTestId('document-download').click();
    const file = await pending, stream = await file.createReadStream();
    const chunks = []; for await (const chunk of stream) chunks.push(chunk);
    const bytes = Buffer.concat(chunks);
    const token = await page.evaluate(() => localStorage.getItem('vf_token'));
    const response = await page.request.get(new URL(page.url()).origin+`/api/v1/cases/${caseId}/report/artifact?path=${encodeURIComponent(path)}&publication_id=${older}`,
      {headers:{Authorization:'Bearer '+token}});
    assert(response.ok() && bytes.equals(await response.body()), 'Downloaded bytes differ from selected immutable artifact');
  }
  await downloadMatches('E-report/build/report.html');
  await page.screenshot({path:'D:/vibeflow_reverse/tmp/N2-history-selected.png'});
  checks.push('selected_HTML_download_matches_immutable_old_bytes');
  await page.getByTestId('document-reload').click(); await ready('PublishedSecond');
  assert((await page.getByTestId('document-version').textContent()).includes('输入已变化'), 'Current stale report lost distinction');
  checks.push('explicit_reload_returns_to_latest_and_retains_stale_warning');
  await open('E-report/build/report.docx'); await ready('PublishedSecond');
  await page.getByTestId('report-history-toggle').click();
  await page.locator(`[data-publication-id="${older}"]`).click(); await ready('PublishedFirst');
  await downloadMatches('E-report/build/report.docx');
  checks.push('native_DOCX_history_render_and_original_download_match_same_old_version');
  const pattern = '**/report/results?publication_id='+older;
  await page.route(pattern, route => route.fulfill({status:404,body:'{}',contentType:'application/json'}));
  try {
    await page.locator(`[data-publication-id="${older}"]`).click();
    await page.getByRole('alert').filter({hasText:'无法核验报告版本 (404)'}).waitFor();
    assert(await page.getByTestId('document-download').isDisabled(), 'Missing selected version allows download');
  } finally { await page.unroute(pattern); }
  await page.locator(`[data-publication-id="${older}"]`).click(); await ready('PublishedFirst');
  checks.push('missing_selected_version_errors_without_alias_fallback_and_explicit_retry_recovers');
  await page.route('**/report/publications?**',route=>route.fulfill({status:503,body:'{"detail":"fixture unavailable"}',contentType:'application/json'}));
  try {
    await page.getByRole('button',{name:'刷新列表',exact:true}).click();
    await page.getByRole('alert').filter({hasText:'历史列表读取失败'}).waitFor();
    assert(await page.locator(`[data-publication-id="${older}"]`).isVisible(),'Refresh failure erased prior list');
    await ready('PublishedFirst');
  } finally { await page.unroute('**/report/publications?**'); }
  const retried = page.waitForResponse(response => response.url().includes('/report/publications?') && response.ok());
  await page.getByRole('button',{name:'重试',exact:true}).click();
  assert(!new URL((await retried).url()).searchParams.has('before_generation'), 'Refresh retry fetched an older page');
  await page.waitForFunction(()=>!document.querySelector('.report-history [role="alert"]')
    && !document.querySelector('.report-history [role="status"]'));
  checks.push('list_failure_keeps_current_read_and_previous_entries_with_retry');
  const result={passed:true,count:checks.length,checks,fixture:fixture.fixture,secondPublication:fixture.secondPublication};
  await page.evaluate(value=>localStorage.setItem('report-history-result',JSON.stringify(value)),result);
  await page.screenshot({path:'D:/vibeflow_reverse/tmp/N2-history-docx.png'});
  return result;
}
