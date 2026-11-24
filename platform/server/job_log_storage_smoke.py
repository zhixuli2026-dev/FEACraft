"""Real S3 logs: quota, conditional versions, uncertain writes and recovery."""
import argparse,asyncio,json,re,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from testsupport import IsolatedServer,SERVER
from publication_finalizing_smoke import INSPECT,peer
from publication_storage_smoke import terminal
from job_release_smoke import wait_file
from node_storage_budget_smoke import stop

OBSERVE='''
import asyncio,json,os,time
from pathlib import Path
from app.models import JobLogReservation
from app.services import quota,job_log_storage as logs,process_identity_native as identity
from app.services import attempt_objects as objects
from app import storage
root=Path(__file__).parent
async def quotas(db,user):
 limit=root/'quota-limit'
 return {**quota.DEFAULT_QUOTAS,'storage_mb':int(limit.read_text()) if limit.exists() else 4}
quota.quotas_for=quotas
@app.get('/api/v1/fixture/log/{job_id}')
async def log_state(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db,write=False)
  row=await db.get(JobLogReservation,job_id)
  key=logs.log_key(job_id)
  versions=storage.s3().raw.list_object_versions(**{'Bucket':objects.arguments(case.s3_prefix,key)['Bucket'],
   'Prefix':objects.arguments(case.s3_prefix,key)['Key']}).get('Versions',[])
  local=storage._fs_path(case.s3_prefix,f'.vibeflow/jobs/job-{job_id}.log')
  from app.services import jobrunner
  proc=jobrunner.get_proc(job_id)
  if proc is not None:local=proc.log_path
  return {'reservation':None if row is None else {key:getattr(row,key) for key in ('state','remaining_bytes','size','sha256','writer_json')},
   'usage':await quota.usage(db,user),'prefix':case.s3_prefix,'owner':identity.observe(os.getpid()),
   'versions':len(versions),'remote_bytes':sum(v['Size'] for v in versions),
   'local_log':local.read_text(encoding='utf8',errors='replace') if local.is_file() else None}
@app.post('/api/v1/fixture/log-duplicate/{job_id}')
async def duplicate(job_id:int,user:User=Depends(get_current_user)):
 from app.services.job_execution import Claim
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  owner=Claim(job.id,a.id,a.root_job_id,a.number,a.claim_token)
 await logs.upload(owner,case.s3_prefix,storage._fs_path(case.s3_prefix,f'.vibeflow/jobs/job-{job_id}.log'))
 return {'uploaded':True}
@app.post('/api/v1/fixture/log-corrupt/{job_id}')
async def corrupt(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
 await asyncio.to_thread(storage._s3_put,case.s3_prefix,logs.log_key(job_id),b'corrupt remote log')
 return {'corrupted':True}
@app.post('/api/v1/fixture/log-idempotent/{job_id}')
async def idempotent(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  row=await db.get(JobLogReservation,job_id)
  ticket=logs.Ticket(**{key:getattr(row,key) for key in ('job_id','attempt_id','case_id','user_id','binding_json','sha256','size','token')})
 data=objects.read(case.s3_prefix,logs.log_key(job_id),limit=ticket.size)
 await asyncio.to_thread(logs.put_new,case.s3_prefix,ticket,data)
 return {'verified':True}
@app.post('/api/v1/fixture/log-legacy/{case_id}')
async def legacy_job(case_id:int,user:User=Depends(get_current_user)):
 import sys
 from app.services import jobrunner
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  job=Job(case_id=case.id,type='shell',command='legacy log fixture')
  db.add(job);await db.flush();job.log_s3_key=f'.vibeflow/jobs/job-{job.id}.log';await db.commit()
 payload="from pathlib import Path; print('NO_LOG_COPY',not Path('.vibeflow/job-logs').exists()); p=Path('.vibeflow/job-logs'); p.mkdir(parents=True); (p/'forged.log').write_text('forged'); Path('legacy-output.txt').write_text('legacy output')"
 proc=await jobrunner.start_job(job.id,job.command,case.s3_prefix,argv=[sys.executable,'-c',payload])
 return {'job_id':job.id,'stage':proc.staging_prefix,'isolated':proc.isolated_workspace}
'''

