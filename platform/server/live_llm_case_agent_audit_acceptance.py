"""One real DeepSeek Agent turn, approved caseDict write, and committed case audit event."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode
import uuid

import httpx

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer


def verify(configuration):
    api = IsolatedServer('live-cli-startup', app='live_fixture:app')
    original_limit = "if calls>8:raise RuntimeError('Live acceptance exceeded its eight-request limit')"
    assert FIXTURE.count(original_limit) == 1
    fixture = FIXTURE.replace(original_limit,
        "if calls>2:raise RuntimeError('Case audit acceptance exceeded the two-request limit')")
    (api.root / 'live_fixture.py').write_text(fixture, encoding='utf8')
    print('LIVE_CASE_AGENT_AUDIT_EVIDENCE ' + str(api.root), flush=True)
    record = {'passed': False, 'provider': 'deepseek', 'model': configuration['model'],
              'source_profile_id': 26, 'mock_llm_calls': 0, 'http_request_limit': 2}
    secret = configuration['key']
    trace = api.root / 'upstream.jsonl'

    def save(name, data):
        output = json.dumps(data, ensure_ascii=False, indent=2)
        if secret in output:
            raise ValueError('Model credential reached acceptance evidence')
        (api.root / name).write_text(output, encoding='utf8')

    def upstream():
        return [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []

    def calls():
        return [event for event in upstream() if event['kind'] == 'http' and event['path'].endswith('/chat/completions')]

    try:
        with api:
            case = api.create_case('Isolated real Agent caseDict audit')
            case_dict = api.prefix + '/caseDict'
            events_url = api.prefix + '/events'
            audit_raw = api.prefix + '/raw?' + urlencode({'path': '0-caseDict/case-events.jsonl'})
            old = 'VF_A22_START_' + uuid.uuid4().hex
            replacement = 'VF_A22_CONFIRMED_' + uuid.uuid4().hex
            initial = api.call('GET', case_dict)
            seeded = initial['content'].replace('    // userIntent        ;', f'    userIntent {old};', 1)
            assert seeded.count(old) == 1
            api.call('PUT', case_dict, {'content': seeded, 'expected_revision': initial['dict_revision'],
                'expected_sha256': initial['content_sha256']})
            before = api.call('GET', case_dict)
            original_events = api.call('GET', events_url)
            original_raw = api.call('GET', audit_raw, raw=True)
            assert original_events['events'] == list(reversed([json.loads(line) for line in original_raw.splitlines()]))
            assert original_events['total'] == 2

            def preserved(old_dict, event_page, raw_bytes):
                assert api.call('GET', case_dict) == old_dict
                assert api.call('GET', events_url) == event_page
                assert api.call('GET', audit_raw, raw=True) == raw_bytes

            profile = api.call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'Case audit live approval',
                'base_url': configuration['base_url'], 'default_model': configuration['model'], 'api_key': secret})
            assert secret not in json.dumps(profile)
            model_settings = {'provider_profile_id': profile['id'], 'model': configuration['model']}
            interaction = api.call('POST', '/conversations', {'case_id': case['id'], 'title': 'Real case audit tool',
                'permission_mode': 'approve_writes', **model_settings})
            route = '/conversations/' + str(interaction['id'])
            prompt = (f'这是经授权的隔离算例。请只调用一次 propose_text_replacement，把文件 0-caseDict/caseDict 中唯一的 '
                      f'{old} 精确替换为 {replacement}。只改这一处 userIntent，不改阶段，不使用其它工具。'
                      '等待批准。收到成功工具结果后只回复“已完成”，不要再次发起操作。')
            client_headers = {'Authorization': 'Bearer ' + api.token}
            with httpx.Client(base_url=api.base + '/api/v1', headers=client_headers, timeout=100, trust_env=False) as client:
                def stream(path, body, label):
                    frames = []
                    deadline = time.monotonic() + 110
                    with client.stream('POST', path, json=body) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if time.monotonic() > deadline or len(frames) >= 512:
                                raise TimeoutError('Bounded case audit SSE exceeded its test budget')
                            if line.startswith('data:'):
                                frames.append(json.loads(line[5:]))
                    save(label + '.json', frames)
                    assert not any(frame['type'] in {'stream.error', 'request.failed', 'request.timed_out'} for frame in frames)
                    return frames

                proposed = stream(route + '/messages', {'content': prompt, **model_settings}, 'model-proposal-events')
                requested = [frame['tool_call'] for frame in proposed if frame['type'] == 'approval.requested']
                assert len(requested) == 1 and not any(frame['type'] == 'tool.result' for frame in proposed)
                proposal = requested[0]
                assert proposal['tool'] == 'propose_text_replacement'
                arguments = proposal['args']
                assert (arguments['path'], arguments['old_text'], arguments['new_text']) == ('0-caseDict/caseDict', old, replacement)
                assert arguments['baseline_sha256'] == before['content_sha256']
                assert len(calls()) == 1 and calls()[0]['status'] == 200
                preserved(before, original_events, original_raw)
                record['pending_proposal_has_no_case_event'] = True

                approval = route + '/tool-calls/' + proposal['id'] + ':approve'
                foreign = api.call('POST', '/auth/register', {'email': uuid.uuid4().hex + '@example.com',
                    'name': 'Isolated unauthorized account', 'password': 'isolated-approval-123'})
                unauthorized = {'Authorization': 'Bearer ' + foreign['token']}
                api.call('POST', approval, {}, expect=404, extra_headers=unauthorized)
                api.call('GET', events_url, expect=404, extra_headers=unauthorized)
                preserved(before, original_events, original_raw)
                assert len(calls()) == 1
                record['unauthorized_approval_404_no_revision_event_or_model'] = True

                applied = stream(approval, {}, 'model-approval-events')
                executed = [frame for frame in applied if frame['type'] == 'tool.result']
                assert len(executed) == 1 and executed[0]['ok'] and executed[0]['approved']
                assert executed[0]['tool'] == 'propose_text_replacement'
                assert any(frame['type'] == 'run.completed' for frame in applied)
                history = api.call('GET', route)
                assert any(row['type'] == 'tool_result' and row['payload'].get('approved') and row['payload'].get('ok')
                    and row['payload']['tool_call_id'] == executed[0]['tool_call_id'] for row in history['events'])

            latest = api.call('GET', case_dict)
            amended_events = api.call('GET', events_url)
            amended_raw = api.call('GET', audit_raw, raw=True)
            assert latest['content'] == before['content'].replace(old, replacement, 1)
            assert latest['parsed']['intent']['userIntent'] == replacement and latest['workflow_state'] == 'A'
            assert latest['dict_revision'] == before['dict_revision'] + 1
            assert amended_raw.startswith(original_raw) and amended_events['total'] == original_events['total'] + 1
            committed = amended_events['events'][0]
            assert amended_events['events'][1:] == original_events['events']
            assert amended_events['events'] == list(reversed([json.loads(line) for line in amended_raw.splitlines()]))
            assert committed['type'] == 'caseDict.updated' and committed['source'] == 'agent'
            assert committed['case_id'] == case['id'] and committed['revision'] == latest['dict_revision']
            assert committed['previous_state'] == committed['workflow_state'] == 'A'
            assert committed['schema'] == 'vibeflow.case-event.v1' and committed['event_id'] and committed['created_at']
            provenance = api.call('GET', api.prefix + '/provenance')
            assert provenance['chain_valid'] and provenance['events'][0]['source'] == 'agent'
            assert provenance['events'][0]['casedict_sha256'] == latest['content_sha256']
            page = api.call('GET', events_url + '?limit=1&offset=0')
            assert page['events'] == [committed] and page['has_more'] is True
            api.call('POST', approval, {}, expect=409)
            preserved(latest, amended_events, amended_raw)
            assert len(calls()) == 2 and all(event['status'] == 200 for event in calls())
            assert len([event for event in upstream() if event['kind'] == 'stream_identity' and event.get('model')]) == 2
            (api.root / 'case-events.jsonl').write_bytes(amended_raw)
            save('case-events-api.json', amended_events)
            record.update(conversation_id=interaction['id'], approved_tool=executed[0]['tool'],
                provider_tool_call_id=executed[0]['tool_call_id'], committed_audit_event=committed,
                case_event_bytes_sha256=hashlib.sha256(amended_raw).hexdigest(),
                denied_repeat_409_no_duplicate=True, raw_and_paginated_api_match=True)
        record['passed'] = api.proc.poll() is not None
        print('LIVE_CASE_AGENT_AUDIT_OK ' + str(api.root), flush=True)
    except Exception as error:
        record['failure'] = {'type': type(error).__name__, 'detail': str(error).replace(secret, '[REDACTED]')[:1600]}
        raise RuntimeError('Actual Agent case audit acceptance failed. Inspect ' + str(api.root)) from None
    finally:
        record['api_exited'] = api.proc is not None and api.proc.poll() is not None
        record['real_model_requests'] = len(calls())
        save('case-agent-audit.json', record)
        save('live-result.json', {'passed': record['passed'], 'provider': 'deepseek', 'model': configuration['model'],
            'real_model_requests': len(calls()), 'mock_llm_calls': 0, 'upstream': upstream()})
        if record['api_exited']:
            (api.root / '.vault_key').unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, choices=[26], required=True)
    args = parser.parse_args()
    verify(asyncio.run(source(args.source_profile_id)))
