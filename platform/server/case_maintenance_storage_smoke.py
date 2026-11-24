"""Real per-case maintenance selection, durable credit and deferred work."""
import argparse,asyncio,json,re,tempfile,time,uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from testsupport import IsolatedServer,SERVER
from publication_finalizing_smoke import INSPECT
from s3_file_transactions_smoke import FIXTURE as WRITER
from job_release_smoke import wait_file
from node_storage_budget_smoke import stop

INSPECTION='''
import json,time
from pathlib import Path
from app.models import OwnerStorageReservation,Case
from app.services import quota,owner_storage_budget as capacity,storage_maintenance as maintenance
root=Path(__file__).parent
async def quotas(db,user):
 p=root/'quota-limit'
 return {**quota.DEFAULT_QUOTAS,'storage_mb':int(p.read_text()) if p.exists() else 4}
quota.quotas_for=quotas
@app.get('/api/v1/fixture/maintenance/{case_id}')
async def inspect(case_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db,write=False)
  rows=list(await db.scalars(select(OwnerStorageReservation).where(OwnerStorageReservation.user_id==user.id).order_by(OwnerStorageReservation.created_at)))
  return {'prefix':case.s3_prefix,'rows':[{'id':r.id,'state':r.state,'size':r.size,'remaining_bytes':r.remaining_bytes,'receipt':json.loads(r.receipt_json)} for r in rows],
   'usage':await quota.usage(db,user)}
'''

READER='''
import asyncio
original_gate=maintenance.has_case_work
async def gate(db,case):
 result=await original_gate(db,case)
 if (root/'gate-hold').exists() and not result:
  (root/'gate-ready').touch()
  while not (root/'gate-release').exists():await asyncio.sleep(.02)
 return result
maintenance.has_case_work=gate
raw=capacity.raw_client();original_put=raw.put_object
def put(**args):
 if (root/'hold-recovery').exists() and args['Key'].endswith('/.vibeflow/live-store/head.json'):
  (root/'ready').touch();deadline=time.monotonic()+90
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('Maintenance physical PUT hold')
   time.sleep(.02)
 return original_put(**args)
raw.put_object=put
'''


def boot(store,database_url,name,token=None,writer=False):
    api=IsolatedServer('case-maintenance-'+name,app='case_maintenance:app',
                       database_url=database_url if database_url.startswith('postgresql+') else None)
    settings={**store.settings(),'VIBEFLOW_DATABASE_URL':database_url}
    if token is not None:
        api.token=token[0];settings['VIBEFLOW_JWT_SECRET']=token[1]
    (api.root/'case_maintenance.py').write_text('import os\nos.environ.update('+repr(settings)+')\nfrom app.main import app\n'+
        (WRITER if writer else '')+INSPECT+INSPECTION+(READER if not writer else ''),encoding='utf8')
    return api


def read(store,prefix,path):
    from botocore.exceptions import ClientError
    try:response=store.client.get_object(Bucket=store.bucket,Key=prefix+'/'+path)
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode']==404:return None
        raise
    try:return response['Body'].read()
    finally:response['Body'].close()


