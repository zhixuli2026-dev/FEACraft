"""FF-F03. Verify actual DeepSeek Markdown in the normal Edge chat workbench."""

import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit

from app.services.process_identity_native import probe
from live_llm_acceptance import FIXTURE, source
from live_llm_agenthost_order_acceptance import OBSERVE, compare, digest, jsonl, ordered
from live_llm_reasoning_card_acceptance import write_json
from testsupport import IsolatedServer


CASE = 'F03 actual Markdown answer'
HEADING = 'F03 工程回执'
TAIL = 'F03 结束'
REFERENCE = 'https://example.org/f03'
MARKDOWN = r"""# F03 工程回执

**压力**与*流量*。行内代码 `$x+1$` 保持原样。行内公式 $x_1^2$ 和 \(\alpha+1\)。

```text
literal $a+b$ and \(c+d\)
```

| 参数 | 数值 |
| --- | --- |
| 压力 | 20 Pa |

[参考文档](https://example.org/f03) [惰性 URI](vf-fixture-only:F03)

<strong>允许的静态标记</strong> <vf-fixture-mark>保留的标签内容</vf-fixture-mark>

$$\frac{3}{5}$$

\[\sqrt{9}\]

无效公式 $\frac{3}$。**F03 结束**。
"""
PROMPT = ('请直接逐字输出下列普通 Markdown 技术示例。不要输出说明、首尾分隔线或整份代码围栏。'
          '保留原有代码围栏、链接、惰性标记和所有反斜杠。不要使用工具。\n\n' + MARKDOWN)
