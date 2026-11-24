"""Correlate native job completion, publication and database evidence on S3."""
import asyncio
import json
from pathlib import Path
import sys
import time
import uuid

from live_llm_job_repeat_acceptance import main, put


TRACE = '''
import functools, json, time, traceback
from contextvars import ContextVar
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.models import DurableEvent, JobAttempt
from app.routers.jobs import _get_job
from app.services import artifact_publication, case_validation, execution_storage, job_execution, jobrunner, publication_source_validation

trace_root = Path(__file__).parent
trace_path = trace_root/'finalization-observer.jsonl'
get_settings().execution_workspace_limit_mb = 8
get_settings().job_heartbeat_seconds = 1
get_settings().storage_measurement_timeout_seconds = 60
finishing_job = ContextVar('diagnostic_finishing_job',default=None)
def emit(value):
    with trace_path.open('a', encoding='utf8') as stream:
        stream.write(json.dumps({'time':time.time(),**value})+'\\n')

def observe(target, name):
    original = getattr(target, name)
    @functools.wraps(original)
    async def observed(*args, **kwargs):
        owner = args[1] if name == 'renew_locked' else getattr(args[0], 'owner', args[0])
        marker = {'stage':name,'job_id':getattr(owner, 'job_id', None)}
        started = time.monotonic()
        token = finishing_job.set(args[0].job_id) if name == '_finish_owned' else None
        try:
            value = await original(*args,**kwargs)
        except BaseException as error:
            emit({**marker,'state':'error','kind':type(error).__name__,
                  'code':getattr(error,'code',None),
                  'sqlite_code':getattr(getattr(error,'orig',None),'sqlite_errorname',None),
                  'seconds':time.monotonic()-started,
                  'frames':[[Path(frame.filename).name,frame.name,frame.lineno]
                            for frame in traceback.extract_tb(error.__traceback__)[-8:]]})
            raise
        finally:
            if token is not None:
                finishing_job.reset(token)
        emit({**marker,'state':'returned','seconds':time.monotonic()-started})
        return value
    setattr(target, name, observed)

observe(job_execution,'_finish_owned')
observe(artifact_publication,'verify_files_for_finish')
observe(publication_source_validation.SourceValidation,'inspect')
observe(publication_source_validation.SourceValidation,'require_current')
observe(publication_source_validation.SourceValidation,'release_locked')
observe(case_validation,'renew_locked')
observe(jobrunner.JobProcess,'_run')

snapshot = execution_storage.Watch._snapshot
def observed_snapshot(self, deadline):
    control = trace_root/'storage-gate'
    if finishing_job.get() is None or not control.exists():
        return snapshot(self,deadline)
    mode = control.read_text(encoding='utf8').strip()
    control.unlink()
    assert self.root.resolve().is_relative_to((trace_root/'files').resolve())
    ready = trace_root/'storage-gate-ready.json'
    temporary = ready.with_suffix('.tmp')
    temporary.write_text(json.dumps({'job_id':finishing_job.get(),'mode':mode,'at':time.time(),
        'root':self.root.relative_to(trace_root).as_posix(),'attempt_deadline':self.budget.deadline,
        'measurement_deadline':deadline}))
    temporary.replace(ready)
    if mode in {'measure','expire'}:
        until = self.budget.deadline+12 if mode=='expire' else time.monotonic()+18
        while not (trace_root/'storage-gate-release').exists():
            if time.monotonic() >= until:
                raise TimeoutError('Final disk observation gate was not released')
            time.sleep(.025)
    elif mode == 'exceed':
        # A real, isolated allocation occurs immediately before the filesystem scan.
        with (self.root/'diagnostic-quota-fill.bin').open('xb') as stream:
            stream.truncate(9 * 1024 * 1024)
    else:
        raise AssertionError('Unrecognized diagnostic mode')
    try:
        value = snapshot(self,deadline)
    except Exception as error:
        emit({'stage':'final_storage_sample','state':'error','job_id':finishing_job.get(),
              'mode':mode,'kind':type(error).__name__})
        raise
    emit({'stage':'final_storage_sample','state':'returned','job_id':finishing_job.get(),'bytes':value[0],'mode':mode})
    return value
execution_storage.Watch._snapshot = observed_snapshot

@app.get('/api/v1/testing/finalization/usage/{job_id}')
async def usage(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    job = await _get_job(job_id,user,db,write=False)
    rows = await db.execute(select(DurableEvent.type,DurableEvent.payload_json).join(
        JobAttempt,JobAttempt.id==DurableEvent.attempt_id).where(JobAttempt.job_id==job.id,
        DurableEvent.type.in_(('attempt.resource_usage','attempt.storage_usage','attempt.completion_visible'))))
    return {kind:json.loads(payload) for kind,payload in rows}
'''

PROGRAM = '''print("Time = 0.1")
print("smoothSolver: Solving for Ux, Initial residual = 1e-1, Final residual = 1e-3, No Iterations 3")
print("ExecutionTime = 2.0 s")
print("End")
'''


def trace_report(api):
    source = api.root/'finalization-observer.jsonl'
    return [json.loads(line) for line in source.read_text(encoding='utf8').splitlines()] if source.exists() else []


def read_terminal(api, identifier):
    observations = []
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        state = api.call('GET',f'/jobs/{identifier}')
        view = {key:state.get(key) for key in ('status','phase','completion_pending','completion_error')}
        if not observations or observations[-1] != view:
            observations.append(view)
        if state['status'] not in {'queued','running'} or state['completion_error']:
            return state, observations
        time.sleep(.13)
    raise TimeoutError(f'Job {identifier} did not reach a terminal state')


