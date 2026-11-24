"""Explicit repeat operations bound to authentic attempts and immutable inputs."""
import asyncio,json
from contextlib import AsyncExitStack
from copy import deepcopy
from sqlalchemy import select
from fastapi import HTTPException
from .. import storage
from ..models import (ArtifactPublication,CaseEventCounter,DurableEvent,Job,JobAttempt,JobExecutionHead,
    JobSubmissionRequest,WorkflowRun,WorkflowStep)
from . import job_execution as execution,job_delivery,step_handlers,attempt_workspace
from .workflow_state import ExecutionConflict,TERMINAL
from .case_write_capacity import committing
from .case_write import CaseWriteConflict
from .async_ops import finish_before_cancel
from .durable_events import json_text

SCHEMA='vibeflow.job-operation.v1'

async def source(db,identifier,user):
    from ..routers.workspaces import get_owned_case
    job=await db.get(Job,identifier,populate_existing=True)
    if job is None:raise HTTPException(404,'作业不存在')
    case=await get_owned_case(job.case_id,user,db,write=True)
    attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1)
        .execution_options(populate_existing=True))
    submission=await db.scalar(select(JobSubmissionRequest).where(JobSubmissionRequest.job_id==job.id))
    return case,job,attempt,submission

async def trusted(db,job,attempt,submission,*,allow_reprepare=False):
    try:return await _trusted(db,job,attempt,submission,allow_reprepare=allow_reprepare)
    except ExecutionConflict:raise
    except (ValueError,TypeError,KeyError,AttributeError) as error:
        raise ExecutionConflict('evidence_invalid','原尝试的请求、处理器或封存记录损坏，不能重复执行') from error


async def _trusted(db,job,attempt,submission,*,allow_reprepare=False):
    if attempt is None or submission is None:
        raise ExecutionConflict('recovery_required','历史请求缺少可验证的封存输入，请检查后重新提交')
    attempt_workspace.require_sealed(job,attempt)
    document=json.loads(attempt.input_manifest_json);spec=document['execution_spec']
    if (attempt.job_id!=job.id or submission.job_id!=job.id or submission.case_id!=job.case_id
            or json.loads(submission.execution_spec_json)!=spec or job.command!=spec.get('command')
            or document.get('submission_request_hash',submission.request_hash)!=submission.request_hash):
        raise ExecutionConflict('evidence_invalid','原作业请求与封存执行配置不一致')
    handler=step_handlers.validate_binding(job,attempt)
    reservation=json.loads(attempt.publication_json)
    if handler is None and reservation.get('schema')=='vibeflow.artifact-publication.v1':
        raise ExecutionConflict('evidence_invalid','正式成果预留缺少原处理器记录，不能当作旧命令重新准备')
    if handler is not None:
        publication=await db.get(ArtifactPublication,reservation.get('publication_id'),populate_existing=True)
        if (publication is None or publication.attempt_id!=attempt.id or publication.case_id!=job.case_id
                or publication.kind!=document['handler']['publication_kind']):
            raise ExecutionConflict('evidence_invalid','原处理器的成果预留与尝试归属不一致')
    if attempt.step_id:
        step=await db.get(WorkflowStep,attempt.step_id,populate_existing=True)
        run=await db.get(WorkflowRun,step.run_id,populate_existing=True) if step else None
        if step is None or run is None or run.case_id!=job.case_id or step.active_attempt_id!=attempt.id:
            raise ExecutionConflict('evidence_invalid','原尝试的工作流归属不一致')
        if run.status not in TERMINAL or step.status not in TERMINAL:
            raise ExecutionConflict('state_conflict','请等待原工作流完成收尾后再明确创建新执行')
    legacy=handler is None and legacy_professional(job,attempt,spec)
    if legacy and not allow_reprepare:
        raise ExecutionConflict('recovery_required','旧专业命令尚未绑定注册处理器，请明确选择正式处理器按当前输入重新准备')
    if handler is None and not legacy:
        from .command_safety import command_block_reason
        if command_block_reason(spec['command']):
            raise ExecutionConflict('invalid_request','原命令不符合当前执行策略，请检查后新提交')
    return document,handler


def legacy_professional(job,attempt,spec):
    return job.type!='shell' or spec.get('argv') is not None or spec.get('evidence_run_dir') or attempt.step_id


