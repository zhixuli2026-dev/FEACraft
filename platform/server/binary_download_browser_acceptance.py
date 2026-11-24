"""Browser download, error and permission recovery using a local FS API."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit


NAME = '网格 数据-é.bin'
PAYLOAD = bytes(range(256)) * 16384 + b'\x00\x8b\xff'
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    root = Path(settings['root'])
    raw_path = '/api/v1' + settings['case_prefix'] + '/raw'
    waiting = asyncio.Event()
    release = asyncio.Event()
    started = []
    downloads = []
    report = {}

    async with async_playwright() as playwright:
        edge = await playwright.chromium.launch(channel=settings['channel'])
        context = await edge.new_context(viewport={'width': 1450, 'height': 940}, locale='zh-CN')
        await context.add_init_script('''
        if (location.origin === %s) {
          localStorage.setItem('vf_token', %s);
          const create = URL.createObjectURL.bind(URL);
          const revoke = URL.revokeObjectURL.bind(URL);
          window.downloadUrls = { created: [], revoked: [] };
          URL.createObjectURL = blob => {
            const url = create(blob);
            if (blob.size === %d) window.downloadUrls.created.push(url);
            return url;
          };
          URL.revokeObjectURL = url => {
            if (window.downloadUrls.created.includes(url)) window.downloadUrls.revoked.push(url);
            revoke(url);
          };
        }
        ''' % (json.dumps(settings['base']), json.dumps(settings['viewer']), len(PAYLOAD)))
        page = await context.new_page()
        expect.set_options(timeout=15000)
        page.set_default_timeout(15000)
        panel = page.get_by_test_id('binary-file-download')
        button = panel.get_by_role('button', name='下载', exact=True)
        status = panel.get_by_role('status')

        async def observe(route):
            request = route.request
            if urlsplit(request.url).path != raw_path:
                await route.fallback()
                return
            headers = await request.all_headers()
            started.append(headers.get('authorization') == 'Bearer ' + settings['viewer'])
            if len(started) in {1, 4}:
                waiting.set()
                await release.wait()
            await route.continue_()

        async def share(enabled):
            response = await context.request.put(settings['base'] + '/api/v1' + settings['share_path'],
                headers={'Authorization': 'Bearer ' + settings['owner']}, data=settings['share'] | {'enabled': enabled})
            assert response.status == 200, response.status

        async def exact_download(event):
            item = await event.value
            assert await item.failure() is None
            assert item.suggested_filename == NAME, item.suggested_filename
            data = (await item.path()).read_bytes()
            assert len(data) == len(PAYLOAD) and hashlib.sha256(data).hexdigest() == SHA256
            await expect(status).to_have_text('已开始下载')
            return {'file_name': item.suggested_filename, 'bytes': len(data), 'sha256': SHA256}

        def is_raw(response):
            return urlsplit(response.url).path == raw_path and response.request.method == 'GET'

        try:
            await page.route('**/raw?*', observe)
            page.on('download', lambda download: downloads.append(download.suggested_filename))
            await page.goto(settings['base'], wait_until='domcontentloaded')
            await page.locator('[data-case-name=' + json.dumps(settings['case']) + ']').click()
            await page.locator('[data-path=' + json.dumps(NAME, ensure_ascii=False) + ']').click()
            await expect(button).to_be_enabled()
            async with page.expect_download() as first, page.expect_response(is_raw) as response:
                await button.focus()
                await button.press('Enter')
                await asyncio.wait_for(waiting.wait(), timeout=8)
                await expect(status).to_have_text('下载中…')
                await expect(button).to_be_disabled()
                await button.evaluate('(node) => node.click()')
                release.set()
            initial = await response.value
            assert initial.status == 200 and initial.headers.get('content-length') == str(len(PAYLOAD))
            report['first'] = await exact_download(first)
            assert started == [True]
            report['keyboard_pending_and_duplicate_suppressed'] = True
            await panel.screenshot(path=str(root / 'binary-download-success.png'))

            await share(False)
            async with page.expect_response(is_raw) as rejected:
                await button.click()
            assert (await rejected.value).status == 404
            alert = panel.get_by_role('alert')
            await expect(alert).to_contain_text('下载失败（404）')
            await expect(button).to_be_enabled()
            assert len(downloads) == 1
            report['revoked_same_tab_status'] = 404
            await panel.screenshot(path=str(root / 'binary-download-error.png'))

            await share(True)
            async with page.expect_download() as retry, page.expect_response(is_raw) as response:
                await button.click()
            assert (await response.value).status == 200
            report['retry_same_tab'] = await exact_download(retry)
            await expect(alert).to_have_count(0)
            assert started == [True, True, True] and downloads == [NAME, NAME]
            await page.wait_for_function('window.downloadUrls.revoked.length === 2')
            assert await page.evaluate('''() => window.downloadUrls.created.length === 2 &&
                window.downloadUrls.created.every(url => window.downloadUrls.revoked.includes(url) &&
                !Array.from(document.links).some(link => link.href === url))''')
            waiting.clear()
            release.clear()
            async with page.expect_event('requestfailed', predicate=lambda failed: urlsplit(failed.url).path == raw_path) as cancelled:
                await button.click()
                await asyncio.wait_for(waiting.wait(), timeout=8)
                await expect(status).to_have_text('下载中…')
                await page.get_by_role('button', name='关闭 ' + NAME, exact=True).click()
                await expect(panel).to_have_count(0)
                release.set()
            assert 'ERR_ABORTED' in (await cancelled.value).failure
            assert started == [True, True, True, True] and downloads == [NAME, NAME]
            report['closing_pending_download_aborted'] = True
            report['browser_bearer_requests'] = len(started)
            report['blob_urls_reclaimed'] = 2
            report['passed'] = True
        finally:
            release.set()
            (root / 'browser-download.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
            await edge.close()


def verify(args):
    import httpx
    from testsupport import IsolatedServer

    api = IsolatedServer('a21-binary-download', app='binary_download_fixture:app')
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    (api.root / 'binary_download_fixture.py').write_text('from app.main import app\n'
        'from fastapi.staticfiles import StaticFiles\n'
        + 'app.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n', encoding='utf8')
    temp = api.root / 'browser-tmp'
    temp.mkdir()
    report = {'passed': False, 'storage': 'fs', 'database': 'sqlite', 'real_model_requests': 0, 'mock_llm_calls': 0}
    print('BINARY_DOWNLOAD_EVIDENCE', api.root, flush=True)
    try:
        with api, httpx.Client(base_url=api.base + '/api/v1', trust_env=False, timeout=30) as client:
            case = api.create_case('Binary download acceptance')
            owner = {'Authorization': 'Bearer ' + api.token}
            params = {'path': NAME}
            raw = api.prefix + '/raw'
            metadata = api.prefix + '/file'
            upload = client.post(api.prefix + '/upload', params=params, headers=owner,
                                 files={'file': (NAME, PAYLOAD, 'application/octet-stream')})
            assert upload.status_code == 200, upload.status_code
            for endpoint in (raw, metadata):
                assert client.get(endpoint, params=params).status_code in {401, 403}
            email = 'download-viewer@example.com'
            account = api.call('POST', '/auth/register', {'email': email, 'name': 'Download viewer',
                               'password': 'isolated-binary-download-123'})
            viewer = {'Authorization': 'Bearer ' + account['token']}
            for endpoint in (raw, metadata):
                assert client.get(endpoint, params=params, headers=viewer).status_code == 404
            org = api.call('GET', '/org/mine')['org']['id']
            api.call('POST', f'/org/{org}/members', {'email': email, 'role': 'viewer'}, expect=201)
            api.call('POST', f'/org/{org}/invitation:accept', {}, extra_headers=viewer)
            workspace = api.call('GET', '/workspaces')[0]['id']
            share_path = f'/workspaces/{workspace}/sharing'
            share = {'role': 'viewer', 'org_id': org}
            api.call('PUT', share_path, share | {'enabled': True})
            meta = client.get(metadata, params=params, headers=viewer).json()
            assert meta['kind'] == 'binary' and meta['size'] == len(PAYLOAD)
            denied = client.post(api.prefix + '/upload', params=params, headers=viewer,
                                 files={'file': (NAME, b'forbidden', 'application/octet-stream')})
            assert denied.status_code in {403, 404}
            head = client.head(raw, params=params, headers=owner)
            assert head.status_code == 200 and not head.content
            assert head.headers['content-length'] == str(len(PAYLOAD)) and head.headers['accept-ranges'] == 'bytes'
            partial = client.get(raw, params=params, headers=owner | {'Range': 'bytes=256-263'})
            assert partial.status_code == 206 and partial.content == PAYLOAD[256:264]
            missing = client.get(raw, params={'path': 'missing.bin'}, headers=owner)
            assert missing.status_code == 404
            report['api'] = {'unshared_account_denied': True, 'unauthenticated_denied': True,
                             'viewer_write_status': denied.status_code, 'head_status': 200,
                             'range_status': 206, 'missing_file_status': 404}
            settings = {'root': str(api.root), 'base': api.base, 'owner': api.token, 'viewer': account['token'],
                'case': case['name'], 'case_prefix': api.prefix, 'share_path': share_path, 'share': share,
                'channel': args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(settings), text=True, check=True, timeout=140,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            report['browser'] = json.loads((api.root / 'browser-download.json').read_text('utf8'))
        report['passed'] = report['browser']['passed'] and api.proc.poll() is not None
    finally:
        report['api_exited'] = api.proc is not None and api.proc.poll() is not None
        (api.root / 'binary-download-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    assert report['passed']
    print('BINARY_DOWNLOAD_PASS', api.root / 'binary-download-result.json', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        verify(parser.parse_args())
