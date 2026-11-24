"""Exercise project-tree synchronization in a browser with real APIs and storage."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from urllib.parse import parse_qs, quote, urlsplit

FIXTURE = '''
import tempfile
from fastapi import Depends
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case

@app.get('/api/v1/testing/a12-prefix/{case_id}')
async def case_prefix(case_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    return {'prefix':case.s3_prefix}

@app.post('/api/v1/testing/a12-bulk/{case_id}')
async def external_bulk(case_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    assert not storage._use_s3()
    case = await get_owned_case(case_id, user, db)
    target = storage._fs_path(case.s3_prefix, 'Ref/测量数据/批量')
    storage_root = storage._fs_root().resolve(strict=True)
    with tempfile.TemporaryDirectory(dir=storage_root) as staging:
        staged = Path(staging)/'new-files'
        assert staged.resolve().is_relative_to(storage_root) and target.is_relative_to(storage_root)
        staged.mkdir()
        for i in range(257):
            (staged/f'sample-{i:03}.txt').write_text('bulk data', encoding='utf-8')
        staged.rename(target)
    return {'files':257}
'''


def browser_check(data):
    from playwright.sync_api import expect, sync_playwright

    root = Path(data['root'])
    case, other = data['cases']
    tree_path = f'/api/v1/cases/{case["id"]}/tree'
    event_path = f'/api/v1/cases/{case["id"]}/workflow-events'
    folder = 'Ref/测量数据'
    sibling = folder+'/已有.txt'
    added = folder+'/新增 🚀.txt'
    report = {'checks': [], 'tree_reads': [], 'sync_responses': [], 'event_cursors': []}

    def save():
        (root/'tree-delta-browser.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width':1500,'height':1120}, locale='zh-CN')
        remote = playwright.request.new_context(base_url=data['peer'], extra_http_headers={
            'Authorization':'Bearer '+data['token']})
        page.add_init_script("localStorage.setItem('vf_token',"+json.dumps(data['token'])+")")
        page.add_init_script('''
            const BrowserSocket = window.WebSocket;
            window.treeTestSockets = [];
            window.WebSocket = class extends BrowserSocket {
              constructor(...args) {
                super(...args);
                if (new URL(this.url).pathname === '/ws/events') window.treeTestSockets.push(this);
              }
            };
        ''')

        def read_request(request):
            parsed = urlsplit(request.url)
            path = parsed.path
            if path in {tree_path, tree_path+':patch'}:
                report['tree_reads'].append({'method':request.method,'path':path,
                    'paths': request.post_data_json.get('paths') if request.method=='POST' else None})
            if path == event_path:
                report['event_cursors'].append(int(parse_qs(parsed.query)['after'][0]))

        page.on('request', read_request)
        authorized = {'Authorization':'Bearer '+data['token']}
        full_watermarks = []

        def on_response(response):
            path = urlsplit(response.url).path
            if path == tree_path and response.ok:
                full_watermarks.append(response)
            if path in {tree_path, tree_path+':patch', event_path}:
                report['sync_responses'].append({'path':path,'status':response.status})

        page.on('response',on_response)

        def ready_cursor():
            raw = page.request.get(data['base']+event_path+'?after=0&limit=100',headers=authorized,timeout=6000)
            if not raw.ok:
                return False
            initial = next((event['seq'] for event in raw.json()['events']
                if event.get('source')=='file_watch' and event.get('initial')), None)
            return initial is not None and any(response.json()['watermark'] >= initial for response in full_watermarks)

        def phase(name):
            report['phase'] = name
            save()

        def row(path):
            return page.locator('[data-path='+json.dumps(path, ensure_ascii=False)+']')

        def select_case(value):
            page.locator('[data-case-name='+json.dumps(value['name'], ensure_ascii=False)+']').click()

        def wait_for(predicate, failure):
            deadline = time.monotonic()+25
            while not predicate():
                assert time.monotonic()<deadline, failure
                page.wait_for_timeout(80)

        def settled_cursor():
            result = page.request.get(data['base']+event_path+'?after=0&limit=1',headers=authorized,timeout=6000)
            assert result.ok
            watermark = result.json()['watermark']
            start = len(report['event_cursors'])
            def applied():
                page.evaluate("window.dispatchEvent(new Event('online'))")
                return any(value >= watermark for value in report['event_cursors'][start:])
            wait_for(applied,'Browser did not finish applying prior events')

        def write(path, content=None):
            route = f'/api/v1/cases/{case["id"]}/file'
            deadline = time.monotonic()+25
            while True:
                if content is None:
                    response = remote.delete(route+'?path='+quote(path),timeout=12000)
                else:
                    response = remote.put(route,data={'path':path,'content':content},timeout=12000)
                if response.ok:
                    return
                assert response.status==409 and response.json()['detail']['code'] in {'storage_quota_busy','validation_busy'}, response.status
                assert time.monotonic()<deadline
                page.wait_for_timeout(150)

        def has_patch(path, start=0):
            return any(path in (entry['paths'] or []) for entry in report['tree_reads'][start:])

        def fulls(start=0):
            return sum(entry['method']=='GET' for entry in report['tree_reads'][start:])

        try:
            page.goto(data['base'],wait_until='domcontentloaded')
            select_case(case)
            expect(row(folder)).to_be_visible(timeout=25000)
            row(folder).click()
            expect(row(sibling)).to_be_visible()
            row(folder).evaluate('(node) => { window.folderBeforePatch = node }')
            page.screenshot(path=root/'tree-before.png')

            # Establish a settled event cursor before measuring ordinary writes.
            phase('initial cursor')
            wait_for(ready_cursor,'Browser never read a tree covering the watcher baseline')
            settled_cursor()
            seed = folder+'/已连接.txt'
            write(seed,'watcher ready')
            expect(row(seed)).to_be_visible(timeout=25000)
            wait_for(lambda: has_patch(seed),'Initial incremental update did not occur')
            page.wait_for_timeout(1300)
            start = len(report['tree_reads'])
            write(added,'browser delta backed by actual peer API')
            expect(row(added)).to_be_visible(timeout=25000)
            wait_for(lambda: has_patch(added,start),'External case write did not request its path')
            expect(row(sibling)).to_be_visible()
            assert row(folder).evaluate('(node) => node === window.folderBeforePatch')
            page.screenshot(path=root/'tree-incremental.png')
            modified_from = len(report['tree_reads'])
            write(added,'modified content with a different length')
            wait_for(lambda: has_patch(added,modified_from),'External modification did not refresh its path')
            write(added)
            expect(row(added)).to_have_count(0,timeout=25000)
            expect(row(sibling)).to_be_visible()
            assert not fulls(start), report['tree_reads'][start:]
            report['checks'].append('cross_api_create_modify_delete_with_zero_full_reads_and_preserved_open_folder')
            save()

            if data['network_case']:
                phase('network disk external write')
                network_path = folder+'/网络盘写入.txt'
                start = len(report['tree_reads'])
                (Path(data['network_case'])/network_path).write_text('actual SMB write',encoding='utf-8')
                expect(row(network_path)).to_be_visible(timeout=25000)
                assert has_patch(network_path,start) and not fulls(start)
                report['checks'].append('actual_network_disk_write_incrementally_refreshes_browser')

            if not data['s3']:
                phase('bulk snapshot overflow')
                start = len(report['tree_reads'])
                bulk = page.request.post(data['base']+f'/api/v1/testing/a12-bulk/{case["id"]}',
                                         headers=authorized,timeout=12000)
                assert bulk.ok and bulk.json()['files']==257
                expect(row(folder+'/批量')).to_be_visible(timeout=25000)
                assert fulls(start)>=1
                row(folder+'/批量').click()
                assert row(folder+'/批量/sample-256.txt').count() == 1
                report['checks'].append('actual_path_budget_overflow_reloads_whole_tree')

            settled_cursor()
            aborted = []
            fallback_file = folder+'/整树恢复.txt'

            def fail_patch(route):
                if fallback_file in route.request.post_data_json.get('paths',[]) and not aborted:
                    aborted.append(True)
                    route.abort()
                else:
                    route.continue_()

            page.route('**/tree:patch', fail_patch)
            phase('failed patch fallback')
            start = len(report['tree_reads'])
            write(fallback_file,'actual state recovered after loss of one patch')
            expect(row(fallback_file)).to_be_visible(timeout=25000)
            assert aborted and fulls(start)>=1
            page.unroute('**/tree:patch', fail_patch)
            report['checks'].append('failed_patch_falls_back_to_current_tree')

            seen_cursor_error, seen_zero = [], []

            def force_cursor(route):
                params = parse_qs(urlsplit(route.request.url).query)
                if not seen_cursor_error:
                    actual = route.fetch(url=data['base']+event_path+'?after=9007199254740991&limit=100')
                    assert actual.status == 409
                    seen_cursor_error.append(True)
                    route.fulfill(response=actual)
                else:
                    seen_zero.append(params.get('after') == ['0'])
                    route.continue_()

            page.route('**/workflow-events?**', force_cursor)
            phase('cursor reset')
            page.evaluate("window.dispatchEvent(new Event('online'))")
            wait_for(lambda: any(seen_zero),'Invalid cursor did not restart at zero')
            page.unroute('**/workflow-events?**', force_cursor)
            report['checks'].append('actual_cursor_rejection_reconciles_from_zero')
            settled_cursor()

            delayed = []
            late = folder+'/迟到响应.txt'

            def hold_patch(route):
                if urlsplit(route.request.url).path == tree_path+':patch' and late in route.request.post_data_json.get('paths',[]):
                    delayed.append((route,route.fetch()))
                else:
                    route.continue_()

            page.route('**/tree:patch', hold_patch)
            phase('case switch with pending patch')
            write(late,'arrived after switching cases')
            wait_for(lambda: bool(delayed),'Expected a real pending patch response')
            select_case(other)
            expect(row('Ref/另一算例.txt')).to_be_visible(timeout=25000)
            for route, response in delayed:
                assert response.ok
                route.fulfill(response=response)
            page.wait_for_timeout(350)
            expect(row(late)).to_have_count(0)
            expect(row('Ref/另一算例.txt')).to_be_visible()
            page.screenshot(path=root/'tree-after-switch.png')
            report['checks'].append('late_patch_ignored_after_case_switch')

            phase('offline peer mutation')
            select_case(case)
            expect(row(folder)).to_be_visible(timeout=25000)
            if not row(sibling).is_visible():
                row(folder).click()
            expect(row(sibling)).to_be_visible()
            def usable_socket():
                return page.evaluate('''(caseId) => window.treeTestSockets.some(socket =>
                    new URL(socket.url).searchParams.get('case_id') === String(caseId) && socket.readyState === WebSocket.OPEN
                )''',case['id'])
            wait_for(usable_socket,'The selected case did not connect')
            settled_cursor()
            page.context.set_offline(True)
            page.evaluate('window.treeTestSockets.forEach(socket => socket.close())')
            offline_file = folder+'/断线期间.txt'
            write(offline_file,'persistent while socket is disconnected')
            page.context.set_offline(False)
            expect(row(offline_file)).to_be_visible(timeout=25000)
            def reconnection():
                return page.evaluate('''(caseId) => window.treeTestSockets.some(socket => {
                    const params = new URL(socket.url).searchParams;
                    return params.get('case_id') === String(caseId) && Number(params.get('after')) > 0 && socket.readyState === WebSocket.OPEN;
                })''',case['id'])
            wait_for(reconnection,'Reconnection did not pass its prior cursor')
            report['checks'].append('disconnect_and_reconnect_from_committed_cursor')
            report['connection_cursors'] = page.evaluate('''(caseId) => window.treeTestSockets.flatMap(socket => {
                const params = new URL(socket.url).searchParams;
                return params.get('case_id') === String(caseId) ? [Number(params.get('after'))] : [];
            })''',case['id'])
            phase('complete')
        finally:
            save()
            remote.dispose()
            browser.close()


def scenario(api, *, store=None, **_):
    from live_llm_job_repeat_acceptance import put
    from live_llm_publication_restart_acceptance import worker_peer

    case = api.call('GET',api.prefix)
    workspace = api.call('GET','/workspaces')[0]['id']
    other = api.call('POST',f'/workspaces/{workspace}/cases',{'name':'切换验证算例'})
    path = 'Ref/测量数据/已有.txt'
    put(api,path,'persistent existing editor text')
    prior_id, prior_prefix = api.case_id, api.prefix
    try:
        api.case_id, api.prefix = other['id'], f'/cases/{other["id"]}'
        put(api,'Ref/另一算例.txt','isolated tree')
    finally:
        api.case_id, api.prefix = prior_id, prior_prefix

    # The incremental endpoint has the same read access as the full tree.
    paths = [path,'Ref/测量数据/不存在.txt'] if store is not None else [path,'Ref/测量数据']
    patch = api.call('POST',prior_prefix+'/tree:patch',{'paths':paths})['entries']
    assert any(item['path']==path for item in patch)
    assert len(patch)==(1 if store is not None else 2)
    api.call('POST',prior_prefix+'/tree:patch',{'paths':['../outside.txt']},expect=422)
    email = uuid.uuid4().hex+'@example.com'
    token = api.call('POST','/auth/register',{'name':'Other file tree account',
        'email':email,'password':'delta-guest-test-123'})['token']
    viewer = {'Authorization':'Bearer '+token}
    api.call('POST',prior_prefix+'/tree:patch',{'paths':[path]},expect=404,extra_headers=viewer)
    org = api.call('GET','/org/mine')['org']['id']
    api.call('POST',f'/org/{org}/members',{'email':email,'role':'viewer'},expect=201)
    api.call('POST',f'/org/{org}/invitation:accept',{},extra_headers=viewer)
    share = {'org_id':org,'role':'viewer','enabled':True}
    api.call('PUT',f'/workspaces/{workspace}/sharing',share)
    assert api.call('POST',prior_prefix+'/tree:patch',{'paths':[path]},extra_headers=viewer)['entries'][0]['path']==path
    api.call('PUT',f'/workspaces/{workspace}/sharing',{**share,'enabled':False})
    api.call('POST',prior_prefix+'/tree:patch',{'paths':[path]},expect=404,extra_headers=viewer)

    network_case = None
    if os.environ.get('VIBEFLOW_TEST_TREE_NETWORK') == '1':
        assert store is None and sys.platform == 'win32'
        local = Path(api.env['VIBEFLOW_STORAGE_FS_ROOT']).resolve(strict=True)
        unc = Path('\\\\127.0.0.1\\'+local.drive[0]+'$').joinpath(*local.parts[1:])
        prefix = api.call('GET',f'/testing/a12-prefix/{case["id"]}')['prefix']
        network_case = str(unc/prefix)
        assert (Path(network_case)/path).read_text(encoding='utf-8') == 'persistent existing editor text'

    with worker_peer(api,store) as peer:
        args = {'root':str(api.root),'base':api.base,'peer':peer.base,'token':api.token,'cases':[case,other],
                's3':store is not None,'network_case':network_case}
        try:
            subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],str(Path(__file__).resolve()),'--browser'],
                           input=json.dumps(args),text=True,timeout=190,check=True)
        finally:
            page = api.call('GET',prior_prefix+'/workflow-events?after=0&limit=200')
            trace = [{key:event.get(key) for key in ('seq','type','source','paths_complete','paths')}
                     for event in page['events']]
            (api.root/'tree-events.json').write_text(json.dumps(trace,ensure_ascii=False,indent=2),encoding='utf-8')
    return json.loads((api.root/'tree-delta-browser.json').read_text(encoding='utf-8'))


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser_check(json.load(sys.stdin))
    else:
        from live_llm_job_repeat_acceptance import main

        bundle = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
        assert (bundle/'index.html').is_file()
        fixture = FIXTURE + "\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/', StaticFiles(directory="+repr(str(bundle))+", html=True))\n"
        asyncio.run(main(selected_override=scenario,fixture_override=fixture))