def reprepare_candidates(job):
    return [{'key':handler.key,'version':handler.version} for _,handler in sorted(step_handlers.registered().items())
        if handler.job_type==job.type and not handler.key.startswith('command.') and handler.key!='solve.openfoam.resume']


def reprepare_target(job,attempt,document,handler,record):
    if handler is not None or not legacy_professional(job,attempt,document['execution_spec']):
        raise ExecutionConflict('invalid_request','只有未绑定正式处理器的旧专业请求可选择重新准备处理器')
    if (not isinstance(record,dict) or set(record)!={'key','version'}
            or not isinstance(record['key'],str) or not isinstance(record['version'],str)):
        raise ExecutionConflict('invalid_request','重新准备必须明确注册处理器名称和版本')
    if record not in reprepare_candidates(job):
        raise ExecutionConflict('invalid_request','重新准备处理器必须属于原作业类型，不能沿用任意命令或旧检查点')
    return step_handlers.get_handler(record['key'],record['version'])

def drained(case,job,attempt):
    if job.status not in TERMINAL or attempt.phase!=job.status:
        raise ExecutionConflict('state_conflict','请等待原作业完成取消和收尾')
    if job_delivery.occupying(case,job,attempt):
        raise ExecutionConflict('recovery_required','原作业的进程、内容写入或成果发布尚未核验结束')

async def options(db,identifier,user):
    case,job,attempt,submission=await source(db,identifier,user)
    head=await db.get(JobExecutionHead,attempt.root_job_id) if attempt else None
    counter=await db.get(CaseEventCounter,case.id)
    reason=None;handler=None
    try:
        _,handler=await trusted(db,job,attempt,submission);drained(case,job,attempt)
    except ExecutionConflict as error:reason={'code':error.code,'message':error.detail}
    retry_reason=reason
    if retry_reason is None and (job.status not in {'failed','cancelled','interrupted'} or head is None or head.active_attempt_id!=attempt.id):
        retry_reason={'code':'state_conflict','message':'只能重试当前失败、取消或中断的末次尝试'}
    reprepare={'available':False,'inputs':'current_inputs','parameters_required':True,
        'action':'rerun','handler_choices':[],'reason':{'code':'invalid_request','message':'此请求无需旧专业处理器重新准备'}}
    if handler is None:
        try:
            document,old_handler=await trusted(db,job,attempt,submission,allow_reprepare=True)
            drained(case,job,attempt)
            if old_handler is None and legacy_professional(job,attempt,document['execution_spec']):
                choices=reprepare_candidates(job)
                reprepare.update(available=bool(choices),handler_choices=choices,
                    reason=None if choices else {'code':'recovery_required','message':'此旧作业类型没有对应正式处理器，请检查后新提交'})
        except ExecutionConflict as error:reprepare['reason']={'code':error.code,'message':error.detail}
    resume_reason=retry_reason
    policy=None
    if resume_reason is None:
        if handler is None or handler.key not in {'solve.openfoam','solve.openfoam.resume'}:
            resume_reason={'code':'checkpoint_required','message':'此处理器没有注册可验证的续算检查点协议'}
        else:
            from .solve_restart_policy import preview
            try:
                policy=preview(attempt)
                if policy=='disabled':
                    resume_reason={'code':'checkpoint_disabled','message':'原求解尝试的算例策略禁用了检查点续算'}
            except ExecutionConflict as error:
                resume_reason={'code':error.code,'message':error.detail}
    return {'job_id':job.id,'source_attempt_id':attempt.id if attempt else None,'source_version':attempt.version if attempt else None,
        'expected_revision':case.dict_revision,'expected_event_sequence':counter.next_seq if counter else 1,
        'handler':{'key':handler.key,'version':handler.version} if handler else None,
        'retry':{'available':retry_reason is None,'inputs':'original_sealed_inputs','reason':retry_reason},
        'rerun':{'available':reason is None,'inputs':'current_inputs','parameters_required':handler is not None and not handler.key.startswith('command.'),'reason':reason,
            'handler':{'key':'solve.openfoam' if handler.key=='solve.openfoam.resume' else handler.key,'version':handler.version} if handler else None},
        'reprepare':reprepare,
        'resume':{'available':resume_reason is None,'requires_checkpoint_validation':True,'parameters':['time'],
            'checkpoint_policy':policy,'supports_latest':policy=='latestWritten','reason':resume_reason}}

