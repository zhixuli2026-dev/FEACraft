"""Stage a new OpenFOAM attempt from its authenticated original and checkpoint."""
import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from dataclasses import dataclass
from functools import partial
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
from .. import storage
from . import attempt_objects as objects, attempt_seal_journal as seals, attempt_workspace as workspace
from . import case_copy_io, command_input_seal, local_retry_seal, step_handlers
from .case_provision import io
from .case_write import CaseWriteConflict
from .file_stage_locations import MOUNTED_SCHEMA, locate
from .local_file_edits import _version
from .owned_staging import batch
from .publication_inputs import PublicationSource
from .request_deadline import current
from .seal_storage_budget import estimate
from .solve_resume_artifacts import NAME, SCHEMA
from .workflow_state import ExecutionConflict


def stale():
    return CaseWriteConflict('input_stale','续算的原封存输入或检查点在准备期间发生变化')


def require_parent(case_id,previous,checkpoint):
    if (checkpoint.get('schema')!=SCHEMA or checkpoint['source']!={
            'job_id':previous.job_id,'attempt_id':previous.id,'case_id':case_id,'version':previous.version,
            'input_sha256':hashlib.sha256(previous.input_manifest_json.encode()).hexdigest(),
            'output_sha256':hashlib.sha256(previous.output_manifest_json.encode()).hexdigest()}):
        raise ExecutionConflict('state_conflict','续算来源与原封存尝试不一致')


@dataclass(frozen=True)
class CheckpointSource:
    prefix: str
    manifest: dict
    output: dict

    def versions(self):
        try:
            if storage._use_s3():
                inputs=objects.sealed_input_versions(self.prefix,self.manifest)
                objects.output_inventory(self.prefix,self.manifest,self.output)
                outputs={row['path']:(row['size'],row['etag'])
                         for row in storage._s3_list(self.prefix,self.manifest['run_path'])}
                if outputs.keys()!=self.output['files'].keys() or any(
                    not etag or size!=self.output['files'][path]['size'] for path,(size,etag) in outputs.items()):
                    raise stale()
                return inputs,outputs
            inputs,metadata=local_retry_seal._archive_paths(self.prefix,self.manifest)
            runtime=workspace.checked_path(storage._fs_path(self.prefix,'.'),self.manifest['run_path'])
            return (_version(metadata),local_retry_seal._tree_versions(inputs,self.manifest),
                    local_retry_seal._tree_versions(runtime,self.output))
        except (OSError,ValueError,KeyError) as error:
            raise stale() from error


def snapshot(case,previous,source,prepared,checkpoint):
    view=prepared.source_view
    if (not view.matches(case.s3_prefix,source.manifest,source.output)
            or not view.input_view.matches(case.s3_prefix,source.manifest)):
        raise stale()
    run=checkpoint['run_dir']
    controls={run+'/system/controlDict',run+'/'+NAME}
    if set(prepared.generated_inputs)!=controls:
        raise ValueError('Continuation must generate only its checkpoint record and control dictionary')
    original=source.manifest
    excluded=controls|checkpoint['files'].keys()
    paths=sorted(path for path in original['files'] if path not in excluded)
    paths+=sorted(name for name in original['directories']
                  if not any(path.startswith(name+'/') for path in original['files']))
    return workspace.Snapshot(case.s3_prefix,str(uuid.uuid4()),paths,source=view.input_view.root,
        publications=[PublicationSource(view.root,checkpoint['files'],{})],
        references=original.get('publications',[]),selection=original['selection'],allow_empty=True,
        generated_inputs=prepared.generated_inputs,
        provenance={'case_id':case.id,'job_id':None,'dict_revision':original['source']['dict_revision'],
                    'retry_of_attempt_id':previous.id,'runtime':prepared.spec['runtime'],
                    'selection_mode':original['source'].get('selection_mode','explicit')})


