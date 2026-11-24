"""Observe actual scene saves and recovery through the current Edge/VTK session."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit


CASE = 'Actual scene script revisions'
FIELD = 'Ref/live-scene.vtp'
SCRIPT = 'D-post/scenes.py'
LOG = 'D-post/logs/viz_executor.log'
IMAGE = 'D-post/figures/scene-watcher.png'
INITIAL = """assert __file__ == VIBEFLOW_PARAVIEW_SCRIPT
assert __file__.replace(chr(92), '/').endswith('/D-post/scenes.py')
saves = 1
VF_surface('Original surface', 'Temperature')
"""
RECOVERED = """assert __file__ == VIBEFLOW_PARAVIEW_SCRIPT
assert saves == 1
saves += 1
VF_slice('Recovered section', 'Temperature', 'X', .50)
"""
LATEST = """assert saves == 2
saves += 1
VF_slice('Recovered section', 'Temperature', 'X', .65)
VF_surface('Newest revision', 'Temperature')
assert set(VF_list()) == {'Original surface', 'Recovered section', 'Newest revision'}
VF_shot('figures/scene-watcher.png')
"""
AFTER = """assert __file__ == VIBEFLOW_PARAVIEW_SCRIPT
assert saves == 3
VF_remove('Newest revision')
VF_surface('Restored watcher', 'Temperature')
"""


def gui(settings):
    from PIL import Image
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    api = base + '/api/v1'
    case = api + f"/cases/{settings['case']}"
    auth = {'Authorization': 'Bearer ' + settings['token']}
    result = {'passed': False, 'checks': [], 'model_requests': [], 'page_errors': [], 'storage_busy_retries': 0}
    session = None
    with sync_playwright() as driver:
        edge = driver.chromium.launch(channel='msedge')
        result['edge'] = edge.version
        context = edge.new_context(viewport={'width': 1520, 'height': 1010}, locale='zh-CN')
        context.add_init_script(f"if (location.origin === {json.dumps(base)}) localStorage.setItem('vf_token', {json.dumps(settings['token'])});")
        page = context.new_page()
        page.set_default_timeout(28_000)
        expect.set_options(timeout=20_000)
        page.on('pageerror', lambda error: result['page_errors'].append(str(error)))
        page.on('request', lambda req: result['model_requests'].append(urlsplit(req.url).path)
                if req.method == 'POST' and urlsplit(req.url).path.endswith(('/messages', '/turns', '/goal:run')) else None)
        frame = page.frame_locator('iframe[title="可视化"]')
        drawer = frame.locator('.v-navigation-drawer')
        drawer_open = re.compile(r'\bv-navigation-drawer--active\b')
        state = frame.get_by_test_id('scene-script-status')

        def node(name):
            return frame.get_by_role('treeitem', name=name, exact=True)

        def write_with_retry(send):
            deadline = time.monotonic() + 6
            while True:
                response = send()
                if response.status != 409:
                    return response
                detail = response.json().get('detail')
                if not isinstance(detail, dict) or detail.get('code') != 'storage_quota_busy':
                    return response
                if time.monotonic() >= deadline:
                    return response
                result['storage_busy_retries'] += 1
                page.wait_for_timeout(200)

        def save(content):
            response = write_with_retry(lambda: page.request.put(case + '/file', headers=auth,
                                                                  data={'path': SCRIPT, 'content': content}))
            assert response.ok, response.text()[:500]

        def save_native(content):
            target = Path(settings['local_script']).resolve(strict=True)
            assert target.is_relative_to((root / 'files').resolve(strict=True))
            temporary = target.with_name('scene-watcher-fixture.tmp')
            temporary.write_text(content, encoding='utf8', newline='\n')
            temporary.replace(target)

        def malformed():
            data = b'\xff\xfe\x80 invalid UTF-8 scene text\n'
            path = case + '/uploads'
            reply = write_with_retry(lambda: page.request.post(path, headers=auth,
                data={'path': SCRIPT, 'size': len(data), 'overwrite': True, 'sha256': hashlib.sha256(data).hexdigest()}))
            assert reply.ok, reply.text()[:500]
            entry = path + '/' + reply.json()['id']
            assert write_with_retry(lambda: page.request.put(entry, headers=auth, params={'offset': 0}, data=data)).ok
            complete = write_with_retry(lambda: page.request.post(entry + ':complete', headers=auth))
            assert complete.ok and complete.json()['sha256'] == hashlib.sha256(data).hexdigest()

        def raw(path):
            response = page.request.get(case + '/raw', headers=auth, params={'path': path})
            assert response.ok, (path, response.status)
            return response.body()

        def scene_log():
            return raw(LOG).decode('utf-8')

        def open_drawer():
            expect(state).to_be_visible()
            if not drawer_open.search(drawer.get_attribute('class') or ''):
                frame.locator('.v-app-bar-nav-icon').click()
            expect(drawer).to_have_class(drawer_open)

        def activate_case():
            row = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
            if row.get_attribute('aria-pressed') != 'true':
                row.click()

        try:
            page.goto(base, wait_until='domcontentloaded')
            activate_case()
            folder = page.locator('[data-path="Ref"]')
            if folder.get_attribute('aria-expanded') == 'false':
                folder.click()
            with page.expect_response(lambda response: response.request.method == 'POST' and response.url == case + '/viz:sessions',
                                      timeout=50_000) as opened:
                page.locator('[data-path=' + json.dumps(FIELD) + ']').click()
            assert opened.value.ok, opened.value.text()
            session = opened.value.json()['id']
            expect(page.locator('iframe[title="可视化"]')).to_have_attribute('src', re.compile(f'^/viz-proxy/{session}/index.html'))
            open_drawer()
            expect(state).to_have_text('监听中')
            save(INITIAL)
            expect(node('Original surface')).to_be_visible()
            expect(state).to_have_text('已执行 · 1 个对象')
            result['checks'].append('first_public_save_executes_with_real_case_context')

            malformed()
            expect(state).to_contain_text('执行失败')
            assert 'UnicodeDecodeError' in scene_log()
            expect(node('Original surface')).to_be_visible()
            save(RECOVERED)
            expect(node('Recovered section')).to_be_visible()
            expect(state).to_have_text('已执行 · 2 个对象')
            result['checks'].append('binary_encoding_failure_retains_scene_and_namespace')

            before = scene_log()
            start = time.monotonic()
            save_native("VF_surface('Superseded revision', 'Temperature')\n")
            page.wait_for_timeout(210)
            save_native(LATEST)
            result['rapid_save_seconds'] = time.monotonic() - start
            assert result['rapid_save_seconds'] < 0.49, result['rapid_save_seconds']
            expect(state).to_have_text('已执行 · 3 个对象')
            expect(node('Newest revision')).to_be_visible()
            expect(node('Superseded revision')).to_have_count(0)
            png = raw(IMAGE)
            (root / 'scene-watcher-native.png').write_bytes(png)
            with Image.open(root / 'scene-watcher-native.png') as image:
                assert image.width > 100 and image.height > 100 and len(image.getcolors(1 << 20)) > 3
            assert '+Recovered section slice Temperature X@0.65' in scene_log()[len(before):]
            page.wait_for_timeout(1150)
            expect(state).to_have_text('已执行 · 3 个对象')
            assert scene_log()[len(before):].count('exec ok') == 1
            result['checks'].append('rapid_saves_execute_latest_revision_once')

            save("VF_surface('Prohibited side effect')\nViewSize = (800, 600)\n")
            expect(state).to_contain_text('执行失败')
            expect(node('Prohibited side effect')).to_have_count(0)
            assert 'ViewSize 不允许' in scene_log()
            save('VF_surface(\n')
            page.wait_for_function("""async ([url, auth, path]) => {
                const request = await fetch(url + '/raw?path=' + encodeURIComponent(path), {headers: auth});
                return (await request.text()).includes('SyntaxError');
            }""", arg=[case, auth, LOG], timeout=12_000)
            save(AFTER)
            expect(node('Restored watcher')).to_be_visible()
            expect(node('Newest revision')).to_have_count(0)
            expect(node('Recovered section')).to_be_visible()
            result['checks'].append('view_size_and_syntax_failures_allow_same_kernel_recovery')

            page.reload(wait_until='domcontentloaded')
            activate_case()
            page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^可视化$')).click()
            close = page.get_by_role('button', name=f'关闭可视化会话 {session}', exact=True)
            close.locator('..').get_by_role('button', name='打开', exact=True).click()
            open_drawer()
            expect(node('Restored watcher')).to_be_visible()
            page.screenshot(path=str(root / 'scene-watcher-workbench.png'))
            endpoint = api + f'/viz:sessions/{session}'
            with page.expect_response(lambda reply: reply.url == endpoint and reply.request.method == 'DELETE', timeout=45_000) as stopped:
                close.click()
            assert stopped.value.ok, stopped.value.text()
            record = page.request.get(endpoint, headers=auth).json()
            assert record['phase'] == 'closed' and record['stop']['verified'] and record['stop']['scope_empty'], record
            result['stop'] = record['stop']
            session = None
            result['checks'].append('browser_rejoin_and_owned_kernel_stop')
            assert not result['model_requests'] and not result['page_errors'], result
            result['passed'] = True
        finally:
            if not result['passed']:
                page.screenshot(path=str(root / 'scene-watcher-failure.png'))
            if session is not None:
                page.request.delete(api + f'/viz:sessions/{session}', headers=auth)
            (root / 'scene-watcher-browser.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
            edge.close()


def verify(args):
    import httpx
    from testsupport import IsolatedServer
    from viz_colormap_browser_acceptance import fixture
    from viz_information_browser_acceptance import upload, write_result

    server = IsolatedServer('viz-scene-watch-d09', app='scene_watch_fixture:app')
    root = server.root
    scratch = root / 'native-tmp'
    scratch.mkdir()
    dist = args.dist.resolve(strict=True)
    env = dict(os.environ, TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch), PYTHONIOENCODING='utf8')
    (root / 'scene_watch_fixture.py').write_text('import os\nfrom fastapi.staticfiles import StaticFiles\n'
        f'os.environ.update(TEMP={str(scratch)!r}, TMP={str(scratch)!r})\nfrom app.main import app\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf8')
    path = root / Path(FIELD).name
    result = {'passed': False, 'native_fixture': fixture(path), 'model_calls': 0}
    print('VIZ_SCENE_WATCH_EVIDENCE', root, flush=True)
    try:
        with server as api:
            case = api.create_case(CASE)
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                              trust_env=False, timeout=30) as client:
                upload(api, client, path, FIELD)
            other = api.call('POST', '/auth/register', {'name': 'Other scene owner', 'email': 'other-scene@example.com',
                                                        'password': 'isolated-test-123'})
            foreign = {'Authorization': 'Bearer ' + other['token']}
            api.call('PUT', api.prefix + '/file', {'path': SCRIPT, 'content': INITIAL}, expect=404, extra_headers=foreign)
            api.call('POST', api.prefix + '/viz:sessions', {'file_path': FIELD}, expect=404, extra_headers=foreign)
            with sqlite3.connect((root / 'test.sqlite').as_uri() + '?mode=ro', uri=True) as connection:
                prefix, = connection.execute('select s3_prefix from cases where id=?', (case['id'],)).fetchone()
            local_script = (root / 'files' / prefix / SCRIPT).resolve(strict=True)
            assert local_script.is_relative_to((root / 'files').resolve(strict=True))
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', __file__, '--browser'],
                input=json.dumps({'root': str(root), 'case': case['id'], 'base': api.base, 'token': api.token,
                                  'local_script': str(local_script)}),
                text=True, encoding='utf8', check=True, timeout=200, cwd=root, env=env)
            result['browser'] = json.loads((root / 'scene-watcher-browser.json').read_text(encoding='utf8'))
            assert result['browser']['passed']
            result['other_account_http'] = 404
        result['isolated_api_exited'] = server.proc.poll() is not None
        assert result['isolated_api_exited']
        result['passed'] = True
    finally:
        write_result(root / 'scene-watcher-result.json', result)
    print('VIZ_SCENE_WATCH_PASS', root / 'scene-watcher-result.json', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        gui(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        verify(parser.parse_args())
