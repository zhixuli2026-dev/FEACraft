"""Seal existing OpenFOAM files without inventing a confirmed solver lineage."""
import asyncio
import hashlib
from pathlib import Path, PurePosixPath
import shutil
import sys
import uuid

from ..models import ArtifactPublicationHead
from . import postprocess_executor as executor, postprocess_publication
from .async_ops import finish_before_cancel
from .attempt_workspace import checked_path, inventory
from .compose_artifacts import live_existing, record
from .case_evidence import safe_run_dir
from .postprocess_artifacts import configuration, reference_paths
from .postprocess_handler import PostprocessHandler, WORKER
from .postprocess_reference_inputs import previous_selections
from .runtime_config import get_runtime_setting
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict


class LegacyPostprocessHandler(PostprocessHandler):
    key='postprocess.case'

    async def prepare(self,db,case,user,*,parameters,budget,observations=None,resolved_inputs=None):
        if set(parameters)!={'run_dir'}:
            raise ExecutionConflict('invalid_request','Case extraction requires its source run directory')
        reader=db if observations is None else observations
        if await reader.get(ArtifactPublicationHead,(case.id,'solve')) is not None:
            raise ExecutionConflict('input_stale','A registered solver requires its publication-backed extraction')
        run_dir=safe_run_dir(parameters['run_dir'])
        def read_inputs(root):
            if not checked_path(root,run_dir+'/system/controlDict').is_file():
                raise ValueError('Postprocessing requires an OpenFOAM controlDict')
            targets=inventory(root,['D-post']) if (root/'D-post').exists() else {}
            return (configuration(root,run_dir,case_inputs=True),
                postprocess_publication._source_snapshot(root,run_dir,case_inputs=True),
                {p:{k:r[k] for k in ('size','sha256')} for p,r in targets.items()})
        config,source,targets=await finish_before_cancel(asyncio.to_thread(postprocess_publication.inspect_inputs,
            case.s3_prefix,run_dir,read_inputs,case_inputs=True,include_postprocess_targets=True))
        references=await previous_selections(reader,case,config)
        if references and resolved_inputs is not None:
            from .publication_inputs import resolve

            resolved_inputs.extend(await resolve(reader,case,references))
        paths=['0-caseDict/caseDict',run_dir]
        for path in reference_paths(config):
            if not any(PurePosixPath(path).is_relative_to(base) for base in paths):paths.append(path)
        python=get_runtime_setting('python',sys.executable)
        if not shutil.which(python):
            raise ExecutionConflict('runtime_unavailable','Configured extraction Python is unavailable')
        from .quota import check_job_quota
        await check_job_quota(db,user, case_id=case.id)
        execution_id=str(uuid.uuid4())
        spec={'command':'OpenFOAM postprocess','case_prefix':case.s3_prefix,'runtime':'windows',
            'argv':[python,str(WORKER),'--run-dir',run_dir,'--execution-id',execution_id,'--case-inputs'],
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        self.validate_spec(spec)
        return PreparedStep(self.key,self.version,self.job_type,spec,paths,references or None,self.publication_kind,
            {'handler':'postprocess-case.v1','handler_version':self.version,'case_id':case.id,'run_dir':run_dir,
             'execution_id':execution_id,'source':source,'input_publications':references,'targets_before':targets,
             'worker_sha256':hashlib.sha256(WORKER.read_bytes()).hexdigest(),
             'executor_sha256':hashlib.sha256(Path(executor.__file__).read_bytes()).hexdigest()})

    def validate_spec(self,spec):
        args=spec.get('argv')
        if not isinstance(args,list) or len(args)!=7 or args[-1]!='--case-inputs':
            raise ExecutionConflict('evidence_invalid','Case extraction requires its registered worker mode')
        super().validate_spec({**spec,'argv':args[:-1]})

    def source(self,prefix,descriptor):
        return postprocess_publication.source_snapshot(prefix,descriptor['run_dir'],case_inputs=True)

    def artifacts(self,run,descriptor,output,frozen_files):
        records=super().artifacts(run,descriptor,output,frozen_files)
        descriptor['output_files']=records
        return records

    def before_publish(self,prefix,descriptor):
        # Preserve compatibility with durable descriptors created before the
        # destination precondition was recorded. New attempts always carry it.
        if 'targets_before' not in descriptor:return
        for path in descriptor['output_files']:
            data=live_existing(prefix,path)
            if (record(data) if data is not None else None)!=descriptor['targets_before'].get(path):
                raise ValueError('Postprocessing output destination changed: '+path)

    async def validate_publication(self,db,case,descriptor):
        if await db.get(ArtifactPublicationHead,(case.id,'solve')) is not None:
            raise ValueError('Registered solver replaced the legacy file lineage; extract its current version')
