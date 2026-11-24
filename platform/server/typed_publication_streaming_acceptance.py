"""Real typed commands and immutable S3 artifact members through the production API."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import httpx

from s3testsupport import ObjectStore
from testsupport import SERVER, IsolatedServer


MEMBER = 'D-post/large-artifact.bin'
RUNTIME = SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed'

FIXTURE = r'''
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import threading
import time
import tracemalloc

from botocore.exceptions import ClientError
from fastapi import Depends
from app import storage
from app.deps import get_current_user
from app.main import app
from app.services import artifact_publication, attempt_objects, fs_directory, process_identity_native
from app.services import publication_alias_objects, s3_case_files

root = Path(__file__).parent
member = 'D-post/large-artifact.bin'
size = int((root/'member-size.txt').read_text())
objects = storage.s3().raw
raw_put = objects.put_object
put_member = attempt_objects.put_output_member
verify_final = artifact_publication.verify_files_for_finish
prepare_aliases = publication_alias_objects.prepare
apply_aliases = publication_alias_objects.apply
remove_source = s3_case_files.remove_owned
retry_sharing = fs_directory.retry_sharing_violation
source_after_verification = threading.Event()
source_exercised = threading.Event()
source_handle = threading.local()

def save(row):
    with (root/'publication-events.jsonl').open('a', encoding='utf8') as output:
        output.write(json.dumps({**row, 'recorded_ns': time.time_ns()}) + '\n')
        output.flush()
        os.fsync(output.fileno())

def measured(prefix, base, name, source, expected):
    if not base.startswith('.vibeflow/publications/') or name != member:
        return put_member(prefix, base, name, source, expected)
    tracemalloc.start()
    try:
        return put_member(prefix, base, name, source, expected)
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        save({'kind': 'memory', 'pid': os.getpid(), 'prefix': prefix, 'size': expected['size'], 'peak': peak})

def observed_put(**kwargs):
    key = kwargs['Key']
    selected = '/.vibeflow/publications/' in key and key.endswith('/files/' + member)
    alias = ('/.vibeflow/live-store/objects/' in key and key.endswith('/large-artifact.bin')
             and (root/'exercise-alias-streaming').is_file())
    if selected or alias:
        assert kwargs['IfNoneMatch'] == '*'
        assert not isinstance(kwargs['Body'], (bytes, bytearray, memoryview))
        scratch = storage._fs_root()/'.node-storage-budget'
        claims = [p.name for p in scratch.glob('*.json')
                  if json.loads(p.read_text(encoding='utf8')).get('bytes') == size]
        assert claims
    try:
        result = raw_put(**kwargs)
    except ClientError as error:
        if selected:
            save({'kind': 'put', 'pid': os.getpid(), 'key': key,
                  'status': error.response['ResponseMetadata']['HTTPStatusCode'], 'node_claims': claims})
        raise
    if selected:
        save({'kind': 'put', 'pid': os.getpid(), 'key': key,
              'status': result['ResponseMetadata']['HTTPStatusCode'], 'node_claims': claims})
        if (root/'arm-interruption').is_file() and not (root/'unacknowledged-put.json').exists():
            with (root/'unacknowledged-put.json').open('x', encoding='utf8') as output:
                json.dump({'pid': os.getpid(), 'key': key}, output)
                output.flush()
                os.fsync(output.fileno())
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                time.sleep(.05)
            raise TimeoutError('Artifact fixture did not terminate its original worker')
    if alias:
        save({'kind': 'alias_body_put', 'prefix': key.split('/.vibeflow/live-store/objects/', 1)[0],
              'key': key, 'status': result['ResponseMetadata']['HTTPStatusCode'], 'node_claims': claims})
    return result

async def observed_verification(owner, *args, **kwargs):
    save({'kind': 'verification_started', 'job_id': owner.job_id})
    try:
        return await verify_final(owner, *args, **kwargs)
    finally:
        save({'kind': 'verification_returned', 'job_id': owner.job_id})
        if owner.job_id == 1 and (root/'exercise-source-sharing').is_file():
            source_after_verification.set()

def observed_aliases(prefix, *args, **kwargs):
    save({'kind': 'alias_preparation_started', 'prefix': prefix})
    measure = (root/'exercise-alias-streaming').is_file()
    if measure:
        tracemalloc.start()
    try:
        return prepare_aliases(prefix, *args, **kwargs)
    finally:
        if measure:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            save({'kind': 'alias_prepare_memory', 'prefix': prefix, 'peak': peak})
        save({'kind': 'alias_preparation_returned', 'prefix': prefix})

def observed_alias_apply(prefix, row, digest):
    if not (root/'exercise-alias-streaming').is_file():
        return apply_aliases(prefix, row, digest)
    arm = root/'arm-alias-conflict'
    if arm.is_file() and int(arm.read_text()) == row.case_id:
        gate = root/'alias-conflict-gate.json'
        pending = gate.with_suffix('.tmp')
        pending.write_text(json.dumps({'prefix': prefix, 'publication_id': row.id}), encoding='utf8')
        pending.replace(gate)
        deadline = time.monotonic()+45
        while not (root/'release-alias-conflict').is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError('The external alias edit did not arrive')
            time.sleep(.05)
    tracemalloc.start()
    try:
        return apply_aliases(prefix, row, digest)
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        save({'kind': 'alias_apply_memory', 'prefix': prefix, 'peak': peak})

def observed_retry(operation):
    if not hasattr(source_handle, 'errors'):
        return retry_sharing(operation)
    def attempt():
        try:
            return operation()
        except OSError as error:
            source_handle.errors.append(getattr(error, 'winerror', None))
            source_handle.blocked.set()
            raise
    return retry_sharing(attempt)

def observed_source_cleanup(path, **kwargs):
    if not source_after_verification.is_set() or source_exercised.is_set():
        return remove_source(path, **kwargs)
    source_exercised.set()
    target = path/'B-mesh'
    assert target.is_dir()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # An actual open directory handle with read/write sharing and without delete sharing.
    handle = kernel.CreateFileW(str(target), 0x1, 0x1|0x2, None, 3, 0x02000000, None)
    if handle in {None, ctypes.c_void_p(-1).value}:
        raise ctypes.WinError(ctypes.get_last_error())
    errors = source_handle.errors = []
    blocked = source_handle.blocked = threading.Event()
    def release():
        blocked.wait(1.5)
        time.sleep(.3)
        kernel.CloseHandle(handle)
    opener = threading.Thread(target=release)
    opener.start()
    started = time.monotonic()
    try:
        remove_source(path, **kwargs)
    finally:
        elapsed = time.monotonic()-started
        opener.join(timeout=3)
        del source_handle.errors, source_handle.blocked
        save({'kind': 'source_cleanup_sharing', 'winerrors': errors, 'elapsed': elapsed,
              'owned_scratch_removed': not path.exists(), 'directory': target.name})

attempt_objects.put_output_member = measured
objects.put_object = observed_put
artifact_publication.verify_files_for_finish = observed_verification
publication_alias_objects.prepare = observed_aliases
publication_alias_objects.apply = observed_alias_apply
s3_case_files.remove_owned = observed_source_cleanup
fs_directory.retry_sharing_violation = observed_retry

@app.get('/api/v1/test/typed-publication-process')
async def process(user=Depends(get_current_user)):
    return process_identity_native.observe(os.getpid())

@app.post('/api/v1/test/typed-publication-exit')
async def exit_original(body: dict, user=Depends(get_current_user)):
    assert (root/'unacknowledged-put.json').is_file()
    assert body['pid'] == os.getpid()
    os._exit(79)
'''

PRODUCER = """
import hashlib
import os
from pathlib import Path
import sys

