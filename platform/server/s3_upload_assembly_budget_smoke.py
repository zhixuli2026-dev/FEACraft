"""Measure actual resumable S3 upload assembly under a live node claim."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time

import httpx

from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


MIB = 1024 * 1024
SIZE = 40 * MIB
CHUNK = 8 * MIB
COMPETING = 160 * MIB
ASSEMBLY_CLAIM = SIZE + 4096  # File body plus the process-bound owner marker.
PATH = 'Ref/large-resumable-upload.bin'
EXISTING = 'Original destination must remain readable'
RUNTIME = SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed'
FIXTURE = r'''
import asyncio
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time

from fastapi import Depends
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.routers.workspaces import get_owned_case
from app.services import node_storage_budget, process_identity_native, s3_uploads


root = Path(__file__).parent
mode = ContextVar('upload_assembly_measurement', default=None)
prepare = s3_uploads.prepare_completion
cleanup = s3_uploads.cleanup_cache


def record(path, data):
    target = root / path
    scratch = target.with_suffix('.writing')
    scratch.write_text(json.dumps(data, sort_keys=True), encoding='utf8')
    scratch.replace(target)


def reserved():
    directory = storage._fs_root() / node_storage_budget.DIRECTORY
    return sum(json.loads(path.read_text(encoding='utf8'))['bytes'] for path in directory.glob('*.json'))


def measure(cache):
    directory = Path(cache['directory']) if cache.get('directory') else None
    if directory is not None:
        assert directory.resolve().is_relative_to(root.resolve())
    part = directory / 'data.part' if directory is not None else None
    return {'directory_created': directory is not None, 'directory_present': directory is not None and directory.exists(),
            'assembled_bytes': part.stat().st_size if part is not None and part.exists() else 0,
            'claimed_bytes':reserved()}


def observed_prepare(prefix, identifier, cache):
    try:
        return prepare(prefix, identifier, cache)
    finally:
        if mode.get() is not None:
            record(mode.get() + '-assembly.json', measure(cache))


def observed_cleanup(cache):
    selected = mode.get()
    before = measure(cache) if selected is not None else None
    try:
        return cleanup(cache)
    finally:
        if selected is not None:
            record(selected + '-cleanup.json', {'before':before, 'after':measure(cache)})


s3_uploads.prepare_completion = observed_prepare
s3_uploads.cleanup_cache = observed_cleanup


@app.get('/fixture/upload-binding/{case_id}')
async def binding(case_id:int, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    return {'prefix':case.s3_prefix, 'process':process_identity_native.observe(os.getpid())}


@app.post('/fixture/live-node-claim')
async def occupy(user=Depends(get_current_user)):
    async with node_storage_budget.allocation(160 * 1024 * 1024):
        record('claim-ready.json', {'claimed_bytes':reserved()})
        until = time.monotonic() + 125
        while not (root / 'claim-release').exists():
            if time.monotonic() >= until:
                raise TimeoutError('Isolated competing claim was not released')
            await asyncio.sleep(.03)
    return {'released':True}


class InspectComplete:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get('headers', ()))
        selected = headers.get(b'x-fixture-assembly-mode')
        if not scope.get('path', '').endswith(':complete') or selected is None:
            return await self.app(scope, receive, send)
        selected = selected.decode('ascii')
        assert selected in {'low','damaged','normal','repeat'}
        token = mode.set(selected)
        try:
            await self.app(scope, receive, send)
        finally:
            mode.reset(token)


app = InspectComplete(app)
'''


def wait_record(root, name):
    path = root / name
    until = time.monotonic() + 30
    while True:
        try:
            return json.loads(path.read_text(encoding='utf8'))
        except (FileNotFoundError, PermissionError):
            if time.monotonic() >= until:
                raise
            time.sleep(.03)


def payload(path):
    digest = hashlib.sha256()
    with path.open('xb') as stream:
        for _ in range(SIZE // MIB):
            block = os.urandom(MIB)
            stream.write(block)
            digest.update(block)
    return digest.hexdigest()


def upload_chunks(api, client, path, digest):
    route = api.prefix + '/uploads'
    session = api.call('POST', route, {'path':PATH, 'size':SIZE, 'sha256':digest, 'overwrite':True})
    endpoint = route + '/' + session['id']
    with path.open('rb') as stream:
        for offset in range(0, SIZE, CHUNK):
            response = client.put(endpoint, params={'offset':offset}, content=stream.read(CHUNK),
                                  headers={'Content-Type':'application/octet-stream'})
            assert response.status_code == 200, response.text[:500]
    return session['id'], endpoint


def object_key(prefix, identifier, offset):
    index = f'{offset:010d}'
    return prefix + f'/.vibeflow/uploads/{identifier}/chunks/{index}/data'


def remote_chunks(store, prefix, identifier):
    return [store.client.head_object(Bucket=store.bucket, Key=object_key(prefix, identifier, offset))
             for offset in range(0,SIZE,CHUNK)]


def original_chunks_held(api, store, prefix, identifier, endpoint, original):
    state = api.call('GET', endpoint)
    assert state['status'] == 'uploading' and state['offset'] == SIZE, state
    observed = remote_chunks(store, prefix, identifier)
    assert [(row['ETag'], row['ContentLength']) for row in observed] == [
        (row['ETag'], row['ContentLength']) for row in original]
    assert api.call('GET', api.prefix + '/raw?path=' + PATH, raw=True) == EXISTING.encode('utf8')


def complete(client, endpoint, mode):
    response = client.post(endpoint + ':complete', headers={'X-Fixture-Assembly-Mode': mode})
    return {'status':response.status_code, 'document':response.json()}


def exercise(api, store, report, baseline):
    case = api.create_case('Actual remote upload assembly')
    binding = api.call('GET', f'/fixture/upload-binding/{case["id"]}', api_prefix=False)
    prefix = binding['prefix']
    report['api_process'] = binding['process']
    local = api.root / 'original-40mib.bin'
    digest = report['original_sha256'] = payload(local)
    api.put(PATH, EXISTING)
    headers = {'Authorization':'Bearer ' + api.token}
    with httpx.Client(base_url=api.base + '/api/v1', headers=headers, trust_env=False, timeout=150) as client:
        identifier, endpoint = upload_chunks(api, client, local, digest)
        originals = remote_chunks(store, prefix, identifier)
        assert [value['ContentLength'] for value in originals] == [CHUNK] * 5
        with ThreadPoolExecutor(max_workers=1) as worker:
            competitor = worker.submit(client.post, api.base + '/fixture/live-node-claim')
            try:
                competing = wait_record(api.root, 'claim-ready.json')
                assert competing['claimed_bytes'] == COMPETING
                low = report['full_node'] = complete(client, endpoint, 'low')
                assert low['status'] == 409 and low['document']['detail']['code'] == 'node_storage_full', low
                trace = report['low_observation'] = wait_record(api.root, 'low-assembly.json')
                assert trace['claimed_bytes'] == COMPETING
                if baseline:
                    assert trace['assembled_bytes'] == SIZE and trace['directory_present'], trace
                    report['low_cleanup'] = wait_record(api.root, 'low-cleanup.json')
                    assert not report['low_cleanup']['after']['directory_present']
                else:
                    assert trace['assembled_bytes'] == 0 and not trace['directory_created'], trace
                    assert not (api.root / 'low-cleanup.json').exists()
            finally:
                (api.root / 'claim-release').touch()
                result = competitor.result(timeout=30)
                assert result.status_code == 200 and result.json() == {'released':True}
        original_chunks_held(api, store, prefix, identifier, endpoint, originals)
        report['original_chunks_preserved_after_low_capacity'] = len(originals)
        report['original_target_preserved_after_low_capacity'] = True
        if not baseline:
            key = object_key(prefix, identifier, 0)
            with local.open('rb') as stream:
                original = stream.read(CHUNK)
            corrupted = bytes([original[0] ^ 0xff]) + original[1:]
            metadata = {'Bucket':store.bucket, 'Key':key, 'ContentType':originals[0]['ContentType']}
            changed = store.client.put_object(**metadata, Body=corrupted, IfMatch=originals[0]['ETag'])
            try:
                failed = report['damaged'] = complete(client, endpoint, 'damaged')
                assert failed['status'] == 409 and '远端分块内容与摘要不一致' in failed['document']['detail'], failed
                cleanup = report['damaged_cleanup'] = wait_record(api.root, 'damaged-cleanup.json')
                assert cleanup['before']['assembled_bytes'] == CHUNK and cleanup['before']['claimed_bytes'] == ASSEMBLY_CLAIM
                assert not cleanup['after']['directory_present'] and cleanup['after']['claimed_bytes'] == 0
            finally:
                store.client.put_object(**metadata, Body=original, IfMatch=changed['ETag'])
            original_chunks_held(api, store, prefix, identifier, endpoint, originals)
            report['original_target_preserved_after_damaged'] = True

        success = report['retry'] = complete(client, endpoint, 'normal')
        assert success['status'] == 200 and success['document']['sha256'] == digest, success
        cleanup = report['normal_cleanup'] = wait_record(api.root, 'normal-cleanup.json')
        assert cleanup['before']['assembled_bytes'] == SIZE
        assert cleanup['before']['claimed_bytes'] == (0 if baseline else ASSEMBLY_CLAIM), cleanup
        assert not cleanup['after']['directory_present'] and cleanup['after']['claimed_bytes'] == 0
        repeat = report['repeated_completion'] = complete(client, endpoint, 'repeat')
        assert repeat == success and not (api.root / 'repeat-assembly.json').exists(), repeat
        hashed, count = hashlib.sha256(), 0
        with client.stream('GET', api.prefix + '/raw', params={'path':PATH}) as response:
            assert response.status_code == 200
            for block in response.iter_bytes(65536):
                count += len(block)
                hashed.update(block)
        report['readback'] = {'size':count, 'sha256':hashed.hexdigest()}
        assert count == SIZE and hashed.hexdigest() == digest


def run(baseline=False):
    from app.services import process_identity_native

    api = IsolatedServer('upload-assembly-budget-s3', app='upload_assembly_fixture:app')
    target = api.root / 'upload-assembly-budget.json'
    report = {'passed':False, 'baseline':baseline, 'api_port':api.port,
              'real_model_calls':0, 'mock_llm_calls':0}
    print('UPLOAD_ASSEMBLY_EVIDENCE', api.root, flush=True)
    try:
        with ObjectStore(executable=RUNTIME, volume_max=24, authenticated=True) as store:
            report['store_receipt'] = str(store.root / 'store.json')
            configuration = store.settings()
            scratch = api.root / 'native-temp'
            scratch.mkdir()
            configuration.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch),
                                 VIBEFLOW_NODE_STORAGE_LIMIT_MB='192', VIBEFLOW_NODE_STORAGE_MIN_FREE_MB='0')
            module = api.root / 'upload_assembly_fixture.py'
            try:
                module.write_text('import os\nos.environ.update(' + repr(configuration) + ')\n' + FIXTURE, encoding='utf8')
                with api:
                    exercise(api, store, report, baseline)
            finally:
                module.unlink(missing_ok=True)
        report['store_exit_code'] = store.proc.poll()
        report['api_exit_code'] = api.proc.poll()
        assert report['store_exit_code'] is not None and report['api_exit_code'] is not None
        api.proc = None
        report['api_exit'] = process_identity_native.probe(report['api_process'])
        with socket.socket() as probe:
            probe.settimeout(.25)
            report['api_listener'] = probe.connect_ex(('127.0.0.1', api.port)) == 0
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__, 'message':str(error)[:1400]}
        raise
    finally:
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    print('UPLOAD_ASSEMBLY_PASS', target, flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    run(os.environ.get('VIBEFLOW_UPLOAD_ASSEMBLY_BASELINE') == '1')