def operation_record(action,job,attempt):
    return {'schema':SCHEMA,'action':action,'source_job_id':job.id,'source_attempt_id':attempt.id,'source_version':attempt.version,
        'source_input_sha256':execution.request_hash(json.loads(attempt.input_manifest_json)),
        'source_output_sha256':execution.request_hash(json.loads(attempt.output_manifest_json)),
        'source_step_id':attempt.step_id}

async def attach(db,job,attempt,record):
    document=json.loads(attempt.input_manifest_json);document['operation']=record;attempt.input_manifest_json=json_text(document)
    await execution.attempt_event(db,job,attempt,'attempt.operation_requested',record)

def require_source_attempt(attempt,body):
    if attempt is None or attempt.id!=body['source_attempt_id'] or attempt.version!=body['source_version']:
        raise ExecutionConflict('state_conflict','原尝试已变化，请刷新操作选项')


def require_retry_parameters(body):
    if any(body.get(key) is not None for key in ('parameters','expected_revision','expected_event_sequence')):
        raise ExecutionConflict('invalid_request','重试只使用原封存输入，不能带入当前输入或替换参数')


async def finish_retry(db,job,attempt,record,user_id):
    from .distributed_queue import distributed_enabled

    await attach(db,job,attempt,record)
    await job_delivery.request(db,job,user_id=user_id,mode='redis' if distributed_enabled() else 'local')
    return job,True


async def inspect_retry(db,identifier,user,body):
    case,job,attempt,submission=await source(db,identifier,user)
    require_source_attempt(attempt,body)
    document,handler=await trusted(db,job,attempt,submission)
    execution.require_retry_head(job,attempt,await db.get(JobExecutionHead,attempt.root_job_id,populate_existing=True))
    drained(case,job,attempt)
    record=operation_record('retry',job,attempt)
    descriptor=json.loads(attempt.publication_json)['descriptor'] if handler is not None else None
    return case,document['workspace'],record,handler,descriptor


async def staged_local_retry(db,identifier,case_id,user_id,body,key,digest,*,settle_fallback):
    from . import case_copy_io, document_admission, local_retry_seal, publication_inputs, workflow_fallback
    from .case_provision import io
    from .case_read_capacity import file_read_deadline
    from .local_source_reads import LocalSourceReads, read_source, supported
    from .publication_read_set import PublicationReadSet
    from .request_deadline import STAGING_CLEANUP_SECONDS

    code,label='retry_seal','封存重试输入'
    async with file_read_deadline(code,label,seconds=STAGING_CLEANUP_SECONDS):
        _,user=await document_admission.authorize(db,case_id,user_id)
        replay=await execution.lookup_submission(db,case_id,user_id,key,digest)
        if replay is not None:return replay,False
        if settle_fallback:
            case,job,attempt,_=await source(db,identifier,user)
            require_source_attempt(attempt,body)
            await workflow_fallback.settle_retry_source(db,case,job)
            _,user=await document_admission.authorize(db,case_id,user_id)
        case,original,record,handler,descriptor=await inspect_retry(db,identifier,user,body)
        db.expunge(case)
        await finish_before_cancel(db.rollback())

        with case_copy_io.admit(code=code+'_busy',label=label):
            async with local_retry_seal.prepare(case,original) as staged:
                files,observations=LocalSourceReads(),PublicationReadSet()
                source_observed=handler is not None and supported(handler)
                with files.collect():
                    if source_observed and await io(read_source,handler,case.s3_prefix,descriptor)!=descriptor['source']:
                        raise ExecutionConflict('input_stale','专业输入已变化，请选择按当前输入重跑')
                    await publication_inputs.validate(observations,case,original)
                files.seal()
                observations.seal()
                case,user=await document_admission.authorize(db,case_id,user_id)
                async with committing(db,case,content_write=False):
                    replay=await execution.lookup_submission(db,case_id,user_id,key,digest)
                    if replay is not None:return replay,False
                    case,original,observed,handler,descriptor=await inspect_retry(db,identifier,user,body)
                    if observed!=record:
                        raise ExecutionConflict('state_conflict','原尝试在准备期间发生变化')
                    if handler is not None and not source_observed and await io(handler.source,case.s3_prefix,descriptor)!=descriptor['source']:
                        raise ExecutionConflict('input_stale','专业输入已变化，请选择按当前输入重跑')
                    await observations.require_current(db)
                    await files.require_current()
                    if files.uncovered:
                        await publication_inputs.validate(db,case,original)
                    new,new_attempt=await execution.allocate_retry(db,record['source_attempt_id'],registered=True,
                        user_id=user_id,key=key,digest=digest,staged_workspace=staged)
                    return await finish_retry(db,new,new_attempt,record,user_id)


