"""Explicit new workflow/attempt transactions from authenticated stopped work.

Preparation has no control transaction. The final admission rechecks its source
and read set, then commits the graph, sealed retry and delivery intent together.
Old runs, Jobs and attempts are never reopened or rewritten.
"""
import asyncio
from contextlib import AsyncExitStack
import json
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import inspect, select
from .. import storage

from ..models import ArtifactPublication, Case, Job, JobAttempt, JobExecutionHead, JobSubmissionRequest, WorkflowRun, WorkflowStep
from . import job_execution, job_repeat, step_handlers, workflow_scheduler as scheduler
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict
from .durable_events import append_event, json_text
from .publication_read_set import PublicationReadSet, encoded
from .workflow_state import ExecutionConflict, TERMINAL, transition

SCHEMA = 'vibeflow.workflow-plan.v5'
STAGES = {'geometry':'A', 'mesh':'B', 'udf-build':'C', 'solve':'C', 'postprocess':'D', 'report':'E'}
FIELDS = {'source_run_id', 'source_run_version', 'source_step_id', 'source_step_version',
          'source_attempt_id', 'source_attempt_version', 'source_job_id', 'source_sha256',
          'step_key', 'handler', 'handler_version', 'start_state', 'mode',
          'checkpoint_time', 'checkpoint_sha256', 'reused'}


def make_plan(target, parameters, lineage):
    if not isinstance(lineage, dict) or set(lineage) != FIELDS:
        raise ValueError('Invalid workflow resume lineage')
    for key in ('source_run_id', 'source_step_id', 'source_attempt_id'):
        if not isinstance(lineage[key], str) or str(uuid.UUID(lineage[key])) != lineage[key]:
            raise ValueError('Invalid resume source identity')
    for key in ('source_run_version', 'source_step_version', 'source_attempt_version', 'source_job_id'):
        if type(lineage[key]) is not int or lineage[key] < (1 if key == 'source_job_id' else 0):
            raise ValueError('Invalid resume source version')
    if not isinstance(lineage['source_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', lineage['source_sha256']):
        raise ValueError('Invalid resume source digest')
    key = lineage['step_key']
    if key not in STAGES or lineage['start_state'] != STAGES[key] or lineage['mode'] not in {'retry_step', 'checkpoint'}:
        raise ValueError('Invalid resume boundary')
    plan = scheduler.make_plan(target, lineage['start_state'], parameters)
    selected = next((item for item in plan['steps'] if item['key'] == key), None)
    if selected is None:
        raise ValueError('Resume boundary is outside the target graph')
    handler = step_handlers.get_handler(lineage['handler'], lineage['handler_version'])
    allowed = {selected['handler']}
    if key == 'solve' and selected['handler'] == 'solve.openfoam':
        allowed.add('solve.openfoam.resume')
    if handler.key not in allowed or handler.version != selected['version']:
        raise ValueError('Resume handler differs from its registered boundary')
    if lineage['mode'] == 'checkpoint':
        if (key != 'solve' or handler.key != 'solve.openfoam.resume'
                or not isinstance(lineage['checkpoint_time'], str) or not lineage['checkpoint_time']
                or not isinstance(lineage['checkpoint_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', lineage['checkpoint_sha256'])):
            raise ValueError('A native checkpoint must have an explicit time and digest')
    elif lineage['checkpoint_time'] is not None or lineage['checkpoint_sha256'] is not None:
        raise ValueError('Retry cannot introduce a different checkpoint')
    selected['handler'] = handler.key
    # A solve retry reuses its already committed UDF library; it does not
    # silently rebuild the library with today's compiler or source files.
    reused = lineage['reused']
    expected_reuse = {'udf-build'} if key == 'solve' and parameters.get('solve_mode') == 'udf' else set()
    if not isinstance(reused, dict) or set(reused) != expected_reuse:
        raise ValueError('Invalid resume prefix')
    from .workflow_recompute import FIELDS as PUBLICATION_FIELDS
    for item in plan['steps']:
        if item['key'] in reused:
            reference = reused[item['key']]
            if not isinstance(reference, dict) or set(reference) != PUBLICATION_FIELDS or reference['kind'] != item['key']:
                raise ValueError('Invalid resume prefix publication')
            item['reuse'] = reference
    return dict(plan, schema=SCHEMA, resumption=dict(lineage))


def row_record(row):
    return {column.key: encoded(getattr(row, column.key)) for column in inspect(type(row)).column_attrs}


async def udf_reference(db, case_id, build):
    """Resolve the full immutable identity behind a five-field UI projection.

    A later publication can supersede this historical artifact. Current-head
    and source freshness are checked separately when admitting a new retry.
    """
    from .workflow_queries import object_record
    from .workflow_recompute import FIELDS as PUBLICATION_FIELDS
    if build is None or build.status != 'succeeded' or build.handler != 'udf.build.workflow':
        raise ExecutionConflict('evidence_invalid', '原UDF求解缺少已完成的正式编译步骤')
    output = object_record(build.output_manifest_json, '原UDF编译输出')
    projection = output.get('publication')
    if not isinstance(projection, dict) or projection.get('status') != 'committed':
        raise ExecutionConflict('evidence_invalid', '原UDF编译缺少已发布成果')
    row = await db.get(ArtifactPublication, projection.get('id'), populate_existing=True) if isinstance(projection.get('id'),str) else None
    if (row is None or row.case_id != case_id or row.kind != 'udf-build'
            or row.status not in {'committed','superseded'} or type(projection.get('generation')) is not int
            or any(projection.get(key) != getattr(row,key) for key in ('id','kind','generation','manifest_sha256'))):
        raise ExecutionConflict('evidence_invalid', '原UDF编译投影与实际发布记录不一致')
    reference = {key:getattr(row,key) for key in PUBLICATION_FIELDS}
    if build.active_attempt_id is not None:
        attempt = await db.get(JobAttempt, row.attempt_id, populate_existing=True)
        job = await db.get(Job, attempt.job_id, populate_existing=True) if attempt else None
        if (attempt is None or attempt.id != build.active_attempt_id or attempt.step_id != build.id
                or attempt.phase != 'succeeded' or job is None or job.case_id != case_id or job.status != 'succeeded'
                or output.get('input_attempt_id') != attempt.id):
            raise ExecutionConflict('evidence_invalid', '原UDF发布成果不属于原编译尝试')
    elif (projection.get('attempt_id') != row.attempt_id
            or object_record(build.input_manifest_json, '原UDF复用输入').get('reuse') != reference):
        raise ExecutionConflict('evidence_invalid', '原UDF复用前缀丢失完整发布引用')
    return reference


async def source_digest(db, run, step, attempt, job, submission):
    steps = list(await db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == run.id)
        .order_by(WorkflowStep.step_key).execution_options(populate_existing=True)))
    return job_execution.request_hash({'run':row_record(run), 'steps':[row_record(item) for item in steps],
        'step':row_record(step), 'attempt':row_record(attempt), 'job':row_record(job), 'submission':row_record(submission)})


