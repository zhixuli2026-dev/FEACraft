"""Durable conversation fence shared by provider, Goal, approval and CLI runs."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta, timezone
import hashlib
import json
import logging
import os
import secrets
from contextvars import ContextVar

from sqlalchemy import select, update

from ..db import SessionLocal
from ..models import AcpTurn, AgentEvent, Conversation, ConversationExecution, NativeCliTurn, NativeRequest, ToolCall, User
from . import process_identity_native as identity
from .async_ops import finish_before_cancel
from .case_write import coordinate
from .durable_events import json_text, now
from .workflow_state import ExecutionConflict

LEASE_SECONDS = 30
active_owner = ContextVar('conversation_execution_owner', default=None)


@dataclass(frozen=True)
class Owner:
    conversation_id: int
    resource_id: str
    backend: str
    token: str
    worker_id: str
    birth: dict


def document(row):
    if row.phase not in {'idle', 'active', 'recovery_required'} or row.version < 0:
        raise ValueError('Invalid conversation execution state')
    values = (row.backend, row.resource_id, row.owner_id, row.owner_token_sha256, row.owner_identity_json, row.lease_expires_at)
    if row.phase == 'idle':
        if any(value is not None for value in values):
            raise ValueError('Idle conversation retains an owner')
    else:
        if any(value is None for value in values) or row.backend not in {'api', 'goal', 'approval', 'codex'}:
            raise ValueError('Active conversation has no valid owner')
        identity.validate(json.loads(row.owner_identity_json))
    return row


async def assert_idle(db, cid):
    row = await db.get(ConversationExecution, cid, populate_existing=True)
    if row is None:
        return
    try:
        document(row)
    except (ValueError, TypeError, KeyError):
        raise ExecutionConflict('evidence_invalid', 'Conversation execution evidence is invalid')
    if row.phase != 'idle':
        code = 'recovery_required' if row.phase == 'recovery_required' or row.lease_expires_at.replace(tzinfo=timezone.utc) <= now() else 'state_conflict'
        if identity.probe(json.loads(row.owner_identity_json))['state'] != 'match':
            code = 'recovery_required'
        raise ExecutionConflict(code, 'Conversation already has an execution owner')


async def acquire(db, cid, backend, resource_id):
    """Caller holds the case coordinator; this method flushes without commit."""
    from .job_execution import WORKER_ID
    await assert_idle(db, cid)
    if backend != 'codex' and await unsafe_native(db, cid):
        raise ExecutionConflict('recovery_required', 'Previous native execution requires reconciliation')
    if backend not in {'api', 'goal', 'approval', 'codex'} or not isinstance(resource_id, str) or not 1 <= len(resource_id) <= 128:
        raise ValueError('Invalid conversation execution binding')
    owner = Owner(cid, resource_id, backend, secrets.token_hex(32), WORKER_ID,
                  await asyncio.to_thread(identity.observe, os.getpid()))
    row = await db.get(ConversationExecution, cid)
    if row is None:
        row = ConversationExecution(conversation_id=cid)
        db.add(row)
    row.phase, row.backend, row.resource_id = 'active', backend, resource_id
    row.owner_id = owner.worker_id
    row.owner_token_sha256 = hashlib.sha256(owner.token.encode()).hexdigest()
    row.owner_identity_json = json_text(owner.birth)
    row.lease_expires_at = now() + timedelta(seconds=LEASE_SECONDS)
    await db.flush()
    document(row)
    return owner


async def current(db, owner, *, launch=True):
    found = await db.execute(update(ConversationExecution).where(
        ConversationExecution.conversation_id == owner.conversation_id,
        ConversationExecution.phase == 'active', ConversationExecution.backend == owner.backend,
        ConversationExecution.resource_id == owner.resource_id, ConversationExecution.owner_id == owner.worker_id,
        ConversationExecution.owner_token_sha256 == hashlib.sha256(owner.token.encode()).hexdigest()
    ).values(version=ConversationExecution.version + 1))
    if found.rowcount != 1:
        raise ExecutionConflict('state_conflict', 'Conversation execution owner changed')
    row = await db.get(ConversationExecution, owner.conversation_id, populate_existing=True)
    document(row)
    if json.loads(row.owner_identity_json) != owner.birth:
        raise ExecutionConflict('evidence_invalid', 'Conversation owner birth changed')
    if launch and row.lease_expires_at.replace(tzinfo=timezone.utc) <= now():
        raise ExecutionConflict('lease_expired', 'Conversation execution lease expired')
    return row


async def renew(db, owner):
    row = await current(db, owner)
    row.lease_expires_at = now() + timedelta(seconds=LEASE_SECONDS)


async def start(cid, user_id, backend, resource_id):
    from ..routers.workspaces import get_owned_case
    async with SessionLocal() as db:
        conv = await db.get(Conversation, cid)
        user = await db.get(User, user_id)
        if conv is None or user is None or user.status != 'active':
            raise ExecutionConflict('state_conflict', 'Conversation or user unavailable')
        case = await get_owned_case(conv.case_id, user, db)
        from .owner_storage_budget import hold_case
        async with hold_case(db, case):
            async with coordinate(db, case):
                user = await db.get(User, user_id, populate_existing=True)
                if user is None or user.status != 'active':
                    raise ExecutionConflict('state_conflict', 'Conversation user became unavailable')
                await get_owned_case(conv.case_id, user, db)
                owner = await acquire(db, cid, backend, resource_id)
            await finish_before_cancel(db.commit())
    return owner


def stop_binding(owner):
    return hashlib.sha256(owner.token.encode()).hexdigest()


async def _stop_pending(db, cid, binding):
    from .conversation_history import event_text_field

    return await db.scalar(select(AgentEvent.id).where(AgentEvent.conversation_id == cid,
        AgentEvent.type == 'system', event_text_field(db, 'kind') == 'execution_stop',
        event_text_field(db, 'execution_binding') == binding).limit(1)) is not None


async def request_stop(cid, user_id, *, run_id=None, native_turn=None):
    """A peer can signal the verified owner without taking over its process scope."""
    from ..routers.agent import _append_event
    from ..routers.workspaces import get_owned_case
    from .conversation_history import event_text_field

    async with SessionLocal() as db:
        conv = await db.get(Conversation, cid)
        user = await db.get(User, user_id)
        if conv is None or user is None or user.status != 'active':
            return False
        case = await get_owned_case(conv.case_id, user, db, write=True)
        async with coordinate(db, case):
            await db.refresh(user)
            if user.status != 'active':
                return False
            await get_owned_case(conv.case_id, user, db, write=True)
            row = await db.get(ConversationExecution, cid, populate_existing=True)
            if row is None or row.phase != 'active':
                return False
            try:
                document(row)
            except (ValueError, TypeError, KeyError):
                return False
            if row.lease_expires_at.replace(tzinfo=timezone.utc) <= now():
                return False
            if native_turn is not None:
                if row.backend != 'codex' or row.resource_id != native_turn:
                    return False
            elif row.backend not in {'api', 'approval', 'goal'}:
                return False
            elif run_id is not None:
                latest = await db.scalar(select(AgentEvent.payload).where(AgentEvent.conversation_id == cid,
                    AgentEvent.type == 'run_state', event_text_field(db, 'run_id') == run_id)
                    .order_by(AgentEvent.seq.desc()).limit(1))
                if not latest:
                    return False
                current_run = json.loads(latest)
                if (current_run.get('status') != 'started' or
                        current_run.get('execution_binding') != row.owner_token_sha256):
                    return False
            if not await _stop_pending(db, cid, row.owner_token_sha256):
                await _append_event(db, cid, 'system', {'kind': 'execution_stop',
                    'execution_binding':row.owner_token_sha256, 'run_id':run_id, 'native_turn_id':native_turn},
                )
                from . import audit
                await audit.case_event(db, case, user_id, 'agent.cancel.requested', 'conversation', cid,
                    {'backend': row.backend, 'run_id': run_id, 'native_turn_id': native_turn})
        await finish_before_cancel(db.commit())
    return True


async def watch(owner, cancel, *, on_requested=None):
    loop = asyncio.get_running_loop()
    renew_at = loop.time() + 5
    try:
        while True:
            await asyncio.sleep(1)
            async with SessionLocal() as db:
                if await _stop_pending(db, owner.conversation_id, stop_binding(owner)):
                    (on_requested or cancel.set)()
                    return
            # A slow query counts toward the renewal interval along with sleeps.
            if loop.time() < renew_at:
                continue
            async with SessionLocal() as db:
                await renew(db, owner)
                await db.commit()
            renew_at = loop.time() + 5
    except asyncio.CancelledError:
        raise
    except Exception as error:
        reason = error.code if isinstance(error, ExecutionConflict) else type(error).__name__
        logging.getLogger(__name__).warning('Conversation execution heartbeat stopped: %s', reason)
        cancel.set()


async def unsafe_native(db, cid):
    from .native_request_records import public
    from . import codex_records
    from ..models import NativeCliSession, NativeCliFork, NativeCliSessionRequest
    from .. import storage
    tool = await db.scalar(select(ToolCall.id).where(ToolCall.conversation_id == cid,
        ToolCall.status.in_(('queued', 'running'))).limit(1))
    if tool is not None:
        return True
    acp = await db.scalar(select(AcpTurn.id).where(AcpTurn.conversation_id == cid,
        AcpTurn.phase.in_(('admitted', 'running', 'recovery_required'))).limit(1))
    if acp is not None:
        return True
    requests = (await db.scalars(select(NativeRequest).where(NativeRequest.conversation_id == cid))).all()
    for request in requests:
        value = public(request)
        if value['phase'] in {'active', 'recovery_required', 'evidence_invalid'} or (
                value['children'] and value['children']['children'] and value['cleanup_verified'] is not True):
            return True
        stage = json.loads(request.profile_json).get('staging_prefix')
        if stage is not None and any(path.exists() for path in (storage._fs_path(stage, '.'), storage._staging_manifest_path(stage), storage._terminal_cleanup_marker(stage))):
            return True
    pending = await db.scalar(select(NativeCliTurn.id).where(NativeCliTurn.conversation_id == cid,
        NativeCliTurn.phase.not_in(('completed', 'failed', 'cancelled', 'interrupted'))).limit(1))
    if pending is not None:
        return True
    from .codex_session_requests import public as startup_public
    startups = (await db.scalars(select(NativeCliSessionRequest).where(NativeCliSessionRequest.conversation_id == cid))).all()
    for startup in startups:
        if (await startup_public(db, startup))['phase'] not in {'completed', 'failed', 'cancelled'}:
            return True
    fork = await db.scalar(select(NativeCliFork).where(NativeCliFork.conversation_id == cid))
    if fork is not None:
        from .codex_forks import public as fork_public
        if (await fork_public(db, fork))['phase'] not in {'completed', 'failed'}:
            return True
    sessions = (await db.scalars(select(NativeCliSession).where(NativeCliSession.conversation_id == cid))).all()
    for session in sessions:
        value = codex_records.public(session)
        # Metadata-only live sessions do not own a conversation execution.
        if value['phase'] == 'evidence_invalid' or (session.phase in {'closed', 'failed'} and value['cleanup_verified'] is not True):
            return True
        stage = json.loads(session.profile_json).get('staging_prefix')
        if stage is not None and session.phase in {'closed', 'failed'} and any(path.exists() for path in (
                storage._fs_path(stage, '.'), storage._staging_manifest_path(stage), storage._terminal_cleanup_marker(stage))):
            return True
    return False


async def release(owner, *, db=None):
    async def work(connection):
        row = await current(connection, owner, launch=False)
        if await unsafe_native(connection, owner.conversation_id):
            row.phase = 'recovery_required'
        else:
            row.phase = 'idle'
            row.backend = row.resource_id = row.owner_id = row.owner_token_sha256 = row.owner_identity_json = row.lease_expires_at = None
        document(row)
    if db is not None:
        return await work(db)
    async with SessionLocal() as connection:
        await work(connection)
        await connection.commit()
