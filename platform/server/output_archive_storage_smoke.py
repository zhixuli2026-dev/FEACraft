"""S3 output capacity survives unknown writes and API death without native replay."""
import argparse,asyncio,json,re,tempfile,time,uuid
from pathlib import Path
from testsupport import IsolatedServer,SERVER
from publication_finalizing_smoke import INSPECT,peer
from publication_storage_smoke import terminal
from job_release_smoke import wait_file
from node_storage_budget_smoke import stop

OBSERVE='''
import asyncio,json,os,time
from pathlib import Path
from app.models import OutputArchiveReservation,Case
from app.services import quota,output_archive_storage as archive,process_identity_native as identity
from app.services import attempt_objects as objects
from app import storage
root=Path(__file__).parent
async def quotas(db,user):
 limit=root/'quota-limit'
 return {**quota.DEFAULT_QUOTAS,'storage_mb':int(limit.read_text()) if limit.exists() else 4}
quota.quotas_for=quotas
@app.get('/api/v1/fixture/archive/{job_id}')
async def archive_state(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db,write=False)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  row=await db.get(OutputArchiveReservation,a.id)
  output=json.loads(a.output_manifest_json);manifest=json.loads(a.input_manifest_json)['workspace']
  remote_bytes=sum(item['size'] for item in storage._s3_list(case.s3_prefix,manifest['run_path']))
  try:remote_bytes+=len(objects.read(case.s3_prefix,'.vibeflow/attempts/'+a.id+'/outputs.json'))
  except FileNotFoundError:pass
  return {'reservation':None if row is None else {key:getattr(row,key) for key in ('state','remaining_bytes','size','manifest_sha256','writer_json')},
   'usage':await quota.usage(db,user),'output':output,'prefix':case.s3_prefix,'owner':identity.observe(os.getpid()),'remote_bytes':remote_bytes}
@app.post('/api/v1/fixture/archive-duplicate/{job_id}')
async def duplicate(job_id:int,user:User=Depends(get_current_user)):
 from app.services.job_execution import Claim
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  owner=Claim(job.id,a.id,a.root_job_id,a.number,a.claim_token)
 async with archive.capacity(owner,case.id,case.s3_prefix,a.input_manifest_json,a.output_manifest_json):
  raise AssertionError('Second archiver spent the first writer capacity')
@app.post('/api/v1/fixture/archive-corrupt/{job_id}')
async def corrupt(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  manifest=json.loads(a.input_manifest_json)['workspace'];output=json.loads(a.output_manifest_json)
  path=manifest['run_path']+'/'+next(iter(output['files']))
 await asyncio.to_thread(storage._s3_put,case.s3_prefix,path,b'corrupt remote output')
 return {'corrupted':True}
'''

FAULT='''
def mode():return (root/'mode').read_text()
def hold():
 (root/'ready').touch()
 deadline=time.monotonic()+100
 while not (root/'release').exists():
  if time.monotonic()>deadline:raise TimeoutError('Archive hold expired')
  time.sleep(.02)
original_admit=archive.admit
async def admitting(*args):
 if mode()=='quota':(root/'quota-limit').write_text('0')
 return await original_admit(*args)
archive.admit=admitting
original=objects.persist_outputs
def outputs(prefix,manifest,run,records,directories):
 if mode() not in {'after_copy','corrupt'}:hold()
 if mode()=='partial_error':
  name=next(iter(records));record=records[name]
  objects.put_new(prefix,manifest['run_path']+'/'+name,objects.checked((run/name).read_bytes(),record))
  raise TimeoutError('Unknown outcome after a partial output upload')
 result=original(prefix,manifest,run,records,directories)
 if mode() in {'after_copy','corrupt'}:hold()
 if mode() in {'complete_error','complete_error_live'}:raise TimeoutError('Lost complete upload response')
 return result
objects.persist_outputs=outputs
'''


