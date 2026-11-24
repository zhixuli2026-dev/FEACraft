"""FF-F08A. Real DeepSeek proposals, Edge diff disclosure, conflicts, approval and history."""
import argparse
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import unquote, urlencode, urlsplit

import httpx

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer


CASE = 'FF-F08A actual approval diff'
LONG = 'Ref/large-approval.txt'
NEW = 'Ref/approval-created.txt'
NON_TEXT = 'Ref/approval-nontext.bin'
OLD_LINES = 'APPROVAL_LONG_BEFORE\nAPPROVAL_OBSOLETE_LINE\n'
UPDATED_LINE = 'APPROVAL_LONG_AFTER $&\n'
NEW_CONTENT = 'Approval-created alpha\nApproval-created beta'
NON_TEXT_CONTENT = 'A harmless UTF-8 file classified as non-text by its suffix.'


def encode_json(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def digest(text):
    return hashlib.sha256(text.encode('utf8')).hexdigest()


def write_json(path, value):
    path.write_text(encode_json(value), encoding='utf8')


def frames_from(text):
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith('data:')]


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings['root'])
    base = settings['base']
    case_id = settings['case']
    auth = {'Authorization': 'Bearer ' + settings['token']}
    result = {'passed': False, 'observations': {}, 'decisions': []}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        context = edge.new_context(viewport={'width': 1530, 'height': 970}, locale='zh-CN')
        context.add_init_script('if(location.origin===' + json.dumps(base) + ')localStorage.setItem("vf_token",'
                                + json.dumps(settings['token']) + ');')
        page = context.new_page()
        page.set_default_timeout(60000)
        expect.set_options(timeout=65000)
        result['edge'] = edge.version
        choose = page.get_by_test_id('agent-conversation')
        card = page.get_by_test_id('approval-card')

        def select(name, *, pending=True):
            case = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
            if case.get_attribute('aria-pressed') != 'true':
                case.click()
            if not choose.is_visible():
                page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
            cid = str(settings['conversations'][name]['id'])
            choose.select_option(cid)
            if pending:
                expect(card).to_be_visible()
                expect(card).to_contain_text(settings['conversations'][name]['path'])
            return cid

        def wait_for_workbench():
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                page.wait_for_timeout(250)
                with closing(sqlite3.connect((root / 'test.sqlite').as_uri() + '?mode=ro', uri=True)) as sql:
                    active = sql.execute("SELECT count(*) FROM native_requests WHERE phase='active' "
                        "AND kind IN ('runtime.python_probe','runtime.wsl_probe')").fetchone()[0]
                if active == 0:
                    return
            raise AssertionError('Actual workbench capability probes remained active')

        def external_save(text, original_hash):
            target = base + f'/api/v1/cases/{case_id}/file'
            payload = {'path': LONG, 'content': text, 'expected_sha256': original_hash}
            for attempt in range(2):
                wait_for_workbench()
                reply = context.request.put(target, headers=auth, data=payload, timeout=65000)
                if reply.status == 200:
                    return
                assert (attempt == 0 and reply.status == 409
                        and reply.json()['detail']['code'] == 'storage_quota_busy'), (reply.status, reply.text()[:250])
            raise AssertionError('The real outside save did not complete')

        def counts(added, removed):
            numbers = card.get_by_test_id('approval-diff-counts')
            expect(numbers).to_have_attribute('data-added', str(added))
            expect(numbers).to_have_attribute('data-removed', str(removed))
            return {'added': added, 'removed': removed}

        def decision(name, accept):
            wait_for_workbench()
            cid = settings['conversations'][name]['id']
            tool = settings['conversations'][name]['tool_id']
            action = 'approve' if accept else 'reject'
            endpoint = f'/api/v1/conversations/{cid}/tool-calls/{tool}:{action}'
            button = card.get_by_role('button', name='批准执行' if accept else '拒绝', exact=True)
            with page.expect_response(lambda response: response.request.method == 'POST'
                    and unquote(urlsplit(response.url).path) == endpoint, timeout=145000) as observed:
                button.click()
            reply = observed.value
            assert reply.status == 200, (reply.status, reply.text()[:350])
            result['decisions'].append({'name': name, 'action': action, 'status': reply.status})
            if accept:
                events = frames_from(reply.text())
                write_json(root / (name + '-approved-events.json'), events)
                updated = [frame for frame in events if frame['type'] == 'tool.result' and frame.get('ok')]
                assert len(updated) == 1 and any(frame['type'] == 'run.completed' for frame in events)
            expect(card).to_have_count(0)

        def interrupted_decision(name, accept):
            wait_for_workbench()
            cid = settings['conversations'][name]['id']
            tool = settings['conversations'][name]['tool_id']
            action = 'approve' if accept else 'reject'
            endpoint = f'/api/v1/conversations/{cid}/tool-calls/{tool}:{action}'
            target = lambda url: unquote(urlsplit(url).path) == endpoint
            button = card.get_by_role('button', name='批准执行' if accept else '拒绝', exact=True)
            status = page.get_by_test_id('profile-status')
            observed = []

            def drop_before_send(route):
                observed.append('dropped_before_transmission')
                route.abort()

            page.route(target, drop_before_send)
            button.evaluate('(node) => { node.click(); node.click(); }')
            expect(status).to_contain_text('审批仍在等待，可以重试')
            expect(card).to_be_visible()
            expect(button).to_be_enabled()
            page.unroute(target, drop_before_send)
            assert observed == ['dropped_before_transmission'], observed
            page.screenshot(path=str(root / (name + '-decision-retry.png')))

            def drop_after_commit(route):
                response = route.fetch(timeout=145000)
                observed.append(response.status)
                if accept and response.ok:
                    write_json(root / (name + '-approved-events.json'), frames_from(response.text()))
                route.abort()

            page.route(target, drop_after_commit)
            button.click()
            expect(status).to_contain_text('已同步服务器的审批状态')
            expect(card).to_have_count(0)
            history = page.locator('[data-testid="chat-tool-card"]').filter(has_text=settings['conversations'][name]['path'])
            expected_status = 'completed' if accept else 'cancelled'
            expect(history).to_have_attribute('data-status', expected_status)
            page.unroute(target, drop_after_commit)
            assert observed == ['dropped_before_transmission', 200], observed
            if accept:
                events = json.loads((root / (name + '-approved-events.json')).read_text(encoding='utf8'))
                assert sum(event['type'] == 'tool.result' and event.get('ok') for event in events) == 1
                assert any(event['type'] == 'run.completed' for event in events)
            for repeated_action in ('approve', 'reject'):
                duplicate = context.request.post(base + f'/api/v1/conversations/{cid}/tool-calls/{tool}:{repeated_action}',
                    data={}, headers=auth)
                assert duplicate.status == 409, (repeated_action, duplicate.status)
            result['decisions'].append({'name': name, 'action': action, 'status': 200,
                'browser_dropped_before_send_and_after_actual_commit': True, 'double_click_single_request': True,
                'both_repeat_decisions_refused': True, 'projected_terminal_state': expected_status})
            page.screenshot(path=str(root / (name + '-decision-reconciled.png')))

        try:
            page.goto(base, wait_until='domcontentloaded')
            select('long')
            seen = counts(1, 2)
            row_changes = card.locator('[data-testid="approval-diff-row"][data-kind="add"]')
            expect(row_changes).to_have_text('+ ' + UPDATED_LINE.rstrip('\n'))
            deletions = card.locator('[data-testid="approval-diff-row"][data-kind="del"]')
            expect(deletions).to_have_count(2)
            expect(deletions.nth(1)).to_contain_text('APPROVAL_OBSOLETE_LINE')
            expect(card.locator('[data-kind="gap"]').first).to_contain_text('省略')
            colors = [row_changes.evaluate('(node) => getComputedStyle(node).color'),
                      deletions.first.evaluate('(node) => getComputedStyle(node).color')]
            assert colors[0] != colors[1]
            expect(card.get_by_role('button', name='批准执行')).to_be_enabled()
            page.screenshot(path=str(root / 'approval-long-diff.png'))
            before = (root / 'long-source.txt').read_text(encoding='utf8')
            changed = before.replace('Tabular record 17', 'External actor changed record 17', 1)
            external_save(changed, digest(before))
            page.reload(wait_until='domcontentloaded')
            select('long')
            failure = card.get_by_test_id('approval-diff-status')
            expect(failure).to_have_attribute('data-state', 'stale')
            expect(failure).to_contain_text('文件已偏离提案时的版本')
            expect(card.get_by_test_id('approval-diff-counts')).to_have_count(0)
            expect(card.get_by_role('button', name='批准执行')).to_be_disabled()
            page.screenshot(path=str(root / 'approval-stale-diff.png'))

            wait_for_workbench()
            missing = context.request.delete(base + f'/api/v1/cases/{case_id}/file?' + urlencode({
                'path': LONG, 'expected_sha256': digest(changed)}), headers=auth, timeout=65000)
            assert missing.status == 200, (missing.status, missing.text()[:250])
            observed = context.request.get(base + f'/api/v1/cases/{case_id}/file:version?' + urlencode({'path': LONG}),
                headers=auth)
            assert observed.status == 200 and not observed.json()['exists']
            page.reload(wait_until='domcontentloaded')
            select('long')
            expect(failure).to_have_attribute('data-state', 'stale')
            expect(card.get_by_test_id('approval-diff-counts')).to_have_count(0)
            expect(card.get_by_role('button', name='批准执行')).to_be_disabled()
            page.screenshot(path=str(root / 'approval-deleted-baseline.png'))
            external_save(changed, None)
            decision('long', False)
            result['observations']['long'] = {**seen, 'colors': colors, 'original_lines': len(before.splitlines()),
                'last_changed_line': 6202, 'external_sha256': digest(changed), 'stale_after_reload': True,
                'external_delete_status': missing.status, 'external_deletion_displayed_as_stale': True}

            select('nontext')
            explanation = card.get_by_test_id('approval-diff-status')
            expect(explanation).to_have_attribute('data-state', 'binary')
            expect(explanation).to_contain_text('当前文件为二进制')
            expect(card.get_by_test_id('approval-diff-counts')).to_have_count(0)
            card.get_by_text('查看提案参数', exact=True).click()
            expect(card).to_contain_text(settings['conversations']['nontext']['actual_arguments']['content'])
            page.screenshot(path=str(root / 'approval-nontext.png'))
            if settings.get('one_time'):
                interrupted_decision('nontext', False)
            else:
                decision('nontext', False)
            result['observations']['nontext'] = {'binary_label': True, 'line_counts': None}

            select('new')
            expect(card).to_contain_text('新建文件')
            result['observations']['new'] = counts(2, 0)
            added = card.locator('[data-testid="approval-diff-row"][data-kind="add"]')
            expect(added.nth(1)).to_contain_text('[无结尾换行]')
            page.screenshot(path=str(root / 'approval-new-file.png'))
            if settings.get('one_time'):
                interrupted_decision('new', True)
            else:
                decision('new', True)
            page.reload(wait_until='domcontentloaded')
            select('new', pending=False)
            expect(choose).to_have_value(str(settings['conversations']['new']['id']))
            expect(card).to_have_count(0)
            history = page.locator('[data-testid="chat-tool-card"][data-kind="edit"][data-status="completed"]')
            history = history.filter(has_text=NEW)
            expect(history.first).to_be_visible()
            result['observations']['new']['history_after_refresh'] = True
            result['passed'] = True
        except BaseException:
            page.screenshot(path=str(root / 'approval-diff-failure.png'), timeout=4000)
            raise
        finally:
            write_json(root / 'approval-diff-browser.json', result)
            edge.close()


