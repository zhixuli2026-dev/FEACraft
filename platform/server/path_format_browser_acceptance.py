"""Verify A13 with native browser clipboard writes and ordinary case APIs."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit


FILE = 'Ref/nested/format-proof.txt'
OWNER = 'A13 clipboard owner'
OTHER = 'A13 independent account'


def record(response, path):
    return response.request.method == 'GET' and urlsplit(response.url).path == '/api/v1' + path


async def browser(data):
    from playwright.async_api import async_playwright, expect

    base, root = data['base'], Path(data['root'])
    result = {'passed': False, 'checks': [], 'model_requests': 0, 'mock_llm_calls': 0, 'channel': data['channel']}
    model_requests = []
    errors = []

    async with async_playwright() as playwright:
        chrome = await playwright.chromium.launch(channel=data['channel'])
        result['browser_version'] = chrome.version

        async def context(token, legacy=None):
            storage = [{'name':'vf_token', 'value':token}]
            if legacy is not None:
                storage.append({'name':'vf_path_format', 'value':legacy})
            ctx = await chrome.new_context(locale='zh-CN', viewport={'width':1700, 'height':1000},
                storage_state={'cookies':[], 'origins':[{'origin':base, 'localStorage':storage}]})
            await ctx.grant_permissions(['clipboard-read', 'clipboard-write'], origin=base)
            page = await ctx.new_page()
            requests = []
            def observe(request):
                parts = urlsplit(request.url)
                if parts.netloc == urlsplit(base).netloc:
                    requests.append((request.method, parts.path, parse_qs(parts.query)))
                if ((request.method == 'POST' and re.search(r'/conversations/[^/]+/(messages|goal:run)$', parts.path))
                        or (request.method == 'POST' and re.search(r'/codex/.*/turns$', parts.path))
                        or parts.hostname == 'api.deepseek.com'):
                    model_requests.append(parts.path)
            page.on('request', observe)
            page.on('pageerror', lambda error: errors.append(type(error).__name__))
            await page.goto(base, wait_until='domcontentloaded')
            assert await page.evaluate('window.isSecureContext')
            return ctx, page, requests

        async def setting(page, expected):
            await page.locator('.dv-tab').filter(has_text=re.compile(r'^设置(?:与账户)?$')).click(timeout=20000)
            control = page.get_by_title('路径文本格式', exact=True)
            await expect(control).to_have_value(expected, timeout=20000)
            return control

        async def open_file(page, name):
            await page.locator('[data-case-name='+json.dumps(name)+']').click(timeout=25000)
            for path in ('Ref', 'Ref/nested'):
                item = page.locator('[data-path='+json.dumps(path)+']')
                await expect(item).to_be_visible(timeout=20000)
                if await item.get_attribute('aria-expanded') == 'false':
                    await item.click()
            await page.locator('[data-path='+json.dumps(FILE)+']').click(timeout=20000)
            editor = page.locator('[data-editor-path='+json.dumps(FILE)+'][data-active="true"]')
            await expect(editor).to_be_visible(timeout=20000)
            assert await editor.get_attribute('title') == FILE

        async def clipboard_is(page, expected):
            await page.wait_for_function('async expected => (await navigator.clipboard.readText()) === expected',
                                         arg=expected, timeout=8000)
            return await page.evaluate('navigator.clipboard.readText()')

        async def copies(page, info, mode):
            sep = '\\' if mode == 'windows' else '/'
            expect_relative = FILE.replace('/', sep)
            expect_storage = (info['prefix'] + '/' + FILE).replace('/', sep)
            await page.get_by_role('button',name='复制相对路径',exact=True).click()
            relative = await clipboard_is(page, expect_relative)
            async with page.expect_response(lambda response: record(response, '/cases/'+str(info['id']))) as received:
                await page.get_by_role('button',name='复制路径',exact=True).click()
            response = await received.value
            assert response.status == 200
            summary = await response.json()
            assert summary['storage_prefix'] == info['prefix'] and '\\' not in summary['storage_prefix']
            full = await clipboard_is(page, expect_storage)
            return {'mode':mode, 'relative':relative, 'storage':full, 'api_storage_prefix':summary['storage_prefix']}

        async def canonical_request(page, requests, case_id):
            editor = page.locator('[data-editor-path='+json.dumps(FILE)+'][data-active="true"]')
            await expect(editor).to_be_visible(timeout=15000)
            match = [query['path'][0] for method, path, query in requests
                     if method == 'GET' and path == f'/api/v1/cases/{case_id}/file'
                     and 'path' in query and query['path'][0].endswith('format-proof.txt')]
            assert match and set(match) == {FILE}, match

        suffix = data['channel'] or 'chromium'
        try:
            owner = data['owner']
            ctx, page, requests = await context(owner['token'])
            try:
                await open_file(page, OWNER)
                control = await setting(page, 'linux')
                result['checks'].append({'fresh_default':await copies(page, owner, 'linux')})
                await control.select_option('windows')
                await expect(control).to_have_value('windows')
                result['checks'].append({'after_windows_switch':await copies(page, owner, 'windows')})
                await canonical_request(page, requests, owner['id'])
                await page.get_by_role('button',name='加入聊天',exact=True).click()
                await expect(page.get_by_test_id('agent-input')).to_have_value('@'+FILE+' ')
                result['canonical_file_query_and_draft'] = FILE
                requests.clear()
                await page.reload(wait_until='domcontentloaded')
                control = await setting(page, 'windows')
                assert await page.evaluate("localStorage.getItem('vf_path_format')") == 'windows'
                await open_file(page, OWNER)
                await canonical_request(page, requests, owner['id'])
                result['windows_reloaded_file_query'] = FILE
                result['checks'].append({'windows_after_reload':await copies(page, owner, 'windows')})
                await setting(page, 'windows')
                await page.screenshot(path=root / (suffix+'-windows.png'))
                await control.select_option('linux')
                await expect(control).to_have_value('linux')
                result['checks'].append({'after_posix_switch':await copies(page, owner, 'linux')})
                await page.reload(wait_until='domcontentloaded')
                await setting(page, 'linux')
                assert await page.evaluate("localStorage.getItem('vf_path_format')") == 'linux'
                await open_file(page, OWNER)
                result['checks'].append({'posix_after_reload':await copies(page, owner, 'linux')})
            finally:
                await ctx.close()

            legacy, page, _ = await context(owner['token'], 'legacy-unrecognised-format')
            try:
                await open_file(page, OWNER)
                await setting(page, 'linux')
                result['checks'].append({'legacy_value_fallback':await copies(page, owner, 'linux')})
            finally:
                await legacy.close()

            other = data['other']
            stranger, page, requests = await context(other['token'])
            try:
                await page.locator('[data-case-name='+json.dumps(OTHER)+']').wait_for(timeout=20000)
                await expect(page.locator('[data-case-name='+json.dumps(OWNER)+']')).to_have_count(0)
                await open_file(page, OTHER)
                await setting(page, 'linux')
                result['checks'].append({'independent_account_clipboard':await copies(page, other, 'linux')})
                await canonical_request(page, requests, other['id'])
                auth = {'Authorization':'Bearer '+other['token']}
                foreign = base+'/api/v1/cases/'+str(owner['id'])
                summary = await stranger.request.get(foreign, headers=auth)
                file = await stranger.request.get(foreign+'/file', params={'path':FILE}, headers=auth)
                assert summary.status == file.status == 404
                assert owner['prefix'] not in await summary.text()
                result['cross_account'] = {'case_status':summary.status, 'file_status':file.status}
            finally:
                await stranger.close()

            assert not errors and not model_requests, (errors, model_requests)
            result['passed'] = True
        except BaseException as error:
            result['failure'] = {'type':type(error).__name__, 'message':str(error)[:1600]}
            raise
        finally:
            result['model_requests'] = len(model_requests)
            result['page_errors'] = errors
            (root / (suffix+'-path-format.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
            await chrome.close()


def run(args):
    from app.services import process_identity_native
    from testsupport import IsolatedServer, SERVER

    bundle = args.dist.resolve(strict=True)
    index = bundle/'index.html'
    html = index.read_text(encoding='utf8')
    script = bundle / re.search(r'\bsrc="/([^"]+\.js)"', html).group(1)
    api = IsolatedServer('path-format-browser', app='path_format_fixture:app')
    (api.root/'path_format_fixture.py').write_text('import os\nfrom app.main import app\n'
        + 'from app.deps import get_current_user\nfrom app.services import process_identity_native\n'
        + 'from fastapi import Depends\nfrom fastapi.staticfiles import StaticFiles\n'
        + '@app.get("/fixture/path-format-process")\n'
        + 'async def fixture_process(user=Depends(get_current_user)):\n'
        + '    return process_identity_native.observe(os.getpid())\n'
        + 'app.mount("/", StaticFiles(directory=' + repr(str(bundle)) + ', html=True))\n', encoding='utf8')
    print('PATH_FORMAT_BROWSER ' + str(api.root), flush=True)
    with api:
        participants = {}
        for key, name in [('owner', OWNER), ('other', OTHER)]:
            case = api.create_case(name)
            api.put(FILE, name + ' fixture content\n')
            summary = api.call('GET', '/cases/'+str(case['id']))
            participants[key] = {'id':case['id'], 'token':api.token, 'prefix':summary['storage_prefix']}
        process = api.call('GET', '/fixture/path-format-process', api_prefix=False)
        for channel in args.channel or ['msedge', 'chromium']:
            payload = {'base':api.base, 'root':str(api.root),
                       'channel': None if channel == 'chromium' else channel, **participants}
            subprocess.run([str(args.browser_python), '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--browser'],
                input=json.dumps(payload), text=True, timeout=240, check=True)
    assert api.proc.poll() is not None
    deadline = time.monotonic()+10
    while (observation := process_identity_native.probe(process))['state'] != 'exited' and time.monotonic() < deadline:
        time.sleep(.1)
    assert observation['state'] == 'exited', observation
    source_paths = [Path(__file__), *(SERVER.parent/'web/src'/name for name in (
        'panels/EditorPanel.tsx', 'panels/SettingsPanel.tsx', 'store.ts', 'utils/pathRefs.ts'))]
    (api.root/'bundle-attestation.json').write_text(json.dumps({
        'bundle':str(bundle), 'index_sha256':hashlib.sha256(index.read_bytes()).hexdigest(),
        'main_js_sha256':hashlib.sha256(script.read_bytes()).hexdigest(),
        'isolated_api_exited':True, 'source_sha256':{
            str(path.relative_to(SERVER.parent)):hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}},
        indent=2), encoding='utf8')
    print('PATH_FORMAT_BROWSER_OK ' + str(api.root), flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', action='append', choices=('msedge', 'chromium'))
        run(parser.parse_args())