async def require_binding(db, run, lineage):
    """The old source is immutable; its head may legitimately advance later."""
    source = await db.get(WorkflowRun, lineage['source_run_id'], populate_existing=True)
    step = await db.get(WorkflowStep, lineage['source_step_id'], populate_existing=True)
    attempt = await db.get(JobAttempt, lineage['source_attempt_id'], populate_existing=True)
    job = await db.get(Job, lineage['source_job_id'], populate_existing=True)
    submission = await db.scalar(select(JobSubmissionRequest).where(JobSubmissionRequest.job_id == lineage['source_job_id']))
    if (source is None or source.id == run.id or source.case_id != run.case_id or source.status not in TERMINAL
            or step is None or step.run_id != source.id or step.step_key != lineage['step_key']
            or attempt is None or attempt.step_id != step.id or step.active_attempt_id != attempt.id
            or job is None or job.case_id != run.case_id or job.id != attempt.job_id or submission is None
            or (source.version, step.version, attempt.version) != (lineage['source_run_version'],
                lineage['source_step_version'], lineage['source_attempt_version'])
            or await source_digest(db, source, step, attempt, job, submission) != lineage['source_sha256']):
        raise ExecutionConflict('evidence_invalid', '恢复来源或原历史记录已变化，请保留原流程核查')
    source_plan = json.loads(source.plan_json)
    plan = json.loads(run.plan_json)
    expected_handler = 'solve.openfoam.resume' if lineage['mode'] == 'checkpoint' else step.handler
    if (run.target != source.target or run.run_dir != source.run_dir or run.budget_json != source.budget_json
            or plan['parameters'] != source_plan['parameters'] or lineage['handler'] != expected_handler
            or lineage['handler_version'] != step.handler_version
            or (lineage['mode'] == 'checkpoint' and step.handler not in {'solve.openfoam','solve.openfoam.resume'})):
        raise ExecutionConflict('evidence_invalid', '恢复图、处理器或预算与原来源不一致')
    if lineage['reused']:
        build = await db.scalar(select(WorkflowStep).where(WorkflowStep.run_id == source.id,
            WorkflowStep.step_key == 'udf-build'))
        if lineage['reused'] != {'udf-build':await udf_reference(db, run.case_id, build)}:
            raise ExecutionConflict('evidence_invalid', '恢复前缀与原UDF编译产物不一致')
    target_step = await db.scalar(select(WorkflowStep).where(WorkflowStep.run_id == run.id,
        WorkflowStep.step_key == lineage['step_key']))
    child = await db.get(JobAttempt, target_step.active_attempt_id, populate_existing=True) if target_step and target_step.active_attempt_id else None
    if (child is None or child.step_id != target_step.id or child.retry_of_attempt_id != attempt.id
            or child.root_job_id != attempt.root_job_id):
        raise ExecutionConflict('evidence_invalid', '恢复流程未绑定原请求族的新尝试')
    child_job = await db.get(Job, child.job_id, populate_existing=True)
    handler = await step_handlers.require_binding(db, child_job, child) if child_job else None
    if handler is None or (handler.key, handler.version) != (lineage['handler'], lineage['handler_version']):
        raise ExecutionConflict('evidence_invalid', '恢复尝试的正式处理器归属不一致')
    operation = json.loads(child.input_manifest_json).get('operation', {})
    expected_operation = job_repeat.operation_record('resume' if lineage['mode'] == 'checkpoint' else 'retry', job, attempt)
    expected_operation['workflow_resume'] = {'source_run_id':source.id, 'mode':lineage['mode']}
    if lineage['mode'] == 'checkpoint':
        expected_operation.update(checkpoint_time=lineage['checkpoint_time'], checkpoint_sha256=lineage['checkpoint_sha256'])
        descriptor = json.loads(child.publication_json)['descriptor']
        if descriptor.get('checkpoint_sha256') != lineage['checkpoint_sha256']:
            raise ExecutionConflict('evidence_invalid', '恢复检查点摘要与封存尝试不一致')
    if operation != expected_operation:
        raise ExecutionConflict('evidence_invalid', '恢复尝试缺少原来源审计绑定')


