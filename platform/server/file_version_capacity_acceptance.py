"""Measure the real version endpoint while an independent writer replaces its source."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import time
from urllib.parse import urlencode

from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER

DIGEST_TEST_SECONDS = 5

FIXTURE = '''
import json, time, tracemalloc
from pathlib import Path
from fastapi import Depends
from app.main import app
from app.db import get_db
from app.deps import get_current_user
from app.routers import files
from app.routers.workspaces import get_owned_case
from app.services import case_write

root = Path(__file__).parent
@app.get('/fixture/{case_id}/prefix')
async def prefix(case_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    return case.s3_prefix

module, name = (case_write, 'file_record') if hasattr(files, 'read_file_record') else (files, 'file_hash')
original = getattr(module, name)
def observed(prefix, path):
    if path.startswith('Ref/held-') and (root/'hold-workers').exists():
        record = original(prefix, path)
        (root/path.split('/')[-1]).touch()
        end = time.monotonic()+20
        while not (root/'release-workers').exists():
            if time.monotonic()>=end:
                raise TimeoutError('Worker observation not released')
            time.sleep(.02)
        return record
    if path != 'Ref/version-large.bin':
        return original(prefix, path)
    tracemalloc.start()
    try:
        value = original(prefix, path)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    (root/'peak.json').write_text(json.dumps({'peak': peak}))
    if (root/'hold-version').exists():
        (root/'digest-ready').touch()
        end = time.monotonic()+20
        while not (root/'release-version').exists():
            if time.monotonic() >= end:
                raise TimeoutError('External file replacement did not finish')
            time.sleep(.02)
    return value
setattr(module, name, observed)
'''


def check_capacity(api):
    requests = []
    for index in range(5):
        api.create_case('Independent version reader '+str(index))
        path = 'Ref/held-'+str(index)
        api.put(path, 'observed')
        requests.append((api.prefix+'/file:version?'+urlencode({'path':path}), {'Authorization':'Bearer '+api.token}))

    def read(index, expected):
        route, headers = requests[index]
        return api.call('GET',route,expect=expected,extra_headers=headers)

    (api.root/'hold-workers').touch()
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(read, i, 504) for i in range(4)]
        try:
            end = time.monotonic()+12
            while not all((api.root/('held-'+str(i))).exists() for i in range(4)):
                if time.monotonic()>=end:
                    raise TimeoutError('Four independent readers did not enter')
                time.sleep(.02)
            assert api.call('GET','/api/health',api_prefix=False)['ok']
            assert read(4,429)['detail']['code']=='file_version_busy'
            time.sleep(DIGEST_TEST_SECONDS+.5)
            assert not any(f.done() for f in pending)
            assert read(4,429)['detail']['code']=='file_version_busy'
        finally:
            (api.root/'release-workers').touch()
        for f in pending:
            assert f.result(timeout=10)['detail']['code']=='file_version_timeout'
    assert read(4,200)['content_sha256']==hashlib.sha256(b'observed').hexdigest()
    return {'active':4, 'fifth':429, 'fifth_while_draining':429, 'timed_out':4, 'after_drain':200}


def verify(args, database_url=None):
    backend = args.backend
    api = IsolatedServer('file-version-'+backend, app='version_fixture:app', database_url=database_url)
    result = {'backend': backend, 'database': 'postgresql' if database_url else 'sqlite', 'passed': False}
    print(api.root, flush=True)
    context = ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') if backend=='s3' else nullcontext()
    try:
        with context as store:
            environment = store.settings() if store else {}
            if database_url:
                environment['VIBEFLOW_STORAGE_MEASUREMENT_TIMEOUT_SECONDS'] = str(DIGEST_TEST_SECONDS)
            settings = 'import os\nos.environ.update('+repr(environment)+')\n'
            (api.root/'version_fixture.py').write_text(settings+FIXTURE, encoding='utf8')
            with api:
                case = api.create_case('File version consistency')
                prefix = api.call('GET',f"/fixture/{case['id']}/prefix",api_prefix=False).rstrip('/')

                def install(path, data):
                    if store:
                        store.client.put_object(Bucket=store.bucket, Key=prefix+'/'+path, Body=data)
                    else:
                        target = api.root/'files'/prefix/path
                        temporary = target.with_suffix('.new')
                        temporary.write_bytes(data)
                        temporary.replace(target)

                def version(path):
                    return api.call('GET',api.prefix+'/file:version?'+urlencode({'path':path}))

                large_path = 'Ref/version-large.bin'
                large = bytes(range(256))*(128*1024)+b'end'
                replacement = b'new version'
                install(large_path, large)
                (api.root/'hold-version').touch()
                with ThreadPoolExecutor(max_workers=1) as queries:
                    query = queries.submit(version, large_path)
                    try:
                        end = time.monotonic()+25
                        while not (api.root/'digest-ready').exists():
                            if query.done():
                                raise AssertionError(('no version observation',query.result()))
                            if time.monotonic() >= end:
                                raise TimeoutError('Hash was not observed')
                            time.sleep(.02)
                        install(large_path, replacement)
                    finally:
                        (api.root/'release-version').touch()
                    first = query.result(timeout=30)
                result.update(source_bytes=len(large), first=first,
                              peak_bytes=json.loads((api.root/'peak.json').read_text())['peak'])
                assert first['content_sha256'] == hashlib.sha256(large).hexdigest()
                assert first['size'] == len(large), 'Returned a digest and size from different versions'
                second = version(large_path)
                assert second['size']==len(replacement) and second['content_sha256']==hashlib.sha256(replacement).hexdigest()
                for path, data in [('Ref/空.txt',b''),('Ref/missing.txt',None)]:
                    if data is not None:
                        install(path,data)
                    row = version(path)
                    assert row['exists']==(data is not None) and row['size']==0
                    assert row['content_sha256']==(None if data is None else hashlib.sha256(data).hexdigest())
                assert result['peak_bytes'] < len(large)//3
                result['second'] = second
                api.put('C-run/case-1/system/controlDict', 'application icoFoam;\n')
                derived = api.call('POST',api.prefix+'/runs:derive',{'base':'C-run/case-1','name':'version-check'})
                assert derived['ok'] and derived['created']=='C-run/version-check'
                assert version('C-run/version-check/system/controlDict')['content_sha256']==version('C-run/case-1/system/controlDict')['content_sha256']
                copied = api.call('POST',api.prefix+':copy',{'name':'Version-checked copy'})
                copied_record = api.call('GET',f"/cases/{copied['id']}/file:version?"+urlencode({'path':large_path}))
                assert copied_record['size']==second['size'] and copied_record['content_sha256']==second['content_sha256']
                result.update(derived=derived['created'],copied_id=copied['id'])
                if database_url:
                    result['reader_limit'] = check_capacity(api)
                result['passed'] = True
    finally:
        (api.root/'version-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result, api.root


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend',choices=('fs','s3'),required=True)
    parser.add_argument('--postgres',action='store_true')
    args = parser.parse_args()
    if args.postgres:
        from workflow_resume_acceptance import postgres
        raise SystemExit(asyncio.run(postgres(args,verify_callback=verify)))
    verify(args)
