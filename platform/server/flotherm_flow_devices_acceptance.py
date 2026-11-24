"""Exercise original airflow inputs and verify pressure and flux in OpenFOAM v2406."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile

from flotherm_channel_case_acceptance import _xyz, load, wsl
from flotherm_cli_acceptance import invoke
from flotherm_ecxml_acceptance import authenticated_case_api
from flotherm_paired_acceptance import node, numbers, token


DOMAIN = [0, 0, 0, .3, .12, .12]
AREA = .12**2
ORIGIN = [.12, 0, 0]
SIZE = [.04, .12, .12]
CURVE = [[0, 20], [.008, 12], [.016, 0]]
COEFFICIENTS = {'d': [1.2e6, 0, 0], 'f': [400, 0, 0]}
FIXED = .2*AREA
FIXED_LIBRARY = 'constant/flotherm-libs/libvibeflowFlothermFixedFlow.so'


def xml(kind, name, content):
    origin, size = ([.14, 0, 0], [0, .12, .12]) if kind == 'rectangular2dFan' else (ORIGIN, SIZE)
    return ('<neutralXML><solutionDomain>'+_xyz('location', DOMAIN[:3])+_xyz('size', DOMAIN[3:])+
            '<ambientConditions><temperature>300</temperature></ambientConditions></solutionDomain><geometry>'
            f'<{kind}><name>{name}</name>'+_xyz('location', origin)+_xyz('size', size)+
            f'{content}</{kind}></geometry></neutralXML>').encode()


def fixed_fan():
    return xml('rectangular2dFan', 'Fixed fan',
        f'<plane>-yz</plane><thickness>.024</thickness><flowDefinition><fixedFlowRate>{FIXED}</fixedFlowRate></flowDefinition>')


def with_solid(raw):
    material = ('<materials><material><name>Aluminium</name><density>2700</density><specific_heat>900</specific_heat>'
                '<thermalConductivity><isotropic><conductivity>160</conductivity></isotropic></thermalConductivity>'
                '</material></materials>')
    solid = ('<solid3dBlock><name>Small block</name><material>Aluminium</material>'
             +_xyz('location', [.024,.024,.024])+_xyz('size', [.012,.012,.012])+'</solid3dBlock>')
    return raw.replace(b'<geometry>', (material+'<geometry>').encode()).replace(b'</geometry>', (solid+'</geometry>').encode())


def pdml(kind, name):
    edges = [size if i == j else 0 for i, size in enumerate(SIZE) for j in range(3)]
    return (b'#FFFB V2310 airflow device\n'+bytes.fromhex('fffb00000906000a')+
            node(kind, name, numbers(0x1003009, *SIZE), numbers(0x100300A, *edges),
                 numbers(0x100300B, *ORIGIN))+token(1, 0)+b'\n#END\n')


def configuration(velocity, *, size=.015):
    boundaries = {'mode':'open'} if velocity is None else {'mode':'channel', 'inletVelocity':velocity}
    return {'boundaries':boundaries, 'domain':DOMAIN, 'ambient_temperature_k':300,
            'openfoam':{'baseCellSize':size, 'endTime':400, 'writeInterval':200, 'gravity':[0,0,0]}}


def build(root, name, raw, extension, config, *, overrides=None):
    source, settings = root/f'{name}.{extension}', root/f'{name}.json'
    source.write_bytes(raw)
    settings.write_text(json.dumps(config), encoding='utf8')
    ir = root/(name+'-ir')
    invoke('import', source, '--config', settings, '--output', ir)
    obj, = [item for item in load(ir/'flotherm-ir.json')['objects'] if 'flow_device' in item]
    assert 'material' not in obj and obj['power_w'] == 0
    case = root/(name+'-case')
    if overrides:
        raw_settings = root/(name+'-overrides.json')
        raw_settings.write_text(json.dumps({key:{obj['id']:value} for key,value in overrides.items()}), encoding='utf8')
        result = invoke('build', ir, '--config', raw_settings, '--output', case)
    else:
        result = invoke('build', ir, '--output', case)
    return case, result, obj


def last_table(case, suffix, leaf):
    paths = [path for path in (case/'postProcessing'/'fluid'/suffix).glob(f'*/{leaf}')]
    assert len(paths) == 1, paths
    return paths[0].read_text(encoding='utf8').splitlines()[-1].split()


def solve(case, *, parallel=False):
    assert invoke('check', case)['ok']
    script = 'Allrun.parallel' if parallel else 'Allrun.serial'
    wsl(case, f'ulimit -c 0\nbash {script} > log.run 2>&1')
    assert invoke('check', case)['ok']
    gate = load(case/'mesh_gate.json')
    device, = load(case/'case-manifest.json')['flow_devices']
    assert gate['ok'] and gate['flow_zone_counts'][device['cell_zone']] == device['expected_cells']
    log = (case/'log.run').read_text(encoding='utf8')
    if parallel:
        assert re.search(r'nProcs\s*:\s*2\b', log) and 'Reconstructing fields' in log
    model = 'explicitPorositySource' if device['model']=='DarcyForchheimer' else device['model']
    assert f'Selecting finite volume options type {model}' in log and 'End' in log
    assert 'FOAM FATAL' not in log and 'Could not load' not in log
    pressure_dict = case/'system'/'verifyFlowDict'
    pressure_dict.write_text('''FoamFile { version 2.0; format ascii; class dictionary; object verifyFlowDict; }
functions
{
    verifyPressure
    {
        type probes;
        libs (sampling);
        region fluid;
        fields (p);
        probeLocations ((.087 .057 .057) (.222 .057 .057));
    }
}
''', encoding='utf8')
    try:
        wsl(case, "postProcess -region fluid -fields '(p)' -latestTime -dict system/verifyFlowDict > log.verifyPressure 2>&1")
    finally:
        pressure_dict.unlink()
    probe_paths = list((case/'postProcessing').rglob('verifyPressure/*/p'))
    if not probe_paths:
        probe_paths = list((case/'postProcessing').rglob('verifyPressure/fluid/*/p'))
    assert len(probe_paths) == 1, probe_paths
    time, upstream, downstream = probe_paths[0].read_text(encoding='utf8').splitlines()[-1].split()
    assert int(time) == configuration(.6)['openfoam']['endTime']
    pressure = [float(upstream), float(downstream)]
    result = {'time':time, 'pressure_pa':pressure, 'device':device, 'mesh_gate':gate}
    if 'face_zone' in device:
        assert gate['flow_zone_counts'][device['face_zone']] == device['expected_faces']
        moment, mass_flow = last_table(case, device['cell_zone']+'Flux', 'surfaceFieldValue.dat')
        assert moment == time
        rho = pressure[0 if device['direction'][0]>0 else 1]*28.966/(8314.46261815324*300)
        state = case/('processor0' if parallel else '')/time/'fluid'/'uniform'/f'{device["source"]}Properties'
        text = state.read_text(encoding='utf8')
        gradient = float(re.search(r'(?m)^gradient\s+([^;]+);', text)[1])
        result.update(signed_mass_flow_kg_s=float(mass_flow), independent_flow_m3_s=float(mass_flow)/rho,
                      native_pressure_rise_pa=gradient*device['thickness_m'])
        result['native_flow_m3_s'] = float(re.search(r'(?m)^flow_rate\s+([^;]+);', text)[1])
        if model == 'fixedVolumetricFan':
            library = (case/FIXED_LIBRARY).read_bytes()
            assert library.startswith(b'\x7fELF')
            result['library_sha256'] = hashlib.sha256(library).hexdigest()
    return result


def verify(root):
    points = ''.join(f'<fanCurvePoint><volumeFlow>{q}</volumeFlow><pressure>{p}</pressure></fanCurvePoint>'
                     for q,p in CURVE[::-1])
    pq = xml('axial3dFan', 'PQ fan',
             f'<plane>+yz</plane><hubSize>.02</hubSize><flowDefinition><fanCurve>{points}</fanCurve></flowDefinition>')
    fans = []
    for speed in (.35, .75):
        raw = with_solid(pq) if speed == .35 else pq
        case, built, obj = build(root, f'pq{speed}', raw, 'ecxml', configuration(speed))
        assert built['runnable_preflight'] and obj['flow_device']['pq_curve'] == CURVE
        result = solve(case)
        q, pressure = result['native_flow_m3_s'], result['native_pressure_rise_pa']
        left,right = next((left,right) for left,right in zip(CURVE,CURVE[1:]) if left[0] <= q <= right[0])
        expected = left[1]+(q-left[0])/(right[0]-left[0])*(right[1]-left[1])
        assert math.isclose(q, AREA*speed, rel_tol=.01) and math.isclose(q,result['independent_flow_m3_s'],rel_tol=.01), result
        assert math.isclose(pressure,expected,abs_tol=.001), result
        assert math.isclose(result['pressure_pa'][1]-result['pressure_pa'][0],expected,abs_tol=.35), result
        fans.append(result)
    assert fans[0]['mesh_gate']['regions'] == ['fluid','solid1']
    assert fans[0]['native_pressure_rise_pa'] > fans[1]['native_pressure_rise_pa'] > 0
    raw = root/'pq0.75.pdml'
    raw.write_bytes(pdml(0x2032000, 'PQ fan'))
    invoke('import', root/'pq0.75.ecxml', '--pdml', raw, '--config', root/'pq0.75.json',
           '--output', root/'paired-ir')
    paired = load(root/'paired-ir'/'flotherm-ir.json')
    assert paired['objects'][0]['flow_device']['pq_curve'] == CURVE and len(paired['source_associations']) == 1

    raw = pdml(0x202A000, 'Resistance bed')
    _, blocked, obj = build(root, 'unresolved', raw, 'pdml', configuration(.6))
    assert obj['mapping'] == 'missing-info' and 'DarcyForchheimer' in blocked['blockers'][0]['reason']
    case, built, _ = build(root, 'resistance', raw, 'pdml', configuration(.6, size=.006),
                          overrides={'resistances':{'coefficients':COEFFICIENTS}})
    assert built['runnable_preflight']
    resistance = solve(case)
    upstream,downstream = resistance['pressure_pa']
    density = (upstream+downstream)/2*28.966/(8314.46261815324*300)
    expected = (1.846e-5*COEFFICIENTS['d'][0]+.5*density*COEFFICIENTS['f'][0]*.6)*.6*SIZE[0]
    assert math.isclose(upstream-downstream,expected,abs_tol=.35), resistance
    resistance['reference_pressure_drop_pa'] = expected

    case, built, _ = build(root, 'fixed', fixed_fan(), 'ecxml', configuration(None))
    assert built['runnable_preflight']
    fixed_result = solve(case)
    assert fixed_result['device']['direction'] == [-1,0,0]
    assert math.isclose(fixed_result['independent_flow_m3_s'], FIXED, rel_tol=.02), fixed_result
    assert math.isclose(fixed_result['native_flow_m3_s'], FIXED, rel_tol=.02), fixed_result
    assert fixed_result['native_pressure_rise_pa'] > 0, fixed_result
    case, built, _ = build(root, 'fixed-parallel', fixed_fan(), 'ecxml', {**configuration(None), 'scotch':False})
    assert built['runnable_preflight']
    parallel = solve(case, parallel=True)
    assert math.isclose(parallel['independent_flow_m3_s'], FIXED, rel_tol=.02), parallel
    assert math.isclose(parallel['native_flow_m3_s'], FIXED, rel_tol=.02), parallel
    return {'openfoam':'v2406', 'uid':65534, 'actual_model_calls':0, 'simulated_model_calls':0,
            'pq_operating_points':fans, 'paired_geometry_verified':True,
            'pdml_resistance':resistance, 'fixed_flow':fixed_result, 'fixed_flow_parallel':parallel}


def verify_s3():
    raw = with_solid(fixed_fan())
    with authenticated_case_api('flotherm-flow-devices-s3') as api:
        case = api.create_case('FloTHERM fixed volumetric fan')
        path = 'Ref/fan.ecxml'
        api.put(path, raw.decode())
        imported = api.call('POST', api.prefix+'/flotherm:import',
                            {'path':path, 'expected_sha256':hashlib.sha256(raw).hexdigest()})
        api.put('Ref/flow-ir.json', imported['content'])
        api.put('Ref/flow-config.json', json.dumps({'boundaries':{'mode':'open'}, 'openfoam':{'gravity':[0,0,0]}}))
        output = 'C-run/flow'
        job = api.call('POST', api.prefix+'/flotherm:convert', {'ir_path':'Ref/flow-ir.json',
            'settings_path':'Ref/flow-config.json', 'output_dir':output})['job_id']
        result = api.wait_job(job, timeout=100)
        assert result['status']=='succeeded' and not result['completion_pending'], result
        def read(name):
            return api.call('GET', api.prefix+f'/file?path={output}/{name}')['content']
        device, = json.loads(read('case-manifest.json'))['flow_devices']
        assert device['model']=='fixedVolumetricFan' and device['fixed_flow_m3_s']==FIXED
        assert json.loads(read('preflight.json'))['ok']
        files = json.loads(read('.flotherm2foam-generated'))['files']
        assert {'Allrun.build', 'system/fluid/topoSetDict', 'constant/fluid/fvOptions',
                'constant/flotherm-fixed-flow-src/fixedVolumetricFan.C'} <= set(files)
        assert 'type fixedVolumetricFan;' in read('constant/fluid/fvOptions')
        return {'case':case['id'], 'job':job, 'root':str(api.root), 'device':device}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--s3', action='store_true')
    args = parser.parse_args()
    if args.work:
        args.work.mkdir()
        data = verify(args.work.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='flotherm-flow-') as folder:
            data = verify(Path(folder).resolve())
    if args.s3:
        data['authenticated_s3'] = verify_s3()
    args.report.write_text(json.dumps(data, indent=2)+'\n', encoding='utf8')
    print(json.dumps({'report':str(args.report), 'openfoam': data['openfoam']}))
