"""Retire a dead API's validator and link an explicit same-attempt retry.

Original solver/worker journals and missing original stop records are preserved.
The recovered request remains a failed historical request with separate actual
scope evidence; the next request inherits spent and unsettled output credit.
"""
import asyncio,json,os,uuid
from ..db import SessionLocal
from ..models import DurableEvent,NativeRequest
from . import finalizing_native_scope as scopes,finalizing_native_usage as usage,native_request_records as records
from . import process_identity_native as identity
from .durable_events import append_event,json_text,now
from .async_ops import finish_before_cancel

SCHEMA='vibeflow.finalizer-owner-recovery.v1'
RECOVERED='job.finalizer_owner_recovered'
RETRIED='job.finalizer_retried'
LIMIT=128


def event_id(request_id,kind):return str(uuid.uuid5(uuid.UUID(request_id),SCHEMA+':'+kind))


async def read(db,row):
    from .job_output_finalizing import digest,validate_result
    event=await db.get(DurableEvent,event_id(row.id,'recovered'),populate_existing=True)
    if event is None:raise ValueError('Dead validator lacks its independent recovery certificate')
    value=json.loads(event.payload_json)
    if (event.case_id!=row.case_id or event.type!=RECOVERED or row.phase!='failed' or row.error!='OwnerExited'
            or row.ended_at is None or set(value)!={'schema','request_id','owner','owner_exit','observer','observed_at',
                'profile_sha256','children_sha256','result_sha256','closures_sha256','output_settlement'}
            or value['schema']!=SCHEMA or value['request_id']!=row.id or value['owner']!=json.loads(row.owner_identity_json)
            or value['owner_exit']!={'state':'exited','reason':'process_exited'}
            or value['profile_sha256']!=digest(json.loads(row.profile_json))
            or value['children_sha256']!=digest(json.loads(row.children_json))
            or value['result_sha256']!=digest(json.loads(row.result_json))):
        raise ValueError('Independent owner recovery differs from its preserved request')
    from datetime import datetime,timedelta,timezone
    observer=identity.validate(value['observer']);stamp=datetime.fromisoformat(value['observed_at'])
    at=event.created_at.replace(tzinfo=timezone.utc) if event.created_at.tzinfo is None else event.created_at
    if (observer['host_id']!=value['owner']['host_id'] or observer==value['owner'] or stamp.utcoffset()!=timedelta(0) or stamp>at):
        raise ValueError('Invalid independent owner exit witness')
    proofs={}
    for key,child in records.document(row)['children'].items():
        scope=await scopes.read(db,row,child)
        if child['identity'] is None:
            from .finalizing_native_preparation import read as read_preparation
            preparation=await read_preparation(db,row,child,scope)
            if preparation is not None:proofs[key]=preparation;continue
        if scope is None or scope['closed'] is None:raise ValueError('Recovered validator lost its actual scope closure')
        proofs[key]=scope['closed']
    if value['closures_sha256']!=digest(proofs):raise ValueError('Independent recovery scope receipts changed')
    result=validate_result(json.loads(row.result_json),json.loads(row.profile_json))
    if result['error']!='OwnerExited' or result['prepared'] is not None or result['usage']!=usage.validate_reconciliation(row,value['output_settlement'],proofs):
        raise ValueError('Dead validator lost observed usage or retained unknown credit')
    return value


