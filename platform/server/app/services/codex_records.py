"""Owner-fenced CLI journals; historical reads never restart or signal a PID."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

from sqlalchemy import select, update

from ..config import get_settings
from ..db import SessionLocal
from ..models import NativeCliSession
from . import codex_docker, process_identity_native as identity, process_record, process_stop
from .durable_events import json_text, now
from .owned_stdio import packed
from .codex_wsl_state import mounted_path, native_root
from .terminal_records import verify_root
from .workflow_state import ExecutionConflict

SCHEMA = 'vibeflow.cli-process.v1'
NOT_STARTED = 'vibeflow.cli-not-started.v1'
LEASE_SECONDS = 30


class CliSessionCapacity(ExecutionConflict):
    def __init__(self, limit):
        super().__init__('cli_session_capacity', f'账户已达到 {limit} 个原生 CLI 并行会话的上限')


@dataclass(frozen=True)
class Owner:
    id: str
    token: str
    worker_id: str
    nonce: str
    birth: dict
    selection_sha256: str


def digest(value):
    return hashlib.sha256(packed(value)).hexdigest()


def profile(value):
    fields = {'case_id', 'conversation_id', 'user_id', 'runtime', 'program', 'program_sha256', 'argv', 'cwd',
              'auth_home', 'auth_config_sha256', 'source_prefix', 'staging_prefix', 'permission_mode', 'transport', 'nonce'}
    if not isinstance(value, dict) or value.get('runtime') not in {'windows', 'wsl', 'docker'}:
        raise ValueError('Invalid CLI selection profile')
    native = value['runtime'] == 'wsl'
    container = value['runtime'] == 'docker'
    native_fields = fields | {'model', 'credential', 'conversation_provider_id', 'distro', 'state_root'}
    allowed = (native_fields, native_fields | {'native_state'}, native_fields | {'native_state','native_management','scope_lifecycle'}) if native else (
        fields, fields | {'model'}, fields | {'model', 'credential', 'conversation_provider_id'})
    if container:
        docker_fields = fields | {'model','credential','conversation_provider_id','docker_runtime'}
        allowed = (docker_fields,docker_fields | {'docker_stage'})
        runtime = codex_docker.runtime_from(value.get('docker_runtime'))
    if set(value) not in allowed:
        raise ValueError('Invalid CLI selection profile')
    if native and (not isinstance(value['distro'], str) or not re.fullmatch('[a-z0-9-]{1,128}', value['distro'])):
        raise ValueError('Invalid CLI native distribution')
    if native:
        native_root(value['state_root'])
    if 'credential' in value:
        from .codex_credentials import validate
        if value['credential'] is not None:
            validate(value['credential'])
        pid = value['conversation_provider_id']
        if pid is not None and (type(pid) is not int or pid <= 0 or value['conversation_id'] is None):
            raise ValueError('Invalid CLI conversation provider binding')
    if 'model' in value and (not isinstance(value['model'], str) or len(value['model']) > 120 or '\0' in value['model']):
        raise ValueError('Invalid CLI model selection')
    for key in ('case_id', 'conversation_id', 'user_id'):
        if value[key] is None and key != 'user_id':
            continue
        if type(value[key]) is not int or value[key] <= 0:
            raise ValueError('Invalid CLI user or conversation binding')
    if value['conversation_id'] is not None and value['case_id'] is None:
        raise ValueError('CLI conversation requires a case')
    for key in ('program', 'cwd', 'auth_home'):
        path = value[key]
        if not isinstance(path, str) or len(path) > 4096 or '\0' in path or not Path(path).is_absolute():
            raise ValueError('Invalid CLI absolute path')
    if native:
        expected_home = native_root(value['state_root']) / 'users' / str(value['user_id']) / 'wsl' / value['distro']
        credential = value['credential']
        if credential is not None:
            expected_home = expected_home / 'providers' / str(credential['profile_id']) / credential['revision']
        if mounted_path(value['auth_home'], value['distro']) != str(expected_home / 'home'):
            raise ValueError('CLI private home crosses its native account binding')
    program = mounted_path(value['program'], value['distro']) if native else value['program']
    if (not isinstance(value['argv'], list) or not 1 <= len(value['argv']) <= 128 or value['argv'][0] != program
            or any(not isinstance(item, str) or not item or '\0' in item for item in value['argv'])):
        raise ValueError('Invalid CLI command profile')
    if value['permission_mode'] not in {'ask', 'plan', 'approve_writes', 'full_access'}:
        raise ValueError('Invalid CLI permission mode')
    for key in ('nonce', 'program_sha256', 'auth_config_sha256'):
        if not isinstance(value[key], str) or not re.fullmatch('[0-9a-f]{64}', value[key]):
            raise ValueError('Invalid CLI profile digest')
    transport = value['transport']
    transport_fields = {'profile_sha256', 'bridge_sha256', 'identity_sha256'}
    if container:
        transport_fields = {'profile_sha256','entry_sha256','configuration_sha256'}
    if native:
        transport_fields.add('native_probe_sha256')
    if 'native_state' in value:
        from .codex_wsl_state import home_path
        preparation = value['native_state']
        if (home_path(preparation) != mounted_path(value['auth_home'], value['distro'])
                or preparation['root'] != value['state_root']
                or hashlib.sha256(preparation['configuration'].encode('utf8')).hexdigest() != value['auth_config_sha256']):
            raise ValueError('Private state initialization differs from its CLI selection')
        expected_owner = {'schema': 'vibeflow.cli-account.v1', 'user_id': value['user_id'],
                          'runtime': 'wsl', 'distro': value['distro']}
        if value['credential'] is not None:
            expected_owner['credential'] = value['credential']
        if preparation['owner'] != expected_owner:
            raise ValueError('Private state initialization crosses its account ownership')
        transport_fields.add('native_state_sha256')
    if (not isinstance(transport, dict) or set(transport) != transport_fields
            or any(not isinstance(item, str) or not re.fullmatch('[0-9a-f]{64}', item) for item in transport.values())):
        raise ValueError('Invalid CLI transport profile')
    cwd = mounted_path(value['cwd'], value['distro']) if native else value['cwd']
    launch_profile = {'argv': value['argv'], 'cwd': cwd, 'nonce': value['nonce']}
    if container:
        from .codex_permissions import READ_ONLY
        if ((value['permission_mode'] not in READ_ONLY) != ('docker_stage' in value)
                or value['staging_prefix'] is not None
                or value['credential'] is None or value['credential']['owner_id'] != value['user_id']
                or transport['entry_sha256'] != value['program_sha256']):
            raise ValueError('Docker CLI selection differs from its supported authority')
        if 'docker_stage' in value:
            from .codex_docker_stage import validate
            validate(value)
        launch_profile = codex_docker.launch_profile(value['argv'],value['nonce'],runtime)
    if 'native_state' in value:
        launch_profile['native_state'] = value['native_state']
    if 'native_management' in value:
        from .cli_scope_lifecycle import validate_profile
        validate_profile(value)
        launch_profile['native_management']=value['native_management']
    if transport['profile_sha256'] != digest(launch_profile):
        raise ValueError('CLI stream crosses its launch profile')
    for key in ('source_prefix', 'staging_prefix'):
        if value[key] is not None and (not isinstance(value[key], str) or not value[key] or len(value[key]) > 4096 or '\0' in value[key]):
            raise ValueError('Invalid CLI source profile')
    if ((value['case_id'] is None) != (value['source_prefix'] is None)
            or (value['staging_prefix'] is not None and value['source_prefix'] is None)):
        raise ValueError('CLI source does not match its case binding')
    return value


def document(row):
    selected = profile(json.loads(row.profile_json))
    if any(getattr(row, column) != selected[key] for column, key in (
            ('case_id', 'case_id'), ('conversation_id', 'conversation_id'), ('created_by', 'user_id'),
            ('launch_nonce', 'nonce'), ('runtime', 'runtime'))):
        raise ValueError('CLI selection crosses its session binding')
    identity.validate(json.loads(row.owner_identity_json))
    if row.runtime == 'docker':
        return docker_document(row,selected)
    value = json.loads(row.process_json)
    if value == {}:
        value = {'schema': SCHEMA, 'root': None, 'bridge': None}
    managed='native_management' in selected
    if not isinstance(value, dict) or set(value) != ({'schema', 'root', 'bridge','management'} if managed else {'schema','root','bridge'}) or value['schema'] != SCHEMA:
        raise ValueError('Invalid CLI process journal')
    root, bridge = value['root'], value['bridge']
    if root is not None:
        process_record.validate(root, bound=False)
        if (root['launch_nonce'] != row.launch_nonce or root['runtime'] != row.runtime
                or (root['native'] is not None) != (row.runtime == 'wsl')):
            raise ValueError('CLI root belongs to another launch')
        if root['native'] is not None and (root['native']['distribution'] != selected['distro']
                or (root['native']['process']['uid'] != 0 if managed else root['native']['process']['uid'] == 0)):
            raise ValueError('CLI native root differs from its selected non-root distribution')
    if bridge is not None:
        identity.validate(bridge)
        if root is None or bridge['platform'] != ('linux' if row.runtime == 'wsl' else root['host']['platform']):
            raise ValueError('CLI bridge has no matching root')
        if managed and bridge['uid']!=65534:raise ValueError('Managed CLI bridge must be non-root')
        if row.runtime == 'wsl' and any(bridge[key] != root['native']['process'][key] for key in (('host_id','boot_id') if managed else ('uid','host_id','boot_id'))):
            raise ValueError('CLI bridge differs from its native root identity')
    if managed:
        from .cli_scope_lifecycle import validate
        validate(row,selected,value)
    initialized = initialization(row)
    stop = json.loads(row.stop_json) or None
    if stop is not None:
        if stop.get('schema') == NOT_STARTED:
            not_started_receipt(row,stop,root is not None or bridge is not None)
        elif root is None:
            raise ValueError('CLI cleanup has no recorded root')
        else:
            process_stop.validate(stop, root)
    lifecycle(row,stop,root is not None and bridge is not None and initialized.get('userAgent'))
    return value, stop


def initialization(row):
    initialized = json.loads(row.initialize_json)
    if (not isinstance(initialized, dict) or not set(initialized) <= {'userAgent', 'platformFamily', 'platformOs'}
            or any(not isinstance(item, str) or '\0' in item for item in initialized.values())
            or len(json_text(initialized).encode('utf8')) > 4096):
        raise ValueError('Invalid CLI initialization summary')
    return initialized


def not_started_receipt(row,stop,has_launch):
    if (set(stop) != {'schema','launch_nonce','owner','verified'} or stop['launch_nonce'] != row.launch_nonce
            or stop['owner'] != json.loads(row.owner_identity_json) or stop['verified'] is not True
            or has_launch or row.phase not in {'closed','failed'}):
        raise ValueError('Invalid CLI not-started receipt')


def docker_document(row, selected):
    journal = json.loads(row.process_json)
    if not isinstance(journal,dict) or set(journal) != set(codex_docker.JOURNAL) or journal['schema'] != codex_docker.SCHEMA:
        raise ValueError('Invalid Docker CLI process journal')
    origin, containers = journal['launch'],journal['containers']
    if origin is not None:
        expected = codex_docker.launch(codex_docker.runtime_from(selected['docker_runtime']),row.id,row.created_by,row.launch_nonce)
        if origin != expected:
            raise ValueError('Docker CLI launch receipt belongs to another session')
    if containers is not None:
        codex_docker.ids(containers)
        if origin is None:
            raise ValueError('Docker CLI containers have no launch receipt')
    stop = json.loads(row.stop_json) or None
    if stop is not None:
        if stop.get('schema') == NOT_STARTED:
            not_started_receipt(row,stop,origin is not None)
        elif origin is None:
            raise ValueError('Docker CLI cleanup has no launch receipt')
        else:
            codex_docker.validate_stop(stop,origin,containers)
    initialized = initialization(row)
    lifecycle(row,stop,origin is not None and containers is not None and initialized.get('userAgent'))
    return journal, stop


def lifecycle(row,stop,active_initialized):
    if row.phase == 'active' and not active_initialized:
        raise ValueError('Active CLI lacks registered initialization')
    if row.phase == 'closed' and (stop is None or not stop['verified'] or row.error is not None):
        raise ValueError('Closed CLI lacks verified cleanup')
    if row.thread_state not in {'none', 'starting', 'ready'} or ((row.thread_state == 'ready') != (row.thread_id is not None)):
        raise ValueError('Invalid CLI thread checkpoint')


async def admit_capacity(db, user_id):
    """The caller holds this user's quota lock through the session insert."""
    limit = get_settings().codex_concurrent_sessions
    count = 0
    query = (select(NativeCliSession).where(NativeCliSession.created_by == user_id)
             .order_by(NativeCliSession.created_at.desc()).execution_options(yield_per=32))
    rows = await db.stream_scalars(query)
    try:
        async for row in rows:
            if row.phase in {'closed', 'failed'}:
                try:
                    journal, stop = document(row)
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass  # Unknown historical cleanup still occupies capacity.
                else:
                    management = journal.get('management')
                    if management is not None and management['retirement'] is not None:
                        continue
                    if management is None and stop is not None and stop['verified'] is True:
                        continue
            count += 1
            if count >= limit:
                raise CliSessionCapacity(limit)
    finally:
        await rows.close()


