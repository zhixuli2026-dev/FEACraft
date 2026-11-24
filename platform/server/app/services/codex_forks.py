"""Owned native fork admission, at-most-once RPC and durable branch provenance."""
import asyncio
import json
import uuid
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy import select, update

from ..db import SessionLocal
from ..models import AgentEvent, Case, Conversation, ConversationExecution, NativeCliFork, NativeCliSession, NativeCliTurn, ToolCall
from . import codex_sessions as sessions, codex_records, codex_turn_records, conversation_execution as control
from .async_ops import finish_before_cancel
from .case_write import coordinate
from .codex_conversations import authorize
from .codex_history import native_fork_allowed
from .codex_permissions import THREAD_POLICIES
from .durable_events import now
from .supervisor import register, unregister
from .workflow_state import ExecutionConflict

RUNS = {}
TERMINAL = {'completed', 'failed', 'recovery_required'}


def same_environment(old, selected):
    return all(old.get(key) == selected.get(key) for key in (
        'runtime', 'program_sha256', 'auth_config_sha256', 'auth_home', 'source_prefix',
        'permission_mode', 'model', 'credential', 'distro', 'state_root', 'docker_runtime'))


async def source(db, cid, user_id, turn_id):
    from ..routers.agent import _load_events
    conversation = await authorize(db, cid, user_id)
    turn = await db.get(NativeCliTurn, turn_id, populate_existing=True)
    latest = await db.scalar(select(NativeCliTurn.id).where(NativeCliTurn.conversation_id == cid,
        NativeCliTurn.created_by == user_id).order_by(NativeCliTurn.created_at.desc(), NativeCliTurn.id.desc()).limit(1))
    if turn is None or turn.conversation_id != cid or turn.created_by != user_id:
        raise HTTPException(404, '原生分支来源不存在')
    value = await codex_turn_records.public(db, turn)
    if value['phase'] != 'completed' or value['cleanup_verified'] is not True or turn.id != latest:
        raise ExecutionConflict('input_stale', '原生分叉需要最后一个已完成且已核验收尾的原生回合')
    previous = await db.get(NativeCliSession, turn.session_id)
    boundary = next((e for e in reversed(await _load_events(db, cid, bounded=False))
        if e['type'] == 'message' and e.get('payload', {}).get('native_execution_id') == turn.id
        and e['payload'].get('role') == 'assistant'), None)
    if boundary is None or not native_fork_allowed(boundary, latest_native_turn=True, same_environment=True):
        raise ExecutionConflict('evidence_invalid', '原生回合缺少对应的已完成回答')
    if boundary['payload'].get('native_turn_id') != turn.native_turn_id or boundary['payload'].get('native_thread_id') != turn.thread_id:
        raise ExecutionConflict('evidence_invalid', '原生分支边界与线程不一致')
    await sessions.authorize(db, conversation.case_id, cid, user_id, expected=json.loads(previous.profile_json))
    return conversation, turn, previous, boundary


