"""Actual DeepSeek selects Crossref search and a public HTTPS document fetch."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import time

import httpx

from live_llm_acceptance import source
from live_llm_shared_index_acceptance import SharedIndexServer, configure


DOI = '10.1063/1.168744'
TITLE = 'A tensorial approach to computational continuum mechanics using object-oriented techniques'
URL = 'https://api.crossref.org/works/' + DOI


def verify(config, public_dns_ip, *, cancel_only=False):
    api = SharedIndexServer('live-f20-f21-web-tools', app='live_fixture:app')
    configure(api, {'VIBEFLOW_WEB_SEARCH_PROVIDER': 'crossref'}, False)
    path = api.root / 'live_fixture.py'
    fixture = '\nlive_request_limit=4\n'
    if public_dns_ip:
        assert ipaddress.IPv4Address(public_dns_ip).is_global
        # The host-specific DNS workaround uses a real DNS address and leaves
        # the actual resolver's check, direct connection and TLS intact.
        fixture = f'''import socket
crossref_addr = {public_dns_ip!r}
original_lookup = socket.getaddrinfo
def observed_lookup(host, *args, **kwargs):
    if host == 'api.crossref.org':
        host = crossref_addr
    return original_lookup(host, *args, **kwargs)
socket.getaddrinfo = observed_lookup
''' + path.read_text(encoding='utf8') + fixture
    else:
        fixture = path.read_text(encoding='utf8') + fixture
    if cancel_only:
        fixture += '''
import asyncio
from app.services.websearch import CrossrefSearchProvider
actual_search = CrossrefSearchProvider.search
async def hold_result(self, query, max_results):
    records = await actual_search(self, query, max_results)
    assert records and records[0].get('url')
    (root/'crossref-received').touch()
    try:
        await asyncio.Future()
    finally:
        (root/'search-task-closed').touch()
CrossrefSearchProvider.search = hold_result
'''
    path.write_text(fixture, encoding='utf8')
    result = {'passed': False, 'model': config['model'], 'mock_llm_calls': 0, 'public_dns_ip': public_dns_ip}
    print('LIVE_WEB_TOOLS_EVIDENCE', api.root, flush=True)

    def turn(client, path, prompt, profile):
        with client.stream('POST', path + '/messages', json={'content': prompt,
                           'provider_profile_id': profile['id']}) as response:
            response.raise_for_status()
            return [json.loads(line[5:]) for line in response.iter_lines() if line.startswith('data:')]

    try:
        with api:
            case = api.create_case('Actual Crossref and direct web fetch')
            profile = api.call('POST', '/provider-profiles', {'name': 'Live web tools', 'provider': 'deepseek',
                               'base_url': config['base_url'], 'default_model': config['model'], 'api_key': config['key']})
            cid = api.call('POST', '/conversations', {'case_id': case['id'], 'title': 'Public scholarly record',
                           'provider_profile_id': profile['id'], 'model': config['model'], 'permission_mode': 'plan'})['id']
            route = f'/conversations/{cid}'
            prompt = (f'请调用一次 web_search 搜索精确论文标题 {TITLE}，max_results=1。'
                      f'另外调用一次 web_fetch 读取已知 Crossref 公开元数据 URL {URL}。'
                      '只使用这两个只读工具。收到结果后回复工具实际找到的 DOI，不要再调用工具。')
            client_options = dict(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                                  timeout=110, trust_env=False)
            if cancel_only:
                with httpx.Client(**client_options) as client, ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(turn, client, route,
                        f'仅调用一次 web_search 查找 {TITLE}，max_results=1。等待真实搜索结果。', profile)
                    deadline = time.monotonic() + 30
                    try:
                        while not (api.root / 'crossref-received').exists():
                            assert time.monotonic() < deadline and not pending.done(), 'Actual search did not return'
                            time.sleep(.1)
                    finally:
                        cancel = api.call('POST', route + '/messages:cancel', {})
                    assert cancel['ok']
                    frames = pending.result(timeout=25)
                assert (api.root / 'search-task-closed').exists()
                assert any(row['type']=='tool.result' and row['tool']=='web_search' and row['status']=='cancelled'
                           for row in frames)
                assert any(row['type']=='run.cancelled' for row in frames)
                result.update(provider_completed_before_delivery=True, api_cancelled=True, provider_task_closed=True)
            else:
                with httpx.Client(**client_options) as client:
                    verify_public_turns(client, route, prompt, profile, api, turn, result)
                history = api.call('GET', route)['events']
                assert [row['payload']['tool'] for row in history if row['type']=='tool_result'].count('web_fetch')==2
                outsider = api.call('POST', '/auth/register', {'email': 'other-web-tool@example.com',
                                    'password': 'isolated-test-123', 'name': 'Foreign web reader'})
                api.call('GET', route, expect=404, extra_headers={'Authorization': 'Bearer ' + outsider['token']})
                result['history_persisted_and_private'] = True
        receipts = [json.loads(line) for line in (api.root / 'upstream.jsonl').read_text(encoding='utf8').splitlines()]
        assert len([row for row in receipts if row['kind']=='http' and row['status']==200])==(1 if cancel_only else 4)
        result['passed'] = True
    finally:
        trace = api.root / 'upstream.jsonl'
        if trace.is_file():
            result['upstream'] = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
        encoded = json.dumps(result, ensure_ascii=False, indent=2)
        assert config['key'] not in encoded
        (api.root / 'live-result.json').write_text(encoded, encoding='utf8')


def verify_public_turns(client, route, prompt, profile, api, turn, result):
    frames = turn(client, route, prompt, profile)
    (api.root / 'actual-public-tool-sse.json').write_text(json.dumps(frames, ensure_ascii=False, indent=2), encoding='utf8')
    tools = {row['tool']: row for row in frames if row['type']=='tool.result'}
    assert set(tools)=={'web_search','web_fetch'} and all(tool['ok'] for tool in tools.values()), tools
    assert TITLE.lower() in tools['web_search']['result'].lower() and DOI in tools['web_search']['result']
    assert URL in tools['web_fetch']['result'] and 'HTTP 200' in tools['web_fetch']['result']
    assert any(row['type']=='run.completed' for row in frames)
    assert DOI in ''.join(row['text'] for row in frames if row['type']=='text.delta')
    assert tools['web_search']['run_id'] == next(row['run_id'] for row in frames if row['type']=='run.completed')
    blocked = turn(client, route, f'请调用一次 web_fetch 尝试 {api.base}/api/health。'
                   '这是隔离测试环境的回环地址。报告工具的实际拒绝结果，不要重试。', profile)
    denied = next(row for row in blocked if row['type']=='tool.result')
    assert denied['tool']=='web_fetch' and not denied['ok'] and '非公网' in denied['result']
    assert any(row['type']=='run.completed' for row in blocked)
    result.update(crossref_search=True, fetch_http_status=200, denied_loopback=True,
                  returned_doi=DOI, distinct_turns=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', required=True, type=int)
    parser.add_argument('--crossref-ip', help='Actual Linux/Windows public DNS A record verified for this run')
    parser.add_argument('--cancel-only', action='store_true')
    args = parser.parse_args()
    verify(asyncio.run(source(args.source_profile_id)), args.crossref_ip, cancel_only=args.cancel_only)
