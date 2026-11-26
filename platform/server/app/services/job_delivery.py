"""Durable launch intent; SQL claim is authoritative across repeated deliveries.

An intent is committed with admission, before any process or queue operation.
Legacy queued rows without an intent are never guessed into executable work.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import re

from fastapi import HTTPException
from sqlalchemy import case as sql_case, select, func, or_
from sqlalchemy.orm import aliased

from ..db import SessionLocal
from ..models import Case, DurableEvent, Job, JobAttempt, JobSubmissionRequest, User, Workspace
from .durable_events import json_text, lock_case_execution
from .workflow_state import ExecutionConflict, TERMINAL

EVENT = 'attempt.delivery_requested'
SCHEMA = 'vibeflow.job-delivery.v1'
FIELDS = {'schema','case_id','job_id','attempt_id','requested_by','host_id','mode','spec_sha256','batch'}
TARGET_SCHEMA = 'vibeflow.job-delivery.v2'
TARGET_FIELDS = FIELDS | {'origin_host_id','target_platform'}


class DeliveryDeferred(ExecutionConflict):
    def __init__(self, detail):
        super().__init__('delivery_deferred', detail)


async def intent(db, job, attempt):
    events = list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id == attempt.id,
        DurableEvent.type == EVENT).order_by(DurableEvent.seq)))
    return validate_intent(job, attempt, events)


def validate_intent(job, attempt, events):
    if not events:
        return None
    from .job_execution import request_hash
    try:
        value = json.loads(events[0].payload_json)
        spec = json.loads(attempt.input_manifest_json)['execution_spec']
        targeted = isinstance(value,dict) and value.get('schema')==TARGET_SCHEMA
        if (len(events) != 1 or events[0].case_id != job.case_id or not isinstance(value,dict)
                or set(value) != (TARGET_FIELDS if targeted else FIELDS)
                or value['schema'] != (TARGET_SCHEMA if targeted else SCHEMA)
                or type(value['case_id']) is not int or value['case_id'] != job.case_id
                or type(value['job_id']) is not int or value['job_id'] != job.id
                or value['attempt_id'] != attempt.id or attempt.job_id != job.id
                or type(value['requested_by']) is not int or value['requested_by'] <= 0
                or value['mode'] not in {'local','redis'}
                or not isinstance(value['host_id'],str) or not re.fullmatch('[0-9a-f]{64}',value['host_id'])
                or value['spec_sha256'] != request_hash(spec) or job.command != spec['command']):
            raise ValueError('Invalid delivery binding')
        if targeted and (value['mode']!='redis' or value['target_platform'] not in {'windows','linux'}
                or not isinstance(value['origin_host_id'],str)
                or not re.fullmatch('[0-9a-f]{64}',value['origin_host_id'])):
            raise ValueError('Invalid targeted delivery binding')
        group = value['batch']
        if group is not None:
            if (not isinstance(group,dict) or set(group) != {'id','jobs','max_concurrent'}
                    or not isinstance(group['id'],str) or not re.fullmatch('[0-9a-f]{32}',group['id'])
                    or type(group['max_concurrent']) is not int or not 1 <= group['max_concurrent'] <= 4
                    or not isinstance(group['jobs'],list) or not 1 <= len(group['jobs']) <= 50
                    or any(type(v) is not int or v <= 0 for v in group['jobs'])
                    or len(set(group['jobs'])) != len(group['jobs']) or job.id not in group['jobs']):
                raise ValueError('Invalid batch delivery binding')
        return value
    except (ValueError,TypeError,KeyError,AttributeError) as error:
        raise ExecutionConflict('evidence_invalid','Durable delivery intent is damaged') from error


async def request(db, job, *, user_id, mode='local', batch=None):
    from . import job_execution, process_identity_native
    from .job_execution import attempt_event
    await lock_case_execution(db, job.case_id)
    await db.refresh(job)
    attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id)
        .order_by(JobAttempt.number.desc()).limit(1).execution_options(populate_existing=True))
    if attempt is None or job.status != 'queued' or attempt.phase != 'queued' or attempt.claim_token is not None:
        raise ExecutionConflict('state_conflict','Only an unclaimed admitted attempt can request delivery')
    from .attempt_workspace import require_sealed
    require_sealed(job,attempt)
    spec = json.loads(attempt.input_manifest_json)['execution_spec']
    value = {'schema':SCHEMA,'case_id':job.case_id,'job_id':job.id,'attempt_id':attempt.id,
        'requested_by':user_id,'host_id':process_identity_native.host_id(),'mode':mode,
        'spec_sha256':job_execution.request_hash(spec),'batch':batch}
    from .job_execution_hosts import requires_remote, selected
    from ..config import get_settings
    handler=None
    if get_settings().job_handler_execution_hosts:
        from .step_handlers import validate_binding
        handler=validate_binding(job,attempt)
    target = selected(spec['runtime'], mode, handler=handler.key if handler else None)
    if target is None and await requires_remote(db,job,attempt):
        raise ExecutionConflict('runtime_unavailable','当前求解处理器没有部署可用的远程 worker 路由')
    if target is not None:
        value.update(schema=TARGET_SCHEMA, origin_host_id=value['host_id'],
            host_id=target['host_id'], target_platform=target['platform'])
    previous = await intent(db,job,attempt)
    if previous is not None:
        if previous != value:
            raise ExecutionConflict('state_conflict','Delivery was already requested with a different policy')
        return attempt.id
    await attempt_event(db,job,attempt,EVENT,value)
    await intent(db,job,attempt)  # Validate the exact durable representation.
    return attempt.id


def occupying(case, job, attempt):
    """Unknown cleanup retains capacity; never-claimed cancellations do not."""
    if attempt is None:
        return job.status == 'running'
    if attempt.claim_token is None:
        return job.status == 'running'
    if job.status not in TERMINAL or attempt.phase not in TERMINAL:
        return True
    from .job_write_checkpoint import validate
    from .job_execution import completion_visibility
    try:
        validate(case,job,attempt)
        if completion_visibility(attempt) in {'pending','invalid'}:
            return True
    except (ValueError,TypeError,KeyError,AttributeError):
        return True
    return False


def latest_attempt_id():
    candidate = aliased(JobAttempt)
    return (select(candidate.id).where(candidate.job_id == Job.id)
            .order_by(candidate.number.desc()).limit(1).correlate(Job).scalar_subquery())


def jobs_requested_by(user_id, *, latest_only):
    """Use committed delivery actor, with the legacy submission/owner fallback."""
    events = select(DurableEvent.id).where(DurableEvent.case_id == Case.id,
                                          DurableEvent.attempt_id == JobAttempt.id, DurableEvent.type == EVENT)
    payload = DurableEvent.payload_json
    for whitespace in (' ', '\n', '\r', '\t'):
        payload = func.replace(payload, whitespace, '')
    requester = f'"requested_by":{user_id}'
    requested = events.where(or_(payload.contains(requester + ','), payload.contains(requester + '}'))).exists()
    fallback = func.coalesce(JobSubmissionRequest.created_by, Workspace.user_id) == user_id
    query = (select(Case, Job, JobAttempt).select_from(Job)
             .join(Case, Case.id == Job.case_id).join(Workspace, Workspace.id == Case.workspace_id))
    if latest_only:
        query = query.outerjoin(JobAttempt, JobAttempt.id == latest_attempt_id())
    else:
        query = query.join(JobAttempt, JobAttempt.job_id == Job.id)
    return (query.outerjoin(JobSubmissionRequest, JobSubmissionRequest.job_id == Job.id)
            .where(or_(requested, fallback & ~events.exists())))


@asynccontextmanager
async def occupied_jobs(db, owner_id, *, exclude=None):
    query = (select(Case, Job, JobAttempt).select_from(Job)
             .join(Case, Case.id == Job.case_id).join(Workspace, Workspace.id == Case.workspace_id)
             .outerjoin(JobAttempt, JobAttempt.id == latest_attempt_id())
             .where(Workspace.user_id == owner_id, or_(Job.status == 'running', JobAttempt.claim_token.is_not(None))))
    if exclude is not None:
        query = query.where(Job.id != exclude)
    rows = await db.stream(query.execution_options(yield_per=32))
    try:
        yield (job.id async for case, job, attempt in rows if occupying(case, job, attempt))
    finally:
        await rows.close()


async def require_capacity(db, case, job, attempt, spec, *, dispatch_only=False):
    """Called under case control before the claim, not after native creation.

    Lock order is case control followed by account rows in ID order. Counting
    other cases does not acquire their control locks.
