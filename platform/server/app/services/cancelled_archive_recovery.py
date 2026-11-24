"""Cancel a stopped original attempt without claiming its archive completed.

The versioned preparation excludes unregistered finalization writers. Exact
copy-request closure excludes local PUT work; uncertain remote bytes stay
reserved. This path never creates a validator, cache, log upload or publication.
"""
import asyncio,json,os,uuid
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from sqlalchemy import select

from ..db import SessionLocal
from ..models import CaseStorageOperation,DurableEvent,NativeRequest,OutputArchiveReservation
from . import job_output_finalizing as full,attempt_output_recovery as outputs,output_archive_storage as archives
from . import execution_budget,job_execution,finalizing_io as io,process_identity_native as identity
from .async_ops import finish_before_cancel
from .durable_events import append_event,json_text,lock_case_execution,now
from .workflow_state import ExecutionConflict

CHECKPOINT='vibeflow.job-write-checkpoint.v6'
SCHEMA='vibeflow.cancelled-archive-recovery.v1'
COPY='attempt.cancelled_archive_copy_closed'
COPY_SCHEMA='vibeflow.cancelled-archive-copy.v1'


def pending(job,attempt,output):
    return (job.status=='cancelled' and attempt.cancel_requested_at is not None
        and attempt.phase in {'cancelling','cancelled'} and outputs.current_contract(output.get(outputs.KEY))
        and output.get('object_store',{}).get('state')=='pending' and full.KEY not in output)


def copy_id(attempt):return str(uuid.uuid5(uuid.UUID(attempt.id),COPY_SCHEMA))


def reservation(row):
    if row is None:return None
    identity.validate(json.loads(row.writer_json))
    if row.state not in {'active','released','materialized'} or row.remaining_bytes!=(row.size if row.state=='active' else 0):
        raise ValueError('Pending archive reservation has inconsistent capacity')
    return {'attempt_id':row.attempt_id,'case_id':row.case_id,'user_id':row.user_id,
        'binding_sha256':outputs.digest(row.binding_json),'input_sha256':row.input_sha256,
        'manifest_sha256':row.manifest_sha256,'size':row.size,'claim_sha256':outputs.digest(row.token),
        'writer':json.loads(row.writer_json)}


async def current_copy(db,records,user_id):
    case,job,attempt,*_=records
    row=await db.get(OutputArchiveReservation,attempt.id,populate_existing=True)
    if row is not None:
        _,_,receipt,size=archives.plan(case.s3_prefix,attempt.input_manifest_json,attempt.output_manifest_json)
        from .upload_storage import binding
        ticket=SimpleNamespace(attempt_id=attempt.id,case_id=case.id,user_id=user_id,binding_json=binding(case),
            input_sha256=outputs.digest(attempt.input_manifest_json),manifest_sha256=receipt['sha256'],size=size)
        archives.require_binding(row,ticket)
    return row,reservation(row)


async def no_validator(db,case,job,attempt):
    if full.KEY in json.loads(attempt.output_manifest_json):raise ValueError('Pending cancellation acquired a validator pointer')
    rows=await db.scalars(select(NativeRequest).where(NativeRequest.case_id==case.id,NativeRequest.kind==full.KIND))
    for row in rows:
        profile=json.loads(row.profile_json)
        if profile.get('job_id')==job.id or profile.get('attempt_id')==attempt.id:
            raise ExecutionConflict('recovery_required','原任务存在独立校验记录，不能按未启动校验的路径取消')


