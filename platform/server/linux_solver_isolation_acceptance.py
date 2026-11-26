"""Verify native solver isolation and numeric publication in disposable Docker. No LLM."""

import argparse
import asyncio
from contextlib import ExitStack
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile

import aiohttp
import numpy as np

from linux_calculix_acceptance import DECK, MATERIAL
from linux_su2_acceptance import CONFIG_PATH, EXAMPLES, MESH_PATH, fields
from linux_terminal_compute_acceptance import account, request
from mixed_load_calculix import check_displacements, timestamp, worker_records
from mixed_load_test_support import CCX_IMAGE, WEB_IMAGE, Deployment, docker


ELMER_EXAMPLE = Path(__file__).resolve().parent/'examples/elmer/axial'
ELMER_MODULES = '/run/elmer/share/elmersolver/lib'


@dataclass(frozen=True)
class Solver:
    name: str
    image: str
    setting: str
    binary: str
    input: str
    input_key: str
    dependency: str
    result: str
    parameters: tuple

    @property
    def packaged(self):
        if self.name == 'elmer':
            return '/work/acceptance-tools/elmer/bin/ElmerSolver'
        return '/usr/local/bin/' + self.binary

    @property
    def instrumented(self):
        if self.name == 'elmer':
            return '/work/acceptance-tools/elmer/bin/ElmerProbe'
        return '/work/acceptance-tools/' + self.binary

    def dependencies(self):
        if self.name == 'calculix':
            return {self.dependency: MATERIAL.encode()}
        if self.name == 'elmer':
            return {f'brick/{name}': (ELMER_EXAMPLE/'brick'/name).read_bytes()
                    for name in ('mesh.header', 'mesh.nodes', 'mesh.elements', 'mesh.boundary')}
        return {self.dependency: (EXAMPLES/'mesh_NACA0012_inv.su2').read_bytes()}

    def config(self, value, invalid=False, restart=False):
        if self.name == 'calculix':
            deck = DECK.replace(',-1000', f',-{value}')
            return deck.replace('1,1,2,3,4,5,6,7,8', '1,1,2,3,4,5,6,7,999') if invalid else deck
        if self.name == 'elmer':
            deck = (ELMER_EXAMPLE/'axial.sif').read_text(encoding='ascii')
            deck = deck.replace('Displacement 1 = 0.001', f'Displacement 1 = {value:g}')
            if invalid:
                deck = deck.replace('Output File = "axial.result"', 'Restart File = File "axial.result"\n'
                                    '  Restart Position = Integer 99\n  Output File = "invalid.result"')
                deck = deck.replace('Post File = "axial.vtu"', 'Post File = "invalid.vtu"')
            return deck
        config = (EXAMPLES/'inv_NACA0012.cfg').read_text('utf-8')
        config = config.replace('MESH_FILENAME= mesh_NACA0012_inv.su2', 'MESH_FILENAME= '+self.dependency)
        config = config.replace('AOA= 1.25', f'AOA= {value}')
        if invalid:
            return config.replace('SOLVER= EULER', 'SOLVER= INVALID_SOLVER')
        if restart:
            return (config.replace('RESTART_SOL= NO','RESTART_SOL= YES').replace('ITER= 250','ITER= 300')
                    .replace('SOLUTION_FILENAME= solution_flow','SOLUTION_FILENAME= restart_flow'))
        return config


CALCULIX_INPUT = 'C-run/space & cube/beam.inp'
SOLVERS = {
    'calculix': Solver('calculix', CCX_IMAGE, 'VIBEFLOW_CALCULIX', 'ccx', CALCULIX_INPUT, 'input_path',
                       'C-run/material.inp', CALCULIX_INPUT[:-4]+'.frd', (1000, 2000, 1500)),
    'su2': Solver('su2', 'vibeflow/api:linux-su2-v1-20261118', 'VIBEFLOW_SU2', 'SU2_CFD',
                  CONFIG_PATH, 'config_path', MESH_PATH, 'flow.vtu', (1.25, 2.0, 1.5)),
    'elmer': Solver('elmer', CCX_IMAGE, 'VIBEFLOW_ELMER', 'ElmerSolver',
                    'axial.sif', 'input_path', 'brick', 'brick/axial_t0001.vtu', (.001, .002, .0015)),
}


