"""Verify installed CLI budgets and recovery with authorized DeepSeek inference."""
import argparse
import asyncio
import json
import shutil
import sqlite3
import time
import uuid

import httpx

from cli_managed_session_checks import FIXTURE as MANAGED
from live_llm_acceptance import FIXTURE as LIVE, source
from testsupport import IsolatedServer


FIXTURE = r'''
import base64
from app.services.owned_stdio import OwnedStdio

MODEL_DELTAS = {'item/agentMessage/delta', 'item/reasoning/textDelta', 'item/reasoning/summaryTextDelta'}
original_receive = OwnedStdio._receive
async def observe_pipe(self):
    value = await original_receive(self)
    runtime = self.runtime_budget
    kind = value.get('type')
    if runtime is not None and self.max_output_bytes and self.close_operation is None and kind in {'stdout', 'stderr'}:
        path = _base / ('pipe-' + self.owned.nonce + '.json')
        if not hasattr(self, '_test_budget_sizes'):
            self._test_budget_sizes = {'stdout': 0, 'stderr': 0, 'frames': 0, 'model_delta_events': {}}
            self._test_budget_stdout = bytearray()
        sizes = self._test_budget_sizes
        data = base64.b64decode(value['data'], validate=True)
        sizes[kind] += len(data)
        sizes['frames'] += 1
        if kind == 'stdout':
            pending = self._test_budget_stdout
            pending.extend(data)
            while (end := pending.find(b'\n')) >= 0:
                message = json.loads(pending[:end])
                del pending[:end + 1]
                method = message.get('method')
                if method in MODEL_DELTAS and message.get('params', {}).get('delta'):
                    counts = sizes['model_delta_events']
                    counts[method] = counts.get(method, 0) + 1
        path.write_text(json.dumps(sizes), encoding='utf8')
    return value
OwnedStdio._receive = observe_pipe

@app.put('/api/v1/test/cli-session-budget')
async def set_cli_session_budget(body: dict, user=Depends(get_current_user)):
    if body.get('native'):
        await native_root()
    settings = get_settings()
    settings.codex_session_lifetime_seconds = body['seconds']
    settings.codex_session_max_output_bytes = body['output_bytes']
    return {'ready': True}
'''


def wait_for(read, settled, *, seconds=90):
    deadline = time.monotonic() + seconds
    while True:
        record = read()
        if settled(record):
            return record
        if time.monotonic() >= deadline:
            raise TimeoutError('Native record did not settle: ' + str(record.get('phase')))
        time.sleep(.25)


async def probe_exits(root):
    from app.services.owned_process import probe_native
    from app.services.process_identity_native import probe

    identities = {}

    def collect(value):
        if isinstance(value, dict):
            if value.get('schema') == 'vibeflow.os-process.v1':
                identities[json.dumps(value, sort_keys=True)] = value
            else:
                for child in value.values():
                    collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    with sqlite3.connect(root / 'test.sqlite') as db:
        for process, stop in db.execute('SELECT process_json, stop_json FROM native_cli_sessions'):
            collect(json.loads(process))
            collect(json.loads(stop))
    observations = []
    for expected in identities.values():
        observed = probe(expected) if expected['platform'] == 'windows' else await probe_native('vibeflow-linux', expected)
        observations.append({'platform': expected['platform'], 'pid': expected['pid'], 'state': observed['state']})
    return observations


