"""Terminal request fencing and actual drainage after an HTTP cancellation.

The API may still be alive. Its cancelled NativeRequest permanently rejects
launches; that fence never stands in for a physical process/scope closure.
Original terminal rows remain unchanged when a later request reconciles credit.
"""
import asyncio,copy,json,re,uuid
from datetime import timezone

from ..db import SessionLocal
from ..models import DurableEvent,NativeRequest
from . import finalizing_native_scope as scopes,finalizing_native_usage as usage,native_request_records as records
from .async_ops import finish_before_cancel
from .durable_events import append_event,lock_case_execution

LEGACY_SCHEMA='vibeflow.finalizer-cancelled-recovery.v1'
SCHEMA='vibeflow.finalizer-cancelled-recovery.v2'
FENCED='job.finalizer_cancelled'
CHANNEL='job.finalizer_cancelled_channel'
DRAINED='job.finalizer_cancelled_drained'


def event_id(request_id,stage,*,schema=SCHEMA):
    if schema not in {LEGACY_SCHEMA,SCHEMA}:raise ValueError('Unsupported cancelled validator history version')
    return str(uuid.uuid5(uuid.UUID(request_id),schema+':'+stage))


def terminal_digest(row):
    if (row.kind!='job.finalize' or row.phase!='cancelled' or not isinstance(row.error,str)
            or re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,79}',row.error) is None or row.ended_at is None):
        raise ValueError('Only a durably cancelled validator may use its terminal request fence')
    ended=row.ended_at.replace(tzinfo=timezone.utc) if row.ended_at.tzinfo is None else row.ended_at.astimezone(timezone.utc)
    return scopes.digest([scopes.fingerprint(row),row.case_id,row.created_by,row.kind,row.error,ended.isoformat()])


async def record(db,row,progress):
    """Commit the terminal fence and last read windows with the real owner row."""
    value=copy.deepcopy(usage.validate(progress,row));channels=value.pop('channels')
    profile=json.loads(row.profile_json)
    from .job_output_finalizing import validate_result
    result=validate_result(json.loads(row.result_json),profile)
    if result['error']!=row.error or result['usage']!=value['usage']:
        raise ValueError('Cancelled finalizer lost its last actual output accounting')
    # Persist each bounded channel separately instead of putting the complete
    # native tree into one event with a 256KiB payload limit.
    for key,item in channels.items():
        await append_event(db,row.case_id,CHANNEL,{'schema':SCHEMA,'request_id':row.id,'child_id':key,'value':item},
            attempt_id=profile['attempt_id'],event_id=event_id(row.id,'channel:'+key))
    payload={'schema':SCHEMA,'request_id':row.id,'request_sha256':terminal_digest(row),
        'cancellation':{'observed':True,'primary_error':row.error},
        'progress':value,'channels_sha256':{k:scopes.digest(v) for k,v in channels.items()}}
    await append_event(db,row.case_id,FENCED,payload,attempt_id=profile['attempt_id'],event_id=event_id(row.id,'fenced'))


