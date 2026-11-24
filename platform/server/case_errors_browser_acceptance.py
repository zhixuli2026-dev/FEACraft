"""Verify the error editor, native evidence references, and signed revisions."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote, urlsplit


ERROR_ID = 'cb15-geometry'
REPORT_PATH = 'Ref/几何核对 记录.txt'
NATIVE_MARKER = 'CB15_NATIVE_CADQUERY_EVIDENCE'


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    evidence = Path(settings['root'])
    path = f"/api/v1/cases/{settings['case_id']}"
    headers = {'Authorization': 'Bearer ' + settings['token']}
    report = {'statuses': [], 'rejected_statuses': [], 'evidence': []}
    expect.set_options(timeout=20000)
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(channel=settings['channel'])
        context = await chromium.new_context(viewport={'width': 1380, 'height': 920})
        await context.add_init_script('if(location.origin===' + json.dumps(settings['base']) +
            ')localStorage.setItem("vf_token",' + json.dumps(settings['token']) + ');')
        page = await context.new_page()
        page.set_default_timeout(15000)
        section = page.get_by_role('region', name='算例错误', exact=True)
        entry = section.get_by_test_id('error-entry-' + ERROR_ID)
        add = section.get_by_test_id('error-create-form')
        route = path + '/caseDict/errors'

        def save():
            (evidence / 'browser.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')

        async def open_case(reload=False):
            if reload:
                await page.reload()
            else:
                await page.goto(settings['base'])
            if not await section.is_visible():
                await page.locator('[data-case-name=' + json.dumps(settings['case'], ensure_ascii=False) + ']').click()
                tab = page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$'))
                if not await section.is_visible() and await tab.is_visible():
                    await tab.click()
            await expect(section).to_be_visible()

        async def click_write(button, method, suffix='', status=200):
            async with page.expect_response(lambda response: urlsplit(response.url).path == route + suffix
                and response.request.method == method) as received:
                await button.click()
            response = await received.value
            assert response.status == status, (response.status, await response.text())
            return await response.json()

        async def add_path(form, reference):
            await form.get_by_label('相关证据文件', exact=True).fill(reference)
            await form.get_by_role('button', name='添加证据', exact=True).click()
            await expect(form.get_by_role('button', name=reference, exact=True)).to_be_visible()

        await open_case()
        await section.get_by_role('button', name='新增错误', exact=True).click()
        await add.get_by_label('错误编号', exact=True).fill(ERROR_ID)
        await add.get_by_label('标题', exact=True).fill('核对几何构建')
        await add.get_by_label('阶段', exact=True).select_option('geometry')
        await add.get_by_label('分类', exact=True).select_option('geometry')
        await add.get_by_label('严重程度', exact=True).select_option('warning')
        await add.get_by_label('阻断阶段推进', exact=True).check()
        await add.get_by_label('错误详情', exact=True).fill('等待检查 CadQuery 输出。')
        await add_path(add, settings['job_log'])
        created = await click_write(add.get_by_role('button', name='创建错误', exact=True), 'POST')
        assert created['summary'] == {'total': 1, 'open': 1, 'blocking': 1, 'resolved': 0, 'lastErrorId': ERROR_ID}
        await expect(entry).to_have_attribute('data-status', 'open')
        report['statuses'].append('open')
        save()

        await entry.get_by_label('标题', exact=True).fill('几何结果需要复验')
        await entry.get_by_label('阶段', exact=True).select_option('post')
        await entry.get_by_label('分类', exact=True).select_option('postprocess')
        await entry.get_by_label('严重程度', exact=True).select_option('fatal')
        await entry.get_by_label('阻断阶段推进', exact=True).uncheck()
        await entry.get_by_label('错误详情', exact=True).fill('比较实体体积、运行日志和校验记录。')
        await add_path(entry, REPORT_PATH)
        updated = await click_write(entry.get_by_role('button', name='保存错误字段', exact=True), 'PATCH', '/' + ERROR_ID)
        assert updated['summary']['blocking'] == 0
        await expect(entry).to_have_attribute('data-dirty', 'false')
        await expect(entry.get_by_text('几何结果需要复验 · ' + ERROR_ID, exact=True)).to_be_visible()
        report['metadata_updated'] = True
        save()

        async with page.expect_response(lambda response: urlsplit(response.url).path == path + '/file'
            and response.request.method == 'GET' and 'job-' in response.url) as read_log:
            await entry.get_by_role('button', name=settings['job_log'], exact=True).click()
        opened = await read_log.value
        assert opened.status == 200 and NATIVE_MARKER in (await opened.json())['content']
        report['evidence'].append(settings['job_log'])
        await page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$')).click()
        await expect(entry).to_be_visible()

        async def transition(action, status, expected=200):
            button = entry.get_by_test_id(f'error-{action}-{ERROR_ID}')
            receipt = await click_write(button, 'PATCH', '/' + ERROR_ID, expected)
            if expected == 200:
                await expect(entry).to_have_attribute('data-status', status)
                report['statuses'].append(status)
            else:
                report['rejected_statuses'].append(status)
                await expect(entry.get_by_role('alert')).to_be_visible()
            save()
            return receipt

        await transition('investigate', 'investigating')
        await transition('propose', 'fixProposed', 422)
        await entry.get_by_label('修复方案', exact=True).fill('复核实体体积。')
        await transition('propose', 'fixProposed')
        await entry.get_by_label('修复内容', exact=True).fill('已用实际 CadQuery 结果重算。')
        await transition('fixed', 'fixed')
        await transition('verified', 'verified', 422)
        await entry.get_by_label('验证说明', exact=True).fill('日志与核对记录一致，实际体积 24。')
        verified = await transition('verified', 'verified')
        assert verified['summary']['resolved'] == 1 and verified['summary']['open'] == 0
        await expect(section.get_by_test_id('error-summary')).to_have_text('未结 0 · 阻断 0 · 已处理 1')
        await entry.screenshot(path=str(evidence / 'verified-error.png'))

        await open_case(reload=True)
        await expect(entry).to_have_attribute('data-status', 'verified')
        await expect(entry.get_by_label('验证说明', exact=True)).to_have_value('日志与核对记录一致，实际体积 24。')
        await expect(entry.get_by_role('button', name=REPORT_PATH, exact=True)).to_be_visible()
        report['evidence'].append(REPORT_PATH)
        await transition('reopen', 'open')

        await entry.get_by_label('错误详情', exact=True).fill('本地尚待提交的说明')
        current = await context.request.get(settings['base'] + path + '/caseDict', headers=headers)
        snapshot = await current.json()
        external = await context.request.patch(settings['base'] + route + '/' + ERROR_ID, headers=headers,
            data={'message': '同事已补充外部说明', 'expected_revision': snapshot['dict_revision'],
                'expected_sha256': snapshot['content_sha256']})
        assert external.status == 200, await external.text()
        await click_write(entry.get_by_role('button', name='保存错误字段', exact=True), 'PATCH', '/' + ERROR_ID, 409)
        await expect(entry.get_by_label('错误详情', exact=True)).to_have_value('本地尚待提交的说明')
        await entry.get_by_role('button', name='查看服务器版本', exact=True).click()
        await expect(entry.get_by_test_id('form-server-version')).to_contain_text('同事已补充外部说明')
        await entry.get_by_role('button', name='保留草稿，基于此版本继续编辑', exact=True).click()
        await click_write(entry.get_by_role('button', name='保存错误字段', exact=True), 'PATCH', '/' + ERROR_ID)
        report['stale_conflict_preserved_draft'] = True
        await transition('wontfix', 'wontFix')
        await page.set_viewport_size({'width': 780, 'height': 950})
        await expect(entry.get_by_label('相关证据文件', exact=True)).to_be_visible()
        await entry.screenshot(path=str(evidence / 'error-compact.png'))
        save()
        await chromium.close()


def verify(args):
    from app.services.casedict import set_block
    from testsupport import IsolatedServer

    api = IsolatedServer('case-errors-browser', app='cb15_browser_fixture:app')
    bundle = args.dist.resolve(strict=True)
    (api.root / 'cb15_browser_fixture.py').write_text('from app.main import app\n'
        'from fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='
        + repr(str(bundle)) + ', html=True))\n', encoding='utf8')
    temp = api.root / 'browser-tmp'
    temp.mkdir()
    report = {'passed': False, 'model_calls': 0, 'backend': 'fs/sqlite'}
    try:
        with api:
            case = api.create_case('实际错误编辑与验证')
            prefix, token = api.prefix, api.token
            code = ('import cadquery as cq\ndef build_geometry(ctx):\n'
                f'    print({NATIVE_MARKER!r}, cq.__version__, flush=True)\n'
                '    return {"box":cq.Workplane("XY").box(4,2,3)}\n')
            submitted = api.put('A-geo/geometry.py', code)
            job = api.wait_job(submitted['auto_preview_job'])
            assert NATIVE_MARKER in job['log_backlog'] and job['exit_code'] == 0
            assert abs(api.call('GET', prefix + '/geometry/validation')['objects'][0]['volume'] - 24) < 1e-9
            job_log = job['log_path']
            assert NATIVE_MARKER.encode() in api.call('GET', prefix + '/raw?path=' + quote(job_log), raw=True)
            api.put(REPORT_PATH, '实测体积 24，运行日志中的 CadQuery 标识已核对。')
            settings = {'root': str(api.root), 'base': api.base, 'token': token, 'case': case['name'],
                'case_id': case['id'], 'channel': args.channel, 'job_log': job_log}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(settings), text=True, check=True, timeout=170,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            report['browser'] = json.loads((api.root / 'browser.json').read_text(encoding='utf8'))
            dict_path, lifecycle = prefix + '/caseDict', prefix + '/caseDict/errors'
            dictionary = api.call('GET', dict_path)
            chain = api.call('GET', prefix + '/provenance')
            entry = dictionary['parsed']['errors']['entries'][ERROR_ID]
            head = chain['events'][0]
            assert chain['chain_valid'] and not chain['issues']
            assert head['source'] == 'error-lifecycle' and head['revision'] == dictionary['dict_revision']
            assert head['casedict_sha256'] == dictionary['content_sha256']
            signed = head['errors']['entries'][ERROR_ID]
            assert signed['relatedPaths'] == [job_log, REPORT_PATH] and signed['status'] == 'wontFix'
            assert entry['severity'] == 'fatal' and entry['stage'] == 'post' and entry['category'] == 'postprocess'
            assert [e['errors']['entries'][ERROR_ID]['status'] for e in chain['events'] if 'errors' in e
                and ERROR_ID in e['errors'].get('entries', {})].count('verified') == 1
            raw_chain = api.call('GET', prefix + '/raw?path=0-caseDict/provenance.jsonl', raw=True)
            assert head == json.loads(raw_chain.splitlines()[-1])
            report['audit'] = {'sha256': head['event_sha256'], 'errors_sha256': head['errors_sha256'],
                'exact_entry': signed, 'raw_history_sha256': hashlib.sha256(raw_chain).hexdigest(),
                'revision': head['revision'], 'native_job_id': job['id'], 'native_log_path': job_log}
            for body in ({'relatedPaths': ['Ref/missing.txt']}, {'relatedPaths': ['../Ref/not-a-reference.txt']},
                {'severity': 'invalid'}):
                api.call('PATCH', lifecycle + '/' + ERROR_ID, body, expect=422)
            new = {'id': ERROR_ID, 'title': 'Duplicate', 'stage': 'run', 'category': 'solver', 'severity': 'error'}
            assert api.call('POST', lifecycle, new, expect=409)['detail']['code'] == 'error_exists'
            api.create_case('其他账号')
            api.call('POST', lifecycle, new, expect=404)
            api.call('PATCH', lifecycle + '/' + ERROR_ID, {'status': 'open'}, expect=404)
            api.token = token
            assert api.call('GET', dict_path) == dictionary
            assert api.call('GET', prefix + '/provenance') == chain
            report['api_atomic_denials'] = {'invalid_fields_and_refs_422': 3, 'duplicate_409': 1, 'cross_account_404': 2}
            updated = set_block(dictionary['content'], 'intent', {'userIntent': 'Check stable error references.'})
            api.call('PUT', dict_path, {'content': updated, 'expected_revision': dictionary['dict_revision']})
            unchanged_errors = api.call('GET', prefix + '/provenance')['events'][0]
            assert unchanged_errors['errors_sha256'] == head['errors_sha256'] and 'errors' not in unchanged_errors
            report['unchanged_error_snapshot_not_duplicated'] = True
            workspace = api.call('GET', prefix)['workspace_id']
            legacy = api.call('POST', f'/workspaces/{workspace}/cases', {'name': '历史无条目错误汇总'})
            legacy_route = '/cases/' + str(legacy['id']) + '/caseDict'
            initial = api.call('GET', legacy_route)
            legacy_text = set_block(initial['content'], 'errors', {'summary': {'open': 2, 'blocking': 1}})
            api.call('PUT', legacy_route, {'content': legacy_text, 'expected_revision': initial['dict_revision']})
            historical = api.call('GET', legacy_route)
            denied = api.call('POST', legacy_route + '/errors', new, expect=409)
            assert denied['detail']['code'] == 'error_summary_reconciliation_required'
            assert api.call('GET', legacy_route) == historical
            refusal = api.call('POST', legacy_route + ':advance', {'state': 'V', 'confirmed': True,
                'expected_revision': historical['dict_revision']}, expect=409)
            assert '2 个未结清错误和 1 个阻断错误' in refusal['detail']
            reconciled = set_block(historical['content'], 'errors', {'summary': {'open': 0, 'blocking': 0}})
            api.call('PUT', legacy_route, {'content': reconciled, 'expected_revision': historical['dict_revision']})
            assert api.call('POST', legacy_route + '/errors', new)['summary']['open'] == 1
            assert api.call('PATCH', legacy_route + '/errors/' + ERROR_ID, {'status': 'fixed',
                'resolution': 'Checked current case inputs.'})['summary']['resolved'] == 1
            report['legacy_summary_preserved_and_explicitly_reconciled'] = True
        report['passed'] = True
    finally:
        (api.root / 'case-errors-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print('CASE_ERRORS_BROWSER_OK', api.root, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        verify(parser.parse_args())
