"""Persist ownership before any finalization cache/log/publication IO.

This is a local IO exclusion proof, never a native process-stop certificate.
Every protected thread is joined before the caller records its actual return.
Legacy operations without this contract require their existing recovery path.
"""
import asyncio,json,os,uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import SimpleNamespace
from sqlalchemy import select

from ..db import SessionLocal
from ..models import CaseStorageOperation,CaseValidationFence,DurableEvent,JobLogReservation
from . import job_output_finalizing as finalizing,storage_operations as operations,process_identity_native as identity
from .async_ops import finish_before_cancel
from .durable_events import append_event,json_text,lock_case_execution,now
from .workflow_state import ExecutionConflict,TERMINAL

SCHEMA='vibeflow.finalizing-io.v1'
STARTED='job.finalizing_io_started'
DRAINED='job.finalizing_io_drained'
EXITED='job.finalizing_io_owner_exited'
CURRENT=ContextVar('finalizing_io_guard',default=None)


def event_id(identifier,stage):return str(uuid.uuid5(uuid.UUID(identifier),SCHEMA+':'+stage))
def binding(case,job,attempt):
    return {'job_id':job.id,'attempt_id':attempt.id,'case_id':case.id,
        'source_prefix':case.s3_prefix,'frozen_sha256':finalizing.frozen_binding(case,job,attempt)}
def busy():return ExecutionConflict('output_recovery_busy','原收尾存储操作仍在执行或实际停止尚未核验')


async def intent(db,row,expected):
    event=await db.get(DurableEvent,event_id(row.id,'started'),populate_existing=True)
    value=json.loads(event.payload_json) if event else None
    if (event is None or event.id!=row.event_id or event.type!=STARTED or event.case_id!=row.case_id
            or event.attempt_id!=row.journal_id or not isinstance(value,dict)
            or set(value)!={'schema','operation_id','binding','owner','admitted_job_status'}
            or value['schema']!=SCHEMA or value['operation_id']!=row.id or value['binding']!=expected
            or value['owner']!=json.loads(row.owner_json) or value['admitted_job_status'] not in {'running','cancelled','failed','interrupted','succeeded'}
            or row.purpose!='recovery' or row.journal_id!=expected['attempt_id'] or row.case_id!=expected['case_id']
            or row.source_prefix!=expected['source_prefix'] or row.plan_sha256!=finalizing.digest(expected)):
        raise ExecutionConflict('file_recovery_required','原存储操作缺少此收尾尝试的持久归属证明；保留现场并按原入口恢复')
    identity.validate(value['owner']);return value


async def closed(db,row,started):
    local=await db.get(DurableEvent,event_id(row.id,'drained'),populate_existing=True)
    exited=await db.get(DurableEvent,event_id(row.id,'exited'),populate_existing=True)
    if local is not None and exited is not None:raise ValueError('IO operation has conflicting return and exit proofs')
    event=local or exited
    if event is None:return None
    value=json.loads(event.payload_json)
    fields={'schema','operation_id','intent_sha256','log_ticket'}|({'io_drained','outcome'} if local else {'owner_exit','observer'})
    if (event.case_id!=row.case_id or event.attempt_id!=row.journal_id or event.type!=(DRAINED if local else EXITED)
            or set(value)!=fields or value['schema']!=SCHEMA or value['operation_id']!=row.id
            or value['intent_sha256']!=finalizing.digest(started)):
        raise ValueError('Finalizing IO closure changed its exact reservation')
    if local:
        if value['io_drained'] is not True or not isinstance(value['outcome'],str) or len(value['outcome'])>80:
            raise ValueError('Finalizing IO return has no actual local drainage proof')
    elif value['owner_exit']!={'state':'exited','reason':'process_exited'} or identity.validate(value['observer'])==started['owner']:
        raise ValueError('Finalizing IO recovery lacks independent exact owner exit')
    return value


def log_ticket(row):
    return {'claim_sha256':finalizing.outputs.digest(row.token),'attempt_id':row.attempt_id,'sha256':row.sha256,'size':row.size,
        'binding_sha256':finalizing.outputs.digest(row.binding_json),'writer':json.loads(row.writer_json)}


async def matching_log(db,row,started):
    log=await db.get(JobLogReservation,started['binding']['job_id'],populate_existing=True)
    return log_ticket(log) if log is not None and log.attempt_id==row.journal_id and log.writer_json==row.owner_json else None