@dataclass(frozen=True)
class PreparedLocalCheckpoint:
    id: str
    draft: dict
    directory: Path
    copy_files: tuple
    user_peak: int
    already_counted: int
    source_guard: Callable

    def install(self,case,attempt,manifest):
        self.source_guard(case,attempt)
        return local_retry_seal.install_files(case,attempt,manifest,self.directory,self.copy_files)

    async def bind_retry(self,db,case,attempt):
        return await local_retry_seal.bind_workspace(db,case,attempt,self)


@dataclass(frozen=True)
class StagedCheckpoint:
    retry_source: dict
    checkpoint_sha256: str
    workspace: object

    @property
    def id(self):
        return self.workspace.id

    async def bind_retry(self,db,case,attempt):
        return await self.workspace.bind_retry(db,case,attempt)


def prepare_files(item,directory,manifest,budget,payload_counted,source_guard):
    item.write_files(directory,deadline=current())
    return PreparedLocalCheckpoint(manifest['attempt_id'],manifest,directory,local_retry_seal.copy_versions(directory,manifest),
                                   budget.user_bytes,payload_counted,source_guard)


@asynccontextmanager
async def prepare(case,user,previous,reads,time,*,lease=None,owner_id=None):
    document=json.loads(previous.input_manifest_json)
    source=CheckpointSource(case.s3_prefix,document['workspace'],json.loads(previous.output_manifest_json))
    capacity=nullcontext() if lease is not None else case_copy_io.admit(code='checkpoint_seal_busy',label='封存检查点')
    with capacity:
        try:
            versions=await io(source.versions)
        except CaseWriteConflict as error:
            raise ExecutionConflict('checkpoint_invalid','原执行结果不能作为所选续算检查点：'+str(error)) from error
        spec=document['execution_spec']
        prepared=await step_handlers.get_handler('solve.openfoam.resume').prepare(reads,case,user,
            parameters={'source_attempt_id':previous.id,'time':time},
            budget={name:spec[name] for name in ('timeout_seconds','max_log_bytes')})
        try:
            name=prepared.descriptor['run_dir']+'/'+NAME
            checkpoint=json.loads(prepared.generated_inputs[name])
            require_parent(case.id,previous,checkpoint)
            item=snapshot(case,previous,source,prepared,checkpoint)
            await io(partial(item.inspect,deadline=current()))
            manifest=await io(item.describe)
            expected={**checkpoint['frozen_files'],**checkpoint['files']}
            expected[name]={**objects.digest(prepared.generated_inputs[name]),'executable':False}
            if manifest['files']!=expected:
                raise stale()

            def source_guard(bound_case,attempt):
                if (bound_case.s3_prefix!=source.prefix or attempt.id!=manifest['attempt_id']
                        or attempt.retry_of_attempt_id!=previous.id or source.versions()!=versions):
                    raise stale()

            if lease is not None:
                if lease.case.s3_prefix!=case.s3_prefix:
                    raise stale()
                staged=await command_input_seal.upload_checkpoint(lease,owner_id,item,source_guard)
                yield prepared,checkpoint,StagedCheckpoint(source.manifest,prepared.descriptor['checkpoint_sha256'],staged)
            else:
                upper=SimpleNamespace(id=manifest['attempt_id'],job_id=2**63-1)
                admitted={**manifest,'source':{**manifest['source'],'job_id':upper.job_id}}
                await io(seals.validate,case,upper.id,seals.describe_plan(case,upper,admitted))
                budget=await io(partial(estimate,case,upper,None,manifest=admitted))
                default=storage._fs_path('.','.local-checkpoint-staging')
                location=await io(locate,item.root,item.relative,default)
                schema=MOUNTED_SCHEMA if location.local else 'vibeflow.local-checkpoint-staging.v1'
                async with batch(location.scratch,budget.user_bytes,schema,
                                 target_root=location.scratch if location.local else None) as slot:
                    staged=await io(prepare_files,item,slot/'attempt',manifest,budget,
                                    budget.payload_bytes if location.local else 0,source_guard)
                    yield prepared,checkpoint,StagedCheckpoint(source.manifest,prepared.descriptor['checkpoint_sha256'],staged)
        finally:
            await io(prepared.source_view.cleanup)


