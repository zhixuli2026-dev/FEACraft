"""Registered OpenFOAM mesh execution with sealed native inputs."""
import asyncio
import hashlib
from pathlib import Path
import re
import shlex
import shutil
import uuid

from .. import storage
from . import mesh_boundary_layers, mesh_publication, openfoam_cht, openfoam_numerical_inputs
from .async_ops import finish_before_cancel
from .case_evidence import safe_run_dir
from .casedict import parse_casedict
from .geometry_handler import GeometryHandler
from .jobrunner import win_to_wsl_path
from .mesh_artifacts import (EXTRA_NATIVE_INPUTS,IMPORTED_GMSH,artifact_paths,gmsh_format,selection,source_input_name,
                             mesh_source,validate_source_role)
from .runtime_config import get_runtime_setting
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict,InvalidStepInput
from . import openfoam_runtime

WORKER=Path(__file__).with_name('mesh_worker.py')


def command(run_dir, execution_id, distro, bashrc, max_log_bytes):
    return 'source '+shlex.quote(bashrc)+' && exec '+shlex.join(['python3',win_to_wsl_path(WORKER),
        '--run-dir',run_dir,'--execution-id',execution_id,'--distro',distro,
        '--max-log-bytes',str(max_log_bytes)])


class MeshHandler:
    key='mesh.generate'
    version='1'
    job_type='mesh'
    publication_kind='mesh'
    cancellation=dict(GeometryHandler.cancellation)
    recovery=dict(GeometryHandler.recovery)

    async def prepare(self,db,case,user,*,parameters,budget,observations=None,resolved_inputs=None):
        if 'run_dir' not in parameters or set(parameters)-{'run_dir','geometry_publication_id'}:
            raise ExecutionConflict('invalid_request','Mesh requires a registered run directory')
        run_dir=safe_run_dir(parameters['run_dir'])
        def read_inputs(root,run_dir):
            kind=mesh_source(root)
            text = (root/'0-caseDict/caseDict').read_text(encoding='utf-8')
            parsed=parse_casedict(text)
            mesh=parsed['mesh']
            refinement = 'refinement' in mesh
            generated={}
            try:
                input_name=source_input_name(root/run_dir,kind)
                if not (root/run_dir/input_name).is_file() or not (root/run_dir/'system/controlDict').is_file():
                    raise ValueError(input_name+' and system/controlDict are required in run_dir')
                artifact_paths(mesh,run_dir,kind,input_name)
                validate_source_role(mesh,kind)
                for name in EXTRA_NATIVE_INPUTS.get(kind,()):
                    if not (root/run_dir/name).is_file():
                        raise ValueError(kind+' requires '+name)
                if kind==IMPORTED_GMSH:
                    gmsh_format(root/run_dir/input_name)
                if refinement and kind!='blockMesh':
                    raise ValueError('托管方向加密需要 blockMesh 生成的原生网格')
                layers=mesh_boundary_layers.declaration(mesh)
                if layers and kind!='blockMesh':
                    raise ValueError('当前托管边界层检查需要 blockMesh 原生网格')
                native=root/run_dir/openfoam_numerical_inputs.SOLUTION
                if ('linearSolvers' in (parsed.get('numericalMethod') or {}) and not native.exists()
                        and not native.with_suffix('.gz').exists() and kind!=openfoam_cht.MESH_SOURCE):
                    from .solve_artifacts import control_values
                    controls=(root/run_dir/'system/controlDict').read_text(encoding='utf-8')
                    generated=openfoam_numerical_inputs.native_inputs(root,run_dir,control_values(controls).get('application'),text)
            except ValueError as error:
                raise InvalidStepInput(str(error)) from error
            if kind == openfoam_cht.MESH_SOURCE:
                openfoam_cht.inspect_inputs(root,run_dir)
            return kind,selection(root,run_dir),mesh_publication._source_snapshot(root,run_dir),refinement,generated
        kind,paths,source,refinement,generated=await finish_before_cancel(asyncio.to_thread(
            mesh_publication.inspect_inputs,case.s3_prefix,read_inputs,run_dir))
        distro,bashrc=openfoam_runtime.configured()
        from .artifact_publication import current
        from .publication_inputs import resolve
        reader=db if observations is None else observations
        _,geometry=await current(reader,case.id,'geometry')
        if geometry is None:
            raise ExecutionConflict('input_stale','Registered mesh requires a current geometry publication')
        if parameters.get('geometry_publication_id',geometry.id)!=geometry.id:
            raise ExecutionConflict('input_stale','Confirmed workflow geometry has been replaced; start a new workflow')
        references=[{'publication_id':geometry.id,'paths':['A-geo/validation.json'],'require_current':True}]
        resolved=await resolve(reader,case,references)
        if resolved_inputs is not None:
            resolved_inputs.extend(resolved)
        from .quota import check_job_quota
        await check_job_quota(db,user, case_id=case.id)
        execution_id=str(uuid.uuid4())
        spec={'command':command(run_dir,execution_id,distro,bashrc,budget['max_log_bytes']),
            'case_prefix':case.s3_prefix,'runtime':'wsl','argv':None,
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],
            'evidence_run_dir':None}
        spec.update(openfoam_runtime.transport(spec['command'],distro))
        self.validate_spec(spec)
        derived={path:{'sha256':hashlib.sha256(body).hexdigest(),'size':len(body)} for path,body in generated.items()}
        return PreparedStep(self.key,self.version,self.job_type,spec,paths,references,
            self.publication_kind,{'handler':'mesh.v1','handler_version':self.version,'source':source,
            'run_dir':run_dir,'distro':distro,'execution_id':execution_id,'mesh_source':kind,
            'mesh_quality_version':2,'mesh_artifact_index_version':1,'mesh_family_evidence_version':1,
            'mesh_boundary_layer_evidence_version':1,'mesh_dimension_evidence_version':1,
            'mesh_quality_preset_version':1,
            **({'mesh_study_geometry_version':1} if kind=='blockMesh' else {}),
            **({'mesh_refinement_version':1} if refinement else {}),
            **({'generated_numerical':derived} if derived else {}),
            'worker_sha256':hashlib.sha256(WORKER.read_bytes()).hexdigest(),'input_publications':references},
            generated_inputs=generated or None)

    def validate_spec(self,spec):
        try:
            parts=shlex.split(spec['command'])
            if (len(parts)!=14 or parts[:1]!=['source'] or parts[2:6]!=['&&','exec','python3',win_to_wsl_path(WORKER)] or
                    parts[6::2]!=['--run-dir','--execution-id','--distro','--max-log-bytes'] or
                    not re.fullmatch(r'/usr/lib/openfoam/[A-Za-z0-9_.-]+/etc/bashrc',parts[1]) or
                    not re.fullmatch(r'[a-z0-9-]+',parts[11]) or safe_run_dir(parts[7])!=parts[7] or
                    str(uuid.UUID(parts[9]))!=parts[9] or int(parts[13])!=spec['max_log_bytes'] or
                    not openfoam_runtime.valid_transport(spec,parts[11]) or spec.get('evidence_run_dir') is not None):
                raise ValueError('Unexpected worker command')
        except (ValueError,KeyError,TypeError,IndexError) as error:
            raise ExecutionConflict('evidence_invalid','Mesh command differs from its registered worker') from error

    def artifacts(self,run,descriptor,output,frozen_files):
        return mesh_publication.artifacts_for(run,descriptor,output,frozen_files)

    def source(self,prefix,descriptor):
        return mesh_publication.source_snapshot(prefix,descriptor['run_dir'])
