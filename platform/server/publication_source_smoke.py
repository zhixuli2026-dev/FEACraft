"""Actual Pandoc/consumer completion under cross-API source reservations."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer,SERVER
from job_release_smoke import wait_file
from professional_launch_smoke import settled
from workflow_migration_smoke import alembic


CONTROLLED='''import os
os.environ['VIBEFLOW_JOB_LEASE_SECONDS']='10'
os.environ['VIBEFLOW_JOB_HEARTBEAT_SECONDS']='2'
import asyncio,json,time,uuid
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Case,Job,JobAttempt,ArtifactPublication,ArtifactPublicationHead,CaseValidationFence
from app.routers.workspaces import get_owned_case
from app.services import publication_source_validation as source,case_validation as fence,jobrunner
from app.services import publication_inputs,artifact_publication as pub
from app.services.report_handler import ReportHandler
from app.services.durable_events import lock_case_execution
root=Path(os.environ.get('VIBEFLOW_SOURCE_CONTROL',str(Path(__file__).parent)))
active=ContextVar('fixture_source_validation',default=False)
def control():return json.loads((root/'control.json').read_text())
def enter(stage):
 if active.get() and control()['stage']==stage and not (root/'blocked.once').exists():
  (root/'blocked.once').touch();(root/'blocked.ready').touch();return True
 return False
def pause():
 try:
  for _ in range(3000):
   if (root/'blocked.continue').exists():return
   time.sleep(.02)
  raise TimeoutError('Source validation barrier')
 finally:(root/'read.drained').touch()
checking=source.SourceValidation.inspect
async def inspect(self,*args):
 token=active.set(True)
 try:
  result=await checking(self,*args)
  if enter('verified'):
   for _ in range(3000):
    if (root/'blocked.continue').exists():break
    await asyncio.sleep(.02)
   else:raise TimeoutError('Verified source barrier')
   if control()['action']=='proof_changed':self.snapshot['prefix']+='-forged'
  return result
 finally:active.reset(token)
source.SourceValidation.inspect=inspect
original_source=ReportHandler.source
def report_source(self,*args):
 if enter('source'):pause()
 return original_source(self,*args)
ReportHandler.source=report_source
original_files=publication_inputs.inspect_files
def inputs(*args):
 if enter('dependency'):pause()
 return original_files(*args)
publication_inputs.inspect_files=inputs
original_clock=fence.database_now
async def clock(db):return await original_clock(db)+timedelta(seconds=control().get('clock',0))
fence.database_now=clock
renew=fence.renew_locked
async def renewing(*args,**kwargs):
 if control()['action']=='renewal_failure' and (root/'blocked.ready').exists():
  (root/'renewal.failed').touch();raise OSError('Injected renewal failure')
 return await renew(*args,**kwargs)
fence.renew_locked=renewing
final=pub.finalize
async def finalize(db,job,*args):
 result=await final(db,job,*args)
 if control()['action']=='commit_error' and (root/'blocked.ready').exists():db.info['source_commit_failure']=True
 return result
pub.finalize=finalize
commit=AsyncSession.commit
async def committing(db):
 if db.info.pop('source_commit_failure',False):raise OSError('Injected terminal commit failure')
 return await commit(db)
AsyncSession.commit=committing
release=source.SourceValidation.release_locked
async def releasing(self,db,status):
 if control()['action']=='release_expired' and status=='succeeded':
  job=await db.get(Job,self.owner.job_id)
  attempt=await db.get(JobAttempt,self.owner.attempt_id)
  assert job.status=='succeeded' and attempt.phase=='succeeded'
  value=control();value['clock']=30
  (root/'control.json').write_text(json.dumps(value))
  (root/'terminal.expired').touch()
 return await release(self,db,status)
source.SourceValidation.release_locked=releasing

@app.get('/api/v1/fixture/state/{job_id}')
async def state(job_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  row=await db.scalar(select(ArtifactPublication).where(ArtifactPublication.attempt_id==a.id))
  head=await db.get(ArtifactPublicationHead,(job.case_id,'report'))
  held=await db.get(CaseValidationFence,job.case_id)
  proc=jobrunner.get_proc(job_id)
  return {'status':job.status,'phase':a.phase,'heartbeat':str(a.heartbeat_at),'lease':str(a.lease_expires_at),
   'publication':row.id if row else None,'publication_status':row.status if row else None,
   'active':head.active_publication_id if head else None,'fence':held.status if held else None,
   'fence_heartbeat':str(held.heartbeat_at) if held else None,'fence_generation':held.generation if held else None,
   'pump_done':proc._pump_task.done() if proc else None,'finished':proc.finished if proc else None,
   'completion_error':proc.completion_error if proc else None,'stop':proc._owned_process.stop_result if proc else None,
   'output':json.loads(a.output_manifest_json)}

@app.post('/api/v1/fixture/mutate/{job_id}')
async def mutate(job_id:int,body:dict,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  action=body['action']
  if action=='task_cancel':jobrunner.get_proc(job_id)._pump_task.cancel();return {'ok':True}
  if action=='revoke':
   case_id=case.id;await db.rollback();return {'revoked':await fence.revoke_expired(case_id)}
  await lock_case_execution(db,case.id)
  if action=='producer_changed':
   producer=await db.get(Job,body['producer']);assert producer.case_id==case.id
   producer.status='failed'
  elif action=='owner_changed':
   a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id));a.claim_token=uuid.uuid4().hex
  elif action=='unknown_history':
   a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id));a.write_checkpoint_json='{}'
  elif action=='missing_head':
   db.add(ArtifactPublicationHead(case_id=case.id,kind='postprocess',next_generation=1,version=0))
  elif action=='superseded':
   head=await db.get(ArtifactPublicationHead,(case.id,'report'));head.next_generation+=1;head.version+=1
  else:raise ValueError(action)
  await db.commit();return {'ok':True}
'''

MATRIX=[('source',action) for action in ('normal','cancel','task_cancel','renewal_failure','expired','revoked','source_changed')]
MATRIX += [('verified',action) for action in ('missing_head','proof_changed','owner_changed','commit_error','superseded','producer_changed','release_expired')]
MATRIX += [('dependency',action) for action in ('normal','damaged_input')]


QUIESCENCE=CONTROLLED+'''
import sys
from app.services import job_execution
acquire=source.SourceValidation.acquire
async def acquiring(self,*args):
 (root/(str(self.owner.job_id)+'.acquire.enter')).touch()
 return await acquire(self,*args)
source.SourceValidation.acquire=acquiring
immutable=pub.verify_immutable_files
def verifying(prepared):
 (root/(str(prepared.job_id)+'.immutable.enter')).touch()
 return immutable(prepared)
pub.verify_immutable_files=verifying
aliases=pub.repair_aliases
async def repairing(owner):
 if control()['action']=='alias_wait' and not (root/'alias.once').exists():
  (root/'alias.once').touch();(root/'alias.ready').touch()
  for _ in range(3000):
   if (root/'alias.continue').exists():break
   await asyncio.sleep(.02)
  else:raise TimeoutError('Alias barrier')
 return await aliases(owner)
pub.repair_aliases=repairing
@app.post('/api/v1/fixture/writer')
async def writer(case_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  spec={'command':'controlled actual case writer','runtime':'windows','case_prefix':case.s3_prefix,
   'argv':[sys.executable,'-I','-S',str(root/'writer.py')],
   'timeout_seconds':60,'max_log_bytes':1048576,'evidence_run_dir':None}
  job,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=None,
   digest=job_execution.request_hash(spec),job_type='shell',command=spec['command'],spec=spec)
  await db.commit()
 await jobrunner.start_job(job.id,spec['command'],spec['case_prefix'],argv=spec['argv'],
  timeout_seconds=60,max_log_bytes=1048576)
 return {'job_id':job.id}
'''


def verify_quiescence(database_url=None):
    api=IsolatedServer('publication-quiescence',app='fixture:app',database_url=database_url)
    (api.root/'fixture.py').write_text(QUIESCENCE,encoding='utf8')
    (api.root/'writer.py').write_text('from pathlib import Path\nimport time\nroot=Path('+repr(str(api.root))+')\n'
        '(root/"writer.ready").touch()\nfor _ in range(2500):\n'
        ' if (root/"writer.continue").exists():break\n time.sleep(.02)\n'
        'else:raise TimeoutError("writer barrier")\nPath("native.txt").write_text("NATIVE_DRAINED")\n',encoding='utf8')
    def configure(stage='',action=''):
        (api.root/'control.json').write_text(json.dumps({'stage':stage,'action':action}),encoding='utf8')
    configure();checks=[];receipts=[]
    with api:
        peer=IsolatedServer('publication-quiescence-peer',app='fixture:app',database_url=database_url)
        overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        overrides['VIBEFLOW_SOURCE_CONTROL']=str(api.root)
        (peer.root/'fixture.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+QUIESCENCE,encoding='utf8')
        with peer:
            for action in ('writer_wait','alias_wait'):
                configure()
                for name in ('blocked.once','blocked.ready','blocked.continue','read.drained',
                             'writer.ready','writer.continue','alias.ready','alias.continue','alias.once'):
                    (api.root/name).unlink(missing_ok=True)
                api.create_case('Quiescence '+action);peer.token=api.token
                api.put('E-report/report.md','# Quiescent snapshot\n')
                if action=='writer_wait':
                    configure('source',action)
                    previous=api.call('POST',f'/fixture/writer?case_id={api.case_id}')['job_id']
                    wait_file(api.root/'writer.ready')
                else:
                    configure('',action)
                    previous=api.call('POST',api.prefix+'/report:build',{})['job_id']
                    wait_file(api.root/'alias.ready',seconds=90)
                    prior=api.call('GET',f'/fixture/state/{previous}')
                    assert prior['status']=='succeeded' and not prior['finished'],prior
                    configure('source',action)
                job=api.call('POST',api.prefix+'/report:build',{})['job_id']
                def state():return api.call('GET',f'/fixture/state/{job}')
                try:
                    wait_file(api.root/f'{job}.acquire.enter',seconds=90)
                    initial=state();time.sleep(12);waiting=state()
                    assert not (api.root/f'{job}.immutable.enter').exists(),waiting
                    assert waiting['heartbeat']!=initial['heartbeat'] and waiting['phase']=='finalizing',waiting
                    (api.root/('writer.continue' if action=='writer_wait' else 'alias.continue')).touch()
                    assert settled(api,previous)['status']=='succeeded'
                    wait_file(api.root/'blocked.ready',seconds=30)
                    assert (api.root/f'{job}.immutable.enter').exists() and state()['fence']=='held'
                    peer.call('PUT',api.prefix+'/file',{'path':'blocked.txt','content':'FORBIDDEN'},expect=409)
                finally:
                    for name in ('writer.continue','alias.continue','blocked.continue'):(api.root/name).touch()
                final=settled(api,job)
                assert final['status']=='succeeded' and final['attempt']['process_stop']['verified'],final
                checks.append(action+'_precedes_immutable_hash_and_live_validation')
                receipts.append({'check':checks[-1],'initial':initial,'waiting':waiting,'final':state()})
                print('PASS '+checks[-1],flush=True)
            configure()
            api.create_case('Unknown history requires recovery');peer.token=api.token
            api.put('E-report/report.md','# Unknown historical write state\n')
            previous=api.call('POST',api.prefix+'/report:build',{})['job_id']
            assert settled(api,previous)['status']=='succeeded'
            active=api.call('GET',api.prefix+'/report/results')['publication']['id']
            api.call('POST',f'/fixture/mutate/{previous}',{'action':'unknown_history'})
            job=api.call('POST',api.prefix+'/report:build',{})['job_id']
            wait_file(api.root/f'{job}.acquire.enter',seconds=90)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                final=api.call('GET',f'/fixture/state/{job}')
                if final['pump_done']:break
                time.sleep(.05)
            assert final['pump_done'] and final['completion_error'] and final['status']=='running',final
            assert final['active']==active and final['fence']=='released' and final['stop']['verified'],final
            assert not (api.root/f'{job}.immutable.enter').exists()
            checks.append('unknown_terminal_history_requires_recovery_without_waiting_full_job_budget')
            receipts.append({'check':checks[-1],'final':final})
            print('PASS '+checks[-1],flush=True)
    result={'count':len(checks),'checks':checks,'receipts':receipts,'api_root':str(api.root),'peer_root':str(peer.root)}
    (api.root/'quiescence-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


def verify(database_url=None,matrix=MATRIX):
    api=IsolatedServer('publication-source',app='fixture:app',database_url=database_url)
    (api.root/'fixture.py').write_text(CONTROLLED,encoding='utf8')
    control=api.root/'control.json'
    def configure(stage='',action='',clock=0):
        control.write_text(json.dumps({'stage':stage,'action':action,'clock':clock}),encoding='utf8')
    configure()
    checks=[];receipts=[]
    with api:
        peer=IsolatedServer('publication-source-peer',app='fixture:app',database_url=database_url)
        overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        overrides['VIBEFLOW_SOURCE_CONTROL']=str(api.root)
        (peer.root/'fixture.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+CONTROLLED,encoding='utf8')
        with peer:
            for stage,action in matrix:
                configure()
                for name in ('blocked.once','blocked.ready','blocked.continue','read.drained','renewal.failed'):
                    (api.root/name).unlink(missing_ok=True)
                api.create_case('Source '+stage+' '+action);peer.token=api.token
                case_root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                api.put('E-report/report.md','# Initial actual report\n')
                producer=api.call('POST',api.prefix+'/report:build',{})['job_id']
                assert settled(api,producer)['status']=='succeeded'
                original=api.call('GET',api.prefix+'/report/results')
                previous=original['publication']['id']
                consumer=stage=='dependency' or action=='producer_changed'
                if not consumer:api.put('E-report/report.md','# Updated actual report\n')
                workspace=api.call('GET',api.prefix)['workspace_id']
                unrelated=api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Unrelated'})['id']
                configure(stage,action)
                submitted=api.call('POST',api.prefix+('/jobs' if consumer else '/report:build'),
                    {'command':'echo CONSUMED','input_publications':[{'publication_id':previous}]} if consumer else {})
                job=submitted['id'] if consumer else submitted['job_id']
                def state():return api.call('GET',f'/fixture/state/{job}')
                def mutate(which,client=api,**values):
                    return client.call('POST',f'/fixture/mutate/{job}',{'action':which,**values})
                try:
                    wait_file(api.root/'blocked.ready',seconds=90)
                    initial=state();assert initial['fence']=='held',initial
                    blocked=peer.call('PUT',api.prefix+'/file',{'path':'probe.txt','content':'FORBIDDEN'},expect=409)
                    assert blocked['detail']['code']=='validation_busy'
                    peer.call('PUT',f'/cases/{unrelated}/file',{'path':'probe.txt','content':'UNRELATED'})
                    peer.call('GET',api.prefix+'/report/publications')
                    peer.call('GET',api.prefix+'/report/results?publication_id='+previous)
                    if action=='normal':
                        time.sleep(12)
                        later=state()
                        assert later['heartbeat']!=initial['heartbeat'] and later['fence_heartbeat']!=initial['fence_heartbeat'],later
                        assert later['fence']=='held' and later['status']=='running'
                    if action=='cancel':
                        started=time.monotonic();peer.call('POST',f'/jobs/{job}:cancel')
                        assert time.monotonic()-started<3
                    elif action=='task_cancel':
                        mutate('task_cancel');mutate('task_cancel');time.sleep(.2)
                        assert not state()['pump_done'] and not (api.root/'read.drained').exists()
                        peer.call('PUT',api.prefix+'/file',{'path':'probe.txt','content':'FORBIDDEN'},expect=409)
                    elif action=='renewal_failure':wait_file(api.root/'renewal.failed')
                    elif action in {'expired','revoked'}:
                        configure(stage,action,clock=100)
                        if action=='revoked':
                            assert mutate('revoke',peer)['revoked']
                            peer.call('PUT',api.prefix+'/file',{'path':'probe.txt','content':'AFTER_REVOKE'})
                        else:peer.call('PUT',api.prefix+'/file',{'path':'probe.txt','content':'FORBIDDEN'},expect=409)
                    elif action=='source_changed':
                        (case_root/'E-report/report.md').write_text('# Out of band source change\n',encoding='utf8')
                    elif action=='damaged_input':
                        artifact=next(path for path in original['manifest']['artifacts'] if path.endswith('.html'))
                        target=case_root/'.vibeflow/publications'/previous/'files'/artifact
                        target.write_bytes(target.read_bytes()+b'CORRUPTED')
                    elif action in {'missing_head','owner_changed','superseded'}:mutate(action,peer)
                    elif action=='producer_changed':mutate(action,peer,producer=producer)
                finally:(api.root/'blocked.continue').touch()
                deadline=time.monotonic()+45
                while time.monotonic()<deadline:
                    final=state()
                    if final['pump_done']:break
                    time.sleep(.05)
                else:raise TimeoutError(str(final))
                assert final['fence'] in {'released','revoked'} and final['stop']['verified'],final
                pending=action in {'task_cancel','renewal_failure','expired','revoked','proof_changed','owner_changed','commit_error','release_expired'}
                if action=='release_expired':assert (api.root/'terminal.expired').exists()
                if pending:
                    assert not final['finished'] and final['completion_error'] and final['status']=='running',final
                    assert final['active']==previous
                elif action=='cancel':assert final['status']=='cancelled' and final['active']==previous,final
                elif action in {'source_changed','damaged_input','missing_head','producer_changed'}:
                    assert final['status']=='failed' and final['active']==previous,final
                else:
                    assert final['status']=='succeeded' and final['finished'],final
                    assert final['active']==(previous if consumer or action=='superseded' else final['publication']),final
                    if action=='superseded':assert final['publication_status']=='superseded'
                configure()
                peer.call('PUT',api.prefix+'/file',{'path':'after.txt','content':'AFTER_DRAIN'})
                checks.append(stage+'_'+action)
                receipts.append({'check':checks[-1],'job_id':job,'case_id':api.case_id,'initial':initial,'final':final})
                print('PASS '+checks[-1],flush=True)
    result={'count':len(checks),'checks':checks,'receipts':receipts,'api_root':str(api.root),'peer_root':str(peer.root),
        'actual_native':['registered Pandoc','Windows publication consumer'],
        'scope':'Final source/dependency validation; external writes and DB changes are explicit fault injections'}
    (api.root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='publication-source-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    results['sqlite']['quiescence']=await asyncio.to_thread(verify_quiescence)
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_pubsource_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_pubsource_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,target)
            results['postgresql']['quiescence']=await asyncio.to_thread(verify_quiescence,target)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('PUBLICATION_SOURCE_OK '+json.dumps({'evidence':str(report),'counts':{key:value['count'] for key,value in results.items()}}))


if __name__=='__main__':asyncio.run(main())
