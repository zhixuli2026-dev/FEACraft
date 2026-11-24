"""Actual running Linux owner death, detached writer retirement and real DeepSeek."""
import asyncio,json,shlex,subprocess,time
from pathlib import Path
import asyncpg
import live_llm_remote_worker_acceptance as baseline
from app.services.owned_process import probe_native,wsl_path

CERTIFICATE_CHECK='''
import asyncio,copy,json,os,sys
from pathlib import Path
os.environ.update(json.loads(Path(sys.argv[1]).read_text()))
sys.path.insert(0,SERVER)
from app.db import SessionLocal
from app.models import Job,JobAttempt,Case
from sqlalchemy import select
from app.services.job_linux_namespaces import validate_certificate
async def main():
 async with SessionLocal() as db:
  job=await db.get(Job,int(sys.argv[2]))
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
  case=await db.get(Case,job.case_id)
  original=json.loads(attempt.write_checkpoint_json)
  validate_certificate(case,job,attempt,original)
  edits=[('invented_stop',('stop',),{}),('missing_namespaces',('recovery','namespaces'),[]),
   ('changed_init_birth',('recovery','namespaces',0,'prepared','init','pid'),1),
   ('live_init',('recovery','namespaces',0,'init_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('live_owner',('recovery','worker_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('changed_snapshot',('recovery','snapshot_sha256'),'0'*64),('boolean_case_id',('case_id',),True),
   ('changed_original_output',('recovery','original_output_manifest_json'),'\u007b"invented":true\u007d')]
  refused=[]
  if attempt.phase=='cancelled':
   assert original['recovery']['cancel_requested_at']==attempt.cancel_requested_at.isoformat()
   edits += [('missing_cancel_request',('recovery','cancel_requested_at'),None),
    ('changed_cancel_request',('recovery','cancel_requested_at'),'2000-01-01T00:00:00')]
  for name,path,value in edits:
   changed=copy.deepcopy(original);target=changed
   for key in path[:-1]:target=target[key]
   target[path[-1]]=value
   try:validate_certificate(case,job,attempt,changed)
   except (ValueError,KeyError,TypeError):refused.append(name)
   else:raise AssertionError('Changed certificate accepted: '+name)
  print(json.dumps({'actual_certificate_valid':True,'refused':refused,'database_modified':False}))
asyncio.run(main())
'''


class CrashWorker(baseline.LinuxWorker):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.api.env['VIBEFLOW_JOB_HEARTBEAT_SECONDS']='1'
    def fixture_code(self,root):
        return '''
original_startup=on_startup
async def crash_watch():
 while not Path(CRASH).exists():await asyncio.sleep(.05)
 os._exit(17)
async def on_startup(ctx):
 await original_startup(ctx)
 ctx['actual_crash_watch']=asyncio.create_task(crash_watch())
'''.replace('CRASH',repr(wsl_path(root/'crash')))