def scenario(api, *, store=None, **_):
    path = 'Ref/finalizing-diagnostic.py'
    put(api, path, PROGRAM)
    workspace = api.call('GET','/workspaces')[0]['id']
    peer = api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Independent write during final sample'})['id']
    control = api.root/'storage-gate'
    entered = api.root/'storage-gate-ready.json'
    release = api.root/'storage-gate-release'
    jobs = []
    for mode, status in [('observe','succeeded'),('measure','succeeded'),('exceed','failed'),('expire','failed')]:
        if mode != 'observe':
            control.write_text(mode,encoding='utf8')
        submission = {'type':'solve', 'command':f'"{sys.executable}" "{path}"',
                      'runtime':'windows', 'timeout_seconds':35 if mode=='expire' else 80}
        key = uuid.uuid4().hex
        admission_deadline = time.monotonic()+30
        admission_retries = 0
        while True:
            response = api.call('POST',api.prefix+'/jobs',submission,
                extra_headers={'Idempotency-Key':key},expect=(200,409))
            detail = response.get('detail')
            if not detail:
                break
            assert isinstance(detail,dict) and detail.get('code') in {'storage_quota_busy','validation_busy'},detail
            if time.monotonic()>=admission_deadline:
                raise TimeoutError('Actual job admission did not drain: '+detail['code'])
            admission_retries += 1
            time.sleep(.1)
        identifier = response['id']
        writer = None
        after_deadline = None
        if mode in {'measure','expire'}:
            try:
                until = time.monotonic()+48
                while not entered.exists():
                    current = api.call('GET',f'/jobs/{identifier}')
                    assert not current['completion_error'], current['completion_error']
                    if time.monotonic() >= until:
                        raise TimeoutError('Native job did not reach its final disk observation')
                    time.sleep(.08)
                observation = json.loads(entered.read_text())
                assert observation['job_id'] == identifier and observation['mode'] == mode
                if mode=='expire':
                    assert observation['measurement_deadline']==observation['attempt_deadline']
                    while time.monotonic() <= observation['attempt_deadline']+.2:
                        time.sleep(.08)
                    current = api.call('GET',f'/jobs/{identifier}')
                    assert current['attempt']['phase']=='finalizing' and current['completion_error'] is None
                    assert not any(row['stage']=='final_storage_sample' and row['job_id']==identifier
                                   for row in trace_report(api))
                    after_deadline = {'still_finalizing':True,'terminal_waited_for_scan':True,
                                      'measurement_uses_attempt_deadline':True}
                else:
                    started = time.monotonic()
                    updated = api.call('PUT',f'/cases/{peer}/file',
                        {'path':'Ref/during-final-sample.txt','content':'INDEPENDENT_WRITE'},
                        expect=(200,409))
                    writer = {'ok':updated.get('ok') is True,'detail':updated.get('detail'),
                              'seconds':time.monotonic()-started}
                    def lease_renewed():
                        return any(row['stage']=='renew_locked' and row['job_id']==identifier
                                   and row['state']=='returned' and row['time']>=observation['at']
                                   for row in trace_report(api))
                    until = time.monotonic()+5
                    while time.monotonic() < until and not lease_renewed():
                        time.sleep(.05)
                    writer['source_lease_renewed'] = lease_renewed()
                    (api.root/'writer-during-final-sample.json').write_text(json.dumps(writer))
            finally:
                release.touch()
        state, observations = read_terminal(api,identifier)
        run = {'job_id':identifier,'mode':mode,'states':observations,'writer':writer,'admission_retries':admission_retries}
        if after_deadline is not None:
            run['after_deadline'] = after_deadline
            run['usage'] = api.call('GET',f'/testing/finalization/usage/{identifier}')
        jobs.append(run)
        trace = trace_report(api)
        (api.root/'finalization-diagnostic.json').write_text(
            json.dumps({'jobs':jobs,'trace':trace},indent=2),encoding='utf8')
        assert state['status']==status and state['completion_error'] is None, (run,trace[-8:])
        if mode in {'exceed','expire'}:
            assert state['attempt']['reason_code']=='resource_limit'
            assert state['exit_code']==-1
            assert state['attempt']['outputs']['publication']['status']=='failed'
        if mode == 'exceed':
            assert any(row.get('bytes',0)>8*1024*1024 and row.get('job_id')==identifier for row in trace)
        elif mode == 'expire':
            outcome = [row for row in trace if row['stage']=='final_storage_sample' and row['job_id']==identifier]
            assert len(outcome)==1 and outcome[0]['state']=='error' and outcome[0]['kind']=='MeasurementUnavailable', outcome
            assert run['usage']['attempt.resource_usage']['violation']=='timeout'
            assert run['usage']['attempt.storage_usage']['state']=='unverified'
            assert 'attempt.completion_visible' not in run['usage']
        else:
            assert state['parsed_summary']['times']==[.1]
            assert state['parsed_summary']['residuals']=={'Ux':[.001]}
        if mode=='measure':
            assert writer['ok'] and writer['source_lease_renewed'], writer
            assert api.call('GET',f'/cases/{peer}/raw?path=Ref%2Fduring-final-sample.txt',raw=True)==b'INDEPENDENT_WRITE'
        entered.unlink(missing_ok=True)
        release.unlink(missing_ok=True)
    return {'storage':'s3' if store else 'fs','terminal_jobs':jobs,
            'trace':trace_report(api),'record':'finalization-diagnostic.json'}


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=TRACE,store_volume_max=6))
