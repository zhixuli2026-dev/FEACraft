"""Idempotent conversation CLI startup; observers never launch or take over."""
import asyncio
import json
import uuid
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy import select, update

from ..db import SessionLocal
from ..models import Case, ConversationExecution, NativeCliSession, NativeCliSessionRequest, ToolCall
from . import codex_sessions as sessions, codex_records, codex_credentials as credentials
from . import codex_conversations as conversations, codex_turn_records as turn_records
from . import conversation_execution as control
from .async_ops import finish_before_cancel
from .codex_sessions import control_case as coordinate
from .durable_events import json_text, now
from .supervisor import register, unregister
from .workflow_state import ExecutionConflict
from . import request_deadline

RUNS = {}
STARTUP_SECONDS = request_deadline.CLI_STARTUP_SECONDS
TERMINAL = {'completed', 'failed', 'cancelled', 'recovery_required'}
FIELDS = {'provider_profile_id', 'provider_wire_api', 'runtime', 'purpose'}
SELECTION = {'case_id', 'conversation_id', 'user_id', 'runtime', 'source_prefix', 'permission_mode',
             'model', 'credential', 'conversation_provider_id'}


def request_value(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('Invalid native session request')
    pid = value['provider_profile_id']
    if (value['runtime'] not in {'windows', 'wsl', 'docker'} or value['purpose'] not in {'chat', 'inspect'}
            or (pid is not None and (type(pid) is not int or pid <= 0))
            or value['provider_wire_api'] not in {None, 'responses'}):
        raise ValueError('Invalid native session request selection')
    return value


def selection(row):
    request = request_value(json.loads(row.request_json))
    selected = json.loads(row.selection_json)
    if (not isinstance(selected, dict) or set(selected) != SELECTION
            or row.request_sha256 != turn_records.digest(request)
            or selected['case_id'] != row.case_id or selected['conversation_id'] != row.conversation_id
            or selected['user_id'] != row.created_by or selected['runtime'] != request['runtime']):
        raise ValueError('Invalid native session request binding')
    credential = selected['credential']
    if credential is not None:
        credentials.validate(credential)
        if credential['profile_id'] != request['provider_profile_id']:
            raise ValueError('Invalid native startup credential')
    elif request['provider_profile_id'] is not None:
        raise ValueError('Native startup credential is missing')
    return request, selected


async def public(db, row, *, refresh=True):
    phase = row.phase
    session = await db.get(NativeCliSession, row.id, populate_existing=True)
    # A registration commit can land between the parent and child reads.
    if refresh and session is not None and row.session_id is None:
        return await public(db, await db.get(NativeCliSessionRequest, row.id, populate_existing=True), refresh=False)
    native = codex_records.public(session) if session is not None else None
    valid = True
    try:
        request, selected = selection(row)
        valid = (row.phase in TERMINAL | {'admitted', 'starting'} and row.version >= 0
                 and bool(row.client_request_id) and row.session_id in {None, row.id}
                 and ((row.ended_at is not None) == (row.phase in TERMINAL)))
        if session is not None:
            actual = json.loads(session.profile_json)
            valid = valid and row.session_id == session.id and all(actual.get(k) == selected[k] for k in SELECTION)
            valid = valid and session.created_by == row.created_by and session.conversation_id == row.conversation_id
        elif row.session_id is not None:
            valid = False
        if row.not_started and (session is not None or row.phase not in {'failed', 'cancelled'}):
            valid = False
        if row.phase == 'completed' and (session is None or session.phase not in {'active','closed','failed'}):
            valid = False
        if row.phase in {'failed','cancelled'} and not (row.not_started or native and
                native['phase'] in {'closed','failed'} and native['cleanup_verified'] is True):
            valid = False
    except (ValueError, TypeError, KeyError):
        request, valid = {}, False
    if not valid or native and native['phase'] == 'evidence_invalid':
        phase = 'evidence_invalid'
    elif native and native['phase'] == 'recovery_required':
        phase = 'recovery_required'
    elif phase not in TERMINAL:
        owner = await db.get(ConversationExecution, row.conversation_id, populate_existing=True)
        try:
            if owner is not None: control.document(owner)
            if (owner is None or owner.phase != 'active' or owner.backend != 'codex' or owner.resource_id != row.id
                    or owner.lease_expires_at.replace(tzinfo=timezone.utc) <= now()):
                if refresh and (owner is None or owner.phase == 'idle' or owner.resource_id != row.id):
                    return await public(db, await db.get(NativeCliSessionRequest, row.id, populate_existing=True), refresh=False)
                phase = 'recovery_required'
            else:
                from . import process_identity_native as identity
                if identity.probe(json.loads(owner.owner_identity_json))['state'] != 'match':
                    phase = 'recovery_required'
        except (ValueError, TypeError, KeyError, OSError):
            phase = 'evidence_invalid'
    return {'id': row.id, 'case_id': row.case_id, 'conversation_id': row.conversation_id,
            'client_request_id': row.client_request_id, 'phase': phase, 'runtime': request.get('runtime'),
            'purpose': request.get('purpose'), 'session': native, 'error': row.error,
            'cleanup_verified': True if row.not_started else native['cleanup_verified'] if native else None,
            'created_at': row.created_at.isoformat(), 'ended_at': row.ended_at.isoformat() if row.ended_at else None}


async def get(identifier, user_id):
    async with SessionLocal() as db:
        row = await db.get(NativeCliSessionRequest, identifier)
        if row is None or row.created_by != user_id:
            raise HTTPException(404, '原生会话启动请求不存在')
        await conversations.authorize(db, row.conversation_id, user_id)
        return await public(db, row)


async def listing(db, cid, user_id, request_id=None):
    await conversations.authorize(db, cid, user_id)
    query = select(NativeCliSessionRequest).where(NativeCliSessionRequest.conversation_id == cid,
        NativeCliSessionRequest.created_by == user_id)
    if request_id is not None:
        query = query.where(NativeCliSessionRequest.client_request_id == request_id)
    rows = (await db.scalars(query.order_by(NativeCliSessionRequest.created_at.desc()).limit(50))).all()
    return [await public(db, row) for row in rows]


class Execution:
    def __init__(self, identifier, owner, user_id, requested, selected):
        self.id, self.owner, self.user_id = identifier, owner, user_id
        self.requested, self.selected = requested, selected
        self.task = self.session = None
        self.entered = self.finalizing = self.user_cancel = False
        self.cancel = asyncio.Event()

    async def current(self, db, *, launch=True):
        await control.current(db, self.owner, launch=launch)
        found = await db.execute(update(NativeCliSessionRequest).where(NativeCliSessionRequest.id == self.id,
            NativeCliSessionRequest.phase.in_(('admitted','starting'))).values(version=NativeCliSessionRequest.version + 1))
        if found.rowcount != 1:
            raise ExecutionConflict('state_conflict', 'CLI startup ownership changed')
        row = await db.get(NativeCliSessionRequest, self.id, populate_existing=True)
        if selection(row) != (self.requested, self.selected):
            raise ExecutionConflict('evidence_invalid', 'CLI startup selection changed')
        return row

    async def guard(self, db):
        if self.cancel.is_set(): raise asyncio.CancelledError
        await self.current(db)
        conv = await conversations.authorize(db, self.owner.conversation_id, self.user_id)
        case = await db.get(Case, conv.case_id)
        if (conv.model != self.selected['model'] or conv.permission_mode != self.selected['permission_mode']
                or conv.provider_profile_id != self.selected['conversation_provider_id']
                or case.s3_prefix != self.selected['source_prefix']):
            raise ExecutionConflict('input_stale', '会话设置在启动前发生变化')
        if self.selected['credential'] is not None:
            await credentials.check(db, self.selected['credential'], self.user_id)

    async def registered(self, db, session):
        row = await self.current(db)
        if any(session.selected.get(k) != self.selected[k] for k in SELECTION):
            raise ExecutionConflict('input_stale', 'CLI startup environment changed')
        row.session_id = session.id

    async def stop(self, *, error=None):
        self.user_cancel = error is None
        self.cancel.set()
        if self.entered and self.task is not None and not self.finalizing:
            self.task.cancel()
        if self.task is not None and self.task is not asyncio.current_task():
            await finish_before_cancel(asyncio.gather(self.task, return_exceptions=True))

    async def run(self):
        self.entered = True
        heartbeat = asyncio.create_task(control.watch(self.owner, self.cancel))
        async def cancelled():
            await self.cancel.wait()
            if not self.finalizing: self.task.cancel()
        watcher = asyncio.create_task(cancelled())
        delivered = commit_started = False
        failure = None
        try:
            async with request_deadline.scope(STARTUP_SECONDS):
                async with SessionLocal() as db:
                    async with coordinate(db, await db.get(Case, self.selected['case_id'])):
                        await self.guard(db)
                        row = await self.current(db); row.phase = 'starting'
                    await db.commit()
                self.session = sessions.Session(self.selected['case_id'], self.owner.conversation_id, self.user_id,
                    provider_profile_id=self.requested['provider_profile_id'], provider_wire_api=self.requested['provider_wire_api'],
                    runtime=self.requested['runtime'])
                self.session.id = self.id
                self.session.startup_guard, self.session.on_registered = self.guard, self.registered
                await self.session.start()
                self.session.startup_guard = self.session.on_registered = None
                if self.cancel.is_set(): raise asyncio.CancelledError
                async with self.session.access() as db:
                    row = await self.current(db)
                    state = codex_records.public(await db.get(NativeCliSession, row.session_id))
                    if state['phase'] != 'active':
                        raise ExecutionConflict('evidence_invalid', 'CLI startup did not become active')
                    row.phase, row.ended_at = 'completed', now()
                    await db.flush()
                    await control.release(self.owner, db=db)
                    commit_started = True
                delivered = True
        except BaseException as error:
            failure = type(error).__name__
        finally:
            self.finalizing = True
            if self.session is not None:
                self.session.startup_guard = self.session.on_registered = None
            certain = not commit_started
            if commit_started and not delivered:
                # Acquire the same case write lock before reading: a still
                # committing transaction must settle before this observation.
                # Never close a session another request may already be using.
                try:
                    async with SessionLocal() as db:
                        async with coordinate(db, await db.get(Case, self.selected['case_id']), allow_deleted=True):
                            row = await db.get(NativeCliSessionRequest, self.id, populate_existing=True)
                            if row.phase == 'completed' and row.session_id == self.id:
                                delivered = True
                            else:
                                await self.current(db, launch=False); certain = True
                except Exception:
                    certain = False
            try:
                if not delivered and certain:
                    if self.session is not None:
                        try:
                            await self.session.close(error=failure or 'StartupInterrupted')
                        except Exception as error:
                            failure = failure or type(error).__name__
                    async with SessionLocal() as db:
                        async with coordinate(db, await db.get(Case, self.selected['case_id']), allow_deleted=True):
                            row = await self.current(db, launch=False)
                            session = await db.get(NativeCliSession, self.id)
                            if session is not None: row.session_id = session.id
                            row.not_started = session is None and (self.session is None or self.session.cleanup_verified is True)
                            safe = row.not_started or session is not None and codex_records.public(session)['cleanup_verified'] is True
                            if self.session is not None and self.session.pending_stage(): safe = False
                            row.phase = ('cancelled' if self.user_cancel else 'failed') if safe else 'recovery_required'
                            row.error, row.ended_at = failure or 'StartupInterrupted', now()
                            await db.flush()
                            await control.release(self.owner, db=db)
                        await db.commit()
            finally:
                for task in (heartbeat, watcher): task.cancel()
                await asyncio.gather(heartbeat, watcher, return_exceptions=True)
                RUNS.pop(self.id, None); unregister('codex_start:' + self.id)


async def submit(cid, user_id, request_id, requested):
    request_value(requested)
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128 or '\0' in request_id:
        raise ValueError('Invalid native startup request ID')
    digest = turn_records.digest(requested)
    async def allocate():
        async with SessionLocal() as db:
            conv = await conversations.authorize(db, cid, user_id)
            async with coordinate(db, await db.get(Case, conv.case_id)):
                conv = await conversations.authorize(db, cid, user_id)
                row = await db.scalar(select(NativeCliSessionRequest).where(NativeCliSessionRequest.conversation_id == cid,
                    NativeCliSessionRequest.created_by == user_id, NativeCliSessionRequest.client_request_id == request_id))
                if row is not None:
                    if row.request_sha256 != digest:
                        raise ExecutionConflict('idempotency_conflict', '启动请求ID已绑定其他参数')
                    return await public(db, row)
                await control.assert_idle(db, cid)
                if await control.unsafe_native(db, cid):
                    raise ExecutionConflict('recovery_required', '会话仍有待核验的原生执行')
                if await db.scalar(select(ToolCall.id).where(ToolCall.conversation_id == cid,
                    ToolCall.status.in_(('pending','queued','running'))).limit(1)) is not None:
                    raise ExecutionConflict('state_conflict', '请先处理待审批的工具')
                credential, secret = await credentials.select(db, requested['provider_profile_id'], user_id, requested['provider_wire_api'])
                if secret and secret in request_id: raise ValueError('Startup request ID contains credential data')
                case = await db.get(Case, conv.case_id)
                selected = {'case_id': case.id, 'conversation_id': cid, 'user_id': user_id,
                    'runtime': requested['runtime'], 'source_prefix': case.s3_prefix, 'permission_mode': conv.permission_mode,
                    'model': conv.model, 'credential': credential, 'conversation_provider_id': conv.provider_profile_id}
                identifier = str(uuid.uuid4())
                owner = await control.acquire(db, cid, 'codex', identifier)
                row = NativeCliSessionRequest(id=identifier, case_id=case.id, conversation_id=cid, created_by=user_id,
                    client_request_id=request_id, request_sha256=digest, request_json=json_text(requested), selection_json=json_text(selected))
                db.add(row); await db.flush()
            await db.commit()
            value = await public(db, row)
        execution = Execution(identifier, owner, user_id, requested, selected)
        RUNS[identifier] = execution
        register('codex_start', identifier, case.id, execution.stop)
        execution.task = asyncio.create_task(execution.run())
        execution.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return value
    return await finish_before_cancel(allocate())


async def cancel(identifier, user_id):
    value = await get(identifier, user_id)
    execution = RUNS.get(identifier)
    if execution is not None and execution.user_id == user_id:
        from . import audit
        await audit.record_request(value['case_id'], user_id, 'codex.startup.cancel.requested', 'native_startup', identifier)
        await execution.stop()
        return await get(identifier, user_id)
    if value['phase'] not in {'completed','failed','cancelled'}:
        raise ExecutionConflict('recovery_required', '启动归属不可用，不能自动重放或接管')
    return value


async def close_all():
    results = await asyncio.gather(*(run.stop(error='ServerShutdown') for run in list(RUNS.values())), return_exceptions=True)
    if any(isinstance(value, BaseException) for value in results):
        raise RuntimeError('Some native startup requests could not be finalized')