async def source(db, run_id, user, step_id, *, write, expected=None):
    from .workflow_execution import owned
    from .workflow_plan_migration import source_drained
    from .workflow_recompute import checked_snapshot
    await owned(db, run_id, user, write=write)
    async with scheduler.locked_run(db, run_id) as (run, case, steps):
        plan = json.loads(run.plan_json)
        if plan.get('execution_contract') != step_handlers.CONTRACT:
            raise ExecutionConflict('recovery_required', '旧计划需要明确迁移，不能按原生检查点恢复')
        step = next((item for item in steps if item.id == step_id), None)
        if run.status not in TERMINAL or step is None or step.step_key not in STAGES:
            raise ExecutionConflict('state_conflict', '恢复需要已结束流程中的原生步骤')
        attempt = await db.get(JobAttempt, step.active_attempt_id, populate_existing=True) if step.active_attempt_id else None
        job = await db.get(Job, attempt.job_id, populate_existing=True) if attempt else None
        submission = await db.scalar(select(JobSubmissionRequest).where(JobSubmissionRequest.job_id == job.id)) if job else None
        if attempt is None or job is None or attempt.phase not in {'failed','cancelled','interrupted'}:
            raise ExecutionConflict('state_conflict', '此步骤没有可重试的失败、取消或中断尝试')
        if expected is not None:
            if (run.version, step.version, attempt.id, attempt.version) != (expected['expected_version'],
                    expected['expected_step_version'], expected['source_attempt_id'], expected['expected_attempt_version']):
                raise ExecutionConflict('state_conflict', '原流程、步骤或尝试版本已变化，请重新核对')
            if case.dict_revision != expected['expected_revision']:
                raise ExecutionConflict('revision_conflict', '工程修订已变化，请重新核对恢复方案')
        document, handler = await job_repeat.trusted(db, job, attempt, submission)
        if handler is None or (handler.key, handler.version) != (step.handler, step.handler_version):
            raise ExecutionConflict('recovery_required', '原步骤没有可信的注册处理器')
        job_repeat.drained(case, job, attempt)
        await source_drained(db, case, run)
        head = await db.get(JobExecutionHead, attempt.root_job_id, populate_existing=True)
        if head is None or head.active_attempt_id != attempt.id:
            raise ExecutionConflict('state_conflict', '原请求族已有后续尝试，请转到后续流程')
        active = await db.scalar(select(WorkflowRun.id).where(WorkflowRun.case_id == case.id,
            WorkflowRun.status.not_in(TERMINAL)).limit(1))
        if active is not None:
            raise ExecutionConflict('state_conflict', '算例还有活动流程，请先完成或取消它')
        snapshot = checked_snapshot(case)
        if snapshot['state'] != STAGES[step.step_key]:
            raise ExecutionConflict('confirmation_required', '请明确恢复工程阶段至 '+STAGES[step.step_key]+'，再核对恢复方案')
        if snapshot['engineering_sha256'] != json.loads(run.input_manifest_json)['engineering_sha256']:
            raise ExecutionConflict('input_stale', '工程输入已变化，请选择当前输入重算')
        spec = document['execution_spec']; budget = json.loads(run.budget_json)
        if not (0 < spec['timeout_seconds'] <= budget['timeout_seconds']
                and 1024 <= spec['max_log_bytes'] <= budget['max_log_bytes']):
            raise ExecutionConflict('evidence_invalid', '原步骤预算与流程预算不一致')
        reused = {}
        if step.step_key == 'solve' and plan['parameters'].get('solve_mode') == 'udf':
            build = next((item for item in steps if item.step_key == 'udf-build'), None)
            reused['udf-build'] = await udf_reference(db, case.id, build)
        return case, run, step, attempt, job, submission, handler, snapshot, reused


