"""Run real isolated ParaView and numerical postprocessing jobs with durable publication."""

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import tempfile

import aiohttp
from PIL import Image

from app.services.casedict import set_block
from linux_geometry_isolation_acceptance import OBSERVE_CHILD
from linux_openfoam_isolation_acceptance import IMAGE as FOAM_IMAGE, RUN as SOLVE_RUN, finished, namespace, registered_flow
from linux_solver_isolation_acceptance import fetch
from linux_terminal_compute_acceptance import account, request
from mixed_load_test_support import API_IMAGE, WEB_IMAGE, Deployment, docker
from postprocess_array_formula_acceptance import FOAM, RUN as ARCHIVE_RUN, assert_formula_outputs, config


EXAMPLES = Path(__file__).resolve().parent / 'examples/paraview'
SCRIPT_PATH = 'D-post/script space & %/naca.py'
CANARY = '/work/data/batch-host-fixture.txt'
ENV_CANARY = 'BATCH_ACCEPTANCE_FIXTURE'
SLOW_CHILD = 'paraview-acceptance-child'


async def put(client, endpoint, owner, path, value):
    token, case = owner
    return await request(client, endpoint, 'PUT', f'/cases/{case}/file', token=token,
                         json={'path': path, 'content': value})


async def paraview(client, api, peer, owner, backend, redis, scratch):
    token, case = owner
    route = f'/cases/{case}'
    data = (EXAMPLES/'naca0012-su2.vtu').read_bytes()
    form = aiohttp.FormData()
    form.add_field('file', data, filename='naca.vtu', content_type='application/octet-stream')
    uploaded = await request(client, api, 'POST', route+'/upload', token=token, params={'path': 'Ref/naca.vtu'}, data=form)
    assert uploaded['content_sha256'] == hashlib.sha256(data).hexdigest()
    factor = 1 if backend == 'pvpython' else 2
    await put(client, api, owner, 'Ref/render.json', json.dumps({'factor': factor}))
    guard = f'''import os
import socket
from pathlib import Path

assert not Path({CANARY!r}).exists()
assert {ENV_CANARY!r} not in os.environ
assert not (Path.cwd().parent / 'manifest.json').exists()
try:
    with socket.create_connection(({redis!r}, 6379), timeout=1):
        raise AssertionError('The worker network is visible inside ParaView')
except OSError:
    pass
'''
    await put(client, api, owner, SCRIPT_PATH, guard+'\n'+(EXAMPLES/'naca_postprocess.py').read_text(encoding='utf-8'))
    started = await request(client, api, 'POST', route+'/paraview:run', token=token,
                            json={'script_path': SCRIPT_PATH, 'backend': backend})
    job = await finished(client, peer, token, started['job_id'], scratch)
    attempt = job['attempt']
    child, = attempt['child_processes']['children'].values()
    assert child['purpose'] == 'paraview_run' and child['stop']['verified'] and child['stop']['scope_empty']
    publication = attempt['outputs']['publication']
    assert publication['status'] == 'committed'
    base = '.vibeflow/publications/'+publication['id']
    manifest = json.loads(await fetch(client, peer, owner, base+'/manifest.json'))
    hashes = {}
    outputs = {}
    for name, record in manifest['files'].items():
        if name not in {'D-post/statistics.json', 'D-post/pressure.png', 'D-post/sonic.csv'}:
            continue
        data = await fetch(client, peer, owner, base+'/files/'+name)
        assert hashlib.sha256(data).hexdigest() == record['sha256']
        outputs[name] = data
        hashes[name] = record['sha256']
    statistics = json.loads(outputs['D-post/statistics.json'])
    assert statistics['factor'] == factor and statistics['sonic_points'] > 1
    assert len(outputs['D-post/sonic.csv'].splitlines()) == statistics['sonic_points']+1
    with Image.open(io.BytesIO(outputs['D-post/pressure.png'])) as picture:
        assert picture.format == 'PNG' and picture.size == (640, 360)
        assert len(picture.convert('RGB').getcolors(picture.width*picture.height)) > 64
    return {'job': job['id'], 'network': namespace(child['identity']), 'child_stop': child['stop'],
            'root_stop': attempt['process_stop'], 'publication': publication['id'],
            'statistics': statistics, 'hashes': hashes}


