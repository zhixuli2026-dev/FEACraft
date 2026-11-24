"""Durably owned read-only recovery operations and verified download receipts."""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
import uuid

from fastapi import HTTPException

from .. import storage
from ..db import SessionLocal
from ..models import NativeRequest, NativeSession
from . import native_request_records as records, sandbox_recovery_inspection as inspection
from . import sandbox_recovery_native as native, sandbox_volume
from .async_ops import finish_before_cancel
from .attempt_workspace import ordinary, checked_path
from .case_write import assert_case_available
from .owned_process import wsl_path
from .sandbox_export_execution import read

DIRECTORY = '.terminal-recovery'
NATIVE = Path(native.__file__).resolve()
BUDGET = 2 * sandbox_volume.MAX_BYTES + 4 * sandbox_volume.MAX_MANIFEST + native.MAX_CONFIG
_recovery_slots = asyncio.Semaphore(2)


def profile(value):
    """Execution admission always requires the original image directory."""
    return _profile(value, require_image=True)


def snapshot_profile(value):
    """Read a retained snapshot without opening its former source image.

    Callers still require its committed completion receipt and verify retained
    manifest/archive/file hashes. This is never a native launch permission.
    """
    return _profile(value, require_image=False)


def _profile(value, *, require_image):
    fields = {'session_id', 'root', 'image_root', 'distro', 'nonce', 'gate', 'processes',
              'disk', 'fingerprint', 'source_prefix', 'code'}
    if (not isinstance(value, dict) or set(value) not in (fields, fields | {'baseline'})
            or str(uuid.UUID(value['session_id'])) != value['session_id']
            or not isinstance(value['distro'], str) or not re.fullmatch('[a-z0-9-]+', value['distro'])
            or any(not isinstance(value[k], str) or not re.fullmatch('[0-9a-f]{64}', value[k]) for k in ('nonce', 'fingerprint'))
            or not isinstance(value['source_prefix'], str) or not value['source_prefix']
            or not isinstance(value['code'], dict) or set(value['code']) != set(native.CODE)
            or any(not isinstance(d, str) or not re.fullmatch('[0-9a-f]{64}', d) for d in value['code'].values())):
        raise ValueError('Invalid recovery ownership profile')
    parent = storage._fs_root().resolve()
    for key, directory in [('root', DIRECTORY), ('image_root', '.terminal-export')]:
        path = Path(value[key])
        if path.parent != parent / directory or not re.fullmatch('[0-9a-f]{32}', path.name):
            raise ValueError('Recovery path is outside private node storage')
        if key == 'root' or require_image:
            ordinary(path.parent); ordinary(path)
    from .process_identity_native import validate
    if validate(value['gate'])['platform'] != 'linux':
        raise ValueError('Recovery gate is not Linux')
    if not isinstance(value['processes'], list) or not 1 <= len(value['processes']) <= 65 or value['gate'] not in value['processes']:
        raise ValueError('Recovery lacks native exit identities')
    for birth in value['processes']:
        if validate(birth)['platform'] != 'linux':
            raise ValueError('Invalid native recovery identity')
    return value


def settings(selected):
    return {'schema': native.SCHEMA, 'root': wsl_path(selected['root']), 'image_root': wsl_path(selected['image_root']),
            **{key: selected[key] for key in ('disk', 'session_id', 'nonce', 'gate', 'processes', 'code')}}


async def capture(sid, user_id, *, db=None):
    from ..routers.terminal import _authorize_terminal
    async def query(connection):
        row = await connection.get(NativeSession, sid, populate_existing=True)
        if row is None or row.created_by != user_id:
            raise HTTPException(404, 'Terminal session not found')
        case = await _authorize_terminal(connection, row.case_id, user_id)
        assert_case_available(case)
        snapshot = await inspection.capture(connection, row, case.s3_prefix)
        return row, case.s3_prefix, snapshot
    if db is not None:
        return await query(db)
    async with SessionLocal() as connection:
        return await query(connection)


def native_births(snapshot, distro):
    result = []
    for _, selected, birth in snapshot.processes:
        if selected is not None:
            if selected != distro:
                raise inspection.InspectionBlocked('native_distribution_changed')
            if birth not in result:
                result.append(birth)
    return result