async def staged_retry(db,identifier,case_id,user_id,body,key,digest,*,settle_fallback):
    from . import command_input_seal, document_admission, publication_inputs, workflow_fallback
    from .case_provision import io
    from .case_read_capacity import file_read_deadline
    from .publication_read_set import PublicationReadSet
    from .request_deadline import STAGING_CLEANUP_SECONDS

    code,label='retry_seal','封存重试输入'
    async with file_read_deadline(code,label,seconds=STAGING_CLEANUP_SECONDS):
        async with AsyncExitStack() as scopes:
            while True:
                replay=await execution.lookup_submission(db,case_id,user_id,key,digest)
                if replay is not None:return replay,False
                try:
                    if settle_fallback:
                        _,user=await document_admission.authorize(db,case_id,user_id)
                        case,job,attempt,submission=await source(db,identifier,user)
                        require_source_attempt(attempt,body)
                        await workflow_fallback.settle_retry_source(db,case,job)
                    await finish_before_cancel(db.rollback())
                    lease,owner_id=await scopes.enter_async_context(
                        command_input_seal.stage_case(case_id,user_id,code=code,label=label))
                    break
                except CaseWriteConflict as error:
                    if error.code!='file_sync_busy':raise
                except HTTPException as error:
                    if (error.status_code!=409 or not isinstance(error.detail,dict)
                            or error.detail.get('code')!='storage_quota_busy'):
                        raise
                await finish_before_cancel(db.rollback())
                await asyncio.sleep(.2)
            case,user=await document_admission.authorize(db,case_id,user_id)
            replay=await execution.lookup_submission(db,case_id,user_id,key,digest)
            if replay is not None:return replay,False
            case,original,record,handler,descriptor=await inspect_retry(db,identifier,user,body)
            db.expunge(case)
            await finish_before_cancel(db.rollback())

            observations=PublicationReadSet()
            if handler is not None and await io(handler.source,case.s3_prefix,descriptor)!=descriptor['source']:
                raise ExecutionConflict('input_stale','专业输入已变化，请选择按当前输入重跑')
            await publication_inputs.validate(observations,case,original)
            observations.seal()
            staged=await command_input_seal.upload_retry(lease,owner_id,case.dict_revision,original)

            case,user=await document_admission.authorize(db,case_id,user_id)
            async with committing(db,case,content_write=False):
                case,job,attempt,submission=await source(db,identifier,user)
                require_source_attempt(attempt,body)
                if operation_record('retry',job,attempt)!=record:
                    raise ExecutionConflict('state_conflict','原尝试在准备期间发生变化')
                await trusted(db,job,attempt,submission)
                drained(case,job,attempt)
                await observations.require_current(db)
                new,new_attempt=await execution.allocate_retry(db,attempt.id,registered=True,user_id=user_id,
                    key=key,digest=digest,staged_workspace=staged)
                return await finish_retry(db,new,new_attempt,record,user_id)


async def require_rerun_confirmation(db,case,body,*,lease=None,allow_pending_replay=False):
    counter=await db.get(CaseEventCounter,case.id,populate_existing=True)
    expected=body.get('expected_event_sequence')
    current=counter.next_seq if counter else 1
    if lease is not None and lease.started_sequence==expected:
        # Exactly this request's lease-start event may advance the cursor.
        expected=lease.started_sequence+1
    elif lease is not None:
        expected=None
    if body.get('expected_revision')==case.dict_revision:
        if expected==current:
            return
        if allow_pending_replay and expected==current-1:
            from .storage_operations import active

            operation=await active(db,case.id)
            if operation is not None and operation.purpose=='staging':
                event=await db.scalar(select(DurableEvent).where(DurableEvent.case_id==case.id,
                    DurableEvent.seq==expected,DurableEvent.type=='storage.operation.started'))
                if event is not None and json.loads(event.payload_json).get('operation_id')==operation.id:
                    # A concurrent same-key submission may still commit. Acquiring our own lease
                    # can never turn this deferred confirmation into authorization for a new run.
                    return
    raise ExecutionConflict('input_stale','当前输入或事件已变化，请刷新后确认重跑')


