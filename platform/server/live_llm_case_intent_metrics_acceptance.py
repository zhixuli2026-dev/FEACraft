"""Compare explicitly named caseDict criteria with real OpenFOAM report output."""
import asyncio
from functools import partial
import hashlib
import json
from pathlib import Path
import subprocess
import sys

async def verify(api, *, baseline, browser_dist):
    from app.services.casedict import parse_casedict, set_block
    from live_llm_remote_post_report_acceptance import scenario as solve_and_report
    from sqlalchemy.engine import make_url

    case = api.call('GET', api.prefix + '/caseDict')
    intent = {
        'targetOutputs': {
            'meanTemperature': {'metric': '"mean.T@1"', 'unit': 'K'},
            'wrongUnit': {'metric': '"mean.T@1"', 'unit': 'C'},
            'missingMetric': {'metric': 'unmeasured', 'unit': 'K'},
        },
        'acceptanceCriteria': {
            'temperatureRange': {'target': 'meanTemperature', 'min': 349, 'max': 351},
            'tooCold': {'target': 'meanTemperature', 'max': 340},
            'wrongUnit': {'target': 'wrongUnit', 'min': 349},
            'noEvidence': {'target': 'missingMetric', 'min': 349},
            'badReference': {'target': 'undefinedTarget', 'min': 349},
            'reversedBounds': {'target': 'meanTemperature', 'min': 351, 'max': 349},
            'invalidNumber': {'target': 'meanTemperature', 'min': 'nan'},
        },
        'openQuestions': {'wallCondition': {'text': '"Confirm wall condition"', 'status': 'open'}},
        'assumptions': {'material': {'text': '"Homogeneous material"', 'status': 'proposed'}},
        'decisionLog': {'reference': {'text': '"Compare with analytic result"', 'status': 'accepted'}},
    }
    content = set_block(case['content'], 'intent', intent)
    updated = api.call('PUT', api.prefix + '/caseDict', {
        'content': content, 'expected_revision': case['dict_revision'],
    })
    saved = api.call('GET', api.prefix + '/caseDict')
    parsed = parse_casedict(saved['content'])['intent']
    assert parsed['targetOutputs']['meanTemperature']['metric'] == 'mean.T@1'
    assert parsed['acceptanceCriteria']['temperatureRange']['target'] == 'meanTemperature'
    if not baseline:
        api.call('PUT', api.prefix + '/caseDict/block', {'block': 'intent', 'expected_revision': saved['dict_revision'],
            'entries': {**parsed,
                'openQuestions': {'wallCondition': {'text': 'Wall condition confirmed', 'status': 'resolved'}},
                'assumptions': {'material': {'text': 'Homogeneous material', 'status': 'confirmed'}},
                'decisionLog': {
                    'reference': {'text': 'Compare with analytic result', 'status': 'superseded'},
                    'reference2': {'text': 'Use updated comparison', 'status': 'accepted'},
                }}})
        page = api.call('GET', api.prefix + '/provenance?offset=0&limit=1')
        history = page['intent_lifecycle']
        assert page['chain_valid'] and page['has_more']
        assert history['current'] == {'openQuestions': {'wallCondition': 'resolved'},
            'assumptions': {'material': 'confirmed'}, 'decisionLog': {'reference': 'superseded', 'reference2': 'accepted'}}
        assert history['revisions'][0]['event_id'] == page['events'][0]['event_id']
        assert {(row['kind'], row['id'], row['action'], row['from_status'], row['to_status'])
                for row in history['revisions'][0]['changes']} == {
            ('openQuestions', 'wallCondition', 'updated', 'open', 'resolved'),
            ('assumptions', 'material', 'updated', 'proposed', 'confirmed'),
            ('decisionLog', 'reference', 'updated', 'accepted', 'superseded'),
            ('decisionLog', 'reference2', 'created', None, 'accepted'),
        }
        earlier = api.call('GET', api.prefix + '/provenance?offset=1&limit=1')
        assert earlier['intent_lifecycle']['current'] == history['current']
        assert earlier['intent_lifecycle']['revisions'][0]['revision'] == updated['dict_revision']
        assert {row['action'] for row in earlier['intent_lifecycle']['revisions'][0]['changes']} == {'created'}
        outsider = api.call('POST', '/auth/register', {'email':'intent-metrics-outsider@example.com',
            'name':'Other organization','password':'isolated-test-123'})
        outsider_headers = {'Authorization': 'Bearer ' + outsider['token']}
        api.call('GET', api.prefix+'/provenance', expect=404, extra_headers=outsider_headers)
        api.call('GET', api.prefix+'/report/compose-result', expect=404, extra_headers=outsider_headers)
        organization = api.call('GET', '/org/mine')['org']['id']
        workspace = api.call('GET', api.prefix)['workspace_id']
        api.call('POST', f'/org/{organization}/members',
                 {'email':'intent-metrics-outsider@example.com', 'role':'viewer'}, expect=201)
        api.call('POST', f'/org/{organization}/invitation:accept', {}, extra_headers=outsider_headers)
        share = {'org_id':organization,'role':'viewer','enabled':True}
        api.call('PUT', f'/workspaces/{workspace}/sharing', share)
        viewer = api.call('GET', api.prefix + '/provenance?limit=1', extra_headers=outsider_headers)
        assert viewer['intent_lifecycle'] == history
        api.call('PUT', api.prefix + '/caseDict/block', {'block':'intent','entries':{}},
                 expect=404, extra_headers=outsider_headers)

    database = make_url(api.database_url) if api.database_url is not None else None
    engineering = await solve_and_report(api, selected=database)
    report = api.call('GET', api.prefix + '/report/compose-result?output=E-report%2Freport.md')
    assert report['publication']['applied'] and report['evidence']['report_ready']
    content = report['content']
    assert 'temperatureRange' in content and '| mean.T@1 | volumeAverages |' in content
    result = {'passed': True, 'baseline': baseline, 'intent_revision': updated['dict_revision'],
              'actual_temperature_k': engineering['mean_temperature_k'],
              'target_recorded': True, 'actual_metric_reported': True, 'criterion_evaluated': not baseline}
    if baseline:
        assert 'intent_acceptance' not in report['evidence']
    else:
        acceptance = report['evidence']['intent_acceptance']
        targets = acceptance['targets']
        assert targets['meanTemperature'] == {'status':'measured', 'metric':'mean.T@1',
            'unit':'K', 'value':engineering['mean_temperature_k']}
        evidence = acceptance['evidence']
        metric_bytes = api.call('GET', api.prefix + '/raw?path='+evidence['path'], raw=True)
        assert hashlib.sha256(metric_bytes).hexdigest() == evidence['sha256']
        criteria = acceptance['criteria']
        assert {key: (row['status'], row.get('reason')) for key, row in criteria.items()} == {
            'temperatureRange': ('pass', None), 'tooCold': ('fail', None),
            'wrongUnit': ('unverified', 'unit_mismatch'), 'noEvidence': ('unverified', 'missing_metric'),
            'badReference': ('unverified', 'unknown_target'), 'reversedBounds': ('unverified', 'invalid_bounds'),
            'invalidNumber': ('unverified', 'invalid_bounds'),
        }
        assert '符合' in content and '超出范围' in content
        generated = api.call('GET', api.prefix + '/report/compose-result?output=E-report%2Freport.generated.md')
        assert generated['evidence']['intent_acceptance'] == acceptance
        assert api.call('GET', api.prefix + '/report/compose-result',
                        extra_headers=outsider_headers)['evidence']['intent_acceptance'] == acceptance
        if browser_dist:
            settings = {'base':api.base, 'token':api.token, 'case_name':api.call('GET',api.prefix)['name'],
                        'root':str(api.root)}
            await asyncio.to_thread(subprocess.run, ['C:/ProgramData/anaconda3/python.exe','-B','-X','utf8',
                str(Path(__file__).resolve()), '--report-browser'], input=json.dumps(settings),
                text=True, timeout=90, check=True)
        final = api.call('GET', api.prefix + '/caseDict')
        current = final['parsed']['intent']
        current['openQuestions'] = {}
        receipt = api.call('PUT', api.prefix + '/caseDict/block',
            {'block': 'intent', 'entries': current, 'expected_revision': final['dict_revision']})
        after = api.call('GET', api.prefix + '/provenance?limit=1')
        assert after['intent_lifecycle']['revisions'][0]['revision'] == receipt['dict_revision']
        assert after['intent_lifecycle']['revisions'][0]['changes'] == [
            {'kind':'openQuestions', 'id':'wallCondition', 'action':'removed',
             'from_status':'resolved', 'to_status':None}]
        api.call('PUT', api.prefix + '/caseDict/block',
            {'block':'intent','entries':parsed,'expected_revision':final['dict_revision']}, expect=409)
        assert api.call('GET', api.prefix + '/provenance?limit=1') == after
        saved_report = api.call('GET', api.prefix + '/report/compose-result?publication_id='+report['publication_id'])
        assert saved_report['evidence']['intent_acceptance'] == acceptance and saved_report['publication']['stale']
        api.call('PUT', f'/workspaces/{workspace}/sharing', {**share, 'enabled':False})
        api.call('GET', api.prefix + '/provenance', expect=404, extra_headers=outsider_headers)
        result.update(acceptance=acceptance, lifecycle_paginated=True, lifecycle_removal_recorded=True,
                      historical_report_preserved=True, outsider_denied=True, read_only_access=True,
                      stale_revision_rejected=True, browser_preview=bool(browser_dist))
    (api.root / ('intent-metrics-baseline.json' if baseline else 'intent-metrics-result.json')).write_text(
        json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


def scenario(api, *, baseline, browser_dist, **_):
    return asyncio.run(verify(api, baseline=baseline, browser_dist=browser_dist))


async def report_browser(settings):
    from playwright.async_api import async_playwright, expect

    async with async_playwright() as toolkit:
        browser = await toolkit.chromium.launch(channel='msedge')
        context = await browser.new_context(viewport={'width':1440,'height':915}, locale='zh-CN')
        origin = json.dumps(settings['base'])
        token = json.dumps(settings['token'])
        await context.add_init_script(f"if (location.origin === {origin}) localStorage.setItem('vf_token', {token});")
        page = await context.new_page()
        expect.set_options(timeout=25000)
        await page.goto(settings['base'])
        await page.locator('[data-case-name='+json.dumps(settings['case_name'])+']').click()
        folder = page.locator('[data-path="E-report"]')
        await expect(folder).to_be_visible()
        if await folder.get_attribute('aria-expanded') == 'false':
            await folder.click()
        await page.locator('[data-path="E-report/report.md"]').click()
        preview = page.get_by_test_id('markdown-preview')
        if not await preview.is_visible():
            await page.get_by_test_id('markdown-preview-toggle').click()
        passed = preview.get_by_role('row').filter(has_text='temperatureRange')
        await expect(passed).to_contain_text('符合')
        await expect(passed).to_contain_text('349')
        await expect(preview.get_by_role('row').filter(has_text='tooCold')).to_contain_text('超出范围')
        await expect(preview.get_by_role('row').filter(has_text='noEvidence')).to_contain_text('未找到指标')
        await expect(preview.get_by_role('row').filter(has_text='wrongUnit').last).to_contain_text('单位不一致')
        await passed.locator('..').screenshot(path=str(Path(settings['root'])/'intent-metric-report.png'))
        await browser.close()


if __name__ == '__main__':
    if sys.argv[1:] == ['--report-browser']:
        asyncio.run(report_browser(json.load(sys.stdin)))
        sys.exit()
    from live_llm_job_repeat_acceptance import main
    from s3testsupport import ObjectStore, SERVER
    baseline = '--intent-baseline' in sys.argv
    if baseline:
        sys.argv.remove('--intent-baseline')
    browser_dist = None
    fixture = ''
    if '--intent-browser-dist' in sys.argv:
        position = sys.argv.index('--intent-browser-dist')
        browser_dist = Path(sys.argv[position+1]).resolve(strict=True)
        assert (browser_dist/'index.html').is_file()
        del sys.argv[position:position+2]
        fixture = ('\nfrom fastapi.staticfiles import StaticFiles\n'
                   f"app.mount('/', StaticFiles(directory={str(browser_dist)!r}, html=True))\n")
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=partial(scenario,baseline=baseline,browser_dist=browser_dist),fixture_override=fixture,
                     store_factory=lambda:ObjectStore(executable=binary, volume_max=8, authenticated=True)))