async def create(identifier, selected, *, db,scope_owner=None):
    from .job_execution import WORKER_ID
    profile(selected)
    from .case_validation import admit_native_locked
    await admit_native_locked(db,selected['case_id'])
    await admit_capacity(db, selected['user_id'])
    birth = await asyncio.to_thread(identity.observe, os.getpid())
    owner = Owner(identifier, secrets.token_hex(32), WORKER_ID, selected['nonce'], birth, digest(selected))
    at = now()
    process={}
    if selected['runtime'] == 'docker':
        process = codex_docker.JOURNAL
    if 'native_management' in selected:
        from . import cli_scope_allocation as scopes
        allocation=await scopes.owned(db,scope_owner)
        _,_,receipt,retired=scopes.document(allocation)
        if not receipt or retired:raise ValueError('CLI session requires an unadopted durable allocation')
        process={'schema':SCHEMA,'root':None,'bridge':None,'management':{
            'allocation':receipt,'guardian':None,'preflight':None,'retirement':None}}
    row=NativeCliSession(id=identifier, case_id=selected['case_id'], conversation_id=selected['conversation_id'],
        created_by=selected['user_id'], owner_id=owner.worker_id, owner_token_sha256=hashlib.sha256(owner.token.encode()).hexdigest(),
        owner_identity_json=json_text(birth), launch_nonce=owner.nonce, runtime=selected['runtime'], profile_json=json_text(selected),
        heartbeat_at=at, lease_expires_at=at + timedelta(seconds=LEASE_SECONDS),process_json=json_text(process))
    db.add(row)
    await db.flush()
    if 'native_management' in selected:
        await scopes.adopt(db,scope_owner,row)
        document(row)
    return owner