async def scenario(api,worker,selected,*,cancel_after_exit=False,filename='namespace-recovery.json'):
    result={'passed':False,'mock_llm_calls':0,'checks':[]}
    def save():(api.root/filename).write_text(json.dumps(result,indent=2)+'\n','utf8')
    result['cancel_after_exit']=cancel_after_exit
    def record(text):result['checks'].append(text);save();print('NAMESPACE_PASS '+text,flush=True)
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
        row=await connection.fetchrow('''SELECT j.status,a.id,a.phase,a.reason_code,a.cancel_requested_at,a.worker_identity_json,a.process_identity_json,
          a.child_processes_json,a.write_checkpoint_json,a.output_manifest_json,a.input_manifest_json,a.publication_json
          FROM jobs j JOIN job_attempts a ON a.job_id=j.id WHERE j.id=$1 ORDER BY a.number DESC LIMIT 1''',identifier)
        value=dict(row)
        value['cancel_requested_at']=value['cancel_requested_at'].isoformat() if value['cancel_requested_at'] else None
        return value
    try:
        prior=(await call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)).decode()
        marker=api.root/'detached-heartbeat.txt'
        writer="import signal,time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\np=Path("+repr(wsl_path(marker))+")\nwhile True:\n p.write_text(str(time.time_ns()))\n time.sleep(.05)\n"
        command="import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',"+repr(writer)+"],start_new_session=True)\nprint('ACTUAL_RUNNING_OWNER',flush=True)\ntime.sleep(120)\n"
        job=await call('POST',api.prefix+'/jobs',body={'type':'convert','command':'python3 -c '+shlex.quote(command),
            'runtime':'windows','timeout_seconds':180})
        identifier=job['id'];deadline=time.monotonic()+60
        while True:
            current=await call('GET','/jobs/'+str(identifier))
            # Remote S3 logs are finalized later; the real writer's marker and
            # durable running registration establish that business code ran.
            if current['status']=='running' and current['attempt'].get('process_identity') and marker.exists():break
            assert time.monotonic()<deadline,'Actual command did not start'
            await asyncio.sleep(.2)
        before=await snapshot(identifier)
        rows=await connection.fetch('SELECT id,seq,payload_json FROM durable_events WHERE attempt_id=$1 AND type=$2',
            before['id'],'attempt.linux_namespace_prepared')
        assert len(rows)==1
        namespace=json.loads(rows[0]['payload_json'])
        assert namespace['process']==json.loads(before['process_identity_json']) and namespace['init']['uid']==65534
        assert (await probe_native('vibeflow-linux',namespace['init']))['state']=='match'
        first=marker.read_text();await asyncio.sleep(.2);assert marker.read_text()!=first
        # The real host /proc view identifies all members of this exact namespace.
        code="import sys,json;from pathlib import Path;sys.path.insert(0,"+repr(wsl_path(baseline.SERVER))+");from app.services.process_identity_native import observe\nrows=[]\nfor p in Path('/proc').iterdir():\n if p.name.isdigit():\n  try:\n   if str((p/'ns/pid').stat().st_ino)=="+repr(namespace['init']['pid_namespace'])+":rows.append(observe(int(p.name)))\n  except (OSError,ValueError):pass\nprint(json.dumps(rows))"
        scan=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','--exec','python3','-c',code],
            capture_output=True,text=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        assert scan.returncode==0
        members=json.loads(scan.stdout);assert len(members)>=4
        result.update(job_id=identifier,before=before,namespace_event=dict(rows[0]),members_before=members)
        record('actual running namespace contains PID 1, command and detached SIGTERM-ignoring writer')
        (worker.root/'crash').touch()
        process=worker.process
        await asyncio.to_thread(process.wait,30);worker.log.close();worker.process=None
        assert process.returncode==17
        owner_exit=await probe_native('vibeflow-linux',worker.birth);assert owner_exit['state']=='exited'
        result['crash']={'birth':worker.birth,'exit_code':17,'observation':owner_exit}
        if cancel_after_exit:
            await call('POST','/jobs/'+str(identifier)+':cancel')
            pending=await snapshot(identifier);result['cancelled_before_recovery']=pending;save()
            assert pending['status']=='cancelled' and pending['phase']=='cancelling' and pending['cancel_requested_at']
        expected='cancelled' if cancel_after_exit else 'interrupted'
        await asyncio.to_thread(worker.start)
        deadline=time.monotonic()+90
        while True:
            after=await snapshot(identifier)
            result['after']=after;save()
            if after['status']==expected and after['write_checkpoint_json']!='{}':break
            assert time.monotonic()<deadline,'Running owner loss did not recover its drain'
            await asyncio.sleep(.25)
        checkpoint=json.loads(after['write_checkpoint_json'])
        assert after['phase']==expected and after['reason_code']==('cancel_requested' if cancel_after_exit else 'recovery_required')
        if cancel_after_exit:assert after['cancel_requested_at']==pending['cancel_requested_at']
        assert checkpoint['schema']=='vibeflow.job-write-checkpoint.v4'
        assert checkpoint['stop'] is None and checkpoint['recovery']['schema']=='vibeflow.job-linux-scope-recovery.v1'
        assert json.loads(after['output_manifest_json']).get('process_stop') is None
        for field in ('id','worker_identity_json','process_identity_json','child_processes_json','input_manifest_json','publication_json'):
            assert before[field]==after[field],field
        assert await connection.fetchval('SELECT count(*) FROM job_attempts WHERE job_id=$1',identifier)==1
        absent=[]
        for birth in members:
            observation=await probe_native('vibeflow-linux',birth)
            assert observation['state']=='exited';absent.append({'birth':birth,'observation':observation})
        stable=marker.read_text();await asyncio.sleep(1.2);assert marker.read_text()==stable
        assert (await call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)).decode()==prior
        event=await connection.fetch('SELECT payload_json FROM durable_events WHERE attempt_id=$1 AND type=$2',
            before['id'],'attempt.content_scope_recovered')
        assert len(event)==1 and json.loads(event[0]['payload_json'])['replayed'] is False
        result.update(after=after,members_after=absent,recovery_event=json.loads(event[0]['payload_json']))
        record('new worker confirms original namespace retirement, preserves missing stop facts and original published output')
        checked=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec',baseline.PYTHON,
            '-B','-c','SERVER='+repr(wsl_path(baseline.SERVER))+'\n'+CERTIFICATE_CHECK,
            wsl_path(worker.root/'settings.json'),str(identifier)],capture_output=True,text=True,timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        assert checked.returncode==0,checked.stderr[-1500:]
        result['certificate_checks']=json.loads(checked.stdout)
        fresh=await call('POST',api.prefix+'/jobs',body={'type':'convert','runtime':'windows','timeout_seconds':180,
            'command':"python3 -c "+shlex.quote("from pathlib import Path;import secrets;Path('Ref/proof.txt').write_text('RETIREMENT_'+secrets.token_hex(16)+'\\n')")})
        deadline=time.monotonic()+90
        while True:
            fresh=await call('GET','/jobs/'+str(fresh['id']))
            if fresh['status']=='succeeded' and fresh['attempt']['outputs'].get('completion',{}).get('state')=='settled':break
            assert fresh['status'] not in {'failed','cancelled','interrupted'}
            assert time.monotonic()<deadline
            await asyncio.sleep(.25)
        result['fresh_job']=fresh
        record('explicit fresh task succeeds after recovered drain; original command was not replayed')
        result['passed']=True;save()
    except BaseException as error:
        result['failure_type']=type(error).__name__;save();raise
    finally:await connection.close();save()


async def verify(configuration,selected,admin,observed):
    return await baseline.verify(configuration,selected,admin,observed,worker_factory=CrashWorker,after_publish=scenario)


if __name__=='__main__':
    asyncio.run(baseline.main(entry='platform/server/live_llm_remote_namespace_acceptance.py',verifier=verify))
