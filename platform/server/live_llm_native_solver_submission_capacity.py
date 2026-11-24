"""Run actual CalculiX and SU2 after observing the public admission write scope."""
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import json
import re
import sys
from urllib.parse import urlencode
import uuid

from live_llm_case_provenance_capacity_acceptance import sql_available, wait_for
from live_llm_job_repeat_acceptance import main
from s3testsupport import ObjectStore, SERVER


FIXTURE = '''
import asyncio
import time
from app.services import attempt_workspace, calculix_artifacts, local_rerun_seal, command_input_seal

original_solver_digest = attempt_workspace.digest_file
original_calculix_record = calculix_artifacts.record
original_solver_local_stage = local_rerun_seal.prepare_files
original_solver_remote_stage = command_input_seal.upload

def observe_solver(name, value):
    entered = root/(name+'-entered')
    if entered.exists():
        return
    entered.write_text(str(value), encoding='utf-8')
    deadline = time.monotonic()+25
    while not (root/(name+'-release')).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Solver observation did not drain')
        time.sleep(.025)

def solver_digest(path, **options):
    result = original_solver_digest(path, **options)
    if path.name == 'naca0012.su2' and (root/'pause-su2').exists():
        observe_solver('su2', result['size'])
    return result

def observe_include(data):
    result = original_calculix_record(data)
    if data.startswith(b'** VIBEFLOW INCLUDE') and (root/'pause-calculix').exists():
        observe_solver('calculix', result['size'])
    return result

def solver_local_stage(*args, **kwargs):
    result = original_solver_local_stage(*args, **kwargs)
    if (root/'pause-after-seal').exists():
        observe_solver('after-seal',len(result.draft['files']))
    return result

async def solver_remote_stage(*args, **kwargs):
    result = await original_solver_remote_stage(*args, **kwargs)
    if (root/'pause-after-seal').exists():
        await asyncio.to_thread(observe_solver,'after-seal',len(result.draft['manifest']['files']))
    return result

attempt_workspace.digest_file = solver_digest
calculix_artifacts.record = observe_include
local_rerun_seal.prepare_files = solver_local_stage
command_input_seal.upload = solver_remote_stage

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case

@app.post('/api/v1/testing/solver/{case_id}/optional-config')
async def add_optional_config(case_id: int, user=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    storage.put_text(case.s3_prefix, 'runtime.cfg', '%% new optional runtime configuration\\n')
    return {'ok':True}
'''


def scenario(api, *, baseline, store=None, **_):
    deck_source = ast.parse((SERVER/'calculix_smoke.py').read_text(encoding='utf-8'))
    deck = next(ast.literal_eval(node.value) for node in deck_source.body if isinstance(node, ast.Assign)
                and any(isinstance(name, ast.Name) and name.id=='DECK' for name in node.targets))
    include = '** VIBEFLOW INCLUDE\n' + '** bounded physical solver input\n' * 65536
    include_path = 'C-run/calculix/comments.inp'
    ccx_source = 'C-run/calculix/beam.inp'
    assert api.put(include_path, include)['ok']
    assert api.put(ccx_source, '*INCLUDE,INPUT='+include_path+'\n'+deck)['ok']
    config = (SERVER/'examples/su2/inv_NACA0012.cfg').read_text(encoding='utf-8').replace('ITER= 250','ITER= 3')
    config = config.replace('MESH_FILENAME= mesh_NACA0012_inv.su2','MESH_FILENAME= C-run/su2/naca0012.su2')
    mesh = (SERVER/'examples/su2/mesh_NACA0012_inv.su2').read_text(encoding='utf-8')
    config_path = 'C-run/su2/naca0012.cfg'
    assert api.put(config_path,config)['ok'] and api.put('C-run/su2/naca0012.su2',mesh)['ok']
    workspace = api.call('GET',api.prefix)['workspace_id']
    unrelated = api.call('POST', f'/workspaces/{workspace}/cases', {'name':'Independent solver writer'})
    cases = {'calculix':({'input_path':ccx_source},len(include.encode())), 'su2':({'config_path':config_path},len(mesh.encode()))}
    report = {'baseline':baseline,'backend':'s3' if store else 'fs','solvers':{}}
    for name,(payload,size) in cases.items():
        route = api.prefix+'/'+name+':solve'
        headers = {'Idempotency-Key':uuid.uuid4().hex}
        probe = api.root/('pause-'+name)
        probe.touch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(api.call, 'POST', route, payload, extra_headers=headers)
            try:
                marker = api.root/(name+'-entered')
                wait_for(pending,marker)
                assert int(marker.read_text()) == size
                writable = sql_available(api)
                assert writable is not baseline, f'Unexpected SQL writer scope during {name} dependency read'
                if not baseline:
                    saved = api.call('PUT',f"/cases/{unrelated['id']}/file",
                        {'path':'Ref/'+name+'.txt','content':'saved during actual input read'})
                    assert saved['ok'] and not pending.done()
            finally:
                (api.root/(name+'-release')).touch()
            job_id = pending.result(timeout=30)['job_id']
        probe.unlink()
        job = api.wait_job(job_id,timeout=180)
        assert job['attempt']['outputs']['publication']['status']=='committed'
        assert job['attempt']['handler']['key']==name+'.solve'
        assert api.call('POST',route,payload,extra_headers=headers)['job_id']==job_id
        artifact = 'C-run/calculix/beam.frd' if name=='calculix' else 'flow.vtu'
        data = api.call('GET',api.prefix+'/raw?'+urlencode({'path':artifact}),raw=True)
        if name=='calculix':
            _, displacements = data.split(b'DISP',1)
            assert any(abs(float(number))>1e-10 for number in re.findall(rb'[+-]?\d+\.\d+E[+-]\d+',displacements))
        else:
            assert b'UnstructuredGrid' in data and b'Density' in data
        report['solvers'][name] = {'job_id':job_id,'dependency_size':size,'output_size':len(data),'sql_writer_available':writable}
    if not baseline:
        probe = api.root/'pause-after-seal'
        probe.touch()
        route = api.prefix+'/su2:solve'
        headers = {'Idempotency-Key':uuid.uuid4().hex}
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(api.call,'POST',route,cases['su2'][0],expect=409,extra_headers=headers)
            try:
                wait_for(pending,api.root/'after-seal-entered')
                assert api.call('POST',f'/testing/solver/{api.case_id}/optional-config')['ok']
            finally:
                (api.root/'after-seal-release').touch()
            assert pending.result(timeout=30)['detail']['code']=='input_stale'
        probe.unlink()
        replay = api.call('POST',route,cases['su2'][0],extra_headers=headers)
        settled = api.wait_job(replay['job_id'],timeout=180)
        assert 'runtime.cfg' in settled['attempt']['workspace']['files']
        assert settled['attempt']['outputs']['publication']['status']=='committed'
        report.update(appearing_optional_input_rejected=True, rejected_key_reused=True)
    (api.root/'native-solver-submission-capacity.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report


if __name__ == '__main__':
    baseline = '--solver-baseline' in sys.argv
    if baseline:
        sys.argv.remove('--solver-baseline')
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=partial(scenario,baseline=baseline),fixture_override=FIXTURE,
        store_volume_max=8,store_factory=partial(ObjectStore,executable=binary,volume_max=8,authenticated=True)))
