"""Observe same-case finalization before and after actual log writer exit."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from app.services.process_identity_native import probe
from job_log_streaming_smoke import FIXTURE, arguments, state, submit, verified_bytes, wait_for
from s3testsupport import ObjectStore
from testsupport import SERVER, IsolatedServer


TRACE = r'''
import traceback
from app.services import job_drain_recovery, publication_source_validation

acquire = publication_source_validation.SourceValidation.acquire

async def traced_acquire(self, status, prepared):
    try:
        return await acquire(self, status, prepared)
    except Exception as error:
        cause = error.__cause__
        inner = cause.__cause__ if cause else None
        record('source_validation_failure', job_id=self.owner.job_id,
               error=type(error).__name__, code=getattr(error,'code',None),
               cause=getattr(cause,'code',None), requires_recovery=getattr(cause,'requires_recovery',False),
               inner=str(inner) if inner else None,
               frames=[item.name for item in traceback.extract_tb(cause.__traceback__)] if cause else [])
        raise

publication_source_validation.SourceValidation.acquire = traced_acquire

@app.post('/fixture/historical-drain/{job_id}')
async def historical_drain(job_id:int, user=Depends(get_current_user)):
    await load_ticket(job_id,user)
    return {'result':await job_drain_recovery.reconcile(job_id)}
'''


def submit_small(api):
    api.put('Ref/native_stdout.py','import sys\nsys.stdout.buffer.write(b"original stdout\\n")\n')
    command = subprocess.list2cmdline([sys.executable,'-I','-B','Ref/native_stdout.py'])
    return api.call('POST',api.prefix+'/jobs',{'type':'shell','runtime':'windows','command':command,
        'input_paths':['Ref/native_stdout.py'],'max_log_bytes':1024,'timeout_seconds':90})['id']


def checkpoint(api, job_id):
    uri = (api.root/'test.sqlite').resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True)) as db:
        result = db.execute('SELECT a.write_checkpoint_json,a.reason_code FROM job_attempts a WHERE a.job_id=?',
                            (job_id,)).fetchone()
        events = db.execute('SELECT d.type,d.payload_json FROM durable_events d JOIN job_attempts a '
                            'ON a.id=d.attempt_id WHERE a.job_id=? ORDER BY d.seq',(job_id,)).fetchall()
    return {'checkpoint':json.loads(result[0]),'reason':result[1],
            'events':[{'type':name,'payload':json.loads(payload)} for name,payload in events]}


def trace(api):
    return [json.loads(line) for line in (api.root/'late-log-trace.jsonl').read_text(encoding='utf8').splitlines()]


def main():
    fixed = os.environ.get('VIBEFLOW_JOB_LOG_FOLLOWUP_FIXED','1')=='1'
    api = IsolatedServer('job-log-followup',app='followup_fixture:app',access_log=True)
    store = ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
                        volume_max=8,authenticated=True)
    report = {'passed':False,'api':str(api.root),'s3':str(store.root),'fixed':fixed,
              'model_calls':0,'mock_llm_calls':0}
    try:
        with store:
            scratch = api.root/'native-temp'
            scratch.mkdir()
            env = {**store.settings(),'TEMP':str(scratch),'TMP':str(scratch),'TMPDIR':str(scratch)}
            (api.root/'followup_fixture.py').write_text('import os\nos.environ.update('+repr(env)+')\n'+FIXTURE+TRACE,
                                                       encoding='utf8')
            with api:
                api.create_case('Same-case log recovery')
                user = api.call('GET','/auth/me')['email']
                birth = api.call('GET','/fixture/identity',api_prefix=False)
                (api.root/'hold-response').touch()
                (api.root/'lose-response').touch()
                original = submit_small(api)
                job = api.wait_job(original,timeout=55,expected='failed')
                assert job['attempt']['reason_code']=='sync_failed', job
                pending = state(api,original)
                content = b'original stdout\n'
                assert pending['state']=='active' and pending['remaining_bytes']==len(content), pending
                remote = verified_bytes(store,arguments(store,pending))
                assert remote['sha256']==hashlib.sha256(content).hexdigest(), remote
                assert any(item['name']=='put' and item['status']==200 for item in trace(api))
                assert api.call('POST',f'/fixture/reconcile/{original}',api_prefix=False)=={'result':None}
                settled = state(api,original)
                assert settled['state']=='materialized' and settled['remaining_bytes']==0, settled
                original_stop = settled['output_manifest_json']['process_stop']
                assert original_stop['verified'] and original_stop['scope_observation']['complete'], original_stop
                assert checkpoint(api,original)['checkpoint']=={}
                live = api.call('POST',f'/fixture/historical-drain/{original}',api_prefix=False)
                assert live=={'result':'owner_live'} and probe(birth)['state']=='match', live
                report['original'] = {'job_id':original,'attempt_id':settled['attempt_id'],
                    'remote':remote,'active_bytes':pending['remaining_bytes'],'settled_bytes':settled['remaining_bytes'],
                    'physical_stop_verified':True,'historical_recovery_while_live':live['result']}

                blocked = submit(api,empty=True)
                def observe_blocked():
                    selected = api.call('GET',f'/jobs/{blocked}')
                    return selected if selected['completion_error'] or selected['status']=='failed' else None
                public = wait_for(observe_blocked,bool,60)
                blocked_row = state(api,blocked)
                drained = checkpoint(api,blocked)
                assert blocked_row['state']=='materialized' and blocked_row['size']==0, blocked_row
                assert drained['checkpoint']['stop']['verified'], drained
                failure = next(item for item in trace(api)
                               if item['name']=='source_validation_failure' and item['job_id']==blocked)
                assert failure['code']=='recovery_required' and failure['cause']=='validation_job_busy', failure
                assert failure['requires_recovery'] and 'require_drained_locked' in failure['frames'], failure
                assert failure['error']==('JobHistoryRecoveryRequired' if fixed else 'ExecutionConflict'), failure
                if fixed:
                    assert public['status']=='failed' and public['attempt']['reason_code']=='recovery_required', public
                    assert public['completion_error'] is None, public
                    assert blocked_row['output_manifest_json']['process_stop']==drained['checkpoint']['stop']
                    assert public['attempt']['process_stop']['verified']
                    assert any(event['type']=='attempt.process_stopped' for event in drained['events'])
                else:
                    assert public['status']=='running' and public['attempt']['phase']=='finalizing', public
                    assert public['completion_error']=='Job finalization did not complete: ExecutionConflict', public
                report['same_case_before_exit'] = {'job':public,'failure':failure,'empty_log':True,
                                                   'actual_content_drained':True}
            report['original_api_exit'] = wait_for(lambda:probe(birth),lambda row:row['state']=='exited',12)
            (api.root/'api.log').replace(api.root/'original-api.log')

            with api:
                api.token = api.call('POST','/auth/login',{'email':user,'password':'isolated-test-123'})['token']
                peer_birth = api.call('GET','/fixture/identity',api_prefix=False)
                recovered = checkpoint(api,original)
                witness = recovered['checkpoint']
                assert witness['schema']=='vibeflow.job-write-checkpoint.v2', recovered
                assert witness['stop']==original_stop and witness['recovery']['owner_observation']==report['original_api_exit']
                assert state(api,original)['status']=='failed'
                blocked_after_restart = state(api,blocked)['phase']
                assert blocked_after_restart==('failed' if fixed else 'finalizing'), blocked_after_restart
                new = submit(api,empty=True)
                public = api.wait_job(new,timeout=55)
                new_row = state(api,new)
                assert public['status']=='succeeded' and new_row['state']=='materialized' and new_row['size']==0
                assert new_row['output_manifest_json']['process_stop']['verified']
                launches = {job_id:sum(event['type']=='attempt.launch_prepared' for event in checkpoint(api,job_id)['events'])
                            for job_id in (original,blocked,new)}
                assert all(count==1 for count in launches.values()), launches
                report['after_startup_recovery'] = {'original_checkpoint':witness['schema'],
                    'original_failure_preserved':True,'original_stop_preserved':True,'new_job':new,'status':public['status'],
                    'prior_blocked_phase':blocked_after_restart,'native_launches':launches}
            report['peer_api_exit'] = wait_for(lambda:probe(peer_birth),lambda row:row['state']=='exited',12)
        assert store.proc.poll() is not None
        report['object_store_launcher_exited'] = True
        report['passed'] = True
    except BaseException as error:
        report.update(error=type(error).__name__,detail=str(error)[:4000]);raise
    finally:
        path = api.root/'job-log-followup.json'
        path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
        print(f'JOB_LOG_FOLLOWUP_RESULT {path}',flush=True)


if __name__=='__main__':
    main()
