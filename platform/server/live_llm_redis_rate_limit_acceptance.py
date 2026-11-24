"""Verify actual shared Redis rate admissions against official DeepSeek traffic."""
import argparse
import asyncio
from contextlib import contextmanager
import json
import os
import secrets
import socket
import subprocess
import time

from cryptography.fernet import Fernet
import httpx
from redis import Redis
from redis.exceptions import RedisError

from live_llm_acceptance import FIXTURE, source
from live_llm_chat_markdown_acceptance import wait_for_api_exit
from testsupport import IsolatedServer


def docker(*args, check=True):
    return subprocess.run(['docker', *args], check=check, capture_output=True, text=True)


@contextmanager
def redis_server():
    name = 'vf-j19-' + secrets.token_hex(6)
    password = secrets.token_urlsafe(24)
    with socket.socket() as free:
        free.bind(('127.0.0.1', 0))
        port = free.getsockname()[1]
    docker('run', '--pull=never', '--rm', '--detach', '--name', name, '-p', f'127.0.0.1:{port}:6379',
           'redis:7-alpine', 'redis-server', '--save', '', '--appendonly', 'no', '--requirepass', password)
    try:
        with Redis(host='127.0.0.1', port=port, password=password, socket_timeout=1) as client:
            for _ in range(50):
                try:
                    if client.ping():
                        break
                except RedisError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError('Isolated Redis did not start')
        yield name, port, password
    finally:
        docker('unpause', name, check=False)
        docker('rm', '-f', name, check=False)


def configure(api, config, vault):
    source_code = 'import os\nos.environ.update(' + repr(config) + ')\nlive_request_limit=3\n' + FIXTURE
    source_code += '''
from cryptography.fernet import Fernet
from app.services.process_identity_native import observe
(root/'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')
'''
    source_code += 'vault=Fernet(' + repr(vault) + ')\nllm._vault=lambda:vault\n'
    path = api.root / 'rate_fixture.py'
    path.write_text(source_code, encoding='utf8')
    return path


def verify(primary, peer, redis_name, deepseek, report):
    case = primary.create_case('A shared rate budget')
    peer.token = primary.token
    registration = primary.call('POST', '/auth/register', {'email': 'rate-limit-member@example.com',
        'password': 'test-only-member-001', 'name': 'Rate limit member'})
    other = {'Authorization': 'Bearer ' + registration['token']}
    org = primary.call('GET', '/org/mine')['org']['id']
    primary.call('POST', f'/org/{org}/members', {'email': registration['user']['email'], 'role': 'member'}, expect=201)
    peer.call('POST', f'/org/{org}/invitation:accept', {}, extra_headers=other)

    def profile(name):
        return primary.call('POST', '/provider-profiles', {'name': name, 'provider': 'deepseek',
            'base_url': deepseek['base_url'], 'default_model': deepseek['model'], 'api_key': deepseek['key']})['id']

    first, second = profile('Rate profile one'), profile('Rate profile two')
    primary.call('PUT', f'/org/{org}/provider-profiles/{first}', {'enabled': True})
    chat = primary.call('POST', '/conversations', {'title': 'Limited agent', 'case_id': case['id'],
        'provider_profile_id': first, 'model': deepseek['model'], 'permission_mode': 'ask'})['id']

    with httpx.Client(trust_env=False, timeout=45) as client:
        owner = {'Authorization': 'Bearer ' + primary.token}

        def request(api, method, path, *, headers=owner, payload=None, status=200):
            response = client.request(method, api.base + '/api/v1' + path, headers=headers, json=payload)
            assert response.status_code == status, (api is primary, path, response.status_code, status, response.text[:220])
            return response

        def translate(api, profile_id, headers=owner, status=200):
            response = request(api, 'POST', '/llm/translate', headers=headers, status=status,
                payload={'text': 'Inlet velocity is 8 metres per second.', 'target': 'zh', 'provider_profile_id': profile_id})
            if status == 200:
                assert response.json()['ok'] and '8' in response.json()['translation']
            else:
                assert int(response.headers['retry-after']) >= 1 and response.headers['x-content-type-options']=='nosniff'
            return response

        translate(primary, first)
        limited = translate(peer, first, status=429)
        request(peer, 'POST', f'/provider-profiles/{first}:test', status=429)
        stream = request(peer, 'POST', f'/conversations/{chat}/messages', payload={
            'content': 'This must remain inside the test platform.', 'provider_profile_id': first})
        frames = [json.loads(line[5:]) for line in stream.text.splitlines() if line.startswith('data:')]
        assert any(frame.get('code') == 'provider_rate_limited' and frame.get('status') == 429 for frame in frames)
        assert not any(frame['type'] in ('request.usage', 'text.delta') for frame in frames)
        assert request(primary, 'GET', f'/conversations/{chat}').status_code == 200
        translate(peer, second)
        translate(peer, first, headers=other)
        report.update(shared_replica_status=limited.status_code, retry_after=int(limited.headers['retry-after']),
                      separate_profile_succeeded=True, shared_profile_other_user_succeeded=True,
                      agent_sse_rejected=True, blocked_connection_test=True)

        statuses = []
        for index in range(45):
            api = primary if index % 2 == 0 else peer
            response = client.get(api.base + '/api/v1/auth/me', headers={'X-Forwarded-For': f'192.0.2.{index + 1}'})
            statuses.append(response.status_code)
        assert 401 in statuses and statuses[-1] == 429 and all(s in (401, 429) for s in statuses), statuses
        report['untrusted_proxy_cannot_rotate_bucket'] = True

        docker('pause', redis_name)
        try:
            request(peer, 'GET', '/workspaces', status=503)
            assert client.get(peer.base + '/api/health').status_code == 200
        finally:
            docker('unpause', redis_name)
        assert request(peer, 'GET', '/workspaces').status_code == 200
        report['redis_fail_closed_and_recovered'] = True


