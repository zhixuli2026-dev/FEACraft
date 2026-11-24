"""Actual Windows/WSL root startup loss, cancellation and fresh publication."""
import asyncio,hashlib,json,shlex,subprocess,sys,time
from pathlib import Path
import asyncpg
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native,wsl_path
import workflow_batch_redis_support as workers_module
import live_llm_remote_worker_acceptance as baseline

FAULT='''
from app.services import job_launch_registration,job_native_scopes,job_execution
from app.services.async_ops import finish_before_cancel
async def crash_boundary(owner,stage,observation=None):
 root=Path(FAULT_ROOT)
 armed=root/'armed'
 if armed.exists() and armed.read_text()==stage:
  armed.unlink()
  path=root/'prepared.json';staged=path.with_suffix('.staged')
  staged.write_text(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id,'worker':identity.observe(os.getpid()),'uncommitted_observation':observation}))
  staged.replace(path)
  while not (root/'crash-now').exists():await asyncio.sleep(.02)
  os._exit(17)
original_prepared=job_launch_registration.prepared
async def prepared(owner,process):
 await original_prepared(owner,process)
 await crash_boundary(owner,'prepared')
job_launch_registration.prepared=prepared
original_observed=job_launch_registration.observed
async def observed(owner,value):
 if value['stage']=='created':await crash_boundary(owner,'unobserved',value)
 await original_observed(owner,value)
 await crash_boundary(owner,value['stage'])
job_launch_registration.observed=observed
original_admitted=job_native_scopes.admitted
async def admitted(owner,entry):
 await original_admitted(owner,entry)
 if entry.id is None:await crash_boundary(owner,'admitted')
job_native_scopes.admitted=admitted
original_release=job_execution.release_process
async def released(owner,process):
 await original_release(owner,process)
 await finish_before_cancel(crash_boundary(owner,'running'))
job_execution.release_process=released
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
  root=('recovery','root_preparation');registration=original['recovery']['root_preparation']
  if registration['prepared'] is not None:process=(*root,'prepared','identity')
  elif registration['admission'] is not None:process=(*root,'admission','value','identity')
  else:process=(*root,'observations',len(registration['observations'])-1)
  edits=[('invented_stop',('stop',),{}),('missing_guardian',('recovery','guardians'),[]),
   ('live_owner',('recovery','worker_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('changed_snapshot',('recovery','snapshot_sha256'),'0'*64),
   ('missing_preparation',root,None),('missing_observations',(*root,'observations'),[]),
   ('reordered_observations',(*root,'observations'),list(reversed(registration['observations'])) if len(registration['observations'])>1 else registration['observations']*2),
   ('changed_claim',(*root,'intent','claim_sha256'),'0'*64),
   ('changed_prepared_birth',(*process,'host','pid'),1),
   ('invented_original_running',(*root,'original_phase'),'running'),
   ('changed_cancellation',(*root,'cancel_requested_at'),'2000-01-01T00:00:00'),
   ('schema_downgrade',('recovery','schema'),'vibeflow.job-scope-recovery.v1')]
  if original['recovery']['native_history']:
   edits += [('missing_kernel_history',('recovery','native_history'),[]),
    ('missing_kernel_retirement',('recovery','native_retirements'),[]),
    ('live_native_gate',('recovery','native_retirements',0,'native_gate_exit'),{'state':'match','reason':'birth_and_scope_match'})]
   if registration['observations'][-1]['native'] is not None:
    edits += [('changed_native_uid',(*process,'native','process','uid'),0)]
   else:
    assert original['recovery']['native_retirements'][0]['native_gate_exit'] is None
    edits += [('invented_native_exit',('recovery','native_retirements',0,'native_gate_exit'),{'state':'exited','reason':'process_exited'})]
  if registration['admission'] is not None:
   edits += [('changed_admission_seed',(*root,'admission','binding','seed'),'0'*64),
    ('changed_admission_observation',(*root,'admission','value','observation','result','state'),'absent')]
  if 'root_preparation_sha256' in original['recovery']['guardians'][0]['observed']:
   edits += [('missing_guardian_preparation_binding',('recovery','guardians',0,'observed','root_preparation_sha256'),None)]
  if registration['prepared'] is None and registration['admission'] is None:
   edits += [('incomplete_observation_chain',(*root,'observations'),registration['observations'][:-1])]
   latest=registration['observations'][-1]
   if latest['stage']=='created' or latest['runtime']!='windows' and latest['stage']=='host_ready':
    edits += [('missing_observer_host_birth',('recovery','guardians',0,'observed','host_identity'),None)]
   if original['recovery']['native_retirements']:
    assert original['recovery']['native_retirements'][0]['admission'] is None
    edits += [('invented_admission',('recovery','native_retirements',0,'admission'),{})]
  refused=[]
  for name,path,value in edits:
   altered=copy.deepcopy(original);target=altered
   for key in path[:-1]:target=target[key]
   target[path[-1]]=value
   try:validate_certificate(case,job,attempt,altered)
   except (ValueError,KeyError,TypeError,AttributeError):refused.append(name)
   else:raise AssertionError('Altered root recovery certificate accepted: '+name)
  assert json.loads(attempt.write_checkpoint_json)==original
  print(json.dumps({'original_valid':True,'refused':refused,'database_modified':False}))
 await engine.dispose()
asyncio.run(main())
'''


