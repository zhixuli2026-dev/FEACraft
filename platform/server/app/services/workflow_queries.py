"""Authorized, bounded workflow reads; diagnosis never grants execution rights."""
import base64
from datetime import datetime, timezone
import json
import uuid

from sqlalchemy import and_, func, or_, select

from ..models import DurableEvent, Job, JobAttempt, WorkflowRun
from .workflow_state import ExecutionConflict


def object_record(encoded, label):
    try:
        value = json.loads(encoded)
        if not isinstance(value, dict): raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ExecutionConflict('evidence_invalid', label + '记录无法核验，请保留原流程人工核查') from None


def plan_record(run):
    plan = object_record(run.plan_json, '工作流计划')
    steps = plan.get('steps')
    if (not isinstance(steps, list) or not steps or any(not isinstance(row, dict)
            or not isinstance(row.get('key'), str) or not row['key'] for row in steps)
            or len({row['key'] for row in steps}) != len(steps)
            or not isinstance(plan.get('parameters', {}), dict)):
        raise ExecutionConflict('evidence_invalid', '工作流步骤定义无法核验，请保留原流程人工核查')
    return plan


async def active_binding(db, run, step):
    if step.active_attempt_id is None: return
    attempt = await db.get(JobAttempt, step.active_attempt_id, populate_existing=True)
    job = await db.get(Job, attempt.job_id, populate_existing=True) if attempt else None
    if attempt is None or attempt.step_id != step.id or job is None or job.case_id != run.case_id:
        raise ExecutionConflict('evidence_invalid', '步骤的当前尝试与作业归属无法核验，请保留原记录')


def encode_cursor(row, filters):
    stamp = row.created_at
    stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    payload = dict(filters, v=1, created_at=stamp.isoformat(), id=row.id)
    return base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).decode().rstrip('=')


def decode_cursor(cursor, filters):
    try:
        value = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
        if not isinstance(value, dict) or set(value) != set(filters) | {'v', 'created_at', 'id'}: raise ValueError()
        if type(value['v']) is not int or value['v'] != 1 or type(value['case_id']) is not int: raise ValueError()
        if any(value[key] != expected for key, expected in filters.items()): raise ValueError()
        if str(uuid.UUID(value['id'])) != value['id']: raise ValueError()
        stamp = datetime.fromisoformat(value['created_at'])
        if stamp.utcoffset() is None: raise ValueError()
        return stamp.astimezone(timezone.utc), value['id']
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ExecutionConflict('invalid_cursor', '工作流分页游标无效或不属于当前筛选，请从第一页重新读取') from None


async def runs_page(db, case_id, user, *, limit=50, cursor=None, status=None, target=None):
    from ..routers.workspaces import get_owned_case
    from .workflow_execution import run_summary
    await get_owned_case(case_id, user, db, write=False)
    filters = {'case_id': case_id, 'status': status, 'target': target}
    query = select(WorkflowRun).where(WorkflowRun.case_id == case_id)
    if status is not None: query = query.where(WorkflowRun.status == status)
    if target is not None: query = query.where(WorkflowRun.target == target)
    if cursor:
        stamp, identifier = decode_cursor(cursor, filters)
        query = query.where(or_(WorkflowRun.created_at < stamp,
            and_(WorkflowRun.created_at == stamp, WorkflowRun.id < identifier)))
    rows = list(await db.scalars(query.order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc()).limit(limit + 1)))
    return {'items': [run_summary(row) for row in rows[:limit]],
        'next_cursor': encode_cursor(rows[limit-1], filters) if len(rows) > limit else None}


async def events(db, run_id, user, *, after=0, limit=100):
    from .workflow_execution import owned
    from .event_replay import envelope, visible
    from .durable_events import validate_payload, _validate_links
    run = await owned(db, run_id, user, write=False)
    watermark = await db.scalar(select(func.max(DurableEvent.seq)).where(DurableEvent.case_id == run.case_id)) or 0
    if after > watermark:
        raise ExecutionConflict('event_cursor_ahead', '事件游标超出当前算例，请核对原流程并从零重新读取')
    rows = list(await db.scalars(select(DurableEvent).where(DurableEvent.case_id == run.case_id,
        DurableEvent.run_id == run.id, DurableEvent.seq > after, DurableEvent.seq <= watermark)
        .order_by(DurableEvent.seq).limit(limit + 1)))
    result = []; cursor = after; blocked = False
    for row in rows[:limit]:
        payload = object_record(row.payload_json, '工作流事件')
        try:
            validate_payload(payload)
            await _validate_links(db, row.case_id, row.run_id, row.step_id, row.attempt_id)
        except ValueError: raise ExecutionConflict('evidence_invalid', '工作流事件载荷无法安全核验，请保留原记录') from None
        if not await visible(db, row): blocked = True; break
        result.append(envelope(row)); cursor = row.seq
    more = not blocked and len(rows) > limit
    if not blocked and not more: cursor = watermark
    return {'case_id': run.case_id, 'run_id': run.id, 'events': result, 'next_after': cursor,
        'watermark': watermark, 'has_more': more, 'blocked': blocked}
