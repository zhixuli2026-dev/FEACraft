"""Real native file growth must fail before a durable successful publication."""
import argparse,asyncio,json,re,shlex,subprocess,sys,tempfile,time,uuid
from pathlib import Path
from testsupport import IsolatedServer,SERVER

FIXTURE='''
import os
os.environ.update(VIBEFLOW_EXECUTION_WORKSPACE_LIMIT_MB='1',VIBEFLOW_EXECUTION_STORAGE_POLL_SECONDS='.05',VIBEFLOW_NODE_STORAGE_MIN_FREE_MB='0')
import asyncio,json,sys
from pathlib import Path
from app.services import jobrunner,job_execution,job_delivery,native_execution
from app.config import get_settings
root=Path(__file__).parent
async def paused(stop):await stop.wait()
job_delivery.recover_forever=paused
create=job_execution._new_attempt
async def creating(*args,**kwargs):
 get_settings().execution_workspace_limit_mb=16 if kwargs.get('previous') is not None else 1
 attempt=await create(*args,**kwargs)
 if (root/'mode').read_text()=='frozen':get_settings().execution_workspace_limit_mb=16
 return attempt
job_execution._new_attempt=creating
start=jobrunner.start_job
async def starting(*args,**kwargs):
 if (root/'mode').read_text()=='hook':
  async def hook(status,code):
   jp=jobrunner._registry[args[0]]
   await asyncio.to_thread((jp.cwd/'late.bin').write_bytes,b'h'*(2*1024*1024))
   await asyncio.sleep(.2)
  kwargs['on_finish']=hook
 return await start(*args,**kwargs)
jobrunner.start_job=starting
execute=native_execution.NativeExecutor._execute
async def executing(self,entry,purpose,parameters,argv,timeout,limit,output):
 if not hasattr(self.owner,'job_id'):return await execute(self,entry,purpose,parameters,argv,timeout,limit,output)
 mode=(root/'mode').read_text()
 if self.kind=='report' and mode==entry.caller+'_disk':
  argv=[str(Path(getattr(sys,'_base_executable',sys.executable)).resolve()),'-I','-S',str(root/'native_growth.py'),entry.caller]
 return await execute(self,entry,purpose,parameters,argv,timeout,limit,output)
native_execution.NativeExecutor._execute=executing
from app.services.execution_storage import Watch
snapshot=Watch._snapshot
def observed(self,deadline):
 if (root/'mode').read_text()=='unverified' and (self.root/'run/growth.bin').exists():raise ValueError('Injected unverifiable active directory')
 return snapshot(self,deadline)
Watch._snapshot=observed
from app.main import app
from fastapi import Depends
from sqlalchemy import select
from app.db import SessionLocal
from app.models import Job,JobAttempt,DurableEvent
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
@app.get('/api/v1/fixture/storage/{job_id}')
async def storage_usage(job_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
   DurableEvent.type.in_(['attempt.storage_usage','attempt.resource_usage']))))
  return {'policy':json.loads(attempt.input_manifest_json)['storage_policy'],
   'events':[{'type':e.type,'payload':json.loads(e.payload_json)} for e in events]}
@app.post('/api/v1/fixture/retry/{job_id}')
async def retry_policy(job_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  previous=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  _,attempt=await job_execution.allocate_retry(db,previous.id)
  value=json.loads(attempt.input_manifest_json)['storage_policy']
  await db.commit()
  return value
'''


