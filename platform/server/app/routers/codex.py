"""Owned CLI session API; callers cannot select arbitrary commands or homes."""
import json
import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..db import SessionLocal, get_db
from ..deps import get_current_user
from ..models import NativeCliSession, NativeCliApproval, NativeCliEvent, NativeCliTurn, User
from ..services import codex_records as records, codex_sessions as sessions, codex_turns as turns, codex_turn_records
from ..services.codex_stdio import CodexRpcError
from ..services.async_ops import finish_before_cancel
from ..services.request_lifecycle import wait
from ..services.workflow_state import ExecutionConflict
from ..services.case_write import CaseWriteConflict
from ..services import codex_conversations

router = APIRouter(prefix='/api/v1/codex', tags=['codex'])


@router.get('/capabilities')
async def capabilities(user: User = Depends(get_current_user)):
    from ..config import get_settings
    return {'docker':bool(get_settings().acp_docker_image)}


class ForkIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_turn_id: str = Field(min_length=1, max_length=36)
    client_request_id: str = Field(min_length=1, max_length=128, pattern=r'^[^\x00]+$')


@router.post('/conversations/{cid}/fork')
async def fork_conversation(cid: int, body: ForkIn, request: Request, user: User = Depends(get_current_user)):
    from ..services import codex_forks
    return await execute(codex_forks.submit(cid, user.id, body.source_turn_id, body.client_request_id), request)


@router.get('/conversations/{cid}/forks')
async def conversation_forks(cid: int, client_request_id: str | None = None, user: User = Depends(get_current_user), db=Depends(get_db)):
    from ..services import codex_forks
    return await codex_forks.listing(db, cid, user.id, client_request_id)


@router.get('/forks/{identifier}')
async def get_fork(identifier: str, user: User = Depends(get_current_user)):
    from ..services import codex_forks
    return await codex_forks.get(identifier, user.id)


@router.post('/forks/{identifier}:cancel')
async def cancel_fork(identifier: str, request: Request, user: User = Depends(get_current_user)):
    from ..services import codex_forks
    return await execute(codex_forks.cancel(identifier, user.id), request)


@router.get('/conversations/{cid}/turns')
async def conversation_turns(cid: int, before: str | None = None, client_message_id: str | None = None,
                             limit: int = 20, user: User = Depends(get_current_user), db=Depends(get_db)):
    return await codex_conversations.page(db, cid, user.id, before=before, client_message_id=client_message_id, limit=limit)


@router.get('/conversations/{cid}/activity')
async def conversation_activity(cid: int, user: User = Depends(get_current_user), db=Depends(get_db)):
    return await codex_conversations.activity(db, cid, user.id)


class ConversationSettingsIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider_profile_id: int = Field(gt=0)
    model: str = Field(default='', max_length=200)
    permission_mode: Literal['approve_writes', 'full_access', 'ask', 'plan']


@router.patch('/conversations/{cid}/settings')
async def conversation_settings(cid: int, body: ConversationSettingsIn, user: User = Depends(get_current_user), db=Depends(get_db)):
    from ..models import Case, ToolCall
    from ..routers.agent import _owned_profile, _conv_out
    from ..services import conversation_execution as control
    from ..services.durable_events import lock_case_execution
    from ..services.case_write import assert_case_available
    from ..services.storage_operations import admit_locked
    from ..services.case_validation import assert_writable_locked
    conversation = await codex_conversations.authorize(db, cid, user.id)
    case = await db.get(Case, conversation.case_id)
    try:
        # These fields only change SQL metadata. Serialize against execution
        # admission without reserving remote file capacity or recovering files.
        await lock_case_execution(db, case.id)
        await db.refresh(case)
        assert_case_available(case)
        conversation = await codex_conversations.authorize(db, cid, user.id)
        await admit_locked(db, case.id)
        await assert_writable_locked(db, case.id)
        await control.assert_idle(db, cid)
        if await control.unsafe_native(db, cid):
            raise ExecutionConflict('recovery_required', '原生执行尚未完成收尾')
        if await db.scalar(select(ToolCall.id).where(ToolCall.conversation_id == cid,
                ToolCall.status.in_(('pending', 'queued', 'running'))).limit(1)) is not None:
            raise ExecutionConflict('state_conflict', '请先处理待审批的工具')
        await _owned_profile(db, body.provider_profile_id, user.id)
        conversation.provider_profile_id = body.provider_profile_id
        conversation.model = body.model
        conversation.permission_mode = body.permission_mode
        await finish_before_cancel(db.commit())
    except ExecutionConflict as error:
        raise HTTPException(409, {'code': error.code, 'message': str(error)}) from error
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    return _conv_out(conversation)


class SessionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    case_id: int | None = Field(default=None, gt=0)
    conversation_id: int | None = Field(default=None, gt=0)
    provider_profile_id: int | None = Field(default=None, gt=0)
    provider_wire_api: Literal['responses'] | None = None
    runtime: Literal['windows', 'wsl', 'docker'] = 'windows'


class SessionRequestIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_request_id: str = Field(min_length=1, max_length=128, pattern=r'^[^\x00]+$')
    provider_profile_id: int | None = Field(default=None, gt=0)
    provider_wire_api: Literal['responses'] | None = None
    runtime: Literal['windows', 'wsl', 'docker'] = 'windows'
    purpose: Literal['chat', 'inspect'] = 'chat'


@router.post('/conversations/{cid}/session-requests')
async def create_session_request(cid: int, body: SessionRequestIn, request: Request, user: User = Depends(get_current_user)):
    from ..services import codex_session_requests as startups
    return await execute(startups.submit(cid, user.id, body.client_request_id,
        body.model_dump(exclude={'client_request_id'})), request)


@router.get('/conversations/{cid}/session-requests')
async def session_requests(cid: int,
        client_request_id: str | None = Query(default=None, min_length=1, max_length=128, pattern=r'^[^\x00]+$'),
        user: User = Depends(get_current_user), db=Depends(get_db)):
    from ..services import codex_session_requests as startups
    return await startups.listing(db, cid, user.id, client_request_id)


@router.get('/session-requests/{identifier}')
async def session_request(identifier: str, user: User = Depends(get_current_user)):
    from ..services import codex_session_requests as startups
    return await startups.get(identifier, user.id)


@router.post('/session-requests/{identifier}:cancel')
async def cancel_session_request(identifier: str, request: Request, user: User = Depends(get_current_user)):
    from ..services import codex_session_requests as startups
    return await execute(startups.cancel(identifier, user.id), request)


class ThreadIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    resume: bool = False


class NativeImageIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='image', min_length=1, max_length=255)
    mime: Literal['image/png', 'image/jpeg', 'image/gif', 'image/webp']
    data: str = Field(min_length=1, max_length=4 * ((4 * 1024 * 1024 + 2) // 3) + 64)


class NativeModelOptionsIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    effort: str | None = Field(default=None, min_length=1, max_length=64, pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
    summary: Literal['auto', 'concise', 'detailed', 'none'] | None = None
    service_tier: str | None = Field(default=None, min_length=1, max_length=64, pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


class TurnIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(default='', max_length=128 * 1024)
    client_message_id: str = Field(min_length=1, max_length=128)
    goal: bool = False
    timeout_seconds: int = Field(default=600, ge=1, le=3600)
    resume: bool = False
    goal_source_turn_id: str | None = Field(default=None, min_length=1, max_length=36)
    images: list[NativeImageIn] = Field(default_factory=list, max_length=3)
    model_options: NativeModelOptionsIn | None = None


async def execute(awaitable, request):
    try:
        return await wait(awaitable, request=request)
    except records.CliSessionCapacity as error:
        raise HTTPException(429, {'code': error.code, 'message': str(error)}) from error
    except ExecutionConflict as error:
        raise HTTPException(409, {'code': error.code, 'message': str(error)}) from error
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    except CodexRpcError as error:
        raise HTTPException(502, 'CLI 请求被拒绝，请检查账户或运行环境') from error
    except (RuntimeError, ValueError, OSError, EOFError) as error:
        raise HTTPException(502, 'CLI 执行或收尾失败，请查看会话记录') from error


@router.post('/sessions')
async def create(body: SessionIn, request: Request, user: User = Depends(get_current_user)):
    if body.conversation_id is not None and body.case_id is None:
        raise HTTPException(400, 'CLI 会话需要明确的算例绑定')
    async def work():
        session = None
        try:
            session = await sessions.create(body.case_id, body.conversation_id, user.id,
                provider_profile_id=body.provider_profile_id, provider_wire_api=body.provider_wire_api, runtime=body.runtime)
            return records.public(await sessions.row(session.id, user.id))
        except BaseException as error:
            if session is not None:
                try:
                    await finish_before_cancel(session.close(error='StartupResponseLost'))
                except Exception as cleanup_error:
                    error.add_note('CLI cleanup: ' + type(cleanup_error).__name__)
            raise
    return await execute(work(), request)


@router.get('/sessions')
async def listing(user: User = Depends(get_current_user)):
    async with SessionLocal() as db:
        rows = (await db.scalars(select(NativeCliSession).where(NativeCliSession.created_by == user.id)
                                .order_by(NativeCliSession.created_at.desc()).limit(100))).all()
        return [records.public(row) for row in rows]


@router.get('/sessions/{identifier}')
async def get(identifier: str, user: User = Depends(get_current_user)):
    return records.public(await sessions.row(identifier, user.id))


@router.get('/sessions/{identifier}/staging')
async def staging(identifier: str, request: Request, path: str | None = Query(None, min_length=1, max_length=1024),
                  user: User = Depends(get_current_user)):
    from ..services.codex_docker_stage import inspect
    return await execute(inspect(identifier,user.id,path=path),request)


@router.post('/sessions/{identifier}:reconcile-initialization')
async def reconcile_initialization(identifier: str, request: Request, user: User = Depends(get_current_user)):
    from ..services.cli_initialization_recovery import recover
    return await execute(recover(identifier, user.id), request)


@router.post('/sessions/{identifier}:reconcile-scope')
async def reconcile_scope(identifier: str, request: Request, user: User = Depends(get_current_user)):
    from ..services.cli_session_recovery import recover
    return await execute(recover(identifier,user.id),request)


@router.post('/turns/{identifier}:reconcile-staging')
async def reconcile_turn_staging(identifier: str, request: Request, user: User = Depends(get_current_user)):
    from ..services.codex_staging_recovery import reconcile
    return await execute(reconcile(identifier, user.id), request)


@router.delete('/sessions/{identifier}')
async def close(identifier: str, request: Request, user: User = Depends(get_current_user)):
    async def work():
        row = await sessions.row(identifier, user.id)
        session = sessions.SESSIONS.get(identifier)
        if session is not None and session.user_id == user.id:
            from ..services import audit
            await audit.record_request(row.case_id, user.id, 'codex.session.close.requested', 'native_session', identifier)
            await session.close()
            return records.public(await sessions.row(identifier, user.id))
        value = records.public(row)
        if value['phase'] not in {'closed', 'failed'} or value['cleanup_verified'] is not True:
            raise ExecutionConflict('recovery_required', 'CLI owner unavailable; history is read-only')
        selected = json.loads(row.profile_json)
        stage = selected['staging_prefix']
        if stage is not None:
            from .. import storage
            if any(path.exists() for path in (storage._fs_path(stage, '.'), storage._staging_manifest_path(stage), storage._terminal_cleanup_marker(stage))):
                raise ExecutionConflict('recovery_required', 'CLI staging remains pending')
        return value
    return await execute(work(), request)


@router.get('/sessions/{identifier}/account')
async def account(identifier: str, request: Request, user: User = Depends(get_current_user)):
    async def work():
        session = await sessions.live(identifier, user.id)
        result = await session.rpc('account')
        account = result.get('account')
        if isinstance(account, dict):
            account = {key: account[key] for key in ('type', 'id', 'email', 'planType') if key in account}
        elif account is not None:
            raise ValueError('Invalid account response')
        from ..services.codex_credentials import public as credential_public
        return {'account': account, 'requiresOpenaiAuth': bool(result.get('requiresOpenaiAuth')),
                'credential': credential_public(session.credential)}
    return await execute(work(), request)


@router.get('/sessions/{identifier}/models')
async def models(identifier: str, request: Request, user: User = Depends(get_current_user),
                 cursor: str | None = Query(default=None, max_length=4096), limit: int = Query(default=50, ge=1, le=100)):
    async def work():
        session = await sessions.live(identifier, user.id)
        result = await session.rpc('models', cursor=cursor, limit=limit)
        from ..services.codex_model_options import public_model
        return {'data': [public_model(item) for item in result['data']],
                'nextCursor': result.get('nextCursor')}
    return await execute(work(), request)


@router.get('/sessions/{identifier}/sandbox')
async def sandbox(identifier: str, request: Request, user: User = Depends(get_current_user)):
    async def work():
        session = await sessions.live(identifier, user.id)
        value = await session.rpc('sandbox')
        return {'runtime': session.runtime, 'legacy_readiness': value['status'],
                'windows_override': (sessions.windows_sandbox_override(session.selected['argv'])
                                     if session.runtime == 'windows' else None),
                'mxc_availability': 'not_applicable' if session.runtime != 'windows' else 'not_probed',
                'setup_authority': 'deployment_operator'}
    return await execute(work(), request)


@router.post('/sessions/{identifier}/thread')
async def thread(identifier: str, request: Request, body: ThreadIn | None = None, user: User = Depends(get_current_user)):
    async def work():
        session = await sessions.live(identifier, user.id)
        return await session.create_thread(resume=bool(body and body.resume))
    return await execute(work(), request)


async def submit_turn(identifier, user_id, body, *, foreground=False):
    try:
        return await turns.submit(identifier, user_id, body.content, body.client_message_id, goal=body.goal,
            seconds=body.timeout_seconds, resume=body.resume, goal_source_turn_id=body.goal_source_turn_id,
            foreground=foreground, images=body.images, model_options=body.model_options)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


async def turn_events(identifier, user_id, after=0, *, cancel_on_disconnect=False):
    cursor = after
    terminal_seen = False
    try:
        while True:
            state = await turns.row(identifier, user_id)
            async with SessionLocal() as db:
                events = (await db.scalars(select(NativeCliEvent).where(NativeCliEvent.turn_id == identifier,
                    NativeCliEvent.seq > cursor).order_by(NativeCliEvent.seq).limit(100))).all()
            for event in events:
                payload = {'type': event.type, 'turn_id': identifier, 'seq': event.seq, 'payload': json.loads(event.payload_json)}
                yield f'id: {event.seq}\nevent: {event.type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
                cursor = event.seq
                terminal_seen = terminal_seen or event.type == 'stream.done'
            if state['phase'] in codex_turn_records.TERMINAL and cursor >= state['last_seq']:
                return
            if state['phase'] in {'recovery_required', 'evidence_invalid'}:
                yield 'event: execution.unavailable\ndata: ' + json.dumps({'type': 'execution.unavailable', 'phase': state['phase']}) + '\n\n'
                return
            if not events:
                yield ': keep-alive\n\n'
                await asyncio.sleep(.1)
    finally:
        if cancel_on_disconnect and not terminal_seen:
            execution = turns.RUNS.get(identifier)
            if execution is not None and execution.session.user_id == user_id:
                await finish_before_cancel(execution.stop())


@router.post('/sessions/{identifier}/turns')
async def create_turn(identifier: str, body: TurnIn, request: Request, user: User = Depends(get_current_user)):
    async def work():
        value, _created = await submit_turn(identifier, user.id, body)
        return value
    return await execute(work(), request)


@router.post('/sessions/{identifier}/turns:stream')
async def create_turn_stream(identifier: str, body: TurnIn, request: Request,
                             user: User = Depends(get_current_user), db=Depends(get_db)):
    user_id = user.id
    value, created = await execute(submit_turn(identifier, user_id, body, foreground=True), request)
    await db.rollback()
    return StreamingResponse(turn_events(value['id'], user_id, cancel_on_disconnect=created), media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.get('/turns/{identifier}')
async def get_turn(identifier: str, user: User = Depends(get_current_user)):
    return await turns.row(identifier, user.id)


@router.get('/turns/{identifier}/events')
async def watch_turn(identifier: str, request: Request, after: int = 0, user: User = Depends(get_current_user), db=Depends(get_db)):
    if after == 0 and request.headers.get('last-event-id'):
        try:
            after = int(request.headers['last-event-id'])
        except ValueError as error:
            raise HTTPException(400, '无效的事件游标') from error
    if after < 0:
        raise HTTPException(400, '事件游标必须非负')
    user_id = user.id
    state = await turns.row(identifier, user_id)
    if after > state['last_seq']:
        raise HTTPException(400, '事件游标超出已提交范围')
    await db.rollback()
    return StreamingResponse(turn_events(identifier, user_id, after), media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.post('/turns/{identifier}:cancel')
async def cancel_turn(identifier: str, request: Request, user: User = Depends(get_current_user)):
    return await execute(turns.cancel(identifier, user.id), request)


@router.post('/turns/{identifier}:reconcile')
async def reconcile_turn(identifier: str, request: Request, user: User = Depends(get_current_user)):
    from ..services.codex_turn_recovery import recover
    return await execute(recover(identifier,user.id),request)


@router.get('/turns/{identifier}/approvals')
async def approvals(identifier: str, user: User = Depends(get_current_user)):
    await turns.row(identifier, user.id)
    async with SessionLocal() as db:
        rows = (await db.scalars(select(NativeCliApproval).where(NativeCliApproval.turn_id == identifier)
                                .order_by(NativeCliApproval.created_at))).all()
        return [{'id': row.id, 'method': row.method, 'phase': row.phase, 'params': json.loads(row.params_json)} for row in rows]


@router.post('/turns/{identifier}/approvals/{approval_id}:respond')
async def answer(identifier: str, approval_id: str, body: dict, request: Request, user: User = Depends(get_current_user)):
    async def work():
        await turns.row(identifier, user.id)
        execution = turns.RUNS.get(identifier)
        if execution is None or execution.session.user_id != user.id:
            raise ExecutionConflict('recovery_required', 'Native turn owner unavailable; approval cannot be replayed')
        try:
            return await execution.answer(approval_id, body)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
    return await execute(work(), request)
