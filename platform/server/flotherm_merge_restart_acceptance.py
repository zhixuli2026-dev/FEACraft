"""Verify native FloTHERM box unions and preserved serial/MPI checkpoints."""

import argparse
import json
import math
from pathlib import Path
import re
import tempfile

from flotherm_channel_case_acceptance import _xyz, load, wsl
from flotherm_cli_acceptance import invoke, snapshot
from flotherm_ecxml_acceptance import NEUTRAL_ECXML, authenticated_case_api
from flotherm_flow_devices_acceptance import FIXED, configuration, fixed_fan
from flotherm_refinement_acceptance import build, stage


CONFIG = {'boundaries':{'mode':'channel','inletVelocity':.21},
          'openfoam':{'baseCellSize':.015625,'endTime':800,'writeInterval':200},
          'writeFormat':'binary','startFrom':'latestTime','mergeTouchingBoxes':True}


def source_xml(overlap=False):
    specimens = [('Left heater',[.09375,.0625,.0625],[.046875,.03125,.03125],.25),
                 ('Right heater',[.125 if overlap else .140625,.0625,.0625],[.046875,.046875,.03125],.15),
                 ('Separate solid',[.234375,.109375,.0625],[.03125,.03125,.03125],0)]
    solids = []
    for name,origin,dimensions,power in specimens:
        solids.append(f'<solid3dBlock><name>{name}</name><material>Alloy</material>'+
                      _xyz('location',origin)+_xyz('size',dimensions)+
                      f'<powerDissipation>{power}</powerDissipation></solid3dBlock>')
    return ('<neutralXML><solutionDomain>'+_xyz('location',[0,0,0])+_xyz('size',[.375,.1875,.1875])+
            '<ambientConditions><temperature>300</temperature></ambientConditions></solutionDomain>'
            '<materials><material><name>Alloy</name><density>2400</density><specific_heat>900</specific_heat>'
            '<thermalConductivity><isotropic><conductivity>12</conductivity></isotropic></thermalConductivity>'
            '</material></materials><geometry>'+''.join(solids)+'</geometry></neutralXML>').encode()


def run_until(case, name, end, script, previous=None):
    control = case/'system/controlDict'
    original = control.read_bytes()
    try:
        wsl(case,f'foamDictionary system/controlDict -entry endTime -set {end} > log.settings\n'
                 f'ulimit -c 0\nbash {script} > log.{name} 2>&1',timeout=240)
    finally:
        control.write_bytes(original)
    log = (case/f'log.{name}').read_text(encoding='utf8')
    if previous is not None:
        assert f'solver from {previous}' in log and f'\nTime = {previous+1}\n' in log
        assert 'blockMesh finished' not in log
    assert f'\nTime = {end}\n' in log and (case/str(end)).is_dir()
    return log