async def rerun_preflight_replay(db,case,user_id,body,key,digest,*,allow_pending_replay=False):
    try:
        await require_rerun_confirmation(db,case,body,allow_pending_replay=allow_pending_replay)
    except ExecutionConflict:
        replay=await execution.lookup_submission(db,case.id,user_id,key,digest)
        if replay is not None:return replay
        raise


async def prepare_rerun_handler(db,case,user,document,handler,body):
    if handler is None:
        if body.get('parameters') not in (None,{}):
            raise ExecutionConflict('invalid_request','命令重跑不能隐式替换原命令，请通过新提交入口修改命令')
        return None
    spec=deepcopy(document['execution_spec'])
    command_handler=handler.key.startswith('command.')
    if command_handler and body.get('parameters') not in (None,{}):
        raise ExecutionConflict('invalid_request','类型化命令重跑保留原命令和预算，请通过新提交入口修改命令')
    if not command_handler and body.get('parameters') is None:
        raise ExecutionConflict('invalid_request','专业重跑需要明确的处理器参数')
    # Rerun a continuation against current inputs with the original solver.
    target=step_handlers.get_handler('solve.openfoam') if handler.key=='solve.openfoam.resume' else handler
    parameters={'spec':spec} if command_handler else body['parameters']
    if not command_handler:
        from .job_repeat_parameters import public_parameters
        parameters=public_parameters(target,parameters)
    prepare=getattr(target,'prepare_rerun',target.prepare)
    return await prepare(db,case,user,parameters=parameters,
        budget={'timeout_seconds':spec['timeout_seconds'],'max_log_bytes':spec['max_log_bytes']})


async def staged_local_rerun(db,identifier,case_id,user_id,body,key,digest):
    from . import case_copy_io, document_admission as admission, local_rerun_seal, publication_inputs
    from .case_read_capacity import file_read_deadline
    from .publication_read_set import PublicationReadSet
    from .request_deadline import STAGING_CLEANUP_SECONDS

    reprepare=body.get('reprepare_handler')
    code,label='rerun_seal','封存重跑输入'
    async with file_read_deadline(code,label,seconds=STAGING_CLEANUP_SECONDS):
        _,user=await admission.authorize(db,case_id,user_id)
        replay=await execution.lookup_submission(db,case_id,user_id,key,digest)
        if replay is not None:return replay,False
        case,job,attempt,submission=await source(db,identifier,user)
        require_source_attempt(attempt,body)
        document,handler=await trusted(db,job,attempt,submission,allow_reprepare=reprepare is not None)
        drained(case,job,attempt)
        replay=await rerun_preflight_replay(db,case,user_id,body,key,digest)
        if replay is not None:return replay,False
        expected=operation_record('rerun',job,attempt)
        record=dict(expected)
        if reprepare is not None:
            handler=reprepare_target(job,attempt,document,handler,reprepare)
            record['reprepared_handler']={'key':handler.key,'version':handler.version}
        prepared=await prepare_rerun_handler(db,case,user,document,handler,body)
        db.expunge(case)
        await finish_before_cancel(db.rollback())
        if prepared is not None and prepared.admission is not None:
            prepared=await admission.complete(prepared,case_id,user_id)
            checked,user=await admission.authorize(db,case_id,user_id)
            replay=await rerun_preflight_replay(db,checked,user_id,body,key,digest)
            if replay is not None:return replay,False
            await admission.validate(db,checked,user,prepared)
            await finish_before_cancel(db.rollback())
        observations=PublicationReadSet()
        spec,paths,publications=await rerun_inputs(observations,case,document,prepared)
        resolved=await publication_inputs.resolve(observations,case,publications) if publications else None
        observations.seal()
        generated=prepared.generated_inputs if prepared is not None else None
        all_live=prepared is None and paths is None and publications is None
        with case_copy_io.admit(code=code+'_busy',label=label):
            async with local_rerun_seal.prepare(case,spec,paths,resolved,generated,all_live=all_live) as staged:
                verify_rerun_workspace(prepared,staged.draft)
                case,user=await admission.authorize(db,case_id,user_id)
                async with committing(db,case,content_write=False):
                    replay=await execution.lookup_submission(db,case_id,user_id,key,digest)
                    if replay is not None:return replay,False
                    case,job,attempt,submission=await source(db,identifier,user)
                    require_source_attempt(attempt,body)
                    await require_rerun_confirmation(db,case,body)
                    if operation_record('rerun',job,attempt)!=expected:
                        raise ExecutionConflict('state_conflict','原尝试在准备期间发生变化')
                    await trusted(db,job,attempt,submission,allow_reprepare=reprepare is not None)
                    drained(case,job,attempt)
                    await observations.require_current(db)
                    rerun=await submit_rerun(db,case,user,job,prepared,record,key,digest,
                                             inputs=(spec,paths,publications),staged=staged)
                    return rerun,True


