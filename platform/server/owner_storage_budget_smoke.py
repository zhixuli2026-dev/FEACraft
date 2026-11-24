"""Versioned S3 owner credit survives caller SQL rollback and lost processes."""
import argparse,asyncio,json,re,tempfile,time,uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from testsupport import IsolatedServer,SERVER
from publication_finalizing_smoke import INSPECT,peer
from job_release_smoke import wait_file
from node_storage_budget_smoke import stop

OBSERVE='''
import asyncio,json,time,uuid
from pathlib import Path
from sqlalchemy import select
from app import storage
from app.models import OwnerStorageReservation,Case
from app.services import owner_storage_budget as budget,quota,case_provision as provision
root=Path(__file__).parent
active_task=None
async def quotas(db,user):
 p=root/'quota-limit'
 return {**quota.DEFAULT_QUOTAS,'storage_mb':int(p.read_text()) if p.exists() else 4}
quota.quotas_for=quotas
@app.get('/api/v1/fixture/owner/{case_id}')
async def state(case_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db,write=False)
  rows=list(await db.scalars(select(OwnerStorageReservation).where(OwnerStorageReservation.user_id==user.id).order_by(OwnerStorageReservation.created_at)))
  return {'rows':[{**{k:getattr(r,k) for k in ('id','size','remaining_bytes','state')},'receipt':json.loads(r.receipt_json)} for r in rows],
   'usage':await quota.usage(db,user),'name':case.name,'prefix':case.s3_prefix}
@app.post('/api/v1/fixture/owner-probe')
async def probe(user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  async with budget.hold(db,user.id):await db.commit()
 return {'reconciled':True}
@app.post('/api/v1/fixture/owner-cancel')
async def cancel():
 assert active_task is not None;active_task.cancel()
 return {'cancelled':True}
@app.post('/api/v1/fixture/owner-write/{case_id}')
async def writing(case_id:int,body:dict,user:User=Depends(get_current_user)):
 global active_task
 active_task=asyncio.current_task();mode=body['mode'];(root/'mode').write_text(mode)
 if mode=='quota':(root/'quota-limit').write_text('0')
 if mode=='credit_exhaust':
  measured=await quota._storage_usage(user)
  amount=4*1024*1024-measured.bytes-(128*1024+1024)
  from functools import partial
  from app.config import get_settings
  await provision.io(partial(budget.raw_client().put_object,Bucket=get_settings().s3_bucket,Key=f'workspaces/{user.id}/.capacity-test-filler',Body=b'f'*amount,IfNoneMatch='*'))
 async with SessionLocal() as db:
  async with budget.hold(db,user.id):
   case=await get_owned_case(case_id,user,db,write=True)
   case.name='UNCOMMITTED NAME';await db.flush()
   path='.vibeflow/live-store/objects/'+str(uuid.uuid5(uuid.NAMESPACE_URL,case.s3_prefix))+'/budget.bin'
   from app.services.attempt_objects import arguments
   from functools import partial
   await provision.io(partial(storage.s3().put_object,**arguments(case.s3_prefix,path),Body=b'x'*(128*1024),IfNoneMatch='*'))
   if mode=='rollback':raise RuntimeError('Injected caller rollback')
   case.name='Owner budget '+mode
   if mode!='deferred_commit':await db.commit()
 return {'written':True}
'''

FAULT='''
raw=budget.raw_client();original_put=raw.put_object
def mode():return (root/'mode').read_text() if (root/'mode').exists() else ''
def wait():
 (root/'ready').touch();deadline=time.monotonic()+90
 while not (root/'release').exists():
  if time.monotonic()>deadline:raise TimeoutError('Owner capacity test hold')
  time.sleep(.02)
def putting(**args):
 selected=mode();payload=args['Key'].endswith('/budget.bin')
 ledger='/.storage-capacity/' in args['Key'] and args['Key'].endswith('/head.json')
 value=json.loads(args['Body']) if ledger else None
 if ledger and not value['closed'] and selected in {'head_unknown_before','head_unknown_after'} and not (root/'head-faulted').exists():
  (root/'head-faulted').touch();(root/'late-head.json').write_text(json.dumps({k:(v.decode() if isinstance(v,bytes) else v) for k,v in args.items()}))
  if selected=='head_unknown_after':original_put(**args)
  raise TimeoutError('Lost capacity journal response')
 if ledger and value['closed'] and selected=='fence_unknown' and not (root/'fence-faulted').exists():
  (root/'fence-faulted').touch();original_put(**args)
  raise TimeoutError('Lost final fence response')
 if payload and selected in {'normal','live_busy','cancel_write','unknown_before','death_before'}:wait()
 if payload and selected=='unknown_before':raise TimeoutError('Unknown payload did not materialize')
 result=original_put(**args)
 if payload and selected=='death_after':wait()
 if payload and selected=='unknown_after':raise TimeoutError('Lost successful payload response')
 return result
raw.put_object=putting
original_acquire=budget.acquire
async def acquiring(*args,**kwargs):
 result=await original_acquire(*args,**kwargs)
 if mode()=='claim_cancel':
  (root/'ready').touch()
  while not (root/'release').exists():await asyncio.sleep(.02)
 return result
budget.acquire=acquiring
'''