HISTORY_FIXTURE = '''
import json, os
from pathlib import Path
import httpx
from app.main import app
from app.services.process_identity_native import observe
root=Path(__file__).parent
(root/'api-birth.json').write_text(json.dumps(observe(os.getpid())),encoding='utf8')
original_send=httpx.AsyncClient.send
async def local_only(self, request, *args, **kwargs):
    if request.url.host not in {'localhost', '127.0.0.1', '::1'}:
        with (root/'external-request-attempts.jsonl').open('a',encoding='utf8') as log:
            log.write(json.dumps({'host':request.url.host,'method':request.method})+'\\n')
        raise RuntimeError('History-only operation forbids external requests')
    return await original_send(self,request,*args,**kwargs)
httpx.AsyncClient.send=local_only
'''


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings['root'])
    base, conversation = settings['base'], settings['conversation']
    endpoint = f'/api/v1/conversations/{conversation}/messages'
    result = {'passed': False}
    page_errors = []
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        context = edge.new_context(viewport={'width': 1520, 'height': 1020}, locale='zh-CN',
                                   permissions=['clipboard-read', 'clipboard-write'])
        context.add_init_script('if(location.origin===' + json.dumps(base) + ')localStorage.setItem("vf_token",'
                                + json.dumps(settings['token']) + ');')
        if settings.get('history_only'):
            context.add_init_script("""if (navigator.clipboard) {
                const nativeWrite = navigator.clipboard.writeText.bind(navigator.clipboard);
                navigator.clipboard.writeText = async (plain) => {
                    const result = await nativeWrite(plain);
                    window.f03CopiedArgument = plain;
                    return result;
                };
            }""")
        page = context.new_page()
        page.set_default_timeout(65000)
        expect.set_options(timeout=65000)
        page.on('pageerror', lambda error: page_errors.append(str(error)))
        result['edge'] = edge.version
        choose = page.get_by_test_id('agent-conversation')
        composer = page.get_by_test_id('agent-input')
        answer = page.get_by_test_id('chat-markdown').filter(has=page.get_by_role('heading', name=HEADING, exact=True))
        message = page.get_by_test_id('chat-message').filter(has=answer)

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
            deadline = time.monotonic() + 50
            sqlite_url = (root / 'test.sqlite').as_uri() + '?mode=ro'
            while time.monotonic() < deadline:
                page.wait_for_timeout(200)
                with closing(sqlite3.connect(sqlite_url, uri=True)) as db:
                    active_probes = db.execute("SELECT count(*) FROM native_requests WHERE phase='active' "
                        "AND kind IN ('runtime.python_probe','runtime.wsl_probe')").fetchone()[0]
                if active_probes == 0:
                    return
            raise AssertionError('Workstation capability probes did not finish')

        def check_markdown(raw, label):
            expect(answer).to_have_count(1)
            expect(answer).to_have_attribute('data-authoritative', 'true')
            expect(answer.locator('p > code')).to_have_text('$x+1$')
            fence = answer.locator('pre code.language-text')
            expect(fence).to_contain_text(r'literal $a+b$ and \(c+d\)')
            expect(fence.locator('.katex')).to_have_count(0)
            expect(answer.locator('strong').filter(has_text='压力')).to_have_count(1)
            expect(answer.locator('em').filter(has_text='流量')).to_have_count(1)
            table = answer.get_by_role('table')
            expect(table.get_by_role('columnheader', name='参数')).to_have_count(1)
            expect(table.get_by_role('cell', name='20 Pa', exact=True)).to_have_count(1)
            expect(answer.get_by_role('link', name='参考文档')).to_have_attribute('href', REFERENCE)
            blocked = answer.locator('a').filter(has_text='惰性 URI')
            expect(blocked).to_have_count(1)
            assert blocked.get_attribute('href') is None
            expect(answer.locator('strong').filter(has_text='允许的静态标记')).to_have_count(1)
            expect(answer.locator('vf-fixture-mark')).to_have_count(0)
            expect(answer.get_by_text('保留的标签内容', exact=False)).to_be_visible()
            expect(answer.locator('.katex')).to_have_count(4)
            expect(answer.locator('.math-block .katex-display')).to_have_count(2)
            formulae = answer.locator('.katex annotation').all_text_contents()
            assert formulae == ['x_1^2', r'\alpha+1', r'\frac{3}{5}', r'\sqrt{9}'], formulae
            expect(answer.locator('.katex-error')).to_contain_text(r'\frac{3}')
            expect(answer.locator('strong').filter(has_text=TAIL)).to_be_visible()
            page.bring_to_front()
            copy = message.get_by_test_id('chat-copy')
            page.evaluate("navigator.clipboard.writeText('F03 isolated clipboard observation')")
            copy.click()
            expect(copy).to_have_text('已复制')
            observed_copy = page.evaluate('navigator.clipboard.readText()')
            result[f'{label}_clipboard_observation'] = {
                'expected_chars': len(raw), 'actual_chars': len(observed_copy),
                'windows_crlf_count': observed_copy.count('\r\n'),
                'actual_sha256': digest(observed_copy),
            }
            if settings.get('history_only'):
                assert page.evaluate('window.f03CopiedArgument') == raw
            assert observed_copy.replace('\r\n', '\n') == raw.replace('\r\n', '\n')
            answer.get_by_role('heading', name=HEADING).scroll_into_view_if_needed()
            page.screenshot(path=str(root / f'markdown-{label}.png'))
            return {'formulae': formulae, 'body_equals_clipboard': True,
                    'clipboard_raw_byte_equal': observed_copy == raw, 'inert_markup_sanitized': True}

        try:
            page.goto(base, wait_until='domcontentloaded')
            select_conversation()
            if settings.get('history_only'):
                raw = (root / 'retained-model-answer.md').read_bytes().decode('utf8')
                result['first'] = check_markdown(raw, 'reopened')
                page.reload(wait_until='domcontentloaded')
                select_conversation()
                result['after_reload'] = check_markdown(raw, 'after-reload')
                assert not page_errors, page_errors
                result.update(passed=True, raw_sha256=digest(raw), source='persisted_real_model_history')
                return
            wait_for_workbench()
            composer.fill(PROMPT)
            with page.expect_response(lambda response: response.request.method == 'POST'
                                      and urlsplit(response.url).path == endpoint, timeout=70000) as observed:
                composer.press('Enter')
            response = observed.value
            if response.status != 200:
                raise AssertionError(f'Actual message returned HTTP {response.status}: {response.text()[:350]}')
            assert 'text/event-stream' in response.headers['content-type']
            frames = [json.loads(line[5:]) for line in response.text().splitlines() if line.startswith('data:')]
            run = ordered(frames)
            assert sum(frame['type'] == 'run.completed' for frame in frames) == 1
            raw = ''.join(frame['text'] for frame in frames if frame['type'] == 'text.delta')
            (root / 'real-answer.md').write_bytes(raw.encode('utf8'))
            write_json(root / 'markdown-wire.json', [
                {'type': frame['type'], 'seq': frame['seq'], 'run_id': frame['run_id'],
                 **({'text_sha256': digest(frame['text'])} if isinstance(frame.get('text'), str) else {})}
                for frame in frames])
            assert all(marker in raw for marker in ('vf-fixture-only:F03', 'vf-fixture-mark', HEADING, TAIL))
            result['first'] = check_markdown(raw, 'completed')
            page.reload(wait_until='domcontentloaded')
            select_conversation()
            result['after_reload'] = check_markdown(raw, 'after-reload')
            lanes = root / 'upstream-lanes.jsonl'
            deadline = time.monotonic() + 8
            while not lanes.is_file() and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            result['upstream_lanes'] = compare(jsonl(lanes)[0], frames)
            assert not page_errors, page_errors
            result.update(passed=True, run_id=run, frames=len(frames), raw_sha256=digest(raw))
        finally:
            if not result['passed']:
                page.screenshot(path=str(root / 'markdown-failure.png'))
            write_json(root / 'markdown-browser.json', result)
            edge.close()


