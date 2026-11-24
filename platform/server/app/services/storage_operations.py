"""SQL fencing for network work; expiry never grants another writer authority."""
import asyncio
from contextlib import asynccontextmanager,contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
from types import SimpleNamespace
import uuid

from sqlalchemy import select,update
from ..db import SessionLocal
from ..models import Case,CaseStorageOperation,CaseValidationFence
from . import process_identity_native as identity
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict
from .durable_events import lock_case_execution,append_event,json_text,now

_current=ContextVar('case_storage_operation',default=None)
_inside_control=ContextVar('case_storage_control',default=False)


def inside_control():return _inside_control.get()


def current():
    return _current.get()


@contextmanager
def owned_context(lease):
    """Use an already committed reservation with its own settlement protocol."""
    if _current.get() is not None:raise ValueError('Storage operation contexts cannot be nested')
    token=_current.set(lease)
    try:yield
    finally:_current.reset(token)


def busy():
    return CaseWriteConflict('file_sync_busy','算例存储操作正在执行或停止尚未核验，请稍后重试')


async def active(db,case_id):
    return await db.scalar(select(CaseStorageOperation).where(CaseStorageOperation.case_id==case_id,
        CaseStorageOperation.state=='active').execution_options(populate_existing=True))


async def admit_locked(db, case_id):
    """True only for this execution context's exact durable reservation."""
    row = await db.scalar(
        select(CaseStorageOperation)
        .where(CaseStorageOperation.case_id == case_id,
               CaseStorageOperation.state.in_(('active', 'drained', 'exited')))
        .order_by((CaseStorageOperation.state == 'active').desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return False
    if row.state != 'active':
        raise CaseWriteConflict('file_recovery_required', '存储操作尚未核对，请执行文件事务恢复后重试')
    lease = _current.get()
    if lease is None or (row.id, row.case_id, row.owner_json) != (lease.id, lease.case.id, lease.owner_json):
        raise busy()
    return True


@dataclass
class Lease:
    id: str
    case: object
    owner_json: str
    checked: object
    purpose: str
    resolved: bool=False
    transfers_drained: bool=False
    document_key: tuple|None=None
    started_sequence: int|None=None
    verified_s3_conditions: tuple|None=None

    async def require_locked(self,db):
        row=await active(db,self.case.id)
        if row is None or (row.id,row.owner_json,row.source_prefix)!=(self.id,self.owner_json,self.case.s3_prefix):
            raise busy()
        case=await db.get(Case,self.case.id,populate_existing=True)
        if case is None or case.s3_prefix!=self.case.s3_prefix:
            raise CaseWriteConflict('input_stale','存储操作的算例来源已变化')
        return row

    @asynccontextmanager
    async def control(self,*,authorize=True,on_commit=None):
        marker=_inside_control.set(True)
        try:
            async with SessionLocal() as db:
                await lock_case_execution(db,self.case.id)
                if authorize:
                    case=await self.checked(db)
                    if (case.id,case.s3_prefix)!=(self.case.id,self.case.s3_prefix):
                        raise CaseWriteConflict('input_stale','存储操作的算例来源已变化')
                await self.require_locked(db)
                yield db
                async def commit():
                    await db.commit()
                    if on_commit is not None:
                        on_commit()

                await finish_before_cancel(commit())
        finally:_inside_control.reset(marker)

    async def checkpoint(self):
        async with self.control():pass

    async def bind(self,document):
        key=(document['plan']['id'],document['plan_sha256'],document['event_id'])
        async with self.control() as db:
            row=await self.require_locked(db)
            if row.journal_id is not None and (row.journal_id,row.plan_sha256,row.event_id)!=key:
                raise ValueError('Storage operation already bound another journal')
            row.journal_id,row.plan_sha256,row.event_id=key
        self.document_key=key

    async def has_drain_proof(self,document):
        key=(document['plan']['id'],document['plan_sha256'],document['event_id'])
        writer=document['plan'].get('writer')
        if writer is None:return False
        if self.transfers_drained and self.document_key==key and json.loads(self.owner_json)==writer['process']:
            return True
        async with self.control(authorize=False) as db:
            rows=await db.scalars(select(CaseStorageOperation).where(CaseStorageOperation.case_id==self.case.id,
                CaseStorageOperation.source_prefix==self.case.s3_prefix,CaseStorageOperation.purpose=='staging',
                CaseStorageOperation.journal_id==key[0],CaseStorageOperation.plan_sha256==key[1],
                CaseStorageOperation.event_id==key[2],CaseStorageOperation.state.in_(('drained','exited','settled'))))
            return any(json.loads(row.owner_json)==writer['process'] for row in rows)


async def _finish(lease):
    async with SessionLocal() as db:
        await lock_case_execution(db,lease.case.id)
        row=await db.get(CaseStorageOperation,lease.id,populate_existing=True)
        if row is None:return  # Acquisition can roll back before its reply.
        if row.state!='active' or row.owner_json!=lease.owner_json:raise busy()
        row.state='settled' if lease.resolved else 'drained'
        row.ended_at=now()
        if lease.resolved:
            await db.execute(update(CaseStorageOperation).where(CaseStorageOperation.case_id==lease.case.id,
                CaseStorageOperation.source_prefix==lease.case.s3_prefix,
                CaseStorageOperation.state.in_(('drained','exited'))).values(state='settled'))
        await append_event(db,lease.case.id,'storage.operation.ended',{'operation_id':lease.id,'state':row.state})
        await finish_before_cancel(db.commit())


def owner_exited(owner_json):
    return identity.probe(identity.validate(json.loads(owner_json)))['state'] == 'exited'


async def _acquire(checked,purpose,binding=None,*,validate_locked=None):
    if binding is not None:
        import re
        if (not isinstance(binding,tuple) or len(binding)!=3
                or any(not isinstance(value,str) for value in binding)
                or str(uuid.UUID(binding[0]))!=binding[0] or str(uuid.UUID(binding[2]))!=binding[2]
                or not re.fullmatch('[0-9a-f]{64}',binding[1])):
            raise ValueError('Invalid initial storage journal binding')
    owner_json=json_text(await asyncio.to_thread(identity.observe,os.getpid()))
    async with SessionLocal() as db:
        case=await checked(db)
        reference=SimpleNamespace(id=case.id,s3_prefix=case.s3_prefix)
        previous=await active(db,case.id)
        candidate=(previous.id,previous.owner_json) if previous is not None else None
        await finish_before_cancel(db.commit())
    # No SQL transaction spans an OS observation either. Foreign/unknown/alive
    # owners are never displaced, even if their heartbeat timestamp is old.
    if candidate is not None:
        try:exited=await asyncio.to_thread(owner_exited,candidate[1])
        except (ValueError,TypeError,KeyError,AttributeError) as error:
            raise CaseWriteConflict('file_recovery_required','存储操作的归属证明损坏，请保留现场核对') from error
        if not exited:raise busy()
    lease=Lease(str(uuid.uuid4()),reference,owner_json,checked,purpose,document_key=binding)
    try:
        async with SessionLocal() as db:
            case=await checked(db)
            await lock_case_execution(db,reference.id)
            if (case.id,case.s3_prefix)!=(reference.id,reference.s3_prefix):
                raise CaseWriteConflict('input_stale','存储操作的算例来源已变化')
            if validate_locked is not None:
                await validate_locked(db,case)
            previous=await active(db,case.id)
            actual=(previous.id,previous.owner_json) if previous is not None else None
            if actual!=candidate:raise busy()
            if previous is not None:
                previous.state,previous.ended_at='exited',now()
                await append_event(db,case.id,'storage.operation.exited',{'operation_id':previous.id})
                await db.flush()
            validation=await db.get(CaseValidationFence,case.id,populate_existing=True)
            if validation is not None and validation.status=='held':
                from .case_validation import ValidationConflict
                raise ValidationConflict()
            db.add(CaseStorageOperation(id=lease.id,case_id=case.id,source_prefix=case.s3_prefix,
                purpose=purpose,state='active',owner_json=owner_json,
                journal_id=binding[0] if binding else None,plan_sha256=binding[1] if binding else None,
                event_id=binding[2] if binding else None))
            event=await append_event(db,case.id,'storage.operation.started',{'operation_id':lease.id,'purpose':purpose})
            lease.started_sequence=event.seq
            await finish_before_cancel(db.commit())
    except BaseException:
        await finish_before_cancel(_finish(lease))
        raise
    return lease


@asynccontextmanager
async def hold(checked,*,purpose,binding=None,validate_locked=None):
    lease=await _acquire(checked,purpose,binding,validate_locked=validate_locked)
    token=_current.set(lease)
    try:
        from .remote_write_budget import capacity
        async with capacity(lease):yield lease
    finally:
        try:await finish_before_cancel(_finish(lease))
        finally:_current.reset(token)


def require_context(lease):
    if _current.get() is not lease:raise ValueError('Storage IO requires its reserved execution context')


async def status_locked(db,case_id):
    from .. import storage
    rows=(await db.scalars(select(CaseStorageOperation).where(CaseStorageOperation.case_id==case_id,
        CaseStorageOperation.state!='settled').order_by(CaseStorageOperation.created_at))).all()
    if not rows:return None
    return {'backend':'s3' if storage._use_s3() else 'fs','status':'busy' if any(row.state=='active' for row in rows) else 'recovery_required',
            'code':'file_sync_busy' if any(row.state=='active' for row in rows) else 'file_recovery_required',
            'operations':[{'id':row.id,'purpose':row.purpose,'state':row.state,'journal_id':row.journal_id} for row in rows]}
