"""Actual owner death after namespace/launch preparation, before running commit."""
import asyncio,hashlib,json,shlex,time,subprocess
import asyncpg
from app.services.owned_process import probe_native,wsl_path
import live_llm_remote_worker_acceptance as baseline

FAULT='''
from app.services import job_launch_registration
original_prepared=job_launch_registration.prepared
async def faulted_prepared(owner,process):
 await original_prepared(owner,process)
 root=Path(FAULT_ROOT)
 if (root/'armed').exists():
  (root/'armed').unlink()
  path=root/'prepared.json';staged=path.with_suffix('.staged')
  staged.write_text(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id}))
  staged.replace(path)
  while not (root/'crash-now').exists():await asyncio.sleep(.02)
  os._exit(17)
job_launch_registration.prepared=faulted_prepared
'''

CERTIFICATE_CHECK='''
import asyncio,copy,json,os,sys
from pathlib import Path
os.environ.update(json.loads(Path(sys.argv[1]).read_text()))
sys.path.insert(0,SERVER)
from app.db import SessionLocal
from app.models import Job,JobAttempt,Case
from sqlalchemy import select
from app.services.job_linux_namespaces import validate_certificate
from app.services.job_write_checkpoint import validate
async def main():
 async with SessionLocal() as db:
  job=await db.get(Job,int(sys.argv[2]))
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
  case=await db.get(Case,job.case_id)
  original=json.loads(attempt.write_checkpoint_json)
  validate(case,job,attempt);validate_certificate(case,job,attempt,original)
  edits=[('invented_stop',('stop',),{}),('missing_namespace',('recovery','namespaces'),[]),
   ('changed_init_birth',('recovery','namespaces',0,'prepared','init','pid'),1),
   ('live_init',('recovery','namespaces',0,'init_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('live_owner',('recovery','worker_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('changed_snapshot',('recovery','snapshot_sha256'),'0'*64),
   ('missing_registration',('recovery','registration'),None),
   ('invented_original_running',('recovery','registration','original_phase'),'running'),
   ('missing_observations',('recovery','registration','observations'),[]),
   ('changed_original_claim',('recovery','registration','intent','claim_sha256'),'0'*64),
   ('changed_prepared_gate',('recovery','registration',*(('prepared','identity') if original['recovery']['registration']['prepared'] is not None else ('namespace','process')),'pid'),1),
   ('schema_downgrade',('recovery','schema'),'vibeflow.job-linux-scope-recovery.v1')]
  if original['recovery']['registration']['original_phase']=='cancelling':
   edits.append(('changed_cancel_request',('recovery','registration','cancel_requested_at'),'2000-01-01T00:00:00'))
  refused=[]
  for name,path,value in edits:
   altered=copy.deepcopy(original);target=altered
   for key in path[:-1]:target=target[key]
   target[path[-1]]=value
   try:validate_certificate(case,job,attempt,altered)
   except (ValueError,KeyError,TypeError):refused.append(name)
   else:raise AssertionError('Altered startup certificate accepted: '+name)
  assert json.loads(attempt.write_checkpoint_json)==original
  print(json.dumps({'original_valid':True,'refused':refused,'database_modified':False}))
asyncio.run(main())
'''


class CrashWorker(baseline.LinuxWorker):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.api.env['VIBEFLOW_JOB_HEARTBEAT_SECONDS']='1'
    def fixture_code(self,root):return 'FAULT_ROOT='+repr(wsl_path(root))+'\n'+FAULT