async def check_session(db, case_id, user_id, selected):
    from .sandbox_image_cleanup import assert_recovery_allowed
    await assert_recovery_allowed(db,selected['session_id'])
    profile(selected)
    row, prefix, snapshot = await capture(selected['session_id'], user_id, db=db)
    original = json.loads(row.profile_json)
    if (row.case_id != case_id or prefix != selected['source_prefix'] or snapshot.fingerprint != selected['fingerprint']
            or original['distro'] != selected['distro']
            or original['terminal']['command'][-1] != wsl_path(selected['image_root'])
            or json.loads(row.process_json)['gate']['process'] != selected['gate']
            or native_births(snapshot, selected['distro']) != selected['processes']):
        raise inspection.InspectionBlocked('recovery_source_registration_changed')
    return snapshot


async def verify_exit(sid, user_id, expected=None):
    row, prefix, before = await capture(sid, user_id)
    observed = await inspection.observe(before)
    if observed['status'] != 'processes_exited':
        raise inspection.InspectionBlocked(observed['reason'])
    _, _, after = await capture(sid, user_id)
    if before.fingerprint != after.fingerprint or (expected is not None and after.fingerprint != expected):
        raise inspection.InspectionBlocked('records_changed_during_inspection')
    return row, prefix, before


def command(selected, parameters):
    profile(selected)
    if parameters != {'distro': selected['distro'], 'session_id': selected['session_id']} or selected['code'] != native.code():
        raise ValueError('Recovery invocation changed')
    config = Path(selected['root']) / 'settings.json'
    raw = read(config.parent, config.name, native.MAX_CONFIG)
    if json.loads(raw) != settings(selected):
        raise ValueError('Recovery settings differ from durable registration')
    return ['wsl.exe', '-d', selected['distro'], '--exec', 'python3', '-I', '-S', '-u',
            wsl_path(NATIVE), '--config', wsl_path(config), '--sha256', hashlib.sha256(raw).hexdigest()], 210, 65536


def run(selected):
    from .native_execution_client import run_native, ChildExecutionError
    result = run_native('sandbox_recover', {'distro': selected['distro'], 'session_id': selected['session_id']},
                        timeout=210, capture=True)
    if result.returncode:
        native.write(Path(selected['root']), 'diagnostic.txt', result.stdout)
        raise ChildExecutionError('Read-only sandbox recovery failed')


def receipt(value, selected):
    fields = {'schema', 'session_id', 'nonce', 'manifest_sha256', 'archive_sha256', 'archive_bytes', 'image_sha256'}
    if (not isinstance(value, dict) or set(value) != fields or value['schema'] != native.SCHEMA
            or value['session_id'] != selected['session_id'] or value['nonce'] != selected['nonce']
            or type(value['archive_bytes']) is not int or not 0 < value['archive_bytes'] <= native.MAX_ARCHIVE
            or any(not isinstance(value[k], str) or not re.fullmatch('[0-9a-f]{64}', value[k])
                   for k in ('manifest_sha256', 'archive_sha256', 'image_sha256'))):
        raise ValueError('Invalid recovery completion receipt')
    return value


def completed(selected, expected=None):
    root = Path(selected['root'])
    value = receipt(json.loads(read(root, 'complete.json', 16384)), selected)
    if expected is not None and value != expected:
        raise ValueError('Recovery completion differs from committed receipt')
    raw = read(root, 'manifest.json', sandbox_volume.MAX_MANIFEST)
    manifest = json.loads(raw)
    if (hashlib.sha256(raw).hexdigest() != value['manifest_sha256'] or manifest['binding']['gate'] != selected['gate']
            or manifest['binding']['filesystem'] != 'ext4'):
        raise ValueError('Recovery manifest differs from committed evidence')
    return value, manifest


async def recover(sid, user_id):
    from . import request_deadline
    async with request_deadline.scope(request_deadline.SANDBOX_RECOVERY_SECONDS) as deadline:
        try:
            await asyncio.wait_for(_recovery_slots.acquire(), min(.1,deadline.remaining()))
        except TimeoutError:
            deadline.check()
            raise inspection.InspectionBlocked('recovery_busy')
        try:
            deadline.check()
            return await _recover(sid, user_id, deadline)
        finally:
            _recovery_slots.release()


