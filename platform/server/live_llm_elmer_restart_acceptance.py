"""Actual Elmer ASCII/binary restart and an uninterrupted numerical comparison."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
import re
import time
from urllib.parse import urlencode
import uuid

from live_llm_case_provenance_capacity_acceptance import wait_for
from live_llm_elmer_acceptance import DECK, MESH, completed, temperature_points
from live_llm_job_repeat_acceptance import main
from s3testsupport import ObjectStore, SERVER


FIXTURE = '''
import asyncio
import os
import time
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import command_input_seal, local_rerun_seal

checkpoint = 'line/first_ascii.result'
prepare_local = local_rerun_seal.prepare_files
prepare_remote = command_input_seal.upload

def after_checkpoint(manifest):
    if not (root/'elmer-pause').exists() or checkpoint not in manifest['files']:
        return
    (root/'elmer-paused').touch()
    deadline = time.monotonic()+20
    while not (root/'elmer-release').exists():
        if time.monotonic()>=deadline:
            raise TimeoutError('Sealed state observation was not released')
        time.sleep(.025)

def paused_local(*args,**kwargs):
    staged = prepare_local(*args,**kwargs)
    after_checkpoint(staged.draft)
    return staged

async def paused_remote(*args,**kwargs):
    staged = await prepare_remote(*args,**kwargs)
    await asyncio.to_thread(after_checkpoint,staged.draft['manifest'])
    return staged

local_rerun_seal.prepare_files = paused_local
command_input_seal.upload = paused_remote

@app.post('/api/v1/testing/elmer/{case_id}/checkpoint/{action}')
async def change_checkpoint(case_id: int, action: str,user=Depends(get_current_user),db: AsyncSession=Depends(get_db)):
    case = await get_owned_case(case_id,user,db)
    original = root/'original-ascii.result'
    if action == 'restore':
        data = original.read_bytes()
    else:
        previous = storage.get_bytes(case.s3_prefix,checkpoint)
        data = previous.replace(b'1.00000000E-001',b'1.50000000E-001',1)
        assert len(data)==len(previous) and data!=previous
        original.write_bytes(previous)
    if storage._use_s3():
        storage.put_bytes(case.s3_prefix,checkpoint,data)
    else:
        path = storage._fs_path(case.s3_prefix,checkpoint)
        stamp = path.stat()
        path.write_bytes(data)
        os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    return {'bytes':len(data)}
'''


def deck(name, steps, *, checkpoint=None, binary=False):
    simulation = ['Simulation Type = Transient','Timestepping Method = BDF','BDF Order = 1',
                  'Timestep Sizes = 0.1',f'Timestep Intervals = {steps}','Output Intervals = 1']
    if checkpoint:
        simulation += [f'Restart File = File "{checkpoint}"','Restart Position = Integer 0']
    if binary:
        simulation.append('Binary Output = Logical True')
    return (DECK.replace('Simulation Type = Steady State','\n  '.join(simulation))
            .replace('"heat.result"',f'"{name}.result"').replace('"heat.vtu"',f'"{name}.vtu"'))


def scenario(api, *, store=None, **_):
    for name,text in {
        'mesh.header':'3 2 2\n2\n101 2\n202 2\n',
        'mesh.nodes':'1 -1 0.0 0.0 0.0\n2 -1 0.5 0.0 0.0\n3 -1 1.0 0.0 0.0\n',
        'mesh.elements':'1 1 202 1 2\n2 1 202 2 3\n',
        'mesh.boundary':'1 1 1 0 101 1\n2 2 2 0 101 3\n',
    }.items():
        assert api.put(MESH+'/'+name,text)['ok']
    route = api.prefix+'/elmer:solve'
    mutation = f'/testing/elmer/{api.case_id}/checkpoint/'

    def raw(path):
        return api.call('GET',api.prefix+'/raw?'+urlencode({'path':path}),raw=True)

    def run(path,name,count):
        assert api.put(path,deck(name,count,binary=name=='first_binary'))['ok']
        result = api.call('POST',route,{'input_path':path},extra_headers={'Idempotency-Key':uuid.uuid4().hex})
        completed(api,result['job_id'],input_path=path)
        return result['job_id']

    reference_id = run('C-run/elmer/full.sif','full',4)
    reference = temperature_points(raw(MESH+'/full_t0004.vtu'))
    report = {'reference_job':reference_id,'reference_temperature':reference,'formats':{},'storage':'s3' if store else 'fs'}

    for encoding in ('ascii','binary'):
        source = 'first_'+encoding
        output = 'continued_'+encoding
        path = 'C-run/elmer/'+encoding+'.sif'
        checkpoint = 'line/'+source+'.result'
        first_id = run(path,source,2)
        state = raw(checkpoint)
        expected = {checkpoint:hashlib.sha256(state).hexdigest()}
        index_digest = None
        if encoding=='binary':
            assert state.lstrip().startswith(b'BINARY 3.L')
            index_digest = hashlib.sha256(raw(checkpoint+'.pos')).hexdigest()
        else:
            assert state.lstrip().startswith(b'ASCII 3')
            assert api.put(path,deck(source,2,checkpoint=source+'.result'))['ok']
            assert api.call('POST',route,{'input_path':path},expect=400)['detail']
            assert api.put(path,deck(output,2,checkpoint='missing.result'))['ok']
            assert api.call('POST',route,{'input_path':path},expect=404)['detail']

        assert api.put(path,deck(output,2,checkpoint=source+'.result'))['ok']
        headers = {'Idempotency-Key':uuid.uuid4().hex}
        if encoding=='ascii':
            probe = api.root/'elmer-pause'
            probe.touch()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(api.call,'POST',route,{'input_path':path},extra_headers=headers,expect=409)
                try:
                    wait_for(pending,api.root/'elmer-paused')
                    assert api.call('POST',mutation+'replace')['bytes']==len(state)
                finally:
                    (api.root/'elmer-release').touch()
                refused = pending.result(timeout=30)
            assert refused['detail']['code']=='input_stale'
            probe.unlink()
            api.call('POST',mutation+'restore')

        resume = api.call('POST',route,{'input_path':path},extra_headers=headers)['job_id']
        attempt = completed(api,resume,input_path=path)
        sealed = attempt['workspace']['files']
        for name,digest in expected.items():
            assert sealed[name]['sha256']==digest and hashlib.sha256(raw(name)).hexdigest()==digest
        if index_digest is not None:
            assert checkpoint+'.pos' not in sealed
            assert hashlib.sha256(raw(checkpoint+'.pos')).hexdigest()==index_digest
        assert api.call('POST',route,{'input_path':path},extra_headers=headers)['job_id']==resume
        profile = temperature_points(raw(MESH+'/'+output+'_t0002.vtu'))
        fields = dict(profile)
        max_difference = max(abs(fields[x]-value) for x,value in reference)
        assert max_difference<1e-12
        steps = re.findall(rb'^Time:\s+\d+\s+\d+\s+([\d.E+-]+)',raw('line/'+output+'.result'),re.M)
        assert [float(t) for t in steps]==[.3,.4]
        report['formats'][encoding] = {'first_job':first_id,'resume_job':resume,'checkpoint_sha256':expected,
            'final_temperature':profile,'max_difference':max_difference,'new_times':[float(t) for t in steps],
            'original_index_sha256':index_digest}

    path = 'C-run/elmer/out-of-range.sif'
    invalid = deck('invalid_position',1,checkpoint='first_ascii.result').replace('Integer 0','Integer 99')
    assert api.put(path,invalid)['ok']
    failed = api.call('POST',route,{'input_path':path})['job_id']
    attempt = api.wait_job(failed,timeout=180,expected='failed')['attempt']
    assert attempt['process_stop']['verified'] and (attempt['outputs'].get('publication') or {}).get('status')!='committed'
    deadline = time.monotonic()+60
    while True:
        saved = api.call('PUT',api.prefix+'/file',{'path':'Ref/after-elmer-failure.txt',
            'content':'The case accepts writes after the stopped solver.'},expect=(200,409))
        if saved.get('ok'):
            break
        assert saved['detail']['code'] in {'file_sync_busy','storage_quota_busy','validation_busy'}, saved
        assert time.monotonic()<deadline, 'Failed solve archival did not release case admission'
        time.sleep(.15)
    report.update(passed=True,stale_checkpoint_http=409,stale_request_key_reused=True,
                  original_checkpoints_preserved=True,rejected_position_job=failed,writable_after_failed_restart=True)
    (api.root/'elmer-restart.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report


if __name__ == '__main__':
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=8,
        store_factory=partial(ObjectStore,executable=binary,volume_max=8,authenticated=True)))
