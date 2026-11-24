"""Real Docker Pandoc reports, restart persistence and authorized DeepSeek readback."""
import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path
import secrets
import subprocess
import time
import zipfile

import httpx
from live_llm_acceptance import source

ROOT = Path(__file__).resolve().parents[2]


def verify(directory, profile_id):
    state = json.loads((directory / 'deployment-state.json').read_text('utf8'))
    assert state['project'] == 'vf-review-20261117' and state['loopback_only']
    evidence = directory / ('reports-' + secrets.token_hex(5))
    (evidence / 'private').mkdir(parents=True)
    report = {'passed': False, 'checks': [], 'jobs': [], 'real_model_application_requests': 0,
              'mock_llm_calls': 0, 'claude_calls': 0, 'base_url': state['base_url']}
    secret_values = []

    def write(name, value):
        text = json.dumps(value, ensure_ascii=False, indent=2)
        assert all(secret not in text for secret in secret_values)
        (evidence / name).write_text(text, 'utf8')

    def save(): write('report-result.json', report)

    def check(name):
        report['checks'].append(name)
        save()
        print('DOCKER_REPORT_PASS ' + name, flush=True)

    print('DOCKER_REPORT_EVIDENCE ' + str(evidence), flush=True)
    try:
        with httpx.Client(base_url=state['base_url'], trust_env=False, timeout=180) as client:
            def call(method, path, body=None):
                response = client.request(method, '/api/v1' + path, json=body)
                if response.status_code != 200:
                    report['last_response'] = {'path': path, 'status': response.status_code,
                                               'body': response.text[:1200]}
                    save()
                assert response.status_code == 200, path + ' HTTP ' + str(response.status_code)
                return response.json()

            account = {'email': 'report-' + secrets.token_hex(8) + '@example.com',
                       'password': secrets.token_hex(20), 'name': 'Actual Docker reports'}
            secret_values.append(account['password'])
            (evidence / 'private/account.json').write_text(json.dumps(account), 'utf8')
            auth = call('POST', '/auth/register', account)
            secret_values.append(auth['token'])
            client.headers['Authorization'] = 'Bearer ' + auth['token']
            workspace_id = call('GET', '/workspaces')[0]['id']
            case_id = call('POST', f'/workspaces/{workspace_id}/cases', {'name': 'Docker Pandoc reports'})['id']
            report.update(case_id=case_id, workspace_id=workspace_id)
            prefix = f'/cases/{case_id}'

            def raw(path):
                response = client.get('/api/v1' + prefix + '/raw', params={'path': path})
                assert response.status_code == 200, 'Missing actual report: ' + path
                return response.content

            settings = call('GET', '/runtime/config')
            assert settings['pandoc'] == '/usr/bin/pandoc'
            report['pandoc_path'] = settings['pandoc']
            check('installed_Linux_Pandoc_discovered_by_actual_API')
            nonce = 'PANDOC_' + secrets.token_hex(16)
            markdown = '# Container report\n\n' + nonce + '\n\n| Field | Value |\n|---|---|\n| Velocity | 2 m/s |\n'
            call('PUT', prefix + '/file', {'path': 'E-report/report.md', 'content': markdown})
            created = call('POST', prefix + '/report:build', {})
            job_id = created['job_id']
            report['jobs'].append({'id': job_id}); save()
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                job = call('GET', '/jobs/' + str(job_id))
                if job['status'] in {'failed', 'cancelled', 'interrupted'}:
                    write('job-terminal.json', job)
                    raise AssertionError('Actual Pandoc job ' + job['status'])
                if (job['status'] == 'succeeded' and not job.get('completion_pending')
                        and (job.get('attempt') or {}).get('outputs', {}).get('completion', {}).get('state') == 'settled'):
                    break
                time.sleep(.25)
            else:
                write('job-timeout.json', job)
                raise TimeoutError('Actual report publication did not settle')
            write('job-terminal.json', job)
            assert job['attempt']['process_stop']['verified']
            children = job['attempt']['child_processes']['children']
            assert children and all(child.get('stop', {}).get('verified') for child in children.values())
            report['jobs'][-1].update(stop_verified=True, owned_children=len(children))
            check('actual_registered_report_job_and_owned_children_stopped')

            html, docx = raw('E-report/build/report.html'), raw('E-report/build/report.docx')
            assert nonce.encode() in html and b'2 m/s' in html
            with zipfile.ZipFile(io.BytesIO(docx)) as archive:
                document = archive.read('word/document.xml')
                assert nonce.encode() in document and b'2 m/s' in document
            toolchain = json.loads(raw('E-report/build/report-toolchain.json'))
            assert toolchain['pandoc'] == '/usr/bin/pandoc' and toolchain['pandoc_version'].startswith('pandoc ')
            assert len(toolchain['pandoc_sha256']) == 64
            report['toolchain'] = toolchain
            report['artifacts'] = {kind: {'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
                                   for kind, value in [('html', html), ('docx', docx)]}
            (evidence / 'report.html').write_bytes(html)
            (evidence / 'report.docx').write_bytes(docx)
            check('HTML_and_DOCX_actual_download_content_and_toolchain_provenance')

            restarted = subprocess.run(['docker', 'compose', '-p', state['project'], '-f', state['compose'],
                '--profile', 'distributed', 'restart', '-t', '90', 'api', 'arq-worker'], capture_output=True, timeout=180)
            assert restarted.returncode == 0, 'Actual isolated service restart failed'
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    response = client.get('/api/health', timeout=5)
                    if response.status_code == 200 and response.json().get('ok'): break
                except (httpx.HTTPError, ValueError): pass
                time.sleep(.5)
            else: raise TimeoutError('Actual API did not return after restart')
            client.headers.pop('Authorization', None)
            auth = call('POST', '/auth/login', {'email': account['email'], 'password': account['password']})
            secret_values.append(auth['token']); client.headers['Authorization'] = 'Bearer ' + auth['token']
            assert raw('E-report/build/report.html') == html and raw('E-report/build/report.docx') == docx
            assert json.loads(raw('E-report/build/report-toolchain.json')) == toolchain
            assert call('GET', '/jobs/' + str(job_id))['status'] == 'succeeded'
            check('actual_restart_preserves_report_bytes_job_and_toolchain')

            config = asyncio.run(source(profile_id)); secret_values.append(config['key'])
            profile = call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'Docker report verification',
                'base_url': config['base_url'], 'default_model': config['model'], 'api_key': config['key']})
            conversation = call('POST', '/conversations', {'case_id': case_id, 'title': 'Read actual Pandoc report',
                'provider_profile_id': profile['id'], 'model': config['model'], 'permission_mode': 'plan'})
            report['model'] = {'provider': 'deepseek', 'host': 'api.deepseek.com', 'requested_model': config['model'],
                'conversation_id': conversation['id'], 'upstream_HTTP_count': 'not instrumented'}
            events = []; report['real_model_application_requests'] += 1; save()
            with client.stream('POST', f'/api/v1/conversations/{conversation["id"]}/messages', json={
                'content': '请调用 read_text_file 读取 E-report/build/report.html，找到以 PANDOC_ 开头的验证字符串并原样回复。不要猜测，不要修改文件。',
                'provider_profile_id': profile['id'], 'model': config['model']}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith('data:'): events.append(json.loads(line[5:]))
            write('live-model-events.json', events)
            answer = ''.join(event.get('text', '') for event in events if event.get('type') == 'text.delta')
            assert nonce in answer and any(e.get('type') == 'tool.result' for e in events)
            assert any(e.get('type') == 'run.completed' for e in events)
            assert not any(e.get('type') in {'stream.error', 'request.failed', 'request.timed_out'} for e in events)
            stored = call('GET', f'/conversations/{conversation["id"]}')
            assert any(e['type'] == 'tool_result' and nonce in str(e['payload']) for e in stored['events'])
            report['model']['answer'] = answer
            check('actual_DeepSeek_reads_generated_HTML_and_persists_tool_result')
            report['passed'] = True
    except BaseException as error:
        detail = str(error)
        for secret in secret_values: detail = detail.replace(secret, '<redacted>')
        report['failure'] = {'type': type(error).__name__, 'detail': detail[:1500]}
        raise
    finally:
        save()
    print(json.dumps({'passed': True, 'evidence': str(evidence), 'checks': len(report['checks'])}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--profile-id', type=int, default=26)
    args = parser.parse_args()
    review = json.loads(args.review.read_text('utf8'))
    assert review['entry'] == 'platform/server/live_llm_docker_report_acceptance.py'
    assert review['classification'] == 'real_llm'
    for path, digest in review['sources'].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path
    verify(args.directory, args.profile_id)