"""
    from . import linux_account_capacity as account_capacity
    value = await intent(db,job,attempt)
    account_limit = account_capacity.limit()
    if value is None:
        if account_limit:
            requester = await db.scalar(select(JobSubmissionRequest.created_by).where(JobSubmissionRequest.job_id == job.id))
            if requester is None:
                requester = await db.scalar(select(Workspace.user_id).where(Workspace.id == case.workspace_id))
            await account_capacity.lock_accounts(db, requester)
            if await account_capacity.full_locked(db, requester, account_limit):
                raise DeliveryDeferred(account_capacity.MESSAGE)
        return
    from .attempt_workspace import require_sealed
    require_sealed(job,attempt)
    from . import process_identity_native
    from ..routers.workspaces import get_owned_case
    from .quota import occupied_remote_jobs, quotas_for
    if not (dispatch_only and value['schema']==TARGET_SCHEMA):
        if value['host_id'] != process_identity_native.host_id():
            raise DeliveryDeferred('Delivery belongs to another runtime host')
        if value['schema']==TARGET_SCHEMA:
            from .job_execution_hosts import platform_name
            if value['target_platform']!=platform_name():
                raise DeliveryDeferred('Delivery requires a different runtime platform')
    if json.loads(attempt.input_manifest_json)['execution_spec'] != spec:
        raise ExecutionConflict('evidence_invalid','Delivery differs from its admitted execution specification')
    caller = await db.get(User,value['requested_by'])
    if caller is None or caller.status != 'active':
        raise DeliveryDeferred('Delivery requester is no longer available')
    try:
        await get_owned_case(case.id,caller,db,write=True)
    except HTTPException as error:
        raise DeliveryDeferred('Delivery requester no longer has case write access') from error
    workspace = await db.get(Workspace,case.workspace_id)
    if dispatch_only and value.get('target_platform') == 'windows':
        account_limit = 0
    await account_capacity.lock_accounts(db, workspace.user_id, caller.id if account_limit else workspace.user_id)
    if account_limit and await account_capacity.full_locked(db, caller.id, account_limit):
        raise DeliveryDeferred(account_capacity.MESSAGE)
    owner = await db.get(User,workspace.user_id)
    quota = (await quotas_for(db,owner))['job_concurrent']
    if type(quota) is not int or quota < 1:
        raise DeliveryDeferred('No job capacity is configured')
    remote = await occupied_remote_jobs(db,workspace.user_id)
    if remote >= quota:
        raise DeliveryDeferred('Job concurrency capacity is occupied')
    used = set()
    async with occupied_jobs(db, workspace.user_id, exclude=job.id) as jobs:
        async for identifier in jobs:
            used.add(identifier)
            if remote + len(used) >= quota:
                raise DeliveryDeferred('Job concurrency capacity is occupied')
    group = value['batch']
    if group is not None:
        members = (await db.execute(select(Job,JobAttempt).join(JobAttempt,JobAttempt.id==latest_attempt_id())
            .where(Job.case_id==case.id,Job.id.in_(group['jobs'])))).all()
        if len(members)!=len(group['jobs']):
            raise ExecutionConflict('evidence_invalid','Batch delivery membership is incomplete')
        for member,member_attempt in members:
            other = await intent(db,member,member_attempt)
            if (other is None or other['batch'] != group or other['requested_by'] != value['requested_by']
                    or other['host_id'] != value['host_id'] or other['mode'] != value['mode']
                    or other['schema']!=value['schema'] or other.get('target_platform')!=value.get('target_platform')):
                raise ExecutionConflict('evidence_invalid','Batch delivery policies disagree')
        if len(used.intersection(group['jobs'])) >= group['max_concurrent']:
            raise DeliveryDeferred('Batch concurrency capacity is occupied')
    from .workflow_batches import require_capacity as workflow_batch_capacity
    await workflow_batch_capacity(db,case,job,attempt,used)


async def refuse_unsealed(job_id):
    """Persist a diagnosis for old queued inputs without claiming or rewriting them."""
    from . import job_recovery_notice as notices
    from .attempt_workspace import require_sealed
    async with SessionLocal() as db:
        records=await notices.rows(db,job_id,lock=True)
        _,job,attempt,_=records
        if job is None or job.status!='queued':return False
        if attempt and (attempt.phase!='queued' or attempt.claim_token is not None):return False
        try:require_sealed(job,attempt)
        except ExecutionConflict:
            await notices.record(db,records,'input_recovery_required')
            await db.commit()
            return True
    return False


async def deliver(job_id, *, redis_worker=False):
    from . import jobrunner, job_execution, distributed_queue
    if await refuse_unsealed(job_id):return 'input_recovery_required'
    async with SessionLocal() as db:
        job = await db.get(Job,job_id)
        if job is None or job.status != 'queued':
            return 'already_owned_or_terminal'
        attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id)
            .order_by(JobAttempt.number.desc()).limit(1))
        if attempt is None or attempt.phase != 'queued' or attempt.claim_token is not None:
            return 'already_owned_or_terminal'
        value = await intent(db,job,attempt)
        if value is None:
            return 'legacy_delivery_unverified'
        spec = json.loads(attempt.input_manifest_json)['execution_spec']
        identifier = attempt.id
    if value['mode'] == 'redis' and not redis_worker:
        # Keep only this owner's selected runnable case head in Redis. SQL
        # retains the rest; a flood cannot fill the transport ahead
        # of a different user's first request.
        if not await redis_head(job_id,value):
            return 'deferred'
        await distributed_queue.enqueue_delivery(job_id,identifier,
            target_host=value['host_id'] if value['schema']==TARGET_SCHEMA else None)
        return 'enqueued'
    if redis_worker and value['mode'] != 'redis':
        raise ExecutionConflict('evidence_invalid','Redis delivery differs from the selected execution mode')
    try:
        launch = jobrunner.start_job(job_id,spec['command'],spec['case_prefix'],runtime=spec['runtime'],
            argv=spec['argv'],timeout_seconds=spec['timeout_seconds'],max_log_bytes=spec['max_log_bytes'],
            evidence_run_dir=spec['evidence_run_dir'])
        if redis_worker:
            await _redis_execution(job_id, launch)
        else:
            await launch
    except job_execution.JobAlreadyClaimed:
        return 'already_owned_or_terminal'
    except DeliveryDeferred:
        return 'deferred'
    return 'started'


async def _redis_execution(job_id, launch):
    import asyncio
    from . import distributed_queue
    from .async_ops import finish_before_cancel
    operation = asyncio.create_task(launch)
    try:
        proc = await asyncio.shield(operation)
        distributed_queue.ACTIVE[job_id] = proc
        await proc.wait()
    except asyncio.CancelledError:
        async def drain():
            # Startup owns launch registration; finish that handoff before
            # stopping it, including cancellation before ACTIVE registration.
            proc = await operation
            distributed_queue.ACTIVE[job_id] = proc
            proc.interrupt()
            await proc.wait()
        await finish_before_cancel(drain())
        raise
    finally:
        distributed_queue.ACTIVE.pop(job_id, None)


def queued():
    latest=select(JobAttempt.job_id.label('job_id'),func.max(JobAttempt.number).label('number')).group_by(JobAttempt.job_id).subquery()
    return (select(Job.id.label('job_id'),Case.id.label('case_id'),Workspace.user_id.label('owner_id'))
        .join(Case,Case.id==Job.case_id).join(Workspace,Workspace.id==Case.workspace_id)
        .join(latest,latest.c.job_id==Job.id)
        .join(JobAttempt,(JobAttempt.job_id==Job.id)&(JobAttempt.number==latest.c.number))
        .where(Job.status=='queued',JobAttempt.phase=='queued',JobAttempt.claim_token.is_(None),
            JobAttempt.cancel_requested_at.is_(None),select(DurableEvent.id).where(
                DurableEvent.attempt_id==JobAttempt.id,DurableEvent.type==EVENT).exists()))


async def redis_head(job_id,value):
    async with SessionLocal() as db:
        owner=await db.scalar(select(Workspace.user_id).join(Case,Case.workspace_id==Workspace.id).where(Case.id==value['case_id']))
        # Every v1 delivery field is an ASCII identifier or integer. Removing
        # JSON whitespace also accepts earlier equivalent pretty-printed v1
        # receipts without backend-specific JSON casts or trusting their data.
        payload=DurableEvent.payload_json
        for whitespace in (' ','\n','\r','\t'):payload=func.replace(payload,whitespace,'')
        matches=select(DurableEvent.id).where(DurableEvent.attempt_id==JobAttempt.id,DurableEvent.type==EVENT,
            payload.contains('"mode":"redis"'),payload.contains('"host_id":"'+value['host_id']+'"')).exists()
        cases=queued().where(Workspace.user_id==owner,matches).add_columns(Job.created_at.label('queued_at'),func.row_number().over(
            partition_by=Case.id,order_by=Job.id).label('rank')).subquery()
        served=(select(Job.case_id.label('case_id'),func.max(JobAttempt.started_at).label('started'))
            .join(JobAttempt,JobAttempt.job_id==Job.id).group_by(Job.case_id).subquery())
        # Fair waiting begins at admission, or at the case's most recent
        # service if it has already used a turn since this request arrived.
        # An older preview must not let a newly admitted case jump ahead of
        # an already enqueued head while every worker is stopped.
        waiting_since=sql_case((served.c.started>cases.c.queued_at,served.c.started),else_=cases.c.queued_at)
        candidates=list(await db.scalars(select(cases.c.job_id).outerjoin(served,served.c.case_id==cases.c.case_id)
            .where(cases.c.rank==1).order_by(waiting_since,cases.c.job_id)))
    if job_id not in candidates:
        return False
    # Capacity is a prefilter here, never a reservation. Atomic native claim
    # repeats it. A full batch in one case must not block another ready case.
    for identifier in candidates:
        async with SessionLocal() as db:
            candidate=await db.get(Job,identifier)
            await lock_case_execution(db,candidate.case_id)
            await db.refresh(candidate)
            if candidate.status!='queued':continue
            attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==identifier)
                .order_by(JobAttempt.number.desc()).limit(1))
            if attempt is None or attempt.phase!='queued' or attempt.claim_token is not None:continue
            case=await db.get(Case,candidate.case_id)
            try:
                await require_capacity(db,case,candidate,attempt,json.loads(attempt.input_manifest_json)['execution_spec'],dispatch_only=True)
            except (ExecutionConflict,KeyError,ValueError,HTTPException):
                continue
            return identifier==job_id
    return False


async def pending(limit=64, *, after=0):
    if type(limit) is not int or not 1<=limit<=256 or type(after) is not int or after<0:
        raise ValueError('Invalid delivery scan bound')
    # Rank within cases first, then within billing owners. A large prefix of
    # queued jobs from one case/user cannot consume the whole recovery batch.
    cases=queued().where(Job.id>after).add_columns(func.row_number().over(
        partition_by=Case.id,order_by=Job.id).label('case_rank')).subquery()
    owners=select(cases,func.row_number().over(partition_by=cases.c.owner_id,
        order_by=(cases.c.case_rank,cases.c.job_id)).label('owner_rank')).subquery()
    async with SessionLocal() as db:
        return list(await db.scalars(select(owners.c.job_id).order_by(
            owners.c.owner_rank,owners.c.case_rank,owners.c.job_id).limit(limit)))


async def recover_once(*, after=0):
    outcomes = {}
    semaphore = asyncio.Semaphore(4)
    async def dispatch(job_id):
        async with semaphore:
            try:
                outcomes[job_id] = await deliver(job_id)
            except ExecutionConflict as error:
                outcomes[job_id] = error.code
            except Exception as error:
                outcomes[job_id] = type(error).__name__
    await asyncio.gather(*(dispatch(job_id) for job_id in await pending(after=after)))
    return outcomes


async def recover_forever(stop):
    cursor = 0
    while not stop.is_set():
        try:
            outcomes = await recover_once(after=cursor)
            cursor = max(outcomes) if len(outcomes) == 64 else 0
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print('[WARN] Durable job delivery deferred: '+type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(),timeout=2)
        except asyncio.TimeoutError:
            pass