def verify(configuration):
    api = IsolatedServer('live-cli-session-budget', app='live_fixture:app')
    (api.root / 'live_fixture.py').write_text(LIVE + MANAGED + FIXTURE, encoding='utf8')
    report = {'passed': False, 'provider': 'deepseek', 'host': 'api.deepseek.com', 'model': configuration['model'],
              'protocol': 'responses', 'mock_llm_calls': 0, 'actual_model_streams_observed': 0, 'checks': []}
    final_sessions = []
    print('LIVE_CLI_SESSION_BUDGET ' + str(api.root), flush=True)

    def save():
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        assert configuration['key'] not in encoded
        (api.root / 'live-result.json').write_text(encoded + '\n', encoding='utf8')

    def call(method, path, body=None, *, status=200, as_text=False):
        with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                          trust_env=False, timeout=100) as client:
            response = client.request(method, path, json=body)
        assert response.status_code == status, (method, path, response.status_code,
            response.text.replace(configuration['key'], '[REDACTED]')[:700])
        return response.text if as_text else response.json()

    def set_budget(*, seconds=0, output_bytes=0, native=False):
        call('PUT', '/test/cli-session-budget', {'seconds': seconds, 'output_bytes': output_bytes, 'native': native})

    def get_session(identifier):
        return call('GET', '/codex/sessions/' + identifier)

    def open_session(runtime, conversation, *, status=200):
        return call('POST', '/codex/sessions', {'case_id': api.case_id, 'conversation_id': conversation['id'],
            'provider_profile_id': profile['id'], 'provider_wire_api': 'responses', 'runtime': runtime}, status=status)

    def receipt(session, error=None, reason=None):
        assert session['phase'] == ('failed' if error else 'closed'), (session['phase'], session['error'])
        assert session['error'] == error and session['cleanup_verified'] is True, (session['error'], session['cleanup_verified'])
        stop = session['stop']
        assert stop['verified'] and stop['scope_empty'], (stop['verified'], stop['scope_empty'])
        if reason is not None:
            assert stop['reason'] == reason, stop['reason']
        final_sessions.append(session)
        return {'id': session['id'], 'runtime': session['runtime'], 'reason': stop['reason'],
                'cleanup_verified': session['cleanup_verified']}

    def pipe_sizes(session):
        path = api.root / ('pipe-' + session['stop']['launch_nonce'] + '.json')
        return json.loads(path.read_text(encoding='utf8'))

    def run_turn(session, prompt):
        turn = call('POST', '/codex/sessions/' + session['id'] + '/turns',
                    {'content': prompt, 'client_message_id': uuid.uuid4().hex, 'timeout_seconds': 120})
        report.setdefault('submitted_turns', []).append(turn['id'])
        save()
        path = '/codex/turns/' + turn['id']
        turn = wait_for(lambda: call('GET', path), lambda value: value['phase'] in {
            'completed', 'failed', 'cancelled', 'interrupted', 'recovery_required', 'evidence_invalid'}, seconds=150)
        raw = call('GET', path + '/events', as_text=True)
        assert configuration['key'] not in raw
        (api.root / (turn['id'] + '.sse')).write_text(raw, encoding='utf8')
        events = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith('data:')]
        answer = ''.join(event['payload']['text'] for event in events if event['type'] == 'text.delta')
        if answer:
            report['actual_model_streams_observed'] += 1
        report.setdefault('turn_observations', []).append({'id': turn['id'], 'phase': turn['phase'],
            'error': turn['error'], 'native_status': turn['native_status'], 'observed_text_bytes': len(answer.encode('utf8'))})
        save()
        return turn, answer

    try:
        with api:
            api.create_case('Native Codex session runtime budgets')
            email = api.call('GET', '/auth/me')['email']
            profile = call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'Native budget inference',
                'base_url': configuration['base_url'], 'default_model': configuration['model'], 'api_key': configuration['key']})
            conversation = call('POST', '/conversations', {'case_id': api.case_id, 'title': 'Native budget inference',
                'permission_mode': 'ask', 'provider_profile_id': profile['id'], 'model': configuration['model']})
            for runtime in ('windows', 'wsl'):
                existing = {item['id'] for item in call('GET', '/codex/sessions')}
                set_budget(output_bytes=64, native=runtime == 'wsl')
                refused = open_session(runtime, conversation, status=409)
                assert refused['detail']['code'] == 'cli_session_log_limit', refused
                rows = [item for item in call('GET', '/codex/sessions') if item['id'] not in existing]
                assert len(rows) == 1
                check = receipt(rows[0], 'CliSessionOutputLimit', 'log_limit')
                sizes = pipe_sizes(rows[0])
                assert sizes['stdout'] + sizes['stderr'] > 64
                report['checks'].append(dict(check, check='startup_output_refusal', pipe_sizes=sizes))
                save()

                set_budget(seconds=12)
                session = open_session(runtime, conversation)
                assert session['phase'] == 'active'
                # A settings change does not extend an already running process.
                set_budget()
                ended = wait_for(lambda: get_session(session['id']), lambda value: value['phase'] != 'active', seconds=30)
                check = receipt(ended, 'CliSessionTimeout', 'timeout')
                report['checks'].append(dict(check, check='idle_lifetime_after_settings_change', limit_seconds=12))
                save()

            output_limit = 64 * 1024
            set_budget(output_bytes=output_limit)
            session = open_session('wsl', conversation)
            turn, answer = run_turn(session, 'Do not call tools. Stream 110 numbered lines of English prose about different '
                'weather observations. Each line must contain 20 to 26 words. Begin immediately with line 1.')
            assert turn['phase'] == 'failed' and turn['error'] == 'CliSessionOutputLimit', report['turn_observations']
            ended = get_session(session['id'])
            check = receipt(ended, 'CliSessionOutputLimit', 'log_limit')
            sizes = pipe_sizes(ended)
            assert sizes['stdout'] > 0 and sizes['stdout'] + sizes['stderr'] > output_limit and sizes['frames'] > 1
            assert sizes['model_delta_events'], 'CLI pipe must contain actual model deltas before the stop'
            if not answer:
                report['actual_model_streams_observed'] += 1
            history = call('GET', '/conversations/' + str(conversation['id']))['events']
            assert not any(event['type'] == 'message' and event['payload'].get('role') == 'assistant' for event in history)
            report['checks'].append(dict(check, check='actual_model_stream_stopped', pipe_sizes=sizes, limit_bytes=output_limit))
            save()

            set_budget()
            session = open_session('wsl', conversation)
            nonce = 'BUDGET_RECOVERY_' + uuid.uuid4().hex
            turn, answer = run_turn(session, 'Do not call tools. Reply with this exact string only: ' + nonce)
            assert turn['phase'] == turn['native_status'] == 'completed' and turn['cleanup_verified']
            assert answer == nonce
            history = call('GET', '/conversations/' + str(conversation['id']))['events']
            assert any(event['type'] == 'message' and event['payload'].get('role') == 'assistant'
                       and event['payload'].get('content') == nonce for event in history)
            report['checks'].append(dict(receipt(get_session(session['id'])), check='next_actual_model_turn_delivered'))
            save()

            initial_api = api.proc
            api.__exit__(None, None, None)
            assert initial_api.poll() is not None
            shutil.copyfile(api.root / 'api.log', api.root / 'api-before-restart.log')
            api.__enter__()
            api.token = api.call('POST', '/auth/login', {'email': email, 'password': 'isolated-test-123'})['token']
            for expected in final_sessions:
                actual = get_session(expected['id'])
                assert actual['stop'] == expected['stop'] and actual['phase'] == expected['phase']
                assert actual['error'] == expected['error'] and actual['cleanup_verified'] is True
            report['cold_receipts_verified'] = len(final_sessions)
            report['api_restarted'] = True
        assert api.proc.poll() is not None
        report['process_exit_observations'] = asyncio.run(probe_exits(api.root))
        assert {entry['platform'] for entry in report['process_exit_observations']} == {'windows', 'linux'}
        assert all(entry['state'] == 'exited' for entry in report['process_exit_observations'])
        report['api_exited'] = True
        report['passed'] = True
        return report
    finally:
        save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    args = parser.parse_args()
    result = verify(asyncio.run(source(args.source_profile_id)))
    print(json.dumps({'passed': result['passed'], 'checks': len(result['checks']),
                      'actual_model_streams_observed': result['actual_model_streams_observed']}))