async def preview(db, run_id, user):
    from .workflow_execution import owned
    run = await owned(db, run_id, user, write=False)
    identifiers = list(await db.scalars(select(WorkflowStep.id).where(WorkflowStep.run_id == run.id,
        WorkflowStep.active_attempt_id.is_not(None))))
    result = {'source_run_id':run.id, 'expected_version':run.version,
        'expected_revision':(await db.get(Case, run.case_id, populate_existing=True)).dict_revision,
        'input_policy':'original_sealed_inputs', 'steps':[]}
    for identifier in identifiers:
        item = {'source_step_id':identifier, 'retry_step':{'available':False}, 'checkpoint':{'available':False}}
        try:
            _, _, step, attempt, _, _, handler, _, _ = await source(db, run_id, user, identifier, write=False)
            item.update(key=step.step_key, expected_step_version=step.version, source_attempt_id=attempt.id,
                expected_attempt_version=attempt.version, handler={'key':handler.key,'version':handler.version})
            item['retry_step'] = {'available':True}
            policy = None
            if handler.key in {'solve.openfoam','solve.openfoam.resume'}:
                from .solve_restart_policy import preview as checkpoint_policy
                policy = checkpoint_policy(attempt)
            item['checkpoint'] = {'available':policy in ('explicit','latestWritten'),
                'requires_written_time':policy!='latestWritten', 'requires_checkpoint_validation':True,
                'checkpoint_policy':policy,'supports_latest':policy=='latestWritten'}
        except ExecutionConflict as error:
            item['reason'] = {'code':error.code, 'message':error.detail}
        result['steps'].append(item)
    return result


async def start(db, run_id, user, *, key, request=None, **values):
    from .case_read_capacity import file_read_deadline
    from .request_deadline import STAGING_CLEANUP_SECONDS
    from .request_lifecycle import wait

    code,label=(('workflow_checkpoint_seal','封存工作流检查点') if values['mode']=='checkpoint'
                else ('workflow_retry_seal','封存工作流重试输入'))
    async with file_read_deadline(code,label,seconds=STAGING_CLEANUP_SECONDS):
        return await wait(_start(db,run_id,user,key=key,**values),request=request)


