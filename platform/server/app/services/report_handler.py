"""Registered HTML/DOCX production from confirmed immutable engineering evidence."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path,PureWindowsPath,PurePosixPath
import shutil
import sys
import uuid

from .. import storage
from . import report_artifacts as artifacts, report_builder, report_publication
from .attempt_workspace import digest_file, inventory
from .async_ops import finish_before_cancel
from .case_evidence import safe_run_dir
from .case_write import CaseWriteConflict
from .casedict import CASE_DICT_PATH
from .geometry_handler import GeometryHandler
from .runtime_config import get_runtime_setting
from .step_handlers import PreparedStep
from . import document_admission as admission
from .workflow_state import ExecutionConflict

WORKER=Path(__file__).with_name('registered_report_worker.py')
# Keep the repository fallback without indexing ancestors that do not exist in
# shallow deployments such as /app/app/services. Runtime config/PATH wins below.
DEFAULT_PANDOC=str((Path(__file__).resolve().parent/'../../../../extracted/share/vibeflow/tools/pandoc/pandoc.exe').resolve())


def runtime_paths():
    return get_runtime_setting('python',sys.executable),str(Path(get_runtime_setting('pandoc',DEFAULT_PANDOC)).resolve())


def toolchain_files(pandoc):
    return {'pandoc_sha256':Path(pandoc),'worker_sha256':WORKER,
            'compiler_sha256':Path(artifacts.__file__),'builder_sha256':Path(report_builder.__file__)}


def toolchain_versions(pandoc):
    stamps={}
    for name,path in toolchain_files(pandoc).items():
        info=path.stat()
        stamps[name]=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    return stamps


def hash_toolchain(pandoc):
    from .request_deadline import current
    stamps=toolchain_versions(pandoc)
    deadline=current()
    hashes={name:digest_file(path,deadline=deadline)['sha256'] for name,path in toolchain_files(pandoc).items()}
    if toolchain_versions(pandoc)!=stamps:
        raise ExecutionConflict('input_stale','Report tools changed while preparing the build')
    return hashes,stamps


def require_toolchain(prepared):
    pandoc=prepared.descriptor['pandoc']
    try:
        current=runtime_paths(),toolchain_versions(pandoc)
    except (OSError,ValueError) as error:
        raise CaseWriteConflict('input_stale','Report tools are no longer available') from error
    expected=(prepared.spec['argv'][0],pandoc),prepared.admission['toolchain_versions']
    if current!=expected:
        raise CaseWriteConflict('input_stale','Report tools changed during preparation')


def live_source_paths(prepared):
    source=prepared.descriptor['source']
    paths={CASE_DICT_PATH,'D-post/data/latest-postprocess.json',*source['files'],*source['postprocess_guard']['evidence']}
    if source['mode']=='compose':
        paths.add('B-mesh/quality.json')
    for resolution in source.get('resource_resolution',()):
        paths.update(resolution['candidates'])
    return tuple(sorted(paths))


def report_seed(prefix,mode,pandoc,standalone):
    with artifacts.source_view(prefix) as (root,load,versions):
        source=artifacts.source_snapshot(root,mode,pandoc,standalone=standalone,
            known_files={'E-report/report.md':{}} if mode=='markdown' else {},gate_prefix=prefix,load=load)
        markdown=(root/'E-report/report.md').read_bytes() if mode=='markdown' else None
        dictionary=inventory(root,[CASE_DICT_PATH])
    return source,markdown,dictionary,versions


async def report_inputs(db,case,run_dir,pinned=None):
    from .artifact_publication import current,read_manifest
    from .publication_inputs import resolve
    from .postprocess_publication import _gate_files
    _,post=await current(db,case.id,'postprocess')
    if post is None or (post.id if pinned is None else pinned)!=post.id:
        raise ExecutionConflict('input_stale','Confirmed workflow postprocessing is no longer current')
    if storage._use_s3():
        from .publication_objects import bound_metadata
        manifest=await finish_before_cancel(asyncio.to_thread(bound_metadata,case.s3_prefix,post))
    else:
        _,manifest=await finish_before_cancel(asyncio.to_thread(read_manifest,case.s3_prefix,post))
    if manifest['descriptor'].get('handler')!='postprocess.v1' or manifest['descriptor']['run_dir']!=run_dir:
        raise ExecutionConflict('input_stale','Postprocessing belongs to another run or handler')
    _,mesh=await current(db,case.id,'mesh')
    if mesh is None:
        raise ExecutionConflict('input_stale','Report has no current mesh quality evidence')
    references=[{'publication_id':post.id,'paths':None,'require_current':True},
        {'publication_id':mesh.id,'paths':['B-mesh/quality.json'],'require_current':True}]
    resolved=await resolve(db,case,references)
    # resolve already checked the current postprocess and its complete upstream
    # graph. Run the same file/readiness gate against that verified selection,
    # rather than traversing the graph a second time during one preparation.
    selected=next(item for item in resolved if item.reference['publication_id']==post.id)
    try:
        reason=await finish_before_cancel(asyncio.to_thread(_gate_files,case.s3_prefix,post,selected.files))
    except (ValueError,OSError,KeyError,TypeError):
        reason='后处理发布或上游输入已失效，请重新提取'
    if reason:
        raise ExecutionConflict('input_stale',reason)
    return references,resolved


async def document_inputs(db,case,run_dir,pinned=None):
    """Keep engineering lineage strict; ordinary documents still use the gate."""
    from ..models import ArtifactPublicationHead
    from .postprocess_gate import authoritative_reason
    registered=await db.get(ArtifactPublicationHead,(case.id,'postprocess')) is not None
    if registered or pinned is not None:
        from .artifact_publication import current,read_manifest
        from .publication_inputs import resolve
        _,post=await current(db,case.id,'postprocess')
        if post is not None:
            _,manifest=read_manifest(case.s3_prefix,post)
            descriptor=manifest['descriptor']
            if descriptor.get('handler')=='postprocess-case.v1':
                if descriptor['run_dir']!=run_dir or pinned not in (None,post.id):
                    raise ExecutionConflict('input_stale','Case postprocessing belongs to another run/version')
                reason=await authoritative_reason(db,case)
                if reason:raise ExecutionConflict('input_stale',reason)
                references=[{'publication_id':post.id,'paths':None,'require_current':True}]
                return references,await resolve(db,case,references),False
        references,resolved=await report_inputs(db,case,run_dir,pinned)
        return references,resolved,True
    reason=await authoritative_reason(db,case)
    if reason:
        raise ExecutionConflict('input_stale',reason)
    return [],[],False


async def validate_document_publication(db,case,descriptor):
    if not descriptor.get('registered_postprocess',True):
        from ..models import ArtifactPublicationHead
        from .postprocess_gate import authoritative_reason
        head=await db.get(ArtifactPublicationHead,(case.id,'postprocess'))
        if head is not None:
            from .artifact_publication import current,read_manifest
            _,post=await current(db,case.id,'postprocess')
            pinned={item['publication_id'] for item in descriptor.get('input_publications',[])}
            if post is None or post.id not in pinned:
                raise ValueError('Postprocessing registration changed; submit a fresh document build')
            from .case_provision import io

            _,manifest=await io(read_manifest,case.s3_prefix,post)
            if manifest['descriptor'].get('handler')!='postprocess-case.v1':
                raise ValueError('Document no longer has its original case-file extraction lineage')
        reason=await authoritative_reason(db,case)
        if reason:
            raise ValueError(reason)


class ReportHandler:
    key='report.build'
    version='1'
    job_type='convert'
    publication_kind='report'
    cancellation=dict(GeometryHandler.cancellation)
    recovery=dict(GeometryHandler.recovery)

    async def prepare(self,db,case,user,*,parameters,budget,observations=None,resolved_inputs=None):
        if ('run_dir' not in parameters or set(parameters)-{'run_dir','mode','postprocess_publication_id','allow_standalone'}
                or type(parameters.get('allow_standalone',False)) is not bool):
            raise ExecutionConflict('invalid_request','Report requires its registered source mode and run directory')
        run_dir=safe_run_dir(parameters['run_dir'])
        mode=parameters.get('mode','compose')
        if not isinstance(mode,str) or mode not in {'compose','markdown'}:
            raise ExecutionConflict('invalid_request','Unsupported report source mode')
        reader=db if observations is None else observations
        if parameters.get('allow_standalone',False):
            references,resolved,registered=await document_inputs(reader,case,run_dir,parameters.get('postprocess_publication_id'))
        else:
            # Workflow dispatch must retain its registered engineering contract.
            # Only the standalone document endpoint opts into legacy evidence.
            references,resolved=await report_inputs(reader,case,run_dir,parameters.get('postprocess_publication_id'))
            registered=True
        published={path for item in resolved for path in item.files}
        python,pandoc=runtime_paths()
        if not shutil.which(python) or not Path(pandoc).is_file():
            raise ExecutionConflict('runtime_unavailable','Configured report Python or Pandoc is unavailable')
        # Pandoc receives the selected bytes in an independently owned request.
        from .case_write import bounded_case_read
        source,markdown,dictionary,versions=await bounded_case_read(report_seed,case.s3_prefix,mode,pandoc,
            not registered,code='report_preparation',label='报告输入准备')
        if markdown is not None and hashlib.sha256(markdown).hexdigest()!=source['files']['E-report/report.md']['sha256']:
            raise ExecutionConflict('input_stale','Report Markdown changed while selecting inputs')
        if mode=='markdown':
            from .compose_publication import build_reference
            record=source['files']['E-report/report.md']
            source_reference=await build_reference(reader,case,{key:record[key] for key in ('size','sha256')})
            references+=source_reference
            if resolved_inputs is not None and source_reference:
                from .publication_inputs import resolve
                resolved+=await resolve(reader,case,source_reference)
        if resolved_inputs is not None:
            # The draft reference was verified separately from the engineering
            # publications. Seal the combined inputs in request-normalized order.
            resolved_inputs.extend(sorted(resolved,key=lambda item:item.reference['publication_id']))
        paths=['0-caseDict/caseDict',*[path for path in source['files'] if path not in published]]
        hashes,stamps=await bounded_case_read(hash_toolchain,pandoc,code='report_preparation',label='报告工具核验')
        from .quota import check_job_quota
        await check_job_quota(db,user, case_id=case.id)
        execution_id=str(uuid.uuid4())
        generated_at=datetime.now(timezone.utc).isoformat()
        spec={'command':'Pandoc report','case_prefix':case.s3_prefix,'runtime':'windows',
            'argv':[python,str(WORKER),'--mode',mode,'--pandoc',pandoc,'--execution-id',execution_id,
                '--generated-at='+generated_at],
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        self.validate_spec(spec)
        prepared=PreparedStep(self.key,self.version,self.job_type,spec,paths,references or None,self.publication_kind,
            {'handler':'report.v1','handler_version':self.version,'case_id':case.id,'run_dir':run_dir,'mode':mode,
             'execution_id':execution_id,'source':source,'input_publications':references,'pandoc':pandoc,
             'registered_postprocess':registered,'generated_at':generated_at,**hashes})
        return admission.pending(prepared,case,parameters,budget,dictionary=dictionary,
            markdown=markdown,published=sorted(published),case_versions=versions,toolchain_versions=stamps)

    async def complete_preparation(self,prepared,case_id,user_id):
        descriptor=prepared.descriptor
        from .native_request_execution import scope
        from .case_read_view import async_read_view
        async with async_read_view(artifacts.source_view(prepared.spec['case_prefix'])) as (root,load,versions):
            async with scope(case_id,user_id,root,'report.prepare',{'pandoc':descriptor['pandoc']}) as executor:
                source=await executor.run_sync(artifacts.source_snapshot,root,descriptor['mode'],descriptor['pandoc'],
                    standalone=not descriptor['registered_postprocess'],markdown=prepared.admission['markdown'],track_resolution=True,
                    gate_prefix=prepared.spec['case_prefix'],load=load)
        seed=descriptor['source']
        if (any(source[key]!=value for key,value in seed.items() if key!='files')
                or any(source['files'].get(path)!=record for path,record in seed['files'].items())):
            raise ExecutionConflict('input_stale','Report source changed during native preparation')
        paths=['0-caseDict/caseDict',*[path for path in source['files'] if path not in prepared.admission['published']]]
        initial=prepared.admission['case_versions']
        if initial is not None and any(versions.get(path)!=version for path,version in initial.items()):
            raise CaseWriteConflict('input_stale','Report source version changed during native preparation')
        ready=admission.ready(prepared,input_paths=paths,descriptor={**descriptor,'source':source})
        return replace(ready,admission={**ready.admission,'case_versions':versions,'markdown':None})

    def validate_spec(self,spec):
        try:
            argv=spec['argv']
            if (spec.get('command')!='Pandoc report' or spec.get('runtime')!='windows' or
                    spec.get('evidence_run_dir') is not None or not isinstance(argv,list) or len(argv) not in {8,9} or
                    not isinstance(argv[0],str) or not argv[0] or argv[1:3]!=[str(WORKER),'--mode'] or
                    argv[3] not in {'compose','markdown'} or argv[4]!='--pandoc' or
                    not isinstance(argv[5],str) or not (PureWindowsPath(argv[5]).is_absolute() or PurePosixPath(argv[5]).is_absolute()) or
                    argv[6]!='--execution-id' or str(uuid.UUID(argv[7]))!=argv[7]):
                raise ValueError('Unexpected report worker')
            if len(argv)==9:
                artifacts.generation_time(argv[8])
        except (KeyError,TypeError,ValueError,AttributeError,IndexError) as error:
            raise ExecutionConflict('evidence_invalid','Report differs from its registered worker') from error

    def artifacts(self,run,descriptor,output,frozen_files):
        return report_publication.artifacts_for(run,descriptor,output,frozen_files)

    def source(self,prefix,descriptor):
        return artifacts.inspect_case_source(prefix,descriptor['mode'],descriptor['pandoc'],
            standalone=not descriptor.get('registered_postprocess',True),known_files=descriptor['source']['files'],
            known_resolution=descriptor['source'].get('resource_resolution'))

    async def validate_publication(self,db,case,descriptor):
        await validate_document_publication(db,case,descriptor)
