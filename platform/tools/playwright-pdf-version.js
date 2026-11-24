async (page) => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const origin = new URL(page.url()).origin;
  const backend = await page.evaluate(() => localStorage.getItem('pdf-fixture-api'));
  assert(origin.startsWith('http://127.0.0.1:') && backend?.startsWith('http://127.0.0.1:')
    && !backend.endsWith(':8000'), 'Disposable API/UI required');
  const registered = await page.request.post(origin+'/api/v1/auth/register', {data:{
    email:`pdf-version-${Date.now()}@example.com`,name:'pdf-version',password:'isolated-pdf-version-123'}});
  assert(registered.ok(), 'Registration failed');
  const {token} = await registered.json();
  await page.evaluate(token=>localStorage.setItem('vf_token',token),token);
  const headers = {Authorization:'Bearer '+token};
  const call = async (path,data) => {
    const response = await page.request.fetch(origin+'/api/v1'+path,{method:data?'POST':'GET',data,headers});
    assert(response.ok(),path+' '+response.status()+' '+await response.text());
    return response.json();
  };
  const workspaces = await call('/workspaces');
  const fixture = await call(`/workspaces/${workspaces[0].id}/cases`,{name:'pdf-version-acceptance'});
  const payloads = {};
  for (const version of ['one','two']) {
    const response = await page.request.get(backend+'/fixture/pdf/'+version);
    assert(response.ok(), 'PDF fixture unavailable');
    payloads[version] = await response.body();
  }
  assert(payloads.one.length===payloads.two.length && payloads.one.length>4000000,'Equal-length large PDFs required');
  const path = 'Ref/large.pdf';
  const upload = async (version, selected=path) => {
    const response = await page.request.post(origin+`/api/v1/cases/${fixture.id}/upload?path=${encodeURIComponent(selected)}`,
      {headers,multipart:{file:{name:'large.pdf',mimeType:'application/pdf',buffer:payloads[version]}}});
    assert(response.ok(),'Upload '+response.status()+' '+await response.text());
  };
  await upload('one');
  await page.reload();
  await page.locator('[data-case-name="pdf-version-acceptance"]').click();
  const open = selected => page.evaluate(async selected => {
    const {useStore}=await import('/src/store.ts');
    const {activatePanel}=await import('/src/shell/dock.ts');
    await useStore.getState().openFile(selected);activatePanel('viewer');
  },selected);
  const waitText = text => page.waitForFunction(text => document.querySelector('.pdf-text-layer')?.textContent?.includes(text)
    || document.querySelector('[data-testid="pdf-viewer"]')?.textContent?.includes(text),text,{timeout:30000});
  const requests=[];
  page.on('request', request => {
    if (request.url().includes('/raw?') && request.headers().range) requests.push(request.headers());
  });
  await open(path);
  await waitText('PDFVersionOne');
  const checks=[];
  const stats = await page.getByTestId('pdf-viewer').evaluate(element=>({
    requests:Number(element.dataset.rangeRequests),bytes:Number(element.dataset.rangeBytes)}));
  assert(stats.requests>=2 && stats.bytes<payloads.one.length,'PDF stopped reading bounded ranges: '+JSON.stringify(stats));
  assert(requests.length>=2 && requests.every(h=>/^[0-9a-f]{64}$/.test(h['x-vibeflow-file-version'])),
    'Range requests did not carry versions');
  const originalVersion=requests[0]['x-vibeflow-file-version'];
  assert(requests.every(h=>h['x-vibeflow-file-version']===originalVersion),'Initial range versions differ');
  checks.push('real_PDFjs_incremental_render_uses_one_version');
  await page.getByTestId('pdf-next').click();
  await waitText('PDFVersionOne page 2');
  await page.getByTestId('pdf-prev').click();
  await waitText('PDFVersionOne page 1');
  checks.push('page_navigation_retains_text_rendering');

  await upload('two');
  await page.getByTestId('pdf-search').fill('PDFVersionOne');
  await page.getByTestId('pdf-search-run').click();
  await page.getByTestId('pdf-error').filter({hasText:'文件已变化'}).waitFor();
  assert(await page.getByTestId('pdf-viewer').count()===0,'Invalid PDF remains interactive');
  checks.push('equal_length_file_replacement_stops_real_reader');
  await page.getByTestId('pdf-reload').click();
  await waitText('PDFVersionTwo');
  assert(requests.at(-1)['x-vibeflow-file-version']!==originalVersion,'Reload retained old token');
  checks.push('explicit_reload_reads_new_file_version');

  const pattern='**/raw?**';
  await page.route(pattern,async route=>{
    if (!route.request().headers().range) return route.continue();
    const response=await route.fetch();
    const modified={...response.headers()};delete modified['x-vibeflow-file-version'];
    await route.fulfill({response,headers:modified});
  });
  try {
    await page.getByTestId('pdf-reload').click();
    await page.getByTestId('pdf-error').filter({hasText:'版本校验失败'}).waitFor();
    checks.push('unverifiable_range_response_refused_in_browser');
  } finally {await page.unroute(pattern);}
  await page.getByTestId('pdf-reload').click();
  await waitText('PDFVersionTwo');
  checks.push('reader_recovers_after_protocol_failure');

  await upload('two','Ref/other.pdf');
  let release,entered,finished;
  const gate=new Promise(resolve=>{release=resolve;});
  const started=new Promise(resolve=>{entered=resolve;});
  const exited=new Promise(resolve=>{finished=resolve;});
  await page.route(pattern,async route=>{
    if (route.request().method()!=='HEAD' || !decodeURIComponent(route.request().url()).includes(path)) return route.continue();
    entered();await gate;
    try {await route.continue();} catch {} finally {finished();}
  });
  try {
    await page.getByTestId('pdf-reload').click();await started;
    await open('Ref/other.pdf');await waitText('PDFVersionTwo');
    release();await exited;
    assert(await page.getByTestId('pdf-viewer').getAttribute('data-file')==='Ref/other.pdf','Late HEAD restored old file');
    assert(await page.getByTestId('pdf-error').count()===0,'Old aborted HEAD surfaced an error');
    checks.push('late_HEAD_does_not_replace_new_file');
  } finally {release();await page.unroute(pattern);}
  const result={passed:true,count:checks.length,checks,caseId:fixture.id,pdfBytes:payloads.one.length,initialRead:stats};
  await page.evaluate(result=>localStorage.setItem('pdf-version-result',JSON.stringify(result)),result);
  return result;
}
