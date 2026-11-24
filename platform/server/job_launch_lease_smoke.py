"""Actual launches renew before release; bulk input reads do not block cancellation.

Staging cases deliberately route real filesystem copies through the staging
adapter. They do not claim coverage of a real S3 service or network outage.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from job_release_smoke import wait_file
from app.services import process_identity_native as identity
from app.services.owned_process import wsl_path, probe_native
from workflow_migration_smoke import alembic

CONTROLLED = '''import os
os.environ['VIBEFLOW_JOB_LEASE_SECONDS']='10'
os.environ['VIBEFLOW_JOB_HEARTBEAT_SECONDS']='2'
import asyncio,json,time
from pathlib import Path
from sqlalchemy import select
from fastapi import Depends
from app.db import SessionLocal
from app.models import Job,JobAttempt,User
from app.services import job_execution,jobrunner,attempt_workspace
from app.services.owned_process import OwnedProcess
from app.services.durable_events import lock_case_execution,json_text
from app import storage
from app.services import job_delivery
async def controlled_delivery(stop):await stop.wait()
job_delivery.recover_forever=controlled_delivery
# The staging adapter's explicit upload functions target this disposable fs
# fixture. Never contact the configured object-storage endpoint from this test.
storage._s3_put=lambda prefix,path,data,content_type=None:storage._fs_put(prefix,path,data)
storage._s3_delete=storage._fs_delete
root=Path(__file__).parent
beats={}
def control():return json.loads((root/'control.json').read_text())
submit=job_execution.submit
async def submitting(*args,**kwargs):
 # This branch tests the retained legacy staging adapter. Default S3 shell
 # isolation is covered against real SeaweedFS by s3_attempt_smoke.
 if control()['stage']=='staging':kwargs['isolate_default']=False
 return await submit(*args,**kwargs)
job_execution.submit=submitting
def blocked(stage):
 return control()['stage']==stage and not (root/'blocked.once').exists()
def pause_sync():
 (root/'blocked.once').touch();(root/'blocked.ready').touch()
 try:
  deadline=time.monotonic()+45
  while not (root/'blocked.continue').exists() and time.monotonic()<deadline:time.sleep(.02)
  if not (root/'blocked.continue').exists():raise TimeoutError('Fixture bulk-read barrier')
 finally:(root/'io.drained').touch()
async def pause_async():
 (root/'blocked.once').touch();(root/'blocked.ready').touch()
 try:
  for _ in range(2250):
   if (root/'blocked.continue').exists():return
   await asyncio.sleep(.02)
  raise TimeoutError('Fixture native-preparation barrier')
 finally:(root/'io.drained').touch()
claim=job_execution.claim_job
async def claiming(*args,**kwargs):
 owner=await claim(*args,**kwargs)
 (root/'claim.json').write_text(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id}))
 return owner
job_execution.claim_job=claiming
heartbeat=job_execution.heartbeat
async def beating(owner):
 beats[owner.job_id]=beats.get(owner.job_id,0)+1
 if control()['action']=='heartbeat_failure' and (root/'blocked.ready').exists():
  (root/'heartbeat.failed').touch();raise OSError('Injected heartbeat failure')
 return await heartbeat(owner)
job_execution.heartbeat=beating
prepare=jobrunner.prepare_job_staging
def preparing(prefix,job_id):
 result=prepare(prefix,job_id)
 (root/'staging.json').write_text(json.dumps({'prefix':result}))
 if blocked('staging'):pause_sync()
 return result
jobrunner.prepare_job_staging=preparing
jobrunner._use_s3=lambda:control()['stage']=='staging'
inspect=attempt_workspace.inspect_runtime
def inspecting(prefix,manifest,complete):
 if blocked('workspace_finish' if complete else 'workspace'):pause_sync()
 return inspect(prefix,manifest,complete)
attempt_workspace.inspect_runtime=inspecting
native_prepare=OwnedProcess.prepare
async def preparing_native(self,*args,**kwargs):
 result=await native_prepare(self,*args,**kwargs)
 (root/'native.json').write_text(json.dumps(self.record))
 if blocked('native_prepare'):await pause_async()
 return result
OwnedProcess.prepare=preparing_native
release=job_execution.release_process
async def releasing(owner,process):
 if blocked('registered'):await pause_async()
 return await release(owner,process)
job_execution.release_process=releasing
from app.main import app
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
@app.get('/api/v1/fixture/state/{job_id}')
async def state(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  proc=jobrunner.get_proc(job_id)
  return {'job_id':job_id,'status':job.status,'phase':a.phase,'heartbeat':a.heartbeat_at.isoformat(),
   'lease':a.lease_expires_at.isoformat(),'beats':beats.get(job_id,0),'worker':json.loads(a.worker_identity_json),
   'guard_detached':proc._launch_guard is None if proc else None,
   'heartbeat_done':proc._heartbeat_task.done() if proc and proc._heartbeat_task else None}
@app.post('/api/v1/fixture/mutate/{job_id}')
async def mutate(job_id:int,data:dict,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  await lock_case_execution(db,case.id)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  original=json.loads(a.input_manifest_json)
  if data['kind']=='manifest_changed':
   original['fixture_changed']=True;a.input_manifest_json=json_text(original)
  else:
   from app.storage import _fs_path
   (_fs_path(case.s3_prefix,original['workspace']['run_path'])/'source.txt').write_text('changed work copy')
  await db.commit()
 return {'ok':True}
'''

PEER = '''import os
os.environ.update(OVERRIDES)
from app.services import job_delivery
async def controlled_delivery(stop):await stop.wait()
job_delivery.recover_forever=controlled_delivery
from app.main import app
'''


def matrix():
    return [('staging','normal','windows'),('workspace','normal','windows'),
            ('native_prepare','normal','windows'),('native_prepare','normal','wsl'),
            ('workspace_finish','normal','windows'),
            *[(stage,'cancel','windows') for stage in ('staging','workspace','native_prepare','registered','workspace_finish')],
            *[(stage,'heartbeat_failure','windows') for stage in ('staging','workspace','native_prepare','registered','workspace_finish')],
            ('workspace','manifest_changed','windows'),('workspace','run_changed','windows')]


def settled(api, job_id):
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        row=api.call('GET',f'/jobs/{job_id}');attempt=row.get('attempt',{})
        if (row['status'] in {'succeeded','failed','cancelled','interrupted'} and row['ended_at'] is not None
                and not row['completion_pending'] and attempt.get('phase') in {'succeeded','failed','cancelled','interrupted'}
                and (attempt.get('process_identity') is None or attempt.get('process_stop') is not None)):
            return row
        time.sleep(.05)
    raise TimeoutError('Launch did not settle: '+str(job_id))


def verify(database_url=None):
    api=IsolatedServer('job-launch-lease',app='launch_fixture:app',database_url=database_url)
    (api.root/'launch_fixture.py').write_text(CONTROLLED,encoding='utf8')
    (api.root/'control.json').write_text(json.dumps({'stage':'none','action':'none'}),encoding='utf8')
    checks=[];receipts=[]
    with api:
        peer=IsolatedServer('job-launch-lease-peer',app='launch_peer:app',database_url=database_url)
        overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        (peer.root/'launch_peer.py').write_text(PEER.replace('OVERRIDES',repr(overrides)),encoding='utf8')
        with peer:
            for stage,action,runtime in matrix():
                for name in ('claim.json','blocked.once','blocked.ready','blocked.continue','io.drained',
                             'heartbeat.failed','staging.json','native.json','payload.started'):
                    (api.root/name).unlink(missing_ok=True)
                (api.root/'control.json').write_text(json.dumps({'stage':stage,'action':action}),encoding='utf8')
                api.create_case('Launch lease '+stage+' '+action+' '+runtime);peer.token=api.token
                api.put('source.txt','sealed source\n')
                # The workspace id changes with each fixture account. Resolve
                # the actual case prefix from the authenticated case response.
                case=api.call('GET',api.prefix)
                source=api.root/'files'/f"workspaces/{case['workspace_id']}/cases/{api.case_id}/source.txt"
                source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
                marker=api.root/'payload.started';script=api.root/'payload.py'
                marker_path=str(marker) if runtime=='windows' else wsl_path(marker)
                script.write_text('from pathlib import Path\np=Path('+repr(marker_path)+')\n'
                    'with p.open("a") as f:f.write("start\\n")\nprint("lease payload completed")\n',encoding='utf8')
                command='"'+sys.executable+'" "'+str(script)+'"' if runtime=='windows' else 'python3 '+wsl_path(script)
                body={'command':command,'runtime':runtime}
                if stage.startswith('workspace'):body['input_paths']=['source.txt']
                # Cancellation may return the admitted terminal Job, or reject
                # a prepared native launch whose binding was invalidated.
                expected=(200,409) if action=='cancel' else 200 if action=='normal' or stage=='workspace_finish' else 409
                timings={}
                with ThreadPoolExecutor(max_workers=1) as pool:
                    submit=pool.submit(api.call,'POST',api.prefix+'/jobs',body,expect=expected)
                    wait_file(api.root/'blocked.ready')
                    claim=json.loads((api.root/'claim.json').read_text());job_id=claim['job_id']
                    before=api.call('GET',f'/fixture/state/{job_id}')
                    assert identity.probe(before['worker']['process'])['state']=='match'
                    if action=='normal':
                        end=time.monotonic()+12
                        while time.monotonic()<end:time.sleep(.1)
                        during=api.call('GET',f'/fixture/state/{job_id}')
                        assert during['beats']>=before['beats']+4 and during['heartbeat']!=before['heartbeat'],during
                        assert datetime.fromisoformat(during['lease']).replace(tzinfo=timezone.utc)>datetime.now(timezone.utc)
                        assert during['phase']==('finalizing' if stage=='workspace_finish' else 'starting' if stage=='native_prepare' else 'claimed'),during
                    elif action=='cancel':
                        started=time.monotonic();reply=peer.call('POST',f'/jobs/{job_id}:cancel')
                        timings['durable_cancel_seconds']=time.monotonic()-started
                        assert reply['ok'] and timings['durable_cancel_seconds']<2.5,(reply,timings)
                        assert peer.call('GET',f'/jobs/{job_id}')['status']=='cancelled'
                        assert not peer.call('POST',f'/jobs/{job_id}:cancel')['ok']
                    elif action=='heartbeat_failure':
                        wait_file(api.root/'heartbeat.failed',seconds=8)
                    else:
                        api.call('POST',f'/fixture/mutate/{job_id}',{'kind':action})
                    if action in {'cancel','heartbeat_failure'} and stage in {'workspace','staging'}:
                        time.sleep(.15)
                        assert not submit.done() and not (api.root/'io.drained').exists(), 'Returned before filesystem work drained'
                    (api.root/'blocked.continue').touch()
                    response=submit.result(timeout=30)
                    if action=='cancel' and stage!='workspace_finish' and 'id' in response:
                        assert response['status']=='cancelled',response
                final=settled(api,job_id)
                target='succeeded' if action=='normal' else 'cancelled' if action=='cancel' else 'failed'
                assert final['status']==target,final
                if action=='normal' or stage=='workspace_finish':
                    assert marker.read_text().splitlines()==['start']
                    assert final['attempt']['process_stop']['verified']
                else:assert not marker.exists()
                if action in {'manifest_changed','run_changed'}:
                    assert final['attempt']['reason_code']=='input_stale',final
                if (api.root/'native.json').exists():
                    actual=json.loads((api.root/'native.json').read_text())
                    assert identity.probe(actual['host'])['state']=='exited'
                    if actual['native'] is not None:
                        assert asyncio.run(probe_native(actual['native']['distribution'],actual['native']['process']))['state']=='exited'
                state=api.call('GET',f'/fixture/state/{job_id}')
                if stage in {'native_prepare','registered','workspace_finish'} or action=='normal':
                    assert state['guard_detached'] is True
                if (api.root/'staging.json').exists():
                    prefix=json.loads((api.root/'staging.json').read_text())['prefix']
                    assert not (api.root/'files'/prefix).exists(),'Retired prelaunch/normal staging survived'
                assert hashlib.sha256(source.read_bytes()).hexdigest()==source_hash
                assert (api.root/'io.drained').exists()
                row={'stage':stage,'action':action,'runtime':runtime,'before':before,'final':final,
                     'state':state,'timings':timings,'response':response,'source_unchanged':True}
                if action=='normal':row['during']=during
                receipts.append(row);checks.append(stage+'_'+action+'_'+runtime)
                (api.root/'launch-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
                print('PASS '+checks[-1],flush=True)
            before=api.call('GET',f'/fixture/state/{job_id}')
            time.sleep(2.3)
            after=api.call('GET',f'/fixture/state/{job_id}')
            assert after['beats']==before['beats'],'Preparation heartbeat survived failed launch'
            checks.append('failed_preparation_heartbeat_retired')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),'peer_evidence':str(peer.root),
            'staging_backend':'real filesystem copies through forced staging adapter','real_s3_used':False,
            'database_isolated':True,'developer_database_touched':False}
    (api.root/'launch-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='job-launch-lease-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_launch_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_launch_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,
            password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('JOB_LAUNCH_LEASE_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