async def scenario(api,worker,selected,*,stage='launch',cancel_after_exit=False,filename='startup-loss.json'):
    result={'passed':False,'mock_llm_calls':0,'jobs':[]}
    def save():(api.root/filename).write_text(json.dumps(result,indent=2)+'\n','utf8')
    result.update(stage=stage,cancel_after_exit=cancel_after_exit)
    async def call(method,path,**kwargs):
        deadline=time.monotonic()+45
        while True:
            try:return await asyncio.to_thread(api.call,method,path,**kwargs)
            except AssertionError as error:
                if not any(c in str(error) for c in ('storage_quota_busy','validation_busy')) or time.monotonic()>deadline:raise
                await asyncio.sleep(.2)
    connection=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,
        password=selected.password,database=selected.database,timeout=10)
    async def snapshot(identifier):
        row=await connection.fetchrow('''SELECT j.status,a.id,a.job_id,a.phase,a.reason_code,a.worker_identity_json,
          a.process_identity_json,a.child_processes_json,a.write_checkpoint_json,a.output_manifest_json,
          a.input_manifest_json,a.publication_json FROM jobs j JOIN job_attempts a ON a.job_id=j.id WHERE j.id=$1''',identifier)
        return dict(row)
    try:
        prior=await call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)
        root=worker.root;marker=api.root/'payload-must-not-run'
        (root/'armed').write_text(stage)
        code="from pathlib import Path;Path("+repr(wsl_path(marker))+").touch()"
        created=await call('POST',api.prefix+'/jobs',body={'type':'convert','runtime':'windows','timeout_seconds':120,
            'command':'python3 -c '+shlex.quote(code)})
        identifier=created['id'];deadline=time.monotonic()+90
        while not (root/'prepared.json').exists():
            assert worker.process.poll() is None and time.monotonic()<deadline
            await asyncio.sleep(.05)
        before=await snapshot(identifier);result['before']=before
        assert before['phase']=='starting' and before['process_identity_json']=='{}' and before['write_checkpoint_json']=='{}'
        rows=await connection.fetch('SELECT payload_json FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.linux_namespace_prepared')
        assert len(rows)==1;prepared=json.loads(rows[0]['payload_json']);result['namespace']=prepared;save()
        prepared_count=await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.launch_prepared')
        assert prepared_count==(1 if stage=='launch' else 0)
        result['original_launch_prepared_count']=prepared_count
        await asyncio.sleep(1)
        assert (await snapshot(identifier))['write_checkpoint_json']=='{}' and not marker.exists()
        (root/'crash-now').touch();await asyncio.to_thread(worker.process.wait,45)
        worker.log.close();assert worker.process.returncode==17
        observation=await probe_native('vibeflow-linux',worker.birth);assert observation['state']=='exited'
        result['crash']={'identity':worker.birth,'observation':observation,'exit_code':17};worker.process=None;save()
        for name,birth in [('gate',prepared['process']['host']),('init',prepared['init'])]:
            deadline=time.monotonic()+30
            while True:
                observed=await probe_native('vibeflow-linux',birth)
                if observed['state']=='exited':break
                assert time.monotonic()<deadline,'Original prepared namespace did not exit'
                await asyncio.sleep(.1)
            result[name+'_exit']=observed
        if cancel_after_exit:
            await call('POST','/jobs/'+str(identifier)+':cancel')
            cancelled=await snapshot(identifier);result['cancelled_before_recovery']=cancelled;save()
            assert cancelled['status']=='cancelled' and cancelled['phase']=='cancelling'
        expected='cancelled' if cancel_after_exit else 'interrupted'
        await asyncio.to_thread(worker.start)
        deadline=time.monotonic()+90
        while True:
            after=await snapshot(identifier);result['after']=after;save()
            if after['phase']==expected and after['write_checkpoint_json']!='{}':break
            notices=await connection.fetch('SELECT payload_json FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.registration_recovery_required')
            for notice in notices:
                value=json.loads(notice['payload_json'])
                if value['owner']['state']=='exited':
                    result['unrecovered_notice']=value;save()
                    raise AssertionError('Actual exited startup owner remains registration_recovery_required')
            assert time.monotonic()<deadline,'Actual startup owner was not recovered'
            await asyncio.sleep(.25)
        assert after['status']==expected and after['reason_code']==('cancel_requested' if cancel_after_exit else 'recovery_required')
        for key in ['id','worker_identity_json','process_identity_json','child_processes_json','input_manifest_json']:
            assert before[key]==after[key],key
        assert after['process_identity_json']=='{}' and json.loads(after['output_manifest_json']).get('process_stop') is None
        checkpoint=json.loads(after['write_checkpoint_json'])
        assert checkpoint['schema']=='vibeflow.job-write-checkpoint.v4' and checkpoint['state']=='recovered_stopped' and checkpoint['stop'] is None
        assert checkpoint['recovery']['schema']=='vibeflow.job-linux-startup-recovery.v1'
        assert (checkpoint['recovery']['registration']['prepared'] is None)==(stage=='namespace')
        assert prepared_count==await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.launch_prepared')
        assert await connection.fetchval('SELECT count(*) FROM job_attempts WHERE job_id=$1',identifier)==1
        events=await connection.fetch('SELECT payload_json FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.content_scope_recovered')
        assert len(events)==1 and json.loads(events[0]['payload_json'])['replayed'] is False
        assert not marker.exists() and await call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)==prior
        checked=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec',baseline.PYTHON,
            '-B','-c','SERVER='+repr(wsl_path(baseline.SERVER))+'\n'+CERTIFICATE_CHECK,
            wsl_path(worker.root/'settings.json'),str(identifier)],capture_output=True,text=True,timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        assert checked.returncode==0,checked.stderr[-1500:]
        result['certificate_checks']=json.loads(checked.stdout);save()
        code="from pathlib import Path;import secrets;Path('Ref/proof.txt').write_bytes(('RETIREMENT_'+secrets.token_hex(16)+'\\n').encode())"
        fresh=await call('POST',api.prefix+'/jobs',body={'type':'convert','runtime':'windows','command':'python3 -c '+shlex.quote(code)})
        deadline=time.monotonic()+120
        while True:
            job=await call('GET','/jobs/'+str(fresh['id']))
            if job['status']=='succeeded' and job['attempt']['outputs'].get('completion',{}).get('state')=='settled':break
            assert job['status'] not in {'failed','cancelled','interrupted'} and time.monotonic()<deadline
            await asyncio.sleep(.25)
        result['jobs'].append(job);result['passed']=True;save()
        print('STARTUP_LOSS_PASS original missing registration preserved; namespace retired; fresh job succeeded',flush=True)
    except BaseException as error:result['failure_type']=type(error).__name__;save();raise
    finally:
        (worker.root/'crash-now').touch()
        await connection.close();save()


async def verify(configuration,selected,admin,observed):
    return await baseline.verify(configuration,selected,admin,observed,worker_factory=CrashWorker,after_publish=scenario)
if __name__=='__main__':asyncio.run(baseline.main(entry='platform/server/live_llm_remote_startup_loss_acceptance.py',verifier=verify))
