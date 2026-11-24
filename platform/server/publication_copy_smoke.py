"""Real registered Pandoc bundles: prepare outside locks, fence the final commit."""
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
from job_launch_lease_smoke import PEER, settled
from app.services import process_identity_native as identity
from workflow_migration_smoke import alembic

CONTROLLED = '''import os
os.environ['VIBEFLOW_JOB_LEASE_SECONDS']='10'
os.environ['VIBEFLOW_JOB_HEARTBEAT_SECONDS']='2'
import asyncio,json,time
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import SessionLocal
from app.models import ArtifactPublication,ArtifactPublicationHead,Job,JobAttempt,User
from app.services import artifact_publication as pub,job_execution,jobrunner
from app.services.durable_events import lock_case_execution
root=Path(__file__).parent
beats={}
def control():return json.loads((root/'control.json').read_text())
def blocked(stage):return control()['stage']==stage and not (root/'blocked.once').exists()
def enter():
 (root/'blocked.once').touch();(root/'blocked.ready').touch()
def pause():
 enter()
 try:
  deadline=time.monotonic()+60
  while not (root/'blocked.continue').exists() and time.monotonic()<deadline:time.sleep(.02)
  if not (root/'blocked.continue').exists():raise TimeoutError('Fixture publication copy barrier')
  if control()['action']=='copy_error':raise OSError('Injected copy/fsync failure')
 finally:(root/'copy.drained').touch()
copy=pub.prepare_bundle
def copying(prefix,row,run,records,descriptor):
 if blocked('copy'):
  (root/'context.json').write_text(json.dumps({'prefix':prefix,'publication':row.id,'run':str(run),'records':records}))
  pause()
 return copy(prefix,row,run,records,descriptor)
pub.prepare_bundle=copying
prepare=pub.prepare_bundle_for_finish
async def preparing(*args):
 result=await prepare(*args)
 if result is not None and blocked('prepared'):
  (root/'context.json').write_text(json.dumps({'prefix':result.prefix,'publication':result.publication.id,
    'records':result.records,'workspace':json.loads(result.input_json)['workspace']}))
  enter()
  deadline=time.monotonic()+60
  while not (root/'blocked.continue').exists() and time.monotonic()<deadline:await asyncio.sleep(.02)
  if not (root/'blocked.continue').exists():raise TimeoutError('Fixture prepared bundle barrier')
 return result
pub.prepare_bundle_for_finish=preparing
heartbeat=job_execution.heartbeat
async def beating(owner):
 beats[owner.job_id]=beats.get(owner.job_id,0)+1
 if control()['action']=='heartbeat_failure' and (root/'blocked.ready').exists():
  (root/'heartbeat.failed').touch();raise OSError('Injected heartbeat failure')
 return await heartbeat(owner)
job_execution.heartbeat=beating
final=pub.finalize
async def finalize(db,job,*args):
 result=await final(db,job,*args)
 if control()['action']=='commit_error' and (root/'blocked.ready').exists():db.info['fixture_commit_fail']=True
 return result
pub.finalize=finalize
commit=AsyncSession.commit
async def committing(db):
 if db.info.pop('fixture_commit_fail',False):raise OSError('Injected publication commit failure')
 return await commit(db)
AsyncSession.commit=committing
from app.main import app
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
@app.get('/api/v1/fixture/state/{job_id}')
async def state(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  row=await db.scalar(select(ArtifactPublication).where(ArtifactPublication.attempt_id==a.id))
  head=await db.get(ArtifactPublicationHead,(case.id,'report'))
  proc=jobrunner.get_proc(job_id)
  return {'status':job.status,'phase':a.phase,'heartbeat':a.heartbeat_at.isoformat(),'lease':a.lease_expires_at.isoformat(),
   'beats':beats.get(job_id,0),'worker':json.loads(a.worker_identity_json),'publication':row.id,
   'publication_status':row.status,'manifest_sha256':row.manifest_sha256,'active':head.active_publication_id,
   'pump_done':proc._pump_task.done(),'pump_cancelled':proc._pump_task.cancelled(),'finished':proc.finished,
   'completion_error':proc.completion_error,'stop':proc._owned_process.stop_result,
   'heartbeat_done':proc._heartbeat_task.done()}
@app.post('/api/v1/fixture/mutate/{job_id}')
async def mutate(job_id:int,data:dict,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  if data['kind']=='task_cancel':jobrunner.get_proc(job_id)._pump_task.cancel()
  else:
   await lock_case_execution(db,case.id)
   a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
   if data['kind']=='binding_changed':
    value=json.loads(a.output_manifest_json);value['fixture_changed']=True;a.output_manifest_json=json.dumps(value)
   elif data['kind']=='identity_changed':
    row=await db.scalar(select(ArtifactPublication).where(ArtifactPublication.attempt_id==a.id))
    row.immutable_path=row.immutable_path+'-wrong'
   await db.commit()
 return {'ok':True}
'''

