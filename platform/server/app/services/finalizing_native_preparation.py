"""Versioned proof of business non-release for incomplete validator startup.

A missing launch stage alone proves nothing for historical requests. New
reservations atomically bind the implementation and output window before any
scope creation. Unknown read-only bootstrap births remain explicitly unknown;
they are never rewritten as observed exits or original process-stop records.
"""
import asyncio,copy,hashlib,json,os,re,uuid
from datetime import datetime,timedelta,timezone

from ..db import SessionLocal
from ..models import DurableEvent,NativeRequest
from . import finalizing_native_scope as scopes,native_request_records as records
from . import process_identity_native as identity,owned_process
from .async_ops import finish_before_cancel
from .durable_events import append_event,json_text,lock_case_execution,now

SCHEMA='vibeflow.finalizer-preparation-closure.v1'
CANCEL_SCHEMA='vibeflow.finalizer-preparation-closure.v2'
TERMINAL_SCHEMA='vibeflow.finalizer-preparation-closure.v3'
INTENDED='job.finalizer_preparation_intended'
CLOSED='job.finalizer_preparation_closed'
LEGACY_FILES=set(scopes.HOST_FILES)|set(scopes.protocol.FILES)|{'finalizing_native_preparation.py'}
FILES=LEGACY_FILES|{'finalizing_request_cancellation.py'}


def event_id(child_id,stage):return str(uuid.uuid5(uuid.UUID(child_id),SCHEMA+':'+stage))

def terminal_digest(row):
    if row.kind!='job.finalize' or row.phase not in {'failed','succeeded'} or row.ended_at is None:
        raise ValueError('Preparation has no immutable completed request fence')
    ended=row.ended_at.replace(tzinfo=timezone.utc) if row.ended_at.tzinfo is None else row.ended_at.astimezone(timezone.utc)
    return scopes.digest([scopes.fingerprint(row),row.error,ended.isoformat()])
def code():return {p:hashlib.sha256(scopes.owned_process.GATE.with_name(p).read_bytes()).hexdigest() for p in sorted(FILES)}


async def intent(db,row,child):
    event=await db.get(DurableEvent,event_id(child['id'],'intended'),populate_existing=True)
    if event is None:return None
    value=json.loads(event.payload_json)
    if (event.case_id!=row.case_id or event.type!=INTENDED or len(event.payload_json.encode())>65536
            or set(value)!={'schema','binding','seed','implementation','output_usage'} or value['schema']!=SCHEMA
            or value['binding']!=scopes.binding(row,child) or not isinstance(value['seed'],str)
            or re.fullmatch('[0-9a-f]{64}',value['seed']) is None or not isinstance(value['implementation'],dict)
            or set(value['implementation']) not in (LEGACY_FILES,FILES) or any(not isinstance(v,str) or re.fullmatch('[0-9a-f]{64}',v) is None for v in value['implementation'].values())):
        raise ValueError('Incomplete validator reservation lacks its exact launch contract')
    from .execution_output_checkpoint import validate_usage
    validate_usage(value['output_usage'],json.loads(row.profile_json)['max_log_bytes'])
    return value


async def reserve(db,row,child,nonce):
    from . import finalizing_linux_scope as linux
    if linux.applies(row):return await linux.reserve(db,row,child,nonce)
    if not isinstance(nonce,str) or re.fullmatch('[0-9a-f]{64}',nonce) is None:
        raise ValueError('Validator reservation requires its not-yet-used process nonce')
    if await intent(db,row,child) is not None:raise ValueError('Validator reservation already has a launch contract')
    progress=json.loads(row.result_json)
    value={'schema':SCHEMA,'binding':scopes.binding(row,child),'seed':nonce,'implementation':code(),'output_usage':progress['usage']}
    await append_event(db,row.case_id,INTENDED,value,event_id=event_id(child['id'],'intended'))


async def require_open(db,row,child,stage,value):
    selected=await intent(db,row,child)
    if selected is None or selected['implementation']!=code():raise ValueError('Validator cannot start under a missing or changed preparation contract')
    if await db.get(DurableEvent,event_id(child['id'],'closed')) is not None:
        raise ValueError('Validator reservation was independently sealed before release')
    if stage=='plan' and (value['seed']!=selected['seed'] or any(selected['implementation'].get(n)!=h for n,h in value['code'].items())):
        raise ValueError('Validator scope plan changed its atomic reservation')


