"""Registered fresh serial OpenFOAM solve, pinned to its confirmed mesh."""
import asyncio
import hashlib
from pathlib import Path
import re
import shlex
import shutil
import uuid

from .. import storage
from . import mesh_worker,openfoam_acoustic_medium,openfoam_boundary_inputs,openfoam_cht,openfoam_cht_materials,openfoam_conduction_material,openfoam_deformation,openfoam_dynamic_refinement,openfoam_equations,openfoam_fluid_model,openfoam_fluid_thermophysics,openfoam_numerical_inputs,openfoam_overset,openfoam_pressure_reference,openfoam_reference_temperature,openfoam_rotation,solve_publication
from .async_ops import finish_before_cancel
from .case_evidence import file_record,safe_run_dir
from .case_execution_settings import selected_restart
from .casedict import CASE_DICT_PATH,parse_casedict
from .geometry_handler import GeometryHandler
from .jobrunner import win_to_wsl_path
from .mesh_artifacts import ARTIFACTS, QUALITY, boundaries, selection
from .runtime_config import get_runtime_setting
from .solve_artifacts import configuration, gravity_input, has_initial_fields, motion_input, porosity_input, transport_input
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict, InvalidStepInput
from . import openfoam_runtime, openfoam_source_runner, openfoam_structural_feedback

WORKER=Path(__file__).with_name('solve_worker.py')


def source_selection(solver):
    from ..config import get_settings

    configured = get_settings().openfoam_source_library_dir
    if not configured or solver not in openfoam_source_runner.SOLVERS:
        return {}
    try:
        openfoam_source_runner.library_path(configured, solver)
    except ValueError as error:
        raise ExecutionConflict('runtime_unavailable', str(error)) from error
    return {'source_library_dir': configured}


def command(run_dir,execution_id,distro,bashrc,max_log_bytes,solver,source_library_dir=''):
    arguments = ['python3',win_to_wsl_path(WORKER),
        '--run-dir',run_dir,'--execution-id',execution_id,'--distro',distro,
        '--max-log-bytes',str(max_log_bytes),'--solver',solver]
    if source_library_dir:
        arguments.extend(['--source-library-dir',source_library_dir])
    return 'source '+shlex.quote(bashrc)+' && exec '+shlex.join(arguments)