async def current(db, owner, *, launch=False):
    found = await db.execute(update(NativeCliSession).where(NativeCliSession.id == owner.id,
        NativeCliSession.owner_id == owner.worker_id, NativeCliSession.launch_nonce == owner.nonce,
        NativeCliSession.owner_token_sha256 == hashlib.sha256(owner.token.encode()).hexdigest(),
        NativeCliSession.phase.in_(('starting', 'active'))).values(version=NativeCliSession.version + 1))
    if found.rowcount != 1:
        raise ExecutionConflict('state_conflict', 'CLI session ownership changed')
    row = await db.get(NativeCliSession, owner.id, populate_existing=True)
    if json.loads(row.owner_identity_json) != owner.birth:
        raise ExecutionConflict('evidence_invalid', 'CLI owner birth changed')
    if digest(json.loads(row.profile_json)) != owner.selection_sha256:
        raise ExecutionConflict('evidence_invalid', 'CLI selection changed after admission')
    if launch and row.lease_expires_at.replace(tzinfo=timezone.utc) <= now():
        raise ExecutionConflict('recovery_required', 'CLI session lease expired')
    document(row)
    return row


async def register_root(db, owner, root, transport):
    row = await current(db, owner, launch=True)
    if row.runtime == 'docker':
        journal, _ = document(row)
        if journal['launch'] is not None or root != transport.receipt:
            raise ExecutionConflict('state_conflict', 'Docker CLI launch is already registered')
        journal['launch'] = root
        row.process_json = json_text(journal)
        document(row)
        return
    if root != transport.owned.record:
        raise ValueError('CLI root differs from its actual retained registration')
    await transport.owned.verify_registration()
    journal, _ = document(row)
    if journal['root'] is not None:
        raise ExecutionConflict('state_conflict', 'CLI root is already registered')
    await verify_root(root)
    journal['root'] = root
    row.process_json = json_text(journal)
    document(row)
    if row.lease_expires_at.replace(tzinfo=timezone.utc) <= now():
        raise ExecutionConflict('lease_expired', 'CLI lease expired during root registration')


