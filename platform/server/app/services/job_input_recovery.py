"""Explicit fresh-input replacement of an unsealed, never-claimed request.

The original is cancelled atomically with the independent sealed request and
audit link. Claimed/unknown writers cannot use this path to bypass stop proofs.
"""
import json, re, uuid
from datetime import datetime

from sqlalchemy import inspect, or_, select

from ..models import CaseEventCounter, JobAttempt, JobInputRecovery
from . import attempt_workspace, job_delivery, job_execution as execution, job_repeat, step_handlers
from .case_write_capacity import committing
from .durable_events import append_event, json_text, now
from .workflow_state import ExecutionConflict

SCHEMA='vibeflow.job-input-recovery.v1'
ACTION='reprepare_current_inputs'


def fingerprint(job,attempt,submission):
    def raw(row):
        if row is None:return None
        return {column.key:(getattr(row,column.key).isoformat() if hasattr(getattr(row,column.key),'isoformat') else getattr(row,column.key)) for column in inspect(type(row)).columns}
    return execution.request_hash([raw(job),raw(attempt),raw(submission)])


async def eligible(db,job,attempt):
    if attempt is None:raise ExecutionConflict('recovery_required','旧请求没有可核验的执行尝试，请保留现场人工核查')
    if attempt.step_id is not None:raise ExecutionConflict('workflow_recovery_required','工作流请求需要在原流程中核对输入并重新计算')
    if job.status not in {'queued','cancelled'} or attempt.phase!=job.status:
        raise ExecutionConflict('recovery_required','此入口仅接受尚未执行或已取消的未封存请求')
    try:
        unknown=any(json.loads(value)!={} for value in (attempt.worker_identity_json,attempt.process_identity_json,
            attempt.child_processes_json,attempt.write_checkpoint_json,attempt.output_manifest_json,attempt.publication_json))
    except (ValueError,TypeError):raise ExecutionConflict('evidence_invalid','原请求的执行或写入记录损坏，请保留现场') from None
    if (attempt.claim_token is not None or attempt.worker_id is not None or attempt.lease_expires_at is not None
            or attempt.heartbeat_at is not None or attempt.started_at is not None
            or unknown):
        raise ExecutionConflict('recovery_required','原请求可能已执行或保留写入，不能按未执行请求重新准备')
    attempts=list(await db.scalars(select(JobAttempt).where(JobAttempt.job_id==job.id)))
    if len(attempts)!=1 or attempt.root_job_id!=job.id or attempt.number!=1 or attempt.retry_of_attempt_id is not None:
        raise ExecutionConflict('recovery_required','原尝试关系需要人工核查，不能自动重建')
    from ..models import JobExecutionHead
    head=await db.get(JobExecutionHead,job.id,populate_existing=True)
    if head is None or head.active_attempt_id!=attempt.id:raise ExecutionConflict('evidence_invalid','原请求执行关系无法核验')
    try:attempt_workspace.require_sealed(job,attempt)
    except ExecutionConflict:pass
    else:raise ExecutionConflict('invalid_request','原请求已有封存输入，请使用重试或重跑入口')
    from . import jobrunner
    if jobrunner.get_proc(job.id) is not None:raise ExecutionConflict('recovery_required','原请求仍由当前实例管理，请先核验其执行与收尾')


def receipt(row):
    try:
        value=json.loads(row.payload_json)
        fields={'schema','id','case_id','actor_user_id','source_job_id','source_attempt_id','source_snapshot_sha256',
            'target_job_id','target_attempt_id','request_sha256','recorded_at','expected_revision','expected_event_sequence',
            'handler','note','old_execution_replayed','original_inputs_reconstructed','stop_evidence_created','new_execution_requested'}
        if (not isinstance(value,dict) or set(value)!=fields or value['schema']!=SCHEMA or value['id']!=row.id or value['source_job_id']!=row.source_job_id
                or value['target_job_id']!=row.target_job_id or value['source_attempt_id']!=row.source_attempt_id
                or value['case_id']!=row.case_id or value['actor_user_id']!=row.actor_user_id
                or value['old_execution_replayed'] is not False or value['original_inputs_reconstructed'] is not False
                or value['stop_evidence_created'] is not False or value['new_execution_requested'] is not True):
            raise ValueError('Invalid input recovery receipt')
        for key in ('id','source_attempt_id','target_attempt_id'):
            if str(uuid.UUID(value[key]))!=value[key]:raise ValueError('Invalid input recovery identity')
        for key in ('source_snapshot_sha256','request_sha256'):
            if not isinstance(value[key],str) or not re.fullmatch('[0-9a-f]{64}',value[key]):raise ValueError('Invalid input recovery digest')
        for key in ('case_id','actor_user_id','source_job_id','target_job_id','expected_event_sequence'):
            if type(value[key]) is not int or value[key]<=0:raise ValueError('Invalid input recovery integer')
        if type(value['expected_revision']) is not int or value['expected_revision']<0:raise ValueError('Invalid input recovery revision')
        if (not isinstance(value['note'],str) or len(value['note'])>2000 or '\0' in value['note']
                or datetime.fromisoformat(value['recorded_at']).tzinfo is None):raise ValueError('Invalid input recovery annotation')
        if value['handler'] is not None and (not isinstance(value['handler'],dict) or set(value['handler'])!={'key','version'}
                or any(not isinstance(v,str) or not v or len(v)>200 for v in value['handler'].values())):
            raise ValueError('Invalid input recovery handler')
        return value
    except (ValueError,KeyError,TypeError,AttributeError):raise ExecutionConflict('evidence_invalid','输入恢复审计无法核验，请保留记录') from None


