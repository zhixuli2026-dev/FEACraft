"""Verify actual solver fields and publish only this attempt's result version."""
import asyncio
import gzip
import hashlib
import json
import math
from pathlib import Path
import re

from .. import storage
from . import attempt_workspace as workspace
from . import openfoam_source_runner
from . import openfoam_acoustic_medium,openfoam_boundary_inputs,openfoam_cht,openfoam_cht_materials,openfoam_conduction_material,openfoam_deformation,openfoam_dynamic_refinement,openfoam_fluid_model,openfoam_fluid_thermophysics,openfoam_numerical_inputs,openfoam_reference_temperature,openfoam_rotation
from .async_ops import finish_before_cancel
from .case_evidence import case_input_fingerprint, safe_run_dir, verify_records
from .mesh_publication import source_snapshot
from .solve_artifacts import (EVIDENCE, TOOLCHAIN, LOG, INVOCATION, field_header, group_output_paths,
    gravity_dictionary, initial_snapshot, managed, motion_dictionary, output_times, porosity_dictionary, requested_gravity,
    requested_motion, requested_porosity, requested_viscosity, result, transport_dictionary)

_DERIVED_INPUTS = (
    ('generated_gravity', ('constant/g',), requested_gravity, gravity_dictionary, '重力'),
    ('generated_transport', ('constant/transportProperties',), requested_viscosity, transport_dictionary, '材料物性'),
    ('generated_porosity', ('constant/fvOptions','system/fvOptions'), requested_porosity, porosity_dictionary, '多孔阻力'),
    ('generated_motion', ('constant/dynamicMeshDict',), requested_motion, motion_dictionary, '网格运动'),
    ('generated_acoustic_medium',(openfoam_acoustic_medium.PATH,),openfoam_acoustic_medium.requested,
     openfoam_acoustic_medium.dictionary,'声学介质'),
    ('generated_conduction_material',(openfoam_conduction_material.PATH,),openfoam_conduction_material.requested,
     openfoam_conduction_material.dictionary,'导热材料'),
    ('generated_fluid_model',(openfoam_fluid_model.PATH,),openfoam_fluid_model.requested,
     openfoam_fluid_model.dictionary,'流动模型'),
    ('generated_perfect_gas',(openfoam_fluid_thermophysics.GAS_PATH,),
     lambda text: openfoam_fluid_thermophysics.requested(text,'perfectGas'),openfoam_fluid_thermophysics.dictionary,'理想气体材料'),
    ('generated_vof',(openfoam_fluid_thermophysics.VOF_PATH,),
     lambda text: openfoam_fluid_thermophysics.requested(text,'VOF'),openfoam_fluid_thermophysics.dictionary,'双相流体材料'),
    ('generated_rotation',(openfoam_rotation.PATH,),openfoam_rotation.requested,
     openfoam_rotation.dictionary,'旋转坐标'),
    ('generated_deformation',(openfoam_deformation.DYNAMIC,),openfoam_deformation.requested,
     openfoam_deformation.dynamic_dictionary,'变形网格'),
    ('generated_dynamic_refinement',(openfoam_dynamic_refinement.PATH,),openfoam_dynamic_refinement.requested,
     openfoam_dynamic_refinement.dictionary,'相界面动态加密'),
    ('generated_mesh_displacement',(openfoam_deformation.DISPLACEMENT,),openfoam_deformation.requested,
     openfoam_deformation.displacement_dictionary,'网格点位移'),
    ('generated_numerical',(openfoam_numerical_inputs.SOLUTION,),openfoam_numerical_inputs.requested_linear_solvers,
     openfoam_numerical_inputs.solution_dictionary,'线性求解器'),
)
_OVERLAID_INPUTS = (
    ('generated_reference_temperature',openfoam_reference_temperature.TRANSPORT,'浮力参考温度'),
    ('generated_reference_pressure',(openfoam_numerical_inputs.SOLUTION,),'压力参考值'),
)
_STRUCTURAL_FEEDBACK_FILES=(openfoam_deformation.DYNAMIC,openfoam_deformation.DISPLACEMENT)


