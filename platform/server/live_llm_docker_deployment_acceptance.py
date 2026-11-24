"""Actual isolated Compose deployment and authorized DeepSeek; no mock models."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import time

import httpx
from live_llm_acceptance import source

ROOT = Path(__file__).resolve().parents[2]


def verify(directory, profile_id):
    state = json.loads((directory / 'deployment-state.json').read_text('utf8'))
    assert state['project'] == 'vf-review-20261117' and state['loopback_only']
    assert state['base_url'].startswith('http://127.0.0.1:')
    evidence = directory / ('acceptance-' + secrets.token_hex(5))
    evidence.mkdir()
    report = {'passed': False, 'checks': [], 'mock_llm_calls': 0, 'claude_calls': 0,
              'real_model_application_requests': 0, 'jobs': [], 'base_url': state['base_url']}
    private = evidence / 'private'; private.mkdir()
    secret_values = []

    def save():
        text = json.dumps(report, ensure_ascii=False, indent=2)
        assert all(value not in text for value in secret_values)
        (evidence / 'deployment-result.json').write_text(text, 'utf8')

    def check(name):
        report['checks'].append(name); save(); print('DEPLOYMENT_PASS ' + name, flush=True)

    def compose(*args):
        result = subprocess.run(['docker', 'compose', '-p', state['project'], '-f', state['compose'],
                                 '--profile', 'distributed', *args], capture_output=True, timeout=180)
        assert result.returncode == 0, 'Isolated Compose command failed: ' + args[0]

    try:
        with httpx.Client(base_url=state['base_url'], timeout=180, trust_env=False) as client:
            page = client.get('/')
            assert page.status_code == 200 and 'text/html' in page.headers.get('content-type', '')
            assert 'Content-Security-Policy' in page.headers
            health = client.get('/api/health'); assert health.status_code == 200 and health.json()['ok']
            check('actual_web_static_CSP_and_proxied_API_health')

            def call(method, path, body=None):
                response = client.request(method, '/api/v1' + path, json=body)
                if response.status_code != 200:
                    report['last_failure_response'] = {'method': method, 'path': path, 'status': response.status_code,
                        'content_type': response.headers.get('content-type'), 'body': response.text[:1200]}
                    save()
                assert response.status_code == 200, method + ' ' + path + ' HTTP ' + str(response.status_code)
                return response.json()

            registration = {'email': 'docker-' + secrets.token_hex(8) + '@example.com',
                            'password': secrets.token_hex(20), 'name': 'Docker deployment acceptance'}
            secret_values.append(registration['password'])
            (private / 'account.json').write_text(json.dumps(registration), 'utf8')
            account = call('POST', '/auth/register', registration)
            secret_values.append(account['token'])
            client.headers['Authorization'] = 'Bearer ' + account['token']
            workspace = call('GET', '/workspaces')[0]['id']
            case = call('POST', f'/workspaces/{workspace}/cases', {'name': 'Actual Docker worker'})
            case_id = case['id']; report.update(workspace_id=workspace, case_id=case_id)
            check('actual_PostgreSQL_registration_workspace_and_case')
            content = secrets.token_hex(1024 * 1024 + 17)
            digest = hashlib.sha256(content.encode()).hexdigest()
            call('PUT', f'/cases/{case_id}/file', {'path': 'Ref/large-proxy.txt', 'content': content})
            raw = client.get(f'/api/v1/cases/{case_id}/raw', params={'path': 'Ref/large-proxy.txt'})
            assert raw.status_code == 200 and hashlib.sha256(raw.content).hexdigest() == digest
            report['large_file'] = {'bytes': len(raw.content), 'sha256': digest}
            check('large_file_through_real_nginx_and_API_exact')
            nonce = 'DOCKER_' + secrets.token_hex(16)
            call('PUT', f'/cases/{case_id}/file', {'path': 'Ref/deployment-proof.txt', 'content': nonce})

            def submit(script):
                command = 'python -u -c ' + shlex.quote(script)
                job = call('POST', f'/cases/{case_id}/jobs', {'command': command, 'runtime': 'windows', 'timeout_seconds': 90})
                report['jobs'].append({'id': job['id'], 'submitted': True}); save()
                return job['id']

            def wait_job(job_id, expected, timeout=100):
                end = time.monotonic() + timeout
                while time.monotonic() < end:
                    job = call('GET', f'/jobs/{job_id}')
                    if job['status'] not in {'queued', 'running'} and job['status'] != expected:
                        report['jobs'][-1]['terminal'] = job; save()
                        raise AssertionError('Unexpected actual job status: ' + job['status'])
                    stop = (job.get('attempt') or {}).get('process_stop')
                    if (job['status'] == expected and not job.get('completion_pending')
                            and stop is not None and stop.get('verified')):
                        report['jobs'][-1]['terminal'] = job; save()
                        assert job['status'] == expected, 'Unexpected actual job status: ' + job['status']
                        assert job['attempt']['process_stop']['verified']
                        return job
                    time.sleep(.2)
                raise TimeoutError('Actual distributed job did not finish')

            job_id = submit("from pathlib import Path;import os;Path('Ref/container-result.txt').write_text('actual-native-'+str(os.getuid()));print('NATIVE_DONE',flush=True)")
            terminal = wait_job(job_id, 'succeeded')
            worker = subprocess.run(['docker', 'inspect', state['project'] + '-arq-worker',
                '--format', '{{.Config.Hostname}}'], capture_output=True, text=True, timeout=15)
            assert worker.returncode == 0
            assert terminal['attempt']['process_identity']['worker_id'].startswith(worker.stdout.strip() + ':')
            check('real_Redis_worker_native_job_and_verified_stop')
            cancelled_id = submit("import time;print('CANCEL_READY',flush=True);time.sleep(60)")
            end = time.monotonic() + 60
            while time.monotonic() < end:
                current = call('GET', f'/jobs/{cancelled_id}')
                if current['status'] == 'running' and 'CANCEL_READY' in current.get('log_backlog', ''): break
                if current['status'] not in {'queued', 'running'}: raise AssertionError('Cancellation job failed before running')
                time.sleep(.2)
            else: raise TimeoutError('Actual cancellation job never reached its output marker')
            call('POST', f'/jobs/{cancelled_id}:cancel', {})
            wait_job(cancelled_id, 'cancelled')
            check('actual_running_worker_cancellation_and_verified_stop')

            backup = client.get(f'/api/v1/workspaces/{workspace}/backup')
            assert backup.status_code == 200
            (evidence / 'workspace-backup.zip').write_bytes(backup.content)
            restored = client.post('/api/v1/workspaces:restore', files={'file': ('workspace.zip', backup.content, 'application/zip')})
            assert restored.status_code == 200, 'Actual proxied backup restore HTTP ' + str(restored.status_code)
            report['restored_workspace'] = restored.json()['workspace_id']
            check('actual_workspace_backup_and_restore_through_nginx')
            compose('restart', '-t', '90', 'api', 'arq-worker')
            end = time.monotonic() + 90
            while time.monotonic() < end:
                try:
                    response = client.get('/api/health', timeout=5)
                    if response.status_code == 200 and response.json().get('ok'): break
                except (httpx.HTTPError, ValueError): pass
                time.sleep(.5)
            else: raise TimeoutError('Restarted actual API unavailable')
            client.headers.pop('Authorization', None)
            login = call('POST', '/auth/login', {'email': registration['email'], 'password': registration['password']})
            secret_values.append(login['token']); client.headers['Authorization'] = 'Bearer ' + login['token']
            assert call('GET', f'/jobs/{job_id}')['status'] == 'succeeded'
            raw = client.get(f'/api/v1/cases/{case_id}/raw', params={'path': 'Ref/large-proxy.txt'})
            assert raw.status_code == 200 and hashlib.sha256(raw.content).hexdigest() == digest
            check('actual_API_worker_restart_preserves_accounts_files_jobs')

            configuration = asyncio.run(source(profile_id))
            secret_values.append(configuration['key'])
            profile = call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'Actual container DeepSeek',
                'base_url': configuration['base_url'], 'default_model': configuration['model'], 'api_key': configuration['key']})
            conversation = call('POST', '/conversations', {'case_id': case_id, 'title': 'Actual Docker DeepSeek',
                'provider_profile_id': profile['id'], 'model': configuration['model'], 'permission_mode': 'plan'})
            report['model'] = {'provider': 'deepseek', 'host': 'api.deepseek.com', 'requested_model': configuration['model'],
                               'conversation_id': conversation['id'], 'upstream_HTTP_count': 'not instrumented'}
            events = []; report['real_model_application_requests'] += 1; save()
            with client.stream('POST', f'/api/v1/conversations/{conversation["id"]}/messages', json={
                'content': '请调用 read_text_file 读取 Ref/deployment-proof.txt，完成后原样回复文件里的验证字符串。不要猜测，不要修改文件。',
                'provider_profile_id': profile['id'], 'model': configuration['model']}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith('data:'): events.append(json.loads(line[5:]))
            text = json.dumps(events, ensure_ascii=False)
            assert all(value not in text for value in secret_values)
            (evidence / 'live-model-events.json').write_text(text, 'utf8')
            answer = ''.join(event.get('text', '') for event in events if event.get('type') == 'text.delta')
            assert nonce in answer and any(event.get('type') == 'tool.result' for event in events)
            assert any(event.get('type') == 'run.completed' for event in events)
            assert not any(event.get('type') in {'stream.error', 'request.failed'} for event in events)
            stored = call('GET', f'/conversations/{conversation["id"]}')
            assert any(event['type'] == 'tool_result' and nonce in str(event['payload']) for event in stored['events'])
            report['model']['answer'] = answer
            check('actual_DeepSeek_tool_read_stream_and_persisted_result_in_container')
            report['passed'] = True
    except BaseException as error:
        detail = str(error)
        for value in secret_values: detail = detail.replace(value, '<redacted>')
        report['failure'] = {'type': type(error).__name__, 'detail': detail[:1500]}
        raise
    finally:
        save()
    print(json.dumps({'passed': report['passed'], 'evidence': str(evidence), 'checks': len(report['checks'])}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--profile-id', type=int, default=26)
    args = parser.parse_args()
    review = json.loads(args.review.read_text('utf8'))
    assert review['entry'] == 'platform/server/live_llm_docker_deployment_acceptance.py'
    for path, digest in review['sources'].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path
    verify(args.directory, args.profile_id)