async def links(db,job_id):
    return [receipt(row) for row in await db.scalars(select(JobInputRecovery).where(
        or_(JobInputRecovery.source_job_id==job_id,JobInputRecovery.target_job_id==job_id)).order_by(JobInputRecovery.created_at))]


async def options(db,identifier,user):
    case,job,attempt,submission=await job_repeat.source(db,identifier,user)
    reason=None
    try:await eligible(db,job,attempt)
    except (ValueError,TypeError,AttributeError):reason={'code':'evidence_invalid','message':'原请求记录损坏，请保留现场核查'}
    except ExecutionConflict as error:reason={'code':error.code,'message':error.detail}
    existing=await links(db,job.id)
    if any(row['source_job_id']==job.id for row in existing):reason={'code':'replacement_exists','message':'此旧请求已有独立的新请求，请查看关联作业'}
    choices=job_repeat.reprepare_candidates(job)
    if reason is None and job.type!='shell' and not choices:
        reason={'code':'recovery_required','message':'此旧作业类型没有对应的注册处理器，请保留原请求并人工核查'}
    counter=await db.get(CaseEventCounter,case.id)
    from .workflow_recovery import job_reference
    return {'job_id':job.id,'case_id':case.id,'available':reason is None,'reason':reason,'action':ACTION,
        'source_attempt_id':attempt.id if attempt else None,'source_snapshot_sha256':fingerprint(job,attempt,submission),
        'expected_revision':case.dict_revision,'expected_event_sequence':counter.next_seq if counter else 1,
        'command_available':job.type=='shell','handler_choices':choices,'links':existing,
        'workflow':await job_reference(db,job,attempt),
        'original_inputs_reconstructed':False,'old_execution_replayed':False,'stop_evidence_created':False}


async def replay(db,case,user,identifier,key,digest):
    job=await execution.lookup_submission(db,case.id,user.id,key,digest)
    if job is None:return None
    row=await db.scalar(select(JobInputRecovery).where(JobInputRecovery.target_job_id==job.id))
    if row is None or row.source_job_id!=identifier or row.actor_user_id!=user.id:
        raise ExecutionConflict('evidence_invalid','已提交的新请求缺少匹配恢复审计，请保留现场')
    return job,receipt(row),False


def command_spec(case,body):
    from .command_safety import command_block_reason
    command=body['command'];reason=command_block_reason(command)
    if reason:raise ExecutionConflict('invalid_request',reason)
    return {'command':command,'runtime':body['runtime'],'case_prefix':case.s3_prefix,'argv':None,
        'timeout_seconds':body['timeout_seconds'],'max_log_bytes':body['max_log_bytes'],'evidence_run_dir':None}


