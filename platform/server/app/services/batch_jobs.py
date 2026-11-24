"""单进程有界批量作业队列；Job/清单持久化，执行器仍需单 worker。"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field

from fastapi import HTTPException
from . import jobrunner
from .supervisor import register as supervisor_register, unregister as supervisor_unregister


@dataclass
class BatchState:
    batch_id: str
    case_id: int
    user_id: int
    case_prefix: str
    runtime: str
    job_ids: list[int]
    max_concurrent: int
    next_index: int = 0
    cancelled: bool = False
    processes: set[jobrunner.JobProcess] = field(default_factory=set)
    tasks: list[asyncio.Task] = field(default_factory=list)


_ACTIVE: dict[str, BatchState] = {}
_USER_LOCKS: dict[int, asyncio.Lock] = {}


def active_batch(batch_id: str) -> BatchState | None:
    return _ACTIVE.get(batch_id)


async def _start_when_capacity(state: BatchState, job_id: int) -> jobrunner.JobProcess | None:
    """按用户配额串行检查并启动，避免多个批次同时越过最后一个名额。"""
    from ..db import SessionLocal
    from ..models import Job, JobAttempt, User
    from sqlalchemy import select
    from .job_delivery import DeliveryDeferred, intent
    from .quota import check_job_quota

    lock = _USER_LOCKS.setdefault(state.user_id, asyncio.Lock())
    while not state.cancelled:
        async with lock:
            async with SessionLocal() as check_db:
                job = await check_db.get(Job, job_id)
                user = await check_db.get(User, state.user_id)
                if job is None or user is None or job.status != "queued":
                    return None
                attempt = await check_db.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id)
                    .order_by(JobAttempt.number.desc()).limit(1))
                if attempt is None:
                    raise jobrunner.job_execution.ExecutionConflict(
                        'evidence_invalid', 'Local batch member has no admitted attempt')
                if attempt.phase != 'queued' or attempt.claim_token is not None:
                    return None
                delivery = await intent(check_db, job, attempt)
                group = {'id': state.batch_id, 'jobs': state.job_ids,
                    'max_concurrent': state.max_concurrent}
                if (job.case_id != state.case_id or delivery is None or delivery['mode'] != 'local'
                        or delivery['requested_by'] != state.user_id or delivery['batch'] != group):
                    raise jobrunner.job_execution.ExecutionConflict(
                        'evidence_invalid', 'Local batch differs from its durable delivery binding')
                spec = json.loads(attempt.input_manifest_json)['execution_spec']
                if spec['case_prefix'] != state.case_prefix:
                    raise jobrunner.job_execution.ExecutionConflict(
                        'evidence_invalid', 'Local batch execution directory differs from its admitted case')
                try:
                    await check_job_quota(check_db, user, case_id=state.case_id)
                except HTTPException as error:
                    if error.status_code != 429:
                        raise
                else:
                    try:
                        proc = await jobrunner.start_job(
                            job_id, spec['command'], spec['case_prefix'], runtime=spec['runtime'],
                            argv=spec['argv'], timeout_seconds=spec['timeout_seconds'],
                            max_log_bytes=spec['max_log_bytes'], evidence_run_dir=spec['evidence_run_dir'])
                    except DeliveryDeferred:
                        pass
                    else:
                        state.processes.add(proc)
                        return proc
        await asyncio.sleep(0.1)
    return None


async def _worker(state: BatchState):
    while not state.cancelled:
        index = state.next_index
        state.next_index += 1
        if index >= len(state.job_ids):
            return
        job_id = state.job_ids[index]
        proc = None
        try:
            proc = await _start_when_capacity(state, job_id)
            if proc is None:
                continue
            await proc.wait()
        except asyncio.CancelledError:
            raise
        except jobrunner.job_execution.JobAlreadyClaimed:
            continue
        except Exception as error:
            try:
                await jobrunner._persist_status(job_id, "failed", -1)
            except (jobrunner.JobStateConflict, jobrunner.job_execution.ExecutionConflict):
                pass
            print(f"[WARN] 批量作业 #{job_id} 启动/等待失败: {type(error).__name__}: {error}")
        finally:
            if proc is not None:
                state.processes.discard(proc)


async def _mark_cancelled(state: BatchState):
    await cancel_durable(state.case_id,state.batch_id,state.job_ids)


async def cancel_durable(case_id,batch_id,job_ids,*,actor_user_id=None):
    """Publish every member's cancellation under one case fence/SQL commit.

    Workers may observe all pending cancellations after this transaction; a
    fast first process exit cannot let later members start between commits.
    """
    from ..db import SessionLocal
    from ..models import Case, Job
    from .durable_events import lock_case_execution,append_event
    from .async_ops import finish_before_cancel
    if (not isinstance(batch_id,str) or re.fullmatch('[0-9a-f]{32}',batch_id) is None
            or not job_ids or len(job_ids)>50 or any(type(v) is not int or v<=0 for v in job_ids)
            or len(set(job_ids))!=len(job_ids)):
        raise ValueError('Invalid durable batch cancellation membership')
    async with SessionLocal() as db:
        await lock_case_execution(db,case_id)
        selected=[]
        for job_id in job_ids:
            job=await db.get(Job,job_id,populate_existing=True)
            if job is None or job.case_id!=case_id:
                raise jobrunner.job_execution.ExecutionConflict('evidence_invalid','Batch membership no longer belongs to its case')
            if job.status in {'queued','running'}:
                await jobrunner.job_execution.request_cancel(db,job_id)
                selected.append(job_id)
        if selected:
            await append_event(db,case_id,'job.batch_cancel_requested',{
                'schema':'vibeflow.batch-cancel.v1','batch_id':batch_id,'jobs':selected})
            if actor_user_id is not None:
                from . import audit
                await audit.case_event(db, await db.get(Case, case_id), actor_user_id,
                                       'job.batch.cancel.requested', 'job_batch', batch_id, {'count': len(selected)})
        await finish_before_cancel(db.commit())
        return selected


async def start_batch(state: BatchState):
    _ACTIVE[state.batch_id] = state
    supervisor_register("batch", state.batch_id, state.case_id, lambda: cancel_active(state))
    state.tasks = [asyncio.create_task(_worker(state)) for _ in range(state.max_concurrent)]

    async def cleanup():
        await asyncio.gather(*state.tasks, return_exceptions=True)
        if state.cancelled:
            await _mark_cancelled(state)
        _ACTIVE.pop(state.batch_id, None)
        supervisor_unregister(f"batch:{state.batch_id}")
        if not _ACTIVE or all(item.user_id != state.user_id for item in _ACTIVE.values()):
            _USER_LOCKS.pop(state.user_id, None)

    asyncio.create_task(cleanup())


async def cancel_case(case_id: int):
    """统一进程取消返回前调用，确保本 case 所有排队项已持久化取消。"""
    for state in list(_ACTIVE.values()):
        if state.case_id == case_id:
            state.cancelled = True
            await _mark_cancelled(state)


def cancel_active(state: BatchState):
    state.cancelled = True
    for proc in list(state.processes):
        proc.cancel()
    try:
        asyncio.get_running_loop().create_task(_mark_cancelled(state))
    except RuntimeError:
        pass  # 非事件循环线程由 cleanup 最终回写