async def log_writer_drained(db,log):
    from ..models import Case,Job,JobAttempt
    case=await db.get(Case,log.case_id);job=await db.get(Job,log.job_id);attempt=await db.get(JobAttempt,log.attempt_id)
    if case is None or job is None or attempt is None:return False
    expected=binding(case,job,attempt)
    rows=await db.scalars(select(CaseStorageOperation).where(CaseStorageOperation.case_id==case.id,
        CaseStorageOperation.journal_id==attempt.id,CaseStorageOperation.owner_json==log.writer_json))
    for row in rows:
        started=await intent(db,row,expected);proof=await closed(db,row,started)
        if proof is not None and proof['log_ticket']==log_ticket(log):return True
    return False


async def pending_log(db,case,job,attempt,log):
    expected=binding(case,job,attempt)
    rows=await db.scalars(select(CaseStorageOperation).where(CaseStorageOperation.case_id==case.id,
        CaseStorageOperation.journal_id==attempt.id,CaseStorageOperation.owner_json==log.writer_json))
    for row in rows:
        started=await intent(db,row,expected)
        if row.state=='active':return True  # Observation only; acquisition must still exclude its owner.
        proof=await closed(db,row,started)
        if proof is not None and proof['log_ticket']==log_ticket(log):return True
    return False


@dataclass
class Guard:
    lease:object
    started:dict
    trusted_previous:bool=False
    protocol_protected:bool=False
    work_returned:bool=False


async def snapshot(db,job_id,user_id):
    return await finalizing.snapshot(db,job_id,user_id,allow_cancellation=True,allow_pending_log=True)


async def acquire(job_id,user_id,attempt_id,inventory_sha256):
    async with SessionLocal() as db:
        records=await snapshot(db,job_id,user_id);case,job,attempt,_,output,*_=records
        if attempt.id!=attempt_id or finalizing.outputs.inventory_digest(output)!=inventory_sha256:
            raise ExecutionConflict('state_conflict','原收尾尝试或清单已变化')
        terminal=job.status in TERMINAL and attempt.phase==job.status
        completion_pending=terminal and finalizing.job_execution.completion_visibility(attempt) in {'pending','invalid'}
        expected=binding(case,job,attempt)
        rows=list(await db.scalars(select(CaseStorageOperation).where(CaseStorageOperation.case_id==case.id,
            CaseStorageOperation.state.in_(('active','drained','exited')))))
        if terminal:
            if not rows and not completion_pending:return None
            # A completed response needs no IO admission against unrelated
            # later file work. Only retire this attempt's exact leftover gate.
            for row in rows:
                if row.journal_id!=attempt.id or await db.get(DurableEvent,event_id(row.id,'started')) is None:return None
        candidates=[]
        for row in rows:
            started=await intent(db,row,expected);proof=await closed(db,row,started)
            if row.state!='active' and proof is None:raise ValueError('Prior finalizing IO has no stop authority')
            candidates.append((row.id,row.owner_json,row.state,started,proof))
        old=list(await db.scalars(select(CaseStorageOperation).where(CaseStorageOperation.case_id==case.id,
            CaseStorageOperation.journal_id==attempt.id,CaseStorageOperation.state=='settled')))
        trusted=False
        for row in old:
            event=await db.get(DurableEvent,event_id(row.id,'started'))
            if event is None:continue
            started=await intent(db,row,expected)
            if started['admitted_job_status']=='running' and await closed(db,row,started) is not None:trusted=True
    for identifier,owner,state,started,proof in candidates:
        if proof is None and await asyncio.to_thread(identity.probe,json.loads(owner))!={'state':'exited','reason':'process_exited'}:
            raise busy()
        trusted=trusted or started['admitted_job_status']=='running'
    owner_json=json_text(await asyncio.to_thread(identity.observe,os.getpid()))
    identifier=str(uuid.uuid4())
    async def checked(db):return (await snapshot(db,job_id,user_id))[0]
    lease=operations.Lease(identifier,SimpleNamespace(id=case.id,s3_prefix=case.s3_prefix),owner_json,checked,'recovery')
    guard=Guard(lease,{},trusted)
    try:
        async with SessionLocal() as db:
            await lock_case_execution(db,case.id)
            current=await snapshot(db,job_id,user_id)
            if binding(*current[:3])!=expected:raise ExecutionConflict('state_conflict','原收尾绑定在存储登记前变化')
            guard.protocol_protected=finalizing.outputs.current_contract(current[4].get(finalizing.outputs.KEY))
            actual=list(await db.scalars(select(CaseStorageOperation).where(CaseStorageOperation.case_id==case.id,
                CaseStorageOperation.state.in_(('active','drained','exited')))))
            if {(r.id,r.owner_json,r.state) for r in actual}!={(r[0],r[1],r[2]) for r in candidates}:raise busy()
            fence=await db.get(CaseValidationFence,case.id,populate_existing=True)
            # Only a fully visible terminal response is metadata-only. Pending
            # compatibility repair still takes the normal file-write gate.
            if (not terminal or completion_pending) and fence is not None and fence.status=='held':raise ExecutionConflict('case_validation_busy','算例正在校验')
            observer=json.loads(owner_json)
            for row in actual:
                started=await intent(db,row,expected);proof=await closed(db,row,started)
                if proof is None:
                    proof={'schema':SCHEMA,'operation_id':row.id,'intent_sha256':finalizing.digest(started),
                        'owner_exit':{'state':'exited','reason':'process_exited'},'observer':observer,'log_ticket':await matching_log(db,row,started)}
                    await append_event(db,case.id,EXITED,proof,attempt_id=attempt.id,event_id=event_id(row.id,'exited'))
                row.state='settled';row.ended_at=row.ended_at or now()
            await db.flush()
            guard.started={'schema':SCHEMA,'operation_id':identifier,'binding':expected,'owner':observer,'admitted_job_status':current[1].status}
            db.add(CaseStorageOperation(id=identifier,case_id=case.id,source_prefix=case.s3_prefix,purpose='recovery',state='active',
                owner_json=owner_json,journal_id=attempt.id,plan_sha256=finalizing.digest(expected),event_id=event_id(identifier,'started')))
            await append_event(db,case.id,STARTED,guard.started,attempt_id=attempt.id,event_id=event_id(identifier,'started'))
            await finish_before_cancel(db.commit())
    except BaseException:
        await finish_before_cancel(finish(guard,'admission_failed'));raise
    return guard


