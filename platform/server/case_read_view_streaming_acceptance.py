"""Exercise the production case read view on actual filesystem and S3 backends."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid

import httpx

from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


ASSET = 'Ref/large-view-input.bin'
EMPTY = 'Ref/empty-view-input.txt'
SIZE = 40*1024*1024
RUNTIME = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
LEGACY_RUNTIME = SERVER.parents[1]/'third_party/seaweed-runtime/weed'

FIXTURE = r'''
import asyncio
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time
import tracemalloc

from botocore.exceptions import ClientError
from fastapi import Depends, HTTPException
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.models import Case, Workspace
from app.routers.workspaces import get_owned_case
from app.services import case_evidence, case_read_view, process_identity_native, s3_live_store
from app.services.case_write import CaseWriteConflict
from app.services.request_deadline import current, scope

root = Path(__file__).parent
context = ContextVar('a26_case_read_view', default=None)
real_get = s3_live_store.Client.get_object

class ObservedBody:
    def __init__(self, real, state, get):
        self.real, self.state, self.get = real, state, get

    def read(self, count):
        self.get['max_read'] = max(count, self.get['max_read'])
        if self.get['key'].endswith('/large-view-input.bin') and self.get['received'] and not self.get['fenced']:
            self.get['fenced'] = True
            if self.state['mode']=='replace':
                (root/'copy-began').touch()
                deadline = time.monotonic()+15
                while not (root/'copy-release').exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Fixture replacement missed its copy fence')
                    time.sleep(.02)
            elif self.state['mode']=='timeout':
                deadline = current()
                assert deadline is not None
                while not deadline.expired:
                    time.sleep(.02)
        chunk = self.real.read(count)
        self.get['received'] += len(chunk)
        return chunk

    def close(self):
        try:
            self.real.close()
        finally:
            self.get['closed'] = True

def witnessed_get(self, **params):
    state = context.get()
    if state is None or '/Ref/' not in params['Key']:
        return real_get(self, **params)
    result = {'key':params['Key'], 'if_match':params.get('IfMatch'), 'received':0,
              'max_read':0, 'closed':False, 'fenced':False}
    state['gets'].append(result)
    try:
        response = real_get(self, **params)
    except ClientError as error:
        result['status'] = error.response['ResponseMetadata']['HTTPStatusCode']
        raise
    result['status'] = response['ResponseMetadata']['HTTPStatusCode']
    response['Body'] = ObservedBody(response['Body'], state, result)
    return response

s3_live_store.Client.get_object = witnessed_get

@app.get('/fixture/view/{case_id}/binding')
async def binding(case_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    return {'prefix':case.s3_prefix,'process':process_identity_native.observe(os.getpid())}

@app.post('/fixture/view:legacy-case')
async def legacy(body: dict, user=Depends(get_current_user), db=Depends(get_db)):
    ws = await db.get(Workspace,body['workspace_id'])
    if ws is None or ws.user_id!=user.id:
        raise HTTPException(404)
    case = Case(workspace_id=ws.id,name='pre-catalog case fixture',s3_prefix='')
    db.add(case)
    await db.flush()
    case.s3_prefix = f'workspaces/{user.id}/cases/{case.id}'
    await db.commit()
    return {'id':case.id,'prefix':case.s3_prefix}

@app.post('/fixture/view/{case_id}')
async def verify(case_id: int, body: dict, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    prefix = case.s3_prefix
    await db.rollback()
    mode, paths = body['mode'],body['paths']
    state = {'mode':mode,'gets':[]}
    token = context.set(state)
    result = {'entered':False}
    if mode=='measure':
        tracemalloc.start()
    try:
        async def consume():
            async with case_read_view.async_view(prefix,paths) as view:
                result['entered']=True
                result['root_is_private_cache'] = await asyncio.to_thread(
                    lambda:view.parent.parent.name=='.case-read-views')
                def files():
                    return [case_evidence.file_record(view,view/path) for path in paths]
                result['files'] = await asyncio.to_thread(files)
                if result['root_is_private_cache']:
                    rows = (view.parent/'files.jsonl').read_text(encoding='utf-8').splitlines()
                    records = [json.loads(row) for row in rows]
                    result['journal'] = [row for row in records if row.get('stage') is not True]
                    result['stage_journal'] = [row for row in records if row.get('stage') is True]
        try:
            if mode=='timeout':
                async with scope(3.0):
                    await consume()
            else:
                await consume()
        except TimeoutError:
            result['error']='timeout'
        except CaseWriteConflict as error:
            result['error']=error.code
        if mode=='measure':
            result['peak_traced_bytes']=tracemalloc.get_traced_memory()[1]
    finally:
        if mode=='measure':
            tracemalloc.stop()
        context.reset(token)
    result['gets']=state['gets']
    parent = storage._fs_root()/'.case-read-views'
    result['remaining_cache_containers'] = len(list(parent.iterdir())) if parent.exists() else 0
    from app.services.node_storage_budget import observe
    result['reserved_bytes'] = (await asyncio.to_thread(observe)).get('reserved_bytes')
    return result

@app.post('/fixture/view:stop')
async def stop(body: dict, user=Depends(get_current_user)):
    assert body['pid']==os.getpid()
    os._exit(79)
'''


def source_file(root):
    digest = hashlib.sha256()
    asset = root/'payload.bin'
    with asset.open('xb') as body:
        for _ in range(SIZE//(1024*1024)):
            chunk = os.urandom(1024*1024)
            digest.update(chunk)
            body.write(chunk)
    return asset, digest.hexdigest()


def install(api, store, prefix, name, source=None, *, raw=False):
    if store is not None:
        client = store.raw_client if raw else store.client
        args = {'Bucket':store.bucket, 'Key':prefix+'/'+name}
        if isinstance(source,Path):
            with source.open('rb') as body:
                client.put_object(**args,Body=body)
        else:
            client.put_object(**args,Body=source or b'')
        return client.head_object(**args)['ETag']
    root = (api.root/'files'/prefix).resolve()
    target = (root/name).resolve()
    assert root.is_relative_to(api.root.resolve()) and target.is_relative_to(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(source,Path):
        shutil.copyfile(source,target)
    else:
        target.write_bytes(source or b'')
    return None


def evaluate(api, case, paths, mode='measure'):
    result = api.call('POST',f'/fixture/view/{case}',{'paths':paths,'mode':mode},api_prefix=False)
    assert result['remaining_cache_containers']==0 and result['reserved_bytes']==0, result
    return result


def legacy_empty(api,store,report=None):
    ws = api.call('GET','/workspaces')[0]['id']
    row = api.call('POST','/fixture/view:legacy-case',{'workspace_id':ws},api_prefix=False)
    if report is not None:
        binding = api.call('GET',f'/fixture/view/{row["id"]}/binding',api_prefix=False)
        report['api_process'] = binding['process']
    version = install(api,store,row['prefix'],EMPTY,raw=True)
    result = evaluate(api,row['id'],[EMPTY])
    assert result['entered'] and result['root_is_private_cache'], result
    assert result['files']==result['journal']==[{'path':EMPTY,'size':0,'sha256':hashlib.sha256(b'').hexdigest()}]
    gets = result['gets']
    assert gets[0]['key']==row['prefix']+'/'+EMPTY and gets[0]['if_match']==version, gets
    assert gets[-1]['status']==200 and gets[-1]['closed'] and gets[-1]['received']==0, gets
    return result


def replacement(api,store,case,prefix):
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(evaluate,api,case,[ASSET,EMPTY],'replace')
        release = api.root/'copy-release'
        try:
            deadline = time.monotonic()+11
            while not (api.root/'copy-began').exists():
                if reading.done():
                    raise AssertionError(reading.result())
                if time.monotonic()>=deadline:
                    raise TimeoutError('No real S3 body reached its partial read fence')
                time.sleep(.02)
            installed = install(api,store,prefix,ASSET,b'external replacement')
        finally:
            release.touch()
        result = reading.result(timeout=20)
    assert not result['entered'] and result['error']=='input_stale',result
    original = next(item for item in result['gets'] if item['key'].endswith(ASSET))
    assert original['closed'] and original['received']==SIZE and original['if_match']!=installed,result
    fresh = evaluate(api,case,[ASSET])
    assert fresh['files'][0]['sha256']==hashlib.sha256(b'external replacement').hexdigest(),fresh
    return result


def exercise(api,store,report):
    if report['backend']=='legacy':
        user = api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
            'name':'Pre-catalog empty input','password':'isolated-test-123'})
        api.token = user['token']
        report['legacy_empty'] = legacy_empty(api,store,report)
        return
    case = api.create_case('Actual bounded case view')
    identifier = case['id']
    binding = api.call('GET',f'/fixture/view/{identifier}/binding',api_prefix=False)
    prefix = binding['prefix']
    report['api_process'] = binding['process']
    asset,digest = source_file(api.root)
    try:
        version = install(api,store,prefix,ASSET,asset)
        install(api,store,prefix,EMPTY)
    finally:
        asset.unlink()
    result = evaluate(api,identifier,[ASSET,EMPTY])
    expected = [
        {'path':ASSET,'size':SIZE,'sha256':digest},
        {'path':EMPTY,'size':0,'sha256':hashlib.sha256(b'').hexdigest()},
    ]
    assert result['entered'] and result['root_is_private_cache']==bool(store),result
    assert result['files']==expected and result['peak_traced_bytes']<SIZE//4,result
    report['normal'] = result
    if store:
        assert sorted(result['journal'],key=lambda row:row['path'])==sorted(expected,key=lambda row:row['path'])
        assert len(result['gets'])==2 and all(get['closed'] and get['status']==200 for get in result['gets'])
        http = next(item for item in result['gets'] if item['key'].endswith(ASSET))
        assert http['if_match']==version and http['received']==SIZE and http['max_read']<=64*1024
        timeout = evaluate(api,identifier,[ASSET,EMPTY],'timeout')
        assert not timeout['entered'] and timeout['error']=='timeout',timeout
        partial = next(item for item in timeout['gets'] if item['key'].endswith(ASSET))
        assert 0<partial['received']<SIZE and partial['closed'],timeout
        report['deadline'] = timeout
        report['replacement'] = replacement(api,store,identifier,prefix)
        report['legacy_empty'] = legacy_empty(api,store)


def stop_api(api,process):
    with httpx.Client(trust_env=False,timeout=10) as client:
        try:
            client.post(api.base+'/fixture/view:stop',json={'pid':process['pid']},
                        headers={'Authorization':'Bearer '+api.token})
        except httpx.TransportError:
            pass
        else:
            raise ValueError('Case view fixture did not exit')
    api.proc.wait(timeout=15)


def run(backend):
    from app.services import process_identity_native

    api = IsolatedServer('case-view-'+backend,app='case_view_fixture:app')
    report = {'backend':backend,'passed':False,'real_model_requests':0,'mock_llm_calls':0}
    store_context = (ObjectStore(executable=RUNTIME if backend=='s3' else LEGACY_RUNTIME,
                      volume_max=12,authenticated=backend=='s3') if backend!='fs' else nullcontext())
    print('CASE_READ_VIEW_STREAMING '+str(api.root),flush=True)
    try:
        with store_context as store:
            settings = store.settings() if store else {}
            module = api.root/'case_view_fixture.py'
            try:
                module.write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf-8')
                with api:
                    try:
                        exercise(api,store,report)
                    finally:
                        if 'api_process' in report:
                            stop_api(api,report['api_process'])
            finally:
                module.unlink(missing_ok=True)
        if store:
            assert store.proc.poll() is not None
            report.update({'s3_stopped':True,'s3_receipt':str(store.root/'store.json')})
        report['api_exit'] = process_identity_native.probe(report['api_process'])
        assert report['api_exit']['state']=='exited'
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__,'message':str(error)[:1200]}
        raise
    finally:
        paths = [Path(__file__).resolve(),*(SERVER/'app/services'/name for name in
            ('attempt_objects.py','private_cache.py','case_read_view.py'))]
        report['source_sha256'] = {path.relative_to(SERVER).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in paths}
        (api.root/'case-view-streaming.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print('CASE_READ_VIEW_STREAMING_OK '+str(api.root),flush=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('fs','s3','legacy'), required=True)
    run(parser.parse_args().backend)