PROBE = """#!/usr/local/bin/python3
import hashlib
import os
from pathlib import Path
import socket
import sys
import time

assert all(hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest for name, digest in DEPENDENCIES.items())
assert not Path('/work/acceptance-tools/neighbor').exists()
assert not Path('/work/data/sibling-file').exists()
assert not Path('/workspace/server/app/main.py').exists()
assert not Path('/tmp/solver-sibling-file').exists()
assert not any(key in os.environ for key in ('VIBEFLOW_JWT_SECRET', 'VIBEFLOW_S3_SECRET_KEY'))
assert os.environ['HOME'] == '/tmp' and os.environ['TMPDIR'] == '/tmp'
assert Path('/proc/self/uid_map').read_text().split() == ['0', '65534', '1']
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
assert int(status['CapEff'], 16) == int(status['CapBnd'], 16) == 0
assert status['NoNewPrivs'].strip() == '1'
mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
assert all(any(entry[4] == mount and 'ro' in entry[5].split(',') for entry in mounts)
           for mount in READONLY)
if ELMER_MODULES:
    assert os.environ['ELMER_LIB'] == ELMER_MODULES
    assert (Path(ELMER_MODULES)/'StressSolve.so').is_file()
    assert not Path('/run/elmer/SHA256SUMS').exists()
try:
    with socket.create_connection((REDIS_IP, 6379), timeout=0.5):
        pass
except OSError:
    pass
else:
    raise AssertionError('Isolated solver reached the disposable Redis listener')
assert not Path('/tmp/solver-case-local').exists()
Path('/tmp/solver-case-local').touch()
# Keep the independently admitted children alive long enough to compare their
# simultaneous namespaces. The kernel can recycle IDs after a child stops.
time.sleep(3)
os.execv(EXECUTABLE, [EXECUTABLE, *sys.argv[1:]])
"""


async def fetch(client, endpoint, owner, path):
    token, case = owner
    for retry in range(16):
        async with client.get(endpoint+f'/api/v1/cases/{case}/raw', params={'path': path},
                              headers={'Authorization': 'Bearer '+token}) as response:
            if response.status == 200:
                if retry:
                    print('SOLVER_READ_RETRIED', retry, flush=True)
                return await response.read()
            detail = (await response.json()).get('detail') if response.status == 409 else None
            if (not isinstance(detail, dict) or detail.get('code') not in {'file_sync_busy', 'storage_quota_busy'}
                    or retry == 15):
                raise RuntimeError((path, response.status, (await response.text())[:400]))
        await asyncio.sleep(.5)


def elmer_fields(grid_path, strain):
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkCommonDataModel import VTK_HEXAHEDRON, VTK_QUAD
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader

    reader = vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(grid_path))
    reader.Update()
    grid = reader.GetOutput()
    cells = [grid.GetCellType(index) for index in range(grid.GetNumberOfCells())]
    assert grid.GetNumberOfPoints() == 12 and sorted(cells) == sorted([VTK_HEXAHEDRON]*2 + [VTK_QUAD]*2)
    displacement = vtk_to_numpy(grid.GetPointData().GetArray('displacement'))
    stress = vtk_to_numpy(grid.GetPointData().GetArray('stress_xx'))
    x = (vtk_to_numpy(grid.GetPoints().GetData())-displacement)[:, 0]
    assert set(np.round(x, 12)) == {0., .5, 1.}
    expected = np.zeros_like(displacement)
    expected[:, 0] = x * strain
    np.testing.assert_allclose(displacement, expected, rtol=0, atol=1e-12)
    np.testing.assert_allclose(stress, 210e9 * strain, rtol=0, atol=1e-5)
    return {'strain': strain, 'expected_stress_pa': 210e9 * strain,
            'max_stress_error_pa': float(np.max(np.abs(stress-210e9*strain))),
            'max_displacement_error_m': float(np.max(np.abs(displacement-expected)))}


