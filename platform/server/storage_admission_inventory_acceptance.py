"""Observe physical S3 usage inventory during file saves and owner pool admission."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
import copy
import json
import http.client
from pathlib import Path
import queue
import secrets
import sqlite3
import sys
import threading
import time

import aiohttp
import httpx

from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


FIXTURE = r'''
from contextvars import ContextVar
import json
from pathlib import Path
import time

from app.main import app
from fastapi import HTTPException
from app.services import storage_admission, storage_usage

root = Path(__file__).parent
observe = ContextVar('observe_inventory', default=False)
scan_s3 = storage_usage._scan_s3
scan_fs = storage_usage._scan_fs
advance = getattr(storage_admission, '_advance_revision', None)

async def record_fence(db, owner, expected):
    try:
        return await advance(db, owner, expected)
    except HTTPException as error:
        if observe.get() and expected is not None and error.status_code == 409:
            received = await storage_admission.quota_revision(db, owner)
            (root/'stale-inventory.json').write_text(json.dumps({'before':expected,'after':received}),encoding='utf8')
        raise

if advance is not None:
    storage_admission._advance_revision = record_fence

def actual_inventory(scan, settings, owner, counter):
    scan(settings, owner, counter)
    if not observe.get():return
    with (root/'inventory-reads.jsonl').open('a',encoding='utf8') as reads:
        reads.write(json.dumps({'entries':counter.entries,'bytes':counter.bytes})+'\n')
    evidence = root/'inventory-paused.json'
    if evidence.exists():return
    report = evidence.with_suffix('.temp')
    report.write_text(json.dumps({'owner':owner,'entries':counter.entries,'bytes':counter.bytes}),encoding='utf8')
    report.replace(evidence)
    end = time.monotonic()+20
    while not (root/'inventory-release').exists():
        if time.monotonic()>=end:
            raise TimeoutError('Isolation marker was not released')
        time.sleep(.02)

class Observe:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get('headers', ()))
        marker = observe.set(headers.get(b'x-observe-inventory') == b'pause')
        try:
            await self.app(scope, receive, send)
        finally:
            observe.reset(marker)

storage_usage._scan_s3 = lambda *args: actual_inventory(scan_s3, *args)
storage_usage._scan_fs = lambda *args: actual_inventory(scan_fs, *args)
app = Observe(app)
'''


def writer_available(api):
    with closing(sqlite3.connect(api.root/'test.sqlite',timeout=.15)) as connection:
        try:
            connection.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as error:
            assert error.sqlite_errorcode==sqlite3.SQLITE_BUSY,error
            return False
        connection.rollback()
        return True


def send_chunk(api,path,data,*,expire=False):
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                      timeout=35,trust_env=False) as client:
        headers={'X-Observe-Inventory':'pause'}
        if expire:headers['X-Expire-Admission']='1'
        response=client.put(path,params={'offset':0},content=data,headers=headers)
        if expire:
            assert response.status_code==503,(response.status_code,response.text[:1000])
            return response.status_code
        response.raise_for_status()
        return response.json()


def run(baseline,pool,filesystem,chunk=False):
    label = ('storage-chunk-inventory' if chunk else 'storage-fs-inventory' if filesystem else
             'storage-pool-inventory' if pool else 'storage-admission-inventory')
    api = IsolatedServer(label,app='inventory_fixture:app',access_log=True)
    result = {'passed':False,'baseline':baseline,'owner_pool':pool,'filesystem':filesystem,'chunk':chunk,
              'actual_model_calls':0,'simulated_llm_calls':0}
    print('STORAGE_ADMISSION_INVENTORY',api.root,flush=True)
    runtime = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    try:
        manager = nullcontext() if filesystem else ObjectStore(executable=runtime,volume_max=8,authenticated=True)
        with manager as store:
            settings = store.settings() if store else {}
            if store:result['store_root'] = str(store.root)
            module = api.root/'inventory_fixture.py'
            module.write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')
            try:
                with api, ThreadPoolExecutor(max_workers=2) as workers:
                    api.create_case('Owner with paused S3 inventory')
                    first = copy.copy(api)
                    sibling = copy.copy(first)
                    if not baseline:
                        first_workspace = first.call('GET','/workspaces')[0]['id']
                        sibling_case = first.call('POST',f'/workspaces/{first_workspace}/cases',{'name':'Another case of the same owner'})
                        sibling.case_id = sibling_case['id']
                        sibling.prefix = f'/cases/{sibling.case_id}'
                    api.create_case('Independent storage owner')
                    workspace = api.call('GET','/workspaces')[0]
                    if chunk:
                        payload=b'chunk admission physical bytes\x00'*4096
                        upload=first.call('POST',first.prefix+'/uploads',{'path':'inventory.bin','size':len(payload)})
                        upload_url=f'{first.prefix}/uploads/{upload["id"]}'
                        pending=workers.submit(send_chunk,first,upload_url,payload)
                    else:
                        method,path,body=('POST','/uploads',{'path':'pool.txt','size':30}) if pool else (
                            'PUT','/file',{'path':'inventory.txt','content':'S3 write after inventory'})
                        pending = workers.submit(first.call,method,first.prefix+path,body,
                                                 extra_headers={'X-Observe-Inventory':'pause'})
                    evidence = api.root/'inventory-paused.json'
                    try:
                        until = time.monotonic()+20
                        while not evidence.exists():
                            assert time.monotonic()<until and not pending.done(), 'Actual inventory did not reach its pause'
                            time.sleep(.03)
                        result['actual_inventory'] = json.loads(evidence.read_text(encoding='utf8'))
                        assert result['actual_inventory']['bytes']>0
                        result['sqlite_writer_available'] = writer_available(api)
                        assert result['sqlite_writer_available'] is (not baseline)
                        parallel = workers.submit(api.call,'PATCH',api.prefix,{'name':'Changed while inventory paused'},
                                                  expect=(200,409))
                        if baseline:
                            time.sleep(.5)
                            result['independent_metadata_completed_during_scan'] = parallel.done()
                            if parallel.done():
                                assert 'id' not in parallel.result(),parallel.result()
                        else:
                            assert parallel.result(timeout=8)['name']=='Changed while inventory paused'
                            result['independent_metadata_completed_during_scan'] = True
                            assert sibling.put('other-case.txt','same owner: different case')['ok']
                            assert sibling.call('GET',sibling.prefix+'/raw?path=other-case.txt',raw=True)==b'same owner: different case'
                            result['same_owner_other_case_saved_during_scan'] = True
                    finally:
                        (api.root/'inventory-release').touch()
                    saved=pending.result(timeout=30)
                    if chunk:
                        assert saved['offset']==len(payload)
                        with closing(sqlite3.connect(api.root/'test.sqlite')) as db:
                            credit=db.execute('select state,amount from upload_chunk_credits where upload_id=?',(upload['id'],)).fetchall()
                            assert credit==[('settled',len(payload))],credit
                            assert db.execute('select remaining_bytes from upload_storage_reservations where id=?',
                                              (upload['id'],)).fetchone()==(0,)
                        first.call('POST',upload_url+':complete')
                        assert first.call('GET',first.prefix+'/raw?path=inventory.bin',raw=True)==payload
                        result['settled_payload_bytes']=len(payload)
                    elif pool:
                        assert saved['size']==30 and saved['offset']==0,saved
                        assert first.call('GET',f'{first.prefix}/uploads/{saved["id"]}')['path']=='pool.txt'
                        result['upload_session_created_and_read'] = True
                    else:
                        assert saved['ok']
                        result['original_save_reached_http_200'] = True
                    if not baseline:
                        fence = json.loads((api.root/'stale-inventory.json').read_text('utf8'))
                        assert fence['after']>fence['before'],fence
                        result['stale_inventory_retried'] = fence
                        if chunk:
                            reads=[json.loads(row) for row in (api.root/'inventory-reads.jsonl').read_text('utf8').splitlines()]
                            assert len(reads)==2 and reads[1]['bytes']>reads[0]['bytes'],reads
                            result['chunk_inventory_reads']=reads
                        elif filesystem:
                            records=[json.loads(row) for row in (api.root/'inventory-reads.jsonl').read_text('utf8').splitlines()]
                            assert len(records)==2 and records[1]['bytes']>records[0]['bytes'],records
                            result['ordinary_edit_inventory_reads']=records
                            for source in (first,sibling):
                                case=source.call('GET',source.prefix)
                                directory=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/case['storage_prefix']/'.vibeflow/file-transactions'
                                assert not (directory/'pending').exists()
                                assert not list((directory/'history').glob('*/commit-ready.json'))
                                receipts=[json.loads(p.read_text('utf8')) for p in (directory/'history').glob('*/resolution.json')]
                                assert any(r.get('decision')=='committed' for r in receipts),receipts
                            result['local_receipts_published_without_another_inventory']=True
                            result['both_local_files_verified'] = True
                        else:
                            check_s3_reservations(api,first,sibling,saved,result,pool)
                    if not pool and not chunk:
                        assert first.call('GET',first.prefix+'/raw?path=inventory.txt',raw=True)==b'S3 write after inventory'
                    assert workspace['id']!=first.call('GET','/workspaces')[0]['id']
            finally:
                module.unlink(missing_ok=True)
        assert (store is None or store.proc.poll()==0) and api.proc.poll() is not None
        result['passed'] = True
    finally:
        (api.root/'storage-admission.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')


COLD_CUT = r'''
import os
from app.services.local_file_edits import PreparedEdit
install = PreparedEdit.install_file

def interrupted(self, *args):
    result = install(self, *args)
    marker = root/'cut-before-sql'
    if marker.exists():
        marker.unlink()
        os._exit(84)
    return result

PreparedEdit.install_file = interrupted
'''


def run_cold_filesystem(baseline):
    api=IsolatedServer('storage-fs-cold-inventory',app='inventory_fixture:app',access_log=True)
    report={'passed':False,'baseline':baseline,'actual_model_calls':0,'simulated_llm_calls':0}
    fixture=api.root/'inventory_fixture.py'
    fixture.write_text('import os\nos.environ[\'VIBEFLOW_JWT_SECRET\']='+repr(secrets.token_hex(32))+'\n'+FIXTURE+COLD_CUT,encoding='utf8')
    print('LOCAL_COLD_INVENTORY',api.root,flush=True)
    try:
        api.__enter__()
        api.create_case('Cold file recovery')
        case=api.call('GET',api.prefix)
        first=copy.copy(api)
        workspace=first.call('GET','/workspaces')[0]['id']
        sibling=copy.copy(first)
        sibling_case=first.call('POST',f'/workspaces/{workspace}/cases',{'name':'Other case of same owner'})
        sibling.prefix=f"/cases/{sibling_case['id']}"
        api.create_case('Independent account for file recovery')
        original='original cold recovery file'
        assert first.put('cold.txt',original)['ok']
        (api.root/'cut-before-sql').touch()
        try:first.put('cold.txt','uncommitted bytes')
        except (http.client.RemoteDisconnected,ConnectionResetError):pass
        else:raise AssertionError('The isolated API remained live')
        assert api.proc.wait(timeout=10)==84
        api.__exit__(None,None,None)
        directory=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/case['storage_prefix']/'.vibeflow/file-transactions'
        document=json.loads((directory/'pending/manifest.json').read_text('utf8'))
        assert (directory/'pending/commit-ready.json').is_file()
        report['api_exit_before_sql_commit']=84
        api.__enter__()
        evidence=api.root/'inventory-paused.json'
        with ThreadPoolExecutor(max_workers=2) as workers:
            recovery=workers.submit(first.call,'POST',first.prefix+'/file-transactions:recover',
                                    extra_headers={'X-Observe-Inventory':'pause'})
            try:
                deadline=time.monotonic()+15
                while not evidence.exists():
                    assert time.monotonic()<deadline and not recovery.done(),'Cold recovery did not reach real inventory'
                    time.sleep(.03)
                report['actual_inventory']=json.loads(evidence.read_text('utf8'))
                report['sqlite_writer_available']=writer_available(api)
                assert report['sqlite_writer_available'] is not baseline
                if not baseline:
                    assert api.call('PATCH',api.prefix,{'name':'Changed while cold recovery scanned'})['name']=='Changed while cold recovery scanned'
                    assert sibling.put('sibling.txt','saved while recovery read the disk')['ok']
                    report['independent_case_and_same_owner_save']=True
            finally:(api.root/'inventory-release').touch()
            recovery.result(timeout=25)
        assert first.call('GET',first.prefix+'/raw?path=cold.txt',raw=True)==original.encode()
        assert not (directory/'pending').exists()
        history=directory/'history'/document['plan']['id']
        assert not (history/'commit-ready.json').exists()
        receipt=json.loads((history/'resolution.json').read_text('utf8'))
        assert receipt=={'transaction_id':document['plan']['id'],'event_id':document['event_id'],
                         'plan_sha256':document['plan_sha256'],'decision':'rolled_back'}
        report['sql_rollback_not_overridden_by_prepared_receipt']=True
        if not baseline:
            fence=json.loads((api.root/'stale-inventory.json').read_text('utf8'))
            assert fence['after']>fence['before'],fence
            report['stale_inventory_retried']=fence
        report['passed']=True
    finally:
        api.__exit__(None,None,None)
        fixture.unlink(missing_ok=True)
        (api.root/'storage-admission.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')


CHUNK_DEADLINE = r'''
from app.services import owner_storage_budget

expire = ContextVar('expire_chunk_admission',default=False)
settings_for_budget = owner_storage_budget.get_settings

def budget_settings():
    settings = settings_for_budget()
    if expire.get():
        return settings.model_copy(update={'storage_measurement_timeout_seconds':3})
    return settings

class AdmissionDeadline:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = expire.set(dict(scope.get('headers',())).get(b'x-expire-admission')==b'1')
        try:await self.app(scope,receive,send)
        finally:expire.reset(token)

owner_storage_budget.get_settings = budget_settings
app = AdmissionDeadline(app)
'''


def run_chunk_timeout():
    api=IsolatedServer('storage-chunk-deadline',app='inventory_fixture:app',access_log=True)
    runtime=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    result={'passed':False,'actual_model_calls':0,'simulated_llm_calls':0}
    print('CHUNK_ADMISSION_DEADLINE',api.root,flush=True)

    def receipts():
        with closing(sqlite3.connect(api.root/'test.sqlite')) as db:
            return tuple(db.execute('select count(*) from '+table).fetchone()[0] for table in
                         ('owner_storage_reservations','upload_chunk_credits'))

    try:
        with ObjectStore(executable=runtime,volume_max=8,authenticated=True) as store:
            fixture=api.root/'inventory_fixture.py'
            fixture.write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE+CHUNK_DEADLINE,encoding='utf8')
            try:
                with api, ThreadPoolExecutor(max_workers=1) as workers:
                    api.create_case('Actual chunk deadline')
                    payload=b'marked physical upload\x00'*4096
                    upload=api.call('POST',api.prefix+'/uploads',{'path':'deadline.bin','size':len(payload)})
                    path=f'{api.prefix}/uploads/{upload["id"]}'
                    before=receipts()
                    pending=workers.submit(send_chunk,api,path,payload,expire=True)
                    try:
                        evidence=api.root/'inventory-paused.json'
                        until=time.monotonic()+9
                        while not evidence.exists():
                            assert time.monotonic()<until and not pending.done(),'Actual inventory did not reach the deadline barrier'
                            time.sleep(.02)
                        time.sleep(3.3)
                        assert not pending.done(),'Deadline returned before its active inventory drained'
                        assert writer_available(api)
                        assert receipts()==before
                        result['expired_request_drained_without_holding_writer']=True
                    finally:(api.root/'inventory-release').touch()
                    result['expired_request_status']=pending.result(timeout=10)
                    assert receipts()==before
                    assert api.call('GET',path)['offset']==0
                    result['no_new_grants_or_chunk_transfers_after_expiry']=True
                    assert send_chunk(api,path,payload)['offset']==len(payload)
                    api.call('POST',path+':complete')
                    assert api.call('GET',api.prefix+'/raw?path=deadline.bin',raw=True)==payload
                    result['retry_stored_exact_bytes']=len(payload)
            finally:fixture.unlink(missing_ok=True)
        assert store.proc.poll()==0 and api.proc.poll() is not None
        result['passed']=True
    finally:(api.root/'storage-admission.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')


OWNER_RECOVERY = r'''
import os
from botocore.client import BaseClient
from fastapi import Depends
from app.db import SessionLocal
from app.deps import get_current_user
from app.services import s3_streams, owner_storage_budget

reviewing = ContextVar('observe_owner_recovery',default=False)
owner_advance = storage_admission._advance_revision
real_snapshot = s3_streams.snapshot
real_call = BaseClient._make_api_call

def payload_call(client, operation, args):
    payload = operation == 'PutObject' and '/chunks/' in args.get('Key','') and args['Key'].endswith('/data') and args.get('ContentLength',0)>0
    response = real_call(client,operation,args)
    if payload:
        with (root/'real-payload-puts.jsonl').open('a',encoding='utf8') as handle:
            handle.write(json.dumps({'bytes':args['ContentLength']})+'\n')
        if (root/'exit-after-payload').exists():
            (root/'exit-after-payload').unlink()
            os._exit(79)
    return response

def proof_read(prefix,path,**options):
    result = real_snapshot(prefix,path,**options)
    marker = root/'owner-proof-paused.json'
    if reviewing.get() and '/chunks/' in path and path.endswith('/data') and result[0] and not marker.exists():
        temporary=marker.with_suffix('.tmp')
        temporary.write_text(json.dumps(result[0]),encoding='utf8')
        temporary.replace(marker)
        deadline = time.monotonic()+20
        while not (root/'owner-proof-release').exists():
            if time.monotonic()>=deadline:raise TimeoutError('Remote owner proof marker not released')
            time.sleep(.02)
    return result

async def proof_fence(db,owner,expected):
    try:return await owner_advance(db,owner,expected)
    except HTTPException as error:
        if reviewing.get() and expected is not None and error.status_code==409:
            (root/'owner-proof-stale.json').write_text(json.dumps({'expected':expected,'seen':await storage_admission.quota_revision(db,owner)}),encoding='utf8')
        raise

@app.app.post('/api/v1/fixture/recovery-epoch')
async def change_epoch(user=Depends(get_current_user)):
    async with SessionLocal() as db:
        await storage_admission.lock_owner(db,user.id,wait=True)
        await db.commit()
    return {'committed':True}

class ObserveRecovery:
    def __init__(self, app):self.app=app
    async def __call__(self, scope, receive, send):
        token=reviewing.set(dict(scope.get('headers',())).get(b'x-observe-owner-proof')==b'1')
        try:await self.app(scope,receive,send)
        finally:reviewing.reset(token)

BaseClient._make_api_call = payload_call
s3_streams.snapshot = proof_read
storage_admission._advance_revision = proof_fence
app = ObserveRecovery(app)
'''


async def terminal_for_proof(api,ready,release):
    async with aiohttp.ClientSession(trust_env=False) as client:
        async with client.ws_connect(api.base+'/ws/terminal',params={
                'token':api.token,'shell':'wsl-sandbox','case':api.case_id}) as ws:
            async with asyncio.timeout(50):
                first=await ws.receive_json()
                assert first['type']=='ready',first.get('type')
                await ws.send_json({'type':'input','data':"printf 'RECOVERY_HEART''BEAT_OK\\n'\n"})
                output=b''
                while b'RECOVERY_HEARTBEAT_OK' not in output:
                    frame=await ws.receive()
                    assert frame.type==aiohttp.WSMsgType.BINARY,frame.type
                    output+=frame.data
            ready.put(first['session'])
            assert await asyncio.to_thread(release.wait,55)


def run_owner_recovery(baseline,*,expire=False):
    api=IsolatedServer('storage-owner-deadline' if expire else 'storage-owner-proof',app='inventory_fixture:app',access_log=True)
    runtime=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    result={'passed':False,'baseline':baseline,'actual_model_calls':0,'simulated_llm_calls':0}
    print('OWNER_RECOVERY_LOCK',api.root,flush=True)

    def transfer_rows(upload_id):
        with closing(sqlite3.connect(api.root/'test.sqlite')) as db:
            return db.execute('select state,amount from upload_chunk_credits where upload_id=? order by created_at,id',
                              (upload_id,)).fetchall()

    def terminal_lease(identifier):
        with closing(sqlite3.connect(api.root/'test.sqlite',timeout=.15)) as db:
            return db.execute('select heartbeat_at,lease_expires_at,phase from native_sessions where id=?',
                              (identifier,)).fetchone()

    def put(client_api,path,payload,*,probe=False,expire=False):
        with httpx.Client(base_url=client_api.base+'/api/v1',timeout=55,trust_env=False,
                          headers={'Authorization':'Bearer '+client_api.token}) as client:
            headers={'X-Observe-Owner-Proof':'1'} if probe else {}
            if expire:headers['X-Expire-Admission']='1'
            response=client.put(path,params={'offset':0},content=payload,headers=headers)
            if expire:
                assert response.status_code==503,(response.status_code,response.text[:1000])
                return response.status_code
            response.raise_for_status()
            return response.json()

    try:
        with ObjectStore(executable=runtime,volume_max=8,authenticated=True) as store:
            fixture=api.root/'inventory_fixture.py'
            observe_setup=FIXTURE+OWNER_RECOVERY
            if expire:observe_setup+=CHUNK_DEADLINE.replace("timeout_seconds':3","timeout_seconds':8")
            fixture.write_text('import os\nos.environ.update('+repr({**store.settings(),
                'VIBEFLOW_JWT_SECRET':secrets.token_hex(32)})+')\n'+observe_setup,encoding='utf8')
            try:
                api.__enter__()
                api.create_case('Exited owner storage grant')
                first=copy.copy(api)
                api.create_case('Independent owner stays writable')
                payload=b'proof of actual prior upload bytes\x00'*4096
                upload=first.call('POST',first.prefix+'/uploads',{'path':'proof.bin','size':len(payload)})
                path=f'{first.prefix}/uploads/{upload["id"]}'
                (api.root/'exit-after-payload').touch()
                try:put(first,path,payload)
                except httpx.TransportError:pass
                else:raise AssertionError('API did not exit during the actual upload')
                assert api.proc.wait(timeout=10)==79
                result['api_exit_after_real_payload_put']=79
                api.__exit__(None,None,None)
                api.__enter__()
                assert transfer_rows(upload['id'])==[('active',len(payload))]
                marker=api.root/'owner-proof-paused.json'
                with ThreadPoolExecutor(max_workers=3) as workers:
                    terminal_release=threading.Event()
                    terminal_open=queue.Queue()
                    sid=None
                    try:
                        if expire:
                            terminal=workers.submit(asyncio.run,terminal_for_proof(api,terminal_open,terminal_release))
                            while sid is None:
                                try:sid=terminal_open.get(timeout=.2)
                                except queue.Empty:
                                    if terminal.done():terminal.result()
                            prior_lease=terminal_lease(sid)
                            assert prior_lease and prior_lease[2]=='active',prior_lease
                        pending=workers.submit(put,first,path,payload,probe=True,expire=expire)
                        until=time.monotonic()+25
                        while not marker.exists():
                            assert time.monotonic()<until and not pending.done(),'Remote proof did not reach the observation barrier'
                            time.sleep(.03)
                        result['actual_proof']=json.loads(marker.read_text('utf8'))
                        assert result['actual_proof']['size']==len(payload)
                        result['sqlite_writer_available']=writer_available(api)
                        assert result['sqlite_writer_available'] is not baseline
                        peer=workers.submit(api.call,'PATCH',api.prefix,{'name':'Changed while old proof paused'},expect=(200,409))
                        if baseline:
                            time.sleep(.4);result['independent_case_available']=peer.done()
                            if peer.done():assert 'id' not in peer.result()
                        elif expire:
                            assert peer.result(timeout=6)['name']=='Changed while old proof paused'
                            time.sleep(8.2)
                            assert not pending.done(),'Expired request returned before remote proof drained'
                            later_lease=terminal_lease(sid)
                            assert later_lease[2]=='active' and all(a>b for a,b in zip(later_lease[:2],prior_lease[:2])),(prior_lease,later_lease)
                            assert transfer_rows(upload['id'])==[('active',len(payload))]
                            result['actual_wsl_heartbeat_and_lease_advanced']={'before':prior_lease[:2],'during':later_lease[:2]}
                            result['active_credit_retained_while_expired_proof_drained']=True
                        else:
                            assert peer.result(timeout=6)['name']=='Changed while old proof paused'
                            result['independent_case_available']=True
                            assert first.call('POST','/fixture/recovery-epoch')['committed']
                            assert transfer_rows(upload['id'])==[('active',len(payload))]
                        (api.root/'owner-proof-release').touch()
                        if expire:
                            result['expired_recovery_status']=pending.result(timeout=15)
                            assert transfer_rows(upload['id'])==[('active',len(payload))]
                            assert api.call('DELETE','/terminal/sessions/'+sid)['ok']
                            terminal_row=api.call('GET','/terminal/sessions/'+sid)
                            assert terminal_row['phase']=='closed' and terminal_row['cleanup_verified'] is True
                            result['wsl_terminal_stop_verified']=True
                        else:assert pending.result(timeout=35)['offset']==len(payload)
                    finally:
                        (api.root/'owner-proof-release').touch()
                        terminal_release.set()
                    if expire:
                        terminal.result(timeout=15)
                        assert put(first,path,payload)['offset']==len(payload)
                assert transfer_rows(upload['id'])==[('settled',len(payload)),('settled',0)]
                first.call('POST',path+':complete')
                assert first.call('GET',first.prefix+'/raw?path=proof.bin',raw=True)==payload
                written=[json.loads(line) for line in (api.root/'real-payload-puts.jsonl').read_text('utf8').splitlines()]
                assert written==[{'bytes':len(payload)}],written
                result['real_payload_puts']=len(written)
                result['settled_and_verified_bytes']=len(payload)
                if not baseline and not expire:
                    stale=json.loads((api.root/'owner-proof-stale.json').read_text('utf8'))
                    assert stale['seen']>stale['expected'],stale
                    result['stale_proof_retried']=stale
            finally:
                api.__exit__(None,None,None)
                fixture.unlink(missing_ok=True)
        assert store.proc.poll()==0
        result['passed']=True
    finally:(api.root/'storage-admission.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')


def check_s3_reservations(api,first,sibling,saved,result,pool):
    with closing(sqlite3.connect(api.root/'test.sqlite')) as db:
        if pool:
            rows=db.execute('select state,remaining_bytes from owner_storage_reservations where user_id=?',
                            (result['actual_inventory']['owner'],)).fetchall()
            assert rows and all(state=='settled' and remain==0 for state,remain in rows),rows
            assert db.execute('select remaining_bytes from upload_storage_reservations where id=?',
                              (saved['id'],)).fetchone()==(30,)
            result['pool_reservations_settled']=len(rows)
            result['upload_payload_still_reserved']=30
        writes = db.execute('select case_id,state,remaining_bytes,request_json from storage_write_reservations where case_id in (?,?)',
                            (first.case_id,sibling.case_id)).fetchall()
    assert {case for case,*_ in writes}==({sibling.case_id} if pool else {first.case_id,sibling.case_id})
    assert all(state=='settled' and remainder==0 for _,state,remainder,_ in writes)
    for case, path in ((first.case_id,'inventory.txt'),(sibling.case_id,'other-case.txt')):
        if pool and case==first.case_id:continue
        assert any(written==case and json.loads(request)['key'].endswith('/'+path)
                   for written,_,_,request in writes),path
    result['write_reservations_settled'] = len(writes)
    if pool:
        scans=[json.loads(row) for row in (api.root/'inventory-reads.jsonl').read_text('utf8').splitlines()]
        assert len(scans)==2 and scans[1]['bytes']>scans[0]['bytes'],scans
        result['upload_intent_inventory_reads']=scans
        with closing(sqlite3.connect(api.root/'test.sqlite')) as db:
            plan,encoded=db.execute("select id,quotas from plans where code='free'").fetchone()
            quota=json.loads(encoded);quota['storage_mb']=1
            db.execute('update plans set quotas=? where id=?',(json.dumps(quota),plan));db.commit()
        first.call('POST',first.prefix+'/uploads',{'path':'over-budget.bin','size':1024*1024},expect=429)
        with closing(sqlite3.connect(api.root/'test.sqlite')) as db:
            assert not db.execute('select id from upload_storage_reservations where path=?',('over-budget.bin',)).fetchall()
            assert not db.execute("select id from owner_storage_reservations where state='active'").fetchall()
        result['oversized_upload_refused_without_active_grant']=True


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',action='store_true')
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--pool',action='store_true')
    modes.add_argument('--filesystem',action='store_true')
    modes.add_argument('--cold-filesystem',action='store_true')
    modes.add_argument('--chunk',action='store_true')
    modes.add_argument('--chunk-timeout',action='store_true')
    modes.add_argument('--owner-recovery',action='store_true')
    modes.add_argument('--owner-recovery-timeout',action='store_true')
    args = parser.parse_args()
    enforce([Path(__file__)])
    if args.cold_filesystem:run_cold_filesystem(args.baseline)
    elif args.chunk_timeout:run_chunk_timeout()
    elif args.owner_recovery:run_owner_recovery(args.baseline)
    elif args.owner_recovery_timeout:run_owner_recovery(False,expire=True)
    else:run(args.baseline,args.pool,args.filesystem,args.chunk)