FAULT='''
def mode():return (root/'mode').read_text()
def hold():
 (root/'ready').touch()
 deadline=time.monotonic()+100
 while not (root/'release').exists():
  if time.monotonic()>deadline:raise TimeoutError('Log hold expired')
  time.sleep(.02)
original_admit=logs.admit
async def admitting(owner,ticket,prefix):
 if mode() in {'quota','legacy_quota'}:(root/'quota-limit').write_text('0')
 if mode()=='existing_versions':
  storage._s3_put(prefix,logs.log_key(ticket.job_id),b'previous one')
  storage._s3_put(prefix,logs.log_key(ticket.job_id),b'previous two')
 if mode()=='legacy_protected':
  storage._s3_put(prefix,f'.vibeflow/jobs/job-{ticket.job_id}.log',b'historical log first')
  storage._s3_put(prefix,f'.vibeflow/jobs/job-{ticket.job_id}.log',b'historical log second')
 if mode()=='wrong_path':
  async with SessionLocal() as db:
   job=await db.get(Job,ticket.job_id);job.log_s3_key='Ref/unauthorized.log';await db.commit()
 return await original_admit(owner,ticket,prefix)
logs.admit=admitting
original=logs.put_new
def uploading(prefix,ticket,data):
 if mode() not in {'after_copy','corrupt'}:hold()
 if mode()=='unknown':raise TimeoutError('Unknown response before observed upload')
 result=original(prefix,ticket,data)
 if mode() in {'after_copy','corrupt'}:hold()
 if mode()=='complete_error_live':raise TimeoutError('Lost complete log upload response')
 return result
logs.put_new=uploading
from app.services import jobrunner
original_upload=jobrunner._upload_remote_log
async def runner_upload(owner,prefix,path):
 if mode()=='empty':path.write_bytes(b'')
 await original_upload(owner,prefix,path)
jobrunner._upload_remote_log=runner_upload
if mode()=='startup':
 async def fail_start(self):raise OSError('Injected process launch failure')
 jobrunner.JobProcess.start=fail_start
'''


