"""Verify deferred owner settlement after real business commit, rollback and close."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import http.client
import json
from pathlib import Path
import secrets
import sys
import time

from s3testsupport import ObjectStore
from storage_admission_inventory_acceptance import writer_available
from testsupport import IsolatedServer, SERVER


FIXTURE = r'''
import asyncio,json,os,time,uuid
from contextvars import ContextVar
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from app import storage
from app.db import SessionLocal
from app.deps import get_current_user
from app.main import app
from app.models import OwnerStorageReservation
from app.routers.workspaces import get_owned_case
from app.services import attempt_objects,owner_storage_budget as budget
from app.services.async_ops import finish_before_cancel

root=Path(__file__).parent
selected=ContextVar('selected_deferred_test',default='')
closing=ContextVar('closing_deferred_test',default=False)
close=budget.close_journal
load=budget.load

def observed_close(ticket,*args,**kwargs):
    token=closing.set(True)
    try:return close(ticket,*args,**kwargs)
    finally:closing.reset(token)

def observed_load(raw,ticket):
    value,etag=load(raw,ticket)
    mode=selected.get()
    marker=root/(mode+'-s3-proof-paused.json')
    if closing.get() and mode and not marker.exists():
        assert value and not value['closed'] and value['used']>0 and etag
        temp=marker.with_suffix('.tmp')
        temp.write_text(json.dumps({'id':ticket['id'],'real_remote_journal':True,'bytes':value['used']}),encoding='utf8')
        temp.replace(marker)
        end=time.monotonic()+20
        while not (root/(mode+'-s3-proof-release')).exists():
            if time.monotonic()>=end:raise TimeoutError('Unreleased settlement proof')
            time.sleep(.02)
    return value,etag

@app.post('/api/v1/fixture/deferred/{case_id}/{mode}')
async def deferred(case_id:int,mode:str,user=Depends(get_current_user)):
    assert mode in {'commit','rollback','close'}
    token=selected.set(mode)
    try:
        async with SessionLocal() as db:
            case=await get_owned_case(case_id,user,db,write=True)
            async with budget.hold(db,user.id) as grant:
                path='.vibeflow/live-store/objects/'+str(uuid.uuid4())+'/deferred.bin'
                case.name='New name '+mode
                await db.flush()
                await finish_before_cancel(asyncio.to_thread(storage.s3().put_object,
                    **attempt_objects.arguments(case.s3_prefix,path),IfNoneMatch='*',Body=b'actual deferred S3 bytes'))
                ticket=grant.ticket['id']
            async with db.begin_nested():
                assert await db.scalar(select(OwnerStorageReservation.id).where(OwnerStorageReservation.id==ticket))==ticket
            marker=root/(mode+'-body-returned')
            temp=marker.with_suffix('.tmp')
            temp.write_text(json.dumps({'path':path,'id':ticket}),encoding='utf8')
            temp.replace(marker)
            async with asyncio.timeout(23):
                while not (root/(mode+'-transaction-release')).exists():await asyncio.sleep(.02)
            if mode=='commit':await db.commit()
            elif mode=='rollback':await db.rollback()
        return {'id':ticket,'path':path}
    finally:selected.reset(token)

@app.get('/api/v1/fixture/deferred-ledger/{identifier}')
async def state(identifier:str,user=Depends(get_current_user)):
    async with SessionLocal() as read:
        row=await read.scalar(select(OwnerStorageReservation).where(
            OwnerStorageReservation.id==identifier,OwnerStorageReservation.user_id==user.id))
        assert row is not None
        receipt=json.loads(row.receipt_json)
        return {'state':row.state,'remaining':row.remaining_bytes,'closed':receipt.get('closed')}

@app.post('/api/v1/fixture/deferred-retry')
async def retry(user=Depends(get_current_user)):
    await budget.recover_owner(user.id,wait=True)
    return {'checked':True}

@app.post('/api/v1/fixture/deferred-cut')
async def cut(user=Depends(get_current_user)):
    os._exit(76)

budget.close_journal=observed_close
budget.load=observed_load
'''


def run(store,database_url=None,*,baseline=False,crash=False):
    engine='postgresql' if database_url else 'sqlite'
    api=IsolatedServer('owner-deferred-'+engine,app='deferred_fixture:app',database_url=database_url,access_log=True)
    report={'passed':False,'baseline':baseline,'crash':crash,'database':engine,'actual_model_calls':0,'simulated_llm_calls':0,'checks':{}}
    print('OWNER_DEFERRED_EVIDENCE',api.root,flush=True)
    fixture=api.root/'deferred_fixture.py'
    settings={**store.settings(),'VIBEFLOW_JWT_SECRET':secrets.token_hex(32)}
    fixture.write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')

    def reached(marker,pending):
        end=time.monotonic()+15
        while not marker.exists():
            if pending.done():pending.result()
            assert time.monotonic()<end,marker.name
            time.sleep(.02)

    try:
        with api,ThreadPoolExecutor(max_workers=2) as workers:
            api.create_case('Unaffected account')
            peer=copy.copy(api)
            for mode in (('commit',) if crash else ('commit','rollback','close')):
                original='Original name '+mode
                api.create_case(original)
                proof=api.root/(mode+'-s3-proof-paused.json')
                proof_release=api.root/(mode+'-s3-proof-release')
                body=api.root/(mode+'-body-returned')
                transaction_release=api.root/(mode+'-transaction-release')
                request=workers.submit(api.call,'POST',f'/fixture/deferred/{api.case_id}/{mode}')
                try:
                    if baseline:
                        reached(proof,request)
                        assert not body.exists()
                        if not database_url:assert not writer_available(api)
                        assert api.call('GET',api.prefix)['name']==original
                        proof_release.touch()
                        reached(body,request)
                        transaction_release.touch()
                    else:
                        reached(body,request)
                        assert not proof.exists(),'S3 settlement ran before the caller ended SQL'
                        assert api.call('GET',api.prefix)['name']==original
                        if not database_url:assert not writer_available(api)
                        transaction_release.touch()
                        reached(proof,request)
                        if not database_url:assert writer_available(api)
                        other='Changed during '+mode+' settlement'
                        assert peer.call('PATCH',peer.prefix,{'name':other})['name']==other
                        observed='New name commit' if mode=='commit' else original
                        assert api.call('GET',api.prefix)['name']==observed
                        if not crash:proof_release.touch()
                    evidence=json.loads(proof.read_text('utf8'))
                    if crash:
                        try:api.call('POST','/fixture/deferred-cut')
                        except (http.client.RemoteDisconnected,ConnectionResetError):pass
                        else:raise AssertionError('API remained live after the isolated cut')
                        assert api.proc.wait(timeout=10)==76
                        try:request.result(timeout=8)
                        except (http.client.RemoteDisconnected,ConnectionResetError):pass
                        else:raise AssertionError('Ended process returned a write response')
                finally:proof_release.touch();transaction_release.touch()
                if crash:
                    api.__exit__(None,None,None)
                    api.__enter__()
                    result=json.loads(body.read_text('utf8'))
                    active=api.call('GET','/fixture/deferred-ledger/'+result['id'])
                    assert active['state']=='active' and active['remaining']>0 and active['closed'] is None
                    assert api.call('POST','/fixture/deferred-retry')['checked']
                    report['real_exit_after_sql_before_s3_close']=76
                    report['post_restart_retained_pool_recovered']=True
                else:result=request.result(timeout=30)
                assert result['id']==evidence['id']
                if baseline and mode!='commit':assert api.call('POST','/fixture/deferred-retry')['checked']
                claim=api.call('GET','/fixture/deferred-ledger/'+result['id'])
                assert claim=={'state':'settled','remaining':0,'closed':True},claim
                current=api.call('GET',api.prefix)
                assert current['name']==('New name commit' if mode=='commit' else original)
                actual=store.raw_client.get_object(Bucket=store.bucket,Key=current['storage_prefix']+'/'+result['path'])
                try:assert actual['Body'].read()==b'actual deferred S3 bytes'
                finally:actual['Body'].close()
                report['checks'][mode]={'real_remote_journal':evidence['real_remote_journal'],
                    'sql_outcome_verified':True,'proof_after_outer_transaction':not baseline,
                    'independent_account_wrote_during_settlement':not baseline,'owner_credit_settled':True}
        assert api.proc.poll() is not None
        report['passed']=True
    finally:
        fixture.unlink(missing_ok=True)
        (api.root/'owner-deferred.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('OWNER_DEFERRED_PASS',api.root,flush=True)
    return 0


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',action='store_true')
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--exit-at-proof',action='store_true')
    args=parser.parse_args()
    if args.baseline and args.exit_at_proof:parser.error('The baseline never reaches S3 after SQL')
    if args.postgres:
        from mixed_read_capacity_checks import postgres
    executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    with ObjectStore(executable=executable,volume_max=10,authenticated=True) as store:
        if args.postgres:
            return await postgres(store,'owner-deferred',lambda target,url,_:run(target,url,baseline=args.baseline,crash=args.exit_at_proof))
        return await asyncio.to_thread(run,store,baseline=args.baseline,crash=args.exit_at_proof)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    raise SystemExit(asyncio.run(main()))
