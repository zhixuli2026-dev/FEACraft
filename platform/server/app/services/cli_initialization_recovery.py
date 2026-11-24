"""Recover initializer credit after independently observing its actual writers.

This is not a CLI scope-stop receipt. Session history and private files remain.
The fixed initializer runs synchronously in the registered bridge; no child is
allowed to start until the prepared credit transaction has committed.
"""
import asyncio
import hashlib
import json
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select

from ..db import SessionLocal
from ..models import CliInitializationReservation, NativeCliSession, User
from . import cli_initialization_capacity as capacity, codex_records as records
from . import process_identity_native as identity
from .async_ops import finish_before_cancel
from .durable_events import now
from .storage_admission import lock_owner
from .workflow_state import ExecutionConflict

STATUS = 'initializer_exited'
SCHEMA = 'vibeflow.cli-initializer-exit.v1'
UNRELEASED = 'initializer_not_released'
UNRELEASED_SCHEMA = 'vibeflow.cli-initializer-not-released.v1'
SLOTS = asyncio.Semaphore(2)
ROLES = ('owner', 'host', 'native', 'bridge')


def mode(credit, session):
    journal, _ = records.document(session)
    if session.runtime == 'wsl' and journal['root'] is not None and journal['bridge'] is not None:
        return STATUS, ROLES
    bound = json.loads(credit.binding_json)
    if (session.runtime == 'wsl' and journal['bridge'] is None and session.phase in {'starting', 'failed'}
            and bound.get('schema') == 'vibeflow.cli-initialization-capacity.v3'
            and bound.get('launch_order') == capacity.LAUNCH_ORDER):
        return UNRELEASED, ('owner', 'host', 'native') if journal['root'] is not None else ('owner',)
    raise ExecutionConflict('recovery_required', '初始化恢复缺少已登记的进程或许可顺序凭证，额度继续保留')


def bindings(session, credit=None, *, legacy=False):
    journal, _ = records.document(session)
    root = journal['root']
    if credit is None and (session.runtime != 'wsl' or root is None or journal['bridge'] is None):
        raise ExecutionConflict('recovery_required', '初始化恢复缺少已登记的原生进程身份，额度继续保留')
    # Scope retirement is subsequent lifecycle metadata, not a change to the
    # immutable registered initializer writers. Keep legacy receipts readable.
    process=journal
    managed='management' in journal
    if managed and not legacy:
        process={key:journal[key] for key in ('schema','root','bridge')}
    elif managed and 'recovery' in journal['management']:
        from .cli_session_recovery import original_journal
        process=original_journal(journal)
    value = {
        'session_id': session.id,
        'selection_sha256': records.digest(json.loads(session.profile_json)),
        'process_sha256': records.digest(process),
        'owner_sha256': records.digest(json.loads(session.owner_identity_json)),
    }
    if managed and not legacy:value['writer_binding_schema']='vibeflow.cli-initializer-writers.v1'
    if credit is not None:
        status, _ = mode(credit, session)
        if status == UNRELEASED:
            value['initialization_binding_sha256'] = records.digest(json.loads(credit.binding_json))
    return value


def validate_receipt(credit, session):
    receipt = json.loads(credit.receipt_json)
    status, roles = mode(credit, session)
    if (credit.state != 'settled' or credit.remaining_bytes != 0
            or set(receipt) != {'schema', 'status', 'proof'}
            or receipt['schema'] != 'vibeflow.cli-initialization-receipt.v1'
            or receipt['status'] != status):
        raise ValueError('Invalid recovered initializer credit')
    proof = receipt['proof']
    if (not isinstance(proof, dict)
            or set(proof) != {'schema', 'binding', 'observed_at', 'observations'}
            or proof['schema'] != (SCHEMA if status == STATUS else UNRELEASED_SCHEMA)
            or proof['binding'] != bindings(session, credit,legacy='writer_binding_schema' not in proof.get('binding',{}))
            or proof['observations'] != {role: {'state': 'exited', 'reason': 'process_exited'} for role in roles}
            or not isinstance(proof['observed_at'], str)
            or datetime.fromisoformat(proof['observed_at']).tzinfo is None):
        raise ValueError('Initializer exit receipt differs from its journal')
    return receipt


def check_code(session):
    from .owned_stdio import BRIDGE, IDENTITY, NATIVE_PROBE, NATIVE_STATE
    from .owned_process import GATE
    selected = json.loads(session.profile_json)
    if 'native_management' in selected:
        from .cli_scope_lifecycle import check_code as check_scope_code
        check_scope_code(selected)
    journal, _ = records.document(session)
    expected = {BRIDGE: selected['transport']['bridge_sha256'],
                IDENTITY: selected['transport']['identity_sha256'],
                NATIVE_PROBE: selected['transport']['native_probe_sha256'],
                NATIVE_STATE: selected['transport']['native_state_sha256']}
    if journal['root'] is not None:
        expected[GATE] = journal['root']['gate_sha256']
        if journal['root']['identity_probe_sha256'] != expected[IDENTITY]:
            raise ExecutionConflict('evidence_invalid', '初始化根身份实现与登记摘要不一致')
    if any(hashlib.sha256(path.read_bytes()).hexdigest() != digest for path, digest in expected.items()):
        raise ExecutionConflict('evidence_invalid', '初始化实现与登记摘要不一致，额度继续保留')