def verify(args, configuration):
    from app.services.process_identity_native import probe

    api = IsolatedServer('live-f08a-approval-diff', app='approval_diff_fixture:app', access_log=True)
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    # Keep the real production encryption/decryption path. The test vault key
    # exists only in this private API process and is lost when it exits.
    setup = '''
from cryptography.fernet import Fernet
import os
from app.services.process_identity_native import observe
from fastapi.staticfiles import StaticFiles
isolated_vault = Fernet(Fernet.generate_key())
llm._vault = lambda: isolated_vault
(root / 'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')
'''
    (api.root / 'approval_diff_fixture.py').write_text(FIXTURE.replace('if calls>8:', 'if calls>4:') + setup
        + 'app.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n', encoding='utf8')
    result = {'passed': False, 'provider': 'deepseek', 'host': 'api.deepseek.com', 'model': configuration['model'],
              'mock_llm_calls': 0, 'conversations': {}}
    print('LIVE_F08A_EVIDENCE', api.root, flush=True)

    def save():
        log = api.root / 'upstream.jsonl'
        upstream = [json.loads(line) for line in log.read_text(encoding='utf8').splitlines()] if log.exists() else []
        result['upstream'] = upstream
        result['model_calls'] = sum(row['kind'] == 'http' and row['path'].endswith('/chat/completions') for row in upstream)
        assert configuration['key'] not in encode_json(result)
        write_json(api.root / 'live-result.json', result)

    try:
        with api:
            case = api.create_case(CASE)
            base_lines = [f'Tabular record {number}\n' for number in range(6500)]
            base_lines[6200:6202] = OLD_LINES.splitlines(keepends=True)
            before = ''.join(base_lines)
            (api.root / 'long-source.txt').write_bytes(before.encode('utf8'))
            api.put(LONG, before)
            # The file's bytes are benign text. Its extension takes the real
            # ordinary binary-file read path, without sending its bytes upstream.
            api.put(NON_TEXT, NON_TEXT_CONTENT)
            classified = api.call('GET', api.prefix + '/file?' + urlencode({'path': NON_TEXT}))
            assert classified['kind'] == 'binary'
            profile = api.call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'F08A real acceptance',
                'base_url': configuration['base_url'], 'default_model': configuration['model'],
                'api_key': configuration['key']})
            assert configuration['key'] not in encode_json(profile)
            proposal_context = '这些都是本轮隔离验收的无害文本。只调用指定工具一次，等待人工批准。'
            spec = {
                'long': ('propose_text_replacement', LONG, {
                    'old_text': OLD_LINES, 'new_text': UPDATED_LINE}, digest(before)),
                'nontext': ('propose_file_update', NON_TEXT,
                    {'content': 'APPROVAL_ONE_TIME_REJECT' if args.one_time else 'An explicit text replacement.'},
                    digest(NON_TEXT_CONTENT)),
                'new': ('propose_file_update', NEW, {'content': NEW_CONTENT}, None),
            }
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                              trust_env=False, timeout=95) as client:
                for name, (tool, path, arguments, expected) in spec.items():
                    conversation = api.call('POST', '/conversations', {'case_id': case['id'], 'title': 'Diff ' + name,
                        'provider_profile_id': profile['id'], 'model': configuration['model'], 'permission_mode': 'approve_writes'})
                    cid = conversation['id']
                    prompt = (f'{proposal_context} 请实际调用 {tool}，精确参数为 '
                        + encode_json({'path': path, **arguments, 'reason': '实际审批对照验收'})
                        + '。JSON 中的转义换行表示实际换行。收到工具结果后只简短说明结果，不再调用工具。')
                    if name == 'nontext':
                        prompt += '这里只提出普通 ASCII 文本，即使文件名以 .bin 结尾，也请逐字使用指定 content。'
                    events = []
                    try:
                        with client.stream('POST', f'/conversations/{cid}/messages', json={
                            'content': prompt, 'provider_profile_id': profile['id'], 'model': configuration['model'],
                            'reasoning_effort': 'off',
                        }) as response:
                            response.raise_for_status()
                            for line in response.iter_lines():
                                if line.startswith('data:'):
                                    events.extend(frames_from(line))
                                if len(events) > 10000:
                                    raise ValueError('Live stream evidence exceeded its event bound')
                    finally:
                        assert configuration['key'] not in encode_json(events)
                        write_json(api.root / (name + '-events.json'), events)
                        save()
                    pending = [frame['tool_call'] for frame in events if frame['type'] == 'approval.requested']
                    assert len(pending) == 1 and pending[0]['tool'] == tool, (name, pending)
                    actual = pending[0]['args']
                    assert actual['path'] == path and all(actual[key] == val for key, val in arguments.items()), (name, actual)
                    assert 'baseline_sha256' in actual and actual['baseline_sha256'] == expected
                    history = api.call('GET', f'/conversations/{cid}')
                    stored, = history['pending_tools']
                    assert stored['status'] == 'pending' and all(stored[key] == pending[0][key] for key in ('id', 'tool', 'args'))
                    result['conversations'][name] = {'id': cid, 'path': path, 'tool': tool, 'tool_id': pending[0]['id'],
                        'actual_arguments': actual}
                    save()

            temp = api.root / 'browser-tmp'
            temp.mkdir()
            subprocess.run([str(args.browser_python), str(Path(__file__).resolve()), '--browser'],
                input=json.dumps({'root': str(api.root), 'base': api.base, 'case': case['id'],
                                  'token': api.token, 'conversations': result['conversations'], 'one_time': args.one_time}),
                cwd=api.root, env=dict(os.environ, PYTHONIOENCODING='utf8', TEMP=str(temp), TMP=str(temp)),
                encoding='utf8', check=True, timeout=440)
            result['browser'] = json.loads((api.root / 'approval-diff-browser.json').read_text(encoding='utf8'))
            assert result['browser']['passed']
            actual_long = api.call('GET', api.prefix + '/file?' + urlencode({'path': LONG}))['content']
            assert digest(actual_long) == result['browser']['observations']['long']['external_sha256']
            actual_nontext = api.call('GET', api.prefix + '/raw?' + urlencode({'path': NON_TEXT}), raw=True)
            assert actual_nontext == NON_TEXT_CONTENT.encode('utf8')
            created = api.call('GET', api.prefix + '/file?' + urlencode({'path': NEW}))['content']
            assert created == NEW_CONTENT
            for row in result['conversations'].values():
                assert not api.call('GET', f"/conversations/{row['id']}")['pending_tools']
            result['saved_sources'] = {'external_survived': digest(actual_long), 'non_text_unchanged': digest(NON_TEXT_CONTENT),
                                       'created_content': digest(created)}

        identity = json.loads((api.root / 'api-birth.json').read_text(encoding='utf8'))
        deadline = time.monotonic() + 15
        while probe(identity)['state'] != 'exited' and time.monotonic() < deadline:
            time.sleep(.2)
        assert probe(identity)['state'] == 'exited'
        with socket.socket() as connection:
            connection.settimeout(1)
            assert connection.connect_ex(('127.0.0.1', api.port)) != 0
        assert not (api.root / '.vault_key').exists()
        result['cleanup'] = {'api_pid': identity['pid'], 'api_exited': True, 'port_closed': True, 'vault_key_on_disk': False}
        save()
        assert result['model_calls'] == 4 and all(row['status'] == 200 for row in result['upstream'] if row['kind'] == 'http')
        result['passed'] = True
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'detail': str(error).replace(configuration['key'], '[REDACTED]')[:950]}
        raise
    finally:
        save()
    print('LIVE_F08A_PASS', api.root / 'live-result.json', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source-profile-id', type=int, required=True)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--one-time', action='store_true', help='Exercise FF-F29 decision transport loss on the real proposal cards.')
        args = parser.parse_args()
        verify(args, asyncio.run(source(args.source_profile_id)))