async def _recover(sid, user_id, deadline):
    from .native_request_execution import scope
    from . import node_storage_budget as budget
    row, prefix, snapshot = await verify_exit(sid, user_id)
    deadline.check()
    from .sandbox_image_cleanup import assert_recovery_allowed
    from . import sandbox_baseline
    async with SessionLocal() as db:
        await assert_recovery_allowed(db,sid)
        baseline = await sandbox_baseline.registered(db, row)
    deadline.check()
    original = json.loads(row.profile_json)
    case_id = row.case_id
    selected = {'session_id': sid, 'root': '', 'image_root': str(storage._fs_root().resolve() / '.terminal-export'
                / original['terminal']['command'][-1].rsplit('/', 1)[-1]),
                'distro': original['distro'], 'nonce': secrets.token_hex(32),
                'gate': json.loads(row.process_json)['gate']['process'], 'source_prefix': prefix,
                'fingerprint': snapshot.fingerprint, 'processes': native_births(snapshot, original['distro']),
                'disk': {}, 'code': native.code()}
    if baseline is not None:
        if baseline['image_root'] != selected['image_root'] or baseline['gate'] != selected['gate']:
            raise ValueError('Recovery baseline differs from the original image')
        selected['baseline'] = baseline
    claim = None
    owner = None
    execution_started = False
    failure = None
    async def admit():
        nonlocal claim
        claim = await asyncio.to_thread(budget._admit, BUDGET)
    async def reserve_owner():
        nonlocal owner
        owner = await records.create(case_id, user_id, 'terminal.recover', selected)
    async def release_if_drained():
        if claim is None:return
        if owner is not None:
            try:
                async with SessionLocal() as db:
                    request = await db.get(NativeRequest, owner.id)
                    if request.phase=='active':return
                    journal = records.document(request)
                    if any(c['stop'] is None or c['stop']['verified'] is not True for c in journal['children'].values()):
                        return
            except Exception:
                # Keep the API-owned node claim if durable cleanup cannot be
                # established. A failed HTTP request is not proof that I/O ended.
                return
        await asyncio.to_thread(budget._release, *claim)
    try:
        deadline.check()
        await finish_before_cancel(admit())
        deadline.check()
        def prepare():
            parent = storage._fs_root().resolve() / DIRECTORY
            parent.mkdir(exist_ok=True); ordinary(parent)
            root = parent / uuid.uuid4().hex
            root.mkdir(exist_ok=False)
            selected['root'] = str(root)
            selected['disk'] = json.loads(read(Path(selected['image_root']), 'disk.json', 16384))
            if baseline is not None:
                raw, _ = sandbox_baseline.verified(selected['image_root'], baseline)
                native.write(root, 'initial.json', raw)
            profile(selected)
            native.write(root, 'settings.json', json.dumps(settings(selected)).encode())
            native.write(root, 'node-claim.json', json.dumps({'claim': claim[1].name, 'bytes': BUDGET}).encode())
        await finish_before_cancel(asyncio.to_thread(prepare))
        deadline.check()
        await finish_before_cancel(reserve_owner())
        deadline.check()
        async def before_release():
            deadline.check()
            await verify_exit(sid, user_id, selected['fingerprint'])
            deadline.check()
        execution_started = True
        async with scope(case_id, user_id, selected['root'], 'terminal.recover', selected,
                         owner=owner, before_release=before_release) as executor:
            await executor.run_sync(run, selected, timeout=300)
            deadline.check()
            executor.result_receipt, _ = await finish_before_cancel(asyncio.to_thread(completed, selected))
            deadline.check()
        deadline.check()
        return executor.owner.id
    except BaseException as error:
        failure=error
        raise
    finally:
        if owner is not None and not execution_started:
            await finish_before_cancel(records.complete(owner,
                'TimeoutError' if deadline.expired else type(failure).__name__,
                cancelled=isinstance(failure,asyncio.CancelledError) and not deadline.expired))
        await finish_before_cancel(release_if_drained())


