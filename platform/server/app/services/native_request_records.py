"""Owner-fenced native request journals; never create Jobs or recover by PID."""
import asyncio
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import uuid

from sqlalchemy import update

from ..db import SessionLocal
from ..models import NativeRequest
from . import native_child_records as children,owned_process,process_identity_native as os_identity,process_record,process_stop
from .durable_events import json_text,now
from .workflow_state import ExecutionConflict

SCHEMA='vibeflow.native-request-children.v1'
LEASE_SECONDS=30
PURPOSES={'report.prepare':'pandoc_parse','latex.prepare':'latex_probe','synctex.query':'synctex_query',
          'runtime.wsl_probe':'wsl_runtime_probe','runtime.python_probe':'python_runtime_probe','agent.shell':'agent_shell',
          'terminal.export':'sandbox_export','terminal.recover':'sandbox_recover','terminal.cleanup':'sandbox_image_cleanup'}
PURPOSES['job.finalize']=None
NATIVE_KINDS={'latex.prepare','synctex.query','runtime.wsl_probe','terminal.export','terminal.recover','terminal.cleanup'}
GLOBAL_KINDS={'runtime.wsl_probe','runtime.python_probe'}


@dataclass(frozen=True)
class Owner:
    id: str
    token: str
    worker_id: str
    nonce: str


def profile(kind,value):
    if not isinstance(value,dict):raise ValueError('Invalid native request profile')
    if kind=='job.finalize':
        from .job_output_finalizing import native_profile
        return native_profile(value)
    if kind=='report.prepare':
        fields={'pandoc','purpose'} if value.get('purpose')=='pandoc_cite' else {'pandoc'}
        if (set(value)!=fields
                or not isinstance(value['pandoc'],str)
                or not Path(value['pandoc']).is_absolute() or not Path(value['pandoc']).is_file()):
            raise ValueError('Invalid request Pandoc profile')
    elif kind=='latex.prepare':
        from .latex_artifacts import RECIPES
        if (set(value)!={'distro','recipe'} or not isinstance(value['distro'],str)
                or not re.fullmatch('[a-z0-9-]+',value['distro']) or value['recipe'] not in RECIPES):
            raise ValueError('Invalid request LaTeX profile')
    elif kind=='synctex.query':
        from .synctex_execution import profile as synctex_profile
        synctex_profile(value)
    elif kind in GLOBAL_KINDS:
        from .runtime_probe_execution import profile as runtime_profile
        runtime_profile(kind,value)
    elif kind=='terminal.export':
        from .sandbox_export_execution import profile as export_profile
        export_profile(value)
    elif kind=='terminal.recover':
        from .sandbox_recovery_execution import profile as recovery_profile
        recovery_profile(value)
    elif kind=='terminal.cleanup':
        from .sandbox_image_cleanup import profile as cleanup_profile
        cleanup_profile(value)
    elif kind=='agent.shell':
        from .agent_shell import profile as shell_profile
        shell_profile(value)
    else:raise ValueError('Unsupported native request kind')
    return value