def verify(store,modes,url=None):
    from app.services import process_identity_native as identity
    report=Path(tempfile.mkdtemp(prefix='case-maintenance-runs-',dir=SERVER.parents[1]/'tmp'))
    database=url or 'sqlite+aiosqlite:///'+(report/'test.sqlite').as_posix()
    store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    checks=[];runs=[]
    for mode in modes:
        api=boot(store,database,'writer-'+mode,writer=True)
        with api:
            case=api.create_case('Maintenance '+mode);case_id=case['id']
            api.put('Ref/marker.txt','old');api.call('POST','/case-provisions:recover')
            baseline=api.call('GET',f'/fixture/maintenance/{case_id}');prefix=baseline['prefix']
            token=(api.token,api.env['VIBEFLOW_JWT_SECRET'])
            reader=boot(store,database,'reader-'+mode,token)
            with reader,ThreadPoolExecutor(max_workers=2) as pool:
                if mode=='orphan_cache':
                    cache=Path(reader.env['VIBEFLOW_STORAGE_FS_ROOT'])/prefix/'.vibeflow/attempts'/str(uuid.uuid4())/'unowned.bin'
                    cache.parent.mkdir(parents=True,exist_ok=True);cache.write_bytes(b'keep without rollback proof')
                    result=reader.call('POST','/case-provisions:maintenance')
                    assert cache.read_bytes()==b'keep without rollback proof'
                    assert result['attempts']==[]
                else:
                    if mode=='late_arrival':
                        (reader.root/'gate-hold').touch()
                        scanning=pool.submit(reader.call,'POST','/case-provisions:maintenance')
                        wait_file(reader.root/'gate-ready',seconds=25)
                    (api.root/'mode').write_text('after_commit' if mode=='committed' else 'after_one')
                    writing=pool.submit(api.call,'PUT',api.prefix+'/file',{'path':'Ref/marker.txt','content':'new'})
                    wait_file(api.root/'ready',seconds=30)
                    if mode=='late_arrival':
                        (reader.root/'gate-release').touch();skipped=scanning.result(timeout=20)
                        assert skipped['uploads']==[] and skipped['attempts']==[]
                        assert not writing.done(),'Skipped scan interfered with an active writer'
                        assert read(store,prefix,'.vibeflow/file-transactions/pending.json') is not None
                        (reader.root/'gate-hold').unlink()
                    stop(api.proc)
                    try:writing.result(timeout=15)
                    except OSError:pass
                    else:raise AssertionError('Killed writer returned success')
                    pending=read(store,prefix,'.vibeflow/file-transactions/pending.json');assert pending is not None
                    if mode=='quota':
                        (reader.root/'quota-limit').write_text('0')
                        denied=reader.call('POST','/case-provisions:maintenance')
                        assert any(r['status']=='recovery_required' for r in denied['uploads']),denied
                        assert read(store,prefix,'.vibeflow/file-transactions/pending.json')==pending
                        assert read(store,prefix,'Ref/marker.txt')==b'new'
                        (reader.root/'quota-limit').write_text('4')
                    if mode=='unknown_recovery':
                        (reader.root/'hold-recovery').touch()
                        recovering=pool.submit(reader.call,'POST','/case-provisions:maintenance')
                        wait_file(reader.root/'ready',seconds=30)
                        held=reader.call('GET',f'/fixture/maintenance/{case_id}')
                        active=[row for row in held['rows'] if row['state']=='active']
                        assert len(active)==1 and active[0]['remaining_bytes']==active[0]['size']>0,held
                        stop(reader.proc)
                        try:recovering.result(timeout=15)
                        except OSError:pass
                        else:raise AssertionError('Killed maintenance returned success')
                        # A new API must reconcile the pool before its own recovery write.
                        with boot(store,database,'second-reader',token) as second:
                            result=second.call('POST','/case-provisions:maintenance')
                            after=second.call('GET',f'/fixture/maintenance/{case_id}')
                            retained=next(row for row in after['rows'] if row['id']==active[0]['id'])
                            assert 0<retained['remaining_bytes']<retained['size'],retained
                            assert retained['receipt']['closed'] and retained['receipt']['pending']['key'].endswith('/head.json')
                            result={'maintenance':result,'retained':retained,'second':str(second.root)}
                    else:result=reader.call('POST','/case-provisions:maintenance')
                    assert read(store,prefix,'Ref/marker.txt')==(b'new' if mode=='committed' else b'old')
                    assert read(store,prefix,'.vibeflow/file-transactions/pending.json') is None
                if mode!='unknown_recovery':
                    state=reader.call('GET',f'/fixture/maintenance/{case_id}')
                    assert not any(row['state']=='active' for row in state['rows']),state
                    result={'maintenance':result,'state':state}
                (reader.root/'maintenance-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
                checks.append(mode);runs.append({'mode':mode,'writer':str(api.root),'reader':str(reader.root)})
                print('PASS case maintenance '+mode,flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs}
    (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+');args=parser.parse_args()
    modes=args.modes or ['rollback','committed','quota','unknown_recovery','late_arrival','orphan_cache']
    from s3testsupport import ObjectStore
    from s3_typed_publication_smoke import RUNTIME
    report=Path(tempfile.mkdtemp(prefix='case-maintenance-evidence-',dir=SERVER.parents[1]/'tmp'))
    with ObjectStore(executable=RUNTIME) as store:results={'sqlite':await asyncio.to_thread(verify,store,modes)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_casemaintenance_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_casemaintenance_[0-9a-f]{32}',name)
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
    print('CASE_MAINTENANCE_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