async def solve(client, endpoint, owner, solver, value, *, invalid=False, packaged=False, restart=None):
    token, case = owner
    config = solver.config(value, invalid=invalid, restart=restart is not None)
    await request(client, endpoint, 'PUT', f'/cases/{case}/file', token=token,
                  json={'path': solver.input, 'content': config})
    row = await request(client, endpoint, 'POST', f'/cases/{case}/{solver.name}:solve', token=token,
                        json={solver.input_key: solver.input})
    jid = row['job_id']
    async with asyncio.timeout(150):
        while True:
            job = await request(client, endpoint, 'GET', f'/jobs/{jid}', token=token)
            if job['ended_at'] is not None and not job['completion_pending']:
                break
            await asyncio.sleep(.5)
    attempt = job['attempt']
    assert attempt['process_stop']['verified'] and attempt['process_stop']['scope_empty'], job
    child, = attempt['child_processes']['children'].values()
    assert child['purpose'] == solver.name+'_solve' and child['phase'] == 'stopped', child
    assert child['stop']['verified'] and child['stop']['scope_empty'], child
    proof = child['identity']['linux_network']
    assert proof['host'] != proof['init'], proof
    assert child['identity']['host']['uid'] == 65534
    if invalid:
        assert job['status'] == 'failed', job
        assert attempt['outputs'].get('publication', {}).get('status') != 'committed'
        return {'job': jid, 'native_stop_verified': True, 'network': proof}, None
    assert job['status'] == 'succeeded' and job['exit_code'] == 0, job
    publication = attempt['outputs']['publication']
    assert publication['status'] == 'committed'
    base = '.vibeflow/publications/' + publication['id']
    manifest = json.loads(await fetch(client, endpoint, owner, base+'/manifest.json'))
    assert manifest['descriptor']['executable'] == (solver.packaged if packaged else solver.instrumented)
    source = manifest['descriptor']['source']['files']
    for name, content in solver.dependencies().items():
        assert source[name]['sha256'] == hashlib.sha256(content).hexdigest()
    if restart is not None:
        assert source['restart_flow.dat']['sha256'] == hashlib.sha256(restart).hexdigest()
    data = await fetch(client, endpoint, owner, solver.result)
    assert data == await fetch(client, endpoint, owner, base+'/files/'+solver.result)
    digest = hashlib.sha256(data).hexdigest()
    assert digest == manifest['files'][solver.result]['sha256']
    result = {'job': jid, 'publication': publication['id'], 'result_sha256': digest,
              'native_stop_verified': True, 'network': proof, 'child': child['id'], 'child_stopped_at': child['ended_at']}
    if solver.name == 'calculix':
        _, error = check_displacements(data, value)
        result.update(load=value, max_absolute_error=error)
    elif solver.name == 'elmer':
        checkpoint = await fetch(client, endpoint, owner, 'brick/axial.result')
        assert checkpoint.lstrip().startswith(b'ASCII 3')
        assert hashlib.sha256(checkpoint).hexdigest() == manifest['files']['brick/axial.result']['sha256']
        with tempfile.TemporaryDirectory() as scratch:
            grid = Path(scratch)/'axial.vtu'
            grid.write_bytes(data)
            result.update(elmer_fields(grid, value))
    else:
        history = await fetch(client, endpoint, owner, 'history.csv')
        assert hashlib.sha256(history).hexdigest() == manifest['files']['history.csv']['sha256']
        rows = [{key.strip(): float(number) for key, number in item.items()}
                for item in csv.DictReader(io.StringIO(history.decode()), skipinitialspace=True)]
        assert rows and all(math.isfinite(number) for item in rows for number in item.values())
        residual = next(key for key in rows[0] if 'rms' in key.lower() and 'rho' in key.lower())
        if restart is None:
            assert rows[-1][residual] < rows[0][residual] - 1
        with tempfile.TemporaryDirectory() as scratch:
            grid = Path(scratch)/'flow.vtu'
            grid.write_bytes(data)
            summary = fields(grid)
        result.update(angle_of_attack=value, density_residual=[rows[0][residual], rows[-1][residual]],
                      iterations=len(rows), fields=summary, restart=restart is not None)
    return result, data


