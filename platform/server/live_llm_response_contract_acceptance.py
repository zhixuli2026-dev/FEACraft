"""FF-F06A and FF-F18 on both official DeepSeek streaming endpoints."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
from traceback import extract_tb

import httpx
from playwright.sync_api import expect, sync_playwright

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer


CASE = 'Actual response contract'
PATHS = {'chat_completions': '/v1/chat/completions', 'messages': '/anthropic/v1/messages'}
OBSERVE = '''
import hashlib

original_contract_send = httpx.AsyncClient.send
original_contract_lines = httpx.Response.aiter_lines

async def contract_payload(self, request, *args, **kwargs):
    if request.url.host == 'api.deepseek.com' and request.url.path.endswith(('/chat/completions','/messages')):
        path = request.url.path
        body = json.loads(request.content)
        system = body['system'] if path.endswith('/messages') else body['messages'][0]['content']
        receipt({'kind':'response_contract_payload','path':path,'stream':body['stream'],
            'model':body['model'],'has_reasoning_effort':'reasoning_effort' in body,
            'reasoning_effort':body.get('reasoning_effort'),'has_thinking':'thinking' in body,'thinking':body.get('thinking'),
            'contract_present':all(tag in system for tag in ('Status Update','Todo','Next Steps','Parameter name:',
                'Purpose:', 'Explanation:', 'Recommendation:', 'Response language is determined by the latest user message'))})
    return await original_contract_send(self,request,*args,**kwargs)

async def contract_lanes(self,*args,**kwargs):
    path = self.request.url.path
    if self.request.url.host != 'api.deepseek.com' or not path.endswith(('/chat/completions','/messages')):
        async for line in original_contract_lines(self,*args,**kwargs):
            yield line
        return
    text, reasoning = [], []
    def write_lanes():
        receipt({'kind':'response_contract_lanes','path':path,
            'text_sha256':hashlib.sha256(''.join(text).encode()).hexdigest(),
            'reasoning_sha256':hashlib.sha256(''.join(reasoning).encode()).hexdigest(),
            'text_chunks':len(text),'reasoning_chunks':len(reasoning)})
    recorded = False
    async for line in original_contract_lines(self,*args,**kwargs):
        data = line[5:].strip() if line.startswith('data:') else ''
        if data == '[DONE]' and not recorded:
            write_lanes()
            recorded = True
        elif data.startswith('{'):
            row = json.loads(data)
            if path.endswith('/messages'):
                delta=row.get('delta',{})
                body=delta.get('text') if delta.get('type') == 'text_delta' else None
                thoughts=delta.get('thinking') if delta.get('type') == 'thinking_delta' else None
            else:
                delta=(row.get('choices') or [{}])[0].get('delta') or {}
                body,thoughts=delta.get('content'),delta.get('reasoning_content')
            if body:text.append(body)
            if thoughts:reasoning.append(thoughts)
        yield line
    if not recorded:
        write_lanes()

httpx.AsyncClient.send = contract_payload
httpx.Response.aiter_lines = contract_lanes
'''
CASES = {
    'clarification': ('几何、出口和壁面条件已确认。当前只需确认流体种类和入口速度。请分别向我追问这两个缺项，当前不选择任何值。', 'off'),
    'progress': ('Write a progress report for this heat transfer case. The measured inlet is 297 K, '
                 'the coarse mesh is ready, the mesh independence check is pending, and no files were changed. '
                 'Keep it concise and do not run tools.', 'off'),
    'explicit_language': ('请用西班牙语写一句话解释为什么要做网格无关性检查。不运行工具。', 'off'),
    'exact_json_with_reasoning': ('请心算 (27×14−18)÷6。只输出包含数字字段 result 的 JSON 对象，'
                                  '不要输出 Markdown、前言或其他字段。不运行工具。', 'low'),
}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def validate_answer(name, answer):
    plain = answer.replace('**', '').replace('`', '')
    if name == 'clarification':
        assert len(re.findall(r'[\u4e00-\u9fff]', plain)) >= 20, answer
        parts = plain.split('Parameter name:')
        assert len(parts) == 3, answer
        for parameter in parts[1:]:
            indices = [parameter.find(label) for label in ('Purpose:', 'Explanation:', 'Recommendation:')]
            assert all(at >= 0 for at in indices) and indices == sorted(indices), answer
        assert '流体' in plain and ('速度' in plain or '流速' in plain), answer
    elif name == 'progress':
        assert not re.search('[\u4e00-\u9fff]', plain) and '297 K' in plain, answer
        lines = [line.lower().strip() for line in plain.splitlines() if line.strip()]
        sections = [next((i for i, line in enumerate(lines) if re.fullmatch(
            r'(?:#+\s*)?(?:[123]\.\s*)?' + heading + r'\s*:?', line)), -1)
            for heading in ('status update', 'todo', 'next steps')]
        assert all(pos >= 0 for pos in sections) and sections == sorted(sections), answer
        assert re.search(r'\b(mesh|grid)\b', plain, re.I), answer
    elif name == 'explicit_language':
        assert not re.search('[\u4e00-\u9fff]', plain), answer
        assert re.search(r'\b(malla|mallado)\b', plain, re.I) and re.search(r'\b(resultados|solución)\b', plain, re.I), answer
    else:
        assert json.loads(answer) == {'result':60}, answer


def edge_replay(api, conversation_ids):
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(api.root / 'edge-test-profile',
            channel='msedge', viewport={'width':1450,'height':980})
        context.add_init_script('if(location.origin===' + json.dumps(api.base) + ')localStorage.setItem("vf_token",'
                                + json.dumps(api.token) + ');')
        page = context.new_page()
        expect.set_options(timeout=50_000)
        page.set_default_timeout(50_000)
        submissions = []
        page.on('request', lambda request: submissions.append(request.url)
                if request.method == 'POST' and request.url.endswith('/messages') else None)

        def select_conversation(cid):
            case = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
            expect(case).to_be_visible()
            if case.get_attribute('aria-pressed') != 'true':
                case.click()
            selector = page.get_by_test_id('agent-conversation')
            if not selector.is_visible():
                page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
            selector.select_option(str(cid))

        page.goto(api.base, wait_until='domcontentloaded')
        for protocol in PATHS:
            select_conversation(conversation_ids[protocol])
            log = page.get_by_test_id('chat-message-list')
            expect(log.get_by_test_id('chat-markdown')).to_have_count(1)
            expect(log.get_by_test_id('chat-markdown')).not_to_have_text('')
            expect(log.get_by_test_id('reasoning-card')).to_have_count(1)
            expect(log.get_by_test_id('reasoning-content')).to_be_hidden()
            page.reload(wait_until='domcontentloaded')
            select_conversation(conversation_ids[protocol])
            expect(log.get_by_test_id('chat-markdown')).to_have_count(1)
            expect(log.get_by_test_id('chat-markdown')).not_to_have_text('')
            expect(log.get_by_test_id('reasoning-card')).to_have_count(1)
        assert not submissions
        context.close()


def verify(args, config):
    api = IsolatedServer('live-f18-response-contract', app='response_contract_fixture:app')
    dist = args.dist.resolve(strict=True)
    mount = '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n'
    (api.root / 'response_contract_fixture.py').write_text(FIXTURE + '\nlive_request_limit = 8\n' + OBSERVE + mount,
                                                           encoding='utf8')
    report = {'passed':False,'provider':'DeepSeek','model':config['model'],'mock_llm_calls':0,'checks':[]}
    print('LIVE_RESPONSE_CONTRACT_EVIDENCE',api.root,flush=True)
    try:
        with api:
            case = api.create_case(CASE)
            last = {}
            with httpx.Client(base_url=api.base+'/api/v1',trust_env=False,timeout=125,
                              headers={'Authorization':'Bearer '+api.token}) as client:
                for protocol, path in PATHS.items():
                    platform_provider = 'deepseek' if protocol == 'chat_completions' else 'anthropic'
                    base = config['base_url'] if protocol == 'chat_completions' else 'https://api.deepseek.com/anthropic'
                    profile = api.call('POST','/provider-profiles',{'provider':platform_provider,'name':protocol,
                        'base_url':base,'default_model':config['model'],'api_key':config['key']})
                    selected = {'provider_profile_id':profile['id'],'model':config['model']}
                    for scenario,(prompt,effort) in CASES.items():
                        cid = api.call('POST','/conversations',{'case_id':case['id'],'title':protocol+' '+scenario,
                            'permission_mode':'ask', **selected})['id']
                        route = f'/conversations/{cid}'
                        with client.stream('POST',route+'/messages', json={'content':prompt,'reasoning_effort':effort, **selected}) as response:
                            response.raise_for_status()
                            frames = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith('data:')]
                        kinds = [item['type'] for item in frames]
                        assert kinds.count('run.completed') == 1 and 'tool.result' not in kinds, kinds
                        answer = ''.join(item['text'] for item in frames if item['type']=='text.delta')
                        reasoning = ''.join(item['text'] for item in frames if item['type']=='reasoning.delta')
                        assert answer
                        stored, = [item['payload'] for item in api.call('GET',route)['events']
                                   if item['type']=='message' and item['payload'].get('role')=='assistant']
                        assert stored['content']==answer and stored.get('reasoning','')==reasoning
                        row = {'path':path,'scenario':scenario,'effort':effort,'conversation_id':cid,'answer':answer,
                            'text_sha256':digest(answer),'reasoning_sha256':digest(reasoning),'reasoning_characters':len(reasoning),
                            'stream_matches_history':True}
                        report['checks'].append(row)
                        if reasoning:
                            assert kinds.index('reasoning.delta') < kinds.index('text.delta') < kinds.index('run.completed')
                            last[protocol] = cid
                        try:
                            validate_answer(scenario,answer)
                            row['verified'] = True
                        except (AssertionError,ValueError) as error:
                            row['verified'] = False
                            row['assertion'] = str(error).replace(config['key'],'[REDACTED]')[:400]
            assert set(last) == set(PATHS), 'Each actual protocol must return reasoning in at least one scenario'
            edge_replay(api,last)
            report['edge_reload'] = True
            outsider = api.call('POST','/auth/register',{'name':'Independent response-test user',
                'email':'response-contract-other@example.com','password':'isolated-test-123'})
            for cid in last.values():
                api.call('GET',f'/conversations/{cid}',expect=404,
                         extra_headers={'Authorization':'Bearer '+outsider['token']})
            report['cross_account_status'] = 404
        report['passed'] = all(row['verified'] for row in report['checks'])
    except Exception as error:
        frame = extract_tb(error.__traceback__)[-1]
        report['failure'] = {'type':type(error).__name__,'location':f'{Path(frame.filename).name}:{frame.lineno}',
            'detail':str(error).replace(config['key'],'[REDACTED]')[:1300]}
    finally:
        (api.root / '.vault_key').unlink(missing_ok=True)
        upstream = api.root / 'upstream.jsonl'
        report['upstream'] = [json.loads(line) for line in upstream.read_text(encoding='utf8').splitlines()] if upstream.is_file() else []
        calls = [v for v in report['upstream'] if v['kind']=='http']
        payloads = [v for v in report['upstream'] if v['kind']=='response_contract_payload']
        lanes = [v for v in report['upstream'] if v['kind']=='response_contract_lanes']
        report['successful_model_calls'] = sum(call['status']==200 for call in calls)
        wire_verified = report['successful_model_calls'] == len(calls) == len(payloads) == len(lanes) == len(report['checks']) == 8
        if wire_verified:
            for result, payload, raw in zip(report['checks'],payloads,lanes,strict=True):
                base = payload['path']==raw['path']==result['path'] and payload['stream'] and payload['contract_present']
                matches = raw['text_sha256']==result['text_sha256'] and raw['reasoning_sha256']==result['reasoning_sha256']
                if result['effort']=='off':
                    params = not payload['has_reasoning_effort'] and not payload['has_thinking']
                elif payload['path'].endswith('/messages'):
                    params = payload['thinking']=={'type':'enabled','budget_tokens':1024} and not payload['has_reasoning_effort']
                else:
                    params = payload['reasoning_effort']=='low' and not payload['has_thinking']
                result['exact_upstream_lanes'] = matches
                result['reasoning_parameters_match'] = params
                wire_verified = wire_verified and base and matches and params
        report['wire_verified'] = wire_verified
        report['passed'] = report['passed'] and wire_verified
        output = api.root / 'live-result.json'
        encoded = json.dumps(report,ensure_ascii=False,indent=2)
        assert config['key'] not in encoded
        output.write_text(encoded+'\n',encoding='utf8')
    print('LIVE_RESPONSE_CONTRACT_RESULT',output,'passed='+str(report['passed']),flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--dist',type=Path,required=True)
    arguments = parser.parse_args()
    raise SystemExit(verify(arguments, asyncio.run(source(arguments.source_profile_id))))