async def register_bridge(db, owner, transport):
    row = await current(db, owner, launch=True)
    journal, _ = document(row)
    if row.runtime == 'docker':
        if journal['launch'] != transport.receipt or journal['containers'] is not None:
            raise ValueError('Docker CLI containers belong to another launch')
        journal['containers'] = transport.container_ids
        row.process_json = json_text(journal)
        document(row)
        return
    if journal['root'] != transport.owned.record or journal['bridge'] is not None:
        raise ValueError('CLI bridge is attached to another root')
    if json.loads(row.profile_json)['transport']['profile_sha256'] != transport.profile_sha256:
        raise ValueError('CLI bridge profile changed')
    await transport.verify_bridge()
    journal['bridge'] = transport.bridge_birth
    row.process_json = json_text(journal)
    document(row)
    if row.lease_expires_at.replace(tzinfo=timezone.utc) <= now():
        raise ExecutionConflict('lease_expired', 'CLI lease expired during bridge registration')


async def activate(db, owner, result):
    row = await current(db, owner, launch=True)
    if row.phase != 'starting':
        raise ExecutionConflict('state_conflict', 'CLI was already activated')
    row.initialize_json = json_text({key: result[key] for key in ('userAgent', 'platformFamily', 'platformOs') if key in result})
    row.phase = 'active'
    document(row)


