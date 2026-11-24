"""Real Edge and Windows Codex goal turns forwarded to official DeepSeek."""
import argparse
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid


CASE = 'Actual Native Goal acceptance'
FIRST = ('Goal for this session: read proof.txt once using PowerShell Get-Content. Reply only with the verification string, '
         'with no formatting. Keep that string in context for the next turn. Do not edit any file or use other tools.')
CONTINUE = ('Continue the same goal. Without reading the file again or using any tools, reply with exactly the verification '
            'string from the preceding assistant answer, with no formatting.')
LONG = ('For this next turn, without invoking tools, print integers from 1 through 800, one per line. Begin at 1 and do '
        'not summarize or omit lines.')
NATIVE_OPTIONS = '''
from app.services import codex_sessions
original_native_command = codex_sessions.command
def goal_acceptance_command(credential=None):
    argv = original_native_command(credential)
    return [argv[0], '--disable', 'multi_agent', '--disable', 'multi_agent_v2', '-c', 'web_search="disabled"', *argv[1:]]
codex_sessions.command = goal_acceptance_command
'''


def wait_for_process_exit(birth, timeout=10):
    from app.services.process_identity_native import probe

    deadline = time.monotonic() + timeout
    observed = probe(birth)
    while observed['state'] == 'match' and time.monotonic() < deadline:
        time.sleep(.05)
        observed = probe(birth)
    return observed


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    cid = settings['conversation']
    api = base + '/api/v1'
    headers = {'Authorization': 'Bearer ' + settings['token']}
    response_path = re.compile(r'/api/v1/codex/sessions/[^/]+/turns$')
    observed = {'passed': False, 'turns': [], 'api_goal_requests': 0, 'native_submissions': 0}
    arrivals = root / 'forwarder-arrivals.jsonl'

    def upstream_started():
        return len(arrivals.read_text('utf8').splitlines()) if arrivals.exists() else 0

    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel=settings['channel'])
        context = edge.new_context(viewport={'width': 1680, 'height': 970}, locale='zh-CN')
        context.add_init_script('if(location.origin===' + json.dumps(base) + ')localStorage.setItem("vf_token",' +
                                json.dumps(settings['token']) + ');')
        page = context.new_page()
        page.set_default_timeout(45000)
        draft = page.get_by_test_id('native-input')
        waiting = page.get_by_test_id('native-goal-waiting')
        advance = page.get_by_test_id('native-goal-continue')
        stop = page.get_by_test_id('native-stop')

        def observe(request):
            if request.method != 'POST':
                return
            path = urlsplit(request.url).path
            if path.endswith('/goal:run'):
                observed['api_goal_requests'] += 1
            if response_path.fullmatch(path):
                observed['native_submissions'] += 1

        def api_get(path):
            r = context.request.get(api + path, headers=headers, timeout=45000)
            assert r.ok, (path, r.status, r.text()[:170])
            return r

        def select_case():
            page.locator('[data-case-name=' + json.dumps(CASE) + ']').click()
            page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
            page.get_by_test_id('agent-conversation').select_option(str(cid))
            page.get_by_test_id('agent-backend').select_option('windows')
            expect(draft).to_be_enabled(timeout=90000)

        def submit(button, prompt, previous=None):
            draft.fill(prompt)
            with page.expect_response(lambda answer: answer.request.method == 'POST' and response_path.fullmatch(urlsplit(answer.url).path),
                                      timeout=120000) as submitted:
                button.click()
            result = submitted.value
            assert result.status == 200, (result.status, result.text()[:250])
            body = result.request.post_data_json
            assert body['content'] == prompt and body['goal'] is True
            assert body['resume'] is (previous is not None) and body.get('goal_source_turn_id') == previous
            row = result.json()
            observed['turns'].append(row['id'])
            return row['id']

        def verify_waiting(identifier, original=None):
            expect(waiting).to_have_attribute('data-turn-id', identifier, timeout=210000)
            expect(waiting.get_by_role('status')).to_contain_text('等待继续')
            expect(stop).to_be_disabled()
            row = api_get('/codex/turns/' + identifier).json()
            assert row['phase'] == 'completed' and row['status'] == 'waiting_continue' and row['cleanup_verified'] is True
            assert row['mode'] == 'goal' and row['thread_id']
            events = api_get('/codex/turns/' + identifier + '/events').text()
            (root / (identifier + '.sse')).write_text(events, 'utf8')
            messages = [json.loads(line[5:]) for line in events.splitlines() if line.startswith('data:')]
            assert messages[-1]['type'] == 'stream.done' and messages[-1]['payload']['status'] == 'waiting_continue'
            answer = ''.join(event['payload']['text'] for event in messages if event['type'] == 'text.delta').strip()
            assert answer == settings['proof'], 'Actual Codex reply did not match the isolated proof'
            tools = [event for event in messages if event['type'] == 'item.completed' and event['payload'].get('type') == 'commandExecution']
            if original is None:
                assert len(tools) == 1 and tools[0]['payload']['exitCode'] == 0 and settings['proof'] in tools[0]['payload']['aggregatedOutput']
            else:
                assert row['thread_id'] == original['thread_id'] and row['native_turn_id'] != original['native_turn_id']
                assert any(e['type'] == 'turn.admitted' and e['payload'].get('goal_source_turn_id') == original['id'] for e in messages)
                assert not tools and not any(e['type'] == 'context.handoff' for e in messages)
            return row

        try:
            page.on('request', observe)
            page.goto(base, wait_until='domcontentloaded')
            select_case()
            page.get_by_role('checkbox', name='目标回合').check()
            first = submit(page.get_by_role('button', name='发送', exact=True), FIRST)
            original = verify_waiting(first)
            observed['first_waiting'] = True
            before_reload = upstream_started()
            page.reload(wait_until='domcontentloaded')
            select_case()
            expect(waiting).to_have_attribute('data-turn-id', first, timeout=90000)
            page.wait_for_timeout(3100)
            existing = api_get(f'/codex/conversations/{cid}/turns').json()['data']
            assert [row['id'] for row in existing] == [first] and upstream_started() == before_reload
            assert observed['native_submissions'] == 1
            observed['refresh_without_dispatch'] = True
            page.screenshot(path=str(root / 'native-goal-waiting.png'))
            second = submit(advance, CONTINUE, first)
            resumed = verify_waiting(second, original)
            observed['same_native_thread'] = resumed['thread_id']

            def try_stale(route):
                path = urlsplit(route.request.url).path
                assert route.request.method == 'POST' and response_path.fullmatch(path)
                body = route.request.post_data_json
                starts = upstream_started()
                refused = context.request.post(route.request.url, headers=headers,
                    data={**body, 'goal_source_turn_id': first, 'client_message_id': str(uuid.uuid4())})
                assert refused.status == 409 and refused.json()['detail']['code'] == 'input_stale'
                assert upstream_started() == starts
                observed['superseded_goal_rejected'] = True
                route.continue_()

            page.route(re.compile(r'/api/v1/codex/sessions/[^/]+/turns$'), try_stale, times=1)
            before_cancel = upstream_started()
            third = submit(advance, LONG, second)
            deadline = time.monotonic() + 85
            while upstream_started() == before_cancel:
                assert time.monotonic() < deadline, 'Actual DeepSeek request never started'
                page.wait_for_timeout(100)
            expect(stop).to_be_enabled()
            with page.expect_response(lambda answer: answer.request.method == 'POST' and answer.url.endswith(f'/turns/{third}:cancel'),
                                      timeout=115000) as stopping:
                stop.click()
            response = stopping.value
            assert response.status == 200, (response.status, response.text()[:200])
            cancelled = response.json()
            assert cancelled['phase'] == 'cancelled' and cancelled['cleanup_verified'] is True
            assert cancelled['thread_id'] == original['thread_id']
            expect(stop).to_be_disabled()
            expect(waiting).to_have_count(0)
            assert api_get(f'/codex/conversations/{cid}/activity').json()['phase'] == 'idle'
            assert observed['native_submissions'] == 3 and observed['api_goal_requests'] == 0
            observed['actual_stop'] = True
            observed['passed'] = True
        finally:
            (root / 'browser-result.json').write_text(json.dumps(observed, ensure_ascii=False, indent=2), 'utf8')
            if not observed['passed']:
                page.screenshot(path=str(root / 'native-goal-failure.png'))
            edge.close()


