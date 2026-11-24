"""Actual PostgreSQL checkpoint failure, cold-worker recovery and S3 integrity rejection."""
import asyncio,hashlib,json,shlex,time
from pathlib import Path
import asyncpg

import live_llm_remote_worker_acceptance as baseline
from app.services.owned_process import probe_native,wsl_path
from app.storage import _s3_key

OBSERVATION='''
from app.services import job_write_checkpoint,publication_preparation_recovery
from app import storage
original_drain=job_write_checkpoint.record_drained
async def observed_drain(owner,*args,**kwargs):
 try:return await original_drain(owner,*args,**kwargs)
 except Exception as error:
  if 'ACTUAL_DRAIN_COMMIT_FAILURE' in str(error):
   Path(SQL_ERROR).write_text(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id,
    'error_type':type(error).__name__,'sqlstate':getattr(getattr(error,'orig',None),'sqlstate',None),
    'actual_database_error':True}))
  raise
job_write_checkpoint.record_drained=observed_drain
original_copy=publication_preparation_recovery.resume_copy
def observed_copy(records):
 case,job,attempt=records[:3]
 workspace=json.loads(attempt.input_manifest_json)['workspace']
 run=storage._fs_path(case.s3_prefix,workspace['run_path'])
 before=run.exists()
 result=original_copy(records)
 with Path(COPY_AUDIT).open('a') as stream:
  stream.write(json.dumps({'job_id':job.id,'before_run_exists':before,'after_run_exists':run.exists(),
   'bundle_sha256':result.bundle_sha256})+'\\n')
 return result
publication_preparation_recovery.resume_copy=observed_copy
'''


class ObservedWorker(baseline.LinuxWorker):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.api.env['VIBEFLOW_JOB_HEARTBEAT_SECONDS']='1'

    def fixture_code(self,root):
        return 'SQL_ERROR='+repr(wsl_path(root/'sql-error.json'))+'\nCOPY_AUDIT='+repr(wsl_path(root/'copy-audit.jsonl'))+'\n'+OBSERVATION


