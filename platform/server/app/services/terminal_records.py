"""Owner-fenced terminal session records; transport loss never invents cleanup."""
import asyncio
from datetime import timedelta,timezone
import hashlib
import json
import os
import re
import secrets
import uuid

from sqlalchemy import update

from ..db import SessionLocal
from ..models import NativeSession,Case
from . import process_identity_native as identity,process_record,process_stop,terminal_protocol as protocol
from .durable_events import now,json_text,lock_case_execution
from .native_request_records import Owner
from .owned_process import probe_native,GATE,PROBE
from .workflow_state import ExecutionConflict

SCHEMA='vibeflow.native-session-process.v1'
LEASE_SECONDS=30
TAIL_BYTES=256*1024


def profile(value,case_id):
    fields={'terminal','distro','code','source_prefix','staging_prefix'}
    if not isinstance(value,dict) or set(value) not in (fields,fields|{'credential'}):
        raise ValueError('Invalid terminal ownership profile')
    settings=protocol.validate(value['terminal'])
    linux_host=settings['command']==list(protocol.LINUX_SHELL)
    if 'credential' in value:
        from .terminal_credentials import validate
        validate(value['credential'])
        if settings['shell']!='cmd' or linux_host:raise ValueError('API binding requires Windows terminal')
    if case_id is not None and (type(case_id) is not int or case_id<=0):raise ValueError('Invalid terminal case binding')
    if settings['shell']=='wsl-sandbox' and case_id is None:raise ValueError('Sandbox terminal requires a case')
    if settings['shell']=='cmd':
        if value['distro'] is not None:raise ValueError('Host terminal cannot select a native distribution')
    elif not isinstance(value['distro'],str) or not re.fullmatch('[a-z0-9-]+',value['distro']):raise ValueError('Invalid terminal distribution')
    names={'bridge_sha256','pty_gate_sha256','identity_probe_sha256','protocol_sha256'}
    variants=(names,names|{'sandbox_sha256'},names|{'sandbox_sha256','disk_sha256'},
              names|{'sandbox_sha256','tree_metadata_sha256'},names|{'sandbox_sha256','disk_sha256','tree_metadata_sha256'})
    if not isinstance(value['code'],dict) or set(value['code']) not in variants or any(not isinstance(x,str) or not re.fullmatch('[0-9a-f]{64}',x) for x in value['code'].values()):
        raise ValueError('Invalid terminal implementation profile')
    for key in ('source_prefix','staging_prefix'):
        if value[key] is not None and (not isinstance(value[key],str) or not value[key] or len(value[key])>4096 or '\0' in value[key]):raise ValueError('Invalid terminal staging profile')
    if value['staging_prefix'] is not None and (case_id is None or value['source_prefix'] is None):raise ValueError('Terminal staging has no case source')
    return value


