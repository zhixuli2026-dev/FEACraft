"""Exercise live engineering evidence hashes against real filesystem and SeaweedFS files."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

import httpx

from s3testsupport import ObjectStore
from testsupport import SERVER, IsolatedServer


PATH = 'C-run/evidence/constant/polyMesh/points'
SIZE = 40 * 1024 * 1024
LABEL = '当前网格'
RUNTIME = SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed'

FIXTURE = r'''
import asyncio
import os
from pathlib import Path
import threading
import time
import tracemalloc

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.routers.workspaces import get_owned_case
from app.services import case_evidence, file_read, process_identity_native

root = Path(__file__).parent
worker = threading.local()
real_s3 = storage.s3
chunks = file_read.FileRead.chunks

def selected_s3():
    override = getattr(worker, 'anonymous', None)
    return override if override is not None else real_s3()

def witnessed_body(self, *args, **kwargs):
    yield from chunks(self, *args, **kwargs)
    if getattr(worker, 'after_read', False):
        (root/'evidence-body-drained').touch()
        deadline = time.monotonic() + 15
        while not (root/'release-evidence-check').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Evidence test writer did not replace the selected file')
            time.sleep(.02)

storage.s3 = selected_s3
file_read.FileRead.chunks = witnessed_body

def evaluate(prefix, records, mode):
    worker.after_read = mode == 'replace'
    worker.anonymous = None
    if mode == 'denied':
        worker.anonymous = boto3.client('s3', endpoint_url=storage.get_settings().s3_endpoint,
            aws_access_key_id='anonymous-fixture', aws_secret_access_key='unused-for-unsigned',
            config=Config(signature_version=UNSIGNED, retries={'total_max_attempts':1},
                          s3={'addressing_style':'path'}))
    measured = mode == 'measure'
    if measured:
        tracemalloc.start()
    try:
        reason = case_evidence.verify_records(prefix, records, label='当前网格', required=True)
        return {'reason':reason, 'peak_traced_bytes':tracemalloc.get_traced_memory()[1] if measured else None}
    except ClientError as error:
        return {'service_status':error.response.get('ResponseMetadata',{}).get('HTTPStatusCode'),
                'service_code':error.response.get('Error',{}).get('Code')}
    finally:
        if measured:
            tracemalloc.stop()
        if worker.anonymous is not None:
            worker.anonymous.close()
        del worker.anonymous, worker.after_read

@app.get('/fixture/evidence/{case_id}/binding')
async def binding(case_id: int, user=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    return {'prefix':case.s3_prefix, 'api_process':process_identity_native.observe(os.getpid())}

@app.post('/fixture/evidence/{case_id}/check')
async def verify(case_id: int, body: dict, user=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    prefix = case.s3_prefix
    await db.rollback()
    return await asyncio.to_thread(evaluate, prefix, body['records'], body['mode'])

@app.post('/fixture/evidence:stop')
async def stop(body: dict, user=Depends(get_current_user)):
    assert body['pid'] == os.getpid()
    os._exit(79)
'''


def source_file(root):
    asset = root / 'mesh-source.bin'
    digest = hashlib.sha256()
    with asset.open('xb') as out:
        for _ in range(SIZE // (1024 * 1024)):
            block = os.urandom(1024 * 1024)
            out.write(block)
            digest.update(block)
    return asset, digest.hexdigest()


def shutdown(api, process):
    with httpx.Client(trust_env=False, timeout=10) as client:
        try:
            client.post(api.base+'/fixture/evidence:stop', json={'pid':process['pid']},
                        headers={'Authorization': 'Bearer '+api.token})
        except httpx.TransportError:
            pass
        else:
            raise AssertionError('Isolated verifier returned before its explicit exit')
    api.proc.wait(timeout=15)
    return api.proc.returncode


def exercise(api, store, report):
    case = api.create_case('Engineering evidence byte integrity')
    route = f'/fixture/evidence/{case["id"]}'
    binding = api.call('GET', route+'/binding', api_prefix=False)
    prefix = binding['prefix']
    process = binding['api_process']
    report['api_process'] = process
    asset, expected = source_file(api.root)
    large = {'path': PATH, 'sha256': expected}
    empty = {'path':PATH+'.empty', 'sha256':hashlib.sha256(b'').hexdigest()}

    def install(path, source=None):
        if store:
            key = prefix+'/'+path
            if source is None:
                store.client.put_object(Bucket=store.bucket, Key=key, Body=b'')
            else:
                with source.open('rb') as body:
                    store.client.put_object(Bucket=store.bucket, Key=key, Body=body)
            return store.client.head_object(Bucket=store.bucket, Key=key)['ETag']
        root = (api.root/'files'/prefix).resolve()
        target = (root/path).resolve()
        assert root.is_relative_to(api.root.resolve()) and target.is_relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        scratch = target.with_suffix('.owned-new')
        if source is None:
            scratch.touch()
        else:
            shutil.copyfile(source, scratch)
        scratch.replace(target)
        file_info = target.stat()
        return [file_info.st_dev, file_info.st_ino, file_info.st_mtime_ns, file_info.st_ctime_ns]

    def verify(records, mode='check'):
        return api.call('POST', route+'/check', {'records': records, 'mode': mode}, api_prefix=False)

    opening = install(PATH, asset)
    install(empty['path'])
    result = verify([large], 'measure')
    assert result['reason'] is None and result['peak_traced_bytes'] < 8*1024*1024, result
    report['large_without_size_field'] = result
    assert verify([empty])['reason'] is None
    report['empty'] = True
    missing = verify([{**large, 'path':PATH+'.absent'}])['reason']
    assert missing == LABEL+f'文件缺失或不可读，现有证据已过期: {PATH}.absent', missing
    report['missing'] = missing
    changed = LABEL+f'文件已变化，现有证据已过期: {PATH}；请重新计算'
    assert verify([{**large,'sha256':'0'*64}])['reason'] == changed
    report['mismatch'] = changed
    with ThreadPoolExecutor(max_workers=1) as workers:
        query = workers.submit(verify, [large], 'replace')
        try:
            deadline = time.monotonic()+12
            while not (api.root/'evidence-body-drained').exists():
                if query.done():
                    raise AssertionError(query.result())
                if time.monotonic() >= deadline:
                    raise TimeoutError('Verifier did not finish the pinned body')
                time.sleep(.02)
            replacement = install(PATH, asset)
            assert replacement != opening
        finally:
            (api.root/'release-evidence-check').touch()
        assert query.result(timeout=20)['reason'] == changed
    assert verify([large])['reason'] is None
    report['same_content_replacement'] = {'rejected_original_read':True, 'new_attempt_verified':True,
                                          'old_identity': opening, 'new_identity': replacement}
    if store:
        denied = verify([large], 'denied')
        assert denied.get('service_status') == 403 and denied.get('service_code'), denied
        report['real_store_denial'] = denied
    report['api_exit_code'] = shutdown(api, process)


def run(backend):
    from app.services import process_identity_native

    api = IsolatedServer('case-evidence-'+backend, app='evidence_fixture:app')
    context = ObjectStore(executable=RUNTIME, volume_max=12, authenticated=True) if backend == 's3' else nullcontext()
    report = {'passed':False, 'backend':backend, 'size':SIZE, 'real_model_requests':0, 'mock_llm_calls':0}
    print('CASE_EVIDENCE_STREAMING ' + str(api.root), flush=True)
    try:
        with context as store:
            settings = store.settings() if store else {}
            (api.root/'evidence_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,
                                                        encoding='utf8')
            with api:
                exercise(api, store, report)
        if store:
            assert store.proc.poll() is not None
            report['s3_stopped'] = True
        report['api_exit_proof'] = process_identity_native.probe(report['api_process'])
        assert report['api_exit_proof']['state'] == 'exited'
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__, 'message':str(error)[:1000]}
        raise
    finally:
        report['source_sha256'] = {str(path.relative_to(SERVER)):hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in (Path(__file__), SERVER/'app/services/case_evidence.py',
                                                SERVER/'app/services/file_read.py')}
        (api.root/'case-evidence-streaming.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print('CASE_EVIDENCE_STREAMING_OK ' + str(api.root), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('fs', 's3'), required=True)
    run(parser.parse_args().backend)
