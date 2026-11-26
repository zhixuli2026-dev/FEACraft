"""Verify Studio Pack job orchestration using a Windows command protocol fixture."""
import argparse
import asyncio
from contextlib import nullcontext
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from zipfile import ZipFile

from flotherm_ecxml_acceptance import NEUTRAL_ECXML
from flotherm_paired_acceptance import XTXML, upload_binary
from testsupport import IsolatedServer


PACK = 'Ref/protocol.pack'
OUTPUT = 'Ref/pack-export'
BROWSER_CASE = 'Pack browser acceptance'


def pack_bytes():
    target = BytesIO()
    with ZipFile(target, 'w') as archive:
        archive.writestr('scenario_file.xml', '<scenario id="protocol-only"/>')
    return target.getvalue()


def protocol_launcher(root):
    """Copy neutral fixtures through the native launch boundary."""
    (root/'fixture.ecxml').write_text(NEUTRAL_ECXML, encoding='utf8')
    (root/'fixture.xtxmla').write_text(XTXML, encoding='utf8')
    launcher = root/'pack protocol probe.bat'
    launcher.write_text(
        '@echo off\n'
        'if not "%1"=="-f" exit /b 91\n'
        'if not exist "%~2" exit /b 92\n'
        'if not exist "%FLOUSERDIR%" exit /b 93\n'
        'if not exist model.pack exit /b 94\n'
        'echo PACK_PROTOCOL_STARTED\n'
        'if exist "%~dp0exit-before-export" exit /b 37\n'
        'if exist "%~dp0hold-for-cancel" (\n'
        '  echo started>"%~dp0running-pack-probe"\n'
        '  "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -NonInteractive -Command "Start-Sleep -Seconds 45"\n'
        '  exit /b 97\n'
        ')\n'
        'copy /b "%~dp0fixture.ecxml" exported\\model.ecxml >nul || exit /b 95\n'
        'copy /b "%~dp0fixture.xtxmla" exported\\model.xtxmla >nul || exit /b 96\n'
        'exit /b 0\n', encoding='ascii', newline='\r\n')
    return launcher


def settle(api, job_id, status):
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        detail = api.call('GET', f'/jobs/{job_id}')
        if not detail['completion_pending'] and detail['status'] in ('succeeded', 'failed', 'cancelled', 'interrupted'):
            assert detail['status'] == status, (detail['status'], detail.get('error'), detail.get('log_backlog'))
            stop = detail['attempt']['process_stop']
            if stop is not None:
                assert stop['verified'] and stop['scope_empty']
                return detail
        time.sleep(.25)
    raise TimeoutError('Pack job did not settle')


def submit(api, output, key, *, expect=200, path=PACK):
    for _ in range(12):
        requests = {200, 409, expect}
        value = api.call('POST', api.prefix + '/flotherm:export-pack',
                         {'pack_path': path, 'output_dir': output}, expect=requests,
                         extra_headers={'Idempotency-Key': key})
        detail = value.get('detail')
        if isinstance(detail, dict) and (detail.get('code') in ('storage_quota_busy', 'case_write_busy')
                or detail.get('code') == 'input_stale' and detail.get('path') == '0-caseDict/case-events.jsonl'):
            time.sleep(.35)
            continue
        assert ('job_id' in value) == (expect == 200), value
        return value
    raise TimeoutError('Pack admission stayed busy')


def require_absent(api, path):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        value = api.call('GET', path, expect=(404, 409))
        detail = value.get('detail')
        if not isinstance(detail, dict):
            return
        assert detail['code'] in ('file_sync_busy', 'storage_quota_busy'), value
        time.sleep(.35)
    raise TimeoutError('Pack output remained fenced after the job stopped')