def verify_rerun_workspace(prepared,manifest):
    from .document_admission import validate_workspace

    if prepared is not None and prepared.admission is not None:
        validate_workspace(prepared,manifest)
    if (prepared is not None and prepared.key.startswith('command.')
            and manifest['files']!=prepared.descriptor['source']['files']):
        raise ExecutionConflict('input_stale','类型化命令的当前输入在暂存期间发生变化')


async def staged_remote_rerun(db,identifier,case_id,user_id,body,key,digest):
    from types import SimpleNamespace
    from . import command_input_seal, document_admission as admission, publication_inputs
    from .case_read_capacity import file_read_deadline
    from .publication_read_set import PublicationReadSet
    from .request_deadline import STAGING_CLEANUP_SECONDS

    reprepare=body.get('reprepare_handler')
    code,label='rerun_seal','封存重跑输入'
    async with file_read_deadline(code,label,seconds=STAGING_CLEANUP_SECONDS):
        _,user=await admission.authorize(db,case_id,user_id)
        case,job,attempt,submission=await source(db,identifier,user)
        require_source_attempt(attempt,body)
        document,handler=await trusted(db,job,attempt,submission,allow_reprepare=reprepare is not None)
        drained(case,job,attempt)
        replay=await rerun_preflight_replay(db,case,user_id,body,key,digest,allow_pending_replay=True)
        if replay is not None:return replay,False
        expected=operation_record('rerun',job,attempt)
        record=dict(expected)
        if reprepare is not None:
            handler=reprepare_target(job,attempt,document,handler,reprepare)
            record['reprepared_handler']={'key':handler.key,'version':handler.version}
        prepared=await prepare_rerun_handler(db,case,user,document,handler,body)
        await finish_before_cancel(db.rollback())
        if prepared is not None and prepared.admission is not None:
            prepared=await admission.complete(prepared,case_id,user_id)

        async with AsyncExitStack() as scopes:
            while True:
                replay=await execution.lookup_submission(db,case_id,user_id,key,digest)
                if replay is not None:return replay,False
                await finish_before_cancel(db.rollback())
                try:
                    lease,owner_id=await scopes.enter_async_context(
                        command_input_seal.stage_case(case_id,user_id,code=code,label=label))
                    break
                except CaseWriteConflict as error:
                    if error.code!='file_sync_busy':raise
                except HTTPException as error:
                    if (error.status_code!=409 or not isinstance(error.detail,dict)
                            or error.detail.get('code')!='storage_quota_busy'):
                        raise
                await asyncio.sleep(.2)
            case,user=await admission.authorize(db,case_id,user_id)
            replay=await execution.lookup_submission(db,case_id,user_id,key,digest)
            if replay is not None:return replay,False
            await require_rerun_confirmation(db,case,body,lease=lease)
            if prepared is not None and prepared.admission is not None:
                await admission.validate(db,case,user,prepared)
            db.expunge(case)
            await finish_before_cancel(db.rollback())
            observations=PublicationReadSet()
            spec,paths,publications=await rerun_inputs(observations,case,document,prepared)
            resolved=await publication_inputs.resolve(observations,case,publications) if publications else None
            observations.seal()
            generated=prepared.generated_inputs if prepared is not None else None
            all_live=prepared is None and paths is None and publications is None

            async def verify(manifest):
                verify_rerun_workspace(prepared,manifest)

            staged=await command_input_seal.upload(lease,owner_id,SimpleNamespace(input_paths=paths or [],spec=spec),
                case.dict_revision,verify,publications=resolved,all_live=all_live,generated_inputs=generated,recheck_live=True)
            case,user=await admission.authorize(db,case_id,user_id)
            async with committing(db,case,content_write=False):
                replay=await execution.lookup_submission(db,case_id,user_id,key,digest)
                if replay is not None:return replay,False
                case,job,attempt,submission=await source(db,identifier,user)
                require_source_attempt(attempt,body)
                await require_rerun_confirmation(db,case,body,lease=lease)
                if operation_record('rerun',job,attempt)!=expected:
                    raise ExecutionConflict('state_conflict','原尝试在准备期间发生变化')
                await trusted(db,job,attempt,submission,allow_reprepare=reprepare is not None)
                drained(case,job,attempt)
                await observations.require_current(db)
                rerun=await submit_rerun(db,case,user,job,prepared,record,key,digest,
                                         inputs=(spec,paths,publications),staged=staged)
                return rerun,True


