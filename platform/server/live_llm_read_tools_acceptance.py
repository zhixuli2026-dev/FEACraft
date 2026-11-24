"""Real model file/read/list/search acceptance against isolated FS or real S3."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import uuid
from contextlib import nullcontext

import httpx

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer, SERVER


def browser_check(api, case, scenarios):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge')
        page = browser.new_page(viewport={'width': 1536, 'height': 1000}, locale='zh-CN')
        page.add_init_script('localStorage.setItem("vf_token", ' + json.dumps(api.token) + ');')
        try:
            page.goto(api.base, wait_until='domcontentloaded')
            page.locator('[data-case-name='+json.dumps(case['name'])+']').click(timeout=30000)
            page.locator('.dv-tab', has_text='Agent').first.click(timeout=30000)
            selector = page.get_by_test_id('agent-conversation')
            checks = {}
            for mode, scenario in scenarios.items():
                selector.select_option(str(scenario['conversation_id']), timeout=30000)
                proof = scenario.get('proof', scenario['answer'])
                card = page.get_by_test_id('chat-tool-card').filter(has_text=proof).first
                reply = page.get_by_test_id('chat-message').filter(has_text=scenario['answer']).last
                expect(card).to_have_attribute('data-status', 'completed', timeout=15000)
                expect(reply).to_be_visible(timeout=15000)
                screenshot = mode + '-history.png'
                page.screenshot(path=api.root / screenshot)
                checks[mode] = {'tool_card': 'completed', 'persisted_reply': True, 'screenshot': screenshot}
            return checks
        finally:
            browser.close()


def verify(configuration, modes, store=None, database_url=None, dist=None):
    api = IsolatedServer('live-deepseek-read-tools', app='live_fixture:app', database_url=database_url)
    bootstrap = 'import os\nos.environ.update(' + repr(store.settings()) + ')\n' if store else ''
    fixture = bootstrap + FIXTURE
    if dist is not None:
        fixture += '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n'
    (api.root / 'live_fixture.py').write_text(fixture, encoding='utf8')
    result = dict(passed=False, simulated_llm=False, storage='s3' if store else 'fs',
                  database='postgresql' if database_url else 'sqlite',
                  authenticated_object_storage=bool(store and store.authenticated),
                  model=configuration['model'], scenarios={})

    def save():
        trace = api.root / 'upstream.jsonl'
        result['upstream'] = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        (api.root / 'live-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')

    print('LIVE_READ_EVIDENCE ' + str(api.root), flush=True)
    try:
        with api:
            case = api.create_case('Real model read tools')
            profile = api.call('POST', '/provider-profiles', {
                'provider': 'deepseek', 'name': 'Real read tools', 'base_url': configuration['base_url'],
                'default_model': configuration['model'], 'api_key': configuration['key']})
            directory = 'Ref/live-proof'
            filename = 'proof-' + uuid.uuid4().hex + '.txt'
            path = directory + '/' + filename
            nonce = 'PROOF_' + uuid.uuid4().hex
            content = 'Read from the actual storage backend.\npressure-proof ' + nonce + '\n'
            api.put(path, content)
            api.put(directory + '/build/noise.txt', 'pressure-proof EXCLUDED_BUILD_NOISE')
            if store:
                assert not list((api.root / 'files').rglob(filename)), 'S3 test unexpectedly has a local file mirror'
                result['no_local_file_mirror'] = True
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                              trust_env=False, timeout=180) as client:
                for mode in modes:
                    tool, expected, instruction = {
                        'read': ('read_text_file', nonce, f'调用 read_text_file 读取 {path}，然后原样回复文件中 PROOF_ 开头的完整字符串。'),
                        'list': ('list_files', filename, f'调用 list_files 列出 {directory}，然后原样回复其中 proof- 开头的文件名。'),
                        'search': ('search_text', nonce, f'调用 search_text，在 {directory} 搜索 pressure-proof，然后原样回复匹配内容中 PROOF_ 开头的完整字符串。'),
                    }[mode]
                    conversation = api.call('POST', '/conversations', {
                        'case_id': case['id'], 'title': 'Live ' + mode, 'provider_profile_id': profile['id'],
                        'model': configuration['model'], 'permission_mode': 'plan'})
                    cid = conversation['id']
                    events = []
                    try:
                        with client.stream('POST', f'/conversations/{cid}/messages', json={
                            'content': instruction + '仅调用指定工具一次，不调用其他工具，不修改文件，不猜测；拿到结果后直接回答。',
                            'provider_profile_id': profile['id'], 'model': configuration['model']}) as response:
                            response.raise_for_status()
                            for line in response.iter_lines():
                                if line.startswith('data:'):
                                    events.append(json.loads(line[5:]))
                                if len(json.dumps(events)) > 2 * 1024 * 1024:
                                    raise ValueError('Live evidence exceeds 2 MiB')
                    finally:
                        assert configuration['key'] not in json.dumps(events)
                        (api.root / (mode + '-events.json')).write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding='utf8')
                        save()
                    failures = [e for e in events if e.get('type') in {'stream.error', 'request.failed', 'request.timed_out'}]
                    assert not failures, failures
                    tools = [e for e in events if e.get('type') == 'tool.result']
                    assert len(tools) == 1 and tools[0]['tool'] == tool and tools[0]['ok'], tools
                    assert expected in tools[0]['result'], tools[0]
                    assert 'EXCLUDED_BUILD_NOISE' not in tools[0]['result'] and 'build/noise.txt' not in tools[0]['result']
                    answer = ''.join(e.get('text', '') for e in events if e.get('type') == 'text.delta')
                    assert expected in answer, answer
                    assert any(e.get('type') == 'run.completed' for e in events)
                    saved = api.call('GET', f'/conversations/{cid}')
                    assert any(e['type'] == 'tool_result' and expected in str(e['payload']) for e in saved['events'])
                    read = client.get(api.prefix + '/file', params={'path': path})
                    read.raise_for_status()
                    assert read.json()['content'] == content
                    result['scenarios'][mode] = dict(passed=True, conversation_id=cid, tool=tool,
                        tool_result=tools[0], answer=answer, proof=expected, persisted=True, source_unchanged=True)
                    save()
                    print('LIVE_READ_PASS ' + mode, flush=True)
            if dist is not None:
                result['browser'] = browser_check(api, case, result['scenarios'])
            result['passed'] = True
            save()
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'detail': str(error).replace(configuration['key'], '[REDACTED]')[:1400]}
        save()
        print('LIVE_READ_FAILED ' + json.dumps(result['failure'], ensure_ascii=False), flush=True)
        return 1
    print('LIVE_READ_OK ' + str(api.root / 'live-result.json'), flush=True)
    return 0


async def run(args, modes):
    from live_llm_terminal_agent_acceptance import disposable_database

    configuration = await source(args.source_profile_id)
    store = nullcontext(None)
    if args.storage == 's3':
        from s3testsupport import ObjectStore
        store = ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed', authenticated=True)
    async with disposable_database(args.postgres) as database_url:
        with store as active:
            return await asyncio.to_thread(verify, configuration, modes, active, database_url, args.dist)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--storage', choices=('fs', 's3'), default='s3')
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--dist', type=lambda path: Path(path).resolve())
    parser.add_argument('--modes', default='read,list,search')
    args = parser.parse_args()
    modes = args.modes.split(',')
    if not modes or any(m not in {'read', 'list', 'search'} for m in modes) or len(set(modes)) != len(modes):
        parser.error('Choose unique read/list/search modes')
    if args.dist is not None and not (args.dist / 'index.html').is_file():
        parser.error('Choose an existing frontend build with index.html')
    sys.exit(asyncio.run(run(args, modes)))
