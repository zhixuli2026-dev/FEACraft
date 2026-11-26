"""Durable rendering ownership. Historical reads never launch or signal a PID."""
import asyncio
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path,PurePosixPath,PureWindowsPath
import re
import secrets

from sqlalchemy import or_, select, update

from ..db import SessionLocal
from ..models import NativeVizSession
from . import process_identity_native as identity,process_record,process_stop
from .native_compute_v1 import validate_policy
from .durable_events import now,json_text
from .native_request_records import Owner
from .terminal_records import verify_root
from .workflow_state import ExecutionConflict

LEASE_SECONDS=30
READY_PREFIX=b'VIBEFLOW_VIZ_READY_V1 '
LEGACY_CODE_NAMES={'viz_kernel','result_readers','colormap','animation','process_identity_native'}
TRAME_PREVIOUS_CODE_NAMES=LEGACY_CODE_NAMES | {'viz_filters'}
CODE_NAMES=TRAME_PREVIOUS_CODE_NAMES | {'viz_memory'}
PARAVIEW_CODE_NAMES={'viz_paraview_kernel','process_identity_native','paraview'}


def digest(value):return hashlib.sha256(json_text(value).encode('utf8')).hexdigest()


def profile(value):
    keys={'nonce','python','case_root','source_prefix','staging_prefix','run_dir','file_path','input_path','input_fingerprint','port','code'}
    if not isinstance(value,dict) or set(value)-{'engine','linux_compute_policy'}!=keys:raise ValueError('Invalid visualization profile')
    if 'linux_compute_policy' in value and not any(validate_policy(value['linux_compute_policy']).values()):
        raise ValueError('Visualization compute policy requires a limit')
    engine=value.get('engine','trame')
    if engine not in ('trame','paraview'):raise ValueError('Invalid visualization engine')
    if not isinstance(value['nonce'],str) or not re.fullmatch('[0-9a-f]{64}',value['nonce']):raise ValueError('Invalid visualization nonce')
    for key in ('python','case_root','input_path'):
        if not isinstance(value[key],str) or '\0' in value[key] or not Path(value[key]).is_absolute():raise ValueError('Invalid visualization path')
    target=Path(value['input_path']);root=Path(value['case_root'])
    if root not in target.parents:raise ValueError('Visualization input escapes its case root')
    for key in ('run_dir','file_path'):
        text=value[key]
        if key=='file_path' and text is None:continue
        if (not isinstance(text,str) or not text or '\0' in text or '\\' in text or PurePosixPath(text).is_absolute()
                or PureWindowsPath(text).drive or '..' in PurePosixPath(text).parts):raise ValueError('Invalid visualization relative path')
    for key in ('source_prefix','staging_prefix'):
        text=value[key]
        if key=='staging_prefix' and text is None:continue
        if not isinstance(text,str) or not text or len(text)>4096 or '\0' in text:raise ValueError('Invalid visualization source')
    if type(value['port']) is not int or not 1<=value['port']<=65535:raise ValueError('Invalid visualization port')
    stamp=value['input_fingerprint']
    if not isinstance(stamp,dict) or set(stamp)!={'size','mtime_ns'} or any(type(x) is not int or x<0 for x in stamp.values()):raise ValueError('Invalid visualization input fingerprint')
    code=value['code']
    allowed=(PARAVIEW_CODE_NAMES,) if engine=='paraview' else (LEGACY_CODE_NAMES,TRAME_PREVIOUS_CODE_NAMES,CODE_NAMES)
    if (not isinstance(code,dict) or set(code) not in allowed
            or any(not isinstance(x,str) or not re.fullmatch('[0-9a-f]{64}',x) for x in code.values())):
        raise ValueError('Invalid visualization implementation')
    return value


def ready_document(value,selected):
    if not isinstance(value,dict) or set(value)!={'schema','nonce','profile_sha256','port','process'}:raise ValueError('Invalid visualization readiness record')
    identity.validate(value['process'])
    if (value['schema']!='vibeflow.viz-ready.v1' or value['nonce']!=selected['nonce'] or value['profile_sha256']!=digest(selected)
            or type(value['port']) is not int or value['port']!=selected['port']):raise ValueError('Visualization readiness crosses its selected launch')
    return value


