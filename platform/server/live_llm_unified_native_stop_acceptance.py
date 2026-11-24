"""Click Stop for actual Windows and WSL Codex requests from the unified workbench."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from live_llm_acceptance import source
from live_llm_chat_markdown_acceptance import wait_for_api_exit
from live_llm_native_reply_sound_acceptance import (
    FIXTURE, MARKERS, TURN_POST, ResponsesBudget, linux_relay, windows_relay,
)
from testsupport import IsolatedServer


CASE = 'Unified chat native stop'


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings['root'])
    ids = settings['conversations']
    base = settings['base']
    result = {'passed': False, 'turns': {}, 'user_posts': []}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        result['edge'] = edge.version
        context = edge.new_context(viewport={'width': 1530, 'height': 980}, locale='zh-CN')
        context.add_init_script('if(location.origin===' + json.dumps(base) + ')localStorage.setItem("vf_token",'
                                + json.dumps(settings['token']) + ');')
        page = context.new_page()
        page.set_default_timeout(45000)
        choose = page.get_by_test_id('agent-conversation')
        backend = page.get_by_test_id('agent-backend')
        stop = page.get_by_test_id('native-stop')
        composer = page.get_by_test_id('native-input')

        def sent(request):
            path = urlsplit(request.url).path
            if request.method == 'POST' and (TURN_POST.fullmatch(path) or path.endswith(':cancel')):
                result['user_posts'].append(path)

        def choose_case():
            row = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
            expect(row).to_be_visible()
            if row.get_attribute('aria-pressed') != 'true':
                row.click()
            if not choose.is_visible():
                page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()

        def settled(runtime):
            turn = result['turns'][runtime]['id']
            select = page.get_by_role('combobox', name='查看原生回合')
            expect(select).to_have_value(turn, timeout=90000)
            expect(page.get_by_test_id('native-turn-status')).to_have_text(
                re.compile(r'已取消.*收尾已验证'), timeout=90000)
            expect(stop).to_be_disabled()
            expect(composer).to_be_enabled()

        page.on('request', sent)
        try:
            page.goto(base, wait_until='domcontentloaded')
            choose_case()
            for runtime in MARKERS:
                choose.select_option(str(ids[runtime]))
                backend.select_option(runtime)
                expect(composer).to_be_enabled(timeout=90000)
                composer.fill(f'Reply only {MARKERS[runtime]}. Do not use tools.')
                with page.expect_response(lambda response: response.request.method == 'POST'
                                          and TURN_POST.fullmatch(urlsplit(response.url).path), timeout=135000) as submitted:
                    composer.press('Enter')
                assert submitted.value.ok, submitted.value.status
                turn = submitted.value.json()['id']
                result['turns'][runtime] = {'id': turn}
                expect(stop).to_be_enabled(timeout=35000)
                receipt_file = root / 'official-responses.json'
                for _ in range(200):
                    page.wait_for_timeout(200)
                    try:
                        receipts = json.loads(receipt_file.read_text(encoding='utf8'))
                    except (FileNotFoundError, json.JSONDecodeError):
                        continue
                    if any(row.get('runtime') == runtime and row.get('status') == 200 for row in receipts):
                        break
                else:
                    raise AssertionError(f'{runtime} actual Responses receipt unavailable')
                target = f'/api/v1/codex/turns/{turn}:cancel'
                with page.expect_response(lambda response: response.request.method == 'POST'
                                          and urlsplit(response.url).path == target, timeout=25000) as stopped:
                    stop.click()
                assert stopped.value.ok, stopped.value.status
                (root / ('release-' + runtime)).touch()
                settled(runtime)
                result['turns'][runtime]['stopped_from_browser'] = True
                page.screenshot(path=str(root / f'{runtime}-stopped.png'))

            page.reload(wait_until='domcontentloaded')
            choose_case()
            for runtime in MARKERS:
                choose.select_option(str(ids[runtime]))
                expect(backend).to_have_value(runtime)
                settled(runtime)
            choose.select_option(str(ids['api']))
            expect(backend).to_have_value('api')
            expect(page.get_by_test_id('agent-input')).to_be_visible()
            assert len(result['user_posts']) == 4, result['user_posts']
            result.update(passed=True, restored_after_reload=True, api_panel_available=True)
        finally:
            for runtime in MARKERS:
                (root / ('release-' + runtime)).touch()
            if not result['passed']:
                page.screenshot(path=str(root / 'stop-failure.png'))
            (root / 'browser-stop.json').write_text(json.dumps(result, indent=2), encoding='utf8')
            edge.close()


def verify(args, config):
    api = IsolatedServer('live-f01-unified-stop', app='stop_fixture:app')
    token = secrets.token_hex(32)
    budget = ResponsesBudget(config, api.root)
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').exists()
    report = {'passed': False, 'model': config['model'], 'mock_llm_calls': 0, 'stops': {}}
    print('LIVE_F01_EVIDENCE', api.root, flush=True)
    try:
        with windows_relay(budget, token) as windows, linux_relay(budget, token) as linux:
            settings = {'VIBEFLOW_CODEX': str(Path(shutil.which('codex.exe')).resolve(strict=True)),
                        'VIBEFLOW_CODEX_WSL_PATH': str(args.wsl_codex.resolve(strict=True)),
                        'VIBEFLOW_CODEX_WSL_STATE_ROOT': linux['private_root']}
            preface = (f'import os\nos.environ.update({settings!r})\ndist={str(dist)!r}\n'
                       'read_only_mode=False\nno_turn_mode=False\naudio_worklet=""\n')
            (api.root / 'stop_fixture.py').write_text(preface + FIXTURE, encoding='utf8')
            with api:
                case = api.create_case(CASE)
                ids = {}
                for runtime, port in (('windows', windows), ('wsl', linux['port'])):
                    profile = api.call('POST', '/provider-profiles', {
                        'provider': 'custom', 'name': f'Stop {runtime}', 'api_key': token,
                        'base_url': f'http://127.0.0.1:{port}', 'default_model': config['model'],
                    })
                    ids[runtime] = api.call('POST', '/conversations', {
                        'case_id': case['id'], 'title': runtime, 'provider_profile_id': profile['id'],
                        'model': config['model'], 'permission_mode': 'ask',
                    })['id']
                ids['api'] = api.call('POST', '/conversations', {'case_id': case['id'], 'title': 'API'})['id']
                browser_temp = api.root / 'edge-tmp'
                browser_temp.mkdir()
                subprocess.run([str(args.browser_python.resolve(strict=True)), str(Path(__file__).resolve()), '--browser'],
                               cwd=api.root, encoding='utf8', check=True, timeout=650,
                               env=dict(os.environ, PYTHONIOENCODING='utf8', TEMP=str(browser_temp), TMP=str(browser_temp)),
                               input=json.dumps({'root': str(api.root), 'base': api.base, 'token': api.token,
                                                 'conversations': ids}))
                report['browser'] = json.loads((api.root / 'browser-stop.json').read_text(encoding='utf8'))
                for runtime, observed in report['browser']['turns'].items():
                    identifier = observed['id']
                    turn = api.call('GET', '/codex/turns/' + identifier)
                    session = api.call('GET', '/codex/sessions/' + turn['session_id'])
                    assert turn['phase'] == 'cancelled' and turn['cleanup_verified'] is True, turn['phase']
                    assert session['runtime'] == runtime and session['stop']['scope_empty'] is True
                    activity = api.call('GET', f'/codex/conversations/{ids[runtime]}/activity')
                    assert activity['phase'] == 'idle', activity['phase']
                    report['stops'][runtime] = {'turn': identifier, 'phase': turn['phase'],
                                                'scope_empty': True, 'execution_idle': True}
            report['api_cleanup'] = wait_for_api_exit(api.root, api.port)
        assert len(budget.receipts) == 2 and all(receipt['status'] == 200 for receipt in budget.receipts)
        report.update(passed=True, wsl_relay_closed=(api.root / 'wsl-relay-cleanup.json').is_file())
    finally:
        report['receipts'] = budget.receipts
        encoded = json.dumps(report, indent=2)
        assert config['key'] not in encoded and token not in encoded
        (api.root / 'live-stop-result.json').write_text(encoded, encoding='utf8')


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source-profile-id', type=int, required=True)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--wsl-codex', type=Path, required=True)
        args = parser.parse_args()
        verify(args, asyncio.run(source(args.source_profile_id)))