def births(value):
    result=[]
    def add(role,distribution,birth):result.append({'role':role,'distribution':distribution,'birth':birth})
    if value is None:return result
    distribution=value['plan']['allocation']['distribution'] if value['plan']['allocation'] else None
    prepared=value['prepared']
    if prepared is not None:
        add('allocation_host',None,prepared['host']['host']);add('allocation_creator',distribution,prepared['native']['controller'])
    selected=value['guardian']
    if selected is not None:add('guardian',None,selected['preparation']['guardian'])
    admitted=value['admitted']
    if admitted is not None:
        add('preflight_host',None,admitted['host']);add('preflight_native',distribution,admitted['native']['process'])
    for stage in scopes.process_preparation.STAGES:
        observation=value[stage]
        if observation is None:continue
        add(stage+'_host',None,observation['host'])
        if observation['native'] is not None:add(stage+'_native',distribution,observation['native']['process'])
    return result


def unobserved(value):
    if value is None:return []
    result=[];native=value['plan']['allocation'] is not None
    if native and value['prepared'] is None:result.append('allocation_controller')
    if value['guardian'] is None:result.append('windows_guardian')
    if native and value['guardian'] is not None and value['admitted'] is None:result.append('native_preflight')
    return result


def validate_retirement(scope,receipt):
    if scope is None or scope['prepared'] is None:
        if receipt is not None:raise ValueError('Unpermitted allocation has a fabricated retirement')
        return
    scopes.control.validate_receipt(receipt)
    allocated=scope['allocated'];request=scope['plan']['allocation']
    operation='retire' if allocated is not None else 'abandon'
    arguments={'scope':allocated['result']} if allocated is not None else {'allocation_request':request,'preparation':scope['prepared']['native']}
    if receipt['request']['operation']!=operation or receipt['request']['distribution']!=request['distribution'] or receipt['request']['arguments']!=arguments:
        raise ValueError('Preparation retirement changed its actual allocation')


async def read(db,row,child,scope):
    from . import finalizing_linux_scope as linux
    if linux.applies(row):return await linux.preparation_read(db,row,child,scope)
    event=await db.get(DurableEvent,event_id(child['id'],'closed'),populate_existing=True)
    cancelled=False;terminal=False
    if event is None:
        event=await db.get(DurableEvent,event_id(child['id'],'cancelled_closed'),populate_existing=True)
        cancelled=event is not None
    if event is None:
        event=await db.get(DurableEvent,event_id(child['id'],'terminal_closed'),populate_existing=True)
        terminal=event is not None
    if event is None:return None
    selected=await intent(db,row,child);value=json.loads(event.payload_json)
    fields={'schema','binding','intent_sha256','scope_sha256','business_not_released','owner_exit','observer','observed_at',
        'guardian','native','known_birth_exits','unobserved_bootstraps','unknown_process_exit_inferred'}
    if cancelled:fields=(fields-{'owner_exit'})|{'request_fence_sha256'}
    if terminal:fields=(fields-{'owner_exit'})|{'terminal_request_sha256'}
    if (event.case_id!=row.case_id or event.type!=CLOSED or selected is None or set(value)!=fields or value['schema']!=(TERMINAL_SCHEMA if terminal else CANCEL_SCHEMA if cancelled else SCHEMA)
            or value['binding']!=scopes.binding(row,child) or value['intent_sha256']!=scopes.digest(selected)
            or value['scope_sha256']!=scopes.digest(scope) or child['identity'] is not None or child['stop'] is not None
            or child['phase'] not in {'preparing','failed'} or value['business_not_released'] is not True
            or not (cancelled or terminal) and value['owner_exit']!={'state':'exited','reason':'process_exited'} or value['unknown_process_exit_inferred'] is not False
            or value['unobserved_bootstraps']!=unobserved(scope)):
        raise ValueError('Incomplete validator closure differs from its unreleased reservation')
    if scope is not None and (scope['plan']['seed']!=selected['seed'] or any(selected['implementation'].get(n)!=h for n,h in scope['plan']['code'].items())):
        raise ValueError('Preparation closure lost its original scope implementation')
    observer=identity.validate(value['observer']);owner=json.loads(row.owner_identity_json)
    stamp=datetime.fromisoformat(value['observed_at']);at=event.created_at.replace(tzinfo=timezone.utc) if event.created_at.tzinfo is None else event.created_at
    if (not (cancelled or terminal) and observer==owner) or observer['host_id']!=owner['host_id'] or stamp.utcoffset()!=timedelta(0) or stamp>at:
        raise ValueError('Invalid preparation owner-exit witness')
    if cancelled:
        from .finalizing_request_cancellation import require_fence
        fence,_=await require_fence(db,row)
        if value['request_fence_sha256']!=scopes.digest(fence):raise ValueError('Preparation closure lost its terminal cancellation fence')
    if terminal and value['terminal_request_sha256']!=terminal_digest(row):raise ValueError('Preparation closure changed its completed request')
    expected=[dict(item,exit={'state':'exited','reason':'process_exited'}) for item in births(scope)]
    if value['known_birth_exits']!=expected:raise ValueError('Preparation lacks actual exit observations for its known births')
    guardian=scope['guardian'] if scope is not None else None
    if guardian is not None:
        from .sandbox_image_guardian import validate_proof
        validate_proof({k:guardian[k] for k in ('request','preparation')},value['guardian'])
    elif value['guardian'] is not None:raise ValueError('Unknown guardian birth was replaced with a fabricated proof')
    validate_retirement(scope,value['native'])
    return value


