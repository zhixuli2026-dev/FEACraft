"""Owner-wide transactional admission with bounded writer/settlement waits."""
import asyncio,math,sqlite3,time
from contextlib import asynccontextmanager
from sqlalchemy import select,update,func
from sqlalchemy.exc import OperationalError
from fastapi import HTTPException
from ..models import User,UploadStorageReservation,PublicationStorageReservation,OutputArchiveReservation,JobLogReservation,StorageWriteReservation,OwnerStorageReservation,CliInitializationReservation
from ..storage_models import StorageQuotaEpoch
from .async_ops import finish_before_cancel

# A separate PostgreSQL advisory namespace; transaction lifetime is the fence.
NAMESPACE=1447449425
SQLITE_TRY_SECONDS=.1  # Absorb short global-writer bookkeeping, never 30s.


def busy():
    return HTTPException(409,{'code':'storage_quota_busy','message':'同一账户的存储写入正在结算，请稍后重试'})


class InventoryOutdated(HTTPException):
    def __init__(self):
        response=busy()
        super().__init__(response.status_code,response.detail)


def bounds(*,settlement=False):
    from ..config import get_settings
    from .request_deadline import current
    # Existing work still needs settlement after its request expires. This
    # exception is explicit at the six release-only call sites, not admission.
    parent=None if settlement else current()
    if parent is not None:parent.check()
    end=time.monotonic()+get_settings().storage_measurement_timeout_seconds
    return min(end,parent.deadline) if parent is not None else end,parent


def check(end,parent):
    if parent is not None:parent.check()
    if time.monotonic()>=end:raise busy()


async def _sqlite_lock(db,user_id,wait,end,parent):
    connection=await db.connection()
    raw=await connection.get_raw_connection()
    previous=int((await connection.exec_driver_sql('PRAGMA busy_timeout')).scalar_one())
    check(end,parent)
    maximum=end-time.monotonic()
    if not wait:maximum=min(maximum,SQLITE_TRY_SECONDS)
    milliseconds=min(previous,max(0,math.floor(maximum*1000)))
    try:
        await connection.exec_driver_sql('PRAGMA busy_timeout='+str(milliseconds))
        if not raw.driver_connection.in_transaction:
            # Acquire the writer before a new transaction can take a read
            # snapshot. Never replace an existing caller-owned transaction.
            await connection.exec_driver_sql('BEGIN IMMEDIATE')
        check(end,parent)
        if user_id is not None:
            result=await db.execute(update(User).where(User.id==user_id).values(id=User.id))
            if result.rowcount!=1:raise ValueError('Quota owner is missing')
        check(end,parent)
    except OperationalError as error:
        code=getattr(error.orig,'sqlite_errorcode',None)
        if type(code) is int and code&255 in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED}:
            if parent is not None:parent.check()
            # BUSY_SNAPSHOT needs a fresh outer transaction. No automatic
            # rollback/retry of the caller's work can be safe here.
            raise busy() from error
        raise
    finally:
        # This coroutine is drained even when its caller is cancelled. The
        # pooled connection must not inherit another request's wait policy.
        await connection.exec_driver_sql('PRAGMA busy_timeout='+str(previous))


async def sqlite_write_intent(db):
    """Reserve the SQLite writer before the first transactional read of a write request."""
    if db.get_bind().dialect.name=='sqlite':
        end,parent=bounds()
        await finish_before_cancel(_sqlite_lock(db,None,True,end,parent))


async def quota_revision(db,user_id):
    revision=await db.scalar(select(StorageQuotaEpoch.revision).where(StorageQuotaEpoch.user_id==user_id))
    return revision if revision is not None else 0


async def _advance_revision(db,user_id,expected):
    update_epoch=update(StorageQuotaEpoch).where(StorageQuotaEpoch.user_id==user_id)
    if expected is not None:update_epoch=update_epoch.where(StorageQuotaEpoch.revision==expected)
    result=await db.execute(update_epoch.values(revision=StorageQuotaEpoch.revision+1))
    if result.rowcount==1:return
    if expected is not None and (expected!=0 or await quota_revision(db,user_id)!=0):raise InventoryOutdated()
    db.add(StorageQuotaEpoch(user_id=user_id,revision=1))
    await db.flush()


async def lock_owner(db,user_id,*,wait=False,settlement=False,inventory_revision=None):
    if type(user_id) is not int or not 0<user_id<2**31:raise ValueError('Invalid quota owner')
    if settlement and not wait:raise ValueError('Settlement requires its bounded waiting scope')
    end,parent=bounds(settlement=settlement)
    if db.get_bind().dialect.name=='postgresql':
        # Waiting is reserved for admission/settlement holding no case lock.
        # Poll a try-lock under one absolute budget; no new deadline per retry.
        while True:
            check(end,parent)
            acquired=await db.scalar(select(func.pg_try_advisory_xact_lock(NAMESPACE,user_id)))
            check(end,parent)
            if acquired:break
            if not wait:raise busy()
            await asyncio.sleep(min(.05,max(0,end-time.monotonic())))
    elif db.get_bind().dialect.name=='sqlite':
        # SQLite serializes writers. Do not use an in-process lock: API peers
        # and workers must participate in the same transaction exclusion.
        await finish_before_cancel(_sqlite_lock(db,user_id,wait,end,parent))
    else:raise RuntimeError('Unsupported storage quota database')
    # A live write can replace a durable credit with physical bytes while a
    # reader lists objects. Even equal before/after credit totals do not prove
    # the inventory is current. Advance within the same serialized transaction.
    await _advance_revision(db,user_id,inventory_revision)
    check(end,parent)


@asynccontextmanager
async def sqlite_settlement_session():
    """Wait before starting an already-owned writer's settlement transaction."""
    from ..db import SessionLocal

    deadline, _ = bounds(settlement=True)
    while time.monotonic() < deadline:
        async with SessionLocal() as db:
            async with asyncio.timeout_at(deadline):
                await db.connection()
            try:
                await finish_before_cancel(_sqlite_lock(db, None, False, deadline, None))
            except HTTPException as error:
                if (error.status_code != 409 or not isinstance(error.detail, dict)
                        or error.detail.get('code') != 'storage_quota_busy'):
                    raise
            else:
                yield db
                return
        # No transaction work can be replayed, and waiters release their connection.
        await asyncio.sleep(min(.05, max(0, deadline - time.monotonic())))
    raise TimeoutError('SQLite writer did not become available for settlement')


async def reserved_bytes(db,user_id):
    # PostgreSQL SUM(bigint) returns numeric/Decimal; SQLite returns int.
    # All summands are constrained integer columns, so this is lossless.
    total=0
    for model in (UploadStorageReservation,PublicationStorageReservation,OutputArchiveReservation,JobLogReservation,StorageWriteReservation,CliInitializationReservation):
        total+=int((await db.scalar(select(func.sum(model.remaining_bytes)).where(
            model.user_id==user_id,model.state=='active'))) or 0)
    from .owner_storage_budget import exclusion
    query=select(func.sum(OwnerStorageReservation.remaining_bytes)).where(
        OwnerStorageReservation.user_id==user_id,OwnerStorageReservation.state=='active')
    excluded=exclusion(db,user_id)
    if excluded is not None:query=query.where(OwnerStorageReservation.id!=excluded)
    total+=int((await db.scalar(query)) or 0)
    return total
