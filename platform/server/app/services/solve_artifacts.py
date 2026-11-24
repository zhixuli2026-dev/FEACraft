"""Pure serial OpenFOAM configuration and evidence checks."""
import gzip
import json
import math
from pathlib import PurePosixPath
import re

from . import case_materials, case_physical_frames, openfoam_acoustic_medium, openfoam_boundary_inputs, openfoam_cht, openfoam_cht_materials, openfoam_conduction_material, openfoam_deformation, openfoam_dynamic_refinement, openfoam_equations, openfoam_fluid_model, openfoam_fluid_thermophysics, openfoam_overset, openfoam_pressure_reference, openfoam_reference_temperature, openfoam_rotation
from .case_evidence import (capture_solve_inputs, complete_solve_evidence,
    is_run_input, run_inventory, safe_run_dir)
from .case_boundary_inputs import active_material_names, material_for_active_load
from .casedict import parse_casedict
from .casedict_schema import finite_vector, linear_motion_velocity, porous_coefficients, valid_cell_zone
from . import openfoam_structural_feedback
from .logparse import parse_solver_log
from .udf_control import _TOKENS

EVIDENCE='solve-evidence.json'
TOOLCHAIN='solve-toolchain.json'
LOG='solve.log'
INVOCATION='solve-invocation.json'


def requested_gravity(case_dictionary):
    case=parse_casedict(case_dictionary)
    problem=case.get('physicalProblem')
    if not isinstance(problem, dict) or 'gravityEnabled' not in problem:
        return None
    enabled = str(problem['gravityEnabled']).lower()
    if enabled not in {'true', 'false'}:
        raise ValueError('physicalProblem.gravityEnabled must be true or false')
    if enabled=='false':
        return (0.,0.,0.)
    from .case_physical_frames import global_gravity
    return global_gravity(problem,case.get('geometry'))


def gravity_dictionary(gravity):
    vector = ' '.join(repr(component) for component in gravity)
    return (
        'FoamFile\n{\n    version 2.0;\n    format ascii;\n'
        '    class uniformDimensionedVectorField;\n    object g;\n}\n'
        f'dimensions [0 1 -2 0 0 0 0];\nvalue ({vector});\n'
    ).encode('ascii')


def _native_dictionary(path):
    compressed = path.with_name(path.name+'.gz')
    if not path.is_file() and not compressed.is_file():
        return None
    stream = path.open('rt', encoding='utf-8') if path.is_file() else gzip.open(compressed, 'rt', encoding='utf-8')
    with stream:
        text = stream.read(64 * 1024 + 1)
    if len(text) > 64 * 1024:
        raise ValueError('OpenFOAM '+path.name+' exceeds its dictionary size limit')
    return parse_casedict(text)


def gravity_input(root, run_dir):
    gravity = requested_gravity((root/'0-caseDict/caseDict').read_text(encoding='utf-8'))
    if gravity is None:
        return {}
    path = run_dir + '/constant/g'
    current = _native_dictionary(root/path)
    if current is not None:
        if finite_vector(current.get('value')) != gravity:
            raise ValueError('OpenFOAM constant/g differs from physicalProblem.gravityVector')
        return {}
    return {path: gravity_dictionary(gravity)}


def requested_viscosity(case_dictionary):
    return declared_viscosity(parse_casedict(case_dictionary))


