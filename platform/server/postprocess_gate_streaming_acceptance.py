"""Exercise bounded postprocessing report gates with real files and native extraction."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import httpx

from app.services.casedict import parse_casedict, set_block
from app.services.case_evidence import case_input_fingerprint
from app.services.postprocess_executor import config_from_case_dict, postprocess_fingerprint
from cae_benchmarks import thermal_files
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


RUN = 'C-run/conduction'
REFERENCE = 'Ref/verification-T'
IGNORED = RUN+'/scratch/ignored.bin'
SIZE = 40*1024*1024
RUNTIME = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'

FIXTURE = r'''
import asyncio
import json
import os
from pathlib import Path
import threading
import time
import tracemalloc

from fastapi import Depends
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.routers.workspaces import get_owned_case
from app.services import artifact_publication, attempt_objects, postprocess_gate
from app.services import postprocess_publication, process_identity_native, s3_live_store
from app.services.case_write import CaseWriteConflict

root = Path(__file__).parent
worker = threading.local()
match = postprocess_gate.matches_current_sha256
select = attempt_objects.selection
get_object = s3_live_store.Client.get_object

def fence():
    (root/'fence-ready').touch()
    deadline = time.monotonic()+15
    while not (root/'fence-release').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('External fixture writer did not complete')
        time.sleep(.02)

def after_initial_verification(*args):
    if getattr(worker, 'mode', None) == 'legacy_replacement':
        fence()
    return match(*args)

def instrumented_selection(*args, **kwargs):
    result = select(*args, **kwargs)
    if getattr(worker, 'mode', None) in {'inventory', 'inventory_replacement'}:
        worker.selections += 1
        if worker.selections == 1 and worker.mode == 'inventory_replacement':
            fence()
    return result

def instrumented_get(self, **kwargs):
    if getattr(worker, 'mode', None) in {'inventory', 'inventory_replacement'}:
        worker.get_keys.append(kwargs['Key'])
    return get_object(self, **kwargs)

postprocess_gate.matches_current_sha256 = after_initial_verification
attempt_objects.selection = instrumented_selection
s3_live_store.Client.get_object = instrumented_get

def evaluate(call, mode):
    worker.mode = mode
    worker.get_keys = []
    worker.selections = 0
    traced = mode in {'legacy', 'inventory'}
    if traced:
        tracemalloc.start()
    try:
        try:
            outcome = {'value':call()}
        except CaseWriteConflict as conflict:
            outcome = {'conflict':conflict.code}
        return {**outcome, 'body_get_keys':worker.get_keys,
            'selection_count':worker.selections,
            'peak_traced_bytes':tracemalloc.get_traced_memory()[1] if traced else None}
    finally:
        if traced:
            tracemalloc.stop()
        del worker.mode, worker.get_keys, worker.selections

@app.get('/fixture/postprocess/{case_id}/binding')
async def binding(case_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    return {'prefix':case.s3_prefix, 'api_process':process_identity_native.observe(os.getpid())}

@app.post('/fixture/postprocess/{case_id}/verify')
async def verify(case_id: int, body: dict, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    prefix = case.s3_prefix
    mode = body['mode']
    if mode.startswith('legacy'):
        return await asyncio.to_thread(evaluate, lambda:postprocess_gate.report_gate_reason(case), mode)
    if mode == 'authoritative':
        return {'value':await postprocess_gate.authoritative_reason(db, case)}
    _, row = await artifact_publication.current(db, case.id, 'postprocess')
    assert row and row.status == 'committed'
    pointer = json.loads(artifact_publication.read_file(prefix, row, postprocess_publication.LATEST))
    manifest = json.loads(artifact_publication.read_file(prefix, row, pointer['manifest_path']))
    return await asyncio.to_thread(evaluate, lambda:postprocess_publication._gate_inventory(
        prefix, manifest['run_dir']), mode)

@app.post('/fixture/postprocess:stop')
async def stop(body: dict, user=Depends(get_current_user)):
    assert body['pid'] == os.getpid()
    os._exit(79)
'''


def put_live(api, store, prefix, path, data):
    if store:
        args = {'Bucket':store.bucket, 'Key':prefix+'/'+path}
        if isinstance(data, Path):
            with data.open('rb') as body:
                store.client.put_object(**args, Body=body)
        else:
            store.client.put_object(**args, Body=data)
        return store.client.head_object(**args)['ETag']
    root = (api.root/'files'/prefix).resolve()
    target = (root/path).resolve()
    assert root.is_relative_to(api.root.resolve()) and target.is_relative_to(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix('.owned-replacement')
    if isinstance(data, Path):
        shutil.copyfile(data, scratch)
    else:
        scratch.write_bytes(data)
    scratch.replace(target)
    return target.stat().st_ino


def source_file(root):
    target = root/'source.bin'
    digest = hashlib.sha256()
    with target.open('xb') as body:
        for _ in range(SIZE//(1024*1024)):
            chunk = os.urandom(1024*1024)
            body.write(chunk)
            digest.update(chunk)
    return target, digest.hexdigest()


def configured_case(api, name, config):
    case = api.create_case(name)
    fixture = f'/fixture/postprocess/{case["id"]}'
    binding = api.call('GET', fixture+'/binding', api_prefix=False)
    saved = api.call('GET', api.prefix+'/caseDict')
    contents = set_block(saved['content'], 'postProcessing', config)
    api.call('PUT', api.prefix+'/caseDict',
             {'content':contents, 'expected_revision':saved['dict_revision']})
    current = api.call('GET', api.prefix+'/caseDict')['content']
    return fixture, binding, current


def verify(api, route, mode):
    return api.call('POST', route+'/verify', {'mode':mode}, api_prefix=False)


def replace_during(api, verify_call, edit):
    ready, release = (api.root/name for name in ('fence-ready','fence-release'))
    for flag in (ready,release):
        flag.unlink(missing_ok=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(verify_call)
        try:
            deadline = time.monotonic()+12
            while not ready.exists():
                if reading.done():
                    raise AssertionError(reading.result())
                if time.monotonic() >= deadline:
                    raise TimeoutError('Gate did not enter its real-storage recheck')
                time.sleep(.02)
            edit()
        finally:
            release.touch()
        return reading.result(timeout=20)


def legacy(api, store, asset, sha256, report):
    route, binding, contents = configured_case(api, 'Legacy report references', {
        'runDir':RUN, 'derivedQuantities':{'referenceFields':{'expected':{
            'path':REFERENCE, 'field':'T', 'time':1, 'kind':'verification', 'l2_max':1e-6}}}})
    config = config_from_case_dict(parse_casedict(contents))
    assert config['referenceFields'][0]['path'] == REFERENCE
    prefix = binding['prefix']
    report['api_process'] = binding['api_process']
    put_live(api, store, prefix, REFERENCE, asset)
    artifact, data = 'D-post/data/postprocess-92/reference-receipt.csv', b'T,350\n'
    put_live(api, store, prefix, artifact, data)
    directory = 'D-post/data/postprocess-92/'
    for path, document in {
        'D-post/data/latest-postprocess.json': {'job_id':92},
        directory+'metrics.json':{'source':{'case_input_sha256':case_input_fingerprint(contents),
            'postprocess_sha256':postprocess_fingerprint(config)}, 'readiness':{'report_ready':True}},
        directory+'artifact-manifest.json':{'inputs':[{'path':REFERENCE, 'size':SIZE, 'sha256':sha256}],
            'artifacts':[{'path':artifact, 'size':len(data), 'sha256':hashlib.sha256(data).hexdigest()}]},
    }.items():
        put_live(api, store, prefix, path, json.dumps(document).encode())
    observed = verify(api, route, 'legacy')
    assert observed['value'] is None and observed['peak_traced_bytes'] < SIZE//4, observed
    assert verify(api, route, 'authoritative')['value'] is None
    report['legacy'] = {'size':SIZE, 'source_sha256':sha256, 'passing_peak_traced_bytes':observed['peak_traced_bytes']}
    observed = replace_during(api, lambda:verify(api, route, 'legacy_replacement'),
                              lambda:put_live(api, store, prefix, REFERENCE, b'changed verification'))
    expected = f'参考文件已变化，现有证据已过期: {REFERENCE}；请重新运行 postprocess:run'
    assert observed['value'] == expected, observed
    next_reason = verify(api, route, 'authoritative')['value']
    changed = f'计算输入文件已变化，现有证据已过期: {REFERENCE}；请重新计算'
    assert next_reason == changed, next_reason
    report['legacy'].update({'late_reference_change':expected, 'following_manifest_change':next_reason})


def native_files(root):
    seed = root/'native-thermal'
    for relative, contents in thermal_files().items():
        target = seed/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding='utf-8')
    directory = (seed/RUN).resolve()
    wsl_path = '/mnt/'+directory.drive[0].lower()+directory.as_posix()[2:]
    with (root/'openfoam.log').open('wb') as log:
        subprocess.run(['wsl.exe', '-d', 'vibeflow-linux', '-u', 'nobody', '--cd', wsl_path,
            '--exec', '/bin/bash', '-c',
            'source /usr/lib/openfoam/openfoam2406/etc/bashrc && blockMesh && laplacianFoam'],
            cwd=root, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=75,
            creationflags=subprocess.CREATE_NO_WINDOW)
    assert (directory/'1/T').is_file()
    return {p.relative_to(seed).as_posix():p for p in directory.rglob('*') if p.is_file()}


def typed(api, store, asset, report):
    route, binding, _ = configured_case(api, 'Typed extraction report gate', {
        'runDir':RUN, 'fields':{'T':{'unit':'K'}},
        'dataExtraction':{'volumeAverages':{'mean':{'field':'T','time':1}}},
        'derivedQuantities':{'referenceValues':{'analytic':{'metric':'"mean.T@1"','value':350,
            'unit':'K','kind':'verification','source':'"T(x)=300+100*x"','l2_max':1e-6}}}})
    seed = native_files(api.root)
    prefix = binding['prefix']
    for path, source in seed.items():
        put_live(api, store, prefix, path, source)
    job = api.call('POST', api.prefix+'/postprocess:run', {'run_dir':RUN})
    done = api.wait_job(job['job_id'], timeout=120)
    deadline = time.monotonic()+45
    while done.get('completion_pending', False):
        if time.monotonic() >= deadline:
            raise TimeoutError('Postprocessing compatibility publication did not settle')
        time.sleep(.1)
        done = api.call('GET', f'/jobs/{job["job_id"]}')
    result = api.call('GET', api.prefix+'/postprocess/results')
    mean = next(row['value'] for row in result['metrics']['metrics'] if row['name']=='mean.T@1')
    assert abs(mean-350) < 1e-6 and result['metrics']['readiness']['report_ready'], result
    assert result['publication']['status'] == 'committed' and not result['publication']['stale'], result
    assert verify(api, route, 'authoritative')['value'] is None
    expected = result['manifest']['run_inventory']
    put_live(api, store, prefix, IGNORED, asset)
    observed = verify(api, route, 'inventory')
    assert observed['value'] == expected and IGNORED not in observed['value'], observed
    assert not observed['body_get_keys'] and observed['peak_traced_bytes'] < SIZE//4, observed
    assert observed['selection_count'] == (2 if store else 0), observed
    report['typed'] = {'job_id':job['job_id'], 'publication_id':result['publication']['id'],
                       'mean_K':mean, 'ignored_size':SIZE, 'inventory':observed}
    if store:
        race = replace_during(api, lambda:verify(api, route, 'inventory_replacement'),
                              lambda:put_live(api, store, prefix, IGNORED, b'new external revision'))
        assert race['conflict'] == 'input_stale' and not race['body_get_keys'], race
        assert race['selection_count'] == 2, race
        report['typed']['s3_replacement'] = race
    extra = RUN+'/77/T'
    put_live(api, store, prefix, extra, seed[RUN+'/1/T'])
    changed = verify(api, route, 'inventory')
    assert changed['value'] == sorted([*expected,extra]) and not changed['body_get_keys'], changed
    reason = verify(api, route, 'authoritative')['value']
    assert reason in {'后处理发布或上游输入已失效，请重新提取',
                      '当前求解输入或输出时刻目录已变化，请重新提取'}, reason
    report['typed'].update({'new_time':extra, 'authoritative_stale':reason})


def shutdown(api, process):
    with httpx.Client(trust_env=False, timeout=10) as client:
        try:
            client.post(api.base+'/fixture/postprocess:stop', json={'pid':process['pid']},
                        headers={'Authorization':'Bearer '+api.token})
        except httpx.TransportError:
            pass
        else:
            raise AssertionError('Disposable verifier returned before its controlled exit')
    api.proc.wait(timeout=15)


def run(backend):
    from app.services import process_identity_native

    api = IsolatedServer('postprocess-gate-'+backend, app='postprocess_gate_fixture:app')
    context = ObjectStore(executable=RUNTIME, volume_max=12, authenticated=True) if backend == 's3' else nullcontext()
    report = {'passed':False, 'backend':backend, 'real_model_requests':0, 'mock_llm_calls':0}
    print('POSTPROCESS_GATE_STREAMING '+str(api.root), flush=True)
    try:
        with context as store:
            settings = store.settings() if store else {}
            module = api.root/'postprocess_gate_fixture.py'
            try:
                module.write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE, encoding='utf-8')
                with api:
                    asset, sha256 = source_file(api.root)
                    try:
                        legacy(api, store, asset, sha256, report)
                        typed(api, store, asset, report)
                    finally:
                        asset.unlink(missing_ok=True)
                        if 'api_process' in report:
                            shutdown(api, report['api_process'])
            finally:
                module.unlink(missing_ok=True)
        if store:
            assert store.proc.poll() is not None
            report['s3_stopped'] = True
            report['s3_receipt'] = str(store.root/'store.json')
        report['api_exit'] = process_identity_native.probe(report['api_process'])
        assert report['api_exit']['state'] == 'exited'
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__, 'message':str(error)[:1000]}
        raise
    finally:
        paths = (Path(__file__).resolve(), *(SERVER/'app/services'/name for name in (
            'case_evidence.py','file_read.py','postprocess_gate.py','postprocess_publication.py')))
        report['source_sha256'] = {path.relative_to(SERVER).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in paths}
        (api.root/'postprocess-gate-streaming.json').write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print('POSTPROCESS_GATE_STREAMING_OK '+str(api.root), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('fs','s3'), required=True)
    run(parser.parse_args().backend)
