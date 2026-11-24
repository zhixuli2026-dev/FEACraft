"""Durable preparation observations for the fixed image-cleanup launch."""
import hashlib
import json
import re
import uuid

from ..db import SessionLocal
from ..models import DurableEvent
from . import native_request_records as records, owned_process, process_preparation
from .durable_events import append_event, json_text

SCHEMA = 'vibeflow.sandbox-image-launch.v1'
INTENT = 'terminal.image_launch_intended'
OBSERVED = 'terminal.image_launch_observed'
UNSTARTED = 'terminal.image_launch_unstarted'
UNSTARTED_SCHEMA = 'vibeflow.sandbox-image-unstarted.v1'
STAGES = process_preparation.STAGES
HOST_FILES = ('owned_process.py', 'windows_process_create.py', 'windows_process_scope.py',
              'windows_process_lifetime.py', 'async_ops.py')
GUARDED_HOST_FILES = (*HOST_FILES, 'windows_scope_guard.py', 'windows_scope_guardian.py')
PREFLIGHT_HOST_FILES = (*GUARDED_HOST_FILES, 'native_process_preflight.py')
IMPLEMENTATION_FIELDS = {'host_creation', 'host_launch_sha256', 'native_scope_helpers'}


def implementations():
    from .native_scope_controller import FILES
    if getattr(owned_process, 'WINDOWS_CREATION', None) != 'job_list_v1':
        raise ValueError('Loaded host launcher does not provide atomic Job creation')
    def hashes(names):
        return {name: hashlib.sha256(owned_process.GATE.with_name(name).read_bytes()).hexdigest() for name in names}
    return {'host_creation': 'job_list_v1', 'host_launch_sha256': hashes(PREFLIGHT_HOST_FILES),
            'native_scope_helpers': hashes(FILES)}


def validate_implementations(intent):
    from .native_scope_controller import FILES
    if not IMPLEMENTATION_FIELDS.intersection(intent):
        return  # Historical intent: this is not an atomic-launch attestation.
    if not IMPLEMENTATION_FIELDS.issubset(intent) or intent['host_creation'] != 'job_list_v1':
        raise ValueError('Incomplete or unsupported launch implementation binding')
    for key, names in (('host_launch_sha256', HOST_FILES), ('native_scope_helpers', FILES)):
        values = intent[key]
        allowed = (set(names), set(GUARDED_HOST_FILES),set(PREFLIGHT_HOST_FILES)) if key=='host_launch_sha256' else (set(names),)
        if not isinstance(values, dict) or set(values) not in allowed:
            raise ValueError('Launch implementation files differ')
        if any(not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None for value in values.values()):
            raise ValueError('Invalid launch implementation digest')


def require_implementation(intent):
    validate_implementations(intent)
    if 'host_creation' in intent and any(intent[key] != value for key, value in implementations().items()):
        raise ValueError('Launch implementation changed after durable intent')


def event_id(child_id, stage):
    return str(uuid.uuid5(uuid.UUID(child_id), SCHEMA + ':' + stage))


def binding(row, child):
    if row.kind != 'terminal.cleanup':
        raise ValueError('Image launch evidence requires a cleanup request')
    selected = json.loads(row.profile_json)
    return {'schema':SCHEMA, 'request_id':row.id, 'case_id':row.case_id, 'created_by':row.created_by,
        'owner_id':row.owner_id, 'owner_identity_sha256':hashlib.sha256(row.owner_identity_json.encode()).hexdigest(),
        'child_id':child['id'], 'parent_launch_nonce':row.launch_nonce,
        'request_sha256':child['request_sha256'], 'purpose':child['purpose'], 'caller':child['caller'],
        'runtime':'wsl', 'distribution':selected['distro']}


