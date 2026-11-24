"""FF-F04: real, progressively delivered DeepSeek Markdown through the Edge workbench."""

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
from urllib.parse import urlsplit

from live_llm_acceptance import FIXTURE, source
from live_llm_agenthost_order_acceptance import OBSERVE, compare, digest, jsonl, ordered
from live_llm_chat_markdown_acceptance import wait_for_api_exit
from live_llm_reasoning_card_acceptance import write_json
from testsupport import IsolatedServer


CASE = 'F04 actual streaming Markdown'
STABLE = 'F04 流式段落保持'
FIRST = 'F04_CODE_BEGIN'
SECOND = 'F04_CODE_MIDDLE'
TAIL = 'F04 最后一段'
MARKDOWN = """# F04 流式章节

**F04 流式段落保持**

````text
F04_CODE_BEGIN
```text
$literal$

F04_CODE_MIDDLE
```
~~~
F04_CODE_END
````

**F04 最后一段**
"""
PROMPT = ('请原样逐字输出以下普通 Markdown 作为完整回答，不加说明，也不要把整份输出再放进围栏。'
          '外层四反引号、内层三反引号、美元符号、三个波浪符和空行均必须保持。不使用工具。\n\n' + MARKDOWN)

STAGES = '''
lane_lines = httpx.Response.aiter_lines

async def observe_markdown_stages(self, *args, **kwargs):
    if self.request.url.host != 'api.deepseek.com':
        async for line in lane_lines(self, *args, **kwargs):
            yield line
        return
    marker_index = 0
    markers = ('F04_CODE_BEGIN', 'F04_CODE_MIDDLE')
    body = ''
    async for line in lane_lines(self, *args, **kwargs):
        reaches_stage = False
        if line.startswith('data:') and line[5:].strip() != '[DONE]':
            data = json.loads(line[5:])
            choice = (data.get('choices') or [{}])[0]
            body += (choice.get('delta') or {}).get('content') or ''
            reaches_stage = marker_index < len(markers) and markers[marker_index] in body
        yield line
        if reaches_stage:
            stage = markers[marker_index]
            marker_index += 1
            with (root/'official-stage-prefixes.jsonl').open('a',encoding='utf8') as log:
                log.write(json.dumps({'marker':stage,'character_count':len(body),
                    'sha256':hashlib.sha256(body.encode('utf8')).hexdigest()})+'\\n')
            deadline = time.monotonic() + 34
            while not (root/('release-'+stage)).exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError('Edge did not observe the actual stream stage')
                await asyncio.sleep(.04)

httpx.Response.aiter_lines = observe_markdown_stages
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
        list_view = page.get_by_test_id('chat-message-list')
        active = list_view.locator('[data-testid="chat-markdown"][data-authoritative="false"]')
        completed = list_view.locator('[data-testid="chat-markdown"][data-authoritative="true"]')

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
            end = time.monotonic() + 45
            path = (root / 'test.sqlite').as_uri() + '?mode=ro'
            while time.monotonic() < end:
                page.wait_for_timeout(200)
                with closing(sqlite3.connect(path, uri=True)) as db:
                    active_probes = db.execute("SELECT count(*) FROM native_requests WHERE phase='active' "
                        "AND kind IN ('runtime.python_probe','runtime.wsl_probe')").fetchone()[0]
                if active_probes == 0:
                    return
            raise AssertionError('Workstation capability probes did not finish')

        def streamed_code(marker):
            pre = active.locator('pre code.language-text')
            expect(pre).to_have_count(1, timeout=20000)
            expect(pre).to_contain_text(marker, timeout=20000)
            expect(active).to_have_attribute('data-cursor-count', '1')
            expect(list_view.get_by_test_id('chat-cursor')).to_have_count(1)
            expect(completed).to_have_count(0)
            return pre

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
                raise AssertionError(f'Actual message request returned HTTP {response.status}: {response.text()[:320]}')
            assert 'text/event-stream' in response.headers['content-type']
            stable_block = active.get_by_test_id('chat-block').filter(has=page.get_by_text(STABLE, exact=True))
            first = streamed_code(FIRST)
            assert SECOND not in first.text_content()
            stable_block.evaluate('(node) => { window.f04StableBlock = node; }')
            active.evaluate('(node) => { window.f04StreamingAnswer = node; }')
            page.screenshot(path=str(root / 'markdown-stream-first.png'))
            (root / ('release-' + FIRST)).touch()

            second = streamed_code(SECOND)
            preserved_code = second.text_content()
            assert FIRST in preserved_code and '$literal$\n\n' + SECOND in preserved_code
            assert stable_block.evaluate('(node) => node === window.f04StableBlock')
            assert active.evaluate('(node) => node === window.f04StreamingAnswer')
            expect(active.locator('.katex')).to_have_count(0)
            page.screenshot(path=str(root / 'markdown-stream-second.png'))
            (root / ('release-' + SECOND)).touch()
            result['streaming'] = {'same_stable_block': True, 'same_answer_node': True, 'cursor_per_stage': [1, 1],
                                   'nested_code_kept_in_one_block': True}

            frames = [json.loads(line[5:]) for line in response.text().splitlines() if line.startswith('data:')]
            run = ordered(frames)
            terminals = [frame for frame in frames if frame['type'] == 'run.completed']
            assert len(terminals) == 1 and terminals[0].get('answer_event_id')
            raw = ''.join(frame['text'] for frame in frames if frame['type'] == 'text.delta')
            (root / 'real-answer.md').write_bytes(raw.encode('utf8'))
            write_json(root / 'markdown-wire.json', [
                {key: frame[key] for key in ('type', 'seq', 'run_id', 'answer_event_id') if key in frame}
                | ({'text_sha256': digest(frame['text'])} if isinstance(frame.get('text'), str) else {})
                for frame in frames])

            def final():
                expect(active).to_have_count(0)
                expect(completed).to_have_count(1)
                expect(completed.get_by_text(TAIL, exact=True)).to_be_visible()
                expect(list_view.get_by_test_id('chat-cursor')).to_have_count(0)
                code = completed.locator('pre code.language-text')
                expect(code).to_have_count(1)
                expected_code = MARKDOWN.split('````text\n', 1)[1].split('\n````', 1)[0]
                assert code.text_content().rstrip('\n') == expected_code
                return completed.inner_html()

            first_html = final()
            assert page.evaluate('!window.f04StableBlock.isConnected && !window.f04StreamingAnswer.isConnected')
            completed.locator('pre').scroll_into_view_if_needed()
            page.screenshot(path=str(root / 'markdown-completed.png'))
            page.reload(wait_until='domcontentloaded')
            select_conversation()
            assert final() == first_html
            stage_prefixes = jsonl(root / 'official-stage-prefixes.jsonl')
            assert [row['marker'] for row in stage_prefixes] == [FIRST, SECOND]
            for row in stage_prefixes:
                prefix = raw[:row['character_count']]
                assert row['marker'] in prefix and digest(prefix) == row['sha256']
            lanes = root / 'upstream-lanes.jsonl'
            deadline = time.monotonic() + 8
            while not lanes.is_file() and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            result['official_lanes'] = compare(jsonl(lanes)[0], frames)
            result.update(passed=True, run_id=run, frames=len(frames), answer_event_id=terminals[0]['answer_event_id'],
                          raw_sha256=digest(raw), final_render_replaced_stream=True, final_cursors=0,
                          final_answer_count=1, html_identical_after_reload=True)
        finally:
            for stage in (FIRST, SECOND):
                (root / ('release-' + stage)).touch()
            if not result['passed']:
                page.screenshot(path=str(root / 'markdown-failure.png'))
            write_json(root / 'markdown-browser.json', result)
            edge.close()


def verify(args, configuration):
    endpoint = urlsplit(configuration['base_url'])
    assert endpoint.scheme == 'https' and endpoint.hostname == 'api.deepseek.com'
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    python = args.browser_python.resolve(strict=True)
    api = IsolatedServer('live-f04-streaming-markdown', app='streaming_markdown_fixture:app')
    guard = "if calls>8:raise RuntimeError('Live acceptance exceeded its eight-request limit')"
    assert FIXTURE.count(guard) == 1
    fixture = FIXTURE.replace(guard, "if calls>1:raise RuntimeError('FF-F04 acceptance allows one actual request')")
    mount = '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/",StaticFiles(directory=' + repr(str(dist)) + ',html=True))\n'
    (api.root / 'streaming_markdown_fixture.py').write_text(fixture + '\nprobe_sparse=False\n' + OBSERVE + STAGES + mount,
                                                         encoding='utf8')
    result = {'passed': False, 'provider': 'deepseek', 'host': endpoint.hostname, 'model': configuration['model'],
              'maximum_actual_requests': 1, 'mock_llm_calls': 0}
    print('LIVE_F04_EVIDENCE', api.root, flush=True)

    try:
        with api:
            case = api.create_case(CASE)
            profile = api.call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'F04 authorized DeepSeek',
                               'base_url': configuration['base_url'], 'default_model': configuration['model'],
                               'api_key': configuration['key']})
            cid = api.call('POST', '/conversations', {'case_id': case['id'], 'title': '真实流式 Markdown',
                                    'provider_profile_id': profile['id'], 'model': configuration['model'],
                                    'permission_mode': 'ask'})['id']
            sandbox = api.root / 'edge-tmp'
            sandbox.mkdir()
            subprocess.run([str(python), str(Path(__file__).resolve()), '--browser'],
                           input=json.dumps({'root': str(api.root), 'base': api.base, 'token': api.token, 'conversation': cid}),
                           cwd=api.root, env=dict(os.environ, PYTHONIOENCODING='utf8', TEMP=str(sandbox), TMP=str(sandbox)),
                           encoding='utf8', timeout=410, check=True)
            result['browser'] = json.loads((api.root / 'markdown-browser.json').read_text(encoding='utf8'))
            assert result['browser']['passed']
            run = result['browser']['run_id']
            route = f'/conversations/{cid}'
            detail = api.call('GET', route)
            messages = [event for event in detail['events'] if event['type'] == 'message'
                        and event['payload'].get('role') == 'assistant' and event['payload'].get('run_id') == run]
            assert len(messages) == 1 and messages[0]['id'] == result['browser']['answer_event_id']
            assert digest(messages[0]['payload']['content']) == result['browser']['raw_sha256']
            assert api.call('GET', f'/fixture/stream-owner/{cid}/{run}') == {
                'phase': 'idle', 'registered': False, 'sequence_tracked': False}
            visitor = api.call('POST', '/auth/register', {'name': 'Independent stream visitor',
                               'email': 'f04-unrelated@example.com', 'password': 'isolated-test-123'})
            api.call('GET', route, expect=404, extra_headers={'Authorization': 'Bearer ' + visitor['token']})
            result['history'] = {'matches_committed_answer_event_id': True, 'source_matches': True, 'other_account': 404}
        result['cleanup'] = wait_for_api_exit(api.root, api.port)
        calls = [row for row in jsonl(api.root / 'upstream.jsonl') if row['kind'] == 'http']
        assert len(calls) == 1 and calls[0]['status'] == 200
        result['passed'] = True
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'detail': str(error).replace(configuration['key'], '[REDACTED]')[:400]}
        raise
    finally:
        result['upstream'] = jsonl(api.root / 'upstream.jsonl')
        result['model_calls'] = sum(row['kind'] == 'http' for row in result['upstream'])
        assert configuration['key'] not in json.dumps(result)
        write_json(api.root / 'live-result.json', result)
    print('LIVE_F04_PASS', api.root / 'live-result.json', flush=True)


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