def document(row):
    selected=profile(json.loads(row.profile_json),row.case_id)
    if 'credential' in selected and selected['credential']['owner_id']!=row.created_by:raise ValueError('Terminal API belongs to another owner')
    if selected['terminal']['shell']!=row.shell or selected['terminal']['nonce']!=row.launch_nonce:raise ValueError('Terminal profile crosses its session')
    identity.validate(json.loads(row.owner_identity_json))
    value=json.loads(row.process_json)
    if value=={}:value={'schema':SCHEMA,'root':None,'gate':None}
    if not isinstance(value,dict) or set(value) not in ({'schema','root','gate'},{'schema','root','gate','export'}) or value['schema']!=SCHEMA:raise ValueError('Invalid terminal process journal')
    export=value.get('export')
    if export is not None:
        from .sandbox_export_publication import SCHEMA as EXPORT_SCHEMA
        if (not isinstance(export,dict) or set(export)!={'schema','session_id','request_id','manifest_sha256','event_id'}
                or export['schema']!=EXPORT_SCHEMA or export['session_id']!=row.id
                or any(str(uuid.UUID(export[key]))!=export[key] for key in ('request_id','event_id'))
                or not re.fullmatch('[0-9a-f]{64}',export['manifest_sha256'])):
            raise ValueError('Invalid sandbox publication receipt')
    if (row.phase=='closed' and any(mode in selected['terminal']['command'] for mode in ('--external-export','--persistent-export')) and export is None):
        raise ValueError('Closed sandbox terminal lacks publication proof')
    root=value['root'];gate=value['gate']
    expected=('linux' if selected['terminal']['command']==list(protocol.LINUX_SHELL) else 'windows') if row.shell=='cmd' else row.shell
    if root is not None:
        process_record.validate(root,bound=False)
        if root['launch_nonce']!=row.launch_nonce or root['runtime']!=expected or (root['native'] is not None)!=(row.shell!='cmd'):
            raise ValueError('Terminal root belongs to another session')
        if root['native'] is not None and root['native']['distribution']!=selected['distro']:raise ValueError('Terminal native distribution differs')
    if gate is not None:
        if root is None or not isinstance(gate,dict) or set(gate)!={'schema','launch_nonce','process','profile_sha256',*selected['code']}:
            raise ValueError('Invalid terminal gate journal')
        identity.validate(gate['process'])
        if (gate['schema']!='vibeflow.terminal-gate.v1' or gate['launch_nonce']!=row.launch_nonce
                or gate['profile_sha256']!=protocol.digest(selected['terminal'])
                or any(gate[key]!=digest for key,digest in selected['code'].items())
                or gate['process']['platform']!=('windows' if expected=='windows' else 'linux')):
            raise ValueError('Terminal gate crosses its selected profile')
    stop=json.loads(row.stop_json)
    if stop=={}:stop=None
    if stop is not None:
        if root is None:raise ValueError('Terminal cleanup has no recorded root')
        process_stop.validate(stop,root)
    if row.phase=='active' and (root is None or gate is None):raise ValueError('Active terminal has no registered gate')
    if row.phase=='closed' and (gate is None or stop is None or stop['verified'] is not True or row.error is not None):raise ValueError('Closed terminal lacks verified cleanup')
    if len(row.tail_text.encode('utf8'))>TAIL_BYTES:raise ValueError('Terminal tail exceeds its bound')
    return value,stop


async def create(case_id,user_id,selected):
    from .job_execution import WORKER_ID
    profile(selected,case_id)
    owner=Owner(str(uuid.uuid4()),secrets.token_hex(32),WORKER_ID,selected['terminal']['nonce'])
    birth=await asyncio.to_thread(identity.observe,os.getpid())
    async with SessionLocal() as db:
        if case_id is not None:
            from .case_write import assert_case_available
            case=await db.get(Case,case_id)
            if case is None:assert_case_available(case)
            # Selection has already released its transaction. Serialize the
            # durable reservation with copy/trash before native startup begins.
            await lock_case_execution(db,case_id)
            await db.refresh(case)
            assert_case_available(case)
            from .case_validation import assert_writable_locked
            await assert_writable_locked(db,case_id)
        if 'credential' in selected:
            from .quota import admit_model
            await admit_model(db, user_id, 'terminal')
        at=now()
        db.add(NativeSession(id=owner.id,case_id=case_id,created_by=user_id,owner_id=owner.worker_id,
            owner_token_sha256=hashlib.sha256(owner.token.encode()).hexdigest(),owner_identity_json=json_text(birth),
            launch_nonce=owner.nonce,shell=selected['terminal']['shell'],profile_json=json_text(selected),
            heartbeat_at=at,lease_expires_at=at+timedelta(seconds=LEASE_SECONDS)))
        from . import audit
        credential = selected.get('credential', {})
        await audit.session_event(db, case_id, user_id, 'terminal.launch.admitted', 'terminal', owner.id,
            {'shell': selected['terminal']['shell'], 'harness': credential.get('harness'),
             'provider_profile_id': credential.get('profile_id')})
        await db.commit()
    return owner


async def current(db,owner,*,launch=False):
    found=await db.execute(update(NativeSession).where(NativeSession.id==owner.id,NativeSession.owner_id==owner.worker_id,
        NativeSession.owner_token_sha256==hashlib.sha256(owner.token.encode()).hexdigest(),NativeSession.launch_nonce==owner.nonce,
        NativeSession.phase.in_(('starting','active'))).values(version=NativeSession.version+1))
    if found.rowcount!=1:raise ExecutionConflict('state_conflict','Terminal session ownership changed')
    row=await db.get(NativeSession,owner.id,populate_existing=True)
    if launch and row.lease_expires_at.replace(tzinfo=timezone.utc)<=now():raise ExecutionConflict('recovery_required','Terminal lease expired before release')
    document(row)
    return row