def verify(args, configuration):
    from live_llm_codex_terminal_provider_acceptance import EXE, Forwarder
    from live_llm_shared_index_acceptance import SharedIndexServer, configure

    assert configuration['model'] == 'deepseek-flash'
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    api = SharedIndexServer('live-native-goal', app='live_fixture:app')
    configure(api, {'VIBEFLOW_CODEX': str(EXE)}, False)
    fixture = api.root / 'live_fixture.py'
    fixture.write_text(fixture.read_text('utf8') + NATIVE_OPTIONS + '\nfrom fastapi.staticfiles import StaticFiles\n' +
                       'app.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n', 'utf8')
    browser_tmp = api.root / 'browser-tmp'
    browser_tmp.mkdir()
    proof = 'NATIVE_GOAL_' + uuid.uuid4().hex
    report = {'passed': False, 'mock_llm_calls': 0, 'upstream': [], 'runtime': 'windows',
              'codex_sha256': sha256(EXE.read_bytes()).hexdigest()}
    print('F50_NATIVE_GOAL_EVIDENCE', api.root, flush=True)

    def persist():
        content = json.dumps(report, ensure_ascii=False, indent=2)
        assert configuration['key'] not in content
        (api.root / 'native-goal-result.json').write_text(content, 'utf8')

    try:
        with Forwarder(configuration, api.root) as upstream:
            try:
                with api:
                    api.create_case(CASE)
                    api.put('proof.txt', proof + '\n')
                    profile = api.call('POST', '/provider-profiles', {'provider': 'custom', 'name': 'Actual Codex Responses',
                        'base_url': upstream.base, 'default_model': configuration['model'], 'api_key': configuration['key']})
                    conv = api.call('POST', '/conversations', {'case_id': api.case_id, 'title': 'Native Goal',
                        'permission_mode': 'full_access', 'provider_profile_id': profile['id'], 'model': configuration['model']})
                    report['conversation'] = conv['id']
                    subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                        input=json.dumps({'root': str(api.root), 'base': api.base, 'conversation': conv['id'], 'token': api.token,
                                          'proof': proof, 'channel': args.channel}), text=True, check=True, timeout=650,
                        env=dict(os.environ, TEMP=str(browser_tmp), TMP=str(browser_tmp)))
                    report['browser'] = json.loads((api.root / 'browser-result.json').read_text('utf8'))
                    assert report['browser']['passed']
                    report['turns'] = [api.call('GET', '/codex/turns/' + identity) for identity in report['browser']['turns']]
                    profiles = [api.call('GET', '/codex/sessions/' + turn['session_id']) for turn in report['turns']]
                    assert [turn['phase'] for turn in report['turns']] == ['completed', 'completed', 'cancelled']
                    assert all(value['cleanup_verified'] is True and value['stop']['verified'] and value['stop']['scope_empty'] for value in profiles)
                    report['session_cleanup'] = [{'id': item['id'], 'phase': item['phase'], 'verified': item['cleanup_verified']}
                                                 for item in profiles]
                    assert api.call('GET', api.prefix + '/file?path=proof.txt')['content'] == proof + '\n'
                    # Cancellation is local. Count any real HTTP completion from the stopped turn independently.
                    deadline = time.monotonic() + 180
                    while len(upstream.receipts) < upstream.count:
                        assert time.monotonic() < deadline, 'Outstanding real upstream receipt'
                        time.sleep(.15)
                    assert len(upstream.receipts) >= 4 and all(item['status'] == 200 for item in upstream.receipts)
            finally:
                report['upstream'] = list(upstream.receipts)
                if hasattr(api, 'birth'):
                    report['api_birth'] = api.birth
                    report['api_exit'] = wait_for_process_exit(api.birth)
                persist()
        assert report['api_exit']['state'] == 'exited'
        report['passed'] = True
    except Exception as error:
        report['failure'] = str(error).replace(configuration['key'], '[REDACTED]')[:700]
    finally:
        persist()
    print('F50_NATIVE_GOAL_RESULT', report['passed'], flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source-profile-id', type=int, required=True)
        parser.add_argument('--review', type=Path, required=True)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        args = parser.parse_args()
        entry = Path(__file__).resolve()
        repo = entry.parents[2]
        gate = json.loads(args.review.read_text('utf8'))
        name = entry.relative_to(repo).as_posix()
        assert gate['entry'] == name and gate['classification'] == 'real_llm' and name in gate['sources']
        for path, digest in gate['sources'].items():
            candidate = (repo / path).resolve()
            assert candidate.is_relative_to(repo) and sha256(candidate.read_bytes()).hexdigest() == digest, path
        from live_llm_codex_terminal_provider_acceptance import EXE
        assert sha256(EXE.read_bytes()).hexdigest() == gate['codex_exe_sha256']
        from live_llm_acceptance import source
        sys.exit(verify(args, asyncio.run(source(args.source_profile_id))))
