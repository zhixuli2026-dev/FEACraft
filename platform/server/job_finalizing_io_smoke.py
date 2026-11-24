"""Real Job finalization IO, leases, cancellation and binding races on isolated APIs.

Object-storage transfer uses the real staging adapter with filesystem substitutes.
The OpenFOAM-shaped evidence payload is an IO fixture, not a CFD solver validation.
"""
import argparse
import asyncio
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
from job_launch_lease_smoke import PEER
from app.services import process_identity_native as identity
from workflow_migration_smoke import alembic

CONTROLLED = '''import os
os.environ['VIBEFLOW_JOB_LEASE_SECONDS']='10'
os.environ['VIBEFLOW_JOB_HEARTBEAT_SECONDS']='2'
import asyncio,json,time
from pathlib import Path
from sqlalchemy import select
from fastapi import Depends
from app.db import SessionLocal,get_db
from app.models import Job,JobAttempt,User
from app.services import job_execution,jobrunner,case_evidence,logparse
from app.services.durable_events import lock_case_execution
from app import storage
root=Path(__file__).parent
beats={}
def control():return json.loads((root/'control.json').read_text())
def pause(stage):
 if control()['stage']!=stage or (root/'blocked.once').exists():return
 (root/'blocked.once').touch();(root/'blocked.ready').touch()
 try:
  deadline=time.monotonic()+45
  while not (root/'blocked.continue').exists() and time.monotonic()<deadline:time.sleep(.02)
  if not (root/'blocked.continue').exists():raise TimeoutError('Fixture finalization IO barrier')
  if control()['action']=='io_error':raise OSError('Injected finalization IO error')
 finally:(root/'io.drained').touch()
for suffix in ('put','get','size','get_range','delete','exists','list'):
 setattr(storage,'_s3_'+suffix,getattr(storage,'_fs_'+suffix))
storage._use_s3=lambda:control()['stage'] in {'sync','cleanup'}
jobrunner._use_s3=storage._use_s3
from app.services import staging_sync
from staging_test_facade import install
install()
sync=staging_sync.sync_job
async def syncing(*args):
 return await sync(*args)
staging_sync.sync_job=syncing
collect=staging_sync.collect
def collecting(*args):
 if control().get('sync_boundary','collect')=='collect':pause('sync')
 return collect(*args)
staging_sync.collect=collecting
from app.services import s3_file_transactions as remote_files
prepare_after=remote_files._prepare_after
def preparing_after(*args):
 if control().get('sync_boundary')=='prepare' and (root/'payload.started').exists():pause('sync')
 return prepare_after(*args)
remote_files._prepare_after=preparing_after
apply=remote_files._apply
def applying(*args):
 if control().get('sync_boundary')=='apply' and (root/'payload.started').exists():pause('sync')
 if control().get('sync_boundary')=='recovery' and (root/'payload.started').exists():
  if args[-2]=='before':pause('sync')
  result=apply(*args)
  if args[-2]=='after' and not (root/'recovery-fault.once').exists():
   (root/'recovery-fault.once').touch();raise OSError('Injected live write response failure')
  return result
 return apply(*args)
remote_files._apply=applying
publish_pending=remote_files._publish_pending
def publishing_pending(*args):
 if control().get('sync_boundary')=='metadata' and (root/'payload.started').exists():pause('sync')
 return publish_pending(*args)
remote_files._publish_pending=publishing_pending
load_pending=remote_files._load
def loading_pending(*args,**kwargs):
 if control().get('sync_boundary')=='metadata_read' and (root/'payload.started').exists():pause('sync')
 return load_pending(*args,**kwargs)
remote_files._load=loading_pending
cleanup=jobrunner.cleanup_job_staging
def cleaning(*args):
 pause('cleanup');result=cleanup(*args);(root/'cleanup.returned').touch();return result
jobrunner.cleanup_job_staging=cleaning
evidence=case_evidence.complete_solve_evidence
def completing(*args):
 pause('evidence');return evidence(*args)
case_evidence.complete_solve_evidence=completing
write=Path.write_text
def writing(self,*args,**kwargs):
 if re_match(self.name):pause('evidence_write')
 return write(self,*args,**kwargs)
def re_match(name):return name.startswith('job-') and name.endswith('-evidence.json')
Path.write_text=writing
from app.services import file_read
reader=file_read.FileRead
def local_chunks(prefix,path):
 with storage._fs_path(prefix,path).open('rb') as source:
  yield from iter(lambda:source.read(65536),b'')
def reading(prefix,path):
 if path.endswith('.log'):pause('summary_read')
 if storage._use_s3():
  from types import SimpleNamespace
  return SimpleNamespace(chunks=lambda:local_chunks(prefix,path))
 return reader(prefix,path)
file_read.FileRead=reading
parse=logparse.parse_log_file
def parsing(*args,**kwargs):
 pause('summary_parse');return parse(*args,**kwargs)
logparse.parse_log_file=parsing
heartbeat=job_execution.heartbeat
async def beating(owner):
 beats[owner.job_id]=beats.get(owner.job_id,0)+1
 if control()['action']=='heartbeat_failure' and (root/'blocked.ready').exists():
  (root/'heartbeat.failed').touch();raise OSError('Injected heartbeat failure')
 return await heartbeat(owner)
job_execution.heartbeat=beating
from app.main import app
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
@app.post('/api/v1/fixture/start/{case_id}')
async def start(case_id:int,data:dict,db=Depends(get_db),user:User=Depends(get_current_user)):
 import sys
 case=await get_owned_case(case_id,user,db)
 job=Job(case_id=case.id,type=data.get('type','shell'),command='legacy finalization IO fixture')
 db.add(job);await db.flush();job.log_s3_key=f'.vibeflow/jobs/job-{job.id}.log';await db.commit()
 await jobrunner.start_job(job.id,job.command,case.s3_prefix,
  argv=[sys.executable,str(root/'payload.py')],evidence_run_dir=data.get('run_dir'))
 return {'id':job.id}
@app.get('/api/v1/fixture/state/{job_id}')
async def state(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  proc=jobrunner.get_proc(job_id)
  return {'job_id':job_id,'status':job.status,'phase':a.phase,'heartbeat':a.heartbeat_at.isoformat(),
   'lease':a.lease_expires_at.isoformat(),'beats':beats.get(job_id,0),'worker':json.loads(a.worker_identity_json),
   'local_phase':proc.phase,'finished':proc.finished,'pump_done':proc._pump_task.done(),
   'pump_cancelled':proc._pump_task.cancelled(),'completion_error':proc.completion_error,
   'log_closed':proc._log_handle.closed,'stop':proc._owned_process.stop_result,
   'heartbeat_done':proc._heartbeat_task.done(),'staging_prefix':proc.staging_prefix,
   'parsed_summary':job.parsed_summary,'log_key':job.log_s3_key,'prefix':case.s3_prefix}
@app.post('/api/v1/fixture/action/{job_id}')
async def act(job_id:int,data:dict,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  if data['kind']=='task_cancel':jobrunner.get_proc(job_id)._pump_task.cancel()
  else:
   await lock_case_execution(db,case.id)
   if data['kind']=='log_changed':job.log_s3_key='.vibeflow/jobs/rebound.log'
   elif data['kind']=='prefix_changed':case.s3_prefix=case.s3_prefix+'rebound/'
   elif data['kind']=='summary_changed':job.parsed_summary=json.dumps({'fixture':'newer summary'})
   else:raise ValueError('Unknown fixture action')
   await db.commit()
 return {'ok':True}
'''