async def perform(db,identifier,user,body,key,*,request=None):
    execution.validate_key(key)
    if key is None:raise ExecutionConflict('idempotency_required','输入重新准备需要固定的 Idempotency-Key')
    case,job,attempt,submission=await job_repeat.source(db,identifier,user)
    digest=execution.request_hash({'operation':'job.input-recovery','source_job_id':identifier,**body})
    previous=await replay(db,case,user,identifier,key,digest)
    if previous is not None:return previous
    prepared=None
    async with committing(db,case,content_write=False):
        previous=await replay(db,case,user,identifier,key,digest)
        if previous is not None:return previous
        case,job,attempt,submission=await job_repeat.source(db,identifier,user)
        await eligible(db,job,attempt)
        if await db.scalar(select(JobInputRecovery.id).where(JobInputRecovery.source_job_id==job.id)) is not None:
            raise ExecutionConflict('replacement_exists','原请求已有独立的新请求，请查看关联作业')
        if fingerprint(job,attempt,submission)!=body['source_snapshot_sha256'] or attempt.id!=body['source_attempt_id']:
            raise ExecutionConflict('state_conflict','原请求已变化，请重新核对输入恢复方案')
        counter=await db.get(CaseEventCounter,case.id,populate_existing=True)
        if case.dict_revision!=body['expected_revision'] or (counter.next_seq if counter else 1)!=body['expected_event_sequence']:
            raise ExecutionConflict('input_stale','当前输入或事件已变化，请刷新后重新确认')
        handler=body.get('handler')
        if handler is None:
            if job.type!='shell' or not body.get('command') or body.get('parameters') is not None:
                raise ExecutionConflict('invalid_request','普通命令需明确新命令；专业请求需选择注册处理器及参数')
            command_spec(case,body)
        else:
            if handler not in job_repeat.reprepare_candidates(job) or body.get('command') is not None or body.get('parameters') is None or body.get('input_paths') is not None:
                raise ExecutionConflict('invalid_request','专业重新准备需选择原作业类型的注册处理器并明确参数')
            target=step_handlers.get_handler(handler['key'],handler['version'])
            from .job_repeat_parameters import public_parameters
            prepared=await target.prepare(db,case,user,parameters=public_parameters(target,body['parameters']),
                budget={'timeout_seconds':body['timeout_seconds'],'max_log_bytes':body['max_log_bytes']})
        if prepared is None or prepared.admission is None:
            return await submit(db,case,user,job,attempt,body,prepared,key,digest)
    from . import document_admission as admission
    prepared=await admission.complete(prepared,case.id,user.id,request=request)
    case,user=await admission.authorize(db,case.id,user.id)
    async with committing(db,case,content_write=False):
        previous=await replay(db,case,user,identifier,key,digest)
        if previous is not None:return previous
        case,job,attempt,submission=await job_repeat.source(db,identifier,user)
        await eligible(db,job,attempt)
        if fingerprint(job,attempt,submission)!=body['source_snapshot_sha256']:
            raise ExecutionConflict('state_conflict','原请求在准备期间发生变化，请重新核查')
        await admission.validate(db,case,user,prepared)
        return await submit(db,case,user,job,attempt,body,prepared,key,digest)


async def submit(db,case,user,old,old_attempt,body,prepared,key,digest):
    if await db.scalar(select(JobInputRecovery.id).where(JobInputRecovery.source_job_id==old.id)) is not None:
        raise ExecutionConflict('replacement_exists','原请求已有独立的新请求，请查看关联作业')
    from .quota import check_job_quota
    await check_job_quota(db,user, case_id=case.id)
    # No claimed process exists: this is a durable cancellation decision only.
    if old.status=='queued':await execution.request_cancel(db,old.id)
    spec=prepared.spec if prepared else command_spec(case,body)
    job,created=await execution.submit(db,case_id=case.id,user_id=user.id,key=key,digest=digest,
        job_type=prepared.job_type if prepared else 'shell',command=spec['command'],spec=spec,
        input_paths=prepared.input_paths if prepared else body.get('input_paths'),
        input_publications=prepared.input_publications if prepared else None,
        generated_inputs=prepared.generated_inputs if prepared else None,isolate_default=prepared is None)
    if not created:raise ExecutionConflict('state_conflict','新请求已提交，请使用相同恢复请求读取原回执')
    attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
    if prepared is not None:await step_handlers.bind(db,case,job,attempt,prepared,deliver_intent=False)
    record={'schema':SCHEMA,'id':str(uuid.uuid4()),'case_id':case.id,'actor_user_id':user.id,
        'source_job_id':old.id,'source_attempt_id':old_attempt.id,'source_snapshot_sha256':body['source_snapshot_sha256'],
        'target_job_id':job.id,'target_attempt_id':attempt.id,'request_sha256':digest,'recorded_at':now().isoformat(),
        'expected_revision':body['expected_revision'],'expected_event_sequence':body['expected_event_sequence'],
        'handler':body.get('handler'),'note':body['note'],'old_execution_replayed':False,
        'original_inputs_reconstructed':False,'stop_evidence_created':False,'new_execution_requested':True}
    db.add(JobInputRecovery(id=record['id'],case_id=case.id,source_job_id=old.id,source_attempt_id=old_attempt.id,
        target_job_id=job.id,actor_user_id=user.id,payload_json=json_text(record),created_at=datetime.fromisoformat(record['recorded_at'])))
    manifest=json.loads(attempt.input_manifest_json);manifest['input_recovery']=record;attempt.input_manifest_json=json_text(manifest)
    await append_event(db,case.id,'job.inputs_reprepared',record,attempt_id=attempt.id)
    from .distributed_queue import distributed_enabled
    await job_delivery.request(db,job,user_id=user.id,mode='redis' if distributed_enabled() else 'local')
    await db.flush()
    return job,record,True