async def evidence(db, row, child):
    values=[]
    for stage in ('intent', *STAGES):
        event=await db.get(DurableEvent,event_id(child['id'],stage),populate_existing=True)
        if event is None:
            values.append(None)
            continue
        if event.case_id!=row.case_id or event.type!=(INTENT if stage=='intent' else OBSERVED):
            raise ValueError('Image startup event belongs to another execution')
        if len(event.payload_json.encode())>16384:
            raise ValueError('Image startup evidence exceeds bound')
        values.append(json.loads(event.payload_json))
    intent=values[0]
    if intent is None:
        if any(value is not None for value in values):raise ValueError('Missing image launch intent')
        return None, []
    expected=binding(row,child)
    legacy=set(expected)|{'launch_nonce','gate_sha256','identity_probe_sha256'}
    if set(intent) not in (legacy, legacy|IMPLEMENTATION_FIELDS, legacy|IMPLEMENTATION_FIELDS|{'scope_plan_sha256'}) or any(intent[k]!=v for k,v in expected.items()):
        raise ValueError('Image startup differs from original reservation')
    if 'scope_plan_sha256' in intent and (not isinstance(intent['scope_plan_sha256'],str)
            or re.fullmatch('[0-9a-f]{64}',intent['scope_plan_sha256']) is None):
        raise ValueError('Invalid image scope plan digest')
    validate_implementations(intent)
    observations=[];missing=False
    for value in values[1:]:
        if value is None:missing=True;continue
        if missing or set(value)!={'request_id','child_id','preparation'} or value['request_id']!=row.id or value['child_id']!=child['id']:
            raise ValueError('Image startup observations are incomplete or reordered')
        observations.append(value['preparation'])
    process_preparation.chain(observations,intent)
    return intent,observations


async def reserve(owner, child_id, process, *, scope_preparation=None):
    from .case_validation import admit_session_locked
    from ..models import NativeRequest
    async with SessionLocal() as db:
        await admit_session_locked(db,NativeRequest,owner.id)
        row=await records._current(db,owner,owner.nonce,launch=True)
        child=records.document(row)['children'][child_id]
        await require_unsealed(db,child)
        if child['phase']!='preparing' or (await evidence(db,row,child))[0] is not None:
            raise ValueError('Image child already has an intended launch')
        intent={**binding(row,child),'launch_nonce':process.nonce,
            'gate_sha256':hashlib.sha256(owned_process.GATE.read_bytes()).hexdigest(),
            'identity_probe_sha256':hashlib.sha256(owned_process.PROBE.read_bytes()).hexdigest(), **implementations()}
        if scope_preparation is not None:
            from . import sandbox_image_scope as scopes, native_scope_controller as protocol
            await scopes.prepared(db,row,child,scope_preparation)
            intent['scope_plan_sha256']=protocol.digest(scope_preparation['request'])
            if (process.nonce!=scope_preparation['native']['reservation']['nonce']
                    or intent['native_scope_helpers']!=scope_preparation['request']['helpers']):
                raise ValueError('Prepared scope changed before launch intent')
        await append_event(db,row.case_id,INTENT,intent,event_id=event_id(child_id,'intent'))
        await db.commit()


async def observed(owner, child_id, value):
    from .case_validation import admit_session_locked
    from ..models import NativeRequest
    async with SessionLocal() as db:
        await admit_session_locked(db,NativeRequest,owner.id)
        row=await records._current(db,owner,owner.nonce,launch=True)
        child=records.document(row)['children'][child_id]
        if child['phase']!='preparing':raise ValueError('Image child is no longer preparing')
        intent,previous=await evidence(db,row,child)
        require_implementation(intent)
        from .sandbox_image_scope import require_allocated
        await require_allocated(db,row,child)
        process_preparation.chain([*previous,value],intent)
        await append_event(db,row.case_id,OBSERVED,{'request_id':row.id,'child_id':child_id,'preparation':value},
            event_id=event_id(child_id,value['stage']))
        await db.commit()


async def require_ready(db, row, child, identity):
    intent,values=await evidence(db,row,child)
    if not values:raise ValueError('Image cleanup lacks durable startup observations')
    require_implementation(intent)
    from .sandbox_image_scope import require_allocated
    await require_allocated(db,row,child)
    process_preparation.matches_record(values[-1],identity)