def verify(store,modes,url=None):
    from app.services import process_identity_native as identity
    report=Path(tempfile.mkdtemp(prefix='job-log-runs-',dir=SERVER.parents[1]/'tmp'))
    database=url or 'sqlite+aiosqlite:///'+(report/'test.sqlite').as_posix()
    store.raw_client.put_bucket_versioning(Bucket=store.settings()['VIBEFLOW_S3_BUCKET'],VersioningConfiguration={'Status':'Enabled'})
    checks=[];runs=[]
    for mode in modes:
        api=IsolatedServer('job-log-'+mode,app='log_fixture:app',database_url=url)
        settings={**store.settings(),'VIBEFLOW_DATABASE_URL':database,'VIBEFLOW_NODE_STORAGE_MIN_FREE_MB':'0'}
        code='import os\nos.environ.update('+repr(settings)+')\nfrom app.main import app\n'+INSPECT+OBSERVE+FAULT
        (api.root/'log_fixture.py').write_text(code,encoding='utf8')
        (api.root/'mode').write_text(mode)
        with api:
            api.env['VIBEFLOW_DATABASE_URL']=database
            case=api.create_case('Log '+mode);api.put('E-report/report.md','# Log admission\n\nReal logs.\n')
            startup_pool=startup_request=None
            if mode=='startup':
                startup_pool=ThreadPoolExecutor(max_workers=1)
                startup_request=startup_pool.submit(api.call,'POST',api.prefix+'/report:build',{},expect=(200,500),raw=True)
                wait_file(api.root/'ready',seconds=25)
                job_id=api.call('GET',api.prefix+'/jobs')[0]['id']
            elif mode=='legacy_quota':
                created=api.call('POST',f"/fixture/log-legacy/{case['id']}")
                assert created['stage'] and not created['isolated'],created
                job_id=created['job_id']
            else:job_id=api.call('POST',api.prefix+'/report:build',{})['job_id']
            reader_root=None
            if mode in {'quota','legacy_quota','existing_versions','wrong_path'}:
                final=terminal(api,job_id);after=api.call('GET',f'/fixture/log/{job_id}')
                assert final['status']=='failed',final
                if mode in {'quota','legacy_quota'}:assert final['attempt']['reason_code']=='resource_limit',final
                assert after['reservation'] is None and after['versions']==(2 if mode=='existing_versions' else 0),after
                assert 'local evidence retained' in after['local_log'] and not (api.root/'ready').exists()
                if mode=='existing_versions':assert after['usage']['storage_bytes_used']>=after['remote_bytes']>0
                if mode=='legacy_quota':
                    assert (api.root/'files'/created['stage']).is_dir()
                    api.call('GET',api.prefix+'/raw?path=legacy-output.txt',expect=404)
                before=None
            else:
                wait_file(api.root/'ready',seconds=80)
                before=api.call('GET',f'/fixture/log/{job_id}');claim=before['reservation']
                assert claim['state']=='active' and claim['remaining_bytes']==claim['size']>=0
                assert before['usage']['storage_bytes_reserved']==claim['size'],before
                if mode in {'normal','empty','startup','legacy_protected'}:
                    reader=peer(api);script=reader.root/'finalizing_peer.py'
                    script.write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+script.read_text(encoding='utf8')+OBSERVE,encoding='utf8')
                    with reader:
                        reader.call('POST',f'/fixture/log-duplicate/{job_id}',expect=409)
                        if mode=='normal':
                            free=4*1024*1024-before['usage']['storage_bytes_used']
                            reader.call('POST',api.prefix+'/uploads',{'path':'Ref/competing.bin','size':free},expect=429)
                        (api.root/'release').touch()
                        if startup_request is not None:
                            startup_request.result(timeout=25);startup_pool.shutdown()
                        final=terminal(api,job_id)
                        after=reader.call('GET',f'/fixture/log/{job_id}')
                        assert final['status']==('failed' if mode=='startup' else 'succeeded'),final
                        assert after['reservation']['state']=='materialized' and after['remote_bytes']==claim['size'] and after['versions']==1,after
                        reader.call('POST',f'/fixture/log-idempotent/{job_id}')
                        assert reader.call('GET',f'/fixture/log/{job_id}')['versions']==1
                        log=reader.call('GET',f'/jobs/{job_id}/log')
                        assert log['offset']==claim['size'],log
                        if mode=='empty':assert claim['size']==0
                        if mode=='startup':assert 'Injected process launch failure' in log['chunk']
                        if mode=='legacy_protected':
                            key=f'.vibeflow/job-logs/job-{job_id}.log'
                            old=f'.vibeflow/jobs/job-{job_id}.log'
                            assert reader.call('GET',api.prefix+'/raw?path='+old,raw=True)==b'historical log second'
                            reader.call('PUT',api.prefix+'/file',{'path':key,'content':'overwrite'},expect=409)
                            reader.call('POST',api.prefix+'/uploads',{'path':key,'size':10},expect=409)
                            copied=reader.call('POST',api.prefix+':copy',{'name':'Copy excludes execution logs'})
                            reader.call('GET',f"/cases/{copied['id']}/raw?path="+key,expect=404)
                            created=api.call('POST',f"/fixture/log-legacy/{case['id']}")
                            assert created['stage'] and not created['isolated'],created
                            legacy=terminal(api,created['job_id'])
                            assert legacy['status']=='succeeded' and 'NO_LOG_COPY True' in legacy['log_backlog'],legacy
                            legacy_receipt=reader.call('GET',f"/fixture/log/{created['job_id']}")
                            assert legacy_receipt['reservation']['state']=='materialized' and legacy_receipt['versions']==1,legacy_receipt
                            assert reader.call('GET',api.prefix+'/raw?path=legacy-output.txt',raw=True)==b'legacy output'
                            reader.call('GET',api.prefix+'/raw?path=.vibeflow/job-logs/forged.log',expect=404)
                            assert reader.call('GET',f'/jobs/{job_id}/log')==log
                    reader_root=str(reader.root)
                else:
                    if mode in {'unknown','complete_error_live'}:
                        (api.root/'release').touch();failed=terminal(api,job_id)
                        assert failed['status']=='failed'
                        pending=api.call('GET',f'/fixture/log/{job_id}')
                        assert pending['reservation']['state']=='active' and pending['usage']['storage_bytes_reserved']==claim['size']
                        assert 'local evidence retained' in pending['local_log']
                    if mode in {'after_copy','corrupt'}:assert before['remote_bytes']==claim['size']
                    if mode=='corrupt':api.call('POST',f'/fixture/log-corrupt/{job_id}')
                    if mode=='complete_error_live':assert identity.probe(before['owner'])['state']=='match'
                    else:
                        stop(api.proc);deadline=time.monotonic()+5
                        while identity.probe(before['owner'])!={'state':'exited','reason':'process_exited'}:
                            assert time.monotonic()<deadline
                            time.sleep(.02)
                    reader=peer(api);script=reader.root/'finalizing_peer.py'
                    peer_code=script.read_text(encoding='utf8')
                    assert repr(api.env['VIBEFLOW_STORAGE_FS_ROOT']) in peer_code
                    peer_code=peer_code.replace(repr(api.env['VIBEFLOW_STORAGE_FS_ROOT']),repr(str(reader.root/'fresh-cache')))
                    script.write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+peer_code+OBSERVE,encoding='utf8')
                    with reader:
                        final=terminal(reader,job_id);after=reader.call('GET',f'/fixture/log/{job_id}')
                        verified=mode in {'after_copy','complete_error_live'}
                        assert after['reservation']['state']==('materialized' if verified else 'active'),after
                        assert after['usage']['storage_bytes_reserved']==(0 if verified else claim['size']),after
                        assert final['status']==('failed' if mode in {'unknown','complete_error_live'} else 'interrupted'),final
                        assert after['local_log'] is None and not script.with_suffix('.replayed').exists()
                    reader_root=str(reader.root)
            (api.root/'log-result.json').write_text(json.dumps({'before':before,'after':after,'job':final},indent=2),encoding='utf8')
            checks.append(mode);runs.append({'mode':mode,'root':str(api.root),'reader':reader_root})
            (report/'checks.json').write_text(json.dumps({'passed':False,'checks':checks,'runs':runs},indent=2),encoding='utf8')
            print('PASS remote log '+mode,flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs}
    (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--postgres-only',action='store_true');parser.add_argument('--modes',nargs='+')
    args=parser.parse_args();modes=args.modes or ['normal','empty','startup','quota','existing_versions','wrong_path','unknown','death','after_copy','corrupt','complete_error_live','legacy_protected','legacy_quota']
    from s3testsupport import ObjectStore
    from s3_typed_publication_smoke import RUNTIME
    report=Path(tempfile.mkdtemp(prefix='job-log-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={}
    if not args.postgres_only:
        with ObjectStore(executable=RUNTIME) as store:results['sqlite']=await asyncio.to_thread(verify,store,modes)
    if args.postgres or args.postgres_only:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_joblog_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_joblog_[0-9a-f]{32}',name)
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
    print('JOB_LOG_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
