"""Exercise a long, real DeepSeek answer through SSE, Markdown and Edge Clipboard."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from live_llm_acceptance import FIXTURE, source
from live_llm_chat_markdown_acceptance import wait_for_api_exit
from testsupport import IsolatedServer


CASE = 'F05 real long Markdown'
ROW = ('communication configuration documentation infrastructure internationalization maintenance '
       'organization representation specification synchronization validation verification')
CODE = '```text\n' + '\n'.join(f'{index:03d} {ROW}' for index in range(1, 231)) + '\nF05_CODE_END\n```'
PROMPT = '直接逐字输出下列文本代码块，保留所有 230 行、行号和完整围栏。请勿省略、总结或调用工具。\n\n' + CODE


def digest(text):
    return hashlib.sha256(text.encode('utf8')).hexdigest()


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    cid = settings['conversation']
    result = {'passed': False}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        result['edge'] = edge.version
        context = edge.new_context(viewport={'width': 1500, 'height': 950}, locale='zh-CN',
                                   permissions=['clipboard-read', 'clipboard-write'])
        context.add_init_script('if(location.origin===' + json.dumps(base) + ')localStorage.setItem("vf_token",'
                                + json.dumps(settings['token']) + ');')
        page = context.new_page()
        expect.set_options(timeout=45000)
        page_errors = []
        page.on('pageerror', lambda error: page_errors.append(str(error)))
        chooser = page.get_by_test_id('agent-conversation')
        composer = page.get_by_test_id('agent-input')
        answer = page.get_by_test_id('chat-markdown').filter(has=page.get_by_test_id('chat-code-degraded'))
        message = page.get_by_test_id('chat-message').filter(has=answer)

        def enter():
            row = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
            expect(row).to_be_visible()
            if row.get_attribute('aria-pressed') != 'true':
                row.click()
            if not chooser.is_visible():
                page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
            chooser.select_option(str(cid))
            expect(composer).to_be_enabled()

        def check_copy(raw):
            expect(answer).to_have_attribute('data-authoritative', 'true')
            degraded = answer.get_by_test_id('chat-code-degraded')
            assert degraded.text_content() == raw.strip()
            expect(degraded.locator('code, .katex')).to_have_count(0)
            page.bring_to_front()
            button = message.get_by_test_id('chat-copy')
            button.click()
            expect(button).to_have_text('已复制')
            copied = page.evaluate('navigator.clipboard.readText()').replace('\r\n', '\n')
            assert copied == raw.replace('\r\n', '\n')
            return digest(copied)

        try:
            page.goto(base, wait_until='domcontentloaded')
            enter()
            composer.fill(PROMPT)
            endpoint = f'/api/v1/conversations/{cid}/messages'
            with page.expect_response(lambda response: response.request.method == 'POST'
                                      and urlsplit(response.url).path == endpoint, timeout=150000) as sent:
                page.get_by_test_id('agent-send').click()
            response = sent.value
            assert response.ok, response.status
            wire = response.text()
            frames = [json.loads(line[5:]) for line in wire.splitlines() if line.startswith('data:')]
            raw = ''.join(frame['text'] for frame in frames if frame['type'] == 'text.delta')
            size = len(raw.encode('utf8'))
            (root / 'actual-answer.md').write_bytes(raw.encode('utf8'))
            result.update(answer_bytes=size, text_frames=sum(frame['type']=='text.delta' for frame in frames),
                          raw_sha256=digest(raw), tail_present='F05_CODE_END' in raw)
            assert sum(frame['type']=='run.completed' for frame in frames) == 1
            assert size > 32768 and raw.startswith('```text\n') and result['tail_present'], result
            result['first_clipboard_sha256'] = check_copy(raw)
            page.screenshot(path=str(root / 'large-code.png'))
            page.reload(wait_until='domcontentloaded')
            enter()
            result['reopened_clipboard_sha256'] = check_copy(raw)
            assert not page_errors, page_errors
            result['passed'] = True
        finally:
            if not result['passed']:
                page.screenshot(path=str(root / 'large-code-failure.png'))
            (root / 'browser-performance.json').write_text(json.dumps(result, indent=2), encoding='utf8')
            edge.close()


def verify(args, config):
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    api = IsolatedServer('live-f05-chat-performance', app='performance_fixture:app')
    fixture = 'live_request_limit=1\n' + FIXTURE
    fixture += '''
import os
from cryptography.fernet import Fernet
from app.services.process_identity_native import observe
vault = Fernet(Fernet.generate_key())
llm._vault = lambda: vault
(root/'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')
'''
    fixture += f'\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n'
    (api.root / 'performance_fixture.py').write_text(fixture, encoding='utf8')
    result = {'passed': False, 'provider': 'deepseek', 'model': config['model'], 'mock_llm_calls': 0}
    print('LIVE_F05_EVIDENCE', api.root, flush=True)
    try:
        with api:
            case = api.create_case(CASE)
            profile = api.call('POST', '/provider-profiles', {
                'name': 'F05 official DeepSeek', 'provider': 'deepseek', 'base_url': config['base_url'],
                'api_key': config['key'], 'default_model': config['model'],
            })
            cid = api.call('POST', '/conversations', {
                'case_id': case['id'], 'title': '真实长代码块', 'provider_profile_id': profile['id'],
                'model': config['model'], 'permission_mode': 'ask',
            })['id']
            temp = api.root / 'edge-tmp'
            temp.mkdir()
            subprocess.run([str(args.browser_python.resolve(strict=True)), str(Path(__file__).resolve()), '--browser'],
                           cwd=api.root, timeout=420, check=True, encoding='utf8',
                           env=dict(os.environ, PYTHONIOENCODING='utf8', TEMP=str(temp), TMP=str(temp)),
                           input=json.dumps({'root': str(api.root), 'base': api.base, 'token': api.token, 'conversation': cid}))
            result['browser'] = json.loads((api.root / 'browser-performance.json').read_text(encoding='utf8'))
            events = api.call('GET', f'/conversations/{cid}')['events']
            saved = [event['payload']['content'] for event in events if event['type']=='message'
                     and event['payload'].get('role')=='assistant']
            assert len(saved) == 1 and digest(saved[0]) == result['browser']['raw_sha256']
            other = api.call('POST', '/auth/register', {'email': 'f05-other@example.com',
                             'password': 'isolated-demo-123', 'name': 'F05 unrelated account'})
            api.call('GET', f'/conversations/{cid}', expect=404, extra_headers={'Authorization': 'Bearer ' + other['token']})
            result.update(persisted_equal=True, outsider_status=404)
        result['cleanup'] = wait_for_api_exit(api.root, api.port)
        rows = [json.loads(line) for line in (api.root / 'upstream.jsonl').read_text(encoding='utf8').splitlines()]
        assert len([row for row in rows if row['kind']=='http' and row['status']==200]) == 1
        result['passed'] = True
    finally:
        receipts = api.root / 'upstream.jsonl'
        if receipts.is_file():
            result['upstream'] = [json.loads(line) for line in receipts.read_text(encoding='utf8').splitlines()]
        encoded = json.dumps(result, indent=2)
        assert config['key'] not in encoded
        (api.root / 'live-performance-result.json').write_text(encoded, encoding='utf8')


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