def declared_viscosity(parsed):
    active = active_material_names(parsed)
    regions = parsed.get('regions', {})
    fluid = [region.get('material') for region in regions.values()
             if isinstance(region, dict) and region.get('role') == 'fluidDomain'
             and (active or 'material' in region)] if isinstance(regions, dict) else []
    if not fluid:
        if active:
            raise ValueError('icoFoam active material requires a single declared fluidDomain')
        return None
    if len(fluid) > 1 or (not active and not isinstance(fluid[0], str)):
        raise ValueError('icoFoam requires a single named fluid-domain material')
    name = material_for_active_load(parsed, fluid[0])
    materials = parsed.get('materials', {})
    entries = materials.get('entries') if isinstance(materials, dict) else None
    material = entries.get(name) if isinstance(entries, dict) else None
    if not isinstance(material, dict):
        raise ValueError('The fluid-domain material is missing from materials.entries: '+name)
    if active and (material.get('role') != 'fluid' or str(material.get('confirmed')).lower() != 'true'):
        raise ValueError('icoFoam active material must be a confirmed fluid: '+name)
    if not {'density', 'viscosity'}.intersection(material):
        if active:
            raise ValueError('icoFoam active material requires density and dynamic viscosity: '+name)
        return None
    if str(material.get('confirmed')).lower() == 'false':
        raise ValueError('Confirm the fluid-domain material before solving: '+name)
    density = case_materials.quantity_si('density', material.get('density'))
    viscosity = case_materials.quantity_si('viscosity', material.get('viscosity'))
    result = viscosity / density
    if not math.isfinite(result) or result <= 0:
        raise ValueError('icoFoam requires finite positive kinematic viscosity')
    return result


def transport_dictionary(viscosity):
    return (
        'FoamFile\n{\n    version 2.0;\n    format ascii;\n'
        '    class dictionary;\n    object transportProperties;\n}\n'
        'transportModel Newtonian;\n'
        f'nu [0 2 -1 0 0 0 0] {viscosity!r};\n'
    ).encode('ascii')


def transport_input(root, run_dir):
    viscosity = requested_viscosity((root/'0-caseDict/caseDict').read_text(encoding='utf-8'))
    if viscosity is None:
        return {}
    path = run_dir+'/constant/transportProperties'
    current = _native_dictionary(root/path)
    if current is not None:
        entry = current.get('nu')
        match = re.fullmatch(r'(?:nu\s+)?\[\s*0\s+2\s+-1\s+0\s+0\s+0\s+0\s*\]\s+(\S+)',
                             ' '.join(entry) if isinstance(entry, list) else '')
        if match is None or not math.isclose(float(match[1]), viscosity, rel_tol=1e-9):
            raise ValueError('OpenFOAM transportProperties.nu differs from the fluid-domain material')
        return {}
    return {path: transport_dictionary(viscosity)}


def requested_porosity(case_dictionary):
    regions = parse_casedict(case_dictionary).get('regions')
    if not isinstance(regions, dict):
        return []
    sources = []
    for name, region in sorted(regions.items()):
        if not isinstance(region, dict) or 'porousResistance' not in region:
            continue
        zone = region.get('cellZone')
        if str(region.get('porous')).lower() != 'true' or not valid_cell_zone(zone):
            raise ValueError('Porous region needs a valid declared cellZone: '+name)
        sources.append({'source':f'vibeflowPorous{len(sources)+1}', 'model':'DarcyForchheimer',
                        'cell_zone':zone, 'coefficients':porous_coefficients(region['porousResistance'])})
    if len({source['cell_zone'] for source in sources}) != len(sources):
        raise ValueError('Each Darcy-Forchheimer declaration must target a different cellZone')
    return sources


def porosity_dictionary(sources):
    from .flotherm_flow_devices import source_entries
    header = 'FoamFile\n{\n    version 2.0;\n    format ascii;\n    class dictionary;\n    object fvOptions;\n}\n'
    return (header+source_entries(sources)+'\n').encode('ascii')


