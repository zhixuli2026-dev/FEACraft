"""Registered latexmk execution with a separate durable channel for each main."""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import time
import uuid

from sqlalchemy import select

from .. import storage
from ..models import ArtifactPublication,ArtifactPublicationHead
from . import latex_artifacts as artifacts
from .async_ops import finish_before_cancel
from .geometry_handler import GeometryHandler
from .runtime_config import get_runtime_setting
from .step_handlers import PreparedStep
from . import document_admission as admission
from .workflow_state import ExecutionConflict

WORKER=Path(__file__).with_name('registered_latex_worker.py')


class LatexHandler:
    key='latex.build'
    version='1'
    job_type='convert'
    publication_kind='latex'
    cancellation=dict(GeometryHandler.cancellation)
    recovery=dict(GeometryHandler.recovery)

    def publication_channel(self,descriptor):
        return artifacts.channel(descriptor['main'])

    async def prepare(self,db,case,user,*,parameters,budget,observations=None,resolved_inputs=None):
        if set(parameters)-{'input_aliases'}!={'main','recipe','resources'} or parameters['recipe'] not in artifacts.RECIPES:
            raise ExecutionConflict('invalid_request','LaTeX requires a main source and a registered recipe')
        main=artifacts.main_path(parameters['main'])
        resources=artifacts.normalize_resources(parameters['resources'])
        aliases=artifacts.normalize_input_aliases(parameters.get('input_aliases',{}))
        from .postprocess_gate import authoritative_reason
        reader=db if observations is None else observations
        reason=await authoritative_reason(reader,case)
        if reason:
            raise ExecutionConflict('input_stale',reason)
        references,resolved=[],[]
        registered=await reader.get(ArtifactPublicationHead,(case.id,'postprocess')) is not None
        if registered:
            from .report_handler import document_inputs
            from .casedict import parse_casedict
            from .postprocess_executor import config_from_case_dict
            raw=await finish_before_cancel(asyncio.to_thread(storage.get_bytes,case.s3_prefix,'0-caseDict/caseDict'))
            run_dir=config_from_case_dict(parse_casedict(raw.decode('utf-8')))['run_dir']
            references,resolved,registered=await document_inputs(reader,case,run_dir)
        if resolved_inputs is not None:
            resolved_inputs.extend(resolved)
        published={path for item in resolved for path in item.files}
        from .case_read_view import inspect
        def read_inputs(root):
            if not (root/main).is_file():raise FileNotFoundError(main)
            source=artifacts.source(root,main,resources,input_aliases=aliases,gate_prefix=case.s3_prefix)
            return source,artifacts.selection(root,source['files'],published)
        source,selected=await finish_before_cancel(asyncio.to_thread(inspect,case.s3_prefix,read_inputs))
        python=get_runtime_setting('python',sys.executable)
        from .openfoam_runtime import distribution,NATIVE_LINUX
        distro=distribution()
        if not shutil.which(python) or (distro!=NATIVE_LINUX and not shutil.which('wsl.exe')):
            raise ExecutionConflict('runtime_unavailable','Configured Python or native execution runtime is unavailable')
        runtime=None  # Native probing runs after this selection transaction ends.
        from .quota import check_job_quota
        await check_job_quota(db,user, case_id=case.id)
        execution_id=str(uuid.uuid4())
        epoch=int(time.time())
        spec={'command':'Registered latexmk','case_prefix':case.s3_prefix,'runtime':'windows',
            'argv':[python,str(WORKER),'--main='+main,'--recipe='+parameters['recipe'],'--distro='+distro,
                '--execution-id='+execution_id,'--epoch='+str(epoch),'--max-log-bytes='+str(budget['max_log_bytes'])],
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        spec['argv'].append('--resources-json='+json.dumps(resources,ensure_ascii=False))
        if aliases:
            spec['argv'].append('--input-aliases-json='+json.dumps(aliases,ensure_ascii=False))
        self.validate_spec(spec)
        kind=artifacts.channel(main)
        retired={}
        from .artifact_publication import read_manifest
        previous=await reader.scalars(select(ArtifactPublication).where(ArtifactPublication.case_id==case.id,
            ArtifactPublication.kind==kind,ArtifactPublication.status=='committed')
            .order_by(ArtifactPublication.generation.desc()).limit(50))
        for row in previous:
            try:
                _,manifest=await finish_before_cancel(asyncio.to_thread(read_manifest,case.s3_prefix,row))
                for path,record in manifest['files'].items():
                    if path==artifacts.paths(main)['bbl']:
                        retired.setdefault(path,record)
            except (OSError,ValueError,KeyError):
                continue
        prepared=PreparedStep(self.key,self.version,self.job_type,spec,selected,references or None,kind,
            {'handler':'latex.v1','handler_version':self.version,'case_id':case.id,'main':main,
             'recipe':parameters['recipe'],'distro':distro,'epoch':epoch,'execution_id':execution_id,'resources':resources,
             'source':source,'input_publications':references,'registered_postprocess':registered,
             'runtime':runtime,'max_log_bytes':budget['max_log_bytes'],'retired_aliases':retired,
             **({'input_aliases':aliases} if aliases else {}),
             'worker_sha256':hashlib.sha256(WORKER.read_bytes()).hexdigest(),
             'compiler_sha256':hashlib.sha256(Path(artifacts.__file__).read_bytes()).hexdigest(),
             'native_sha256':hashlib.sha256(artifacts.NATIVE.read_bytes()).hexdigest()})
        return admission.pending(prepared,case,parameters,budget)

    async def complete_preparation(self,prepared,case_id,user_id):
        from .native_request_execution import scope
        descriptor=prepared.descriptor
        from .case_read_view import async_view
        async with async_view(prepared.spec['case_prefix'],paths=['0-caseDict/caseDict']) as root:
            async with scope(case_id,user_id,root,'latex.prepare',
                    {'distro':descriptor['distro'],'recipe':descriptor['recipe']}) as executor:
                runtime=await executor.run_sync(artifacts.probe,descriptor['distro'],descriptor['recipe'])
        return admission.ready(prepared,descriptor={**descriptor,'runtime':runtime})

    def validate_spec(self,spec):
        try:
            argv=spec['argv']
            if (spec.get('command')!='Registered latexmk' or spec.get('runtime')!='windows' or
                    spec.get('evidence_run_dir') is not None or not isinstance(argv,list) or len(argv) not in (9,10) or
                    not isinstance(argv[0],str) or not argv[0] or argv[1]!=str(WORKER)):
                raise ValueError('Unexpected LaTeX worker')
            values=dict(part.split('=',1) for part in argv[2:])
            if (len(values)!=len(argv)-2 or set(values)-{'--input-aliases-json'}!={'--main','--recipe','--distro','--execution-id','--epoch','--max-log-bytes','--resources-json'} or
                    artifacts.main_path(values['--main'])!=values['--main'] or values['--recipe'] not in artifacts.RECIPES or
                    not re.fullmatch(r'[a-z0-9-]+',values['--distro']) or
                    str(uuid.UUID(values['--execution-id']))!=values['--execution-id'] or
                    int(values['--epoch'])<0 or int(values['--max-log-bytes'])!=spec['max_log_bytes']):
                raise ValueError('Unexpected LaTeX parameters')
            resources=json.loads(values['--resources-json'])
            if artifacts.normalize_resources(resources)!=resources:
                raise ValueError('LaTeX resources are not normalized')
            aliases=json.loads(values.get('--input-aliases-json','{}'))
            if artifacts.normalize_input_aliases(aliases)!=aliases:
                raise ValueError('LaTeX input aliases are not normalized')
        except (ValueError,KeyError,TypeError,AttributeError) as error:
            raise ExecutionConflict('evidence_invalid','LaTeX differs from its registered worker') from error

    async def validate_publication(self,db,case,descriptor):
        if not descriptor['registered_postprocess']:
            from .postprocess_gate import authoritative_reason
            reason=await authoritative_reason(db,case)
            if reason:
                raise ValueError(reason)

    def source(self,prefix,descriptor):
        from .case_read_view import inspect
        return inspect(prefix,artifacts.source,descriptor['main'],descriptor['resources'],
                       input_aliases=descriptor.get('input_aliases'),gate_prefix=prefix)

    def artifacts(self,run,descriptor,output,frozen_files):
        from .latex_publication import artifacts_for
        records=artifacts_for(run,descriptor,output,frozen_files)
        descriptor['retired_aliases']={path:value for path,value in descriptor['retired_aliases'].items() if path not in records}
        return records

    def write_aliases(self,prefix,row,manifest):
        from .latex_publication import write_aliases
        return write_aliases(prefix,row,manifest)