def _validate_scalar_history(path, header, cells, dimensions):
    """Check a native old-time scalar field that VTK omits from its arrays."""
    opener = gzip.open if path.suffix == '.gz' else open
    bound = 16 * 1024 * 1024
    with opener(path, 'rb') as stream:
        data = stream.read(bound + 1)
    if len(data) > bound or header['class'] != 'volScalarField':
        raise ValueError('Invalid OpenFOAM scalar history size or class: '+path.name)
    pattern = rb'\bdimensions\s*\[\s*'+rb'\s+'.join(dimensions.split())+rb'\s*\]\s*;'
    if not re.search(pattern, data):
        raise ValueError('OpenFOAM scalar history has incompatible dimensions: '+path.name)
    uniform = re.search(rb'\binternalField\s+uniform\s+([^;]+);', data)
    if uniform:
        values = [float(uniform[1])]
    else:
        listing = re.search(rb'\binternalField\s+nonuniform\s+List<scalar>\s+(\d+)\s*\(', data)
        if not listing or int(listing[1]) != cells:
            raise ValueError('OpenFOAM scalar history has an incompatible cell count: '+path.name)
        if header['format'] == 'binary':
            import numpy as np
            architecture = re.fullmatch(r'(LSB|MSB);label=(?:32|64);scalar=(32|64)', header.get('arch', ''))
            if architecture is None:
                raise ValueError('Unsupported binary scalar history architecture: '+path.name)
            dtype = ('<' if architecture[1] == 'LSB' else '>') + ('f4' if architecture[2] == '32' else 'f8')
            end = listing.end() + cells * np.dtype(dtype).itemsize
            if not re.match(rb'\s*\)\s*;', data[end:]):
                raise ValueError('Malformed binary scalar history: '+path.name)
            values = np.frombuffer(data, dtype=dtype, count=cells, offset=listing.end())
        else:
            payload = re.match(rb'([^()]*)\)\s*;', data[listing.end():])
            if payload is None:
                raise ValueError('Malformed scalar history: '+path.name)
            values = [float(item) for item in payload[1].split()]
            if len(values) != cells:
                raise ValueError('Acoustic pressure history has an incompatible cell count')
    if not all(math.isfinite(value) for value in values):
        raise ValueError('OpenFOAM scalar history contains non-finite values: '+path.name)