async def retire(records_before,user_id,*,allow_cancellation=False):
    from . import job_output_finalizing as finalizing
    case,job,attempt,manifest,output,*_=records_before
    pointer=output.get(finalizing.KEY)
    if pointer is None:return False
    async with SessionLocal() as db:
        row=await db.get(NativeRequest,pointer['request_id'])
        if row is None or row.kind!=finalizing.KIND or row.created_by!=user_id or json.loads(row.profile_json)!=finalizing.profile_for(records_before):
            raise ValueError('Independent recovery request belongs to another attempt')
        before=scopes.fingerprint(row)
    if row.phase!='active':
        if row.phase=='cancelled':
            from .finalizing_request_cancellation import retire as retire_cancelled
            root=finalizing.objects.storage._fs_path(case.s3_prefix,manifest['run_path']).parent
            return await retire_cancelled(row,root)
        return row.error=='OwnerExited'
    owner=json.loads(row.owner_identity_json)
    observed=await asyncio.to_thread(identity.probe,owner)
    if observed!={'state':'exited','reason':'process_exited'}:
        from .workflow_state import ExecutionConflict
        raise ExecutionConflict('recovery_required','原恢复校验仍在进行，尚未核验其API实际退出')
    proofs=await scopes.recover(row)
    root=finalizing.objects.storage._fs_path(case.s3_prefix,manifest['run_path']).parent
    settled,evidence=await finish_before_cancel(asyncio.to_thread(usage.reconcile,row,root,proofs))
    observer=await asyncio.to_thread(identity.observe,os.getpid());at=now().isoformat()
    async with SessionLocal() as db:
        current=await finalizing.snapshot(db,job.id,user_id,locked=True,allow_cancellation=allow_cancellation)
        if current[4].get(finalizing.KEY)!=pointer or finalizing.profile_for(current)!=json.loads(row.profile_json):
            raise ValueError('Original attempt changed during independent validator recovery')
        selected=await db.get(NativeRequest,row.id,populate_existing=True)
        if selected is None:raise ValueError('Independent request disappeared')
        if selected.phase!='active':await read(db,selected);return True
        if scopes.fingerprint(selected)!=before:raise ValueError('Independent request changed during owner recovery')
        journal=records.document(selected)
        for child in journal['children'].values():
            if child['phase'] not in {'stopped','failed'}:
                child.update(phase='failed',error='OwnerExited',ended_at=now().isoformat())
                # Preserve identity and missing stop exactly; separate scope
                # receipts prove recovery without inventing the dead API's stop.
        selected.children_json=json_text(journal);records.document(selected)
        result={'schema':finalizing.CERTIFICATE,'profile_sha256':finalizing.digest(json.loads(selected.profile_json)),
            'usage':settled,'prepared':None,'error':'OwnerExited'}
        selected.result_json=json_text(finalizing.validate_result(result,json.loads(selected.profile_json)))
        selected.phase='failed';selected.error='OwnerExited';selected.ended_at=now();selected.version+=1
        payload={'schema':SCHEMA,'request_id':selected.id,'owner':owner,'owner_exit':observed,'observer':observer,'observed_at':at,
            'profile_sha256':finalizing.digest(json.loads(selected.profile_json)),
            'children_sha256':finalizing.digest(journal),'result_sha256':finalizing.digest(result),
            'closures_sha256':finalizing.digest(proofs),'output_settlement':evidence}
        await append_event(db,case.id,RECOVERED,payload,attempt_id=attempt.id,event_id=event_id(selected.id,'recovered'))
        await db.flush();await read(db,selected);await finish_before_cancel(db.commit())
    return True


async def link(db,old,new,profile):
    from .job_output_finalizing import digest
    old_usage=await retry_usage(db,old)
    await chain(db,old,old_usage)
    payload={'schema':SCHEMA,'previous_request_id':old.id,'request_id':new.id,
        'previous_result_sha256':digest(json.loads(old.result_json)),'profile_sha256':digest(profile)}
    await append_event(db,old.case_id,RETRIED,payload,attempt_id=profile['attempt_id'],event_id=event_id(new.id,'retried'))


async def chain(db,row,current_usage):
    from .job_output_finalizing import digest
    visited={row.id}
    while True:
        event=await db.get(DurableEvent,event_id(row.id,'retried'),populate_existing=True)
        if event is None:return
        value=json.loads(event.payload_json)
        if (event.type!=RETRIED or event.case_id!=row.case_id or set(value)!={'schema','previous_request_id','request_id','previous_result_sha256','profile_sha256'}
                or value['schema']!=SCHEMA or value['request_id']!=row.id or value['profile_sha256']!=digest(json.loads(row.profile_json))):
            raise ValueError('Independent retry lost its original lineage')
        previous=await db.get(NativeRequest,value['previous_request_id'],populate_existing=True)
        if (previous is None or previous.id in visited or len(visited)>=LIMIT or previous.kind!=row.kind
                or previous.case_id!=row.case_id or previous.created_by!=row.created_by or previous.profile_json!=row.profile_json):
            raise ValueError('Independent validator retry chain is invalid or exhausted')
        old=await retry_usage(db,previous);result=json.loads(previous.result_json)
        if value['previous_result_sha256']!=digest(result):raise ValueError('Independent retry predecessor changed')
        if (current_usage['observed_output_bytes']<old['observed_output_bytes']
                or current_usage.get('unsettled_output_bytes',0)<old.get('unsettled_output_bytes',0)
                or any(current_usage['by_source'].get(k,0)<n for k,n in old['by_source'].items())):
            raise ValueError('Independent retry reset spent or unsettled output credit')
        visited.add(previous.id);row=previous;current_usage=old


async def retry_usage(db,row):
    if row.phase=='cancelled':
        from .finalizing_request_cancellation import read as cancelled_receipt
        return (await cancelled_receipt(db,row))['usage']
    await read(db,row)
    return json.loads(row.result_json)['usage']
