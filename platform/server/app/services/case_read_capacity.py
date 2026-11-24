"""Admission for audited read API sessions whose coordinator may recover S3."""
from contextlib import asynccontextmanager
import asyncio
from fastapi import HTTPException
from .. import storage
from ..config import get_settings
from .async_ops import finish_before_cancel
from .case_write import coordinate
from . import owner_storage_budget
from .owner_storage_budget import hold_case
from .request_deadline import scope as deadline_scope


@asynccontextmanager
async def file_read_deadline(code, label, *, seconds=None):
    """One request budget for lock admission and work done by read threads."""
    try:
        async with deadline_scope(get_settings().storage_measurement_timeout_seconds if seconds is None else seconds):
            yield
    except TimeoutError as error:
        raise HTTPException(504, {'code':code+'_timeout','message':label+'超时，请稍后重试'}) from error


async def read_ready(db,case):
    from sqlalchemy import select
    from ..models import NativeCliSessionRequest,ConversationExecution
    from .storage_operations import admit_locked
    from .case_write import CaseWriteConflict
    storage_owned = await admit_locked(db,case.id)
    starting=await db.scalar(select(NativeCliSessionRequest.id).join(ConversationExecution,
        ConversationExecution.resource_id==NativeCliSessionRequest.id).where(
            NativeCliSessionRequest.case_id==case.id,
            NativeCliSessionRequest.phase.in_(('admitted','starting')),
            ConversationExecution.backend=='codex',ConversationExecution.phase=='active').limit(1))
    if starting is not None:
        raise CaseWriteConflict('file_sync_busy','CLI 正在准备工作副本，请稍后读取文件')
    return storage_owned


@asynccontextmanager
async def admitted(db,case,*,polling=False,chunk=None):
    """Retry only a busy admission, never a recovery or a completed read."""
    loop=asyncio.get_running_loop()
    deadline=loop.time()+get_settings().storage_measurement_timeout_seconds
    while True:
        if polling:await read_ready(db,case)
        context=(hold_case(db,case) if chunk is None
                 else owner_storage_budget.hold_case(db,case,chunk=chunk))
        try:
            owner=await context.__aenter__()
            break
        except HTTPException as error:
            if (error.status_code!=409 or not isinstance(error.detail,dict)
                    or error.detail.get('code')!='storage_quota_busy'):
                raise
            remaining=deadline-loop.time()
            if remaining<=0:raise
            await asyncio.sleep(min(.05,remaining))
            if loop.time()>=deadline:raise
    try:yield owner
    except BaseException as error:
        if not await context.__aexit__(type(error),error,error.__traceback__):raise
    else:await context.__aexit__(None,None,None)


@asynccontextmanager
async def reading(db,case,*,defer_if_case_busy=False,skip_if_consumed=None,defer_journal=False):
    """Caller authorizes before entry and builds its complete response inside.

    These API sessions have no business writes to commit. End their transaction
    before closing the durable owner pool so its settlement survives the read
    dependency's normal rollback, including 404/409/416 responses.
    """
    if not storage._use_s3():
        async with coordinate(db,case,content_write=False,defer_journal=defer_journal) as snapshot:yield snapshot
        return
    # Recheck before each credit attempt so queued polling reads cannot starve
    # an admitted CLI startup. Its own snapshot preparation uses admitted()
    # directly, then performs the same authoritative case-locked file checks.
    async with admitted(db,case,polling=True):
        try:
            # A competing dispatcher may have committed while this one waited
            # for the exclusive S3 grant. A duplicate needs no recovery fence.
            if skip_if_consumed is not None and await skip_if_consumed():
                yield None
                return
            if defer_if_case_busy and db.get_bind().dialect.name=='postgresql':
                # Outbox events already have a case counter. Take the same row
                # lock as coordinate(), or return the storage grant immediately.
                # Waiting here could deny the current case writer its seal quota.
                from sqlalchemy import select
                from ..models import CaseEventCounter
                from .case_write import CaseWriteConflict
                locked=await db.scalar(select(CaseEventCounter.case_id)
                    .where(CaseEventCounter.case_id==case.id).with_for_update(skip_locked=True))
                if locked is None:
                    raise CaseWriteConflict('case_busy','事件投递等待算例写入完成')
            async with coordinate(db,case,content_write=False,defer_journal=defer_journal) as snapshot:yield snapshot
        finally:
            await finish_before_cancel(db.rollback())


@asynccontextmanager
async def reading_before_action(db,case):
    """Finish a read/recovery transaction before a separately admitted action.

    Entry must precede business writes. Unlike ``reading``, commit retains the
    loaded objects in our expire_on_commit=False sessions. Exit releases both
    the case lock and owner credit before launching or cancelling a job.
    """
    if db.new or db.dirty or db.deleted:
        raise ValueError('Read preparation must precede business writes')
    async with admitted(db,case):
        try:
            async with coordinate(db,case,content_write=False) as snapshot:
                yield snapshot
            await finish_before_cancel(db.commit())
        except BaseException:
            await finish_before_cancel(db.rollback())
            raise