def wait_for_api_exit(root, port):
    identity = json.loads((root / 'api-birth.json').read_text(encoding='utf8'))
    deadline = time.monotonic() + 15
    while probe(identity)['state'] != 'exited' and time.monotonic() < deadline:
        time.sleep(.2)
    assert probe(identity)['state'] == 'exited'
    with socket.socket() as connection:
        connection.settimeout(1)
        assert connection.connect_ex(('127.0.0.1', port)) != 0
    assert not (root / '.vault_key').exists()
    return {'api_pid': identity['pid'], 'api_exited': True, 'port_closed': True,
            'vault_key_on_disk': False}


def verify(args, configuration):
    endpoint = urlsplit(configuration['base_url'])
    assert endpoint.scheme == 'https' and endpoint.hostname == 'api.deepseek.com'
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    python = args.browser_python.resolve(strict=True)
    api = IsolatedServer('live-f03-chat-markdown', app='chat_markdown_fixture:app')
    guard = "if calls>8:raise RuntimeError('Live acceptance exceeded its eight-request limit')"
    assert FIXTURE.count(guard) == 1
    fixture = FIXTURE.replace(guard, "if calls>1:raise RuntimeError('FF-F03 acceptance allows one actual request')")
    mount = '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/",StaticFiles(directory=' + repr(str(dist)) + ',html=True))\n'
    (api.root / 'chat_markdown_fixture.py').write_text(fixture + '\nprobe_sparse=False\n' + OBSERVE + mount, encoding='utf8')
    result = {'passed': False, 'provider': 'deepseek', 'host': endpoint.hostname, 'model': configuration['model'],
              'maximum_actual_requests': 1, 'mock_llm_calls': 0}
    print('LIVE_F03_EVIDENCE', api.root, flush=True)

    try:
        with api:
            case = api.create_case(CASE)
            profile = api.call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'F03 authorized DeepSeek',
                               'base_url': configuration['base_url'], 'default_model': configuration['model'],
                               'api_key': configuration['key']})
            cid = api.call('POST', '/conversations', {'case_id': case['id'], 'title': 'Markdown 工程回复',
                                    'provider_profile_id': profile['id'], 'model': configuration['model'],
                                    'permission_mode': 'ask'})['id']
            sandbox = api.root / 'edge-tmp'
            sandbox.mkdir()
            subprocess.run([str(python), str(Path(__file__).resolve()), '--browser'],
                           input=json.dumps({'root': str(api.root), 'base': api.base, 'token': api.token,
                                             'conversation': cid}),
                           cwd=api.root, env=dict(os.environ, PYTHONIOENCODING='utf8', TEMP=str(sandbox), TMP=str(sandbox)),
                           encoding='utf8', timeout=420, check=True)
            result['browser'] = json.loads((api.root / 'markdown-browser.json').read_text(encoding='utf8'))
            run = result['browser']['run_id']
            route = f'/conversations/{cid}'
            detail = api.call('GET', route)
            messages = [event['payload'] for event in detail['events'] if event['type'] == 'message'
                        and event['payload'].get('role') == 'assistant' and event['payload'].get('run_id') == run]
            assert len(messages) == 1 and digest(messages[0]['content']) == result['browser']['raw_sha256']
            assert api.call('GET', f'/fixture/stream-owner/{cid}/{run}') == {
                'phase': 'idle', 'registered': False, 'sequence_tracked': False}
            other = api.call('POST', '/auth/register', {'name': 'Isolated Markdown visitor',
                             'email': 'f03-unrelated@example.com', 'password': 'isolated-test-123'})
            api.call('GET', route, expect=404, extra_headers={'Authorization': 'Bearer ' + other['token']})
            result['history'] = {'raw_equal': True, 'cross_account_status': 404}

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
    print('LIVE_F03_PASS', api.root / 'live-result.json', flush=True)


