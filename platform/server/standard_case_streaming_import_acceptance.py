"""Real StandardCase scaffold and large ZIP import over HTTP. No inference."""
import argparse
import asyncio
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import random
import subprocess
from unittest.mock import patch
import zipfile

import httpx

from app.services import process_identity_native as identity
from live_llm_terminal_agent_acceptance import disposable_database
from s3testsupport import ObjectStore
from terminal_shutdown_acceptance import RUNNER
import testsupport
from testsupport import IsolatedServer, SERVER


LARGE_BYTES = 40 * 1024 * 1024
GEOMETRY = b'\n# author-owned geometry\ndef build_geometry(ctx):\n    return []\n'
ROOT = SERVER.parents[1]
APP = '''
import gc, json, os, tracemalloc
from pathlib import Path
from app import storage
from app.main import app
from app.services import case_import
from app.services.process_identity_native import observe

root = Path(__file__).parent
@app.on_event('startup')
async def birth():
    (root / 'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')

original = case_import._prepare_files
def prepare(*args):
    prepared = original(*args)
    if (root / 'mutate-import').exists():
        prepared.files['Ref/witness.bin'].path.write_bytes(b'BYTES_CHANGED')
    return prepared
case_import._prepare_files = prepare

@app.middleware('http')
async def measurement(request, next_handler):
    if not request.url.path.endswith('/cases:import') or not (root / 'measure-import').exists():
        return await next_handler(request)
    gc.collect()
    tracemalloc.start()
    try:
        return await next_handler(request)
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        claims = storage._fs_root() / '.node-storage-budget'
        (root / 'import-measurement.json').write_text(json.dumps(
            {'peak': peak, 'remaining_node_claims': len(list(claims.glob('*.json')))}), encoding='utf8')
'''


