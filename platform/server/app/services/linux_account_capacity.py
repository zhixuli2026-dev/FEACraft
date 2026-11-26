"""Admission shared by native Linux launches across API and worker processes."""
import json
import sys

from fastapi import HTTPException
from sqlalchemy import or_, select, update

from ..config import get_settings
from ..models import (AcpThread, AcpTurn, Job, JobAttempt, NativeCliSession, NativeCliSessionRequest,
                      NativeRequest, NativeSession, NativeVizSession, User)
from .native_request_records import DOCUMENT_KINDS


CODE = 'linux_account_execution_limit'
MESSAGE = '该账户的 Linux 执行已达到并发上限，请等待任务结束或关闭闲置会话'
REQUEST_KINDS = ('agent.shell', *DOCUMENT_KINDS)
JOURNAL_ERRORS = (ValueError, TypeError, KeyError, AttributeError)


def limit(*, docker=False):
    return get_settings().linux_account_max_active_executions if docker or sys.platform == 'linux' else 0


async def lock_accounts(db, *user_ids):
    """All claim paths take account locks after case control, in ID order."""
    for user_id in sorted(set(user_ids)):
        await db.execute(update(User).where(User.id == user_id).values(id=User.id))


def possibly_linux(raw, *, worker=False):
    try:
        value = json.loads(raw)
        birth = value['process'] if worker else value
        return birth['platform'] != 'windows'
    except JOURNAL_ERRORS:
        return True  # Without a recorded platform, retain uncertain capacity.


def _terminal_occupied(row):
    if not possibly_linux(row.owner_identity_json):
        return False
    if row.phase in {'starting', 'active'}:
        return True
    from .terminal_records import document
    journal, stop = document(row)
    return journal['root'] is not None and not (stop and stop['verified'])


def _viz_occupied(row):
    if not possibly_linux(row.owner_identity_json):
        return False
    if row.phase in {'starting', 'active'}:
        return True
    from .viz_records import document
    _, process, _, stop = document(row)
    return process is not None and not (stop and stop['verified'])


def _request_occupied(row):
    if not possibly_linux(row.owner_identity_json):
        return False
    if row.phase == 'active':
        return True
    from .native_request_records import document
    children = document(row)['children'].values()
    return any(child['phase'] in {'preparing', 'running'}
               or (child['identity'] is not None and not (child['stop'] and child['stop']['verified']))
               for child in children)


def _codex_occupied(row):
    if row.phase in {'starting', 'active'}:
        return True
    from .codex_records import document
    _, stop = document(row)
    return not (stop and stop['verified'])


def _codex_request_occupied(row):
    from .codex_session_requests import selection
    requested, _ = selection(row)
    return requested['runtime'] == 'docker' and not row.not_started


def _acp_occupied(row):
    if row.phase in {'admitted', 'running', 'recovery_required'}:
        return True
    if row.phase in {'failed', 'cancelled'} and row.process_json is None and row.stop_json is None:
        return False  # This turn finished before registering a launch.
    from .acp_execution import verified_cleanup
    return not verified_cleanup(row)


async def _interactive_count(db, user_id, capacity, transfer_request_id):
    startup = select(NativeCliSessionRequest).where(NativeCliSessionRequest.session_id.is_(None))
    if transfer_request_id is not None:
        startup = startup.where(NativeCliSessionRequest.id != transfer_request_id)
    sources = (
        (select(NativeSession), NativeSession, _terminal_occupied),
        (select(NativeVizSession), NativeVizSession, _viz_occupied),
        (select(NativeRequest).where(NativeRequest.kind.in_(REQUEST_KINDS)), NativeRequest,
         _request_occupied),
        (select(NativeCliSession).where(NativeCliSession.runtime == 'docker'), NativeCliSession, _codex_occupied),
        (startup, NativeCliSessionRequest, _codex_request_occupied),
        (select(AcpTurn).join(AcpThread, AcpThread.id == AcpTurn.thread_id).where(AcpThread.runtime == 'linux'),
         AcpTurn, _acp_occupied),
    )
    count = 0
    for query, model, occupied in sources:
        rows = await db.stream_scalars(query.where(model.created_by == user_id).execution_options(yield_per=32))
        try:
            async for row in rows:
                try:
                    if not occupied(row):
                        continue
                except JOURNAL_ERRORS:
                    pass  # Damaged evidence cannot prove cleanup.
                count += 1
                if count >= capacity:
                    return count
        finally:
            await rows.close()
    return count


async def _jobs_full(db, user_id, remaining):
    from .job_delivery import jobs_requested_by, occupying
    query = jobs_requested_by(user_id, latest_only=True).where(
        or_(Job.status == 'running', JobAttempt.claim_token.is_not(None)))
    rows = await db.stream(query.execution_options(yield_per=32))
    try:
        async for case, job, attempt in rows:
            if attempt is not None and not possibly_linux(attempt.worker_identity_json, worker=True):
                continue
            if occupying(case, job, attempt):
                remaining -= 1
                if remaining == 0:
                    return True
    finally:
        await rows.close()
    return False


async def full_locked(db, user_id, capacity, *, transfer_request_id=None):
    count = await _interactive_count(db, user_id, capacity, transfer_request_id)
    return count >= capacity or await _jobs_full(db, user_id, capacity - count)


async def admit(db, user_id, *, docker=False, transfer_request_id=None):
    capacity = limit(docker=docker)
    if not capacity:
        return
    await lock_accounts(db, user_id)
    if await full_locked(db, user_id, capacity, transfer_request_id=transfer_request_id):
        raise HTTPException(429, {'code': CODE, 'message': MESSAGE, 'limit': capacity})