async def require_fence(db,row):
    expected=terminal_digest(row);profile=json.loads(row.profile_json)
    event=await db.get(DurableEvent,event_id(row.id,'fenced'),populate_existing=True)
    legacy=await db.get(DurableEvent,event_id(row.id,'fenced',schema=LEGACY_SCHEMA),populate_existing=True)
    if event is not None and legacy is not None:raise ValueError('Cancelled validator has ambiguous terminal fence versions')
    schema=SCHEMA
    if event is None:event=legacy;schema=LEGACY_SCHEMA
    if event is None:raise ValueError('Historical cancelled validator has no durable terminal fence')
    value=json.loads(event.payload_json)
    fields={'schema','request_id','request_sha256','progress','channels_sha256'}
    if schema==SCHEMA:fields.add('cancellation')
    if (event.case_id!=row.case_id or event.type!=FENCED or event.attempt_id!=profile['attempt_id']
            or set(value)!=fields or value['schema']!=schema or value['request_id']!=row.id or value['request_sha256']!=expected
            or schema==LEGACY_SCHEMA and row.error!='CancelledError'
            or not isinstance(value['channels_sha256'],dict)
            or set(value['channels_sha256'])!=set(records.document(row)['children'])):
        raise ValueError('Cancelled validator differs from its immutable terminal fence')
    if schema==SCHEMA and value['cancellation']!={'observed':True,'primary_error':row.error}:
        raise ValueError('Cancelled validator lost the cancellation and preserved primary failure')
    progress=copy.deepcopy(value['progress']);channels={}
    if 'channels' in progress:raise ValueError('Cancelled read windows have two authorities')
    for key,digest in value['channels_sha256'].items():
        selected=await db.get(DurableEvent,event_id(row.id,'channel:'+key,schema=schema),populate_existing=True)
        if selected is None:raise ValueError('Cancelled validator lost a read window')
        item=json.loads(selected.payload_json)
        if (selected.case_id!=row.case_id or selected.type!=CHANNEL or selected.attempt_id!=profile['attempt_id']
                or set(item)!={'schema','request_id','child_id','value'} or item['schema']!=schema
                or item['request_id']!=row.id or item['child_id']!=key or scopes.digest(item['value'])!=digest):
            raise ValueError('Cancelled read window changed after terminal commit')
        channels[key]=item['value']
    progress['channels']=channels;usage.validate(progress,row)
    if progress['usage']!=json.loads(row.result_json)['usage']:raise ValueError('Cancelled accounting changed after terminal commit')
    return value,progress


async def closures(db,row):
    from .finalizing_native_preparation import read as read_preparation
    proofs={}
    for key,child in records.document(row)['children'].items():
        scope=await scopes.read(db,row,child)
        if child['identity'] is None:
            proof=await read_preparation(db,row,child,scope)
            if proof is None:raise ValueError('Cancelled preparation lacks actual closure and non-release evidence')
        else:
            if scope is None or scope['closed'] is None:raise ValueError('Cancelled validator lacks actual scope closure')
            proof=scope['closed']
        proofs[key]=proof
    return proofs


async def read(db,row):
    fence,progress=await require_fence(db,row)
    event=await db.get(DurableEvent,event_id(row.id,'drained',schema=fence['schema']),populate_existing=True)
    if event is None:raise ValueError('Cancelled request fence has no physical drainage certificate')
    value=json.loads(event.payload_json);proofs=await closures(db,row)
    if (event.case_id!=row.case_id or event.type!=DRAINED or event.attempt_id!=json.loads(row.profile_json)['attempt_id']
            or set(value)!={'schema','request_id','fence_sha256','closures_sha256','output_settlement','usage'}
            or value['schema']!=fence['schema'] or value['request_id']!=row.id or value['fence_sha256']!=scopes.digest(fence)
            or value['closures_sha256']!=scopes.digest(proofs) or value['output_settlement']['progress']!=progress
            or value['usage']!=usage.validate_reconciliation(row,value['output_settlement'],proofs)):
        raise ValueError('Cancelled drainage changed its terminal request or actual output settlement')
    return value


async def retire(row,root):
    async with SessionLocal() as db:
        fence,progress=await require_fence(db,row)
        if await db.get(DurableEvent,event_id(row.id,'drained',schema=fence['schema'])) is not None:
            await read(db,row);return True
    before=scopes.fingerprint(row)
    proofs=await scopes.recover(row,cancelled=True)
    settled,evidence=await finish_before_cancel(asyncio.to_thread(usage.reconcile,row,root,proofs,progress=progress))
    value={'schema':fence['schema'],'request_id':row.id,'fence_sha256':scopes.digest(fence),
        'closures_sha256':scopes.digest(proofs),'output_settlement':evidence,'usage':settled}
    async with SessionLocal() as db:
        from .case_validation import assert_writable_locked
        await lock_case_execution(db,row.case_id);await assert_writable_locked(db,row.case_id)
        selected=await db.get(NativeRequest,row.id,populate_existing=True)
        if selected is None or scopes.fingerprint(selected)!=before:raise ValueError('Cancelled validator changed during drainage')
        await require_fence(db,selected)
        if await db.get(DurableEvent,event_id(row.id,'drained',schema=fence['schema'])) is None:
            await append_event(db,row.case_id,DRAINED,value,attempt_id=json.loads(row.profile_json)['attempt_id'],event_id=event_id(row.id,'drained',schema=fence['schema']))
        await db.flush();await read(db,selected);await finish_before_cancel(db.commit())
    return True