async def scenario(api,worker,selected,*,runtime,stage,cancel=False,cancel_before_exit=False,certificate_check=CHECK):
    name=runtime+'-'+stage+('-cancel' if cancel else '')
    root=api.root/('root-'+name);root.mkdir()
    result={'passed':False,'runtime':runtime,'stage':stage,'cancel':cancel,'cancel_before_exit':cancel_before_exit,'mock_llm_calls':0,'jobs':[]}
    def save():(root/'result.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
    async def call(method,path,**kwargs):
        deadline=time.monotonic()+45
        while True:
            try:return await asyncio.to_thread(api.call,method,path,**kwargs)
            except AssertionError as error:
                if not any(c in str(error) for c in ('storage_quota_busy','validation_busy')) or time.monotonic()>deadline:raise
                await asyncio.sleep(.2)
    async def submit(code):
        command=('"'+sys.executable+'" -c "'+code+'"') if runtime=='windows' else 'test "$(id -u)" -eq 65534 && python3 -c '+shlex.quote(code)
        return await call('POST',api.prefix+'/jobs',body={'type':'geometry' if runtime=='windows' else 'convert',
            'runtime':runtime,'command':command,'timeout_seconds':120})
    async def settled(identifier):
        deadline=time.monotonic()+120
        while True:
            job=await call('GET','/jobs/'+str(identifier))
            if job['status']=='succeeded' and job['attempt']['outputs'].get('completion',{}).get('state')=='settled':
                result['jobs'].append(job);save();return job
            assert job['status'] not in {'failed','cancelled','interrupted'} and time.monotonic()<deadline,'Fresh root job did not settle'
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
    workers_module.WORKER=original.replace('asyncio.run(main())','FAULT_ROOT='+repr(str(root))+'\n'+FAULT+'\nasyncio.run(main())')
    try:
        await asyncio.to_thread(windows.start)
        prior=await call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)
        marker=root/'payload-must-not-run'
        (root/'armed').write_text(stage)
        code='from pathlib import Path;Path('+repr(str(marker) if runtime=='windows' else wsl_path(marker))+').touch()'
        if stage=='running':
            code='from pathlib import Path;import time;p=Path('+repr(str(marker) if runtime=='windows' else wsl_path(marker))+');[(p.write_text(str(time.time_ns())),time.sleep(.05)) for _ in range(2400)]'
        created=await submit(code);identifier=created['id'];deadline=time.monotonic()+90
        while not (root/'prepared.json').exists():
            assert time.monotonic()<deadline,'Root did not reach actual startup boundary'
            await asyncio.sleep(.05)
        fault=json.loads((root/'prepared.json').read_text());assert fault['job_id']==identifier
        before=await snapshot(identifier);result['before']=before
        assert before['phase']==('running' if stage=='running' else 'starting') and before['child_processes_json']==before['write_checkpoint_json']=='{}'
        assert (before['process_identity_json']=='{}')==(stage!='running')
        rows=await connection.fetch('SELECT type,payload_json FROM durable_events WHERE attempt_id=$1 ORDER BY seq',before['id'])
        prepared=[json.loads(r['payload_json']) for r in rows if r['type']=='attempt.launch_prepared']
        admitted=[json.loads(r['payload_json']) for r in rows if r['type']=='attempt.native_scope_admitted']
        observations=[json.loads(r['payload_json']) for r in rows if r['type']=='attempt.launch_observed']
        observed_only=stage in {'unobserved','created','host_ready','native_ready'}
        assert len(prepared)==(0 if stage=='admitted' or observed_only else 1)
        if observed_only:
            assert not admitted
            if stage=='unobserved':
                assert observations==[] and fault['uncommitted_observation']['stage']=='created'
                process=fault['uncommitted_observation']
            else:
                assert observations[-1]['stage']==stage
                process=observations[-1]
            result['original_observations']=observations
        else:process=prepared[0]['identity'] if prepared else admitted[0]['value']['identity']
        assert process['host']['platform']=='windows' and identity.probe(process['host'])['state']=='match'
        if runtime=='wsl' and process['native'] is not None:
            assert process['native']['process']['uid']==65534
            assert (await probe_native(process['native']['distribution'],process['native']['process']))['state']=='match'
        if runtime=='wsl' and process['native'] is None:
            from app.services.native_scope_control import control
            allocations=[json.loads(r['payload_json']) for r in rows if r['type']=='attempt.native_scope_allocated']
            assert len(allocations)==1 and stage in {'unobserved','created','host_ready'}
            allocation=allocations[0];distribution=allocation['binding']['distribution'];scope=allocation['value']['result']
            result['original_allocation']=allocation
            result['live_scope_observation']=await control(distribution,'observe',{'scope':scope})
            assert result['live_scope_observation']['result']['state']=='empty'
        result['actual_uncommitted_identity' if stage=='unobserved' else 'actual_observed_identity' if observed_only else 'actual_prepared_identity']=process
        result.update(original_launch_prepared_count=len(prepared),original_native_admitted_count=len(admitted));save()
        await asyncio.sleep(1);assert (await snapshot(identifier))['write_checkpoint_json']=='{}'
        if stage=='running':
            assert marker.exists();heartbeat=marker.read_text();await asyncio.sleep(.2);assert marker.read_text()!=heartbeat
            assert json.loads(before['process_identity_json'])==process
        else:assert not marker.exists()
        crashed=next(r for r in windows.running if r['identity']==fault['worker'])
        if cancel:
            windows.running.remove(crashed)
            await asyncio.to_thread(windows.stop)
            windows.running.append(crashed)
        if cancel_before_exit:
            assert cancel and stage=='running'
            await call('POST','/jobs/'+str(identifier)+':cancel')
            pending=await snapshot(identifier);result['cancelled_before_recovery']=pending;save()
            assert pending['status']=='cancelled' and pending['phase']=='cancelling' and pending['cancel_requested_at']
            assert crashed['process'].poll() is None and identity.probe(process['host'])['state']=='match'
        (root/'crash-now').touch();await asyncio.to_thread(crashed['process'].wait,30)
        crashed['log'].close();windows.running.remove(crashed)
        assert crashed['process'].returncode==17 and identity.probe(fault['worker'])['state']=='exited'
        result['crash']={'identity':fault['worker'],'exit_code':17,'actual_probe':identity.probe(fault['worker'])};save()
        if cancel:
            if not cancel_before_exit:
                await call('POST','/jobs/'+str(identifier)+':cancel')
                pending=await snapshot(identifier);assert pending['status']=='cancelled' and pending['phase']=='cancelling'
                result['cancelled_before_recovery']=pending;save()
            await asyncio.to_thread(windows.start)
        expected='cancelled' if cancel else 'interrupted';deadline=time.monotonic()+120
        while True:
            after=await snapshot(identifier);result['after']=after;save()
            if after['phase']==expected and after['write_checkpoint_json']!='{}':break
            assert time.monotonic()<deadline,'Original prepared root did not recover'
            await asyncio.sleep(.25)
        assert after['status']==expected and after['reason_code']==('cancel_requested' if cancel else 'recovery_required')
        for key in ['id','worker_identity_json','process_identity_json','child_processes_json','input_manifest_json','publication_json']:
            assert before[key]==after[key],key
        assert json.loads(after['output_manifest_json']).get('process_stop') is None
        checkpoint=json.loads(after['write_checkpoint_json']);proof=checkpoint['recovery']
        assert checkpoint['stop'] is None and proof['schema']==('vibeflow.job-scope-recovery.v1' if stage=='running' else 'vibeflow.job-root-startup-recovery.v1')
        if stage=='running':assert proof['cancel_requested_at']==pending['cancel_requested_at']==after['cancel_requested_at']
        else:assert proof['root_preparation']['prepared']==(prepared[0] if prepared else None)
        assert await connection.fetchval('SELECT count(*) FROM job_attempts WHERE job_id=$1',identifier)==1
        assert await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.launch_prepared')==len(prepared)
        if observed_only:
            assert proof['root_preparation']['prepared'] is None and proof['root_preparation']['admission'] is None
            assert proof['root_preparation']['observations']==observations
            assert await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.native_scope_admitted')==0
            assert await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.launch_observed')==len(observations)
        assert await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.content_scope_recovered')==1
        assert await call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)==prior
        if stage=='running':
            heartbeat=marker.read_text();await asyncio.sleep(1.2);assert marker.read_text()==heartbeat
            result['payload_started_and_stopped']=True
        else:assert not marker.exists();result['payload_not_run']=True
        result['prior_artifact_preserved']=True
        assert identity.probe(process['host'])['state']=='exited'
        if runtime=='wsl':
            assert len(proof['native_retirements'])==1 and proof['native_retirements'][0]['retirement']['result']['state']=='absent'
            if process['native'] is not None:
                assert (await probe_native(process['native']['distribution'],process['native']['process']))['state']=='exited'
            else:
                assert proof['native_retirements'][0]['native_gate_exit'] is None
                result['retired_scope_observation']=await control(distribution,'observe',{'scope':scope})
                assert result['retired_scope_observation']['result']['state']=='absent'
                # Actual kernel admission against the original revoked scope
                # must fail; no native birth/exit is synthesized for this window.
                code="import json,sys;sys.path.insert(0,"+repr(wsl_path(baseline.SERVER))+");from app.services.process_cgroup_native import join\ntry:join(json.loads(sys.argv[1]))\nexcept ValueError as error:\n assert str(error)=='Native scope has been revoked';print(json.dumps({'late_join_refused':True}))\nelse:raise AssertionError('Revoked scope accepted a late gate')"
                rejected=await asyncio.to_thread(subprocess.run,['wsl.exe','-d',distribution,'--user','root','--exec',baseline.PYTHON,'-I','-B','-c',code,json.dumps(scope)],
                    capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
                assert rejected.returncode==0,rejected.stderr[-1500:]
                result['actual_late_join']=json.loads(rejected.stdout)
        env=dict(api.env);env.update(worker.broker.settings());env.update(worker.store.settings())
        checked=await asyncio.to_thread(subprocess.run,[sys.executable,'-X','utf8','-B','-c',certificate_check,str(identifier)],
            cwd=baseline.SERVER,env=env,capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        assert checked.returncode==0,checked.stderr[-1500:]
        result['certificate_checks']=json.loads(checked.stdout);save()
        prefix="import json,os,sys;from pathlib import Path;os.environ.update(json.loads(Path(sys.argv[1]).read_text()));sys.path.insert(0,"+repr(wsl_path(baseline.SERVER))+");del sys.argv[1]\n"
        checked=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec',baseline.PYTHON,
            '-B','-c',prefix+certificate_check,wsl_path(worker.root/'settings.json'),str(identifier)],
            capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        assert checked.returncode==0,checked.stderr[-1500:]
        result['linux_certificate_checks']=json.loads(checked.stdout);save()
        fresh=await submit("from pathlib import Path;import secrets;Path('Ref/proof.txt').write_text('RETIREMENT_'+secrets.token_hex(16)+chr(10))")
        fresh=await settled(fresh['id'])
        if runtime=='wsl':assert fresh['attempt']['process_identity']['native']['process']['uid']==65534
        artifact=await call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)
        assert artifact!=prior and artifact.startswith(b'RETIREMENT_')
        result['fresh_artifact_sha256']=hashlib.sha256(artifact).hexdigest();result['passed']=True;save()
        print('ROOT_STARTUP_LOSS_PASS '+name+' recovered original and published fresh command',flush=True)
    finally:
        (root/'crash-now').touch();workers_module.WORKER=original
        try:
            await asyncio.to_thread(windows.stop);result['windows_workers']=windows.receipts
        finally:await connection.close();save()


async def scenarios(api,worker,selected):
    for runtime,stage,cancel in [('windows','prepared',False),('wsl','prepared',False),('wsl','admitted',False),('wsl','admitted',True)]:
        await scenario(api,worker,selected,runtime=runtime,stage=stage,cancel=cancel)


async def verify(configuration,selected,admin,observed):
    windows={'host_id':identity.host_id(),'platform':'windows'}
    environment={'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps({'windows':{'host_id':observed['host_id'],'platform':'linux'},'wsl':windows}),
        'VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS':json.dumps({'command.geometry':windows})}
    return await baseline.verify(configuration,selected,admin,observed,after_publish=scenarios,environment_overrides=environment)


if __name__=='__main__':asyncio.run(baseline.main(entry='platform/server/live_llm_root_startup_loss_acceptance.py',verifier=verify))