async def _start(db, run_id, user, *, key, **values):
    from .workflow_execution import owned, detail
    from .workflow_recompute import replay
    from .case_write_capacity import committing
    from . import case_copy_io, checkpoint_seal, command_input_seal, engineering_stage, job_delivery, local_retry_seal, publication_inputs
    from .local_source_reads import LocalSourceReads, read_source, supported
    from .quota import check_job_quota
    from ..routers.workspaces import get_owned_case
    job_execution.validate_key(key)
    if key is None or values['confirmed'] is not True:
        raise ExecutionConflict('confirmation_required', '恢复需要明确确认和固定幂等键')
    mode = values['mode']; time = values.get('checkpoint_time')
    if (mode not in {'retry_step','checkpoint'} or (mode == 'checkpoint' and not time)
            or (mode == 'retry_step' and time is not None)):
        raise ExecutionConflict('invalid_request', '原输入重试与检查点续算的参数不能混用')
    run = await owned(db, run_id, user)
    case_id, user_id = run.case_id, user.id
    digest = job_execution.request_hash(dict(values, operation='workflow.resume', source_run_id=run_id))
    existing = await replay(db, case_id, user_id, key, digest)
    if existing is not None:
        return await detail(db, existing.id, user)
    from .durable_events import lock_case_execution
    await lock_case_execution(db, case_id)
    # A peer can commit while this request is waiting for the control lock.
    # Resolve that receipt before testing the now-retired source/head.
    existing = await replay(db, case_id, user_id, key, digest)
    if existing is not None:
        return await detail(db, existing.id, user)
    case, run, step, attempt, job, submission, handler, snapshot, reused = await source(
        db, run_id, user, values['source_step_id'], write=True, expected=values)
    source_sha = await source_digest(db, run, step, attempt, job, submission)
    descriptor = json.loads(attempt.publication_json)['descriptor']
    document = json.loads(attempt.input_manifest_json)
    budget = json.loads(run.budget_json)
    parameters = json.loads(run.plan_json)['parameters']
    lineage = dict(source_run_id=run.id, source_run_version=run.version, source_step_id=step.id,
        source_step_version=step.version, source_attempt_id=attempt.id, source_attempt_version=attempt.version,
        source_job_id=job.id, source_sha256=source_sha, step_key=step.step_key, handler=handler.key,
        handler_version=handler.version, start_state=STAGES[step.step_key], mode=mode,
        checkpoint_time=None, checkpoint_sha256=None, reused=reused)
    # Detached records and short read sessions keep native checkpoint inspection
    # out of the case/SQL write lock. Both source and observations are rechecked.
    db.expunge_all()
    await db.rollback()
    reads = PublicationReadSet(); prepared = None; checkpoint = None
    files=LocalSourceReads()
    scopes=AsyncExitStack()
    lease=None
    staged=None
    try:
        owner_id=None
        if storage._use_s3():
            code,label=(('workflow_checkpoint_seal','封存工作流检查点') if mode=='checkpoint'
                        else ('workflow_retry_seal','封存工作流重试输入'))
            while True:
                existing=await replay(db,case_id,user_id,key,digest)
                if existing is not None:
                    return await detail(db,existing.id,user)
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
            existing=await replay(db,case_id,user_id,key,digest)
            if existing is not None:
                return await detail(db,existing.id,user)
            current=await db.get(Case,case_id,populate_existing=True)
            if current is None or (current.s3_prefix,current.dict_revision)!=(case.s3_prefix,case.dict_revision):
                raise ExecutionConflict('input_stale','工作流恢复准备期间算例来源已变化')
            await finish_before_cancel(db.rollback())
        scopes.enter_context(files.collect())
        for stage in 'BCDE'[:'ABCDE'.index(lineage['start_state'])]:
            reason = await engineering_stage.evidence_reason(reads, case, stage)
            if reason:
                raise ExecutionConflict('evidence_invalid', reason)
        if mode == 'checkpoint':
            if handler.key not in {'solve.openfoam','solve.openfoam.resume'}:
                raise ExecutionConflict('checkpoint_required', '此处理器没有注册真实检查点协议')
            prepared,checkpoint,staged=await scopes.enter_async_context(
                checkpoint_seal.prepare(case,user,attempt,reads,time,lease=lease,owner_id=owner_id))
            lineage.update(handler=prepared.key, checkpoint_time=checkpoint['label'],
                checkpoint_sha256=prepared.descriptor['checkpoint_sha256'])
        else:
            actual = await finish_before_cancel(asyncio.to_thread(read_source,handler,case.s3_prefix,descriptor))
            if actual != descriptor['source']:
                raise ExecutionConflict('input_stale', '专业输入已变化，请选择当前输入重算')
            await publication_inputs.validate(reads, case, document['workspace'])
            if lease is not None:
                staged=await command_input_seal.upload_retry(lease,owner_id,case.dict_revision,document['workspace'])
            else:
                scopes.enter_context(case_copy_io.admit(code='workflow_retry_seal_busy',label='封存工作流重试输入'))
                staged=await scopes.enter_async_context(local_retry_seal.prepare(case,document['workspace']))
        reads.seal()
        files.seal()
        plan = make_plan(run.target, parameters, lineage)
        case = await get_owned_case(case_id, user, db, write=True)
        async with committing(db, case, expected_revision=values['expected_revision'], content_write=False):
            existing = await replay(db, case_id, user_id, key, digest)
            if existing is not None:
                return await detail(db, existing.id, user)
            case, original, step, attempt, job, submission, handler, current, current_reused = await source(
                db, run_id, user, values['source_step_id'], write=True, expected=values)
            if (await source_digest(db, original, step, attempt, job, submission) != source_sha
                    or current != snapshot or current_reused != reused):
                raise ExecutionConflict('state_conflict', '恢复准备期间来源或工程状态已变化')
            await reads.require_current(db)
            if lease is None:
                if not supported(handler):
                    actual = await finish_before_cancel(asyncio.to_thread(handler.source,case.s3_prefix,descriptor))
                    if actual != descriptor['source']:
                        raise ExecutionConflict('input_stale','恢复提交前专业输入已变化')
                await files.require_current()
                if files.uncovered:
                    await publication_inputs.validate(db,case,document['workspace'])
            await check_job_quota(db, user, case_id=case.id)
            new = WorkflowRun(id=str(uuid.uuid4()), case_id=case_id, org_id=original.org_id, created_by=user_id,
                target=original.target, request_hash=digest, idempotency_key=key,
                expected_revision=case.dict_revision, run_dir=original.run_dir,
                plan_json=json_text(plan), input_manifest_json=json_text(snapshot), budget_json=json_text(budget))
            db.add(new); await db.flush()
            steps = []
            for definition in plan['steps']:
                item = WorkflowStep(run_id=new.id, step_key=definition['key'], handler=definition['handler'],
                    handler_version=definition['version'], dependencies_json=json_text(definition['dependencies']),
                    confirmation_state=definition['confirmation_state'])
                db.add(item); steps.append(item)
            await db.flush()
            await append_event(db, case_id, 'workflow.created', {'status':'queued', 'target':new.target,
                'version':0, 'step_count':len(steps), 'source_run_id':run_id, 'resume_mode':mode}, run_id=new.id)
            await transition(db, 'run', new.id, 'running', expected_version=new.version)
            preflight = steps[0]
            for state in ('ready','running','finalizing','succeeded'):
                await scheduler.move_step(db, preflight, state)
            preflight.output_manifest_json = json_text(snapshot)
            # Only the validated UDF artifact may precede the retry boundary.
            for item in steps[1:]:
                if item.step_key == lineage['step_key']:
                    await scheduler.move_step(db, item, 'ready'); selected = item; break
                reference = reused.get(item.step_key)
                if reference is None:
                    raise ExecutionConflict('evidence_invalid', '恢复图含有未经验证的前置执行')
                await scheduler.move_step(db, item, 'ready')
                from .workflow_recompute import complete_reuse
                await complete_reuse(db, new, case, item, reference)
                if item.status != 'succeeded':
                    raise ExecutionConflict('input_stale', '原UDF编译产物已失效')
            retry, child = await job_execution.allocate_retry(db, attempt.id, registered=True, user_id=user_id,
                key='workflow.'+new.id+'.'+selected.step_key, digest=digest,
                continuation=(prepared,checkpoint) if prepared else None, step_id=selected.id,staged_workspace=staged)
            selected.active_attempt_id = child.id
            record = job_repeat.operation_record('resume' if mode == 'checkpoint' else 'retry', job, attempt)
            record['workflow_resume'] = {'source_run_id':run_id, 'mode':mode}
            if prepared:
                record.update(checkpoint_time=checkpoint['label'], checkpoint_sha256=lineage['checkpoint_sha256'])
            await job_repeat.attach(db, retry, child, record)
            selected.input_manifest_json = child.input_manifest_json
            await scheduler.move_step(db, selected, 'running')
            await require_binding(db, new, lineage)
            from .distributed_queue import distributed_enabled
            await job_delivery.request(db, retry, user_id=user_id, mode='redis' if distributed_enabled() else 'local')
            await append_event(db, case_id, 'workflow.resumed', {'source_run_id':run_id,
                'source_step_id':step.id, 'source_attempt_id':attempt.id, 'mode':mode,
                'checkpoint_time':lineage['checkpoint_time']}, run_id=new.id, step_id=selected.id, attempt_id=child.id)
            new_id, job_id = new.id, retry.id
        await scopes.aclose()
        await step_handlers.launch(job_id)
        return await detail(db, new_id, user)
    finally:
        await scopes.aclose()
