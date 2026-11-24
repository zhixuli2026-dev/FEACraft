"""FF-F07. Real DeepSeek tool selection and live Edge tool-card transitions."""

import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
import uuid

from live_llm_acceptance import FIXTURE, source
from live_llm_agenthost_order_acceptance import OBSERVE, jsonl, ordered
from live_llm_chat_markdown_acceptance import wait_for_api_exit
from live_llm_reasoning_card_acceptance import write_json
from testsupport import IsolatedServer


CASE = 'F07 actual tool status'
DIRECTORY = 'Ref/f07-cards'
MISSING = DIRECTORY + '/missing.txt'
PROMPT = ('在同一次工具调用批次立即并行调用两个只读工具：'
          'list_files(subdir="Ref/f07-cards") 与 read_text_file(path="Ref/f07-cards/missing.txt")。'
          '即使第二个路径可能缺失也要实际读取。不要运行其他工具。'
          '收到两条工具结果后，直接给出目录中 .txt 的实际文件名并说明缺失结果。')

GATES = '''
from contextlib import asynccontextmanager
from app.services.tools import ToolExecutor

capacity = agent.file_tool_capacity
execute_case_tool = ToolExecutor.execute_case_tool

async def observe_tool_boundary(status):
    (root / ('reached-' + status)).touch()
    deadline = time.monotonic() + 27
    while not (root / ('release-' + status)).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Edge did not observe the actual tool status')
        await asyncio.sleep(.04)

@asynccontextmanager
async def observed_capacity(db, case, name):
    if name == 'read_text_file':
        await observe_tool_boundary('queued')
    async with capacity(db, case, name):
        yield

async def observed_execute(self, db, case, name, args, **kwargs):
    if name == 'read_text_file' and args.get('path') == 'Ref/f07-cards/missing.txt':
        await observe_tool_boundary('running')
    return await execute_case_tool(self, db, case, name, args, **kwargs)

agent.file_tool_capacity = observed_capacity
ToolExecutor.execute_case_tool = observed_execute
'''


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings['root'])
    base, conversation = settings['base'], settings['conversation']
    endpoint = f'/api/v1/conversations/{conversation}/messages'
    result = {'passed': False}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        context = edge.new_context(viewport={'width': 1480, 'height': 1020}, locale='zh-CN')
        context.add_init_script('if(location.origin===' + json.dumps(base) + ')localStorage.setItem("vf_token",'
                                + json.dumps(settings['token']) + ');')
        page = context.new_page()
        page.set_default_timeout(65000)
        expect.set_options(timeout=62000)
        result['edge'] = edge.version
        choose = page.get_by_test_id('agent-conversation')
        composer = page.get_by_test_id('agent-input')
        file_card = page.locator('[data-testid="chat-tool-card"][data-kind="file"]')
        folder_card = page.locator('[data-testid="chat-tool-card"][data-kind="folder"]')

        def select_conversation():
            case = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
            expect(case).to_be_visible()
            if case.get_attribute('aria-pressed') != 'true':
                case.click()
            if not choose.is_visible():
                page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
            choose.select_option(str(conversation))
            expect(composer).to_be_enabled()

        def wait_for_workbench():
            deadline = time.monotonic() + 45
            path = (root / 'test.sqlite').as_uri() + '?mode=ro'
            while time.monotonic() < deadline:
                page.wait_for_timeout(200)
                with closing(sqlite3.connect(path, uri=True)) as db:
                    active_probes = db.execute("SELECT count(*) FROM native_requests WHERE phase='active' "
                        "AND kind IN ('runtime.python_probe','runtime.wsl_probe')").fetchone()[0]
                if active_probes == 0:
                    return
            raise AssertionError('Workstation capability probes did not finish')

        def check_file(status, title):
            expect(file_card).to_have_count(1, timeout=20000)
            expect(file_card).to_have_attribute('data-status', status, timeout=20000)
            expect(file_card).to_contain_text('📄 读取文件 · ' + title)
            assert '已读取文件' not in file_card.inner_text()

        def check_results():
            check_file('error', '失败')
            expect(file_card.locator('pre')).to_contain_text('文件不存在: ' + MISSING)
            expect(folder_card).to_have_count(1)
            expect(folder_card).to_have_attribute('data-status', 'completed')
            expect(folder_card).to_contain_text('📁 已查看目录 · 已完成')
            expect(folder_card.locator('pre')).to_contain_text(settings['filename'])

        try:
            page.goto(base, wait_until='domcontentloaded')
            select_conversation()
            wait_for_workbench()
            composer.fill(PROMPT)
            with page.expect_response(lambda response: response.request.method == 'POST'
                                      and urlsplit(response.url).path == endpoint, timeout=75000) as observed:
                composer.press('Enter')
            response = observed.value
            if response.status != 200:
                raise AssertionError(f'Actual Agent request returned HTTP {response.status}: {response.text()[:300]}')
            assert 'text/event-stream' in response.headers['content-type']
            check_file('queued', '等待运行')
            file_card.evaluate('(node) => { window.f07LiveFileCard = node; }')
            page.screenshot(path=str(root / 'tool-queued.png'))
            (root / 'release-queued').touch()
            check_file('running', '正在运行')
            assert file_card.evaluate('(node) => node === window.f07LiveFileCard')
            page.screenshot(path=str(root / 'tool-running.png'))
            (root / 'release-running').touch()

            frames = [json.loads(line[5:]) for line in response.text().splitlines() if line.startswith('data:')]
            run = ordered(frames)
            write_json(root / 'tool-wire.json', frames)
            assert sum(frame['type'] == 'run.completed' for frame in frames) == 1
            real_results = [frame for frame in frames if frame['type'] == 'tool.result']
            assert {frame['tool'] for frame in real_results} == {'read_text_file', 'list_files'} and len(real_results) == 2
            failure = next(frame for frame in real_results if frame['tool'] == 'read_text_file')
            success = next(frame for frame in real_results if frame['tool'] == 'list_files')
            assert failure['args']['path'] == MISSING and failure['status'] == 'error' and failure['ok'] is False
            assert failure['result'].startswith('FileNotFoundError: 文件不存在: ' + MISSING)
            assert success['status'] == 'completed' and success['ok'] is True and settings['filename'] in success['result']
            for tool in real_results:
                states = [frame['status'] for frame in frames if frame['type'] == 'tool.state'
                          and frame['tool_call_id'] == tool['tool_call_id']]
                assert states == ['queued', 'running']
            answer = ''.join(frame['text'] for frame in frames if frame['type'] == 'text.delta')
            assert settings['filename'] in answer
            check_results()
            assert file_card.evaluate('(node) => node === window.f07LiveFileCard')
            page.screenshot(path=str(root / 'tool-results.png'))
            page.reload(wait_until='domcontentloaded')
            select_conversation()
            check_results()
            page.screenshot(path=str(root / 'tool-history.png'))
            result.update(passed=True, run_id=run, frames=len(frames), actual_results=real_results,
                          stable_card_across_states=True, recovered_both_cards=True)
        finally:
            for stage in ('queued', 'running'):
                (root / ('release-' + stage)).touch()
            if not result['passed']:
                page.screenshot(path=str(root / 'tool-failure.png'))
            write_json(root / 'tool-browser.json', result)
            edge.close()


