"""Registered extraction pinned to the workflow's confirmed solver publication."""
import asyncio
import hashlib
from pathlib import Path, PurePosixPath
import shutil
import sys
import uuid

from .. import storage
from . import postprocess_executor as executor, postprocess_publication
from .async_ops import finish_before_cancel
from .case_evidence import safe_run_dir
from .geometry_handler import GeometryHandler
from .mesh_artifacts import selection
from .postprocess_artifacts import configuration, reference_paths
from .postprocess_reference_inputs import previous_selections
from .runtime_config import get_runtime_setting
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict

WORKER=Path(__file__).with_name('postprocess_worker.py')


class PostprocessHandler:
    key='postprocess.run'
    version='1'
    job_type='postprocess'
    publication_kind='postprocess'
    cancellation=dict(GeometryHandler.cancellation)
    recovery=dict(GeometryHandler.recovery)

    async def prepare(self,db,case,user,*,parameters,budget,observations=None,resolved_inputs=None):
        if 'run_dir' not in parameters or set(parameters)-{'run_dir','solve_publication_id'}:
            raise ExecutionConflict('invalid_request','Postprocessing requires its registered run directory')
        run_dir=safe_run_dir(parameters['run_dir'])
        def read_inputs(root):
            return configuration(root,run_dir),selection(root,run_dir),postprocess_publication._source_snapshot(root,run_dir)
        config,paths,source=await finish_before_cancel(asyncio.to_thread(
            postprocess_publication.inspect_inputs,case.s3_prefix,run_dir,read_inputs,include_run_inputs=True))
        python=get_runtime_setting('python',sys.executable)
        if not shutil.which(python):
            raise ExecutionConflict('runtime_unavailable','Configured extraction Python is unavailable')
        from .artifact_publication import current,read_manifest
        from .publication_inputs import resolve
        reader=db if observations is None else observations
        _,solve=await current(reader,case.id,'solve')
        if solve is None or parameters.get('solve_publication_id',solve.id)!=solve.id:
            raise ExecutionConflict('input_stale','Confirmed workflow solve is no longer current')
        if storage._use_s3():
            from .publication_objects import bound_metadata

            # The selected solve bundle is verified below with the other
            # dependencies. Only its authenticated descriptor is needed here.
            manifest=await finish_before_cancel(asyncio.to_thread(bound_metadata,case.s3_prefix,solve))
        else:
            _,manifest=await finish_before_cancel(asyncio.to_thread(read_manifest,case.s3_prefix,solve))
        descriptor=manifest['descriptor']
        if descriptor.get('handler') not in {'solve.v1','solve.resume.v1','workflow-mpi.v1','workflow-udf.v1'} or descriptor['run_dir']!=run_dir:
            raise ExecutionConflict('input_stale','Solver version belongs to another run/handler')
        references=[{'publication_id':solve.id,'paths':None,'require_current':True},*descriptor['input_publications']]
        references.extend(await previous_selections(reader,case,config))
        resolved=await resolve(reader,case,references)
        if resolved_inputs is not None:
            resolved_inputs.extend(resolved)
        published={path for item in resolved for path in item.files}
        for path in reference_paths(config):
            if path not in published and not any(PurePosixPath(path).is_relative_to(base) for base in paths):
                paths.append(path)
        from .quota import check_job_quota
        await check_job_quota(db,user, case_id=case.id)
        execution_id=str(uuid.uuid4())
        spec={'command':'OpenFOAM postprocess','case_prefix':case.s3_prefix,'runtime':'windows',
            'argv':[python,str(WORKER),'--run-dir',run_dir,'--execution-id',execution_id],
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        self.validate_spec(spec)
        return PreparedStep(self.key,self.version,self.job_type,spec,paths,references,self.publication_kind,
            {'handler':'postprocess.v1','handler_version':self.version,'case_id':case.id,'run_dir':run_dir,
             'execution_id':execution_id,'source':source,'input_publications':references,
             'worker_sha256':hashlib.sha256(WORKER.read_bytes()).hexdigest(),
             'executor_sha256':hashlib.sha256(Path(executor.__file__).read_bytes()).hexdigest()})

    def validate_spec(self,spec):
        try:
            argv=spec['argv']
            if (spec.get('command')!='OpenFOAM postprocess' or spec.get('runtime')!='windows' or
                    spec.get('evidence_run_dir') is not None or not isinstance(argv,list) or len(argv)!=6 or
                    not isinstance(argv[0],str) or not argv[0] or argv[1:3]!=[str(WORKER),'--run-dir'] or
                    argv[4]!='--execution-id' or safe_run_dir(argv[3])!=argv[3] or str(uuid.UUID(argv[5]))!=argv[5]):
                raise ValueError('Unexpected extraction worker')
        except (KeyError,TypeError,ValueError,AttributeError,IndexError) as error:
            raise ExecutionConflict('evidence_invalid','Postprocessing differs from its registered worker') from error

    def artifacts(self,run,descriptor,output,frozen_files):
        return postprocess_publication.artifacts_for(run,descriptor,output,frozen_files)

    def source(self,prefix,descriptor):
        return postprocess_publication.source_snapshot(prefix,descriptor['run_dir'])