def porosity_input(root, run_dir, solver):
    sources = requested_porosity((root/'0-caseDict/caseDict').read_text(encoding='utf-8'))
    if not sources:
        return {}
    if solver != 'pimpleFoam':
        raise ValueError('Managed Darcy-Forchheimer resistance currently requires pimpleFoam')
    path = run_dir+'/constant/fvOptions'
    body = porosity_dictionary(sources)
    current = _native_dictionary(root/path)
    if current is None:
        current = _native_dictionary(root/run_dir/'system/fvOptions')
    if current is None:
        return {path:body}
    existing = {}
    for option in current.values():
        if not isinstance(option, dict) or option.get('type') != 'explicitPorositySource':
            continue
        values = option.get('explicitPorositySourceCoeffs')
        if isinstance(values, dict) and isinstance(values.get('cellZone'), str):
            existing.setdefault(values['cellZone'], []).append((option, values))
    for row in sources:
        matches = existing.get(row['cell_zone'], [])
        if len(matches) != 1:
            raise ValueError('Existing fvOptions must have one explicit porosity source for '+row['cell_zone'])
        option, values = matches[0]
        axes = values.get('coordinateSystem')
        if (str(option.get('active', 'true')).lower() not in {'true','yes','on','1'}
                or values.get('selectionMode') != 'cellZone' or values.get('type') != 'DarcyForchheimer'
                or porous_coefficients(values) != row['coefficients'] or not isinstance(axes, dict)
                or set(axes) - {'type','origin','e1','e2'} or axes.get('type', 'cartesian') != 'cartesian'
                or [finite_vector(axes.get(key)) for key in ('origin','e1','e2')] != [(0.,0.,0.),(1.,0.,0.),(0.,1.,0.)]):
            raise ValueError('Existing fvOptions differs from the configured Darcy-Forchheimer source')
    return {}


def requested_motion(case_dictionary):
    return _region_motion(parse_casedict(case_dictionary))


def _region_motion(case):
    moving = case_physical_frames.moving_region(case.get('physicalProblem'))
    regions = case.get('regions')
    regions = regions if isinstance(regions, dict) else {}
    motions = [(name,region) for name,region in regions.items() if isinstance(region,dict) and 'motion' in region]
    if moving is not None and not any(name == moving for name, _ in motions):
        raise ValueError('movingFrame.region 必须引用已声明运动的区域：'+moving)
    if not motions:
        return None
    if len(motions) != 1:
        raise ValueError('Managed OpenFOAM linear motion supports one moving cellZone')
    name,region = motions[0]
    zone = region.get('cellZone')
    if str(region.get('moving')).lower() != 'true' or not valid_cell_zone(zone):
        raise ValueError('Moving region needs a valid declared cellZone: '+name)
    return zone,linear_motion_velocity(region['motion'])


def motion_dictionary(motion):
    zone,velocity = motion
    components = ' '.join(map(repr,velocity))
    return (
        'FoamFile\n{\n    version 2.0;\n    format ascii;\n    class dictionary;\n    object dynamicMeshDict;\n}\n'
        'dynamicFvMesh dynamicMotionSolverFvMesh;\nmotionSolverLibs (fvMotionSolvers);\nmotionSolver solidBody;\n'
        f'cellZone {zone};\nsolidBodyMotionFunction linearMotion;\nvelocity ({components});\n'
    ).encode('ascii')


def motion_input(root, run_dir, solver, *, require_native=False):
    motion = requested_motion((root/'0-caseDict/caseDict').read_text(encoding='utf-8'))
    if motion is None:
        return {}
    moving_overset = solver == openfoam_overset.SOLVER
    if solver != 'pimpleFoam' and not moving_overset:
        raise ValueError('Managed linear mesh motion requires pimpleFoam or overLaplacianDyMFoam')
    path = run_dir+'/constant/dynamicMeshDict'
    current = openfoam_boundary_inputs._existing(root,path)
    if current is not None:
        expected = parse_casedict(motion_dictionary(motion).decode('ascii'))
        if moving_overset:
            expected['dynamicFvMesh'] = openfoam_overset.MOTION_MESH
        names = ('dynamicFvMesh','motionSolver','cellZone','solidBodyMotionFunction')
        if any(current.get(key) != expected[key] for key in names) or finite_vector(current.get('velocity')) != motion[1]:
            raise ValueError('Existing dynamicMeshDict differs from the declared OpenFOAM linear motion')
        return {}
    if require_native or moving_overset:
        raise ValueError('封存运动坐标缺少 dynamicMeshDict')
    return {path:motion_dictionary(motion)}