async def wait_exit(item):
    birth=item['birth'];distribution=item['distribution']
    async with asyncio.timeout(50):
        while True:
            result=await owned_process.probe_native(distribution,birth) if distribution else await asyncio.to_thread(identity.probe,birth)
            if result=={'state':'exited','reason':'process_exited'}:return dict(item,exit=result)
            if result['state']=='mismatch' and distribution and item['role']=='preflight_native' and birth['uid']==0:
                # Only the fixed same-process drop to nobody is possible after
                # the durable native permit; a UID change is never an exit.
                candidate=dict(birth,uid=65534);result=await owned_process.probe_native(distribution,candidate)
            if result['state']!='match':raise ValueError('Known bootstrap exit could not be observed')
            await asyncio.sleep(.1)


async def recover(row,child,scope,*,cancelled=False,terminal=False):
    from .case_validation import assert_writable_locked
    before=scopes.fingerprint(row)
    if child['identity'] is not None or child['stop'] is not None:raise ValueError('Preparation closure cannot replace a registered process stop')
    async with SessionLocal() as db:
        existing=await read(db,row,child,scope)
        if existing is not None:return existing
        selected=await intent(db,row,child)
    if selected is None:raise ValueError('Historical incomplete startup has no negative release proof')
    if cancelled and terminal:raise ValueError('Preparation retirement has two owner authorities')
    if terminal:authority=terminal_digest(row)
    elif cancelled:
        from .finalizing_request_cancellation import require_fence
        async with SessionLocal() as db:fence,_=await require_fence(db,row)
    else:
        owner=json.loads(row.owner_identity_json);exited=await asyncio.to_thread(identity.probe,owner)
        if exited!={'state':'exited','reason':'process_exited'}:raise ValueError('Preparation owner is not provably exited')
    proof=None;retired=None
    if scope is not None:
        if scope['closed'] is not None:
            proof=scope['closed']['guardian'];retired=scope['closed']['native']
        else:
            if scope['guardian'] is not None:
                from .sandbox_image_guardian import recover as recover_guardian
                proof=await recover_guardian({k:scope['guardian'][k] for k in ('request','preparation')})
            if scope['prepared'] is not None:
                # Abandon must wait for the actual prepared creator to exit;
                # scope retirement separately handles an allocated kernel tree.
                if scope['allocated'] is None:
                    for item in births(scope)[:2]:await wait_exit(item)
                retired=await scopes.retirement(scope)
    observations=[await wait_exit(item) for item in births(scope)]
    value={'schema':SCHEMA,'binding':scopes.binding(row,child),'intent_sha256':scopes.digest(selected),
        'scope_sha256':scopes.digest(scope),'business_not_released':True,
        'observer':await asyncio.to_thread(identity.observe,os.getpid()),'observed_at':now().isoformat(),
        'guardian':proof,'native':retired,'known_birth_exits':observations,'unobserved_bootstraps':unobserved(scope),
        'unknown_process_exit_inferred':False}
    if terminal:value.update(schema=TERMINAL_SCHEMA,terminal_request_sha256=authority)
    elif cancelled:value.update(schema=CANCEL_SCHEMA,request_fence_sha256=scopes.digest(fence))
    else:value['owner_exit']=exited
    async with SessionLocal() as db:
        await lock_case_execution(db,row.case_id);await assert_writable_locked(db,row.case_id)
        current=await db.get(NativeRequest,row.id,populate_existing=True)
        if current is None or scopes.fingerprint(current)!=before:raise ValueError('Preparation request changed during actual retirement')
        fresh=await scopes.read(db,current,child)
        if fresh!=scope:raise ValueError('Preparation scope changed during actual retirement')
        existing=await read(db,current,child,fresh)
        if existing is not None:return existing
        await append_event(db,row.case_id,CLOSED,value,event_id=event_id(child['id'],'terminal_closed' if terminal else 'cancelled_closed' if cancelled else 'closed'))
        await db.flush();await read(db,current,child,fresh);await finish_before_cancel(db.commit())
    return value