async def verify_root(root):
    process_record.validate(root,bound=False)
    if root['gate_sha256']!=hashlib.sha256(GATE.read_bytes()).hexdigest() or root['identity_probe_sha256']!=hashlib.sha256(PROBE.read_bytes()).hexdigest():
        raise ValueError('Terminal root launch implementation differs')
    if (await asyncio.to_thread(identity.probe,root['host']))['state']!='match':raise ValueError('Terminal host birth changed')
    if root['native'] is not None:
        native=root['native']
        if (await probe_native(native['distribution'],native['process']))['state']!='match':raise ValueError('Terminal native birth changed')


async def register_root(owner,root):
    async with SessionLocal() as db:
        row=await current(db,owner,launch=True)
        if json.loads(row.process_json)!={}:raise ExecutionConflict('state_conflict','Terminal root is already registered')
        await verify_root(root)
        row.process_json=json_text({'schema':SCHEMA,'root':root,'gate':None});document(row)
        if row.lease_expires_at.replace(tzinfo=timezone.utc)<=now():raise ExecutionConflict('recovery_required','Terminal lease expired during root verification')
        await db.commit()


async def register_gate(owner,transport):
    async with SessionLocal() as db:
        row=await current(db,owner,launch=True);journal,_=document(row)
        if journal['root']!=transport.owned.record or journal['gate'] is not None:raise ValueError('Terminal gate is attached to another root')
        await verify_root(journal['root']);await transport.verify_gate()
        journal['gate']=transport.gate_birth;row.process_json=json_text(journal);row.phase='active';document(row)
        if row.lease_expires_at.replace(tzinfo=timezone.utc)<=now():raise ExecutionConflict('recovery_required','Terminal lease expired during gate verification')
        await db.commit()


async def heartbeat(owner):
    async with SessionLocal() as db:
        row=await current(db,owner,launch=True);row.heartbeat_at=now();row.lease_expires_at=row.heartbeat_at+timedelta(seconds=LEASE_SECONDS)
        await db.commit()


async def complete(owner,root,stop,*,tail='',exit_code=None,error=None,export=None):
    async with SessionLocal() as db:
        row=await current(db,owner);journal,_=document(row)
        if export is not None:
            from ..models import NativeRequest,DurableEvent
            request=await db.get(NativeRequest,export['request_id'])
            event=await db.get(DurableEvent,export['event_id'])
            if (request is None or request.phase!='succeeded' or request.kind!='terminal.export'
                    or request.case_id!=row.case_id or json.loads(request.profile_json)['session_id']!=row.id
                    or event is None or event.case_id!=row.case_id or event.type!='terminal.export_published'
                    or json.loads(event.payload_json)!={key:value for key,value in export.items() if key!='event_id'}):
                raise ValueError('Sandbox publication has no matching committed evidence')
            journal['export']=export
        if root is not None:
            process_record.validate(root,bound=False)
            if journal['root'] is not None and journal['root']!=root:raise ValueError('Terminal cleanup changed its registered root')
            journal['root']=root
        if stop is not None:
            if root is None:raise ValueError('Terminal cleanup has no actual root')
            process_stop.validate(stop,root)
        if error is None and (stop is None or stop['verified'] is not True):raise ExecutionConflict('evidence_invalid','Terminal has no verified cleanup')
        if error is not None and (not isinstance(error,str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,79}',error)):raise ValueError('Invalid terminal error category')
        if exit_code is not None:
            if type(exit_code) is not int or not -(2**31)<=exit_code<2**32:raise ValueError('Invalid terminal exit status')
            if exit_code>=2**31:exit_code-=2**32
        row.process_json=json_text(journal);row.stop_json=json_text(stop or {});row.tail_text=tail.encode('utf8')[-TAIL_BYTES:].decode('utf8','ignore')
        row.exit_code=exit_code;row.error=error;row.phase='failed' if error else 'closed';row.ended_at=now();document(row)
        await db.commit()


def public(row):
    phase=row.phase;valid=True
    try:journal,stop=document(row)
    except (ValueError,TypeError,KeyError,AttributeError):journal=None;stop=None;phase='evidence_invalid';valid=False
    if phase in {'starting','active'} and row.lease_expires_at.replace(tzinfo=timezone.utc)<=now():phase='recovery_required'
    return {'id':row.id,'case_id':row.case_id,'shell':row.shell,'phase':phase,'error':row.error,'process':journal,'stop':stop,
        'cleanup_verified':stop['verified'] if stop is not None else None,'exit_code':row.exit_code,
        'created_at':row.created_at.isoformat(),'ended_at':row.ended_at.isoformat() if row.ended_at is not None else None,
        'tail':row.tail_text if valid else None,
        'credential':json.loads(row.profile_json).get('credential') if valid else None}
