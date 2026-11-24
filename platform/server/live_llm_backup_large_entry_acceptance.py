"""Actual v1 restore of a 40 MiB member with authorized model readback."""
import argparse
import asyncio
import base64
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import random
import secrets
import zipfile

import httpx

from app.services import process_identity_native as identity
from live_llm_acceptance import FIXTURE as LIVE, source
from live_llm_terminal_agent_acceptance import disposable_database
from s3testsupport import ObjectStore
from terminal_shutdown_acceptance import RUNNER
from testsupport import IsolatedServer, SERVER
from unittest.mock import patch


LARGE = 40 * 1024 * 1024
ROOT = SERVER.parents[1]
FIXTURE = '''
import gc, os, tracemalloc
from app.config import get_settings
from app.services.backup_restore import RestoreFiles
from app.services.process_identity_native import observe

root = Path(__file__).parent
@app.on_event('startup')
async def process_birth():
    (root / 'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')

load = RestoreFiles.load
def load_with_limit(self, upload):
    if not (root / 'low-disk').exists():
        return load(self, upload)
    settings = get_settings()
    previous = settings.node_storage_limit_mb
    try:
        settings.node_storage_limit_mb = 1
        return load(self, upload)
    finally:
        settings.node_storage_limit_mb = previous
RestoreFiles.load = load_with_limit

@app.middleware('http')
async def observe_full_restore(request, next_handler):
    if request.url.path != '/api/v1/workspaces:restore' or not (root / 'measure').exists():
        return await next_handler(request)
    gc.collect()
    tracemalloc.start()
    try:
        return await next_handler(request)
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        claims = storage._fs_root() / '.node-storage-budget'
        (root / 'restore-measurement.json').write_text(json.dumps(
            {'peak': peak, 'remaining_node_claims': len(list(claims.glob('*.json')))}), encoding='utf8')
'''


def manifest():
    return {'schema': 'vibeflow.workspace-backup.v1', 'workspace': {'name': 'Legacy large entry'},
            'cases': [{'id': 5, 'name': 'Restored source', 'workflow_state': 'A', 'dict_revision': 1,
                       'case_meta': '{}', 'parse_status': 'ok'}]}


def record(path, payload):
    return {'data': base64.b64encode(payload).decode('ascii'), 'case_id': 5, 'path': path,
            'sha256': hashlib.sha256(payload).hexdigest()}


def archive(root, proof):
    path = root / 'legacy-v1-large.zip'
    contents = manifest()
    small = [record('Ref/restored-proof.txt', proof.encode('ascii')),
             record('0-caseDict/caseDict', b'workflowState A;\n'), record('Ref/empty.bin', b'')]
    generator = random.Random(20261119)
    digest = hashlib.sha256()
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as bundle:
        with bundle.open('manifest.json', 'w') as stream:
            stream.write(json.dumps(contents).encode('ascii')[:-1] + b',"files":[{"data":"')
            remaining = LARGE
            while remaining:
                chunk = generator.randbytes(min(remaining, 768 * 1024))
                stream.write(base64.b64encode(chunk))
                digest.update(chunk)
                remaining -= len(chunk)
            item = {'case_id': 5, 'path': 'Ref/large.bin', 'sha256': digest.hexdigest()}
            stream.write(b'",' + json.dumps(item).encode('ascii')[1:] + b',')
            stream.write(json.dumps(small).encode('ascii')[1:] + b'}')
    return path, digest.hexdigest()


def small_archive(path, *, reject=False):
    contents = manifest()
    entry = record('Ref/编码.bin', b'\xfb\xff')
    if reject:
        entry['data'] = 'bad padding==='
    contents['files'] = [record('0-caseDict/caseDict', b'workflowState A;\n'), entry]
    encoded = json.dumps(contents, ensure_ascii=False).replace('+/8=', r'\u002b\/8\u003d')
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('manifest.json', encoded.encode('utf-16'))
    return path


def model_readback(api, client, configuration, case_id, proof):
    profile = api.call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'Restored legacy source',
        'base_url': configuration['base_url'], 'default_model': configuration['model'], 'api_key': configuration['key']})
    conversation = api.call('POST', '/conversations', {'case_id': case_id, 'title': 'Read restored source',
        'permission_mode': 'plan', 'provider_profile_id': profile['id'], 'model': configuration['model']})
    events = []
    with client.stream('POST', f'/conversations/{conversation["id"]}/messages', json={
        'content': '只调用一次 read_text_file 读取 Ref/restored-proof.txt，再完整原样返回文件中的标识。',
        'provider_profile_id': profile['id'], 'model': configuration['model']}) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith('data:'):
                events.append(json.loads(line[5:]))
    tools = [event for event in events if event.get('type') == 'tool.result']
    assert len(tools) == 1 and tools[0]['tool'] == 'read_text_file' and tools[0]['ok']
    assert proof in tools[0]['result']
    answer = ''.join(event.get('text', '') for event in events if event.get('type') == 'text.delta')
    assert proof in answer and any(event.get('type') == 'run.completed' for event in events)