async def browser(data):
    from playwright.async_api import async_playwright, expect

    root, launcher = Path(data['root']), Path(data['launcher'])
    async with async_playwright() as playwright:
        chrome = await playwright.chromium.launch(channel='msedge')
        ctx = await chrome.new_context(locale='zh-CN', viewport={'width': 1536, 'height': 1060},
            storage_state={'cookies': [], 'origins': [{'origin': data['base'], 'localStorage': [
                {'name': 'vf_token', 'value': data['token']}]}]})
        page = await ctx.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        studio = page.locator('.flotherm-studio:visible')
        panel = studio.locator('details').filter(has=page.locator('summary').filter(has_text=re.compile(r'^Pack 导出$')))
        job = studio.get_by_role('region', name='作业与输出')

        async def open_studio():
            await page.locator('[data-case-name='+json.dumps(BROWSER_CASE)+']').click(timeout=25000)
            await page.locator('.dv-tab').filter(has_text=re.compile(r'^FloTHERM$')).click(timeout=20000)

        await page.goto(data['base'], wait_until='domcontentloaded')
        await open_studio()
        held = launcher.with_suffix('.disabled')
        launcher.rename(held)
        try:
            await panel.locator('summary').click()
            await expect(panel.get_by_text(re.compile('FLOTHERM_BAT'))).to_be_visible()
            await expect(panel.get_by_role('button', name='提交 Pack 导出')).to_have_count(0)
        finally:
            held.rename(launcher)
        await panel.get_by_role('button', name='重新检查导出环境').click()
        await panel.get_by_label('Pack 文件', exact=True).select_option(PACK)
        await panel.get_by_label('Pack 导出目录').fill('../invalid')
        await expect(panel.get_by_role('button', name='提交 Pack 导出')).to_be_disabled()
        await panel.get_by_label('Pack 导出目录').fill(OUTPUT)
        await panel.screenshot(path=root/'pack-desktop.png')
        for _ in range(12):
            async with page.expect_response(lambda response: response.request.method == 'POST' and
                    response.url.endswith('/flotherm:export-pack'), timeout=35000) as response:
                button = (panel.get_by_role('button', name='提交 Pack 导出') if _ == 0 else
                          job.get_by_role('button', name='用原请求确认提交'))
                await button.click()
            reply = await response.value
            if reply.ok:
                submitted = await reply.json()
                break
            assert reply.status == 409 and (await reply.json())['detail']['code'] in ('storage_quota_busy', 'case_write_busy')
            await asyncio.sleep(.5)
        else:
            raise AssertionError('The browser could not confirm the Pack export submission')
        await expect(job.get_by_text('已完成', exact=True)).to_be_visible(timeout=95000)
        await expect(job.get_by_role('button', name='model.xtxmla', exact=True)).to_be_visible()
        await page.reload(wait_until='domcontentloaded')
        await open_studio()
        await expect(job.get_by_text(f'Pack 导出 #{submitted["job_id"]}', exact=True)).to_be_visible(timeout=30000)
        await job.get_by_role('button', name='用 ECXML 预检').click()
        await expect(studio.get_by_label('FloTHERM 输入文件', exact=True)).to_have_value(OUTPUT+'/model.ecxml')
        await studio.get_by_role('button', name='检查输入', exact=True).click()
        await expect(studio.get_by_text('预检通过 · ECXML')).to_be_visible(timeout=25000)

        await page.set_viewport_size({'width': 390, 'height': 844})
        await expect(panel.locator('summary')).to_be_visible()
        if not await panel.evaluate('(element) => element.open'):
            await panel.locator('summary').click()
        await expect(panel.get_by_role('button', name='提交 Pack 导出')).to_be_visible()
        dimensions = await studio.evaluate('(node) => [node.clientWidth, node.scrollWidth]')
        assert dimensions[0] == dimensions[1], dimensions
        await panel.screenshot(path=root/'pack-mobile.png')
        assert not errors, errors
        (root/'pack-browser.json').write_text(json.dumps({'passed': True, 'version': chrome.version,
            'job_id': submitted['job_id'], 'unavailable_and_retry': True, 'ecxml_preflight': True,
            'narrow_studio_widths': dimensions, 'model_calls': 0}, indent=2)+'\n', encoding='utf8')
        await chrome.close()