def control_values(text, *, extra=()):
    """Read literal top-level controls without rewriting native dictionaries."""
    keys={'application','startFrom','startTime','stopAt','endTime','deltaT','timePrecision',*extra}
    tokens=[m.group() for m in _TOKENS.finditer(text) if not m.group().startswith(('//','/*'))]
    result,depth,index={},0,0
    while index<len(tokens):
        token=tokens[index]
        if token=='{':
            depth+=1
        elif token=='}':
            depth-=1
        elif depth==0 and token in keys:
            if token in result or index+2>=len(tokens) or tokens[index+2]!=';':
                raise ValueError('Solver controls must be unique explicit scalar entries: '+token)
            result[token]=tokens[index+1].strip('"')
            index+=2
        index+=1
        if depth<0:
            raise ValueError('Unbalanced controlDict')
    if depth:
        raise ValueError('Unbalanced controlDict')
    return result


def configuration(root,run_dir,*,resume_time=None,require_initial=True):
    run_dir=safe_run_dir(run_dir)
    from .case_numerical_method import declared_time_keys
    from .openfoam_numerical_inputs import declared_configuration
    dictionary=(root/'0-caseDict/caseDict').read_text(encoding='utf-8')
    case=parse_casedict(dictionary)
    method=case.get('numericalMethod') or {}
    control=(root/run_dir/'system/controlDict').read_text(encoding='utf-8')
    cfg=control_values(control,extra=declared_time_keys(method))
    solver=cfg.get('application','')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*Foam',solver):
        raise ValueError('application must name a native OpenFOAM solver')
    selected=method.get('solver')
    if selected and selected!=solver:
        raise ValueError('caseDict numericalMethod.solver differs from controlDict.application')
    try:
        start=float(cfg.get('startTime','0'))
        end,delta=float(cfg['endTime']),float(cfg['deltaT'])
        precision=int(cfg.get('timePrecision','6'))
    except (KeyError,TypeError,ValueError) as error:
        raise ValueError('Solver time controls are missing or invalid') from error
    expected_start=0 if resume_time is None else float(resume_time)
    if (cfg.get('startFrom','startTime')!='startTime' or start!=expected_start or
            cfg.get('stopAt','endTime')!='endTime'):
        raise ValueError('Serial solver controls differ from the explicit fresh/resume start time')
    if not all(math.isfinite(value) for value in (start,end,delta)) or end<=start or delta<=0 or not 1<=precision<=16:
        raise ValueError('Solver time controls must be finite and positive')
    if require_initial and not has_initial_fields(root,run_dir):
        raise ValueError('Fresh solve requires an explicit nonempty 0 directory; 0.orig is preserved')
    configuration={'solver':solver,'start_time':start,'end_time':end,'delta_t':delta,'time_precision':precision}
    equations=openfoam_equations.configuration(case,solver)
    if equations is not None:
        configuration['continuum']=equations
    conduction=openfoam_conduction_material.declared(case)
    if conduction is not None:
        if solver!=openfoam_conduction_material.SOLVER:
            raise ValueError('声明的材料导热需要托管单区域 laplacianFoam')
        configuration['conduction_material']=conduction
    medium=openfoam_acoustic_medium.declared(case)
    if medium is None:
        diffusivity=conduction['diffusivity_m2_s'] if conduction is not None else None
        configuration.update(openfoam_boundary_inputs.native_configuration(root,run_dir,case,solver,
            thermal_diffusivity=diffusivity))
    else:
        if solver!='acousticFoam':
            raise ValueError('声明的压力波声学介质需要 acousticFoam')
        configuration.update(acoustic_medium=medium,acoustic_density_kg_m3=medium['density_kg_m3'])
    reference_temperature=openfoam_reference_temperature.declared(case)
    if reference_temperature is not None:
        configuration['buoyancy_reference_temperature_K']=reference_temperature
    reference_pressure=openfoam_pressure_reference.declared(case)
    if reference_pressure is not None:
        configuration['pressure_reference']=reference_pressure
    flow=openfoam_fluid_model.declared(case)
    if flow is not None:
        configuration['fluid_model']=flow
    thermophysics=openfoam_fluid_thermophysics.declared(case)
    if thermophysics is not None:
        configuration['fluid_thermophysics']=thermophysics
    refinement=openfoam_dynamic_refinement.declared(case.get('mesh'))
    if refinement is not None:
        if resume_time is not None:
            raise ValueError('Dynamic refinement currently needs a fresh phase-front solve')
        configuration['dynamic_refinement']=refinement
    rotation=openfoam_rotation.declared(case)
    if rotation is not None:
        configuration['rotation']=rotation
    moving=case_physical_frames.moving_region(case.get('physicalProblem'))
    if moving is not None:
        zone, velocity = _region_motion(case)
        configuration['moving_frame']={'region':moving,'cell_zone':zone,'velocity_m_s':list(velocity)}
    deformation=openfoam_deformation.declared(case)
    if deformation is not None:
        if _region_motion(case) is not None:
            raise ValueError('同一算例只能声明 Laplacian 网格变形或刚体线性运动')
        configuration['deformation']=deformation
    feedback=openfoam_structural_feedback.selected(case)
    if feedback is not None:
        if (solver!=openfoam_deformation.SOLVER or any(value is not None for value in (resume_time,deformation,rotation))
                or _region_motion(case) is not None):
            raise ValueError('结构位移反馈需要从初始时刻串行运行 pimpleFoam，且只能使用一套网格运动设置')
        precision=int(control_values(control,extra=('writePrecision',)).get('writePrecision','6'))
        if not 1<=precision<=32:
            raise ValueError('结构反馈需要可核验的网格点写出精度')
        configuration['structural_feedback']={**feedback,'write_precision':precision}
    configuration.update(declared_configuration(root,run_dir,solver,method,cfg))
    try:
        motion=_region_motion(case) if solver==openfoam_overset.SOLVER else None
        overset=openfoam_overset.configuration(root,run_dir,solver,case.get('interfaces'),motion=motion)
        if motion is not None:
            motion_input(root,run_dir,solver,require_native=True)
            write_precision=int(control_values(control,extra=('writePrecision',)).get('writePrecision','6'))
            if write_precision < 1:
                raise ValueError('Overset 运动要求可核验的原生 points 写出精度')
            overset['motion']['write_precision']=write_precision
    except (OSError,ValueError) as error:
        raise openfoam_overset.OversetInputError(str(error)) from error
    if overset is not None:
        configuration['overset']=overset
    if openfoam_cht_materials.selection(case) and solver != openfoam_cht.SOLVER:
        raise ValueError('区域材料共轭传热需要 chtMultiRegionFoam')
    if solver == openfoam_cht.SOLVER:
        if (case.get('mesh') or {}).get('source') != openfoam_cht.MESH_SOURCE:
            raise ValueError('Managed CHT requires verified imported OpenFOAM region meshes')
        native=(root/run_dir/'constant/regionProperties').read_text(encoding='utf8')
        regions=openfoam_cht.case_regions(dictionary,native)
        materials=openfoam_cht_materials.declared(case,regions)
        openfoam_cht.require_initial_fields(root,run_dir,regions,allow_generated_materials=materials is not None)
        configuration['regions']=regions
        if materials is not None:
            configuration['cht_materials']=materials
    return configuration