def verify(root):
    ir, model, _ = stage(root,'seam',source_xml(),CONFIG,'ecxml')
    objects = [row['id'] for row in model['objects']]
    model['monitors'].append({'type':'region-temperature','objects':objects[:2]})
    (ir/'flotherm-ir.json').write_text(json.dumps(model),encoding='utf8')
    case,built = build(root,'seam',ir,{'objectOverrides':{objects[1]:{'refinement':1}}})
    assert built['runnable_preflight'] and invoke('check',case)['ok']
    manifest = load(case/'case-manifest.json')
    merged, = manifest['merged_solid_regions'].values()
    assert [row['id'] for row in merged['members']]==objects[:2]
    assert [row['power_w'] for row in manifest['heat_sources']]==[.25,.15]
    assert len({row['cell_zone'] for row in manifest['monitor_results']})==2
    cold = run_until(case,'cold',200,'Allrun.serial')
    assert 'Starting serial solver from 0' in cold and 'blockMesh finished' in cold
    gate = load(case/'mesh_gate.json')
    counts = gate['merged_zone_counts']['solid0']
    assert gate['ok'] and gate['regions']==['fluid','solid0','solid2'] and min(counts.values())>0
    expected = .046875*.03125*.03125+.046875*.046875*.03125
    assert math.isclose(gate['region_stats']['solid0']['volume_m3'],expected,rel_tol=1e-9)
    original_mesh = snapshot(case/'constant/fluid/polyMesh')
    originals = {200:snapshot(case/'200')}
    pre = wsl(case,'bash Allrun.pre',expected=1)
    assert 'without remeshing' in pre.stderr
    assert snapshot(case/'constant/fluid/polyMesh')==original_mesh
    run_until(case,'serialRestart',400,'Allrun.serial',200)
    originals[400] = snapshot(case/'400')
    first_mpi = run_until(case,'parallelStart',600,'Allrun.parallel',400)
    assert 'Time = 400' in first_mpi and 'on 2 ranks' in first_mpi and 'Reconstructing fields' in first_mpi
    originals[600] = snapshot(case/'600')
    run_until(case,'parallelRestart',800,'Allrun.parallel',600)
    assert all(snapshot(case/str(key))==value for key,value in originals.items())
    assert snapshot(case/'constant/fluid/polyMesh')==original_mesh
    flow = invoke('flow',case,'--energy-balance')
    assert math.isclose(flow['energy_balance']['port_net_sensible_w'],.4,abs_tol=.002)
    monitors = {}
    for row in manifest['monitor_results']:
        files = list((case/'postProcessing/solid0'/row['object_name']).glob('*/volFieldValue.dat'))
        assert files, row
        last = [file.read_text(encoding='utf8').splitlines()[-1].split() for file in files]
        assert any(int(parts[0])==800 and float(parts[1])>300 for parts in last),last
        monitors[row['object']] = next(float(parts[1]) for parts in last if int(parts[0])==800)
    assert not math.isclose(*monitors.values(),abs_tol=.01)
    last_checkpoint = snapshot(case/'800')
    (case/'1000').mkdir()
    invalid = wsl(case,'bash Allrun.solve',expected=1)
    assert 'Incomplete latest checkpoint' in invalid.stderr
    assert snapshot(case/'800')==last_checkpoint and all(snapshot(case/str(key))==value for key,value in originals.items())
    (case/'1000').rmdir()
    assert invoke('check',case)['ok']

    originals[800] = last_checkpoint
    run_until(case,'serialFromParallel',1000,'Allrun.solve',800)
    originals[1000] = snapshot(case/'1000')
    replaced_fields = run_until(case,'parallelFromSerial',1200,'Allrun.parallel',1000)
    assert re.search(r'decomposePar .*?-fields',replaced_fields),replaced_fields[:2000]
    originals[1200] = snapshot(case/'1200')
    control = case/'system/controlDict'
    recipe = control.read_bytes()
    try:
        wsl(case,'foamDictionary system/controlDict -entry endTime -set 1400 > log.settings\n'
                 'mpirun -np 2 chtMultiRegionSimpleFoam -parallel > log.rankOnly 2>&1')
    finally:
        control.write_bytes(recipe)
    assert not (case/'1400').exists() and all((case/f'processor{i}/1400/fluid/T').is_file() for i in range(2))
    resumed = run_until(case,'rankRecovery',1600,'Allrun.solve',1400)
    assert 'Reconstructing fields' in resumed and (case/'1400/fluid/T').is_file()
    assert all(snapshot(case/str(key))==value for key,value in originals.items())
    assert snapshot(case/'constant/fluid/polyMesh')==original_mesh
    restored_flow = invoke('flow',case,'--energy-balance')
    last_before_mismatch = snapshot(case/'1600')
    incomplete_rank = case/'processor0/1800'
    incomplete_rank.mkdir()
    refusal = wsl(case,'bash Allrun.parallel',expected=1)
    assert 'different latest checkpoint times' in refusal.stderr
    assert snapshot(case/'1600')==last_before_mismatch
    incomplete_rank.rmdir()

    overlap, _, _ = stage(root,'overlap',source_xml(overlap=True),CONFIG,'ecxml')
    _, blocked = build(root,'unmerged-overlap',overlap,{'mergeTouchingBoxes':False})
    assert not blocked['runnable_preflight'] and '重叠' in blocked['blockers'][0]['reason']
    candidate, result = build(root,'merged-overlap',overlap)
    assert result['runnable_preflight']
    wsl(candidate,'bash Allrun.pre > log.pre 2>&1')
    actual = load(candidate/'mesh_gate.json')['region_stats']['solid0']['volume_m3']
    common = .015625*.03125*.03125
    assert math.isclose(actual,expected-common,rel_tol=1e-9)
    return {'manifest':manifest,'mesh_gate':gate,'flow':flow,'member_temperature_k':monitors,
            'old_checkpoints_immutable':True,'incomplete_checkpoint_refused':True,
            'processor_only_checkpoint_restored':True,'restored_flow':restored_flow,
            'mismatched_ranks_refused':True,
            'overlap_only_when_explicit':True,'overlap_union_volume_m3':actual}