async def incomplete_births(db, row, child):
    """Only stages with a complete known process set can prove exit."""
    unstarted=await unstarted_evidence(db,row,child)
    if unstarted is not None:return [],hashlib.sha256(json_text(unstarted).encode()).hexdigest()
    intent,values=await evidence(db,row,child)
    from .sandbox_image_scope import guarded_births
    guarded=await guarded_births(db,row,child)
    if guarded is not None:return guarded
    if not values:
        from .sandbox_image_scope import unadmitted_births
        unadmitted=await unadmitted_births(db,row,child)
        if unadmitted is not None:return unadmitted
    if not values or values[-1]['stage']=='host_ready':
        raise ValueError('Image cleanup startup still has an unknown process identity')
    last=values[-1]
    # Historical paths: created commits before host_ready and the first host
    # release. With no host_ready event, no WSL child could have been released.
    # host_ready alone is ambiguous and remains blocked; native_ready has both
    # actual births. Neither branch manufactures a normal process/stop record.
    digest=hashlib.sha256(json_text({'intent':intent,'observations':values}).encode()).hexdigest()
    births=[('cleanup_host:'+child['id'],None,last['host'])]
    if last['native'] is not None:
        births.append(('cleanup_native:'+child['id'],last['native']['distribution'],last['native']['process']))
    return births,digest


async def require_unsealed(db,child):
    if await db.get(DurableEvent,event_id(child['id'],'unstarted')) is not None:
        raise ValueError('Image reservation was sealed without starting a process')


async def unstarted_evidence(db,row,child):
    event=await db.get(DurableEvent,event_id(child['id'],'unstarted'),populate_existing=True)
    if event is None:return None
    if event.case_id!=row.case_id or event.type!=UNSTARTED or len(event.payload_json.encode())>16384:
        raise ValueError('Invalid unstarted image reservation evidence')
    value=json.loads(event.payload_json)
    if (set(value)!={'schema','binding','launch_nonce','implementation'} or value['schema']!=UNSTARTED_SCHEMA
            or value['binding']!=binding(row,child) or not isinstance(value['launch_nonce'],str)
            or re.fullmatch('[0-9a-f]{64}',value['launch_nonce']) is None):
        raise ValueError('Unstarted image reservation differs from its owner')
    hashes=value['implementation']
    if (not isinstance(hashes,dict) or set(hashes)!={'native_execution.py','native_request_execution.py','sandbox_image_launch.py'}
            or any(not isinstance(v,str) or re.fullmatch('[0-9a-f]{64}',v) is None for v in hashes.values())):
        raise ValueError('Unstarted image reservation lacks implementation binding')
    from .sandbox_image_scope import evidence as scope_evidence
    intent,observations=await evidence(db,row,child)
    if (child['identity'] is not None or child['stop'] is not None or intent is not None or observations
            or await scope_evidence(db,row,child) is not None):
        raise ValueError('Unstarted image evidence conflicts with actual preparation')
    return value


async def seal_unstarted(owner,child_id,process):
    """Attest only an actually unused retained object, before closing its owner."""
    if any(getattr(process,name) is not None for name in ('proc','scope','guardian','record','native','native_scope','preparation')):
        return
    from .sandbox_image_scope import evidence as scope_evidence
    async with SessionLocal() as db:
        row=await records._current(db,owner,owner.nonce)
        child=records.document(row)['children'][child_id]
        intent,observations=await evidence(db,row,child)
        if intent is not None or observations or await scope_evidence(db,row,child) is not None:return
        if child['phase']!='preparing' or child['identity'] is not None or child['stop'] is not None:
            raise ValueError('Only an unused image reservation may be sealed')
        value={'schema':UNSTARTED_SCHEMA,'binding':binding(row,child),'launch_nonce':process.nonce,
            'implementation':{name:hashlib.sha256(owned_process.GATE.with_name(name).read_bytes()).hexdigest()
                for name in ('native_execution.py','native_request_execution.py','sandbox_image_launch.py')}}
        previous=await unstarted_evidence(db,row,child)
        if previous is not None:
            if previous!=value:raise ValueError('Unstarted image reservation already differs')
            return
        await append_event(db,row.case_id,UNSTARTED,value,event_id=event_id(child_id,'unstarted'))
        await db.commit()
