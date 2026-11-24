"""Kill an isolated API with actual queued, running and finalizing Jobs."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sys
from urllib.error import URLError
import uuid

from app.services import process_identity_native as identity
from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
from live_llm_job_repeat_acceptance import main, put
from live_llm_outbox_restart_acceptance import restart
from live_llm_publication_restart_acceptance import FIXTURE as PID_FIXTURE, reboot


FIXTURE = PID_FIXTURE + '''
import asyncio
import json
from fastapi import Depends
from sqlalchemy import select
from app.db import get_db
from app.deps import get_current_user
from app.models import Job, DurableEvent
from app.routers.jobs import _get_job, _latest_attempt
from app.routers.workspaces import get_owned_case
from app.services import job_execution, jobrunner
from app.services.durable_events import lock_case_execution

root = Path(__file__).parent
original_phase = job_execution.phase
async def phase(owner,target,**kwargs):
    value = await original_phase(owner,target,**kwargs)
    if target=='finalizing' and (root/'hold-finalization').exists():
        (root/'finalizing-job').write_text(str(owner.job_id))
        while (root/'hold-finalization').exists():
            await asyncio.sleep(.05)
    return value
job_execution.phase = phase

original_start = jobrunner.start_job
async def start(job_id,command,*args,**kwargs):
    if 'orphan-queued.py' in command and (root/'hold-queue').exists():
        (root/'queued-job').write_text(str(job_id))
        while (root/'hold-queue').exists():
            await asyncio.sleep(.05)
    return await original_start(job_id,command,*args,**kwargs)
jobrunner.start_job = start

@app.get('/api/v1/testing/recovery/job/{job_id}')
async def evidence(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    job = await _get_job(job_id,user,db,write=False)
    attempt = await _latest_attempt(db,job_id)
    events = (await db.scalars(select(DurableEvent).where(DurableEvent.case_id==job.case_id,
        DurableEvent.attempt_id==(attempt.id if attempt else None)).order_by(DurableEvent.seq))).all()
    return {'id':job.id,'status':job.status,'exit_code':job.exit_code,
        'ended_at':job.ended_at.isoformat() if job.ended_at else None,
        'phase':attempt.phase if attempt else None,'attempt_id':attempt.id if attempt else None,
        'claimed':attempt.claim_token is not None if attempt else False,
        'output':attempt.output_manifest_json if attempt else None,
        'native':json.loads(attempt.process_identity_json).get('host') if attempt else None,
        'events':[{'id':row.id,'type':row.type,'payload':json.loads(row.payload_json)} for row in events]}

@app.post('/api/v1/testing/recovery/corrupt/{job_id}')
async def corrupt(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    job = await _get_job(job_id,user,db)
    await lock_case_execution(db,job.case_id)
    attempt = await _latest_attempt(db,job_id)
    assert attempt.phase=='finalizing' and job.status=='running'
    attempt.output_manifest_json = '{unreadable-historical-output'
    await db.commit()
    return {'retained':attempt.output_manifest_json}

@app.post('/api/v1/testing/recovery/legacy/{case_id}')
async def legacy(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    await get_owned_case(case_id,user,db)
    job = Job(case_id=case_id,type='shell',command='legacy queue entry',status='queued')
    db.add(job)
    await db.commit()
    return {'id':job.id}

@app.get('/api/v1/testing/recovery/startup')
async def startup_result(user=Depends(get_current_user)):
    return app.state.job_recovery
'''


def scenario(api, *, store=None, **_):
    root = api.root
    report = {'storage':'s3' if store else 'fs','restarts':[]}
    output = root/'job-orphan-restart.json'

    def save():
        output.write_text(json.dumps(report,indent=2),encoding='utf8')

    def submit(prefix, path, token):
        return api.call('POST',prefix+'/jobs',{'runtime':'windows','type':'shell',
            'command':f'"{sys.executable}" "{path}"','timeout_seconds':100},
            extra_headers={'Authorization':'Bearer '+token,'Idempotency-Key':uuid.uuid4().hex})

    def evidence(job_id, token):
        return api.call('GET',f'/testing/recovery/job/{job_id}',
            extra_headers={'Authorization':'Bearer '+token})

    def detail(job_id, token):
        return api.call('GET',f'/jobs/{job_id}',extra_headers={'Authorization':'Bearer '+token})

    damaged_token = api.token
    damaged_path = 'Ref/orphan-damaged.py'
    put(api,damaged_path,'print("Actual early finalization")\n')
    (root/'hold-finalization').touch()
    damaged_id = submit(api.prefix,damaged_path,damaged_token)['id']
    wait_for(lambda:(root/'finalizing-job').exists(),'Actual Job never entered finalization',30)
    assert int((root/'finalizing-job').read_text())==damaged_id

    api.create_case('Running orphan recovery')
    running_token = api.token
    started = root/'running-starts.txt'
    running_path = 'Ref/orphan-running.py'
    put(api,running_path,f'from pathlib import Path\nimport time\nwith Path({str(started)!r}).open("a") as f:f.write("once\\n")\n'
        'time.sleep(85)\n')
    running_id = submit(api.prefix,running_path,running_token)['id']
    wait_for(lambda:started.exists() and started.read_text().strip()=='once','Native job did not start',35)
    running_before = evidence(running_id,running_token)
    assert running_before['phase']=='running' and identity.probe(running_before['native'])['state']=='match'

    api.create_case('Durable queue recovery')
    queued_token = api.token
    queued_path = 'Ref/orphan-queued.py'
    queue_starts = root/'queued-starts.txt'
    put(api,queued_path,f'from pathlib import Path\nwith Path({str(queue_starts)!r}).open("a") as f:f.write("once\\n")\n'
        'print("Durable queue executed")\n')
    legacy_id = api.call('POST',f'/testing/recovery/legacy/{api.case_id}')['id']
    bad = api.call('POST',f'/testing/recovery/corrupt/{damaged_id}',extra_headers={'Authorization':'Bearer '+damaged_token})['retained']
    damaged_before = evidence(damaged_id,damaged_token)
    assert damaged_before['output']==bad and damaged_before['phase']=='finalizing'
    (root/'hold-queue').touch()
    with ThreadPoolExecutor(max_workers=1) as requests:
        submitted = requests.submit(submit,api.prefix,queued_path,queued_token)
        wait_for(lambda:(root/'queued-job').exists(),'Submission never reached the durable pre-claim boundary',30)
        queued_id = int((root/'queued-job').read_text())
        queued_before = evidence(queued_id,queued_token)
        assert queued_before['status']=='queued' and queued_before['phase']=='queued' and not queued_before['claimed']
        assert 'attempt.delivery_requested' in {event['type'] for event in queued_before['events']}
        report.update(before={'damaged':damaged_before,'running':running_before,'queued':queued_before,'legacy_id':legacy_id})
        save()
        old_pid = int((root/'api-pid').read_text())
        kill_owned_api(api,old_pid)
        report['original_api_exited'] = old_pid
        save()
        (root/'hold-finalization').unlink()
        (root/'hold-queue').unlink()
        restart(api)
        new_pid = int((root/'api-pid').read_text())
        assert new_pid!=old_pid
        report['restarts'].append([old_pid,new_pid])
        try:
            submitted.result(timeout=10)
        except (URLError, ConnectionError, TimeoutError):
            pass
        else:
            raise AssertionError('Original submission returned before the API exit')
    save()

    recovered = evidence(running_id,running_token)
    assert recovered['attempt_id']==running_before['attempt_id'] and recovered['status']=='interrupted'
    assert recovered['phase']=='interrupted' and recovered['exit_code'] is None and recovered['ended_at']
    assert identity.probe(running_before['native'])['state']=='exited' and started.read_text().splitlines()==['once']
    exits = [event for event in recovered['events'] if event['type']=='attempt.owner_exited']
    assert len(exits)==1 and exits[0]['payload']['replayed'] is False
    assert exits[0]['payload']['observation']=={'state':'exited','reason':'process_exited'}

    expected = api.wait_job(queued_id,timeout=65)
    assert expected['attempt']['id']==queued_before['attempt_id'] and queue_starts.read_text().splitlines()==['once']
    legacy = detail(legacy_id,queued_token)
    assert legacy['status']=='queued' and legacy['recovery']['reason']=='legacy_unverified'
    damaged = detail(damaged_id,damaged_token)
    assert damaged['status']=='running' and damaged['attempt']['phase']=='finalizing'
    assert damaged['recovery']['reason']=='state_recovery_required'
    assert any(issue['field']=='outputs' for issue in damaged['record_issues'])
    saved = evidence(damaged_id,damaged_token)
    assert saved['output']==bad
    assert api.call('GET','/testing/recovery/startup').get('unreadable_output_retained')==1
    api.call('GET',f'/jobs/{running_id}',expect=404)
    api.call('GET',f'/jobs/{damaged_id}',expect=404)

    report['restarts'].append(reboot(api))
    repeated = evidence(running_id,running_token)
    assert [e for e in repeated['events'] if e['type']=='attempt.owner_exited']==exits
    assert detail(legacy_id,queued_token)['recovery']['event_id']==legacy['recovery']['event_id']
    assert detail(damaged_id,damaged_token)['recovery']['event_id']==damaged['recovery']['event_id']
    assert evidence(damaged_id,damaged_token)['output']==bad
    assert detail(queued_id,queued_token)['attempt']['id']==queued_before['attempt_id']
    assert queue_starts.read_text().splitlines()==started.read_text().splitlines()==['once']
    report.update(interrupted_id=running_id,interruption_event=exits[0],queued_id=queued_id,
        queued_attempt_reused=True,damaged_history_preserved=True,legacy_requires_review=True,
        idempotent_second_restart=True,cross_account_http=404)
    save()
    return report


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=6))
