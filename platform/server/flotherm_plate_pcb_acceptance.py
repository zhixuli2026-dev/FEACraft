"""Verify ECXML plates and PDML PCB laminates through native OpenFOAM."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile

from app.services.flotherm_channel import _area
from flotherm_channel_case_acceptance import load, wsl
from flotherm_cli_acceptance import invoke
from flotherm_ecxml_acceptance import authenticated_case_api
from flotherm_paired_acceptance import method, node, numbers, token
from flotherm_physics_acceptance import _face_direction, _mesh, _native_wall_report, _values


CONFIG = {'boundaries':{'mode':'channel','inletVelocity':.25},
          'openfoam':{'baseCellSize':.019,'thinPlateThickness':.0024,'endTime':1800}}
DOMAIN = [0,0,0,.3,.14,.14]
ECXML = '''<neutralXML><solutionDomain>
<location><x>0</x><y>0</y><z>0</z></location><size><x>.3</x><y>.14</y><z>.14</z></size>
<ambientConditions><temperature>300</temperature></ambientConditions></solutionDomain>
<materials>
<material><name>Plate</name><density>1700</density><specific_heat>850</specific_heat>
<thermalConductivity><isotropic><conductivity>1.3</conductivity></isotropic></thermalConductivity></material>
<material><name>Equivalent PCB</name><density>1950</density><specific_heat>850</specific_heat>
<thermalConductivity><orthotropic><x>5</x><y>.35</y><z>6</z></orthotropic></thermalConductivity></material>
</materials><geometry>
<solid2dBlock><name>Flat heater</name><material>Plate</material><plane>+xy</plane><thickness>.003</thickness>
<location><x>.045</x><y>.035</y><z>.049</z></location><size><x>.055</x><y>.06</y><z>.024</z></size>
<powerDissipation>.12</powerDissipation></solid2dBlock>
<printedCircuitBoard><name>Board</name><material>Equivalent PCB</material><plane>-xz</plane>
<location><x>.155</x><y>.06</y><z>.038</z></location><size><x>.067</x><y>.004</y><z>.042</z></size>
<powerDissipation>.18</powerDissipation></printedCircuitBoard>
<monitorPoint><name>Plate centre</name><location><x>.072</x><y>.058</y><z>.049</z></location></monitorPoint>
</geometry></neutralXML>'''


def _material(name, k, density, cp):
    return node(0x201B000,name,method(0x1001002,(8,name)),numbers(0x201B004,k),
                numbers(0x201B001,density),numbers(0x201B002,cp))


def _heat(name, power):
    return node(0x2022000,name,method(0x1001002,(8,name)),method(0x2022004,(6,power),(12,0x3044001)))


def pdml(copper_percent=4):
    substrate = _material('FR4',.3,1850,900)
    copper = _material('Cu',390,8960,385)
    board = node(0x2035000,'Swapped PCB',numbers(0x1003009,.067,.042,.004),
        numbers(0x100300A,.067,0,0,0,0,.042,0,-.004,0),numbers(0x100300B,.155,.064,.038),
        method(0x1001005,(8,'FR4'),(12,0x301000e)),method(0x1001005,(8,'Cu'),(12,0x301000f)),
        method(0x2035004,(12,0x302d002)),numbers(0x2035005,copper_percent),method(0x1001008,(8,'boardHeat')))
    plate = node(0x2025000,'Centre slice',numbers(0x1003009,.055,.06,.024),
        numbers(0x100300A,.055,0,0,0,.06,0,0,0,.024),numbers(0x100300B,.045,.035,.037),
        method(0x1003002,(12,0x3013004)),method(0x1003003,(12,0x3015003)),
        method(0x1001005,(8,'FR4')),method(0x1001008,(8,'plateHeat')))
    return (b'#FFFB V2310 plate-and-board-fixture\n'+bytes.fromhex('fffb00000906000a')+substrate+copper+
            _heat('boardHeat',.18)+_heat('plateHeat',.12)+plate+board+token(1,0)+b'\n#END\n')


def verify_tensor(case, moment, global_conductivity):
    digests = {}
    points,faces,owners,neighbors,patches = _mesh(case,'solid1',digests)
    wall = patches['solid1_to_fluid']
    vertices = [set() for _ in range(max(owners)+1)]
    for i,face in enumerate(faces):
        vertices[owners[i]].update(face)
        if i < len(neighbors):
            vertices[neighbors[i]].update(face)
    raw = (case/f'{moment}/solid1/T').read_bytes()
    temperatures = _values(raw,b'internalField',len(vertices))
    target = re.search(rb'\bsolid1_to_fluid\s*\{([^}]+)\}',raw)[1]
    surface = _values(target,b'value',wall.count)
    contributions = [0.,0.,0.]
    depths = set()
    for i,temp in zip(range(wall.first,wall.first+wall.count),surface):
        cell = owners[i]
        axis,coordinate = _face_direction(points,faces[i])
        positions = [points[index][axis] for index in vertices[cell]]
        center = (max(positions)+min(positions))/2
        contributions[axis] += global_conductivity[axis]*(temperatures[cell]-temp)/abs(center-coordinate)*_area(points,faces[i])
        if axis==1:
            depths.add(round(center,8))
    _,_,fluid_owners,_,fluid_patches = _mesh(case,'fluid',digests)
    native = {row['name']:row['integral_from_field_w'] for row in _native_wall_report(case,moment,digests,fluid_owners,fluid_patches)}
    assert len(depths) >= 2, depths
    assert all(value > 0 for value in contributions), contributions
    assert math.isclose(sum(contributions),native['fluid_to_solid1'],rel_tol=.004), (contributions,native)
    assert math.isclose(native['fluid_to_solid1'],.18,rel_tol=.007), native
    return {'expected_global_conductivity_w_mk':global_conductivity,'fourier_power_by_axis_w':contributions,
            'native_board_power_w':native['fluid_to_solid1'],'normal_cell_count':len(depths),'files_sha256':digests}


def run_case(root, label, source, config, global_k, thickness, provenance):
    suffix = 'pdml' if label=='pdml' else 'ecxml'
    raw,config_file = root/f'{label}.{suffix}',root/f'{label}.json'
    raw.write_bytes(source)
    config_file.write_text(json.dumps(config),encoding='utf8')
    ir,case = root/(label+'-ir'),root/(label+'-case')
    response = invoke('import',raw,'--config',config_file,'--output',ir)
    assert response['source']['sha256'] == hashlib.sha256(source).hexdigest()
    assert response['coverage']['equivalent'] == 2
    assert invoke('build',ir,'--output',case)['runnable_preflight'] and invoke('check',case)['ok']
    wsl(case,'bash Allrun.serial > log.run 2>&1')
    assert load(case/'mesh_gate.json')['ok'] and invoke('check',case)['ok']
    manifest = load(case/'case-manifest.json')
    solids = manifest['solid_regions']
    assert math.isclose(solids['solid0']['thin_plate']['thickness_m'],thickness)
    assert solids['solid0']['thin_plate']['thickness_source'] == provenance
    assert math.isclose(solids['solid0']['thin_plate']['volume_m3'],.055*.06*thickness,rel_tol=1e-10)
    result = invoke('flow',case,'--energy-balance')
    assert math.isclose(result['energy_balance']['source_power_w'],.3)
    wall = {row['name']:row['integral_from_field_w'] for row in result['wall_heat_flux']['patches']}
    assert math.isclose(wall['fluid_to_solid0'],.12,rel_tol=.007)
    tensor = verify_tensor(case,result['time'],global_k)
    return {'root':str(case),'source':manifest['source'],'plate':solids['solid0'],'board':solids['solid1'],
            'mesh_cells':manifest['mesh_cells'],'tensor':tensor,'flow':result}


def verify(root):
    xml = run_case(root,'ecxml',ECXML.encode(),CONFIG,[5,.35,6],.003,'source.thickness')
    case = Path(xml['root'])
    manifest = load(case/'case-manifest.json')
    probe, = manifest['monitor_results']
    native = case/f'postProcessing/{probe["object_name"]}/solid0/0/T'
    instant,temperature = native.read_text(encoding='utf8').splitlines()[-1].split()
    assert instant == xml['flow']['time'] and float(temperature) > 300
    xml['plate_probe_k'] = float(temperature)
    options = {**CONFIG,'domain':DOMAIN,'ambient_temperature_k':300}
    k_parallel = .96*.3+.04*390
    k_normal = 1/(.96/.3+.04/390)
    binary = run_case(root,'pdml',pdml(),options,[k_parallel,k_normal,k_parallel],.0024,'openfoam.thinPlateThickness')
    board = binary['board']
    assert board['conductivity_axes'] == [[1,0,0],[0,0,1],[0,-1,0]]
    assert all(math.isclose(a,b) for a,b in zip(board['conductivity_w_mk'],[k_parallel,k_parallel,k_normal]))
    assert math.isclose(board['density_kg_m3'],.96*1850+.04*8960)
    assert math.isclose(board['density_kg_m3']*board['specific_heat_j_kg_k'],.96*1850*900+.04*8960*385)
    assert math.isclose(binary['plate']['thin_plate']['plane_reference_m'][2],.049)
    assert board['pcb']['laminate']['copper_volume_fraction'] == .04

    config = root/'override.json'
    config.write_text(json.dumps({'objectOverrides':{'ecxmlSolid1':{'plateThickness':.006}}}),encoding='utf8')
    override = root/'overridden'
    assert invoke('build',root/'ecxml-ir','--config',config,'--output',override)['runnable_preflight']
    item = load(override/'case-manifest.json')['solid_regions']['solid0']['thin_plate']
    assert item['thickness_source'] == 'objectOverrides.plateThickness' and item['thickness_m'] == .006
    bad = root/'invalid.pdml'
    bad.write_bytes(pdml(101))
    failure = invoke('import',bad,'--config',root/'pdml.json','--output',root/'invalid-ir',status=2,returns_json=False)
    assert '铜含量' in failure.stderr and not (root/'invalid-ir').exists()
    return {'ecxml':xml,'pdml':binary,'plate_override':item,'invalid_copper_fraction_rejected':True}


def verify_s3():
    with authenticated_case_api('flotherm-plate-pcb-s3') as api:
        case = api.create_case('FloTHERM plate and PCB')
        path = 'Ref/source.ecxml'
        api.put(path,ECXML)
        imported = api.call('POST',api.prefix+'/flotherm:import',{'path':path,
                   'expected_sha256':hashlib.sha256(ECXML.encode()).hexdigest()})
        api.put('Ref/normalized.json',imported['content'])
        api.put('Ref/thermal.json',json.dumps(CONFIG))
        output = 'C-run/plate-pcb'
        job = api.call('POST',api.prefix+'/flotherm:convert',{'ir_path':'Ref/normalized.json',
                       'settings_path':'Ref/thermal.json','output_dir':output})['job_id']
        outcome = api.wait_job(job,timeout=100)
        assert outcome['status'] == 'succeeded' and not outcome['completion_pending'], outcome
        manifest = json.loads(api.call('GET',api.prefix+f'/file?path={output}/case-manifest.json')['content'])
        solids = manifest['solid_regions']
        assert solids['solid0']['thin_plate']['thickness_m'] == .003
        assert solids['solid1']['conductivity_w_mk'] == [5,.35,6]
        return {'job':job,'case':case['id'],'evidence':str(api.root),'solid_regions':solids}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--s3',action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='flotherm-plate-pcb-',dir=args.report.resolve().parent))
    evidence = {'passed':False,'root':str(root),'actual_model_calls':0,'simulated_model_calls':0,'wsl_uid':65534}
    try:
        evidence.update(verify(root))
        if args.s3:
            evidence['authenticated_s3'] = verify_s3()
        evidence['passed'] = True
    finally:
        args.report.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('FLOTHERM_PLATE_PCB_PASS',args.report)


if __name__ == '__main__':
    main()
