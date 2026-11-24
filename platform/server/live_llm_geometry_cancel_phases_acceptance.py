"""Cancel the same real geometry workflow at three distinct durable phases."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import json
import uuid

from app.services import process_identity_native
from app.services.windows_process_scope import ProcessHandle
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put


FIXTURE = '''
import asyncio
from fastapi import Depends
from sqlalchemy import select
from app.db import get_db
from app.deps import get_current_user
from app.models import ArtifactPublication, ArtifactPublicationHead, DurableEvent, JobAttempt
from app.routers.jobs import _get_job
from app.services import finalizing_stage, job_launch_registration

async def hold_phase(phase, job_id, observation):
    selected = root/'cancel-phase'
    if not selected.exists() or selected.read_text()!=phase: return
    marker = root/(phase+'-stage.json')
    tmp = marker.with_suffix('.tmp')
    tmp.write_text(json.dumps({'job':job_id,'observation':observation}))
    tmp.replace(marker)
    async with asyncio.timeout(60):
        while not (root/(phase+'-continue')).exists(): await asyncio.sleep(.05)

original_observed = job_launch_registration.observed
async def observed(owner,value):
    await original_observed(owner,value)
    if value['stage']=='created': await hold_phase('starting',owner.job_id,value['host'])
job_launch_registration.observed = observed

original_begin = finalizing_stage.begin
async def begin(proc):
    stage = await original_begin(proc)
    if stage is not None:
        await hold_phase('finalizing',proc.job_id,stage.value['stop'])
    return stage
finalizing_stage.begin = begin

@app.get('/api/v1/testing/geometry-publication/{job_id}')
async def publication_state(job_id: int,user=Depends(get_current_user),db=Depends(get_db)):
    job = await _get_job(job_id,user,db,write=False)
    attempt = (await db.scalars(select(JobAttempt).where(JobAttempt.job_id==job.id))).one()
    pub = (await db.scalars(select(ArtifactPublication).where(ArtifactPublication.attempt_id==attempt.id))).one()
    head = await db.get(ArtifactPublicationHead,(job.case_id,pub.kind))
    states = await db.scalars(select(DurableEvent.payload_json).where(DurableEvent.case_id==job.case_id,
        DurableEvent.attempt_id==attempt.id, DurableEvent.type=='attempt.state_changed').order_by(DurableEvent.seq))
    return {'job':job.status,'attempt':attempt.phase,'attempt_id':attempt.id,'publication':pub.id,
            'publication_status':pub.status,'active':head.active_publication_id,
            'phases':[json.loads(value)['status'] for value in states]}
'''


def geometry_source(root):
    return f'''
import cadquery as cq
import json,os,subprocess,sys
from pathlib import Path

def build_geometry(ctx):
    root = Path({str(root)!r})
    selected = root/'cancel-phase'
    if not selected.exists(): return {{'box':cq.Workplane('XY').box(1,1,1)}}
    phase = selected.read_text()
    if phase=='running':
        print('ACTUAL_RUNNING_PREVIEW',flush=True)
        child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)'])
        pids = [os.getpid(),child.pid]
    else:
        pids = [os.getpid()]
    witness = root/(phase+'-payload.json')
    tmp = witness.with_suffix('.tmp')
    tmp.write_text(json.dumps(pids)); tmp.replace(witness)
    if phase=='running': child.wait(timeout=100)
    return {{'box':cq.Workplane('XY').box(2,2,2)}}
'''


def scenario(api, *, store=None, **_):
    preview = api.prefix+'/geometry:preview'
    raw = api.prefix+'/raw?path=A-geo/scene.json'
    state = '/testing/geometry-publication/'
    seeded = put(api,'A-geo/geometry.py',geometry_source(api.root))
    baseline = seeded.get('auto_preview_job') or api.call('POST',preview)['job_id']
    api.wait_job(baseline)
    active = api.call('GET',state+str(baseline))
    assert active['active']==active['publication'] and active['publication_status']=='committed'
    original = api.call('GET',raw,raw=True)
    result = {'baseline_publication':active['active'],'scene_sha256':hashlib.sha256(original).hexdigest(),
              'storage':'s3' if store else 'fs','phases':{}}
    selected = api.root/'cancel-phase'
    for phase in ('starting','running','finalizing'):
        stage = api.root/(phase+'-stage.json')
        payload = api.root/(phase+'-payload.json')
        selected.write_text(phase)
        with ThreadPoolExecutor(max_workers=1) as pool, ExitStack() as scope:
            future = pool.submit(api.call,'POST',preview,{},
                                 extra_headers={'Idempotency-Key':uuid.uuid4().hex})
            try:
                handles = []
                if phase=='running':
                    identifier = future.result(timeout=35)['job_id']
                    wait_for(payload.is_file,'Geometry did not start its actual child',45)
                    identities = [process_identity_native.observe(pid) for pid in json.loads(payload.read_text())]
                else:
                    wait_for(stage.is_file,'Geometry did not reach '+phase,45)
                    at = json.loads(stage.read_text())
                    identifier = at['job']
                    identities = [at['observation']] if phase=='starting' else []
                    if phase=='finalizing':
                        assert at['observation']['verified'] and at['observation']['root_exit_code']==0
                        assert payload.is_file()
                for identity in identities:
                    handle = ProcessHandle.query(identity)
                    scope.callback(handle.close)
                    handles.append(handle)
                observed = api.call('GET',state+str(identifier))
                assert observed['attempt']==phase and observed['active']==active['active']
                assert observed['publication_status']=='prepared'
                assert api.call('POST',f'/jobs/{identifier}:cancel')['ok']
            finally:
                (api.root/(phase+'-continue')).touch()
            response = future.result(timeout=35)
            assert response['job_id']==identifier
            if phase=='starting': assert response['status']=='cancelled'
            def cancelled():
                job = api.call('GET',f'/jobs/{identifier}')
                if job['attempt']['phase']=='cancelled' and (job['attempt'].get('process_stop') or {}).get('verified'):
                    return job
            job = wait_for(cancelled,'Cancelled geometry did not finish native cleanup',60)
            assert job['status']=='cancelled' and job['attempt']['process_stop']['scope_empty']
            for handle in handles: handle.wait_exit(5)
        settled = api.call('GET',state+str(identifier))
        assert settled['job']==settled['attempt']=='cancelled' and settled['attempt_id']==observed['attempt_id']
        assert settled['publication_status']=='failed' and settled['active']==active['active']
        assert settled['phases'].count('cancelled')==1 and settled['phases'][-2:]==['cancelling','cancelled']
        assert phase in settled['phases'] and 'succeeded' not in settled['phases']
        log = api.call('GET',f'/jobs/{identifier}/log?offset=0')
        assert log['available']
        expected_log = {'starting':'JobLaunchCancelled','running':'ACTUAL_RUNNING_PREVIEW','finalizing':'scene.json written'}
        assert expected_log[phase] in log['chunk']
        assert api.call('GET',raw,raw=True)==original
        assert payload.exists()==(phase!='starting')
        usage = api.call('GET','/org/mine')['usage']
        assert usage['running_jobs']==usage['storage_bytes_reserved']==0
        result['phases'][phase] = {'job':identifier,'observed':observed,'settled':settled,
            'payload_started':payload.exists(),'stop':job['attempt']['process_stop'],'held_handles_exited':len(handles),
            'log_sha256':hashlib.sha256(log['chunk'].encode()).hexdigest(),
            'running_after':usage['running_jobs'],'bytes_reserved_after':usage['storage_bytes_reserved']}
        (api.root/'geometry-cancel-phases.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    selected.unlink()
    api.create_case('Readback of geometry cancellation receipts')
    return result


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=12))
