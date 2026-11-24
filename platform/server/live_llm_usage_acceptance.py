"""Compare Edge and saved Agent usage with actual DeepSeek stream counters."""
import argparse
import asyncio
import json
from pathlib import Path
from traceback import extract_tb
from urllib.parse import urlsplit

from playwright.sync_api import expect

from live_llm_acceptance import FIXTURE, source
from live_llm_session_settings_acceptance import browser, CASE, select_session
from testsupport import IsolatedServer


OBSERVE = '''
usage_origin_lines = httpx.Response.aiter_lines
async def observe_usage(self, *args, **kwargs):
    observed = {}
    try:
        async for line in usage_origin_lines(self, *args, **kwargs):
            if self.request.url.host == 'api.deepseek.com' and line.startswith('data:'):
                try:
                    data = json.loads(line[5:])
                    data = data.get('message', {}) if data.get('type') == 'message_start' else data
                    if isinstance(data.get('usage'), dict):
                        observed.update(data['usage'])
                except ValueError:
                    pass
            yield line
    finally:
        if observed:
            receipt({'kind':'actual_usage','path':self.request.url.path,'counts':observed})
httpx.Response.aiter_lines = observe_usage
'''


def records(api):
    path = api.root/'upstream.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.is_file() else []


def setup(api, config):
    case = api.create_case(CASE)
    ids = {}
    for protocol, provider, endpoint in (
        ('completions', 'deepseek', config['base_url']),
        ('messages', 'anthropic', 'https://api.deepseek.com/anthropic'),
    ):
        profile = api.call('POST','/provider-profiles',{'provider':provider,'name':f'实测 {protocol}',
                           'base_url':endpoint,'default_model':config['model'],'api_key':config['key']})
        ids[protocol] = api.call('POST','/conversations',{'case_id':case['id'],'model':config['model'],
                                 'provider_profile_id':profile['id'],'permission_mode':'ask','title':protocol})['id']
        if protocol == 'completions':
            empty_profile = profile['id']
    ids['empty'] = api.call('POST','/conversations',{'case_id':case['id'],'title':'无用量会话',
                           'provider_profile_id':empty_profile,'model':config['model']})['id']
    return ids


def normalize_expected(protocol, source_counts):
    if protocol == 'completions':
        input_tokens = source_counts['prompt_tokens']
        output_tokens = source_counts['completion_tokens']
        hits = source_counts['prompt_cache_hit_tokens']
        return input_tokens, output_tokens, hits, source_counts['total_tokens']
    hits = source_counts.get('cache_read_input_tokens', 0)
    input_tokens = source_counts['input_tokens'] + hits + source_counts.get('cache_creation_input_tokens', 0)
    output_tokens = source_counts['output_tokens']
    return input_tokens, output_tokens, hits, input_tokens+output_tokens


def request_from_edge(api, page, protocol, cid):
    panel = select_session(page, cid)
    summary = panel.get_by_test_id('context-usage')
    expect(summary).to_contain_text('文本估算约')
    marker = 'ACTUAL_USAGE_'+protocol.upper()
    draft = page.get_by_test_id('agent-input')
    draft.fill('只在正文原样回复 '+marker+'。')
    with page.expect_response(lambda response: response.request.method=='POST'
        and urlsplit(response.url).path==f'/api/v1/conversations/{cid}/messages', timeout=60_000) as captured:
        draft.press('Enter')
    response = captured.value
    assert response.status == 200
    frames = [json.loads(line[5:]) for line in response.text().splitlines() if line.startswith('data:')]
    assert marker in ''.join(row['text'] for row in frames if row['type']=='text.delta')
    reports = [row['usage'] for row in frames if row['type']=='request.usage']
    assert [row['status'] for row in reports]==['pending','reported'], reports
    saved = api.call('GET',f'/conversations/{cid}')
    assert saved['latest_model_usage']==reports[-1]
    assert [row['payload']['reported_usage'] for row in saved['events'] if row['type']=='request_state'
            and row['payload'].get('reported_usage')]==[reports[-1]['counts']]
    wire = [row for row in records(api) if row['kind']=='actual_usage'][-1]
    assert wire['path'].endswith('/chat/completions' if protocol=='completions' else '/messages')
    expected = normalize_expected(protocol,wire['counts'])
    observed = reports[-1]['counts']
    assert tuple(observed[key] for key in ('input_tokens','output_tokens','cache_read_input_tokens','total_tokens'))==expected
    expect(summary).to_contain_text(f'{expected[-1]:,} tokens')
    assert f'总输入 {expected[0]:,}' in summary.get_attribute('title')
    summary.focus()
    summary.press('Enter')
    expect(panel.get_by_test_id('usage-input_tokens')).to_have_text(f'{expected[0]:,}')
    expect(panel.get_by_test_id('usage-output_tokens')).to_have_text(f'{expected[1]:,}')
    expect(panel).to_contain_text('按字符数估算')
    expect(panel).to_contain_text('实际费用以服务商账单为准')
    return reports[-1]


