"""Verify FloTHERM solid fins, sealed cavities and heat transfer in OpenFOAM."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import tempfile

from flotherm_channel_case_acceptance import _xyz, load, wsl
from flotherm_cli_acceptance import invoke
from flotherm_ecxml_acceptance import authenticated_case_api
from flotherm_paired_acceptance import method, node, numbers, token
from flotherm_plate_pcb_acceptance import _heat, _material


DOMAIN = [0,0,0,.375,.25,.25]
ORIGIN = [.125,.09375,.09375]
BASE = [.125,.0625,.015625]
HEIGHT,FIN = .03125,.0078125
FIN_CENTRES = [.00390625+i*.015625 for i in range(4)]
SHELL_ORIGIN = [.0625,.0625,.0625]
SHELL_DIMS = [.25,.125,.125]
WALL = .015625
POWER = .1
CONFIG = {'domain':DOMAIN, 'ambient_temperature_k':300, 'boundaries':{'mode':'channel','inletVelocity':.35},
          'openfoam':{'baseCellSize':.015625,'endTime':1200,'writeInterval':600,'gravity':[0,0,0]},
          'writeFormat':'binary'}


def _block(name, position, size):
    return f'<solid3dBlock><name>{name}</name>'+_xyz('location',position)+_xyz('size',size)+'</solid3dBlock>'


def source_xml(*, enclosed=True, fin_shift=0):
    material = ('<material><name>Alloy</name><density>2700</density><specific_heat>900</specific_heat>'
                '<thermalConductivity><isotropic><conductivity>160</conductivity></isotropic></thermalConductivity>'
                '</material>')
    shell = ('<enclosure><name>Sealed shell</name><material>Alloy</material>'
             +_xyz('location',SHELL_ORIGIN)+_xyz('size',SHELL_DIMS)+f'<wallThickness>{WALL}</wallThickness></enclosure>')
    fins = [_block(f'Fin {i+1}',[ORIGIN[0],ORIGIN[1]+y+(fin_shift if i==1 else 0),ORIGIN[2]+BASE[2]],
                   [BASE[0],FIN,HEIGHT]) for i,y in enumerate(FIN_CENTRES)]
    geometry = ('<heatsink><name>Cooling fins</name><material>Alloy</material>'
                f'<powerDissipation>{POWER}</powerDissipation><parts>'
                +_block('Base',ORIGIN,BASE)+''.join(fins)+'</parts></heatsink>')
    points = [('Fin solid',[.1328125,.1015625,.12109375]), ('Fin gap',[.1328125,.109375,.12109375]),
              ('Ambient air',[.034,.10,.10])]
    if enclosed:
        points.append(('Shell wall',[.0703125,.1015625,.12109375]))
    probes = ''.join(f'<monitorPoint><name>{name}</name>{_xyz("location",point)}</monitorPoint>' for name,point in points)
    return ('<neutralXML><solutionDomain>'+_xyz('location',DOMAIN[:3])+_xyz('size',DOMAIN[3:])+
            '<ambientConditions><temperature>300</temperature></ambientConditions></solutionDomain>'
            f'<materials>{material}</materials><geometry>{shell if enclosed else ""}{geometry}{probes}'
            '</geometry></neutralXML>').encode()


def source_pdml(*, pins=False, enclosure=False, angle=0):
    material, reference = _material('Alloy',160,2700,900), method(0x1001005,(8,'Alloy'))
    if enclosure:
        axes = [span if i==j else 0 for i,span in enumerate(SHELL_DIMS) for j in range(3)]
        part = node(0x2031000,'Sealed shell',numbers(0x1003009,*SHELL_DIMS),numbers(0x100300A,*axes),
                    numbers(0x100300B,*SHELL_ORIGIN),reference)
    else:
        def enum(offset, value):
            return method(0x2034000+offset,(12,value))

        size = [BASE[0],BASE[1],BASE[2]+HEIGHT]
        theta = math.radians(angle)
        c,s = math.cos(theta),math.sin(theta)
        edges = [c*size[0],s*size[0],0,-s*size[1],c*size[1],0,0,0,size[2]]
        modes = [enum(0x1a,0x307c001),enum(0x1b,0x307d001),numbers(0x203401c,.015625)] if pins else []
        part = node(0x2034000,'Cooling fins',numbers(0x1003009,*size),numbers(0x100300A,*edges),
                   numbers(0x100300B,*ORIGIN),reference,method(0x1001008,(8,'sinkHeat')),
                   enum(1,0x303e001),numbers(0x2034002,BASE[1],BASE[0],BASE[2]),
                   numbers(0x2034004,.00390625,.00390625),method(0x2034006,(12,0x302a001),(12,0x302c001)),
                   numbers(0x2034007,HEIGHT,FIN,0),*(enum(i,0x306a001) for i in (9,0xe,0x13)),
                   enum(0x18,0x307b002 if pins else 0x307b001),numbers(0x2034019,3,3 if pins else 2),
                   numbers(0x2034020,0,0),*(enum(i,0x302a001) for i in (0x21,0x22,0x23)),
                   enum(0x24,0x307a001),enum(0x25,0x307a001),numbers(0x2034026,0,0),*modes)
    return (b'#FFFB V2310 enclosure and heatsink fixture\n'+bytes.fromhex('fffb00000906000a')+
            material+_heat('sinkHeat',POWER)+part+token(1,0)+b'\n#END\n')


def build(root, label, raw, ext, config=CONFIG):
    source, settings, ir = root/f'{label}.{ext}',root/f'{label}.json',root/(label+'-ir')
    source.write_bytes(raw)
    settings.write_text(json.dumps(config),encoding='utf8')
    result = invoke('import',source,'--config',settings,'--output',ir)
    assert result['source']['sha256']==hashlib.sha256(raw).hexdigest()
    case = root/(label+'-case')
    return case, invoke('build',ir,'--output',case), load(ir/'flotherm-ir.json')


def solve(case, *, timeout=180):
    assert invoke('check',case)['ok']
    wsl(case,'ulimit -c 0\nbash Allrun.serial > log.run 2>&1',timeout=timeout)
    assert invoke('check',case)['ok']
    report = invoke('flow',case,'--energy-balance')
    gate, manifest = (load(case/name) for name in ('mesh_gate.json','case-manifest.json'))
    assert gate['ok'] and 'End' in (case/'log.run').read_text(encoding='utf8')
    energy = report['energy_balance']
    assert math.isclose(energy['source_power_w'],POWER) and abs(energy['source_to_wall_residual_w'])<.002, energy
    assert abs(energy['wall_to_ports_residual_w'])<.002, energy
    return {'case_path':str(case),'mesh_gate':gate,'manifest':manifest,'flow':report}


def probe_temperature(case, manifest, name):
    record, = [row for row in manifest['monitor_results'] if row['name']==name]
    probes = list((case/'postProcessing'/record['object_name']/record['region']).glob('*/T'))
    assert len(probes)==1,probes
    return record['region'],float(probes[0].read_text(encoding='utf8').splitlines()[-1].split()[-1])


def verify(root):
    enclosed_config = {**CONFIG,'openfoam':{**CONFIG['openfoam'],'endTime':600,'writeInterval':300}}
    case, built, _ = build(root,'enclosed',source_xml(),'ecxml',enclosed_config)
    assert built['runnable_preflight'],built
    enclosed = solve(case,timeout=480)
    gate, manifest = enclosed['mesh_gate'], enclosed['manifest']
    assert gate['regions']==['fluid','cavity0','solid0','solid1']
    shell_volume = math.prod(SHELL_DIMS)-math.prod(size-2*WALL for size in SHELL_DIMS)
    sink_volume = math.prod(BASE)+len(FIN_CENTRES)*BASE[0]*FIN*HEIGHT
    assert math.isclose(gate['region_stats']['solid0']['volume_m3'],shell_volume,rel_tol=1e-8)
    assert math.isclose(gate['region_stats']['solid1']['volume_m3'],sink_volume,rel_tol=1e-8)
    cavity_volume = math.prod(size-2*WALL for size in SHELL_DIMS)-sink_volume
    assert math.isclose(gate['region_stats']['cavity0']['volume_m3'],cavity_volume,rel_tol=1e-8)
    assert set(manifest['mesh_patches']['cavity0'])=={'cavity0_to_solid0','cavity0_to_solid1'}
    enclosed['probes'] = {name:probe_temperature(case,manifest,name) for name in
                          ('Fin solid','Fin gap','Shell wall','Ambient air')}
    assert [value[0] for value in enclosed['probes'].values()]==['solid1','cavity0','solid0','fluid']
    assert enclosed['probes']['Fin solid'][1]>enclosed['probes']['Fin gap'][1]>300

    case,built,model = build(root,'pins',source_pdml(pins=True),'pdml')
    assert built['runnable_preflight'],built
    assert model['objects'][0]['heatsink']['family']=='inlineRectangularPinFin'
    pins = solve(case)
    actual = pins['mesh_gate']['region_stats']['solid0']['volume_m3']
    assert math.isclose(actual,math.prod(BASE)+9*.015625*FIN*HEIGHT,rel_tol=1e-8)

    xml,pdml = root/'pair.ecxml',root/'pair.pdml'
    xml.write_bytes(source_xml(enclosed=False))
    pdml.write_bytes(source_pdml())
    invoke('import',xml,'--pdml',pdml,'--config',root/'pins.json','--output',root/'paired-ir')
    assert load(root/'paired-ir/flotherm-ir.json')['source_associations'][0]['verified_by']==[
        'name','bounds_m','part_corners']
    xml.write_bytes(source_xml(enclosed=False,fin_shift=.001))
    refused = invoke('import',xml,'--pdml',pdml,'--config',root/'pins.json','--output',root/'bad-pair',
                     status=2,returns_json=False)
    assert '配对' in refused.stderr

    case,built,model = build(root,'shell-without-thickness',source_pdml(enclosure=True),'pdml')
    assert not built['runnable_preflight'] and '壁厚' in built['blockers'][0]['reason']
    options = root/'thickness.json'
    options.write_text(json.dumps({'objectOverrides':{
        model['objects'][0]['id']:{'wallThickness':WALL}}}),encoding='utf8')
    converted = root/'shell-with-thickness'
    assert invoke('build',root/'shell-without-thickness-ir','--config',options,'--output',converted)['runnable_preflight']
    wsl(converted,'bash Allrun.pre > log.pre 2>&1')
    assert load(converted/'mesh_gate.json')['ok']
    assert load(converted/'case-manifest.json')['solid_regions']['solid0']['enclosure']['thickness_source']==(
        'objectOverrides.wallThickness')
    return {'ecxml_enclosed':enclosed,'pdml_pins':pins,'paired_fin_geometry':True,
            'wrong_fin_position_refused':True,'missing_pdml_shell_thickness_refused':True,
            'pdml_shell_override_mesh':load(converted/'mesh_gate.json')}


def verify_s3():
    raw = source_xml()
    with authenticated_case_api('flotherm-parts-s3') as api:
        case = api.create_case('FloTHERM fins and enclosure')
        path = 'Ref/device.ecxml'
        api.put(path,raw.decode())
        imported = api.call('POST',api.prefix+'/flotherm:import',{'path':path,'expected_sha256':hashlib.sha256(raw).hexdigest()})
        api.put('Ref/device-ir.json',imported['content'])
        options = {key:CONFIG[key] for key in ('boundaries','openfoam','writeFormat')}
        api.put('Ref/device-config.json',json.dumps(options))
        output = 'C-run/device'
        job = api.call('POST',api.prefix+'/flotherm:convert',{'ir_path':'Ref/device-ir.json',
            'settings_path':'Ref/device-config.json','output_dir':output})['job_id']
        result = api.wait_job(job,timeout=100)
        assert result['status']=='succeeded' and not result['completion_pending'],result
        def read(name):
            return api.call('GET',api.prefix+f'/file?path={output}/{name}')['content']
        published = json.loads(read('case-manifest.json'))
        assert len(published['sealed_cavities'])==1 and published['solid_regions']['solid1']['heatsink']['part_count']==5
        assert json.loads(read('preflight.json'))['ok']
        assert 'cavity0' in read('constant/regionProperties')
        return {'case':case['id'],'job':job,'root':str(api.root)}


def verify_tilted(root):
    config = {**CONFIG,'openfoam':{**CONFIG['openfoam'],'baseCellSize':.0165,'endTime':320,'writeInterval':160}}
    case,built,_ = build(root,'tilted',source_pdml(angle=13),'pdml',config)
    assert built['runnable_preflight'],built
    result = solve(case,timeout=420)
    exact = math.prod(BASE)+len(FIN_CENTRES)*BASE[0]*FIN*HEIGHT
    assert math.isclose(result['mesh_gate']['region_stats']['solid0']['volume_m3'],exact,rel_tol=.005)
    assert result['manifest']['mesh_geometry']['solid0']['kind']=='box-union'
    return result


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--work',type=Path)
    parser.add_argument('--s3',action='store_true')
    args = parser.parse_args()
    directory = args.work or Path(tempfile.mkdtemp(prefix='flotherm-parts-',dir=args.report.resolve().parent))
    if args.work:
        directory.mkdir()
    evidence = {'passed':False,'root':str(directory.resolve()),'openfoam':'v2406','uid':65534,
                'actual_model_calls':0,'simulated_model_calls':0}
    try:
        evidence.update(verify(directory.resolve()))
        evidence['pdml_tilted_plate_fins'] = verify_tilted(directory.resolve())
        if args.s3:
            evidence['authenticated_s3'] = verify_s3()
        evidence['passed'] = True
    finally:
        args.report.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('FLOTHERM_PARTS_PASS',args.report)