def verify(configuration, database_url, store):
    import subprocess
    import testsupport

    api = IsolatedServer('backup-large-entry', app='restore_fixture:app', database_url=database_url)
    print('BACKUP_LARGE_ENTRY ' + str(api.root), flush=True)
    settings = store.settings() if store else {}
    header = 'import os\nos.environ.update(' + repr(settings) + ')\n'
    (api.root / 'restore_fixture.py').write_text(header + LIVE + '\nfrom app import storage\n' + FIXTURE, encoding='utf8')
    runner = api.root / 'graceful_server.py'
    runner.write_text(RUNNER, encoding='utf8')
    old_launch = subprocess.Popen

    def launch(argv, **kwargs):
        assert argv[1:3] == ['-m', 'uvicorn']
        return old_launch([argv[0], '-X', 'utf8', '-B', str(runner), *argv[3:]], **kwargs)

    report = {'passed': False, 'backend': 's3' if store else 'fs',
              'database': 'postgresql' if database_url else 'sqlite', 'mock_llm_calls': 0}
    birth = None
    try:
        with patch.object(testsupport.subprocess, 'Popen', launch):
            api.__enter__()
        birth = json.loads((api.root / 'api-birth.json').read_text(encoding='utf8'))
        api.create_case('Legacy restore owner')
        proof = 'RESTORED_' + secrets.token_hex(18)
        path, expected_hash = archive(api.root, proof)
        with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization': 'Bearer '+api.token},
                          trust_env=False, timeout=180) as client:
            def restore(candidate):
                with candidate.open('rb') as stream:
                    return client.post('/workspaces:restore', files={'file': ('v1.zip', stream, 'application/zip')})

            def claim_count():
                return len(list((api.root/'files'/'.node-storage-budget').glob('*.json')))

            def workspaces():
                return {item['id'] for item in api.call('GET', '/workspaces')}

            before = workspaces()
            denied = restore(small_archive(api.root/'invalid.zip', reject=True))
            assert denied.status_code == 400 and 'Base64' in denied.json()['detail']
            (api.root/'low-disk').touch()
            try:
                denied = restore(path)
                assert denied.status_code == 409 and denied.json()['detail']['code'] == 'node_storage_full'
            finally:
                (api.root/'low-disk').unlink()
            assert workspaces() == before

            (api.root/'measure').touch()
            try:
                response = restore(path)
            finally:
                (api.root/'measure').unlink()
            assert response.status_code == 200, response.text[:400]
            result = response.json()
            assert result['cases'] == 1 and result['files'] == 4
            measured = json.loads((api.root/'restore-measurement.json').read_text(encoding='utf8'))
            assert measured['peak'] < LARGE // 2 and measured['remaining_node_claims'] == 0, measured
            case_id = api.call('GET', f'/workspaces/{result["workspace_id"]}/cases')[0]['id']
            digest = hashlib.sha256()
            observed = 0
            with client.stream('GET', f'/cases/{case_id}/raw', params={'path': 'Ref/large.bin'}) as download:
                download.raise_for_status()
                for block in download.iter_bytes(1024 * 1024):
                    observed += len(block)
                    digest.update(block)
            assert observed == LARGE and digest.hexdigest() == expected_hash
            assert api.call('GET', f'/cases/{case_id}/raw?path=Ref/empty.bin', raw=True) == b''

            old = restore(small_archive(api.root/'legacy-utf16.zip'))
            assert old.status_code == 200, old.text[:400]
            old_id = api.call('GET', f'/workspaces/{old.json()["workspace_id"]}/cases')[0]['id']
            assert client.get(f'/cases/{old_id}/raw', params={'path': 'Ref/编码.bin'}).content == b'\xfb\xff'
            assert claim_count() == 0
            report.update(member_bytes=observed, member_sha256=expected_hash, full_restore=measured,
                          invalid_base64_status=400, quota_status=409, legacy_utf16_escaped_readback=True,
                          rejected_requests_created_no_workspace=True)
            model_readback(api, client, configuration, case_id, proof)
        trace = (api.root/'upstream.jsonl').read_text(encoding='utf8').splitlines()
        responses = [row for row in map(json.loads, trace) if row['kind']=='http' and row['path'].endswith('/chat/completions')]
        assert len(responses) == 2 and all(row['status'] == 200 for row in responses)
        report.update(passed=True, model=configuration['model'], provider='deepseek', actual_HTTP_200=2)
    except BaseException as error:
        report['error'] = type(error).__name__
        raise
    finally:
        try:
            if birth is not None:
                (api.root/'graceful-stop').touch()
                api.proc.wait(45)
                observed_exit = identity.probe(birth)
                report['api_exit'] = observed_exit
                assert api.proc.returncode == 0 and observed_exit['state'] == 'exited', observed_exit
        finally:
            if api.proc is not None:
                api.__exit__(None, None, None)
            (api.root/'backup-large-entry.json').write_text(json.dumps(report, indent=2), encoding='utf8')


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--s3', action='store_true')
    args = parser.parse_args()
    configuration = await source(args.source_profile_id)
    backend = ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed', volume_max=20) if args.s3 else nullcontext()
    async with disposable_database(args.postgres) as database:
        with backend as store:
            await asyncio.to_thread(verify, configuration, database, store)
    if args.s3:
        assert backend.proc is not None and backend.proc.poll() is not None


if __name__ == '__main__':
    asyncio.run(main())