async def load(sid, identifier, user_id):
    from ..routers.terminal import _authorize_terminal
    async with SessionLocal() as db:
        terminal = await db.get(NativeSession, sid)
        if terminal is None or terminal.created_by != user_id:
            raise HTTPException(404, 'Terminal session not found')
        case = await _authorize_terminal(db, terminal.case_id, user_id)
        assert_case_available(case)
        row = await db.get(NativeRequest, identifier)
        if row is None or row.kind != 'terminal.recover' or row.created_by != user_id or row.case_id != terminal.case_id:
            raise HTTPException(404, 'Recovery operation not found')
        selected = snapshot_profile(json.loads(row.profile_json))
        if selected['session_id'] != sid:
            raise HTTPException(404, 'Recovery operation not found')
        from . import sandbox_baseline
        baseline = await sandbox_baseline.registered(db, terminal)
        if 'baseline' in selected and selected['baseline'] != baseline:
            raise ValueError('Recovery baseline differs from its committed source')
        value = records.public(row)
        value = {key: value[key] for key in ('id', 'phase', 'error', 'cleanup_verified', 'created_at', 'ended_at')}
        value.update(session_id=sid, command_replayed=False, project_files_modified=False)
        if row.phase == 'succeeded':
            if value['cleanup_verified'] is not True:
                raise ValueError('Recovery completed without verified process cleanup')
            expected = receipt(json.loads(row.result_json), selected)
        else:
            expected = None
    if expected is not None:
        _, manifest = await finish_before_cancel(asyncio.to_thread(completed, selected, expected))
        value.update(receipt=expected, manifest=manifest)
        if 'baseline' in selected:
            _, initial = await finish_before_cancel(asyncio.to_thread(sandbox_baseline.verified, selected['root'], baseline))
            value['baseline'] = initial
            value['baseline_sha256'] = baseline['sha256']
    return value, selected


async def list_operations(sid, user_id, before=None):
    from sqlalchemy import JSON, case, cast, func, or_, select, and_
    from ..routers.terminal import _authorize_terminal
    async with SessionLocal() as db:
        terminal = await db.get(NativeSession, sid)
        if terminal is None or terminal.created_by != user_id:
            raise HTTPException(404, 'Terminal session not found')
        available = await _authorize_terminal(db, terminal.case_id, user_id)
        assert_case_available(available)
        if db.bind.dialect.name == 'sqlite':
            binding = case((func.json_valid(NativeRequest.profile_json),
                            func.json_extract(NativeRequest.profile_json, '$.session_id')), else_=None)
        else:
            binding = cast(NativeRequest.profile_json, JSON)['session_id'].as_string()
        conditions = [NativeRequest.created_by == user_id, NativeRequest.case_id == terminal.case_id,
                      NativeRequest.kind == 'terminal.recover', binding == sid]
        if before is not None:
            anchor = await db.scalar(select(NativeRequest).where(*conditions, NativeRequest.id == before))
            if anchor is None:raise HTTPException(404, 'Recovery page cursor not found')
            conditions.append(or_(NativeRequest.created_at < anchor.created_at,
                                  and_(NativeRequest.created_at == anchor.created_at, NativeRequest.id < anchor.id)))
        rows = list(await db.scalars(select(NativeRequest).where(*conditions)
                                    .order_by(NativeRequest.created_at.desc(), NativeRequest.id.desc()).limit(51)))
        items = []
        for row in rows[:50]:
            public = records.public(row)
            items.append({key: public[key] for key in ('id', 'phase', 'error', 'cleanup_verified', 'created_at', 'ended_at')})
        return {'session_id': sid, 'recoveries': items, 'next_before': rows[49].id if len(rows) > 50 else None}


def download(selected, expected):
    """Keep one verified ordinary file handle through streaming; no path reopen."""
    from .request_deadline import current
    budget=current()
    deadline=time.monotonic()+30
    if budget is not None:
        budget.check()
        deadline=min(deadline,budget.deadline)
    target = checked_path(Path(selected['root']), 'recovery.zip')
    ordinary(target)
    stream = target.open('rb')
    try:
        digest = hashlib.sha256()
        count = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('Recovery download verification exceeded its deadline')
            chunk = stream.read(1024 * 1024)
            if time.monotonic() >= deadline:
                raise TimeoutError('Recovery download verification exceeded its deadline')
            if not chunk:break
            count += len(chunk)
            if count > expected['archive_bytes']:
                raise ValueError('Recovery archive grew beyond its receipt')
            digest.update(chunk)
        if count != expected['archive_bytes'] or digest.hexdigest() != expected['archive_sha256']:
            raise ValueError('Recovery download differs from committed evidence')
        stream.seek(0)
        return stream
    except BaseException:
        stream.close()
        raise