from alias_invalidation_test import FIXTURE as ALIAS_FIXTURE
CONTROLLED += ALIAS_FIXTURE + """
read_alias_manifest=artifact_publication.read_manifest
def reading_alias_manifest(*args):
 if control().get('sync_boundary')=='aliases' and (root/'payload.started').exists():pause('sync')
 return read_alias_manifest(*args)
artifact_publication.read_manifest=reading_alias_manifest
"""

STAGES = ('evidence', 'evidence_write', 'sync', 'summary_read', 'summary_parse', 'cleanup')


def matrix():
    return ([(stage, 'normal') for stage in STAGES]
            + [(stage, 'cancel') for stage in STAGES if stage != 'cleanup']
            + [(stage, 'task_cancel') for stage in STAGES]
            + [(stage, 'io_error') for stage in STAGES]
            + [(stage, 'heartbeat_failure') for stage in ('evidence', 'sync', 'summary_parse')]
            + [('summary_parse', action) for action in ('log_changed', 'prefix_changed', 'summary_changed')])


def wait_done(api, job_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = api.call('GET', f'/fixture/state/{job_id}')
        if state['pump_done'] and state['heartbeat_done']:
            return state
        time.sleep(.05)
    raise TimeoutError('Finalization did not drain: ' + str(state))


def verify(database_url=None,selected=None,sync_boundary='collect'):
    api = IsolatedServer('job-finalizing-io', app='finalizing_fixture:app', database_url=database_url)
    (api.root/'finalizing_fixture.py').write_text(CONTROLLED, encoding='utf8')
    (api.root/'control.json').write_text(json.dumps({'stage':'none','action':'none'}), encoding='utf8')
    checks, receipts = [], []
    with api:
        peer = IsolatedServer('job-finalizing-io-peer', app='finalizing_peer:app', database_url=database_url)
        overrides = {key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        (peer.root/'finalizing_peer.py').write_text(PEER.replace('OVERRIDES', repr(overrides)), encoding='utf8')
        with peer:
            for stage, action in matrix():
                if sync_boundary=='aliases' and (stage!='sync' or action not in {'normal','cancel','task_cancel','io_error'}):continue
                if selected and stage+'_'+action not in selected:continue
                for name in ('blocked.once','blocked.ready','blocked.continue','io.drained','heartbeat.failed','payload.started','cleanup.returned','recovery-fault.once'):
                    (api.root/name).unlink(missing_ok=True)
                (api.root/'control.json').write_text(json.dumps({'stage':stage,'action':action,'sync_boundary':sync_boundary}), encoding='utf8')
                api.create_case('Finalizing '+stage+' '+action); peer.token = api.token
                case = api.call('GET', api.prefix)
                api.put('source.txt', 'unaltered source\n')
                case_root = api.root/'files'/f"workspaces/{case['workspace_id']}/cases/{api.case_id}"
                alias=None
                if sync_boundary=='aliases':alias=api.call('POST',f'/fixture/alias/{api.case_id}')
                source = case_root/'source.txt'; source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                script = api.root/'payload.py'; marker = api.root/'payload.started'
                code = 'from pathlib import Path\nPath('+repr(str(marker))+').write_text("once")\n'
                body = {'command':'"'+sys.executable+'" "'+str(script)+'"'}
                if stage.startswith('evidence'):
                    # Deliberately minimal IO fixtures: these are not physical mesh/field values.
                    for path in ('system/controlDict','constant/polyMesh/points','constant/polyMesh/faces',
                                 'constant/polyMesh/owner','constant/polyMesh/boundary'):
                        api.put('C-run/fixture/'+path, 'explicit evidence IO fixture\n')
                    code += ('p=Path("C-run/fixture/1/U");p.parent.mkdir(parents=True,exist_ok=True);'
                             'p.write_text("explicit output IO fixture")\n')
                    body.update(type='solve', run_dir='C-run/fixture')
                log = ('Time = 1\nCourant Number mean: 0.1 max: 0.5\n'
                       'smoothSolver: Solving for Ux, Initial residual = 0.01, Final residual = 1e-06, No Iterations 2\n'
                       'ExecutionTime = 1.2 s  ClockTime = 2 s\nEnd')
                if sync_boundary=='aliases':code += 'Path("alias.txt").write_text("new")\n'
                code += 'print('+repr(log)+')\n'
                script.write_text(code, encoding='utf8')
                # Deliberately exercise the historical direct-runner contract.
                # Public submissions now use sealed workspaces and typed publication;
                # those are covered by the separate publication/attempt suites.
                submitted = api.call('POST', f'/fixture/start/{api.case_id}', body); job_id = submitted['id']
                wait_file(api.root/'blocked.ready')
                started = time.monotonic(); before = api.call('GET', f'/fixture/state/{job_id}')
                responsive_seconds = time.monotonic()-started
                assert responsive_seconds < 2.5 and not before['pump_done'], before
                assert identity.probe(before['worker']['process'])['state']=='match'
                assert before['stop']['verified'], before
                assert marker.read_text()=='once'
                during = None; timings = {'state_response_seconds':responsive_seconds}
                if action == 'normal' and stage != 'cleanup':
                    time.sleep(12)
                    during = api.call('GET', f'/fixture/state/{job_id}')
                    assert during['beats'] >= before['beats']+4 and during['heartbeat'] != before['heartbeat'], during
                    assert datetime.fromisoformat(during['lease']).replace(tzinfo=timezone.utc)>datetime.now(timezone.utc)
                    assert during['status']=='running' and during['phase']=='finalizing', during
                elif action == 'cancel':
                    started = time.monotonic(); reply = peer.call('POST', f'/jobs/{job_id}:cancel')
                    timings['durable_cancel_seconds'] = time.monotonic()-started
                    assert reply['ok'] and timings['durable_cancel_seconds']<2.5, (reply, timings)
                    assert peer.call('GET', f'/jobs/{job_id}')['status']=='cancelled'
                    assert not peer.call('POST', f'/jobs/{job_id}:cancel')['ok']
                elif action == 'task_cancel':
                    for _ in range(2):
                        api.call('POST', f'/fixture/action/{job_id}', {'kind':action})
                        time.sleep(.1)
                    during = api.call('GET', f'/fixture/state/{job_id}')
                    assert not during['pump_done'] and not (api.root/'io.drained').exists(), during
                    if stage in {'evidence','evidence_write','sync'}:assert not during['log_closed']
                    assert during['finished'] == (stage=='cleanup'), during
                elif action == 'heartbeat_failure':
                    wait_file(api.root/'heartbeat.failed', seconds=8)
                elif action.endswith('_changed'):
                    api.call('POST', f'/fixture/action/{job_id}', {'kind':action})
                (api.root/'blocked.continue').touch()
                state = wait_done(api, job_id)
                final = api.call('GET', f'/jobs/{job_id}')
                if action == 'task_cancel' and stage != 'cleanup':
                    assert final['status']=='running' and final['ended_at'] is None, final
                    assert state['phase']=='finalizing' and state['local_phase']=='finalization_failed', state
                    assert not state['finished'] and state['pump_cancelled'] and state['completion_error'], state
                    # No false terminal receipt: recovery of this cancelled task is separate N5 work.
                else:
                    target = ('cancelled' if action=='cancel' else 'failed' if action=='heartbeat_failure'
                              or (action=='io_error' and stage in {'evidence','evidence_write','sync'} and sync_boundary!='aliases')
                              or (stage=='sync' and sync_boundary=='recovery') else 'succeeded')
                    assert final['status']==target and final['ended_at'] is not None, final
                    assert state['finished'] and state['local_phase']=='completed' and not state['completion_error'], state
                    assert final['attempt']['process_stop']['verified'], final
                    if action=='heartbeat_failure':assert final['attempt']['reason_code']=='persistence_failed'
                    if action=='io_error' and target=='failed':
                        assert final['attempt']['reason_code']==('sync_failed' if stage=='sync' else 'evidence_invalid')
                if action.endswith('_changed'):
                    expected = {'fixture':'newer summary'} if action=='summary_changed' else {}
                    assert json.loads(state['parsed_summary'] or '{}')==expected, state
                elif action not in {'task_cancel','io_error'} and not (stage=='sync' and (action=='cancel' or sync_boundary=='recovery')):
                    assert final['parsed_summary']['summary']['last_time']==1, final
                if stage.startswith('evidence') and action=='normal':
                    evidence = json.loads((case_root/f'.vibeflow/jobs/job-{job_id}-evidence.json').read_text())
                    assert evidence['job_id']==job_id and evidence['summary']['finished_cleanly'] and evidence['outputs']
                if alias:
                    alias_after=api.call('GET',f'/fixture/alias/{api.case_id}')
                    committed=action in {'normal','io_error'}
                    assert alias_after['publication_id']==(None if committed else alias['publication_id']),alias_after
                    assert (case_root/'alias.txt').read_text()==('new' if committed else 'old')
                staging = None
                if state['staging_prefix']:
                    stage_root = api.root/'files'/state['staging_prefix']
                    retained = stage_root.exists()
                    expected = ((stage=='sync' and (action in {'task_cancel','cancel'} or (action=='io_error' and sync_boundary!='aliases') or sync_boundary=='recovery'))
                                or (stage=='cleanup' and action=='io_error'))
                    returned = (api.root/'cleanup.returned').exists()
                    # Existing storage cleanup is best effort (rmtree(ignore_errors=True)).
                    # A drained thread does not prove every directory was removed on Windows.
                    # Retain observations; do not silently upgrade this unit to recovery coverage.
                    if expected:assert retained and not returned, (stage, action, retained)
                    else:assert returned, (stage, action)
                    staging = {'retained':retained,'cleanup_returned':returned,
                               'remaining':[p.relative_to(stage_root).as_posix() for p in stage_root.rglob('*')]}
                assert state['log_closed'] and (api.root/'io.drained').exists()
                assert hashlib.sha256(source.read_bytes()).hexdigest()==source_hash
                receipts.append({'stage':stage,'action':action,'before':before,'during':during,'state':state,
                                 'final':final,'timings':timings,'source_unchanged':True,'staging':staging})
                checks.append(stage+'_'+action)
                (api.root/'io-receipts.json').write_text(json.dumps(receipts,indent=2), encoding='utf8')
                print('PASS '+checks[-1], flush=True)
    result = {'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),'peer_evidence':str(peer.root),
              'real_s3_used':False,'real_cfd_solve':False,'database_isolated':True,'developer_database_touched':False}
    (api.root/'io-result.json').write_text(json.dumps(result,indent=2), encoding='utf8')
    return result


async def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--checks',nargs='+')
    parser.add_argument('--sync-boundary',choices=['collect','prepare','apply','metadata','metadata_read','recovery','aliases'],default='collect'); args = parser.parse_args()
    report = Path(tempfile.mkdtemp(prefix='job-finalizing-io-evidence-',dir=SERVER.parents[1]/'tmp'))
    if args.checks:assert set(args.checks)<=set(stage+'_'+action for stage,action in matrix())
    results = {'sqlite':await asyncio.to_thread(verify,None,args.checks,args.sync_boundary)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url = make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name = 'tmp_vf_finalio_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_finalio_[0-9a-f]{32}',name)
        admin = await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,
                                     password=url.password,database='postgres',timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"'); created = True
            temporary = url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql'] = await asyncio.to_thread(verify,temporary,args.checks,args.sync_boundary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed'] = True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('JOB_FINALIZING_IO_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
