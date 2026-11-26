"""Verify real isolated CadQuery jobs on filesystem or authenticated S3 storage."""

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import json
import math
from pathlib import Path
import tempfile

import aiohttp

from app.services.casedict import set_block
from linux_openfoam_isolation_acceptance import finished, namespace
from linux_terminal_compute_acceptance import account, request
from mixed_load_test_support import API_IMAGE, WEB_IMAGE, Deployment, docker


CANARY = '/work/data/geometry-host-fixture.txt'
CANARY_ENV = 'GEOMETRY_ACCEPTANCE_FIXTURE'
VENV = '/work/data/geometry-python'
SLOW_CHILD = 'cadquery-acceptance-child'
STEM = 'example-box'
SOURCE = '''import os
from pathlib import Path
import socket
import subprocess
import sys

import cadquery as cq
from fixture_dims import depth, height, hold

def build_geometry(ctx):
    assert Path.cwd() == ctx.case_root
    assert not Path({canary!r}).exists()
    assert {canary_env!r} not in os.environ
    assert sys.prefix == {python_prefix!r}, sys.prefix
    try:
        with socket.create_connection(({redis_ip!r}, 6379), timeout=1):
            raise AssertionError('The worker network is reachable from CadQuery')
    except OSError:
        pass
    if hold:
        child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(90)', {child!r}])
        child.wait()
    ctx.log('geometry-boundary-verified')
    return {{{stem!r}: cq.Workplane('XY').box(float(ctx.params['width']), depth, height)}}

def export_geometry(ctx, objects):
    ctx.artifact_path('export-hook.txt').write_text('real-cadquery-export', encoding='utf-8')
'''
OBSERVE_CHILD = '''
from pathlib import Path
import sys
ancestor = int(sys.argv[1])
for proc in Path('/proc').glob('[0-9]*'):
    try:
        arguments = (proc / 'cmdline').read_bytes().split(b'\\0')
        if sys.argv[2].encode() not in (arguments[:1] if len(sys.argv) > 3 else arguments):
            continue
        pid = int(proc.name)
        while pid > 1 and pid != ancestor:
            fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[-1].split()
            pid = int(fields[1])
        if pid == ancestor:
            print(proc.name)
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue
'''


async def write_file(client, endpoint, token, case, path, content):
    return await request(client, endpoint, 'PUT', f'/cases/{case}/file', token=token,
                         json={'path': path, 'content': content})


async def seed(client, endpoint, owner, width, source):
    token, case = owner
    route = f'/cases/{case}/caseDict'
    current = await request(client, endpoint, 'GET', route, token=token)
    dictionary = set_block(current['content'], 'geometry', {'parameters': {'width': width}})
    await request(client, endpoint, 'PUT', route, token=token,
                  json={'content': dictionary, 'expected_revision': current['dict_revision']})
    await write_file(client, endpoint, token, case, 'A-geo/fixture_dims.py', 'depth=2\nheight=3\nhold=False\n')
    return (await write_file(client, endpoint, token, case, 'A-geo/geometry.py', source))['auto_preview_job']


def receipt(job):
    attempt = job['attempt']
    return {'job': job['id'], 'network': namespace(attempt['process_identity']), 'stop': attempt['process_stop']}


async def preview_results(client, peer, owner, width, tolerance):
    token, case = owner
    route = f'/cases/{case}/geometry'
    scene = await request(client, peer, 'GET', route+'/scene', token=token)
    report = await request(client, peer, 'GET', route+'/validation', token=token)
    geometry, = scene['objects']
    solid, = report['objects']
    assert geometry['name'] == solid['name'] == STEM and geometry['faces'] and geometry['vertices']
    assert math.isclose(geometry['bbox']['max'][0] - geometry['bbox']['min'][0], width, abs_tol=1e-5)
    assert math.isclose(solid['volume'], width*6, abs_tol=1e-8)
    assert report['summary']['valid'] and report['summary']['watertight']
    assert 'geometry-boundary-verified' in scene['messages']
    if tolerance is not None:
        assert scene['tolerance'] == tolerance
    assert scene['publication']['id'] == report['publication']['id']
    return {'publication': scene['publication']['id'], 'volume': solid['volume'], 'tolerance': scene['tolerance']}


