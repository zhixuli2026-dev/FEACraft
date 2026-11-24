"""Verify imported FloTHERM mesh regions with actual OpenFOAM cell levels."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile

from flotherm_channel_case_acceptance import _xyz, load, source_xml, wsl
from flotherm_cli_acceptance import invoke
from flotherm_ecxml_acceptance import authenticated_case_api
from flotherm_geometry_acceptance import raw_cylinder
from flotherm_parts_acceptance import _heat, _material
from flotherm_paired_acceptance import method, node, numbers, token
from flotherm_flow_devices_acceptance import CURVE, configuration, xml


OPTIONS = {'boundaries':{'mode':'channel','inletVelocity':.15},'openfoam':{
    'baseCellSize':.02,'endTime':2000,'writeInterval':1000},'writeFormat':'binary'}
AREA_START = [.018,.027,.036]
AREA_SIZE = [.03,.048,.052]


def region_xml(name, position, dimensions):
    return f'<region><name>{name}</name>'+_xyz('location',position)+_xyz('size',dimensions)+'</region>'


def ecxml(*, tiny=False, outside=False):
    position = [.312,.027,.036] if outside else AREA_START
    requested = region_xml('Local wake volume',position,[1e-6]*3 if tiny else AREA_SIZE)
    retained = region_xml('Coordinate observation',[.245,.034,.025],[.025,.031,.04])
    return source_xml().replace('</geometry>',requested+retained+'</geometry>')


def binary():
    size, angle = (.12,.12,.075), math.pi/4
    c,s = math.cos(angle),math.sin(angle)
    signed_edges = [size[0]*c,size[0]*s,0,size[1]*s,-size[1]*c,0,0,0,size[2]]
    rotated = node(0x2029000,'Rotated sampling volume',numbers(0x1003009,*size),
                   numbers(0x100300A,*signed_edges),numbers(0x100300B,.023,.12,.025))
    solid = node(0x2025000,'Thermal block',numbers(0x1003009,.04,.04,.04),
                 numbers(0x100300A,.04,0,0,0,.04,0,0,0,.04),
                 numbers(0x100300B,.25,.14,.13),method(0x1001005,(8,'Alloy')),
                 method(0x1001008,(8,'heat')))
    return (b'#FFFB V2310 local mesh region\n'+bytes.fromhex('fffb00000906000a')+
            _material('Alloy',160,2700,900)+_heat('heat',.25)+rotated+solid+token(1,0)+b'\n#END\n')


def stage(root, label, raw, config, extension):
    source, options, ir = root/f'{label}.{extension}',root/f'{label}.json',root/(label+'-ir')
    source.write_bytes(raw)
    options.write_text(json.dumps(config),encoding='utf8')
    imported = invoke('import',source,'--config',options,'--output',ir)
    assert imported['source']['sha256']==hashlib.sha256(raw).hexdigest()
    return ir,load(ir/'flotherm-ir.json'),source


def build(root, label, ir, settings=None):
    case = root/(label+'-case')
    options = []
    if settings:
        conf = root/(label+'-override.json')
        conf.write_text(json.dumps(settings),encoding='utf8')
        options = ['--config',conf]
    return case,invoke('build',ir,*options,'--output',case)


def mesh(case):
    assert invoke('check',case)['ok']
    wsl(case,'ulimit -c 0\nbash Allrun.pre > log.pre 2>&1')
    gate,manifest = [load(case/name) for name in ('mesh_gate.json','case-manifest.json')]
    assert gate['ok'] and manifest['refinements']
    for row in manifest['refinements']:
        evidence = gate['refinements'][row['name']]
        assert evidence['id']==row['id'] and evidence['minimum_level']==row['level'] and evidence['cells']>0
    return gate,manifest


def energy(case, watts):
    wsl(case,'ulimit -c 0\nbash Allrun.solve > log.run 2>&1',timeout=240)
    flow = invoke('flow',case,'--energy-balance')
    report = flow['energy_balance']
    assert math.isclose(report['source_power_w'],watts) and abs(report['source_to_wall_residual_w'])<.01*watts
    return flow


def coarse_aabb_cells(case, region):
    low,high = region['bounds_m'][:3],region['bounds_m'][3:]
    box = ' '.join('('+' '.join(map(str,point))+')' for point in (low,high))
    target = case/'system'/'aabbWitnessDict'
    target.write_text(
        'FoamFile { version 2.0; format ascii; class dictionary; object aabbWitnessDict; }\n'
        'actions (\n'
        f'{{ name aabbWitness; type cellSet; action new; source boxToCell; box {box}; }}\n'
        '{ name aabbWitness; type cellSet; action subset; source fieldToCell; field cellLevel; min 0; max 0; }\n'
        ');\n',encoding='utf8')
    try:
        wsl(case,'topoSet -time 0 -dict system/aabbWitnessDict > log.aabbWitness')
    finally:
        target.unlink()
    counts = list(map(int,re.findall(r'cellSet aabbWitness now size (\d+)',
                    (case/'log.aabbWitness').read_text(encoding='utf8'))))
    assert len(counts)==2 and 0<counts[1]<counts[0],counts
    return counts[1]


def verify(root):
    stage_ir, model, _ = stage(root,'region',ecxml().encode(),OPTIONS,'ecxml')
    requested,retained = [row for row in model['regions'] if row['type']=='mesh-region']
    first = model['objects'][0]['id']
    settings = {'refinement':{requested['id']:1},'objectOverrides':{first:{'refinement':1}},'maxSkewness':2.0}
    case, result = build(root,'refined',stage_ir,settings)
    assert result['runnable_preflight'],result
    gate,manifest = mesh(case)
    assert gate['max_skewness']<=2 and len(gate['refinements'])==2
    assert all(gate['region_stats'][name]['cells']>=count for name,count in manifest['background_mesh_cells'].items())
    assert gate['region_stats']['solid0']['cells']>manifest['background_mesh_cells']['solid0']
    rows = {row['id']:row for row in manifest['regions']}
    assert rows[requested['id']]['mapping']=='refinement' and rows[retained['id']]['mapping']=='metadata-only'
    coverage = load(case/'conversion-coverage.json')
    assert coverage['refinements']==manifest['refinements'] and coverage['summary']['input_power_w']==.5
    xml_flow = energy(case,.5)
    assert invoke('check',case)['ok']

    config = {**OPTIONS,'domain':[0,0,0,.36,.24,.24],'ambient_temperature_k':300}
    stage_ir, model, _ = stage(root,'rotated',binary(),config,'pdml')
    region, = model['regions']
    case, result = build(root,'rotated',stage_ir,{'refinement':{region['id']:1}})
    assert result['runnable_preflight'],result
    rotated_gate, rotated_manifest = mesh(case)
    choice, = rotated_manifest['refinements']
    assert 'oriented_box' in choice and choice['oriented_box']['origin_m']!=region['origin_m']
    assert 'Applying source rotatedBoxToCell' in (case/'log.refinementCheck').read_text(encoding='utf8')
    assert 'searchableRotatedBox' in (case/'system/snappyHexMeshDict').read_text(encoding='utf8')
    coarse_outside_region = coarse_aabb_cells(case,region)
    pdml_flow = energy(case,.25)

    curve = raw_cylinder().replace('</geometry>',region_xml('Upstream',AREA_START,AREA_SIZE)+'</geometry>')
    curved_ir, model, _ = stage(root,'curved',curve.encode(),OPTIONS,'ecxml')
    case,result = build(root,'curved',curved_ir,{'refinement':{'ecxmlRegion1':1},
                                               'objectOverrides':{model['objects'][0]['id']:{'refinement':2}}})
    assert result['runnable_preflight']
    surface_gate, surface_manifest = mesh(case)
    assert surface_manifest['mesh_geometry']['solid0']['refinement']==[2,2]
    curved_flow = energy(case,.5)

    tiny, _, _ = stage(root,'tiny',ecxml(tiny=True).encode(),OPTIONS,'ecxml')
    tiny_case, accepted = build(root,'tiny',tiny,{'refinement':{requested['id']:1}})
    assert accepted['runnable_preflight']
    wsl(tiny_case,'bash Allrun.pre > log.pre 2>&1',expected=1)
    refused = load(tiny_case/'mesh_gate.json')
    assert not refused['ok'] and 'counts [0, 0]' in refused['reason']
    outside, _, _ = stage(root,'outside',ecxml(outside=True).encode(),OPTIONS,'ecxml')
    _, rejected = build(root,'outside',outside,{'refinement':{requested['id']:1}})
    assert not rejected['runnable_preflight'] and '超出' in rejected['blockers'][0]['reason']
    skew_case, accepted = build(root,'skew',curved_ir,{'maxSkewness':.05})
    assert accepted['runnable_preflight']
    wsl(skew_case,'bash Allrun.pre > log.pre 2>&1',expected=1)
    assert not load(skew_case/'mesh_gate.json')['ok']
    diagnostic = (skew_case/'log.pre').read_text(encoding='utf8')
    assert 'actual mesh skewness' in diagnostic.lower() or 'mesh quality limits' in diagnostic

    # A whole-fluid case has a named mesh and no splitMeshRegions step.
    points = ''.join(f'<fanCurvePoint><volumeFlow>{q}</volumeFlow><pressure>{p}</pressure></fanCurvePoint>' for q,p in CURVE)
    fan = xml('axial3dFan','Refined fan',f'<plane>+yz</plane><flowDefinition><fanCurve>{points}</fanCurve></flowDefinition>')
    stage_ir, model, _ = stage(root,'fan',fan,configuration(.35),'ecxml')
    case, accepted = build(root,'fan',stage_ir,{'objectOverrides':{model['objects'][0]['id']:{'refinement':1}}})
    assert accepted['runnable_preflight']
    fan_gate,fan_manifest = mesh(case)
    device, = fan_manifest['flow_devices']
    assert fan_gate['regions']==['fluid']
    assert fan_gate['flow_zone_counts'][device['cell_zone']]>=8*device['expected_cells']
    assert fan_gate['flow_zone_counts'][device['face_zone']]>=4*device['expected_faces']
    wsl(case,'ulimit -c 0\nbash Allrun.solve > log.run 2>&1')
    state = case/'400'/'fluid'/'uniform'/f'{device["source"]}Properties'
    fan_volume_flow = float(re.search(r'(?m)^flow_rate\s+([^;]+);',state.read_text(encoding='utf8'))[1])
    assert math.isclose(fan_volume_flow,.35*device['area_m2'],rel_tol=.01)
    return {'ecxml':{'gate':gate,'manifest':manifest,'flow':xml_flow},
            'pdml_rotated':{'gate':rotated_gate,'manifest':rotated_manifest,'flow':pdml_flow,
                            'coarse_cells_in_aabb':coarse_outside_region},
            'curved_surface':{'gate':surface_gate,'manifest':surface_manifest,'flow':curved_flow},
            'fluid_only_fan':{'gate':fan_gate,'volume_flow_m3_s':fan_volume_flow},
            'empty_target_refused':True,'outside_target_refused':True,
            'excess_skewness_refused':True}


def verify_s3():
    raw = ecxml()
    with authenticated_case_api('flotherm-region-s3') as api:
        case = api.create_case('FloTHERM local refinement')
        path = 'Ref/region.ecxml'
        api.put(path,raw)
        imported = api.call('POST',api.prefix+'/flotherm:import',{'path':path})
        model = json.loads(imported['content'])
        region = next(row for row in model['regions'] if row['type']=='mesh-region')
        api.put('Ref/model.json',imported['content'])
        api.put('Ref/settings.json',json.dumps({**OPTIONS,'refinement':{region['id']:1}}))
        output = 'C-run/refined'
        job = api.call('POST',api.prefix+'/flotherm:convert',{
            'ir_path':'Ref/model.json','settings_path':'Ref/settings.json','output_dir':output})['job_id']
        result = api.wait_job(job,timeout=100)
        assert result['status']=='succeeded' and not result['completion_pending'],result
        def read(name):
            return api.call('GET',api.prefix+f'/file?path={output}/{name}')['content']
        manifest = json.loads(read('case-manifest.json'))
        assert manifest['refinements'][0]['id']==region['id']
        assert 'fieldToCell' in read('system/refinementCheckDict')
        assert json.loads(read('conversion-coverage.json'))['refinements']==manifest['refinements']
        return {'case':case['id'],'job':job,'root':str(api.root)}


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--work',type=Path)
    parser.add_argument('--s3',action='store_true')
    args = parser.parse_args()
    root = args.work or Path(tempfile.mkdtemp(prefix='flotherm-refinement-',dir=args.report.resolve().parent))
    if args.work:
        root.mkdir()
    report = {'passed':False,'root':str(root.resolve()),'openfoam':'v2406','uid':65534,
              'actual_model_calls':0,'simulated_model_calls':0}
    try:
        report.update(verify(root.resolve()))
        if args.s3:
            report['authenticated_s3'] = verify_s3()
        report['passed'] = True
    finally:
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('FLOTHERM_REFINEMENT_PASS',args.report)
