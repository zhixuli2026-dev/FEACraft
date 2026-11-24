async (page) => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const origin = new URL(page.url()).origin;
  assert(origin.startsWith('http://127.0.0.1:') && !origin.endsWith(':8000'), 'Isolated UI only');
  await page.addInitScript(() => {
    const Original = window.WebSocket;
    window.fixtureSockets = [];
    window.WebSocket = class extends Original {
      constructor(...args) { super(...args); window.fixtureSockets.push(this); }
    };
  });
  const fixture = await page.evaluate(async () => {
    const call = async (path, body, token) => {
      const r = await fetch('/api/v1'+path,{method:body ? 'POST':'GET',headers:{'Content-Type':'application/json',
        ...(token ? {Authorization:'Bearer '+token} : {})},body:body ? JSON.stringify(body):undefined});
      if (!r.ok) throw new Error('Fixture '+r.status);
      return r.json();
    };
    const auth = await call('/auth/register',{email:`events-${Date.now()}@example.com`,name:'events',password:'isolated-events-123'});
    localStorage.setItem('vf_token',auth.token);
    const workspaces = await call('/workspaces',null,auth.token);
    const c = await call(`/workspaces/${workspaces[0].id}/cases`,{name:'cursor-browser-acceptance'},auth.token);
    const job = await call(`/fixture/job/${c.id}`,{status:'running'},auth.token);
    return {caseId:c.id,jobId:job.job_id};
  });
  const pages = [];
  page.on('response', async response => {
    if (response.url().includes(`/cases/${fixture.caseId}/workflow-events?`) && response.ok()) {
      try { pages.push(await response.json()); } catch {}
    }
  });
  await page.reload();
  await page.locator('[data-case-name="cursor-browser-acceptance"]').click();
  await page.locator('.dv-tab').filter({hasText:/^作业$/}).click();
  const job = page.getByRole('button',{name:`#${fixture.jobId} Cursor delivery acceptance`,exact:true});
  await job.click();
  await page.getByTestId('job-metadata').locator('summary').filter({hasText:'运行中'}).waitFor();
  const token = await page.evaluate(() => localStorage.getItem('vf_token'));
  await page.context().setOffline(true);
  try {
    const response = await page.request.post(`${origin}/api/v1/fixture/job/${fixture.caseId}`,{
      headers:{Authorization:'Bearer '+token},data:{status:'failed'}});
    assert(response.ok(),'Offline server transition failed');
  } finally { await page.context().setOffline(false); }
  await page.getByTestId('job-metadata').locator('summary').filter({hasText:'失败'}).waitFor();
  await page.waitForFunction(() => window.fixtureSockets.some(ws => ws.url.includes('/ws/events?') && ws.readyState===1));
  await page.evaluate(({caseId,jobId}) => {
    const ws = window.fixtureSockets.findLast(ws => ws.url.includes('/ws/events?') && ws.readyState===1);
    // Old, duplicate and out-of-order success notifications are not state.
    for (const seq of [999999,1,1,3,2]) ws.dispatchEvent(new MessageEvent('message',{data:JSON.stringify({
      case_id:caseId,seq,event_id:'late-'+seq,type:'jobs.updated',job_id:jobId,status:'succeeded'})}));
    ws.close();
  }, fixture);
  await page.waitForFunction(() => window.fixtureSockets.filter(ws => ws.url.includes('/ws/events?')).length>=2);
  await page.getByTestId('job-metadata').locator('summary').filter({hasText:'失败'}).waitFor();
  assert(pages.some(p => p.events.some(e => e.type==='jobs.updated' && e.payload.status==='failed')),
    'Actual failed event was not replayed');
  assert(pages.every(p => p.next_after < 999999),'Socket hint incorrectly advanced HTTP cursor');
  const final = await page.request.get(`${origin}/api/v1/jobs/${fixture.jobId}`,{headers:{Authorization:'Bearer '+token}});
  assert((await final.json()).status==='failed','Page no longer agrees with DB');
  await page.reload();
  await page.locator('[data-case-name="cursor-browser-acceptance"]').click();
  await page.locator('.dv-tab').filter({hasText:/^作业$/}).click();
  await job.click();
  await page.getByTestId('job-metadata').locator('summary').filter({hasText:'失败'}).waitFor();
  const jobs = await page.request.get(`${origin}/api/v1/cases/${fixture.caseId}/jobs`,{headers:{Authorization:'Bearer '+token}});
  assert((await jobs.json()).length===1,'Reconnect or refresh created another job');
  return {passed:true,fixture,cursorPages:pages.length,checks:['offline_completion_reconciles_actual_Job_panel',
    'duplicate_and_reordered_success_hints_do_not_override_DB','socket_reconnect_uses_durable_cursor',
    'page_reload_does_not_execute_again']};
}