def verify(modes,url=None):
    api=IsolatedServer('execution-storage',app='storage_fixture:app',database_url=url)
    (api.root/'storage_fixture.py').write_text(FIXTURE,encoding='utf8')
    (api.root/'native_growth.py').write_text("from pathlib import Path\nimport sys,time\nPath('native-large.bin').write_bytes(b'n'*(2*1024*1024))\nPath(__file__).with_name(sys.argv[1]+'-started').touch()\ntime.sleep(20)\nPath(__file__).with_name(sys.argv[1]+'-finished').touch()\n",encoding='utf8')
    observations=[];checks=[]
    with api:
        for mode in modes:
            (api.root/'mode').write_text(mode);api.create_case('Execution disk '+mode)
            if mode.endswith('_disk'):
                api.put('E-report/report.md','# Real disk budget report\n')
                created=api.call('POST',api.prefix+'/report:build',{});identifier=created['job_id']
            else:
                script=api.root/'growth.py'
                size=32768 if mode in {'normal','hook','unverified'} else 2*1024*1024
                script.write_text("from pathlib import Path\nimport time\nPath('growth.bin').write_bytes(b'x'*"+str(size)+")\n"+
                    ("time.sleep(20)\nPath("+repr(str(api.root/'unexpected-finished'))+").touch()\n" if mode in {'running','unverified'} else ''),encoding='utf8')
                body={'command':subprocess.list2cmdline([sys.executable,str(script)]),'timeout_seconds':30}
                if mode=='wsl':
                    body.update(runtime='wsl',command='python3 -c '+shlex.quote("from pathlib import Path; import time; Path('growth.bin').write_bytes(b'w'*(2*1024*1024)); time.sleep(20); Path('unexpected-finished').touch()"))
                created=api.call('POST',api.prefix+'/jobs',body)
                identifier=created['id']
            deadline=time.monotonic()+90
            while True:
                final=api.call('GET',f'/jobs/{identifier}')
                if final['status'] not in {'queued','running'} and final['attempt']['phase']==final['status'] and not final['completion_pending']:break
                assert time.monotonic()<deadline,('Job did not settle',mode,final)
                time.sleep(.05)
            evidence=api.call('GET',f'/fixture/storage/{identifier}')
            observations.append({'mode':mode,'job':final,**evidence})
            (api.root/'storage-observations.json').write_text(json.dumps(observations,indent=2),encoding='utf8')
            usage=[e['payload'] for e in evidence['events'] if e['type']=='attempt.storage_usage']
            output=[e['payload'] for e in evidence['events'] if e['type']=='attempt.resource_usage']
            assert len(usage)==len(output)==1,evidence
            assert evidence['policy']['max_workspace_bytes']==1024*1024
            assert usage[0]['samples']>0
            assert final['attempt']['process_stop']['verified']
            assert api.call('GET',f'/jobs/{identifier}/process')['state']=='exited'
            if mode=='normal':
                assert final['status']=='succeeded' and usage[0]['peak_bytes']<1024*1024
            else:
                assert final['status']=='failed' and final['attempt']['reason_code']=='resource_limit',final
                if mode=='unverified':
                    assert usage[0]['state']=='unverified' and usage[0]['current_bytes'] is None
                    assert output[0]['violation']=='disk_unverified'
                else:assert usage[0]['peak_bytes']>1024*1024 and output[0]['violation']=='disk_limit'
                assert output[0]['violation_source']=='storage'
            if mode in {'running','unverified'}:assert not (api.root/'unexpected-finished').exists()
            if mode=='retry':assert api.call('POST',f'/fixture/retry/{identifier}')==evidence['policy']
            if mode=='wsl':assert final['attempt']['process_stop']['native_state']=='exited' and final['attempt']['process_stop']['scope_empty']
            if mode.endswith('_disk'):
                caller=mode.split('_')[0]
                assert (api.root/(caller+'-started')).exists() and not (api.root/(caller+'-finished')).exists()
                children=final['attempt']['child_processes']['children']
                assert children and all(row['stop']['verified'] for row in children.values())
                assert final['attempt']['outputs']['publication']['status']=='failed'
            checks.append(mode+'_keeps_frozen_disk_allowance_drain_and_terminal_evidence')
            print('PASS '+checks[-1],flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (api.root/'storage-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+');args=parser.parse_args()
    modes=args.modes or ['normal','fast','running','frozen','hook','worker_disk','finalizer_disk','wsl','unverified','retry']
    report=Path(tempfile.mkdtemp(prefix='execution-storage-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify,modes)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_execstorage_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_execstorage_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,modes,target)
        finally:
            if created:await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('EXECUTION_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