async def observe_copy(records,user_id,owner_event_id):
    async with SessionLocal() as db:
        row,core=await current_copy(db,records,user_id)
        if row is None:method,reference='not_admitted',None
        elif await outputs.writer_drained(db,row):method,reference='request_drained',outputs.copy_event_id(row.attempt_id,row.token)
        elif row.state in {'released','materialized'}:method,reference='reservation_closed',None
        elif core['writer']==records[5]['process']:method,reference='original_owner_exited',owner_event_id
        else:method,reference='owner_exited',None
        state=row.state if row is not None else None
        remaining=row.remaining_bytes if row is not None else None
    # Probe outside both SQL and owner locks. Mismatch, expiry and unknown are
    # never exits; the exact reservation is checked again before certification.
    observer=await asyncio.to_thread(identity.observe,os.getpid())
    if method=='owner_exited' and await asyncio.to_thread(identity.probe,core['writer'])!={'state':'exited','reason':'process_exited'}:
        raise ExecutionConflict('output_archive_busy','原补传请求仍在执行或其实际结束无法核验；保留取消中状态和完整容量预留')
    return {'schema':COPY_SCHEMA,'job_id':records[1].id,'attempt_id':records[2].id,
        'reservation':core,'reservation_state':state,'remaining_bytes':remaining,'method':method,'reference':reference,
        'observer':observer,'observed_at':now().isoformat(),
        'owner_exit':{'state':'exited','reason':'process_exited'} if method in {'owner_exited','original_owner_exited'} else None}


async def verify_copy(db,records,user_id,value):
    fields={'schema','job_id','attempt_id','reservation','reservation_state','remaining_bytes','method','reference','observer','observed_at','owner_exit'}
    if (not isinstance(value,dict) or set(value)!=fields or value['schema']!=COPY_SCHEMA
            or value['job_id']!=records[1].id or value['attempt_id']!=records[2].id):
        raise ValueError('Cancelled archive copy proof lost its original attempt')
    row,core=await current_copy(db,records,user_id)
    if core!=value['reservation']:raise ExecutionConflict('state_conflict','取消期间原补传归属或清单发生变化')
    observer=identity.validate(value['observer'])
    if datetime.fromisoformat(value['observed_at']).utcoffset()!=timedelta(0):raise ValueError('Copy observation requires UTC')
    method=value['method'];reference=value['reference']
    if core is None:
        if method!='not_admitted' or any(value[k] is not None for k in ('reservation_state','remaining_bytes','reference','owner_exit')):
            raise ValueError('Missing reservation does not prove a prior writer stopped')
        return
    state=value['reservation_state']
    if state not in {'active','released','materialized'} or value['remaining_bytes']!=(core['size'] if state=='active' else 0):
        raise ValueError('Copy closure lost its retained capacity snapshot')
    if method=='request_drained':
        if reference!=outputs.copy_event_id(row.attempt_id,row.token) or not await outputs.writer_drained(db,row):
            raise ValueError('Cancelled copy has no exact actual request return')
    elif method=='reservation_closed':
        if state not in {'released','materialized'} or row.state!=state or reference is not None:
            raise ValueError('Copy reservation was not closed before cancellation')
    elif method=='original_owner_exited':
        event=await outputs.read_owner_exit(db,records)
        if core['writer']!=records[5]['process'] or event is None or reference!=event.id:
            raise ValueError('Copy writer differs from the positively exited original owner')
    elif method=='owner_exited':
        writer=core['writer']
        if (reference is not None or observer==writer or observer['platform']!=writer['platform'] or observer['host_id']!=writer['host_id']):
            raise ValueError('Copy owner exit has no independent local witness')
    else:raise ValueError('Unknown pending archive copy closure')
    if value['owner_exit']!=({'state':'exited','reason':'process_exited'} if method in {'owner_exited','original_owner_exited'} else None):
        raise ValueError('Copy closure confused local return with process exit')