async def start_job(db,identifier,case_id,user_id,body,key,digest):
    from . import document_admission, job_delivery, job_execution as execution, job_repeat
    from .async_ops import finish_before_cancel
    from .case_read_capacity import file_read_deadline
    from .case_write_capacity import committing
    from .distributed_queue import distributed_enabled
    from .local_source_reads import LocalSourceReads
    from .publication_read_set import PublicationReadSet
    from .request_deadline import STAGING_CLEANUP_SECONDS

    code,label='checkpoint_seal','封存检查点'
    async with file_read_deadline(code,label,seconds=STAGING_CLEANUP_SECONDS), AsyncExitStack() as scopes:
        case,user=await document_admission.authorize(db,case_id,user_id)
        replay=await execution.lookup_submission(db,case_id,user_id,key,digest)
        if replay is not None:return replay,False
        case,job,previous,submission=await job_repeat.source(db,identifier,user)
        job_repeat.require_source_attempt(previous,body)
        _,handler=await job_repeat.trusted(db,job,previous,submission)
        job_repeat.drained(case,job,previous)
        if handler is None or handler.key not in {'solve.openfoam','solve.openfoam.resume'}:
            raise ExecutionConflict('checkpoint_required','此求解器尚无已注册的续算检查点协议')
        if (not isinstance(body.get('parameters'),dict) or set(body['parameters'])!={'time'}
                or body.get('expected_revision') is not None or body.get('expected_event_sequence') is not None):
            raise ExecutionConflict('invalid_request','续算仅接受明确的原尝试检查点时刻，不能混入当前输入')
        record=job_repeat.operation_record('resume',job,previous)
        db.expunge_all()
        await finish_before_cancel(db.rollback())
        lease=owner_id=None
        if storage._use_s3():
            while True:
                replay=await execution.lookup_submission(db,case_id,user_id,key,digest)
                if replay is not None:return replay,False
                await finish_before_cancel(db.rollback())
                try:
                    lease,owner_id=await scopes.enter_async_context(command_input_seal.stage_case(
                        case_id,user_id,code=code,label=label))
                    break
                except CaseWriteConflict as error:
                    if error.code!='file_sync_busy':raise
                except HTTPException as error:
                    if (error.status_code!=409 or not isinstance(error.detail,dict)
                            or error.detail.get('code')!='storage_quota_busy'):
                        raise
                await asyncio.sleep(.2)
            replay=await execution.lookup_submission(db,case_id,user_id,key,digest)
            if replay is not None:return replay,False
            await finish_before_cancel(db.rollback())

        reads=PublicationReadSet()
        files=LocalSourceReads()
        scopes.enter_context(files.collect())
        async with prepare(case,user,previous,reads,body['parameters']['time'],lease=lease,owner_id=owner_id) as state:
            prepared,checkpoint,staged=state
            reads.seal()
            files.seal()
            case,user=await document_admission.authorize(db,case_id,user_id)
            async with committing(db,case,content_write=False):
                replay=await execution.lookup_submission(db,case_id,user_id,key,digest)
                if replay is not None:return replay,False
                case,job,previous,submission=await job_repeat.source(db,identifier,user)
                job_repeat.require_source_attempt(previous,body)
                _,handler=await job_repeat.trusted(db,job,previous,submission)
                job_repeat.drained(case,job,previous)
                if job_repeat.operation_record('resume',job,previous)!=record:
                    raise ExecutionConflict('state_conflict','原尝试在准备期间发生变化')
                await reads.require_current(db)
                if lease is None:
                    await files.require_current()
                    if files.uncovered:
                        from .publication_inputs import validate
                        await validate(db,case,staged.retry_source)
                new,new_attempt=await execution.allocate_retry(db,previous.id,registered=True,user_id=user_id,
                    key=key,digest=digest,continuation=(prepared,checkpoint),staged_workspace=staged)
                record['checkpoint_time']=checkpoint['label']
                record['checkpoint_sha256']=prepared.descriptor['checkpoint_sha256']
                await job_repeat.attach(db,new,new_attempt,record)
                await job_delivery.request(db,new,user_id=user_id,mode='redis' if distributed_enabled() else 'local')
                return new,True
