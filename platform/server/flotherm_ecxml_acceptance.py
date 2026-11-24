"""Native neutral ECXML import, real OpenFOAM solve and authenticated store checks."""

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from flotherm_cli_acceptance import invoke


NEUTRAL_ECXML = '''<?xml version="1.0" encoding="UTF-8"?>
<neutralXML xmlns="urn:ecxml:sample">
  <name>Homogeneous solid heat benchmark</name>
  <solutionDomain>
    <location><x>0</x><y>0</y><z>0</z></location>
    <size><x>0.2</x><y>0.2</y><z>0.2</z></size>
    <ambientConditions><temperature>310</temperature></ambientConditions>
  </solutionDomain>
  <materials><material><name>Homogeneous solid</name><density>2000</density><specific_heat>500</specific_heat>
    <thermalConductivity><isotropic><conductivity>160</conductivity></isotropic></thermalConductivity>
  </material></materials>
  <geometry>
    <assembly><name>Test fixture</name>
      <solid3dBlock><name>Heated solid</name><material>Homogeneous solid</material>
        <location><x>0</x><y>0</y><z>0</z></location><size><x>0.2</x><y>0.2</y><z>0.2</z></size>
        <powerDissipation>1200</powerDissipation>
      </solid3dBlock>
      <solidCylinder><name>Inactive cylinder</name><active>false</active></solidCylinder>
    </assembly>
  </geometry>
</neutralXML>
'''


def assert_case(root):
    manifest = json.loads((root/'case-manifest.json').read_text(encoding='utf8'))
    preflight = json.loads((root/'preflight.json').read_text(encoding='utf8'))
    assert preflight['ok'], preflight
    assert len(manifest['heat_sources']) == 1
    source = manifest['heat_sources'][0]
    assert source['zone'] == 'ecxmlSolid1Cells' and source['power_w'] == 1200 and source['su'] == .0012
    assert 'uniform 310.0' in (root/'0/T').read_text(encoding='utf8')
    raw = (root/'constant/transportProperties').read_text(encoding='utf8')
    assert float(raw.split('] ')[-1].split(';')[0]) == .00016


def reference_temperatures(probe_cell=None):
    extent, power, conductivity, ambient, count = .2, 1200, 160, 310, 10
    dx = extent/count
    forcing = power/extent**3/conductivity*dx**2
    offsets = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
    theta = {(i, j, k): 0.0 for i in range(count) for j in range(count) for k in range(count)}
    neighbors = {point: [neighbor for x, y, z in offsets
                         if (neighbor := (point[0]+x, point[1]+y, point[2]+z)) in theta] for point in theta}
    for _ in range(3000):
        change = 0.0
        for point, adjacent in neighbors.items():
            # A fixed wall temperature is half a cell from the adjacent center.
            updated = (forcing + sum(theta[item] for item in adjacent))/(12-len(adjacent))
            change = max(change, abs(updated-theta[point]))
            theta[point] = updated
        if change < 1e-11:
            if probe_cell is not None:
                return ambient+theta[probe_cell]
            return sorted(ambient+value for value in theta.values())
    raise AssertionError('Reference finite difference did not converge')


def solve(output, zone='ecxmlSolid1Cells', *, binary=False):
    assert output.drive.upper() == 'D:'
    wsl_path = '/mnt/d'+output.as_posix()[2:]
    command = '''source /usr/lib/openfoam/openfoam2406/etc/bashrc || exit
set -e
cd -- "$1"
id -u
blockMesh > log.blockMesh 2>&1
topoSet > log.topoSet 2>&1
checkMesh > log.checkMesh 2>&1
laplacianFoam > log.laplacianFoam 2>&1
'''
    result = subprocess.run(['wsl.exe', '--distribution', 'vibeflow-linux', '--user', 'nobody',
        '--exec', 'bash', '-c', command, 'flotherm-acceptance', wsl_path], capture_output=True,
        encoding='utf8', errors='replace', text=True, timeout=95)
    logs = {path.name:path.read_text(encoding='utf8', errors='replace') for path in output.glob('log.*')}
    assert result.returncode == 0, (result.stdout, result.stderr, {p:s[-2500:] for p,s in logs.items()})
    assert result.stdout.split()[0] == '65534'
    assert 'Mesh OK' in logs['log.checkMesh'] and 'End' in logs['log.laplacianFoam']
    assert re.search(rf'{re.escape(zone)}.*size 1000', logs['log.topoSet'])
    final = (output/'100/T').read_bytes()
    if binary:
        import struct

        assert b'arch        "LSB;label=32;scalar=64";' in final[:1024]
        start = re.search(rb'internalField\s+nonuniform List<scalar>\s*1000\s*\(', final[:1200]).end()
        values = struct.unpack_from('<1000d', final, start)
        assert final[start+8000:start+8002] == b');'
    else:
        body = re.search(rb'internalField\s+nonuniform List<scalar>\s*1000\s*\(([^)]+)\)', final)
        values = [float(value) for value in body[1].split()]
    reference = reference_temperatures()
    assert len(values) == len(reference) == 1000 and min(values) > 310
    error = max(abs(a-b) for a, b in zip(sorted(values), reference))
    assert error < 0.001, (min(values), max(values), reference[0], reference[-1], error)
    return {'uid': 65534, 'cells': len(values), 'minimum_k': min(values), 'maximum_k': max(values),
            'reference_max_error_k': error, 'time': 100}