async def exports(client, peer, owner, publication, scratch=None):
    token, case = owner
    route = f'/cases/{case}/geometry'
    result = await request(client, peer, 'GET', route+'/results', token=token,
                           params={'publication_id': publication})
    assert result['publication']['status'] == 'committed'
    assert result['publication']['id'] == publication
    files = {}
    for item in result['files']:
        async with client.get(peer+'/api/v1'+route+'/artifact', headers={'Authorization': 'Bearer '+token},
                              params={'publication_id': publication, 'path': item['path']}) as response:
            assert response.status == 200, (response.status, await response.text())
            data = await response.read()
        assert len(data) == item['size'] and hashlib.sha256(data).hexdigest() == item['sha256']
        name = Path(item['path']).name
        if scratch is not None:
            (scratch / name).write_bytes(data)
        files[name] = item['sha256']
    assert set(files) == {STEM+'.step', STEM+'.stl'}
    return files


async def cancel_export(client, endpoint, peer, owner, deployment, scratch, original):
    token, case = owner
    await write_file(client, endpoint, token, case, 'A-geo/fixture_dims.py', 'depth=2\nheight=3\nhold=True\n')
    pending = await request(client, endpoint, 'POST', f'/cases/{case}/geometry:export', token=token, json={})
    route = f'/jobs/{pending["job_id"]}'
    async with asyncio.timeout(55):
        while True:
            job = await request(client, peer, 'GET', route, token=token)
            if job['ended_at'] is not None:
                raise AssertionError(('Export exited before cancellation', job['status'], job['log_backlog'][-500:]))
            process = job['attempt'].get('process_identity')
            if process is not None:
                worker_pid = str(process['pid'])
                child = await asyncio.to_thread(docker, 'exec', deployment.worker, 'python', '-B', '-c',
                                                OBSERVE_CHILD, worker_pid, SLOW_CHILD)
                if child:
                    assert child.isdecimal(), child
                    break
            await asyncio.sleep(.2)
    cancelled = await request(client, peer, 'POST', route+':cancel', token=token)
    assert cancelled['ok']
    job = await finished(client, peer, token, pending['job_id'], scratch, expected='cancelled')
    assert docker('exec', deployment.worker, 'python', '-B', '-c', OBSERVE_CHILD, worker_pid, SLOW_CHILD) == ''
    assert job['attempt']['outputs'].get('publication', {}).get('status') != 'committed'
    assert await exports(client, peer, owner, original['publication']) == original['files']
    return {**receipt(job), 'observed_child': int(child), 'prior_artifacts_preserved': True}


async def verify(endpoint, peer, deployment, scratch, evidence, *, redis_ip, python_prefix):
    source = SOURCE.format(canary=CANARY, canary_env=CANARY_ENV, redis_ip=redis_ip, python_prefix=python_prefix,
                           child=SLOW_CHILD, stem=STEM)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as client:
        first = await account(client, endpoint)
        second = await account(client, peer)
        for owner, width in ((first, 4), (second, 10)):
            seeded = await seed(client, endpoint, owner, width, source)
            token, case = owner
            job = await finished(client, peer, token, seeded, scratch)
            evidence.setdefault('automatic_previews', []).append({
                **receipt(job), **await preview_results(client, peer, owner, width, None)})
        token, case = first
        route = f'/cases/{case}/geometry'
        await request(client, peer, 'GET', route+'/scene', token=second[0], expected=404)
        manual = await request(client, endpoint, 'POST', route+':preview', token=token, json={'tolerance': .15})
        job = await finished(client, peer, token, manual['job_id'], scratch)
        evidence['manual_preview'] = {**receipt(job), **await preview_results(client, peer, first, 4, .15)}
        assert evidence['manual_preview']['publication'] == manual['publication_id']
        await request(client, peer, 'GET', f'/cases/{case}/file', token=token, expected=404,
                      params={'path': 'A-geo/export-hook.txt'})
        result = await request(client, endpoint, 'POST', route+':export', token=token, json={})
        job = await finished(client, peer, token, result['job_id'], scratch)
        file = await request(client, peer, 'GET', f'/cases/{case}/file', token=token,
                             params={'path': 'A-geo/export-hook.txt'})
        assert file['content'] == 'real-cadquery-export'
        active = await preview_results(client, peer, first, 4, None)
        assert active['publication'] == result['publication_id']
        evidence['export'] = {**receipt(job), **active, 'files': await exports(client, peer, first, active['publication'], scratch)}
        evidence['cancelled_export'] = await cancel_export(client, endpoint, peer, first, deployment, scratch, evidence['export'])