def validate_fields(root,run_dir,evidence,*,times=None):
    import numpy as np
    import vtk
    from vtk.util.numpy_support import vtk_to_numpy
    times=output_times(root,run_dir) if times is None else times
    if not times:
        raise ValueError('Solver produced no output time directories')
    marker=workspace.checked_path(root,run_dir+'/'+run_dir.rsplit('/',1)[-1]+'.foam')
    reader=vtk.vtkOpenFOAMReader()
    errors=[]
    reader.AddObserver('ErrorEvent',lambda *_:errors.append('OpenFOAM reader error'))
    reader.SetFileName(str(marker))
    reader.CreateCellToPointOff()
    reader.UpdateInformation()
    reader.EnableAllCellArrays()
    reader.EnableAllPatchArrays()
    labels=reader.GetTimeValues()
    available=[labels.GetValue(i) for i in range(labels.GetNumberOfValues())] if labels else []
    configuration=evidence.get('configuration', {})
    solver=configuration.get('solver')
    refined=evidence.get('dynamic_refinement')
    history_fields={}
    if solver == 'acousticFoam':
        history_fields['pa_0']=b'1 -1 -2 0 0 0 0'
    if refined is not None:
        history_fields[configuration['dynamic_refinement']['field']+'_0']=b'0 0 0 0 0 0 0'
    solved=set(evidence['summary']['fields'])
    if solver in {'pimpleFoam','interFoam'}:
        # CorrectPhi solves a temporary pressure corrector.
        solved.discard('pcorr')
        solved.update(('p_rgh' if solver == 'interFoam' else 'p','U'))
    elif solver == 'rhoCentralFoam':
        # The conservative momentum and energy variables are intermediate fields.
        solved.difference_update({'rhoU','rhoUx','rhoUy','rhoUz','rhoE'})
        solved.update(('rho','p','T','U'))
    components={'volScalarField':1,'volVectorField':3,'volSphericalTensorField':1,
                'volSymmTensorField':6,'volTensorField':9}
    regions=configuration.get('regions', {})
    cell_counts={}

    def named_blocks(block):
        if block is None or not hasattr(block,'GetNumberOfBlocks'):
            raise ValueError('OpenFOAM reader did not provide mesh blocks')
        return {block.GetMetaData(i).Get(vtk.vtkCompositeDataSet.NAME()):block.GetBlock(i)
                for i in range(block.GetNumberOfBlocks()) if block.HasMetaData(i)}

    def leaves(block):
        if hasattr(block,'GetNumberOfBlocks'):
            for index in range(block.GetNumberOfBlocks()):
                child=block.GetBlock(index)
                if child is not None:
                    yield from leaves(child)
        elif block.GetNumberOfCells():
            yield block

    for label,paths in sorted(times.items(),key=lambda item:float(item[0])):
        value=float(label)
        if not any(math.isclose(value,time,rel_tol=0,abs_tol=1e-9) for time in available):
            raise ValueError('Output time cannot be read by OpenFOAM reader')
        reader.UpdateTimeStep(value)
        output=reader.GetOutput()
        blocks=named_blocks(output) if regions else {'':output}
        required={name:openfoam_cht.INITIAL_FIELDS[region['group']] for name,region in regions.items()} if regions else {'':solved}
        for region,required_fields in required.items():
            block=blocks.get(region)
            grid=named_blocks(block).get('internalMesh') if block is not None else None
            if errors or grid is None or grid.GetNumberOfCells()<1:
                raise ValueError('OpenFOAM output fields or mesh are unreadable: '+(region or run_dir))
            size=grid.GetNumberOfCells()
            expected_count=refined['written_cells'][label] if refined is not None else cell_counts.setdefault(region,size)
            if size!=expected_count:
                raise ValueError('Output field mesh changed across written times: '+(region or run_dir))
            if refined is not None:
                levels=grid.GetCellData().GetArray('cellLevel')
                values=vtk_to_numpy(levels) if levels is not None else np.array([])
                if (values.size!=size or not np.all(np.isfinite(values)) or np.any(values!=np.rint(values))
                        or np.any(values<0) or np.any(values>configuration['dynamic_refinement']['maxRefinement'])):
                    raise ValueError('Dynamic mesh lacks the native cellLevel evidence for '+label)
            parent=root/run_dir/label/region
            fields={}
            histories=set()
            for path in paths:
                if path.parent != parent:
                    continue
                header=field_header(path)
                kind=header.get('class','')
                if not kind.startswith('vol'):
                    continue
                name=path.stem if path.suffix=='.gz' else path.name
                if kind not in components or header.get('object')!=name or name in fields:
                    raise ValueError('Solver field identity/type is invalid')
                if name in history_fields:
                    if name in histories:
                        raise ValueError('Duplicate scalar field history: '+name)
                    _validate_scalar_history(path,header,size,history_fields[name])
                    histories.add(name)
                    continue
                fields[name]=components[kind]
            if not fields or any(name not in fields and not (name[-1:] in {'x','y','z'} and name[:-1] in fields)
                                 for name in required_fields):
                raise ValueError('An output time is missing a solved field: '+(region or run_dir))
            for part in leaves(block):
                for name,count in fields.items():
                    array=part.GetCellData().GetArray(name)
                    if (array is None or array.GetNumberOfComponents()!=count or array.GetNumberOfTuples()!=part.GetNumberOfCells()
                            or not np.all(np.isfinite(vtk_to_numpy(array)))):
                        raise ValueError('Solver internal/boundary field has missing, malformed or non-finite values: '+name)


