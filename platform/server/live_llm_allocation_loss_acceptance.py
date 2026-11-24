"""Real native allocation owner loss before root launch, then fresh publication."""
import asyncio,hashlib,json,shlex,subprocess,sys,time
from pathlib import Path
import asyncpg
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native,wsl_path
from app.services.native_scope_control import control
import workflow_batch_redis_support as workers_module
import live_llm_remote_worker_acceptance as baseline

FAULT='''
from app.services import job_native_scopes,job_launch_registration,job_scope_guardians
from app.services.owned_process import OwnedProcess
from contextvars import ContextVar
async def boundary(owner,stage,bound,value):
 root=Path(FAULT_ROOT);armed=root/'armed'
 if armed.exists() and armed.read_text()==stage:
  armed.unlink();path=root/'boundary.json';staged=path.with_suffix('.staged')
  staged.write_text(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id,'worker':identity.observe(os.getpid()),'binding':bound,'value':value}))
  staged.replace(path)
  while not (root/'crash-now').exists():await asyncio.sleep(.02)
  os._exit(17)
original_record=job_native_scopes.record
async def record(owner,kind,bound,value,**kwargs):
 if bound['child_id'] is None and kind==job_native_scopes.ALLOCATED:await boundary(owner,'unacknowledged',bound,value)
 await original_record(owner,kind,bound,value,**kwargs)
 if bound['child_id'] is None:
  stage={job_native_scopes.INTENT:'intended',job_native_scopes.PREPARED:'prepared',job_native_scopes.ALLOCATED:'allocated'}.get(kind)
  if stage:await boundary(owner,stage,bound,value)
job_native_scopes.record=record
original_reserve=job_launch_registration.reserve
async def reserve(owner,process):
 await original_reserve(owner,process)
 await boundary(owner,'launch_intended',{}, {'launch_nonce':process.nonce})
job_launch_registration.reserve=reserve
guard_owner=ContextVar('real_guard_preparation_owner',default=None)
original_guard=job_scope_guardians.prepare
async def guard(owner,owned,**kwargs):
 token=guard_owner.set(owner if kwargs.get('child_id') is None else None)
 try:return await original_guard(owner,owned,**kwargs)
 finally:guard_owner.reset(token)
job_scope_guardians.prepare=guard
original_owned_guard=OwnedProcess.prepare_guardian
async def owned_guard(self,*args,**kwargs):
 await original_owned_guard(self,*args,**kwargs)
 owner=guard_owner.get()
 if owner is not None:
  await boundary(owner,'guardian_returned',{}, {'request':self.guardian.request,'prepared':self.guardian.prepared})
OwnedProcess.prepare_guardian=owned_guard
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
  job=await db.get(Job,int(sys.argv[1]));attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id));case=await db.get(Case,job.case_id)
  original=json.loads(attempt.write_checkpoint_json);validate(case,job,attempt);validate_certificate(case,job,attempt,original)
  proof=original['recovery'];root=('recovery',);closure=(*root,'closure')
  assert proof['schema']=='vibeflow.job-allocation-recovery.v1' and proof['launch_history']==[]
  assert original['stop'] is None and json.loads(attempt.process_identity_json)==json.loads(attempt.child_processes_json)=={}
  edits=[('invented_stop',('stop',),{}),('live_owner',(*root,'worker_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('changed_snapshot',(*root,'snapshot_sha256'),'0'*64),('missing_history',(*root,'native_history'),[]),
   ('changed_history_binding',(*closure,'history_sha256'),'0'*64),('invented_release',(*closure,'business_not_released'),False),
   ('invented_unknown_exit',(*closure,'unknown_process_exit_inferred'),True),('changed_claim',('claim_sha256',),'0'*64),
   ('invented_launch',(*root,'launch_history'),[['attempt.launch_intended',{}]]),
   ('legacy_intent',(*root,'native_history',0,1,'value'),{}),('changed_contract',(*root,'native_history',0,1,'value','business_release'),'unconditional'),
   ('invented_running',(*root,'original_phase'),'running'),('changed_cancellation',(*root,'cancel_requested_at'),'2000-01-01T00:00:00'),
   ('invented_original_output',(*root,'original_output_manifest_json'),'{"process_stop":{}}'),
   ('schema_downgrade',(*root,'schema'),'vibeflow.job-scope-recovery.v1')]
  if proof['closure']['retirement'] is None:
   edits += [('invented_retirement',(*closure,'retirement'),{}),('missing_unknown_bootstraps',(*closure,'unobserved_bootstraps'),[])]
  else:
   edits += [('missing_retirement',(*closure,'retirement'),None),('missing_known_births',(*closure,'known_birth_exits'),[]),
    ('live_creator',(*closure,'known_birth_exits',1,'exit'),{'state':'match','reason':'birth_and_scope_match'}),
    ('changed_retirement_distribution',(*closure,'retirement','request','distribution'),'wrong-distro'),
    ('changed_retirement_operation',(*closure,'retirement','request','operation'),'observe')]
  refused=[]
  for name,path,value in edits:
   altered=copy.deepcopy(original);target=altered
   for key in path[:-1]:target=target[key]
   target[path[-1]]=value
   try:validate_certificate(case,job,attempt,altered)
   except (ValueError,KeyError,TypeError,AttributeError):refused.append(name)
   else:raise AssertionError('Altered allocation certificate accepted: '+name)
  assert json.loads(attempt.write_checkpoint_json)==original
  print(json.dumps({'original_valid':True,'refused':refused,'database_modified':False}))
 await engine.dispose()
asyncio.run(main())
'''