target = Path('D-post/large-artifact.bin')
target.parent.mkdir(exist_ok=True)
sha256 = hashlib.sha256()
with target.open('xb') as output:
    for index in range(int(sys.argv[2])):
        block = os.urandom(1024 * 1024)
        output.write(block)
        sha256.update(block)
Path('D-post/small-fixture.txt').write_text('native artifact', encoding='utf8')
with Path(sys.argv[1]).open('a', encoding='utf8') as count:
    count.write(sha256.hexdigest() + '\\n')
"""

CONFLICT_PRODUCER = """
from pathlib import Path
import sys

Path('D-post/small-fixture.txt').write_text('generated alias', encoding='utf8')
Path('D-post/companion.txt').write_text('generated companion', encoding='utf8')
with Path(sys.argv[1]).open('a', encoding='utf8') as count:
    count.write('ran once\\n')
"""


def delivery(api, case, size_mib, producer=PRODUCER):
    api.put('Ref/typed-artifact-producer.py', producer)
    counter = api.root / f'native-{case["id"]}.txt'
    command = subprocess.list2cmdline([sys.executable, '-X', 'utf8', 'Ref/typed-artifact-producer.py',
                                       str(counter), str(size_mib)])
    job = api.call('POST', api.prefix + '/jobs', {'type': 'convert', 'command': command,
                                                'runtime': 'windows', 'timeout_seconds': 600})
    return job, counter


def wait_until(check, message, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(.15)
    raise TimeoutError(message)


def exit_fixture(api, birth):
    try:
        with httpx.Client(trust_env=False, timeout=10) as client:
            client.post(api.base+'/api/v1/test/typed-publication-exit',
                        headers={'Authorization': 'Bearer '+api.token}, json={'pid': birth['pid']})
    except httpx.TransportError:
        pass
    else:
        raise AssertionError('The isolated worker returned a response while exiting')
    api.proc.wait(timeout=15)
    return api.proc.returncode


def check_object(store, key, expected, size_bytes):
    response = store.raw_client.get_object(Bucket=store.bucket, Key=key)
    checksum = hashlib.sha256()
    size = 0
    with response['Body'] as body:
        for data in iter(lambda: body.read(1024*1024), b''):
            checksum.update(data)
            size += len(data)
    assert (size, checksum.hexdigest()) == (size_bytes, expected)
    return response['ETag']


def check_public(api, path, expected, size_bytes):
    digest = hashlib.sha256()
    size = 0
    headers = {'Authorization': 'Bearer ' + api.token}
    with httpx.Client(trust_env=False, timeout=45) as client:
        with client.stream('GET', api.base + '/api/v1' + api.prefix + '/raw',
                           params={'path': path}, headers=headers) as response:
            response.raise_for_status()
            assert int(response.headers['content-length']) == size_bytes
            for block in response.iter_bytes(1024*1024):
                digest.update(block)
                size += len(block)
    assert size == size_bytes and digest.hexdigest() == expected


def published(api, store, job_id, counter, size_bytes):
    job = api.wait_job(job_id, timeout=540)
    attempt = job['attempt']
    publication = attempt['outputs']['publication']
    assert publication['status'] == 'committed'
    prefix = attempt['workspace']['storage']['prefix']
    immutable = '.vibeflow/publications/' + publication['id']
    manifest = api.call('GET', api.prefix + '/raw?path=' + immutable + '/manifest.json')
    expected, = counter.read_text(encoding='utf8').splitlines()
    assert manifest['descriptor']['handler'] == 'case-command.v1'
    assert manifest['files'][MEMBER]['size'] == size_bytes
    assert manifest['files'][MEMBER]['sha256'] == expected
    physical_key = prefix + '/' + immutable + '/files/' + MEMBER
    version = check_object(store, physical_key, expected, size_bytes)
    check_public(api, immutable + '/files/' + MEMBER, expected, size_bytes)
    return {'job_id': job_id, 'attempt_id': attempt['id'], 'publication_id': publication['id'],
            'prefix': prefix, 'key': physical_key, 'sha256': expected, 'etag': version}


def settled_alias(api, job_id):
    def settled():
        job = api.call('GET', f'/jobs/{job_id}')
        completion = job['attempt'].get('outputs', {}).get('completion') or {}
        return job if completion.get('state') == 'settled' else None
    return wait_until(settled, 'The compatibility alias did not reach its terminal receipt', 60)


def verify_alias_conflict(api, store, size_mib):
    case = api.create_case('External S3 destination edit wins over alias plan')
    (api.root/'arm-alias-conflict').write_text(str(case['id']), encoding='utf8')
    job, counter = delivery(api, case, size_mib, CONFLICT_PRODUCER)
    gate = api.root/'alias-conflict-gate.json'
    wait_until(gate.exists, 'The alias did not reach its durable plan', 90)
    pending = json.loads(gate.read_text(encoding='utf8'))
    path = 'D-post/small-fixture.txt'
    try:
        store.client.put_object(Bucket=store.bucket, Key=pending['prefix'] + '/' + path,
                                Body=b'external destination edit', IfNoneMatch='*')
    finally:
        (api.root/'release-alias-conflict').touch()
    final = settled_alias(api, job['id'])
    outputs = final['attempt']['outputs']
    assert final['status'] == 'succeeded' and outputs['publication']['id'] == pending['publication_id']
    assert outputs['completion']['alias_error'] == 'ValueError'
    assert api.call('GET', api.prefix+'/raw?path='+path, raw=True) == b'external destination edit'
    api.call('GET', api.prefix+'/raw?path=D-post/companion.txt', expect=404)
    immutable = '.vibeflow/publications/'+pending['publication_id']+'/files/'
    assert api.call('GET', api.prefix+'/raw?path='+immutable+path, raw=True) == b'generated alias'
    assert api.call('GET', api.prefix+'/raw?path='+immutable+'D-post/companion.txt', raw=True) == b'generated companion'
    assert counter.read_text(encoding='utf8').splitlines() == ['ran once']
    return {'job_id': job['id'], 'state': outputs['completion']['alias_error'],
            'external_edit_preserved': True, 'companion_alias_not_applied': True, 'immutable_artifacts_verified': True}


def run_fixture(database, store, size_mib, source_sharing, alias_streaming):
    from app.services import process_identity_native as identity

    api = IsolatedServer('typed-publication-streaming', app='stream_fixture:app', database_url=database)
    size_bytes = size_mib * 1024 * 1024
    (api.root/'member-size.txt').write_text(str(size_bytes), encoding='utf8')
    if source_sharing:
        (api.root/'exercise-source-sharing').touch()
    if alias_streaming:
        (api.root/'exercise-alias-streaming').touch()
    (api.root/'stream_fixture.py').write_text('import os\nos.environ.update(' + repr(store.settings()) + ')\n' + FIXTURE,
                                             encoding='utf8')
    print('TYPED_PUBLICATION_STREAMING ' + str(api.root) + ' STORE ' + str(store.root), flush=True)
    evidence = {'passed': False, 'database': 'postgresql' if database else 'sqlite',
                'member_bytes': size_bytes, 'source_sharing_test': source_sharing,
                'real_model_requests': 0, 'mock_llm_calls': 0}
    births = []
    try:
        with api:
            case = api.create_case('Normal typed artifact stream')
            births.append(api.call('GET', '/test/typed-publication-process'))
            job, counter = delivery(api, case, size_mib)
            normal = published(api, store, job['id'], counter, size_bytes)
            if alias_streaming:
                assert settled_alias(api, job['id'])['attempt']['outputs']['completion']['alias_error'] is None
                check_public(api, MEMBER, normal['sha256'], size_bytes)
            evidence['normal'] = normal

            (api.root/'arm-interruption').touch()
            case = api.create_case('Typed artifact stream recovered after API exit')
            job, counter = delivery(api, case, size_mib)
            email = api.call('GET', '/auth/me')['email']
            gate = api.root/'unacknowledged-put.json'
            wait_until(gate.exists, 'Publisher did not reach the completed PUT', 120)
            pending = json.loads(gate.read_text(encoding='utf8'))
            assert pending['pid'] == births[-1]['pid']
            detail = api.call('GET', f'/jobs/{job["id"]}')
            attempt = detail['attempt']
            assert detail['status'] == 'running' and attempt['phase'] == 'finalizing'
            uploaded = store.raw_client.head_object(Bucket=store.bucket, Key=pending['key'])
            assert uploaded['ContentLength'] == size_bytes
            original_etag = uploaded['ETag']
            evidence['original_put'] = pending
            evidence['interrupted_job'] = {'status': detail['status'], 'phase': attempt['phase'],
                                           'attempt_id': attempt['id']}
            evidence['original_launcher_exit_code'] = exit_fixture(api, births[-1])
            assert identity.probe(births[-1])['state'] == 'exited'
            api.__exit__(RuntimeError, None, None)
            (api.root/'arm-interruption').unlink()
            api.__enter__()
            api.token = api.call('POST', '/auth/login', {'email': email, 'password': 'isolated-test-123'})['token']
            births.append(api.call('GET', '/test/typed-publication-process'))
            api.case_id, api.prefix = case['id'], f'/cases/{case["id"]}'
            recovered = published(api, store, job['id'], counter, size_bytes)
            assert recovered['attempt_id'] == attempt['id'] and recovered['key'] == pending['key']
            assert recovered['etag'] == original_etag
            evidence['recovered'] = recovered
            if alias_streaming:
                assert settled_alias(api, job['id'])['attempt']['outputs']['completion']['alias_error'] is None
                check_public(api, MEMBER, recovered['sha256'], size_bytes)
                evidence['alias_conflict'] = verify_alias_conflict(api, store, size_mib)
            records = [json.loads(line) for line in (api.root/'publication-events.jsonl').read_text(encoding='utf8').splitlines()]
            evidence['publication_events'] = records
            if source_sharing:
                cleanup, = [row for row in records if row['kind'] == 'source_cleanup_sharing']
                assert cleanup['winerrors'] and set(cleanup['winerrors']) <= {32, 33}
                assert cleanup['owned_scratch_removed'] and cleanup['elapsed'] < 3
                evidence['source_cleanup'] = cleanup
            for item, statuses in [(normal, [200]), (recovered, [200, 412])]:
                sent = [event for event in records if event.get('key') == item['key']]
                assert [event['status'] for event in sent] == statuses
                scratch = api.root/'files'/'.node-storage-budget'
                assert all(not (scratch/name).exists() for event in sent for name in event['node_claims'])
                peak, = [event['peak'] for event in records if event['kind'] == 'memory' and event['prefix'] == item['prefix']]
                assert peak < 12*1024*1024
                item['peak_traced_bytes'] = peak
                item['publication_node_claims_released'] = True
                if alias_streaming:
                    selected, = [event for event in records if event['kind'] == 'alias_body_put'
                                 and event['prefix'] == item['prefix']]
                    assert selected['status'] == 200
                    assert all(not (scratch/name).exists() for name in selected['node_claims'])
                    item['alias'] = {'public_sha256': item['sha256'], 'put': selected}
                    for phase in ('prepare', 'apply'):
                        peak, = [event['peak'] for event in records if event['kind'] == f'alias_{phase}_memory'
                                 and event['prefix'] == item['prefix']]
                        assert peak < 16 * 1024 * 1024, (phase, peak)
                        item['alias'][phase+'_peak_traced_bytes'] = peak
            evidence['recovery_launcher_exit_code'] = exit_fixture(api, births[-1])
            evidence['passed'] = True
    except BaseException as error:
        evidence['failure'] = {'type': type(error).__name__, 'message': str(error)[:1500]}
        raise
    finally:
        deadline = time.monotonic() + 10
        probes = [identity.probe(birth) for birth in births]
        while any(probe['state'] != 'exited' for probe in probes) and time.monotonic() < deadline:
            time.sleep(.05)
            probes = [identity.probe(birth) for birth in births]
        evidence['api_exits'] = probes
        evidence['passed'] = evidence['passed'] and all(probe['state'] == 'exited' for probe in probes)
        evidence['source_sha256'] = {
            str(path.relative_to(SERVER)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (Path(__file__), SERVER/'app/services/attempt_objects.py',
                         SERVER/'app/services/publication_objects.py', SERVER/'app/services/publication_storage.py',
                         SERVER/'app/services/s3_case_files.py', SERVER/'app/services/fs_directory.py',
                         SERVER/'app/services/publication_alias_objects.py', SERVER/'app/services/s3_live_store.py')}
        (api.root/'typed-publication-streaming.json').write_text(json.dumps(evidence, indent=2), encoding='utf8')
    assert evidence['passed']
    return api.root


async def main(postgres, size_mib, source_sharing, alias_streaming):
    from live_llm_terminal_agent_acceptance import disposable_database

    store = ObjectStore(executable=RUNTIME, volume_max=24)
    async with disposable_database(postgres) as database:
        with store:
            result = await asyncio.to_thread(run_fixture, database, store, size_mib, source_sharing, alias_streaming)
    assert store.proc.poll() is not None
    (result/'s3-cleanup.json').write_text(json.dumps({'root': str(store.root), 'stopped': True}), encoding='utf8')
    print('TYPED_PUBLICATION_STREAMING_OK ' + str(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--size-mib', type=int, default=40)
    parser.add_argument('--source-sharing', action='store_true')
    parser.add_argument('--alias-streaming', action='store_true')
    args = parser.parse_args()
    if not 16 <= args.size_mib <= 64:
        parser.error('--size-mib must be between 16 and 64')
    asyncio.run(main(args.postgres, args.size_mib, args.source_sharing, args.alias_streaming))