async def public(db, row, *, refresh=True):
    phase = row.phase
    session = await db.get(NativeCliSession, row.session_id, populate_existing=True) if row.session_id else None
    value = codex_records.public(session) if session is not None else None
    target = await db.get(Conversation, row.conversation_id)
    original = await db.get(NativeCliTurn, row.source_turn_id)
    original_session = await db.get(NativeCliSession, original.session_id) if original is not None else None
    boundary = await db.get(AgentEvent, row.boundary_event_id)
    try:
        payload = json.loads(boundary.payload) if boundary is not None else {}
    except (ValueError, TypeError):
        payload = {}
    valid = (phase in TERMINAL | {'admitted', 'forking', 'registered'}
        and row.request_sha256 == codex_turn_records.digest({'source_turn_id': row.source_turn_id})
        and original is not None and original.conversation_id == row.source_conversation_id
        and original.created_by == row.created_by and original.case_id == row.case_id and original.phase == 'completed'
        and original_session is not None and original_session.conversation_id == row.source_conversation_id
        and original_session.created_by == row.created_by and original_session.case_id == row.case_id
        and boundary is not None and boundary.conversation_id == row.source_conversation_id and boundary.type == 'message'
        and isinstance(payload, dict) and payload.get('role') == 'assistant' and payload.get('native_execution_id') == original.id
        and payload.get('native_thread_id') == original.thread_id and payload.get('native_turn_id') == original.native_turn_id
        and target is not None and target.case_id == row.case_id and target.forked_from_id == row.source_conversation_id
        and target.branch_head_event_id == row.boundary_event_id and row.source_conversation_id != row.conversation_id
        and (session is None and row.session_id is None or session is not None and session.created_by == row.created_by
             and session.case_id == row.case_id and session.conversation_id == row.conversation_id))
    if phase == 'completed':
        valid = valid and value is not None and value['phase'] == 'closed' and value['cleanup_verified'] is True and value['thread_id'] == row.thread_id and bool(row.thread_id)
    if not valid or value and value['phase'] == 'evidence_invalid':
        phase = 'evidence_invalid'
    elif phase not in TERMINAL:
        execution = await db.get(ConversationExecution, row.conversation_id, populate_existing=True)
        if refresh and (execution is None or execution.phase == 'idle' or execution.resource_id != row.id):
            latest = await db.get(NativeCliFork, row.id, populate_existing=True)
            if latest is not None:
                return await public(db, latest, refresh=False)
        try:
            from .process_identity_native import probe
            if (execution is None or execution.phase != 'active' or execution.resource_id != row.id or
                execution.lease_expires_at.replace(tzinfo=timezone.utc) <= now() or
                probe(json.loads(execution.owner_identity_json))['state'] != 'match'):
                phase = 'recovery_required'
        except (ValueError, TypeError, KeyError, OSError):
            phase = 'evidence_invalid'
    return {'id': row.id, 'source_conversation_id': row.source_conversation_id,
        'conversation_id': row.conversation_id, 'source_turn_id': row.source_turn_id,
        'boundary_event_id': row.boundary_event_id, 'client_request_id': row.client_request_id,
        'phase': phase, 'session_id': row.session_id, 'thread_id': row.thread_id,
        'cleanup_verified': value['cleanup_verified'] if value else None, 'error': row.error,
        'runtime': session.runtime if session else original_session.runtime if original_session else None,
        'created_at': row.created_at.isoformat()}


async def get(identifier, user_id):
    async with SessionLocal() as db:
        row = await db.get(NativeCliFork, identifier)
        if row is None or row.created_by != user_id:
            raise HTTPException(404, '原生分支不存在')
        await authorize(db, row.conversation_id, user_id)
        return await public(db, row)


async def listing(db, cid, user_id, request_id=None):
    await authorize(db, cid, user_id)
    query = select(NativeCliFork).where(NativeCliFork.source_conversation_id == cid, NativeCliFork.created_by == user_id)
    if request_id is not None:
        query = query.where(NativeCliFork.client_request_id == request_id)
    rows = (await db.scalars(query.order_by(NativeCliFork.created_at.desc()).limit(50))).all()
    return [await public(db, row) for row in rows]