def has_initial_fields(root,run_dir):
    directory = root/run_dir/'0'
    return directory.is_dir() and any(directory.iterdir())


def initial_snapshot(root,run_dir,execution_id,*,resume_time=None):
    cfg=configuration(root,run_dir,resume_time=resume_time)
    if 'overset' in cfg:
        from .mesh_artifacts import boundaries
        patches=boundaries(root/run_dir/'constant/polyMesh/boundary')
        openfoam_overset.mesh_bindings(cfg['overset']['interfaces'],patches)
    openfoam_reference_temperature.prepare(root,run_dir,cfg['solver'],cfg.get('buoyancy_reference_temperature_K'),require_native=True)
    if 'pressure_reference' in cfg:
        openfoam_pressure_reference.prepare(root,run_dir,cfg['solver'],cfg['pressure_reference'],require_native=True)
        openfoam_pressure_reference.pressure_field(root,run_dir,required=True)
    if openfoam_acoustic_medium.prepare(root,run_dir,cfg.get('acoustic_medium')):
        raise ValueError('封存的 acousticFoam 输入缺少声学介质')
    openfoam_conduction_material.prepare(root,run_dir,cfg['solver'],cfg.get('conduction_material'),require_native=True)
    openfoam_fluid_model.prepare(root,run_dir,cfg['solver'],cfg.get('fluid_model'),require_native=True)
    openfoam_fluid_thermophysics.prepare(root,run_dir,cfg['solver'],cfg.get('fluid_thermophysics'),require_native=True)
    openfoam_dynamic_refinement.prepare(root,run_dir,cfg['solver'],cfg.get('dynamic_refinement'),require_native=True)
    openfoam_rotation.prepare(root,run_dir,cfg['solver'],cfg.get('rotation'),require_native=True)
    if 'moving_frame' in cfg:
        motion_input(root,run_dir,cfg['solver'],require_native=True)
    openfoam_deformation.prepare(root,run_dir,cfg['solver'],cfg.get('deformation'),require_native=True)
    if 'structural_feedback' in cfg:
        openfoam_structural_feedback.verify(root,run_dir,cfg['structural_feedback'])
    openfoam_cht_materials.prepare(root,run_dir,cfg.get('cht_materials'),require_native=True)
    snapshot=capture_solve_inputs(root,run_dir,0,regions=cfg.get('regions',()))
    snapshot.pop('job_id')
    return {**snapshot,'schema':'vibeflow.solve-result.v1','execution_id':execution_id,
        'configuration':cfg}


