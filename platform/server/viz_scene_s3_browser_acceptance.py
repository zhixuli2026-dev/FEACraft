"""Actual authenticated SeaweedFS edits observed by one native scene renderer in Edge."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from PIL import Image

from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER
from viz_colormap_browser_acceptance import fixture
from viz_information_browser_acceptance import upload, write_result
from viz_scene_watcher_acceptance import FIELD, IMAGE, INITIAL as LOCAL_INITIAL, LOG, SCRIPT


RUNTIME = SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed'
INITIAL = "assert '# D09 S3 initial' in open(__file__, encoding='utf-8').read()\n# D09 S3 initial\n" + LOCAL_INITIAL
INVALID = "VF_surface('Forbidden partial execution', 'Temperature')\nViewSize = (800, 600)\n"
RECOVERED = '''assert __file__ == VIBEFLOW_PARAVIEW_SCRIPT
assert __file__.replace(chr(92), '/').endswith('/D-post/scenes.py')
assert '# D09 S3 recovered' in open(__file__, encoding='utf-8').read()
# D09 S3 recovered
assert saves == 1
assert set(VF_list()) == {'Original surface'}
saves += 1
VF_slice('Recovered section', 'Temperature', 'X', .50)
assert set(VF_list()) == {'Original surface', 'Recovered section'}
VF_shot('figures/scene-watcher.png')
'''
APP_FIXTURE = r'''
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time

from fastapi import Depends, HTTPException
from app import storage
from app.deps import get_current_user
from app.main import app
from app.routers import viz
from app.services import file_transactions, process_identity_native


if OBSERVE_PRECOMMIT:
    write_files = file_transactions.write

    async def recognized_uncommitted_write(db, case, changes, **options):
        result = await write_files(db, case, changes, **options)
        contents = changes.get('D-post/scenes.py')
        if contents is None or b'# D09 S3 initial\n' not in contents:
            return result
        root = Path(__file__).parent
        connection = await db.connection()
        raw = await connection.get_raw_connection()
        candidate = hashlib.sha256(contents).hexdigest()
        observed = await asyncio.to_thread(storage.get_bytes, case.s3_prefix, 'D-post/scenes.py')
        record = {'transaction_open': db.in_transaction() and raw.driver_connection.in_transaction,
                  'physical_source_sha256': hashlib.sha256(observed).hexdigest(), 'candidate_sha256': candidate}
        temporary = root / 'scene-uncommitted.partial'
        temporary.write_text(json.dumps(record), encoding='utf8')
        temporary.replace(root / 'scene-uncommitted.json')
        until = time.monotonic() + 18
        while not (root / 'scene-allow-commit').exists():
            if time.monotonic() >= until:
                raise TimeoutError('The isolated browser did not release the scene save')
            await asyncio.sleep(.025)
        return result

    file_transactions.write = recognized_uncommitted_write


@app.get('/fixture/viz-api-process')
def api_process(user=Depends(get_current_user)):
    return process_identity_native.observe(os.getpid())


@app.get('/fixture/viz-stage/{sid}')
async def active_stage(sid: int, user=Depends(get_current_user)):
    await viz._row(sid, user.id)
    session = viz._sessions.get(sid)
    if session is None or not session.selected['staging_prefix']:
        raise HTTPException(404, 'Owned temporary stage is missing')
    prefix = session.selected['staging_prefix']
    root = storage._fs_path(prefix, '.').resolve(strict=True)
    assert root.is_relative_to(Path(__file__).parent.resolve())

    def record(relative):
        path = root / relative
        if not path.exists():
            return None
        assert path.resolve(strict=True).is_relative_to(root)
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()

    return {'stage': str(root), 'manifest': str(storage._staging_manifest_path(prefix)),
            'marker': str(storage._terminal_cleanup_marker(prefix)),
            'source_prefix': session.selected['source_prefix'],
            'canonical_sha256': record('D-post/scenes.py'),
            'shadow_sha256': record('.vibeflow/attempts/viz-scenes.py'),
            'relay_temporary_files': sorted(path.name for path in
                (root / '.vibeflow/attempts').glob('.viz-scene-*.tmp')),
            'png_sha256': record('D-post/figures/scene-watcher.png')}
'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


def current_session(api, sid):
    return api.call('GET', f'/viz:sessions/{sid}')


def stored_bytes(store, prefix, path):
    key = prefix.rstrip('/') + '/' + path
    response = store.client.get_object(Bucket=store.bucket, Key=key)
    try:
        return response['Body'].read()
    finally:
        response['Body'].close()


def temporary_script_keys(store, prefix):
    keys = []
    physical = store.raw_client.get_paginator('list_objects_v2')
    for page in physical.paginate(Bucket=store.bucket, Prefix=prefix.rstrip('/') + '/'):
        keys.extend(row['Key'] for row in page.get('Contents', []) if any(
            pattern in row['Key'] for pattern in
            ('/.viz-scene-', 'viz-scenes.py', '/.scenes.py.', 'scenes.py.tmp', 'scenes.py.part')))
    return keys


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root, base, sid = Path(settings['root']), settings['base'], settings['session']
    case = base + f"/api/v1/cases/{settings['case']}"
    headers = {'Authorization': 'Bearer ' + settings['token']}
    result = {'passed': False, 'checks': [], 'page_errors': [], 'model_requests': []}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        result['edge'] = edge.version
        window = edge.new_context(viewport={'width':1500, 'height':1000}, locale='zh-CN')
        window.add_cookies([{'name':f'vf_viz_{sid}', 'value':settings['token'], 'domain':'127.0.0.1',
                            'path':f'/viz-proxy/{sid}/', 'httpOnly':True, 'sameSite':'Strict'}])
        page = window.new_page()
        page.set_default_timeout(25_000)
        expect.set_options(timeout=20_000)
        page.on('pageerror', lambda error: result['page_errors'].append(str(error)))
        page.on('request', lambda req: result['model_requests'].append(urlsplit(req.url).path)
                if req.method == 'POST' and urlsplit(req.url).path.endswith(('/messages','/turns','/goal:run')) else None)
        state = page.get_by_test_id('scene-script-status')

        def node(name):
            return page.get_by_role('treeitem', name=name, exact=True)

        def save(content):
            response = page.request.put(case + '/file', headers=headers, data={'path':SCRIPT, 'content':content})
            assert response.status == 200, (response.status, response.text()[:500])
            expected = sha(content.encode('utf8'))
            assert response.json()['content_sha256'] == expected
            return expected

        def observe_pending_commit():
            request = Request(case + '/file', method='PUT', headers={**headers, 'Content-Type':'application/json'},
                              data=json.dumps({'path':SCRIPT, 'content':INITIAL}).encode('utf8'))

            def submit():
                with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
                    return response.status, json.load(response)

            marker = root / 'scene-uncommitted.json'
            expected = sha(INITIAL.encode('utf8'))
            with ThreadPoolExecutor(max_workers=1) as worker:
                pending = worker.submit(submit)
                try:
                    until = time.monotonic() + 12
                    while True:
                        try:
                            evidence = json.loads(marker.read_text(encoding='utf8'))
                            break
                        except (FileNotFoundError, PermissionError):
                            assert time.monotonic() < until and not pending.done(), 'Scene write did not open the SQL window'
                            page.wait_for_timeout(35)
                    assert evidence['transaction_open'] and evidence['candidate_sha256'] == expected, evidence
                    page.wait_for_timeout(2100)
                    expect(state).to_have_text('监听中', timeout=2500)
                    expect(node('Original surface')).to_have_count(0, timeout=2500)
                    assert not pending.done(), 'The ordinary PUT returned before the transaction was released'
                    result['uncommitted'] = evidence
                finally:
                    (root / 'scene-allow-commit').touch()
                status, document = pending.result(timeout=20)
            assert status == 200 and document['content_sha256'] == expected, (status, document)
            result['checks'].append('open_storage_transaction_is_not_rendered_before_commit')
            return expected

        try:
            reply = page.goto(base + f'/viz-proxy/{sid}/index.html', wait_until='domcontentloaded')
            assert reply.status == 200, reply.status
            expect(page.locator('main canvas:visible').first).to_be_visible()
            drawer = page.locator('.v-navigation-drawer')
            if 'v-navigation-drawer--active' not in (drawer.get_attribute('class') or ''):
                page.locator('.v-app-bar-nav-icon').click()
            expect(state).to_have_text('监听中')
            result['initial_script_sha256'] = save(INITIAL) if settings['baseline'] else observe_pending_commit()
            if settings['baseline']:
                page.wait_for_timeout(4300)
                expect(state).to_have_text('监听中')
                expect(node('Original surface')).to_have_count(0)
                result['checks'].append('committed_owner_save_is_not_observed_by_download_only_renderer')
            else:
                expect(node('Original surface')).to_be_visible()
                expect(state).to_have_text('已执行 · 1 个对象')
                result['checks'].append('committed_owner_save_executes_in_open_renderer')
                save(INVALID)
                expect(state).to_contain_text('执行失败')
                expect(node('Forbidden partial execution')).to_have_count(0)
                expect(node('Original surface')).to_be_visible()
                result['checks'].append('view_size_rejects_entire_revision')
                result['recovered_script_sha256'] = save(RECOVERED)
                expect(node('Original surface')).to_be_visible()
                expect(node('Recovered section')).to_be_visible()
                expect(state).to_have_text('已执行 · 2 个对象')
                assert urlsplit(page.url).path == f'/viz-proxy/{sid}/index.html'
                result['checks'].append('next_revision_uses_same_session_and_python_namespace')
            page.screenshot(path=str(root / 'scene-s3-renderer.png'))
            assert not result['model_requests'] and not result['page_errors'], result
            result['passed'] = True
        finally:
            if not result['passed']:
                page.screenshot(path=str(root / 'scene-s3-renderer-failure.png'))
            write_result(root / 'scene-s3-browser.json', result)
            edge.close()


def verify(args):
    import httpx
    from app.services import process_identity_native

    server = IsolatedServer('viz-scene-s3-d09', app='scene_s3_fixture:app')
    root = server.root
    scratch = root / 'native-tmp'
    scratch.mkdir()
    browser_python = args.browser_python.resolve(strict=True)
    env = dict(os.environ, TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch), PYTHONIOENCODING='utf8')
    target = root / 'scene-s3-result.json'
    local = root / Path(FIELD).name
    result = {'passed':False, 'baseline':args.baseline, 'native_fixture':fixture(local), 'model_calls':0,
              'api_port':server.port}
    print('VIZ_SCENE_S3_EVIDENCE', root, flush=True)
    try:
        with ObjectStore(executable=RUNTIME, volume_max=16, authenticated=True) as store:
            result['s3_receipt'] = str(store.root / 'store.json')
            settings = store.settings()
            settings.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
            module = root / 'scene_s3_fixture.py'
            try:
                module.write_text('import os\nos.environ.update(' + repr(settings) + ')\n'
                                  f'OBSERVE_PRECOMMIT = {not args.baseline!r}\n' + APP_FIXTURE, encoding='utf8')
                with server as api:
                    case = api.create_case('S3 live scene source')
                    result['api_process'] = api.call('GET', '/fixture/viz-api-process', api_prefix=False)
                    with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization':'Bearer ' + api.token},
                                      trust_env=False, timeout=100) as client:
                        upload(api, client, local, FIELD)
                        created = client.post(api.prefix + '/viz:sessions', json={'file_path':FIELD})
                        assert created.status_code == 200, created.text[:1200]
                    sid = created.json()['id']
                    running = current_session(api, sid)
                    assert running['phase'] == 'active' and running['alive'] is True
                    result['session'] = {'id':sid, 'port':running['port'], 'process':running['process']}
                    stage_url = f'/fixture/viz-stage/{sid}'
                    try:
                        before = result['stage_before'] = api.call('GET', stage_url, api_prefix=False)
                        prefix = before['source_prefix']
                        assert before['canonical_sha256'] == sha(stored_bytes(store, prefix, SCRIPT))
                        other = api.call('POST', '/auth/register', {'name':'Other scene account',
                            'email':uuid.uuid4().hex + '@example.com', 'password':'isolated-test-123'})
                        foreign = {'Authorization':'Bearer ' + other['token']}
                        api.call('PUT', api.prefix + '/file', {'path':SCRIPT, 'content':INITIAL}, expect=404, extra_headers=foreign)
                        api.call('GET', f'/viz:sessions/{sid}', expect=404, extra_headers=foreign)
                        result['foreign_status'] = [404, 404]
                        subprocess.run([str(browser_python), '-B', '-X', 'utf8', __file__, '--browser'],
                            input=json.dumps({'root':str(root), 'case':case['id'], 'base':api.base, 'token':api.token,
                                              'session':sid, 'baseline':args.baseline}),
                            text=True, encoding='utf8', check=True, timeout=150, cwd=root, env=env)
                        result['browser'] = json.loads((root / 'scene-s3-browser.json').read_text(encoding='utf8'))
                        assert result['browser']['passed']
                        after = result['stage_after'] = api.call('GET', stage_url, api_prefix=False)
                        still_running = current_session(api, sid)
                        assert still_running['phase'] == 'active' and still_running['process'] == running['process']
                        assert after['stage'] == before['stage']
                        selected = INITIAL if args.baseline else RECOVERED
                        expected_sha = sha(selected.encode('utf8'))
                        assert sha(stored_bytes(store, prefix, SCRIPT)) == expected_sha
                        if args.baseline:
                            assert after['canonical_sha256'] == before['canonical_sha256'], after
                            assert after['shadow_sha256'] is None and after['png_sha256'] is None, after
                        else:
                            assert after['canonical_sha256'] == expected_sha and after['png_sha256'] is not None, after
                            assert after['shadow_sha256'] is None and not after['relay_temporary_files'], after
                    finally:
                        stopped = api.call('DELETE', f'/viz:sessions/{sid}', expect=(200, 502))
                        terminal = current_session(api, sid)
                        result['close'] = {'reply':stopped, 'record':terminal}
                    assert stopped == {'ok':True} and terminal['phase'] == 'closed' and terminal['cleanup_verified'], terminal
                    assert terminal['stop']['verified'] and terminal['stop']['scope_empty'], terminal['stop']
                    assert all(not Path(before[name]).exists() for name in ('stage','manifest','marker'))
                    final_script = stored_bytes(store, prefix, SCRIPT)
                    assert sha(final_script) == expected_sha and final_script == selected.encode('utf8')
                    result['persisted_script_sha256'] = sha(final_script)
                    temporary_scripts = result['persisted_temporary_script_keys'] = temporary_script_keys(store, prefix)
                    assert not temporary_scripts, temporary_scripts
                    if not args.baseline:
                        log = stored_bytes(store, prefix, LOG)
                        png = stored_bytes(store, prefix, IMAGE)
                        (root / 'scene-s3-executor.log').write_bytes(log)
                        (root / 'scene-s3-native.png').write_bytes(png)
                        text = log.decode('utf8')
                        assert 'ViewSize 不允许' in text and text.count('exec ok') == 2, text
                        assert 'Original surface' in text and 'Recovered section' in text
                        with Image.open(io.BytesIO(png)) as image:
                            assert image.width > 100 and image.height > 100 and len(image.getcolors(1 << 20)) > 3
                            result['native_png_size'] = [image.width, image.height]
                        assert sha(png) == after['png_sha256']
                        result['persisted_png_sha256'] = sha(png)
                        result['persisted_log_sha256'] = sha(log)
            finally:
                module.unlink(missing_ok=True)
        result['s3_exit_code'] = store.proc.poll()
        assert result['s3_exit_code'] is not None
        result['api_exit_code'] = server.proc.poll()
        assert result['api_exit_code'] is not None
        server.proc = None
        result['api_exit'] = process_identity_native.probe(result['api_process'])
        with socket.socket() as probe:
            probe.settimeout(.25)
            result['api_listener'] = probe.connect_ex(('127.0.0.1', server.port)) == 0
        result['passed'] = True
    except BaseException as error:
        result['failure'] = {'type':type(error).__name__, 'message':str(error)[:1000]}
        raise
    finally:
        write_result(target, result)
    print('VIZ_SCENE_S3_PASS', target, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(SERVER.parent / 'tools'))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--baseline', action='store_true')
        verify(parser.parse_args())
