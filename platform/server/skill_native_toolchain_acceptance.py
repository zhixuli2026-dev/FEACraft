"""Run the examples against installed solver toolchains through the production jobs API."""

import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
from urllib.parse import quote
from xml.etree import ElementTree

from testsupport import IsolatedServer, SERVER


def check_lammps(outputs):
    trajectory = outputs['relaxed.lammpstrj'].decode('ascii').splitlines()
    first = trajectory.index('ITEM: ATOMS id type x y z') + 1
    positions = [tuple(float(value) for value in row.split()[2:]) for row in trajectory[first:]]
    assert len(positions) == 2
    distance = math.dist(*positions)
    assert abs(distance - 1) < 1e-6
    assert 'Minimization stats:' in outputs['log.lammps'].decode('utf8')
    return {'particles': len(positions), 'bond_length': distance}


def check_freefem(outputs):
    summary, = csv.DictReader(io.StringIO(outputs['poisson.csv'].decode('ascii')))
    assert int(summary['triangles']) == 2048 and 0 < float(summary['l2_error']) < 2e-5
    assert abs(float(summary['center_value']) - 1) < 2e-5
    rows = list(csv.DictReader(io.StringIO(outputs['poisson-section.csv'].decode('ascii'))))
    assert len(rows) == 33
    max_error = max(abs(float(row['numerical']) - math.sin(math.pi * float(row['x']))) for row in rows)
    assert max_error < 3e-5
    mesh = outputs['poisson.mesh'].decode('ascii').split()
    assert int(mesh[mesh.index('Triangles') + 1]) == 2048
    return {'triangles': 2048, 'l2_error': float(summary['l2_error']), 'maximum_section_error': max_error}


def check_scilab(outputs):
    rows = [tuple(map(float, row)) for row in csv.reader(io.StringIO(outputs['decay.csv'].decode('ascii')))]
    assert len(rows) == 41 and rows[0] == (0.0, 1.0, 1.0) and rows[-1][0] == 2.0
    max_error = max(abs(value - math.exp(-2 * time)) for time, value, _ in rows)
    assert max_error < 1e-8
    return {'samples': len(rows), 'maximum_error': max_error, 'final_value': rows[-1][1]}


def check_petsc(outputs):
    result = json.loads(outputs['petsc-summary.json'])
    assert result['solver'] == 'cg' and result['preconditioner'] == 'jacobi'
    assert result['converged_reason'] > 0 and result['residual_l2'] < 1e-11 and 1 < result['iterations'] < 64
    rows = [(float(row['x']), float(row['value'])) for row in csv.DictReader(
        io.StringIO(outputs['petsc-poisson.csv'].decode('ascii')))]
    assert len(rows) == 63 and rows[0][0] == 1/64 and rows[-1][0] == 63/64
    error = max(abs(value - x * (1 - x)) for x, value in rows)
    assert error < 1e-10
    return {**result, 'points': len(rows), 'maximum_error': error}


TOOLS = {
    'lammps': ('lammps/relax-dimer.in', 'env OMP_NUM_THREADS=1 /usr/bin/lmp -in relax-dimer.in',
               ('relaxed.lammpstrj', 'log.lammps'), 'lammps', check_lammps),
    'freefem': ('freefem/poisson.edp', '/usr/bin/FreeFem++ -nw poisson.edp',
                ('poisson.csv', 'poisson-section.csv', 'poisson.mesh'), 'freefem++', check_freefem),
    'scilab': ('scilab/decay.sce', 'env HOME="$PWD" /usr/bin/scilab-cli -nb -f decay.sce',
               ('decay.csv',), 'scilab-cli', check_scilab),
    'petsc': ('petsc/poisson_line.py',
              'env PETSC_DIR=/usr/lib/petscdir/petsc3.19/x86_64-linux-gnu-real /usr/bin/python3 poisson_line.py',
              ('petsc-poisson.csv', 'petsc-summary.json'), 'python3-petsc4py-real', check_petsc),
}


def download_outputs(api, attempt, filenames):
    workspace = attempt['workspace']
    outputs = {}
    for name in filenames:
        relative = workspace['run_path'] + '/' + name
        expected = attempt['outputs']['files'][name]
        data = api.call('GET', api.prefix + '/raw?path=' + quote(relative), raw=True)
        assert hashlib.sha256(data).hexdigest() == expected['sha256']
        assert len(data) == expected['size']
        outputs[name] = data
    return outputs


def verify_openvsp(api, distribution):
    source = SERVER / 'examples' / 'openvsp' / 'wing_polar.py'
    api.put(source.name, source.read_text('utf8'))
    job = api.call('POST', api.prefix + '/jobs', {
        'runtime': 'windows', 'input_paths': [source.name], 'timeout_seconds': 60,
        'command': subprocess.list2cmdline([sys.executable, source.name, '--distribution', str(distribution.resolve(strict=True))]),
    })
    attempt = api.wait_job(job['id'], timeout=80)['attempt']
    assert attempt['process_stop']['verified'] and attempt['process_stop']['scope_empty']
    assert list(attempt['workspace']['files']) == [source.name]
    names = ['wing-results/' + name for name in (
        'VerificationWing.vsp3', 'VerificationWing.polar', 'VerificationWing.adb', 'wing-polar.csv', 'wing-summary.json')]
    outputs = download_outputs(api, attempt, names)
    metrics = json.loads(outputs['wing-results/wing-summary.json'])
    rows = [(float(row['alpha_deg']), float(row['CL'])) for row in csv.DictReader(
        io.StringIO(outputs['wing-results/wing-polar.csv'].decode('ascii')))]
    assert metrics['alpha_deg'] == [0.0, 3.0, 6.0] and [alpha for alpha, _ in rows] == metrics['alpha_deg']
    lift = metrics['lift_coefficient']
    assert abs(lift[0]) < .05 and .15 < lift[1] < lift[2] < 1.0
    assert [value for _, value in rows] == lift
    assert (lift[2] - lift[0]) / math.radians(6) > 3
    return {'job_id': job['id'], 'runtime': 'windows', 'stop': attempt['process_stop'], 'metrics': metrics,
            'downloads': {name: attempt['outputs']['files'][name] for name in names}}


