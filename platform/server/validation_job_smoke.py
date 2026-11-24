"""Actual Job writers, callbacks and cross-API read-validation admission.

Real Windows owned scopes are used for every writer. Synthetic never-started
validators are inherited from the reservation protocol fixture, not presented
as evidence of native exit. No development DB or running API is modified.
"""
import argparse
import asyncio
import json
from pathlib import Path
import re
import tempfile
import time
import uuid

from sqlalchemy import text
from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from case_validation_smoke import FIXTURE as VALIDATION
from job_release_smoke import wait_file
from professional_launch_smoke import settled
from workflow_migration_smoke import alembic, engine_for

CONTROLLED = VALIDATION + '''
import asyncio,json,sys
from pathlib import Path
from app.services import jobrunner,job_write_checkpoint as checkpoint,native_child_records
from app.models import JobSubmissionRequest
root=Path(__file__).parent
modes={};saved={}
async def barrier(job_id,stage):
 (root/(str(job_id)+'.'+stage+'.ready')).touch()
 for _ in range(3000):
  if (root/(str(job_id)+'.'+stage+'.continue')).exists():return
  await asyncio.sleep(.02)
 raise TimeoutError('Job validation fixture '+stage)
record=checkpoint.record_drained
async def recording(owner,*args,**kwargs):
 mode=modes.get(owner.job_id)
 if mode=='before':await barrier(owner.job_id,'before')
 result=await record(owner,*args,**kwargs)
 if mode=='after':
  assert result;await barrier(owner.job_id,'after')
 return result
checkpoint.record_drained=recording
from app.services import attempt_workspace
workspace=attempt_workspace.runtime_workspace
async def runtime_workspace(owner,**kwargs):
 if modes.get(owner.job_id)=='prepare_fail':
  from app.services.case_write import CaseWriteConflict
  raise CaseWriteConflict('input_stale','Injected pre-spawn preparation failure')
 return await workspace(owner,**kwargs)
attempt_workspace.runtime_workspace=runtime_workspace

@app.post('/api/v1/fixture/job')
async def create_job(case_id:int,mode:str='normal',queued:bool=False,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  nonce=uuid.uuid4().hex
  spec={'command':'fixture native '+nonce,'case_prefix':case.s3_prefix,'runtime':'windows',
   'argv':[sys.executable,'-I','-S',str(root/'payload.py'),nonce,'wait' if mode=='native' else 'exit'],
   'timeout_seconds':60,'max_log_bytes':1048576,'evidence_run_dir':None}
  job,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=None,
   digest=job_execution.request_hash(spec),job_type='shell',command=spec['command'],spec=spec)
  await db.commit();modes[job.id]=mode
 if not queued:await launch(job.id)
 return {'id':job.id,'nonce':nonce}

async def launch(job_id):
 async with SessionLocal() as db:
  row=await db.scalar(select(JobSubmissionRequest).where(JobSubmissionRequest.job_id==job_id))
  spec=json.loads(row.execution_spec_json)
 async def hook(*_):
  if modes.get(job_id)=='hook':
   await barrier(job_id,'hook')
   storage.put_text(spec['case_prefix'],'hook.txt','HOOK_DRAINED')
 return await jobrunner.start_job(job_id,spec['command'],spec['case_prefix'],argv=spec['argv'],
  timeout_seconds=60,max_log_bytes=1048576,on_finish=hook)

@app.post('/api/v1/fixture/job/{job_id}/launch')
async def launch_job(job_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
 await launch(job_id);return {'ok':True}

@app.get('/api/v1/fixture/job/{job_id}')
async def state(job_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  return {'status':job.status,'phase':a.phase,'claimed':a.claim_token is not None,
   'checkpoint':json.loads(a.write_checkpoint_json),'children':json.loads(a.child_processes_json),
   'identity':json.loads(a.process_identity_json),'heartbeat':str(a.heartbeat_at)}

@app.post('/api/v1/fixture/job/{job_id}/mutate')
async def mutate(job_id:int,kind:str,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  if kind=='restore':
   for key,value in saved[job_id].items():setattr(a,key,value)
  else:
   saved[job_id]={key:getattr(a,key) for key in ('claim_token','write_checkpoint_json','child_processes_json','lease_expires_at')}
   if kind=='missing':a.write_checkpoint_json='{}'
   elif kind=='malformed':a.write_checkpoint_json='[]'
   elif kind=='claim':a.claim_token=uuid.uuid4().hex
   elif kind=='children':a.child_processes_json='[]'
   elif kind=='expired':a.lease_expires_at=job_execution.now()-timedelta(seconds=600)
   elif kind=='unverified':
    value=json.loads(a.write_checkpoint_json);value['stop']['verified']=False;a.write_checkpoint_json=json.dumps(value)
   else:raise ValueError(kind)
  await db.commit();return {'ok':True}

@app.post('/api/v1/fixture/job/{job_id}/child')
async def child(job_id:int,action:str='reserve',user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  owner=await load_owner(db,job_id);a=await db.get(JobAttempt,owner.attempt_id)
  nonce=json.loads(a.process_identity_json)['launch_nonce']
 identifier=str(uuid.uuid4())
 async def send():raise AssertionError('Closed native child was released')
 if action=='reserve':await native_child_records.reserve(owner,nonce,identifier,'pandoc_version','finalizer',{})
 elif action=='register':await native_child_records.register(owner,nonce,identifier,{},None)
 elif action=='release':await native_child_records.release(owner,nonce,identifier,None,send)
 else:raise ValueError(action)
 return {'ok':True}

@app.post('/api/v1/fixture/job/{job_id}/repeat_finish')
async def repeat_finish(job_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  owner=await load_owner(db,job_id);a=await db.get(JobAttempt,owner.attempt_id)
  stop=json.loads(a.write_checkpoint_json)['stop']
 await job_execution.finish(owner,'succeeded',0,process_stop=stop,content_drained=True)
 return {'ok':True}
'''