async def verify(endpoint, reader, solver, receipt):
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=45)) as client:
        owners = [await account(client, endpoint) for _ in range(2)]
        dependencies = {name: content.decode('utf-8') for name,content in solver.dependencies().items()}
        for token, case in owners:
            for name, content in dependencies.items():
                await request(client, endpoint, 'PUT', f'/cases/{case}/file', token=token,
                              json={'path': name, 'content': content})
        first_value, second_value, packaged_value = solver.parameters
        (first, data), (second, other) = await asyncio.gather(
            solve(client, endpoint, owners[0], solver, first_value),
            solve(client, endpoint, owners[1], solver, second_value))
        assert data != other and first['network']['init'] != second['network']['init']
        await request(client, endpoint, 'GET', f"/jobs/{first['job']}", token=owners[1][0], expected=404)
        if solver.name == 'su2':
            restart = await fetch(client, endpoint, owners[0], 'restart_flow.dat')
            continued, data = await solve(client, endpoint, owners[0], solver, first_value, restart=restart)
            original = '.vibeflow/publications/'+first['publication']+'/files/'+solver.result
            assert hashlib.sha256(await fetch(client, endpoint, owners[0], original)).hexdigest() == first['result_sha256']
            assert continued['result_sha256'] != first['result_sha256']
            receipt['continued_solver'] = continued
        invalid, _ = await solve(client, endpoint, owners[0], solver, first_value, invalid=True)
        receipt.update(jobs=[first, second], invalid=invalid, cross_owner_status=404)
        assert await fetch(client, endpoint, owners[0], solver.result) == data
        assert await fetch(client, endpoint, owners[1], solver.result) == other
        if receipt['storage'] == 's3':
            assert await fetch(client, reader, owners[0], solver.result) == data
            assert await fetch(client, reader, owners[1], solver.result) == other
            receipt['cold_peer_publications'] = True
        owner = await account(client, reader)
        token, case = owner
        for name, content in dependencies.items():
            await request(client, reader, 'PUT', f'/cases/{case}/file', token=token,
                          json={'path': name, 'content': content})
        receipt['packaged_solver'], _ = await solve(client, reader, owner, solver, packaged_value, packaged=True)