def main(args):
    scratch = Path(tempfile.mkdtemp(prefix='j16-geometry-', dir=Path(__file__).resolve().parents[2]/'tmp'))
    print('GEOMETRY_ISOLATION_EVIDENCE', scratch, flush=True)
    original = set(docker('ps', '-q', '--no-trunc').splitlines())
    evidence = {'passed': False, 'storage': args.storage, 'actual_model_calls': 0, 'simulated_model_calls': 0}
    try:
        with ExitStack() as stack:
            deployment = Deployment(stack, scratch, args.api_image, WEB_IMAGE)
            docker('stop', deployment.worker)
            if args.storage == 's3':
                deployment.configure_s3(scratch)
            provision = f'install -d -o 65534 -g 65534 /work/data/files; touch {CANARY}'
            if args.runtime == 'venv':
                provision += f'; python -m venv --copies --without-pip --system-site-packages {VENV}'
            docker('run', '--rm', '--network', 'none', '--mount', deployment.data, '--entrypoint', '/bin/sh',
                   args.api_image, '-ec', provision)
            deployment.env[CANARY_ENV] = 'present-in-worker'
            deployment.flags.extend(('-e', CANARY_ENV))
            if args.runtime == 'venv':
                deployment.runtime.extend(('-e', f'VIBEFLOW_PYTHON={VENV}/bin/python'))
            python_prefix = VENV if args.runtime == 'venv' else '/usr/local'
            evidence['python_prefix'] = python_prefix
            deployment.worker = deployment.start_worker(deployment.label+'-isolated-worker')
            assert docker('exec', deployment.worker, 'id', '-u') == '65534'
            redis_ip = docker('inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', deployment.redis)
            control = ('import os,socket,sys; from pathlib import Path; assert Path(sys.argv[1]).is_file(); '
                       'assert os.environ[sys.argv[2]]; socket.create_connection((sys.argv[3],6379),timeout=2).close()')
            docker('exec', deployment.worker, 'python', '-I', '-c', control, CANARY, CANARY_ENV, redis_ip)
            _, endpoint = deployment.api(deployment.a)
            if args.storage == 's3':
                cache = deployment.volume('cold-peer-cache', '/work/data/files')
                docker('run', '--rm', '--network', 'none', '--mount', cache, '--entrypoint', '/usr/bin/chown',
                       args.api_image, '65534:65534', '/work/data/files')
                deployment.runtime.extend(('--mount', cache))
            reader, peer = deployment.api(deployment.b)
            if args.storage == 's3':
                assert docker('exec', reader, 'find', '/work/data/files', '-type', 'f') == ''
            asyncio.run(verify(endpoint, peer, deployment, scratch, evidence, redis_ip=redis_ip, python_prefix=python_prefix))
            evidence['boundary'] = {'outside_file_hidden': True, 'worker_env_hidden': True, 'worker_redis_tcp_denied': True,
                                    'other_account_denied': True, 'case_write': True}
            volume = docker('run', '--rm', '--network', 'none', '--entrypoint', 'python',
                            '--mount', f'type=bind,source={scratch},target=/exports,readonly', args.api_image, '-I', '-c',
                            f'import cadquery as cq; print(cq.importers.importStep("/exports/{STEM}.step").val().Volume())')
            assert math.isclose(float(volume), 24, abs_tol=1e-8), volume
            evidence['reimported_step_volume'] = float(volume)
            evidence['passed'] = True
    except Exception as error:
        evidence['failure'] = type(error).__name__+': '+str(error)[:1200]
        raise
    finally:
        evidence['original_services'] = original == set(docker('ps', '-q', '--no-trunc').splitlines())
        evidence['passed'] = evidence['passed'] and evidence['original_services']
        (scratch/'result.json').write_text(json.dumps(evidence, indent=2)+'\n', encoding='utf-8')
        print('GEOMETRY_ISOLATION_RESULT', evidence['passed'], flush=True)
    assert evidence['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage', choices=('fs', 's3'), required=True)
    parser.add_argument('--runtime', choices=('system', 'venv'), default='system')
    parser.add_argument('--api-image', default=API_IMAGE)
    main(parser.parse_args())
