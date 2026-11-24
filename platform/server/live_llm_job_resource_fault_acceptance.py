"""Drive four real fault paths through job submission and verify termination and quota."""
import asyncio
from contextlib import ExitStack
import hashlib
import json
import shutil
import uuid

from app.services import process_identity_native
from app.services.windows_process_scope import ProcessHandle
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put


FIXTURE = '''
from fastapi import Depends
from sqlalchemy import select
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.models import ArtifactPublication, ArtifactPublicationHead, DurableEvent, JobAttempt
from app.routers.jobs import _get_job

original_floor = get_settings().node_storage_min_free_mb
original_workspace = get_settings().execution_workspace_limit_mb

@app.post('/api/v1/testing/disk-floor')
def configure_disk_floor(body: dict, user=Depends(get_current_user)):
    floor = body.get('megabytes')
    get_settings().node_storage_min_free_mb = original_floor if floor is None else floor
    get_settings().execution_workspace_limit_mb = body.get('workspace_mb',original_workspace)
    return {'megabytes': get_settings().node_storage_min_free_mb}

@app.get('/api/v1/testing/job-usage/{job_id}')
async def fault_usage(job_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    job = await _get_job(job_id,user,db,write=False)
    rows = list(await db.scalars(select(DurableEvent).join(JobAttempt, JobAttempt.id==DurableEvent.attempt_id)
        .where(JobAttempt.job_id==job.id, DurableEvent.case_id==job.case_id,
        DurableEvent.type.in_(['attempt.resource_usage','attempt.storage_usage']))))
    states = await db.scalars(select(DurableEvent.payload_json).join(JobAttempt,JobAttempt.id==DurableEvent.attempt_id)
        .where(JobAttempt.job_id==job.id,DurableEvent.type=='attempt.state_changed').order_by(DurableEvent.seq))
    heads = await db.scalars(select(ArtifactPublicationHead.active_publication_id).where(ArtifactPublicationHead.case_id==job.case_id))
    publications = await db.scalars(select(ArtifactPublication.id).where(ArtifactPublication.case_id==job.case_id))
    return {'events':[{'attempt':row.attempt_id,'type':row.type,'usage':json.loads(row.payload_json)} for row in rows],
            'phases':[json.loads(value)['status'] for value in states],
            'heads':list(heads),'publications':list(publications)}
'''

DRIVER = '''
import json, os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable,'-u','Ref/child.py'])
witness = Path(WITNESS)
temp = witness.with_suffix('.tmp')
temp.write_text(json.dumps([os.getpid(), child.pid]))
temp.replace(witness)
raise SystemExit(child.wait(timeout=120))
'''

CHILD = '''
import os, sys, time
from pathlib import Path
deadline = time.monotonic()+90
while not Path(RELEASE).exists():
    if time.monotonic() > deadline: raise TimeoutError('Fault was not armed')
    time.sleep(.05)
if MODE == 'child_exit':
    print('Child exit 37',flush=True)
    raise SystemExit(37)
if MODE == 'log_limit':
    sys.stdout.buffer.write(b'x'*16384)
    sys.stdout.flush()
if MODE == 'disk_limit':
    block = os.urandom(1024*1024)
    with Path(PRESSURE).open('xb') as stream:
        for _ in range(128): stream.write(block)
        stream.flush()
        os.fsync(stream.fileno())
if MODE == 'workspace_limit':
    Path('output.bin').write_bytes(os.urandom(2*1024*1024))
time.sleep(90)
'''


