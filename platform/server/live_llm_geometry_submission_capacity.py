"""Exercise explicit and save-triggered CadQuery input admission and real model readback."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
import sys
import time
import uuid

from live_llm_case_provenance_capacity_acceptance import sql_available, wait_for
from live_llm_job_repeat_acceptance import main
from s3testsupport import ObjectStore, SERVER


FIXTURE = '''
import asyncio
import hashlib
import time
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import attempt_workspace, command_input_seal, local_rerun_seal

original_geometry_digest = attempt_workspace.digest_file
CAPACITY = 'geometry-capacity.txt'
GEOMETRY = "import cadquery as cq\\ndef build_geometry(ctx):\\n return {'box': cq.Workplane('XY').box(2, 3, 4)}\\n"

def pause(name, data):
    entered = root/(name+'-entered')
    if entered.exists():
        return
    entered.write_text(str(data), encoding='utf-8')
    stop = time.monotonic()+30
    while not (root/(name+'-release')).exists():
        if time.monotonic() >= stop:
            raise TimeoutError('Geometry test observation did not drain')
        time.sleep(.025)

def measure_geometry_digest(path, **options):
    record = original_geometry_digest(path, **options)
    if path.name == CAPACITY and (root/'geometry-probe-read').exists():
        pause('geometry-read', record['size'])
    return record

attempt_workspace.digest_file = measure_geometry_digest
original_local_stage = local_rerun_seal.prepare_files
original_remote_stage = command_input_seal.upload

def after_local_stage(*args, **kwargs):
    staged = original_local_stage(*args, **kwargs)
    if (root/'geometry-probe-stage').exists():
        pause('geometry-stage', staged.draft['files']['A-geo/'+CAPACITY]['size'])
    return staged

async def after_remote_stage(*args, **kwargs):
    staged = await original_remote_stage(*args, **kwargs)
    if (root/'geometry-probe-stage').exists():
        await asyncio.to_thread(pause, 'geometry-stage', staged.draft['manifest']['files']['A-geo/'+CAPACITY]['size'])
    return staged

local_rerun_seal.prepare_files = after_local_stage
command_input_seal.upload = after_remote_stage

@app.post('/api/v1/testing/geometry/{case_id}/{marker}')
async def seed_geometry(case_id: int, marker: str, user=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    data = marker.encode('ascii') * (12*1024*1024)
    if marker == 'a':
        storage.put_text(case.s3_prefix,'A-geo/geometry.py',GEOMETRY)
    storage.put_bytes(case.s3_prefix,'A-geo/'+CAPACITY,data)
    return {'sha256':hashlib.sha256(data).hexdigest(), 'size':len(data), 'geometry':GEOMETRY}
'''


def scenario(api, *, baseline, store=None, **_):
    seed_url = f'/testing/geometry/{api.case_id}/'
    seed = api.call('POST', seed_url+'a')
    workspace = api.call('GET', api.prefix)['workspace_id']
    other = api.call('POST', f'/workspaces/{workspace}/cases', {'name':'Independent geometry admission write'})
    preview = api.prefix+'/geometry:preview'
    export = api.prefix+'/geometry:export'
    key = uuid.uuid4().hex
    headers = {'Idempotency-Key':key}
    (api.root/'geometry-probe-read').touch()
    duplicate = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(api.call, 'POST', preview, {'tolerance':.12}, extra_headers=headers)
        try:
            entered = api.root/'geometry-read-entered'
            wait_for(pending, entered)
            assert int(entered.read_text()) == seed['size']
            free = sql_available(api)
            assert free is not baseline, 'The geometry body read has an unexpected SQL lock scope'
            if not baseline:
                saved = api.call('PUT', f"/cases/{other['id']}/file", {'path':'Ref/independent.txt','content':'actual write'})
                assert saved['ok'] and not pending.done()
                duplicate = pool.submit(api.call, 'POST', preview, {'tolerance':.12}, extra_headers=headers)
                time.sleep(.3)
        finally:
            (api.root/'geometry-read-release').touch()
        admitted = pending.result(timeout=30)
        if duplicate is not None:
            assert duplicate.result(timeout=30)['job_id'] == admitted['job_id']
    (api.root/'geometry-probe-read').unlink()
    job = api.wait_job(admitted['job_id'])
    assert job['attempt']['workspace']['files']['A-geo/geometry-capacity.txt']['sha256'] == seed['sha256']
    assert job['attempt']['outputs']['publication']['status'] == 'committed'
    assert job['attempt']['handler']['key'] == 'geometry.preview'
    assert api.call('POST', preview, {'tolerance':.12},extra_headers=headers)['job_id'] == admitted['job_id']
    wrong_key = api.call('POST', preview, {'tolerance':.14},extra_headers=headers,expect=409)
    assert wrong_key['detail']['code'] == 'idempotency_conflict'
    result = {'baseline':baseline,'sql_writer_available':free,'preview_job':admitted['job_id'],
              'staged_actual_bytes':seed['size'],'tested_tolerance':.12}
    if not baseline:
        headers = {'Idempotency-Key':uuid.uuid4().hex}
        (api.root/'geometry-probe-stage').touch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(api.call, 'POST', export, expect=409, extra_headers=headers)
            try:
                wait_for(pending, api.root/'geometry-stage-entered')
                updated = api.call('POST', seed_url+'b')
                assert updated['size']==seed['size'] and updated['sha256']!=seed['sha256']
            finally:
                (api.root/'geometry-stage-release').touch()
            stale = pending.result(timeout=30)
            assert stale['detail']['code'] == 'input_stale', stale
        (api.root/'geometry-probe-stage').unlink()
        # The old request key must still work after the rejected mutation.
        accepted = api.call('POST', export, extra_headers=headers)
        job = api.wait_job(accepted['job_id'])
        assert job['attempt']['outputs']['publication']['status']=='committed'
        assert job['attempt']['workspace']['files']['A-geo/geometry-capacity.txt']['sha256']==updated['sha256']
        geometry = seed['geometry'].replace('box(2, 3, 4)', 'box(3, 3, 4)')
        saved = api.put('A-geo/geometry.py',geometry)
        automatic = api.wait_job(saved['auto_preview_job'])
        assert automatic['attempt']['workspace']['files']['A-geo/geometry.py']['sha256']==hashlib.sha256(geometry.encode()).hexdigest()
        assert automatic['attempt']['outputs']['publication']['status']=='committed'
        result.update(export_job=accepted['job_id'], auto_preview_job=saved['auto_preview_job'],
                      external_same_size_update_rejected=True, rejected_key_reused=True, concurrent_key_replayed=True)
    (api.root/'geometry-submission-capacity.json').write_text(json.dumps(result, indent=2),encoding='utf-8')
    return result


if __name__ == '__main__':
    baseline = '--geometry-baseline' in sys.argv
    if baseline:
        sys.argv.remove('--geometry-baseline')
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=partial(scenario,baseline=baseline), fixture_override=FIXTURE,
        store_volume_max=8, store_factory=partial(ObjectStore, executable=binary, volume_max=8, authenticated=True)))
