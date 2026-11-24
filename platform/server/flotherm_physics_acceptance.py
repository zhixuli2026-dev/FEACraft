"""Solve FloTHERM open and turbulent cases, and check tensor heat flux from raw results."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile

from app.services.flotherm_channel import _area, _latest_scalar, _walls
from app.services.flotherm_heat_balance import _faces, _labels, _points, _read, _read_bytes
from app.services.flotherm_surface_results import mesh_patches, scalar_field
from flotherm_channel_case_acceptance import load, source_xml, wsl
from flotherm_cli_acceptance import invoke
from flotherm_ecxml_acceptance import authenticated_case_api
from flotherm_paired_acceptance import numbers, pdml_fixture


TENSOR = (160,12,2)
AMBIENT = tuple(f'ambient_{axis}{side}' for axis in 'xyz' for side in ('Min','Max'))
OPEN = {'boundaries':{'mode':'open'}, 'openfoam':{'baseCellSize':.02,'endTime':2000}}
RAS = {'boundaries':{'mode':'channel','inletVelocity':1.2}, 'openfoam':{
    'baseCellSize':.04,'endTime':2200,'turbulence':'RAS'},
    'domain':[-.08,-.06,-.06,.32,.26,.26], 'ambient_temperature_k':300}


def orthotropic_xml(tensor=TENSOR, *, cold=False):
    vector = ''.join(f'<{axis}>{value}</{axis}>' for axis,value in zip('xyz',tensor))
    raw = source_xml().replace('<isotropic><conductivity>160</conductivity></isotropic>',
                               f'<orthotropic>{vector}</orthotropic>',1)
    return raw.replace('>0.3</powerDissipation>','>0</powerDissipation>' if cold else '>0.8</powerDissipation>').replace(
        '>0.2</powerDissipation>','>0</powerDissipation>' if cold else '>0.2</powerDissipation>')


def orthotropic_pdml():
    raw = pdml_fixture(power=.4)
    scalar = numbers(0x201B004,160)
    directional = b''.join(numbers(code,value) for code,value in zip((0x201B007,0x201B008,0x201B009),TENSOR))
    assert raw.count(scalar) == 1
    return raw.replace(scalar,directional,1)


def _mesh(case, region, digests):
    prefix = f'constant/{region}/polyMesh/'
    points = _points(_read(case,prefix+'points',digests))
    faces = _faces(_read(case,prefix+'faces',digests))
    owners = _labels(_read(case,prefix+'owner',digests),'owner')
    neighbors = _labels(_read(case,prefix+'neighbour',digests),'neighbour')
    patches = mesh_patches(_read(case,prefix+'boundary',digests),internal_faces=len(neighbors),total_faces=len(faces))
    return points, faces, owners, neighbors, patches


def _values(data, key, count):
    marker = rb'\b'+key+rb'\s+(?:uniform\s+([^;]+)|nonuniform\s+List<scalar>\s+(\d+)\s*\(([^)]*)\))\s*;'
    match = re.search(marker,data)
    assert match and (match[1] or int(match[2]) == count)
    values = [float(match[1])]*count if match[1] else list(map(float,match[3].split()))
    assert len(values) == count and all(map(math.isfinite,values))
    return values


def _face_direction(points, face):
    xyz = list(zip(*(points[index] for index in face)))
    axis, = [axis for axis,values in enumerate(xyz) if max(values) == min(values)]
    return axis, xyz[axis][0]


def _native_wall_report(case, moment, digests, owners, patches):
    report, reasons = _walls(case,digests,str(moment),'fluid','channelWallHeat','wallHeatFlux',
                             'constant/fluid/polyMesh/',f'{moment}/fluid/',patches,len(owners),max(owners)+1)
    assert not reasons
    return report['patches']


def verify_tensor(case, moment, manifest, relative_tolerance=.0001):
    digests = {}
    region, solid = next((name,row) for name,row in manifest['solid_regions'].items()
                         if isinstance(row['conductivity_w_mk'],list))
    assert solid['conductivity_w_mk'] == list(TENSOR)
    points, faces, owners, neighbors, patches = _mesh(case,region,digests)
    patch = patches[region+'_to_fluid']
    cell_points = [set() for _ in range(max(owners)+1)]
    for index, face in enumerate(faces):
        cell_points[owners[index]].update(face)
        if index < len(neighbors):
            cell_points[neighbors[index]].update(face)
    data = _read_bytes(case,f'{moment}/{region}/T',digests)
    temperatures = _values(data,b'internalField',len(cell_points))
    body = re.search(rb'\b'+(region+'_to_fluid').encode()+rb'\s*\{([^}]+)\}',data)[1]
    face_values = _values(body,b'value',patch.count)
    watts = [0.,0.,0.]
    gradient_area = [0.,0.,0.]
    for face_id, face_temp in zip(range(patch.first,patch.first+patch.count),face_values):
        face = faces[face_id]
        axis, coordinate = _face_direction(points,face)
        owner = owners[face_id]
        axis_values = [points[label][axis] for label in cell_points[owner]]
        center = (min(axis_values)+max(axis_values))/2
        derivative = (temperatures[owner]-face_temp)/abs(center-coordinate)
        gradient_area[axis] += derivative*_area(points,face)
        watts[axis] += derivative*_area(points,face)*TENSOR[axis]

    f_points,f_faces,f_owner,_,f_patches = _mesh(case,'fluid',digests)
    timing = f'{moment}/fluid/'
    native_walls = _native_wall_report(case,moment,digests,f_owner,f_patches)
    _, wall_values = scalar_field(_read(case,timing+'wallHeatFlux',digests),class_name=b'volScalarField',
                                  object_name=b'wallHeatFlux',patches=f_patches,internal_count=max(f_owner)+1)
    fluid_patch = 'fluid_to_'+region
    boundary = f_patches[fluid_patch]
    observed = [0.,0.,0.]
    for i, flux in zip(range(boundary.first,boundary.first+boundary.count),wall_values[fluid_patch]):
        face = f_faces[i]
        axis,_ = _face_direction(f_points,face)
        observed[axis] += flux*_area(f_points,face)
    assert all(x > 0 for x in observed)
    assert all(math.isclose(a,b,rel_tol=relative_tolerance,abs_tol=2e-7)
               for a,b in zip(watts,observed)), (watts,observed)
    assert math.isclose(sum(watts),solid['power_w'],rel_tol=.008), (watts,solid)
    return {'conductivity_w_mk':list(TENSOR), 'solid_gradient_recalculation_w':watts,
            'native_fluid_wall_by_axis_w':observed,'wall_heat_w':sum(row['integral_from_field_w'] for row in native_walls),
            'inferred_conductivity_w_mk':[value/gradient for value,gradient in zip(observed,gradient_area)],
            'relative_axis_tolerance':relative_tolerance,
            'files_sha256':digests}


def _open_flow(case, moment):
    digests = {}
    _, _, _, neighbors, patches = _mesh(case,'fluid',digests)
    assert all(patches[patch].kind == 'patch' for patch in AMBIENT)
    axes, fluxes = scalar_field(_read(case,f'{moment}/fluid/phi',digests),class_name=b'surfaceScalarField',
                                object_name=b'phi',patches=patches,internal_count=len(neighbors))
    assert axes == (1,0,-1,0,0,0,0)
    net = {patch:math.fsum(fluxes[patch]) for patch in AMBIENT}
    for patch,value in net.items():
        native = _latest_scalar(case,digests,'fluid',patch+'Flow',str(moment))
        assert math.isclose(value,native,rel_tol=1e-6,abs_tol=1e-11)
    speed = _read_bytes(case,f'{moment}/fluid/U',digests)
    center = re.search(rb'internalField\s+nonuniform\s+List<vector>\s+\d+\s*\(\s*((?:\([^)]*\)\s*)+)\)',speed)
    velocities = [math.hypot(*map(float,value.split())) for value in re.findall(rb'\(([^)]+)\)',center[1])] if center else []
    if not velocities:
        velocities = [math.hypot(*map(float,re.search(rb'internalField\s+uniform\s+\(([^)]+)\)',speed)[1].split()))]
    return {'outer_flux_kg_s':net,'mass_imbalance_kg_s':math.fsum(net.values()),
            'maximum_internal_velocity_m_s':max(velocities),'files_sha256':digests}


def build_and_solve(root, label, raw, config, suffix='ecxml'):
    source, options = root/f'{label}.{suffix}', root/f'{label}.json'
    source.write_bytes(raw)
    options.write_text(json.dumps(config),encoding='utf8')
    ir, case = root/(label+'-ir'),root/(label+'-case')
    model = invoke('import',source,'--config',options,'--output',ir)
    assert model['source']['sha256'] == hashlib.sha256(raw).hexdigest()
    assert invoke('build',ir,'--output',case)['runnable_preflight'] and invoke('check',case)['ok']
    wsl(case,'bash Allrun.serial > log.run 2>&1')
    assert load(case/'mesh_gate.json')['ok'] and invoke('check',case)['ok']
    return case, load(case/'case-manifest.json')


def verify(root):
    open_case, manifest = build_and_solve(root,'open',orthotropic_xml().encode(),OPEN)
    moment = OPEN['openfoam']['endTime']
    assert manifest['physics'] == {'boundary_mode':'open','turbulence':'laminar','gravity_m_s2':[0,-9.81,0]}
    assert len(manifest['solid_regions']) == 2
    plume = _open_flow(open_case,moment)
    magnitude = sum(abs(value) for value in plume['outer_flux_kg_s'].values())
    assert abs(plume['mass_imbalance_kg_s']) < max(1e-10,magnitude*.001), plume
    assert plume['outer_flux_kg_s']['ambient_yMax'] > 0 and plume['outer_flux_kg_s']['ambient_yMin'] < 0, plume
    assert .001 < plume['maximum_internal_velocity_m_s'] < 1.0, plume
    digests = plume['files_sha256']
    _, _, owners, _, patches = _mesh(open_case,'fluid',digests)
    walls = _native_wall_report(open_case,moment,digests,owners,patches)
    plume['wall_w'] = {row['name']:row['integral_from_field_w'] for row in walls}
    assert math.isclose(sum(plume['wall_w'].values()),1.,rel_tol=.008)
    assert manifest['solid_regions']['solid0']['conductivity_w_mk'] == list(TENSOR)

    cold_settings = {**OPEN,'openfoam':{**OPEN['openfoam'],'endTime':600}}
    cold_case, _ = build_and_solve(root,'open-equilibrium',orthotropic_xml(cold=True).encode(),cold_settings)
    equilibrium = _open_flow(cold_case,600)
    assert equilibrium['maximum_internal_velocity_m_s'] < .005, equilibrium

    ras_case, manifest = build_and_solve(root,'ras',orthotropic_pdml(),RAS,'pdml')
    moment = RAS['openfoam']['endTime']
    raw = (ras_case/'log.run').read_text(encoding='utf8')
    assert 'Selecting RAS turbulence model realizableKE' in raw
    assert 'Solving for epsilon' in raw and 'Solving for k' in raw and 'End' in raw
    assert manifest['physics']['turbulence'] == 'realizableKE'
    cell_count = manifest['mesh_cells']['fluid']
    turbulence = {field:_values((ras_case/f'{moment}/fluid/{field}').read_bytes(),b'internalField',cell_count)
                  for field in ('nut','k','epsilon')}
    assert max(turbulence['nut']) > 1e-6
    assert min(turbulence['k']) > 0 and min(turbulence['epsilon']) > 0
    flow = invoke('flow',ras_case,'--region','fluid','--wall-heat-flux','channelWallHeat')
    assert flow['inlet']['inward'] > .1
    ras_tensor = verify_tensor(ras_case,moment,manifest,relative_tolerance=.002)

    source = root/'invalid.ecxml'
    source.write_text(orthotropic_xml((160,12,0)),encoding='utf8')
    assert '必须为正' in invoke('import',source,'--output',root/'invalid-ir',status=2,returns_json=False).stderr
    config = root/'invalid.json'
    config.write_text(json.dumps({**OPEN,'openfoam':{**OPEN['openfoam'],'monitorChannelFlow':True}}),encoding='utf8')
    rejected = invoke('import',root/'open.ecxml','--config',config,'--output',root/'bad-config',status=2,returns_json=False)
    assert '没有固定入口和出口' in rejected.stderr
    return {'open_plume':plume,'open_equilibrium':equilibrium,'ras_tensor':ras_tensor,
            'ras_mass_inlet_kg_s':flow['inlet']['inward'],'ras_max_eddy_viscosity_m2_s':max(turbulence['nut']),
            'invalid_tensor_rejected':True,'channel_monitor_rejected_in_open_mode':True}


def verify_s3():
    with authenticated_case_api('flotherm-physics-s3') as api:
        api.create_case('FloTHERM open anisotropic case')
        path = 'Ref/open.ecxml'
        raw = orthotropic_xml()
        api.put(path,raw)
        imported = api.call('POST',api.prefix+'/flotherm:import',{'path':path,
            'expected_sha256':hashlib.sha256(raw.encode()).hexdigest()})
        api.put('Ref/model.json',imported['content'])
        api.put('Ref/open.json',json.dumps(OPEN))
        job = api.call('POST',api.prefix+'/flotherm:convert',{'ir_path':'Ref/model.json',
                       'settings_path':'Ref/open.json','output_dir':'C-run/open'})['job_id']
        result = api.wait_job(job,timeout=100)
        assert result['status'] == 'succeeded' and not result['completion_pending'], result
        manifest = json.loads(api.call('GET',api.prefix+'/file?path=C-run/open/case-manifest.json')['content'])
        assert manifest['physics']['boundary_mode'] == 'open'
        assert manifest['solid_regions']['solid0']['conductivity_w_mk'] == list(TENSOR)
        return {'job':job,'evidence':str(api.root)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--s3',action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='flotherm-physics-acceptance-',dir=args.report.resolve().parent))
    evidence = {'passed':False,'root':str(root),'actual_model_calls':0,'simulated_model_calls':0,'wsl_uid':65534}
    try:
        evidence.update(verify(root))
        if args.s3:
            evidence['authenticated_s3'] = verify_s3()
        evidence['passed'] = True
    finally:
        args.report.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('FLOTHERM_PHYSICS_PASS',args.report)


if __name__ == '__main__':
    main()
