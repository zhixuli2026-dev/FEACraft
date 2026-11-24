"""Real versioned S3 physical writes: durable quota, unknown replies and recovery."""
import argparse,asyncio,json,re,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from testsupport import IsolatedServer,SERVER
from publication_finalizing_smoke import INSPECT,peer
from job_release_smoke import wait_file
from node_storage_budget_smoke import stop

OBSERVE='''
import asyncio,io,json,os,time,uuid
from pathlib import Path
from app import storage
from app.models import StorageWriteReservation,Case
from app.services import remote_write_budget as budget,storage_operations as operations,quota
from app.services.durable_events import lock_case_execution
from app.services.case_write import assert_case_available
from app.services import process_identity_native as identity
root=Path(__file__).parent
async def quotas(db,user):
 limit=root/'quota-limit'
 return {**quota.DEFAULT_QUOTAS,'storage_mb':int(limit.read_text()) if limit.exists() else 4}
quota.quotas_for=quotas
def key_for(prefix):return '.vibeflow/file-transactions/objects/'+str(uuid.uuid5(uuid.NAMESPACE_URL,prefix))+'/after/0/payload.bin'
def checked_for(case_id,user):
 async def checked(db):
  await lock_case_execution(db,case_id)
  case=await get_owned_case(case_id,user,db);assert_case_available(case);return case
 return checked
@app.get('/api/v1/fixture/remote/{case_id}')
async def state(case_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  rows=await db.scalars(select(StorageWriteReservation).where(StorageWriteReservation.case_id==case_id).order_by(StorageWriteReservation.created_at))
  return {'rows':[{**{key:getattr(row,key) for key in ('id','state','size','remaining_bytes','operation_id')},
   'request':json.loads(row.request_json),'receipt':json.loads(row.receipt_json)} for row in rows],
   'usage':await quota.usage(db,user),'name':case.name,'prefix':case.s3_prefix,'owner':identity.observe(os.getpid())}
@app.post('/api/v1/fixture/remote-compete')
async def compete(body:dict,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:await quota.check_storage_quota(db,user,body['bytes'])
 return {'admitted':True}
@app.post('/api/v1/fixture/remote-probe/{case_id}')
async def probe(case_id:int,user:User=Depends(get_current_user)):
 async with operations.hold(checked_for(case_id,user),purpose='recovery') as lease:lease.resolved=True
 return {'reconciled':True}
@app.post('/api/v1/fixture/remote-write/{case_id}')
async def write(case_id:int,body:dict,user:User=Depends(get_current_user)):
 from app.config import get_settings
 mode=body['mode']
 async with operations.hold(checked_for(case_id,user),purpose='staging') as lease:
  prefix=lease.case.s3_prefix
  args={'Bucket':get_settings().s3_bucket,'Key':storage._s3_key(prefix,key_for(prefix)),
   'Body':b'x'*(128*1024),'IfNoneMatch':'*'}
  if mode=='logical':args['Key']=storage._s3_key(prefix,'Ref/budget.txt')
  if mode=='stream_growth':args['Body']=io.BytesIO(args['Body'])
  if mode=='unconditional':args.pop('IfNoneMatch')
  if mode=='quota':(root/'quota-limit').write_text('0')
  if mode=='inside_control':
   async with lease.control():await asyncio.to_thread(storage.s3().put_object,**args)
  else:await asyncio.to_thread(storage.s3().put_object,**args)
  if mode=='rollback':
   async with lease.control() as db:
    case=await db.get(Case,case_id);case.name='SHOULD ROLL BACK';await db.flush()
    raise RuntimeError('Business transaction rolled back after acknowledged object')
  lease.resolved=True
 return {'written':True}
'''

FAULT='''
def mode():return (root/'mode').read_text()
def hold():
 (root/'ready').touch()
 deadline=time.monotonic()+90
 while not (root/'release').exists():
  if time.monotonic()>deadline:raise TimeoutError('Test did not release physical PUT')
  time.sleep(.02)
raw=storage.s3().raw.raw
original_put=raw.put_object
def putting(**args):
 if not args['Key'].endswith('/payload.bin'):return original_put(**args)
 selected=mode()
 if selected in {'normal','unknown','death','complete_unknown'}:hold()
 if selected=='unknown':raise TimeoutError('Unknown request outcome')
 if selected=='stream_growth':
  bounded=args['Body'];position=bounded.source.tell();bounded.source.seek(0,2)
  bounded.source.write(b'growth'*(256*1024));bounded.source.seek(position)
 result=original_put(**args)
 if selected in {'after_write','corrupt'}:hold()
 if selected=='complete_unknown':raise TimeoutError('Lost successful physical PUT reply')
 return result
raw.put_object=putting
'''