def verify(args, config):
    api = IsolatedServer('live-f15-usage',app='usage_fixture:app')
    dist = args.dist.resolve(strict=True)
    mount = '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='+repr(str(dist))+', html=True))\n'
    (api.root/'usage_fixture.py').write_text(FIXTURE+'\nlive_request_limit=2\n'+OBSERVE+mount,encoding='utf-8')
    print('LIVE_F15_EVIDENCE',api.root,flush=True)
    result = {'passed':False,'provider':'DeepSeek','mock_llm_calls':0}
    try:
        with api:
            ids = setup(api,config)
            email = api.call('GET','/auth/me')['email']
            with browser(api) as page:
                outcomes = {protocol:request_from_edge(api,page,protocol,ids[protocol])
                            for protocol in ('completions','messages')}
                page.screenshot(path=str(api.root/'usage-desktop.png'))
                panel = page.locator('.agent-context-usage')
                page.set_viewport_size({'width':960,'height':850})
                page.evaluate("document.documentElement.dataset.theme='light'")
                assert panel.evaluate('(node) => node.scrollWidth <= node.clientWidth + 1')
                panel.screenshot(path=str(api.root/'usage-narrow.png'))
                empty = select_session(page,ids['empty'])
                expect(empty.get_by_test_id('context-usage')).to_contain_text('文本估算约')
            outsider = api.call('POST','/auth/register',{'email':'usage-unrelated@example.com',
                'name':'Unrelated usage reader','password':'isolated-test-123'})
            api.call('GET',f'/conversations/{ids["messages"]}',expect=404,
                     extra_headers={'Authorization':'Bearer '+outsider['token']})
        with api:
            api.token = api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
            assert all(api.call('GET',f'/conversations/{ids[key]}')['latest_model_usage']==value
                       for key,value in outcomes.items())
            with browser(api) as page:
                panel = select_session(page,ids['messages'])
                total = outcomes['messages']['counts']['total_tokens']
                expect(panel.get_by_test_id('context-usage')).to_contain_text(f'{total:,} tokens')
                panel = select_session(page,ids['completions'])
                total = outcomes['completions']['counts']['total_tokens']
                expect(panel.get_by_test_id('context-usage')).to_contain_text(f'{total:,} tokens')
        result.update(passed=True,outcomes=outcomes, keyboard=True, no_narrow_overflow=True,
                      conversation_switch_isolated=True, cold_api_restart=True,cross_account_404=True)
    except Exception as error:
        frame = extract_tb(error.__traceback__)[-1]
        result['failure'] = {'type':type(error).__name__,'location':f'{Path(frame.filename).name}:{frame.lineno}',
                             'detail':str(error).replace(config['key'],'[REDACTED]')[:1200]}
    finally:
        (api.root/'.vault_key').unlink(missing_ok=True)
        result['actual_calls'] = [row for row in records(api) if row['kind'] in {'http','transport_error','actual_usage'}]
        result['successful_model_calls'] = sum(row['kind']=='http' and row['status']==200 for row in result['actual_calls'])
        result['passed'] = result['passed'] and result['successful_model_calls']==2
        encoded = json.dumps(result,ensure_ascii=False,indent=2)
        assert config['key'] not in encoded
        (api.root/'f15-result.json').write_text(encoded+'\n',encoding='utf-8')
    print('LIVE_F15_RESULT',api.root/'f15-result.json','passed='+str(result['passed']),flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',required=True,type=int)
    parser.add_argument('--dist',required=True,type=Path)
    args = parser.parse_args()
    raise SystemExit(verify(args,asyncio.run(source(args.source_profile_id))))
