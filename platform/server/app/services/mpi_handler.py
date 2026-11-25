"""Legacy-case MPI input isolation and immutable reconstructed results."""
import asyncio
import hashlib
from pathlib import Path, PurePosixPath
import re
import sys
import uuid

from . import mpi_artifacts as artifacts,attempt_workspace as workspace
from .async_ops import finish_before_cancel
from .case_numerical_method import declared_time_keys
from .casedict import CASE_DICT_PATH, parse_casedict
from .compose_artifacts import live_existing,record
from .case_read_view import inspect, delta_source
from .durable_events import json_text
from .geometry_handler import GeometryHandler
from .openfoam_numerical_inputs import DECOMPOSE, SOLUTION, declared_configuration, mpi_decomposition, native_inputs
from .solve_artifacts import control_values
from .step_handlers import PreparedStep
from . import openfoam_runtime, openfoam_source_runner
from .workflow_state import ExecutionConflict, InvalidStepInput

WORKER=Path(__file__).with_name('mpi_worker.py')
NATIVE=Path(__file__).with_name('mpi_native.py')
QUERIES=Path(__file__).with_name('case_file_queries.py')


def run_path(value):
    path=workspace.canonical_path(value)
    if path.split('/')[0].casefold()=='.vibeflow':raise ValueError('MPI cannot use runtime metadata as its case')
    return path