def make_archive(root, case_dict, large):
    target = root / ('large.zip' if large else 'small.zip')
    digest = hashlib.sha256()
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        prefix = 'Project/'
        archive.writestr(prefix + '0-caseDict/caseDict', case_dict)
        archive.writestr(prefix + 'A-geo/geometry.py', GEOMETRY)
        archive.writestr(prefix + 'Ref/witness.bin', b'BYTES_ORIGINAL')
        archive.writestr(prefix + 'B-mesh/.gitkeep', b'')
        if large:
            source = random.Random(20261119)
            with archive.open(prefix + 'Ref/large.bin', 'w') as member:
                for _ in range(LARGE_BYTES // (1024 * 1024)):
                    block = source.randbytes(1024 * 1024)
                    digest.update(block)
                    member.write(block)
    return target, digest.hexdigest()


def verify(database, store):
    api = IsolatedServer('standard-case-stream-import', app='standard_import_fixture:app', database_url=database)
    print('STANDARD_CASE_IMPORT ' + str(api.root), flush=True)
    settings = store.settings() if store else {}
    fixture = 'import os\nos.environ.update(' + repr(settings) + ')\n' + APP
    (api.root / 'standard_import_fixture.py').write_text(fixture, encoding='utf-8')
    runner = api.root / 'graceful_server.py'
    runner.write_text(RUNNER, encoding='utf-8')
    spawn = subprocess.Popen

    def launch(argv, **options):
        assert argv[1:3] == ['-m', 'uvicorn']
        return spawn([argv[0], '-X', 'utf8', '-B', str(runner), *argv[3:]], **options)

    report = {'passed': False, 'backend': 's3' if store else 'fs',
              'database': 'postgresql' if database else 'sqlite', 'real_model_calls': 0, 'mock_llm_calls': 0}
    birth = None
    try:
        with patch.object(testsupport.subprocess, 'Popen', launch):
            api.__enter__()
        birth = json.loads((api.root / 'api-birth.json').read_text(encoding='utf-8'))
        original = api.create_case('Original standard scaffold')
        workspace = api.call('GET', '/workspaces')[0]['id']
        scaffold = {
            '0-caseDict/caseDict', '0-caseDict/memory.md', 'A-geo/geometry.py', 'B-mesh/.gitkeep',
            'C-run/.gitkeep', 'D-post/postprocess.py', 'D-post/scenes.py', 'D-post/pvpython.py',
            'D-post/summary.md', 'E-report/report.md', 'UDF/README.md', 'Ref/.gitkeep',
        }
        with httpx.Client(base_url=api.base+'/api/v1', timeout=180, trust_env=False,
                          headers={'Authorization': 'Bearer ' + api.token}) as client:
            def raw(case_id, path):
                response = client.get(f'/cases/{case_id}/raw', params={'path': path})
                response.raise_for_status()
                return response.content

            def upload(path, name):
                with path.open('rb') as stream:
                    return client.post(f'/workspaces/{workspace}/cases:import', data={'name': name},
                                       files={'file': (path.name, stream, 'application/zip')})

            before = {path: raw(original['id'], path) for path in sorted(scaffold)}
            for path in ['B-mesh/.gitkeep', 'C-run/.gitkeep', 'Ref/.gitkeep']:
                assert before[path] == b''
            for path in (item for item in scaffold if item.endswith('.py')):
                compile(before[path].decode('utf-8'), path, 'exec')
            assert b'workflowState' in before['0-caseDict/caseDict']
            small, _ = make_archive(api.root, before['0-caseDict/caseDict'], False)
            (api.root / 'mutate-import').touch()
            try:
                conflict = upload(small, 'Verified retry')
            finally:
                (api.root / 'mutate-import').unlink()
            assert conflict.status_code == 409, conflict.text[:400]
            assert [case['id'] for case in api.call('GET', f'/workspaces/{workspace}/cases')] == [original['id']]
            retried = upload(small, 'Verified retry')
            assert retried.status_code == 200, retried.text[:400]
            assert raw(retried.json()['id'], 'Ref/witness.bin') == b'BYTES_ORIGINAL'

            archive, digest = make_archive(api.root, before['0-caseDict/caseDict'], True)
            (api.root / 'measure-import').touch()
            try:
                response = upload(archive, 'Verified large standard case')
            finally:
                (api.root / 'measure-import').unlink()
            assert response.status_code == 200, response.text[:400]
            case = response.json()
            observed = hashlib.sha256()
            size = 0
            with client.stream('GET', f'/cases/{case["id"]}/raw', params={'path': 'Ref/large.bin'}) as download:
                download.raise_for_status()
                for chunk in download.iter_bytes(1024 * 1024):
                    observed.update(chunk)
                    size += len(chunk)
            assert size == LARGE_BYTES and observed.hexdigest() == digest
            assert case['workflow_state'] == original['workflow_state'] == 'A'
            assert raw(case['id'], 'A-geo/geometry.py') == GEOMETRY
            assert raw(case['id'], 'B-mesh/.gitkeep') == b''
            assert all(raw(original['id'], path) == data for path, data in before.items())
        measured = json.loads((api.root / 'import-measurement.json').read_text(encoding='utf-8'))
        assert measured['peak'] < LARGE_BYTES // 2 and measured['remaining_node_claims'] == 0, measured
        report.update(passed=True, scaffold_files=len(before), largest_bytes=size, sha256=digest,
                      entire_request=measured, modified_staging_status=409, same_name_retry=True,
                      custom_geometry_preserved=True, original_scaffold_preserved=True)
    except BaseException as error:
        report['error'] = type(error).__name__
        raise
    finally:
        try:
            if birth is not None:
                (api.root / 'graceful-stop').touch()
                api.proc.wait(45)
                report['api_exit'] = identity.probe(birth)
                assert report['api_exit']['state'] == 'exited' and api.proc.returncode == 0
        finally:
            if api.proc is not None:
                api.__exit__(None, None, None)
            (api.root / 'standard-case-stream-import.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--s3', action='store_true')
    args = parser.parse_args()
    backend = ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed', volume_max=20) if args.s3 else nullcontext()
    async with disposable_database(args.postgres) as database:
        with backend as store:
            await asyncio.to_thread(verify, database, store)
    if args.s3:
        assert backend.proc is not None and backend.proc.poll() is not None


if __name__ == '__main__':
    asyncio.run(main())