async def cancel_paraview(client, api, peer, owner, worker, old, scratch):
    token, case = owner
    source = ('import subprocess\n'
              f'subprocess.run(["/usr/local/bin/python", "-I", "-c", "import time; time.sleep(90)", {SLOW_CHILD!r}],'
              ' check=True)\n')
    await put(client, api, owner, SCRIPT_PATH, source)
    started = await request(client, api, 'POST', f'/cases/{case}/paraview:run', token=token,
                            json={'script_path': SCRIPT_PATH, 'backend': 'pvpython'})
    route = f'/jobs/{started["job_id"]}'
    async with asyncio.timeout(55):
        while True:
            job = await request(client, peer, 'GET', route, token=token)
            assert job['ended_at'] is None, ('ParaView exited before cancellation', job['status'])
            children = job['attempt']['child_processes']['children']
            running = next((item for item in children.values() if item['phase'] == 'running'), None)
            if running is not None:
                parent_pid = str(running['identity']['pid'])
                pid = await asyncio.to_thread(docker, 'exec', worker, 'python', '-B', '-c',
                                              OBSERVE_CHILD, parent_pid, SLOW_CHILD)
                if pid:
                    assert pid.isdecimal(), pid
                    break
            await asyncio.sleep(.2)
    response = await request(client, peer, 'POST', route+':cancel', token=token)
    assert response['ok']
    job = await finished(client, peer, token, started['job_id'], scratch, expected='cancelled')
    attempt = job['attempt']
    child, = attempt['child_processes']['children'].values()
    assert child['stop']['verified'] and child['stop']['scope_empty']
    assert attempt['outputs'].get('publication', {}).get('status') != 'committed'
    assert docker('exec', worker, 'python', '-B', '-c', OBSERVE_CHILD, parent_pid, SLOW_CHILD) == ''
    path = 'D-post/pressure.png'
    for relative in (path, '.vibeflow/publications/'+old['publication']+'/files/'+path):
        assert hashlib.sha256(await fetch(client, peer, owner, relative)).hexdigest() == old['hashes'][path]
    return {'job': job['id'], 'observed_child': int(pid), 'root_stop': attempt['process_stop'],
            'child_stop': child['stop'], 'previous_publication_preserved': True}


async def postprocess(client, api, peer, owner, archive, scratch):
    token, case = owner
    prefix = f'/cases/{case}'
    if archive is not None:
        run_dir = ARCHIVE_RUN
        hashes = json.loads((archive/'inputs.json').read_text(encoding='utf-8'))
        assert set(hashes) == set(FOAM)
        for relative, expected in hashes.items():
            data = (archive/relative).read_bytes()
            assert hashlib.sha256(data).hexdigest() == expected, relative
            await put(client, api, owner, run_dir+'/'+relative, data.decode('utf-8'))
    else:
        run_dir = SOLVE_RUN
        await registered_flow(client, api, owner, scratch)
        exported = scratch/'foam-archive'
        hashes = {}
        for relative in FOAM:
            data = await fetch(client, peer, owner, run_dir+'/'+relative)
            path = exported/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            hashes[relative] = hashlib.sha256(data).hexdigest()
        (exported/'inputs.json').write_text(json.dumps(hashes, indent=2)+'\n', encoding='utf-8')
    original = await request(client, api, 'GET', prefix+'/caseDict', token=token)
    configured = set_block(original['content'], 'postProcessing', {**config(), 'runDir': run_dir})
    await request(client, api, 'PUT', prefix+'/caseDict', token=token,
                  json={'content': configured, 'expected_revision': original['dict_revision']})
    started = await request(client, api, 'POST', prefix+'/postprocess:run', token=token, json={'run_dir': run_dir})
    job = await finished(client, peer, token, started['job_id'], scratch)
    results = await request(client, peer, 'GET', prefix+'/postprocess/results', token=token, params={'job_id': job['id']})
    folder = results['result_dir']
    probes = (await fetch(client, peer, owner, folder+'/probes.csv')).decode('utf-8')
    line = (await fetch(client, peer, owner, folder+'/pressure_line.csv')).decode('utf-8')
    attempt = job['attempt']
    assert bool(attempt['workspace'].get('publications')) == (archive is None)
    assert results['publication']['status'] == 'committed'
    return {'job': job['id'], 'network': namespace(attempt['process_identity']), 'root_stop': attempt['process_stop'],
            'publication': results['publication']['id'], 'input_sha256': hashes,
            'verification': assert_formula_outputs(results, probes, line)}