class Execution:
    def __init__(self, identifier, owners, user_id, selected):
        self.id, self.owners, self.user_id, self.selected = identifier, owners, user_id, selected
        self.task = self.session = None
        self.finalizing = False
        self.cancel = asyncio.Event()

    async def current(self, db):
        for owner in self.owners:
            await control.current(db, owner)
        found = await db.execute(update(NativeCliFork).where(NativeCliFork.id == self.id,
            NativeCliFork.phase.not_in(TERMINAL)).values(version=NativeCliFork.version + 1))
        if found.rowcount != 1:
            raise ExecutionConflict('state_conflict', '原生分支已结束或归属改变')
        return await db.get(NativeCliFork, self.id, populate_existing=True)

    async def stop(self, *, error=None):
        self.cancel.set()
        if self.task is not None and self.task is not asyncio.current_task() and not self.finalizing:
            self.task.cancel()
        if self.task is not None and self.task is not asyncio.current_task():
            await finish_before_cancel(asyncio.gather(self.task, return_exceptions=True))

    async def run(self):
        heartbeats = [asyncio.create_task(control.watch(owner, self.cancel)) for owner in self.owners]
        async def expired():
            await self.cancel.wait()
            if not self.finalizing: self.task.cancel()
        watcher = asyncio.create_task(expired())
        issued, registered, failure = False, False, None
        try:
            async with SessionLocal() as db:
                row = await db.get(NativeCliFork, self.id)
                cid, case_id = row.conversation_id, row.case_id
                source_cid, source_turn = row.source_conversation_id, row.source_turn_id
            self.session = sessions.Session(case_id, cid, self.user_id,
                provider_profile_id=self.selected['credential']['profile_id'], provider_wire_api='responses',
                runtime=self.selected['runtime'], fork_only=True)
            # Startup already has its own shared deadline and verified retirement.
            await self.session.start()
            self.session.turn_execution = self
            async with asyncio.timeout(45):
                async with self.session.operation_lock:
                    await asyncio.to_thread(self.session.verify_code)
                    async with self.session.access() as db:
                        row = await self.current(db)
                        _, prior_turn, _, boundary = await source(db, source_cid, self.user_id, source_turn)
                        if boundary['id'] != row.boundary_event_id or not same_environment(self.selected, self.session.selected):
                            raise ExecutionConflict('input_stale', '分叉运行环境发生变化，请采用文字交接')
                        row.session_id = self.session.id
                        await codex_records.thread_start(db, self.session.owner, self.session.client.transport)
                        row.phase = 'forking'
                    issued = True
                    approval, sandbox = THREAD_POLICIES[self.selected['permission_mode']]
                    result = await self.session.client.fork_thread(prior_turn.thread_id,
                        last_turn_id=prior_turn.native_turn_id, exclude_turns=True,
                        cwd=self.session.client.transport.profile['cwd'],
                        approval_policy=approval, sandbox=sandbox,
                        defer_goal_continuation=True,
                        model=self.selected.get('model') or (self.selected.get('credential') or {}).get('default_model') or None)
                    thread = result.get('thread', {})
                    identifier = thread.get('id')
                    self.session.check_identifier(identifier)
                    if not isinstance(identifier, str) or not identifier or identifier == prior_turn.thread_id or thread.get('forkedFromId') != prior_turn.thread_id:
                        raise ValueError('CLI fork returned an invalid source/thread binding')
                    async with self.session.access() as db:
                        row = await self.current(db)
                        await codex_records.thread_ready(db, self.session.owner, self.session.client.transport, identifier)
                        row.thread_id, row.phase = identifier, 'registered'
                    registered = True
        except BaseException as error:
            failure = type(error).__name__
        finally:
            self.finalizing = True
            try:
                if self.session is not None:
                    await self.session.close(error=failure)
            except Exception as error:
                failure = failure or type(error).__name__
            try:
                async with SessionLocal() as db:
                    row = await db.get(NativeCliFork, self.id)
                    async with coordinate(db, await db.get(Case, row.case_id), allow_deleted=True):
                        for owner in self.owners: await control.current(db, owner, launch=False)
                        # Preserve even a startup failure's actual owned session.
                        if self.session is not None and await db.get(NativeCliSession, self.session.id) is not None:
                            row.session_id = self.session.id
                        if registered and failure is None:
                            try:
                                await source(db, row.source_conversation_id, self.user_id, row.source_turn_id)
                                await sessions.authorize(db, row.case_id, row.conversation_id, self.user_id, expected=self.session.selected)
                            except Exception as error:
                                failure = type(error).__name__
                        row.phase = 'completed' if registered and failure is None else 'recovery_required' if issued else 'failed'
                        row.error, row.ended_at = failure, now()
                        if row.phase == 'completed':
                            state = codex_records.public(await db.get(NativeCliSession, row.session_id))
                            if state['phase'] != 'closed' or state['cleanup_verified'] is not True:
                                raise ExecutionConflict('evidence_invalid', '原生分支缺少所属进程收尾证据')
                            from ..routers.agent import _append_event
                            await _append_event(db, row.conversation_id, 'system', {'kind': 'native_fork',
                                'fork_id': row.id, 'source_turn_id': row.source_turn_id, 'native_thread_id': row.thread_id,
                                'boundary_event_id': row.boundary_event_id})
                        for owner in self.owners: await control.release(owner, db=db)
                    await db.commit()
            finally:
                for task in [*heartbeats, watcher]: task.cancel()
                await asyncio.gather(*heartbeats, watcher, return_exceptions=True)
                if self.session: self.session.turn_execution = None
                RUNS.pop(self.id, None); unregister('codex_fork:' + self.id)