async def perform(db,identifier,user,body,key,*,request=None):
    execution.validate_key(key)
    if key is None:raise ExecutionConflict('idempotency_required','重复执行操作需要 Idempotency-Key')
    action=body['action']
    if action not in {'retry','rerun','resume'}:raise ExecutionConflict('invalid_request','未知重复执行操作')
    reprepare=body.get('reprepare_handler')
    if reprepare is not None and action!='rerun':
        raise ExecutionConflict('invalid_request','旧专业处理器重新准备只允许按当前输入重跑，不能重试或续算旧命令')
    case,job,attempt,submission=await source(db,identifier,user)
    digest=execution.request_hash({'operation':'job.repeat','source_job_id':identifier,**body})
    # A committed replay is read-only. An unrelated storage settlement or
    # backend outage must not force it to reacquire write capacity. Recheck
    # again under admission below when no committed request was observed.
    replay=await execution.lookup_submission(db,case.id,user.id,key,digest)
    if replay is not None:return replay,False
    if action=='retry':
        from .request_lifecycle import wait
        from .workflow_fallback import JOB_STAGE
        require_retry_parameters(body)
        settle_fallback=job.status=='failed' and job.type in JOB_STAGE
        prepare=staged_retry if storage._use_s3() else staged_local_retry
        return await wait(prepare(db,identifier,case.id,user.id,body,key,digest,
                                  settle_fallback=settle_fallback),request=request)
    if action=='rerun':
        from .request_lifecycle import wait
        prepare=staged_remote_rerun if storage._use_s3() else staged_local_rerun
        return await wait(prepare(db,identifier,case.id,user.id,body,key,digest),request=request)
    from .checkpoint_seal import start_job
    from .request_lifecycle import wait
    return await wait(start_job(db,identifier,case.id,user.id,body,key,digest),request=request)

async def rerun_inputs(db,case,document,prepared):
    spec=prepared.spec if prepared else deepcopy(document['execution_spec'])
    paths=prepared.input_paths if prepared else None
    publications=prepared.input_publications if prepared else None
    if prepared is None:
        manifest=document['workspace']
        if manifest['source'].get('selection_mode')!='case':paths=manifest['selection']
        if manifest.get('publications'):
            from .artifact_publication import current
            publications=[]
            for reference in manifest['publications']:
                selected=dict(reference)
                if reference.get('require_current',True):
                    original=await db.get(ArtifactPublication,reference['publication_id'])
                    _,fresh=await current(db,case.id,original.kind) if original is not None else (None,None)
                    if fresh is None:raise ExecutionConflict('input_stale','所选上游成果没有可用的当前版本')
                    selected['publication_id']=fresh.id
                publications.append({k:selected[k] for k in ('publication_id','paths','require_current') if k in selected})
    return spec,paths,publications


async def submit_rerun(db,case,user,old,prepared,record,key,digest,*,inputs,staged):
    spec,paths,publications=inputs
    job,_=await execution.submit(db,case_id=case.id,user_id=user.id,key=key,digest=digest,
        job_type=prepared.job_type if prepared else old.type,command=spec['command'],spec=spec,
        input_paths=paths,input_publications=publications,
        generated_inputs=prepared.generated_inputs if prepared else None,isolate_default=prepared is None,
        staged_workspace=staged)
    attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
    if prepared is not None:await step_handlers.bind(db,case,job,attempt,prepared,deliver_intent=False)
    await attach(db,job,attempt,record)
    from .distributed_queue import distributed_enabled
    await job_delivery.request(db,job,user_id=user.id,mode='redis' if distributed_enabled() else 'local')
    return job