def verify(store,modes,url=None):
    from app.services import process_identity_native as identity
    report=Path(tempfile.mkdtemp(prefix='remote-write-runs-',dir=SERVER.parents[1]/'tmp'))
    database=url or 'sqlite+aiosqlite:///'+(report/'test.sqlite').as_posix()
    store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    checks=[];runs=[]
    for mode in modes:
        api=IsolatedServer('remote-write-'+mode,app='write_fixture:app',database_url=url)
        settings={**store.settings(),'VIBEFLOW_DATABASE_URL':database}
        (api.root/'write_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\nfrom app.main import app\n'+INSPECT+OBSERVE+FAULT,encoding='utf8')
        (api.root/'mode').write_text(mode)
        with api:
            api.env['VIBEFLOW_DATABASE_URL']=database
            case=api.create_case('Remote budget '+mode);case_id=case['id']
            baseline=api.call('GET',f'/fixture/remote/{case_id}')
            path=f'/fixture/remote-write/{case_id}';reader_root=None;held=None
            if mode in {'normal','unknown','death','complete_unknown','after_write','corrupt'}:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    request=pool.submit(api.call,'POST',path,{'mode':mode},expect=(200,500),raw=True)
                    wait_file(api.root/'ready',seconds=25)
                    held=api.call('GET',f'/fixture/remote/{case_id}')
                    assert len(held['rows'])==1 and held['rows'][0]['state']=='active'
                    assert held['usage']['storage_bytes_reserved']==held['rows'][0]['size']==128*1024
                    if mode in {'normal','unknown','complete_unknown'}:
                        if mode=='normal':
                            free=4*1024*1024-held['usage']['storage_bytes_used']
                            api.call('POST','/fixture/remote-compete',{'bytes':free},expect=429)
                        (api.root/'release').touch();request.result(timeout=25)
                        after=api.call('GET',f'/fixture/remote/{case_id}')
                        assert after['rows'][0]['state']==('settled' if mode=='normal' else 'active'),after
                    else:
                        if mode=='corrupt':
                            store.raw_client.put_object(Bucket=store.bucket,Key=held['rows'][0]['request']['key'],Body=b'corrupt')
                        stop(api.proc)
                        try:request.result(timeout=15)
                        except OSError:pass
                        else:raise AssertionError('Killed writer returned a response')
                        deadline=time.monotonic()+5
                        while identity.probe(held['owner'])['state']!='exited':
                            assert time.monotonic()<deadline;time.sleep(.02)
                    reader=peer(api);script=reader.root/'finalizing_peer.py'
                    script.write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+script.read_text(encoding='utf8')+OBSERVE,encoding='utf8')
                    with reader:
                        reader.call('POST',f'/fixture/remote-probe/{case_id}')
                        after=reader.call('GET',f'/fixture/remote/{case_id}')
                        settled=mode in {'normal','complete_unknown','after_write'}
                        assert after['rows'][0]['state']==('settled' if settled else 'active'),after
                        assert after['usage']['storage_bytes_reserved']==(0 if settled else 128*1024),after
                        assert after['name']=='Remote budget '+mode
                        if mode in {'complete_unknown','after_write'}:assert after['rows'][0]['receipt']['status']=='verified_immutable'
                        assert not script.with_suffix('.replayed').exists()
                    reader_root=str(reader.root)
            elif mode=='precondition':
                api.call('POST',path,{'mode':mode})
                api.call('POST',path,{'mode':mode},expect=500,raw=True)
                after=api.call('GET',f'/fixture/remote/{case_id}')
                assert [r['receipt']['status'] for r in after['rows']]==[200,412],after
                assert all(r['remaining_bytes']==0 and r['state']=='settled' for r in after['rows'])
                assert after['usage']['storage_bytes_used']-baseline['usage']['storage_bytes_used']==128*1024
            else:
                rejected=mode in {'quota','unconditional','inside_control','rollback'}
                api.call('POST',path,{'mode':mode},expect=(429 if mode=='quota' else 500) if rejected else 200,raw=rejected)
                after=api.call('GET',f'/fixture/remote/{case_id}')
                if mode in {'quota','unconditional','inside_control'}:assert after['rows']==[],after
                else:
                    assert all(r['state']=='settled' and r['remaining_bytes']==0 for r in after['rows']),after
                    allocated=sum(r['size'] for r in after['rows'] if r['receipt']['status']==200)
                    assert after['usage']['storage_bytes_used']-baseline['usage']['storage_bytes_used']==allocated,after
                    if mode=='logical':
                        assert any(r['request']['key'].endswith('/head.json') for r in after['rows'])
                        assert allocated>128*1024
                    if mode=='stream_growth':assert allocated==128*1024
                assert after['name']=='Remote budget '+mode
            (api.root/'capacity-result.json').write_text(json.dumps({'baseline':baseline,'held':held,'after':after},indent=2),encoding='utf8')
            checks.append(mode);runs.append({'mode':mode,'root':str(api.root),'reader':reader_root})
            (report/'checks.json').write_text(json.dumps({'passed':False,'checks':checks,'runs':runs},indent=2),encoding='utf8')
            print('PASS remote write '+mode,flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs}
    (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8');return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+');args=parser.parse_args()
    modes=args.modes or ['normal','logical','quota','unknown','complete_unknown','death','after_write','corrupt','rollback','precondition','stream_growth','unconditional','inside_control']
    from s3testsupport import ObjectStore
    from s3_typed_publication_smoke import RUNTIME
    report=Path(tempfile.mkdtemp(prefix='remote-write-evidence-',dir=SERVER.parents[1]/'tmp'))
    with ObjectStore(executable=RUNTIME) as store:results={'sqlite':await asyncio.to_thread(verify,store,modes)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_remotewrite_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_remotewrite_[0-9a-f]{32}',name)
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
    print('REMOTE_WRITE_BUDGET_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
