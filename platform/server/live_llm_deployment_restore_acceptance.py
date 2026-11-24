"""Verify a restored PG/S3 deployment, including old encrypted DeepSeek credentials.

Does not import/replace model providers or generate responses. Reuses an isolated
account/profile that existed before the snapshot; never reads the model API key.
"""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return json.loads(path.read_text('utf8'))


def run(base_url, previous):
    folder = ROOT / 'tmp' / ('live-deployment-restore-' + secrets.token_hex(6))
    folder.mkdir()
    result = {'passed': False, 'mock_llm_calls': 0, 'provider': 'deepseek',
              'model': 'deepseek-flash', 'checks': [], 'evidence': str(folder),
              'credential_source': 'pre-snapshot encrypted provider profile; no fresh key import'}
    session = None

    def save(name, value):
        (folder / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf8')

    print('RESTORE_LIVE_EVIDENCE ' + str(folder), flush=True)
    with httpx.Client(base_url=base_url + '/api/v1', trust_env=False, timeout=180) as client:
        def call(method, path, **kwargs):
            until = time.monotonic() + 45
            while True:
                response = client.request(method, path, **kwargs)
                if response.status_code == 409:
                    detail = response.json().get('detail')
                    if isinstance(detail, dict) and detail.get('code') == 'storage_quota_busy' and time.monotonic() < until:
                        time.sleep(.3)
                        continue
                # Do not print response bodies from credential/session errors.
                assert response.status_code in (200, 201, 202), (method, path, response.status_code)
                return response.json()

        try:
            account = read(previous / 'private/account.json')
            login = call('POST', '/auth/login', json={k: account[k] for k in ('email', 'password')})
            client.headers['Authorization'] = 'Bearer ' + login['token']
            old = read(previous / 'result.json')
            prior_conversation = read(previous / 'conversation.json')
            history = call('GET', '/conversations/' + str(old['conversation_id']))
            assert any(e['type'] == 'message' and e['payload'].get('role') == 'assistant'
                       and old['answer'] in e['payload'].get('content', '') for e in history['events'])
            response = client.get('/cases/' + str(old['case_id']) + '/raw', params={'path': 'Ref/proof.txt'})
            response.raise_for_status()
            assert response.content == (old['answer'] + '\n').encode()
            result['checks'].append('original_account_conversation_and_S3_file_restored')

            # Publication IDs and immutable PDF bytes must survive the joint restore.
            latex_account = read(ROOT / 'tmp/linux-tex-20261118/linux-4f556736de/private/account.json')
            tex_login = call('POST', '/auth/login', json={k: latex_account[k] for k in ('email', 'password')})
            expected = read(ROOT / 'tmp/linux-tex-20261118/deployment-result.json')['PDF_hashes_after_recreate']
            result['restored_PDF_hashes'] = {}
            for name in ('linux-4f556736de', 'linux-8c3eddcd08', 'linux-262f07703f'):
                prior = read(ROOT / 'tmp/linux-tex-20261118' / name / 'result.json')
                for recipe, record in prior['recipes'].items():
                    response = client.get('/cases/' + str(prior['cases'][0]) + '/latex/artifact',
                        params={'publication_id': record['publication_id'], 'path': record['pdf']},
                        headers={'Authorization': 'Bearer ' + tex_login['token']})
                    response.raise_for_status()
                    digest = hashlib.sha256(response.content).hexdigest()
                    assert digest == expected[recipe]
                    result['restored_PDF_hashes'][recipe] = digest
            assert len(result['restored_PDF_hashes']) == 3
            result['checks'].append('three_original_immutable_PDF_publications_identical')

            workspace = call('GET', '/workspaces')[0]['id']
            case = call('POST', f'/workspaces/{workspace}/cases', json={'name': 'Restored deployment ' + secrets.token_hex(6)})
            proof = 'RESTORED_DEPLOYMENT_' + secrets.token_hex(16)
            call('PUT', '/cases/' + str(case['id']) + '/file', json={'path': 'Ref/proof.txt', 'content': proof + '\n'})
            profile_id = prior_conversation['provider_profile_id']
            conversation = call('POST', '/conversations', json={'case_id': case['id'],
                'title': 'Restored original encrypted profile', 'provider_profile_id': profile_id,
                'model': 'deepseek-flash', 'permission_mode': 'full_access'})
            result.update(case_id=case['id'], conversation_id=conversation['id'], original_profile_id=profile_id)
            session = call('POST', '/codex/sessions', json={'case_id': case['id'],
                'conversation_id': conversation['id'], 'provider_profile_id': profile_id, 'provider_wire_api': 'responses'})
            save('session.json', session)
            result['session_id'] = session['id']
            save('result.json', result)
            turn = call('POST', '/codex/sessions/' + session['id'] + '/turns', json={
                'content': 'Actually execute cat Ref/proof.txt, then reply only with its exact contents.',
                'client_message_id': uuid.uuid4().hex, 'timeout_seconds': 120})
            result['turn_id'] = turn['id']
            save('result.json', result)
            until = time.monotonic() + 180
            while turn['phase'] not in {'completed', 'failed', 'cancelled', 'interrupted', 'recovery_required', 'evidence_invalid'}:
                assert time.monotonic() < until, 'Observation deadline: retain original turn, do not resubmit'
                time.sleep(.5)
                turn = call('GET', '/codex/turns/' + turn['id'])
            save('turn.json', turn)
            response = client.get('/codex/turns/' + turn['id'] + '/events')
            response.raise_for_status()
            (folder / 'events.sse').write_text(response.text, 'utf8')
            events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
            answer = ''.join(e.get('payload', {}).get('text', '') for e in events if e.get('type') == 'text.delta')
            commands = [e['payload'] for e in events if e['type'] == 'item.completed'
                        and e.get('payload', {}).get('type') == 'commandExecution']
            assert turn['phase'] == 'completed' and turn['native_status'] == 'completed' and turn['cleanup_verified']
            assert proof in answer and any(c.get('exitCode') == 0 and proof in c.get('aggregatedOutput', '') for c in commands)
            result.update(answer=answer, commands=commands, usage=turn['usage'])
            result['checks'].append('original_encrypted_DeepSeek_profile_decrypts_and_real_Codex_reads_new_random_file')
            history = call('GET', '/conversations/' + str(conversation['id']))
            save('conversation.json', history)
            assert any(e['type'] == 'message' and e['payload'].get('role') == 'assistant'
                       and proof in e['payload'].get('content', '') for e in history['events'])
            response = client.get('/cases/' + str(case['id']) + '/raw', params={'path': 'Ref/proof.txt'})
            response.raise_for_status()
            assert response.content == (proof + '\n').encode()
            result['checks'].append('restored_PG_and_S3_accept_new_writes_and_preserve_turn_history')
            result['passed'] = True
        except Exception as error:
            result['failure'] = {'type': type(error).__name__, 'message': str(error)[:800]}
        finally:
            if session:
                try:
                    closed = call('DELETE', '/codex/sessions/' + session['id'])
                    save('closed-session.json', closed)
                    assert closed['phase'] == 'closed' and closed['cleanup_verified']
                    result['checks'].append('restored_native_session_closed_and_stop_verified')
                except Exception as error:
                    result['passed'] = False
                    result['close_failure'] = type(error).__name__
            save('result.json', result)
    print(json.dumps(result, ensure_ascii=False))
    assert result['passed'], str(folder)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('review', type=Path)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--previous', type=Path, required=True)
    args = parser.parse_args()
    review = read(args.review)
    assert review['classification'] == 'real_llm'
    for name, value in review['sources'].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value, name
    run(args.base_url.rstrip('/'), args.previous)