def document(row):
    selected=profile(json.loads(row.profile_json));identity.validate(json.loads(row.owner_identity_json))
    if selected['nonce']!=row.launch_nonce:raise ValueError('Visualization profile crosses its session')
    values=[json.loads(value) for value in (row.process_json,row.ready_json,row.stop_json)]
    if any(not isinstance(value,dict) for value in values):raise ValueError('Invalid visualization evidence document')
    root,ready,stop=(value or None for value in values)
    if root is not None:
        process_record.validate(root,bound=False)
        if root['launch_nonce']!=row.launch_nonce or root['runtime']!='windows' or root['native'] is not None:raise ValueError('Visualization root crosses its session')
    if ready is not None:
        if root is None:raise ValueError('Visualization readiness has no root')
        ready_document(ready,selected)
        if ready['process']['platform']!=root['host']['platform']:raise ValueError('Visualization readiness crosses its runtime')
    if stop is not None:
        if root is None:raise ValueError('Visualization cleanup has no root')
        process_stop.validate(stop,root,linux_compute_policy=selected.get('linux_compute_policy'))
    if row.phase=='active' and (root is None or ready is None):raise ValueError('Active visualization has no ready owned kernel')
    if row.phase=='closed' and (ready is None or stop is None or stop['verified'] is not True or row.error is not None):raise ValueError('Closed visualization lacks verified cleanup')
    return selected,root,ready,stop


async def create(case_id,user_id,selected,*,db=None):
    from .job_execution import WORKER_ID
    profile(selected);token=secrets.token_hex(32);birth=await asyncio.to_thread(identity.observe,os.getpid())
    async def insert(db):
        from .case_validation import admit_native_locked
        await admit_native_locked(db,case_id)
        from .linux_account_capacity import admit
        await admit(db, user_id)
        at=now()
        row=NativeVizSession(case_id=case_id,created_by=user_id,owner_id=WORKER_ID,
            owner_token_sha256=hashlib.sha256(token.encode()).hexdigest(),owner_identity_json=json_text(birth),
            launch_nonce=selected['nonce'],profile_json=json_text(selected),heartbeat_at=at,
            lease_expires_at=at+timedelta(seconds=LEASE_SECONDS),last_activity_at=at)
        db.add(row);await db.flush()
        from . import audit
        await audit.session_event(db, case_id, user_id, 'viz.launch.admitted', 'viz_session', row.id,
                                  {'engine': selected.get('engine', 'trame')})
        return Owner(row.id,token,WORKER_ID,selected['nonce'])
    if db is not None:return await insert(db)
    async with SessionLocal() as own_db:
        owner=await insert(own_db);await own_db.commit();return owner


async def current(db,owner,*,launch=False):
    found=await db.execute(update(NativeVizSession).where(NativeVizSession.id==owner.id,NativeVizSession.owner_id==owner.worker_id,
        NativeVizSession.owner_token_sha256==hashlib.sha256(owner.token.encode()).hexdigest(),NativeVizSession.launch_nonce==owner.nonce,
        NativeVizSession.phase.in_(('starting','active'))).values(version=NativeVizSession.version+1))
    if found.rowcount!=1:raise ExecutionConflict('state_conflict','Visualization session ownership changed')
    row=await db.get(NativeVizSession,owner.id,populate_existing=True);document(row)
    if launch and row.lease_expires_at.replace(tzinfo=timezone.utc)<=now():raise ExecutionConflict('recovery_required','Visualization owner lease expired')
    return row


async def register_root(owner,root):
    async with SessionLocal() as db:
        row=await current(db,owner,launch=True)
        if json.loads(row.process_json)!={}:raise ExecutionConflict('state_conflict','Visualization root is already registered')
        await verify_root(root);row.process_json=json_text(root);document(row)
        if row.lease_expires_at.replace(tzinfo=timezone.utc)<=now():raise ExecutionConflict('recovery_required','Visualization lease expired while verifying root')
        await db.commit()


async def released(owner):
    async with SessionLocal() as db:
        row=await current(db,owner,launch=True)
        if not json.loads(row.process_json) or row.phase!='starting':raise ValueError('Visualization is not awaiting release')
        await db.commit()


