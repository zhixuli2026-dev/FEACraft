"""FF-F06B: one genuine DeepSeek call through the normal Edge chat surface."""

import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit

from app.services.process_identity_native import probe
from live_llm_acceptance import FIXTURE, source
from live_llm_agenthost_order_acceptance import OBSERVE, compare, digest, jsonl, ordered
from testsupport import IsolatedServer


CASE = 'F06B actual reasoning disclosure'
PROMPT = '请心算 (27×14-18)÷6。最后只写无标点的一行，格式为 FF06B_RESULT_数字，不使用工具。'
MARKER = 'FF06B_RESULT_60'
HOLD = '''
probe_sparse = True

async def await_browser_observation():
    deadline = time.monotonic() + 30
    while not (root / 'release-observed-reasoning').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Edge did not inspect the first genuine reasoning fragment')
        await asyncio.sleep(.05)
'''


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings['root'])
    base, conversation = settings['base'], settings['conversation']
    route = f'/api/v1/conversations/{conversation}'
    result = {'passed': False}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        context = edge.new_context(viewport={'width': 1460, 'height': 940}, locale='zh-CN',
                                   permissions=['clipboard-read', 'clipboard-write'])
        context.add_init_script('if(location.origin===' + json.dumps(base) + ')localStorage.setItem("vf_token",'
                                + json.dumps(settings['token']) + ');')
        page = context.new_page()
        page.set_default_timeout(70000)
        expect.set_options(timeout=65000)
        result['edge'] = edge.version
        choose = page.get_by_test_id('agent-conversation')
        composer = page.get_by_test_id('agent-input')
        cards = page.get_by_test_id('reasoning-card')
        active = page.locator('[data-testid="reasoning-card"][data-streaming="true"]')
        completed = page.get_by_test_id('chat-message').filter(has=page.get_by_test_id('reasoning-card'))
        history = page.get_by_test_id('chat-message-list')

        def select_conversation():
            case = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
            expect(case).to_be_visible()
            if case.get_attribute('aria-pressed') != 'true':
                case.click()
            if not choose.is_visible():
                page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
            choose.select_option(str(conversation))
            expect(composer).to_be_enabled()

        def disclosure(card, *, opened, running):
            suffix = ' · 进行中' if running else ''
            button = card.get_by_role('button', name='推理过程（非最终回答）' + suffix, exact=True)
            content = card.get_by_test_id('reasoning-content')
            expect(button).to_have_attribute('aria-expanded', str(opened).lower())
            target = button.get_attribute('aria-controls')
            assert target
            expect(content).to_have_attribute('id', target)
            if opened:
                expect(content).to_be_visible()
            else:
                expect(content).to_be_hidden()
            return button, content

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

        try:
            page.goto(base, wait_until='domcontentloaded')
            select_conversation()
            expect(cards).to_have_count(0)
            options = page.locator('details.agent-settings')
            summary = options.locator('summary')
            if options.get_attribute('open') is None:
                summary.click()
            options.get_by_test_id('reasoning-effort').select_option('low')
            summary.click()
            wait_for_workbench()
            composer.fill(PROMPT)
            with page.expect_response(lambda response: response.request.method == 'POST'
                                      and urlsplit(response.url).path == route + '/messages', timeout=70000) as observed:
                composer.press('Enter')
            response = observed.value
            if response.status != 200:
                raise AssertionError(f'Actual message request returned HTTP {response.status}: {response.text()[:300]}')
            assert 'text/event-stream' in response.headers['content-type']
            expect(active).to_have_count(1)
            live_toggle, partial = disclosure(active, opened=True, running=True)
            partial_text = partial.text_content()
            assert partial_text
            expect(history.get_by_test_id('chat-markdown')).to_have_count(0)
            page.screenshot(path=str(root / 'reasoning-active.png'))
            live_toggle.focus()
            live_toggle.press('Space')
            disclosure(active, opened=False, running=True)
            live_toggle.press('Enter')
            disclosure(active, opened=True, running=True)
            result['active'] = {'content_sha256': digest(partial_text), 'content_characters': len(partial_text),
                                'expanded_by_default': True, 'keyboard_toggle': True, 'final_answer_visible': False}
            (root / 'release-observed-reasoning').touch()

            frames = [json.loads(line[5:]) for line in response.text().splitlines() if line.startswith('data:')]
            run = ordered(frames)
            assert sum(frame['type'] == 'run.completed' for frame in frames) == 1
            assert not any(frame['type'] in {'stream.error', 'request.failed', 'request.timed_out'} for frame in frames)
            reasoning = ''.join(frame['text'] for frame in frames if frame['type'] == 'reasoning.delta')
            answer = ''.join(frame['text'] for frame in frames if frame['type'] == 'text.delta')
            assert reasoning.startswith(partial_text) and MARKER in answer, (bool(reasoning), bool(answer))
            write_json(root / 'reasoning-wire.json',
                       [{'type': frame['type'], 'seq': frame['seq'], 'run_id': frame['run_id'],
                         **({'text_sha256': digest(frame['text'])} if isinstance(frame.get('text'), str) else {})}
                        for frame in frames])

            def check_completed(screenshot):
                expect(active).to_have_count(0)
                expect(completed).to_have_count(1)
                expect(history.get_by_test_id('chat-markdown')).to_have_count(1)
                expect(history.get_by_test_id('chat-markdown')).to_contain_text(MARKER)
                sealed = completed.get_by_test_id('reasoning-card')
                toggle, _ = disclosure(sealed, opened=False, running=False)
                page.bring_to_front()
                copy = completed.get_by_test_id('chat-copy')
                copy.click()
                expect(copy).to_have_text('已复制')
                assert page.evaluate('navigator.clipboard.readText()') == answer
                toggle.focus()
                toggle.press('Enter')
                _, opened = disclosure(sealed, opened=True, running=False)
                assert opened.text_content() == reasoning
                page.screenshot(path=str(root / screenshot))
                toggle.press('Space')
                disclosure(sealed, opened=False, running=False)

            check_completed('reasoning-completed-open.png')
            page.reload(wait_until='domcontentloaded')
            select_conversation()
            check_completed('reasoning-after-reload.png')
            receipts = root / 'upstream-lanes.jsonl'
            deadline = time.monotonic() + 8
            while not receipts.is_file() and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            write_json(root / 'reasoning-content-digests.json', compare(jsonl(receipts)[0], frames))
            result.update(passed=True, run_id=run, frames=len(frames), final_answer_count=1,
                          final_collapsed_by_default=True, copy_is_exact_answer=True,
                          final_reasoning_sha256=digest(reasoning), final_answer_sha256=digest(answer),
                          same_after_reload=True, accessible_control_relationship=True)
        finally:
            (root / 'release-observed-reasoning').touch()
            if not result['passed']:
                page.screenshot(path=str(root / 'reasoning-failure.png'))
            write_json(root / 'reasoning-browser.json', result)
            edge.close()


