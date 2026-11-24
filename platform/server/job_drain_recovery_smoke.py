"""Actual completed producer, dead-owner history and guarded drain recovery."""
import argparse,asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time
import tempfile,uuid,re
from sqlalchemy.engine import make_url
from workflow_migration_smoke import alembic,SERVER

from publication_source_smoke import CONTROLLED,IsolatedServer,settled,wait_file

EXTRA='''
from app.models import DurableEvent
from app.services import job_drain_recovery as recovery,job_recovery,job_write_checkpoint
from app.services import job_worker_identity,process_stop
from app.services import job_execution
from app.services import process_identity_native as identity
if os.environ.get('VIBEFLOW_TEST_DEFER_RECOVERY','1')=='1':
 async def defer_recovery():return {'fixture':'explicit_reconciliation'}
 job_recovery.reconcile_startup=defer_recovery
observe=recovery.observe_owner
async def observing(owner):
 result=await observe(owner)
 if (root/'observe.hold').exists():
  (root/'observe.ready').touch()
  deadline=time.monotonic()+60
  while not (root/'observe.release').exists():
   if time.monotonic()>deadline:raise TimeoutError('Recovery observation barrier')
   await asyncio.sleep(.02)
 return result
recovery.observe_owner=observing
event=job_execution.attempt_event
async def recording(db,job,attempt,kind,payload):
 result=await event(db,job,attempt,kind,payload)
 if kind=='attempt.content_drain_recovered' and (root/'commit.fault').exists():
  db.info['recovery_commit_fault']=(root/'commit.fault').read_text()
 return result
job_execution.attempt_event=recording
recovery_commit=AsyncSession.commit
async def commit_recovery(db):
 fault=db.info.pop('recovery_commit_fault',None)
 if fault=='before':raise OSError('Injected recovery transaction rollback')
 result=await recovery_commit(db)
 if fault=='after':raise OSError('Injected lost recovery commit acknowledgement')
 return result
AsyncSession.commit=commit_recovery
reservations={}
@app.post('/api/v1/fixture/fence/{job_id}/{action}')
async def validation_fence(job_id:int,action:str,user=Depends(get_current_user)):
 from app.services import case_validation,job_write_checkpoint
 if action=='reserve':
  async with SessionLocal() as db:
   old=await db.get(Job,job_id);case=await get_owned_case(old.case_id,user,db)
   spec={'command':'Synthetic never-started validator','runtime':'windows','case_prefix':case.s3_prefix,
    'argv':None,'timeout_seconds':60,'max_log_bytes':1024,'evidence_run_dir':None}
   job,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=None,
    digest=job_execution.request_hash(spec),job_type='shell',command=spec['command'],spec=spec)
   owner=await job_execution.claim(db,job.id,spec);await db.commit()
  assert await job_write_checkpoint.record_drained(owner,None,not_started=True)
  async with SessionLocal() as db:
   job,attempt=await job_execution._owned(db,owner)
   job.status='running';attempt.phase='finalizing';await db.commit()
  async with SessionLocal() as db:
   reserved=await case_validation.reserve_locked(db,owner,lease_seconds=60);await db.commit()
  reservations[job_id]=(owner,reserved)
 else:
  owner,reserved=reservations.pop(job_id)
  async with SessionLocal() as db:
   await case_validation.release_locked(db,reserved);await db.commit()
  async with SessionLocal() as db:
   job,attempt=await job_execution._owned(db,owner)
   job.status='cancelled';attempt.phase='cancelled';await db.commit()
 return {'ok':True}
@app.get('/api/v1/fixture/history/{job_id}')
async def history(job_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==a.id,
   DurableEvent.type=='attempt.content_drain_recovered')))
  try:job_write_checkpoint.validate(case,job,a);valid=True
  except (ValueError,TypeError,KeyError,AttributeError):valid=False
  return {'status':job.status,'phase':a.phase,'checkpoint':json.loads(a.write_checkpoint_json),
   'worker_state':job_worker_identity.inspect(job,a),'stop':process_stop.public(job,a),'valid':valid,
   'events':len(events),'fields':{key:getattr(a,key) for key in ('phase','claim_token','worker_identity_json',
    'process_identity_json','child_processes_json','input_manifest_json','output_manifest_json','publication_json')},
   'case_prefix':case.s3_prefix,'observer':identity.observe(os.getpid())}
@app.post('/api/v1/fixture/edit/{job_id}')
async def edit(job_id:int,body:dict,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  await lock_case_execution(db,case.id)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  allowed={'phase','claim_token','worker_identity_json','process_identity_json','child_processes_json',
   'input_manifest_json','output_manifest_json','publication_json','write_checkpoint_json'}
  for key,value in body.items():
   if key=='case_prefix':case.s3_prefix=value
   else:
    assert key in allowed;setattr(a,key,value)
  await db.commit()
 return {'ok':True}
@app.post('/api/v1/fixture/recover/{job_id}')
async def recover(job_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
 return {'result':await job_recovery.reconcile_job(job_id)}
'''


