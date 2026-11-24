"""Disconnect a real browser during a real job, replay old hints and reload."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

CASE = 'Event replay acceptance'
COMMAND = 'python Ref/complete.py'

OBSERVE = """
window.replaySockets = [];
window.replayPages = [];
const Original = window.WebSocket;
window.WebSocket = class extends Original {
  constructor(...args) {
    super(...args);
    if (new URL(this.url).pathname === '/ws/events') window.replaySockets.push(this);
  }
};
const realFetch = window.fetch;
window.fetch = async (...args) => {
  const response = await realFetch(...args);
  const url = new URL(response.url);
  if (url.pathname.endsWith('/workflow-events') && response.ok) {
    response.clone().json().then(body => window.replayPages.push({
      after: Number(url.searchParams.get('after')), next: body.next_after,
      events: body.events.map(e => ({seq:e.seq, id:e.event_id, status:e.payload.status}))
    })).catch(() => {});
  }
  return response;
};
"""


def browser(data):
    import urllib.request
    from playwright.sync_api import sync_playwright, expect
    root = Path(data['root'])
    jid = data['job']
    base = data['base']
    token = data['token']
    route = '/cases/'+str(data['case'])
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        req = urllib.request.Request(base+'/api/v1'+path, headers={'Authorization':'Bearer '+token})
        with client.open(req,timeout=20) as response:
            return json.load(response)

    with sync_playwright() as play:
        chrome = play.chromium.launch()
        try:
            context = chrome.new_context(viewport={'width':1500,'height':950},locale='zh-CN')
            context.add_init_script("localStorage.setItem('vf_token',"+json.dumps(token)+");\n"+OBSERVE)
            page = context.new_page()
            page.goto(base,wait_until='domcontentloaded')

            def select_job():
                page.locator('[data-case-name="'+CASE+'"]').click(timeout=30000)
                page.locator('.dv-tab').filter(has_text='作业').first.click()
                page.get_by_role('button',name='作业与日志',exact=True).click()
                page.get_by_role('button',name=f'#{jid} {COMMAND}',exact=True).click()
                return page.get_by_test_id('job-metadata').locator('summary')

            status = select_job()
            expect(status).to_contain_text('运行中',timeout=30000)
            # A response can expose next_after before its state refresh has
            # completed. A subsequent request proves the browser adopted it.
            page.wait_for_function("replayPages.some(p=>p.after>0) && replaySockets.some(s=>s.readyState===1)",
                                   timeout=45000)
            pages_before = page.evaluate('replayPages')
            context.set_offline(True)
            page.evaluate('replaySockets.forEach(s=>s.close())')
            (root/'allow-job-exit').touch()
            try:
                deadline = time.monotonic()+75
                while time.monotonic()<deadline:
                    actual = get(f'/jobs/{jid}')
                    if actual['status']=='failed':
                        break
                    time.sleep(.15)
                else:
                    raise TimeoutError('Job did not finish while the browser was offline')
                assert actual['exit_code']==7
                events = get(route+'/workflow-events?after=0&limit=200')['events']
                changes = [e for e in events if e['type']=='jobs.updated' and e['payload']['job_id']==jid]
                running = next(e for e in changes if e['payload']['status']=='running')
                failed = next(e for e in changes if e['payload']['status']=='failed')
            finally:
                context.set_offline(False)
            expect(status).to_contain_text('失败',timeout=30000)
            page.wait_for_function("seq=>replayPages.some(p=>p.after>0 && p.events.some(e=>e.seq===seq && e.status==='failed'))",
                                   arg=failed['seq'],timeout=30000)
            page.wait_for_function('replaySockets.some(s=>s.readyState===1)')
            count = page.evaluate('replayPages.length')
            page.evaluate("""events=>{
                const socket=replaySockets.findLast(s=>s.readyState===1);
                for(const event of events) socket.dispatchEvent(new MessageEvent('message',{data:JSON.stringify(event)}));
            }""",[failed,running,running,failed])
            page.wait_for_function('n=>replayPages.length>n',arg=count)
            expect(status).to_contain_text('失败')
            pages = page.evaluate('replayPages')
            page.reload(wait_until='domcontentloaded')
            expect(select_job()).to_contain_text('失败',timeout=30000)
            page.wait_for_function('seq=>replayPages.some(p=>p.next>=seq)',arg=failed['seq'])
            page.screenshot(path=root/'event-replay-after-refresh.png')
            listed = get(route+'/jobs')
            assert [(r['id'],r['status'],r['attempt']['id']) for r in listed]==[(jid,'failed',data['attempt'])]
            result={'job':jid,'attempt':data['attempt'],'actual_exit_code':7,'offline_completion':True,
                    'running_seq':running['seq'],'failed_seq':failed['seq'],'pages_before_offline':pages_before,
                    'pages_after_reconnect':pages,'pages_after_reload':page.evaluate('replayPages'),
                    'delivered_hint_seq':[failed['seq'],running['seq'],running['seq'],failed['seq']]}
            (root/'browser-replay.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        finally:
            chrome.close()


def scenario(api, *, store=None, **_):
    from live_llm_job_repeat_acceptance import put
    case = api.create_case(CASE)
    marker, release = api.root/'real-executions',api.root/'allow-job-exit'
    put(api,'Ref/complete.py',"from pathlib import Path\nimport time\nwith Path("+repr(str(marker))+
        ").open('a') as f:f.write('started\\n')\ndeadline=time.monotonic()+180\nwhile not Path("+
        repr(str(release))+").exists() and time.monotonic()<deadline:time.sleep(.05)\nraise SystemExit(7)\n")
    submitted = api.call('POST',api.prefix+'/jobs',{'command':COMMAND})
    job = api.call('GET','/jobs/'+str(submitted['id']))
    data={'case':case['id'],'root':str(api.root),'base':api.base,'job':job['id'],
          'attempt':job['attempt']['id'],'token':api.token}
    try:
        subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],str(Path(__file__).resolve()),'--browser'],
                       input=json.dumps(data),text=True,timeout=150,check=True)
    finally:
        release.touch(exist_ok=True)
    assert marker.read_text().splitlines()==['started']
    result=json.loads((api.root/'browser-replay.json').read_text())
    result.update(executions=1,backend='s3' if store else 'fs')
    (api.root/'browser-replay.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


if __name__=='__main__':
    if sys.argv[1:]==['--browser']:
        browser(json.load(sys.stdin))
    else:
        from live_llm_job_repeat_acceptance import main
        bundle = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
        assert (bundle/'index.html').is_file()
        fixture="\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/',StaticFiles(directory="+repr(str(bundle))+",html=True))\n"
        asyncio.run(main(selected_override=scenario,fixture_override=fixture))
