"""Two independent, registered report-draft output channels."""
import asyncio
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import sys
import uuid

from .. import storage
from . import compose_artifacts as artifacts,report_builder
from .async_ops import finish_before_cancel
from .case_evidence import safe_run_dir
from .geometry_handler import GeometryHandler
from .report_handler import document_inputs,validate_document_publication
from .runtime_config import get_runtime_setting
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict

WORKER=Path(__file__).with_name('registered_compose_worker.py')


class ComposeHandler:
    version='1'
    job_type='convert'
    cancellation=dict(GeometryHandler.cancellation)
    recovery=dict(GeometryHandler.recovery)

    def __init__(self,output):
        self.output=output
        self.key,self.publication_kind=artifacts.OUTPUTS[output]

    async def prepare(self,db,case,user,*,parameters,budget,draft_inputs=None,document_sources=None,
                      observations=None,resolved_inputs=None):
        if set(parameters)-{'run_dir','overwrite'} or type(parameters.get('overwrite',False)) is not bool:
            raise ExecutionConflict('invalid_request','Invalid composition parameters')
        run_dir=safe_run_dir(parameters['run_dir'])
        if document_sources is None:
            reader=db if observations is None else observations
            document_sources=await document_inputs(reader,case,run_dir)
        references,resolved,registered=document_sources
        if resolved_inputs is not None:
            resolved_inputs.extend(resolved)
        if draft_inputs is None:
            from .compose_input_preparation import inspect_live_source
            source,previous,has_text=await finish_before_cancel(asyncio.to_thread(
                inspect_live_source,case.s3_prefix,standalone=not registered,track_resolution=True,
                output=self.output,overwrite=parameters.get('overwrite',False)))
        else:
            if (draft_inputs.prefix,draft_inputs.run_dir,draft_inputs.output)!=(case.s3_prefix,run_dir,self.output):
                raise ExecutionConflict('input_stale','Report request differs from its prepared draft inputs')
            previous,has_text = draft_inputs.previous,draft_inputs.has_text
            source = draft_inputs.describe(registered)
        if has_text and not parameters.get('overwrite',False):
            raise ExecutionConflict('file_conflict','报告已有内容；请明确覆盖，系统会先备份旧稿')
        python=get_runtime_setting('python',sys.executable)
        if not shutil.which(python):
            raise ExecutionConflict('runtime_unavailable','Configured composition Python is unavailable')
        from .quota import check_job_quota
        await check_job_quota(db,user, case_id=case.id)
        execution_id=str(uuid.uuid4())
        generated_at=datetime.now(timezone.utc).isoformat()
        spec={'command':'Compose report','case_prefix':case.s3_prefix,'runtime':'windows',
            'argv':[python,str(WORKER),'--output',self.output,'--execution-id',execution_id,'--case-name='+case.name,
                '--generated-at='+generated_at],
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        self.validate_spec(spec)
        published={path for item in resolved for path in item.files}
        paths=['0-caseDict/caseDict',*[path for path in source['files'] if path not in published]]
        if previous is not None:
            paths.append(self.output)
        return PreparedStep(self.key,self.version,self.job_type,spec,paths,references or None,self.publication_kind,
            {'handler':'report-compose.v1','handler_version':self.version,'case_id':case.id,'run_dir':run_dir,
             'execution_id':execution_id,'output':self.output,'case_name':case.name,'source':source,
             'registered_postprocess':registered,'generated_at':generated_at,
             'input_publications':references,'generated_paths':[self.output],
             'target_before':previous,
             'worker_sha256':hashlib.sha256(WORKER.read_bytes()).hexdigest(),
             'composer_sha256':hashlib.sha256(Path(artifacts.__file__).read_bytes()).hexdigest(),
             'builder_sha256':hashlib.sha256(Path(report_builder.__file__).read_bytes()).hexdigest()})

    def validate_spec(self,spec):
        try:
            argv=spec['argv']
            if (spec.get('command')!='Compose report' or spec.get('runtime')!='windows' or
                    spec.get('evidence_run_dir') is not None or not isinstance(argv,list) or len(argv) not in {7,8} or
                    not isinstance(argv[0],str) or not argv[0] or argv[1:5]!=[str(WORKER),'--output',self.output,'--execution-id'] or
                    str(uuid.UUID(argv[5]))!=argv[5] or not isinstance(argv[6],str) or not argv[6].startswith('--case-name=')):
                raise ValueError('Unexpected composition worker')
            if len(argv)==8:
                artifacts.report_artifacts.generation_time(argv[7])
        except (KeyError,ValueError,TypeError,AttributeError,IndexError) as error:
            raise ExecutionConflict('evidence_invalid','Composition differs from its registered worker') from error

    def source(self,prefix,descriptor):
        from .compose_input_preparation import inspect_live_source
        source,_,_=inspect_live_source(prefix,standalone=not descriptor.get('registered_postprocess',True),
                                       track_resolution='resource_resolution' in descriptor['source'])
        return source

    async def validate_publication(self,db,case,descriptor):
        await validate_document_publication(db,case,descriptor)

    def artifacts(self,run,descriptor,output,frozen_files):
        return artifacts.artifacts_for(run,descriptor,output,frozen_files)

    def before_publish(self,prefix,descriptor):
        return artifacts.before_publish(prefix,descriptor)

    def write_aliases(self,prefix,row,manifest):
        return artifacts.write_aliases(prefix,row,manifest)
