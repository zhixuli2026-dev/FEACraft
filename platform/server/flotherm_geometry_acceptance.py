"""Check primitive reconstruction and solve native FloTHERM surface meshes."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import tempfile

from flotherm_channel_case_acceptance import load, source_xml, wsl
from flotherm_cli_acceptance import invoke
from flotherm_ecxml_acceptance import authenticated_case_api
from flotherm_paired_acceptance import method, node, numbers, token


ECXML = {'boundaries':{'mode':'channel','inletVelocity':.15},
         'openfoam':{'baseCellSize':.02,'endTime':1400},'writeFormat':'binary'}
PDML = {'domain':[0,0,0,.34,.26,.18], 'ambient_temperature_k':300,
        'boundaries':{'mode':'channel','inletVelocity':.23},
        'openfoam':{'baseCellSize':.029,'endTime':1600},'writeFormat':'binary'}
PRIMITIVES = (
    (0x2028000,'prism',(.06,.05,.04),(.053,.045,.045),.05),
    (0x2073100,'tet',(.19,.045,.064),(.05,.055,.05),.06),
    (0x2073200,'invertedTet',(.16,.14,.039),(.05,.045,.043),.07),
    (0x2025000,'oriented-box',(.06,.143,.098),(.05,.04,.044),.08),
    (0x2030000,'cylinder',(.27,.15,.05),(.046,.045,.046),.09),
)


def raw_cylinder():
    raw = source_xml().replace('<solid3dBlock><name>Metal block</name>',
                              '<solidCylinder><name>Metal block</name><plane>+xy</plane>',1)
    raw = raw.replace('</solid3dBlock>','</solidCylinder>',1).replace('<conductivity>160','<conductivity>3',1)
    probe = ('<monitorPoint><name>Air inside cylinder envelope</name><location>'
             '<x>.061</x><y>.032</y><z>.061</z></location></monitorPoint>')
    return raw.replace('</assembly>',probe+'</assembly>',1)


def raw_pdml(rotate=math.pi/6):
    basis = ((math.cos(rotate),math.sin(rotate),0),(-math.sin(rotate),math.cos(rotate),0),(0,0,1))
    origin = (.12,.02,.025)
    material = node(0x201B000,'Native geometry model material',method(0x1001002,(8,'material')),
                    numbers(0x201B004,3),numbers(0x201B001,1800),numbers(0x201B002,800))
    parts, sources = [], []
    for code,name,world,edges,power in PRIMITIVES:
        arm = [a-b for a,b in zip(world,origin)]
        local = [sum(a*b for a,b in zip(axis,arm)) for axis in basis]
        extra = (method(0x2030001,(12,0x3029002)),numbers(0x2030004,.023),numbers(0x2030005,.045)) if name == 'cylinder' else ()
        axes = [value if i==j else 0 for i,value in enumerate(edges) for j in range(3)]
        parts.append(node(code,name,numbers(0x100300A,*axes),numbers(0x1003009,*edges),numbers(0x100300B,*local),
                          method(0x1001005,(8,'material')),method(0x1001008,(8,'heat-'+name)),*extra))
        sources.append(node(0x2022000,'Total heat '+name,method(0x1001002,(8,'heat-'+name)),
                            method(0x2022004,(6,power),(12,0x3044001))))
    assembly = node(0x202E000,'Rotated assembly',numbers(0x1003009,1,1,1),numbers(0x100300B,*origin),
                    numbers(0x100300A,*(value for col in basis for value in col)),*parts)
    return b'#FFFB V2310 native-geometry-records\n'+bytes.fromhex('fffb00000906000a')+material+b''.join(sources)+assembly+token(1,0)+b'\n#END\n'


def verify_pairing(root):
    xml = root/'pair.ecxml'
    raw = ('<neutralXML><solutionDomain><location><x>0</x><y>0</y><z>0</z></location>'
           '<size><x>.2</x><y>.2</y><z>.2</z></size><ambientConditions><temperature>300</temperature></ambientConditions>'
           '</solutionDomain><materials><material><name>Rod metal</name><density>2000</density>'
           '<specific_heat>900</specific_heat><thermalConductivity><isotropic><conductivity>3</conductivity>'
           '</isotropic></thermalConductivity></material></materials><geometry><solidCylinder><name>Rod</name>'
           '<location><x>.04</x><y>.04</y><z>.04</z></location><size><x>.04</x><y>.04</y><z>.04</z></size>'
           '<plane>xy</plane><material>Rod metal</material></solidCylinder></geometry></neutralXML>')
    xml.write_text(raw,encoding='utf8')
    for axis, position, label in ((3,(.06,.06,.04),'matched'),(1,(.04,.06,.06),'wrong-axis')):
        record = node(0x2030000,'Rod',numbers(0x100300A,.04,0,0,0,.04,0,0,0,.04),
                      numbers(0x1003009,.04,.04,.04),numbers(0x100300B,*position),
                      method(0x2030001,(12,0x3029000+axis)),numbers(0x2030004,.02),numbers(0x2030005,.04))
        part = root/label
        part.mkdir()
        binary = part/'pair.pdml'
        binary.write_bytes(b'#FFFB V2310 paired-cylinder-fixture\n'+bytes.fromhex('fffb00000906000a')+record+token(1,0)+b'\n#END\n')
        out = root/(label+'-ir')
        if axis == 3:
            invoke('import',xml,'--pdml',binary,'--output',out)
            association, = load(out/'flotherm-ir.json')['source_associations']
            assert association['verified_by'] == ['name','bounds_m','axis','cross_section']
        else:
            rejected = invoke('import',xml,'--pdml',binary,'--output',out,status=2,returns_json=False)
            assert '配对' in rejected.stderr and not out.exists()


def build(root, name, raw, config, extension):
    source, settings = root/f'{name}.{extension}',root/f'{name}.json'
    source.write_bytes(raw)
    settings.write_text(json.dumps(config),encoding='utf8')
    ir, case = root/(name+'-ir'),root/(name+'-case')
    source_entry = invoke('import',source,'--config',settings,'--output',ir)['source']
    assert source_entry['sha256'] == hashlib.sha256(raw).hexdigest()
    assert invoke('build',ir,'--output',case)['runnable_preflight'] and invoke('check',case)['ok']
    return ir,case


def solve(case, expected_volumes, expected_power):
    wsl(case,'bash Allrun.serial > log.run 2>&1')
    assert 'Finished meshing without any errors' in (case/'log.snappyHexMesh').read_text(encoding='utf8')
    manifest = load(case/'case-manifest.json')
    gate = load(case/'mesh_gate.json')
    assert gate['ok'] and gate['regions'] == ['fluid',*manifest['solid_regions']]
    assert invoke('check',case)['ok']
    result = invoke('flow',case,'--energy-balance')
    assert math.isclose(result['energy_balance']['source_power_w'],expected_power)
    geometry = {}
    for name,row in manifest['mesh_geometry'].items():
        expected = expected_volumes[row['kind']]
        measured = gate['region_stats'][name]['volume_m3']
        assert math.isclose(row['volume_m3'],expected,rel_tol=1e-10)
        assert math.isclose(measured,expected,rel_tol=.08), (name,row,expected,measured)
        mesh = case/row['surface']
        wsl(case,f'surfaceCheck {row["surface"]} > log.surfaceCheck.{name} 2>&1')
        checked = (case/f'log.surfaceCheck.{name}').read_text(encoding='utf8')
        assert 'Surface is closed' in checked and 'Number of unconnected parts : 1' in checked, checked[-1500:]
        geometry[row['kind']] = {**row, 'native_cells':gate['region_stats'][name]['cells'],
                                 'native_volume_m3':measured,'relative_volume_error':abs(measured/expected-1),
                                 'stl_sha256':hashlib.sha256(mesh.read_bytes()).hexdigest()}
    return {'input':manifest['source'],'mesh':geometry,'result':result}


def verify(root):
    verify_pairing(root)
    raw = raw_cylinder()
    ir, case = build(root,'ecxml',raw.encode(),ECXML,'ecxml')
    data = load(ir/'flotherm-ir.json')
    cylinder = data['objects'][0]['shape']
    assert cylinder['kind'] == 'cylinder' and cylinder['edges_m'][-1] == [0,0,.04]
    monitored = {row['name']:row['objects'] for row in data['monitors']}
    assert monitored['Solid sample'] == [data['objects'][0]['id']]
    assert monitored['Air inside cylinder envelope'] == []
    xml = solve(case,{'cylinder':math.pi*.015*.03*.04,'box':.04*.04*.06},.5)
    manifest = load(case/'case-manifest.json')
    measurements = {}
    for monitor in manifest['monitor_results']:
        region,name = monitor['region'],monitor['object_name']
        file, = (case/'postProcessing'/name/region).glob('*/T')
        step, result = file.read_text(encoding='utf8').splitlines()[-1].split()
        assert step == xml['result']['time'] and math.isfinite(float(result))
        measurements[monitor['name']] = {'region':region,'temperature_k':float(result)}
    assert measurements['Air inside cylinder envelope']['region'] == 'fluid'
    assert measurements['Solid sample']['region'] == 'solid0'
    xml['probes'] = measurements

    ir, case = build(root,'pdml',raw_pdml(),PDML,'pdml')
    model = load(ir/'flotherm-ir.json')
    for obj,(_,name,position,_,_) in zip(model['objects'],PRIMITIVES):
        assert obj['shape']['kind'] == name
        assert all(math.isclose(a,b,abs_tol=1e-12) for a,b in zip(obj['shape']['reference_m'],position))
    factors = {'prism':.5,'tet':1/6,'invertedTet':1/6,'oriented-box':1,'cylinder':math.pi/4}
    volumes = {name:math.prod(edges)*factors[name] for _,name,_,edges,_ in PRIMITIVES}
    pdml = solve(case,volumes,.35)

    original = raw.replace('<x>0.15</x><y>0.04</y><z>0.03</z>','<x>0.075</x><y>0.04</y><z>0.04</z>',1)
    assert original != raw
    overlap = root/'overlap.ecxml'
    overlap.write_text(original,encoding='utf8')
    invoke('import',overlap,'--config',root/'ecxml.json','--output',root/'overlap-ir')
    guarded = invoke('build',root/'overlap-ir','--output',root/'overlap-case')
    assert not guarded['runnable_preflight'] and any('相交或接触' in row['reason'] for row in guarded['blockers'])
    assert 'unresolved blockers' in wsl(root/'overlap-case','bash Allrun.pre',expected=1).stderr
    return {'ecxml':xml,'pdml':pdml,'overlap_blocked':True,'same_bounds_wrong_axis_rejected':True}


def verify_s3():
    with authenticated_case_api('flotherm-geometry-s3') as api:
        case = api.create_case('FloTHERM cylinder conversion')
        path, raw = 'Ref/cylinder.ecxml',raw_cylinder()
        api.put(path,raw)
        result = api.call('POST',api.prefix+'/flotherm:import',{'path':path,
                          'expected_sha256':hashlib.sha256(raw.encode()).hexdigest()})
        api.put('Ref/geometry-ir.json',result['content'])
        api.put('Ref/mesh.json',json.dumps(ECXML))
        output = 'C-run/cylinder'
        job = api.call('POST',api.prefix+'/flotherm:convert',{'ir_path':'Ref/geometry-ir.json',
                       'settings_path':'Ref/mesh.json','output_dir':output})['job_id']
        result = api.wait_job(job,timeout=100)
        assert result['status'] == 'succeeded' and not result['completion_pending'], result
        def read(name):
            return json.loads(api.call('GET',api.prefix+f'/file?path={output}/{name}')['content'])
        geometry = read('case-manifest.json')['mesh_geometry']
        assert geometry['solid0']['kind'] == 'cylinder'
        files = read('.flotherm2foam-generated')['files']
        assert geometry['solid0']['surface'] in files and 'system/snappyHexMeshDict' in files
        return {'case':case['id'],'job':job,'evidence':str(api.root),'geometry':geometry}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--s3',action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='flotherm-geometry-acceptance-',dir=args.report.resolve().parent))
    evidence = {'passed':False,'root':str(root),'uid':65534,'actual_model_calls':0,'simulated_model_calls':0}
    try:
        evidence.update(verify(root))
        if args.s3:
            evidence['authenticated_s3'] = verify_s3()
        evidence['passed'] = True
    finally:
        args.report.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('FLOTHERM_GEOMETRY_PASS',args.report)


if __name__ == '__main__':
    main()
