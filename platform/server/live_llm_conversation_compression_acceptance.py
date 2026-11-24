"""FF-F37A/B: real DeepSeek compression, repeat compression, and Edge handoff."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import time
from traceback import extract_tb
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer


CASE = 'Actual conversation compression'
OBSERVE = '''
import asyncio

recorded_send = httpx.AsyncClient.send
async def observe_summary_request(self, request, *args, **kwargs):
    if request.url.host != 'api.deepseek.com' or not request.url.path.endswith('/chat/completions'):
        return await recorded_send(self, request, *args, **kwargs)
    payload = json.loads(request.content)
    response = await recorded_send(self, request, *args, **kwargs)
    if payload.get('stream') is False:
        input_text = payload['messages'][-1]['content']
        probes = json.loads((root / 'probe-input.json').read_text(encoding='utf8'))
        receipt({'kind':'compression_input', 'length':len(input_text),
                 'old_full_message':probes['old_prompt'] in input_text,
                 'prior_summary_included':'已有摘要：' in input_text,
                 'contains_first_marker':probes['markers'][0] in input_text,
                 'contains_second_marker':probes['markers'][1] in input_text})
        arm = root / 'pause-summary.arm'
        if arm.is_file():
            arm.unlink()
            (root / 'pause-summary.ready').touch()
            deadline = time.monotonic() + 45
            while not (root / 'pause-summary.release').exists():
                if time.monotonic() > deadline:
                    raise TimeoutError('Waiting for a real competing conversation write')
                await asyncio.sleep(.05)
    return response
httpx.AsyncClient.send = observe_summary_request
'''


def events(api, cid):
    return api.call('GET', f'/conversations/{cid}')['events']


def summaries(rows):
    return [row for row in rows if row['type'] == 'system' and row['payload'].get('kind') == 'summary']


def turn(client, cid, body, model):
    with client.stream('POST', f'/conversations/{cid}/messages', json={'content': body, **model}) as response:
        response.raise_for_status()
        frames = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith('data:')]
    kinds = [frame['type'] for frame in frames]
    assert kinds.count('run.completed') == 1, kinds
    return ''.join(frame['text'] for frame in frames if frame['type'] == 'text.delta')


def response_for(page, path):
    return page.expect_response(lambda response: response.request.method == 'POST'
                                and urlsplit(response.url).path == '/api/v1' + path, timeout=110_000)


def select_conversation(page, cid):
    case = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
    expect(case).to_be_visible()
    if case.get_attribute('aria-pressed') != 'true':
        case.click()
    selector = page.get_by_test_id('agent-conversation')
    if not selector.is_visible():
        page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
    selector.select_option(str(cid))
    expect(page.get_by_test_id('agent-input')).to_be_enabled()
    return selector


def browser_actions(api, cid, fallback_id, notes, markers, report):
    route = f'/conversations/{cid}'
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(api.root / 'edge-test-profile',
            channel='msedge', viewport={'width': 1450, 'height': 980})
        context.add_init_script('if(location.origin===' + json.dumps(api.base) + ')localStorage.setItem("vf_token",'
                                + json.dumps(api.token) + ');')
        page = context.new_page()
        page.set_default_timeout(50_000)
        expect.set_options(timeout=50_000)
        page.goto(api.base, wait_until='domcontentloaded')
        selector = select_conversation(page, cid)
        settings = page.locator('details.agent-settings')
        if settings.get_attribute('open') is None:
            settings.locator('summary').click()

        initial = events(api, cid)
        compress = page.get_by_role('button', name='压缩', exact=True)
        (api.root / 'pause-summary.arm').touch()
        with response_for(page, route + '/compress') as observed:
            compress.click()
            try:
                expect(page.get_by_role('button', name='压缩中…')).to_be_disabled()
                expect(page.get_by_test_id('agent-stop')).to_be_disabled()
                select_conversation(page, fallback_id)
            finally:
                (api.root / 'pause-summary.release').touch()
        assert observed.value.status == 200, observed.value.status
        first = observed.value.json()
        assert first['ok'] and markers[0] in first['summary'] and markers[1] not in first['summary']
        first_row, = summaries(events(api, cid))
        first_messages = [item for item in initial if item['type'] == 'message']
        assert first_row['payload']['covers_max_id'] < first_messages[-6]['id']
        history = page.get_by_test_id('chat-message-list')
        expect(compress).to_be_enabled()
        expect(selector).to_have_value(str(fallback_id))
        expect(history.get_by_text(first['summary'], exact=True)).to_have_count(0)
        select_conversation(page, cid)
        expect(history.get_by_text(first['summary'], exact=True)).to_be_visible()
        intact = events(api, cid)
        assert not api.call('POST', route + '/compress', {'keep_last':6})['ok']
        assert events(api, cid) == intact

        second = api.call('POST', route + '/compress', {'keep_last':2})
        assert second['ok'] and all(marker in second['summary'] for marker in markers)
        durable = events(api, cid)
        assert all(item in durable for item in initial)
        first_row, newest = summaries(durable)
        assert first_row['payload']['covers_max_id'] < newest['payload']['covers_max_id'] < first_messages[-2]['id']
        assert newest['payload']['text'] == second['summary']
        page.reload(wait_until='domcontentloaded')
        selector = select_conversation(page, cid)
        expect(history.get_by_text(second['summary'], exact=True)).to_be_visible()
        report['compression'] = {'summary_ids':[first_row['id'], newest['id']], 'recent_message_ids': [row['id'] for row in first_messages[-2:]],
                                 'append_only':True,'same_boundary_skips_provider':True,'edge_reload':True,
                                 'late_result_stays_in_original_conversation':True}

        if settings.get_attribute('open') is None:
            settings.locator('summary').click()
        with response_for(page, route + '/handoff') as observed:
            page.get_by_test_id('handoff').click()
        assert observed.value.status == 200, observed.value.status
        target = observed.value.json()
        target_id = target['id']
        expect(selector).to_have_value(str(target_id))
        original = api.call('GET', route)
        for key in ('case_id','provider_profile_id','model','permission_mode'):
            assert target[key] == original[key]
        only, = events(api, target_id)
        assert only['payload']['kind'] == 'handoff' and only['payload']['text'] == second['summary']
        assert only['payload']['source_conversation_id'] == cid and only['payload']['covers_max_id'] == newest['id']
        expect(history.get_by_text(second['summary'], exact=True)).to_be_visible()
        assert all(note not in history.inner_text() for note in notes)
        check = '请从已交接的摘要中原样输出此前约定的两个核对编号。只回复编号，不调用工具。'
        composer = page.get_by_test_id('agent-input')
        composer.fill(check)
        with response_for(page, f'/conversations/{target_id}/messages') as observed:
            composer.press('Enter')
        assert observed.value.status == 200
        frames = [json.loads(line[5:]) for line in observed.value.text().splitlines() if line.startswith('data:')]
        assert any(frame['type'] == 'run.completed' for frame in frames)
        answer = ''.join(frame['text'] for frame in frames if frame['type'] == 'text.delta')
        assert all(marker in answer for marker in markers)
        page.reload(wait_until='domcontentloaded')
        select_conversation(page, target_id)
        expect(history.get_by_text(second['summary'], exact=True)).to_be_visible()
        expect(history.get_by_test_id('chat-markdown')).to_contain_text(markers[0])
        report['handoff'] = {'target_id':target_id, 'inherited_configuration':True,'only_summary_event':True,
                             'live_model_read_back_both_markers':True,'edge_reload':True}
        context.close()


def race(api, client, cid, model):
    endpoint = f'/conversations/{cid}/compress'
    original = summaries(events(api, cid))
    for suffix in ('ready','release'):
        (api.root / f'pause-summary.{suffix}').unlink(missing_ok=True)
    (api.root / 'pause-summary.arm').touch()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, endpoint, json={'keep_last':2})
        try:
            deadline = time.monotonic() + 100
            while not (api.root / 'pause-summary.ready').exists():
                if pending.done():
                    raise AssertionError(f'No pause after live compression: {pending.result().status_code}')
                if time.monotonic() >= deadline:
                    raise TimeoutError('No live compression response')
                time.sleep(.05)
            turn(client, cid, '确认一条新备注。只回复：好。不使用工具。', model)
        finally:
            (api.root / 'pause-summary.release').touch()
        result = pending.result(timeout=65)
    assert result.status_code == 409 and result.json()['detail']['code'] == 'history_changed'
    assert summaries(events(api, cid)) == original
    retry = api.call('POST', endpoint, {'keep_last':2})
    assert retry['ok'] and len(summaries(events(api, cid))) == len(original) + 1
    return {'late_summary_rejected':True, 'retry_committed':True}


def verify(args, config):
    api = IsolatedServer('live-f37-compression', app='compression_fixture:app')
    dist = args.dist.resolve(strict=True)
    mount = '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n'
    (api.root / 'compression_fixture.py').write_text(FIXTURE + '\nlive_request_limit = 12\n' + OBSERVE + mount, encoding='utf8')
    report = {'passed':False, 'provider':'DeepSeek', 'model':config['model'], 'mock_llm_calls':0}
    print('LIVE_F37_EVIDENCE', api.root, flush=True)
    try:
        with api:
            case = api.create_case(CASE)
            profile = api.call('POST', '/provider-profiles', {'provider':'deepseek','name':CASE,
                'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})
            model = {'provider_profile_id':profile['id'], 'model':config['model']}
            cid = api.call('POST', '/conversations', {'case_id':case['id'],'title':CASE,'permission_mode':'ask',**model})['id']
            route = f'/conversations/{cid}'
            markers = ['CHECK_' + uuid4().hex[:8].upper() for _ in range(2)]
            old_note = f'记录第一个算例核对编号 {markers[0]}，后续摘要保留原样。' + '这是重复背景说明，不需要保留。' * 90
            notes = [old_note, f'记录第二个算例核对编号 {markers[1]}，后续摘要保留原样。',
                     '补记入口温度使用 300 K。', '出口压力取 0 Pa。']
            old_prompt = notes[0] + ' 请只回答：好。不使用工具。'
            (api.root / 'probe-input.json').write_text(json.dumps({'old_prompt':old_prompt,'markers':markers},ensure_ascii=False),encoding='utf8')
            with httpx.Client(base_url=api.base+'/api/v1', trust_env=False, timeout=125,
                              headers={'Authorization':'Bearer '+api.token}) as client:
                for note in notes:
                    assert turn(client, cid, note + ' 请只回答：好。不使用工具。', model).strip()
                source_rows = events(api, cid)
                fallback = api.call('POST', route+'/handoff', {'keep_chars':1000})
                fallback_event, = events(api, fallback['id'])
                expected = '\n'.join(f"{row['payload']['role']}: {row['payload']['content']}"
                    for row in source_rows if row['type']=='message')[-1000:]
                assert fallback_event['payload']['text'] == expected and len(expected) == 1000
                assert fallback_event['payload']['kind'] == 'handoff' and fallback_event['payload']['source_conversation_id'] == cid
                report['bounded_text_fallback'] = True

                browser_actions(api, cid, fallback['id'], notes, markers, report)
                turn(client, cid, '新增一条有效备注。只回答：好。不使用工具。', model)
                report['concurrent_write'] = race(api, client, cid, model)

            outsider = api.call('POST', '/auth/register', {'name':'Other test account',
                                'email':'f37-outside@example.com','password':'isolated-test-123'})
            headers = {'Authorization':'Bearer '+outsider['token']}
            for action in ('compress','handoff'):
                api.call('POST', route+'/'+action, {}, expect=404, extra_headers=headers)
            api.call('POST', route+'/compress', {'keep_last':1}, expect=422)
            report['cross_account_status'] = 404

        report['passed'] = True
    except Exception as error:
        frame = extract_tb(error.__traceback__)[-1]
        report['failure'] = {'type':type(error).__name__, 'location':f'{Path(frame.filename).name}:{frame.lineno}',
                             'detail':str(error).replace(config['key'],'[REDACTED]')[:900]}
    finally:
        (api.root / '.vault_key').unlink(missing_ok=True)
        log = api.root / 'upstream.jsonl'
        report['upstream'] = [json.loads(line) for line in log.read_text(encoding='utf8').splitlines()] if log.is_file() else []
        http = [item for item in report['upstream'] if item['kind']=='http']
        probes = [item for item in report['upstream'] if item['kind']=='compression_input']
        report['successful_model_calls'] = sum(item['status']==200 for item in http)
        report['transport_failures'] = sum(item['kind']=='transport_error' for item in report['upstream'])
        report['passed'] = report['passed'] and len(http) == report['successful_model_calls'] == 11 and len(probes) == 4
        if report['passed']:
            first, second = probes[:2]
            report['passed'] = (first['old_full_message'] and not first['prior_summary_included'] and first['contains_first_marker']
                and not second['old_full_message'] and second['prior_summary_included']
                and second['contains_first_marker'] and second['contains_second_marker']
                and all(item['length'] <= 12000 for item in probes))
        path = api.root / 'live-result.json'
        text = json.dumps(report,ensure_ascii=False,indent=2)
        assert config['key'] not in text
        path.write_text(text+'\n',encoding='utf8')
    print('LIVE_F37_RESULT', path, 'passed='+str(report['passed']), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--dist',type=Path,required=True)
    args = parser.parse_args()
    raise SystemExit(verify(args, asyncio.run(source(args.source_profile_id))))
