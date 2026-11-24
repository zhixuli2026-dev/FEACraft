"""Actual stage-confirmation and error-form acceptance for the open/blocking gate."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


ERRORS = {
    'open-warning': {'title': '复核未完成', 'stage': 'mesh', 'category': 'meshQuality',
                     'severity': 'warning', 'status': 'open', 'blocking': False},
    'blocking-note': {'title': '保留阻断记录', 'stage': 'mesh', 'category': 'meshQuality',
                      'severity': 'info', 'status': 'open', 'blocking': True},
}


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    root = Path(settings['root'])
    result = {'server_gate_counts': []}
    path = f"/api/v1/cases/{settings['case_id']}"
    headers = {'Authorization': 'Bearer ' + settings['token']}
    expect.set_options(timeout=25000)
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(channel=settings['channel'])
        context = await chromium.new_context(viewport={'width': 1500, 'height': 950})
        await context.add_init_script('if(location.origin===' + json.dumps(settings['base'])
            + ')localStorage.setItem("vf_token",' + json.dumps(settings['token']) + ');')
        page = await context.new_page()
        page.set_default_timeout(25000)
        phase = page.get_by_role('region', name='阶段、输入与工作流')
        heading = phase.get_by_role('heading', name=re.compile(r'^当前阶段'))
        stage_posts = []

        def observe_stage(request):
            if urlsplit(request.url).path == path + '/caseDict:advance' and request.method == 'POST':
                stage_posts.append(request)

        page.on('request', observe_stage)

        async def state():
            values = []
            for endpoint in ('/caseDict', '/provenance'):
                response = await context.request.get(settings['base'] + path + endpoint, headers=headers)
                assert response.status == 200
                values.append(await response.json())
            return values

        async def confirm(open_count, blocking_count):
            before = await state()
            status = 409 if open_count or blocking_count else 200
            await expect(heading).to_have_text('当前阶段 A 几何')
            evidence = phase.locator('details.case-flow-evidence > summary')
            await expect(evidence).to_contain_text('项当前有效')
            await phase.get_by_label('目标阶段', exact=True).select_option('V')
            sent = len(stage_posts)
            await phase.get_by_role('button', name='核对阶段确认', exact=True).click()
            button = phase.get_by_role('button', name='明确确认V阶段', exact=True)
            await expect(button).to_be_enabled()
            assert len(stage_posts) == sent
            async with page.expect_response(lambda response: urlsplit(response.url).path == path + '/caseDict:advance'
                and response.request.method == 'POST') as received:
                await button.click()
            response = await received.value
            assert response.status == status, (response.status, await response.text())
            assert response.request.post_data_json == {'state': 'V', 'confirmed': True,
                'expected_revision': before[0]['dict_revision']}
            if status == 409:
                message = f'{open_count} 个未结清错误和 {blocking_count} 个阻断错误'
                assert message in (await response.json())['detail']
                await expect(phase.get_by_role('alert')).to_contain_text(message)
                await expect(heading).to_have_text('当前阶段 A 几何')
                assert await state() == before
            else:
                receipt = await response.json()
                assert receipt['workflow_state'] == 'V' and receipt['dict_revision'] == before[0]['dict_revision'] + 1
                await expect(heading).to_have_text('当前阶段 V 开发')
                after = await state()
                assert after[1]['events'][0]['source'] == 'workflow-advance'
            result['server_gate_counts'].append({'open': open_count, 'blocking': blocking_count, 'http': status})

        async def change_error(identifier, action, expected):
            entry = page.get_by_test_id('error-entry-' + identifier)
            if action == 'fixed':
                await entry.get_by_test_id('error-resolution-' + identifier).fill('已补齐原始检查记录')
            async with page.expect_response(lambda response: urlsplit(response.url).path == path + '/caseDict/errors/' + identifier
                and response.request.method == 'PATCH') as received:
                await entry.get_by_test_id('error-' + action + '-' + identifier).click()
            response = await received.value
            assert response.status == 200, await response.text()
            await expect(entry).to_have_attribute('data-status', expected)
            summary = (await response.json())['summary']
            return (summary['open'], summary['blocking'])

        try:
            await page.goto(settings['base'])
            await page.locator('[data-case-name=' + json.dumps(settings['case']) + ']').click()
            if not await heading.is_visible():
                tab = page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$'))
                if await tab.is_visible():
                    await tab.click()
            await expect(heading).to_be_visible()
            await confirm(1, 1)
            assert await change_error('blocking-note', 'fixed', 'fixed') == (1, 0)
            await confirm(1, 0)
            assert await change_error('blocking-note', 'reopen', 'open') == (1, 1)
            assert await change_error('open-warning', 'fixed', 'fixed') == (0, 1)
            await confirm(0, 1)
            await phase.screenshot(path=str(root / 'error-gate-blocking-only.png'))
            assert await change_error('blocking-note', 'fixed', 'fixed') == (0, 0)
            await confirm(0, 0)
            result['passed'] = True
        except Exception:
            await page.screenshot(path=str(root / 'error-gate-browser-failure.png'))
            raise
        finally:
            (root / 'error-gate-browser.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
            await chromium.close()


def verify(args):
    from app.services.casedict import set_block
    from testsupport import IsolatedServer

    api = IsolatedServer('case-error-gate', app='case_error_gate_fixture:app')
    bundle = args.dist.resolve(strict=True)
    assert (bundle / 'index.html').is_file()
    override = ''
    if args.staged_schema:
        override = ('import importlib.util,sys\nname="app.services.casedict_schema"\n'
            'spec=importlib.util.spec_from_file_location(name,' + repr(str(args.staged_schema.resolve(strict=True))) + ')\n'
            'mod=importlib.util.module_from_spec(spec)\nsys.modules[name]=mod\nspec.loader.exec_module(mod)\n')
    (api.root / 'case_error_gate_fixture.py').write_text(override + 'from app.main import app\n'
        + 'from app.services import casedict_schema\nimport hashlib\nfrom pathlib import Path\n'
        + '@app.get("/api/v1/test/error-gate-schema-sha256")\n'
        + 'def schema_hash():return {"sha256":hashlib.sha256(Path(casedict_schema.__file__).read_bytes()).hexdigest()}\n'
        + 'from fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='
        + repr(str(bundle)) + ', html=True))\n', encoding='utf8')
    temporary = api.root / 'browser-tmp'
    temporary.mkdir()
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0}

    def advance(prefix, revision, expected):
        return api.call('POST', prefix + '/caseDict:advance', {'state': 'V', 'confirmed': True,
            'expected_revision': revision}, expect=expected)

    try:
        with api:
            actual_schema = api.call('GET', '/test/error-gate-schema-sha256')['sha256']
            expected_schema = args.staged_schema or Path(__file__).parent / 'app/services/casedict_schema.py'
            assert actual_schema == hashlib.sha256(expected_schema.read_bytes()).hexdigest()
            report['actual_schema_sha256'] = actual_schema
            case = api.create_case('Actual open and blocking errors')
            prefix = api.prefix
            current = api.call('GET', prefix + '/caseDict')
            assert api.call('GET', prefix + '/provenance')['total'] == 0
            bad = {
                'errors.summary.open': {'summary': {'open': {'number': 1}}, 'entries': ERRORS},
                'errors.summary.blocking': {'summary': {'blocking': ['0','1']}, 'entries': ERRORS},
                'errors.entries.bad': {'summary': {'total': 1, 'open': 0}, 'entries': {'bad': 'invalid'}},
                'forged_zero_summary': {'summary': {'open': 0, 'blocking': 0}, 'entries': ERRORS},
            }
            for field, block in bad.items():
                response = api.call('PUT', prefix + '/caseDict', {'content': set_block(current['content'], 'errors', block),
                    'expected_revision': current['dict_revision']}, expect=422)
                assert (field if field != 'forged_zero_summary' else 'errors.summary.open') in response['detail']
            assert api.call('GET', prefix + '/caseDict') == current
            assert api.call('GET', prefix + '/provenance')['total'] == 0
            report['invalid_and_forged_summary_denials'] = list(bad)
            text = set_block(current['content'], 'errors', {'summary': {'total': 2, 'open': 1, 'blocking': 1, 'resolved': 0}, 'entries': ERRORS})
            api.call('PUT', prefix + '/caseDict', {'content': text, 'expected_revision': current['dict_revision']})
            settings = {'root': str(api.root), 'base': api.base, 'token': api.token, 'case': case['name'],
                'case_id': case['id'], 'channel': args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(settings), text=True, check=True, timeout=220,
                env=dict(os.environ, TEMP=str(temporary), TMP=str(temporary)))
            report['browser'] = json.loads((api.root / 'error-gate-browser.json').read_text(encoding='utf8'))

            workspace = api.call('GET', prefix)['workspace_id']
            legacy = api.call('POST', f'/workspaces/{workspace}/cases', {'name': 'Historical summary-only error gate'})
            legacy_prefix = f"/cases/{legacy['id']}"
            for open_count, blocking_count in ((1, 0), (0, 1), (0, 0)):
                saved = api.call('GET', legacy_prefix + '/caseDict')
                updated = api.call('PUT', legacy_prefix + '/caseDict', {'content': set_block(saved['content'], 'errors',
                    {'summary': {'open': open_count, 'blocking': blocking_count}}),
                    'expected_revision': saved['dict_revision']})
                current = api.call('GET', legacy_prefix + '/caseDict')
                if open_count or blocking_count:
                    refusal = advance(legacy_prefix, updated['dict_revision'], 409)
                    assert f'{open_count} 个未结清错误和 {blocking_count} 个阻断错误' in refusal['detail']
                    assert api.call('GET', legacy_prefix + '/caseDict') == current
                else:
                    assert advance(legacy_prefix, updated['dict_revision'], 200)['workflow_state'] == 'V'
            report['legacy_summary_gate'] = True
        report['passed'] = api.proc.poll() is not None
    finally:
        report['api_exited'] = api.proc is not None and api.proc.poll() is not None
        (api.root / 'case-error-gate-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print('CASE_ERROR_GATE_BROWSER_OK', api.root, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        parser.add_argument('--staged-schema', type=Path)
        verify(parser.parse_args())
