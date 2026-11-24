"""Exercise the six intent fields through the actual editor, revision and report routes."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlencode, urlsplit


INTENT = {
    'userIntent': 'Initial case question',
    'targetOutputs': ['temperature'],
    'acceptanceCriteria': ['residual', 'stability'],
    'assumptions': {'thermalMaterial': '材料在本算例中均匀'},
    'limitations': ['singleCase'],
    'openQuestions': [],
    'decisionLog': ['(', '先复核基准求解', ')'],
}
FIRST = {'userIntent': 'Updated case question', 'openQuestions': ['refinement', '边界条件待现场复核']}
SECOND = {'decisionLog': ['(', '先保留原始测试记录', ')']}
LIFECYCLE = {
    'openQuestions': {'wallCondition': {'text': '确认壁面条件', 'status': 'open'}},
    'assumptions': {'material': {'text': '材料均匀', 'status': 'proposed'}},
    'decisionLog': {'reference': {'text': '采用解析参考解', 'status': 'accepted'}},
}


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    root = Path(settings['root'])
    case = f"/api/v1/cases/{settings['case_id']}"
    headers = {'Authorization': 'Bearer ' + settings['token']}
    report = {'passed': False, 'revisions': []}
    expect.set_options(timeout=25000)
    async with async_playwright() as driver:
        chromium = await driver.chromium.launch(channel=settings['channel'])
        context = await chromium.new_context(viewport={'width': 1500, 'height': 950})
        await context.add_init_script('if(location.origin===' + json.dumps(settings['base'])
            + ')localStorage.setItem("vf_token",' + json.dumps(settings['token']) + ');')
        page = await context.new_page()
        page.set_default_timeout(25000)
        editor = page.get_by_test_id('block-editor')

        async def show_intent(case_name=settings['case_name'], *, reload=True):
            if reload:
                await page.goto(settings['base'])
            await page.locator('[data-case-name=' + json.dumps(case_name) + ']').click()
            if not await editor.is_visible():
                tab = page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$'))
                if await tab.is_visible():
                    await tab.click()
            await expect(editor).to_be_visible()
            await editor.get_by_label('编辑的字典块', exact=True).select_option('intent')

        async def field(name):
            keys = await editor.get_by_placeholder('键', exact=True).evaluate_all('(nodes) => nodes.map(node => node.value)')
            return editor.get_by_placeholder(re.compile(r'^值（')).nth(keys.index(name))

        async def read(endpoint):
            response = await context.request.get(settings['base'] + case + endpoint, headers=headers)
            assert response.status == 200
            return await response.json()

        async def check_inputs(expected):
            for name, value in expected.items():
                actual = await (await field(name)).input_value()
                assert (actual if isinstance(value, str) else json.loads(actual)) == value, (name, actual)

        async def save(changes, expected):
            before = await read('/caseDict')
            for name, value in changes.items():
                await (await field(name)).fill(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
            async with page.expect_response(lambda response: urlsplit(response.url).path == case + '/caseDict/block'
                and response.request.method == 'PUT') as received:
                await editor.get_by_role('button', name='应用该块', exact=True).click()
            response = await received.value
            assert response.status == 200, await response.text()
            request = response.request.post_data_json
            assert request['expected_revision'] == before['dict_revision'] and request['expected_sha256'] == before['content_sha256']
            assert request['entries'] == expected
            await expect(editor.get_by_role('status')).to_contain_text('已保存')
            await expect(editor).to_have_attribute('data-dirty', 'false')
            latest = await read('/caseDict')
            assert latest['dict_revision'] == before['dict_revision'] + 1 and latest['parsed']['intent'] == expected
            revision = (await read('/provenance'))['events'][0]
            assert revision['intent'] == expected and revision['casedict_sha256'] == latest['content_sha256']
            report['revisions'].append(revision['revision'])

        try:
            await show_intent()
            await check_inputs(INTENT)
            await save(FIRST, INTENT | FIRST)
            await save(SECOND, INTENT | FIRST | SECOND)
            await show_intent()
            await check_inputs(INTENT | FIRST | SECOND)
            await editor.screenshot(path=str(root / 'intent-editor.png'))
            if 'lifecycle_case' in settings:
                selected = settings['lifecycle_case']
                case = f"/api/v1/cases/{selected['id']}"
                await show_intent(selected['name'])
                panel = page.locator('details.case-intent-history')
                summary = panel.locator('summary')
                await expect(panel).not_to_have_attribute('open', '')
                await summary.focus()
                await summary.press('Space')
                current = panel.get_by_label('当前状态', exact=True)
                await expect(current).to_contain_text('wallCondition')
                await expect(current).to_contain_text('待解决')
                await panel.get_by_role('button', name='较早修订').click()
                await expect(panel.locator('ol')).to_contain_text('新增，待解决')
                await expect(panel.get_by_role('button', name='较早修订')).to_be_disabled()

                before = await read('/caseDict')
                changes = {'openQuestions': {'wallCondition': {'text': '壁面条件已确认', 'status': 'resolved'}}}
                await save(changes, before['parsed']['intent'] | changes)
                await expect(current).to_contain_text('已解决')
                await expect(panel.locator('ol')).to_contain_text('待解决 → 已解决')
                await expect(panel.get_by_role('button', name='较新修订')).to_be_disabled()
                await panel.screenshot(path=str(root / 'intent-lifecycle.png'))
                await page.set_viewport_size({'width': 1040, 'height': 780})
                await expect(panel.locator('ol')).to_contain_text('已解决')
                assert await panel.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
                await panel.screenshot(path=str(root / 'intent-lifecycle-narrow.png'))

                await show_intent(settings['case_name'], reload=False)
                await expect(panel).not_to_have_attribute('open', '')
                await panel.locator('summary').click()
                await expect(panel.get_by_label('当前状态', exact=True)).to_contain_text('thermalMaterial')
                await expect(panel).not_to_contain_text('wallCondition')
                report['lifecycle'] = {'keyboard_open':True, 'paged_history':True, 'live_revision_refresh':True,
                                       'narrow_layout':True, 'case_switch_isolated':True}
            report['passed'] = True
        except Exception:
            await page.screenshot(path=str(root / 'intent-browser-failure.png'))
            raise
        finally:
            (root / 'intent-browser-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
            await chromium.close()


def verify(args):
    from app.services.casedict import find_block_span
    from testsupport import IsolatedServer

    api = IsolatedServer('case-intent-browser', app='case_intent_fixture:app')
    bundle = args.dist.resolve(strict=True)
    assert (bundle / 'index.html').is_file()
    (api.root / 'case_intent_fixture.py').write_text('from app.main import app\n'
        + 'from fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='
        + repr(str(bundle)) + ', html=True))\n', encoding='utf8')
    temporary = api.root / 'browser-tmp'
    temporary.mkdir()
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0}

    def compose(prefix, name):
        path = 'E-report/report.generated.md'
        prepared = api.call('POST', prefix + '/report:compose?wait=false', {'output': path}, expect=(200,202))
        job = api.wait_job(prepared['job_id'])
        assert job['attempt']['process_stop']['verified']
        data = api.call('GET', prefix + '/raw?' + urlencode({'path': path}), raw=True)
        (api.root / name).write_bytes(data)
        return data.decode('utf8'), {'job_id': job['id'], 'sha256': hashlib.sha256(data).hexdigest()}

    try:
        with api:
            case = api.create_case('Actual intent roundtrip')
            prefix = api.prefix
            case_dict = prefix + '/caseDict'
            initial = api.call('GET', case_dict)
            api.call('PUT', case_dict + '/block', {'block': 'intent', 'entries': INTENT,
                'expected_revision': initial['dict_revision']})
            before = api.call('GET', case_dict)
            assert before['parsed']['intent'] == INTENT
            before_meta = api.call('GET', prefix)
            before_history = api.call('GET', prefix + '/provenance')
            assert before_history['events'][0]['intent'] == INTENT
            start, end = find_block_span(before['content'], 'intent')
            malformed = before['content'][:start - 1] + 'invalid;' + before['content'][end + 1:]
            reply = api.call('PUT', case_dict, {'content': malformed, 'expected_revision': before['dict_revision']}, expect=422)
            assert 'intent' in reply['detail'] and api.call('GET', case_dict) == before
            assert api.call('GET', prefix) == before_meta and api.call('GET', prefix + '/provenance') == before_history
            report['invalid_block_rejected_atomically'] = True
            foreign = api.call('POST', '/auth/register', {'email': 'isolated-intent-outsider@example.com',
                'name': 'Other organization', 'password': 'isolated-test-123'})
            headers = {'Authorization': 'Bearer ' + foreign['token']}
            api.call('GET', prefix + '/provenance', expect=404, extra_headers=headers)
            api.call('PUT', case_dict + '/block', {'block': 'intent', 'entries': INTENT}, expect=404, extra_headers=headers)
            report['foreign_read_and_write_denied'] = True

            settings = {'root': str(api.root), 'base': api.base, 'case_id': case['id'],
                'case_name': case['name'], 'token': api.token, 'channel': args.channel}
            if args.lifecycle:
                lifecycle_case = api.call('POST', f"/workspaces/{before_meta['workspace_id']}/cases",
                                          {'name': 'Engineering intent lifecycle'})
                settings['lifecycle_case'] = {key: lifecycle_case[key] for key in ('id', 'name')}
                for revision in range(13):
                    api.call('PUT', f"/cases/{lifecycle_case['id']}/caseDict/block",
                             {'block': 'intent', 'entries': {**LIFECYCLE, 'userIntent': f'Thermal verification {revision}'}})
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(settings), text=True, timeout=200, check=True,
                env=dict(os.environ, TEMP=str(temporary), TMP=str(temporary)))
            report['browser'] = json.loads((api.root / 'intent-browser-result.json').read_text(encoding='utf8'))
            final = api.call('GET', case_dict)
            history = api.call('GET', prefix + '/provenance')
            assert final['parsed']['intent'] == INTENT | FIRST | SECOND
            assert history['chain_valid'] and history['total'] == before_history['total'] + 2
            assert history['events'][0]['intent'] == INTENT | FIRST | SECOND
            assert history['events'][1]['intent'] == INTENT | FIRST and history['events'][2] == before_history['events'][0]
            compose_page = api.call('GET', prefix + '/provenance?offset=2&limit=1')
            assert compose_page['events'][0]['intent'] == INTENT
            for block in ('caseMeta', 'physicalProblem'):
                start, end = find_block_span(before['content'], block)
                new_start, new_end = find_block_span(final['content'], block)
                assert before['content'][start:end] == final['content'][new_start:new_end]

            rendered, report['native_current_report'] = compose(prefix, 'intent-generated-report.md')
            for expected in ('temperature', 'residual', 'stability', '材料在本算例中均匀',
                             'singleCase', '边界条件待现场复核', '先保留原始测试记录'):
                assert expected in rendered
            report['current_report_contains_all_six_fields'] = True
            assert api.call('GET', case_dict) == final and api.call('GET', prefix + '/provenance') == history

            workspace = before_meta['workspace_id']
            legacy = api.call('POST', f'/workspaces/{workspace}/cases', {'name': 'Legacy intent synonyms'})
            legacy_prefix = f"/cases/{legacy['id']}"
            legacy_values = {'primaryAssumptions': ['Existing thermal assumptions'], 'knownLimitations': 'Existing scope limit'}
            api.call('PUT', legacy_prefix + '/caseDict/block', {'block': 'intent', 'entries': legacy_values})
            assert api.call('GET', legacy_prefix + '/caseDict')['parsed']['intent'] == legacy_values
            assert api.call('GET', legacy_prefix + '/provenance')['events'][0]['intent'] == legacy_values
            rendered, report['native_legacy_report'] = compose(legacy_prefix, 'intent-generated-legacy-report.md')
            assert 'Existing thermal assumptions' in rendered and 'Existing scope limit' in rendered
            report['legacy_fields_read_and_render'] = True
        report['passed'] = api.proc.poll() is not None
    finally:
        report['api_exited'] = api.proc is not None and api.proc.poll() is not None
        (api.root / 'intent-roundtrip-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print('INTENT_ROUNDTRIP_BROWSER_OK', api.root, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        parser.add_argument('--lifecycle', action='store_true')
        verify(parser.parse_args())
