"""Snapshot-bound manual retention; an acknowledgement never grants write authority."""
import json
import uuid

from fastapi import HTTPException

from ..db import SessionLocal
from ..models import Case, DurableEvent, Job, User, Workspace
from . import job_recovery_notice as notices
from .async_ops import finish_before_cancel
from .durable_events import append_event, lock_case_execution, now
from .workflow_state import ExecutionConflict

SCHEMA = 'vibeflow.job-recovery-disposition.v1'
EVENT = 'job.recovery_retained_for_review'
ACTION = 'retain_for_review'


async def records(db, job_id, user_id, *, write=False, locked=False):
    from ..routers.workspaces import get_owned_case
    user = await db.get(User, user_id, populate_existing=True)
    if user is None or user.status != 'active':
        raise HTTPException(403, '账户已不可用')
    job = await db.get(Job, job_id, populate_existing=True)
    if job is None:
        raise HTTPException(404, '作业不存在')
    await get_owned_case(job.case_id, user, db, write=write)
    if locked:
        await lock_case_execution(db, job.case_id)
        # Recheck access after the serialization point as well as before it.
        await db.refresh(user)
        if user.status != 'active':
            raise HTTPException(403, '账户已不可用')
        await db.refresh(job)
        case = await db.get(Case, job.case_id, populate_existing=True)
        if case is not None:
            await db.get(Workspace, case.workspace_id, populate_existing=True)
        await get_owned_case(job.case_id, user, db, write=write)
    return await notices.rows(db, job_id)


def checkpoint_unverified(value):
    case,job,attempt,_=value
    if attempt is None or attempt.write_checkpoint_json=='{}':return True
    from .job_write_checkpoint import validate
    try:validate(case,job,attempt)
    except (ValueError,TypeError,KeyError,AttributeError,ExecutionConflict):return True
    return False


def issue_codes(value):
    _, job, attempt, head = value
    if attempt is None:
        return ['legacy_attempt_missing']
    issues = []
    if head is None or head.active_attempt_id != attempt.id:
        issues.append('attempt_lineage_unverified')
    if attempt.write_checkpoint_json != '{}':
        if not checkpoint_unverified(value):return issues + ['use_existing_checkpoint_recovery']
        issues.append('write_checkpoint_unverified')
    issues.append('content_drain_unverified')
    try:
        output = json.loads(attempt.output_manifest_json)
        children = json.loads(attempt.child_processes_json)['children']
        if not isinstance(output, dict) or not isinstance(children, dict):
            raise ValueError('Invalid historical journal')
        if not isinstance(output.get('process_stop'), dict) or output['process_stop'].get('verified') is not True:
            issues.append('root_stop_unverified')
        if any(not isinstance(child, dict) or not isinstance(child.get('stop'), dict)
               or child['stop'].get('verified') is not True for child in children.values()):
            issues.append('child_stop_unverified')
    except (ValueError, TypeError, KeyError):
        issues.append('process_history_unreadable')
    if job.status in {'failed', 'interrupted', 'cancelled'}:
        issues.append('terminal_outcome_does_not_prove_cleanup')
    return issues


def event_id(job_id, fingerprint):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'vibeflow.manual-retention:' + str(job_id) + ':' + fingerprint))


def receipt(event, value, fingerprint):
    _, job, attempt, _ = value
    payload = json.loads(event.payload_json)
    if (event.type != EVENT or event.case_id != job.case_id
            or event.attempt_id != (attempt.id if attempt else None)
            or payload.get('schema') != SCHEMA or payload.get('job_id') != job.id
            or payload.get('attempt_id') != (attempt.id if attempt else None)
            or payload.get('snapshot_sha256') != fingerprint or payload.get('action') != ACTION
            or payload.get('stop_evidence_created') is not False
            or payload.get('execution_authorized') is not False):
        raise ExecutionConflict('recovery_required', '原人工审查记录无法核验，请保留现场核查')
    return {'state': 'retained_for_manual_review', 'event_id': event.id, 'action': ACTION,
            'snapshot_sha256': fingerprint, 'recorded_at': payload['recorded_at'],
            'stop_evidence_created': False, 'content_drained': False,
            'execution_authorized': False, 'automatic_replay': False}


async def describe(db, value):
    _, job, attempt, _ = value
    fingerprint = notices.fingerprint(value)
    event = await db.get(DurableEvent, event_id(job.id, fingerprint))
    available = checkpoint_unverified(value)
    return {'schema': SCHEMA, 'job_id': job.id, 'attempt_id': attempt.id if attempt else None,
            'snapshot_sha256': fingerprint, 'original_status': job.status,
            'original_phase': attempt.phase if attempt else None, 'issues': issue_codes(value),
            'available_actions': [ACTION] if available else [], 'automatic_replay': False,
            'execution_authorized': False,
            'instructions': [
                '保留原作业、私有产物、日志和进程记录，登记人工审查。',
                '由原执行主机管理员核对对应进程范围及完整停止证据；PID不存在或作业终态不足以证明全部写入已经结束。',
                '具有原证明的记录使用对应准备、输出、缓存或发布恢复入口；缺少证明时继续保留现场。',
                '人工审查登记不会放行后续写入、发布、删除或重新执行；重新计算须走独立的显式重跑流程。'],
            'disposition': receipt(event, value, fingerprint) if event else None}


async def options(job_id, user_id):
    async with SessionLocal() as db:
        return await describe(db, await records(db, job_id, user_id))


async def retain(job_id, user_id, attempt_id, fingerprint):
    async with SessionLocal() as db:
        value = await records(db, job_id, user_id, write=True, locked=True)
        _, job, attempt, _ = value
        if (attempt.id if attempt else None) != attempt_id or notices.fingerprint(value) != fingerprint:
            raise ExecutionConflict('state_conflict', '原任务状态已变化，请重新读取人工审查方案')
        if not checkpoint_unverified(value):
            raise ExecutionConflict('state_conflict', '该记录已有检查点，请使用对应恢复入口核验')
        identifier = event_id(job.id, fingerprint)
        event = await db.get(DurableEvent, identifier)
        if event is None:
            payload = {'schema': SCHEMA, 'job_id': job.id, 'attempt_id': attempt_id,
                       'snapshot_sha256': fingerprint, 'action': ACTION,
                       'issues': issue_codes(value), 'original_status': job.status,
                       'original_phase': attempt.phase if attempt else None,
                       'actor_user_id': user_id, 'recorded_at': now().isoformat(),
                       'stop_evidence_created': False, 'execution_authorized': False}
            event = await append_event(db, job.case_id, EVENT, payload,
                                       attempt_id=attempt_id, event_id=identifier)
            await finish_before_cancel(db.commit())
        return receipt(event, value, fingerprint)
