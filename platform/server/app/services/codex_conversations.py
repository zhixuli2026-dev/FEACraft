"""Read-only native conversation discovery for reload and response-loss recovery."""
from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from ..models import Conversation, ConversationExecution, NativeCliSession, NativeCliTurn, NativeCliFork, NativeCliSessionRequest, User
from . import codex_turn_records as turns, conversation_execution as control


async def append_answer(db, turn, text, model):
    from ..routers.agent import _append_event

    await _append_event(db, turn.conversation_id, 'message', {
        'role': 'assistant', 'content': text, 'native_turn_id': turn.native_turn_id,
        'native_turn_complete': True, 'native_thread_id': turn.thread_id,
        'native_execution_id': turn.id, 'backend': 'codex', 'model': model,
    })


async def context(db, cid, user_id, *, resume, goal_source_turn_id=None):
    """Freeze visible text while admission holds the case/conversation fence."""
    from ..routers.agent import _load_events
    from .codex_history import build_text_handoff, MAX_HANDOFF_CHARS
    from .workflow_state import ExecutionConflict
    events = await _load_events(db, cid)
    if resume:
        from .codex_forks import public as fork_public
        previous = await db.scalar(select(NativeCliTurn).where(NativeCliTurn.conversation_id == cid,
            NativeCliTurn.created_by == user_id, NativeCliTurn.phase == 'completed')
            .order_by(NativeCliTurn.created_at.desc(), NativeCliTurn.id.desc()).limit(1))
        visible = [e for e in events if e['type'] == 'message' or
                   e['type'] == 'system' and e.get('payload', {}).get('kind') in {'summary', 'handoff'}]
        if goal_source_turn_id is not None and (previous is None or previous.id != goal_source_turn_id
                or previous.mode != 'goal' or (await turns.public(db, previous))['cleanup_verified'] is not True):
            raise ExecutionConflict('input_stale', '等待继续的目标回合已变化，请刷新后重新选择')
        if previous is None:
            fork = await db.scalar(select(NativeCliFork).where(NativeCliFork.conversation_id == cid, NativeCliFork.created_by == user_id))
            if fork is not None and (await fork_public(db, fork))['phase'] == 'completed' and visible and visible[-1]['id'] == fork.boundary_event_id:
                return ''
        if previous is None or not visible or visible[-1].get('payload', {}).get('native_execution_id') != previous.id:
            raise ExecutionConflict('input_stale', '会话历史已变化，请使用文字交接开启新线程')
        return ''
    has_selected_handoff = any(e['type'] == 'system' and e['payload'].get('kind') == 'handoff'
        and e['payload'].get('handoff_mode') == 'bounded_text' for e in events)
    return build_text_handoff(events, keep_chars=MAX_HANDOFF_CHARS if has_selected_handoff else 32000)


async def authorize(db, cid, user_id):
    from ..routers.workspaces import get_owned_case
    user = await db.get(User, user_id, populate_existing=True)
    if user is None or user.status != 'active':
        raise HTTPException(403, '用户已不可用')
    conversation = await db.get(Conversation, cid, populate_existing=True)
    if conversation is None:
        raise HTTPException(404, '会话不存在')
    await get_owned_case(conversation.case_id, user, db)
    return conversation


async def page(db, cid, user_id, *, before=None, client_message_id=None, limit=20):
    await authorize(db, cid, user_id)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise HTTPException(400, '每页回合数量必须为 1–50')
    if before is not None and (not isinstance(before, str) or not 1 <= len(before) <= 36):
        raise HTTPException(400, '无效的历史游标')
    if client_message_id is not None and (not isinstance(client_message_id, str)
            or not 1 <= len(client_message_id) <= 128 or '\0' in client_message_id or before is not None):
        raise HTTPException(400, '无效的消息查询')
    query = select(NativeCliTurn).where(NativeCliTurn.conversation_id == cid, NativeCliTurn.created_by == user_id)
    if before is not None:
        anchor = await db.get(NativeCliTurn, before)
        if anchor is None or anchor.conversation_id != cid or anchor.created_by != user_id:
            raise HTTPException(404, '历史游标不属于当前会话')
        query = query.where(or_(NativeCliTurn.created_at < anchor.created_at,
            and_(NativeCliTurn.created_at == anchor.created_at, NativeCliTurn.id < anchor.id)))
    if client_message_id is not None:
        query = query.where(NativeCliTurn.client_message_id == client_message_id)
    rows = (await db.scalars(query.order_by(NativeCliTurn.created_at.desc(), NativeCliTurn.id.desc()).limit(limit + 1))).all()
    data = []
    for row in rows[:limit]:
        value = await turns.public(db, row)
        session = await db.get(NativeCliSession, row.session_id)
        value['runtime'] = session.runtime if session is not None else None
        data.append(value)
    return {'data': data, 'next_cursor': rows[limit - 1].id if len(rows) > limit else None}


async def activity(db, cid, user_id):
    await authorize(db, cid, user_id)
    execution = await db.get(ConversationExecution, cid, populate_existing=True)
    active = None
    backend = None
    phase = 'idle'
    from .codex_forks import public as fork_public
    seed = await db.scalar(select(NativeCliFork).where(NativeCliFork.conversation_id == cid, NativeCliFork.created_by == user_id))
    native_fork = await fork_public(db, seed) if seed is not None else None
    active_fork = None
    active_session_request = None
    if execution is not None:
        try:
            control.document(execution)
            phase, backend = execution.phase, execution.backend
        except (ValueError, TypeError, KeyError):
            return {'phase': 'evidence_invalid', 'backend': None, 'active_turn': None, 'owned_by_current_user': False}
        if execution.phase != 'idle' and execution.backend == 'codex':
            row = await db.get(NativeCliTurn, execution.resource_id)
            if row is not None and row.conversation_id == cid and row.created_by == user_id:
                active = await turns.public(db, row)
                session = await db.get(NativeCliSession, row.session_id)
                active['runtime'] = session.runtime if session is not None else None
                phase = active['phase']
            else:
                fork = await db.get(NativeCliFork, execution.resource_id)
                if fork is not None and fork.created_by == user_id and cid in {fork.source_conversation_id, fork.conversation_id}:
                    active_fork = await fork_public(db, fork)
                    phase = active_fork['phase']
                else:
                    startup = await db.get(NativeCliSessionRequest, execution.resource_id)
                    if startup is not None and startup.created_by == user_id and startup.conversation_id == cid:
                        from .codex_session_requests import public as startup_public
                        active_session_request = await startup_public(db, startup)
                        phase = active_session_request['phase']
    return {'phase': phase, 'backend': backend, 'active_turn': active, 'active_fork': active_fork,
            'native_fork': native_fork, 'active_session_request': active_session_request,
            'owned_by_current_user': active is not None or active_fork is not None or active_session_request is not None}