def validate_certificate(case,job,attempt,value):
    from .job_write_checkpoint import FIELDS,claim_digest
    output=json.loads(attempt.output_manifest_json);proof=value.get('recovery')
    fields={'schema','binding_sha256','owner_exit_event_id','copy_event_id','io_operation_id','io_intent_sha256','usage_sha256'}
    if (set(value)!=FIELDS|{'recovery'} or value.get('schema')!=CHECKPOINT or value.get('state')!='stopped'
            or not pending(job,attempt,output) or value['case_id']!=case.id or value['job_id']!=job.id or job.case_id!=case.id
            or value['attempt_id']!=attempt.id or attempt.job_id!=job.id or value['case_prefix']!=case.s3_prefix
            or value['claim_sha256']!=claim_digest(attempt) or value['process_identity_json']!=attempt.process_identity_json
            or value['child_processes_json']!=attempt.child_processes_json or value['stop']!=output[outputs.KEY]['stop']
            or not isinstance(proof,dict) or set(proof)!=fields or proof['schema']!=SCHEMA
            or proof['binding_sha256']!=full.frozen_binding(case,job,attempt) or proof['copy_event_id']!=copy_id(attempt)
            or job.log_s3_key!=output[full.source.KEY]['source_log_key']):
        raise ValueError('Pending archive cancellation differs from its original stopped execution')
    outputs.validate_preparation(case,job,attempt,output,output[outputs.KEY]);outputs.closed(job,attempt,value['stop'])
    full.source.validate(case,job,attempt,output,output[full.source.KEY])
    if datetime.fromisoformat(value['recorded_at']).utcoffset()!=timedelta(0):raise ValueError('Cancellation checkpoint requires UTC')
    if output.get('process_stop',value['stop'])!=value['stop']:raise ValueError('Cancellation changed original physical stop')
    return value


async def verify_history(db,case,job,attempt,checkpoint):
    from ..models import Workspace
    workspace=await db.get(Workspace,case.workspace_id)
    records=(case,job,attempt,None,None,full.job_execution_owner(job,attempt),None)
    validate_certificate(case,job,attempt,checkpoint);proof=checkpoint['recovery']
    owner=await outputs.read_owner_exit(db,records)
    if owner is None or proof['owner_exit_event_id']!=owner.id:raise ValueError('Cancellation lost original worker exit')
    event=await db.get(DurableEvent,copy_id(attempt))
    if event is None or event.type!=COPY or event.case_id!=case.id or event.attempt_id!=attempt.id:
        raise ValueError('Cancellation lost exact archive copy closure')
    await verify_copy(db,records,workspace.user_id,json.loads(event.payload_json))
    stamp=datetime.fromisoformat(json.loads(event.payload_json)['observed_at'])
    recorded=event.created_at.replace(tzinfo=timezone.utc) if event.created_at.tzinfo is None else event.created_at
    if stamp>recorded:raise ValueError('Copy closure predates its observation')
    row=await db.get(CaseStorageOperation,proof['io_operation_id'])
    if row is None:raise ValueError('Cancellation lost its persistent IO exclusion')
    started=await io.intent(db,row,io.binding(case,job,attempt))
    if proof['io_intent_sha256']!=full.digest(started):raise ValueError('Cancellation changed its IO intent')
    await no_validator(db,case,job,attempt)
    from .execution_output_checkpoint import read
    usage=(await read(db,job,attempt))['payload']['usage']
    if proof['usage_sha256']!=full.digest(usage):raise ValueError('Cancellation lost original consumed/unknown output credit')
    original=json.loads(attempt.output_manifest_json)[outputs.KEY]['output_usage']
    if any(original[k]!=usage[k] for k in original if k not in {'violation','violation_source'}):
        raise ValueError('Unstarted cancellation changed original output counters')
    events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type==full.DRAINED).limit(2)))
    if len(events)!=1 or events[0].case_id!=case.id or json.loads(events[0].payload_json)!={'checkpoint_sha256':outputs.digest(attempt.write_checkpoint_json)}:
        raise ValueError('Cancellation lost its unique checkpoint history')


@dataclass(frozen=True)
class Recovery:
    binding_sha256:str