def artifacts_for(run,descriptor,output,frozen_files):
    run_dir=descriptor['run_dir']
    from .mesh_artifacts import input_snapshot
    resumed=None
    if descriptor['handler']=='solve.resume.v1':
        from .solve_resume_artifacts import NAME,verify_frozen,require_toolchain
        resumed=verify_frozen(run,run_dir,run_dir+'/'+NAME,completed=True)
        if hashlib.sha256((run/run_dir/NAME).read_bytes()).hexdigest()!=descriptor['checkpoint_sha256']:
            raise ValueError('Continuation checkpoint record differs from preparation')
    else:
        observed = input_snapshot(run,run_dir)
        for kind, relatives, *_ in _DERIVED_INPUTS:
            for path, expected in descriptor.get(kind, {}).items():
                if path != run_dir+'/'+relatives[0] or observed['files'].pop(path, None) != expected:
                    raise ValueError('Generated OpenFOAM input differs from the sealed input: '+relatives[0])
                if path in observed['selection']:
                    observed['selection'].remove(path)
        feedback = descriptor.get('generated_structural_feedback')
        if feedback:
            if set(feedback)!={run_dir+'/'+name for name in _STRUCTURAL_FEEDBACK_FILES}:
                raise ValueError('Structural feedback generated inputs differ from preparation')
            for path,record in feedback.items():
                if observed['files'].pop(path,None)!=record:
                    raise ValueError('Structural feedback input differs from the sealed displacement')
                if path in observed['selection']:
                    observed['selection'].remove(path)
        regional = descriptor.get('generated_cht_materials', {})
        material_paths = {run_dir+'/'+openfoam_cht_materials.relative(name)
                          for name in descriptor['configuration'].get('cht_materials', {})}
        for path, expected in regional.items():
            if path not in material_paths or observed['files'].pop(path, None) != expected:
                raise ValueError('Generated CHT material differs from the sealed input: '+path)
        for kind,relatives,label in _OVERLAID_INPUTS:
            reference_paths={run_dir+'/'+name for name in relatives}
            for path,record in descriptor.get(kind,{}).items():
                original=descriptor['source']['files'].get(path)
                if path not in reference_paths or original is None or observed['files'].get(path)!=record:
                    raise ValueError(label+' differs from the sealed native input')
                observed['files'][path]=original
        generated_fields = descriptor.get('generated_fields', {})
        field_paths = {run_dir+'/0/'+field for field in openfoam_boundary_inputs.FIELDS}
        for path, expected in generated_fields.items():
            if path not in field_paths or observed['files'].pop(path, None) != expected:
                raise ValueError('Generated OpenFOAM initial fields differ from the sealed input')
        if generated_fields and run_dir+'/0' not in descriptor['source']['selection']:
            observed['selection'].remove(run_dir+'/0')
        if observed != descriptor['source']:
            raise ValueError('Solver source inputs differ from preparation')
    before=initial_snapshot(run,run_dir,descriptor['execution_id'],resume_time=resumed['time'] if resumed else None)
    for item in before['inputs']:
        if any(frozen_files[item['path']][key]!=item[key] for key in ('size','sha256')):
            raise ValueError('Solver inputs/mesh changed after sealing')
    if before['configuration']!=descriptor['configuration']:
        raise ValueError('Solver controls differ from the prepared contract')
    evidence=json.loads((run/run_dir/EVIDENCE).read_text(encoding='utf-8'))
    expected=result(run,before)
    if resumed:expected['resume']=resumed
    if evidence!=expected:
        raise ValueError('Solver proof differs from actual inputs, log or results')
    toolchain=json.loads((run/run_dir/TOOLCHAIN).read_text(encoding='utf-8'))
    invocation=json.loads((run/run_dir/INVOCATION).read_text(encoding='utf-8'))
    if invocation!={'schema':'vibeflow.solve-invocation.v1','state':'prepared','snapshot':before,'toolchain':toolchain,'resume':resumed}:
        raise ValueError('Solver invocation receipt differs from actual frozen inputs/toolchain')
    if (toolchain.get('schema')!='vibeflow.solve-toolchain.v1' or
            toolchain.get('execution_id')!=descriptor['execution_id'] or
            toolchain.get('worker_sha256')!=descriptor['worker_sha256'] or
            toolchain.get('runner_sha256')!=descriptor['runner_sha256'] or
            toolchain.get('solver')!=descriptor['configuration']['solver'] or
            toolchain.get('distro')!=descriptor['distro'] or not toolchain.get('openfoam') or
            not toolchain.get('executable_sha256')):
        raise ValueError('Solver toolchain does not belong to this invocation')
    directory = descriptor.get('source_library_dir')
    source_build = toolchain.get('source_build')
    if directory:
        library = openfoam_source_runner.library_path(directory,toolchain['solver'])
        if (not isinstance(source_build,dict) or toolchain.get('executable')!=library
                or source_build.get('solver')!=toolchain['solver']
                or source_build.get('openfoam')!=toolchain['openfoam']
                or source_build.get('library_sha256')!=toolchain['executable_sha256']
                or source_build.get('loader_sha256')!=openfoam_source_runner.digest(Path(openfoam_source_runner.__file__))):
            raise ValueError('Solver source build does not belong to this invocation')
    elif source_build is not None:
        raise ValueError('Unexpected solver source build')
    if resumed:require_toolchain(toolchain,resumed['toolchain'])
    validate_fields(run,run_dir,evidence)
    if output.get('schema')!='vibeflow.attempt-outputs.v1':
        raise ValueError('Solver output capture is missing')
    records={}
    for path,record in output['files'].items():
        if managed(path,run_dir):
            actual=workspace.digest_file(workspace.checked_path(run,path))
            if any(actual[key]!=record[key] for key in ('size','sha256')):
                raise ValueError('Solver output changed after capture')
            records[path]=record
    required={run_dir+'/'+name for name in (EVIDENCE,TOOLCHAIN,LOG,INVOCATION)}|{row['path'] for row in evidence['outputs']}
    if resumed:
        records[run_dir+'/'+NAME]=output['files'][run_dir+'/'+NAME]
        if workspace.digest_file(run/run_dir/NAME)['sha256']!=records[run_dir+'/'+NAME]['sha256']:
            raise ValueError('Captured continuation checkpoint record changed')
    if not required.issubset(records):
        raise ValueError('Solver publication is incomplete')
    return records