def verify(args, configuration):
    endpoint = urlsplit(configuration['base_url'])
    assert endpoint.scheme == 'https' and endpoint.hostname == 'api.deepseek.com'
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    python = args.browser_python.resolve(strict=True)
    api = IsolatedServer('live-f06b-reasoning-card', app='reasoning_fixture:app')
    assert FIXTURE.count("if calls>8:raise RuntimeError('Live acceptance exceeded its eight-request limit')") == 1
    assert OBSERVE.count('await asyncio.sleep(.22)') == 1
    fixture = FIXTURE.replace("if calls>8:raise RuntimeError('Live acceptance exceeded its eight-request limit')",
                              "if calls>1:raise RuntimeError('FF-F06B acceptance allows one actual request')")
    observer = OBSERVE.replace('await asyncio.sleep(.22)', 'await await_browser_observation()')
    mount = '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n'
    (api.root / 'reasoning_fixture.py').write_text(fixture + HOLD + observer + mount, encoding='utf8')
    result = {'passed': False, 'provider': 'deepseek', 'host': endpoint.hostname, 'model': configuration['model'],
              'mock_llm_calls': 0, 'maximum_actual_requests': 1}
    print('LIVE_F06B_EVIDENCE', api.root, flush=True)

    def save():
        result['upstream'] = jsonl(api.root / 'upstream.jsonl')
        result['model_calls'] = sum(row['kind'] == 'http' for row in result['upstream'])
        assert configuration['key'] not in json.dumps(result)
        write_json(api.root / 'live-result.json', result)

    try:
        with api:
            case = api.create_case(CASE)
            profile = api.call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'F06B authorized DeepSeek',
                               'base_url': configuration['base_url'], 'default_model': configuration['model'],
                               'api_key': configuration['key']})
            conversation = api.call('POST', '/conversations', {'case_id': case['id'], 'title': '真实推理过程',
                                    'provider_profile_id': profile['id'], 'model': configuration['model'],
                                    'permission_mode': 'ask'})['id']
            sandbox = api.root / 'edge-tmp'
            sandbox.mkdir()
            subprocess.run([str(python), str(Path(__file__).resolve()), '--browser'],
                           input=json.dumps({'root': str(api.root), 'base': api.base, 'token': api.token,
                                             'conversation': conversation}),
                           cwd=api.root, env=dict(os.environ, PYTHONIOENCODING='utf8', TEMP=str(sandbox), TMP=str(sandbox)),
                           encoding='utf8', timeout=360, check=True)
            result['browser'] = json.loads((api.root / 'reasoning-browser.json').read_text(encoding='utf8'))
            assert result['browser']['passed']
            result['exact_upstream_lanes'] = json.loads((api.root / 'reasoning-content-digests.json').read_text(encoding='utf8'))
            run = result['browser']['run_id']
            route = f'/conversations/{conversation}'
            detail = api.call('GET', route)
            messages = [event['payload'] for event in detail['events'] if event['type'] == 'message'
                        and event['payload'].get('role') == 'assistant' and event['payload'].get('run_id') == run]
            assert len(messages) == 1 and digest(messages[0]['reasoning']) == result['browser']['final_reasoning_sha256']
            assert digest(messages[0]['content']) == result['browser']['final_answer_sha256']
            assert api.call('GET', f'/fixture/stream-owner/{conversation}/{run}') == {
                'phase': 'idle', 'registered': False, 'sequence_tracked': False}
            other = api.call('POST', '/auth/register', {'name': 'Independent test user',
                             'email': 'f06b-unrelated@example.com', 'password': 'isolated-test-123'})
            api.call('GET', route, expect=404, extra_headers={'Authorization': 'Bearer ' + other['token']})
            result['history'] = {'one_assistant_message': True, 'text_and_reasoning_match': True,
                                 'cross_account_status': 404}

        identity = json.loads((api.root / 'api-birth.json').read_text(encoding='utf8'))
        deadline = time.monotonic() + 15
        while probe(identity)['state'] != 'exited' and time.monotonic() < deadline:
            time.sleep(.2)
        assert probe(identity)['state'] == 'exited'
        with socket.socket() as connection:
            connection.settimeout(1)
            assert connection.connect_ex(('127.0.0.1', api.port)) != 0
        assert not (api.root / '.vault_key').exists()
        result['cleanup'] = {'api_pid': identity['pid'], 'api_exited': True, 'port_closed': True,
                             'vault_key_on_disk': False}
        save()
        assert result['model_calls'] == 1 and all(row['status'] == 200 for row in result['upstream'] if row['kind'] == 'http')
        result['passed'] = True
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'detail': str(error).replace(configuration['key'], '[REDACTED]')[:400]}
        raise
    finally:
        save()
    print('LIVE_F06B_PASS', api.root / 'live-result.json', flush=True)


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
