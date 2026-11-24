"""Durably admitted, registered native removal of a retired sandbox image."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import uuid

from sqlalchemy import select

from .. import storage
from ..db import SessionLocal
from ..models import DurableEvent, NativeRequest
from . import sandbox_retirement as retirement, sandbox_recovery_execution as recovery
from . import sandbox_image_cleanup_native as native, native_request_records as records
from .async_ops import finish_before_cancel
from .attempt_workspace import ordinary
from .durable_events import append_event, json_text, lock_case_execution
from .owned_process import wsl_path
from .sandbox_export_execution import read

SCHEMA=native.SCHEMA
INTENT='terminal.image_cleanup_started'
DONE='terminal.image_cleanup_completed'
DIRECTORY='.terminal-image-cleanup'
NATIVE=Path(native.__file__).resolve()


def event_id(sid,kind):return str(uuid.uuid5(uuid.UUID(sid),SCHEMA+':'+kind))


def profile(value):
    fields={'session_id','recovery_id','intent_id','root','image_root','distro','nonce','disk','image_sha256','processes','code'}
    if not isinstance(value,dict) or set(value)!=fields:raise ValueError('Invalid image cleanup profile')
    for key in ('session_id','recovery_id','intent_id'):
        if str(uuid.UUID(value[key]))!=value[key]:raise ValueError('Invalid image cleanup identity')
    for key in ('nonce','image_sha256'):
        if not isinstance(value[key],str) or not re.fullmatch('[0-9a-f]{64}',value[key]):raise ValueError('Invalid cleanup digest')
    if not isinstance(value['distro'],str) or not re.fullmatch('[a-z0-9-]+',value['distro']):raise ValueError('Invalid cleanup distribution')
    parent=storage._fs_root().resolve()
    for key,directory in (('root',DIRECTORY),('image_root','.terminal-export')):
        path=Path(value[key])
        if path.parent!=parent/directory or not re.fullmatch('[0-9a-f]{32}',path.name):raise ValueError('Cleanup path outside private storage')
        ordinary(path.parent);ordinary(path)
    if (not isinstance(value['code'],dict) or set(value['code'])!=set(native.CODE)
            or any(not re.fullmatch('[0-9a-f]{64}',sha) for sha in value['code'].values())):raise ValueError('Invalid cleanup implementation binding')
    from .process_identity_native import validate
    if not isinstance(value['processes'],list) or not 1<=len(value['processes'])<=256:raise ValueError('Invalid original process set')
    for birth in value['processes']:
        if validate(birth)['platform']!='linux':raise ValueError('Cleanup requires native process births')
    return value


def settings(selected):
    return {'schema':SCHEMA,**{key:selected[key] for key in ('session_id','recovery_id','intent_id','nonce','disk','image_sha256','processes','code')},
        'root':wsl_path(selected['root']),'image_root':wsl_path(selected['image_root'])}


def command(selected,parameters):
    profile(selected)
    if parameters!={'session_id':selected['session_id'],'distro':selected['distro']} or selected['code']!=native.code():
        raise ValueError('Image cleanup invocation changed')
    path=Path(selected['root'])/'settings.json';raw=read(path.parent,path.name,native.MAX_CONFIG)
    if json.loads(raw)!=settings(selected):raise ValueError('Cleanup settings differ from durable profile')
    return ['wsl.exe','-d',selected['distro'],'--exec','python3','-I','-S','-u',wsl_path(NATIVE),
        '--config',wsl_path(path),'--sha256',hashlib.sha256(raw).hexdigest()],210,65536


def receipt(value,selected):
    fields={'schema','session_id','recovery_id','intent_id','nonce','image_sha256','image_absent',
            'deleted_this_request','image_verified','observations','outer_gate'}
    if (not isinstance(value,dict) or set(value)!=fields or value['schema']!=SCHEMA
            or any(value[key]!=selected[key] for key in ('session_id','recovery_id','intent_id','nonce','image_sha256'))
            or value['image_absent'] is not True or type(value['deleted_this_request']) is not bool
            or value['image_verified'] is not value['deleted_this_request']):raise ValueError('Invalid image cleanup receipt')
    from .process_identity_native import validate
    if validate(value['outer_gate'])['platform']!='linux':raise ValueError('Missing outer cleanup gate identity')
    checks=value['observations']
    if len(checks)!=len(selected['processes']):raise ValueError('Incomplete original exit observations')
    for item,birth in zip(checks,selected['processes']):
        if item['birth']!=birth or item['observation']['state']!='exited':raise ValueError('Original exit was not observed')
    return value


async def assert_recovery_allowed(db,sid):
    if await db.get(DurableEvent,event_id(sid,'intent')) is not None:
        raise recovery.inspection.InspectionBlocked('image_cleanup_started')


async def evidence(db,sid,user_id):
    row,case=await retirement.authorized(db,sid,user_id)
    witness=await retirement.verified(db,row)
    if witness is None:raise ValueError('Image cleanup requires retired original records')
    recovered=await db.get(NativeRequest,witness['recovery_id'],populate_existing=True)
    selected=recovery.snapshot_profile(json.loads(recovered.profile_json))
    if case.s3_prefix!=selected['source_prefix']:raise ValueError('Case source changed')
    return row,case,witness,selected


async def readers(db,row,selected,exclude=None):
    # Include malformed profiles in the rejection path rather than silently
    # hiding an unknown reader. Bound the scan before considering any deletion.
    values=list(await db.scalars(select(NativeRequest).where(NativeRequest.case_id==row.case_id,
        NativeRequest.kind.in_(['terminal.recover','terminal.cleanup'])).order_by(NativeRequest.id).limit(129)))
    if len(values)>128:raise ValueError('Image reader history exceeds inspection bound')
    births=list(selected['processes'])
    for request in values:
        current=json.loads(request.profile_json)
        if not current.get('session_id') or not current.get('image_root'):raise ValueError('Unbound image reader')
        if current['session_id']!=row.id and current['image_root']!=selected['image_root']:continue
        if request.id==exclude:continue
        if current['session_id']!=row.id or current['image_root']!=selected['image_root']:
            raise ValueError('Image reader binding changed')
        journal=records.document(request)
        from .sandbox_image_scope import require_closed
        await require_closed(db,request)
        from .sandbox_image_reconciliation import verified as reconciled
        independent=await reconciled(db,request)
        if not independent and request.phase not in {'succeeded','failed','cancelled'}:raise ValueError('Image reader is active or requires reconciliation')
        for child in journal['children'].values():
            if independent and child['identity'] is None:
                for observation in independent['observations']:
                    if observation['role']=='cleanup_native:'+child['id']:
                        if observation['distribution']!=selected['distro']:raise ValueError('Reader distribution changed')
                        if observation['birth'] not in births:births.append(observation['birth'])
                continue
            if child['identity'] is None or (not independent and (child['stop'] is None or child['stop']['verified'] is not True)):
                raise ValueError('Image reader has no verified stop')
            process=child['identity']['native']
            if process is None or process['distribution']!=selected['distro']:raise ValueError('Reader distribution changed')
            if process['process'] not in births:births.append(process['process'])
    return births


async def committed(db,row,witness,selected):
    value=await db.get(DurableEvent,event_id(row.id,'intent'),populate_existing=True)
    if value is None:return None
    document=json.loads(value.payload_json)
    expected={'schema':SCHEMA,'session_id':row.id,'case_id':row.case_id,'created_by':row.created_by,
        'recovery_id':witness['recovery_id'],'retirement_event_id':row.retirement_event_id,
        'image_root':selected['image_root'],'disk':selected['disk'],'image_sha256':witness['receipt']['image_sha256'],
        'distro':selected['distro']}
    if value.type!=INTENT or value.case_id!=row.case_id or document!=expected:raise ValueError('Image cleanup intent changed')
    return document


async def check_session(db,case_id,user_id,selected,request_id=None):
    profile(selected)
    row,case,witness,snapshot=await evidence(db,selected['session_id'],user_id)
    document=await committed(db,row,witness,snapshot)
    if document is None or case.id!=case_id or selected['intent_id']!=event_id(row.id,'intent'):
        raise ValueError('Image cleanup has no matching committed intent')
    if await db.get(DurableEvent,event_id(row.id,'done')) is not None:raise ValueError('Image cleanup is already complete')
    for key in ('session_id','recovery_id','image_root','disk','image_sha256','distro'):
        if selected[key]!=document[key]:raise ValueError('Cleanup execution differs from committed intent')
    if selected['processes']!=await readers(db,row,snapshot,exclude=request_id):raise ValueError('Image readers changed')


def completed_file(selected):
    return receipt(json.loads(read(Path(selected['root']),'complete.json',native.MAX_CONFIG)),selected)


async def completed(db,row,document):
    value=await db.get(DurableEvent,event_id(row.id,'done'),populate_existing=True)
    if value is None:return None
    result=json.loads(value.payload_json)
    if value.type!=DONE or value.case_id!=row.case_id or result['intent']!=document:raise ValueError('Invalid image cleanup completion')
    request=await db.get(NativeRequest,result['request_id'],populate_existing=True)
    if (request is None or request.kind!='terminal.cleanup' or request.case_id!=row.case_id or request.created_by!=row.created_by
            or request.phase!='succeeded' or records.public(request)['cleanup_verified'] is not True):raise ValueError('Native cleanup is not complete')
    selected=profile(json.loads(request.profile_json));proof=receipt(json.loads(request.result_json),selected)
    if selected['session_id']!=row.id or selected['intent_id']!=event_id(row.id,'intent') or proof!=result['receipt']:
        raise ValueError('Cleanup completion has a different native receipt')
    for key in ('session_id','recovery_id','image_root','disk','image_sha256','distro'):
        if selected[key]!=document[key]:raise ValueError('Completed cleanup profile differs from intent')
    children=records.document(request)['children']
    if len(children)!=1 or next(iter(children.values()))['identity']['native']['process']!=proof['outer_gate']:
        raise ValueError('Cleanup receipt is not bound to its registered outer gate')
    if os.path.lexists(Path(document['image_root'])/'volume.ext4'):raise ValueError('Image reappeared after cleanup')
    _,_,_,snapshot=await evidence(db,row.id,row.created_by)
    await readers(db,row,snapshot)
    return result


def public(document,result):
    return {'status':'completed' if result else 'pending','session_id':document['session_id'],
        'recovery_id':document['recovery_id'],'image_absent':bool(result),'snapshot_retained':True,
        'metadata_retained':True,'deleted_this_request':result['receipt']['deleted_this_request'] if result else None,
        'request_id':result['request_id'] if result else None}


async def require_finished(db,row,witness):
    if await db.get(DurableEvent,event_id(row.id,'intent')) is None:return
    _,_,_,snapshot=await evidence(db,row.id,row.created_by)
    document=await committed(db,row,witness,snapshot)
    if not await completed(db,row,document):raise ValueError('Image cleanup has not finished')


async def status(sid,user_id):
    async with SessionLocal() as db:
        row,_,witness,snapshot=await evidence(db,sid,user_id)
        document=await committed(db,row,witness,snapshot)
        return public(document,await completed(db,row,document)) if document else {'status':'not_started','session_id':sid}


async def clean(sid,recovery_id,user_id):
    from . import request_deadline
    async with request_deadline.scope(request_deadline.IMAGE_CLEANUP_SECONDS) as deadline:
        return await _clean(sid,recovery_id,user_id,deadline)


async def _clean(sid,recovery_id,user_id,deadline):
    from .case_validation import assert_writable_locked
    from .native_request_execution import scope
    from .native_execution_client import run_native, ChildExecutionError
    from . import node_storage_budget as budget
    from .sandbox_image_reconciliation import reconcile
    await reconcile(sid,recovery_id,user_id)
    deadline.check()
    async with SessionLocal() as db:
        row,_,checked_witness,checked_snapshot=await evidence(db,sid,user_id);case_id=row.case_id
        document=await committed(db,row,checked_witness,checked_snapshot)
        if document:
            result=await completed(db,row,document)
            if result:return public(document,result)
    def archive():
        with recovery.download(checked_snapshot,checked_witness['receipt']):pass
    # Archive I/O can drain after cancellation without retaining a case SQL
    # write lock. Recheck the exact durable selection under that lock below.
    deadline.check()
    await finish_before_cancel(asyncio.to_thread(archive))
    deadline.check()
    async with SessionLocal() as db:
        await lock_case_execution(db,case_id);await assert_writable_locked(db,case_id)
        row,case,witness,snapshot=await evidence(db,sid,user_id)
        if witness!=checked_witness or snapshot!=checked_snapshot:
            raise ValueError('Cleanup snapshot binding changed during archive verification')
        if witness['recovery_id']!=recovery_id:raise ValueError('Cleanup must retain the retirement snapshot')
        document=await committed(db,row,witness,snapshot)
        if document:
            result=await completed(db,row,document)
            if result:return public(document,result)
        processes=await readers(db,row,snapshot)
        deadline.check()
        if document is None:
            document={'schema':SCHEMA,'session_id':sid,'case_id':case_id,'created_by':user_id,'recovery_id':recovery_id,
                'retirement_event_id':row.retirement_event_id,'image_root':snapshot['image_root'],'disk':snapshot['disk'],
                'image_sha256':witness['receipt']['image_sha256'],'distro':snapshot['distro']}
            await append_event(db,case_id,INTENT,document,event_id=event_id(sid,'intent'))
            deadline.check()
            await finish_before_cancel(db.commit())
    claim=None;owner=None;execution_started=False;failure=None
    async def admit():
        nonlocal claim
        claim=await asyncio.to_thread(budget._admit,3*native.MAX_CONFIG)
    async def reserve_owner(selected):
        nonlocal owner
        owner=await records.create(case_id,user_id,'terminal.cleanup',selected)
    try:
        deadline.check()
        await finish_before_cancel(admit())
        deadline.check()
        selected={key:document[key] for key in ('session_id','recovery_id','image_root','disk','image_sha256','distro')}
        selected.update(intent_id=event_id(sid,'intent'),nonce=secrets.token_hex(32),code=native.code(),processes=processes)
        def prepare():
            parent=storage._fs_root().resolve()/DIRECTORY;parent.mkdir(exist_ok=True);ordinary(parent)
            root=parent/uuid.uuid4().hex;root.mkdir(exist_ok=False);selected['root']=str(root)
            profile(selected)
            native.module('sandbox_export_native').write(root,'settings.json',json_text(settings(selected)).encode())
        await finish_before_cancel(asyncio.to_thread(prepare))
        deadline.check()
        await finish_before_cancel(reserve_owner(selected))
        deadline.check()
        async def before_release():
            deadline.check()
            await finish_before_cancel(asyncio.to_thread(archive))
            deadline.check()
        def run():
            result=run_native('sandbox_image_cleanup',{'session_id':sid,'distro':selected['distro']},timeout=210,capture=True)
            if result.returncode:raise ChildExecutionError('Registered image cleanup failed')
        execution_started=True
        async with scope(case_id,user_id,selected['root'],'terminal.cleanup',selected,owner=owner,before_release=before_release) as executor:
            await executor.run_sync(run,timeout=300)
            deadline.check()
            executor.result_receipt=await finish_before_cancel(asyncio.to_thread(completed_file,selected))
        deadline.check()
        async with SessionLocal() as db:
            await lock_case_execution(db,case_id);await assert_writable_locked(db,case_id)
            row,_,witness,snapshot=await evidence(db,sid,user_id)
            current=await committed(db,row,witness,snapshot)
            if current!=document:raise ValueError('Cleanup intent changed before completion')
            existing=await completed(db,row,document)
            if existing:return public(document,existing)
            await readers(db,row,snapshot,exclude=executor.owner.id)
            deadline.check()
            result={'intent':document,'request_id':executor.owner.id,'receipt':executor.result_receipt}
            await append_event(db,case_id,DONE,result,event_id=event_id(sid,'done'))
            await db.flush();await completed(db,row,document)
            deadline.check()
            await finish_before_cancel(db.commit())
            return public(document,result)
    except BaseException as error:
        failure=error
        raise
    finally:
        if owner is not None and not execution_started:
            await finish_before_cancel(records.complete(owner,
                'TimeoutError' if deadline.expired else type(failure).__name__,
                cancelled=isinstance(failure,asyncio.CancelledError) and not deadline.expired))
        async def release_if_drained():
            if claim is None:return
            if owner is not None:
                try:
                    async with SessionLocal() as db:
                        request=await db.get(NativeRequest,owner.id)
                        journal=records.document(request)
                        from .sandbox_image_scope import require_closed
                        await require_closed(db,request)
                        if any(child['stop'] is None or child['stop']['verified'] is not True for child in journal['children'].values()):
                            from .sandbox_image_reconciliation import terminal_drain_observed
                            if not await terminal_drain_observed(db,request):return
                except Exception:return
            await asyncio.to_thread(budget._release,*claim)
        await finish_before_cancel(release_if_drained())