def verify(store,modes,url=None):
    from app.services import process_identity_native as identity
    report=Path(tempfile.mkdtemp(prefix='archive-storage-runs-',dir=SERVER.parents[1]/'tmp'))
    database=url or 'sqlite+aiosqlite:///'+(report/'test.sqlite').as_posix()
    checks=[];runs=[]
    for mode in modes:
        api=IsolatedServer('archive-storage-'+mode,app='archive_fixture:app',database_url=url)
        settings={**store.settings(),'VIBEFLOW_DATABASE_URL':database,'VIBEFLOW_NODE_STORAGE_MIN_FREE_MB':'0'}
        code='import os\nos.environ.update('+repr(settings)+')\nfrom app.main import app\n'+INSPECT+OBSERVE+FAULT
        (api.root/'archive_fixture.py').write_text(code,encoding='utf8')
        (api.root/'mode').write_text('after_copy' if mode=='fresh_after_copy' else mode)
        with api:
            api.env['VIBEFLOW_DATABASE_URL']=database
            api.create_case('Archive '+mode);api.put('E-report/report.md','# Archive capacity\n\nReal outputs.\n')
            job_id=api.call('POST',api.prefix+'/report:build',{})['job_id']
            reader_root=None
            if mode=='quota':
                final=terminal(api,job_id);after=api.call('GET',f'/fixture/archive/{job_id}')
                assert final['status']=='failed' and final['attempt']['reason_code']=='resource_limit',final
                assert after['reservation'] is None and after['remote_bytes']==0 and not (api.root/'ready').exists(),after
                before=None
            else:
                wait_file(api.root/'ready',seconds=80)
                before=api.call('GET',f'/fixture/archive/{job_id}');claim=before['reservation']
                assert claim['state']=='active' and claim['remaining_bytes']==claim['size']>0
                assert before['usage']['storage_bytes_reserved']==claim['size']
                if mode=='normal':
                    reader=peer(api);script=reader.root/'finalizing_peer.py'
                    script.write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+script.read_text(encoding='utf8')+OBSERVE,encoding='utf8')
                    with reader:
                        reader.call('POST',f'/fixture/archive-duplicate/{job_id}',expect=409)
                        free=4*1024*1024-before['usage']['storage_bytes_used']
                        reader.call('POST',api.prefix+'/uploads',{'path':'Ref/competing.bin','size':free},expect=429)
                        (api.root/'release').touch();final=terminal(api,job_id)
                        after=reader.call('GET',f'/fixture/archive/{job_id}')
                        assert final['status']=='succeeded' and after['reservation']['state']=='materialized',after
                        assert after['remote_bytes']==claim['size'] and after['output']['object_store']['state']=='complete'
                    reader_root=str(reader.root)
                else:
                    if mode in {'partial_error','complete_error','complete_error_live'}:
                        (api.root/'release').touch();failed=terminal(api,job_id)
                        assert failed['status']=='failed'
                        pending=api.call('GET',f'/fixture/archive/{job_id}')
                        assert pending['reservation']['state']=='active' and pending['usage']['storage_bytes_reserved']==claim['size']
                    if mode in {'after_copy','fresh_after_copy','corrupt'}:assert before['remote_bytes']==claim['size']
                    if mode=='corrupt':api.call('POST',f'/fixture/archive-corrupt/{job_id}')
                    if mode=='complete_error_live':
                        assert identity.probe(before['owner'])['state']=='match'
                    else:
                        stop(api.proc);deadline=time.monotonic()+5
                        while identity.probe(before['owner'])!={'state':'exited','reason':'process_exited'}:
                            assert time.monotonic()<deadline
                            time.sleep(.02)
                    reader=peer(api);script=reader.root/'finalizing_peer.py'
                    peer_code=script.read_text(encoding='utf8')
                    if mode=='fresh_after_copy':
                        assert repr(api.env['VIBEFLOW_STORAGE_FS_ROOT']) in peer_code
                        peer_code=peer_code.replace(repr(api.env['VIBEFLOW_STORAGE_FS_ROOT']),repr(str(reader.root/'fresh-cache')))
                    script.write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+peer_code+OBSERVE,encoding='utf8')
                    with reader:
                        final=terminal(reader,job_id);after=reader.call('GET',f'/fixture/archive/{job_id}')
                        verified=mode in {'after_copy','fresh_after_copy','complete_error','complete_error_live'}
                        assert after['reservation']['state']==('materialized' if verified else 'active'),after
                        assert after['usage']['storage_bytes_reserved']==(0 if verified else claim['size']),after
                        assert after['output']['object_store']['state']=='pending'
                        assert final['status']==('failed' if mode in {'partial_error','complete_error','complete_error_live'} else 'interrupted'),final
                        assert not script.with_suffix('.replayed').exists()
                    reader_root=str(reader.root)
            (api.root/'archive-result.json').write_text(json.dumps({'before':before,'after':after,'job':final},indent=2),encoding='utf8')
            checks.append(mode);runs.append({'mode':mode,'root':str(api.root),'reader':reader_root})
            (report/'checks.json').write_text(json.dumps({'passed':False,'checks':checks,'runs':runs},indent=2),encoding='utf8')
            print('PASS output archive '+mode,flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs}
    (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+')
    args=parser.parse_args();modes=args.modes or ['normal','quota','partial_error','death','after_copy','complete_error','corrupt','complete_error_live','fresh_after_copy']
    from s3testsupport import ObjectStore
    from s3_typed_publication_smoke import RUNTIME
    report=Path(tempfile.mkdtemp(prefix='archive-storage-evidence-',dir=SERVER.parents[1]/'tmp'))
    with ObjectStore(executable=RUNTIME) as store:results={'sqlite':await asyncio.to_thread(verify,store,modes)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_archivestorage_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_archivestorage_[0-9a-f]{32}',name)
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
    print('OUTPUT_ARCHIVE_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