async def require_recovery(db,job,attempt,proof,status,reason):
    from ..models import Case
    from .job_write_checkpoint import validate
    case=await db.get(Case,job.case_id,populate_existing=True);checkpoint=validate(case,job,attempt)
    if (not isinstance(proof,Recovery) or status!='cancelled' or reason!='cancel_requested'
            or checkpoint['schema']!=CHECKPOINT or proof.binding_sha256!=full.frozen_binding(case,job,attempt)):
        raise ValueError('Pending archive closure only authorizes original cancellation')
    await verify_history(db,case,job,attempt,checkpoint)


async def settle(records,user_id):
    case,job,attempt,_,output,*_=records
    guard=io.require_returned_cancel()
    if not pending(job,attempt,output) or not guard.protocol_protected:raise ValueError('Unversioned pending archive has no cancellation authority')
    observed,_,owner_event_id=await outputs.observe_original(records,user_id,persist=True,allow_cancelling=True)
    if observed!={'state':'exited','reason':'process_exited'} or owner_event_id is None:
        raise ExecutionConflict('recovery_required','原worker实际退出尚未核验；不能结束原尝试')
    claim=job_execution.Claim(job.id,attempt.id,attempt.root_job_id,attempt.number,attempt.claim_token)
    budget=await execution_budget.load(claim);budget.restore(output[outputs.KEY]['output_usage'])
    copy=await observe_copy(records,user_id,owner_event_id) if attempt.write_checkpoint_json=='{}' else None
    with budget.activate():
        async with SessionLocal() as db:
            await lock_case_execution(db,case.id)
            # Same owner lock as archive admission. Absence of a reservation is
            # certified only after any in-flight admission commits or refuses.
            from .storage_admission import lock_owner
            await lock_owner(db,user_id)
            current=await full.snapshot(db,job.id,user_id,allow_cancellation=True,allow_pending_log=True)
            case,job,attempt,*_=current
            if full.frozen_binding(case,job,attempt)!=full.frozen_binding(*records[:3]):
                raise ExecutionConflict('state_conflict','原取消收尾绑定已变化')
            await no_validator(db,case,job,attempt)
            if attempt.write_checkpoint_json=='{}':
                await verify_copy(db,current,user_id,copy)
                await append_event(db,case.id,COPY,copy,attempt_id=attempt.id,event_id=copy_id(attempt))
                from .job_write_checkpoint import claim_digest
                certificate={'schema':SCHEMA,'binding_sha256':full.frozen_binding(case,job,attempt),'owner_exit_event_id':owner_event_id,
                    'copy_event_id':copy_id(attempt),'io_operation_id':guard.lease.id,'io_intent_sha256':full.digest(guard.started),
                    'usage_sha256':full.digest(budget.usage())}
                checkpoint={'schema':CHECKPOINT,'case_id':case.id,'job_id':job.id,'attempt_id':attempt.id,'case_prefix':case.s3_prefix,
                    'claim_sha256':claim_digest(attempt),'state':'stopped','process_identity_json':attempt.process_identity_json,
                    'child_processes_json':attempt.child_processes_json,'stop':output[outputs.KEY]['stop'],'recorded_at':now().isoformat(),'recovery':certificate}
                attempt.write_checkpoint_json=json_text(checkpoint);validate_certificate(case,job,attempt,checkpoint)
                from .execution_output_checkpoint import record
                await record(db,job,attempt,claim)
                await job_execution.attempt_event(db,job,attempt,full.DRAINED,{'checkpoint_sha256':outputs.digest(attempt.write_checkpoint_json)})
                await finish_before_cancel(db.commit())
        from .publication_source_validation import for_finish
        async with for_finish(claim,'cancelled',None) as (validation,unused):
            await job_execution._finish_owned(claim,'cancelled',job.exit_code,reason='cancel_requested',process_stop=output[outputs.KEY]['stop'],
                return_result=False,check_success=None,prepared=None,validation=validation,recovery=Recovery(full.frozen_binding(case,job,attempt)))
    async with SessionLocal() as db:final=await full.snapshot(db,job.id,user_id)
    return await full.response(final)
