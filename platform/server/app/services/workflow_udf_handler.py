"""Canonical workflow producers using the existing real UDF native pipeline."""
import asyncio,hashlib,re
from dataclasses import replace
from pathlib import Path

from .. import storage
from .async_ops import finish_before_cancel
from .case_read_view import inspect
from .composite_inputs import live_selection
from .udf_handler import UdfHandler
from . import attempt_workspace as workspace,solve_publication,udf_artifacts
from .workflow_state import ExecutionConflict,InvalidStepInput
from . import udf_dependencies, udf_profiles


def source_name(value):
    if not isinstance(value,str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,39}',value):
        raise ExecutionConflict('invalid_request','UDF name must identify one registered OpenFOAM source project')
    return value


def compiler_cache(path,name):
    return udf_dependencies.compiler_cache(path,name)


def build_source(prefix,name,compiler_policy=None):
    """Declared source tree and reference inputs, excluding native build cache.

    The native pipeline still seals its complete original case. These semantic
    inputs avoid treating compiled outputs or engineering confirmations as C++
    edits. Full external/system dependency closure remains separately declared.
    """
    name=source_name(name);base='UDF/'+name
    def read(root):
        source=udf_artifacts.snapshot(root)
        kind=udf_profiles.read(root,base)
        files={p:r for p,r in source['files'].items()
            if (p.startswith(base+'/') and not compiler_cache(p,name)) or p.startswith('Ref/')}
        for tail in (udf_profiles.PROFILES[kind].source,'Make/files','Make/options'):
            if base+'/'+tail not in files:raise ValueError('UDF source/Make input is missing')
        result={'schema':'vibeflow.workflow-udf-input.v1','name':name,'files':files}
        if kind!=udf_profiles.LEGACY:result['udf_kind']=kind
        if compiler_policy is not None:
            result['compiler']=udf_dependencies.projection(source['files'],name,compiler_policy,source['directories'])
        return result
    optional=['Ref']
    if compiler_policy is not None:
        optional+=compiler_policy['direct_paths']+compiler_policy['search_roots']+compiler_policy.get('enumerated_directories',[])
    if '.' in optional:return inspect(prefix,read)
    optional=sorted({p for p in optional if p!=base and not p.startswith(base+'/')})
    return inspect(prefix,read,paths=[base],optional_paths=optional)


