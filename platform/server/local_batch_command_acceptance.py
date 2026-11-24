"""Actual legacy commands through local BatchState; no model round is exercised."""
import ast,hashlib,json,time,uuid
from pathlib import Path

from live_llm_local_prepared_batch_acceptance import FIXTURE
from live_llm_failure_fallback_acceptance import wait_for
from testsupport import IsolatedServer


def main():
    api=IsolatedServer('local-batch-commands',app='local_command_fixture:app')
    code='from app.main import app\n'+FIXTURE;ast.parse(code)
    (api.root/'local_command_fixture.py').write_text(code,encoding='utf8')
    (api.root/'entry-source.py').write_bytes(Path(__file__).read_bytes())
    report={'passed':False,'model_calls':0,'mock_llm_calls':0,'kind':'actual_OS_SQL_legacy_command_compatibility'}
    print('LOCAL_BATCH_COMMAND_EVIDENCE '+str(api.root),flush=True)
    try:
        with api:
            api.create_case('Actual legacy local command compatibility')
            role=api.call('GET','/testing/local-batch-role')
            assert role['delivery_stopped'] and not role['distributed']
            api.call('POST','/testing/local-batch-quota',{'job_concurrent':1})
            tokens=['ACTUAL_LOCAL_COMMAND_'+uuid.uuid4().hex for _ in range(2)]
            body={'commands':['python -c "import time;time.sleep(1);print(\''+token+'\')"' for token in tokens],
                'runtime':'windows','max_concurrent':2}
            key=uuid.uuid4().hex
            batch=api.call('POST',api.prefix+'/jobs:batch',body,expect=202,extra_headers={'Idempotency-Key':key})
            maximum=0;observed_owner=False
            def observed():
                nonlocal maximum,observed_owner
                proof=api.call('GET',api.prefix+'/testing/local-batch/'+batch['id'])
                active=[row for row in proof['jobs'] if row['phase'] not in {'queued','succeeded','failed','cancelled','interrupted'}]
                maximum=max(maximum,len(active))
                assert len(active)<=1
                for row in active:
                    assert proof['active_local_batch'] and row['worker']['pid']==role['pid'];observed_owner=True
                current=api.call('GET',api.prefix+'/job-batches/'+batch['id'])
                assert current['status'] not in {'failed','cancelled'}
                return current if current['status']=='succeeded' else None
            wait_for(observed,'Actual legacy local commands did not finish',120)
            proof=api.call('GET',api.prefix+'/testing/local-batch/'+batch['id'])
            for item,token in zip(batch['jobs'],tokens):
                job=api.wait_job(item['id']);assert token in job['log_backlog']
                wait_for(lambda:api.call('GET','/testing/workflow-job-drain/'+str(item['id']))['verified'],
                    'Actual legacy local command did not drain')
            assert maximum==1 and observed_owner
            assert all(row['mode']=='local' and row['submission_spec_equal'] and row['claimed']
                and len(row['launch_calls'])==1 and row['launch_calls'][0]['spec']==row['spec'] for row in proof['jobs'])
            replay=api.call('POST',api.prefix+'/jobs:batch',body,expect=202,extra_headers={'Idempotency-Key':key})
            assert replay['id']==batch['id'] and [row['id'] for row in replay['jobs']]==[row['id'] for row in batch['jobs']]
            report.update(passed=True,role=role,actual_jobs=[row['id'] for row in batch['jobs']],
                exact_spec_evidence=proof,user_limit=1,batch_limit=2,actual_maximum_active=maximum,
                same_key_same_jobs=True,actual_native_stdout_verified=True)
    finally:
        report['api_exited']=api.proc is not None and api.proc.poll() is not None
        report['production_source_sha256']=hashlib.sha256((Path(__file__).parent/'app/services/batch_jobs.py').read_bytes()).hexdigest()
        (api.root/'acceptance.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print('LOCAL_BATCH_COMMAND_OK '+str(api.root),flush=True)


if __name__=='__main__':main()