async def verify_prepared(db, owner, transport):
    """Re-fence ownership after native preparation and before the CLI launch."""
    row = await current(db, owner, launch=True)
    journal, _ = document(row)
    if (row.phase != 'starting' or not transport.native_state_prepared
            or journal['root'] != transport.owned.record or journal['bridge'] != transport.bridge_birth
            or json.loads(row.profile_json)['transport']['profile_sha256'] != transport.profile_sha256):
        raise ExecutionConflict('state_conflict', 'CLI preparation no longer owns its registered scope')


async def heartbeat(db, owner):
    row = await current(db, owner, launch=True)
    row.heartbeat_at = now()
    row.lease_expires_at = row.heartbeat_at + timedelta(seconds=LEASE_SECONDS)


async def check(db, owner, transport):
    row = await current(db, owner, launch=True)
    journal, _ = document(row)
    matched = (journal['launch'] == transport.receipt and journal['containers'] == transport.container_ids
               if row.runtime == 'docker' else journal['root'] == transport.owned.record and journal['bridge'] == transport.bridge_birth)
    if row.phase != 'active' or not matched:
        raise ExecutionConflict('state_conflict', 'CLI is not the registered active session')
    return row


async def thread_start(db, owner, transport):
    row = await check(db, owner, transport)
    if row.thread_state != 'none':
        raise ExecutionConflict('idempotency_conflict', 'CLI thread creation was already admitted')
    row.thread_state = 'starting'