async def submit(cid, user_id, turn_id, request_id):
    digest = codex_turn_records.digest({'source_turn_id': turn_id})
    async def allocate():
        async with SessionLocal() as db:
            conversation = await authorize(db, cid, user_id)
            async with coordinate(db, await db.get(Case, conversation.case_id)):
                existing = await db.scalar(select(NativeCliFork).where(NativeCliFork.source_conversation_id == cid,
                    NativeCliFork.created_by == user_id, NativeCliFork.client_request_id == request_id))
                if existing is not None:
                    if existing.request_sha256 != digest:
                        raise ExecutionConflict('idempotency_conflict', '分支请求ID已绑定其他回合')
                    return await public(db, existing)
                await control.assert_idle(db, cid)
                if await control.unsafe_native(db, cid):
                    raise ExecutionConflict('recovery_required', '来源会话仍待收尾')
                if await db.scalar(select(ToolCall.id).where(ToolCall.conversation_id == cid,
                    ToolCall.status.in_(('pending','queued','running'))).limit(1)) is not None:
                    raise ExecutionConflict('state_conflict', '请先处理来源会话的工具请求')
                conversation, prior_turn, prior, boundary = await source(db, cid, user_id, turn_id)
                selected = json.loads(prior.profile_json)
                if selected.get('credential') is None:
                    raise ExecutionConflict('input_stale', '原生分叉需要绑定API配置的来源')
                target = Conversation(case_id=conversation.case_id, title=conversation.title[:290] + ' · 原生分叉',
                    forked_from_id=cid, branch_head_event_id=boundary['id'], provider_profile_id=conversation.provider_profile_id,
                    model=conversation.model, permission_mode=conversation.permission_mode)
                db.add(target); await db.flush()
                identifier = str(uuid.uuid4())
                owners = [await control.acquire(db, item, 'codex', identifier) for item in (cid, target.id)]
                row = NativeCliFork(id=identifier, source_conversation_id=cid, conversation_id=target.id,
                    source_turn_id=prior_turn.id, boundary_event_id=boundary['id'], case_id=conversation.case_id,
                    created_by=user_id, client_request_id=request_id, request_sha256=digest)
                db.add(row); await db.flush()
            await db.commit()
            value = await public(db, row)
        execution = Execution(identifier, owners, user_id, selected)
        RUNS[identifier] = execution
        register('codex_fork', identifier, conversation.case_id, execution.stop)
        execution.task = asyncio.create_task(execution.run())
        execution.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return value
    return await finish_before_cancel(allocate())


async def cancel(identifier, user_id):
    value = await get(identifier, user_id)
    execution = RUNS.get(identifier)
    if execution is not None:
        from . import audit
        async with SessionLocal.begin() as db:
            row = await db.get(NativeCliFork, identifier)
            await audit.session_event(db, row.case_id, user_id, 'codex.fork.cancel.requested', 'native_fork', identifier)
        await execution.stop()
        return await get(identifier, user_id)
    if value['phase'] not in {'completed','failed'}:
        raise ExecutionConflict('recovery_required', '原生分支归属不可用；历史仅供读取')
    return value
