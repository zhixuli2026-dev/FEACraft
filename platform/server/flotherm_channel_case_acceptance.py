"""Convert raw FloTHERM inputs and verify coupled heat transfer in OpenFOAM."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile

from flotherm_cli_acceptance import invoke
from flotherm_ecxml_acceptance import authenticated_case_api
from flotherm_paired_acceptance import node, numbers, pdml_fixture


CONFIG = {'boundaries':{'mode':'channel','inletVelocity':.15},
          'openfoam':{'baseCellSize':.02,'endTime':2000,'writeInterval':500}, 'writeFormat':'binary'}


def _xyz(tag, point):
    return f'<{tag}>'+''.join(f'<{axis}>{value}</{axis}>' for axis,value in zip('xyz',point))+f'</{tag}>'


def source_xml(air_probe=(.217,.054,.062), second_x=.15):
    contents = []
    materials = []
    for name, density, cp, k, low, size, watts in (
            ('Metal',2700,900,160,(.06,.03,.04),(.03,.06,.04),.3),
            ('Polymer',1100,1200,1.5,(second_x,.04,.03),(.04,.04,.06),.2)):
        materials.append(f'<material><name>{name}</name><density>{density}</density><specific_heat>{cp}</specific_heat>'
                         f'<thermalConductivity><isotropic><conductivity>{k}</conductivity></isotropic>'
                         '</thermalConductivity></material>')
        contents.append(f'<solid3dBlock><name>{name} block</name><material>{name}</material>'
                        f'{_xyz("location",low)}{_xyz("size",size)}'
                        f'<powerDissipation>{watts}</powerDissipation></solid3dBlock>')
    for name, location in [('Solid sample',(.076,.056,.062)),('Air sample',air_probe)]:
        contents.append(f'<monitorPoint><name>{name}</name>{_xyz("location",location)}</monitorPoint>')
    return ('<neutralXML xmlns="urn:ecxml:sample"><name>Cooling channel</name><solutionDomain>'
            f'{_xyz("location",(0,0,0))}{_xyz("size",(.3,.12,.12))}'
            '<ambientConditions><temperature>300</temperature></ambientConditions></solutionDomain>'
            f'<materials>{"".join(materials)}</materials><geometry><assembly><name>Device</name>'
            f'{"".join(contents)}</assembly></geometry></neutralXML>')


def load(path):
    return json.loads(path.read_text(encoding='utf8'))


def wsl(case, command, *, expected=0, timeout=180):
    prefix = '''source /usr/lib/openfoam/openfoam2406/etc/bashrc || exit
set -e
cd -- "$1"
id -u
'''
    args = ['wsl.exe','--distribution','vibeflow-linux','--user','nobody','--exec','bash','-c',
            prefix+command,'channel-case','/mnt/d'+case.as_posix()[2:]]
    result = subprocess.run(args,capture_output=True,encoding='utf8',errors='replace',timeout=timeout)
    logs = {path.name:path.read_text(encoding='utf8',errors='replace')[-2400:] for path in case.glob('log.*')}
    assert result.returncode == expected and result.stdout.split()[0] == '65534', (result,logs)
    return result


def solve(case, power, probes, *, parallel=False):
    assert invoke('check',case)['ok']
    solution = 'Allrun.parallel' if parallel else 'Allrun.serial'
    wsl(case,f'bash {solution} > log.run 2>&1')
    assert invoke('check',case)['ok']
    manifest = load(case/'case-manifest.json')
    mesh = load(case/'mesh_gate.json')
    assert mesh['ok'] and mesh['regions'] == ['fluid',*manifest['solid_regions']]
    assert (case/'log.checkMesh').read_text(encoding='utf8').count('Mesh OK.') == len(mesh['regions'])
    log = (case/'log.run').read_text(encoding='utf8')
    assert 'Selecting turbulence model type laminar' in log and 'End' in log
    assert log.count('Selecting finite volume options type scalarSemiImplicitSource') >= len(manifest['heat_sources'])
    report = invoke('flow',case,'--energy-balance')
    energy = report['energy_balance']
    assert math.isclose(energy['source_power_w'],power,abs_tol=1e-12)
    assert abs(energy['source_to_wall_residual_w']) < power*.01, energy
    assert abs(energy['wall_to_ports_residual_w']) < power*.02, energy
    region_temperatures = {}
    for region in mesh['regions']:
        path = case/f'postProcessing/{region}/temperature_{region}/0/fieldMinMax.dat'
        lines = path.read_text(encoding='utf8').splitlines()
        moment, low, high = lines[-1].split()
        assert moment == report['time'] and 'min(T)' in lines[1] and 299 < float(low) <= float(high)
        region_temperatures[region] = {'minimum_k':float(low),'maximum_k':float(high)}
    observations = {}
    for sensor in manifest['monitor_results']:
        region = sensor['region']
        name = sensor['object_name']
        folders = [case/'postProcessing'/name/region,case/'postProcessing'/region/name]
        paths = [path for folder in folders for path in folder.glob('*/T')]
        assert len(paths) == 1, paths
        final, value = paths[0].read_text(encoding='utf8').splitlines()[-1].split()
        temperature = float(value)
        assert final == report['time'] and temperature > 299, (sensor,temperature)
        observations[sensor['name']] = {'region':region,'temperature_k':temperature,'sources':sensor['sources']}
    assert {key:row['region'] for key,row in observations.items()} == probes
    return {'uid':65534,'openfoam':'v2406','mesh':mesh,'mesh_cells':manifest['mesh_cells'],
            'source':manifest['source'],'flow':report,'probes':observations,'region_temperatures':region_temperatures}


def verify(root):
    config = root/'channel.json'
    config.write_text(json.dumps(CONFIG),encoding='utf8')
    xml = root/'source.ecxml'
    xml.write_text(source_xml(),encoding='utf8')
    imported, case = root/'ecxml-ir', root/'ecxml-case'
    assert invoke('import',xml,'--config',config,'--output',imported)['coverage']['objects'] == 2
    assert invoke('build',imported,'--output',case)['runnable_preflight']
    result = solve(case,.5,{'Solid sample':'solid0','Air sample':'fluid'})
    assert result['mesh_cells'] == {'fluid':992,'solid0':16,'solid1':16}
    expected_inlet = 101325 / (8314.46261815324/28.966*300) * .15*.12*.12
    assert math.isclose(expected_inlet,result['flow']['inlet']['inward'],rel_tol=2e-5)
    wall = {row['name']:row for row in result['flow']['wall_heat_flux']['patches']}
    assert math.isclose(wall['fluid_to_solid0']['area_m2'],.0108,abs_tol=1e-10)
    assert math.isclose(wall['fluid_to_solid1']['area_m2'],.0128,abs_tol=1e-10)
    assert 300 < result['probes']['Air sample']['temperature_k'] < result['probes']['Solid sample']['temperature_k']
    temperatures = case/'postProcessing/fluid/outletTemperature/0/surfaceFieldValue.dat'
    data = temperatures.read_bytes()
    try:
        temperatures.write_bytes(data.rsplit(b'\n',2)[0]+b'\n')
        assert 'outletTemperature' in invoke('flow',case,'--energy-balance',status=2,returns_json=False).stderr
    finally:
        temperatures.write_bytes(data)

    pdml, pdml_config = root/'source.pdml', root/'pdml.json'
    probe = node(0x2027000,'Inlet air',numbers(0x100300B,.063,.253,.093),
                 numbers(0x1003009,.001,.001,.001),
                 numbers(0x100300A,.001,0,0,0,.001,0,0,0,.001))
    pdml.write_bytes(pdml_fixture(power=.2,observations=(probe,)))
    pdml_config.write_text(json.dumps({**CONFIG,'domain':[-.08,-.06,-.06,.32,.26,.26],'ambient_temperature_k':300,
        'openfoam':{'baseCellSize':.04,'endTime':5000}, 'writeFormat':'ascii'}),encoding='utf8')
    pdml_ir, pdml_case = root/'pdml-ir',root/'pdml-case'
    invoke('import',pdml,'--config',pdml_config,'--output',pdml_ir)
    assert invoke('build',pdml_ir,'--output',pdml_case)['runnable_preflight']
    binary_source = solve(pdml_case,.2,{'Inlet air':'fluid'},parallel=True)
    assert binary_source['source']['sha256'] == hashlib.sha256(pdml.read_bytes()).hexdigest()

    failures = {}
    for name, content, reason in [('interface_probe',source_xml((.06,.054,.062)),'探针'),
                                  ('overlap',source_xml(second_x=.08),'重叠')]:
        source = root/(name+'.ecxml')
        source.write_text(content,encoding='utf8')
        model, target = root/(name+'-ir'),root/(name+'-case')
        invoke('import',source,'--config',config,'--output',model)
        failure = invoke('build',model,'--output',target)
        assert not failure['runnable_preflight'] and any(reason in item['reason'] for item in failure['blockers'])
        assert 'unresolved blockers' in wsl(target,'bash Allrun.pre',expected=1).stderr
        failures[name] = failure['blockers']
    return {'ecxml':result,'pdml_parallel':binary_source,'failures':failures,'stale_native_temperature_rejected':True}


def verify_s3():
    with authenticated_case_api('flotherm-channel-case-s3') as api:
        case = api.create_case('FloTHERM generated CHT channel')
        source = 'Ref/channel.ecxml'
        raw = source_xml()
        api.put(source,raw)
        imported = api.call('POST',api.prefix+'/flotherm:import',{
            'path':source,'expected_sha256':hashlib.sha256(raw.encode()).hexdigest()})
        api.put('Ref/channel-ir.json',imported['content'])
        api.put('Ref/channel-settings.json',json.dumps(CONFIG))
        output = 'C-run/channel'
        job = api.call('POST',api.prefix+'/flotherm:convert',{'ir_path':'Ref/channel-ir.json',
            'settings_path':'Ref/channel-settings.json','output_dir':output})['job_id']
        state = api.wait_job(job,timeout=100)
        assert state['status'] == 'succeeded' and not state['completion_pending'], state
        def read(name):
            return json.loads(api.call('GET',api.prefix+f'/file?path={output}/{name}')['content'])
        manifest = read('case-manifest.json')
        assert manifest['application'] == 'chtMultiRegionSimpleFoam' and len(manifest['solid_regions']) == 2
        assert read('preflight.json')['ok'] and read('power-balance.json')['generated_power_w'] == .5
        assert 'constant/fluid/thermophysicalProperties' in read('.flotherm2foam-generated')['files']
        return {'job':job,'case':case['id'],'root':str(api.root),'solid_regions':list(manifest['solid_regions'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--s3',action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='flotherm-generated-channel-',dir=args.report.resolve().parent))
    report = {'passed':False,'root':str(root),'actual_model_calls':0,'simulated_model_calls':0}
    try:
        report.update(verify(root))
        if args.s3:
            report['authenticated_s3'] = verify_s3()
        report['passed'] = True
    finally:
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('FLOTHERM_CHANNEL_CASE_PASS',args.report)


if __name__ == '__main__':
    main()