def main(args):
    solver = SOLVERS[args.solver]
    root = Path(tempfile.mkdtemp(prefix=f'j16-{solver.name}-', dir=Path(__file__).resolve().parents[2]/'tmp'))
    print('SOLVER_ISOLATION_EVIDENCE', root, flush=True)
    original = set(docker('ps', '-q', '--no-trunc').splitlines())
    receipt = {'solver': solver.name, 'storage': args.storage, 'passed': False,
               'actual_model_calls': 0, 'simulated_model_calls': 0}
    try:
        with ExitStack() as stack:
            deployment = Deployment(stack, root, solver.image, WEB_IMAGE)
            docker('stop', deployment.worker)
            if args.storage == 's3':
                deployment.configure_s3(root)
            redis_ip = docker('inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',
                              deployment.redis)
            fixture = root/'probe'
            preamble, body = PROBE.split('\n', 1)
            volume = deployment.volume('proof-tool', '/work/acceptance-tools')
            mounts = []
            prepare = ''
            modules = None
            execute = solver.packaged
            readonly = ['/usr', solver.instrumented]
            if solver.name == 'elmer':
                bundle = args.elmer_bundle.resolve(strict=True)
                runtime = deployment.volume('elmer-runtime', '/opt')
                mounts = ['--mount', runtime, '--mount', f'type=bind,source={bundle},target=/bundle,readonly']
                prepare = ('cd /bundle; sha256sum --quiet -c SHA256SUMS; '
                           'mkdir -p /opt/elmer /work/acceptance-tools/elmer; '
                           'cp -R /bundle/. /opt/elmer/; cp -R /bundle/. /work/acceptance-tools/elmer/; '
                           'chmod 0555 /opt/elmer/bin/ElmerSolver /work/acceptance-tools/elmer/bin/ElmerSolver; ')
                execute = '/opt/elmer/bin/ElmerSolver'
                modules = ELMER_MODULES
                readonly = ['/usr', '/run/elmer/bin/ElmerProbe', ELMER_MODULES]
                deployment.runtime.extend(('--mount', runtime+',readonly'))
            dependencies = {name: hashlib.sha256(content).hexdigest() for name,content in solver.dependencies().items()}
            constants = {'REDIS_IP': redis_ip, 'DEPENDENCIES': dependencies, 'READONLY': readonly,
                         'EXECUTABLE': execute, 'ELMER_MODULES': modules}
            fixture.write_bytes((preamble+'\n'+''.join(f'{key} = {value!r}\n' for key, value in constants.items())+body).encode())
            docker('run', '--rm', '--network', 'none', '--pull=never', '--mount', volume, '--mount', deployment.data, *mounts,
                   '--mount', f'type=bind,source={fixture},target=/probe,readonly', '--entrypoint', '/bin/sh', solver.image,
                   '-ec', prepare+'install -D -m 0555 /probe "$1"; touch /work/acceptance-tools/neighbor; '
                          'install -d -o 65534 -g 65534 /work/data/files; touch /work/data/sibling-file',
                   'prepare-fixture', solver.instrumented)
            deployment.runtime.extend(('--mount', volume+',readonly'))
            deployment.env[solver.setting] = solver.instrumented
            deployment.flags.extend(('-e', solver.setting))
            worker = deployment.worker = deployment.start_worker(deployment.label+'-isolated-worker')
            assert docker('exec', worker, 'id', '-u') == '65534'
            docker('exec', worker, 'python', '-c',
                   'import socket,sys; from pathlib import Path; '
                   'assert Path("/work/acceptance-tools/neighbor").is_file(); '
                   'assert Path("/work/data/sibling-file").is_file(); '
                   'Path("/tmp/solver-sibling-file").touch(); socket.create_connection((sys.argv[1],6379),2).close()',
                   redis_ip)
            _, endpoint = deployment.api(deployment.a)
            deployment.env[solver.setting] = solver.packaged
            if args.storage == 's3':
                cache = deployment.volume('cold-peer-cache', '/work/data/files')
                docker('run', '--rm', '--network', 'none', '--pull=never', '--mount', cache,
                       '--entrypoint', '/usr/bin/chown', solver.image, '65534:65534', '/work/data/files')
                deployment.runtime.extend(('--mount', cache))
            peer, reader = deployment.api(deployment.b)
            if args.storage == 's3':
                assert docker('exec', peer, 'find', '/work/data/files', '-type', 'f') == ''
            asyncio.run(verify(endpoint, reader, solver, receipt))
            observed = {row['job']: row for row in worker_records(deployment)}
            started = [timestamp(observed[row['job']]['child_running_at'][row['child']]) for row in receipt['jobs']]
            stopped = [timestamp(row['child_stopped_at']) for row in receipt['jobs']]
            receipt['native_overlap_ms'] = min(stopped) - max(started)
            assert receipt['native_overlap_ms'] > 0, receipt
            docker('exec', worker, 'test', '!', '-e', '/tmp/solver-case-local')
            receipt['passed'] = True
    except Exception as error:
        receipt['error'] = type(error).__name__
        raise
    finally:
        receipt['original_services'] = original == set(docker('ps', '-q', '--no-trunc').splitlines())
        receipt['passed'] = receipt['passed'] and receipt['original_services']
        (root/'result.json').write_text(json.dumps(receipt, indent=2)+'\n', encoding='utf-8')
    assert receipt['passed']
    print('SOLVER_ISOLATION_OK', solver.name, args.storage, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--solver', choices=SOLVERS, default='calculix')
    parser.add_argument('--storage', choices=('fs', 's3'), default='fs')
    parser.add_argument('--elmer-bundle', type=Path, help='Source-built Linux Elmer serial release with SHA256SUMS')
    args = parser.parse_args()
    if args.solver == 'elmer' and args.elmer_bundle is None:
        parser.error('--elmer-bundle is required for the Elmer run')
    main(args)