def verify(browser_python=None, store=None):
    report = {'passed': False, 'protocol_only': True, 'licensed_flotherm': False, 'model_calls': 0}
    api = IsolatedServer('flotherm-pack-studio', app='pack_fixture:app')
    launcher = protocol_launcher(api.root)
    env = {**(store.settings() if store else {}), 'FLOTHERM_BAT': str(launcher)}
    fixture = 'import os\nos.environ.update(' + repr(env) + ')\nfrom app.main import app\n'
    if browser_python:
        dist = Path(__file__).resolve().parents[1]/'web/dist'
        fixture += ('from fastapi.staticfiles import StaticFiles\n'
                    + f'app.mount("/", StaticFiles(directory={str(dist.resolve(strict=True))!r}, html=True))\n')
    (api.root/'pack_fixture.py').write_text(fixture, encoding='utf8')
    report['storage'] = 's3' if store else 'fs'
    print('FLOTHERM_PACK_EVIDENCE', api.root, flush=True)
    packed = pack_bytes()
    try:
        with api:
            api.create_case('Pack protocol acceptance')
            upload_binary(api, api.case_id, PACK, packed)
            endpoint = api.prefix + '/flotherm'
            assert api.call('GET', endpoint + '/pack-export-capability')['available']
            raw = f'{api.prefix}/raw?path='
            original = hashlib.sha256(packed).hexdigest()

            api.call('POST', endpoint + ':export-pack', {
                'pack_path': PACK, 'output_dir': OUTPUT, 'launcher': 'not-allowed.bat',
            }, expect=422)
            (api.root/'exit-before-export').touch()
            failed_output = 'Ref/failed-pack'
            failed = submit(api, failed_output, 'pack-failed')
            failed_detail = settle(api, failed['job_id'], 'failed')
            assert 'PACK_PROTOCOL_STARTED' in failed_detail['log_backlog']
            assert '退出码 37' in failed_detail['log_backlog']
            require_absent(api, raw + failed_output + '/model.ecxml')
            (api.root/'exit-before-export').unlink()
            report['failure_logged_without_file_publication'] = True

            (api.root/'hold-for-cancel').touch()
            cancelled = submit(api, 'Ref/cancelled-pack', 'pack-cancelled')
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not (api.root/'running-pack-probe').exists():
                time.sleep(.15)
            assert (api.root/'running-pack-probe').exists()
            assert api.call('POST', f'/jobs/{cancelled["job_id"]}:cancel', {})['ok']
            settle(api, cancelled['job_id'], 'cancelled')
            require_absent(api, raw + 'Ref/cancelled-pack/model.ecxml')
            (api.root/'hold-for-cancel').unlink()
            report['running_native_scope_cancelled'] = True

            launched = submit(api, OUTPUT, 'pack-success')
            same = submit(api, OUTPUT, 'pack-success')
            assert same['job_id'] == launched['job_id']
            detail = settle(api, launched['job_id'], 'succeeded')
            assert detail['attempt']['outputs']['publication']['id']
            assert 'PACK_PROTOCOL_STARTED' in detail['log_backlog']
            manifest = json.loads(api.call('GET', raw + OUTPUT + '/export-manifest.json', raw=True))
            assert manifest['job_id'] == launched['job_id'] and manifest['pack_sha256'] == original
            assert api.call('GET', raw + OUTPUT + '/model.ecxml', raw=True) == (api.root/'fixture.ecxml').read_bytes()
            assert api.call('GET', raw + OUTPUT + '/model.xtxmla', raw=True) == (api.root/'fixture.xtxmla').read_bytes()
            assert hashlib.sha256(api.call('GET', raw + PACK, raw=True)).hexdigest() == original
            assert 'content' in api.call('POST', endpoint + ':import', {'path': OUTPUT + '/model.ecxml'})
            assert 'detail' in submit(api, OUTPUT, 'pack-cannot-overwrite', expect=409)
            outsider = api.call('POST', '/auth/register', {
                'email': 'pack-outsider@example.com', 'password': 'disposable-pack-test', 'name': 'Other account',
            })['token']
            api.call('GET', endpoint + '/pack-export-capability', expect=404,
                     extra_headers={'Authorization': 'Bearer ' + outsider})
            if browser_python:
                api.create_case(BROWSER_CASE)
                upload_binary(api, api.case_id, PACK, packed)
                subprocess.run([str(browser_python), '-B', '-X', 'utf8', __file__, '--browser'],
                    input=json.dumps({'root':str(api.root), 'base':api.base, 'token':api.token,
                                      'launcher':str(launcher)}).encode(), check=True, timeout=230)
                report['browser'] = json.loads((api.root/'pack-browser.json').read_text('utf8'))
            report.update(passed=True, publication=detail['attempt']['outputs']['publication']['id'],
                          same_job_on_retry=True, existing_output_protected=True, original_pack_preserved=True,
                          peer_account_denied=True, neutral_import_verified=True, pack_sha256=original)
    finally:
        (api.root/'pack-result.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
    print('FLOTHERM_PACK_PROTOCOL_PASS', api.root/'pack-result.json', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--browser-python', type=Path)
        parser.add_argument('--seaweed', type=Path, help='Use the authenticated SeaweedFS test fixture')
        args = parser.parse_args()
        if args.seaweed:
            from s3testsupport import ObjectStore
            target = ObjectStore(executable=args.seaweed, authenticated=True, volume_max=6)
        else:
            target = nullcontext()
        with target as store:
            verify(args.browser_python, store)
        if store:
            assert store.proc.poll() is not None and not (store.root/'s3-identity.json').exists()
