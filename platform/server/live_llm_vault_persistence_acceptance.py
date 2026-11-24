"""Actual API credential creation, key restore, restart and two real DeepSeek calls."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import httpx

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer, SERVER

ROOT = SERVER.parents[1]


def verify(configuration):
    api = IsolatedServer('live-vault-persistence', app='live_fixture:app')
    (api.root/'live_fixture.py').write_text(FIXTURE, encoding='utf-8')
    original_path = api.root/'original-volume/provider.key'
    restored_path = api.root/'restored-volume/provider.key'
    previous = os.environ.get('VIBEFLOW_VAULT_KEY_PATH')
    result = {'passed': False, 'provider': 'deepseek', 'model': configuration['model'],
              'mock_llm_calls': 0, 'real_llm_calls': 0, 'api_exits': [], 'checks': []}
    registration = {'email': uuid.uuid4().hex+'@example.com', 'password': uuid.uuid4().hex,
                    'name': 'Vault persistence acceptance'}
    def save():
        trace = api.root/'upstream.jsonl'
        if trace.exists():
            result['upstream'] = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
            result['real_llm_calls'] = sum(r['kind'] == 'http' for r in result['upstream'])
        (api.root/'vault-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    def provision(destination, original=None):
        argv = [sys.executable, '-B', str(ROOT/'platform/scripts/init-provider-vault.py'), str(destination)]
        if original:
            argv += ['--copy-from', str(original)]
        p = subprocess.run(argv, capture_output=True, timeout=15)
        assert p.returncode == 0, 'Isolated deployment key provisioning failed'
    print('LIVE_VAULT_EVIDENCE '+str(api.root), flush=True)
    try:
        provision(original_path)
        original_bytes = original_path.read_bytes()
        for round_number, path in enumerate((original_path, restored_path)):
            if round_number:
                provision(restored_path, original_path)
                assert restored_path.read_bytes() == original_bytes
            os.environ['VIBEFLOW_VAULT_KEY_PATH'] = str(path)
            try:
                with api:
                    if not round_number:
                        api.token = api.call('POST', '/auth/register', registration)['token']
                        profile = api.call('POST', '/provider-profiles', {
                            'provider': 'deepseek', 'name': 'Persistent real credentials',
                            'base_url': configuration['base_url'], 'default_model': configuration['model'],
                            'api_key': configuration['key']})
                        assert configuration['key'] not in json.dumps(profile)
                        profile_id = profile['id']
                        result['profile_id'] = profile_id
                    else:
                        api.token = api.call('POST', '/auth/login', {
                            'email': registration['email'], 'password': registration['password']})['token']
                        profiles = api.call('GET', '/provider-profiles')
                        assert any(p['id'] == profile_id for p in profiles)
                    with httpx.Client(base_url=api.base+'/api/v1',
                                      headers={'Authorization': 'Bearer '+api.token},
                                      trust_env=False, timeout=180) as client:
                        response = client.post('/llm/translate', json={
                            'text': 'The calibration identifier is VAULT_'+uuid.uuid4().hex+'.',
                            'target': 'zh', 'provider_profile_id': profile_id})
                        response.raise_for_status()
                        translated = response.json()
                        assert translated['ok'] and translated['translation'].strip()
                        assert configuration['key'] not in json.dumps(translated)
                    assert path.read_bytes() == original_bytes
                    assert not (api.root/'.vault_key').exists(), 'Configured key was ignored'
                    result['checks'].append('real_translation_before_restart' if not round_number
                                            else 'real_translation_after_key_restore_and_restart')
                    print('LIVE_VAULT_PASS '+result['checks'][-1], flush=True)
            finally:
                if api.proc is not None:
                    result['api_exits'].append({'pid': api.proc.pid, 'exit_code': api.proc.poll(),
                                                'confirmed_exited': api.proc.poll() is not None})
                log = api.root/'api.log'
                if log.exists():
                    (api.root/f'api-{round_number}.log').write_bytes(log.read_bytes())
                save()
        # Actual configuration rejection and exclusive creation; no model simulation.
        for name, path in [('missing', api.root/'missing.key'), ('invalid', api.root/'invalid.key')]:
            if name == 'invalid':
                path.write_bytes(b'invalid-key-for-configuration-check')
            env = dict(os.environ, VIBEFLOW_VAULT_KEY_PATH=str(path))
            p = subprocess.run([sys.executable, '-B', '-c',
                                'from app.services.llm import validate_vault_configuration;validate_vault_configuration()'],
                               cwd=SERVER, env=env, capture_output=True, timeout=15)
            assert p.returncode != 0 and b'Configured provider vault' in p.stderr
            if name == 'missing':
                assert not path.exists()
            else:
                assert path.read_bytes() == b'invalid-key-for-configuration-check'
            result['checks'].append(name+'_key_rejected_without_replacement')
        p = subprocess.run([sys.executable, '-B', str(ROOT/'platform/scripts/init-provider-vault.py'),
                            str(restored_path)], capture_output=True, timeout=15)
        assert p.returncode != 0 and restored_path.read_bytes() == original_bytes
        result['checks'].append('provisioning_refuses_overwrite')
        upstream = [json.loads(line) for line in (api.root/'upstream.jsonl').read_text(encoding='utf-8').splitlines()]
        calls = [r for r in upstream if r['kind'] == 'http']
        assert len(calls) == 2 and all(r['host'] == 'api.deepseek.com' and r['status'] == 200 for r in calls)
        assert all(x['confirmed_exited'] for x in result['api_exits'])
        result.update(passed=True, real_llm_calls=len(calls), upstream=upstream,
                      key_bytes_unchanged=True, profile_recreated_after_restart=False)
        save()
    except BaseException as error:
        result['failure_type'] = type(error).__name__
        result['failure'] = str(error).replace(configuration['key'], '[REDACTED]')[:1000]
        save()
        print('LIVE_VAULT_FAILED '+result['failure_type'], flush=True)
        return 1
    finally:
        if previous is None:
            os.environ.pop('VIBEFLOW_VAULT_KEY_PATH', None)
        else:
            os.environ['VIBEFLOW_VAULT_KEY_PATH'] = previous
    print('LIVE_VAULT_OK '+str(api.root/'vault-result.json'), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--review', type=Path, required=True)
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding='utf-8'))
    assert review['entry'] == 'platform/server/live_llm_vault_persistence_acceptance.py'
    assert review['classification'] == 'real_llm'
    for name, digest in review['sources'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
    return verify(asyncio.run(source(args.source_profile_id)))


if __name__ == '__main__':
    sys.exit(main())
