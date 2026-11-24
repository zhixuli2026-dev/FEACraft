"""Actual Linux worker death after durable preparation; startup and periodic recovery."""
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import time

import asyncpg

import live_llm_remote_worker_acceptance as baseline
from app.services.owned_process import probe_native, wsl_path

FAULT = '''
from app.services import job_execution,publication_finalizing
original_finish=job_execution._finish_owned
async def faulted_finish(owner,*args,**kwargs):
 if Path(FAULT_PATH).exists() and kwargs.get('recovery') is None:
  Path(FAULT_PATH).unlink()
  Path(CRASH_PATH).write_text(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id,'exit_code':17}))
  os._exit(17)
 return await original_finish(owner,*args,**kwargs)
job_execution._finish_owned=faulted_finish
original_ready=ready
async def ready(ctx):
 await original_ready(ctx)
 async def audit():
  while True:
   value={'passes':ctx.get('recovery_passes',0),'outcomes':ctx.get('recovery_outcomes',{}),
    'error':ctx.get('recovery_error'),'finalization_tasks':len(publication_finalizing._tasks)}
   Path(AUDIT_PATH).write_text(json.dumps(value))
   await asyncio.sleep(.1)
 ctx['recovery_audit']=asyncio.create_task(audit())
original_create_worker=create_worker
def create_worker(**kwargs):
 if OBSERVER:kwargs['queue_name']='vibeflow:recovery-observer'
 return original_create_worker(**kwargs)
'''


class FaultWorker(baseline.LinuxWorker):
    observer = False

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.api.env['VIBEFLOW_JOB_HEARTBEAT_SECONDS']='1'
        self.crashes=[]

    def fixture_code(self, root):
        return ('FAULT_PATH='+repr(wsl_path(root/'fault'))+'\nCRASH_PATH='+repr(wsl_path(root/'crash.json'))+
            '\nAUDIT_PATH='+repr(wsl_path(root/'recovery-audit.json'))+'\nOBSERVER='+repr(self.observer)+'\n'+FAULT)

    async def retired_crash(self):
        await asyncio.to_thread(self.process.wait,60)
        self.log.close()
        exit_code=self.process.returncode
        observed=await probe_native('vibeflow-linux',self.birth)
        assert observed['state']=='exited'
        receipt={'birth':self.birth,'observation':observed,'launcher_exit_code':exit_code,
            'marker':json.loads((self.root/'crash.json').read_text()),'root':str(self.root)}
        self.crashes.append(receipt)
        self.process=None
        assert exit_code==17, 'Expected actual worker exit at prepared finalization boundary'
        return receipt


def audit(worker):
    try:return json.loads((worker.root/'recovery-audit.json').read_text())
    except (FileNotFoundError,json.JSONDecodeError):return {}