def verify_fenicsx(api, interpreter):
    if not interpreter.is_absolute():
        raise ValueError('FEniCSx requires an absolute interpreter path in WSL')
    source = SERVER / 'examples' / 'fenicsx' / 'poisson_square.py'
    api.put(source.name, source.read_text('utf8'))
    command = ('id -u > uid.txt && env HOME="$PWD" XDG_CACHE_HOME="$PWD/.compile-cache" '
               + shlex.quote(str(interpreter)) + ' ' + source.name)
    job = api.call('POST', api.prefix + '/jobs', {
        'runtime': 'wsl', 'input_paths': [source.name], 'timeout_seconds': 80, 'command': command,
    })
    attempt = api.wait_job(job['id'], timeout=100)['attempt']
    stop = attempt['process_stop']
    assert stop['verified'] and stop['scope_empty'] and stop['native_state'] == 'exited'
    assert list(attempt['workspace']['files']) == [source.name]
    names = ('fenics-summary.json', 'poisson-square.xdmf', 'poisson-square.h5')
    outputs = download_outputs(api, attempt, (*names, 'uid.txt'))
    assert outputs['uid.txt'].strip() == b'65534'
    metrics = json.loads(outputs['fenics-summary.json'])
    assert metrics['coarse']['cells'] == 1152 and metrics['fine']['cells'] == 4608
    assert metrics['fine']['degrees_of_freedom'] == 2401 and metrics['fine']['l2_error'] < 5e-5
    assert metrics['coarse']['reason'] > 0 and metrics['fine']['reason'] > 0
    assert 3.8 < metrics['error_ratio'] < 4.2
    document = ElementTree.fromstring(outputs['poisson-square.xdmf'])
    assert document.find('.//Topology').get('NumberOfElements') == '4608'
    field = document.find('.//Attribute[@Name="u"]/DataItem')
    assert field.get('Dimensions') == '2401 1' and field.text.startswith('poisson-square.h5:')
    assert outputs['poisson-square.h5'].startswith(b'\x89HDF\r\n\x1a\n')
    return {'job_id': job['id'], 'runtime': 'wsl', 'uid': 65534, 'stop': stop, 'metrics': metrics,
            'workspace': attempt['workspace']['run_path'],
            'downloads': {name: attempt['outputs']['files'][name] for name in names}}


def main(openvsp=None, fenicsx=None):
    api = IsolatedServer('ff-f64-managed-tools')
    proof = {'passed': False, 'actual_llm_calls': 0, 'simulated_llm_calls': 0, 'jobs': {}}
    try:
        with api:
            api.create_case('Native skill toolchains')
            for name, (example, argv, files, package, check) in TOOLS.items():
                input_path = SERVER / 'examples' / example
                api.put(input_path.name, input_path.read_text('utf8'))
                job = api.call('POST', api.prefix + '/jobs', {
                    'runtime': 'wsl', 'input_paths': [input_path.name], 'timeout_seconds': 60,
                    'command': f'id -u > uid.txt && dpkg-query -W {package} > version.txt && {argv}',
                })
                details = api.wait_job(job['id'], timeout=80)
                attempt = details['attempt']
                stop = attempt['process_stop']
                assert stop['verified'] and stop['scope_empty'] and stop['native_state'] == 'exited'
                workspace = attempt['workspace']
                assert list(workspace['files']) == [input_path.name]
                outputs = download_outputs(api, attempt, (*files, 'uid.txt', 'version.txt'))
                assert outputs['uid.txt'].strip() == b'65534'
                source = api.call('GET', api.prefix + '/raw?path=' + input_path.name, raw=True)
                assert source.decode('utf8') == input_path.read_text('utf8')
                api.call('GET', api.prefix + '/raw?path=' + files[0], expect=404, raw=True)
                metrics = check(outputs)
                proof['jobs'][name] = {'job_id': job['id'], 'uid': 65534, 'runtime': 'wsl', 'stop': stop,
                                       'metrics': metrics, 'workspace': workspace['run_path'],
                                       'version': outputs['version.txt'].decode('ascii').split()[1],
                                       'downloads': {file: attempt['outputs']['files'][file] for file in files}}
            if openvsp is not None:
                proof['jobs']['openvsp'] = verify_openvsp(api, openvsp)
            if fenicsx is not None:
                proof['jobs']['fenicsx'] = verify_fenicsx(api, fenicsx)
        proof['passed'] = True
    finally:
        path = api.root / 'skill-native-toolchain.json'
        path.write_text(json.dumps(proof, indent=2), encoding='utf8')
        print('FF_F64_NATIVE_TOOLS', path, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--openvsp', type=Path, help='Also run the installed Windows OpenVSP distribution')
    parser.add_argument('--fenicsx', type=PurePosixPath, help='Also run FEniCSx with this installed WSL Python')
    args = parser.parse_args()
    main(args.openvsp, args.fenicsx)
