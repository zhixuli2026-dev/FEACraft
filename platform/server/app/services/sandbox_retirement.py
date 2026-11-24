"""Reconcile dead sandbox owners without inventing normal stop receipts.

Original process journals and exit codes remain intact. An independent durable
event binds actual observations, a verified retained snapshot, and fenced rows.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import update

from ..db import SessionLocal
from ..models import DurableEvent, NativeRequest, NativeSession, User
from . import native_request_records, terminal_records, sandbox_recovery_execution as recovery
from . import sandbox_recovery_inspection as inspection
from .async_ops import finish_before_cancel
from .case_write import assert_case_available
from .durable_events import append_event, json_text, lock_case_execution, now

SCHEMA = 'vibeflow.sandbox-retirement.v1'
EVENT = 'terminal.sandbox_retired'
MODELS = {'native_sessions':NativeSession, 'native_requests':NativeRequest}
_slots = asyncio.Semaphore(2)
COMMON = ('id','case_id','created_by','owner_id','owner_token_sha256','owner_identity_json',
          'launch_nonce','profile_json','phase','version','error','ended_at')


def stamp(value):
    if isinstance(value, datetime):return value.replace(tzinfo=timezone.utc).isoformat()
    return value


def binding(row, overrides=None):
    fields = COMMON + (('shell','process_json','stop_json','exit_code') if isinstance(row,NativeSession)
                       else ('kind','children_json','result_json','conversation_id','tool_call_id'))
    value = {name:stamp(getattr(row,name)) for name in fields}
    value['retirement_event_id'] = row.retirement_event_id
    value.update({key:stamp(item) for key,item in (overrides or {}).items()})
    return hashlib.sha256(json_text(value).encode()).hexdigest()


async def authorized(db, sid, user_id):
    from ..routers.terminal import _authorize_terminal
    await db.get(User,user_id,populate_existing=True)
    row = await db.get(NativeSession,sid,populate_existing=True)
    if row is None or row.created_by != user_id:
        raise HTTPException(404,'Terminal session not found')
    case = await _authorize_terminal(db,row.case_id,user_id)
    assert_case_available(case)
    return row,case


async def resources(db, snapshot):
    rows = [await db.get(NativeSession,snapshot.session_id,populate_existing=True)]
    for role,_,_ in snapshot.owners:
        if role.startswith('export_owner:'):
            row = await db.get(NativeRequest,role.split(':',1)[1],populate_existing=True)
            if row is None:raise ValueError('Original export disappeared')
            rows.append(row)
    return rows


def probes(snapshot, observed):
    expected = [(role,None,birth) for role,birth,_ in snapshot.owners] + list(snapshot.processes)
    checked = observed['checks']
    if len(expected) != len(checked):raise ValueError('Incomplete exit observation')
    result = []
    for (role,distro,birth),check in zip(expected,checked):
        if check['role'] != role or check['state'] != 'exited':
            raise inspection.InspectionBlocked('owner_identity_unverified' if 'owner' in role else 'process_exit_unverified')
        result.append({'role':role,'distribution':distro,'birth':birth,'observation':check})
    return result


def projected(row, event_id, at):
    if isinstance(row,NativeRequest) and row.phase=='succeeded':
        if native_request_records.public(row)['cleanup_verified'] is not True:
            raise inspection.InspectionBlocked('export_completion_unverified')
        return None
    if row.retirement_event_id is not None:
        raise inspection.InspectionBlocked('retirement_binding_changed')
    return {'phase':'cancelled' if row.phase=='cancelled' else 'failed', 'version':row.version+1,
            'error':row.error or 'OwnerExitReconciled', 'ended_at':row.ended_at or at,
            'retirement_event_id':event_id}


async def verified(db, row, cache=None):
    """Verify the committed whole group; never reinterpret an old stop journal.

    Caller must hold case control when using this to admit a new writer/validator.
    A cache is scoped to one such locked traversal, never stored on the session.
    """
    event_id = row.retirement_event_id
    if event_id is None:return None
    identity = (row.__tablename__,row.id)
    if cache is not None and event_id in cache:
        document = cache[event_id]
        if not any((entry['table'],entry['id'])==identity and entry['retired'] for entry in document['resources']):
            raise ValueError('Retirement belongs to another resource')
        return document
    event = await db.get(DurableEvent,event_id,populate_existing=True)
    if event is None or event.type!=EVENT or event.case_id!=row.case_id:
        raise ValueError('Missing retirement commit witness')
    if len(event.payload_json.encode())>256*1024:raise ValueError('Oversized retirement witness')
    document = json.loads(event.payload_json)
    if (document['schema']!=SCHEMA or document['case_id']!=row.case_id
            or document['created_by']!=row.created_by or document['archive_verified'] is not True
            or document['command_replayed'] is not False or document['project_files_published'] is not False
            or not 1<=len(document['resources'])<=inspection.MAX_EXPORTS+1):
        raise ValueError('Invalid retirement witness')
    entries=document['resources'];seen=set();actual=[]
    for index,entry in enumerate(entries):
        if entry['table'] != ('native_sessions' if index == 0 else 'native_requests'):
            raise ValueError('Retirement resources are not in canonical order')
        if type(entry['retired']) is not bool or (index == 0 and not entry['retired']):
            raise ValueError('Invalid retirement resource state')
        key=(entry['table'],entry['id'])
        if key in seen or entry['table'] not in MODELS:raise ValueError('Duplicate retirement resource')
        seen.add(key)
        resource=await db.get(MODELS[entry['table']],entry['id'],populate_existing=True)
        if (resource is None or resource.case_id!=document['case_id'] or resource.created_by!=document['created_by']
                or binding(resource)!=entry['after_sha256']
                or resource.retirement_event_id!=(event_id if entry['retired'] else None)
                or binding(resource,entry['original'])!=entry['before_sha256']):
            raise ValueError('Retired ownership record changed')
        if isinstance(resource,NativeSession):
            terminal_records.document(resource)
            if resource.id!=document['session_id'] or resource.shell!='wsl-sandbox' or resource.phase!='failed':
                raise ValueError('Retirement does not identify a failed sandbox')
        else:
            native_request_records.document(resource)
            if resource.kind!='terminal.export' or json.loads(resource.profile_json)['session_id']!=document['session_id']:
                raise ValueError('Retired export belongs to another sandbox')
            if entry['retired'] and resource.phase not in {'failed','cancelled'}:
                raise ValueError('Retirement did not fence export execution')
        actual.append(resource)
    if identity not in seen or not any((entry['table'],entry['id'])==identity and entry['retired'] for entry in entries):
        raise ValueError('Unbound retirement pointer')
    terminal=next((item for item in actual if isinstance(item,NativeSession)),None)
    if terminal is None:raise ValueError('Retirement lacks original terminal')
    # Rebuild the exact expected identities from the still-retained journals.
    expected=[('terminal_owner',None,json.loads(terminal.owner_identity_json))]
    roots=json.loads(terminal.process_json);selected=json.loads(terminal.profile_json)
    process_rows=[]
    def add(role, process):
        process_rows.append((role+'_host',None,process['host']))
        process_rows.append((role+'_native',process['native']['distribution'],process['native']['process']))
    add('terminal',roots['root']);process_rows.append(('terminal_gate',selected['distro'],roots['gate']['process']))
    for export in actual[1:]:
        expected.append(('export_owner:'+export.id,None,json.loads(export.owner_identity_json)))
        for child in native_request_records.document(export)['children'].values():
            add('export:'+export.id+':'+child['id'],child['identity'])
    expected+=process_rows
    checks=document['observations']
    if len(checks)!=len(expected):raise ValueError('Retirement omitted original identities')
    for value,(role,distro,birth) in zip(checks,expected):
        if (value['role']!=role or value['distribution']!=distro or value['birth']!=birth
                or value['observation']['role']!=role or value['observation']['state']!='exited'):
            raise ValueError('Retirement has no matching actual exit observation')
    recovered=await db.get(NativeRequest,document['recovery_id'],populate_existing=True)
    if (recovered is None or recovered.kind!='terminal.recover' or recovered.phase!='succeeded'
            or recovered.case_id!=row.case_id or recovered.created_by!=row.created_by
            or native_request_records.public(recovered)['cleanup_verified'] is not True
            or hashlib.sha256(recovered.profile_json.encode()).hexdigest()!=document['recovery_profile_sha256']
            or json.loads(recovered.result_json)!=document['receipt']):
        raise ValueError('Retirement no longer has its committed recovery snapshot')
    recovery_profile=json.loads(recovered.profile_json)
    if (recovery_profile['session_id']!=terminal.id
            or recovery_profile['fingerprint']!=document['original_fingerprint']
            or recovery_profile['source_prefix']!=document['source_prefix']
            or terminal.exit_code!=document['original_exit_code']
            or (selected['staging_prefix'] is not None)!=document['staging_retained']):
        raise ValueError('Retirement snapshot binding changed')
    recovery.receipt(document['receipt'],recovery_profile)
    if cache is not None:cache[event_id]=document
    return document


def public(document, event_id):
    return {'status':'retired','event_id':event_id,'session_id':document['session_id'],
            'recovery_id':document['recovery_id'],'observed_at':document['observed_at'],
            'process_exit_verified':True,'normal_stop_receipt_invented':False,'command_replayed':False,
            'project_files_published':False,'staging_retained':document['staging_retained'],
            'original_exit_code':document['original_exit_code'],
            'retired_exports':sum(entry['table']=='native_requests' and entry['retired'] for entry in document['resources'])}


async def status(sid,user_id):
    async with SessionLocal() as db:
        row,_=await authorized(db,sid,user_id)
        document=await verified(db,row)
        return public(document,row.retirement_event_id) if document else {'status':'not_retired','session_id':sid}


async def retire(sid,recovery_id,user_id):
    try:
        await asyncio.wait_for(_slots.acquire(), .1)
    except TimeoutError:
        raise inspection.InspectionBlocked('retirement_busy')
    try:
        return await _retire(sid,recovery_id,user_id)
    finally:
        _slots.release()


async def _retire(sid,recovery_id,user_id):
    async with SessionLocal() as db:
        row,_=await authorized(db,sid,user_id)
        previous=await verified(db,row)
        if previous:
            if previous['recovery_id']!=recovery_id:raise inspection.InspectionBlocked('retirement_already_recorded')
            return public(previous,row.retirement_event_id)
        if row.phase=='closed':raise inspection.InspectionBlocked('session_already_closed')
    value,selected=await recovery.load(sid,recovery_id,user_id)
    if value['phase']!='succeeded' or value['cleanup_verified'] is not True:
        raise inspection.InspectionBlocked('recovery_not_complete')
    # This is an actual whole-archive read, not trust in a DB success label.
    def verify_archive():
        with recovery.download(selected,value['receipt']):pass
    await finish_before_cancel(asyncio.to_thread(verify_archive))
    original,prefix,before=await recovery.capture(sid,user_id)
    if selected['fingerprint']!=before.fingerprint or selected['source_prefix']!=prefix:
        raise inspection.InspectionBlocked('recovery_source_registration_changed')
    observed=await inspection.observe(before)
    if observed['status']!='processes_exited':raise inspection.InspectionBlocked(observed['reason'])
    observations=probes(before,observed)
    async with SessionLocal() as db:
        await lock_case_execution(db,original.case_id)
        from .case_validation import assert_writable_locked
        await assert_writable_locked(db,original.case_id)
        row,case=await authorized(db,sid,user_id)
        previous=await verified(db,row)
        if previous:
            if previous['recovery_id']!=recovery_id:raise inspection.InspectionBlocked('retirement_already_recorded')
            return public(previous,row.retirement_event_id)
        current=await inspection.capture(db,row,case.s3_prefix)
        if current.fingerprint!=before.fingerprint:raise inspection.InspectionBlocked('records_changed_during_inspection')
        recovered=await db.get(NativeRequest,recovery_id,populate_existing=True)
        if (recovered is None or recovered.phase!='succeeded' or recovered.kind!='terminal.recover'
                or recovered.case_id!=case.id or recovered.created_by!=user_id
                or json.loads(recovered.profile_json)!=selected or json.loads(recovered.result_json)!=value['receipt']
                or native_request_records.public(recovered)['cleanup_verified'] is not True):
            raise inspection.InspectionBlocked('recovery_evidence_changed')
        rows=await resources(db,current)
        event_id=str(uuid.uuid4());at=now();entries=[];updates=[]
        for resource in rows:
            after=projected(resource,event_id,at)
            old={key:stamp(getattr(resource,key)) for key in (after or {})}
            entries.append({'table':resource.__tablename__,'id':resource.id,'retired':after is not None,
                'original':old,'before_sha256':binding(resource),'after_sha256':binding(resource,after)})
            if after:updates.append((resource,after))
        document={'schema':SCHEMA,'case_id':case.id,'created_by':user_id,'session_id':sid,'recovery_id':recovery_id,
            'observed_at':observed['observed_at'],'original_fingerprint':current.fingerprint,
            'original_exit_code':row.exit_code,'source_prefix':prefix,'archive_verified':True,
            'staging_retained':json.loads(row.profile_json)['staging_prefix'] is not None,
            'command_replayed':False,'project_files_published':False,'resources':entries,'observations':observations,
            'receipt':value['receipt'],'recovery_profile_sha256':hashlib.sha256(recovered.profile_json.encode()).hexdigest()}
        await append_event(db,case.id,EVENT,document,event_id=event_id)
        for resource,after in updates:
            model=type(resource)
            changed=await db.execute(update(model).where(model.id==resource.id,model.version==resource.version,
                model.retirement_event_id.is_(None)).values(**after).execution_options(synchronize_session=False))
            if changed.rowcount!=1:raise inspection.InspectionBlocked('records_changed_during_inspection')
        await db.flush()
        row=await db.get(NativeSession,sid,populate_existing=True)
        await verified(db,row)
        await finish_before_cancel(db.commit())
        return public(document,event_id)