async def finish(guard,outcome):
    lease=guard.lease
    async with SessionLocal() as db:
        await lock_case_execution(db,lease.case.id)
        row=await db.get(CaseStorageOperation,lease.id,populate_existing=True)
        if row is None:return
        started=await intent(db,row,guard.started['binding'])
        if row.owner_json!=lease.owner_json:raise busy()
        if await closed(db,row,started) is not None:return
        if row.state!='active':raise ValueError('Finalizing IO reservation ended without its exact proof')
        value={'schema':SCHEMA,'operation_id':row.id,'intent_sha256':finalizing.digest(started),'io_drained':True,
            'outcome':outcome,'log_ticket':await matching_log(db,row,started)}
        await append_event(db,row.case_id,DRAINED,value,attempt_id=row.journal_id,event_id=event_id(row.id,'drained'))
        row.state='settled';row.ended_at=now();await finish_before_cancel(db.commit())


@asynccontextmanager
async def hold(job_id,user_id,attempt_id,inventory_sha256):
    guard=None
    async def reserve():
        nonlocal guard
        guard=await acquire(job_id,user_id,attempt_id,inventory_sha256)
    try:await finish_before_cancel(reserve())
    except BaseException:
        if guard is not None:await finish_before_cancel(finish(guard,'admission_cancelled'))
        raise
    if guard is None:yield None;return
    token=CURRENT.set(guard);outcome='returned'
    try:
        with operations.owned_context(guard.lease):
            try:yield guard
            except BaseException as error:outcome=type(error).__name__;raise
    finally:
        try:await finish_before_cancel(finish(guard,outcome))
        finally:CURRENT.reset(token)


def require_returned_cancel():
    guard=CURRENT.get()
    if guard is None or not guard.work_returned:
        raise ExecutionConflict('recovery_required','历史登记前存储操作尚无实际结束证明；不能推断已排空')
    if not (guard.started['admitted_job_status']=='running' or guard.trusted_previous or guard.protocol_protected):
        raise ExecutionConflict('legacy_finalizing_io_unverified','旧记录缺少登记前操作的结束证明；保留原数据，先核查旧API和执行现场，不能自动补造排空')
    operations.require_context(guard.lease)
    return guard


async def legacy_attention(job_id):
    from . import job_recovery_notice as notices
    async with SessionLocal() as lookup:
        _,job,_,_=await notices.rows(lookup,job_id)
        if job is None:return
        case_id=job.case_id
    async with SessionLocal() as db:
        await lock_case_execution(db,case_id)
        records=await notices.rows(db,job_id)
        _,job,attempt,_=records
        if (job is not None and attempt is not None and job.status=='cancelled' and attempt.phase=='cancelling'
                and finalizing.KEY not in json.loads(attempt.output_manifest_json)):
            await notices.record(db,records,'finalizing_io_unverified')
            await finish_before_cancel(db.commit())