async def scenario(api, worker, selected):
    result={'passed':False,'mock_llm_calls':0,'checks':[],'recoveries':[]}
    def save():(api.root/'owner-recovery.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
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
        row=await connection.fetchrow('''SELECT j.status,a.id,a.phase,a.worker_identity_json,a.process_identity_json,
          a.write_checkpoint_json,a.output_manifest_json,a.input_manifest_json
          FROM jobs j JOIN job_attempts a ON a.job_id=j.id WHERE j.id=$1 ORDER BY a.number DESC LIMIT 1''',identifier)
        fields=dict(row)
        fields['input_sha256']=hashlib.sha256(fields.pop('input_manifest_json').encode()).hexdigest()
        fields['attempt_count']=await connection.fetchval('SELECT count(*) FROM job_attempts WHERE job_id=$1',identifier)
        return fields
    async def crashed_job(target):
        (target.root/'fault').write_text('actual exit before SQL terminal commit','utf8')
        command="from pathlib import Path\nimport secrets,os\nassert os.getuid()==65534\nPath('Ref/proof.txt').write_text('RETIREMENT_'+secrets.token_hex(16)+'\\n')\n"
        job=await call('POST',api.prefix+'/jobs',body={'type':'convert','command':'python3 -c '+shlex.quote(command),
            'runtime':'windows','timeout_seconds':120})
        receipt=await target.retired_crash()
        assert receipt['marker']['job_id']==job['id']
        before=await snapshot(job['id'])
        assert before['status']=='running' and before['phase']=='finalizing'
        checkpoint=json.loads(before['write_checkpoint_json'])
        assert checkpoint['state']=='stopped' and checkpoint['stop']['verified']
        assert 'finalization_intent' in json.loads(before['output_manifest_json'])
        root=json.loads(before['process_identity_json'])['host']
        assert (await probe_native('vibeflow-linux',root))['state']=='exited'
        return job['id'],before,receipt
    async def recovered(identifier,before,observer,previous_passes,label):
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            after=await snapshot(identifier)
            output=json.loads(after['output_manifest_json'])
            observed=audit(observer)
            if (after['status']=='succeeded' and output.get('completion',{}).get('state')=='settled'
                    and observed.get('passes',0)>previous_passes and observed.get('finalization_tasks')==0):break
            await asyncio.sleep(.25)
        else:raise TimeoutError('Actual execution-host recovery did not complete: '+label)
        assert after['phase']=='succeeded' and before['attempt_count']==after['attempt_count']==1
        for key in ('id','input_sha256','worker_identity_json','process_identity_json','write_checkpoint_json'):
            assert before[key]==after[key],key
        before_intent=json.loads(before['output_manifest_json'])['finalization_intent']
        assert output['finalization_intent']==before_intent
        events=await connection.fetch('SELECT payload_json FROM durable_events WHERE attempt_id=$1 AND type=$2',after['id'],'attempt.finalization_recovered')
        assert len(events)==1
        proof=json.loads(events[0]['payload_json'])
        assert proof['owner_observation']=={'state':'exited','reason':'process_exited'}
        assert proof['native_execution_started'] is False and proof['replayed'] is False
        assert proof['bundle_sha256']==before_intent['bundle_sha256']
        result['recoveries'].append({'job_id':identifier,'before':before,'after':after,'recovery_event':proof,
            'observer':observer.birth,'audit':observed,'previous_passes':previous_passes,'kind':label})
        result['checks'].append(label);save();print('OWNER_RECOVERY_PASS '+label,flush=True)
    observer=None
    try:
        identifier,before,receipt=await crashed_job(worker)
        # The submitting Windows API is still alive, but cannot observe this
        # Linux worker's local process birth. A new Linux worker supplies it.
        await asyncio.sleep(1)
        assert (await snapshot(identifier))['status']=='running'
        await asyncio.to_thread(worker.start)
        await recovered(identifier,before,worker,0,'new execution-host worker resumes the prepared commit after actual owner exit')
        result['crashes']=[receipt]

        observer=FaultWorker(api,worker.broker,worker.store,selected,worker.observed)
        observer.index=100;observer.observer=True
        await asyncio.to_thread(observer.start)
        deadline=time.monotonic()+30
        while audit(observer).get('passes',0)<1:
            assert time.monotonic()<deadline
            await asyncio.sleep(.1)
        passes=audit(observer)['passes']
        identifier,before,receipt=await crashed_job(worker)
        await recovered(identifier,before,observer,passes,'already-running worker periodically recovers a peer without restart')
        result['crashes'].append(receipt)
        await observer.stop()
        result['observer_stops']=observer.receipts
        await asyncio.to_thread(worker.start)
        result['passed']=True;save()
    except BaseException as error:
        result['failure_type']=type(error).__name__;save();raise
    finally:
        try:
            if observer is not None and observer.process is not None:await observer.stop()
        finally:await connection.close();save()


async def verify(configuration,selected,admin,observed):
    return await baseline.verify(configuration,selected,admin,observed,worker_factory=FaultWorker,after_publish=scenario)


if __name__=='__main__':
    asyncio.run(baseline.main(entry='platform/server/live_llm_remote_worker_recovery_acceptance.py',verifier=verify))