def verify_history(args):
    original = args.history_from.resolve(strict=True)
    raw = (original / 'real-answer.md').read_bytes().decode('utf8')
    official = jsonl(original / 'upstream-lanes.jsonl')
    receipt = json.loads((original / 'live-result.json').read_text(encoding='utf8'))
    assert receipt['model_calls'] == 1 and receipt['host'] == 'api.deepseek.com'
    assert len(official) == 1 and official[0]['done']
    body_lanes = [row for row in official[0]['groups'] if row['type'] == 'text.delta']
    assert len(body_lanes) == 1 and body_lanes[0]['sha256'] == digest(raw)
    old_pid = json.loads((original / 'api-birth.json').read_text(encoding='utf8'))
    assert probe(old_pid)['state'] == 'exited'
    dist = args.dist.resolve(strict=True)
    python = args.browser_python.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    api = IsolatedServer('f03-retained-history', app='chat_history_fixture:app')
    (api.root / 'retained-model-answer.md').write_bytes(raw.encode('utf8'))
    shutil.copytree(original / 'files', api.root / 'files', dirs_exist_ok=True)
    old_db = (original / 'test.sqlite').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(old_db, uri=True)) as db, closing(sqlite3.connect(api.root / 'test.sqlite')) as clone:
        rows = [(cid, json.loads(payload)) for cid, payload in db.execute(
            "SELECT conversation_id,payload FROM agent_events WHERE type='message'")]
        cid, message = next((cid, item) for cid, item in rows if item.get('role') == 'assistant')
        assert message['content'] == raw
        owner_email, = db.execute('SELECT email FROM users ORDER BY id LIMIT 1').fetchone()
        db.backup(clone)
    mount = '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/",StaticFiles(directory=' + repr(str(dist)) + ',html=True))\n'
    (api.root / 'chat_history_fixture.py').write_text(HISTORY_FIXTURE + mount, encoding='utf8')
    print('F03_RETAINED_EVIDENCE', api.root, flush=True)
    result = {'passed': False, 'model_calls': 0, 'original_model_receipt': str(original / 'live-result.json'),
              'source_raw_sha256': digest(raw)}
    try:
        with api:
            account = api.call('POST', '/auth/login', {'email': owner_email, 'password': 'isolated-test-123'})
            api.token = account['token']
            route = f'/conversations/{cid}'
            detail = api.call('GET', route)
            saved = [row['payload']['content'] for row in detail['events'] if row['type'] == 'message'
                     and row['payload'].get('role') == 'assistant']
            assert saved == [raw]
            sandbox = api.root / 'edge-tmp'
            sandbox.mkdir()
            subprocess.run([str(python), str(Path(__file__).resolve()), '--browser'],
                           input=json.dumps({'root': str(api.root), 'base': api.base, 'token': api.token,
                                             'conversation': cid, 'history_only': True}),
                           cwd=api.root, env=dict(os.environ, PYTHONIOENCODING='utf8', TEMP=str(sandbox), TMP=str(sandbox)),
                           encoding='utf8', timeout=330, check=True)
            result['browser'] = json.loads((api.root / 'markdown-browser.json').read_text(encoding='utf8'))
            assert result['browser']['passed'] and result['browser']['raw_sha256'] == digest(raw)
            other = api.call('POST', '/auth/register', {'name': 'Isolated history visitor',
                             'email': 'f03-unrelated@example.com', 'password': 'isolated-test-123'})
            api.call('GET', route, expect=404, extra_headers={'Authorization': 'Bearer ' + other['token']})
            result['cross_account_status'] = 404
            assert not (api.root / 'external-request-attempts.jsonl').exists()
        result['cleanup'] = wait_for_api_exit(api.root, api.port)
        result['passed'] = True
    finally:
        write_json(api.root / 'retained-history-result.json', result)
    print('F03_RETAINED_HISTORY_PASS', api.root / 'retained-history-result.json', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        operation = parser.add_mutually_exclusive_group(required=True)
        operation.add_argument('--source-profile-id', type=int)
        operation.add_argument('--history-from', type=Path)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        args = parser.parse_args()
        if args.history_from:
            verify_history(args)
        else:
            verify(args, asyncio.run(source(args.source_profile_id)))
