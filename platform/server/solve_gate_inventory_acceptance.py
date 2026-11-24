"""Real registered solve followed by a cold D-stage confirmation."""

import argparse
import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import re
from shutil import copyfile
import sys
import time
from urllib.parse import quote, urlsplit
import zipfile

import httpx

from app.services.process_identity_native import probe
from case_workflow_thermal_fixture import GEOMETRY, RUN, case_dictionary, thermal_files
from live_llm_terminal_agent_acceptance import disposable_database
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


IGNORED = RUN + '/unrelated-large.bin'
FINAL = RUN + '/1/T'
SIZE = 40 * 1024 * 1024
APP = r'''
import json
import os
from pathlib import Path
import traceback
from fastapi import Depends
from botocore.client import BaseClient
from app.main import app
from app.deps import get_current_user
from app.services import publication_objects
from app.services.process_identity_native import observe

root = Path(__file__).parent
original = BaseClient._make_api_call
read_manifest = publication_objects.read_manifest
def observed(self, operation, params):
    if operation == 'GetObject' and (root/'observe-d-gate').is_file():
        key = params.get('Key','')
        if key.endswith('/unrelated-large.bin'):
            (root/'unrelated-get').touch()
    return original(self, operation, params)
BaseClient._make_api_call = observed

def record_materialization(prefix, row):
    if (root/'observe-d-gate').is_file():
        funcs = [frame.name for frame in traceback.extract_stack(limit=9)]
        with (root/'manifest-materializations.jsonl').open('a', encoding='utf8') as output:
            output.write(json.dumps({'publication_id':row.id, 'callers':funcs})+'\n')
    return read_manifest(prefix, row)

publication_objects.read_manifest = record_materialization

@app.on_event('startup')
async def birth():
    (root/'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')

@app.post('/fixture/stop', status_code=204)
async def stop(body: dict, user=Depends(get_current_user)):
    assert body['pid'] == os.getpid()
    os._exit(82)
'''


def prepare(api):
    initial = api.create_case('Preparation template')
    workspace = api.call('GET', api.prefix)['workspace_id']
    template = api.call('GET', api.prefix+'/caseDict')['content']
    archive = api.root/'thermal-case.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as package:
        package.writestr('0-caseDict/caseDict', case_dictionary(template))
        package.writestr('A-geo/geometry.py', GEOMETRY)
        for path, data in thermal_files().items():
            package.writestr(path, data)
    with archive.open('rb') as payload, httpx.Client(trust_env=False, timeout=70) as client:
        response = client.post(f'{api.base}/api/v1/workspaces/{workspace}/cases:import',
            headers={'Authorization':'Bearer '+api.token}, data={'name':'Cold D stage'},
            files={'file':(archive.name, payload, 'application/zip')})
    assert response.status_code == 200, response.text[:800]
    case = response.json()
    api.case_id = case['id']
    api.prefix = f'/cases/{api.case_id}'
    assert initial['id'] != api.case_id
    return api.call('GET', api.prefix)


def await_confirmation(api, run_id, stage):
    deadline = time.monotonic() + 280
    while time.monotonic() < deadline:
        result = api.call('GET', '/workflow-runs/'+run_id)
        assert result['status'] not in {'failed','cancelled','interrupted'}, str(result)[:3500]
        step = next(row for row in result['steps'] if row['key'] == 'confirm_'+stage)
        if step['status'] == 'awaiting_confirmation':
            jobs = {}
            for producer in result['steps']:
                if producer['status'] == 'succeeded' and producer['attempts']:
                    job = api.wait_job(producer['attempts'][-1]['job_id'])
                    assert job['attempt']['process_stop']['verified'] is True
                    jobs[producer['key']] = job
            return step, jobs
        time.sleep(.15)
    raise TimeoutError('Workflow did not reach confirmation '+stage)


def confirm(api, step, status=200):
    return api.call('POST', api.prefix+'/caseDict:advance', {
        'state':step['confirmation_state'], 'confirmed':True,
        'expected_revision':step['confirmation_revision'],
        'workflow_step_id':step['id'], 'expected_step_version':step['version']}, expect=status)