class WorkflowUdfHandler(UdfHandler):
    def __init__(self,mode):
        super().__init__(mode)
        self.key='udf.build.workflow' if mode=='build' else 'solve.openfoam.udf'
        self.job_type='udf' if mode=='build' else 'solve'
        self.publication_kind='udf-build' if mode=='build' else 'solve'

    def publication_channel(self,descriptor):return self.publication_kind

    async def prepare_rerun(self,db,case,user,*,parameters,budget):
        return await self.prepare(db,case,user,parameters=parameters,budget=budget,_reuse_registration=self.mode=='integrate')

    async def prepare(self,db,case,user,*,parameters,budget,_reuse_registration=False,observations=None,resolved_inputs=None):
        allowed={'name','run_dir'}|({'mesh_publication_id','udf_build_publication_id'} if self.mode=='integrate' else set())
        if not {'name','run_dir'}<=set(parameters) or set(parameters)-allowed:
            raise ExecutionConflict('invalid_request','Workflow UDF requires its source name and run directory')
        name=source_name(parameters['name']);run_dir=parameters['run_dir']
        from .case_evidence import safe_run_dir
        run_dir=safe_run_dir(run_dir)
        seconds=budget['timeout_seconds']
        if type(seconds) not in {int,float} or seconds<1 or int(seconds)!=seconds:
            raise ExecutionConflict('invalid_request','UDF timeout must use positive whole seconds')
        declared=await finish_before_cancel(asyncio.to_thread(build_source,case.s3_prefix,name))
        references=[];mounted=[];scientific=None;resolved=None
        if self.mode=='integrate':
            from .artifact_publication import current,read_manifest
            from .publication_inputs import resolve
            from .mesh_publication import inspect_inputs,_source_snapshot
            from .solve_artifacts import configuration
            config,scientific=await finish_before_cancel(asyncio.to_thread(inspect_inputs,case.s3_prefix,
                lambda root,directory:(configuration(root,directory),_source_snapshot(root,directory)),run_dir))
            if 'pressure_reference' in config:
                raise InvalidStepInput('声明的压力参考值请使用串行求解')
            if 'conduction_material' in config:
                raise InvalidStepInput('声明的材料导热请使用串行求解')
            if 'fluid_model' in config or 'fluid_thermophysics' in config:
                raise InvalidStepInput('声明的标准流动模型请使用串行求解')
            if 'rotation' in config:
                raise InvalidStepInput('声明的 MRF 旋转区域请使用串行求解')
            if 'moving_frame' in config or 'deformation' in config:
                raise InvalidStepInput('声明的运动坐标系请使用串行求解')
            if 'continuum' in config:
                raise InvalidStepInput('声明的连续介质守恒方程请使用串行求解')
            solver=udf_profiles.PROFILES[declared.get('udf_kind',udf_profiles.LEGACY)].solver
            if config['solver']!=solver:
                raise ExecutionConflict('invalid_request','This UDF project requires '+solver)
            reader=db if observations is None else observations
            for kind,param in [('mesh','mesh_publication_id'),('udf-build','udf_build_publication_id')]:
                _,row=await current(reader,case.id,kind)
                if row is None or parameters.get(param,row.id)!=row.id:
                    raise ExecutionConflict('input_stale','Workflow UDF '+kind+' is no longer current')
                if observations is not None and storage._use_s3():
                    from .publication_objects import bound_metadata
                    manifest=await finish_before_cancel(asyncio.to_thread(bound_metadata,case.s3_prefix,row))
                else:
                    _,manifest=await finish_before_cancel(asyncio.to_thread(read_manifest,case.s3_prefix,row))
                descriptor=manifest['descriptor']
                if kind=='mesh':
                    if descriptor['run_dir']!=run_dir:raise ExecutionConflict('input_stale','UDF mesh belongs to another run')
                    paths=sorted(p for p in manifest['files'] if p.startswith(run_dir+'/constant/polyMesh/'))
                else:
                    if descriptor.get('handler')!='workflow-udf-build.v1' or descriptor.get('name')!=name:
                        raise ExecutionConflict('input_stale','UDF library belongs to another source producer')
                    paths=['UDF/'+name+'/'+udf_profiles.artifact(descriptor['native']['configuration'])]
                    if (descriptor['source'].get('compiler',{}).get('policy',{}).get('query_schema')!=udf_dependencies.QUERY_SCHEMA):
                        raise ExecutionConflict('input_stale','Rebuild the UDF library to record its actual compiler inputs')
                    declared=descriptor['source']
                if not paths or any(p not in manifest['files'] for p in paths):
                    raise ExecutionConflict('evidence_invalid','Workflow UDF producer has no required files')
                references.append({'publication_id':row.id,'paths':paths,'require_current':True});mounted+=paths
            resolved=await resolve(reader,case,references)
            if resolved_inputs is not None:
                resolved_inputs.extend(resolved)
        prepared=await super().prepare(db,case,user,parameters={'name':name,'run_dir':run_dir if self.mode=='integrate' else ''},
            budget={**budget,'timeout_seconds':int(seconds)},_reuse_registration=_reuse_registration,
            _input_publications=resolved)
        native=prepared.descriptor
        descriptor={'handler':'workflow-udf-build.v1' if self.mode=='build' else 'workflow-udf.v1',
            'handler_version':self.version,'case_id':case.id,'mode':self.mode,'name':name,
            'run_dir':run_dir if self.mode=='integrate' else None,'execution_id':native['execution_id'],
            'source':declared if self.mode=='build' else {'solver':scientific,'udf':declared},
            'input_publications':references,'native':native,
            **{k:native[k] for k in ('worker_sha256','native_sha256','common_native_sha256','distro')}}
        return replace(prepared,key=self.key,job_type=self.job_type,publication_kind=self.publication_kind,
            input_paths=live_selection(native['source'],mounted) if mounted else prepared.input_paths,
            input_publications=references or None,descriptor=descriptor)

    def source(self,prefix,descriptor):
        expected=descriptor['source'] if self.mode=='build' else descriptor['source']['udf']
        compiler=expected.get('compiler')
        declared=build_source(prefix,descriptor['name'],compiler['policy'] if compiler is not None else None)
        if compiler is None and not descriptor['native']['configuration'].get('dependency_tracking'):
            # Old manifests remain readable history. Explicit recomputation
            # must rebuild a producer that never recorded compiler provenance.
            declared['compiler_provenance_missing']=True
        elif compiler is not None and compiler['policy'].get('query_schema')!=udf_dependencies.QUERY_SCHEMA:
            declared['file_query_provenance_missing']=True
        if self.mode=='build':return declared
        result={'solver':solve_publication.source_snapshot(prefix,descriptor['run_dir']),'udf':declared}
        if descriptor['source'].get('schema')=='vibeflow.workflow-udf-source.v2':
            from .case_query_source import current
            pinned={p for ref in descriptor['input_publications'] for p in ref['paths']}
            result.update(schema='vibeflow.workflow-udf-source.v2',native_case=current(prefix,descriptor['native'],
                pinned_paths=pinned,retired_aliases=descriptor.get('retired_aliases')))
        return result

    def artifacts(self,run,descriptor,output,frozen_files):
        native=descriptor['native'];records=super().artifacts(run,native,output,frozen_files)
        if self.mode=='build':
            proof=native['receipt']['native'].get('compiler_dependencies')
            tracking=native['configuration'].get('dependency_tracking')
            if tracking:
                if proof is None or proof.get('helper_sha256')!=tracking['helper_sha256'] or proof['schema']!=udf_dependencies.SCHEMA:
                    raise ValueError('Workflow UDF has no bound actual compiler provenance')
                if hashlib.sha256(Path(udf_dependencies.__file__).read_bytes()).hexdigest()!=tracking['helper_sha256']:
                    raise ValueError('UDF dependency helper changed during execution')
                expected=udf_dependencies.projection(native['source']['files'],descriptor['name'],proof['policy'],native['source']['directories'])
                if any(proof.get(k)!=expected.get(k) for k in ['files','entries','listings']):raise ValueError('UDF compiler provenance differs from sealed source')
                descriptor['source']['compiler']=expected
            base='UDF/'+descriptor['name']+'/'
            required={base+name for name in (udf_profiles.artifact(native['configuration']),'udf-build.log','udf-load.log')}
            selected={p:r for p,r in records.items() if p in required}
            if set(selected)!=required:
                raise ValueError('Workflow UDF build is missing its native artifact or logs')
            deleted={}
        else:
            if native['source'].get('schema')=='vibeflow.case-file-queries.v1':
                descriptor['source'].update(schema='vibeflow.workflow-udf-source.v2',native_case=native['source'])
            from .mpi_artifacts import is_input
            selected={p:r for p,r in records.items() if p.startswith(descriptor['run_dir']+'/') and not is_input(p,descriptor['run_dir'])}
            if descriptor['run_dir']+'/udf-evidence.json' not in selected:
                raise ValueError('Workflow UDF solve has no truthful UDF evidence')
            deleted={p:r for p,r in native['deleted_files'].items() if p.startswith(descriptor['run_dir']+'/') and not is_input(p,descriptor['run_dir'])}
        descriptor['output_files']=selected;descriptor['deleted_files']=deleted
        return selected

    def before_publish(self,prefix,descriptor):
        # The original validator checks exactly the outputs that this adapter
        # publishes; compiler caches and derived controls stay private.
        native={**descriptor['native'],'output_files':descriptor['output_files'],'deleted_files':descriptor['deleted_files']}
        return super().before_publish(prefix,native)