def document(request):
    if request.kind not in PURPOSES:raise ValueError('Unsupported native request kind')
    scope(request.case_id,request.kind)
    linux_compute_policy = None
    private_network = False
    if request.kind=='agent.shell':
        selected=profile(request.kind,json.loads(request.profile_json))
        if 'linux_home' in selected:
            from .terminal_protocol import validate_linux_home
            validate_linux_home(selected['linux_home'],request.created_by)
        linux_compute_policy = selected.get('linux_compute_policy')
        private_network = selected.get('linux_network') == 'private'
        if request.conversation_id!=selected['conversation_id'] or request.tool_call_id!=selected['tool_call_id']:
            raise ValueError('Agent shell request crosses its tool binding')
    elif request.conversation_id is not None or request.tool_call_id is not None:raise ValueError('Non-tool request has an Agent binding')
    os_identity.validate(json.loads(request.owner_identity_json))
    value=json.loads(request.children_json)
    if value=={}:return {'schema':SCHEMA,'children':{}}
    if (not isinstance(value,dict) or set(value)!={'schema','children'} or value['schema']!=SCHEMA
            or not isinstance(value['children'],dict) or len(value['children'])>children.MAX_CHILDREN):
        raise ValueError('Invalid native request journal')
    for key,row in value['children'].items():
        if (not isinstance(row,dict) or set(row)!=children.FIELDS or str(uuid.UUID(key))!=key
                or row['id']!=key or row['caller']!='finalizer' or row['parent_launch_nonce']!=request.launch_nonce
                or row['purpose'] not in allowed_purposes(request)
                or row['phase'] not in {'preparing','running','stopped','failed'}):
            raise ValueError('Native child belongs to another request or profile')
        if not isinstance(row['request_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',row['request_sha256']):
            raise ValueError('Invalid native request digest')
        if row['error'] is not None and (not isinstance(row['error'],str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,79}',row['error'])):
            raise ValueError('Invalid native request child error')
        terminal=row['phase'] in {'stopped','failed'}
        for name in ('created_at','ended_at'):
            stamp=row[name]
            if name=='ended_at' and not terminal:
                if stamp is not None:raise ValueError('Active native request child has ended')
            elif not isinstance(stamp,str) or len(stamp)>64 or datetime.fromisoformat(stamp).utcoffset()!=timedelta(0):
                raise ValueError('Native request timestamps require UTC')
        if not terminal and (row['stop'] is not None or row['error'] is not None):
            raise ValueError('Active native request child has a cleanup result')
        if row['phase']=='preparing' and row['identity'] is not None:
            raise ValueError('Unregistered native request child has an identity')
        if row['identity'] is not None:
            process_record.validate(row['identity'],bound=False)
            if private_network and row['phase'] in {'running','stopped'} and 'linux_network' not in row['identity']:
                raise ValueError('Linux Agent child lacks its configured network namespace')
            native=is_native(request)
            if row['identity']['runtime']!=('wsl' if native else 'windows') or (row['identity']['native'] is not None)!=native:
                raise ValueError('Native request child runtime differs')
            if native and row['identity']['native']['distribution']!=json.loads(request.profile_json)['distro']:
                raise ValueError('Native request child distribution differs')
        if row['stop'] is not None:
            if row['identity'] is None:raise ValueError('Native request cleanup has no identity')
            process_stop.validate(row['stop'],row['identity'],linux_compute_policy=linux_compute_policy)
        if row['phase']=='running' and row['identity'] is None:
            raise ValueError('Running native request child has no identity')
        if row['phase']=='stopped' and (row['stop'] is None or not row['stop']['verified'] or row['error'] is not None):
            raise ValueError('Stopped native request child lacks exit proof')
        if row['phase']=='stopped' and linux_compute_policy is not None and 'linux_compute_usage' not in row['stop']:
            raise ValueError('Stopped Agent child lacks configured Linux compute usage')
    return value


def is_native(request):
    if request.kind=='job.finalize':
        selected=json.loads(request.profile_json)
        return selected['validator']=='latex' and selected['distro']!='native-linux'
    if request.kind in {'runtime.wsl_probe','latex.prepare','synctex.query'}:
        from .openfoam_runtime import NATIVE_LINUX
        return json.loads(request.profile_json)['distro']!=NATIVE_LINUX
    return request.kind in NATIVE_KINDS or (request.kind=='agent.shell' and json.loads(request.profile_json)['runtime']=='wsl')


def allowed_purposes(request):
    if request.kind=='job.finalize':
        from .job_output_finalizing import allowed_purposes as selected
        return selected(profile(request.kind,json.loads(request.profile_json)))
    if request.kind=='report.prepare' and json.loads(request.profile_json).get('purpose')=='pandoc_cite':
        return {'pandoc_cite'}
    purpose=PURPOSES[request.kind]
    if request.kind in {'latex.prepare','synctex.query'} and not is_native(request):purpose+='_linux'
    return {purpose}


async def create(case_id,user_id,kind,settings,*,db=None):
    from .job_execution import WORKER_ID
    scope(case_id,kind);profile(kind,settings)
    if kind=='agent.shell' and 'linux_home' in settings:
        from .terminal_protocol import validate_linux_home
        validate_linux_home(settings['linux_home'],user_id)
    owner=Owner(str(uuid.uuid4()),secrets.token_hex(32),WORKER_ID,secrets.token_hex(32))
    birth=await asyncio.to_thread(os_identity.observe,os.getpid())
    async def insert(db):
        from .case_validation import admit_native_locked
        await admit_native_locked(db,case_id)
        if kind=='terminal.export':
            from .sandbox_export_execution import check_session
            await check_session(db,case_id,user_id,settings,birth)
        if kind=='terminal.recover':
            from .sandbox_recovery_execution import check_session
            await check_session(db,case_id,user_id,settings)
        if kind=='terminal.cleanup':
            from .sandbox_image_cleanup import check_session
            await check_session(db,case_id,user_id,settings)
        at=now()
        row=NativeRequest(id=owner.id,case_id=case_id,created_by=user_id,owner_id=owner.worker_id,
            owner_token_sha256=hashlib.sha256(owner.token.encode()).hexdigest(),owner_identity_json=json_text(birth),
            launch_nonce=owner.nonce,kind=kind,profile_json=json_text(settings),heartbeat_at=at,
            lease_expires_at=at+timedelta(seconds=LEASE_SECONDS),
            conversation_id=settings['conversation_id'] if kind=='agent.shell' else None,
            tool_call_id=settings['tool_call_id'] if kind=='agent.shell' else None)
        db.add(row);await db.flush()
    if db is not None:await insert(db)
    else:
        async with SessionLocal() as own_db:
            await insert(own_db);await own_db.commit()
    return owner


def scope(case_id,kind):
    if kind in GLOBAL_KINDS:
        if case_id is not None:raise ValueError('Global runtime requests must not invent a case binding')
    elif type(case_id) is not int or case_id<=0:
        raise ValueError('Document requests require an existing positive case identity')


async def _current(db,owner,nonce,*,launch=False):
    # The UPDATE serializes each JSON mutation on PostgreSQL and SQLite, without
    # taking a case control lock or retaining a read snapshot before a write.
    found=await db.execute(update(NativeRequest).where(NativeRequest.id==owner.id,
        NativeRequest.owner_id==owner.worker_id,NativeRequest.launch_nonce==owner.nonce,
        NativeRequest.owner_token_sha256==hashlib.sha256(owner.token.encode()).hexdigest(),
        NativeRequest.phase=='active').values(version=NativeRequest.version+1))
    if found.rowcount!=1 or nonce!=owner.nonce:
        raise ExecutionConflict('state_conflict','Native request ownership changed')
    row=await db.get(NativeRequest,owner.id,populate_existing=True)
    if launch and row.lease_expires_at.replace(tzinfo=timezone.utc)<=now():
        raise ExecutionConflict('recovery_required','Native request lease expired before launch')
    return row


async def heartbeat(owner):
    async with SessionLocal() as db:
        # Keep the case -> native row order used by release. Recovery validators
        # also stop when the original attempt is cancelled or loses its head.
        selected=await db.get(NativeRequest,owner.id)
        if selected is not None and selected.kind=='job.finalize':
            from .case_validation import admit_session_locked
            await admit_session_locked(db,NativeRequest,owner.id)
        row=await _current(db,owner,owner.nonce,launch=True)
        if row.kind=='job.finalize':
            from .job_output_finalizing import require_native_release
            await require_native_release(db,row)
        row.heartbeat_at=now();row.lease_expires_at=row.heartbeat_at+timedelta(seconds=LEASE_SECONDS)
        await db.commit()


async def reserve(owner,nonce,identifier,purpose,caller,parameters,*,preparation_nonce=None):
    async with SessionLocal() as db:
        if preparation_nonce is not None:
            from .case_validation import admit_session_locked
            await admit_session_locked(db,NativeRequest,owner.id)
        request=await _current(db,owner,nonce,launch=True)
        value=document(request)
        if request.kind in {'agent.shell','terminal.recover','terminal.cleanup'} and value['children']:raise ExecutionConflict('state_conflict','This native operation cannot execute twice')
        if len(value['children'])>=children.MAX_CHILDREN or identifier in value['children']:
            raise ExecutionConflict('resource_limit','Native request child capacity exceeded')
        value['children'][identifier]={'id':identifier,'purpose':purpose,'caller':caller,'phase':'preparing',
            'parent_launch_nonce':nonce,'request_sha256':hashlib.sha256(json_text(parameters).encode()).hexdigest(),
            'identity':None,'stop':None,'error':None,'created_at':now().isoformat(),'ended_at':None}
        request.children_json=json_text(value);document(request)
        if request.kind=='job.finalize':
            from .finalizing_native_usage import reserve as reserve_usage
            from .finalizing_native_preparation import reserve as reserve_preparation
            reserve_usage(request,identifier)
            await reserve_preparation(db,request,value['children'][identifier],preparation_nonce)
        elif preparation_nonce is not None:raise ValueError('Unexpected independent-validator preparation binding')
        await db.commit()


async def register(owner,nonce,identifier,identity,distribution):
    async with SessionLocal() as db:
        request=await _current(db,owner,nonce,launch=True)
        value=document(request);row=value['children'][identifier]
        if request.kind=='terminal.cleanup':
            from .sandbox_image_launch import require_ready
            await require_ready(db,request,row,identity)
        if request.kind=='job.finalize':
            from .finalizing_native_scope import require_ready
            await require_ready(db,request,row,identity)
        process_record.validate(identity,bound=False)
        native=is_native(request)
        if request.kind in {'runtime.wsl_probe','latex.prepare','synctex.query','job.finalize'} and not native and json.loads(request.profile_json).get('distro')=='native-linux':
            if distribution!=json.loads(request.profile_json)['distro']:
                raise ExecutionConflict('evidence_invalid','Native Linux request changed its runtime profile')
            distribution=None
        if (row['phase']!='preparing' or identity['runtime']!=('wsl' if native else 'windows')
                or (identity['native'] is not None)!=native
                or (native and identity['native']['distribution']!=distribution)
                or (not native and distribution is not None)
                or identity['gate_sha256']!=hashlib.sha256(owned_process.GATE.read_bytes()).hexdigest()
                or identity['identity_probe_sha256']!=hashlib.sha256(owned_process.PROBE.read_bytes()).hexdigest()):
            raise ExecutionConflict('evidence_invalid','Native request launch differs from its profile')
        if (await asyncio.to_thread(os_identity.probe,identity['host']))['state']!='match':
            raise ExecutionConflict('evidence_invalid','Native request host birth differs')
        if native and (await owned_process.probe_native(distribution,identity['native']['process']))['state']!='match':
            raise ExecutionConflict('evidence_invalid','Native request native birth differs')
        if request.lease_expires_at.replace(tzinfo=timezone.utc)<=now():
            raise ExecutionConflict('recovery_required','Native request lease expired during identity verification')
        row.update(phase='running',identity=identity)
        request.children_json=json_text(value);document(request)
        await db.commit()


async def release(owner,nonce,identifier,process,send):
    """Hold the request's ownership fence through its final control send."""
    async with SessionLocal() as db:
        from .case_validation import admit_session_locked
        await admit_session_locked(db,NativeRequest,owner.id)
        request=await _current(db,owner,nonce,launch=True)
        row=document(request)['children'][identifier]
        if row['phase']!='running' or row['identity']!=process.record:
            raise ExecutionConflict('evidence_invalid','Native request registration changed before release')
        if request.kind=='terminal.recover':
            from .sandbox_recovery_execution import check_session
            await check_session(db,request.case_id,request.created_by,json.loads(request.profile_json))
        if request.kind=='terminal.cleanup':
            from .sandbox_image_cleanup import check_session
            await check_session(db,request.case_id,request.created_by,json.loads(request.profile_json),request.id)
            from .sandbox_image_scope import require_allocated
            await require_allocated(db,request,row)
        if request.kind=='job.finalize':
            from .job_output_finalizing import require_native_release
            await require_native_release(db,request)
            from .finalizing_native_scope import require_ready
            await require_ready(db,request,row,process.record)
        await asyncio.wait_for(send(),timeout=5)
        await db.commit()


async def finish(owner,nonce,identifier,identity,stop,error):
    async with SessionLocal() as db:
        request=await _current(db,owner,nonce)
        value=document(request);row=value['children'][identifier]
        if row['phase'] in {'stopped','failed'}:
            if row['identity']!=identity or row['stop']!=stop or row['error']!=error:
                raise ExecutionConflict('state_conflict','Native request cleanup already differs')
            return
        if identity is not None:
            process_record.validate(identity,bound=False)
            if row['identity'] is not None and row['identity']!=identity:
                raise ExecutionConflict('evidence_invalid','Native request cleanup changed its birth')
            row['identity']=identity
        if stop is not None:
            if row['identity'] is None:raise ValueError('Native request cleanup has no identity')
            policy = json.loads(request.profile_json).get('linux_compute_policy') if request.kind=='agent.shell' else None
            process_stop.validate(stop,row['identity'],linux_compute_policy=policy)
        if request.kind=='terminal.cleanup' and error is None:
            from .sandbox_image_scope import require_closed
            await require_closed(db,request)
        row.update(phase='stopped' if stop is not None and stop['verified'] and error is None else 'failed',
            stop=stop,error=error,ended_at=now().isoformat())
        request.children_json=json_text(value);document(request)
        await db.commit()


async def complete(owner,error=None,*,cancelled=False,result=None):
    finalizer_cancelled=cancelled and isinstance(result,dict) and result.get('schema')=='vibeflow.finalizing-recovery.v1'
    case_id=None
    if finalizer_cancelled:
        # Look up the immutable binding in a separate read transaction. Reading
        # it in the writer transaction before its first DML can trigger SQLite
        # WAL SQLITE_BUSY_SNAPSHOT when another writer commits in between.
        async with SessionLocal() as lookup:
            selected=await lookup.get(NativeRequest,owner.id)
            case_id=selected.case_id if selected is not None else None
    async with SessionLocal() as db:
        if finalizer_cancelled and case_id is not None:
            from .durable_events import lock_case_execution
            # Closing an already-owned writer is allowed even while content
            # admission is blocked. Only the event sequence lock is required.
            await lock_case_execution(db,case_id)
        request=await _current(db,owner,owner.nonce)
        if finalizer_cancelled and request.case_id!=case_id:raise ValueError('Cancelled request changed its immutable case')
        value=document(request)
        progress=json.loads(request.result_json) if request.kind=='job.finalize' else None
        if request.kind=='job.finalize':
            if error is None and any(row['phase']!='stopped' for row in value['children'].values()):error='NativeValidationError'
            if error is None:
                from .finalizing_native_scope import require_closed
                await require_closed(db,request)
            from .job_output_finalizing import validate_result
            request.result_json=json_text(validate_result(result,json.loads(request.profile_json)))
        if error is None and any(row['phase']!='stopped' for row in value['children'].values()):
            raise ExecutionConflict('evidence_invalid','Native request lacks verified child cleanup')
        if request.kind=='terminal.cleanup' and error is None:
            from .sandbox_image_scope import require_closed
            await require_closed(db,request)
        if request.kind in {'terminal.recover','terminal.cleanup'} and error is None:
            if request.kind=='terminal.cleanup':
                from .sandbox_image_cleanup import receipt
            else:
                from .sandbox_recovery_execution import receipt
            if len(value['children'])!=1:
                raise ExecutionConflict('evidence_invalid','Recovery lacks one verified native execution')
            if request.kind=='terminal.cleanup':
                child=next(iter(value['children'].values()))
                if child['identity']['native']['process']!=result.get('outer_gate'):
                    raise ExecutionConflict('evidence_invalid','Cleanup receipt belongs to another native gate')
            request.result_json=json_text(receipt(result,json.loads(request.profile_json)))
        elif result is not None and error is None and request.kind!='job.finalize':
            raise ValueError('Unexpected native result receipt')
        request.phase='cancelled' if cancelled else 'failed' if error else 'succeeded'
        request.error=error;request.ended_at=now()
        if request.kind=='job.finalize' and cancelled:
            from .finalizing_request_cancellation import record
            await record(db,request,progress)
        await db.commit()


async def release_check(owner,processes=None):
    async with SessionLocal() as db:
        row=await _current(db,owner,owner.nonce,launch=True);journal=document(row)
        if processes is not None and (set(journal['children'])!=set(processes) or any(journal['children'][key]['phase']!='running' or journal['children'][key]['identity']!=birth for key,birth in processes.items())):
            raise ExecutionConflict('evidence_invalid','Registered Agent birth differs from its retained launch')
        await db.commit()


def public(request):
    """Read durable evidence only; an expired owner is never automatically replaced."""
    try:
        journal=document(request)
        owner_identity=json.loads(request.owner_identity_json)
        cleanup=(all(row['stop'] is not None and row['stop']['verified'] for row in journal['children'].values())
                 if journal['children'] else None)
    except (ValueError,KeyError,TypeError,AttributeError):
        journal=None;cleanup=None;owner_identity=None
    phase=request.phase
    if journal is None:phase='evidence_invalid'
    elif phase=='active' and request.lease_expires_at.replace(tzinfo=timezone.utc)<=now():phase='recovery_required'
    return {'id':request.id,'case_id':request.case_id,'kind':request.kind,'phase':phase,'error':request.error,
        'conversation_id':request.conversation_id,'tool_call_id':request.tool_call_id,
        'owner_identity':owner_identity,'children':journal,'cleanup_verified':cleanup,
        'created_at':request.created_at.isoformat(),'ended_at':request.ended_at.isoformat() if request.ended_at else None}