def exercise(api, store, report):
    case = prepare(api)
    report['started_case'] = case['id']
    run = api.call('POST', api.prefix+'/workflow-runs', {'target':'solve','run_dir':RUN,
        'expected_revision':case['dict_revision'],'parameters':{'geometry_mode':'export'},
        'budget':{'timeout_seconds':250,'max_log_bytes':4*1024*1024}})
    report['workflow_run_id'] = run['id']
    for stage in ('B','C'):
        step, _ = await_confirmation(api, run['id'], stage)
        assert confirm(api, step)['workflow_state'] == stage
        report['latest_confirmation'] = stage
    step, jobs = await_confirmation(api, run['id'], 'D')
    solve = jobs['solve']
    artifact = solve['attempt']['outputs']['publication']
    assert artifact['status'] == 'committed'
    report['jobs'] = {name:job['id'] for name,job in jobs.items()}
    report['solve_publication_id'] = artifact['id']
    prefix = case['storage_prefix']
    root = (api.root/'files'/prefix).resolve()
    assert root.is_relative_to(api.root.resolve())
    bundle = root/'.vibeflow'/'publications'/artifact['id']
    assert bundle.resolve().is_relative_to(root)
    exact = api.call('GET', api.prefix+'/raw?path='+quote(FINAL, safe='/'), raw=True)
    assert b'volScalarField' in exact and re.search(rb'\bobject\s+T\s*;', exact)
    if store and bundle.exists():
        bundle.rename(api.root/'pre-check-solve-cache')
    assert not bundle.exists() if store else bundle.is_dir()

    def put(path, data):
        if store:
            args = {'Bucket':store.bucket,'Key':prefix+'/'+path}
            if isinstance(data, Path):
                with data.open('rb') as stream:
                    store.client.put_object(**args, Body=stream)
            else:
                store.client.put_object(**args, Body=data)
            return
        target = (root/path).resolve()
        assert target.is_relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, Path):
            copyfile(data, target)
        else:
            target.write_bytes(data)

    def remove(path):
        if store:
            store.client.delete_object(Bucket=store.bucket, Key=prefix+'/'+path)
        else:
            (root/path).unlink()

    unrelated = api.root/'unrelated-source.bin'
    with unrelated.open('wb') as data:
        data.truncate(SIZE)
    put(IGNORED, unrelated)
    observe = api.root/'observe-d-gate'
    observe.touch()
    try:
        current = api.call('GET', api.prefix+'/caseDict')
        assert current['workflow_state'] == 'C'
        put(FINAL, exact + b'\n// changed live field\n')
        response = confirm(api, step, status=409)
        assert '当前求解文件已变化' in str(response), response
        if store:
            assert not bundle.exists()
        put(FINAL, exact)

        extra = RUN+'/77/T'
        put(extra, exact)
        response = confirm(api, step, status=409)
        assert '未发布或缺失' in str(response), response
        remove(extra)

        equivalent = RUN+'/1.0/T'
        put(equivalent, exact)
        confirm(api, step, status=409)
        remove(equivalent)
        assert api.call('GET', api.prefix+'/caseDict') == current
        if store:
            assert not bundle.exists()
        advanced = confirm(api, step)
        assert advanced['workflow_state'] == 'D'
        assert advanced['dict_revision'] == current['dict_revision'] + 1
        assert api.call('GET','/workflow-runs/'+run['id'])['status'] == 'succeeded'
    finally:
        observe.unlink(missing_ok=True)
    assert not (api.root/'unrelated-get').exists()
    if store:
        assert not bundle.exists(), 'D-stage confirmation restored the solve publication cache'
    report.update(confirmed_D=True, extra_time_status=409, duplicate_numeric_time_status=409,
                  changed_live_field_status=409, unrelated_bytes=SIZE, unrelated_s3_body_gets=0 if store else None,
                  no_solve_cache_created=not bundle.exists() if store else None)


def exit_api(api):
    birth = json.loads((api.root/'api-birth.json').read_text(encoding='utf8'))
    with httpx.Client(timeout=10, trust_env=False) as client:
        try:
            client.post(api.base+'/fixture/stop', json={'pid':birth['pid']},
                        headers={'Authorization':'Bearer '+api.token})
        except (httpx.RemoteProtocolError, httpx.ReadError):
            pass
    api.proc.wait(20)
    result = probe(birth)
    assert api.proc.returncode == 82 and result['state'] == 'exited'
    return result


def verify(database, store):
    backend = 's3' if store else 'fs'
    api = IsolatedServer('solve-d-inventory-'+backend, app='solve_d_fixture:app', database_url=database,
                         access_log=True)
    open_request = api.opener.open

    def allow_slow_fixture(request, *args, timeout, **kwargs):
        request_path = urlsplit(request.full_url).path
        try:
            return open_request(request, *args, timeout=max(timeout, 110), **kwargs)
        except TimeoutError as error:
            raise TimeoutError(f'{request.get_method()} {request_path} timed out in the isolated fixture') from error

    api.opener.open = allow_slow_fixture
    settings = store.settings() if store else {}
    (api.root/'solve_d_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+APP,
                                              encoding='utf8')
    print('SOLVE_D_INVENTORY_EVIDENCE '+str(api.root), flush=True)
    report = {'passed':False,'backend':backend,'database':'postgresql' if database else 'sqlite',
              'real_model_requests':0,'mock_llm_calls':0}
    try:
        with api:
            try:
                exercise(api, store, report)
            finally:
                if api.proc.poll() is None and api.token:
                    report['api_exit'] = exit_api(api)
        report['passed'] = True
    except Exception as error:
        report['failure'] = str(error)[:800]
        raise
    finally:
        (api.root/'solve-d-inventory.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    print('SOLVE_D_INVENTORY_OK '+str(api.root), flush=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('fs','s3'), required=True)
    use_s3 = parser.parse_args().backend == 's3'
    runtime = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    context = ObjectStore(executable=runtime, volume_max=12, authenticated=True) if use_s3 else nullcontext()
    async with disposable_database(use_s3) as database:
        with context as store:
            await asyncio.to_thread(verify, database, store)
    if use_s3:
        assert context.proc is not None and context.proc.poll() is not None


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    asyncio.run(main())
