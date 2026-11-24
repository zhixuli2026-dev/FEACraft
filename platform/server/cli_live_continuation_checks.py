"""Actual API restart, native fork/resume and real model continuity; no doubles."""
import json
import shutil
import time
import uuid

import httpx


def verify(api, call, profile, conversation, source_turn, nonce, report, save):
    report['continuations'] = {'api_restart_verified': False, 'restart_stage': 'reading_account', 'turns': []}
    save()
    email = api.call('GET', '/auth/me')['email']
    old_process = api.proc
    report['continuations']['restart_stage'] = 'stopping_api'; save()
    api.__exit__(None, None, None)
    assert old_process.poll() is not None
    report['continuations']['restart_stage'] = 'preserving_log'; save()
    shutil.copyfile(api.root / 'api.log', api.root / 'api-before-continuation-restart.log')
    report['continuations']['restart_stage'] = 'starting_api'; save()
    api.__enter__()
    report['continuations']['restart_stage'] = 'reauthenticating'; save()
    api.token = api.call('POST', '/auth/login', {'email': email, 'password': 'isolated-test-123'})['token']
    resumed_source = call('GET', '/codex/turns/' + source_turn['id'])
    assert resumed_source['phase'] == 'completed' and resumed_source['cleanup_verified']
    report['continuations'].update(api_restart_verified=True, old_api_exit_code=old_process.returncode, restart_stage='verified')
    save()
    fork = None
    try:
        request = {'source_turn_id': source_turn['id'], 'client_request_id': uuid.uuid4().hex}
        fork = call('POST', f'/codex/conversations/{conversation["id"]}/fork', request)
        report['continuations']['fork'] = fork; save()
        deadline = time.monotonic() + 90
        while fork['phase'] not in {'completed', 'failed', 'recovery_required', 'evidence_invalid'}:
            if time.monotonic() > deadline:raise TimeoutError('Actual native fork did not settle')
            time.sleep(.3); fork = call('GET', '/codex/forks/' + fork['id'])
        report['continuations']['fork'] = fork; save()
        assert fork['phase'] == 'completed' and fork['cleanup_verified'], fork
        assert fork['thread_id'] != source_turn['thread_id']
        duplicate = call('POST', f'/codex/conversations/{conversation["id"]}/fork', request)
        assert duplicate['id'] == fork['id'] and duplicate['thread_id'] == fork['thread_id']
        for label, cid, thread_id in [('branch', fork['conversation_id'], fork['thread_id']),
                                      ('original', conversation['id'], source_turn['thread_id'])]:
            session = None; turn = None
            item = {'label': label, 'conversation_id': cid, 'expected_thread_id': thread_id}
            report['continuations']['turns'].append(item); save()
            try:
                session = call('POST', '/codex/sessions', {'case_id': api.case_id, 'conversation_id': cid,
                    'provider_profile_id': profile['id'], 'provider_wire_api': 'responses', 'runtime': report['runtime']})
                item['session_id'] = session['id']; save()
                body = {'content': '不要调用工具。请只回复你此前从文件中读取的完整验证字符串，不添加其他文字。',
                        'client_message_id': uuid.uuid4().hex, 'resume': True, 'timeout_seconds': 120}
                assert nonce not in body['content']
                turn = call('POST', '/codex/sessions/' + session['id'] + '/turns', body)
                report['actual_CLI_turns_started'] += 1; item['turn_id'] = turn['id']; save()
                deadline = time.monotonic() + 180
                while turn['phase'] not in {'completed', 'failed', 'cancelled', 'interrupted', 'recovery_required', 'evidence_invalid'}:
                    if time.monotonic() > deadline:raise TimeoutError('Actual resumed model turn did not settle')
                    assert turn['phase'] != 'awaiting_input', 'Text-only continuation unexpectedly requested a tool'
                    time.sleep(.3); turn = call('GET', '/codex/turns/' + turn['id'])
                item['turn'] = turn; save()
                assert turn['phase'] == turn['native_status'] == 'completed' and turn['cleanup_verified'], turn
                assert turn['thread_id'] == thread_id
                with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                                  trust_env=False, timeout=30) as client:
                    response = client.get('/codex/turns/' + turn['id'] + '/events'); response.raise_for_status()
                    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
                assert not any(e['type'] == 'context.handoff' for e in events), 'Resume fell back to text handoff'
                assert not any(e['type'] == 'item.completed' and e.get('payload', {}).get('type') == 'commandExecution' for e in events)
                answer = ''.join(e.get('payload', {}).get('text', '') for e in events if e['type'] == 'text.delta')
                assert answer == nonce, answer
                history = api.call('GET', '/conversations/' + str(cid))
                assert any(e['type'] == 'message' and e['payload'].get('native_execution_id') == turn['id']
                           and e['payload'].get('content') == nonce for e in history['events'])
                replay = call('POST', '/codex/sessions/' + session['id'] + '/turns', body)
                assert replay['id'] == turn['id'] and replay['native_turn_id'] == turn['native_turn_id']
                activity = call('GET', '/codex/conversations/' + str(cid) + '/activity')
                assert activity['phase'] == 'idle', activity
                item.update(passed=True, strict_answer=True, persisted_answer=True, native_thread_preserved=True,
                            no_text_handoff=True, no_additional_tools=True, replay_same_turn=True, execution_idle=True)
                save()
            finally:
                if turn is not None and turn['phase'] not in {'completed', 'failed', 'cancelled', 'interrupted'}:
                    item['cancel'] = call('POST', '/codex/turns/' + turn['id'] + ':cancel')
                if session is not None:
                    item['session_final'] = call('DELETE', '/codex/sessions/' + session['id'])
                    assert item['session_final']['cleanup_verified']
                save()
        report['continuations']['passed'] = True; save()
    finally:
        if fork is not None and fork['phase'] not in {'completed', 'failed', 'recovery_required'}:
            report['continuations']['fork_cancel'] = call('POST', '/codex/forks/' + fork['id'] + ':cancel')
            save()