async def scenario(api,worker,selected,stage,cancel=False,*,runtime='wsl',certificate_check=CHECK):
    launching=stage in {'launch_intended','guardian_returned'}
    root=api.root/(('handoff-'+runtime+'-' if launching else 'allocation-')+stage+('-cancel' if cancel else ''));root.mkdir()
    result={'passed':False,'stage':stage,'cancel':cancel,'runtime':runtime,'mock_llm_calls':0,'jobs':[]}
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
        return await call('POST',api.prefix+'/jobs',body={'type':'geometry' if runtime=='windows' else 'convert','runtime':runtime,
            'command':command,'timeout_seconds':120})
    connection=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,password=selected.password,database=selected.database,timeout=10)
    async def snapshot(identifier):
        value=dict(await connection.fetchrow('''SELECT j.status,a.id,a.phase,a.reason_code,a.cancel_requested_at,a.worker_identity_json,
          a.process_identity_json,a.child_processes_json,a.write_checkpoint_json,a.output_manifest_json,
          a.input_manifest_json,a.publication_json FROM jobs j JOIN job_attempts a ON a.job_id=j.id WHERE j.id=$1''',identifier))
        value['cancel_requested_at']=value['cancel_requested_at'].isoformat() if value['cancel_requested_at'] else None
        return value
    async def histories(attempt):
        rows=await connection.fetch('SELECT type,payload_json FROM durable_events WHERE attempt_id=$1 ORDER BY seq',attempt)
        native=[[r['type'],json.loads(r['payload_json'])] for r in rows if r['type'] in {
            'attempt.native_scope_intent','attempt.native_scope_prepared','attempt.native_scope_allocated','attempt.native_scope_admitted','attempt.native_scope_retired'}]
        launch=[[r['type'],json.loads(r['payload_json'])] for r in rows if r['type'].startswith('attempt.launch_') or r['type'].startswith('attempt.scope_guardian_')]
        return native,launch
    api.env.update(VIBEFLOW_WORKER_HOST_QUEUE='true',VIBEFLOW_JOB_HEARTBEAT_SECONDS='1')
    windows=workers_module.Workers(api,worker.broker,worker.store);windows.sequence=len(list(api.root.glob('batch-worker-*')))
    original=workers_module.WORKER;workers_module.WORKER=original.replace('asyncio.run(main())','FAULT_ROOT='+repr(str(root))+'\n'+FAULT+'\nasyncio.run(main())')
    try:
        await asyncio.to_thread(windows.start)
        prior=await call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True);marker=root/'payload-must-not-run'
        (root/'armed').write_text(stage)
        created=await submit('from pathlib import Path;Path('+repr(str(marker) if runtime=='windows' else wsl_path(marker))+').touch()');identifier=created['id']
        deadline=time.monotonic()+90
        while not (root/'boundary.json').exists():
            assert time.monotonic()<deadline,'Allocation did not reach real boundary'
            await asyncio.sleep(.05)
        fault=json.loads((root/'boundary.json').read_text());assert fault['job_id']==identifier
        before=await snapshot(identifier);history,launch=await histories(before['id']);result.update(before=before,original_history=history,original_launch_history=launch,fault=fault);save()
        assert before['phase']=='starting' and before['process_identity_json']==before['child_processes_json']==before['write_checkpoint_json']=='{}'
        if launching:
            assert len(launch)==1 and launch[0][0]=='attempt.launch_intended'
            assert len(history)==(3 if runtime=='wsl' else 0)
            if stage=='guardian_returned':
                assert identity.probe(fault['value']['prepared']['guardian'])['state']=='match'
                assert fault['value']['request']['owner']==fault['worker']
        else:assert not launch and len(history)=={'intended':1,'prepared':2,'unacknowledged':2,'allocated':3}[stage]
        if stage=='prepared':
            preparation=fault['value']
            assert identity.probe(preparation['host']['host'])['state']=='match'
            assert (await probe_native(fault['binding']['distribution'],preparation['native']['controller']))['state']=='match'
        scoped=stage in {'unacknowledged','allocated'} or launching and runtime=='wsl'
        if scoped:
            allocation=history[-1][1] if launching else {'binding':fault['binding'],'value':fault['value']}
            scope=allocation['value']['result'];distribution=allocation['binding']['distribution']
            result['before_scope']=await control(distribution,'observe',{'scope':scope})
            assert result['before_scope']['result']['state']=='empty';save()
        await asyncio.sleep(1);assert not marker.exists() and (await snapshot(identifier))['write_checkpoint_json']=='{}'
        crashed=next(v for v in windows.running if v['identity']==fault['worker'])
        if cancel:
            windows.running.remove(crashed);await asyncio.to_thread(windows.stop);windows.running.append(crashed)
        (root/'crash-now').touch();await asyncio.to_thread(crashed['process'].wait,30)
        crashed['log'].close();windows.running.remove(crashed)
        assert crashed['process'].returncode==17 and identity.probe(fault['worker'])['state']=='exited'
        result['crash']={'identity':fault['worker'],'exit_code':17,'actual_probe':identity.probe(fault['worker'])};save()
        if cancel:
            await call('POST','/jobs/'+str(identifier)+':cancel');pending=await snapshot(identifier)
            assert pending['status']=='cancelled' and pending['phase']=='cancelling'
            result['cancelled_before_recovery']=pending;save();await asyncio.to_thread(windows.start)
        deadline=time.monotonic()+120;expected='cancelled' if cancel else 'interrupted'
        while True:
            after=await snapshot(identifier);result['after']=after;save()
            if after['phase']==expected and after['write_checkpoint_json']!='{}':break
            assert time.monotonic()<deadline,'Original allocation did not recover'
            await asyncio.sleep(.25)
        assert after['status']==expected and after['reason_code']==('cancel_requested' if cancel else 'recovery_required')
        for key in ['id','worker_identity_json','process_identity_json','child_processes_json','input_manifest_json','publication_json']:assert before[key]==after[key]
        assert await histories(before['id'])==(history,launch) and not marker.exists()
        assert await connection.fetchval('SELECT count(*) FROM job_attempts WHERE job_id=$1',identifier)==1
        checkpoint=json.loads(after['write_checkpoint_json']);proof=checkpoint['recovery'];closure=proof['closure']
        assert checkpoint['stop'] is None and json.loads(after['output_manifest_json']).get('process_stop') is None
        assert proof['native_history']==history and proof['launch_history']==launch and closure['business_not_released']
        if cancel:assert proof['cancel_requested_at']==pending['cancel_requested_at']==after['cancel_requested_at']
        if launching:
            assert proof['schema']=='vibeflow.job-launch-preparation-recovery.v1' and closure['unobserved_bootstraps']==['windows_guardian']
            if runtime=='windows':assert closure['retirement'] is None and closure['known_birth_exits']==[]
            else:assert len(closure['known_birth_exits'])==2 and closure['retirement']['request']['operation']=='retire'
            if stage=='guardian_returned':
                from app.services import windows_scope_guardian as protocol
                deadline=time.monotonic()+30
                while identity.probe(fault['value']['prepared']['guardian'])['state']!='exited':
                    assert time.monotonic()<deadline,'Original uncommitted guardian did not exit'
                    await asyncio.sleep(.1)
                receipt=protocol.read(fault['value']['request'],'receipt.json')
                protocol.validate_receipt(fault['value']['request'],fault['value']['prepared'],receipt)
                result['actual_uncommitted_guardian_receipt']=receipt
        elif stage=='intended':assert closure['retirement'] is None and closure['known_birth_exits']==[]
        else:
            assert len(closure['known_birth_exits'])==2
            assert closure['retirement']['request']['operation']==('retire' if stage=='allocated' else 'abandon')
        if scoped:
            result['after_scope']=await control(distribution,'observe',{'scope':scope})
            assert result['after_scope']['result']['state']=='absent'
            if stage=='unacknowledged':assert closure['retirement']['result']['resolved_scope']==scope
        assert await call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)==prior
        result.update(payload_not_run=True,prior_artifact_preserved=True);save()
        env=dict(api.env);env.update(worker.broker.settings());env.update(worker.store.settings())
        checked=await asyncio.to_thread(subprocess.run,[sys.executable,'-X','utf8','-B','-c',certificate_check,str(identifier)],cwd=baseline.SERVER,env=env,capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        assert checked.returncode==0,checked.stderr[-1500:]
        result['certificate_checks']=json.loads(checked.stdout)
        prefix="import json,os,sys;from pathlib import Path;os.environ.update(json.loads(Path(sys.argv[1]).read_text()));sys.path.insert(0,"+repr(wsl_path(baseline.SERVER))+");del sys.argv[1]\n"
        checked=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec',baseline.PYTHON,'-B','-c',prefix+certificate_check,wsl_path(worker.root/'settings.json'),str(identifier)],capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        assert checked.returncode==0,checked.stderr[-1500:]
        result['linux_certificate_checks']=json.loads(checked.stdout);save()
        fresh=await submit("from pathlib import Path;import secrets;Path('Ref/proof.txt').write_text('RETIREMENT_'+secrets.token_hex(16)+chr(10))")
        deadline=time.monotonic()+120
        while True:
            job=await call('GET','/jobs/'+str(fresh['id']))
            if job['status']=='succeeded' and job['attempt']['outputs'].get('completion',{}).get('state')=='settled':break
            assert job['status'] not in {'failed','cancelled','interrupted'} and time.monotonic()<deadline
            await asyncio.sleep(.25)
        if runtime=='wsl':assert job['attempt']['process_identity']['native']['process']['uid']==65534
        result['jobs'].append(job);artifact=await call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)
        assert artifact!=prior and artifact.startswith(b'RETIREMENT_')
        result.update(fresh_artifact_sha256=hashlib.sha256(artifact).hexdigest(),passed=True);save()
        print('ALLOCATION_LOSS_PASS '+stage+('-cancel' if cancel else '')+' closed original preparation and published new command',flush=True)
    finally:
        (root/'crash-now').touch();workers_module.WORKER=original
        try:await asyncio.to_thread(windows.stop);result['windows_workers']=windows.receipts
        finally:await connection.close();save()


async def scenarios(api,worker,selected):
    for stage,cancel in [('intended',False),('prepared',False),('unacknowledged',False),('allocated',False),('unacknowledged',True)]:
        await scenario(api,worker,selected,stage,cancel)
    from live_llm_root_unobserved_loss_acceptance import CHECK as root_check
    from live_llm_root_startup_loss_acceptance import scenario as root_scenario
    await root_scenario(api,worker,selected,runtime='wsl',stage='unobserved',certificate_check=root_check)

async def verify(configuration,selected,admin,observed):
    windows={'host_id':identity.host_id(),'platform':'windows'}
    environment={'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps({'windows':{'host_id':observed['host_id'],'platform':'linux'},'wsl':windows})}
    return await baseline.verify(configuration,selected,admin,observed,after_publish=scenarios,environment_overrides=environment)

if __name__=='__main__':
    asyncio.run(baseline.main(entry='platform/server/live_llm_allocation_loss_acceptance.py',verifier=verify))
