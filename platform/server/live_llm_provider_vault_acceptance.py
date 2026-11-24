"""Issue a real DeepSeek translation after reopening a provider stored with Windows DPAPI."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import socket
import subprocess
import time

from cryptography.fernet import Fernet

from app.services import provider_vault
from app.services.process_identity_native import probe
from app.services.redaction import redact_text
from live_llm_acceptance import source
from live_llm_shared_index_acceptance import SharedIndexServer, configure


def await_api_exit(api):
    birth = api.birth
    if probe(birth)['state']=='match':
        subprocess.run(['taskkill', '/PID', str(birth['pid']), '/T', '/F'], capture_output=True, timeout=10)
    deadline = time.monotonic() + 10
    while probe(birth)['state']!='exited' and time.monotonic() < deadline:
        time.sleep(.1)
    assert probe(birth)['state']=='exited'
    with socket.socket() as connection:
        assert connection.connect_ex(('127.0.0.1', api.port)) != 0


def verify(config):
    assert os.name == 'nt'
    api = SharedIndexServer('live-f14-provider-vault', app='live_fixture:app')
    configure(api, {}, False)
    with (api.root / 'live_fixture.py').open('a', encoding='utf8') as fixture:
        fixture.write('\nlive_request_limit=1\n')
    report = {'passed': False, 'provider': 'deepseek', 'model': config['model'], 'mock_llm_calls': 0}
    vault_path = api.root / '.vault_key'
    print('LIVE_F14_EVIDENCE', api.root, flush=True)
    try:
        email = 'isolated-vault-verify@example.com'
        password = 'isolated-test-123'
        with api:
            api.token = api.call('POST', '/auth/register', {'name': 'Vault owner', 'email': email,
                                  'password': password})['token']
            profile = {'name': 'Windows DPAPI', 'provider': 'deepseek', 'base_url': config['base_url'],
                       'default_model': config['model'], 'api_key': config['key']}
            for url in ('http://example.com/v1', 'https://person:password@example.com/v1',
                        'https://example.com/v1?token=bogus'):
                rejected = api.call('POST', '/provider-profiles', {**profile, 'base_url': url}, expect=400, raw=True)
                assert config['key'].encode() not in rejected
            persisted = api.call('POST', '/provider-profiles', profile)
            assert 'key_ciphertext' not in persisted and 'api_key' not in persisted
            pid = persisted['id']
            protected = vault_path.read_bytes()
            assert protected.startswith(provider_vault.DPAPI_PREFIX) and config['key'].encode() not in protected
            report.update(endpoint_rejections=3, protected_file=True)
        await_api_exit(api)
        with api:
            api.token = api.call('POST', '/auth/login', {'email': email, 'password': password})['token']
            response = api.call('POST', '/llm/translate', {'text': 'Heat transfer coefficient', 'target': 'zh',
                                                         'provider_profile_id': pid})
            assert response['ok'] and '传热系数' in response['translation']
            assert vault_path.read_bytes() == protected
            report.update(restarted_api_real_translation=True, vault_unchanged_after_restart=True)

        # These are file and redaction checks. They never contact a model.
        legacy = api.root / 'legacy-key'
        legacy_key = Fernet.generate_key()
        legacy.write_bytes(legacy_key)
        ciphertext = Fernet(legacy_key).encrypt(b'legacy-vault-verification')
        assert provider_vault.local(legacy).decrypt(ciphertext) == b'legacy-vault-verification'
        assert legacy.read_bytes() == legacy_key
        corrupt = api.root / 'corrupt-key'
        invalid = provider_vault.DPAPI_PREFIX + b'invalid'
        corrupt.write_bytes(invalid)
        try:
            provider_vault.local(corrupt)
        except (OSError, ValueError):
            assert corrupt.read_bytes() == invalid
        else:
            raise AssertionError('Invalid DPAPI file must fail closed')
        concurrent = api.root / 'concurrent-key'
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(lambda _: provider_vault.local(concurrent).encrypt(b'single-key'), range(16)))
        assert all(provider_vault.read(concurrent).decrypt(token)==b'single-key' for token in tokens)
        assert len(list(api.root.glob('.provider-vault-*'))) == 0
        redacted = redact_text('Bearer hidden-credential {"refresh_token":"SECOND_SECRET",'
                               '"apiKey":"THIRD_SECRET"} known%2Furi%2Bvalue', ('known/uri+value',))
        assert not any(secret in redacted for secret in ('hidden-credential', 'SECOND_SECRET', 'THIRD_SECRET', 'known'))
        assert len(redact_text('温度'*1000, limit=17).encode('utf8')) <= 17
        report.update(legacy_key_read=True, corrupt_key_preserved=True, concurrent_writers=16, redaction=True)
        observed = [json.loads(line) for line in (api.root / 'upstream.jsonl').read_text(encoding='utf8').splitlines()]
        assert len(observed)==1 and observed[0]['kind']=='http' and observed[0]['status']==200
        await_api_exit(api)
        report['api_exit_verified_after_both_runs'] = True
        report['passed'] = True
    finally:
        trace = api.root / 'upstream.jsonl'
        if trace.is_file():
            report['upstream'] = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
        encoded = json.dumps(report, indent=2)
        assert config['key'] not in encoded
        (api.root / 'vault-result.json').write_text(encoded, encoding='utf8')


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    args = parser.parse_args()
    verify(asyncio.run(source(args.source_profile_id)))
