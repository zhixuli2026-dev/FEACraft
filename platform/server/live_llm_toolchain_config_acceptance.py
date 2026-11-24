"""Real relocated Pandoc reports, configuration restart/restore and DeepSeek readback."""
import argparse
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile

import httpx

from live_llm_acceptance import FIXTURE, source
from live_llm_sandbox_recovery_acceptance import model_read
from testsupport import IsolatedServer, SERVER

ROOT = SERVER.parents[1]


def verify(configuration):
    api = IsolatedServer('live-toolchain-config', app='live_fixture:app')
    (api.root/'live_fixture.py').write_text(FIXTURE, encoding='utf-8')
    binary = ROOT/'extracted/share/vibeflow/tools/pandoc/pandoc.exe'
    folder = api.root/'relocated tools'
    folder.mkdir()
    setup_pandoc, env_pandoc = folder/'setup-pandoc.exe', folder/'env-pandoc.exe'
    for p in (setup_pandoc, env_pandoc):
        shutil.copyfile(binary, p)
        assert hashlib.sha256(p.read_bytes()).digest() == hashlib.sha256(binary.read_bytes()).digest()
    previous = {key: os.environ.get(key) for key in ('VIBEFLOW_PANDOC', 'VIBEFLOW_PYTHON',
                                                   'VIBEFLOW_WSL_DISTRO', 'VIBEFLOW_OPENFOAM_BASHRC',
                                                   'VIBEFLOW_VAULT_KEY_PATH')}
    for key in previous:
        os.environ.pop(key, None)
    result = {'passed': False, 'jobs': [], 'api_exits': [], 'mock_llm_calls': 0,
              'real_llm_calls': 0, 'checks': []}
    def save():
        trace = api.root/'upstream.jsonl'
        if trace.exists():
            rows = [json.loads(x) for x in trace.read_text(encoding='utf-8').splitlines()]
            result['real_llm_calls'] = sum(x['kind'] == 'http' for x in rows)
        (api.root/'toolchain-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    def call(method, path, body=None, **kwargs):
        deadline = time.monotonic()+40
        while True:
            try:
                return api.call(method, path, body, **kwargs)
            except AssertionError as error:
                if not any(x in str(error) for x in ('storage_quota_busy', 'validation_busy')) or time.monotonic() >= deadline:
                    raise
                time.sleep(.2)
    def publication(job_id):
        with sqlite3.connect('file:'+str(api.root/'test.sqlite')+'?mode=ro', uri=True) as db:
            return db.execute('SELECT publication_json FROM job_attempts WHERE job_id=?', (job_id,)).fetchone()[0]
    print('TOOLCHAIN_EVIDENCE '+str(api.root), flush=True)
    try:
        for phase in range(3):
            if phase == 1:
                os.environ['VIBEFLOW_PANDOC'] = str(env_pandoc)
            elif phase == 2:
                os.environ.pop('VIBEFLOW_PANDOC')
                (api.root/'runtime.json').write_bytes(backup)
            chosen = env_pandoc if phase == 1 else setup_pandoc
            try:
                with api:
                    if phase == 0:
                        api.create_case('Relocated toolchain')
                        email = call('GET', '/auth/me')['email']
                        call('PATCH', '/runtime/config', {'pandoc': str(setup_pandoc), 'python': sys.executable})
                        backup = (api.root/'runtime.json').read_bytes()
                    else:
                        api.token = call('POST', '/auth/login', {'email': email, 'password': 'isolated-test-123'})['token']
                    settings = call('GET', '/runtime/config')
                    assert Path(settings['pandoc']) == chosen
                    if phase == 1:
                        refused = call('PATCH', '/runtime/config', {'pandoc': str(setup_pandoc)}, expect=400)
                        assert 'VIBEFLOW_PANDOC' in refused['detail']
                        call('PATCH', '/runtime/config', {'python': sys.executable})
                        assert json.loads((api.root/'runtime.json').read_text())['pandoc'] == str(setup_pandoc)
                    if phase in (0, 1):
                        with httpx.Client(base_url=api.base, headers={'Authorization': 'Bearer '+api.token},
                                          trust_env=False, timeout=120) as client:
                            response = client.get('/api/v1/runtime/capabilities')
                            response.raise_for_status()
                            caps = response.json()
                        assert Path(caps['binaries']['pandoc']) == chosen
                        registry = {row['tool']: row for row in caps['environment_registry']}
                        assert registry['pandoc']['available'] and Path(registry['pandoc']['value']) == chosen
                        assert all(next(c['ok'] for c in caps['stage_checks'][stage]['checks']
                                        if c['check'] == 'report/pandoc') for stage in ('D', 'E'))
                        result.setdefault('capabilities', []).append(caps)
                        save()
                    proof = 'RETIREMENT_'+uuid.uuid4().hex
                    call('PUT', api.prefix+'/file', {'path': 'E-report/report.md', 'content': '# Toolchain verification\n\n'+proof+'\n'})
                    created = call('POST', api.prefix+'/report:build', {})
                    deadline = time.monotonic()+180
                    while True:
                        job = call('GET', '/jobs/'+str(created['job_id']))
                        if job['status'] == 'succeeded' and job['attempt']['outputs'].get('completion', {}).get('state') == 'settled':
                            break
                        assert job['status'] not in {'failed', 'cancelled', 'interrupted'} and time.monotonic() < deadline, 'Report did not settle'
                        time.sleep(.25)
                    assert job['attempt']['process_stop']['verified']
                    recorded = publication(job['id'])
                    assert Path(json.loads(recorded)['descriptor']['pandoc']) == chosen
                    html = call('GET', api.prefix+'/raw?path=E-report/build/report.html', raw=True)
                    docx = call('GET', api.prefix+'/raw?path=E-report/build/report.docx', raw=True)
                    assert proof.encode() in html
                    with zipfile.ZipFile(io.BytesIO(docx)) as z:
                        assert proof.encode() in z.read('word/document.xml')
                    result['jobs'].append({'id': job['id'], 'phase': phase, 'pandoc': str(chosen),
                                           'stop_verified': True, 'publication_json': recorded,
                                           'html_sha256': hashlib.sha256(html).hexdigest(),
                                           'docx_sha256': hashlib.sha256(docx).hexdigest()})
                    for old in result['jobs']:
                        assert publication(old['id']) == old['publication_json']
                    result['checks'].append(('persisted_setup', 'deployment_override', 'restored_setup')[phase])
                    save()
                    print('TOOLCHAIN_REPORT_PASS '+result['checks'][-1], flush=True)
                    if phase == 2:
                        model_read(api, '', '', proof, configuration, already_restored=True,
                                   path='E-report/build/report.html', expected_content=html)
            finally:
                if api.proc is not None:
                    result['api_exits'].append({'pid': api.proc.pid, 'exit_code': api.proc.poll(),
                                                'exited': api.proc.poll() is not None})
                log = api.root/'api.log'
                if log.exists():
                    (api.root/f'api-{phase}.log').write_bytes(log.read_bytes())
                save()
        # Missing executable is a configuration test, never a fabricated binary/model.
        code = '''from app.services.runtime_registry import environment_registry,stage_check
r={x['tool']:x for x in environment_registry()}
assert not r['pandoc']['available'] and r['pandoc']['source']=='explicit_error'
for stage in ('D','E'):
 assert next(x['ok'] for x in stage_check(stage,{}, {})['checks'] if x['check']=='report/pandoc') is False
print('MISSING_TOOL_REJECTED')
'''
        env = dict(os.environ, VIBEFLOW_PANDOC=str(api.root/'missing-pandoc.exe'))
        checked = subprocess.run([sys.executable, '-B', '-c', code], cwd=SERVER, env=env,
                                 capture_output=True, timeout=15)
        assert checked.returncode == 0, 'Missing executable capability check failed'
        result['checks'].append('missing_deployment_tool_not_masked_by_local_fallback')
        with sqlite3.connect('file:'+str(api.root/'test.sqlite')+'?mode=ro', uri=True) as db:
            requests = db.execute('SELECT id,kind,phase,children_json FROM native_requests').fetchall()
        children = []
        for identifier, kind, phase, raw in requests:
            assert phase != 'active'
            for child in json.loads(raw)['children'].values():
                assert child['stop'] and child['stop']['verified'], (identifier, kind)
                children.append(child['id'])
        result['native_requests'] = len(requests)
        result['native_children_stopped'] = len(children)
        assert len(result['jobs']) == 3 and all(r['exited'] for r in result['api_exits'])
        result['passed'] = True
        save()
    except BaseException as error:
        result['failure_type'] = type(error).__name__
        result['failure'] = str(error).replace(configuration['key'], '[REDACTED]')[:1200]
        save()
        print('TOOLCHAIN_FAILED '+result['failure_type'], flush=True)
        return 1
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print('TOOLCHAIN_OK '+str(api.root/'toolchain-result.json'), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--review', type=Path, required=True)
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding='utf-8'))
    assert review['entry'] == 'platform/server/live_llm_toolchain_config_acceptance.py'
    assert review['classification'] == 'real_llm'
    for name, digest in review['sources'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
    return verify(asyncio.run(source(args.source_profile_id)))


if __name__ == '__main__':
    sys.exit(main())