async def gate_reason(db,case,*,freshness=None):
    from .artifact_publication import current
    from .case_write import CaseWriteConflict
    from .publication_inputs import resolve
    try:
        head,row=await current(db,case.id,'solve')
        if head is None:
            return None,False
        if row is None:
            return '当前求解成果尚未发布或已被修改，请重新求解',True
        sources = await resolve(db, case, [{'publication_id':row.id,'paths':None,'require_current':True}],
                                materialize=False,_validated_current=freshness)
        reason = await finish_before_cancel(asyncio.to_thread(_gate_files, case.s3_prefix, row, sources[0]))
        return reason, True
    except TimeoutError as error:
        raise CaseWriteConflict('validation_busy','求解证据核验超时，请稍后重试') from error
    except (OSError,ValueError,KeyError,TypeError):
        return '求解发布或上游输入已失效，请重新求解',True


def _gate_files(prefix, row, source):
    from .casedict import CASE_DICT_PATH, parse_casedict

    descriptor = source.manifest['descriptor']
    run_dir = descriptor['run_dir']
    dictionary = storage.get_bytes(prefix, CASE_DICT_PATH).decode('utf-8')
    requested = (parse_casedict(dictionary).get('postProcessing') or {}).get('runDir')
    if requested and requested != run_dir:
        return '求解版本不属于当前后处理运行目录'
    reason = verify_records(prefix, [{'path':path, **record} for path, record in source.files.items()],
                            label='当前求解', required=True, parallel_remote=True)
    if reason:
        return reason
    handler = descriptor.get('handler')
    evidence_name = {'workflow-mpi.v1':'mpi-evidence.json','workflow-udf.v1':'udf-evidence.json'}.get(handler, EVIDENCE)
    path = run_dir + '/' + evidence_name
    expected = source.files[path]
    if source.root is None:
        from .attempt_objects import read
        data = read(prefix, row.immutable_path+'/files/'+path, limit=expected['size'])
    else:
        data = workspace.checked_path(source.root, path).read_bytes()
    if len(data) != expected['size'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
        raise ValueError('Solver evidence changed after publication verification')
    evidence = json.loads(data)
    checked = evidence.get('inputs')
    if handler == 'workflow-mpi.v1':
        # The generated decomposition stays private to the sealed attempt.
        original = descriptor['native'].get('origin_source', descriptor['native']['source'])['files']
        checked = [record for record in checked if record['path'] in original]
    elif handler == 'workflow-udf.v1':
        checked = [{'path':path, **record} for path, record in descriptor['source']['solver']['files'].items()]
    fields = descriptor.get('generated_fields')
    if fields:
        frozen = {record['path']:{key:record[key] for key in ('size','sha256')}
                  for record in checked if record['path'] in fields}
        if (frozen != fields or evidence['case_input_sha256'] != case_input_fingerprint(dictionary)
                or any(storage.exists(prefix,path+suffix) for path in fields for suffix in ('','.gz'))):
            return '初值或边界输入已变化，请重新求解'
        checked = [record for record in checked if record['path'] not in fields]
    feedback=descriptor.get('generated_structural_feedback')
    if feedback:
        frozen={record['path']:{key:record[key] for key in ('size','sha256')}
                for record in checked if record['path'] in feedback}
        if (frozen!=feedback or evidence['case_input_sha256']!=case_input_fingerprint(dictionary)
                or any(storage.exists(prefix,path+suffix) for path in feedback for suffix in ('','.gz'))):
            return '结构反馈输入已变化，请重新求解'
        checked=[record for record in checked if record['path'] not in feedback]
    for kind, relatives, requested_input, make_dictionary, label in _DERIVED_INPUTS:
        generated = descriptor.get(kind)
        if not generated:
            continue
        value = requested_input(dictionary)
        input_path = run_dir+'/'+relatives[0]
        if value is None or any(storage.exists(prefix,run_dir+'/'+relative+suffix)
                                for relative in relatives for suffix in ('','.gz')):
            return f'{label}输入已变化，请重新求解'
        text = make_dictionary(value)
        expected = {'size':len(text), 'sha256':hashlib.sha256(text).hexdigest()}
        frozen = {record['path']:{key:record[key] for key in ('size','sha256')}
                  for record in checked if record['path'] == input_path}
        if generated != {input_path:expected} or frozen != generated:
            return f'{label}输入已变化，请重新求解'
        checked = [record for record in checked if record['path'] != input_path]
    regional = descriptor.get('generated_cht_materials')
    if regional:
        from .casedict import parse_casedict
        cfg = descriptor['configuration']
        materials = openfoam_cht_materials.declared(parse_casedict(dictionary), cfg['regions'])
        expected = {}
        for name, properties in (materials or {}).items():
            path = run_dir+'/'+openfoam_cht_materials.relative(name)
            if path in regional:
                body = openfoam_cht_materials.dictionary(properties)
                expected[path] = {'size':len(body),'sha256':hashlib.sha256(body).hexdigest()}
        frozen = {record['path']:{key:record[key] for key in ('size','sha256')}
                  for record in checked if record['path'] in regional}
        if (expected != regional or frozen != regional
                or any(storage.exists(prefix,path+suffix) for path in regional for suffix in ('','.gz'))):
            return '共轭传热区域材料已变化，请重新求解'
        checked = [record for record in checked if record['path'] not in regional]
    for kind,relatives,label in _OVERLAID_INPUTS:
        overlay=descriptor.get(kind)
        if not overlay:
            continue
        frozen={record['path']:{key:record[key] for key in ('size','sha256')}
                for record in checked if record['path'] in overlay}
        originals=descriptor['source']['files']
        if (frozen!=overlay or not overlay.keys()<=originals.keys() or not overlay.keys()<={run_dir+'/'+name for name in relatives}
                or evidence['case_input_sha256']!=case_input_fingerprint(dictionary)):
            return f'{label}输入已变化，请重新求解'
        checked=[{'path':record['path'],**originals[record['path']]} if record['path'] in overlay else record
                 for record in checked]
    if evidence.get('resume'):
        # The derived continuation controlDict belongs to the private attempt.
        checked = [record for record in checked if record['path'] != run_dir+'/system/controlDict']
    reason = verify_records(prefix, checked, label='当前求解输入与网格', required=True, parallel_remote=True)
    if reason:
        return reason
    if _gate_output_paths(prefix, run_dir) != {record['path'] for record in evidence['outputs']}:
        return '工作副本输出时刻包含未发布或缺失的场，请对齐当前求解版本'
    return None


def _gate_output_paths(prefix, run_dir):
    if not storage._use_s3():
        root = storage._fs_path(prefix, '.')
        return {path.relative_to(root).as_posix() for paths in output_times(root,run_dir).values() for path in paths}
    from .attempt_objects import selection
    from .case_write import CaseWriteConflict

    run_dir = safe_run_dir(run_dir)
    versions = selection(prefix, [run_dir], False)
    observed = {path for paths in group_output_paths(versions, run_dir).values() for path in paths}
    if selection(prefix, [run_dir], False) != versions:
        raise CaseWriteConflict('input_stale', 'S3 solve output inventory changed during inspection')
    return observed
