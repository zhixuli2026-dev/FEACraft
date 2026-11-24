"""Actual Windows Pandoc child preparation, worker death and fresh report recovery."""
import asyncio,hashlib,io,json,shlex,subprocess,sys,time,zipfile
from pathlib import Path
from urllib.parse import quote
import asyncpg
from app.services import process_identity_native as identity
import workflow_batch_redis_support as workers_module
import live_llm_remote_worker_acceptance as baseline

FAULT='''
from app.services import child_launch_registration
original_child_prepared=child_launch_registration.prepared
async def crash_after_child_prepared(owner,parent_nonce,identifier,process):
 await original_child_prepared(owner,parent_nonce,identifier,process)
 root=Path(FAULT_ROOT)
 if (root/'child-armed').exists():
  (root/'child-armed').unlink()
  target=root/'child-prepared.json';staged=target.with_suffix('.staged')
  staged.write_text(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id,'child_id':identifier,
   'worker':identity.observe(os.getpid())}))
  staged.replace(target)
  while not (root/'child-crash-now').exists():await asyncio.sleep(.02)
  os._exit(17)
child_launch_registration.prepared=crash_after_child_prepared
'''

CHECK='''
import asyncio,copy,json,sys
from sqlalchemy import select
from app.db import SessionLocal,engine
from app.models import Job,JobAttempt,Case
from app.services.job_scope_recovery import validate_certificate
from app.services.job_write_checkpoint import validate
async def main():
 async with SessionLocal() as db:
  job=await db.get(Job,int(sys.argv[1]))
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
  case=await db.get(Case,job.case_id)
  original=json.loads(attempt.write_checkpoint_json)
  validate(case,job,attempt);validate_certificate(case,job,attempt,original)
  child=next(iter(original['recovery']['child_preparations']))
  edits=[('invented_stop',('stop',),{}),('missing_guardian',('recovery','guardians'),[]),
   ('live_owner',('recovery','worker_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('changed_snapshot',('recovery','snapshot_sha256'),'0'*64),
   ('missing_preparations',('recovery','child_preparations'),{}),
   ('missing_child_history',('recovery','child_preparations',child),[]),
   ('missing_prepared',('recovery','child_preparations',child),original['recovery']['child_preparations'][child][:-1]),
   ('reordered_history',('recovery','child_preparations',child),list(reversed(original['recovery']['child_preparations'][child]))),
   ('changed_reservation',('recovery','child_preparations',child,0,1,'purpose'),'pandoc_wrong'),
   ('changed_claim',('recovery','child_preparations',child,1,1,'claim_sha256'),'0'*64),
   ('changed_prepared_birth',('recovery','child_preparations',child,-1,1,'identity','host','pid'),1),
   ('schema_downgrade',('recovery','schema'),'vibeflow.job-scope-recovery.v1')]
  refused=[]
  if attempt.phase=='cancelled':
   assert job.status=='cancelled' and attempt.reason_code=='cancel_requested'
   assert original['recovery']['cancel_requested_at']==attempt.cancel_requested_at.isoformat()
   edits += [('missing_cancel_request',('recovery','cancel_requested_at'),None),
    ('changed_cancel_request',('recovery','cancel_requested_at'),'2000-01-01T00:00:00')]
  for name,path,value in edits:
   altered=copy.deepcopy(original);target=altered
   for key in path[:-1]:target=target[key]
   target[path[-1]]=value
   try:validate_certificate(case,job,attempt,altered)
   except (ValueError,KeyError,TypeError,AttributeError):refused.append(name)
   else:raise AssertionError('Corrupted child recovery certificate accepted: '+name)
  assert json.loads(attempt.write_checkpoint_json)==original
  print(json.dumps({'original_valid':True,'refused':refused,'database_modified':False}))
 await engine.dispose()
asyncio.run(main())
'''