def result(root,snapshot):
    from .native_openfoam_residuals import has_nonfinite_telemetry
    from .openfoam_numerical_inputs import observed_configuration
    run_dir=snapshot['run_dir']
    evidence=complete_solve_evidence(root,snapshot,root/run_dir/LOG)
    cfg=snapshot['configuration']
    if configuration(root,run_dir,resume_time=cfg['start_time'] if cfg['start_time'] else None)!=cfg:
        raise ValueError('Solver controls changed during execution')
    log=(root/run_dir/LOG).read_text(encoding='utf-8',errors='replace')
    if has_nonfinite_telemetry(log):
        raise ValueError('Solver log contains non-finite residuals or times')
    parsed=parse_solver_log(log)
    times=parsed['times']
    residuals=[point[key] for series in parsed['residuals_detail'].values() for point in series for key in ('initial','final')]
    if (not times or times[0]<=cfg['start_time'] or any(not math.isfinite(value) for value in times+residuals) or
            any(value<0 for value in residuals) or any(a>b for a,b in zip(times,times[1:]))):
        raise ValueError('Solver times/residuals are invalid or non-finite')
    last=times[-1]
    evidence.update(observed_configuration(cfg,log,parsed['residuals_detail']))
    if 'continuum' in cfg:
        evidence['observed_equations']=openfoam_equations.observed(cfg['solver'],cfg['continuum'],log)
    if 'fluid_model' in cfg:
        evidence['observed_fluid_model']=openfoam_fluid_model.observed(cfg['solver'],cfg['fluid_model'],log)
    if 'fluid_thermophysics' in cfg:
        evidence['observed_fluid_thermophysics']=openfoam_fluid_thermophysics.observed(cfg['fluid_thermophysics'],log)
    if 'dynamic_refinement' in cfg:
        labels=group_output_paths([record['path'] for record in evidence['outputs']],run_dir)
        evidence['dynamic_refinement']=openfoam_dynamic_refinement.observed(cfg['dynamic_refinement'],log,labels)
    if 'rotation' in cfg:
        evidence['observed_rotation']=openfoam_rotation.observed(cfg['rotation'],log)
    if 'moving_frame' in cfg:
        zone=cfg['moving_frame']['cell_zone']
        if 'Applying motion to cellZone: '+zone not in log or 'linearMotion' not in log:
            raise ValueError('原生日志未证明声明的线性运动区域')
        evidence['observed_moving_frame']={'cell_zone':zone,'method':'linearMotion'}
    if 'deformation' in cfg:
        evidence['observed_deformation']=openfoam_deformation.observed(cfg['deformation'],log)
    if 'structural_feedback' in cfg:
        labels=group_output_paths([record['path'] for record in evidence['outputs']],run_dir)
        feedback=cfg['structural_feedback']
        evidence['structural_feedback']=openfoam_structural_feedback.observed(
            root,run_dir,feedback,max(labels,key=float),log,feedback['write_precision'])
    if 'overset' in cfg:
        evidence['overset']=openfoam_overset.observed(cfg['overset'],log)
        motion=cfg['overset'].get('motion')
        if motion:
            labels=group_output_paths([record['path'] for record in evidence['outputs']],run_dir)
            label=max(labels,key=float)
            evidence['overset']['motion']=openfoam_overset.observed_motion(
                motion,log,root/run_dir,label,write_precision=motion['write_precision'])
    final_step=evidence.get('adaptive_time_steps',{}).get('last_s',cfg['delta_t'])
    tolerance=max(1e-9,0.501*final_step,10**(-cfg['time_precision'])*max(1,abs(cfg['end_time'])))
    completion='end_time_reached'
    if not math.isclose(last,cfg['end_time'],rel_tol=0,abs_tol=tolerance):
        solution=parse_casedict((root/run_dir/'system/fvSolution').read_text(encoding='utf-8'))
        convergence=any(isinstance(solution.get(name),dict) and solution[name].get('residualControl')
            for name in ('SIMPLE','PIMPLE','PISO'))
        if (not convergence or not re.search(r'solution converged in\s+\d+\s+iterations',log,re.I) or
                not cfg['start_time']<last<cfg['end_time']):
            raise ValueError('Solver ended before the requested end time without convergence evidence')
        completion='residual_control_converged'
    evidence['completion_reason']=completion
    evidence['parsed_log']=json.loads(json.dumps(parsed,allow_nan=False))
    return evidence


