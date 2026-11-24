"""Registered UDF build/integration with delayed, guarded publication."""
import asyncio
import hashlib
import math
from pathlib import Path
import re
import sys
import uuid

from . import udf_artifacts as artifacts,attempt_workspace as workspace,udf_profiles
from .async_ops import finish_before_cancel
from .compose_artifacts import live_existing,record
from .case_read_view import inspect, delta_source
from .durable_events import json_text
from .geometry_handler import GeometryHandler
from .step_handlers import PreparedStep
from . import openfoam_runtime
from .workflow_state import ExecutionConflict
from .case_evidence import safe_run_dir
from .udf_control import insert_function,insert_library
from .casedict import parse_casedict
from .openfoam_fluid_model import ras_model

WORKER=Path(__file__).with_name('udf_worker.py')
NATIVE=Path(__file__).with_name('udf_native.py')
COMMON=Path(__file__).with_name('mpi_native.py')
DEPENDENCIES=Path(__file__).with_name('udf_dependencies.py')


def boundary_control(root,run_dir,control,library):
    path=workspace.checked_path(root,run_dir+'/0/U')
    if path.stat().st_size>1024*1024:
        raise ValueError('UDF 速度初值超过 1 MiB')
    boundaries=parse_casedict(path.read_text(encoding='utf-8')).get('boundaryField')
    if not isinstance(boundaries,dict) or not any(isinstance(value,dict) and value.get('type')=='vibeflowFixedVelocity'
                                                  for value in boundaries.values()):
        raise ValueError('UDF 速度边界需要在 0/U 指定 vibeflowFixedVelocity patch')
    return insert_library(control,library)


def source_control(root,run_dir,control,library):
    paths=[run_dir+'/'+name for name in ('constant/fvOptions','system/fvOptions') if (root/run_dir/name).is_file()]
    if len(paths)!=1:
        raise ValueError('动量源项需要唯一的原生 fvOptions 字典')
    path=workspace.checked_path(root,paths[0])
    if path.stat().st_size>1024*1024:
        raise ValueError('UDF fvOptions 超过 1 MiB')
    values=parse_casedict(path.read_text(encoding='utf-8'))
    options=values.get('options',values)
    if not isinstance(options,dict) or not any(isinstance(entry,dict) and entry.get('type')=='vibeflowUniformAcceleration'
            and str(entry.get('active','true')).lower() in {'true','yes','on','1'} for entry in options.values()):
        raise ValueError('fvOptions 需要启用 vibeflowUniformAcceleration')
    return insert_library(control,library)


def turbulence_control(root,run_dir,control,library):
    path=workspace.checked_path(root,run_dir+'/constant/turbulenceProperties')
    if path.stat().st_size>1024*1024:
        raise ValueError('UDF turbulenceProperties 超过 1 MiB')
    properties=parse_casedict(path.read_text(encoding='utf-8'))
    ras=properties.get('RAS')
    if (properties.get('simulationType')!='RAS' or not isinstance(ras,dict)
            or ras_model(ras)!='vibeflowKEpsilon'
            or str(ras.get('turbulence','')).lower() not in {'true','yes','on','1'}):
        raise ValueError('turbulenceProperties 需要启用 RAS 模型 vibeflowKEpsilon')
    coefficients=ras.get('vibeflowKEpsilonCoeffs')
    try:
        effective_cmu=float(coefficients['effectiveCmu'])
    except (KeyError,TypeError,ValueError) as error:
        raise ValueError('vibeflowKEpsilon 需要有限的正数 effectiveCmu') from error
    if not math.isfinite(effective_cmu) or effective_cmu<=0:
        raise ValueError('vibeflowKEpsilon 需要有限的正数 effectiveCmu')
    return insert_library(control,library)


LIBRARY_CONTROLS={udf_profiles.BOUNDARY:boundary_control,udf_profiles.SOURCE:source_control,
                  udf_profiles.TURBULENCE:turbulence_control}