class MpiHandler:
    key='mpi.openfoam';version='1';job_type='solve';publication_kind='mpi'
    cancellation=dict(GeometryHandler.cancellation);recovery=dict(GeometryHandler.recovery)

    def publication_channel(self,descriptor):
        return 'mpi:'+hashlib.sha256(run_path(descriptor['run_dir']).casefold().encode()).hexdigest()[:40]

    async def prepare(self,db,case,user,*,parameters,budget):
        if set(parameters)-{'force'}!={'run_dir','solver','subdomains'}:raise ValueError('MPI requires run directory, solver and process count')
        run_dir=run_path(parameters['run_dir']);solver=parameters['solver'];count=parameters['subdomains']
        if not re.fullmatch('[A-Za-z][A-Za-z0-9_+-]*',solver) or type(count) is not int or not 1<=count<=64:raise ValueError('Invalid MPI solver/count')
        force = parameters.get('force', True)
        if type(force) is not bool:
            raise ValueError('MPI force must be a boolean')
        distro,bashrc=openfoam_runtime.configured()
        def read_inputs(root):
            control=workspace.checked_path(root,run_dir+'/system/controlDict')
            source=artifacts.snapshot(root)
            dictionary=run_dir+'/'+DECOMPOSE
            native=workspace.checked_path(root,dictionary).read_bytes() if dictionary in source['files'] else None
            document=workspace.checked_path(root,CASE_DICT_PATH).read_text(encoding='utf8')
            try:
                selection,text=mpi_decomposition(document,native,count)
                method=parse_casedict(document).get('numericalMethod') or {}
                keys=declared_time_keys(method)
                values=control_values(control.read_text(encoding='utf-8'),extra=keys) if keys else {}
                numerics=declared_configuration(root,run_dir,solver,method,values)
                solution=native_inputs(root,run_dir,solver,document).get(run_dir+'/'+SOLUTION)
            except ValueError as error:
                raise InvalidStepInput(str(error)) from error
            return source,workspace.live_roots(root),selection,text,numerics,solution
        source,paths,selection,text,numerics,solution=await finish_before_cancel(asyncio.to_thread(inspect,case.s3_prefix,read_inputs))
        if not force and any(PurePosixPath(path).parent.as_posix() == run_dir
                and re.fullmatch(r'processor[0-9]+', PurePosixPath(path).name)
                for path in source['directories']):
            raise ExecutionConflict('mpi_processor_exists', '求解目录已有 MPI 分区，请确认强制重新分解')
        from .quota import check_job_quota
        await check_job_quota(db,user, case_id=case.id)
        identifier=str(uuid.uuid4())
        configuration={'source':source,'execution_id':identifier,'run_dir':run_dir,'solver':solver,'subdomains':count,
            'distro':distro,'bashrc':bashrc,'decompose_text':text,'max_log_bytes':budget['max_log_bytes'],'pmix_gds':'hash',
            'native_sha256':hashlib.sha256(NATIVE.read_bytes()).hexdigest()}
        from .solve_handler import source_selection
        source_build=source_selection(solver)
        if source_build:
            configuration.update(source_build,source_loader_sha256=openfoam_source_runner.digest(Path(openfoam_source_runner.__file__)))
        if selection is not None:
            configuration['decomposition']=selection
        if numerics:
            configuration['numerics']=numerics
        if solution is not None:
            configuration['solution_text']=solution.decode('utf-8')
        if 'force' in parameters:
            configuration['force'] = force
        configuration['case_queries']={'schema':'vibeflow.case-file-queries.v1','helper_sha256':hashlib.sha256(QUERIES.read_bytes()).hexdigest()}
        spec={'command':'OpenFOAM MPI '+solver,'case_prefix':case.s3_prefix,'runtime':'windows',
            'argv':[sys.executable,str(WORKER),'--run-dir='+run_dir,'--solver='+solver,'--subdomains='+str(count),'--distro='+distro,
                '--bashrc='+bashrc,'--execution-id='+identifier,'--timeout='+str(budget['timeout_seconds']),
                '--max-log-bytes='+str(budget['max_log_bytes'])],
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        self.validate_spec(spec)
        descriptor={'handler':'mpi.v1','handler_version':self.version,'case_id':case.id,'run_dir':run_dir,'solver':solver,
            'subdomains':count,'source':source,'configuration':configuration,'execution_id':identifier,'distro':distro,
            'native_sha256':configuration['native_sha256'],'worker_sha256':hashlib.sha256(WORKER.read_bytes()).hexdigest()}
        return PreparedStep(self.key,self.version,self.job_type,spec,paths,None,self.publication_channel(descriptor),
            descriptor,generated_inputs={artifacts.CONFIG:json_text(configuration).encode('utf8')})

    def validate_spec(self,spec):
        try:
            args=spec['argv']
            if (spec.get('runtime')!='windows' or spec.get('evidence_run_dir') is not None
                    or not isinstance(args,list) or len(args)!=10 or args[:2]!=[sys.executable,str(WORKER)]):raise ValueError('Invalid worker')
            parts=dict(value.split('=',1) for value in args[2:])
            if (set(parts)!={'--run-dir','--solver','--subdomains','--distro','--bashrc','--execution-id','--timeout','--max-log-bytes'}
                    or run_path(parts['--run-dir'])!=parts['--run-dir'] or not re.fullmatch('[A-Za-z][A-Za-z0-9_+-]*',parts['--solver'])
                    or spec.get('command')!='OpenFOAM MPI '+parts['--solver'] or not 1<=int(parts['--subdomains'])<=64
                    or not re.fullmatch('[a-z0-9-]+',parts['--distro'])
                    or not re.fullmatch('/usr/lib/openfoam/[A-Za-z0-9_.-]+/etc/bashrc',parts['--bashrc'])
                    or str(uuid.UUID(parts['--execution-id']))!=parts['--execution-id']
                    or int(parts['--timeout'])!=spec['timeout_seconds'] or int(parts['--max-log-bytes'])!=spec['max_log_bytes']):
                raise ValueError('Invalid native parameters')
            if spec['timeout_seconds']<=0 or spec['max_log_bytes']<=0:raise ValueError('Invalid native budget')
        except (ValueError,KeyError,TypeError,AttributeError) as error:
            raise ExecutionConflict('evidence_invalid','MPI command differs from its registered contract') from error

    def source(self,prefix,descriptor):
        if descriptor.get('source',{}).get('schema')=='vibeflow.case-file-queries.v1':
            from .case_query_source import current
            return current(prefix,descriptor)
        return delta_source(prefix,descriptor)

    def before_publish(self,prefix,descriptor):
        before=artifacts.file_records(descriptor.get('origin_source',descriptor['source']))
        for path in set(descriptor['output_files'])|set(descriptor['deleted_files']):
            data=live_existing(prefix,path);actual=record(data) if data is not None else None
            if actual!=before.get(path):raise ValueError('MPI destination changed: '+path)

    def artifacts(self,run,descriptor,output,frozen_files):return artifacts.artifacts(run,descriptor,output,frozen_files)