def verify_cli(root, run_solver):
    source = root/'thermal.ecxml'
    source.write_bytes(NEUTRAL_ECXML.encode())
    ir = root/'model-ir'
    output = root/'built-case'
    imported = invoke('import', source, '--output', ir)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert imported['source']['sha256'] == digest and imported['source']['inactive_entries'] == 1
    assert invoke('validate', ir)['coverage']['power_w'] == 1200
    for selected, nested in ((ir, ir/'case'), (ir/'flotherm-ir.json', ir/'nested-case'), (ir, ir)):
        original = (ir/'flotherm-ir.json').read_bytes()
        error = invoke('build', selected, '--output', nested, '--force', status=2, returns_json=False)
        assert error.stderr and (ir/'flotherm-ir.json').read_bytes() == original
        assert nested == ir or not nested.exists()
    assert invoke('build', ir, '--output', output)['runnable_preflight']
    assert invoke('check', output)['ok']
    assert_case(output)
    model = json.loads((ir/'flotherm-ir.json').read_text(encoding='utf8'))
    assert model['assembly']['groups'] == [{'path': ['Test fixture']}]
    assert model['objects'][0]['path'] == 'Test fixture/Heated solid'
    report = {'sha256': digest, 'commands': ['import', 'validate', 'build', 'check'], 'power_w': 1200}
    if run_solver:
        report['openfoam'] = solve(output)
    for name, old, new, phrase in (
        ('bad_material', '<density>2000', '<density>0', '导热率、密度和比热'),
        ('unsupported_object', '<active>false</active>', '<active>true</active>', 'solidCylinder'),
        ('missing_domain', '<solutionDomain>', '<unreadDomain>', 'solutionDomain'),
    ):
        invalid = root/(name+'.ecxml')
        raw = NEUTRAL_ECXML.replace(old, new)
        if name == 'missing_domain':
            raw = raw.replace('</solutionDomain>', '</unreadDomain>')
        invalid.write_text(raw, encoding='utf8')
        rejected = root/name
        failure = invoke('import', invalid, '--output', rejected, status=2, returns_json=False)
        assert phrase in failure.stderr and not rejected.exists(), failure.stderr
    roomy = root/'roomy.ecxml'
    roomy.write_text(NEUTRAL_ECXML.replace('<size><x>0.2', '<size><x>0.3', 1), encoding='utf8')
    invoke('import', roomy, '--output', root/'roomy-ir')
    candidate = root/'roomy-case'
    nonuniform = invoke('build', root/'roomy-ir', '--output', candidate)
    assert not nonuniform['runnable_preflight'] and invoke('check', candidate, status=2)['blockers']
    report['rejected'] = ['bad_material', 'unsupported_object', 'missing_domain', 'nonuniform_domain_static_gate',
                          'same_or_nested_case_and_ir_outputs']
    return report


@contextmanager
def authenticated_case_api(label):
    from s3testsupport import ObjectStore
    from testsupport import IsolatedServer, SERVER

    executable = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    with ObjectStore(executable=executable, volume_max=9, authenticated=True) as store:
        api = IsolatedServer(label, app='flotherm_raw_s3_test:app')
        config = api.root/'flotherm_raw_s3_test.py'
        config.write_text('import os\nos.environ.update('+repr(store.settings())+')\nfrom app.main import app\n', encoding='utf8')
        try:
            with api:
                yield api
        finally:
            config.unlink(missing_ok=True)


def verify_s3():
    with authenticated_case_api('flotherm-raw-s3') as api:
        case = api.create_case('FloTHERM ECXML S3')
        path = 'Ref/thermal.ecxml'
        api.put(path, NEUTRAL_ECXML)
        inspection = api.call('POST', api.prefix+'/flotherm:inspect', {'paths': [path]})
        expected = inspection['files'][0]['sha256']
        api.put(path, NEUTRAL_ECXML+'\n')
        assert api.call('POST', api.prefix+'/flotherm:import', {'path': path, 'expected_sha256': expected},
                        expect=409)['detail']['code'] == 'input_stale'
        result = api.call('POST', api.prefix+'/flotherm:import', {'path': path})
        assert result['source']['sha256'] != expected and result['coverage']['power_w'] == 1200
        ir = 'Ref/flotherm-ir.json'
        write = {'path': ir, 'content': result['content'], 'expected_sha256': None}
        api.call('PUT', api.prefix+'/file', write)
        api.call('PUT', api.prefix+'/file', write, expect=409)
        raw = {'ir_path': ir, 'output_dir': 'C-run/imported'}
        job = api.call('POST', api.prefix+'/flotherm:convert', raw)['job_id']
        details = api.wait_job(job, timeout=100)
        assert details['status'] == 'succeeded' and not details['completion_pending']
        case_path = api.prefix+'/file?path=C-run/imported/power-balance.json'
        balance = json.loads(api.call('GET', case_path)['content'])
        assert balance['input_power_w'] == 1200 and balance['balanced']
        return {'job': job, 'case': case['id'], 'power_w': balance['input_power_w'], 'evidence': str(api.root),
                'stale_input_rejected': True, 'duplicate_target_rejected': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--solver', action='store_true')
    parser.add_argument('--s3', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='flotherm-neutral-') as temp:
        report = {'cli': verify_cli(Path(temp), args.solver), 'model_requests': 0}
    if args.s3:
        report['authenticated_s3'] = verify_s3()
    output = json.dumps(report, ensure_ascii=False, indent=2)
    args.report.write_text(output+'\n', encoding='utf8')
    print(output)