async def thread_ready(db, owner, transport, thread_id):
    if not isinstance(thread_id, str) or not 1 <= len(thread_id) <= 128 or '\0' in thread_id:
        raise ValueError('Invalid CLI thread ID')
    row = await check(db, owner, transport)
    if row.thread_state != 'starting':
        raise ExecutionConflict('state_conflict', 'CLI thread is not awaiting registration')
    row.thread_state = 'ready'
    row.thread_id = thread_id
    document(row)


async def complete(owner, root, stop, *, error=None, exit_code=None, not_started=False,management=None):
    async with SessionLocal() as db:
        row = await current(db, owner)
        journal, _ = document(row)
        if management is not None:
            previous=journal.get('management')
            if previous is None or any(previous[k] is not None and previous[k]!=management[k] for k in previous):
                raise ValueError('CLI scope cleanup changed committed evidence')
            journal['management']=management
        if row.runtime == 'docker':
            if journal['launch'] != root:
                raise ValueError('Docker CLI cleanup differs from the admitted scope')
            if stop is not None:
                codex_docker.validate_stop(stop,root,journal['containers'])
        elif root is not None:
            process_record.validate(root, bound=False)
            if journal['root'] is not None and journal['root'] != root:
                raise ValueError('CLI cleanup changed its retained root')
            journal['root'] = root
        if stop is not None and row.runtime != 'docker':
            if root is None:
                raise ValueError('CLI cleanup has no retained root')
            process_stop.validate(stop, root)
        if not_started:
            if root is not None or stop is not None or journal.get('root',journal.get('launch')) is not None:
                raise ValueError('A created CLI root cannot be marked not-started')
            stop = {'schema': NOT_STARTED, 'launch_nonce': owner.nonce, 'owner': owner.birth, 'verified': True}
        if error is None and (stop is None or not stop['verified']):
            raise ExecutionConflict('evidence_invalid', 'CLI has no verified cleanup')
        if error is not None and (not isinstance(error, str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,79}', error)):
            raise ValueError('Invalid CLI failure category')
        if exit_code is not None:
            if type(exit_code) is not int or not -(2**31) <= exit_code < 2**32:
                raise ValueError('Invalid CLI exit status')
            if exit_code >= 2**31:
                exit_code -= 2**32
        row.process_json = json_text(journal)
        row.stop_json = json_text(stop or {})
        row.phase = 'failed' if error else 'closed'
        row.error = error
        row.exit_code = exit_code
        row.ended_at = now()
        document(row)
        await db.commit()


def public(row):
    phase = row.phase
    try:
        journal, stop = document(row)
    except (ValueError, TypeError, KeyError, AttributeError):
        journal, stop, phase = None, None, 'evidence_invalid'
    if phase in {'starting', 'active'} and row.lease_expires_at.replace(tzinfo=timezone.utc) <= now():
        phase = 'recovery_required'
    from .codex_credentials import public as credential_public
    credential = credential_public(json.loads(row.profile_json).get('credential')) if journal is not None else None
    cleanup=(journal['management']['retirement'] is not None if journal is not None and 'management' in journal
             else stop['verified'] if stop is not None else None)
    return {'id': row.id, 'case_id': row.case_id, 'conversation_id': row.conversation_id, 'runtime': row.runtime,
            'credential': credential,
            'phase': phase, 'thread_id': row.thread_id if journal is not None else None,
            'thread_state': row.thread_state if journal is not None else None, 'process': journal, 'stop': stop,
            'cleanup_verified': cleanup, 'error': row.error, 'exit_code': row.exit_code,
            'created_at': row.created_at.isoformat(), 'ended_at': row.ended_at.isoformat() if row.ended_at is not None else None}
