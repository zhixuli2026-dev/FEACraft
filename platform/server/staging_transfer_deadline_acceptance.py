"""Real SQL/S3 staging under an expired deadline, cancellation and full copy pool."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time

from app.services import process_identity_native as identity
from live_llm_terminal_agent_acceptance import disposable_database
from s3testsupport import ObjectStore
from staging_sync_smoke import boot
from testsupport import SERVER


FIXTURE = r'''
import json
from contextlib import nullcontext
from contextvars import ContextVar
from threading import current_thread
from fastapi import HTTPException
from app.services import private_cache, request_deadline, staging_download, staging_preparation
from app.services.async_ops import finish_before_cancel

fault_target = ContextVar('fault_target', default=None)
waiting_requests = {}
open_path = Path.open


class PausedFile:
    def __init__(self,stream,name):
        self.stream,self.name,self.calls=stream,name,0

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self,*args):
        return self.stream.__exit__(*args)

    def fileno(self):return self.stream.fileno()

    def read(self,size):
        data=self.stream.read(size)
        self.calls+=1
        with open_path(root/(self.name+'.reads'),'a',encoding='utf8') as log:
            log.write(str(len(data))+'\n')
        if self.calls==1:
            deadline=request_deadline.current()
            (root/(self.name+'.ready')).write_text(json.dumps({'deadline':deadline.deadline if deadline else None,
                'thread':current_thread().name,'started_at':time.monotonic()}),encoding='utf8')
            end=time.monotonic()+25
            while not (root/(self.name+'.release')).is_file():
                if time.monotonic()>=end:raise AssertionError('Staging fixture reader not released')
                time.sleep(.02)
        return data


def paused_open(path,*args,**kwargs):
    stream=open_path(path,*args,**kwargs)
    name=fault_target.get()
    if name and path.name=='proof.bin' and args==('rb',) and not (root/(name+'.ready')).exists():
        return PausedFile(stream,name)
    return stream
Path.open=paused_open


unlink_path = Path.unlink


def pause_first_unlink(path, *args, **kwargs):
    selected = cleanup_target.get()
    if selected is not None:
        name, directory, deadline = selected
        ready = root / (name+'.ready')
        if path.is_relative_to(directory) and path.name in {'keep.txt','proof.bin'} and not ready.exists():
            ready.write_text(json.dumps({'deadline': deadline.deadline,
                                        'path': path.relative_to(directory).as_posix()}), encoding='utf8')
            end = time.monotonic()+15
            while not (root/(name+'.release')).is_file():
                if time.monotonic()>=end:raise AssertionError('Staging unlink not released')
                time.sleep(.02)
    return unlink_path(path, *args, **kwargs)


cleanup_target = ContextVar('cleanup_target', default=None)
Path.unlink = pause_first_unlink


preparation_fault = ContextVar('preparation_fault', default=None)
download_staged_file = staging_download.download


def observe_download(prefix, name, record, path):
    digest = download_staged_file(prefix,name,record,path)
    fault = preparation_fault.get()
    if fault and name == 'proof.bin':
        label,fail = fault
        deadline = request_deadline.current()
        (root/(label+'.ready')).write_text(json.dumps({
            'deadline':deadline.deadline,'stage':path.parent.name,'bytes':path.stat().st_size,
        }),encoding='utf8')
        wait_until = time.monotonic()+25
        while not (root/(label+'.release')).exists():
            if time.monotonic()>=wait_until:raise AssertionError('Staging download not released')
            time.sleep(.02)
        if fail:raise OSError('Injected read error after actual download')
    return digest


staging_download.download = observe_download


@app.post('/fixture/bounded-preparation/{case_id}')
async def bounded_preparation(case_id:int,body:dict,db=Depends(get_db),user=Depends(get_current_user)):
    case=await get_owned_case(case_id,user,db)
    prefix=case.s3_prefix
    await db.rollback()
    name=body.get('fault')
    token=preparation_fault.set((name,body.get('fail')) if name else None)
    try:
        async with request_deadline.scope(body.get('seconds',20)):
            stage=await finish_before_cancel(asyncio.to_thread(storage.prepare_job_staging,prefix,case_id))
            return {'stage':stage}
    except TimeoutError as error:
        raise HTTPException(504,'preparation_deadline') from error
    except OSError as error:
        raise HTTPException(502,{'error':str(error),'notes':getattr(error,'__notes__',[])}) from error
    finally:
        preparation_fault.reset(token)


@app.post('/fixture/reap-preparations')
async def reap_preparations(user=Depends(get_current_user)):
    return await finish_before_cancel(asyncio.to_thread(private_cache.recover))


@app.get('/fixture/node-budget')
async def node_budget(user=Depends(get_current_user)):
    from app.services.node_storage_budget import observe
    return await finish_before_cancel(asyncio.to_thread(observe))


@app.post('/fixture/cleanup-stage/{case_id}')
async def cleanup_stage(case_id:int,body:dict,db=Depends(get_db),user=Depends(get_current_user)):
    from app.routers.terminal import _cleanup_staging
    await get_owned_case(case_id,user,db)
    await db.rollback()
    selected = body['stage']
    path = root/'files'/selected
    assert path.parent == root/'files'/'.staging'
    cleanup = storage.cleanup_job_staging if body['job'] else _cleanup_staging
    try:
        async with request_deadline.scope(body.get('seconds', 20)) as deadline:
            name = body.get('fault')
            token = cleanup_target.set((name, path, deadline) if name else None)
            try:
                await finish_before_cancel(asyncio.to_thread(cleanup,selected,synchronized=True))
            finally:
                cleanup_target.reset(token)
    except TimeoutError as error:
        raise HTTPException(504,'staging_cleanup_deadline') from error
    return {'cleaned': True}


@app.post('/fixture/bounded-sync/{case_id}')
async def bounded_sync(case_id:int,body:dict,db=Depends(get_db),user=Depends(get_current_user)):
    case=await get_owned_case(case_id,user,db)
    prefix,user_id=case.s3_prefix,user.id
    await db.rollback()
    async def authorized(locked):
        from app.models import User
        return await get_owned_case(case_id,await locked.get(User,user_id),locked)
    name=body.get('fault')
    token=fault_target.set(name)
    original=request_deadline.STAGING_CLEANUP_SECONDS
    if body.get('seconds') is not None:request_deadline.STAGING_CLEANUP_SECONDS=body['seconds']
    if name:waiting_requests[name]=(user_id,asyncio.current_task())
    try:
        budget=request_deadline.scope(body['parent']) if body.get('parent') else nullcontext()
        async with budget:
            return await staging_sync.sync_authorized(body['stage'],prefix,case_id,authorized)
    except TimeoutError as error:
        raise HTTPException(504,'staging_deadline') from error
    except storage.StagingConflict as error:
        raise HTTPException(409,{'code':'staging_conflict','message':str(error)}) from error
    except asyncio.CancelledError as error:
        raise HTTPException(499,'staging_cancelled') from error
    finally:
        if name:waiting_requests.pop(name,None)
        request_deadline.STAGING_CLEANUP_SECONDS=original
        fault_target.reset(token)


@app.post('/fixture/cancel-sync/{name}')
async def cancel_sync(name:str,user=Depends(get_current_user)):
    owner,task=waiting_requests[name]
    if user.id!=owner:raise HTTPException(404)
    task.cancel()
    return {'cancelled':True}


@app.get('/fixture/staging-operation-count/{case_id}')
async def staging_operation_count(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
    from sqlalchemy import func,select
    from app.models import CaseStorageOperation
    await get_owned_case(case_id,user,db)
    return {'count':await db.scalar(select(func.count()).select_from(CaseStorageOperation).where(
        CaseStorageOperation.case_id==case_id,CaseStorageOperation.purpose=='staging'))}
'''


def run(store, database):
    def launch(settings, database_url):
        isolated = boot(settings,database_url)
        fixture = isolated.root/'staging_fixture.py'
        fixture.write_text(fixture.read_text(encoding='utf8')+FIXTURE, encoding='utf8')
        return isolated

    api = launch(store.settings(),database)
    report = {'passed': False, 'database': 'postgresql' if database else 'sqlite',
              'actual_model_requests': 0, 'mock_llm_calls': 0, 'checks': {}}
    print('STAGING_TRANSFER_DEADLINE_EVIDENCE', api.root, flush=True)

    def sidecar(name, extension):
        return api.root/f'{name}.{extension}'

    def start(case, payload):
        selected = api.call('POST', f'/fixture/stage/{case}', {}, api_prefix=False)
        target = api.root/'files'/selected['stage']/'proof.bin'
        target.write_bytes(payload)
        return case, selected['stage'], target

    def synchronize(selection, *, expect=200, **options):
        case, stage, _ = selection
        return api.call('POST', f'/fixture/bounded-sync/{case}', {'stage': stage, **options},
                        api_prefix=False, expect=expect)

    def paused(name):
        end = time.monotonic()+15
        while time.monotonic()<end:
            try:
                return json.loads(sidecar(name, 'ready').read_text(encoding='utf8'))
            except (FileNotFoundError, ValueError):
                time.sleep(.02)
        raise TimeoutError('Actual staged file did not start reading: '+name)

    try:
        with api, ThreadPoolExecutor(max_workers=5) as executor:
            sample = b'V'*(3*1024*1024)
            first = api.create_case('Staging transfer limits')['id']
            workspace = api.call('GET', '/workspaces')[0]['id']
            cases = [first]+[api.call('POST', f'/workspaces/{workspace}/cases', {'name': f'Bounded transfer {i}'})['id']
                             for i in range(4)]
            selection = start(first, sample)
            for name, options, error in (('deadline', {'seconds': 3}, 504),
                                          ('inherited', {'parent': 3}, 504), ('cancel', {}, 499)):
                transfer = executor.submit(synchronize, selection, expect=error, fault=name, **options)
                try:
                    state = paused(name)
                    assert state['thread'].startswith('case-copy') and state['deadline']>state['started_at'], state
                    if error==504:
                        time.sleep(max(0,state['deadline']-time.monotonic())+.15)
                    else:
                        assert api.call('POST',f'/fixture/cancel-sync/{name}',api_prefix=False)['cancelled']
                    assert not transfer.done()
                    owner = api.call('GET', f'/fixture/storage-owner/{first}', api_prefix=False)['process']
                    assert identity.probe(owner)['state']=='match', owner
                    assert selection[2].stat().st_size==len(sample)
                finally:
                    sidecar(name, 'release').touch()
                assert transfer.result(timeout=10)['detail']==('staging_deadline' if error==504 else 'staging_cancelled')
                api.call('GET', f'/cases/{first}/raw?path=proof.bin', expect=404)
                reads = [int(line) for line in sidecar(name,'reads').read_text().splitlines()]
                assert (reads==[1024*1024] if error==504 else sum(reads)==len(sample)), reads
                report['checks'][name] = {**state, 'read_bytes': sum(reads), 'active_while_draining': True,
                                           'published': False, 'status': error}
            assert synchronize(selection)=={'uploaded': 1, 'deleted': 0}
            assert api.call('GET',f'/cases/{first}/raw?path=proof.bin',raw=True)==sample
            report['checks']['replayed_sha256']=hashlib.sha256(sample).hexdigest()

            selections = [start(case,b'new') for case in cases]
            count_route = f'/fixture/staging-operation-count/{cases[-1]}'
            last_before = api.call('GET',count_route,api_prefix=False)
            handles = [executor.submit(synchronize, chosen, fault=f'slot-{i}') for i, chosen in enumerate(selections[:4])]
            try:
                transfers = [paused(f'slot-{i}') for i in range(4)]
                assert len({item['thread'] for item in transfers})==4
                rejection = synchronize(selections[-1], expect=429)
                assert rejection['detail']['code']=='staging_sync_busy', rejection
                assert all(not handle.done() for handle in handles)
                assert api.call('GET',count_route,api_prefix=False)==last_before
            finally:
                for i in range(4):sidecar(f'slot-{i}', 'release').touch()
            for handle in handles:assert handle.result(timeout=25)=={'uploaded': 1, 'deleted': 0}
            assert synchronize(selections[-1])=={'uploaded': 1, 'deleted': 0}
            for case in cases:assert api.call('GET',f'/cases/{case}/raw?path=proof.bin',raw=True)==b'new'
            report['checks']['concurrency']={'workers':len(transfers),'fifth_response':429,'completed_after_retry':5}

            if os.name == 'nt':
                import _winapi
                external = api.root/'unrelated-directory'
                external.mkdir()
                sentinel = external/'keep.txt'
                sentinel.write_bytes(b'outside staging')
                for case, job in zip(cases[1:3], (False, True)):
                    name = 'job-cleanup' if job else 'terminal-cleanup'
                    prepared = api.call('POST',f'/fixture/stage/{case}',{'nested/keep.txt':'published'},
                                        api_prefix=False)
                    stage = prepared['stage']
                    directory = api.root/'files'/stage
                    assert synchronize((case,stage,directory/'proof.bin'))=={'uploaded': 1,'deleted': 0}
                    # An interrupted cleanup now owns the local tree; any link
                    # in it must be removed without traversing its destination.
                    _winapi.CreateJunction(str(external),str(directory/'linked'))
                    marker = directory.with_suffix('.terminal-cleanup.json')
                    manifest = directory.with_suffix('.manifest.json')
                    original = manifest.read_bytes()
                    files = [directory/'proof.bin', directory/'nested/keep.txt']

                    route = f'/fixture/cleanup-stage/{case}'
                    body = {'stage': stage, 'job': job}
                    pending = executor.submit(api.call,'POST',route,{**body,'fault':name,'seconds':2},
                                              api_prefix=False,expect=504)
                    try:
                        state = paused(name)
                        assert all(file.is_file() for file in files) and marker.is_file()
                        time.sleep(max(0,state['deadline']-time.monotonic())+.12)
                        assert not pending.done()
                    finally:
                        sidecar(name, 'release').touch()
                    assert pending.result(timeout=10)['detail']=='staging_cleanup_deadline'
                    assert sum(file.is_file() for file in files)==1
                    assert json.loads(marker.read_text(encoding='utf8')) == {
                        'schema':'vibeflow.terminal-cleanup.v1','stage_prefix':stage,
                        'intent':'cleanup_only','source_synchronized':True}
                    assert manifest.read_bytes()==original
                    rejected = synchronize((case,stage,directory/'proof.bin'), expect=409)
                    assert rejected['detail']['code']=='staging_conflict', rejected
                    assert api.call('POST',route,body,api_prefix=False)=={'cleaned':True}
                    assert not any(os.path.lexists(path) for path in (directory,manifest,marker))
                    assert api.call('POST',route,body,api_prefix=False)=={'cleaned':True}
                    assert sentinel.read_bytes()==b'outside staging'
                    assert api.call('GET',f'/cases/{case}/raw?path=proof.bin',raw=True)==b'new'
                    assert api.call('GET',f'/cases/{case}/raw?path=nested/keep.txt',raw=True)==b'published'
                    report['checks'][name] = {**state, 'status':504, 'drained':True,
                                              'sync_blocked':True,'retry_completed':True,
                                              'junction_destination_preserved':True,'remote_preserved':True}

            case = cases[-1]
            result = api.call('POST',f'/fixture/legacy-worker/{case}',api_prefix=False)
            assert result['status']=='succeeded' and result['stage'] and not result['isolated'], result
            directory = api.root/'files'/result['stage']
            deadline = time.monotonic()+10
            while any(os.path.lexists(p) for p in (directory, directory.with_suffix('.manifest.json'),
                                                   directory.with_suffix('.terminal-cleanup.json'))):
                if time.monotonic()>=deadline:raise AssertionError('Legacy job staging remains after publication')
                time.sleep(.025)
            assert api.call('GET',f'/cases/{case}/raw?path=legacy-output.txt',raw=True)==b'REAL STAGING'
            report['checks']['legacy_worker']={'status':result['status'],'stage_removed':True,'published':True}

            source = cases[1]
            copied = b'P'*(300*1024)
            assert synchronize(start(source,copied))=={'uploaded':1,'deleted':0}
            route = f'/fixture/bounded-preparation/{source}'
            probe = lambda peer=api: peer.call('GET','/fixture/node-budget',api_prefix=False)
            reap = lambda peer=api: peer.call('POST','/fixture/reap-preparations',api_prefix=False)
            storage_root = api.root/'files'
            receipts = storage_root/'.staging-preparations'

            for mode, body, status in (
                ('download-deadline', {'seconds':4}, 504),
                ('download-error', {'fail':True}, 502),
            ):
                if mode=='download-error' and os.name!='nt':continue
                before = probe()
                response = executor.submit(api.call,'POST',route,{**body,'fault':mode},
                                           expect=status,api_prefix=False)
                try:
                    state = paused(mode)
                    name = state['stage']
                    directory = storage_root/'.staging'/name
                    receipt = receipts/(name+'.json')
                    target = directory/'proof.bin'
                    assert state['bytes']==len(copied) and target.read_bytes()==copied
                    assert any(row['name']==receipt.name and row['status']=='active' for row in reap())
                    live = probe()
                    assert live['reserved_bytes']>=len(copied) and receipt.is_file()
                    if status==504:
                        time.sleep(max(0,state['deadline']-time.monotonic())+.12)
                        assert not response.done()
                        sidecar(mode,'release').touch()
                        assert response.result(timeout=15)['detail']=='preparation_deadline'
                    else:
                        # A real Windows reader denies deletion of the downloaded file.
                        with target.open('rb'):
                            sidecar(mode,'release').touch()
                            detail = response.result(timeout=15)['detail']
                            assert detail['error']=='Injected read error after actual download'
                            assert detail['notes']==['Staging preparation cleanup: PermissionError'], detail
                finally:
                    sidecar(mode,'release').touch()
                assert target.read_bytes()==copied
                assert json.loads(receipt.read_text(encoding='utf8'))['discarded'] is True
                retained = probe()
                assert retained['reserved_bytes']==0 and retained['bytes']>=before['bytes']+len(copied)
                blocked = synchronize((source,'.staging/'+name,target),expect=409)
                assert blocked['detail']['code']=='staging_conflict'
                assert any(row['name']==receipt.name and row['status']=='removed' for row in reap())
                assert not directory.exists() and not receipt.exists()
                settled = probe()
                assert settled['reserved_bytes']==0 and settled['bytes']==before['bytes'], (before,settled)
                assert api.call('GET',f'/cases/{source}/raw?path=proof.bin',raw=True)==copied
                report['checks'][mode]={'status':status,'claim_while_active':live['reserved_bytes'],
                    'physical_bytes_retained':retained['bytes']-before['bytes'],
                    'active_before_stop':True,'sync_blocked':True,'detached_cleanup':True,
                    'claim_after_cleanup':settled['reserved_bytes']}

            before = probe()
            response = executor.submit(api.call,'POST',route,{'fault':'download-exit'},
                                       api_prefix=False)
            state = paused('download-exit')
            name = state['stage']
            directory = storage_root/'.staging'/name
            receipt = receipts/(name+'.json')
            ownership = json.loads(receipt.read_text(encoding='utf8'))
            assert not ownership['discarded'] and identity.probe(ownership['owner'])['state']=='match'
            assert probe()['reserved_bytes']>=len(copied)
            assert identity.probe(ownership['owner'])['state']=='match'
            os.kill(ownership['owner']['pid'],signal.SIGTERM if os.name=='nt' else signal.SIGKILL)
            api.proc.wait(timeout=10)
            try:
                response.result(timeout=15)
            except OSError:
                pass
            else:
                raise AssertionError('Exited API returned a prepared snapshot')
            deadline = time.monotonic()+10
            observed = identity.probe(ownership['owner'])
            while observed['state']!='exited' and time.monotonic()<deadline:
                time.sleep(.05)
                observed = identity.probe(ownership['owner'])
            assert observed['state']=='exited', observed
            assert (directory/'proof.bin').read_bytes()==copied and receipt.is_file()
            shared = {**store.settings(),'VIBEFLOW_STORAGE_FS_ROOT':str(storage_root),
                      'VIBEFLOW_JWT_SECRET':api.env['VIBEFLOW_JWT_SECRET'],
                      'VIBEFLOW_DATABASE_URL':api.env['VIBEFLOW_DATABASE_URL']}
            cold = launch(shared,database)
            with cold:
                cold.token = api.token
                retained = probe(cold)
                assert retained['reserved_bytes']==0 and retained['bytes']>=before['bytes']+len(copied)
                assert any(row['name']==receipt.name and row['status']=='removed' for row in reap(cold))
                assert not directory.exists() and not receipt.exists()
                retry = cold.call('POST',route,{},api_prefix=False)
                assert (storage_root/retry['stage']/'proof.bin').read_bytes()==copied
                assert not (receipts/((storage_root/retry['stage']).name+'.json')).exists()
                assert cold.call('GET',f'/cases/{source}/raw?path=proof.bin',raw=True)==copied
            report['checks']['download-exit']={'original_process_exited':True,'cold_api_exited':True,
                'physical_bytes_after_exit':retained['bytes']-before['bytes'],
                'claim_after_owner_exit':retained['reserved_bytes'],'retry_succeeded':True}
        report['passed'] = True
    except BaseException as error:
        report['failure_type'] = type(error).__name__
        raise
    finally:
        report['api_exited']=api.proc is not None and api.proc.poll() is not None
        (api.root/'staging-transfer-deadline.json').write_text(json.dumps(report,indent=2),encoding='utf8')


async def main(postgres):
    async with disposable_database(postgres) as database:
        with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
                         authenticated=True,volume_max=6) as store:
            await asyncio.to_thread(run,store,database)
    assert store.proc.poll() is not None
    print('STAGING_TRANSFER_STORE_STOPPED',store.root,flush=True)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres',action='store_true')
    asyncio.run(main(parser.parse_args().postgres))