def verify(database_url=None):
    api = IsolatedServer('validation-job', app='fixture:app', database_url=database_url)
    if database_url is None:
        alembic('sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix(),api.root,'upgrade','head')
    (api.root/'fixture.py').write_text(CONTROLLED, encoding='utf8')
    (api.root/'payload.py').write_text('from pathlib import Path\nimport sys,time\nroot=Path('+repr(str(api.root))+')\nnonce=sys.argv[1]\n(root/(nonce+".native.ready")).touch()\nif sys.argv[2]=="wait":\n for _ in range(2500):\n  if (root/(nonce+".native.continue")).exists():break\n  time.sleep(.02)\n else:raise TimeoutError("Native fixture")\nPath(nonce+".txt").write_text("NATIVE_DRAINED")\n',encoding='utf8')
    checks=[];receipts=[]
    with api:
        peer=IsolatedServer('validation-job-peer',app='peer:app',database_url=database_url)
        overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        (peer.root/'peer.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+VALIDATION,encoding='utf8')
        with peer:
            api.create_case('Job admission')
            peer.token=api.token
            def validator():return peer.call('POST',f'/fixture/owner?case_id={api.case_id}')['job_id']
            def op(job,action,expect=200):
                return peer.call('POST',f'/fixture/op?job_id={job}&action={action}',expect=expect)
            def create(mode='normal',queued=False):
                return api.call('POST',f'/fixture/job?case_id={api.case_id}&mode={mode}&queued={str(queued).lower()}')
            def state(job):return api.call('GET',f'/fixture/job/{job}')
            def ready(job,stage):wait_file(api.root/f'{job}.{stage}.ready')
            def resume(job,stage):(api.root/f'{job}.{stage}.continue').touch()
            def busy(owner):assert op(owner,'reserve',409)['detail']['code']=='validation_job_busy'

            owner=validator();queued=create(queued=True);job=queued['id']
            op(owner,'reserve');checks.append('unclaimed_queue_does_not_block_reservation')
            response=api.call('POST',f'/fixture/job/{job}/launch',expect=409)
            assert response['detail']['code']=='validation_busy' and response['detail']['job_id']==job
            assert not state(job)['claimed'] and not (api.root/(queued['nonce']+'.native.ready')).exists()
            assert not list((api.root/'files').rglob(f'job-{job}.log'))
            checks.append('held_reservation_rejects_claim_before_native_and_log_creation')
            peer.call('POST','/fixture/clock?seconds=4000')
            assert op(owner,'current',409)['detail']['code']=='validation_stale'
            api.call('POST',f'/fixture/job/{job}/launch',expect=409)
            checks.append('expired_held_reservation_still_rejects_claim')
            assert op(owner,'revoke')['revoked'];peer.call('POST','/fixture/clock?seconds=0')
            api.call('POST',f'/fixture/job/{job}/launch');final=settled(api,job)
            assert final['status']=='succeeded' and state(job)['checkpoint']['state']=='stopped'
            checks.append('committed_revoke_allows_same_queued_job_once')
            op(owner,'reserve');op(owner,'release')

            api.create_case('Known pre-spawn failure');peer.token=api.token;owner=validator()
            never=create('prepare_fail',queued=True);job=never['id']
            api.call('POST',f'/fixture/job/{job}/launch',expect=409)
            assert state(job)['status']=='failed' and state(job)['checkpoint']['state']=='not_started'
            assert not (api.root/(never['nonce']+'.native.ready')).exists()
            op(owner,'reserve');op(owner,'release')
            checks.append('drained_pre_spawn_failure_has_positive_never_started_receipt')

            for mode in ('native','hook','before'):
                api.create_case('Busy '+mode);peer.token=api.token
                owner=validator();record=create(mode);job=record['id']
                if mode=='native':wait_file(api.root/(record['nonce']+'.native.ready'))
                else:ready(job,mode)
                try:
                    busy(owner);assert state(job)['checkpoint']=={}
                    checks.append(mode+'_unfinished_writer_blocks_peer_reservation')
                    if mode=='native':
                        api.call('POST',f'/fixture/job/{job}/mutate?kind=expired');busy(owner)
                        api.call('POST',f'/fixture/job/{job}/mutate?kind=restore')
                        checks.append('expired_running_job_is_not_assumed_stopped')
                finally:
                    if mode=='native':(api.root/(record['nonce']+'.native.continue')).touch()
                    else:resume(job,mode)
                final=settled(api,job);assert final['attempt']['process_stop']['verified']
                assert state(job)['checkpoint']['state']=='stopped'
                op(owner,'reserve');op(owner,'release')
                checks.append(mode+'_drained_job_allows_reservation')
                receipts.append({'job':job,'mode':mode,'stop':final['attempt']['process_stop']})

            api.create_case('Two finalizers');peer.token=api.token;owner=validator()
            jobs=[create('after')['id'] for _ in range(2)]
            try:
                for job in jobs:ready(job,'after')
                assert all(state(job)['phase']=='finalizing' for job in jobs)
                # The validating Job itself also needs a drain proof; no own-Job exemption.
                op(jobs[0],'reserve');op(jobs[0],'release')
                op(jobs[1],'reserve');op(jobs[1],'release')
                checks.append('two_drained_finalizers_can_validate_without_mutual_wait')
                for job in jobs:
                    for action in ('reserve','register','release'):
                        assert api.call('POST',f'/fixture/job/{job}/child?action={action}',expect=409)['detail']['code']=='state_conflict'
                    assert state(job)['children']=={}
                checks.append('checkpoint_closes_child_reserve_register_and_release')
                assert api.call('POST',f'/fixture/job/{jobs[0]}/repeat_finish',expect=409)['detail']['code']=='recovery_required'
                checks.append('drained_finalizer_cannot_restart_publication_preparation')
                for kind in ('missing','malformed','claim','children','unverified'):
                    api.call('POST',f'/fixture/job/{jobs[0]}/mutate?kind={kind}')
                    busy(owner)
                    api.call('POST',f'/fixture/job/{jobs[0]}/mutate?kind=restore')
                    checks.append(kind+'_checkpoint_cannot_authorize_reservation')
                op(owner,'reserve')
                op(jobs[0],'heartbeat');checks.append('heartbeat_continues_during_read_reservation')
                peer.call('POST',f'/jobs/{jobs[0]}:cancel')
                checks.append('cross_api_cancel_commits_during_read_reservation')
                assert op(owner,'current')['ok'];op(owner,'release')
            finally:
                for job in jobs:resume(job,'after')
            for i,job in enumerate(jobs):
                final=settled(api,job)
                assert final['status']==('cancelled' if i==0 else 'succeeded'),final
                assert final['attempt']['process_stop']['verified']
            checks.append('cancelled_and_successful_finalizers_keep_verified_stop')
            api.call('POST',f'/fixture/job/{jobs[1]}/mutate?kind=missing');busy(owner)
            api.call('POST',f'/fixture/job/{jobs[1]}/mutate?kind=restore')
            checks.append('successful_terminal_status_without_drain_proof_still_blocks')
            op(owner,'reserve');op(owner,'release')
            checks.append('terminal_drain_receipts_remain_usable')
        target=api.env['VIBEFLOW_DATABASE_URL']
    result={'count':len(checks),'checks':checks,'receipts':receipts,'api_root':str(api.root),
        'peer_root':str(peer.root),'scope':'Job admission/drain protocol; real publication integration still pending'}
    (api.root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result,target


async def migration_history(url,root):
    engine=engine_for(url)
    try:
        async with engine.connect() as conn:
            before=(await conn.execute(text('SELECT id,write_checkpoint_json FROM job_attempts ORDER BY id'))).all()
            assert any(row[1]!='{}' for row in before)
        try:await asyncio.to_thread(alembic,url,root,'downgrade','5f3eb0a4d976')
        except AssertionError:pass
        else:raise AssertionError('Downgrade discarded Job write-drain history')
        async with engine.begin() as conn:
            # Empty newer additive tables may have been rolled back already on
            # SQLite; transactional PostgreSQL can retain the newer head.
            from workflow_migration_smoke import HEAD
            assert (await conn.execute(text('SELECT version_num FROM alembic_version'))).scalar_one() in {'6a4fc1b5e087',HEAD}
            assert (await conn.execute(text('SELECT id,write_checkpoint_json FROM job_attempts ORDER BY id'))).all()==before
            await conn.execute(text("UPDATE job_attempts SET write_checkpoint_json='{}'"))
        await engine.dispose()
        await asyncio.to_thread(alembic,url,root,'downgrade','5f3eb0a4d976')
        await asyncio.to_thread(alembic,url,root,'upgrade','head')
        await asyncio.to_thread(alembic,url,root,'check')
        async with engine.connect() as conn:
            rows=(await conn.execute(text('SELECT id,write_checkpoint_json FROM job_attempts ORDER BY id'))).all()
            assert [r[0] for r in rows]==[r[0] for r in before] and all(r[1]=='{}' for r in rows)
        return {'history_loss_rejected':True,'populated_roundtrip':True,'metadata_diff':0}
    finally:await engine.dispose()


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='validation-job-evidence-',dir=SERVER.parents[1]/'tmp'))
    result,url=await asyncio.to_thread(verify)
    result['migration']=await migration_history(url,report);results={'sqlite':result}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_jobdrain_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_jobdrain_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            result,_=await asyncio.to_thread(verify,target)
            result['migration']=await migration_history(target,report);results['postgresql']=result
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('VALIDATION_JOB_OK '+json.dumps({'evidence':str(report),'counts':{key:value['count'] for key,value in results.items()}}))


if __name__=='__main__':asyncio.run(main())