def group_output_paths(paths, run_dir):
    run_dir = safe_run_dir(run_dir)
    values = {}
    for path in paths:
        if not is_run_input(path, run_dir) or is_run_input(path, run_dir, results=False):
            continue
        label = PurePosixPath(path).relative_to(run_dir).parts[0]
        numeric = float(label)
        if numeric <= 0 or not math.isfinite(numeric):
            raise ValueError('Fresh solver produced an invalid output time')
        values.setdefault(label, []).append(path)
    numbers = [float(label) for label in values]
    if len(numbers) != len(set(numbers)):
        raise ValueError('Different output directories represent the same time')
    return values


def output_times(root,run_dir):
    files = {path.relative_to(root).as_posix(): path for path in run_inventory(root, run_dir)}
    return {label: [files[path] for path in paths]
            for label, paths in group_output_paths(files, run_dir).items()}


def field_header(path):
    opener=gzip.open if path.suffix=='.gz' else open
    with opener(path,'rb') as stream:
        text=stream.read(8192).decode('utf-8','replace')
    match=re.search(r'FoamFile\s*\{([^}]+)\}',text,re.S)
    if not match:
        raise ValueError('Field has no OpenFOAM header: '+path.name)
    header=parse_casedict(match[1])
    if header.get('format') not in {'ascii','binary'}:
        raise ValueError('Unsupported field format')
    return header


def managed(path,run_dir):
    return path in {run_dir+'/'+EVIDENCE,run_dir+'/'+TOOLCHAIN,run_dir+'/'+LOG,run_dir+'/'+INVOCATION,run_dir+'/resume-checkpoint.json',
        run_dir+'/'+PurePosixPath(run_dir).name+'.foam'} or (
        is_run_input(path,run_dir) and not is_run_input(path,run_dir,results=False))