class UdfHandler:
    version='1';job_type='udf';publication_kind='udf'
    cancellation=dict(GeometryHandler.cancellation);recovery=dict(GeometryHandler.recovery)

    def __init__(self,mode):
        if mode not in {'build','integrate'}:raise ValueError('Unsupported UDF mode')
        self.mode=mode;self.key='udf.'+mode

    def publication_channel(self,descriptor):
        identity=self.mode+':'+descriptor['name']+':'+descriptor['run_dir'].casefold()
        return 'udf:'+hashlib.sha256(identity.encode()).hexdigest()[:40]

    async def prepare_rerun(self,db,case,user,*,parameters,budget):
        return await self.prepare(db,case,user,parameters=parameters,budget=budget,
            _reuse_registration=self.mode=='integrate')

    async def prepare(self,db,case,user,*,parameters,budget,_reuse_registration=False,_input_publications=None):
        if set(parameters)!={'name','run_dir'} or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,39}',parameters['name']):
            raise ValueError('Invalid UDF parameters')
        name=parameters['name'];run_dir=safe_run_dir(parameters['run_dir']) if self.mode=='integrate' else ''
        if self.mode=='build' and parameters['run_dir']:raise ValueError('Build does not select a solve directory')
        distro,bashrc=openfoam_runtime.configured()
        udf_dir='UDF/'+name
        def read_inputs(root):
            source=artifacts.snapshot(root)
            profile=udf_profiles.read(root,udf_dir)
            model=udf_profiles.PROFILES[profile]
            mounted={}
            if _input_publications:
                from .composite_inputs import overlay_snapshot
                source=overlay_snapshot(source,_input_publications)
                mounted={p:publication.root for publication in _input_publications for p in publication.files}
            control_text=None
            if self.mode=='build':
                for path in (model.source,'Make/files','Make/options'):workspace.checked_path(root,udf_dir+'/'+path)
            else:
                artifact=udf_dir+'/'+model.artifact
                workspace.checked_path(mounted.get(artifact,root),artifact)
                control=workspace.checked_path(root,run_dir+'/system/controlDict').read_text(encoding='utf8')
                try:
                    if profile==udf_profiles.LEGACY:
                        control_text=insert_function(control,name,reuse_identical=_reuse_registration)
                    else:
                        from .solve_artifacts import control_values
                        if control_values(control).get('application')!=model.solver:
                            raise ValueError('此 UDF 项目需要原生 '+model.solver)
                        if profile==udf_profiles.SOLVER:
                            control_text=control
                        else:
                            setup=LIBRARY_CONTROLS[profile]
                            control_text=setup(root,run_dir,control,model.artifact)
                except FileExistsError as error:
                    raise ExecutionConflict('function_exists',str(error)) from error
            return source,control_text,workspace.live_roots(root),profile
        source,control_text,paths,profile=await finish_before_cancel(asyncio.to_thread(inspect,case.s3_prefix,read_inputs))
        from .quota import check_job_quota
        await check_job_quota(db,user, case_id=case.id)
        identifier=str(uuid.uuid4())
        config={'source':source,'execution_id':identifier,'mode':self.mode,'name':name,'run_dir':run_dir,'udf_dir':udf_dir,
            'distro':distro,'bashrc':bashrc,'control_text':control_text,'max_log_bytes':budget['max_log_bytes'],
            'native_sha256':hashlib.sha256(NATIVE.read_bytes()).hexdigest(),'common_native_sha256':hashlib.sha256(COMMON.read_bytes()).hexdigest()}
        if profile!=udf_profiles.LEGACY:
            config['udf_kind']=profile
        config['dependency_tracking']={'schema':'vibeflow.udf-compiler-inputs.v1',
            'helper_sha256':hashlib.sha256(DEPENDENCIES.read_bytes()).hexdigest(),'file_queries':True}
        if self.mode=='integrate':
            from . import case_file_queries
            config['case_queries']={'schema':case_file_queries.SCHEMA,
                'helper_sha256':hashlib.sha256(Path(case_file_queries.__file__).read_bytes()).hexdigest()}
            config['load_log']=run_dir+'/udf-load.log'
        spec={'command':'OpenFOAM UDF '+self.mode+' '+name,'case_prefix':case.s3_prefix,'runtime':'windows',
            'argv':[sys.executable,str(WORKER),'--mode='+self.mode,'--name='+name,'--run-dir='+run_dir,'--distro='+distro,
                '--bashrc='+bashrc,'--execution-id='+identifier,'--timeout='+str(budget['timeout_seconds']),
                '--max-log-bytes='+str(budget['max_log_bytes'])],
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        self.validate_spec(spec)
        descriptor={'handler':'udf.v1','handler_version':self.version,'case_id':case.id,'mode':self.mode,'name':name,
            'run_dir':run_dir,'source':source,'configuration':config,'execution_id':identifier,'distro':distro,
            'native_sha256':config['native_sha256'],'common_native_sha256':config['common_native_sha256'],
            'worker_sha256':hashlib.sha256(WORKER.read_bytes()).hexdigest()}
        return PreparedStep(self.key,self.version,self.job_type,spec,paths,None,self.publication_channel(descriptor),
            descriptor,generated_inputs={artifacts.CONFIG:json_text(config).encode('utf8')})

    def validate_spec(self,spec):
        try:
            args=spec['argv']
            if (spec.get('runtime')!='windows' or spec.get('evidence_run_dir') is not None or not isinstance(args,list)
                    or len(args)!=10 or args[:2]!=[sys.executable,str(WORKER)]):raise ValueError('Invalid UDF worker')
            parts=dict(value.split('=',1) for value in args[2:])
            if (set(parts)!={'--mode','--name','--run-dir','--distro','--bashrc','--execution-id','--timeout','--max-log-bytes'}
                    or parts['--mode']!=self.mode or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,39}',parts['--name'])
                    or spec['command']!='OpenFOAM UDF '+self.mode+' '+parts['--name']
                    or not re.fullmatch('[a-z0-9-]+',parts['--distro'])
                    or not re.fullmatch('/usr/lib/openfoam/[A-Za-z0-9_.-]+/etc/bashrc',parts['--bashrc'])
                    or str(uuid.UUID(parts['--execution-id']))!=parts['--execution-id']
                    or int(parts['--timeout'])!=spec['timeout_seconds'] or int(parts['--max-log-bytes'])!=spec['max_log_bytes']
                    or spec['timeout_seconds']<=0 or spec['max_log_bytes']<=0):raise ValueError('Invalid UDF execution contract')
            if self.mode=='integrate':
                if safe_run_dir(parts['--run-dir'])!=parts['--run-dir']:raise ValueError('Invalid UDF run directory')
            elif parts['--run-dir']:raise ValueError('Build cannot select solve directory')
        except (ValueError,KeyError,TypeError,AttributeError) as error:
            raise ExecutionConflict('evidence_invalid','UDF command differs from registered contract') from error

    def source(self,prefix,descriptor):
        if descriptor['source'].get('schema')=='vibeflow.case-file-queries.v1':
            from .case_query_source import current
            return current(prefix,descriptor)
        return delta_source(prefix,descriptor)

    def before_publish(self,prefix,descriptor):
        before=artifacts.file_records(descriptor.get('origin_source',descriptor['source']))
        for path in set(descriptor['output_files'])|set(descriptor['deleted_files']):
            data=live_existing(prefix,path)
            if (record(data) if data is not None else None)!=before.get(path):raise ValueError('UDF destination changed: '+path)

    def artifacts(self,run,descriptor,output,frozen):return artifacts.artifacts(run,descriptor,output,frozen)