def verify(args, configuration):
    endpoint = urlsplit(configuration['base_url'])
    assert endpoint.scheme == 'https' and endpoint.hostname == 'api.deepseek.com'
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    python = args.browser_python.resolve(strict=True)
    api = IsolatedServer('live-f07-tool-cards', app='tool_card_fixture:app')
    guard = "if calls>8:raise RuntimeError('Live acceptance exceeded its eight-request limit')"
    assert FIXTURE.count(guard) == 1
    fixture = FIXTURE.replace(guard, "if calls>2:raise RuntimeError('FF-F07 acceptance allows two actual requests')")
    mount = '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/",StaticFiles(directory=' + repr(str(dist)) + ',html=True))\n'
    (api.root / 'tool_card_fixture.py').write_text(fixture + '\nprobe_sparse=False\n' + OBSERVE + GATES + mount,
                                                 encoding='utf8')
    result = {'passed': False, 'provider': 'deepseek', 'host': endpoint.hostname, 'model': configuration['model'],
              'maximum_actual_requests': 2, 'mock_llm_calls': 0}
    print('LIVE_F07_EVIDENCE', api.root, flush=True)

    try:
        with api:
            case = api.create_case(CASE)
            filename = 'actual-' + uuid.uuid4().hex[:12] + '.txt'
            known = DIRECTORY + '/' + filename
            content = 'Real local text fixture for reading and listing.\n'
            api.put(known, content)
            absent = api.prefix + '/file?' + urlencode({'path': MISSING})
            api.call('GET', absent, expect=404)
            profile = api.call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'F07 authorized DeepSeek',
                               'base_url': configuration['base_url'], 'default_model': configuration['model'],
                               'api_key': configuration['key']})
            cid = api.call('POST', '/conversations', {'case_id': case['id'], 'title': '真实工具卡状态',
                                    'provider_profile_id': profile['id'], 'model': configuration['model'],
                                    'permission_mode': 'plan'})['id']
            sandbox = api.root / 'edge-tmp'
            sandbox.mkdir()
            subprocess.run([str(python), str(Path(__file__).resolve()), '--browser'],
                           input=json.dumps({'root': str(api.root), 'base': api.base, 'token': api.token,
                                             'conversation': cid, 'filename': filename}),
                           cwd=api.root, env=dict(os.environ, PYTHONIOENCODING='utf8', TEMP=str(sandbox), TMP=str(sandbox)),
                           encoding='utf8', timeout=410, check=True)
            result['browser'] = json.loads((api.root / 'tool-browser.json').read_text(encoding='utf8'))
            assert result['browser']['passed']
            assert all((api.root / ('reached-' + stage)).is_file() for stage in ('queued', 'running'))
            route = f'/conversations/{cid}'
            events = api.call('GET', route)['events']
            histories = [event['payload'] for event in events if event['type'] in {'tool_state','tool_result'}]
            for tool in result['browser']['actual_results']:
                observed = [item for item in histories if item['tool_call_id'] == tool['tool_call_id']]
                assert [item['status'] for item in observed] == ['queued', 'running', tool['status']]
                assert observed[-1]['ok'] == tool['ok'] and observed[-1]['result'] == tool['result']
            api.call('GET', absent, expect=404)
            assert api.call('GET', api.prefix + '/file?' + urlencode({'path': known}))['content'] == content
            assert api.call('GET', f"/fixture/stream-owner/{cid}/{result['browser']['run_id']}") == {
                'phase': 'idle', 'registered': False, 'sequence_tracked': False}
            visitor = api.call('POST', '/auth/register', {'name': 'Other tool-card viewer',
                               'email': 'f07-unrelated@example.com', 'password': 'isolated-test-123'})
            api.call('GET', route, expect=404, extra_headers={'Authorization': 'Bearer ' + visitor['token']})
            result['history'] = {'states_match_wire': True, 'actual_paths_unchanged': True, 'cross_account': 404}
        result['cleanup'] = wait_for_api_exit(api.root, api.port)
        calls = [row for row in jsonl(api.root / 'upstream.jsonl') if row['kind'] == 'http']
        assert len(calls) == 2 and all(row['status'] == 200 for row in calls)
        result['passed'] = True
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'detail': str(error).replace(configuration['key'], '[REDACTED]')[:400]}
        raise
    finally:
        result['upstream'] = jsonl(api.root / 'upstream.jsonl')
        result['model_calls'] = sum(row['kind'] == 'http' for row in result['upstream'])
        assert configuration['key'] not in json.dumps(result)
        write_json(api.root / 'live-result.json', result)
    print('LIVE_F07_PASS', api.root / 'live-result.json', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source-profile-id', type=int, required=True)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        args = parser.parse_args()
        verify(args, asyncio.run(source(args.source_profile_id)))
