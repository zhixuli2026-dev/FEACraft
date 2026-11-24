"""Actual SQL owner admission: contention, stale snapshots, budgets and API peers."""
import argparse,asyncio,hashlib,json,os,re,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fastapi import HTTPException
from sqlalchemy import event,select,text,update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from testsupport import SERVER,IsolatedServer
SOURCES=['app/services/storage_admission.py','app/services/async_ops.py','app/services/owner_storage_budget.py','app/services/case_read_capacity.py']
def hashes():return {p:hashlib.sha256((SERVER/p).read_bytes()).hexdigest() for p in SOURCES}

async def core(url=None):
    from app.db import Base
    from app.models import User
    from app.config import get_settings
    from app.services import storage_admission as admission,request_deadline as deadline
    from app.services.async_ops import finish_before_cancel
    root=Path(tempfile.mkdtemp(prefix='owner-lock-core-',dir=SERVER.parents[1]/'tmp'))
    sqlite=url is None;url=url or 'sqlite+aiosqlite:///'+(root/'test.sqlite').as_posix()
    engine=create_async_engine(url,connect_args={'timeout':30} if sqlite else {})
    if sqlite:
        @event.listens_for(engine.sync_engine,'connect')
        def config(connection,_):
            cursor=connection.cursor();cursor.execute('PRAGMA journal_mode=WAL');cursor.execute('PRAGMA busy_timeout=30000');cursor.close()
        async with engine.begin() as connection:await connection.run_sync(Base.metadata.create_all)
    session=async_sessionmaker(engine,expire_on_commit=False)
    report={'passed':False,'database':'sqlite' if sqlite else 'postgresql','model_calls':0,'mock_llm_calls':0,'checks':[]};checks=report['checks']
    settings=get_settings();old=settings.storage_measurement_timeout_seconds;settings.storage_measurement_timeout_seconds=2
    async def busy(operation):
        try:await operation
        except HTTPException as error:assert error.status_code==409 and error.detail['code']=='storage_quota_busy';return error
        raise AssertionError('Actual contended quota lock granted')
    try:
        async with session() as db:
            db.add_all([User(id=1,email='actual-lock-one@example.com',name='original',password_hash='not-an-authentication-fixture'),User(id=2,email='actual-lock-two@example.com',name='other',password_hash='not-an-authentication-fixture')]);await db.commit()
        async with session() as held,session() as waiting:
            await admission.lock_owner(held,1);started=time.monotonic();await busy(admission.lock_owner(waiting,1));elapsed=time.monotonic()-started
            assert elapsed<1,elapsed
            if sqlite:assert (await waiting.execute(text('PRAGMA busy_timeout'))).scalar_one()==30000
            await waiting.rollback()
            if sqlite:await busy(admission.lock_owner(waiting,2))
            else:await admission.lock_owner(waiting,2)
            checks.append({'check':'actual_nowait_contention_structured_409_preserves_backend_lock_scope','elapsed_seconds':round(elapsed,4),'other_user_excluded':sqlite})
        async with session() as held,session() as waiting:
            await admission.lock_owner(held,1);task=asyncio.create_task(admission.lock_owner(waiting,1,wait=True));await asyncio.sleep(.1);assert not task.done();await held.rollback();await task
            async with session() as competitor:await busy(admission.lock_owner(competitor,1))
            if sqlite:assert (await waiting.execute(text('PRAGMA busy_timeout'))).scalar_one()==30000
            await waiting.rollback()
            checks.append({'check':'actual_waiter_acquires_after_release_and_keeps_transaction_fence'})
        settings.storage_measurement_timeout_seconds=.15
        async with session() as held,session() as waiting:
            await admission.lock_owner(held,1);started=time.monotonic();await busy(admission.lock_owner(waiting,1,wait=True));elapsed=time.monotonic()-started
            assert .06<=elapsed<.8,elapsed
            if sqlite:assert (await waiting.execute(text('PRAGMA busy_timeout'))).scalar_one()==30000
            checks.append({'check':'actual_settlement_wait_uses_one_configured_budget','configured_seconds':.15,'elapsed_seconds':round(elapsed,4)})
        settings.storage_measurement_timeout_seconds=2
        async with session() as held,session() as waiting:
            await admission.lock_owner(held,1);started=time.monotonic()
            try:
                async with deadline.scope(.15):await admission.lock_owner(waiting,1,wait=True)
            except (TimeoutError,HTTPException) as error:
                if isinstance(error,HTTPException):assert error.status_code==409
            else:raise AssertionError('Expired parent lock admitted')
            elapsed=time.monotonic()-started;assert elapsed<.8
            if sqlite:assert (await waiting.execute(text('PRAGMA busy_timeout'))).scalar_one()==30000
            checks.append({'check':'actual_parent_deadline_does_not_refresh_for_lock_wait','parent_seconds':.15,'elapsed_seconds':round(elapsed,4)})
        async with session() as held,session() as waiting:
            await admission.lock_owner(held,1);captured={}
            async def operation():
                try:await admission.lock_owner(waiting,1,wait=True);captured['unexpected_return']=True
                finally:
                    if sqlite:captured['restored_timeout']=(await waiting.execute(text('PRAGMA busy_timeout'))).scalar_one()
                    await finish_before_cancel(waiting.rollback())
            task=asyncio.create_task(operation());await asyncio.sleep(.05);accepted=[]
            for _ in range(3):accepted.append(task.cancel());await asyncio.sleep(.005)
            if sqlite:assert not task.done()
            await held.rollback()
            try:await task
            except asyncio.CancelledError:pass
            else:raise AssertionError('Cancellation was swallowed')
            assert not captured.get('unexpected_return')
            if sqlite:assert captured['restored_timeout']==30000
            async with session() as fresh:await admission.lock_owner(fresh,1)
            checks.append({'check':'actual_repeated_cancellation_drains_wait_and_restores_connection','cancel_accepted':accepted,'restored_timeout_ms':captured.get('restored_timeout')})
        async with session() as db:
            await admission.lock_owner(db,1);await db.execute(update(User).where(User.id==1).values(name='caller pending change'))
            await admission.lock_owner(db,1);await db.rollback()
        async with session() as db:assert await db.scalar(select(User.name).where(User.id==1))=='original'
        checks.append({'check':'reentrant_admission_does_not_commit_or_rollback_caller_business'})
        async with session() as db:
            completed=False
            try:
                async with deadline.scope(.1):
                    try:await asyncio.sleep(.3)
                    finally:
                        await finish_before_cancel(admission.lock_owner(db,1,wait=True,settlement=True));completed=True
            except TimeoutError:pass
            else:raise AssertionError('Actual parent timer did not expire')
            assert completed;await db.rollback()
        checks.append({'check':'actual_expired_request_allows_only_explicit_bounded_settlement'})
        if sqlite:
            async with session() as stale:
                await stale.execute(text('BEGIN'));assert await stale.scalar(select(User.name).where(User.id==1))=='original'
                async with session() as writer:
                    await admission.lock_owner(writer,1);await writer.execute(update(User).where(User.id==1).values(name='committed replacement'));await writer.commit()
                await busy(admission.lock_owner(stale,1));assert await stale.scalar(select(User.name).where(User.id==1))=='original'
                assert (await stale.execute(text('PRAGMA busy_timeout'))).scalar_one()==30000
                await stale.rollback();await admission.lock_owner(stale,1)
                assert await stale.scalar(select(User.name).where(User.id==1))=='committed replacement'
            checks.append({'check':'actual_BUSY_SNAPSHOT_refuses_without_replaying_or_losing_caller_snapshot'})
            empty=create_async_engine('sqlite+aiosqlite:///'+(root/'missing-users.sqlite').as_posix())
            try:
                async with async_sessionmaker(empty)() as db:
                    try:await admission.lock_owner(db,1)
                    except OperationalError as error:assert getattr(error.orig,'sqlite_errorcode',None)==1
                    else:raise AssertionError('Missing actual table was masked as a busy conflict')
            finally:await empty.dispose()
            checks.append({'check':'actual_missing_table_operational_error_not_masked_as_quota_busy'})
        async with session() as db:
            token=deadline._current.set(deadline.Deadline(time.monotonic()-1))
            try:
                try:await admission.lock_owner(db,1)
                except TimeoutError:assert not db.in_transaction()
                else:raise AssertionError('Explicit expired parent admitted')
            finally:deadline._current.reset(token)
        checks.append({'check':'explicit_expired_parent_protocol_refuses_before_SQL'})
        report['passed']=True
    finally:
        settings.storage_measurement_timeout_seconds=old;await engine.dispose();report['source_hashes']=hashes();(root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('OWNER_LOCK_CORE_OK '+str(root)+' checks='+str(len(checks)),flush=True);return root


FIXTURE=r'''
import asyncio,os,time
from fastapi import Depends,HTTPException
from app.main import app
from app.db import get_db,SessionLocal
from app.deps import get_current_user
from app.config import get_settings
from app.services.storage_admission import lock_owner
from app.services import request_deadline
from app.services import owner_storage_budget
from app.models import OwnerStorageReservation
from sqlalchemy import select,func
from app.services.async_ops import finish_before_cancel
held=None;entered=asyncio.Event();release=asyncio.Event()
@app.get('/api/v1/testing/lock-state')
async def state(user=Depends(get_current_user)):return {'pid':os.getpid(),'held':entered.is_set() and not release.is_set()}
@app.post('/api/v1/testing/lock-control')
async def control(body:dict,user=Depends(get_current_user)):
 global held
 if body['action']=='exit':os._exit(79)
 if body['action']=='release':release.set();await held;held=None;return {'released':True}
 assert held is None;entered.clear();release.clear()
 async def hold():
  async with SessionLocal() as db:
   await lock_owner(db,user.id);entered.set();await release.wait();await finish_before_cancel(db.rollback())
 held=asyncio.create_task(hold());await entered.wait();return {'held':True}
@app.post('/api/v1/testing/lock-admit')
async def admit(body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 started=time.monotonic()
 try:
  async with request_deadline.scope(body.get('seconds',2)):
   await lock_owner(db,user.id,wait=body.get('wait',False))
   await finish_before_cancel(db.commit());return {'admitted':True,'elapsed':time.monotonic()-started}
 except TimeoutError:
  await finish_before_cancel(db.rollback());raise HTTPException(408,{'code':'owner_lock_deadline'})
 finally:await finish_before_cancel(db.rollback())
@app.post('/api/v1/testing/lock-budget')
async def budget(body:dict,user=Depends(get_current_user)):
 get_settings().storage_measurement_timeout_seconds=body['seconds'];return {'seconds':body['seconds']}
@app.post('/api/v1/testing/lock-expired-settlement')
async def expired_settlement(user=Depends(get_current_user),db=Depends(get_db)):
 user_id=user.id
 try:
  async with request_deadline.scope(.15):
   async with owner_storage_budget.hold(db,user_id):await asyncio.sleep(.4)
 except TimeoutError:pass
 else:raise AssertionError('Real request deadline did not expire')
 active=await db.scalar(select(func.count()).select_from(OwnerStorageReservation).where(OwnerStorageReservation.user_id==user_id,OwnerStorageReservation.state=='active'))
 assert active==0
 return {'actual_timeout':True,'active_owner_pools':active}
'''


def api_run(url=None,store=None):
    import httpx
    from app.services import process_identity_native as identity
    api=IsolatedServer('owner-lock-api',app='owner_lock_fixture:app',database_url=url)
    peer=IsolatedServer('owner-lock-peer',app='owner_lock_peer:app',database_url=url)
    settings=store.settings() if store else {}
    (api.root/'owner_lock_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')
    report={'passed':False,'database':'postgresql' if url else 'sqlite','storage':'s3' if store else 'fs','model_calls':0,'mock_llm_calls':0,'checks':[]};checks=report['checks'];births={}
    print('OWNER_LOCK_API '+str(api.root),flush=True)
    def request(server,method,path,**kw):return httpx.request(method,server.base+'/api/v1'+path,headers={'Authorization':'Bearer '+server.token},trust_env=False,timeout=15,**kw)
    try:
        with api:
            api.create_case('Actual quota lock API');births['main']=identity.observe(api.call('GET','/testing/lock-state')['pid'])
            shared={k:v for k,v in api.env.items() if k.startswith('VIBEFLOW_')};shared.update(settings)
            (peer.root/'owner_lock_peer.py').write_text('import os\nos.environ.update('+repr(shared)+')\n'+FIXTURE,encoding='utf8');peer.token=api.token
            api.call('POST','/testing/lock-budget',{'seconds':2})
            with peer:
                births['peer']=identity.observe(peer.call('GET','/testing/lock-state')['pid'])
                peer.call('POST','/testing/lock-control',{'action':'acquire'})
                start=time.monotonic();r=request(api,'POST','/testing/lock-admit',json={})
                assert r.status_code==409 and r.json()['detail']['code']=='storage_quota_busy';assert time.monotonic()-start<1.5
                checks.append({'check':'actual_peer_owner_lock_returns_structured_conflict_without_500'})
                r=request(api,'POST','/testing/lock-admit',json={'wait':True,'seconds':.2})
                assert r.status_code in {408,409},(r.status_code,r.text[:800])
                if store:
                    api.call('POST','/testing/lock-budget',{'seconds':.2});start=time.monotonic()
                    r=request(api,'GET',api.prefix+'/tree');assert r.status_code==409,(r.status_code,r.text[:800]);assert time.monotonic()-start<1.5
                    api.call('POST','/testing/lock-budget',{'seconds':2})
                    checks.append({'check':'actual_S3_tree_exhausted_owner_admission_returns_409_within_budget'})
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(request,api,'POST','/testing/lock-admit',json={'wait':True,'seconds':2})
                    time.sleep(.1);assert not future.done();peer.call('POST','/testing/lock-control',{'action':'release'})
                    response=future.result();assert response.status_code==200,(response.status_code,response.text[:800])
                checks.append({'check':'actual_waiting_API_admits_only_after_peer_releases'})
                assert isinstance(api.call('GET',api.prefix+'/tree')['entries'],list)
                api.call('PUT',api.prefix+'/file',{'path':'Ref/owner-lock.txt','content':'Actual admitted file bytes'})
                assert api.call('GET',api.prefix+'/raw?path=Ref/owner-lock.txt',raw=True)==b'Actual admitted file bytes'
                checks.append({'check':'real_file_read_write_after_contention_preserves_bytes'})
                if store:
                    value=api.call('POST','/testing/lock-expired-settlement',{});assert value['actual_timeout'] and value['active_owner_pools']==0
                    checks.append({'check':'actual_expired_remote_owner_scope_settles_without_leaking_pool'})
                peer.call('POST','/testing/lock-control',{'action':'acquire'})
                try:request(peer,'POST','/testing/lock-control',json={'action':'exit'})
                except httpx.TransportError:pass
                else:raise AssertionError('Actual API abrupt exit returned HTTP')
                until=time.monotonic()+10
                while identity.probe(births['peer'])['state']!='exited':assert time.monotonic()<until;time.sleep(.03)
                response=request(api,'POST','/testing/lock-admit',json={'wait':True,'seconds':2});assert response.status_code==200
                checks.append({'check':'actual_peer_API_exit_releases_SQL_fence_without_recreating_quota'})
                report['functional_passed']=True
    except BaseException as error:report['failure']={'type':type(error).__name__,'detail':str(error)[:1200]};raise
    finally:
        report['births']=births;report['exit_probes']={}
        for name,birth in births.items():
            until=time.monotonic()+10
            while True:
                probe=identity.probe(birth)
                if probe['state']=='exited' or time.monotonic()>=until:break
                time.sleep(.03)
            report['exit_probes'][name]=probe
        report['passed']=bool(report.get('functional_passed') and len(births)==2 and all(p['state']=='exited' for p in report['exit_probes'].values()))
        report['peer_root']=str(peer.root);report['source_hashes']=hashes();(api.root/'lock-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    assert report['passed'];print('OWNER_LOCK_API_OK '+str(api.root)+' checks='+str(len(checks)),flush=True);return api.root


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--service-only',action='store_true');parser.add_argument('--api-only',action='store_true');parser.add_argument('--postgres',action='store_true');parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    async def run(url=None):
        if not args.api_only:await core(url)
        if args.service_only:return
        def api():
            if not args.s3:return api_run(url)
            from s3testsupport import ObjectStore
            store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed');receipt={'passed':False}
            print('OWNER_LOCK_S3 '+str(store.root),flush=True)
            try:
                with store:
                    root=api_run(url,store);receipt.update(passed=True,api_root=str(root));return root
            finally:
                receipt['store_stopped']=store.proc is not None and store.proc.poll() is not None;(store.root/'owner-lock-result.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf8')
        return await asyncio.to_thread(api)
    if not args.postgres:await run();return
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    u=make_url(Settings(_env_file=SERVER/'.env').database_url);assert u.drivername=='postgresql+asyncpg' and u.host in {'localhost','127.0.0.1','::1'}
    name='tmp_vf_repeat_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_repeat_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='owner-lock-postgres-',dir=SERVER.parents[1]/'tmp'));report={'passed':False,'database':name,'developer_database_modified':False}
    print('OWNER_LOCK_POSTGRES '+str(root),flush=True)
    conn=await asyncpg.connect(host=u.host,port=u.port or 5432,user=u.username,password=u.password,database='postgres',timeout=10);created=False
    try:
        await conn.execute('CREATE DATABASE "'+name+'"');created=True;url=u.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head');value=await run(url);report.update(passed=True,api_root=str(value) if value else None)
    finally:
        try:
            if created:await conn.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:
            await conn.close();(root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
if __name__=='__main__':asyncio.run(main())