def fault(api, mode):
    api.create_case('Execution resource fault '+mode)
    witness = api.root/(mode+'-pids.json')
    release = api.root/(mode+'-armed')
    pressure = api.root/(mode+'-pressure.bin')
    put(api,'Ref/driver.py','WITNESS = '+repr(str(witness))+'\n'+DRIVER)
    put(api,'Ref/child.py',f'MODE = {mode!r}\nRELEASE = {str(release)!r}\nPRESSURE = {str(pressure)!r}\n'+CHILD)
    source = api.call('GET',api.prefix+'/raw?path=Ref/child.py',raw=True)
    base_usage = api.call('GET','/org/mine')['usage']
    assert base_usage['running_jobs'] == base_usage['storage_bytes_reserved'] == 0
    request = {'runtime':'windows','command':'cmd.exe /d /c python Ref/driver.py',
               'input_paths':['Ref/driver.py','Ref/child.py'],
               'timeout_seconds':12 if mode=='timeout' else 100,
               'max_log_bytes':1024 if mode=='log_limit' else 1048576}
    identifier = None
    disk = mode=='disk_limit'
    try:
        if disk:
            assert api.root.drive.lower()=='d:' and shutil.disk_usage(api.root).free > 1024**3
            floor = shutil.disk_usage(api.root).free//(1024**2)-64
            api.call('POST','/testing/disk-floor',{'megabytes':floor})
        elif mode=='workspace_limit':
            api.call('POST','/testing/disk-floor',{'workspace_mb':1})
        try:
            submitted = api.call('POST',api.prefix+'/jobs',request,expect=(200,202),
                                 extra_headers={'Idempotency-Key':uuid.uuid4().hex})
            identifier = submitted['id']
        finally:
            if disk or mode=='workspace_limit': api.call('POST','/testing/disk-floor',{})
        wait_for(witness.is_file,'Job did not start its driver and child',25)
        pids = json.loads(witness.read_text())
        with ExitStack() as scope:
            handles = []
            for pid in pids:
                handle = ProcessHandle.query(process_identity_native.observe(pid))
                scope.callback(handle.close)
                handles.append(handle)
            running = api.call('GET','/org/mine')['usage']['running_jobs']
            assert running==1
            release.touch()
            job = api.wait_job(identifier,timeout=60,expected='failed')
            for handle in handles: handle.wait_exit(5)
        stop = job['attempt']['process_stop']
        assert stop['verified'] and stop['scope_empty'] and not job['completion_pending']
        assert job['exit_code']!=0 and job['ended_at'] and job['attempt']['phase']=='failed'
        durable = api.call('GET',f'/testing/job-usage/{identifier}')
        events = durable['events']
        assert durable['phases'][-1]=='failed' and durable['phases'].count('failed')==1 and 'succeeded' not in durable['phases']
        assert not durable['heads'] and not durable['publications']
        assert api.call('GET',api.prefix+'/raw?path=Ref/child.py',raw=True)==source
        log = api.call('GET',f'/jobs/{identifier}/log?offset=0')
        assert log['available'] and ('Child exit 37' if mode=='child_exit' else '[resource limit]') in log['chunk']
        assert len(events)==2 and all(e['attempt']==job['attempt']['id'] for e in events)
        usage = {e['type']:e['usage'] for e in events}
        output = usage['attempt.resource_usage']
        storage = usage['attempt.storage_usage']
        if mode=='child_exit':
            assert stop['root_exit_code']==job['exit_code']==37 and output['violation'] is None
            assert job['attempt']['reason_code']=='process_exit_failed'
        else:
            expected = 'disk_limit' if mode=='workspace_limit' else mode
            assert stop['reason']==output['violation']==expected and job['attempt']['reason_code']=='resource_limit'
        if disk:
            assert storage['minimum_free_bytes'] < storage['policy']['min_free_bytes'] == floor*1024**2
            assert storage['peak_bytes'] < storage['policy']['max_workspace_bytes'] and pressure.stat().st_size>0
            assert f"低于预留阈值 {floor*1024**2} 字节" in job['log_backlog']
        if mode=='workspace_limit':
            assert storage['peak_bytes'] > storage['policy']['max_workspace_bytes']==1024**2
            assert storage['minimum_free_bytes'] >= storage['policy']['min_free_bytes']
            assert '超过上限 1048576 字节' in job['log_backlog']
        if mode=='log_limit': assert output['observed_output_bytes'] > output['max_log_bytes']==1024
        final_usage = api.call('GET','/org/mine')['usage']
        assert final_usage['running_jobs']==final_usage['storage_bytes_reserved']==0
        return {'job':identifier,'attempt':job['attempt']['id'],'reason':job['attempt']['reason_code'],
                'exit_code':job['exit_code'],'stop':stop,'native_driver_and_child_exited':True,
                'storage':storage,'output':output,'running_before_fault':running,'phases':durable['phases'],
                'source_sha256':hashlib.sha256(source).hexdigest(),'log_sha256':hashlib.sha256(log['chunk'].encode()).hexdigest(),
                'publication_count':len(durable['publications']),'active_heads':durable['heads'],
                'running_after':final_usage['running_jobs'],'storage_reserved_after':final_usage['storage_bytes_reserved']}
    finally:
        try:
            if identifier and api.call('GET',f'/jobs/{identifier}')['status'] in {'running','queued'}:
                api.call('POST',f'/jobs/{identifier}:cancel')
        finally:
            pressure.unlink(missing_ok=True)


def scenario(api, *, store=None, **_):
    result = {'storage_backend':'s3' if store else 'fs','faults':{}}
    for mode in ('child_exit','log_limit','timeout','workspace_limit','disk_limit'):
        result['faults'][mode] = fault(api,mode)
        (api.root/'job-resource-faults.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    api.create_case('Readback of completed execution resource faults')
    return result


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
