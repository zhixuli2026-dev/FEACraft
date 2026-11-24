"""Cancel real chunk admission before inventory drains and after durable SQL commit."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
from queue import Empty, Queue
import sqlite3
import sys
from threading import Event
import time

import httpx

from s3testsupport import ObjectStore
from storage_admission_inventory_acceptance import FIXTURE, terminal_for_proof, writer_available
from testsupport import IsolatedServer, SERVER


CANCEL = r'''
import asyncio
from fastapi import Depends,HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import OwnerStorageReservation,UploadChunkCredit,UploadStorageReservation
from app.services import owner_storage_budget, s3_uploads

phase=ContextVar('observe_grant_cancel',default='')
claiming=ContextVar('observe_grant_commit',default=False)
owners={}
persist=owner_storage_budget._persist_grant
commit=AsyncSession.commit
read_meta=s3_uploads._meta

async def selected_grant(*args,**kwargs):
    marker=claiming.set(phase.get() in {'commit','settlement'})
    try:return await persist(*args,**kwargs)
    finally:claiming.reset(marker)

async def held_commit(db):
    await commit(db)
    if claiming.get() and not (root/'commit-acknowledgement-paused').exists():
        (root/'commit-acknowledgement-paused').touch()
        async with asyncio.timeout(20):
            while not (root/'commit-acknowledgement-release').exists():await asyncio.sleep(.02)

def held_settlement_meta(prefix,upload_id):
    value=read_meta(prefix,upload_id)
    marker=root/'settlement-read-paused.json'
    if (phase.get()=='settlement' and (root/'commit-acknowledgement-release').exists() and not marker.exists()):
        receipt=marker.with_suffix('.tmp')
        receipt.write_text(json.dumps({k:value[0][k] for k in ('offset','size','status')}),encoding='utf8')
        receipt.replace(marker)
        end=time.monotonic()+32
        while not (root/'settlement-read-release').exists():
            if time.monotonic()>=end:raise TimeoutError('Actual settlement metadata read not released')
            time.sleep(.02)
    return value

@app.app.post('/api/v1/fixture/active-cancel/{kind}')
async def active_cancel(kind:str,user=Depends(get_current_user)):
    task=owners.get(kind)
    if task is None or task.done():raise HTTPException(409,'No selected live HTTP request')
    return {'cancelled_live_task':task.cancel(), 'pending_cancellation':task.cancelling()}

@app.app.get('/api/v1/fixture/cancel-ledger/{identifier}')
async def ledger(identifier:str,user=Depends(get_current_user)):
    async with SessionLocal() as read:
        upload=await read.get(UploadStorageReservation,identifier)
        if upload is None or upload.user_id!=user.id:raise HTTPException(404)
        credits=await read.scalars(select(UploadChunkCredit).where(UploadChunkCredit.upload_id==identifier)
                                  .order_by(UploadChunkCredit.created_at,UploadChunkCredit.id))
        selected=[{'id':row.id,'state':row.state,'amount':row.amount} for row in credits]
        grants=await read.scalars(select(OwnerStorageReservation).where(OwnerStorageReservation.user_id==user.id))
        return {'remaining':upload.remaining_bytes,'credits':selected,
                'grants':{row.id:[row.state,row.remaining_bytes] for row in grants}}

class CancelRequest:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        raw=dict(scope.get('headers',())).get(b'x-cancel-stage')
        if not raw:return await self.app(scope,receive,send)
        name=raw.decode('ascii')
        marker=phase.set(name)
        owners[name]=asyncio.current_task()
        try:await self.app(scope,receive,send)
        except asyncio.CancelledError:
            # Only this isolated observer sends 499. Cancellation itself was
            # delivered to the real HTTP task running the actual application.
            await JSONResponse({'cancelled':True},status_code=499)(scope,receive,send)
        finally:
            owners.pop(name,None)
            phase.reset(marker)

AsyncSession.commit=held_commit
s3_uploads._meta=held_settlement_meta
owner_storage_budget._persist_grant=selected_grant
app=CancelRequest(app)
'''


def send(api,path,payload,*,phase=None):
    headers={}
    if phase:
        headers['X-Cancel-Stage']=phase
        if phase=='inventory':headers['X-Observe-Inventory']='pause'
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                      trust_env=False,timeout=45) as client:
        response=client.put(path,params={'offset':0},content=payload,headers=headers)
        assert response.status_code==(499 if phase else 200),(response.status_code,response.text[:800])
        return response.json()


def verify(store,database_url=None,*,baseline=False):
    engine='postgresql' if database_url else 'sqlite'
    api=IsolatedServer('owner-cancel-'+engine,app='cancel_fixture:app',database_url=database_url,access_log=True)
    report={'passed':False,'baseline':baseline,'database':engine,'model_calls':0,'simulated_llm_calls':0,'checks':{}}
    print('OWNER_CANCEL_EVIDENCE',api.root,flush=True)
    fixture=api.root/'cancel_fixture.py'
    fixture.write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE+CANCEL,encoding='utf8')
    try:
        with api,ThreadPoolExecutor(max_workers=3) as workers:
            api.create_case('Cancellation during real S3 admission')
            first=copy.copy(api)
            case=first.call('GET',first.prefix)
            api.create_case('Other account remains writable')
            for mode in ('inventory','commit'):
                upload_id=first.call('POST',first.prefix+'/uploads',{'path':mode+'.bin','size':94208})['id']
                path=f'{first.prefix}/uploads/{upload_id}'
                probe='/fixture/cancel-ledger/'+upload_id
                payload=bytes(range(256))*368
                before=first.call('GET',probe)
                assert not before['credits'] and before['remaining']==len(payload)
                if mode=='inventory':
                    paused=api.root/'inventory-paused.json';release=api.root/'inventory-release'
                else:
                    paused=api.root/'commit-acknowledgement-paused';release=api.root/'commit-acknowledgement-release'
                pending=workers.submit(send,first,path,payload,phase=mode)
                try:
                    deadline=time.monotonic()+20
                    while not paused.exists():
                        assert time.monotonic()<deadline and not pending.done(),'Selected request did not reach its actual barrier'
                        time.sleep(.025)
                    if not database_url:assert writer_available(api)
                    during=first.call('GET',probe)
                    if mode=='inventory':assert during==before
                    else:
                        entered=set(during['grants'])-set(before['grants'])
                        assert len(entered)==1 and during['grants'][next(iter(entered))][0]=='active'
                        assert [r['state'] for r in during['credits']]==['active'] and during['remaining']==0
                    cancellation=first.call('POST','/fixture/active-cancel/'+mode)
                    assert cancellation['cancelled_live_task'] and cancellation['pending_cancellation']>0
                    target='Independent change during '+mode
                    assert api.call('PATCH',api.prefix,{'name':target})['name']==target
                    assert not pending.done(),'Cancelled request returned before outstanding I/O/commit drained'
                finally:release.touch()
                assert pending.result(timeout=35)=={'cancelled':True}
                settled=first.call('GET',probe)
                grants=set(settled['grants'])-set(before['grants'])
                expected=1 if baseline or mode=='commit' else 0
                assert len(grants)==expected,(mode,grants,settled)
                assert len(settled['credits'])==expected and all(row['state']=='settled' for row in settled['credits'])
                assert all(settled['grants'][key]==['settled',0] for key in grants)
                assert settled['remaining']==len(payload)
                remote=case['storage_prefix'].rstrip('/')+'/.vibeflow/uploads/'+upload_id+'/chunks/'
                assert not store.raw_client.list_objects_v2(Bucket=store.bucket,Prefix=remote).get('Contents')
                assert send(first,path,payload)['offset']==len(payload)
                first.call('POST',path+':complete')
                assert first.call('GET',first.prefix+'/raw?path='+mode+'.bin',raw=True)==payload
                report['checks'][mode]={'new_grants':len(grants),'new_transfers':len(settled['credits']),
                    'actual_chunk_writes_by_cancelled_request':0,'future_bytes_restored':settled['remaining'],
                    'retry_bytes_verified':len(payload),'independent_account_remained_writable':True}
        assert api.proc.poll() is not None
        report['passed']=True
    finally:
        fixture.unlink(missing_ok=True)
        (api.root/'owner-cancel.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('OWNER_CANCEL_PASS',api.root,flush=True)
    return 0


def verify_settlement_load(store,*,baseline=False):
    from contextlib import closing

    api=IsolatedServer('owner-cancel-settlement-load',app='cancel_fixture:app',access_log=True)
    report={'passed':False,'baseline':baseline,'model_calls':0,'simulated_llm_calls':0}
    print('OWNER_SETTLEMENT_LOAD_EVIDENCE',api.root,flush=True)
    fixture=api.root/'cancel_fixture.py'
    fixture.write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE+CANCEL,encoding='utf8')
    payload=bytes(range(256))*368

    def lease(sid):
        with closing(sqlite3.connect(api.root/'test.sqlite',timeout=.15)) as db:
            return db.execute('select heartbeat_at,lease_expires_at,phase from native_sessions where id=?',(sid,)).fetchone()

    def barrier(file,future,seconds=20):
        end=time.monotonic()+seconds
        while not file.exists():
            assert time.monotonic()<end and not future.done(),file.name
            time.sleep(.02)

    def upload(owner,identifier):
        route=owner.prefix+'/uploads/'+identifier
        pushed=send(owner,route,payload)
        assert pushed['offset']==len(payload)
        ledger=owner.call('GET','/fixture/cancel-ledger/'+identifier)
        credit,=ledger['credits']
        assert credit['state']=='settled' and ledger['grants'][credit['id']]==['settled',0] and ledger['remaining']==0
        return len(payload)

    try:
        with api,ThreadPoolExecutor(max_workers=10) as workers:
            api.create_case('Cancelled chunk awaiting owner settlement')
            cancelled=copy.copy(api)
            identifier=cancelled.call('POST',cancelled.prefix+'/uploads',{'path':'returned.bin','size':len(payload)})['id']
            peers=[]
            for index in range(6):
                api.create_case('Concurrent writer '+str(index))
                owner=copy.copy(api)
                item=owner.call('POST',owner.prefix+'/uploads',{'path':'peer.bin','size':len(payload)})['id']
                peers.append((owner,item))
            api.create_case('Independent native WSL terminal')
            opened=Queue(); stopped=Event(); sid=None
            stage=api.root/'commit-acknowledgement-release'; settle=api.root/'settlement-read-release'
            terminal=workers.submit(asyncio.run,terminal_for_proof(api,opened,stopped))
            try:
                while sid is None:
                    try:sid=opened.get(timeout=.2)
                    except Empty:
                        if terminal.done():terminal.result()
                pending=workers.submit(send,cancelled,cancelled.prefix+'/uploads/'+identifier,payload,phase='settlement')
                barrier(api.root/'commit-acknowledgement-paused',pending)
                assert cancelled.call('POST','/fixture/active-cancel/settlement')['cancelled_live_task']
                stage.touch()
                selected=api.root/'settlement-read-paused.json'
                barrier(selected,pending)
                report['actual_remote_metadata']=json.loads(selected.read_text('utf8'))
                report['sqlite_writer_available']=writer_available(api)
                assert report['sqlite_writer_available'] is not baseline
                original=lease(sid)
                if baseline:
                    time.sleep(6.7)
                    assert lease(sid)==original
                    report['wsl_heartbeat_advanced_during_settlement']=False
                else:
                    in_flight=[workers.submit(upload,*peer) for peer in peers]
                    end=time.monotonic()+22
                    report['bytes_settled_by_other_accounts']=[t.result(timeout=max(.1,end-time.monotonic())) for t in in_flight]
                    while lease(sid)==original:
                        assert time.monotonic()<end
                        time.sleep(.05)
                    updated=lease(sid)
                    assert updated[2]=='active' and all(a>b for a,b in zip(updated[:2],original[:2]))
                    report['wsl_heartbeat_advanced_during_settlement']={'before':original[:2],'during':updated[:2]}
                assert not pending.done()
                settle.touch()
                assert pending.result(timeout=15)=={'cancelled':True}
                ledger=cancelled.call('GET','/fixture/cancel-ledger/'+identifier)
                credit,=ledger['credits']
                assert credit['state']=='settled' and ledger['grants'][credit['id']]==['settled',0]
                assert ledger['remaining']==len(payload)
                report['cancelled_credit_settled_and_payload_restored']=ledger['remaining']
                assert api.call('DELETE','/terminal/sessions/'+sid)['ok']
                row=api.call('GET','/terminal/sessions/'+sid)
                assert row['phase']=='closed' and row['cleanup_verified'] is True
                report['wsl_stop_verified']=True
            finally:
                stage.touch();settle.touch();stopped.set()
            terminal.result(timeout=15)
        assert api.proc.poll() is not None
        report['passed']=True
    finally:
        fixture.unlink(missing_ok=True)
        (api.root/'owner-cancel.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('OWNER_SETTLEMENT_LOAD_PASS',api.root,flush=True)
    return 0


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',action='store_true')
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--settlement-load',action='store_true')
    args=parser.parse_args()
    if args.postgres and args.settlement_load:parser.error('--settlement-load uses SQLite lease evidence')
    if args.postgres:
        from mixed_read_capacity_checks import postgres
    runtime=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    with ObjectStore(executable=runtime,volume_max=10,authenticated=True) as store:
        if args.settlement_load:return await asyncio.to_thread(verify_settlement_load,store,baseline=args.baseline)
        if args.postgres:
            return await postgres(store,'owner-cancel',lambda target,url,_:verify(target,url,baseline=args.baseline))
        return await asyncio.to_thread(verify,store,baseline=args.baseline)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    raise SystemExit(asyncio.run(main()))