def run(database_url=None,*,wsl=False):
    first=IsolatedServer('drain-recovery',app='fixture:app',database_url=database_url)
    (first.root/'fixture.py').write_text(CONTROLLED+EXTRA,encoding='utf8')
    (first.root/'control.json').write_text(json.dumps({'stage':'','action':'','clock':0}),encoding='utf8')
    checks=[]
    def passed(name):checks.append(name);print('PASS '+name,flush=True)
    with first:
        first.create_case('Recover actual stopped publisher')
        prefix=first.prefix
        first.put('E-report/report.md','# Actual original producer\n')
        original=first.call('POST',prefix+'/report:build',{})
        job=original['job_id'];settled(first,job)
        wsl_job=None
        if wsl:
            submitted=first.call('POST',prefix+'/jobs',{'command':'python3 -c "print(731)"','runtime':'wsl',
                'input_paths':['E-report/report.md']})
            wsl_job=submitted['id'];settled(first,wsl_job)
            first.call('POST',f'/fixture/edit/{wsl_job}',{'write_checkpoint_json':'{}'})
        def history(client):return client.call('GET',f'/fixture/history/{job}')
        def edit(client,data):return client.call('POST',f'/fixture/edit/{job}',data)
        def recover(client):return client.call('POST',f'/fixture/recover/{job}')['result']
        initial=history(first)
        assert initial['valid'] and initial['stop']['verified'] and initial['worker_state']['state']=='match'
        edit(first,{'write_checkpoint_json':'{}'})
        assert recover(first)=='owner_live' and history(first)['checkpoint']=={}
        passed('live_original_owner_is_not_recovered_even_after_terminal_success')
        first.proc.kill();first.proc.wait(timeout=10)
        overrides={key:first.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        overrides['VIBEFLOW_SOURCE_CONTROL']=str(first.root)
        def peer(label,*,automatic=False):
            server=IsolatedServer(label,app='fixture:app',database_url=database_url)
            env={**overrides,'VIBEFLOW_TEST_DEFER_RECOVERY':'0' if automatic else '1'}
            (server.root/'fixture.py').write_text('import os\nos.environ.update('+repr(env)+')\n'+CONTROLLED+EXTRA,encoding='utf8')
            server.token=first.token
            return server
        with peer('drain-recovery-peer') as api,peer('drain-recovery-control') as control:
            base=history(api)
            assert base['worker_state']['state']=='exited' and base['stop']==initial['stop']
            saved=base['fields']
            def altered(field,mutate):
                value=json.loads(saved[field]);mutate(value);return {field:json.dumps(value)}
            faults=[
                ('changed_claim',{'claim_token':'e'*32}),
                ('wrong_phase',{'phase':'running'}),
                ('wrong_case_path',{'case_prefix':base['case_prefix']+'-changed'}),
                ('invalid_worker',{'worker_identity_json':'{}'}),
                ('missing_parent_stop',altered('output_manifest_json',lambda v:v.pop('process_stop'))),
                ('unverified_parent_stop',altered('output_manifest_json',lambda v:v['process_stop'].update(verified=False))),
                ('missing_membership_receipt',altered('output_manifest_json',lambda v:v['process_stop'].pop('scope_observation'))),
                ('pending_aliases',altered('output_manifest_json',lambda v:v['completion'].update(state='pending',settled_at=None))),
                ('missing_child_journal',{'child_processes_json':'{}'}),
                ('unverified_child_stop',altered('child_processes_json',lambda v:next(iter(v['children'].values()))['stop'].update(verified=False))),
            ]
            for name,fault in faults:
                edit(api,fault)
                assert recover(api)=='drain_history_unverified',name
                assert history(api)['checkpoint']=={} and history(api)['events']==0,name
                edit(api,{key:base['case_prefix'] if key=='case_prefix' else saved[key] for key in fault})
                passed(name+'_cannot_create_recovery_authority')
            for name,process in (
                ('live_other_owner',base['observer']),
                ('foreign_owner',{**json.loads(saved['worker_identity_json'])['process'],'host_id':'f'*64}),
                ('mismatched_owner',{**base['observer'],'creation_time_100ns':str(int(base['observer']['creation_time_100ns'])+1)})):
                value=json.loads(saved['worker_identity_json']);value['process']=process
                edit(api,{'worker_identity_json':json.dumps(value)})
                # New child launch intent binds the original worker document.
                # Replacing it now invalidates history before an OS probe.
                assert recover(api)=='drain_history_unverified'
                assert history(api)['checkpoint']=={}
                edit(api,{'worker_identity_json':saved['worker_identity_json']})
                passed(name+'_changed_worker_binding_is_rejected_without_signalling')
            for field,value in (('claim_token','a'*32),('case_prefix',base['case_prefix']+'-race'),
                                ('output_manifest_json',json.dumps({**json.loads(saved['output_manifest_json']),'fixture_changed':True}))):
                for marker in ('observe.ready','observe.release'): (first.root/marker).unlink(missing_ok=True)
                (first.root/'observe.hold').touch()
                try:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pending=pool.submit(recover,api)
                        wait_file(first.root/'observe.ready')
                        edit(control,{field:value})
                        (first.root/'observe.release').touch()
                        assert pending.result(timeout=15)=='drain_recovery_changed'
                finally:
                    (first.root/'observe.release').touch();(first.root/'observe.hold').unlink(missing_ok=True)
                assert history(api)['checkpoint']=={}
                edit(api,{field:base['case_prefix'] if field=='case_prefix' else saved[field]})
                passed('recovery_rechecks_'+field+'_after_unlocked_owner_observation')
            with ThreadPoolExecutor(max_workers=2) as pool:
                replies=[future.result(timeout=20) for future in [pool.submit(recover,api),pool.submit(recover,control)]]
            assert sorted(replies)==['content_drain_recovered','content_drained'],replies
            recovered=history(api)
            assert recovered['valid'] and recovered['checkpoint']['schema']=='vibeflow.job-write-checkpoint.v2'
            assert recovered['events']==1 and recovered['stop']==initial['stop']
            assert recovered['fields']==saved and recovered['status']=='succeeded'
            assert recover(api)=='content_drained' and history(api)['events']==1
            passed('two_APIs_recover_once_preserving_original_stop_and_terminal_bytes')
            checkpoint=recovered['checkpoint']
            broken=json.loads(json.dumps(checkpoint));broken['recovery']['snapshot_sha256']='0'*64
            edit(api,{'write_checkpoint_json':json.dumps(broken)})
            assert not history(api)['valid'] and recover(api)=='drain_proof_unverified'
            edit(api,{'write_checkpoint_json':json.dumps(checkpoint)})
            passed('changed_recovery_certificate_cannot_grant_write_drain_permission')
            if wsl_job is not None:
                assert api.call('POST',f'/fixture/recover/{wsl_job}')['result']=='content_drain_recovered'
                native=api.call('GET',f'/fixture/history/{wsl_job}')
                assert native['valid'] and native['stop']['native_state']=='exited'
                passed('actual_WSL_parent_cleanup_can_recover_without_new_native_execution')
            api.call('PUT',prefix+'/file',{'path':'E-report/report.md','content':'# Recovered case accepts a new real report\n'})
            target=api.call('POST',prefix+'/report:build',{});settled(api,target['job_id'])
            assert api.call('GET',prefix+'/report/results')['publication']['id']==target['publication_id']
            passed('real_new_publication_succeeds_after_known_history_recovery')
            api.call('POST',f'/fixture/fence/{job}/reserve')
            edit(api,{'write_checkpoint_json':'{}'})
            assert recover(control)=='validation_busy' and history(api)['checkpoint']=={}
            api.call('POST',f'/fixture/fence/{job}/release')
            assert recover(api)=='content_drain_recovered'
            passed('held_content_reservation_blocks_recovery_until_exact_release')
            for fault in ('before','after'):
                edit(api,{'write_checkpoint_json':'{}'})
                previous=history(api)['events']
                (first.root/'commit.fault').write_text(fault)
                try:api.call('POST',f'/fixture/recover/{job}',expect=500,raw=True)
                finally:(first.root/'commit.fault').unlink()
                observed=history(control)
                if fault=='before':
                    assert observed['checkpoint']=={} and observed['events']==previous
                    assert recover(control)=='content_drain_recovered'
                else:
                    assert observed['valid'] and observed['events']==previous+1
                    assert recover(control)=='content_drained' and history(api)['events']==previous+1
                passed('recovery_'+fault+'_commit_failure_preserves_exact_transaction_outcome')
            edit(api,{'write_checkpoint_json':'{}'})
        with peer('drain-recovery-startup',automatic=True) as restarted:
            state=history(restarted)
            assert state['valid'] and state['checkpoint']['schema']=='vibeflow.job-write-checkpoint.v2'
            assert state['stop']==initial['stop']
            assert recover(restarted)=='content_drained'
            passed('API_startup_includes_terminal_history_with_missing_drain_metadata')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(first.root),
        'actual_payload':'Pandoc parent plus actual registered native children',
        'fixture_fault':'Only checkpoint removed initially; negative metadata faults restored before recovery',
        'developer_database_touched':False}
    (first.root/'recovery-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--postgres-config',type=Path);parser.add_argument('--wsl',action='store_true')
    args=parser.parse_args();report=Path(tempfile.mkdtemp(prefix='drain-recovery-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(run,wsl=args.wsl)}
    if args.postgres or args.postgres_config:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=args.postgres_config or SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_drains_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_drains_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(run,target,wsl=args.wsl)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('JOB_DRAIN_RECOVERY_OK '+json.dumps({'evidence':str(report),'counts':{key:value['count'] for key,value in results.items()}}))
if __name__=='__main__':asyncio.run(main())
