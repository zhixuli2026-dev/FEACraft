"""Actual text handoff API → Codex Windows → Codex WSL → API."""

import argparse
import asyncio
import json
from pathlib import Path
import secrets
import shutil
import time
import uuid

from live_llm_acceptance import source
from live_llm_native_reply_sound_acceptance import ResponsesBudget, linux_relay, windows_relay
from live_llm_shared_index_acceptance import SharedIndexServer, configure


RESTRAINED = '''
from app.services import codex_sessions
def restrained(argv):
    return [argv[0], '--disable', 'multi_agent', '--disable', 'multi_agent_v2',
            '-c', 'web_search="disabled"', *argv[1:]]
windows_command = codex_sessions.command
wsl_command = codex_sessions.native_command
codex_sessions.command = lambda credential=None: restrained(windows_command(credential))
def native_command(credential=None, *, distro=None):
    program, argv = wsl_command(credential, distro=distro)
    return program, restrained(argv)
codex_sessions.native_command = native_command
'''


def verify(config, exe):
    api = SharedIndexServer('live-f16-environment-handoff', app='live_fixture:app')
    budget = ResponsesBudget(config, api.root, per_runtime={'windows': 2, 'wsl': 1})
    token = secrets.token_hex(32)
    report = {'passed': False, 'model': config['model'], 'mock_llm_calls': 0, 'turns': []}
    print('LIVE_F16_EVIDENCE', api.root, flush=True)

    def json_frames(raw):
        return [json.loads(line[5:]) for line in raw.decode('utf8').splitlines() if line.startswith('data:')]

    def api_turn(conversation, profile, prompt, expected):
        url = f'/conversations/{conversation}/messages'
        frames = json_frames(api.call('POST', url, {'content': prompt, 'provider_profile_id': profile}, raw=True))
        assert any(frame['type']=='run.completed' for frame in frames)
        answer = ''.join(frame['text'] for frame in frames if frame['type']=='text.delta').strip()
        assert answer==expected, answer
        return latest_answer(conversation, expected)

    def latest_answer(conversation, expected):
        rows = api.call('GET', f'/conversations/{conversation}')['events']
        answer = next(row for row in reversed(rows) if row['type']=='message' and row['payload'].get('role')=='assistant')
        assert answer['payload']['content'].strip()==expected
        return answer

    def handoff(conversation, title, event=None):
        route = f'/conversations/{conversation}/handoff'
        body = {'title': title}
        if event is not None:
            body['head_event_id'] = event['id']
        new = api.call('POST', route, body)
        row = api.call('GET', f'/conversations/{new["id"]}')['events'][0]
        assert row['payload']['source_conversation_id']==conversation
        assert row['payload']['kind']=='handoff' and new['case_id']==api.case_id
        return new['id'], row['payload']['text']

    def native_turn(conversation, runtime, profile, prompt, *, resume=False, fail=False):
        session = api.call('POST', '/codex/sessions', {'case_id': api.case_id, 'conversation_id': conversation,
                           'provider_profile_id': profile, 'provider_wire_api': 'responses', 'runtime': runtime})
        turn = api.call('POST', f'/codex/sessions/{session["id"]}/turns', {
            'content': prompt, 'client_message_id': uuid.uuid4().hex, 'resume': resume, 'timeout_seconds': 125})
        route = f'/codex/turns/{turn["id"]}'
        deadline = time.monotonic() + 165
        while turn['phase'] not in {'completed','failed','cancelled','interrupted','recovery_required','evidence_invalid'}:
            assert time.monotonic()<deadline and turn['phase']!='awaiting_input', turn['phase']
            time.sleep(.25)
            turn = api.call('GET', route)
        assert turn['cleanup_verified'] is True, turn
        assert api.call('GET', f'/codex/conversations/{conversation}/activity')['phase']=='idle'
        frames = json_frames(api.call('GET', route+'/events', raw=True))
        (api.root / f'{turn["id"]}-events.json').write_text(json.dumps(frames, ensure_ascii=False), encoding='utf8')
        report['turns'].append({'runtime': runtime, 'phase': turn['phase'], 'id': turn['id'],
                                'thread_id': turn['thread_id'], 'native_turn_id': turn['native_turn_id'],
                                'resume': resume, 'scope_empty': True})
        if fail:
            assert turn['phase']=='failed' and not any(frame['type']=='text.delta' for frame in frames), turn
            return turn, None
        assert turn['phase']=='completed' and turn['native_turn_id'], turn
        assert any(frame['type']=='context.handoff' for frame in frames) is (not resume)
        assert not any(frame['type']=='approval.requested' for frame in frames)
        return turn, ''.join(frame['payload']['text'] for frame in frames if frame['type']=='text.delta').strip()

    try:
        with windows_relay(budget, token) as windows, linux_relay(budget, token) as linux:
            for runtime in ('windows','wsl'):
                (api.root / ('release-' + runtime)).touch()
            configure(api, {'VIBEFLOW_CODEX': str(Path(shutil.which('codex.exe')).resolve(strict=True)),
                            'VIBEFLOW_CODEX_WSL_PATH': str(exe.resolve(strict=True)),
                            'VIBEFLOW_CODEX_WSL_STATE_ROOT': linux['private_root']}, False)
            with (api.root / 'live_fixture.py').open('a', encoding='utf8') as stream:
                stream.write('\nlive_request_limit=2\n' + RESTRAINED)
            with api:
                api.create_case('Real cross-backend handoff')
                real = api.call('POST', '/provider-profiles', {'name': 'API DeepSeek', 'provider': 'deepseek',
                                'base_url': config['base_url'], 'default_model': config['model'], 'api_key': config['key']})['id']
                profiles = {runtime: api.call('POST', '/provider-profiles', {'name': runtime, 'provider': 'custom',
                            'base_url': f'http://127.0.0.1:{port}', 'default_model': config['model'], 'api_key': token})['id']
                            for runtime,port in (('windows',windows),('wsl',linux['port']))}
                original = api.call('POST', '/conversations', {'case_id': api.case_id, 'title': 'API source',
                                    'model': config['model'], 'provider_profile_id': real, 'permission_mode': 'ask'})['id']
                initial = 'VFSTART_' + uuid.uuid4().hex[:12]
                added = 'VFWINDOWS_' + uuid.uuid4().hex[:12]
                expected = initial + '|' + added
                answer = api_turn(original, real, 'Reply exactly ' + initial + '. Do not call tools.', initial)
                win, text = handoff(original, 'Windows text', answer)
                assert initial in text
                turn, actual = native_turn(win, 'windows', profiles['windows'],
                    'Find the prior assistant verification string. Reply with that string, then | then '+added+'. Do not use tools.')
                assert actual==expected, actual
                reused, actual = native_turn(win, 'windows', profiles['windows'],
                    'Repeat the last assistant response verbatim. Do not use tools.', resume=True)
                assert actual==expected and reused['thread_id']==turn['thread_id']
                assert reused['native_turn_id']!=turn['native_turn_id']
                report['same_runtime_latest_turn_verified'] = True

                wsl, text = handoff(win, 'WSL text')
                assert initial in text and added in text, 'Later Windows turns missing from legacy handoff'
                native_turn(win, 'wsl', profiles['wsl'], 'Do not run a tool.', resume=True, fail=True)
                before_wsl = list(budget.requests)
                assert before_wsl==['windows','windows'], before_wsl
                independent, actual = native_turn(wsl, 'wsl', profiles['wsl'],
                    'From the earlier messages, repeat the last pair of verification strings joined by |. Do not use tools.')
                assert actual==expected and independent['thread_id']!=turn['thread_id']
                closing, text = handoff(wsl, 'API closing', latest_answer(wsl, expected))
                assert expected in text
                api_turn(closing, real, 'Repeat the two verification strings already in the handoff separated by |. Do not use tools.', expected)
                outsider = api.call('POST', '/auth/register', {'email': 'handoff-other@example.com',
                                    'password': 'isolated-test-123', 'name': 'Foreign handoff reader'})
                api.call('POST', f'/conversations/{wsl}/handoff', {}, expect=404,
                         extra_headers={'Authorization': 'Bearer ' + outsider['token']})
                report.update(cross_runtime_resume_denied=True, independent_native_thread=True,
                              latest_bounded_text_preserved=True, acl_verified=True)
        rows = [json.loads(line) for line in (api.root / 'upstream.jsonl').read_text(encoding='utf8').splitlines()]
        assert sum(row['kind']=='http' and row['status']==200 for row in rows)==2
        assert len(budget.receipts)==3 and all(row['status']==200 for row in budget.receipts)
        report.update(passed=True, api_requests=rows)
    finally:
        report['responses_requests'] = budget.receipts
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        assert config['key'] not in encoded and token not in encoded
        (api.root / 'handoff-result.json').write_text(encoded, encoding='utf8')


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--wsl-codex', type=Path, required=True)
    args = parser.parse_args()
    verify(asyncio.run(source(args.source_profile_id)), args.wsl_codex)