def verify_conduction(root):
    ir,_,_ = stage(root,'conduction',NEUTRAL_ECXML.encode(),{'startFrom':'latest'},'ecxml')
    case,built = build(root,'conduction',ir)
    assert built['runnable_preflight']
    run_until(case,'cold',20,'Allrun.serial')
    before = snapshot(case/'20')
    run_until(case,'continued',100,'Allrun.serial',20)
    assert snapshot(case/'20')==before
    result = invoke('balance',case)
    assert result['ok']
    ir,_,_ = stage(root,'conduction-mpi',NEUTRAL_ECXML.encode(),
                   {'parallelSubdomains':2,'scotch':False,'writeFormat':'binary'},'ecxml')
    case,built = build(root,'conduction-mpi',ir)
    assert built['runnable_preflight']
    log = run_until(case,'parallel',100,'Allrun.parallel')
    assert re.search(r'nProcs\s*:\s*2\b',log)
    deck = (case/'system/decomposeParDict').read_text(encoding='utf8')
    assert 'method simple;' in deck
    parallel = invoke('balance',case)
    return {'restarted':result,'parallel':parallel}


def verify_fixed_flow(root):
    config = configuration(None)
    config['openfoam']['startFrom'] = 'latestTime'
    ir,_,_ = stage(root,'fixed',fixed_fan(),config,'ecxml')
    case,built = build(root,'fixed',ir)
    assert built['runnable_preflight']
    run_until(case,'fixedCold',200,'Allrun.serial')
    device, = load(case/'case-manifest.json')['flow_devices']
    property_name = f'fluid/uniform/{device["source"]}Properties'
    before = (case/'200'/property_name).read_bytes()
    run_until(case,'fixedParallel',400,'Allrun.parallel',200)
    assert (case/'200'/property_name).read_bytes()==before
    raw = (case/'400'/property_name).read_text(encoding='utf8')
    rate = float(re.search(r'(?m)^flow_rate\s+([^;]+);',raw)[1])
    assert math.isclose(rate,FIXED,rel_tol=.02)
    return {'serial_to_mpi_checkpoint_preserved':True,'volume_flow_m3_s':rate}


def verify_s3():
    raw = source_xml()
    with authenticated_case_api('flotherm-merge-restart-s3') as api:
        case = api.create_case('Merged FloTHERM solids and restart')
        api.put('Ref/model.ecxml',raw.decode())
        imported = api.call('POST',api.prefix+'/flotherm:import',{'path':'Ref/model.ecxml'})
        api.put('Ref/model.json',imported['content'])
        api.put('Ref/settings.json',json.dumps(CONFIG))
        output = 'C-run/merged'
        job = api.call('POST',api.prefix+'/flotherm:convert',{
            'ir_path':'Ref/model.json','settings_path':'Ref/settings.json','output_dir':output})['job_id']
        result = api.wait_job(job,timeout=100)
        assert result['status']=='succeeded' and not result['completion_pending'],result
        def read(name):
            return api.call('GET',api.prefix+f'/raw?path={output}/{name}',raw=True).decode('utf8')
        manifest = json.loads(read('case-manifest.json'))
        assert manifest['start_from']=='latestTime' and len(manifest['merged_solid_regions'])==1
        assert 'reconstructPar -allRegions -newTimes' in read('Allrun.parallel')
        assert 'require_fields' in read('system/flotherm_restart.py')
        assert 'python3 system/flotherm_clean.py' in read('Allclean')
        assert 'cellProcAddressing' in read('system/flotherm_clean.py')
        assert 'objectCells0' in read('system/solid0/mergedSourcesDict')
        return {'case':case['id'],'job':job,'root':str(api.root)}


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--work',type=Path)
    parser.add_argument('--s3',action='store_true')
    args = parser.parse_args()
    root = args.work or Path(tempfile.mkdtemp(prefix='flotherm-merge-restart-',dir=args.report.resolve().parent))
    if args.work:
        root.mkdir()
    report = {'passed':False,'root':str(root.resolve()),'openfoam':'v2406','uid':65534,
              'actual_model_calls':0,'simulated_model_calls':0}
    try:
        report['conjugate'] = verify(root.resolve())
        report['conduction'] = verify_conduction(root.resolve())
        report['fixed_flow'] = verify_fixed_flow(root.resolve())
        if args.s3:
            report['authenticated_s3'] = verify_s3()
        report['passed'] = True
    finally:
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('FLOTHERM_MERGE_RESTART_PASS',args.report)