async def scenario(api,worker,selected):
    result={'passed':False,'mock_llm_calls':0,'checks':[]}
    def save():(api.root/'drain-failure.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
    def record(text):result['checks'].append(text);save();print('DRAIN_FAILURE_PASS '+text,flush=True)
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
          a.write_checkpoint_json,a.output_manifest_json,a.input_manifest_json,a.publication_json
          FROM jobs j JOIN job_attempts a ON a.job_id=j.id WHERE j.id=$1 ORDER BY a.number DESC LIMIT 1''',identifier)
        data=dict(row);data['input_sha256']=hashlib.sha256(data.pop('input_manifest_json').encode()).hexdigest()
        data['attempt_count']=await connection.fetchval('SELECT count(*) FROM job_attempts WHERE job_id=$1',identifier)
        return data
    trigger=False;corrupted=None
    try:
        await connection.execute('''CREATE FUNCTION actual_drain_failure() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.write_checkpoint_json IS DISTINCT FROM OLD.write_checkpoint_json
             AND NEW.write_checkpoint_json::jsonb->>'schema'='vibeflow.job-write-checkpoint.v1' THEN
            RAISE EXCEPTION 'ACTUAL_DRAIN_COMMIT_FAILURE' USING ERRCODE='P0001';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER actual_drain_failure BEFORE UPDATE OF write_checkpoint_json ON job_attempts
        FOR EACH ROW EXECUTE FUNCTION actual_drain_failure();''')
        trigger=True
        result['trigger']=await connection.fetchval("SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgname='actual_drain_failure'")
        command="from pathlib import Path\nimport secrets\nPath('Ref/proof.txt').write_text('RETIREMENT_'+secrets.token_hex(16)+'\\n')\n"
        job=await call('POST',api.prefix+'/jobs',body={'type':'convert','command':'python3 -c '+shlex.quote(command),
            'runtime':'windows','timeout_seconds':180})
        identifier=job['id'];deadline=time.monotonic()+90
        errorfile=worker.root/'sql-error.json'
        while not errorfile.exists():
            assert time.monotonic()<deadline,'Actual SQL checkpoint fault did not occur'
            await asyncio.sleep(.2)
        fault=json.loads(errorfile.read_text());assert fault['job_id']==identifier and fault['sqlstate']=='P0001'
        await asyncio.sleep(1.2)
        before=await snapshot(identifier)
        assert before['status']=='running' and before['phase']=='finalizing' and before['write_checkpoint_json']=='{}'
        output=json.loads(before['output_manifest_json']);assert 'finalization_intent' not in output
        preparation=output['bundle_preparation'];assert preparation['stop']['verified']
        root=json.loads(before['process_identity_json'])['host']
        assert (await probe_native('vibeflow-linux',root))['state']=='exited'
        live_owner=await probe_native('vibeflow-linux',worker.birth);assert live_owner['state']=='match'
        result.update(job_id=identifier,before=before,sql_fault=fault,live_owner_observation=live_owner)
        record('actual PostgreSQL rejects the drain record while original worker remains alive; no premature recovery')
        await worker.stop()
        result['original_worker_exit']=worker.receipts[-1]
        await connection.execute('DROP TRIGGER actual_drain_failure ON job_attempts; DROP FUNCTION actual_drain_failure();')
        trigger=False

        publication=json.loads(before['publication_json'])['publication_id']
        prefix=await connection.fetchval('SELECT c.s3_prefix FROM cases c JOIN jobs j ON j.case_id=c.id WHERE j.id=$1',identifier)
        path='.vibeflow/publications/'+publication+'/files/Ref/proof.txt'
        key=_s3_key(prefix,path);client=worker.store.raw_client
        response=await asyncio.to_thread(client.get_object,Bucket=worker.store.bucket,Key=key)
        original=response['Body'].read();response['Body'].close()
        assert original.startswith(b'RETIREMENT_')
        await asyncio.to_thread(client.put_object,Bucket=worker.store.bucket,Key=key,Body=b'ACTUAL_CORRUPTED_BUNDLE\n')
        corrupted=(key,original)
        previous=await connection.fetchval('SELECT coalesce(max(seq),0) FROM durable_events WHERE attempt_id=$1',before['id'])
        await asyncio.to_thread(worker.start)
        deadline=time.monotonic()+60
        while True:
            rows=await connection.fetch('SELECT seq,payload_json FROM durable_events WHERE attempt_id=$1 AND seq>$2 AND type=$3',
                before['id'],previous,'job.recovery_required')
            refusal=next((dict(r) for r in rows if json.loads(r['payload_json']).get('reason')=='publication_recovery_required'),None)
            if refusal:break
            assert time.monotonic()<deadline,'Corrupt immutable bundle was not refused'
            await asyncio.sleep(.25)
        blocked=await snapshot(identifier)
        assert blocked['status']=='running' and blocked['write_checkpoint_json']=='{}'
        result['corrupt_bundle_refusal']=refusal
        record('cold worker refuses an actually corrupted S3 bundle without fabricating drain evidence')
        await asyncio.to_thread(client.put_object,Bucket=worker.store.bucket,Key=key,Body=original)
        corrupted=None
        deadline=time.monotonic()+120
        while True:
            after=await snapshot(identifier)
            if after['status']=='succeeded' and json.loads(after['output_manifest_json']).get('completion',{}).get('state')=='settled':break
            assert time.monotonic()<deadline,'Recovery did not settle after original immutable bytes were restored'
            await asyncio.sleep(.3)
        checkpoint=json.loads(after['write_checkpoint_json'])
        assert checkpoint['schema']=='vibeflow.job-write-checkpoint.v3' and checkpoint['stop']==preparation['stop']
        assert checkpoint['recovery']['owner_observation']=={'state':'exited','reason':'process_exited'}
        assert before['attempt_count']==after['attempt_count']==1
        for field in ('id','input_sha256','worker_identity_json','process_identity_json','publication_json'):assert before[field]==after[field],field
        copies=[json.loads(line) for line in (worker.root/'copy-audit.jsonl').read_text().splitlines()]
        assert copies and all(not r['before_run_exists'] and not r['after_run_exists'] for r in copies)
        assert all(r['bundle_sha256']==preparation['bundle_sha256'] for r in copies)
        events=await connection.fetch('SELECT type,payload_json FROM durable_events WHERE attempt_id=$1 AND type=ANY($2::varchar[])',
            before['id'],['attempt.bundle_copy_drained','attempt.finalization_recovered'])
        assert len(events)==2 and {r['type'] for r in events}=={'attempt.bundle_copy_drained','attempt.finalization_recovered'}
        final=next(json.loads(r['payload_json']) for r in events if r['type']=='attempt.finalization_recovered')
        assert final['native_execution_started'] is False and final['replayed'] is False
        result.update(after=after,copy_observations=copies,recovery_events=[dict(r) for r in events],
            restored_output_sha256=hashlib.sha256(original).hexdigest())
        record('real periodic recovery verifies the restored bundle and records a distinct drain certificate with no old run directory')
        result['passed']=True;save()
    except BaseException as error:
        result['failure_type']=type(error).__name__;save();raise
    finally:
        try:
            if corrupted:
                key,data=corrupted
                await asyncio.to_thread(worker.store.raw_client.put_object,Bucket=worker.store.bucket,Key=key,Body=data)
            if trigger:await connection.execute('DROP TRIGGER actual_drain_failure ON job_attempts; DROP FUNCTION actual_drain_failure();')
        finally:await connection.close();save()


async def verify(configuration,selected,admin,observed):
    return await baseline.verify(configuration,selected,admin,observed,worker_factory=ObservedWorker,after_publish=scenario)


if __name__=='__main__':
    asyncio.run(baseline.main(entry='platform/server/live_llm_remote_drain_failure_acceptance.py',verifier=verify))