async def mark_ready(owner,session):
    async with SessionLocal() as db:
        row=await current(db,owner,launch=True);selected,root,ready,_=document(row)
        if root!=session.owned.record or ready is not None:raise ValueError('Visualization readiness belongs to another root')
        await verify_root(root);await session.verify_ready();ready_document(session.ready_birth,selected)
        row.ready_json=json_text(session.ready_birth);row.phase='active';document(row)
        if row.lease_expires_at.replace(tzinfo=timezone.utc)<=now():raise ExecutionConflict('recovery_required','Visualization lease expired while verifying readiness')
        await db.commit()


async def heartbeat(owner,last_activity):
    async with SessionLocal() as db:
        row=await current(db,owner,launch=True);row.heartbeat_at=now();row.lease_expires_at=row.heartbeat_at+timedelta(seconds=LEASE_SECONDS)
        row.last_activity_at=datetime.fromtimestamp(last_activity,tz=timezone.utc);await db.commit()


async def complete(owner,root,stop,*,error=None,exit_code=None,last_activity):
    async with SessionLocal() as db:
        row=await current(db,owner);selected,previous,_,_=document(row)
        if root is not None:
            process_record.validate(root,bound=False)
            if previous is not None and previous!=root:raise ValueError('Visualization cleanup changed its registered root')
            row.process_json=json_text(root)
        if stop is not None:
            if root is None:raise ValueError('Visualization cleanup has no actual root')
            process_stop.validate(stop,root,linux_compute_policy=selected.get('linux_compute_policy'))
        if error is None and (stop is None or stop['verified'] is not True):raise ValueError('Visualization has no verified cleanup')
        if error is not None and (not isinstance(error,str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,79}',error)):raise ValueError('Invalid visualization error category')
        if exit_code is not None:
            if type(exit_code) is not int or not -(2**31)<=exit_code<2**32:raise ValueError('Invalid visualization exit status')
            if exit_code>=2**31:exit_code-=2**32
        row.stop_json=json_text(stop or {});row.exit_code=exit_code;row.error=error;row.phase='failed' if error else 'closed';row.ended_at=now()
        row.last_activity_at=datetime.fromtimestamp(last_activity,tz=timezone.utc);document(row);await db.commit()


def public(row):
    phase=row.phase
    try:selected,root,ready,stop=document(row)
    except (ValueError,TypeError,KeyError,AttributeError):selected=None;root=None;ready=None;stop=None;phase='evidence_invalid'
    if phase in {'starting','active'} and row.lease_expires_at.replace(tzinfo=timezone.utc)<=now():phase='recovery_required'
    return {'id':row.id,'case_id':row.case_id,'phase':phase,'error':row.error,'process':root,'ready':ready,'stop':stop,
        'cleanup_verified':stop['verified'] if stop is not None else None,'exit_code':row.exit_code,
        'run_dir':selected['run_dir'] if selected else None,'file_path':selected['file_path'] if selected else None,
        'port':selected['port'] if selected else None,'started_at':row.created_at.replace(tzinfo=timezone.utc).timestamp(),
        'engine':selected.get('engine','trame') if selected else None,
        'last_activity':row.last_activity_at.replace(tzinfo=timezone.utc).timestamp(),
        'ended_at':row.ended_at.isoformat() if row.ended_at is not None else None}


async def occupied_slots(db, user_id, *, case_id=None):
    scope = NativeVizSession.created_by == user_id
    if case_id is not None:
        scope = or_(scope, NativeVizSession.case_id == case_id)
    rows = await db.stream_scalars(select(NativeVizSession).where(scope).execution_options(yield_per=32))
    user_slots = case_slots = 0
    try:
        async for row in rows:
            if row.phase not in {'starting','active'}:
                try:
                    _, process, _, stop = document(row)
                except (ValueError,TypeError,KeyError,AttributeError):
                    pass  # An invalid terminal record cannot prove that the renderer exited.
                else:
                    if process is None or (stop and stop['verified']):
                        continue
            user_slots += row.created_by == user_id
            case_slots += row.case_id == case_id
    finally:
        await rows.close()
    return user_slots, case_slots