MATRIX = [('copy',action) for action in ('normal','cancel','source','winner','output','copy_error','task_cancel','heartbeat_failure')]
MATRIX += [('prepared',action) for action in ('cancel','bundle_changed','manifest_changed','input_changed','binding_changed','identity_changed','commit_error')]


def done(api, job_id):
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        row=api.call('GET',f'/fixture/state/{job_id}')
        if row['pump_done'] and row['heartbeat_done']:return row
        time.sleep(.05)
    raise TimeoutError(str(row))


def verify(database_url=None, *, name='publication-copy', controlled=CONTROLLED, matrix=MATRIX):
    api=IsolatedServer(name,app='copy_fixture:app',database_url=database_url)
    (api.root/'copy_fixture.py').write_text(controlled,encoding='utf8')
    (api.root/'control.json').write_text(json.dumps({'stage':'none','action':'none'}),encoding='utf8')
    checks=[];receipts=[]
    with api:
        peer=IsolatedServer(name+'-peer',app='copy_peer:app',database_url=database_url)
        overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        (peer.root/'copy_peer.py').write_text(PEER.replace('OVERRIDES',repr(overrides)),encoding='utf8')
        with peer:
            for stage,action in matrix:
                for name in ('blocked.once','blocked.ready','blocked.continue','copy.drained','heartbeat.failed','context.json'):
                    (api.root/name).unlink(missing_ok=True)
                (api.root/'control.json').write_text(json.dumps({'stage':'none','action':'none'}),encoding='utf8')
                api.create_case(name+' '+stage+' '+action);peer.token=api.token
                case=api.call('GET',api.prefix)
                case_root=api.root/'files'/f"workspaces/{case['workspace_id']}/cases/{api.case_id}"
                api.put('E-report/report.md','# Initial actual Pandoc report\n')
                first=api.call('POST',api.prefix+'/report:build',{});settled(api,first['job_id'])
                old=first['publication_id']
                aliases={p:hashlib.sha256((case_root/p).read_bytes()).hexdigest() for p in ('E-report/build/report.html','E-report/build/report.docx')}
                api.put('E-report/report.md','# Updated actual Pandoc report\n')
                (api.root/'control.json').write_text(json.dumps({'stage':stage,'action':action}),encoding='utf8')
                submitted=api.call('POST',api.prefix+'/report:build',{});job_id=submitted['job_id']
                wait_file(api.root/'blocked.ready',seconds=90)
                before=api.call('GET',f'/fixture/state/{job_id}')
                assert before['status']=='running' and before['phase']=='finalizing' and before['active']==old,before
                assert before['stop']['verified'] and identity.probe(before['worker']['process'])['state']=='match'
                started=time.monotonic();current=peer.call('GET',api.prefix+'/report/results')
                timings={'reader_seconds':time.monotonic()-started}
                assert timings['reader_seconds']<3 and current['publication']['id']==old,(timings,current)
                context=json.loads((api.root/'context.json').read_text())
                during=None;winner=None
                reserved=stage in {'input_verify','bundle_verify','verified'}
                if action=='normal':
                    time.sleep(12);during=api.call('GET',f'/fixture/state/{job_id}')
                    assert during['beats']>=before['beats']+4 and during['heartbeat']!=before['heartbeat']
                    assert datetime.fromisoformat(during['lease']).replace(tzinfo=timezone.utc)>datetime.now(timezone.utc)
                elif action=='cancel':
                    started=time.monotonic();reply=peer.call('POST',f'/jobs/{job_id}:cancel')
                    timings['cancel_seconds']=time.monotonic()-started
                    assert reply['ok'] and timings['cancel_seconds']<3,(reply,timings)
                elif action=='source':
                    if reserved:
                        reply=peer.call('PUT',api.prefix+'/file',{'path':'E-report/report.md','content':'# Forbidden\n'},expect=409)
                        assert reply['detail']['code']=='validation_busy'
                        # Reserved writers cannot enter; retain a separate
                        # explicit external-file fault to test source rejection.
                        (case_root/'E-report/report.md').write_text('# External change during validation\n',encoding='utf8')
                    else:
                        peer.call('PUT',api.prefix+'/file',{'path':'E-report/report.md','content':'# Changed during copy\n'})
                elif action=='winner':
                    assert not reserved,'A reserved validator excludes new publication admission'
                    winner=peer.call('POST',api.prefix+'/report:build',{})
                    # Both native writers must drain before either validator
                    # can acquire the case. Release the first writer's fixture
                    # before waiting for the newer generation to finish.
                elif action=='writer_blocked':
                    assert reserved
                    count=len(peer.call('GET',api.prefix+'/jobs'))
                    reply=peer.call('POST',api.prefix+'/report:build',{},expect=409)
                    assert reply['detail']['code']=='validation_busy'
                    reply=peer.call('PUT',api.prefix+'/file',{'path':'E-report/report.md','content':'# Forbidden\n'},expect=409)
                    assert reply['detail']['code']=='validation_busy'
                    assert len(peer.call('GET',api.prefix+'/jobs'))==count
                elif action=='output':
                    target=Path(context['run'])/'E-report/build/report.html'
                    assert target.resolve().is_relative_to(api.root.resolve());target.write_bytes(b'changed after validation')
                elif action in {'bundle_changed','manifest_changed','input_changed'}:
                    if action=='input_changed':target=case_root/context['workspace']['inputs_path']/'E-report/report.md'
                    else:
                        target=case_root/'.vibeflow/publications'/context['publication']
                        target=target/('manifest.json' if action=='manifest_changed' else 'files/E-report/build/report.html')
                    assert target.resolve().is_relative_to(api.root.resolve());target.write_bytes(b'changed after copy')
                elif action in {'binding_changed','identity_changed'}:
                    api.call('POST',f'/fixture/mutate/{job_id}',{'kind':action})
                elif action=='task_cancel':
                    for _ in range(2):api.call('POST',f'/fixture/mutate/{job_id}',{'kind':action});time.sleep(.1)
                    during=api.call('GET',f'/fixture/state/{job_id}')
                    assert not during['pump_done'] and not (api.root/'copy.drained').exists()
                elif action=='heartbeat_failure':wait_file(api.root/'heartbeat.failed',seconds=8)
                (api.root/'blocked.continue').touch()
                if winner is not None:
                    settled(peer,winner['job_id'])
                    assert peer.call('GET',api.prefix+'/report/results')['publication']['id']==winner['publication_id']
                state=done(api,job_id);final=api.call('GET',f'/jobs/{job_id}')
                normal=action in {'normal','writer_blocked'}
                if action in {'task_cancel','heartbeat_failure','commit_error'} or (reserved and action=='binding_changed'):
                    assert final['status']=='running' and final['ended_at'] is None and state['completion_error'],(state,final)
                    assert state['phase']=='finalizing' and state['publication_status']=='prepared' and state['active']==old,state
                    assert not state['finished'] and state['pump_cancelled']==(action=='task_cancel')
                else:
                    target='succeeded' if normal or action=='winner' else 'cancelled' if action=='cancel' else 'failed'
                    assert final['status']==target and final['ended_at'] is not None,(state,final)
                    assert final['attempt']['process_stop']['verified'] and state['finished']
                    assert state['publication_status']==('superseded' if action=='winner' else 'committed' if normal else 'failed')
                    if action=='source':assert final['attempt']['reason_code']=='input_stale'
                expected=submitted['publication_id'] if normal else winner['publication_id'] if winner else old
                assert state['active']==expected,state
                assert peer.call('GET',api.prefix+'/report/results')['publication']['id']==expected
                if not normal and action!='winner':
                    assert {p:hashlib.sha256((case_root/p).read_bytes()).hexdigest() for p in aliases}==aliases
                if stage in {'copy','input_verify','bundle_verify'}:assert (api.root/'copy.drained').exists()
                receipts.append({'stage':stage,'action':action,'before':before,'during':during,'state':state,'final':final,'timings':timings,
                                 'old_publication':old,'winner':winner,'publication_directory_exists':(case_root/'.vibeflow/publications'/submitted['publication_id']).exists()})
                checks.append(stage+'_'+action)
                (api.root/'copy-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
                print('PASS '+checks[-1],flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),'peer_evidence':str(peer.root),
            'actual_native_report':True,'developer_database_touched':False,'database_isolated':True}
    (api.root/'copy-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='publication-copy-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_pubcopy_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_pubcopy_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
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
    print('PUBLICATION_COPY_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