def run(config):
    # Uvicorn reads its proxy trust policy before importing the app module.
    os.environ['FORWARDED_ALLOW_IPS'] = ''
    primary = IsolatedServer('live-j19-rate-primary', app='rate_fixture:app')
    peer = IsolatedServer('live-j19-rate-peer', app='rate_fixture:app')
    report = {'passed': False, 'provider': config['provider'], 'model': config['model'], 'mock_llm_calls': 0}
    print('LIVE_J19_EVIDENCE', primary.root, flush=True)
    paths = []
    try:
        with redis_server() as (name, port, password):
            vault = Fernet.generate_key()
            shared = {'VIBEFLOW_RATE_LIMIT_BACKEND': 'redis', 'VIBEFLOW_RATE_LIMIT_NAMESPACE': name,
                      'VIBEFLOW_RATE_LIMIT_PROFILE_PER_MINUTE': '1', 'VIBEFLOW_RATE_LIMIT_API_PER_MINUTE': '40',
                      'VIBEFLOW_REDIS_PORT': str(port), 'VIBEFLOW_REDIS_HOST': '127.0.0.1',
                      'VIBEFLOW_REDIS_PASSWORD': password}
            paths.append(configure(primary, shared, vault))
            with primary:
                for key in ('VIBEFLOW_DATABASE_URL', 'VIBEFLOW_STORAGE_FS_ROOT', 'VIBEFLOW_JWT_SECRET'):
                    shared[key] = primary.env[key]
                paths.append(configure(peer, shared, vault))
                with peer:
                    verify(primary, peer, name, config, report)
            report['api_exit'] = [wait_for_api_exit(api.root, api.port) for api in (primary, peer)]
        report['passed'] = True
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
        traces = [api.root / 'upstream.jsonl' for api in (primary, peer)]
        report['upstream'] = [[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
                              if trace.is_file() else [] for trace in traces]
        actual = [row for group in report['upstream'] for row in group if row['kind']=='http']
        report['actual_http_200'] = sum(row['status']==200 for row in actual)
        report['passed'] = report['passed'] and len(actual)==report['actual_http_200']==3
        evidence = json.dumps(report, indent=2)
        assert config['key'] not in evidence
        (primary.root / 'rate-limit-result.json').write_text(evidence, encoding='utf8')
    assert report['passed'], report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    args = parser.parse_args()
    run(asyncio.run(source(args.source_profile_id)))