def verify(store,modes,url=None):
    report=Path(tempfile.mkdtemp(prefix='owner-storage-runs-',dir=SERVER.parents[1]/'tmp'))
    database=url or 'sqlite+aiosqlite:///'+(report/'test.sqlite').as_posix()
    store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    checks=[];runs=[]
    for mode in modes:
        api=IsolatedServer('owner-storage-'+mode,app='owner_fixture:app',database_url=url)
        settings={**store.settings(),'VIBEFLOW_DATABASE_URL':database}
        (api.root/'owner_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\nfrom app.main import app\n'+INSPECT+OBSERVE+FAULT,encoding='utf8')
        with api:
            api.env['VIBEFLOW_DATABASE_URL']=database
            case=api.create_case('Owner budget '+mode);case_id=case['id']
            if mode in {'owner_noop','maintenance_noop'}:api.call('POST','/case-provisions:recover')
            baseline=api.call('GET',f'/fixture/owner/{case_id}')
            path=f'/fixture/owner-write/{case_id}'
            if mode in {'normal','live_busy','cancel_write','unknown_before','death_before','death_after','claim_cancel'}:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    future=pool.submit(api.call,'POST',path,{'mode':mode},expect=(200,500),raw=True)
                    wait_file(api.root/'ready',seconds=30)
                    held=api.call('GET',f'/fixture/owner/{case_id}')
                    row=held['rows'][-1]
                    assert row['state']=='active' and row['size']>128*1024 and row['remaining_bytes']==row['size'],row
                    assert held['name']=='Owner budget '+mode
                    competing=None
                    if mode=='live_busy':
                        competing=pool.submit(api.call,'POST','/fixture/owner-probe',expect=(200,409))
                        time.sleep(.25)
                        if competing.done():assert competing.result()['detail']['code']=='storage_quota_busy'
                    if mode.startswith('death'):
                        stop(api.proc)
                        try:future.result(timeout=15)
                        except OSError:pass
                        else:raise AssertionError('Killed creator returned a success')
                    else:
                        if mode in {'claim_cancel','cancel_write'}:
                            api.call('POST','/fixture/owner-cancel');time.sleep(.2)
                            assert not future.done(),'Cancellation returned before its writer drained'
                        (api.root/'release').touch();future.result(timeout=30)
                        if competing is not None:
                            reply=competing.result(timeout=30)
                            if 'detail' in reply:
                                assert reply['detail']['code']=='storage_quota_busy'
                                api.call('POST','/fixture/owner-probe')
            elif mode=='owner_noop':
                api.call('POST','/case-provisions:recover')
                api.call('POST','/case-provisions:recover')
                api.call('POST','/fixture/owner-probe')
            elif mode=='maintenance_noop':
                api.call('POST','/case-provisions:maintenance')
                api.call('POST','/case-provisions:maintenance')
                api.call('POST','/fixture/owner-probe')
            else:api.call('POST',path,{'mode':mode},expect=200 if mode=='deferred_commit' else 429 if mode in {'quota','credit_exhaust'} else 500,raw=True)
            reader=peer(api);script=reader.root/'finalizing_peer.py'
            script.write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+script.read_text(encoding='utf8')+OBSERVE,encoding='utf8')
            with reader:
                before=reader.call('GET',f'/fixture/owner/{case_id}')
                candidate=before['rows'][-1]
                if mode=='deferred_commit':
                    assert candidate['state']=='settled' and candidate['remaining_bytes']==0
                    assert candidate['receipt']['closed'] is True
                reader.call('POST','/fixture/owner-probe')
                after=reader.call('GET',f'/fixture/owner/{case_id}')
                chosen=next(r for r in after['rows'] if r['id']==candidate['id'])
                unknown=mode in {'unknown_before','death_before'}
                assert chosen['remaining_bytes']==(128*1024 if unknown else 0),(mode,chosen)
                assert chosen['state']==('active' if unknown else 'settled'),(mode,chosen)
                assert after['name']=='Owner budget '+mode
                assert after['usage']['storage_bytes_reserved']==(128*1024 if unknown else 0),after
                if mode in {'owner_noop','maintenance_noop'}:assert after['usage']['storage_bytes_used']==baseline['usage']['storage_bytes_used'],'Empty recovery/maintenance allocated remote objects'
                assert not script.with_suffix('.replayed').exists()
                if mode.startswith('head_unknown'):
                    from botocore.exceptions import ClientError
                    late=json.loads((api.root/'late-head.json').read_text());late['Body']=late['Body'].encode()
                    try:store.raw_client.put_object(**late)
                    except ClientError as error:assert error.response['ResponseMetadata']['HTTPStatusCode']==412
                    else:raise AssertionError('Closed journal admitted a delayed request')
                result={'before':before,'after':after,'reader':str(reader.root)}
            (api.root/'owner-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
            checks.append(mode);runs.append({'mode':mode,'root':str(api.root)})
            print('PASS owner capacity '+mode,flush=True)
    return {'passed':True,'count':len(checks),'checks':checks,'runs':runs}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+');args=parser.parse_args()
    modes=args.modes or ['normal','live_busy','unknown_before','unknown_after','death_before','death_after','rollback','deferred_commit','head_unknown_before','head_unknown_after','fence_unknown','quota','credit_exhaust','claim_cancel','cancel_write','owner_noop','maintenance_noop']
    from s3testsupport import ObjectStore
    from s3_typed_publication_smoke import RUNTIME
    report=Path(tempfile.mkdtemp(prefix='owner-storage-evidence-',dir=SERVER.parents[1]/'tmp'))
    with ObjectStore(executable=RUNTIME) as store:results={'sqlite':await asyncio.to_thread(verify,store,modes)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_ownerstorage_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_ownerstorage_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            with ObjectStore(executable=RUNTIME) as store:results['postgresql']=await asyncio.to_thread(verify,store,modes,target)
        finally:
            if created:await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('OWNER_STORAGE_BUDGET_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