async def authorized(db, identifier, user_id, *, locked=False):
    query = select(NativeCliSession).where(NativeCliSession.id == identifier)
    if locked:
        query = query.with_for_update()
    session = await db.scalar(query.execution_options(populate_existing=True))
    if session is None or session.created_by != user_id:
        raise HTTPException(404, 'CLI 会话不存在')
    user = await db.get(User, user_id, populate_existing=True)
    if user is None or user.status != 'active':
        raise HTTPException(403, 'CLI 用户已不可用')
    credit = await db.get(CliInitializationReservation, identifier, populate_existing=True)
    if credit is None:
        raise ExecutionConflict('recovery_required', '该会话没有可核验的原生初始化预留')
    capacity.validate(credit, session)
    return session, credit


async def observe(session, roles=ROLES):
    from .owned_process import probe_native
    journal, _ = records.document(session)
    root = journal['root']
    result = {}
    births = [('owner', json.loads(session.owner_identity_json))]
    if 'host' in roles:births.append(('host', root['host']))
    for role, birth in births:
        result[role] = await finish_before_cancel(asyncio.to_thread(identity.probe, birth))
        if result[role]['state'] != 'exited':
            raise ExecutionConflict('recovery_required', '初始化所属进程仍存活或身份无法核验，额度继续保留')
    births = []
    if 'native' in roles:births.append(('native', root['native']['process']))
    if 'bridge' in roles:births.append(('bridge', journal['bridge']))
    for role, birth in births:
        result[role] = await probe_native(root['native']['distribution'], birth)
        if result[role]['state'] != 'exited':
            raise ExecutionConflict('recovery_required', '原生初始化进程仍存活或身份无法核验，额度继续保留')
    return result


async def _recover(identifier, user_id):
    async with SessionLocal() as db:
        session, credit = await authorized(db, identifier, user_id)
        if credit.state == 'settled':
            if json.loads(credit.receipt_json).get('status') in {STATUS, UNRELEASED}:
                validate_receipt(credit, session)
            snapshot = None
        else:
            status, roles = mode(credit, session)
            snapshot = bindings(session, credit)
            version = session.version
            await finish_before_cancel(asyncio.to_thread(check_code, session))
    if snapshot is not None:
        # No SQL lock or transaction is held across actual OS/native probes.
        observed = await observe(session, roles)
        proof = {'schema': SCHEMA if status == STATUS else UNRELEASED_SCHEMA, 'binding': snapshot,
                 'observed_at': now().isoformat(), 'observations': observed}
        async with SessionLocal() as db:
            await lock_owner(db, user_id)
            current, credit = await authorized(db, identifier, user_id, locked=True)
            if bindings(current, credit) != snapshot or current.version != version:
                raise ExecutionConflict('state_conflict', '初始化记录在核验期间发生变化，请重新核验')
            if credit.state == 'active':
                capacity.settle(credit, status, {'proof': proof})
                validate_receipt(credit, current)
            elif json.loads(credit.receipt_json).get('status') in {STATUS, UNRELEASED}:
                validate_receipt(credit, current)
            await finish_before_cancel(db.commit())
    # Reconcile this exact bound record after committing proof. Do not require
    # a global parse of unrelated or partially written node JSON to repair it.
    async with SessionLocal() as db:
        session, credit = await authorized(db, identifier, user_id)
        selected = capacity.settlement_ticket(credit, session)
    if selected is not None:
        from .cli_node_capacity import release
        await finish_before_cancel(asyncio.to_thread(release, selected, allow_fragments=True))
    async with SessionLocal() as db:
        session, credit = await authorized(db, identifier, user_id)
        from .cli_node_capacity import outstanding
        ticket = capacity.ticket(credit, session)
        node_pending = ticket is not None and await finish_before_cancel(asyncio.to_thread(outstanding, ticket))
        return {'session_id': identifier, 'state': credit.state, 'remaining_bytes': credit.remaining_bytes,
                'receipt': json.loads(credit.receipt_json), 'node_claim_pending': node_pending,
                'session_cleanup_verified': records.public(session)['cleanup_verified']}


async def recover(identifier, user_id):
    try:
        await asyncio.wait_for(SLOTS.acquire(), .1)
    except TimeoutError as error:
        raise ExecutionConflict('recovery_busy', '初始化恢复正在核验，请稍后重试') from error
    try:
        async with asyncio.timeout(60):
            return await _recover(identifier, user_id)
    except TimeoutError as error:
        raise ExecutionConflict('recovery_required', '初始化核验超时，未核验的额度继续保留') from error
    finally:
        SLOTS.release()
