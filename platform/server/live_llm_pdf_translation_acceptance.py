"""Exercise PDF text selection and clipboard with authorized DeepSeek and real Edge."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import httpx

from live_llm_acceptance import source
from testsupport import IsolatedServer


CASE = "PDF selection translation"
PDF = "Ref/translation-sample.pdf"
ENGLISH = "The inlet pressure is measured upstream."
DELAYED = "The wake extends behind the cylinder."
CHINESE = "流体沿管道向下游流动。"
TEX = rf"""\documentclass[12pt]{{article}}
\usepackage{{fontspec}}
\setmainfont[Path=/mnt/c/Windows/Fonts/]{{msyh.ttc}}
\pagestyle{{empty}}
\begin{{document}}
\noindent {ENGLISH}\par\medskip
\noindent {DELAYED}\par
\newpage
\noindent {CHINESE}\par
\end{{document}}
"""

FIXTURE = r'''
import asyncio, hashlib, json, time
from pathlib import Path
import httpx
from cryptography.fernet import Fernet
from fastapi.staticfiles import StaticFiles
from app.services import llm

root = Path(__file__).parent
fixture_vault = Fernet(Fernet.generate_key())
llm._vault = lambda: fixture_vault  # Encrypt with a process-local key, without retaining decryptable credentials.
from app.main import app
from app.services import process_identity_native
import os

(root / 'api-birth.json').write_text(json.dumps(process_identity_native.observe(os.getpid())))
original_send = httpx.AsyncClient.send
calls = 0

async def observe_actual_request(self, request, *args, **kwargs):
    global calls
    is_translation = request.url.host == 'api.deepseek.com' and request.url.path.endswith('/chat/completions')
    if is_translation:
        calls += 1
        assert calls <= 3
        payload = json.loads(request.content)
        assert payload['stream'] is False
        prefix, separator, text = payload['messages'][-1]['content'].partition('\n\n')
        assert separator and prefix in {'Translate the source text below into Simplified Chinese.',
                                        'Translate the source text below into English.'}
        digest = hashlib.sha256(text.encode()).hexdigest()
    response = await original_send(self, request, *args, **kwargs)
    if is_translation:
        record = {'host': request.url.host, 'status': response.status_code, 'source_sha256': digest}
        if response.status_code == 200:
            result = response.json()
            record.update({key: result[key] for key in ('model', 'id', 'usage')})
        with (root / 'upstream.jsonl').open('a', encoding='utf8') as output:
            output.write(json.dumps(record) + '\n')
        if digest == DELAYED_DIGEST:
            (root / 'held-response').touch()
            deadline = time.monotonic() + 25
            while not (root / 'release-response').exists():
                if time.monotonic() > deadline:
                    raise TimeoutError('Browser did not release the real response')
                await asyncio.sleep(.04)
    return response

httpx.AsyncClient.send = observe_actual_request
app.mount('/', StaticFiles(directory=DIST, html=True))
'''


def wait_for(check, timeout=35):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(.08)
    raise TimeoutError("Acceptance boundary did not arrive")


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings['root'])
    base = settings['base']
    stages = {'passed': False, 'requests': []}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel=settings['channel'])
        context = edge.new_context(viewport={'width': 1560, 'height': 1020}, locale='zh-CN',
                                   permissions=['clipboard-read', 'clipboard-write'])
        context.add_init_script(f"localStorage.setItem('vf_token', {json.dumps(settings['token'])});")
        page = context.new_page()
        expect.set_options(timeout=15000)
        translate = page.get_by_test_id('pdf-translate')
        card = page.get_by_test_id('pdf-translation')
        content = page.get_by_test_id('pdf-translation-content')
        copy = page.get_by_test_id('pdf-copy-translation')

        def is_translation(request):
            return request.method == 'POST' and request.url == base + '/api/v1/llm/translate'

        def request_received(request):
            if is_translation(request):
                stages['requests'].append(request.post_data_json)

        def select(text):
            match = page.locator('.textLayer span').get_by_text(text, exact=True)
            if not match.count():
                match = page.locator('.textLayer span').filter(has_text=re.compile('^' + re.escape(text) + '$'))
            expect(match).to_be_visible()
            match.scroll_into_view_if_needed()
            box = match.bounding_box()
            y = box['y'] + box['height'] / 2
            page.mouse.move(box['x'] + .5, y)
            page.mouse.down()
            page.mouse.move(box['x'] + box['width'] - .5, y, steps=22)
            page.mouse.up()
            assert page.evaluate('window.getSelection().toString().trim()') == text
            expect(translate).to_be_visible()

        def response_for(text):
            return page.expect_response(lambda response: is_translation(response.request)
                                        and response.request.post_data_json['text'] == text, timeout=50000)

        def assert_copied(text, expected_target, trigger):
            with response_for(text) as receipt:
                trigger.click()
            response = receipt.value
            assert response.status == 200, response.status
            data = response.json()
            assert data['source'] == text and data['target'] == expected_target
            translated = data['translation'].strip()
            assert translated and translated != text
            if expected_target == 'zh':
                assert '压力' in translated and '上游' in translated
            else:
                lowered = translated.lower()
                assert 'downstream' in lowered and any(term in lowered for term in ('pipe', 'duct', 'channel'))
            expect(content).to_have_text(translated)
            copy.click()
            expect(card).to_contain_text('译文已复制')
            assert page.evaluate('navigator.clipboard.readText()') == data['translation']
            stages[expected_target] = {'source': text, 'translation': translated, 'copied_exactly': True}
            page.screenshot(path=str(root / f'translation-{expected_target}.png'))

        page.on('request', request_received)
        try:
            page.goto(base)
            page.locator('[data-case-name=' + json.dumps(CASE) + ']').click()
            directory = page.locator('[data-path="Ref"]')
            expect(directory).to_be_visible()
            if not page.locator('[data-path=' + json.dumps(PDF) + ']').is_visible():
                directory.click()
            page.locator('[data-path=' + json.dumps(PDF) + ']').click()
            select(ENGLISH)
            with response_for(ENGLISH) as rejected:
                translate.click()
            assert rejected.value.status == 400
            expect(card).to_contain_text('无可用 provider profile')
            stages['missing_profile_status'] = 400
            if settings['baseline']:
                expect(copy).to_be_visible()
                stages['baseline_exposes_copy_on_error'] = True
                page.screenshot(path=str(root / 'baseline-copy-on-error.png'))
                stages['passed'] = True
                return
            expect(copy).to_have_count(0)
            page.screenshot(path=str(root / 'translation-error.png'))
            (root / 'setup-profile').touch()
            wait_for((root / 'profile-ready').exists)
            assert_copied(ENGLISH, 'zh', page.get_by_role('button', name='重试翻译', exact=True))
            card.get_by_role('button', name='关闭', exact=True).click()
            expect(card).to_have_count(0)

            select(DELAYED)
            with response_for(DELAYED) as pending:
                translate.click()
                expect(content).to_have_text('翻译中…')
                wait_for((root / 'held-response').exists, 45)
                page.get_by_test_id('pdf-next').click()
                expect(page.get_by_test_id('pdf-page-indicator')).to_have_text('2 / 2')
                select(CHINESE)
                (root / 'release-response').touch()
            assert pending.value.status == 200
            assert pending.value.json()['translation'].strip()
            expect(translate).to_be_visible()
            expect(card).to_have_count(0)
            stages['page_change_discards_actual_old_result'] = True
            assert_copied(CHINESE, 'en', translate)

            assert stages['requests'] == [
                {'text': ENGLISH, 'target': 'zh'}, {'text': ENGLISH, 'target': 'zh'},
                {'text': DELAYED, 'target': 'zh'}, {'text': CHINESE, 'target': 'en'},
            ]
            stages['passed'] = True
        finally:
            (root / 'browser-result.json').write_text(json.dumps(stages, ensure_ascii=False, indent=2), encoding='utf8')
            if not stages['passed']:
                page.screenshot(path=str(root / 'browser-failure.png'))
            edge.close()


def pdf_sample(api):
    source_path = api.root / 'sample.tex'
    source_path.write_text(TEX, encoding='utf8')
    linux = '/mnt/' + api.root.drive[0].lower() + api.root.as_posix()[2:]
    with (api.root / 'xelatex.log').open('wb') as log:
        subprocess.run(['wsl.exe', '-d', 'vibeflow-linux', '-u', 'nobody', '--cd', linux, '--exec', '/usr/bin/xelatex',
                        '-interaction=nonstopmode', '-halt-on-error', '-no-shell-escape', source_path.name],
                       stdout=log, stderr=subprocess.STDOUT, timeout=35, check=True)
    with (api.root / 'sample.pdf').open('rb') as pdf:
        expected = hashlib.file_digest(pdf, 'sha256').hexdigest()
        pdf.seek(0)
        response = httpx.post(api.base + '/api/v1' + api.prefix + '/upload',
            params={'path': PDF, 'expected_absent': 'true'}, headers={'Authorization': 'Bearer ' + api.token},
            files={'file': ('translation-sample.pdf', pdf, 'application/pdf')}, trust_env=False, timeout=30)
        response.raise_for_status()
        assert response.json()['content_sha256'] == expected
    return expected


def verify(args):
    from app.services.process_identity_native import probe

    config = None if args.baseline else asyncio.run(source(args.source_profile_id))
    api = IsolatedServer('live-pdf-translation', app='translation_fixture:app', access_log=True)
    root = api.root
    print('LIVE_PDF_TRANSLATION_EVIDENCE', root, flush=True)
    dist = args.dist.resolve(strict=True)
    sha = lambda text: hashlib.sha256(text.encode()).hexdigest()
    (root / 'translation_fixture.py').write_text(
        f'DELAYED_DIGEST = {sha(DELAYED)!r}\nDIST = {str(dist)!r}\n' + FIXTURE, encoding='utf8')
    temporary = root / 'browser-temp'
    temporary.mkdir()
    result = {'passed': False, 'baseline': args.baseline, 'mock_llm_calls': 0}
    upstream = root / 'upstream.jsonl'
    try:
        with api:
            api.create_case(CASE)
            result['pdf_sha256'] = pdf_sample(api)
            context = {'root': str(root), 'base': api.base, 'token': api.token, 'channel': args.channel,
                       'baseline': args.baseline}
            with subprocess.Popen([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                                  stdin=subprocess.PIPE, encoding='utf8',
                                  env=dict(os.environ, TMP=str(temporary), TEMP=str(temporary))) as child:
                try:
                    child.stdin.write(json.dumps(context))
                    child.stdin.close()
                    if not args.baseline:
                        marker = root / 'setup-profile'
                        wait_for(lambda: marker.exists() or child.poll() is not None, 80)
                        assert marker.exists(), 'Browser failed before the recovery step'
                        profile = api.call('POST', '/provider-profiles', {
                            'provider': 'deepseek', 'name': 'Isolated PDF translation',
                            'default_model': config['model'], 'base_url': config['base_url'], 'api_key': config['key']})
                        assert config['key'] not in json.dumps(profile)
                        (root / 'profile-ready').touch()
                    assert child.wait(timeout=150) == 0
                finally:
                    (root / 'release-response').touch()
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=10)
            result['browser'] = json.loads((root / 'browser-result.json').read_text(encoding='utf8'))
            assert result['browser']['passed']
            profile_id = None if args.baseline else profile['id']
            api.token = None
            api.call('POST', '/llm/translate', {'text': ENGLISH, 'target': 'zh'}, expect=401)
            if profile_id is not None:
                api.create_case('Different PDF account')
                api.call('POST', '/llm/translate', {'text': ENGLISH, 'target': 'zh',
                         'provider_profile_id': profile_id}, expect=404)
                result['other_profile_404'] = True
            birth = json.loads((root / 'api-birth.json').read_text(encoding='utf8'))
        wait_for(lambda: probe(birth)['state'] == 'exited', 12)
        with socket.socket() as connection:
            connection.settimeout(.5)
            assert connection.connect_ex(('127.0.0.1', api.port)) != 0
        result['api_exited'] = True
        records = [json.loads(line) for line in upstream.read_text(encoding='utf8').splitlines()] if upstream.exists() else []
        assert args.baseline and not records or (len(records) == 3 and
            [row['source_sha256'] for row in records] == [sha(text) for text in (ENGLISH, DELAYED, CHINESE)]
            and len({row['id'] for row in records}) == 3
            and all(row['status'] == 200 and row['host'] == 'api.deepseek.com' for row in records))
        result.update(passed=True, real_model_requests=len(records), upstream=records)
    finally:
        (root / 'live-pdf-translation-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    print('LIVE_PDF_TRANSLATION_PASS', root, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source-profile-id', type=int)
        parser.add_argument('--baseline', action='store_true')
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        options = parser.parse_args()
        if not options.baseline and options.source_profile_id is None:
            parser.error('--source-profile-id is required for real model acceptance')
        verify(options)
