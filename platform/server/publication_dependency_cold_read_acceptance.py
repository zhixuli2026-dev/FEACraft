"""Verify public dependency reads against real native and S3 artifacts."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import httpx

from app.services.process_identity_native import probe
from live_llm_terminal_agent_acceptance import disposable_database
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


LARGE = 'D-post/dependency-large.bin'
SMALL = 'D-post/dependency-note.txt'
SIZE = 40 * 1024 * 1024
NOTE = b'owned marker\n'
PRODUCER = f'''
from pathlib import Path

with Path({LARGE!r}).open('wb') as output:
    for _ in range(40):
        output.write(b'v' * (1024 * 1024))
Path({SMALL!r}).write_bytes({NOTE!r})
print('created both native publication members')
'''

APP = '''
import json
import os
from pathlib import Path
import time
import tracemalloc
from fastapi import Depends

from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.models import ArtifactPublication, JobAttempt
from app.routers.workspaces import get_owned_case
from app.services import attempt_objects, publication_inputs, publication_objects
from app.services.case_read_capacity import reading
from app.services.job_execution import completion_visibility
from app.services.process_identity_native import observe

root = Path(__file__).parent
verify = publication_objects.verified_manifest
verify_members = attempt_objects.verify_members

def measured(prefix, row):
    if not (root/'measure').is_file():
        return verify(prefix, row)
    tracemalloc.start()
    try:
        return verify(prefix, row)
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        (root/'verify-peak.json').write_text(json.dumps({'allocations_peak':peak}), encoding='utf8')

publication_objects.verified_manifest = measured

def pause_after_members(prefix, base, records, **options):
    result = verify_members(prefix, base, records, **options)
    if base.startswith('.vibeflow/publications/') and (root/'hold-members').is_file():
        (root/'hold-members').unlink()
        (root/'verified-members').touch()
        end = time.monotonic() + 15
        while not (root/'release-members').is_file():
            if time.monotonic() >= end:
                raise TimeoutError('Owner did not release the actual verification')
            time.sleep(.02)
    return result

attempt_objects.verify_members = pause_after_members

@app.on_event('startup')
async def birth():
    (root/'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')

@app.get('/fixture/visibility/{publication_id}')
async def visibility(publication_id: str, user=Depends(get_current_user), db=Depends(get_db)):
    row = await db.get(ArtifactPublication, publication_id)
    await get_owned_case(row.case_id, user, db, write=False)
    attempt = await db.get(JobAttempt, row.attempt_id)
    return {'visibility':completion_visibility(attempt), 'status':row.status}

@app.post('/fixture/references/{case_id}')
async def references(case_id: int, body: dict, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    async with reading(db, case):
        values = await publication_inputs.resolve(db, case, body['selections'],
                        materialize=body.get('mount', False))
        source, = values
        await publication_inputs.validate(db, case, {'publications':[source.reference], 'files':source.files})
        return {'files':source.files, 'root':str(source.root) if source.root else None}

@app.post('/fixture/stop', status_code=204)
async def stop(body: dict, user=Depends(get_current_user)):
    assert body['pid'] == os.getpid()
    os._exit(81)
'''


def exit_api(api):
    birth = json.loads((api.root/'api-birth.json').read_text(encoding='utf8'))
    with httpx.Client(timeout=10, trust_env=False) as client:
        try:
            client.post(api.base+'/fixture/stop', json={'pid':birth['pid']},
                        headers={'Authorization':'Bearer '+api.token})
        except (httpx.RemoteProtocolError, httpx.ReadError):
            pass
    api.proc.wait(20)
    result = probe(birth)
    assert api.proc.returncode == 81 and result['state'] == 'exited'
    return result


def exercise(api, store, report):
    case = api.create_case('Cold dependency verification')
    api.put('Ref/artifact-maker.py', PRODUCER)
    command = subprocess.list2cmdline([sys.executable, '-B', 'Ref/artifact-maker.py'])
    job = api.call('POST', api.prefix+'/jobs', {'type':'convert', 'runtime':'windows',
                   'command':command, 'timeout_seconds':120})
    completed = api.wait_job(job['id'])
    attempt = completed['attempt']
    assert attempt['process_stop']['verified'] is True
    artifact = attempt['outputs']['publication']
    assert artifact['status'] == 'committed'
    identifier = artifact['id']
    metadata = api.call('GET', '/fixture/visibility/'+identifier, api_prefix=False)
    assert metadata['status'] == 'committed' and metadata['visibility'] != 'pending', metadata
    prefix = api.call('GET', api.prefix)['storage_prefix']
    case_root = (api.root/'files'/prefix).resolve()
    path = case_root/'.vibeflow'/'publications'/identifier
    parked = api.root/'original-read-cache'
    assert path.resolve().is_relative_to(case_root) and case_root.is_relative_to(api.root.resolve())
    if store and path.exists():
        path.rename(parked)
    assert (not path.exists()) if store else path.is_dir()

    route = api.prefix+'/dependencies'

    def selected_node():
        graph = api.call('GET', route)
        return next(node for node in graph['publications'] if node['publication_id'] == identifier)

    (api.root/'measure').touch()
    observed = selected_node()
    (api.root/'measure').unlink()
    assert observed['status'] == 'fresh', observed
    if store:
        assert not path.exists(), 'Advisory API materialized the cold S3 publication'
        peak = json.loads((api.root/'verify-peak.json').read_text(encoding='utf8'))['allocations_peak']
        assert peak < SIZE // 4, peak
        report['immutable_bundle_traced_bytes'] = peak
        key = f'{prefix}/.vibeflow/publications/{identifier}/files/{SMALL}'
        (api.root/'hold-members').touch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(selected_node)
            try:
                until = time.monotonic() + 10
                while not (api.root/'verified-members').exists():
                    assert not pending.done() and time.monotonic() < until
                    time.sleep(.02)
                store.client.put_object(Bucket=store.bucket, Key=key, Body=b'other marker\n')
            finally:
                (api.root/'release-members').touch()
            changed = pending.result(timeout=10)
        assert changed['status'] == 'unverifiable' and not path.exists(), changed
        store.client.put_object(Bucket=store.bucket, Key=key, Body=NOTE)
        report['post_hash_replacement_rejected'] = True
    report['fresh_graph'] = True

    references = '/fixture/references/'+str(case['id'])
    expected = {'publication_id': identifier, 'paths':[LARGE], 'require_current':False}
    reference_result = api.call('POST', references, {'selections':[expected]}, api_prefix=False)
    member = reference_result['files'][LARGE]
    digest = hashlib.sha256()
    for _ in range(40):
        digest.update(b'v' * (1024 * 1024))
    assert member['size'] == SIZE and member['sha256'] == digest.hexdigest()
    if store:
        assert reference_result['root'] is None and not path.exists()
    report['historical_reference_verified'] = True

    mounted = api.call('POST', references, {'selections':[expected], 'mount':True}, api_prefix=False)
    mounted_file = Path(mounted['root'])/LARGE
    assert mounted_file == path/'files'/LARGE
    with mounted_file.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == digest.hexdigest()
    report['normal_mount_verified'] = True

    corrupt = b'other marker\n'
    assert len(corrupt) == len(NOTE)
    if store:
        # Remove the new cache as well. A corrupt remote member must stay
        # rejected even though a copy of the original bytes exists on the node.
        path.rename(api.root/'materialized-read-cache')
        key = f'{prefix}/.vibeflow/publications/{identifier}/files/{SMALL}'
        store.client.put_object(Bucket=store.bucket, Key=key, Body=corrupt)
        api.call('POST', references, {'selections':[expected]}, api_prefix=False, expect=409)
        assert not path.exists()
    else:
        (path/'files'/SMALL).write_bytes(corrupt)
    broken = selected_node()
    assert broken['status'] == 'unverifiable' and broken['reasons'] == [{'code':'bundle_unverifiable'}], broken
    if store:
        assert not path.exists()
    report.update(corrupted_artifact_rejected=True, native_job_id=job['id'], bytes=SIZE,
                  verified_digest=digest.hexdigest(), publication_id=identifier,
                  immutable_cache_after_graph=False if store else None)


def verify(database, store):
    backend = 's3' if store else 'fs'
    api = IsolatedServer('cold-dependency-'+backend, app='dependency_fixture:app', database_url=database)
    settings = store.settings() if store else {}
    (api.root/'dependency_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+APP,
                                                   encoding='utf8')
    print('COLD_DEPENDENCY_EVIDENCE '+str(api.root), flush=True)
    report = {'passed':False,'backend':backend,'database':'postgresql' if database else 'sqlite',
              'real_model_requests':0,'mock_llm_calls':0}
    try:
        with api:
            try:
                exercise(api, store, report)
            finally:
                if api.proc.poll() is None and api.token:
                    report['api_exit'] = exit_api(api)
        report['passed'] = True
    finally:
        (api.root/'cold-dependency-read.json').write_text(json.dumps(report,indent=2), encoding='utf8')
    print('COLD_DEPENDENCY_OK '+str(api.root), flush=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('fs','s3'), required=True)
    use_s3 = parser.parse_args().backend == 's3'
    runtime = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    context = ObjectStore(executable=runtime, volume_max=12, authenticated=True) if use_s3 else nullcontext()
    async with disposable_database(use_s3) as database:
        with context as store:
            await asyncio.to_thread(verify, database, store)
    if use_s3:
        assert context.proc is not None and context.proc.poll() is not None


if __name__ == '__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    asyncio.run(main())