async def scenario(api,worker,selected,*,cancel_after_exit=False,filename='child-startup-loss.json',stage='prepared',certificate_check=None,fault_override=None,handoff=False):
    result={'passed':False,'mock_llm_calls':0,'jobs':[],'cancel_after_exit':cancel_after_exit}
    def save():(api.root/filename).write_text(json.dumps(result,indent=2)+'\n','utf8')
    async def call(method,path,**kwargs):
        deadline=time.monotonic()+45
        while True:
            try:return await asyncio.to_thread(api.call,method,path,**kwargs)
            except AssertionError as error:
                if not any(c in str(error) for c in ('storage_quota_busy','validation_busy')) or time.monotonic()>deadline:raise
                await asyncio.sleep(.2)
    async def raw(path):return await call('GET',api.prefix+'/raw?path='+quote(path,safe=''),raw=True)
    async def settled(identifier):
        deadline=time.monotonic()+180
        while True:
            job=await call('GET','/jobs/'+str(identifier))
            if job['status']=='succeeded' and job['attempt']['outputs'].get('completion',{}).get('state')=='settled':
                result['jobs'].append(job);save();return job
            assert job['status'] not in {'failed','cancelled','interrupted'} and time.monotonic()<deadline,'Actual report did not settle'
            await asyncio.sleep(.25)
    connection=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,
        password=selected.password,database=selected.database,timeout=10)
    async def snapshot(identifier):
        value=dict(await connection.fetchrow('''SELECT j.status,a.id,a.phase,a.reason_code,a.cancel_requested_at,a.worker_identity_json,
          a.process_identity_json,a.child_processes_json,a.write_checkpoint_json,a.output_manifest_json,
          a.input_manifest_json,a.publication_json FROM jobs j JOIN job_attempts a ON a.job_id=j.id WHERE j.id=$1''',identifier))
        value['cancel_requested_at']=value['cancel_requested_at'].isoformat() if value['cancel_requested_at'] else None
        return value
    api.env.update(VIBEFLOW_WORKER_HOST_QUEUE='true',VIBEFLOW_JOB_HEARTBEAT_SECONDS='1')
    windows=workers_module.Workers(api,worker.broker,worker.store)
    windows.sequence=len(list(api.root.glob('batch-worker-*')))
    original=workers_module.WORKER
    from child_observation_acceptance_support import fault_for
    fault=fault_override if fault_override is not None else FAULT if stage=='prepared' else fault_for(stage)
    check=CHECK if certificate_check is None else certificate_check
    fault_root=api.root/('windows-child-'+stage);fault_root.mkdir()
    workers_module.WORKER=original.replace('asyncio.run(main())','FAULT_ROOT='+repr(str(fault_root))+'\n'+fault+'\nasyncio.run(main())')
    try:
        await asyncio.to_thread(windows.start)
        await call('PUT',api.prefix+'/file',body={'path':'E-report/report.md','content':'# Original report\n\nBEFORE_WORKER_LOSS\n'})
        first=await call('POST',api.prefix+'/report:build',body={});await settled(first['job_id'])
        paths=['E-report/build/report.html','E-report/build/report.docx']
        prior={p:await raw(p) for p in paths}
        result['prior_artifacts']={p:hashlib.sha256(v).hexdigest() for p,v in prior.items()};save()
        await call('PUT',api.prefix+'/file',body={'path':'E-report/report.md','content':'# Recovered report\n\nAFTER_WORKER_LOSS\n'})
        (fault_root/'child-armed').touch()
        created=await call('POST',api.prefix+'/report:build',body={});identifier=created['job_id']
        deadline=time.monotonic()+90
        while not (fault_root/'child-prepared.json').exists():
            assert time.monotonic()<deadline,'Actual child did not reach prepared boundary'
            await asyncio.sleep(.05)
        marker=json.loads((fault_root/'child-prepared.json').read_text());assert marker['job_id']==identifier
        result['stage']=stage;result['independent_fault_observation']=marker
        before=await snapshot(identifier);result['before']=before;save()
        children=json.loads(before['child_processes_json'])['children'];child=children[marker['child_id']]
        assert before['phase']=='running' and before['write_checkpoint_json']=='{}'
        assert child['phase']=='preparing' and child['identity'] is None and child['stop'] is None
        await asyncio.sleep(1)
        assert (await snapshot(identifier))['write_checkpoint_json']=='{}'
        crashed=next(r for r in windows.running if r['identity']==marker['worker'])
        if cancel_after_exit:
            windows.running.remove(crashed);await asyncio.to_thread(windows.stop);windows.running.append(crashed)
        (fault_root/'child-crash-now').touch();await asyncio.to_thread(crashed['process'].wait,30)
        crashed['log'].close();windows.running.remove(crashed)
        assert crashed['process'].returncode==17 and identity.probe(marker['worker'])['state']=='exited'
        result['crash']={'identity':marker['worker'],'exit_code':17,'actual_probe':identity.probe(marker['worker'])};save()
        if cancel_after_exit:
            await call('POST','/jobs/'+str(identifier)+':cancel')
            pending=await snapshot(identifier);result['cancelled_before_recovery']=pending;save()
            assert pending['status']=='cancelled' and pending['phase']=='cancelling' and pending['cancel_requested_at']
            await asyncio.to_thread(windows.start)
        expected='cancelled' if cancel_after_exit else 'interrupted'
        deadline=time.monotonic()+90
        while True:
            after=await snapshot(identifier);result['after']=after;save()
            if after['phase']==expected and after['write_checkpoint_json']!='{}':break
            assert time.monotonic()<deadline,'Actual prepared child did not recover after worker loss'
            await asyncio.sleep(.3)
        assert after['status']==expected and after['reason_code']==('cancel_requested' if cancel_after_exit else 'recovery_required')
        for key in ['id','worker_identity_json','process_identity_json','child_processes_json','input_manifest_json','publication_json']:
            assert before[key]==after[key],key
        assert json.loads(after['output_manifest_json']).get('process_stop') is None
        checkpoint=json.loads(after['write_checkpoint_json']);proof=checkpoint['recovery']
        assert checkpoint['stop'] is None and proof['schema']==('vibeflow.job-child-handoff-recovery.v1' if handoff else 'vibeflow.job-child-startup-recovery.v1')
        if handoff:
            from child_handoff_acceptance_support import verify_independent
            result['independent_handoff_checks']=await verify_independent(marker)
            assert await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.child_launch_observed')==0
            assert await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.child_launch_prepared')==0
        elif stage!='prepared':
            assert await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.child_launch_prepared')==0
            observed=identity.probe(marker['preparation']['host']);assert observed['state']=='exited'
            result['independent_child_host_exit']=observed
        if cancel_after_exit:assert proof['cancel_requested_at']==pending['cancel_requested_at']==after['cancel_requested_at']
        assert await connection.fetchval('SELECT count(*) FROM job_attempts WHERE job_id=$1',identifier)==1
        assert await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.content_scope_recovered')==1
        assert await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.child_running')==0
        for p,v in prior.items():assert await raw(p)==v
        result['original_artifacts_preserved']=True
        result['scope_exits']=[]
        for item in proof['guardians']:
            for name,birth in [('guardian',item['prepared']['prepared']['guardian']),
                               ('host',json.loads(before['process_identity_json'])['host'] if item['prepared']['child_id'] is None
                                else item['observed'].get('host_identity'))]:
                if birth is None:continue
                observed=identity.probe(birth);assert observed['state']=='exited'
                result['scope_exits'].append({'role':name,'identity':birth,'actual_probe':observed})
        env=dict(api.env);env.update(worker.broker.settings());env.update(worker.store.settings())
        checked=await asyncio.to_thread(subprocess.run,[sys.executable,'-X','utf8','-B','-c',check,str(identifier)],
            cwd=baseline.SERVER,env=env,capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        assert checked.returncode==0,checked.stderr[-1500:]
        result['certificate_checks']=json.loads(checked.stdout);save()
        from app.services.owned_process import wsl_path
        prefix="import json,os,sys;from pathlib import Path;os.environ.update(json.loads(Path(sys.argv[1]).read_text()));sys.path.insert(0,"+repr(wsl_path(baseline.SERVER))+");del sys.argv[1]\n"
        checked=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec',baseline.PYTHON,
            '-B','-c',prefix+check,wsl_path(worker.root/'settings.json'),str(identifier)],
            capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        assert checked.returncode==0,checked.stderr[-1500:]
        result['linux_certificate_checks']=json.loads(checked.stdout);save()
        fresh=await call('POST',api.prefix+'/report:build',body={});await settled(fresh['job_id'])
        artifacts={p:await raw(p) for p in paths}
        assert b'AFTER_WORKER_LOSS' in artifacts[paths[0]] and b'BEFORE_WORKER_LOSS' not in artifacts[paths[0]]
        with zipfile.ZipFile(io.BytesIO(artifacts[paths[1]])) as archive:
            assert b'AFTER_WORKER_LOSS' in archive.read('word/document.xml')
        result['fresh_artifacts']={p:hashlib.sha256(v).hexdigest() for p,v in artifacts.items()}
        digest=hashlib.sha256(b''.join(artifacts.values())).hexdigest()
        code="from pathlib import Path;Path('Ref/proof.txt').write_text("+repr('RETIREMENT_'+digest+'\n')+")"
        proof_job=await call('POST',api.prefix+'/jobs',body={'type':'convert','runtime':'windows','command':'python3 -c '+shlex.quote(code)})
        await settled(proof_job['id']);result['passed']=True;save()
        print('CHILD_STARTUP_LOSS_PASS actual Pandoc worker crash, original artifacts preserved, fresh HTML/DOCX built',flush=True)
    finally:
        (fault_root/'child-crash-now').touch();workers_module.WORKER=original
        try:
            await asyncio.to_thread(windows.stop);result['windows_workers']=windows.receipts
        finally:await connection.close();save()


async def verify(configuration,selected,admin,observed):
    environment={'VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS':json.dumps({'report.build':{'host_id':identity.host_id(),'platform':'windows'}})}
    return await baseline.verify(configuration,selected,admin,observed,after_publish=scenario,environment_overrides=environment)

if __name__=='__main__':asyncio.run(baseline.main(entry='platform/server/live_llm_child_startup_loss_acceptance.py',verifier=verify))