class SolveHandler:
    key='solve.openfoam'
    version='1'
    job_type='solve'
    publication_kind='solve'
    cancellation=dict(GeometryHandler.cancellation)
    recovery={**GeometryHandler.recovery,'resume':True,'resume_policy':'explicit_new_attempt_with_validated_native_checkpoint'}

    async def prepare(self,db,case,user,*,parameters,budget,observations=None,resolved_inputs=None):
        if 'run_dir' not in parameters or set(parameters)-{'run_dir','mesh_publication_id'}:
            raise ExecutionConflict('invalid_request','Solver requires a registered run directory')
        run_dir=safe_run_dir(parameters['run_dir'])
        from .mesh_publication import _source_snapshot,inspect_inputs
        def read_inputs(root,run_dir):
            document=(root/CASE_DICT_PATH).read_text(encoding='utf-8')
            parsed=parse_casedict(document)
            try:
                cfg=configuration(root,run_dir,require_initial=False)
                numerical=openfoam_numerical_inputs.native_inputs(root,run_dir,cfg['solver'],document)
            except openfoam_overset.OversetInputError as error:
                raise InvalidStepInput(str(error)) from error
            except ValueError as error:
                physical=parsed.get('physicalProblem')
                if ((isinstance(physical,dict) and physical.get('referencePressureRole')=='gauge')
                        or (isinstance(physical,dict) and 'movingFrame' in physical)
                        or openfoam_conduction_material.selection(parsed) is not None
                        or openfoam_fluid_model.selection(parsed) is not None
                        or openfoam_fluid_thermophysics.selection(parsed) is not None
                        or openfoam_rotation.selection(parsed)
                        or openfoam_deformation.selection(parsed)
                        or any(isinstance(entry,dict) and 'structuralDisplacement' in entry
                               for entry in (parsed.get('interfaces') or {}).values())
                        or openfoam_equations.selection(parsed) is not None
                        or openfoam_cht_materials.selection(parsed)
                        or any(name in (parsed.get('numericalMethod') or {}) for name in ('linearSolvers','timeControl','nonlinear','stabilization'))):
                    raise InvalidStepInput(str(error)) from error
                raise
            try:
                gravity=gravity_input(root,run_dir)
                temperature=openfoam_reference_temperature.prepare(root,run_dir,cfg['solver'],cfg.get('buoyancy_reference_temperature_K'))
                pressure=openfoam_pressure_reference.prepare(root,run_dir,cfg['solver'],cfg.get('pressure_reference'))
                if 'pressure_reference' in cfg:
                    openfoam_pressure_reference.pressure_field(root,run_dir)
                medium=openfoam_acoustic_medium.prepare(root,run_dir,cfg.get('acoustic_medium'))
                conduction=openfoam_conduction_material.prepare(root,run_dir,cfg['solver'],cfg.get('conduction_material'))
                flow=openfoam_fluid_model.prepare(root,run_dir,cfg['solver'],cfg.get('fluid_model'))
                thermophysics=openfoam_fluid_thermophysics.prepare(root,run_dir,cfg['solver'],cfg.get('fluid_thermophysics'))
                refinement=openfoam_dynamic_refinement.prepare(root,run_dir,cfg['solver'],cfg.get('dynamic_refinement'))
                rotation=openfoam_rotation.prepare(root,run_dir,cfg['solver'],cfg.get('rotation'))
                motion=motion_input(root,run_dir,cfg['solver'])
                deforming,displacement=openfoam_deformation.prepare(root,run_dir,cfg['solver'],cfg.get('deformation'))
                feedback=cfg.get('structural_feedback')
                feedback_geometry=openfoam_structural_feedback.prepared_inputs(root,run_dir,feedback) if feedback else None
                cht_materials=openfoam_cht_materials.prepare(root,run_dir,cfg.get('cht_materials'))
                transport=transport_input(root,run_dir) if cfg['solver'] == 'icoFoam' else {}
            except (OSError,ValueError,OverflowError) as error:
                raise InvalidStepInput(str(error)) from error
            generated = {'generated_gravity': gravity,
                         'generated_porosity': porosity_input(root,run_dir,cfg['solver']),
                         'generated_motion': motion,
                         'generated_deformation': deforming,
                         'generated_mesh_displacement': displacement,
                         'generated_cht_materials': cht_materials,
                         'generated_reference_temperature': temperature,
                         'generated_reference_pressure': pressure,
                         'generated_acoustic_medium': medium,
                         'generated_conduction_material': conduction,
                         'generated_fluid_model': flow,
                         'generated_dynamic_refinement': refinement,
                         'generated_transport': transport,
                         'generated_rotation': rotation,
                         'generated_numerical': numerical}
            if thermophysics:
                kind=cfg['fluid_thermophysics']['model']
                generated[openfoam_fluid_thermophysics.DERIVED_INPUTS[kind]]=thermophysics
            boundary_record = source_seed = None
            try:
                declarations, initial = openfoam_boundary_inputs.active_declarations(parsed,cfg['solver'])
                if declarations or initial:
                    patch_path = root/run_dir/'constant/polyMesh/boundary'
                    if cfg['solver'] in openfoam_boundary_inputs.SOLVER_FIELDS and not patch_path.is_file():
                        raise ValueError('请先确认提供完整边界清单的 OpenFOAM 网格')
                    patches = boundaries(patch_path) if patch_path.is_file() else []
                    if initial.get('initialization') in {'restart','mapped','analytical','fromMeasurement'}:
                        conditions = openfoam_boundary_inputs.source_conditions(
                            root,run_dir,cfg,parsed,declarations,initial,patches)
                        needed = None
                        if initial['initialization'] == 'analytical':
                            needed = openfoam_boundary_inputs.analytical_specs(cfg,initial,parsed)
                        elif initial['initialization'] == 'fromMeasurement':
                            from .openfoam_initial_measurement import prepare as prepare_measurements
                            needed = prepare_measurements(root,run_dir,cfg,parsed,initial)
                        source_seed = initial, conditions, needed
                    else:
                        generated['generated_fields'] = openfoam_boundary_inputs.field_inputs(
                            root,run_dir,cfg,parsed,declarations,initial,patches)
                    boundary_record = file_record(root,patch_path)
            except (OSError,ValueError) as error:
                raise InvalidStepInput(str(error)) from error
            if source_seed is None and not generated.get('generated_fields') and not has_initial_fields(root,run_dir):
                raise ValueError('Fresh solve requires initial fields in 0 or a complete caseDict declaration')
            paths=selection(root,run_dir)
            if pressure:
                system=run_dir+'/system'
                paths.remove(system)
                paths.extend(path.relative_to(root).as_posix() for path in sorted((root/system).iterdir()))
            paths=[path for path in paths if path not in temperature and path not in pressure]
            source = _source_snapshot(root,run_dir)
            if source_seed is not None and source_seed[0]['initialization'] == 'fromMeasurement':
                measurement = source_seed[2][2]
                if source['files'].get(measurement['path']) != {key:measurement[key] for key in ('size','sha256')}:
                    raise ExecutionConflict('input_stale','测量初值 CSV 在读取期间已变化')
            policy='disabled' if feedback else selected_restart(parsed)
            return cfg,paths,source,generated,boundary_record,policy,source_seed,feedback_geometry
        cfg,paths,source,derived,boundary_record,restart_policy,source_seed,feedback_geometry=await finish_before_cancel(asyncio.to_thread(
            inspect_inputs,case.s3_prefix,read_inputs,run_dir))
        distro,bashrc=openfoam_runtime.configured()
        from .artifact_publication import current,read_manifest
        from .publication_inputs import resolve
        reader=db if observations is None else observations
        _,mesh=await current(reader,case.id,'mesh')
        if mesh is None or parameters.get('mesh_publication_id',mesh.id)!=mesh.id:
            raise ExecutionConflict('input_stale','Confirmed workflow mesh is no longer current')
        if observations is not None and storage._use_s3():
            from .publication_objects import bound_metadata
            manifest=await finish_before_cancel(asyncio.to_thread(bound_metadata,case.s3_prefix,mesh))
        else:
            _,manifest=await finish_before_cancel(asyncio.to_thread(read_manifest,case.s3_prefix,mesh))
        if manifest['descriptor']['run_dir']!=run_dir:
            raise ExecutionConflict('input_stale','Mesh belongs to another run directory')
        if cfg['solver'] == openfoam_cht.SOLVER:
            if manifest['descriptor'].get('mesh_source') != openfoam_cht.MESH_SOURCE:
                raise ExecutionConflict('input_stale','CHT requires a confirmed multi-region mesh publication')
            mesh_paths=[QUALITY]
        else:
            mesh_paths=sorted(path for path in manifest['files'] if path.startswith(run_dir+'/constant/polyMesh/'))
        if manifest['descriptor'].get('mesh_artifact_index_version')==1:
            mesh_paths=sorted([*mesh_paths,ARTIFACTS])
        if boundary_record:
            expected = manifest['files'].get(boundary_record['path'], {})
            if any(expected.get(key) != boundary_record[key] for key in ('size', 'sha256')):
                raise ExecutionConflict('input_stale','Declared fields do not match the confirmed mesh boundaries')
        initial_source = None
        if source_seed is not None:
            from . import openfoam_initial_source
            initial,conditions,needed = source_seed
            try:
                if needed is None:
                    fields,initial_source = await openfoam_initial_source.resolve(reader,case,initial,cfg,run_dir,mesh,manifest)
                else:
                    fields,initial_source = await finish_before_cancel(asyncio.to_thread(
                        openfoam_initial_source.generated,case.s3_prefix,mesh,manifest,run_dir,initial['initialization'],needed))
                derived['generated_fields'] = openfoam_boundary_inputs.render_source_fields(run_dir,fields,conditions)
            except (OSError,ValueError,KeyError) as error:
                raise InvalidStepInput(str(error)) from error
        references=[{'publication_id':mesh.id,'paths':mesh_paths,'require_current':True}]
        if feedback_geometry is not None:
            from ..models import ArtifactPublication

            feedback=cfg['structural_feedback']
            publication=await reader.get(ArtifactPublication,feedback['publication_id'])
            if (publication is None or publication.case_id!=case.id or not publication.kind.startswith('calculix:')
                    or publication.status not in {'committed','superseded'}):
                raise InvalidStepInput('当前算例没有所选的 CalculiX 结构反馈发布')
            try:
                data=await finish_before_cancel(asyncio.to_thread(
                    openfoam_structural_feedback.read_archive,case.s3_prefix,publication,feedback))
                derived['generated_structural_feedback']=openfoam_structural_feedback.generated(run_dir,feedback,feedback_geometry,data)
            except (OSError,ValueError,KeyError) as error:
                raise InvalidStepInput(str(error)) from error
            references.append({'publication_id':publication.id,'paths':[feedback['frd_path']],'require_current':False})
        resolved=await resolve(reader,case,references)
        if resolved_inputs is not None:
            resolved_inputs.extend(resolved)
        from .quota import check_job_quota
        await check_job_quota(db,user, case_id=case.id)
        execution_id=str(uuid.uuid4())
        source_build = source_selection(cfg['solver'])
        spec={'command':command(run_dir,execution_id,distro,bashrc,budget['max_log_bytes'],cfg['solver'],**source_build),
            'case_prefix':case.s3_prefix,'runtime':'wsl','argv':None,
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        spec.update(openfoam_runtime.transport(spec['command'],distro))
        self.validate_spec(spec)
        generated = {path:body for inputs in derived.values() for path,body in inputs.items()}
        derived_records = {kind:{path:{'size':len(body),'sha256':hashlib.sha256(body).hexdigest()}
                                for path,body in inputs.items()} for kind,inputs in derived.items() if inputs}
        return PreparedStep(self.key,self.version,self.job_type,spec,paths,references,
            self.publication_kind,{'handler':'solve.v1','handler_version':self.version,'source':source,
            'run_dir':run_dir,'configuration':cfg,'distro':distro,'execution_id':execution_id,
            'worker_sha256':hashlib.sha256(WORKER.read_bytes()).hexdigest(),
            'runner_sha256':hashlib.sha256(Path(mesh_worker.__file__).read_bytes()).hexdigest(),
            **({'restart_policy':restart_policy} if restart_policy is not None else {}),
            **({'initial_source':initial_source} if initial_source is not None else {}),
            'input_publications':references,**source_build,**derived_records},generated_inputs=generated or None)

    def validate_spec(self,spec):
        try:
            parts=shlex.split(spec['command'])
            if (len(parts) not in {16,18} or parts[0]!='source' or parts[2:6]!=['&&','exec','python3',win_to_wsl_path(WORKER)] or
                    parts[6:16:2]!=['--run-dir','--execution-id','--distro','--max-log-bytes','--solver'] or
                    not re.fullmatch(r'/usr/lib/openfoam/[A-Za-z0-9_.-]+/etc/bashrc',parts[1]) or
                    not re.fullmatch(r'[a-z0-9-]+',parts[11]) or safe_run_dir(parts[7])!=parts[7] or
                    str(uuid.UUID(parts[9]))!=parts[9] or int(parts[13])!=spec['max_log_bytes'] or
                    not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*Foam',parts[15]) or
                    not openfoam_runtime.valid_transport(spec,parts[11]) or spec.get('evidence_run_dir') is not None):
                raise ValueError('Unexpected solver command')
            if len(parts)==18:
                if parts[16]!='--source-library-dir':
                    raise ValueError('Unexpected solver source option')
                openfoam_source_runner.library_path(parts[17],parts[15])
                if spec['command']!=command(parts[7],parts[9],parts[11],parts[1],int(parts[13]),parts[15],parts[17]):
                    raise ValueError('Noncanonical solver source command')
        except (ValueError,KeyError,TypeError,IndexError) as error:
            raise ExecutionConflict('evidence_invalid','Solver command differs from its registered worker') from error

    def artifacts(self,run,descriptor,output,frozen_files):
        return solve_publication.artifacts_for(run,descriptor,output,frozen_files)

    def source(self,prefix,descriptor):
        return solve_publication.source_snapshot(prefix,descriptor['run_dir'])