async def verify(api, peer, redis, worker, scratch, evidence, archive):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as client:
        owners = [await account(client, api), await account(client, peer)]
        if archive is not None:
            evidence['paraview'] = {}
            for owner, backend in zip(owners, ('pvpython', 'pvbatch'), strict=True):
                evidence['paraview'][backend] = await paraview(client, api, peer, owner, backend, redis, scratch)
            evidence['paraview_cancel'] = await cancel_paraview(
                client, api, peer, owners[0], worker, evidence['paraview']['pvpython'], scratch)
        evidence['postprocess'] = await postprocess(client, api, peer, owners[0], archive, scratch)
        await request(client, peer, 'GET', f'/jobs/{evidence["postprocess"]["job"]}', token=owners[1][0], expected=404)


def main(args):
    scratch = Path(tempfile.mkdtemp(prefix='j16-batch-', dir=Path(__file__).resolve().parents[2]/'tmp'))
    print('BATCH_ISOLATION_EVIDENCE', scratch, flush=True)
    origin = set(docker('ps', '-q', '--no-trunc').splitlines())
    evidence = {'passed': False, 'storage': args.storage, 'registered_solve': args.from_solve, 'actual_model_calls': 0}
    image = args.api_image or (FOAM_IMAGE if args.from_solve else API_IMAGE)
    try:
        with ExitStack() as stack:
            deployment = Deployment(stack, scratch, image, WEB_IMAGE)
            docker('stop', deployment.worker)
            if args.storage == 's3':
                deployment.configure_s3(scratch)
            docker('run', '--rm', '--network', 'none', '--mount', deployment.data, '--entrypoint', '/bin/sh', image,
                   '-ec', f'install -d -o 65534 -g 65534 /work/data/files; touch {CANARY}')
            deployment.env[ENV_CANARY] = 'present'
            deployment.flags.extend(('-e', ENV_CANARY))
            deployment.worker = deployment.start_worker(deployment.label+'-isolated-worker')
            assert docker('exec', deployment.worker, 'id', '-u') == '65534'
            redis = docker('inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', deployment.redis)
            docker('exec', deployment.worker, 'python', '-I', '-c',
                   'import os,socket,sys; assert os.path.isfile(sys.argv[1]); assert os.environ[sys.argv[2]]; '
                   'socket.create_connection((sys.argv[3],6379),timeout=2).close()', CANARY, ENV_CANARY, redis)
            _, api = deployment.api(deployment.a)
            if args.storage == 's3':
                cache = deployment.volume('cold-peer-cache', '/work/data/files')
                docker('run', '--rm', '--network', 'none', '--mount', cache, '--entrypoint', '/usr/bin/chown',
                       image, '65534:65534', '/work/data/files')
                deployment.runtime.extend(('--mount', cache))
            reader, peer = deployment.api(deployment.b)
            if args.storage == 's3':
                assert docker('exec', reader, 'find', '/work/data/files', '-type', 'f') == ''
            asyncio.run(verify(api, peer, redis, deployment.worker, scratch, evidence, args.foam_archive))
            evidence['passed'] = True
    except Exception as error:
        evidence['failure'] = type(error).__name__+': '+str(error)[:1200]
        raise
    finally:
        evidence['original_services'] = origin == set(docker('ps', '-q', '--no-trunc').splitlines())
        evidence['passed'] = evidence['passed'] and evidence['original_services']
        (scratch/'result.json').write_text(json.dumps(evidence, indent=2)+'\n', encoding='utf-8')
        print('BATCH_ISOLATION_RESULT', evidence['passed'], flush=True)
    assert evidence['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage', choices=('fs', 's3'), required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--foam-archive', type=Path)
    source.add_argument('--from-solve', action='store_true')
    parser.add_argument('--api-image')
    main(parser.parse_args())
